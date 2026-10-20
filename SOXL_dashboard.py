import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import json
import base64
import requests
from cryptography.fernet import Fernet, InvalidToken
from pathlib import Path
from datetime import datetime, date, time, timedelta
import uuid
from zoneinfo import ZoneInfo

try:
    import yfinance as yf
except ImportError:
    yf = None

from backtest_engine import load_data, run_backtest
from data_updater import update_soxl_data
from adaptive_strategy_test import (
    add_market_signals,
    is_risk_b,
    run_risk_divisor_backtest,
)
from SOXL_V9_backtest import (
    run_v9_backtest,
    TB3_LOC_PLUS_DOLLAR,
)
# V11 engine is embedded to avoid separate-module loading failures on Streamlit Cloud.
import numpy as np
from SOXL_V9_backtest import (
    add_v9_signals, is_risk_b, calculate_v9_statistics,
    INITIAL_CAPITAL, POSITION_DIVISOR, MAX_POSITIONS,
    TARGET_RETURN, HOLDING_DAYS, RESERVE_RATIO,
)
CRASH_THRESHOLD = -0.09
CRASH_LOC_MULTIPLIER = 0.91
from SOXL_V10_backtest import run_v10_backtest

def run_v11_backtest(
    raw_df,
    initial_capital=INITIAL_CAPITAL,
    start_date=None,
    end_date=None,
    crash_threshold=CRASH_THRESHOLD,
    position_divisor=POSITION_DIVISOR,
    max_positions=MAX_POSITIONS,
    target_return=TARGET_RETURN,
    tb3_loc_plus_dollar=TB3_LOC_PLUS_DOLLAR,
    holding_days_limit=HOLDING_DAYS,
    reserve_ratio=RESERVE_RATIO,
    enable_risk_b=True,
    enable_tb3=True,
    enable_crash_buy=True,
    crash_extra_slots=1,
    d2_cut=-0.10,
    pause_streak=4,
    seed_positions=None,
    seed_cash=None,
    seed_nav=None,
):
    """V11 Final: V10 with TB3 disabled, D2 early exit, 4-down-day entry pause.

    - Base buy: previous NAV / 7 at the day's Close.
    - Extra crash buy: if today's Close <= previous trading day's Close * 0.91,
      buy one additional NAV/7 slot at today's Close.
    - Risk B blocks both new buys.
    - Maximum 7 open slots; same-day sale proceeds cannot fund same-day buys.
    - Existing V9 TB3 dynamic LOC and TIME 7 rules are unchanged.
    """
    crash_threshold = float(crash_threshold)
    crash_loc_multiplier = 1.0 + crash_threshold
    position_divisor = max(1, int(position_divisor))
    max_positions = max(1, int(max_positions))
    target_return = float(target_return)
    tb3_loc_plus_dollar = float(tb3_loc_plus_dollar)
    holding_days_limit = max(1, int(holding_days_limit))
    reserve_ratio = min(max(float(reserve_ratio), 0.0), 0.95)
    crash_extra_slots = max(0, int(crash_extra_slots))
    enable_tb3 = False  # V11 Final: TB3 permanently disabled

    df = add_v9_signals(raw_df)
    df["Prev_Close"] = df["Close"].shift(1)
    down = df["Close"].diff().lt(0)
    streak = np.zeros(len(df), dtype=int)
    for i in range(len(df)):
        streak[i] = (streak[i - 1] if i else 0) + 1 if bool(down.iloc[i]) else 0
    df["Pause_Streak_Prev"] = pd.Series(streak, index=df.index).shift(1).fillna(0).astype(int)

    if start_date is not None:
        df = df[df["Date"] >= pd.Timestamp(start_date)].copy()
    if end_date is not None:
        df = df[df["Date"] <= pd.Timestamp(end_date)].copy()

    df = df.reset_index(drop=False).rename(columns={"index": "Original_Index"})
    if df.empty:
        raise ValueError("선택한 기간에 데이터가 없습니다.")

    cash = float(initial_capital if seed_cash is None else seed_cash)
    positions, trades, equity_records, daily_records = list(seed_positions or []), [], [], []
    previous_nav = float(initial_capital if seed_nav is None else seed_nav)
    max_open_positions = len(positions)
    risk_days = risk_skip_days = tb3_days = 0
    normal_buy_days = blocked_buy_days = 0
    crash_signal_days = crash_extra_buy_days = crash_slots_bought = 0
    position_seq = 0

    for _, row in df.iterrows():
        date = pd.Timestamp(row["Date"])
        close_price = float(row["Close"])
        prev_close = float(row["Prev_Close"]) if pd.notna(row["Prev_Close"]) else np.nan
        original_index = int(row["Original_Index"])

        cash_before_sales = float(cash)
        sale_proceeds_today = 0.0
        remaining_positions = []

        ma_gap = row["Signal_MA200_Gap"]
        momentum = row["Signal_Momentum_20D"]
        risk_state = bool(enable_risk_b and is_risk_b(ma_gap, momentum))
        tb3_state = bool(enable_tb3 and row["TB3_State"])
        crash_state = bool(
            enable_crash_buy
            and crash_extra_slots > 0
            and pd.notna(prev_close)
            and close_price <= prev_close * crash_loc_multiplier
        )

        if risk_state:
            risk_days += 1
        if tb3_state:
            tb3_days += 1
        if crash_state:
            crash_signal_days += 1

        # exits first; proceeds remain unavailable for today's buys
        for position in positions:
            holding_days = original_index - position["entry_original_index"]
            entry_price = float(position["entry_price"])
            shares = float(position["shares"])
            invested = float(position["invested"])
            normal_target = entry_price * (1 + target_return)
            if tb3_state:
                active_target = entry_price + tb3_loc_plus_dollar
                target_mode = "TB3_DYN_LOC"
            else:
                active_target = normal_target
                target_mode = "NORMAL_LOC"

            exit_type = None
            if close_price >= active_target:
                exit_type = target_mode
            elif d2_cut is not None and holding_days >= 3 and position.get("d2_flag", False):
                exit_type = "D2_CUT_NEXT_MOC"
            elif holding_days >= holding_days_limit:
                exit_type = "TIME"

            if exit_type is None:
                remaining_positions.append(position)
                continue

            proceeds = shares * close_price
            sale_proceeds_today += proceeds
            trades.append({
                "Position_ID": position.get("position_id"),
                "Buy_Type": position.get("buy_type", "기본 MOC"),
                "Entry_Date": position["entry_date"],
                "Exit_Date": date,
                "Entry_Price": entry_price,
                "Exit_Price": close_price,
                "Normal_Target_Price": normal_target,
                "Active_Target_Price": active_target,
                "Shares": shares,
                "Invested": invested,
                "Holding_Days": holding_days,
                "Return": close_price / entry_price - 1,
                "Profit": proceeds - invested,
                "Exit_Type": exit_type,
                "TB3_On_Exit": tb3_state,
                "Risk_Entry": position["risk_entry"],
                "Signal_MA200_Gap_At_Entry": position["signal_ma_gap"],
                "Signal_Momentum_20D_At_Entry": position["signal_momentum"],
            })
        positions = remaining_positions
        # On day 2 after entry evaluate the loss; on day 3 sell at MOC.
        if d2_cut is not None:
            for position in positions:
                if original_index - position["entry_original_index"] == 2:
                    position["d2_flag"] = bool(close_price / position["entry_price"] - 1 <= d2_cut)

        target_buy_amount = previous_nav / position_divisor
        reserve_amount = previous_nav * reserve_ratio
        available_cash = max(0.0, cash_before_sales - reserve_amount)
        total_buy_amount = 0.0
        buys_today = 0
        crash_extra_bought = False

        pause_now = pause_streak > 0 and int(row["Pause_Streak_Prev"]) >= pause_streak
        if len(positions) < max_positions:
            if risk_state or pause_now:
                risk_skip_days += 1
            else:
                desired_buys = 1 + crash_extra_slots if crash_state else 1
                for buy_index in range(desired_buys):
                    if len(positions) >= max_positions or available_cash <= 0 or target_buy_amount <= 0:
                        break
                    buy_amount = min(target_buy_amount, available_cash)
                    if buy_amount <= 0:
                        break
                    shares = buy_amount / close_price
                    position_seq += 1
                    buy_type = (f"급락 LOC ({crash_threshold:.1%})" if buy_index >= 1 else "기본 MOC")
                    positions.append({
                        "position_id": f"v11-{date.date().isoformat()}-{position_seq:06d}",
                        "buy_type": buy_type,
                        "entry_date": date,
                        "entry_original_index": original_index,
                        "entry_price": close_price,
                        "shares": shares,
                        "invested": buy_amount,
                        "risk_entry": risk_state,
                        "signal_ma_gap": float(ma_gap) if pd.notna(ma_gap) else np.nan,
                        "signal_momentum": float(momentum) if pd.notna(momentum) else np.nan,
                    })
                    available_cash -= buy_amount
                    total_buy_amount += buy_amount
                    buys_today += 1
                    if buy_index >= 1:
                        crash_extra_bought = True
                if buys_today > 0:
                    normal_buy_days += 1
                elif target_buy_amount > 0:
                    blocked_buy_days += 1
        elif target_buy_amount > 0 and not risk_state and not pause_now:
            blocked_buy_days += 1

        if crash_state:
            crash_slots_bought += buys_today
            if crash_extra_bought:
                crash_extra_buy_days += 1

        cash = cash_before_sales - total_buy_amount + sale_proceeds_today
        position_value = sum(p["shares"] * close_price for p in positions)
        total_equity = cash + position_value
        max_open_positions = max(max_open_positions, len(positions))

        equity_records.append({
            "Date": date,
            "Cash": cash,
            "Position_Value": position_value,
            "Equity": total_equity,
            "Open_Positions": len(positions),
        })
        daily_records.append({
            "Date": date,
            "Previous_NAV": previous_nav,
            "Risk_State": risk_state,
            "Pause_State": bool(pause_now),
            "Previous_Down_Streak": int(row["Pause_Streak_Prev"]),
            "TB3_State": tb3_state,
            "Crash_State": crash_state,
            "Prev_Close": prev_close,
            "Crash_LOC_Price": prev_close * crash_loc_multiplier if pd.notna(prev_close) else np.nan,
            "Target_Buy_Amount": target_buy_amount,
            "Actual_Buy_Amount": total_buy_amount,
            "Buy_Slots": buys_today,
            "Cash_Before_Sales": cash_before_sales,
            "Sale_Proceeds_Today": sale_proceeds_today,
            "End_Cash": cash,
            "Open_Positions": len(positions),
            "Equity": total_equity,
        })
        previous_nav = total_equity

    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(equity_records)
    daily_df = pd.DataFrame(daily_records)
    stats = calculate_v9_statistics(trades_df, equity_df, initial_capital, max_open_positions)
    stats.update({
        "risk_days": int(risk_days),
        "risk_skip_days": int(risk_skip_days),
        "tb3_days": int(tb3_days),
        "normal_buy_days": int(normal_buy_days),
        "blocked_buy_days": int(blocked_buy_days),
        "crash_signal_days": int(crash_signal_days),
        "crash_extra_buy_days": int(crash_extra_buy_days),
        "crash_slots_bought": int(crash_slots_bought),
        "crash_threshold": float(crash_threshold),
        "position_divisor": int(position_divisor),
        "max_positions_limit": int(max_positions),
        "target_return": float(target_return),
        "holding_days_limit": int(holding_days_limit),
        "reserve_ratio": float(reserve_ratio),
        "enable_risk_b": bool(enable_risk_b),
        "enable_tb3": False,
        "d2_cut": d2_cut,
        "pause_streak": int(pause_streak),
        "enable_crash_buy": bool(enable_crash_buy),
        "crash_extra_slots": int(crash_extra_slots),
    })
    return {
        "stats": stats,
        "trades": trades_df,
        "equity": equity_df,
        "daily": daily_df,
        "open_positions": positions,
        "signal_data": df,
    }


def run_v11_live_transition(raw_df, initial_capital=INITIAL_CAPITAL,
                            start_date=None, end_date=None,
                            transition_date="2026-10-19"):
    """V10 history through the prior trading day, then V11 from transition_date.

    Preserves V10 position_id, entry_date, entry index, and synthetic model shares
    for ALL still-open legacy slots. Thus existing encrypted V10 overrides remain
    addressable by their original v10| keys. Never edits persistence storage.
    """
    switch = pd.Timestamp(transition_date).normalize()
    prices = raw_df.copy()
    prices["Date"] = pd.to_datetime(prices["Date"]).dt.normalize()
    legacy_end = prices.loc[prices["Date"] < switch, "Date"].max()
    if pd.isna(legacy_end):
        raise ValueError("V11 전환일 이전에 V10 가격 데이터가 없습니다.")
    legacy = run_v10_backtest(
        raw_df=raw_df, initial_capital=initial_capital,
        start_date=start_date, end_date=legacy_end,
    )
    target_end = pd.Timestamp(end_date).normalize() if end_date is not None else prices["Date"].max()
    if target_end < switch or not (prices["Date"].between(switch, target_end)).any():
        legacy["transition_date"] = switch
        legacy["strategy_phase"] = "V10_LEGACY"
        return legacy

    # Shallow-copy dicts to avoid mutating snapshots from the V10 engine.
    carried = [dict(pos) for pos in legacy["open_positions"]]
    next_period = run_v11_backtest(
        raw_df=raw_df, initial_capital=initial_capital,
        start_date=switch, end_date=target_end,
        seed_positions=carried,
        seed_cash=float(legacy["equity"]["Cash"].iloc[-1]),
        seed_nav=float(legacy["equity"]["Equity"].iloc[-1]),
    )
    full_trades = pd.concat([legacy["trades"], next_period["trades"]], ignore_index=True)
    full_equity = pd.concat([legacy["equity"], next_period["equity"]], ignore_index=True)
    full_daily = pd.concat([legacy["daily"], next_period["daily"]], ignore_index=True)
    next_period["trades"] = full_trades
    next_period["equity"] = full_equity
    next_period["daily"] = full_daily
    next_period["stats"] = calculate_v9_statistics(
        full_trades, full_equity, initial_capital,
        int(max(legacy["stats"].get("max_open_positions", 0), next_period["stats"].get("max_open_positions", 0)))
    )
    next_period["transition_date"] = switch
    next_period["strategy_phase"] = "V11_FROM_SWITCH"
    next_period["carried_v10_slot_ids"] = [p.get("position_id") for p in carried]
    return next_period


from SOXL_V10_backtest import (
    run_v10_backtest,
    CRASH_THRESHOLD as V10_CRASH_THRESHOLD,
    CRASH_LOC_MULTIPLIER as V10_CRASH_LOC_MULTIPLIER,
)

# ============================================================
# PAGE CONFIG
# ============================================================

st.set_page_config(
    page_title="SOXL 퀀트 전략 대시보드",
    page_icon="📈",
    layout="wide",
)


# ============================================================
# STYLE
# ============================================================

