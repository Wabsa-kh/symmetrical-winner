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
# UTILS & MIGRATION
# ==========================================
def load_json(filepath, default_value=None):
    if not os.path.exists(filepath):
        return default_value if default_value is not None else {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        
        # --- DATABASE MIGRATION LOGIC ---
        # If we detect old keys (no #long or #short), we move them to #long by default
        if filepath == DATABASE_FILE and "queues" in data:
            updated_queues = {}
            migration_happened = False
            for key, val in data["queues"].items():
                if "#long" not in key and "#short" not in key:
                    new_key = f"{key}#long"
                    updated_queues[new_key] = val
                    migration_happened = True
                    print(f"📦 Migrated old queue: {key} -> {new_key}")
                else:
                    updated_queues[key] = val
            if migration_happened:
                data["queues"] = updated_queues
        return data
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
# 1. SCANNER (Uses Unique Keys)
# ==========================================
def update_channel_queues(channel_urls, uploaded_ids, all_queues, is_shorts=False):
    mode_suffix = "#short" if is_shorts else "#long"
    
    for channel_url in channel_urls:
        queue_key = f"{channel_url}{mode_suffix}"
        print(f"🔍 SCANNING: {queue_key}")
        
        base_url = channel_url.rstrip("/")
        scan_url = f"{base_url}/shorts" if is_shorts else f"{base_url}/videos"

        if queue_key not in all_queues:
            all_queues[queue_key] = []

        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'tv']}}
        }
        
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                if os.path.exists(COOKIES_FILE): ydl_opts['cookiefile'] = COOKIES_FILE
                
                info = ydl.extract_info(scan_url, download=False)
                if 'entries' in info:
                    new_count = 0
                    entries = list(info['entries'])
                    entries.reverse() # Oldest to Newest
                    
                    for e in entries:
                        video_id = e.get('id')
                        video_url = f"https://www.youtube.com/watch?v={video_id}"
                        
                        # Master check: Not uploaded AND not already in THIS specific queue
                        if video_id and video_id not in uploaded_ids and video_url not in all_queues[queue_key]:
                            all_queues[queue_key].append(video_url)
                            new_count += 1
                    print(f"   -> Found {new_count} new videos.")
        except Exception as e:
            print(f"   ❌ Scan failed: {e}")
            
    return all_queues

# ==========================================
# 2. DOWNLOADER
# ==========================================
def download_video(url):
    print(f"📥 DOWNLOADING: {url}")
    out_video = 'tmp_video.mp4'
    out_thumb = 'tmp_video.jpg'
    
    # Cleanup previous
    for f in [out_video, out_thumb]:
        if os.path.exists(f): os.remove(f)

    ydl_opts = {
        'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
        'outtmpl': 'tmp_video.%(ext)s',
        'writethumbnail': True,
        'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}],
        'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'tv']}},
        'quiet': True
    }
    if os.path.exists(COOKIES_FILE): ydl_opts['cookiefile'] = COOKIES_FILE

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return {
                'video_file': out_video,
                'thumb_file': out_thumb,
                'title': info.get('title', 'Untitled'),
                'description': info.get('description', ''),
                'video_id': info.get('id')
            }
    except Exception as e:
        print(f"   ❌ Download failed: {e}")
    return None

# ==========================================
# 3. UPLOADER
# ==========================================
def attempt_upload(video_data, accounts, settings):
    print(f"📤 UPLOADING: {video_data['title']}")
    body = {
        'snippet': {
            'title': video_data['title'][:100],
            'description': video_data['description'][:5000],
            'categoryId': settings.get('category_id', '24'),
        },
        'status': {
            'privacyStatus': settings.get('privacy_status', 'public'),
            'selfDeclaredMadeForKids': settings.get('made_for_kids', False),
        }
    }

    for account in accounts:
        try:
            youtube = get_auth_service(account)
            media = MediaFileUpload(video_data['video_file'], chunksize=-1, resumable=True)
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            
            res = None
            while res is None:
                status, res = request.next_chunk()
                if status: print(f"      -> {int(status.progress() * 100)}% complete...")
            
            uploaded_id = res['id']
            print(f"   ✅ Success! ID: {uploaded_id}")

            if os.path.exists(video_data['thumb_file']):
                try:
                    youtube.thumbnails().set(videoId=uploaded_id, media_body=MediaFileUpload(video_data['thumb_file'])).execute()
                except: pass
            
            return uploaded_id
        except HttpError as e:
            if e.resp.status in [403, 429]: continue # Quota next
            print(f"   ❌ API Error: {e}"); break
        except Exception as e:
            print(f"   ❌ Error: {e}"); continue
    return None

# ==========================================
# 4. TRACK LOGIC
# ==========================================
def process_track(mode, channel_list, database, tokens, settings):
    mode_suffix = "#short" if mode == 'short' else "#long"
    idx_key = f'last_{mode}_index'
    idx = database['state'].get(idx_key, -1)
    
    total = len(channel_list)
    if total == 0: return False

    for i in range(total):
        curr_idx = (idx + 1 + i) % total
        channel_url = channel_list[curr_idx]
        queue_key = f"{channel_url}{mode_suffix}"
        
        queue = database['queues'].get(queue_key, [])
        if queue:
            target_url = queue[0]
            print(f"\n🎯 [{mode.upper()}]: {target_url}")
            
            video_data = download_video(target_url)
            if video_data:
                up_id = attempt_upload(video_data, tokens['accounts'], settings)
                if up_id:
                    database['queues'][queue_key].pop(0)
                    database['uploaded_videos'].append(video_data['video_id'])
                    database['state'][idx_key] = curr_idx
                    return True
    return False

# ==========================================
# EXECUTION
# ==========================================
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--mode', choices=['long', 'short'], required=True)
    args = parser.parse_args()

    config = load_json(CONFIG_FILE)
    database = load_json(DATABASE_FILE, {"uploaded_videos": [], "queues": {}, "state": {}})
    
    tokens = {"accounts": []}
    if os.path.exists(TOKENS_FILE): tokens = load_json(TOKENS_FILE)
    elif os.environ.get("BOT_TOKENS"): tokens = json.loads(os.environ.get("BOT_TOKENS"))

    mode = args.mode
    channel_list = config.get(f'{mode}_channels', [])
    is_shorts = (mode == 'short')

    # 1. Update active mode queue
    database['queues'] = update_channel_queues(channel_list, database['uploaded_videos'], database['queues'], is_shorts=is_shorts)
    save_json(DATABASE_FILE, database)

    # 2. Process active mode video
    success = process_track(mode, channel_list, database, tokens, config['upload_settings'])

    if success:
        save_json(DATABASE_FILE, database)
        print(f"🏁 {mode.upper()} Finished.")
