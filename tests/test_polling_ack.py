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


if __name__ == "__main__":
    unittest.main()