st.markdown(
    """
    <style>
    .block-container {
        width: 100%;
        max-width: 1600px;
        padding-top: 1.2rem;
        padding-bottom: 3rem;
        padding-left: clamp(0.8rem, 2.2vw, 2.4rem);
        padding-right: clamp(0.8rem, 2.2vw, 2.4rem);
    }

    [data-testid="stMetric"] {
        min-width: 0;
    }

    [data-testid="stMetricValue"] {
        font-size: clamp(1.15rem, 1.8vw, 1.65rem);
        white-space: normal;
        overflow-wrap: anywhere;
    }

    [data-testid="stMetricLabel"] {
        font-size: clamp(0.78rem, 1.1vw, 0.9rem);
        white-space: normal;
    }

    [data-testid="stDataFrame"],
    [data-testid="stDataEditor"] {
        width: 100%;
        max-width: 100%;
        overflow-x: auto;
    }

    [data-testid="stHorizontalBlock"] {
        gap: clamp(0.45rem, 1vw, 1rem);
    }

    .stTabs [data-baseweb="tab-list"] {
        gap: 0.25rem;
        overflow-x: auto;
        scrollbar-width: thin;
        flex-wrap: nowrap;
    }

    .stTabs [data-baseweb="tab"] {
        flex: 0 0 auto;
        white-space: nowrap;
        padding-left: 0.7rem;
        padding-right: 0.7rem;
    }

    @media (max-width: 900px) {
        .block-container {
            padding-left: 0.75rem;
            padding-right: 0.75rem;
        }

        h1 { font-size: 1.65rem !important; }
        h2 { font-size: 1.3rem !important; }
        h3 { font-size: 1.08rem !important; }

        [data-testid="stMetricValue"] {
            font-size: 1.12rem;
        }

        [data-testid="stMetricLabel"] {
            font-size: 0.76rem;
        }

        .stButton > button,
        .stDownloadButton > button {
            width: 100%;
            min-height: 2.55rem;
        }
    }

    @media (max-width: 640px) {
        .block-container {
            padding-top: 0.7rem;
            padding-left: 0.55rem;
            padding-right: 0.55rem;
        }

        .stTabs [data-baseweb="tab"] {
            font-size: 0.82rem;
            padding-left: 0.45rem;
            padding-right: 0.45rem;
        }
    }

    /* SOXL UI refresh: presentation only, no order/state changes */
    :root { --soxl-navy:#142d50; --soxl-blue:#2563eb; --soxl-line:#e1e8f1; }
    .stApp { background: #f4f7fb; }
    [data-testid="stHeader"] { background: rgba(244,247,251,0.93); }
    [data-testid="stSidebar"] { background: #edf2f8; border-right:1px solid var(--soxl-line); }
    .block-container { max-width: 1760px; padding-top: 1.05rem; }
    h1,h2,h3 { color: #173253; letter-spacing:-0.025em; }
    h1 { font-weight:800 !important; font-size: clamp(1.75rem,2.3vw,2.35rem) !important; }
    h2,h3 { font-weight:750 !important; }
    [data-testid="stMetric"] {
        background:#fff; border:1px solid var(--soxl-line);
        border-radius:14px; padding:1rem 1.1rem; min-height:112px;
        box-shadow:0 3px 12px rgba(20,45,80,0.035);
    }
    [data-testid="stMetricLabel"] { color:#64748b; font-weight:650; }
    [data-testid="stMetricValue"] { color:#142d50; font-weight:800; letter-spacing:-0.035em; }
    [data-testid="stMetricDelta"] { font-weight:650; }
    [data-testid="stVerticalBlockBorderWrapper"] > div {
        border-color:var(--soxl-line);
    }
    [data-testid="stDataFrame"],[data-testid="stDataEditor"] {
        border:1px solid var(--soxl-line); border-radius:12px; background:#fff;
    }
    .stTabs [data-baseweb="tab-list"] {
        background:#eaf0f8; padding:.3rem; border-radius:12px; gap:.25rem;
    }
    .stTabs [data-baseweb="tab"] {
        border-radius:9px; color:#53677e; font-weight:650; min-height:42px;
        padding-left:1rem; padding-right:1rem;
    }
    .stTabs [aria-selected="true"] { background:#fff; color:#1e5ac6; box-shadow:0 1px 5px rgba(20,45,80,.09); }
    .stButton > button[kind="primary"] {
        background:#205ac9; border-color:#205ac9; border-radius:10px; font-weight:700;
    }
    .stButton > button[kind="secondary"], .stDownloadButton > button {
        border-radius:10px; border:1px solid #d6e0ec; background:#fff; font-weight:650;
    }
    div[data-testid="stExpander"] { border:1px solid var(--soxl-line); background:#fff; border-radius:12px; }
    .stAlert { border-radius:12px; }
    hr { border-color:#e4eaf2 !important; margin:1.1rem 0 !important; }
    .soxl-lead { background:linear-gradient(108deg,#153355,#244e84);
        padding:1.25rem 1.5rem; border-radius:16px; color:#fff;
        margin-bottom:1.1rem; box-shadow:0 5px 22px rgba(23,52,87,.13); }
    .soxl-lead-title { color:#fff; font-size:1.55rem; font-weight:800; letter-spacing:-.025em; }
    .soxl-lead-note { color:#d9e6f8; font-size:.9rem; margin-top:.28rem; }
    @media(max-width:900px) {
       [data-testid="stMetric"] {padding:.75rem; min-height:90px;}
       .soxl-lead {padding:1rem;}
       .soxl-lead-title {font-size:1.25rem;}
    }

    /* Full dashboard redesign: condensed top bar and fixed hierarchy */
    .soxl-section-line {display:flex;justify-content:space-between;align-items:center;
      margin:.2rem 0 .8rem;font-size:1.2rem;font-weight:800;color:#173253;}
    .soxl-section-meta {font-size:.78rem;font-weight:600;color:#64748b;}
    [data-testid="stMetric"] {min-height:105px;}
    [data-testid="stMetricValue"] {font-size:clamp(1.02rem,1.45vw,1.5rem) !important;}
    [data-testid="stVerticalBlockBorderWrapper"] > div[data-testid="stVerticalBlock"] {
      background:#fff;border-radius:14px;
    }
    @media (max-width:900px) {.soxl-section-line {flex-direction:column;align-items:flex-start;gap:4px;}}
    /* V2: new overview panels and a clearer working hierarchy */
    .overview-signal {
      border:1px solid #dce5f1; border-radius:15px; padding:1.2rem 1.25rem;
      background:#fff; min-height:155px; margin-bottom:.8rem;
    }
    .overview-signal-buy {border-left:6px solid #159a69; background:#f2fbf6;}
    .overview-signal-danger {border-left:6px solid #d64d5c; background:#fff5f6;}
    .overview-signal-neutral {border-left:6px solid #5182d4; background:#f5f8ff;}
    .overview-signal-small {font-size:.84rem;color:#63758d;font-weight:650}
    .overview-signal-title {font-size:1.55rem;font-weight:850;color:#15365e;margin:.35rem 0;}
    .overview-signal-desc {font-size:.9rem;color:#506780;line-height:1.6;}
    div[data-testid="stVerticalBlock"] > div:has(>div[data-testid="stPlotlyChart"]) {
      border-radius:14px;
    }

    /* Clean layout v4 — one cohesive final set of visual rules */
    :root {color-scheme:light; --ui-text:#14243c; --ui-muted:#64748b; --ui-border:#e2e8f0;}
    .stApp {background:#f5f7fb !important; color:var(--ui-text) !important;}
    [data-testid="stHeader"] {background:#f5f7fb !important;}
    [data-testid="stSidebar"] {background:#f9fbff !important; border-right:1px solid var(--ui-border) !important;}
    [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {gap:.62rem !important;}
    [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {font-size:.88rem;line-height:1.45;}
    .block-container {max-width:1700px !important;padding:1.1rem 1.6rem 2.2rem !important;}
    [data-testid="stHorizontalBlock"] {gap:.9rem !important;}
    .redesign-header {display:flex;align-items:center;gap:12px;margin:8px 0 15px;min-height:55px;position:relative;z-index:2;}
    .redesign-logo {font-size:1.8rem;color:#2563eb !important;font-weight:900;}
    .redesign-headline {font-size:1.75rem;font-weight:850;color:#112446 !important;letter-spacing:-.035em;line-height:1.2;}
    .redesign-subline {font-size:.86rem;color:#64748b !important;margin-top:5px;}
    .redesign-nav-title,.soxl-side-section {font-weight:800;font-size:.84rem;color:#54657d;margin:.1rem 0 .5rem;}
    .redesign-side-links {display:flex;flex-direction:column;gap:2px;margin:5px 0;}
    .redesign-side-links a {display:block;padding:9px 10px;text-decoration:none;color:#3a4c68 !important;border-radius:9px;font-weight:650;font-size:.88rem;}
    .redesign-side-links a:hover {background:#eaf1ff;color:#1c5fd6 !important;}
    .soxl-kpi-grid {display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:10px;margin:10px 0 14px;}
    .soxl-kpi {min-width:0;height:119px;box-sizing:border-box;background:#fff;border:1px solid #e4e9f2;border-radius:13px;padding:14px 13px;box-shadow:0 3px 12px rgba(28,46,90,.035);overflow:hidden;}
    .soxl-kpi-label {font-size:.82rem;line-height:1.25;font-weight:750;color:#536681;margin:0 0 12px;}
    .soxl-kpi-value {font-size:clamp(1.08rem,1.5vw,1.48rem);font-weight:850;color:#142a50;letter-spacing:-.035em;line-height:1.16;white-space:nowrap;}
    .soxl-kpi-red .soxl-kpi-value {color:#dd3c56;}
    .soxl-kpi-description {font-size:.73rem;line-height:1.3;color:#6c7b91;margin-top:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
    [data-testid="stMetric"] {min-height:95px !important;padding:12px !important;background:#fff !important;border:1px solid #e4e9f2 !important;}
    [data-testid="stMetricValue"] {font-size:clamp(1.04rem,1.35vw,1.38rem) !important;white-space:normal !important;overflow-wrap:normal !important;color:#13294b !important;}
    [data-testid="stMetricLabel"] {font-size:.82rem !important;font-weight:700 !important;}
    [data-testid="stMetricDelta"] {font-size:.76rem !important;}
    [data-testid="stVerticalBlockBorderWrapper"] {border-radius:13px !important;border-color:#e1e7f0 !important;background:#fff !important;}
    [data-testid="stVerticalBlockBorderWrapper"] > div {background:#fff !important;}
    [data-testid="stVerticalBlockBorderWrapper"] h4 {font-size:1.1rem !important;font-weight:800 !important;margin:.1rem 0 .25rem !important;color:#182b4d !important;}
    [data-testid="stPlotlyChart"] {margin:0 !important;}
    [data-testid="stDataFrame"] {border:1px solid #e5ebf4;border-radius:9px;}
    div[data-testid="stButton"] button {font-weight:700;border-radius:9px;min-height:39px;}
    .redesign-smalltitle {font-size:.83rem;font-weight:750;color:#273e60;margin:6px 0;}
    .redesign-date {color:#68788d;font-size:.76rem;}
    .redesign-decision {border-radius:14px;padding:16px 16px;margin:4px 0 10px;min-height:100px;border:1px solid #f3c4cd;}
    .redesign-buy {background:linear-gradient(135deg,#ffe8ec,#fff5f7);}
    .redesign-wait {background:linear-gradient(135deg,#fff2f4,#fff8f9);}
    .redesign-decision-title {font-size:1.55rem;color:#cc2343;font-weight:850;line-height:1.25;}
    .redesign-decision-info {font-size:.82rem;color:#7b4b58;margin-top:5px;}
    .redesign-statusline {display:flex;justify-content:space-between;gap:8px;padding:10px 12px;margin:6px 0 8px;border-radius:9px;background:#fff6f7;color:#7b6270;font-size:.83rem;border:1px solid #f4d6dc;}
    .redesign-statusline b {color:#1e8f5a;}
    .redesign-info-tag {font-size:.84rem;color:#5f5360;line-height:1.45;}
    .pair-note {font-size:.78rem;color:#7a8798;margin-top:8px;}
    .cta-fill button {min-height:56px !important; height:56px !important;}
    @media(max-width:1400px){.soxl-kpi-grid{grid-template-columns:repeat(3,minmax(0,1fr));}.soxl-kpi{height:100px;}.soxl-kpi-label{margin-bottom:7px;}.soxl-kpi-description{margin-top:7px;}}
    @media(max-width:900px){.block-container{padding:.85rem !important;}.redesign-headline{font-size:1.34rem;}.soxl-kpi-grid{grid-template-columns:repeat(2,minmax(0,1fr));}.soxl-kpi-value{font-size:1.15rem;}}

    /* Polished fintech visual system: desktop alignment and compact hierarchy */
    :root { --ui-navy:#172b4e;--ui-blue:#2063e7;--ui-edge:#e4eaf2;--ui-soft:#f6f8fc; }
    .stApp {background:#f5f8fc!important;color:#172b4e!important;}
    .block-container {max-width:1680px!important;padding:0.68rem 1.25rem 2.3rem!important;}
    [data-testid="stSidebar"] {background:#f8faff!important;}
    [data-testid="stSidebar"] [data-testid="stVerticalBlock"] {gap:.48rem!important;}
    .redesign-header {margin:1px 0 9px!important;min-height:42px!important;}
    .redesign-headline {font-size:1.62rem!important;line-height:1.1!important;}
    .redesign-subline {font-size:.78rem!important;margin-top:3px!important;}
    .soxl-kpi-grid {gap:9px!important;margin:11px 0 13px!important;}
    .soxl-kpi {height:109px!important;padding:12px 14px!important;box-shadow:0 2px 10px rgba(24,45,75,.035)!important;}
    .soxl-kpi-label {font-size:.81rem!important;margin-bottom:8px!important;}
    .soxl-kpi-value {font-size:clamp(1.02rem,1.43vw,1.48rem)!important;line-height:1.17!important;letter-spacing:-.04em;}
    .soxl-kpi-description {font-size:.73rem!important;margin-top:8px!important;}
    [data-testid="stHorizontalBlock"] {gap:.7rem!important;}
    div[data-testid="stVerticalBlockBorderWrapper"] {background:#fff!important;border:1px solid var(--ui-edge)!important;border-radius:13px!important;box-shadow:0 2px 12px rgba(26,43,70,.025)!important;}
    div[data-testid="stVerticalBlockBorderWrapper"] h4 {font-size:1.02rem!important;font-weight:800!important;letter-spacing:-.03em!important;margin:0!important;}
    div[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] p {margin-bottom:0;}
    [data-testid="stPlotlyChart"] {margin:0!important;}
    div.st-key-redesign_refresh button {height:59px!important;min-height:59px!important;font-size:.91rem!important;border-radius:10px!important;white-space:nowrap;}
    .redesign-decision {padding:14px 16px!important;min-height:105px!important;margin:3px 0 8px!important;border-radius:11px!important;border:1px solid #facbd2!important;}
    .redesign-buy {background:linear-gradient(115deg,#ffe4e9,#fff2f4)!important;}
    .redesign-wait {background:linear-gradient(115deg,#fff0f2,#fff8f8)!important;}
    .redesign-decision-title {font-size:1.55rem!important;color:#c72243!important;}
    .redesign-decision-info {color:#814a58!important;font-size:.79rem!important;}
    .redesign-statusline {background:#fff6f7!important;border:1px solid #f9e3e8!important;border-radius:9px!important;}
    .redesign-statusline b {color:#12865d!important;}
    .redesign-info-tag {font-weight:650!important;color:#324762!important;}
    .account-tiles {display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:6px;}
    .account-tile {border:1px solid #e6edf6;background:#f9fbff;border-radius:11px;padding:15px 14px;min-width:0;}
    .account-tile-total {grid-column:span 2;background:#f1f6ff;border-color:#dce7fa;}
    .account-tile-label {color:#738198;font-size:.84rem;font-weight:700;margin-bottom:8px;}
    .account-tile-value {color:#172d55;font-size:clamp(1rem,1.25vw,1.25rem);font-weight:850;letter-spacing:-.04em;white-space:nowrap;}
    .account-tile-sub {color:#4773b1;font-size:.79rem;margin-top:6px;}
    .pair-note {color:#78869b;font-size:.76rem;line-height:1.4;margin-top:7px;}
    @media (min-width:901px) and (max-width:1400px) {
       .soxl-kpi-grid {grid-template-columns:repeat(6,minmax(0,1fr))!important;}
       .soxl-kpi {height:108px!important;padding:10px 8px!important;}
       .soxl-kpi-value {font-size:clamp(.88rem,1.35vw,1.2rem)!important;}
       .soxl-kpi-label,.soxl-kpi-description {font-size:.69rem!important;}
    }
    @media(max-width:900px) {
       .block-container {padding:.65rem .7rem 2rem!important;}
       .soxl-kpi-grid {grid-template-columns:repeat(3,minmax(0,1fr))!important;}
       .soxl-kpi {height:103px!important;}
       .soxl-kpi-value {font-size:1.02rem!important;}
    }
    @media(max-width:540px) {.soxl-kpi-grid {grid-template-columns:repeat(2,minmax(0,1fr))!important;}}

/* Reference recreation V5: unified, narrow sidebar and 2-column main dashboard */
:root {--r-navy:#14294d;--r-blue:#1767ee;--r-muted:#667b97;--r-border:#e5ebf5;}
[data-testid="stAppViewContainer"] {background:#f5f8fd!important;}
[data-testid="stSidebar"] {width:230px!important;min-width:230px!important;background:#f9fbff!important;}
[data-testid="stSidebar"] > div:first-child {width:230px!important;}
[data-testid="stSidebar"] .redesign-side-links a {padding:9px 14px!important;font-size:.86rem!important;}
[data-testid="stSidebar"] .redesign-side-links a:before {content:"";display:inline-block;width:6px;height:6px;background:#4483e9;border-radius:2px;margin-right:10px;vertical-align:2px;}
.block-container {max-width:1800px!important;padding:.4rem 1rem 2rem!important;}
.redesign-header {margin:0 0 7px!important;min-height:38px!important;}
.redesign-headline {font-size:1.4rem!important;line-height:1.15!important;}
.redesign-subline {font-size:.73rem!important;margin-top:2px!important;}
[data-testid="stVerticalBlock"] {gap:.42rem!important;}
[data-testid="stHorizontalBlock"] {gap:.55rem!important;}
[data-testid="stVerticalBlockBorderWrapper"] {border-radius:9px!important;border:1px solid var(--r-border)!important;box-shadow:0 2px 7px rgba(25,54,100,.035)!important;background:#fff!important;}
[data-testid="stVerticalBlockBorderWrapper"] > div {background:#fff!important;}
.ref-kpi-grid {display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:8px;margin:7px 0 10px;}
.ref-kpi {border:1px solid var(--r-border);border-radius:9px;background:#fff;padding:12px 10px;min-width:0;height:100px;box-sizing:border-box;box-shadow:0 1px 7px rgba(25,54,100,.035);}
.ref-kpi-label {font-size:.72rem;font-weight:750;color:#536883;display:flex;align-items:center;gap:7px;white-space:nowrap;}
.ref-kpi-value {font-size:clamp(.92rem,1.4vw,1.44rem);color:var(--r-navy);font-weight:850;margin:8px 0 5px;white-space:nowrap;letter-spacing:-.035em;}
.ref-kpi.red .ref-kpi-value {color:#e23450;}
.ref-kpi-foot {color:#74849b;font-size:.69rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.ref-kpi.green .ref-kpi-foot {color:#0c9f69;font-weight:700;}
.ref-panel-head {display:flex;align-items:center;gap:7px;color:#162b51;font-size:.99rem;font-weight:800;min-height:25px;white-space:nowrap;letter-spacing:-.025em;}
.ref-panel-head span {min-width:0;text-overflow:ellipsis;overflow:hidden;}
.ref-panel-head small {font-size:.68rem;color:#8b9ab0;margin-left:auto;font-weight:600;}
.ref-chart-subtitle {font-size:.78rem;color:#263e65;font-weight:800;margin:1px 0 0;}
.ref-action-banner {background:linear-gradient(110deg,#e2efff,#d8e7ff);border:1px solid #c7dafe;border-radius:9px;padding:8px 12px;margin:1px 0 4px;}
.ref-action-main {font-size:1.16rem;font-weight:850;color:#155ed3;}
.ref-action-detail {font-size:.68rem;color:#5779a9;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.ref-action-bottom {display:flex;justify-content:space-between;align-items:center;gap:8px;color:#5d718a;font-size:.68rem;white-space:nowrap;}
.ref-action-bottom b {color:#168b5e;}.ref-action-bottom strong {color:#163568;}
.ref-account-grid {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;margin:5px 0;}
.ref-account-item {min-width:0;padding:9px 7px;border:1px solid #e9eef8;border-radius:8px;background:#f8fbff;}
.ref-account-name {font-size:.69rem;font-weight:700;color:#657b9b;white-space:nowrap;}
.ref-account-value {font-size:clamp(.75rem,.99vw,1.04rem);font-weight:850;color:#172c51;letter-spacing:-.045em;white-space:nowrap;margin:8px 0 5px;}
.ref-account-sub {font-size:.69rem;font-weight:700;color:#5b7190;}
.ref-perf-grid {display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:6px;margin:3px 0 8px;}
.ref-perf-grid > div {min-width:0;}.ref-perf-grid small {display:block;font-size:.67rem;color:#75869c;white-space:nowrap;}
.ref-perf-grid strong {font-size:clamp(.88rem,1.15vw,1.2rem);font-weight:850;color:#1565e8;white-space:nowrap;}
.ref-perf-grid > div:nth-child(3) strong {color:#db3d52;}
.ref-bar-list {display:grid;gap:5px;}
.ref-bar-row {display:grid;grid-template-columns:74px minmax(0,1fr) 65px;gap:7px;align-items:center;font-size:.66rem;color:#5d6f8a;}
.ref-bar-track {height:8px;background:#eaf0fa;border-radius:2px;overflow:hidden;}.ref-bar-fill {height:100%;background:#a3afc3;}.ref-bar-fill.main {background:#2563eb;}
.ref-bar-row b {text-align:right;font-size:.65rem;color:#587396;}
div.st-key-redesign_refresh button {height:64px!important;min-height:64px!important;white-space:nowrap;font-size:.83rem!important;}
[data-testid="stDataFrame"] {border-radius:6px!important;}
[data-testid="stDataFrame"] iframe {font-size:.75rem;}
@media(max-width:1250px) {.ref-kpi-value{font-size:.96rem;}.ref-kpi{padding:10px 7px;}.ref-kpi-label{font-size:.65rem;}.ref-account-value{font-size:.78rem;}.ref-action-bottom{font-size:.62rem;}}
@media(max-width:900px) {.ref-kpi-grid{grid-template-columns:repeat(3,minmax(0,1fr));}.ref-kpi{height:94px;}[data-testid="stSidebar"] {width:210px!important;min-width:210px!important;}}


/* V6 — stable summary layout and legible actionable order card */
.ref-kpi-grid{gap:10px!important;margin:11px 0 12px!important;}
.ref-kpi{height:112px!important;padding:14px 13px!important;border-radius:12px!important;}
.ref-kpi-label{font-size:.79rem!important;gap:8px!important;}
.ref-kpi-value{font-size:clamp(1.04rem,1.38vw,1.40rem)!important;line-height:1.12!important;margin:10px 0 7px!important;}
.ref-kpi-foot{font-size:.73rem!important;}
.ref-panel-head{font-size:1.03rem!important;min-height:27px!important;margin-bottom:6px!important;}
.ref-chart-subtitle{font-size:.85rem!important;line-height:1.4!important;margin:10px 0 5px!important;}
.ref-action-card{border-radius:12px;border:1px solid #e5eaf2;padding:10px 14px 11px;background:#fafbfd;}
.ref-action-buy{border-left:5px solid #159c68;background:linear-gradient(105deg,#f4fff9,#fff);}
.ref-action-wait{border-left:5px solid #ca4057;background:linear-gradient(105deg,#fff5f7,#fff);}
.ref-action-check{border-left:5px solid #db7d28;background:#fff9ef;}
.ref-action-state{display:flex;align-items:center;gap:8px;flex-wrap:wrap;color:#203657;line-height:1.1;}
.ref-action-state strong{font-size:1.25rem;font-weight:900;}
.ref-action-state-note{font-size:.73rem;color:#5a6d88;font-weight:600;}
.ref-action-dot{width:11px;height:11px;border-radius:50%;background:#d4485d;display:inline-block;flex-shrink:0;}
.ref-action-buy .ref-action-dot{background:#16a66e;}
.ref-action-check .ref-action-dot{background:#d98b2a;}
.ref-action-amount-label{font-size:.71rem;color:#71809a;margin-top:7px;font-weight:700;}
.ref-action-amount{font-size:clamp(1.45rem,1.9vw,1.95rem);font-weight:900;color:#183156;line-height:1.13;letter-spacing:-.035em;margin:2px 0 5px;}
.ref-action-qty{display:inline-block;background:#e9f0fc;border-radius:7px;padding:4px 8px;font-size:.9rem;color:#163d77;font-weight:800;}
.ref-action-quotes{font-size:.74rem;color:#516783;margin-top:6px;}
.ref-action-meta{display:flex;justify-content:space-between;align-items:center;gap:6px;color:#647590;font-size:.69rem;margin:6px 0 1px;flex-wrap:wrap;}
.ref-action-meta b{color:#148a61;}
.ref-account-grid{gap:8px!important;margin-top:7px!important;}
.ref-account-item{padding:11px 10px!important;}
.ref-account-name{font-size:.75rem!important;}
.ref-account-value{font-size:clamp(.84rem,1.04vw,1.10rem)!important;margin:9px 0 5px!important;}
.ref-account-sub{font-size:.72rem!important;}
.ref-perf-grid strong{font-size:clamp(.98rem,1.2vw,1.25rem)!important;}
@media(max-width:1250px){.ref-action-amount{font-size:1.55rem;}.ref-action-state-note{font-size:.68rem;}}


/* V7 — one surface per panel; remove inner colored boxes and nested scrolling */
.ref-action-card,.ref-action-buy,.ref-action-wait,.ref-action-check{
 background:transparent!important;background-image:none!important;
 border:0!important;border-radius:0!important;padding:8px 0!important;
 box-shadow:none!important;
}
.ref-action-qty{background:transparent!important;border:0!important;padding:3px 0!important;border-radius:0!important;}
.ref-action-meta{background:transparent!important;border:0!important;padding:4px 0!important;}
.ref-account-item{background:transparent!important;border:0!important;box-shadow:none!important;
 border-radius:0!important;padding:9px 9px!important;}
.ref-account-item:not(:last-child){border-right:1px solid #e5ebf4!important;}
.ref-account-grid{background:transparent!important;gap:0!important;}
.ref-bar-track{background:transparent!important;border:1px solid #e7edf6!important;}
.ref-kpi{background:#fff!important;}
.ref-panel-head{margin-bottom:7px!important;}
[data-testid="stVerticalBlockBorderWrapper"]{overflow:visible!important;}
[data-testid="stDataFrame"]{background:transparent!important;}
[data-testid="stDataFrame"] > div{background:transparent!important;}

/* V8 – compact natural-height tables. No nested iframe, no internal scroll. */
.ref-table-wrap{width:100%;overflow:visible;background:transparent;}
.ref-data-table{width:100%;table-layout:fixed;border-collapse:collapse;background:transparent;color:#273b58;font-size:.78rem;}
.ref-data-table th{font-size:.72rem;font-weight:750;color:#657994;text-align:left;padding:8px 7px;border-bottom:1px solid #dce5f1;white-space:nowrap;}
.ref-data-table td{padding:9px 7px;border-bottom:1px solid #eef2f8;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.ref-data-table tr:last-child td{border-bottom:0;}
.ref-data-table tbody tr:hover{background:rgba(42,103,210,.035);}
.ref-data-table th:first-child,.ref-data-table td:first-child{text-align:center;}
/* Reduce vertical bulk without globally shrinking typography. */
.ref-action-card{padding:4px 0 5px!important;}
.ref-action-amount{font-size:1.55rem!important;margin:0!important;}
.ref-action-amount-label{margin-top:4px!important;}
.ref-action-quotes{margin-top:3px!important;}
.ref-action-meta{font-size:.68rem!important;margin:3px 0 0!important;}
.ref-panel-head{min-height:22px!important;margin-bottom:3px!important;}
.ref-account-item{padding:6px 7px!important;}
.ref-account-value{margin:5px 0 3px!important;}


/* V9 clean chart surfaces, aligned main panels, essential-only order card */
[data-testid="stPlotlyChart"]{background:transparent!important;}
[data-testid="stPlotlyChart"]>div{background:transparent!important;}
.ref-chart-subtitle{margin:5px 0 2px!important;font-size:.82rem!important;}
.ref-action-card{display:none!important;} /* superseded by essential-only card */
.action-essential{background:transparent;border:none;padding:5px 1px 2px;}
.action-essential-state{display:flex;align-items:center;gap:9px;min-height:34px;color:#203c60;}
.action-essential-state strong{font-size:1.35rem;letter-spacing:-.02em;line-height:1.2;}
.action-essential-state span{font-size:.83rem;color:#7787a0;font-weight:650;}
.action-essential.is-buy .action-essential-state strong{color:#159b68;}
.action-essential.is-wait .action-essential-state strong{color:#667891;}
.action-essential.is-check .action-essential-state strong{color:#c07526;}
.action-essential-numbers{display:grid;grid-template-columns:1fr 1.25fr;gap:20px;align-items:end;margin-top:12px;}
.action-essential-item{min-width:0;}
.action-essential-item small{display:block;color:#7888a2;font-size:.77rem;font-weight:650;margin-bottom:5px;}
.action-essential-item strong{font-size:clamp(1.17rem,1.55vw,1.75rem);line-height:1.2;letter-spacing:-.04em;color:#172f57;font-weight:850;white-space:nowrap;}
div.st-key-order_table_tabs [data-baseweb="tab-list"]{background:transparent!important;border:0!important;padding:0!important;box-shadow:none!important;}
div.st-key-order_table_tabs [data-baseweb="tab"]{background:transparent!important;color:#6d7f99!important;
 font-weight:500!important;box-shadow:none!important;border:0!important;border-radius:0!important;
 min-height:34px!important;padding:6px 13px!important;}
div.st-key-order_table_tabs [data-baseweb="tab"][aria-selected="true"]{font-weight:850!important;color:#1a355e!important;
 background:transparent!important;box-shadow:none!important;border:0!important;}
div.st-key-order_table_tabs [data-baseweb="tab-highlight"],
div.st-key-order_table_tabs [data-baseweb="tab-border"]{background:transparent!important;border-color:transparent!important;}
/* Let the cards use natural heights without any inner fixed-height scroll surface. */

/* V10: 150% text rhythm, roomier risk/performance content, action summary cards */
.ref-panel-head,.ref-panel-head span,.ref-panel-head small,
.ref-kpi-label,.ref-kpi-value,.ref-kpi-foot,.ref-chart-subtitle,
.ref-account-name,.ref-account-value,.ref-account-sub,
.ref-perf-grid small,.ref-perf-grid strong,.ref-bar-row,.ref-bar-row b,
.ref-data-table th,.ref-data-table td,.action-essential,
.action-essential-state,.action-essential-state span,
.action-essential-item small,.action-essential-item strong,
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stCaptionContainer"],
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stMarkdownContainer"] p{
 line-height:1.5!important;
}
.ref-chart-subtitle{margin:8px 0 4px!important;}
.ref-perf-grid{margin:10px 0 22px!important;row-gap:16px!important;}
.ref-perf-grid > div{display:flex;flex-direction:column;gap:6px!important;}
.ref-bar-list{gap:17px!important;margin:9px 0 12px!important;}
.ref-bar-row{grid-template-columns:76px minmax(0,1fr) 65px!important;gap:10px!important;
 min-height:22px!important;font-size:.74rem!important;}
.ref-bar-row b{font-size:.71rem!important;}
.ref-bar-track{height:10px!important;}
.action-essential{padding:9px 0 11px!important;}
.action-essential-state{min-height:39px!important;color:#ae2346!important;}
.action-essential-state strong{font-size:1.55rem!important;color:#c72d4d!important;}
.action-essential.is-buy .action-essential-state strong,
.action-essential.is-wait .action-essential-state strong,
.action-essential.is-check .action-essential-state strong{color:#c72d4d!important;}
.action-essential-state span{color:#89415b!important;font-size:.91rem!important;}
.action-essential-numbers{grid-template-columns:1fr 1.2fr!important;gap:10px!important;margin-top:12px!important;}
.action-essential-item{border:1px solid #f1cbd3!important;background:#fff7f8!important;
 border-radius:12px!important;padding:11px 12px!important;min-height:89px!important;
 display:flex;flex-direction:column;justify-content:center;}
.action-essential-item small{color:#a35a70!important;font-size:.80rem!important;margin-bottom:4px!important;}
.action-essential-item strong{font-size:clamp(1.18rem,1.6vw,1.65rem)!important;color:#99273d!important;white-space:normal!important;overflow-wrap:anywhere;}
@media(max-width:1200px){
 .action-essential-numbers{grid-template-columns:1fr 1fr!important;}
 .action-essential-item{padding:8px!important;min-height:80px!important;}
 .action-essential-item strong{font-size:1.13rem!important;}
}

/* V11 visual spacing consistency: 1.5x title-to-content gaps */
.ref-panel-head{font-size:1.03rem!important;line-height:1.5!important;margin-bottom:12px!important;min-height:25px!important;}
.ref-chart-subtitle{font-size:1.03rem!important;line-height:1.5!important;font-weight:800!important;color:#162b51!important;margin:16px 0 12px!important;}
/* In Streamlit the markdown title's parent also has spacing, keep adjacent elements even. */
.ref-panel-head + .ref-table-wrap{margin-top:4px!important;}
.ref-panel-head + .ref-account-grid{margin-top:4px!important;}
/* Order-tab selection: selected text bold, zero painted background or shadow. */
.st-key-order_table_tabs [data-baseweb="tab-list"],
.st-key-order_table_tabs [data-baseweb="tab"],
.st-key-order_table_tabs [data-baseweb="tab"][aria-selected="true"],
.st-key-order_table_tabs [data-baseweb="tab"]:hover,
.st-key-order_table_tabs [data-baseweb="tab"] [data-testid="stMarkdownContainer"],
.st-key-order_table_tabs button[role="tab"],
.st-key-order_table_tabs button[role="tab"][aria-selected="true"]{
 background:transparent!important;background-color:transparent!important;background-image:none!important;
 box-shadow:none!important;border-radius:0!important;
}
.st-key-order_table_tabs [role="tab"]{font-weight:450!important;color:#687890!important;}
.st-key-order_table_tabs [role="tab"][aria-selected="true"]{font-weight:850!important;color:#172d51!important;}
.st-key-order_table_tabs [role="tab"] p,
.st-key-order_table_tabs [role="tab"] span{font-weight:inherit!important;color:inherit!important;}
.st-key-order_table_tabs [data-baseweb="tab-highlight"],
.st-key-order_table_tabs [data-baseweb="tab-border"]{background:transparent!important;border-color:transparent!important;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# TITLE
# ============================================================

st.markdown("""<div class="redesign-header"><div class="redesign-logo"><svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="#2563eb" stroke-width="2.5" stroke-linecap="round"><path d="M4 20V14M9 20V10M14 20V6M19 20V3"/></svg></div><div><div class="redesign-headline">SOXL 퀀트투자 대시보드</div><div class="redesign-subline">실전 운용 · 전략 분석 · 보유 슬롯 관리</div></div></div>""", unsafe_allow_html=True)
# The controls are created later (once the saved settings have loaded), but displayed here.
with st.container(border=True):
    _control_strategy_col, _control_start_col, _control_end_col, _control_refresh_col = st.columns([2.7, 1.5, 1.5, 1.25], gap="small")
    with _control_strategy_col:
        _redesign_strategy_placeholder = st.empty()
    with _control_start_col:
        _redesign_start_placeholder = st.empty()
    with _control_end_col:
        _redesign_end_placeholder = st.empty()
    with _control_refresh_col:
        _redesign_refresh_placeholder = st.empty()



# ============================================================
# DATA
# ============================================================

@st.cache_data
def get_data():

    return load_data(
        "SOXL_adjusted.csv"
    )


def refresh_data_cache():

    st.cache_data.clear()


@st.cache_data(ttl=60, show_spinner=False)
def get_soxl_realtime_snapshot(fallback_close):
    """
    Yahoo Finance의 최근 장중 가격을 조회합니다.

    실시간 조회에 실패하면 마지막 확정 일봉 종가를 반환합니다.
    이 가격은 화면의 평가손익 표시용으로만 사용하며,
    전략 신호나 다음 매수금액 계산에는 사용하지 않습니다.
    """

    fallback = float(fallback_close)
    snapshot = {
        "price": fallback,
        "previous_close": None,
        "change_pct": None,
        "updated_at": None,
        "source": "확정 일봉 종가",
        "is_live": False,
        "error": None,
    }

    if yf is None:
        snapshot["error"] = "yfinance가 설치되어 있지 않습니다."
        return snapshot

    try:
        ticker = yf.Ticker("SOXL")

        intraday = ticker.history(
            period="1d",
            interval="1m",
            auto_adjust=False,
            prepost=False,
        )

        if intraday.empty or "Close" not in intraday.columns:
            raise ValueError("장중 시세를 받지 못했습니다.")

        close_series = intraday["Close"].dropna()
        if close_series.empty:
            raise ValueError("유효한 장중 가격이 없습니다.")

        current_price = float(close_series.iloc[-1])
        last_ts = pd.Timestamp(close_series.index[-1])
        if last_ts.tzinfo is not None:
            last_ts = last_ts.tz_convert("America/New_York")

        previous_close = None
        daily = ticker.history(
            period="5d",
            interval="1d",
            auto_adjust=False,
            prepost=False,
        )

        if not daily.empty and "Close" in daily.columns:
            daily_close = daily["Close"].dropna()
            if len(daily_close) >= 1:
                ny_today = datetime.now(ZoneInfo("America/New_York")).date()
                last_daily_date = pd.Timestamp(daily_close.index[-1]).date()

                if last_daily_date == ny_today and len(daily_close) >= 2:
                    previous_close = float(daily_close.iloc[-2])
                else:
                    previous_close = float(daily_close.iloc[-1])

        change_pct = (
            current_price / previous_close - 1
            if previous_close is not None and previous_close > 0
            else None
        )

        snapshot.update({
            "price": current_price,
            "previous_close": previous_close,
            "change_pct": change_pct,
            "updated_at": last_ts,
            "source": "Yahoo Finance 1분 시세",
            "is_live": True,
        })

    except Exception as e:
        snapshot["error"] = str(e)

    return snapshot


@st.cache_data(ttl=300, show_spinner=False)
def get_usdkrw_realtime_snapshot():
    """USD/KRW 참고환율. Yahoo Chart API 우선, yfinance 보조."""
    result = {
        "rate": None,
        "source": "조회 실패",
        "is_live": False,
        "error": None,
    }

    # 1) Yahoo Finance Chart API: Streamlit Cloud에서 yfinance가 막히는 경우를 대비한 1순위
    try:
        url = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X"
        params = {
            "range": "5d",
            "interval": "5m",
            "includePrePost": "true",
            "events": "div,splits",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; SOXL-V9-Dashboard/1.0)",
            "Accept": "application/json,text/plain,*/*",
        }
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        payload = r.json()
        chart_result = (payload.get("chart") or {}).get("result") or []
        if chart_result:
            closes = (((chart_result[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
            valid = [float(x) for x in closes if x is not None and pd.notna(x) and float(x) > 0]
            if valid:
                result.update({
                    "rate": valid[-1],
                    "source": "Yahoo Finance Chart API USD/KRW",
                    "is_live": True,
                    "error": None,
                })
                return result
    except Exception as e:
        result["error"] = f"Yahoo API: {e}"

    # 2) yfinance 보조 경로
    if yf is not None:
        try:
            ticker = yf.Ticker("KRW=X")
            intraday = ticker.history(period="1d", interval="5m", auto_adjust=False, prepost=True)
            if not intraday.empty and "Close" in intraday.columns:
                values = pd.to_numeric(intraday["Close"], errors="coerce").dropna()
                if not values.empty:
                    result.update({
                        "rate": float(values.iloc[-1]),
                        "source": "yfinance USD/KRW",
                        "is_live": True,
                        "error": None,
                    })
                    return result

            daily = ticker.history(period="5d", interval="1d", auto_adjust=False)
            if not daily.empty and "Close" in daily.columns:
                values = pd.to_numeric(daily["Close"], errors="coerce").dropna()
                if not values.empty:
                    result.update({
                        "rate": float(values.iloc[-1]),
                        "source": "yfinance USD/KRW 최근 종가",
                        "is_live": False,
                        "error": None,
                    })
                    return result
        except Exception as e:
            prev = result.get("error")
            result["error"] = f"{prev or ''} / yfinance: {e}".strip(" / ")

    return result


@st.cache_data(ttl=3600, show_spinner=False)
def get_usdkrw_daily_rates(start_date_value, end_date_value):
    """보유 슬롯 매수일별 USD/KRW. Yahoo Chart API 우선, yfinance 보조."""
    start_ts = pd.Timestamp(start_date_value).normalize() - pd.Timedelta(days=10)
    end_ts = pd.Timestamp(end_date_value).normalize() + pd.Timedelta(days=3)

    # 1) Yahoo Chart API
    try:
        period1 = int(start_ts.tz_localize("UTC").timestamp())
        period2 = int((end_ts + pd.Timedelta(days=1)).tz_localize("UTC").timestamp())
        url = "https://query1.finance.yahoo.com/v8/finance/chart/KRW=X"
        params = {
            "period1": period1,
            "period2": period2,
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        }
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; SOXL-V9-Dashboard/1.0)",
            "Accept": "application/json,text/plain,*/*",
        }
        r = requests.get(url, params=params, headers=headers, timeout=10)
        r.raise_for_status()
        payload = r.json()
        chart_result = (payload.get("chart") or {}).get("result") or []
        if chart_result:
            item = chart_result[0]
            timestamps = item.get("timestamp") or []
            closes = (((item.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [])
            rows = []
            for ts, close in zip(timestamps, closes):
                if close is None or not pd.notna(close) or float(close) <= 0:
                    continue
                # Yahoo timestamps are UTC; only date matching is needed here.
                rows.append({
                    "Date": pd.to_datetime(int(ts), unit="s", utc=True).tz_convert("America/New_York").tz_localize(None).normalize(),
                    "USD_KRW": float(close),
                })
            if rows:
                return (
                    pd.DataFrame(rows)
                    .drop_duplicates(subset=["Date"], keep="last")
                    .sort_values("Date")
                    .reset_index(drop=True)
                )
    except Exception:
        pass

    # 2) yfinance 보조 경로
    if yf is not None:
        try:
            fx = yf.download(
                "KRW=X",
                start=start_ts.date().isoformat(),
                end=(end_ts + pd.Timedelta(days=1)).date().isoformat(),
                auto_adjust=False,
                progress=False,
                threads=False,
            )
            if not fx.empty:
                if isinstance(fx.columns, pd.MultiIndex):
                    fx.columns = fx.columns.get_level_values(0)
                if "Close" in fx.columns:
                    fx = fx.reset_index()[["Date", "Close"]].rename(columns={"Close": "USD_KRW"})
                    fx["Date"] = pd.to_datetime(fx["Date"]).dt.tz_localize(None).dt.normalize()
                    fx["USD_KRW"] = pd.to_numeric(fx["USD_KRW"], errors="coerce")
                    fx = fx.dropna(subset=["USD_KRW"])
                    fx = fx[fx["USD_KRW"] > 0]
                    if not fx.empty:
                        return fx.sort_values("Date").reset_index(drop=True)
        except Exception:
            pass

    return pd.DataFrame(columns=["Date", "USD_KRW"])


def lookup_entry_fx(entry_date, fx_daily, fallback_rate=None):
    """매수일 또는 그 직전 이용 가능한 환율을 반환합니다."""
    if fx_daily is not None and not fx_daily.empty:
        d = pd.Timestamp(entry_date).normalize()
        eligible = fx_daily[fx_daily["Date"] <= d]
        if not eligible.empty:
            value = float(eligible.iloc[-1]["USD_KRW"])
            if value > 0:
                return value
    if fallback_rate is not None and pd.notna(fallback_rate) and float(fallback_rate) > 0:
        return float(fallback_rate)
    return None


# ============================================================
# DASHBOARD SETTINGS PERSISTENCE
# ============================================================

SETTINGS_FILE = Path(__file__).with_name("dashboard_settings.json")


def load_dashboard_settings():
    """마지막으로 저장한 대시보드 설정을 불러옵니다."""

    defaults = {
        "start_date": None,
        "initial_capital": 100_000_000,
    }

    if not SETTINGS_FILE.exists():
        return defaults

    try:
        with SETTINGS_FILE.open("r", encoding="utf-8") as f:
            saved = json.load(f)

        if isinstance(saved, dict):
            defaults.update(saved)

    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        # 설정 파일이 손상되어도 대시보드는 기본값으로 계속 실행합니다.
        pass

    return defaults


def save_dashboard_settings(start_date_value, initial_capital_value):
    """시작일과 초기 투자금을 로컬 JSON 파일에 저장합니다."""

    payload = {
        "start_date": pd.Timestamp(start_date_value).date().isoformat(),
        "initial_capital": int(initial_capital_value),
    }

    try:
        with SETTINGS_FILE.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except OSError as e:
        st.sidebar.warning(f"설정값 저장 실패: {e}")




# ============================================================
# ENCRYPTED LIVE PORTFOLIO PERSISTENCE (GitHub + Streamlit Secrets)
# ============================================================

LIVE_PORTFOLIO_PATH = "live_portfolio.enc"
DEFAULT_GITHUB_REPO = "bgkoo1/soxl-v9-dashboard"
DEFAULT_GITHUB_BRANCH = "main"


def _portfolio_persistence_config():
    """Streamlit Secrets에서 GitHub 저장/암호화 설정을 읽습니다."""
    try:
        token = str(st.secrets.get("GITHUB_TOKEN", "")).strip()
        encryption_key = str(st.secrets.get("PORTFOLIO_ENCRYPTION_KEY", "")).strip()
        repo = str(st.secrets.get("GITHUB_REPO", DEFAULT_GITHUB_REPO)).strip()
        branch = str(st.secrets.get("GITHUB_BRANCH", DEFAULT_GITHUB_BRANCH)).strip()
    except Exception:
        return None

    if not token or not encryption_key or not repo:
        return None

    try:
        Fernet(encryption_key.encode("utf-8"))
    except Exception:
        return None

    return {
        "token": token,
        "key": encryption_key,
        "repo": repo,
        "branch": branch or DEFAULT_GITHUB_BRANCH,
    }


def portfolio_persistence_enabled():
    return _portfolio_persistence_config() is not None


def _github_contents_url(config):
    return f"https://api.github.com/repos/{config['repo']}/contents/{LIVE_PORTFOLIO_PATH}"


def _github_headers(config):
    return {
        "Authorization": f"Bearer {config['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _sanitize_v9_cashflows(items):
    """암호화 저장소의 입출금 내역을 안전한 형식으로 정리합니다."""
    cleaned = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            flow_date = pd.Timestamp(item.get("date")).date().isoformat()
            flow_type = str(item.get("type", "입금")).strip()
            if flow_type not in {"입금", "출금"}:
                continue
            amount = float(item.get("amount", 0))
            if amount <= 0:
                continue
            flow_id = str(item.get("id") or f"{flow_date}-{len(cleaned)+1}")
            memo = str(item.get("memo", "")).strip()[:100]
        except Exception:
            continue
        cleaned.append({
            "id": flow_id,
            "date": flow_date,
            "type": flow_type,
            "amount": amount,
            "memo": memo,
        })
    cleaned.sort(key=lambda x: (x["date"], x["id"]))
    return cleaned



def _sanitize_v9_trade_overrides(items):
    """실제 체결 거래 오버라이드를 안전한 형식으로 정리합니다."""
    cleaned = {}
    if not isinstance(items, dict):
        return cleaned

    allowed_strategies = {
        "일반 LOC(+2.7%)",
        "방어 LOC(+$0.10)",
        "TIME/MOC",
        "수동 매도",
        "기타",
    }

    for key, item in items.items():
        if not isinstance(item, dict):
            continue
        try:
            entry_date = pd.Timestamp(item.get("entry_date")).date().isoformat()
            exit_date = pd.Timestamp(item.get("exit_date")).date().isoformat()
            entry_price = float(item.get("entry_price"))
            exit_price = float(item.get("exit_price"))
            quantity = max(1, int(round(float(item.get("quantity")))))
            exit_strategy = str(item.get("exit_strategy", "기타")).strip()
        except Exception:
            continue

        if entry_price <= 0 or exit_price <= 0:
            continue
        if exit_strategy not in allowed_strategies:
            exit_strategy = "기타"

        cleaned[str(key)] = {
            "entry_date": entry_date,
            "exit_date": exit_date,
            "entry_price": round(entry_price, 6),
            "exit_price": round(exit_price, 6),
            "quantity": quantity,
            "exit_strategy": exit_strategy,
        }

    return cleaned

def _sanitize_dashboard_settings(settings):
    """암호화 저장소의 운용 시작일/초기 투자금을 안전한 형식으로 정리합니다."""
    defaults = {"start_date": None, "initial_capital": 100_000_000}
    if not isinstance(settings, dict):
        return defaults

    start_raw = settings.get("start_date")
    if start_raw:
        try:
            defaults["start_date"] = pd.Timestamp(start_raw).date().isoformat()
        except Exception:
            pass

    try:
        capital = int(round(float(settings.get("initial_capital", 100_000_000))))
        defaults["initial_capital"] = max(1_000_000, min(10_000_000_000, capital))
    except Exception:
        pass
    return defaults


def _sanitize_v9_manual_trades(items):
    """전략에 없는 수동 거래를 안전한 형식으로 정리합니다. 현재는 청산 완료 거래만 저장합니다."""
    cleaned = []
    allowed_strategies = {
        "일반 LOC(+2.7%)",
        "방어 LOC(+$0.10)",
        "TIME/MOC",
        "수동 매도",
        "기타",
    }
    seen = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        try:
            trade_id = str(item.get("id") or uuid.uuid4().hex).strip()
            if not trade_id or trade_id in seen:
                trade_id = uuid.uuid4().hex
            entry_date = pd.Timestamp(item.get("entry_date")).date().isoformat()
            exit_date = pd.Timestamp(item.get("exit_date")).date().isoformat()
            entry_price = float(item.get("entry_price"))
            exit_price = float(item.get("exit_price"))
            quantity = max(1, int(round(float(item.get("quantity")))))
            exit_strategy = str(item.get("exit_strategy", "기타")).strip()
            note = str(item.get("note", "")).strip()[:120]
            affect_nav = bool(item.get("affect_nav", True))
        except Exception:
            continue
        if entry_price <= 0 or exit_price <= 0:
            continue
        if pd.Timestamp(exit_date) < pd.Timestamp(entry_date):
            continue
        if exit_strategy not in allowed_strategies:
            exit_strategy = "기타"
        seen.add(trade_id)
        cleaned.append({
            "id": trade_id,
            "entry_date": entry_date,
            "exit_date": exit_date,
            "entry_price": round(entry_price, 6),
            "exit_price": round(exit_price, 6),
            "quantity": quantity,
            "exit_strategy": exit_strategy,
            "note": note,
            "affect_nav": affect_nav,
        })
    cleaned.sort(key=lambda x: (x["exit_date"], x["entry_date"], x["id"]))
    return cleaned


def _sanitize_v9_excluded_positions(items):
    """실제로 매수하지 않은 전략 슬롯 키를 안전한 목록으로 정리합니다."""
    if not isinstance(items, (list, tuple, set)):
        return []
    cleaned = []
    seen = set()
    for item in items:
        key = str(item).strip()
        if key and key not in seen:
            cleaned.append(key)
            seen.add(key)
    return cleaned


def _sanitize_v9_entry_prices(items):
    """현재 보유 슬롯의 실제 매수체결가 오버라이드를 안전한 형식으로 정리합니다."""
    cleaned = {}
    if not isinstance(items, dict):
        return cleaned
    for k, v in items.items():
        try:
            price = float(v)
            if price > 0:
                cleaned[str(k)] = round(price, 6)
        except (TypeError, ValueError):
            continue
    return cleaned


def _load_persisted_v9_state():
    """GitHub 암호화 파일에서 설정 + 실제수량 + 입출금 + 실제 거래값을 함께 복원합니다."""
    config = _portfolio_persistence_config()
    empty = {"settings": _sanitize_dashboard_settings({}), "quantities": {}, "entry_prices": {}, "excluded_positions": [], "cashflows": [], "trade_overrides": {}, "manual_trades": []}
    if config is None:
        return empty, "Secrets 미설정"

    try:
        response = requests.get(
            _github_contents_url(config),
            headers=_github_headers(config),
            params={"ref": config["branch"]},
            timeout=10,
        )
        if response.status_code == 404:
            return empty, "저장 데이터 없음"
        response.raise_for_status()
        item = response.json()
        encrypted = base64.b64decode(item["content"])
        decrypted = Fernet(config["key"].encode("utf-8")).decrypt(encrypted)
        payload = json.loads(decrypted.decode("utf-8"))
        if not isinstance(payload, dict):
            payload = {}

        quantities = payload.get("quantities", {})
        if not isinstance(quantities, dict):
            quantities = {}
        cleaned_qty = {}
        for k, v in quantities.items():
            try:
                cleaned_qty[str(k)] = max(0, int(round(float(v))))
            except (TypeError, ValueError):
                continue

        entry_prices = _sanitize_v9_entry_prices(payload.get("entry_prices", {}))
        excluded_positions = _sanitize_v9_excluded_positions(payload.get("excluded_positions", []))
        settings = _sanitize_dashboard_settings(payload.get("settings", {}))
        cashflows = _sanitize_v9_cashflows(payload.get("cashflows", []))
        trade_overrides = _sanitize_v9_trade_overrides(payload.get("trade_overrides", {}))
        manual_trades = _sanitize_v9_manual_trades(payload.get("manual_trades", []))
        return {
            "settings": settings,
            "quantities": cleaned_qty,
            "entry_prices": entry_prices,
            "excluded_positions": excluded_positions,
            "cashflows": cashflows,
            "trade_overrides": trade_overrides,
            "manual_trades": manual_trades,
        }, "GitHub 암호화 저장값 불러옴"
    except InvalidToken:
        return empty, "암호화 키가 저장 데이터와 일치하지 않음"
    except Exception as e:
        return empty, f"영구 저장 불러오기 실패: {e}"


def _load_persisted_portfolio_revision(commit_sha):
    """Read-only recovery from an encrypted historical GitHub blob. Never publish secrets."""
    config = _portfolio_persistence_config()
    if config is None:
        raise ValueError("GitHub 암호화 저장 연결이 필요합니다.")
    url = _github_contents_url(config)
    response = requests.get(url, headers=_github_headers(config),
                            params={"ref": commit_sha}, timeout=12)
    response.raise_for_status()
    item = response.json()
    decrypted = Fernet(config["key"].encode("utf-8")).decrypt(
        base64.b64decode(item["content"]))
    payload = json.loads(decrypted.decode("utf-8"))
    return {
        "quantities": dict(payload.get("quantities", {})),
        "entry_prices": _sanitize_v9_entry_prices(payload.get("entry_prices", {})),
        "excluded_positions": _sanitize_v9_excluded_positions(payload.get("excluded_positions", [])),
    }


def _save_persisted_v9_state(quantity_map=None, entry_prices=None, cashflows=None, trade_overrides=None, settings=None, manual_trades=None, excluded_positions=None):
    """실제수량·입출금·실제 체결값을 하나의 암호화 파일로 저장합니다."""
    config = _portfolio_persistence_config()
    if config is None:
        return False, "Streamlit Secrets에 GitHub/암호화 설정이 없습니다."

    cleaned_qty = {}
    for k, v in (quantity_map or {}).items():
        try:
            cleaned_qty[str(k)] = max(0, int(round(float(v))))
        except (TypeError, ValueError):
            continue
    cleaned_entry_prices = _sanitize_v9_entry_prices(entry_prices)
    cleaned_excluded_positions = _sanitize_v9_excluded_positions(
        st.session_state.get("v9_excluded_positions", []) if excluded_positions is None else excluded_positions
    )
    cleaned_cashflows = _sanitize_v9_cashflows(cashflows)
    cleaned_trade_overrides = _sanitize_v9_trade_overrides(trade_overrides)
    cleaned_settings = _sanitize_dashboard_settings(settings or {})
    cleaned_manual_trades = _sanitize_v9_manual_trades(manual_trades)

    payload = {
        "version": 6,
        "updated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "settings": cleaned_settings,
        "quantities": cleaned_qty,
        "entry_prices": cleaned_entry_prices,
        "excluded_positions": cleaned_excluded_positions,
        "cashflows": cleaned_cashflows,
        "trade_overrides": cleaned_trade_overrides,
        "manual_trades": cleaned_manual_trades,
    }
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    encrypted = Fernet(config["key"].encode("utf-8")).encrypt(raw)
    encoded = base64.b64encode(encrypted).decode("ascii")

    try:
        sha = None
        current = requests.get(
            _github_contents_url(config),
            headers=_github_headers(config),
            params={"ref": config["branch"]},
            timeout=10,
        )
        if current.status_code == 200:
            sha = current.json().get("sha")
        elif current.status_code != 404:
            current.raise_for_status()

        body = {
            "message": "Update encrypted live portfolio state",
            "content": encoded,
            "branch": config["branch"],
        }
        if sha:
            body["sha"] = sha

        response = requests.put(
            _github_contents_url(config),
            headers=_github_headers(config),
            json=body,
            timeout=15,
        )
        # 동시 저장 충돌 시 최신 SHA를 다시 읽고 1회 재시도합니다.
        if response.status_code == 409:
            current = requests.get(
                _github_contents_url(config),
                headers=_github_headers(config),
                params={"ref": config["branch"]},
                timeout=10,
            )
            current.raise_for_status()
            body["sha"] = current.json().get("sha")
            response = requests.put(
                _github_contents_url(config),
                headers=_github_headers(config),
                json=body,
                timeout=15,
            )
        response.raise_for_status()
        return True, "운용설정·실제 보유수량·미매수 슬롯·입출금·실제 거래값을 암호화하여 영구 저장했습니다."
    except Exception as e:
        return False, f"영구 저장 실패: {e}"


def load_persisted_v9_quantities():
    """기존 호출부 호환용: 저장 상태에서 실제 보유수량만 반환합니다."""
    state, status = _load_persisted_v9_state()
    st.session_state["v9_settings"] = state.get("settings", {})
    st.session_state["v9_entry_price_overrides"] = state.get("entry_prices", {})
    st.session_state["v9_excluded_positions"] = state.get("excluded_positions", [])
    st.session_state["v9_cashflows"] = state.get("cashflows", [])
    st.session_state["v9_trade_overrides"] = state.get("trade_overrides", {})
    st.session_state["v9_manual_trades"] = state.get("manual_trades", [])
    return state.get("quantities", {}), status


def load_persisted_v9_cashflows():
    """입출금 장부를 불러오며 수량도 같은 세션에 동기화합니다."""
    state, status = _load_persisted_v9_state()
    st.session_state["v9_settings"] = state.get("settings", {})
    st.session_state["v9_quantity_overrides"] = state.get("quantities", {})
    st.session_state["v9_entry_price_overrides"] = state.get("entry_prices", {})
    st.session_state["v9_excluded_positions"] = state.get("excluded_positions", [])
    st.session_state["v9_cashflows"] = state.get("cashflows", [])
    st.session_state["v9_trade_overrides"] = state.get("trade_overrides", {})
    st.session_state["v9_manual_trades"] = state.get("manual_trades", [])
    st.session_state["v9_quantity_persistence_status"] = status
    return state.get("cashflows", []), status


def save_persisted_v9_quantities(quantity_map):
    """실제 보유수량 저장 시 기존 입출금 장부를 보존합니다."""
    return _save_persisted_v9_state(
        quantity_map=quantity_map,
        entry_prices=st.session_state.get("v9_entry_price_overrides", {}),
        cashflows=st.session_state.get("v9_cashflows", []),
        trade_overrides=st.session_state.get("v9_trade_overrides", {}),
        settings=st.session_state.get("v9_settings", {}),
        manual_trades=st.session_state.get("v9_manual_trades", []),
    )


def save_persisted_v9_cashflows(cashflows):
    """입출금 장부 저장 시 기존 실제 보유수량을 보존합니다."""
    return _save_persisted_v9_state(
        quantity_map=st.session_state.get("v9_quantity_overrides", {}),
        entry_prices=st.session_state.get("v9_entry_price_overrides", {}),
        cashflows=cashflows,
        trade_overrides=st.session_state.get("v9_trade_overrides", {}),
        settings=st.session_state.get("v9_settings", {}),
        manual_trades=st.session_state.get("v9_manual_trades", []),
    )



def load_persisted_v9_trade_overrides():
    """실제 체결 거래 오버라이드를 불러옵니다."""
    state, status = _load_persisted_v9_state()
    st.session_state["v9_settings"] = state.get("settings", {})
    st.session_state["v9_quantity_overrides"] = state.get("quantities", {})
    st.session_state["v9_entry_price_overrides"] = state.get("entry_prices", {})
    st.session_state["v9_excluded_positions"] = state.get("excluded_positions", [])
    st.session_state["v9_cashflows"] = state.get("cashflows", [])
    st.session_state["v9_trade_overrides"] = state.get("trade_overrides", {})
    st.session_state["v9_manual_trades"] = state.get("manual_trades", [])
    return state.get("trade_overrides", {}), status


def save_persisted_v9_trade_overrides(trade_overrides, quantity_map=None):
    """실제 체결 거래 저장 시 수량/입출금 장부를 함께 보존합니다."""
    return _save_persisted_v9_state(
        entry_prices=st.session_state.get("v9_entry_price_overrides", {}),
        quantity_map=(
            quantity_map
            if quantity_map is not None
            else st.session_state.get("v9_quantity_overrides", {})
        ),
        cashflows=st.session_state.get("v9_cashflows", []),
        trade_overrides=trade_overrides,
        settings=st.session_state.get("v9_settings", {}),
        manual_trades=st.session_state.get("v9_manual_trades", []),
    )

def save_persisted_dashboard_settings(settings):
    """운용 시작일/초기 투자금을 기존 암호화 상태와 함께 저장합니다."""
    cleaned = _sanitize_dashboard_settings(settings)
    st.session_state["v9_settings"] = cleaned
    return _save_persisted_v9_state(
        quantity_map=st.session_state.get("v9_quantity_overrides", {}),
        entry_prices=st.session_state.get("v9_entry_price_overrides", {}),
        cashflows=st.session_state.get("v9_cashflows", []),
        trade_overrides=st.session_state.get("v9_trade_overrides", {}),
        settings=cleaned,
        manual_trades=st.session_state.get("v9_manual_trades", []),
    )


def save_persisted_v9_manual_trades(manual_trades):
    """수동 거래를 기존 암호화 상태와 함께 저장합니다."""
    cleaned = _sanitize_v9_manual_trades(manual_trades)
    st.session_state["v9_manual_trades"] = cleaned
    return _save_persisted_v9_state(
        quantity_map=st.session_state.get("v9_quantity_overrides", {}),
        entry_prices=st.session_state.get("v9_entry_price_overrides", {}),
        cashflows=st.session_state.get("v9_cashflows", []),
        trade_overrides=st.session_state.get("v9_trade_overrides", {}),
        settings=st.session_state.get("v9_settings", {}),
        manual_trades=cleaned,
    )


def save_persisted_v9_open_position_overrides(quantity_map, entry_price_map, excluded_positions=None):
    """현재 슬롯의 실제 보유수량과 실제 매수체결가를 함께 영구 저장합니다."""
    st.session_state["v9_quantity_overrides"] = dict(quantity_map or {})
    st.session_state["v9_entry_price_overrides"] = _sanitize_v9_entry_prices(entry_price_map or {})
    if excluded_positions is not None:
        st.session_state["v9_excluded_positions"] = _sanitize_v9_excluded_positions(excluded_positions)
    return _save_persisted_v9_state(
        quantity_map=st.session_state["v9_quantity_overrides"],
        entry_prices=st.session_state["v9_entry_price_overrides"],
        cashflows=st.session_state.get("v9_cashflows", []),
        trade_overrides=st.session_state.get("v9_trade_overrides", {}),
        settings=st.session_state.get("v9_settings", {}),
        manual_trades=st.session_state.get("v9_manual_trades", []),
        excluded_positions=st.session_state.get("v9_excluded_positions", []),
    )


def summarize_v9_cashflows(cashflows, as_of_date=None):
    """지정일 현재 추가 입금/출금 및 순입금을 계산합니다."""
    cutoff = pd.Timestamp(as_of_date).date() if as_of_date is not None else None
    deposits = withdrawals = 0.0
    for item in _sanitize_v9_cashflows(cashflows):
        flow_date = pd.Timestamp(item["date"]).date()
        if cutoff is not None and flow_date > cutoff:
            continue
        amount = float(item["amount"])
        if item["type"] == "입금":
            deposits += amount
        else:
            withdrawals += amount
    return {
        "deposits": float(deposits),
        "withdrawals": float(withdrawals),
        "net": float(deposits - withdrawals),
    }


# ============================================================
# V8 / DISPLAY HELPERS
# ============================================================

def add_result_tables(result, initial_capital):
    """V8 실험 엔진 결과에 기존 대시보드용 하락폭/연도별 표를 추가합니다."""

    enriched = dict(result)
    equity = result["equity"].copy()
    equity["Date"] = pd.to_datetime(equity["Date"])

    running_max = equity["Equity"].cummax()
    drawdown = equity["Equity"] / running_max - 1

    enriched["drawdown"] = pd.DataFrame({
        "Date": equity["Date"],
        "Drawdown": drawdown,
    })

    yearly_rows = []
    previous_equity = float(initial_capital)

    for year, group in equity.groupby(equity["Date"].dt.year):
        year_end = float(group["Equity"].iloc[-1])
        year_return = year_end / previous_equity - 1

        year_running_max = group["Equity"].cummax()
        year_mdd = float((group["Equity"] / year_running_max - 1).min())

        yearly_rows.append({
            "Year": int(year),
            "Year_Return": year_return,
            "Year_MDD": year_mdd,
            "Year_End_Equity": year_end,
        })

        previous_equity = year_end

    enriched["yearly"] = pd.DataFrame(yearly_rows)
    return enriched


def build_signal_table(full_price_df):
    """오늘 주문용 확정 일봉 지표. 선택된 신호 기준일 자체의 값만 사용합니다."""

    signals = (
        full_price_df
        .copy()
        .sort_values("Date")
        .reset_index(drop=True)
    )
    signals["Date"] = pd.to_datetime(signals["Date"]).dt.normalize()

    for window in [5, 20, 50, 200]:
        signals[f"MA{window}"] = (
            signals["Close"]
            .rolling(window=window, min_periods=window)
            .mean()
        )

    signals["MA200_Gap"] = signals["Close"] / signals["MA200"] - 1
    signals["Momentum_20D"] = signals["Close"] / signals["Close"].shift(20) - 1

    signals["Risk_B"] = signals.apply(
        lambda row: (
            is_risk_b(row["MA200_Gap"], row["Momentum_20D"])
            if pd.notna(row["MA200_Gap"]) and pd.notna(row["Momentum_20D"])
            else False
        ),
        axis=1,
    )

    signals["TB3"] = (
        (signals["MA5"] < signals["MA20"])
        & (signals["MA20"] < signals["MA50"])
    ).fillna(False)

    return signals

def _nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    offset = (weekday - d.weekday()) % 7
    return d + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year, month, weekday):
    if month == 12:
        d = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        d = date(year, month + 1, 1) - timedelta(days=1)
    offset = (d.weekday() - weekday) % 7
    return d - timedelta(days=offset)


def _observed_fixed_holiday(d):
    if d.weekday() == 5:   # Saturday -> Friday
        return d - timedelta(days=1)
    if d.weekday() == 6:   # Sunday -> Monday
        return d + timedelta(days=1)
    return d


def _easter_sunday(year):
    """Gregorian Easter date (Anonymous Gregorian algorithm)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def nyse_standard_holidays(year):
    """NYSE의 정기 휴장일을 계산합니다. 임시/특별 휴장은 별도 예외일 수 있습니다."""
    holidays = set()

    # New Year's Day. 다음 해 1/1이 토요일이면 전년도 12/31 휴장도 포함합니다.
    holidays.add(_observed_fixed_holiday(date(year, 1, 1)))
    next_new_year_observed = _observed_fixed_holiday(date(year + 1, 1, 1))
    if next_new_year_observed.year == year:
        holidays.add(next_new_year_observed)

    # Martin Luther King Jr. Day (1998~)
    if year >= 1998:
        holidays.add(_nth_weekday(year, 1, 0, 3))

    # Washington's Birthday / Presidents Day
    holidays.add(_nth_weekday(year, 2, 0, 3))

    # Good Friday
    holidays.add(_easter_sunday(year) - timedelta(days=2))

    # Memorial Day
    holidays.add(_last_weekday(year, 5, 0))

    # Juneteenth (NYSE observed from 2022)
    if year >= 2022:
        holidays.add(_observed_fixed_holiday(date(year, 6, 19)))

    # Independence Day
    holidays.add(_observed_fixed_holiday(date(year, 7, 4)))

    # Labor Day
    holidays.add(_nth_weekday(year, 9, 0, 1))

    # Thanksgiving Day
    holidays.add(_nth_weekday(year, 11, 3, 4))

    # Christmas Day
    holidays.add(_observed_fixed_holiday(date(year, 12, 25)))

    return holidays


def is_nyse_trading_day(value):
    d = pd.Timestamp(value).date()
    return d.weekday() < 5 and d not in nyse_standard_holidays(d.year)


def previous_nyse_session(value):
    d = pd.Timestamp(value).date() - timedelta(days=1)
    while not is_nyse_trading_day(d):
        d -= timedelta(days=1)
    return pd.Timestamp(d).normalize()


def next_nyse_session(value, include_today=False):
    d = pd.Timestamp(value).date()
    if not include_today:
        d += timedelta(days=1)
    while not is_nyse_trading_day(d):
        d += timedelta(days=1)
    return pd.Timestamp(d).normalize()


def count_missing_sessions(latest_date, required_date):
    """latest_date 다음 거래일부터 required_date까지 빠진 거래일 수."""
    latest = pd.Timestamp(latest_date).normalize()
    required = pd.Timestamp(required_date).normalize()
    if latest >= required:
        return 0

    count = 0
    cursor = latest + pd.Timedelta(days=1)
    while cursor <= required:
        if is_nyse_trading_day(cursor):
            count += 1
        cursor += pd.Timedelta(days=1)
    return count


def get_moc_deadline(entry_date, holding_days=7):
    """NYSE 정기 거래일 기준 MOC(종가 매도) 예정일을 계산합니다."""

    entry = pd.Timestamp(entry_date).normalize()

    # 진입일 Day 0. 이후 7번째 거래일이 TIME/MOC 기한입니다.
    cursor = entry
    found = 0
    while found < holding_days:
        cursor += pd.Timedelta(days=1)
        if is_nyse_trading_day(cursor):
            found += 1

    return cursor.normalize(), False


def get_live_market_context(latest_data_date):
    """미국 동부시간 기준으로 지금 주문할 대상 세션과 신호 기준일을 계산합니다."""

    seoul_now = datetime.now(ZoneInfo("Asia/Seoul"))
    ny_now = datetime.now(ZoneInfo("America/New_York"))

    today_kst = pd.Timestamp(seoul_now.date()).normalize()
    us_today = pd.Timestamp(ny_now.date()).normalize()
    latest_data_date = pd.Timestamp(latest_data_date).normalize()

    us_is_trading_day = is_nyse_trading_day(us_today)
    us_time = ny_now.time().replace(tzinfo=None)

    # MOC 주문을 낼 수 있는 현재/다음 미국 세션을 정합니다.
    # 정규장 마감(16:00 ET) 이후면 다음 거래일을 주문 대상일로 봅니다.
    if us_is_trading_day and us_time < time(16, 0):
        action_session = us_today
        if us_time < time(9, 30):
            session_status = "PRE-MARKET(장 시작 전)"
        else:
            session_status = "OPEN(거래 중)"
    else:
        action_session = next_nyse_session(us_today, include_today=False)
        if us_is_trading_day:
            session_status = "CLOSED(오늘 장 마감)"
        else:
            session_status = "CLOSED(오늘 휴장)"

    required_signal_date = previous_nyse_session(action_session)
    missing_sessions = count_missing_sessions(latest_data_date, required_signal_date)

    return {
        "seoul_now": seoul_now,
        "ny_now": ny_now,
        "today_kst": today_kst,
        "us_today": us_today,
        "us_is_trading_day": us_is_trading_day,
        "session_status": session_status,
        "action_session": action_session,
        "required_signal_date": required_signal_date,
        "missing_sessions": missing_sessions,
        "calendar_exact": True,
    }



def run_live_v8_portfolio(full_price_df, start_date, initial_capital):
    """
    V8 실전 포트폴리오 상태를 계산합니다.

    - 시장지표는 전체 과거 데이터로 먼저 계산해 MA200/20일 수익률의 사전 이력을 보존합니다.
    - 자금 운용은 사용자가 선택한 start_date에서 initial_capital로 새로 시작합니다.
    - Risk B 판단은 add_market_signals()가 만든 전일 확정 Signal_* 값을 사용합니다.
    - 신규매수 목표액은 전 거래일 NAV / 7입니다.
    - 당일 매도대금은 당일 신규매수에 재사용하지 않습니다.
    """
    prepared = add_market_signals(full_price_df.copy())
    prepared["Date"] = pd.to_datetime(prepared["Date"])
    prepared = prepared.sort_values("Date").reset_index(drop=True)

    start_ts = pd.Timestamp(start_date).normalize()
    run_df = prepared[
        prepared["Date"].dt.normalize() >= start_ts
    ].copy().reset_index(drop=True)

    if run_df.empty:
        return {
            "trades": pd.DataFrame(),
            "equity": pd.DataFrame(),
            "cash": float(initial_capital),
            "open_positions": 0,
        }

    cash = float(initial_capital)
    positions = []
    closed_trades = []
    equity_records = []
    prev_nav = float(initial_capital)

    for i, row in run_df.iterrows():
        date_value = pd.Timestamp(row["Date"])
        close_price = float(row["Close"])

        # 당일 매도대금은 신규매수에 쓸 수 없으므로 장 시작 시점 현금을 따로 보존
        cash_available_for_buy = cash

        remaining = []
        for pos in positions:
            holding = i - pos["entry_index"]
            entry_price = pos["entry_price"]
            target_price = entry_price * (1 + BASE_TARGET_RETURN)
            exit_type = None

            # 진입 당일에는 목표수익 매도 불가
            if holding >= 1 and close_price >= target_price:
                exit_type = "TARGET"
            elif holding >= BASE_HOLDING_DAYS:
                exit_type = "TIME"

            if exit_type is None:
                remaining.append(pos)
                continue

            exit_price = close_price
            proceeds = pos["shares"] * exit_price
            cash += proceeds
            ret = exit_price / entry_price - 1
            closed_trades.append({
                "Entry_Date": pos["entry_date"],
                "Exit_Date": date_value,
                "Entry_Price": entry_price,
                "Exit_Price": exit_price,
                "Target_Price": target_price,
                "Shares": pos["shares"],
                "Invested": pos["invested"],
                "Holding_Days": holding,
                "Return": ret,
                "Profit": (exit_price - entry_price) * pos["shares"],
                "Exit_Type": exit_type,
            })

        positions = remaining

        ma_gap = row.get("Signal_MA200_Gap")
        mom20 = row.get("Signal_Momentum_20D")
        risk_b = (
            pd.notna(ma_gap)
            and pd.notna(mom20)
            and is_risk_b(ma_gap, mom20)
        )

        target_buy = prev_nav / BASE_POSITION_DIVISOR

        if (
            not risk_b
            and len(positions) < BASE_MAX_POSITIONS
            and target_buy > 0
            and cash_available_for_buy > 0
        ):
            investment = min(target_buy, cash_available_for_buy)
            shares = investment / close_price
            cash -= investment
            positions.append({
                "entry_date": date_value,
                "entry_price": close_price,
                "shares": shares,
                "invested": investment,
                "entry_index": i,
            })

        position_value = sum(p["shares"] * close_price for p in positions)
        nav = cash + position_value
        equity_records.append({
            "Date": date_value,
            "Cash": cash,
            "Position_Value": position_value,
            "Equity": nav,
            "Open_Positions": len(positions),
        })
        prev_nav = nav

    # 화면 표시용으로 현재 열린 포지션을 END 행으로만 추가합니다.
    # 실제 현금/자산 계산에서는 청산하지 않습니다.
    last_date = pd.Timestamp(run_df.iloc[-1]["Date"])
    last_close = float(run_df.iloc[-1]["Close"])
    trades = list(closed_trades)
    for pos in positions:
        entry_price = pos["entry_price"]
        holding = (len(run_df) - 1) - pos["entry_index"]
        trades.append({
            "Entry_Date": pos["entry_date"],
            "Exit_Date": last_date,
            "Entry_Price": entry_price,
            "Exit_Price": last_close,
            "Target_Price": entry_price * (1 + BASE_TARGET_RETURN),
            "Shares": pos["shares"],
            "Invested": pos["invested"],
            "Holding_Days": holding,
            "Return": last_close / entry_price - 1,
            "Profit": (last_close - entry_price) * pos["shares"],
            "Exit_Type": "END",
        })

    return {
        "trades": pd.DataFrame(trades),
        "equity": pd.DataFrame(equity_records),
        "cash": float(cash),
        "open_positions": len(positions),
    }


def reconstruct_live_cash_and_open_count(result, current_close):
    """run_backtest의 END 기록을 이용해 실제 미청산 현금과 슬롯 수를 복원합니다."""
    trades_df = result.get("trades", pd.DataFrame())
    if trades_df.empty or "Exit_Type" not in trades_df.columns:
        nav = float(result["equity"]["Equity"].iloc[-1])
        return nav, 0

    open_df = trades_df[trades_df["Exit_Type"] == "END"].copy()
    nav = float(result["equity"]["Equity"].iloc[-1])
    if open_df.empty:
        return nav, 0

    position_value = float((open_df["Shares"] * float(current_close)).sum())
    return max(0.0, nav - position_value), len(open_df)


def build_open_positions_table(trades_df, current_close, as_of_date, holding_days=7):
    """백테스트 종료 시 END로 표시된 거래를 현재 보유 중인 슬롯처럼 표시합니다."""

    if trades_df.empty or "Exit_Type" not in trades_df.columns:
        return pd.DataFrame()

    open_trades = trades_df[trades_df["Exit_Type"] == "END"].copy()

    if open_trades.empty:
        return pd.DataFrame()

    rows = []

    for slot_no, (_, trade) in enumerate(open_trades.iterrows(), start=1):
        deadline, estimated = get_moc_deadline(
            trade["Entry_Date"],
            holding_days=holding_days,
        )

        entry_date = pd.Timestamp(trade["Entry_Date"]).normalize()
        target_price = float(trade.get("Target_Price", trade["Entry_Price"] * 1.027))
        current_return = current_close / float(trade["Entry_Price"]) - 1

        if as_of_date > deadline:
            status = "MOC(종가 매도) 기한 확인 필요"
            remaining_text = "기한 지남"
        elif as_of_date == deadline:
            status = "MOC(종가 매도) 오늘 예정"
            remaining_text = "오늘"
        else:
            # 외부 캘린더 패키지 없이 자체 NYSE 거래일 함수로
            # as_of_date 다음 날부터 MOC 기한까지 남은 실제 거래일 수를 계산합니다.
            remaining = 0
            cursor = pd.Timestamp(as_of_date).normalize() + pd.Timedelta(days=1)
            while cursor <= deadline:
                if is_nyse_trading_day(cursor):
                    remaining += 1
                cursor += pd.Timedelta(days=1)

            remaining_text = f"{remaining}거래일"
            status = "TARGET(목표수익 매도) 대기"

        rows.append({
            "_key": key,
            "슬롯": slot_no,
            "매수유형": str(pos.get("buy_type", "기본 MOC")),
            "매수일": entry_date.date(),
            "매수가": f"${float(trade['Entry_Price']):,.2f}",
            "현재가": f"${current_close:,.2f}",
            "현재 수익률": f"{current_return:.2%}",
            "평가손익": f"{float(trade['Invested']) * current_return:+,.0f}원",
            "목표 매도가": f"${target_price:,.2f}",
            "투자금": f"{float(trade['Invested']):,.0f}원",
            "MOC(종가 매도) 예정일": f"{deadline.date()}{' (예상)' if estimated else ''}",
            "남은 거래일": remaining_text,
            "상태": status,
        })

    return pd.DataFrame(rows)



def v9_position_key(pos):
    """현재 슬롯을 수량 오버라이드와 연결하기 위한 안정적인 키.

    V10은 같은 날 같은 종가에 2개 슬롯이 생길 수 있으므로 position_id를 우선 사용합니다.
    V9 및 과거 저장값은 기존 날짜|가격 키를 그대로 유지합니다.
    """
    position_id = pos.get("position_id") if hasattr(pos, "get") else None
    if position_id is not None and str(position_id).startswith("v11-"):
        return f"v11|{str(position_id).strip()}"
    if position_id is not None and str(position_id).strip() and str(position_id).lower() != "nan":
        return f"v10|{str(position_id).strip()}"
    d = pd.Timestamp(pos["entry_date"]).normalize().date().isoformat()
    px = float(pos["entry_price"])
    return f"{d}|{px:.6f}"


def get_v9_quantity_overrides(open_positions, fx_daily=None, fallback_fx=None):
    """영구 저장값을 우선 불러오고, 새 슬롯에는 전략 기준 정수수량을 채웁니다."""
    if "v9_quantity_overrides" not in st.session_state:
        stored, status = load_persisted_v9_quantities()
        st.session_state["v9_quantity_overrides"] = stored
        st.session_state["v9_quantity_persistence_status"] = status
    else:
        stored = st.session_state.get("v9_quantity_overrides", {})

    if not isinstance(stored, dict):
        stored = {}

    # 과거에 청산된 티어의 실제 수량도 Trades에서 계속 사용해야 하므로
    # 저장 맵 전체를 유지하고, 현재 열린 슬롯만 기본값을 보충합니다.
    merged = dict(stored)
    for pos in open_positions or []:
        key = v9_position_key(pos)
        entry_date = pd.Timestamp(pos["entry_date"]).normalize()
        entry_price = float(pos["entry_price"])
        invested = float(pos["invested"])
        entry_fx = lookup_entry_fx(entry_date, fx_daily, fallback_fx)
        strategy_qty = (
            int(round(invested / (entry_price * entry_fx)))
            if entry_fx is not None and entry_fx > 0 and entry_price > 0
            else 0
        )
        saved_qty = merged.get(key, strategy_qty)
        try:
            saved_qty = max(0, int(round(float(saved_qty))))
        except (TypeError, ValueError):
            saved_qty = max(1, strategy_qty)
        merged[key] = saved_qty

    st.session_state["v9_quantity_overrides"] = merged
    return merged


def calculate_v9_actual_portfolio(
    open_positions,
    model_cash,
    confirmed_close,
    qty_overrides,
    entry_price_overrides=None,
    excluded_positions=None,
    fx_daily=None,
    fallback_fx=None,
):
    """
    전략상 매수금액과 실제 입력수량의 차이를 현금에 되돌려 실제 운용 NAV를 계산합니다.
    백테스트 엔진 자체는 수정하지 않고, 실전 포트폴리오 스냅샷만 보정합니다.
    """
    adjusted_cash = float(model_cash)
    confirmed_position_value = 0.0
    rows = []
    excluded = set(_sanitize_v9_excluded_positions(excluded_positions or []))

    for pos in open_positions or []:
        key = v9_position_key(pos)
        if key in excluded:
            # 전략상 슬롯은 유지하되 실전 포트폴리오에서는 미매수(0주)로 처리합니다.
            entry_date = pd.Timestamp(pos["entry_date"]).normalize()
            model_invested = float(pos["invested"])
            adjusted_cash += model_invested
            rows.append({
                "Key": key, "Entry_Date": entry_date, "Entry_Price": float(pos["entry_price"]),
                "Entry_FX": lookup_entry_fx(entry_date, fx_daily, fallback_fx),
                "Model_Invested": model_invested, "Actual_Qty": 0, "Actual_Invested": 0.0, "Confirmed_Value": 0.0,
            })
            continue
        entry_date = pd.Timestamp(pos["entry_date"]).normalize()
        strategy_entry_price = float(pos["entry_price"])
        key = v9_position_key(pos)
        entry_price = float((entry_price_overrides or {}).get(key, strategy_entry_price))
        model_invested = float(pos["invested"])
        entry_fx = lookup_entry_fx(entry_date, fx_daily, fallback_fx)

        if entry_fx is None or entry_fx <= 0 or entry_price <= 0:
            actual_qty = None
            actual_invested = model_invested
        else:
            strategy_qty = int(round(model_invested / (strategy_entry_price * entry_fx)))
            actual_qty = max(0, int(qty_overrides.get(key, max(1, strategy_qty))))
            actual_invested = actual_qty * entry_price * entry_fx

        # 전략 계획금액보다 덜 샀으면 현금이 늘고, 더 샀으면 현금이 줄어듭니다.
        adjusted_cash += model_invested - actual_invested
        confirmed_value = actual_invested * float(confirmed_close) / entry_price
        confirmed_position_value += confirmed_value

        rows.append({
            "Key": key,
            "Entry_Date": entry_date,
            "Entry_Price": entry_price,
            "Entry_FX": entry_fx,
            "Model_Invested": model_invested,
            "Actual_Qty": actual_qty,
            "Actual_Invested": actual_invested,
            "Confirmed_Value": confirmed_value,
        })

    return {
        "cash": float(adjusted_cash),
        "confirmed_position_value": float(confirmed_position_value),
        "nav": float(adjusted_cash + confirmed_position_value),
        "positions": pd.DataFrame(rows),
    }


def v9_exit_strategy_label(exit_type):
    mapping = {
        "TARGET": "일반 LOC(+2.7%)",
        "NORMAL_LOC": "일반 LOC(+2.7%)",
        "TB3_DYN_LOC": "방어 LOC(+$0.10)",
        "TIME": "TIME/MOC",
    }
    return mapping.get(str(exit_type), "기타")


def build_v9_actual_trade_records(
    trades_df,
    qty_overrides=None,
    trade_overrides=None,
    fx_daily=None,
    fallback_fx=None,
    excluded_positions=None,
):
    """전략 거래를 기본값으로 두고 저장된 실제 체결값을 덮어쓴 거래표를 만듭니다."""
    qty_overrides = qty_overrides or {}
    trade_overrides = _sanitize_v9_trade_overrides(trade_overrides or {})
    rows = []
    excluded = set(_sanitize_v9_excluded_positions(excluded_positions or []))

    for _, trade in (trades_df if trades_df is not None else pd.DataFrame()).iterrows():
        strategy_entry_date = pd.Timestamp(trade["Entry_Date"]).normalize()
        strategy_exit_date = pd.Timestamp(trade["Exit_Date"]).normalize()
        strategy_entry_price = float(trade["Entry_Price"])
        strategy_exit_price = float(trade["Exit_Price"])
        invested = float(trade.get("Invested", 0.0))
        key = v9_position_key({
            "entry_date": strategy_entry_date,
            "entry_price": strategy_entry_price,
            "position_id": trade.get("Position_ID"),
        })

        if key in excluded:
            continue

        strategy_fx = lookup_entry_fx(strategy_entry_date, fx_daily, fallback_fx)
        strategy_qty = None
        if strategy_fx is not None and strategy_fx > 0 and strategy_entry_price > 0:
            strategy_qty = max(1, int(round(invested / (strategy_entry_price * strategy_fx))))

        default_qty = strategy_qty
        if key in qty_overrides:
            try:
                default_qty = max(0, int(round(float(qty_overrides[key]))))
            except Exception:
                pass
        if default_qty is None:
            default_qty = 1

        defaults = {
            "entry_date": strategy_entry_date.date().isoformat(),
            "exit_date": strategy_exit_date.date().isoformat(),
            "entry_price": strategy_entry_price,
            "exit_price": strategy_exit_price,
            "quantity": default_qty,
            "exit_strategy": v9_exit_strategy_label(trade.get("Exit_Type")),
        }
        actual = dict(defaults)
        if key in trade_overrides:
            actual.update(trade_overrides[key])

        actual_entry_date = pd.Timestamp(actual["entry_date"]).normalize()
        actual_exit_date = pd.Timestamp(actual["exit_date"]).normalize()
        actual_entry_price = float(actual["entry_price"])
        actual_exit_price = float(actual["exit_price"])
        actual_qty = max(0, int(round(float(actual["quantity"]))))
        actual_fx = lookup_entry_fx(actual_entry_date, fx_daily, fallback_fx)

        actual_invested = None
        actual_profit = None
        actual_return = (
            actual_exit_price / actual_entry_price - 1
            if actual_entry_price > 0
            else None
        )
        if actual_fx is not None and actual_fx > 0 and actual_entry_price > 0:
            actual_invested = actual_qty * actual_entry_price * actual_fx
            actual_profit = actual_qty * (actual_exit_price - actual_entry_price) * actual_fx

        changed = key in trade_overrides
        rows.append({
            "_key": key,
            "_source": "strategy",
            "_manual_id": None,
            "전략 매수일": strategy_entry_date.date(),
            "전략 매도일": strategy_exit_date.date(),
            "전략 매수가": strategy_entry_price,
            "전략 매도가": strategy_exit_price,
            "전략 수량": strategy_qty if strategy_qty is not None else default_qty,
            "전략 매도전략": v9_exit_strategy_label(trade.get("Exit_Type")),
            "매수유형": str(trade.get("Buy_Type", "기본 MOC")),
            "매수일": actual_entry_date.date(),
            "매도일": actual_exit_date.date(),
            "매수가($)": actual_entry_price,
            "매도가($)": actual_exit_price,
            "수량(주)": actual_qty,
            "매도전략": str(actual["exit_strategy"]),
            "적용환율": actual_fx,
            "투자금(원)": actual_invested,
            "수익률": actual_return,
            "손익(원)": actual_profit,
            "전략손익(원)": float(trade.get("Profit", 0.0)),
            "보유 거래일": int(trade.get("Holding_Days", 0)),
            "수정": "수정됨" if changed else "전략값",
            "메모": "",
            "NAV반영": True,
        })

    return pd.DataFrame(rows)


def build_v9_manual_trade_records(manual_trades, fx_daily=None, fallback_fx=None):
    """전략에 존재하지 않는 사용자의 수동 청산거래를 실제 거래표 형식으로 변환합니다."""
    rows = []
    for item in _sanitize_v9_manual_trades(manual_trades):
        entry_date = pd.Timestamp(item["entry_date"]).normalize()
        exit_date = pd.Timestamp(item["exit_date"]).normalize()
        entry_price = float(item["entry_price"])
        exit_price = float(item["exit_price"])
        qty = int(item["quantity"])
        entry_fx = lookup_entry_fx(entry_date, fx_daily, fallback_fx)
        invested = profit = None
        if entry_fx is not None and entry_fx > 0:
            invested = qty * entry_price * entry_fx
            profit = qty * (exit_price - entry_price) * entry_fx
        rows.append({
            "_key": f"manual|{item['id']}",
            "_source": "manual",
            "_manual_id": item["id"],
            "전략 매수일": pd.NaT,
            "전략 매도일": pd.NaT,
            "전략 매수가": float("nan"),
            "전략 매도가": float("nan"),
            "전략 수량": float("nan"),
            "전략 매도전략": "-",
            "매수유형": "수동 추가",
            "매수일": entry_date.date(),
            "매도일": exit_date.date(),
            "매수가($)": entry_price,
            "매도가($)": exit_price,
            "수량(주)": qty,
            "매도전략": item["exit_strategy"],
            "적용환율": entry_fx,
            "투자금(원)": invested,
            "수익률": (exit_price / entry_price - 1) if entry_price > 0 else None,
            "손익(원)": profit,
            "전략손익(원)": 0.0,
            "보유 거래일": max(0, (exit_date - entry_date).days),
            "수정": "수동추가",
            "메모": item.get("note", ""),
            "NAV반영": bool(item.get("affect_nav", True)),
        })
    return pd.DataFrame(rows)


def calculate_v9_manual_realized_profit(manual_trades, fx_daily=None, fallback_fx=None):
    """NAV 반영으로 지정한 수동 청산거래의 실현손익 합계를 계산합니다."""
    records = build_v9_manual_trade_records(manual_trades, fx_daily=fx_daily, fallback_fx=fallback_fx)
    if records.empty:
        return 0.0
    mask = records["NAV반영"].fillna(False).astype(bool)
    profits = pd.to_numeric(records.loc[mask, "손익(원)"], errors="coerce").dropna()
    return float(profits.sum()) if not profits.empty else 0.0


def calculate_v9_realized_execution_adjustment(
    trades_df,
    trade_overrides,
    qty_overrides=None,
    fx_daily=None,
    fallback_fx=None,
    excluded_positions=None,
):
    """사용자가 수정·저장한 청산거래만 실전 현금/NAV에 반영할 손익 차이를 계산합니다."""
    cleaned = _sanitize_v9_trade_overrides(trade_overrides or {})
    if not cleaned or trades_df is None or trades_df.empty:
        return 0.0

    actual_records = build_v9_actual_trade_records(
        trades_df,
        qty_overrides=qty_overrides,
        trade_overrides=cleaned,
        fx_daily=fx_daily,
        fallback_fx=fallback_fx,
        excluded_positions=excluded_positions,
    )
    if actual_records.empty:
        return 0.0

    adjusted = 0.0
    for _, row in actual_records.iterrows():
        if row["_key"] not in cleaned:
            continue
        actual_profit = row.get("손익(원)")
        if actual_profit is None or pd.isna(actual_profit):
            continue
        adjusted += float(actual_profit) - float(row.get("전략손익(원)", 0.0))
    return float(adjusted)


def build_v9_quantity_editor_rows(open_positions, qty_overrides, fx_daily=None, fallback_fx=None):
    rows = []
    for slot_no, pos in enumerate(open_positions or [], start=1):
        key = v9_position_key(pos)
        entry_date = pd.Timestamp(pos["entry_date"]).normalize()
        entry_price = float(pos["entry_price"])
        invested = float(pos["invested"])
        entry_fx = lookup_entry_fx(entry_date, fx_daily, fallback_fx)
        strategy_qty = (
            int(round(invested / (entry_price * entry_fx)))
            if entry_fx is not None and entry_fx > 0 and entry_price > 0
            else 0
        )
        rows.append({
            "_key": key,
            "슬롯": slot_no,
            "매수일": entry_date.date(),
            "매수가": f"${entry_price:,.2f}",
            "전략 기준수량": strategy_qty,
            "실제 보유수량": int(qty_overrides.get(key, max(1, strategy_qty))),
        })
    return pd.DataFrame(rows)


def build_v9_open_positions_table(
    open_positions,
    current_close,
    as_of_date,
    tb3_state,
    holding_days=7,
    fx_daily=None,
    fallback_fx=None,
    qty_overrides=None,
    entry_price_overrides=None,
    excluded_positions=None,
):
    """V9 미청산 포지션을 현재 적용 LOC와 실제 입력수량 기준으로 표시합니다."""
    if not open_positions:
        return pd.DataFrame()

    rows = []
    excluded = set(_sanitize_v9_excluded_positions(excluded_positions or []))
    visible_positions = [p for p in open_positions if v9_position_key(p) not in excluded]
    for slot_no, pos in enumerate(visible_positions, start=1):
        entry_date = pd.Timestamp(pos["entry_date"]).normalize()
        strategy_entry_price = float(pos["entry_price"])
        key = v9_position_key(pos)
        entry_price = float((entry_price_overrides or {}).get(key, strategy_entry_price))
        invested = float(pos["invested"])
        deadline, estimated = get_moc_deadline(entry_date, holding_days=holding_days)

        if tb3_state:
            target_price = entry_price + TB3_LOC_PLUS_DOLLAR
            loc_mode = "방어 +$0.10"
        else:
            target_price = entry_price * (1 + BASE_TARGET_RETURN)
            loc_mode = "일반 +2.7%"

        current_return = float(current_close) / entry_price - 1

        if as_of_date > deadline:
            status = "MOC(종가 매도) 기한 확인 필요"
            remaining_text = "기한 지남"
        elif as_of_date == deadline:
            status = "MOC(종가 매도) 오늘 예정"
            remaining_text = "오늘"
        else:
            remaining = 0
            cursor = pd.Timestamp(as_of_date).normalize() + pd.Timedelta(days=1)
            while cursor <= deadline:
                if is_nyse_trading_day(cursor):
                    remaining += 1
                cursor += pd.Timedelta(days=1)
            remaining_text = f"{remaining}거래일"
            status = "LOC(목표수익 매도) 대기"

        # 백테스트 엔진의 pos["shares"]는 원화 자본을 달러 가격으로 나눈
        # 수익률 계산용 합성 단위입니다. 실제 주문수량 표시에 사용하면 안 됩니다.
        # 실제 수량은 원화 매수금액을 매수일 USD/KRW로 달러 환산한 뒤 계산합니다.
        entry_fx = lookup_entry_fx(entry_date, fx_daily, fallback_fx)
        strategy_qty = (
            int(round(invested / (strategy_entry_price * entry_fx)))
            if entry_fx is not None and entry_fx > 0 and strategy_entry_price > 0
            else None
        )
        if strategy_qty is not None:
            selected_qty = max(0, int((qty_overrides or {}).get(key, max(1, strategy_qty))))
            actual_invested = selected_qty * entry_price * entry_fx
        else:
            selected_qty = None
            actual_invested = invested

        rows.append({
            "_key": key,
            "슬롯": slot_no,
            "매수유형": str(pos.get("buy_type", "기본 MOC")),
            "매수일": entry_date.date(),
            "매수금액": f"{actual_invested:,.0f}원",
            "전략 매수가": f"${strategy_entry_price:,.2f}",
            "매수가": float(entry_price),
            "적용환율": f"{entry_fx:,.2f}원/$" if entry_fx is not None else "환율 조회 필요",
            "보유수량": f"{selected_qty:,.0f}주" if selected_qty is not None else "계산 불가",
            "현재가": f"${float(current_close):,.2f}",
            "현재 수익률": f"{current_return:.2%}",
            "평가손익": f"{actual_invested * current_return:+,.0f}원",
            "LOC 모드": loc_mode,
            "LOC 주문가": f"${target_price:,.2f}",
            "MOC 예정일": f"{deadline.date()}{' (예상)' if estimated else ''}",
            "남은 거래일": remaining_text,
            "상태": status,
        })

    return pd.DataFrame(rows)


def build_live_open_holdings_df(
    strategy_mode, live_result, live_trades,
    qty_overrides=None, entry_price_overrides=None, excluded_positions=None, fx_daily=None, fallback_fx=None,
):
    """실시간 평가손익 계산에 사용할 미청산 포지션 원자료."""
    if strategy_mode.startswith(("V9", "V10", "V11")):
        positions = live_result.get("open_positions", [])
        if not positions:
            return pd.DataFrame(columns=["Entry_Price", "Invested"])
        rows = []
        excluded = set(_sanitize_v9_excluded_positions(excluded_positions or []))
        for p in positions:
            if v9_position_key(p) in excluded:
                continue
            entry_date = pd.Timestamp(p["entry_date"]).normalize()
            strategy_entry_price = float(p["entry_price"])
            key = v9_position_key(p)
            entry_price = float((entry_price_overrides or {}).get(key, strategy_entry_price))
            model_invested = float(p["invested"])
            entry_fx = lookup_entry_fx(entry_date, fx_daily, fallback_fx)
            if entry_fx is not None and entry_fx > 0 and entry_price > 0:
                strategy_qty = int(round(model_invested / (strategy_entry_price * entry_fx)))
                actual_qty = max(0, int((qty_overrides or {}).get(key, max(1, strategy_qty))))
                actual_invested = actual_qty * entry_price * entry_fx
            else:
                actual_invested = model_invested
            rows.append({"Entry_Price": entry_price, "Invested": actual_invested})
        return pd.DataFrame(rows)

    if not live_trades.empty and "Exit_Type" in live_trades.columns:
        return live_trades[live_trades["Exit_Type"] == "END"].copy()

    return pd.DataFrame(columns=["Entry_Price", "Invested"])


# ============================================================
# COMMON TRADE SUMMARY
# ============================================================

def summarize_trades(
    subset
):

    if subset.empty:

        return {
            "Trades": 0,
            "Avg_Return": None,
            "Median_Return": None,
            "Win_Rate": None,
            "Target_Rate": None,
            "Time_Rate": None,
            "Avg_Holding": None,
            "Worst_Return": None,
            "Best_Return": None,
            "Profit_Factor": None,
            "Total_Profit": None,
        }


    winners = subset[
        subset["Profit"] > 0
    ]

    losers = subset[
        subset["Profit"] < 0
    ]


    gross_profit = (
        winners["Profit"].sum()
    )

    gross_loss = abs(
        losers["Profit"].sum()
    )


    if gross_loss > 0:

        profit_factor = (
            gross_profit
            / gross_loss
        )

    elif gross_profit > 0:

        profit_factor = float("inf")

    else:

        profit_factor = None


    return {

        "Trades":
            len(subset),

        "Avg_Return":
            subset[
                "Return"
            ].mean(),

        "Median_Return":
            subset[
                "Return"
            ].median(),

        "Win_Rate":
            (
                subset[
                    "Return"
                ] > 0
            ).mean(),

        "Target_Rate":
            subset["Exit_Type"].isin(
                ["TARGET", "NORMAL_LOC", "TB3_DYN_LOC"]
            ).mean(),

        "Time_Rate":
            (
                subset[
                    "Exit_Type"
                ] == "TIME"
            ).mean(),

        "Avg_Holding":
            subset[
                "Holding_Days"
            ].mean(),

        "Worst_Return":
            subset[
                "Return"
            ].min(),

        "Best_Return":
            subset[
                "Return"
            ].max(),

        "Profit_Factor":
            profit_factor,

        "Total_Profit":
            subset[
                "Profit"
            ].sum(),

    }


# ============================================================
# TIME ANALYSIS
# ============================================================

def analyze_time_exits(
    trades_df,
    price_df,
):

    time_trades = trades_df[
        trades_df["Exit_Type"] == "TIME"
    ].copy()


    if time_trades.empty:

        return {
            "trades": pd.DataFrame(),
            "summary": {},
            "buckets": pd.DataFrame(),
            "yearly": pd.DataFrame(),
        }


    time_trades[
        "Entry_Date"
    ] = pd.to_datetime(
        time_trades[
            "Entry_Date"
        ]
    )


    time_trades[
        "Exit_Date"
    ] = pd.to_datetime(
        time_trades[
            "Exit_Date"
        ]
    )


    price_data = (
        price_df[
            [
                "Date",
                "Close",
            ]
        ]
        .copy()
        .reset_index(drop=True)
    )


    price_data[
        "Date"
    ] = pd.to_datetime(
        price_data[
            "Date"
        ]
    )


    date_to_index = {
        pd.Timestamp(date).normalize(): i
        for i, date
        in enumerate(
            price_data["Date"]
        )
    }


    horizons = [
        1,
        3,
        5,
        10,
    ]


    # --------------------------------------------------------
    # FORWARD RETURNS
    # --------------------------------------------------------

    for horizon in horizons:

        results = []


        for _, trade in (
            time_trades.iterrows()
        ):

            exit_date = (
                pd.Timestamp(
                    trade[
                        "Exit_Date"
                    ]
                )
                .normalize()
            )


            exit_index = (
                date_to_index.get(
                    exit_date
                )
            )


            if exit_index is None:

                results.append(
                    None
                )

                continue


            future_index = (
                exit_index
                + horizon
            )


            if future_index >= len(
                price_data
            ):

                results.append(
                    None
                )

                continue


            exit_price = float(
                trade[
                    "Exit_Price"
                ]
            )


            future_price = float(
                price_data.iloc[
                    future_index
                ][
                    "Close"
                ]
            )


            results.append(
                future_price
                / exit_price
                - 1
            )


        time_trades[
            f"Forward_{horizon}D"
        ] = results


    # --------------------------------------------------------
    # RETURN BUCKETS
    # --------------------------------------------------------

    time_trades[
        "Return_Bucket"
    ] = pd.cut(

        time_trades[
            "Return"
        ],

        bins=[
            float("-inf"),
            -0.30,
            -0.20,
            -0.10,
            -0.05,
            0,
            float("inf"),
        ],

        labels=[
            "-30% 이하",
            "-30% ~ -20%",
            "-20% ~ -10%",
            "-10% ~ -5%",
            "-5% ~ 0%",
            "0% 이상",
        ],

        right=False,

    )


    bucket_df = (
        time_trades[
            "Return_Bucket"
        ]
        .value_counts(
            sort=False
        )
        .reset_index()
    )


    bucket_df.columns = [
        "구간",
        "거래수",
    ]


    bucket_df[
        "비율"
    ] = (
        bucket_df[
            "거래수"
        ]
        / len(
            time_trades
        )
    )


    # --------------------------------------------------------
    # YEARLY
    # --------------------------------------------------------

    time_trades[
        "Exit_Year"
    ] = (
        time_trades[
            "Exit_Date"
        ]
        .dt.year
    )


    yearly_df = (
        time_trades
        .groupby(
            "Exit_Year"
        )
        .agg(

            TIME_Count=(
                "Return",
                "size",
            ),

            Avg_Return=(
                "Return",
                "mean",
            ),

            Median_Return=(
                "Return",
                "median",
            ),

            Total_Profit=(
                "Profit",
                "sum",
            ),

            Worst_Return=(
                "Return",
                "min",
            ),

        )
        .reset_index()
    )


    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    total_loss = abs(

        trades_df.loc[
            trades_df[
                "Profit"
            ] < 0,
            "Profit",
        ].sum()

    )


    time_loss = abs(

        time_trades.loc[
            time_trades[
                "Profit"
            ] < 0,
            "Profit",
        ].sum()

    )


    summary = {

        "count":
            len(
                time_trades
            ),

        "avg_return":
            time_trades[
                "Return"
            ].mean(),

        "median_return":
            time_trades[
                "Return"
            ].median(),

        "worst_return":
            time_trades[
                "Return"
            ].min(),

        "best_return":
            time_trades[
                "Return"
            ].max(),

        "total_profit":
            time_trades[
                "Profit"
            ].sum(),

        "loss_share":
            (
                time_loss
                / total_loss
                if total_loss > 0
                else 0
            ),

    }


    for horizon in horizons:

        col = (
            f"Forward_{horizon}D"
        )

        valid = (
            time_trades[
                col
            ]
            .dropna()
        )


        if len(valid) > 0:

            summary[
                f"forward_{horizon}_avg"
            ] = valid.mean()

            summary[
                f"forward_{horizon}_median"
            ] = valid.median()

            summary[
                f"forward_{horizon}_positive"
            ] = (
                valid > 0
            ).mean()

        else:

            summary[
                f"forward_{horizon}_avg"
            ] = None

            summary[
                f"forward_{horizon}_median"
            ] = None

            summary[
                f"forward_{horizon}_positive"
            ] = None


    return {

        "trades":
            time_trades,

        "summary":
            summary,

        "buckets":
            bucket_df,

        "yearly":
            yearly_df,

    }


# ============================================================
# MARKET REGIME V2
# ============================================================

def analyze_market_regime_v2(
    trades_df,
    full_price_df,
):

    prices = (
        full_price_df
        .copy()
        .sort_values(
            "Date"
        )
        .reset_index(
            drop=True
        )
    )


    prices[
        "Date"
    ] = pd.to_datetime(
        prices[
            "Date"
        ]
    )


    # --------------------------------------------------------
    # INDICATORS
    # --------------------------------------------------------

    prices[
        "MA200"
    ] = (

        prices[
            "Close"
        ]

        .rolling(
            window=200,
            min_periods=200,
        )

        .mean()

    )


    prices[
        "MA200_Gap"
    ] = (

        prices[
            "Close"
        ]

        / prices[
            "MA200"
        ]

        - 1

    )


    prices[
        "Momentum_20D"
    ] = (

        prices[
            "Close"
        ]

        / prices[
            "Close"
        ].shift(
            20
        )

        - 1

    )


    # --------------------------------------------------------
    # MA200 GAP BUCKET
    # --------------------------------------------------------

    ma_labels = [

        "-30% 이하",

        "-30% ~ -15%",

        "-15% ~ -5%",

        "-5% ~ +5%",

        "+5% ~ +20%",

        "+20% 이상",

    ]


    prices[
        "MA200_Bucket"
    ] = pd.cut(

        prices[
            "MA200_Gap"
        ],

        bins=[
            float("-inf"),
            -0.30,
            -0.15,
            -0.05,
            0.05,
            0.20,
            float("inf"),
        ],

        labels=
            ma_labels,

        right=False,

    )


    # --------------------------------------------------------
    # MOMENTUM BUCKET
    # --------------------------------------------------------

    momentum_labels = [

        "-20% 이하",

        "-20% ~ -5%",

        "-5% ~ +5%",

        "+5% ~ +20%",

        "+20% 이상",

    ]


    prices[
        "Momentum_Bucket"
    ] = pd.cut(

        prices[
            "Momentum_20D"
        ],

        bins=[
            float("-inf"),
            -0.20,
            -0.05,
            0.05,
            0.20,
            float("inf"),
        ],

        labels=
            momentum_labels,

        right=False,

    )


    # --------------------------------------------------------
    # MERGE TO TRADES
    # --------------------------------------------------------

    trades = (
        trades_df.copy()
    )


    trades[
        "Entry_Date"
    ] = pd.to_datetime(
        trades[
            "Entry_Date"
        ]
    )


    indicators = (
        prices[
            [
                "Date",
                "MA200",
                "MA200_Gap",
                "Momentum_20D",
                "MA200_Bucket",
                "Momentum_Bucket",
            ]
        ]
        .copy()
    )


    indicators = (
        indicators.rename(
            columns={
                "Date":
                    "Entry_Date"
            }
        )
    )


    trades = (
        trades.merge(
            indicators,
            on=
                "Entry_Date",
            how=
                "left",
        )
    )


    # --------------------------------------------------------
    # MA SUMMARY
    # --------------------------------------------------------

    ma_rows = []


    for label in ma_labels:

        subset = trades[
            trades[
                "MA200_Bucket"
            ] == label
        ]


        summary = (
            summarize_trades(
                subset
            )
        )


        summary[
            "Bucket"
        ] = label


        ma_rows.append(
            summary
        )


    ma_summary = (
        pd.DataFrame(
            ma_rows
        )
    )


    # --------------------------------------------------------
    # MOMENTUM SUMMARY
    # --------------------------------------------------------

    momentum_rows = []


    for label in momentum_labels:

        subset = trades[
            trades[
                "Momentum_Bucket"
            ] == label
        ]


        summary = (
            summarize_trades(
                subset
            )
        )


        summary[
            "Bucket"
        ] = label


        momentum_rows.append(
            summary
        )


    momentum_summary = (
        pd.DataFrame(
            momentum_rows
        )
    )


    # --------------------------------------------------------
    # COMBINATION SUMMARY
    # --------------------------------------------------------

    combo_rows = []


    for ma_label in ma_labels:

        for mom_label in momentum_labels:

            subset = trades[
                (
                    trades[
                        "MA200_Bucket"
                    ] == ma_label
                )
                &
                (
                    trades[
                        "Momentum_Bucket"
                    ] == mom_label
                )
            ]


            summary = (
                summarize_trades(
                    subset
                )
            )


            summary[
                "MA200_Bucket"
            ] = ma_label


            summary[
                "Momentum_Bucket"
            ] = mom_label


            combo_rows.append(
                summary
            )


    combo_summary = (
        pd.DataFrame(
            combo_rows
        )
    )


    return {

        "prices":
            prices,

        "trades":
            trades,

        "ma_summary":
            ma_summary,

        "momentum_summary":
            momentum_summary,

        "combo_summary":
            combo_summary,

        "ma_labels":
            ma_labels,

        "momentum_labels":
            momentum_labels,

    }


# ============================================================
# FORMAT REGIME TABLE
# ============================================================

def format_regime_table(
    source_df
):

    df = (
        source_df.copy()
    )


    result = pd.DataFrame()


    result[
        "구간"
    ] = df[
        "Bucket"
    ]


    result[
        "거래수"
    ] = df[
        "Trades"
    ]


    for source, target in [

        (
            "Avg_Return",
            "평균 수익률",
        ),

        (
            "Median_Return",
            "중앙값",
        ),

        (
            "Win_Rate",
            "승률",
        ),

        (
            "Target_Rate",
            "목표가 매도 비율(LOC)",
        ),

        (
            "Time_Rate",
            "기한만료 매도 비율(TIME)",
        ),

        (
            "Worst_Return",
            "최악 거래",
        ),

    ]:

        result[
            target
        ] = df[
            source
        ].map(

            lambda x:
                f"{x:.2%}"
                if pd.notna(x)
                else "-"

        )


    result[
        "평균 보유기간"
    ] = df[
        "Avg_Holding"
    ].map(

        lambda x:
            f"{x:.2f}일"
            if pd.notna(x)
            else "-"

    )


    result[
        "손익 효율(PF)"
    ] = df[
        "Profit_Factor"
    ].map(

        lambda x:
            (
                "∞"
                if x == float("inf")
                else f"{x:.2f}"
            )
            if pd.notna(x)
            else "-"

    )


    return result


# ============================================================
# LOAD DATA
# ============================================================

try:

    full_df = get_data()

except Exception as e:

    st.error(
        f"데이터 로딩 실패: {e}"
    )

    st.stop()


# ============================================================
# BASE PARAMETERS
# ============================================================

BASE_POSITION_DIVISOR = 7
BASE_MAX_POSITIONS = 7
BASE_TARGET_RETURN = 0.027
BASE_HOLDING_DAYS = 7
BASE_RESERVE_RATIO = 0.0


# ============================================================
# APP MODE / BACKTEST LAB
# ============================================================

def _lab_safe_stat(stats, key, default=float("nan")):
    value = stats.get(key, default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _lab_pf_text(value):
    try:
        if value == float("inf"):
            return "∞"
        if pd.isna(value):
            return "-"
        return f"{float(value):.2f}"
    except Exception:
        return "-"


def _lab_build_result(full_price_df, start_value, end_value, capital, strategy_name, custom_params=None):
    """Pure backtest helper. Never reads or writes live_portfolio.enc."""
    custom_params = custom_params or {}
    start_ts = pd.Timestamp(start_value)
    end_ts = pd.Timestamp(end_value)
    selected_df = full_price_df[
        (full_price_df["Date"] >= start_ts) & (full_price_df["Date"] <= end_ts)
    ].copy().reset_index(drop=True)
    if selected_df.empty:
        raise ValueError("선택한 기간에 데이터가 없습니다.")

    if strategy_name.startswith("V11"):
        raw = run_v11_backtest(raw_df=full_price_df, initial_capital=capital, start_date=start_value, end_date=end_value)
        return add_result_tables(raw, capital)

    if strategy_name.startswith("V10"):
        raw = run_v10_backtest(
            raw_df=full_price_df,
            initial_capital=capital,
            start_date=start_value,
            end_date=end_value,
        )
        return add_result_tables(raw, capital)

    if strategy_name.startswith("V9"):
        raw = run_v9_backtest(
            raw_df=full_price_df,
            initial_capital=capital,
            start_date=start_value,
            end_date=end_value,
        )
        return add_result_tables(raw, capital)

    if strategy_name.startswith("V8"):
        raw = run_risk_divisor_backtest(
            raw_df=selected_df,
            risk_divisor=None,
            initial_capital=capital,
        )
        return add_result_tables(raw, capital)

    if strategy_name.startswith("V7"):
        raw = run_backtest(
            df=selected_df,
            initial_capital=capital,
            position_divisor=BASE_POSITION_DIVISOR,
            max_positions=BASE_MAX_POSITIONS,
            target_return=BASE_TARGET_RETURN,
            holding_days=BASE_HOLDING_DAYS,
            reserve_ratio=BASE_RESERVE_RATIO,
        )
        return add_result_tables(raw, capital)

    # Custom Lab: V10 엔진을 범용화해 V7~V10 계열 규칙을 독립적으로 실험합니다.
    raw = run_v10_backtest(
        raw_df=full_price_df,
        initial_capital=capital,
        start_date=start_value,
        end_date=end_value,
        crash_threshold=-float(custom_params.get("crash_pct", 9.0)) / 100.0,
        position_divisor=int(custom_params.get("position_divisor", 7)),
        max_positions=int(custom_params.get("max_positions", 7)),
        target_return=float(custom_params.get("target_pct", 2.7)) / 100.0,
        tb3_loc_plus_dollar=float(custom_params.get("tb3_plus", 0.10)),
        holding_days_limit=int(custom_params.get("holding_days", 7)),
        reserve_ratio=float(custom_params.get("reserve_pct", 0.0)) / 100.0,
        enable_risk_b=bool(custom_params.get("enable_risk_b", True)),
        enable_tb3=bool(custom_params.get("enable_tb3", True)),
        enable_crash_buy=bool(custom_params.get("enable_crash_buy", True)),
        crash_extra_slots=int(custom_params.get("crash_extra_slots", 1)),
    )
    return add_result_tables(raw, capital)


def render_backtest_lab(full_price_df):
    st.subheader("🧪 전략 백테스트 · 연구 전용")
    st.info(
        "이 페이지는 SOXL 시장 데이터와 전략 엔진만 사용합니다. "
        "실제 보유수량·실제 체결가·입출금·수동거래·live_portfolio.enc는 읽거나 수정하지 않습니다."
    )

    min_d = pd.Timestamp(full_price_df["Date"].min()).date()
    max_d = pd.Timestamp(full_price_df["Date"].max()).date()
    default_start = max(min_d, date(2022, 1, 1))

    st.sidebar.header("🧪 Backtest Lab")
    strategy_name = st.sidebar.selectbox(
        "전략",
        [
            "V11 Final(D2 손절 + 연속하락 방어, TB3 OFF)",
            "V10 Final(-9% 급락 추가매수)",
            "V9 Final(약세필터 + TB3 방어LOC)",
            "V8 장기과매도 약세필터",
            "V7 Base(기본 7분할)",
            "Custom Lab(직접 설정)",
        ],
        key="lab_strategy",
    )
    lab_initial_capital = st.sidebar.number_input(
        "가상 초기 투자금 (원)",
        min_value=1_000_000,
        max_value=10_000_000_000,
        value=100_000_000,
        step=10_000_000,
        key="lab_capital",
    )
    lab_start = st.sidebar.date_input(
        "백테스트 시작일",
        value=default_start,
        min_value=min_d,
        max_value=max_d,
        key="lab_start_date",
    )
    lab_end = st.sidebar.date_input(
        "백테스트 종료일",
        value=max_d,
        min_value=min_d,
        max_value=max_d,
        key="lab_end_date",
    )

    custom = {}
    if strategy_name.startswith("Custom"):
        st.sidebar.divider()
        st.sidebar.caption("Custom 설정은 실전 운용 설정과 완전히 분리됩니다.")
        custom["position_divisor"] = st.sidebar.slider("분할 수", 2, 20, 7, key="lab_divisor")
        custom["max_positions"] = st.sidebar.slider("최대 동시 보유 슬롯", 1, 20, 7, key="lab_max_positions")
        custom["target_pct"] = st.sidebar.slider("일반 LOC 목표수익률 (%)", 0.1, 15.0, 2.7, 0.1, key="lab_target_pct")
        custom["holding_days"] = st.sidebar.slider("TIME 보유기간 (거래일)", 1, 30, 7, key="lab_holding_days")
        custom["reserve_pct"] = st.sidebar.slider("현금 보유비율 (%)", 0.0, 50.0, 0.0, 1.0, key="lab_reserve_pct")
        custom["enable_risk_b"] = st.sidebar.checkbox("장기과매도 약세필터(Risk B)", True, key="lab_risk_b")
        custom["enable_tb3"] = st.sidebar.checkbox("TB3 방어 LOC", True, key="lab_tb3")
        if custom["enable_tb3"]:
            custom["tb3_plus"] = st.sidebar.number_input("TB3 LOC = 매수가 + $", 0.01, 5.00, 0.10, 0.01, key="lab_tb3_plus")
        else:
            custom["tb3_plus"] = 0.10
        custom["enable_crash_buy"] = st.sidebar.checkbox("급락 추가매수", True, key="lab_crash_enable")
        if custom["enable_crash_buy"]:
            custom["crash_pct"] = st.sidebar.slider("급락 LOC 기준 (%)", 5.0, 20.0, 9.0, 0.5, key="lab_crash_pct")
            custom["crash_extra_slots"] = st.sidebar.slider("급락 시 추가 슬롯 수", 1, 3, 1, key="lab_crash_slots")
        else:
            custom["crash_pct"] = 9.0
            custom["crash_extra_slots"] = 0

    if lab_start > lab_end:
        st.error("백테스트 시작일이 종료일보다 늦습니다.")
        return

    comparison_options = [
        "비교 안 함",
        "V11 Final(D2 손절 + 연속하락 방어, TB3 OFF)",
        "V10 Final(-9% 급락 추가매수)",
        "V9 Final(약세필터 + TB3 방어LOC)",
        "V8 장기과매도 약세필터",
        "V7 Base(기본 7분할)",
    ]
    comparison_name = st.sidebar.selectbox("비교 전략 (선택)", comparison_options, key="lab_compare")

    with st.spinner("백테스트 계산 중..."):
        result = _lab_build_result(
            full_price_df, lab_start, lab_end, float(lab_initial_capital), strategy_name, custom
        )
        comparison_result = None
        if comparison_name != "비교 안 함" and comparison_name != strategy_name:
            comparison_result = _lab_build_result(
                full_price_df, lab_start, lab_end, float(lab_initial_capital), comparison_name, {}
            )

    stats = result.get("stats", {})
    final_equity = _lab_safe_stat(stats, "final_equity")
    cagr = _lab_safe_stat(stats, "cagr")
    mdd = _lab_safe_stat(stats, "mdd")
    calmar = _lab_safe_stat(stats, "calmar")
    pf = stats.get("profit_factor", float("nan"))
    win_rate = _lab_safe_stat(stats, "win_rate")
    total_trades = int(stats.get("total_trades", len(result.get("trades", []))) or 0)
    avg_holding = _lab_safe_stat(stats, "avg_holding_days")

    st.markdown(f"### {strategy_name}")
    st.caption(f"검증기간 {lab_start} ~ {lab_end} · 초기자금 {lab_initial_capital:,.0f}원")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("최종자산", f"{final_equity:,.0f}원" if pd.notna(final_equity) else "-")
    c2.metric("연평균 복리수익률(CAGR)", f"{cagr:.2%}" if pd.notna(cagr) else "-")
    c3.metric("최대 낙폭(MDD)", f"{mdd:.2%}" if pd.notna(mdd) else "-")
    c4.metric("수익·낙폭 효율(Calmar)", f"{calmar:.2f}" if pd.notna(calmar) else "-")
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("손익 효율(PF)", _lab_pf_text(pf))
    c6.metric("승률", f"{win_rate:.2%}" if pd.notna(win_rate) else "-")
    c7.metric("총 거래 수", f"{total_trades:,}")
    c8.metric("평균 보유기간", f"{avg_holding:.2f}일" if pd.notna(avg_holding) else "-")

    equity = result.get("equity", pd.DataFrame()).copy()
    if not equity.empty:
        equity["Date"] = pd.to_datetime(equity["Date"])
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=equity["Date"], y=equity["Equity"], mode="lines", name=strategy_name))
        if comparison_result is not None:
            ce = comparison_result.get("equity", pd.DataFrame()).copy()
            if not ce.empty:
                ce["Date"] = pd.to_datetime(ce["Date"])
                fig.add_trace(go.Scatter(x=ce["Date"], y=ce["Equity"], mode="lines", name=comparison_name))
        fig.update_layout(
            title="자산곡선",
            xaxis_title="날짜",
            yaxis_title="자산 (원)",
            hovermode="x unified",
            legend_title_text="전략",
            margin=dict(l=10, r=10, t=55, b=10),
            height=430,
        )
        st.plotly_chart(fig, use_container_width=True)

    drawdown = result.get("drawdown", pd.DataFrame()).copy()
    if not drawdown.empty:
        drawdown["Date"] = pd.to_datetime(drawdown["Date"])
        dd_fig = go.Figure()
        dd_fig.add_trace(go.Scatter(
            x=drawdown["Date"], y=drawdown["Drawdown"] * 100,
            mode="lines", name="Drawdown"
        ))
        dd_fig.update_layout(
            title="Drawdown",
            xaxis_title="날짜",
            yaxis_title="낙폭 (%)",
            hovermode="x unified",
            margin=dict(l=10, r=10, t=55, b=10),
            height=300,
        )
        st.plotly_chart(dd_fig, use_container_width=True)

    tab_year, tab_trade, tab_setting = st.tabs(["📅 연도별", "🧾 거래내역", "⚙️ 실행조건"])
    with tab_year:
        yearly = result.get("yearly", pd.DataFrame()).copy()
        if yearly.empty:
            st.caption("연도별 데이터가 없습니다.")
        else:
            display = yearly.copy()
            if "Year_Return" in display.columns:
                display["연 수익률"] = display["Year_Return"].map(lambda x: f"{x:.2%}")
            if "Year_MDD" in display.columns:
                display["연중 최대 낙폭"] = display["Year_MDD"].map(lambda x: f"{x:.2%}")
            if "Year_End_Equity" in display.columns:
                display["연말 자산"] = display["Year_End_Equity"].map(lambda x: f"{x:,.0f}원")
            keep = [c for c in ["Year", "연 수익률", "연중 최대 낙폭", "연말 자산"] if c in display.columns]
            display = display[keep].rename(columns={"Year": "연도"})
            st.dataframe(display, use_container_width=True, hide_index=True)

    with tab_trade:
        trades = result.get("trades", pd.DataFrame()).copy()
        if trades.empty:
            st.caption("거래내역이 없습니다.")
        else:
            show_cols = [c for c in [
                "Position_ID", "Buy_Type", "Entry_Date", "Exit_Date", "Entry_Price", "Exit_Price",
                "Return", "Profit", "Holding_Days", "Exit_Type"
            ] if c in trades.columns]
            trade_display = trades[show_cols].copy()
            for c in ["Entry_Date", "Exit_Date"]:
                if c in trade_display.columns:
                    trade_display[c] = pd.to_datetime(trade_display[c]).dt.date
            if "Return" in trade_display.columns:
                trade_display["Return"] = trade_display["Return"].map(lambda x: f"{x:.2%}")
            st.dataframe(trade_display, use_container_width=True, hide_index=True, height=460)
            st.download_button(
                "⬇️ 거래내역 CSV 다운로드",
                data=trades.to_csv(index=False).encode("utf-8-sig"),
                file_name=f"SOXL_backtest_{lab_start}_{lab_end}.csv",
                mime="text/csv",
                use_container_width=True,
            )

    with tab_setting:
        settings_rows = [
            {"항목": "페이지", "값": "백테스트 Lab (실전 데이터와 분리)"},
            {"항목": "전략", "값": strategy_name},
            {"항목": "기간", "값": f"{lab_start} ~ {lab_end}"},
            {"항목": "가상 초기자금", "값": f"{lab_initial_capital:,.0f}원"},
        ]
        if strategy_name.startswith("Custom"):
            settings_rows.extend([
                {"항목": "분할 수", "값": custom["position_divisor"]},
                {"항목": "최대 슬롯", "값": custom["max_positions"]},
                {"항목": "LOC 목표", "값": f"+{custom['target_pct']:.1f}%"},
                {"항목": "TIME", "값": f"{custom['holding_days']}거래일"},
                {"항목": "Risk B", "값": "ON" if custom["enable_risk_b"] else "OFF"},
                {"항목": "TB3", "값": "ON" if custom["enable_tb3"] else "OFF"},
                {"항목": "급락 추가매수", "값": (f"-{custom['crash_pct']:.1f}% / +{custom['crash_extra_slots']}슬롯" if custom["enable_crash_buy"] else "OFF")},
            ])
        st.dataframe(pd.DataFrame(settings_rows), use_container_width=True, hide_index=True)


