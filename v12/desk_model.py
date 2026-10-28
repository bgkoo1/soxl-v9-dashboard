"""Presentation calculations for live account data, never order transmission."""
from decimal import Decimal
import pandas as pd
from v12.meritz import amount


def execution_frames(activity, source_timezone=None, symbol="SOXL"):
    """Return daily USD notionals and a Korean-labelled actual execution tape."""
    rows = []
    for f in (activity or {}).get("fills", []):
        if f["symbol"] != symbol or f["currency"] != "USD":
            continue
        if source_timezone:
            stamp = pd.Timestamp(f["timestamp"]).tz_localize(source_timezone).tz_convert("America/New_York")
            day = stamp.date().isoformat()
        else:
            day = pd.to_datetime(f["order_date"], format="%Y%m%d").date().isoformat()
        qty, price = amount(f["quantity"]), amount(f["price"])
        rows.append({"거래일": day, "체결시각(원문)": f["timestamp"], "구분": "매수" if f["side"] == "BUY" else "매도",
                     "수량(주)": float(qty), "체결가($)": float(price), "체결금액($)": float(qty * price),
                     "주문번호": f["order_no"], "체결번호": f["execution_no"]})
    tape = pd.DataFrame(rows, columns=["거래일", "체결시각(원문)", "구분", "수량(주)", "체결가($)", "체결금액($)", "주문번호", "체결번호"])
    daily = []
    for day in sorted(set(tape["거래일"]), reverse=True):
        subset = tape[tape["거래일"] == day]
        buys, sells = subset[subset["구분"] == "매수"], subset[subset["구분"] == "매도"]
        daily.append({"거래일": day, "매수수량": buys["수량(주)"].sum(), "매수금액($)": buys["체결금액($)"].sum(),
                      "매도수량": sells["수량(주)"].sum(), "매도금액($)": sells["체결금액($)"].sum(),
                      "체결건수": len(subset)})
    return pd.DataFrame(daily), tape


def slot_table(positions, data, price, next_session, orders=None):
    dates = list(pd.to_datetime(data.Date).dt.normalize())
    reasons = {s["슬롯"]: s for s in (orders or {}).get("sells", [])}
    rows = []
    for i, p in enumerate(positions, 1):
        d = pd.Timestamp(p["entry_date"])
        age = len(dates) - dates.index(d) if d in dates else None
        reason = reasons.get(p["slot_id"])
        entry, qty = float(p["entry_price"]), float(p["quantity"])
        target = entry * 1.032
        rows.append({"슬롯": i, "매수일(미국)": p["entry_date"], "다음 주문일 기준": f"D{age}" if age is not None else "종가·이력 확인",
                     "수량": qty, "매수가($)": entry, "현재가($)": price,
                     "LOC 목표가($)": target, "목표까지(%)": (target / price - 1) * 100 if price else None,
                     "평가손익($)": (price - entry) * qty if price else None,
                     "다음 행동": reason["사유"] if reason else "종가·체결 대조 대기",
                     "주문": reason["구분"] if reason else "확인 대기"})
    return pd.DataFrame(rows)
