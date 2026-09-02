import pandas as pd
import numpy as np

from backtest_engine import load_data, run_backtest


DATA_FILE = "SOXL_adjusted.csv"

INITIAL_CAPITAL = 100_000_000

BASE_DIVISOR = 7

MAX_POSITIONS = 7
TARGET_RETURN = 0.027
HOLDING_DAYS = 7
RESERVE_RATIO = 0.0


# ============================================================
# TEST PARAMETERS
# ============================================================

RISK_DIVISORS = [
    7,
    8,
    9,
    10,
    12,
    14,
    16,
    20,
    None,   # 위험구간 신규매수 중단
]


# ============================================================
# INDICATORS
# ============================================================

def add_market_signals(df):

    data = df.copy()

    data["Date"] = pd.to_datetime(
        data["Date"]
    )

    data = (
        data
        .sort_values("Date")
        .reset_index(drop=True)
    )

    # --------------------------------------------------------
    # MA200
    # --------------------------------------------------------

    data["MA200"] = (
        data["Close"]
        .rolling(
            window=200,
            min_periods=200,
        )
        .mean()
    )

    # --------------------------------------------------------
    # MA200 괴리율
    # --------------------------------------------------------

    data["MA200_Gap"] = (
        data["Close"]
        / data["MA200"]
        - 1
    )

    # --------------------------------------------------------
    # 최근 20거래일 수익률
    # --------------------------------------------------------

    data["Momentum_20D"] = (
        data["Close"]
        / data["Close"].shift(20)
        - 1
    )

    # --------------------------------------------------------
    # 실제 주문에서는 전일 확정정보만 사용
    # --------------------------------------------------------

    data["Signal_MA200_Gap"] = (
        data["MA200_Gap"]
        .shift(1)
    )

    data["Signal_Momentum_20D"] = (
        data["Momentum_20D"]
        .shift(1)
    )

    return data


# ============================================================
# RISK B CONDITION
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

    # --------------------------------------------------------
    # Risk B
    #
    # MA200 대비 -15% 이하
    #
    # AND
    #
    # 최근 20일 수익률:
    # -20% 이상 ~ -5% 미만
    # --------------------------------------------------------

    return (
        ma_gap < -0.15
        and
        -0.20 <= momentum < -0.05
    )


# ============================================================
# STATISTICS
# ============================================================

