from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import httpx
import base64
import os

app = Flask(__name__)

claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")

conversations = {}

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


@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "")
    num_media = int(request.form.get("NumMedia", 0))

    if not incoming_msg and num_media == 0:
        return str(MessagingResponse())

    if sender not in conversations:
        conversations[sender] = []

    # Build message content (text + images)
    content = []

    for i in range(num_media):
        media_url = request.form.get(f"MediaUrl{i}")
        media_content_type = request.form.get(f"MediaContentType{i}", "image/jpeg")
        if media_url and media_content_type.startswith("image/"):
            try:
                b64_data, detected_type = fetch_image_as_base64(media_url)
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": detected_type,
                        "data": b64_data,
                    },
                })
            except Exception as e:
                print(f"Failed to fetch image {i}: {e}")

    if incoming_msg:
        content.append({"type": "text", "text": incoming_msg})
    elif content:
        # Image with no caption — ask for inspection analysis
        content.append({
            "type": "text",
            "text": "Analyze this site photo. Identify progress, issues, safety concerns, and any observations.",
        })

    if not content:
        return str(MessagingResponse())

    conversations[sender].append({"role": "user", "content": content})

    if len(conversations[sender]) > 10:
        conversations[sender] = conversations[sender][-10:]

    try:
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=conversations[sender],
        )
        reply = response.content[0].text

        conversations[sender].append({
            "role": "assistant",
            "content": reply,
        })

    except Exception as e:
        print(f"Claude API error: {e}")
        reply = "Sorry, I encountered an error. Please try again."

    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)


@app.route("/", methods=["GET"])
def health():
    return "Floatex WhatsApp Bot is running", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)

