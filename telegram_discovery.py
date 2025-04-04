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
import time
from pathlib import Path
from dotenv import load_dotenv

# Telethon imports
from telethon import TelegramClient
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.types import InputMessagesFilterUrl, InputPeerEmpty, MessageMediaDocument, MessageMediaPhoto
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

# Default timeout in seconds (30 minutes)
DEFAULT_TIMEOUT = 30 * 60

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

async def process_message_media(client, message, channel_id):
    """Process media attachments in a message for potential keybox files."""
    if not message.media:
        return

    try:
        if isinstance(message.media, (MessageMediaDocument, MessageMediaPhoto)):
            # Simple download without dc_id parameter
            try:
                media_content = await message.download_media(file=bytes)
            except Exception as e:
                logger.error(f"Download failed, trying fallback: {e}")
                try:
                    # Fallback to client download
                    media_content = await client.download_media(message, bytes)
                except Exception as e2:
                    logger.error(f"All download attempts failed: {e2}")
                    media_content = None
            
            if not media_content:
                return
                
            # Check if file has XML in filename
            if hasattr(message.media, 'document') and message.media.document.attributes:
                for attr in message.media.document.attributes:
                    if hasattr(attr, 'file_name') and attr.file_name:
                        if XML_FILE_PATTERN.match(attr.file_name):
                            logger.info(f"Found XML file: {attr.file_name}")
                            
            # Look for XML content
            if media_content and (media_content.startswith(b'<?xml') or b'<AndroidAttestation>' in media_content):
                logger.info(f"Found potential keybox XML content in message {message.id}")
                
    except Exception as e:
        logger.error(f"Error processing media: {e}")

async def run_discovery_with_timeout(timeout=DEFAULT_TIMEOUT, leave_after_completion=True):
    """Run discovery with a timeout."""
    try:
        # Use asyncio.wait_for to implement the timeout
        await asyncio.wait_for(
            run_discovery(leave_after_completion), 
            timeout=timeout
        )
        logger.info(f"Discovery completed successfully within timeout of {timeout} seconds")
        return True
    except asyncio.TimeoutError:
        logger.warning(f"Discovery timed out after {timeout} seconds")
        
        # Create client to disconnect any pending connections
        if TELEGRAM_SESSION_STRING:
            try:
                client = TelegramClient(
                    StringSession(TELEGRAM_SESSION_STRING), 
                    int(TELEGRAM_API_ID), 
                    TELEGRAM_API_HASH
                )
                await client.start()
                await client.disconnect()
                logger.info("Successfully disconnected client after timeout")
            except Exception as e:
                logger.error(f"Error disconnecting client after timeout: {e}")
        
        return False
    except Exception as e:
        logger.error(f"Error in discovery process: {e}")
        return False

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
        
        # Record start time to monitor progress
        start_time = time.time()
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
        logger.info(f"Time elapsed: {time.time() - start_time:.2f} seconds")
        
        # Step 2: Search for relevant channels using simplified search approach
        logger.info(f"Searching globally using term: {SEARCH_TERM}")
        global_discovered = 0

        try:
            # Use a simpler search approach with limit to avoid long-running operations
            message_count = 0
            async for result in client.iter_messages(None, search=SEARCH_TERM, limit=50):
                message_count += 1
                if result.chat:
                    try:
                        chat = await client.get_entity(result.chat_id)
                        if hasattr(chat, 'id') and hasattr(chat, 'title'):
                            channel_id = str(chat.id)
                            channel_name = chat.title
                            
                            # Add to discovered channels
                            if add_discovered_channel(channel_id, channel_name, f"global_search:{SEARCH_TERM}"):
                                global_discovered += 1
                                logger.info(f"Found channel from search: {channel_name} ({channel_id})")
                    except Exception as chat_error:
                        logger.debug(f"Error getting chat entity: {chat_error}")
                
                # Check if we're approaching timeout
                if message_count % 10 == 0:
                    elapsed = time.time() - start_time
                    logger.info(f"Processed {message_count} search results. Time elapsed: {elapsed:.2f} seconds")
            
            logger.info(f"Discovered {global_discovered} additional channels from global search")
        except Exception as e:
            logger.error(f"Error in global search for '{SEARCH_TERM}': {e}")
            if isinstance(e, FloodWaitError):
                wait_time = min(e.seconds, 10)  # Cap wait time to avoid long delays
                logger.warning(f"Rate limited. Waiting {wait_time} seconds")
                await asyncio.sleep(wait_time)
        
        logger.info(f"Time elapsed after search: {time.time() - start_time:.2f} seconds")
        
        # Step 3: Extract channel links from messages in existing channels (with limited scope)
        logger.info("Looking for channel links in existing dialogs...")
        link_discovered = 0
        
        try:
            # Limit the number of dialogs and messages to check to avoid timeouts
            dialog_count = 0
            async for dialog in client.iter_dialogs(limit=10):
                dialog_count += 1
                if dialog.is_channel:
                    try:
                        # Get a limited number of recent messages
                        message_count = 0
                        async for message in client.iter_messages(dialog.id, limit=30):
                            message_count += 1
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
                                
                            # Check time elapsed to avoid timeouts
                            if message_count % 10 == 0:
                                elapsed = time.time() - start_time
                                if elapsed > (DEFAULT_TIMEOUT * 0.8):  # If we've used 80% of timeout
                                    logger.warning(f"Approaching timeout, skipping remaining message checks")
                                    break
                    except Exception as e:
                        logger.error(f"Error processing messages from channel {dialog.id}: {e}")
                        continue
                
                # Check if we're approaching timeout
                elapsed = time.time() - start_time
                if elapsed > (DEFAULT_TIMEOUT * 0.8):  # If we've used 80% of timeout
                    logger.warning(f"Approaching timeout, skipping remaining dialogs")
                    break
        except Exception as e:
            logger.error(f"Error searching for links in messages: {e}")
        
        logger.info(f"Discovered {link_discovered} channels from links in messages")
        logger.info(f"Time elapsed: {time.time() - start_time:.2f} seconds")
        
        # Step 4: Join a limited number of discovered channels
        join_count = 0
        max_joins = 5  # Limit number of joins to save time
        
        try:
            # Get channels that are in 'pending' state
            conn = sqlite3.connect(str(TELEGRAM_DB))
            c = conn.cursor()
            c.execute('SELECT channel_id, channel_name FROM discovered_channels WHERE join_status = "pending" LIMIT ?', 
                     (max_joins,))
            pending_channels = c.fetchall()
            conn.close()
            
            if pending_channels:
                logger.info(f"Attempting to join {len(pending_channels)} new channels...")
                
                for channel_id, channel_name in pending_channels:
                    # Check if we're approaching timeout
                    elapsed = time.time() - start_time
                    if elapsed > (DEFAULT_TIMEOUT * 0.9):  # If we've used 90% of timeout
                        logger.warning(f"Approaching timeout, skipping remaining joins")
                        break
                        
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
                            try:
                                from telegram_crawler import add_channel
                                add_channel(channel_id, channel_name)
                            except ImportError:
                                logger.warning("Could not import add_channel from telegram_crawler")
                            
                            # Sleep to avoid rate limiting, but not too long
                            await asyncio.sleep(1)
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
        logger.info(f"Total time elapsed: {time.time() - start_time:.2f} seconds")
        
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
        # Use the timeout wrapper function
        asyncio.run(run_discovery_with_timeout(timeout=1800))  # 30 minutes
    except KeyboardInterrupt:
        print("\nDiscovery interrupted. Exiting...")
