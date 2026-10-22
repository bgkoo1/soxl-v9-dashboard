import time
from pathlib import Path
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf
try:
    import pandas_market_calendars as mcal
except ModuleNotFoundError:
    mcal = None

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
        "todate": (end_date - timedelta(days=1)).strftime("%m/%d/%Y"),
        "limit": 100,
        "offset": 0,
    }
    try:
        r = requests.get(NASDAQ_URL, params=params, headers=HEADERS, timeout=20)
        r.raise_for_status()
        payload = r.json()
        api_status = payload.get("status") or {}
        if isinstance(api_status, dict) and api_status.get("rCode") not in (None, 200):
            raise RuntimeError(f"Nasdaq API status: {api_status}")
        data = payload.get("data") or {}
        table = data.get("tradesTable") or {}
        rows_raw = table.get("rows") or []
        print(f"NASDAQ_DIAGNOSTIC http={r.status_code} status={payload.get('status')} message={payload.get('message')} data_keys={list(data) if isinstance(data, dict) else type(data).__name__} table_keys={list(table) if isinstance(table, dict) else type(table).__name__} raw_rows={len(rows_raw)}")
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
        print(f"NASDAQ_DIAGNOSTIC parsed_rows={len(rows)} requested_start={start_date} requested_end={end_date}")
        return normalize_dataframe(pd.DataFrame(rows)) if rows else pd.DataFrame()
    except Exception as e:
        print(f"Nasdaq API 실패: {type(e).__name__}: {e}")
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
    """Return the latest fully completed NYSE trading day in New York time.

    pandas_market_calendars is optional so importing data_updater never prevents
    the Streamlit app from starting. GitHub Actions installs the package and
    therefore uses the exact NYSE calendar. If it is unavailable in the web
    app, fall back to the latest completed weekday.
    """
    now_ny = datetime.now(NY_TIMEZONE)
    today = pd.Timestamp(now_ny.date())
    end_day = today if (now_ny.hour > 16 or (now_ny.hour == 16 and now_ny.minute >= 30)) else today - pd.Timedelta(days=1)

    if mcal is not None:
        start_day = end_day - pd.Timedelta(days=10)
        nyse = mcal.get_calendar("NYSE")
        sched = nyse.schedule(start_date=start_day.date(), end_date=end_day.date())
        if not sched.empty:
            return pd.Timestamp(sched.index[-1]).normalize()
        print("WARNING: NYSE 캘린더 결과가 비어 있어 평일 기준 fallback을 사용합니다.")
    else:
        print("WARNING: pandas_market_calendars 미설치 - 평일 기준 fallback을 사용합니다.")

    # Fallback for Streamlit runtime only. Actions should normally take the exact-calendar path above.
    candidate = end_day.normalize()
    while candidate.weekday() >= 5:  # Saturday/Sunday
        candidate -= pd.Timedelta(days=1)
    return candidate


