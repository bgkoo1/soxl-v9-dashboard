"""V12 Final 1.0. Pure calculations; USD prices and USD capital throughout.

D0 is the entry session. Forced exits override LOC. Entry filters use only
the previous confirmed session. Research fills assume closing auctions fill
at Close, allow fractional shares, and have no fees/slippage.
"""
from copy import deepcopy
import math
import numpy as np
import pandas as pd

SLOTS = 6
TARGET = 1.032
CRASH = .91


def high_vol(value):
    # Account only for machine precision at the exact 0.1000 boundary.
    return pd.notna(value) and (value >= .10 or math.isclose(float(value), .10, rel_tol=0, abs_tol=1e-12))


def prepare(raw):
    data = raw[["Date", "Close"]].copy()
    data["Date"] = pd.to_datetime(data["Date"], errors="raise").dt.normalize()
    data["Close"] = pd.to_numeric(data["Close"], errors="raise")
    if data.empty or data["Date"].isna().any() or data["Date"].duplicated().any():
        raise ValueError("날짜 누락·중복 또는 빈 가격 데이터입니다.")
    if not np.isfinite(data["Close"]).all() or (data["Close"] <= 0).any():
        raise ValueError("종가는 유한한 양수여야 합니다.")
    data = data.sort_values("Date").reset_index(drop=True)
    c = data["Close"]
    data["ma_gap"] = c / c.rolling(200, min_periods=200).mean() - 1
    data["momentum"] = c / c.shift(20) - 1
    data["vol20"] = np.log(c / c.shift()).rolling(20, min_periods=20).std(ddof=1)
    data["risk_b"] = (data.ma_gap < -.15) & (data.momentum >= -.20) & (data.momentum < -.05)
    streak, count = [], 0
    for falling in c.diff().lt(0):
        count = count + 1 if falling else 0
        streak.append(count)
    data["down_streak"] = streak
    return data


def exit_reason(position, holding_days):
    """One sell order per position, regardless of the number of triggers."""
    if position.get("full_pending"):
        return "조건부 전체 MOC"
    if position.get("d2_pending"):
        return "D2 손절"
    if holding_days >= 7:
        return "D7 MOC"
    return "LOC +3.2%"


def backtest(raw, initial_usd=100000., start=None, end=None):
    if not math.isfinite(initial_usd) or initial_usd <= 0:
        raise ValueError("초기 USD 자산을 확인하세요.")
    data = prepare(raw)
    selected = data.index[data["Date"].between(
        pd.Timestamp(start) if start else data.Date.iloc[0],
        pd.Timestamp(end) if end else data.Date.iloc[-1])]
    # Full previous-session MA200 is required before the first entry.
    selected = [i for i in selected if i >= 200]
    if not selected:
        raise ValueError("전일 MA200 계산을 위한 200거래일 준비 기간이 필요합니다.")
    cash, previous_nav = float(initial_usd), float(initial_usd)
    positions, fills, daily = [], [], []
    seq = 0
    for i in selected:
        row, prev = data.iloc[i], data.iloc[i - 1]
        close = float(row.Close)
        cash_before_sales, proceeds = cash, 0.
        survivors = []
        for p in positions:
            age = i - p["entry_index"]
            reason = exit_reason(p, age)
            if reason == "LOC +3.2%" and close < p["entry_price"] * TARGET:
                survivors.append(p)
                continue
            amount = p["quantity"] * close
            proceeds += amount
            fills.append({"slot_id": p["slot_id"], "entry_date": p["entry_date"],
                          "exit_date": row.Date, "entry_price": p["entry_price"],
                          "exit_price": close, "quantity": p["quantity"], "reason": reason,
                          "profit_usd": amount - p["quantity"] * p["entry_price"],
                          "return": close / p["entry_price"] - 1, "holding_days": age})
        positions = survivors
        d2 = False
        for p in positions:
            if i - p["entry_index"] == 2:
                p["d2_pending"] = close / p["entry_price"] - 1 <= -.10 + 1e-12
                d2 |= p["d2_pending"]
        blocked = bool(prev.risk_b or prev.down_streak >= 4)
        crash = close <= float(prev.Close) * CRASH
        available, bought = cash_before_sales, 0.
        count = 0
        if not blocked:
            for _ in range(2 if crash else 1):
                if len(positions) >= SLOTS or available <= 1e-9:
                    break
                budget = min(previous_nav / SLOTS, available)
                seq += 1
                positions.append({"slot_id": f"V12-{seq:06d}", "entry_date": row.Date,
                                  "entry_index": i, "entry_price": close,
                                  "quantity": budget / close,
                                  "buy_type": "기본 MOC" if count == 0 else "급락 LOC",
                                  "d2_pending": False, "full_pending": False})
                available -= budget
                bought += budget
                count += 1
        full = bool(d2 and high_vol(row.vol20))
        if full:
            for p in positions:
                p["full_pending"] = True
        cash = cash_before_sales - bought + proceeds
        nav = cash + sum(p["quantity"] * close for p in positions)
        daily.append({"Date": row.Date, "NAV_USD": nav, "Cash_USD": cash,
                      "Slots": len(positions), "Buy_Slots": count,
                      "Buy_USD": bought, "Cash_Before_Sales": cash_before_sales,
                      "Sale_USD": proceeds, "Risk_B": bool(prev.risk_b),
                      "Paused": bool(prev.down_streak >= 4), "Vol20": row.vol20,
                      "Full_Exit_Signal": full})
        previous_nav = nav
    equity = pd.DataFrame(daily)
    peak = equity.NAV_USD.cummax().clip(lower=initial_usd)
    equity["Drawdown"] = equity.NAV_USD / peak - 1
    years = (equity.Date.iloc[-1] - equity.Date.iloc[0]).days / 365.25
    total = equity.NAV_USD.iloc[-1] / initial_usd - 1
    mdd = float(equity.Drawdown.min())
    cagr = (1 + total) ** (1 / years) - 1 if years > 0 else None
    return {"equity": equity, "trades": pd.DataFrame(fills),
            "positions": deepcopy(positions), "data": data,
            "stats": {"total_return": total, "cagr": cagr, "mdd": mdd,
                      "calmar": cagr / abs(mdd) if cagr is not None and mdd < 0 else None}}


