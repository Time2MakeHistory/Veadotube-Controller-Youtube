import json
import time
import re
from urllib.parse import urlparse, parse_qs
import requests
import keyboard
import pytchat

# ---------------------------
# Config Loading
# ---------------------------
def load_config():
    with open("config.json", "r", encoding="utf-8") as f:
        return json.load(f)

config = load_config()
last_used = {}

# ---------------------------
# YouTube Live Video Resolver
# ---------------------------
YOUTUBE_API_SEARCH = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_API_CHANNELS = "https://www.googleapis.com/youtube/v3/channels"

def normalize_handle(handle_or_url):
    """Return an @handle string if possible, else None."""
    h = handle_or_url.strip()
    if h.startswith("@"):
        return h
    # Try to parse URL forms to get @handle
    try:
        u = urlparse(h)
        if u.netloc and "youtube" in u.netloc:
            # Examples: https://www.youtube.com/@SomeHandle
            if u.path.startswith("/@"):
                return u.path.strip("/")
    except Exception:
        pass
    return None

def normalize_channel_id(cid_or_url):
    """Return a UC... channel ID if possible, else None."""
    cid = cid_or_url.strip()
    if cid.startswith("UC") and len(cid) >= 20:
        return cid
    # Parse URL forms like https://www.youtube.com/channel/UCxxxx
    try:
        u = urlparse(cid)
        if u.netloc and "youtube" in u.netloc:
            m = re.match(r"^/channel/(UC[a-zA-Z0-9_\-]{20,})/?$", u.path)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None

def resolve_live_video_id(cfg):
    """
    Resolve a live VIDEO_ID using (in priority order):
    1) Explicit video_id in config
    2) YouTube Data API (if api_key + channel_id/handle provided)
    3) Redirect trick via /live on channel URL or handle
    """
    # 1) Explicit video_id
    if cfg.get("video_id"):
        return cfg["video_id"]

    api_key = cfg.get("api_key", "").strip()

    # Prefer channel_id if present
    channel_id = None
    if cfg.get("channel_id"):
        channel_id = normalize_channel_id(cfg["channel_id"]) or cfg["channel_id"]

    # Or handle
    channel_handle = None
    if cfg.get("channel_handle"):
        channel_handle = normalize_handle(cfg["channel_handle"]) or cfg["channel_handle"]

    # Or channel_url (try to derive handle or channel_id)
    if cfg.get("channel_url"):
        # Try handle first
        maybe_handle = normalize_handle(cfg["channel_url"])
        if maybe_handle:
            channel_handle = maybe_handle
        else:
            maybe_cid = normalize_channel_id(cfg["channel_url"])
            if maybe_cid:
                channel_id = maybe_cid

    # 2) Use API if available
    if api_key:
        try:
            # If we only have a handle, resolve to channel_id
            if channel_handle and not channel_id:
                r = requests.get(
                    YOUTUBE_API_CHANNELS,
                    params={"part": "id", "forHandle": channel_handle, "key": api_key},
                    timeout=10,
                )
                r.raise_for_status()
                data = r.json()
                items = data.get("items", [])
                if items:
                    channel_id = items[0]["id"]

            if channel_id:
                r = requests.get(
                    YOUTUBE_API_SEARCH,
                    params={
                        "part": "id",
                        "channelId": channel_id,
                        "eventType": "live",
                        "type": "video",
                        "maxResults": 1,
                        "key": api_key,
                    },
                    timeout=10,
                )
                r.raise_for_status()
                data = r.json()
                items = data.get("items", [])
                if items:
                    return items[0]["id"]["videoId"]
        except Exception as e:
            print(f"[WARN] YouTube API lookup failed: {e}")

    # 3) Fallback: redirect trick via /live
    # Prefer handle if present; else channel_id; else raw channel_url
    fallback_targets = []
    if channel_handle:
        fallback_targets.append(f"https://www.youtube.com/{channel_handle}/live")
    if channel_id:
        fallback_targets.append(f"https://www.youtube.com/channel/{channel_id}/live")
    if cfg.get("channel_url"):
        # As-is
        u = cfg["channel_url"].rstrip("/")
        if not u.endswith("/live"):
            u = u + "/live"
        fallback_targets.append(u)

    for url in fallback_targets:
        try:
            resp = requests.get(url, allow_redirects=True, timeout=10)
            final_url = resp.url
            # Usually redirects to /watch?v=VIDEOID when live
            if "watch" in final_url and "v=" in final_url:
                q = parse_qs(urlparse(final_url).query)
                vid = q.get("v", [None])[0]
                if vid:
                    return vid
            # As a secondary attempt, scan the HTML for "watch?v="
            m = re.search(r"watch\?v=([a-zA-Z0-9_\-]{6,})", resp.text)
            if m:
                return m.group(1)
        except Exception as e:
            print(f"[WARN] Redirect lookup failed for {url}: {e}")

    raise ValueError(
        "Could not resolve a live VIDEO_ID. Provide 'video_id' directly, or set "
        "'api_key' with 'channel_id' or 'channel_handle', or a valid 'channel_url' that has /live."
    )

