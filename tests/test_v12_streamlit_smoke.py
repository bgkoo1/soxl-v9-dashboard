"""Render the real V12 order console through Streamlit's testing runtime."""
import unittest
from streamlit.testing.v1 import AppTest

APP = """
import pandas as pd
from SOXL_V12_backtest import run_v12_backtest
from v12_full_order_console import render_v12_full_console

data=pd.read_csv("SOXL_adjusted.csv")
data["Date"]=pd.to_datetime(data["Date"])
data=data[data["Date"]<=pd.Timestamp("2026-10-23")].copy()
raw=run_v12_backtest(data,initial_capital=100000000)
state={"schema_version":1,"positions":[
    {"slot_id":"actual-1","entry_date":"2026-10-20","entry_price":"100",
     "quantity":"12","source":"manual","model_exit":"직접 매수","note":""}
],"closed_trades":[],"cashflows":[]}
render_v12_full_console(data,raw,state,None,None)
"""

class Smoke(unittest.TestCase):
    def test_order_cards_and_management_render(self):
        app=AppTest.from_string(APP,default_timeout=90).run()
        self.assertFalse(app.exception, str([x.message for x in app.exception]))
        md=" ".join(str(x.value) for x in app.markdown)
        self.assertIn("TODAY'S ACTION",md)
        self.assertIn("보유 슬롯 미리보기",md)
        self.assertIn("상세 주문",md)
        columns=[set(str(c) for c in table.value.columns) for table in app.dataframe]
        self.assertTrue(any("LOC 목표가" in c and "목표 매도금액" in c for c in columns))
        labels=[x.label for x in app.tabs]
        self.assertIn("실제 매도 등록",labels)
        self.assertIn("매도 취소·복구",labels)
        self.assertIn("입출금 관리",labels)

if __name__=="__main__":
    unittest.main()
