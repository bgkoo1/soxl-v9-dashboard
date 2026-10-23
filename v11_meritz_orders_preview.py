"""Read-only Meritz overseas outstanding-order preview; never certifies whole account flat."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import requests
from v11_meritz_account_probe import _mac
from v11_meritz_auth_probe import BASE_URL

def _qty(value):
    try:
        if isinstance(value,bool): raise ValueError()
        n=Decimal(str(value))
        if not n.is_finite() or n<0: raise ValueError()
        return n
    except (InvalidOperation,TypeError,ValueError):
        raise ValueError("미체결 수량 형식 오류") from None

def _pages(sender,path,headers,params):
    seen=set()
    for _ in range(100):
        r=sender.get(BASE_URL+path,headers=headers,params=params,timeout=15)
        body=r.json()
        if not isinstance(body,dict) or not r.ok or str(body.get("rsp_cd")) not in ("5766","5762","5820"):
            raise ValueError("해외 주문 조회 업무 응답 오류")
        data=body.get("data")
        if not isinstance(data,list):
            raise ValueError("해외 주문 목록 형식 오류")
        yield data
        cont=str(body.get("tr_cont","0"))
        if cont=="0": return
        key=body.get("tr_cont_key")
        if cont!="1" or not isinstance(key,str) or not key or key in seen:
            raise ValueError("해외 주문 연속조회 오류")
        seen.add(key)
        params=dict(params,tr_cont="1",tr_cont_key=key)
    raise ValueError("해외 주문 연속조회 한도 초과")

def preview_overseas_orders(secrets, *, session=None):
    sender=session if session is not None else requests
    mac=_mac(secrets)
    key=str(secrets.get("MERITZ_APP_KEY","")).strip()
    secret=str(secrets.get("MERITZ_APP_SECRET","")).strip()
    if not key or not secret: raise ValueError("메리츠 인증정보 누락")
    try:
        auth=sender.post(BASE_URL+"/oauth2/token",
            data={"grant_type":"client_credentials","client_id":key,"client_secret":secret,"scope":"login"},
            headers={"Content-Type":"application/x-www-form-urlencoded"},timeout=12)
        auth.raise_for_status()
        token=auth.json().get("access_token")
        if not isinstance(token,str) or not token: raise ValueError("인증 실패")
        headers={"authorization":"Bearer "+token,"mac_address":mac}
        outstanding=[]
        for page in _pages(sender,"/trading/v1/overseas/orders/today",headers,
                           {"cntg_cls_code":"2","seln_byov_clcd":"0"}):
            for row in page:
                if not isinstance(row,dict): raise ValueError("주문 행 형식 오류")
                qty=_qty(row.get("ncnt_qty"))
                if qty>0:
                    outstanding.append({"구분":"당일 미체결","종목":str(row.get("ovrs_stck_symb_iscd") or row.get("iscd") or "미확인"),
                        "미체결 수량":str(qty),"주문번호":str(row.get("oder_no",""))})
        # The reserved-orders query uses a bounded lookback. Older active reservations may exist.
        today=datetime.now(timezone.utc).date()
        start=(today-timedelta(days=90)).strftime("%Y%m%d")
        end=(today+timedelta(days=365)).strftime("%Y%m%d")
        reserved=[]
        for page in _pages(sender,"/trading/v1/overseas/reserved-orders",headers,
                           {"iqry_sta_date":start,"iqry_fin_date":end,"seln_byov_clcd":"0",
                            "iqry_idnt_clcd":"2"}):
            for row in page:
                if not isinstance(row,dict): raise ValueError("예약주문 행 형식 오류")
                if str(row.get("rsev_cncl_yn","N")).upper()=="Y": continue
                reserved.append({"구분":"예약 주문","종목":str(row.get("ovrs_stck_symb_iscd") or row.get("iscd") or "미확인"),
                    "미체결 수량":str(_qty(row.get("rsev_qty",row.get("odqt")))),
                    "주문번호":str(row.get("rsev_acpt_srno",""))})
        return {"orders":outstanding+reserved,"today_count":len(outstanding),
                "reserved_count":len(reserved),"complete_account_orders_verified":False,
                "live_orders_enabled":False,
                "note":"당일 해외 미체결 및 제한 기간 예약주문 조회입니다. 국내 주문·이전 날짜 유효 주문 및 기타 주문 유형의 부재를 증명하지 않습니다."}
    except (requests.RequestException,KeyError,TypeError,AttributeError):
        raise ValueError("해외 주문 조회 통신 또는 응답 오류") from None
