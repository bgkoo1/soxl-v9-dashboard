import pandas as pd
import numpy as np
from pathlib import Path

FLOAT_TOL = 1e-10


def load_data(file_path):
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(
            f"데이터 파일을 찾을 수 없습니다: {path.resolve()}"
        )

    df = pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]

    required = ["Date", "Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required if c not in df.columns]

    if missing:
        raise ValueError(f"필수 컬럼이 없습니다: {missing}")

    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")

    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df = (
        df.dropna(subset=["Date", "Open", "High", "Low", "Close"])
        .sort_values("Date")
        .drop_duplicates("Date", keep="last")
        .reset_index(drop=True)
    )

    return df


def run_backtest(
    df,
    initial_capital=100_000_000,
    position_divisor=7,
    max_positions=7,
    target_return=0.027,
    holding_days=7,
    reserve_ratio=0.0,
):
    if initial_capital <= 0:
        raise ValueError("초기자산은 0보다 커야 합니다.")

    if position_divisor < 1:
        raise ValueError("분할 수는 1 이상이어야 합니다.")

    if max_positions < 1:
        raise ValueError("최대 포지션 수는 1 이상이어야 합니다.")

    if target_return <= 0:
        raise ValueError("LOC 목표수익률은 0보다 커야 합니다.")

    if holding_days < 1:
        raise ValueError("최대 보유기간은 1거래일 이상이어야 합니다.")

    if not 0 <= reserve_ratio < 1:
        raise ValueError("Reserve 비율은 0 이상 1 미만이어야 합니다.")

    available_cash = float(initial_capital)
    pending_cash = 0.0

    positions = []
    trades = []
    equity_records = []
    daily_records = []

    prev_equity = float(initial_capital)
    max_open_positions = 0

    for i, row in df.iterrows():
        date = row["Date"]
        close_price = float(row["Close"])

        # 전일 매도대금을 오늘부터 사용 가능
        released_cash = pending_cash
        available_cash += released_cash
        pending_cash = 0.0

        # 기존 포지션 청산
        remaining_positions = []
        today_sale_proceeds = 0.0

        for p in positions:
            elapsed_days = i - p["Entry_Index"]

            target_price = (
                p["Entry_Price"]
                * (1.0 + target_return)
            )

            exit_type = None
            exit_price = None

            # LOC
            if (
                elapsed_days >= 1
                and close_price + FLOAT_TOL >= target_price
            ):
                exit_type = "TARGET"
                exit_price = close_price

            # TIME
            elif elapsed_days >= holding_days:
                exit_type = "TIME"
                exit_price = close_price

            if exit_type is None:
                remaining_positions.append(p)
                continue

            proceeds = (
                p["Shares"]
                * exit_price
            )

            profit = (
                proceeds
                - p["Invested"]
            )

            return_rate = (
                exit_price
                / p["Entry_Price"]
                - 1.0
            )

            # 당일 매도대금은 당일 신규매수에 사용하지 않음
            today_sale_proceeds += proceeds

            trades.append(
                {
                    "Entry_Date": p["Entry_Date"],
                    "Exit_Date": date,
                    "Entry_Price": p["Entry_Price"],
                    "Target_Price": target_price,
                    "Exit_Price": exit_price,
                    "Shares": p["Shares"],
                    "Invested": p["Invested"],
                    "Holding_Days": elapsed_days,
                    "Return": return_rate,
                    "Profit": profit,
                    "Exit_Type": exit_type,
                }
            )

        positions = remaining_positions

        # 전일 NAV 기준 신규매수 금액
        target_buy_amount = (
            prev_equity
            / position_divisor
        )

        # Reserve 비율
        reserve_amount = (
            prev_equity
            * reserve_ratio
        )

        spendable_cash = max(
            0.0,
            available_cash - reserve_amount
        )

        actual_invested = 0.0

        if (
            len(positions) < max_positions
            and spendable_cash > FLOAT_TOL
            and target_buy_amount > FLOAT_TOL
        ):
            actual_invested = min(
                target_buy_amount,
                spendable_cash
            )

            shares = (
                actual_invested
                / close_price
            )

            available_cash -= actual_invested

            positions.append(
                {
                    "Entry_Date": date,
                    "Entry_Index": i,
                    "Entry_Price": close_price,
                    "Shares": shares,
                    "Invested": actual_invested,
                }
            )

        # 오늘 매도대금은 다음 거래일부터 사용 가능
        pending_cash = today_sale_proceeds

        # 일별 자산
        position_value = sum(
            p["Shares"] * close_price
            for p in positions
        )

        equity = (
            available_cash
            + pending_cash
            + position_value
        )

        max_open_positions = max(
            max_open_positions,
            len(positions)
        )

        equity_records.append(
            {
                "Date": date,
                "Available_Cash": available_cash,
                "Pending_Cash": pending_cash,
                "Position_Value": position_value,
                "Equity": equity,
                "Open_Positions": len(positions),
                "Target_Buy_Amount": target_buy_amount,
                "Actual_Invested": actual_invested,
            }
        )

        daily_records.append(
            {
                "Date": date,
                "Released_Cash": released_cash,
                "Today_Sale_Proceeds": today_sale_proceeds,
                "Target_Buy_Amount": target_buy_amount,
                "Actual_Invested": actual_invested,
                "Available_Cash_End": available_cash,
                "Pending_Cash_End": pending_cash,
                "Open_Positions_End": len(positions),
                "Equity_End": equity,
            }
        )

        prev_equity = equity

    # 마지막 데이터 날짜 잔여 포지션 평가청산
    if positions:
        last_row = df.iloc[-1]

        last_date = last_row["Date"]
        last_close = float(last_row["Close"])
        last_index = len(df) - 1

        final_liquidation = 0.0

        for p in positions:
            elapsed_days = (
                last_index
                - p["Entry_Index"]
            )

            target_price = (
                p["Entry_Price"]
                * (1.0 + target_return)
            )

            proceeds = (
                p["Shares"]
                * last_close
            )

            profit = (
                proceeds
                - p["Invested"]
            )

            return_rate = (
                last_close
                / p["Entry_Price"]
                - 1.0
            )

            final_liquidation += proceeds

            trades.append(
                {
                    "Entry_Date": p["Entry_Date"],
                    "Exit_Date": last_date,
                    "Entry_Price": p["Entry_Price"],
                    "Target_Price": target_price,
                    "Exit_Price": last_close,
                    "Shares": p["Shares"],
                    "Invested": p["Invested"],
                    "Holding_Days": elapsed_days,
                    "Return": return_rate,
                    "Profit": profit,
                    "Exit_Type": "END",
                }
            )

        available_cash += final_liquidation
        positions = []

        equity_records[-1]["Available_Cash"] = available_cash
        equity_records[-1]["Position_Value"] = 0.0

        equity_records[-1]["Equity"] = (
            available_cash
            + pending_cash
        )

        equity_records[-1]["Open_Positions"] = 0

    trades_df = pd.DataFrame(trades)
    equity_df = pd.DataFrame(equity_records)
    daily_df = pd.DataFrame(daily_records)

    stats = calculate_statistics(
        df=df,
        trades_df=trades_df,
        equity_df=equity_df,
        initial_capital=initial_capital,
        max_open_positions=max_open_positions,
    )

    yearly_df = calculate_yearly_performance(
        equity_df=equity_df,
        initial_capital=initial_capital,
    )

    drawdown_df = calculate_drawdown(
        equity_df
    )

    return {
        "stats": stats,
        "trades": trades_df,
        "equity": equity_df,
        "daily": daily_df,
        "yearly": yearly_df,
        "drawdown": drawdown_df,
    }


def calculate_statistics(
    df,
    trades_df,
    equity_df,
    initial_capital,
    max_open_positions,
):
    start_date = df["Date"].iloc[0]
    end_date = df["Date"].iloc[-1]

    years = (
        end_date - start_date
    ).days / 365.25

    final_equity = float(
        equity_df["Equity"].iloc[-1]
    )

    if years > 0 and final_equity > 0:
        cagr = (
            final_equity
            / initial_capital
        ) ** (
            1.0 / years
        ) - 1.0
    else:
        cagr = np.nan

    equity = (
        equity_df["Equity"]
        .astype(float)
    )

    running_max = (
        equity.cummax()
    )

    drawdown = (
        equity
        / running_max
        - 1.0
    )

    mdd = float(
        drawdown.min()
    )

    total_trades = len(
        trades_df
    )

    if total_trades == 0:
        return {
            "initial_equity": float(initial_capital),
            "final_equity": final_equity,
            "cagr": cagr,
            "mdd": mdd,
            "calmar": np.nan,
            "total_trades": 0,
            "win_rate": np.nan,
            "target_rate": np.nan,
            "time_rate": np.nan,
            "avg_return": np.nan,
            "avg_win": np.nan,
            "avg_loss": np.nan,
            "profit_factor": np.nan,
            "worst_trade": np.nan,
            "best_trade": np.nan,
            "avg_holding_days": np.nan,
            "max_open_positions": int(max_open_positions),
        }

    wins = (
        trades_df["Return"] > 0
    )

    losses = (
        trades_df["Return"] <= 0
    )

    target_mask = (
        trades_df["Exit_Type"]
        == "TARGET"
    )

    time_mask = (
        trades_df["Exit_Type"]
        == "TIME"
    )

    gross_profit = float(
        trades_df.loc[
            trades_df["Profit"] > 0,
            "Profit"
        ].sum()
    )

    gross_loss = abs(
        float(
            trades_df.loc[
                trades_df["Profit"] < 0,
                "Profit"
            ].sum()
        )
    )

    profit_factor = (
        gross_profit / gross_loss
        if gross_loss > 0
        else np.inf
    )

    return {
        "initial_equity": float(initial_capital),

        "final_equity": final_equity,

        "cagr": float(cagr),

        "mdd": mdd,

        "calmar": (
            float(cagr / abs(mdd))
            if mdd < 0
            else np.nan
        ),

        "total_trades": int(total_trades),

        "win_rate": float(
            wins.mean()
        ),

        "target_rate": float(
            target_mask.mean()
        ),

        "time_rate": float(
            time_mask.mean()
        ),

        "avg_return": float(
            trades_df["Return"].mean()
        ),

        "avg_win": (
            float(
                trades_df.loc[
                    wins,
                    "Return"
                ].mean()
            )
            if wins.any()
            else np.nan
        ),

        "avg_loss": (
            float(
                trades_df.loc[
                    losses,
                    "Return"
                ].mean()
            )
            if losses.any()
            else np.nan
        ),

        "profit_factor": float(
            profit_factor
        ),

        "worst_trade": float(
            trades_df["Return"].min()
        ),

        "best_trade": float(
            trades_df["Return"].max()
        ),

        "avg_holding_days": float(
            trades_df["Holding_Days"].mean()
        ),

        "max_open_positions": int(
            max_open_positions
        ),
    }


def calculate_drawdown(
    equity_df
):
    result = equity_df[
        ["Date", "Equity"]
    ].copy()

    result["Running_Max"] = (
        result["Equity"].cummax()
    )

    result["Drawdown"] = (
        result["Equity"]
        / result["Running_Max"]
        - 1.0
    )

    return result


def calculate_yearly_performance(
    equity_df,
    initial_capital,
):
    temp = equity_df[
        ["Date", "Equity"]
    ].copy()

    temp["Year"] = (
        temp["Date"].dt.year
    )

    rows = []
    previous_equity = float(
        initial_capital
    )

    for year, group in temp.groupby(
        "Year"
    ):
        year_end_equity = float(
            group["Equity"].iloc[-1]
        )

        year_return = (
            year_end_equity
            / previous_equity
            - 1.0
        )

        running_max = (
            group["Equity"].cummax()
        )

        year_drawdown = (
            group["Equity"]
            / running_max
            - 1.0
        )

        rows.append(
            {
                "Year": int(year),

                "Year_End_Equity":
                    year_end_equity,

                "Year_Return":
                    float(year_return),

                "Year_MDD":
                    float(
                        year_drawdown.min()
                    ),
            }
        )

        previous_equity = (
            year_end_equity
        )

    return pd.DataFrame(
        rows
    )