def calculate_statistics(
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
        equity[
            "Equity"
        ].iloc[-1]
    )


    start_date = pd.Timestamp(
        equity[
            "Date"
        ].iloc[0]
    )

    end_date = pd.Timestamp(
        equity[
            "Date"
        ].iloc[-1]
    )


    years = (
        end_date
        - start_date
    ).days / 365.25


    if years > 0:

        cagr = (
            final_equity
            / initial_equity
        ) ** (
            1 / years
        ) - 1

    else:

        cagr = 0.0


    # --------------------------------------------------------
    # MDD
    # --------------------------------------------------------

    running_max = (
        equity[
            "Equity"
        ]
        .cummax()
    )


    drawdown = (
        equity[
            "Equity"
        ]
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
    # TRADES
    # --------------------------------------------------------

    total_trades = len(
        trades
    )


    if total_trades > 0:

        wins = trades[
            trades[
                "Return"
            ] > 0
        ]

        losses = trades[
            trades[
                "Return"
            ] <= 0
        ]


        win_rate = (
            trades[
                "Return"
            ] > 0
        ).mean()


        target_rate = (
            trades[
                "Exit_Type"
            ] == "TARGET"
        ).mean()


        time_rate = (
            trades[
                "Exit_Type"
            ] == "TIME"
        ).mean()


        avg_return = (
            trades[
                "Return"
            ].mean()
        )


        avg_win = (
            wins[
                "Return"
            ].mean()
            if len(wins) > 0
            else 0.0
        )


        avg_loss = (
            losses[
                "Return"
            ].mean()
            if len(losses) > 0
            else 0.0
        )


        gross_profit = (
            trades.loc[
                trades[
                    "Profit"
                ] > 0,
                "Profit",
            ]
            .sum()
        )


        gross_loss = abs(
            trades.loc[
                trades[
                    "Profit"
                ] < 0,
                "Profit",
            ]
            .sum()
        )


        if gross_loss > 0:

            profit_factor = (
                gross_profit
                / gross_loss
            )

        else:

            profit_factor = np.inf


        worst_trade = float(
            trades[
                "Return"
            ].min()
        )


        best_trade = float(
            trades[
                "Return"
            ].max()
        )


        avg_holding_days = float(
            trades[
                "Holding_Days"
            ].mean()
        )


    else:

        win_rate = 0.0
        target_rate = 0.0
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

        "target_rate":
            float(target_rate),

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
# ADAPTIVE BACKTEST
# ============================================================

def run_risk_divisor_backtest(
    raw_df,
    risk_divisor,
    initial_capital=INITIAL_CAPITAL,
):

    df = add_market_signals(
        raw_df
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

    risk_buy_days = 0

    risk_skip_days = 0

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
            row[
                "Date"
            ]
        )


        close_price = float(
            row[
                "Close"
            ]
        )


        # ----------------------------------------------------
        # 당일 매도대금은 당일 신규매수에 사용하지 않음
        # ----------------------------------------------------

        cash_before_sales = float(
            cash
        )


        sale_proceeds_today = 0.0


        remaining_positions = []


        # ====================================================
        # 1. 기존 포지션 청산
        # ====================================================

        for position in positions:

            holding_days = (
                i
                - position[
                    "entry_index"
                ]
            )


            entry_price = float(
                position[
                    "entry_price"
                ]
            )


            shares = float(
                position[
                    "shares"
                ]
            )


            invested = float(
                position[
                    "invested"
                ]
            )


            target_price = (
                entry_price
                * (
                    1
                    + TARGET_RETURN
                )
            )


            exit_type = None


            # ------------------------------------------------
            # LOC
            #
            # 종가가 목표가 이상인 날의 종가로 청산
            # ------------------------------------------------

            if (
                close_price
                >= target_price
            ):

                exit_type = (
                    "TARGET"
                )


            # ------------------------------------------------
            # TIME
            #
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

                    "Target_Price":
                        target_price,

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

                    "Risk_Entry":
                        position[
                            "risk_entry"
                        ],

                    "Buy_Divisor":
                        position[
                            "buy_divisor"
                        ],

                    "Signal_MA200_Gap":
                        position[
                            "signal_ma_gap"
                        ],

                    "Signal_Momentum_20D":
                        position[
                            "signal_momentum"
                        ],
                }
            )


        positions = (
            remaining_positions
        )


        # ====================================================
        # 2. 오늘 시장상태
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


        if risk_state:

            risk_days += 1


        # ====================================================
        # 3. 신규 매수 목표금액
        # ====================================================

        if risk_state:

            # ------------------------------------------------
            # None = 신규매수 중단
            # ------------------------------------------------

            if risk_divisor is None:

                target_buy_amount = 0.0

                effective_divisor = (
                    "CASH"
                )

            else:

                target_buy_amount = (
                    previous_nav
                    / risk_divisor
                )

                effective_divisor = (
                    risk_divisor
                )


        else:

            target_buy_amount = (
                previous_nav
                / BASE_DIVISOR
            )

            effective_divisor = (
                BASE_DIVISOR
            )


        # ====================================================
        # 4. 사용가능 현금
        # ====================================================

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
        # 5. 신규 MOC 매수
        # ====================================================

        if slot_available:

            if (
                risk_state
                and
                risk_divisor is None
            ):

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

                        "entry_index":
                            i,

                        "entry_price":
                            close_price,

                        "shares":
                            shares,

                        "invested":
                            actual_investment,

                        "risk_entry":
                            risk_state,

                        "buy_divisor":
                            effective_divisor,

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


                if risk_state:

                    risk_buy_days += 1

                else:

                    normal_buy_days += 1


            elif (
                target_buy_amount > 0
                and
                available_cash <= 0
            ):

                blocked_buy_days += 1


        # ====================================================
        # 6. END-OF-DAY CASH
        # ====================================================

        cash = (
            cash_before_sales
            - buy_amount
            + sale_proceeds_today
        )


        # ====================================================
        # 7. PORTFOLIO VALUE
        # ====================================================

        position_value = sum(

            position[
                "shares"
            ]
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

                "Signal_MA200_Gap":
                    ma_gap,

                "Signal_Momentum_20D":
                    momentum,

                "Buy_Divisor":
                    effective_divisor,

                "Target_Buy_Amount":
                    target_buy_amount,

                "Actual_Buy_Amount":
                    buy_amount,

                "Cash_Before_Sales":
                    cash_before_sales,

                "Sale_Proceeds_Today":
                    sale_proceeds_today,

                "Cash_End":
                    cash,

                "Equity":
                    total_equity,

                "Open_Positions":
                    len(positions),
            }
        )


        previous_nav = (
            total_equity
        )


    # ========================================================
    # 8. 마지막 거래일 잔여 포지션 청산
    # ========================================================

    if len(positions) > 0:

        last_row = (
            df.iloc[-1]
        )


        last_date = pd.Timestamp(
            last_row[
                "Date"
            ]
        )


        last_close = float(
            last_row[
                "Close"
            ]
        )


        for position in positions:

            shares = float(
                position[
                    "shares"
                ]
            )


            invested = float(
                position[
                    "invested"
                ]
            )


            entry_price = float(
                position[
                    "entry_price"
                ]
            )


            holding_days = (
                len(df)
                - 1
                - position[
                    "entry_index"
                ]
            )


            proceeds = (
                shares
                * last_close
            )


            cash += (
                proceeds
            )


            trades.append(
                {
                    "Entry_Date":
                        position[
                            "entry_date"
                        ],

                    "Exit_Date":
                        last_date,

                    "Entry_Price":
                        entry_price,

                    "Exit_Price":
                        last_close,

                    "Target_Price":
                        entry_price
                        * (
                            1
                            + TARGET_RETURN
                        ),

                    "Shares":
                        shares,

                    "Invested":
                        invested,

                    "Holding_Days":
                        holding_days,

                    "Return":
                        last_close
                        / entry_price
                        - 1,

                    "Profit":
                        proceeds
                        - invested,

                    "Exit_Type":
                        "END",

                    "Risk_Entry":
                        position[
                            "risk_entry"
                        ],

                    "Buy_Divisor":
                        position[
                            "buy_divisor"
                        ],

                    "Signal_MA200_Gap":
                        position[
                            "signal_ma_gap"
                        ],

                    "Signal_Momentum_20D":
                        position[
                            "signal_momentum"
                        ],
                }
            )


        positions = []


        equity_records[-1][
            "Cash"
        ] = cash

        equity_records[-1][
            "Position_Value"
        ] = 0.0

        equity_records[-1][
            "Equity"
        ] = cash

        equity_records[-1][
            "Open_Positions"
        ] = 0


    trades_df = (
        pd.DataFrame(
            trades
        )
    )


    equity_df = (
        pd.DataFrame(
            equity_records
        )
    )


    daily_df = (
        pd.DataFrame(
            daily_records
        )
    )


    equity_df[
        "Date"
    ] = pd.to_datetime(
        equity_df[
            "Date"
        ]
    )


    stats = calculate_statistics(
        trades_df,
        equity_df,
        initial_capital,
        max_open_positions,
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

        "risk_days":
            risk_days,

        "risk_buy_days":
            risk_buy_days,

        "risk_skip_days":
            risk_skip_days,

        "normal_buy_days":
            normal_buy_days,

        "blocked_buy_days":
            blocked_buy_days,

    }


