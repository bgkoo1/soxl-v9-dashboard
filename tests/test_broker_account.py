import unittest
from copy import deepcopy
from decimal import Decimal
from unittest.mock import patch
from v12.meritz import Client, ReadError
from v12.broker_book import rebuild, buying_cash

SETTINGS = {"MERITZ_APP_KEY": "test-key", "MERITZ_APP_SECRET": "test-secret",
            "MERITZ_MAC_ADDRESS": "AA-BB-CC-DD-EE-FF"}


class Response:
    def __init__(self, body, status=200):
        self.body, self.status_code, self.ok = body, status, status < 400
    def json(self):
        return self.body


def response(data, **extra):
    return Response({"rsp_cd": "5766", "data": data, **extra})


def fill(no="1", execution="1", side="2", qty="5", px="100", stamp="2026-10-21 04:30:00"):
    return {"oder_date": "20261020", "oder_no": no, "cntg_no": execution, "iscd": "SOXL.P",
        "seln_byov_clcd": side, "cntg_dqty": qty, "cntg_crnc_prc": px, "cntg_ymdh": stamp, "crcd": "USD"}


def snapshot():
    return {"fetched_at": "2026-10-28T10:00:00+00:00", "start": "20260101", "end": "20261028",
            "cash_usd": "1000", "estimated_cash_usd": "1000", "buying_power_usd": "1200",
            "reusable_usd": "300", "fx": "1400", "krw_buying_power": "0",
            "holdings": [{"symbol": "SOXL", "currency": "USD", "quantity": "5", "sellable": "5",
                          "average_price": "100", "price": "101", "value": "505", "unrealized": "5"}],
            "fills": [{"id": "20261020:1:1:SOXL:2", "order_key": "20261020:1", "order_date": "20261020",
                       "order_no": "1", "execution_no": "1", "timestamp": "2026-10-21 04:30:00",
                       "symbol": "SOXL", "currency": "USD", "side": "BUY", "quantity": "5", "price": "100"}],
            "outstanding": [], "transactions": []}


class Session:
    def __init__(self, history=None, details=None):
        self.calls = []
        self.routes = {
            "deposit": [response({"oder_able_cash_amt": "100"})],
            "currencies": [response([{"crcd": "USD", "frcr_amt": "1000", "estm_frcr_amt": "1100", "vltn_exrt": "1400"}])],
            "estimate": [response([{"crcd": "USD", "oder_crnc_oder_able_camt": "1200", "camt": "300"}])],
            "holdings": [response([{"ovrs_stck_symb_iscd": "SOXL", "crcd": "USD", "rmnd_dqty": "5",
                                    "seln_able_dqty": "5", "avrg_unpr": "100", "psnt_prc": "101",
                                    "frcr_vltn_amt": "505", "vltn_prls_camt": "5"}])],
            "history": history or [response([{**fill(execution="0"), "ncnt_odqt": "0"}])],
            "detail": details or [response([fill()])],
            "transactions": [response([])],
        }
    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return Response({"access_token": "secret-token", "expires_in": 3600})
    def get(self, url, **kwargs):
        self.calls.append(("GET", url, deepcopy(kwargs)))
        return self.routes[url.rsplit("/", 1)[-1]].pop(0)