st.sidebar.markdown("<div class='redesign-nav-title'>메뉴</div>", unsafe_allow_html=True)
app_page = st.sidebar.radio(
    "화면 선택",
    ["대시보드", "백테스트 분석"],
    key="app_page_mode_redesign",
    label_visibility="collapsed",
)
st.sidebar.markdown("""<div class="redesign-side-links">
  <a href="#redesign-order-management">주문 관리</a>
  <a href="#redesign-slot-preview">보유 슬롯</a>
  <a href="#redesign-performance">성과 리포트</a>
  <a href="#redesign-analysis">심층 분석</a>
</div>""", unsafe_allow_html=True)
if app_page == "백테스트 분석":
    render_backtest_lab(full_df)
    st.stop()


# ============================================================
# SIDEBAR
# ============================================================

st.sidebar.markdown("<div class='soxl-side-section'>실전 운용</div>", unsafe_allow_html=True)


st.sidebar.caption("확정 일봉 데이터")


latest_data_date = (
    full_df[
        "Date"
    ]
    .max()
    .date()
)


st.sidebar.markdown(f"**{latest_data_date}**")


if _redesign_refresh_placeholder.button(
    "↻ 데이터 업데이트",
    use_container_width=True,
    type="primary",
    key="redesign_refresh",
):

    try:

        result = (
            update_soxl_data(
                "SOXL_adjusted.csv"
            )
        )


        refresh_data_cache()


        st.sidebar.success(
            result.get(
                "message",
                "업데이트 완료"
            )
        )


        st.rerun()

    except Exception as e:

        st.sidebar.error(
            str(e)
        )


