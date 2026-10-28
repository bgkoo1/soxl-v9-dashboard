"""Session-private account refresh and read-only broker views."""
import hashlib
import time
from datetime import date
from decimal import Decimal
import pandas as pd
import streamlit as st
from v12.meritz import Client, ReadError, amount
from v12.broker_book import rebuild, buying_cash


@st.fragment(run_every="60s")
def refresh_clock():
    if (st.session_state.get("broker_mode_enabled") and
            time.monotonic() - st.session_state.get("broker_attempt", 0) >= 300):
        st.rerun(scope="app")


def controls(settings):
    """Called inside sidebar, only after dashboard authentication."""
    configured = bool(settings.get("MERITZ_APP_KEY") and settings.get("MERITZ_APP_SECRET"))
    mode = st.radio("운용 데이터 기준", ["메리츠 실계좌", "수동 원장"], index=0 if configured else 1)
    enabled = mode == "메리츠 실계좌"
    st.session_state.broker_mode_enabled = enabled
    if not enabled:
        return False, None, None, None
    st.caption("접속 시 조회 · 열린 화면에서 5분마다 갱신 · 계좌 원본은 세션에만 유지")
    start = st.date_input("체결 이력 조회 시작일", value=date(2026, 1, 1), max_value=date.today(), key="broker_history_start")
    whole = st.checkbox("계좌 개설 이후 전체 이력 조회", value=False)
    tz = st.selectbox("체결일시 기준 시간대", ["선택 필요", "Asia/Seoul", "America/New_York", "UTC"],
                      help="메리츠 체결내역에 표시되는 시간대에 맞춥니다. V12 보유일은 미국 거래일로 변환합니다.")
    source_tz = None if tz == "선택 필요" else tz
    start_string = "00000000" if whole else start.strftime("%Y%m%d")
    fingerprint = hashlib.sha256("|".join(str(settings.get(k, "")) for k in [
        "MERITZ_APP_KEY", "MERITZ_APP_SECRET", "MERITZ_MAC_ADDRESS"]).encode()).hexdigest()
    request_key = (fingerprint, start_string, date.today().isoformat())
    refresh = st.button("메리츠 계좌 새로고침", type="primary")
    expired = time.monotonic() - st.session_state.get("broker_attempt", 0) >= 300
    if refresh or expired or st.session_state.get("broker_request") != request_key:
        st.session_state.broker_attempt = time.monotonic()
        st.session_state.broker_request = request_key
        st.session_state.broker_error = None
        try:
            if st.session_state.get("broker_client_key") != fingerprint:
                st.session_state.pop("broker_live_snapshot", None)
                st.session_state.broker_client = Client(settings)
                st.session_state.broker_client_key = fingerprint
            # Publish only a complete snapshot, never a partial result.
            with st.spinner("메리츠 예수금·잔고·체결 상세 조회 중…"):
                snapshot = st.session_state.broker_client.snapshot(start=start_string)
            st.session_state.broker_live_snapshot = snapshot
        except ReadError as exc:
            st.session_state.broker_error = str(exc)
        except Exception:
            st.session_state.broker_error = "계좌 응답 처리 중 오류가 발생했습니다. 원본 응답·인증정보는 표시하지 않습니다."
    snapshot = st.session_state.get("broker_live_snapshot")
    error = st.session_state.get("broker_error")
    if error:
        st.error(error)
    book = None
    if snapshot and not error:
        try:
            book = rebuild(snapshot, source_tz)
        except Exception:
            error = "체결 이력의 수량·시각을 복원할 수 없습니다. 시간대·거래 이력을 확인하세요."
            st.error(error)
    if snapshot:
        stamp = pd.Timestamp(snapshot["fetched_at"]).tz_convert("Asia/Seoul")
        st.caption(f"마지막 정상 조회 {stamp:%Y-%m-%d %H:%M:%S KST}")
    refresh_clock()
    return enabled, snapshot, book, error


