import os
import json
import yt_dlp
import re
import argparse
import time
import sys
from datetime import datetime
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

# --- FILE PATHS ---
CONFIG_FILE = "config.json"
DATABASE_FILE = "database.json"
TOKENS_FILE = "tokens.json"
COOKIES_FILE = "cookies.txt"

# ==========================================
# UTILS
# ==========================================
def load_json(filepath, default_value=None):
    if not os.path.exists(filepath):
        return default_value if default_value is not None else {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading {filepath}: {e}")
        return default_value if default_value is not None else {}

def save_json(filepath, data):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"Error saving {filepath}: {e}")
        return False

def get_auth_service(account):
    creds = Credentials(
        token=None,
        refresh_token=account['refresh_token'],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=account['client_id'],
        client_secret=account['client_secret']
    )
    return build('youtube', 'v3', credentials=creds)

# ==========================================
# 1. SCANNER (Targeted for Videos vs Shorts)
# ==========================================
def update_channel_queues(channel_urls, uploaded_ids, all_queues, is_shorts=False):
    type_label = "SHORTS" if is_shorts else "LONG"
    for channel_url in channel_urls:
        print(f"🔍 SCANNING {type_label}: {channel_url}")
        
        # Determine tab: /shorts for shorts, /videos for long form
        base_url = channel_url.rstrip("/")
        scan_url = f"{base_url}/shorts" if is_shorts else f"{base_url}/videos"

        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'tv']}}
        }
        
        if channel_url not in all_queues:
            all_queues[channel_url] = []
        else:
            # Regular scan, just check latest 15 to save time
            ydl_opts['playlistend'] = 15

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                if os.path.exists(COOKIES_FILE): 
                    ydl_opts['cookiefile'] = COOKIES_FILE
                
                info = ydl.extract_info(scan_url, download=False)
                if 'entries' in info:
                    new_count = 0
                    entries = list(info['entries'])
                    # Process oldest to newest so queue stays in chronological order
                    entries.reverse() 
                    
                    for e in entries:
                        video_id = e.get('id')
                        video_url = f"https://www.youtube.com/watch?v={video_id}"
                        
                        if video_id and video_id not in uploaded_ids and video_url not in all_queues[channel_url]:
                            all_queues[channel_url].append(video_url)
                            new_count += 1
                    print(f"   -> Found {new_count} new videos.")
        except Exception as e:
            print(f"   ❌ Scan failed: {e}")
            
    return all_queues

# ==========================================
# 2. DOWNLOADER (Multi-Strategy)
# ==========================================
def download_video(url):
    print(f"📥 DOWNLOADING: {url}")
    
    # We try different "clients" to bypass YouTube's bot detection
    strategies = [
        {'player_client': ['ios', 'android']},
        {'player_client': ['tv']},
        {'player_client': ['web_safari']}
    ]

    for strategy in strategies:
        ydl_opts = {
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
            'outtmpl': 'tmp_video.%(ext)s',
            'writethumbnail': True,
            'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}],
            'extractor_args': {'youtube': strategy},
            'quiet': True
        }
        if os.path.exists(COOKIES_FILE): ydl_opts['cookiefile'] = COOKIES_FILE

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                return {
                    'video_file': 'tmp_video.mp4',
                    'thumb_file': 'tmp_video.jpg',
                    'title': info.get('title', 'Untitled'),
                    'description': info.get('description', ''),
                    'video_id': info.get('id')
                }
        except Exception as e:
            print(f"   ⚠️ Strategy {strategy} failed, trying next...")
            
    return None

