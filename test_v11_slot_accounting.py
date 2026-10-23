"""Offline tests for V11 broker-verified FIFO accounting."""
import unittest
from uuid import uuid4
from decimal import Decimal
from v11_trade_journal_schema import empty_journal
from v11_slot_accounting import summarize_slots

def fill(kind, qty, price, slot=1, source="BROKER_VERIFIED", fee="0", minute=0):
    buy = kind == "BUY_FILLED"
    return {"event_id": str(uuid4()), "occurred_at": f"2026-10-24T10:{minute:02d}:00+09:00",
            "slot_id": slot, "event_type": kind, "source": source,
            "order_type": "LOC" if buy else "MOC",
            "side": "BUY" if buy else "SELL", "order_state": "FILLED",
            "quantity": str(qty), "price": str(price), "fee_usd": str(fee)}

class TestSlotAccounting(unittest.TestCase):
    def journal(self, *events):
        j = empty_journal()
        j["events"] = list(events)
        return j

    def test_buy_and_partial_sell_fifo_with_fees(self):
        result = summarize_slots(self.journal(
            fill("BUY_FILLED", 10, 20, fee="1", minute=0),
            fill("BUY_FILLED", 10, 30, fee="1", minute=1),
            fill("SELL_FILLED", 15, 40, fee="2", minute=2)))
        s = result["slots"][0]
        self.assertEqual(Decimal(s["quantity"]), Decimal("5"))
        self.assertEqual(Decimal(s["remaining_cost_usd"]), Decimal("150.5"))
        self.assertEqual(Decimal(s["realized_pnl_usd"]), Decimal("347.5"))
        self.assertEqual(Decimal(result["total_realized_pnl_usd"]), Decimal("347.5"))

    def test_reject_oversell(self):
        with self.assertRaises(ValueError):
            summarize_slots(self.journal(fill("SELL_FILLED", 1, 40)))

    def test_dry_run_not_mixed_into_actuals(self):
        result = summarize_slots(self.journal(fill("BUY_FILLED", 50, 40, source="STRATEGY_DRY_RUN")))
        self.assertEqual(result["slots"][0]["quantity"], "0")

    def test_slots_are_separate(self):
        result = summarize_slots(self.journal(fill("BUY_FILLED", 2, 20, slot=1),
                                              fill("BUY_FILLED", 3, 30, slot=2, minute=1)))
        self.assertEqual(result["slots"][0]["quantity"], "2")
        self.assertEqual(result["slots"][1]["quantity"], "3")

    def test_negative_fee_rejected(self):
        with self.assertRaises(ValueError):
            summarize_slots(self.journal(fill("BUY_FILLED", 2, 20, fee="-1")))

if __name__ == "__main__":
    unittest.main()
