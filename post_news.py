#!/usr/bin/env python3
"""
Kresan News Poster
Тягне свіжі новини за ключовими словами і публікує дайджест у канал.
Джерело: Google News RSS (безкоштовно, без ключів).
"""

import json
import os
import sys
import re
import html
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from xml.etree import ElementTree

import requests

BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("TG_CHANNEL_ID", "")

BASE = Path(__file__).parent
SOURCES_FILE = BASE / "sources.json"
SEEN_FILE = BASE / "seen_news.json"

MAX_NEWS_PER_POST = 2      # скільки новин в одному пості
MAX_TITLE_LEN = 140        # обрізати задовгі заголовки


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
    # тримаємо тільки останні 300, щоб файл не ріс нескінченно
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump({"seen": seen[-2000:]}, f, ensure_ascii=False, indent=2)


def clean_title(title):
    """Прибирає HTML і назву джерела в кінці."""
    title = html.unescape(title)
    title = re.sub(r"<[^>]+>", "", title)
    # Google News додає " - Джерело" в кінці
    title = re.sub(r"\s+-\s+[^-]+$", "", title).strip()
    if len(title) > MAX_TITLE_LEN:
        title = title[:MAX_TITLE_LEN].rsplit(" ", 1)[0] + "…"
    return title


def fetch_news(query, lang="uk", country="UA"):
    """Тягне новини з Google News RSS за ключовим словом."""
    url = (
        f"https://news.google.com/rss/search?"
        f"q={quote(query)}&hl={lang}&gl={country}&ceid={country}:{lang}"
    )
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ElementTree.fromstring(r.content)
        items = []
        for item in root.findall(".//item"):
            title_el = item.find("title")
            link_el = item.find("link")
            source_el = item.find("source")
            if title_el is None or link_el is None:
                continue
            items.append({
                "title": clean_title(title_el.text or ""),
                "link": link_el.text or "",
                "source": (source_el.text if source_el is not None else "") or "",
            })
        log(f"'{query}': знайдено {len(items)}")
        return items
    except Exception as e:
        log(f"Помилка запиту '{query}': {e}")
        return []


def build_digest(news_items, header):
    """Формує текст поста."""
    lines = []
    if header:
        lines += [f"<b>{header}</b>", ""]
    for n in news_items:
        lines.append(f'<b>{html.escape(n["title"])}</b>')
        if n["source"]:
            lines.append(f'<i>{html.escape(n["source"])}</i>')
        lines.append(f'<a href="{n["link"]}">Читати</a>')
        lines.append("")
    return "\n".join(lines).strip()


def send(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    r = requests.post(url, json=payload, timeout=30)
    data = r.json()
    if data.get("ok"):
        log("Дайджест опубліковано")
        return True
    log(f"ПОМИЛКА Telegram: {data}")
    return False


def main():
    if not BOT_TOKEN or not CHANNEL_ID:
        log("ПОМИЛКА: не задано TG_BOT_TOKEN або TG_CHANNEL_ID")
        sys.exit(1)

    cfg = load_sources()
    queries = cfg.get("keywords", [])
    header = cfg.get("header", "Новини галузі")

    if not queries:
        log("Немає ключових слів у sources.json")
        sys.exit(1)

    seen = load_seen()
    seen_set = set(seen)

    fresh = []
    for q in queries:
        for item in fetch_news(q):
            key = item["link"]
            if key and key not in seen_set:
                fresh.append(item)
                seen_set.add(key)
            if len(fresh) >= MAX_NEWS_PER_POST:
                break
        if len(fresh) >= MAX_NEWS_PER_POST:
            break

    if not fresh:
        log("Нових новин немає, пропускаємо публікацію")
        sys.exit(0)

    text = build_digest(fresh, header)
    if send(text):
        seen.extend(n["link"] for n in fresh)
        save_seen(seen)
        log(f"Збережено {len(fresh)} новин у пам'ять")
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