# ==========================================
# 3. UPLOADER (With Quota Failover)
# ==========================================
def attempt_upload(video_data, accounts, settings):
    print(f"📤 UPLOADING: {video_data['title']}")
    
    body = {
        'snippet': {
            'title': video_data['title'][:100],
            'description': video_data['description'][:5000],
            'categoryId': settings.get('category_id', '22'),
        },
        'status': {
            'privacyStatus': settings.get('privacy_status', 'public'),
            'selfDeclaredMadeForKids': settings.get('made_for_kids', False),
        }
    }

    for account in accounts:
        print(f"   -> Account: {account['name']}")
        try:
            youtube = get_auth_service(account)
            media = MediaFileUpload(video_data['video_file'], chunksize=-1, resumable=True)
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status: print(f"      -> {int(status.progress() * 100)}% complete...")
            
            uploaded_id = response['id']
            print(f"   ✅ Success! ID: {uploaded_id}")

            if os.path.exists(video_data['thumb_file']):
                try:
                    youtube.thumbnails().set(videoId=uploaded_id, media_body=MediaFileUpload(video_data['thumb_file'])).execute()
                except: print("      -> Thumbnail failed (Channel might not be verified).")
            
            return uploaded_id

        except HttpError as e:
            # Check for quota errors to try next account
            if e.resp.status in [403, 429]:
                print(f"   ❌ Quota exceeded on {account['name']}. Trying next account...")
                continue
            print(f"   ❌ API Error: {e}")
            break
        except Exception as e:
            print(f"   ❌ System Error: {e}")
            continue
    return None

# ==========================================
# 4. TRACK LOGIC (Round Robin)
# ==========================================
def process_active_track(mode, channel_list, database, tokens, settings):
    idx_key = f'last_{mode}_index'
    idx = database['state'].get(idx_key, -1)
    
    total = len(channel_list)
    if total == 0:
        print(f"💤 No {mode} channels configured.")
        return False

    # Check each channel starting from the one after the last used
    for i in range(total):
        curr_idx = (idx + 1 + i) % total
        curr_channel = channel_list[curr_idx]
        
        queue = database['queues'].get(curr_channel, [])
        if queue:
            target_url = queue[0]
            print(f"\n🎯 MODE [{mode.upper()}]: Processing {target_url}")
            
            video_data = download_video(target_url)
            if video_data:
                up_id = attempt_upload(video_data, tokens['accounts'], settings)
                if up_id:
                    # Successfully uploaded
                    database['queues'][curr_channel].pop(0)
                    database['uploaded_videos'].append(video_data['video_id'])
                    database['state'][idx_key] = curr_idx
                    
                    # Cleanup temp files
                    for f in ['tmp_video.mp4', 'tmp_video.jpg']:
                        if os.path.exists(f): os.remove(f)
                    return True
                else:
                    print(f"❌ Upload failed for {target_url}")
            else:
                print(f"❌ Download failed for {target_url}")
                # Optional: pop from queue if download fails repeatedly? 
                # For now, we leave it to retry.
    
    return False

# ==========================================
# MAIN EXECUTION
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['long', 'short'], required=True, help="Mode: long or short")
    args = parser.parse_args()

    print(f"🤖 BOT STARTED | MODE: {args.mode.upper()} | TIME: {datetime.now()}")

    # 1. Load Data
    config = load_json(CONFIG_FILE)
    database = load_json(DATABASE_FILE, {"uploaded_videos": [], "queues": {}, "state": {}})
    
    # Auth loading (From file or Env)
    tokens = {"accounts": []}
    if os.path.exists(TOKENS_FILE):
        tokens = load_json(TOKENS_FILE)
    elif os.environ.get("BOT_TOKENS"):
        tokens = json.loads(os.environ.get("BOT_TOKENS"))

    # 2. Configure based on mode
    mode = args.mode
    channel_list = config.get(f'{mode}_channels', [])
    is_shorts = (mode == 'short')

    # 3. Scan & Update Queues (Only for the active mode to save API/Time)
    database['queues'] = update_channel_queues(
        channel_list, 
        database['uploaded_videos'], 
        database['queues'], 
        is_shorts=is_shorts
    )
    save_json(DATABASE_FILE, database)

    # 4. Process Video
    success = process_active_track(mode, channel_list, database, tokens, config['upload_settings'])

    if success:
        save_json(DATABASE_FILE, database)
        print(f"🏁 {mode.upper()} task completed successfully.")
    else:
        print(f"🏁 {mode.upper()} task finished with nothing to upload.")
