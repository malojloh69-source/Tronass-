#!/usr/bin/env python3
"""Tronaccs configurable tracker. Python 3.10+, standard library only."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import signal
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from filters import Catalog, DEFAULT_FILTERS, canonical, matches

API = "https://api.tronaccs.market"
SITE = "https://tronaccs.market"
ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("tron-rating")


class ServiceError(Exception):
    def __init__(self, message, code=0, retry_after=0, description=""):
        super().__init__(message)
        self.code = code
        self.retry_after = max(0, float(retry_after))
        self.description = description


class SchemaError(ServiceError):
    pass


def request_json(url, payload=None, timeout=30, files=None):
    headers = {"User-Agent": "TronRatingWatcher/2.0", "Accept": "application/json"}
    if url.startswith(API + "/"):
        headers.update({"Origin": SITE, "Referer": SITE + "/"})
    body = None
    if files:
        boundary = "tronaccs-" + uuid.uuid4().hex
        parts = []
        for key, value in (payload or {}).items():
            value = json.dumps(value) if isinstance(value, (dict, list, bool)) else str(value)
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        for key, path in files.items():
            path = Path(path)
            mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"; filename="{path.name}"\r\nContent-Type: {mime}\r\n\r\n'.encode()
                         + path.read_bytes() + b"\r\n")
        body = b"".join(parts) + f"--{boundary}--\r\n".encode()
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    elif payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload).encode()
    try:
        with urlopen(Request(url, data=body, headers=headers), timeout=timeout) as response:
            raw = response.read(8 * 1024 * 1024 + 1)
            if len(raw) > 8 * 1024 * 1024:
                raise SchemaError("Ответ сервиса слишком большой")
            return json.loads(raw)
    except HTTPError as exc:
        retry = exc.headers.get("Retry-After", "0")
        data = {}
        try:
            data = json.loads(exc.read(65536))
            retry = data.get("parameters", {}).get("retry_after", retry)
        except (ValueError, AttributeError):
            pass
        try:
            retry = float(retry)
        except (TypeError, ValueError):
            retry = 60 if exc.code == 429 else 0
        # Never log URLs: Telegram URLs contain the bot token.
        message = ("Tronaccs отклонил запрос (HTTP 403); проверьте доступ с вашего хостинга"
                   if exc.code == 403 and url.startswith(API + "/") else f"Сервис вернул HTTP {exc.code}")
        description = str(data.get("description", ""))[:300] if isinstance(data, dict) else ""
        raise ServiceError(message, exc.code, retry, description) from None
    except (URLError, TimeoutError, OSError):
        raise ServiceError("Ошибка сети или тайм-аут") from None
    except (ValueError, UnicodeError):
        raise SchemaError("Сервис вернул ответ не в формате JSON") from None


def integer(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise SchemaError(f"Неверное поле {field}")
    try:
        return int(value)
    except ValueError:
        raise SchemaError(f"Неверное поле {field}") from None


@dataclass(frozen=True)
class Listing:
    id: int
    title: str
    level: int
    price: str
    published: float
    country: str
    premium: bool
    attributes: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, raw):
        if not isinstance(raw, dict) or not isinstance(raw.get("telegram"), dict):
            raise SchemaError("В выдаче Telegram нет объекта telegram")
        tg = raw["telegram"]
        if "raiting_level" not in tg:
            raise SchemaError("На сайте изменилось поле telegram.raiting_level")
        item_id = integer(raw.get("id"), "id")
        if item_id <= 0:
            raise SchemaError("Неверный ID объявления")
        try:
            published = datetime.fromisoformat(raw["time_add"].replace("Z", "+00:00"))
            if published.tzinfo is None:
                raise ValueError()
            price = Decimal(str(raw["price"]))
            if not price.is_finite() or price < 0:
                raise ValueError()
        except (KeyError, TypeError, AttributeError, ValueError, InvalidOperation):
            raise SchemaError("Некорректная цена или дата объявления") from None
        country = tg.get("country") or {}
        attributes = {key: tg[key] for key in ("raiting_stars", "stars", "contacts", "dialogs", "channels", "chats",
                      "two_factor", "spamblock", "admin_channels_count", "admin_chats_count") if key in tg}
        attributes.update({"country_id": country.get("id"), "country_code": country.get("countryCode"),
                           "origin_id": (raw.get("item_origin") or {}).get("id"),
                           "seller": (raw.get("seller") or {}).get("username")})
        return cls(item_id, str(raw.get("title") or "Аккаунт Telegram")[:180],
                   integer(tg["raiting_level"], "raiting_level"), str(price),
                   published.timestamp(), str(country.get("name") or "Не указана")[:80],
                   tg.get("premium") in (1, "1", True), attributes)

    def message(self):
        price = f"{Decimal(self.price):,.2f}".replace(",", " ").rstrip("0").rstrip(".")
        return (f"🔔 <b>Новый аккаунт — рейтинг {self.level}</b>\n\n"
                f"{html.escape(self.title)}\n"
                f"⭐ Уровень рейтинга: <b>{self.level}</b>\n"
                f"💰 Цена продавца: <b>{price} ₽</b>\n"
                f"🌍 Страна: {html.escape(self.country)}\n"
                f"💎 Premium: {'Да' if self.premium else 'Нет'}\n\n"
                f"Объявление #{self.id}\n"
                "<i>Комиссия сайта в цену продавца не включена.</i>")

    def keyboard(self):
        return {"inline_keyboard": [[{"text": "Открыть объявление", "url": f"{SITE}/telegram/{self.id}"}],
                                    [{"text": "⚙️ Настройки", "callback_data": "nav:filters"}]]}


class Market:
    def __init__(self, max_pages=200, page_delay=1.0, transport=request_json):
        self.max_pages = max_pages
        self.page_delay = page_delay
        self.transport = transport

    def page(self, number, filters=None):
        # These are the same public endpoint, category and sort used by the website.
        params = {"category": "telegram", "page": number,
                  "orderBy": "time_add", "orderType": "DESC"}
        params.update(DEFAULT_FILTERS if filters is None else filters)
        payload = self.transport(API + "/items?" + urlencode(params))
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise SchemaError("На сайте изменилась структура списка объявлений")
        if not isinstance(payload.get("hasMore"), bool):
            raise SchemaError("На сайте изменилось поле пагинации hasMore")
        items = [Listing.parse(x) for x in payload["data"]]
        if payload["hasMore"] and not items:
            raise SchemaError("Сайт вернул пустую страницу с hasMore=true")
        if any(a.published < b.published for a, b in zip(items, items[1:])):
            raise SchemaError("Сайт не применил сортировку по дате: мониторинг приостановлен")
        return items, payload["hasMore"]

    def since(self, cutoff, stop, filters=None):
        found = {}
        fingerprints = set()
        newest = None
        for page in range(1, self.max_pages + 1):
            if stop.is_set():
                raise ServiceError("Остановка")
            items, more = self.page(page, filters)
            fingerprint = tuple(x.id for x in items)
            if items and fingerprint in fingerprints:
                raise SchemaError("Сайт повторяет одну страницу: проверьте пагинацию")
            fingerprints.add(fingerprint)
            for item in items:
                newest = max(newest or item.published, item.published)
                if item.published >= cutoff:
                    found[item.id] = item
            if not more or (items and items[-1].published < cutoff):
                return sorted(found.values(), key=lambda x: (x.published, x.id)), newest
            if stop.wait(self.page_delay):
                raise ServiceError("Остановка")
        # No checkpoint is advanced on partial scans. Increase max_scan_pages to catch up.
        raise ServiceError("Достигнут max_scan_pages; увеличьте его в config.json, чтобы обработать пропуск")


class State:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY, active INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS seen(id INTEGER PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS outbox(
                item_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, text TEXT NOT NULL,
                keyboard TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0, next_try REAL NOT NULL DEFAULT 0,
                PRIMARY KEY(item_id,chat_id));
            CREATE TABLE IF NOT EXISTS profiles(id TEXT PRIMARY KEY, filters TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS watch_seen(profile TEXT NOT NULL, id INTEGER NOT NULL, PRIMARY KEY(profile,id));
            CREATE TABLE IF NOT EXISTS ui(chat_id INTEGER PRIMARY KEY, message_id INTEGER, pending TEXT NOT NULL DEFAULT '{}');
        """)
        columns = {x[1] for x in self.db.execute("PRAGMA table_info(chats)")}
        with self.db:
            if "profile" not in columns:
                self.db.execute("ALTER TABLE chats ADD COLUMN profile TEXT NOT NULL DEFAULT ''")
            self.db.execute("INSERT OR IGNORE INTO profiles VALUES('',?)", (canonical(DEFAULT_FILTERS),))
            # Upgrade the previous database without replaying already observed listings.
            self.db.execute("INSERT OR IGNORE INTO watch_seen SELECT '',id FROM seen")

    @staticmethod
    def key(profile, name):
        return f"profile:{profile}:{name}" if profile else name

    def profile_filters(self, profile=""):
        with self.lock:
            row = self.db.execute("SELECT filters FROM profiles WHERE id=?", (profile,)).fetchone()
            return json.loads(row[0]) if row else dict(DEFAULT_FILTERS)

    def chat_profile(self, chat_id):
        with self.lock:
            row = self.db.execute("SELECT profile FROM chats WHERE id=?", (chat_id,)).fetchone()
            return row[0] if row else ""

    def chat_filters(self, chat_id):
        return self.profile_filters(self.chat_profile(chat_id))

    def set_filters(self, chat_id, filters):
        encoded = canonical(filters)
        profile = "" if encoded == canonical(DEFAULT_FILTERS) else hashlib.sha256(encoded.encode()).hexdigest()[:24]
        with self.lock, self.db:
            if self.chat_profile(chat_id) == profile:
                return profile
            self.db.execute("INSERT OR IGNORE INTO profiles VALUES(?,?)", (profile, encoded))
            if not self.db.execute("SELECT 1 FROM chats WHERE active=1 AND profile=?", (profile,)).fetchone():
                self.reset_profile_cursor(profile)
            self.db.execute("INSERT OR IGNORE INTO chats(id,active,profile) VALUES(?,0,?)", (chat_id, profile))
            self.db.execute("UPDATE chats SET profile=? WHERE id=?", (profile, chat_id))
            self.db.execute("DELETE FROM outbox WHERE chat_id=? AND sent=0", (chat_id,))
        return profile

    def reset_profile_cursor(self, profile):
        for name in ("baseline", "watermark", "last_success", "error"):
            self.db.execute("DELETE FROM settings WHERE key=?", (self.key(profile, name),))

    def active_profiles(self):
        with self.lock:
            return [x[0] for x in self.db.execute("SELECT DISTINCT profile FROM chats WHERE active=1 ORDER BY profile")]

    def ui_get(self, chat_id):
        with self.lock:
            row = self.db.execute("SELECT message_id,pending FROM ui WHERE chat_id=?", (chat_id,)).fetchone()
            return {"message_id": row[0], "pending": json.loads(row[1])} if row else {"message_id": None, "pending": {}}

    def ui_set(self, chat_id, message_id=None, pending=None):
        with self.lock, self.db:
            previous = self.ui_get(chat_id)
            self.db.execute("INSERT OR REPLACE INTO ui VALUES(?,?,?)", (chat_id,
                            previous["message_id"] if message_id is None else message_id,
                            json.dumps(previous["pending"] if pending is None else pending)))

    def get(self, key):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, str(value)))

    def subscribe(self, chat_id, active):
        with self.lock, self.db:
            was_active = self.is_active(chat_id)
            profile = self.chat_profile(chat_id)
            if active and not was_active and not self.db.execute(
                    "SELECT 1 FROM chats WHERE active=1 AND profile=? AND id<>?", (profile, chat_id)).fetchone():
                self.reset_profile_cursor(profile)
            self.db.execute("INSERT INTO chats(id,active) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET active=excluded.active",
                            (chat_id, int(active)))
            if not active:
                self.db.execute("DELETE FROM outbox WHERE chat_id=? AND sent=0", (chat_id,))

    def is_active(self, chat_id):
        with self.lock:
            row = self.db.execute("SELECT active FROM chats WHERE id=?", (chat_id,)).fetchone()
            return bool(row and row[0])

    def restrict_chats(self, allowed):
        if not allowed:
            return
        with self.lock:
            chats = self.db.execute("SELECT id FROM chats WHERE active=1").fetchall()
            for chat in chats:
                if chat[0] not in allowed:
                    self.subscribe(chat[0], False)

    def baseline(self, items, now=None, profile=""):
        cutoff = max((x.published for x in items), default=now if now is not None else time.time())
        with self.lock, self.db:
            self.db.executemany("INSERT OR IGNORE INTO watch_seen VALUES(?,?)", [(profile, x.id) for x in items])
            for key in ("baseline", "watermark"):
                self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (self.key(profile, key), str(cutoff)))

    def ingest(self, items, newest, profile="", catalog=None):
        # Outbox insertion, seen IDs and checkpoint commit together.
        with self.lock, self.db:
            saved_baseline = self.get(self.key(profile, "baseline"))
            if saved_baseline is None:
                # A subscription can be resumed while an earlier network scan is in flight.
                self.baseline(items, profile=profile)
                self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (self.key(profile, "last_success"), str(time.time())))
                return 0
            baseline = float(saved_baseline)
            filters = self.profile_filters(profile)
            chats = self.db.execute("SELECT id FROM chats WHERE active=1 AND profile=?", (profile,)).fetchall()
            count = 0
            for item in items:
                fresh = self.db.execute("INSERT OR IGNORE INTO watch_seen VALUES(?,?)", (profile, item.id)).rowcount
                if fresh and matches(item, filters, catalog) and item.published >= baseline:
                    for chat in chats:
                        self.db.execute("INSERT OR IGNORE INTO outbox(item_id,chat_id,text,keyboard) VALUES(?,?,?,?)",
                                        (item.id, chat[0], item.message(), json.dumps(item.keyboard())))
                    count += 1
            old = float(self.get(self.key(profile, "watermark")))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (self.key(profile, "watermark"), str(max(old, newest or old))))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (self.key(profile, "last_success"), str(time.time())))
            return count

    def pending(self):
        with self.lock:
            return self.db.execute("""SELECT o.* FROM outbox o JOIN chats c ON c.id=o.chat_id
                WHERE o.sent=0 AND c.active=1 AND o.next_try<=? ORDER BY o.rowid LIMIT 30""", (time.time(),)).fetchall()

    def sent(self, item_id, chat_id):
        with self.lock, self.db:
            self.db.execute("UPDATE outbox SET sent=1 WHERE item_id=? AND chat_id=?", (item_id, chat_id))

    def retry(self, row, delay):
        with self.lock, self.db:
            self.db.execute("UPDATE outbox SET attempts=attempts+1,next_try=? WHERE item_id=? AND chat_id=?",
                            (time.time() + delay, row["item_id"], row["chat_id"]))


