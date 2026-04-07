from flask import Flask, request, send_file
from twilio.twiml.messaging_response import MessagingResponse
from twilio.rest import Client as TwilioClient
from supabase import create_client
import anthropic
import httpx
import base64
import json
import os
import re
import threading

app = Flask(__name__)

@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

# --- Clients ---
claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
twilio_client = TwilioClient(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

BOT_NAME = os.environ.get("BOT_NAME", "Floatex AI")
BOT_NUMBER = os.environ.get("BOT_NUMBER", "")  # e.g. whatsapp:+14155238886

# In-memory conversation history (per sender, for contextual replies)
conversations = {}

# --- Image dedup + batch queue ---
import hashlib
import time as _time

# Track recent image hashes to skip duplicates (same image sent multiple times)
_recent_image_hashes = {}  # hash -> timestamp
_IMAGE_DEDUP_WINDOW = 300  # 5 minutes — skip if same image seen within this window

# Batch queue for image processing
_image_queue = []  # list of {message_id, media_base64, media_type, text, project_id, sender_name, group_name, group_id, queued_at}
_BATCH_INTERVAL = 120  # Process queued images every 2 minutes
_batch_timer = None

# --- System Prompts ---

SYSTEM_PROMPT = """You are the AI assistant for Floatex Solar, India's leading floating solar company (~75% market share, 1GW+ delivered). Clients: L&T, NTPC, GAIL, Sterling Wilson.

WHEN RECEIVING PHOTOS: Analyze for progress, safety, material condition, workmanship. Flag issues. Be concise — bullet points.

WHEN RECEIVING DPR: Extract date, counts, array status. Flag any zero-progress days.

RESPONSE RULES:
- This is WhatsApp. Keep replies under 200 words.
- Use bullet points, not paragraphs.
- Reference project-specific data from the context provided.
- Flag safety concerns prominently.
"""

EXTRACTION_PROMPT = """Analyze this WhatsApp message (and any images) from a Floatex Solar group chat.
Return a JSON object with these fields:

{
  "project_id": "P014 or P013 or P016 etc, or null if unclear",
  "knowledge": [
    {
      "category": "progress|issue|decision|schedule|material|safety|engineering|weather|manpower",
      "fact": "concise extracted fact",
      "confidence": "high|medium|low"
    }
  ],
  "alerts": [
    {
      "severity": "critical|high|medium|low",
      "category": "safety|schedule_delay|quality|material_damage|design_mismatch|budget",
      "title": "short title",
      "description": "what happened and why it matters",
      "target_team": "site|design|management|procurement"
    }
  ]
}

Rules:
- Only extract real, actionable information. Skip greetings, acknowledgements, chit-chat.
- BE VIGILANT about issues. If the image analysis mentions ANY concern, recommendation, missing item, or deviation — even softly worded ones like "check that..." or "ensure..." or "recommend..." — create an alert for it. These "recommendations" often indicate real problems observed.
- Missing hardware (washers, bolts, clamps), incorrect installation, deviations from spec = at minimum MEDIUM alert.
- Missing safety equipment (PPE, life jackets) = HIGH alert.
- critical = immediate danger or major financial impact. high = needs action within 24h. medium = should be addressed soon. low = FYI.
- When in doubt, create the alert. It's better to over-alert than to miss a real issue.
- If the message is truly routine with no extractable info, return empty arrays.
- Return ONLY valid JSON, no markdown, no explanation."""


# --- Helpers ---

def is_duplicate_image(media_base64):
    """Check if this image was already processed recently."""
    # Hash first 10KB of image data (fast, catches exact dupes and near-dupes from same sender)
    sample = media_base64[:10000] if len(media_base64) > 10000 else media_base64
    img_hash = hashlib.md5(sample.encode()).hexdigest()
    now = _time.time()

    # Clean old entries
    expired = [h for h, t in _recent_image_hashes.items() if now - t > _IMAGE_DEDUP_WINDOW]
    for h in expired:
        del _recent_image_hashes[h]

    if img_hash in _recent_image_hashes:
        return True
    _recent_image_hashes[img_hash] = now
    return False


def save_image_to_storage(media_base64, media_type, message_id, project_id):
    """Save image to Supabase Storage and return the file path."""
    try:
        ext = media_type.split("/")[-1] if media_type else "jpg"
        if ext == "jpeg":
            ext = "jpg"
        file_path = f"wa-photos/{project_id or 'unknown'}/{message_id}.{ext}"
        file_bytes = base64.b64decode(media_base64)

        supabase.storage.from_("portal-files").upload(
            file_path, file_bytes,
            {"content-type": media_type or "image/jpeg", "upsert": "true"},
        )
        print(f"[STORAGE] Saved image: {file_path} ({len(file_bytes)} bytes)")
        return file_path
    except Exception as e:
        print(f"[STORAGE] Failed to save image: {e}")
        return None


def fetch_image_as_base64(media_url):
    """Fetch an image from Twilio and return (base64_data, media_type)."""
    response = httpx.get(
        media_url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        follow_redirects=True,
    )
    media_type = response.headers.get("content-type", "image/jpeg")
    b64 = base64.standard_b64encode(response.content).decode("utf-8")
    return b64, media_type


def is_bot_tagged(message_body):
    """Check if the bot was mentioned/tagged in the message."""
    if not message_body:
        return False
    lower = message_body.lower()
    triggers = [BOT_NAME.lower(), "@floatex", "@ai", "@bot", "floatex ai"]
    return any(t in lower for t in triggers)


def is_group_message(sender):
    """Group messages have a different sender format."""
    # Twilio group messages come from the group, with participant info
    return bool(request.form.get("WaId")) and "g.us" in (request.form.get("From", "") or "")


def detect_project_id(text):
    """Try to detect project ID from message text."""
    if not text:
        return None
    match = re.search(r'\b(P\d{3})\b', text, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    # Check project names
    project_map = {
        "tilaiya": "P014", "getalsud": "P013", "gail": "P016",
        "hazira": "P017", "mejia": "P018", "indravati": "P019",
        "gridco": "P019", "ongc": "P017",
    }
    lower = text.lower()
    for keyword, pid in project_map.items():
        if keyword in lower:
            return pid
    return None


def store_message(sender, sender_name, group_id, group_name, body, num_media, media_urls, media_types, message_sid):
    """Store incoming message in Supabase."""
    try:
        result = supabase.table("whatsapp_messages").insert({
            "sender": sender,
            "sender_name": sender_name,
            "group_id": group_id,
            "group_name": group_name,
            "message": body or "",
            "role": "user",
            "num_media": num_media,
            "media_urls": media_urls,
            "media_types": media_types,
            "message_sid": message_sid,
            "project_tag": detect_project_id(body),
            "is_dpr": bool(body and ("dpr" in body.lower() or "daily progress" in body.lower())),
            "bot_responded": False,
            "processed": False,
        }).execute()
        return result.data[0]["id"] if result.data else None
    except Exception as e:
        print(f"Error storing message: {e}")
        return None


def analyze_image_vision(b64_data, media_type, caption="", system_prompt=None):
    """Send image to Claude Vision and return analysis text."""
    prompt = caption or "Analyze this site photo. Focus on: progress, safety issues, material condition. Be concise — bullet points, under 200 words."
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=600,
        system=system_prompt or SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64_data}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    return response.content[0].text


def store_media_analysis(message_id, media_url, media_type, analysis, project_id):
    """Store image analysis in Supabase."""
    # Extract tags from analysis
    tag_keywords = {
        "safety": ["safety", "hazard", "ppe", "danger", "risk", "warning"],
        "progress": ["progress", "installed", "completed", "launched", "towed"],
        "damage": ["damage", "crack", "broken", "bent", "corrosion", "rust"],
        "quality": ["quality", "alignment", "workmanship", "defect", "gap"],
        "weather": ["weather", "rain", "wind", "water level", "flood"],
    }
    analysis_lower = analysis.lower()
    tags = [tag for tag, keywords in tag_keywords.items() if any(k in analysis_lower for k in keywords)]

    try:
        supabase.table("wa_media_analysis").insert({
            "message_id": message_id,
            "media_url": media_url,
            "media_type": media_type,
            "analysis": analysis,
            "tags": tags,
            "project_id": project_id,
        }).execute()
    except Exception as e:
        print(f"Error storing media analysis: {e}")


def extract_and_store_knowledge(message_id, body, image_analyses, project_id, sender, group_name, memory=""):
    """Use Claude to extract knowledge and detect alerts from the message. Text-only — no images to save tokens."""
    # Build context for extraction
    context_parts = []
    if body:
        context_parts.append(f"Message text: {body}")
    for i, analysis in enumerate(image_analyses):
        context_parts.append(f"Image {i+1} analysis: {analysis}")

    if not context_parts:
        return []

    context = "\n".join(context_parts)
    if group_name:
        context = f"From group: {group_name}\nSender: {sender}\n{context}"

    # Inject memory so extraction knows about recurring patterns
    extraction_system = EXTRACTION_PROMPT
    if memory:
        extraction_system = EXTRACTION_PROMPT + "\n\n" + memory

    try:
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=512,
            system=extraction_system,
            messages=[{"role": "user", "content": context}],
        )
        raw = response.content[0].text.strip()
        # Clean markdown code fences if present
        if raw.startswith("```"):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        data = json.loads(raw)
    except Exception as e:
        print(f"Extraction error: {e}")
        return []

    # Use project_id from message detection (validated), fall back to extracted but don't trust it for FK
    extracted_project = project_id  # Only use the one we detected from message text (matches projects table)
    alerts = []

    # Store knowledge
    for item in data.get("knowledge", []):
        try:
            supabase.table("wa_knowledge").insert({
                "message_id": message_id,
                "project_id": extracted_project,
                "category": item["category"],
                "fact": item["fact"],
                "source_sender": sender,
                "source_group": group_name,
                "confidence": item.get("confidence", "medium"),
            }).execute()
        except Exception as e:
            print(f"Error storing knowledge: {e}")

    # Store and dispatch alerts
    for alert in data.get("alerts", []):
        try:
            result = supabase.table("wa_alerts").insert({
                "message_id": message_id,
                "project_id": extracted_project,
                "severity": alert["severity"],
                "category": alert["category"],
                "title": alert["title"],
                "description": alert["description"],
                "target_team": alert["target_team"],
                "source_group": group_name,
            }).execute()
            if result.data:
                alerts.append(result.data[0])
        except Exception as e:
            print(f"Error storing alert: {e}")

    return alerts


