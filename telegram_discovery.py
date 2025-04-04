#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Telegram Channel Discovery Extension for KeyBoxer
# This module extends the telegram crawler to discover new channels

import os
import re
import sys
import json
import sqlite3
import asyncio
import logging
from pathlib import Path
from dotenv import load_dotenv

# Telethon imports
from telethon import TelegramClient
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.functions.messages import SearchGlobalRequest, ImportChatInviteRequest
from telethon.tl.types import InputMessagesFilterUrl, InputPeerEmpty
from telethon.errors import ChatAdminRequiredError, ChannelPrivateError, InviteHashInvalidError, FloodWaitError

# Load environment variables
load_dotenv()
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE")

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

# Patterns for finding channel references
INVITE_LINK_PATTERN = re.compile(r't\.me/[+]([\w-]+)')
CHANNEL_LINK_PATTERN = re.compile(r't\.me/([\w_]+)')
KEYBOX_RELATED_TERMS = [
    'keybox', 'attestation', 'android key', 'safetynet', 'integrity', 
    'play integrity', 'strongbox', 'keymaster', 'android security',
    'android root', 'magisk', 'custom rom', 'rooted device'
]

def setup_database():
    """Setup the SQLite database for storing telegram discovery data."""
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    # Create tables if they don't exist
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
    
    conn.commit()
    conn.close()

