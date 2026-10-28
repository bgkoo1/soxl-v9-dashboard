from pathlib import Path
from unittest.mock import patch
import unittest
from streamlit.testing.v1 import AppTest
from test_broker_account import snapshot, SETTINGS
from v12.meritz import ReadError


class BrokerAppTests(unittest.TestCase):
    def test_account_values_replace_manual_mode_and_failed_refresh_blocks(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py"), default_timeout=90)
        app.secrets.update({**SETTINGS, "DASHBOARD_PASSWORD": "test-password"})
        app.session_state.authenticated = True
        with patch("v12.broker_ui.Client") as client:
            client.return_value.snapshot.return_value = snapshot()
            app.run()
            self.assertFalse(app.exception, [x.message for x in app.exception])
            self.assertEqual(next(m for m in app.metric if m.label == "메리츠 USD 예수금").value, "$1,000.00")
            next(x for x in app.selectbox if x.label == "체결일시 기준 시간대").select("Asia/Seoul").run()
            self.assertFalse(app.exception, [x.message for x in app.exception])
            self.assertFalse(any(b.label == "매수 체결 반영" for b in app.button))
            self.assertTrue(any("SOXL 체결 복원 수량과 메리츠 잔고 일치" in x.value for x in app.success))
            self.assertEqual(client.return_value.snapshot.call_count, 1)
            client.return_value.snapshot.side_effect = ReadError("history: 네트워크 연결 또는 시간 초과입니다.")
            next(b for b in app.button if b.label == "메리츠 계좌 새로고침").click().run()
            self.assertFalse(app.exception, [x.message for x in app.exception])
            self.assertTrue(any("정상 조회하지 못했습니다" in x.value for x in app.error))
            self.assertFalse(any(b.label == "주문계획 CSV" for b in app.button))


if __name__ == "__main__":
    unittest.main()
