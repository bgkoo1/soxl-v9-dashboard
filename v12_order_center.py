"""Restore the legacy order-first information hierarchy for V12, read-only.

Displays actual user-confirmed slots separately from the historical model.
Never treats model shares as broker shares or market quote as guaranteed MOC.
"""
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from html import escape
import pandas as pd
import streamlit as st
from v12_portfolio_reconciliation import reconcile_portfolio
from v12_manual_holdings import LedgerError

def _num(value):
    try:
        v=Decimal(str(value))
        if not v.is_finite() or v<0:
            return None
        return v
    except (InvalidOperation,ValueError,TypeError):
        return None

def entry_filter(closes):
    if len(closes)<200:
        return {"blocked":True,"reason":"MA200 산출을 위한 시세 부족","last4":0}
    prices=[float(x) for x in closes]
    streak=0
    for j in range(len(prices)-1,0,-1):
        if prices[j]<prices[j-1]:streak+=1
        else:break
    ma=sum(prices[-200:])/200
    momentum=prices[-1]/prices[-21]-1
    risk=prices[-1]/ma-1<-.15 and -.20<=momentum<-.05
    reason="Risk B 신규매수 제한" if risk else ("직전 4거래일 연속 하락 제한" if streak>=4 else "정상 신호")
    return {"blocked":risk or streak>=4,"reason":reason,"last4":streak}

def _render_box(label,value,description="",tone=""):
    background="#12283c" if tone!="warning" else "#3c2d20"
    st.markdown(
        '<div style="border:1px solid #38526d;border-radius:14px;padding:18px 21px;'
        f'background:{background};min-height:158px;color:#f8fafc">'
        f'<div style="font-size:13px;letter-spacing:.05em;color:#a9bdd4;font-weight:700">{escape(label)}</div>'
        f'<div style="font-size:25px;font-weight:800;margin:10px 0">{escape(value)}</div>'
        f'<div style="font-size:13px;color:#c4d3e3">{escape(description)}</div>'
        '</div>',unsafe_allow_html=True)

