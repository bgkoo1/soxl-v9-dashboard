"""V11 fresh-start journal schema and offline validation.

No persistence, no broker requests, no orders. V10 ledger is never read or modified.
"""
from datetime import datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

EVENTS = {"SLOT_OPENED", "BUY_PLANNED", "BUY_SUBMITTED", "BUY_FILLED",
          "SELL_PLANNED", "SELL_SUBMITTED", "SELL_FILLED",
          "ORDER_REJECTED", "ORDER_CANCELLED", "SLOT_CLOSED"}
ORDER_TYPES = {"LOC", "MOC"}
SIDES = {"BUY", "SELL"}
ORDER_STATES = {"PLANNED", "SUBMITTED", "PARTIAL", "FILLED", "REJECTED", "CANCELLED"}


def _nonnegative(value, field, *, allow_zero=True):
    if isinstance(value, bool):
        raise ValueError(field + ": boolean not allowed")
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(field + ": invalid decimal") from None
    if not n.is_finite() or n < 0 or (not allow_zero and n == 0):
        raise ValueError(field + ": invalid amount")
    return n


def validate_event(event):
    """Validate a single event without modifying it or executing any trade."""
    if not isinstance(event, dict):
        raise ValueError("event must be an object")
    required = {"event_id", "occurred_at", "slot_id", "event_type", "source"}
    if not required.issubset(event):
        raise ValueError("required event fields missing")
    try:
        UUID(str(event["event_id"]))
        timestamp = datetime.fromisoformat(str(event["occurred_at"]))
    except (ValueError, TypeError):
        raise ValueError("invalid event_id or timestamp") from None
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    if not isinstance(event["slot_id"], int) or isinstance(event["slot_id"], bool) or not 1 <= event["slot_id"] <= 7:
        raise ValueError("slot_id must be integer 1..7")
    if event["event_type"] not in EVENTS:
        raise ValueError("unknown event type")
    if event["source"] not in ("BROKER_VERIFIED", "STRATEGY_DRY_RUN", "MANUAL_VERIFIED"):
        raise ValueError("unknown event source")
    if event["event_type"] in {"BUY_PLANNED", "BUY_SUBMITTED", "BUY_FILLED",
                               "SELL_PLANNED", "SELL_SUBMITTED", "SELL_FILLED",
                               "ORDER_REJECTED", "ORDER_CANCELLED"}:
        if event.get("order_type") not in ORDER_TYPES or event.get("side") not in SIDES:
            raise ValueError("order type or side missing")
        if event.get("order_state") not in ORDER_STATES:
            raise ValueError("order state missing")
        _nonnegative(event.get("quantity"), "quantity", allow_zero=False)
        if event.get("price") is not None:
            _nonnegative(event["price"], "price", allow_zero=False)
        if event["event_type"].endswith("FILLED") and event.get("price") is None:
            raise ValueError("fill price required")
    return dict(event)


def validate_journal(journal):
    """Fail closed on unknown schema, duplicates, or out-of-order event timestamps."""
    if not isinstance(journal, dict) or journal.get("schema_version") != 1:
        raise ValueError("unsupported V11 journal schema")
    if journal.get("strategy") != "SOXL_V11" or journal.get("mode") != "READ_ONLY":
        raise ValueError("unexpected strategy or mode")
    if journal.get("live_orders_enabled") is not False:
        raise ValueError("live orders must remain disabled")
    events = journal.get("events")
    if not isinstance(events, list):
        raise ValueError("events must be a list")
    validated = [validate_event(x) for x in events]
    ids = [x["event_id"] for x in validated]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate event_id")
    timestamps = [datetime.fromisoformat(x["occurred_at"]) for x in validated]
    if timestamps != sorted(timestamps):
        raise ValueError("event timestamps out of order")
    return {"schema_version": 1, "strategy": "SOXL_V11", "mode": "READ_ONLY",
            "live_orders_enabled": False, "events": validated}


def empty_journal():
    return {"schema_version": 1, "strategy": "SOXL_V11", "mode": "READ_ONLY",
            "live_orders_enabled": False, "events": []}
