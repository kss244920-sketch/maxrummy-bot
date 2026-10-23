#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MaxRummy / YonoGames Cocos Platform Client Engine
Reverse-engineered WebSocket protocol client supporting:
- SMS OTP sending
- Account registration with OTP + auto-claiming Bind Bonus
- Phone login & authentication
- Phone binding & bonus claiming (Bind Bonus, 7-Day Sign-in, Daily Rewards)
- Real-time balance extraction (Cash, Deposit, Bonus, VIP)
- Multi-threaded bulk balance checking & bulk bonus claiming
"""

import os
import sys
import time
import json
import ssl
import socket
import random
import hashlib
import struct
import asyncio
import re
import secrets
from pathlib import Path
from typing import Dict, Any, Optional, List, Tuple
from concurrent.futures import ThreadPoolExecutor

import requests
import msgpack
import websockets

try:
    import urllib3.util.connection
    urllib3.util.connection.HAS_IPV6 = False
except Exception:
    pass

# Load Multi-Platform Registry
PLATFORMS_FILE = Path(__file__).parent / "platforms.json"

def load_platforms() -> List[Dict[str, Any]]:
    if PLATFORMS_FILE.exists():
        try:
            with open(PLATFORMS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return []

PLATFORMS_LIST: List[Dict[str, Any]] = load_platforms()
PLATFORMS_MAP: Dict[str, Dict[str, Any]] = {p["id"]: p for p in PLATFORMS_LIST}

# Fallback Configuration constants (SpinGold)
LOGIN_SERVER_HOST = "lo.ghjio08.com"
LOGIN_WS_URI = f"wss://{LOGIN_SERVER_HOST}/ws"
SMS_OTP_URL = "https://service.eujujo08.com/sms/dosend"

APP_ID = 17
RES_VERSION = "1.2.1.3"
APP_VERSION = "1.1.9"
BUNDLE_ID = "com.spin.glg.games"
AUTH_SALT = "hero888"
DEFAULT_INVITE_CODE = "H1QKDHCU"

# MsgId definitions
MSG_LOGIN = 1
MSG_LOGIN_USERID = 2
MSG_RELOGIN_USERID = 3
MSG_SYNC_COIN = 29
MSG_PERSIONAL_INFO = 62
MSG_BIND_PHONE = 134
MSG_CHECK_PHONE_USED = 136
MSG_BIND_INVITE_CODE = 28
MSG_REQ_REWARD_TODAY = 152
MSG_EVENT_SIGN_REWARD = 419
MSG_EVENT_CYCLE_SIGN_CONFIG = 426
MSG_EVENT_CYCLE_SIGN_REWARD = 427
MSG_GET_NEWER_GIFT_REWARDS = 665  # Bind Phone / Welcome Bonus
MSG_REQ_MAIL_LIST = 202  # Mail Inbox list
MSG_REQ_GET_ALL_MALL_ATTACH = 215  # Claim all mail bonus attachments

# Login Types
LOGIN_TYPE_GUEST = 1
LOGIN_TYPE_PHONE_REGIS = 8
LOGIN_TYPE_PHONE = 9

# SPCODE error descriptions
SPCODE_MESSAGES = {
    200: "Success",
    201: "Phone already used / bound",
    209: "Authentication failed",
    214: "Update required",
    216: "Registration unavailable now, please try again later",
    217: "Device login restrictions",
    218: "Unsupported device",
    333: "Wrong password",
    334: "Invalid OTP code",
    335: "New device login - OTP verification required",
    339: "Send too frequently, please wait",
    340: "App version update required",
    341: "Number on cooldown, please wait before trying again",
    405: "This account has been banned",
    426: "Registration currently unavailable",
    554: "Phone already bound to this account",
    555: "Phone number already bound to another account",
    560: "Need to bind phone number first",
    563: "Cannot enjoy any discount",
    565: "No welcome / bind gift available (Already claimed)",
    761: "Please login after 2 minutes",
    801: "Version mismatch - update required",
    803: "Maintenance in progress",
    955: "Account does not exist",
    976: "Reward already claimed or expired"
}


def src_sum(data: bytes, length: int) -> int:
    """
    Cocos Global.srcSum implementation:
    Computes 16-bit CRC/checksum on first min(len, 128) bytes.
    """
    s = 65535
    check_len = min(len(data), length)
    for i in range(check_len):
        s ^= data[i]
        if (s & 1) == 0:
            s >>= 1
        else:
            s = (s >> 1) ^ 28849
    return s & 0xFFFF


def pack_packet(payload: dict, cur_time_ms: Optional[int] = None) -> bytes:
    """
    Packs payload into game network framing:
    Header: [2-byte len, 2-byte checksum, 4-byte unix_seconds_ts]
    Body: msgpack encoded payload
    """
    if cur_time_ms is None:
        cur_time_ms = int(time.time() * 1000)

    raw_body = msgpack.packb(payload)
    body_len = len(raw_body)
    chksum = src_sum(raw_body, min(body_len, 128))
    ts_sec = int(cur_time_ms / 1000)

    header = struct.pack(">HHI", body_len, chksum, ts_sec)
    return header + raw_body


def unpack_packet(data: bytes) -> Any:
    """
    Unpacks game network packet by removing 8-byte header and decoding msgpack.
    """
    if len(data) < 8:
        return None
    raw_payload = data[8:]
    try:
        decoded = msgpack.unpackb(raw_payload)
        if isinstance(decoded, str):
            try:
                decoded = json.loads(decoded)
            except Exception:
                pass
        return decoded
    except Exception:
        return None


def clean_phone_number(phone: str) -> str:
    """Strips non-digits and returns 10-digit Indian phone number."""
    digits = re.sub(r"\D", "", str(phone))
    return digits[-10:] if len(digits) >= 10 else digits


class MaxRummyClient:
    def __init__(self, platform: Optional[Any] = None, timeout: float = 12.0):
        self.timeout = timeout
        self.session = requests.Session()

        # Load platform config or default to Max Rummy
        if isinstance(platform, dict):
            self.platform = platform
        elif isinstance(platform, str) and platform in PLATFORMS_MAP:
            self.platform = PLATFORMS_MAP[platform]
        else:
            self.platform = PLATFORMS_MAP.get("maxrummy") or PLATFORMS_MAP.get("spingold", {
                "id": "maxrummy",
                "name": "Max Rummy",
                "app_id": 17,
                "bundle_id": "com.maxfun.rmy.games",
                "app_version": "1.0.3",
                "res_version": "1.1.1.0",
                "login_server": "lo.cw237rjwa3o8.com",
                "sms_otp_url": "https://service.cwh3218shj3iu.com/sms/dosend",
                "auth_salt": "hero888",
                "invite_code": "QUMZJ6HHDP4"
            })

        self.platform_id = self.platform.get("id", "maxrummy")
        self.platform_name = self.platform.get("name", "Max Rummy")
        self.login_server = self.platform.get("login_server", LOGIN_SERVER_HOST)
        self.login_ws_uri = f"wss://{self.login_server}/ws" if not self.login_server.startswith("ws") else self.login_server
        self.sms_otp_url = self.platform.get("sms_otp_url", SMS_OTP_URL)
        self.app_id = self.platform.get("app_id", APP_ID)
        self.bundle_id = self.platform.get("bundle_id", BUNDLE_ID)
        self.res_version = self.platform.get("res_version", RES_VERSION)
        self.app_version = self.platform.get("app_version", APP_VERSION)
        self.auth_salt = self.platform.get("auth_salt", AUTH_SALT)
        self.invite_code = self.platform.get("invite_code", DEFAULT_INVITE_CODE)

    def send_otp(self, phone: str, channel: int = 0, otptype: int = 6) -> Dict[str, Any]:
        """
        Sends registration/binding OTP via HTTP service.
        otptype=6 (Direct Register OTP - matches SpinGold Registration).
        otptype=1 (Phone Binding OTP - matches PhoneBindingView).
        """
        clean_phone = clean_phone_number(phone)
        if len(clean_phone) != 10:
            return {"ok": False, "msg": f"Invalid phone number: {phone}"}

        params = {
            "phone": clean_phone,
            "channel": channel,
            "otptype": otptype
        }
        try:
            resp = self.session.get(self.sms_otp_url, params=params, timeout=self.timeout)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    return {
                        "ok": True,
                        "msg": f"OTP sent successfully to {clean_phone}",
                        "phone": clean_phone,
                        "data": data.get("data", {})
                    }
                else:
                    return {
                        "ok": False,
                        "msg": data.get("msg") or f"OTP send error code: {data.get('code')}",
                        "raw": data
                    }
            return {"ok": False, "msg": f"HTTP Error {resp.status_code}"}
        except Exception as e:
            return {"ok": False, "msg": f"Request failed: {str(e)}"}

    async def _ws_connect(self, uri: str):
        ssl_ctx = None
        if uri.startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

        ua = "Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"
        parsed_host = uri.replace("wss://", "").replace("ws://", "").split("/")[0].split(":")[0] or self.login_server
        headers = {
            "User-Agent": ua,
            "Origin": f"https://{parsed_host}"
        }

        ws_kwargs = {
            "ssl": ssl_ctx,
            "open_timeout": max(self.timeout, 12.0),
            "close_timeout": 1.5,
            "max_size": 10 * 1024 * 1024,
            "family": socket.AF_INET
        }
        try:
            import inspect
            sig_params = inspect.signature(websockets.connect).parameters
            if "user_agent_header" in sig_params:
                ws_kwargs["user_agent_header"] = ua
            if "additional_headers" in sig_params:
                ws_kwargs["additional_headers"] = headers
            elif "extra_headers" in sig_params:
                ws_kwargs["extra_headers"] = headers
        except Exception:
            pass

        last_exc = None
        for attempt in range(2):
            try:
                return await websockets.connect(uri, **ws_kwargs)
            except Exception as e:
                last_exc = e
                status_code = getattr(e, "status_code", None)
                if status_code is None and hasattr(e, "response"):
                    status_code = getattr(e.response, "status_code", None)

                if status_code in (400, 403):
                    # Retry cleanly with User-Agent but without Origin header if server blocks it
                    clean_headers = {"User-Agent": ua}
                    if "additional_headers" in ws_kwargs:
                        ws_kwargs["additional_headers"] = clean_headers
                    elif "extra_headers" in ws_kwargs:
                        ws_kwargs["extra_headers"] = clean_headers
                    try:
                        return await websockets.connect(uri, **ws_kwargs)
                    except Exception as inner_e:
                        last_exc = inner_e
                if attempt < 1:
                    await asyncio.sleep(0.5)
        raise last_exc

    async def login_async(self, phone: str, password: str) -> Dict[str, Any]:
        """
        Async phone login & balance check:
        1. Connects to Login Server -> Sends MsgId.LOGIN (c=1, t=9).
        2. Retrieves node server URL and tokens.
        3. Connects to Node Server -> Sends MsgId.LOGIN_USERID (c=2) & MsgId.PERSIONAL_INFO (c=62).
        4. Extracts real-time balance (Coin, Deposit, Bonus, Draw info).
        """
        clean_phone = clean_phone_number(phone)
        if len(clean_phone) != 10:
            return {"ok": False, "msg": f"Invalid phone format: {phone}", "phone": phone}

        now = int(time.time() * 1000)
        x_sign = hashlib.md5(f"{now}{self.auth_salt}".encode()).hexdigest()
        device_id = hashlib.md5(f"device_{clean_phone}_{self.platform_id}".encode()).hexdigest()[:16]

        login_req = {
            "c": MSG_LOGIN,
            "c_ts": now,
            "x": x_sign,
            "c_idx": 1,
            "user": clean_phone,
            "passwd": password,
            "app": self.app_id,
            "v": self.res_version,
            "av": self.app_version,
            "t": LOGIN_TYPE_PHONE,
            "accessToken": "",
            "platform": "Android",
            "phone": "Xiaomi(13)",
            "code": "",
            "token": "",
            "bwss": 1,
            "LoginExData": 1,
            "language": 2,
            "dlan": "en-US",
            "client_uuid": f"{now}{random.randint(1000000, 9999999)}",
            "bundleid": self.bundle_id,
            "deviceid": device_id,
            "dinfo": "{}"
        }

        try:
            async with await self._ws_connect(self.login_ws_uri) as ws:
                pkt = pack_packet(login_req, now)
                await ws.send(pkt)

                resp_bytes = await asyncio.wait_for(ws.recv(), timeout=self.timeout)
                resp = unpack_packet(resp_bytes)
                if not resp or not isinstance(resp, dict):
                    return {"ok": False, "msg": "Invalid response from login server", "phone": clean_phone}

                spcode = resp.get("spcode")
                if spcode is not None and spcode != 0 and spcode != 200:
                    param = resp.get("param")
                    if spcode == 341 and param:
                        mins, secs = divmod(int(param), 60)
                        err_msg = f"Too many attempts. Number on cooldown for {mins}m {secs}s"
                    else:
                        err_msg = SPCODE_MESSAGES.get(spcode, f"Login failed (Error {spcode})")
                    return {
                        "ok": False,
                        "spcode": spcode,
                        "msg": err_msg,
                        "phone": clean_phone
                    }

                # Login successful, parse node credentials
                net_server = resp.get("net")
                uid = resp.get("uid")
                server_id = resp.get("server", 1)
                sub_id = resp.get("subid", 1)
                token = resp.get("token")
                player_info = resp.get("playerInfo") or {}

                coin = float(player_info.get("coin", 0.0))
                dcoin = float(player_info.get("dcoin", 0.0))
                ecoin = float(player_info.get("ecoin", 0.0))
                nickname = player_info.get("playername") or player_info.get("nick") or f"Player_{uid}"
                vip = player_info.get("vip", 0)

                # Connect to node server to fetch live wallet info
                if net_server:
                    try:
                        clean_host = net_server.replace("ws://", "").replace("wss://", "")
                        proto = "ws://" if ":" in clean_host else "wss://"
                        if not clean_host.endswith("/ws"):
                            clean_host += "/ws"
                        node_uri = f"{proto}{clean_host}"

                        async with await self._ws_connect(node_uri) as node_ws:
                            node_now = int(time.time() * 1000)
                            node_login = {
                                "c": MSG_LOGIN_USERID,
                                "uid": uid,
                                "openid": "",
                                "server": server_id,
                                "subid": sub_id,
                                "token": token,
                                "deviceid": device_id,
                                "appver": self.app_version,
                                "app": self.app_id,
                                "bundleid": self.bundle_id,
                                "v": self.res_version,
                                "c_ts": node_now,
                                "c_idx": 1
                            }
                            await node_ws.send(pack_packet(node_login, node_now))

                            # Send PERSIONAL_INFO (62) to get precise wallet & draw info
                            info_now = int(time.time() * 1000)
                            info_req = {
                                "c": MSG_PERSIONAL_INFO,
                                "otheruid": uid,
                                "c_ts": info_now,
                                "c_idx": 2
                            }
                            await node_ws.send(pack_packet(info_req, info_now))

                            end_time = time.time() + 2.0
                            while time.time() < end_time:
                                try:
                                    n_bytes = await asyncio.wait_for(node_ws.recv(), timeout=0.8)
                                    n_resp = unpack_packet(n_bytes)
                                    if n_resp and isinstance(n_resp, dict):
                                        p_info = n_resp.get("playerInfo") or {}
                                        if p_info:
                                            coin = float(p_info.get("coin", coin))
                                            dcoin = float(p_info.get("dcoin", dcoin))
                                            ecoin = float(p_info.get("ecoin", ecoin))
                                            nickname = p_info.get("playername") or nickname
                                            vip = p_info.get("vip", vip)
                                        if n_resp.get("c") == MSG_PERSIONAL_INFO:
                                            break
                                except asyncio.TimeoutError:
                                    break
                    except Exception:
                        pass

                total_balance = round(coin + dcoin + ecoin, 2)
                return {
                    "ok": True,
                    "msg": "Login Successful",
                    "uid": uid,
                    "phone": clean_phone,
                    "password": password,
                    "nickname": nickname,
                    "coin": round(coin, 2),
                    "dcoin": round(dcoin, 2),
                    "ecoin": round(ecoin, 2),
                    "total_balance": total_balance,
                    "vip": vip,
                    "token": token,
                    "net_server": net_server,
                    "server_id": server_id,
                    "sub_id": sub_id
                }

        except asyncio.TimeoutError:
            return {"ok": False, "msg": "Connection timeout", "phone": clean_phone}
        except Exception as e:
            return {"ok": False, "msg": f"Connection error: {str(e)}", "phone": clean_phone}

    async def register_async(
        self,
        phone: str,
        password: str,
        otp: str,
        name: Optional[str] = None,
        invite_code: str = ""
    ) -> Dict[str, Any]:
        """
        Async account registration:
        1. Sends MsgId.LOGIN (c=1, t=8, PHONE_REGIS) with OTP verification.
        2. Automatically connects to node server and claims Bind / Welcome Bonus (MsgId.GET_NEWER_GIFT_REWARDS = 665).
        """
        clean_phone = clean_phone_number(phone)
        if len(clean_phone) != 10:
            return {"ok": False, "msg": f"Invalid phone format: {phone}", "phone": phone}

        if not name:
            first_names = ["Amit", "Rohit", "Rahul", "Suresh", "Vikram", "Deepak", "Manoj", "Ajay", "Sunil", "Rakesh"]
            last_names = ["Kumar", "Sharma", "Verma", "Singh", "Patel", "Gupta", "Yadav", "Chauhan", "Mishra", "Joshi"]
            name = f"{random.choice(first_names)} {random.choice(last_names)}"

        if not invite_code:
            invite_code = self.invite_code

        now = int(time.time() * 1000)
        x_sign = hashlib.md5(f"{now}{self.auth_salt}".encode()).hexdigest()
        # Android device ID: 16-character lowercase hex string
        device_id = hashlib.md5(f"device_{clean_phone}_{self.platform_id}".encode()).hexdigest()[:16]

        is_wss = 1 if self.login_ws_uri.startswith("wss://") else 0
        reg_req = {
            "c": MSG_LOGIN,
            "c_ts": now,
            "x": x_sign,
            "c_idx": 1,
            "user": clean_phone,
            "passwd": password,
            "pwd": password,
            "app": self.app_id,
            "v": self.res_version,
            "av": self.app_version,
            "t": LOGIN_TYPE_PHONE_REGIS,
            "accessToken": name,
            "holdername": name,
            "otp": str(otp).strip(),
            "platform": "Android",
            "phone": "Xiaomi(13)",
            "code": "",
            "token": str(otp).strip(),
            "bwss": is_wss,
            "LoginExData": "mb_reg",
            "language": 2,
            "dlan": "en-US",
            "client_uuid": f"{now}{random.randint(1000000, 9999999)}",
            "bundleid": self.bundle_id,
            "deviceid": device_id,
            "dinfo": "{}"
        }

        try:
            async with await self._ws_connect(self.login_ws_uri) as ws:
                pkt = pack_packet(reg_req, now)
                await ws.send(pkt)

                resp_bytes = await asyncio.wait_for(ws.recv(), timeout=self.timeout)
                resp = unpack_packet(resp_bytes)
                if not resp or not isinstance(resp, dict):
                    return {"ok": False, "msg": "Invalid response from server", "phone": clean_phone}

                spcode = resp.get("spcode")
                if spcode == 216:
                    # Auto-fallback: retry without invite code or with alternative LoginExData
                    for fallback_patch in [{"code": ""}, {"code": "", "LoginExData": 1}]:
                        try:
                            f_now = int(time.time() * 1000)
                            f_x_sign = hashlib.md5(f"{f_now}{self.auth_salt}".encode()).hexdigest()
                            f_req = dict(reg_req)
                            f_req.update(fallback_patch)
                            f_req["c_ts"] = f_now
                            f_req["x"] = f_x_sign
                            f_req["client_uuid"] = f"{f_now}{random.randint(1000000, 9999999)}"
                            async with await self._ws_connect(self.login_ws_uri) as f_ws:
                                await f_ws.send(pack_packet(f_req, f_now))
                                f_bytes = await asyncio.wait_for(f_ws.recv(), timeout=self.timeout)
                                f_resp = unpack_packet(f_bytes)
                                if isinstance(f_resp, dict) and f_resp.get("spcode") in (None, 0, 200) and f_resp.get("uid"):
                                    resp = f_resp
                                    spcode = 200
                                    break
                        except Exception:
                            pass

                if spcode is not None and spcode != 0 and spcode != 200:
                    param = resp.get("param")
                    if spcode == 341 and param:
                        mins, secs = divmod(int(param), 60)
                        err_msg = f"Too many attempts. Number on cooldown for {mins}m {secs}s"
                    else:
                        err_msg = SPCODE_MESSAGES.get(spcode, f"Registration failed (Error {spcode})")
                    return {
                        "ok": False,
                        "spcode": spcode,
                        "msg": err_msg,
                        "phone": clean_phone
                    }

                uid = resp.get("uid")
                token = resp.get("token")
                net_server = resp.get("net")
                server_id = resp.get("server", 1)
                sub_id = resp.get("subid", 1)

                # Auto-claim Bind / Newer Gift Bonus
                claimed_bonus = 0.0
                claim_details = []
                final_balance = 0.0
                if net_server:
                    try:
                        claim_res = await self._claim_bonuses_on_node(
                            net_server, uid, server_id, sub_id, token, device_id
                        )
                        claimed_bonus = claim_res.get("total_claimed", 0.0)
                        claim_details = claim_res.get("details", [])
                        final_balance = claim_res.get("final_balance", 0.0)
                    except Exception as e:
                        claim_details.append(f"Node claim err: {str(e)}")

                    try:
                        bal_res = await self.login_async(clean_phone, password)
                        if bal_res.get("ok"):
                            final_balance = bal_res.get("total_balance", 0.0)
                    except Exception:
                        pass

                return {
                    "ok": True,
                    "msg": "Registration Successful & Bonus Claimed!",
                    "uid": uid,
                    "phone": clean_phone,
                    "password": password,
                    "name": name,
                    "token": token,
                    "claimed_bonus": round(claimed_bonus, 2),
                    "details": claim_details,
                    "total_balance": final_balance or round(claimed_bonus, 2)
                }
        except asyncio.TimeoutError:
            return {"ok": False, "msg": "Connection timeout", "phone": clean_phone}
        except Exception as e:
            return {"ok": False, "msg": f"Registration error: {str(e)}", "phone": clean_phone}

    async def _claim_bonuses_on_node(
        self,
        net_server: str,
        uid: int,
        server_id: int,
        sub_id: int,
        token: str,
        device_id: str
    ) -> Dict[str, Any]:
        """
        Internal worker to connect to node server and claim:
        1. MsgId.GET_NEWER_GIFT_REWARDS (665) -> Bind Phone / Welcome Bonus
        2. MsgId.EVENT_CYCLE_SIGN_REWARD (427) -> 7-Day Sign-in Reward
        3. MsgId.EVENT_SIGN_REWARD (419) -> Daily Sign-in Reward
        4. MsgId.REQ_REWARD_TODAY (152) -> Daily Reward
        """
        clean_host = net_server.replace("ws://", "").replace("wss://", "")
        proto = "ws://" if ":" in clean_host else "wss://"
        if not clean_host.endswith("/ws"):
            clean_host += "/ws"
        node_uri = f"{proto}{clean_host}"

        claimed_details = []
        total_claimed = 0.0
        coin = 0.0
        dcoin = 0.0
        ecoin = 0.0
        got_gift_reward = False
        got_wallet_info = False

        async with await self._ws_connect(node_uri) as node_ws:
            node_now = int(time.time() * 1000)
            # Handshake / Login node server
            node_login = {
                "c": MSG_LOGIN_USERID,
                "uid": uid,
                "openid": "",
                "server": server_id,
                "subid": sub_id,
                "token": token,
                "deviceid": device_id,
                "appver": self.app_version,
                "app": self.app_id,
                "bundleid": self.bundle_id,
                "v": self.res_version,
                "c_ts": node_now,
                "c_idx": 1
            }
            await node_ws.send(pack_packet(node_login, node_now))

            # Wait for node login handshake ack
            try:
                ack_bytes = await asyncio.wait_for(node_ws.recv(), timeout=2.0)
                ack_resp = unpack_packet(ack_bytes)
                if ack_resp and isinstance(ack_resp, dict):
                    p_info = ack_resp.get("playerInfo") or {}
                    if p_info:
                        coin = float(p_info.get("coin", 0.0))
                        dcoin = float(p_info.get("dcoin", 0.0))
                        ecoin = float(p_info.get("ecoin", 0.0))
            except Exception:
                pass

            # Bonus 1: Bind Phone / Welcome Gift (665)
            now_1 = int(time.time() * 1000)
            await node_ws.send(pack_packet({"c": MSG_GET_NEWER_GIFT_REWARDS, "uid": uid, "c_ts": now_1, "c_idx": 2}, now_1))

            # Bonus 2: 7-Day Cycle Sign-in (427)
            now_2 = int(time.time() * 1000)
            await node_ws.send(pack_packet({"c": MSG_EVENT_CYCLE_SIGN_REWARD, "uid": uid, "c_ts": now_2, "c_idx": 3}, now_2))

            # Bonus 3: Daily Sign-in Reward (419)
            now_3 = int(time.time() * 1000)
            await node_ws.send(pack_packet({"c": MSG_EVENT_SIGN_REWARD, "uid": uid, "c_ts": now_3, "c_idx": 4}, now_3))

            # Bonus 4: Today Reward (152)
            now_4 = int(time.time() * 1000)
            await node_ws.send(pack_packet({"c": MSG_REQ_REWARD_TODAY, "uid": uid, "c_ts": now_4, "c_idx": 5}, now_4))

            # Bind Referral / Invite Code on Node Server (Msg 28 & Msg 257)
            if self.invite_code:
                now_bind = int(time.time() * 1000)
                await node_ws.send(pack_packet({"c": 28, "code": self.invite_code, "c_ts": now_bind, "c_idx": 6}, now_bind))
                await node_ws.send(pack_packet({"c": 257, "code": self.invite_code, "vid": "-1", "c_ts": now_bind, "c_idx": 7}, now_bind))

            # Refresh wallet (62)
            now_wallet = int(time.time() * 1000)
            await node_ws.send(pack_packet({"c": MSG_PERSIONAL_INFO, "uid": uid, "otheruid": uid, "c_ts": now_wallet, "c_idx": 8}, now_wallet))

            # Gather responses
            end_time = time.time() + 4.0
            while time.time() < end_time:
                try:
                    n_bytes = await asyncio.wait_for(node_ws.recv(), timeout=1.2)
                    n_resp = unpack_packet(n_bytes)
                    if not n_resp or not isinstance(n_resp, dict):
                        continue

                    cmd = n_resp.get("c")
                    sp = n_resp.get("spcode")

                    # Check 665 (Welcome / Bind Phone Bonus)
                    if cmd == MSG_GET_NEWER_GIFT_REWARDS:
                        got_gift_reward = True
                        if n_resp.get("code") == 200 and (not sp or sp == 0):
                            rewards = n_resp.get("rewards") or []
                            r_amt = 0.0
                            for rw in rewards:
                                r_amt += float(rw.get("count", rw.get("coin", rw.get("num", 0.0))))
                            if r_amt > 0:
                                total_claimed += r_amt
                                claimed_details.append(f"🎁 Bind Bonus: ₹{r_amt:.2f}")
                            else:
                                claimed_details.append("🎁 Bind Bonus: Claimed")
                        elif sp:
                            claimed_details.append(f"Bind Bonus: {SPCODE_MESSAGES.get(sp, f'Code {sp}')}")

                    # Check 427 (7-Day Cycle Sign-In)
                    elif cmd == MSG_EVENT_CYCLE_SIGN_REWARD:
                        if n_resp.get("code") == 200 and not sp:
                            rewards = n_resp.get("rewards") or []
                            r_amt = 0.0
                            for rw in rewards:
                                r_amt += float(rw.get("coin", rw.get("num", 0.0)))
                            if r_amt > 0:
                                total_claimed += r_amt
                                claimed_details.append(f"📅 7-Day Bonus: ₹{r_amt:.2f}")
                            else:
                                claimed_details.append("📅 7-Day Bonus: Claimed")
                        elif sp:
                            claimed_details.append(f"7-Day Bonus: {SPCODE_MESSAGES.get(sp, f'Code {sp}')}")

                    # Check 419 (Daily Sign-in)
                    elif cmd == MSG_EVENT_SIGN_REWARD:
                        if n_resp.get("code") == 200 and not sp:
                            claimed_details.append("☀️ Daily Sign-in: Claimed")

                    # Check 152 (Today Reward)
                    elif cmd == MSG_REQ_REWARD_TODAY:
                        if n_resp.get("code") == 200:
                            claimed_details.append("🌟 Today Reward: Checked")

                    if cmd == MSG_PERSIONAL_INFO:
                        got_wallet_info = True
                        p_info = n_resp.get("playerInfo") or {}
                        coin = float(p_info.get("coin", coin))
                        dcoin = float(p_info.get("dcoin", dcoin))
                        ecoin = float(p_info.get("ecoin", ecoin))

                    if got_gift_reward and got_wallet_info:
                        break

                except asyncio.TimeoutError:
                    pass

        total_balance = round(coin + dcoin + ecoin, 2)
        return {
            "total_claimed": round(total_claimed, 2),
            "details": claimed_details,
            "final_balance": total_balance
        }

    async def claim_bonuses_async(self, phone: str, password: str) -> Dict[str, Any]:
        """
        Logs into an account and automatically collects all available bonuses:
        - Bind Phone / Welcome Gift (665)
        - 7-Day Sign-in Reward (427)
        - Daily Sign-in Reward (419)
        - Free Today Bonus (152)
        """
        login_res = await self.login_async(phone, password)
        if not login_res.get("ok"):
            return login_res

        net_server = login_res.get("net_server")
        if not net_server:
            return {"ok": False, "msg": "Node server not provided by login gateway", "phone": phone}

        uid = login_res["uid"]
        server_id = login_res["server_id"]
        sub_id = login_res["sub_id"]
        token = login_res["token"]
        clean_p = clean_phone_number(phone)
        device_id = hashlib.md5(f"device_{clean_p}_{self.platform_id}".encode()).hexdigest()[:16]

        bal_before = login_res.get("total_balance", 0.0)

        claim_info = await self._claim_bonuses_on_node(
            net_server, uid, server_id, sub_id, token, device_id
        )

        # Refresh balance after claim
        refreshed = await self.login_async(phone, password)
        bal_after = refreshed.get("total_balance", bal_before)
        diff = round(bal_after - bal_before, 2)
        if diff < 0:
            diff = 0.0

        return {
            "ok": True,
            "msg": "Bonuses processed",
            "phone": phone,
            "uid": uid,
            "balance_before": bal_before,
            "balance_after": bal_after,
            "claimed_amount": diff if diff > 0 else claim_info.get("total_claimed", 0.0),
            "details": claim_info.get("details", []),
            "account_info": refreshed
        }

    async def bind_phone_and_claim_async(
        self,
        guest_phone_or_user: str,
        guest_pass: str,
        phone_to_bind: str,
        otp: str,
        new_password: str
    ) -> Dict[str, Any]:
        """
        Binds a phone number to an existing account/guest and automatically claims the bind bonus:
        1. Logs into the account.
        2. Sends MsgId.BIND_PHONE (134) on the node server.
        3. Sends MsgId.GET_NEWER_GIFT_REWARDS (665) to credit the Bind Bonus.
        """
        login_res = await self.login_async(guest_phone_or_user, guest_pass)
        if not login_res.get("ok"):
            return login_res

        net_server = login_res.get("net_server")
        if not net_server:
            return {"ok": False, "msg": "Node server not available"}

        uid = login_res["uid"]
        clean_target_phone = clean_phone_number(phone_to_bind)
        device_id = hashlib.md5(f"device_{clean_target_phone}_{self.platform_id}".encode()).hexdigest()[:16]

        node_uri = f"wss://{net_server}/ws" if not net_server.startswith("ws") else net_server
        if not node_uri.endswith("/ws"):
            node_uri += "/ws"

        try:
            async with await self._ws_connect(node_uri) as node_ws:
                node_now = int(time.time() * 1000)
                # Node login
                node_login = {
                    "c": MSG_LOGIN_USERID,
                    "uid": uid,
                    "openid": "",
                    "server": login_res["server_id"],
                    "subid": login_res["sub_id"],
                    "token": login_res["token"],
                    "deviceid": device_id,
                    "appver": self.app_version,
                    "app": self.app_id,
                    "bundleid": self.bundle_id,
                    "v": self.res_version,
                    "c_ts": node_now,
                    "c_idx": 1
                }
                await node_ws.send(pack_packet(node_login, node_now))

                # Send BIND_PHONE (134)
                bind_now = int(time.time() * 1000)
                bind_req = {
                    "c": MSG_BIND_PHONE,
                    "phone": clean_target_phone,
                    "code": str(otp).strip(),
                    "pwd": new_password,
                    "name": f"User_{clean_target_phone[-4:]}",
                    "dinfo": "{}",
                    "c_ts": bind_now,
                    "c_idx": 2
                }
                await node_ws.send(pack_packet(bind_req, bind_now))

                # Send GET_NEWER_GIFT_REWARDS (665)
                claim_now = int(time.time() * 1000)
                await node_ws.send(pack_packet({"c": MSG_GET_NEWER_GIFT_REWARDS, "c_ts": claim_now, "c_idx": 3}, claim_now))

                bind_success = False
                err_msg = ""
                end_time = time.time() + 3.0
                while time.time() < end_time:
                    try:
                        n_bytes = await asyncio.wait_for(node_ws.recv(), timeout=0.8)
                        n_resp = unpack_packet(n_bytes)
                        if not n_resp or not isinstance(n_resp, dict):
                            continue
                        if n_resp.get("c") == MSG_BIND_PHONE:
                            if n_resp.get("code") == 200 and not n_resp.get("spcode"):
                                bind_success = True
                            else:
                                sp = n_resp.get("spcode")
                                err_msg = SPCODE_MESSAGES.get(sp, f"Error {sp}")
                    except asyncio.TimeoutError:
                        break

                if bind_success:
                    # Fetch updated balance
                    bal_res = await self.login_async(clean_target_phone, new_password)
                    return {
                        "ok": True,
                        "msg": "Phone bound & bonus claimed successfully!",
                        "phone": clean_target_phone,
                        "uid": uid,
                        "balance": bal_res.get("total_balance", 0.0)
                    }
                else:
                    return {
                        "ok": False,
                        "msg": err_msg or "Phone binding failed",
                        "phone": clean_target_phone
                    }
        except Exception as e:
            return {"ok": False, "msg": f"Bind connection error: {str(e)}"}

    # Synchronous wrappers
    def login(self, phone: str, password: str) -> Dict[str, Any]:
        return asyncio.run(self.login_async(phone, password))

    def register(self, phone: str, password: str, otp: str, name: Optional[str] = None, invite_code: str = "") -> Dict[str, Any]:
        return asyncio.run(self.register_async(phone, password, otp, name, invite_code))

    def claim_bonuses(self, phone: str, password: str) -> Dict[str, Any]:
        return asyncio.run(self.claim_bonuses_async(phone, password))

    def bind_phone_and_claim(self, guest_user: str, guest_pass: str, phone: str, otp: str, new_pass: str) -> Dict[str, Any]:
        return asyncio.run(self.bind_phone_and_claim_async(guest_user, guest_pass, phone, otp, new_pass))

    @classmethod
    async def check_all_platforms_async(cls, phone: str, timeout: float = 6.0) -> List[Dict[str, Any]]:
        """
        Pings all registered Cocos / Yono platforms in parallel via WebSocket LOGIN (c=1, t=9).
        Categorizes each platform:
        - 'available': Fresh / Unregistered (SPCODE 955)
        - 'registered': Account Exists (SPCODE 333)
        - 'cooldown': Number on cooldown (SPCODE 341)
        - 'other' / 'error': Other response or timeout
        """
        clean_phone = clean_phone_number(phone)
        if len(clean_phone) != 10:
            return []

        async def _check_single(p: Dict[str, Any]) -> Dict[str, Any]:
            c = cls(platform=p, timeout=timeout)
            now = int(time.time() * 1000)
            x_sign = hashlib.md5(f"{now}{c.auth_salt}".encode()).hexdigest()
            device_id = hashlib.md5(f"device_{clean_phone}_{p['id']}".encode()).hexdigest()[:16]

            req = {
                "c": MSG_LOGIN,
                "c_ts": now,
                "x": x_sign,
                "c_idx": 1,
                "user": clean_phone,
                "passwd": "Check_Probe_12345",
                "app": c.app_id,
                "v": c.res_version,
                "av": c.app_version,
                "t": LOGIN_TYPE_PHONE,
                "accessToken": "",
                "platform": "Android",
                "phone": "Xiaomi(13)",
                "code": "",
                "token": "",
                "bwss": 1,
                "LoginExData": 1,
                "language": 2,
                "dlan": "en-US",
                "client_uuid": f"{now}{random.randint(1000000, 9999999)}",
                "bundleid": c.bundle_id,
                "deviceid": device_id,
                "dinfo": "{}"
            }
            try:
                async with await c._ws_connect(c.login_ws_uri) as ws:
                    pkt = pack_packet(req, now)
                    await ws.send(pkt)
                    resp_bytes = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    resp = unpack_packet(resp_bytes)
                    spcode = resp.get("spcode") if isinstance(resp, dict) else None
                    if spcode == 955:
                        status = "available"
                        msg = "Fresh / Available"
                    elif spcode == 333:
                        status = "registered"
                        msg = "Already Registered"
                    elif spcode == 341:
                        status = "cooldown"
                        msg = "Number on cooldown"
                    else:
                        status = "other"
                        msg = SPCODE_MESSAGES.get(spcode, f"Code {spcode}")

                    return {
                        "platform_id": p["id"],
                        "name": p["name"],
                        "status": status,
                        "spcode": spcode,
                        "msg": msg,
                        "invite_code": p.get("invite_code", ""),
                        "platform": p
                    }
            except Exception as e:
                return {
                    "platform_id": p["id"],
                    "name": p["name"],
                    "status": "error",
                    "spcode": None,
                    "msg": str(e),
                    "invite_code": p.get("invite_code", ""),
                    "platform": p
                }

        sem = asyncio.Semaphore(12)

        async def _check_single_safe(p: Dict[str, Any]) -> Dict[str, Any]:
            async with sem:
                try:
                    return await asyncio.wait_for(_check_single(p), timeout=timeout + 1.0)
                except asyncio.TimeoutError:
                    return {
                        "platform_id": p["id"],
                        "name": p["name"],
                        "status": "error",
                        "spcode": None,
                        "msg": "Connection timeout",
                        "invite_code": p.get("invite_code", ""),
                        "platform": p
                    }
                except Exception as e:
                    return {
                        "platform_id": p["id"],
                        "name": p["name"],
                        "status": "error",
                        "spcode": None,
                        "msg": str(e),
                        "invite_code": p.get("invite_code", ""),
                        "platform": p
                    }

        tasks = [_check_single_safe(p) for p in PLATFORMS_LIST]
        return await asyncio.gather(*tasks)

    @classmethod
    def check_all_platforms(cls, phone: str, timeout: float = 6.0) -> List[Dict[str, Any]]:
        return asyncio.run(cls.check_all_platforms_async(phone, timeout=timeout))


def bulk_check_accounts(
    accounts: List[Tuple[str, str]],
    max_workers: int = 5,
    progress_callback = None
) -> Dict[str, Any]:
    """Checks multiple accounts in parallel using a thread pool."""
    total = len(accounts)
    results = []
    success_count = 0
    fail_count = 0
    total_cash = 0.0

    def _worker(item):
        phone, password = item
        client = MaxRummyClient()
        return client.login(phone, password)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_worker, acc): acc for acc in accounts}
        done_count = 0
        for future in futures:
            res = future.result()
            results.append(res)
            done_count += 1
            if res.get("ok"):
                success_count += 1
                total_cash += res.get("coin", 0.0)
            else:
                fail_count += 1

            if progress_callback:
                progress_callback(done_count, total, res)

    return {
        "total": total,
        "success": success_count,
        "failed": fail_count,
        "total_cash": round(total_cash, 2),
        "results": results
    }


def bulk_claim_bonuses(
    accounts: List[Tuple[str, str]],
    max_workers: int = 5,
    progress_callback = None
) -> Dict[str, Any]:
    """Claims bonuses on multiple accounts in parallel using a thread pool."""
    total = len(accounts)
    results = []
    success_count = 0
    fail_count = 0
    total_claimed = 0.0

    def _worker(item):
        phone, password = item
        client = MaxRummyClient()
        return client.claim_bonuses(phone, password)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_worker, acc): acc for acc in accounts}
        done_count = 0
        for future in futures:
            res = future.result()
            results.append(res)
            done_count += 1
            if res.get("ok"):
                success_count += 1
                total_claimed += res.get("claimed_amount", 0.0)
            else:
                fail_count += 1

            if progress_callback:
                progress_callback(done_count, total, res)

    return {
        "total": total,
        "success": success_count,
        "failed": fail_count,
        "total_claimed": round(total_claimed, 2),
        "results": results
    }


if __name__ == "__main__":
    print("=== MaxRummy / YonoGames Client Test ===")
    test_phone = "9876543210"
    test_pass = "Test12345"

    client = MaxRummyClient()
    print(f"Testing Login on {test_phone}...")
    res = client.login(test_phone, test_pass)
    print("Result:", json.dumps(res, indent=2))
