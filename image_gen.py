"""
image_gen.py — динамическая генерация PNG-картинок для MemTrace через Pillow.

Два генератора:
  1. render_top_image(entries, lang)      -> BytesIO (PNG)
     Топ пользователей по количеству приглашённых рефералов.
  2. render_subscription_image(...)       -> BytesIO (PNG)
     Круговой таймер "сколько осталось подписки".

Оба используют общую "шапку" (лого MemTrace + бумажный самолётик + тэглайн +
разделитель), вырезанную один раз из присланных дизайнов и сохранённую в
assets/header.png — так итоговые картинки визуально совпадают с макетом,
а весь контент под шапкой рисуется программно и поэтому:
  - в топе может быть от 1 до 10 строк (а не жёстко 3, как в статичном макете);
  - кольцо таймера рисуется под реальный остаток подписки, а не под
    захардкоженные 12д/08ч/34м с картинки.

Использование см. в конце файла (if __name__ == "__main__") и в
chat_automation_bot.py (функции _send_top_image / _send_subscription_image).
"""

from __future__ import annotations

import io
import math
import os
from dataclasses import dataclass
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont

# ---------------------------------------------------------------------------
# Пути / ассеты
# ---------------------------------------------------------------------------

ASSETS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
HEADER_PATH = os.path.join(ASSETS_DIR, "header.png")

FONT_DIR = os.path.join(ASSETS_DIR, "fonts")
FONT_BOLD = os.path.join(FONT_DIR, "DejaVuSans-Bold.ttf")
FONT_REGULAR = os.path.join(FONT_DIR, "DejaVuSans.ttf")

# Запасные системные пути на случай, если папку assets/fonts не скопировали
# вместе со скриптом (Linux / macOS / Windows).
_FALLBACK_FONT_PATHS = {
    FONT_BOLD: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "C:\\Windows\\Fonts\\arialbd.ttf",
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    ],
    FONT_REGULAR: [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:\\Windows\\Fonts\\arial.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ],
}

# ---------------------------------------------------------------------------
# Палитра (взята пипеткой из присланных макетов)
# ---------------------------------------------------------------------------

BLACK = (0, 0, 0)
BG = (5, 5, 6)
PANEL_FILL = (13, 13, 14)
PANEL_BORDER = (54, 54, 56)
PANEL_BORDER_ACCENT = (229, 25, 25)
WHITE = (240, 240, 240)
GRAY = (150, 150, 152)
DIM_GRAY = (110, 110, 112)
RED = (229, 20, 20)
RED_SOFT = (180, 20, 20)

CANVAS_W = 1672

_font_cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _font(path: str, size: int) -> ImageFont.FreeTypeFont:
    key = (path, size)
    if key in _font_cache:
        return _font_cache[key]

    candidates = [path] + _FALLBACK_FONT_PATHS.get(path, [])
    last_error: Optional[Exception] = None
    for candidate in candidates:
        try:
            font = ImageFont.truetype(candidate, size)
            _font_cache[key] = font
            return font
        except Exception as exc:  # файл не найден / битый шрифт -- пробуем следующий
            last_error = exc
            continue

    raise FileNotFoundError(
        f"Не удалось найти ни один шрифт из {candidates}. "
        f"Убедись, что папка assets/fonts лежит рядом со скриптом. "
        f"Последняя ошибка: {last_error}"
    )


def _load_header() -> Image.Image:
    if os.path.exists(HEADER_PATH):
        return Image.open(HEADER_PATH).convert("RGB")
    # запасной вариант, если ассет не найден -- просто чёрная плашка нужной высоты
    return Image.new("RGB", (CANVAS_W, 360), BG)


# ---------------------------------------------------------------------------
# Текстовые хелперы
# ---------------------------------------------------------------------------

