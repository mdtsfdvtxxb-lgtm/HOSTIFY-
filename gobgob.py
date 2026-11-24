# -*- coding: utf-8 -*-
import atexit
from datetime import datetime, timedelta
import hashlib
import hmac
import io
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
from threading import Thread
import time
import zipfile
from flask import Flask, render_template_string, request
import psutil
import requests
import telebot
from telebot import types

USDT_BDT_RATE = 120.0
MONETAG_ZONE_ID = "12284632"
AD_CLAIM_COOLDOWN_HOURS = 24
MAX_ACCOUNTS_PER_DEVICE = 1
STREAK_REWARD_DAYS = 7
STREAK_BONUS_VIP_DAYS = 1

TOKEN = "9240200295:AAFLy16Jecm7WtfdJPrzwqcsHG1JiqqlKD8"
OWNER_ID = 8452462370
ADMIN_ID = 8452462370
YOUR_USERNAME = "@freelancertradernahid"
UPDATE_CHANNEL = "hostifyofficialsbot"
CHANNEL_INVITE_LINK = "https://t.me/hostifyofficialsbot"

WELCOME_IMAGE_URL = "https://kommodo.ai/i/yzQ8AH5jtwxrGkFQgwRc"

BINANCE_API_KEY = "bMp22v6OBD5P3FjfoGxSzDSL3ZPygQn4AxlsiQ7VszNgteNapqbuA83LT4Vu5c1F"
BINANCE_SECRET_KEY = "A12hNDLKLS5OYtsfK0L6SRGoqlavkowb38cg6ItbdzqD8XjSgLQLkMiwl1YvAqd8"
BINANCE_PAY_ID = "1246752417"

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_BOTS_DIR = os.path.join(BASE_DIR, "upload_bots")
IROTECH_DIR = os.path.join(BASE_DIR, "inf")
DATABASE_PATH = os.path.join(IROTECH_DIR, "bot_data.db")
WELCOME_IMAGE_PATH = os.path.join(BASE_DIR, "welcome_banner.jpg")
DEVICE_PEPPER = hashlib.sha256(f"{TOKEN}:{OWNER_ID}:HOSTIFY_DEVICE_GUARD_V2".encode("utf-8")).hexdigest()

FREE_USER_LIMIT = 0
SUBSCRIBED_USER_LIMIT = 15
ADMIN_LIMIT = 999
OWNER_LIMIT = float("inf")

os.makedirs(UPLOAD_BOTS_DIR, exist_ok=True)
os.makedirs(IROTECH_DIR, exist_ok=True)

bot = telebot.TeleBot(TOKEN)

bot_scripts = {}
user_subscriptions = {}
user_files = {}
active_users = set()
banned_users = set()
admin_ids = {ADMIN_ID, OWNER_ID}
maintenance_mode = False
user_last_msg = {}
pending_bot_tokens = {}
pending_ticket_users = set()
pending_admin_replies = {}
pending_proxy_users = set()
pending_ai_audit_users = set()
pending_ai_persona_users = set()
notified_expiries = set()

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

DB_LOCK = threading.Lock()


def _extract_client_ip(req):
    if req is None:
        return ""
    xff = req.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    xri = req.headers.get("X-Real-IP", "")
    if xri:
        return xri.strip()
    return (req.remote_addr or "").strip()


def build_device_fingerprint(user_id=0, req=None, client_dev_id="", extra_sig=""):
    clean_client_id = re.sub(r"[^A-Za-z0-9_.-]", "", str(client_dev_id or "").strip())[:80]
    clean_extra = re.sub(r"[^A-Za-z0-9_.:;,-]", "", str(extra_sig or "").strip())[:160]
    ua = ""
    lang = ""
    ip = ""
    sec_ch_ua = ""
    sec_ch_platform = ""
    if req is not None:
        ua = req.headers.get("User-Agent", "").strip()
        lang = req.headers.get("Accept-Language", "").strip()
        ip = _extract_client_ip(req)
        sec_ch_ua = req.headers.get("Sec-CH-UA", "").strip()
        sec_ch_platform = req.headers.get("Sec-CH-UA-Platform", "").strip()

    if clean_client_id:
        payload = f"cid:{clean_client_id}|ua:{ua}|plat:{sec_ch_platform}|ch:{sec_ch_ua}|ext:{clean_extra}"
        digest = hmac.new(DEVICE_PEPPER.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"dev_{digest[:28]}"

    if ua or ip or clean_extra:
        payload = f"soft:{ua}|{lang}|{ip}|{sec_ch_platform}|{clean_extra}"
        digest = hmac.new(DEVICE_PEPPER.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"sft_{digest[:28]}"

    return f"tg_{user_id}"


def log_device_abuse(user_id, existing_owner_id, device_hash, action_type, detail=""):
    try:
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute(
                """INSERT INTO device_abuse_logs
                   (user_id, existing_owner_id, device_hash, action_type, detail, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (int(user_id), int(existing_owner_id or 0), str(device_hash), str(action_type), str(detail), datetime.now().isoformat()),
            )
            conn.commit()
            conn.close()
    except Exception as e:
        logger.error(f"Device abuse log error: {e}")


def bind_or_check_device(user_id, device_hash, source="web", enforce_strict=True):
    if user_id in admin_ids or user_id == OWNER_ID:
        return True, None, ""
    if not device_hash or str(device_hash).startswith("tg_"):
        if enforce_strict:
            return False, None, "❌ ডিভাইস ভেরিফিকেশন সম্পন্ন হয়নি। অনুগ্রহ করে অ্যাড পেজ সঠিকভাবে সম্পন্ন করুন।"
        return True, None, ""

    now_str = datetime.now().isoformat()
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()

        c.execute("SELECT owner_user_id, is_blocked FROM device_locks WHERE device_hash = ?", (device_hash,))
        lock_row = c.fetchone()

        if lock_row:
            owner_uid, is_blocked = int(lock_row[0]), int(lock_row[1] or 0)
            if is_blocked == 1 and owner_uid != int(user_id):
                conn.close()
                log_device_abuse(user_id, owner_uid, device_hash, source, "Blocked device lock")
                return False, owner_uid, "❌ এই ডিভাইসটি সিকিউরিটি লকের আওতায় ব্লক করা হয়েছে!"

            if owner_uid != int(user_id):
                c.execute("SELECT COUNT(DISTINCT user_id) FROM device_accounts WHERE device_hash = ?", (device_hash,))
                acc_count = int((c.fetchone() or [1])[0] or 1)
                c.execute(
                    "INSERT OR REPLACE INTO device_accounts (device_hash, user_id, source, last_seen) VALUES (?, ?, ?, ?)",
                    (device_hash, int(user_id), source, now_str),
                )
                conn.commit()
                conn.close()
                log_device_abuse(user_id, owner_uid, device_hash, source, f"Multi-account attempt (count={acc_count + 1})")
                return (
                    False,
                    owner_uid,
                    f"❌ **SAME-DEVICE MULTI-ACCOUNT BLOCKED!**\n"
                    f"এই ডিভাইসটি ইতিমধ্যে অন্য একটি Telegram অ্যাকাউন্ট (`{owner_uid}`) দিয়ে লক করা আছে।\n"
                    f"একই ডিভাইস থেকে একাধিক Telegram অ্যাকাউন্ট দিয়ে VIP বা Voucher নেওয়া সম্পূর্ণ নিষিদ্ধ।",
                )

            c.execute("UPDATE device_locks SET last_seen = ? WHERE device_hash = ?", (now_str, device_hash))
            c.execute(
                "INSERT OR REPLACE INTO device_accounts (device_hash, user_id, source, last_seen) VALUES (?, ?, ?, ?)",
                (device_hash, int(user_id), source, now_str),
            )
            c.execute(
                "INSERT OR REPLACE INTO user_devices (user_id, device_hash, updated_at) VALUES (?, ?, ?)",
                (int(user_id), device_hash, now_str),
            )
            conn.commit()
            conn.close()
            return True, None, ""

        c.execute("SELECT user_id FROM user_devices WHERE device_hash = ? AND user_id != ? LIMIT 1", (device_hash, int(user_id)))
        other_dev = c.fetchone()
        if other_dev:
            other_uid = int(other_dev[0])
            c.execute(
                "INSERT OR IGNORE INTO device_locks (device_hash, owner_user_id, is_blocked, created_at, last_seen) VALUES (?, ?, 0, ?, ?)",
                (device_hash, other_uid, now_str, now_str),
            )
            c.execute(
                "INSERT OR REPLACE INTO device_accounts (device_hash, user_id, source, last_seen) VALUES (?, ?, ?, ?)",
                (device_hash, int(user_id), source, now_str),
            )
            conn.commit()
            conn.close()
            log_device_abuse(user_id, other_uid, device_hash, source, "Matched prior user_devices fingerprint")
            return (
                False,
                other_uid,
                f"❌ **SAME-DEVICE MULTI-ACCOUNT BLOCKED!**\n"
                f"এই ডিভাইসটি পূর্বে অন্য একটি Telegram অ্যাকাউন্ট (`{other_uid}`) এর সাথে যুক্ত ছিল।",
            )

        c.execute(
            "INSERT INTO device_locks (device_hash, owner_user_id, is_blocked, created_at, last_seen) VALUES (?, ?, 0, ?, ?)",
            (device_hash, int(user_id), now_str, now_str),
        )
        c.execute(
            "INSERT OR REPLACE INTO device_accounts (device_hash, user_id, source, last_seen) VALUES (?, ?, ?, ?)",
            (device_hash, int(user_id), source, now_str),
        )
        c.execute(
            "INSERT OR REPLACE INTO user_devices (user_id, device_hash, updated_at) VALUES (?, ?, ?)",
            (int(user_id), device_hash, now_str),
        )
        conn.commit()
        conn.close()
        return True, None, ""


def init_db():
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
            user_id INTEGER PRIMARY KEY,
            plan_name TEXT,
            expiry TEXT,
            file_limit INTEGER DEFAULT 15
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS user_files (
            user_id INTEGER,
            file_name TEXT,
            file_type TEXT,
            bot_token TEXT,
            PRIMARY KEY (user_id, file_name)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS active_users (user_id INTEGER PRIMARY KEY)""")
        c.execute("""CREATE TABLE IF NOT EXISTS banned_users (user_id INTEGER PRIMARY KEY)""")
        c.execute("""CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)""")
        c.execute("""CREATE TABLE IF NOT EXISTS plans (
            plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            file_limit INTEGER,
            price TEXT,
            duration INTEGER,
            buy_link TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS referrals (
            user_id INTEGER PRIMARY KEY,
            referred_by INTEGER,
            ref_count INTEGER DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS used_txids (tx_id TEXT PRIMARY KEY)""")
        c.execute("""CREATE TABLE IF NOT EXISTS vouchers (
            code TEXT PRIMARY KEY,
            days INTEGER,
            file_limit INTEGER,
            max_uses INTEGER DEFAULT 1,
            used_count INTEGER DEFAULT 0,
            is_used INTEGER DEFAULT 0,
            expires_at TEXT DEFAULT ''
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS voucher_redemptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT,
            user_id INTEGER,
            device_hash TEXT,
            redeemed_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS user_devices (
            user_id INTEGER PRIMARY KEY,
            device_hash TEXT,
            updated_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS device_locks (
            device_hash TEXT PRIMARY KEY,
            owner_user_id INTEGER,
            is_blocked INTEGER DEFAULT 0,
            created_at TEXT,
            last_seen TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS device_accounts (
            device_hash TEXT,
            user_id INTEGER,
            source TEXT,
            last_seen TEXT,
            PRIMARY KEY (device_hash, user_id)
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS device_abuse_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            existing_owner_id INTEGER,
            device_hash TEXT,
            action_type TEXT,
            detail TEXT,
            created_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS ad_sessions (
            token TEXT PRIMARY KEY,
            user_id INTEGER,
            device_hash TEXT,
            verified INTEGER DEFAULT 0,
            created_at TEXT,
            claimed INTEGER DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS support_tickets (
            ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            message_text TEXT,
            status TEXT DEFAULT 'OPEN',
            created_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS daily_streaks (
            user_id INTEGER PRIMARY KEY,
            streak_count INTEGER DEFAULT 0,
            last_claim_at TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS user_proxies (
            proxy_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            proxy_url TEXT,
            latency_ms INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 0,
            created_at TEXT
        )""")

        for alter_sql in [
            "ALTER TABLE subscriptions ADD COLUMN file_limit INTEGER DEFAULT 15",
            "ALTER TABLE user_files ADD COLUMN bot_token TEXT",
            "ALTER TABLE vouchers ADD COLUMN max_uses INTEGER DEFAULT 1",
            "ALTER TABLE vouchers ADD COLUMN used_count INTEGER DEFAULT 0",
            "ALTER TABLE vouchers ADD COLUMN expires_at TEXT DEFAULT ''",
            "ALTER TABLE ad_sessions ADD COLUMN device_hash TEXT",
        ]:
            try:
                c.execute(alter_sql)
            except Exception:
                pass

        c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (OWNER_ID,))
        if ADMIN_ID != OWNER_ID:
            c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (ADMIN_ID,))
        conn.commit()
        conn.close()


def load_data():
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT user_id, plan_name, expiry, file_limit FROM subscriptions")
        for row in c.fetchall():
            try:
                flim = int(row[3]) if row[3] is not None else SUBSCRIBED_USER_LIMIT
                user_subscriptions[row[0]] = {
                    "plan_name": row[1],
                    "expiry": datetime.fromisoformat(row[2]),
                    "file_limit": flim,
                }
            except Exception:
                pass

        c.execute("SELECT user_id, file_name, file_type FROM user_files")
        for uid, fname, ftype in c.fetchall():
            user_files.setdefault(uid, [])
            if (fname, ftype) not in user_files[uid]:
                user_files[uid].append((fname, ftype))

        c.execute("SELECT user_id FROM active_users")
        active_users.update(u[0] for u in c.fetchall())
        c.execute("SELECT user_id FROM banned_users")
        banned_users.update(u[0] for u in c.fetchall())
        c.execute("SELECT user_id FROM admins")
        admin_ids.update(u[0] for u in c.fetchall())
        conn.close()
    except Exception as e:
        logger.error(f"Load Data Error: {e}")


init_db()
load_data()


# --- Flask Server with Premium Stylist Graphic Motion UI & Strict Device Guard ---
app = Flask("")


def get_base_url():
    render_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBAPP_URL")
    if render_url:
        return render_url.rstrip("/") if render_url.startswith("http") else f"https://{render_url.rstrip('/')}"
    port = int(os.environ.get("PORT", 8080))
    return f"http://127.0.0.1:{port}"


