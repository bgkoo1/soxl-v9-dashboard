from pathlib import Path
from unittest.mock import patch
import unittest
from streamlit.testing.v1 import AppTest
from test_broker_account import snapshot, SETTINGS
from v12.meritz import ReadError

ROOT = Path(__file__).resolve().parents[1]
QUOTE = {"price":"101.2", "previous_close":"100", "change_pct":"1.2", "session":"정규장 최근가",
         "delayed":False, "source":"메리츠 해외주식 시세", "received_at":"2026-10-28T10:00:00+00:00"}

class BrokerAppTests(unittest.TestCase):
    def test_live_account_quote_daily_fills_and_independent_failures(self):
        app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=90)
        app.secrets.update({**SETTINGS, "DASHBOARD_PASSWORD":"test-password"})
        app.session_state.authenticated = True
        source = snapshot()
        activity = {k:source[k] for k in ["start","end","fills","outstanding","transactions"]}
        activity["history_asof"] = source["fetched_at"]
        with patch("v12.live_desk.Client") as client:
            client.return_value.balances.return_value = source
            client.return_value.activity.return_value = activity
            client.return_value.quote.return_value = QUOTE
            app.run()
            self.assertFalse(app.exception, [x.message for x in app.exception])
            self.assertEqual(next(m for m in app.metric if m.label == "USD 예수금").value, "$1,000.00")
            self.assertEqual(next(m for m in app.metric if m.label == "SOXL 현재가").value, "$101.2000")
            self.assertTrue(any(x.label == "체결 확인일" for x in app.selectbox))
            self.assertFalse(any(b.label == "매수 체결 반영" for b in app.button))
            next(x for x in app.selectbox if x.label == "체결일시 기준 시간대").select("Asia/Seoul").run()
            self.assertFalse(app.exception, [x.message for x in app.exception])
            tables = [set(t.value.columns) for t in app.dataframe]
            self.assertTrue(any("LOC 목표가($)" in cols and "다음 행동" in cols for cols in tables))
            self.assertTrue(any("매수수량" in cols and "매도수량" in cols for cols in tables))
            self.assertEqual(client.return_value.balances.call_count, 1)
            client.return_value.activity.side_effect = ReadError("체결 조회 실패")
            next(b for b in app.button if b.label == "계좌·시세 지금 새로고침").click().run()
            self.assertFalse(app.exception, [x.message for x in app.exception])
            self.assertTrue(any("일별 체결내역" in x.value for x in app.error))
            self.assertEqual(next(m for m in app.metric if m.label == "USD 예수금").value, "$1,000.00")
            self.assertEqual(next(m for m in app.metric if m.label == "SOXL 현재가").value, "$101.2000")
            self.assertTrue(any("최신 조회가 실패" in m.value for m in app.markdown))

    def test_unconfigured_account_shows_visible_menu_and_unknown_values(self):
        app = AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=90).run()
        self.assertFalse(app.exception, [x.message for x in app.exception])
        self.assertIn("메리츠 실계좌", next(x for x in app.radio if x.key == "workspace_mode").options)
        self.assertEqual(next(m for m in app.metric if m.label == "USD 예수금").value, "—")
        self.assertTrue(any("MERITZ_APP_KEY" in x.value for x in app.error))

if __name__ == "__main__":
    unittest.main()