# Resolve video ID once on startup
VIDEO_ID = resolve_live_video_id(config)
print(f"[INFO] Resolved live VIDEO_ID: {VIDEO_ID}")

# ---------------------------
# Connect to chat
# ---------------------------
chat = pytchat.create(video_id=VIDEO_ID)

def is_trusted(user):
    return user.lower() in [u.lower() for u in config.get("trusted_users", [])]

def trigger_expression(expr_key):
    keyboard.press_and_release(config["expressions"][expr_key]["key"])
    print(f"Triggered {expr_key} → {config['expressions'][expr_key]['key']}")

def enable_expression(expr_key, user):
    if expr_key in config["expressions"]:
        config["expressions"][expr_key]["enabled"] = True
        print(f"{user} ENABLED expression: {expr_key}")
    else:
        print(f"{user} tried to enable unknown expression: {expr_key}")

def disable_expression(expr_key, user):
    if expr_key in config["expressions"]:
        config["expressions"][expr_key]["enabled"] = False
        print(f"{user} DISABLED expression: {expr_key}")
    else:
        print(f"{user} tried to disable unknown expression: {expr_key}")

# ---------------------------
# Main Loop
# ---------------------------
while chat.is_alive():
    for c in chat.get().sync_items():
        msg = c.message.strip().lower()
        user = c.author.name

        # Admin command: Refresh config (and optionally re-resolve live video if channel info changed)
        if msg == "!refreshconfig" and is_trusted(user):
            config = load_config()
            print(f"Config reloaded by {user}")
            # If stream changed, allow switching without restart
            try:
                new_vid = resolve_live_video_id(config)
                if new_vid != VIDEO_ID:
                    print(f"[INFO] New VIDEO_ID detected: {new_vid} (old was {VIDEO_ID}). Reconnecting...")
                    VIDEO_ID = new_vid
                    chat = pytchat.create(video_id=VIDEO_ID)
            except Exception as e:
                print(f"[WARN] After reload, could not resolve new VIDEO_ID: {e}")
            continue

        # Admin: Enable/disable expressions
        if is_trusted(user):
            if msg.startswith("!enable "):
                expr_key = msg.split("!enable ", 1)[1].strip()
                enable_expression(expr_key, user)
                continue
            elif msg.startswith("!disable "):
                expr_key = msg.split("!disable ", 1)[1].strip()
                disable_expression(expr_key, user)
                continue

        # Viewer-triggered expressions
        for expr_key, data in config["expressions"].items():
            command = f"!{data['command']}"
            if msg == command and data.get("enabled", True):
                now = time.time()
                cooldown = config.get("cooldown_seconds", 0)
                if expr_key not in last_used or now - last_used[expr_key] > cooldown:
                    trigger_expression(expr_key)
                    last_used[expr_key] = now
                else:
                    print(f"Cooldown active for {expr_key}")
