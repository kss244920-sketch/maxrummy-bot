#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
1-by-1 Multi-Platform Sequential Registration Tool
Sequentially registers a mobile number on all available fresh Yono platforms,
claims welcome/bind bonus for each, and saves to registered_accounts.txt.
"""

import sys
import time
import json
import asyncio
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()
sys.path.insert(0, str(BASE_DIR))

from maxrummy_client import (
    MaxRummyClient,
    clean_phone_number,
    PLATFORMS_MAP
)
from maxrummy_bot import parse_otp_and_password
import config

GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


async def run_multi_registration(phone_input: str):
    phone = clean_phone_number(phone_input)
    if len(phone) != 10:
        print(f"{RED}Invalid mobile number! 10-digit number required.{RESET}")
        return

    print(f"\n{CYAN}{BOLD}{'=' * 65}{RESET}")
    print(f"{CYAN}{BOLD}   1-BY-1 MULTI-PLATFORM REGISTRATION & BONUS CLAIM   {RESET}")
    print(f"{CYAN}{BOLD}{'=' * 65}{RESET}")
    print(f"📱 Target Mobile: {GREEN}{BOLD}{phone}{RESET}\n")

    print(f"{YELLOW}⏳ Scanning all 48 Yono platforms to build fresh registration queue...{RESET}")
    results = await MaxRummyClient.check_all_platforms_async(phone, timeout=6.0)
    avail = [r for r in results if r.get("status") == "available"]

    if not avail:
        print(f"{RED}❌ Koi bhi platform is number par fresh/available nahi mila.{RESET}")
        return

    print(f"\n{GREEN}✅ Found {len(avail)} Fresh Platforms Available!{RESET}")
    for idx, a in enumerate(avail, start=1):
        print(f"  {idx}. {a['name']} ({a['platform_id']})")

    confirm = input(f"\n{YELLOW}Kya 1-by-1 registration shuru karein? (Y/n): {RESET}").strip().lower()
    if confirm == "n":
        print(f"{YELLOW}Cancelled.{RESET}")
        return

    stats = {
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "total_bonus": 0.0,
        "records": []
    }

    for idx, item in enumerate(avail, start=1):
        pid = item["platform_id"]
        p_info = PLATFORMS_MAP[pid]
        p_name = p_info["name"]

        print(f"\n{CYAN}{'━' * 60}{RESET}")
        print(f"{BOLD}[{idx}/{len(avail)}] 🎰 Platform: {GREEN}{p_name}{RESET} ({pid})")
        print(f"{CYAN}{'━' * 60}{RESET}")

        client = MaxRummyClient(platform=p_info)

        # Trigger Send OTP
        print(f"⏳ Sending OTP via {p_name} SMS gateway...")
        otp_res = client.send_otp(phone, otptype=6)
        if not otp_res.get("ok"):
            print(f"{RED}❌ OTP Send Failed: {otp_res.get('msg')}{RESET}")
            stats["failed"] += 1
            continue

        print(f"{GREEN}✅ OTP sent successfully to {phone}!{RESET}")

        # Wait for user input
        while True:
            raw_input = input(f"{YELLOW}👉 Enter OTP for {p_name} (ya 's' to skip, 'q' to quit): {RESET}").strip()
            if raw_input.lower() == "q":
                print(f"\n{YELLOW}⚠️ Process stopped by user.{RESET}")
                print_summary(phone, stats, len(avail))
                return
            if raw_input.lower() == "s":
                print(f"{YELLOW}⏭️ Skipped {p_name}.{RESET}")
                stats["skipped"] += 1
                break

            otp, pwd = parse_otp_and_password(raw_input)
            if not otp or len(otp) < 4:
                print(f"{RED}Invalid OTP format. Kripya 4-6 digit OTP dalein.{RESET}")
                continue

            print(f"⚡ Registering {phone} on {p_name} with OTP {otp}...")
            reg_res = await client.register_async(phone=phone, password=pwd, otp=otp)

            if reg_res.get("ok"):
                uid = reg_res.get("uid")
                bonus = reg_res.get("claimed_bonus", 0.0)
                bal = reg_res.get("total_balance", bonus)
                name = reg_res.get("nickname") or reg_res.get("name") or "Player"

                stats["success"] += 1
                stats["total_bonus"] += bonus
                stats["records"].append({
                    "platform": p_name,
                    "uid": uid,
                    "bonus": bonus,
                    "balance": bal,
                    "pwd": pwd
                })

                # Save record to registered_accounts.txt
                rec = f"[{p_name}] {phone}:{pwd} | UID: {uid} | Name: {name} | Bonus: ₹{bonus:.2f} | Balance: ₹{bal:.2f} | Date: {time.strftime('%Y-%m-%d %H:%M')}"
                with open(config.ACCOUNTS_FILE, "a", encoding="utf-8") as f:
                    f.write(rec + "\n")

                print(f"{GREEN}{BOLD}🎉 SUCCESS! {p_name} Registered!{RESET}")
                print(f"• UID: {BOLD}{uid}{RESET} | Nickname: {name}")
                print(f"• Claimed Bonus: {GREEN}{BOLD}₹{bonus:.2f}{RESET} | Total Balance: {GREEN}{BOLD}₹{bal:.2f}{RESET}")
                break
            else:
                err_msg = reg_res.get("msg", "Failed")
                print(f"{RED}❌ Registration Failed on {p_name}: {err_msg}{RESET}")
                retry = input(f"{YELLOW}Dobara OTP try karna hai? (y/N): {RESET}").strip().lower()
                if retry != "y":
                    stats["failed"] += 1
                    break

    print_summary(phone, stats, len(avail))


def print_summary(phone: str, stats: dict, total_avail: int):
    print(f"\n{CYAN}{BOLD}{'=' * 65}{RESET}")
    print(f"{CYAN}{BOLD}   GRAND REGISTRATION SUMMARY   {RESET}")
    print(f"{CYAN}{BOLD}{'=' * 65}{RESET}")
    print(f"📱 Mobile: {BOLD}{phone}{RESET}")
    print(f"✅ Successful Registrations: {GREEN}{BOLD}{stats['success']}/{total_avail}{RESET}")
    print(f"❌ Failed: {RED}{stats['failed']}{RESET}")
    print(f"⏭️ Skipped: {YELLOW}{stats['skipped']}{RESET}")
    print(f"💰 Total Bonus Collected: {GREEN}{BOLD}₹{stats['total_bonus']:.2f}{RESET}")
    print(f"📁 Accounts Saved in: {config.ACCOUNTS_FILE}\n")


if __name__ == "__main__":
    p_num = sys.argv[1] if len(sys.argv) > 1 else input("Enter 10-digit mobile number: ").strip()
    asyncio.run(run_multi_registration(p_num))