def send_proactive_alert(alert):
    """Send alert to relevant contacts via WhatsApp."""
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    alert_severity = severity_rank.get(alert["severity"], 2)

    try:
        contacts = supabase.table("wa_alert_contacts").select("*").eq(
            "team", alert["target_team"]
        ).eq("is_active", True).execute()

        for contact in contacts.data or []:
            contact_min = severity_rank.get(contact.get("min_severity", "medium"), 2)
            if alert_severity > contact_min:
                continue  # Alert not severe enough for this contact

            # Check project filter
            project_ids = contact.get("project_ids")
            if project_ids and alert.get("project_id") and alert["project_id"] not in project_ids:
                continue

            severity_emoji = {"critical": "🚨", "high": "⚠️", "medium": "📋", "low": "ℹ️"}
            emoji = severity_emoji.get(alert["severity"], "📋")

            message_body = (
                f"{emoji} *FLOATEX ALERT — {alert['severity'].upper()}*\n\n"
                f"*{alert['title']}*\n"
                f"{alert['description']}\n\n"
                f"Project: {alert.get('project_id', 'Unknown')}\n"
                f"Team: {alert['target_team']}\n"
                f"Source: {alert.get('source_group', 'Direct message')}"
            )

            try:
                twilio_client.messages.create(
                    from_=BOT_NUMBER,
                    to=contact["phone_number"],
                    body=message_body,
                )
                print(f"Alert sent to {contact['name']} ({contact['phone_number']})")
            except Exception as e:
                print(f"Failed to send alert to {contact['name']}: {e}")

    except Exception as e:
        print(f"Error dispatching alerts: {e}")


def send_group_alert(alert, group_sender):
    """Send alert message back into the originating group."""
    if not group_sender:
        return

    severity_emoji = {"critical": "🚨", "high": "⚠️", "medium": "📋", "low": "ℹ️"}
    emoji = severity_emoji.get(alert["severity"], "📋")

    # Only send in-group alerts for high/critical
    if alert["severity"] not in ("critical", "high"):
        return

    message_body = (
        f"{emoji} *Alert Detected*\n\n"
        f"*{alert['title']}*\n"
        f"{alert['description']}\n\n"
        f"_{alert['target_team'].title()} team has been notified._"
    )

    try:
        twilio_client.messages.create(
            from_=BOT_NUMBER,
            to=group_sender,
            body=message_body,
        )
    except Exception as e:
        print(f"Failed to send group alert: {e}")


