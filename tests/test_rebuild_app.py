import unittest
from pathlib import Path
from streamlit.testing.v1 import AppTest


class AppTests(unittest.TestCase):
    def test_fresh_dashboard_and_buy_sell_cycle(self):
        app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "streamlit_app.py"), default_timeout=90).run()
        self.assertFalse(app.exception, [x.message for x in app.exception])
        next(x for x in app.radio if x.key == "workspace_mode").set_value("수동 원장·백테스트").run()
        self.assertIn("오늘의 주문계획", [t.label for t in app.tabs])
        self.assertIn("V12 백테스트", [t.label for t in app.tabs])
        self.assertTrue(any(m.label == "사용 중인 슬롯" for m in app.metric))
        next(x for x in app.checkbox if x.key == "buy_confirm").check()
        next(x for x in app.button if x.label == "매수 체결 반영").click().run()
        self.assertFalse(app.exception, [x.message for x in app.exception])
        self.assertEqual(len(app.session_state.ledger["positions"]), 1)
        next(x for x in app.checkbox if x.label == "증권사에서 실제 매도 체결을 확인했습니다").check()
        next(x for x in app.button if x.label == "매도 체결 반영").click().run()
        self.assertFalse(app.exception, [x.message for x in app.exception])
        self.assertEqual(len(app.session_state.ledger["positions"]), 0)
        self.assertEqual(len(app.session_state.ledger["closed_trades"]), 1)
        next(x for x in app.checkbox if x.label == "잘못 기록된 매도를 취소하고 수량을 복구합니다").check()
        next(x for x in app.button if x.label == "선택한 매도 취소").click().run()
        self.assertFalse(app.exception, [x.message for x in app.exception])
        self.assertEqual(len(app.session_state.ledger["positions"]), 1)
        self.assertEqual(len(app.session_state.ledger["closed_trades"]), 0)


if __name__ == "__main__":
    unittest.main()
