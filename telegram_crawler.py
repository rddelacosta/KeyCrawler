#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Telegram Crawler for KeyBoxer

import os
import sqlite3
import json
import asyncio
import logging
import hashlib
import io
import re
import zipfile
import gzip
import tarfile
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

# Telethon imports
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto, InputDocumentFileLocation
from telethon.errors import FloodWaitError, RPCError
from telethon.tl.functions.auth import ExportAuthorizationRequest, ImportAuthorizationRequest

# Import KeyBoxer check function
from check import keybox_check

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("telegram_crawler.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("telegram_crawler")

# Load environment variables
load_dotenv()
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE")
TELEGRAM_SESSION_STRING = os.getenv("TELEGRAM_SESSION_STRING")

# Folders
BASE_DIR = Path(__file__).resolve().parent
KEYS_DIR = BASE_DIR / "keys"
KEYS_DIR.mkdir(exist_ok=True)
TELEGRAM_SESSION_DIR = BASE_DIR / "telegram_session"
TELEGRAM_SESSION_DIR.mkdir(exist_ok=True)
TELEGRAM_DB = BASE_DIR / "telegram_data.db"

# State file for saving progress
STATE_FILE = BASE_DIR / "telegram_state.json"

# XML-related patterns
XML_PATTERN = re.compile(r'<AndroidAttestation>|<KeyAttestationStatement>|<KeyboxInfo>|<NumberOfCertificates>', re.IGNORECASE)
XML_FILE_PATTERN = re.compile(r'.*\.xml$', re.IGNORECASE)
ARCHIVE_EXTENSIONS = ['.zip', '.gz', '.tar', '.tgz', '.tar.gz']

def load_state():
    """Load the state from the JSON file."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {
        'channels': {},
        'last_check_time': None
    }

def save_state(state):
    """Save the state to the JSON file."""
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

def setup_database():
    """Setup the SQLite database for storing telegram messages."""
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    # Create tables if they don't exist
    c.execute('''
    CREATE TABLE IF NOT EXISTS channels (
        id INTEGER PRIMARY KEY,
        channel_id TEXT UNIQUE,
        channel_name TEXT,
        last_message_id INTEGER DEFAULT 0
    )
    ''')
    
    c.execute('''
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY,
        channel_id TEXT,
        message_id INTEGER,
        date TEXT,
        sender_id INTEGER,
        username TEXT,
        message TEXT,
        media_type TEXT,
        media_path TEXT,
        keybox_found BOOLEAN DEFAULT 0,
        keybox_valid BOOLEAN DEFAULT 0,
        processed BOOLEAN DEFAULT 0,
        UNIQUE(channel_id, message_id)
    )
    ''')
    
    c.execute('''
    CREATE TABLE IF NOT EXISTS keyboxes (
        id INTEGER PRIMARY KEY,
        message_id INTEGER,
        channel_id TEXT,
        hash TEXT UNIQUE,
        file_path TEXT,
        valid BOOLEAN DEFAULT 0,
        FOREIGN KEY(message_id, channel_id) REFERENCES messages(message_id, channel_id)
    )
    ''')
    
    conn.commit()
    conn.close()

def add_channel(channel_id, channel_name=None):
    """Add a channel to the database."""
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    try:
        c.execute(
            'INSERT OR IGNORE INTO channels (channel_id, channel_name) VALUES (?, ?)',
            (channel_id, channel_name)
        )
        conn.commit()
        logger.info(f"Added channel {channel_id} ({channel_name}) to database")
    except sqlite3.Error as e:
        logger.error(f"Error adding channel to database: {e}")
    finally:
        conn.close()

def get_channels():
    """Get all channels from the database."""
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    c.execute('SELECT channel_id, channel_name, last_message_id FROM channels')
    channels = c.fetchall()
    
    conn.close()
    return channels

def update_channel_last_message(channel_id, message_id):
    """Update the last processed message ID for a channel."""
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    c.execute(
        'UPDATE channels SET last_message_id = ? WHERE channel_id = ?',
        (message_id, channel_id)
    )
    
    conn.commit()
    conn.close()

def save_message(channel_id, message):
    """Save a message to the database."""
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    try:
        # Get sender info
        if message.sender:
            sender_id = message.sender.id
            username = message.sender.username
        else:
            sender_id = None
            username = None
            
        # Determine media type
        media_type = None
        if message.media:
            media_type = message.media.__class__.__name__
            
        # Insert message data
        c.execute('''
        INSERT OR IGNORE INTO messages 
        (channel_id, message_id, date, sender_id, username, message, media_type, processed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            channel_id,
            message.id,
            message.date.strftime('%Y-%m-%d %H:%M:%S'),
            sender_id,
            username,
            message.text if message.text else "",
            media_type,
            False
        ))
        
        conn.commit()
    except sqlite3.Error as e:
        logger.error(f"Error saving message to database: {e}")
    finally:
        conn.close()

def is_archive(content):
    """Check if content is a valid archive format."""
    try:
        if content.startswith(b'PK\x03\x04'):
            return 'zip'
        elif content.startswith(b'\x1f\x8b'):
            return 'gzip'
        elif len(content) > 257+5 and content[257:257+5] == b'ustar':
            return 'tar'
    except:
        pass
    return None

def extract_xml_from_archive(content, archive_type):
    """Extract XML files from an archive."""
    xml_files = []
    
    try:
        if archive_type == 'zip':
            with io.BytesIO(content) as content_io:
                with zipfile.ZipFile(content_io) as zip_ref:
                    file_list = zip_ref.namelist()
                    for file_name in file_list:
                        if file_name.lower().endswith('.xml'):
                            with zip_ref.open(file_name) as xml_file:
                                xml_content = xml_file.read()
                                xml_files.append((file_name, xml_content))
        
        elif archive_type == 'gzip':
            with io.BytesIO(content) as content_io:
                with gzip.GzipFile(fileobj=content_io) as gz_file:
                    extracted_content = gz_file.read()
                    if extracted_content.startswith(b'<?xml'):
                        xml_files.append(("extracted.xml", extracted_content))
        
        elif archive_type == 'tar':
            with io.BytesIO(content) as content_io:
                with tarfile.open(fileobj=content_io, mode='r') as tar_ref:
                    for member in tar_ref.getmembers():
                        if member.name.lower().endswith('.xml'):
                            f = tar_ref.extractfile(member)
                            if f:
                                xml_content = f.read()
                                xml_files.append((member.name, xml_content))
    
    except Exception as e:
        logger.error(f"Error extracting from archive: {e}")
    
    return xml_files

def process_potential_keybox(content, channel_id, message_id):
    """Process content that might be a keybox.xml."""
    try:
        # Check if this is valid XML and potentially a keybox
        if not XML_PATTERN.search(content.decode('utf-8', errors='ignore')):
            return False
            
        # Attempt to validate keybox
        is_valid = keybox_check(content)
        
        # Generate hash for the content
        hash_value = hashlib.sha256(content).hexdigest()
        file_path = KEYS_DIR / f"{hash_value}.xml"
        
        # Store valid keybox
        if is_valid:
            with open(file_path, "wb") as f:
                f.write(content)
                
            # Update database
            conn = sqlite3.connect(TELEGRAM_DB)
            c = conn.cursor()
            
            c.execute(
                'UPDATE messages SET keybox_found = 1, keybox_valid = 1 WHERE channel_id = ? AND message_id = ?',
                (channel_id, message_id)
            )
            
            c.execute(
                'INSERT OR IGNORE INTO keyboxes (message_id, channel_id, hash, file_path, valid) VALUES (?, ?, ?, ?, ?)',
                (message_id, channel_id, hash_value, str(file_path), True)
            )
            
            conn.commit()
            conn.close()
            
            logger.info(f"Found valid keybox in message {message_id} from channel {channel_id}. Saved to {file_path}")
            return True
        else:
            # Still record that we found a keybox but it was invalid
            conn = sqlite3.connect(TELEGRAM_DB)
            c = conn.cursor()
            
            c.execute(
                'UPDATE messages SET keybox_found = 1, keybox_valid = 0 WHERE channel_id = ? AND message_id = ?',
                (channel_id, message_id)
            )
            
            c.execute(
                'INSERT OR IGNORE INTO keyboxes (message_id, channel_id, hash, file_path, valid) VALUES (?, ?, ?, ?, ?)',
                (message_id, channel_id, hash_value, None, False)
            )
            
            conn.commit()
            conn.close()
            
            logger.info(f"Found invalid keybox in message {message_id} from channel {channel_id}")
            return False
            
    except Exception as e:
        logger.error(f"Error processing potential keybox: {e}")
        return False

async def download_file_with_proper_dc_handling(client, message):
    """Download media with proper DC handling"""
    try:
        if not message.media:
            return None
            
        # First try standard download
        try:
            media_content = await message.download_media(file=bytes)
            return media_content
        except Exception as download_error:
            logger.error(f"Error in standard download: {download_error}")
            
            # If that fails, try a more direct approach
            if hasattr(message.media, 'document'):
                try:
                    # Get document
                    document = message.media.document
                    # Get bytes directly from client
                    result = await client.download_media(message, bytes)
                    return result
                except Exception as fallback_error:
                    logger.error(f"Error in fallback download: {fallback_error}")
                    
                    # If we get here, try one more approach
                    try:
                        # Try with get_file method which handles DC switching internally
                        media_bytes = await client.get_file(message.media.document)
                        return media_bytes
                    except Exception as last_error:
                        logger.error(f"Final download attempt failed: {last_error}")
        
        return None
    except Exception as e:
        logger.error(f"Error downloading media: {e}")
        return None

async def process_message_media(client, message, channel_id):
    """Process media attachments in a message."""
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
                
            # Check if it's an XML file by filename
            if hasattr(message.media, 'document') and message.media.document.attributes:
                for attr in message.media.document.attributes:
                    if hasattr(attr, 'file_name') and attr.file_name:
                        if XML_FILE_PATTERN.match(attr.file_name):
                            # Direct XML file
                            process_potential_keybox(media_content, channel_id, message.id)
                            return
            
            # Check if it's an archive
            archive_type = is_archive(media_content)
            if archive_type:
                xml_files = extract_xml_from_archive(media_content, archive_type)
                for file_name, xml_content in xml_files:
                    process_potential_keybox(xml_content, channel_id, message.id)
            
            # Check if it could be an XML file by content (even without proper extension)
            if media_content.startswith(b'<?xml') or b'<AndroidAttestation>' in media_content:
                process_potential_keybox(media_content, channel_id, message.id)
                
    except Exception as e:
        logger.error(f"Error processing media for message {message.id}: {e}")
    finally:
        # Mark message as processed
        conn = sqlite3.connect(TELEGRAM_DB)
        c = conn.cursor()
        c.execute(
            'UPDATE messages SET processed = 1, media_path = ? WHERE channel_id = ? AND message_id = ?',
            ("processed", channel_id, message.id)
        )
        conn.commit()
        conn.close()

async def process_message_text(message, channel_id):
    """Process text content in a message for potential keybox XML."""
    if not message.text:
        return
        
    # Look for XML content in the message text
    if '<?xml' in message.text or '<AndroidAttestation>' in message.text:
        try:
            # Extract XML content from the message text
            xml_start = message.text.find('<?xml')
            if xml_start == -1:
                xml_start = message.text.find('<AndroidAttestation>')
                
            if xml_start >= 0:
                xml_content = message.text[xml_start:].encode('utf-8')
                process_potential_keybox(xml_content, channel_id, message.id)
        except Exception as e:
            logger.error(f"Error processing text content for message {message.id}: {e}")
    
    # Mark message as processed
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    c.execute(
        'UPDATE messages SET processed = 1 WHERE channel_id = ? AND message_id = ?',
        (channel_id, message.id)
    )
    conn.commit()
    conn.close()

async def scrape_channel(client, channel_id, last_message_id=0):
    """Scrape messages from a channel."""
    try:
        # Determine the entity (supports both channel username and numerical ID)
        if channel_id.startswith('-100'):
            entity = int(channel_id)
        else:
            entity = channel_id
            
        # Get channel information
        channel_entity = await client.get_entity(entity)
        channel_name = getattr(channel_entity, 'title', channel_id)
        
        # Update channel name in database
        conn = sqlite3.connect(TELEGRAM_DB)
        c = conn.cursor()
        c.execute(
            'UPDATE channels SET channel_name = ? WHERE channel_id = ?',
            (channel_name, channel_id)
        )
        conn.commit()
        conn.close()
        
        logger.info(f"Scraping channel: {channel_name} ({channel_id})")
        
        # Get messages
        message_count = 0
        async for message in client.iter_messages(entity, min_id=last_message_id):
            message_count += 1
            
            # Save message to database
            save_message(channel_id, message)
            
            # Process message contents
            await process_message_text(message, channel_id)
            await process_message_media(client, message, channel_id)
            
            # Update last message ID in state
            update_channel_last_message(channel_id, message.id)
            
            if message_count % 50 == 0:
                logger.info(f"Processed {message_count} messages from {channel_name}")
                
        return message_count
        
    except Exception as e:
        logger.error(f"Error scraping channel {channel_id}: {e}")
        if isinstance(e, FloodWaitError):
            logger.warning(f"Rate limited. Need to wait {e.seconds} seconds")
            await asyncio.sleep(e.seconds)
        return 0

async def continuous_scraping():
    """Continuously scrape all channels in the database."""
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        logger.error("Telegram API credentials not found. Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env file.")
        return
        
    # Create Telegram client using session string if available
    if TELEGRAM_SESSION_STRING:
        client = TelegramClient(
            StringSession(TELEGRAM_SESSION_STRING), 
            int(TELEGRAM_API_ID), 
            TELEGRAM_API_HASH
        )
    else:
        client = TelegramClient(
            str(TELEGRAM_SESSION_DIR / "telegram_session"), 
            int(TELEGRAM_API_ID), 
            TELEGRAM_API_HASH
        )
    
    try:
        logger.info("Starting Telegram client")
        # Start client with the appropriate method
        if TELEGRAM_SESSION_STRING:
            await client.start()
        else:
            await client.start(phone=TELEGRAM_PHONE)
        
        while True:
            channels = get_channels()
            
            if not channels:
                logger.info("No channels to scrape. Add channels using the add_channel function.")
                await asyncio.sleep(600)  # Wait 10 minutes before checking again
                continue
                
            logger.info(f"Starting to scrape {len(channels)} channels")
            
            # Load state
            state = load_state()
            state['last_check_time'] = datetime.now().isoformat()
            
            for channel_id, channel_name, last_message_id in channels:
                try:
                    message_count = await scrape_channel(client, channel_id, last_message_id)
                    logger.info(f"Scraped {message_count} new messages from {channel_name} ({channel_id})")
                    
                    # Add a delay between channels to avoid rate limits
                    await asyncio.sleep(5)
                except Exception as e:
                    logger.error(f"Error during channel scraping: {e}")
                    continue
            
            # Save state
            save_state(state)
            
            logger.info("Completed scraping cycle. Waiting before next cycle.")
            await asyncio.sleep(300)  # Wait 5 minutes between cycles
            
    except Exception as e:
        logger.error(f"Error in continuous scraping: {e}")
    finally:
        if client.is_connected():
            await client.disconnect()

async def one_time_scrape():
    """Run a one-time scrape of all channels."""
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        logger.error("Telegram API credentials not found. Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env file.")
        return
        
    # Create Telegram client using session string if available
    if TELEGRAM_SESSION_STRING:
        client = TelegramClient(
            StringSession(TELEGRAM_SESSION_STRING), 
            int(TELEGRAM_API_ID), 
            TELEGRAM_API_HASH
        )
    else:
        client = TelegramClient(
            str(TELEGRAM_SESSION_DIR / "telegram_session"), 
            int(TELEGRAM_API_ID), 
            TELEGRAM_API_HASH
        )
    
    try:
        logger.info("Starting Telegram client for one-time scrape")
        # Start client with the appropriate method
        if TELEGRAM_SESSION_STRING:
            await client.start()
        else:
            await client.start(phone=TELEGRAM_PHONE)
        
        channels = get_channels()
        
        if not channels:
            logger.info("No channels to scrape. Add channels using the add_channel function.")
            return
            
        logger.info(f"Starting to scrape {len(channels)} channels")
        
        for channel_id, channel_name, last_message_id in channels:
            try:
                message_count = await scrape_channel(client, channel_id, last_message_id)
                logger.info(f"Scraped {message_count} new messages from {channel_name} ({channel_id})")
                
                # Add a delay between channels to avoid rate limits
                await asyncio.sleep(5)
            except Exception as e:
                logger.error(f"Error during channel scraping: {e}")
                continue
        
        logger.info("One-time scrape completed")
        
    except Exception as e:
        logger.error(f"Error in one-time scrape: {e}")
    finally:
        if client.is_connected():
            await client.disconnect()

async def list_available_channels():
    """List all channels available to the user's Telegram account."""
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        logger.error("Telegram API credentials not found. Set TELEGRAM_API_ID and TELEGRAM_API_HASH in .env file.")
        return
        
    # Create Telegram client
    if TELEGRAM_SESSION_STRING:
        client = TelegramClient(
            StringSession(TELEGRAM_SESSION_STRING), 
            int(TELEGRAM_API_ID), 
            TELEGRAM_API_HASH
        )
    else:
        client = TelegramClient(
            str(TELEGRAM_SESSION_DIR / "telegram_session"), 
            int(TELEGRAM_API_ID), 
            TELEGRAM_API_HASH
        )
    
    try:
        logger.info("Starting Telegram client to list channels")
        # Start client with the appropriate method
        if TELEGRAM_SESSION_STRING:
            await client.start()
        else:
            await client.start(phone=TELEGRAM_PHONE)
        
        print("\nAvailable channels:")
        print("-" * 50)
        print(f"{'Channel Name':<30} {'Channel ID':<20}")
        print("-" * 50)
        
        async for dialog in client.iter_dialogs():
            if dialog.is_channel:
                print(f"{dialog.name:<30} {dialog.id:<20}")
                
    except Exception as e:
        logger.error(f"Error listing channels: {e}")
    finally:
        if client.is_connected():
            await client.disconnect()

async def main():
    """Main function for running the Telegram crawler."""
    # Setup the database tables
    setup_database()
    
    # Print a menu
    print("\nTelegram Crawler for KeyBoxer")
    print("=" * 40)
    print("1. Add channel")
    print("2. List tracked channels")
    print("3. List available channels")
    print("4. Run one-time scrape")
    print("5. Start continuous scraping")
    print("6. View statistics")
    print("7. Exit")
    
    choice = input("\nSelect an option: ")
    
    if choice == "1":
        channel_id = input("Enter channel ID or username: ")
        add_channel(channel_id)
        print(f"Channel {channel_id} added")
        
    elif choice == "2":
        channels = get_channels()
        print("\nTracked channels:")
        for channel_id, channel_name, last_message_id in channels:
            print(f"- {channel_name or 'Unknown'} ({channel_id}) - Last message: {last_message_id}")
            
    elif choice == "3":
        await list_available_channels()
        
    elif choice == "4":
        print("Starting one-time scrape...")
        await one_time_scrape()
        
    elif choice == "5":
        print("Starting continuous scraping. Press Ctrl+C to stop.")
        try:
            await continuous_scraping()
        except KeyboardInterrupt:
            print("\nStopping continuous scraping...")
            
    elif choice == "6":
        # View statistics
        conn = sqlite3.connect(TELEGRAM_DB)
        c = conn.cursor()
        
        c.execute('SELECT COUNT(*) FROM messages')
        message_count = c.fetchone()[0]
        
        c.execute('SELECT COUNT(*) FROM messages WHERE keybox_found = 1')
        keyboxes_found = c.fetchone()[0]
        
        c.execute('SELECT COUNT(*) FROM keyboxes WHERE valid = 1')
        valid_keyboxes = c.fetchone()[0]
        
        c.execute('SELECT channel_name, COUNT(*) as msg_count FROM messages JOIN channels ON messages.channel_id = channels.channel_id GROUP BY messages.channel_id ORDER BY msg_count DESC')
        channel_stats = c.fetchall()
        
        conn.close()
        
        print("\nStatistics:")
        print(f"Total messages processed: {message_count}")
        print(f"Keyboxes found: {keyboxes_found}")
        print(f"Valid keyboxes: {valid_keyboxes}")
        
        print("\nMessages per channel:")
        for channel_name, count in channel_stats:
            print(f"- {channel_name or 'Unknown'}: {count} messages")
            
    elif choice == "7":
        print("Exiting...")
        return
        
    else:
        print("Invalid choice")
    
    # Run main again for menu loop
    await main()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nScript interrupted. Exiting...")
