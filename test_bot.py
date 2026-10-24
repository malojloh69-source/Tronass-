"""Offline tests: no Telegram credentials, no live notifications."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from main import Bot, Listing, Market, SchemaError, ServiceError, State


def raw(item_id=1, level=2, second=0, stars=0):
    return {"id": item_id, "title": "Аккаунт <тест>", "price": 100,
            "time_add": f"2026-10-22T10:{second // 60:02}:{second % 60:02}.000Z",
            "telegram": {"raiting_level": level, "raiting_stars": stars,
                         "country": {"name": "Россия"}, "premium": 0}}


class FakeTelegram:
    def __init__(self):
        self.messages = []
        self.fail = None

    def send(self, chat_id, text, keyboard=None):
        if self.fail:
            raise self.fail
        self.messages.append((chat_id, text, keyboard))


class BotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.state = State(self.path)
        self.state.activate_admin(42)

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def bot(self, market=None):
        return Bot({"poll_seconds": 30, "allowed_chat_ids": []}, self.state, market, FakeTelegram())

    def test_exact_rating_level_independent_of_stars(self):
        self.state.subscribe(42, True)
        self.state.baseline([Listing.parse(raw(1, 1))])
        items = [Listing.parse(raw(10 + i, level, 1, stars=100000 if level < 2 else 0))
                 for i, level in enumerate([-1, 0, 1, 2, 3, 7])]
        self.state.ingest(items, items[-1].published)
        self.assertEqual({r["item_id"] for r in self.state.pending()}, {13, 14, 15})

    def test_baseline_existing_and_older_items_are_not_notified(self):
        self.state.subscribe(42, True)
        self.state.baseline([Listing.parse(raw(10, 4, 60))])
        items = [Listing.parse(raw(10, 4, 60)), Listing.parse(raw(9, 5, 30))]
        self.state.ingest(items, items[0].published)
        self.assertEqual(len(self.state.pending()), 0)

    def test_same_second_new_id_not_lost(self):
        self.state.subscribe(42, True)
        old, new = Listing.parse(raw(1)), Listing.parse(raw(2))
        self.state.baseline([old])
        self.state.ingest([old, new], new.published)
        self.assertEqual([r["item_id"] for r in self.state.pending()], [2])

    def test_restart_dedup_and_outbox_persistence(self):
        self.state.subscribe(42, True)
        self.state.baseline([Listing.parse(raw(1))])
        item = Listing.parse(raw(2, 4, 1))
        self.state.ingest([item], item.published)
        self.state.db.close()
        self.state = State(self.path)
        self.state.ingest([item], item.published)
        self.assertEqual(len(self.state.pending()), 1)
        self.state.sent(2, 42)
        self.state.ingest([item], item.published)
        self.assertEqual(len(self.state.pending()), 0)

    def test_stop_cancels_backlog_and_start_does_not_replay(self):
        self.state.subscribe(42, True)
        self.state.baseline([Listing.parse(raw(1))])
        item = Listing.parse(raw(2, 4, 1))
        self.state.ingest([item], item.published)
        self.state.subscribe(42, False)
        self.state.subscribe(42, True)
        self.assertEqual(len(self.state.pending()), 0)
        self.state.ingest([item], item.published)
        self.assertEqual(len(self.state.pending()), 0)

    def test_failed_telegram_send_keeps_outbox_for_retry(self):
        self.state.subscribe(42, True)
        self.state.baseline([Listing.parse(raw(1))])
        item = Listing.parse(raw(2, 4, 1))
        self.state.ingest([item], item.published)
        bot = self.bot()
        bot.tg.fail = ServiceError("Ошибка сети")
        bot.deliver_once()
        row = self.state.db.execute("SELECT * FROM outbox").fetchone()
        self.assertEqual(row["sent"], 0)
        self.assertEqual(row["attempts"], 1)
        self.state.db.execute("UPDATE outbox SET next_try=0")
        self.state.db.commit()
        bot.tg.fail = None
        bot.deliver_once()
        self.assertEqual(len(bot.tg.messages), 1)
        self.assertEqual(len(self.state.pending()), 0)

    def test_unknown_rating_is_an_error_instead_of_silent_skip(self):
        item = raw()
        del item["telegram"]["raiting_level"]
        with self.assertRaises(SchemaError):
            Listing.parse(item)

    def test_invalid_numeric_and_date_fields_fail_closed(self):
        for value in [True, None, "unknown", 2.5]:
            with self.subTest(value=value), self.assertRaises(SchemaError):
                Listing.parse(raw(level=value))
        for value in ["NaN", "Infinity", -1]:
            item = raw()
            item["price"] = value
            with self.subTest(value=value), self.assertRaises(SchemaError):
                Listing.parse(item)

    def test_message_escape_price_and_safe_item_url(self):
        item = Listing.parse(raw())
        self.assertIn("&lt;тест&gt;", item.message())
        self.assertIn("100 ₽", item.message())
        self.assertIn("telegram/1", json.dumps(item.keyboard()))

    def test_many_pages_overlap_and_cursor_boundaries(self):
        calls = []
        def transport(url):
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query["raitingLevelFrom"], ["2"])
            self.assertEqual(query["orderType"], ["DESC"])
            page = int(query["page"][0])
            calls.append(page)
            batches = {1: [raw(i, 2, i) for i in range(60, 35, -1)],
                       2: [raw(i, 2, i) for i in range(36, 11, -1)],
                       3: [raw(i, 2, i) for i in range(12, 0, -1)]}
            return {"data": batches[page], "hasMore": page < 3}
        market = Market(page_delay=0, transport=transport)
        items, newest = market.since(Listing.parse(raw(second=10)).published, threading.Event())
        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(len(items), 51)
        self.assertEqual(len({i.id for i in items}), 51)
        self.assertEqual(newest, Listing.parse(raw(second=60)).published)

    def test_repeated_page_and_unsorted_results_detected(self):
        market = Market(page_delay=0, transport=lambda _: {"data": [raw(2, second=2)], "hasMore": True})
        with self.assertRaises(SchemaError):
            market.since(Listing.parse(raw()).published, threading.Event())
        market = Market(transport=lambda _: {"data": [raw(1), raw(2, second=2)], "hasMore": False})
        with self.assertRaises(SchemaError):
            market.page(1)

    def test_failed_scan_preserves_checkpoint(self):
        def transport(url):
            if parse_qs(urlsplit(url).query)["page"] == ["2"]:
                raise ServiceError("HTTP 503", 503)
            return {"data": [raw(20, 4, 60)], "hasMore": True}
        self.state.baseline([Listing.parse(raw(1))])
        before = self.state.get("watermark")
        bot = self.bot(Market(page_delay=0, transport=transport))
        with self.assertRaises(ServiceError):
            bot.scan()
        self.assertEqual(self.state.get("watermark"), before)
        self.assertEqual(len(self.state.pending()), 0)

    def test_scan_page_limit_does_not_commit_partial_results(self):
        market = Market(max_pages=1, page_delay=0, transport=lambda _: {"data": [raw(2, 4, 2)], "hasMore": True})
        self.state.baseline([Listing.parse(raw(1))])
        before = self.state.get("watermark")
        with self.assertRaises(ServiceError):
            self.bot(market).scan()
        self.assertEqual(self.state.get("watermark"), before)

    def test_empty_initial_catalog_and_no_subscribers(self):
        self.state.baseline([], now=Listing.parse(raw()).published)
        self.state.ingest([Listing.parse(raw(2, 4, 1))], Listing.parse(raw(2, 4, 1)).published)
        self.state.subscribe(42, True)
        self.assertEqual(len(self.state.pending()), 0)

    def test_private_chat_and_access_gate(self):
        bot = self.bot()
        bot.command({"chat": {"id": -1, "type": "group"}, "text": "/start"})
        self.assertFalse(self.state.is_active(-1))
        bot.command({"chat": {"id": 43, "type": "private"}, "text": "/start"})
        self.assertFalse(self.state.is_active(43))
        bot.command({"chat": {"id": 42, "type": "private"}, "text": "/start"})
        self.assertTrue(self.state.is_active(42))

    def test_revocation_removes_subscriber_and_pending_queue(self):
        self.state.register_user(43)
        self.state.change_access(42, 43, True)
        self.state.subscribe(43, True)
        self.state.baseline([Listing.parse(raw(1))])
        item = Listing.parse(raw(2, 4, 1))
        self.state.ingest([item], item.published)
        self.state.change_access(42, 43, False)
        Bot({"poll_seconds": 30}, self.state, None, FakeTelegram())
        self.assertFalse(self.state.is_active(43))
        self.assertEqual(len(self.state.pending()), 0)


if __name__ == "__main__":
    unittest.main()
