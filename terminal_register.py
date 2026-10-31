#!/usr/bin/env python3
"""
Sequential 1-by-1 Multi-Platform Registration Runner for MaxRummy / Yono Bot.
Runs sequentially through platforms, checks status, requests 1 OTP at a time,
registers, claims bonuses, and logs to registered_accounts.txt.
"""

import sys
import os
import asyncio
import time
import json
from pathlib import Path

# Add project root to sys.path
BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from maxrummy_client import MaxRummyClient, clean_phone_number, PLATFORMS_MAP, PLATFORMS_LIST
import config

ACCOUNTS_FILE = getattr(config, "ACCOUNTS_FILE", BASE_DIR / "registered_accounts.txt")

# Priority platforms with verified login servers & fresh status
PRIORITY_ORDER = [
    "bossrummy",
    "gamerummy",
    "indclub",
    "okrummy",
    "goldrummy",
    "spin101",
    "spin777",
    "spinwinner",
    "yn777",
    "winrummy",
    "inrrummy",
    "toprummy",
    "loverummy",
    "jaiho91",
    "jaihorummy",
    "jaihowin",
    "jeetspin",
    "joyrummy",
    "mbmbet",
    "rumblerummy",
    "rummyludo",
    "rummyzip",
    "sagaslots",
    "shareslots",
    "slotsspin",
    "slotswinner",
    "spincrush",
    "spingold",
    "spinlucky",
    "yono777",
    "yonoarcade",
    "yonogames",
    "yonorummy",
    "yonovip",
    "yonoslots",
    "101z",
    "567slots",
    "abcrummy",
    "hirummy",
    "indbingo",
    "indslot",
    "jaiho777",
    "jaihoarcade",
    "jaihoslots"
]

def get_ordered_platforms():
    ordered = []
    seen = set()
    for pid in PRIORITY_ORDER:
        if pid in PLATFORMS_MAP and pid not in seen:
            ordered.append(PLATFORMS_MAP[pid])
            seen.add(pid)
    for p in PLATFORMS_LIST:
        if p["id"] not in seen:
            ordered.append(p)
            seen.add(p["id"])
    return ordered

def save_account_record(platform_name: str, phone: str, password: str, uid: int, name: str, bonus: float, bal: float):
    rec = (
        f"[{platform_name}] {phone}:{password} | "
        f"UID: {uid} | Name: {name} | "
        f"Bonus: ₹{bonus:.2f} | Balance: ₹{bal:.2f} | "
        f"Date: {time.strftime('%Y-%m-%d %H:%M:%S')}"
    )
    with open(ACCOUNTS_FILE, "a", encoding="utf-8") as f:
        f.write(rec + "\n")
    print(f"📁 Record saved to {ACCOUNTS_FILE}:\n   {rec}\n")

async def check_platform_status(client: MaxRummyClient, phone: str):
    """
    Check if phone is fresh (spcode 209), registered (333), or on cooldown (341).
    Does NOT consume or invalidate any OTP.
    """
    import hashlib
    from maxrummy_client import pack_packet, unpack_packet
    
    clean_phone = clean_phone_number(phone)
    now = int(time.time() * 1000)
    x_sign = hashlib.md5(f"{now}{client.auth_salt}".encode()).hexdigest()
    
    req = {
        "c": 1, "c_ts": now, "x": x_sign, "c_idx": 1,
        "user": clean_phone, "passwd": "CheckStatusDummyPwd123!",
        "app": client.app_id, "v": client.res_version, "av": client.app_version,
        "t": 9, "accessToken": "",
        "platform": "Android", "phone": "Xiaomi(13)",
        "code": "", "token": "", "bwss": 1 if client.login_ws_uri.startswith("wss://") else 0,
        "LoginExData": "", "language": 2, "dlan": "en-US",
        "bundleid": client.bundle_id, "deviceid": "dev_check_status", "dinfo": "{}"
    }
    try:
        async with await client._ws_connect(client.login_ws_uri) as ws:
            await ws.send(pack_packet(req, now))
            raw = await asyncio.wait_for(ws.recv(), timeout=4.0)
            resp = unpack_packet(raw)
            spcode = resp.get("spcode")
            if spcode == 209:
                return "FRESH", 0
            elif spcode == 333:
                return "REGISTERED", 0
            elif spcode == 341:
                return "COOLDOWN", resp.get("param", 600)
            else:
                return f"SPCODE_{spcode}", 0
    except Exception as e:
        return f"ERR_{type(e).__name__}", 0

async def register_single(platform_id: str, phone: str, otp: str, password: str = None):
    clean_phone = clean_phone_number(phone)
    if not password:
        password = "Pass@" + clean_phone[-5:]
    
    client = MaxRummyClient(platform_id)
    print(f"\n⚡ Registering {clean_phone} on {client.platform_name} ({platform_id}) with OTP '{otp}'...")
    res = await client.register_async(phone=clean_phone, password=password, otp=otp)
    
    if res.get("ok"):
        uid = res.get("uid")
        bonus = res.get("claimed_bonus", 0.0)
        bal = res.get("total_balance", bonus)
        name = res.get("nickname") or res.get("name") or "Player"
        print(f"\n🎉 SUCCESS: Registered on {client.platform_name}!")
        print(f"   UID: {uid}")
        print(f"   Password: {password}")
        print(f"   Bonus Claimed: ₹{bonus:.2f}")
        print(f"   Wallet Balance: ₹{bal:.2f}")
        save_account_record(client.platform_name, clean_phone, password, uid, name, bonus, bal)
        return True, res
    else:
        print(f"\n❌ FAILED on {client.platform_name}: {res.get('msg')} (spcode: {res.get('spcode')})")
        return False, res

