
import pandas as pd
import numpy as np
from pathlib import Path

from backtest_engine import load_data


# ============================================================
# V9 STRATEGY
# V8 + TB3 Dynamic LOC
# ============================================================

DATA_FILE = "SOXL_adjusted.csv"

INITIAL_CAPITAL = 100_000_000

POSITION_DIVISOR = 7
MAX_POSITIONS = 7

TARGET_RETURN = 0.027
TB3_LOC_PLUS_DOLLAR = 0.10

HOLDING_DAYS = 7
RESERVE_RATIO = 0.0


# ============================================================
# SIGNALS
# ============================================================

def add_v9_signals(df):
    """
    모든 실제 주문 판단에는 '전일 확정 정보'만 사용한다.

    Risk B:
        MA200 괴리율 < -15%
        AND
        최근 20거래일 수익률 -20% 이상 ~ -5% 미만

    TB3:
        MA5 < MA20 < MA50

    TB3는 신규매수를 막는 조건이 아니다.
    기존 보유 포지션의 LOC 목표만 조정한다.
    """

    data = df.copy()

    data["Date"] = pd.to_datetime(
        data["Date"],
        errors="coerce",
    )

    data = (
        data
        .dropna(subset=["Date", "Close"])
        .sort_values("Date")
        .drop_duplicates("Date", keep="last")
        .reset_index(drop=True)
    )

    # --------------------------------------------------------
    # Moving averages
    # --------------------------------------------------------

    for window in [5, 20, 50, 200]:

        data[f"MA{window}"] = (
            data["Close"]
            .rolling(
                window=window,
                min_periods=window,
            )
            .mean()
        )

    # --------------------------------------------------------
    # Risk B indicators
    # --------------------------------------------------------

    data["MA200_Gap"] = (
        data["Close"]
        / data["MA200"]
        - 1
    )

    data["Momentum_20D"] = (
        data["Close"]
        / data["Close"].shift(20)
        - 1
    )

    # --------------------------------------------------------
    # Previous-day confirmed signals
    # --------------------------------------------------------

    data["Signal_MA5"] = (
        data["MA5"]
        .shift(1)
    )

    data["Signal_MA20"] = (
        data["MA20"]
        .shift(1)
    )

    data["Signal_MA50"] = (
        data["MA50"]
        .shift(1)
    )

    data["Signal_MA200_Gap"] = (
        data["MA200_Gap"]
        .shift(1)
    )

    data["Signal_Momentum_20D"] = (
        data["Momentum_20D"]
        .shift(1)
    )

    # --------------------------------------------------------
    # TB3
    # MA5 < MA20 < MA50
    # --------------------------------------------------------

    data["TB3_State"] = (
        (
            data["Signal_MA5"]
            < data["Signal_MA20"]
        )
        &
        (
            data["Signal_MA20"]
            < data["Signal_MA50"]
        )
    ).fillna(False)

    return data


# ============================================================
# RISK B
# ============================================================

def is_risk_b(
    ma_gap,
    momentum,
):

    if (
        pd.isna(ma_gap)
        or pd.isna(momentum)
    ):
        return False

    return (
        ma_gap < -0.15
        and
        -0.20 <= momentum < -0.05
    )


# ============================================================
# STATISTICS
# ============================================================

