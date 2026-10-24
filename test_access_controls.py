"""Authorization, private administration and repeated start regression tests."""
import hashlib
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import access
from access import DENIED_TEXT, OWNER_URL
from main import Bot, Listing, State
from test_bot import raw
from test_settings import PhotoTelegram

# Test-only credential. The production verifier and its input are not copied here.
ENTRY = "/unit-admin-access"


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state.sqlite3"
        self.state = State(self.path)
        self.tg = PhotoTelegram()
        self.bot = Bot({"poll_seconds": 30}, self.state, None, self.tg)
        digest = hashlib.pbkdf2_hmac("sha256", ENTRY.casefold().encode(), access._SALT, access._ITERATIONS)
        self.verifier = patch("access._DIGEST", digest)
        self.verifier.start()

    def tearDown(self):
        self.verifier.stop()
        self.state.db.close()
        self.temp.cleanup()

    def write(self, text, user=42, username=None):
        self.bot.command({"chat": {"id": user, "type": "private"},
                          "from": {"id": user, "username": username or f"person{user}", "first_name": f"Person {user}"},
                          "text": text})

    def click(self, data, user=7):
        self.bot.callback({"id": "callback", "from": {"id": user, "username": f"person{user}", "first_name": f"Person {user}"},
                           "message": {"message_id": 1, "chat": {"id": user, "type": "private"}}, "data": data})

    def authorize_admin(self, user=7):
        self.write(ENTRY, user)

    def grant(self, target=42):
        self.write("/start", target)
        self.authorize_admin()
        self.click(f"adm:grant:{target}:pending:0")

    def test_denied_start_has_exact_text_owner_button_and_new_message_every_time(self):
        for _ in range(3):
            self.write("/start")
            screen = self.tg.screens[-1]
            self.assertEqual(screen["caption"], DENIED_TEXT)
            self.assertIsNone(screen["message_id"])
            button = screen["keyboard"]["inline_keyboard"][0][0]
            self.assertEqual(button["url"], OWNER_URL)
            self.assertEqual(button["style"], "danger")
        self.assertFalse(self.state.has_access(42))
        self.assertFalse(self.state.is_active(42))
        self.assertEqual(self.state.access_counts(), (1, 0, 1))

    def test_authorized_start_always_sends_new_photo_while_navigation_edits(self):
        self.grant()
        self.write("/start")
        self.assertIsNone(self.tg.screens[-1]["message_id"])
        self.click("nav:filters", 42)
        self.assertIsNotNone(self.tg.screens[-1]["message_id"])
        self.write("/start")
        self.assertIsNone(self.tg.screens[-1]["message_id"])
        self.assertIn("Что отслеживаем", self.tg.screens[-1]["caption"])

    def test_private_entry_grants_admin_and_tracker_immediately_without_configured_ids(self):
        self.bot.cfg["allowed_chat_ids"] = [999]
        self.authorize_admin(42)
        self.assertTrue(self.state.is_admin(42))
        self.assertTrue(self.state.has_access(42))
        self.assertTrue(self.state.is_active(42))
        self.assertIn("Админ-панель", self.tg.screens[-1]["caption"])
        self.authorize_admin(43)
        self.assertTrue(self.state.is_admin(43))
        self.assertTrue(self.state.has_access(43))

    def test_verifier_accepts_case_and_bot_mention_but_rejects_extra_arguments(self):
        self.assertTrue(access.is_admin_entry(ENTRY.upper() + "@example_bot"))
        for value in (ENTRY + " extra", "wrong", "/wrong", "", "/" + "x" * 200):
            self.assertFalse(access.is_admin_entry(value))

    def test_admin_can_grant_from_pending_user_and_ordinary_user_gets_no_admin_role(self):
        self.grant()
        self.assertTrue(self.state.has_access(42))
        self.assertFalse(self.state.is_admin(42))
        self.write("/start")
        self.assertTrue(self.state.is_active(42))
        buttons = str(self.tg.screens[-1]["keyboard"])
        self.assertNotIn("nav:admin", buttons)
        self.write("/admin")
        self.assertEqual(self.tg.screens[-1]["caption"], "Админ-панель недоступна.")

    def test_ordinary_user_cannot_grant_or_revoke_access_through_forged_buttons(self):
        self.grant()
        self.write("/start", 43)
        self.click("adm:grant:43:pending:0", 42)
        self.assertFalse(self.state.has_access(43))
        self.click("adm:revoke:7:allowed:0", 42)
        self.assertTrue(self.state.is_admin(7))
        with self.assertRaises(PermissionError):
            self.state.change_access(42, 43, True)

    def test_revoked_access_blocks_old_buttons_commands_scans_and_queued_notifications(self):
        self.grant()
        profile = self.state.set_filters(42, {"raitingLevelFrom": 5})
        self.write("/start")
        self.state.baseline([Listing.parse(raw(1, 5))], profile=profile)
        item = Listing.parse(raw(2, 6, 1))
        self.state.ingest([item], item.published, profile)
        self.assertEqual(len(self.state.pending()), 1)
        self.click("adm:revoke:42:allowed:0")
        self.assertFalse(self.state.has_access(42))
        self.assertFalse(self.state.is_active(42))
        self.assertFalse(self.state.pending())
        before = self.state.chat_filters(42)
        self.click("quick:all", 42)
        self.assertEqual(self.state.chat_filters(42), before)
        self.assertEqual(self.tg.screens[-1]["caption"], DENIED_TEXT)
        self.write("/check")
        self.assertNotIn(profile, self.bot.check_profiles)
        self.assertNotIn(profile, self.state.active_profiles())
        self.assertFalse(self.state.needs_notification(item, profile))
        with self.assertRaises(PermissionError):
            self.state.subscribe(42, True)

    def test_grants_and_admin_role_survive_restart(self):
        self.grant()
        self.write("/start")
        self.state.db.close()
        self.state = State(self.path)
        self.assertTrue(self.state.is_admin(7))
        self.assertTrue(self.state.has_access(42))
        self.assertTrue(self.state.is_active(42))
        self.bot = Bot({"poll_seconds": 30}, self.state, None, self.tg)
        self.write("/start")
        self.assertIn("Что отслеживаем", self.tg.screens[-1]["caption"])

    def test_private_entry_is_not_echoed_in_messages_help_or_buttons(self):
        self.authorize_admin(42)
        self.write("/help")
        self.write("/start")
        self.assertNotIn(ENTRY.casefold(), str(self.tg.screens).casefold())

    def test_every_tracker_command_is_blocked_without_access(self):
        for command in ("/settings", "/rating", "/status", "/check", "/menu", "/admin", "hello"):
            with self.subTest(command=command):
                self.write(command)
                self.assertEqual(self.tg.screens[-1]["caption"], DENIED_TEXT)
        self.assertFalse(self.bot.check_profiles)

    def test_admin_panel_lists_registered_usernames_and_all_buttons_remain_red(self):
        self.write("/start", username="customer_name")
        self.authorize_admin()
        self.click("adm:list:pending:0")
        self.assertIn("@customer_name", str(self.tg.screens[-1]["keyboard"]))
        self.click("adm:user:42:pending:0")
        self.assertIn("@customer_name", self.tg.screens[-1]["caption"])
        for screen in self.tg.screens:
            self.assertLessEqual(len(screen["caption"].encode("utf-16-le")) // 2, 1024)
            for row in screen["keyboard"]["inline_keyboard"]:
                for item in row:
                    self.assertEqual(item["style"], "danger")
                    if "callback_data" in item:
                        self.assertLessEqual(len(item["callback_data"].encode()), 64)

    def test_username_changes_do_not_change_the_users_rights(self):
        self.grant()
        self.write("/start", username="new_name")
        self.assertTrue(self.state.has_access(42))
        self.assertEqual(self.state.access_user(42)["username"], "new_name")

    def test_failed_private_entries_are_rate_limited_without_disclosing_the_input(self):
        for _ in range(10):
            self.write("/wrong")
        self.write(ENTRY)
        self.assertFalse(self.state.is_admin(42))
        self.bot.auth_attempts[42] = [time.monotonic() - 61]
        self.write(ENTRY)
        self.assertTrue(self.state.is_admin(42))

    def test_admin_access_cannot_be_removed_by_tracker_access_toggle(self):
        self.authorize_admin()
        self.click("adm:revoke:7:allowed:0")
        self.assertTrue(self.state.is_admin(7))
        self.assertTrue(self.state.has_access(7))

    def test_authorization_does_not_work_in_group_chats(self):
        self.bot.command({"chat": {"id": -10, "type": "group"}, "text": ENTRY})
        self.assertFalse(self.state.is_admin(-10))
        self.assertFalse(self.tg.screens)


if __name__ == "__main__":
    unittest.main()