st.sidebar.divider()


# ============================================================
# SETTINGS
# ============================================================

# 암호화 GitHub 저장값을 우선 사용하고, 기존 로컬 JSON은 1회 마이그레이션용 fallback으로만 사용합니다.
if "v9_persisted_state_loaded" not in st.session_state:
    persisted_state, persisted_status = _load_persisted_v9_state()
    st.session_state["v9_settings"] = persisted_state.get("settings", {})
    st.session_state["v9_quantity_overrides"] = persisted_state.get("quantities", {})
    st.session_state["v9_entry_price_overrides"] = persisted_state.get("entry_prices", {})
    st.session_state["v9_excluded_positions"] = persisted_state.get("excluded_positions", [])
    st.session_state["v9_cashflows"] = persisted_state.get("cashflows", [])
    st.session_state["v9_trade_overrides"] = persisted_state.get("trade_overrides", {})
    st.session_state["v9_manual_trades"] = persisted_state.get("manual_trades", [])
    st.session_state["v9_persistence_status"] = persisted_status
    st.session_state["v9_persisted_state_loaded"] = True

saved_settings = _sanitize_dashboard_settings(st.session_state.get("v9_settings", {}))
local_settings = load_dashboard_settings()
if not saved_settings.get("start_date") and local_settings.get("start_date"):
    saved_settings = _sanitize_dashboard_settings(local_settings)

