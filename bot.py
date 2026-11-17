#!/usr/bin/env python3
"""GTA 6 news bot: collects news, videos, guides and big streams and posts them to a Telegram channel.

Usage:
  python bot.py              # one pass (for cron / GitHub Actions)
  python bot.py --loop 300   # run forever, a pass every 300 s (for a VPS)
  python bot.py --dry-run    # print posts instead of sending, don't save state
"""
import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config.yaml"
UA = "Mozilla/5.0 (compatible; gta6-news-bot/1.0)"
MAX_SEEN = 20000

session = requests.Session()
session.headers["User-Agent"] = UA


def log(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


# ---------------------------------------------------------------- state
def load_state():
    try:
        s = json.loads(STATE_FILE.read_text("utf-8"))
    except (OSError, ValueError):
        s = {}
    s.setdefault("seen", [])
    s.setdefault("yt_ids", {})
    return s


def save_state(s):
    s["seen"] = s["seen"][-MAX_SEEN:]
    STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=0), "utf-8")


def h(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def title_key(title):
    t = re.sub(r"\s+[-–—|]\s+[^-–—|]{2,40}$", "", title)  # drop " - Source" suffix
    return re.sub(r"[^\w]+", "", t.lower())


# ---------------------------------------------------------------- parsing
def strip_html(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_date(s):
    if not s:
        return None
    s = s.strip()
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def local(tag):
    return tag.rsplit("}", 1)[-1]


def child(el, name):
    for c in el:
        if local(c.tag) == name:
            return c
    return None


def text_of(el, *names):
    for n in names:
        c = child(el, n)
        if c is not None and (c.text or "").strip():
            return c.text.strip()
    return ""


def parse_feed(xml_bytes):
    root = ET.fromstring(xml_bytes)
    items = []
    for el in root.iter():
        kind = local(el.tag)
        if kind not in ("item", "entry"):
            continue
        link = text_of(el, "link")
        if not link:  # Atom: <link href=...>
            for c in el:
                if local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                    link = c.get("href", "")
                    break
        guid = text_of(el, "guid", "id") or link
        thumb = ""
        for c in el.iter():
            if local(c.tag) in ("thumbnail", "content") and c.get("url", "").startswith("http") \
                    and re.search(r"\.(jpe?g|png|webp)|ytimg", c.get("url", "")):
                thumb = c.get("url")
                break
        items.append({
            "id": guid,
            "title": strip_html(text_of(el, "title")),
            "url": link,
            "summary": strip_html(text_of(el, "description", "summary", "encoded")
                                  or (child(child(el, "group"), "description").text
                                      if child(el, "group") is not None
                                      and child(child(el, "group"), "description") is not None else "")),
            "date": parse_date(text_of(el, "pubDate", "published", "updated", "date")),
            "origin": text_of(el, "source"),
            "thumb": thumb,
        })
    return items


def fetch(url, **kw):
    for attempt in range(3):
        try:
            r = session.get(url, timeout=20, **kw)
            if r.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == 2:
                raise
            log("  retry", url, e)
            time.sleep(3)
    raise RuntimeError(f"rate-limited: {url}")


# ---------------------------------------------------------------- sources
def youtube_feed_url(handle, state):
    cid = state["yt_ids"].get(handle)
    if not cid:
        page = fetch(f"https://www.youtube.com/@{handle}").text
        m = re.search(r"feeds/videos\.xml\?channel_id=(UC[\w-]+)", page) \
            or re.search(r'"externalId":"(UC[\w-]+)"', page)
        if not m:
            raise RuntimeError(f"YouTube channel @{handle} not found")
        cid = state["yt_ids"][handle] = m.group(1)
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"


def collect_feed(src, state, kw_re):
    url = youtube_feed_url(src["handle"], state) if src["type"] == "youtube" else src["url"]
    items = parse_feed(fetch(url).content)
    out = []
    for it in items:
        if src.get("filter") and not kw_re.search(f"{it['title']} {it['summary'][:500]}"):
            continue
        origin = it.pop("origin")
        if origin.startswith("http"):
            origin = ""
        if origin and it["title"].endswith(f" - {origin}"):
            it["title"] = it["title"][: -len(origin) - 3]
        it["source"] = origin or src["name"]
        it["kind"] = "video" if src["type"] == "youtube" else "news"
        it["tag"] = src.get("tag")
        if src.get("summary") is False:
            it["summary"] = ""
        out.append(it)
    return out


class Twitch:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cid = os.environ.get("TWITCH_CLIENT_ID")
        secret = os.environ.get("TWITCH_CLIENT_SECRET")
        if not (self.cid and secret):
            raise RuntimeError("TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET not set — streams skipped")
        r = session.post("https://id.twitch.tv/oauth2/token", timeout=20, data={
            "client_id": self.cid, "client_secret": secret, "grant_type": "client_credentials"})
        r.raise_for_status()
        self.hdr = {"Client-Id": self.cid, "Authorization": f"Bearer {r.json()['access_token']}"}

    def get(self, path, params):
        r = session.get(f"https://api.twitch.tv/helix/{path}", headers=self.hdr, params=params, timeout=20)
        r.raise_for_status()
        return r.json()

    def streams(self):
        game_ids = [g["id"] for g in self.get("games", [("name", n) for n in self.cfg["game_names"]])["data"]]
        if not game_ids:
            raise RuntimeError("Twitch category for GTA 6 not found")
        langs = set(self.cfg.get("languages") or [])
        watch = {w.lower() for w in self.cfg.get("watchlist") or []}
        min_v = int(self.cfg.get("min_viewers", 3000))
        out, cursor = [], None
        for _ in range(5):  # up to 500 streams, sorted by viewers desc
            params = [("game_id", g) for g in game_ids] + [("first", 100)]
            if cursor:
                params.append(("after", cursor))
            data = self.get("streams", params)
            for s in data["data"]:
                big = s["viewer_count"] >= min_v
                if not (big or s["user_login"].lower() in watch):
                    continue
                if langs and s["language"] not in langs and s["user_login"].lower() not in watch:
                    continue
                out.append({
                    "id": f"twitch:{s['id']}",
                    "title": s["title"],
                    "url": f"https://twitch.tv/{s['user_login']}",
                    "summary": "",
                    "date": parse_date(s["started_at"]),
                    "source": "Twitch",
                    "kind": "stream",
                    "streamer": s["user_name"],
                    "viewers": s["viewer_count"],
                    "lang": s["language"],
                    "thumb": s["thumbnail_url"].replace("{width}", "1280").replace("{height}", "720")
                             + f"?t={int(time.time())}",
                    "tag": None,
                })
            cursor = data.get("pagination", {}).get("cursor")
            if not cursor or (data["data"] and data["data"][-1]["viewer_count"] < min_v and not watch):
                break
        return out


# ---------------------------------------------------------------- formatting
TAGS = [
    ("трейлер", r"trailer|трейлер|тизер|teaser"),
    ("обновление", r"\bupdate|patch|hotfix|обновлен|патч|\bdlc\b|title update|pre-?load|предзагруз"),
    ("гайд", r"guide|how to|tips|walkthrough|tutorial|гайд|как (?:получить|найти|сделать|заработать)|советы|прохождени|секрет|easter egg|пасхалк"),
    ("настройки", r"settings|graphics|\bfps\b|performance|controls|config|настройк|графика|оптимизац|управлени|производительн"),
    ("слухи", r"leak|rumou?r|insider|report(?:ed|edly)?|утечк|слух|инсайд|слив"),
]
EMOJI = {"stream": "🔴", "video": "🎬", "трейлер": "🎞", "обновление": "🛠", "гайд": "📘",
         "настройки": "⚙️", "слухи": "🕵️", "официально": "⭐️", "сообщество": "💬"}


def classify(it):
    text = f"{it['title']} {it['summary'][:300]}".lower()
    tags = [t for t, rx in TAGS if re.search(rx, text)]
    if it.get("tag"):
        tags.insert(0, it["tag"])
    if it["kind"] == "stream":
        tags = ["стрим"]
    elif it["kind"] == "video":
        tags.insert(0, "видео")
    if not tags:
        tags = ["новости"]
    return list(dict.fromkeys(tags))


_translate_ok = True


def translate(text, target):
    global _translate_ok
    if not _translate_ok or not text or re.search(r"[а-яё]", text, re.I):
        return text
    try:
        r = session.get("https://translate.googleapis.com/translate_a/single", timeout=10,
                        params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text})
        r.raise_for_status()
        return "".join(p[0] for p in r.json()[0] if p[0]) or text
    except Exception as e:  # noqa: BLE001 — translation is best effort
        log("  translate failed, keeping originals:", e)
        _translate_ok = False
        return text


def render(it, cfg):
    tags = classify(it)
    tr = cfg.get("translate", {})
    esc = html.escape
    if it["kind"] == "stream":
        head = f"🔴 <b>{esc(it['streamer'])}</b> в эфире с GTA 6 — {it['viewers']:,} зрителей".replace(",", " ")
        body = esc(it["title"], quote=False)
        link = f'▶️ <a href="{esc(it["url"])}">Смотреть на Twitch</a>'
    else:
        title = it["title"]
        if tr.get("enabled"):
            title = translate(title, tr.get("target", "ru"))
        emoji = next((EMOJI[t] for t in tags if t in EMOJI), "📰")
        head = f"{emoji} <b>{esc(title, quote=False)}</b>"
        summary = it["summary"]
        if summary and tr.get("enabled"):
            summary = translate(summary[:400], tr.get("target", "ru"))
        if len(summary) > 350:
            summary = summary[:350].rsplit(" ", 1)[0] + "…"
        body = esc(summary, quote=False)
        link = f'🔗 <a href="{esc(it["url"])}">{esc(it["source"])}</a>'
    hashtags = "#GTA6 " + " ".join("#" + t for t in tags)
    return "\n\n".join(p for p in (head, body, link, hashtags) if p)


# ---------------------------------------------------------------- telegram
def tg_send(token, chat, text, photo=None):
    base = f"https://api.telegram.org/bot{token}"
    for attempt in range(4):
        if photo:
            r = session.post(f"{base}/sendPhoto", timeout=30, data={
                "chat_id": chat, "photo": photo, "caption": text[:1024], "parse_mode": "HTML"})
        else:
            r = session.post(f"{base}/sendMessage", timeout=30, data={
                "chat_id": chat, "text": text, "parse_mode": "HTML",
                "link_preview_options": json.dumps({"prefer_large_media": True})})
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 10)
            log(f"  telegram flood limit, waiting {wait}s")
            time.sleep(wait + 1)
            continue
        if r.status_code == 400 and photo:  # bad thumbnail — fall back to plain message
            photo = None
            continue
        if not r.ok:
            raise RuntimeError(f"Telegram error {r.status_code}: {r.text[:300]}")
        return
    raise RuntimeError("Telegram: too many retries")


