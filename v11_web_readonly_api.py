"""Local-only, read-only API for the SOXL V11 web prototype.

Run from repository root:
  pip install flask requests
  export MERITZ_APP_KEY=... MERITZ_APP_SECRET=... MERITZ_MAC_ADDRESS=...
  python v11_web_readonly_api.py

Binds to 127.0.0.1 only. Do NOT expose via public hosting or port forwarding.
The GitHub Pages static site does not connect to this API yet.
"""
import os
from datetime import datetime, timezone
from flask import Flask, jsonify
from v11_meritz_account_preview import preview_account
from v11_meritz_orders_preview import preview_overseas_orders

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

def _credentials():
    keys = ("MERITZ_APP_KEY", "MERITZ_APP_SECRET", "MERITZ_MAC_ADDRESS")
    values = {key: os.environ.get(key, "") for key in keys}
    if any(not value for value in values.values()):
        raise ValueError("서버 측 메리츠 인증정보가 설정되지 않았습니다.")
    return values

@app.after_request
def _private_cache(response):
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response

@app.get("/api/v11/health")
def health():
    return jsonify({"status": "ready", "mode": "READ_ONLY",
                    "live_orders_enabled": False, "broker_connected": False})

@app.get("/api/v11/account")
def account():
    try:
        result = preview_account(_credentials())
        return jsonify({"as_of": datetime.now(timezone.utc).isoformat(),
                        "mode": "READ_ONLY", "account": result,
                        "fresh_start_verified": False})
    except ValueError:
        return jsonify({"error": "계좌 조회 또는 응답 검증에 실패했습니다.",
                        "fresh_start_verified": False}), 503

@app.get("/api/v11/orders")
def orders():
    try:
        result = preview_overseas_orders(_credentials())
        return jsonify({"as_of": datetime.now(timezone.utc).isoformat(),
                        "mode": "READ_ONLY", "orders": result,
                        "fresh_start_verified": False})
    except ValueError:
        return jsonify({"error": "해외 주문 조회 또는 응답 검증에 실패했습니다.",
                        "fresh_start_verified": False}), 503

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8765, debug=False)
