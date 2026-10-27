"""Fail-closed V12 broker reconciliation and trade-sizing tests."""
import unittest
from v12_portfolio_reconciliation import reconcile_portfolio
from v12_manual_holdings import LedgerError


def snapshot(qty="12",usd="1200",krw="0",extra=None):
    return {"SOXL 보유 수량(주)":qty,"USD 외화 예수금($)":usd,
            "원화 주문 가능 현금(원)":krw,
            "해외 보유 종목":[{"종목":"SOXL","수량":qty}]+(extra or [])}


def slots(qty="12",count=1):
    return [{"slot_id":f"v12-actual-{i}","entry_date":"2026-10-20",
             "entry_price":"105.2","quantity":str(qty),"source":"manual",
             "model_exit":"TIME","note":""} for i in range(count)]


def calc(rows=None,account=None,**options):
    kwargs=dict(soxl_close="100",fx_krw_per_usd="1300",risk_blocked=False,
                orders_verified_empty=True,quote_fresh=True,fx_fresh=True,
                broker_fresh=True)
    kwargs.update(options)
    return reconcile_portfolio(
        slots() if rows is None else rows,
        snapshot() if account is None else account,**kwargs
    )


class ReconcileTests(unittest.TestCase):
    def test_usd_krw_total_nav_and_one_slot(self):
        a=calc(account=snapshot(usd="1200",krw="1300000"))
        self.assertEqual(a["nav_usd_estimate"],"3400")
        self.assertEqual(a["nav_krw_estimate"],"4420000")
        self.assertEqual(a["slots"],1)
        self.assertTrue(a["buy_preview_ready"])
        self.assertAlmostEqual(float(a["one_slot_usd"]),3400/6)
        self.assertAlmostEqual(float(a["base_buy_usd_reference"]),3400/6)
        self.assertAlmostEqual(float(a["crash_buy_usd_reference"]),3400/6)

    def test_mismatch_prevents_orders(self):
        result=calc(rows=slots("11"))
        self.assertFalse(result["buy_preview_ready"])
        self.assertTrue(any("불일치" in s for s in result["blockers"]))

    def test_unregistered_soxl_prevents_orders(self):
        result=calc(rows=[])
        self.assertFalse(result["buy_preview_ready"])

    def test_other_holdings_blocks_incomplete_nav(self):
        a=calc(account=snapshot(extra=[{"종목":"QQQ","수량":"1"}]))
        self.assertFalse(a["buy_preview_ready"])

    def test_six_slots_blocks(self):
        a=calc(rows=slots("2",count=6))
        self.assertEqual(a["free_slots"],0)
        self.assertFalse(a["buy_preview_ready"])

    def test_missing_quote_fx_orders_and_stale(self):
        for kw in [
            {"soxl_close":None},{"fx_krw_per_usd":None},{"quote_fresh":False},
            {"fx_fresh":False},{"broker_fresh":False},{"orders_verified_empty":False},
            {"risk_blocked":True}
        ]:
            with self.subTest(kw=kw):
                self.assertFalse(calc(**kw)["buy_preview_ready"])

    def test_partial_liquid_usd_never_assume_krw_spendable(self):
        a=calc(account=snapshot(usd="100",krw="1300000"))
        self.assertTrue(a["buy_preview_ready"])
        self.assertEqual(a["base_buy_usd_reference"],"100")
        self.assertEqual(a["crash_buy_usd_reference"],"0")

    def test_invalid_qty_prevents_valuation(self):
        with self.assertRaises(LedgerError):
            calc(account=snapshot(qty="-2"))

if __name__=="__main__":
    unittest.main()
