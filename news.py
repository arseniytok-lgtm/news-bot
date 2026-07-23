#!/usr/bin/env python3
"""
Telegram News Bot
Бере новини напряму з RSS українських видань.
Публікує: картинка + заголовок + витяг. Без посилань.
"""

import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

TOKEN = os.environ.get("TG_BOT_TOKEN", "")
CHANNEL = os.environ.get("TG_CHANNEL_ID", "")

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
MEMORY_FILE = BASE / "memory.json"

BREAKING_MINUTES = 45
TITLE_LIMIT = 180
SUMMARY_LIMIT = 400
MEMORY_LIMIT = 3000

BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

NS = {
    "media": "http://search.yahoo.com/mrss/",
    "content": "http://purl.org/rss/1.0/modules/content/",
}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- конфіг і памʼять ----------

def read_config():
    if not CONFIG_FILE.exists():
        log("ПОМИЛКА: немає config.json")
        sys.exit(1)
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def read_memory():
    if not MEMORY_FILE.exists():
        return []
    try:
        with open(MEMORY_FILE, encoding="utf-8") as f:
            return json.load(f).get("published", [])
    except Exception:
        return []


def write_memory(items):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"published": items[-MEMORY_LIMIT:]}, f, ensure_ascii=False, indent=2)


# ---------- текст ----------

def strip_tags(raw):
    text = html.unescape(raw or "")
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def cut(text, limit):
    if len(text) <= limit:
        return text
    piece = text[:limit]
    for sep in (". ", "! ", "? "):
        pos = piece.rfind(sep)
        if pos > limit * 0.5:
            return piece[: pos + 1]
    return piece.rsplit(" ", 1)[0] + "…"


# ---------- читання стрічок ----------

def find_image(node, description_html):
    """Шукає картинку в різних форматах RSS."""
    for tag in ("media:content", "media:thumbnail"):
        found = node.find(tag, NS)
        if found is not None:
            url = found.get("url")
            if url and url.startswith("http"):
                return url

    enclosure = node.find("enclosure")
    if enclosure is not None:
        url = enclosure.get("url", "")
        mime = enclosure.get("type", "")
        if url.startswith("http") and ("image" in mime or not mime):
            return url

    if description_html:
        match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', description_html, re.I)
        if match:
            url = html.unescape(match.group(1))
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith("http"):
                return url

    encoded = node.find("content:encoded", NS)
    if encoded is not None and encoded.text:
        match = re.search(r'<img[^>]+src=["\']([^"\']+)["\']', encoded.text, re.I)
        if match:
            url = html.unescape(match.group(1))
            if url.startswith("//"):
                url = "https:" + url
            if url.startswith("http"):
                return url

    return None


def read_feed(url):
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": BROWSER})
        if resp.status_code != 200:
            log(f"{urlparse(url).netloc}: код {resp.status_code}")
            return []
        root = ElementTree.fromstring(resp.content)

        source_name = ""
        channel_title = root.find(".//channel/title")
        if channel_title is not None and channel_title.text:
            source_name = channel_title.text.strip()

        items = []
        for node in root.findall(".//item"):
            title_el = node.find("title")
            link_el = node.find("link")
            if title_el is None or not title_el.text:
                continue

            desc_el = node.find("description")
            desc_raw = desc_el.text if desc_el is not None else ""

            date_el = node.find("pubDate")

            items.append({
                "title": cut(strip_tags(title_el.text), TITLE_LIMIT),
                "summary": cut(strip_tags(desc_raw), SUMMARY_LIMIT),
                "link": (link_el.text or "").strip() if link_el is not None else "",
                "image": find_image(node, desc_raw or ""),
                "source": source_name,
                "pubdate": (date_el.text if date_el is not None else "") or "",
            })

        log(f"{urlparse(url).netloc}: {len(items)} новин")
        return items

    except Exception as exc:
        log(f"{urlparse(url).netloc}: помилка — {exc}")
        return []


# ---------- терміновість ----------