# --- Knowledge Retrieval (Compounding Intelligence) ---

MAX_MEMORY_CHARS = 1500  # Hard cap on memory context to control token usage

def build_memory_context(project_id):
    """Fetch accumulated knowledge — optimized for minimal tokens."""
    lines = []

    try:
        # Single batch: fetch key data in parallel via Promise-style
        # 1. Top 10 facts + 5 open alerts + 5 doc summaries — 3 queries instead of 5
        kq = supabase.table("wa_knowledge").select("category, fact").eq("is_current", True).order("extracted_at", desc=True).limit(10)
        if project_id:
            kq = kq.eq("project_id", project_id)
        knowledge = kq.execute().data or []

        aq = supabase.table("wa_alerts").select("severity, title").in_("status", ["new", "acknowledged"]).order("created_at", desc=True).limit(5)
        if project_id:
            aq = aq.eq("project_id", project_id)
        alerts = aq.execute().data or []

        dq = supabase.table("doc_knowledge").select("doc_no, summary").eq("processing_status", "processed").order("created_at", desc=True).limit(8)
        if project_id:
            dq = dq.eq("project_id", project_id)
        docs = dq.execute().data or []

        # Build compact context
        if knowledge:
            lines.append("KNOWN FACTS:")
            for k in knowledge[:8]:
                lines.append(f"- [{k['category']}] {k['fact'][:100]}")

        if alerts:
            lines.append("OPEN ALERTS (don't duplicate):")
            for a in alerts:
                lines.append(f"- [{a['severity']}] {a['title'][:80]}")

        if docs:
            lines.append("PROJECT DOCS:")
            for d in docs:
                lines.append(f"- {d['doc_no']}: {(d.get('summary') or '')[:80]}")

    except Exception as e:
        print(f"Memory context error: {e}")

    if not lines:
        return ""

    result = "\n".join(lines)
    if len(result) > MAX_MEMORY_CHARS:
        result = result[:MAX_MEMORY_CHARS] + "..."
    return result


# --- Background Processing ---

def process_in_background(message_id, incoming_msg, media_urls, media_types, project_id, sender, sender_name, group_id, group_name, is_group):
    """Heavy processing: image analysis, knowledge extraction, alerts — runs in background thread."""
    try:
        # Build memory context from accumulated knowledge
        memory = build_memory_context(project_id)
        enriched_system = SYSTEM_PROMPT
        if memory:
            enriched_system = SYSTEM_PROMPT + "\n\n" + memory
            print(f"[BG] Memory context: {len(memory)} chars injected")

        image_analyses = []

        # Analyze images with Claude Vision — now with accumulated knowledge
        for i, url in enumerate(media_urls):
            if not media_types[i].startswith("image/"):
                continue
            try:
                b64_data, detected_type = fetch_image_as_base64(url)
                analysis = analyze_image_vision(b64_data, detected_type, incoming_msg, system_prompt=enriched_system)
                image_analyses.append(analysis)

                if message_id:
                    store_media_analysis(message_id, url, detected_type, analysis, project_id)
            except Exception as e:
                print(f"[BG] Image analysis failed for {url}: {e}")

        # Extract knowledge + detect alerts (with memory for pattern awareness)
        alerts = []
        if message_id and (incoming_msg or image_analyses):
            alerts = extract_and_store_knowledge(
                message_id, incoming_msg, image_analyses, project_id, sender_name, group_name,
                memory=memory,
            )

        # Dispatch alerts
        for alert in alerts:
            send_proactive_alert(alert)
            if is_group:
                send_group_alert(alert, sender)

        # Mark processed
        if message_id:
            supabase.table("whatsapp_messages").update({"processed": True}).eq("id", message_id).execute()

        print(f"[BG] Processed message {message_id}: {len(image_analyses)} images, {len(alerts)} alerts")

    except Exception as e:
        print(f"[BG] Background processing error: {e}")


# --- Async Reply (via Twilio API, not TwiML) ---

def send_reply_async(message_id, incoming_msg, sender, image_analyses=None, enriched_system=None):
    """Send reply via Twilio API. Reuses Vision analysis for images (no extra API call)."""
    try:
        if image_analyses:
            # Reuse the Vision analysis as the reply — no extra Claude call
            reply = "\n\n".join(image_analyses)
        elif incoming_msg:
            # Text-only: need a Claude call with memory-enriched system prompt
            if sender not in conversations:
                conversations[sender] = []

            conversations[sender].append({"role": "user", "content": incoming_msg})

            if len(conversations[sender]) > 10:
                conversations[sender] = conversations[sender][-10:]

            response = claude.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                system=enriched_system or SYSTEM_PROMPT,
                messages=conversations[sender],
            )
            reply = response.content[0].text
            conversations[sender].append({"role": "assistant", "content": reply})
        else:
            return

        # Truncate for WhatsApp limit
        if len(reply) > 1500:
            reply = reply[:1497] + "..."

        # Send via Twilio API
        twilio_client.messages.create(
            from_=BOT_NUMBER,
            to=sender,
            body=reply,
        )
        print(f"[REPLY] Sent to {sender} ({len(reply)} chars)")

        if message_id:
            supabase.table("whatsapp_messages").update({"bot_responded": True}).eq("id", message_id).execute()

    except Exception as e:
        print(f"[REPLY] Error: {e}")


# --- Main Webhook ---

