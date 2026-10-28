"""SOXL V12 research workspace — broker READ ONLY. User-approved corrections are saved only to separate encrypted V12 ledger."""
from pathlib import Path
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import streamlit as st
from SOXL_V12_backtest import run_v12_backtest
from v12_manual_holdings import load_portfolio_state, save_ledger, reopen_slot, close_manual_slot, reconcile_total, LedgerError
from decimal import Decimal
from datetime import datetime, timezone
import requests
from v12_portfolio_reconciliation import reconcile_portfolio
from v12_full_order_console import render_v12_full_console
from v11_meritz_readonly_adapter import get_readonly_connection_status
from v11_meritz_account_preview import MeritzAccountReadError

NAME = "SOXL V12 Final · 6분할"
RULES = {
    "분할": "6분할 / 최대 6슬롯",
    "익절": "매수가 대비 LOC +3.2%",
    "손절": "D2 종가 -10% 이하 → D3 MOC",
    "조건부 전체 청산": "D2 손절 신호 + 20일 로그수익률 변동성 ≥ 10% → D3 MOC",
    "최대 보유": "D7 MOC",
    "급락 추가매수": "전일 종가 × 0.91 LOC (추가 1슬롯)",
    "매수 제한": "Risk B / 직전 4거래일 연속 하락",
    "TB3": "OFF",
}

@st.cache_data(ttl=180, show_spinner=False)
def _read_prices():
    from backtest_engine import load_data
    p = Path(__file__).resolve().parent / "SOXL_adjusted.csv"
    df = load_data(p)
    if df.empty:
        raise ValueError("SOXL 가격 데이터가 비어 있습니다")
    return df

@st.cache_data(ttl=180, show_spinner=False)
def _run_research(data, capital):
    # Historical whole-series model. Does not inherit legacy V10/V11 slots.
    return run_v12_backtest(raw_df=data, initial_capital=capital)

@st.cache_data(ttl=120,show_spinner=False)
def _market_reference(ticker):
    """Reference quote with source timestamp; never treated as a guaranteed fill."""
    url=f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    response=requests.get(url,params={"range":"5d","interval":"1d"},
             headers={"User-Agent":"Mozilla/5.0 (SOXL V12 readonly)"},
             timeout=12)
    response.raise_for_status()
    item=(response.json().get("chart") or {}).get("result") or []
    if not item:
        raise ValueError("Yahoo 시세 결과 없음")
    meta=item[0].get("meta") or {}
    rate=meta.get("regularMarketPrice")
    stamp=meta.get("regularMarketTime")
    rate=float(rate)
    if not np.isfinite(rate) or rate<=0 or not isinstance(stamp,(int,float)):
        raise ValueError("Yahoo 시세/시각 검증 실패")
    age=(datetime.now(timezone.utc)-datetime.fromtimestamp(stamp,timezone.utc)).total_seconds()
    # 4 calendar days allows ordinary US three-day holiday weekends but no older data.
    return {"price":rate,"stamp":datetime.fromtimestamp(stamp,timezone.utc).isoformat(),
            "fresh":0 <= age <= 4*86400,"source":"Yahoo Finance (참고 시세)"}


def render_v12_dashboard():
    """Original-layout V12 actual trading dashboard, Meritz GET-only."""
    from v12_full_order_console import render_v12_full_console
    from v12_full_workspace import _restore_form
    from v12_manual_holdings import load_portfolio_state, LedgerError
    from v11_meritz_account_preview import preview_account, MeritzAccountReadError
    from v11_meritz_orders_preview import preview_overseas_orders

    st.sidebar.caption("V12 Final 6분할 · 상세 주문·슬롯 관리 · READ ONLY")
    try:
        data=_read_prices()
        result=_run_research(data,100_000_000)
    except Exception:
        st.error("V12 시세 데이터/연구 엔진 조회 오류. 파일 및 배포 로그를 확인하세요.")
        return
    try:
        state,sha=load_portfolio_state(st.secrets)
        ledger_error=None
    except LedgerError as exc:
        state={"schema_version":1,"positions":[],"closed_trades":[],"cashflows":[]}
        sha=None
        ledger_error=str(exc)

    st.caption("배포 버전 · V12-FULL-ORDER-20261028 · 기존 대시보드 디자인 + 사용자 확정 실제 거래")
    with st.expander("메리츠 조회 전용 · 계좌 새로고침",expanded=False):
        st.caption("메리츠 API가 실패해도 실제 수동 거래 원장은 유지됩니다. 주문 전송 없음.")
        if st.button("메리츠 잔고 새로 조회 (READ ONLY)",key="v12_complete_broker_refresh"):
            try:
                snapshot=preview_account(st.secrets)
                st.session_state["v12_recent_broker_snapshot"]=snapshot
                st.session_state["v12_recent_broker_read_at"]=datetime.now(timezone.utc).isoformat()
            except MeritzAccountReadError as exc:
                for name in ("v12_recent_broker_snapshot","v12_recent_broker_read_at",
                             "v12_recent_order_snapshot","v12_recent_soxl_quote","v12_recent_fx_quote"):
                    st.session_state.pop(name,None)
                st.error("메리츠 계좌 조회 실패: "+str(exc))
            except Exception:
                st.session_state.pop("v12_recent_broker_snapshot",None)
                st.session_state.pop("v12_recent_broker_read_at",None)
                st.error("메리츠 API 계좌 조회 처리 오류: 실제 잔고 갱신 중지")
            else:
                try:st.session_state["v12_recent_order_snapshot"]=preview_overseas_orders(st.secrets)
                except Exception:
                    st.session_state["v12_recent_order_snapshot"]=None
                    st.warning("미체결·예약주문 조회 실패")
                for ticker,name in [("SOXL","v12_recent_soxl_quote"),("KRW=X","v12_recent_fx_quote")]:
                    try:st.session_state[name]=_market_reference(ticker)
                    except Exception:
                        st.session_state[name]=None
                        st.warning(ticker+" 시세 조회 실패")
                st.success("계좌 조회 성공 · 메리츠 주문 전송 없음")
                st.rerun()

    render_v12_full_console(data,result,state,sha,ledger_error)
    with st.expander("가상 자동매도 취소 · 미매도 슬롯 실제 보유 복구",expanded=False):
        if ledger_error:
            st.error("암호화 원장 읽기 실패: 가상 매도 취소 저장을 차단합니다.")
        else:
            _restore_form(state,sha,data,result)
    with st.expander("V12 연구용 백테스트 자산곡선 · 전체 거래",expanded=False):
        eq=result.get("equity")
        if isinstance(eq,pd.DataFrame) and not eq.empty:
            st.line_chart(eq.set_index("Date")["Equity"],height=250)
        trades=result.get("trades")
        if isinstance(trades,pd.DataFrame) and not trades.empty:
            st.dataframe(trades.tail(80).iloc[::-1],hide_index=True,use_container_width=True)
        st.caption("연구용 이상적 가상 체결이며 실제 계좌 체결기록과 별도입니다.")
