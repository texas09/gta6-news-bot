"""Vertical short video (1080x1920) «3 главные новости GTA 6 за день» for TikTok / Shorts / Reels.

Frames are drawn with Pillow and piped into ffmpeg. The video has a silent audio track:
music or voice-over is meant to be added in the TikTok / YouTube app (trending sounds reach more people).

Standalone test:  python shorts.py out.mp4   (uses sample items)
"""
import io
import math
import subprocess
import sys
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent
FONT = ROOT / "assets" / "fonts" / "Montserrat.ttf"
BANNER = ROOT / "assets" / "banner.jpg"
W, H, FPS = 1080, 1920, 30
INTRO, PER_ITEM, OUTRO = 2.5, 5.5, 3.0
YELLOW, PINK, WHITE, DARK = (255, 214, 90), (255, 70, 150), (255, 255, 255), (20, 6, 35)
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
          "сентября", "октября", "ноября", "декабря"]


def font(size, weight="ExtraBold"):
    f = ImageFont.truetype(str(FONT), size)
    try:
        f.set_variation_by_name(weight)
    except (OSError, ValueError):
        pass
    return f


def wrap(draw, text, fnt, max_w, max_lines):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=fnt) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=fnt) > max_w:
            lines[-1] = lines[-1].rsplit(" ", 1)[0] if " " in lines[-1] else lines[-1][:-1]
        lines[-1] += "…"
    return lines


def fit_headline(draw, text, max_w, max_h):
    """Largest font size at which the headline fits the box."""
    for size in range(84, 43, -4):
        f = font(size, "Black")
        lines = wrap(draw, text, f, max_w, 6)
        if len(lines) * size * 1.22 <= max_h and not lines[-1].endswith("…"):
            return f, lines
    f = font(44, "Black")
    return f, wrap(draw, text, f, max_w, 6)


def load_image(url):
    for _ in range(3 if url else 0):
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return Image.open(io.BytesIO(r.content)).convert("RGB")
        except Exception:  # noqa: BLE001 — retry, then fall back to the banner
            continue
    return Image.open(BANNER).convert("RGB")


