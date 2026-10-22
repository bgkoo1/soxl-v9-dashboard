import unittest
from v11_meritz_readonly_adapter import load_readonly_config, get_readonly_connection_status
class Tests(unittest.TestCase):
    def test_no_credentials(self):
        r=get_readonly_connection_status({})
        self.assertEqual(r["status"],"NOT_CONFIGURED")
        self.assertFalse(r["live_orders_enabled"])
    def test_credentials_not_connection(self):
        d={"MERITZ_APP_KEY":"abc","MERITZ_APP_SECRET":"secret","MERITZ_MAC_ADDRESS":"mac"}
        r=get_readonly_connection_status(d)
        self.assertFalse(r["connected"])
        self.assertEqual(r["status"],"CREDENTIALS_PRESENT_UNVERIFIED")
    def test_repr_redacts_secrets(self):
        d={"MERITZ_APP_KEY":"abc","MERITZ_APP_SECRET":"topsecret","MERITZ_MAC_ADDRESS":"mac"}
        c=load_readonly_config(d)
        self.assertNotIn("topsecret",repr(c))
        self.assertTrue(c.read_only)
if __name__=="__main__":unittest.main()
