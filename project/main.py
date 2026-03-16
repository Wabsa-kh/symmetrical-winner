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

class ConfigManager:
    @staticmethod
    def load_json(filepath, default_value=None):
        if not os.path.exists(filepath):
            return default_value if default_value is not None else {}
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading {filepath}: {e}")
            return default_value if default_value is not None else {}

    @staticmethod
    def save_json(filepath, data):
        try:
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving {filepath}: {e}")
            return False

class Scanner:
    def __init__(self, uploaded_ids):
        self.uploaded_ids = uploaded_ids

    def scan_channel(self, channel_url, current_queue):
        print(f"\n🔍 SCANNING: {channel_url}")
        ydl_opts = {
            'extract_flat': True,
            'quiet': True,
            'extractor_args': {'youtube': {'player_client': ['ios', 'android', 'tv']}}
        }
        
        # If queue is empty, scan more, else scan just the latest
        if not current_queue:
            print("   -> Initial scan for this channel.")
        else:
            ydl_opts['playlistend'] = 15

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(channel_url, download=False)
                if 'entries' in info:
                    entries = list(info['entries'])
                    new_urls = []
                    for e in entries:
                        if e['id'] and e['id'] not in self.uploaded_ids and f"https://www.youtube.com/watch?v={e['id']}" not in current_queue:
                            new_urls.append(f"https://www.youtube.com/watch?v={e['id']}")
                    
                    if new_urls:
                        print(f"   -> Found {len(new_urls)} new videos.")
                        return new_urls
        except Exception as e:
            print(f"   ⚠️ Scanner failed for {channel_url}: {e}")
        return []

class Downloader:
    def __init__(self, cookies_path=None):
        self.cookies_path = cookies_path

    def get_strategies(self):
        strategies = [
            # 1. Anonymous Mobile (Best for avoiding blocks initially)
            {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
                'extractor_args': {'youtube': {'player_client': ['ios', 'android']}},
            },
            # 2. TV Client
            {
                'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
                'extractor_args': {'youtube': {'player_client': ['tv']}},
            }
        ]
        
        # Add cookie-based strategies if cookies exist
        if self.cookies_path and os.path.exists(self.cookies_path):
            strategies.extend([
                {
                    'cookiefile': self.cookies_path,
                    'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
                    'extractor_args': {'youtube': {'player_client': ['web_safari', 'web_creator']}},
                },
                {
                    'cookiefile': self.cookies_path,
                    'format': 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
                    'extractor_args': {'youtube': {'player_client': ['ios', 'android']}},
                }
            ])
        return strategies

    def download_video(self, url):
        print(f"\n📥 DOWNLOADING: {url}")
        
        strategies = self.get_strategies()
        for i, base_config in enumerate(strategies):
            print(f"   -> Strategy {i+1}...")
            config = base_config.copy()
            config.update({
                'outtmpl': 'tmp_video.%(ext)s',
                'writethumbnail': True,
                'postprocessors': [{'key': 'FFmpegThumbnailsConvertor', 'format': 'jpg'}],
                'quiet': False
            })

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

