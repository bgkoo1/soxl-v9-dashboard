import pandas as pd
import numpy as np

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


def run_v10_backtest(raw_df, initial_capital=INITIAL_CAPITAL, start_date=None, end_date=None):
    """V10 = V9 + same-day crash LOC extra slot.

    - Base buy: previous NAV / 7 at the day's Close.
    - Extra crash buy: if today's Close <= previous trading day's Close * 0.91,
      buy one additional NAV/7 slot at today's Close.
    - Risk B blocks both new buys.
    - Maximum 7 open slots; same-day sale proceeds cannot fund same-day buys.
    - Existing V9 TB3 dynamic LOC and TIME 7 rules are unchanged.
    """
    df = add_v9_signals(raw_df)
    df["Prev_Close"] = df["Close"].shift(1)

    if start_date is not None:
        df = df[df["Date"] >= pd.Timestamp(start_date)].copy()
    if end_date is not None:
        df = df[df["Date"] <= pd.Timestamp(end_date)].copy()

    df = df.reset_index(drop=False).rename(columns={"index": "Original_Index"})
    if df.empty:
        raise ValueError("선택한 기간에 데이터가 없습니다.")

    cash = float(initial_capital)
    positions, trades, equity_records, daily_records = [], [], [], []
    previous_nav = float(initial_capital)
    max_open_positions = 0
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
        risk_state = is_risk_b(ma_gap, momentum)
        tb3_state = bool(row["TB3_State"])
        crash_state = bool(pd.notna(prev_close) and close_price <= prev_close * CRASH_LOC_MULTIPLIER)

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
            normal_target = entry_price * (1 + TARGET_RETURN)
            if tb3_state:
                active_target = entry_price + TB3_LOC_PLUS_DOLLAR
                target_mode = "TB3_DYN_LOC"
            else:
                active_target = normal_target
                target_mode = "NORMAL_LOC"

            exit_type = None
            if close_price >= active_target:
                exit_type = target_mode
            elif holding_days >= HOLDING_DAYS:
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

        target_buy_amount = previous_nav / POSITION_DIVISOR
        reserve_amount = previous_nav * RESERVE_RATIO
        available_cash = max(0.0, cash_before_sales - reserve_amount)
        total_buy_amount = 0.0
        buys_today = 0
        crash_extra_bought = False

        if len(positions) < MAX_POSITIONS:
            if risk_state:
                risk_skip_days += 1
            else:
                desired_buys = 2 if crash_state else 1
                for buy_index in range(desired_buys):
                    if len(positions) >= MAX_POSITIONS or available_cash <= 0 or target_buy_amount <= 0:
                        break
                    buy_amount = min(target_buy_amount, available_cash)
                    if buy_amount <= 0:
                        break
                    shares = buy_amount / close_price
                    position_seq += 1
                    buy_type = "급락 LOC (-9%)" if buy_index == 1 else "기본 MOC"
                    positions.append({
                        "position_id": f"{date.date().isoformat()}-{position_seq:06d}",
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
                    if buy_index == 1:
                        crash_extra_bought = True
                if buys_today > 0:
                    normal_buy_days += 1
                elif target_buy_amount > 0:
                    blocked_buy_days += 1
        elif target_buy_amount > 0 and not risk_state:
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
            "TB3_State": tb3_state,
            "Crash_State": crash_state,
            "Prev_Close": prev_close,
            "Crash_LOC_Price": prev_close * CRASH_LOC_MULTIPLIER if pd.notna(prev_close) else np.nan,
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
        "crash_threshold": float(CRASH_THRESHOLD),
    })
    return {
        "stats": stats,
        "trades": trades_df,
        "equity": equity_df,
        "daily": daily_df,
        "open_positions": positions,
        "signal_data": df,
    }
