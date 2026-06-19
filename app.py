from flask import Flask, request, jsonify
from flask_cors import CORS
import google.generativeai as genai
import requests
import sqlite3
import threading
import time
import base64
import tempfile
import os
from datetime import datetime, timedelta

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
        CREATE TABLE IF NOT EXISTS doctors (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT    NOT NULL,
            specialty  TEXT    NOT NULL,
            phone      TEXT,
            available  INTEGER DEFAULT 1,
            created_at TEXT    DEFAULT (datetime('now'))
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT,
            phone       TEXT,
            type        TEXT,
            date        TEXT,
            time        TEXT,
            status      TEXT    DEFAULT 'en attente',
            doctor_id   INTEGER REFERENCES doctors(id),
            doctor_name TEXT,
            reminder_24 INTEGER DEFAULT 0,
            reminder_1  INTEGER DEFAULT 0,
            created_at  TEXT    DEFAULT (datetime('now'))
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

    c.execute("INSERT OR IGNORE INTO clinic (id, name) VALUES (1, 'Ma Clinique')")
    conn.commit()
    conn.close()
    print("✅ Base de données initialisée")


# ===== HELPERS =====

def get_clinic_info():
    conn = get_db()
    row = conn.execute("SELECT * FROM clinic WHERE id=1").fetchone()
    conn.close()
    return dict(row) if row else {}

def get_available_doctor(specialty=None):
    """Trouve un médecin disponible selon la spécialité."""
    conn = get_db()
    if specialty:
        row = conn.execute(
            "SELECT * FROM doctors WHERE available=1 AND specialty LIKE ? LIMIT 1",
            (f"%{specialty}%",)
        ).fetchone()
    if not specialty or not row:
        row = conn.execute(
            "SELECT * FROM doctors WHERE available=1 LIMIT 1"
        ).fetchone()
    conn.close()
    return dict(row) if row else None

def get_conversation_history(phone):
    conn = get_db()
    rows = conn.execute(
        "SELECT role, message FROM conversations WHERE phone=? ORDER BY created_at DESC LIMIT 10",
        (phone,)
    ).fetchall()
    conn.close()
    return [{"role": r["role"], "parts": [{"text": r["message"]}]} for r in reversed(rows)]

def save_message(phone, role, message):
    conn = get_db()
    conn.execute(
        "INSERT INTO conversations (phone, role, message) VALUES (?, ?, ?)",
        (phone, role, message)
    )
    conn.commit()
    conn.close()


# ===== TRANSCRIPTION VOCALE =====

def download_voice(url):
    """Télécharge le fichier audio depuis Green API."""
    try:
        r = requests.get(url, timeout=15)
        suffix = ".ogg"
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        tmp.write(r.content)
        tmp.close()
        return tmp.name
    except Exception as e:
        print(f"Erreur téléchargement vocal: {e}")
        return None

def transcribe_voice(file_path):
    """Transcrit un message vocal avec Gemini."""
    try:
        with open(file_path, "rb") as f:
            audio_data = f.read()
        audio_b64 = base64.b64encode(audio_data).decode("utf-8")
        response = model.generate_content([
            {"mime_type": "audio/ogg", "data": audio_b64},
            "Transcris exactement ce message vocal en français. Retourne uniquement le texte transcrit, sans commentaire."
        ])
        return response.text.strip()
    except Exception as e:
        print(f"Erreur transcription: {e}")
        return None
    finally:
        try:
            os.unlink(file_path)
        except:
            pass


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

def notify_doctor(doctor_phone, doctor_name, patient_name, apt_type, apt_date, apt_time, apt_id):
    """Envoie un message au médecin pour confirmation du RDV."""
    if not doctor_phone:
        return
    message = f"""👨‍⚕️ *SmartClinique — Nouveau RDV*

Bonjour Dr. {doctor_name},

Un rendez-vous vous a été attribué :
• Patient : {patient_name}
• Type : {apt_type}
• Date : {apt_date}
• Heure proposée : {apt_time}

Répondez :
*1* — Confirmer
*2* — Proposer une autre heure
*3* — Refuser"""
    send_whatsapp(doctor_phone, message)


# ===== RAPPELS AUTOMATIQUES =====

def send_reminders():
    """Vérifie et envoie les rappels 24h et 1h avant chaque RDV."""
    while True:
        try:
            now = datetime.now()
            conn = get_db()
            apts = conn.execute(
                "SELECT * FROM appointments WHERE status='confirmé'"
            ).fetchall()

            for a in apts:
                try:
                    apt_dt = datetime.strptime(f"{a['date']} {a['time']}", "%Y-%m-%d %H:%M")
                except:
                    continue

                diff = apt_dt - now

                # Rappel 24h avant
                if 23 * 3600 <= diff.total_seconds() <= 25 * 3600 and not a["reminder_24"]:
                    msg = f"""⏰ *Rappel SmartClinique*

Bonjour {a['name']},

Vous avez un rendez-vous demain :
📅 {a['date']} à {a['time']}
🏥 {a['type']}
👨‍⚕️ Dr. {a['doctor_name'] or 'à confirmer'}

Répondez :
*1* — Confirmer ma présence
*2* — Reporter
*3* — Annuler"""
                    send_whatsapp(a["phone"], msg)
                    conn.execute("UPDATE appointments SET reminder_24=1 WHERE id=?", (a["id"],))
                    conn.commit()
                    print(f"📨 Rappel 24h envoyé à {a['name']}")

                # Rappel 1h avant
                if 55 * 60 <= diff.total_seconds() <= 65 * 60 and not a["reminder_1"]:
                    msg = f"""⏰ *Rappel SmartClinique*

Bonjour {a['name']},

Votre rendez-vous est dans 1 heure :
📅 {a['date']} à {a['time']}
🏥 {a['type']}
👨‍⚕️ Dr. {a['doctor_name'] or 'à confirmer'}

Répondez :
*1* — Je serai présent(e)
*2* — Reporter
*3* — Annuler"""
                    send_whatsapp(a["phone"], msg)
                    conn.execute("UPDATE appointments SET reminder_1=1 WHERE id=?", (a["id"],))
                    conn.commit()
                    print(f"📨 Rappel 1h envoyé à {a['name']}")

            conn.close()
        except Exception as e:
            print(f"Erreur rappels: {e}")

        time.sleep(300)  # Vérifie toutes les 5 minutes


# ===== IA =====

def get_ai_reply(phone, user_message):
    clinic = get_clinic_info()

    # Vérifier si c'est une réponse à un rappel (1/2/3)
    if user_message.strip() in ["1", "2", "3"]:
        conn = get_db()
        # Cherche le dernier RDV de ce patient
        apt = conn.execute(
            "SELECT * FROM appointments WHERE phone=? ORDER BY created_at DESC LIMIT 1",
            (phone,)
        ).fetchone()
        conn.close()

        if apt:
            if user_message == "1":
                conn = get_db()
                conn.execute("UPDATE appointments SET status='confirmé' WHERE id=?", (apt["id"],))
                conn.commit()
                conn.close()
                return "✅ Parfait ! Votre présence est confirmée. Nous vous attendons !"
            elif user_message == "2":
                return "📅 D'accord, souhaitez-vous reporter votre rendez-vous ? Donnez-nous votre nouvelle disponibilité."
            elif user_message == "3":
                conn = get_db()
                conn.execute("UPDATE appointments SET status='annulé' WHERE id=?", (apt["id"],))
                conn.commit()
                conn.close()
                return "❌ Votre rendez-vous a été annulé. N'hésitez pas à nous recontacter pour en fixer un nouveau."

    system_prompt = f"""Tu es la secrétaire virtuelle de {clinic.get('name', 'la clinique')}.
Tu gères UNIQUEMENT : les rendez-vous, les horaires, les tarifs et les directions.
Tu ne donnes JAMAIS de conseils médicaux.
Téléphone : {clinic.get('phone', 'non renseigné')}
Adresse : {clinic.get('address', 'non renseignée')}
Horaires : {clinic.get('hours', 'non renseignés')}
Tarifs : {clinic.get('prices', 'non renseignés')}
Pour prendre un rendez-vous, demande : nom complet, type de consultation, date et heure souhaitées.
IMPORTANT : Détecte automatiquement la langue du patient et réponds TOUJOURS dans sa langue (français, wolof, dioula, anglais, arabe...). Max 3 phrases."""

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


# ===== WEBHOOK WHATSAPP =====

@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"status": "no data"}), 200
    # Gestion des appels manqués
    if data.get("typeWebhook") == "outgoingCall":
        call_data = data.get("callData", {})
        if call_data.get("callStatus") == "missed":
            phone = data.get("senderData", {}).get("sender", "").replace("@c.us", "")
            clinic = get_clinic_info()
            msg = f"👋 Bonjour, nous n'avons pas pu répondre à votre appel.\nJe suis la secrétaire virtuelle de {clinic.get('name', 'la clinique')}.\nComment puis-je vous aider ? (rendez-vous, horaires, tarifs...)"
            send_whatsapp(phone, msg)
            print(f"📞 Appel manqué de {phone} — message envoyé")
        return jsonify({"status": "ok"}), 200

    if data.get("typeWebhook") != "incomingMessageReceived":
        return jsonify({"status": "ignored"}), 200

    msg_data = data.get("messageData", {})
    msg_type = msg_data.get("typeMessage")
    phone    = data.get("senderData", {}).get("sender", "").replace("@c.us", "")

    # Gestion des messages vocaux
    if msg_type == "audioMessage":
        audio_url = msg_data.get("fileData", {}).get("downloadUrl") or \
                    msg_data.get("audioMessageData", {}).get("downloadUrl")
        if not audio_url:
            return jsonify({"status": "no audio url"}), 200

        send_whatsapp(phone, "🎤 J'écoute votre message vocal, un instant...")
        file_path = download_voice(audio_url)
        if not file_path:
            send_whatsapp(phone, "⚠️ Je n'ai pas pu lire votre vocal. Pouvez-vous écrire votre message ?")
            return jsonify({"status": "download failed"}), 200

        transcription = transcribe_voice(file_path)
        if not transcription:
            send_whatsapp(phone, "⚠️ Je n'ai pas pu transcrire votre vocal. Pouvez-vous écrire votre message ?")
            return jsonify({"status": "transcription failed"}), 200

        print(f"🎤 Vocal transcrit de {phone}: {transcription}")
        user_message = transcription

    elif msg_type == "textMessage":
        user_message = msg_data.get("textMessageData", {}).get("textMessage", "").strip()
    else:
        return jsonify({"status": "not supported"}), 200

    if not phone or not user_message:
        return jsonify({"status": "missing fields"}), 200

    # Vérifier si c'est un médecin qui confirme un RDV
    if user_message in ["1", "2", "3"]:
        conn = get_db()
        doctor = conn.execute("SELECT * FROM doctors WHERE phone=?", (phone,)).fetchone()
        if doctor:
            apt = conn.execute(
                "SELECT * FROM appointments WHERE doctor_id=? AND status='en attente' ORDER BY created_at DESC LIMIT 1",
                (doctor["id"],)
            ).fetchone()
            if apt:
                if user_message == "1":
                    conn.execute("UPDATE appointments SET status='confirmé' WHERE id=?", (apt["id"],))
                    conn.commit()
                    send_whatsapp(apt["phone"], f"✅ Bonjour {apt['name']}, votre rendez-vous du {apt['date']} à {apt['time']} avec Dr. {doctor['name']} est confirmé !")
                    send_whatsapp(phone, f"✅ RDV confirmé avec {apt['name']} le {apt['date']} à {apt['time']}.")
                elif user_message == "2":
                    send_whatsapp(phone, f"📅 Quelle heure proposez-vous pour {apt['name']} le {apt['date']} ?")
                elif user_message == "3":
                    conn.execute("UPDATE appointments SET status='refusé', doctor_id=NULL, doctor_name=NULL WHERE id=?", (apt["id"],))
                    conn.commit()
                    send_whatsapp(apt["phone"], f"❌ Bonjour {apt['name']}, nous devons reprogrammer votre rendez-vous. Nous vous recontactons rapidement.")
                conn.close()
                return jsonify({"status": "ok"}), 200
        conn.close()

    print(f"📩 {phone}: {user_message}")
    reply = get_ai_reply(phone, user_message)
    send_whatsapp(phone, reply)
    return jsonify({"status": "ok"}), 200