saved_initial_capital = saved_settings.get("initial_capital", 100_000_000)
try:
    saved_initial_capital = int(saved_initial_capital)
except (TypeError, ValueError):
    saved_initial_capital = 100_000_000

saved_initial_capital = max(
    1_000_000,
    min(10_000_000_000, saved_initial_capital),
)

st.sidebar.caption("전략과 운용 기준은 상단에서 선택합니다")

strategy_mode = _redesign_strategy_placeholder.selectbox(
    "전략 선택",
    [
        "V11 Final(D2 -10% + 4일 연속 하락 방어)",
        "V10 Final(V9 + -9% 급락 추가매수)",
        "V9 Final(약세필터 + 하락추세 방어LOC)",
        "V8 장기과매도 약세필터",
        "V7 Base(기본 7분할)",
        "Custom(직접 설정)",
    ],
    index=0,
)

with st.sidebar.expander("⚙️ 운용 상세 설정", expanded=False):
    if strategy_mode.startswith("V11"):
        st.info("V11 실전 전환일: 2026-10-19 (미국 거래일). 이전 V10 보유 슬롯과 저장 수량·매수가를 유지하고, 이후 신규 주문에 V11 규칙을 적용합니다.")

    is_custom = strategy_mode.startswith("Custom")

    initial_capital = (
        st.number_input(

            "초기 투자금 (원)",

            min_value=
                1_000_000,

            max_value=
                10_000_000_000,

            value=
                saved_initial_capital,

            step=
                10_000_000,

            key=
                "initial_capital_input",

        )
    )


    position_divisor = (
        st.slider(
            "Split(분할 수)",
            2,
            20,
            7,
            disabled=not is_custom,
        )
    )


    max_positions = (
        st.slider(
            "Max Positions(최대 동시 보유 수)",
            1,
            20,
            7,
            disabled=not is_custom,
        )
    )


    target_percent = (
        st.slider(
            "LOC(종가 조건 주문) 목표수익률 (%)",
            0.5,
            15.0,
            2.7,
            0.1,
            disabled=not is_custom,
        )
    )


    holding_days = (
        st.slider(
            "TIME(기한 만료 매도) 보유기간",
            1,
            30,
            7,
            disabled=not is_custom,
        )
    )


    reserve_percent = (
        st.slider(
            "Reserve(남겨둘 현금) (%)",
            0.0,
            50.0,
            0.0,
            1.0,
            disabled=not is_custom,
        )
    )


st.sidebar.divider()


# ============================================================
# DATE RANGE
# ============================================================

min_date = (
    full_df[
        "Date"
    ]
    .min()
    .date()
)


max_date = (
    full_df[
        "Date"
    ]
    .max()
    .date()
)


saved_start_date = min_date
saved_start_raw = saved_settings.get("start_date")

if saved_start_raw:
    try:
        candidate_start = pd.Timestamp(saved_start_raw).date()
        if min_date <= candidate_start <= max_date:
            saved_start_date = candidate_start
    except (TypeError, ValueError):
        pass


start_date = (
    _redesign_start_placeholder.date_input(
        "운용 시작일",
        saved_start_date,
        min_value=
            min_date,
        max_value=
            max_date,
        key=
            "start_date_input",
    )
)


# 시작일 또는 초기 투자금이 바뀌면 암호화 GitHub 저장소에 자동 저장합니다.
# 로컬 JSON도 fallback/migration 용도로 함께 갱신합니다.
current_saved_start = saved_settings.get("start_date")
current_saved_capital = saved_settings.get("initial_capital")

if (
    current_saved_start != pd.Timestamp(start_date).date().isoformat()
    or str(current_saved_capital) != str(int(initial_capital))
):
    new_settings = {
        "start_date": pd.Timestamp(start_date).date().isoformat(),
        "initial_capital": int(initial_capital),
    }
    save_dashboard_settings(
        start_date_value=start_date,
        initial_capital_value=initial_capital,
    )
    if portfolio_persistence_enabled():
        ok_settings, msg_settings = save_persisted_dashboard_settings(new_settings)
        st.session_state["v9_settings_save_ok"] = ok_settings
        st.session_state["v9_settings_save_status"] = msg_settings
    else:
        st.session_state["v9_settings"] = _sanitize_dashboard_settings(new_settings)

if portfolio_persistence_enabled():
    st.sidebar.caption("🔐 시작일·초기 투자금은 GitHub 암호화 저장소에 영구 저장됩니다.")
else:
    st.sidebar.warning("시작일·초기 투자금은 현재 로컬 저장만 사용 중입니다. Secrets 설정을 확인하세요.")


end_date = (
    _redesign_end_placeholder.date_input(
        "분석 종료일",
        max_date,
        min_value=
            min_date,
        max_value=
            max_date,
    )
)


if start_date > end_date:

    st.error(
        "시작일이 종료일보다 늦습니다."
    )

    st.stop()


# ============================================================
# LIVE CASHFLOW MANAGEMENT (V9 ONLY)
# ============================================================

v9_cashflows = []
v9_cashflow_summary = {"deposits": 0.0, "withdrawals": 0.0, "net": 0.0}

if strategy_mode.startswith(("V9", "V10", "V11")):
    if "v9_cashflows" not in st.session_state:
        loaded_cashflows, cashflow_status = load_persisted_v9_cashflows()
        st.session_state["v9_cashflows"] = loaded_cashflows
        st.session_state["v9_cashflow_persistence_status"] = cashflow_status

    v9_cashflows = _sanitize_v9_cashflows(st.session_state.get("v9_cashflows", []))
    today_kst_for_cashflow = datetime.now(ZoneInfo("Asia/Seoul")).date()
    v9_cashflow_summary = summarize_v9_cashflows(v9_cashflows, today_kst_for_cashflow)

    with st.sidebar.expander("💰 추가 자금 입출금", expanded=False):
        st.caption(
            f"누적 입금 {v9_cashflow_summary['deposits']:,.0f}원 · "
            f"누적 출금 {v9_cashflow_summary['withdrawals']:,.0f}원 · "
            f"순입금 {v9_cashflow_summary['net']:+,.0f}원"
        )
        st.caption("추가 자금은 실전 현금·NAV·다음 신규매수 금액에만 반영되며 백테스트 CAGR/MDD에는 반영되지 않습니다.")

        flow_date = st.date_input(
            "입출금일",
            value=today_kst_for_cashflow,
            max_value=today_kst_for_cashflow,
            key="v9_cashflow_date",
        )
        flow_type = st.selectbox(
            "구분",
            ["입금", "출금"],
            key="v9_cashflow_type",
        )
        flow_amount = st.number_input(
            "금액 (원)",
            min_value=0,
            max_value=10_000_000_000,
            value=0,
            step=100_000,
            key="v9_cashflow_amount",
        )
        flow_memo = st.text_input(
            "메모 (선택)",
            placeholder="예: 9월 월급 여유자금",
            key="v9_cashflow_memo",
        )

        if st.button("💾 입출금 저장", use_container_width=True, key="save_v9_cashflow"):
            if int(flow_amount) <= 0:
                st.error("0원보다 큰 금액을 입력하세요.")
            else:
                new_flow = {
                    "id": datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d%H%M%S%f"),
                    "date": pd.Timestamp(flow_date).date().isoformat(),
                    "type": flow_type,
                    "amount": float(flow_amount),
                    "memo": flow_memo.strip(),
                }
                updated_flows = _sanitize_v9_cashflows(v9_cashflows + [new_flow])
                st.session_state["v9_cashflows"] = updated_flows
                ok, msg = save_persisted_v9_cashflows(updated_flows)
                st.session_state["v9_cashflow_persistence_ok"] = ok
                st.session_state["v9_cashflow_persistence_status"] = msg
                if ok:
                    st.rerun()
                else:
                    st.error(msg)

        if v9_cashflows:
            st.markdown("**최근 입출금 내역**")
            recent_flows = list(reversed(v9_cashflows[-8:]))
            recent_df = pd.DataFrame([
                {
                    "날짜": item["date"],
                    "구분": item["type"],
                    "금액": f"{float(item['amount']):,.0f}원",
                    "메모": item.get("memo", ""),
                }
                for item in recent_flows
            ])
            st.dataframe(recent_df, use_container_width=True, hide_index=True, height=min(320, max(160, 36 * (len(recent_df) + 1))))

            delete_options = {
                f"{item['date']} · {item['type']} · {float(item['amount']):,.0f}원"
                + (f" · {item.get('memo','')}" if item.get('memo') else ""): item["id"]
                for item in recent_flows
            }
            delete_label = st.selectbox(
                "삭제할 내역",
                list(delete_options.keys()),
                key="v9_cashflow_delete_select",
            )
            if st.button("🗑️ 선택 내역 삭제", use_container_width=True, key="delete_v9_cashflow"):
                delete_id = delete_options[delete_label]
                updated_flows = [x for x in v9_cashflows if x.get("id") != delete_id]
                st.session_state["v9_cashflows"] = updated_flows
                ok, msg = save_persisted_v9_cashflows(updated_flows)
                st.session_state["v9_cashflow_persistence_ok"] = ok
                st.session_state["v9_cashflow_persistence_status"] = msg
                if ok:
                    st.rerun()
                else:
                    st.error(msg)

        persistence_msg = st.session_state.get("v9_cashflow_persistence_status", "")
        if portfolio_persistence_enabled():
            st.caption(f"🔐 암호화 영구저장 · {persistence_msg or '연결됨'}")
        else:
            st.warning("Streamlit Secrets의 GitHub/암호화 설정을 확인하세요.")


df = full_df[
    (
        full_df[
            "Date"
        ].dt.date
        >= start_date
    )
    &
    (
        full_df[
            "Date"
        ].dt.date
        <= end_date
    )
].copy()


df = (
    df.reset_index(
        drop=True
    )
)


# ============================================================
# BACKTEST(과거검증)
# ============================================================

base_result = run_backtest(
    df=df,
    initial_capital=initial_capital,
    position_divisor=BASE_POSITION_DIVISOR,
    max_positions=BASE_MAX_POSITIONS,
    target_return=BASE_TARGET_RETURN,
    holding_days=BASE_HOLDING_DAYS,
    reserve_ratio=BASE_RESERVE_RATIO,
)

# V8: 평소 NAV/7, Risk B에서는 신규매수만 쉬기
v8_raw_result = run_risk_divisor_backtest(
    raw_df=df,
    risk_divisor=None,
    initial_capital=initial_capital,
)

v8_result = add_result_tables(
    v8_raw_result,
    initial_capital,
)

# V9: 전체 이력으로 지표를 먼저 계산한 뒤 선택 기간만 백테스트합니다.
v9_raw_result = run_v9_backtest(
    raw_df=full_df,
    initial_capital=initial_capital,
    start_date=start_date,
    end_date=end_date,
)
v9_result = add_result_tables(v9_raw_result, initial_capital)

# V10: V9 + 종가가 전일 대비 -9% 이하이면 같은 종가에 추가 1슬롯 매수
v10_raw_result = run_v10_backtest(
    raw_df=full_df,
    initial_capital=initial_capital,
    start_date=start_date,
    end_date=end_date,
)
v10_result = add_result_tables(v10_raw_result, initial_capital)

v11_raw_result = run_v11_backtest(raw_df=full_df, initial_capital=initial_capital, start_date=start_date, end_date=end_date)
v11_result = add_result_tables(v11_raw_result, initial_capital)

if strategy_mode.startswith("V11"):
    current_result = v11_result
    strategy_short_name = "V11 Final(D2 -10% + 4일 연속 하락 방어)"
elif strategy_mode.startswith(("V10", "V11")):
    current_result = v10_result
    strategy_short_name = "V10 Final(V9 + -9% 급락 추가매수)"

elif strategy_mode.startswith("V9"):
    current_result = v9_result
    strategy_short_name = "V9 Final(약세필터 + 하락추세 방어LOC)"

elif strategy_mode.startswith("V8"):
    current_result = v8_result
    strategy_short_name = "V8 장기과매도 약세필터"

elif strategy_mode.startswith("V7"):
    current_result = base_result
    strategy_short_name = "V7 Base(기본 전략)"

else:
    current_result = run_backtest(
        df=df,
        initial_capital=initial_capital,
        position_divisor=position_divisor,
        max_positions=max_positions,
        target_return=target_percent / 100,
        holding_days=holding_days,
        reserve_ratio=reserve_percent / 100,
    )
    strategy_short_name = "Custom(직접 설정)"

stats = current_result["stats"]
base_stats = base_result["stats"]
trades = current_result["trades"]

# 비교용 전략 결과를 한곳에서 관리합니다.
comparison_results = {
    "V7 Base(기본 전략)": base_result,
    "V8 장기과매도 약세필터": v8_result,
    "V9 Final(약세필터 + 하락추세 방어LOC)": v9_result,
    "V10 Final(V9 + -9% 급락 추가매수)": v10_result,
    "V11 Final(D2 -10% + 4일 연속 하락 방어)": v11_result,
}
if strategy_short_name.startswith("Custom"):
    comparison_results[strategy_short_name] = current_result

# 현재 선택 전략과 다른 전략을 기본 비교 대상으로 사용합니다.
comparison_candidates = [
    name for name in comparison_results.keys()
    if name != strategy_short_name
]
if strategy_mode.startswith(("V10", "V11")) and "V9 Final(약세필터 + 하락추세 방어LOC)" in comparison_candidates:
    default_comparison_name = "V9 Final(약세필터 + 하락추세 방어LOC)"
elif strategy_mode.startswith("V9") and "V10 Final(V9 + -9% 급락 추가매수)" in comparison_candidates:
    default_comparison_name = "V10 Final(V9 + -9% 급락 추가매수)"
elif strategy_mode.startswith("V8") and "V9 Final(약세필터 + 하락추세 방어LOC)" in comparison_candidates:
    default_comparison_name = "V9 Final(약세필터 + 하락추세 방어LOC)"
elif comparison_candidates:
    default_comparison_name = comparison_candidates[0]
else:
    default_comparison_name = strategy_short_name

# 실전 주문 화면은 백테스트 날짜 선택과 분리합니다.
# 미국 동부시간을 기준으로 현재/다음 주문 대상 세션을 정하고,
# 그 세션 직전의 확정 일봉까지만 신호에 사용할 수 있습니다.
live_context = get_live_market_context(latest_data_date)
today_kst = live_context["today_kst"]
ny_now = live_context["ny_now"]
us_today = live_context["us_today"]
us_is_trading_day = live_context["us_is_trading_day"]
session_status = live_context["session_status"]
action_session = live_context["action_session"]
required_signal_date = live_context["required_signal_date"]
missing_sessions = live_context["missing_sessions"]

latest_confirmed_date = pd.Timestamp(full_df["Date"].max()).normalize()

# 데이터가 하루 정도 늦어도 주문을 강제로 막지 않습니다.
# 다만 미래정보를 쓰지 않도록 주문 대상 세션의 직전 거래일 이하 데이터 중
# 가장 최근에 실제로 보유한 확정 일봉을 신호 기준일로 사용합니다.
all_signals = build_signal_table(full_df)
eligible_signals = all_signals[
    all_signals["Date"].dt.normalize() <= required_signal_date
].copy()

latest_risk_state = False
latest_tb3_state = False
latest_ma_gap = None
latest_momentum = None
latest_ma5 = None
latest_ma20 = None
latest_ma50 = None
latest_signal_close = None
signal_ready = False
signal_source_date = None

if not eligible_signals.empty:
    latest_signal = eligible_signals.iloc[-1]
    signal_source_date = pd.Timestamp(latest_signal["Date"]).normalize()
    latest_ma_gap = latest_signal["MA200_Gap"]
    latest_momentum = latest_signal["Momentum_20D"]
    latest_ma5 = latest_signal["MA5"]
    latest_ma20 = latest_signal["MA20"]
    latest_ma50 = latest_signal["MA50"]
    latest_signal_close = float(latest_signal["Close"]) if pd.notna(latest_signal["Close"]) else None
    signal_ready = (
        pd.notna(latest_ma_gap)
        and pd.notna(latest_momentum)
        and pd.notna(latest_ma5)
        and pd.notna(latest_ma20)
        and pd.notna(latest_ma50)
    )
    if signal_ready:
        latest_risk_state = is_risk_b(latest_ma_gap, latest_momentum)
        latest_tb3_state = bool(latest_ma5 < latest_ma20 < latest_ma50) and not strategy_mode.startswith("V11")

# V11 buy pause is based on consecutive down closes observed BEFORE the order session.
v11_down_streak = 0
if not eligible_signals.empty:
    closes = pd.to_numeric(eligible_signals["Close"], errors="coerce").dropna()
    for idx in range(len(closes) - 1, 0, -1):
        if closes.iloc[idx] < closes.iloc[idx - 1]:
            v11_down_streak += 1
        else:
            break
v11_pause_now = strategy_mode.startswith("V11") and v11_down_streak >= 4

# 2거래일 이상 뒤처지면 경고하되, 가장 최근 확정 데이터로 신호는 계속 계산합니다.
data_delay_warning = missing_sessions >= 2

# 실전 포트폴리오는 사용자가 선택한 시작일과 초기 투자금으로 실제 운용을
# 시작했다고 가정해 최신 확정 데이터까지 다시 계산합니다.
# 백테스트 종료일(end_date)은 과거검증 화면에만 사용하고, 오늘 할 일은 항상 최신일까지 봅니다.
live_df = full_df[
    full_df["Date"].dt.date >= start_date
].copy().reset_index(drop=True)

if live_df.empty:
    st.error("선택한 시작일 이후의 가격 데이터가 없습니다.")
    st.stop()

live_close = float(full_df.iloc[-1]["Close"])

if strategy_mode.startswith("V11"):
    live_result = run_v11_live_transition(raw_df=full_df, initial_capital=initial_capital, start_date=start_date, end_date=latest_confirmed_date, transition_date="2026-10-19")
    live_cash = float(live_result["equity"]["Cash"].iloc[-1])
    live_open_count = len(live_result.get("open_positions", []))
elif strategy_mode.startswith(("V10", "V11")):
    live_result = run_v10_backtest(
        raw_df=full_df,
        initial_capital=initial_capital,
        start_date=start_date,
        end_date=latest_confirmed_date,
    )
    live_cash = float(live_result["equity"]["Cash"].iloc[-1])
    live_open_count = len(live_result.get("open_positions", []))
elif strategy_mode.startswith("V9"):
    live_result = run_v9_backtest(
        raw_df=full_df,
        initial_capital=initial_capital,
        start_date=start_date,
        end_date=latest_confirmed_date,
    )
    live_cash = float(live_result["equity"]["Cash"].iloc[-1])
    live_open_count = len(live_result.get("open_positions", []))
elif strategy_mode.startswith("V8"):
    live_result = run_live_v8_portfolio(
        full_price_df=full_df,
        start_date=start_date,
        initial_capital=initial_capital,
    )
    live_cash = float(live_result["cash"])
    live_open_count = int(live_result["open_positions"])
else:
    if strategy_mode.startswith("V7"):
        live_result = run_backtest(
            df=live_df,
            initial_capital=initial_capital,
            position_divisor=BASE_POSITION_DIVISOR,
            max_positions=BASE_MAX_POSITIONS,
            target_return=BASE_TARGET_RETURN,
            holding_days=BASE_HOLDING_DAYS,
            reserve_ratio=BASE_RESERVE_RATIO,
        )
    else:
        live_result = run_backtest(
            df=live_df,
            initial_capital=initial_capital,
            position_divisor=position_divisor,
            max_positions=max_positions,
            target_return=target_percent / 100,
            holding_days=holding_days,
            reserve_ratio=reserve_percent / 100,
        )

    live_cash, live_open_count = reconstruct_live_cash_and_open_count(
        live_result,
        current_close=live_close,
    )

live_trades = live_result["trades"]
model_live_nav = float(live_result["equity"]["Equity"].iloc[-1])

# 주문 대상 미국 거래일 기준으로 보유 슬롯의 MOC 기한을 보여줍니다.
position_reference_date = action_session

# V9 실전 운용에서는 사용자가 입력한 실제 보유수량을 현금/NAV/다음 주문금액에 반영합니다.
portfolio_fx_snapshot = get_usdkrw_realtime_snapshot() if strategy_mode.startswith(("V9", "V10", "V11")) else {"rate": None}
portfolio_fallback_fx = portfolio_fx_snapshot.get("rate")
v9_fx_daily = pd.DataFrame(columns=["Date", "USD_KRW"])
v9_quantity_overrides = {}
v9_entry_price_overrides = st.session_state.get("v9_entry_price_overrides", {})
v9_trade_overrides = st.session_state.get("v9_trade_overrides", {})
v9_manual_trades = _sanitize_v9_manual_trades(st.session_state.get("v9_manual_trades", []))
realized_execution_adjustment = 0.0
manual_realized_profit = 0.0
net_external_cashflow = 0.0

if strategy_mode.startswith(("V9", "V10", "V11")):
    if "v9_trade_overrides" not in st.session_state:
        loaded_trade_overrides, trade_override_status = load_persisted_v9_trade_overrides()
        st.session_state["v9_trade_overrides"] = loaded_trade_overrides
        st.session_state["v9_trade_persistence_status"] = trade_override_status
    v9_trade_overrides = _sanitize_v9_trade_overrides(
        st.session_state.get("v9_trade_overrides", {})
    )
    v9_entry_price_overrides = _sanitize_v9_entry_prices(
        st.session_state.get("v9_entry_price_overrides", {})
    )
    v9_open_positions = live_result.get("open_positions", [])
    if v9_open_positions:
        earliest_entry = min(pd.Timestamp(p["entry_date"]) for p in v9_open_positions)
        latest_entry = max(pd.Timestamp(p["entry_date"]) for p in v9_open_positions)
        v9_fx_daily = get_usdkrw_daily_rates(earliest_entry, latest_entry)
    v9_quantity_overrides = get_v9_quantity_overrides(
        v9_open_positions,
        fx_daily=v9_fx_daily,
        fallback_fx=portfolio_fallback_fx,
    )
    actual_portfolio = calculate_v9_actual_portfolio(
        v9_open_positions,
        model_cash=live_cash,
        confirmed_close=live_close,
        qty_overrides=v9_quantity_overrides,
        entry_price_overrides=v9_entry_price_overrides,
        excluded_positions=st.session_state.get("v9_excluded_positions", []),
        fx_daily=v9_fx_daily,
        fallback_fx=portfolio_fallback_fx,
    )
    # 사용자가 수정·저장한 과거 청산거래의 실제 체결 손익 차이도 실전 현금/NAV에 반영합니다.
    # 백테스트 통계는 순수 V9 그대로 유지합니다.
    if v9_trade_overrides and not live_result.get("trades", pd.DataFrame()).empty:
        override_dates = []
        for item in v9_trade_overrides.values():
            try:
                override_dates.append(pd.Timestamp(item.get("entry_date")))
            except Exception:
                pass
        trade_model_dates = pd.to_datetime(live_result["trades"]["Entry_Date"], errors="coerce").dropna()
        if not trade_model_dates.empty:
            fx_start = min(override_dates + [trade_model_dates.min()]) if override_dates else trade_model_dates.min()
            fx_end = max(override_dates + [trade_model_dates.max()]) if override_dates else trade_model_dates.max()
            execution_fx_daily = get_usdkrw_daily_rates(fx_start, fx_end)
        else:
            execution_fx_daily = v9_fx_daily
        realized_execution_adjustment = calculate_v9_realized_execution_adjustment(
            live_result["trades"],
            trade_overrides=v9_trade_overrides,
            qty_overrides=v9_quantity_overrides,
            fx_daily=execution_fx_daily,
            fallback_fx=portfolio_fallback_fx,
            excluded_positions=st.session_state.get("v9_excluded_positions", []),
        )

    # 전략에 없던 수동 추가 거래는 'NAV 반영'으로 저장한 경우 실현손익을 실전 NAV에 추가합니다.
    # 기존 전략 거래와 중복되는 거래를 수동 추가하면 이중 반영될 수 있으므로 수동 추가는 전략표에 없는 거래에만 사용합니다.
    if v9_manual_trades:
        manual_dates = [pd.Timestamp(x["entry_date"]) for x in v9_manual_trades]
        manual_fx = get_usdkrw_daily_rates(min(manual_dates), max(manual_dates)) if manual_dates else v9_fx_daily
        manual_realized_profit = calculate_v9_manual_realized_profit(
            v9_manual_trades,
            fx_daily=manual_fx,
            fallback_fx=portfolio_fallback_fx,
        )

    # 추가 입출금은 백테스트 엔진과 분리해 실전 현금/NAV에만 더합니다.
    # 실제수량·실제 체결 보정과 함께 다음 신규매수 금액(NAV/7)의 기준이 됩니다.
    net_external_cashflow = float(v9_cashflow_summary.get("net", 0.0))
    live_cash = (
        float(actual_portfolio["cash"])
        + realized_execution_adjustment
        + manual_realized_profit
        + net_external_cashflow
    )
    live_nav = (
        float(actual_portfolio["nav"])
        + realized_execution_adjustment
        + manual_realized_profit
        + net_external_cashflow
    )

    # 실제 운용 슬롯 수는 전략 엔진의 open_positions 개수가 아니라
    # 사용자가 실제로 보유 중인 슬롯(실제수량 > 0)만 셉니다.
    # 따라서 '미매수 처리' 슬롯과 실제 보유수량 0 슬롯은
    # 오늘 할 일 / 신규매수 가능 슬롯 계산에서 빈 슬롯으로 취급합니다.
    actual_positions_df = actual_portfolio.get("positions", pd.DataFrame())
    if isinstance(actual_positions_df, pd.DataFrame) and not actual_positions_df.empty:
        live_open_count = int(
            (pd.to_numeric(actual_positions_df["Actual_Qty"], errors="coerce").fillna(0) > 0).sum()
        )
    else:
        live_open_count = 0

    open_positions_display = build_v9_open_positions_table(
        v9_open_positions,
        current_close=live_close,
        as_of_date=position_reference_date,
        tb3_state=latest_tb3_state,
        holding_days=BASE_HOLDING_DAYS,
        fx_daily=v9_fx_daily,
        fallback_fx=portfolio_fallback_fx,
        qty_overrides=v9_quantity_overrides,
        entry_price_overrides=v9_entry_price_overrides,
        excluded_positions=st.session_state.get("v9_excluded_positions", []),
    )
    if strategy_mode.startswith("V11") and not open_positions_display.empty:
        pending = {v9_position_key(p) for p in v9_open_positions if p.get("d2_flag", False)}
        if "_key" in open_positions_display.columns and "상태" in open_positions_display.columns:
            flagged = open_positions_display["_key"].isin(pending)
            open_positions_display.loc[flagged, "상태"] = "MOC(종가 매도) 오늘 예정"
            if "남은 거래일" in open_positions_display.columns:
                open_positions_display.loc[flagged, "남은 거래일"] = "오늘"