def process_all_in_background(form_data):
    """All processing in one background thread: store, analyze, extract, reply."""
    incoming_msg = form_data.get("body", "").strip()
    sender = form_data["sender"]
    sender_name = form_data.get("sender_name", "")
    num_media = form_data.get("num_media", 0)
    message_sid = form_data.get("message_sid", "")
    group_id = form_data.get("group_id")
    group_name = form_data.get("group_name")
    media_urls = form_data.get("media_urls", [])
    media_types = form_data.get("media_types", [])
    is_group = bool(group_id)

    try:
        # Store message
        project_id = detect_project_id(incoming_msg)
        message_id = store_message(
            sender, sender_name, group_id, group_name,
            incoming_msg, num_media, media_urls, media_types, message_sid,
        )
        print(f"[BG] Stored message {message_id} from {sender_name}")

        # Build memory context from accumulated knowledge
        memory = build_memory_context(project_id)
        enriched_system = SYSTEM_PROMPT
        if memory:
            enriched_system = SYSTEM_PROMPT + "\n\n" + memory
            print(f"[BG] Memory context: {len(memory)} chars injected")

        # Image analysis with accumulated knowledge
        image_analyses = []
        for i, url in enumerate(media_urls):
            if not media_types[i].startswith("image/"):
                continue
            try:
                b64_data, detected_type = fetch_image_as_base64(url)
                analysis = analyze_image_vision(b64_data, detected_type, incoming_msg, system_prompt=enriched_system)
                image_analyses.append(analysis)
                if message_id:
                    store_media_analysis(message_id, url, detected_type, analysis, project_id)
            except Exception as e:
                print(f"[BG] Image analysis failed for {url}: {e}")

        # Knowledge extraction + alerts (with memory for pattern awareness)
        alerts = []
        if message_id and (incoming_msg or image_analyses):
            alerts = extract_and_store_knowledge(
                message_id, incoming_msg, image_analyses, project_id, sender_name, group_name,
                memory=memory,
            )

        # Silent mode — store alerts in DB but don't send WhatsApp messages
        # Alerts visible on portal WhatsApp Intelligence dashboard
        # for alert in alerts:
        #     send_proactive_alert(alert)
        #     if is_group:
        #         send_group_alert(alert, sender)

        if message_id:
            supabase.table("whatsapp_messages").update({"processed": True}).eq("id", message_id).execute()

        print(f"[BG] Processed: {len(image_analyses)} images, {len(alerts)} alerts")

        # Silent mode — observe and build knowledge, never reply
        # Bot sits in groups, extracts knowledge, stores alerts, but does not send messages
        # To re-enable replies, uncomment the block below:
        # should_reply = not is_group or is_bot_tagged(incoming_msg)
        # if should_reply:
        #     send_reply_async(message_id, incoming_msg, sender, image_analyses=image_analyses or None, enriched_system=enriched_system)

    except Exception as e:
        print(f"[BG] Error: {e}")


@app.route("/webhook", methods=["POST"])
def webhook():
    # Capture all form data immediately, then return
    form_data = {
        "body": request.form.get("Body", ""),
        "sender": request.form.get("From", ""),
        "sender_name": request.form.get("ProfileName", ""),
        "num_media": int(request.form.get("NumMedia", 0)),
        "message_sid": request.form.get("MessageSid", ""),
        "group_id": request.form.get("GroupId", None),
        "group_name": request.form.get("GroupName", None),
        "media_urls": [],
        "media_types": [],
    }

    for i in range(form_data["num_media"]):
        url = request.form.get(f"MediaUrl{i}")
        ctype = request.form.get(f"MediaContentType{i}", "")
        if url:
            form_data["media_urls"].append(url)
            form_data["media_types"].append(ctype)

    if not form_data["body"].strip() and form_data["num_media"] == 0:
        return str(MessagingResponse())

    # Everything in background — return immediately
    threading.Thread(target=process_all_in_background, args=(form_data,), daemon=True).start()

    return str(MessagingResponse())


# --- Document Knowledge Extraction ---

DOC_EXTRACTION_PROMPT = """You are analyzing a document from Floatex Solar's project management portal. Extract structured knowledge.

Return a JSON object:
{
  "summary": "2-3 sentence summary of what this document contains",
  "category": "engineering|procurement|quality|safety|drawing|schedule|contractual",
  "specs": [{"param": "parameter name", "value": "value", "unit": "unit"}],
  "quantities": [{"item": "item name", "qty": "quantity", "unit": "unit"}],
  "vendors": [{"name": "vendor", "item": "what they supply", "price": "if mentioned", "currency": "INR/USD"}],
  "dates": [{"event": "what happens", "date": "YYYY-MM-DD or description", "status": "planned|completed|delayed"}],
  "references": [{"doc_no": "referenced document number", "relationship": "how it relates"}],
  "decisions": [{"decision": "what was decided", "conditions": "any conditions", "approved_by": "who"}],
  "tags": ["relevant", "searchable", "tags"]
}

Rules:
- Extract EVERY technical spec, dimension, rating, material grade, cable size, load value, safety factor you find
- For ordering notes: capture vendor, item, quantity, price, delivery date
- For drawings: capture key dimensions, coordinates, layout parameters
- For analysis reports: capture methodology, key results, safety factors, failure modes
- If a field has no data, use empty array []
- Return ONLY valid JSON"""


