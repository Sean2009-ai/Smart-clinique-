from flask import Flask, request, jsonify
from flask_cors import CORS
import google.generativeai as genai
import requests
import sqlite3
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)

# ===== CONFIG =====
GEMINI_API_KEY     = "AQ.Ab8RN6L0YtMdxHjvgbwWdY97zMQ44zi4IdQnxsqOa1Q27j8NsQ"
GREEN_API_INSTANCE = "7107643519"
GREEN_API_TOKEN    = "b244010ecf624ab1a55a02b9b81e61ac703f62f003514130a3"
GREEN_API_BASE     = f"https://api.green-api.com/waInstance{GREEN_API_INSTANCE}"

genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.5-flash")

DB_PATH = "smartclinique.db"

# ===== BASE DE DONNÉES =====

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS clinic (
            id      INTEGER PRIMARY KEY,
            name    TEXT    NOT NULL,
            phone   TEXT,
            address TEXT,
            hours   TEXT,
            prices  TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS patients (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL,
            phone      TEXT    UNIQUE NOT NULL,
            created_at TEXT    DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_id INTEGER REFERENCES patients(id),
            name       TEXT,
            phone      TEXT,
            type       TEXT,
            date       TEXT,
            time       TEXT,
            status     TEXT    DEFAULT 'en attente',
            created_at TEXT    DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            phone      TEXT    NOT NULL,
            role       TEXT    NOT NULL,
            message    TEXT    NOT NULL,
            created_at TEXT    DEFAULT (datetime('now'))
        )
    """)

    # Ligne de config clinique vide si elle n'existe pas
    c.execute("INSERT OR IGNORE INTO clinic (id, name) VALUES (1, 'Ma Clinique')")

    conn.commit()
    conn.close()
    print("✅ Base de données initialisée")


# ===== HELPERS =====

def get_clinic_info():
    conn = get_db()
    row = conn.execute("SELECT * FROM clinic WHERE id=1").fetchone()
    conn.close()
    if row:
        return dict(row)
    return {}

def get_conversation_history(phone):
    conn = get_db()
    rows = conn.execute(
        "SELECT role, message FROM conversations WHERE phone=? ORDER BY created_at DESC LIMIT 10",
        (phone,)
    ).fetchall()
    conn.close()
    # Retourner dans l'ordre chronologique
    return [{"role": r["role"], "parts": [{"text": r["message"]}]} for r in reversed(rows)]

def save_message(phone, role, message):
    conn = get_db()
    conn.execute(
        "INSERT INTO conversations (phone, role, message) VALUES (?, ?, ?)",
        (phone, role, message)
    )
    conn.commit()
    conn.close()


# ===== IA =====

def get_ai_reply(phone, user_message):
    clinic = get_clinic_info()
    prices = clinic.get("prices") or "non renseignés"

    system_prompt = f"""Tu es la secrétaire virtuelle de {clinic.get('name', 'la clinique')}.
Tu gères UNIQUEMENT : les rendez-vous, les horaires, les tarifs et les directions.
Tu ne donnes JAMAIS de conseils médicaux. Si quelqu'un pose une question médicale, dis :
"Pour toute question médicale, nos médecins seront heureux de vous aider lors de votre consultation."
Téléphone : {clinic.get('phone', 'non renseigné')}
Adresse : {clinic.get('address', 'non renseignée')}
Horaires : {clinic.get('hours', 'non renseignés')}
Tarifs : {prices}
Pour prendre un rendez-vous, demande : nom complet, type de consultation, date et heure souhaitées.
Réponds toujours en français, de façon chaleureuse et professionnelle. Sois concis (max 3 phrases)."""

    history = get_conversation_history(phone)
    save_message(phone, "user", user_message)

    try:
        chat = model.start_chat(history=history)
        response = chat.send_message(
            system_prompt + "\n\n" + user_message,
            generation_config={"max_output_tokens": 300}
        )
        reply = response.text
    except Exception as e:
        print(f"Erreur Gemini: {e}")
        reply = f"Désolée, service temporairement indisponible. Appelez-nous au {clinic.get('phone', '')}."

    save_message(phone, "model", reply)
    return reply


# ===== GREEN API =====

def send_whatsapp(phone, message):
    chat_id = phone.replace("+", "").replace(" ", "") + "@c.us"
    url = f"{GREEN_API_BASE}/sendMessage/{GREEN_API_TOKEN}"
    try:
        r = requests.post(url, json={"chatId": chat_id, "message": message}, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Erreur Green API: {e}")
        return None


# ===== WEBHOOK WHATSAPP =====

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "no data"}), 200
    if data.get("typeWebhook") != "incomingMessageReceived":
        return jsonify({"status": "ignored"}), 200

    msg_data = data.get("messageData", {})
    if msg_data.get("typeMessage") != "textMessage":
        return jsonify({"status": "not text"}), 200

    phone        = data.get("senderData", {}).get("sender", "").replace("@c.us", "")
    user_message = msg_data.get("textMessageData", {}).get("textMessage", "").strip()

    if not phone or not user_message:
        return jsonify({"status": "missing fields"}), 200

    print(f"📩 {phone}: {user_message}")
    reply = get_ai_reply(phone, user_message)
    send_whatsapp(phone, reply)
    print(f"✅ Réponse: {reply[:60]}...")
    return jsonify({"status": "ok"}), 200


# ===== API CLINIQUE =====

@app.route("/api/clinic", methods=["GET"])
def get_clinic():
    return jsonify(get_clinic_info())

@app.route("/api/clinic", methods=["PUT"])
def update_clinic():
    data = request.get_json()
    conn = get_db()
    conn.execute("""
        UPDATE clinic SET name=?, phone=?, address=?, hours=?, prices=? WHERE id=1
    """, (data.get("name"), data.get("phone"), data.get("address"), data.get("hours"), data.get("prices")))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ===== API PATIENTS =====

@app.route("/api/patients", methods=["GET"])
def get_patients():
    conn = get_db()
    rows = conn.execute("SELECT * FROM patients ORDER BY created_at DESC").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/patients", methods=["POST"])
def create_patient():
    data = request.get_json()
    conn = get_db()
    try:
        conn.execute("INSERT INTO patients (name, phone) VALUES (?, ?)", (data["name"], data["phone"]))
        conn.commit()
    except sqlite3.IntegrityError:
        return jsonify({"error": "Ce numéro existe déjà"}), 400
    finally:
        conn.close()
    return jsonify({"status": "ok"}), 201


# ===== API RENDEZ-VOUS =====

@app.route("/api/appointments", methods=["GET"])
def get_appointments():
    conn = get_db()
    rows = conn.execute("SELECT * FROM appointments ORDER BY date, time").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/appointments", methods=["POST"])
def create_appointment():
    data = request.get_json()
    conn = get_db()
    conn.execute("""
        INSERT INTO appointments (name, phone, type, date, time, status)
        VALUES (?, ?, ?, ?, ?, 'en attente')
    """, (data.get("name"), data.get("phone"), data.get("type"), data.get("date"), data.get("time")))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"}), 201

@app.route("/api/appointments/<int:apt_id>", methods=["PATCH"])
def update_appointment(apt_id):
    data = request.get_json()
    conn = get_db()
    conn.execute("UPDATE appointments SET status=? WHERE id=?", (data.get("status"), apt_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/appointments/<int:apt_id>", methods=["DELETE"])
def delete_appointment(apt_id):
    conn = get_db()
    conn.execute("DELETE FROM appointments WHERE id=?", (apt_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ===== API STATS =====

@app.route("/api/stats", methods=["GET"])
def get_stats():
    conn = get_db()
    total       = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    confirmed   = conn.execute("SELECT COUNT(*) FROM appointments WHERE status='confirmé'").fetchone()[0]
    pending     = conn.execute("SELECT COUNT(*) FROM appointments WHERE status='en attente'").fetchone()[0]
    patients    = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    convs       = conn.execute("SELECT COUNT(DISTINCT phone) FROM conversations").fetchone()[0]
    conn.close()
    return jsonify({
        "total_appointments": total,
        "confirmed": confirmed,
        "pending": pending,
        "total_patients": patients,
        "conversations": convs
    })


# ===== MAIN =====

@app.route("/")
def home():
    return jsonify({"status": "SmartClinique API ✅", "version": "3.0"})

if __name__ == "__main__":
    init_db()
    print("🚀 SmartClinique démarré sur http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=True)
    