class Telegram:
    def __init__(self, token, transport=request_json, banner=None):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.transport = transport
        self.banner = Path(banner or ROOT / "banner.jpg")
        self.photo_file_id = None

    def call(self, method, files=None, **payload):
        kwargs = {"timeout": 50}
        if files:
            kwargs["files"] = files
        data = self.transport(self.base + method, payload, **kwargs)
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise ServiceError("Telegram отклонил запрос", data.get("error_code", 0) if isinstance(data, dict) else 0,
                               data.get("parameters", {}).get("retry_after", 0) if isinstance(data, dict) else 0,
                               data.get("description", "") if isinstance(data, dict) else "")
        return data["result"]

    def send(self, chat_id, text, keyboard=None):
        payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML",
                   "link_preview_options": {"is_disabled": True}}
        if keyboard:
            payload["reply_markup"] = keyboard
        return self.call("sendMessage", **payload)

    def screen(self, chat_id, text, keyboard, message_id=None):
        payload = {"chat_id": chat_id, "caption": text, "parse_mode": "HTML", "reply_markup": keyboard}
        if message_id:
            try:
                return self.call("editMessageCaption", message_id=message_id, **payload)
            except ServiceError as exc:
                if exc.code == 400 and "not modified" in exc.description.lower():
                    return {"message_id": message_id}
                if exc.code != 400 or not any(x in exc.description.lower() for x in ("not found", "can't be edited", "cannot be edited", "no caption")):
                    raise
        if self.photo_file_id:
            try:
                return self.call("sendPhoto", photo=self.photo_file_id, **payload)
            except ServiceError as exc:
                if exc.code != 400:
                    raise
                self.photo_file_id = None
        result = self.call("sendPhoto", files={"photo": self.banner}, **payload)
        if result.get("photo"):
            self.photo_file_id = result["photo"][-1]["file_id"]
        return result