def plan(raw, holdings, cash_usd, next_session, closed_trades=None):
    """Next-session plan for actual user lots; never changes holdings.

    Reconstruct pending D2 signals from recorded entry price and historical
    closes. A missed MOC remains overdue until a real fill is recorded.
    """
    data = prepare(raw)
    cash_usd = float(cash_usd)
    if not math.isfinite(cash_usd) or cash_usd < 0:
        raise ValueError("USD 예수금은 0 이상의 숫자여야 합니다.")
    if pd.Timestamp(next_session) <= data.Date.iloc[-1]:
        raise ValueError("주문 예정일은 확정 종가일 이후여야 합니다.")
    latest = data.iloc[-1]
    ref = float(latest.Close)
    positions, issues, full_pending = [], [], False
    for item in holdings:
        p = dict(item)
        entry = pd.Timestamp(p["entry_date"])
        indexes = data.index[data.Date == entry]
        price, qty = float(p["entry_price"]), float(p["quantity"])
        if not (math.isfinite(price) and math.isfinite(qty) and price > 0 and qty > 0):
            raise ValueError("매수가·수량을 확인하세요.")
        if len(indexes) != 1:
            issues.append(f"{p['slot_id']}: 매수일 가격 기록이 없어 주문 판정 불가")
            continue
        entry_i = int(indexes[0])
        p["age_next"] = len(data) - entry_i
        d2_i = entry_i + 2
        p["d2_pending"] = False
        p["full_pending"] = False
        if d2_i < len(data):
            d2 = data.iloc[d2_i]
            triggered = float(d2.Close) / price - 1 <= -.10 + 1e-12
            p["d2_pending"] = triggered
            if triggered and high_vol(d2.vol20):
                # Only lots already bought on that signal date are targets.
                p["full_signal_date"] = d2.Date
        positions.append(p)
    signal_dates = [p["full_signal_date"] for p in positions if "full_signal_date" in p]
    # A triggering lot can have filled while another full-exit target remains
    # unfilled. Its journal record must keep the remaining MOC obligation alive.
    for t in closed_trades or []:
        indexes = data.index[data.Date == pd.Timestamp(t["entry_date"])]
        if len(indexes) != 1:
            continue
        d2_i = int(indexes[0]) + 2
        if d2_i < len(data):
            d2 = data.iloc[d2_i]
            if (pd.Timestamp(t["exit_date"]) > d2.Date and
                    float(d2.Close) / float(t["entry_price"]) - 1 <= -.10 + 1e-12 and
                    high_vol(d2.vol20)):
                signal_dates.append(d2.Date)
    sells = []
    for p in positions:
        p["full_pending"] = any(pd.Timestamp(p["entry_date"]) <= d for d in signal_dates)
        full_pending |= p["full_pending"]
        reason = exit_reason(p, p["age_next"])
        sells.append({"슬롯": p["slot_id"], "매수일": p["entry_date"],
                      "주문일": str(pd.Timestamp(next_session).date()), "보유일": f"D{p['age_next']}",
                      "구분": "LOC" if reason == "LOC +3.2%" else "MOC",
                      "사유": reason, "수량": float(p["quantity"]),
                      "지정가($)": float(p["entry_price"]) * TARGET if reason == "LOC +3.2%" else None})
    nav = cash_usd + sum(float(p["quantity"]) * ref for p in holdings)
    blocked = bool(latest.risk_b or latest.down_streak >= 4 or
                   pd.isna(latest.ma_gap) or issues or len(holdings) > SLOTS)
    # Do not pre-release occupied slots from merely planned sells.
    room = max(0, SLOTS - len(holdings))
    available, buys = cash_usd, []
    if not blocked:
        for kind in ["기본 MOC", "급락 LOC"][:room]:
            budget = min(nav / SLOTS, available)
            limit = ref if kind == "기본 MOC" else ref * CRASH
            qty = math.floor(budget / limit)
            if qty < 1:
                break
            buys.append({"구분": kind, "배정예산($)": budget,
                         "참고수량(주)": qty, "지정가($)": None if kind == "기본 MOC" else limit})
            available -= budget
    return {"sells": sells, "buys": buys, "nav": nav, "reference": ref,
            "cash": cash_usd, "blocked": blocked, "issues": issues,
            "risk_b": bool(latest.risk_b), "streak": int(latest.down_streak),
            "vol20": float(latest.vol20), "full_pending": full_pending}
