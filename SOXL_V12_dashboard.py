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
    """Full V12 operator workspace, preserving actual transaction journal."""
    from v12_full_workspace import render_full_workspace
    st.sidebar.caption("V12 Final 6분할 · 조회 전용 · 실제 매매기록 사용자 확인")
    try:
        raw=_read_prices()
        research=_run_research(raw,100_000_000)
    except Exception:
        st.error("V12 가격 데이터 또는 연구 엔진을 불러오지 못했습니다. 데이터 및 배포 로그를 확인하세요.")
        return
    render_full_workspace(raw,research)
