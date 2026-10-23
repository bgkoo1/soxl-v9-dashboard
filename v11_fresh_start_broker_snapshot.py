"""Normalize a broker-verified full-account snapshot for fresh-start gating.

This module performs no network calls and never submits orders.
Raw Meritz field mapping must be verified separately against official documentation.
"""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

ACTIVE={"PENDING","ACCEPTED","PARTIAL","CANCEL_REQUESTED","OPEN","NEW"}
FINAL={"FILLED","CANCELLED","CANCELED","REJECTED","EXPIRED"}

def _nonnegative(value, label):
    if isinstance(value,bool):
        raise ValueError(f"{label}: boolean invalid")
    try:
        n=Decimal(str(value))
    except (InvalidOperation,TypeError,ValueError):
        raise ValueError(f"{label}: invalid amount") from None
    if not n.is_finite() or n<0:
        raise ValueError(f"{label}: negative/nonfinite amount")
    return n

def normalize_full_account_snapshot(snapshot):
    """Only accepts explicitly complete, normalized broker data."""
    if not isinstance(snapshot,dict):
        raise ValueError("snapshot must be a mapping")
    for key in ("holdings_complete","orders_complete","cash_complete"):
        if snapshot.get(key) is not True:
            raise ValueError(f"{key}: completeness not confirmed")
    as_of=snapshot.get("as_of")
    try:
        dt=datetime.fromisoformat(as_of)
    except (ValueError,TypeError):
        raise ValueError("as_of: timezone-aware ISO timestamp required") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("as_of: timezone required")
    if not isinstance(snapshot.get("holdings"),list) or not isinstance(snapshot.get("orders"),list):
        raise ValueError("holdings/orders: complete lists required")
    positions=[]
    for holding in snapshot["holdings"]:
        if not isinstance(holding,dict) or not str(holding.get("symbol","")).strip():
            raise ValueError("holding symbol missing")
        quantity=_nonnegative(holding.get("quantity"),"holding quantity")
        if quantity>0:
            positions.append({"symbol":str(holding["symbol"]).upper(),"quantity":str(quantity)})
    active_orders=[]
    for order in snapshot["orders"]:
        if not isinstance(order,dict) or not str(order.get("symbol","")).strip():
            raise ValueError("order symbol missing")
        status=str(order.get("status","")).upper()
        if status not in ACTIVE|FINAL:
            raise ValueError("unknown order status")
        remaining=_nonnegative(order.get("remaining_quantity"),"remaining_quantity")
        if status in ACTIVE:
            active_orders.append({"symbol":str(order["symbol"]).upper(),"status":status,"remaining_quantity":str(remaining)})
    cash=_nonnegative(snapshot.get("available_cash"),"available_cash")
    if cash<=0:
        raise ValueError("available_cash must be positive")
    return {"authenticated_and_queried":True,"positions_complete":True,
            "orders_complete":True,"positions":positions,"active_orders":active_orders,
            "available_cash":str(cash),"balance_as_of_verified":False,
            "as_of":dt.isoformat(),"live_orders_enabled":False}

def evaluate_snapshot_readiness(snapshot, *, strategy_verified=False,
                                dry_run_passed=False, market_data_verified=False,
                                explicit_live_approval=False):
    from v11_fresh_start_gate import evaluate_fresh_start
    normalized=normalize_full_account_snapshot(snapshot)
    normalized.update(strategy_and_order_rules_verified=strategy_verified,
        dry_run_passed=dry_run_passed,market_data_verified=market_data_verified,
        explicit_live_approval=explicit_live_approval)
    result=evaluate_fresh_start(normalized)
    result["as_of"]=normalized["as_of"]
    result["available_cash"]=normalized["available_cash"]
    result["positions_count"]=len(normalized["positions"])
    result["active_orders_count"]=len(normalized["active_orders"])
    return result
