#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Configuration for MaxRummy / YonoGames Automation Bot
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent.resolve()

def get_data_dir() -> Path:
    jrnm_vol = Path("/app/data")
    if jrnm_vol.exists() and os.path.isdir(jrnm_vol):
        return jrnm_vol
    return BASE_DIR

DATA_DIR = get_data_dir()

# Telegram Bot Token (can be overridden via environment variable)
BOT_TOKEN = os.getenv("MAXRUMMY_BOT_TOKEN") or os.getenv("BOT_TOKEN") or "8948124151:AAHDfoNjx_VBdIhn3KvS-87JrlX5Nv3xYvs"

# Owner and Support Contacts
OWNER_ID = 8327850869
SUPPORT_USERNAMES = ["@OwAtom", "@OwKarma"]

# Files
ACCOUNTS_FILE = DATA_DIR / "registered_accounts.txt"
CHECKED_ACCOUNTS_FILE = DATA_DIR / "checked_accounts.txt"
ADMIN_SETTINGS_FILE = DATA_DIR / "admin_settings.json"
