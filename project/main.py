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

# How many videos a single videos().list call accepts (YouTube API limit)
STATS_BATCH_SIZE = 50

def extract_video_id(url):
    """Pull the 11-char video id out of any YouTube URL shape we use."""
    m = re.search(r'(?:v=|/shorts/|youtu\.be/)([\w-]{11})', url or '')
    return m.group(1) if m else None

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
# POPULARITY SORT
# ==========================================
def fetch_video_stats(urls, account, stats_cache):
    """
    Look up view counts for every video URL using the YouTube Data API
    (videos().list with part=statistics, 50 IDs per call). Results are
    cached in stats_cache {video_id: view_count} so re-sorts across runs
    are cheap and don't burn quota on already-known videos.
    """
    to_fetch = []
    url_by_id = {}
    for url in urls:
        vid = extract_video_id(url)
        if not vid:
            continue
        url_by_id[vid] = url
        if str(stats_cache.get(vid)) not in (None, 'None'):
            continue
        to_fetch.append(vid)

    if not to_fetch or not account:
        return stats_cache

    youtube = get_auth_service(account)
    for i in range(0, len(to_fetch), STATS_BATCH_SIZE):
        batch = to_fetch[i:i + STATS_BATCH_SIZE]
        try:
            resp = youtube.videos().list(
                part='statistics', id=','.join(batch)
            ).execute()
            returned = set()
            for item in resp.get('items', []):
                vid = item['id']
                views = int(item.get('statistics', {}).get('viewCount', 0))
                stats_cache[vid] = views
                returned.add(vid)
            # Videos with no public stats (deleted/private) -> treat as 0
            for vid in batch:
                if vid not in returned:
                    stats_cache[vid] = 0
        except HttpError as e:
            print(f"   ⚠️ Stats lookup failed (batch {i//STATS_BATCH_SIZE}): {e}")
            break
        except Exception as e:
            print(f"   ⚠️ Stats lookup error: {e}")
            break
    return stats_cache

def sort_queue_by_popularity(urls, stats_cache):
    """Sort URLs highest-views-first. Missing stats sort last (stable)."""
    def key(url):
        vid = extract_video_id(url)
        return stats_cache.get(vid, -1)
    return sorted(urls, key=key, reverse=True)

# ==========================================
# 1. SCANNER (Uses Unique Keys)
# ==========================================
def rescan_channel(channel_url, queue_key, uploaded_ids, queue, rebuild=False):
    """
    Fetch the channel's video/shorts list with yt-dlp and return a queue list.
    - rebuild=False (incremental): append newly discovered, non-uploaded videos
      to the existing queue, preserving order (oldest-first).
    - rebuild=True: discard the existing queue and rebuild it fresh from the
      channel, still excluding any video already in uploaded_ids.

    Never touches uploaded_ids (read-only). Returns the new queue list.
    """
    base_url = channel_url.rstrip("/")
    is_shorts = queue_key.endswith("#short")
    scan_url = f"{base_url}/shorts" if is_shorts else f"{base_url}/videos"

    # Cookies MUST be on the opts dict before YoutubeDL() is constructed,
    # otherwise they're silently ignored (this is what caused missed videos).
    # - 'youtube' args apply to single-video extraction (download + stats)
    # - 'youtubetab' args apply to channel tab scanning (/videos, /shorts).
    #   skip=authcheck prevents the "Playlists that require authentication"
    #   error that aborts scanning some channels (e.g. @KDrama-Nest) with a 404.
    ydl_opts = {
        'extract_flat': True,
        'quiet': True,
        'extractor_args': {
            'youtube': {'player_client': ['ios', 'android', 'tv']},
            'youtubetab': {'skip': 'authcheck'},
        }
    }
    if os.path.exists(COOKIES_FILE):
        ydl_opts['cookiefile'] = COOKIES_FILE

    new_queue = [] if rebuild else list(queue)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(scan_url, download=False)
            if 'entries' not in info:
                return new_queue

            entries = list(info['entries'])
            entries.reverse()  # Oldest to Newest
            new_count = 0
            for e in entries:
                video_id = e.get('id')
                if not video_id:
                    continue
                video_url = f"https://www.youtube.com/watch?v={video_id}"

                # Exclude anything already uploaded — both in rebuild and
                # incremental mode (belt-and-suspenders prune of stale entries).
                if video_id in uploaded_ids:
                    continue
                if video_url not in new_queue:
                    new_queue.append(video_url)
                    new_count += 1
            print(f"   -> {'Rebuilt' if rebuild else 'Found'} with {new_count} {'fresh' if rebuild else 'new'} videos.")
    except Exception as e:
        print(f"   ❌ Scan failed: {e}")

    return new_queue