class Uploader:
    def __init__(self, accounts, settings):
        self.accounts = accounts
        self.settings = settings

    def get_service(self, account):
        creds = Credentials(
            token=None,
            refresh_token=account['refresh_token'],
            token_uri="https://oauth2.googleapis.com/token",
            client_id=account['client_id'],
            client_secret=account['client_secret']
        )
        return build('youtube', 'v3', credentials=creds)

    def upload(self, video_data):
        print(f"\n📤 UPLOADING: {video_data['title']}")
        
        body = {
            'snippet': {
                'title': video_data['title'][:100],
                'description': video_data['description'][:5000],
                'categoryId': self.settings['category_id'],
                'defaultLanguage': self.settings['language'],
                'defaultAudioLanguage': self.settings['language']
            },
            'status': {
                'privacyStatus': self.settings['privacy_status'],
                'selfDeclaredMadeForKids': self.settings['made_for_kids'],
                'license': 'youtube'
            }
        }

        for account in self.accounts:
            print(f"   -> Trying account: {account['name']}...")
            try:
                youtube = self.get_service(account)
                
                # Video Upload
                media = MediaFileUpload(video_data['video_file'], chunksize=-1, resumable=True)
                request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
                
                response = None
                while response is None:
                    status, response = request.next_chunk()
                    if status:
                        print(f"      -> {int(status.progress() * 100)}% complete...")
                
                uploaded_id = response['id']
                print(f"   ✅ SUCCESS! Video ID: {uploaded_id}")

                # Thumbnail Upload
                if os.path.exists(video_data['thumb_file']):
                    try:
                        youtube.thumbnails().set(
                            videoId=uploaded_id,
                            media_body=MediaFileUpload(video_data['thumb_file'])
                        ).execute()
                        print("      -> Thumbnail attached.")
                    except:
                        print("      -> Thumbnail upload failed.")
                
                return uploaded_id

            except HttpError as e:
                try:
                    err = json.loads(e.content.decode())['error']['errors'][0]
                    reason = err['reason']
                    msg = err['message']
                except:
                    reason = "unknown"; msg = str(e)
                
                print(f"   ❌ API Error: [{reason}] {msg}")
                if reason in ["quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"]:
                    continue # Try next account
                return None
            except Exception as e:
                print(f"   ❌ System Error: {e}")
                continue
        
        print("\n🚨 ALL ACCOUNTS FAILED.")
        return None

        # Load tokens from file or environment variable
        if os.path.exists(TOKENS_FILE):
            self.tokens = ConfigManager.load_json(TOKENS_FILE)
        elif os.environ.get("BOT_TOKENS"):
            try:
                self.tokens = json.loads(os.environ.get("BOT_TOKENS"))
            except:
                print("❌ Error parsing BOT_TOKENS environment variable.")
                self.tokens = {"accounts": []}
        else:
            print("⚠️ No tokens found (file or environment).")
            self.tokens = {"accounts": []}

    def run(self):
        print(f"🤖 BOT STARTED: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
        # 1. Update Queues
        scanner = Scanner(self.database['uploaded_videos'])
        for channel in self.config['source_channels']:
            if channel not in self.database['queues']:
                self.database['queues'][channel] = []
            
            new_videos = scanner.scan_channel(channel, self.database['queues'][channel])
            if new_videos:
                self.database['queues'][channel] = new_urls = new_videos + self.database['queues'][channel]
        
        ConfigManager.save_json(DATABASE_FILE, self.database)

        # 2. Select Video (Round Robin)
        channels = list(self.database['queues'].keys())
        if not channels:
            print("💤 No channels to scan.")
            return

        idx = self.database['state']['last_channel_index']
        total = len(channels)
        
        target_video = None
        target_channel = None
        new_idx = idx

        for i in range(total):
            curr_idx = (idx + 1 + i) % total
            curr_channel = channels[curr_idx]
            if self.database['queues'][curr_channel]:
                target_video = self.database['queues'][curr_channel][0]
                target_channel = curr_channel
                new_idx = curr_idx
                break
        
        if not target_video:
            print("💤 All queues are empty.")
            return

        # 3. Process Video
        print(f"\n🎯 SELECTED: {target_video} from {target_channel}")
        
        downloader = Downloader(COOKIES_FILE if os.path.exists(COOKIES_FILE) else None)
        video_data = downloader.download_video(target_video)
        
        if video_data:
            uploader = Uploader(self.tokens['accounts'], self.config['upload_settings'])
            uploaded_id = uploader.upload(video_data)
            
            if uploaded_id:
                # Cleanup and Save State
                self.database['queues'][target_channel].pop(0)
                self.database['uploaded_videos'].append(video_data['video_id'])
                self.database['state']['last_channel_index'] = new_idx
                ConfigManager.save_json(DATABASE_FILE, self.database)
                
                # Clean temp files
                for f in [video_data['video_file'], video_data['thumb_file']]:
                    if os.path.exists(f): os.remove(f)
                
                print("\n🎉 SUCCESSFULLY PROCESSED 1 VIDEO.")
            else:
                print("\n❌ Failed to upload. Video remains in queue.")
        else:
            print("\n❌ Failed to download. Video remains in queue.")

if __name__ == "__main__":
    bot = YouTubeBot(os.path.dirname(os.path.abspath(__file__)))
    bot.run()