def create_ad_session(uid):
    token = secrets.token_urlsafe(16)
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute(
            "INSERT INTO ad_sessions (token, user_id, device_hash, verified, created_at, claimed) VALUES (?, ?, '', 0, ?, 0)",
            (token, uid, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
    return token


@app.route("/")
def flask_home():
    return "HOSTIFY Cloud & Flask Ad Server is Live!"


@app.route("/ad/<token>")
def ad_page(token):
    soft_dev_hash = build_device_fingerprint(0, request)
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT user_id, verified, claimed FROM ad_sessions WHERE token = ?", (token,))
        row = c.fetchone()
        if row:
            c.execute("UPDATE ad_sessions SET device_hash = ? WHERE token = ?", (soft_dev_hash, token))
            conn.commit()
        conn.close()

    if not row:
        return "<h3>Invalid or expired ad session. Please request a new link from the bot.</h3>", 404

    html_template = f"""
    <!DOCTYPE html>
    <html lang="bn">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>HOSTIFY ✨ Premium VIP Stylist Graphic Motion Engine</title>
        <style>
            :root {{
                --bg-dark: #040711;
                --panel-bg: rgba(12, 19, 38, 0.86);
                --border-glow: rgba(56, 189, 248, 0.42);
                --cyan: #38bdf8;
                --indigo: #818cf8;
                --emerald: #22c55e;
                --rose: #f43f5e;
                --amber: #f59e0b;
                --text-main: #f8fafc;
                --text-muted: #94a3b8;
            }}

            * {{ box-sizing: border-box; margin: 0; padding: 0; }}

            body {{
                background: radial-gradient(circle at 15% 20%, #0f172a 0%, var(--bg-dark) 68%);
                color: var(--text-main);
                font-family: 'Segoe UI', system-ui, -apple-system, Arial, sans-serif;
                min-height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                padding: 24px 14px;
                overflow-x: hidden;
                position: relative;
            }}

            .sys-grid {{
                position: fixed;
                inset: 0;
                background-image:
                    linear-gradient(rgba(56, 189, 248, 0.07) 1px, transparent 1px),
                    linear-gradient(90deg, rgba(129, 140, 248, 0.07) 1px, transparent 1px);
                background-size: 36px 36px;
                animation: gridDrift 16s linear infinite;
                z-index: 0;
                pointer-events: none;
            }}

            @keyframes gridDrift {{
                0% {{ transform: translate(0, 0); }}
                100% {{ transform: translate(36px, 36px); }}
            }}

            .orb {{
                position: fixed;
                border-radius: 50%;
                filter: blur(72px);
                opacity: 0.36;
                z-index: 0;
                pointer-events: none;
                animation: orbFloat 8s ease-in-out infinite alternate;
            }}
            .orb-1 {{ width: 270px; height: 270px; background: #0284c7; top: -60px; left: -50px; }}
            .orb-2 {{ width: 310px; height: 310px; background: #6366f1; bottom: -80px; right: -60px; animation-delay: 2s; }}
            .orb-3 {{ width: 190px; height: 190px; background: #10b981; top: 52%; left: 64%; animation-delay: 3.5s; }}

            @keyframes orbFloat {{
                0% {{ transform: translate(0, 0) scale(1); }}
                100% {{ transform: translate(24px, -28px) scale(1.14); }}
            }}

            .card {{
                position: relative;
                z-index: 2;
                background: var(--panel-bg);
                backdrop-filter: blur(20px);
                -webkit-backdrop-filter: blur(20px);
                padding: 28px 22px;
                border-radius: 26px;
                max-width: 470px;
                width: 100%;
                border: 1px solid var(--border-glow);
                box-shadow: 0 24px 65px rgba(0, 0, 0, 0.72), 0 0 34px rgba(56, 189, 248, 0.16);
                text-align: center;
                animation: cardEntrance 0.8s cubic-bezier(0.16, 1, 0.3, 1) forwards;
                overflow: hidden;
            }}

            .card::before {{
                content: "";
                position: absolute;
                top: 0; left: -100%;
                width: 100%; height: 2.5px;
                background: linear-gradient(90deg, transparent, var(--cyan), var(--amber), var(--indigo), transparent);
                animation: scanLine 3s linear infinite;
            }}

            @keyframes cardEntrance {{
                0% {{ opacity: 0; transform: translateY(30px) scale(0.95); }}
                100% {{ opacity: 1; transform: translateY(0) scale(1); }}
            }}

            @keyframes scanLine {{
                0% {{ left: -100%; }}
                100% {{ left: 100%; }}
            }}

            .motion-stage {{
                position: relative;
                width: 118px;
                height: 118px;
                margin: 0 auto 14px;
                display: flex;
                align-items: center;
                justify-content: center;
            }}

            .pulse-ring {{
                position: absolute;
                inset: 6px;
                border-radius: 50%;
                border: 2px solid rgba(56, 189, 248, 0.5);
                animation: radarPulse 2.3s cubic-bezier(0.24, 0, 0.38, 1) infinite;
            }}
            .pulse-ring:nth-child(2) {{
                animation-delay: 1.15s;
                border-color: rgba(129, 140, 248, 0.45);
            }}

            @keyframes radarPulse {{
                0% {{ transform: scale(0.8); opacity: 0.92; }}
                100% {{ transform: scale(1.42); opacity: 0; }}
            }}

            svg.motion-svg {{
                width: 102px;
                height: 102px;
                position: relative;
                z-index: 2;
            }}

            .spin-slow {{
                transform-origin: 48px 48px;
                animation: spin360 11s linear infinite;
            }}
            .spin-reverse {{
                transform-origin: 48px 48px;
                animation: spinReverse 6.5s linear infinite;
            }}
            .core-beat {{
                transform-origin: 48px 48px;
                animation: coreBeat 1.7s ease-in-out infinite;
            }}

            @keyframes spin360 {{ to {{ transform: rotate(360deg); }} }}
            @keyframes spinReverse {{ to {{ transform: rotate(-360deg); }} }}
            @keyframes coreBeat {{
                0%, 100% {{ transform: scale(0.94); }}
                50% {{ transform: scale(1.09); }}
            }}

            .telemetry-bar {{
                display: flex;
                justify-content: center;
                gap: 8px;
                flex-wrap: wrap;
                margin: 10px 0 14px;
            }}

            .pill {{
                font-size: 11px;
                padding: 4px 10px;
                border-radius: 999px;
                background: rgba(30, 41, 59, 0.88);
                border: 1px solid rgba(148, 163, 184, 0.25);
                color: #bae6fd;
                display: inline-flex;
                align-items: center;
                gap: 5px;
            }}

            .dot-live {{
                width: 7px; height: 7px;
                border-radius: 50%;
                background: var(--emerald);
                box-shadow: 0 0 8px var(--emerald);
                animation: blinkDot 1.2s infinite;
            }}

            @keyframes blinkDot {{
                0%, 100% {{ opacity: 1; transform: scale(1); }}
                50% {{ opacity: 0.35; transform: scale(0.75); }}
            }}

            h2 {{
                font-size: 20px;
                letter-spacing: 0.4px;
                background: linear-gradient(90deg, #38bdf8, #fbbf24, #a5b4fc, #34d399);
                background-size: 220% auto;
                -webkit-background-clip: text;
                -webkit-text-fill-color: transparent;
                animation: textShimmer 3.8s linear infinite;
                margin-bottom: 8px;
            }}

            @keyframes textShimmer {{
                0% {{ background-position: 0% center; }}
                100% {{ background-position: 220% center; }}
            }}

            p.desc {{
                color: var(--text-muted);
                font-size: 13.5px;
                line-height: 1.65;
                margin-bottom: 14px;
            }}

            .progress-box {{
                width: 100%;
                height: 8px;
                background: rgba(15, 23, 42, 0.92);
                border-radius: 999px;
                overflow: hidden;
                border: 1px solid rgba(56, 189, 248, 0.24);
                margin: 12px 0 18px;
            }}

            .progress-fill {{
                height: 100%;
                width: 0%;
                background: linear-gradient(90deg, #0284c7, #38bdf8, #fbbf24, #22c55e);
                border-radius: 999px;
                transition: width 0.35s ease;
                box-shadow: 0 0 16px rgba(56, 189, 248, 0.65);
            }}

            .btn {{
                position: relative;
                width: 100%;
                background: linear-gradient(135deg, #2563eb, #0ea5e9);
                color: #fff;
                padding: 14px 22px;
                border: none;
                border-radius: 14px;
                font-size: 15px;
                font-weight: 700;
                cursor: pointer;
                overflow: hidden;
                transition: transform 0.2s ease, box-shadow 0.2s ease;
                box-shadow: 0 10px 25px rgba(14, 165, 233, 0.38);
            }}

            .btn:hover:not(:disabled) {{
                transform: translateY(-2px) scale(1.01);
                box-shadow: 0 14px 30px rgba(56, 189, 248, 0.52);
            }}

            .btn:active:not(:disabled) {{
                transform: scale(0.98);
            }}

            .btn::after {{
                content: "";
                position: absolute;
                top: 0; left: -120%;
                width: 60%; height: 100%;
                background: linear-gradient(90deg, transparent, rgba(255,255,255,0.36), transparent);
                transform: skewX(-20deg);
                animation: btnSheen 3.2s infinite;
            }}

            @keyframes btnSheen {{
                0% {{ left: -120%; }}
                45%, 100% {{ left: 150%; }}
            }}

            .btn:disabled {{
                opacity: 0.75;
                cursor: not-allowed;
            }}

            .status {{
                margin-top: 16px;
                font-size: 13.5px;
                font-weight: 600;
                min-height: 22px;
                color: var(--emerald);
                animation: fadeUp 0.4s ease;
            }}

            .status.err {{
                color: var(--rose);
            }}

            @keyframes fadeUp {{
                from {{ opacity: 0; transform: translateY(6px); }}
                to {{ opacity: 1; transform: translateY(0); }}
            }}

            .shield-note {{
                margin-top: 14px;
                font-size: 11.5px;
                color: #64748b;
            }}
        </style>
        <script src='//libtl.com/sdk.js' data-zone='{MONETAG_ZONE_ID}' data-sdk='show_{MONETAG_ZONE_ID}'></script>
    </head>
    <body>
        <div class="sys-grid"></div>
        <div class="orb orb-1"></div>
        <div class="orb orb-2"></div>
        <div class="orb orb-3"></div>

        <div class="card">
            <div class="motion-stage">
                <div class="pulse-ring"></div>
                <div class="pulse-ring"></div>
                <svg class="motion-svg" viewBox="0 0 96 96" fill="none">
                    <circle class="spin-slow" cx="48" cy="48" r="42" stroke="#38bdf8" stroke-width="2" stroke-dasharray="10 8" opacity="0.8"/>
                    <circle class="spin-reverse" cx="48" cy="48" r="32" stroke="#818cf8" stroke-width="2" stroke-dasharray="16 10" opacity="0.88"/>
                    <g class="core-beat">
                        <polygon points="48,22 70,35 70,61 48,74 26,61 26,35" fill="rgba(14, 165, 233, 0.18)" stroke="#fbbf24" stroke-width="2"/>
                        <path d="M40 48L46 54L57 42" stroke="#22c55e" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round"/>
                    </g>
                </svg>
            </div>

            <div class="telemetry-bar">
                <span class="pill"><span class="dot-live"></span> VIP MOTION ENGINE</span>
                <span class="pill" id="devBadge">🔒 DEVICE LOCKED</span>
            </div>

            <h2>✨ HOSTIFY VIP STYLIST MOTION</h2>
            <p class="desc">
                ফ্রি VIP ক্লেইম করতে নিচের <b>Watch Ad Now</b> বাটনে ক্লিক করে অ্যাডটি সম্পন্ন করুন।
                ভেরিফাই হলে বোটে ফিরে গিয়ে <b>Verify Ad &amp; Claim VIP</b> চাপুন।
            </p>

            <div class="progress-box">
                <div class="progress-fill" id="progressBar"></div>
            </div>

            <button id="adBtn" class="btn" onclick="startAd()">👀 Watch Ad Now</button>
            <div id="statusMsg" class="status"></div>
            <div class="shield-note">🛡️ Stylist Motion &amp; Multi-Account Guard Enabled</div>
        </div>

        <script>
            let secondsLeft = 6;
            let verifyingSent = false;

            function setProgress(pct) {{
                const bar = document.getElementById('progressBar');
                if (bar) bar.style.width = Math.max(0, Math.min(100, pct)) + '%';
            }}

            function getPersistentDeviceId() {{
                const key = 'hostify_client_dev_id';
                let id = '';
                try {{ id = localStorage.getItem(key) || ''; }} catch (e) {{}}
                if (!id) {{
                    try {{ id = sessionStorage.getItem(key) || ''; }} catch (e) {{}}
                }}
                if (!id) {{
                    const m = document.cookie.match(/(?:^|; )hostify_client_dev_id=([^;]+)/);
                    if (m) id = decodeURIComponent(m[1]);
                }}
                if (!id) {{
                    const rnd = (window.crypto && crypto.randomUUID)
                        ? crypto.randomUUID().replace(/-/g, '')
                        : (Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2));
                    id = 'dev_' + rnd + '_' + Date.now().toString(36);
                }}
                try {{ localStorage.setItem(key, id); }} catch (e) {{}}
                try {{ sessionStorage.setItem(key, id); }} catch (e) {{}}
                document.cookie = key + '=' + encodeURIComponent(id) + '; max-age=31536000; path=/; SameSite=Lax';
                return id;
            }}

            function getExtraSignals() {{
                const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || '';
                const scr = (window.screen && screen.width && screen.height)
                    ? `${{screen.width}}x${{screen.height}}x${{screen.colorDepth || 24}}`
                    : '';
                const hw = navigator.hardwareConcurrency || 0;
                const mem = navigator.deviceMemory || 0;
                const plat = navigator.platform || '';
                return `${{tz}}|${{scr}}|c${{hw}}|m${{mem}}|p:${{plat}}`;
            }}

            const clientDevId = getPersistentDeviceId();
            const extraSig = getExtraSignals();
            document.getElementById('devBadge').innerText = '🔒 ID: ' + clientDevId.slice(0, 12) + '…';

            function confirmVerified() {{
                if (verifyingSent) return;
                verifyingSent = true;
                setProgress(90);
                const statusEl = document.getElementById('statusMsg');
                statusEl.classList.remove('err');
                statusEl.innerText = '⚙️ সিস্টেম ভেরিফিকেশন চলছে...';

                fetch('/api/verify-ad', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{
                        token: '{token}',
                        client_dev_id: clientDevId,
                        extra_sig: extraSig
                    }})
                }})
                .then(r => r.json())
                .then(data => {{
                    if (data.ok) {{
                        setProgress(100);
                        statusEl.classList.remove('err');
                        statusEl.innerText = '✅ অ্যাড ভেরিফাই হয়েছে! এখন বোটে গিয়ে Claim করুন।';
                    }} else {{
                        verifyingSent = false;
                        setProgress(0);
                        statusEl.classList.add('err');
                        statusEl.innerText = data.message || '⚠️ ভেরিফাই করা যায়নি (Multi-Account ব্লক হতে পারে)।';
                    }}
                }})
                .catch(() => {{
                    verifyingSent = false;
                    setProgress(0);
                    statusEl.classList.add('err');
                    statusEl.innerText = '⚠️ নেটওয়ার্ক সমস্যা হয়েছে, আবার চেষ্টা করুন।';
                }});
            }}

            function startAd() {{
                const btn = document.getElementById('adBtn');
                btn.disabled = true;
                setProgress(20);
                btn.innerText = '🎬 Loading Ad Stream...';

                const runFallbackTimer = () => {{
                    const timer = setInterval(() => {{
                        secondsLeft--;
                        const elapsed = 6 - secondsLeft;
                        setProgress(20 + Math.round((elapsed / 6) * 65));
                        btn.innerText = '⏳ Verifying (' + Math.max(0, secondsLeft) + 's)...';
                        if (secondsLeft <= 0) {{
                            clearInterval(timer);
                            btn.innerText = '✅ Ad Completed';
                            confirmVerified();
                        }}
                    }}, 1000);
                }};

                if (typeof show_{MONETAG_ZONE_ID} === 'function') {{
                    show_{MONETAG_ZONE_ID}({{ ytag: true }}).then(() => {{
                        setProgress(85);
                        btn.innerText = '✅ Ad Completed';
                        confirmVerified();
                    }}).catch(() => {{
                        runFallbackTimer();
                    }});
                }} else {{
                    runFallbackTimer();
                }}
            }}
        </script>
    </body>
    </html>
    """
    return render_template_string(html_template)


@app.route("/api/verify-ad", methods=["POST"])
def api_verify_ad():
    data = request.get_json(silent=True) or {}
    token = data.get("token", "").strip()
    client_dev_id = data.get("client_dev_id", "").strip()
    extra_sig = data.get("extra_sig", "").strip()
    if not token:
        return {"ok": False, "message": "Invalid token"}, 400

    dev_hash = build_device_fingerprint(0, request, client_dev_id, extra_sig)

    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT user_id, claimed FROM ad_sessions WHERE token = ?", (token,))
        sess = c.fetchone()
        if not sess:
            conn.close()
            return {"ok": False, "message": "Session expired"}, 404
        uid, claimed = int(sess[0]), int(sess[1] or 0)
        conn.close()

    if claimed == 1:
        return {"ok": False, "message": "এই সেশন ইতিমধ্যে ক্লেইম করা হয়েছে।"}, 400

    allowed, _, reason_bn = bind_or_check_device(uid, dev_hash, source="ad_verify", enforce_strict=True)
    if not allowed:
        return {"ok": False, "message": reason_bn}, 403

    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("UPDATE ad_sessions SET verified = 1, device_hash = ? WHERE token = ?", (dev_hash, token))
        updated = c.rowcount
        conn.commit()
        conn.close()

    return {"ok": updated > 0}


def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)


def keep_alive():
    t = Thread(target=run_flask, daemon=True)
    t.start()


def is_blocked_for_user(uid):
    if uid in banned_users:
        return True, "❌ আপনি এই বোট থেকে ব্যানড হয়েছেন।"
    if maintenance_mode and uid not in admin_ids:
        return True, "🛠️ বোট বর্তমানে Maintenance Mode-এ আছে। কিছুক্ষণ পর আবার চেষ্টা করুন।"
    return False, None


def is_anti_flood(uid):
    if uid in admin_ids:
        return False
    now = time.time()
    last = user_last_msg.get(uid, 0)
    if now - last < 0.5:
        return True
    user_last_msg[uid] = now
    return False


def play_stylist_animation(message, title="VIP MOTION ENGINE", frames=None):
    frames = frames or [
        f"✨ **[▰▱▱▱▱] {title}** — _Initializing..._",
        f"💎 **[▰▰▰▱▱] {title}** — _Syncing Core..._",
        f"🚀 **[▰▰▰▰▰] {title}** — _Ready!_",
    ]
    try:
        sent = bot.reply_to(message, frames[0], parse_mode="Markdown")
        for fr in frames[1:]:
            time.sleep(0.25)
            try:
                bot.edit_message_text(fr, sent.chat.id, sent.message_id, parse_mode="Markdown")
            except Exception:
                pass
        return sent
    except Exception:
        return None


def parse_price_to_usdt(price_str):
    price_clean = str(price_str).upper().strip()
    numbers = re.findall(r"[-+]?\d*\.\d+|\d+", price_clean)
    if not numbers:
        return 0.0, price_str
    val = float(numbers[0])
    if "BDT" in price_clean or "TK" in price_clean:
        return round(val / USDT_BDT_RATE, 2), f"{price_str} (~{round(val / USDT_BDT_RATE, 2)} USDT)"
    return round(val, 2), f"{val} USDT"


def safe_extract_zip(zip_path, dest_dir):
    dest_real = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            target_path = os.path.realpath(os.path.join(dest_real, member.filename))
            if not target_path.startswith(dest_real + os.sep) and target_path != dest_real:
                raise ValueError("Unsafe zip path detected.")
        zf.extractall(dest_real)


def add_plan_db(name, file_limit, price, duration, buy_link):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute(
            "INSERT INTO plans (name, file_limit, price, duration, buy_link) VALUES (?, ?, ?, ?, ?)",
            (name, int(file_limit), price, int(duration), buy_link),
        )
        conn.commit()
        conn.close()


def get_all_plans():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT plan_id, name, file_limit, price, duration, buy_link FROM plans ORDER BY plan_id ASC")
    plans = c.fetchall()
    conn.close()
    return plans


def get_plan_by_id(plan_id):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT plan_id, name, file_limit, price, duration, buy_link FROM plans WHERE plan_id = ?", (int(plan_id),))
    plan = c.fetchone()
    conn.close()
    return plan


def delete_plan_db(plan_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM plans WHERE plan_id = ?", (int(plan_id),))
        deleted = c.rowcount
        conn.commit()
        conn.close()
    return deleted > 0


def save_subscription(user_id, plan_name, expiry_or_days, file_limit=SUBSCRIBED_USER_LIMIT, stack=True):
    now = datetime.now()
    existing = user_subscriptions.get(user_id)
    if isinstance(expiry_or_days, int):
        base_time = existing["expiry"] if (stack and existing and existing["expiry"] > now) else now
        new_expiry = base_time + timedelta(days=expiry_or_days)
    else:
        new_expiry = expiry_or_days

    final_limit = int(file_limit) if file_limit is not None else SUBSCRIBED_USER_LIMIT
    if stack and existing and existing["expiry"] > now:
        final_limit = max(int(existing.get("file_limit", SUBSCRIBED_USER_LIMIT)), final_limit)

    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO subscriptions (user_id, plan_name, expiry, file_limit) VALUES (?, ?, ?, ?)",
            (user_id, plan_name, new_expiry.isoformat(), final_limit),
        )
        conn.commit()
        conn.close()
    user_subscriptions[user_id] = {
        "plan_name": plan_name,
        "expiry": new_expiry,
        "file_limit": final_limit,
    }
    notified_expiries.discard(user_id)
    return new_expiry, final_limit


def remove_subscription_db(user_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
    user_subscriptions.pop(user_id, None)


def reset_user_device_db(user_id):
    uid = int(user_id)
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT device_hash FROM user_devices WHERE user_id = ?", (uid,))
        r1 = c.fetchone()
        c.execute("SELECT device_hash FROM device_locks WHERE owner_user_id = ?", (uid,))
        r2 = c.fetchall()

        hashes = set()
        if r1 and r1[0]:
            hashes.add(r1[0])
        for item in r2:
            if item[0]:
                hashes.add(item[0])

        c.execute("DELETE FROM user_devices WHERE user_id = ?", (uid,))
        c.execute("DELETE FROM device_locks WHERE owner_user_id = ?", (uid,))
        c.execute("DELETE FROM device_accounts WHERE user_id = ?", (uid,))
        for h in hashes:
            c.execute("DELETE FROM device_locks WHERE device_hash = ?", (h,))
            c.execute("DELETE FROM device_accounts WHERE device_hash = ?", (h,))
            c.execute("UPDATE user_devices SET device_hash = '' WHERE device_hash = ?", (h,))

        affected = c.rowcount
        conn.commit()
        conn.close()
    return bool(hashes) or affected > 0


def add_admin_db(new_admin_id):
    admin_ids.add(int(new_admin_id))
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (int(new_admin_id),))
        conn.commit()
        conn.close()


def create_voucher_db(code, days, file_limit=15, max_uses=1, valid_hours=0):
    clean_code = code.strip().upper()
    max_u = max(1, int(max_uses))
    vh = int(valid_hours or 0)
    expires_at = (datetime.now() + timedelta(hours=vh)).isoformat() if vh > 0 else ""
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute(
            """INSERT OR REPLACE INTO vouchers (code, days, file_limit, max_uses, used_count, is_used, expires_at)
               VALUES (?, ?, ?, ?, 0, 0, ?)""",
            (clean_code, int(days), int(file_limit), max_u, expires_at),
        )
        conn.commit()
        conn.close()


def get_all_vouchers():
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT code, days, file_limit, max_uses, used_count, is_used, expires_at FROM vouchers ORDER BY code ASC")
    rows = c.fetchall()
    conn.close()
    return rows


def delete_voucher_db(code):
    clean_code = code.strip().upper()
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM vouchers WHERE code = ?", (clean_code,))
        deleted = c.rowcount
        c.execute("DELETE FROM voucher_redemptions WHERE code = ?", (clean_code,))
        conn.commit()
        conn.close()
    return deleted > 0


def clean_inactive_vouchers_db():
    now = datetime.now()
    rows = get_all_vouchers()
    removed = 0
    for code, days, flim, max_u, used_c, is_used, exp_at in rows:
        max_u = max(1, int(max_u or 1))
        used_c = int(used_c or 0)
        is_exp = False
        if exp_at:
            try:
                is_exp = datetime.fromisoformat(exp_at) <= now
            except Exception:
                pass
        if is_used == 1 or used_c >= max_u or is_exp:
            if delete_voucher_db(code):
                removed += 1
    return removed


def build_vouchers_view_payload():
    rows = get_all_vouchers()
    if not rows:
        return "🎟️ এখনো কোনো ভাউচার কোড তৈরি করা হয়নি।", None
    now = datetime.now()
    lines = [f"🎟️ **CREATED VOUCHERS (Total: {len(rows)})**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    m = types.InlineKeyboardMarkup(row_width=1)
    m.add(types.InlineKeyboardButton("🧹 Clean Used / Expired Vouchers", callback_data="clean_vouchers"))
    for code, days, flim, max_u, used_c, is_used, exp_at in rows:
        max_u = max(1, int(max_u or 1))
        used_c = int(used_c or 0)
        exp_label = "No Exp"
        is_exp = False
        if exp_at:
            try:
                dt = datetime.fromisoformat(exp_at)
                is_exp = dt <= now
                exp_label = dt.strftime("%d/%m %H:%M")
            except Exception:
                pass
        if is_exp:
            st = "⏰ Expired"
        elif is_used == 1 or used_c >= max_u:
            st = "🔴 Used/Full"
        else:
            st = "🟢 Active"
        lines.append(f"• `{code}` — `{days}d` | Limit: `{flim}` | Used: `{used_c}/{max_u}` | Exp: `{exp_label}` | {st}")
        m.add(types.InlineKeyboardButton(f"🗑️ Delete: {code}", callback_data=f"del_vouch_{code}"))
    return "\n".join(lines), m


def is_txid_used(tx_id):
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT tx_id FROM used_txids WHERE tx_id=?", (str(tx_id).strip(),))
    row = c.fetchone()
    conn.close()
    return row is not None


def add_used_txid(tx_id):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO used_txids (tx_id) VALUES (?)", (str(tx_id).strip(),))
        conn.commit()
        conn.close()


def check_binance_payment(pay_order_id):
    if not BINANCE_API_KEY or BINANCE_API_KEY == "YOUR_NEW_BINANCE_API_KEY_HERE":
        return False, 0.0, "API Key কনফিগার করা নেই।"
    endpoint = "https://api.binance.com/sapi/v1/pay/transactions"
    timestamp = int(time.time() * 1000)
    query_string = f"timestamp={timestamp}"
    signature = hmac.new(BINANCE_SECRET_KEY.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
    url = f"{endpoint}?{query_string}&signature={signature}"
    headers = {"X-MBX-APIKEY": BINANCE_API_KEY}
    try:
        res = requests.get(url, headers=headers, timeout=10)
        if res.status_code == 200:
            data = res.json()
            transactions = data.get("data", []) if isinstance(data, dict) else data
            for item in transactions:
                if str(item.get("orderId", "")).strip() == str(pay_order_id).strip():
                    amount = float(item.get("amount", 0.0))
                    return True, amount, f"{amount} USDT"
            return False, 0.0, "TxID Binance এ পাওয়া যায়নি।"
        return False, 0.0, "Binance API Error"
    except Exception as e:
        return False, 0.0, f"Error: {e}"


def get_channel_url():
    if CHANNEL_INVITE_LINK:
        return CHANNEL_INVITE_LINK
    clean_ch = str(UPDATE_CHANNEL).lstrip("@")
    return f"https://t.me/{clean_ch}"


def check_force_sub(user_id):
    if user_id in admin_ids:
        return True
    try:
        channel_target = (
            f"@{UPDATE_CHANNEL}"
            if not str(UPDATE_CHANNEL).startswith(("@", "-100"))
            else str(UPDATE_CHANNEL)
        )
        member = bot.get_chat_member(channel_target, user_id)
        return member.status in ["member", "administrator", "creator", "restricted"]
    except Exception as e:
        logger.error(f"Force Sub Check Error for {user_id}: {e}")
        return False


def send_welcome_banner(chat_id, caption_text, reply_markup=None):
    if WELCOME_IMAGE_URL:
        try:
            bot.send_photo(chat_id, WELCOME_IMAGE_URL, caption=caption_text, reply_markup=reply_markup, parse_mode="Markdown")
            return
        except Exception as e:
            logger.warning(f"Welcome image URL send failed, trying local backup: {e}")

    if os.path.exists(WELCOME_IMAGE_PATH):
        try:
            with open(WELCOME_IMAGE_PATH, "rb") as img_f:
                bot.send_photo(chat_id, img_f, caption=caption_text, reply_markup=reply_markup, parse_mode="Markdown")
            return
        except Exception as e:
            logger.warning(f"Local welcome image send failed, falling back to text: {e}")

    bot.send_message(chat_id, caption_text, reply_markup=reply_markup, parse_mode="Markdown")


def get_user_folder(user_id):
    folder = os.path.join(UPLOAD_BOTS_DIR, str(user_id))
    os.makedirs(folder, exist_ok=True)
    return folder


def get_user_file_limit(user_id):
    if user_id == OWNER_ID:
        return OWNER_LIMIT
    if user_id in admin_ids:
        return ADMIN_LIMIT
    sub = user_subscriptions.get(user_id)
    if sub and sub["expiry"] > datetime.now():
        return int(sub.get("file_limit", SUBSCRIBED_USER_LIMIT))
    return FREE_USER_LIMIT


def get_user_file_count(user_id):
    return len(user_files.get(user_id, []))


def is_bot_running(script_owner_id, file_name):
    skey = f"{script_owner_id}_{file_name}"
    sinfo = bot_scripts.get(skey)
    if sinfo and sinfo.get("process"):
        try:
            proc = psutil.Process(sinfo["process"].pid)
            return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
        except Exception:
            return False
    return False


def kill_process_tree(sinfo):
    try:
        proc = sinfo.get("process")
        if proc and hasattr(proc, "pid"):
            parent = psutil.Process(proc.pid)
            for child in parent.children(recursive=True):
                child.terminate()
            parent.terminate()
    except Exception:
        pass
    try:
        lf = sinfo.get("log_file")
        if lf and not lf.closed:
            lf.close()
    except Exception:
        pass


def cleanup_all_scripts():
    for skey in list(bot_scripts.keys()):
        kill_process_tree(bot_scripts[skey])
        bot_scripts.pop(skey, None)


atexit.register(cleanup_all_scripts)


def get_active_proxy_for_user(uid):
    try:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT proxy_url FROM user_proxies WHERE user_id = ? AND is_active = 1 LIMIT 1", (int(uid),))
        row = c.fetchone()
        conn.close()
        return row[0] if row else ""
    except Exception:
        return ""


def run_script(script_path, owner_id, folder, fname, msg=None, bot_api_token=""):
    skey = f"{owner_id}_{fname}"
    if skey in bot_scripts:
        kill_process_tree(bot_scripts[skey])
    try:
        log_path = os.path.join(folder, f"{os.path.splitext(fname)[0]}.log")
        log_file = open(log_path, "a", encoding="utf-8", errors="ignore")
        env = os.environ.copy()
        if bot_api_token:
            env["BOT_TOKEN"] = bot_api_token
            env["TELEGRAM_BOT_TOKEN"] = bot_api_token
        active_px = get_active_proxy_for_user(owner_id)
        if active_px:
            env["HTTP_PROXY"] = active_px
            env["HTTPS_PROXY"] = active_px
            env["ALL_PROXY"] = active_px
        proc = subprocess.Popen(
            [sys.executable, script_path],
            cwd=folder,
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.PIPE,
            env=env,
        )
        bot_scripts[skey] = {
            "process": proc,
            "log_file": log_file,
            "file_name": fname,
            "file_path": script_path,
            "owner_id": owner_id,
            "start_time": datetime.now(),
            "folder": folder,
            "type": "py",
            "bot_token": bot_api_token,
            "auto_restart": True,
        }
        if msg:
            px_line = f"\n🌐 Proxy: `{active_px}`" if active_px else ""
            bot.reply_to(msg, f"🚀 **PYTHON BOT STARTED!**\n📄 File: `{fname}`\n🆔 PID: `{proc.pid}`{px_line}", parse_mode="Markdown")
    except Exception as e:
        if msg:
            bot.reply_to(msg, f"❌ Start Error: {e}")


def run_js_script(script_path, owner_id, folder, fname, msg=None, bot_api_token=""):
    skey = f"{owner_id}_{fname}"
    if skey in bot_scripts:
        kill_process_tree(bot_scripts[skey])
    try:
        log_path = os.path.join(folder, f"{os.path.splitext(fname)[0]}.log")
        log_file = open(log_path, "a", encoding="utf-8", errors="ignore")
        env = os.environ.copy()
        if bot_api_token:
            env["BOT_TOKEN"] = bot_api_token
            env["TELEGRAM_BOT_TOKEN"] = bot_api_token
        active_px = get_active_proxy_for_user(owner_id)
        if active_px:
            env["HTTP_PROXY"] = active_px
            env["HTTPS_PROXY"] = active_px
            env["ALL_PROXY"] = active_px
        proc = subprocess.Popen(
            ["node", script_path],
            cwd=folder,
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.PIPE,
            env=env,
        )
        bot_scripts[skey] = {
            "process": proc,
            "log_file": log_file,
            "file_name": fname,
            "file_path": script_path,
            "owner_id": owner_id,
            "start_time": datetime.now(),
            "folder": folder,
            "type": "js",
            "bot_token": bot_api_token,
            "auto_restart": True,
        }
        if msg:
            px_line = f"\n🌐 Proxy: `{active_px}`" if active_px else ""
            bot.reply_to(msg, f"🚀 **NODE.JS BOT STARTED!**\n📄 File: `{fname}`\n🆔 PID: `{proc.pid}`{px_line}", parse_mode="Markdown")
    except Exception as e:
        if msg:
            bot.reply_to(msg, f"❌ JS Start Error: {e}")


def auto_crash_recovery_loop():
    while True:
        time.sleep(20)
        try:
            for skey, sinfo in list(bot_scripts.items()):
                if not sinfo.get("auto_restart", True):
                    continue
                oid = sinfo.get("owner_id")
                fname = sinfo.get("file_name")
                fpath = sinfo.get("file_path")
                folder = sinfo.get("folder")
                stype = sinfo.get("type")
                btoken = sinfo.get("bot_token", "")
                if oid and fname and fpath and os.path.exists(fpath) and not is_bot_running(oid, fname):
                    logger.info(f"Auto-restarting crashed bot: {skey}")
                    if stype == "js":
                        run_js_script(fpath, oid, folder, fname, None, btoken)
                    else:
                        run_script(fpath, oid, folder, fname, None, btoken)

            now = datetime.now()
            for uid, sub in list(user_subscriptions.items()):
                if sub["expiry"] <= now and uid not in notified_expiries:
                    notified_expiries.add(uid)
                    try:
                        bot.send_message(uid, "⏳ আপনার VIP সাবস্ক্রিপশনের মেয়াদ শেষ হয়ে গেছে। পুনরায় **Earn VIP** বা **View Plans** থেকে সচল করুন।", parse_mode="Markdown")
                    except Exception:
                        pass
        except Exception as e:
            logger.error(f"Background monitor error: {e}")


def run_all_user_scripts():
    started_count = 0
    for uid, flist in list(user_files.items()):
        folder = get_user_folder(uid)
        token_file = os.path.join(folder, "bot_token.txt")
        saved_token = ""
        if os.path.exists(token_file):
            with open(token_file, "r", encoding="utf-8", errors="ignore") as tf:
                saved_token = tf.read().strip()

        for fname, ftype in flist:
            fpath = os.path.join(folder, fname)
            if os.path.exists(fpath) and not is_bot_running(uid, fname):
                if ftype == "js" or fname.endswith(".js"):
                    run_js_script(fpath, uid, folder, fname, None, saved_token)
                    started_count += 1
                elif ftype == "py" or fname.endswith(".py"):
                    run_script(fpath, uid, folder, fname, None, saved_token)
                    started_count += 1
    return started_count


def save_user_file(uid, fname, ftype, bot_api_token=""):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute(
            "INSERT OR REPLACE INTO user_files (user_id, file_name, file_type, bot_token) VALUES (?, ?, ?, ?)",
            (uid, fname, ftype, bot_api_token),
        )
        conn.commit()
        conn.close()
    user_files.setdefault(uid, [])
    user_files[uid] = [f for f in user_files[uid] if f[0] != fname]
    user_files[uid].append((fname, ftype))


def remove_user_file_db(uid, fname):
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("DELETE FROM user_files WHERE user_id = ? AND file_name = ?", (uid, fname))
        conn.commit()
        conn.close()
    if uid in user_files:
        user_files[uid] = [f for f in user_files[uid] if f[0] != fname]


def main_menu_markup(uid):
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    rows = [
        ["📢 𝗨𝗽𝗱𝗮𝘁𝗲𝘀 𝗖𝗵𝗮𝗻𝗻𝗲𝗹", "⚡ 𝗦𝗽𝗲𝗲𝗱 & 𝗣𝗶𝗻𝗴"],
        ["🚀 𝗨𝗽𝗹𝗼𝗮𝗱 𝗕𝗼𝘁", "📁 𝗠𝗮𝗻𝗮𝗴𝗲 𝗕𝗼𝘁𝘀"],
        ["🩺 𝗕𝗼𝘁 𝗛𝗲𝗮𝗹𝘁𝗵", "💾 𝗕𝗮𝗰𝗸𝘂𝗽"],
        ["🔥 𝗗𝗮𝗶𝗹𝘆 𝗦𝘁𝗿𝗲𝗮𝗸", "🌐 𝗣𝗿𝗼𝘅𝘆 𝗩𝗮𝘂𝗹𝘁"],
        ["🧪 𝗣𝗿𝗼𝘅𝘆 𝗧𝗲𝘀𝘁", "🔄 𝗥𝗼𝘁𝗮𝘁𝗲 𝗣𝗿𝗼𝘅𝘆"],
        ["✨ 𝗦𝘁𝘆𝗹𝗶𝘀𝘁 𝗖𝗮𝗿𝗱", "🛡️ 𝗩𝗜𝗣 𝗦𝗵𝗶𝗲𝗹𝗱"],
        ["🤖 𝗔𝗜 𝗖𝗼𝗱𝗲 𝗔𝘂𝗱𝗶𝘁", "🧠 𝗔𝗜 𝗟𝗼𝗴 𝗗𝗼𝗰"],
        ["✨ 𝗔𝗜 𝗦𝘁𝘆𝗹𝗶𝘀𝘁", "🔮 𝗔𝗜 𝗣𝗿𝗲𝗱𝗶𝗰𝘁"],
        ["💳 𝗩𝗶𝗲𝘄 𝗣𝗹𝗮𝗻𝘀", "🎁 𝗘𝗮𝗿𝗻 𝗩𝗜𝗣"],
        ["🎟️ 𝗥𝗲𝗱𝗲𝗲𝗺 𝗖𝗼𝗱𝗲", "🔗 𝗥𝗲𝗳𝗲𝗿𝗿𝗮𝗹"],
        ["👤 𝗠𝘆 𝗣𝗿𝗼𝗳𝗶𝗹𝗲", "🆘 𝗧𝗶𝗰𝗸𝗲𝘁"],
        ["💬 𝗦𝘂𝗽𝗽𝗼𝗿𝘁", "👑 𝗖𝗼𝗻𝘁𝗮𝗰𝘁 𝗢𝘄𝗻𝗲𝗿"],
    ]
    if uid in admin_ids:
        rows.insert(10, ["🛡️ 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹", "📊 𝗦𝘆𝘀𝘁𝗲𝗺 𝗦𝘁𝗮𝘁𝘀"])
    for r in rows:
        markup.add(*[types.KeyboardButton(t) for t in r])
    return markup


def admin_panel_markup():
    maint_label = "🛠️ Maint: ON (Click OFF)" if maintenance_mode else "🛠️ Maint: OFF (Click ON)"
    m = types.InlineKeyboardMarkup(row_width=2)
    m.add(
        types.InlineKeyboardButton("➕ Add Plan", callback_data="add_plan_init"),
        types.InlineKeyboardButton("🗑️ Manage Plans", callback_data="manage_plans"),
    )
    m.add(
        types.InlineKeyboardButton("💎 Add Sub", callback_data="add_subscription"),
        types.InlineKeyboardButton("❌ Remove Sub", callback_data="remove_subscription"),
    )
    m.add(
        types.InlineKeyboardButton("🎟️ Create Voucher", callback_data="create_voucher"),
        types.InlineKeyboardButton("🎲 Gen Vouchers", callback_data="gen_bulk_vouchers"),
    )
    m.add(
        types.InlineKeyboardButton("📋 View Vouchers", callback_data="view_vouchers"),
        types.InlineKeyboardButton("🔓 Reset Device", callback_data="reset_device"),
    )
    m.add(
        types.InlineKeyboardButton("🚨 Abuse Logs", callback_data="view_abuse_logs"),
        types.InlineKeyboardButton("🚫 Ban", callback_data="ban_user"),
    )
    m.add(
        types.InlineKeyboardButton("✅ Unban", callback_data="unban_user"),
        types.InlineKeyboardButton("📣 Broadcast", callback_data="broadcast"),
    )
    m.add(
        types.InlineKeyboardButton("⚙️ Run All", callback_data="run_all_scripts"),
        types.InlineKeyboardButton("🛑 Stop All", callback_data="stop_all_scripts"),
    )
    m.add(
        types.InlineKeyboardButton("➕ Add Admin", callback_data="add_admin"),
        types.InlineKeyboardButton(maint_label, callback_data="toggle_maint"),
    )
    m.add(
        types.InlineKeyboardButton("📊 System Stats", callback_data="stats"),
    )
    return m


# --- 10 Premium / VIP / Proxy / Smart / Stylist Features ---
def _logic_proxy_vault(message):
    uid = message.from_user.id
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT proxy_id, proxy_url, latency_ms, is_active FROM user_proxies WHERE user_id = ? ORDER BY proxy_id DESC", (uid,))
    rows = c.fetchall()
    conn.close()

    pending_proxy_users.add(uid)
    lines = [f"🌐 **PROXY VAULT (`{uid}`)**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    if not rows:
        lines.append("ℹ️ এখনো কোনো প্রক্সি সেভ করা নেই।")
    else:
        for pid, purl, lat, act in rows[:10]:
            st = "🟢 Active" if int(act or 0) == 1 else "⚪ Standby"
            lines.append(f"• `#{pid}` `{purl}` — `{lat}ms` | {st}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("➕ নতুন প্রক্সি যোগ করতে `http://ip:port` বা `socks5://ip:port` লিখে পাঠান *(বাতিল করতে `cancel`)*।")
    msg = bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_add_proxy)


def process_add_proxy(message):
    uid = message.from_user.id
    pending_proxy_users.discard(uid)
    raw = (message.text or "").strip()
    if not raw or raw.lower() == "cancel":
        bot.reply_to(message, "❌ Proxy Vault ইনপুট বাতিল করা হয়েছে।")
        return
    if not re.match(r"^(https?|socks5h?)://[\w.-]+:\d+$", raw, re.IGNORECASE):
        bot.reply_to(message, "⚠️ সঠিক প্রক্সি ফরম্যাট দিন, যেমন: `http://127.0.0.1:8080` বা `socks5://user:pass@host:1080`।", parse_mode="Markdown")
        return

    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("UPDATE user_proxies SET is_active = 0 WHERE user_id = ?", (uid,))
        c.execute(
            "INSERT INTO user_proxies (user_id, proxy_url, latency_ms, is_active, created_at) VALUES (?, ?, 0, 1, ?)",
            (uid, raw, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
    bot.reply_to(message, f"✅ প্রক্সি সেভ ও Active করা হয়েছে:\n`{raw}`", parse_mode="Markdown")


def _logic_proxy_test(message):
    uid = message.from_user.id
    play_stylist_animation(message, "SMART PROXY TESTER")
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT proxy_id, proxy_url, is_active FROM user_proxies WHERE user_id = ?", (uid,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        bot.reply_to(message, "🧪 টেস্ট করার মতো কোনো প্রক্সি পাওয়া যায়নি। আগে **🌐 𝗣𝗿𝗼𝘅𝘆 𝗩𝗮𝘂𝗹𝘁** এ প্রক্সি যোগ করুন।", parse_mode="Markdown")
        return

    lines = ["🧪 **SMART PROXY DIAGNOSTICS**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for pid, purl, act in rows[:8]:
        t0 = time.time()
        ok = False
        try:
            r = requests.get("https://api.telegram.org", proxies={"http": purl, "https": purl}, timeout=4)
            ok = r.status_code in (200, 301, 302, 404)
        except Exception:
            ok = False
        ms = int((time.time() - t0) * 1000)
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute("UPDATE user_proxies SET latency_ms = ? WHERE proxy_id = ?", (ms, pid))
            conn.commit()
            conn.close()
        badge = "🟢 Online" if ok else "🔴 Timeout/Err"
        act_tag = " [ACTIVE]" if int(act or 0) == 1 else ""
        lines.append(f"• `#{pid}` `{purl}`{act_tag}\n  Status: {badge} | Latency: `{ms}ms`")
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


def _logic_rotate_proxy(message):
    uid = message.from_user.id
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT proxy_id, proxy_url, is_active FROM user_proxies WHERE user_id = ? ORDER BY proxy_id ASC", (uid,))
    rows = c.fetchall()
    conn.close()

    if len(rows) < 2:
        bot.reply_to(message, "🔄 রোটেশন করার জন্য অন্তত ২টি প্রক্সি প্রয়োজন। **🌐 𝗣𝗿𝗼𝘅𝘆 𝗩𝗮𝘂𝗹𝘁** থেকে আরও প্রক্সি যোগ করুন।", parse_mode="Markdown")
        return

    active_idx = 0
    for idx, r in enumerate(rows):
        if int(r[3] if len(r) > 3 else r[2]) == 1:
            active_idx = idx
            break
    next_row = rows[(active_idx + 1) % len(rows)]
    next_id, next_url = next_row[0], next_row[1]

    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("UPDATE user_proxies SET is_active = 0 WHERE user_id = ?", (uid,))
        c.execute("UPDATE user_proxies SET is_active = 1 WHERE proxy_id = ?", (next_id,))
        conn.commit()
        conn.close()

    bot.reply_to(
        message,
        f"🔄 **AUTO PROXY ROTATED!**\n"
        f"🌐 New Active Proxy (`#{next_id}`): `{next_url}`\n"
        f"⚡ নতুন বট চালু বা রিস্টার্ট করলে এই প্রক্সিটি `HTTP_PROXY` / `HTTPS_PROXY` হিসেবে ইনজেক্ট হবে।",
        parse_mode="Markdown",
    )


def _logic_stylist_card(message):
    uid = message.from_user.id
    play_stylist_animation(message, "STYLIST VIP CARD")
    sub = user_subscriptions.get(uid)
    is_vip = bool(sub and sub["expiry"] > datetime.now())
    tier = "👑 OWNER" if uid == OWNER_ID else ("🛡️ ADMIN" if uid in admin_ids else ("💎 VIP" if is_vip else "🆓 FREE"))
    px = get_active_proxy_for_user(uid) or "Direct (No Proxy)"
    flist = user_files.get(uid, [])
    running_cnt = sum(1 for fn, _ in flist if is_bot_running(uid, fn))

    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT streak_count FROM daily_streaks WHERE user_id = ?", (uid,))
    srow = c.fetchone()
    streak = int(srow[0] or 0) if srow else 0
    conn.close()

    card = (
        f"✨ ╔══════════════════════════╗ ✨\n"
        f"      **HOSTIFY VIP STYLIST CARD**\n"
        f"✨ ╚══════════════════════════╝ ✨\n"
        f"👤 **User:** `{message.from_user.first_name}` (`{uid}`)\n"
        f"🏅 **Tier:** `{tier}`\n"
        f"🔥 **Streak:** `{streak}` দিন\n"
        f"🚀 **Bots:** `{running_cnt}/{len(flist)}` Running (Limit: `{get_user_file_limit(uid)}`)\n"
        f"🌐 **Proxy:** `{px}`\n"
        f"⚡ **Node Load:** CPU `{psutil.cpu_percent()}%` | RAM `{psutil.virtual_memory().percent}%`"
    )
    bot.reply_to(message, card, parse_mode="Markdown")


def _logic_vip_shield(message):
    uid = message.from_user.id
    flist = user_files.get(uid, [])
    shielded = 0
    for fn, _ in flist:
        skey = f"{uid}_{fn}"
        if skey in bot_scripts:
            bot_scripts[skey]["auto_restart"] = True
            shielded += 1
    bot.reply_to(
        message,
        f"🛡️ **VIP ANTI-CRASH SHIELD ACTIVE**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Auto-Restart Guard Enabled on `{shielded}` active bot(s).\n"
        f"🔁 কোনো বট ক্র্যাশ করলে ২০ সেকেন্ডের মধ্যে অটোমেটিক রিস্টার্ট হবে।",
        parse_mode="Markdown",
    )


# --- 10 Premium / VIP / Proxy / Smart / Unique / Stylist AI Features ---
def _logic_ai_code_audit(message):
    uid = message.from_user.id
    pending_ai_audit_users.add(uid)
    msg = bot.reply_to(
        message,
        "🤖 **AI CODE AUDITOR**\n"
        "যে বট ফাইলের AI অডিট চান সেটির নাম লিখুন (যেমন: `bot.py`) অথবা সরাসরি Python/JS কোড পেস্ট করুন *(বাতিল করতে `cancel`)*:",
        parse_mode="Markdown",
    )
    bot.register_next_step_handler(msg, process_ai_code_audit)


def process_ai_code_audit(message):
    uid = message.from_user.id
    pending_ai_audit_users.discard(uid)
    raw = (message.text or "").strip()
    if not raw or raw.lower() == "cancel":
        bot.reply_to(message, "❌ AI Code Audit বাতিল করা হয়েছে।")
        return

    folder = get_user_folder(uid)
    candidate_path = os.path.join(folder, os.path.basename(raw))
    if os.path.exists(candidate_path) and os.path.isfile(candidate_path):
        with open(candidate_path, "r", encoding="utf-8", errors="ignore") as f:
            code_src = f.read()
        target_label = os.path.basename(raw)
    else:
        code_src = raw
        target_label = "Pasted Snippet"

    findings = []
    score = 95
    if re.search(r"\d{8,10}:[A-Za-z0-9_-]{35}", code_src):
        findings.append("⚠️ হার্ডকোডেড Telegram Bot Token পাওয়া গেছে — `os.environ.get('BOT_TOKEN')` ব্যবহার করুন।")
        score -= 20
    if "eval(" in code_src or "exec(" in code_src:
        findings.append("🚨 `eval()` বা `exec()` ব্যবহার করা হয়েছে — নিরাপত্তা ঝুঁকি থাকতে পারে।")
        score -= 25
    if "while True:" in code_src and "sleep" not in code_src:
        findings.append("⚡ `while True:` লুপে `time.sleep()` নেই — CPU স্পাইক হতে পারে।")
        score -= 15
    if "except:" in code_src:
        findings.append("💡 Bare `except:` ব্লক রয়েছে — নির্দিষ্ট Exception হ্যান্ডেল করা উত্তম।")
        score -= 10
    if not findings:
        findings.append("✅ কোনো গুরুতর কোড বা সিকিউরিটি ত্রুটি পাওয়া যায়নি।")

    score = max(10, score)
    report = (
        f"🤖 **AI CODE AUDIT REPORT (`{target_label}`)**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 **AI Health Score:** `{score}/100`\n"
        f"🔍 **Findings:**\n" + "\n".join(f"• {x}" for x in findings)
    )
    bot.reply_to(message, report, parse_mode="Markdown")


def _logic_ai_log_doc(message):
    uid = message.from_user.id
    flist = user_files.get(uid, [])
    folder = get_user_folder(uid)
    if not flist:
        bot.reply_to(message, "🧠 বিশ্লেষণ করার মতো কোনো বট ফাইল বা লগ পাওয়া যায়নি।")
        return

    lines = ["🧠 **AI LOG ERROR DOCTOR**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for fname, _ in flist[:6]:
        log_path = os.path.join(folder, f"{os.path.splitext(fname)[0]}.log")
        if not os.path.exists(log_path):
            lines.append(f"• `{fname}`: কোনো লগ ফাইল নেই।")
            continue
        with open(log_path, "r", encoding="utf-8", errors="ignore") as lf:
            tail = lf.read()[-3000:]
        diag = "✅ Healthy — কোনো ক্র্যাশ/এরর নেই।"
        if "ModuleNotFoundError" in tail or "No module named" in tail:
            diag = "❌ Missing Package (`ModuleNotFoundError`) — `requirements.txt` বা প্যাকেজ ইনস্টল করুন।"
        elif "SyntaxError" in tail or "IndentationError" in tail:
            diag = "❌ Syntax/Indentation Error — কোডের ইন্ডেন্টেশন ও সিনট্যাক্স চেক করুন।"
        elif "Conflict: terminated by other getUpdates" in tail:
            diag = "⚠️ Telegram Polling Conflict — একই টোকেন দিয়ে অন্য কোথাও বট চলছে।"
        elif "401 Unauthorized" in tail:
            diag = "🔑 Invalid Bot Token (`401`) — সঠিক BotFather টোকেন দিন।"
        lines.append(f"• `{fname}`:\n  {diag}")
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


def _logic_ai_stylist(message):
    uid = message.from_user.id
    pending_ai_persona_users.add(uid)
    msg = bot.reply_to(
        message,
        "✨ **AI STYLIST & PERSONA STUDIO**\n"
        "আপনার বটের নাম বা যেকোনো মেসেজ পাঠান — AI সেটিকে **VIP Stylist** ফরম্যাটে সাজিয়ে দেবে *(বাতিল করতে `cancel`)*:",
        parse_mode="Markdown",
    )
    bot.register_next_step_handler(msg, process_ai_stylist)


def process_ai_stylist(message):
    uid = message.from_user.id
    pending_ai_persona_users.discard(uid)
    txt = (message.text or "").strip()
    if not txt or txt.lower() == "cancel":
        bot.reply_to(message, "❌ AI Stylist Studio বাতিল করা হয়েছে।")
        return

    styled = (
        f"✨ **━━━━━━━━━━━━━━━━━━━━━** ✨\n"
        f"💎 **VIP STYLIST EDITION**\n"
        f"✨ **━━━━━━━━━━━━━━━━━━━━━** ✨\n"
        f"🔹 {txt}\n"
        f"⚡ *Powered by HOSTIFY Cloud & AI*"
    )
    bot.reply_to(message, styled, parse_mode="Markdown")


def _logic_ai_predict(message):
    uid = message.from_user.id
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    risk = min(99, int(cpu * 0.4 + ram * 0.45 + len(bot_scripts) * 1.5))
    level = "🟢 Low Risk" if risk < 40 else ("🟡 Moderate Risk" if risk < 70 else "🔴 High Load Risk")
    bot.reply_to(
        message,
        f"🔮 **AI CRASH PREDICTOR & DEPLOY ADVISOR**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 **Predicted Instability:** `{risk}%` ({level})\n"
        f"🖥️ **System:** CPU `{cpu}%` | RAM `{ram}%`\n"
        f"🚀 **AI Deploy Tip:** টোকেন `BOT_TOKEN` এনভায়রনমেন্টে রাখুন এবং আপলোডের আগে লোকালি একবার রান করে যাচাই করুন।",
        parse_mode="Markdown",
    )


# --- Feature: Daily Streak with 7-Day VIP Reward ---
def _logic_daily_streak(message):
    uid = message.from_user.id
    now = datetime.now()

    if uid not in admin_ids and uid != OWNER_ID:
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute("SELECT device_hash FROM user_devices WHERE user_id = ?", (uid,))
            drow = c.fetchone()
            dev_hash = drow[0] if (drow and drow[0]) else ""
            conn.close()

        if dev_hash:
            allowed, _, reason_bn = bind_or_check_device(uid, dev_hash, source="daily_streak", enforce_strict=True)
            if not allowed:
                bot.reply_to(message, reason_bn, parse_mode="Markdown")
                return

    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT streak_count, last_claim_at FROM daily_streaks WHERE user_id = ?", (uid,))
        row = c.fetchone()

        streak_count = int(row[0] or 0) if row else 0
        last_claim_dt = None
        if row and row[1]:
            try:
                last_claim_dt = datetime.fromisoformat(row[1])
            except Exception:
                last_claim_dt = None

        if last_claim_dt is not None:
            elapsed = now - last_claim_dt
            if elapsed < timedelta(hours=24):
                rem = timedelta(hours=24) - elapsed
                hrs = rem.seconds // 3600
                mins = (rem.seconds % 3600) // 60
                conn.close()
                bot.reply_to(
                    message,
                    f"🔥 **DAILY STREAK STATUS**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 Current Streak: `{streak_count}` দিন\n"
                    f"⏳ পরবর্তী ক্লেইম করতে পারবেন `{hrs} ঘণ্টা {mins} মিনিট` পর।",
                    parse_mode="Markdown",
                )
                return

            if elapsed <= timedelta(hours=48):
                new_streak = streak_count + 1
            else:
                new_streak = 1
        else:
            new_streak = 1

        c.execute(
            "INSERT OR REPLACE INTO daily_streaks (user_id, streak_count, last_claim_at) VALUES (?, ?, ?)",
            (uid, new_streak, now.isoformat()),
        )
        conn.commit()
        conn.close()

    bonus_msg = ""
    if new_streak % STREAK_REWARD_DAYS == 0:
        new_exp, final_lim = save_subscription(uid, "Streak Reward VIP", STREAK_BONUS_VIP_DAYS, SUBSCRIBED_USER_LIMIT, stack=True)
        bonus_msg = (
            f"\n🎉 **STREAK MILESTONE!** `{STREAK_REWARD_DAYS}` দিন পূর্ণ হওয়ায় `+{STREAK_BONUS_VIP_DAYS}` দিন VIP (Limit: `{final_lim}`) যুক্ত হয়েছে!\n"
            f"⏳ মেয়াদ: `{new_exp.strftime('%Y-%m-%d %H:%M')}`"
        )

    rem_to_reward = STREAK_REWARD_DAYS - (new_streak % STREAK_REWARD_DAYS)
    if rem_to_reward == STREAK_REWARD_DAYS:
        rem_to_reward = STREAK_REWARD_DAYS

    bot.reply_to(
        message,
        f"🔥 **DAILY STREAK UPDATED!**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ আজকের স্ট্রিক ক্লেইম সফল হয়েছে!\n"
        f"📈 Current Streak: `{new_streak}` দিন\n"
        f"🎁 Next VIP Reward in: `{rem_to_reward}` দিন"
        f"{bonus_msg}",
        parse_mode="Markdown",
    )


# --- Feature: Instant User Bot Files ZIP Backup ---
def _logic_bot_backup(message):
    uid = message.from_user.id
    folder = get_user_folder(uid)
    files_to_zip = []
    for root, _, files in os.walk(folder):
        for fname in files:
            if fname.endswith(".log"):
                continue
            full_p = os.path.join(root, fname)
            rel_p = os.path.relpath(full_p, folder)
            files_to_zip.append((full_p, rel_p))

    if not files_to_zip:
        bot.reply_to(message, "📂 ব্যাকআপ করার মতো আপনার কোনো বট ফাইল নেই। আগে **🚀 𝗨𝗽𝗹𝗼𝗮𝗱 𝗕𝗼𝘁** থেকে ফাইল আপলোড করুন।", parse_mode="Markdown")
        return

    wait_msg = bot.reply_to(message, "💾 আপনার বট ফাইলগুলোর `.zip` ব্যাকআপ তৈরি হচ্ছে...", parse_mode="Markdown")
    buf = io.BytesIO()
    try:
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for full_p, rel_p in files_to_zip:
                zf.write(full_p, arcname=rel_p)
        buf.seek(0)
        zip_name = f"hostify_backup_{uid}_{datetime.now().strftime('%Y%m%d_%H%M')}.zip"
        bot.send_document(
            message.chat.id,
            buf,
            visible_file_name=zip_name,
            caption=f"💾 **BOT BACKUP READY**\n📄 Total Files: `{len(files_to_zip)}`",
            parse_mode="Markdown",
        )
        try:
            bot.delete_message(message.chat.id, wait_msg.message_id)
        except Exception:
            pass
    except Exception as e:
        bot.edit_message_text(f"❌ ব্যাকআপ তৈরি করতে সমস্যা হয়েছে: `{e}`", message.chat.id, wait_msg.message_id, parse_mode="Markdown")


# --- Feature: Live Bot Health & Process Inspector ---
def _logic_bot_health(message):
    uid = message.from_user.id
    flist = user_files.get(uid, [])
    folder = get_user_folder(uid)
    now = datetime.now()

    if not flist:
        bot.reply_to(
            message,
            f"🩺 **BOT HEALTH MONITOR**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 Hosted Files: `0` / `{get_user_file_limit(uid)}`\n"
            f"⚡ Server CPU: `{psutil.cpu_percent()}%` | RAM: `{psutil.virtual_memory().percent}%`\n"
            f"💡 নতুন বট চালু করতে **🚀 𝗨𝗽𝗹𝗼𝗮𝗱 𝗕𝗼𝘁** চাপুন।",
            parse_mode="Markdown",
        )
        return

    lines = [f"🩺 **BOT HEALTH MONITOR (`{uid}`)**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    running_cnt = 0
    for fname, ftype in flist:
        skey = f"{uid}_{fname}"
        sinfo = bot_scripts.get(skey, {})
        running = is_bot_running(uid, fname)
        if running:
            running_cnt += 1
        pid = sinfo["process"].pid if (running and sinfo.get("process")) else "N/A"
        start_dt = sinfo.get("start_time")
        uptime_str = "Stopped"
        if running and isinstance(start_dt, datetime):
            delta = now - start_dt
            hrs = delta.seconds // 3600 + delta.days * 24
            mins = (delta.seconds % 3600) // 60
            uptime_str = f"{hrs}h {mins}m"

        log_path = os.path.join(folder, f"{os.path.splitext(fname)[0]}.log")
        log_kb = round(os.path.getsize(log_path) / 1024, 1) if os.path.exists(log_path) else 0.0
        st_icon = "🟢 Running" if running else "🔴 Stopped"
        lines.append(
            f"• `{fname}` (`{ftype.upper()}`) — {st_icon}\n"
            f"  PID: `{pid}` | Uptime: `{uptime_str}` | Log: `{log_kb} KB`"
        )

    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"📊 Active: `{running_cnt}/{len(flist)}` | CPU: `{psutil.cpu_percent()}%` | RAM: `{psutil.virtual_memory().percent}%`")
    bot.reply_to(message, "\n".join(lines), parse_mode="Markdown")


# --- Feature: Support Ticket System with Admin Reply ---
def _logic_ticket_prompt(message):
    uid = message.from_user.id
    pending_ticket_users.add(uid)
    msg = bot.reply_to(
        message,
        "🆘 **SUPPORT TICKET**\n"
        "আপনার সমস্যা বা অনুরোধ বিস্তারিত লিখে পাঠান।\n"
        "*(বাতিল করতে `cancel` লিখুন)*",
        parse_mode="Markdown",
    )
    bot.register_next_step_handler(msg, process_support_ticket)


def process_support_ticket(message):
    uid = message.from_user.id
    pending_ticket_users.discard(uid)
    text = (message.text or "").strip()
    if not text:
        bot.reply_to(message, "⚠️ খালি টিকিট পাঠানো যাবে না।")
        return
    if text.lower() == "cancel":
        bot.reply_to(message, "❌ সাপোর্ট টিকিট বাতিল করা হয়েছে।")
        return

    uname = f"@{message.from_user.username}" if message.from_user.username else "NoUsername"
    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute(
            "INSERT INTO support_tickets (user_id, username, message_text, status, created_at) VALUES (?, ?, ?, 'OPEN', ?)",
            (uid, uname, text, datetime.now().isoformat()),
        )
        ticket_id = c.lastrowid
        conn.commit()
        conn.close()

    bot.reply_to(
        message,
        f"✅ **টিকিট #{ticket_id} জমা হয়েছে!**\nআমাদের টিম দ্রুত আপনার সাথে যোগাযোগ করবে।",
        parse_mode="Markdown",
    )

    admin_msg = (
        f"🆘 **NEW SUPPORT TICKET #{ticket_id}**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 User: `{uid}` ({uname})\n"
        f"📝 Message:\n{text}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    rm = types.InlineKeyboardMarkup()
    rm.add(types.InlineKeyboardButton(f"💬 Reply to #{ticket_id}", callback_data=f"reply_tkt_{ticket_id}_{uid}"))

    notified = set()
    for aid in set(admin_ids) | {OWNER_ID}:
        if aid in notified:
            continue
        notified.add(aid)
        try:
            bot.send_message(aid, admin_msg, reply_markup=rm, parse_mode="Markdown")
        except Exception:
            pass


def process_admin_ticket_reply(message):
    aid = message.from_user.id
    info = pending_admin_replies.pop(aid, None)
    if not info:
        return
    reply_txt = (message.text or "").strip()
    if not reply_txt:
        bot.reply_to(message, "⚠️ খালি রিপ্লাই পাঠানো যাবে না।")
        return

    target_uid = info["target_uid"]
    ticket_id = info["ticket_id"]
    try:
        bot.send_message(
            target_uid,
            f"📩 **SUPPORT REPLY (Ticket #{ticket_id})**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{reply_txt}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown",
        )
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute("UPDATE support_tickets SET status = 'ANSWERED' WHERE ticket_id = ?", (ticket_id,))
            conn.commit()
            conn.close()
        bot.reply_to(message, f"✅ Ticket #{ticket_id} এর রিপ্লাই ইউজার `{target_uid}` এর কাছে পাঠানো হয়েছে।", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(message, f"❌ রিপ্লাই পাঠাতে সমস্যা হয়েছে: {e}")


@bot.message_handler(commands=["start"])
def start_cmd(message):
    uid = message.from_user.id
    if is_anti_flood(uid):
        return
    blocked, reason = is_blocked_for_user(uid)
    if blocked:
        bot.reply_to(message, reason)
        return

    args = message.text.split()
    if len(args) > 1 and uid not in active_users:
        try:
            ref_id = int(args[1])
            if ref_id != uid:
                with DB_LOCK:
                    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
                    c = conn.cursor()
                    c.execute("INSERT OR IGNORE INTO referrals (user_id, referred_by) VALUES (?, ?)", (uid, ref_id))
                    c.execute("INSERT OR IGNORE INTO referrals (user_id, ref_count) VALUES (?, 0)", (ref_id,))
                    c.execute("UPDATE referrals SET ref_count = ref_count + 1 WHERE user_id = ?", (ref_id,))
                    conn.commit()
                    conn.close()
        except Exception:
            pass

    if uid not in active_users:
        active_users.add(uid)
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO active_users (user_id) VALUES (?)", (uid,))
            conn.commit()
            conn.close()

    is_joined = check_force_sub(uid)
    verify_markup = types.InlineKeyboardMarkup()
    verify_markup.add(types.InlineKeyboardButton("📢 Join Official Channel", url=get_channel_url()))
    verify_markup.add(types.InlineKeyboardButton("🔄 Verify Membership", callback_data="check_join"))

    welcome_caption = (
        f"✨ **WELCOME TO HOSTIFY CLOUD** ✨\n"
        f"👋 Hello, **{message.from_user.first_name}**!\n"
        f"⚡ 24/7 Python & Node.js Bot Hosting + Proxy & AI Suite.\n\n"
        + ("✅ আপনার মেম্বারশিপ ভেরিফাইড!" if is_joined else "⚠️ বটের পূর্ণ সেবা পেতে নিচের চ্যানেলে জয়েন করে **Verify Membership** চাপুন।")
    )
    send_welcome_banner(
        message.chat.id,
        welcome_caption,
        reply_markup=main_menu_markup(uid) if is_joined else verify_markup,
    )


@bot.callback_query_handler(func=lambda call: call.data == "check_join")
def check_join_cb(call):
    uid = call.from_user.id
    blocked, reason = is_blocked_for_user(uid)
    if blocked:
        bot.answer_callback_query(call.id, reason, show_alert=True)
        return
    if check_force_sub(uid):
        bot.answer_callback_query(call.id, "✅ Verified!")
        try:
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except Exception:
            pass
        send_welcome_banner(
            call.message.chat.id,
            f"✅ **VERIFIED!**\n"
            f"✨ **WELCOME TO HOSTIFY CLOUD** ✨\n"
            f"👋 Hello, **{call.from_user.first_name}**!\n"
            f"⚡ 24/7 Python & Node.js Bot Hosting + Proxy & AI Suite.",
            reply_markup=main_menu_markup(uid),
        )
    else:
        bot.answer_callback_query(call.id, "❌ আপনি এখনো চ্যানেলে জয়েন করেননি!", show_alert=True)


def _logic_my_profile(message):
    uid = message.from_user.id
    sub = user_subscriptions.get(uid)
    is_vip_active = bool(sub and sub["expiry"] > datetime.now())
    status = (
        "👑 System Owner"
        if uid == OWNER_ID
        else (
            "🛡️ Administrator"
            if uid in admin_ids
            else (
                f"💎 {sub['plan_name']}"
                if is_vip_active
                else "🆓 Standard Free Tier"
            )
        )
    )
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT ref_count FROM referrals WHERE user_id = ?", (uid,))
    row = c.fetchone()
    ref_count = row[0] if row else 0
    c.execute("SELECT device_hash FROM user_devices WHERE user_id = ?", (uid,))
    drow = c.fetchone()
    dev_status = "Locked 🔒" if (drow and drow[0]) else "Not Bound 🔓"
    c.execute("SELECT streak_count FROM daily_streaks WHERE user_id = ?", (uid,))
    srow = c.fetchone()
    streak_val = int(srow[0] or 0) if srow else 0
    c.execute("SELECT COUNT(*) FROM user_proxies WHERE user_id = ?", (uid,))
    prow = c.fetchone()
    proxy_cnt = int(prow[0] or 0) if prow else 0
    conn.close()

    expiry_str = "Lifetime" if uid in admin_ids else "None"
    if uid not in admin_ids and is_vip_active:
        expiry_str = sub["expiry"].strftime("%Y-%m-%d %H:%M")

    text = (
        f"👤 **USER ACCOUNT PROFILE**\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 Telegram ID: `{uid}`\n"
        f"🔰 Account Tier: `{status}`\n"
        f"🔐 Device Guard: `{dev_status}`\n"
        f"🔥 Daily Streak: `{streak_val}` দিন\n"
        f"🌐 Saved Proxies: `{proxy_cnt}`\n"
        f"⏳ Expiry: `{expiry_str}`\n"
        f"📁 Deployed Bots: `{get_user_file_count(uid)}` / `{get_user_file_limit(uid)}`\n"
        f"🎁 Successful Referrals: `{ref_count} users`\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    )
    bot.reply_to(message, text, parse_mode="Markdown")


def _logic_referral(message):
    uid = message.from_user.id
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("SELECT ref_count FROM referrals WHERE user_id = ?", (uid,))
    row = c.fetchone()
    ref_count = row[0] if row else 0
    conn.close()
    bot_user = bot.get_me().username
    bot.reply_to(
        message,
        f"🎁 **YOUR REFERRAL LINK**\n━━━━━━━━━━━━━━━━━━━\n`https://t.me/{bot_user}?start={uid}`\n\n👥 Total Invited: `{ref_count}`",
        parse_mode="Markdown",
    )


def _logic_redeem_prompt(message):
    msg = bot.reply_to(message, "🎟️ আপনার ভাউচার বা প্রোমো কোডটি লিখুন:", parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_redeem_voucher)


def process_redeem_voucher(message):
    code = message.text.strip().upper()
    uid = message.from_user.id

    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT days, file_limit, max_uses, used_count, is_used, expires_at FROM vouchers WHERE code = ?", (code,))
        row = c.fetchone()
        if not row:
            conn.close()
            bot.reply_to(message, "❌ ভুল অথবা অকার্যকর ভাউচার কোড!")
            return

        days, limit, max_uses, used_count, is_used, expires_at = row
        max_uses = max(1, int(max_uses or 1))
        used_count = int(used_count or 0)

        if expires_at:
            try:
                if datetime.fromisoformat(expires_at) <= datetime.now():
                    conn.close()
                    bot.reply_to(message, "⏰ এই ভাউচার কোডটির মেয়াদ শেষ হয়ে গেছে!")
                    return
            except Exception:
                pass

        if is_used == 1 or used_count >= max_uses:
            conn.close()
            bot.reply_to(message, "❌ এই ভাউচার কোডটির ব্যবহারের লিমিট শেষ হয়ে গেছে!")
            return

        c.execute("SELECT id FROM voucher_redemptions WHERE code = ? AND user_id = ?", (code, uid))
        if c.fetchone():
            conn.close()
            bot.reply_to(message, "❌ আপনি এই অ্যাকাউন্ট থেকে ইতিমধ্যে এই ভাউচার কোডটি ব্যবহার করেছেন!")
            return

        c.execute("SELECT device_hash FROM user_devices WHERE user_id = ?", (uid,))
        dev_row = c.fetchone()
        dev_hash = dev_row[0] if (dev_row and dev_row[0]) else ""
        conn.close()

    if uid not in admin_ids and uid != OWNER_ID:
        if not dev_hash:
            bot.reply_to(
                message,
                "❌ আপনার ডিভাইস এখনো সিস্টেমের সাথে ভেরিফাইড নয়!\n"
                "ভাউচার রিডিম করার আগে **🎁 Earn VIP** থেকে অ্যাড পেজ ভেরিফাই করে ডিভাইস লক সম্পন্ন করুন।",
                parse_mode="Markdown",
            )
            return

        allowed, _, reason_bn = bind_or_check_device(uid, dev_hash, source="voucher_redeem", enforce_strict=True)
        if not allowed:
            bot.reply_to(message, reason_bn, parse_mode="Markdown")
            return

    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT days, file_limit, max_uses, used_count, is_used FROM vouchers WHERE code = ?", (code,))
        fresh = c.fetchone()
        if not fresh:
            conn.close()
            bot.reply_to(message, "❌ ভাউচার কোডটি পাওয়া যায়নি!")
            return
        _, _, f_max, f_used, f_is_used = fresh
        f_max = max(1, int(f_max or 1))
        f_used = int(f_used or 0)
        if f_is_used == 1 or f_used >= f_max:
            conn.close()
            bot.reply_to(message, "❌ এই ভাউচার কোডটির ব্যবহারের লিমিট শেষ হয়ে গেছে!")
            return

        if dev_hash:
            c.execute("SELECT id FROM voucher_redemptions WHERE code = ? AND device_hash = ?", (code, dev_hash))
            if c.fetchone():
                conn.close()
                log_device_abuse(uid, 0, dev_hash, "voucher_redeem", f"Duplicate device redemption for {code}")
                bot.reply_to(message, "❌ এই ডিভাইস থেকে ইতিমধ্যে এই ভাউচার কোডটি ব্যবহার করা হয়েছে!")
                return

        new_used = f_used + 1
        new_is_used = 1 if new_used >= f_max else 0

        c.execute(
            "UPDATE vouchers SET used_count = ?, is_used = ? WHERE code = ?",
            (new_used, new_is_used, code),
        )
        c.execute(
            "INSERT INTO voucher_redemptions (code, user_id, device_hash, redeemed_at) VALUES (?, ?, ?, ?)",
            (code, uid, dev_hash, datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()

    new_expiry, final_limit = save_subscription(uid, f"Voucher VIP ({days}d)", int(days), int(limit), stack=True)
    bot.reply_to(
        message,
        f"🎉 **ভাউচার কোড সফলভাবে রিডিম হয়েছে!**\n"
        f"💎 যোগ হয়েছে: `+{days}` দিন | ফাইল লিমিট: `{final_limit}`\n"
        f"⏳ নতুন মেয়াদ: `{new_expiry.strftime('%Y-%m-%d %H:%M')}`\n"
        f"📊 কোড ব্যবহার: `{new_used}/{f_max}`",
        parse_mode="Markdown",
    )


def _logic_earn_vip(message):
    uid = message.from_user.id
    if uid not in admin_ids:
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute(
                "SELECT created_at FROM ad_sessions WHERE user_id = ? AND claimed = 1 ORDER BY created_at DESC LIMIT 1",
                (uid,),
            )
            last_claim = c.fetchone()
            c.execute("SELECT device_hash FROM user_devices WHERE user_id = ?", (uid,))
            u_dev = c.fetchone()
            bound_hash = u_dev[0] if (u_dev and u_dev[0]) else ""
            if bound_hash:
                c.execute("SELECT owner_user_id, is_blocked FROM device_locks WHERE device_hash = ?", (bound_hash,))
                dlock = c.fetchone()
                if dlock and int(dlock[0]) != uid:
                    conn.close()
                    log_device_abuse(uid, int(dlock[0]), bound_hash, "earn_vip", "User bound to already-locked device")
                    bot.reply_to(
                        message,
                        f"❌ **SAME-DEVICE MULTI-ACCOUNT BLOCKED!**\nএই ডিভাইসটি অন্য Telegram অ্যাকাউন্ট (`{dlock[0]}`) এর সাথে লক করা আছে।",
                        parse_mode="Markdown",
                    )
                    return
            conn.close()

        if last_claim and last_claim[0]:
            try:
                last_dt = datetime.fromisoformat(last_claim[0])
                next_allowed = last_dt + timedelta(hours=AD_CLAIM_COOLDOWN_HOURS)
                if datetime.now() < next_allowed:
                    rem = next_allowed - datetime.now()
                    hrs = rem.seconds // 3600
                    mins = (rem.seconds % 3600) // 60
                    bot.reply_to(
                        message,
                        f"⏳ আপনি ইতিমধ্যে ফ্রি অ্যাড VIP ক্লেইম করেছেন। পরবর্তী ক্লেইম করতে পারবেন `{hrs} ঘণ্টা {mins} মিনিট` পর।",
                        parse_mode="Markdown",
                    )
                    return
            except Exception:
                pass

    session_token = create_ad_session(uid)
    ad_url = f"{get_base_url()}/ad/{session_token}"

    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("👀 Watch Ad (Required)", url=ad_url))
    markup.add(types.InlineKeyboardButton("✅ Verify Ad & Claim VIP", callback_data=f"verify_claim_{session_token}"))

    bot.reply_to(
        message,
        "🎁 **EARN FREE VIP (AD VERIFICATION REQUIRED)**\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "১. **Watch Ad** বাটনে ক্লিক করে অ্যাডটি সম্পন্ন করুন।\n"
        "২. অ্যাড ভেরিফাই হওয়ার পর **Verify Ad & Claim VIP** চাপুন।\n"
        "🔒 *একই ডিভাইসে একাধিক Telegram অ্যাকাউন্ট দিয়ে ক্লেইম ব্লক করা হয়েছে।*",
        reply_markup=markup,
        parse_mode="Markdown",
    )


@bot.callback_query_handler(func=lambda call: call.data.startswith("verify_claim_"))
def handle_verify_claim(call):
    uid = call.from_user.id
    blocked, reason = is_blocked_for_user(uid)
    if blocked:
        bot.answer_callback_query(call.id, reason, show_alert=True)
        return
    token = call.data.replace("verify_claim_", "", 1)

    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT user_id, device_hash, verified, claimed FROM ad_sessions WHERE token = ?", (token,))
        row = c.fetchone()
        if not row:
            conn.close()
            bot.answer_callback_query(call.id, "❌ সেশন পাওয়া যায়নি। আবার Earn VIP চাপুন।", show_alert=True)
            return

        sess_uid, sess_dev_hash, verified, claimed = row
        if int(sess_uid) != int(uid):
            conn.close()
            bot.answer_callback_query(call.id, "❌ এটি আপনার সেশন নয়!", show_alert=True)
            return
        if int(claimed or 0) == 1:
            conn.close()
            bot.answer_callback_query(call.id, "⚠️ এই অ্যাড সেশন দিয়ে ইতিমধ্যে ক্লেইম করা হয়েছে!", show_alert=True)
            return
        if int(verified or 0) != 1:
            conn.close()
            bot.answer_callback_query(call.id, "❌ আপনি এখনো অ্যাড দেখেননি/ভেরিফাই হয়নি! আগে Watch Ad চাপুন।", show_alert=True)
            return

        conn.close()

    allowed, _, reason_bn = bind_or_check_device(uid, sess_dev_hash, source="ad_claim", enforce_strict=True)
    if not allowed:
        bot.answer_callback_query(call.id, "❌ Same-device multi-account detected!", show_alert=True)
        bot.send_message(call.message.chat.id, reason_bn, parse_mode="Markdown")
        return

    with DB_LOCK:
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("UPDATE ad_sessions SET claimed = 1 WHERE token = ? AND claimed = 0", (token,))
        updated = c.rowcount
        conn.commit()
        conn.close()

    if updated <= 0:
        bot.answer_callback_query(call.id, "⚠️ ইতিমধ্যে ক্লেইম করা হয়েছে!", show_alert=True)
        return

    new_expiry, final_limit = save_subscription(uid, "Verified Ad VIP", 7, SUBSCRIBED_USER_LIMIT, stack=True)
    bot.answer_callback_query(call.id, "✅ অ্যাড ভেরিফাইড! ৭ দিনের VIP যুক্ত হয়েছে।", show_alert=True)
    bot.send_message(
        call.message.chat.id,
        "🎉 **AD VERIFIED & VIP ACTIVATED!**\n"
        f"💎 Plan: `7 Days VIP` (Limit: `{final_limit} Files`)\n"
        f"⏳ New Expiry: `{new_expiry.strftime('%Y-%m-%d %H:%M')}`",
        parse_mode="Markdown",
    )


def _logic_view_plans(m_or_c):
    chat_id = m_or_c.chat.id if isinstance(m_or_c, telebot.types.Message) else m_or_c.message.chat.id
    plans = get_all_plans()
    if not plans:
        bot.send_message(chat_id, "ℹ️ বর্তমানে কোনো প্ল্যান যুক্ত করা নেই।")
        return
    for p in plans:
        pid, name, limit, price, duration, _ = p
        usdt, fprice = parse_price_to_usdt(price)
        markup = types.InlineKeyboardMarkup()
        if usdt == 0 or str(price).lower() in ["free", "0"]:
            markup.add(types.InlineKeyboardButton(f"🎁 Claim {name} (Free)", callback_data=f"claim_free_{pid}"))
        else:
            markup.add(types.InlineKeyboardButton(f"🛒 Purchase {name} ({usdt} USDT)", callback_data=f"buy_binance_{pid}"))
        bot.send_message(
            chat_id,
            f"📦 **PLAN:** `{name}`\n📁 Limit: `{limit}`\n⏱️ Duration: `{duration} Days`\n💰 Price: `{fprice}`",
            reply_markup=markup,
            parse_mode="Markdown",
        )


def _logic_upload_prompt(message):
    uid = message.from_user.id
    if not (uid in admin_ids or uid == OWNER_ID or (uid in user_subscriptions and user_subscriptions[uid]["expiry"] > datetime.now())):
        bot.reply_to(message, "❌ ফাইল হোস্ট করতে প্রথমে VIP প্ল্যান একটিভ করুন (**🎁 Earn VIP** বা **💳 View Plans** থেকে)।", parse_mode="Markdown")
        return
    bot.reply_to(
        message,
        "🚀 **বট আপলোড করুন**\nআপনার `.py`, `.js` অথবা `.zip` ফাইলটি এখন সেন্ড করুন।\n*(Zip ফাইল দিলে ভেতরের `.py` / `.js` ফাইল অটোমেটিক লিস্টে যুক্ত হবে)*",
        parse_mode="Markdown",
    )


@bot.message_handler(content_types=["document"])
def handle_docs(message):
    uid = message.from_user.id
    blocked, reason = is_blocked_for_user(uid)
    if blocked:
        bot.reply_to(message, reason)
        return
    if not (uid in admin_ids or uid == OWNER_ID or (uid in user_subscriptions and user_subscriptions[uid]["expiry"] > datetime.now())):
        bot.reply_to(message, "❌ ফাইল আপলোড করতে প্রথমে VIP প্ল্যান একটিভ করুন।")
        return

    limit = get_user_file_limit(uid)
    if get_user_file_count(uid) >= limit:
        bot.reply_to(message, f"⚠️ আপনার ফাইল লিমিট (`{limit}`) পূর্ণ হয়ে গেছে।", parse_mode="Markdown")
        return

    doc = message.document
    fname = os.path.basename(doc.file_name)
    fext = os.path.splitext(fname)[1].lower()
    if fext not in [".py", ".js", ".zip"]:
        bot.reply_to(message, "⚠️ শুধুমাত্র `.py`, `.js` এবং `.zip` ফাইল সাপোর্টেড!")
        return

    try:
        wait_msg = bot.reply_to(message, f"⏳ `{fname}` ডাউনলোড হচ্ছে...", parse_mode="Markdown")
        finfo = bot.get_file(doc.file_id)
        content = bot.download_file(finfo.file_path)
        folder = get_user_folder(uid)
        fpath = os.path.join(folder, fname)
        with open(fpath, "wb") as f:
            f.write(content)

        if fext == ".zip":
            safe_extract_zip(fpath, folder)
            os.remove(fpath)
            extracted_scripts = []
            for root, _, files in os.walk(folder):
                for file in files:
                    if file.endswith((".py", ".js")):
                        rel_path = os.path.relpath(os.path.join(root, file), folder)
                        ftype = "py" if file.endswith(".py") else "js"
                        save_user_file(uid, rel_path, ftype)
                        extracted_scripts.append(rel_path)

            if not extracted_scripts:
                bot.edit_message_text("⚠️ Zip এক্সট্রাক্ট হয়েছে, কিন্তু ভেতরে কোনো `.py` বা `.js` ফাইল পাওয়া যায়নি।", message.chat.id, wait_msg.message_id, parse_mode="Markdown")
                return

            target_script = extracted_scripts[0]
            target_full_path = os.path.join(folder, target_script)
            pending_bot_tokens[uid] = {
                "folder": folder,
                "fname": target_script,
                "fpath": target_full_path,
                "ftype": "py" if target_script.endswith(".py") else "js",
            }
            bot.edit_message_text(f"✅ Zip এক্সট্রাক্ট হয়েছে! পাওয়া ফাইল: `{target_script}`", message.chat.id, wait_msg.message_id, parse_mode="Markdown")
            msg = bot.send_message(
                message.chat.id,
                "🔑 যে বটটি হোস্ট করবেন, সেটির **Bot API Token** পাঠান (অথবা কোডে টোকেন বসানো থাকলে `skip` লিখুন):",
                parse_mode="Markdown",
            )
            bot.register_next_step_handler(msg, process_bot_token_and_start)
            return

        bot.edit_message_text(f"✅ `{fname}` আপলোড হয়েছে!", message.chat.id, wait_msg.message_id, parse_mode="Markdown")
        pending_bot_tokens[uid] = {
            "folder": folder,
            "fname": fname,
            "fpath": fpath,
            "ftype": fext[1:],
        }
        msg = bot.send_message(
            message.chat.id,
            "🔑 যে বটটি হোস্ট করবেন, সেটির **Bot API Token** পাঠান (অথবা কোডে টোকেন বসানো থাকলে `skip` লিখুন):",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, process_bot_token_and_start)
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {e}")


def process_bot_token_and_start(message):
    uid = message.from_user.id
    info = pending_bot_tokens.pop(uid, None)
    if not info:
        return
    raw_token = message.text.strip()
    bot_api_token = "" if raw_token.lower() == "skip" else raw_token

    if bot_api_token:
        with open(os.path.join(info["folder"], "bot_token.txt"), "w", encoding="utf-8") as tf:
            tf.write(bot_api_token)
        try:
            with open(info["fpath"], "r", encoding="utf-8", errors="ignore") as sf:
                code_txt = sf.read()
            if "YOUR_BOT_TOKEN_HERE" in code_txt:
                code_txt = code_txt.replace("YOUR_BOT_TOKEN_HERE", bot_api_token)
                with open(info["fpath"], "w", encoding="utf-8") as sf:
                    sf.write(code_txt)
        except Exception:
            pass

    save_user_file(uid, info["fname"], info["ftype"], bot_api_token)
    if info["ftype"] == "js":
        run_js_script(info["fpath"], uid, info["folder"], info["fname"], message, bot_api_token)
    else:
        run_script(info["fpath"], uid, info["folder"], info["fname"], message, bot_api_token)


def _logic_check_files(message):
    uid = message.from_user.id
    flist = user_files.get(uid, [])
    if not flist:
        bot.reply_to(message, "📂 আপনার কোনো ফাইল হোস্ট করা নেই।")
        return
    m = types.InlineKeyboardMarkup(row_width=1)
    for fname, ftype in flist:
        status = "🟢 Running" if is_bot_running(uid, fname) else "🔴 Stopped"
        m.add(types.InlineKeyboardButton(f"📄 {fname} [{ftype.upper()}] — {status}", callback_data=f"file_{uid}_{fname}"))
    bot.reply_to(message, "📁 **Your Hosted Bots:**", reply_markup=m, parse_mode="Markdown")


@bot.callback_query_handler(func=lambda call: True)
def callbacks(call):
    uid = call.from_user.id
    data = call.data

    if data == "check_join":
        check_join_cb(call)
        return

    if data.startswith("verify_claim_"):
        handle_verify_claim(call)
        return

    blocked, reason = is_blocked_for_user(uid)
    if blocked:
        bot.answer_callback_query(call.id, reason, show_alert=True)
        return

    if data.startswith("reply_tkt_") and uid in admin_ids:
        bot.answer_callback_query(call.id)
        parts = data.split("_")
        tkt_id = int(parts[2])
        target_uid = int(parts[3])
        pending_admin_replies[uid] = {"ticket_id": tkt_id, "target_uid": target_uid}
        msg = bot.send_message(
            call.message.chat.id,
            f"💬 Ticket #{tkt_id} (User `{target_uid}`) এর জন্য রিপ্লাই মেসেজটি লিখুন:",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, process_admin_ticket_reply)
        return

    if data == "view_plans_cb":
        _logic_view_plans(call)
    elif data.startswith("claim_free_"):
        pid = int(data.split("_")[2])
        plan = get_plan_by_id(pid)
        if plan:
            new_exp, _ = save_subscription(uid, plan[1], int(plan[4]), int(plan[2]), stack=True)
            bot.answer_callback_query(call.id, "🎉 Claimed!")
            bot.send_message(
                call.message.chat.id,
                f"🎉 `{plan[1]}` সচল হয়েছে।\n⏳ নতুন মেয়াদ: `{new_exp.strftime('%Y-%m-%d %H:%M')}`",
                parse_mode="Markdown",
            )

    elif data.startswith("buy_binance_"):
        pid = int(data.split("_")[2])
        plan = get_plan_by_id(pid)
        if plan:
            usdt, _ = parse_price_to_usdt(plan[3])
            m = types.InlineKeyboardMarkup()
            m.add(types.InlineKeyboardButton("🔍 Submit TxID", callback_data=f"sub_tx_{pid}"))
            bot.send_message(call.message.chat.id, f"💛 Binance Pay ID: `{BINANCE_PAY_ID}`\nAmount: `{usdt} USDT`", reply_markup=m, parse_mode="Markdown")

    elif data.startswith("sub_tx_"):
        pid = int(data.split("_")[2])
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "📩 TxID পাঠান:")
        bot.register_next_step_handler(msg, lambda m: process_txid(m, pid))

    elif data.startswith("file_"):
        _, owner_id, fname = data.split("_", 2)
        running = is_bot_running(int(owner_id), fname)
        m = types.InlineKeyboardMarkup(row_width=2)
        m.add(
            types.InlineKeyboardButton("🛑 Stop" if running else "▶️ Start", callback_data=f"{'stop' if running else 'start'}_{owner_id}_{fname}"),
            types.InlineKeyboardButton("🔄 Restart", callback_data=f"restart_{owner_id}_{fname}"),
        )
        m.add(
            types.InlineKeyboardButton("📜 Logs", callback_data=f"log_{owner_id}_{fname}"),
            types.InlineKeyboardButton("🗑️ Delete", callback_data=f"del_{owner_id}_{fname}"),
        )
        bot.send_message(call.message.chat.id, f"📄 File: `{fname}`\nStatus: `{'Running' if running else 'Stopped'}`", reply_markup=m, parse_mode="Markdown")

    elif data.startswith(("stop_", "start_", "restart_", "log_", "del_")):
        action, owner_id, fname = data.split("_", 2)
        oid = int(owner_id)
        skey = f"{oid}_{fname}"
        folder = get_user_folder(oid)
        fpath = os.path.join(folder, fname)

        if action == "stop":
            if skey in bot_scripts:
                bot_scripts[skey]["auto_restart"] = False
                kill_process_tree(bot_scripts[skey])
                del bot_scripts[skey]
            bot.answer_callback_query(call.id, "Stopped!")
            bot.send_message(call.message.chat.id, f"🛑 `{fname}` বন্ধ করা হয়েছে।", parse_mode="Markdown")
        elif action in ("start", "restart"):
            if os.path.exists(fpath):
                token_file = os.path.join(folder, "bot_token.txt")
                saved_token = ""
                if os.path.exists(token_file):
                    with open(token_file, "r", encoding="utf-8", errors="ignore") as tf:
                        saved_token = tf.read().strip()
                if fname.endswith(".js"):
                    run_js_script(fpath, oid, folder, fname, call.message, saved_token)
                else:
                    run_script(fpath, oid, folder, fname, call.message, saved_token)
                bot.answer_callback_query(call.id, "Done!")
            else:
                bot.answer_callback_query(call.id, "File not found!", show_alert=True)
        elif action == "log":
            log_path = os.path.join(folder, f"{os.path.splitext(fname)[0]}.log")
            if os.path.exists(log_path):
                with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                    logs = f.read()[-1500:]
                bot.send_message(call.message.chat.id, f"📜 **Logs (`{fname}`):**\n```\n{logs or 'Empty'}\n```", parse_mode="Markdown")
            else:
                bot.answer_callback_query(call.id, "No logs yet!", show_alert=True)
        elif action == "del":
            if skey in bot_scripts:
                bot_scripts[skey]["auto_restart"] = False
                kill_process_tree(bot_scripts[skey])
                del bot_scripts[skey]
            remove_user_file_db(oid, fname)
            if os.path.exists(fpath):
                os.remove(fpath)
            bot.answer_callback_query(call.id, "Deleted!")
            bot.send_message(call.message.chat.id, f"🗑️ `{fname}` ডিলিট করা হয়েছে।", parse_mode="Markdown")

    # --- Admin actions ---
    elif data == "add_plan_init" and uid in admin_ids:
        bot.answer_callback_query(call.id)
        msg = bot.send_message(
            call.message.chat.id,
            "📝 **Format:** `Name | Limit | Price | Duration | Link`\nউদাহরণ: `VIP | 15 | 5 USDT | 30 | https://t.me/hostifyofficialsbot`",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, handle_add_plan_step)

    elif data == "manage_plans" and uid in admin_ids:
        plans = get_all_plans()
        if not plans:
            bot.answer_callback_query(call.id, "কোনো প্ল্যান নেই!", show_alert=True)
            return
        m = types.InlineKeyboardMarkup(row_width=1)
        for p in plans:
            pid, name, limit, price, duration, _ = p
            m.add(types.InlineKeyboardButton(f"🗑️ Delete: {name} ({price} / {duration}d)", callback_data=f"del_plan_{pid}"))
        bot.answer_callback_query(call.id)
        bot.edit_message_text("🗑️ যে প্ল্যানটি ডিলিট করতে চান সেটিতে ক্লিক করুন:", call.message.chat.id, call.message.message_id, reply_markup=m)

    elif data.startswith("del_plan_") and uid in admin_ids:
        try:
            pid = int(data.replace("del_plan_", "", 1))
        except ValueError:
            bot.answer_callback_query(call.id, "Invalid Plan ID!", show_alert=True)
            return
        if delete_plan_db(pid):
            bot.answer_callback_query(call.id, "✅ প্ল্যান ডিলিট হয়েছে!", show_alert=True)
            plans = get_all_plans()
            if plans:
                m = types.InlineKeyboardMarkup(row_width=1)
                for p in plans:
                    m.add(types.InlineKeyboardButton(f"🗑️ Delete: {p[1]} ({p[3]} / {p[4]}d)", callback_data=f"del_plan_{p[0]}"))
                bot.edit_message_text("🗑️ যে প্ল্যানটি ডিলিট করতে চান সেটিতে ক্লিক করুন:", call.message.chat.id, call.message.message_id, reply_markup=m)
            else:
                bot.edit_message_text("✅ সব প্ল্যান ডিলিট করা হয়েছে। আর কোনো প্ল্যান বাকি নেই।", call.message.chat.id, call.message.message_id)
        else:
            bot.answer_callback_query(call.id, "❌ প্ল্যানটি পাওয়া যায়নি!", show_alert=True)

    elif data == "add_subscription" and uid in admin_ids:
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "💎 **Format:** `UserID PlanName Days [FileLimit]`\nউদাহরণ: `8452462370 VIP 30 15`", parse_mode="Markdown")
        bot.register_next_step_handler(msg, handle_add_sub_step)

    elif data == "remove_subscription" and uid in admin_ids:
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "❌ সাবস্ক্রিপশন রিমুভ করতে User ID পাঠান:")
        bot.register_next_step_handler(msg, handle_remove_sub_step)

    elif data == "reset_device" and uid in admin_ids:
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "🔓 যে ইউজারের Device Lock রিসেট করতে চান তার **Telegram User ID** পাঠান:", parse_mode="Markdown")
        bot.register_next_step_handler(msg, handle_reset_device_step)

    elif data == "view_abuse_logs" and uid in admin_ids:
        bot.answer_callback_query(call.id)
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute(
            "SELECT user_id, existing_owner_id, action_type, detail, created_at FROM device_abuse_logs ORDER BY id DESC LIMIT 12"
        )
        rows = c.fetchall()
        conn.close()
        if not rows:
            bot.send_message(call.message.chat.id, "✅ কোনো Multi-Account Abuse লগ পাওয়া যায়নি।")
            return
        lines = ["🚨 **RECENT DEVICE ABUSE LOGS**", "━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
        for u_id, own_id, act, det, dt in rows:
            lines.append(f"• Attempt: `{u_id}` | Owner: `{own_id}` | `{act}`\n  _{det}_ (`{dt[:16]}`)")
        bot.send_message(call.message.chat.id, "\n".join(lines), parse_mode="Markdown")

    elif data == "create_voucher" and uid in admin_ids:
        bot.answer_callback_query(call.id)
        msg = bot.send_message(
            call.message.chat.id,
            "🎟️ **Format:** `CODE DAYS FILE_LIMIT MAX_USES [VALID_HOURS]`\n"
            "• `CODE`: ভাউচার কোড\n"
            "• `DAYS`: কত দিনের VIP\n"
            "• `FILE_LIMIT`: কত ফাইল লিমিট\n"
            "• `MAX_USES`: মোট কতজন/কতবার ইউজ করতে পারবে\n"
            "• `VALID_HOURS` *(ঐচ্ছিক)*: কোডটি কত ঘণ্টা ভ্যালিড থাকবে (`0` দিলে আনলিমিটেড)\n\n"
            "উদাহরণ: `HOSTIFY7 7 15 1 24`",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, handle_create_voucher_step)

    elif data == "gen_bulk_vouchers" and uid in admin_ids:
        bot.answer_callback_query(call.id)
        msg = bot.send_message(
            call.message.chat.id,
            "🎲 **Bulk Random Voucher Generator**\n"
            "**Format:** `COUNT DAYS FILE_LIMIT MAX_USES [VALID_HOURS]`\n"
            "উদাহরণ: `5 7 15 1 48` (৫টি র‍্যান্ডম কোড তৈরি হবে)",
            parse_mode="Markdown",
        )
        bot.register_next_step_handler(msg, handle_bulk_vouchers_step)

    elif data == "view_vouchers" and uid in admin_ids:
        bot.answer_callback_query(call.id)
        text, markup = build_vouchers_view_payload()
        bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

    elif data == "clean_vouchers" and uid in admin_ids:
        removed = clean_inactive_vouchers_db()
        bot.answer_callback_query(call.id, f"🧹 {removed}টি কোড মুছে ফেলা হয়েছে!", show_alert=True)
        text, markup = build_vouchers_view_payload()
        try:
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
        except Exception:
            bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="Markdown")

    elif data.startswith("del_vouch_") and uid in admin_ids:
        v_code = data.replace("del_vouch_", "", 1).strip().upper()
        if delete_voucher_db(v_code):
            bot.answer_callback_query(call.id, f"✅ ভাউচার {v_code} ডিলিট হয়েছে!", show_alert=True)
            text, markup = build_vouchers_view_payload()
            try:
                bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup, parse_mode="Markdown")
            except Exception:
                bot.send_message(call.message.chat.id, text, reply_markup=markup, parse_mode="Markdown")
        else:
            bot.answer_callback_query(call.id, "❌ ভাউচারটি পাওয়া যায়নি!", show_alert=True)

    elif data == "add_admin" and uid in admin_ids:
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "➕ নতুন Admin-এর Telegram User ID পাঠান:")
        bot.register_next_step_handler(msg, handle_add_admin_step)

    elif data == "toggle_maint" and uid in admin_ids:
        global maintenance_mode
        maintenance_mode = not maintenance_mode
        state_text = "ON (Users Blocked)" if maintenance_mode else "OFF (Users Allowed)"
        bot.answer_callback_query(call.id, f"Maintenance: {state_text}", show_alert=True)
        bot.edit_message_text(
            f"🛡️ **ADMIN CONTROL CONSOLE**\n🛠️ Maintenance Mode: `{state_text}`",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=admin_panel_markup(),
            parse_mode="Markdown",
        )

    elif data == "ban_user" and uid in admin_ids:
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "🚫 ব্যান করার জন্য User ID পাঠান:")
        bot.register_next_step_handler(msg, handle_ban_step)

    elif data == "unban_user" and uid in admin_ids:
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "✅ আনব্যান করার জন্য User ID পাঠান:")
        bot.register_next_step_handler(msg, handle_unban_step)

    elif data == "broadcast" and uid in admin_ids:
        bot.answer_callback_query(call.id)
        msg = bot.send_message(call.message.chat.id, "📢 সমস্ত ইউজারের কাছে পাঠানোর জন্য ব্রডকাস্ট মেসেজটি সেন্ড করুন:")
        bot.register_next_step_handler(msg, process_broadcast)

    elif data == "run_all_scripts" and uid in admin_ids:
        count = run_all_user_scripts()
        bot.answer_callback_query(call.id, f"Started {count} bots!", show_alert=True)
        bot.send_message(call.message.chat.id, f"⚙️ মোট `{count}`টি বন্ধ থাকা বট চালু করা হয়েছে।", parse_mode="Markdown")

    elif data == "stop_all_scripts" and uid in admin_ids:
        for skey in list(bot_scripts.keys()):
            bot_scripts[skey]["auto_restart"] = False
            kill_process_tree(bot_scripts[skey])
            del bot_scripts[skey]
        bot.answer_callback_query(call.id, "All bots terminated!", show_alert=True)
        bot.send_message(call.message.chat.id, "🛑 সব রানিং বট বন্ধ করা হয়েছে।")

    elif data == "stats" and uid in admin_ids:
        bot.answer_callback_query(call.id)
        conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM device_locks")
        locked_devs = int((c.fetchone() or [0])[0] or 0)
        c.execute("SELECT COUNT(*) FROM device_abuse_logs")
        abuse_cnt = int((c.fetchone() or [0])[0] or 0)
        conn.close()
        bot.send_message(
            call.message.chat.id,
            f"📊 **ENTERPRISE SYSTEM METRICS**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Total Unique Users: `{len(active_users)}`\n"
            f"💎 Active Subscriptions: `{len(user_subscriptions)}`\n"
            f"🚀 Running Bot Instances: `{len(bot_scripts)}`\n"
            f"🔐 Locked Devices: `{locked_devs}`\n"
            f"🚨 Multi-Account Blocks: `{abuse_cnt}`\n"
            f"🚫 Blacklisted Users: `{len(banned_users)}`\n"
            f"🛠️ Maintenance Mode: `{'ON' if maintenance_mode else 'OFF'}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            parse_mode="Markdown",
        )


def handle_add_plan_step(m):
    try:
        parts = [x.strip() for x in m.text.split("|")]
        if len(parts) < 4:
            bot.reply_to(m, "❌ সঠিক ফরম্যাট দিন: `Name | Limit | Price | Duration | Link`", parse_mode="Markdown")
            return
        name, limit, price, duration = parts[0], int(parts[1]), parts[2], int(parts[3])
        link = parts[4] if len(parts) > 4 else ""
        add_plan_db(name, limit, price, duration, link)
        bot.reply_to(m, f"✅ প্ল্যান `{name}` সফলভাবে যুক্ত হয়েছে!", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(m, f"❌ Error: {e}")


def handle_add_sub_step(m):
    try:
        parts = m.text.split()
        target_uid, plan_name, days = int(parts[0]), parts[1], int(parts[2])
        flim = int(parts[3]) if len(parts) > 3 else SUBSCRIBED_USER_LIMIT
        new_exp, final_lim = save_subscription(target_uid, plan_name, days, flim, stack=True)
        bot.reply_to(
            m,
            f"✅ ইউজার `{target_uid}` এর জন্য `{days}` দিনের `{plan_name}` (Limit: `{final_lim}`) যুক্ত হয়েছে।\n⏳ মেয়াদ: `{new_exp.strftime('%Y-%m-%d %H:%M')}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        bot.reply_to(m, f"❌ Error: {e}")


def handle_remove_sub_step(m):
    try:
        target_uid = int(m.text.strip())
        remove_subscription_db(target_uid)
        bot.reply_to(m, f"✅ ইউজার `{target_uid}` এর সাবস্ক্রিপশন রিমুভ করা হয়েছে।", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(m, f"❌ Error: {e}")


def handle_reset_device_step(m):
    try:
        target_uid = int(m.text.strip())
        if reset_user_device_db(target_uid):
            bot.reply_to(m, f"🔓 ইউজার `{target_uid}` এর Device Lock সফলভাবে রিসেট হয়েছে।", parse_mode="Markdown")
        else:
            bot.reply_to(m, f"ℹ️ ইউজার `{target_uid}` এর নামে কোনো সেভ করা Device Lock পাওয়া যায়নি।", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(m, f"❌ Error: {e}")


def handle_add_admin_step(m):
    try:
        new_uid = int(m.text.strip())
        add_admin_db(new_uid)
        bot.reply_to(m, f"✅ `{new_uid}` কে নতুন Admin যুক্ত করা হয়েছে।", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(m, f"❌ Error: {e}")


def handle_create_voucher_step(m):
    try:
        parts = m.text.split()
        if len(parts) < 2:
            bot.reply_to(m, "❌ ফরম্যাট: `CODE DAYS FILE_LIMIT MAX_USES [VALID_HOURS]`", parse_mode="Markdown")
            return
        code = parts[0].strip().upper()
        days = int(parts[1])
        limit = int(parts[2]) if len(parts) > 2 else 15
        max_uses = int(parts[3]) if len(parts) > 3 else 1
        valid_hours = int(parts[4]) if len(parts) > 4 else 0
        create_voucher_db(code, days, limit, max_uses, valid_hours)
        exp_info = f"{valid_hours} ঘণ্টা" if valid_hours > 0 else "Unlimited"
        bot.reply_to(
            m,
            f"🎟️ ভাউচার কোড `{code}` তৈরি হয়েছে!\n"
            f"⏱️ VIP মেয়াদ: `{days}` দিন | 📁 Limit: `{limit}` | 👥 Max Uses: `{max_uses}` | ⏳ Code Valid: `{exp_info}`",
            parse_mode="Markdown",
        )
    except Exception as e:
        bot.reply_to(m, f"❌ Error: {e}")


def handle_bulk_vouchers_step(m):
    try:
        parts = m.text.split()
        if len(parts) < 2:
            bot.reply_to(m, "❌ ফরম্যাট: `COUNT DAYS FILE_LIMIT MAX_USES [VALID_HOURS]` (উদাহরণ: `5 7 15 1 24`)", parse_mode="Markdown")
            return
        count = min(30, max(1, int(parts[0])))
        days = int(parts[1])
        limit = int(parts[2]) if len(parts) > 2 else 15
        max_uses = int(parts[3]) if len(parts) > 3 else 1
        valid_hours = int(parts[4]) if len(parts) > 4 else 0
        generated = []
        for _ in range(count):
            rnd = f"HOST-{secrets.token_hex(3).upper()}"
            create_voucher_db(rnd, days, limit, max_uses, valid_hours)
            generated.append(f"`{rnd}`")
        exp_info = f"{valid_hours}h" if valid_hours > 0 else "No Expiry"
        bot.reply_to(
            m,
            f"🎲 **{count}টি র‍্যান্ডম ভাউচার তৈরি হয়েছে!**\n"
            f"⏱️ VIP: `{days}d` | 📁 Limit: `{limit}` | 👥 Uses: `{max_uses}` | ⏳ Valid: `{exp_info}`\n\n"
            + "\n".join(generated),
            parse_mode="Markdown",
        )
    except Exception as e:
        bot.reply_to(m, f"❌ Error: {e}")


def handle_ban_step(m):
    try:
        target_uid = int(m.text.strip())
        banned_users.add(target_uid)
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO banned_users (user_id) VALUES (?)", (target_uid,))
            conn.commit()
            conn.close()
        bot.reply_to(m, f"✅ ইউজার `{target_uid}` ব্যান করা হয়েছে।", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(m, f"❌ Error: {e}")


def handle_unban_step(m):
    try:
        target_uid = int(m.text.strip())
        banned_users.discard(target_uid)
        with DB_LOCK:
            conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
            c = conn.cursor()
            c.execute("DELETE FROM banned_users WHERE user_id = ?", (target_uid,))
            conn.commit()
            conn.close()
        bot.reply_to(m, f"✅ ইউজার `{target_uid}` আনব্যান করা হয়েছে।", parse_mode="Markdown")
    except Exception as e:
        bot.reply_to(m, f"❌ Error: {e}")


def process_txid(message, pid):
    tx = message.text.strip()
    uid = message.from_user.id
    plan = get_plan_by_id(pid)
    if not plan:
        bot.reply_to(message, "❌ প্ল্যানটি পাওয়া যায়নি।")
        return
    if is_txid_used(tx):
        bot.reply_to(message, "❌ এই TxID টি ইতিপূর্বেই ব্যবহার করা হয়েছে!")
        return
    valid, _, _ = check_binance_payment(tx)
    nums = re.findall(r"\d+", str(plan[3]))
    is_free = (float(nums[0]) == 0) if nums else False
    if valid or is_free:
        add_used_txid(tx)
        new_exp, _ = save_subscription(uid, plan[1], int(plan[4]), int(plan[2]), stack=True)
        bot.reply_to(
            message,
            f"🎉 Payment Verified! `{plan[1]}` সচল হয়েছে।\n⏳ নতুন মেয়াদ: `{new_exp.strftime('%Y-%m-%d %H:%M')}`",
            parse_mode="Markdown",
        )
    else:
        bot.reply_to(message, "❌ Binance Pay হিস্টোরিতে এই TxID পাওয়া যায়নি।")


def process_broadcast(message):
    success, fail = 0, 0
    status_msg = bot.reply_to(message, "📢 ব্রডকাস্ট পাঠানো হচ্ছে...")
    for target_uid in list(active_users):
        try:
            bot.copy_message(target_uid, message.chat.id, message.message_id)
            success += 1
            time.sleep(0.03)
        except Exception:
            fail += 1
    bot.edit_message_text(f"✅ Broadcast Done!\nSuccess: `{success}` | Failed: `{fail}`", message.chat.id, status_msg.message_id, parse_mode="Markdown")


BUTTON_MAPPING = {
    "📢 𝗨𝗽𝗱𝗮𝘁𝗲𝘀 𝗖𝗵𝗮𝗻𝗻𝗲𝗹": lambda m: bot.reply_to(m, f"📢 Channel: {get_channel_url()}"),
    "⚡ 𝗦𝗽𝗲𝗲𝗱 & 𝗣𝗶𝗻𝗴": lambda m: bot.reply_to(m, f"⚡ CPU: `{psutil.cpu_percent()}%` | RAM: `{psutil.virtual_memory().percent}%`", parse_mode="Markdown"),
    "🚀 𝗨𝗽𝗹𝗼𝗮𝗱 𝗕𝗼𝘁": _logic_upload_prompt,
    "📁 𝗠𝗮𝗻𝗮𝗴𝗲 𝗕𝗼𝘁𝘀": _logic_check_files,
    "🩺 𝗕𝗼𝘁 𝗛𝗲𝗮𝗹𝘁𝗵": _logic_bot_health,
    "💾 𝗕𝗮𝗰𝗸𝘂𝗽": _logic_bot_backup,
    "🔥 𝗗𝗮𝗶𝗹𝘆 𝗦𝘁𝗿𝗲𝗮𝗸": _logic_daily_streak,
    "🌐 𝗣𝗿𝗼𝘅𝘆 𝗩𝗮𝘂𝗹𝘁": _logic_proxy_vault,
    "🧪 𝗣𝗿𝗼𝘅𝘆 𝗧𝗲𝘀𝘁": _logic_proxy_test,
    "🔄 𝗥𝗼𝘁𝗮𝘁𝗲 𝗣𝗿𝗼𝘅𝘆": _logic_rotate_proxy,
    "✨ 𝗦𝘁𝘆𝗹𝗶𝘀𝘁 𝗖𝗮𝗿𝗱": _logic_stylist_card,
    "🛡️ 𝗩𝗜𝗣 𝗦𝗵𝗶𝗲𝗹𝗱": _logic_vip_shield,
    "🤖 𝗔𝗜 𝗖𝗼𝗱𝗲 𝗔𝘂𝗱𝗶𝘁": _logic_ai_code_audit,
    "🧠 𝗔𝗜 𝗟𝗼𝗴 𝗗𝗼𝗰": _logic_ai_log_doc,
    "✨ 𝗔𝗜 𝗦𝘁𝘆𝗹𝗶𝘀𝘁": _logic_ai_stylist,
    "🔮 𝗔𝗜 𝗣𝗿𝗲𝗱𝗶𝗰𝘁": _logic_ai_predict,
    "💳 𝗩𝗶𝗲𝘄 𝗣𝗹𝗮𝗻𝘀": _logic_view_plans,
    "🎁 𝗘𝗮𝗿𝗻 𝗩𝗜𝗣": _logic_earn_vip,
    "🎟️ 𝗥𝗲𝗱𝗲𝗲𝗺 𝗖𝗼𝗱𝗲": _logic_redeem_prompt,
    "🔗 𝗥𝗲𝗳𝗲𝗿𝗿𝗮𝗹": _logic_referral,
    "👤 𝗠𝘆 𝗣𝗿𝗼𝗳𝗶𝗹𝗲": _logic_my_profile,
    "🆘 𝗧𝗶𝗰𝗸𝗲𝘁": _logic_ticket_prompt,
    "💬 𝗦𝘂𝗽𝗽𝗼𝗿𝘁": lambda m: bot.reply_to(m, f"💬 Support: {YOUR_USERNAME}"),
    "👑 𝗖𝗼𝗻𝘁𝗮𝗰𝘁 𝗢𝘄𝗻𝗲𝗿": lambda m: bot.reply_to(m, f"👑 Owner: {YOUR_USERNAME}"),
    "🛡️ 𝗔𝗱𝗺𝗶𝗻 𝗣𝗮𝗻𝗲𝗹": lambda m: (
        play_stylist_animation(m, "ADMIN CONSOLE"),
        bot.reply_to(
            m,
            f"🛡️ **ADMIN CONTROL CONSOLE**\n🛠️ Maintenance Mode: `{'ON' if maintenance_mode else 'OFF'}`",
            reply_markup=admin_panel_markup(),
            parse_mode="Markdown",
        ),
    )[1] if m.from_user.id in admin_ids else None,
    "📊 𝗦𝘆𝘀𝘁𝗲𝗺 𝗦𝘁𝗮𝘁𝘀": lambda m: bot.reply_to(
        m,
        f"📊 Active Users: `{len(active_users)}` | Running Bots: `{len(bot_scripts)}` | Maint: `{'ON' if maintenance_mode else 'OFF'}`",
        parse_mode="Markdown",
    ) if m.from_user.id in admin_ids else None,
}


@bot.message_handler(func=lambda m: not is_anti_flood(m.from_user.id) and m.text in BUTTON_MAPPING and BUTTON_MAPPING[m.text] is not None)
def main_btns(m):
    uid = m.from_user.id
    blocked, reason = is_blocked_for_user(uid)
    if blocked:
        bot.reply_to(m, reason)
        return
    if not check_force_sub(uid):
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📢 Join Official Channel", url=get_channel_url()))
        markup.add(types.InlineKeyboardButton("🔄 Verify Membership", callback_data="check_join"))
        bot.send_message(m.chat.id, "⚠️ আগে চ্যানেলে জয়েন করে Verify Membership চাপুন!", reply_markup=markup)
        return
    BUTTON_MAPPING[m.text](m)


if __name__ == "__main__":
    logger.info("🤖 HOSTIFY Bot Started...")
    keep_alive()
    Thread(target=auto_crash_recovery_loop, daemon=True).start()
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