else:
    live_nav = model_live_nav
    open_positions_display = build_open_positions_table(
        live_trades,
        current_close=live_close,
        as_of_date=position_reference_date,
        holding_days=(BASE_HOLDING_DAYS if not is_custom else holding_days),
    )

# 실제 신규매수 행동은 시장 신호 + 현재 포트폴리오의 슬롯/현금을 함께 봅니다.
max_slots_for_live = max_positions if is_custom else BASE_MAX_POSITIONS
divisor_for_next = position_divisor if is_custom else BASE_POSITION_DIVISOR
reserve_ratio_for_live = (reserve_percent / 100) if is_custom else BASE_RESERVE_RATIO
reserve_floor = live_nav * reserve_ratio_for_live
usable_cash = max(0.0, live_cash - reserve_floor)

# V10 추가 LOC 주문 계획: 전일 확정 종가의 91%에 LOC를 걸고,
# 당일 종가가 그 가격 이하로 마감되면 해당 종가에 추가 1슬롯이 체결된 것으로 다음날 반영합니다.
v10_crash_loc_price = (
    float(latest_signal_close) * V10_CRASH_LOC_MULTIPLIER
    if strategy_mode.startswith(("V10", "V11")) and latest_signal_close is not None
    else None
)
v10_base_buy_amount = 0.0
v10_extra_buy_amount = 0.0
v10_available_slots = max(0, max_slots_for_live - live_open_count)

if not signal_ready:
    next_action = "CHECK DATA(데이터 확인 필요)"
    next_buy_amount = 0.0
    market_state_text = "DATA CHECK(데이터 확인 필요)"
elif v11_pause_now:
    next_action = "SKIP(4일 연속 하락 · 신규매수 유예)"
    next_buy_amount = 0.0
    market_state_text = f"V11 방어: {v11_down_streak}일 연속 하락 · 신규매수 유예"
elif (strategy_mode.startswith("V8") or strategy_mode.startswith(("V9", "V10", "V11"))) and latest_risk_state:
    next_action = "SKIP(신규매수 쉬기)"
    next_buy_amount = 0.0
    market_state_text = (
        "장기과매도 약세 + 단기하락추세(신규매수 중단 · 방어 LOC)"
        if strategy_mode.startswith(("V9", "V10", "V11")) and latest_tb3_state
        else "장기과매도 약세구간(신규매수 중단)"
    )
elif live_open_count >= max_slots_for_live:
    next_action = "WAIT(보유 슬롯 가득 참)"
    next_buy_amount = 0.0
    market_state_text = (
        "단기하락추세(방어 LOC 적용)" if strategy_mode.startswith(("V9", "V10", "V11")) and latest_tb3_state
        else "NORMAL(정상 매수 구간)"
    )
elif usable_cash <= 0:
    next_action = "WAIT(사용 가능한 현금 없음)"
    next_buy_amount = 0.0
    market_state_text = (
        "단기하락추세(방어 LOC 적용)" if strategy_mode.startswith(("V9", "V10", "V11")) and latest_tb3_state
        else "NORMAL(정상 매수 구간)"
    )
else:
    target_buy_amount = live_nav / divisor_for_next
    next_buy_amount = min(target_buy_amount, usable_cash)
    next_action = "BUY(신규매수)" if next_buy_amount > 0 else "WAIT(신규매수 대기)"
    market_state_text = (
        "단기하락추세(방어 LOC 적용)" if strategy_mode.startswith(("V9", "V10", "V11")) and latest_tb3_state
        else "NORMAL(정상 매수 구간)"
    )

# V10에서는 기본 MOC 1슬롯과 급락 LOC 추가 1슬롯을 별도 주문으로 표시합니다.
# 최대 7슬롯과 현재 가용현금 한도를 동시에 적용합니다.
if strategy_mode.startswith(("V10", "V11")) and next_action.startswith("BUY"):
    one_slot_target = live_nav / BASE_POSITION_DIVISOR
    if v10_available_slots >= 1 and usable_cash > 0:
        v10_base_buy_amount = min(one_slot_target, usable_cash)
    cash_after_base = max(0.0, usable_cash - v10_base_buy_amount)
    if v10_available_slots >= 2 and cash_after_base > 0:
        v10_extra_buy_amount = min(one_slot_target, cash_after_base)

moc_today_count = 0
if action_session is not None and not open_positions_display.empty:
    moc_today_count = int(
        open_positions_display["상태"]
        .eq("MOC(종가 매도) 오늘 예정")
        .sum()
    )

# ============================================================
# ANALYSIS
# ============================================================

time_analysis = (
    analyze_time_exits(
        trades,
        df,
    )
)


market_analysis = (
    analyze_market_regime_v2(
        trades,
        full_df,
    )
)


# ============================================================
# HEADER is consolidated into the first-screen overview.
# ============================================================
# ============================================================
# TODAY'S ACTION + LIVE STATUS
# ============================================================


# Compact order-status rendering lives in the lower left panel; avoid a duplicate heading here.


# 행동 문구는 기존 전략 계산 결과를 그대로 사용합니다.
if next_action.startswith("BUY"):
    action_icon = "🟢"
    action_word = "BUY"
    if strategy_mode.startswith(("V10", "V11")):
        action_title = "기본 MOC 1슬롯 + 급락 LOC 대기"
        action_amount_text = f"기본 {v10_base_buy_amount:,.0f}원"
        crash_price_text = f"${v10_crash_loc_price:,.2f}" if v10_crash_loc_price is not None else "계산 필요"
        if v10_extra_buy_amount > 0:
            action_reason = (
                f"추가 LOC {crash_price_text} · 종가가 해당 가격 이하이면 "
                f"추가 {v10_extra_buy_amount:,.0f}원 1슬롯 체결 · 최대 {max_slots_for_live}슬롯"
            )
        else:
            action_reason = (
                f"기본 1슬롯 매수 · 추가 LOC {crash_price_text}는 "
                "빈 슬롯 또는 현금 부족으로 주문 불가"
            )
    else:
        action_title = "오늘 신규매수"
        action_amount_text = f"{next_buy_amount:,.0f}원"
        if strategy_mode.startswith("V9") and latest_tb3_state:
            action_reason = f"단기하락추세 · 신규매수는 계속 · 확정 NAV의 1/{divisor_for_next} · 보유 티어 방어 LOC 적용"
        else:
            action_reason = f"NORMAL(정상 매수 구간) · 확정 NAV의 1/{divisor_for_next}"
    action_class = "action-buy"
elif next_action.startswith("SKIP"):
    action_icon = "🟠"
    action_word = "SKIP"
    action_title = "오늘 신규매수 없음"
    action_amount_text = "0원"
    action_reason = "장기과매도 약세구간(신규매수 중단) · 기존 매도 규칙 유지"
    action_class = "action-skip"
elif next_action.startswith("CHECK DATA"):
    action_icon = "🔴"
    action_word = "CHECK DATA"
    action_title = "데이터 확인 필요"
    action_amount_text = "신규매수 보류"
    action_reason = "신호 계산에 필요한 확정 일봉을 확인하세요."
    action_class = "action-check"
elif live_open_count >= max_slots_for_live:
    action_icon = "🔵"
    action_word = "WAIT"
    action_title = "신규매수 없음"
    action_amount_text = f"{live_open_count} / {max_slots_for_live} 슬롯"
    action_reason = "현재 보유 슬롯이 모두 사용 중입니다."
    action_class = "action-wait"
elif usable_cash <= 0:
    action_icon = "🔵"
    action_word = "WAIT"
    action_title = "신규매수 없음"
    action_amount_text = "사용 가능 현금 0원"
    action_reason = "현재 신규매수에 사용할 수 있는 현금이 없습니다."
    action_class = "action-wait"
else:
    action_icon = "🔵"
    action_word = "WAIT"
    action_title = "신규매수 대기"
    action_amount_text = "0원"
    action_reason = "현재 조건에서는 신규매수를 진행하지 않습니다."
    action_class = "action-wait"

st.markdown(
    """
    <style>
    .compact-card {
        border: 1px solid rgba(128,128,128,0.20);
        border-radius: 16px;
        padding: 1.15rem 1.25rem;
        min-height: 205px;
        background: rgba(128,128,128,0.045);
        margin-bottom: 0.35rem;
    }
    .compact-card.action-buy { border-left: 7px solid #20a464; }
    .compact-card.action-skip { border-left: 7px solid #f0a23b; }
    .compact-card.action-wait { border-left: 7px solid #4b8fd8; }
    .compact-card.action-check { border-left: 7px solid #db5b5b; }
    .compact-card.live-card { border-left: 7px solid #7b61d1; }
    .card-kicker {
        font-size: 0.84rem;
        font-weight: 700;
        opacity: 0.72;
        margin-bottom: 0.55rem;
        letter-spacing: 0.02em;
    }
    .action-word {
        font-size: 1.55rem;
        font-weight: 850;
        line-height: 1.0;
        margin-bottom: 0.25rem;
    }
    .action-subtitle {
        font-size: 0.96rem;
        font-weight: 650;
        margin-bottom: 0.75rem;
    }
    .action-main-value {
        font-size: 2.05rem;
        font-weight: 850;
        line-height: 1.05;
        margin-bottom: 0.55rem;
    }
    .card-note {
        font-size: 0.88rem;
        opacity: 0.72;
        line-height: 1.4;
    }
    .live-price-row {
        display: flex;
        align-items: baseline;
        gap: 0.75rem;
        margin-bottom: 0.8rem;
    }
    .live-price {
        font-size: 2.05rem;
        font-weight: 850;
        line-height: 1.05;
    }
    .live-change {
        font-size: 1.0rem;
        font-weight: 750;
    }
    .live-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 0.55rem 1.2rem;
        margin-top: 0.2rem;
    }
    .live-label {
        font-size: 0.78rem;
        opacity: 0.62;
        margin-bottom: 0.05rem;
    }
    .live-value {
        font-size: 1.05rem;
        font-weight: 750;
    }
    .action-qty {
        display: inline-block;
        padding: 0.32rem 0.60rem;
        margin: -0.10rem 0 0.60rem 0;
        border-radius: 8px;
        background: rgba(128,128,128,0.10);
        font-size: 0.94rem;
        font-weight: 750;
    }
    .signal-card {
        border: 1px solid rgba(128,128,128,0.22);
        border-radius: 14px;
        padding: 0.90rem 1.05rem;
        margin: 0.20rem 0 0.60rem 0;
        min-height: 112px;
        background: rgba(128,128,128,0.045);
    }
    .signal-card.signal-on-danger { border-left: 7px solid #e05252; background: rgba(224,82,82,0.075); }
    .signal-card.signal-on-warning { border-left: 7px solid #f0a23b; background: rgba(240,162,59,0.075); }
    .signal-card.signal-off { border-left: 7px solid #20a464; }
    .signal-title { font-size: 0.92rem; font-weight: 750; opacity: 0.78; margin-bottom: 0.25rem; }
    .signal-status { font-size: 1.28rem; font-weight: 850; margin-bottom: 0.20rem; }
    .signal-detail { font-size: 0.80rem; opacity: 0.66; line-height: 1.35; }
    .status-strip {
        padding: 0.58rem 0.75rem;
        margin: 0.55rem 0 0.4rem 0;
        border-radius: 10px;
        background: rgba(128,128,128,0.055);
        font-size: 0.86rem;
        line-height: 1.45;
        overflow-wrap: anywhere;
    }
    .v10-crash-card {
        border: 1px solid rgba(52, 124, 220, 0.22);
        border-left: 7px solid #347cdc;
        border-radius: 14px;
        padding: 0.95rem 1.10rem 0.90rem 1.10rem;
        margin: 0.55rem 0 0.75rem 0;
        background: rgba(52, 124, 220, 0.07);
    }
    .v10-crash-head {
        display: flex;
        justify-content: space-between;
        align-items: center;
        gap: 0.8rem;
        margin-bottom: 0.72rem;
    }
    .v10-crash-title {
        font-size: 1.02rem;
        font-weight: 850;
    }
    .v10-crash-badge {
        display: inline-block;
        padding: 0.23rem 0.55rem;
        border-radius: 999px;
        background: rgba(52, 124, 220, 0.13);
        font-size: 0.78rem;
        font-weight: 800;
        white-space: nowrap;
    }
    .v10-crash-grid {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 0.55rem;
        margin-bottom: 0.65rem;
    }
    .v10-crash-metric {
        padding: 0.62rem 0.72rem;
        border-radius: 10px;
        background: rgba(255,255,255,0.58);
        border: 1px solid rgba(128,128,128,0.12);
        min-width: 0;
    }
    .v10-crash-label {
        font-size: 0.75rem;
        opacity: 0.64;
        margin-bottom: 0.12rem;
    }
    .v10-crash-value {
        font-size: 1.16rem;
        font-weight: 850;
        line-height: 1.2;
        overflow-wrap: anywhere;
    }
    .v10-crash-note {
        font-size: 0.82rem;
        opacity: 0.72;
        line-height: 1.42;
    }

    @media (max-width: 900px) {
        .compact-card {
            min-height: auto;
            padding: 0.85rem 0.9rem;
        }
        .action-word { font-size: 1.42rem; }
        .action-main-value { font-size: 1.52rem; }
        .live-price { font-size: 1.58rem; }
        .live-grid { grid-template-columns: 1fr; gap: 0.45rem; }
        .signal-card { min-height: auto; }
        .status-strip { font-size: 0.78rem; }
        .v10-crash-grid { grid-template-columns: 1fr 1fr; }
        .v10-crash-value { font-size: 1.05rem; }
    }

    @media (max-width: 640px) {
        .action-word { font-size: 1.28rem; }
        .action-main-value { font-size: 1.35rem; }
        .live-price-row { align-items: flex-start; flex-direction: column; gap: 0.2rem; }
        .live-price { font-size: 1.45rem; }
        .signal-status { font-size: 1.1rem; }
        .v10-crash-head { align-items: flex-start; flex-direction: column; gap: 0.35rem; }
        .v10-crash-grid { grid-template-columns: 1fr 1fr; }
        .v10-crash-metric { padding: 0.52rem 0.60rem; }
        .v10-crash-value { font-size: 0.98rem; }
    }
    </style>
    """,
    unsafe_allow_html=True,
)

@st.fragment(run_every="5m")
def render_compact_trading_dashboard():
    # 실시간 가격은 평가손익 표시 전용입니다.
    snapshot = get_soxl_realtime_snapshot(live_close)
    realtime_price = float(snapshot["price"])

    # MOC 체결가는 장 마감 전 확정할 수 없으므로 현재 SOXL 참고시세와
    # USD/KRW 참고환율을 이용해 예상 주문수량을 표시합니다.
    fx_snapshot = get_usdkrw_realtime_snapshot()
    realtime_fx = fx_snapshot.get("rate")
    estimated_buy_qty = (
        next_buy_amount / (realtime_price * float(realtime_fx))
        if next_buy_amount > 0 and realtime_price > 0
        and realtime_fx is not None and float(realtime_fx) > 0
        else 0.0
    )
    action_quantity_text = (
        f"예상 {estimated_buy_qty:,.0f}주 · 환율 {float(realtime_fx):,.2f}원/$"
        if estimated_buy_qty > 0
        else ("환율 조회 필요" if next_buy_amount > 0 else "-")
    )

    v10_extra_estimated_qty = 0.0
    if (
        strategy_mode.startswith(("V10", "V11"))
        and v10_extra_buy_amount > 0
        and v10_crash_loc_price is not None
        and v10_crash_loc_price > 0
        and realtime_fx is not None
        and float(realtime_fx) > 0
    ):
        v10_extra_estimated_qty = (
            v10_extra_buy_amount / (v10_crash_loc_price * float(realtime_fx))
        )

    open_df = build_live_open_holdings_df(
        strategy_mode,
        live_result,
        live_trades,
        qty_overrides=v9_quantity_overrides,
        entry_price_overrides=v9_entry_price_overrides,
        excluded_positions=st.session_state.get("v9_excluded_positions", []),
        fx_daily=v9_fx_daily,
        fallback_fx=portfolio_fallback_fx,
    )

    if open_df.empty:
        invested_open = 0.0
        realtime_position_value = 0.0
        unrealized_pl = 0.0
        unrealized_return = 0.0
    else:
        invested_open = float(open_df["Invested"].sum())
        realtime_position_value = float(
            (
                open_df["Invested"]
                * realtime_price
                / open_df["Entry_Price"]
            ).sum()
        )
        unrealized_pl = realtime_position_value - invested_open
        unrealized_return = (
            unrealized_pl / invested_open
            if invested_open > 0
            else 0.0
        )

    realtime_nav = float(live_cash) + realtime_position_value

    change_text = (
        f"{snapshot['change_pct']:+.2%}"
        if snapshot["change_pct"] is not None
        else "-"
    )
    pl_sign = "+" if unrealized_pl > 0 else ""
    ret_sign = "+" if unrealized_return > 0 else ""

    left, right = st.columns(2, gap="medium")

    with left:
        st.markdown(
            f"""
            <div class="compact-card {action_class}">
                <div class="card-kicker">TODAY'S ACTION(오늘 주문)</div>
                <div class="action-word">{action_icon} {action_word}</div>
                <div class="action-subtitle">{action_title}</div>
                <div class="action-main-value">{action_amount_text}</div>
                <div class="action-qty">주문수량 · {action_quantity_text}</div>
                <div class="card-note">{action_reason}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with right:
        st.markdown(
            f"""
            <div class="compact-card live-card">
                <div class="card-kicker">⚡ LIVE STATUS(실시간 현황)</div>
                <div class="live-price-row">
                    <div class="live-price">SOXL ${realtime_price:,.2f}</div>
                    <div class="live-change">{change_text}</div>
                </div>
                <div class="live-grid">
                    <div>
                        <div class="live-label">평가손익</div>
                        <div class="live-value">{pl_sign}{unrealized_pl:,.0f}원</div>
                    </div>
                    <div>
                        <div class="live-label">평가수익률</div>
                        <div class="live-value">{ret_sign}{unrealized_return:.2%}</div>
                    </div>
                    <div>
                        <div class="live-label">보유 평가액</div>
                        <div class="live-value">{realtime_position_value:,.0f}원</div>
                    </div>
                    <div>
                        <div class="live-label">실시간 NAV</div>
                        <div class="live-value">{realtime_nav:,.0f}원</div>
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )

    if strategy_mode.startswith(("V10", "V11")):
        if v10_crash_loc_price is not None and next_action.startswith("BUY"):
            extra_qty_text = (
                f"예상 {v10_extra_estimated_qty:,.0f}주"
                if v10_extra_estimated_qty > 0
                else "예상수량 계산 필요"
            )
            if v10_extra_buy_amount > 0:
                st.markdown(
                    f"""
                    <div class="v10-crash-card">
                        <div class="v10-crash-head">
                            <div class="v10-crash-title">⚡ -9% 급락 추가매수</div>
                            <div class="v10-crash-badge">추가 LOC 1슬롯 대기</div>
                        </div>
                        <div class="v10-crash-grid">
                            <div class="v10-crash-metric">
                                <div class="v10-crash-label">LOC 주문가격</div>
                                <div class="v10-crash-value">${v10_crash_loc_price:,.2f}</div>
                            </div>
                            <div class="v10-crash-metric">
                                <div class="v10-crash-label">예상 주문수량</div>
                                <div class="v10-crash-value">{v10_extra_estimated_qty:,.0f}주</div>
                            </div>
                            <div class="v10-crash-metric">
                                <div class="v10-crash-label">예정금액</div>
                                <div class="v10-crash-value">{v10_extra_buy_amount:,.0f}원</div>
                            </div>
                            <div class="v10-crash-metric">
                                <div class="v10-crash-label">체결조건</div>
                                <div class="v10-crash-value">종가 ≤ ${v10_crash_loc_price:,.2f}</div>
                            </div>
                        </div>
                        <div class="v10-crash-note">
                            기준: 전일 확정 종가 ${latest_signal_close:,.2f} × 0.91 · 조건 충족 시 당일 종가로 추가 1슬롯 체결 판정 · 다음 확정 일봉 업데이트 후 현재 보유슬롯에 자동 반영
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
            else:
                st.caption(
                    f"V10 급락 LOC 기준가 ${v10_crash_loc_price:,.2f} · 현재는 추가 주문에 사용할 두 번째 빈 슬롯/현금이 없습니다."
                )

    # V9/V10 핵심 신호를 한눈에 확인할 수 있도록 별도 상태 카드로 표시합니다.
    if strategy_mode.startswith(("V9", "V10", "V11")):
        risk_on = bool(latest_risk_state)
        trend_on = bool(latest_tb3_state)
        risk_class = "signal-on-danger" if risk_on else "signal-off"
        trend_class = "signal-on-warning" if trend_on else "signal-off"
        risk_status = "ON · 신규매수 중단" if risk_on else "OFF · 신규매수 가능"
        trend_status = "ON · 방어 LOC 적용" if trend_on else "OFF · 일반 LOC 적용"
        risk_detail = "MA200 대비 -15% 미만 + 20일 수익률 -20%~-5%"
        trend_detail = "MA5 < MA20 < MA50"
        rc1, rc2 = st.columns(2, gap="medium")
        with rc1:
            st.markdown(
                f'''<div class="signal-card {risk_class}">
                    <div class="signal-title">⛔ 장기과매도 약세구간</div>
                    <div class="signal-status">{risk_status}</div>
                    <div class="signal-detail">{risk_detail}</div>
                </div>''',
                unsafe_allow_html=True,
            )
        with rc2:
            st.markdown(
                f'''<div class="signal-card {trend_class}">
                    <div class="signal-title">📉 단기하락추세 구간</div>
                    <div class="signal-status">{trend_status}</div>
                    <div class="signal-detail">{trend_detail}</div>
                </div>''',
                unsafe_allow_html=True,
            )

    # 핵심 포트폴리오 숫자는 한 줄로 압축합니다.
    s1, s2, s3, s4, s5, s6 = st.columns(6)
    s1.metric("확정 총자산 NAV", f"{live_nav:,.0f}원", help="V9/V10에서는 실제 보유수량과 누적 추가 입출금을 반영합니다.")
    s2.metric("실시간 추정 자산", f"{realtime_nav:,.0f}원")
    s3.metric("사용 가능 현금", f"{usable_cash:,.0f}원", help="실제 보유수량 보정과 추가 입출금을 반영한 사용가능 현금입니다.")
    s4.metric("누적 순입금", f"{net_external_cashflow:+,.0f}원" if strategy_mode.startswith(("V9", "V10", "V11")) else "-")
    s5.metric("보유 슬롯", f"{live_open_count} / {max_slots_for_live}")
    s6.metric("오늘 종가주문", f"{moc_today_count}건")

    signal_date_text = (
        str(signal_source_date.date())
        if signal_source_date is not None
        else "확인 필요"
    )
    data_state_text = (
        "Data Latest(최신)"
        if missing_sessions == 0
        else f"Data Delay(지연) {missing_sessions}일"
    )
    if strategy_mode.startswith(("V9", "V10", "V11")):
        if latest_risk_state and latest_tb3_state:
            market_short = "장기과매도 약세 + 단기하락추세"
        elif latest_risk_state:
            market_short = "장기과매도 약세"
        elif latest_tb3_state:
            market_short = "단기하락추세 · 방어 LOC 적용"
        else:
            market_short = "정상"
    else:
        market_short = (
            "장기과매도 약세" if strategy_mode.startswith("V8") and latest_risk_state else "정상"
        )

    st.markdown(
        f"""
        <div class="status-strip">
            🇺🇸 <b>{action_session.date()} {session_status.split('(')[0]}</b>
            &nbsp;·&nbsp; Signal {signal_date_text}
            &nbsp;·&nbsp; {data_state_text}
            &nbsp;·&nbsp; Regime {market_short}
            &nbsp;·&nbsp; Updated ET {ny_now.strftime('%H:%M')}
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 정상일 때는 메시지를 만들지 않고, 예외 상황만 표시합니다.
    if signal_source_date is None:
        st.error("신호 계산에 사용할 충분한 확정 일봉이 없습니다. 데이터를 확인하세요.")
    elif missing_sessions == 1:
        st.info(
            f"직전 거래일 데이터가 아직 없어 {signal_source_date.date()} 확정 일봉을 사용합니다. "
            "1거래일 지연은 주문을 자동 보류하지 않습니다."
        )
    elif missing_sessions >= 2:
        st.warning(
            f"주문 판단 데이터가 {missing_sessions}거래일 뒤처져 있습니다. "
            f"현재 {signal_source_date.date()} 확정 일봉 기준 참고 신호입니다."
        )

    if (strategy_mode.startswith("V8") or strategy_mode.startswith(("V9", "V10", "V11"))) and signal_ready and latest_risk_state:
        st.warning(
            "장기과매도 약세구간이라 오늘 신규매수는 쉽니다. 기존 포지션의 매도 규칙은 계속 적용됩니다."
        )

    if strategy_mode.startswith(("V9", "V10")) and signal_ready and latest_tb3_state:
        st.info(
            "TB3 ON: MA5 < MA20 < MA50. 기존 보유 티어의 현재 LOC는 모두 매수가 + $0.10입니다. "
            "TB3가 해제되면 다시 +2.7% LOC로 복귀합니다."
        )

    with st.expander("운용·신호 상세 정보"):
        st.write(f"운용 시작일: {start_date}")
        st.write(f"초기 투자금: {initial_capital:,.0f}원")
        if strategy_mode.startswith(("V9", "V10", "V11")):
            st.write(
                f"추가 입금/출금: 입금 {v9_cashflow_summary['deposits']:,.0f}원 · "
                f"출금 {v9_cashflow_summary['withdrawals']:,.0f}원 · "
                f"순입금 {v9_cashflow_summary['net']:+,.0f}원"
            )
            st.write(
                f"실제 체결 보정손익: {realized_execution_adjustment:+,.0f}원 "
                f"({len(v9_trade_overrides):,}건 수정 저장)"
            )
            st.write(
                f"수동 추가 거래 실현손익: {manual_realized_profit:+,.0f}원 "
                f"({sum(1 for x in v9_manual_trades if x.get('affect_nav', True)):,}건 NAV 반영)"
            )
        st.write(f"미국 주문 대상일: {action_session.date()} · {session_status}")
        st.write(f"신호 기준일: {signal_date_text}")
        st.write(f"시장 상태: {market_state_text}")
        st.write("장기과매도 약세조건: MA200 대비 -15% 미만이며 20거래일 수익률이 -20% 이상 -5% 미만")
        if strategy_mode.startswith(("V9", "V10", "V11")):
            st.write("V11은 TB3 제거 · 일반 LOC +2.7% 적용" if strategy_mode.startswith("V11") else f"단기하락추세 상태: {'ON' if latest_tb3_state else 'OFF'} · 조건 MA5 < MA20 < MA50")
            if pd.notna(latest_ma5) and pd.notna(latest_ma20) and pd.notna(latest_ma50):
                st.write(f"MA5 / MA20 / MA50: {latest_ma5:.2f} / {latest_ma20:.2f} / {latest_ma50:.2f}")
            st.write(
                f"현재 LOC 규칙: {'매수가 + $0.10' if latest_tb3_state else '매수가 × 1.027'}"
            )
            if strategy_mode.startswith(("V10", "V11")):
                crash_text = f"${v10_crash_loc_price:,.2f}" if v10_crash_loc_price is not None else "계산 필요"
                st.write(
                    f"급락 추가매수: 전일 종가 × 0.91 = {crash_text} LOC · "
                    "당일 종가가 기준가 이하이면 같은 종가에 추가 1슬롯 체결"
                )
        if pd.notna(latest_ma_gap):
            st.write(f"MA200 Gap: {latest_ma_gap:.2%}")
        if pd.notna(latest_momentum):
            st.write(f"Momentum 20D: {latest_momentum:.2%}")
        if snapshot["is_live"]:
            updated_text = (
                pd.Timestamp(snapshot["updated_at"]).strftime("%Y-%m-%d %H:%M ET")
                if snapshot["updated_at"] is not None
                else "확인 불가"
            )
            st.write(f"실시간 시세: {snapshot['source']} · {updated_text}")
        else:
            st.write(f"실시간 시세 조회 실패 → 마지막 확정 종가 ${live_close:,.2f} 사용")

    st.caption(
        "실시간 가격은 평가손익과 예상 주문수량 표시용이며 장기과매도 약세·단기하락추세·신규매수금액·TIME/MOC 판단에는 반영하지 않습니다. "
        "V11은 TB3를 사용하지 않으며 D2 손절·4일 연속 하락 매수 유예를 적용합니다. 실시간 영역은 5분마다 자동 새로고침됩니다."
    )

    if strategy_mode.startswith(("V9", "V10", "V11")):
        v9_open_positions = live_result.get("open_positions", [])

    st.subheader("📦 현재 보유 슬롯 · 체결 정보")

    if strategy_mode.startswith(("V9", "V10", "V11")):
        realtime_positions = build_v9_open_positions_table(
            v9_open_positions,
            current_close=realtime_price,
            as_of_date=position_reference_date,
            tb3_state=latest_tb3_state and not strategy_mode.startswith("V11"),
            holding_days=BASE_HOLDING_DAYS,
            fx_daily=v9_fx_daily,
            fallback_fx=portfolio_fallback_fx,
            qty_overrides=v9_quantity_overrides,
            entry_price_overrides=v9_entry_price_overrides,
            excluded_positions=st.session_state.get("v9_excluded_positions", []),
        )
    else:
        realtime_positions = build_open_positions_table(
            live_trades,
            current_close=realtime_price,
            as_of_date=position_reference_date,
            holding_days=(BASE_HOLDING_DAYS if not is_custom else holding_days),
        )

    if realtime_positions.empty:
        st.info("현재 보유 중으로 계산되는 슬롯이 없습니다.")
        return

    position_table = realtime_positions.copy()
    if strategy_mode.startswith("V11") and not position_table.empty:
        flagged = {v9_position_key(p) for p in v9_open_positions if p.get("d2_flag", False)}
        if "_key" in position_table.columns and "상태" in position_table.columns:
            position_table.loc[position_table["_key"].isin(flagged), "상태"] = "MOC(종가 매도) 오늘 예정"
        if "_key" in position_table.columns and "LOC 모드" in position_table.columns:
            position_table.loc[position_table["_key"].isin(flagged), "LOC 모드"] = "D2 -10% · 다음 MOC"

    def _remaining_sort_value(value):
        value = str(value)
        if value == "기한 지남":
            return -1
        if value == "오늘":
            return 0
        try:
            return int(value.replace("거래일", ""))
        except Exception:
            return 999

    position_table["_sort"] = position_table["남은 거래일"].map(_remaining_sort_value)
    position_table = (
        position_table
        .sort_values(["_sort", "매수일"], ascending=[True, True])
        .drop(columns=["_sort"])
        .reset_index(drop=True)
    )

    if strategy_mode.startswith(("V9", "V10", "V11")):
        preferred_cols = [
            "_key", "슬롯", "매수유형", "매수일", "매수금액", "전략 매수가", "매수가", "적용환율", "보유수량",
            "LOC 모드", "LOC 주문가", "현재가", "현재 수익률",
            "평가손익", "MOC 예정일", "남은 거래일", "상태",
        ]
        position_table = position_table[[c for c in preferred_cols if c in position_table.columns]]

        # 실제로 매수하지 않은 자동 생성 슬롯은 실전 운용 기록에서 완전히 제외할 수 있습니다.
        # data_editor의 숨김 키에 의존하지 않고 현재 표시 테이블의 고정 슬롯 키를 직접 저장합니다.
        if "_key" in position_table.columns and not position_table.empty:
            delete_options = {}
            for _, _row in position_table.iterrows():
                _key = str(_row.get("_key", "")).strip()
                if not _key:
                    continue
                _slot = _row.get("슬롯", "-")
                _date = _row.get("매수일", "-")
                _buy_type = _row.get("매수유형", "기본 MOC")
                _price = _row.get("매수가", _row.get("전략 매수가", "-"))
                _label = f"슬롯 {_slot} · {_date} · {_buy_type} · {_price}"
                delete_options[_label] = _key

            # The dropdown values are immutable position keys, never row numbers.
            # Change widget identity whenever the visible slot roster changes.
            if delete_options:
                active_positions = {}
                for pos in v9_open_positions:
                    key = v9_position_key(pos)
                    if key in active_positions:
                        st.error("중복 슬롯 ID가 발견되어 미매수 제외 기능을 중단했습니다.")
                        active_positions = {}
                        break
                    active_positions[key] = pos
                shown_keys = [str(k) for k in position_table["_key"].tolist()
                              if str(k) in active_positions]
                if shown_keys:
                    roster_hash = __import__("hashlib").sha256(
                        "|".join(sorted(shown_keys)).encode("utf-8")
                    ).hexdigest()[:12]
                    row_by_key = {str(row["_key"]): row
                                  for _, row in position_table.iterrows()
                                  if str(row["_key"]) in shown_keys}
                    st.markdown("**🗑️ 실제 미매수 슬롯 제외**")
                    col_pick, col_remove = st.columns([4, 1])
                    with col_pick:
                        selected_key = st.selectbox(
                            "미매수 슬롯 선택",
                            shown_keys,
                            format_func=lambda key: (
                                f"{row_by_key[key]['매수일']} · "
                                f"{row_by_key[key]['매수유형']} · "
                                f"{row_by_key[key]['전략 매수가']} · ID {key}"
                            ),
                            key=f"exclude_one_slot_{roster_hash}",
                        )
                    with col_remove:
                        st.write("")
                        st.write("")
                        remove_pressed = st.button(
                            "🗑️ 선택 삭제",
                            use_container_width=True,
                            disabled=not portfolio_persistence_enabled(),
                            key=f"exclude_one_slot_btn_{roster_hash}",
                        )
                    if remove_pressed:
                        try:
                            # Guard against stale selectbox selection or duplicate IDs.
                            matched = [p for p in v9_open_positions
                                       if v9_position_key(p) == selected_key]
                            if len(matched) != 1 or selected_key not in row_by_key:
                                raise ValueError("현재 표시된 슬롯과 선택한 ID가 일치하지 않습니다. 새로고침 후 다시 시도하세요.")
                            display_date = str(row_by_key[selected_key]["매수일"])
                            model_date = pd.Timestamp(matched[0]["entry_date"]).date().isoformat()
                            if display_date != model_date:
                                raise ValueError("선택 슬롯의 매수일이 현재 모델과 일치하지 않습니다.")
                            current, status = _load_persisted_v9_state()
                            if status != "GitHub 암호화 저장값 불러옴":
                                raise ValueError(f"최신 저장 데이터 불러오기 실패: {status}")
                            excluded = set(current["excluded_positions"])
                            if selected_key in excluded:
                                raise ValueError("이미 미매수 처리된 슬롯입니다.")
                            excluded.add(selected_key)
                            # Only excluded_positions changes. All actual quantities,
                            # entry prices and other encrypted records are retained.
                            ok, msg = _save_persisted_v9_state(
                                quantity_map=current["quantities"],
                                entry_prices=current["entry_prices"],
                                excluded_positions=sorted(excluded),
                                cashflows=current["cashflows"],
                                trade_overrides=current["trade_overrides"],
                                manual_trades=current["manual_trades"],
                                settings=current["settings"],
                            )
                            if not ok:
                                raise ValueError(msg)
                            st.session_state["v9_quantity_overrides"] = current["quantities"]
                            st.session_state["v9_entry_price_overrides"] = current["entry_prices"]
                            st.session_state["v9_excluded_positions"] = sorted(excluded)
                            st.toast(f"{display_date} 슬롯 제외 완료")
                            st.rerun()
                        except Exception as exc:
                            st.error(f"미매수 슬롯 제외 실패: {exc}")

        # 실제 보유수량과 실제 매수체결가는 Open Positions 표에서 직접 수정합니다.
        editor_table = position_table.copy()
        excluded_now = set(_sanitize_v9_excluded_positions(st.session_state.get("v9_excluded_positions", [])))
        editor_table["보유수량"] = (
            editor_table["보유수량"]
            .astype(str)
            .str.replace("주", "", regex=False)
            .str.replace(",", "", regex=False)
            .pipe(pd.to_numeric, errors="coerce")
            .fillna(0)
            .round()
            .astype(int)
        )

        # 현재 미국 동부 날짜(ET)가 MOC 예정일이면 해당 두 셀을 강조합니다.
        # 예: 미국 9/2 MOC 예정일은 한국시간 기준 서머타임 시 9/2 13:00부터 강조됩니다.
        access_us_date = datetime.now(ZoneInfo("America/New_York")).date()
        access_date_text = access_us_date.isoformat()

        def _highlight_moc_today(data):
            styles = pd.DataFrame("", index=data.index, columns=data.columns)
            if "MOC 예정일" in data.columns and "남은 거래일" in data.columns:
                due_mask = data["MOC 예정일"].astype(str).str.startswith(access_date_text)
                styles.loc[due_mask, "MOC 예정일"] = (
                    "background-color: #fff1a8; color: #7a4b00; font-weight: 800;"
                )
                styles.loc[due_mask, "남은 거래일"] = (
                    "background-color: #fff1a8; color: #7a4b00; font-weight: 800;"
                )
            return styles

        styled_editor = editor_table.style.apply(_highlight_moc_today, axis=None)
        edited_positions = st.data_editor(
            styled_editor,
            use_container_width=True,
            hide_index=True,
            disabled=[c for c in editor_table.columns if c not in {"보유수량", "매수가"}],
            column_config={
                "_key": None,
                "슬롯": st.column_config.NumberColumn("슬롯", width="small"),
                "매수유형": st.column_config.TextColumn("매수유형", width="small"),
                "매수일": st.column_config.DateColumn("매수일", width="small"),
                "매수금액": st.column_config.TextColumn("매수금액", width="small"),
                "전략 매수가": st.column_config.TextColumn("전략가", width="small"),
                "매수가": st.column_config.NumberColumn(
                    "✏️ 실제 체결가", min_value=0.01, step=0.01, format="$%.2f", width="small"
                ),
                "적용환율": st.column_config.TextColumn("환율", width="small"),
                "보유수량": st.column_config.NumberColumn(
                    "✏️ 수량", min_value=0, step=1, format="%d주", width="small"
                ),
                "LOC 모드": st.column_config.TextColumn("LOC", width="small"),
                "LOC 주문가": st.column_config.TextColumn("LOC가", width="small"),
                "현재가": st.column_config.TextColumn("현재가", width="small"),
                "현재 수익률": st.column_config.TextColumn("수익률", width="small"),
                "평가손익": st.column_config.TextColumn("평가손익", width="small"),
                "MOC 예정일": st.column_config.TextColumn("MOC일", width="small"),
                "남은 거래일": st.column_config.TextColumn("남은일", width="small"),
                "상태": st.column_config.TextColumn("상태", width="medium"),
            },
            key="v9_open_positions_editor_" + __import__("hashlib").sha256("|".join(sorted(editor_table["_key"].astype(str))).encode()).hexdigest()[:12],
        )

        save_c1, save_c2 = st.columns([1, 1])
        if save_c1.button(
            "💾 실제 수량·체결가 저장",
            use_container_width=True,
            key="save_v9_open_qty_inline",
        ):
            merged_map = dict(st.session_state.get("v9_quantity_overrides", {}))
            merged_price_map = dict(st.session_state.get("v9_entry_price_overrides", {}))
            excluded_set = set(_sanitize_v9_excluded_positions(st.session_state.get("v9_excluded_positions", [])))
            for _, row in edited_positions.iterrows():
                key = str(row.get("_key", "")).strip()
                if key:
                    qty = max(0, int(round(float(row["보유수량"]))))
                    merged_map[key] = qty
                    actual_entry = float(row["매수가"])
                    if actual_entry <= 0:
                        st.error("실제 매수체결가는 0보다 커야 합니다.")
                        st.stop()
                    merged_price_map[key] = round(actual_entry, 6)
            st.session_state["v9_quantity_overrides"] = merged_map
            st.session_state["v9_entry_price_overrides"] = merged_price_map
            ok, msg = save_persisted_v9_open_position_overrides(merged_map, merged_price_map, sorted(excluded_set))
            st.session_state["v9_quantity_persistence_status"] = msg
            st.session_state["v9_quantity_persistence_ok"] = ok
            st.rerun()

        if save_c2.button(
            "↩️ 현재 슬롯 전략값으로 복원",
            use_container_width=True,
            key="reset_v9_open_qty_inline",
        ):
            merged_map = dict(st.session_state.get("v9_quantity_overrides", {}))
            merged_price_map = dict(st.session_state.get("v9_entry_price_overrides", {}))
            excluded_set = set(_sanitize_v9_excluded_positions(st.session_state.get("v9_excluded_positions", [])))
            for pos in v9_open_positions or []:
                excluded_set.discard(v9_position_key(pos))
                key = v9_position_key(pos)
                entry_date = pd.Timestamp(pos["entry_date"]).normalize()
                entry_price = float(pos["entry_price"])
                invested = float(pos["invested"])
                entry_fx = lookup_entry_fx(entry_date, v9_fx_daily, portfolio_fallback_fx)
                if entry_fx is not None and entry_fx > 0 and entry_price > 0:
                    merged_map[key] = max(1, int(round(invested / (entry_price * entry_fx))))
                merged_price_map.pop(key, None)
            st.session_state["v9_quantity_overrides"] = merged_map
            st.session_state["v9_entry_price_overrides"] = merged_price_map
            ok, msg = save_persisted_v9_open_position_overrides(merged_map, merged_price_map, sorted(excluded_set))
            st.session_state["v9_quantity_persistence_status"] = msg
            st.session_state["v9_quantity_persistence_ok"] = ok
            st.rerun()

        excluded_keys = _sanitize_v9_excluded_positions(st.session_state.get("v9_excluded_positions", []))
        if excluded_keys:
            with st.expander(f"↩️ 미매수 처리한 슬롯 복원 ({len(excluded_keys)}개)"):
                restore_key = st.selectbox(
                    "복원할 슬롯",
                    excluded_keys,
                    format_func=lambda x: x,
                    key="restore_excluded_v9_position",
                )
                if st.button("선택 슬롯 다시 표시", use_container_width=True, key="restore_excluded_v9_position_btn"):
                    new_excluded = [x for x in excluded_keys if x != restore_key]
                    ok, msg = save_persisted_v9_open_position_overrides(
                        st.session_state.get("v9_quantity_overrides", {}),
                        st.session_state.get("v9_entry_price_overrides", {}),
                        new_excluded,
                    )
                    st.session_state["v9_quantity_persistence_status"] = msg
                    st.session_state["v9_quantity_persistence_ok"] = ok
                    st.rerun()

        persistence_msg = st.session_state.get("v9_quantity_persistence_status", "")
        if portfolio_persistence_enabled():
            if st.session_state.get("v9_quantity_persistence_ok") is False:
                st.error(f"☁️ 영구저장 오류 · {persistence_msg}")
            else:
                st.caption(f"🔐 실제 보유수량·매수체결가·미매수 슬롯 영구저장 연결됨 · {persistence_msg or 'GitHub 암호화 저장소 사용'}")
        else:
            st.warning(
                "현재는 세션 저장만 사용 중입니다. Streamlit Secrets의 GitHub/암호화 설정을 확인하세요."
            )
    else:
        st.dataframe(
            position_table,
            use_container_width=True,
            hide_index=True,
            height=min(520, max(220, 38 * (len(position_table) + 1))),
        )
    if strategy_mode.startswith(("V9", "V10", "V11")):
        st.caption(
            f"현재가·수익률·평가손익은 {'장중 참고시세' if snapshot['is_live'] else '마지막 확정 종가'} 기준입니다. · "
            f"실제 보유수량과 실제 매수체결가는 표에서 직접 수정할 수 있으며, 저장값은 매수금액·현재 수익률·평가손익·LOC 주문가·실전 NAV 계산에 반영됩니다. · "
            f"목표 매도가는 직전 확정 일봉의 단기하락추세 상태를 반영합니다. · "
            f"V10/V11 선택 시 급락일에 생성된 추가 슬롯은 '급락 LOC (-9%)'로 구분되어 다음 확정 일봉 업데이트 후 자동 표시됩니다. · "
            f"MOC 예정일은 매수일 Day 0 이후 {BASE_HOLDING_DAYS}번째 미국 거래일입니다."
        )
    else:
        st.caption(
            f"현재가·수익률·평가손익은 {'장중 참고시세' if snapshot['is_live'] else '마지막 확정 종가'} 기준입니다. · "
            f"MOC 예정일은 매수일을 Day 0으로 두고 "
            f"{BASE_HOLDING_DAYS if not is_custom else holding_days}번째 미국 거래일입니다."
        )



# ============================================================
# REFERENCE SCREEN LAYOUT (visual-only; trading engine unchanged)
# ============================================================
from html import escape as _html_escape

# Inline vector icons: independent of fonts, emoji and external icon packages.
def _ui_icon(name, color="#2867ea", size=17):
    paths = {
        "chart": '<path d="M3 3v18h18"/><path d="m6 16 5-5 4 3 5-8"/>',
        "target": '<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="5"/><circle cx="12" cy="12" r="1"/>',
        "wallet": '<rect x="3" y="6" width="18" height="15" rx="2"/><path d="M3 9V5a2 2 0 0 1 2-2h14"/><path d="M16 14h5"/>',
        "grid": '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/>',
        "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z"/><path d="M14 2v6h6M8 13h8M8 17h8"/>',
        "bars": '<path d="M4 21v-7h4v7M10 21V9h4v12M16 21V3h4v18"/>',
    }
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" '
            f'stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" '
            f'aria-hidden="true" style="vertical-align:-3px;flex-shrink:0">{paths.get(name,paths["chart"])}</svg>')