def add_discovered_channel(channel_id, channel_name, source):
    """Add a newly discovered channel to the database."""
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    try:
        c.execute(
            'INSERT OR IGNORE INTO discovered_channels (channel_id, channel_name, source) VALUES (?, ?, ?)',
            (channel_id, channel_name, source)
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

def update_channel_join_status(channel_id, status):
    """Update the join status of a channel."""
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    try:
        c.execute(
            'UPDATE discovered_channels SET join_status = ? WHERE channel_id = ?',
            (status, channel_id)
        )
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Database error updating channel status: {e}")
    finally:
        conn.close()

def get_pending_channels():
    """Get channels pending to be joined."""
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    try:
        c.execute('SELECT channel_id, channel_name FROM discovered_channels WHERE join_status = "pending"')
        channels = c.fetchall()
        return channels
    except sqlite3.Error as e:
        logger.error(f"Database error getting pending channels: {e}")
        return []
    finally:
        conn.close()

async def extract_channel_from_link(client, link):
    """Extract channel information from a Telegram link."""
    try:
        # Handle t.me/joinchat or t.me/+ links (private channels)
        invite_match = INVITE_LINK_PATTERN.search(link)
        if invite_match:
            invite_hash = invite_match.group(1)
            try:
                await client(ImportChatInviteRequest(invite_hash))
                # We need to wait a bit after joining to get entity
                await asyncio.sleep(2)
                # The entity should now be accessible in recent dialogs
                async for dialog in client.iter_dialogs(limit=5):
                    # Return the most recent joined channel
                    if dialog.is_channel:
                        return str(dialog.id), dialog.title
            except Exception as e:
                logger.error(f"Error joining private channel via invite link: {e}")
                return None, None
                
        # Handle t.me/username links (public channels)
        channel_match = CHANNEL_LINK_PATTERN.search(link)
        if channel_match:
            username = channel_match.group(1)
            try:
                entity = await client.get_entity(username)
                return str(entity.id), getattr(entity, 'title', username)
            except Exception as e:
                logger.error(f"Error getting entity for username {username}: {e}")
                return None, None
                
        return None, None
    except Exception as e:
        logger.error(f"Error extracting channel from link {link}: {e}")
        return None, None

async def search_for_channels(client, query, limit=20):
    """Search for channels related to the query."""
    discovered = 0
    
    try:
        # Method 1: Search global messages for related terms
        logger.info(f"Searching Telegram for: {query}")
        
        # We need a generic peer for global search
        result = await client(SearchGlobalRequest(
            q=query,
            offset_peer=InputPeerEmpty(),
            offset_rate=0,
            limit=limit,
            filter=InputMessagesFilterUrl()
        ))
        
        for message in result.messages:
            if not message.peer_id:
                continue
                
            try:
                # Get entity information
                channel = await client.get_entity(message.peer_id)
                
                if hasattr(channel, 'username') and channel.username:
                    channel_id = str(channel.id)
                    channel_name = getattr(channel, 'title', channel.username)
                    
                    if add_discovered_channel(channel_id, channel_name, f"search:{query}"):
                        discovered += 1
                        logger.info(f"Discovered channel: {channel_name}")
            except Exception as e:
                logger.error(f"Error processing search result: {e}")
                continue
                
        # Method 2: Look for channel links in messages
        if hasattr(result, 'messages'):
            for message in result.messages:
                if not message.message:
                    continue
                    
                # Extract t.me links from message text
                for link_match in CHANNEL_LINK_PATTERN.finditer(message.message):
                    channel_username = link_match.group(1)
                    try:
                        channel = await client.get_entity(channel_username)
                        channel_id = str(channel.id)
                        channel_name = getattr(channel, 'title', channel_username)
                        
                        if add_discovered_channel(channel_id, channel_name, f"link_in_message:{query}"):
                            discovered += 1
                    except Exception as e:
                        logger.debug(f"Error processing channel link {channel_username}: {e}")
        
        logger.info(f"Discovered {discovered} new channels while searching for '{query}'")
        return discovered
        
    except Exception as e:
        logger.error(f"Error during channel discovery: {e}")
        if isinstance(e, FloodWaitError):
            logger.warning(f"Hit rate limit. Need to wait {e.seconds} seconds")
            await asyncio.sleep(e.seconds)
        return 0

async def join_discovered_channels(client, max_joins=5):
    """Attempt to join pending discovered channels."""
    pending_channels = get_pending_channels()
    
    if not pending_channels:
        logger.info("No pending channels to join")
        return 0
        
    joined_count = 0
    for channel_id, channel_name in pending_channels[:max_joins]:
        try:
            # Check if it's a numerical ID or username
            if channel_id.startswith('-100'):
                # Get the entity first (this might fail if we can't access it)
                try:
                    entity = await client.get_entity(int(channel_id))
                except Exception as e:
                    logger.error(f"Cannot get entity for channel {channel_id}: {e}")
                    update_channel_join_status(channel_id, "failed")
                    continue
            else:
                # Username-based channel
                try:
                    entity = await client.get_entity(channel_name)
                except Exception:
                    # Try with ID if name fails
                    try:
                        entity = await client.get_entity(int(channel_id))
                    except Exception as e:
                        logger.error(f"Cannot get entity for channel {channel_name}: {e}")
                        update_channel_join_status(channel_id, "failed")
                        continue
            
            # Attempt to join the channel
            await client(JoinChannelRequest(entity))
            update_channel_join_status(channel_id, "joined")
            logger.info(f"Successfully joined channel: {channel_name}")
            
            # Add the channel to tracking list
            from telegram_crawler import add_channel
            add_channel(channel_id, channel_name)
            
            joined_count += 1
            
            # Be nice to Telegram servers
            await asyncio.sleep(random.randint(5, 10))
            
        except ChannelPrivateError:
            logger.warning(f"Cannot join private channel: {channel_name}")
            update_channel_join_status(channel_id, "private")
        except ChatAdminRequiredError:
            logger.warning(f"Admin rights required for channel: {channel_name}")
            update_channel_join_status(channel_id, "admin_required")
        except Exception as e:
            logger.error(f"Error joining channel {channel_name}: {e}")
            if isinstance(e, FloodWaitError):
                logger.warning(f"Hit rate limit. Need to wait {e.seconds} seconds")
                await asyncio.sleep(e.seconds)
                # Don't mark as failed, just stop the current batch
                break
            update_channel_join_status(channel_id, "failed")
    
    return joined_count

async def discover_from_existing_channels(client, max_depth=2, max_per_channel=10):
    """Discover new channels from existing channels."""
    # Get currently tracked channels
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    try:
        c.execute('SELECT channel_id, channel_name FROM channels')
        current_channels = c.fetchall()
    except sqlite3.Error as e:
        logger.error(f"Database error: {e}")
        current_channels = []
    finally:
        conn.close()
    
    if not current_channels:
        logger.info("No existing channels to discover from")
        return 0
    
    total_discovered = 0
    processed_channels = set()
    channels_to_process = [(channel_id, channel_name, 0) for channel_id, channel_name in current_channels]
    
    # Breadth-first search through channel network
    while channels_to_process and (max_depth == -1 or channels_to_process[0][2] <= max_depth):
        channel_id, channel_name, depth = channels_to_process.pop(0)
        
        if channel_id in processed_channels:
            continue
            
        processed_channels.add(channel_id)
        
        try:
            logger.info(f"Discovering channels from: {channel_name} (depth {depth})")
            
            # Analyze the forwarded messages in this channel
            entity = None
            try:
                if channel_id.startswith('-100'):
                    entity = await client.get_entity(int(channel_id))
                else:
                    entity = await client.get_entity(channel_name)
            except Exception as e:
                logger.error(f"Cannot get entity for channel {channel_name}: {e}")
                continue
                
            discovered_in_channel = 0
            
            # Look at messages for forwarded content or links
            async for message in client.iter_messages(entity, limit=50):
                if discovered_in_channel >= max_per_channel:
                    break
                    
                # Check for forwarded messages
                if message.forward and hasattr(message.forward, 'channel_id'):
                    forward_id = str(message.forward.channel_id)
                    try:
                        forward_entity = await client.get_entity(int(message.forward.channel_id))
                        forward_name = getattr(forward_entity, 'title', str(message.forward.channel_id))
                        
                        if add_discovered_channel(forward_id, forward_name, f"forward_from:{channel_name}"):
                            total_discovered += 1
                            discovered_in_channel += 1
                            
                            # Add to processing queue if under depth limit
                            if depth < max_depth:
                                channels_to_process.append((forward_id, forward_name, depth + 1))
                    except Exception as e:
                        logger.debug(f"Cannot process forwarded message: {e}")
                
                # Check for t.me links in messages
                if message.message:
                    for match in CHANNEL_LINK_PATTERN.finditer(message.message):
                        if discovered_in_channel >= max_per_channel:
                            break
                            
                        username = match.group(1)
                        try:
                            link_entity = await client.get_entity(username)
                            if hasattr(link_entity, 'id'):
                                link_id = str(link_entity.id)
                                link_name = getattr(link_entity, 'title', username)
                                
                                if add_discovered_channel(link_id, link_name, f"link_in:{channel_name}"):
                                    total_discovered += 1
                                    discovered_in_channel += 1
                                    
                                    # Add to processing queue if under depth limit
                                    if depth < max_depth:
                                        channels_to_process.append((link_id, link_name, depth + 1))
                        except Exception as e:
                            logger.debug(f"Cannot process channel link {username}: {e}")
            
            logger.info(f"Discovered {discovered_in_channel} channels from {channel_name}")
            
            # Be nice to Telegram servers
            await asyncio.sleep(random.randint(3, 7))
            
        except Exception as e:
            logger.error(f"Error discovering from channel {channel_name}: {e}")
            if isinstance(e, FloodWaitError):
                logger.warning(f"Hit rate limit. Need to wait {e.seconds} seconds")
                await asyncio.sleep(e.seconds)
    
    return total_discovered

async def leave_joined_channels(client, leave_all=False):
    """Leave channels that were joined during the discovery process.
    
    Args:
        client: The Telegram client
        leave_all: If True, leave all joined channels. If False, only leave channels
                  that were joined during the current discovery process.
    
    Returns:
        int: Number of channels left
    """
    # Get channels that were joined during discovery
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    if leave_all:
        # Get all channels with "joined" status
        c.execute('SELECT channel_id, channel_name FROM discovered_channels WHERE join_status = "joined"')
    else:
        # Get only channels that were joined in the current session (last hour)
        c.execute('''
            SELECT channel_id, channel_name 
            FROM discovered_channels 
            WHERE join_status = "joined" 
            AND datetime(discovery_date) > datetime('now', '-1 hour')
        ''')
    
    channels_to_leave = c.fetchall()
    conn.close()
    
    if not channels_to_leave:
        logger.info("No channels to leave")
        return 0
    
    left_count = 0
    from telethon.tl.functions.channels import LeaveChannelRequest
    
    for channel_id, channel_name in channels_to_leave:
        try:
            # Get entity for the channel
            if channel_id.startswith('-100'):
                entity = await client.get_entity(int(channel_id))
            else:
                entity = await client.get_entity(channel_name)
            
            # Leave the channel
            await client(LeaveChannelRequest(entity))
            
            # Update the database
            conn = sqlite3.connect(TELEGRAM_DB)
            c = conn.cursor()
            c.execute(
                'UPDATE discovered_channels SET join_status = "left" WHERE channel_id = ?',
                (channel_id,)
            )
            conn.commit()
            conn.close()
            
            left_count += 1
            logger.info(f"Left channel: {channel_name}")
            
            # Add a small delay to avoid rate limits
            await asyncio.sleep(random.randint(2, 5))
            
        except Exception as e:
            logger.error(f"Error leaving channel {channel_name}: {e}")
            if isinstance(e, FloodWaitError):
                logger.warning(f"Hit rate limit. Need to wait {e.seconds} seconds")
                await asyncio.sleep(e.seconds)
    
    return left_count

async def run_discovery(leave_after_completion=True):
    """Main function to run channel discovery and joining.
    
    Args:
        leave_after_completion: If True, leave all channels joined during this run
    """
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        logger.error("Telegram API credentials not found. Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env file.")
        return
    
    # Setup the database
    setup_database()
    
    # Create Telegram client
    client = TelegramClient(
        str(TELEGRAM_SESSION_DIR / "telegram_session"), 
        int(TELEGRAM_API_ID), 
        TELEGRAM_API_HASH
    )
    
    try:
        logger.info("Starting Telegram client")
        await client.start(phone=TELEGRAM_PHONE)
        
        # Step 1: Search for channels using keywords
        total_discovered = 0
        for term in KEYBOX_RELATED_TERMS:
            discovered = await search_for_channels(client, term, limit=30)
            total_discovered += discovered
            
            # Avoid rate limits
            await asyncio.sleep(random.randint(10, 15))
        
        logger.info(f"Discovered {total_discovered} channels through keyword searches")
        
        # Step 2: Discover channels from existing channels
        discovered_from_existing = await discover_from_existing_channels(client)
        logger.info(f"Discovered {discovered_from_existing} additional channels from existing channels")
        
        # Step 3: Join some of the discovered channels
        joined = await join_discovered_channels(client, max_joins=10)
        logger.info(f"Joined {joined} new channels")
        
        # Provide summary
        conn = sqlite3.connect(TELEGRAM_DB)
        c = conn.cursor()
        
        c.execute('SELECT COUNT(*) FROM discovered_channels')
        total_in_db = c.fetchone()[0]
        
        c.execute('SELECT COUNT(*) FROM discovered_channels WHERE join_status = "joined"')
        total_joined = c.fetchone()[0]
        
        c.execute('SELECT COUNT(*) FROM discovered_channels WHERE join_status = "pending"')
        total_pending = c.fetchone()[0]
        
        conn.close()
        
        logger.info("Discovery Summary:")
        logger.info(f"- Total channels in database: {total_in_db}")
        logger.info(f"- Successfully joined: {total_joined}")
        logger.info(f"- Pending to join: {total_pending}")
        
        # Leave channels if requested
        if leave_after_completion and joined > 0:
            logger.info("Leaving channels that were joined during discovery...")
            left = await leave_joined_channels(client, leave_all=False)
            logger.info(f"Left {left} channels")
        
    except Exception as e:
        logger.error(f"Error in discovery process: {e}")
    finally:
        if client.is_connected():
            await client.disconnect()

if __name__ == "__main__":
    import random
    try:
        asyncio.run(run_discovery())
    except KeyboardInterrupt:
        print("\nDiscovery interrupted. Exiting...")
