"""User-triggered Meritz account snapshot preview; GET-only, no order endpoints or persistence."""
from decimal import Decimal, InvalidOperation
import requests
from v11_meritz_account_probe import _mac
from v11_meritz_auth_probe import BASE_URL

def _amount(v, field):
    if isinstance(v,bool): raise ValueError(field+" 값 오류")
    try: x=Decimal(str(v))
    except (InvalidOperation,ValueError,TypeError): raise ValueError(field+" 값 오류") from None
    if not x.is_finite() or x<0: raise ValueError(field+" 값 오류")
    return x

def _get(sender, path, headers, params=None):
    r=sender.get(BASE_URL+path,headers=headers,params=params,timeout=15)
    body=r.json()
    if not isinstance(body,dict) or not r.ok or str(body.get("rsp_cd")) not in ("5766","0000","0001","5620"):
        raise ValueError("메리츠 조회 업무 응답 검증 실패")
    return body

def preview_account(secrets, *, session=None):
    mac=_mac(secrets)
    key=str(secrets.get("MERITZ_APP_KEY","")).strip()
    secret=str(secrets.get("MERITZ_APP_SECRET","")).strip()
    if not key or not secret: raise ValueError("메리츠 API 인증정보 누락")
    sender=session if session is not None else requests
    try:
        auth=sender.post(BASE_URL+"/oauth2/token",
            data={"grant_type":"client_credentials","client_id":key,"client_secret":secret,"scope":"login"},
            headers={"Content-Type":"application/x-www-form-urlencoded"},timeout=12)
        auth.raise_for_status()
        token=auth.json().get("access_token")
        if not isinstance(token,str) or not token: raise ValueError("토큰 발급 실패")
        headers={"authorization":"Bearer "+token,"mac_address":mac}
        deposit=_get(sender,"/accounts/v1/me/deposit",headers).get("data")
        currencies=_get(sender,"/accounts/v1/me/overseas/currencies",headers).get("data")
        if not isinstance(deposit,dict) or not isinstance(currencies,list):
            raise ValueError("예수금/외화잔고 응답 형식 오류")
        won=_amount(deposit.get("oder_able_cash_amt"),"원화 주문 가능 현금")
        usd=Decimal("0")
        for row in currencies:
            if not isinstance(row,dict) or not str(row.get("crcd","")).strip():
                raise ValueError("외화잔고 행 형식 오류")
            if str(row["crcd"]).upper()=="USD":
                usd+=_amount(row.get("frcr_amt"),"USD 외화 예수금")
        holdings=[]
        params=None
        seen=set()
        for _ in range(100):
            body=_get(sender,"/accounts/v1/me/overseas/holdings",headers,params)
            page=body.get("data")
            if not isinstance(page,list): raise ValueError("해외 보유 목록 형식 오류")
            for row in page:
                if not isinstance(row,dict) or not str(row.get("ovrs_stck_symb_iscd","")).strip():
                    raise ValueError("해외 종목 코드 누락")
                qty=_amount(row.get("rmnd_dqty"),"해외 보유 수량")
                if qty>0:
                    holdings.append({"종목":str(row["ovrs_stck_symb_iscd"]).upper(),"수량":str(qty)})
            if str(body.get("tr_cont","0"))=="0": break
            if str(body.get("tr_cont"))!="1" or not isinstance(body.get("tr_cont_key"),str):
                raise ValueError("해외 잔고 연속조회 정보 오류")
            k=body["tr_cont_key"]
            if not k or k in seen: raise ValueError("해외 잔고 연속조회 중복 키")
            seen.add(k)
            params={"tr_cont":"1","tr_cont_key":k}
        else:
            raise ValueError("해외 잔고 연속조회 한도 초과")
        soxl=sum((Decimal(row["수량"]) for row in holdings if row["종목"]=="SOXL"),Decimal("0"))
        return {"원화 주문 가능 현금(원)":str(won),"USD 외화 예수금($)":str(usd),
                "SOXL 보유 수량(주)":str(soxl),"해외 보유 종목":holdings,
                "status":"조회 완료 · 미체결/국내 보유 검증 전","live_orders_enabled":False}
    except (requests.RequestException,TypeError,KeyError) as exc:
        raise ValueError("계좌 조회 실패 또는 응답 형식 오류") from None
