#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Autonomous Multi-Platform Registration with Firebase Realtime OTP Forwarding.
Sequentially scans available Yono platforms for a phone number, triggers OTP,
auto-fetches incoming OTP SMS from Firebase RTDB, registers the account,
claims all bonuses, and logs records to registered_accounts.txt.
"""

import os
import sys
import time
import json
import re
import argparse
import asyncio
from datetime import datetime
from pathlib import Path
import requests

BASE_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(BASE_DIR))

from maxrummy_client import MaxRummyClient, clean_phone_number, PLATFORMS_MAP, PLATFORMS_LIST
import config

DEFAULT_FIREBASE_DB = "https://chuchilora-c69b2-default-rtdb.firebaseio.com"
DEFAULT_DEVICE_ID = "df69a29e149c7e75"
DEFAULT_PHONE = "8000723437"
ACCOUNTS_FILE = BASE_DIR / "registered_accounts.txt"


def extract_otp_from_sms(body: str) -> str:
    """Extract 4-6 digit OTP from message body."""
    if not body:
        return ""
    # Look for OTP near keyword
    m = re.search(r"(?:otp|code|pin|verification).*?\b(\d{4,6})\b", body, re.IGNORECASE)
    if m:
        return m.group(1)
    # Generic 4-6 digit match
    m = re.search(r"\b(\d{4,6})\b", body)
    if m:
        return m.group(1)
    return ""


def get_latest_firebase_msg_id(session: requests.Session, db_url: str, device_id: str) -> int:
    """Fetch the latest integer message ID from Firebase."""
    try:
        url = f"{db_url}/messages/{device_id}.json?shallow=true"
        r = session.get(url, timeout=6)
        if r.status_code == 200 and r.json():
            keys = [int(k) for k in r.json().keys() if str(k).isdigit()]
            if keys:
                return max(keys)
    except Exception as e:
        print(f"⚠️ Warning fetching latest key: {e}")
    return 0


def poll_firebase_for_otp(session: requests.Session, db_url: str, device_id: str, checkpoint_id: int, timeout: int = 30) -> tuple:
    """
    Polls Firebase for any new SMS message with ID > checkpoint_id.
    Returns (otp, sms_body, sender) or ("", "", "").
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            url = f"{db_url}/messages/{device_id}.json"
            r = session.get(url, timeout=6)
            if r.status_code == 200 and r.json():
                msgs = r.json()
                new_keys = [int(k) for k in msgs.keys() if str(k).isdigit() and int(k) > checkpoint_id]
                if new_keys:
                    new_keys.sort()
                    for k in new_keys:
                        m_data = msgs.get(str(k)) or msgs.get(k)
                        if isinstance(m_data, dict):
                            body = m_data.get("message", "")
                            sender = m_data.get("sender", "")
                            otp = extract_otp_from_sms(body)
                            if otp:
                                return otp, body, sender
        except Exception as e:
            pass
        time.sleep(1.5)
    return "", "", ""


