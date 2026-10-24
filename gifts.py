"""Read public Tronaccs gift lists without inferring gifts from their counts."""
from __future__ import annotations

import json
import logging
import re
import time
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
ASSETS = "https://raw.githubusercontent.com/ssamy2/TelegramGiftsAssests/main"
CATALOG_URL = ASSETS + "/Gifts_Details.json"
LOG = logging.getLogger("tron-rating")
SLUG = re.compile(r"[A-Za-z][A-Za-z0-9]{0,80}-[1-9][0-9]{0,11}\Z")
GIFT_ID = re.compile(r"[0-9]{1,22}\Z")


def clean_text(value, limit=160):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def gift_id(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    text = str(value)
    return text if GIFT_ID.fullmatch(text) else ""


def positive(value):
    return not isinstance(value, bool) and isinstance(value, (int, str)) and str(value).isdigit() and int(value) > 0


def nonnegative(value):
    return not isinstance(value, bool) and isinstance(value, (int, str)) and str(value).isdigit()


def safe_photo_url(value):
    if not isinstance(value, str) or len(value) > 600:
        return ""
    try:
        url = urlsplit(value)
        if url.scheme != "https" or url.username or url.password or url.port not in (None, 443):
            return ""
        host = url.hostname
        allowed = ((host == "nft.fragment.com" and url.path.startswith("/gift/")) or
                   (host == "fragment.com" and url.path.startswith("/file/gifts/")) or
                   (host == "raw.githubusercontent.com" and url.path.startswith("/ssamy2/TelegramGiftsAssests/main/")) or
                   (host == "tronaccs.market" and url.path.startswith("/tgs/")))
        if allowed and ".." not in url.path and not url.query and not url.fragment:
            return value
    except ValueError:
        pass
    return ""


@lru_cache(maxsize=1)
def bundled_gifts():
    try:
        data = json.loads((ROOT / "gift_catalog.json").read_text(encoding="utf-8"))
        return data.get("gifts", {})
    except (OSError, ValueError, AttributeError):
        return {}


def catalog_entries(payload):
    """The public catalogue distinguishes existing collections and waiting limited gifts."""
    entries = {}
    if not isinstance(payload, dict):
        return entries
    for group, status in (("unupgraded", "pending"), ("upgraded", "available"), ("regular_gifts", "ordinary")):
        values = payload.get(group)
        if not isinstance(values, list):
            continue
        for row in values[:5000]:
            if not isinstance(row, dict):
                continue
            ident = gift_id(row.get("regular_id") or row.get("id"))
            title = clean_text(row.get("full_name"))
            if ident and title:
                entries[ident] = {"title": title, "status": status}
    return entries


class GiftCatalog:
    def __init__(self, fetch=None, telegram=None, state=None):
        self.entries = {key: dict(value) for key, value in bundled_gifts().items()}
        self.fetch, self.telegram, self.state = fetch, telegram, state
        self.next_refresh = 0
        if state:
            try:
                saved = json.loads(state.get("gift_catalog_cache") or "{}")
                if isinstance(saved, dict):
                    self.entries.update({key: value for key, value in saved.items()
                                         if gift_id(key) and isinstance(value, dict)})
            except (TypeError, ValueError):
                pass

    def refresh(self):
        if time.monotonic() < self.next_refresh or not (self.fetch or self.telegram):
            return
        self.next_refresh = time.monotonic() + 3600
        if self.fetch:
            try:
                fresh = catalog_entries(self.fetch(CATALOG_URL))
                if fresh:
                    # Keep previously seen sold-out gifts as well as new entries.
                    for ident, entry in fresh.items():
                        self.entries[ident] = {**self.entries.get(ident, {}), **entry}
            except Exception:
                # Catalogue outages must not suppress a new listing or expose token URLs.
                LOG.warning("Каталог названий подарков недоступен; используется сохранённая копия")
                self.next_refresh = time.monotonic() + 600
        if self.telegram:
            try:
                data = self.telegram("getAvailableGifts")
                for row in data.get("gifts", []):
                    ident = gift_id(row.get("id"))
                    if not ident:
                        continue
                    entry = self.entries.setdefault(ident, {})
                    if nonnegative(row.get("upgrade_star_count")):
                        entry["status"] = "available"
                    elif positive(row.get("total_count")):
                        entry["status"] = "pending"
                    else:
                        entry["status"] = "ordinary"
                    sticker = row.get("sticker") or {}
                    thumb = sticker.get("thumbnail") or {}
                    file_id = thumb.get("file_id")
                    if isinstance(file_id, str) and len(file_id) <= 512:
                        entry["thumbnail_file_id"] = file_id
            except Exception as exc:
                LOG.warning("Каталог улучшений Telegram недоступен; используется сохранённая копия")
                self.next_refresh = time.monotonic() + max(600, getattr(exc, "retry_after", 0))
        if self.state:
            self.state.set("gift_catalog_cache", json.dumps(self.entries, ensure_ascii=False))


def read_list(value):
    if isinstance(value, str) and len(value) <= 1024 * 1024:
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, list) else None