def render_order_center(data, result, manual_positions, ledger_error):
    st.markdown("### 오늘의 주문 · 실계좌 현황")
    st.caption("기존 대시보드의 주문·보유슬롯 우선 구성을 복원했습니다. 메리츠 계좌는 조회 전용이고 실제 주문은 전송되지 않습니다.")
    closes=pd.to_numeric(data["Close"],errors="coerce").dropna()
    if closes.empty:
        st.error("SOXL 시장 데이터가 없습니다.")
        return
    latest=float(closes.iloc[-1])
    market_date=pd.Timestamp(data["Date"].iloc[-1]).date()
    from datetime import datetime,timezone
    days_old=(datetime.now(timezone.utc).date()-market_date).days
    filt=entry_filter(closes.tolist())
    if days_old>4 or days_old<0:
        filt={"blocked":True,"reason":"최근 확정 종가 지연: 최신 데이터 확인 필요","last4":filt["last4"]}
    snap=st.session_state.get("v12_recent_broker_snapshot")
    asof=st.session_state.get("v12_recent_broker_read_at")
    recent=False
    if asof:
        try:
            age=(datetime.now(timezone.utc)-datetime.fromisoformat(asof)).total_seconds()
            recent=0<=age<=300
        except (ValueError,TypeError):
            pass
    q=st.session_state.get("v12_recent_soxl_quote")
    fx=st.session_state.get("v12_recent_fx_quote")
    osnap=st.session_state.get("v12_recent_order_snapshot")
    quote=_num(q.get("price")) if isinstance(q,dict) and q.get("fresh") else None
    rate=_num(fx.get("price")) if isinstance(fx,dict) and fx.get("fresh") else None
    rec=None
    blockers=[]
    if ledger_error:
        blockers.append("복구 보유 원장 확인 실패")
    if not snap:
        blockers.append("메리츠 실계좌 최신 잔고 조회 필요")
    if quote is None: blockers.append("최신 SOXL 참고시세 필요")
    if rate is None: blockers.append("최신 USD/KRW 환율 필요")
    if snap and not ledger_error:
        try:
            rec=reconcile_portfolio(
                manual_positions,snap,
                soxl_close=quote,fx_krw_per_usd=rate,
                risk_blocked=filt["blocked"],broker_fresh=recent,
                quote_fresh=quote is not None,fx_fresh=rate is not None,
                orders_verified_empty=bool(osnap is not None and not osnap.get("orders")),
            )
            blockers.extend(rec["blockers"])
        except (LedgerError,ValueError,TypeError):
            blockers.append("메리츠 실제 잔고와 등록 슬롯 대조 실패")
    allowed=bool(rec and rec.get("buy_preview_ready"))
    usd_nav=_num(rec.get("nav_usd_estimate")) if rec else None
    usd_cash=_num(rec.get("usd_cash")) if rec else None
    buy_usd=_num(rec.get("base_buy_usd_reference")) if allowed else None
    extra_usd=_num(rec.get("crash_buy_usd_reference")) if allowed else None
    qty=int((buy_usd/quote).to_integral_value(rounding=ROUND_FLOOR)) if buy_usd is not None and quote else None
    crash_price=Decimal(str(latest))*Decimal(".91")
    extra_qty=int((extra_usd/crash_price).to_integral_value(rounding=ROUND_FLOOR)) if extra_usd is not None and crash_price>0 else None
    left,right=st.columns(2,gap="medium")
    with left:
        if allowed:
            if qty>=1:
                _render_box("TODAY'S ACTION · 기본 매수 참고",f"{qty:,}주",
                    f"예상 매수금액 ${buy_usd:,.2f} · NAV/6 · 미주문 조회용")
            else:
                _render_box("TODAY'S ACTION · 기본 매수 참고","0주",
                    "확인된 가용 USD로는 참고시세 1주 미만",tone="warning")
        else:
            _render_box("TODAY'S ACTION · 신규매수","확인 필요",
                "실계좌 잔고·보유 슬롯·시세 검증 전 수량 안내 보류",tone="warning")
    with right:
        if rec:
            _render_box("LIVE STATUS · 참고 평가 NAV",
                f"${usd_nav:,.2f}" if usd_nav is not None else "미산정",
                f"USD 현금 ${usd_cash:,.2f} · 실제 등록 슬롯 {len(manual_positions)}/6" if usd_cash is not None else "잔고 검증 필요")
        else:
            _render_box("LIVE STATUS · 확정 종가",f"${latest:,.2f}",
                f"확정 가격일 {market_date} · 메리츠 잔고 조회 후 NAV 표시")
    c1,c2,c3,c4=st.columns(4)
    c1.metric("실제 보유 슬롯(사용자 등록)",f"{len(manual_positions)}/6")
    c2.metric("메리츠 SOXL 보유수량",f"{rec['broker_qty']}주" if rec else "조회 필요")
    c3.metric("기본 매수 참고금액",f"${buy_usd:,.2f}" if allowed else "—")
    c4.metric("급락 LOC 매수가",f"${crash_price:,.2f}")
    if allowed:
        st.caption(f"기본 매수 예상수량 {qty:,}주 (참고시세 ${quote:,.2f}) · 급락 LOC 추가매수 {extra_qty:,}주 (지정가 ${crash_price:,.2f}). **실제 MOC 체결수량이 아닌 추정치**")
    else:
        st.warning("다음 매수 수량은 검증 완료 후 표시합니다. "+(" / ".join(dict.fromkeys(blockers[:4])) if blockers else filt["reason"]))
    st.markdown("### 보유 슬롯 미리보기 · 목표 매도")
    if not manual_positions:
        st.info("실제 보유 원장에 등록된 슬롯이 없습니다. 아래 **가상 매도 취소·실제 보유** 탭에서 미매도 슬롯을 복구하거나 직접 등록하세요.")
    else:
        rows=[]
        for idx,lot in enumerate(manual_positions,1):
            n=_num(lot.get("quantity"))
            entry=_num(lot.get("entry_price"))
            if not n or not entry:
                continue
            target=entry*Decimal("1.032")
            invested=n*entry
            target_value=n*target
            mark=n*quote if quote is not None else None
            rows.append({
                "슬롯":idx,"매수일":lot["entry_date"],
                "보유수량(주)":str(n),
                "실제 매수가($)":f"{entry:,.2f}",
                "매수금액($)":f"{invested:,.2f}",
                "LOC 목표매도가($)":f"{target:,.2f}",
                "목표매도금액($)":f"{target_value:,.2f}",
                "현재 평가액($)":f"{mark:,.2f}" if mark is not None else "시세 확인 필요",
                "목표매도금액(원)":f"{(target_value*rate):,.0f}" if rate else "환율 확인 필요",
                "상태":"실제 보유(수동 확인)",
            })
        if rows:
            st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)
            st.caption("슬롯별 매수금액과 목표매도금액은 실제 등록 수량 × 매수가/LOC 목표가로 계산합니다. 확정 체결금액이나 증권사 평가손익이 아닙니다.")
    st.markdown("### 상세 주문 · 슬롯 편집 · 실제 체결가 관리")
    st.caption("슬롯을 복구·정정하려면 아래 ‘가상 매도 취소·실제 보유’ 탭을 사용하세요. 연구용 가상 청산 슬롯과 실제 보유 슬롯을 혼합하지 않습니다.")
    if rec:
        st.caption(f"실계좌 잔고 조회 상태: {'최근 5분 이내' if recent else '조회값 오래됨'} · 현금 {rec['usd_cash']} USD")
    st.caption(f"시장 신호: {filt['reason']} · 데이터 {market_date} 기준. 기본 MOC 매수의 실제 API 접수 가능 여부는 별도 검증이 필요합니다.")
