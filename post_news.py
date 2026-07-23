#!/usr/bin/env python3
"""
News Poster
Публікує одну свіжу новину з картинкою в Telegram-канал.
Джерело: Google News RSS.
"""

import json
import os
import sys
import re
import html
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, urlparse
from xml.etree import ElementTree

import requests

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("TG_CHANNEL_ID", "")

BASE = Path(__file__).parent
SOURCES_FILE = BASE / "sources.json"
SEEN_FILE = BASE / "seen_news.json"

MAX_TITLE_LEN = 200

# Новина вважається терміновою, якщо вийшла менш ніж N хвилин тому
BREAKING_MINUTES = 45

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_sources():
    if not SOURCES_FILE.exists():
        log(f"ПОМИЛКА: немає {SOURCES_FILE}")
        return {}
    with open(SOURCES_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_seen():
    if not SEEN_FILE.exists():
        return []
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            return json.load(f).get("seen", [])
    except Exception:
        return []


def save_seen(seen):
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen": seen[-2000:]}, f, ensure_ascii=False, indent=2)


def clean_title(title):
    title = html.unescape(title or "")
    title = re.sub(r"<[^>]+>", "", title)
    title = re.sub(r"\s+-\s+[^-]+$", "", title).strip()
    if len(title) > MAX_TITLE_LEN:
        title = title[:MAX_TITLE_LEN].rsplit(" ", 1)[0] + "…"
    return title


def fetch_news(query, lang="uk", country="UA"):
    url = (
        f"https://news.google.com/rss/search?"
        f"q={quote(query)}&hl={lang}&gl={country}&ceid={country}:{lang}"
    )
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": UA})
        r.raise_for_status()
        root = ElementTree.fromstring(r.content)
        items = []
        for item in root.findall(".//item"):
            t = item.find("title")
            l = item.find("link")
            s = item.find("source")
            d = item.find("pubDate")
            if t is None or l is None:
                continue
            items.append({
                "title": clean_title(t.text),
                "link": (l.text or "").strip(),
                "source": (s.text if s is not None else "") or "",
                "pubdate": (d.text if d is not None else "") or "",
            })
        log(f"'{query}': знайдено {len(items)}")
        return items
    except Exception as e:
        log(f"Помилка запиту '{query}': {e}")
        return []


def age_minutes(pubdate_str):
    """Скільки хвилин тому вийшла новина. None якщо невідомо."""
    if not pubdate_str:
        return None
    try:
        dt = parsedate_to_datetime(pubdate_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return delta.total_seconds() / 60
    except Exception:
        return None


def is_breaking(item, breaking_words):
    """Чи термінова новина: за словами в заголовку або за свіжістю."""
    title_low = item["title"].lower()
    for w in breaking_words:
        if w.lower() in title_low:
            return True, f"слово '{w}'"
    age = age_minutes(item.get("pubdate", ""))
    if age is not None and age <= BREAKING_MINUTES:
        return True, f"свіжа ({int(age)} хв)"
    return False, ""


def resolve_url(google_link):
    """Розгортає посилання Google News у справжню адресу статті."""
    try:
        r = requests.get(google_link, timeout=15, headers={"User-Agent": UA}, allow_redirects=True)
        final = r.url
        if "news.google.com" in final:
            # інколи Google віддає HTML із редіректом усередині
            m = re.search(r'<a[^>]+href="(https?://(?!news\.google)[^"]+)"', r.text)
            if m:
                final = html.unescape(m.group(1))
            else:
                m = re.search(r'url=(https?://[^"&]+)', r.text)
                if m:
                    final = html.unescape(m.group(1))
        return final
    except Exception as e:
        log(f"Не вдалось розгорнути посилання: {e}")
        return google_link


def fetch_image(article_url):
    """Дістає картинку зі сторінки статті (og:image)."""
    if "news.google.com" in article_url:
        return None
    try:
        r = requests.get(article_url, timeout=15, headers={"User-Agent": UA})
        if r.status_code != 200:
            return None
        head = r.text[:200000]
        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        ]
        for p in patterns:
            m = re.search(p, head, re.IGNORECASE)
            if m:
                img = html.unescape(m.group(1)).strip()
                if img.startswith("//"):
                    img = "https:" + img
                elif img.startswith("/"):
                    parsed = urlparse(article_url)
                    img = f"{parsed.scheme}://{parsed.netloc}{img}"
                if img.startswith("http"):
                    return img
        return None
    except Exception as e:
        log(f"Картинку не знайдено: {e}")
        return None


def build_caption(item, link, breaking=False):
    parts = []
    if breaking:
        parts.append("🔴 <b>ТЕРМІНОВО</b>")
        parts.append("")
    parts.append(f"<b>{html.escape(item['title'])}</b>")
    if item["source"]:
        parts.append(f"<i>{html.escape(item['source'])}</i>")
    parts.append("")
    parts.append(f'<a href="{link}">Читати повністю</a>')
    return "\n".join(parts)


def send_photo(photo_url, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": CHANNEL_ID,
        "photo": photo_url,
        "caption": caption[:1024],
        "parse_mode": "HTML",
    }
    r = requests.post(url, json=payload, timeout=40)
    return r.json()


def send_text(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    r = requests.post(url, json=payload, timeout=30)
    return r.json()


def main():
    if not BOT_TOKEN or not CHANNEL_ID:
        log("ПОМИЛКА: не задано TG_BOT_TOKEN або TG_CHANNEL_ID")
        sys.exit(1)

    cfg = load_sources()
    queries = cfg.get("keywords", [])
    if not queries:
        log("Немає ключових слів у sources.json")
        sys.exit(1)

    breaking_words = cfg.get("breaking_words", [])

    seen = load_seen()
    seen_set = set(seen)

    # Збираємо всі свіжі новини з усіх запитів
    candidates = []
    for q in queries:
        for item in fetch_news(q):
            if item["link"] and item["link"] not in seen_set:
                candidates.append(item)
                seen_set.add(item["link"])

    if not candidates:
        log("Нових новин немає")
        sys.exit(0)

    # Шукаємо термінову
    picked = None
    is_urgent = False
    for item in candidates:
        urgent, reason = is_breaking(item, breaking_words)
        if urgent:
            picked = item
            is_urgent = True
            log(f"ТЕРМІНОВА: {reason}")
            break

    if not picked:
        picked = candidates[0]

    log(f"Обрано: {picked['title'][:60]}...")

    real_link = resolve_url(picked["link"])
    log(f"Посилання: {real_link[:80]}")

    image = fetch_image(real_link)
    caption = build_caption(picked, real_link, breaking=is_urgent)

    if image:
        log(f"Картинка: {image[:70]}")
        res = send_photo(image, caption)
        if not res.get("ok"):
            log(f"Фото не пройшло ({res.get('description')}), надсилаю текстом")
            res = send_text(caption)
    else:
        log("Картинки немає, надсилаю текстом")
        res = send_text(caption)

    if res.get("ok"):
        log("Опубліковано")
        seen.append(picked["link"])
        save_seen(seen)
    else:
        log(f"ПОМИЛКА Telegram: {res}")
        sys.exit(1)


if __name__ == "__main__":
    main()
