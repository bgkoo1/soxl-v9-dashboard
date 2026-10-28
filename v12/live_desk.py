"""Account-first operations desk. Each read source has its own freshness state."""
import hashlib
import time
from datetime import date
from decimal import Decimal
import pandas as pd
import streamlit as st
from v12.meritz import Client, ReadError, amount
from v12.broker_book import rebuild, buying_cash
from v12.desk_model import execution_frames, slot_table
from v12.engine import plan, prepare

INTERVALS = {"balance": 30, "activity": 60, "quote": 15}


@st.fragment(run_every="15s")
def live_clock():
    if st.session_state.get("live_auto") and time.monotonic() - st.session_state.get("live_render", 0) >= 14:
        st.rerun(scope="app")


def sync_sources(client, start, market, force=False):
    """A history failure cannot erase a successful balance or quote read."""
    calls = {"balance": client.balances, "activity": lambda: client.activity(start=start),
             "quote": lambda: client.quote(market=market)}
    now = time.monotonic()
    if force:
        client.clear_activity_cache()
    for name, call in calls.items():
        key = "desk_" + name
        if force or now - st.session_state.get(key + "_attempt", 0) >= INTERVALS[name]:
            st.session_state[key + "_attempt"] = now
            try:
                result = call()
                st.session_state[key] = result
                st.session_state[key + "_error"] = None
            except ReadError as exc:
                st.session_state[key + "_error"] = str(exc)
            except Exception:
                st.session_state[key + "_error"] = "응답을 확인하지 못했습니다. 인증정보나 원본 응답은 표시하지 않습니다."


def clock_text(payload, field):
    if not payload or not payload.get(field):
        return "미조회"
    return pd.Timestamp(payload[field]).tz_convert("Asia/Seoul").strftime("%m-%d %H:%M:%S KST")


