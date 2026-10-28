"""Reconstruct SOXL order lots, then reconcile against broker holdings.

FIFO is an explicit display allocation, not a tax-lot claim. All fills sharing
one buy order form a single slot; partial fills never become extra slots.
"""
from copy import deepcopy
from decimal import Decimal
import pandas as pd
from v12.meritz import amount, ReadError


def rebuild(snapshot, source_timezone=None):
    issues, lots, closed = [], [], []
    soxl = [h for h in snapshot["holdings"] if h["symbol"] == "SOXL"]
    if any(h["currency"] != "USD" for h in soxl):
        issues.append("SOXL 잔고의 통화가 USD와 다릅니다.")
    broker_qty = sum((amount(h["quantity"]) for h in soxl), Decimal(0))
    if source_timezone not in {"Asia/Seoul", "America/New_York", "UTC"}:
        return {"positions": [], "closed_trades": [], "cashflows": [], "schema_version": 1,
                "issues": ["체결일시 기준 시간대를 선택해야 V12 보유 거래일을 계산할 수 있습니다."],
                "broker_quantity": str(broker_qty), "matched": False, "today_sales": "0"}
    fills, today_sales = [], Decimal(0)
    today_ny = pd.Timestamp(snapshot["fetched_at"]).tz_convert("America/New_York").date().isoformat()
    for f in snapshot["fills"]:
        stamp = pd.Timestamp(f["timestamp"]).tz_localize(source_timezone, ambiguous="raise", nonexistent="raise").tz_convert("America/New_York")
        if f["side"] == "SELL" and f["currency"] == "USD" and stamp.date().isoformat() == today_ny:
            today_sales += amount(f["quantity"]) * amount(f["price"])
        if f["symbol"] != "SOXL":
            continue
        if f["currency"] != "USD":
            issues.append("SOXL 체결 통화를 확인할 수 없습니다.")
            continue
        row = deepcopy(f)
        row["entry_day"] = stamp.date().isoformat()
        row["ny_stamp"] = stamp
        fills.append(row)
    fills.sort(key=lambda r: (r["ny_stamp"], int(r["order_no"]), int(r["execution_no"])))
    by_order = {}
    for f in fills:
        qty, price = amount(f["quantity"]), amount(f["price"])
        if f["side"] == "BUY":
            key = f["order_key"]
            if key not in by_order:
                p = {"slot_id": "meritz-" + key.replace(":", "-"), "entry_date": f["entry_day"],
                     "entry_price": str(price), "quantity": "0", "source": "manual",
                     "note": "메리츠 체결 · 주문별 통합 · FIFO 배분", "model_exit": "증권사 체결"}
                lots.append(p)
                by_order[key] = p
            p = by_order[key]
            if p["entry_date"] != f["entry_day"]:
                issues.append("한 주문의 체결이 여러 미국 거래일에 걸쳐 있어 슬롯을 확인해야 합니다.")
            old_qty = amount(p["quantity"])
            p["entry_price"] = str((old_qty * amount(p["entry_price"]) + qty * price) / (old_qty + qty))
            p["quantity"] = str(old_qty + qty)
        else:
            remaining = qty
            for p in lots:
                available = amount(p["quantity"])
                sold = min(available, remaining)
                if sold <= 0:
                    continue
                closed.append({"id": f["id"] + ":" + p["slot_id"], "slot_id": p["slot_id"],
                    "entry_date": p["entry_date"], "exit_date": f["entry_day"],
                    "entry_price": p["entry_price"], "exit_price": str(price), "quantity": str(sold),
                    "exit_strategy": "기타", "note": "메리츠 실제 매도 · FIFO 배분 · 비용 전"})
                p["quantity"] = str(available - sold)
                remaining -= sold
                if remaining <= 0:
                    break
            if remaining > 0:
                issues.append("매수 이력이 부족한 매도가 있습니다. 이전 체결이나 입출고·기업행동 확인이 필요합니다.")
    positions = [p for p in lots if amount(p["quantity"]) > 0]
    qty = sum((amount(p["quantity"]) for p in positions), Decimal(0))
    if abs(qty - broker_qty) > Decimal("0.00000001"):
        issues.append(f"복원 수량 {qty}주와 메리츠 잔고 {broker_qty}주가 다릅니다.")
    if len(positions) > 6:
        issues.append("미청산 매수 주문이 6개를 넘습니다. V12 슬롯 배정을 확인해야 합니다.")
    if snapshot["outstanding"]:
        issues.append("계좌에 미체결 주문이 있습니다. 중복 주문 방지를 위해 주문계획을 보류합니다.")
    if sum((amount(h["sellable"]) for h in soxl), Decimal(0)) < broker_qty:
        issues.append("SOXL 매도가능수량이 보유수량보다 적습니다. 주문계획을 보류합니다.")
    return {"schema_version": 1, "positions": positions, "closed_trades": closed,
            "cashflows": [], "issues": list(dict.fromkeys(issues)), "broker_quantity": str(broker_qty),
            "matched": not issues, "today_sales": str(today_sales)}


def buying_cash(snapshot, book):
    """Conservative cash cap; cash and power are never added together."""
    cash = amount(snapshot["cash_usd"], signed=True)
    power = amount(snapshot["buying_power_usd"], signed=True)
    # Subtract today's gross sale proceeds from deposit conservatively. Broker
    # power's reusable portion is independently excluded; take the smaller cap.
    return max(Decimal(0), min(cash - amount(book["today_sales"]),
                power - amount(snapshot["reusable_usd"])))
