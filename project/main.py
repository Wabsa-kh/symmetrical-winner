import os
import json
import yt_dlp
import re
import time
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
# 1. SMART SCANNER (Reference Aligned)
# ==========================================
def update_channel_queues(source_channels, uploaded_ids, all_queues):
    for channel_url in source_channels:
        print(f"\n🔍 SCANNING: {channel_url}")
        
        # Standardize URL for videos tab
        scan_url = channel_url.rstrip("/") + "/videos" if "@" in channel_url and "/videos" not in channel_url else channel_url

        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'tv']}}
        }
        
        if channel_url not in all_queues or not all_queues[channel_url]:
            print("   -> Initial scan: getting more videos.")
            all_queues[channel_url] = []
        else:
            print("   -> Regular scan: checking latest.")
            ydl_opts['playlistend'] = 20

        def _perform_scan(opts):
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(scan_url, download=False)
                if 'entries' in info:
                    entries = list(info['entries'])
                    fresh = []
                    for e in entries:
                        video_url = e.get('url') or f"https://www.youtube.com/watch?v={e.get('id')}"
                        match = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", video_url)
                        video_id = match.group(1) if match else None
                        
                        if video_id and video_id not in uploaded_ids and video_url not in all_queues[channel_url]:
                            fresh.append(video_url)
                    return fresh
            return []

        try:
            # Try Anonymous
            new_videos = _perform_scan(ydl_opts)
            if not new_videos and os.path.exists(COOKIES_FILE):
                print("   -> Anonymous scan found nothing. Trying with cookies...")
                ydl_opts['cookiefile'] = COOKIES_FILE
                new_videos = _perform_scan(ydl_opts)
            
            if new_videos:
                # Reverse to get Oldest -> Newest inside the batch
                new_videos.reverse()
                # Append to the end of existing queue
                all_queues[channel_url] = all_queues[channel_url] + new_videos
                print(f"   -> Added {len(new_videos)} new videos (Oldest first).")
            else:
                print("   -> No new videos found.")
        except Exception as e:
            print(f"   ❌ Scan failed for {channel_url}: {e}")
            
    return all_queues

# ==========================================
# 2. DOWNLOADER (Reference Aligned)
# ==========================================
def download_video(url):
    print(f"\n📥 DOWNLOADING: {url}")
    
    attempt_configs = [
        { # Strategy 1: Anonymous Mobile
            'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
            'extractor_args': {'youtube': {'player_client': ['ios', 'android']}},
            'outtmpl': 'tmp_video.%(ext)s', 'writethumbnail': True,
            'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}], 'quiet': False
        },
        { # Strategy 2: TV Client with Cookies
            'cookiefile': COOKIES_FILE, 'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
            'extractor_args': {'youtube': {'player_client': ['tv']}},
            'outtmpl': 'tmp_video.%(ext)s', 'writethumbnail': True,
            'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}], 'quiet': False
        },
        { # Strategy 3: Web Safari Client with Cookies
            'cookiefile': COOKIES_FILE, 'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
            'extractor_args': {'youtube': {'player_client': ['web_safari', 'web_creator']}},
            'outtmpl': 'tmp_video.%(ext)s', 'writethumbnail': True,
            'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}], 'quiet': False
        },
        { # Strategy 4: iOS Client with Cookies
            'cookiefile': COOKIES_FILE, 'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
            'extractor_args': {'youtube': {'player_client': ['ios', 'android']}},
            'outtmpl': 'tmp_video.%(ext)s', 'writethumbnail': True,
            'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}], 'quiet': False
        }
    ]

    for i, config in enumerate(attempt_configs):
        if 'cookiefile' in config and not os.path.exists(COOKIES_FILE):
             continue
             
        print(f"   -> Strategy {i+1}...")
        try:
            with yt_dlp.YoutubeDL(config) as ydl:
                info = ydl.extract_info(url, download=True)
                return {
                    'video_file': 'tmp_video.mp4',
                    'thumb_file': 'tmp_video.jpg',
                    'title': info.get('title', 'Untitled'),
                    'description': info.get('description', ''),
                    'video_id': info.get('id')
                }
        except Exception as e:
            print(f"   ❌ Strategy {i+1} failed: {e}")
            
    return None

