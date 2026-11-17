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
        if origin and it["title"].endswith(f" - {origin}"):
            it["title"] = it["title"][: -len(origin) - 3]
        if origin.startswith("http"):  # Google sometimes names the source by its URL
            origin = re.sub(r"^https?://(www\.)?|/.*$", "", origin)
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
    ("обновление", r"\bupdate|patch|hotfix|firmware|обновлен|патч|прошивк|\bdlc\b|title update|pre-?load|предзагруз"),
    ("гайд", r"guide|how to|tips|walkthrough|tutorial|гайд|как (?:получить|найти|сделать|заработать)|советы|прохождени|секрет|easter egg|пасхалк"),
    ("настройки", r"settings|graphics|\bfps\b|performance|controls|config|настройк|графика|оптимизац|управлени|производительн"),
    ("слухи", r"leak|rumou?r|insider|report(?:ed|edly)?|утечк|слух|инсайд|слив"),
]
EMOJI = {"stream": "🔴", "video": "🎬", "трейлер": "🎞", "обновление": "🛠", "гайд": "📘",
         "настройки": "⚙️", "слухи": "🕵️", "официально": "⭐️", "сообщество": "💬"}


def classify(it):
    text = it["title"].lower()  # title only: descriptions gave too many false tags
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
        title = it.get("title_ru") or it["title"]
        if tr.get("enabled") and not it.get("title_ru"):
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
def visible_len(text):
    """Length Telegram counts for captions: text without HTML tags/entities."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", text)))


def tg_send(token, chat, text, photo=None, state=None, silent=False):
    """Send a post with a picture. Picture fallback chain: given URL -> re-upload -> banner -> text only."""
    base = f"https://api.telegram.org/bot{token}"
    state = state if state is not None else {}
    while visible_len(text) > 1024 and "\n\n" in text:  # caption limit: drop paragraphs from the middle
        parts = text.split("\n\n")
        text = "\n\n".join(parts[:1] + parts[2:]) if len(parts) > 2 else parts[0]
    quiet = {"disable_notification": "true"} if silent else {}
    banner = BANNER.exists()
    photo = photo or ("BANNER" if banner else None)
    for attempt in range(5):
        if photo == "BANNER" and not state.get("banner_file_id"):
            with open(BANNER, "rb") as fh:
                r = session.post(f"{base}/sendPhoto", timeout=60, files={"photo": fh}, data={
                    "chat_id": chat, "caption": text, "parse_mode": "HTML", **quiet})
            if r.ok:
                state["banner_file_id"] = r.json()["result"]["photo"][-1]["file_id"]
        elif photo:
            r = session.post(f"{base}/sendPhoto", timeout=30, data={
                "chat_id": chat, "parse_mode": "HTML", "caption": text, **quiet,
                "photo": state["banner_file_id"] if photo == "BANNER" else photo})
        else:
            r = session.post(f"{base}/sendMessage", timeout=30, data={
                "chat_id": chat, "text": text, "parse_mode": "HTML", **quiet,
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
                                     data={"chat_id": chat, "caption": text, "parse_mode": "HTML", **quiet})
                    if r.ok:
                        return r.json()["result"]["message_id"]
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
        return r.json()["result"]["message_id"]
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
def approve_join_requests(token, cfg, state, wait=0, chat=None):
    """Approve pending join requests to the channel. `wait` > 0 = long-poll that many seconds."""
    jcfg = cfg.get("join_requests", {})
    if not jcfg.get("auto_approve", True):
        return 0
    base = f"https://api.telegram.org/bot{token}"
    r = session.get(f"{base}/getUpdates", timeout=wait + 20, params={
        "offset": state.get("tg_offset", 0), "timeout": wait,
        "allowed_updates": json.dumps(["chat_join_request", "message"])})
    r.raise_for_status()
    approved = 0
    for u in r.json().get("result", []):
        state["tg_offset"] = u["update_id"] + 1
        msg = u.get("message")
        if msg and chat:
            try:
                handle_owner_message(token, chat, state, msg)
            except Exception as e:  # noqa: BLE001
                log("owner message:", e)
            continue
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


# ---------------------------------------------------------------- curation
MSK = timezone(timedelta(hours=3))
STOP = {"gta", "гта", "grand", "theft", "auto", "the", "and", "for", "with", "from", "that", "this", "your",
        "will", "has", "have", "about", "into", "что", "как", "для", "это", "его", "она", "они", "при",
        "все", "уже", "или", "так", "был", "было", "будет"}


def words(title):
    """Rough word stems for similarity: lowercase words, first 5 letters, minus filler words."""
    ws = re.findall(r"[a-zа-яё0-9]+", title.lower())
    return {w[:5] for w in ws if len(w) >= 3 and w not in STOP}


def is_similar(title, recent, threshold):
    a = words(title)
    if len(a) < 3:
        return False
    for r in recent:
        b = set(r)
        if len(b) >= 3 and len(a & b) / min(len(a), len(b)) >= threshold:
            return True
    return False


def score(it, tags):
    """Rough importance: decides what gets posted when the hourly limit is tight, what rings, what's in the digest."""
    pts = {"официально": 6, "трейлер": 4, "обновление": 3, "стрим": 4, "гайд": 2, "настройки": 2,
           "слухи": 1, "видео": 1}
    sc = 1 + sum(pts.get(t, 0) for t in tags)
    if "news.google.com" in it.get("url", ""):
        sc -= 1  # Google-only links: no picture, often a reprint
    if it.get("kind") == "stream":
        sc += min(4, it.get("viewers", 0) // 20000)
    return sc


def build_digest(state, max_items, day):
    """'Главное за день' post from what was published today (Moscow time)."""
    today = [p for p in state.get("posted", [])
             if datetime.fromtimestamp(p["ts"], MSK).date() == day and p.get("kind") != "stream"]
    if len(today) < 3:
        return None
    top = sorted(today, key=lambda p: (p["score"], p["ts"]), reverse=True)
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
    head = f"🌙 <b>Главное за день по GTA 6 — {day.day} {months[day.month - 1]}</b>"
    lines, text = [], ""
    for p in top[:max_items]:
        emoji = next((EMOJI[t] for t in p["tags"] if t in EMOJI), "📰")
        line = f'{len(lines) + 1}. {emoji} <a href="{html.escape(p["url"])}">{html.escape(p["title"], quote=False)}</a>'
        candidate = "\n\n".join([head, "\n".join(lines + [line]), "#GTA6 #дайджест"])
        if visible_len(candidate) > 1000:
            break
        lines.append(line)
        text = candidate
    return text


# ---------------------------------------------------------------- pinned posts
PINS_FILE = ROOT / "pins.yaml"


def publish_pins(token, chat, state):
    """Post and pin the posts from pins.yaml once (again only if the file changes).
    Posts go in reverse order so the first one in the file ends up on top of the pinned list."""
    if not PINS_FILE.exists():
        return
    raw = PINS_FILE.read_text("utf-8")
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    if state.get("pins_version") == digest:
        return
    pins = (yaml.safe_load(raw) or {}).get("pins") or []
    base = f"https://api.telegram.org/bot{token}"
    for text in reversed(pins):
        text = text.strip()
        if visible_len(text) <= 1024:
            mid = tg_send(token, chat, text, None, state, silent=True)  # with the channel banner
        else:
            r = session.post(f"{base}/sendMessage", timeout=30, data={
                "chat_id": chat, "text": text, "parse_mode": "HTML", "disable_notification": "true",
                "link_preview_options": json.dumps({"is_disabled": True})})
            if not r.ok:
                raise RuntimeError(f"pin post failed: {r.text[:200]}")
            mid = r.json()["result"]["message_id"]
        r = session.post(f"{base}/pinChatMessage", timeout=20,
                         data={"chat_id": chat, "message_id": mid, "disable_notification": "true"})
        log(f"pinned post {mid}: {'ok' if r.ok else r.text[:150]}")
        time.sleep(2)
    state["pins_version"] = digest
    save_state(state)


# ---------------------------------------------------------------- video link posts (YouTube / TikTok)
URL_RE = re.compile(r"https?://\S+")


def send_link_post(token, chat, url, text, silent=True):
    """Post whose preview is the video itself: YouTube links play right inside Telegram."""
    r = session.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, data={
        "chat_id": chat, "text": text, "parse_mode": "HTML", "disable_notification": str(silent).lower(),
        "link_preview_options": json.dumps({"url": url, "prefer_large_media": True, "show_above_text": True})})
    if not r.ok:
        raise RuntimeError(f"Telegram error {r.status_code}: {r.text[:200]}")
    return r.json()["result"]["message_id"]


