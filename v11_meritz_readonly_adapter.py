"""Meritz Open API credential and read-only integration boundary.

No live requests or order endpoints are implemented in this module.
Endpoint/response mapping must be verified against the official API catalog
before enabling a read-only broker connection.
"""
import os
from dataclasses import dataclass
from typing import Mapping

@dataclass(frozen=True)
class MeritzReadOnlyConfig:
    app_key: str
    app_secret: str
    mac_address: str
    base_url: str = "https://openapi.imeritz.com:9443"
    read_only: bool = True

    def __repr__(self):
        return "MeritzReadOnlyConfig(app_key='***', app_secret='***', mac_address='***', read_only=True)"

def load_readonly_config(secrets: Mapping | None = None) -> MeritzReadOnlyConfig:
    """Read credentials from Streamlit Secrets or process environment, never log them."""
    values = secrets if secrets is not None else os.environ
    names = ("MERITZ_APP_KEY", "MERITZ_APP_SECRET", "MERITZ_MAC_ADDRESS")
    missing = [name for name in names if not str(values.get(name, "")).strip()]
    if missing:
        raise ValueError("Missing Meritz credentials: " + ", ".join(missing))
    return MeritzReadOnlyConfig(
        app_key=str(values[names[0]]).strip(),
        app_secret=str(values[names[1]]).strip(),
        mac_address=str(values[names[2]]).strip(),
    )

def get_readonly_connection_status(secrets: Mapping | None = None):
    """Safe to display in Streamlit; no secrets and no broker requests."""
    try:
        load_readonly_config(secrets)
    except ValueError:
        return {"connected": False, "read_only": True, "live_orders_enabled": False,
                "status": "NOT_CONFIGURED", "message": "메리츠 API 인증정보가 설정되지 않았습니다."}
    return {"connected": False, "read_only": True, "live_orders_enabled": False,
            "status": "CREDENTIALS_PRESENT_UNVERIFIED",
            "message": "인증정보는 있으나 메리츠 서버 연결 및 계좌 조회는 아직 검증되지 않았습니다."}

def reconcile_official_response(normalized_snapshot):
    """Only accepts fully mapped broker data; does not guess API response fields."""
    from v11_broker_readonly_bridge import reconcile
    return reconcile(normalized_snapshot)
