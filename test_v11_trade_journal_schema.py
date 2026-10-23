"""Offline tests for the V11 event journal schema."""
import unittest
from uuid import uuid4
from v11_trade_journal_schema import empty_journal, validate_journal, validate_event

class JournalTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(validate_journal(empty_journal())["events"], [])

    def test_fill_event(self):
        event = {"event_id": str(uuid4()), "occurred_at": "2026-10-24T10:00:00+09:00",
                 "slot_id": 1, "event_type": "BUY_FILLED", "source": "BROKER_VERIFIED",
                 "order_type": "LOC", "side": "BUY", "order_state": "FILLED",
                 "quantity": "20", "price": "40.25"}
        self.assertEqual(validate_event(event)["quantity"], "20")

    def test_missing_price_rejected(self):
        event = {"event_id": str(uuid4()), "occurred_at": "2026-10-24T10:00:00+09:00",
                 "slot_id": 1, "event_type": "BUY_FILLED", "source": "BROKER_VERIFIED",
                 "order_type": "LOC", "side": "BUY", "order_state": "FILLED",
                 "quantity": "20"}
        with self.assertRaises(ValueError):
            validate_event(event)

    def test_live_orders_cannot_be_enabled(self):
        journal = empty_journal()
        journal["live_orders_enabled"] = True
        with self.assertRaises(ValueError):
            validate_journal(journal)

    def test_no_v10_strategy_accepted(self):
        journal = empty_journal()
        journal["strategy"] = "SOXL_V10"
        with self.assertRaises(ValueError):
            validate_journal(journal)

if __name__ == "__main__":
    unittest.main()
