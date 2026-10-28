"""User-confirmed V12 holdings overlay. Never places broker orders.

This file is an encrypted, separate manual ledger for actual holdings that the
historical strategy has closed synthetically. It NEVER edits research trades,
the legacy live_portfolio.enc, or Meritz broker data.
"""
import base64
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import requests
from cryptography.fernet import Fernet, InvalidToken

LEDGER_FILE = "v12_manual_holdings.enc"
SCHEMA_VERSION = 1
MAX_SLOTS = 1000


class LedgerError(ValueError):
    pass


def normalize_slot(slot):
    if not isinstance(slot, dict):
        raise LedgerError("잘못된 슬롯 데이터")
    slot_id = str(slot.get("slot_id", "")).strip()
    if not slot_id or len(slot_id) > 140:
        raise LedgerError("슬롯 ID가 올바르지 않습니다.")
    try:
        bought_on = datetime.strptime(str(slot.get("entry_date", "")), "%Y-%m-%d").date().isoformat()
        price = Decimal(str(slot.get("entry_price")))
        quantity = Decimal(str(slot.get("quantity")))
    except (ValueError, TypeError, InvalidOperation):
        raise LedgerError("매수일·매수가·수량을 확인하세요.") from None
    if not price.is_finite() or not quantity.is_finite() or price <= 0 or quantity <= 0:
        raise LedgerError("실제 매수가와 보유수량은 0보다 커야 합니다.")
    # Never allow an unrealistically large numeric value or sub-share decimal
    # to masquerade as a reconciled trade quantity.
    if price > Decimal("10000000") or quantity > Decimal("100000000"):
        raise LedgerError("매수가 또는 수량이 비정상적으로 큽니다.")
    note = str(slot.get("note", "")).strip()[:160]
    model_exit = str(slot.get("model_exit", "직접 등록")).strip()[:60]
    source = str(slot.get("source", "manual")).strip()
    if source not in {"model_closed", "manual"}:
        raise LedgerError("슬롯 출처 정보가 올바르지 않습니다.")
    return {
        "slot_id": slot_id,
        "entry_date": bought_on,
        "entry_price": str(price),
        "quantity": str(quantity),
        "source": source,
        "model_exit": model_exit,
        "note": note,
        "broker_verified": False,
    }


def validate_ledger(rows):
    if not isinstance(rows, list) or len(rows) > MAX_SLOTS:
        raise LedgerError("슬롯 원장 크기가 올바르지 않습니다.")
    normalized = [normalize_slot(row) for row in rows]
    keys = [s["slot_id"] for s in normalized]
    if len(set(keys)) != len(keys):
        raise LedgerError("중복된 슬롯 ID가 존재합니다.")
    return normalized


def reopen_slot(rows, slot):
    """Explicitly reinstate a user-confirmed open position without changing backtest."""
    existing = validate_ledger(rows)
    restored = normalize_slot(slot)
    if any(x["slot_id"] == restored["slot_id"] for x in existing):
        raise LedgerError("이미 실제 보유 원장에 복구된 슬롯입니다.")
    return validate_ledger(existing + [restored])


def close_manual_slot(rows, slot_id):
    """Remove from *manual* open list only on explicit user confirmation."""
    existing = validate_ledger(rows)
    new = [x for x in existing if x["slot_id"] != slot_id]
    if len(new) == len(existing):
        raise LedgerError("복구 원장에서 슬롯을 찾을 수 없습니다.")
    return new


def _config(secrets):
    try:
        token = str(secrets.get("GITHUB_TOKEN", "")).strip()
        secret = str(secrets.get("PORTFOLIO_ENCRYPTION_KEY", "")).strip()
        repo = str(secrets.get("GITHUB_REPO", "bgkoo1/soxl-v9-dashboard")).strip()
        branch = str(secrets.get("GITHUB_BRANCH", "main")).strip()
        if not token or not secret or not repo or not branch:
            raise LedgerError("GitHub 저장 및 암호화 Secrets가 필요합니다.")
        cipher = Fernet(secret.encode())
        return token, cipher, repo, branch
    except (TypeError, ValueError, KeyError) as exc:
        raise LedgerError("GitHub 암호화 저장 설정을 확인하세요.") from None


def _remote(secrets):
    token, cipher, repo, branch = _config(secrets)
    return (
        f"https://api.github.com/repos/{repo}/contents/{LEDGER_FILE}",
        {"Authorization": f"Bearer {token}",
         "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"},
        cipher, branch
    )