# ---------------------------------------------------------------- join requests
def approve_join_requests(token, cfg, state, wait=0):
    """Approve pending join requests to the channel. `wait` > 0 = long-poll that many seconds."""
    jcfg = cfg.get("join_requests", {})
    if not jcfg.get("auto_approve", True):
        return 0
    base = f"https://api.telegram.org/bot{token}"
    r = session.get(f"{base}/getUpdates", timeout=wait + 20, params={
        "offset": state.get("tg_offset", 0), "timeout": wait,
        "allowed_updates": json.dumps(["chat_join_request"])})
    r.raise_for_status()
    approved = 0
    for u in r.json().get("result", []):
        state["tg_offset"] = u["update_id"] + 1
        req = u.get("chat_join_request")
        if not req:
            continue
        uid = req["from"]["id"]
        ok = session.post(f"{base}/approveChatJoinRequest", timeout=20,
                          data={"chat_id": req["chat"]["id"], "user_id": uid}).ok
        log(f"join request from {req['from'].get('first_name', uid)}: {'approved' if ok else 'failed'}")
        if ok:
            approved += 1
            if jcfg.get("welcome"):  # bot may DM a user for 5 minutes after their request
                session.post(f"{base}/sendMessage", timeout=20, data={
                    "chat_id": req.get("user_chat_id", uid), "text": jcfg["welcome"], "parse_mode": "HTML"})
    return approved


