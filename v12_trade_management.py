"""Pure V12 actual-trade ledger operations (no broker orders, no network)."""
from datetime import date,datetime
from decimal import Decimal,InvalidOperation
from uuid import uuid4
from v12_manual_holdings import LedgerError,normalize_slot,validate_ledger

REASONS={"LOC +3.2%","D2 손절","D7 MOC","조건부 전체 MOC","수동 매도","기타"}
FLOW_TYPES={"입금","출금"}
def decimal(value,field,positive=True):
    if isinstance(value,bool):
        raise LedgerError(field+" 숫자 형식 오류")
    try:n=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError):
        raise LedgerError(field+" 숫자 형식 오류") from None
    if not n.is_finite() or (n<=0 if positive else n<0) or n>Decimal("1e12"):
        raise LedgerError(field+" 금액·수량이 올바르지 않습니다.")
    return n

def day(value,field):
    try:return date.fromisoformat(str(value)).isoformat()
    except (TypeError,ValueError):
        raise LedgerError(field+" 날짜 형식 오류") from None

def validate_closed_trades(trades):
    if not isinstance(trades,list) or len(trades)>30000:
        raise LedgerError("실제 매도 거래 목록 오류")
    ids=set();out=[]
    for t in trades:
        if not isinstance(t,dict):
            raise LedgerError("매도 거래 형식 오류")
        ident=str(t.get("id","")).strip()
        if not ident or ident in ids:raise LedgerError("거래 ID 누락·중복")
        ids.add(ident)
        entry=day(t.get("entry_date"),"매수")
        exit_date=day(t.get("exit_date"),"매도")
        if exit_date<entry:raise LedgerError("매도일이 매수일보다 빠릅니다.")
        price=decimal(t.get("entry_price"),"매수가")
        exit_price=decimal(t.get("exit_price"),"매도가")
        qty=decimal(t.get("quantity"),"매도수량")
        reason=str(t.get("exit_strategy","기타"))
        if reason not in REASONS:raise LedgerError("허용되지 않은 매도 유형")
        slot_id=str(t.get("slot_id","")).strip()
        if not slot_id or len(slot_id)>140:raise LedgerError("원본 슬롯 ID 오류")
        out.append({"id":ident,"slot_id":slot_id,"entry_date":entry,"exit_date":exit_date,
                    "entry_price":str(price),"exit_price":str(exit_price),"quantity":str(qty),
                    "exit_strategy":reason,"note":str(t.get("note",""))[:160]})
    return out

def validate_cashflows(rows):
    if not isinstance(rows,list) or len(rows)>30000:raise LedgerError("입출금 목록 오류")
    ids=set();out=[]
    for r in rows:
        if not isinstance(r,dict):raise LedgerError("입출금 형식 오류")
        ident=str(r.get("id","")).strip()
        if not ident or ident in ids:raise LedgerError("입출금 ID 누락·중복")
        ids.add(ident)
        typ=str(r.get("type",""))
        if typ not in FLOW_TYPES:raise LedgerError("입출금 구분 오류")
        cur=str(r.get("currency","USD")).upper()
        if cur not in {"USD","KRW"}:raise LedgerError("지원하지 않는 통화")
        out.append({"id":ident,"date":day(r.get("date"),"입출금"),"type":typ,"currency":cur,
                    "amount":str(decimal(r.get("amount"),"입출금 금액")),
                    "note":str(r.get("note",""))[:160]})
    return out

def update_slot(state,slot_id,*,entry_date,entry_price,quantity,note=""):
    """User correction, not a broker fill. Never silently alters closed trades."""
    positions=validate_ledger(state["positions"])
    if not any(x["slot_id"]==slot_id for x in positions):
        raise LedgerError("수정할 슬롯을 찾을 수 없습니다.")
    new=[]
    for p in positions:
        if p["slot_id"]==slot_id:
            updated={**p,"entry_date":day(entry_date,"매수"),"entry_price":str(decimal(entry_price,"매수가")),
                     "quantity":str(decimal(quantity,"실제 수량")),"note":note}
            new.append(normalize_slot(updated))
        else:new.append(p)
    return {**state,"positions":validate_ledger(new)}

