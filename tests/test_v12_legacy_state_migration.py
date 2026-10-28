"""Cross-version V12 encrypted ledger persistence and legacy import."""
import unittest,base64,json
from cryptography.fernet import Fernet
from v12_manual_holdings import (
    load_portfolio_state,save_portfolio_state,save_ledger,load_ledger,LedgerError)
from v12_trade_management import add_cashflow,record_sale
from v12_legacy_import import saved_candidates

class Response:
    def __init__(self,status,data=None):
        self.status_code=status;self.data=data or {}
    def raise_for_status(self):
        if self.status_code>=400:
            from requests import HTTPError
            raise HTTPError("request failed")
    def json(self):return self.data

class Fake:
    def __init__(self):
        self.blob=None;self.sha=None;self.n=0
    def get(self,url,**kwargs):
        return Response(404) if self.blob is None else Response(200,
                  {"content":self.blob,"sha":self.sha})
    def put(self,url,**kwargs):
        body=kwargs["json"]
        if (self.sha is None and body.get("sha") is not None) or (
            self.sha is not None and body.get("sha")!=self.sha):
            return Response(409)
        self.n+=1;self.sha=f"sha{self.n}";self.blob=body["content"]
        return Response(200,{"content":{"sha":self.sha}})

class CrossVersion(unittest.TestCase):
    def setUp(self):
        self.key=Fernet.generate_key().decode()
        self.sec={"GITHUB_TOKEN":"dummy","PORTFOLIO_ENCRYPTION_KEY":self.key,
            "GITHUB_REPO":"a/b","GITHUB_BRANCH":"main"}
        self.fake=Fake()
        self.slot={"slot_id":"s1","entry_date":"2026-10-20","entry_price":"100",
            "quantity":"20","source":"manual","model_exit":"직접 매수","note":""}
    def test_legacy_schema_preserved_after_journal_update(self):
        legacy={"schema_version":1,"positions":[self.slot]}
        self.fake.blob=base64.b64encode(Fernet(self.key.encode()).encrypt(
                    json.dumps(legacy).encode())).decode()
        self.fake.sha="old"
        state,sha=load_portfolio_state(self.sec,self.fake)
        self.assertEqual(state["closed_trades"],[])
        self.assertEqual(state["cashflows"],[])
        state=record_sale(state,"s1",quantity=5,exit_date="2026-10-23",
                          exit_price=105,exit_strategy="LOC +3.2%")
        state=add_cashflow(state,date_value="2026-10-23",flow_type="입금",
                           currency="USD",amount="100")
        saved=save_portfolio_state(self.sec,state,sha,self.fake)
        reopened,rev=load_portfolio_state(self.sec,self.fake)
        self.assertEqual(saved,rev)
        self.assertEqual(reopened["positions"][0]["quantity"],"15")
        self.assertEqual(len(reopened["closed_trades"]),1)
        self.assertEqual(len(reopened["cashflows"]),1)
        positions,rev=load_ledger(self.sec,self.fake)
        positions[0]["quantity"]="14"
        save_ledger(self.sec,positions,rev,self.fake)
        current,_=load_portfolio_state(self.sec,self.fake)
        self.assertEqual(current["positions"][0]["quantity"],"14")
        self.assertEqual(len(current["closed_trades"]),1)
        self.assertEqual(len(current["cashflows"]),1)
    def test_stale_sha_fail_closed(self):
        x,sha=load_portfolio_state(self.sec,self.fake)
        new=save_portfolio_state(self.sec,x,sha,self.fake)
        with self.assertRaises(LedgerError):
            save_portfolio_state(self.sec,x,sha,self.fake)
    def test_legacy_saved_lot_not_automatically_verified(self):
        rows=saved_candidates({"quantities":{
            "v11|v11-2026-10-20-000001":"12",
            "v11|v11-2026-10-21-000002":"0"},
            "entry_prices":{"v11|v11-2026-10-20-000001":"101.60"}})
        self.assertEqual(len(rows),1)
        self.assertFalse(rows[0]["broker_verified"])
        self.assertEqual(rows[0]["saved_quantity"],"12")
        self.assertEqual(rows[0]["saved_entry_price"],"101.60")
    def test_excluded_lots_not_offered(self):
        k="v11|v11-2026-10-20-000001"
        self.assertEqual(saved_candidates({"quantities":{k:"12"},
                                           "excluded_positions":[k]}),[])
if __name__=="__main__":unittest.main()
