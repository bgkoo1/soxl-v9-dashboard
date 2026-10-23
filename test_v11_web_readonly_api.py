"""Offline safety tests for V11 local read-only API. No broker network calls."""
import unittest
from unittest.mock import patch
from v11_web_readonly_api import app

class TestV11ReadOnlyAPI(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_health_never_claims_broker_connection(self):
        response = self.client.get("/api/v11/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["mode"], "READ_ONLY")
        self.assertIs(response.json["live_orders_enabled"], False)
        self.assertIs(response.json["broker_connected"], False)
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    @patch("v11_web_readonly_api.preview_account")
    @patch("v11_web_readonly_api._credentials")
    def test_account_is_readonly_and_not_fresh_start_verified(self, credentials, preview):
        credentials.return_value = {"MERITZ_APP_KEY": "mock"}
        preview.return_value = {"SOXL 보유 수량(주)": "3", "live_orders_enabled": False}
        response = self.client.get("/api/v11/account")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["account"]["SOXL 보유 수량(주)"], "3")
        self.assertIs(response.json["fresh_start_verified"], False)
        self.assertEqual(response.json["mode"], "READ_ONLY")
        self.assertNotIn("MERITZ_APP_KEY", response.get_data(as_text=True))

    @patch("v11_web_readonly_api.preview_overseas_orders")
    @patch("v11_web_readonly_api._credentials")
    def test_orders_never_claims_full_account_verified(self, credentials, preview):
        credentials.return_value = {"MERITZ_APP_KEY": "mock"}
        preview.return_value = {"orders": [], "complete_account_orders_verified": False}
        response = self.client.get("/api/v11/orders")
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json["fresh_start_verified"], False)
        self.assertIs(response.json["orders"]["complete_account_orders_verified"], False)

    @patch("v11_web_readonly_api._credentials", side_effect=ValueError("secret"))
    def test_missing_secrets_fail_closed(self, _):
        response = self.client.get("/api/v11/account")
        self.assertEqual(response.status_code, 503)
        self.assertIs(response.json["fresh_start_verified"], False)
        self.assertNotIn("secret", response.get_data(as_text=True))

    def test_no_order_submission_routes(self):
        for route in ("/api/v11/orders", "/api/v11/account"):
            response = self.client.post(route, json={"symbol": "SOXL", "quantity": 1})
            self.assertEqual(response.status_code, 405)

if __name__ == "__main__":
    unittest.main()