def record_sale(state,slot_id,*,quantity,exit_date,exit_price,exit_strategy,note=""):
    """Real user-confirmed sale; removes shares from open lot and journals fill."""
    positions=validate_ledger(state["positions"])
    if exit_strategy not in REASONS:raise LedgerError("매도 유형을 선택하세요.")
    sold=decimal(quantity,"매도수량");price=decimal(exit_price,"실제 매도가")
    d=day(exit_date,"매도")
    match=[p for p in positions if p["slot_id"]==slot_id]
    if len(match)!=1:raise LedgerError("실제 보유 슬롯을 찾을 수 없습니다.")
    p=match[0];remaining=decimal(p["quantity"],"보유수량")-sold
    if remaining<0:raise LedgerError("매도 수량이 미매도 보유 수량을 초과합니다.")
    if d<p["entry_date"]:raise LedgerError("매도일이 매수일보다 빠릅니다.")
    new_positions=[x for x in positions if x["slot_id"]!=slot_id]
    if remaining>0:new_positions.append({**p,"quantity":str(remaining)})
    trades=validate_closed_trades(state.get("closed_trades",[]))
    trades.append({"id":uuid4().hex,"slot_id":slot_id,"entry_date":p["entry_date"],
                   "exit_date":d,"entry_price":p["entry_price"],"exit_price":str(price),
                   "quantity":str(sold),"exit_strategy":exit_strategy,"note":note})
    return {**state,"positions":validate_ledger(new_positions),"closed_trades":validate_closed_trades(trades)}

def cancel_sale(state,trade_id):
    """Explicit sale reversal restores shares and removes exactly that fill."""
    trades=validate_closed_trades(state.get("closed_trades",[]))
    match=[x for x in trades if x["id"]==trade_id]
    if len(match)!=1:raise LedgerError("취소할 실제 매도 기록을 찾을 수 없습니다.")
    t=match[0];slots=validate_ledger(state["positions"])
    held=[p for p in slots if p["slot_id"]==t["slot_id"]]
    if held:
        p=held[0]
        if p["entry_date"]!=t["entry_date"] or Decimal(p["entry_price"])!=Decimal(t["entry_price"]):
            raise LedgerError("매수가·매수일이 수정되어 원거래 자동 복구 불가")
        new=[{**p,"quantity":str(Decimal(p["quantity"])+Decimal(t["quantity"]))}
             if p["slot_id"]==t["slot_id"] else p for p in slots]
    else:
        new=slots+[{"slot_id":t["slot_id"],"entry_date":t["entry_date"],
                    "entry_price":t["entry_price"],"quantity":t["quantity"],
                    "source":"manual","model_exit":"실제 매도 취소","note":"매도 취소로 복원"}]
    return {**state,"positions":validate_ledger(new),
            "closed_trades":[x for x in trades if x["id"]!=trade_id]}

def add_cashflow(state,*,date_value,flow_type,currency,amount,note=""):
    rows=validate_cashflows(state.get("cashflows",[]))
    rows.append({"id":uuid4().hex,"date":day(date_value,"입출금"),"type":flow_type,
                 "currency":currency,"amount":str(decimal(amount,"입출금 금액")),"note":note})
    return {**state,"cashflows":validate_cashflows(rows)}

def remove_cashflow(state,flow_id):
    rows=validate_cashflows(state.get("cashflows",[]))
    left=[r for r in rows if r["id"]!=flow_id]
    if len(rows)==len(left):raise LedgerError("입출금 기록을 찾을 수 없습니다.")
    return {**state,"cashflows":left}

def cashflow_summary(state):
    totals={"USD":Decimal("0"),"KRW":Decimal("0")}
    for r in validate_cashflows(state.get("cashflows",[])):
        a=Decimal(r["amount"])*(1 if r["type"]=="입금" else -1)
        totals[r["currency"]]+=a
    return totals

def realized_summary(state):
    by_ccy=Decimal("0")
    for t in validate_closed_trades(state.get("closed_trades",[])):
        by_ccy+=(Decimal(t["exit_price"])-Decimal(t["entry_price"]))*Decimal(t["quantity"])
    return by_ccy
