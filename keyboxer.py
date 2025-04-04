#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Integrated KeyBoxer with Telegram Support
# This script combines web scraping, GitHub API search, and Telegram scraping

import os
import sys
import argparse
import asyncio
import subprocess
import time
import logging
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("keyboxer_integrated.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("keyboxer_integrated")

# Check if Telegram credentials exist
has_telegram = bool(os.getenv("TELEGRAM_API_ID")) and bool(os.getenv("TELEGRAM_API_HASH"))

def display_banner():
    """Display a nice ASCII art banner for the project."""
    banner = """
 _  __          ______                    _           
| |/ /         / _____)                  | |          
| ' / ___  _   | /       ____ _____ _   _| | ___ ____ 
|  < / _ \| | | |       / ___) ___ | | | | |/___)  _ \\
| . | |_| | |_| \_____ | |   | ____| |_| | |___ | | | |
|_|\_\___/|____)_____)|_|   |_____)\__  |_(___/|_| |_|
                                   (____/             
                                   
    Integrated KeyBox Scraper with Telegram Support
    """
    print(banner)

def run_keyboxer():
    """Run the standard KeyBoxer scraper."""
    logger.info("Starting standard KeyBoxer scraper")
    try:
        subprocess.run([sys.executable, "keyboxer.py"], check=True)
        logger.info("Standard KeyBoxer completed successfully")
    except subprocess.CalledProcessError as e:
        logger.error(f"Standard KeyBoxer failed with error code {e.returncode}")
    except Exception as e:
        logger.error(f"Error running standard KeyBoxer: {e}")

async def run_telegram_crawler(channels=None):
    """Run the Telegram crawler."""
    if not has_telegram:
        logger.warning("Telegram credentials not found. Skipping Telegram crawler.")
        return False
        
    try:
        # Import Telegram crawler components
        from telegram_crawler import setup_database, add_channel, one_time_scrape
        
        # Initialize database
        setup_database()
        
        # Add specified channels or use default list
        if channels:
            for channel in channels:
                add_channel(channel)
                logger.info(f"Added channel {channel} for tracking")
        else:
            # Try to load channels from environment variable
            telegram_channels = os.getenv("TELEGRAM_CHANNELS")
            if telegram_channels:
                import json
                channels_list = json.loads(telegram_channels)
                for channel in channels_list:
                    add_channel(channel)
                    logger.info(f"Added channel {channel} for tracking")
            else:
                logger.warning("No Telegram channels specified. Add channels with the --telegram-channel option.")
                return False
        
        # Run the scraper
        logger.info("Starting Telegram crawler")
        await one_time_scrape()
        logger.info("Telegram crawler completed successfully")
        return True
    except Exception as e:
        logger.error(f"Error running Telegram crawler: {e}")
        return False

def check_keybox_count():
    """Check and display the number of valid keyboxes found."""
    keys_dir = Path(__file__).resolve().parent / "keys"
    xml_files = list(keys_dir.glob("*.xml"))
    
    logger.info(f"Found {len(xml_files)} keyboxes in the keys directory")
    
    # Optionally validate all keyboxes
    try:
        from check import keybox_check
        
        valid_count = 0
        invalid_count = 0
        
        for xml_file in xml_files:
            with open(xml_file, 'rb') as f:
                content = f.read()
                if keybox_check(content):
                    valid_count += 1
                else:
                    invalid_count += 1
                    logger.warning(f"Invalid keybox detected: {xml_file}")
        
        logger.info(f"Validation complete: {valid_count} valid, {invalid_count} invalid keyboxes")
    except ImportError:
        logger.warning("Could not import keybox_check. Skipping validation.")

async def main():
    """Main entry point for the integrated scraper."""
    parser = argparse.ArgumentParser(description="Integrated KeyBoxer with Telegram Support")
    parser.add_argument("--skip-keyboxer", action="store_true", help="Skip running the standard KeyBoxer scraper")
    parser.add_argument("--skip-telegram", action="store_true", help="Skip running the Telegram crawler")
    parser.add_argument("--telegram-channel", action="append", help="Add a Telegram channel to scrape (can be used multiple times)")
    parser.add_argument("--summary", action="store_true", help="Show summary of existing keyboxes without scraping")
    args = parser.parse_args()
    
    display_banner()
    
    # If only summary is requested, just check the count and exit
    if args.summary:
        check_keybox_count()
        return
    
    # Run the standard KeyBoxer scraper if not skipped
    if not args.skip_keyboxer:
        run_keyboxer()
    else:
        logger.info("Skipping standard KeyBoxer as requested")
    
    # Run the Telegram crawler if not skipped and credentials exist
    if not args.skip_telegram:
        if has_telegram:
            await run_telegram_crawler(args.telegram_channel)
        else:
            logger.warning("Telegram credentials not found in .env file. Skipping Telegram crawler.")
    else:
        logger.info("Skipping Telegram crawler as requested")
    
    # Check keybox count at the end
    check_keybox_count()
    
    logger.info("All scraping operations completed")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Script interrupted. Exiting...")
        sys.exit(0)
