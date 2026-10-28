import unittest
from copy import deepcopy
from unittest.mock import patch
from test_broker_account import snapshot, Session, response, Response, SETTINGS
from v12.meritz import Client, ReadError
from v12.desk_model import execution_frames


class DeskTests(unittest.TestCase):
    def test_daily_buy_sell_execution_values_and_ny_dates(self):
        activity = snapshot()
        activity["fills"].append({**activity["fills"][0], "id": "sell2", "side": "SELL", "quantity": "2", "price": "110"})
        original = deepcopy(activity)
        daily, tape = execution_frames(activity, "Asia/Seoul")
        self.assertEqual(daily.iloc[0]["거래일"], "2026-10-20")
        self.assertEqual(daily.iloc[0]["매수수량"], 5)
        self.assertEqual(daily.iloc[0]["매수금액($)"], 500)
        self.assertEqual(daily.iloc[0]["매도수량"], 2)
        self.assertEqual(daily.iloc[0]["매도금액($)"], 220)
        self.assertEqual(len(tape), 2)
        self.assertEqual(activity, original)

    def test_quote_real_time_denied_is_explicit_delayed_fallback(self):
        session = Session()
        session.routes["prices"] = [Response({"rsp_cd": "DENIED"}, 500), response({
            "iscd": "SOXL", "crnc_code": "USD", "prpr": "101.2", "prdy_clpr": "100", "prdy_ctrt": "1.2"})]
        session.routes["daytime"] = [response({"iscd": "SOXL", "dytm_prpr": "0", "dytm_mkt_yn": "N"})]
        session.routes["after"] = [response({"iscd": "SOXL", "mkt_aft_prpr": "0"})]
        with patch("v12.meritz.time.sleep"):
            quote = Client(SETTINGS, session).quote()
        self.assertTrue(quote["delayed"])
        self.assertEqual(quote["price"], "101.2")
        flags = [c[2]["params"]["dely_rltm_cls_code"] for c in session.calls if c[1].endswith("/prices")]
        self.assertEqual(flags, ["0", "1"])

    def test_invalid_quote_does_not_become_zero_price(self):
        session = Session()
        session.routes["prices"] = [response({"iscd": "WRONG", "crnc_code": "USD", "prpr": "0"})] * 2
        with patch("v12.meritz.time.sleep"), self.assertRaises(ReadError):
            Client(SETTINGS, session).quote()

    def test_balance_endpoint_does_not_wait_on_history(self):
        session = Session(history=[Response({"rsp_cd": "FAIL"}, 500)])
        with patch("v12.meritz.time.sleep"):
            client = Client(SETTINGS, session)
            balances = client.balances()
            self.assertEqual(balances["cash_usd"], "1000")
            with self.assertRaises(ReadError):
                client.activity()
        self.assertFalse(any(c[1].endswith("/detail") for c in session.calls))


if __name__ == "__main__":
    unittest.main()