def send_platform_otp(platform_id: str, phone: str):
    client = MaxRummyClient(platform_id)
    clean_phone = clean_phone_number(phone)
    print(f"\n📲 Requesting 1 OTP for {clean_phone} via {client.platform_name}...")
    res = client.send_otp(clean_phone, otptype=6)
    if res.get("ok"):
        print(f"✅ OTP Sent Successfully via {client.platform_name}!")
        print(f"📩 Kripya user se {client.platform_name} ka naya OTP poochein.")
        return True, res
    else:
        print(f"❌ Failed to send OTP on {client.platform_name}: {res.get('msg')}")
        return False, res

async def run_interactive(phone: str, start_platform: str = None):
    clean_phone = clean_phone_number(phone)
    platforms = get_ordered_platforms()
    if start_platform:
        # Move start_platform to front
        platforms = [p for p in platforms if p["id"] == start_platform] + [p for p in platforms if p["id"] != start_platform]
    
    print(f"════════════════════════════════════════════════════════════════")
    print(f"   YONO / MAXRUMMY SEQUENTIAL 1-BY-1 REGISTRATION RUNNER")
    print(f"   Target Mobile: {clean_phone}")
    print(f"   Accounts File: {ACCOUNTS_FILE}")
    print(f"════════════════════════════════════════════════════════════════\n")

    for i, p in enumerate(platforms, 1):
        pid = p["id"]
        pname = p["name"]
        client = MaxRummyClient(p)
        print(f"[{i}/{len(platforms)}] Checking {pname} ({pid})...")
        status, param = await check_platform_status(client, clean_phone)
        
        if status != "FRESH":
            print(f"   ⏭️ Skipping {pname} (Status: {status})\n")
            continue
        
        print(f"   🟢 {pname} is 100% FRESH (Unregistered & 0 Cooldown)!")
        # Send OTP
        ok, send_res = send_platform_otp(pid, clean_phone)
        if not ok:
            print(f"   ⚠️ Could not send OTP on {pname}. Moving to next.\n")
            continue
            
        # Prompt for OTP
        user_otp = input(f"\n👉 Enter OTP received for [{pname}] (or 'skip' / 'exit'): ").strip()
        if user_otp.lower() == "exit":
            print("Exiting runner.")
            break
        elif user_otp.lower() == "skip" or not user_otp:
            print(f"Skipping {pname}.\n")
            continue
            
        success, reg_res = await register_single(pid, clean_phone, user_otp)
        if success:
            print(f"✅ [{pname}] Complete! Moving to next platform in queue...\n")
        else:
            retry = input(f"Retry registration on [{pname}]? (y/N): ").strip().lower()
            if retry == 'y':
                user_otp = input(f"Enter correct OTP for [{pname}]: ").strip()
                if user_otp:
                    await register_single(pid, clean_phone, user_otp)

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Terminal 1-by-1 Registration")
    parser.add_argument("--phone", "-p", default="9106167062", help="Mobile number to register")
    parser.add_argument("--platform", default=None, help="Specific platform ID")
    parser.add_argument("--send-otp", action="store_true", help="Send OTP for platform")
    parser.add_argument("--otp", help="OTP to register with")
    parser.add_argument("--scan", action="store_true", help="Scan status across all platforms")
    args = parser.parse_args()

    clean_phone = clean_phone_number(args.phone)

    if args.scan:
        print(f"🔍 Scanning all 48 platforms for mobile: {clean_phone}...\n")
        async def do_scan():
            platforms = get_ordered_platforms()
            for p in platforms:
                pid = p["id"]
                pname = p["name"]
                c = MaxRummyClient(p)
                status, param = await check_platform_status(c, clean_phone)
                if status == "FRESH":
                    print(f"🟢 [FRESH]      {pname:16} ({pid})")
                elif status == "REGISTERED":
                    print(f"🔴 [REGISTERED] {pname:16} ({pid})")
                elif status == "COOLDOWN":
                    print(f"🟡 [COOLDOWN]   {pname:16} ({pid}) -> {param}s left")
                else:
                    print(f"⚪ [{status:10}] {pname:16} ({pid})")
        asyncio.run(do_scan())
        sys.exit(0)

    if args.send_otp:
        pid = args.platform or "bossrummy"
        send_platform_otp(pid, clean_phone)
        sys.exit(0)

    if args.otp:
        pid = args.platform or "bossrummy"
        asyncio.run(register_single(pid, clean_phone, args.otp))
        sys.exit(0)

    # Default: Interactive mode
    asyncio.run(run_interactive(clean_phone, args.platform))
