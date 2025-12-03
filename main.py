#!/usr/bin/env python3
"""
YouTube scraper (Apify-compatible)

This version includes:
- Resilient navigation and consent handling. 
- Per-type search collection (video/shorts/channel/playlist/live/movie) with per-type caps.
- Canonicalization of collected URLs and defensive filtering of malformed URLs.
- Channel scraping updated to collect both regular videos and shorts from channel pages (reel shelves).
- Forced navigation to canonical shorts URL before extracting shorts metadata to ensure DOM overlay is present.
- Playwright fallback uses player JSON when available to populate metadata fields when API key is not set.
- FIXES: Robust shorts metadata extraction (subscribers, date, duration), improved comment counting for search results. 
"""
import json
import os
import re
import logging
import signal
from typing import List, Dict, Any, Optional, Set
from urllib.parse import urlparse, parse_qs
import time

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError, BrowserContext, Page
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import requests

# Optional Apify dataset/KVS support
try:
    from apify_client import ApifyClient
    HAS_APIFY_CLIENT = True
except Exception:
    HAS_APIFY_CLIENT = False

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "") or os.environ.get("youtubeApiKey", "")
YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"

# Global flags / state
STOP_FLAG = False
SAVED_IDS = set()  # dedupe IDs per run


def _signal_handler(signum, frame):
    global STOP_FLAG
    logging.warning("Received signal %s, setting STOP_FLAG", signum)
    STOP_FLAG = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


# -----------------------
# YouTube API helpers
# -----------------------
def get_video_details_from_api(video_id: str) -> Optional[Dict[str, Any]]:
    if not YOUTUBE_API_KEY:
        logging.debug("No YouTube API key configured")
        return None
    try:
        url = f"{YOUTUBE_API_BASE}/videos"
        params = {"part": "snippet,statistics,contentDetails,status", "id": video_id, "key": YOUTUBE_API_KEY}
        resp = requests.get(url, params=params, timeout=15)
        if resp.status_code == 403:
            logging.error("YouTube API quota exceeded or invalid key")
            return None
        resp.raise_for_status()
        data = resp.json()
        if not data. get("items"):
            return None
        item = data["items"][0]
        snippet = item. get("snippet", {})
        statistics = item.get("statistics", {})
        content_details = item.get("contentDetails", {})
        duration_seconds = parse_iso8601_duration(content_details. get("duration", "") or "")
        return {
            "video_id": video_id,
            "title": snippet.get("title"),
            "description": snippet.get("description"),
            "channel_id": snippet.get("channelId"),
            "channel_title": snippet.get("channelTitle"),
            "published_at": snippet.get("publishedAt"),
            "thumbnails": snippet.get("thumbnails", {}),
            "view_count": int(statistics.get("viewCount")) if statistics. get("viewCount") else None,
            "like_count": int(statistics.get("likeCount")) if statistics.get("likeCount") else None,
            "comment_count": int(statistics.get("commentCount")) if statistics.get("commentCount") else None,
            "duration_seconds": duration_seconds,
            "tags": snippet.get("tags", []),
            "comments_disabled": not statistics.get("commentCount"),
        }
    except Exception as e:
        logging.debug("YouTube API error: %s", e)
        return None


def get_channel_details_from_api(channel_id: str) -> Optional[Dict[str, Any]]:
    if not YOUTUBE_API_KEY:
        return None
    try:
        url = f"{YOUTUBE_API_BASE}/channels"
        params = {"part": "snippet,statistics,brandingSettings", "id": channel_id, "key": YOUTUBE_API_KEY}
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("items"):
            return None
        item = data["items"][0]
        snippet = item.get("snippet", {})
        statistics = item.get("statistics", {})
        branding = item.get("brandingSettings", {}). get("channel", {})
        return {
            "channel_id": channel_id,
            "title": snippet.get("title"),
            "description": snippet.get("description"),
            "custom_url": snippet.get("customUrl"),
            "published_at": snippet.get("publishedAt"),
            "thumbnails": snippet.get("thumbnails", {}),
            "subscriber_count": int(statistics.get("subscriberCount")) if statistics. get("subscriberCount") else None,
            "keywords": branding.get("keywords", "")
        }
    except Exception as e:
        logging.debug("Channel API error: %s", e)
        return None


def parse_iso8601_duration(duration_str: str) -> Optional[int]:
    if not duration_str:
        return None
    try:
        s = duration_str.replace("PT", "")
        h = m = sec = 0
        if "H" in s:
            parts = s.split("H")
            h = int(parts[0])
            s = parts[1]
        if "M" in s:
            parts = s.split("M")
            m = int(parts[0])
            s = parts[1]
        if "S" in s:
            sec = int(s. replace("S", ""))
        return h * 3600 + m * 60 + sec
    except Exception:
        return None


# -----------------------
# Utilities
# -----------------------
def is_valid_channel_name(name: Optional[str]) -> bool:
    """Check if a channel name is valid (not 'Shopping', 'YouTube', or empty, and not just @handle)."""
    if not name:
        return False
    lower_name = name.lower(). strip()
    if lower_name. startswith('@'):
        return False
    return lower_name not in ['shopping', 'youtube', '']


def parse_count_text_to_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    try:
        t = str(text).lower(). strip(). replace(",", "")
        t = re.sub(r'[^\x00-\x7F]+', '', t)
        m = re.search(r'([\d\. ]+)\s*(k|m|b)? ', t)
        if not m:
            d = re.sub(r'[^0-9]', '', t)
            return int(d) if d else None
        num = float(m.group(1))
        suf = m.group(2)
        if suf == 'k':
            return int(num * 1_000)
        if suf == 'm':
            return int(num * 1_000_000)
        if suf == 'b':
            return int(num * 1_000_000_000)
        return int(num)
    except Exception:
        return None


def seconds_to_hms(seconds: Optional[int]) -> Optional[str]:
    if seconds is None:
        return None
    try:
        s = int(seconds)
        h = s // 3600
        m = (s % 3600) // 60
        sec = s % 60
        return f"{h:02d}:{m:02d}:{sec:02d}" if h > 0 else f"{m:02d}:{sec:02d}"
    except Exception:
        return None


