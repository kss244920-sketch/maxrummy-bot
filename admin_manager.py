"""
Admin and Multi-User Management Module for Jio-Gemini Scanner Bot.
Supports Super Admin (8327850869), user registry, whitelisting, role-based access,
broadcast messaging, free mode, and dual-channel isolation (Master Channel hidden from members).
"""

import os
import json
import time
import shutil
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Any

IST = timezone(timedelta(hours=5, minutes=30))

def get_current_ist_date() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")

BASE_DIR = Path(__file__).parent.resolve()

def get_persistent_data_dir() -> Path:
    # JRNM persistent volume mount path: /app/data
    jrnm_vol = Path("/app/data")
    if jrnm_vol.exists() and os.path.isdir(jrnm_vol):
        return jrnm_vol
    return BASE_DIR

ADMIN_SETTINGS_FILE = get_persistent_data_dir() / "admin_settings.json"
AUDIT_LEDGER_FILE = get_persistent_data_dir() / "audit_ledger.jsonl"

DEFAULT_SUPER_ADMIN = 8327850869
DEFAULT_SUPER_ADMINS: Set[int] = {8327850869, 8219398815}
DEFAULT_MASTER_STORAGE_CHANNEL = "-1003907841421"
DEFAULT_MASTER_LOG_CHANNEL = "-1003946923981"
ADMIN_DAILY_LIMIT = 1000


