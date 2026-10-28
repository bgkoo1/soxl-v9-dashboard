"""Offline V12 actual trading workflow integration regression."""
import unittest
from decimal import Decimal
from v12_manual_holdings import reopen_slot,LedgerError
from v12_trade_management import (
    update_slot,record_sale,cancel_sale,add_cashflow,remove_cashflow,
    cashflow_summary,realized_summary,validate_closed_trades)

def seed():
    return {"schema_version":1,"positions":[{"slot_id":"actual-1","entry_date":"2026-10-20",
        "entry_price":"100","quantity":"20","source":"manual","model_exit":"직접 매수",
        "note":""}],"closed_trades":[],"cashflows":[]}

class Workflow(unittest.TestCase):
    def test_correct_real_qty_and_entry(self):
        s=update_slot(seed(),"actual-1",entry_date="2026-10-20",
                      entry_price="101.60",quantity=21,note="정정")
        self.assertEqual(s["positions"][0]["entry_price"],"101.60")
        self.assertEqual(s["positions"][0]["quantity"],"21")
    def test_partial_sale_and_reopen(self):
        s=record_sale(seed(),"actual-1",quantity=8,exit_date="2026-10-22",
                      exit_price="105",exit_strategy="LOC +3.2%")
        self.assertEqual(s["positions"][0]["quantity"],"12")
        self.assertEqual(s["closed_trades"][0]["quantity"],"8")
        self.assertEqual(realized_summary(s),Decimal("40"))
        restored=cancel_sale(s,s["closed_trades"][0]["id"])
        self.assertEqual(restored["positions"][0]["quantity"],"20")
        self.assertFalse(restored["closed_trades"])
    def test_complete_sale_and_undo(self):
        s=record_sale(seed(),"actual-1",quantity=20,exit_date="2026-10-22",
                      exit_price="90",exit_strategy="D2 손절")
        self.assertFalse(s["positions"])
        self.assertEqual(realized_summary(s),Decimal("-200"))
        s=cancel_sale(s,s["closed_trades"][0]["id"])
        self.assertEqual(s["positions"][0]["quantity"],"20")
    def test_sale_exceeds_qty_fails_without_mutation(self):
        before=seed()
        with self.assertRaises(LedgerError):
            record_sale(before,"actual-1",quantity=21,exit_date="2026-10-22",
                        exit_price=104,exit_strategy="LOC +3.2%")
        self.assertEqual(before["positions"][0]["quantity"],"20")
    def test_sale_before_buy_rejected(self):
        with self.assertRaises(LedgerError):
            record_sale(seed(),"actual-1",quantity=1,exit_date="2026-10-19",
                        exit_price=104,exit_strategy="수동 매도")
    def test_cashflow_and_no_double_count(self):
        s=add_cashflow(seed(),date_value="2026-10-24",flow_type="입금",
                       currency="USD",amount=100)
        s=add_cashflow(s,date_value="2026-10-25",flow_type="출금",
                       currency="USD",amount=25)
        self.assertEqual(cashflow_summary(s)["USD"],Decimal("75"))
        self.assertEqual(s["positions"][0]["quantity"],"20")
        s=remove_cashflow(s,s["cashflows"][0]["id"])
        self.assertEqual(cashflow_summary(s)["USD"],Decimal("-25"))
    def test_undo_after_entry_mutation_requires_manual_resolution(self):
        s=record_sale(seed(),"actual-1",quantity=5,exit_date="2026-10-22",
                      exit_price=103.2,exit_strategy="LOC +3.2%")
        s=update_slot(s,"actual-1",entry_date="2026-10-20",
                      entry_price="99",quantity=15)
        with self.assertRaises(LedgerError):
            cancel_sale(s,s["closed_trades"][0]["id"])

if __name__=="__main__":
    unittest.main()