def regular_gift(row, entries):
    if isinstance(row, list) and row:
        data = row[0] if isinstance(row[0], dict) else {"id": row[0]}
    elif isinstance(row, dict):
        data = row.get("gift", row)
        if not isinstance(data, dict):
            return None
        data = {**row, **data}
    else:
        return None
    ident = gift_id(data.get("id") or data.get("gift_id"))
    known = entries.get(ident, {})
    status = known.get("status", "unknown")
    if data.get("can_upgrade") is True or data.get("can_upgrade") == 1:
        status = "available"
    elif data.get("can_upgrade") is False or data.get("can_upgrade") == 0:
        status = "ordinary" if status == "ordinary" else "unavailable"
    elif nonnegative(data.get("upgrade_stars")) or nonnegative(data.get("upgrade_star_count")):
        status = "available"
    elif data.get("limited") is True or positive(data.get("total_count")) or positive(data.get("availability_total")):
        status = "pending"
    if status == "ordinary":
        return {"excluded": True}
    title = clean_text(data.get("title") or data.get("name") or known.get("title"))
    if not title and not ident:
        return None
    return {"kind": "regular", "key": "regular:" + (ident or title),
            "title": title or f"Подарок {ident} (название не указано)", "status": status,
            "photo": safe_photo_url(data.get("photo_url") or data.get("image_url")) or
                     (ASSETS + f"/webp/by_id/{ident}.webp" if ident else ""),
            "thumbnail_file_id": known.get("thumbnail_file_id", ""), "count": 1}


def unique_gift(row, entries):
    data = row
    if isinstance(row, list) and row:
        data = row[0]
    if isinstance(data, list) and len(data) >= 3:
        title, number, slug = data[:3]
        data = {"title": title, "number": number, "slug": slug}
    elif isinstance(data, str):
        data = {"slug": data} if SLUG.fullmatch(data) else {"gift_id": data}
    if not isinstance(data, dict):
        return None
    data = data.get("gift", data)
    if not isinstance(data, dict):
        return None
    slug = data.get("slug") or data.get("name") or ""
    slug = slug if isinstance(slug, str) and SLUG.fullmatch(slug) else ""
    ident = gift_id(data.get("gift_id") or data.get("id"))
    known = entries.get(ident, {})
    title = clean_text(data.get("title") or data.get("base_name") or known.get("title"))
    number = data.get("number", data.get("num"))
    if slug:
        short, slug_number = slug.rsplit("-", 1)
        if not title:
            title = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", short)
            for entry in entries.values():
                name = clean_text(entry.get("title"))
                if re.sub(r"[^a-z0-9]", "", name.lower()) == short.lower():
                    title = name
                    break
        title += " #" + slug_number
        return {"kind": "nft", "key": "nft:" + slug.lower(), "title": title,
                "photo": f"https://nft.fragment.com/gift/{slug.lower()}.medium.jpg", "count": 1}
    if title and positive(number):
        title += f" #{number}"
    if not title and not ident:
        return None
    image = safe_photo_url(data.get("photo_url") or data.get("image_url"))
    return {"kind": "nft", "key": "legacy:" + (ident or title),
            "title": title or "NFT-подарок (название не указано)",
            "photo": image or (ASSETS + f"/webp/by_id/{ident}.webp" if ident else ""),
            "collection_preview": bool(ident and not image), "count": 1}


def gift_details(telegram, catalog=None):
    entries = catalog.entries if catalog else bundled_gifts()
    parts, positions, unknown = [], {}, False
    for key, count_key, parse in (("gifts", "gifts_count", regular_gift), ("unique_gifts", "unique_gifts_count", unique_gift)):
        rows = read_list(telegram.get(key))
        if rows is None:
            unknown = unknown or positive(telegram.get(count_key))
            continue
        represented = 0
        for row in rows:
            gift = parse(row, entries)
            if not gift:
                unknown = True
                continue
            represented += 1
            if gift.get("excluded"):
                continue
            if gift["key"] in positions:
                # Ordinary duplicates are separate gifts, not a Stars balance or a quantity field.
                if gift["kind"] == "regular" or gift.get("collection_preview"):
                    parts[positions[gift["key"]]]["count"] += 1
                continue
            positions[gift["key"]] = len(parts)
            parts.append(gift)
        total = telegram.get(count_key)
        if positive(total) and represented < int(total):
            unknown = True
    if unknown:
        parts.append({"kind": "notice", "text": "Tronaccs не передал полный список подарков. Названия и фото неуказанных подарков недоступны."})
    return parts


def gift_caption(part, item_id):
    import html
    title = html.escape(clean_text(part.get("title"), 180))
    quantity = f" ×{part['count']}" if part.get("count", 1) > 1 else ""
    lines = [f"Объявление #{item_id}", f"<b>{title}{quantity}</b>"]
    if part["kind"] == "nft":
        lines.append("NFT-подарок")
        if part.get("collection_preview"):
            lines.append("Изображение коллекции: точный вариант подарка не указан площадкой.")
    else:
        lines.append({"available": "Обычный подарок — можно улучшить до NFT",
                      "pending": "Лимитированный подарок — улучшение пока недоступно; срок не объявлен",
                      "unavailable": "Обычный подарок — улучшение сейчас недоступно для этого экземпляра"}.get(
                          part.get("status"), "Обычный подарок — статус улучшения неизвестен"))
    return "\n".join(lines)