class Bot:
    def __init__(self, settings, state, market, telegram):
        self.cfg, self.state, self.market, self.tg = settings, state, market, telegram
        self.state.restrict_chats(settings["allowed_chat_ids"])
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.started = time.time()
        self.error = ""
        self.delivery_error = ""
        self.request_lock = threading.Lock()
        self.check_profiles = set()
        self.catalog = Catalog()
        from menus import Menus
        self.menus = Menus(self)

    def request_check(self, chat_id):
        with self.request_lock:
            self.check_profiles.add(self.state.chat_profile(chat_id))
        self.wake.set()

    def scan(self, profile=""):
        filters = self.catalog.validate(self.state.profile_filters(profile))
        key = lambda name: self.state.key(profile, name)
        if self.state.get(key("baseline")) is None:
            items, _ = self.market.page(1, filters)
            self.state.baseline(items, profile=profile)
            self.state.set(key("last_success"), time.time())
            LOG.info("Начальная точка сохранена; старые объявления не отправляются")
            return
        cutoff = max(float(self.state.get(key("baseline"))), float(self.state.get(key("watermark"))) - 300)
        items, newest = self.market.since(cutoff, self.stop, filters)
        count = self.state.ingest(items, newest, profile, self.catalog)
        LOG.info("Проверено объявлений: %s; новых по фильтрам: %s", len(items), count)

    def monitor(self):
        failures = 0
        while not self.stop.is_set():
            self.wake.clear()
            with self.request_lock:
                profiles = sorted(set(self.state.active_profiles()) | self.check_profiles)
                self.check_profiles.clear()
            errors = []
            retry_after = 0
            for index, profile in enumerate(profiles):
                if self.stop.is_set():
                    break
                try:
                    self.scan(profile)
                    self.state.set(self.state.key(profile, "error"), "")
                except (ServiceError, ValueError) as exc:
                    error = str(exc)
                    errors.append(error)
                    self.state.set(self.state.key(profile, "error"), error)
                    retry_after = max(retry_after, getattr(exc, "retry_after", 0))
                    LOG.warning("Проверка не выполнена: %s", error)
                    if getattr(exc, "code", 0) in (403, 429):
                        break
                except Exception:
                    errors.append("Внутренняя ошибка проверки")
                    self.state.set(self.state.key(profile, "error"), errors[-1])
                    LOG.error(errors[-1])
                if index + 1 < len(profiles) and self.stop.wait(1):
                    break
            self.error = errors[0] if errors else ""
            failures = failures + 1 if errors else 0
            delay = max(retry_after, min(600, self.cfg["poll_seconds"] * 2 ** min(failures, 5)))
            # A manual /check must never bypass a site's rate-limit/backoff window.
            if failures:
                self.stop.wait(delay)
            else:
                self.wake.wait(delay)

    def deliver_once(self):
        for row in self.state.pending():
            if self.stop.is_set():
                return
            # /stop could have removed the subscription after pending() was read.
            if not self.state.is_active(row["chat_id"]):
                continue
            try:
                if hasattr(self.tg, "screen"):
                    self.tg.screen(row["chat_id"], row["text"], json.loads(row["keyboard"]))
                else:
                    self.tg.send(row["chat_id"], row["text"], json.loads(row["keyboard"]))
                self.state.sent(row["item_id"], row["chat_id"])
                self.delivery_error = ""
            except ServiceError as exc:
                self.delivery_error = str(exc)
                if exc.code == 403:
                    self.state.subscribe(row["chat_id"], False)
                elif exc.code == 401:
                    LOG.error("Токен Telegram недействителен")
                    self.stop.set()
                    self.wake.set()
                    return
                else:
                    delay = max(exc.retry_after, min(3600, 10 * 2 ** min(row["attempts"], 8)))
                    self.state.retry(row, delay)
                    if exc.code == 429:
                        self.stop.wait(delay)
                        return
            if self.stop.wait(1.1):
                return

    def delivery(self):
        while not self.stop.is_set():
            try:
                self.deliver_once()
            except Exception:
                LOG.error("Ошибка очереди уведомлений; повтор через 5 секунд")
            self.stop.wait(5)

    def command(self, message):
        chat = message.get("chat", {})
        if chat.get("type") != "private":
            return
        chat_id = chat["id"]
        if self.cfg["allowed_chat_ids"] and chat_id not in self.cfg["allowed_chat_ids"]:
            self.menus.show(chat_id, "Этот бот доступен только владельцу.", [])
            return
        text = message.get("text", "").strip()
        command = text.split()[0].split("@")[0].lower() if text else ""
        if command.startswith("/"):
            self.state.ui_set(chat_id, pending={})
        if command == "/start":
            self.state.subscribe(chat_id, True)
            self.wake.set()
            self.state.ui_set(chat_id, pending={})
            self.menus.home(chat_id)
        elif command == "/stop":
            self.state.subscribe(chat_id, False)
            self.state.ui_set(chat_id, pending={})
            self.menus.home(chat_id)
        elif command == "/check":
            self.request_check(chat_id)
            self.menus.home(chat_id, "Проверка запрошена.")
        elif command == "/status":
            self.menus.status(chat_id)
        elif command in ("/settings", "/filters"):
            self.state.ui_set(chat_id, pending={})
            self.menus.filters(chat_id)
        elif command in ("/menu", "/cancel"):
            self.state.ui_set(chat_id, pending={})
            self.menus.home(chat_id)
        elif command == "/help":
            self.menus.help(chat_id)
        elif self.state.ui_get(chat_id)["pending"]:
            self.menus.input(chat_id, text)
        else:
            self.menus.home(chat_id)

    def callback(self, query):
        message = query.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        allowed = not self.cfg["allowed_chat_ids"] or chat_id in self.cfg["allowed_chat_ids"]
        if chat.get("type") != "private" or query.get("from", {}).get("id") != chat_id or not allowed:
            self.tg.call("answerCallbackQuery", callback_query_id=query["id"], text="Нет доступа")
            return
        self.tg.call("answerCallbackQuery", callback_query_id=query["id"])
        # Buttons on earlier notifications may also open the settings menu.
        self.state.ui_set(chat_id, message_id=message["message_id"])
        self.menus.callback(chat_id, query.get("data", ""))

    def polling(self):
        offset = int(self.state.get("telegram_offset") or 0)
        while not self.stop.is_set():
            try:
                updates = self.tg.call("getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"])
                for update in updates:
                    if "message" in update:
                        self.command(update["message"])
                    elif "callback_query" in update:
                        self.callback(update["callback_query"])
                    offset = update["update_id"] + 1
                    self.state.set("telegram_offset", offset)
            except ServiceError as exc:
                if exc.code in (401, 409):
                    LOG.error("Недействительный токен или уже запущена другая копия бота (HTTP %s)", exc.code)
                    self.stop.set()
                    self.wake.set()
                    return
                LOG.warning("Telegram временно недоступен (HTTP %s)", exc.code)
                self.stop.wait(max(5, exc.retry_after))
            except Exception:
                LOG.error("Ошибка обработки команды; повтор через 5 секунд")
                self.stop.wait(5)