# ==========================================
# 3. UPLOADER (Reference Aligned)
# ==========================================
def attempt_upload(video_data, accounts, settings):
    print(f"\n📤 UPLOADING: {video_data['title']}")
    
    body = {
        'snippet': {
            'title': video_data['title'][:100],
            'description': video_data['description'][:5000],
            'categoryId': settings['category_id'],
            'defaultLanguage': settings['language'],
            'defaultAudioLanguage': settings['language']
        },
        'status': {
            'privacyStatus': settings['privacy_status'],
            'selfDeclaredMadeForKids': settings['made_for_kids'],
            'license': 'youtube'
        }
    }

    for account in accounts:
        print(f"   -> Trying account: {account['name']}...")
        try:
            youtube = get_auth_service(account)
            media = MediaFileUpload(video_data['video_file'], chunksize=-1, resumable=True)
            request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
            
            response = None
            while response is None:
                status, response = request.next_chunk()
                if status: print(f"      -> {int(status.progress() * 100)}% complete...")
            
            uploaded_id = response['id']
            print(f"   ✅ SUCCESS! Video ID: {uploaded_id}")

            if os.path.exists(video_data['thumb_file']):
                try:
                    youtube.thumbnails().set(videoId=uploaded_id, media_body=MediaFileUpload(video_data['thumb_file'])).execute()
                    print("      -> Thumbnail attached.")
                except: print("      -> Thumbnail upload failed.")
            
            return uploaded_id

        except HttpError as e:
            try:
                err = json.loads(e.content.decode())['error']['errors'][0]
                reason = err['reason']; msg = err['message']
            except:
                reason = "unknown"; msg = str(e)
            
            print(f"   ❌ API Error: [{reason}] {msg}")
            if reason in ["quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"]: continue
            return None
        except Exception as e:
            print(f"   ❌ System Error: {e}")
            continue
    return None

# ==========================================
# 4. MAIN EXECUTION (Procedural Style)
# ==========================================
if __name__ == "__main__":
    print(f"🤖 BOT STARTED: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    config = load_json(CONFIG_FILE)
    database = load_json(DATABASE_FILE, {
        "uploaded_videos": [],
        "queues": {},
        "state": {"last_channel_index": -1}
    })
    
    # Load tokens
    tokens = {"accounts": []}
    if os.path.exists(TOKENS_FILE):
        tokens = load_json(TOKENS_FILE)
    elif os.environ.get("BOT_TOKENS"):
        try: tokens = json.loads(os.environ.get("BOT_TOKENS"))
        except: print("❌ Error parsing BOT_TOKENS env var.")

    # 1. Update Queues
    database['queues'] = update_channel_queues(config['source_channels'], database['uploaded_videos'], database['queues'])
    save_json(DATABASE_FILE, database)

    # 2. Select Video (Round Robin)
    channels = list(database['queues'].keys())
    if not channels:
        print("💤 No channels to scan.")
    else:
        idx = database['state']['last_channel_index']
        total = len(channels)
        target_video, target_channel, new_idx = None, None, idx

        for i in range(total):
            curr_idx = (idx + 1 + i) % total
            curr_channel = channels[curr_idx]
            if database['queues'][curr_channel]:
                target_video = database['queues'][curr_channel][0]
                target_channel = curr_channel
                new_idx = curr_idx
                break
        
        if not target_video:
            print("💤 All queues are empty.")
        else:
            # 3. Process Video
            print(f"\n🎯 SELECTED: {target_video} from {target_channel}")
            video_data = download_video(target_video)
            
            if video_data:
                uploaded_id = attempt_upload(video_data, tokens['accounts'], config['upload_settings'])
                if uploaded_id:
                    database['queues'][target_channel].pop(0)
                    database['uploaded_videos'].append(video_data['video_id'])
                    database['state']['last_channel_index'] = new_idx
                    save_json(DATABASE_FILE, database)
                    
                    # Clean temp files
                    for f in [video_data['video_file'], video_data['thumb_file']]:
                        if os.path.exists(f): os.remove(f)
                    print("\n🎉 SYSTEM FINISHED.")
                else:
                    print("\n❌ Failed to upload. Video remains in queue.")
            else:
                print("\n❌ Failed to download. Video remains in queue.")
