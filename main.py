#!/usr/bin/env python3
"""
Lightweight YouTube scraper actor.

Features implemented:
- Search by keyword or process explicit video URLs.
- Collect both regular videos and Shorts.
- Extract channel/video metadata without the YouTube Data API.
- Save results to Apify dataset when available (falls back to local NDJSON).
"""
import json
import logging
import os
import re
import signal
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, quote_plus, urlparse

from playwright.sync_api import Page, TimeoutError as PWTimeoutError, sync_playwright

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(levelname)s %(message)s")

STOP_FLAG = False
SAVED_IDS = set()


# -----------------------
# Basic utilities
# -----------------------
def _signal_handler(signum, frame):
    global STOP_FLAG
    logging.warning("Received signal %s, setting STOP_FLAG", signum)
    STOP_FLAG = True


signal.signal(signal.SIGINT, _signal_handler)
signal.signal(signal.SIGTERM, _signal_handler)


def parse_iso8601_duration(duration_str: str) -> Optional[int]:
    if not duration_str:
        return None
    try:
        s = duration_str.replace("PT", "")
        hours = minutes = seconds = 0
        if "H" in s:
            parts = s.split("H")
            hours = int(parts[0])
            s = parts[1]
        if "M" in s:
            parts = s.split("M")
            minutes = int(parts[0])
            s = parts[1]
        if "S" in s:
            seconds = int(s.replace("S", ""))
        return hours * 3600 + minutes * 60 + seconds
    except Exception:
        return None


