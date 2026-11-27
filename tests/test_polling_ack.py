import unittest
from unittest.mock import patch

import bot_telegram


class PollingAcknowledgementTests(unittest.TestCase):
    def test_offset_advances_only_after_success(self):
        updates = [{"update_id": 10}, {"update_id": 11}, {"update_id": 12}]
        outcomes = iter([True, True, False])
        with patch.object(bot_telegram, "process_update_safe", side_effect=lambda update: next(outcomes)):
            self.assertEqual(bot_telegram.process_updates_sequentially(updates, None), 12)

    def test_first_failed_update_does_not_advance_offset(self):
        updates = [{"update_id": 21}]
        with patch.object(bot_telegram, "process_update_safe", return_value=False):
            self.assertIsNone(bot_telegram.process_updates_sequentially(updates, None))

    def test_empty_batch_preserves_offset(self):
        with patch.object(bot_telegram, "process_update_safe") as process:
            self.assertEqual(bot_telegram.process_updates_sequentially([], 40), 40)
            process.assert_not_called()


class PollingReadinessTests(unittest.TestCase):
    def test_missing_token_is_not_ready(self):
        with patch.object(bot_telegram, "TELEGRAM_BOT_TOKEN", ""), patch.dict(
            bot_telegram.POLL_HEALTH, {}, clear=True
        ):
            status, payload = bot_telegram.polling_health_snapshot(now_epoch=1000)
        self.assertEqual(status, 503)
        self.assertEqual(payload["telegram_polling"]["reason"], "bot_token_not_configured")

    def test_authenticated_but_no_successful_poll_is_not_ready(self):
        health = {
            "state": "polling",
            "bot_authenticated": True,
            "last_successful_poll_at": None,
            "last_successful_poll_epoch": None,
            "last_error_code": None,
        }
        with patch.object(bot_telegram, "TELEGRAM_BOT_TOKEN", "test-token"), patch.dict(
            bot_telegram.POLL_HEALTH, health, clear=True
        ):
            status, payload = bot_telegram.polling_health_snapshot(now_epoch=1000)
        self.assertEqual(status, 503)
        self.assertEqual(payload["telegram_polling"]["reason"], "no_successful_poll_yet")
        self.assertNotIn("test-token", str(payload))

    def test_stale_polling_heartbeat_is_not_ready(self):
        health = {
            "state": "polling",
            "bot_authenticated": True,
            "last_successful_poll_at": "test-time",
            "last_successful_poll_epoch": 909,
            "last_error_code": None,
        }
        with patch.object(bot_telegram, "TELEGRAM_BOT_TOKEN", "test-token"), patch.dict(
            bot_telegram.POLL_HEALTH, health, clear=True
        ):
            status, payload = bot_telegram.polling_health_snapshot(now_epoch=1000)
        self.assertEqual(status, 503)
        self.assertEqual(payload["telegram_polling"]["reason"], "polling_heartbeat_stale")
        self.assertEqual(payload["telegram_polling"]["last_successful_poll_age_seconds"], 91)

    def test_recent_authenticated_poll_is_ready(self):
        health = {
            "state": "polling",
            "bot_authenticated": True,
            "last_successful_poll_at": "test-time",
            "last_successful_poll_epoch": 950,
            "last_error_code": None,
        }
        with patch.object(bot_telegram, "TELEGRAM_BOT_TOKEN", "test-token"), patch.dict(
            bot_telegram.POLL_HEALTH, health, clear=True
        ):
            status, payload = bot_telegram.polling_health_snapshot(now_epoch=1000)
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ready")
        self.assertEqual(payload["telegram_polling"]["last_successful_poll_age_seconds"], 50)
        self.assertNotIn("test-token", str(payload))


if __name__ == "__main__":
    unittest.main()
