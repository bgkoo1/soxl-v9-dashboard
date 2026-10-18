import pandas as pd
import numpy as np
from SOXL_V10_backtest import run_v10_backtest

from SOXL_V9_backtest import (
    add_v9_signals,
    is_risk_b,
    calculate_v9_statistics,
    INITIAL_CAPITAL,
    POSITION_DIVISOR,
    MAX_POSITIONS,
    TARGET_RETURN,
    TB3_LOC_PLUS_DOLLAR,
    HOLDING_DAYS,
    RESERVE_RATIO,
)

CRASH_THRESHOLD = -0.09
CRASH_LOC_MULTIPLIER = 0.91


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
