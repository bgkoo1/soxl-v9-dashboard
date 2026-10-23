"""Meritz OAuth authentication probe only. Never reads balances or places orders."""
import requests

BASE_URL="https://openapi.imeritz.com:9443"
def probe_auth(secrets, *, session=None):
    key=str(secrets.get("MERITZ_APP_KEY","")).strip()
    secret=str(secrets.get("MERITZ_APP_SECRET","")).strip()
    if not key or not secret:
        return {"status":"NOT_CONFIGURED","message":"메리츠 앱키 또는 시크릿이 없습니다.","connected":False}
    sender=session if session is not None else requests
    try:
        response=sender.post(
            BASE_URL+"/oauth2/token",
            data={"grant_type":"client_credentials","client_id":key,
                  "client_secret":secret,"scope":"login"},
            headers={"Content-Type":"application/x-www-form-urlencoded"},
            timeout=12,
        )
        body=response.json()
        if not isinstance(body,dict):
            raise ValueError("unexpected token response")
        if response.status_code==200 and isinstance(body.get("access_token"),str) and body["access_token"]:
            return {"status":"AUTH_OK","message":"토큰 발급 성공 · 계좌 조회는 아직 미검증","connected":True}
        return {"status":"AUTH_FAILED","message":"토큰 발급 실패 · 인증정보, 권한 및 서버 설정 확인 필요","connected":False}
    except (requests.RequestException,ValueError,TypeError):
        return {"status":"CONNECTION_ERROR","message":"인증 서버 응답 확인 실패 · 네트워크 또는 응답 형식 점검 필요","connected":False}
