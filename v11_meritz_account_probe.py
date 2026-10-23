"""Meritz official read-only account endpoint probe; no order APIs, no raw account data persistence."""
import re
import requests
from v11_meritz_auth_probe import BASE_URL

PATHS={
    "예수금":"/accounts/v1/me/deposit",
    "계좌 평가":"/accounts/v1/me/valuation",
    "국내 종목":"/accounts/v1/me/holdings",
    "해외 보유":"/accounts/v1/me/overseas/holdings",
    "해외 외화":"/accounts/v1/me/overseas/currencies",
}

def _mac(secrets):
    value=re.sub(r"[:-]","",str(secrets.get("MERITZ_MAC_ADDRESS","")).strip()).upper()
    if not re.fullmatch(r"[0-9A-F]{12}",value):
        raise ValueError("MERITZ_MAC_ADDRESS는 12자리 16진수여야 합니다.")
    return value

def probe_accounts(secrets, *, session=None):
    """User-triggered, GET-only connectivity; never claims positions are zero."""
    mac=_mac(secrets)
    key=str(secrets.get("MERITZ_APP_KEY","")).strip()
    secret=str(secrets.get("MERITZ_APP_SECRET","")).strip()
    if not key or not secret:
        raise ValueError("MERITZ_APP_KEY / MERITZ_APP_SECRET 설정이 필요합니다.")
    sender=session if session is not None else requests
    try:
        auth=sender.post(BASE_URL+"/oauth2/token",
            data={"grant_type":"client_credentials","client_id":key,
                  "client_secret":secret,"scope":"login"},
            headers={"Content-Type":"application/x-www-form-urlencoded"},timeout=12)
        auth.raise_for_status()
        token=auth.json().get("access_token")
        if not isinstance(token,str) or not token:
            raise ValueError("토큰 응답 형식 미확인")
    except (requests.RequestException,ValueError,TypeError):
        return [{"조회":name,"결과":"인증 실패","자료 구조":"미확인"} for name in PATHS]
    results=[]
    headers={"authorization":"Bearer "+token,"mac_address":mac}
    for name,path in PATHS.items():
        try:
            response=sender.get(BASE_URL+path,headers=headers,timeout=15)
            body=response.json()
            if not isinstance(body,dict):
                raise ValueError("응답 형식 오류")
            data=body.get("data")
            code=str(body.get("rsp_cd","")).strip()
            if response.ok and code in ("5766","0000","0001","5620") and data is not None:
                kind="목록" if isinstance(data,list) else ("객체" if isinstance(data,dict) else "기타")
                status="업무 성공 · 수치 검증 전"
            elif response.ok and code in ("5820","5822") and data in ([],{},None):
                kind="빈 자료"
                status="조회 결과 없음 · 잔고 0 확정 불가"
            else:
                kind="미확인"
                status="업무 오류 또는 응답 미검증"
            results.append({"조회":name,"결과":status,"자료 구조":kind})
        except (requests.RequestException,ValueError,TypeError):
            results.append({"조회":name,"결과":"응답 확인 실패","자료 구조":"미확인"})
    return results
