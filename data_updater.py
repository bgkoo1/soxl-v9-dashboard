import pandas as pd
import yfinance as yf
import requests

from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


TICKER = "SOXL"
DATA_FILE = "SOXL_adjusted.csv"

LOOKBACK_DAYS = 10

NY_TIMEZONE = ZoneInfo("America/New_York")

YAHOO_CHART_URL = (
    "https://query1.finance.yahoo.com"
    "/v8/finance/chart/SOXL"
)


# ============================================================
# EXISTING DATA
# ============================================================

def load_existing_data(
    file_path=DATA_FILE
):
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(
            f"기존 데이터 파일을 찾을 수 없습니다: "
            f"{path.resolve()}"
        )

    df = pd.read_csv(path)

    df.columns = [
        str(c).strip()
        for c in df.columns
    ]

    required = [
        "Date",
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    ]

    missing = [
        c for c in required
        if c not in df.columns
    ]

    if missing:
        raise ValueError(
            f"필수 컬럼이 없습니다: {missing}"
        )

    df["Date"] = pd.to_datetime(
        df["Date"],
        errors="coerce"
    )

    for col in [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    ]:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )

    df = (
        df
        .dropna(
            subset=[
                "Date",
                "Open",
                "High",
                "Low",
                "Close",
            ]
        )
        .sort_values("Date")
        .drop_duplicates(
            subset=["Date"],
            keep="last"
        )
        .reset_index(drop=True)
    )

    return df


# ============================================================
# DOWNLOAD END DATE
# ============================================================

def get_download_end_date():
    now_ny = datetime.now(
        NY_TIMEZONE
    )

    today_ny = now_ny.date()

    market_data_ready = (
        now_ny.hour > 16
        or (
            now_ny.hour == 16
            and now_ny.minute >= 15
        )
    )

    if market_data_ready:
        return (
            today_ny
            + timedelta(days=1)
        )

    return today_ny


# ============================================================
# NORMALIZE
# ============================================================

def normalize_dataframe(df):
    if df is None or df.empty:
        return pd.DataFrame()

    df = df.copy()

    required = [
        "Date",
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    ]

    missing = [
        c for c in required
        if c not in df.columns
    ]

    if missing:
        raise ValueError(
            f"가격 데이터 필수 컬럼 누락: {missing}"
        )

    df["Date"] = pd.to_datetime(
        df["Date"],
        errors="coerce"
    )

    if df["Date"].dt.tz is not None:
        df["Date"] = (
            df["Date"]
            .dt.tz_localize(None)
        )

    for col in [
        "Open",
        "High",
        "Low",
        "Close",
        "Volume",
    ]:
        df[col] = pd.to_numeric(
            df[col],
            errors="coerce"
        )

    df = (
        df[required]
        .dropna(
            subset=[
                "Date",
                "Open",
                "High",
                "Low",
                "Close",
            ]
        )
        .sort_values("Date")
        .drop_duplicates(
            subset=["Date"],
            keep="last"
        )
        .reset_index(drop=True)
    )

    return df


# ============================================================
# PRIMARY: YFINANCE
# ============================================================

def download_yfinance(
    start_date,
    end_date
):
    print()
    print(
        "[1차] yfinance 조회"
    )

    try:
        df = yf.download(
            tickers=TICKER,
            start=start_date,
            end=end_date,
            interval="1d",
            auto_adjust=True,
            actions=False,
            repair=False,
            prepost=False,
            progress=False,
            threads=False,
            multi_level_index=False,
        )

        if df is None or df.empty:
            return pd.DataFrame()

        df = df.reset_index()

        df.columns = [
            str(c).strip()
            for c in df.columns
        ]

        return normalize_dataframe(
            df
        )

    except Exception as e:
        print(
            f"yfinance 조회 실패: {e}"
        )

        return pd.DataFrame()


# ============================================================
# FALLBACK: YAHOO CHART API
# ============================================================