def parse_count_text_to_int(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    try:
        cleaned = str(text).lower().strip().replace(",", "")
        cleaned = re.sub(r"[^0-9kmb\. ]", "", cleaned)
        match = re.search(r"([\d\. ]+)(k|m|b)?", cleaned)
        if not match:
            digits = re.sub(r"[^0-9]", "", cleaned)
            return int(digits) if digits else None
        num = float(match.group(1))
        suffix = match.group(2)
        if suffix == "k":
            return int(num * 1_000)
        if suffix == "m":
            return int(num * 1_000_000)
        if suffix == "b":
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
        return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"
    except Exception:
        return None


def extract_hashtags_from_text(text: Optional[str]) -> List[str]:
    if not text:
        return []
    return re.findall(r"#(\w+)", text)


def find_nested_key(data: Any, key: str) -> Any:
    if isinstance(data, dict):
        if key in data:
            return data[key]
        for v in data.values():
            found = find_nested_key(v, key)
            if found is not None:
                return found
    if isinstance(data, list):
        for item in data:
            found = find_nested_key(item, key)
            if found is not None:
                return found
    return None


def _text_from(obj: Any) -> Optional[str]:
    if obj is None:
        return None
    if isinstance(obj, str):
        return obj.strip() or None
    if isinstance(obj, dict):
        if "simpleText" in obj and isinstance(obj["simpleText"], str):
            return obj["simpleText"].strip() or None
        if "runs" in obj and isinstance(obj["runs"], list):
            return "".join(run.get("text", "") for run in obj if isinstance(run, dict)).strip() or None
    return str(obj).strip() if obj else None


# -----------------------
# Navigation helpers
# -----------------------
def handle_consent_aggressive(page: Page) -> bool:
    selectors = [
        'button:has-text("Accept all")', 'button:has-text("I agree")', 'button:has-text("Agree")',
        'button[aria-label*="Accept"]', 'button[aria-label*="Agree"]', 'button#introAgreeButton'
    ]
    for sel in selectors:
        try:
            button = page.locator(sel).first
            if button and button.is_visible(timeout=1200):
                button.click(timeout=3000)
                page.wait_for_timeout(300)
                logging.info("Clicked consent selector: %s", sel)
                return True
        except Exception:
            continue
    return False


def goto_and_ready(page: Page, url: str):
    logging.info("Navigating to %s", url)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
    except Exception as e:
        logging.warning("goto failed for %s: %s", url, e)
    handle_consent_aggressive(page)
    try:
        page.wait_for_function("() => Boolean(window.ytInitialData) || Boolean(window.ytInitialPlayerResponse)", timeout=15_000)
    except Exception:
        logging.debug("ytInitial* wait timed out for %s", url)


# -----------------------
# Input / output helpers
# -----------------------
def load_input() -> Dict[str, Any]:
    env_path = os.getenv("APIFY_INPUT_PATH") or os.getenv("INPUT_PATH")
    default_paths = [env_path, "input.json", "input.local.json"]
    for path in default_paths:
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    logging.warning("No input file found, using defaults")
    return {}


def save_dataset(items: List[Dict[str, Any]]):
    global SAVED_IDS
    if not items:
        return
    new_items: List[Dict[str, Any]] = []
    for item in items:
        vid = item.get("id") or item.get("video_id")
        if vid and vid in SAVED_IDS:
            continue
        if vid:
            SAVED_IDS.add(vid)
        new_items.append(item)
    if not new_items:
        return

    ds_id = os.getenv("APIFY_DEFAULT_DATASET_ID")
    token = os.getenv("APIFY_TOKEN")
    api_base = os.getenv("APIFY_API_BASE_URL")
    if ds_id and token:
        try:
            from apify_client import ApifyClient

            client = ApifyClient(token, api_url=api_base) if api_base else ApifyClient(token)
            dataset = client.dataset(ds_id)
            dataset.push_items(new_items)
            logging.info("Pushed %d items to dataset %s", len(new_items), ds_id)
            return
        except Exception as e:
            logging.warning("Dataset push failed, writing locally: %s", e)

    dataset_path = os.environ.get("APIFY_DATASET_PATH", "./dataset.ndjson")
    with open(dataset_path, "a", encoding="utf-8") as f:
        for item in new_items:
            f.write(json.dumps(item, ensure_ascii=False) + "\n")
    logging.info("Appended %d items to %s", len(new_items), dataset_path)


# -----------------------
# Data extraction helpers
# -----------------------
def try_get_initial_data(page: Page) -> Dict[str, Any]:
    try:
        return page.evaluate("() => window.ytInitialData || {}") or {}
    except Exception:
        return {}


def try_get_player_json(page: Page) -> Dict[str, Any]:
    for expr in [
        "window.ytInitialPlayerResponse || null",
        "window.ytplayer && window.ytplayer.config && window.ytplayer.config.args && JSON.parse(window.ytplayer.config.args.player_response) || null",
    ]:
        try:
            data = page.evaluate(f"() => {expr}")
            if data:
                return data
        except Exception:
            continue
    return {}


def extract_video_id(url: str) -> Optional[str]:
    try:
        parsed = urlparse(url)
        if parsed.netloc and "youtu.be" in parsed.netloc:
            return parsed.path.strip("/").split("/")[0]
        if "watch" in parsed.path and parsed.query:
            qs = parse_qs(parsed.query)
            if "v" in qs and qs["v"]:
                return qs["v"][0]
        match = re.search(r"/shorts/([A-Za-z0-9_-]{11})", parsed.path or "")
        if match:
            return match.group(1)
    except Exception:
        return None
    return None


def is_shorts_url(url: str) -> bool:
    return "/shorts/" in (url or "").lower()


def extract_video_metadata_hybrid(video_id: str, url: str, page: Page) -> Dict[str, Any]:
    try:
        initial_data = try_get_initial_data(page)
        player_json = try_get_player_json(page)

        title = description = upload_date_iso = None
        duration_seconds = None
        channel_id = channel_name = channel_username = None
        view_count = like_count = comments_count = subscriber_count = None
        hashtags: List[str] = []

        if player_json and isinstance(player_json, dict):
            vd = player_json.get("videoDetails", {}) or {}
            if vd:
                title = vd.get("title") or title
                description = vd.get("shortDescription") or vd.get("description") or description
                try:
                    duration_seconds = duration_seconds or (int(vd.get("lengthSeconds")) if vd.get("lengthSeconds") else None)
                except Exception:
                    pass
                if vd.get("viewCount"):
                    view_count = parse_count_text_to_int(vd.get("viewCount"))
                channel_name = vd.get("author") or channel_name
                channel_id = vd.get("channelId") or channel_id
                keywords = vd.get("keywords") or []
                if isinstance(keywords, list):
                    hashtags.extend([k for k in keywords if isinstance(k, str)])

            micro = player_json.get("microformat", {}).get("playerMicroformatRenderer", {}) or {}
            if micro:
                upload_date_iso = upload_date_iso or micro.get("publishDate") or micro.get("uploadDate")
                channel_name = channel_name or micro.get("ownerChannelName")
                if not channel_id:
                    channel_id = micro.get("externalChannelId")
                owner_url = micro.get("ownerProfileUrl") or ""
                match = re.search(r"/@([A-Za-z0-9_.-]+)", owner_url)
                if match:
                    channel_username = match.group(1)

        primary_info = find_nested_key(initial_data, "videoPrimaryInfoRenderer")
        if primary_info:
            title = title or _text_from(primary_info.get("title"))
            upload_date_iso = upload_date_iso or _text_from(primary_info.get("dateText"))
            vc = find_nested_key(primary_info, "viewCount") or find_nested_key(primary_info, "videoViewCountRenderer")
            if isinstance(vc, dict):
                view_count = view_count or parse_count_text_to_int(_text_from(vc.get("simpleText") or vc))
            else:
                view_count = view_count or parse_count_text_to_int(_text_from(vc))

        owner_info = find_nested_key(initial_data, "videoOwnerRenderer")
        if owner_info:
            channel_name = channel_name or _text_from(owner_info.get("title"))
            nav = find_nested_key(owner_info, "navigationEndpoint") or {}
            channel_id = channel_id or nav.get("browseEndpoint", {}).get("browseId")
            if not channel_username:
                handle = _text_from(owner_info.get("subscriberButton", {}).get("channelHandle"))
                if handle and handle.startswith("@"):
                    channel_username = handle[1:]

        if not like_count:
            try:
                like_text = page.evaluate(
                    """() => {
                    const button = document.querySelector('ytd-toggle-button-renderer button[aria-label*="like" i], #segmented-like-button button');
                    if (button) {
                        const label = button.getAttribute('aria-label') || button.innerText || '';
                        const match = label.match(/([\d,. ]+[KMB]?)/i);
                        if (match) return match[1];
                    }
                    const segmented = document.querySelector('ytd-segmented-like-dislike-button-renderer');
                    if (segmented) return segmented.innerText || '';
                    return null;
                }"""
                )
                like_count = parse_count_text_to_int(like_text)
            except Exception:
                pass

        if not subscriber_count:
            try:
                sub_text = page.evaluate(
                    "() => { const el = document.querySelector('#owner-sub-count, yt-formatted-string#subscriber-count'); return el ? (el.innerText || el.textContent) : null; }"
                )
                subscriber_count = parse_count_text_to_int(sub_text)
            except Exception:
                pass

        if not comments_count:
            try:
                comments_text = page.evaluate(
                    """() => {
                    const header = document.querySelector('#comments #count, ytd-comments-header-renderer #count');
                    if (header) return header.innerText || header.textContent;
                    const btn = document.querySelector('button[aria-label*="comment" i]');
                    if (btn) return btn.getAttribute('aria-label');
                    return null;
                }"""
                )
                comments_count = parse_count_text_to_int(comments_text)
            except Exception:
                pass

        if not upload_date_iso:
            try:
                upload_date_iso = page.evaluate(
                    """
                    () => (document.querySelector('meta[itemprop="uploadDate"]') || document.querySelector('meta[itemprop="datePublished"]'))?.getAttribute('content') || null
                    """
                )
            except Exception:
                pass

        if duration_seconds is None:
            try:
                duration_text = page.evaluate(
                    """() => {
                    const meta = document.querySelector('meta[itemprop="duration"], meta[property="og:video:duration"]');
                    if (meta) return meta.getAttribute('content');
                    const badge = document.querySelector('.ytp-time-duration');
                    return badge ? (badge.innerText || badge.textContent) : null;
                }"""
                )
                if duration_text:
                    if str(duration_text).startswith("PT"):
                        duration_seconds = parse_iso8601_duration(duration_text)
                    elif ":" in str(duration_text):
                        parts = str(duration_text).split(":")
                        if len(parts) == 2:
                            duration_seconds = int(parts[0]) * 60 + int(parts[1])
                        elif len(parts) == 3:
                            duration_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            except Exception:
                pass

        if not title:
            try:
                title = page.title().replace("- YouTube", "").strip()
            except Exception:
                title = None

        if not hashtags:
            hashtags = extract_hashtags_from_text(description)

        channel_url = None
        if channel_username:
            channel_url = f"https://www.youtube.com/@{channel_username}"
        elif channel_id:
            channel_url = f"https://www.youtube.com/channel/{channel_id}"

        return {
            "id": video_id,
            "video_id": video_id,
            "video_url": url,
            "content_type": "short" if is_shorts_url(url) else "video",
            "title": title,
            "description": description,
            "upload_date_iso": upload_date_iso,
            "duration_seconds": duration_seconds,
            "duration_text": seconds_to_hms(duration_seconds),
            "video_view_count": view_count,
            "like_count": like_count,
            "comments_count": comments_count,
            "subscriber_count": subscriber_count,
            "channel_id": channel_id,
            "channel_name": channel_name,
            "channel_username": channel_username,
            "channel_url": channel_url,
            "hashtags": hashtags,
            "data_source": "playwright",
        }
    except Exception as e:
        logging.error("Failed to extract metadata for %s: %s", video_id, e, exc_info=True)
        return {"video_id": video_id, "video_url": url, "data_source": "error"}


# -----------------------
# Search helpers
# -----------------------
def collect_video_ids_from_search(page: Page, max_results: int) -> List[str]:
    try:
        ids = page.evaluate(
            """(max) => {
            const results = new Set();
            const traverse = (obj) => {
                if (!obj || typeof obj !== 'object') return;
                if (obj.videoId) results.add(obj.videoId);
                if (obj.watchEndpoint && obj.watchEndpoint.videoId) results.add(obj.watchEndpoint.videoId);
                Object.values(obj).forEach(traverse);
            };
            traverse(window.ytInitialData || {});
            return Array.from(results).slice(0, max);
        }""",
            max_results,
        )
        return [vid for vid in ids if isinstance(vid, str)]
    except Exception as e:
        logging.error("Failed to collect search IDs: %s", e)
        return []


def search_term(term: str, page: Page, max_results: int) -> List[str]:
    search_url = f"https://www.youtube.com/results?search_query={quote_plus(term)}"
    goto_and_ready(page, search_url)
    video_ids = collect_video_ids_from_search(page, max_results)
    return [f"https://www.youtube.com/watch?v={vid}" for vid in video_ids]


# -----------------------
# Runner
# -----------------------
def run():
    input_data = load_input()
    start_urls = [u.get("url") if isinstance(u, dict) else u for u in input_data.get("startUrls", [])]
    search_terms = input_data.get("searchTerms", []) or []
    max_results = int(input_data.get("maxResults", 10) or 10)

    targets: List[str] = []
    for url in start_urls:
        if url:
            targets.append(url)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()

        for term in search_terms:
            if STOP_FLAG:
                break
            term_targets = search_term(term, page, max_results)
            targets.extend(term_targets)

        results: List[Dict[str, Any]] = []
        for url in targets:
            if STOP_FLAG:
                break
            vid = extract_video_id(url)
            if not vid:
                logging.warning("Skipping non-video URL: %s", url)
                continue
            video_page = context.new_page()
            try:
                goto_and_ready(video_page, url)
                final_url = video_page.url or url
                metadata = extract_video_metadata_hybrid(vid, final_url, video_page)
                results.append(metadata)
            except PWTimeoutError:
                logging.error("Timeout while processing %s", url)
            except Exception as e:
                logging.error("Error processing %s: %s", url, e)
            finally:
                video_page.close()

        save_dataset(results)
        browser.close()


if __name__ == "__main__":
    run()