def summary(snapshot, book):
    soxl = [h for h in snapshot["holdings"] if h["symbol"] == "SOXL"]
    usd_value = sum((amount(h["value"]) for h in snapshot["holdings"] if h["currency"] == "USD"), Decimal(0))
    value = sum((amount(h["value"]) for h in soxl), Decimal(0))
    qty = sum((amount(h["quantity"]) for h in soxl), Decimal(0))
    c = st.columns(4)
    c[0].metric("메리츠 USD 예수금", f"${amount(snapshot['cash_usd'], signed=True):,.2f}")
    c[1].metric("USD 주문가능금액", f"${amount(snapshot['buying_power_usd'], signed=True):,.2f}")
    c[2].metric("SOXL 실제 잔고", f"{qty:,.4f}주", f"평가액 ${value:,.2f}", delta_color="off")
    c[3].metric("USD 현금·보유종목 평가합계", f"${amount(snapshot['cash_usd'], signed=True)+usd_value:,.2f}")
    st.caption(f"메리츠 평가환율 {amount(snapshot['fx']):,.4f}원/USD · 원화 주문가능현금 {amount(snapshot['krw_buying_power']):,.0f}원 · USD 예수금과 주문가능금액은 각각 표시하며 합산하지 않습니다.")
    if book:
        if book["matched"]:
            st.success(f"SOXL 체결 복원 수량과 메리츠 잔고 일치 · 미청산 매수 주문 {len(book['positions'])}개")
        else:
            for issue in book["issues"]:
                st.warning(issue)


def holdings_view(snapshot, book):
    st.subheader("메리츠 실제 해외 보유잔고")
    frame = pd.DataFrame(snapshot["holdings"]).rename(columns={
        "symbol": "종목", "currency": "통화", "quantity": "보유수량", "sellable": "매도가능수량",
        "average_price": "평균단가", "price": "현재가", "value": "평가금액", "unrealized": "평가손익"})
    st.dataframe(frame, hide_index=True, width="stretch")
    st.subheader("SOXL 체결로 복원한 주문별 슬롯")
    if book:
        st.dataframe(pd.DataFrame(book["positions"]), hide_index=True, width="stretch")
    st.caption("동일 매수 주문의 부분체결은 한 슬롯으로 합칩니다. 매도는 FIFO로 잔여 매수분에 배분한 참고 원장입니다. 메리츠의 세무상 매입분 지정과 같다는 의미는 아닙니다. 수량 불일치·매도가능수량 부족·미체결이 있으면 주문계획을 보류합니다.")
    st.info("실계좌 모드의 보유·체결은 메리츠 조회값으로 갱신됩니다. 수동 원장 수정은 별도 모드에서 할 수 있습니다.")


def journal_view(snapshot, book):
    st.subheader("메리츠 실제 주문체결 상세")
    frame = pd.DataFrame(snapshot["fills"])
    if not frame.empty:
        show = frame[["order_date", "order_no", "execution_no", "timestamp", "symbol", "currency", "side", "quantity", "price"]]
        st.dataframe(show, hide_index=True, width="stretch")
        st.download_button("실제 체결 CSV 내보내기", show.to_csv(index=False).encode("utf-8-sig"), "meritz_actual_fills.csv", "text/csv")
    else:
        st.info("선택한 기간의 실제 체결이 없습니다.")
    st.subheader("계좌 거래·입출금 내역")
    st.dataframe(pd.DataFrame(snapshot["transactions"]), hide_index=True, width="stretch")
    st.caption("거래내역의 원화금액·외화금액·수량·수수료는 서로 다른 항목입니다. 통화가 다른 금액을 합산하지 않습니다. 원본 계좌·상대계좌 정보는 화면이나 GitHub에 저장하지 않습니다.")
    if snapshot["outstanding"]:
        st.subheader("조회 기간 내 미체결 주문")
        st.dataframe(pd.DataFrame(snapshot["outstanding"]), hide_index=True, width="stretch")