def minutes_old(pubdate):
    if not pubdate:
        return None
    try:
        published = parsedate_to_datetime(pubdate)
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - published).total_seconds() / 60
    except Exception:
        return None


def check_breaking(item, words):
    haystack = (item["title"] + " " + item["summary"]).lower()
    for word in words:
        if word.lower() in haystack:
            return True, f"слово '{word}'"
    age = minutes_old(item["pubdate"])
    if age is not None and age <= BREAKING_MINUTES:
        return True, f"свіжа, {int(age)} хв"
    return False, ""


# ---------- запасна картинка зі сторінки ----------

def page_image(page_url):
    if not page_url:
        return None
    try:
        resp = requests.get(page_url, timeout=15, headers={"User-Agent": BROWSER})
        if resp.status_code != 200:
            return None
        head = resp.text[:250000]
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
            r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]*content=["\']([^"\']+)["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, head, re.IGNORECASE)
            if match:
                image = html.unescape(match.group(1)).strip()
                if image.startswith("//"):
                    image = "https:" + image
                elif image.startswith("/"):
                    parts = urlparse(page_url)
                    image = f"{parts.scheme}://{parts.netloc}{image}"
                if image.startswith("http"):
                    return image
        return None
    except Exception:
        return None


# ---------- публікація ----------

def make_text(item, urgent):
    lines = []
    if urgent:
        lines.append("🔴 <b>ТЕРМІНОВО</b>")
        lines.append("")
    lines.append(f"<b>{html.escape(item['title'])}</b>")
    if item["summary"] and item["summary"].lower() != item["title"].lower():
        lines.append("")
        lines.append(html.escape(item["summary"]))
    if item["source"]:
        lines.append("")
        lines.append(f"<i>{html.escape(item['source'])}</i>")
    return "\n".join(lines)


def publish_photo(image_url, text):
    api = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
    body = {
        "chat_id": CHANNEL,
        "photo": image_url,
        "caption": text[:1024],
        "parse_mode": "HTML",
    }
    return requests.post(api, json=body, timeout=40).json()


def publish_text(text):
    api = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    body = {
        "chat_id": CHANNEL,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    return requests.post(api, json=body, timeout=30).json()


# ---------- головне ----------

def main():
    if not TOKEN or not CHANNEL:
        log("ПОМИЛКА: не задано TG_BOT_TOKEN або TG_CHANNEL_ID")
        sys.exit(1)

    config = read_config()
    feeds = config.get("feeds", [])
    urgent_words = config.get("breaking_words", [])

    if not feeds:
        log("ПОМИЛКА: у config.json немає feeds")
        sys.exit(1)

    published = read_memory()
    known = set(published)

    fresh = []
    for feed in feeds:
        for item in read_feed(feed):
            key = item["link"] or item["title"]
            if key and key not in known:
                item["key"] = key
                fresh.append(item)
                known.add(key)

    if not fresh:
        log("Нових новин немає")
        sys.exit(0)

    log(f"Нових новин у черзі: {len(fresh)}")

    chosen = None
    urgent = False
    for item in fresh:
        flag, reason = check_breaking(item, urgent_words)
        if flag:
            chosen, urgent = item, True
            log(f"ТЕРМІНОВА ({reason})")
            break

    if chosen is None:
        chosen = fresh[0]

    log(f"Публікуємо: {chosen['title'][:70]}")

    image = chosen["image"]
    if image:
        log(f"Картинка з RSS: {image[:80]}")
    else:
        image = page_image(chosen["link"])
        if image:
            log(f"Картинка зі сторінки: {image[:80]}")
        else:
            log("Картинки немає")

    text = make_text(chosen, urgent)

    if image:
        result = publish_photo(image, text)
        if not result.get("ok"):
            log(f"Фото не пройшло ({result.get('description')}), надсилаю текстом")
            result = publish_text(text)
    else:
        result = publish_text(text)

    if result.get("ok"):
        log("Опубліковано успішно")
        published.append(chosen["key"])
        write_memory(published)
    else:
        log(f"ПОМИЛКА Telegram: {result}")
        sys.exit(1)


if __name__ == "__main__":
    main()
