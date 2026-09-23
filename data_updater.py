import time
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
import pandas_market_calendars as mcal

TICKER = "SOXL"
DATA_FILE = "SOXL_adjusted.csv"
LOOKBACK_DAYS = 14
NY_TIMEZONE = ZoneInfo("America/New_York")
YAHOO_URLS = [
    "https://query1.finance.yahoo.com/v8/finance/chart/SOXL",
    "https://query2.finance.yahoo.com/v8/finance/chart/SOXL",
]
NASDAQ_URL = "https://api.nasdaq.com/api/quote/SOXL/historical"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nasdaq.com/",
}


def load_existing_data(file_path=DATA_FILE):
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"기존 데이터 파일을 찾을 수 없습니다: {path.resolve()}")
    df = pd.read_csv(path)
    df.columns = [str(c).strip() for c in df.columns]
    required = ["Date", "Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"필수 컬럼이 없습니다: {missing}")
    return normalize_dataframe(df)


def normalize_dataframe(df):
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    required = ["Date", "Open", "High", "Low", "Close", "Volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(f"가격 데이터 필수 컬럼 누락: {missing}")
    df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
    try:
        if df["Date"].dt.tz is not None:
            df["Date"] = df["Date"].dt.tz_localize(None)
    except AttributeError:
        pass
    for col in ["Open", "High", "Low", "Close", "Volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return (
        df[required]
        .dropna(subset=["Date", "Open", "High", "Low", "Close"])
        .sort_values("Date")
        .drop_duplicates(subset=["Date"], keep="last")
        .reset_index(drop=True)
    )


def get_download_end_date():
    """Yahoo의 end는 exclusive. 미국 동부시간 기준 당일 장 마감 후에는 다음날을 end로 사용."""
    now_ny = datetime.now(NY_TIMEZONE)
    today_ny = now_ny.date()
    # 장 마감 직후 데이터 지연을 감안하되, 수동 실행 시 오전에도 전일까지는 항상 포함.
    if now_ny.hour >= 16:
        return today_ny + timedelta(days=1)
    return today_ny


def download_yfinance_download(start_date, end_date):
    try:
        df = yf.download(
            tickers=TICKER,
            start=start_date,
            end=end_date,
            interval="1d",
            auto_adjust=True,
            actions=False,
            repair=True,
            prepost=False,
            progress=False,
            threads=False,
            multi_level_index=False,
        )
        if df is None or df.empty:
            return pd.DataFrame()
        return normalize_dataframe(df.reset_index())
    except Exception as e:
        print(f"yfinance.download 실패: {e}")
        return pd.DataFrame()


def download_yfinance_history():
    """start/end 경로가 지연될 때를 대비한 별도 yfinance history 경로."""
    try:
        df = yf.Ticker(TICKER).history(
            period="1mo",
            interval="1d",
            auto_adjust=True,
            actions=False,
            repair=True,
            prepost=False,
        )
        if df is None or df.empty:
            return pd.DataFrame()
        out = df.reset_index()
        if "Datetime" in out.columns and "Date" not in out.columns:
            out = out.rename(columns={"Datetime": "Date"})
        return normalize_dataframe(out)
    except Exception as e:
        print(f"yfinance.history 실패: {e}")
        return pd.DataFrame()


def download_yahoo_chart_api(url, start_date, end_date):
    start_dt = datetime(start_date.year, start_date.month, start_date.day, tzinfo=NY_TIMEZONE)
    end_dt = datetime(end_date.year, end_date.month, end_date.day, tzinfo=NY_TIMEZONE)
    params = {
        "period1": int(start_dt.timestamp()),
        "period2": int(end_dt.timestamp()),
        "interval": "1d",
        "includePrePost": "false",
        "events": "div,splits",
    }
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=20)
        r.raise_for_status()
        payload = r.json()
        chart = payload.get("chart", {})
        if chart.get("error"):
            raise RuntimeError(chart["error"])
        results = chart.get("result") or []
        if not results:
            return pd.DataFrame()
        result = results[0]
        timestamps = result.get("timestamp") or []
        indicators = result.get("indicators", {})
        quote_list = indicators.get("quote") or []
        if not quote_list:
            return pd.DataFrame()
        q = quote_list[0]
        adj_list = indicators.get("adjclose") or []
        adj = (adj_list[0].get("adjclose") if adj_list else None) or []
        rows = []
        for i, ts in enumerate(timestamps):
            try:
                raw_close = q.get("close", [])[i]
                raw_open = q.get("open", [])[i]
                raw_high = q.get("high", [])[i]
                raw_low = q.get("low", [])[i]
                if None in (raw_open, raw_high, raw_low, raw_close):
                    continue
                adj_close = adj[i] if i < len(adj) and adj[i] is not None else raw_close
                factor = float(adj_close) / float(raw_close) if raw_close else 1.0
                dt = datetime.fromtimestamp(ts, tz=NY_TIMEZONE)
                vols = q.get("volume", [])
                vol = vols[i] if i < len(vols) and vols[i] is not None else 0
                rows.append({
                    "Date": pd.Timestamp(dt.date()),
                    "Open": float(raw_open) * factor,
                    "High": float(raw_high) * factor,
                    "Low": float(raw_low) * factor,
                    "Close": float(adj_close),
                    "Volume": float(vol),
                })
            except (IndexError, TypeError, ValueError, ZeroDivisionError):
                continue
        return normalize_dataframe(pd.DataFrame(rows))
    except Exception as e:
        print(f"Yahoo Chart API 실패 ({url.split('/')[2]}): {e}")
        return pd.DataFrame()


def _parse_price(v):
    if v is None:
        return None
    s = str(v).replace("$", "").replace(",", "").strip()
    if not s or s in {"N/A", "--"}:
        return None
    return float(s)


def download_nasdaq_recent(start_date, end_date):
    """Yahoo 계열이 모두 지연/차단될 때 신규 일봉을 보완하는 독립 소스."""
    params = {
        "assetclass": "etf",
        "fromdate": start_date.strftime("%m/%d/%Y"),
        "limit": 100,
    }
    try:
        r = requests.get(NASDAQ_URL, params=params, headers=HEADERS, timeout=20)
        r.raise_for_status()
        payload = r.json()
        data = payload.get("data") or {}
        table = data.get("tradesTable") or {}
        rows_raw = table.get("rows") or []
        rows = []
        for x in rows_raw:
            try:
                dt = pd.to_datetime(x.get("date"), errors="coerce")
                if pd.isna(dt):
                    continue
                if dt.date() < start_date or dt.date() >= end_date:
                    continue
                o = _parse_price(x.get("open"))
                h = _parse_price(x.get("high"))
                l = _parse_price(x.get("low"))
                c = _parse_price(x.get("close"))
                v = _parse_price(x.get("volume")) or 0
                if None in (o, h, l, c):
                    continue
                rows.append({"Date": dt.normalize(), "Open": o, "High": h, "Low": l, "Close": c, "Volume": v})
            except Exception:
                continue
        return normalize_dataframe(pd.DataFrame(rows)) if rows else pd.DataFrame()
    except Exception as e:
        print(f"Nasdaq API 실패: {e}")
        return pd.DataFrame()


def download_recent_data(start_date, end_date):
    sources = []
    candidates = []

    funcs = [
        ("YFINANCE_DOWNLOAD", lambda: download_yfinance_download(start_date, end_date)),
        ("YFINANCE_HISTORY", download_yfinance_history),
        ("YAHOO_QUERY1", lambda: download_yahoo_chart_api(YAHOO_URLS[0], start_date, end_date)),
        ("YAHOO_QUERY2", lambda: download_yahoo_chart_api(YAHOO_URLS[1], start_date, end_date)),
        ("NASDAQ", lambda: download_nasdaq_recent(start_date, end_date)),
    ]

    for name, fn in funcs:
        df = fn()
        if df is not None and not df.empty:
            # 조회 범위로 제한. history(period=1mo)는 범위를 넓게 반환할 수 있음.
            df = df[(df["Date"].dt.date >= start_date) & (df["Date"].dt.date < end_date)].copy()
        if df is not None and not df.empty:
            print(f"{name:18s} 마지막 날짜: {df['Date'].max().date()} / {len(df)}행")
            candidates.append(df)
            sources.append(name)
        else:
            print(f"{name:18s} 마지막 날짜: 없음")

    if not candidates:
        return pd.DataFrame(), "NONE"

    # 오래된 소스가 더 최신 데이터를 덮어쓰지 않도록, 모든 소스를 합치되 날짜별로
    # 가장 마지막에 추가된 소스 값을 사용. Yahoo 계열 다음 Nasdaq 순서이며,
    # Nasdaq은 최근 신규행 확보를 위한 fallback 역할.
    combined = pd.concat(candidates, ignore_index=True)
    combined = normalize_dataframe(combined)
    return combined, "+".join(sources)


def get_expected_latest_trading_day():
    """Return the latest fully completed NYSE trading day in New York time."""
    now_ny = datetime.now(NY_TIMEZONE)
    today = pd.Timestamp(now_ny.date())
    # Before 16:30 ET, treat today's bar as not yet finalized.
    end_day = today if (now_ny.hour > 16 or (now_ny.hour == 16 and now_ny.minute >= 30)) else today - pd.Timedelta(days=1)
    start_day = end_day - pd.Timedelta(days=10)
    nyse = mcal.get_calendar("NYSE")
    sched = nyse.schedule(start_date=start_day.date(), end_date=end_day.date())
    if sched.empty:
        raise RuntimeError("NYSE 거래일 캘린더에서 최근 완료 거래일을 계산하지 못했습니다.")
    return pd.Timestamp(sched.index[-1]).normalize()


def update_soxl_data(file_path=DATA_FILE):
    existing = load_existing_data(file_path)
    old_last = existing["Date"].max().normalize()
    expected_last = get_expected_latest_trading_day()
    start_date = (old_last - pd.Timedelta(days=LOOKBACK_DAYS)).date()
    end_date = get_download_end_date()

    print(f"현재 CSV 마지막 날짜 : {old_last.date()}")
    print(f"완료된 최신 거래일    : {expected_last.date()}")
    print(f"재조회 시작일        : {start_date}")
    print(f"재조회 종료일        : {end_date} (exclusive)")
    print(f"yfinance 버전        : {getattr(yf, '__version__', 'unknown')}")

    # 이미 최신이면 파일을 다시 쓰지 않고 정상 종료한다.
    if old_last >= expected_last:
        msg = f"이미 최신 데이터입니다: {old_last.date()}"
        print(msg)
        return {
            "updated": False,
            "rows_added": 0,
            "rows_refreshed": 0,
            "old_last_date": old_last.date(),
            "new_last_date": old_last.date(),
            "downloaded_last_date": old_last.date(),
            "expected_last_date": expected_last.date(),
            "source": "NONE",
            "message": msg,
        }

    downloaded = pd.DataFrame()
    source = "NONE"
    for attempt in range(1, 4):
        print(f"\n=== 다운로드 시도 {attempt}/3 ===")
        downloaded, source = download_recent_data(start_date, end_date)
        if not downloaded.empty and downloaded["Date"].max().normalize() >= expected_last:
            break
        if attempt < 3:
            time.sleep(8)

    if downloaded.empty:
        raise RuntimeError("모든 데이터 소스가 빈 결과를 반환했습니다. Actions 로그를 확인하세요.")

    downloaded_last = downloaded["Date"].max().normalize()
    print(f"통합 다운로드 마지막 날짜: {downloaded_last.date()} ({source})")

    # 완료된 최신 거래일까지 확보하지 못했으면 성공 처리하지 않는다.
    if downloaded_last < expected_last:
        raise RuntimeError(
            "최신 완료 거래일 데이터 확보 실패: "
            f"CSV={old_last.date()}, 다운로드={downloaded_last.date()}, 기대={expected_last.date()}, source={source}"
        )

    # 기존 이력은 그대로 보존하고, 실제 신규 날짜만 append한다.
    new_rows = downloaded[(downloaded["Date"] > old_last) & (downloaded["Date"] <= expected_last)].copy()
    new_rows = normalize_dataframe(new_rows)
    if new_rows.empty:
        raise RuntimeError(
            f"다운로드는 {downloaded_last.date()}까지 왔지만 CSV {old_last.date()} 이후 신규 행이 없습니다."
        )

    merged = pd.concat([existing, new_rows], ignore_index=True)
    merged = normalize_dataframe(merged)
    new_last = merged["Date"].max().normalize()
    rows_added = len(new_rows)

    if new_last < expected_last:
        raise RuntimeError(
            f"병합 후 최신일이 기대 거래일보다 오래되었습니다: 병합={new_last.date()}, 기대={expected_last.date()}"
        )

    merged.to_csv(file_path, index=False)
    msg = f"SOXL 데이터 업데이트 완료: {old_last.date()} → {new_last.date()} ({source})"
    print(msg)

    return {
        "updated": True,
        "rows_added": int(rows_added),
        "rows_refreshed": 0,
        "old_last_date": old_last.date(),
        "new_last_date": new_last.date(),
        "downloaded_last_date": downloaded_last.date(),
        "expected_last_date": expected_last.date(),
        "source": source,
        "message": msg,
    }


if __name__ == "__main__":
    result = update_soxl_data()
    print("\n=== UPDATE RESULT ===")
    for k, v in result.items():
        print(f"{k:22s}: {v}")