def download_yahoo_chart_api(
    start_date,
    end_date
):
    print()
    print(
        "[2차] Yahoo Chart API 직접 조회"
    )

    start_dt = datetime(
        start_date.year,
        start_date.month,
        start_date.day,
        tzinfo=NY_TIMEZONE,
    )

    end_dt = datetime(
        end_date.year,
        end_date.month,
        end_date.day,
        tzinfo=NY_TIMEZONE,
    )

    period1 = int(
        start_dt.timestamp()
    )

    period2 = int(
        end_dt.timestamp()
    )

    params = {
        "period1": period1,
        "period2": period2,
        "interval": "1d",
        "includePrePost": "false",
        "events": "div,splits",
    }

    headers = {
        "User-Agent":
            (
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/120 Safari/537.36"
            )
    }

    try:
        response = requests.get(
            YAHOO_CHART_URL,
            params=params,
            headers=headers,
            timeout=15,
        )

        response.raise_for_status()

        payload = response.json()

        chart = payload.get(
            "chart",
            {}
        )

        error = chart.get(
            "error"
        )

        if error:
            raise RuntimeError(
                f"Yahoo API error: {error}"
            )

        results = chart.get(
            "result"
        )

        if not results:
            return pd.DataFrame()

        result = results[0]

        timestamps = (
            result.get(
                "timestamp"
            )
            or []
        )

        indicators = result.get(
            "indicators",
            {}
        )

        quote_list = indicators.get(
            "quote",
            []
        )

        if not quote_list:
            return pd.DataFrame()

        quote = quote_list[0]

        opens = quote.get(
            "open",
            []
        )

        highs = quote.get(
            "high",
            []
        )

        lows = quote.get(
            "low",
            []
        )

        closes = quote.get(
            "close",
            []
        )

        volumes = quote.get(
            "volume",
            []
        )

        rows = []

        for i, ts in enumerate(
            timestamps
        ):
            try:
                open_price = opens[i]
                high_price = highs[i]
                low_price = lows[i]
                close_price = closes[i]

                volume = (
                    volumes[i]
                    if i < len(volumes)
                    else 0
                )

                if (
                    open_price is None
                    or high_price is None
                    or low_price is None
                    or close_price is None
                ):
                    continue

                dt = datetime.fromtimestamp(
                    ts,
                    tz=NY_TIMEZONE,
                )

                rows.append(
                    {
                        "Date":
                            pd.Timestamp(
                                dt.date()
                            ),

                        "Open":
                            float(open_price),

                        "High":
                            float(high_price),

                        "Low":
                            float(low_price),

                        "Close":
                            float(close_price),

                        "Volume":
                            float(
                                volume or 0
                            ),
                    }
                )

            except (
                IndexError,
                TypeError,
                ValueError,
            ):
                continue

        return normalize_dataframe(
            pd.DataFrame(rows)
        )

    except Exception as e:
        print(
            f"Yahoo Chart API 조회 실패: {e}"
        )

        return pd.DataFrame()


# ============================================================
# COMBINE DOWNLOAD SOURCES
# ============================================================

def download_recent_data(
    start_date,
    end_date
):
    print(
        f"조회 범위 : "
        f"{start_date} ~ "
        f"{end_date} (exclusive)"
    )

    yf_df = download_yfinance(
        start_date,
        end_date,
    )

    api_df = (
        download_yahoo_chart_api(
            start_date,
            end_date,
        )
    )

    if not yf_df.empty:
        print(
            "yfinance 마지막 날짜   : "
            f"{yf_df['Date'].max().date()}"
        )
    else:
        print(
            "yfinance 마지막 날짜   : 없음"
        )

    if not api_df.empty:
        print(
            "Yahoo API 마지막 날짜  : "
            f"{api_df['Date'].max().date()}"
        )
    else:
        print(
            "Yahoo API 마지막 날짜  : 없음"
        )

    if (
        yf_df.empty
        and api_df.empty
    ):
        return (
            pd.DataFrame(),
            "NONE"
        )

    if yf_df.empty:
        return (
            api_df,
            "YAHOO_API"
        )

    if api_df.empty:
        return (
            yf_df,
            "YFINANCE"
        )

    # 두 소스 모두 있으면 병합
    # Yahoo API 값을 최종 우선값으로 사용
    combined = pd.concat(
        [
            yf_df,
            api_df,
        ],
        ignore_index=True,
    )

    combined = (
        combined
        .sort_values("Date")
        .drop_duplicates(
            subset=["Date"],
            keep="last",
        )
        .reset_index(drop=True)
    )

    return (
        combined,
        "YFINANCE+YAHOO_API"
    )


# ============================================================
# UPDATE
# ============================================================