def calculate_v9_statistics(
    trades,
    equity,
    initial_capital,
    max_open_positions,
):

    if equity.empty:
        raise ValueError(
            "Equity 데이터가 없습니다."
        )

    initial_equity = float(
        initial_capital
    )

    final_equity = float(
        equity["Equity"].iloc[-1]
    )

    start_date = pd.Timestamp(
        equity["Date"].iloc[0]
    )

    end_date = pd.Timestamp(
        equity["Date"].iloc[-1]
    )

    years = (
        end_date
        - start_date
    ).days / 365.25

    if years > 0:
        cagr = (
            final_equity
            / initial_equity
        ) ** (1 / years) - 1
    else:
        cagr = 0.0

    # --------------------------------------------------------
    # MDD
    # --------------------------------------------------------

    running_max = (
        equity["Equity"]
        .cummax()
    )

    drawdown = (
        equity["Equity"]
        / running_max
        - 1
    )

    mdd = float(
        drawdown.min()
    )

    if mdd < 0:
        calmar = (
            cagr
            / abs(mdd)
        )
    else:
        calmar = np.inf

    # --------------------------------------------------------
    # Trade statistics
    # --------------------------------------------------------

    total_trades = len(
        trades
    )

    if total_trades > 0:

        wins = trades[
            trades["Return"] > 0
        ]

        losses = trades[
            trades["Return"] <= 0
        ]

        win_rate = float(
            (
                trades["Return"] > 0
            ).mean()
        )

        normal_loc_rate = float(
            (
                trades["Exit_Type"]
                == "NORMAL_LOC"
            ).mean()
        )

        dynamic_loc_rate = float(
            (
                trades["Exit_Type"]
                == "TB3_DYN_LOC"
            ).mean()
        )

        loc_rate = float(
            trades["Exit_Type"]
            .isin(
                [
                    "NORMAL_LOC",
                    "TB3_DYN_LOC",
                ]
            )
            .mean()
        )

        time_rate = float(
            (
                trades["Exit_Type"]
                == "TIME"
            ).mean()
        )

        avg_return = float(
            trades["Return"].mean()
        )

        avg_win = float(
            wins["Return"].mean()
            if len(wins) > 0
            else 0.0
        )

        avg_loss = float(
            losses["Return"].mean()
            if len(losses) > 0
            else 0.0
        )

        gross_profit = float(
            trades.loc[
                trades["Profit"] > 0,
                "Profit",
            ]
            .sum()
        )

        gross_loss = abs(
            float(
                trades.loc[
                    trades["Profit"] < 0,
                    "Profit",
                ]
                .sum()
            )
        )

        if gross_loss > 0:
            profit_factor = (
                gross_profit
                / gross_loss
            )
        else:
            profit_factor = np.inf

        worst_trade = float(
            trades["Return"].min()
        )

        best_trade = float(
            trades["Return"].max()
        )

        avg_holding_days = float(
            trades["Holding_Days"].mean()
        )

    else:

        win_rate = 0.0

        normal_loc_rate = 0.0
        dynamic_loc_rate = 0.0
        loc_rate = 0.0
        time_rate = 0.0

        avg_return = 0.0
        avg_win = 0.0
        avg_loss = 0.0

        profit_factor = 0.0

        worst_trade = 0.0
        best_trade = 0.0

        avg_holding_days = 0.0

    return {

        "initial_equity":
            initial_equity,

        "final_equity":
            final_equity,

        "cagr":
            float(cagr),

        "mdd":
            float(mdd),

        "calmar":
            float(calmar),

        "total_trades":
            int(total_trades),

        "win_rate":
            float(win_rate),

        "loc_rate":
            float(loc_rate),

        "normal_loc_rate":
            float(normal_loc_rate),

        "dynamic_loc_rate":
            float(dynamic_loc_rate),

        "time_rate":
            float(time_rate),

        "avg_return":
            float(avg_return),

        "avg_win":
            float(avg_win),

        "avg_loss":
            float(avg_loss),

        "profit_factor":
            float(profit_factor),

        "worst_trade":
            worst_trade,

        "best_trade":
            best_trade,

        "avg_holding_days":
            avg_holding_days,

        "max_open_positions":
            int(max_open_positions),
    }


# ============================================================
# V9 BACKTEST
# ============================================================

