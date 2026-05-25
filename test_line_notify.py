from __future__ import annotations

import unittest
from types import SimpleNamespace

import line_notify
from line_notify import LineNotifier
from line_notify import QueuedLineNotifier
from line_notify import SubscriberStore


def make_message(index: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        date="2026/05/25",
        time=f"15:{index:02d}",
        company_id=f"23{index:02d}",
        company_name=f"\u6e2c\u8a66\u516c\u53f8{index}",
        subject=f"\u91cd\u5927\u8a0a\u606f\u6e2c\u8a66{index}",
    )


class FakeSubscriberStore:
    def __init__(self, user_ids: list[str], fail: bool = False) -> None:
        self.user_ids = user_ids
        self.fail = fail

    def active_user_ids(self) -> list[str]:
        if self.fail:
            raise RuntimeError("subscriber sheet failed")
        return self.user_ids


class FakeQueueStore:
    def __init__(self, due: bool = True) -> None:
        self.due = due
        self.messages: list[SimpleNamespace] = []
        self.marked: list[str] = []
        self.state: dict[str, str] = {}

    def enqueue(self, messages: list[SimpleNamespace]) -> int:
        existing = {message.data_key for message in self.messages}
        new_messages = [message for message in messages if message.data_key not in existing]
        self.messages.extend(new_messages)
        return len(new_messages)

    def pending_messages(self) -> list[SimpleNamespace]:
        return self.messages

    def is_due(self, interval_seconds: int) -> bool:
        return self.due

    def set_state(self, key: str, value: str) -> None:
        self.state[key] = value

    def mark_notified(self, data_keys: list[str], notified_at: str) -> None:
        self.marked.extend(data_keys)
        self.messages = [message for message in self.messages if message.data_key not in set(data_keys)]


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

    def test_broadcast_429_falls_back_to_subscribers_and_dedupes_targets(self) -> None:
        notifier = LineNotifier(
            enabled=True,
            channel_access_token="token",
            notify_mode="broadcast",
            target_ids=["U1", "U2"],
            subscriber_store=FakeSubscriberStore(["U2", "U3"]),
            broadcast_max_attempts=1,
        )
        pushes: list[tuple[str, str]] = []
        notifier._broadcast_text = lambda text: (_ for _ in ()).throw(RuntimeError("HTTP Error 429:"))
        notifier._push_text = lambda target_id, text: pushes.append((target_id, text))

        notifier.notify_new_messages([make_message(1)])

        self.assertEqual([target_id for target_id, _ in pushes], ["U1", "U2", "U3"])

    def test_broadcast_429_keeps_manual_fallback_when_subscriber_store_fails(self) -> None:
        notifier = LineNotifier(
            enabled=True,
            channel_access_token="token",
            notify_mode="broadcast",
            target_ids=["U1"],
            subscriber_store=FakeSubscriberStore([], fail=True),
            broadcast_max_attempts=1,
        )
        pushes: list[tuple[str, str]] = []
        notifier._broadcast_text = lambda text: (_ for _ in ()).throw(RuntimeError("HTTP Error 429:"))
        notifier._push_text = lambda target_id, text: pushes.append((target_id, text))

        notifier.notify_new_messages([make_message(1)])

        self.assertEqual([target_id for target_id, _ in pushes], ["U1"])

    def test_subscriber_store_reads_only_active_user_ids(self) -> None:
        original_gspread = line_notify.gspread

        class FakeWorksheet:
            def get_all_values(self) -> list[list[str]]:
                return [
                    ["user_id", "display_name", "status"],
                    ["U1", "A", "active"],
                    ["U2", "B", "inactive"],
                    ["U3", "C", ""],
                    ["U1", "A duplicate", "active"],
                    ["", "empty", "active"],
                ]

        class FakeSpreadsheet:
            def worksheet(self, title: str) -> FakeWorksheet:
                return FakeWorksheet()

        class FakeClient:
            def open_by_key(self, sheet_id: str) -> FakeSpreadsheet:
                return FakeSpreadsheet()

        class FakeGspread:
            @staticmethod
            def service_account(filename: str) -> FakeClient:
                return FakeClient()

        line_notify.gspread = FakeGspread
        try:
            store = SubscriberStore("sheet-id", "credentials.json")
            self.assertEqual(store.active_user_ids(), ["U1", "U3"])
        finally:
            line_notify.gspread = original_gspread

    def test_queued_notifier_waits_until_interval_is_due(self) -> None:
        queue = FakeQueueStore(due=False)
        notifier = LineNotifier(enabled=True, channel_access_token="token", notify_mode="broadcast")
        sent_texts: list[str] = []
        notifier._broadcast_text = sent_texts.append
        queued = QueuedLineNotifier(notifier, queue, interval_seconds=900)
        message = make_message(1)
        message.data_key = "key-1"

        sent = queued.notify_new_messages([message])

        self.assertFalse(sent)
        self.assertEqual(len(queue.messages), 1)
        self.assertEqual(sent_texts, [])

    def test_queued_notifier_marks_pending_messages_sent_when_due(self) -> None:
        queue = FakeQueueStore(due=True)
        notifier = LineNotifier(enabled=True, channel_access_token="token", notify_mode="broadcast")
        sent_texts: list[str] = []
        notifier._broadcast_text = sent_texts.append
        queued = QueuedLineNotifier(notifier, queue, interval_seconds=900)
        message = make_message(1)
        message.data_key = "key-1"

        sent = queued.notify_new_messages([message])

        self.assertTrue(sent)
        self.assertEqual(queue.marked, ["key-1"])
        self.assertEqual(queue.messages, [])
        self.assertEqual(len(sent_texts), 1)

    def test_queued_notifier_keeps_pending_messages_when_send_fails(self) -> None:
        queue = FakeQueueStore(due=True)
        notifier = LineNotifier(enabled=True, channel_access_token="token", notify_mode="broadcast", broadcast_max_attempts=1)
        notifier._broadcast_text = lambda text: (_ for _ in ()).throw(RuntimeError("HTTP Error 429:"))
        notifier._push_text = lambda target_id, text: (_ for _ in ()).throw(RuntimeError("HTTP Error 429:"))
        queued = QueuedLineNotifier(notifier, queue, interval_seconds=900)
        message = make_message(1)
        message.data_key = "key-1"

        sent = queued.notify_new_messages([message])

        self.assertFalse(sent)
        self.assertEqual([pending.data_key for pending in queue.messages], ["key-1"])
        self.assertEqual(queue.marked, [])


if __name__ == "__main__":
    unittest.main()