def extract_hashtags_from_text(text: Optional[str]) -> List[str]:
    if not text:
        return []
    return re.findall(r"#(\w+)", text)


# -----------------------
# Storage helpers (dedupe)
# -----------------------
def save_dataset(items: List[Dict[str, Any]]):
    global SAVED_IDS
    if not items:
        return
    new_items = []
    for it in items:
        vid = it.get("id") or it.get("video_id")
        if not vid:
            new_items.append(it)
            continue
        if vid in SAVED_IDS:
            logging.debug("Skipping already-saved id=%s", vid)
            continue
        SAVED_IDS.add(vid)
        new_items.append(it)
    if not new_items:
        return
    ds_id = os.getenv("APIFY_DEFAULT_DATASET_ID")
    token = os.getenv("APIFY_TOKEN")
    api_base = os.getenv("APIFY_API_BASE_URL")
    if HAS_APIFY_CLIENT and ds_id and token:
        try:
            client = ApifyClient(token, api_url=api_base) if api_base else ApifyClient(token)
            dataset = client.dataset(ds_id)
            CHUNK = 100
            for i in range(0, len(new_items), CHUNK):
                dataset.push_items(new_items[i:i + CHUNK])
            logging. info("Pushed %d new items to dataset %s", len(new_items), ds_id)
            return
        except Exception as e:
            logging.warning("Apify client push failed, falling back to local file: %s", e)
    dataset_path = os.environ.get("APIFY_DATASET_PATH", "./dataset. ndjson")
    try:
        with open(dataset_path, "a", encoding="utf-8") as f:
            for it in new_items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        logging.info("Appended %d new items to %s", len(new_items), dataset_path)
    except Exception as e:
        logging.error("Failed to write local dataset: %s", e)


# -----------------------
# Navigation / availability
# -----------------------
def handle_consent_aggressive(page: Page) -> bool:
    selectors = [
        'button:has-text("Accept all")', 'button:has-text("I agree")', 'button:has-text("Agree")',
        'button[aria-label*="Accept"]', 'button[aria-label*="Agree"]', 'button#introAgreeButton'
    ]
    for sel in selectors:
        try:
            el = page.locator(sel). first
            if el and el.is_visible(timeout=1200):
                try:
                    el.click(timeout=3000)
                    page.wait_for_timeout(700)
                    logging.info("Clicked consent selector: %s", sel)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    try:
        for frame in page.frames:
            for sel in selectors:
                try:
                    el = frame. locator(sel).first
                    if el and el.is_visible(timeout=1000):
                        el.click(timeout=2000)
                        logging.info("Clicked consent in frame: %s", sel)
                        return True
                except Exception:
                    continue
    except Exception:
        pass
    return False


def is_page_unavailable(page: Page) -> bool:
    try:
        if page.locator("ytd-page-not-found-renderer").count() > 0:
            return True
        body_text = page.locator("body").inner_text(timeout=2000)
        if body_text:
            lowered = body_text.lower()
            if "this page isn't available" in lowered or "sorry about that" in lowered or "channel is not available" in lowered:
                return True
        content = page.content() or ""
        if "page not found" in content.lower() or "channel unavailable" in content.lower():
            return True
    except Exception:
        return False
    return False


@retry(wait=wait_exponential(multiplier=1, min=2, max=8), stop=stop_after_attempt(1), retry=retry_if_exception_type(Exception))
def goto_and_ready(page: Page, url: str):
    global STOP_FLAG
    logging.info("Navigating to: %s", url)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        logging.warning("page.goto failed for %s: %s", url, e)

    if STOP_FLAG:
        raise Exception("Stop requested")

    try:
        handle_consent_aggressive(page)
    except Exception:
        logging.debug("Consent handling failed")

    try:
        if is_page_unavailable(page):
            logging.warning("Detected YouTube unavailable page for %s", url)
            try_capture(page, key=f"CHANNEL_UNAVAILABLE_{int(time.time())}. png")
            return
    except Exception:
        pass

    try:
        page.wait_for_function(
            "() => Boolean(window.ytInitialPlayerResponse) || Boolean(window.ytInitialData)",
            timeout=20_000
        )
        logging.info("Found ytInitial* data on %s", url)
        return
    except Exception:
        logging.debug("ytInitial* wait timed out, trying fallbacks for %s", url)

    fallback_selectors = [
        "ytd-browse", "ytd-rich-grid-renderer", "ytd-rich-item-renderer", "#contents ytd-rich-item-renderer",
        "ytd-channel-name", "#items ytd-grid-video-renderer", "ytd-section-list-renderer", "ytd-reel-shelf-renderer"
    ]
    for sel in fallback_selectors:
        try:
            el = page. locator(sel).first
            if el and el.is_visible(timeout=3000):
                logging.info("Found fallback selector '%s' on %s", sel, url)
                return
        except Exception:
            continue

    try:
        page. wait_for_timeout(1000)
        ready = page.evaluate("() => document.readyState") or ""
        if ready. lower() in ("complete", "interactive"):
            has_init = page.evaluate("() => Boolean(window.ytInitialData) || Boolean(window.ytInitialPlayerResponse)")
            if has_init:
                logging.info("Document ready and ytInitial* present on %s", url)
                return
    except Exception:
        logging.debug("Document. ready fallback failed for %s", url)

    try:
        html = page.content() or ""
        if "/shorts/" in html or "watch? v=" in html or "ytd-rich-grid-renderer" in html:
            logging. info("Found keywords in HTML for %s; proceeding without ytInitial*", url)
            return
    except Exception:
        logging.debug("HTML scan failed for %s", url)

    try:
        if is_page_unavailable(page):
            logging.warning("Detected YouTube unavailable page for %s (post-fallback)", url)
            try_capture(page, key=f"CHANNEL_UNAVAILABLE_{int(time.time())}.png")
            return
    except Exception:
        pass

    try:
        try_capture(page, key=f"NO_INITDATA_{int(time.time())}.png")
    except Exception:
        logging.debug("Screenshot capture failed")
    logging.warning("Could not detect ytInitial* or fallback selectors on %s — continuing", url)
    return