def _tracked_text(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill,
    tracking: int = 0,
    anchor_center_x: Optional[float] = None,
) -> None:
    """Рисует текст с увеличенным межбуквенным интервалом (как в лого MEMTRACE)."""
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + tracking * max(0, len(text) - 1)
    x, y = xy
    if anchor_center_x is not None:
        x = anchor_center_x - total / 2
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=fill)
        x += w + tracking


def _text_center(
    draw: ImageDraw.ImageDraw, center_x: float, y: float, text: str,
    font: ImageFont.FreeTypeFont, fill, anchor: str = "ma",
) -> None:
    draw.text((center_x, y), text, font=font, fill=fill, anchor=anchor)


def _glow(base: Image.Image, mask_layer: Image.Image, color, blur: int, alpha: int = 160) -> Image.Image:
    """Добавляет мягкое свечение вокруг непрозрачных пикселей mask_layer поверх base."""
    glow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    solid = Image.new("RGBA", base.size, color + (alpha,))
    glow_layer = Image.composite(solid, glow_layer, mask_layer)
    glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(blur))
    return Image.alpha_composite(base.convert("RGBA"), glow_layer).convert("RGB")


def _rounded_rect(draw, box, radius, outline, width=2, fill=None):
    draw.rounded_rectangle(box, radius=radius, outline=outline, width=width, fill=fill)


def _initials(name: str) -> str:
    parts = [p for p in name.replace("@", "").split() if p]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[1][0]).upper()


def _avatar_color(seed: int) -> tuple[int, int, int]:
    palette = [
        (176, 38, 38), (38, 92, 176), (38, 150, 100),
        (150, 100, 30), (120, 60, 170), (30, 140, 150),
    ]
    return palette[seed % len(palette)]


def _circle_avatar(
    size: int, photo: Optional[Image.Image], initials: str, seed: int, ring_color,
) -> Image.Image:
    """Круглый аватар: либо фото пользователя (кроп в круг), либо инициалы на цветной заливке."""
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)

    if photo is not None:
        photo = photo.convert("RGB")
        w, h = photo.size
        side = min(w, h)
        photo = photo.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        photo = photo.resize((size, size), Image.LANCZOS)
        out.paste(photo, (0, 0), mask)
    else:
        fill_img = Image.new("RGBA", (size, size), _avatar_color(seed) + (255,))
        out.paste(fill_img, (0, 0), mask)
        d = ImageDraw.Draw(out)
        f = _font(FONT_BOLD, int(size * 0.38))
        d.text((size / 2, size / 2), initials, font=f, fill=WHITE, anchor="mm")

    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(ring).ellipse((2, 2, size - 2, size - 2), outline=ring_color, width=4)
    out = Image.alpha_composite(out, ring)
    return out


# ---------------------------------------------------------------------------
# 1) Топ приглашающих
# ---------------------------------------------------------------------------

@dataclass
class TopEntry:
    rank: int
    name: str
    username: Optional[str]
    invited: int
    photo: Optional[Image.Image] = None  # уже открытое Pillow-изображение аватарки, если есть


TOP_TITLE = {"ru": "ТОП ПОЛЬЗОВАТЕЛЕЙ ПО ПРИГЛАШЕНИЯМ", "en": "TOP USERS BY INVITES"}
TOP_FOOTER_1 = {"ru": "ПРИГЛАШАЙ ДРУЗЕЙ И ПОДНИМАЙСЯ В ТОП.", "en": "INVITE FRIENDS AND CLIMB THE TOP."}
TOP_FOOTER_2 = {"ru": "ТВОЁ МЕСТО ЖДЁТ ТЕБЯ.", "en": "YOUR SPOT IS WAITING."}
TOP_SUFFIX = {"ru": "приглашений", "en": "invitations"}
TOP_EMPTY = {"ru": "Пока никто никого не пригласил.\nБудь первым!", "en": "Nobody has invited anyone yet.\nBe the first!"}


