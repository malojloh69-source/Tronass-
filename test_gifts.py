"""Gift parsing, original image transport and restart-safe delivery scenarios."""
import html
import io
import json
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from PIL import Image

from gifts import ASSETS, GiftCatalog, bundled_gifts, gift_caption, gift_details, safe_photo_url
from gift_photos import PhotoError, photo_jpeg
from main import Bot, Listing, Market, ServiceError, State, Telegram
from test_bot import raw

AVAILABLE = "6005797617768858105"  # Artisan Brick, exact 64-bit gift ID
PENDING = "5956308547863052791"  # Trojan Horse
ORDINARY = "5170145012310081615"


def account():
    data = raw(2, 4, 1, stars=6543)
    data["telegram"].update({"gifts_count": 2, "unique_gifts_count": 1,
                             "gifts": json.dumps([[AVAILABLE, 100], [PENDING, 500]]),
                             "unique_gifts": json.dumps([[["Plush Pepe", 1, "PlushPepe-1"]]])})
    return data


def jpeg():
    output = io.BytesIO()
    Image.new("RGB", (64, 64), (40, 80, 160)).save(output, "JPEG")
    return output.getvalue()


class GiftParsingTests(unittest.TestCase):
    def test_real_site_tuple_formats_include_titles_status_and_exact_nft_photo(self):
        parts = Listing.parse(account()).attributes["gift_details"]
        self.assertEqual([p["title"] for p in parts], ["Artisan Brick", "Trojan Horse", "Plush Pepe #1"])
        self.assertEqual([p.get("status") for p in parts[:2]], ["available", "pending"])
        self.assertEqual(parts[-1]["photo"], "https://nft.fragment.com/gift/plushpepe-1.medium.jpg")
        self.assertIn(AVAILABLE, parts[0]["photo"])
        self.assertIn("6543", Listing.parse(account()).message())

    def test_excludes_unlimited_gifts_and_aggregates_repeated_regular_gifts(self):
        parts = gift_details({"gifts": [[AVAILABLE, 100], [AVAILABLE, 100], [ORDINARY, 15]], "gifts_count": 3})
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["count"], 2)
        self.assertIn("×2", gift_caption(parts[0], 2))
        self.assertNotEqual(parts[0]["count"], 100)

    def test_instance_can_upgrade_flag_overrides_catalogue_status(self):
        parts = gift_details({"gifts": [{"gift": {"id": AVAILABLE}, "can_upgrade": False}]})
        self.assertEqual(parts[0]["status"], "unavailable")
        self.assertIn("для этого экземпляра", gift_caption(parts[0], 2))

    def test_new_limited_gift_can_have_direct_title_and_zero_upgrade_cost(self):
        parts = gift_details({"gifts": [{"id": "1234567890123456789", "title": "New Gift", "upgrade_stars": 0}]})
        self.assertEqual(parts[0]["title"], "New Gift")
        self.assertEqual(parts[0]["status"], "available")

    def test_pending_label_does_not_promise_an_upgrade_date(self):
        part = gift_details({"gifts": [[PENDING, 500]]})[0]
        caption = gift_caption(part, 2)
        self.assertIn("срок не объявлен", caption)
        self.assertNotIn("можно улучшить", caption)

    def test_missing_or_malformed_lists_never_guess_names_from_counts(self):
        for value in (None, "invalid", {}, "{}"):
            with self.subTest(value=value):
                parts = gift_details({"gifts": value, "gifts_count": 3})
                self.assertEqual(len(parts), 1)
                self.assertEqual(parts[0]["kind"], "notice")
        self.assertEqual(gift_details({"gifts_count": 0, "unique_gifts_count": 0}), [])
        self.assertEqual(gift_details({"gifts": [[None]], "gifts_count": 1})[-1]["kind"], "notice")

    def test_partial_list_reports_missing_gifts_without_discarding_known_ones(self):
        parts = gift_details({"gifts": [[AVAILABLE, 100]], "gifts_count": 3})
        self.assertEqual(parts[0]["title"], "Artisan Brick")
        self.assertEqual(parts[-1]["kind"], "notice")

    def test_legacy_nft_collection_preview_is_explicitly_labelled(self):
        part = gift_details({"unique_gifts": [[AVAILABLE]]})[0]
        self.assertEqual(part["kind"], "nft")
        self.assertIn("Изображение коллекции", gift_caption(part, 2))
        self.assertIn("Artisan Brick", gift_caption(part, 2))

    def test_unique_gift_slug_cannot_escape_asset_path(self):
        parts = gift_details({"unique_gifts": [[["Bad Gift", 1, "../../private"]]], "unique_gifts_count": 1})
        self.assertFalse(any(p.get("photo") for p in parts))
        self.assertFalse(safe_photo_url("https://127.0.0.1/private.jpg"))
        self.assertFalse(safe_photo_url("https://nft.fragment.com@evil.test/gift/a.jpg"))

    def test_captions_escape_titles_and_fit_telegram_with_unicode(self):
        parts = gift_details({"unique_gifts": [[["<b>😀"*200, 1, "PlushPepe-1"]]]})
        caption = gift_caption(parts[0], 2)
        self.assertIn("&lt;b&gt;", caption)
        plain = html.unescape(re.sub(r"<[^>]*>", "", caption))
        self.assertLessEqual(len(plain.encode("utf-16-le"))//2, 1024)


class GiftCatalogTests(unittest.TestCase):
    def test_dynamic_catalogue_and_bot_api_refresh_names_upgrade_flags_and_thumbnail(self):
        fetch = Mock(return_value={"upgraded": [{"regular_id": "1234567890123456789", "full_name": "Fresh Gift"}]})
        telegram = Mock(return_value={"gifts": [{"id": "1234567890123456789", "upgrade_star_count": 25,
                                                 "sticker": {"thumbnail": {"file_id": "thumb-file"}}}]})
        catalog = GiftCatalog(fetch=fetch, telegram=telegram)
        catalog.refresh()
        catalog.refresh()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(telegram.call_count, 1)
        part = gift_details({"gifts": [["1234567890123456789", 200]]}, catalog)[0]
        self.assertEqual((part["title"], part["status"], part["thumbnail_file_id"]), ("Fresh Gift", "available", "thumb-file"))
        self.assertIn(AVAILABLE, catalog.entries)

    def test_catalogue_failure_preserves_offline_names_and_does_not_probe_on_each_listing(self):
        fetch = Mock(side_effect=ServiceError("Unavailable", 503))
        catalog = GiftCatalog(fetch=fetch)
        catalog.refresh()
        catalog.refresh()
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(catalog.entries[AVAILABLE]["title"], "Artisan Brick")

    def test_catalogue_cache_survives_restart_without_user_identifiers(self):
        with tempfile.TemporaryDirectory() as temp:
            state = State(Path(temp)/"state.sqlite3")
            try:
                catalog = GiftCatalog(fetch=lambda _: {"unupgraded": [{"id": "1234567890123456789", "full_name": "Saved Gift"}]}, state=state)
                catalog.refresh()
                self.assertEqual(GiftCatalog(state=state).entries["1234567890123456789"]["title"], "Saved Gift")
            finally:
                state.db.close()


class GiftPhotoTests(unittest.TestCase):
    def test_original_webp_is_converted_to_jpeg_and_alpha_is_flattened(self):
        source = io.BytesIO()
        Image.new("RGBA", (80, 40), (255, 0, 0, 0)).save(source, "WEBP")
        opener = Mock()
        opener.open.return_value = io.BytesIO(source.getvalue())
        with patch("gift_photos.build_opener", return_value=opener):
            result = photo_jpeg(ASSETS + f"/webp/by_id/{AVAILABLE}.webp")
        image = Image.open(io.BytesIO(result))
        self.assertEqual((image.format, image.mode, image.size), ("JPEG", "RGB", (80, 80)))
        self.assertLess(abs(image.getpixel((40, 40))[0]-28), 5)

    def test_invalid_image_content_is_not_uploaded_as_photo(self):
        opener = Mock()
        opener.open.return_value = io.BytesIO(b"<html>upstream unavailable</html>")
        with patch("gift_photos.build_opener", return_value=opener):
            with self.assertRaises(PhotoError) as caught:
                photo_jpeg(ASSETS + f"/webp/by_id/{AVAILABLE}.webp")
        self.assertEqual(caught.exception.code, 400)

    def test_unsafe_url_is_rejected_before_any_request(self):
        with patch("gift_photos.build_opener") as opener:
            with self.assertRaises(PhotoError):
                photo_jpeg("http://localhost:3000/config.json")
            opener.assert_not_called()

    def test_photo_rate_limit_preserves_retry_after(self):
        opener = Mock()
        opener.open.side_effect = HTTPError(ASSETS, 429, "rate limited", {"Retry-After": "17"}, None)
        with patch("gift_photos.build_opener", return_value=opener):
            with self.assertRaises(PhotoError) as caught:
                photo_jpeg(ASSETS + f"/webp/by_id/{AVAILABLE}.webp")
        self.assertEqual((caught.exception.code, caught.exception.retry_after), (429, 17))

    def test_telegram_uploads_real_jpeg_with_gift_name(self):
        calls = []
        def transport(url, payload, **kwargs):
            calls.append((url.rsplit("/", 1)[-1], payload))
            if kwargs.get("files"):
                with Image.open(kwargs["files"]["photo"]) as image:
                    self.assertEqual(image.format, "JPEG")
            return {"ok": True, "result": {"message_id": 9}}
        telegram = Telegram("test-token", transport=transport)
        with patch("main.photo_jpeg", return_value=jpeg()):
            telegram.gift(42, gift_details(account()["telegram"])[-1], 2)
        self.assertEqual(calls[0][0], "sendPhoto")
        self.assertIn("Plush Pepe #1", calls[0][1]["caption"])

    def test_missing_photo_reports_name_and_link_without_suppressing_listing(self):
        calls = []
        telegram = Telegram("test-token", transport=lambda url, payload, **kwargs:
                            calls.append((url, payload)) or {"ok": True, "result": {"message_id": 1}})
        part = gift_details(account()["telegram"])[0]
        with patch("main.photo_jpeg", side_effect=PhotoError(404)):
            telegram.gift(42, part, 2)
        self.assertTrue(calls[0][0].endswith("sendMessage"))
        self.assertIn("Artisan Brick", calls[0][1]["text"])
        self.assertIn("Фото сейчас недоступно", calls[0][1]["text"])

    def test_thumbnail_download_url_with_bot_token_is_not_queued_or_echoed(self):
        calls = []
        def transport(url, payload, **kwargs):
            calls.append((url, payload))
            return {"ok": True, "result": {"file_path": "photos/thumb.jpg", "message_id": 1}}
        telegram = Telegram("test-token", transport=transport)
        part = gift_details(account()["telegram"])[0]
        part["thumbnail_file_id"] = "thumbnail-id"
        with patch("main.photo_jpeg", return_value=jpeg()) as download:
            telegram.gift(42, part, 2)
        self.assertEqual(download.call_args.args[0], "https://api.telegram.org/file/bottest-token/photos/thumb.jpg")
        self.assertNotIn("test-token", json.dumps(part))
        self.assertNotIn("test-token", calls[-1][1]["caption"])

    def test_same_gift_photo_is_reused_for_another_recipient(self):
        calls = []
        def transport(url, payload, **kwargs):
            calls.append((payload, kwargs))
            return {"ok": True, "result": {"message_id": 1, "photo": [{"file_id": "cached-gift"}]}}
        telegram = Telegram("test-token", transport=transport)
        part = gift_details(account()["telegram"])[0]
        with patch("main.photo_jpeg", return_value=jpeg()) as download:
            telegram.gift(42, part, 2)
            telegram.gift(43, part, 2)
        self.assertEqual(download.call_count, 1)
        self.assertEqual(calls[-1][0]["photo"], "cached-gift")
        self.assertEqual(calls[-1][0]["chat_id"], 43)

    def test_rejected_photo_does_not_leave_notification_stuck_forever(self):
        calls = []
        def transport(url, payload, **kwargs):
            calls.append(url.rsplit("/", 1)[-1])
            if url.endswith("sendPhoto"):
                raise ServiceError("Photo rejected", 400)
            return {"ok": True, "result": {"message_id": 1}}
        telegram = Telegram("test-token", transport=transport)
        with patch("main.photo_jpeg", return_value=jpeg()):
            telegram.gift(42, gift_details(account()["telegram"])[0], 2)
        self.assertEqual(calls, ["sendPhoto", "sendMessage"])


class GiftDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"state.sqlite3"
        self.state = State(self.path)
        self.state.activate_admin(42)
        self.state.subscribe(42, True)
        self.state.baseline([Listing.parse(raw(1))])
        self.calls = []
        self.fail_title = None
        self.after_send = None
        def transport(url, payload, **kwargs):
            if self.fail_title and self.fail_title in payload.get("caption", ""):
                raise ServiceError("Temporary failure", 503)
            self.calls.append((url.rsplit("/", 1)[-1], payload))
            if self.after_send:
                self.after_send(payload)
            return {"ok": True, "result": {"message_id": len(self.calls)}}
        self.telegram = Telegram("test-token", transport=transport)
        self.bot = Bot({"poll_seconds": 30}, self.state, None, self.telegram)

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def deliver(self):
        with patch("main.photo_jpeg", return_value=jpeg()), patch.object(self.bot.stop, "wait", return_value=False):
            self.bot.deliver_once()

    def test_full_scan_queues_snapshot_and_delivers_banner_then_named_photos(self):
        def transport(url):
            if url.endswith("/user/settings/commission"):
                return 15
            if "/tg-accounts/" in url:
                return account()
            return {"data": [raw(2, 4, 1), raw(1)], "hasMore": False}
        self.bot.market = Market(transport=transport, page_delay=0)
        self.bot.scan()
        self.assertEqual(len(json.loads(self.state.pending()[0]["gift_parts"])), 3)
        self.deliver()
        self.assertEqual([method for method, _ in self.calls], ["sendPhoto"]*4)
        self.assertIn("Новый аккаунт", self.calls[0][1]["caption"])
        self.assertIn("Artisan Brick", self.calls[1][1]["caption"])
        self.assertIn("Trojan Horse", self.calls[2][1]["caption"])
        self.assertIn("Plush Pepe #1", self.calls[3][1]["caption"])
        self.assertFalse(self.state.pending())

    def test_restart_after_failed_photo_resumes_without_repeating_banner_or_prior_photo(self):
        item = Listing.parse(account())
        self.state.ingest([item], item.published)
        self.fail_title = "Trojan Horse"
        self.deliver()
        row = self.state.db.execute("SELECT * FROM outbox").fetchone()
        self.assertEqual((row["sent"], row["next_part"]), (0, 2))
        self.assertEqual(len(self.calls), 2)
        self.state.db.close()
        self.state = State(self.path)
        self.bot.state = self.state
        self.state.db.execute("UPDATE outbox SET next_try=0")
        self.state.db.commit()
        self.fail_title = None
        self.deliver()
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(sum("Новый аккаунт" in p.get("caption", "") for _, p in self.calls), 1)
        self.assertFalse(self.state.pending())

    def test_stop_between_banner_and_gift_prevents_remaining_photos(self):
        item = Listing.parse(account())
        self.state.ingest([item], item.published)
        self.after_send = lambda _: self.state.subscribe(42, False)
        self.deliver()
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.state.pending())

    def test_access_revocation_between_parts_prevents_remaining_gift_photos(self):
        self.state.subscribe(42, False)
        self.state.register_user(43)
        self.state.change_access(42, 43, True)
        self.state.subscribe(43, True)
        self.state.baseline([Listing.parse(raw(1))])
        item = Listing.parse(account())
        self.state.ingest([item], item.published)
        self.after_send = lambda _: self.state.change_access(42, 43, False)
        self.deliver()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0][1]["chat_id"], 43)
        self.assertFalse(self.state.pending())

    def test_source_with_only_counts_sends_explicit_missing_details_notice(self):
        item = raw(2, 4, 1)
        item["telegram"].update({"gifts_count": 4, "unique_gifts_count": 1})
        listing = Listing.parse(item)
        self.state.ingest([listing], listing.published)
        self.deliver()
        self.assertEqual([m for m, _ in self.calls], ["sendPhoto", "sendMessage"])
        self.assertIn("не передал полный список", self.calls[-1][1]["text"])

    def test_old_outbox_migration_preserves_unsent_listing(self):
        other = Path(self.temp.name)/"old.sqlite3"
        db = sqlite3.connect(other)
        db.execute("CREATE TABLE outbox(item_id INTEGER,chat_id INTEGER,text TEXT,keyboard TEXT,sent INTEGER DEFAULT 0,attempts INTEGER DEFAULT 0,next_try REAL DEFAULT 0,PRIMARY KEY(item_id,chat_id))")
        db.execute("CREATE TABLE access_users(id INTEGER PRIMARY KEY,username TEXT DEFAULT '',name TEXT DEFAULT '',has_access INTEGER DEFAULT 0,is_admin INTEGER DEFAULT 0,requested INTEGER DEFAULT 0,created REAL,updated REAL)")
        db.execute("INSERT INTO access_users(id,has_access) VALUES(42,1)")
        db.execute("INSERT INTO outbox(item_id,chat_id,text,keyboard) VALUES(2,42,'saved notification','{}')")
        db.commit()
        db.close()
        state = State(other)
        try:
            row = state.db.execute("SELECT * FROM outbox").fetchone()
            self.assertEqual((row["text"], row["gift_parts"], row["next_part"]), ("saved notification", "[]", 0))
        finally:
            state.db.close()
