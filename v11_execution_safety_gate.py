"""V11 dry-run safety gate. Never sends broker orders."""
from v11_execution_dryrun_v3 import plan

ACTIVE = {"PENDING", "ACCEPTED", "PARTIAL", "CANCEL_REQUESTED"}
def evaluate(snapshot, control):
    """control is operator-verified, never inferred from backtest."""
    issues = []
    if not control.get("market_data_verified"):
        issues.append("확정 일봉 교차검증 미완료")
    if not control.get("broker_positions_verified"):
        issues.append("증권사 실제 잔고 대조 미완료")
    if not control.get("broker_orders_verified"):
        issues.append("증권사 미체결 주문 대조 미완료")
    if not control.get("manual_approval"):
        issues.append("실거래 전환 승인 없음")
    if not control.get("dry_run_passed"):
        issues.append("모의 운용 검증 미완료")
    if not control.get("new_portfolio_initialized"):
        issues.append("신규 포트폴리오 초기화 미완료")
    if snapshot.get("positions"):
        issues.append("기존 보유 슬롯 남아 있음")
    if any(o.get("status") in ACTIVE for o in snapshot.get("pending_orders", [])):
        issues.append("미체결·취소대기 주문 남아 있음")
    if snapshot.get("allow_buy") and not control.get("signals_verified"):
        issues.append("신규 매수 신호 미검증")
    if control.get("as_of_date") != snapshot.get("date"):
        issues.append("잔고 검증일과 주문 대상일 불일치")
    diagnostic_snapshot = dict(snapshot, allow_buy=False, risk_b=True, pause_4down=True)
    proposed = plan(diagnostic_snapshot)
    return {
        "mode": "DRY_RUN_ONLY",
        "live_orders_enabled": False,
        "review_ready": not issues,
        "blocking_reasons": issues,
        "diagnostic": proposed,
        "note": "승인 조건을 모두 충족해도 이 모듈은 주문을 전송하지 않습니다.",
    }