def owner_id(token, chat, state):
    if not state.get("owner_id"):
        admins = session.get(f"https://api.telegram.org/bot{token}/getChatAdministrators",
                             params={"chat_id": chat}, timeout=20).json()
        state["owner_id"] = next((a["user"]["id"] for a in admins.get("result", [])
                                  if a.get("status") == "creator"), None)
    return state["owner_id"]


def handle_owner_message(token, chat, state, msg):
    """The channel owner sends the bot a video link (+ optional description) -> it goes to the channel."""
    sender = msg.get("from", {}).get("id")
    if msg.get("chat", {}).get("type") != "private" or sender != owner_id(token, chat, state):
        return
    text = msg.get("text") or msg.get("caption") or ""
    base = f"https://api.telegram.org/bot{token}"
    m = URL_RE.search(text)
    if not m:
        if text.startswith("/start") or text.startswith("/help"):
            session.post(f"{base}/sendMessage", timeout=20, data={"chat_id": sender, "text":
                         "Пришлите ссылку на видео (TikTok, YouTube, Shorts) и, если хотите, описание "
                         "в том же сообщении — я опубликую его в канале в течение ~10 минут."})
        return
    url = m.group(0).rstrip(").,")
    desc = (text[:m.start()] + text[m.end():]).strip()
    platform = ("TikTok" if "tiktok.com" in url else "YouTube" if re.search(r"youtu\.?be", url) else "видео")
    body = [f"🔥 <b>{html.escape(desc, quote=False)}</b>" if desc else "🔥 <b>Видео дня по GTA 6</b>",
            f'▶️ <a href="{html.escape(url)}">Смотреть в {platform}</a>', "#GTA6 #видео"]
    mid = send_link_post(token, chat, url, "\n\n".join(body), silent=True)
    session.post(f"{base}/sendMessage", timeout=20, data={"chat_id": sender, "text": f"✅ Опубликовано в канале"})
    log(f"owner link posted to channel: message {mid}")


