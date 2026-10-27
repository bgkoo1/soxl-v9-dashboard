"""V12 regression: preserve V10/V11 and safeguard READ ONLY migration."""
import unittest
from pathlib import Path
import pandas as pd
from SOXL_V12_backtest import run_v12_backtest

ROOT=Path(__file__).resolve().parents[1]

class V12Regression(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw=pd.read_csv(ROOT/"SOXL_adjusted.csv")
        cls.raw["Date"]=pd.to_datetime(cls.raw["Date"])
        cls.raw=cls.raw.loc[cls.raw["Date"]<=pd.Timestamp("2026-10-23")].copy()
        cls.result=run_v12_backtest(cls.raw)

    def test_six_slot_params(self):
        stats=self.result["stats"]
        self.assertEqual(stats["position_divisor"],6)
        self.assertEqual(stats["max_positions_limit"],6)
        self.assertAlmostEqual(stats["target_return"],.032)
        self.assertEqual(stats["d2_cut"],-.10)
        self.assertEqual(stats["holding_days_limit"],7)
        self.assertEqual(stats["v12_vol_window"],20)
        self.assertAlmostEqual(stats["v12_vol_threshold"],.10)
        self.assertFalse(stats["enable_tb3"])
        self.assertLessEqual(stats["max_open_positions"],6)

    def test_reproduce_research_metrics(self):
        s=self.result["stats"]
        self.assertAlmostEqual(s["cagr"],.67274,delta=.002)
        self.assertAlmostEqual(s["mdd"],-.41842,delta=.002)
        self.assertEqual(s["v12_full_liquidation_signals"],23)

    def test_full_exit_timing(self):
        df=self.result["daily"]
        trades=self.result["trades"]
        signals=df.loc[df["V12_Full_Liquidation_Trigger"],"Date"]
        self.assertFalse(signals.empty)
        self.assertTrue((df.loc[df["V12_Full_Liquidation_Trigger"],"V12_Full_Liquidation_Pending"]>0).all())
        full=trades.loc[trades["Exit_Type"]=="V12_FULL_NEXT_MOC"].copy()
        self.assertFalse(full.empty)
        dates=set(self.result["signal_data"]["Date"].dt.normalize())
        for _, row in full.iterrows():
            day=pd.Timestamp(row["Exit_Date"]).normalize()
            self.assertIn(day,dates)
            self.assertNotIn(day,set(signals.dt.normalize()))

    def test_no_state_mutation(self):
        # The model is pure and must not include broker order transmission.
        source=(ROOT/"SOXL_V12_backtest.py").read_text(encoding="utf-8")
        self.assertNotIn("requests.post(",source)
        self.assertNotIn("live_portfolio.enc",source)
        self.assertNotIn("/trading/v1/overseas/orders/buy",source)
        self.assertNotIn("/trading/v1/overseas/orders/sell",source)

if __name__=="__main__":
    unittest.main()
