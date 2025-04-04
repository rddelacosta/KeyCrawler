#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Telegram Channel Discovery Extension for KeyBoxer

import os
import re
import sys
import json
import sqlite3
import asyncio
import logging
import random
from pathlib import Path
from dotenv import load_dotenv

# Telethon imports
from telethon import TelegramClient
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import SearchGlobalRequest, ImportChatInviteRequest
from telethon.tl.types import InputMessagesFilterUrl, InputPeerEmpty
from telethon.errors import (
    ChatAdminRequiredError, 
    ChannelPrivateError, 
    InviteHashInvalidError, 
    FloodWaitError
)
from telethon.sessions import StringSession
from telethon.tl.functions.messages import CheckChatInviteRequest, SearchGlobalRequest
from telethon.errors import ChatAdminRequiredError, ChannelPrivateError, InviteHashInvalidError

# Load environment variables
load_dotenv()
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE")
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")

# Paths
BASE_DIR = Path(__file__).resolve().parent
TELEGRAM_SESSION_DIR = BASE_DIR / "telegram_session"
TELEGRAM_DB = BASE_DIR / "telegram_data.db"

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("telegram_discovery.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("telegram_discovery")

# Patterns and related terms
INVITE_LINK_PATTERN = re.compile(r't\.me/[+]([\w-]+)')
CHANNEL_LINK_PATTERN = re.compile(r't\.me/([\w_]+)')
KEYBOX_RELATED_TERMS = [
    'keybox', 'attestation', 'android key', 'safetynet', 'integrity', 
    'play integrity', 'strongbox', 'keymaster', 'android security',
    'android root', 'magisk', 'custom rom', 'rooted device'
]

def setup_database():
    """Setup the SQLite database for storing telegram discovery data."""
    conn = sqlite3.connect(str(TELEGRAM_DB))
    c = conn.cursor()
    
    # Create discovered_channels table
    c.execute('''
    CREATE TABLE IF NOT EXISTS discovered_channels (
        id INTEGER PRIMARY KEY,
        channel_id TEXT UNIQUE,
        channel_name TEXT,
        join_status TEXT DEFAULT 'pending',
        source TEXT,
        discovery_date TEXT DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    
    # Create channels table if it doesn't exist with consistent schema
    c.execute('''
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY,
        channel_id TEXT UNIQUE,
        channel_name TEXT,
        last_message_id INTEGER DEFAULT 0
    )
    ''')
    
    conn.commit()
    conn.close()

def add_discovered_channel(channel_id, channel_name, source):
    """Add a newly discovered channel to the database."""
    conn = sqlite3.connect(str(TELEGRAM_DB))
    c = conn.cursor()
    
    try:
        c.execute(
            'INSERT OR IGNORE INTO discovered_channels (channel_id, channel_name, source) VALUES (?, ?, ?)',
            (str(channel_id), channel_name, source)
        )
        conn.commit()
        if c.rowcount > 0:
            logger.info(f"Added discovered channel {channel_name} ({channel_id}) from {source}")
            return True
        return False
    except sqlite3.Error as e:
        logger.error(f"Database error adding discovered channel: {e}")
        return False
    finally:
        conn.close()

async def run_discovery(leave_after_completion=True):
    """Main Telegram channel discovery process."""
    # Ensure database is set up
    setup_database()
    
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        logger.error("Telegram API credentials not found. Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env file.")
        return False
    
    # Create Telegram client using session string if available
    if TELEGRAM_SESSION_STRING:
        client = TelegramClient(
            StringSession(TELEGRAM_SESSION_STRING), 
            int(TELEGRAM_API_ID), 
            TELEGRAM_API_HASH
        )
    else:
        logger.error("No Telegram session string found. Cannot proceed.")
        return False
    
    try:
        logger.info("Starting Telegram client")
        await client.start()
        
        discovered_count = 0
        
        # Basic discovery - search dialogs for potential channels
        try:
            async for dialog in client.iter_dialogs(limit=50):
                if dialog.is_channel:
                    channel_id = dialog.id
                    channel_name = dialog.name or str(channel_id)
                    
                    # Try to add to discovered channels
                    if add_discovered_channel(channel_id, channel_name, "dialog_search"):
                        discovered_count += 1
        except Exception as e:
            logger.error(f"Error searching dialogs: {e}")
        
        logger.info(f"Discovered {discovered_count} channels from dialogs")
        
        # Disconnect cleanly
        await client.disconnect()
        
        return True
    
    except Exception as e:
        logger.error(f"Error in discovery process: {e}")
        
        # Ensure client disconnects even if an error occurs
        if client and client.is_connected():
            await client.disconnect()
        
        return False

if __name__ == "__main__":
    try:
        asyncio.run(run_discovery())
    except KeyboardInterrupt:
        print("\nDiscovery interrupted. Exiting...")