class AdminManager:
    def __init__(self, filepath: Path = ADMIN_SETTINGS_FILE):
        self.filepath = filepath
        self._lock = threading.RLock()
        self.super_admin_id: int = DEFAULT_SUPER_ADMIN
        self.super_admin_ids: Set[int] = {8327850869, 8219398815}
        self.admin_user_ids: Set[int] = {8327850869, 8219398815, 6217522461}
        self.whitelisted_user_ids: Set[int] = set()
        self.master_storage_channel: str = DEFAULT_MASTER_STORAGE_CHANNEL
        self.master_log_channel: str = DEFAULT_MASTER_LOG_CHANNEL
        self.free_mode: bool = False
        self.maintenance_mode: bool = False
        self.rate_limit_enabled: bool = True
        self.rate_limit_seconds: float = 1.0
        self.user_registry: Dict[str, dict] = {}
        self.admin_daily_usage: Dict[str, dict] = {}
        self.bot_start_time: float = time.time()

        # Seed from BASE_DIR if persistent location is fresh and empty
        base_fallback = BASE_DIR / "admin_settings.json"
        if not self.filepath.exists() and base_fallback.exists() and self.filepath != base_fallback:
            try:
                shutil.copy(base_fallback, self.filepath)
                print(f"[AdminManager] Initialized persistent settings from {base_fallback} -> {self.filepath}")
            except Exception as e:
                print(f"[AdminManager] Failed to copy fallback settings: {e}")

        self.load()
        self.check_and_refresh_admin_quotas()
        self._start_refresh_daemon()

    def _start_refresh_daemon(self):
        def _loop():
            while True:
                time.sleep(60)
                try:
                    self.check_and_refresh_admin_quotas()
                except Exception:
                    pass
        t = threading.Thread(target=_loop, daemon=True, name="AdminQuotaRefreshDaemon")
        t.start()

    def is_free_mode(self) -> bool:
        return bool(self.free_mode)

    def set_free_mode(self, enabled: bool) -> bool:
        self.free_mode = bool(enabled)
        self.save()
        return self.free_mode

    def toggle_free_mode(self) -> bool:
        self.free_mode = not self.free_mode
        self.save()
        return self.free_mode

    def is_maintenance_mode(self) -> bool:
        return bool(getattr(self, "maintenance_mode", False))

    def set_maintenance_mode(self, enabled: bool) -> bool:
        self.maintenance_mode = bool(enabled)
        self.save()
        return self.maintenance_mode

    def toggle_maintenance_mode(self) -> bool:
        self.maintenance_mode = not self.is_maintenance_mode()
        self.save()
        return self.maintenance_mode

    def is_rate_limit_enabled(self) -> bool:
        return bool(getattr(self, "rate_limit_enabled", True))

    def set_rate_limit_enabled(self, enabled: bool) -> bool:
        self.rate_limit_enabled = bool(enabled)
        self.save()
        return self.rate_limit_enabled

    def toggle_rate_limit(self) -> bool:
        self.rate_limit_enabled = not self.is_rate_limit_enabled()
        self.save()
        return self.rate_limit_enabled

    def load(self):
        env_admin = os.getenv("ADMIN_USER_IDS", "").strip()
        if env_admin:
            for uid in env_admin.replace(",", " ").split():
                c = uid.strip().lstrip("-")
                if c.isdigit():
                    self.admin_user_ids.add(int(c))

        env_master_log = os.getenv("LOG_CHANNEL_ID", "").strip()
        if env_master_log:
            self.master_log_channel = env_master_log

        env_master_storage = os.getenv("STORAGE_CHANNEL_ID", "").strip()
        if env_master_storage:
            self.master_storage_channel = env_master_storage

        if not self.filepath.exists():
            self.save()
            return

        try:
            with open(self.filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            self.free_mode = bool(data.get("free_mode", False))
            self.maintenance_mode = bool(data.get("maintenance_mode", False))
            self.rate_limit_enabled = bool(data.get("rate_limit_enabled", True))
            self.rate_limit_seconds = float(data.get("rate_limit_seconds", 1.0))
            self.master_log_channel = str(data.get("master_log_channel") or self.master_log_channel).strip()
            self.master_storage_channel = str(data.get("master_storage_channel") or self.master_storage_channel).strip()

            admins = data.get("admin_user_ids", list(self.super_admin_ids))
            self.admin_user_ids = {int(x) for x in admins if str(x).lstrip("-").isdigit()}
            for s_id in self.super_admin_ids:
                self.admin_user_ids.add(s_id)

            users = data.get("whitelisted_user_ids", [])
            self.whitelisted_user_ids = {int(x) for x in users if str(x).lstrip("-").isdigit()}

            self.user_registry = data.get("user_registry", {})
            self.admin_daily_usage = data.get("admin_daily_usage", {})
            needs_save = False

            # Ensure Super Admin / Owner (8327850869 - @owatom) has full Owner role & unlimited tokens
            self.super_admin_ids = set(DEFAULT_SUPER_ADMINS)
            s_str = str(self.super_admin_id)
            if s_str in self.user_registry:
                if self.user_registry[s_str].get("role") != "👑 Owner / Super Admin" or self.user_registry[s_str].get("tokens") != 999999 or self.user_registry[s_str].get("username") != "OwAtom":
                    self.user_registry[s_str]["role"] = "👑 Owner / Super Admin"
                    self.user_registry[s_str]["tokens"] = 999999
                    self.user_registry[s_str]["username"] = "OwAtom"
                    needs_save = True
            else:
                self.user_registry[s_str] = {
                    "user_id": self.super_admin_id,
                    "username": "OwAtom",
                    "first_name": "Owner",
                    "role": "👑 Owner / Super Admin",
                    "tokens": 999999,
                    "joined_at": time.time(),
                    "last_active": time.time(),
                    "user_log_channel": None,
                    "user_storage_channel": None
                }
                needs_save = True

            # Ensure Piyush (8219398815) has full Owner / Super Admin role & unlimited tokens
            p_id = 8219398815
            p_str = str(p_id)
            self.super_admin_ids.add(p_id)
            self.admin_user_ids.add(p_id)
            if p_str in self.user_registry:
                if self.user_registry[p_str].get("role") != "👑 Owner / Super Admin" or self.user_registry[p_str].get("tokens") != 999999:
                    self.user_registry[p_str]["role"] = "👑 Owner / Super Admin"
                    self.user_registry[p_str]["tokens"] = 999999
                    needs_save = True
            else:
                self.user_registry[p_str] = {
                    "user_id": p_id,
                    "username": "Piyush",
                    "first_name": "Piyush",
                    "role": "👑 Owner / Super Admin",
                    "tokens": 999999,
                    "joined_at": time.time(),
                    "last_active": time.time(),
                    "user_log_channel": None,
                    "user_storage_channel": None
                }
                needs_save = True

            # Ensure @gurjar040 (6217522461) is Admin ONLY (NOT Owner) with 1,000 daily quota
            g_id = 6217522461
            g_str = str(g_id)
            self.admin_user_ids.add(g_id)
            if g_id in self.super_admin_ids:
                self.super_admin_ids.discard(g_id)

            if g_str in self.user_registry:
                if self.user_registry[g_str].get("role") != "🛡️ Admin" or self.user_registry[g_str].get("tokens") == 999999:
                    self.user_registry[g_str]["role"] = "🛡️ Admin"
                    self.user_registry[g_str]["username"] = "gurjar040"
                    rem = self.get_admin_remaining_daily_tokens(g_id)
                    self.user_registry[g_str]["tokens"] = rem if rem > 0 else ADMIN_DAILY_LIMIT
                    needs_save = True
            else:
                self.user_registry[g_str] = {
                    "user_id": g_id,
                    "username": "gurjar040",
                    "first_name": "Prince",
                    "role": "🛡️ Admin",
                    "tokens": ADMIN_DAILY_LIMIT,
                    "joined_at": time.time(),
                    "last_active": time.time(),
                    "user_log_channel": None,
                    "user_storage_channel": None
                }
                needs_save = True

            # Ensure Dino, GoLu, and 8659204794 are strictly Free Users (not Admins) as requested
            for demote_id in [1879784162, 7812349806, 8659204794]:
                if demote_id in self.admin_user_ids:
                    self.admin_user_ids.discard(demote_id)
                    needs_save = True
                uid_str = str(demote_id)
                if uid_str in self.user_registry:
                    if self.user_registry[uid_str].get("role") != "⚪ Free User":
                        self.user_registry[uid_str]["role"] = "⚪ Free User"
                        needs_save = True
            if needs_save:
                self.save()
            self.check_and_refresh_admin_quotas()


        except Exception as e:
            print(f"[AdminManager] Error loading settings: {e}")

    def save(self):
        with self._lock:
            data = {
                "free_mode": self.free_mode,
                "maintenance_mode": self.is_maintenance_mode(),
                "rate_limit_enabled": self.is_rate_limit_enabled(),
                "rate_limit_seconds": getattr(self, "rate_limit_seconds", 1.0),
                "master_log_channel": self.master_log_channel,
                "master_storage_channel": self.master_storage_channel,
                "admin_user_ids": list(self.admin_user_ids),
                "whitelisted_user_ids": list(self.whitelisted_user_ids),
                "user_registry": self.user_registry,
                "admin_daily_usage": self.admin_daily_usage,
            }
            tmp_file = self.filepath.with_suffix(f".tmp.{os.getpid()}.{time.time_ns()}")
            try:
                with open(tmp_file, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
                tmp_file.replace(self.filepath)
            except Exception as e:
                print(f"[AdminManager] Error saving settings: {e}")
                try:
                    if tmp_file.exists():
                        tmp_file.unlink()
                except Exception:
                    pass

            # Secondary mirror to BASE_DIR if running with custom persistent filepath
            base_file = BASE_DIR / "admin_settings.json"
            if self.filepath != base_file:
                try:
                    with open(base_file, "w", encoding="utf-8") as f_base:
                        json.dump(data, f_base, indent=2)
                except Exception:
                    pass

    def record_user(self, user_id: int, username: str = "", first_name: str = ""):
        if not user_id:
            return
        with self._lock:
            uid_str = str(user_id)
            now = time.time()
            role = self.get_user_role_label(user_id)
            if uid_str not in self.user_registry:
                self.user_registry[uid_str] = {
                    "user_id": user_id,
                    "username": username or "",
                    "first_name": first_name or "",
                    "role": role,
                    "tokens": 0,
                    "joined_at": now,
                    "last_active": now,
                    "user_log_channel": None,
                    "user_storage_channel": None,
                }
            else:
                if username:
                    self.user_registry[uid_str]["username"] = username
                if first_name:
                    self.user_registry[uid_str]["first_name"] = first_name
                if "tokens" not in self.user_registry[uid_str]:
                    self.user_registry[uid_str]["tokens"] = 0
                self.user_registry[uid_str]["last_active"] = now
                self.user_registry[uid_str]["role"] = role
            self.save()

    def _to_int(self, user_id: Any) -> Optional[int]:
        try:
            return int(user_id)
        except (ValueError, TypeError):
            return None

    def get_user_role_label(self, user_id: Any) -> str:
        uid = self._to_int(user_id)
        if not uid:
            return "⚪ Guest"
        if self.is_super_admin(uid):
            return "👑 Owner / Super Admin"
        if uid in self.admin_user_ids:
            return "🛡️ Admin"
        if uid in self.whitelisted_user_ids:
            return "👤 Member"
        return "⚪ Guest"

    def is_super_admin(self, user_id: Any) -> bool:
        uid = self._to_int(user_id)
        if not uid:
            return False
        return uid in self.super_admin_ids or uid == self.super_admin_id

    def is_admin(self, user_id: Any) -> bool:
        uid = self._to_int(user_id)
        if not uid:
            return False
        return self.is_super_admin(uid) or uid in self.admin_user_ids

    def is_whitelisted(self, user_id: Any) -> bool:
        uid = self._to_int(user_id)
        if not uid:
            return False
        return uid in self.whitelisted_user_ids

    def has_access(self, user_id: Any) -> bool:
        return bool(self._to_int(user_id))

    def get_user_by_username(self, username_or_tag: str) -> Optional[int]:
        if not username_or_tag:
            return None
        tag = username_or_tag.strip().lstrip("@").lower()
        with self._lock:
            for uid_str, data in list(self.user_registry.items()):
                u_name = str(data.get("username") or "").strip().lower()
                if u_name == tag:
                    return int(uid_str) if uid_str.lstrip("-").isdigit() else None
        return None

    def add_admin(self, user_id: Any, username: str = "", first_name: str = ""):
        uid = self._to_int(user_id)
        if not uid:
            return
        with self._lock:
            self.admin_user_ids.add(uid)
            if uid in self.whitelisted_user_ids:
                self.whitelisted_user_ids.remove(uid)
            self.record_user(uid, username, first_name)
            self.save()

    def remove_admin(self, user_id: Any):
        uid = self._to_int(user_id)
        if not uid or uid == self.super_admin_id:
            return
        with self._lock:
            if uid in self.admin_user_ids:
                self.admin_user_ids.remove(uid)
                uid_str = str(uid)
                if uid_str in self.user_registry:
                    self.user_registry[uid_str]["role"] = self.get_user_role_label(uid)
                self.save()

    def add_user(self, user_id: Any, username: str = "", first_name: str = ""):
        uid = self._to_int(user_id)
        if not uid:
            return
        with self._lock:
            self.whitelisted_user_ids.add(uid)
            self.record_user(uid, username, first_name)
            self.save()

    def remove_user(self, user_id: Any):
        uid = self._to_int(user_id)
        if not uid:
            return
        with self._lock:
            if uid in self.whitelisted_user_ids:
                self.whitelisted_user_ids.remove(uid)
                uid_str = str(uid)
                if uid_str in self.user_registry:
                    self.user_registry[uid_str]["role"] = self.get_user_role_label(uid)
                self.save()

    def get_master_log_channel(self) -> str:
        return self.master_log_channel

    def set_master_log_channel(self, ch_id: str):
        with self._lock:
            self.master_log_channel = str(ch_id).strip()
            self.save()

    def get_master_storage_channel(self) -> str:
        return self.master_storage_channel

    def set_master_storage_channel(self, ch_id: str):
        with self._lock:
            self.master_storage_channel = str(ch_id).strip()
            self.save()

    def get_user_log_channel(self, user_id: int) -> Optional[str]:
        uid_str = str(user_id)
        with self._lock:
            if uid_str in self.user_registry:
                return self.user_registry[uid_str].get("user_log_channel")
            return None

    def set_user_log_channel(self, user_id: int, ch_id: Optional[str]):
        uid_str = str(user_id)
        with self._lock:
            if uid_str not in self.user_registry:
                self.record_user(user_id)
            self.user_registry[uid_str]["user_log_channel"] = str(ch_id).strip() if ch_id else None
            self.save()

    def get_user_storage_channel(self, user_id: int) -> Optional[str]:
        uid_str = str(user_id)
        with self._lock:
            if uid_str in self.user_registry:
                return self.user_registry[uid_str].get("user_storage_channel")
            return None

    def set_user_storage_channel(self, user_id: int, ch_id: Optional[str]):
        uid_str = str(user_id)
        with self._lock:
            if uid_str not in self.user_registry:
                self.record_user(user_id)
            self.user_registry[uid_str]["user_storage_channel"] = str(ch_id).strip() if ch_id else None
            self.save()

    def get_display_log_channel(self, user_id: int) -> str:
        user_ch = self.get_user_log_channel(user_id)
        if user_ch:
            return user_ch
        client_ch = os.getenv("CLIENT_LOG_CHANNEL")
        if client_ch:
            return client_ch
        if self.is_super_admin(user_id):
            return f"{self.master_log_channel} (Master)"
        return "Not Set"

    def get_display_storage_channel(self, user_id: int) -> str:
        user_ch = self.get_user_storage_channel(user_id)
        if user_ch:
            return user_ch
        client_ch = os.getenv("CLIENT_STORAGE_CHANNEL")
        if client_ch:
            return client_ch
        if self.is_super_admin(user_id):
            return f"{self.master_storage_channel} (Master)"
        return "Not Set"

    def get_client_admin_ids(self) -> List[int]:
        """Returns all client admins (excluding hidden super admin)."""
        with self._lock:
            return [uid for uid in self.admin_user_ids if uid != self.super_admin_id]

    def get_primary_client_admin(self) -> int:
        """Returns primary client admin ID."""
        admins = self.get_client_admin_ids()
        if admins:
            return admins[0]
        return self.super_admin_id

    def get_effective_log_channel(self, user_id: int) -> Optional[str]:
        user_ch = self.get_user_log_channel(user_id)
        if user_ch:
            return user_ch
        client_ch = os.getenv("CLIENT_LOG_CHANNEL")
        if client_ch:
            return client_ch
        if self.is_super_admin(user_id) and self.master_log_channel:
            return self.master_log_channel
        return None

    def get_effective_storage_channel(self, user_id: int) -> Optional[str]:
        user_ch = self.get_user_storage_channel(user_id)
        if user_ch:
            return user_ch
        client_ch = os.getenv("CLIENT_STORAGE_CHANNEL")
        if client_ch:
            return client_ch
        if self.is_super_admin(user_id) and self.master_storage_channel:
            return self.master_storage_channel
        return None

    def has_storage_channel(self, user_id: int) -> bool:
        if self.is_super_admin(user_id):
            return bool(self.master_storage_channel or self.get_user_storage_channel(user_id))
        user_ch = self.get_user_storage_channel(user_id)
        if user_ch and user_ch.strip():
            return True
        if os.getenv("CLIENT_STORAGE_CHANNEL"):
            return True
        return False

    def get_user_by_storage_channel(self, channel_id: Any) -> Optional[int]:
        if not channel_id:
            return None
        ch_str = str(channel_id).strip()
        with self._lock:
            for uid_str, data in list(self.user_registry.items()):
                if data.get("user_storage_channel") and str(data.get("user_storage_channel")).strip() == ch_str:
                    if uid_str.lstrip("-").isdigit():
                        return int(uid_str)
            if self.master_storage_channel and str(self.master_storage_channel).strip() == ch_str:
                return self.super_admin_id
        return None

    def get_all_target_chat_ids(self) -> Set[int]:
        with self._lock:
            targets = set(self.admin_user_ids).union(self.whitelisted_user_ids)
            for uid_str in list(self.user_registry.keys()):
                if uid_str.lstrip("-").isdigit():
                    targets.add(int(uid_str))
        return targets

    def check_and_refresh_admin_quotas(self) -> bool:
        """
        Proactively refreshes admin daily quotas if a new day (IST) has started or 24 hours elapsed.
        Resets used count to 0 and tokens to ADMIN_DAILY_LIMIT (1,000) for all client admins.
        """
        with self._lock:
            today_str = get_current_ist_date()
            now = time.time()
            changed = False
            for admin_id in list(self.admin_user_ids):
                if admin_id == self.super_admin_id:
                    continue
                aid_str = str(admin_id)
                rec = self.admin_daily_usage.get(aid_str, {})
                last_reset = rec.get("last_reset", 0)
                is_new_day = rec.get("date") != today_str
                is_24h_passed = (now - last_reset) >= 86400 if last_reset else False

                if is_new_day or is_24h_passed:
                    self.admin_daily_usage[aid_str] = {
                        "date": today_str,
                        "used": 0,
                        "last_reset": now
                    }
                    if aid_str in self.user_registry:
                        self.user_registry[aid_str]["tokens"] = ADMIN_DAILY_LIMIT
                    changed = True
            if changed:
                self.save()
            return changed

    def get_all_accounts(self, calling_user_id: Optional[int] = None) -> List[dict]:
        self.check_and_refresh_admin_quotas()
        accounts = []
        with self._lock:
            for uid_str, data in list(self.user_registry.items()):
                uid = int(uid_str) if uid_str.lstrip("-").isdigit() else uid_str
                # Silently hide universal super admin from non-super admins
                if calling_user_id != self.super_admin_id and uid == self.super_admin_id:
                    continue
                accounts.append({
                    "user_id": uid,
                    "username": data.get("username") or "",
                    "first_name": data.get("first_name") or "User",
                    "role": self.get_user_role_label(uid if isinstance(uid, int) else 0),
                    "tokens": self.get_display_tokens(uid if isinstance(uid, int) else 0),
                    "raw_tokens": int(data.get("tokens", 0)),
                    "joined_at": data.get("joined_at", 0),
                    "last_active": data.get("last_active", 0)
                })
        return accounts

    def get_admin_remaining_daily_tokens(self, user_id: Any) -> int:
        uid = self._to_int(user_id)
        if not uid:
            return 0
        if uid == self.super_admin_id:
            return 999999
        self.check_and_refresh_admin_quotas()
        uid_str = str(uid)
        with self._lock:
            record = self.admin_daily_usage.get(uid_str, {})
            used = record.get("used", 0)
            return max(0, ADMIN_DAILY_LIMIT - used)

    def record_admin_daily_usage(self, user_id: Any, amount: int = 1) -> int:
        uid = self._to_int(user_id)
        if not uid or uid == self.super_admin_id:
            return 999999
        self.check_and_refresh_admin_quotas()
        uid_str = str(uid)
        today_str = get_current_ist_date()
        with self._lock:
            record = self.admin_daily_usage.get(uid_str, {})
            if record.get("date") != today_str:
                record = {"date": today_str, "used": 0, "last_reset": time.time()}
            record["used"] = record.get("used", 0) + amount
            self.admin_daily_usage[uid_str] = record
            rem = max(0, ADMIN_DAILY_LIMIT - record["used"])
            if uid_str in self.user_registry:
                self.user_registry[uid_str]["tokens"] = rem
            self.save()
            return rem

    def get_tokens(self, user_id: Any) -> int:
        uid = self._to_int(user_id)
        if not uid:
            return 0
        if self.is_super_admin(uid):
            return 999999
        if self.is_admin(uid):
            return self.get_admin_remaining_daily_tokens(uid)
        self.check_and_refresh_admin_quotas()
        with self._lock:
            uid_str = str(uid)
            if uid_str in self.user_registry:
                return int(self.user_registry[uid_str].get("tokens", 0))
            return 0

    def get_display_tokens(self, user_id: Any) -> str:
        uid = self._to_int(user_id)
        if not uid:
            return "0 Tokens"
        if self.is_super_admin(uid):
            return "👑 Unlimited (Owner / Super Admin)"
        if self.is_admin(uid):
            rem = self.get_admin_remaining_daily_tokens(uid)
            return f"🛡️ {rem:,} / {ADMIN_DAILY_LIMIT:,} (Daily Admin Quota)"
        tokens = self.get_tokens(uid)
        if self.is_free_mode():
            return f"{tokens:,} Token{'s' if tokens != 1 else ''} <i>(🆓 Free Mode)</i>"
        return f"{tokens:,} Token{'s' if tokens != 1 else ''}"

    def has_enough_tokens(self, user_id: Any, amount: int = 1) -> bool:
        uid = self._to_int(user_id)
        if not uid:
            return False
        if self.is_super_admin(uid):
            return True
        if self.is_admin(uid):
            return self.get_admin_remaining_daily_tokens(uid) >= amount
        if self.is_free_mode():
            return True
        return self.get_tokens(uid) >= amount

    def add_tokens(self, user_id: Any, amount: int, reason: str = "") -> int:
        uid = self._to_int(user_id)
        if not uid:
            return 0
        with self._lock:
            uid_str = str(uid)
            if uid_str not in self.user_registry:
                self.record_user(uid)
            current = int(self.user_registry[uid_str].get("tokens", 0))
            new_val = max(0, current + amount)
            self.user_registry[uid_str]["tokens"] = new_val
            if uid in self.admin_user_ids and not self.is_super_admin(uid):
                rec = self.admin_daily_usage.get(uid_str, {})
                if rec:
                    rec["used"] = max(0, rec.get("used", 0) - amount)
                    self.admin_daily_usage[uid_str] = rec
            self.save()

            self.log_ledger_event(
                user_id=uid,
                action="TOKEN_ADD",
                change=+amount,
                tokens_after=new_val,
                reason=reason or "Tokens added"
            )
            return new_val

    def deduct_tokens(self, user_id: Any, amount: int = 1, force: bool = False, reason: str = "") -> int:
        uid = self._to_int(user_id)
        if not uid:
            return 0
        if self.is_super_admin(uid) and not force:
            self.log_ledger_event(
                user_id=uid,
                action="FEATURE_EXEC",
                change=0,
                tokens_after=999999,
                reason=reason or "Owner Execution (Unlimited)"
            )
            return 999999
        if self.is_free_mode() and not force:
            tok = self.get_tokens(uid)
            self.log_ledger_event(
                user_id=uid,
                action="FREE_MODE_EXEC",
                change=0,
                tokens_after=tok,
                reason=reason or "Free Mode Execution"
            )
            return tok

        with self._lock:
            uid_str = str(uid)
            if uid_str not in self.user_registry:
                self.record_user(uid)

            if uid in self.admin_user_ids and not self.is_super_admin(uid):
                today_str = get_current_ist_date()
                rec = self.admin_daily_usage.get(uid_str, {})
                if rec.get("date") != today_str:
                    rec = {"date": today_str, "used": 0, "last_reset": time.time()}
                rec["used"] = rec.get("used", 0) + amount
                if "last_reset" not in rec:
                    rec["last_reset"] = time.time()
                self.admin_daily_usage[uid_str] = rec
                new_val = max(0, ADMIN_DAILY_LIMIT - rec["used"])
                self.user_registry[uid_str]["tokens"] = new_val
            else:
                current = int(self.user_registry[uid_str].get("tokens", 0))
                new_val = max(0, current - amount)
                self.user_registry[uid_str]["tokens"] = new_val

            self.save()

            self.log_ledger_event(
                user_id=uid,
                action="TOKEN_DEDUCT",
                change=-amount,
                tokens_after=new_val,
                reason=reason or "Feature usage"
            )
            return new_val

    def set_tokens(self, user_id: Any, amount: int, reason: str = "") -> int:
        uid = self._to_int(user_id)
        if not uid:
            return 0
        with self._lock:
            uid_str = str(uid)
            if uid_str not in self.user_registry:
                self.record_user(uid)
            new_val = max(0, amount)
            self.user_registry[uid_str]["tokens"] = new_val
            if uid in self.admin_user_ids and not self.is_super_admin(uid):
                today_str = get_current_ist_date()
                rec = self.admin_daily_usage.get(uid_str, {})
                if rec.get("date") != today_str:
                    rec = {"date": today_str, "used": 0, "last_reset": time.time()}
                rec["used"] = max(0, ADMIN_DAILY_LIMIT - new_val)
                self.admin_daily_usage[uid_str] = rec
            self.save()

            self.log_ledger_event(
                user_id=uid,
                action="TOKEN_SET",
                change=0,
                tokens_after=new_val,
                reason=reason or f"Tokens set to {amount}"
            )
            return new_val

    def log_ledger_event(
        self,
        user_id: int,
        action: str,
        change: int,
        tokens_after: int,
        reason: str = "",
        extra: Optional[dict] = None
    ):
        """
        Records an immutable transaction entry into the Owner Audit Ledger.
        """
        try:
            now_ist = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
            uid_str = str(user_id)
            user_data = self.user_registry.get(uid_str, {})
            username = user_data.get("username") or ""
            first_name = user_data.get("first_name") or ""
            role = self.get_user_role_label(user_id)

            entry = {
                "timestamp": now_ist,
                "epoch": int(time.time()),
                "user_id": user_id,
                "username": username,
                "first_name": first_name,
                "role": role,
                "action": action,
                "change": change,
                "tokens_after": tokens_after,
                "reason": reason,
                "extra": extra or {}
            }

            AUDIT_LEDGER_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(AUDIT_LEDGER_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            print(f"[AdminManager] Failed to log ledger event: {e}")

    def get_recent_ledger(self, limit: int = 15, filter_user_id: Optional[int] = None) -> List[dict]:
        """
        Returns the last `limit` events from the audit ledger (newest first).
        """
        if not AUDIT_LEDGER_FILE.exists():
            return []
        events = []
        try:
            with open(AUDIT_LEDGER_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        if filter_user_id is not None and ev.get("user_id") != filter_user_id:
                            continue
                        events.append(ev)
                    except Exception:
                        pass
            return events[-limit:][::-1]
        except Exception as e:
            print(f"[AdminManager] Error reading ledger: {e}")
            return []

    def export_ledger_text(self, filter_user_id: Optional[int] = None) -> str:
        """
        Generates a full human-readable audit ledger report for export.
        """
        if not AUDIT_LEDGER_FILE.exists():
            return "No audit transactions recorded yet."
        lines = [
            "================================================================================",
            "           MAXRUMMY & YONOGAMES BOT — OWNER AUDIT & TOKEN LEDGER                ",
            f"           Generated: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}    ",
            f"           Super Admin: 8327850869 (@OwAtom)                                    ",
            "================================================================================\n",
            f"{'TIMESTAMP':<20} | {'USER ID':<12} | {'USERNAME':<15} | {'ACTION':<14} | {'CHANGE':<8} | {'BALANCE':<8} | {'REASON / PLATFORM'}",
            "-" * 115
        ]
        try:
            with open(AUDIT_LEDGER_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                        if filter_user_id is not None and ev.get("user_id") != filter_user_id:
                            continue
                        ts = ev.get("timestamp", "N/A")
                        uid = str(ev.get("user_id", "N/A"))
                        u_name = "@" + ev.get("username") if ev.get("username") else (ev.get("first_name") or "User")
                        act = ev.get("action", "EVENT")
                        chg = f"{ev.get('change', 0):+d} T" if ev.get('change') is not None else "0 T"
                        bal = f"{ev.get('tokens_after', 0)} T"
                        rsn = ev.get("reason", "")
                        lines.append(f"{ts:<20} | {uid:<12} | {u_name:<15} | {act:<14} | {chg:<8} | {bal:<8} | {rsn}")
                    except Exception:
                        pass
            return "\n".join(lines)
        except Exception as e:
            return f"Error exporting ledger: {e}"

    def get_total_distributed_tokens(self) -> int:
        total = 0
        with self._lock:
            for uid_str, data in list(self.user_registry.items()):
                if uid_str.lstrip("-").isdigit():
                    uid = int(uid_str)
                    if not self.is_admin(uid):
                        total += int(data.get("tokens", 0))
        return total

admin_manager = AdminManager()

