"""Offline tests for the fresh-start gate; no credentials or broker calls."""
import unittest
from v11_fresh_start_broker_snapshot import normalize_full_account_snapshot, evaluate_snapshot_readiness

def sample():
    return {"as_of":"2026-10-23T16:00:00-04:00",
            "holdings_complete":True,"orders_complete":True,"cash_complete":True,
            "holdings":[],"orders":[],"available_cash":"70000000"}

class FreshStartTests(unittest.TestCase):
    def test_flat_is_still_not_authorized(self):
        result=evaluate_snapshot_readiness(sample())
        self.assertFalse(result["live_orders_enabled"])
        self.assertFalse(result["review_ready"])
        self.assertGreater(len(result["blocking_reasons"]),0)

    def test_missing_completeness_fails(self):
        data=sample()
        data.pop("orders_complete")
        with self.assertRaises(ValueError):
            normalize_full_account_snapshot(data)

    def test_any_other_holding_blocks(self):
        data=sample()
        data["holdings"]=[{"symbol":"AAPL","quantity":"1"}]
        result=evaluate_snapshot_readiness(data)
        self.assertFalse(result["review_ready"])
        self.assertTrue(any("보유 종목" in x for x in result["blocking_reasons"]))

    def test_cancel_pending_blocks(self):
        data=sample()
        data["orders"]=[{"symbol":"SOXL","status":"CANCEL_REQUESTED","remaining_quantity":"0"}]
        result=evaluate_snapshot_readiness(data)
        self.assertFalse(result["review_ready"])
        self.assertTrue(any("미체결" in x for x in result["blocking_reasons"]))

    def test_bad_values_fail_closed(self):
        for bad in ("NaN","-1","Infinity",True,None):
            with self.subTest(value=bad):
                data=sample()
                data["available_cash"]=bad
                with self.assertRaises(ValueError):
                    normalize_full_account_snapshot(data)

    def test_unknown_order_status_fails(self):
        data=sample()
        data["orders"]=[{"symbol":"SOXL","status":"MYSTERY","remaining_quantity":"0"}]
        with self.assertRaises(ValueError):
            normalize_full_account_snapshot(data)

if __name__=="__main__":
    unittest.main()