def run_v9_backtest(
    raw_df,
    initial_capital=INITIAL_CAPITAL,
    start_date=None,
    end_date=None,
):
    """
    V9 rules
    --------
    1. Normal new buy:
       previous trading day's NAV / 7

    2. Risk B:
       skip NEW buys only

    3. Normal LOC:
       Entry Price * 1.027

    4. TB3 Dynamic LOC:
       if previous-day MA5 < MA20 < MA50,
       active LOC becomes Entry Price + $0.10

       This is reversible.
       When TB3 is no longer true,
       the active target returns to +2.7%.

    5. TIME:
       Entry Day = Day 0
       force exit at Close when holding_days >= 7

    6. Same-day sale proceeds:
       cannot fund same-day MOC buy.
    """

    # --------------------------------------------------------
    # IMPORTANT:
    # indicators are calculated BEFORE date slicing
    # so MA warm-up history is preserved.
    # --------------------------------------------------------

    df = add_v9_signals(
        raw_df
    )

    if start_date is not None:

        start_date = pd.Timestamp(
            start_date
        )

        df = df[
            df["Date"] >= start_date
        ].copy()

    if end_date is not None:

        end_date = pd.Timestamp(
            end_date
        )

        df = df[
            df["Date"] <= end_date
        ].copy()

    df = (
        df
        .reset_index(drop=False)
        .rename(
            columns={
                "index":
                    "Original_Index",
            }
        )
    )

    if df.empty:
        raise ValueError(
            "선택한 기간에 데이터가 없습니다."
        )

    cash = float(
        initial_capital
    )

    positions = []

    trades = []

    equity_records = []

    daily_records = []

    previous_nav = float(
        initial_capital
    )

    max_open_positions = 0

    risk_days = 0
    risk_skip_days = 0

    tb3_days = 0

    normal_buy_days = 0
    blocked_buy_days = 0


    # ========================================================
    # DAILY LOOP
    # ========================================================

    for i in range(
        len(df)
    ):

        row = df.iloc[i]

        date = pd.Timestamp(
            row["Date"]
        )

        close_price = float(
            row["Close"]
        )

        original_index = int(
            row["Original_Index"]
        )

        # ----------------------------------------------------
        # Same-day sale proceeds cannot fund same-day buy
        # ----------------------------------------------------

        cash_before_sales = float(
            cash
        )

        sale_proceeds_today = 0.0

        remaining_positions = []


        # ====================================================
        # 1. Current confirmed market state
        # ====================================================

        ma_gap = row[
            "Signal_MA200_Gap"
        ]

        momentum = row[
            "Signal_Momentum_20D"
        ]

        risk_state = is_risk_b(
            ma_gap,
            momentum,
        )

        tb3_state = bool(
            row["TB3_State"]
        )

        if risk_state:
            risk_days += 1

        if tb3_state:
            tb3_days += 1


        # ====================================================
        # 2. Existing position exits
        # ====================================================

        for position in positions:

            holding_days = (
                original_index
                - position[
                    "entry_original_index"
                ]
            )

            entry_price = float(
                position["entry_price"]
            )

            shares = float(
                position["shares"]
            )

            invested = float(
                position["invested"]
            )

            normal_target_price = (
                entry_price
                * (
                    1
                    + TARGET_RETURN
                )
            )

            # ------------------------------------------------
            # Reversible Dynamic LOC
            # ------------------------------------------------

            if tb3_state:

                active_target_price = (
                    entry_price
                    + TB3_LOC_PLUS_DOLLAR
                )

                target_mode = (
                    "TB3_DYN_LOC"
                )

            else:

                active_target_price = (
                    normal_target_price
                )

                target_mode = (
                    "NORMAL_LOC"
                )

            exit_type = None

            # ------------------------------------------------
            # LOC
            # Earliest possible exit is next trading day
            # because positions are created after this exit loop.
            # ------------------------------------------------

            if (
                close_price
                >= active_target_price
            ):

                exit_type = (
                    target_mode
                )

            # ------------------------------------------------
            # TIME
            # Entry Day = Day 0
            # ------------------------------------------------

            elif (
                holding_days
                >= HOLDING_DAYS
            ):

                exit_type = (
                    "TIME"
                )

            if exit_type is None:

                remaining_positions.append(
                    position
                )

                continue

            exit_price = (
                close_price
            )

            proceeds = (
                shares
                * exit_price
            )

            return_rate = (
                exit_price
                / entry_price
                - 1
            )

            profit = (
                proceeds
                - invested
            )

            sale_proceeds_today += (
                proceeds
            )

            trades.append(
                {
                    "Entry_Date":
                        position[
                            "entry_date"
                        ],

                    "Exit_Date":
                        date,

                    "Entry_Price":
                        entry_price,

                    "Exit_Price":
                        exit_price,

                    "Normal_Target_Price":
                        normal_target_price,

                    "Active_Target_Price":
                        active_target_price,

                    "Shares":
                        shares,

                    "Invested":
                        invested,

                    "Holding_Days":
                        holding_days,

                    "Return":
                        return_rate,

                    "Profit":
                        profit,

                    "Exit_Type":
                        exit_type,

                    "TB3_On_Exit":
                        tb3_state,

                    "Risk_Entry":
                        position[
                            "risk_entry"
                        ],

                    "Signal_MA200_Gap_At_Entry":
                        position[
                            "signal_ma_gap"
                        ],

                    "Signal_Momentum_20D_At_Entry":
                        position[
                            "signal_momentum"
                        ],
                }
            )

        positions = (
            remaining_positions
        )


        # ====================================================
        # 3. New buy target amount
        # ====================================================

        target_buy_amount = (
            previous_nav
            / POSITION_DIVISOR
        )

        reserve_amount = (
            previous_nav
            * RESERVE_RATIO
        )

        available_cash = max(
            0.0,
            cash_before_sales
            - reserve_amount
        )

        buy_amount = 0.0

        slot_available = (
            len(positions)
            < MAX_POSITIONS
        )


        # ====================================================
        # 4. New MOC buy
        # ====================================================

        if slot_available:

            # ------------------------------------------------
            # V8 Risk B
            # skip NEW buy only
            # ------------------------------------------------

            if risk_state:

                risk_skip_days += 1

            elif (
                target_buy_amount > 0
                and
                available_cash > 0
            ):

                buy_amount = min(
                    target_buy_amount,
                    available_cash,
                )

                shares = (
                    buy_amount
                    / close_price
                )

                actual_investment = (
                    shares
                    * close_price
                )

                positions.append(
                    {
                        "entry_date":
                            date,

                        "entry_original_index":
                            original_index,

                        "entry_price":
                            close_price,

                        "shares":
                            shares,

                        "invested":
                            actual_investment,

                        "risk_entry":
                            risk_state,

                        "signal_ma_gap":
                            (
                                float(ma_gap)
                                if pd.notna(ma_gap)
                                else np.nan
                            ),

                        "signal_momentum":
                            (
                                float(momentum)
                                if pd.notna(momentum)
                                else np.nan
                            ),
                    }
                )

                normal_buy_days += 1

            elif (
                target_buy_amount > 0
                and
                available_cash <= 0
            ):

                blocked_buy_days += 1


        # ====================================================
        # 5. End-of-day cash
        # ====================================================

        cash = (
            cash_before_sales
            - buy_amount
            + sale_proceeds_today
        )


        # ====================================================
        # 6. Portfolio value
        # ====================================================

        position_value = sum(

            position["shares"]
            * close_price

            for position
            in positions

        )

        total_equity = (
            cash
            + position_value
        )

        max_open_positions = max(
            max_open_positions,
            len(positions),
        )

        equity_records.append(
            {
                "Date":
                    date,

                "Cash":
                    cash,

                "Position_Value":
                    position_value,

                "Equity":
                    total_equity,

                "Open_Positions":
                    len(positions),
            }
        )

        daily_records.append(
            {
                "Date":
                    date,

                "Previous_NAV":
                    previous_nav,

                "Risk_State":
                    risk_state,

                "TB3_State":
                    tb3_state,

                "Signal_MA5":
                    row["Signal_MA5"],

                "Signal_MA20":
                    row["Signal_MA20"],

                "Signal_MA50":
                    row["Signal_MA50"],

                "Signal_MA200_Gap":
                    ma_gap,

                "Signal_Momentum_20D":
                    momentum,

                "Target_Buy_Amount":
                    target_buy_amount,

                "Actual_Buy_Amount":
                    buy_amount,

                "Cash_Before_Sales":
                    cash_before_sales,

                "Sale_Proceeds_Today":
                    sale_proceeds_today,

                "End_Cash":
                    cash,

                "Open_Positions":
                    len(positions),

                "Equity":
                    total_equity,
            }
        )

        previous_nav = (
            total_equity
        )


    # ========================================================
    # OUTPUT DATAFRAMES
    # ========================================================

    trades_df = pd.DataFrame(
        trades
    )

    equity_df = pd.DataFrame(
        equity_records
    )

    daily_df = pd.DataFrame(
        daily_records
    )


    # ========================================================
    # STATISTICS
    # ========================================================

    stats = calculate_v9_statistics(
        trades=trades_df,
        equity=equity_df,
        initial_capital=initial_capital,
        max_open_positions=max_open_positions,
    )

    stats.update(
        {
            "risk_days":
                int(risk_days),

            "risk_skip_days":
                int(risk_skip_days),

            "tb3_days":
                int(tb3_days),

            "normal_buy_days":
                int(normal_buy_days),

            "blocked_buy_days":
                int(blocked_buy_days),
        }
    )

    return {

        "stats":
            stats,

        "trades":
            trades_df,

        "equity":
            equity_df,

        "daily":
            daily_df,

        "open_positions":
            positions,

        "signal_data":
            df,
    }


