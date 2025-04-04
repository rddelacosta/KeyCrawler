#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Advanced KeyBoxer with Telegram Channel Discovery
# This script combines all crawling methods with auto-discovery

import os
import sys
import argparse
import asyncio
import subprocess
import time
import logging
import random
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("keyboxer_advanced.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("keyboxer_advanced")

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
                                   
    Advanced KeyBox Scraper with Telegram Discovery
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

async def run_telegram_discovery(leave_after_completion=True):
    """Run the Telegram channel discovery process.
    
    Args:
        leave_after_completion: If True, leave all channels joined during discovery
    """
    if not has_telegram:
        logger.warning("Telegram credentials not found. Skipping Telegram discovery.")
        return False
        
    try:
        # Import Telegram discovery module
        from telegram_discovery import run_discovery
        
        # Run the discovery process
        logger.info("Starting Telegram channel discovery")
        await run_discovery(leave_after_completion=leave_after_completion)
        logger.info("Telegram discovery completed successfully")
        return True
    except Exception as e:
        logger.error(f"Error running Telegram discovery: {e}")
        return False

async def run_telegram_crawler():
    """Run the Telegram crawler on discovered channels."""
    if not has_telegram:
        logger.warning("Telegram credentials not found. Skipping Telegram crawler.")
        return False
        
    try:
        # Import Telegram crawler components
        from telegram_crawler import one_time_scrape
        
        # Run the scraper
        logger.info("Starting Telegram crawler on all tracked channels")
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
    """Main entry point for the advanced scraper."""
    parser = argparse.ArgumentParser(description="Advanced KeyBoxer with Telegram Discovery")
    parser.add_argument("--skip-keyboxer", action="store_true", help="Skip running the standard KeyBoxer scraper")
    parser.add_argument("--skip-telegram", action="store_true", help="Skip running the Telegram crawler")
    parser.add_argument("--skip-discovery", action="store_true", help="Skip Telegram channel discovery")
    parser.add_argument("--discovery-only", action="store_true", help="Only run the discovery process")
    parser.add_argument("--summary", action="store_true", help="Show summary of existing keyboxes without scraping")
    parser.add_argument("--keep-joined", action="store_true", help="Keep joined channels (don't leave after completion)")
    args = parser.parse_args()
    
    display_banner()
    
    # Determine if we should leave joined channels
    leave_after_completion = not args.keep_joined
    if leave_after_completion:
        logger.info("Will leave joined channels after completion")
    else:
        logger.info("Will keep joined channels (--keep-joined flag is set)")
    
    # If only summary is requested, just check the count and exit
    if args.summary:
        check_keybox_count()
        return
    
    # If discovery-only mode, just run discovery and exit
    if args.discovery_only:
        if has_telegram:
            await run_telegram_discovery(leave_after_completion=leave_after_completion)
        else:
            logger.error("Telegram credentials not found. Cannot run discovery.")
        return
    
    # Run the standard KeyBoxer scraper if not skipped
    if not args.skip_keyboxer:
        run_keyboxer()
    else:
        logger.info("Skipping standard KeyBoxer as requested")
    
    # Run the Telegram discovery if not skipped
    if not args.skip_discovery and has_telegram:
        await run_telegram_discovery(leave_after_completion=leave_after_completion)
        # Add a small delay between discovery and crawling
        time.sleep(random.randint(5, 10))
    else:
        if args.skip_discovery:
            logger.info("Skipping Telegram discovery as requested")
        elif not has_telegram:
            logger.warning("Telegram credentials not found. Skipping Telegram discovery.")
    
    # Run the Telegram crawler if not skipped
    if not args.skip_telegram and has_telegram:
        await run_telegram_crawler()
    else:
        if args.skip_telegram:
            logger.info("Skipping Telegram crawler as requested")
        elif not has_telegram:
            logger.warning("Telegram credentials not found. Skipping Telegram crawler.")
    
    # Check keybox count at the end
    check_keybox_count()
    
    logger.info("All scraping operations completed")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Script interrupted. Exiting...")
        sys.exit(0)