def cover(img, w, h):
    """Resize + center-crop to exactly w x h."""
    k = max(w / img.width, h / img.height)
    img = img.resize((math.ceil(img.width * k), math.ceil(img.height * k)), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def gradient_bg():
    bg = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(bg)
    for y in range(H):
        t = y / H
        c = tuple(int(a + (b - a) * t) for a, b in zip((30, 10, 60), (200, 40, 120)))
        d.line([(0, y), (W, y)], fill=c)
    return bg


def ease(t):
    t = max(0.0, min(1.0, t))
    return 1 - (1 - t) ** 3


# ---------------------------------------------------------------- scenes
def make_item_scene(item, num):
    """Static layers for one news item; returns a function t -> frame."""
    img = load_image(item.get("img"))
    bg = cover(img, W, H).filter(ImageFilter.GaussianBlur(40))
    bg = Image.blend(bg, Image.new("RGB", (W, H), DARK), 0.55)
    photo_h = 760
    photo = cover(img, W - 80, photo_h)
    # text card
    card = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(card)
    top = 230 + photo_h + 60
    fnt, lines = fit_headline(d, item["title"], W - 140, H - top - 320)
    y = top
    for ln in lines:
        d.text((74, y + 5), ln, font=fnt, fill=DARK)
        d.text((70, y), ln, font=fnt, fill=WHITE)
        y += int(fnt.size * 1.22)
    src = font(40, "SemiBold")
    d.text((70, y + 30), f"Источник: {item.get('source', '')}"[:60], font=src, fill=(220, 220, 235))
    badge = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    b = ImageDraw.Draw(badge)
    b.rounded_rectangle((40, 90, 40 + 190, 90 + 110), 28, fill=PINK)
    b.text((40 + 95, 90 + 55), f"#{num}", font=font(72, "Black"), fill=WHITE, anchor="mm")
    b.text((260, 145), "GTA 6 РАДАР", font=font(52, "Black"), fill=YELLOW, anchor="lm")

    def frame(t):
        f = bg.copy()
        z = 1.0 + 0.08 * (t / PER_ITEM)  # slow Ken Burns zoom
        pw, ph = int(photo.width * z), int(photo.height * z)
        zp = photo.resize((pw, ph), Image.BILINEAR).crop(((pw - photo.width) // 2, (ph - photo.height) // 2,
                                                          (pw + photo.width) // 2, (ph + photo.height) // 2))
        mask = Image.new("L", photo.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle((0, 0, photo.width, photo.height), 36, fill=255)
        f.paste(zp, (40, 230), mask)
        k = ease(t / 0.45)  # headline slides up and fades in
        layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        layer.paste(card, (0, int(60 * (1 - k))))
        if k < 1:
            layer.putalpha(layer.getchannel("A").point(lambda a: int(a * k)))
        f.paste(layer, (0, 0), layer)
        f.paste(badge, (0, 0), badge)
        return f
    return frame


def make_title_scene(lines, sub, dur):
    bg = gradient_bg()
    sun = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(sun)
    sd.ellipse((W // 2 - 330, 1150, W // 2 + 330, 1810), fill=(255, 200, 90, 255))
    for i, y in enumerate(range(1520, 1820, 34)):  # retro stripes cut through the sun
        sd.rectangle((0, y, W, y + 8 + i * 2), fill=(0, 0, 0, 0))
    bg.paste(sun, (0, 0), sun)
    d0 = ImageDraw.Draw(bg)
    for i, y in enumerate(range(1520, 1820, 34)):  # horizon lines
        d0.rectangle((0, y, W, y + 8 + i * 2), fill=(200 + i * 5, 60 + i * 8, 110))
    d0.rectangle((0, 1800, W, H), fill=DARK)
    txt = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(txt)
    y = 430
    for i, (text, size, color) in enumerate(lines):
        f = font(size, "Black")
        d.text((W // 2 + 6, y + 6), text, font=f, fill=DARK, anchor="mt")
        d.text((W // 2, y), text, font=f, fill=color, anchor="mt")
        y += int(size * 1.25)
    if sub:
        d.text((W // 2, y + 40), sub, font=font(50, "Bold"), fill=WHITE, anchor="mt")

    def frame(t):
        f = bg.copy()
        k = ease(t / 0.5)
        s = 0.85 + 0.15 * k
        tw, th = int(W * s), int(H * s)
        layer = txt.resize((tw, th), Image.BILINEAR)
        canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        canvas.paste(layer, ((W - tw) // 2, (H - th) // 2 - int(80 * (1 - s))))
        canvas.putalpha(canvas.getchannel("A").point(lambda a: int(a * k)))
        f.paste(canvas, (0, 0), canvas)
        return f
    return frame


# ---------------------------------------------------------------- render
def render_short(items, day, out_path, channel_name="GTA 6 Радар"):
    """items: [{title, img, source}] (up to 3). Writes an MP4; returns its path."""
    items = items[:3]
    date = f"{day.day} {MONTHS[day.month - 1]}"
    scenes = [(make_title_scene([(f"{len(items)} ГЛАВНЫЕ", 110, WHITE), ("НОВОСТИ", 110, WHITE),
                                 ("GTA 6", 170, YELLOW)], f"за {date}", INTRO), INTRO)]
    scenes += [(make_item_scene(it, i + 1), PER_ITEM) for i, it in enumerate(items)]
    scenes.append((make_title_scene([("ВСЕ НОВОСТИ", 96, WHITE), ("GTA 6", 170, YELLOW), ("В TELEGRAM", 96, WHITE)],
                                    f"«{channel_name}» — ссылка в профиле", OUTRO), OUTRO))
    total = sum(d for _, d in scenes)
    cmd = ["ffmpeg", "-y", "-loglevel", "error",
           "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-f", "lavfi", "-t", f"{total:.2f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(out_path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    prev_last = None
    for frame_fn, dur in scenes:
        n = int(dur * FPS)
        for i in range(n):
            fr = frame_fn(i / FPS)
            if prev_last is not None and i < 6:  # short cross-fade between scenes
                fr = Image.blend(prev_last, fr, (i + 1) / 7)
            proc.stdin.write(fr.tobytes())
            if i == n - 1:
                prev_last = fr
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed")
    return out_path


def caption(items, day, channel_link=""):
    date = f"{day.day} {MONTHS[day.month - 1]}"
    lines = [f"3 главные новости GTA 6 за {date} 🌴", ""]
    lines += [f"{i + 1}. {it['title']}" for i, it in enumerate(items[:3])]
    lines += ["", "Все новости GTA 6 первыми — в Telegram «GTA 6 Радар»" + (f": {channel_link}" if channel_link
                                                                           else " (ссылка в профиле)"),
              "", "#gta6 #гта6 #gtavi #rockstar #новостиигр #игры #gta"]
    return "\n".join(lines)


if __name__ == "__main__":
    from datetime import date
    sample = [
        {"title": "Подтверждена утечка саундтрека GTA 6 с новыми песнями Ланы Дель Рей", "img": None, "source": "PC Gamer"},
        {"title": "Sony выпустила обязательную прошивку для PS5 перед релизом GTA 6", "img": None, "source": "GameGPU"},
        {"title": "В Германии число заявок на отпуск перед выходом GTA 6 выросло на 38%", "img": None, "source": "PlayGround.ru"},
    ]
    out = sys.argv[1] if len(sys.argv) > 1 else "short.mp4"
    render_short(sample, date.today(), out)
    print(out)
