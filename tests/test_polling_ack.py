import unittest
import tempfile
from pathlib import Path
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
        health = {
            "state": "stopped",
            "bot_authenticated": False,
            "last_successful_poll_at": None,
            "last_successful_poll_epoch": None,
            "last_error_code": None,
        }
        with patch.object(bot_telegram, "TELEGRAM_BOT_TOKEN", ""), patch.dict(
            bot_telegram.POLL_HEALTH, health, clear=True
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


class FailedDeliveryTests(unittest.TestCase):
    def test_failed_telegram_send_raises(self):
        failure = {"ok": False, "error_code": 500, "description": "temporary failure"}
        with patch.object(bot_telegram, "tg", return_value=failure):
            with self.assertRaisesRegex(RuntimeError, "sendMessage failed"):
                bot_telegram.send_message(123, "resposta final")

    def test_processing_failure_is_not_acknowledged(self):
        update = {"update_id": 88, "message": {"text": "teste"}}
        with patch.object(bot_telegram, "handle_update", side_effect=RuntimeError("send failed")):
            self.assertFalse(bot_telegram.process_update_safe(update))


class DurableIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.data_file = Path(self.tempdir.name) / "data.json"
        self.db = {"users": {}, "referrals": {}, "processed_updates": {}}
        self.db_patch = patch.object(bot_telegram, "DB", self.db)
        self.data_patch = patch.object(bot_telegram, "DATA_FILE", self.data_file)
        self.db_patch.start()
        self.data_patch.start()
        self.addCleanup(self.db_patch.stop)
        self.addCleanup(self.data_patch.stop)

    def test_duplicate_delivered_update_does_not_reprocess_or_resend(self):
        update = {"update_id": 501}
        calls = []
        def handler(_update):
            calls.append("processed")
            bot_telegram.DB["users"]["counter"] = bot_telegram.DB["users"].get("counter", 0) + 1
            bot_telegram.send_message(123, "resposta única")

        with patch.object(bot_telegram, "handle_update", side_effect=handler), patch.object(
            bot_telegram, "tg", return_value={"ok": True}
        ) as telegram:
            self.assertTrue(bot_telegram.process_update_safe(update))
            telegram.reset_mock()
            self.assertTrue(bot_telegram.process_update_safe(update))
            telegram.assert_not_called()

        self.assertEqual(calls, ["processed"])
        self.assertEqual(self.db["users"]["counter"], 1)
        self.assertTrue(self.db["processed_updates"]["501"]["delivered"])

    def test_failed_delivery_retries_outbox_without_reprocessing(self):
        update = {"update_id": 502}
        calls = []
        def handler(_update):
            calls.append("processed")
            bot_telegram.DB["users"]["counter"] = 1
            bot_telegram.send_message(123, "resposta pendente")

        outcomes = iter([
            {"ok": False, "error_code": 500, "description": "temporary failure"},
            {"ok": True},
        ])
        with patch.object(bot_telegram, "handle_update", side_effect=handler), patch.object(
            bot_telegram, "tg", side_effect=lambda *args, **kwargs: next(outcomes)
        ):
            self.assertFalse(bot_telegram.process_update_safe(update))
            self.assertTrue(bot_telegram.process_update_safe(update))

        self.assertEqual(calls, ["processed"])
        self.assertEqual(self.db["users"]["counter"], 1)
        self.assertTrue(self.db["processed_updates"]["502"]["delivered"])

    def test_partial_chunk_delivery_does_not_repeat_confirmed_chunk(self):
        update = {"update_id": 503}
        def handler(_update):
            bot_telegram.send_message(123, "primeira parte\\n" + ("x" * 4100))

        # The first chunk succeeds, the second fails, then the retry succeeds.
        outcomes = iter([
            {"ok": True},
            {"ok": False, "error_code": 500, "description": "temporary failure"},
            {"ok": True},
        ])
        sent_texts = []
        def telegram(*args, **kwargs):
            sent_texts.append(kwargs["text"])
            return next(outcomes)

        with patch.object(bot_telegram, "handle_update", side_effect=handler), patch.object(
            bot_telegram, "tg", side_effect=telegram
        ):
            self.assertFalse(bot_telegram.process_update_safe(update))
            self.assertTrue(bot_telegram.process_update_safe(update))

        self.assertEqual(len(sent_texts), 3)
        self.assertEqual(sent_texts[1], sent_texts[2])
        self.assertNotEqual(sent_texts[0], sent_texts[1])
        self.assertTrue(self.db["processed_updates"]["503"]["delivered"])
        self.assertTrue(all(item["delivered"] for item in self.db["processed_updates"]["503"]["outbox"]))



class TelegramTimeoutSeparationTests(unittest.TestCase):
    def test_http_timeout_and_telegram_long_poll_timeout_are_separate(self):
        response = type("Response", (), {"status_code": 200, "json": lambda self: {"ok": True}})()
        with patch.object(bot_telegram, "TELEGRAM_API", "https://api.telegram.org/botTEST"), patch.object(
            bot_telegram.HTTP, "post", return_value=response
        ) as post:
            result = bot_telegram.tg(
                "getUpdates",
                request_timeout=35,
                timeout=25,
                allowed_updates=["message"],
            )
        self.assertTrue(result["ok"])
        post.assert_called_once_with(
            "https://api.telegram.org/botTEST/getUpdates",
            json={"timeout": 25, "allowed_updates": ["message"]},
            timeout=35,
        )



if __name__ == "__main__":
    unittest.main()