def fmt_views(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".").replace(".", ",") + " млн"
    if n >= 1000:
        return f"{n // 1000} тыс."
    return str(n)


def iso_duration_s(d):
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", d or "")
    if not m:
        return 0
    days, hrs, mins, secs = (int(x or 0) for x in m.groups())
    return days * 86400 + hrs * 3600 + mins * 60 + secs


def post_viral_youtube(token, chat, state, cfg, dry_run=False):
    """Find GTA 6 videos that are blowing up on YouTube and post the best as playable links."""
    vc = cfg.get("viral", {})
    key = os.environ.get("YOUTUBE_API_KEY")
    if not vc.get("enabled") or not key:
        return
    now = time.time()
    # YouTube API quota: each search costs 100 of 10 000 daily units — search every N minutes only
    if not dry_run and now - state.get("viral_checked", 0) < vc.get("check_every_minutes", 120) * 60:
        return
    state["viral_checked"] = now
    posted_today = [t for t in state.get("viral_ts", []) if t > now - 86400]
    budget = vc.get("max_per_day", 4) - len(posted_today)
    if budget <= 0:
        return
    since = (datetime.now(timezone.utc) - timedelta(hours=vc.get("window_hours", 48))).strftime("%Y-%m-%dT%H:%M:%SZ")
    api = "https://www.googleapis.com/youtube/v3"
    ids = []
    for q, lang in vc.get("queries", [["GTA 6|GTA VI", "en"], ["ГТА 6|GTA 6", "ru"]]):
        r = session.get(f"{api}/search", timeout=20, params={
            "key": key, "part": "id", "type": "video", "q": q, "order": "viewCount", "maxResults": 25,
            "publishedAfter": since, "relevanceLanguage": lang})
        if not r.ok:
            raise RuntimeError(f"YouTube search {r.status_code}: {r.text[:200]}")
        ids += [i["id"]["videoId"] for i in r.json().get("items", [])]
    ids = list(dict.fromkeys(ids))
    if not ids:
        return
    r = session.get(f"{api}/videos", timeout=20, params={
        "key": key, "part": "snippet,statistics,contentDetails", "id": ",".join(ids[:50])})
    r.raise_for_status()
    kw_re = re.compile(cfg["keywords_regex"], re.I)
    skip_channels = {c.lower() for c in vc.get("skip_channels", [])}
    seen = set(state["seen"])
    cands = []
    for v in r.json().get("items", []):
        sn, st = v["snippet"], v.get("statistics", {})
        views = int(st.get("viewCount", 0))
        age_h = max(1.0, (datetime.now(timezone.utc) - parse_date(sn["publishedAt"])).total_seconds() / 3600)
        key_id = h(f"yt:{v['id']}")
        if key_id in seen or views < vc.get("min_views", 100_000) or sn["channelTitle"].lower() in skip_channels:
            continue
        if not kw_re.search(f"{sn['title']} {sn.get('description', '')[:300]}"):
            continue
        if sn.get("liveBroadcastContent") != "none":
            continue
        cands.append({"id": v["id"], "title": html.unescape(sn["title"]), "channel": sn["channelTitle"],
                      "views": views, "age_h": age_h, "speed": views / age_h, "key": key_id,
                      "short": iso_duration_s(v.get("contentDetails", {}).get("duration")) <= 60})
    cands.sort(key=lambda c: c["speed"], reverse=True)
    recent_titles = [p["words"] for p in state.get("posted", []) if p["ts"] > now - 86400]
    tr = cfg.get("translate", {})
    for c in cands[:budget]:
        title = (translate(c["title"], tr.get("target", "ru")) if tr.get("enabled") else None) or c["title"]
        if is_similar(title, recent_titles, cfg.get("filters", {}).get("similar_threshold", 0.6)):
            state["seen"].append(c["key"])
            continue
        url = (f"https://youtube.com/shorts/{c['id']}" if c["short"] else f"https://youtu.be/{c['id']}")
        hours = int(c["age_h"])
        when = f"за {hours} ч" if hours < 48 else f"за {hours // 24} дн"
        text = "\n\n".join([
            f"🔥 <b>Вирусное видео: {html.escape(title, quote=False)}</b>",
            f"👀 {fmt_views(c['views'])} просмотров {when} · 📺 {html.escape(c['channel'], quote=False)}",
            f'▶️ <a href="{url}">Смотреть на YouTube</a>',
            "#GTA6 #видео #вирусное"])
        if dry_run:
            print("-" * 60, "\n" + text)
            continue
        send_link_post(token, chat, url, text, silent=True)
        state["seen"].append(c["key"])
        state.setdefault("viral_ts", []).append(time.time())
        state.setdefault("posted", []).append({"ts": time.time(), "title": title[:200], "url": url,
                                               "tags": ["видео"], "score": 2, "kind": "viral", "img": None,
                                               "source": c["channel"], "words": list(words(title))})
        recent_titles.append(list(words(title)))
        log(f"viral video posted: {c['views']} views, {c['channel']}")
        time.sleep(3)
    state["viral_ts"] = [t for t in state.get("viral_ts", []) if t > now - 86400]