def process_document_file(file_id, document_id, filename, file_url):
    """Fetch a document from Supabase storage, extract text, analyze with Claude."""
    try:
        print(f"[DOC] Processing: {filename} (file_id: {file_id})")
        dk_id = None

        # Get document metadata
        doc_result = supabase.table("documents").select("project_id, doc_no, doc_type, title, section, package").eq("id", document_id).maybe_single().execute()
        doc = doc_result.data if doc_result.data else {}
        if not doc:
            raise Exception(f"Document {document_id} not found in DB")
        project_id = doc.get("project_id")
        doc_no = doc.get("doc_no", "")
        doc_type = doc.get("doc_type", "")

        # Create pending record
        dk_result = supabase.table("doc_knowledge").insert({
            "document_id": document_id,
            "document_file_id": file_id,
            "project_id": project_id,
            "doc_no": doc_no,
            "doc_type": doc_type,
            "category": "engineering",
            "processing_status": "processing",
        }).execute()
        dk_id = dk_result.data[0]["id"] if dk_result.data else None

        # Fetch file from Supabase storage via REST API (bypass client quirks)
        sign_response = httpx.post(
            f"{SUPABASE_URL}/storage/v1/object/sign/portal-files/{file_url}",
            headers={"apikey": SUPABASE_SERVICE_KEY, "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}", "Content-Type": "application/json"},
            json={"expiresIn": 300},
            timeout=15,
        )
        if sign_response.status_code != 200:
            raise Exception(f"File not found in storage: {file_url} (HTTP {sign_response.status_code})")
        signed_url = SUPABASE_URL + "/storage/v1" + sign_response.json().get("signedURL", "")

        dl_response = httpx.get(signed_url, follow_redirects=True, timeout=60)
        if dl_response.status_code != 200:
            raise Exception(f"Download failed: HTTP {dl_response.status_code}")
        file_bytes = dl_response.content
        if len(file_bytes) < 100:
            raise Exception(f"File too small ({len(file_bytes)} bytes)")

        is_pdf = filename.lower().endswith(".pdf")
        is_image = any(filename.lower().endswith(ext) for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp"])

        # Build content for Claude
        content = []

        if is_pdf:
            # Send PDF as base64 document to Claude
            b64_data = base64.standard_b64encode(file_bytes).decode("utf-8")
            content.append({
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": b64_data},
            })
        elif is_image:
            # Send as image
            b64_data = base64.standard_b64encode(file_bytes).decode("utf-8")
            ext = filename.rsplit(".", 1)[-1].lower()
            media_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}.get(ext, "image/jpeg")
            content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_type, "data": b64_data},
            })
        else:
            # Skip unsupported formats
            if dk_id:
                supabase.table("doc_knowledge").update({
                    "processing_status": "skipped",
                    "error_message": f"Unsupported file type: {filename}",
                    "processed_at": json_now(),
                }).eq("id", dk_id).execute()
            print(f"[DOC] Skipped unsupported: {filename}")
            return

        context = f"Document: {doc_no}\nTitle: {doc.get('title', '')}\nType: {doc_type}\nSection: {doc.get('section', '')}\nPackage: {doc.get('package', '')}\nFilename: {filename}"

        # Fetch existing knowledge for cross-referencing
        existing = supabase.table("doc_knowledge").select("doc_no, summary, category, specs, tags").eq("project_id", project_id).eq("processing_status", "processed").limit(20).execute()
        if existing.data:
            context += "\n\nOTHER DOCUMENTS ALREADY PROCESSED FOR THIS PROJECT (use for cross-referencing):\n"
            for e in existing.data:
                context += f"- {e['doc_no']}: {e.get('summary', '')[:100]}\n"

        content.append({"type": "text", "text": context})

        # Call Claude
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=2048,
            system=DOC_EXTRACTION_PROMPT,
            messages=[{"role": "user", "content": content}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        data = json.loads(raw)

        # Generate cross-document insights
        cross_insights = []
        if existing.data and data.get("specs"):
            for spec in data["specs"]:
                if not isinstance(spec, dict):
                    continue
                for e in existing.data:
                    e_specs = e.get("specs") or []
                    if isinstance(e_specs, str):
                        try: e_specs = json.loads(e_specs)
                        except: e_specs = []
                    for es in e_specs:
                        if not isinstance(es, dict):
                            continue
                        if es.get("param") == spec.get("param") and es.get("value") != spec.get("value"):
                            cross_insights.append(f"{spec['param']}: {doc_no} says {spec.get('value')}{spec.get('unit','')} vs {e['doc_no']} says {es.get('value')}{es.get('unit','')}")

        # Update record
        if dk_id:
            supabase.table("doc_knowledge").update({
                "summary": data.get("summary", ""),
                "category": data.get("category", "engineering"),
                "specs": json.dumps(data.get("specs", [])),
                "quantities": json.dumps(data.get("quantities", [])),
                "vendors": json.dumps(data.get("vendors", [])),
                "dates": json.dumps(data.get("dates", [])),
                "references": json.dumps(data.get("references", [])),
                "decisions": json.dumps(data.get("decisions", [])),
                "cross_doc_insights": cross_insights[:10],
                "tags": data.get("tags", []),
                "raw_text_length": len(raw),
                "processing_status": "processed",
                "processed_at": json_now(),
            }).eq("id", dk_id).execute()

        print(f"[DOC] Done: {doc_no} — {data.get('category', '?')} — {len(data.get('specs', []))} specs, {len(data.get('quantities', []))} quantities")

    except Exception as e:
        import traceback
        print(f"[DOC] Error processing {filename}: {e}")
        traceback.print_exc()
        if dk_id:
            supabase.table("doc_knowledge").update({
                "processing_status": "failed",
                "error_message": str(e)[:500],
                "processed_at": json_now(),
            }).eq("id", dk_id).execute()


def json_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


@app.route("/process-document", methods=["POST"])
def process_document():
    """Webhook endpoint called when a new document file is uploaded."""
    data = request.get_json(silent=True) or {}
    file_id = data.get("file_id")
    document_id = data.get("document_id")
    filename = data.get("filename", "")
    file_url = data.get("file_url", "")

    if not file_id or not document_id:
        return {"error": "file_id and document_id required"}, 400

    # Process in background
    threading.Thread(
        target=process_document_file,
        args=(file_id, document_id, filename, file_url),
        daemon=True,
    ).start()

    return {"status": "processing", "file_id": file_id}, 202


@app.route("/process-all-documents", methods=["POST"])
def process_all_documents():
    """Bulk process all existing document files that haven't been processed yet."""
    # Get all files that don't have a doc_knowledge record
    files = supabase.table("document_files").select("id, document_id, filename, file_url").execute()
    processed = supabase.table("doc_knowledge").select("document_file_id").execute()
    processed_ids = set(r["document_file_id"] for r in (processed.data or []))

    unprocessed = [f for f in (files.data or []) if f["id"] not in processed_ids]
    print(f"[DOC] Bulk processing {len(unprocessed)} unprocessed files")

    def process_batch():
        import time
        for i, f in enumerate(unprocessed):
            print(f"[DOC] Batch {i+1}/{len(unprocessed)}: {f['filename']}")
            process_document_file(f["id"], f["document_id"], f["filename"], f["file_url"])
            time.sleep(90)  # 1 doc per 90s — PDFs can be large, 10k tokens/min limit

    threading.Thread(target=process_batch, daemon=True).start()

    return {"status": "processing", "total": len(unprocessed)}, 202


# --- Skill Execution API ---

@app.route("/api/skills/<skill_slug>", methods=["POST"])
def execute_skill(skill_slug):
    """Execute a skill with project context and learned knowledge."""
    data = request.get_json(silent=True) or {}
    project_id = data.get("project_id")
    triggered_by = data.get("triggered_by", "portal")
    input_data = data.get("input", {})

    # Fetch skill
    skill_result = supabase.table("skills").select("*").eq("slug", skill_slug).eq("is_active", True).single().execute()
    if not skill_result.data:
        return {"error": f"Skill '{skill_slug}' not found"}, 404
    skill = skill_result.data

    # Create execution record
    exec_result = supabase.table("skill_executions").insert({
        "skill_id": skill["id"],
        "project_id": project_id,
        "triggered_by": triggered_by,
        "input_data": input_data,
        "status": "running",
    }).execute()
    exec_id = exec_result.data[0]["id"] if exec_result.data else None

    # Run in background
    threading.Thread(
        target=run_skill,
        args=(exec_id, skill, project_id, input_data),
        daemon=True,
    ).start()

    return {"status": "running", "execution_id": exec_id}, 202


def run_skill(exec_id, skill, project_id, input_data):
    """Execute a skill with full context injection."""
    import time
    start = time.time()

    try:
        # 1. Build system prompt: cap base skill at 10K chars to control tokens
        base = skill["base_prompt"]
        if len(base) > 10000:
            base = base[:10000] + "\n\n[... skill content truncated for token efficiency ...]"
        system = base
        if skill.get("learned_context"):
            learned = skill["learned_context"][:2000]
            system += "\n\n--- LEARNED FROM RECENT DOCUMENTS ---\n" + learned

        # 2. Fetch project context from Supabase
        context_parts = []

        if project_id:
            # Project data
            proj = supabase.table("projects").select("*").eq("id", project_id).single().execute()
            if proj.data:
                context_parts.append(f"PROJECT: {proj.data.get('id')} — {proj.data.get('name')} ({proj.data.get('full_name','')})")
                context_parts.append(f"GWM Ref: {proj.data.get('gwm_ref','')} | EPC: {proj.data.get('epc','')} | Consultant: {proj.data.get('epc_consultant_org','')}")

            # Relevant doc_knowledge
            dk = supabase.table("doc_knowledge").select("doc_no, summary, category, specs, decisions").eq("project_id", project_id).eq("processing_status", "processed").limit(20).execute()
            dk_ids = []
            if dk.data:
                context_parts.append("\nPROJECT DOCUMENTS KNOWLEDGE:")
                for d in dk.data:
                    dk_ids.append(d.get("id"))
                    context_parts.append(f"  [{d['doc_no']}] ({d['category']}): {(d.get('summary') or '')[:150]}")
                    specs = d.get("specs") or []
                    if isinstance(specs, str):
                        try: specs = json.loads(specs)
                        except: specs = []
                    for s in specs[:3]:
                        context_parts.append(f"    - {s.get('param','')}: {s.get('value','')} {s.get('unit','')}")

            # Relevant wa_knowledge
            wk = supabase.table("wa_knowledge").select("category, fact").eq("is_current", True).limit(15).execute()
            if wk.data:
                context_parts.append("\nFIELD OBSERVATIONS (from WhatsApp):")
                for w in wk.data:
                    context_parts.append(f"  [{w['category']}] {w['fact']}")

            # Recent alerts
            alerts = supabase.table("wa_alerts").select("severity, title").in_("status", ["new", "acknowledged"]).limit(5).execute()
            if alerts.data:
                context_parts.append("\nACTIVE ALERTS:")
                for a in alerts.data:
                    context_parts.append(f"  [{a['severity'].upper()}] {a['title']}")

        # 3. Build user message
        user_message = "\n".join(context_parts) if context_parts else ""
        if input_data:
            user_message += "\n\nUSER INPUT:\n" + json.dumps(input_data, indent=2)

        # 4. Call Claude
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user_message}],
        )
        output_text = response.content[0].text
        tokens = (response.usage.input_tokens or 0) + (response.usage.output_tokens or 0)

        elapsed = int((time.time() - start) * 1000)

        # 5. Try to extract structured data
        output_structured = None
        try:
            # Look for JSON block in output
            if "```json" in output_text:
                json_match = re.search(r'```json\s*(.*?)\s*```', output_text, re.DOTALL)
                if json_match:
                    output_structured = json.loads(json_match.group(1))
        except:
            pass

        # 6. Update execution record
        if exec_id:
            supabase.table("skill_executions").update({
                "output_text": output_text,
                "output_structured": json.dumps(output_structured) if output_structured else None,
                "doc_knowledge_ids": dk_ids[:10] if project_id else [],
                "tokens_used": tokens,
                "execution_time_ms": elapsed,
                "status": "completed",
            }).eq("id", exec_id).execute()

        # 7. Check if skill should learn from this execution
        check_skill_learning(skill, output_text, input_data, project_id)

        print(f"[SKILL] {skill['slug']} completed — {tokens} tokens, {elapsed}ms")

    except Exception as e:
        print(f"[SKILL] Error: {e}")
        if exec_id:
            supabase.table("skill_executions").update({
                "status": "failed",
                "error_message": str(e)[:500],
            }).eq("id", exec_id).execute()