async def run_auto_pipeline(phone: str, db_url: str, device_id: str, max_platforms: int = 0):
    clean_p = clean_phone_number(phone)
    if len(clean_p) != 10:
        print(f"❌ Invalid mobile number: {phone}")
        return

    print("=" * 65)
    print("   AUTONOMOUS FIREBASE MULTI-PLATFORM REGISTRATION ENGINE   ")
    print("=" * 65)
    print(f"📱 Target Mobile: {clean_p}")
    print(f"🔥 Firebase DB  : {db_url}")
    print(f"📲 Device ID    : {device_id}")
    print("=" * 65)

    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 AutoRegistrar/2.0"

    # Step 1: Scan fresh platforms
    print(f"\n🔍 Scanning all {len(PLATFORMS_LIST)} Yono platforms for fresh status...")
    results = await MaxRummyClient.check_all_platforms_async(clean_p, timeout=5.0)
    avail = [r for r in results if r.get("status") == "available"]

    if not avail:
        print("❌ No fresh platforms available for this phone number.")
        return

    priority_order = [
        "spingold", "maxrummy", "spin777", "goldrummy", "jaiho777",
        "jaiho91", "jaihorummy", "yonorummy", "yonoarcade", "yonoslots",
        "rummyzip", "inrrummy", "winrummy", "jeetspin", "jaihoslots"
    ]
    avail.sort(key=lambda x: priority_order.index(x["platform_id"]) if x["platform_id"] in priority_order else 999)

    if max_platforms > 0:
        avail = avail[:max_platforms]

    print(f"✅ Found {len(avail)} Fresh Platforms Ready to Register:")
    for idx, a in enumerate(avail, start=1):
        print(f"   {idx}. {a['name']} ({a['platform_id']})")

    stats = {
        "success": 0,
        "failed": 0,
        "total_bonus": 0.0,
        "records": []
    }

    # Default base password
    pwd = f"User@{clean_p[-4:]}{int(time.time()) % 1000:03d}"

    print("\n🚀 Starting Autonomous Registration Loop...\n")

    for idx, item in enumerate(avail, start=1):
        pid = item["platform_id"]
        p_info = PLATFORMS_MAP.get(pid, item)
        p_name = p_info["name"]

        print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        print(f"[{idx}/{len(avail)}] 🎰 Platform: {p_name} ({pid})")

        # 1. Checkpoint current Firebase message ID
        ckpt_id = get_latest_firebase_msg_id(s, db_url, device_id)
        print(f"   📍 Firebase Checkpoint ID: {ckpt_id}")

        client = MaxRummyClient(platform=p_info)

        # 2. Trigger send_otp
        print(f"   📲 Sending OTP to {clean_p}...")
        try:
            otp_res = client.send_otp(clean_p, otptype=6)
        except Exception as e:
            otp_res = {"ok": False, "msg": str(e)}

        if not otp_res.get("ok"):
            print(f"   ❌ Send OTP Failed: {otp_res.get('msg')}")
            stats["failed"] += 1
            time.sleep(1)
            continue

        print(f"   ✅ OTP sent via {p_name}! Polling Firebase for incoming SMS...")

        # 3. Poll Firebase for OTP
        otp, sms_body, sender = poll_firebase_for_otp(s, db_url, device_id, ckpt_id, timeout=25)
        if not otp:
            print(f"   ⏳ OTP poll timed out (no SMS received within 25s). Skipping...")
            stats["failed"] += 1
            continue

        print(f"   🔑 OTP Captured from [{sender}]: {otp}")

        # 4. Register account & claim bonus
        print(f"   ⚡ Registering account with password: {pwd}...")
        try:
            reg_res = await client.register_async(clean_p, pwd, otp)
        except Exception as e:
            reg_res = {"ok": False, "msg": str(e)}

        if reg_res.get("ok"):
            claimed_bonus = float(reg_res.get("claimed_bonus", 0.0))
            tot_bal = float(reg_res.get("total_balance", claimed_bonus))
            uid = reg_res.get("uid")
            p_player = reg_res.get("name") or reg_res.get("nickname") or "Player"

            stats["success"] += 1
            stats["total_bonus"] += claimed_bonus

            rec = f"[{p_name}] {clean_p}:{pwd} | UID: {uid} | Bonus: ₹{claimed_bonus:.2f} | Bal: ₹{tot_bal:.2f} | {datetime.now().strftime('%Y-%m-%d %H:%M')}"
            stats["records"].append(rec)

            with open(ACCOUNTS_FILE, "a", encoding="utf-8") as f:
                f.write(rec + "\n")

            print(f"   🎉 SUCCESS! UID: {uid} | Bonus: ₹{claimed_bonus:.2f} | Total Bal: ₹{tot_bal:.2f}")
        else:
            err = reg_res.get("msg", "Unknown error")
            print(f"   ❌ Registration Failed: {err}")
            stats["failed"] += 1

        # Small delay between platforms
        await asyncio.sleep(1.5)

    print("\n" + "=" * 65)
    print("              FINAL REGISTRATION SUMMARY                 ")
    print("=" * 65)
    print(f"📱 Phone Number        : {clean_p}")
    print(f"✅ Successful Accounts : {stats['success']}/{len(avail)}")
    print(f"❌ Failed / Skipped    : {stats['failed']}")
    print(f"💰 Total Bonus Earned  : ₹{stats['total_bonus']:.2f}")
    print(f"📁 Saved To File       : {ACCOUNTS_FILE}")
    print("=" * 65)
    for r in stats["records"]:
        print(f" • {r}")
    print("=" * 65)


def main():
    parser = argparse.ArgumentParser(description="Auto Firebase Multi-Platform Registrar")
    parser.add_argument("--phone", "-p", default=DEFAULT_PHONE, help="Target 10-digit mobile number")
    parser.add_argument("--db", default=DEFAULT_FIREBASE_DB, help="Firebase RTDB URL")
    parser.add_argument("--device", "-d", default=DEFAULT_DEVICE_ID, help="Firebase Device ID")
    parser.add_argument("--max", "-m", type=int, default=0, help="Max platforms to register (0 for all)")

    args = parser.parse_args()
    asyncio.run(run_auto_pipeline(args.phone, args.db, args.device, max_platforms=args.max))


if __name__ == "__main__":
    main()
