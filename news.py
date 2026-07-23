#!/usr/bin/env python3
"""
Telegram News Bot
Публікує ОДНУ свіжу новину з картинкою.
Термінові новини йдуть першими з позначкою.
"""

import html
import json
import os
import re
import sys
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

import requests

TOKEN = os.environ.get("TG_BOT_TOKEN", "")
CHANNEL = os.environ.get("TG_CHANNEL_ID", "")

BASE = Path(__file__).parent
CONFIG_FILE = BASE / "config.json"
MEMORY_FILE = BASE / "memory.json"

BREAKING_MINUTES = 45
TITLE_LIMIT = 200
MEMORY_LIMIT = 2000

BROWSER = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------- конфіг і пам'ять ----------

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


def write_memory(links):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump({"published": links[-MEMORY_LIMIT:]}, f, ensure_ascii=False, indent=2)


# ---------- новини ----------

def tidy_title(raw):
    text = html.unescape(raw or "")
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+-\s+[^-]+$", "", text).strip()
    if len(text) > TITLE_LIMIT:
        text = text[:TITLE_LIMIT].rsplit(" ", 1)[0] + "…"
    return text


def search_news(query):
    url = (
        "https://news.google.com/rss/search?"
        f"q={quote(query)}&hl=uk&gl=UA&ceid=UA:uk"
    )
    try:
        resp = requests.get(url, timeout=20, headers={"User-Agent": BROWSER})
        resp.raise_for_status()
        root = ElementTree.fromstring(resp.content)
        found = []
        for node in root.findall(".//item"):
            title = node.find("title")
            link = node.find("link")
            source = node.find("source")
            pubdate = node.find("pubDate")
            if title is None or link is None:
                continue
            found.append({
                "title": tidy_title(title.text),
                "link": (link.text or "").strip(),
                "source": (source.text if source is not None else "") or "",
                "pubdate": (pubdate.text if pubdate is not None else "") or "",
            })
        log(f"'{query}' — знайдено {len(found)}")
        return found
    except Exception as exc:
        log(f"Помилка пошуку '{query}': {exc}")
        return []


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
    lowered = item["title"].lower()
    for word in words:
        if word.lower() in lowered:
            return True, f"слово '{word}'"
    age = minutes_old(item["pubdate"])
    if age is not None and age <= BREAKING_MINUTES:
        return True, f"свіжа, {int(age)} хв"
    return False, ""


# ---------- посилання і картинка ----------

def unwrap_link(google_url):
    """Розгортає Google News у справжню адресу статті."""
    try:
        resp = requests.get(
            google_url, timeout=15, headers={"User-Agent": BROWSER}, allow_redirects=True
        )
        final = resp.url
        if "news.google.com" in final:
            match = re.search(
                r'<a[^>]+href="(https?://(?!news\.google|www\.google)[^"]+)"', resp.text
            )
            if match:
                final = html.unescape(match.group(1))
            else:
                match = re.search(r'data-n-au="(https?://[^"]+)"', resp.text)
                if match:
                    final = html.unescape(match.group(1))
        return final
    except Exception as exc:
        log(f"Не вдалось розгорнути посилання: {exc}")
        return google_url


def grab_image(page_url):
    """Дістає превʼю-картинку зі сторінки статті."""
    if "news.google.com" in page_url:
        log("Посилання не розгорнулось, картинки не буде")
        return None
    try:
        resp = requests.get(page_url, timeout=15, headers={"User-Agent": BROWSER})
        if resp.status_code != 200:
            log(f"Сторінка віддала {resp.status_code}")
            return None
        head = resp.text[:250000]
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]*content=["\']([^"\']+)["\']',
            r'<meta[^>]*content=["\']([^"\']+)["\'][^>]*property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]*content=["\']([^"\']+)["\']',
            r'<meta[^>]+property=["\']og:image:url["\'][^>]*content=["\']([^"\']+)["\']',
        ]
        for pattern in patterns:
            match = re.search(pattern, head, re.IGNORECASE)
            if not match:
                continue
            image = html.unescape(match.group(1)).strip()
            if image.startswith("//"):
                image = "https:" + image
            elif image.startswith("/"):
                parts = urlparse(page_url)
                image = f"{parts.scheme}://{parts.netloc}{image}"
            if image.startswith("http"):
                return image
        log("og:image на сторінці немає")
        return None
    except Exception as exc:
        log(f"Не вдалось отримати картинку: {exc}")
        return None


# ---------- публікація ----------

def make_text(item, link, urgent):
    lines = []
    if urgent:
        lines.append("🔴 <b>ТЕРМІНОВО</b>")
        lines.append("")
    lines.append(f"<b>{html.escape(item['title'])}</b>")
    lines.append("")
    if item["source"]:
        lines.append(f"Джерело: {html.escape(item['source'])}")
    lines.append(f'<a href="{link}">Читати повністю</a>')
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
        "disable_web_page_preview": False,
    }
    return requests.post(api, json=body, timeout=30).json()


# ---------- головне ----------

def main():
    if not TOKEN or not CHANNEL:
        log("ПОМИЛКА: не задано TG_BOT_TOKEN або TG_CHANNEL_ID")
        sys.exit(1)

    config = read_config()
    queries = config.get("keywords", [])
    urgent_words = config.get("breaking_words", [])

    if not queries:
        log("ПОМИЛКА: у config.json немає keywords")
        sys.exit(1)

    published = read_memory()
    known = set(published)

    fresh = []
    for query in queries:
        for item in search_news(query):
            if item["link"] and item["link"] not in known:
                fresh.append(item)
                known.add(item["link"])

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

    article_url = unwrap_link(chosen["link"])
    log(f"Стаття: {article_url[:90]}")

    image = grab_image(article_url)
    text = make_text(chosen, article_url, urgent)

    if image:
        log(f"Картинка знайдена: {image[:80]}")
        result = publish_photo(image, text)
        if not result.get("ok"):
            log(f"Фото не пройшло ({result.get('description')}), надсилаю текстом")
            result = publish_text(text)
    else:
        result = publish_text(text)

    if result.get("ok"):
        log("Опубліковано успішно")
        published.append(chosen["link"])
        write_memory(published)
    else:
        log(f"ПОМИЛКА Telegram: {result}")
        sys.exit(1)


if __name__ == "__main__":
    main()
