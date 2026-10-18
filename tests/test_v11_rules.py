"""Regression tests for the V11 strategy rules. Run with python -m unittest discover -s tests."""
import unittest
import numpy as np
import pandas as pd

from SOXL_V11_backtest import run_v11_backtest


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
