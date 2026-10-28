import unittest
import pandas as pd
from v12_actual_exit_status import evaluate_actual_slot

def prices(vals):
    dates=pd.bdate_range("2026-10-01",periods=len(vals))
    return pd.DataFrame({"Date":dates,"Close":vals})
def slot(entry,px="100"):
    return {"entry_date":entry,"entry_price":px,"quantity":"2"}

class Tests(unittest.TestCase):
    def test_d2_flag_next_session(self):
        d=prices([100,100,89])
        res=evaluate_actual_slot(slot("2026-10-01"),d)
        self.assertEqual(res["elapsed"],2)
        self.assertIn("D2 손절",res["status"])
        self.assertTrue(res["sell_watch"])
    def test_d7_overdue_is_not_reported_as_filled(self):
        d=prices([100]*11)
        res=evaluate_actual_slot(slot("2026-10-01"),d)
        self.assertIn("기한 경과",res["status"])
        self.assertNotIn("체결 완료",res["status"])
    def test_d6_next_d7(self):
        d=prices([100]*7)
        res=evaluate_actual_slot(slot("2026-10-01"),d)
        self.assertIn("다음 거래일 D7",res["status"])
    def test_loc_met_requires_broker_confirmation(self):
        d=prices([100,104])
        res=evaluate_actual_slot(slot("2026-10-01"),d)
        self.assertIn("체결 확인",res["status"])
        self.assertTrue(res["sell_watch"])
    def test_normal_holding(self):
        d=prices([100,101])
        res=evaluate_actual_slot(slot("2026-10-01"),d)
        self.assertEqual(res["status"],"LOC 매도 대기")

if __name__=="__main__":unittest.main()
