name: Run KeyBoxer with Telegram Crawler

on:
  schedule:
    - cron: '0 */6 * * *'  # Run every 6 hours
  workflow_dispatch:  # Allow manual triggering

permissions:
  contents: write
  id-token: write

jobs:
  run-keyboxer:
    runs-on: ubuntu-latest
    timeout-minutes: 60  # Increased timeout for Telegram crawling
    
    steps:
      - name: Checkout repository
        uses: actions/checkout@v3
      
      - name: Set up Python
        uses: actions/setup-python@v4
        with:
          python-version: '3.10'
      
      - name: Install dependencies
        run: |
          pip install requests python-dotenv beautifulsoup4 lxml cryptography telethon aiohttp asyncio
      
      - name: Create required directories
        run: |
          mkdir -p keys
          mkdir -p telegram_session
          touch cache.txt
      
      - name: Create .env file with tokens
        run: |
          echo "GITHUB_TOKEN=${{ secrets.PAT_TOKEN }}" > .env
          echo "TELEGRAM_API_ID=${{ secrets.TELEGRAM_API_ID }}" >> .env
          echo "TELEGRAM_API_HASH=${{ secrets.TELEGRAM_API_HASH }}" >> .env
          echo "TELEGRAM_PHONE=${{ secrets.TELEGRAM_PHONE }}" >> .env
          echo "TELEGRAM_SESSION_STRING=${{ secrets.TELEGRAM_SESSION_STRING }}" >> .env
          echo "GH_TOKEN=${{ github.token }}" >> .env
      
      - name: Setup Telegram session
        run: |
          python - <<EOF
          import os
          import asyncio
          from telethon import TelegramClient
          from telethon.sessions import StringSession
          
          async def setup_telegram():
              api_id = os.environ.get('TELEGRAM_API_ID')
              api_hash = os.environ.get('TELEGRAM_API_HASH')
              phone = os.environ.get('TELEGRAM_PHONE')
              session_string = os.environ.get('TELEGRAM_SESSION_STRING')
              
              if not session_string:
                  print("No session string found. Creating a new session.")
                  client = TelegramClient('telegram_session/telegram_session', int(api_id), api_hash)
                  await client.start(phone=phone)
                  print("Telegram session created.")
              else:
                  print("Using existing session string.")
                  client = TelegramClient(StringSession(session_string), int(api_id), api_hash)
                  await client.start()
                  print("Telegram session restored from string.")
              
              # List some basic info to verify the session works
              me = await client.get_me()
              print(f"Connected as: {me.first_name} (ID: {me.id})")
              
              # Save session for later use if needed
              if client.session and hasattr(client.session, 'save'):
                  client.session.save()
              
              await client.disconnect()
          
          asyncio.run(setup_telegram())
          EOF
        env:
          TELEGRAM_API_ID: ${{ secrets.TELEGRAM_API_ID }}
          TELEGRAM_API_HASH: ${{ secrets.TELEGRAM_API_HASH }}
          TELEGRAM_PHONE: ${{ secrets.TELEGRAM_PHONE }}
          TELEGRAM_SESSION_STRING: ${{ secrets.TELEGRAM_SESSION_STRING }}
      
      - name: Run Telegram crawler for keyboxes
        run: |
          python - <<EOF
          import os
          import asyncio
          import sqlite3
          import json
          import logging
          from pathlib import Path
          from telegram_crawler import setup_database, add_channel, one_time_scrape
          
          # Setup logging
          logging.basicConfig(
              level=logging.INFO,
              format='%(asctime)s - %(levelname)s - %(message)s'
          )
          logger = logging.getLogger("telegram_runner")
          
          # Initialize database
          setup_database()
          
          # Add default channels to track
          # These are known channels that might have keybox files
          DEFAULT_CHANNELS = [
              "-1001234567890",  # Replace with actual channel IDs
              "keybox_group",
              "androiddebug"
          ]
          
          # If TELEGRAM_CHANNELS is set in secrets, use those instead
          telegram_channels = os.environ.get('TELEGRAM_CHANNELS')
          if telegram_channels:
              channels_to_add = json.loads(telegram_channels)
          else:
              channels_to_add = DEFAULT_CHANNELS
          
          # Add all channels
          for channel in channels_to_add:
              add_channel(channel)
              logger.info(f"Added channel {channel} for tracking")
          
          # Run one-time scrape
          asyncio.run(one_time_scrape())
          
          # Show summary of findings
          db_path = Path("telegram_data.db")
          if db_path.exists():
              conn = sqlite3.connect(db_path)
              c = conn.cursor()
              
              c.execute('SELECT COUNT(*) FROM keyboxes WHERE valid = 1')
              valid_count = c.fetchone()[0]
              
              c.execute('SELECT channel_name, COUNT(*) FROM keyboxes JOIN channels ON keyboxes.channel_id = channels.channel_id WHERE keyboxes.valid = 1 GROUP BY keyboxes.channel_id')
              channel_stats = c.fetchall()
              
              print(f"\nSummary: Found {valid_count} valid keyboxes")
              if channel_stats:
                  print("Valid keyboxes by channel:")
                  for channel, count in channel_stats:
                      print(f"- {channel or 'Unknown'}: {count}")
              
              conn.close()
          EOF
        env:
          TELEGRAM_API_ID: ${{ secrets.TELEGRAM_API_ID }}
          TELEGRAM_API_HASH: ${{ secrets.TELEGRAM_API_HASH }}
          TELEGRAM_PHONE: ${{ secrets.TELEGRAM_PHONE }}
          TELEGRAM_CHANNELS: ${{ secrets.TELEGRAM_CHANNELS }}
          TELEGRAM_SESSION_STRING: ${{ secrets.TELEGRAM_SESSION_STRING }}
      
      - name: Run original KeyBoxer scraper
        run: |
          python keyboxer.py
        env:
          GITHUB_TOKEN: ${{ secrets.PAT_TOKEN }}
      
      - name: List discovered keyboxes
        run: |
          echo "Keyboxes discovered from all sources:"
          ls -la keys/
          
          # Count keyboxes
          KEYBOX_COUNT=$(ls -1 keys/*.xml 2>/dev/null | wc -l)
          echo "Total keyboxes found: $KEYBOX_COUNT"
          
          # Validate all keyboxes one more time to ensure they are valid
          python - <<EOF
          import os
          import glob
          from check import keybox_check
          
          valid_count = 0
          invalid_count = 0
          
          for file_path in glob.glob('keys/*.xml'):
              with open(file_path, 'rb') as file:
                  content = file.read()
                  if keybox_check(content):
                      valid_count += 1
                  else:
                      invalid_count += 1
                      print(f"Warning: Invalid keybox detected: {file_path}")
          
          print(f"\nValidation results:")
          print(f"- Valid keyboxes: {valid_count}")
          print(f"- Invalid keyboxes: {invalid_count}")
          EOF
      
      - name: Create combined report
        run: |
          python - <<EOF
          import os
          import json
          import sqlite3
          import glob
          import hashlib
          from datetime import datetime
          
          # Create report data structure
          report = {
              "timestamp": datetime.now().isoformat(),
              "keyboxes_total": len(glob.glob('keys/*.xml')),
              "sources": {
                  "github": 0,
                  "web": 0,
                  "telegram": 0
              },
              "telegram_channels": [],
              "latest_keyboxes": []
          }
          
          # Count Telegram keyboxes
          if os.path.exists('telegram_data.db'):
              conn = sqlite3.connect('telegram_data.db')
              c = conn.cursor()
              
              c.execute('SELECT COUNT(*) FROM keyboxes WHERE valid = 1')
              report["sources"]["telegram"] = c.fetchone()[0]
              
              c.execute('''
                  SELECT 
                      channels.channel_name, 
                      channels.channel_id, 
                      COUNT(keyboxes.id) as keybox_count 
                  FROM 
                      keyboxes 
                  JOIN 
                      channels ON keyboxes.channel_id = channels.channel_id 
                  WHERE 
                      keyboxes.valid = 1 
                  GROUP BY 
                      keyboxes.channel_id
              ''')
              
              for channel_name, channel_id, count in c.fetchall():
                  report["telegram_channels"].append({
                      "name": channel_name or "Unknown",
                      "id": channel_id,
                      "keyboxes": count
                  })
                  
              # Get latest keyboxes
              c.execute('''
                  SELECT 
                      keyboxes.hash,
                      keyboxes.file_path,
                      messages.date,
                      channels.channel_name
                  FROM 
                      keyboxes 
                  JOIN 
                      messages ON keyboxes.message_id = messages.message_id AND keyboxes.channel_id = messages.channel_id
                  JOIN 
                      channels ON keyboxes.channel_id = channels.channel_id 
                  WHERE 
                      keyboxes.valid = 1 
                  ORDER BY 
                      messages.date DESC
                  LIMIT 10
              ''')
              
              for hash_value, file_path, date, channel_name in c.fetchall():
                  report["latest_keyboxes"].append({
                      "hash": hash_value,
                      "source": f"Telegram: {channel_name or 'Unknown'}",
                      "date": date
                  })
                  
              conn.close()
          
          # Save report
          with open('keybox_report.json', 'w') as f:
              json.dump(report, f, indent=2)
              
          print("Created keybox report: keybox_report.json")
          EOF
      
      - name: Create compressed archive of all keyboxes
        run: |
          # Create a zip file with all keyboxes
          zip -r keyboxes.zip keys/
          
          # Print stats about the archive
          echo "Created keyboxes.zip with all discovered keyboxes"
          ls -la keyboxes.zip
      
      - name: Store keyboxes as artifact
        uses: actions/upload-artifact@v3
        with:
          name: keyboxes
          path: keyboxes.zip
      
      - name: Upload report as artifact
        uses: actions/upload-artifact@v3
        with:
          name: report
          path: keybox_report.json
      
      - name: Upload to gist
        if: ${{ github.event_name != 'pull_request' }}
        run: |
          # Get current date for the gist description
          DATE=$(date -u +"%Y-%m-%d %H:%M:%S UTC")
          
          # Create a JSON payload for the GitHub API
          cat > gist-payload.json << EOF
          {
            "description": "KeyBoxer crawling results - $DATE",
            "public": false,
            "files": {
              "keybox_report.json": {
                "content": $(cat keybox_report.json)
              }
            }
          }
          EOF
          
          # Check if we have an existing Gist ID stored
          GIST_ID_FILE=".gist-id"
          GIST_ID=""
          if [ -f "$GIST_ID_FILE" ]; then
            GIST_ID=$(cat "$GIST_ID_FILE")
            echo "Found existing Gist ID: $GIST_ID"
            
            # Update existing Gist
            RESPONSE=$(curl -s -X PATCH \
              -H "Authorization: token ${{ secrets.GIST_TOKEN }}" \
              -H "Accept: application/vnd.github.v3+json" \
              -d @gist-payload.json \
              "https://api.github.com/gists/$GIST_ID")
              
            echo "Updated existing Gist"
          else
            # Create new Gist
            RESPONSE=$(curl -s -X POST \
              -H "Authorization: token ${{ secrets.GIST_TOKEN }}" \
              -H "Accept: application/vnd.github.v3+json" \
              -d @gist-payload.json \
              "https://api.github.com/gists")
              
            # Extract and save Gist ID for future updates
            GIST_ID=$(echo "$RESPONSE" | grep -o '"id": "[^"]*' | head -1 | cut -d'"' -f4)
            echo "$GIST_ID" > "$GIST_ID_FILE"
            echo "Created new Gist with ID: $GIST_ID"
          fi
          
          # Now upload the zip file to the Gist (encoded as base64)
          if [ -f "keyboxes.zip" ]; then
            # First, base64 encode the zip file
            BASE64_ZIP=$(base64 -w 0 keyboxes.zip)
            
            # Create a JSON payload for updating the Gist with the zip file
            cat > gist-update-payload.json << EOF
            {
              "files": {
                "keyboxes.zip.base64": {
                  "content": "$BASE64_ZIP"
                }
              }
            }
            EOF
            
            # Update the Gist with the encoded zip file
            curl -s -X PATCH \
              -H "Authorization: token ${{ secrets.GIST_TOKEN }}" \
              -H "Accept: application/vnd.github.v3+json" \
              -d @gist-update-payload.json \
              "https://api.github.com/gists/$GIST_ID"
              
            echo "Uploaded base64-encoded zip file to Gist"
            echo "You can find your files at: https://gist.github.com/$GIST_ID"
            echo "This Gist is private and only visible to you"
          else
            echo "No keyboxes.zip file found to upload"
          fi
        env:
          GIST_TOKEN: ${{ secrets.GIST_TOKEN }}