def render_top_image(entries: list[TopEntry], lang: str = "ru") -> io.BytesIO:
    row_h = 118
    row_gap = 16
    top_pad = 82   # шапка -> заголовок раздела
    title_h = 56
    bottom_pad = 130

    n = max(1, len(entries))
    content_h = title_h + n * row_h + (n - 1) * row_gap
    header = _load_header()
    canvas_h = header.height + top_pad + content_h + bottom_pad
    img = Image.new("RGB", (CANVAS_W, canvas_h), BG)
    img.paste(header, (0, 0))
    draw = ImageDraw.Draw(img)

    cx = CANVAS_W // 2
    y = header.height + top_pad

    f_title = _font(FONT_BOLD, 24)
    _tracked_text(draw, (0, y), TOP_TITLE[lang], f_title, GRAY, tracking=6, anchor_center_x=cx)
    y += title_h

    margin_x = 248
    box_w = CANVAS_W - 2 * margin_x

    if not entries:
        f_empty = _font(FONT_REGULAR, 28)
        for i, line in enumerate(TOP_EMPTY[lang].split("\n")):
            draw.text((cx, y + 40 + i * 40), line, font=f_empty, fill=GRAY, anchor="mm")
        y += row_h
    else:
        f_rank = _font(FONT_BOLD, 40)
        f_name = _font(FONT_BOLD, 26)
        f_user = _font(FONT_REGULAR, 20)
        f_count = _font(FONT_BOLD, 34)
        f_suffix = _font(FONT_REGULAR, 18)
        medal_colors = {1: RED, 2: (225, 225, 225), 3: (196, 132, 60)}

        for entry in entries:
            box = (margin_x, y, margin_x + box_w, y + row_h)
            border_color = PANEL_BORDER_ACCENT if entry.rank == 1 else PANEL_BORDER
            _rounded_rect(draw, box, radius=18, outline=border_color, width=2, fill=PANEL_FILL)

            rank_color = medal_colors.get(entry.rank, WHITE)
            draw.text((margin_x + 34, y + row_h / 2), str(entry.rank), font=f_rank,
                       fill=rank_color, anchor="lm")

            avatar_size = 78
            avatar_x = margin_x + 118
            avatar_y = y + (row_h - avatar_size) // 2
            ring_color = RED if entry.rank == 1 else (120, 120, 122)
            avatar = _circle_avatar(avatar_size, entry.photo, _initials(entry.name), entry.rank, ring_color)
            img.paste(avatar, (avatar_x, avatar_y), avatar)

            text_x = avatar_x + avatar_size + 28
            draw.text((text_x, y + row_h / 2 - 22), entry.name, font=f_name, fill=WHITE, anchor="lm")
            handle = f"@{entry.username}" if entry.username else "—"
            draw.text((text_x, y + row_h / 2 + 14), handle, font=f_user, fill=GRAY, anchor="lm")

            count_x = margin_x + box_w - 34
            draw.text((count_x, y + row_h / 2 - 20), f"{entry.invited:,}".replace(",", " "),
                       font=f_count, fill=RED, anchor="rm")
            draw.text((count_x, y + row_h / 2 + 20), TOP_SUFFIX[lang], font=f_suffix, fill=GRAY, anchor="rm")

            y += row_h + row_gap
        y -= row_gap

    y += 44
    f_footer1 = _font(FONT_REGULAR, 20)
    f_footer2 = _font(FONT_BOLD, 20)
    _tracked_text(draw, (0, y), TOP_FOOTER_1[lang], f_footer1, GRAY, tracking=4, anchor_center_x=cx)
    _tracked_text(draw, (0, y + 34), TOP_FOOTER_2[lang], f_footer2, RED, tracking=4, anchor_center_x=cx)

    buf = io.BytesIO()
    buf.name = "top.png"
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# 2) Остаток подписки (круговой таймер)
# ---------------------------------------------------------------------------