# -----------------------
# YouTube helpers
# -----------------------
def clean_video_url(url: str) -> str:
    if not url:
        return url
    try:
        parsed = urlparse(url)
        path = parsed.path or ""
        m = re.search(r"/shorts/([A-Za-z0-9_-]{11})", path)
        if m:
            return f"https://www.youtube.com/shorts/{m.group(1)}"
        if parsed.netloc and "youtu.be" in parsed.netloc:
            vid = path.strip("/").split("/")[0]
            if vid:
                return f"https://www.youtube.com/watch?v={vid}"
        if "watch" in path and parsed.query:
            qs = parse_qs(parsed.query)
            if "v" in qs and qs["v"]:
                return f"https://www.youtube.com/watch? v={qs['v'][0]}"
        return url. split("#")[0]
    except Exception:
        return url. split("#")[0]


def extract_video_id(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        if parsed.netloc and "youtu.be" in parsed.netloc:
            return parsed.path. strip("/").split("/")[0]
        if "/watch" in parsed.path and parsed.query:
            qs = parse_qs(parsed. query)
            if "v" in qs:
                return qs["v"][0]
        m = re.search(r"/shorts/([A-Za-z0-9_-]{11})", parsed.path or "")
        if m:
            return m.group(1)
    except Exception:
        return None
    return None


def is_shorts_url(url: str) -> bool:
    return "/shorts/" in (url or "").lower()


def is_video_url(url: str) -> bool:
    u = (url or "").lower()
    return ("watch? v=" in u) or ("/shorts/" in u) or ("youtu.be/" in u)


def detect_content_type(url: str, page: Page = None) -> str:
    if is_shorts_url(url):
        return "short"
    if page:
        try:
            c = page.content()
            if 'ytd-reel-video-renderer' in c or 'shorts' in (page.url or ""):
                return "short"
        except Exception:
            pass
    return "video"


def try_get_initial_data(page: Page) -> Dict[str, Any]:
    try:
        return page.evaluate("() => window.ytInitialData || {}") or {}
    except Exception:
        return {}


def try_get_player_json(page: Page) -> Dict[str, Any]:
    for expr in [
        "window.ytInitialPlayerResponse || null",
        "window.ytplayer && window.ytplayer.config && window.ytplayer.config. args && JSON.parse(window.ytplayer.config.args.player_response) || null"
    ]:
        try:
            data = page.evaluate(f"() => {expr}")
            if data:
                return data
        except Exception:
            continue
    return {}


def _text_from(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj. strip() or None
    if isinstance(obj, dict):
        if "simpleText" in obj and isinstance(obj["simpleText"], str):
            return obj["simpleText"].strip() or None
        if "runs" in obj and isinstance(obj["runs"], list):
            return "".join(r.get("text", "") for r in obj["runs"] if "text" in r). strip() or None
    return str(obj). strip() if obj else None


def find_nested_key(data: Any, key: str) -> Any:
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for v in data.values():
            found = find_nested_key(v, key)
            if found is not None:
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_nested_key(item, key)
            if found is not None:
                return found
    return None


# -----------------------
# Subtitles extractor
# -----------------------
def extract_subtitles(page: Page) -> Optional[List[Dict[str, Any]]]:
    try:
        page.locator("#description-inline-expander").click(timeout=3000)
        page.wait_for_timeout(200)
        page.locator('#info-container > #menu button[aria-label="More actions"]').click(timeout=3000)
        page.wait_for_timeout(200)
        page.locator("ytd-menu-service-item-renderer a:has-text('Show transcript')").click(timeout=3000)
        page.wait_for_selector("ytd-transcript-segment-renderer", timeout=5000)
        segments = page.query_selector_all("ytd-transcript-segment-renderer")
        out = []
        for seg in segments:
            try:
                ts = seg.query_selector(". segment-timestamp"). inner_text()
                txt = seg.query_selector(".segment-text").inner_text()
                out.append({"timestamp": ts, "text": txt})
            except Exception:
                continue
        return out
    except Exception as e:
        logging.debug("Subtitles not available: %s", e)
        return None


# -----------------------
# Shorts metadata extractor - FIXED VERSION
# -----------------------
def extract_shorts_metadata(video_id: str, url: str, page: Page) -> Dict[str, Any]:
    """
    Extract metadata for a YouTube Short.  Uses API first if available, then falls back to Playwright extraction.
    """
    logging.info("Extracting metadata for SHORT: %s", video_id)
    
    # Try YouTube API first - most reliable source
    api_data = get_video_details_from_api(video_id)
    if api_data:
        thumbnails = api_data.get("thumbnails", {})
        thumbnail = thumbnails.get("maxres", {}).get("url") or thumbnails.get("high", {}).get("url") or f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg"
        channel_id = api_data.get("channel_id")
        
        channel_data = get_channel_details_from_api(channel_id) if channel_id else None
        subscriber_count = channel_data.get("subscriber_count") if channel_data else None
        
        return {
            "video_id": video_id,
            "title": api_data.get("title"),
            "description": api_data.get("description"),
            "video_view_count": api_data.get("view_count"),
            "upload_date_iso": api_data.get("published_at"),
            "duration_seconds": api_data.get("duration_seconds"),
            "duration_text": seconds_to_hms(api_data.get("duration_seconds")),
            "thumbnail_url": thumbnail,
            "like_count": api_data.get("like_count"),
            "comments_count": api_data.get("comment_count"),
            "comments_off": api_data.get("comments_disabled", False),
            "channel_id": channel_id,
            "channel_name": api_data.get("channel_title"),
            "channel_url": f"https://www.youtube.com/channel/{channel_id}" if channel_id else None,
            "subscriber_count": subscriber_count,
            "video_url": url,
            "hashtags": extract_hashtags_from_text(api_data.get("description")),
            "content_type": "short",
            "data_source": "youtube_api"
        }
    
    logging.debug("API data not available for short %s; using Playwright extraction", video_id)
    
    # Initialize all variables
    title = None
    view_count = None
    like_count = None
    channel_name = None
    channel_id = None
    description = None
    channel_username = None
    subscriber_count = None
    upload_date_iso = None
    comments_count = None
    duration_seconds = None
    
    # Fall back to Playwright extraction
    try:
        player_json = try_get_player_json(page)
        initial = try_get_initial_data(page)
        
        logging.debug("player_json keys: %s", list(player_json.keys()) if player_json else "None")
        logging.debug("initial keys: %s", list(initial.keys()) if initial else "None")
        
        # 1. Try player_json. videoDetails first (most reliable)
        if player_json and isinstance(player_json, dict):
            vd = player_json.get("videoDetails", {}) or {}
            if vd:
                logging.debug("videoDetails: title=%s, author=%s, channelId=%s, lengthSeconds=%s",
                            vd.get("title"), vd.get("author"), vd.get("channelId"), vd.get("lengthSeconds"))
                title = vd.get("title")
                description = vd.get("shortDescription") or vd.get("description")
                if vd.get("viewCount"):
                    vc = vd.get("viewCount")
                    view_count = int(vc) if str(vc).isdigit() else parse_count_text_to_int(vc)
                channel_name = vd.get("author")
                channel_id = vd.get("channelId")
                if vd.get("lengthSeconds"):
                    try:
                        duration_seconds = int(vd.get("lengthSeconds"))
                    except Exception:
                        pass
            
            # Extract date and more channel info from microformat
            microformat = player_json.get("microformat", {}). get("playerMicroformatRenderer", {}) or {}
            if microformat:
                logging.debug("microformat: publishDate=%s, ownerChannelName=%s",
                            microformat.get("publishDate"), microformat.get("ownerChannelName"))
                upload_date_iso = upload_date_iso or microformat. get("publishDate") or microformat.get("uploadDate")
                if not is_valid_channel_name(channel_name):
                    channel_name = microformat.get("ownerChannelName")
                external_channel_id = microformat.get("externalChannelId")
                if external_channel_id:
                    channel_id = channel_id or external_channel_id
                owner_profile_url = microformat.get("ownerProfileUrl")
                if owner_profile_url and '/@' in owner_profile_url:
                    match = re.search(r'/@([A-Za-z0-9_.-]+)', owner_profile_url)
                    if match:
                        channel_username = match.group(1)
        
        # 2. Try overlay data from ytInitialData
        if initial:
            overlay = find_nested_key(initial, "reelPlayerOverlayRenderer") or find_nested_key(initial, "shortsPlayerOverlayRenderer")
            if overlay:
                logging.debug("Found overlay renderer")
                title = title or _text_from(overlay. get("reelTitleText") or overlay.get("shortsTitleText"))
                view_count = view_count or parse_count_text_to_int(_text_from(find_nested_key(overlay, "viewCountText")))
                like_count = like_count or parse_count_text_to_int(_text_from(find_nested_key(overlay, "likeButton")))
                
                channel_name_from_overlay = _text_from(find_nested_key(overlay, "channelTitleText"))
                if channel_name_from_overlay and is_valid_channel_name(channel_name_from_overlay):
                    channel_name = channel_name or channel_name_from_overlay
                
                nav = find_nested_key(overlay, "channelNavigationEndpoint") or find_nested_key(overlay, "navigationEndpoint")
                if nav and isinstance(nav, dict):
                    browse_id = nav.get("browseEndpoint", {}).get("browseId")
                    if browse_id and browse_id.startswith("UC"):
                        channel_id = channel_id or browse_id
        
        # 3. Try structured data (ld+json) - comprehensive extraction
        try:
            ld_data = page.evaluate("""() => {
                const ldJsonScripts = document.querySelectorAll('script[type="application/ld+json"]');
                for (const script of ldJsonScripts) {
                    try {
                        const data = JSON.parse(script.textContent);
                        const items = Array.isArray(data) ? data : [data];
                        for (const item of items) {
                            if (item['@type'] === 'VideoObject') {
                                return {
                                    name: item.name || null,
                                    description: item.description || null,
                                    uploadDate: item.uploadDate || item.datePublished || null,
                                    duration: item.duration || null,
                                    author: item.author ?  (typeof item.author === 'string' ? item.author : item.author.name) : null,
                                    authorUrl: item.author && typeof item.author === 'object' ?  item.author.url : null,
                                    commentCount: item.commentCount || null
                                };
                            }
                        }
                    } catch(e) {}
                }
                return null;
            }""")
            if ld_data and isinstance(ld_data, dict):
                logging.debug("ld+json data: %s", ld_data)
                title = title or ld_data.get('name')
                description = description or ld_data.get('description')
                upload_date_iso = upload_date_iso or ld_data.get('uploadDate')
                if ld_data.get('duration') and not duration_seconds:
                    duration_seconds = parse_iso8601_duration(ld_data['duration'])
                author_name = ld_data.get('author')
                if author_name and is_valid_channel_name(author_name):
                    channel_name = channel_name or author_name
                if ld_data.get('authorUrl'):
                    author_url = ld_data['authorUrl']
                    if '/@' in author_url:
                        match = re.search(r'/@([A-Za-z0-9_.-]+)', author_url)
                        if match:
                            channel_username = channel_username or match.group(1)
                    if '/channel/' in author_url:
                        match = re.search(r'/channel/([A-Za-z0-9_-]+)', author_url)
                        if match:
                            channel_id = channel_id or match.group(1)
                if ld_data.get('commentCount'):
                    cc = ld_data['commentCount']
                    comments_count = comments_count or (int(cc) if str(cc). isdigit() else parse_count_text_to_int(str(cc)))
        except Exception as e:
            logging.debug("ld+json extraction failed: %s", e)
        
        # 4. DOM fallbacks for missing fields
        
        # Title from meta tags
        if not title:
            try:
                title = page.evaluate("""() => {
                    const ogTitle = document.querySelector('meta[property="og:title"]');
                    if (ogTitle) {
                        const content = ogTitle.getAttribute('content');
                        if (content && content !== 'YouTube' && ! content.match(/^YouTube\\s*$/)) return content;
                    }
                    const twitterTitle = document.querySelector('meta[name="twitter:title"]');
                    if (twitterTitle) {
                        const content = twitterTitle.getAttribute('content');
                        if (content && content !== 'YouTube') return content;
                    }
                    const metaTitle = document.querySelector('meta[name="title"]');
                    if (metaTitle) {
                        const content = metaTitle.getAttribute('content');
                        if (content && content !== 'YouTube') return content;
                    }
                    if (document.title && document.title !== 'YouTube') {
                        return document.title. replace(/ - YouTube$/, ''). replace(/#shorts/gi, ''). trim();
                    }
                    return null;
                }""")
                logging.debug("Title from meta: %s", title)
            except Exception:
                pass
        
        # Upload date from meta tags
        if not upload_date_iso:
            try:
                upload_date_iso = page.evaluate("""() => {
                    const uploadMeta = document.querySelector('meta[itemprop="uploadDate"]');
                    if (uploadMeta) {
                        const content = uploadMeta.getAttribute('content');
                        if (content) return content;
                    }
                    const datePub = document.querySelector('meta[itemprop="datePublished"]');
                    if (datePub) {
                        const content = datePub.getAttribute('content');
                        if (content) return content;
                    }
                    const releaseMeta = document.querySelector('meta[property="og:video:release_date"]');
                    if (releaseMeta) {
                        const content = releaseMeta.getAttribute('content');
                        if (content) return content;
                    }
                    return null;
                }""")
                logging.debug("Upload date from meta: %s", upload_date_iso)
            except Exception:
                pass
        
        # Duration from DOM
        if not duration_seconds:
            try:
                duration_result = page.evaluate("""() => {
                    const ldJsonScripts = document.querySelectorAll('script[type="application/ld+json"]');
                    for (const ldJson of ldJsonScripts) {
                        try {
                            const data = JSON.parse(ldJson.textContent);
                            const items = Array.isArray(data) ? data : [data];
                            for (const item of items) {
                                if (item. duration) return item.duration;
                            }
                        } catch(e) {}
                    }
                    const badge = document.querySelector('. ytp-time-duration');
                    if (badge) {
                        const text = badge.innerText || badge.textContent;
                        if (text) return text;
                    }
                    const durationMeta = document.querySelector('meta[itemprop="duration"]');
                    if (durationMeta) {
                        return durationMeta.getAttribute('content');
                    }
                    return null;
                }""")
                if duration_result:
                    if duration_result.startswith('PT'):
                        duration_seconds = parse_iso8601_duration(duration_result)
                    else:
                        parts = duration_result.split(':')
                        if len(parts) == 2:
                            duration_seconds = int(parts[0]) * 60 + int(parts[1])
                        elif len(parts) == 3:
                            duration_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                logging.debug("Duration from DOM: %s -> %s seconds", duration_result, duration_seconds)
            except Exception:
                pass
        
        # View count from DOM
        if not view_count:
            try:
                v = page.evaluate("""() => {
                    const factoids = document.querySelectorAll('factoid-renderer, ytd-factoid-renderer');
                    for (const f of factoids) {
                        const text = f.innerText || f.textContent || '';
                        if (/views? /i.test(text)) {
                            const match = text.match(/([\\d,\\.]+[KMB]?)/i);
                            if (match) return match[1];
                        }
                    }
                    const metaLine = document.querySelector('#metadata-line');
                    if (metaLine) {
                        const text = metaLine. innerText || metaLine.textContent || '';
                        const match = text.match(/([\\d,\\.]+[KMB]?)\\s*views?/i);
                        if (match) return match[0];
                    }
                    const viewEl = [... document.querySelectorAll('span,div')].find(n => {
                        const label = n.getAttribute && n.getAttribute('aria-label');
                        return label && /\\d/. test(label) && /views?/i.test(label);
                    });
                    if (viewEl) return viewEl.getAttribute('aria-label');
                    return null;
                }""")
                view_count = parse_count_text_to_int(v)
            except Exception:
                pass
        
        # Like count from DOM
        if not like_count:
            try:
                like_text = page.evaluate("""() => {
                    const buttons = document.querySelectorAll('button[aria-label]');
                    for (const btn of buttons) {
                        const label = btn.getAttribute('aria-label') || '';
                        if (/\\blike\\b/i.test(label) && !/dislike/i.test(label) && /\\d/.test(label)) {
                            const match = label. match(/([\\d,\\.]+[KMB]?)/i);
                            if (match) return match[1];
                        }
                    }
                    const toggleBtn = document.querySelector('ytd-toggle-button-renderer[is-icon-button] button');
                    if (toggleBtn) {
                        const label = toggleBtn.getAttribute('aria-label') || '';
                        if (/like/i.test(label) && /\\d/.test(label)) {
                            const match = label.match(/([\\d,\\.]+[KMB]?)/i);
                            if (match) return match[1];
                        }
                    }
                    return null;
                }""")
                like_count = parse_count_text_to_int(like_text)
            except Exception:
                pass
        
        # Channel info from DOM
        if not is_valid_channel_name(channel_name) or not channel_id:
            try:
                channel_info = page.evaluate("""() => {
                    const overlayLinks = document.querySelectorAll(
                        'ytd-reel-video-renderer a[href*="/@"], ' +
                        'ytd-reel-video-renderer a[href*="/channel/"], ' +
                        '. ytd-reel-player-overlay-renderer a[href*="/@"], ' +
                        '.ytd-reel-player-overlay-renderer a[href*="/channel/"]'
                    );
                    for (const link of overlayLinks) {
                        const text = (link. innerText || link.textContent || '').trim();
                        if (text && text.toLowerCase() !== 'shopping' && text.toLowerCase() !== 'youtube' && ! text.startsWith('@')) {
                            return {name: text, href: link.href};
                        }
                    }
                    
                    const channelNames = document.querySelectorAll('ytd-channel-name a, #channel-name a');
                    for (const link of channelNames) {
                        const text = (link.innerText || link.textContent || '').trim();
                        if (text && text.toLowerCase() !== 'shopping' && text.toLowerCase() !== 'youtube' && !text.startsWith('@')) {
                            return {name: text, href: link.href};
                        }
                    }
                    
                    const ldJsonScripts = document.querySelectorAll('script[type="application/ld+json"]');
                    for (const script of ldJsonScripts) {
                        try {
                            const data = JSON.parse(script. textContent);
                            const items = Array.isArray(data) ? data : [data];
                            for (const item of items) {
                                if (item. author) {
                                    const authorName = typeof item.author === 'string' ? item.author : item.author.name;
                                    const authorUrl = typeof item.author === 'object' ? item.author.url : null;
                                    if (authorName && ! authorName.startsWith('@')) {
                                        return {name: authorName, href: authorUrl};
                                    }
                                }
                            }
                        } catch(e) {}
                    }
                    
                    return null;
                }""")
                if channel_info and isinstance(channel_info, dict):
                    if is_valid_channel_name(channel_info.get('name')):
                        channel_name = channel_name or channel_info['name']
                    if channel_info.get('href'):
                        href = channel_info['href']
                        if '/channel/' in href:
                            match = re.search(r'/channel/([A-Za-z0-9_-]+)', href)
                            if match:
                                channel_id = channel_id or match.group(1)
                        if '/@' in href:
                            match = re.search(r'/@([A-Za-z0-9_.-]+)', href)
                            if match:
                                channel_username = channel_username or match.group(1)
                logging.debug("Channel from DOM: name=%s, id=%s, username=%s", channel_name, channel_id, channel_username)
            except Exception:
                pass
        
        # If we still have @handle as channel name, try to get the real name
        if channel_name and channel_name.startswith('@'):
            channel_username = channel_username or channel_name[1:]
            channel_name = None
        
        # Extract channel_username if we have channel links
        if not channel_username:
            try:
                channel_username = page.evaluate("""() => {
                    const handleLinks = document.querySelectorAll('a[href*="/@"]');
                    for (const link of handleLinks) {
                        const match = link.href.match(/@([A-Za-z0-9_.-]+)/);
                        if (match) return match[1];
                    }
                    return null;
                }""")
            except Exception:
                pass
        
        # Subscriber count from DOM
        if not subscriber_count:
            try:
                sub_text = page.evaluate("""() => {
                    const selectors = [
                        'ytd-reel-video-renderer #subscriber-count',
                        '#owner-sub-count',
                        '. ytd-reel-player-overlay-renderer #subscriber-count',
                        'yt-formatted-string#subscriber-count',
                        '#channel-container #subscriber-count'
                    ];
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const text = (el.innerText || el.textContent || '').trim();
                            if (text && /\\d/.test(text)) return text;
                        }
                    }
                    const ariaEls = document.querySelectorAll('[aria-label*="subscriber" i]');
                    for (const el of ariaEls) {
                        const label = el.getAttribute('aria-label') || '';
                        if (/\\d/.test(label)) return label;
                    }
                    const allSpans = document.querySelectorAll('span, yt-formatted-string');
                    for (const span of allSpans) {
                        const text = span.innerText || span.textContent || '';
                        if (/\\d/.test(text) && /subscribers?/i.test(text)) {
                            return text;
                        }
                    }
                    return null;
                }""")
                subscriber_count = parse_count_text_to_int(sub_text)
                logging.debug("Subscriber count from DOM: %s -> %s", sub_text, subscriber_count)
            except Exception:
                pass
        
        # Comments count from DOM
        if not comments_count:
            try:
                comments_text = page.evaluate("""() => {
                    const commentsBtn = document.querySelector('button[aria-label*="comment" i]');
                    if (commentsBtn) {
                        const label = commentsBtn.getAttribute('aria-label') || '';
                        const match = label.match(/([\\d,\\.]+[KMB]?)/i);
                        if (match) return match[1];
                    }
                    const ldJsonScripts = document.querySelectorAll('script[type="application/ld+json"]');
                    for (const ldJson of ldJsonScripts) {
                        try {
                            const data = JSON.parse(ldJson.textContent);
                            const items = Array.isArray(data) ? data : [data];
                            for (const item of items) {
                                if (item.commentCount !== undefined) return String(item.commentCount);
                            }
                        } catch(e) {}
                    }
                    return null;
                }""")
                comments_count = parse_count_text_to_int(comments_text)
            except Exception:
                pass
        
        # If channel_id is still missing, try to extract from any channel link
        if not channel_id:
            try:
                channel_id = page. evaluate("""() => {
                    const channelLinks = document. querySelectorAll('a[href*="/channel/UC"]');
                    for (const link of channelLinks) {
                        const match = link.href.match(/\\/channel\\/(UC[A-Za-z0-9_-]+)/);
                        if (match) return match[1];
                    }
                    if (window.ytInitialPlayerResponse && window.ytInitialPlayerResponse. videoDetails) {
                        return window.ytInitialPlayerResponse. videoDetails.channelId || null;
                    }
                    return null;
                }""")
                logging.debug("Channel ID from additional extraction: %s", channel_id)
            except Exception:
                pass
        
        # Build channel_url
        if channel_username:
            channel_url = f"https://www.youtube.com/@{channel_username}"
        elif channel_id:
            channel_url = f"https://www. youtube.com/channel/{channel_id}"
        else:
            channel_url = None
        
        hashtags = extract_hashtags_from_text(description)
        
        logging.info("Shorts extraction complete: title=%s, date=%s, channel=%s, channel_id=%s, subs=%s, duration=%s",
                    title[:50] if title else None, upload_date_iso, channel_name, channel_id, subscriber_count, duration_seconds)
        
        return {
            "video_id": video_id,
            "title": title,
            "description": description,
            "video_view_count": view_count,
            "upload_date_iso": upload_date_iso,
            "duration_seconds": duration_seconds,
            "duration_text": seconds_to_hms(duration_seconds),
            "thumbnail_url": f"https://i.ytimg. com/vi/{video_id}/maxresdefault.jpg",
            "like_count": like_count,
            "comments_count": comments_count,
            "comments_off": False,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "channel_username": channel_username,
            "subscriber_count": subscriber_count,
            "channel_url": channel_url,
            "video_url": url,
            "hashtags": hashtags,
            "content_type": "short",
            "data_source": "playwright_shorts"
        }
    except Exception as e:
        logging.error("Shorts extractor failed: %s", e, exc_info=True)
        return {"video_id": video_id, "video_url": url, "data_source": "error"}


# -----------------------
# Hybrid video metadata extractor
# -----------------------
def extract_video_metadata_hybrid(video_id: str, url: str, page: Page = None) -> Dict[str, Any]:
    api_data = get_video_details_from_api(video_id)
    if api_data:
        thumbnails = api_data.get("thumbnails", {})
        thumbnail = thumbnails.get("maxres", {}).get("url") or thumbnails.get("high", {}).get("url") or f"https://i.ytimg. com/vi/{video_id}/maxresdefault.jpg"
        return {
            "video_id": video_id,
            "title": api_data.get("title"),
            "description": api_data.get("description"),
            "video_view_count": api_data.get("view_count"),
            "upload_date_iso": api_data.get("published_at"),
            "duration_seconds": api_data.get("duration_seconds"),
            "duration_text": seconds_to_hms(api_data.get("duration_seconds")),
            "thumbnail_url": thumbnail,
            "like_count": api_data.get("like_count"),
            "comments_count": api_data.get("comment_count"),
            "comments_off": api_data.get("comments_disabled", False),
            "channel_id": api_data.get("channel_id"),
            "channel_name": api_data.get("channel_title"),
            "channel_url": f"https://www.youtube.com/channel/{api_data.get('channel_id')}" if api_data.get("channel_id") else None,
            "video_url": url,
            "hashtags": extract_hashtags_from_text(api_data.get("description")),
            "content_type": detect_content_type(url),
            "data_source": "youtube_api"
        }

    logging.warning("API data not used for %s; using Playwright", video_id)
    if not page:
        return {"video_id": video_id, "video_url": url, "data_source": "none"}

    if is_shorts_url(url):
        return extract_shorts_metadata(video_id, url, page)

    try:
        initial_data = try_get_initial_data(page)
        player_json = try_get_player_json(page)

        title = description = upload_date_iso = None
        duration_seconds = None
        channel_id = channel_name = None
        view_count = like_count = comments_count = None

        if player_json and isinstance(player_json, dict):
            vd = player_json.get("videoDetails", {}) or {}
            if vd:
                title = title or vd.get("title")
                description = description or vd.get("shortDescription") or vd.get("description")
                try:
                    duration_seconds = duration_seconds or (int(vd.get("lengthSeconds")) if vd.get("lengthSeconds") else None)
                except Exception:
                    duration_seconds = duration_seconds or None
                view_count = view_count or (int(vd.get("viewCount")) if vd.get("viewCount") and str(vd.get("viewCount")).isdigit() else parse_count_text_to_int(vd.get("viewCount")))
                channel_name = channel_name or vd. get("author")
                channel_id = channel_id or vd.get("channelId")

        primary_info = find_nested_key(initial_data, "videoPrimaryInfoRenderer")
        if primary_info:
            title = title or _text_from(primary_info.get("title"))
            upload_date_iso = upload_date_iso or _text_from(primary_info.get("dateText"))
            vc = find_nested_key(primary_info, "videoViewCountRenderer")
            if vc and "viewCount" in vc:
                view_count = view_count or parse_count_text_to_int(_text_from(vc.get("viewCount")))

        secondary_info = find_nested_key(initial_data, "videoSecondaryInfoRenderer")
        if secondary_info:
            description = description or _text_from(secondary_info.get("description"))
            like_button = find_nested_key(secondary_info, "likeButton")
            if like_button:
                lr = find_nested_key(like_button, "toggleButtonRenderer")
                if lr:
                    like_count = like_count or parse_count_text_to_int(_text_from(lr.get("defaultText")))

        owner_info = find_nested_key(initial_data, "videoOwnerRenderer")
        if owner_info:
            channel_name = channel_name or _text_from(owner_info.get("title"))
            channel_id_nav = find_nested_key(owner_info, "navigationEndpoint")
            if channel_id_nav:
                channel_id = channel_id or channel_id_nav.get("browseEndpoint", {}).get("browseId")

        if not upload_date_iso:
            try:
                date_text = page.evaluate("""() => {
                    const ldJsonScripts = document.querySelectorAll('script[type="application/ld+json"]');
                    for (const ldJson of ldJsonScripts) {
                        try {
                            const data = JSON.parse(ldJson. textContent);
                            if (data.uploadDate) return data.uploadDate;
                            if (data.datePublished) return data.datePublished;
                        } catch(e) {}
                    }
                    const infoStrings = document.querySelector('#info-strings yt-formatted-string');
                    if (infoStrings) return infoStrings.innerText || infoStrings.textContent;
                    const publishDate = document.querySelector('meta[itemprop="uploadDate"], meta[itemprop="datePublished"]');
                    if (publishDate) {
                        const content = publishDate.getAttribute('content');
                        if (content) return content;
                    }
                    const info = document.querySelector('#info-container #info, #info');
                    if (info) {
                        const text = info.innerText || info. textContent || '';
                        const dateMatch = text.match(/(\\w+\\s+\\d{1,2},\\s+\\d{4}|\\d{1,2}\\s+\\w+\\s+\\d{4})/);
                        if (dateMatch) return dateMatch[0];
                    }
                    return null;
                }""")
                upload_date_iso = date_text or upload_date_iso
            except Exception:
                pass

        if not channel_id:
            try:
                channel_id_from_href = page.evaluate("""() => {
                    const ldJsonScripts = document.querySelectorAll('script[type="application/ld+json"]');
                    for (const ldJson of ldJsonScripts) {
                        try {
                            const data = JSON.parse(ldJson. textContent);
                            if (data.author && data.author.url) {
                                const channelMatch = data.author.url.match(/\\/channel\\/([A-Za-z0-9_-]+)/);
                                if (channelMatch) return channelMatch[1];
                            }
                        } catch(e) {}
                    }
                    const channelLink = document.querySelector('a[href*="/channel/"]');
                    if (channelLink) {
                        const match = channelLink.href.match(/\\/channel\\/([A-Za-z0-9_-]+)/);
                        if (match) return match[1];
                    }
                    const ownerLink = document.querySelector('#owner a[href*="/channel/"], ytd-video-owner-renderer a[href*="/channel/"]');
                    if (ownerLink) {
                        const match = ownerLink.href.match(/\\/channel\\/([A-Za-z0-9_-]+)/);
                        if (match) return match[1];
                    }
                    return null;
                }""")
                channel_id = channel_id_from_href or channel_id
            except Exception:
                pass

        channel_username = None
        try:
            channel_username = page.evaluate("""() => {
                const handleLink = document.querySelector('#owner a[href*="/@"], ytd-video-owner-renderer a[href*="/@"], a. yt-simple-endpoint[href*="/@"]');
                if (handleLink) {
                    const match = handleLink.href.match(/@([A-Za-z0-9_.-]+)/);
                    if (match) return match[1];
                }
                const channelHandle = document.querySelector('#channel-handle, ytd-channel-name #text');
                if (channelHandle) {
                    const text = channelHandle.innerText || channelHandle.textContent || '';
                    const match = text.match(/@([A-Za-z0-9_.-]+)/);
                    if (match) return match[1];
                }
                return null;
            }""")
        except Exception:
            pass

        if not duration_seconds:
            try:
                duration_text_dom = page.evaluate("""() => {
                    const badge = document.querySelector('. ytp-time-duration');
                    if (badge) return badge.innerText || badge.textContent;
                    const ld = document.querySelector('script[type="application/ld+json"]');
                    if (ld) {
                        try {
                            const data = JSON.parse(ld.textContent);
                            if (data. duration) return data.duration;
                        } catch(e) {}
                    }
                    return null;
                }""")
                if duration_text_dom:
                    if duration_text_dom.startswith('PT'):
                        duration_seconds = parse_iso8601_duration(duration_text_dom)
                    else:
                        parts = duration_text_dom.split(':')
                        if len(parts) == 2:
                            duration_seconds = int(parts[0]) * 60 + int(parts[1])
                        elif len(parts) == 3:
                            duration_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            except Exception:
                pass

        if not like_count:
            try:
                like_text = page.evaluate("""() => {
                    const likeBtn = document.querySelector('ytd-menu-renderer button[aria-label*="like"], like-button-view-model button, ytd-toggle-button-renderer[is-icon-button] button[aria-label*="like"]');
                    if (likeBtn) {
                        const label = likeBtn.getAttribute('aria-label');
                        if (label) {
                            const match = label. match(/([\\d,\\. ]+[KMB]?)/i);
                            if (match) return match[1];
                        }
                    }
                    const segmented = document.querySelector('ytd-segmented-like-dislike-button-renderer');
                    if (segmented) {
                        const text = segmented. innerText || '';
                        const match = text. match(/([\\d,\\. ]+[KMB]?)/i);
                        if (match) return match[1];
                    }
                    const likeCount = document.querySelector('#segmented-like-button span, . YtLikeButtonViewModelHost span');
                    if (likeCount) return likeCount.innerText || likeCount.textContent;
                    return null;
                }""")
                like_count = parse_count_text_to_int(like_text)
            except Exception:
                pass

        subscriber_count = None
        try:
            sub_text = page.evaluate("""() => {
                const subCount = document.querySelector('#owner-sub-count, ytd-video-owner-renderer #owner-sub-count');
                if (subCount) return subCount.innerText || subCount.textContent;
                const headerSubs = document.querySelector('yt-formatted-string#subscriber-count');
                if (headerSubs) return headerSubs.innerText || headerSubs.textContent;
                return null;
            }""")
            subscriber_count = parse_count_text_to_int(sub_text)
        except Exception:
            pass

        if not comments_count:
            try:
                comments_header = find_nested_key(initial_data, "commentsEntryPointHeaderRenderer")
                if comments_header:
                    comment_count_text = _text_from(find_nested_key(comments_header, "commentCount"))
                    if comment_count_text:
                        comments_count = parse_count_text_to_int(comment_count_text)
                
                if not comments_count:
                    item_sections = find_nested_key(initial_data, "itemSectionRenderer")
                    if item_sections:
                        header = find_nested_key(item_sections, "commentsHeaderRenderer")
                        if header:
                            count_text = _text_from(find_nested_key(header, "countText"))
                            if count_text:
                                comments_count = parse_count_text_to_int(count_text)
            except Exception:
                pass
            
            if not comments_count:
                try:
                    comments_text = page.evaluate("""() => {
                        const commentsHeader = document.querySelector('#comments #count, ytd-comments-header-renderer #count, #count. ytd-comments-header-renderer');
                        if (commentsHeader) {
                            const text = commentsHeader.innerText || commentsHeader.textContent || '';
                            const match = text.match(/([\\d,\\.]+[KMB]? )/i);
                            if (match) return match[1];
                        }
                        const ldJsonScripts = document.querySelectorAll('script[type="application/ld+json"]');
                        for (const ldJson of ldJsonScripts) {
                            try {
                                const data = JSON.parse(ldJson. textContent);
                                const items = Array.isArray(data) ? data : [data];
                                for (const item of items) {
                                    if (item.commentCount !== undefined) return String(item.commentCount);
                                    if (item.interactionStatistic) {
                                        const stats = Array.isArray(item.interactionStatistic) ? item.interactionStatistic : [item.interactionStatistic];
                                        for (const stat of stats) {