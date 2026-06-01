import os
from flask import Flask, request
from twilio.twiml.messaging_response import MessagingResponse
import google.generativeai as genai

app = Flask(__name__)

# 🔑 Remplace par tes vraies clés
GEMINI_API_KEY = "AQ.Ab8RN6L0YtMdxHjvgbwWdY97zMQ44zi4IdQnxsqOa1Q27j8NsQ"
TWILIO_ACCOUNT_SID = "SKe174f79dc4afea2539256fbfc5d988a5"
TWILIO_AUTH_TOKEN = "j5kTHGUcfFGEamIxAsZzWos0zSvWzotl"

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

SYSTEM_PROMPT = """Tu es la secrétaire virtuelle de la clinique.
Tu gères UNIQUEMENT : les rendez-vous, les horaires, les tarifs et les directions.
Tu ne donnes JAMAIS de conseils médicaux.
Si quelqu'un pose une question médicale, dis :
"Pour toute question médicale, nos médecins seront heureux de vous aider lors de votre consultation."
Réponds toujours en français, de façon chaleureuse et professionnelle."""

conversations = {}

@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    phone = request.form.get("From", "")
    message = request.form.get("Body", "").strip()
    
    if phone not in conversations:
        conversations[phone] = []
    
    conversations[phone].append(f"Patient: {message}")
    history = "\n".join(conversations[phone][-10:])
    
    try:
        prompt = f"{SYSTEM_PROMPT}\n\nHistorique:\n{history}\n\nRéponds maintenant:"
        response = model.generate_content(prompt)
        reply = response.text
    except Exception as e:
        reply = "Désolée, service temporairement indisponible. Appelez-nous directement."
    
    conversations[phone].append(f"Secrétaire: {reply}")
    
    resp = MessagingResponse()
    resp.message(reply)
    return str(resp)

@app.route("/")
def home():
    return "SmartClinique API ✅"

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
