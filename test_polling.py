"""Five-second scheduling, source backoff and reliable /start processing."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from access import DENIED_TEXT, OWNER_URL
from main import Bot, ServiceError, State, Telegram, load_config
from test_settings import PhotoTelegram


class PollingConfigTests(unittest.TestCase):
    def test_default_and_minimum_interval_are_five_seconds(self):
        with tempfile.TemporaryDirectory() as temp, patch("main.ROOT", Path(temp)), patch.dict("os.environ", {}, clear=True):
            self.assertEqual(load_config()["poll_seconds"], 5)
            path = Path(temp)/"config.json"
            path.write_text(json.dumps({"poll_seconds": 5}))
            self.assertEqual(load_config()["poll_seconds"], 5)
            for value in (0, 4, -5):
                with self.subTest(value=value):
                    path.write_text(json.dumps({"poll_seconds": value}))
                    with self.assertRaises(ValueError):
                        load_config()

    def test_explicit_longer_interval_is_preserved(self):
        with tempfile.TemporaryDirectory() as temp, patch("main.ROOT", Path(temp)), patch.dict("os.environ", {}, clear=True):
            (Path(temp)/"config.json").write_text(json.dumps({"poll_seconds": 30}))
            self.assertEqual(load_config()["poll_seconds"], 30)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name)/"state.sqlite3")
        self.state.activate_admin(42)
        self.state.subscribe(42, True)
        self.bot = Bot({"poll_seconds": 5}, self.state, None, PhotoTelegram())

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def finish_wait(self, seconds):
        self.bot.stop.set()
        return False

    def test_successful_scan_waits_five_seconds(self):
        self.bot.scan = Mock()
        with patch("main.time.monotonic", side_effect=[100, 100]), \
                patch.object(self.bot.wake, "wait", side_effect=self.finish_wait) as wait:
            self.bot.monitor()
        wait.assert_called_once_with(5)
        self.bot.scan.assert_called_once_with("")

    def test_request_duration_is_counted_towards_the_five_second_interval(self):
        self.bot.scan = Mock()
        with patch("main.time.monotonic", side_effect=[100, 103.5]), \
                patch.object(self.bot.wake, "wait", side_effect=self.finish_wait) as wait:
            self.bot.monitor()
        wait.assert_called_once_with(1.5)

    def test_slow_scan_finishes_before_next_cycle_without_negative_wait(self):
        self.bot.scan = Mock()
        with patch("main.time.monotonic", side_effect=[100, 107]), \
                patch.object(self.bot.wake, "wait", side_effect=self.finish_wait) as wait:
            self.bot.monitor()
        wait.assert_called_once_with(0)
        self.bot.scan.assert_called_once()

    def test_source_limits_cannot_be_shortened_by_start_or_check(self):
        for error, expected in ((ServiceError("Denied", 403), 60),
                                (ServiceError("Rate limited", 429), 60),
                                (ServiceError("Rate limited", 429, 125), 125)):
            with self.subTest(code=error.code, retry=error.retry_after):
                self.bot.stop.clear()
                self.bot.wake.set()
                self.bot.scan = Mock(side_effect=error)
                with patch.object(self.bot.stop, "wait", side_effect=self.finish_wait) as pause, \
                        patch.object(self.bot.wake, "wait") as interruptible_wait, \
                        self.assertLogs("tron-rating", level="WARNING"):
                    self.bot.monitor()
                pause.assert_called_once_with(expected)
                interruptible_wait.assert_not_called()

    def test_repeated_errors_back_off_up_to_ten_minutes(self):
        self.bot.scan = Mock(side_effect=ServiceError("Network unavailable"))
        waits = []
        def pause(seconds):
            waits.append(seconds)
            if len(waits) == 8:
                self.bot.stop.set()
            return False
        with patch.object(self.bot.stop, "wait", side_effect=pause), self.assertLogs("tron-rating", level="WARNING"):
            self.bot.monitor()
        self.assertEqual(waits, [10, 20, 40, 80, 160, 320, 600, 600])


class StartAndPollingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = State(Path(self.temp.name)/"state.sqlite3")

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    @staticmethod
    def message(user=42, text="/start"):
        return {"chat": {"id": user, "type": "private"},
                "from": {"id": user, "username": f"person{user}", "first_name": f"Person {user}"}, "text": text}

    def test_every_start_registers_and_answers_without_waiting_for_market(self):
        tg = PhotoTelegram()
        market = Mock()
        bot = Bot({"poll_seconds": 5}, self.state, market, tg)
        bot.error = "Tronaccs unavailable"
        for _ in range(3):
            bot.command(self.message(text="/start@tracker_bot invite"))
        self.assertEqual(len(tg.screens), 3)
        self.assertTrue(all(screen["message_id"] is None for screen in tg.screens))
        user = self.state.access_user(42)
        self.assertEqual(user["username"], "person42")
        self.assertFalse(self.state.has_access(42))
        self.assertEqual(tg.screens[-1]["caption"], DENIED_TEXT)
        market.assert_not_called()
        self.assertEqual(market.mock_calls, [])

    def test_rejected_banner_falls_back_to_text_with_owner_button_on_each_start(self):
        replies = []
        def transport(url, payload, **kwargs):
            if url.endswith("sendPhoto"):
                raise ServiceError("Photo rejected", 400)
            replies.append(payload)
            return {"ok": True, "result": {"message_id": len(replies)}}
        bot = Bot({"poll_seconds": 5}, self.state, None, Telegram("test-token", transport=transport))
        bot.command(self.message())
        bot.command(self.message())
        self.assertEqual(len(replies), 2)
        for payload in replies:
            self.assertEqual(payload["text"], DENIED_TEXT)
            self.assertEqual(payload["reply_markup"]["inline_keyboard"][0][0]["url"], OWNER_URL)

    def test_missing_banner_falls_back_to_a_working_authorized_menu(self):
        self.state.activate_admin(42)
        replies = []
        def transport(url, payload, **kwargs):
            if kwargs.get("files"):
                kwargs["files"]["photo"].read_bytes()
            replies.append(payload)
            return {"ok": True, "result": {"message_id": 1}}
        telegram = Telegram("test-token", transport=transport, banner=Path(self.temp.name)/"missing.jpg")
        bot = Bot({"poll_seconds": 5}, self.state, None, telegram)
        bot.command(self.message())
        self.assertTrue(self.state.is_active(42))
        self.assertTrue(bot.wake.is_set())
        self.assertIn("Проверка каждые 5 сек.", replies[0]["text"])
        self.assertTrue(replies[0]["reply_markup"]["inline_keyboard"])

    def test_permanently_rejected_chat_does_not_block_another_users_start(self):
        for code in (400, 403):
            with self.subTest(code=code):
                delivered = []
                def transport(url, payload, **kwargs):
                    if url.endswith("getUpdates"):
                        return {"ok": True, "result": [
                            {"update_id": 10, "message": self.message(42)},
                            {"update_id": 11, "message": self.message(43)}]}
                    if payload.get("chat_id") == 42:
                        raise ServiceError("Reply rejected", code)
                    delivered.append(payload["chat_id"])
                    bot.stop.set()
                    return {"ok": True, "result": {"message_id": 1}}
                bot = Bot({"poll_seconds": 5}, self.state, None, Telegram("test-token", transport=transport))
                with self.assertLogs("tron-rating", level="WARNING"):
                    bot.polling()
                self.assertEqual(delivered, [43])
                self.assertEqual(self.state.get("telegram_offset"), "12")

    def test_temporary_telegram_failure_does_not_discard_start(self):
        def transport(url, payload, **kwargs):
            if url.endswith("getUpdates"):
                return {"ok": True, "result": [{"update_id": 20, "message": self.message()}]}
            raise ServiceError("Rate limited", 429, 17)
        bot = Bot({"poll_seconds": 5}, self.state, None, Telegram("test-token", transport=transport))
        def pause(seconds):
            bot.stop.set()
            return False
        with patch.object(bot.stop, "wait", side_effect=pause) as wait, self.assertLogs("tron-rating", level="WARNING"):
            bot.polling()
        self.assertIsNone(self.state.get("telegram_offset"))
        wait.assert_called_once_with(17)