def _panel_title(icon, title, suffix=""):
    st.markdown(f'<div class="ref-panel-head">{_ui_icon(icon)}<span>{_html_escape(title)}</span>'
                f'<small>{_html_escape(suffix)}</small></div>',unsafe_allow_html=True)

_asset_nav = float(live_nav)
_asset_cash = float(live_cash)
_asset_invested = max(0.0,_asset_nav-_asset_cash)
_asset_cash_pct = _asset_cash / _asset_nav if _asset_nav>0 else 0.
_asset_invest_pct = _asset_invested / _asset_nav if _asset_nav>0 else 0.
_asset_delta = _asset_nav - float(initial_capital) - float(net_external_cashflow)
_asset_gain_pct = 100*_asset_delta/float(initial_capital) if initial_capital else 0.

st.markdown("<div id='redesign-overview'></div>",unsafe_allow_html=True)
_kpi_items = [
    ("실전 총자산",f"₩{_asset_nav:,.0f}",f"{_asset_delta:+,.0f}원 ({_asset_gain_pct:+.1f}%)","wallet","green"),
    ("백테스트 CAGR",f"{stats['cagr']:.1%}","연평균 복리수익률","bars","blue"),
    ("백테스트 MDD",f"{stats['mdd']:.1%}","최대 낙폭","chart","red"),
    ("현금 비중",f"{_asset_cash_pct:.1%}",f"₩{_asset_cash:,.0f}","wallet","blue"),
    ("투자 비중",f"{_asset_invest_pct:.1%}",f"₩{_asset_invested:,.0f}","bars","blue"),
    ("보유 슬롯",f"{live_open_count} / {max_slots_for_live}",f"사용 {live_open_count}개 · 여유 {max(0,max_slots_for_live-live_open_count)}개","grid","violet"),
]
st.markdown('<div class="ref-kpi-grid">'+''.join(
    '<div class="ref-kpi '+tone+'"><div class="ref-kpi-label">'+_ui_icon(icon,'#72849f',14)+
    '<span>'+_html_escape(label)+'</span></div><div class="ref-kpi-value">'+_html_escape(value)+
    '</div><div class="ref-kpi-foot">'+_html_escape(foot)+'</div></div>'
    for label,value,foot,icon,tone in _kpi_items)+'</div>',unsafe_allow_html=True)

_main_left,_main_right=st.columns([1.65,1],gap="small")
with _main_left:
    with st.container(border=True):
        _chart_title_col,_period_col=st.columns([1.6,1.35],vertical_alignment="center",gap="small")
        with _chart_title_col:
            _panel_title("chart","누적 자산곡선")
        with _period_col:
            _period=st.segmented_control("차트 기간",["6개월","1년","3년","전체"],default="전체",key="ref_equity_period",label_visibility="collapsed")
        _eq=current_result.get("equity",pd.DataFrame()).copy()
        if isinstance(_eq,pd.DataFrame) and not _eq.empty and {"Date","Equity"}.issubset(_eq.columns):
            _eq["Date"]=pd.to_datetime(_eq["Date"])
            _eq=_eq.dropna(subset=["Date","Equity"]).sort_values("Date")
            _history=_eq.copy()
            _history["Drawdown"]=(pd.to_numeric(_history["Equity"])/pd.to_numeric(_history["Equity"]).cummax()-1)*100
            _ranges={"6개월":pd.DateOffset(months=6),"1년":pd.DateOffset(years=1),"3년":pd.DateOffset(years=3)}
            if _period in _ranges and not _eq.empty:
                _from=_eq["Date"].max()-_ranges[_period]
                _eq=_eq[_eq["Date"]>=_from].copy()
            _fig=go.Figure()
            _fig.add_trace(go.Scatter(x=_eq["Date"],y=_eq["Equity"],name="퀀트 전략",
               line=dict(color="#2169ee",width=2.6),mode="lines",fill="tozeroy",fillcolor="rgba(33,105,238,.06)",
               hovertemplate="%{x|%Y-%m-%d}<br>전략 NAV ₩%{y:,.0f}<extra></extra>"))
            _fig.update_layout(height=239,margin=dict(l=4,r=4,t=6,b=2),showlegend=False,
               paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",hovermode="x unified",
               xaxis=dict(title=None,showgrid=False,tickfont=dict(size=10,color="#8290a5")),
               yaxis=dict(title=None,tickformat="~s",tickprefix="₩",gridcolor="#edf2fa",tickfont=dict(size=10,color="#8290a5")),
               font=dict(family="Arial, sans-serif"))
            st.plotly_chart(_fig,use_container_width=True,config={"displayModeBar":False},key="ref_main_strategy_only")
            st.markdown('<div class="ref-chart-subtitle">고점 대비 낙폭 (Drawdown)</div>',unsafe_allow_html=True)
            _dd=_history[_history["Date"].isin(_eq["Date"])].copy()
            _ddfig=go.Figure(go.Scatter(x=_dd["Date"],y=_dd["Drawdown"],name="Drawdown",mode="lines",
                line=dict(color="#ed4764",width=1.8),fill="tozeroy",fillcolor="rgba(237,71,100,.12)",
                hovertemplate="%{x|%Y-%m-%d}<br>%{y:.2f}%<extra></extra>"))
            _ddfig.update_layout(height=179,margin=dict(l=4,r=4,t=5,b=3),showlegend=False,
               paper_bgcolor="rgba(0,0,0,0)",plot_bgcolor="rgba(0,0,0,0)",xaxis=dict(showgrid=False,title=None,tickfont=dict(size=9,color="#8290a5")),
               yaxis=dict(title=None,ticksuffix="%",gridcolor="#edf2fa",tickfont=dict(size=10,color="#8290a5")))
            st.plotly_chart(_ddfig,use_container_width=True,config={"displayModeBar":False},key="ref_main_drawdown")
            st.caption("백테스트 누적자산(원) · 실전 계좌 NAV와 별개 · SOXL 단순보유 비교 없음")
        else:
            st.info("선택 전략의 누적자산 자료가 없습니다.")

with _main_right:
    with st.container(border=True):
        _panel_title("target","TODAY'S ACTION · 오늘 주문",str(action_session.date()))
        # Confirmed-close trading signal; current quotes are only used for
        # estimated integer share quantity (not to generate the BUY signal).
        _buy = next_action.startswith("BUY")
        _data_check = next_action.startswith("CHECK DATA")
        _quote = get_soxl_realtime_snapshot(live_close) if _buy else {}
        _fx_info = get_usdkrw_realtime_snapshot() if _buy else {}
        _price = float(_quote.get("price") or 0.0)
        _fx_raw = _fx_info.get("rate")
        _fx = float(_fx_raw) if _fx_raw is not None and float(_fx_raw) > 0 else None
        _order_cash = float(
            v10_base_buy_amount if strategy_mode.startswith(("V10", "V11")) else next_buy_amount
        ) if _buy else 0.0
        _qty = int(_order_cash / (_price * _fx)) if _buy and _price > 0 and _fx else None
        _state = "BUY" if _buy else ("확인 필요" if _data_check else "WAIT")
        _state_ko = "기본 매수" if _buy else ("데이터 확인" if _data_check else "신규 매수 대기")
        _state_class = "is-buy" if _buy else ("is-check" if _data_check else "is-wait")
        _qty_display = f"{_qty:,}주" if _qty is not None else (
            "가격·환율 확인" if _buy else "0주"
        )
        _cash_display = f"₩{_order_cash:,.0f}"
        st.markdown(
            f'<section class="action-essential {_state_class}">'
            f'<div class="action-essential-state">{_ui_icon("target", "#cc3655" if _buy else "#8090a6", 22)}'
            f'<strong>{_html_escape(_state)}</strong><span>{_html_escape(_state_ko)}</span></div>'
            f'<div class="action-essential-numbers">'
            f'<div class="action-essential-item"><small>예상 주문수량</small><strong>{_html_escape(_qty_display)}</strong></div>'
            f'<div class="action-essential-item"><small>총 주문금액</small><strong>{_html_escape(_cash_display)}</strong></div>'
            f'</div></section>',
            unsafe_allow_html=True,
        )
        if _buy:
            st.caption("현재 시세·환율 기준 예상 주수 · 조건부 급락 추가 LOC 주문은 금액에서 제외")
    with st.container(border=True):
        _panel_title("wallet","현재 계좌","확정 자료")
        _acct=[("현금",_asset_cash,_asset_cash_pct),("투자 평가액",_asset_invested,_asset_invest_pct),("총자산",_asset_nav,1.0)]
        st.markdown('<div class="ref-account-grid">'+''.join(
          '<div class="ref-account-item"><div class="ref-account-name">'+_html_escape(n)+
          '</div><div class="ref-account-value">₩'+f'{v:,.0f}'+
          '</div><div class="ref-account-sub">'+f'{p:.1%}'+'</div></div>' for n,v,p in _acct)+'</div>',unsafe_allow_html=True)
    with st.container(border=True):
        st.markdown("<div id='redesign-slot-preview'></div>",unsafe_allow_html=True)
        _panel_title("grid","보유 슬롯 미리보기")
        _open=open_positions_display.copy() if isinstance(open_positions_display,pd.DataFrame) else pd.DataFrame()
        if not _open.empty:
            _cols=[c for c in ["슬롯","매수일","보유수량","현재 수익률","상태"] if c in _open.columns]
            _pv=_open[_cols].head(7).copy()
            if "상태" in _pv:
                _pv["상태"]=_pv["상태"].astype(str).str.replace("LOC(목표수익 매도) 대기","보유",regex=False).str.replace("MOC(종가 매도) 오늘 예정","오늘 매도",regex=False)
            # Lightweight read-only preview (avoids Streamlit grid scroll chrome).
            _pv_headers="".join(f"<th>{_html_escape(str(c))}</th>" for c in _cols)
            _pv_rows="".join(
                "<tr>"+"".join(f"<td>{_html_escape(str(v))}</td>" for v in row)+"</tr>"
                for row in _pv.itertuples(index=False,name=None)
            )
            st.markdown(
                f'<div class="ref-table-wrap"><table class="ref-data-table"><thead><tr>{_pv_headers}</tr></thead>'
                f'<tbody>{_pv_rows}</tbody></table></div>',
                unsafe_allow_html=True,
            )
        else:
            st.caption("현재 보유 중인 슬롯이 없습니다.")

st.markdown("<div id='redesign-order-management'></div>",unsafe_allow_html=True)
_bottom_left,_bottom_right=st.columns([1.65,1],gap="small")
with _bottom_left:
    with st.container(border=True):
        _panel_title("file","상세 주문 · 슬롯 편집 · 실제 체결가 관리")
        with st.container(key="order_table_tabs"):
            _order_tabs=st.tabs(["전체","보유 슬롯","최근 종료"])
            with _order_tabs[0]:
                _preview=open_positions_display.copy() if isinstance(open_positions_display,pd.DataFrame) else pd.DataFrame()
                if not _preview.empty:
                    _cols=[c for c in ["슬롯","매수유형","매수일","전략 매수가","매수가","보유수량","현재 수익률","상태"] if c in _preview.columns]
                    _bottom_preview=_preview[_cols].head(8).copy()
                    _th="".join(f"<th>{_html_escape(str(c))}</th>" for c in _cols)
                    _rows="".join(
                        "<tr>"+"".join(f"<td>{_html_escape(str(v))}</td>" for v in row)+"</tr>"
                        for row in _bottom_preview.itertuples(index=False,name=None)
                    )
                    st.markdown(
                        f'<div class="ref-table-wrap"><table class="ref-data-table"><thead><tr>{_th}</tr></thead>'
                        f'<tbody>{_rows}</tbody></table></div>',
                        unsafe_allow_html=True,
                    )
                else: st.caption("표시할 실전 슬롯이 없습니다.")
            with _order_tabs[1]:
                st.caption(f"현재 {live_open_count}개 슬롯 보유 · 실제 체결가 변경은 아래 상세 관리에서 진행")
            with _order_tabs[2]:
                _closed=live_trades.copy() if isinstance(live_trades,pd.DataFrame) else pd.DataFrame()
                if not _closed.empty:
                    _cols=[c for c in ["Exit_Date","Entry_Date","Exit_Price","Exit_Type"] if c in _closed.columns]
                    st.dataframe(_closed[_cols].tail(5),hide_index=True,use_container_width=True,height=135)
                else: st.caption("표시할 종료 거래가 없습니다.")
with _bottom_right:
    with st.container(border=True):
        st.markdown("<div id='redesign-performance'></div>",unsafe_allow_html=True)
        _panel_title("bars","전략 성과 요약","백테스트")
        _curve=current_result.get("equity",pd.DataFrame())
        _total=(float(_curve["Equity"].iloc[-1]/_curve["Equity"].iloc[0]-1) if len(_curve)>1 else 0.)
        _win=stats.get("win_rate",float("nan"))
        _vals=[("누적 수익률",f"{_total:+.1%}"),("CAGR",f"{stats['cagr']:.1%}"),
               ("MDD",f"{stats['mdd']:.1%}"),("승률",f"{float(_win):.1%}" if pd.notna(_win) else "-")]
        st.markdown('<div class="ref-perf-grid">'+''.join(
           '<div><small>'+_html_escape(n)+'</small><strong>'+_html_escape(v)+'</strong></div>' for n,v in _vals)+'</div>',unsafe_allow_html=True)
        _refsets=[("선택 전략",current_result),("V11",v11_result),("V10",v10_result),("V9",v9_result)]
        _refdata=[]
        for _nm,_res in _refsets:
            _re=_res.get("equity",pd.DataFrame())
            if len(_re)>1:
                _refdata.append((_nm,100*(float(_re["Equity"].iloc[-1]) /float(_re["Equity"].iloc[0])-1)))
        if _refdata:
            _max=max(abs(v) for _,v in _refdata) or 1.
            st.markdown('<div class="ref-bar-list">'+''.join(
               '<div class="ref-bar-row"><span>'+_html_escape(n)+'</span><div class="ref-bar-track"><div class="ref-bar-fill'+(' main' if i==0 else '')+'" style="width:'+str(max(2,round(abs(v)/_max*100,1)))+'%"></div></div><b>'+f'{v:+.1f}%'+ '</b></div>'
               for i,(n,v) in enumerate(_refdata))+'</div>',unsafe_allow_html=True)

st.markdown("<div id='redesign-manage-details'></div>",unsafe_allow_html=True)
with st.expander("실제 체결가 수정 · 슬롯 삭제/복원 · 실전 주문 상세 관리",expanded=False):
    st.caption("기존 실전 저장·편집 기능을 그대로 사용합니다.")
    render_compact_trading_dashboard()

