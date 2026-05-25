from __future__ import annotations

import unittest
from types import SimpleNamespace

from line_notify import LineNotifier


def make_message(index: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        date="2026/05/25",
        time=f"15:{index:02d}",
        company_id=f"23{index:02d}",
        company_name=f"\u6e2c\u8a66\u516c\u53f8{index}",
        subject=f"\u91cd\u5927\u8a0a\u606f\u6e2c\u8a66{index}",
    )


class LineNotifyBroadcastTest(unittest.TestCase):
    def test_broadcast_sends_one_summary_without_fallback(self) -> None:
        notifier = LineNotifier(enabled=True, channel_access_token="token", notify_mode="broadcast", target_ids=["U1"])
        broadcasts: list[str] = []
        pushes: list[tuple[str, str]] = []
        notifier._broadcast_text = broadcasts.append
        notifier._push_text = lambda target_id, text: pushes.append((target_id, text))

        notifier.notify_new_messages([make_message(1), make_message(2)])

        self.assertEqual(len(broadcasts), 1)
        self.assertIn("\u76ee\u524d\u6709 2 \u7b46", broadcasts[0])
        self.assertEqual(pushes, [])

    def test_broadcast_429_retries_once_then_succeeds(self) -> None:
        notifier = LineNotifier(
            enabled=True,
            channel_access_token="token",
            notify_mode="broadcast",
            target_ids=["U1"],
            broadcast_max_attempts=2,
            broadcast_retry_seconds=0,
        )
        attempts = 0
        pushes: list[tuple[str, str]] = []

        def broadcast_once_then_success(text: str) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("HTTP Error 429:")

        notifier._broadcast_text = broadcast_once_then_success
        notifier._push_text = lambda target_id, text: pushes.append((target_id, text))

        notifier.notify_new_messages([make_message(1)])

        self.assertEqual(attempts, 2)
        self.assertEqual(pushes, [])

    def test_broadcast_429_falls_back_to_configured_targets(self) -> None:
        notifier = LineNotifier(
            enabled=True,
            channel_access_token="token",
            notify_mode="broadcast",
            target_ids=["U1", "U2"],
            broadcast_max_attempts=2,
            broadcast_retry_seconds=0,
        )
        attempts = 0
        pushes: list[tuple[str, str]] = []

        def always_rate_limited(text: str) -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("HTTP Error 429:")

        notifier._broadcast_text = always_rate_limited
        notifier._push_text = lambda target_id, text: pushes.append((target_id, text))

        notifier.notify_new_messages([make_message(1), make_message(2)])

        self.assertEqual(attempts, 2)
        self.assertEqual([target_id for target_id, _ in pushes], ["U1", "U2"])
        self.assertTrue(all("\u76ee\u524d\u6709 2 \u7b46" in text for _, text in pushes))


if __name__ == "__main__":
    unittest.main()