def update_soxl_data(file_path=DATA_FILE):
    """Yahoo-only adjusted OHLC updater; fail closed on malformed or stale data.

    Two Yahoo interfaces are a consistency check, NOT independent confirmation.
    Existing historical bars are preserved outside the recent refresh window.
    No broker orders or encrypted portfolio writes.
    """
    existing = load_existing_data(file_path)
    if existing.empty:
        raise RuntimeError("Historical CSV empty; refusing to overwrite.")
    old_last = existing["Date"].max().normalize()
    expected_last = get_expected_latest_trading_day()
    start_date = (old_last - pd.Timedelta(days=LOOKBACK_DAYS)).date()
    end_date = get_download_end_date()
    print(f"CSV latest={old_last.date()}, expected={expected_last.date()}, start={start_date}", flush=True)

    # The historical file was built with adjusted Yahoo bars. Do not mix in
    # Nasdaq raw OHLC or Yahoo auto_adjust=False (dividend/split discontinuity).
    download = download_yfinance_download(start_date, end_date)
    history = download_yfinance_history()
    if not history.empty:
        history = history[
            (history["Date"].dt.date >= start_date) &
            (history["Date"].dt.date < end_date)
        ].copy()
    print(f"YAHOO_DIAGNOSTIC download_rows={len(download)} history_rows={len(history)}", flush=True)

    if download.empty or history.empty:
        raise RuntimeError("Yahoo adjusted download/history unavailable; CSV unchanged.")
    d = download.set_index("Date")
    h = history.set_index("Date")
    if expected_last not in d.index or expected_last not in h.index:
        raise RuntimeError(
            f"Expected confirmed session {expected_last.date()} missing from Yahoo; CSV unchanged."
        )
    common = d.index.intersection(h.index)
    if len(common) < 3:
        raise RuntimeError("Too few overlapping Yahoo sessions; CSV unchanged.")

    for dt in common:
        for col in ("Open", "High", "Low", "Close"):
            dv, hv = float(d.loc[dt, col]), float(h.loc[dt, col])
            if not (pd.notna(dv) and pd.notna(hv) and dv > 0 and hv > 0):
                raise RuntimeError(f"Invalid Yahoo {col} {dt.date()}; CSV unchanged.")
            if abs(dv / hv - 1) > 0.001:
                raise RuntimeError(
                    f"Yahoo interface mismatch {dt.date()} {col}: {dv} vs {hv}; CSV unchanged."
                )

    confirmed = normalize_dataframe(download[
        (download["Date"] <= expected_last) &
        (download["Date"] >= pd.Timestamp(start_date))
    ])
    if confirmed.empty or expected_last not in set(confirmed["Date"]):
        raise RuntimeError("Missing latest Yahoo adjusted session; CSV unchanged.")
    if confirmed["Date"].max() > expected_last:
        raise RuntimeError("Unconfirmed future bar detected; CSV unchanged.")
    for _, row in confirmed.iterrows():
        o, hi, lo, c = (float(row[k]) for k in ("Open", "High", "Low", "Close"))
        if not (lo > 0 and lo <= min(o, c) and hi >= max(o, c)):
            raise RuntimeError(f"Invalid OHLC bounds {row['Date']}; CSV unchanged.")
        if pd.isna(row["Volume"]) or float(row["Volume"]) < 0:
            raise RuntimeError(f"Invalid volume {row['Date']}; CSV unchanged.")

    # Recent source contamination can require correction. Permit bounded revisions
    # ONLY when Yahoo download and history agree, with a visible audit trail.
    # Changes outside the rolling window remain untouched.
    old = existing.set_index("Date")
    fresh = confirmed.set_index("Date")
    overlap = old.index.intersection(fresh.index)
    revisions = []
    for dt in overlap:
        prev, new = float(old.loc[dt, "Close"]), float(fresh.loc[dt, "Close"])
        if prev <= 0:
            raise RuntimeError(f"Invalid stored close {dt.date()}; CSV unchanged.")
        relative_change = abs(new / prev - 1)
        if relative_change > 0.15:
            raise RuntimeError(
                f"Extreme historical discrepancy {dt.date()}: stored={prev}, "
                f"Yahoo={new}, change={relative_change:.2%}; CSV unchanged."
            )
        if relative_change > 0.01:
            revisions.append((dt.date(), prev, new, relative_change))
    for day, prev, new, delta in revisions:
        print(
            f"YAHOO_REVISION_AUDIT date={day} stored={prev:.6f} "
            f"yahoo_adjusted={new:.6f} change={delta:.2%} "
            "download_history_consistent=True",
            flush=True,
        )
    if revisions:
        print(f"YAHOO_REVISION_AUDIT total={len(revisions)} recent-window corrections; "
              "Yahoo same-provider consistency, not independent verification.", flush=True)

    historical = existing[~existing["Date"].isin(confirmed["Date"])]
    merged = normalize_dataframe(pd.concat([historical, confirmed], ignore_index=True))
    if merged["Date"].max().normalize() != expected_last:
        raise RuntimeError("Merged file missing expected last session; CSV unchanged.")
    changed = not merged.equals(existing)
    if changed:
        import os
        import tempfile
        path = Path(file_path)
        with tempfile.NamedTemporaryFile("w", dir=path.parent, suffix=".csv", delete=False) as tmp:
            temp_name = tmp.name
            merged.to_csv(tmp, index=False)
        os.replace(temp_name, path)
    return {
        "updated": changed,
        "rows_added": int((confirmed["Date"] > old_last).sum()),
        "rows_refreshed": int((confirmed["Date"] <= old_last).sum()),
        "audited_close_revisions": len(revisions),
        "old_last_date": old_last.date(),
        "new_last_date": merged["Date"].max().date(),
        "expected_last_date": expected_last.date(),
        "source": "YAHOO_ADJUSTED_DOWNLOAD_HISTORY_CONSISTENCY",
        "verification": "SAME_PROVIDER_NOT_INDEPENDENT",
        "message": "Yahoo adjusted data saved" if changed else "Yahoo adjusted data unchanged",
    }


if __name__ == "__main__":
    result = update_soxl_data()
    print("\n=== UPDATE RESULT ===")
    for k, v in result.items():
        print(f"{k:22s}: {v}")
