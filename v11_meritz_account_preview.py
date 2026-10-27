"""User-triggered Meritz overseas account preview: GET-only diagnostics.

Error messages deliberately reveal ONLY the failed stage, HTTP status class,
and whitelisted broker response code (never broker response text or secrets).
"""
from decimal import Decimal, InvalidOperation
import requests
from v11_meritz_account_probe import _mac
from v11_meritz_auth_probe import BASE_URL

SUCCESS_CODES={"5766","0000","0001","5620"}
EMPTY_CODES={"5820","5822"}
STAGES={
    "CONFIG":"메리츠 인증 설정",
    "TOKEN":"접근토큰 발급",
    "DEPOSIT":"원화 예수금 조회",
    "CURRENCIES":"USD 외화잔고 조회",
    "HOLDINGS":"해외 보유종목 조회",
}

class MeritzAccountReadError(ValueError):
    def __init__(self, stage, reason, *, http_status=None, response_code=None):
        if stage not in STAGES:
            stage="CONFIG"
        self.stage=stage
        self.reason=reason
        self.http_status=http_status if isinstance(http_status,int) and 100<=http_status<=599 else None
        code=str(response_code or "").strip()
        self.response_code=code if code in SUCCESS_CODES|EMPTY_CODES else None
        components=[f"{STAGES[stage]} ({stage})",reason]
        if self.http_status:
            components.append(f"HTTP {self.http_status}")
        if self.response_code:
            components.append(f"업무코드 {self.response_code}")
        super().__init__(" · ".join(components))


def _amount(value, stage, name):
    if isinstance(value,bool):
        raise MeritzAccountReadError(stage,f"{name} 값 형식 오류")
    try:
        n=Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise MeritzAccountReadError(stage,f"{name} 값 형식 오류") from None
    if not n.is_finite() or n<0:
        raise MeritzAccountReadError(stage,f"{name} 값 형식 오류")
    return n


def _safe_json(response,stage):
    try:
        body=response.json()
    except (TypeError,ValueError):
        raise MeritzAccountReadError(stage,"JSON 응답 해석 실패",http_status=response.status_code) from None
    if not isinstance(body,dict):
        raise MeritzAccountReadError(stage,"응답 객체 형식 오류",http_status=response.status_code)
    return body


def _request(sender, method, path, stage, **kwargs):
    try:
        response=getattr(sender,method)(BASE_URL+path,timeout=15 if method=="get" else 12,**kwargs)
    except (requests.Timeout,requests.ConnectionError):
        raise MeritzAccountReadError(stage,"네트워크 연결 또는 시간 초과") from None
    except requests.RequestException:
        raise MeritzAccountReadError(stage,"HTTP 통신 오류") from None
    return response


def _get(sender,path,headers,stage,params=None):
    response=_request(sender,"get",path,stage,headers=headers,params=params)
    body=_safe_json(response,stage)
    code=str(body.get("rsp_cd") or "").strip()
    if not response.ok:
        raise MeritzAccountReadError(stage,"조회 요청이 거절됨",http_status=response.status_code,response_code=code)
    if code in EMPTY_CODES:
        # 5820 is not proof that a missing holdings page or currency list is empty.
        raise MeritzAccountReadError(stage,"조회할 자료 없음: 잔고 0으로 가정하지 않음",
                                     http_status=response.status_code,response_code=code)
    if code not in SUCCESS_CODES:
        raise MeritzAccountReadError(stage,"업무 응답 검증 실패",http_status=response.status_code,response_code=code)
    return body


def preview_account(secrets, *, session=None):
    """Read only. A failed stage never returns invented cash or positions."""
    try:
        mac=_mac(secrets)
        key=str(secrets.get("MERITZ_APP_KEY","")).strip()
        secret=str(secrets.get("MERITZ_APP_SECRET","")).strip()
    except (ValueError,TypeError,AttributeError):
        raise MeritzAccountReadError("CONFIG","MAC 주소 또는 인증 설정 확인 필요") from None
    if not key or not secret:
        raise MeritzAccountReadError("CONFIG","앱 Key 또는 Secret 누락")
    sender=session if session is not None else requests
    auth=_request(sender,"post","/oauth2/token","TOKEN",
                  data={"grant_type":"client_credentials","client_id":key,
                        "client_secret":secret,"scope":"login"},
                  headers={"Content-Type":"application/x-www-form-urlencoded"})
    token_body=_safe_json(auth,"TOKEN")
    if not auth.ok:
        raise MeritzAccountReadError("TOKEN","접근토큰 요청 거절",http_status=auth.status_code)
    token=token_body.get("access_token")
    if not isinstance(token,str) or not token:
        raise MeritzAccountReadError("TOKEN","발급 응답에 접근토큰 없음")
    headers={"authorization":"Bearer "+token,"mac_address":mac}
    deposit=_get(sender,"/accounts/v1/me/deposit",headers,"DEPOSIT").get("data")
    if not isinstance(deposit,dict):
        raise MeritzAccountReadError("DEPOSIT","원화 예수금 데이터 형식 오류")
    won=_amount(deposit.get("oder_able_cash_amt"),"DEPOSIT","원화 주문 가능 현금")
    currencies=_get(sender,"/accounts/v1/me/overseas/currencies",headers,"CURRENCIES").get("data")
    if not isinstance(currencies,list):
        raise MeritzAccountReadError("CURRENCIES","외화잔고 목록 형식 오류")
    usd=Decimal("0")
    for row in currencies:
        if not isinstance(row,dict) or not str(row.get("crcd","")).strip():
            raise MeritzAccountReadError("CURRENCIES","통화별 잔고 행 형식 오류")
        if str(row["crcd"]).upper()=="USD":
            usd+=_amount(row.get("frcr_amt"),"CURRENCIES","USD 외화 예수금")
    holdings=[]
    params=None
    seen=set()
    for _ in range(100):
        body=_get(sender,"/accounts/v1/me/overseas/holdings",headers,"HOLDINGS",params)
        rows=body.get("data")
        if not isinstance(rows,list):
            raise MeritzAccountReadError("HOLDINGS","해외 종목 목록 형식 오류")
        for row in rows:
            if not isinstance(row,dict) or not str(row.get("ovrs_stck_symb_iscd","")).strip():
                raise MeritzAccountReadError("HOLDINGS","해외 종목 코드 누락")
            qty=_amount(row.get("rmnd_dqty"),"HOLDINGS","해외 종목 수량")
            if qty>0:
                holdings.append({"종목":str(row["ovrs_stck_symb_iscd"]).upper(),"수량":str(qty)})
        cont=str(body.get("tr_cont","0"))
        if cont=="0":
            break
        cursor=body.get("tr_cont_key")
        if cont!="1" or not isinstance(cursor,str) or not cursor or cursor in seen:
            raise MeritzAccountReadError("HOLDINGS","연속조회 키가 올바르지 않음")
        seen.add(cursor)
        params={"tr_cont":"1","tr_cont_key":cursor}
    else:
        raise MeritzAccountReadError("HOLDINGS","해외 종목 연속조회 최대 페이지 초과")
    soxl=sum((Decimal(row["수량"]) for row in holdings if row["종목"]=="SOXL"),Decimal("0"))
    return {"원화 주문 가능 현금(원)":str(won),
            "USD 외화 예수금($)":str(usd),
            "SOXL 보유 수량(주)":str(soxl),
            "해외 보유 종목":holdings,
            "status":"계좌 조회 완료 · 미체결/국내 보유 검증 전",
            "live_orders_enabled":False}
