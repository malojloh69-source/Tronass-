"""Settings, migration, menu, query and photo transport integration tests."""
import json
import io
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from filters import Catalog, DEFAULT_FILTERS, matches
from main import Bot, Listing, Market, ServiceError, State, Telegram, request_json
from test_bot import raw


class PhotoTelegram:
    def __init__(self):
        self.screens = []
        self.calls = []

    def screen(self, chat_id, text, keyboard, message_id=None):
        self.screens.append({"chat_id": chat_id, "caption": text, "keyboard": keyboard, "message_id": message_id})
        return {"message_id": message_id or len(self.screens)}

    def call(self, method, **payload):
        self.calls.append((method, payload))
        return True


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.state = State(self.path)
        self.state.activate_admin(42)
        self.state.register_user(43)
        self.state.change_access(42, 43, True)
        self.tg = PhotoTelegram()
        self.bot = Bot({"poll_seconds": 30, "allowed_chat_ids": []}, self.state, None, self.tg)
        self.catalog = self.bot.catalog

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def start(self, user=42):
        self.bot.command({"chat": {"id": user, "type": "private"}, "text": "/start"})

    def click(self, data, user=42):
        self.bot.callback({"id": "callback-test", "from": {"id": user}, "data": data,
                           "message": {"message_id": 1, "chat": {"id": user, "type": "private"}}})

    def write(self, text, user=42):
        self.bot.command({"chat": {"id": user, "type": "private"}, "text": text})

    def test_every_site_filter_has_working_controls_and_correct_query_names(self):
        self.assertEqual(len(self.catalog.fields), 28)
        query = {}
        for key, field in self.catalog.fields.items():
            with self.subTest(key=key):
                self.click("fld:" + key)
                if field["type"] == "number-range":
                    query = self.catalog.apply(query, key, "1 5")
                    self.assertEqual([query[x] for x in self.catalog.keys(key)], [1, 5])
                elif field["type"] == "number":
                    query = self.catalog.apply(query, key, "3")
                    self.assertEqual(query[field["query"]], 3)
                elif field["type"] == "boolean":
                    query = self.catalog.apply(query, key, "0")
                    self.assertEqual(query[field["query"]], 0)
                elif field["type"] == "select":
                    options = self.catalog.options(key)
                    query = self.catalog.apply(query, key, [str(options[0]["value"])])
                    self.assertEqual(query[field["query"]], str(options[0]["value"]))
                else:
                    query = self.catalog.apply(query, key, "test")
                    self.assertEqual(query[field["query"]], "test")
        captured = []
        market = Market(transport=lambda url: captured.append(url) or {"data": [], "hasMore": False})
        market.page(1, query)
        parsed = parse_qs(urlsplit(captured[0]).query)
        for key, value in query.items():
            self.assertEqual(parsed[key], [str(value)])
        self.assertEqual(parsed["premium"], ["0"])

    def test_basic_user_scenario_range_country_boolean_and_restart(self):
        self.start()
        self.click("input:price")
        self.write("100 600")
        self.click("preset:3")
        self.click("set:premium:0")
        ru = next(x for x in self.catalog.countries if x["code"] == "RU")
        self.click(f"toggle:country:{ru['value']}:0")
        expected = {"raitingLevelFrom": 3, "priceFrom": 100, "priceTo": 600, "premium": 0, "country": ru["value"]}
        self.assertEqual(self.state.chat_filters(42), expected)
        self.state.db.close()
        self.state = State(self.path)
        self.assertEqual(self.state.chat_filters(42), expected)
        self.assertTrue(self.state.is_active(42))

    def test_settings_are_independent_and_pause_does_not_reset_values(self):
        self.start(42)
        self.start(43)
        self.click("preset:5", 42)
        self.click("set:twoFactor:0", 43)
        self.write("/stop", 42)
        self.write("/start", 42)
        self.assertEqual(self.state.chat_filters(42), {"raitingLevelFrom": 5})
        self.assertEqual(self.state.chat_filters(43), {"raitingLevelFrom": 2, "two_fa": 0})

    def test_country_search_multi_selection_exclusion_and_clear(self):
        self.start()
        self.click("search:country")
        self.write("RU")
        self.assertIn("Россия", str(self.tg.screens[-1]))
        picks = [next(x for x in self.catalog.countries if x["code"] == code) for code in ("RU", "US")]
        for country in picks:
            self.click(f"toggle:country:{country['value']}:0")
        selected = set(self.state.chat_filters(42)["country"].split(","))
        self.assertEqual(selected, {x["value"] for x in picks})
        self.click(f"toggle:excludeCountry:{picks[0]['value']}:0")
        self.assertEqual(self.state.chat_filters(42)["not_country"], picks[0]["value"])
        self.click("clear:country")
        self.assertNotIn("country", self.state.chat_filters(42))

    def test_invalid_range_is_not_saved_and_input_can_be_cancelled(self):
        self.start()
        self.click("input:ratingLevel")
        for text in ("8 2", "NaN 5", "-1 3", "0 Infinity", "bad"):
            self.write(text)
            self.assertEqual(self.state.chat_filters(42), DEFAULT_FILTERS)
            self.assertTrue(self.state.ui_get(42)["pending"])
        self.click("fld:ratingLevel")
        self.assertFalse(self.state.ui_get(42)["pending"])
        self.click("input:ratingLevel")
        self.write("-")
        self.assertEqual(self.state.chat_filters(42), {})

    def test_all_accounts_query_has_no_hidden_rating_minimum(self):
        self.start()
        self.click("quick:all")
        captured = []
        Market(transport=lambda url: captured.append(url) or {"data": [raw(level=0)], "hasMore": False}).page(1, self.state.chat_filters(42))
        self.assertNotIn("raitingLevelFrom", parse_qs(urlsplit(captured[0]).query))
        self.assertTrue(matches(Listing.parse(raw(level=0)), {}))
        self.assertFalse(matches(Listing.parse(raw(level=0)), DEFAULT_FILTERS))

    def test_zero_boolean_and_bounded_rating_are_checked_locally(self):
        query = self.catalog.apply(DEFAULT_FILTERS, "ratingLevel", "2 4")
        query = self.catalog.apply(query, "premium", "0")
        item = raw(level=3)
        self.assertTrue(matches(Listing.parse(item), query))
        item["telegram"]["premium"] = 1
        self.assertFalse(matches(Listing.parse(item), query))
        self.assertFalse(matches(Listing.parse(raw(level=5)), query))

    def test_filter_change_cancels_pending_old_notifications(self):
        self.start()
        self.state.baseline([Listing.parse(raw(1))])
        item = Listing.parse(raw(2, 4, 1))
        self.state.ingest([item], item.published)
        self.assertEqual(len(self.state.pending()), 1)
        self.click("preset:5")
        self.assertEqual(len(self.state.pending()), 0)

    def test_identical_filters_share_source_scan_and_same_setting_keeps_cursor(self):
        self.start(42)
        self.start(43)
        self.click("preset:5", 42)
        self.click("preset:5", 43)
        profile = self.state.chat_profile(42)
        self.assertEqual(self.state.chat_profile(43), profile)
        self.assertEqual(self.state.active_profiles(), [profile])
        self.state.baseline([Listing.parse(raw(1, 5))], profile=profile)
        before = self.state.get(self.state.key(profile, "baseline"))
        self.click("preset:5", 42)
        self.assertEqual(self.state.get(self.state.key(profile, "baseline")), before)

    def test_photo_menu_all_screens_caption_and_button_limits(self):
        self.start()
        for command in ("/settings", "/status", "/help", "/check", "/stop", "/start"):
            self.write(command)
        for group, _, _ in self.catalog.groups():
            self.click("grp:" + group + ":0")
        for key in self.catalog.fields:
            self.click("fld:" + key)
        for action in ("reset:ask", "reset:yes", "nav:link", "quick:all", "monitor:off", "monitor:on", "nav:home"):
            self.click(action)
        for screen in self.tg.screens:
            self.assertLessEqual(len(screen["caption"].encode("utf-16-le")) // 2, 1024)
            for row in screen["keyboard"]["inline_keyboard"]:
                for item in row:
                    if "callback_data" in item:
                        self.assertLessEqual(len(item["callback_data"].encode()), 64)
        self.assertGreater(len(self.tg.screens), 40)

    def test_notifications_use_banner_photo(self):
        self.start()
        self.state.baseline([Listing.parse(raw(1))])
        item = Listing.parse(raw(2, 4, 1))
        self.state.ingest([item], item.published)
        self.bot.deliver_once()
        self.assertIn("Новый аккаунт", self.tg.screens[-1]["caption"])
        self.assertIn("Открыть объявление", str(self.tg.screens[-1]["keyboard"]))
        self.assertEqual(len(self.state.pending()), 0)

    def test_profile_scan_matches_only_subscribed_users_and_ignores_old_catalog(self):
        self.start(42)
        self.start(43)
        self.click("preset:5", 43)
        profile = self.state.chat_profile(43)
        calls = []
        new = False
        def transport(url):
            if url.endswith("/user/settings/commission"):
                return 15
            rows = [raw(1, 5, 0)] + ([raw(2, 3, 1), raw(3, 6, 2)] if new else [])
            if "/tg-accounts/" in url:
                item_id = int(url.rsplit("/", 1)[-1])
                row = next(x for x in rows if x["id"] == item_id)
                row["telegram"].update({"gifts_count":0,"unique_gifts_count":0})
                return row
            query = parse_qs(urlsplit(url).query)
            minimum = int(query["raitingLevelFrom"][0])
            calls.append(minimum)
            return {"data": sorted([x for x in rows if x["telegram"]["raiting_level"] >= minimum], key=lambda x:x["time_add"], reverse=True), "hasMore": False}
        self.bot.market = Market(transport=transport, page_delay=0)
        self.bot.scan()
        self.bot.scan(profile)
        self.assertFalse(self.state.pending())
        new = True
        self.bot.scan()
        self.bot.scan(profile)
        self.assertEqual({(x["chat_id"], x["item_id"]) for x in self.state.pending()}, {(42, 2), (42, 3), (43, 3)})
        self.assertEqual(calls, [2, 5, 2, 5])

    def test_old_database_is_migrated_without_replaying_seen_items(self):
        old_path = Path(self.temp.name) / "old.sqlite3"
        db = sqlite3.connect(old_path)
        db.executescript("CREATE TABLE chats(id INTEGER PRIMARY KEY,active INTEGER NOT NULL);"
                         "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);"
                         "CREATE TABLE seen(id INTEGER PRIMARY KEY);"
                         "INSERT INTO chats VALUES(42,1); INSERT INTO seen VALUES(1);")
        stamp = Listing.parse(raw()).published
        db.executemany("INSERT INTO settings VALUES(?,?)", [("baseline", str(stamp)), ("watermark", str(stamp))])
        db.commit()
        db.close()
        upgraded = State(old_path)
        try:
            self.assertFalse(upgraded.has_access(42))
            self.assertFalse(upgraded.is_active(42))
            self.assertIsNotNone(upgraded.db.execute("SELECT id FROM watch_seen WHERE profile='' AND id=1").fetchone())
            upgraded.activate_admin(42)
            upgraded.subscribe(42, True)
            upgraded.baseline([Listing.parse(raw(1))])
            upgraded.ingest([Listing.parse(raw(1)), Listing.parse(raw(2, 4, 1))], stamp + 1)
            self.assertEqual([x["item_id"] for x in upgraded.pending()], [2])
            self.assertEqual(upgraded.chat_filters(42), DEFAULT_FILTERS)
        finally:
            upgraded.db.close()

    def test_other_user_cannot_operate_private_menu(self):
        self.start()
        self.bot.callback({"id":"bad", "from":{"id":43}, "data":"quick:all",
                           "message":{"message_id":1,"chat":{"id":42,"type":"private"}}})
        self.assertEqual(self.state.chat_filters(42), DEFAULT_FILTERS)
        self.assertEqual(self.tg.calls[-1][1]["text"], "Нет доступа")

    def test_unknown_or_reserved_query_parameters_are_rejected(self):
        for bad in ({"category":"other"}, {"orderBy":"price"}, {"page":7}, {"priceFrom":float("nan")}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.catalog.validate(bad)

    def test_check_while_paused_checks_once_without_resuming_notifications(self):
        self.start()
        self.write("/stop")
        calls = []
        def transport(url):
            calls.append(url)
            self.bot.stop.set()
            self.bot.wake.set()
            return {"data": [raw()], "hasMore": False}
        self.bot.market = Market(transport=transport)
        self.write("/check")
        self.bot.monitor()
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.state.is_active(42))
        self.assertFalse(self.state.pending())


class TelegramPhotoTests(unittest.TestCase):
    def test_photo_upload_encodes_caption_and_keyboard_as_multipart(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "banner.jpg"
            image.write_bytes(b"jpeg-test-content")
            response = io.BytesIO(b'{"ok":true,"result":{}}')
            with patch("main.urlopen", return_value=response) as open_mock:
                request_json("https://api.telegram.org/botFAKE/sendPhoto",
                             {"chat_id":42,"caption":"Трекер","reply_markup":{"inline_keyboard":[]}}, files={"photo":image})
            request = open_mock.call_args[0][0]
            self.assertIn("multipart/form-data", request.get_header("Content-type"))
            self.assertIn(b'name="photo"; filename="banner.jpg"', request.data)
            self.assertIn(b"jpeg-test-content", request.data)
            self.assertIn("Трекер".encode(), request.data)
            self.assertIn(b'{"inline_keyboard": []}', request.data)

    def test_first_screen_uploads_banner_next_screen_edits_caption_and_notifications_reuse_id(self):
        calls = []
        def transport(url, payload, **kwargs):
            calls.append((url.rsplit("/", 1)[-1], payload, kwargs))
            return {"ok": True, "result": {"message_id": 1, "photo": [{"file_id": "cached-banner"}]}}
        tg = Telegram("fake-token", transport=transport)
        tg.screen(42, "Главное меню", {"inline_keyboard": []})
        tg.screen(42, "Настройки", {"inline_keyboard": []}, message_id=1)
        tg.screen(42, "Уведомление", {"inline_keyboard": []})
        self.assertEqual([x[0] for x in calls], ["sendPhoto", "editMessageCaption", "sendPhoto"])
        self.assertEqual(calls[0][2]["files"]["photo"].name, "banner.jpg")
        self.assertEqual(calls[-1][1]["photo"], "cached-banner")
        self.assertNotIn("files", calls[-1][2])

    def test_unchanged_menu_does_not_send_duplicate_photo(self):
        count = []
        def transport(url, payload, **kwargs):
            count.append(url)
            raise ServiceError("HTTP 400", 400, description="Bad Request: message is not modified")
        tg = Telegram("fake-token", transport=transport)
        self.assertEqual(tg.screen(42, "Same", {}, 12), {"message_id": 12})
        self.assertEqual(len(count), 1)


if __name__ == "__main__":
    unittest.main()
