import json
import os
import tempfile
import unittest
from unittest.mock import patch

from src.notifications import AccountSummary, NotificationDispatcher


class TestScheduledNotifications(unittest.TestCase):
    def setUp(self):
        self.state_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "schedule",
            "SKIP_RANDOM_SLEEP": "0",
            "TELEGRAM_NOTIFY_ALWAYS": "0",
            "TELEGRAM_BOT_TOKEN": "fake_token",
            "TELEGRAM_CHAT_ID": "fake_chat",
        }))
        self.post = self.enterContext(patch("src.notifications.requests.post"))
        self.post.return_value.status_code = 200
        self.enterContext(patch("src.notifications.get_usd_to_uah_rate", return_value=40.0))

    def dispatcher(self, **changes):
        values = dict(
            name="Account_1", checkin_status="ALREADY_DONE",
            pc_status="ALREADY_DONE", mobile_status="ALREADY_DONE",
            coins_earned=40, coins_earned_this_run=0, prior_rewards_today=40,
            initial_coins=1220, total_coins=1220, tasks_failed=1,
        )
        values.update(changes)
        dispatcher = NotificationDispatcher(state_dir=self.state_dir)
        dispatcher.add_summary(AccountSummary(**values))
        return dispatcher

    def test_repeated_failure_is_quiet_but_new_rewards_and_problems_notify(self):
        self.assertTrue(self.dispatcher().send_telegram_notification())
        # A new dispatcher represents the next Actions runner restoring .state.
        self.assertFalse(self.dispatcher().send_telegram_notification())
        self.assertFalse(self.dispatcher(total_coins=1226).send_telegram_notification())
        self.assertEqual(self.post.call_count, 1)

        fresh = self.dispatcher(coins_earned=41, coins_earned_this_run=1,
                                tasks_done=1, tasks_coins=1)
        self.assertTrue(fresh.send_telegram_notification())
        text = self.post.call_args.kwargs["json"]["text"]
        self.assertIn("Зароблено за запуск: <b>+1 🪙</b>", text)
        self.assertIn("чекін: +0, таски: +1", text)
        self.assertNotIn("+41", text)
        self.assertNotIn("Зароблено сьогодні", text)
        self.assertFalse(self.dispatcher().send_telegram_notification())

        captcha = dict(mobile_status="CAPTCHA_REQUIRED")
        self.assertTrue(self.dispatcher(**captcha).send_telegram_notification())
        self.assertFalse(self.dispatcher(**captcha).send_telegram_notification())
        self.assertEqual(self.post.call_count, 3)

    def test_date_and_recipient_changes_allow_alert_again(self):
        dispatcher = self.dispatcher()
        self.assertTrue(dispatcher.send_telegram_notification())
        with open(dispatcher.notification_state_path, encoding="utf-8") as f:
            state = json.load(f)
        state["date"] = "2000-01-01"
        with open(dispatcher.notification_state_path, "w", encoding="utf-8") as f:
            json.dump(state, f)
        self.assertTrue(self.dispatcher().send_telegram_notification())
        other = self.dispatcher()
        other.telegram_chat_id = "other_chat"
        self.assertTrue(other.send_telegram_notification())

    def test_failed_delivery_and_corrupt_state_do_not_suppress_retry(self):
        dispatcher = self.dispatcher()
        self.post.return_value.status_code = 500
        self.assertFalse(dispatcher.send_telegram_notification())
        self.assertFalse(os.path.exists(dispatcher.notification_state_path))
        self.post.return_value.status_code = 200
        self.assertTrue(self.dispatcher().send_telegram_notification())
        with open(dispatcher.notification_state_path, "w", encoding="utf-8") as f:
            f.write("broken json")
        self.assertTrue(self.dispatcher().send_telegram_notification())

    def test_manual_and_forced_reports_bypass_dedup_but_skip_sleep_does_not(self):
        self.assertTrue(self.dispatcher().send_telegram_notification())
        with patch.dict(os.environ, {"SKIP_RANDOM_SLEEP": "1"}):
            self.assertFalse(self.dispatcher().send_telegram_notification())
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}):
            self.assertTrue(self.dispatcher().send_telegram_notification())
        with patch.dict(os.environ, {"TELEGRAM_NOTIFY_ALWAYS": "1"}):
            self.assertTrue(self.dispatcher().send_telegram_notification())

    def test_repeat_report_and_step_summary_do_not_count_prior_rewards(self):
        dispatcher = self.dispatcher()
        with patch.dict(os.environ, {"GITHUB_EVENT_NAME": "workflow_dispatch"}):
            self.assertTrue(dispatcher.send_telegram_notification())
        text = self.post.call_args.kwargs["json"]["text"]
        self.assertIn("Зароблено за запуск: <b>0 🪙</b>", text)
        self.assertNotIn(", +40 монет", text)
        self.assertIn("Зароблено монет за запуск:** +0 🪙", dispatcher.format_step_summary())
        for tasks_coins in (0, 1):
            with self.subTest(tasks_coins=tasks_coins):
                summary = AccountSummary(name="Repeat", checkin_status="ALREADY_DONE",
                                         coins_earned=40 + tasks_coins, tasks_coins=tasks_coins)
                self.assertEqual(summary.coins_earned_this_run, tasks_coins)


if __name__ == "__main__":
    unittest.main()
