import base64
import json
import unittest
from cryptography.fernet import Fernet
from v12_manual_holdings import (
    LedgerError, LEDGER_FILE, normalize_slot, reopen_slot,
    close_manual_slot, save_ledger, load_ledger, reconcile_total
)


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code=status
        self._payload=payload or {}
    def raise_for_status(self):
        if self.status_code>=400:
            from requests.exceptions import HTTPError
            raise HTTPError(f"http {self.status_code}")
    def json(self):
        return self._payload


class FakeSender:
    def __init__(self):
        self.payload=None
        self.sha=None
        self.calls=[]
    def get(self,url,**kwargs):
        self.calls.append(("get",url))
        if self.payload is None:
            return FakeResponse(404)
        return FakeResponse(200,{"content":self.payload,"sha":self.sha})
    def put(self,url,**kwargs):
        self.calls.append(("put",url))
        body=kwargs["json"]
        if (self.sha is not None and body.get("sha") != self.sha) or (
            self.sha is None and "sha" in body
        ):
            return FakeResponse(409)
        self.payload=body["content"]
        self.sha="sha"+str(len(self.calls))
        return FakeResponse(200,{"content":{"sha":self.sha}})


class ManualLedgerTests(unittest.TestCase):
    def setUp(self):
        self.key=Fernet.generate_key().decode()
        self.secrets={"GITHUB_TOKEN":"test","PORTFOLIO_ENCRYPTION_KEY":self.key,
                      "GITHUB_REPO":"test/example","GITHUB_BRANCH":"main"}
        self.sender=FakeSender()
        self.slot={"slot_id":"legacy-2026-10-27-a","entry_date":"2026-10-23",
                   "entry_price":"100.30","quantity":"12","source":"manual",
                   "model_exit":"V11 TIME"}

    def test_reopen_persists_encrypted_without_touching_legacy(self):
        slots,sha=load_ledger(self.secrets,self.sender)
        self.assertEqual(slots,[])
        new=reopen_slot(slots,self.slot)
        saved=save_ledger(self.secrets,new,sha,self.sender)
        self.assertNotIn("legacy-2026-10-27-a",self.sender.payload)
        self.assertTrue(all(LEDGER_FILE in x[1] for x in self.sender.calls))
        loaded,sha=load_ledger(self.secrets,self.sender)
        self.assertEqual(saved,sha)
        self.assertEqual(loaded[0]["slot_id"],self.slot["slot_id"])
        self.assertEqual(loaded[0]["quantity"],"12")
        self.assertFalse(loaded[0]["broker_verified"])
        self.assertTrue(reconcile_total(loaded,"12")["matched"])

    def test_duplicate_reopen_rejected(self):
        a=reopen_slot([],self.slot)
        with self.assertRaises(LedgerError):
            reopen_slot(a,self.slot)

    def test_requires_valid_actual_quantity_and_entry_price(self):
        for item in [{**self.slot,"quantity":"0"},{**self.slot,"entry_price":"nan"},
                     {**self.slot,"entry_date":"bad"}]:
            with self.assertRaises(LedgerError):
                normalize_slot(item)

    def test_reopen_then_explicit_remove(self):
        a=reopen_slot([],self.slot)
        self.assertEqual(close_manual_slot(a,self.slot["slot_id"]),[])
        with self.assertRaises(LedgerError):
            close_manual_slot([],self.slot["slot_id"])

    def test_stale_save_does_not_overwrite(self):
        a=reopen_slot([],self.slot)
        sha=save_ledger(self.secrets,a,None,self.sender)
        with self.assertRaises(LedgerError):
            save_ledger(self.secrets,[],None,self.sender)
        loaded,_=load_ledger(self.secrets,self.sender)
        self.assertEqual(loaded,a)

    def test_encryption_error_fails_closed(self):
        a=reopen_slot([],self.slot)
        save_ledger(self.secrets,a,None,self.sender)
        bad=dict(self.secrets)
        bad["PORTFOLIO_ENCRYPTION_KEY"]=Fernet.generate_key().decode()
        with self.assertRaises(LedgerError):
            load_ledger(bad,self.sender)


if __name__=="__main__":
    unittest.main()
