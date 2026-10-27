"""Conservative reconciliation of broker snapshot to V12 manually confirmed slots.

Informational only. Does not call broker APIs or generate executable orders.
All trade sizing is blocked unless completeness, valuation, and safety gates pass.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from v12_manual_holdings import validate_ledger, LedgerError


def dec(x, label, positive=False):
    try:
        if isinstance(x, bool):
            raise ValueError()
        value=Decimal(str(x))
    except (InvalidOperation, TypeError, ValueError):
        raise LedgerError(f"{label}: 숫자가 올바르지 않습니다.") from None
    if not value.is_finite() or value<0 or (positive and value<=0):
        raise LedgerError(f"{label}: 0 이상 유한값을 입력해야 합니다.")
    return value


def reconcile_portfolio(slots, broker_snapshot, *, soxl_close=None, fx_krw_per_usd=None,
                        risk_blocked=False, orders_verified_empty=False,
                        quote_fresh=False, broker_fresh=False, fx_fresh=False):
    """Compute estimated total NAV and NAV/6 advisory, fail closed for mismatches.

    This is NOT a confirmed broker NAV: closing prices and FX are market estimates.
    Slots refer exclusively to user-entered actual holdings; research slots excluded.
    """
    if not isinstance(broker_snapshot,dict):
        raise LedgerError("증권사 잔고 조회값이 없습니다.")
    positions=validate_ledger(slots)
    quantity=sum((dec(p["quantity"],"슬롯 수량",True) for p in positions),Decimal("0"))
    broker_qty=dec(broker_snapshot.get("SOXL 보유 수량(주)"),"SOXL 계좌 보유수량")
    usd_cash=dec(broker_snapshot.get("USD 외화 예수금($)"),"USD 현금")
    krw_cash=dec(broker_snapshot.get("원화 주문 가능 현금(원)"),"원화 현금")
    other_holdings=[p for p in broker_snapshot.get("해외 보유 종목",[])
                    if str(p.get("종목","")).upper()!="SOXL"
                    and dec(p.get("수량"),"기타 종목 수량")>0]
    price=dec(soxl_close,"SOXL 참고 종가",True) if soxl_close is not None else None
    fx=dec(fx_krw_per_usd,"USD/KRW 참고환율",True) if fx_krw_per_usd is not None else None
    usd_stock_value=(broker_qty*price) if price is not None else None
    usd_nav=usd_cash+usd_stock_value+krw_cash/fx if (fx is not None and usd_stock_value is not None) else None
    krw_nav=usd_nav*fx if usd_nav is not None else None
    blockers=[]
    if len(positions)>6: blockers.append("사용자 보유 슬롯이 V12 최대 6개를 초과")
    if quantity!=broker_qty: blockers.append(f"복구 슬롯 수량 {quantity}주와 메리츠 {broker_qty}주 불일치")
    if other_holdings: blockers.append("계좌에 SOXL 이외의 해외 보유종목 존재 — NAV 범위 불완전")
    if not orders_verified_empty: blockers.append("해외 미체결·예약주문이 없다는 확인 부족")
    if price is None or not quote_fresh: blockers.append("SOXL 참고 가격의 최신성 미확인")
    if fx is None or not fx_fresh: blockers.append("환율의 최신성 미확인")
    if not broker_fresh: blockers.append("메리츠 잔고 스냅샷이 최신 상태로 검증되지 않음")
    if risk_blocked: blockers.append("Risk B 또는 직전 4거래일 연속 하락 신규매수 제한")
    if not positions and broker_qty>0: blockers.append("실제 보유 슬롯 등록 필요")
    if usd_nav is not None and usd_nav<=0: blockers.append("평가 NAV가 0 이하")
    free_slots=max(0,6-len(positions))
    if free_slots==0: blockers.append("보유 슬롯 6개로 신규 매수 불가")
    # Spendable USD only. KRW equivalent is valuated, never presumed already converted.
    amount=min(usd_nav/6,usd_cash) if usd_nav is not None else None
    if amount is not None and amount<=0: blockers.append("USD 주문 가능 현금 부족")
    return {
        "broker_qty":str(broker_qty),"ledger_qty":str(quantity),
        "slots":len(positions),"free_slots":free_slots,
        "usd_cash":str(usd_cash),"krw_cash":str(krw_cash),
        "reference_price":str(price) if price is not None else None,
        "reference_fx":str(fx) if fx is not None else None,
        "soxl_value_usd":str(usd_stock_value) if usd_stock_value is not None else None,
        "nav_usd_estimate":str(usd_nav) if usd_nav is not None else None,
        "nav_krw_estimate":str(krw_nav) if krw_nav is not None else None,
        "one_slot_usd":str(usd_nav/6) if usd_nav is not None else None,
        "base_buy_usd_reference":str(amount) if amount is not None else None,
        "crash_buy_usd_reference":str(min(usd_nav/6,max(usd_cash-amount,Decimal(0)))) if amount is not None and free_slots>=2 else "0",
        "blockers":blockers,
        "buy_preview_ready":not blockers,
        "warning":"참고 종가·환율 기준 추정치. 실제 증권사 총자산 또는 주문 가능 금액 보증 아님. 주문 실행 기능 없음.",
    }