class AccountTests(unittest.TestCase):
    def test_complete_reads_and_actual_fields(self):
        session = Session()
        with patch("v12.meritz.time.sleep"):
            result = Client(SETTINGS, session).snapshot("20260101", "20261028")
        self.assertEqual(result["cash_usd"], "1000")
        self.assertEqual(result["buying_power_usd"], "1200")
        self.assertEqual(result["fills"][0]["execution_no"], "1")
        self.assertEqual(len([c for c in session.calls if c[0] == "POST"]), 1)
        self.assertTrue(all(c[1].endswith("/oauth2/token") for c in session.calls if c[0] == "POST"))

    def test_pages_preserve_cursor_and_no_duplicate_fills(self):
        row = {**fill(execution="0"), "ncnt_odqt": "0"}
        first = response([row], rsp_cd="5762", tr_cont="1", tr_cont_key="cursor   ")
        session = Session(history=[first, response([row])], details=[response([fill(), fill()])])
        with patch("v12.meritz.time.sleep"):
            result = Client(SETTINGS, session).snapshot("20260101", "20261028")
        self.assertEqual(len(result["fills"]), 1)
        calls = [c for c in session.calls if c[1].endswith("/history")]
        self.assertEqual(calls[1][2]["params"]["tr_cont_key"], "cursor   ")
        self.assertEqual(len([c for c in session.calls if c[1].endswith("/detail")]), 1)

    def test_missing_detail_fill_id_fails(self):
        with patch("v12.meritz.time.sleep"), self.assertRaises(ReadError):
            Client(SETTINGS, Session(details=[response([fill(execution="0")])])).snapshot()

    def test_conflicting_duplicate_fails(self):
        with patch("v12.meritz.time.sleep"), self.assertRaises(ReadError):
            Client(SETTINGS, Session(details=[response([fill(), fill(px="105")])])).snapshot()

    def test_incomplete_detail_fails(self):
        with patch("v12.meritz.time.sleep"), self.assertRaises(ReadError):
            Client(SETTINGS, Session(details=[response([fill(qty="2")])])).snapshot()

    def test_error_never_echoes_payload(self):
        session = Session()
        session.routes["deposit"] = [Response({"rsp_cd": "BAD", "rsp_msg": "test-secret PRIVATE-ACCOUNT"}, 500)]
        with self.assertRaises(ReadError) as caught:
            Client(SETTINGS, session).snapshot()
        self.assertNotIn("test-secret", str(caught.exception))
        self.assertNotIn("PRIVATE-ACCOUNT", str(caught.exception))

    def test_repeated_cursor_fails(self):
        page = response([], rsp_cd="5762", tr_cont="1", tr_cont_key="same ")
        session = Session(history=[page, page])
        with patch("v12.meritz.time.sleep"), self.assertRaises(ReadError):
            Client(SETTINGS, session).snapshot()

    def test_fifo_partial_sale_and_timezone(self):
        s = snapshot()
        extra = {**s["fills"][0], "id": "second", "order_key": "20261021:2", "order_no": "2",
                 "timestamp": "2026-10-22 04:30:00", "quantity": "3", "price": "120"}
        sale = {**extra, "id": "sell", "order_key": "20261022:3", "order_no": "3", "side": "SELL",
                "timestamp": "2026-10-23 04:30:00", "quantity": "4", "price": "130"}
        s["fills"].extend([extra, sale])
        s["holdings"][0]["quantity"] = "4"
        s["holdings"][0]["sellable"] = "4"
        original = deepcopy(s)
        book = rebuild(s, "Asia/Seoul")
        self.assertTrue(book["matched"])
        self.assertEqual(book["positions"][0]["entry_date"], "2026-10-20")
        self.assertEqual(book["positions"][0]["quantity"], "1")
        self.assertEqual(book["closed_trades"][0]["quantity"], "4")
        self.assertEqual(s, original)
        self.assertEqual(rebuild(s, "Asia/Seoul"), book)

    def test_partial_buys_one_slot_weighted_price(self):
        s = snapshot()
        s["fills"][0]["quantity"] = "2"
        s["fills"].append({**s["fills"][0], "id": "partial2", "execution_no": "2", "quantity": "3", "price": "110"})
        book = rebuild(s, "Asia/Seoul")
        self.assertTrue(book["matched"])
        self.assertEqual(len(book["positions"]), 1)
        self.assertEqual(Decimal(book["positions"][0]["entry_price"]), Decimal("106"))

    def test_mismatch_unmatched_sell_and_pending_blocks(self):
        s = snapshot()
        s["holdings"][0]["quantity"] = "7"
        self.assertFalse(rebuild(s, "Asia/Seoul")["matched"])
        s = snapshot()
        s["fills"][0]["side"] = "SELL"
        self.assertTrue(any("매수 이력이 부족" in i for i in rebuild(s, "Asia/Seoul")["issues"]))
        s = snapshot()
        s["outstanding"] = [{"symbol": "SOXL", "quantity": "1"}]
        self.assertFalse(rebuild(s, "Asia/Seoul")["matched"])

    def test_unknown_timezone_cannot_create_orders(self):
        self.assertFalse(rebuild(snapshot())["matched"])

    def test_cash_is_capped_and_sales_other_symbols_excluded(self):
        s = snapshot()
        self.assertEqual(buying_cash(s, rebuild(s, "Asia/Seoul")), Decimal("900"))
        s["fills"].append({**s["fills"][0], "id": "other-sell", "order_key": "20261028:2", "order_no": "2",
                           "timestamp": "2026-10-28 17:00:00", "side": "SELL", "symbol": "AAPL", "price": "100", "quantity": "6"})
        self.assertEqual(buying_cash(s, rebuild(s, "Asia/Seoul")), Decimal("400"))


if __name__ == "__main__":
    unittest.main()