# ============================================================
# LABEL
# ============================================================

def divisor_label(
    risk_divisor
):

    if risk_divisor is None:

        return "CASH"

    return f"/{risk_divisor}"


# ============================================================
# PERIOD SWEEP
# ============================================================

def run_period_sweep(
    full_df,
    start_date,
    end_date,
    period_name,
):

    mask = (
        (
            full_df[
                "Date"
            ]
            >= pd.Timestamp(
                start_date
            )
        )
        &
        (
            full_df[
                "Date"
            ]
            <= pd.Timestamp(
                end_date
            )
        )
    )


    df = (
        full_df[
            mask
        ]
        .copy()
        .reset_index(drop=True)
    )


    if len(df) < 250:

        return pd.DataFrame()


    print()
    print()
    print(
        "#" * 100
    )

    print(
        f"# {period_name}"
    )

    print(
        f"# "
        f"{df['Date'].min().date()} "
        f"~ "
        f"{df['Date'].max().date()}"
    )

    print(
        "#" * 100
    )


    rows = []


    for risk_divisor in (
        RISK_DIVISORS
    ):

        result = (
            run_risk_divisor_backtest(
                raw_df=df,
                risk_divisor=
                    risk_divisor,
                initial_capital=
                    INITIAL_CAPITAL,
            )
        )


        stats = (
            result[
                "stats"
            ]
        )


        rows.append(
            {
                "Period":
                    period_name,

                "Risk_Divisor":
                    divisor_label(
                        risk_divisor
                    ),

                "Final_Equity":
                    stats[
                        "final_equity"
                    ],

                "CAGR":
                    stats[
                        "cagr"
                    ],

                "MDD":
                    stats[
                        "mdd"
                    ],

                "Calmar":
                    stats[
                        "calmar"
                    ],

                "Profit_Factor":
                    stats[
                        "profit_factor"
                    ],

                "Win_Rate":
                    stats[
                        "win_rate"
                    ],

                "Target_Rate":
                    stats[
                        "target_rate"
                    ],

                "Time_Rate":
                    stats[
                        "time_rate"
                    ],

                "Worst_Trade":
                    stats[
                        "worst_trade"
                    ],

                "Risk_Days":
                    result[
                        "risk_days"
                    ],

                "Risk_Buy_Days":
                    result[
                        "risk_buy_days"
                    ],

                "Risk_Skip_Days":
                    result[
                        "risk_skip_days"
                    ],

                "Blocked_Days":
                    result[
                        "blocked_buy_days"
                    ],
            }
        )


    result_df = pd.DataFrame(
        rows
    )


    # --------------------------------------------------------
    # PRINT
    # --------------------------------------------------------

    print()


    print(
        result_df[
            [
                "Risk_Divisor",
                "Final_Equity",
                "CAGR",
                "MDD",
                "Calmar",
                "Profit_Factor",
                "Risk_Buy_Days",
                "Risk_Skip_Days",
            ]
        ]
        .to_string(
            index=False,
            formatters={
                "Final_Equity":
                    lambda x:
                        f"{x:,.0f}",

                "CAGR":
                    lambda x:
                        f"{x:.2%}",

                "MDD":
                    lambda x:
                        f"{x:.2%}",

                "Calmar":
                    lambda x:
                        f"{x:.3f}",

                "Profit_Factor":
                    lambda x:
                        f"{x:.3f}",
            }
        )
    )


    # --------------------------------------------------------
    # RANKINGS
    # --------------------------------------------------------

    print()
    print(
        "-" * 100
    )

    print(
        "CALMAR RANKING"
    )

    print(
        "-" * 100
    )


    calmar_rank = (
        result_df
        .sort_values(
            "Calmar",
            ascending=False,
        )
        [
            [
                "Risk_Divisor",
                "CAGR",
                "MDD",
                "Calmar",
                "Final_Equity",
            ]
        ]
    )


    print(
        calmar_rank.to_string(
            index=False,
            formatters={
                "CAGR":
                    lambda x:
                        f"{x:.2%}",

                "MDD":
                    lambda x:
                        f"{x:.2%}",

                "Calmar":
                    lambda x:
                        f"{x:.3f}",

                "Final_Equity":
                    lambda x:
                        f"{x:,.0f}",
            }
        )
    )


    print()
    print(
        "-" * 100
    )

    print(
        "CAGR RANKING"
    )

    print(
        "-" * 100
    )


    cagr_rank = (
        result_df
        .sort_values(
            "CAGR",
            ascending=False,
        )
        [
            [
                "Risk_Divisor",
                "CAGR",
                "MDD",
                "Calmar",
                "Final_Equity",
            ]
        ]
    )


    print(
        cagr_rank.to_string(
            index=False,
            formatters={
                "CAGR":
                    lambda x:
                        f"{x:.2%}",

                "MDD":
                    lambda x:
                        f"{x:.2%}",

                "Calmar":
                    lambda x:
                        f"{x:.3f}",

                "Final_Equity":
                    lambda x:
                        f"{x:,.0f}",
            }
        )
    )


    return result_df


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    df = load_data(
        DATA_FILE
    )


    df[
        "Date"
    ] = pd.to_datetime(
        df[
            "Date"
        ]
    )


    print(
        "=" * 100
    )

    print(
        "SOXL RISK B POSITION-SIZE SENSITIVITY TEST"
    )

    print(
        "=" * 100
    )


    print(
        f"데이터: "
        f"{df['Date'].min().date()} "
        f"~ "
        f"{df['Date'].max().date()}"
    )


    print(
        f"거래일: "
        f"{len(df):,}"
    )


    print()
    print(
        "Risk B 조건:"
    )

    print(
        "  전일 MA200 괴리율 < -15%"
    )

    print(
        "  AND 전일 20거래일 모멘텀 >= -20%"
    )

    print(
        "  AND 전일 20거래일 모멘텀 < -5%"
    )


    # ========================================================
    # BASE COMPATIBILITY CHECK
    # ========================================================

    official_base = (
        run_backtest(
            df=df,
            initial_capital=
                INITIAL_CAPITAL,
            position_divisor=
                7,
            max_positions=
                7,
            target_return=
                TARGET_RETURN,
            holding_days=
                HOLDING_DAYS,
            reserve_ratio=
                RESERVE_RATIO,
        )
    )


    sweep_base = (
        run_risk_divisor_backtest(
            raw_df=df,
            risk_divisor=7,
            initial_capital=
                INITIAL_CAPITAL,
        )
    )


    official_final = (
        official_base[
            "stats"
        ][
            "final_equity"
        ]
    )


    sweep_final = (
        sweep_base[
            "stats"
        ][
            "final_equity"
        ]
    )


    difference = (
        sweep_final
        - official_final
    )


    print()
    print(
        "=" * 100
    )

    print(
        "BASE COMPATIBILITY CHECK"
    )

    print(
        "=" * 100
    )


    print(
        f"기존 V7 최종자산 : "
        f"{official_final:,.0f}"
    )


    print(
        f"Risk /7 최종자산 : "
        f"{sweep_final:,.0f}"
    )


    print(
        f"차이              : "
        f"{difference:,.6f}"
    )


    # ========================================================
    # FULL
    # ========================================================

    full_result = (
        run_period_sweep(
            full_df=df,
            start_date=
                df[
                    "Date"
                ].min(),
            end_date=
                df[
                    "Date"
                ].max(),
            period_name=
                "FULL",
        )
    )


    # ========================================================
    # DEVELOPMENT
    # ========================================================

    development_result = (
        run_period_sweep(
            full_df=df,
            start_date=
                "2010-03-11",
            end_date=
                "2017-12-31",
            period_name=
                "DEVELOPMENT",
        )
    )


    # ========================================================
    # VALIDATION
    # ========================================================

    validation_result = (
        run_period_sweep(
            full_df=df,
            start_date=
                "2018-01-01",
            end_date=
                df[
                    "Date"
                ].max(),
            period_name=
                "VALIDATION",
        )
    )


    # ========================================================
    # COMBINED RESULT
    # ========================================================

    combined = pd.concat(
        [
            full_result,
            development_result,
            validation_result,
        ],
        ignore_index=True,
    )


    # --------------------------------------------------------
    # VALIDATION 중심 최종 비교
    # --------------------------------------------------------

    validation_table = (
        combined[
            combined[
                "Period"
            ] == "VALIDATION"
        ]
        .copy()
        .sort_values(
            "Calmar",
            ascending=False,
        )
    )


    print()
    print()
    print(
        "=" * 100
    )

    print(
        "FINAL VALIDATION RANKING"
    )

    print(
        "=" * 100
    )


    print(
        validation_table[
            [
                "Risk_Divisor",
                "CAGR",
                "MDD",
                "Calmar",
                "Profit_Factor",
                "Final_Equity",
            ]
        ]
        .to_string(
            index=False,
            formatters={
                "CAGR":
                    lambda x:
                        f"{x:.2%}",

                "MDD":
                    lambda x:
                        f"{x:.2%}",

                "Calmar":
                    lambda x:
                        f"{x:.3f}",

                "Profit_Factor":
                    lambda x:
                        f"{x:.3f}",

                "Final_Equity":
                    lambda x:
                        f"{x:,.0f}",
            }
        )
    )


    # ========================================================
    # CSV SAVE
    # ========================================================

    output_file = (
        "SOXL_risk_divisor_sensitivity.csv"
    )


    combined.to_csv(
        output_file,
        index=False,
        encoding="utf-8-sig",
    )


    print()
    print(
        f"결과 저장 완료: "
        f"{output_file}"
    )