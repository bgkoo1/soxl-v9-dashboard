"""Regression tests for the V11 strategy rules. Run with python -m unittest discover -s tests."""
import unittest
import numpy as np
import pandas as pd

from SOXL_V11_backtest import run_v11_backtest, run_v11_live_transition
from SOXL_V10_backtest import run_v10_backtest


def make_data(changes):
    dates = pd.bdate_range("2025-01-02", periods=245)
    close = np.full(len(dates), 100.0)
    for index, price in changes.items():
        close[index] = price
    frame = pd.DataFrame({"Date": dates, "Open": close, "High": close, "Low": close,
                          "Close": close, "Volume": np.full(len(dates), 1_000_000)})
    return frame


class V11RulesTest(unittest.TestCase):
    def test_tb3_stays_disabled(self):
        data = make_data({})
        result = run_v11_backtest(data, start_date=data.Date.iloc[210], end_date=data.Date.iloc[230])
        self.assertFalse(result["stats"]["enable_tb3"])
        self.assertFalse(result["daily"]["TB3_State"].any())

    def test_day_two_loss_flags_day_three_moc(self):
        data = make_data({212: 99., 213: 87., 214: 85.})
        result = run_v11_backtest(data, start_date=data.Date.iloc[211], end_date=data.Date.iloc[218])
        # Entry day=211 (index offset zero); day2=213, exit day3=214.
        exits = result["trades"]
        selected = exits[(exits.Entry_Date == data.Date.iloc[211]) &
                         (exits.Exit_Type == "D2_CUT_NEXT_MOC")]
        self.assertFalse(selected.empty)
        self.assertTrue((pd.to_datetime(selected.Exit_Date) == data.Date.iloc[214]).all())

    def test_legacy_slots_preserved_at_switch(self):
        data = make_data({})
        switch = data.Date.iloc[215]
        legacy = run_v10_backtest(data, start_date=data.Date.iloc[205], end_date=data.Date.iloc[214])
        transitioned = run_v11_live_transition(
            data, start_date=data.Date.iloc[205], end_date=data.Date.iloc[214],
            transition_date=switch,
        )
        self.assertEqual(
            [p["position_id"] for p in legacy["open_positions"]],
            [p["position_id"] for p in transitioned["open_positions"]],
        )
        self.assertEqual(float(legacy["equity"]["Equity"].iloc[-1]),
                         float(transitioned["equity"]["Equity"].iloc[-1]))

    def test_legacy_ids_survive_first_v11_session(self):
        data = make_data({})
        switch = data.Date.iloc[215]
        legacy = run_v10_backtest(data, start_date=data.Date.iloc[210], end_date=data.Date.iloc[214])
        old_ids = {p["position_id"] for p in legacy["open_positions"]}
        transitioned = run_v11_live_transition(
            data, start_date=data.Date.iloc[210], end_date=switch, transition_date=switch)
        still_open = {p["position_id"] for p in transitioned["open_positions"]}
        self.assertTrue(old_ids.issubset(still_open))
        self.assertTrue(any(str(x).startswith("v11-") for x in still_open))

    def test_four_down_closes_block_all_new_buys(self):
        data = make_data({211: 99., 212: 98., 213: 97., 214: 96., 215: 95., 216: 97.})
        result = run_v11_backtest(data, start_date=data.Date.iloc[210], end_date=data.Date.iloc[218])
        days = result["daily"].set_index("Date")
        self.assertTrue(bool(days.loc[data.Date.iloc[215], "Pause_State"]))
        self.assertEqual(int(days.loc[data.Date.iloc[215], "Buy_Slots"]), 0)
        self.assertTrue(bool(days.loc[data.Date.iloc[216], "Pause_State"]))
        self.assertFalse(bool(days.loc[data.Date.iloc[217], "Pause_State"]))


if __name__ == "__main__":
    unittest.main()