st.markdown("<div id='redesign-analysis'></div>",unsafe_allow_html=True)
with st.expander("↗ 상세 백테스트 분석 · 거래내역 · 연도별 성과", expanded=False):
    # ============================================================
    # KPI
    # ============================================================

    st.divider()

    st.subheader(
        f"📊 전략 성과 요약 · {strategy_short_name}"
    )

    comparison_name = st.selectbox(
        "성과 비교 기준",
        options=comparison_candidates if comparison_candidates else [strategy_short_name],
        index=(comparison_candidates.index(default_comparison_name) if default_comparison_name in comparison_candidates else 0),
        help="현재 선택한 전략의 성과를 어떤 전략과 비교할지 선택합니다.",
    )
    comparison_stats = comparison_results[comparison_name]["stats"]

    k1, k2, k3, k4 = (
        st.columns(
            4
        )
    )

    k1.metric(
        "최종자산",
        f"{stats['final_equity']:,.0f}원",
        delta=f"{stats['final_equity'] - comparison_stats['final_equity']:+,.0f}원",
    )

    k2.metric(
        "연평균 복리수익률(CAGR)",
        f"{stats['cagr']:.2%}",
        delta=f"{(stats['cagr'] - comparison_stats['cagr']) * 100:+.2f}%p",
    )

    k3.metric(
        "최대 낙폭(MDD)",
        f"{stats['mdd']:.2%}",
        delta=f"{(stats['mdd'] - comparison_stats['mdd']) * 100:+.2f}%p",
    )

    k4.metric(
        "수익·낙폭 효율(Calmar)",
        f"{stats['calmar']:.3f}",
        delta=f"{stats['calmar'] - comparison_stats['calmar']:+.3f}",
    )

    st.caption(
        f"비교 기준: {comparison_name} · "
        "연평균 복리수익률(CAGR)은 전체 투자기간의 성장률을 연 단위 복리수익률로 환산한 값이며, "
        "최대 낙폭(MDD)은 해당 기간 중 포트폴리오의 최고 자산 대비 가장 크게 하락한 폭입니다. "
        "수익·낙폭 효율(Calmar)은 연평균 복리수익률을 최대 낙폭의 절댓값으로 나눈 값으로, 높을수록 수익 대비 낙폭이 효율적입니다."
    )


    # ============================================================
    # TABS
    # ============================================================

    st.divider()


    tab6, tab1, tab2, tab3, tab4, tab5 = (
        st.tabs(
            [

                "📋 거래내역",

                "📈 자산곡선",

                "📉 위험 분석",

                "⏱ 보유기간 분석",

                "🌦 시장 상태",

                "📅 연도별 성과",

            ]
        )
    )


    # ============================================================
    # TAB 1 EQUITY
    # ============================================================

    with tab1:

        st.subheader("전략별 자산곡선 비교")
        equity_options = list(comparison_results.keys())
        default_equity_options = [
            name for name in [
                "V7 Base(기본 전략)",
                "V8 장기과매도 약세필터",
                "V9 Final(약세필터 + 하락추세 방어LOC)",
                "V10 Final(V9 + -9% 급락 추가매수)",
            ]
            if name in equity_options
        ]
        if strategy_short_name not in default_equity_options and strategy_short_name in equity_options:
            default_equity_options.append(strategy_short_name)

        selected_equity_strategies = st.multiselect(
            "표시할 전략",
            options=equity_options,
            default=default_equity_options,
            help="V7·V8·V9·V10 등 원하는 전략의 자산곡선을 동시에 비교할 수 있습니다.",
        )

        fig = go.Figure()

        for strategy_name in selected_equity_strategies:
            result_item = comparison_results[strategy_name]
            fig.add_trace(
                go.Scatter(
                    x=result_item["equity"]["Date"],
                    y=result_item["equity"]["Equity"],
                    name=strategy_name,
                )
            )

        fig.update_layout(
            height=550,
            hovermode="x unified",
            legend=dict(orientation="h"),
        )

        fig.update_yaxes(
            type="log"
        )

        st.plotly_chart(
            fig,
            use_container_width=True,
        )


    # ============================================================
    # TAB 2 RISK
    # ============================================================

    with tab2:

        st.subheader(
            "Drawdown(고점 대비 하락폭)"
        )


        fig = (
            go.Figure()
        )


        fig.add_trace(
            go.Scatter(

                x=
                    base_result[
                        "drawdown"
                    ][
                        "Date"
                    ],

                y=
                    base_result[
                        "drawdown"
                    ][
                        "Drawdown"
                    ]
                    * 100,

                name=
                    "V7 Base(기본 전략)",

            )
        )


        fig.add_trace(
            go.Scatter(

                x=
                    current_result[
                        "drawdown"
                    ][
                        "Date"
                    ],

                y=
                    current_result[
                        "drawdown"
                    ][
                        "Drawdown"
                    ]
                    * 100,

                name=
                    "Selected(선택 전략)",

            )
        )


        fig.update_layout(
            height=500
        )


        st.plotly_chart(
            fig,
            use_container_width=True,
        )


    # ============================================================
    # TAB 3 TIME
    # ============================================================

    with tab3:

        time_summary = (
            time_analysis[
                "summary"
            ]
        )


        time_trades = (
            time_analysis[
                "trades"
            ]
        )


        if time_trades.empty:

            st.info(
                "TIME(기한 만료 매도) 거래가 없습니다."
            )

        else:

            t1, t2, t3, t4 = (
                st.columns(
                    4
                )
            )


            t1.metric(
                "TIME(기한 만료 매도)",
                f"{time_summary['count']:,}건"
            )


            t2.metric(
                "평균 수익률",
                f"{time_summary['avg_return']:.2%}"
            )


            t3.metric(
                "중앙값",
                f"{time_summary['median_return']:.2%}"
            )


            t4.metric(
                "전체 손실 중 TIME(기한 만료 매도)",
                f"{time_summary['loss_share']:.2%}"
            )


            st.subheader(
                "TIME(기한 만료 매도) 이후 SOXL 움직임"
            )


            horizon_cols = (
                st.columns(
                    4
                )
            )


            for col, h in zip(
                horizon_cols,
                [
                    1,
                    3,
                    5,
                    10,
                ]
            ):

                avg = (
                    time_summary[
                        f"forward_{h}_avg"
                    ]
                )

                positive = (
                    time_summary[
                        f"forward_{h}_positive"
                    ]
                )


                if avg is not None:

                    col.metric(

                        f"{h}일 후 평균",

                        f"{avg:.2%}",

                        delta=
                            f"상승확률 {positive:.1%}",

                        delta_color=
                            "off",

                    )


    # ============================================================
    # TAB 4 MARKET V2
    # ============================================================

    with tab4:

        st.subheader(
            "🌦 Market State(시장상태) 분석"
        )


        st.caption(
            "가격 흐름은 SOXL 종가와 MA5·MA20·MA50 이동평균선으로 확인하고, "
            "아래에서는 기존과 동일하게 MA200 괴리율과 최근 20거래일 모멘텀별 거래 성과를 분석합니다."
        )

        # --------------------------------------------------------
        # PRICE + MA5/20/50 CHART
        # --------------------------------------------------------

        prices_display = full_df[["Date", "Close"]].copy()
        prices_display["MA5"] = prices_display["Close"].rolling(5).mean()
        prices_display["MA20"] = prices_display["Close"].rolling(20).mean()
        prices_display["MA50"] = prices_display["Close"].rolling(50).mean()

        prices_display = prices_display[
            (prices_display["Date"].dt.date >= start_date)
            &
            (prices_display["Date"].dt.date <= end_date)
        ].copy()

        if not prices_display.empty:
            latest_ma_row = prices_display.iloc[-1]
            ma1, ma2, ma3 = st.columns(3)
            ma1.metric("5일 이동평균(MA5)", f"${latest_ma_row['MA5']:,.2f}" if pd.notna(latest_ma_row['MA5']) else "-")
            ma2.metric("20일 이동평균(MA20)", f"${latest_ma_row['MA20']:,.2f}" if pd.notna(latest_ma_row['MA20']) else "-")
            ma3.metric("50일 이동평균(MA50)", f"${latest_ma_row['MA50']:,.2f}" if pd.notna(latest_ma_row['MA50']) else "-")

            if pd.notna(latest_ma_row["MA5"]) and pd.notna(latest_ma_row["MA20"]) and pd.notna(latest_ma_row["MA50"]):
                if latest_ma_row["MA5"] < latest_ma_row["MA20"] < latest_ma_row["MA50"]:
                    st.info("현재 이동평균 배열: MA5 < MA20 < MA50 · V9 단기하락추세 방어 LOC 조건에 해당")
                else:
                    st.caption("현재 이동평균 배열은 V9 단기하락추세 조건(MA5 < MA20 < MA50)에 해당하지 않습니다.")

        fig_price = go.Figure()

        for col_name, display_name in [
            ("Close", "SOXL 종가"),
            ("MA5", "5일 이동평균(MA5)"),
            ("MA20", "20일 이동평균(MA20)"),
            ("MA50", "50일 이동평균(MA50)"),
        ]:
            fig_price.add_trace(
                go.Scatter(
                    x=prices_display["Date"],
                    y=prices_display[col_name],
                    name=display_name,
                )
            )

        fig_price.update_layout(
            height=500,
            hovermode="x unified",
            legend=dict(orientation="h"),
        )

        st.plotly_chart(
            fig_price,
            use_container_width=True,
        )


        # --------------------------------------------------------
        # MA GAP
        # --------------------------------------------------------

        st.divider()

        st.subheader(
            "① MA200 Gap(200일 이동평균선 괴리율)별 성과"
        )


        ma_summary = (
            market_analysis[
                "ma_summary"
            ]
        )


        ma_display_table = format_regime_table(ma_summary)
        st.dataframe(
            ma_display_table,
            use_container_width=True,
            hide_index=True,
            height=min(420, max(220, 36 * (len(ma_display_table) + 1))),
        )


        fig_ma = (
            go.Figure()
        )


        fig_ma.add_trace(
            go.Bar(

                x=
                    ma_summary[
                        "Bucket"
                    ],

                y=
                    ma_summary[
                        "Avg_Return"
                    ]
                    * 100,

                name=
                    "평균 수익률",

            )
        )


        fig_ma.update_layout(

            height=420,

            xaxis_title=
                "MA200 괴리율",

            yaxis_title=
                "평균 거래수익률 (%)",

        )


        st.plotly_chart(
            fig_ma,
            use_container_width=True,
        )


        # --------------------------------------------------------
        # MOMENTUM
        # --------------------------------------------------------

        st.divider()

        st.subheader(
            "② Momentum 20D(최근 20거래일 수익률)별 성과"
        )


        momentum_summary = (
            market_analysis[
                "momentum_summary"
            ]
        )


        momentum_display_table = format_regime_table(momentum_summary)
        st.dataframe(
            momentum_display_table,
            use_container_width=True,
            hide_index=True,
            height=min(420, max(220, 36 * (len(momentum_display_table) + 1))),
        )


        fig_momentum = (
            go.Figure()
        )


        fig_momentum.add_trace(
            go.Bar(

                x=
                    momentum_summary[
                        "Bucket"
                    ],

                y=
                    momentum_summary[
                        "Avg_Return"
                    ]
                    * 100,

                name=
                    "평균 수익률",

            )
        )


        fig_momentum.update_layout(

            height=420,

            xaxis_title=
                "최근 20일 수익률",

            yaxis_title=
                "평균 거래수익률 (%)",

        )


        st.plotly_chart(
            fig_momentum,
            use_container_width=True,
        )


        # --------------------------------------------------------
        # COMBINATION
        # --------------------------------------------------------

        st.divider()

        st.subheader(
            "③ MA200 Gap(괴리율) × Momentum 20D(20일 수익률)"
        )


        metric_option = (
            st.selectbox(

                "조합표 표시 지표",

                [
                    "평균 수익률",
                    "승률",
                    "목표가 매도 비율(LOC)",
                    "기한만료 매도 비율(TIME)",
                    "손익 효율(PF)",
                    "거래수",
                ],

            )
        )


        metric_map = {

            "평균 수익률":
                "Avg_Return",

            "승률":
                "Win_Rate",

            "목표가 매도 비율(LOC)":
                "Target_Rate",

            "기한만료 매도 비율(TIME)":
                "Time_Rate",

            "손익 효율(PF)":
                "Profit_Factor",

            "거래수":
                "Trades",

        }


        metric_col = (
            metric_map[
                metric_option
            ]
        )


        combo = (
            market_analysis[
                "combo_summary"
            ]
        )


        heatmap_df = (
            combo.pivot(

                index=
                    "MA200_Bucket",

                columns=
                    "Momentum_Bucket",

                values=
                    metric_col,

            )
        )


        heatmap_df = (
            heatmap_df.reindex(

                index=
                    market_analysis[
                        "ma_labels"
                    ],

                columns=
                    market_analysis[
                        "momentum_labels"
                    ],

            )
        )


        heatmap_values = (
            heatmap_df.copy()
        )


        if metric_col in [

            "Avg_Return",

            "Win_Rate",

            "Target_Rate",

            "Time_Rate",

        ]:

            heatmap_values = (
                heatmap_values
                * 100
            )


            value_suffix = "%"

        else:

            value_suffix = ""


        fig_heat = (
            go.Figure(

                data=
                    go.Heatmap(

                        z=
                            heatmap_values.values,

                        x=
                            heatmap_values.columns,

                        y=
                            heatmap_values.index,

                        text=
                            heatmap_values.round(
                                2
                            ).astype(
                                str
                            )
                            + value_suffix,

                        texttemplate=
                            "%{text}",

                        hovertemplate=(
                            "MA200: %{y}<br>"
                            "20일 모멘텀: %{x}<br>"
                            f"{metric_option}: "
                            "%{text}"
                            "<extra></extra>"
                        ),

                    )

            )
        )


        fig_heat.update_layout(

            height=520,

            xaxis_title=
                "최근 20거래일 모멘텀",

            yaxis_title=
                "MA200 괴리율",

        )


        st.plotly_chart(
            fig_heat,
            use_container_width=True,
        )


        # --------------------------------------------------------
        # COMBO DETAIL
        # --------------------------------------------------------

        st.subheader(
            "조합별 상세 성과"
        )


        combo_display = (
            combo[
                combo[
                    "Trades"
                ] > 0
            ]
            .copy()
        )


        combo_display[
            "평균 수익률"
        ] = (
            combo_display[
                "Avg_Return"
            ]
            .map(
                lambda x:
                    f"{x:.2%}"
            )
        )


        combo_display[
            "승률"
        ] = (
            combo_display[
                "Win_Rate"
            ]
            .map(
                lambda x:
                    f"{x:.2%}"
            )
        )


        combo_display[
            "LOC율"
        ] = (
            combo_display[
                "Target_Rate"
            ]
            .map(
                lambda x:
                    f"{x:.2%}"
            )
        )


        combo_display[
            "TIME율"
        ] = (
            combo_display[
                "Time_Rate"
            ]
            .map(
                lambda x:
                    f"{x:.2%}"
            )
        )


        combo_display[
            "PF"
        ] = (
            combo_display[
                "Profit_Factor"
            ]
            .map(

                lambda x:
                    (
                        "∞"
                        if x == float("inf")
                        else f"{x:.2f}"
                    )
                    if pd.notna(x)
                    else "-"

            )
        )


        combo_display = (
            combo_display[
                [
                    "MA200_Bucket",
                    "Momentum_Bucket",
                    "Trades",
                    "평균 수익률",
                    "승률",
                    "LOC율",
                    "TIME율",
                    "PF",
                ]
            ]
        )


        combo_display.columns = [

            "MA200 괴리율",

            "20일 모멘텀",

            "거래수",

            "평균 수익률",

            "승률",

            "목표가 매도 비율(LOC)",

            "기한만료 매도 비율(TIME)",

            "손익 효율(PF)",

        ]


        st.dataframe(
            combo_display,
            use_container_width=True,
            hide_index=True,
            height=min(560, max(260, 36 * (len(combo_display) + 1))),
        )


        # --------------------------------------------------------
        # RAW DATA DOWNLOAD
        # --------------------------------------------------------

        market_csv = (
            market_analysis[
                "trades"
            ]
            .to_csv(
                index=False
            )
            .encode(
                "utf-8-sig"
            )
        )


        st.download_button(

            "📥 시장상태 분석 거래내역 다운로드",

            data=
                market_csv,

            file_name=
                "SOXL_market_regime_analysis.csv",

            mime=
                "text/csv",

        )


    # ============================================================
    # TAB 5 YEARLY
    # ============================================================

    with tab5:

        yearly = (
            current_result[
                "yearly"
            ]
            .copy()
        )


        yearly_display = (
            pd.DataFrame(
                {

                    "연도":
                        yearly[
                            "Year"
                        ],

                    "수익률":
                        yearly[
                            "Year_Return"
                        ]
                        .map(
                            lambda x:
                                f"{x:.2%}"
                        ),

                    "연중 최대 낙폭":
                        yearly[
                            "Year_MDD"
                        ]
                        .map(
                            lambda x:
                                f"{x:.2%}"
                        ),

                    "연말자산":
                        yearly[
                            "Year_End_Equity"
                        ]
                        .map(
                            lambda x:
                                f"{x:,.0f}원"
                        ),

                }
            )
        )


        st.caption(
            "연중 최대 낙폭은 각 연도 안에서 기록한 최고 포트폴리오 자산 대비 이후 가장 크게 하락한 폭입니다. "
            "주가 자체의 낙폭이 아니라 전략 자산가치 기준이며, 매년 새로 계산됩니다."
        )


        st.dataframe(
            yearly_display,
            use_container_width=True,
            hide_index=True,
            height=min(520, max(220, 36 * (len(yearly_display) + 1))),
        )


    # ============================================================
    # TAB 6 TRADES
    # ============================================================

    with tab6:

        if strategy_mode.startswith(("V9", "V10", "V11")):
            st.subheader("📋 실제 거래내역(Actual Trades)")

            with st.expander("➕ 수동 거래 추가 / 구글시트 기록 가져오기"):
                st.caption(
                    "전략 백테스트에 존재하지 않는 실제 청산거래를 추가할 수 있습니다. "
                    "기존 전략 거래와 동일한 거래를 다시 추가하면 NAV 손익이 이중 반영될 수 있습니다."
                )
                mc1, mc2 = st.columns(2)
                manual_entry_date = mc1.date_input("매수일", value=start_date, key="manual_trade_entry_date")
                manual_exit_date = mc2.date_input("매도일", value=min(max_date, date.today()), key="manual_trade_exit_date")
                mp1, mp2, mp3 = st.columns(3)
                manual_entry_price = mp1.number_input("매수가($)", min_value=0.01, value=1.00, step=0.01, key="manual_trade_entry_price")
                manual_exit_price = mp2.number_input("매도가($)", min_value=0.01, value=1.00, step=0.01, key="manual_trade_exit_price")
                manual_qty = mp3.number_input("수량(주)", min_value=1, value=1, step=1, key="manual_trade_qty")
                ms1, ms2 = st.columns(2)
                manual_strategy = ms1.selectbox(
                    "매도전략",
                    ["일반 LOC(+2.7%)", "방어 LOC(+$0.10)", "TIME/MOC", "수동 매도", "기타"],
                    index=3,
                    key="manual_trade_strategy",
                )
                manual_affect_nav = ms2.checkbox(
                    "실전 NAV에 손익 반영",
                    value=True,
                    key="manual_trade_affect_nav",
                    help="과거 실제 운용손익을 현재 실전 NAV에 이어서 반영하려면 체크합니다.",
                )
                manual_note = st.text_input("메모(선택)", key="manual_trade_note")
                if st.button("➕ 거래 1건 추가", use_container_width=True, key="add_manual_trade"):
                    if manual_exit_date < manual_entry_date:
                        st.error("매도일은 매수일보다 빠를 수 없습니다.")
                    else:
                        new_item = {
                            "id": uuid.uuid4().hex,
                            "entry_date": pd.Timestamp(manual_entry_date).date().isoformat(),
                            "exit_date": pd.Timestamp(manual_exit_date).date().isoformat(),
                            "entry_price": float(manual_entry_price),
                            "exit_price": float(manual_exit_price),
                            "quantity": int(manual_qty),
                            "exit_strategy": manual_strategy,
                            "note": manual_note,
                            "affect_nav": bool(manual_affect_nav),
                        }
                        updated_manual = _sanitize_v9_manual_trades(
                            st.session_state.get("v9_manual_trades", []) + [new_item]
                        )
                        ok, msg = save_persisted_v9_manual_trades(updated_manual)
                        if ok:
                            st.rerun()
                        else:
                            st.error(msg)

                st.divider()
                st.markdown("**구글시트 → CSV 일괄 가져오기**")
                template_df = pd.DataFrame([{
                    "매수일": "2026-01-02",
                    "매도일": "2026-01-09",
                    "매수가($)": 30.50,
                    "매도가($)": 31.20,
                    "수량(주)": 10,
                    "매도전략": "수동 매도",
                    "메모": "기존 구글시트 기록",
                    "NAV반영": True,
                }])
                st.download_button(
                    "CSV 양식 다운로드",
                    data=template_df.to_csv(index=False).encode("utf-8-sig"),
                    file_name="SOXL_manual_trades_template.csv",
                    mime="text/csv",
                    use_container_width=True,
                    key="download_manual_template",
                )
                manual_csv = st.file_uploader(
                    "구글시트에서 CSV로 내려받은 파일",
                    type=["csv"],
                    key="manual_trade_csv_upload",
                )
                if manual_csv is not None:
                    try:
                        imported = pd.read_csv(manual_csv)
                        required_cols = ["매수일", "매도일", "매수가($)", "매도가($)", "수량(주)"]
                        missing_cols = [c for c in required_cols if c not in imported.columns]
                        if missing_cols:
                            st.error("필수 열이 없습니다: " + ", ".join(missing_cols))
                        else:
                            preview_cols = [c for c in ["매수일", "매도일", "매수가($)", "매도가($)", "수량(주)", "매도전략", "메모", "NAV반영"] if c in imported.columns]
                            st.dataframe(imported[preview_cols].head(20), use_container_width=True, hide_index=True)
                            if st.button("📥 CSV 거래내역 가져오기", use_container_width=True, key="import_manual_trades_csv"):
                                new_rows = []
                                for _, r in imported.iterrows():
                                    strategy_value = str(r.get("매도전략", "수동 매도")).strip()
                                    nav_raw = r.get("NAV반영", True)
                                    if isinstance(nav_raw, str):
                                        affect_nav = nav_raw.strip().lower() not in {"false", "0", "no", "n", "아니오", "미반영"}
                                    else:
                                        affect_nav = bool(nav_raw) if pd.notna(nav_raw) else True
                                    new_rows.append({
                                        "id": uuid.uuid4().hex,
                                        "entry_date": r["매수일"],
                                        "exit_date": r["매도일"],
                                        "entry_price": r["매수가($)"],
                                        "exit_price": r["매도가($)"],
                                        "quantity": r["수량(주)"],
                                        "exit_strategy": strategy_value,
                                        "note": str(r.get("메모", "")) if pd.notna(r.get("메모", "")) else "",
                                        "affect_nav": affect_nav,
                                    })
                                before = len(st.session_state.get("v9_manual_trades", []))
                                merged_manual = _sanitize_v9_manual_trades(
                                    st.session_state.get("v9_manual_trades", []) + new_rows
                                )
                                added = len(merged_manual) - before
                                ok, msg = save_persisted_v9_manual_trades(merged_manual)
                                if ok:
                                    st.success(f"{added:,}건을 가져왔습니다.")
                                    st.rerun()
                                else:
                                    st.error(msg)
                    except Exception as e:
                        st.error(f"CSV 읽기 실패: {e}")

                current_manual = _sanitize_v9_manual_trades(st.session_state.get("v9_manual_trades", []))
                if current_manual:
                    st.divider()
                    st.markdown(f"**현재 수동 추가 거래: {len(current_manual):,}건**")
                    manual_options = {
                        f"{x['entry_date']} → {x['exit_date']} | {x['quantity']}주 | {x['entry_price']:.2f}→{x['exit_price']:.2f} | {x.get('note','')}": x["id"]
                        for x in reversed(current_manual)
                    }
                    delete_manual_label = st.selectbox(
                        "삭제할 수동 거래",
                        list(manual_options.keys()),
                        key="delete_manual_trade_select",
                    )
                    if st.button("🗑️ 선택 거래 삭제", use_container_width=True, key="delete_manual_trade"):
                        delete_manual_id = manual_options[delete_manual_label]
                        remaining_manual = [x for x in current_manual if x["id"] != delete_manual_id]
                        ok, msg = save_persisted_v9_manual_trades(remaining_manual)
                        if ok:
                            st.rerun()
                        else:
                            st.error(msg)

            st.caption(
                "전략 체결값이 기본으로 입력되어 있으며, 실제 매매가 달랐던 거래만 날짜·가격·수량·매도전략을 수정해 저장할 수 있습니다. "
                "저장한 실제 체결 손익은 실전 현금·총자산(NAV)·다음 매수금액에 반영되지만, 전략 백테스트의 연평균 복리수익률(CAGR)·최대 낙폭(MDD)에는 영향을 주지 않습니다."
            )

            if "v9_trade_overrides" not in st.session_state:
                loaded_trade_overrides, trade_override_status = load_persisted_v9_trade_overrides()
                st.session_state["v9_trade_overrides"] = loaded_trade_overrides
                st.session_state["v9_trade_persistence_status"] = trade_override_status

            trade_overrides_for_editor = _sanitize_v9_trade_overrides(
                st.session_state.get("v9_trade_overrides", {})
            )
            qty_map_for_trades = dict(st.session_state.get("v9_quantity_overrides", {}))

            if trades.empty:
                st.info("표시할 청산 거래가 없습니다.")
            else:
                trade_model_dates = pd.to_datetime(trades["Entry_Date"], errors="coerce").dropna()
                override_entry_dates = []
                for item in trade_overrides_for_editor.values():
                    try:
                        override_entry_dates.append(pd.Timestamp(item.get("entry_date")))
                    except Exception:
                        pass

                if not trade_model_dates.empty:
                    fx_start = min(override_entry_dates + [trade_model_dates.min()]) if override_entry_dates else trade_model_dates.min()
                    fx_end = max(override_entry_dates + [trade_model_dates.max()]) if override_entry_dates else trade_model_dates.max()
                    trades_fx_daily = get_usdkrw_daily_rates(fx_start, fx_end)
                else:
                    trades_fx_daily = pd.DataFrame(columns=["Date", "USD_KRW"])

                actual_records_all = build_v9_actual_trade_records(
                    trades,
                    qty_overrides=qty_map_for_trades,
                    trade_overrides=trade_overrides_for_editor,
                    fx_daily=trades_fx_daily,
                    fallback_fx=portfolio_fallback_fx,
                    excluded_positions=st.session_state.get("v9_excluded_positions", []),
                )
                manual_for_editor = _sanitize_v9_manual_trades(st.session_state.get("v9_manual_trades", []))
                if manual_for_editor:
                    manual_entry_dates = [pd.Timestamp(x["entry_date"]) for x in manual_for_editor]
                    manual_fx_daily = get_usdkrw_daily_rates(min(manual_entry_dates), max(manual_entry_dates)) if manual_entry_dates else trades_fx_daily
                    manual_records = build_v9_manual_trade_records(
                        manual_for_editor,
                        fx_daily=manual_fx_daily,
                        fallback_fx=portfolio_fallback_fx,
                    )
                    if not manual_records.empty:
                        actual_records_all = pd.concat([actual_records_all, manual_records], ignore_index=True, sort=False)

                # 실제 거래내역에서 바로 확인할 수 있는 핵심 결과만 요약합니다.
                # 아래 편집표와 같은 데이터를 다시 표 형태로 반복 표시하지 않습니다.
                actual_profit_series = pd.to_numeric(
                    actual_records_all.get("손익(원)", pd.Series(dtype=float)),
                    errors="coerce",
                )
                actual_return_series = pd.to_numeric(
                    actual_records_all.get("수익률", pd.Series(dtype=float)),
                    errors="coerce",
                )
                valid_profit = actual_profit_series.dropna()
                valid_return = actual_return_series.dropna()
                actual_total_profit = float(valid_profit.sum()) if not valid_profit.empty else 0.0
                actual_win_rate = (
                    float((valid_profit > 0).mean())
                    if not valid_profit.empty
                    else float("nan")
                )
                actual_avg_return = (
                    float(valid_return.mean())
                    if not valid_return.empty
                    else float("nan")
                )
                actual_trade_count = int(len(actual_records_all))

                ak1, ak2, ak3, ak4 = st.columns(4)
                ak1.metric("누적 실제 손익", f"{actual_total_profit:+,.0f}원")
                ak2.metric(
                    "실제 거래 승률",
                    f"{actual_win_rate:.2%}" if pd.notna(actual_win_rate) else "-",
                )
                ak3.metric(
                    "평균 실제 수익률",
                    f"{actual_avg_return:.2%}" if pd.notna(actual_avg_return) else "-",
                )
                ak4.metric("총 거래건수", f"{actual_trade_count:,}건")

                view_options = ["최근 30건", "최근 100건", "전체"]
                view_choice = st.selectbox(
                    "표시 범위",
                    view_options,
                    index=1,
                    key="v9_actual_trade_view_range",
                    label_visibility="collapsed",
                )

                actual_records = actual_records_all.sort_values(
                    ["매도일", "매수일"], ascending=[False, False]
                )
                if view_choice == "최근 30건":
                    actual_records = actual_records.head(30)
                elif view_choice == "최근 100건":
                    actual_records = actual_records.head(100)

                actual_records["구분"] = actual_records.get("_source", "strategy").map({"strategy": "전략연결", "manual": "수동추가"}).fillna("전략연결")
                editor_cols = [
                    "구분", "매수유형", "매수일", "매도일", "매수가($)", "매도가($)", "수량(주)",
                    "매도전략", "투자금(원)", "수익률", "손익(원)", "수정",
                ]
                editor_df = actual_records.set_index("_key")[editor_cols].copy()
                editor_df["투자금(원)"] = editor_df["투자금(원)"].map(
                    lambda x: f"{x:,.0f}원" if pd.notna(x) else "환율 조회 필요"
                )
                editor_df["수익률"] = editor_df["수익률"].map(
                    lambda x: f"{x:.2%}" if pd.notna(x) else "계산 불가"
                )
                editor_df["손익(원)"] = editor_df["손익(원)"].map(
                    lambda x: f"{x:+,.0f}원" if pd.notna(x) else "환율 조회 필요"
                )

                editor_height = min(700, max(280, 42 * (len(editor_df) + 1)))
                edited_trades = st.data_editor(
                    editor_df,
                    use_container_width=True,
                    hide_index=True,
                    height=editor_height,
                    row_height=38,
                    disabled=["구분", "매수유형", "투자금(원)", "수익률", "손익(원)", "수정"],
                    column_config={
                        "구분": st.column_config.TextColumn("구분", width="small"),
                        "매수유형": st.column_config.TextColumn("매수유형", width="medium"),
                        "매수일": st.column_config.DateColumn("✏️ 매수일", format="YYYY-MM-DD", width="small"),
                        "매도일": st.column_config.DateColumn("✏️ 매도일", format="YYYY-MM-DD", width="small"),
                        "매수가($)": st.column_config.NumberColumn("✏️ 매수가", min_value=0.01, step=0.01, format="$%.2f", width="small"),
                        "매도가($)": st.column_config.NumberColumn("✏️ 매도가", min_value=0.01, step=0.01, format="$%.2f", width="small"),
                        "수량(주)": st.column_config.NumberColumn("✏️ 수량", min_value=1, step=1, format="%d주", width="small"),
                        "매도전략": st.column_config.SelectboxColumn(
                            "✏️ 실제 매도전략",
                            options=[
                                "일반 LOC(+2.7%)",
                                "방어 LOC(+$0.10)",
                                "TIME/MOC",
                                "수동 매도",
                                "기타",
                            ],
                            width="medium",
                        ),
                        "투자금(원)": st.column_config.TextColumn("투자금", width="medium"),
                        "수익률": st.column_config.TextColumn("수익률", width="small"),
                        "손익(원)": st.column_config.TextColumn("손익", width="medium"),
                        "수정": st.column_config.TextColumn("상태", width="small"),
                    },
                    key="v9_actual_trades_editor",
                )

                st.caption("가격·수량을 수정한 뒤 저장하면 표가 다시 계산되어 투자금·수익률·손익이 갱신됩니다.")

                save_col, reset_col = st.columns(2)

                if save_col.button(
                    "💾 실제 거래 변경 저장",
                    use_container_width=True,
                    key="save_v9_actual_trades",
                ):
                    baseline = actual_records_all.set_index("_key")
                    displayed_keys = set(editor_df.index.astype(str))
                    new_overrides = {
                        k: v for k, v in trade_overrides_for_editor.items()
                        if k not in displayed_keys
                    }
                    updated_qty_map = dict(qty_map_for_trades)
                    validation_error = None

                    for key, row in edited_trades.iterrows():
                        key = str(key)
                        if key not in baseline.index:
                            continue
                        base = baseline.loc[key]
                        try:
                            actual_entry_date = pd.Timestamp(row["매수일"]).date()
                            actual_exit_date = pd.Timestamp(row["매도일"]).date()
                            actual_entry_price = float(row["매수가($)"])
                            actual_exit_price = float(row["매도가($)"])
                            actual_qty = max(1, int(round(float(row["수량(주)"]))))
                            actual_strategy = str(row["매도전략"])
                        except Exception:
                            validation_error = "날짜·가격·수량 입력값을 확인해주세요."
                            break

                        if actual_exit_date < actual_entry_date:
                            validation_error = f"{actual_entry_date} 거래의 매도일이 매수일보다 빠릅니다."
                            break
                        if actual_entry_price <= 0 or actual_exit_price <= 0:
                            validation_error = "매수가와 매도가는 0보다 커야 합니다."
                            break

                        if str(base.get("_source", "strategy")) == "manual":
                            manual_id = str(base.get("_manual_id"))
                            for manual_item in manual_for_editor:
                                if manual_item["id"] == manual_id:
                                    manual_item.update({
                                        "entry_date": actual_entry_date.isoformat(),
                                        "exit_date": actual_exit_date.isoformat(),
                                        "entry_price": actual_entry_price,
                                        "exit_price": actual_exit_price,
                                        "quantity": actual_qty,
                                        "exit_strategy": actual_strategy,
                                    })
                                    break
                            continue

                        strategy_entry_date = pd.Timestamp(base["전략 매수일"]).date()
                        strategy_exit_date = pd.Timestamp(base["전략 매도일"]).date()
                        strategy_entry_price = float(base["전략 매수가"])
                        strategy_exit_price = float(base["전략 매도가"])
                        strategy_qty = max(1, int(round(float(base["전략 수량"]))))
                        strategy_exit_strategy = str(base["전략 매도전략"])

                        changed = any([
                            actual_entry_date != strategy_entry_date,
                            actual_exit_date != strategy_exit_date,
                            abs(actual_entry_price - strategy_entry_price) > 1e-9,
                            abs(actual_exit_price - strategy_exit_price) > 1e-9,
                            actual_qty != strategy_qty,
                            actual_strategy != strategy_exit_strategy,
                        ])

                        if changed:
                            new_overrides[key] = {
                                "entry_date": actual_entry_date.isoformat(),
                                "exit_date": actual_exit_date.isoformat(),
                                "entry_price": actual_entry_price,
                                "exit_price": actual_exit_price,
                                "quantity": actual_qty,
                                "exit_strategy": actual_strategy,
                            }
                            updated_qty_map[key] = actual_qty
                        else:
                            new_overrides.pop(key, None)
                            # 표시된 청산거래가 전략값으로 복원되면 과거 실제수량 오버라이드도 함께 해제합니다.
                            updated_qty_map.pop(key, None)

                    if validation_error:
                        st.error(validation_error)
                    else:
                        st.session_state["v9_trade_overrides"] = new_overrides
                        st.session_state["v9_quantity_overrides"] = updated_qty_map
                        st.session_state["v9_manual_trades"] = _sanitize_v9_manual_trades(manual_for_editor)
                        ok, msg = save_persisted_v9_trade_overrides(
                            new_overrides, quantity_map=updated_qty_map
                        )
                        st.session_state["v9_trade_persistence_ok"] = ok
                        st.session_state["v9_trade_persistence_status"] = msg
                        if ok:
                            st.rerun()
                        else:
                            st.error(msg)

                if reset_col.button(
                    "↩️ 표시 거래 전략값으로 복원",
                    use_container_width=True,
                    key="reset_v9_actual_trades",
                ):
                    displayed_keys = set(editor_df.index.astype(str))
                    strategy_displayed_keys = {k for k in displayed_keys if not str(k).startswith("manual|")}
                    reset_overrides = {
                        k: v for k, v in trade_overrides_for_editor.items()
                        if k not in strategy_displayed_keys
                    }
                    reset_qty_map = dict(qty_map_for_trades)
                    for key in strategy_displayed_keys:
                        reset_qty_map.pop(key, None)
                    st.session_state["v9_trade_overrides"] = reset_overrides
                    st.session_state["v9_quantity_overrides"] = reset_qty_map
                    ok, msg = save_persisted_v9_trade_overrides(
                        reset_overrides, quantity_map=reset_qty_map
                    )
                    st.session_state["v9_trade_persistence_ok"] = ok
                    st.session_state["v9_trade_persistence_status"] = msg
                    if ok:
                        st.rerun()
                    else:
                        st.error(msg)

                persistence_msg = st.session_state.get("v9_trade_persistence_status", "")
                if portfolio_persistence_enabled():
                    if st.session_state.get("v9_trade_persistence_ok") is False:
                        st.error(f"☁️ 실제 거래 영구저장 오류 · {persistence_msg}")
                    else:
                        st.caption(
                            f"🔐 실제 거래 수정값 영구저장 연결됨 · "
                            f"{len(trade_overrides_for_editor):,}건 수정 저장 · "
                            f"{persistence_msg or 'GitHub 암호화 저장소 사용'}"
                        )
                else:
                    st.warning("현재는 세션 저장만 사용 중입니다. Streamlit Secrets의 GitHub/암호화 설정을 확인하세요.")

                # 실제 거래내역 편집표 자체가 상세 손익표 역할을 하므로 중복 요약표는 제거했습니다.

                with st.expander("전략 기준값과 비교"):
                    strategy_only_records = actual_records[actual_records.get("_source", "strategy") != "manual"].copy()
                    strategy_compare = strategy_only_records[[
                        "매수유형", "전략 매수일", "전략 매도일", "전략 매수가", "전략 매도가",
                        "전략 수량", "전략 매도전략", "수정",
                    ]].copy()
                    strategy_compare["전략 매수가"] = strategy_compare["전략 매수가"].map(lambda x: f"${x:,.2f}" if pd.notna(x) else "-")
                    strategy_compare["전략 매도가"] = strategy_compare["전략 매도가"].map(lambda x: f"${x:,.2f}" if pd.notna(x) else "-")
                    strategy_compare["전략 수량"] = strategy_compare["전략 수량"].map(lambda x: f"{int(x):,}주" if pd.notna(x) else "-")
                    st.dataframe(
                        strategy_compare,
                        use_container_width=True,
                        hide_index=True,
                        height=min(520, max(220, 36 * (len(strategy_compare) + 1))),
                    )

                download_actual = actual_records_all[[
                    "매수유형", "매수일", "매도일", "매수가($)", "매도가($)", "수량(주)",
                    "매도전략", "적용환율", "투자금(원)", "수익률", "손익(원)", "수정",
                ]].copy()
                csv_actual = download_actual.to_csv(index=False).encode("utf-8-sig")
                csv_strategy = trades.to_csv(index=False).encode("utf-8-sig")
                dl1, dl2 = st.columns(2)
                dl1.download_button(
                    "📥 실제 거래내역 CSV",
                    data=csv_actual,
                    file_name="SOXL_actual_trades.csv",
                    mime="text/csv",
                    use_container_width=True,
                )
                dl2.download_button(
                    "📥 전략 원본 거래내역 CSV",
                    data=csv_strategy,
                    file_name="SOXL_strategy_trades.csv",
                    mime="text/csv",
                    use_container_width=True,
                )

        else:
            # V7/V8/Custom은 기존 전략 거래표를 읽기 전용으로 유지합니다.
            display_trades = trades.copy()
            exit_label_map = {
                "TARGET": "TARGET(목표수익 매도)",
                "NORMAL_LOC": "LOC(+2.7% 목표수익 매도)",
                "TB3_DYN_LOC": "방어 LOC(매수가 + $0.10 매도)",
                "TIME": "TIME(기한 만료 매도)",
                "END": "END(검증 종료 시 보유)",
            }
            display_trades["매수일"] = pd.to_datetime(display_trades["Entry_Date"]).dt.date
            display_trades["매도일"] = pd.to_datetime(display_trades["Exit_Date"]).dt.date
            display_trades["매수가"] = display_trades["Entry_Price"].map(lambda x: f"${x:,.2f}")
            display_trades["매도가"] = display_trades["Exit_Price"].map(lambda x: f"${x:,.2f}")
            display_trades["투자금"] = display_trades["Invested"].map(lambda x: f"{x:,.0f}원")
            display_trades["수익률"] = display_trades["Return"].map(lambda x: f"{x:.2%}")
            display_trades["손익"] = display_trades["Profit"].map(lambda x: f"{x:,.0f}원")
            display_trades["보유 거래일"] = display_trades["Holding_Days"].map(lambda x: f"{int(x)}일")
            display_trades["매도 이유"] = display_trades["Exit_Type"].map(exit_label_map).fillna(display_trades["Exit_Type"])
            display_trades = display_trades[[
                "매수일", "매도일", "매수가", "매도가", "투자금",
                "수익률", "손익", "보유 거래일", "매도 이유",
            ]]
            st.dataframe(
                display_trades,
                use_container_width=True,
                hide_index=True,
                height=min(650, max(260, 38 * (len(display_trades) + 1))),
            )
            csv = trades.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                "📥 거래내역 CSV 다운로드",
                data=csv,
                file_name="SOXL_current_strategy_trades.csv",
                mime="text/csv",
            )


    # ============================================================
    # FOOTER
    # ============================================================

    st.divider()

    st.caption(
        "V7 Base(기본 전략): 전일 NAV(총자산) / 7분할 / LOC(목표수익 매도) +2.7% / "
        "7거래일 TIME·MOC(기한 만료 종가 매도) / Reserve(남겨둘 현금) 0% / "
        "당일 매도대금 재사용 금지"
    )

    st.caption(
        "V8 장기과매도 약세필터: V7 규칙을 그대로 사용하되, "
        "Risk B(매수 쉬어가기 구간)에서는 신규매수만 쉽니다."
    )