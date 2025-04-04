#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Telegram Setup Helper
# This script helps set up and manage Telegram sessions and channels for KeyBoxer

import os
import sys
import json
import asyncio
import sqlite3
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Telegram API credentials
TELEGRAM_API_ID = os.getenv("TELEGRAM_API_ID")
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE")

# Paths
BASE_DIR = Path(__file__).resolve().parent
TELEGRAM_SESSION_DIR = BASE_DIR / "telegram_session"
TELEGRAM_SESSION_DIR.mkdir(exist_ok=True)
TELEGRAM_DB = BASE_DIR / "telegram_data.db"

# Check if Telethon is installed
try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import SessionPasswordNeededError
except ImportError:
    print("Telethon is not installed. Please install it with: pip install telethon")
    sys.exit(1)

def check_telegram_credentials():
    """Check if Telegram API credentials are set."""
    if not TELEGRAM_API_ID or not TELEGRAM_API_HASH:
        print("Telegram API credentials not found.")
        print("Please set TELEGRAM_API_ID and TELEGRAM_API_HASH in your .env file.")
        return False
    return True

async def create_telegram_session():
    """Create a new Telegram session and generate a session string."""
    if not check_telegram_credentials():
        return
        
    print("\nCreating new Telegram session...")
    print("This will log in to your Telegram account and create a session.")
    
    # Use phone number from .env file or prompt user
    phone = TELEGRAM_PHONE
    if not phone:
        phone = input("Enter your phone number (with country code, e.g., +1234567890): ")
    
    client = TelegramClient(
        str(TELEGRAM_SESSION_DIR / "telegram_session"), 
        int(TELEGRAM_API_ID), 
        TELEGRAM_API_HASH
    )
    
    try:
        print("Connecting to Telegram...")
        await client.connect()
        
        if not await client.is_user_authorized():
            print(f"Sending authentication code to {phone}")
            await client.send_code_request(phone)
            code = input("Enter the code you received: ")
            
            try:
                await client.sign_in(phone, code)
            except SessionPasswordNeededError:
                password = input("Two-step verification is enabled. Please enter your password: ")
                await client.sign_in(password=password)
        
        # Generate session string
        session_string = StringSession.save(client.session)
        
        me = await client.get_me()
        print(f"\nSuccessfully logged in as {me.first_name} {me.last_name if me.last_name else ''} (@{me.username if me.username else 'No username'})")
        print("\nSession created successfully!")
        
        # Save session string to file
        session_file = BASE_DIR / ".session_string"
        with open(session_file, "w") as f:
            f.write(session_string)
        
        print(f"Session string saved to {session_file}")
        print("You can add this to your .env file as TELEGRAM_SESSION_STRING=<session_string>")
        print("Important: Keep this string secure! It can be used to access your Telegram account.")
    
    finally:
        await client.disconnect()

async def list_telegram_channels():
    """List all available Telegram channels the user has access to."""
    if not check_telegram_credentials():
        return
    
    print("\nListing available Telegram channels...")
    
    client = TelegramClient(
        str(TELEGRAM_SESSION_DIR / "telegram_session"), 
        int(TELEGRAM_API_ID), 
        TELEGRAM_API_HASH
    )
    
    try:
        await client.connect()
        
        if not await client.is_user_authorized():
            print("Not authorized. Please run 'create-session' first.")
            return
        
        me = await client.get_me()
        print(f"Logged in as {me.first_name} {me.last_name if me.last_name else ''} (@{me.username if me.username else 'No username'})")
        
        print("\nAvailable channels:")
        print("-" * 60)
        print(f"{'Name':<30} {'ID':<15} {'Type':<10} {'Username':<15}")
        print("-" * 60)
        
        async for dialog in client.iter_dialogs():
            dialog_type = "Private" if dialog.is_user else "Group" if dialog.is_group else "Channel" if dialog.is_channel else "Unknown"
            entity = await client.get_entity(dialog.id)
            username = getattr(entity, 'username', 'None')
            
            # Skip Telegram service channels
            if dialog.id == 777000 or dialog.id == 1087968824:
                continue
                
            # Only show groups and channels
            if dialog.is_group or dialog.is_channel:
                print(f"{dialog.name:<30} {dialog.id:<15} {dialog_type:<10} {username or 'None':<15}")
        
        print("\nTo add a channel to your tracking list, use the channel ID.")
        print("The ID should be used with the --telegram-channel option in keyboxer_integrated.py")
        
    finally:
        await client.disconnect()

def list_tracked_channels():
    """List channels currently being tracked in the database."""
    if not TELEGRAM_DB.exists():
        print("Telegram database not found. No channels are being tracked.")
        return
        
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    try:
        # Create the channels table if it doesn't exist
        c.execute('''
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY,
            channel_id TEXT UNIQUE,
            channel_name TEXT,
            last_message_id INTEGER DEFAULT 0
        )
        ''')
        
        c.execute('SELECT channel_id, channel_name, last_message_id FROM channels')
        channels = c.fetchall()
        
        if not channels:
            print("No channels are currently being tracked.")
            return
            
        print("\nCurrently tracked channels:")
        print("-" * 70)
        print(f"{'Channel ID':<20} {'Channel Name':<30} {'Last Message ID':<15}")
        print("-" * 70)
        
        for channel_id, channel_name, last_message_id in channels:
            print(f"{channel_id:<20} {channel_name or 'Unknown':<30} {last_message_id:<15}")
            
    except sqlite3.Error as e:
        print(f"Database error: {e}")
    finally:
        conn.close()