# ============================================================
# PRINT SUMMARY
# ============================================================

def print_v9_summary(
    result,
    label="V9",
):

    stats = result[
        "stats"
    ]

    print(
        "\n"
        + "=" * 70
    )

    print(
        label
    )

    print(
        "=" * 70
    )

    print(
        f"Initial Equity : "
        f"{stats['initial_equity']:,.0f}"
    )

    print(
        f"Final Equity   : "
        f"{stats['final_equity']:,.0f}"
    )

    print(
        f"CAGR           : "
        f"{stats['cagr']:.2%}"
    )

    print(
        f"MDD            : "
        f"{stats['mdd']:.2%}"
    )

    print(
        f"Calmar         : "
        f"{stats['calmar']:.3f}"
    )

    print(
        f"Profit Factor  : "
        f"{stats['profit_factor']:.3f}"
    )

    print(
        f"Win Rate       : "
        f"{stats['win_rate']:.2%}"
    )

    print(
        f"LOC Rate       : "
        f"{stats['loc_rate']:.2%}"
    )

    print(
        f"  Normal LOC   : "
        f"{stats['normal_loc_rate']:.2%}"
    )

    print(
        f"  TB3 Dyn LOC  : "
        f"{stats['dynamic_loc_rate']:.2%}"
    )

    print(
        f"TIME Rate      : "
        f"{stats['time_rate']:.2%}"
    )

    print(
        f"Avg Trade      : "
        f"{stats['avg_return']:.3%}"
    )

    print(
        f"Worst Trade    : "
        f"{stats['worst_trade']:.2%}"
    )

    print(
        f"Risk B Days    : "
        f"{stats['risk_days']:,}"
    )

    print(
        f"Risk B Skips   : "
        f"{stats['risk_skip_days']:,}"
    )

    print(
        f"TB3 Days       : "
        f"{stats['tb3_days']:,}"
    )

    print(
        "=" * 70
    )


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    raw_df = load_data(
        DATA_FILE
    )

    # --------------------------------------------------------
    # Primary evaluation window:
    # 2022+ as agreed for V9 comparison
    # --------------------------------------------------------

    result_2022 = run_v9_backtest(
        raw_df,
        initial_capital=INITIAL_CAPITAL,
        start_date="2022-01-01",
    )

    print_v9_summary(
        result_2022,
        label=(
            "V9 | "
            "V8 + TB3 Dynamic LOC | "
            "2022+"
        ),
    )

    # --------------------------------------------------------
    # Optional full-history output
    # --------------------------------------------------------

    result_all = run_v9_backtest(
        raw_df,
        initial_capital=INITIAL_CAPITAL,
    )

    print_v9_summary(
        result_all,
        label=(
            "V9 | "
            "Full History"
        ),
    )

    # --------------------------------------------------------
    # Save outputs
    # --------------------------------------------------------

    result_2022[
        "trades"
    ].to_csv(
        "SOXL_V9_trades_2022plus.csv",
        index=False,
        encoding="utf-8-sig",
    )

    result_2022[
        "equity"
    ].to_csv(
        "SOXL_V9_equity_2022plus.csv",
        index=False,
        encoding="utf-8-sig",
    )

    result_2022[
        "daily"
    ].to_csv(
        "SOXL_V9_daily_2022plus.csv",
        index=False,
        encoding="utf-8-sig",
    )

    print(
        "\nSaved:"
    )

    print(
        "SOXL_V9_trades_2022plus.csv"
    )

    print(
        "SOXL_V9_equity_2022plus.csv"
    )

    print(
        "SOXL_V9_daily_2022plus.csv"
    )