def check_skill_learning(skill, output_text, input_data, project_id):
    """After a skill runs, check if the output contains patterns worth learning."""
    try:
        # Ask Haiku to extract learnable patterns (cheaper, fast enough for this)
        response = claude.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            system="You extract reusable patterns from skill execution outputs. Return JSON array of learnings, or empty array if nothing new.",
            messages=[{"role": "user", "content": f"Skill: {skill['slug']}\nInput: {json.dumps(input_data)[:500]}\nOutput excerpt: {output_text[:2000]}\n\nExtract any new reusable patterns (comment templates, formulas, vendor data, workflow steps) that should be remembered for next time. Return: [{{'type': 'comment_pattern|spec_update|formula|vendor_data|workflow_change', 'content': 'the learning'}}] or []"}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = re.sub(r'^```(?:json)?\s*', '', raw)
            raw = re.sub(r'\s*```$', '', raw)
        learnings = json.loads(raw)

        for l in (learnings or []):
            if l.get("content"):
                supabase.table("skill_learnings").insert({
                    "skill_id": skill["id"],
                    "source_doc_no": input_data.get("document_no", ""),
                    "learning_type": l.get("type", "spec_update"),
                    "learning_content": l["content"],
                }).execute()

        # Periodically merge learnings into skill's learned_context
        pending = supabase.table("skill_learnings").select("id, learning_content").eq("skill_id", skill["id"]).eq("applied", False).limit(20).execute()
        if pending.data and len(pending.data) >= 3:
            new_context = "\n".join(f"- {p['learning_content']}" for p in pending.data)
            existing = skill.get("learned_context", "") or ""
            merged = (existing + "\n" + new_context).strip()
            # Keep learned_context under 5000 chars
            if len(merged) > 5000:
                merged = merged[-5000:]
            supabase.table("skills").update({
                "learned_context": merged,
                "version": (skill.get("version", 1) or 1) + 1,
                "updated_at": json_now(),
            }).eq("id", skill["id"]).execute()
            # Mark as applied
            for p in pending.data:
                supabase.table("skill_learnings").update({"applied": True}).eq("id", p["id"]).execute()
            print(f"[SKILL] {skill['slug']} learned {len(pending.data)} new patterns (v{skill.get('version',1)+1})")

    except Exception as e:
        print(f"[SKILL] Learning check error: {e}")


@app.route("/api/skills", methods=["GET"])
def list_skills():
    """List all active skills."""
    result = supabase.table("skills").select("id, slug, name, description, category, version, input_schema, updated_at").eq("is_active", True).execute()
    return {"skills": result.data or []}, 200


@app.route("/api/skills/<skill_slug>/executions", methods=["GET"])
def list_executions(skill_slug):
    """List recent executions for a skill."""
    skill = supabase.table("skills").select("id").eq("slug", skill_slug).single().execute()
    if not skill.data:
        return {"error": "Skill not found"}, 404
    result = supabase.table("skill_executions").select("*").eq("skill_id", skill.data["id"]).order("created_at", desc=True).limit(20).execute()
    return {"executions": result.data or []}, 200


@app.route("/api/generate-doc/<template_slug>", methods=["POST"])
def generate_document(template_slug):
    """Generate a .docx document from a template."""
    from doc_templates import TEMPLATES

    if template_slug not in TEMPLATES:
        return {"error": f"Template '{template_slug}' not found. Available: {list(TEMPLATES.keys())}"}, 404

    data = request.get_json(silent=True) or {}
    project_id = data.get("project_id")

    if not project_id:
        return {"error": "project_id required"}, 400

    # Fetch project data
    proj_result = supabase.table("projects").select("*").eq("id", project_id).single().execute()
    if not proj_result.data:
        return {"error": f"Project '{project_id}' not found"}, 404

    project = proj_result.data
    template = TEMPLATES[template_slug]

    try:
        buffer, doc_no = template["generator"](project)
        filename = template["filename_pattern"].format(pid=project_id)

        return send_file(
            buffer,
            as_attachment=True,
            download_name=filename,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    except Exception as e:
        return {"error": str(e)}, 500


@app.route("/api/templates", methods=["GET"])
def list_templates():
    """List available document templates."""
    from doc_templates import TEMPLATES
    return {"templates": [{"slug": k, "title": v["title"]} for k, v in TEMPLATES.items()]}, 200


@app.route("/api/parse-nit", methods=["POST", "OPTIONS"])
def parse_nit():
    """Parse NIT/tender PDFs: pdfplumber for text, Claude Vision for scanned docs."""
    if request.method == "OPTIONS":
        return "", 204

    import pdfplumber
    import io

    data = request.get_json(silent=True) or {}
    pdfs = data.get("pdfs", [])  # List of {filename, data (base64)}
    text_override = data.get("text", "")  # Direct text input (for testing)

    if not pdfs and not text_override:
        return {"error": "No PDFs provided."}, 400

    NIT_PROMPT = """You are an expert floating solar PV engineer at Floatex Solar reading NIT/tender documents.

Extract ALL DBR-relevant fields. Return ONLY valid JSON (no markdown, no wrapper):

{
  "project_name": "string or null",
  "capacity_ac_mw": "number or null",
  "capacity_dc_mwp": "number or null",
  "design_life": "number or null",
  "client_name": "short name or null",
  "client_full_name": "full legal name or null",
  "epc_name": "string or null",
  "owner_water_body": "string or null",
  "owner_project": "string or null",
  "site_name": "string or null",
  "state": "string or null",
  "country": "India",
  "nearest_town": "with distance or null",
  "nearest_railway": "with distance or null",
  "nearest_airport": "with distance or null",
  "latitude": "decimal degrees or null",
  "longitude": "decimal degrees or null",
  "frl": "meters number or null",
  "mddl": "meters number or null",
  "mwl": "meters number or null",
  "seismic_zone": "Zone II/III/IV/V or null",
  "vb": "wind speed m/s number or null",
  "k1": "number or null — CRITICAL: check if NIT overrides IS 875 default 0.92",
  "k2": "number or null — CRITICAL: check if NIT overrides IS 875 default 1.0",
  "k3": "number or null",
  "k4": "number or null",
  "nit_min_design_wind_pressure": "N/m² number or null",
  "nit_anchor_wind_reduction": "% number or null",
  "wave_height": "meters or null",
  "current_velocity": "m/s or null",
  "module_manufacturer": "string or null",
  "module_wattage": "Wp number or null",
  "module_length": "mm number or null",
  "module_width": "mm number or null",
  "module_height": "mm number or null",
  "module_weight": "kg number or null",
  "module_tilt": "degrees number or null",
  "inverter_type": "SCB/String Inverter/Central Inverter or null",
  "inverter_model": "string or null",
  "inverter_rating": "string or null",
  "inverter_weight": "kg or null",
  "nit_concrete_grade": "M25/M30/M35 or null",
  "nit_hdg_micron": "80/110 number or null",
  "nit_corrosion_category": "C2/C3/C4/C5 or null",
  "nit_handrail_material": "SS 304/SS 316 or null",
  "nit_weld_mesh_gsm": "80/120 number or null",
  "nit_fastener_material": "SS 304/SS 316 or null",
  "nit_clamp_material": "string or null",
  "nit_clamp_coating": "string or null",
  "ref_docs": [{"title": "string", "doc_no": "string or null"}],
  "notes": ["important observations for the engineer"]
}

RULES: Use null for missing fields. Do NOT guess. Convert DMS to decimal. Flag k-factor overrides in notes."""

    try:
        all_text = text_override or ""
        scanned_pdfs = []  # PDFs that need Vision
        mode = "text"

        # Step 1: Try pdfplumber text extraction on each PDF
        for pdf_item in pdfs:
            filename = pdf_item.get("filename", "unknown.pdf")
            pdf_bytes = base64.b64decode(pdf_item["data"])

            try:
                with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                    doc_text = ""
                    for page in pdf.pages:
                        t = page.extract_text()
                        if t:
                            doc_text += t + "\n"

                    avg_chars = len(doc_text.strip()) / max(len(pdf.pages), 1)

                    if avg_chars > 50:
                        # Good text extraction
                        all_text += f"\n\n--- {filename} ---\n\n{doc_text}"
                    else:
                        # Scanned PDF — needs Vision
                        scanned_pdfs.append(pdf_item)
            except Exception:
                scanned_pdfs.append(pdf_item)

        # Step 2: Build Claude request
        content_blocks = []

        if all_text.strip() and len(all_text.strip()) > 50:
            content_blocks.append({
                "type": "text",
                "text": f"NIT/tender document text:\n\n{all_text[:80000]}"
            })

        if scanned_pdfs:
            mode = "vision"
            for pdf_item in scanned_pdfs[:5]:  # Cap at 5 PDFs for Vision
                content_blocks.append({
                    "type": "text",
                    "text": f"\n--- Scanned PDF: {pdf_item.get('filename', 'document')} ---"
                })
                content_blocks.append({
                    "type": "document",
                    "source": {
                        "type": "base64",
                        "media_type": "application/pdf",
                        "data": pdf_item["data"],
                    }
                })

        if not content_blocks:
            return {"error": "No readable content found in uploaded PDFs."}, 400

        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4000,
            messages=[{"role": "user", "content": content_blocks}],
            system=NIT_PROMPT,
        )

        result_text = response.content[0].text.strip()
        if result_text.startswith("```"):
            result_text = re.sub(r'^```\w*\n?', '', result_text)
            result_text = re.sub(r'\n?```$', '', result_text)

        parsed = json.loads(result_text)

        return {
            "status": "ok",
            "fields": parsed,
            "tokens_used": response.usage.input_tokens + response.usage.output_tokens,
            "mode": mode,
            "scanned_count": len(scanned_pdfs),
            "text_count": len(pdfs) - len(scanned_pdfs),
        }, 200

    except json.JSONDecodeError as e:
        return {"error": f"JSON parse error: {str(e)}", "raw": result_text[:500]}, 500
    except Exception as e:
        return {"error": f"NIT parsing failed: {str(e)}"}, 500


def process_image_batch():
    """Process queued images in batch — runs every BATCH_INTERVAL seconds."""
    global _batch_timer, _image_queue

    if not _image_queue:
        _batch_timer = threading.Timer(_BATCH_INTERVAL, process_image_batch)
        _batch_timer.daemon = True
        _batch_timer.start()
        return

    batch = _image_queue[:]
    _image_queue = []
    print(f"[BATCH] Processing {len(batch)} queued images...")

    for item in batch:
        try:
            memory = build_memory_context(item["project_id"])
            enriched_system = SYSTEM_PROMPT
            if memory:
                enriched_system = SYSTEM_PROMPT + "\n\n" + memory

            # Save image to Supabase Storage
            storage_path = save_image_to_storage(
                item["media_base64"], item["media_type"],
                item["message_id"], item["project_id"],
            )
            storage_url = f"portal-files/{storage_path}" if storage_path else "baileys://local"

            # Analyze with Claude Vision
            analysis = analyze_image_vision(
                item["media_base64"], item["media_type"],
                item["text"], system_prompt=enriched_system,
            )

            # Store analysis with actual storage URL
            if item["message_id"]:
                store_media_analysis(
                    item["message_id"], storage_url,
                    item["media_type"], analysis, item["project_id"],
                )

            # Extract knowledge
            if item["message_id"]:
                extract_and_store_knowledge(
                    item["message_id"], item["text"], [analysis],
                    item["project_id"], item["sender_name"], item["group_name"],
                    memory=memory,
                )

            # Mark processed
            if item["message_id"]:
                supabase.table("whatsapp_messages").update({"processed": True}).eq("id", item["message_id"]).execute()

            print(f"[BATCH] Done: msg {item['message_id']} from {item['sender_name']}")
            _time.sleep(2)  # Small delay between images to avoid API rate limits

        except Exception as e:
            print(f"[BATCH] Error processing image: {e}")
            if item.get("message_id"):
                supabase.table("whatsapp_messages").update({"processed": True}).eq("id", item["message_id"]).execute()

    # Schedule next batch
    _batch_timer = threading.Timer(_BATCH_INTERVAL, process_image_batch)
    _batch_timer.daemon = True
    _batch_timer.start()


# Start batch timer on app load
def start_batch_timer():
    global _batch_timer
    _batch_timer = threading.Timer(_BATCH_INTERVAL, process_image_batch)
    _batch_timer.daemon = True
    _batch_timer.start()
    print(f"[BATCH] Image batch processor started (every {_BATCH_INTERVAL}s)")

start_batch_timer()


@app.route("/api/wa-message", methods=["POST", "OPTIONS"])
def wa_message():
    """Receive messages from WA bridge — process text immediately, queue images for batch."""
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    sender = data.get("sender", "")
    sender_name = data.get("sender_name", "")
    group_id = data.get("group_id")
    group_name = data.get("group_name")
    media_type = data.get("media_type")
    media_base64 = data.get("media_base64")
    message_id_ext = data.get("message_id", "")

    if not text and not media_base64:
        return {"status": "skipped", "reason": "empty"}, 200

    has_image = media_base64 and media_type and media_type.startswith("image")

    # Check for duplicate images
    if has_image and is_duplicate_image(media_base64):
        print(f"[DEDUP] Skipping duplicate image from {sender_name}")
        return {"status": "skipped", "reason": "duplicate_image"}, 200

    try:
        # Detect project
        project_id = detect_project_id(text or "")

        # Store message
        message_id = store_message(
            sender, sender_name, group_id, group_name,
            text, 1 if has_image else 0,
            [], [media_type] if media_type else [],
            message_id_ext,
        )

        if has_image:
            # Queue image for batch processing (saves tokens by batching)
            _image_queue.append({
                "message_id": message_id,
                "media_base64": media_base64,
                "media_type": media_type,
                "text": text,
                "project_id": project_id,
                "sender_name": sender_name,
                "group_name": group_name,
                "group_id": group_id,
                "queued_at": _time.time(),
            })
            print(f"[QUEUE] Image queued from {sender_name} ({len(_image_queue)} in queue)")

            return {
                "status": "queued",
                "message_id": message_id,
                "project_id": project_id,
                "queue_size": len(_image_queue),
            }, 200

        else:
            # Text-only: process immediately (cheap — just Haiku)
            if message_id and text:
                memory = build_memory_context(project_id)
                extract_and_store_knowledge(
                    message_id, text, [], project_id, sender_name, group_name,
                    memory=memory,
                )

            if message_id:
                supabase.table("whatsapp_messages").update({"processed": True}).eq("id", message_id).execute()

            return {
                "status": "ok",
                "message_id": message_id,
                "project_id": project_id,
                "images_analyzed": 0,
            }, 200

    except Exception as e:
        print(f"[WA-BRIDGE] Error: {e}")
        return {"error": str(e)}, 500


@app.route("/", methods=["GET"])
def health():
    return "Floatex Intelligence Platform — Bot + Docs + Skills", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
