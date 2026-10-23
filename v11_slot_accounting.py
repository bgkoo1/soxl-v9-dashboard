"""Derive V11 slot positions from verified fill events, without orders or persistence.

FIFO cost basis per slot; partial fills and fees supported. All monetary figures
are in USD and exclude FX/tax. No mark-to-market without a verified quote.
"""
from decimal import Decimal, InvalidOperation
from v11_trade_journal_schema import validate_journal

def _decimal(value, label):
    if isinstance(value, bool):
        raise ValueError(label + ": invalid boolean")
    try:
        n = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(label + ": invalid decimal") from None
    if not n.is_finite() or n < 0:
        raise ValueError(label + ": must be nonnegative finite")
    return n

def summarize_slots(journal):
    validated = validate_journal(journal)
    state = {i: {"lots": [], "realized": Decimal("0"), "buy_fees": Decimal("0"),
                 "sell_fees": Decimal("0"), "buy_quantity": Decimal("0"),
                 "sell_quantity": Decimal("0")} for i in range(1, 8)}
    fills = []
    for event in validated["events"]:
        kind = event["event_type"]
        if kind not in ("BUY_FILLED", "SELL_FILLED"):
            continue
        if event.get("source") != "BROKER_VERIFIED":
            # Do not mix simulations or manually entered records into actual P&L.
            continue
        if kind == "BUY_FILLED" and (event["side"] != "BUY" or event["order_state"] != "FILLED"):
            raise ValueError("inconsistent buy fill")
        if kind == "SELL_FILLED" and (event["side"] != "SELL" or event["order_state"] != "FILLED"):
            raise ValueError("inconsistent sell fill")
        qty = _decimal(event["quantity"], "quantity")
        price = _decimal(event["price"], "price")
        fees = _decimal(event.get("fee_usd", "0"), "fee_usd")
        if qty <= 0 or price <= 0:
            raise ValueError("fill quantity and price must be positive")
        s = state[event["slot_id"]]
        if kind == "BUY_FILLED":
            # Allocate purchase commission to the lot's cost basis.
            s["lots"].append([qty, qty * price + fees])
            s["buy_quantity"] += qty
            s["buy_fees"] += fees
        else:
            held = sum((lot[0] for lot in s["lots"]), Decimal("0"))
            if qty > held:
                raise ValueError("sell exceeds verified slot holdings")
            remaining = qty
            basis = Decimal("0")
            while remaining:
                lot = s["lots"][0]
                taken = min(remaining, lot[0])
                consumed_basis = lot[1] * taken / lot[0]
                basis += consumed_basis
                lot[0] -= taken
                lot[1] -= consumed_basis
                remaining -= taken
                if lot[0] == 0:
                    s["lots"].pop(0)
            pnl = qty * price - fees - basis
            s["realized"] += pnl
            s["sell_quantity"] += qty
            s["sell_fees"] += fees
            fills.append({"event_id": event["event_id"], "slot_id": event["slot_id"],
                          "quantity": str(qty), "realized_pnl_usd": str(pnl)})
    slots = []
    for slot_id, s in state.items():
        qty = sum((lot[0] for lot in s["lots"]), Decimal("0"))
        basis = sum((lot[1] for lot in s["lots"]), Decimal("0"))
        slots.append({"slot_id": slot_id, "quantity": str(qty),
                      "remaining_cost_usd": str(basis),
                      "average_cost_usd": str(basis / qty) if qty else None,
                      "realized_pnl_usd": str(s["realized"]),
                      "buy_quantity": str(s["buy_quantity"]),
                      "sell_quantity": str(s["sell_quantity"]),
                      "buy_fees_usd": str(s["buy_fees"]),
                      "sell_fees_usd": str(s["sell_fees"])})
    return {"strategy": "SOXL_V11", "mode": "READ_ONLY",
            "calculation_method": "FIFO", "currency": "USD",
            "verified_fills_only": True, "slots": slots,
            "total_realized_pnl_usd": str(sum((s["realized"] for s in state.values()), Decimal("0"))),
            "realized_sell_events": fills,
            "note": "실제 확인된 체결만 집계. 평가손익·MDD·세금·환차손익 미포함."}
