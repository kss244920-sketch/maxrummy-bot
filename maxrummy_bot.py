#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MaxRummy & YonoGames Automated Bot
Features:
- Single & Bulk Registration with SMS OTP + Auto-Claim Bind Bonus
- Bind Phone Number & Claim Bonus (Welcome Bind Gift)
- Single & Bulk Claim All Bonuses (Bind Gift, 7-Day Cycle, Daily Sign-in, Today Rewards)
- Single & Bulk Login and Balance Verification
- Token & Quota Billing Engine (Prince Admin daily caps, Owner unlimited)
- 0-Balance Protection with direct recharge support (@OwAtom / @OwKarma)
- Real-time ThreadPool bulk checker with detailed exportable txt reports
"""

import os
import sys
import time
import json
import io
import re
import html
import random
import threading
import http.server
import socketserver
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

import telebot
from telebot import types

# Add current directory to path
BASE_DIR = Path(__file__).parent.resolve()
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

from config import BOT_TOKEN, OWNER_ID, SUPPORT_USERNAMES, ACCOUNTS_FILE, CHECKED_ACCOUNTS_FILE
from admin_manager import admin_manager, IST
from maxrummy_client import (
    MaxRummyClient,
    clean_phone_number,
    bulk_check_accounts,
    bulk_claim_bonuses,
    PLATFORMS_LIST,
    PLATFORMS_MAP
)

# Initialize bot
bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# Thread-safe user state cache with TTL & lock
USER_STATES_LOCK = threading.RLock()
USER_STATES: Dict[int, dict] = {}
LAST_USER_ACTION: Dict[int, float] = {}
CHECK_CACHE: Dict[int, dict] = {}


def set_user_state(user_id: int, state: dict):
    with USER_STATES_LOCK:
        state["_created_at"] = time.time()
        USER_STATES[user_id] = state


def get_user_state(user_id: int) -> Optional[dict]:
    with USER_STATES_LOCK:
        state = USER_STATES.get(user_id)
        if state:
            # 15-minute TTL
            if time.time() - state.get("_created_at", 0) > 900:
                USER_STATES.pop(user_id, None)
                return None
        return state


def pop_user_state(user_id: int) -> Optional[dict]:
    with USER_STATES_LOCK:
        return USER_STATES.pop(user_id, None)


def set_check_cache(user_id: int, phone: str, results: list):
    with USER_STATES_LOCK:
        CHECK_CACHE[user_id] = {"phone": phone, "results": results, "ts": time.time()}


def get_check_cache(user_id: int, phone: str) -> Optional[list]:
    with USER_STATES_LOCK:
        cached = CHECK_CACHE.get(user_id)
        if cached and cached.get("phone") == phone and time.time() - cached.get("ts", 0) < 600:
            return cached.get("results")
    return None


def is_rate_limited(user_id: int, min_interval: float = 0.5) -> bool:
    """Anti-Flood protection against Telegram 429 flood bans."""
    if admin_manager.is_admin(user_id):
        return False
    if not admin_manager.is_rate_limit_enabled():
        return False
    now = time.time()
    last = LAST_USER_ACTION.get(user_id, 0.0)
    if now - last < min_interval:
        return True
    LAST_USER_ACTION[user_id] = now
    return False


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def send_master_log(text: str):
    """Dispatches real-time hits & audit alerts to Master Log Channel."""
    master_chan = admin_manager.master_log_channel
    if not master_chan:
        return
    try:
        bot.send_message(master_chan, text, parse_mode="HTML")
    except Exception as e:
        log(f"Master log send error: {e}")


def safe_edit_or_send(
    chat_id: int,
    text: str,
    reply_markup: Optional[types.InlineKeyboardMarkup] = None,
    message_id: Optional[int] = None,
    reply_to_message_id: Optional[int] = None
) -> Optional[int]:
    """Safely edits an existing message or falls back to sending a new message with HTML entity recovery."""
    if message_id:
        try:
            bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup, parse_mode="HTML")
            return message_id
        except Exception as e:
            err_str = str(e).lower()
            if "message is not modified" in err_str:
                return message_id
            if "can't parse entities" in err_str or "parse mode" in err_str:
                try:
                    bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup, parse_mode=None)
                    return message_id
                except Exception:
                    pass
            log(f"[safe_edit_or_send] edit_message_text failed ({e}), falling back to send_message")

    try:
        sent = bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode="HTML", reply_to_message_id=reply_to_message_id)
        return sent.message_id
    except Exception as e:
        err_str = str(e).lower()
        if "can't parse entities" in err_str or "parse mode" in err_str:
            sent = bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=None, reply_to_message_id=reply_to_message_id)
            return sent.message_id
        log(f"[safe_edit_or_send] send_message failed: {e}")
        return None


def parse_accounts_data(raw_text: str) -> List[Tuple[str, str]]:
    """
    Robust multi-format account parser (supports ':', '|', ',', and '\t').
    Compatible with standard lists, CSVs, and panel scrape exports.
    """
    accounts: List[Tuple[str, str]] = []
    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = None
        for delim in [":", "|", ",", "\t"]:
            if delim in line:
                tokens = [t.strip() for t in line.split(delim)]
                if len(tokens) >= 2:
                    p = clean_phone_number(tokens[0])
                    pw = tokens[1]
                    if len(p) == 10 and pw:
                        parts = (p, pw)
                        break
        if parts:
            accounts.append(parts)
    return accounts


def start_health_server(port: int = 8080):
    """
    Embedded HTTP Server for 24/7 Cloud Hosting (Render / Koyeb / JRNM).
    Prevents inactivity sleeps and satisfies container health checks.
    """
    class HealthHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path in ("/is_busy", "/busy"):
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"busy": False, "process": "Idle"}).encode("utf-8"))
                return

            if self.path.startswith("/backup_and_pin"):
                try:
                    from audit_cloud_manager import dispatch_and_pin_backup
                    res = dispatch_and_pin_backup(trigger_reason="3-Hour Scheduled Restart Checkpoint")
                    self.send_response(200)
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps(res).encode("utf-8"))
                    return
                except Exception as e:
                    self.send_response(500)
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"success": False, "error": str(e)}).encode("utf-8"))
                    return

            if self.path.startswith("/diag"):
                import socket, ssl, urllib.request, urllib.error
                diag = {}
                host = "lo.ergnwp09.com"
                try:
                    diag["dns"] = socket.getaddrinfo(host, 443, socket.AF_INET)
                except Exception as e:
                    diag["dns_error"] = str(e)

                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.settimeout(3.0)
                    t0 = time.time()
                    s.connect((host, 443))
                    diag["tcp_connect_time"] = round(time.time() - t0, 3)
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    ss = ctx.wrap_socket(s, server_hostname=host)
                    diag["ssl_handshake_ok"] = True
                    ss.close()
                except Exception as e:
                    diag["tcp_ssl_error"] = str(e)

                try:
                    req = urllib.request.Request(
                        f"https://{host}/ws",
                        headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13; Mobile)"}
                    )
                    with urllib.request.urlopen(req, timeout=3.0) as resp:
                        diag["http_status"] = resp.status
                except urllib.error.HTTPError as e:
                    diag["http_status"] = e.code
                    diag["http_reason"] = str(e.reason)
                except Exception as e:
                    diag["http_error"] = str(e)

                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(diag).encode("utf-8"))
                return

            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            status = {
                "status": "online",
                "service": "MaxRummy & Yono Multi-Platform Automation Bot",
                "uptime_seconds": round(time.time() - admin_manager.bot_start_time, 1),
                "total_users": len(admin_manager.user_registry),
                "platforms_count": len(PLATFORMS_LIST)
            }
            self.wfile.write(json.dumps(status).encode("utf-8"))

        def log_message(self, format, *args):
            return  # Suppress noisy HTTP access logs

    env_port = os.getenv("PORT") or os.getenv("SERVER_PORT") or str(port)
    try:
        bind_port = int(env_port)
    except ValueError:
        bind_port = port

    def _serve():
        try:
            with socketserver.TCPServer(("", bind_port), HealthHandler) as httpd:
                log(f"⚡ Cloud Health Server active on port {bind_port} (24/7 Keep-Alive)")
                httpd.serve_forever()
        except Exception as e:
            log(f"⚠️ Health Server start error: {e}")

    t = threading.Thread(target=_serve, daemon=True, name="CloudHealthServer")
    t.start()


# ─────────────────────────────────────────────────────────────
#  Helpers & Keyboards
# ─────────────────────────────────────────────────────────────

def get_main_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=2)
    b_multi = types.InlineKeyboardButton(f"🌐 Multi-Platform Checker ({len(PLATFORMS_LIST)} Yono Apps)", callback_data="btn_check_platforms", style="primary")
    b_reg = types.InlineKeyboardButton("🎁 Bind Number & Get Bonus", callback_data="btn_reg_single", style="success")
    b_claim = types.InlineKeyboardButton("💰 Claim Bonus", callback_data="btn_claim_single", style="primary")
    b_check = types.InlineKeyboardButton("💵 Check Balance", callback_data="btn_check_single", style="primary")
    b_bulk_claim = types.InlineKeyboardButton("⚡ Bulk Claim", callback_data="btn_claim_bulk", style="success")
    b_bulk_check = types.InlineKeyboardButton("📊 Bulk Balance", callback_data="btn_check_bulk", style="primary")
    b_profile = types.InlineKeyboardButton("👤 Wallet / Tokens", callback_data="btn_profile", style="primary")
    b_support = types.InlineKeyboardButton("💬 Support", callback_data="btn_support", style="primary")
    
    markup.add(b_multi)
    markup.add(b_reg)
    markup.add(b_claim, b_check)
    markup.add(b_bulk_claim, b_bulk_check)
    markup.add(b_profile, b_support)

    if admin_manager.is_admin(user_id):
        b_admin = types.InlineKeyboardButton("👑 Admin Suite", callback_data="btn_admin", style="danger")
        markup.add(b_admin)

    return markup


def get_insufficient_tokens_msg(user_id: int, required: int = 1) -> Tuple[str, types.InlineKeyboardMarkup]:
    tokens = admin_manager.get_tokens(user_id)
    text = (
        f"⚠️ <b>Insufficient Tokens!</b>\n\n"
        f"❌ Aapke paas is task ke liye paryapt tokens nahi hain!\n"
        f"• <b>Current Tokens:</b> <code>{tokens}</code>\n"
        f"• <b>Required Tokens:</b> <code>{required}</code>\n\n"
        f"💳 Tokens recharge karwane ke liye turant contact karein:\n"
        f"👉 <b>@OwAtom</b>\n"
        f"👉 <b>@OwKarma</b>"
    )
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        types.InlineKeyboardButton("💬 Contact @OwAtom", url="https://t.me/OwAtom", style="primary"),
        types.InlineKeyboardButton("💬 Contact @OwKarma", url="https://t.me/OwKarma", style="primary")
    )
    markup.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="btn_main_menu", style="danger"))
    return text, markup


def save_account_record(filepath: Path, line: str):
    try:
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(line.strip() + "\n")
    except Exception as e:
        log(f"Failed to save account record: {e}")


# ─────────────────────────────────────────────────────────────
#  Bot Command Handlers
# ─────────────────────────────────────────────────────────────

@bot.message_handler(commands=["start", "menu"])
def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name
    admin_manager.record_user(user_id, username=username, first_name=message.from_user.first_name)

    if admin_manager.is_maintenance_mode() and not admin_manager.is_admin(user_id):
        bot.reply_to(message, "🛠️ <b>Bot Maintenance Mode Me Hai!</b>\nKuch hi der me services wapas live ho jayengi.")
        return

    tokens = admin_manager.get_display_tokens(user_id)
    role = admin_manager.get_user_role_label(user_id)

    welcome_text = (
        f"👋 <b>Welcome, {message.from_user.first_name}!</b>\n\n"
        f"🎰 <b>MaxRummy & YonoGames Auto-Bot</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>User ID:</b> <code>{user_id}</code>\n"
        f"• <b>Role:</b> {role}\n"
        f"• <b>Available Tokens:</b> <code>{tokens}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>Available Features:</b>\n"
        f"• 🌐 <b>Multi-Platform Checker:</b> Check {len(PLATFORMS_LIST)}+ Yono apps live & 1-by-1 register\n"
        f"• 📱 <b>Register:</b> OTP verify + auto-claim Bind Bonus\n"
        f"• 🔗 <b>Bind Phone:</b> Account bind karo & instant bonus lo\n"
        f"• 🎁 <b>Claim Bonus:</b> Bind gift, 7-Day & daily rewards auto-collect\n"
        f"• 💰 <b>Check Balance:</b> Single & Bulk balance extraction\n\n"
        f"Neeche diye gaye buttons se apna task select karein:"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_keyboard(user_id))


@bot.message_handler(commands=["balance", "tokens"])
def cmd_balance(message: types.Message):
    user_id = message.from_user.id
    tokens = admin_manager.get_display_tokens(user_id)
    role = admin_manager.get_user_role_label(user_id)
    text = (
        f"👤 <b>Your Account Info</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>User ID:</b> <code>{user_id}</code>\n"
        f"• <b>Role:</b> {role}\n"
        f"• <b>Tokens Balance:</b> <code>{tokens}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Agar aapko tokens recharge karane hain to @OwAtom ya @OwKarma se sampark karein."
    )
    bot.reply_to(message, text, reply_markup=get_main_keyboard(user_id))


@bot.message_handler(commands=["check", "probe"])
def cmd_check_platforms(message: types.Message):
    user_id = message.from_user.id
    if admin_manager.is_maintenance_mode() and not admin_manager.is_admin(user_id):
        bot.reply_to(message, "🛠️ <b>Bot Maintenance Mode Me Hai!</b>")
        return

    parts = message.text.split()
    if len(parts) > 1:
        phone = clean_phone_number(parts[1])
        if len(phone) == 10:
            execute_platform_check(message.chat.id, user_id, phone, reply_to_msg_id=message.message_id)
            return

    prompt_txt = (
        "🌐 <b>Multi-Platform Availability Checker</b>\n\n"
        "Kripya <b>10-digit Mobile Number</b> bhejein jisko check karna hai:\n"
        "<i>(Example: 9876543210)</i>\n\n"
        f"⚡ Ye number ko ek sath <b>{len(PLATFORMS_LIST)} Yono platforms</b> (Spin Gold, Max Rummy, IND Club, Yono Rummy, Spin 777, etc.) par parallel check karega.\n\n"
        "Cancel karne ke liye /cancel bhejein."
    )
    msg_obj = bot.send_message(message.chat.id, prompt_txt)
    bot.register_next_step_handler(msg_obj, step_check_platforms_get_phone)


# ─────────────────────────────────────────────────────────────
#  Callback Query Router
# ─────────────────────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: True)
def on_callback_query(call: types.CallbackQuery):
    user_id = call.from_user.id
    data = call.data

    admin_manager.record_user(user_id, username=call.from_user.username, first_name=call.from_user.first_name)

    if is_rate_limited(user_id, min_interval=0.4):
        bot.answer_callback_query(call.id, "⚠️ Please wait a moment...", show_alert=False)
        return

    if admin_manager.is_maintenance_mode() and not admin_manager.is_admin(user_id):
        bot.answer_callback_query(call.id, "Bot maintenance mode me hai!", show_alert=True)
        return

    if data == "btn_main_menu":
        bot.answer_callback_query(call.id)
        tokens = admin_manager.get_display_tokens(user_id)
        role = admin_manager.get_user_role_label(user_id)
        welcome_text = (
            f"👋 <b>Welcome Back!</b>\n\n"
            f"🎰 <b>MaxRummy & YonoGames Auto-Bot</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>User ID:</b> <code>{user_id}</code>\n"
            f"• <b>Role:</b> {role}\n"
            f"• <b>Tokens:</b> <code>{tokens}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Choose an option below:"
        )
        bot.edit_message_text(welcome_text, call.message.chat.id, call.message.message_id, reply_markup=get_main_keyboard(user_id))

    elif data == "btn_profile":
        bot.answer_callback_query(call.id)
        tokens = admin_manager.get_display_tokens(user_id)
        role = admin_manager.get_user_role_label(user_id)
        txt = (
            f"👤 <b>User Wallet & Statistics</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>User ID:</b> <code>{user_id}</code>\n"
            f"• <b>Username:</b> @{call.from_user.username or 'N/A'}\n"
            f"• <b>Role:</b> {role}\n"
            f"• <b>Tokens:</b> <code>{tokens}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Need more tokens? Contact 👉 @OwAtom / @OwKarma"
        )
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="btn_main_menu", style="danger"))
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif data == "btn_support":
        bot.answer_callback_query(call.id)
        txt = (
            f"💬 <b>Official Support & Token Recharge</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Tokens purchase karne, bulk balance checker access lene, ya kisi bhi query ke liye official admins se contact karein:\n\n"
            f"👑 <b>Owner:</b> @OwAtom\n"
            f"🛡️ <b>Admin:</b> @OwKarma\n\n"
            f"⚡ Instant recharge & 24x7 support available."
        )
        kb = types.InlineKeyboardMarkup(row_width=2)
        kb.add(
            types.InlineKeyboardButton("💬 @OwAtom", url="https://t.me/OwAtom", style="primary"),
            types.InlineKeyboardButton("💬 @OwKarma", url="https://t.me/OwKarma", style="primary")
        )
        kb.add(types.InlineKeyboardButton("🔙 Main Menu", callback_data="btn_main_menu", style="danger"))
        bot.edit_message_text(txt, call.message.chat.id, call.message.message_id, reply_markup=kb)

    # ─────────────────────────────────────────────────────────────
    #  Multi-Platform Checker & Registration Callbacks
    # ─────────────────────────────────────────────────────────────
    elif data == "btn_check_platforms":
        bot.answer_callback_query(call.id)
        prompt_txt = (
            "🌐 <b>Multi-Platform Availability Checker</b>\n\n"
            "Kripya apna <b>10-digit Mobile Number</b> bhejein jisko check karna hai:\n"
            "<i>(Example: 9876543210)</i>\n\n"
            f"⚡ Ye number ko ek sath <b>{len(PLATFORMS_LIST)} Yono platforms</b> (Spin Gold, Max Rummy, IND Club, Yono Rummy, Spin 777, etc.) par parallel check karega.\n\n"
            "Cancel karne ke liye /cancel bhejein."
        )
        msg_obj = bot.send_message(call.message.chat.id, prompt_txt)
        bot.register_next_step_handler(msg_obj, step_check_platforms_get_phone)

    elif data.startswith("pchk:"):
        phone = data.split(":", 1)[1]
        bot.answer_callback_query(call.id, "Re-checking all platforms...")
        execute_platform_check(call.message.chat.id, user_id, phone, message_id=call.message.message_id)

    elif data.startswith("preg:"):
        parts = data.split(":", 2)
        if len(parts) == 3:
            _, p_id, phone = parts
            initiate_single_platform_reg(call, p_id, phone)

    elif data.startswith("pregall:"):
        parts = data.split(":", 1)
        if len(parts) == 2:
            phone = parts[1]
            initiate_multi_platform_reg(call, phone)

    elif data.startswith("psel1:"):
        phone = data.split(":", 1)[1]
        bot.answer_callback_query(call.id, "Loading platforms...")
        render_select_single_platform(call.message.chat.id, user_id, phone, message_id=call.message.message_id)

    elif data.startswith("preport:"):
        phone = data.split(":", 1)[1]
        bot.answer_callback_query(call.id, "Back to report...")
        render_platform_report(call.message.chat.id, user_id, phone, message_id=call.message.message_id)

    # ─────────────────────────────────────────────────────────────
    #  Bind Number & Get Bonus Flow (New Registration + Bind Gift)
    # ─────────────────────────────────────────────────────────────
    elif data == "btn_reg_single":
        if not admin_manager.has_enough_tokens(user_id, 1):
            msg, kb = get_insufficient_tokens_msg(user_id, 1)
            bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb)
            return

        bot.answer_callback_query(call.id)
        prompt_txt = (
            "🎁 <b>Bind Number & Get Bonus (वेलकम बोनस क्लेम)</b>\n\n"
            "Kripya apna <b>10-digit Mobile Number</b> bhejein jisko bind karke bonus claim karna hai:\n"
            "<i>(Example: 9876543210)</i>\n\n"
            "Number par instant OTP aayega. Cancel karne ke liye /cancel bhejein."
        )
        msg_obj = bot.send_message(call.message.chat.id, prompt_txt)
        bot.register_next_step_handler(msg_obj, step_reg_get_phone)

    # ─────────────────────────────────────────────────────────────
    #  Bind Phone & Claim Bonus Flow
    # ─────────────────────────────────────────────────────────────
    elif data == "btn_bind_phone":
        if not admin_manager.has_enough_tokens(user_id, 1):
            msg, kb = get_insufficient_tokens_msg(user_id, 1)
            bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb)
            return

        bot.answer_callback_query(call.id)
        prompt_txt = (
            "🔗 <b>Bind Phone Number & Claim Bonus</b>\n\n"
            "Kripya target account details aur bind karne wala number bhejein format me:\n"
            "<code>Phone:Password</code> (jis account me login karke bind karna hai)\n\n"
            "<i>Example:</i> <code>9876543210:MyPass123</code>\n\n"
            "Ya cancel karne ke liye /cancel bhejein."
        )
        msg_obj = bot.send_message(call.message.chat.id, prompt_txt)
        bot.register_next_step_handler(msg_obj, step_bind_phone_step1)

    # ─────────────────────────────────────────────────────────────
    #  Single Bonus Claim Flow
    # ─────────────────────────────────────────────────────────────
    elif data == "btn_claim_single":
        if not admin_manager.has_enough_tokens(user_id, 1):
            msg, kb = get_insufficient_tokens_msg(user_id, 1)
            bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb)
            return

        bot.answer_callback_query(call.id)
        prompt_txt = (
            "🎁 <b>Claim Account Bonuses (Bind Gift, 7-Day & Daily)</b>\n\n"
            "Kripya account credentials bhejein format me:\n"
            "<code>Phone:Password</code>\n\n"
            "<i>Example:</i> <code>9876543210:MyPass123</code>\n\n"
            "Ye automatically Bind Bonus (Welcome Gift), 7-Day Sign-in, aur Daily Rewards claim karega!\n"
            "Ya cancel karne ke liye /cancel bhejein."
        )
        msg_obj = bot.send_message(call.message.chat.id, prompt_txt)
        bot.register_next_step_handler(msg_obj, step_claim_single_account)

    # ─────────────────────────────────────────────────────────────
    #  Bulk Bonus Claim Flow
    # ─────────────────────────────────────────────────────────────
    elif data == "btn_claim_bulk":
        tokens = admin_manager.get_tokens(user_id)
        if tokens <= 0 and not admin_manager.is_free_mode():
            msg, kb = get_insufficient_tokens_msg(user_id, 1)
            bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb)
            return

        bot.answer_callback_query(call.id)
        prompt_txt = (
            "⚡ <b>Bulk Bonus Claimer (All Accounts)</b>\n\n"
            "Aap accounts ki list paste kar sakte hain ya ek <b>.txt file</b> upload kar sakte hain.\n\n"
            "<b>Format:</b>\n"
            "<code>phone:password</code>\n"
            "<code>phone:password</code>\n\n"
            f"• <b>Your Token Balance:</b> <code>{tokens}</code>\n"
            "<i>(Har successful claim process par 1 token deduct hoga)</i>\n\n"
            "Bhejiye accounts list ya file:"
        )
        msg_obj = bot.send_message(call.message.chat.id, prompt_txt)
        bot.register_next_step_handler(msg_obj, step_claim_bulk_accounts)

    # ─────────────────────────────────────────────────────────────
    #  Single Balance Check Flow
    # ─────────────────────────────────────────────────────────────
    elif data == "btn_check_single":
        if not admin_manager.has_enough_tokens(user_id, 1):
            msg, kb = get_insufficient_tokens_msg(user_id, 1)
            bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb)
            return

        bot.answer_callback_query(call.id)
        prompt_txt = (
            "💰 <b>Single Account Balance Check</b>\n\n"
            "Kripya account credentials bhejein format me:\n"
            "<code>Phone:Password</code>\n\n"
            "<i>Example:</i> <code>9876543210:MyPass123</code>\n\n"
            "Ya cancel karne ke liye /cancel bhejein."
        )
        msg_obj = bot.send_message(call.message.chat.id, prompt_txt)
        bot.register_next_step_handler(msg_obj, step_check_single_account)

    # ─────────────────────────────────────────────────────────────
    #  Bulk Balance Check Flow
    # ─────────────────────────────────────────────────────────────
    elif data == "btn_check_bulk":
        tokens = admin_manager.get_tokens(user_id)
        if tokens <= 0 and not admin_manager.is_free_mode():
            msg, kb = get_insufficient_tokens_msg(user_id, 1)
            bot.edit_message_text(msg, call.message.chat.id, call.message.message_id, reply_markup=kb)
            return

        bot.answer_callback_query(call.id)
        prompt_txt = (
            "📊 <b>Bulk Accounts Balance Checker</b>\n\n"
            "Aap accounts ki list paste kar sakte hain ya ek <b>.txt file</b> upload kar sakte hain.\n\n"
            "<b>Format:</b>\n"
            "<code>phone:password</code>\n"
            "<code>phone:password</code>\n\n"
            f"• <b>Your Token Balance:</b> <code>{tokens}</code>\n"
            "<i>(Har successful account check par 1 token deduct hoga)</i>\n\n"
            "Bhejiye accounts list ya file:"
        )
        msg_obj = bot.send_message(call.message.chat.id, prompt_txt)
        bot.register_next_step_handler(msg_obj, step_check_bulk_accounts)

    # ─────────────────────────────────────────────────────────────
    #  Admin Suite
    # ─────────────────────────────────────────────────────────────
    elif data == "btn_admin":
        if not admin_manager.is_admin(user_id):
            bot.answer_callback_query(call.id, "Access Denied!", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        show_admin_panel(call.message.chat.id, call.message.message_id)

    elif data == "adm_toggle_maint":
        if not admin_manager.is_admin(user_id):
            return
        m = admin_manager.toggle_maintenance_mode()
        bot.answer_callback_query(call.id, f"Maintenance Mode: {'ON' if m else 'OFF'}", show_alert=True)
        show_admin_panel(call.message.chat.id, call.message.message_id)

    elif data == "adm_toggle_free":
        if not admin_manager.is_admin(user_id):
            return
        f = admin_manager.toggle_free_mode()
        bot.answer_callback_query(call.id, f"Free Mode: {'ON' if f else 'OFF'}", show_alert=True)
        show_admin_panel(call.message.chat.id, call.message.message_id)

    elif data == "adm_users_list":
        if not admin_manager.is_admin(user_id):
            return
        bot.answer_callback_query(call.id)
        users = admin_manager.user_registry
        lines = [f"👥 <b>Total Registered Users: {len(users)}</b>\n"]
        for uid_s, u in list(users.items())[:20]:
            lines.append(f"• <code>{uid_s}</code> (@{u.get('username') or 'N/A'}) - {u.get('role')} - <b>{u.get('tokens')} T</b>")
        if len(users) > 20:
            lines.append(f"\n<i>...aur {len(users)-20} users hain.</i>")
        kb = types.InlineKeyboardMarkup()
        kb.add(types.InlineKeyboardButton("🔙 Admin Panel", callback_data="btn_admin", style="danger"))
        bot.edit_message_text("\n".join(lines), call.message.chat.id, call.message.message_id, reply_markup=kb)

    elif data == "adm_ledger_view":
        if not admin_manager.is_super_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Access Denied! Owner Only.", show_alert=True)
            return
        bot.answer_callback_query(call.id)
        show_ledger_view(call.message.chat.id, call.message.message_id)

    elif data.startswith("adm_ledger_ref_"):
        if not admin_manager.is_super_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Access Denied! Owner Only.", show_alert=True)
            return
        bot.answer_callback_query(call.id, "🔄 Refreshing ledger...")
        raw_uid = data.replace("adm_ledger_ref_", "")
        filt_uid = int(raw_uid) if raw_uid.isdigit() and int(raw_uid) > 0 else None
        show_ledger_view(call.message.chat.id, call.message.message_id, filter_user_id=filt_uid)

    elif data.startswith("adm_ledger_exp_"):
        if not admin_manager.is_super_admin(user_id):
            bot.answer_callback_query(call.id, "⛔ Access Denied! Owner Only.", show_alert=True)
            return
        bot.answer_callback_query(call.id, "📥 Generating report...")
        raw_uid = data.replace("adm_ledger_exp_", "")
        filt_uid = int(raw_uid) if raw_uid.isdigit() and int(raw_uid) > 0 else None
        send_ledger_file(call.message.chat.id, filter_user_id=filt_uid)


def show_ledger_view(chat_id: int, message_id: Optional[int] = None, filter_user_id: Optional[int] = None):
    events = admin_manager.get_recent_ledger(limit=10, filter_user_id=filter_user_id)
    filter_label = f" (User: <code>{filter_user_id}</code>)" if filter_user_id else ""
    lines = [
        f"📜 <b>Owner Audit & Activity Ledger</b>{filter_label}",
        f"<i>Latest {len(events)} events (Newest first):</i>",
        "━━━━━━━━━━━━━━━━━━━━━━"
    ]
    if not events:
        lines.append("<i>No transactions recorded yet in ledger.</i>")
    else:
        for ev in events:
            ts = ev.get("timestamp", "N/A")
            uid = ev.get("user_id", "N/A")
            uname = f"@{ev.get('username')}" if ev.get("username") else (ev.get("first_name") or f"ID:{uid}")
            action = ev.get("action", "EVENT")
            chg = ev.get("change", 0)
            chg_str = f"+{chg}" if chg > 0 else str(chg)
            bal = ev.get("tokens_after", 0)
            reason = ev.get("reason", "Activity")

            icon = "🔻" if chg < 0 else ("🔺" if chg > 0 else "ℹ️")
            lines.append(
                f"{icon} <b>{action}</b> ({chg_str} T) → Bal: <b>{bal} T</b>\n"
                f"   • <b>User:</b> {uname} (<code>{uid}</code>)\n"
                f"   • <b>Reason:</b> <code>{reason}</code>\n"
                f"   • <b>Time:</b> <code>{ts}</code>\n"
            )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    kb = types.InlineKeyboardMarkup(row_width=2)
    cb_refresh = f"adm_ledger_ref_{filter_user_id}" if filter_user_id else "adm_ledger_ref_0"
    cb_export = f"adm_ledger_exp_{filter_user_id}" if filter_user_id else "adm_ledger_exp_0"
    b_ref = types.InlineKeyboardButton("🔄 Refresh", callback_data=cb_refresh, style="primary")
    b_exp = types.InlineKeyboardButton("📥 Export TXT Report", callback_data=cb_export, style="success")
    b_back = types.InlineKeyboardButton("🔙 Admin Panel", callback_data="btn_admin", style="danger")
    kb.add(b_ref, b_exp)
    kb.add(b_back)

    text = "\n".join(lines)
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        except Exception:
            bot.send_message(chat_id, text, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)


def send_ledger_file(chat_id: int, filter_user_id: Optional[int] = None):
    report_text = admin_manager.export_ledger_text(filter_user_id=filter_user_id)
    doc = io.BytesIO(report_text.encode("utf-8"))
    date_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    target_str = f"user_{filter_user_id}_" if filter_user_id else "all_"
    doc.name = f"Audit_Ledger_{target_str}{date_str}.txt"
    bot.send_document(
        chat_id,
        doc,
        caption=(
            f"📄 <b>Full Owner Audit Ledger Export</b>\n"
            f"• Scope: {'User ' + str(filter_user_id) if filter_user_id else 'All Users & Activities'}\n"
            f"• Date: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}\n"
            f"• Generated for Owner: <code>8327850869</code>"
        )
    )


def show_admin_panel(chat_id: int, message_id: Optional[int] = None):
    maint_status = "🔴 ON" if admin_manager.is_maintenance_mode() else "🟢 OFF"
    free_status = "🟢 ON" if admin_manager.is_free_mode() else "🔴 OFF"
    total_users = len(admin_manager.user_registry)
    total_tokens = admin_manager.get_total_distributed_tokens()
    is_owner = admin_manager.is_super_admin(chat_id)

    owner_cmds = ""
    if is_owner:
        owner_cmds = (
            f"• <code>/ledger [user_id]</code> (Activity Ledger)\n"
            f"• <code>/exportledger [user_id]</code> (Export Report)\n"
        )

    txt = (
        f"👑 <b>Admin Control Suite</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"• <b>Maintenance Mode:</b> {maint_status}\n"
        f"• <b>Free Mode:</b> {free_status}\n"
        f"• <b>Total Users:</b> {total_users}\n"
        f"• <b>Active Tokens Pool:</b> {total_tokens:,}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Commands Available:\n"
        f"• <code>/addtokens &lt;user_id&gt; &lt;amount&gt;</code>\n"
        f"• <code>/settokens &lt;user_id&gt; &lt;amount&gt;</code>\n"
        f"• <code>/broadcast &lt;message&gt;</code>\n"
        f"{owner_cmds}"
    )
    kb = types.InlineKeyboardMarkup(row_width=2)
    b_maint = types.InlineKeyboardButton(f"🛠️ Maint: {maint_status}", callback_data="adm_toggle_maint", style="danger" if admin_manager.is_maintenance_mode() else "primary")
    b_free = types.InlineKeyboardButton(f"🎁 Free Mode: {free_status}", callback_data="adm_toggle_free", style="success" if admin_manager.is_free_mode() else "primary")
    b_users = types.InlineKeyboardButton("👥 Users List", callback_data="adm_users_list", style="primary")
    kb.add(b_maint, b_free)
    if is_owner:
        b_ledger = types.InlineKeyboardButton("📜 Owner Audit Ledger", callback_data="adm_ledger_view", style="primary")
        kb.add(b_users, b_ledger)
    else:
        kb.add(b_users)
    b_back = types.InlineKeyboardButton("🔙 Main Menu", callback_data="btn_main_menu", style="danger")
    kb.add(b_back)

    if message_id:
        bot.edit_message_text(txt, chat_id, message_id, reply_markup=kb)
    else:
        bot.send_message(chat_id, txt, reply_markup=kb)


# ─────────────────────────────────────────────────────────────
#  Multi-Platform Checker & Registration Engine (10+ Yono)
# ─────────────────────────────────────────────────────────────

def step_check_platforms_get_phone(message: types.Message):
    user_id = message.from_user.id
    if message.text and message.text.startswith("/cancel"):
        bot.reply_to(message, "❌ Check cancelled.", reply_markup=get_main_keyboard(user_id))
        return

    phone = clean_phone_number(message.text or "")
    if len(phone) != 10:
        msg = bot.reply_to(message, "❌ Invalid number! 10-digit number bhejein (ya /cancel karein):")
        bot.register_next_step_handler(msg, step_check_platforms_get_phone)
        return

    execute_platform_check(message.chat.id, user_id, phone, reply_to_msg_id=message.message_id)


def build_platform_report(clean_phone: str, results: List[Dict[str, Any]]) -> Tuple[str, types.InlineKeyboardMarkup]:
    avail = [r for r in results if r.get("status") == "available"]
    reg = [r for r in results if r.get("status") == "registered"]
    cooldown = [r for r in results if r.get("status") == "cooldown"]
    errors = [r for r in results if r.get("status") not in ("available", "registered", "cooldown")]

    lines = [
        f"🌐 <b>Multi-Platform Status Report</b>",
        f"━━━━━━━━━━━━━━━━━━━━━━",
        f"📱 <b>Mobile:</b> <code>{clean_phone}</code>",
        f"📊 <b>Total Checked:</b> {len(results)} Platforms\n"
    ]

    if avail:
        lines.append(f"🟢 <b>Fresh / Available for Registration ({len(avail)}):</b>")
        for r in avail:
            lines.append(f"  • <b>{html.escape(r['name'])}</b> <i>(Fresh)</i>")
        lines.append("")

    if reg:
        lines.append(f"🔴 <b>Already Registered ({len(reg)}):</b>")
        if len(reg) <= 15:
            for r in reg:
                lines.append(f"  • {html.escape(r['name'])}")
        else:
            reg_names = ", ".join(html.escape(r['name']) for r in reg)
            lines.append(f"  <i>{reg_names}</i>")
        lines.append("")

    if cooldown:
        lines.append(f"⏳ <b>Number on Cooldown ({len(cooldown)}):</b>")
        for r in cooldown:
            m = html.escape(str(r.get("msg", "Cooldown")))
            lines.append(f"  • {html.escape(r['name'])} - <i>{m}</i>")
        lines.append("")

    if errors:
        lines.append(f"⚠️ <b>Other / Offline ({len(errors)}):</b>")
        if len(errors) <= 8:
            for r in errors:
                m = html.escape(str(r.get("msg", "Offline")))
                lines.append(f"  • {html.escape(r['name'])} - <i>{m}</i>")
        else:
            err_names = ", ".join(html.escape(r['name']) for r in errors)
            lines.append(f"  <i>{err_names}</i>")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    if avail:
        lines.append(f"🟢 <b>{len(avail)} Fresh Platforms Available!</b>\n"
                     f"• Sabhi par auto-register karne ke liye <b>1 by 1 All</b> dabayein.\n"
                     f"• Niche diye gaye <b>🎯 Select 1 Platform to Claim Bonus</b> button ko dabakar kisi bhi specific app ko chunein.")
    else:
        lines.append("⚠️ Is number par koi bhi platform fresh/available nahi mila.")

    text = "\n".join(lines)
    if len(text) > 3900:
        text = text[:3850] + "\n\n<i>[Report truncated for message length limit]</i>"

    kb = types.InlineKeyboardMarkup()
    if avail:
        kb.row(types.InlineKeyboardButton(f"⚡ 1 by 1 All ({len(avail)} Platforms)", callback_data=f"pregall:{clean_phone}", style="success"))
        kb.row(types.InlineKeyboardButton(f"🎯 Select 1 Platform to Claim Bonus", callback_data=f"psel1:{clean_phone}", style="primary"))

    kb.row(
        types.InlineKeyboardButton("🔄 Re-check", callback_data=f"pchk:{clean_phone}", style="primary"),
        types.InlineKeyboardButton("🔙 Main Menu", callback_data="btn_main_menu", style="danger")
    )
    return text, kb


def render_select_single_platform(chat_id: int, user_id: int, phone: str, message_id: int):
    clean_phone = clean_phone_number(phone)
    results = get_check_cache(user_id, clean_phone)
    if not results:
        results = MaxRummyClient.check_all_platforms(clean_phone, timeout=6.0)
        set_check_cache(user_id, clean_phone, results)

    avail = [r for r in results if r.get("status") == "available"]
    if not avail:
        safe_edit_or_send(
            chat_id,
            f"⚠️ <b>Koi bhi platform fresh/available nahi mila mobile <code>{clean_phone}</code> ke liye!</b>",
            reply_markup=get_main_keyboard(user_id),
            message_id=message_id
        )
        return

    text = (
        f"🎯 <b>Select 1 Platform to Claim Bonus</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 <b>Mobile:</b> <code>{clean_phone}</code>\n"
        f"🟢 <b>Available Unregistered Platforms ({len(avail)}):</b>\n\n"
        f"Niche kisi bhi platform par click karein, bot OTP send karke Welcome Bonus claim karega:"
    )

    kb = types.InlineKeyboardMarkup()
    p_buttons = []
    for r in avail:
        pid = r["platform_id"]
        pname = r["name"]
        p_buttons.append(types.InlineKeyboardButton(f"🎁 {pname}", callback_data=f"preg:{pid}:{clean_phone}", style="success"))

    for i in range(0, len(p_buttons), 2):
        kb.row(*p_buttons[i:i+2])

    kb.row(
        types.InlineKeyboardButton("🔙 Back to Report", callback_data=f"preport:{clean_phone}", style="primary"),
        types.InlineKeyboardButton("🔙 Main Menu", callback_data="btn_main_menu", style="danger")
    )

    safe_edit_or_send(chat_id, text, reply_markup=kb, message_id=message_id)


def render_platform_report(chat_id: int, user_id: int, phone: str, message_id: int):
    clean_phone = clean_phone_number(phone)
    results = get_check_cache(user_id, clean_phone)
    if results:
        text, kb = build_platform_report(clean_phone, results)
        safe_edit_or_send(chat_id, text, reply_markup=kb, message_id=message_id)
    else:
        execute_platform_check(chat_id, user_id, clean_phone, message_id=message_id)


def execute_platform_check(chat_id: int, user_id: int, phone: str, reply_to_msg_id: Optional[int] = None, message_id: Optional[int] = None):
    clean_phone = clean_phone_number(phone)
    if len(clean_phone) != 10:
        bot.send_message(chat_id, "❌ Invalid phone number! 10-digit number required.")
        return

    init_text = f"🔍 <b>Checking mobile <code>{clean_phone}</code> across {len(PLATFORMS_LIST)} Yono platforms in parallel...</b>"
    status_msg_id = None
    if message_id:
        try:
            bot.edit_message_text(init_text, chat_id, message_id, parse_mode="HTML")
            status_msg_id = message_id
        except Exception:
            status_msg = bot.send_message(chat_id, init_text, parse_mode="HTML")
            status_msg_id = status_msg.message_id
    else:
        status_msg = bot.send_message(chat_id, init_text, reply_to_message_id=reply_to_msg_id, parse_mode="HTML")
        status_msg_id = status_msg.message_id

    def _worker():
        try:
            log(f"Starting parallel check across {len(PLATFORMS_LIST)} platforms for {clean_phone}...")
            results = MaxRummyClient.check_all_platforms(clean_phone, timeout=6.0)
            set_check_cache(user_id, clean_phone, results)
            avail = [r for r in results if r.get("status") == "available"]
            reg = [r for r in results if r.get("status") == "registered"]
            cooldown = [r for r in results if r.get("status") == "cooldown"]
            errors = [r for r in results if r.get("status") not in ("available", "registered", "cooldown")]
            log(f"Check done for {clean_phone}: {len(avail)} fresh, {len(reg)} registered, {len(cooldown)} cooldown, {len(errors)} err")

            text, kb = build_platform_report(clean_phone, results)
            safe_edit_or_send(chat_id, text, reply_markup=kb, message_id=status_msg_id)
        except Exception as e:
            import traceback
            traceback.print_exc()
            log(f"Platform check error for {clean_phone}: {e}")
            safe_edit_or_send(
                chat_id,
                f"❌ <b>Platform check error:</b> {html.escape(str(e))}",
                reply_markup=get_main_keyboard(user_id),
                message_id=status_msg_id
            )

    threading.Thread(target=_worker, daemon=True).start()


def initiate_single_platform_reg(call: types.CallbackQuery, platform_id: str, phone: str):
    user_id = call.from_user.id
    if not admin_manager.has_enough_tokens(user_id, 1):
        msg, kb = get_insufficient_tokens_msg(user_id, 1)
        safe_edit_or_send(call.message.chat.id, msg, reply_markup=kb, message_id=call.message.message_id)
        return

    p_info = PLATFORMS_MAP.get(platform_id)
    if not p_info:
        bot.answer_callback_query(call.id, "Platform not found!", show_alert=True)
        return

    p_name = p_info["name"]
    bot.answer_callback_query(call.id, f"Sending OTP for {p_name}...")

    status_msg = bot.send_message(
        call.message.chat.id,
        f"⏳ <b>[{html.escape(p_name)}]</b> Mobile <code>{phone}</code> par OTP bheja ja raha hai..."
    )

    try:
        client = MaxRummyClient(platform=p_info)
        res = client.send_otp(phone)
    except Exception as e:
        res = {"ok": False, "msg": f"Connection error: {str(e)}"}

    if not res.get("ok"):
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("🎯 Select Another Platform", callback_data=f"psel1:{phone}", style="primary"),
            types.InlineKeyboardButton("🔄 Re-check", callback_data=f"pchk:{phone}", style="primary")
        )
        kb.row(
            types.InlineKeyboardButton("🔙 Main Menu", callback_data="btn_main_menu", style="danger")
        )
        safe_edit_or_send(
            call.message.chat.id,
            f"❌ <b>OTP Send Failed for {html.escape(p_name)}!</b>\nReason: {html.escape(str(res.get('msg')))}\n\nKripya dobara koshish karein.",
            reply_markup=kb,
            message_id=status_msg.message_id
        )
        return

    set_user_state(user_id, {
        "action": "single_platform_reg",
        "platform_id": platform_id,
        "platform_name": p_name,
        "phone": phone,
        "otp_time": time.time()
    })

    prompt_txt = (
        f"✅ <b>OTP Sent Successfully for {html.escape(p_name)}!</b>\n\n"
        f"📱 <b>Mobile:</b> <code>{phone}</code>\n"
        f"🎰 <b>Platform:</b> {html.escape(p_name)}\n\n"
        f"Kripya received <b>OTP aur Password</b> bhejein format me:\n"
        f"<code>OTP:Password</code>\n\n"
        f"<i>(Example: 123456:Pass@1234, ya sirf OTP bhejein to auto-password set hoga)</i>\n"
        f"Cancel karne ke liye /cancel bhejein."
    )
    safe_edit_or_send(call.message.chat.id, prompt_txt, message_id=status_msg.message_id)
    bot.register_next_step_handler(status_msg, step_platform_otp_finish)


def step_platform_otp_finish(message: types.Message):
    user_id = message.from_user.id
    if message.text and message.text.startswith("/cancel"):
        pop_user_state(user_id)
        bot.reply_to(message, "❌ Registration cancelled.", reply_markup=get_main_keyboard(user_id))
        return

    state = get_user_state(user_id)
    if not state or state.get("action") != "single_platform_reg":
        bot.reply_to(message, "⚠️ Session expired! Kripya /start se dobara try karein.", reply_markup=get_main_keyboard(user_id))
        return

    text = message.text.strip()
    if ":" in text:
        parts = text.split(":", 1)
        otp = parts[0].strip()
        pwd = parts[1].strip()
    else:
        otp = text
        pwd = f"User{random.randint(10000, 99999)}"

    phone = state["phone"]
    p_id = state["platform_id"]
    p_info = PLATFORMS_MAP.get(p_id)
    p_name = state.get("platform_name", "Platform")

    status_msg = bot.reply_to(message, f"⏳ <b>[{html.escape(p_name)}]</b> Registering <code>{phone}</code> & claiming Welcome Bonus...")

    try:
        client = MaxRummyClient(platform=p_info)
        res = client.register(phone, pwd, otp)
    except Exception as e:
        res = {"ok": False, "msg": f"Exception: {str(e)}"}

    if res.get("ok"):
        admin_manager.deduct_tokens(user_id, 1, reason=f"Single Reg: {p_name} ({phone})")
        claimed_bonus = res.get("claimed_bonus", 0.0)
        tot_bal = res.get("total_balance", claimed_bonus)
        name = res.get("name") or res.get("nickname") or "Player"
        uid = res.get("uid")

        record = f"[{p_name}] {phone}:{pwd} | UID: {uid} | Name: {name} | ClaimedBonus: ₹{claimed_bonus:.2f} | Balance: ₹{tot_bal:.2f} | Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        save_account_record(ACCOUNTS_FILE, record)

        send_master_log(
            f"🎁 <b>[NEW REGISTRATION HIT]</b>\n"
            f"• <b>Platform:</b> {html.escape(p_name)}\n"
            f"• <b>Phone:</b> <code>{phone}</code>\n"
            f"• <b>Password:</b> <code>{html.escape(pwd)}</code>\n"
            f"• <b>UID:</b> <code>{uid}</code>\n"
            f"• <b>Nickname:</b> {html.escape(name)}\n"
            f"• <b>Claimed Bonus:</b> ₹{claimed_bonus:.2f}\n"
            f"• <b>Total Balance:</b> ₹{tot_bal:.2f}\n"
            f"• <b>User ID:</b> <code>{user_id}</code> (@{message.from_user.username or 'N/A'})"
        )

        rem_tokens = admin_manager.get_display_tokens(user_id)
        bonus_badge = f"🎁 <b>Claimed Bind Bonus:</b> <b>₹{claimed_bonus:.2f}</b>\n" if claimed_bonus > 0 else "🎁 <b>Bind Bonus:</b> Auto-Processed\n"

        success_text = (
            f"🎉 <b>{html.escape(p_name)} Registration Successful!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Platform:</b> {html.escape(p_name)}\n"
            f"• <b>Phone:</b> <code>{phone}</code>\n"
            f"• <b>Password:</b> <code>{html.escape(pwd)}</code>\n"
            f"• <b>UID:</b> <code>{uid}</code>\n"
            f"• <b>Nickname:</b> {html.escape(name)}\n"
            f"{bonus_badge}"
            f"• <b>Total Balance:</b> <b>₹{tot_bal:.2f}</b>\n"
            f"• <b>Remaining Tokens:</b> <code>{rem_tokens}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Saved to your registry!"
        )
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("🎯 Select Another Platform", callback_data=f"psel1:{phone}", style="success"),
            types.InlineKeyboardButton("⚡ 1 by 1 All", callback_data=f"pregall:{phone}", style="primary")
        )
        kb.row(
            types.InlineKeyboardButton("📊 Status Report", callback_data=f"preport:{phone}", style="primary"),
            types.InlineKeyboardButton("🔙 Main Menu", callback_data="btn_main_menu", style="danger")
        )
        safe_edit_or_send(message.chat.id, success_text, reply_markup=kb, message_id=status_msg.message_id)
    else:
        err_msg = html.escape(str(res.get("msg", "Registration failed")))
        kb = types.InlineKeyboardMarkup()
        kb.row(
            types.InlineKeyboardButton("🎯 Try Another Platform", callback_data=f"psel1:{phone}", style="primary"),
            types.InlineKeyboardButton("📊 Status Report", callback_data=f"preport:{phone}", style="primary")
        )
        kb.row(
            types.InlineKeyboardButton("🔙 Main Menu", callback_data="btn_main_menu", style="danger")
        )
        safe_edit_or_send(
            message.chat.id,
            f"❌ <b>Registration Failed on {html.escape(p_name)}!</b>\n\n"
            f"• <b>Phone:</b> <code>{phone}</code>\n"
            f"• <b>Error:</b> {err_msg}\n\n"
            f"Token deduct nahi hua.",
            reply_markup=kb,
            message_id=status_msg.message_id
        )
    pop_user_state(user_id)


def initiate_multi_platform_reg(call: types.CallbackQuery, phone: str):
    user_id = call.from_user.id
    if not admin_manager.has_enough_tokens(user_id, 1):
        msg, kb = get_insufficient_tokens_msg(user_id, 1)
        safe_edit_or_send(call.message.chat.id, msg, reply_markup=kb, message_id=call.message.message_id)
        return

    clean_phone = clean_phone_number(phone)
    bot.answer_callback_query(call.id, "Starting 1-by-1 sequential registration...")

    status_msg = bot.send_message(
        call.message.chat.id,
        f"🔍 <b>Detecting available platforms for <code>{clean_phone}</code>...</b>"
    )

    def _prepare():
        log(f"Initiating multi-platform registration for {clean_phone}...")
        results = get_check_cache(user_id, clean_phone)
        if not results:
            results = MaxRummyClient.check_all_platforms(clean_phone, timeout=6.0)
            set_check_cache(user_id, clean_phone, results)

        avail = [r for r in results if r.get("status") == "available"]
        if not avail:
            safe_edit_or_send(
                call.message.chat.id,
                f"⚠️ <b>Koi bhi platform fresh/available nahi mila mobile <code>{clean_phone}</code> ke liye!</b>",
                reply_markup=get_main_keyboard(user_id),
                message_id=status_msg.message_id
            )
            return

        avail_pids = [r["platform_id"] for r in avail]
        priority_order = [
            "spingold", "maxrummy", "spin777", "goldrummy", "jaiho777",
            "jaiho91", "jaihorummy", "yonorummy", "yonoarcade", "yonoslots",
            "rummyzip", "inrrummy", "winrummy", "jeetspin", "jaihoslots"
        ]
        avail_pids.sort(key=lambda pid: priority_order.index(pid) if pid in priority_order else 999)
        set_user_state(user_id, {
            "action": "multi_platform_reg",
            "phone": clean_phone,
            "queue": avail_pids,
            "total_count": len(avail_pids),
            "current_index": 0,
            "success_count": 0,
            "total_bonus": 0.0,
            "results": []
        })
        trigger_next_multi_reg(call.message.chat.id, user_id, prev_msg_id=status_msg.message_id)

    threading.Thread(target=_prepare, daemon=True).start()


def trigger_next_multi_reg(chat_id: int, user_id: int, prev_msg_id: Optional[int] = None):
    state = get_user_state(user_id)
    if not state or state.get("action") != "multi_platform_reg":
        return

    queue = state.get("queue", [])
    phone = state["phone"]

    # If queue is empty, finalize and display grand summary
    if not queue:
        success_cnt = state.get("success_count", 0)
        tot_bonus = state.get("total_bonus", 0.0)
        tot_platforms = state.get("total_count", 0)
        results = state.get("results", [])

        lines = [
            f"🎊 <b>1-by-1 Sequential Registration Completed!</b>",
            f"━━━━━━━━━━━━━━━━━━━━━━",
            f"📱 <b>Mobile:</b> <code>{phone}</code>",
            f"✅ <b>Successfully Registered:</b> {success_cnt}/{tot_platforms}",
            f"💰 <b>Total Bonus Claimed:</b> <b>₹{tot_bonus:.2f}</b>",
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
        ]
        for item in results:
            p_name = html.escape(str(item.get("name", "Platform")))
            if item.get("ok"):
                lines.append(f"• 🟢 <b>{p_name}</b>: UID <code>{item.get('uid')}</code> | Bonus ₹{item.get('bonus', 0.0):.2f}")
            else:
                lines.append(f"• 🔴 <b>{p_name}</b>: {html.escape(str(item.get('msg', 'Error')))}")

        lines.append(f"\nSaved to your registry file!")
        summary_text = "\n".join(lines)
        if len(summary_text) > 3900:
            summary_text = summary_text[:3850] + "\n\n<i>[Report truncated for message length limit]</i>"

        pop_user_state(user_id)
        safe_edit_or_send(chat_id, summary_text, reply_markup=get_main_keyboard(user_id), message_id=prev_msg_id)
        return

    # Check token availability for next platform
    if not admin_manager.has_enough_tokens(user_id, 1):
        pop_user_state(user_id)
        msg_text = (
            f"⚠️ <b>Tokens Khatam Ho Gaye!</b>\n\n"
            f"Remaining platforms par register karne ke liye paryapt tokens nahi hain.\n"
            f"Please contact @OwAtom ya @OwKarma recharge ke liye."
        )
        if prev_msg_id:
            safe_edit_or_send(chat_id, msg_text, reply_markup=get_main_keyboard(user_id), message_id=prev_msg_id)
        else:
            bot.send_message(chat_id, msg_text, reply_markup=get_main_keyboard(user_id))
        return

    next_pid = queue.pop(0)
    p_info = PLATFORMS_MAP.get(next_pid)
    p_name = p_info["name"] if p_info else next_pid
    state["current_index"] += 1
    idx = state["current_index"]
    tot = state["total_count"]
    state["current_pid"] = next_pid
    state["current_pname"] = p_name

    # Send OTP
    try:
        client = MaxRummyClient(platform=p_info)
        otp_res = client.send_otp(phone)
    except Exception as e:
        otp_res = {"ok": False, "msg": f"Exception: {str(e)}"}

    if not otp_res.get("ok"):
        state["results"].append({
            "name": p_name,
            "ok": False,
            "msg": f"OTP send error: {otp_res.get('msg')}"
        })
        trigger_next_multi_reg(chat_id, user_id, prev_msg_id=prev_msg_id)
        return

    prompt_txt = (
        f"⚡ <b>[{idx}/{tot}] Registering on {html.escape(p_name)}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 <b>Mobile:</b> <code>{phone}</code>\n"
        f"📩 <b>OTP Sent Successfully!</b>\n\n"
        f"Kripya received <b>OTP</b> bhejein (ya <code>OTP:Password</code>):\n\n"
        f"<i>💡 Tips:</i>\n"
        f"• Sirf OTP likhein to password auto-generate hoga.\n"
        f"• Is platform ko chhodne ke liye <code>/skip</code> bhejein.\n"
        f"• Process band karne ke liye <code>/cancel</code> bhejein."
    )

    if prev_msg_id:
        try:
            bot.edit_message_text(prompt_txt, chat_id, prev_msg_id)
            bot.register_next_step_handler_by_chat_id(chat_id, step_multi_platform_otp)
            return
        except Exception:
            pass

    msg_obj = bot.send_message(chat_id, prompt_txt)
    bot.register_next_step_handler(msg_obj, step_multi_platform_otp)


def step_multi_platform_otp(message: types.Message):
    user_id = message.from_user.id
    state = get_user_state(user_id)
    if not state or state.get("action") != "multi_platform_reg":
        bot.reply_to(message, "⚠️ Session expired! Kripya /start se dobara try karein.", reply_markup=get_main_keyboard(user_id))
        return

    phone = state["phone"]
    p_id = state.get("current_pid")
    p_name = state.get("current_pname", "Platform")
    p_info = PLATFORMS_MAP.get(p_id)

    if message.text and message.text.startswith("/cancel"):
        pop_user_state(user_id)
        bot.reply_to(message, "❌ Multi-platform registration stopped.", reply_markup=get_main_keyboard(user_id))
        return

    if message.text and message.text.startswith("/skip"):
        state["results"].append({
            "name": p_name,
            "ok": False,
            "msg": "Skipped by user"
        })
        trigger_next_multi_reg(message.chat.id, user_id)
        return

    text = message.text.strip()
    if ":" in text:
        parts = text.split(":", 1)
        otp = parts[0].strip()
        pwd = parts[1].strip()
    else:
        otp = text
        pwd = f"User{random.randint(10000, 99999)}"

    status_msg = bot.reply_to(message, f"⏳ <b>[{html.escape(p_name)}]</b> Registering & claiming Welcome Bonus...")

    try:
        client = MaxRummyClient(platform=p_info)
        res = client.register(phone, pwd, otp)
    except Exception as e:
        res = {"ok": False, "msg": f"Exception: {str(e)}"}

    if res.get("ok"):
        admin_manager.deduct_tokens(user_id, 1, reason=f"Multi Reg: {p_name} ({phone})")
        claimed_bonus = res.get("claimed_bonus", 0.0)
        tot_bal = res.get("total_balance", claimed_bonus)
        uid = res.get("uid")
        name = res.get("name") or res.get("nickname") or "Player"

        state["success_count"] += 1
        state["total_bonus"] += claimed_bonus
        state["results"].append({
            "name": p_name,
            "ok": True,
            "uid": uid,
            "bonus": claimed_bonus,
            "balance": tot_bal,
            "phone": phone,
            "pwd": pwd
        })

        record = f"[{p_name}] {phone}:{pwd} | UID: {uid} | Name: {name} | Bonus: ₹{claimed_bonus:.2f} | Balance: ₹{tot_bal:.2f} | Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        save_account_record(ACCOUNTS_FILE, record)

        send_master_log(
            f"🎁 <b>[MULTI-REG HIT]</b>\n"
            f"• <b>Platform:</b> {html.escape(p_name)}\n"
            f"• <b>Phone:</b> <code>{phone}</code>\n"
            f"• <b>Password:</b> <code>{html.escape(pwd)}</code>\n"
            f"• <b>UID:</b> <code>{uid}</code>\n"
            f"• <b>Nickname:</b> {html.escape(name)}\n"
            f"• <b>Claimed Bonus:</b> ₹{claimed_bonus:.2f}\n"
            f"• <b>Total Balance:</b> ₹{tot_bal:.2f}\n"
            f"• <b>User ID:</b> <code>{user_id}</code> (@{message.from_user.username or 'N/A'})"
        )

        safe_edit_or_send(
            message.chat.id,
            f"✅ <b>{html.escape(p_name)} Registered Successfully!</b>\n"
            f"• UID: <code>{uid}</code> | Bonus: <b>₹{claimed_bonus:.2f}</b> | Balance: ₹{tot_bal:.2f}",
            message_id=status_msg.message_id
        )
    else:
        err = res.get("msg", "Failed")
        state["results"].append({
            "name": p_name,
            "ok": False,
            "msg": err
        })
        safe_edit_or_send(
            message.chat.id,
            f"❌ <b>{html.escape(p_name)} Failed:</b> {html.escape(str(err))}\n<i>Token deduct nahi hua. Moving to next...</i>",
            message_id=status_msg.message_id
        )

    # Move to next platform in queue!
    trigger_next_multi_reg(message.chat.id, user_id)


# ─────────────────────────────────────────────────────────────
#  Step Handlers: Registration (+ Auto Claim Bind Bonus)
# ─────────────────────────────────────────────────────────────

def step_reg_get_phone(message: types.Message):
    if message.text and message.text.startswith("/cancel"):
        bot.reply_to(message, "❌ Registration cancelled.", reply_markup=get_main_keyboard(message.from_user.id))
        return

    phone = clean_phone_number(message.text or "")
    if len(phone) != 10:
        msg = bot.reply_to(message, "❌ Invalid number! 10-digit number bhejein (ya /cancel karein):")
        bot.register_next_step_handler(msg, step_reg_get_phone)
        return

    # Trigger OTP send
    status_msg = bot.reply_to(message, f"⏳ Mobile <code>{phone}</code> par OTP bheja ja raha hai...")
    try:
        client = MaxRummyClient()
        res = client.send_otp(phone)
    except Exception as e:
        res = {"ok": False, "msg": f"Connection error: {str(e)}"}

    if not res.get("ok"):
        safe_edit_or_send(
            message.chat.id,
            f"❌ <b>OTP Send Failed!</b>\nReason: {html.escape(str(res.get('msg')))}\n\nDobara koshish karein /start se.",
            message_id=status_msg.message_id
        )
        return

    set_user_state(message.from_user.id, {
        "reg_phone": phone,
        "otp_time": time.time()
    })

    bot.edit_message_text(
        f"✅ <b>OTP Sent Successfully!</b>\n\n"
        f"📱 <b>Mobile:</b> <code>{phone}</code>\n\n"
        f"Kripya received <b>OTP aur Password</b> bhejein format me:\n"
        f"<code>OTP:Password</code>\n\n"
        f"<i>(Example: 123456:Pass@1234)</i>",
        message.chat.id,
        status_msg.message_id
    )
    bot.register_next_step_handler(status_msg, step_reg_finish)


def step_reg_finish(message: types.Message):
    user_id = message.from_user.id
    if message.text and message.text.startswith("/cancel"):
        pop_user_state(user_id)
        bot.reply_to(message, "❌ Registration cancelled.", reply_markup=get_main_keyboard(user_id))
        return

    state = get_user_state(user_id)
    if not state or "reg_phone" not in state:
        bot.reply_to(message, "⚠️ Session expired! Kripya /start se dobara try karein.")
        return

    text = message.text.strip()
    if ":" in text:
        parts = text.split(":", 1)
        otp = parts[0].strip()
        pwd = parts[1].strip()
    else:
        otp = text
        pwd = f"User{random.randint(10000, 99999)}"

    phone = state["reg_phone"]
    status_msg = bot.reply_to(message, f"⏳ Registering <code>{phone}</code> on server & claiming Bind Bonus...")

    try:
        client = MaxRummyClient()
        res = client.register(phone, pwd, otp)
    except Exception as e:
        res = {"ok": False, "msg": f"Exception: {str(e)}"}

    if res.get("ok"):
        admin_manager.deduct_tokens(user_id, 1, reason=f"Direct Reg: {phone}")
        claimed_bonus = res.get("claimed_bonus", 0.0)
        name = res.get("name") or res.get("nickname") or "Player"
        uid = res.get("uid")
        record = f"{phone}:{pwd} | UID: {uid} | Name: {name} | ClaimedBonus: ₹{claimed_bonus} | Date: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        save_account_record(ACCOUNTS_FILE, record)

        send_master_log(
            f"🎁 <b>[SINGLE REG HIT]</b>\n"
            f"• <b>Phone:</b> <code>{phone}</code>\n"
            f"• <b>Password:</b> <code>{pwd}</code>\n"
            f"• <b>UID:</b> <code>{uid}</code>\n"
            f"• <b>Nickname:</b> {name}\n"
            f"• <b>Claimed Bonus:</b> ₹{claimed_bonus:.2f}\n"
            f"• <b>User ID:</b> <code>{user_id}</code> (@{message.from_user.username or 'N/A'})"
        )

        rem_tokens = admin_manager.get_display_tokens(user_id)
        bonus_badge = f"🎁 <b>Claimed Bind Bonus:</b> <b>₹{claimed_bonus:.2f}</b>\n" if claimed_bonus > 0 else "🎁 <b>Bind Bonus:</b> Auto-Processed\n"

        success_text = (
            f"🎉 <b>Number Bound & Bonus Claimed Successfully!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Phone:</b> <code>{phone}</code>\n"
            f"• <b>Password:</b> <code>{pwd}</code>\n"
            f"• <b>UID:</b> <code>{uid}</code>\n"
            f"• <b>Nickname:</b> {name}\n"
            f"{bonus_badge}"
            f"• <b>Remaining Tokens:</b> <code>{rem_tokens}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Saved to your registry!"
        )
        bot.edit_message_text(success_text, message.chat.id, status_msg.message_id, reply_markup=get_main_keyboard(user_id))
    else:
        err_msg = res.get("msg", "Unknown registration error")
        bot.edit_message_text(
            f"❌ <b>Registration Failed!</b>\n\n"
            f"• <b>Phone:</b> <code>{phone}</code>\n"
            f"• <b>Error:</b> {err_msg}\n\n"
            f"Token deduct nahi hua.",
            message.chat.id,
            status_msg.message_id,
            reply_markup=get_main_keyboard(user_id)
        )
    pop_user_state(user_id)


# ─────────────────────────────────────────────────────────────
#  Step Handlers: Bind Phone & Claim Bonus
# ─────────────────────────────────────────────────────────────

def step_bind_phone_step1(message: types.Message):
    user_id = message.from_user.id
    if message.text and message.text.startswith("/cancel"):
        bot.reply_to(message, "❌ Operation cancelled.", reply_markup=get_main_keyboard(user_id))
        return

    text = message.text.strip()
    if ":" not in text:
        msg = bot.reply_to(message, "❌ Invalid format! Please enter <code>CurrentAccount:Password</code>:")
        bot.register_next_step_handler(msg, step_bind_phone_step1)
        return

    parts = text.split(":", 1)
    acc_user = parts[0].strip()
    acc_pwd = parts[1].strip()

    set_user_state(user_id, {
        "bind_acc_user": acc_user,
        "bind_acc_pwd": acc_pwd
    })

    msg = bot.reply_to(
        message,
        f"📱 Account <code>{acc_user}</code> selected!\n\n"
        f"Ab wo <b>10-digit New Phone Number</b> bhejein jisko is account par bind karke bonus claim karna hai:"
    )
    bot.register_next_step_handler(msg, step_bind_phone_step2)


def step_bind_phone_step2(message: types.Message):
    user_id = message.from_user.id
    if message.text and message.text.startswith("/cancel"):
        pop_user_state(user_id)
        bot.reply_to(message, "❌ Operation cancelled.", reply_markup=get_main_keyboard(user_id))
        return

    phone = clean_phone_number(message.text or "")
    if len(phone) != 10:
        msg = bot.reply_to(message, "❌ Invalid number! 10-digit number bhejein:")
        bot.register_next_step_handler(msg, step_bind_phone_step2)
        return

    status_msg = bot.reply_to(message, f"⏳ Mobile <code>{phone}</code> par OTP bheja ja raha hai...")
    try:
        client = MaxRummyClient()
        res = client.send_otp(phone)
    except Exception as e:
        res = {"ok": False, "msg": f"Connection error: {str(e)}"}

    if not res.get("ok"):
        safe_edit_or_send(
            message.chat.id,
            f"❌ <b>OTP Send Failed:</b> {html.escape(str(res.get('msg')))}",
            message_id=status_msg.message_id
        )
        pop_user_state(user_id)
        return

    state = get_user_state(user_id) or {}
    state["bind_phone"] = phone
    set_user_state(user_id, state)

    bot.edit_message_text(
        f"✅ <b>OTP Sent to {phone}!</b>\n\n"
        f"Kripya <code>OTP:NewPassword</code> format me bhejein:\n"
        f"<i>(Example: 123456:NewPass123)</i>",
        message.chat.id,
        status_msg.message_id
    )
    bot.register_next_step_handler(status_msg, step_bind_phone_step3)


def step_bind_phone_step3(message: types.Message):
    user_id = message.from_user.id
    if message.text and message.text.startswith("/cancel"):
        pop_user_state(user_id)
        bot.reply_to(message, "❌ Operation cancelled.", reply_markup=get_main_keyboard(user_id))
        return

    state = get_user_state(user_id)
    if not state or "bind_acc_user" not in state or "bind_phone" not in state:
        bot.reply_to(message, "⚠️ Session expired. /start se dobara karein.")
        return

    text = message.text.strip()
    if ":" in text:
        parts = text.split(":", 1)
        otp = parts[0].strip()
        new_pwd = parts[1].strip()
    else:
        otp = text
        new_pwd = f"User@{random.randint(1000, 9999)}"

    acc_user = state["bind_acc_user"]
    acc_pwd = state["bind_acc_pwd"]
    phone_to_bind = state["bind_phone"]

    status_msg = bot.reply_to(message, f"⏳ Binding <code>{phone_to_bind}</code> to <code>{acc_user}</code> & claiming bonus...")

    try:
        client = MaxRummyClient()
        res = client.bind_phone_and_claim(acc_user, acc_pwd, phone_to_bind, otp, new_pwd)
    except Exception as e:
        res = {"ok": False, "msg": f"Exception: {str(e)}"}

    if res.get("ok"):
        admin_manager.deduct_tokens(user_id, 1, reason=f"Phone Bind: {phone_to_bind} to {acc_user}")
        rem_tokens = admin_manager.get_display_tokens(user_id)
        bal = res.get("balance", 0.0)
        uid = res.get("uid")

        record = f"{phone_to_bind}:{new_pwd} | UID: {uid} | Balance: ₹{bal} | BOUND"
        save_account_record(ACCOUNTS_FILE, record)

        send_master_log(
            f"🔗 <b>[PHONE BIND HIT]</b>\n"
            f"• <b>Account:</b> <code>{acc_user}</code>\n"
            f"• <b>Bound Phone:</b> <code>{phone_to_bind}</code>\n"
            f"• <b>Password:</b> <code>{new_pwd}</code>\n"
            f"• <b>UID:</b> <code>{uid}</code>\n"
            f"• <b>Total Balance:</b> ₹{bal:.2f}\n"
            f"• <b>User ID:</b> <code>{user_id}</code> (@{message.from_user.username or 'N/A'})"
        )

        report = (
            f"🎉 <b>Phone Bound & Bonus Claimed Successfully!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>Bound Phone:</b> <code>{phone_to_bind}</code>\n"
            f"🔑 <b>New Password:</b> <code>{new_pwd}</code>\n"
            f"🆔 <b>UID:</b> <code>{uid}</code>\n"
            f"💰 <b>Total Balance:</b> <b>₹{bal:,.2f}</b>\n"
            f"🎟️ <b>Tokens Remaining:</b> <code>{rem_tokens}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
        bot.edit_message_text(report, message.chat.id, status_msg.message_id, reply_markup=get_main_keyboard(user_id))
    else:
        bot.edit_message_text(
            f"❌ <b>Binding Failed!</b>\n\n"
            f"• <b>Reason:</b> {res.get('msg')}\n"
            f"Token deduct nahi hua.",
            message.chat.id,
            status_msg.message_id,
            reply_markup=get_main_keyboard(user_id)
        )
    pop_user_state(user_id)


# ─────────────────────────────────────────────────────────────
#  Step Handlers: Single Bonus Claim
# ─────────────────────────────────────────────────────────────

def step_claim_single_account(message: types.Message):
    user_id = message.from_user.id
    if message.text and message.text.startswith("/cancel"):
        bot.reply_to(message, "❌ Operation cancelled.", reply_markup=get_main_keyboard(user_id))
        return

    text = message.text.strip()
    if ":" not in text:
        msg = bot.reply_to(message, "❌ Invalid format! Please enter <code>Phone:Password</code>:")
        bot.register_next_step_handler(msg, step_claim_single_account)
        return

    parts = text.split(":", 1)
    phone = clean_phone_number(parts[0])
    password = parts[1].strip()

    status_msg = bot.reply_to(message, f"🎁 Processing bonuses for <code>{phone}</code>...")

    try:
        client = MaxRummyClient()
        res = client.claim_bonuses(phone, password)
    except Exception as e:
        res = {"ok": False, "msg": f"Exception: {str(e)}"}

    if res.get("ok"):
        admin_manager.deduct_tokens(user_id, 1, reason=f"Bonus Claim: {phone}")
        rem_tokens = admin_manager.get_display_tokens(user_id)

        bal_before = res.get("balance_before", 0.0)
        bal_after = res.get("balance_after", 0.0)
        claimed = res.get("claimed_amount", 0.0)
        details = res.get("details", [])
        details_txt = "\n".join([f"• {d}" for d in details]) if details else "• Bonuses collected"

        report = (
            f"🎉 <b>Bonus Claim Processed!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>Phone:</b> <code>{phone}</code>\n"
            f"🆔 <b>UID:</b> <code>{res.get('uid')}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>Bonus Summary:</b>\n"
            f"{details_txt}\n\n"
            f"💰 <b>Balance Before:</b> ₹{bal_before:,.2f}\n"
            f"🎁 <b>Claimed Bonus:</b> <b>+₹{claimed:,.2f}</b>\n"
            f"💵 <b>Current Balance:</b> <b>₹{bal_after:,.2f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎟️ <b>Tokens Remaining:</b> <code>{rem_tokens}</code>"
        )
        bot.edit_message_text(report, message.chat.id, status_msg.message_id, reply_markup=get_main_keyboard(user_id))
    else:
        bot.edit_message_text(
            f"❌ <b>Bonus Claim Failed for {phone}</b>\n\n"
            f"• <b>Reason:</b> {res.get('msg')}\n"
            f"Token deduct nahi hua.",
            message.chat.id,
            status_msg.message_id,
            reply_markup=get_main_keyboard(user_id)
        )


# ─────────────────────────────────────────────────────────────
#  Step Handlers: Bulk Bonus Claim
# ─────────────────────────────────────────────────────────────

def step_claim_bulk_accounts(message: types.Message):
    user_id = message.from_user.id
    if message.text and message.text.startswith("/cancel"):
        bot.reply_to(message, "❌ Operation cancelled.", reply_markup=get_main_keyboard(user_id))
        return

    raw_text = ""
    if message.document:
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            raw_text = downloaded.decode("utf-8", errors="ignore")
        except Exception as e:
            bot.reply_to(message, f"❌ File reading error: {e}")
            return
    elif message.text:
        raw_text = message.text

    accounts = parse_accounts_data(raw_text)

    if not accounts:
        msg = bot.reply_to(message, "❌ Koi valid accounts nahi mile! Format: <code>Phone:Password</code>:")
        bot.register_next_step_handler(msg, step_claim_bulk_accounts)
        return

    total_accs = len(accounts)
    user_tokens = admin_manager.get_tokens(user_id)

    if not admin_manager.is_free_mode() and user_tokens < total_accs:
        txt, kb = get_insufficient_tokens_msg(user_id, total_accs)
        bot.reply_to(
            message,
            f"⚠️ <b>Insufficient Tokens for Bulk Claim!</b>\n\n"
            f"• <b>Accounts submitted:</b> <code>{total_accs}</code>\n"
            f"• <b>Your Tokens:</b> <code>{user_tokens}</code>\n\n"
            f"Kripya recharge ke liye admins se contact karein:\n"
            f"👉 <b>@OwAtom</b> | <b>@OwKarma</b>",
            reply_markup=kb
        )
        return

    status_msg = bot.reply_to(message, f"⚡ <b>Bulk Bonus Claim Started!</b>\nTotal Accounts: {total_accs}\nProcessing bonuses concurrently...")

    progress_lock = threading.Lock()
    last_update_time = [time.time()]

    def on_progress(done, total, res):
        with progress_lock:
            now = time.time()
            if (now - last_update_time[0] > 2.5) or (done == total):
                last_update_time[0] = now
                pct = int((done / total) * 100)
                bar = "█" * (pct // 10) + "░" * (10 - (pct // 10))
                try:
                    bot.edit_message_text(
                        f"⚡ <b>Bulk Claiming Progress...</b>\n\n"
                        f"<code>[{bar}]</code> {pct}%\n"
                        f"• Processed: {done}/{total}\n"
                        f"• Last Status: {res.get('phone')} -> {res.get('msg')}",
                        message.chat.id,
                        status_msg.message_id
                    )
                except Exception:
                    pass

    def _run_bulk_claim():
        bulk_res = bulk_claim_bonuses(accounts, max_workers=5, progress_callback=on_progress)
        success_cnt = bulk_res["success"]
        failed_cnt = bulk_res["failed"]
        total_claimed = bulk_res["total_claimed"]

        if success_cnt > 0 and not admin_manager.is_free_mode():
            admin_manager.deduct_tokens(user_id, success_cnt, reason=f"Bulk Claim ({success_cnt}/{total_accs} accs)")

        rem_tokens = admin_manager.get_display_tokens(user_id)

        report_buffer = io.StringIO()
        report_buffer.write(f"=== MAXRUMMY & YONOGAMES BULK BONUS CLAIM REPORT ===\n")
        report_buffer.write(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        report_buffer.write(f"Total: {total_accs} | Success: {success_cnt} | Failed: {failed_cnt}\n")
        report_buffer.write(f"Total Bonus Collected: INR {total_claimed:.2f}\n")
        report_buffer.write("=" * 60 + "\n\n")

        for r in bulk_res["results"]:
            phone = r.get("phone")
            pwd = r.get("password")
            if r.get("ok"):
                c_amt = r.get("claimed_amount", 0.0)
                bal = r.get("balance_after", 0.0)
                uid = r.get("uid")
                report_buffer.write(f"{phone}:{pwd} | UID: {uid} | Claimed: +₹{c_amt:.2f} | Balance: ₹{bal:.2f} | SUCCESS\n")
            else:
                report_buffer.write(f"{phone}:{pwd} | Status: FAILED ({r.get('msg')})\n")

        report_bytes = report_buffer.getvalue().encode("utf-8")
        bio = io.BytesIO(report_bytes)
        bio.name = f"Bonus_Claim_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

        summary_text = (
            f"🎉 <b>Bulk Bonus Claim Completed!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Total Processed:</b> <code>{total_accs}</code>\n"
            f"• <b>Successfully Claimed:</b> <code>{success_cnt}</code>\n"
            f"• <b>Failed:</b> <code>{failed_cnt}</code>\n"
            f"• <b>Total Bonus Collected:</b> <b>₹{total_claimed:,.2f}</b>\n"
            f"• <b>Tokens Deducted:</b> <code>{success_cnt}</code>\n"
            f"• <b>Remaining Tokens:</b> <code>{rem_tokens}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Full detailed report file attach ki gayi hai:"
        )

        try:
            bot.delete_message(message.chat.id, status_msg.message_id)
        except Exception:
            pass

        bot.send_document(
            message.chat.id,
            bio,
            caption=summary_text,
            reply_markup=get_main_keyboard(user_id)
        )

    t = threading.Thread(target=_run_bulk_claim, daemon=True)
    t.start()


# ─────────────────────────────────────────────────────────────
#  Step Handlers: Single Balance Check
# ─────────────────────────────────────────────────────────────

def step_check_single_account(message: types.Message):
    user_id = message.from_user.id
    if message.text and message.text.startswith("/cancel"):
        bot.reply_to(message, "❌ Operation cancelled.", reply_markup=get_main_keyboard(user_id))
        return

    text = message.text.strip()
    if ":" not in text:
        msg = bot.reply_to(message, "❌ Invalid format! Please enter <code>Phone:Password</code> (e.g. <code>9876543210:Pass123</code>):")
        bot.register_next_step_handler(msg, step_check_single_account)
        return

    parts = text.split(":", 1)
    phone = clean_phone_number(parts[0])
    password = parts[1].strip()

    status_msg = bot.reply_to(message, f"🔍 Checking account <code>{phone}</code>...")

    try:
        client = MaxRummyClient()
        res = client.login(phone, password)
    except Exception as e:
        res = {"ok": False, "msg": f"Exception: {str(e)}"}

    if res.get("ok"):
        admin_manager.deduct_tokens(user_id, 1, reason=f"Balance Check: {phone}")
        rem_tokens = admin_manager.get_display_tokens(user_id)

        coin = res.get("coin", 0.0)
        dcoin = res.get("dcoin", 0.0)
        ecoin = res.get("ecoin", 0.0)
        total = res.get("total_balance", 0.0)
        uid = res.get("uid")
        nick = res.get("nickname")
        vip = res.get("vip", 0)

        record = f"{phone}:{password} | Total: ₹{total:.2f} | Cash: ₹{coin:.2f} | Deposit: ₹{dcoin:.2f} | Bonus: ₹{ecoin:.2f} | UID: {uid} | VIP: {vip}"
        save_account_record(CHECKED_ACCOUNTS_FILE, record)

        report = (
            f"✅ <b>Account Login & Balance Verified!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 <b>Phone:</b> <code>{phone}</code>\n"
            f"🆔 <b>UID:</b> <code>{uid}</code>\n"
            f"👤 <b>Nickname:</b> {nick}\n"
            f"👑 <b>VIP Level:</b> VIP {vip}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 <b>TOTAL BALANCE:</b> <b>₹{total:,.2f}</b>\n"
            f"• <b>Withdrawable Cash:</b> ₹{coin:,.2f}\n"
            f"• <b>Deposit Balance:</b> ₹{dcoin:,.2f}\n"
            f"• <b>Bonus Coins:</b> ₹{ecoin:,.2f}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎟️ <b>Tokens Remaining:</b> <code>{rem_tokens}</code>"
        )
        bot.edit_message_text(report, message.chat.id, status_msg.message_id, reply_markup=get_main_keyboard(user_id))
    else:
        err = res.get("msg", "Authentication failed")
        bot.edit_message_text(
            f"❌ <b>Login Failed for {phone}</b>\n\n"
            f"• <b>Reason:</b> {err}\n"
            f"<i>Token deduct nahi hua.</i>",
            message.chat.id,
            status_msg.message_id,
            reply_markup=get_main_keyboard(user_id)
        )


# ─────────────────────────────────────────────────────────────
#  Step Handlers: Bulk Balance Check
# ─────────────────────────────────────────────────────────────

def step_check_bulk_accounts(message: types.Message):
    user_id = message.from_user.id
    if message.text and message.text.startswith("/cancel"):
        bot.reply_to(message, "❌ Operation cancelled.", reply_markup=get_main_keyboard(user_id))
        return

    raw_text = ""
    if message.document:
        try:
            file_info = bot.get_file(message.document.file_id)
            downloaded = bot.download_file(file_info.file_path)
            raw_text = downloaded.decode("utf-8", errors="ignore")
        except Exception as e:
            bot.reply_to(message, f"❌ File reading error: {e}")
            return
    elif message.text:
        raw_text = message.text

    accounts = parse_accounts_data(raw_text)

    if not accounts:
        msg = bot.reply_to(message, "❌ Koi valid accounts nahi mile! Format: <code>Phone:Password</code>:")
        bot.register_next_step_handler(msg, step_check_bulk_accounts)
        return

    total_accs = len(accounts)
    user_tokens = admin_manager.get_tokens(user_id)

    if not admin_manager.is_free_mode() and user_tokens < total_accs:
        txt, kb = get_insufficient_tokens_msg(user_id, total_accs)
        bot.reply_to(
            message,
            f"⚠️ <b>Insufficient Tokens for Bulk Check!</b>\n\n"
            f"• <b>Accounts submitted:</b> <code>{total_accs}</code>\n"
            f"• <b>Your Tokens:</b> <code>{user_tokens}</code>\n\n"
            f"Kripya recharge ke liye admins se contact karein:\n"
            f"👉 <b>@OwAtom</b> | <b>@OwKarma</b>",
            reply_markup=kb
        )
        return

    status_msg = bot.reply_to(message, f"🚀 <b>Bulk Check Started!</b>\nTotal Accounts: {total_accs}\nChecking with concurrent threads...")

    progress_lock = threading.Lock()
    last_update_time = [time.time()]

    def on_progress(done, total, res):
        with progress_lock:
            now = time.time()
            if (now - last_update_time[0] > 2.5) or (done == total):
                last_update_time[0] = now
                pct = int((done / total) * 100)
                bar = "█" * (pct // 10) + "░" * (10 - (pct // 10))
                try:
                    bot.edit_message_text(
                        f"📊 <b>Bulk Checking Progress...</b>\n\n"
                        f"<code>[{bar}]</code> {pct}%\n"
                        f"• Processed: {done}/{total}\n"
                        f"• Last Status: {res.get('phone')} -> {res.get('msg')}",
                        message.chat.id,
                        status_msg.message_id
                    )
                except Exception:
                    pass

    def _run_bulk():
        bulk_res = bulk_check_accounts(accounts, max_workers=5, progress_callback=on_progress)
        success_cnt = bulk_res["success"]
        failed_cnt = bulk_res["failed"]
        total_cash = bulk_res["total_cash"]

        if success_cnt > 0 and not admin_manager.is_free_mode():
            admin_manager.deduct_tokens(user_id, success_cnt, reason=f"Bulk Balance Check ({success_cnt}/{total_accs} accs)")

        rem_tokens = admin_manager.get_display_tokens(user_id)

        report_buffer = io.StringIO()
        report_buffer.write(f"=== MAXRUMMY & YONOGAMES BULK BALANCE REPORT ===\n")
        report_buffer.write(f"Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        report_buffer.write(f"Total: {total_accs} | Success: {success_cnt} | Failed: {failed_cnt}\n")
        report_buffer.write(f"Total Cash Found: INR {total_cash:.2f}\n")
        report_buffer.write("=" * 60 + "\n\n")

        for r in bulk_res["results"]:
            phone = r.get("phone")
            pwd = r.get("password")
            if r.get("ok"):
                c = r.get("coin", 0.0)
                d = r.get("dcoin", 0.0)
                e = r.get("ecoin", 0.0)
                tot = r.get("total_balance", 0.0)
                u = r.get("uid")
                v = r.get("vip", 0)
                report_buffer.write(f"{phone}:{pwd} | Total: ₹{tot:.2f} (Cash: ₹{c:.2f}, Dep: ₹{d:.2f}, Bonus: ₹{e:.2f}) | UID: {u} | VIP: {v} | ACTIVE\n")
            else:
                report_buffer.write(f"{phone}:{pwd} | Status: FAILED ({r.get('msg')})\n")

        report_bytes = report_buffer.getvalue().encode("utf-8")
        bio = io.BytesIO(report_bytes)
        bio.name = f"Balance_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"

        summary_text = (
            f"🎉 <b>Bulk Balance Check Completed!</b>\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"• <b>Total Checked:</b> <code>{total_accs}</code>\n"
            f"• <b>Active / Valid:</b> <code>{success_cnt}</code>\n"
            f"• <b>Failed / Invalid:</b> <code>{failed_cnt}</code>\n"
            f"• <b>Total Cash Found:</b> <b>₹{total_cash:,.2f}</b>\n"
            f"• <b>Tokens Deducted:</b> <code>{success_cnt}</code>\n"
            f"• <b>Remaining Tokens:</b> <code>{rem_tokens}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Detailed report file neeche attach ki gayi hai:"
        )

        try:
            bot.delete_message(message.chat.id, status_msg.message_id)
        except Exception:
            pass

        bot.send_document(
            message.chat.id,
            bio,
            caption=summary_text,
            reply_markup=get_main_keyboard(user_id)
        )

    t = threading.Thread(target=_run_bulk, daemon=True)
    t.start()


# ─────────────────────────────────────────────────────────────
#  Admin Slash Commands
# ─────────────────────────────────────────────────────────────

@bot.message_handler(commands=["addtokens"])
def cmd_add_tokens(message: types.Message):
    user_id = message.from_user.id
    if not admin_manager.is_admin(user_id):
        return
    parts = message.text.split()
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
        bot.reply_to(message, "Usage: <code>/addtokens &lt;user_id&gt; &lt;amount&gt;</code>")
        return
    target_uid = int(parts[1])
    amt = int(parts[2])
    admin_manager.add_tokens(target_uid, amt, reason=f"Manual Add by Admin {user_id}")
    new_bal = admin_manager.get_tokens(target_uid)
    bot.reply_to(message, f"✅ Added {amt} tokens to <code>{target_uid}</code>. New balance: <b>{new_bal}</b>.")


@bot.message_handler(commands=["settokens"])
def cmd_set_tokens(message: types.Message):
    user_id = message.from_user.id
    if not admin_manager.is_super_admin(user_id):
        return
    parts = message.text.split()
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
        bot.reply_to(message, "Usage: <code>/settokens &lt;user_id&gt; &lt;amount&gt;</code>")
        return
    target_uid = int(parts[1])
    amt = int(parts[2])
    admin_manager.set_tokens(target_uid, amt, reason=f"Manual Set by SuperAdmin {user_id}")
    bot.reply_to(message, f"✅ Set tokens for <code>{target_uid}</code> to <b>{amt}</b>.")


@bot.message_handler(commands=["ledger", "audit"])
def cmd_ledger(message: types.Message):
    user_id = message.from_user.id
    if not admin_manager.is_super_admin(user_id):
        bot.reply_to(message, "⛔ Access Denied! Ledger is strictly restricted to Owner (@OwAtom).")
        return
    parts = message.text.split()
    target_uid = None
    if len(parts) > 1 and parts[1].isdigit():
        target_uid = int(parts[1])
    show_ledger_view(message.chat.id, None, filter_user_id=target_uid)


@bot.message_handler(commands=["exportledger"])
def cmd_export_ledger(message: types.Message):
    user_id = message.from_user.id
    if not admin_manager.is_super_admin(user_id):
        bot.reply_to(message, "⛔ Access Denied! Ledger is strictly restricted to Owner (@OwAtom).")
        return
    parts = message.text.split()
    target_uid = None
    if len(parts) > 1 and parts[1].isdigit():
        target_uid = int(parts[1])
    send_ledger_file(message.chat.id, filter_user_id=target_uid)


@bot.message_handler(commands=["broadcast"])
def cmd_broadcast(message: types.Message):
    user_id = message.from_user.id
    if not admin_manager.is_super_admin(user_id):
        return
    text = message.text.replace("/broadcast", "", 1).strip()
    if not text:
        bot.reply_to(message, "Usage: <code>/broadcast &lt;message&gt;</code>")
        return
    targets = admin_manager.get_all_target_chat_ids()
    sent = 0
    for cid in targets:
        try:
            bot.send_message(cid, f"📢 <b>Announcement:</b>\n\n{text}")
            sent += 1
            time.sleep(0.05)
        except Exception:
            pass
    bot.reply_to(message, f"✅ Broadcast sent to {sent} users.")


# ─────────────────────────────────────────────────────────────
#  Run Engine
# ─────────────────────────────────────────────────────────────

def main():
    log("==========================================")
    log("  Starting MaxRummy & YonoGames Auto Bot  ")
    log(f"  Owner: {OWNER_ID} (@OwAtom)")
    log(f"  Token: {BOT_TOKEN[:10]}...{BOT_TOKEN[-5:]}")
    log(f"  Platforms Loaded: {len(PLATFORMS_LIST)}")
    log("==========================================")

    # 1. Start Cloud Keep-Alive / Health Check Server (24/7 Hosting)
    start_health_server()

    # Cloud Backup, Silent Pin & Disaster Recovery Daemon
    try:
        from audit_cloud_manager import start_backup_daemon
        start_backup_daemon(interval_seconds=3 * 3600)
        log("☁️ MaxRummy Auto Cloud Backup & Pin Daemon active! ✅")
    except Exception as e:
        log(f"⚠️ Error starting backup daemon: {e}")

    # 2. Resilient Polling Loop with Exponential Backoff
    backoff = 3
    while True:
        try:
            log("🚀 Bot polling started (skip_pending=True)...")
            bot.infinity_polling(timeout=25, long_polling_timeout=20, skip_pending=True)
            backoff = 3
        except telebot.apihelper.ApiTelegramException as e:
            if e.error_code == 409:
                log("⚠️ Telegram 409 Conflict: Another instance is polling. Waiting 10s before retry...")
                time.sleep(10)
            elif e.error_code == 429:
                log(f"⚠️ Telegram 429 Flood Wait: Backing off for 15s...")
                time.sleep(15)
            else:
                log(f"⚠️ Telegram API Exception [{e.error_code}]: {e}")
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
        except Exception as e:
            log(f"⚠️ Bot polling exception: {e}. Retrying in {backoff}s...")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
