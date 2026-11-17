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
        if not thumb:
            for c in el:
                if local(c.tag) == "enclosure" and (c.get("type", "").startswith("image")
                                                    or re.search(r"\.(jpe?g|png|webp)", c.get("url", ""))):
                    thumb = c.get("url", "")
                    break
        if not thumb:
            raw = text_of(el, "encoded", "description", "summary", "content")
            m = re.search(r'<img[^>]+src=["\'](https?://[^"\']+)', html.unescape(raw))
            thumb = m.group(1) if m else ""
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


_tr_dead = set()  # providers that refused during this run


def _tr_google(text, target):
    r = session.get("https://translate.googleapis.com/translate_a/single", timeout=10,
                    params={"client": "gtx", "sl": "auto", "tl": target, "dt": "t", "q": text})
    r.raise_for_status()
    return "".join(p[0] for p in r.json()[0] if p[0])


def _tr_mymemory(text, target):
    # free public API: ~5000 chars/day per IP, max 500 chars per request
    r = session.get("https://api.mymemory.translated.net/get", timeout=15,
                    params={"q": text[:500], "langpair": f"en|{target}"})
    r.raise_for_status()
    d = r.json()
    out = html.unescape(d.get("responseData", {}).get("translatedText") or "")
    if str(d.get("responseStatus")) != "200" or "MYMEMORY WARNING" in out.upper():
        raise RuntimeError(f"MyMemory: {out[:120] or d.get('responseStatus')}")
    return out


TRANSLATORS = [("google", _tr_google), ("mymemory", _tr_mymemory)]


def translate(text, target):
    """Best-effort translation. Returns None if every provider failed."""
    if not text or re.search(r"[а-яё]", text, re.I):
        return text
    for name, fn in TRANSLATORS:
        if name in _tr_dead:
            continue
        try:
            out = fn(text, target).strip()
            if out:
                return out
        except Exception as e:  # noqa: BLE001 — translation is best effort
            log(f"  translator {name} unavailable for this run: {str(e)[:150]}")
            _tr_dead.add(name)
    return None


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
            title = translate(title, tr.get("target", "ru")) or title
        emoji = next((EMOJI[t] for t in tags if t in EMOJI), "📰")
        head = f"{emoji} <b>{esc(title, quote=False)}</b>"
        summary = it["summary"]
        if summary and tr.get("enabled") and not re.search(r"[а-яё]", summary, re.I):
            # descriptions are long, so only Google translates them (MyMemory's daily quota
            # is kept for titles); an untranslated English description is dropped
            summary = ""
            if "google" not in _tr_dead:
                try:
                    summary = _tr_google(it["summary"][:400], tr.get("target", "ru"))
                except Exception as e:  # noqa: BLE001
                    log(f"  translator google unavailable for this run: {str(e)[:150]}")
                    _tr_dead.add("google")
        if len(summary) > 350:
            summary = summary[:350].rsplit(" ", 1)[0] + "…"
        body = esc(summary, quote=False)
        link = f'🔗 <a href="{esc(it["url"])}">{esc(it["source"])}</a>'
    hashtags = "#GTA6 " + " ".join("#" + t for t in tags)
    return "\n\n".join(p for p in (head, body, link, hashtags) if p)


# ---------------------------------------------------------------- images
BANNER = ROOT / "assets" / "banner.jpg"
OG_RE = [re.compile(r'<meta[^>]+(?:property|name)=["\'](?:og:image(?::secure_url)?|twitter:image(?::src)?)["\']'
                    r'[^>]*content=["\']([^"\']+)', re.I),
         re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']'
                    r'(?:og:image|twitter:image)["\']', re.I)]


def find_image(it):
    """Picture for a post: feed thumbnail, else the article's og:image, else None (banner is used)."""
    if it.get("thumb"):
        # some feeds give tiny previews; ask for the large variant
        return re.sub(r"(//images\.(?:pushsquare|purexbox|nintendolife)\.com/[^/]+/)small\.jpg", r"\1large.jpg",
                      it["thumb"])
    url = it.get("url", "")
    if not url.startswith("http") or "news.google.com" in url:  # Google News links are JS redirects
        return None
    try:
        r = session.get(url, timeout=12, stream=True)
        head = r.raw.read(300_000, decode_content=True).decode(r.encoding or "utf-8", "ignore")
        r.close()
        for rx in OG_RE:
            m = rx.search(head)
            if m:
                img = html.unescape(m.group(1)).strip()
                if img.startswith("//"):
                    img = "https:" + img
                if img.startswith("http"):
                    return img
    except Exception as e:  # noqa: BLE001
        log(f"  og:image lookup failed for {url[:80]}: {str(e)[:100]}")
    return None


