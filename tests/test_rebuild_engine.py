import unittest
from copy import deepcopy
import numpy as np
import pandas as pd
from v12.engine import backtest, plan, prepare, exit_reason


def prices(tail=None, n=220):
    values = [100.] * n + list(tail or [])
    return pd.DataFrame({"Date": pd.bdate_range("2024-01-01", periods=len(values)), "Close": values})


class EngineTests(unittest.TestCase):
    def test_exit_priority(self):
        p = {"full_pending": True, "d2_pending": True}
        self.assertEqual(exit_reason(p, 7), "조건부 전체 MOC")
        p["full_pending"] = False
        self.assertEqual(exit_reason(p, 7), "D2 손절")
        p["d2_pending"] = False
        self.assertEqual(exit_reason(p, 7), "D7 MOC")
        self.assertEqual(exit_reason(p, 6), "LOC +3.2%")

    def test_vol20_sample_not_annualized(self):
        raw = prices([100., 80., 130., 75., 110.])
        result = prepare(raw)
        expected = np.log(raw.Close / raw.Close.shift()).iloc[-20:].std(ddof=1)
        self.assertAlmostEqual(result.vol20.iloc[-1], expected, places=13)

    def test_cash_and_slots(self):
        result = backtest(prices([90, 81, 72, 70, 80, 90, 95, 80]))
        daily = result["equity"]
        self.assertTrue((daily.Slots <= 6).all())
        self.assertTrue((daily.Buy_Slots <= 2).all())
        self.assertTrue((daily.Cash_USD >= -1e-7).all())
        self.assertTrue((daily.Buy_USD <= daily.Cash_Before_Sales + 1e-7).all())
        self.assertTrue((daily.Sale_USD > 0).any())

    def test_d2_exit_is_next_session_and_overrides_target(self):
        raw = prices([100., 89., 120., 120.], n=201)
        result = backtest(raw)
        first = result["trades"].query("slot_id == 'V12-000001'").iloc[0]
        self.assertEqual(first.holding_days, 3)
        self.assertEqual(first.reason, "D2 손절")
        self.assertEqual(first.exit_price, 120.)

    def test_future_changes_do_not_change_prefix(self):
        raw = prices([90, 80, 75, 100, 110, 95, 80, 90])
        shorter = backtest(raw.iloc[:-3])["equity"]
        longer = backtest(raw)["equity"].iloc[:len(shorter)]
        pd.testing.assert_frame_equal(shorter, longer)

    def test_previous_down_streak_blocks_both_buys(self):
        raw = prices([99, 98, 97, 96, 80])
        daily = backtest(raw)["equity"]
        self.assertEqual(daily.iloc[-1].Buy_Slots, 0)
        self.assertTrue(daily.iloc[-1].Paused)

    def test_plan_does_not_release_planned_sales(self):
        raw = prices()
        holdings = [{"slot_id": str(i), "entry_date": str(raw.Date.iloc[-10].date()),
                     "entry_price": "100", "quantity": "1"} for i in range(6)]
        before = deepcopy(holdings)
        result = plan(raw, holdings, 1000, raw.Date.iloc[-1] + pd.Timedelta(days=1))
        self.assertEqual(len(result["sells"]), 6)
        self.assertTrue(all(s["사유"] == "D7 MOC" for s in result["sells"]))
        self.assertFalse(result["buys"])
        self.assertEqual(holdings, before)

    def test_exact_vol_threshold(self):
        # Build 20 returns with exactly requested sample std. Only the two
        # most recent sessions count toward this lot's D2 trigger.
        for vol, expected in [(.0999, False), (.1000, True), (.10001, True)]:
            returns = np.array([1., -1.] * 10)
            returns *= vol / returns.std(ddof=1)
            raw = prices(n=201)
            base = 100 * np.exp(np.cumsum(returns))
            tail = pd.DataFrame({"Date": pd.bdate_range(raw.Date.iloc[-1] + pd.Timedelta(days=1), periods=20), "Close": base})
            raw = pd.concat([raw, tail], ignore_index=True)
            holding = [{"slot_id": "A", "entry_date": str(raw.Date.iloc[-3].date()), "entry_price": "1000", "quantity": "1"}]
            result = plan(raw, holding, 0, raw.Date.iloc[-1] + pd.Timedelta(days=1))
            self.assertEqual(result["full_pending"], expected)

    def test_invalid_dates_and_prices_rejected(self):
        for raw in [prices().assign(Close=0), pd.concat([prices(), prices().iloc[[-1]]])]:
            with self.assertRaises(ValueError):
                prepare(raw)

    def test_unmatched_actual_date_blocks_new_buys(self):
        raw = prices()
        holding = [{"slot_id": "A", "entry_date": "2020-01-01", "entry_price": "100", "quantity": "1"}]
        result = plan(raw, holding, 1000, raw.Date.iloc[-1] + pd.Timedelta(days=1))
        self.assertTrue(result["issues"])
        self.assertFalse(result["buys"])


if __name__ == "__main__":
    unittest.main()
