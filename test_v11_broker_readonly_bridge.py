import unittest
from v11_broker_readonly_bridge import reconcile
def sample(): return {'as_of':'2026-10-21T16:00:00-04:00','holdings':[],'orders':[]}
class Tests(unittest.TestCase):
    def test_empty(self):
        r=reconcile(sample());self.assertTrue(r['account_flat_at_snapshot']);self.assertFalse(r['live_orders_enabled'])
    def test_position(self):
        s=sample();s['holdings']=[{'symbol':'SOXL','quantity':'2'}]
        self.assertFalse(reconcile(s)['account_flat_at_snapshot'])
    def test_pending(self):
        s=sample();s['orders']=[{'symbol':'SOXL','status':'CANCEL_REQUESTED','remaining_quantity':0}]
        self.assertFalse(reconcile(s)['account_flat_at_snapshot'])
    def test_missing_not_empty(self):
        s=sample();del s['orders']
        with self.assertRaises(ValueError):reconcile(s)
    def test_unknown_status(self):
        s=sample();s['orders']=[{'symbol':'SOXL','status':'UNKNOWN','remaining_quantity':0}]
        with self.assertRaises(ValueError):reconcile(s)
    def test_timezone_required(self):
        s=sample();s['as_of']='2026-10-21T16:00:00'
        with self.assertRaises(ValueError):reconcile(s)
    def test_negative_qty(self):
        s=sample();s['holdings']=[{'symbol':'SOXL','quantity':'-2'}]
        with self.assertRaises(ValueError):reconcile(s)
if __name__=='__main__':unittest.main()
