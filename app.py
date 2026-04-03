from flask import Flask, request
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

# --- System Prompts ---

SYSTEM_PROMPT = """You are the official AI assistant for Floatex Solar Private Limited (CIN: U28100DL2019PTC352379), India's leading floating solar PV technology company with ~75% domestic market share and 1+ GW delivered capacity.

COMPANY OVERVIEW:
- Floatex operates as a technology/IP licensor and EPC specialist for floating solar
- Major clients: L&T, Tata Power, NTPC, Sterling Wilson
- International expansion: Middle East, Africa, APAC, RAK/Dubai entity

ACTIVE PROJECTS:
1. P014 Tilaiya 155MW (GVREL): 42 arrays, 4,244 anchor points, 210,975m total mooring rope. UTM Zone 45N. Four anchor block types (3.77, 2.76, 3.53, 2.59 MT). Case 1 high current (1.32 m/s) for arrays B12, B24, B38.
2. P013 Getalsud 100MW (L&T): 14 arrays (B01-B14), 2,576 anchors, 166,359.5m mooring rope.
3. P016 GAIL-PPL 17.5MW: Active project.
4. ONGC Hazira 10MW: Total bid Rs 39.5 crores. Sludge removal 487,500 m3 dominates civil costs.
5. DVC Mejia 14MW: Active project.
6. GRIDCO 225MW Upper Indravati (Odisha): Consortium with Jakson Green (Floatex 26% / Jakson 74%). Floatex pricing Rs 76 lakhs/MW DC.

KEY ENGINEERING DATA:
- Aisle Float buoyancy: 74 kg
- Hardware multipliers: modules x2 (clamps), x2.67 (bolts), x4.67 (washers)
- Mooring formula P014: rope length = 3.23 x WD + 0.35
- Mooring formula P013: rope length = sqrt(HD squared + WD squared)
- Target mooring angle: 17.8-18.2 degrees (P014), 15-18 degrees (P013)
- IFP platforms: Ferrocement barge design, GWM is external marine engineering consultant
- Destructive test data: first crack at 8T, ultimate failure at 28T

MANUFACTURING:
- Raipur facility: 15+ machines producing floats and accessories
- Production achievement improved from ~73% (early 2025) to ~91% (late 2025)

WHEN RECEIVING A DAILY PROGRESS REPORT (DPR):
- Extract: date, modules installed today, total modules installed, MW completed, percent complete
- List array-by-array status (launching done / towing done)
- Flag any in-progress arrays
- Note damage count, manpower deployed
- Summarize tomorrow plan
- Keep response concise and structured

SITE INSPECTION (when receiving photos):
- Analyze the image for: structural integrity, safety hazards, installation progress, material condition, workmanship quality
- For floating solar: check float alignment, mooring lines, panel orientation, cable routing, water conditions
- Flag any safety concerns (missing PPE, exposed wiring, unstable structures)
- Note weather/environmental conditions visible in the photo
- If multiple images are sent, analyze each and provide a combined report
- Compare against known project specs when possible

RESPONSE STYLE:
- Be concise - this is WhatsApp, keep replies under 300 words unless more detail is requested
- Use emojis sparingly for status indicators
- For calculations, show the formula and working
- For project queries, reference the specific project by name
- Always be professional and helpful to the site and engineering team
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
    prompt = caption or "Analyze this site photo from a floating solar project. Identify progress, issues, safety concerns, material conditions, and any notable observations."
    response = claude.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1024,
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
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
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

def build_memory_context(project_id):
    """Fetch accumulated knowledge from Supabase and build a context string for Claude."""
    sections = []

    try:
        # 1. Recent knowledge for this project (or all if no project)
        kq = supabase.table("wa_knowledge").select("category, fact, confidence, extracted_at").eq("is_current", True).order("extracted_at", desc=True).limit(30)
        if project_id:
            kq = kq.eq("project_id", project_id)
        knowledge = kq.execute().data or []

        if knowledge:
            # Group by category
            by_cat = {}
            for k in knowledge:
                cat = k["category"]
                if cat not in by_cat:
                    by_cat[cat] = []
                by_cat[cat].append(k["fact"])

            lines = ["ACCUMULATED KNOWLEDGE FROM PAST OBSERVATIONS:"]
            for cat, facts in by_cat.items():
                lines.append(f"\n[{cat.upper()}]")
                for f in facts[:5]:  # Max 5 per category to limit tokens
                    lines.append(f"- {f}")
            sections.append("\n".join(lines))

        # 2. Recent open alerts — so Claude knows what's already flagged
        aq = supabase.table("wa_alerts").select("severity, category, title, status").in_("status", ["new", "acknowledged"]).order("created_at", desc=True).limit(10)
        if project_id:
            aq = aq.eq("project_id", project_id)
        alerts = aq.execute().data or []

        if alerts:
            lines = ["\nCURRENTLY OPEN ALERTS (already flagged, don't duplicate):"]
            for a in alerts:
                lines.append(f"- [{a['severity'].upper()}] {a['title']} ({a['status']})")
            sections.append("\n".join(lines))

        # 3. Pattern detection — recurring issues
        pq = supabase.table("wa_alerts").select("category, title").order("created_at", desc=True).limit(50)
        if project_id:
            pq = pq.eq("project_id", project_id)
        all_alerts = pq.execute().data or []

        if all_alerts:
            # Count by category
            cat_counts = {}
            for a in all_alerts:
                cat = a["category"]
                cat_counts[cat] = cat_counts.get(cat, 0) + 1

            recurring = {c: n for c, n in cat_counts.items() if n >= 2}
            if recurring:
                lines = ["\nRECURRING PATTERNS (pay extra attention to these):"]
                for cat, count in sorted(recurring.items(), key=lambda x: -x[1]):
                    lines.append(f"- {cat}: flagged {count} times — this is a repeat issue")
                sections.append("\n".join(lines))

        # 4. Recent photo analysis summaries — what's been seen before
        mq = supabase.table("wa_media_analysis").select("tags, analysis").order("created_at", desc=True).limit(5)
        if project_id:
            mq = mq.eq("project_id", project_id)
        recent_photos = mq.execute().data or []

        if recent_photos:
            all_tags = {}
            for p in recent_photos:
                for t in (p.get("tags") or []):
                    all_tags[t] = all_tags.get(t, 0) + 1
            if all_tags:
                lines = ["\nRECENT INSPECTION TRENDS (from last " + str(len(recent_photos)) + " photos):"]
                for tag, count in sorted(all_tags.items(), key=lambda x: -x[1]):
                    lines.append(f"- {tag}: observed in {count}/{len(recent_photos)} inspections")
                sections.append("\n".join(lines))

    except Exception as e:
        print(f"Error building memory context: {e}")

    if not sections:
        return ""

    return "\n\n".join(sections) + "\n\nUse this accumulated knowledge to inform your analysis. Compare against past observations. Flag if something is getting worse or is a new issue not seen before."


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
                max_tokens=1024,
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

        for alert in alerts:
            send_proactive_alert(alert)
            if is_group:
                send_group_alert(alert, sender)

        if message_id:
            supabase.table("whatsapp_messages").update({"processed": True}).eq("id", message_id).execute()

        print(f"[BG] Processed: {len(image_analyses)} images, {len(alerts)} alerts")

        # Reply if needed
        should_reply = not is_group or is_bot_tagged(incoming_msg)
        if should_reply:
            send_reply_async(message_id, incoming_msg, sender, image_analyses=image_analyses or None, enriched_system=enriched_system)

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


@app.route("/", methods=["GET"])
def health():
    return "Floatex WhatsApp Bot is running (v2 — Vision + Knowledge Base)", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
