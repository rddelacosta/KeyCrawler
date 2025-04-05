#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Script to add Telegram channels before running the crawler

import os
import json
from dotenv import load_dotenv
from telegram_crawler import add_channel, setup_database

# Load environment variables (including secrets)
load_dotenv()

# Default channels to use if no channels are specified in secrets
DEFAULT_CHANNELS = [
    "-1001796581959",
    "-1002111293060",
    "-1002376413293",
    "-1002360732129"
]

def add_telegram_channels():
    """Add Telegram channels from secrets or use defaults."""
    # Initialize database
    setup_database()
    
    # Check for TELEGRAM_CHANNELS in environment variables
    telegram_channels = os.environ.get('TELEGRAM_CHANNELS')
    
    # If TELEGRAM_CHANNELS exists and is not empty, use those channels
    if telegram_channels:
        try:
            channels_to_add = json.loads(telegram_channels)
            print(f"Found {len(channels_to_add)} channels in TELEGRAM_CHANNELS")
        except json.JSONDecodeError:
            print("Error parsing TELEGRAM_CHANNELS, using default channels")
            channels_to_add = DEFAULT_CHANNELS
    else:
        print("No TELEGRAM_CHANNELS found, using default channels")
        channels_to_add = DEFAULT_CHANNELS
    
    # Add all channels to the database
    added_count = 0
    for channel in channels_to_add:
        add_channel(channel)
        print(f"Added channel {channel} to tracking")
        added_count += 1
    
    print(f"Successfully added {added_count} channels")
    return added_count

if __name__ == "__main__":
    add_telegram_channels()