# ---------------------------------------------------------------- telegram
def tg_send(token, chat, text, photo=None, state=None):
    """Send a post with a picture. Picture fallback chain: given URL -> channel banner -> text only."""
    base = f"https://api.telegram.org/bot{token}"
    state = state if state is not None else {}
    if len(text) > 1024:  # photo captions are limited to 1024 chars
        text = text[:1000].rsplit("\n", 1)[0]
    banner = BANNER.exists()
    photo = photo or ("BANNER" if banner else None)
    for attempt in range(5):
        if photo == "BANNER" and not state.get("banner_file_id"):
            with open(BANNER, "rb") as fh:
                r = session.post(f"{base}/sendPhoto", timeout=60, files={"photo": fh}, data={
                    "chat_id": chat, "caption": text, "parse_mode": "HTML"})
            if r.ok:
                state["banner_file_id"] = r.json()["result"]["photo"][-1]["file_id"]
        elif photo:
            r = session.post(f"{base}/sendPhoto", timeout=30, data={
                "chat_id": chat, "parse_mode": "HTML", "caption": text,
                "photo": state["banner_file_id"] if photo == "BANNER" else photo})
        else:
            r = session.post(f"{base}/sendMessage", timeout=30, data={
                "chat_id": chat, "text": text, "parse_mode": "HTML",
                "link_preview_options": json.dumps({"prefer_large_media": True})})
        if r.status_code == 429:
            wait = r.json().get("parameters", {}).get("retry_after", 10)
            log(f"  telegram flood limit, waiting {wait}s")
            time.sleep(wait + 1)
            continue
        if r.status_code == 400 and photo:  # picture rejected: upload it ourselves, then banner, then text
            log(f"  picture rejected ({r.json().get('description', '')[:80]}), falling back")
            if photo not in ("BANNER", None):
                try:
                    img = session.get(photo, timeout=20, headers={"Referer": photo})
                    img.raise_for_status()
                    if len(img.content) > 9_500_000 or not img.headers.get("content-type", "").startswith("image"):
                        raise ValueError(f"not an uploadable image ({img.headers.get('content-type')})")
                    ext = img.headers["content-type"].split("/")[-1].split(";")[0]
                    r = session.post(f"{base}/sendPhoto", timeout=60,
                                     files={"photo": (f"preview.{ext}", img.content)},
                                     data={"chat_id": chat, "caption": text, "parse_mode": "HTML"})
                    if r.ok:
                        return
                    log(f"  upload rejected too: {r.text[:120]}")
                except Exception as e:  # noqa: BLE001
                    log(f"  could not re-upload picture: {str(e)[:120]}")
                photo = "BANNER" if banner else None
                continue
            if photo == "BANNER":
                state.pop("banner_file_id", None)
                photo = None
            else:
                photo = "BANNER" if banner else None
            continue
        if not r.ok:
            raise RuntimeError(f"Telegram error {r.status_code}: {r.text[:300]}")
        return
    raise RuntimeError("Telegram: too many retries")


def normalize_chat(raw):
    """Accept -100123..., @name, t.me/name; log only the *shape* of the value (logs may be public)."""
    if not raw:
        return raw
    c = raw.strip().strip('"').strip("'")
    m = re.match(r"^(?:https?://)?t\.me/([A-Za-z]\w{3,})/?$", c)
    if m:
        c = "@" + m.group(1)
    elif re.fullmatch(r"[A-Za-z]\w{3,}", c):
        c = "@" + c
    elif re.fullmatch(r"-?\d{9,}", c) and not c.startswith("-100"):
        c = "-100" + c.lstrip("-")  # channel ids need the -100 prefix
    shape = ("numeric id, starts with -100" if re.fullmatch(r"-100\d+", c)
             else "numeric id WITHOUT -100 prefix" if re.fullmatch(r"-?\d+", c)
             else "@username" if c.startswith("@")
             else "private invite link (not usable, need numeric id)" if "t.me/+" in c or "joinchat" in c
             else "unrecognized")
    log(f"TELEGRAM_CHANNEL format: {shape}, length {len(c)}, had extra spaces/quotes: {c != raw and not c.startswith('@')}")
    return c


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
    chat = normalize_chat(str(cfg.get("channel") or "") or os.environ.get("TELEGRAM_CHANNEL"))
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

    # on duplicates prefer direct article links (they have pictures) over Google News redirects
    items.sort(key=lambda i: "news.google.com" in i.get("url", ""))
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
        photo = find_image(it)
        if dry_run:
            print("-" * 60, f"\n[{it['source']}] photo={photo or 'BANNER'}\n{text}")
        else:
            try:
                tg_send(token, chat, text, photo, state)
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