SUB_TITLE = {"ru": "ДО КОНЦА ПОДПИСКИ ОСТАЛОСЬ:", "en": "SUBSCRIPTION TIME LEFT:"}
SUB_EXPIRED_TITLE = {"ru": "ПОДПИСКА ЗАКОНЧИЛАСЬ", "en": "SUBSCRIPTION EXPIRED"}
SUB_LABELS = {
    "ru": ("ДНЕЙ", "ЧАСОВ", "МИНУТ"),
    "en": ("DAYS", "HOURS", "MINUTES"),
}
SUB_ACTIVE_TEXT = {
    "ru": ("ПОДПИСКА АКТИВНА. ПРОДЛИ, ЧТОБЫ СОХРАНИТЬ ДОСТУП", "КО ВСЕМ ФУНКЦИЯМ БОТА."),
    "en": ("SUBSCRIPTION IS ACTIVE. RENEW TO KEEP ACCESS", "TO ALL BOT FEATURES."),
}
SUB_EXPIRED_TEXT = {
    "ru": ("ПОДПИСКА ЗАКОНЧИЛАСЬ. ОФОРМИ НОВУЮ, ЧТОБЫ ВЕРНУТЬ", "ДОСТУП КО ВСЕМ ФУНКЦИЯМ БОТА."),
    "en": ("YOUR SUBSCRIPTION HAS EXPIRED. RENEW TO GET BACK", "ACCESS TO ALL BOT FEATURES."),
}


