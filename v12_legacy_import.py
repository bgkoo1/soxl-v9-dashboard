"""Read-only preview of legacy V9/V10/V11 encrypted saved lot overrides.
No legacy portfolio writes. Saved quantities do NOT imply still-owned shares.
"""
from decimal import Decimal,InvalidOperation
from datetime import date
import base64,json,re
import requests
from cryptography.fernet import Fernet,InvalidToken
from v12_manual_holdings import LedgerError

def infer_date(key):
    key=str(key)
    m=re.search(r"(20[0-9]{2}-[01][0-9]-[0-3][0-9])",key)
    if m:
        try:return date.fromisoformat(m.group(1)).isoformat()
        except ValueError:pass
    return None

def saved_candidates(payload):
    if not isinstance(payload,dict):
        raise LedgerError("기존 계좌 원장 형식 오류")
    qty_map=payload.get("quantities") or {}
    prices=payload.get("entry_prices") or {}
    excluded=set(payload.get("excluded_positions") or [])
    if not isinstance(qty_map,dict) or not isinstance(prices,dict):
        raise LedgerError("기존 원장 수량·매수가 형식 오류")
    rows=[]
    for key,quantity in qty_map.items():
        try:
            qty=Decimal(str(quantity))
            price=Decimal(str(prices.get(key,"0")))
        except (InvalidOperation,ValueError,TypeError):
            continue
        if not qty.is_finite() or qty<=0 or qty>Decimal("100000000"):
            continue
        if not price.is_finite() or price<0 or price>Decimal("10000000"):
            continue
        if key in excluded:
            continue
        d=infer_date(key)
        if not d:continue
        rows.append({"slot_id":"legacy:"+str(key),"old_key":str(key),
                     "entry_date":d,"saved_quantity":str(qty),
                     "saved_entry_price":str(price) if price>0 else "",
                     "requires_price_check":price<=0,
                     "broker_verified":False})
    return sorted(rows,key=lambda r:(r["entry_date"],r["old_key"]),reverse=True)[:500]

def load_legacy_candidates(secrets,session=None):
    sender=session if session is not None else requests
    token=str(secrets.get("GITHUB_TOKEN","")).strip()
    crypt=str(secrets.get("PORTFOLIO_ENCRYPTION_KEY","")).strip()
    repo=str(secrets.get("GITHUB_REPO","bgkoo1/soxl-v9-dashboard")).strip()
    branch=str(secrets.get("GITHUB_BRANCH","main")).strip()
    if not all([token,crypt,repo,branch]):
        raise LedgerError("GitHub 암호화 원장 설정 누락")
    try:
        cipher=Fernet(crypt.encode())
        url=f"https://api.github.com/repos/{repo}/contents/live_portfolio.enc"
        r=sender.get(url,headers={"Authorization":"Bearer "+token,
             "Accept":"application/vnd.github+json"},params={"ref":branch},timeout=15)
        r.raise_for_status()
        obj=r.json()
        payload=json.loads(cipher.decrypt(base64.b64decode(obj["content"])).decode("utf-8"))
        return saved_candidates(payload)
    except (InvalidToken,UnicodeDecodeError,ValueError,TypeError,KeyError,requests.RequestException):
        raise LedgerError("기존 V11 암호화 원장을 안전하게 읽지 못했습니다. 기존 기록은 변경되지 않았습니다.") from None