# ---------------------------------------------------------------- short videos
def channel_owner_and_link(token, chat):
    base = f"https://api.telegram.org/bot{token}"
    admins = session.get(f"{base}/getChatAdministrators", params={"chat_id": chat}, timeout=20).json()
    owner = next((a["user"]["id"] for a in admins.get("result", []) if a.get("status") == "creator"), None)
    info = session.get(f"{base}/getChat", params={"chat_id": chat}, timeout=20).json().get("result", {})
    link = f"https://t.me/{info['username']}" if info.get("username") else ""
    return owner, link


def send_daily_short(token, chat, state, cfg):
    """Render «3 главные новости за день» and DM it to the channel owner with a ready-made description."""
    sc = cfg.get("shorts", {})
    now_msk = datetime.now(MSK)
    today = now_msk.date()
    if not sc.get("enabled") or now_msk.hour < sc.get("hour_msk", 19) or state.get("short_date") == today.isoformat():
        return
    posts = [p for p in state.get("posted", []) if p.get("kind") != "stream"
             and datetime.fromtimestamp(p["ts"], MSK).date() == today]
    if len(posts) < 3:
        return
    top = sorted(posts, key=lambda p: (bool(p.get("img")), p["score"], p["ts"]), reverse=True)[:3]
    owner, link = channel_owner_and_link(token, chat)
    base = f"https://api.telegram.org/bot{token}"
    if not owner or not session.post(f"{base}/sendChatAction", timeout=20,
                                     data={"chat_id": owner, "action": "upload_video"}).ok:
        log("shorts: can't message the channel owner — open the bot in Telegram and press Start")
        return
    import shorts  # heavy imports only when needed
    items = [{"title": p["title"], "source": p.get("source", ""),
              "img": p.get("img") or find_image({"url": p.get("url", ""), "thumb": ""})} for p in top]
    out = ROOT / "short.mp4"
    shorts.render_short(items, today, out)
    with open(out, "rb") as fh:
        r = session.post(f"{base}/sendVideo", timeout=120, files={"video": ("gta6_short.mp4", fh, "video/mp4")},
                         data={"chat_id": owner, "width": 1080, "height": 1920, "supports_streaming": "true",
                               "caption": "🎬 Ролик дня для TikTok / Shorts / Reels. Описание — следующим сообщением."})
    out.unlink(missing_ok=True)
    if not r.ok:
        raise RuntimeError(f"sendVideo failed: {r.text[:200]}")
    session.post(f"{base}/sendMessage", timeout=20, data={"chat_id": owner,
                 "text": shorts.caption(items, today, link), "link_preview_options": json.dumps({"is_disabled": True})})
    state["short_date"] = today.isoformat()
    log("Daily short sent to the channel owner.")