def render_subscription_image(
    days: int,
    hours: int,
    minutes: int,
    fraction_remaining: float,
    lang: str = "ru",
    active: bool = True,
) -> io.BytesIO:
    """
    fraction_remaining: доля оставшегося времени от полного цикла подписки
    (0.0 - только что закончилась, 1.0 - только что продлена на полный срок).
    Используется, чтобы нарисовать красную дугу правильной длины.
    """
    fraction_remaining = max(0.0, min(1.0, fraction_remaining))

    header = _load_header()
    top_pad = 78
    title_h = 50
    ring_size = 560
    ring_pad_top = 40
    bottom_pad_after_ring = 56
    banner_h = 118
    bottom_margin = 60

    canvas_h = (
        header.height + top_pad + title_h + ring_pad_top + ring_size
        + bottom_pad_after_ring + banner_h + bottom_margin
    )
    img = Image.new("RGB", (CANVAS_W, canvas_h), BG)
    img.paste(header, (0, 0))
    draw = ImageDraw.Draw(img)
    cx = CANVAS_W // 2

    y = header.height + top_pad
    f_title = _font(FONT_BOLD, 24)
    title = SUB_TITLE[lang] if active else SUB_EXPIRED_TITLE[lang]
    _tracked_text(draw, (0, y), title, f_title, GRAY if active else RED, tracking=6, anchor_center_x=cx)
    y += title_h + ring_pad_top

    # --- кольцо ---
    ring_layer = Image.new("RGBA", (ring_size, ring_size), (0, 0, 0, 0))
    ring_draw = ImageDraw.Draw(ring_layer)
    track_w = 14
    pad = track_w
    bbox = (pad, pad, ring_size - pad, ring_size - pad)

    # тёмная подложка кольца (полный круг)
    ring_draw.ellipse(bbox, outline=(40, 12, 12), width=track_w)

    # красная дуга остатка, старт сверху (12 часов), по часовой стрелке
    if fraction_remaining > 0.003:
        start_angle = -90
        end_angle = start_angle + 360 * fraction_remaining
        ring_draw.arc(bbox, start=start_angle, end=end_angle, fill=RED, width=track_w)
        # маленькая точка-акцент на конце дуги (граница красный/тёмный сегмент)
        rad = math.radians(end_angle)
        rcx, rcy, rr = ring_size / 2, ring_size / 2, ring_size / 2 - pad
        dot_x, dot_y = rcx + rr * math.cos(rad), rcy + rr * math.sin(rad)
        dot_r = track_w / 2 + 2
        ring_draw.ellipse((dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r), fill=RED)

    glow = ring_layer.filter(ImageFilter.GaussianBlur(10))
    ring_x = cx - ring_size // 2
    img.paste(Image.new("RGB", (ring_size, ring_size), BG), (ring_x, y))
    glow_rgba = Image.new("RGBA", img.size, (0, 0, 0, 0))
    glow_rgba.paste(glow, (ring_x, y), glow)
    img = Image.alpha_composite(img.convert("RGBA"), glow_rgba).convert("RGB")
    img.paste(ring_layer, (ring_x, y), ring_layer)
    draw = ImageDraw.Draw(img)

    # тёмный диск в центре кольца (как в макете)
    inner_pad = track_w + 18
    inner_box = (ring_x + inner_pad, y + inner_pad, ring_x + ring_size - inner_pad, y + ring_size - inner_pad)
    draw.ellipse(inner_box, fill=(9, 9, 10))

    # --- цифры внутри кольца ---
    labels = SUB_LABELS[lang]
    values = [days, hours, minutes] if active else [0, 0, 0]
    f_num = _font(FONT_BOLD, 62)
    f_lbl = _font(FONT_BOLD, 20)
    center_y = y + ring_size / 2
    block_h = 108
    start_y = center_y - block_h  # три блока, средний по центру
    for i, (val, lbl) in enumerate(zip(values, labels)):
        by = start_y + i * block_h + block_h / 2
        num_str = f"{val:02d}" if i > 0 else str(val)
        draw.text((cx, by - 14), num_str, font=f_num, fill=WHITE, anchor="mm")
        lbl_color = RED if i == 0 else GRAY
        _tracked_text(draw, (0, by + 26), lbl, f_lbl, lbl_color, tracking=5, anchor_center_x=cx)

    y += ring_size + bottom_pad_after_ring

    # --- нижний баннер-статус ---
    banner_box = (140, y, CANVAS_W - 140, y + banner_h)
    _rounded_rect(draw, banner_box, radius=16, outline=PANEL_BORDER, width=2, fill=PANEL_FILL)
    icon_cx = 140 + 60
    icon_cy = y + banner_h / 2
    icon_color = RED if active else DIM_GRAY
    draw.regular_polygon((icon_cx, icon_cy, 22), n_sides=3, rotation=0, fill=icon_color)

    line1, line2 = SUB_ACTIVE_TEXT[lang] if active else SUB_EXPIRED_TEXT[lang]
    f_banner = _font(FONT_REGULAR, 19)
    text_x = 140 + 110
    draw.text((text_x, icon_cy - 16), line1, font=f_banner, fill=GRAY, anchor="lm")
    draw.text((text_x, icon_cy + 16), line2, font=f_banner, fill=GRAY, anchor="lm")

    buf = io.BytesIO()
    buf.name = "subscription.png"
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ---------------------------------------------------------------------------
# Локальный прогон для проверки (python3 image_gen.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    demo_entries = [
        TopEntry(1, "Имя Пользователя", "username", 1245),
        TopEntry(2, "Anna K", "anna_k", 987),
        TopEntry(3, "Имя Пользователя", None, 532),
        TopEntry(4, "Ivan Petrov", "ivan_p", 210),
    ]
    with open("out_top.png", "wb") as f:
        f.write(render_top_image(demo_entries, "ru").read())
    with open("out_top_empty.png", "wb") as f:
        f.write(render_top_image([], "ru").read())

    with open("out_sub_active.png", "wb") as f:
        f.write(render_subscription_image(12, 8, 34, fraction_remaining=0.85, lang="ru", active=True).read())
    with open("out_sub_low.png", "wb") as f:
        f.write(render_subscription_image(0, 2, 5, fraction_remaining=0.03, lang="ru", active=True).read())
    with open("out_sub_expired.png", "wb") as f:
        f.write(render_subscription_image(0, 0, 0, fraction_remaining=0.0, lang="ru", active=False).read())
    print("done")