# ===== API CLINIQUE =====

@app.route("/api/clinic", methods=["GET"])
def get_clinic():
    return jsonify(get_clinic_info())

@app.route("/api/clinic", methods=["PUT"])
def update_clinic():
    data = request.get_json()
    conn = get_db()
    conn.execute("UPDATE clinic SET name=?, phone=?, address=?, hours=?, prices=? WHERE id=1",
        (data.get("name"), data.get("phone"), data.get("address"), data.get("hours"), data.get("prices")))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


# ===== API MÉDECINS =====

@app.route("/api/doctors", methods=["GET"])
def get_doctors():
    conn = get_db()
    rows = conn.execute("SELECT * FROM doctors ORDER BY name").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/doctors", methods=["POST"])
def create_doctor():
    data = request.get_json()
    conn = get_db()
    conn.execute("INSERT INTO doctors (name, specialty, phone) VALUES (?, ?, ?)",
        (data["name"], data["specialty"], data.get("phone", "")))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"}), 201

@app.route("/api/doctors/<int:doc_id>", methods=["PATCH"])
def update_doctor(doc_id):
    data = request.get_json()
    conn = get_db()
    conn.execute("UPDATE doctors SET available=? WHERE id=?", (data.get("available"), doc_id))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})

@app.route("/api/doctors/<int:doc_id>", methods=["DELETE"])
def delete_doctor(doc_id):
    conn = get_db()
    conn.execute("DELETE FROM doctors WHERE id=?", (doc_id,))
    conn.commit()
    conn.close()
    return jsonify({"status": "ok"})


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

    # Attribution automatique du médecin
    doctor = get_available_doctor(data.get("type"))
    doctor_id   = doctor["id"]   if doctor else None
    doctor_name = doctor["name"] if doctor else None

    conn = get_db()
    conn.execute("""
        INSERT INTO appointments (name, phone, type, date, time, status, doctor_id, doctor_name)
        VALUES (?, ?, ?, ?, ?, 'en attente', ?, ?)
    """, (data.get("name"), data.get("phone"), data.get("type"),
          data.get("date"), data.get("time"), doctor_id, doctor_name))
    conn.commit()
    conn.close()

    # Notifier le médecin
    if doctor and doctor.get("phone"):
        notify_doctor(
            doctor["phone"], doctor["name"],
            data.get("name"), data.get("type"),
            data.get("date"), data.get("time"),
            doctor_id
        )

    return jsonify({
        "status": "ok",
        "doctor_assigned": doctor_name or "Aucun médecin disponible"
    }), 201

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
    total     = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    confirmed = conn.execute("SELECT COUNT(*) FROM appointments WHERE status='confirmé'").fetchone()[0]
    pending   = conn.execute("SELECT COUNT(*) FROM appointments WHERE status='en attente'").fetchone()[0]
    patients  = conn.execute("SELECT COUNT(*) FROM patients").fetchone()[0]
    convs     = conn.execute("SELECT COUNT(DISTINCT phone) FROM conversations").fetchone()[0]
    doctors   = conn.execute("SELECT COUNT(*) FROM doctors WHERE available=1").fetchone()[0]
    conn.close()
    return jsonify({
        "total_appointments": total,
        "confirmed": confirmed,
        "pending": pending,
        "total_patients": patients,
        "conversations": convs,
        "available_doctors": doctors
    })


# ===== MAIN =====

@app.route("/")
def home():
    return jsonify({"status": "SmartClinique API ✅", "version": "4.0"})

if __name__ == "__main__":
    init_db()
    # Lancer le thread des rappels automatiques
    reminder_thread = threading.Thread(target=send_reminders, daemon=True)
    reminder_thread.start()
    print("⏰ Rappels automatiques activés")
    print("🚀 SmartClinique démarré sur http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=True)