def load_config():
    cfg = {"bot_token": "", "poll_seconds": 30, "max_scan_pages": 200,
           "allowed_chat_ids": [], "database": "state.sqlite3", "port": 3000}
    path = ROOT / "config.json"
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    cfg["bot_token"] = os.getenv("BOT_TOKEN") or cfg["bot_token"]
    cfg["port"] = int(os.getenv("PORT") or cfg["port"])
    cfg["poll_seconds"] = int(cfg["poll_seconds"])
    cfg["max_scan_pages"] = int(cfg["max_scan_pages"])
    cfg["allowed_chat_ids"] = [int(x) for x in cfg["allowed_chat_ids"]]
    if cfg["poll_seconds"] < 15 or cfg["max_scan_pages"] < 1:
        raise ValueError("poll_seconds должен быть >=15, max_scan_pages >=1")
    if not 0 <= cfg["port"] <= 65535:
        raise ValueError("Некорректный порт")
    return cfg


def serve_health(bot):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/health"):
                self.send_error(404)
                return
            profiles = bot.state.active_profiles()
            checks = [bot.state.get(bot.state.key(profile, "last_success")) for profile in profiles]
            healthy = not bot.stop.is_set() and not bot.error and all(
                stamp and time.time() - float(stamp) < max(180, bot.cfg["poll_seconds"] * 4) for stamp in checks)
            body = json.dumps({"running": not bot.stop.is_set(), "market_ok": bool(profiles) and healthy}).encode()
            self.send_response(200 if self.path == "/" or healthy else 503)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", bot.cfg["port"]), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    parser = argparse.ArgumentParser(description="Tronaccs — уведомления о новых аккаунтах с рейтингом 2+")
    parser.add_argument("--check-source", action="store_true", help="Проверить сайт без токена и отправки сообщений")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = load_config()
        market = Market(cfg["max_scan_pages"])
        if args.check_source:
            items, more = market.page(1)
            matches = [x for x in items if x.level >= 2]
            print(json.dumps({"source_ok": True, "items_checked": len(items), "has_more": more,
                              "matching_on_this_page": [{"id": x.id, "level": x.level, "price": x.price} for x in matches]}, ensure_ascii=False, indent=2))
            return
        if not cfg["bot_token"] or cfg["bot_token"] == "PASTE_BOT_TOKEN_HERE":
            raise ValueError("Укажите bot_token в config.json или переменную BOT_TOKEN")
        state = State(ROOT / cfg["database"])
        telegram = Telegram(cfg["bot_token"])
        telegram.call("getMe")
        hook = telegram.call("getWebhookInfo")
        if hook.get("url"):
            raise ValueError("У бота включён webhook. Используйте отдельного бота или отключите webhook перед запуском")
        bot = Bot(cfg, state, market, telegram)
        server = serve_health(bot) if cfg["port"] else None

        def shutdown(*_):
            bot.stop.set()
            bot.wake.set()

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)
        threads = [threading.Thread(target=bot.monitor, daemon=True),
                   threading.Thread(target=bot.delivery, daemon=True),
                   threading.Thread(target=bot.polling, daemon=True)]
        for worker in threads:
            worker.start()
        LOG.info("Бот запущен. Отправьте ему /start")
        while not bot.stop.wait(1):
            pass
        if server:
            server.shutdown()
    except (ServiceError, ValueError, OSError, sqlite3.Error) as exc:
        # These messages are our own messages or local file errors, never token URLs.
        LOG.error("Запуск не выполнен: %s", exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

