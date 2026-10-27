"""No-live-account tests for Meritz stage diagnostics and redaction."""
import unittest
from v11_meritz_account_preview import preview_account, MeritzAccountReadError

VALID={"MERITZ_APP_KEY":"TEST_APPKEY","MERITZ_APP_SECRET":"VERY_SECRET",
       "MERITZ_MAC_ADDRESS":"AA-BB-CC-DD-EE-FF"}

class FakeResponse:
    def __init__(self,status=200,payload=None):
        self.status_code=status
        self._payload=payload
        self.ok=status<400
    def json(self):
        return self._payload

class FakeSession:
    def __init__(self, responses):
        self.responses=list(responses)
        self.calls=[]
    def post(self,url,**kw):
        self.calls.append(("post",url))
        return self.responses.pop(0)
    def get(self,url,**kw):
        self.calls.append(("get",url))
        return self.responses.pop(0)

def token():
    return FakeResponse(200,{"access_token":"PRIVATE_TOKEN"})
def dep():
    return FakeResponse(200,{"rsp_cd":"5766","data":{"oder_able_cash_amt":0}})
def usd():
    return FakeResponse(200,{"rsp_cd":"5766","data":[{"crcd":"USD","frcr_amt":"50.00"}]})
def holding():
    return FakeResponse(200,{"rsp_cd":"5766","tr_cont":"0","data":[
        {"ovrs_stck_symb_iscd":"SOXL","rmnd_dqty":"317.0000000000"}]})

class Diagnostics(unittest.TestCase):
    def test_complete_snapshot(self):
        session=FakeSession([token(),dep(),usd(),holding()])
        result=preview_account(VALID,session=session)
        self.assertEqual(result["SOXL 보유 수량(주)"],"317.0000000000")
        self.assertEqual(result["USD 외화 예수금($)"],"50.00")
        self.assertFalse(result["live_orders_enabled"])

    def test_missing_auth_config_reports_config(self):
        with self.assertRaises(MeritzAccountReadError) as err:
            preview_account({})
        self.assertEqual(err.exception.stage,"CONFIG")

    def test_expired_token_or_forbidden(self):
        session=FakeSession([FakeResponse(401,{"access_token":None,"error":"PRIVATE_TOKEN"})])
        with self.assertRaises(MeritzAccountReadError) as err:
            preview_account(VALID,session=session)
        self.assertEqual(err.exception.stage,"TOKEN")
        self.assertEqual(err.exception.http_status,401)
        self.assertNotIn("PRIVATE_TOKEN",str(err.exception))

    def test_broker_rejected_deposit(self):
        session=FakeSession([token(),FakeResponse(500,{"rsp_cd":"9999","rsp_msg":"ACCOUNT 123456"})])
        with self.assertRaises(MeritzAccountReadError) as err:
            preview_account(VALID,session=session)
        self.assertEqual(err.exception.stage,"DEPOSIT")
        self.assertNotIn("123456",str(err.exception))
        self.assertNotIn("9999",str(err.exception))

    def test_currencies_empty_not_zero(self):
        session=FakeSession([token(),dep(),FakeResponse(200,{"rsp_cd":"5820","data":[]})])
        with self.assertRaises(MeritzAccountReadError) as err:
            preview_account(VALID,session=session)
        self.assertEqual(err.exception.stage,"CURRENCIES")
        self.assertEqual(err.exception.response_code,"5820")

    def test_holdings_missing_list_fail_closed(self):
        session=FakeSession([token(),dep(),usd(),FakeResponse(200,{"rsp_cd":"5766","data":None})])
        with self.assertRaises(MeritzAccountReadError) as err:
            preview_account(VALID,session=session)
        self.assertEqual(err.exception.stage,"HOLDINGS")

    def test_no_secret_leak(self):
        session=FakeSession([token(),FakeResponse(500,{"rsp_cd":"ABC","rsp_msg":"VERY_SECRET"})])
        try:
            preview_account(VALID,session=session)
        except MeritzAccountReadError as exc:
            self.assertNotIn("VERY_SECRET",str(exc))
            self.assertNotIn("TEST_APPKEY",str(exc))
        else:
            self.fail("No exception")

if __name__=="__main__": unittest.main()
