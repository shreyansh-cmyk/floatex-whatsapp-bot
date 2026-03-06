from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import anthropic
import os

app = Flask(__name__)

claude = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

conversations = {}

SYSTEM_PROMPT = """You are a helpful AI assistant for Floatex Solar, India's leading floating solar PV technology company. Help the team with engineering queries, project information, calculations, and general assistance. Be concise since this is WhatsApp. Keep replies under 300 words unless more detail is requested."""

@app.route("/webhook", methods=["POST"])
def webhook():
    incoming_msg = request.form.get("Body", "").strip()
    sender = request.form.get("From", "")
    if not incoming_msg:
        return str(MessagingResponse())
    if sender not in conversations:
        conversations[sender] = []
    conversations[sender].append({"role": "user", "content": incoming_msg})
    if len(conversations[sender]) > 10:
        conversations[sender] = conversations[sender][-10:]
    try:
        response = claude.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=conversations[sender]
        )
        reply = response.content[0].text
        conversations[sender].append({"role": "assistant", "content": reply})
    except Exception as e:
        reply = f"Sorry, error occurred: {str(e)[:50]}"
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

@app.route("/", methods=["GET"])
def health():
    return "Floatex WhatsApp Bot is running", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
