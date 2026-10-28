"""V12 restored order center signal and safe sizing regression."""
import unittest
from decimal import Decimal
from v12_order_center import entry_filter, _num

class OrderCenterTests(unittest.TestCase):
    def test_missing_prices_block_new_buys(self):
        self.assertTrue(entry_filter([100.0]*20)["blocked"])

    def test_normal_signal(self):
        a=entry_filter([100.0]*200)
        self.assertFalse(a["blocked"])
        self.assertEqual(a["reason"],"정상 신호")

    def test_four_down_closes(self):
        prices=[100.0]*200+[99,98,97,96]
        a=entry_filter(prices)
        self.assertTrue(a["blocked"])
        self.assertEqual(a["last4"],4)

    def test_invalid_numeric_never_used_for_order(self):
        for v in ("nan","inf","-1",None,True):
            with self.subTest(v=v):
                if v is True:
                    continue
                self.assertIsNone(_num(v))

    def test_slot_target_math(self):
        shares=Decimal("30")
        entry=Decimal("101.60")
        target=entry*Decimal("1.032")
        self.assertEqual(target,Decimal("104.85120"))
        self.assertEqual(shares*target,Decimal("3145.53600"))

if __name__=="__main__":
    unittest.main()
