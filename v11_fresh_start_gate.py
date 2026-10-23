"""Fail-closed prerequisites for a fresh V11 portfolio. No broker calls or orders."""
from decimal import Decimal, InvalidOperation

def evaluate_fresh_start(broker):
    issues=[]
    if not broker.get("authenticated_and_queried"):
        issues.append("메리츠 계좌 조회 성공 및 응답 형식 검증 필요")
    if not broker.get("positions_complete"):
        issues.append("모든 계좌 보유 종목·수량 조회 검증 필요")
    elif broker.get("positions") != []:
        issues.append("실계좌 보유 종목이 남아 있음")
    if not broker.get("orders_complete"):
        issues.append("미체결·취소대기 주문 전체 조회 검증 필요")
    elif broker.get("active_orders") != []:
        issues.append("미체결·취소대기 주문이 남아 있음")
    try:
        cash=Decimal(str(broker.get("available_cash")))
        if not cash.is_finite() or cash<=0:
            raise InvalidOperation()
    except (InvalidOperation,TypeError,ValueError):
        issues.append("실제 운용 가능 현금 미확인")
    if not broker.get("balance_as_of_verified"):
        issues.append("계좌 잔고 조회 시각·거래일 검증 필요")
    if not broker.get("strategy_and_order_rules_verified"):
        issues.append("V11 주문 규칙·브로커 주문 유형 검증 필요")
    if not broker.get("dry_run_passed"):
        issues.append("모의 운용 및 주문 장애 대응 검증 필요")
    if not broker.get("market_data_verified"):
        issues.append("가격 데이터 검증 필요")
    if not broker.get("explicit_live_approval"):
        issues.append("실거래 전환 최종 승인 필요")
    return {"mode":"DRY_RUN_ONLY","live_orders_enabled":False,
            "review_ready":not issues,"blocking_reasons":issues,
            "note":"이 모듈은 주문을 실행하거나 신규 원장을 생성하지 않습니다."}