def add_tracking_channel(channel_id, channel_name=None):
    """Add a channel to the tracking database."""
    if not channel_id:
        print("Error: Channel ID is required")
        return
        
    # Initialize database if it doesn't exist
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    try:
        # Create the channels table if it doesn't exist
        c.execute('''
        CREATE TABLE IF NOT EXISTS channels (
            id INTEGER PRIMARY KEY,
            channel_id TEXT UNIQUE,
            channel_name TEXT,
            last_message_id INTEGER DEFAULT 0
        )
        ''')
        
        # Add the channel
        c.execute(
            'INSERT OR REPLACE INTO channels (channel_id, channel_name) VALUES (?, ?)',
            (channel_id, channel_name)
        )
        
        conn.commit()
        print(f"Channel {channel_id} {'(' + channel_name + ')' if channel_name else ''} added to tracking list")
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
    finally:
        conn.close()

def remove_tracking_channel(channel_id):
    """Remove a channel from the tracking database."""
    if not channel_id:
        print("Error: Channel ID is required")
        return
        
    if not TELEGRAM_DB.exists():
        print("Telegram database not found. No channels are being tracked.")
        return
        
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    try:
        c.execute('DELETE FROM channels WHERE channel_id = ?', (channel_id,))
        
        if c.rowcount > 0:
            print(f"Channel {channel_id} removed from tracking list")
        else:
            print(f"Channel {channel_id} was not found in the tracking list")
            
        conn.commit()
    except sqlite3.Error as e:
        print(f"Database error: {e}")
    finally:
        conn.close()

def show_session_info():
    """Show information about the current Telegram session."""
    session_file = TELEGRAM_SESSION_DIR / "telegram_session.session"
    
    if not session_file.exists():
        print("No Telegram session found.")
        print("Run 'create-session' to create a new session.")
        return
        
    print("\nTelegram session info:")
    print(f"Session file: {session_file}")
    print(f"Session file size: {session_file.stat().st_size} bytes")
    print(f"Last modified: {datetime.fromtimestamp(session_file.stat().st_mtime)}")
    
    # Check if we have credentials to verify the session
    if check_telegram_credentials():
        print("Use the 'list-channels' command to verify your session is working correctly.")

def export_channels():
    """Export the list of tracked channels to a JSON file."""
    if not TELEGRAM_DB.exists():
        print("Telegram database not found. No channels to export.")
        return
        
    conn = sqlite3.connect(TELEGRAM_DB)
    c = conn.cursor()
    
    try:
        c.execute('SELECT channel_id FROM channels')
        channels = [row[0] for row in c.fetchall()]
        
        if not channels:
            print("No channels to export.")
            return
            
        channels_json = json.dumps(channels)
        
        print("\nTracked channels:")
        print(channels_json)
        print("\nAdd this to your GitHub secrets as TELEGRAM_CHANNELS to use in workflows.")
        
        # Also save to a file
        with open(BASE_DIR / "telegram_channels.json", "w") as f:
            f.write(channels_json)
            
        print(f"Exported {len(channels)} channels to telegram_channels.json")
        
    except sqlite3.Error as e:
        print(f"Database error: {e}")
    finally:
        conn.close()

def show_usage():
    """Show command line usage for the script."""
    print("\nTelegram Setup Helper for KeyBoxer")
    print("=" * 60)
    print("Usage: python telegram_setup.py [command]")
    print("\nAvailable commands:")
    print("  create-session     Create a new Telegram session")
    print("  list-channels      List available Telegram channels")
    print("  list-tracked       List channels currently being tracked")
    print("  add-channel ID     Add a channel to the tracking list")
    print("  remove-channel ID  Remove a channel from the tracking list")
    print("  export-channels    Export tracked channels as JSON for GitHub secrets")
    print("  session-info       Show information about the current session")
    print("  help               Show this help message")
    print("\nExample:")
    print("  python telegram_setup.py create-session")
    print("  python telegram_setup.py add-channel -1001234567890")

async def main():
    """Main entry point for the setup helper."""
    if len(sys.argv) < 2:
        show_usage()
        return
        
    command = sys.argv[1].lower()
    
    if command == 'create-session':
        await create_telegram_session()
    elif command == 'list-channels':
        await list_telegram_channels()
    elif command == 'list-tracked':
        list_tracked_channels()
    elif command == 'add-channel' and len(sys.argv) >= 3:
        channel_id = sys.argv[2]
        channel_name = sys.argv[3] if len(sys.argv) >= 4 else None
        add_tracking_channel(channel_id, channel_name)
    elif command == 'remove-channel' and len(sys.argv) >= 3:
        channel_id = sys.argv[2]
        remove_tracking_channel(channel_id)
    elif command == 'export-channels':
        export_channels()
    elif command == 'session-info':
        show_session_info()
    elif command == 'help':
        show_usage()
    else:
        print(f"Unknown command: {command}")
        show_usage()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nScript interrupted. Exiting...")
        sys.exit(0)
