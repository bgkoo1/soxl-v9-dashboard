import unittest
from v11_execution_safety_gate import evaluate

def snapshot():
    return dict(date="2026-10-22",trading_day_index=100,last_confirmed_close=100,
                previous_close=100,positions=[],pending_orders=[],
                previous_nav=7000,available_cash=7000,sizing_reference_price=100,
                allow_buy=True)
def controls():
    return dict(market_data_verified=True,broker_positions_verified=True,
                broker_orders_verified=True,manual_approval=True,dry_run_passed=True,
                new_portfolio_initialized=True,signals_verified=True,
                as_of_date="2026-10-22")
class GateTests(unittest.TestCase):
    def test_default_blocks(self):
        r=evaluate(snapshot(),{})
        self.assertFalse(r["review_ready"])
        self.assertFalse(r["live_orders_enabled"])
    def test_occupied_blocks(self):
        s=snapshot()
        s["positions"]=[dict(slot_id="legacy",entry_trading_day_index=99,quantity=1,entry_price=100)]
        self.assertFalse(evaluate(s,controls())["review_ready"])
    def test_pending_blocks(self):
        s=snapshot()
        s["pending_orders"]=[dict(status="ACCEPTED",side="BUY",reason="BASE",slot_id="x")]
        self.assertFalse(evaluate(s,controls())["review_ready"])
    def test_no_orders_even_when_ready(self):
        r=evaluate(snapshot(),controls())
        self.assertTrue(r["review_ready"])
        self.assertFalse(r["live_orders_enabled"])
        self.assertFalse(any(o["side"]=="BUY" for o in r["diagnostic"]["orders"]))
    def test_stale_blocks(self):
        c=controls();c["as_of_date"]="2026-10-21"
        self.assertFalse(evaluate(snapshot(),c)["review_ready"])
if __name__=="__main__": unittest.main()