def update_soxl_data(
    file_path=DATA_FILE
):
    existing_df = (
        load_existing_data(
            file_path
        )
    )

    old_last_date = (
        existing_df["Date"]
        .max()
        .normalize()
    )

    start_date = (
        old_last_date
        - pd.Timedelta(
            days=LOOKBACK_DAYS
        )
    ).date()

    end_date = (
        get_download_end_date()
    )

    print(
        f"현재 CSV 마지막 날짜 : "
        f"{old_last_date.date()}"
    )

    print(
        f"재조회 시작일        : "
        f"{start_date}"
    )

    print(
        f"재조회 종료일        : "
        f"{end_date} (exclusive)"
    )

    (
        downloaded_df,
        source,
    ) = download_recent_data(
        start_date,
        end_date,
    )

    if downloaded_df.empty:
        return {
            "updated": False,
            "rows_added": 0,
            "rows_refreshed": 0,
            "old_last_date":
                old_last_date.date(),
            "new_last_date":
                old_last_date.date(),
            "source": source,
            "message":
                (
                    "모든 다운로드 경로에서 "
                    "데이터를 받지 못했습니다. "
                    "기존 CSV는 변경하지 않았습니다."
                ),
        }

    downloaded_last_date = (
        downloaded_df["Date"]
        .max()
        .normalize()
    )

    # ========================================================
    # BACKUP
    # ========================================================

    file_path_obj = Path(
        file_path
    )

    backup_file = (
        file_path_obj
        .with_name(
            "SOXL_adjusted_backup.csv"
        )
    )

    existing_df.to_csv(
        backup_file,
        index=False,
        encoding="utf-8-sig",
    )

    # ========================================================
    # MERGE
    # ========================================================

    first_downloaded_date = (
        downloaded_df["Date"]
        .min()
        .normalize()
    )

    old_before_refresh = (
        existing_df[
            existing_df["Date"]
            < first_downloaded_date
        ]
        .copy()
    )

    combined_df = pd.concat(
        [
            old_before_refresh,
            downloaded_df,
        ],
        ignore_index=True,
    )

    combined_df = (
        combined_df
        .sort_values("Date")
        .drop_duplicates(
            subset=["Date"],
            keep="last",
        )
        .reset_index(drop=True)
    )

    new_last_date = (
        combined_df["Date"]
        .max()
        .normalize()
    )

    rows_added = int(
        (
            combined_df["Date"]
            > old_last_date
        ).sum()
    )

    rows_refreshed = int(
        (
            downloaded_df["Date"]
            <= old_last_date
        ).sum()
    )

    # ========================================================
    # SAVE
    # ========================================================

    combined_df.to_csv(
        file_path,
        index=False,
        encoding="utf-8-sig",
    )

    updated = (
        new_last_date
        > old_last_date
    )

    if updated:
        message = (
            f"{rows_added}개 신규 거래일 추가, "
            f"{rows_refreshed}개 최근 거래일 재검증"
        )
    else:
        message = (
            f"신규 거래일 없음, "
            f"{rows_refreshed}개 최근 거래일 재검증"
        )

    return {
        "updated":
            updated,

        "rows_added":
            rows_added,

        "rows_refreshed":
            rows_refreshed,

        "old_last_date":
            old_last_date.date(),

        "new_last_date":
            new_last_date.date(),

        "downloaded_last_date":
            downloaded_last_date.date(),

        "source":
            source,

        "message":
            message,
    }


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    print("=" * 76)
    print(
        "SOXL DATA UPDATER "
        "- DUAL SOURCE VERSION"
    )
    print("=" * 76)

    try:
        result = (
            update_soxl_data()
        )

        print()
        print("=" * 76)
        print("UPDATE RESULT")
        print("=" * 76)

        print(
            f"데이터 소스       : "
            f"{result['source']}"
        )

        print(
            f"업데이트 여부     : "
            f"{result['updated']}"
        )

        print(
            f"신규 거래일       : "
            f"{result['rows_added']}"
        )

        print(
            f"최근 재검증 거래일: "
            f"{result['rows_refreshed']}"
        )

        print(
            f"기존 마지막일     : "
            f"{result['old_last_date']}"
        )

        print(
            f"최신 마지막일     : "
            f"{result['new_last_date']}"
        )

        if (
            "downloaded_last_date"
            in result
        ):
            print(
                f"다운로드 마지막일 : "
                f"{result['downloaded_last_date']}"
            )

        print(
            f"메시지            : "
            f"{result['message']}"
        )

        print()
        print(
            "백업 파일         : "
            "SOXL_adjusted_backup.csv"
        )

    except Exception as e:
        print()
        print(
            f"업데이트 실패: {e}"
        )

        print(
            "기존 SOXL_adjusted.csv는 "
            "변경하지 않았습니다."
        )

        raise