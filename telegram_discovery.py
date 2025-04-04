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
from telethon.tl.functions.messages import SearchGlobalRequest, ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.types import InputMessagesFilterUrl, InputPeerEmpty
from telethon.errors import (
    ChatAdminRequiredError, 
    ChannelPrivateError, 
    InviteHashInvalidError, 
    FloodWaitError
)
from telethon.sessions import StringSession

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

# Patterns and search terms - mirrored from keyboxer.py
SEARCH_TERM = "<AndroidAttestation>"
INVITE_LINK_PATTERN = re.compile(r't\.me/[+]([\w-]+)')
CHANNEL_LINK_PATTERN = re.compile(r't\.me/([\w_]+)')
XML_FILE_PATTERN = re.compile(r'.*\.xml$', re.IGNORECASE)
SUPPORTED_EXTENSIONS = ['.xml', '.zip', '.gz', '.tar', '.tgz', '.tar.gz']

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
    """Main Telegram channel discovery process with enhanced capabilities."""
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
        
        # Step 1: Discover from existing dialogs
        try:
            logger.info("Searching existing dialogs for channels...")
            async for dialog in client.iter_dialogs(limit=100):
                if dialog.is_channel:
                    channel_id = dialog.id
                    channel_name = dialog.name or str(channel_id)
                    
                    # Try to add to discovered channels
                    if add_discovered_channel(channel_id, channel_name, "dialog_search"):
                        discovered_count += 1
        except Exception as e:
            logger.error(f"Error searching dialogs: {e}")
        
        logger.info(f"Discovered {discovered_count} channels from dialogs")
        
        # Step 2: Search for relevant channels using the exact search term from keyboxer.py
        logger.info(f"Searching globally using term: {SEARCH_TERM}")
        global_discovered = 0
        
        try:
            search_result = await client(SearchGlobalRequest(
                q=SEARCH_TERM,
                filter=None  # To get all types of results
            ))
            
            # Process search results
            if hasattr(search_result, 'chats') and search_result.chats:
                for chat in search_result.chats:
                    if hasattr(chat, 'id') and hasattr(chat, 'title'):
                        channel_id = str(chat.id)
                        channel_name = chat.title
                        
                        # Add to discovered channels
                        if add_discovered_channel(channel_id, channel_name, f"global_search:{SEARCH_TERM}"):
                            global_discovered += 1
                            logger.info(f"Found channel: {channel_name} ({channel_id})")
            
            # Avoid rate limiting
            await asyncio.sleep(2)
        except Exception as e:
            logger.error(f"Error in global search for '{SEARCH_TERM}': {e}")
            if isinstance(e, FloodWaitError):
                wait_time = e.seconds
                logger.warning(f"Rate limited. Waiting {wait_time} seconds")
                await asyncio.sleep(wait_time)
        
        logger.info(f"Discovered {global_discovered} additional channels from global search")
        
        # Step 3: Extract channel links from messages in existing channels
        logger.info("Looking for channel links in existing dialogs...")
        link_discovered = 0
        
        try:
            # Check the most recent messages in existing channels for links to other channels
            for dialog in await client.get_dialogs(limit=20):
                if dialog.is_channel:
                    try:
                        # Get recent messages
                        async for message in client.iter_messages(dialog.id, limit=200):
                            if message.text:
                                # Look for t.me links
                                for match in CHANNEL_LINK_PATTERN.finditer(message.text):
                                    channel_username = match.group(1)
                                    try:
                                        # Try to get the channel entity
                                        channel = await client.get_entity(channel_username)
                                        if hasattr(channel, 'id') and hasattr(channel, 'title'):
                                            channel_id = str(channel.id)
                                            channel_name = channel.title
                                            
                                            # Add to discovered channels
                                            if add_discovered_channel(channel_id, channel_name, "message_link"):
                                                link_discovered += 1
                                                logger.info(f"Found channel from link: {channel_name} ({channel_id})")
                                    except Exception as link_error:
                                        logger.debug(f"Could not resolve channel link {channel_username}: {link_error}")
                                
                                # Also look for invite links
                                for match in INVITE_LINK_PATTERN.finditer(message.text):
                                    invite_code = match.group(1)
                                    try:
                                        # Try to get info about the invite without joining
                                        invite_result = await client(CheckChatInviteRequest(invite_code))
                                        if hasattr(invite_result, 'chat') and hasattr(invite_result.chat, 'id'):
                                            channel_id = str(invite_result.chat.id)
                                            channel_name = invite_result.chat.title
                                            
                                            # Add to discovered channels
                                            if add_discovered_channel(channel_id, channel_name, "invite_link"):
                                                link_discovered += 1
                                                logger.info(f"Found channel from invite: {channel_name} ({channel_id})")
                                    except Exception as invite_error:
                                        logger.debug(f"Could not check invite {invite_code}: {invite_error}")
                                        
                            # Sleep to avoid rate limiting
                            await asyncio.sleep(0.1)
                    except Exception as e:
                        logger.error(f"Error processing messages from channel {dialog.id}: {e}")
                        continue
        except Exception as e:
            logger.error(f"Error searching for links in messages: {e}")
        
        logger.info(f"Discovered {link_discovered} channels from links in messages")
        
        # Step 4: Optionally try to join discovered channels
        join_count = 0
        
        try:
            # Get channels that are in 'pending' state
            conn = sqlite3.connect(str(TELEGRAM_DB))
            c = conn.cursor()
            c.execute('SELECT channel_id, channel_name FROM discovered_channels WHERE join_status = "pending" LIMIT 10')
            pending_channels = c.fetchall()
            conn.close()
            
            if pending_channels:
                logger.info(f"Attempting to join {len(pending_channels)} new channels...")
                
                for channel_id, channel_name in pending_channels:
                    try:
                        # Try to join the channel
                        entity = None
                        
                        # Check if it's a numeric ID or username
                        if channel_id.startswith('-100'):
                            try:
                                entity = await client.get_entity(int(channel_id))
                            except:
                                pass
                        else:
                            try:
                                entity = await client.get_entity(channel_id)
                            except:
                                # If channel_name might be a username, try that
                                if channel_name and '@' not in channel_name and '/' not in channel_name:
                                    try:
                                        entity = await client.get_entity(channel_name)
                                    except:
                                        pass
                        
                        if entity:
                            result = await client(JoinChannelRequest(entity))
                            
                            # Update status in database
                            conn = sqlite3.connect(str(TELEGRAM_DB))
                            c = conn.cursor()
                            c.execute(
                                'UPDATE discovered_channels SET join_status = "joined" WHERE channel_id = ?',
                                (channel_id,)
                            )
                            conn.commit()
                            conn.close()
                            
                            join_count += 1
                            logger.info(f"Successfully joined channel: {channel_name}")
                            
                            # Also add to tracking list for crawler
                            # First, import add_channel from telegram_crawler
                            try:
                                from telegram_crawler import add_channel
                                add_channel(channel_id, channel_name)
                            except ImportError:
                                logger.warning("Could not import add_channel from telegram_crawler")
                            
                            # Sleep to avoid rate limiting
                            await asyncio.sleep(2)
                    except Exception as e:
                        logger.error(f"Error joining channel {channel_id}: {e}")
                        
                        # Update status in database to reflect failure
                        conn = sqlite3.connect(str(TELEGRAM_DB))
                        c = conn.cursor()
                        c.execute(
                            'UPDATE discovered_channels SET join_status = "failed" WHERE channel_id = ?',
                            (channel_id,)
                        )
                        conn.commit()
                        conn.close()
                        continue
        except Exception as e:
            logger.error(f"Error in channel joining process: {e}")
        
        logger.info(f"Successfully joined {join_count} new channels")
        
        # Disconnect cleanly
        if leave_after_completion:
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
