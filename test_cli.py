#!/usr/bin/env python3
"""
CLI Test Tool for MaxRummy / YonoGames Client
Allows quick testing of:
1. Sending OTP to a phone number
2. Registering with OTP and auto-claiming bind bonus
3. Claiming all bonuses for an account
4. Checking account balance
"""

import sys
import json
import argparse
from maxrummy_client import MaxRummyClient


def main():
    parser = argparse.ArgumentParser(description="MaxRummy CLI Test Tool")
    parser.add_argument("--platform", "-P", type=str, default="maxrummy", help="Platform ID (default: maxrummy)")
    subparsers = parser.add_subparsers(dest="command", help="Sub-command to run")

    # Command: probe
    p_probe = subparsers.add_parser("probe", help="Probe if phone is registered on platform")
    p_probe.add_argument("phone", type=str, help="10-digit mobile number")

    # Command: send-otp
    p_otp = subparsers.add_parser("send-otp", help="Send OTP to phone number")
    p_otp.add_argument("phone", type=str, help="10-digit mobile number")
    p_otp.add_argument("--type", "-t", type=int, default=6, help="OTP Type (6=Direct Register, 1=Bind Phone)")

    # Command: register
    p_reg = subparsers.add_parser("register", help="Register account with OTP and claim bind bonus")
    p_reg.add_argument("phone", type=str, help="10-digit mobile number")
    p_reg.add_argument("otp", type=str, help="Received OTP")
    p_reg.add_argument("--password", "-p", type=str, default="User12345", help="Account password (default: User12345)")

    # Command: claim
    p_claim = subparsers.add_parser("claim", help="Claim all bonuses for account")
    p_claim.add_argument("phone", type=str, help="Account phone number")
    p_claim.add_argument("password", type=str, help="Account password")

    # Command: balance
    p_bal = subparsers.add_parser("balance", help="Check account balance")
    p_bal.add_argument("phone", type=str, help="Account phone number")
    p_bal.add_argument("password", type=str, help="Account password")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    client = MaxRummyClient(platform=args.platform)

    if args.command == "probe":
        print(f"⏳ Probing registration status for {args.phone} on {client.platform_name}...")
        import asyncio
        res = asyncio.run(client.login_async(args.phone, "ProbePass12345"))
        print("\n--- Result ---")
        if res.get("spcode") == 955:
            print(f"🟢 FRESH / AVAILABLE! Phone {args.phone} is NOT registered yet on {client.platform_name}.")
        elif res.get("spcode") == 333:
            print(f"🔴 REGISTERED! Phone {args.phone} is ALREADY registered on {client.platform_name}.")
        else:
            print(json.dumps(res, indent=2))

    if args.command == "send-otp":
        print(f"⏳ Sending OTP (type={args.type}) to {args.phone}...")
        res = client.send_otp(args.phone, otptype=args.type)
        print("\n--- Result ---")
        print(json.dumps(res, indent=2))

    elif args.command == "register":
        print(f"⏳ Registering {args.phone} with OTP '{args.otp}' and claiming Bind Bonus...")
        res = client.register(args.phone, args.password, args.otp)
        print("\n--- Result ---")
        print(json.dumps(res, indent=2))

    elif args.command == "claim":
        print(f"⏳ Claiming bonuses for {args.phone}...")
        res = client.claim_bonuses(args.phone, args.password)
        print("\n--- Result ---")
        print(json.dumps(res, indent=2))

    elif args.command == "balance":
        print(f"⏳ Fetching balance for {args.phone}...")
        res = client.login(args.phone, args.password)
        print("\n--- Result ---")
        print(json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
