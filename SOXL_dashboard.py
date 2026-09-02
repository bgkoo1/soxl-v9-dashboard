import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import json
import base64
import requests
from cryptography.fernet import Fernet, InvalidToken
from pathlib import Path
from datetime import datetime, date, time, timedelta
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
        padding-top: 1.8rem;
        padding-bottom: 3rem;
    }

    [data-testid="stMetricValue"] {
        font-size: 1.65rem;
    }

    [data-testid="stMetricLabel"] {
        font-size: 0.9rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ============================================================
# TITLE
# ============================================================

st.title("📈 SOXL Quant Strategy Lab(퀀트 전략 대시보드)")

st.caption(
    "최종 V9 = 장기과매도 약세 필터 + 단기하락추세 방어 LOC · "
    "장기과매도 약세구간에서는 신규매수를 쉬고, 단기하락추세 구간에서는 기존 티어 LOC를 매수가 + $0.10로 전환합니다."
)


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


def load_persisted_v9_quantities():
    """GitHub의 암호화 파일에서 실제 보유수량을 복원합니다."""
    config = _portfolio_persistence_config()
    if config is None:
        return {}, "Secrets 미설정"

    try:
        response = requests.get(
            _github_contents_url(config),
            headers=_github_headers(config),
            params={"ref": config["branch"]},
            timeout=10,
        )
        if response.status_code == 404:
            return {}, "저장 데이터 없음"
        response.raise_for_status()
        item = response.json()
        encrypted = base64.b64decode(item["content"])
        decrypted = Fernet(config["key"].encode("utf-8")).decrypt(encrypted)
        payload = json.loads(decrypted.decode("utf-8"))
        quantities = payload.get("quantities", {}) if isinstance(payload, dict) else {}
        if not isinstance(quantities, dict):
            quantities = {}
        cleaned = {}
        for k, v in quantities.items():
            try:
                cleaned[str(k)] = max(1, int(round(float(v))))
            except (TypeError, ValueError):
                continue
        return cleaned, "GitHub 암호화 저장값 불러옴"
    except InvalidToken:
        return {}, "암호화 키가 저장 데이터와 일치하지 않음"
    except Exception as e:
        return {}, f"영구 저장 불러오기 실패: {e}"


def save_persisted_v9_quantities(quantity_map):
    """실제 보유수량을 암호화해 GitHub에 저장합니다."""
    config = _portfolio_persistence_config()
    if config is None:
        return False, "Streamlit Secrets에 GitHub/암호화 설정이 없습니다."

    cleaned = {}
    for k, v in (quantity_map or {}).items():
        try:
            cleaned[str(k)] = max(1, int(round(float(v))))
        except (TypeError, ValueError):
            continue

    payload = {
        "version": 1,
        "updated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(),
        "quantities": cleaned,
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
            "message": "Update encrypted live portfolio quantities",
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
        response.raise_for_status()
        return True, "실제 보유수량을 암호화하여 영구 저장했습니다."
    except Exception as e:
        return False, f"영구 저장 실패: {e}"


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
            "슬롯": slot_no,
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
    """현재 슬롯을 수량 오버라이드와 연결하기 위한 안정적인 키."""
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
            saved_qty = max(1, int(round(float(saved_qty))))
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

    for pos in open_positions or []:
        key = v9_position_key(pos)
        entry_date = pd.Timestamp(pos["entry_date"]).normalize()
        entry_price = float(pos["entry_price"])
        model_invested = float(pos["invested"])
        entry_fx = lookup_entry_fx(entry_date, fx_daily, fallback_fx)

        if entry_fx is None or entry_fx <= 0 or entry_price <= 0:
            actual_qty = None
            actual_invested = model_invested
        else:
            strategy_qty = int(round(model_invested / (entry_price * entry_fx)))
            actual_qty = int(qty_overrides.get(key, max(1, strategy_qty)))
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
):
    """V9 미청산 포지션을 현재 적용 LOC와 실제 입력수량 기준으로 표시합니다."""
    if not open_positions:
        return pd.DataFrame()

    rows = []
    for slot_no, pos in enumerate(open_positions, start=1):
        entry_date = pd.Timestamp(pos["entry_date"]).normalize()
        entry_price = float(pos["entry_price"])
        invested = float(pos["invested"])
        deadline, estimated = get_moc_deadline(entry_date, holding_days=holding_days)

        if tb3_state:
            target_price = entry_price + TB3_LOC_PLUS_DOLLAR
            loc_mode = "🛡️ 방어 LOC · 매수가 + $0.10"
        else:
            target_price = entry_price * (1 + BASE_TARGET_RETURN)
            loc_mode = "🎯 일반 LOC · +2.7%"

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
            int(round(invested / (entry_price * entry_fx)))
            if entry_fx is not None and entry_fx > 0 and entry_price > 0
            else None
        )
        key = v9_position_key(pos)
        if strategy_qty is not None:
            selected_qty = int((qty_overrides or {}).get(key, max(1, strategy_qty)))
            actual_invested = selected_qty * entry_price * entry_fx
        else:
            selected_qty = None
            actual_invested = invested

        rows.append({
            "슬롯": slot_no,
            "매수일": entry_date.date(),
            "매수금액": f"{actual_invested:,.0f}원",
            "매수가": f"${entry_price:,.2f}",
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
    qty_overrides=None, fx_daily=None, fallback_fx=None,
):
    """실시간 평가손익 계산에 사용할 미청산 포지션 원자료."""
    if strategy_mode.startswith("V9"):
        positions = live_result.get("open_positions", [])
        if not positions:
            return pd.DataFrame(columns=["Entry_Price", "Invested"])
        rows = []
        for p in positions:
            entry_date = pd.Timestamp(p["entry_date"]).normalize()
            entry_price = float(p["entry_price"])
            model_invested = float(p["invested"])
            entry_fx = lookup_entry_fx(entry_date, fx_daily, fallback_fx)
            if entry_fx is not None and entry_fx > 0 and entry_price > 0:
                strategy_qty = int(round(model_invested / (entry_price * entry_fx)))
                key = v9_position_key(p)
                actual_qty = int((qty_overrides or {}).get(key, max(1, strategy_qty)))
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
            "LOC(목표수익 매도) 비율",
        ),

        (
            "Time_Rate",
            "TIME(기한 만료 매도) 비율",
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
        "Profit Factor(이익/손실 비율)"
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
# SIDEBAR
# ============================================================

st.sidebar.header(
    "📊 SOXL Strategy Lab(전략 실험실)"
)


st.sidebar.subheader(
    "📅 데이터 상태"
)


latest_data_date = (
    full_df[
        "Date"
    ]
    .max()
    .date()
)


st.sidebar.metric(
    "현재 최신 일봉",
    str(
        latest_data_date
    )
)


if st.sidebar.button(
    "🔄 최신 데이터 업데이트",
    use_container_width=True,
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

saved_settings = load_dashboard_settings()

saved_initial_capital = saved_settings.get("initial_capital", 100_000_000)
try:
    saved_initial_capital = int(saved_initial_capital)
except (TypeError, ValueError):
    saved_initial_capital = 100_000_000

saved_initial_capital = max(
    1_000_000,
    min(10_000_000_000, saved_initial_capital),
)

st.sidebar.subheader(
    "⚙️ Strategy(전략) 설정"
)

strategy_mode = st.sidebar.radio(
    "Strategy(전략) 선택",
    [
        "V9 Final(약세필터 + 하락추세 방어LOC)",
        "V8 장기과매도 약세필터",
        "V7 Base(기본 7분할)",
        "Custom(직접 설정)",
    ],
    index=0,
)

is_custom = strategy_mode.startswith("Custom")

initial_capital = (
    st.sidebar.number_input(

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
    st.sidebar.slider(
        "Split(분할 수)",
        2,
        20,
        7,
        disabled=not is_custom,
    )
)


max_positions = (
    st.sidebar.slider(
        "Max Positions(최대 동시 보유 수)",
        1,
        20,
        7,
        disabled=not is_custom,
    )
)


target_percent = (
    st.sidebar.slider(
        "LOC(종가 조건 주문) 목표수익률 (%)",
        0.5,
        15.0,
        2.7,
        0.1,
        disabled=not is_custom,
    )
)


holding_days = (
    st.sidebar.slider(
        "TIME(기한 만료 매도) 보유기간",
        1,
        30,
        7,
        disabled=not is_custom,
    )
)


reserve_percent = (
    st.sidebar.slider(
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
    st.sidebar.date_input(
        "시작일",
        saved_start_date,
        min_value=
            min_date,
        max_value=
            max_date,
        key=
            "start_date_input",
    )
)


# 시작일 또는 초기 투자금이 바뀔 때마다 마지막 설정값을 자동 저장합니다.
current_saved_start = saved_settings.get("start_date")
current_saved_capital = saved_settings.get("initial_capital")

if (
    current_saved_start != pd.Timestamp(start_date).date().isoformat()
    or str(current_saved_capital) != str(int(initial_capital))
):
    save_dashboard_settings(
        start_date_value=start_date,
        initial_capital_value=initial_capital,
    )


end_date = (
    st.sidebar.date_input(
        "종료일",
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

if strategy_mode.startswith("V9"):
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
    signal_ready = (
        pd.notna(latest_ma_gap)
        and pd.notna(latest_momentum)
        and pd.notna(latest_ma5)
        and pd.notna(latest_ma20)
        and pd.notna(latest_ma50)
    )
    if signal_ready:
        latest_risk_state = is_risk_b(latest_ma_gap, latest_momentum)
        latest_tb3_state = bool(latest_ma5 < latest_ma20 < latest_ma50)

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

if strategy_mode.startswith("V9"):
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
portfolio_fx_snapshot = get_usdkrw_realtime_snapshot() if strategy_mode.startswith("V9") else {"rate": None}
portfolio_fallback_fx = portfolio_fx_snapshot.get("rate")
v9_fx_daily = pd.DataFrame(columns=["Date", "USD_KRW"])
v9_quantity_overrides = {}

if strategy_mode.startswith("V9"):
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
        fx_daily=v9_fx_daily,
        fallback_fx=portfolio_fallback_fx,
    )
    live_cash = float(actual_portfolio["cash"])
    live_nav = float(actual_portfolio["nav"])
    open_positions_display = build_v9_open_positions_table(
        v9_open_positions,
        current_close=live_close,
        as_of_date=position_reference_date,
        tb3_state=latest_tb3_state,
        holding_days=BASE_HOLDING_DAYS,
        fx_daily=v9_fx_daily,
        fallback_fx=portfolio_fallback_fx,
        qty_overrides=v9_quantity_overrides,
    )
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

if not signal_ready:
    next_action = "CHECK DATA(데이터 확인 필요)"
    next_buy_amount = 0.0
    market_state_text = "DATA CHECK(데이터 확인 필요)"
elif (strategy_mode.startswith("V8") or strategy_mode.startswith("V9")) and latest_risk_state:
    next_action = "SKIP(신규매수 쉬기)"
    next_buy_amount = 0.0
    market_state_text = (
        "장기과매도 약세 + 단기하락추세(신규매수 중단 · 방어 LOC)"
        if strategy_mode.startswith("V9") and latest_tb3_state
        else "장기과매도 약세구간(신규매수 중단)"
    )
elif live_open_count >= max_slots_for_live:
    next_action = "WAIT(보유 슬롯 가득 참)"
    next_buy_amount = 0.0
    market_state_text = (
        "단기하락추세(방어 LOC 적용)" if strategy_mode.startswith("V9") and latest_tb3_state
        else "NORMAL(정상 매수 구간)"
    )
elif usable_cash <= 0:
    next_action = "WAIT(사용 가능한 현금 없음)"
    next_buy_amount = 0.0
    market_state_text = (
        "단기하락추세(방어 LOC 적용)" if strategy_mode.startswith("V9") and latest_tb3_state
        else "NORMAL(정상 매수 구간)"
    )
else:
    target_buy_amount = live_nav / divisor_for_next
    next_buy_amount = min(target_buy_amount, usable_cash)
    next_action = "BUY(신규매수)" if next_buy_amount > 0 else "WAIT(신규매수 대기)"
    market_state_text = (
        "단기하락추세(방어 LOC 적용)" if strategy_mode.startswith("V9") and latest_tb3_state
        else "NORMAL(정상 매수 구간)"
    )

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
# HEADER
# ============================================================

c1, c2, c3 = (
    st.columns(
        [
            1,
            1,
            2,
        ]
    )
)


c1.metric(
    "데이터 최신일",
    str(
        df[
            "Date"
        ]
        .max()
        .date()
    )
)


c2.metric(
    "거래일",
    f"{len(df):,}"
)


c3.info(
    "실제 주문 전에는 증권사 시세를 별도로 확인하세요."
)


# ============================================================
# TODAY'S ACTION + LIVE STATUS
# ============================================================

st.divider()
st.subheader("🧭 Trading Dashboard(오늘의 운용 현황)")

# 행동 문구는 기존 전략 계산 결과를 그대로 사용합니다.
if next_action.startswith("BUY"):
    action_icon = "🟢"
    action_word = "BUY"
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

    open_df = build_live_open_holdings_df(
        strategy_mode,
        live_result,
        live_trades,
        qty_overrides=v9_quantity_overrides,
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

    # V9 핵심 신호를 한눈에 확인할 수 있도록 별도 상태 카드로 표시합니다.
    if strategy_mode.startswith("V9"):
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
    s1, s2, s3, s4, s5 = st.columns(5)
    s1.metric("Confirmed NAV(확정 NAV)", f"{live_nav:,.0f}원", help="V9에서는 입력한 실제 보유수량을 반영합니다.")
    s2.metric("Live NAV(실시간 NAV)", f"{realtime_nav:,.0f}원")
    s3.metric("Cash(사용가능 현금)", f"{usable_cash:,.0f}원", help="실제 보유수량과 전략 기준수량의 매수금액 차이를 반영한 현금입니다.")
    s4.metric("Slots(보유 슬롯)", f"{live_open_count} / {max_slots_for_live}")
    s5.metric("MOC Today(오늘 MOC)", f"{moc_today_count}건")

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
    if strategy_mode.startswith("V9"):
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

    if (strategy_mode.startswith("V8") or strategy_mode.startswith("V9")) and signal_ready and latest_risk_state:
        st.warning(
            "장기과매도 약세구간이라 오늘 신규매수는 쉽니다. 기존 포지션의 매도 규칙은 계속 적용됩니다."
        )

    if strategy_mode.startswith("V9") and signal_ready and latest_tb3_state:
        st.info(
            "TB3 ON: MA5 < MA20 < MA50. 기존 보유 티어의 현재 LOC는 모두 매수가 + $0.10입니다. "
            "TB3가 해제되면 다시 +2.7% LOC로 복귀합니다."
        )

    with st.expander("운용·신호 상세 정보"):
        st.write(f"운용 시작일: {start_date}")
        st.write(f"초기 투자금: {initial_capital:,.0f}원")
        st.write(f"미국 주문 대상일: {action_session.date()} · {session_status}")
        st.write(f"신호 기준일: {signal_date_text}")
        st.write(f"시장 상태: {market_state_text}")
        st.write("장기과매도 약세조건: MA200 대비 -15% 미만이며 20거래일 수익률이 -20% 이상 -5% 미만")
        if strategy_mode.startswith("V9"):
            st.write(f"단기하락추세 상태: {'ON' if latest_tb3_state else 'OFF'} · 조건 MA5 < MA20 < MA50")
            if pd.notna(latest_ma5) and pd.notna(latest_ma20) and pd.notna(latest_ma50):
                st.write(f"MA5 / MA20 / MA50: {latest_ma5:.2f} / {latest_ma20:.2f} / {latest_ma50:.2f}")
            st.write(
                f"현재 LOC 규칙: {'매수가 + $0.10' if latest_tb3_state else '매수가 × 1.027'}"
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
        "모든 V9 신호는 직전 확정 일봉 기준이며 실시간 영역은 5분마다 자동 새로고침됩니다."
    )

    if strategy_mode.startswith("V9"):
        v9_open_positions = live_result.get("open_positions", [])

    st.subheader("📦 Open Positions(현재 보유 슬롯)")

    if strategy_mode.startswith("V9"):
        realtime_positions = build_v9_open_positions_table(
            v9_open_positions,
            current_close=realtime_price,
            as_of_date=position_reference_date,
            tb3_state=latest_tb3_state,
            holding_days=BASE_HOLDING_DAYS,
            fx_daily=v9_fx_daily,
            fallback_fx=portfolio_fallback_fx,
            qty_overrides=v9_quantity_overrides,
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

    if strategy_mode.startswith("V9"):
        preferred_cols = [
            "슬롯", "매수일", "매수금액", "매수가", "적용환율", "보유수량",
            "LOC 모드", "LOC 주문가", "현재가", "현재 수익률",
            "평가손익", "MOC 예정일", "남은 거래일", "상태",
        ]
        position_table = position_table[[c for c in preferred_cols if c in position_table.columns]]

        # 보유수량은 Open Positions 표에서 직접 수정합니다.
        editor_table = position_table.copy()
        editor_table["보유수량"] = (
            editor_table["보유수량"]
            .astype(str)
            .str.replace("주", "", regex=False)
            .str.replace(",", "", regex=False)
            .pipe(pd.to_numeric, errors="coerce")
            .fillna(1)
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
            disabled=[c for c in editor_table.columns if c != "보유수량"],
            column_config={
                "LOC 모드": st.column_config.TextColumn("🔎 현재 LOC 모드", width="large"),
                "LOC 주문가": st.column_config.TextColumn("🎯 LOC 주문가", width="medium"),
                "보유수량": st.column_config.NumberColumn(
                    "✏️ 실제 보유수량", min_value=1, step=1, format="%d주", width="medium"
                ),
                "적용환율": st.column_config.TextColumn("적용환율", width="medium"),
                "매수금액": st.column_config.TextColumn("매수금액", width="medium"),
            },
            key="v9_open_positions_editor",
        )

        save_c1, save_c2 = st.columns([1, 1])
        if save_c1.button(
            "💾 보유수량 변경 저장",
            use_container_width=True,
            key="save_v9_open_qty_inline",
        ):
            merged_map = dict(st.session_state.get("v9_quantity_overrides", {}))
            key_by_slot = {
                i: v9_position_key(pos)
                for i, pos in enumerate(v9_open_positions or [], start=1)
            }
            for _, row in edited_positions.iterrows():
                slot_no = int(row["슬롯"])
                key = key_by_slot.get(slot_no)
                if key:
                    merged_map[key] = max(1, int(round(float(row["보유수량"]))))
            st.session_state["v9_quantity_overrides"] = merged_map
            ok, msg = save_persisted_v9_quantities(merged_map)
            st.session_state["v9_quantity_persistence_status"] = msg
            st.session_state["v9_quantity_persistence_ok"] = ok
            st.rerun()

        if save_c2.button(
            "↩️ 현재 슬롯 전략 기준수량으로 복원",
            use_container_width=True,
            key="reset_v9_open_qty_inline",
        ):
            merged_map = dict(st.session_state.get("v9_quantity_overrides", {}))
            for pos in v9_open_positions or []:
                key = v9_position_key(pos)
                entry_date = pd.Timestamp(pos["entry_date"]).normalize()
                entry_price = float(pos["entry_price"])
                invested = float(pos["invested"])
                entry_fx = lookup_entry_fx(entry_date, v9_fx_daily, portfolio_fallback_fx)
                if entry_fx is not None and entry_fx > 0 and entry_price > 0:
                    merged_map[key] = max(1, int(round(invested / (entry_price * entry_fx))))
            st.session_state["v9_quantity_overrides"] = merged_map
            ok, msg = save_persisted_v9_quantities(merged_map)
            st.session_state["v9_quantity_persistence_status"] = msg
            st.session_state["v9_quantity_persistence_ok"] = ok
            st.rerun()

        persistence_msg = st.session_state.get("v9_quantity_persistence_status", "")
        if portfolio_persistence_enabled():
            if st.session_state.get("v9_quantity_persistence_ok") is False:
                st.error(f"☁️ 영구저장 오류 · {persistence_msg}")
            else:
                st.caption(f"🔐 실제 보유수량 영구저장 연결됨 · {persistence_msg or 'GitHub 암호화 저장소 사용'}")
        else:
            st.warning(
                "현재는 세션 저장만 사용 중입니다. Streamlit Secrets의 GitHub/암호화 설정을 확인하세요."
            )
    else:
        st.dataframe(
            position_table,
            use_container_width=True,
            hide_index=True,
        )
    if strategy_mode.startswith("V9"):
        st.caption(
            f"현재가·수익률·평가손익은 {'장중 참고시세' if snapshot['is_live'] else '마지막 확정 종가'} 기준입니다. · "
            f"보유수량은 매수금액 ÷ (매수가 × 매수일 USD/KRW 참고환율)로 계산한 예상수량을 정수로 반올림해 표시합니다. · "
            f"목표 매도가는 직전 확정 일봉의 단기하락추세 상태를 반영합니다. · "
            f"MOC 예정일은 매수일 Day 0 이후 {BASE_HOLDING_DAYS}번째 미국 거래일입니다."
        )
    else:
        st.caption(
            f"현재가·수익률·평가손익은 {'장중 참고시세' if snapshot['is_live'] else '마지막 확정 종가'} 기준입니다. · "
            f"MOC 예정일은 매수일을 Day 0으로 두고 "
            f"{BASE_HOLDING_DAYS if not is_custom else holding_days}번째 미국 거래일입니다."
        )


render_compact_trading_dashboard()

# ============================================================
# KPI
# ============================================================

st.divider()

st.subheader(
    f"핵심 성과 · {strategy_short_name}"
)


k1, k2, k3, k4 = (
    st.columns(
        4
    )
)


k1.metric(
    "Final Equity(최종자산)",
    f"{stats['final_equity']:,.0f}원"
)


k2.metric(
    "CAGR(연평균 수익률)",
    f"{stats['cagr']:.2%}",
    delta=
        f"{(stats['cagr'] - base_stats['cagr']) * 100:+.2f}%p",
)


k3.metric(
    "MDD(최대 하락폭)",
    f"{stats['mdd']:.2%}",
    delta=
        f"{(stats['mdd'] - base_stats['mdd']) * 100:+.2f}%p",
)


k4.metric(
    "Calmar(수익 대비 하락위험)",
    f"{stats['calmar']:.3f}",
    delta=
        f"{stats['calmar'] - base_stats['calmar']:+.3f}",
)


# ============================================================
# TABS
# ============================================================

st.divider()


tab1, tab2, tab3, tab4, tab5, tab6 = (
    st.tabs(
        [

            "📈 Equity Curve(자산곡선)",

            "📉 Risk(위험)",

            "⏱ TIME(기한 만료 매도)",

            "🌦 Market State(시장상태)",

            "📅 Yearly(연도별)",

            "📋 Trades(거래내역)",

        ]
    )
)


# ============================================================
# TAB 1 EQUITY
# ============================================================

with tab1:

    fig = (
        go.Figure()
    )


    fig.add_trace(
        go.Scatter(

            x=
                base_result[
                    "equity"
                ][
                    "Date"
                ],

            y=
                base_result[
                    "equity"
                ][
                    "Equity"
                ],

            name=
                "V7 Base(기본 전략)",

        )
    )


    fig.add_trace(
        go.Scatter(

            x=
                current_result[
                    "equity"
                ][
                    "Date"
                ],

            y=
                current_result[
                    "equity"
                ][
                    "Equity"
                ],

            name=
                "Selected Strategy(선택 전략)",

        )
    )


    fig.update_layout(
        height=550,
        hovermode=
            "x unified",
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
        "신규 매수 당시의 MA200 괴리율과 "
        "최근 20거래일 모멘텀을 기준으로 "
        "실제 거래 성과를 비교합니다."
    )


    # --------------------------------------------------------
    # PRICE CHART
    # --------------------------------------------------------

    prices_display = (
        market_analysis[
            "prices"
        ]
    )


    prices_display = (
        prices_display[
            (
                prices_display[
                    "Date"
                ].dt.date
                >= start_date
            )
            &
            (
                prices_display[
                    "Date"
                ].dt.date
                <= end_date
            )
        ]
    )


    fig_price = (
        go.Figure()
    )


    fig_price.add_trace(
        go.Scatter(

            x=
                prices_display[
                    "Date"
                ],

            y=
                prices_display[
                    "Close"
                ],

            name=
                "SOXL",

        )
    )


    fig_price.add_trace(
        go.Scatter(

            x=
                prices_display[
                    "Date"
                ],

            y=
                prices_display[
                    "MA200"
                ],

            name=
                "MA200",

        )
    )


    fig_price.update_layout(
        height=500,
        hovermode=
            "x unified",
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


    st.dataframe(

        format_regime_table(
            ma_summary
        ),

        use_container_width=True,

        hide_index=True,

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


    st.dataframe(

        format_regime_table(
            momentum_summary
        ),

        use_container_width=True,

        hide_index=True,

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
                "LOC(목표수익 매도) 비율",
                "TIME(기한 만료 매도) 비율",
                "Profit Factor(이익/손실 비율)",
                "거래수",
            ],

        )
    )


    metric_map = {

        "평균 수익률":
            "Avg_Return",

        "승률":
            "Win_Rate",

        "LOC(목표수익 매도) 비율":
            "Target_Rate",

        "TIME(기한 만료 매도) 비율":
            "Time_Rate",

        "Profit Factor(이익/손실 비율)":
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

        "LOC(목표수익 매도) 비율",

        "TIME(기한 만료 매도) 비율",

        "Profit Factor(이익/손실 비율)",

    ]


    st.dataframe(

        combo_display,

        use_container_width=True,

        hide_index=True,

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

                "MDD(최대 하락폭)":
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


    st.dataframe(

        yearly_display,

        use_container_width=True,

        hide_index=True,

    )


# ============================================================
# TAB 6 TRADES
# ============================================================

with tab6:

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

    # V9은 실제 저장 수량을 Trades까지 이어서 표시합니다.
    # 저장값이 없는 과거 티어는 매수일 환율을 이용한 전략 기준 정수수량을 표시합니다.
    if strategy_mode.startswith("V9") and not display_trades.empty:
        trade_start = pd.to_datetime(display_trades["Entry_Date"]).min()
        trade_end = pd.to_datetime(display_trades["Entry_Date"]).max()
        trades_fx_daily = get_usdkrw_daily_rates(trade_start, trade_end)
        qty_map_for_trades = st.session_state.get("v9_quantity_overrides", {})

        def _trade_qty(row):
            pseudo_pos = {
                "entry_date": pd.Timestamp(row["Entry_Date"]),
                "entry_price": float(row["Entry_Price"]),
            }
            key = v9_position_key(pseudo_pos)
            if key in qty_map_for_trades:
                try:
                    return max(1, int(round(float(qty_map_for_trades[key]))))
                except (TypeError, ValueError):
                    pass
            entry_date = pd.Timestamp(row["Entry_Date"]).normalize()
            entry_price = float(row["Entry_Price"])
            invested = float(row["Invested"])
            entry_fx = lookup_entry_fx(entry_date, trades_fx_daily, portfolio_fallback_fx)
            if entry_fx is not None and entry_fx > 0 and entry_price > 0:
                return max(1, int(round(invested / (entry_price * entry_fx))))
            return None

        display_trades["수량"] = display_trades.apply(_trade_qty, axis=1)
        display_trades["수량"] = display_trades["수량"].map(
            lambda x: f"{int(x):,}주" if pd.notna(x) else "계산 불가"
        )
    else:
        # V7/V8 등 기존 전략은 엔진의 합성 shares를 실주식 수량으로 오해하지 않도록 표시하지 않습니다.
        display_trades["수량"] = "-"
    if "Active_Target_Price" in display_trades.columns:
        target_series = display_trades["Active_Target_Price"]
    elif "Target_Price" in display_trades.columns:
        target_series = display_trades["Target_Price"]
    else:
        target_series = display_trades["Entry_Price"] * (1 + BASE_TARGET_RETURN)
    display_trades["목표 매도가"] = target_series.map(lambda x: f"${x:,.2f}")
    display_trades["투자금"] = display_trades["Invested"].map(lambda x: f"{x:,.0f}원")
    display_trades["수익률"] = display_trades["Return"].map(lambda x: f"{x:.2%}")
    display_trades["손익"] = display_trades["Profit"].map(lambda x: f"{x:,.0f}원")
    display_trades["보유 거래일"] = display_trades["Holding_Days"].map(lambda x: f"{int(x)}일")
    display_trades["매도 이유"] = display_trades["Exit_Type"].map(exit_label_map).fillna(display_trades["Exit_Type"])

    display_trades = display_trades[[
        "매수일",
        "매도일",
        "매수가",
        "매도가",
        "목표 매도가",
        "수량",
        "투자금",
        "수익률",
        "손익",
        "보유 거래일",
        "매도 이유",
    ]]

    st.dataframe(
        display_trades,
        use_container_width=True,
        hide_index=True,
    )


    csv = (
        trades
        .to_csv(
            index=False
        )
        .encode(
            "utf-8-sig"
        )
    )


    st.download_button(

        "📥 거래내역 CSV 다운로드",

        data=
            csv,

        file_name=
            "SOXL_current_strategy_trades.csv",

        mime=
            "text/csv",

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
