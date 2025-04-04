name: Telegram Channel Discovery (Minimal)

on:
  schedule:
    - cron: '0 0 * * 0'  # Run weekly on Sundays
  workflow_dispatch:  # Allow manual triggering

permissions:
  contents: write
  id-token: write

jobs:
  discover-channels:
    runs-on: ubuntu-latest
    timeout-minutes: 120  # Longer timeout for extensive discovery
    
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
          
      - name: Add project directory to PYTHONPATH
        run: |
          echo "PYTHONPATH=$PYTHONPATH:${{ github.workspace }}" >> $GITHUB_ENV
          
      - name: Setup Telegram session
        run: |
          python - <<EOF
          import os
          import asyncio
          from telethon import TelegramClient
          from telethon.sessions import StringSession
          
          async def setup_telegram():
              # Get credentials from environment variables
              api_id = int(os.environ.get('TELEGRAM_API_ID'))
              api_hash = os.environ.get('TELEGRAM_API_HASH')
              session_string = os.environ.get('TELEGRAM_SESSION_STRING')
              
              if not session_string:
                  print("Error: No session string provided. Cannot proceed.")
                  exit(1)
              
              # Create a client using the session string
              client = TelegramClient(StringSession(session_string), api_id, api_hash)
              
              # Start the client (no interaction needed with session string)
              await client.start()
              
              # Print info to verify connection
              me = await client.get_me()
              print(f"Connected as: {me.first_name} (ID: {me.id})")
              
              # Test listing some dialogs
              dialog_count = 0
              channel_count = 0
              async for dialog in client.iter_dialogs(limit=50):
                  dialog_count += 1
                  if dialog.is_channel:
                      channel_count += 1
              
              print(f"Successfully accessed {dialog_count} dialogs")
              print(f"Found {channel_count} channels in the first 50 dialogs")
              
              # Save session to file for other steps to use
              os.makedirs('telegram_session', exist_ok=True)
              with open('telegram_session/telegram_session', 'w') as f:
                  f.write(session_string)
              
              await client.disconnect()
          
          asyncio.run(setup_telegram())
          EOF
        env:
          TELEGRAM_API_ID: ${{ secrets.TELEGRAM_API_ID }}
          TELEGRAM_API_HASH: ${{ secrets.TELEGRAM_API_HASH }}
          TELEGRAM_SESSION_STRING: ${{ secrets.TELEGRAM_SESSION_STRING }}
      
      - name: Run Telegram channel discovery
        run: |
          python - <<EOF
          import os
          import asyncio
          import sqlite3
          import json
          import logging
          from pathlib import Path
          from telegram_discovery import run_discovery, setup_database
          
          # Setup logging
          logging.basicConfig(
              level=logging.INFO,
              format='%(asctime)s - %(levelname)s - %(message)s'
          )
          logger = logging.getLogger("discovery_runner")
          
          # Initialize database
          setup_database()
          
          # Run discovery process (with auto-leaving of channels)
          asyncio.run(run_discovery(leave_after_completion=True))
          
          # Extract discovered channels for next runs
          db_path = Path("telegram_data.db")
          if db_path.exists():
              conn = sqlite3.connect(db_path)
              c = conn.cursor()
              
              # Get joined channels
              c.execute('SELECT channel_id FROM discovered_channels WHERE join_status = "joined"')
              joined_channels = [row[0] for row in c.fetchall()]
              
              # Get pending channels
              c.execute('SELECT channel_id FROM discovered_channels WHERE join_status = "pending"')
              pending_channels = [row[0] for row in c.fetchall()]
              
              # Create a combined list of all channels
              all_channels = joined_channels.copy()
              
              # Also get already tracked channels
              c.execute('SELECT channel_id FROM channels')
              tracked_channels = [row[0] for row in c.fetchall()]
              
              # Combine all lists without duplicates
              for channel in tracked_channels:
                  if channel not in all_channels:
                      all_channels.append(channel)
              
              # Save results
              channel_data = {
                  "joined": joined_channels,
                  "pending": pending_channels,
                  "all": all_channels
              }
              
              with open('discovered_channels.json', 'w') as f:
                  json.dump(channel_data, f, indent=2)
              
              print(f"\nDiscovery Summary:")
              print(f"- Joined channels: {len(joined_channels)}")
              print(f"- Pending channels: {len(pending_channels)}")
              print(f"- Total tracked channels: {len(all_channels)}")
              
              conn.close()
          EOF
        env:
          TELEGRAM_API_ID: ${{ secrets.TELEGRAM_API_ID }}
          TELEGRAM_API_HASH: ${{ secrets.TELEGRAM_API_HASH }}
          TELEGRAM_PHONE: ${{ secrets.TELEGRAM_PHONE }}
          TELEGRAM_SESSION_STRING: ${{ secrets.TELEGRAM_SESSION_STRING }}
      
      - name: Run Keyboxer on discovered channels
        run: |
          python - <<EOF
          import os
          import asyncio
          import json
          import logging
          from telegram_crawler import setup_database, add_channel, one_time_scrape
          
          # Setup logging
          logging.basicConfig(
              level=logging.INFO,
              format='%(asctime)s - %(levelname)s - %(message)s'
          )
          logger = logging.getLogger("crawler_runner")
          
          # Initialize database
          setup_database()
          
          # Add all discovered channels to tracking
          discovered_file = 'discovered_channels.json'
          if os.path.exists(discovered_file):
              with open(discovered_file, 'r') as f:
                  channel_data = json.load(f)
                  
              # Add joined channels to tracking
              for channel in channel_data.get('joined', []):
                  add_channel(channel)
                  logger.info(f"Added joined channel {channel} to tracking")
                  
              # Also add existing tracked channels from secret
              telegram_channels = os.environ.get('TELEGRAM_CHANNELS')
              if telegram_channels:
                  channels_to_add = json.loads(telegram_channels)
                  for channel in channels_to_add:
                      add_channel(channel)
                      logger.info(f"Added channel {channel} from secret to tracking")
                      
              # Run the crawler on all tracked channels
              asyncio.run(one_time_scrape())
          else:
              logger.error("No discovered channels file found")
          EOF
        env:
          TELEGRAM_API_ID: ${{ secrets.TELEGRAM_API_ID }}
          TELEGRAM_API_HASH: ${{ secrets.TELEGRAM_API_HASH }}
          TELEGRAM_PHONE: ${{ secrets.TELEGRAM_PHONE }}
          TELEGRAM_SESSION_STRING: ${{ secrets.TELEGRAM_SESSION_STRING }}
          TELEGRAM_CHANNELS: ${{ secrets.TELEGRAM_CHANNELS }}
      
      - name: Update GitHub secret with discovered channels
        if: ${{ github.event_name != 'pull_request' }}
        run: |
          if [ -f "discovered_channels.json" ]; then
            # Extract the "all" channels array and format it for GitHub secrets
            ALL_CHANNELS=$(cat discovered_channels.json | jq -c '.all')
            
            # Using GitHub CLI to update the secret
            echo "Updating TELEGRAM_CHANNELS secret with discovered channels"
            echo "$ALL_CHANNELS" | gh secret set TELEGRAM_CHANNELS
            
            echo "Secret updated successfully"
          else
            echo "No discovered channels file found, skipping secret update"
          fi
        env:
          GH_TOKEN: ${{ github.token }}
            
      - name: Run standard KeyBoxer
        run: |
          python keyboxer.py
        env:
          GITHUB_TOKEN: ${{ secrets.PAT_TOKEN }}
          TELEGRAM_SESSION_STRING: ${{ secrets.TELEGRAM_SESSION_STRING }}
      
      - name: List discovered keyboxes
        run: |
          echo "Keyboxes discovered from all sources:"
          ls -la keys/
          
          # Count keyboxes
          KEYBOX_COUNT=$(ls -1 keys/*.xml 2>/dev/null | wc -l)
          echo "Total keyboxes found: $KEYBOX_COUNT"
      
      - name: Commit and push keyboxes
        run: |
          # Configure git
          git config --global user.name "GitHub Actions Bot"
          git config --global user.email "actions@github.com"
          
          # Add discovered keyboxes
          git add keys/*.xml
          
          # Add discovered channels JSON
          git add discovered_channels.json
          
          # Check if there are changes to commit
          if git diff --staged --quiet; then
            echo "No changes to commit"
          else
            # Commit and push changes
            git commit -m "Add newly discovered keyboxes [automated]"
            git push
            echo "Committed and pushed new keyboxes"
          fi