def render_live_desk(settings, load_calendar):
    required = ["MERITZ_APP_KEY", "MERITZ_APP_SECRET", "MERITZ_MAC_ADDRESS"]
    missing = [k for k in required if not str(settings.get(k, "")).strip()]
    fingerprint = hashlib.sha256("|".join(str(settings.get(k, "")) for k in required).encode()).hexdigest()
    if st.session_state.get("desk_fingerprint") != fingerprint:
        for k in list(st.session_state):
            if k.startswith("desk_") and k not in {"desk_timezone", "desk_start"}:
                del st.session_state[k]
        st.session_state.desk_fingerprint = fingerprint
    with st.sidebar:
        st.subheader("메리츠 실계좌")
        auto = st.toggle("자동 갱신", value=True, key="live_auto")
        force = st.button("계좌·시세 지금 새로고침", type="primary")
        st.caption("SOXL 시세 15초 · 잔고 30초 · 체결 60초마다 조회")
        with st.expander("계좌·체결 설정", expanded=bool(missing)):
            if missing:
                st.error("설정 필요: " + ", ".join(missing))
                st.caption("Streamlit Secrets의 최상위 항목으로 설정합니다. 값은 이 화면에 표시하지 않습니다.")
            source_tz = st.selectbox("체결일시 기준 시간대", ["선택 필요", "Asia/Seoul", "America/New_York", "UTC"],
                                     key="desk_timezone", help="메리츠 체결내역의 표시 시간대입니다. 미국 거래일로 변환해 V12 보유일을 계산합니다.")
            source_tz = None if source_tz == "선택 필요" else source_tz
            whole = st.checkbox("계좌 개설 이후 전체 체결 조회", value=False)
            start_date = st.date_input("체결 조회 시작일", value=date(2026, 1, 1), max_value=date.today(), key="desk_start")
            market = st.selectbox("SOXL 시세 시장 코드", ["AX", "NY", "OQ"],
                                  index=["AX", "NY", "OQ"].index(str(settings.get("MERITZ_SOXL_MARKET", "AX"))) if str(settings.get("MERITZ_SOXL_MARKET", "AX")) in {"AX", "NY", "OQ"} else 0)
        with st.expander("V12 매수 배정 기준"):
            nav = st.number_input("전일 확정 V12 NAV ($)", min_value=0., value=0., step=100., key="desk_nav")
            st.caption("전일 확정 USD 전략자산입니다. 미입력 시 계좌·시세·매도 판단은 표시하고 신규매수 수량만 보류합니다.")
        if settings.get("DASHBOARD_PASSWORD") and st.button("로그아웃"):
            st.session_state.clear()
            st.rerun()
    start = "00000000" if whole else start_date.strftime("%Y%m%d")
    client = st.session_state.get("desk_client")
    if not missing and client is None:
        try:
            client = Client(settings)
            st.session_state.desk_client = client
        except ReadError as exc:
            st.error(str(exc))
    request_key = (start, market)
    if st.session_state.get("desk_request") != request_key:
        force = True
        # A failed new date range must not show the prior range as if complete.
        st.session_state.pop("desk_activity", None)
        st.session_state.desk_request = request_key
    if client and (force or auto or "desk_balance" not in st.session_state):
        sync_sources(client, start, market, force)
    balance = st.session_state.get("desk_balance")
    activity = st.session_state.get("desk_activity")
    quote = st.session_state.get("desk_quote")
    errors = {name: st.session_state.get("desk_" + name + "_error") for name in INTERVALS}
    st.session_state.live_render = time.monotonic()
    live_clock()
    st.caption("SOXL 시세 조회 " + clock_text(quote, "received_at") + " · 계좌 조회 " + clock_text(balance, "fetched_at") + " · 체결 조회 " + clock_text(activity, "history_asof"))
    status = st.columns(3)
    labels = {"balance": "메리츠 잔고·예수금", "activity": "일별 체결내역", "quote": "SOXL 현재가"}
    for col, name in zip(status, INTERVALS):
        if errors[name]:
            col.error(labels[name] + " · 조회 실패")
        elif st.session_state.get("desk_" + name):
            col.success(labels[name] + " · 조회됨")
        else:
            col.info(labels[name] + " · 연결 대기")
    if missing:
        st.info("메리츠 인증 설정을 확인하면 실제 계좌값을 표시합니다. 연결 전 금액·수량은 0으로 대신 표시하지 않습니다.")
    for name, error in errors.items():
        if error:
            st.warning(labels[name] + ": " + error + " 마지막 정상값이 있으면 조회시각과 함께 참고용으로 표시합니다.")
    price = float(quote["price"]) if quote else None
    soxl = [h for h in (balance or {}).get("holdings", []) if h["symbol"] == "SOXL"]
    quantity = sum((amount(h["quantity"]) for h in soxl), Decimal(0)) if balance else None
    unrealized = sum((amount(h["unrealized"], signed=True) for h in soxl), Decimal(0)) if balance else None
    cards = st.columns(5)
    cards[0].metric("SOXL 현재가", f"${price:,.4f}" if price else "—",
                    f"{float(quote['change_pct']):+.2f}%" if quote else None)
    cards[1].metric("USD 예수금", f"${amount(balance['cash_usd'], signed=True):,.2f}" if balance else "—")
    cards[2].metric("USD 주문가능금액", f"${amount(balance['buying_power_usd'], signed=True):,.2f}" if balance else "—")
    cards[3].metric("SOXL 보유수량", f"{quantity:,.4f}주" if quantity is not None else "—")
    cards[4].metric("SOXL 평가손익", f"${unrealized:,.2f}" if unrealized is not None else "—")
    if quote:
        st.caption(f"{quote['source']} · {quote['session']} · {'지연시세' if quote['delayed'] else '실시간 시세 요청'} · 15초 간격 REST 조회이며 틱 단위 스트리밍은 아닙니다. 표시 시각은 조회 시각입니다.")
    if balance:
        st.caption(f"평가환율 {amount(balance['fx']):,.4f}원/USD · 원화 주문가능현금 {amount(balance['krw_buying_power']):,.0f}원 · 평가손익은 메리츠 계좌 평가값입니다.")
    data, expected, next_day = None, None, None
    try:
        data, expected, next_day = load_calendar()
    except Exception:
        st.warning("V12 확정 종가 데이터를 읽지 못했습니다. 계좌·체결·시세 조회는 계속 표시합니다.")
    book, orders, blockers = None, None, []
    if balance and activity:
        try:
            combined = {**balance, **activity}
            book = rebuild(combined, source_tz)
            blockers.extend(book["issues"])
        except Exception:
            blockers.append("체결 기록을 슬롯으로 복원할 수 없습니다. 체결 시간대·이력을 확인하세요.")
    else:
        blockers.append("계좌와 체결 이력의 정상 조회가 필요합니다.")
    if errors["balance"] or errors["activity"]:
        blockers.append("계좌 또는 체결의 최신 조회가 실패했습니다.")
    if data is not None and not data.empty:
        latest = pd.Timestamp(data.Date.max())
        if latest < expected:
            blockers.append(f"확정 종가가 {latest.date()}까지입니다. {expected.date()} 종가가 필요합니다.")
        st.caption(f"V12 종가 신호 {latest.date()} · 다음 주문일 {next_day.date()} (미국 거래일)")
        if book:
            try:
                orders = plan(data, book["positions"], float(buying_cash(combined, book)), next_day,
                              book["closed_trades"], allocation_nav=nav)
                blockers.extend(orders["issues"])
            except Exception:
                blockers.append("전략 주문계획 계산을 완료하지 못했습니다.")
    else:
        blockers.append("V12 신호를 계산할 가격 데이터가 없습니다.")
    blockers = list(dict.fromkeys(blockers))
    left, right = st.columns([1.1, 1])
    with left:
        st.subheader("지금 확인할 V12 행동")
        if blockers:
            st.warning("주문 판단 확인 대기")
            for issue in blockers:
                st.write("• " + issue)
        elif orders:
            moc = [s for s in orders["sells"] if s["구분"] == "MOC"]
            if moc:
                st.error(f"다음 주문일 MOC 청산 대상 {len(moc)}슬롯")
            elif orders["sells"]:
                st.success(f"보유 {len(orders['sells'])}슬롯 · LOC +3.2% 목표가 확인")
            else:
                st.info("미청산 SOXL 슬롯이 없습니다.")
            if orders["blocked"]:
                st.warning("신규 매수 제한 · Risk B / 연속 하락 / 이력 조건을 확인하세요.")
            elif nav <= 0:
                st.info("매수 후보 계산 대기 · 사이드바에서 전일 확정 NAV를 입력하세요.")
            elif orders["buys"]:
                st.dataframe(pd.DataFrame(orders["buys"]), hide_index=True, width="stretch")
            else:
                st.info("슬롯·예수금 조건에 따른 신규 매수 배정이 없습니다.")
        if data is not None and len(data) >= 200:
            signals = prepare(data).iloc[-1]
            a, b, c = st.columns(3)
            a.metric("Risk B", "차단" if signals.risk_b else "정상")
            b.metric("연속 하락", f"{int(signals.down_streak)}일")
            c.metric("Vol20", f"{float(signals.vol20):.2%}")
            st.caption("확정 종가로 계산한 다음 주문일 필터입니다. 장중 가격으로 D2·전체청산 신호를 확정하지 않습니다.")
    daily, tape = execution_frames(activity, source_tz)
    with right:
        st.subheader("일별 실제 매수·매도 체결")
        st.caption("미국 거래일 기준" if source_tz else "메리츠 주문일 기준 · 시간대 선택 후 미국 거래일로 변환")
        if activity is None:
            st.info("체결 연결 대기 · 조회 오류가 있으면 상단에 표시합니다.")
        elif daily.empty:
            st.info("조회 기간에 SOXL 실제 체결이 없습니다.")
        else:
            chosen = st.selectbox("체결 확인일", list(daily["거래일"]))
            st.dataframe(daily[daily["거래일"] == chosen], hide_index=True, width="stretch")
            detail = tape[tape["거래일"] == chosen].sort_values("체결시각(원문)", ascending=False)
            st.dataframe(detail, hide_index=True, width="stretch")
    st.subheader("6슬롯 운용 현황 · 목표가와 다음 행동")
    if book and book["positions"] and data is not None and not data.empty:
        reference = price
        if reference is None and soxl:
            reference = float(soxl[0]["price"])
            st.caption("시세 조회 미완료: 슬롯 평가의 참고가격은 잔고 조회 당시 메리츠 현재가입니다.")
        table = slot_table(book["positions"], data, reference, next_day, None if blockers else orders)
        st.dataframe(table, hide_index=True, width="stretch")
        st.caption("주문별 부분체결을 통합하고 매도는 FIFO 참고 배분합니다. 체결 복원과 실제 잔고가 일치해야 주문계획을 사용합니다.")
    elif book and book["matched"]:
        st.info("메리츠와 대조한 미청산 SOXL 슬롯이 없습니다.")
    else:
        st.info("계좌·체결 대조 후 슬롯별 진입가·LOC 목표가·D2/D7/MOC 행동을 표시합니다.")
    details = st.tabs(["전체 일별 체결", "해외 실제 보유잔고", "계좌 거래·입출금", "연결 상태"])
    with details[0]:
        st.dataframe(daily, hide_index=True, width="stretch")
        st.dataframe(tape, hide_index=True, width="stretch")
        if not tape.empty:
            st.download_button("SOXL 실제 체결 CSV", tape.to_csv(index=False).encode("utf-8-sig"), "soxl_actual_fills.csv", "text/csv")
    with details[1]:
        st.dataframe(pd.DataFrame((balance or {}).get("holdings", [])).rename(columns={
            "symbol": "종목", "currency": "통화", "quantity": "보유수량", "sellable": "매도가능수량",
            "average_price": "평균단가", "price": "잔고 조회 현재가", "value": "평가금액", "unrealized": "평가손익"}),
            hide_index=True, width="stretch")
    with details[2]:
        st.dataframe(pd.DataFrame((activity or {}).get("transactions", [])), hide_index=True, width="stretch")
        st.caption("원화·외화 금액과 수량을 합산하지 않습니다. 실제 수수료 정보는 계좌 거래내역 기준입니다.")
    with details[3]:
        st.table(pd.DataFrame([{"연결": labels[n], "상태": errors[n] or ("조회됨" if st.session_state.get('desk_' + n) else "연결 대기"),
                                "갱신주기": f"{INTERVALS[n]}초"} for n in INTERVALS]))
        st.caption("현재 앱은 증권사 주문을 전송하지 않습니다. 시세·잔고·체결은 각각 독립적으로 조회하며 앱이 열린 동안 갱신합니다.")