# ---------------------------------------------------------------- main loop
def run_once(dry_run=False):
    cfg = yaml.safe_load(CONFIG_FILE.read_text("utf-8"))
    state = load_state()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHANNEL")
    if token and not dry_run:
        try:
            approve_join_requests(token, cfg, state)
            save_state(state)
        except Exception as e:  # noqa: BLE001
            log("join requests:", e)
    first_run = not state["seen"]
    seen = set(state["seen"])
    kw_re = re.compile(cfg["keywords_regex"], re.I)

    items = []
    for src in cfg["sources"]:
        try:
            got = collect_feed(src, state, kw_re)
            log(f"{src['name']}: {len(got)}")
            items += got
        except Exception as e:  # noqa: BLE001 — one broken source must not stop the rest
            log(f"{src['name']}: ERROR {e}")
    if cfg.get("twitch", {}).get("enabled"):
        try:
            got = Twitch(cfg["twitch"]).streams()
            log(f"Twitch: {len(got)} big streams")
            items += got
        except Exception as e:  # noqa: BLE001
            log(f"Twitch: {e}")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=cfg.get("max_age_hours", 48))
    fresh, batch_keys = [], set()
    for it in items:
        keys = [h(it["id"]), h(it["url"])]
        if it["kind"] != "stream":
            keys.append(h(title_key(it["title"])))
        if any(k in seen or k in batch_keys for k in keys):
            continue
        batch_keys.update(keys)
        it["_keys"] = keys
        if it["date"] and it["date"] < cutoff:
            seen.update(keys)
            state["seen"] += keys
            continue
        fresh.append(it)

    fresh.sort(key=lambda i: i["date"] or datetime.now(timezone.utc))

    if first_run and not dry_run:
        log(f"First run: remembering {len(fresh)} existing items without posting.")
        for it in fresh:
            state["seen"] += it["_keys"]
        save_state(state)
        return

    if not dry_run and not (token and chat):
        sys.exit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL")

    limit = cfg.get("max_posts_per_run", 10)
    posted = 0
    for it in fresh[:limit]:
        text = render(it, cfg)
        photo = it["thumb"] if it["kind"] in ("stream", "video") else None
        if dry_run:
            print("-" * 60, f"\n[{it['source']}] photo={bool(photo)}\n{text}")
        else:
            try:
                tg_send(token, chat, text, photo)
            except Exception as e:  # noqa: BLE001
                log("  send failed:", e)
                continue
            state["seen"] += it["_keys"]
            posted += 1
            time.sleep(cfg.get("pause_between_posts", 3))
    log(f"New: {len(fresh)}, posted: {posted}, queued for next run: {max(0, len(fresh) - limit)}")
    if not dry_run:
        save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, metavar="SECONDS", help="repeat forever with this interval")
    ap.add_argument("--dry-run", action="store_true", help="print posts instead of sending")
    a = ap.parse_args()
    while True:
        try:
            run_once(a.dry_run)
        except Exception as e:  # noqa: BLE001
            if not a.loop:
                raise
            log("pass failed:", e)
        if not a.loop:
            break
        # between passes: long-poll join requests so they are approved instantly
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        until = time.time() + a.loop
        while time.time() < until:
            if not token or a.dry_run:
                time.sleep(max(0, until - time.time()))
                break
            try:
                cfg = yaml.safe_load(CONFIG_FILE.read_text("utf-8"))
                state = load_state()
                approve_join_requests(token, cfg, state, wait=int(min(50, max(1, until - time.time()))))
                save_state(state)
            except Exception as e:  # noqa: BLE001
                log("join requests:", e)
                time.sleep(10)


if __name__ == "__main__":
    main()