def update_channel_queues(channel_urls, uploaded_ids, all_queues, is_shorts=False, rebuild=False):
    mode_suffix = "#short" if is_shorts else "#long"

    for channel_url in channel_urls:
        queue_key = f"{channel_url}{mode_suffix}"
        action = "🔄 REBUILDING" if rebuild else "🔍 SCANNING"
        print(f"{action}: {queue_key}")

        existing = all_queues.get(queue_key, [])
        all_queues[queue_key] = rescan_channel(
            channel_url, queue_key, uploaded_ids, existing, rebuild=rebuild)

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

    config = load_json(CONFIG_FILE)
    database = load_json(DATABASE_FILE, {"uploaded_videos": [], "queues": {}, "state": {}})

    tokens = {"accounts": []}
    if os.path.exists(TOKENS_FILE): tokens = load_json(TOKENS_FILE)
    elif os.environ.get("BOT_TOKENS"): tokens = json.loads(os.environ.get("BOT_TOKENS"))

    # Resolve sort order: CLI override > config > default ("date")
    default_sort = config.get('upload_settings', {}).get('sort_by', 'date')
    parser.add_argument('--sort', choices=['date', 'popularity'], default=default_sort)
    parser.add_argument('--rebuild', action='store_true',
                        help="Wipe each queue for this mode and re-scrape every channel fresh (still excludes uploaded videos) before sorting/uploading")
    parser.add_argument('--dry-run', action='store_true',
                        help="Scan + sort the queues, print the upload order, then exit without downloading/uploading")
    args = parser.parse_args()

    mode = args.mode
    channel_list = config.get(f'{mode}_channels', [])
    is_shorts = (mode == 'short')
    sort_by = args.sort
    rebuild = args.rebuild

    print(f"⚙️  Mode: {mode.upper()} | Sort: {sort_by} | Rebuild: {rebuild}")

    # 1. Update active mode queue (full rebuild from channels if --rebuild)
    database['queues'] = update_channel_queues(
        channel_list, database['uploaded_videos'], database['queues'],
        is_shorts=is_shorts, rebuild=rebuild)

    # 1b. Re-sort queues by popularity if requested (date order left untouched)
    if sort_by == 'popularity':
        # Persist view counts so subsequent runs don't re-query the same videos
        stats_cache = database.setdefault('video_stats', {})
        primary_account = (tokens.get('accounts') or [None])[0]
        all_urls = []
        for ch in channel_list:
            queue_key = f"{ch}{'#short' if is_shorts else '#long'}"
            all_urls.extend(database['queues'].get(queue_key, []))
        if all_urls:
            print(f"📊 Fetching view counts for {len(all_urls)} queued videos...")
            known_before = len(stats_cache)
            fetch_video_stats(all_urls, primary_account, stats_cache)
            print(f"   ✅ View counts available for {len(stats_cache)}/{len(all_urls)} videos"
                  f" (+{len(stats_cache) - known_before} new lookups).")
            for ch in channel_list:
                queue_key = f"{ch}{'#short' if is_shorts else '#long'}"
                if database['queues'].get(queue_key):
                    before = database['queues'][queue_key][0]
                    database['queues'][queue_key] = sort_queue_by_popularity(
                        database['queues'][queue_key], stats_cache)
                    after = database['queues'][queue_key][0]
                    mark = " (changed)" if before != after else ""
                    print(f"   ↕️  Sorted {queue_key}: {len(database['queues'][queue_key])} videos{mark}")

    save_json(DATABASE_FILE, database)

    # 1c. Dry-run: show what *would* upload next, then stop
    if args.dry_run:
        print("\n📋 Dry run — next upload order per channel:")
        for ch in channel_list:
            queue_key = f"{ch}{'#short' if is_shorts else '#long'}"
            q = database['queues'].get(queue_key, [])
            print(f"\n  {queue_key}  ({len(q)} queued)")
            for url in q[:5]:
                vid = extract_video_id(url)
                views = database.get('video_stats', {}).get(vid, 'n/a')
                print(f"     • {url}   views={views}")
            if len(q) > 5:
                print(f"     … and {len(q)-5} more")
        print("\n🚫 Dry run complete — nothing was uploaded.")
        sys.exit(0)

    # 2. Process active mode video
    success = process_track(mode, channel_list, database, tokens, config['upload_settings'])

    if success:
        save_json(DATABASE_FILE, database)
        print(f"🏁 {mode.upper()} Finished.")