def _normalize_state(state):
    """Backwards-compatible V12 encrypted state, preserving user transaction history."""
    if not isinstance(state,dict):
        raise LedgerError("원장 구조가 올바르지 않습니다.")
    version=state.get("schema_version",SCHEMA_VERSION)
    if version != SCHEMA_VERSION:
        raise LedgerError("알 수 없는 V12 원장 버전입니다.")
    from v12_trade_management import validate_closed_trades, validate_cashflows
    return {
        "schema_version":SCHEMA_VERSION,
        "positions":validate_ledger(state.get("positions",[])),
        "closed_trades":validate_closed_trades(state.get("closed_trades",[])),
        "cashflows":validate_cashflows(state.get("cashflows",[])),
    }


def load_portfolio_state(secrets,sender=None):
    """Read the entire V12 state; failure never silently fabricates an empty account."""
    sender=sender or requests
    url,headers,cipher,branch=_remote(secrets)
    try:
        response=sender.get(url,headers=headers,params={"ref":branch},timeout=15)
        if response.status_code==404:
            return _normalize_state({}),None
        response.raise_for_status()
        item=response.json()
        state=json.loads(cipher.decrypt(base64.b64decode(item["content"])).decode("utf-8"))
        return _normalize_state(state),str(item["sha"])
    except (InvalidToken,UnicodeDecodeError):
        raise LedgerError("V12 보유 원장 복호화 실패") from None
    except requests.RequestException:
        raise LedgerError("GitHub 원장 읽기 실패: 기존 기록을 빈 값으로 대체하지 않습니다.") from None
    except (KeyError,TypeError,ValueError) as exc:
        if isinstance(exc,LedgerError):
            raise
        raise LedgerError("V12 보유 원장 데이터 형식 오류") from None


def save_portfolio_state(secrets,state,expected_sha,sender=None):
    """Atomic encrypted write (positions + real sells + cashflows) with SHA guard."""
    sender=sender or requests
    data=_normalize_state(state)
    url,headers,cipher,branch=_remote(secrets)
    payload={**data,"updated_at":datetime.now(timezone.utc).isoformat()}
    encoded=base64.b64encode(cipher.encrypt(json.dumps(
        payload,ensure_ascii=False,separators=(",",":")).encode("utf-8"))).decode("ascii")
    body={"message":"Update encrypted V12 actual execution journal",
          "content":encoded,"branch":branch}
    if expected_sha is not None:
        body["sha"]=expected_sha
    try:
        response=sender.put(url,headers=headers,json=body,timeout=20)
        if response.status_code in (409,412,422):
            raise LedgerError("다른 세션에서 원장이 변경됐습니다. 최신 기록을 다시 읽으세요.")
        response.raise_for_status()
        item=response.json()
        result=item.get("content",{}).get("sha")
        if not result:
            raise LedgerError("암호화 원장 저장 확인 실패")
        return str(result)
    except requests.RequestException:
        raise LedgerError("GitHub 암호화 원장 저장 실패: 기존 기록을 보존합니다.") from None


def load_ledger(secrets,sender=None):
    """Legacy compatibility: existing V12 UI still receives positions + SHA."""
    state,sha=load_portfolio_state(secrets,sender)
    return state["positions"],sha


def save_ledger(secrets,positions,expected_sha,sender=None):
    """Preserve transaction journal when old UI updates only open positions."""
    state,current_sha=load_portfolio_state(secrets,sender)
    if current_sha!=expected_sha:
        raise LedgerError("최신 V12 원장이 변경됐습니다. 새로고침 후 다시 저장하세요.")
    state["positions"]=validate_ledger(positions)
    return save_portfolio_state(secrets,state,current_sha,sender)

def reconcile_total(positions, broker_soxl_quantity):
    try:
        broker = Decimal(str(broker_soxl_quantity))
        manual = sum((Decimal(x["quantity"]) for x in validate_ledger(positions)), Decimal("0"))
    except (InvalidOperation, ValueError, TypeError):
        raise LedgerError("잔고 수량을 확인하세요.") from None
    if broker < 0:
        raise LedgerError("잔고 수량이 올바르지 않습니다.")
    return {"manual": str(manual), "broker": str(broker),
            "difference": str(broker-manual), "matched": broker == manual}