# ---------------------------------------------------------------- main loop
def run_once(dry_run=False):
    cfg = yaml.safe_load(CONFIG_FILE.read_text("utf-8"))
    state = load_state()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = normalize_chat(str(cfg.get("channel") or "") or os.environ.get("TELEGRAM_CHANNEL"))
    if token and not dry_run:
        try:
            approve_join_requests(token, cfg, state, chat=chat)
            save_state(state)
        except Exception as e:  # noqa: BLE001
            log("join requests:", e)
    if token and chat and not dry_run:
        try:
            publish_pins(token, chat, state)
        except Exception as e:  # noqa: BLE001
            log("pins:", e)
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

    # --- curation: junk filter, importance order, re-told duplicates, hourly limit
    flt = cfg.get("filters", {})
    lim = cfg.get("limits", {})
    quiet = cfg.get("quiet_mode", {})
    tr = cfg.get("translate", {})
    excl = re.compile(flt["exclude_regex"], re.I) if flt.get("exclude_regex") else None
    now_ts = time.time()
    state.setdefault("posted", [])
    state["posted"] = [p for p in state["posted"] if p["ts"] > now_ts - 48 * 3600]
    recent_titles = [p["words"] for p in state["posted"] if p["ts"] > now_ts - 24 * 3600]
    last_hour = sum(1 for p in state["posted"] if p["ts"] > now_ts - 3600)
    budget = max(0, min(cfg.get("max_posts_per_run", 20), lim.get("max_posts_per_hour", 10) - last_hour))

    for it in fresh:
        it["_tags"] = classify(it)
        it["_score"] = score(it, it["_tags"])
    candidates = sorted(fresh, key=lambda i: (i["_score"], i["date"] or datetime.now(timezone.utc)), reverse=True)
    picked, skipped_junk, skipped_dup = [], 0, 0
    for it in candidates:
        if len(picked) >= budget:
            break
        if excl and it["kind"] != "stream" and excl.search(f"{it['title']} {it['summary'][:300]}"):
            skipped_junk += 1
            state["seen"] += it["_keys"]
            continue
        if it["kind"] != "stream":
            if tr.get("enabled"):
                it["title_ru"] = translate(it["title"], tr.get("target", "ru")) or it["title"]
            if is_similar(it.get("title_ru") or it["title"], recent_titles, flt.get("similar_threshold", 0.6)):
                skipped_dup += 1
                state["seen"] += it["_keys"]
                continue
            recent_titles.append(list(words(it.get("title_ru") or it["title"])))
        picked.append(it)
    picked.sort(key=lambda i: i["date"] or datetime.now(timezone.utc))  # publish in chronological order

    loud_tags = set(quiet.get("loud_tags", []))
    posted = 0
    for it in picked:
        text = render(it, cfg)
        photo = find_image(it)
        silent = bool(quiet.get("enabled")) and not (loud_tags & set(it["_tags"]))
        if dry_run:
            print("-" * 60, f"\n[{it['source']}] score={it['_score']} silent={silent} photo={photo or 'BANNER'}\n{text}")
            continue
        try:
            tg_send(token, chat, text, photo, state, silent=silent)
        except Exception as e:  # noqa: BLE001
            log("  send failed:", e)
            continue
        state["seen"] += it["_keys"]
        title = it.get("title_ru") or it["title"]
        state["posted"].append({"ts": time.time(), "title": title[:200], "url": it["url"], "tags": it["_tags"],
                                "img": photo, "source": it["source"],
                                "score": it["_score"], "kind": it["kind"], "words": list(words(title))})
        posted += 1
        time.sleep(cfg.get("pause_between_posts", 3))
    log(f"New: {len(fresh)}, junk: {skipped_junk}, re-told: {skipped_dup}, posted: {posted}, "
        f"hourly budget left: {budget - posted}, waiting: {len(fresh) - posted - skipped_junk - skipped_dup}")

    # --- evening digest
    dg = cfg.get("digest", {})
    now_msk = datetime.now(MSK)
    if dg.get("enabled") and now_msk.hour >= dg.get("hour_msk", 21) \
            and state.get("digest_date") != now_msk.date().isoformat():
        text = build_digest(state, dg.get("max_items", 10), now_msk.date())
        if text and dry_run:
            print("=" * 60, "\nDIGEST\n" + text)
        elif text:
            try:
                tg_send(token, chat, text, None, state)
                state["digest_date"] = now_msk.date().isoformat()
                log("Digest posted.")
            except Exception as e:  # noqa: BLE001
                log("  digest failed:", e)
        elif not dry_run:
            state["digest_date"] = now_msk.date().isoformat()  # quiet day: nothing to digest
    try:
        post_viral_youtube(token, chat, state, cfg, dry_run)
    except Exception as e:  # noqa: BLE001
        log("viral:", e)
    if not dry_run and token and chat:
        try:
            send_daily_short(token, chat, state, cfg)
        except Exception as e:  # noqa: BLE001
            log("shorts:", e)
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
                approve_join_requests(token, cfg, state, wait=int(min(50, max(1, until - time.time()))),
                                      chat=normalize_chat(str(cfg.get("channel") or "")
                                                          or os.environ.get("TELEGRAM_CHANNEL")))
                save_state(state)
            except Exception as e:  # noqa: BLE001
                log("join requests:", e)
                time.sleep(10)


if __name__ == "__main__":
    main()
