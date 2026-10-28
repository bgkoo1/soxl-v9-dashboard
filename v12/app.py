"""Fresh Streamlit UI. No legacy dashboard or strategy imports."""
from datetime import date
from pathlib import Path
from uuid import uuid4
import hmac
import json
import pandas as pd
import pandas_market_calendars as mcal
import plotly.graph_objects as go
import streamlit as st
from v12.engine import backtest, plan
from v12_manual_holdings import load_portfolio_state, save_portfolio_state, reopen_slot, LedgerError
from v12_trade_management import record_sale, cancel_sale, update_slot, add_cashflow, realized_summary
from v12.broker_ui import controls as broker_controls, summary as broker_summary, holdings_view, journal_view
from v12.broker_book import buying_cash

ROOT = Path(__file__).resolve().parents[1]
BUILD = "V12-LIVE-DESK-20261028"


def config():
    try:
        return dict(st.secrets)
    except (FileNotFoundError, st.errors.StreamlitSecretNotFoundError):
        return {}


@st.cache_data(ttl=120)
def calendar_data():
    now = pd.Timestamp.now(tz="UTC")
    schedule = mcal.get_calendar("NYSE").schedule(
        start_date=(now - pd.Timedelta(days=15)).date(),
        end_date=(now + pd.Timedelta(days=15)).date())
    completed = schedule[schedule.market_close < now - pd.Timedelta(minutes=20)]
    expected = pd.Timestamp(completed.index[-1]).normalize()
    next_day = pd.Timestamp(schedule[schedule.index > expected].index[0]).normalize()
    data = pd.read_csv(ROOT / "SOXL_adjusted.csv")
    data["Date"] = pd.to_datetime(data["Date"]).dt.normalize()
    return data[data.Date <= expected].copy(), expected, next_day


def pct(n):
    return "—" if n is None or pd.isna(n) else f"{n:.2%}"


def save(new_state, settings):
    try:
        if st.session_state.get("storage_error"):
            raise LedgerError("저장 원장을 읽지 못해 변경을 차단했습니다. 다시 불러오세요.")
        if st.session_state.persistent:
            sha = save_portfolio_state(settings, new_state, st.session_state.ledger_sha)
            st.session_state.ledger_sha = sha
        st.session_state.ledger = new_state
        st.session_state.notice = "암호화 저장 완료" if st.session_state.persistent else "현재 세션에 반영했습니다. 영구 저장은 설정 후 사용할 수 있습니다."
        st.rerun()
    except LedgerError as exc:
        st.error(str(exc))


def render():
    st.set_page_config(page_title="SOXL · V12 Quant", page_icon="◈", layout="wide")
    settings = config()
    private = any(settings.get(k) for k in ["GITHUB_TOKEN", "MERITZ_APP_KEY", "MERITZ_APP_SECRET"])
    password = str(settings.get("DASHBOARD_PASSWORD", ""))
    if private:
        if not password:
            st.error("실계좌·원장 기능을 사용하려면 Streamlit Secrets에 DASHBOARD_PASSWORD를 설정하세요.")
            st.stop()
        if not st.session_state.get("authenticated"):
            st.title("SOXL V12 · 운용자 로그인")
            with st.form("login"):
                st.caption(BUILD)
                entered = st.text_input("대시보드 비밀번호", type="password", autocomplete="current-password")
                if st.form_submit_button("로그인"):
                    if hmac.compare_digest(entered.encode(), password.encode()):
                        st.session_state.authenticated = True
                        st.rerun()
                    st.error("비밀번호를 확인하세요.")
            st.stop()
    st.markdown("""<style>
    .stApp{background:#f4f6fa;color:#16243b}
    .block-container{padding-top:2rem;max-width:1500px}
    div[data-testid='stMetric']{background:white;border:1px solid #dce3ed;border-radius:14px;padding:18px}
    .hero{background:#11263d;color:white;border-radius:20px;padding:26px 30px;margin-bottom:22px}
    .hero small{color:#70dfc6;letter-spacing:2px}.hero h1{color:white;margin:5px 0;font-size:32px}
    .hero p{color:#c5d2e1;margin:0}button[kind='primary']{border-radius:9px}
    </style>""", unsafe_allow_html=True)
    st.markdown('<div class="hero"><small>SOXL QUANT INVESTING</small><h1>V12 Final · 6슬롯 운용</h1><p>메리츠 실제 계좌 · SOXL 현재가 · 오늘의 체결과 다음 행동</p></div>', unsafe_allow_html=True)
    with st.sidebar:
        st.caption(BUILD)
        workspace = st.radio("운용 화면", ["메리츠 실계좌", "수동 원장·백테스트"], index=0, key="workspace_mode")
    if workspace == "메리츠 실계좌":
        from v12.live_desk import render_live_desk
        render_live_desk(settings, calendar_data)
        return
    persistent = bool(settings.get("GITHUB_TOKEN") and settings.get("PORTFOLIO_ENCRYPTION_KEY"))
    if "ledger" not in st.session_state:
        st.session_state.ledger = {"schema_version": 1, "positions": [], "closed_trades": [], "cashflows": []}
        st.session_state.ledger_sha = None
        st.session_state.persistent = persistent
        st.session_state.storage_error = None
        if persistent:
            try:
                state, sha = load_portfolio_state(settings)
                st.session_state.ledger, st.session_state.ledger_sha = state, sha
            except LedgerError as exc:
                st.session_state.storage_error = str(exc)
    if "notice" in st.session_state:
        st.success(st.session_state.pop("notice"))
    with st.sidebar:
        st.title("◈ V12 QUANT")
        st.caption(BUILD)
        st.success("6슬롯 · TB3 OFF")
        st.caption("메리츠 조회 전용 · 주문은 직접 실행")
        broker_mode, broker_snapshot, broker_book, broker_error = False, None, None, None
        confirmed_nav = None
        if broker_mode:
            cash = float(buying_cash(broker_snapshot, broker_book)) if broker_snapshot and broker_book and not broker_error else 0.
            fx = float(broker_snapshot["fx"]) if broker_snapshot else 0.
            confirmed_nav = st.number_input("V12 전일 확정 NAV ($)", min_value=0., value=0., step=100., key="broker_nav",
                help="NAV/6 매수 예산의 기준입니다. V12에 배정한 전일 확정 USD 현금+SOXL 평가액을 입력합니다. 현재 장중 잔고를 전일 확정 NAV로 간주하지 않습니다.")
            st.caption("전일 확정 NAV 입력 전에는 신규매수 계획을 보류합니다. 예수금·보유·체결 조회에는 영향을 주지 않습니다.")
        else:
            cash = st.number_input("전일 확정 USD 예수금 ($)", min_value=0., value=0., step=100., key="cash_usd")
            fx = st.number_input("평가환율 (원/USD, 직접 입력)", min_value=1., value=1400., step=1.)
            st.caption("예수금은 전일 확정 잔액을 입력합니다. 당일 매도 예상대금은 더하지 않습니다.")
        if st.button("가격·원장 다시 불러오기"):
            calendar_data.clear()
            for key in ["ledger", "ledger_sha", "storage_error", "broker_snapshot"]:
                st.session_state.pop(key, None)
            st.rerun()
        st.caption("암호화 저장 연결됨" if persistent else "세션 모드 · 새 세션에서 입력값 초기화")
        if private and st.button("로그아웃"):
            st.session_state.clear()
            st.rerun()
    state = st.session_state.ledger
    if broker_mode:
        if broker_snapshot is None or broker_error or broker_book is None:
            st.error("메리츠 계좌를 정상 조회하지 못했습니다. 사이드바에서 설정을 확인하고 다시 조회하세요.")
            if broker_snapshot:
                st.caption("마지막 정상 조회 자료이며 최신 운용값으로 사용하지 않습니다.")
                broker_summary(broker_snapshot, None)
            st.stop()
        state = broker_book
    if st.session_state.storage_error and not broker_mode:
        st.error(st.session_state.storage_error)
        st.info("원장 복구 전에는 실제 보유 기반 주문계획을 표시하지 않습니다.")
        st.stop()
    try:
        data, expected, next_day = calendar_data()
        if data.empty:
            raise ValueError("확정 가격 데이터가 없습니다.")
        latest = pd.Timestamp(data.Date.max())
        stale = latest < expected
        orders = plan(data, state["positions"], cash, next_day, state["closed_trades"], allocation_nav=confirmed_nav)
    except (ValueError, KeyError, IndexError) as exc:
        st.error(f"가격·거래일 검증 실패: {exc}")
        st.stop()
    st.caption(f"확정 종가 {latest.date()} · 다음 주문일 {next_day.date()} (미국 거래일) · 현재 {pd.Timestamp.now(tz='Asia/Seoul').strftime('%Y-%m-%d %H:%M KST')}")
    if stale:
        st.warning(f"종가 데이터가 지연됐습니다. 필요한 최신 거래일은 {expected.date()}입니다. 주문계획을 숨깁니다.")
    qty = sum(float(p["quantity"]) for p in state["positions"])
    realized = float(realized_summary(state))
    if broker_mode:
        broker_summary(broker_snapshot, broker_book)
    else:
        cols = st.columns(4)
        cols[0].metric("기록 기반 평가자산", f"${orders['nav']:,.2f}", help="입력 예수금 + 기록 수량 × 확정 종가. 증권사 잔고가 아닙니다.")
        cols[1].metric("사용 중인 슬롯", f"{len(state['positions'])} / 6", f"{qty:,.4f}주", delta_color="off")
        cols[2].metric("슬롯 배정액 · NAV/6", f"${orders['nav']/6:,.2f}")
        cols[3].metric("실현 손익 · 비용 전", f"${realized:,.2f}", f"{realized*fx:,.0f}원 · 입력 환율", delta_color="off")
    tab_orders, tab_hold, tab_journal, tab_research, tab_settings = st.tabs([
        "오늘의 주문계획", "실제 보유·체결 관리", "매매일지·입출금", "V12 백테스트", "전략·연결 상태"])
    with tab_orders:
        st.subheader("다음 거래일의 실행 계획")
        flags = st.columns(3)
        flags[0].metric("Risk B", "차단" if orders["risk_b"] else "정상")
        flags[1].metric("연속 하락", f"{orders['streak']}거래일")
        flags[2].metric("20일 변동성", pct(orders["vol20"]))
        for issue in orders["issues"]:
            st.error(issue)
        if broker_mode:
            orders["issues"].extend(broker_book["issues"])
            if not confirmed_nav:
                orders["buys"] = []
                st.info("신규매수 계획에는 사이드바의 V12 전일 확정 NAV가 필요합니다.")
            st.caption("매수 재원은 실제 USD 예수금과 주문가능금액 중 보수적인 금액입니다. 당일 매도대금·재매매 가능 금액을 제외하며, 다른 종목 평가액은 V12 슬롯 예산에 포함하지 않습니다.")
        if not stale and (not broker_mode or (broker_book["matched"] and not orders["issues"])):
            st.markdown("**매도 · 우선순위 반영**")
            if orders["sells"]:
                st.dataframe(pd.DataFrame(orders["sells"]), hide_index=True, width="stretch")
            else:
                st.info("기록된 보유 슬롯이 없습니다.")
            st.markdown("**신규 매수 · 당일 최대 2슬롯**")
            if orders["buys"]:
                st.dataframe(pd.DataFrame(orders["buys"]), hide_index=True, width="stretch")
            else:
                st.info("매수 제한·슬롯 한도·예수금 조건에 따라 신규 매수 계획이 없습니다.")
            st.caption("MOC 참고수량은 확정 종가 기준 추정치입니다. 주문 전 증권사 주문가능금액과 가격 변동을 확인하세요. 매도 예정 슬롯·대금은 매수 여력에 미리 포함하지 않습니다.")
            frame = pd.DataFrame(orders["sells"] + orders["buys"])
            st.download_button("주문계획 CSV", frame.to_csv(index=False).encode("utf-8-sig"), "v12_order_plan.csv", "text/csv")
        st.caption("전체청산 → D2 개별청산 → D7 → LOC 순서로 슬롯마다 매도계획 하나만 생성합니다. 전략상 청산 신호는 실제 체결을 뜻하지 않습니다.")
    with tab_hold:
        if broker_mode:
            holdings_view(broker_snapshot, broker_book)
        else:
            st.subheader("실제 체결 기록으로 관리하는 6슬롯")
            if state["positions"]:
                table = pd.DataFrame(state["positions"])
                table["LOC 목표가($)"] = table.entry_price.astype(float) * 1.032
                table["평가손익($)"] = (orders["reference"] - table.entry_price.astype(float)) * table.quantity.astype(float)
                st.dataframe(table[["slot_id", "entry_date", "entry_price", "quantity", "LOC 목표가($)", "평가손익($)"]], hide_index=True, width="stretch")
            actions = st.tabs(["매수 등록", "실제 매도 등록", "보유 수정", "매도 취소·복구"])
            with actions[0], st.form("buy"):
                entry_date = st.date_input("매수 체결일", value=latest.date(), max_value=date.today())
                price = st.number_input("실제 매수가 ($)", min_value=.0001, value=float(orders["reference"]), format="%.4f")
                quantity = st.number_input("실제 매수수량 (주)", min_value=.0001, value=1., format="%.4f")
                note = st.text_input("매수 메모")
                confirmed = st.checkbox("실제 체결내역을 확인했습니다", key="buy_confirm")
                if st.form_submit_button("매수 체결 반영", type="primary"):
                    if not confirmed:
                        st.error("체결내역 확인을 선택하세요.")
                    elif len(state["positions"]) >= 6:
                        st.error("V12 최대 6슬롯입니다.")
                    elif pd.Timestamp(entry_date) not in set(data.Date):
                        st.error("확정 가격 데이터에 있는 거래일을 선택하세요.")
                    else:
                        slot = {"slot_id": "actual-" + uuid4().hex[:12], "entry_date": str(entry_date), "entry_price": str(price), "quantity": str(quantity), "note": note, "source": "manual"}
                        save({**state, "positions": reopen_slot(state["positions"], slot)}, settings)
            with actions[1]:
                if state["positions"]:
                    with st.form("sell"):
                        ident = st.selectbox("매도한 슬롯", [p["slot_id"] for p in state["positions"]])
                        sold_date = st.date_input("매도 체결일", max_value=date.today())
                        sold_price = st.number_input("실제 매도가 ($)", min_value=.0001, value=float(orders["reference"]), format="%.4f")
                        sold_qty = st.number_input("실제 매도수량 (주)", min_value=.0001, value=1., format="%.4f")
                        reason = st.selectbox("매도 사유", ["LOC +3.2%", "D2 손절", "D7 MOC", "조건부 전체 MOC", "수동 매도", "기타"])
                        checked = st.checkbox("증권사에서 실제 매도 체결을 확인했습니다")
                        if st.form_submit_button("매도 체결 반영", type="primary"):
                            if checked:
                                try:
                                    save(record_sale(state, ident, quantity=sold_qty, exit_date=str(sold_date), exit_price=sold_price, exit_strategy=reason), settings)
                                except LedgerError as exc:
                                    st.error(str(exc))
                            else:
                                st.error("실제 체결 확인을 선택하세요.")
                else:
                    st.info("매도할 보유 슬롯이 없습니다.")
            with actions[2]:
                if state["positions"]:
                    ident = st.selectbox("수정할 슬롯", [p["slot_id"] for p in state["positions"]], key="edit_slot")
                    p = next(p for p in state["positions"] if p["slot_id"] == ident)
                    with st.form("edit_" + ident):
                        d = st.date_input("수정 매수일", value=date.fromisoformat(p["entry_date"]), max_value=date.today())
                        px = st.number_input("수정 매수가 ($)", min_value=.0001, value=float(p["entry_price"]), format="%.4f")
                        q = st.number_input("수정 보유수량", min_value=.0001, value=float(p["quantity"]), format="%.4f")
                        if st.form_submit_button("수정 반영"):
                            if pd.Timestamp(d) not in set(data.Date):
                                st.error("가격 데이터의 거래일을 선택하세요.")
                            else:
                                try:
                                    save(update_slot(state, ident, entry_date=str(d), entry_price=px, quantity=q), settings)
                                except LedgerError as exc:
                                    st.error(str(exc))
            with actions[3]:
                if state["closed_trades"]:
                    names = {t["id"]: f"{t['exit_date']} · {t['slot_id']} · {t['quantity']}주" for t in state["closed_trades"]}
                    ident = st.selectbox("취소할 매도 기록", list(names), format_func=names.get)
                    checked = st.checkbox("잘못 기록된 매도를 취소하고 수량을 복구합니다")
                    if st.button("선택한 매도 취소"):
                        if checked:
                            try:
                                save(cancel_sale(state, ident), settings)
                            except LedgerError as exc:
                                st.error(str(exc))
                        else:
                            st.error("취소·복구 확인을 선택하세요.")
                else:
                    st.info("취소할 매도 기록이 없습니다.")
    with tab_journal:
        if broker_mode:
            journal_view(broker_snapshot, broker_book)
        else:
            st.subheader("실제 매매일지")
            st.dataframe(pd.DataFrame(state["closed_trades"]), hide_index=True, width="stretch")
            st.subheader("입출금 기록")
            st.caption("기록용입니다. 사이드바 USD 예수금과 자동 합산하지 않습니다.")
            st.dataframe(pd.DataFrame(state["cashflows"]), hide_index=True, width="stretch")
            with st.form("cashflow"):
                d = st.date_input("입출금일", max_value=date.today())
                typ = st.selectbox("입출금 구분", ["입금", "출금"])
                cur = st.selectbox("통화", ["USD", "KRW"])
                amount = st.number_input("입출금 금액", min_value=.01, value=100., step=100.)
                if st.form_submit_button("입출금 기록 반영"):
                    try:
                        save(add_cashflow(state, date_value=str(d), flow_type=typ, currency=cur, amount=amount), settings)
                    except LedgerError as exc:
                        st.error(str(exc))
            st.download_button("원장 JSON 내보내기", json.dumps(state, ensure_ascii=False, indent=2), "v12_actual_journal.json", "application/json")
    with tab_research:
        st.subheader("독립 V12 엔진 · USD 기준 연구")
        c1, c2, c3 = st.columns(3)
        initial = c1.number_input("백테스트 초기 USD 자산", min_value=100., value=100000., step=1000.)
        minimum = data.Date.iloc[min(200, len(data)-1)].date()
        start = c2.date_input("시작일", value=max(minimum, date(2020, 1, 1)), min_value=minimum, max_value=latest.date())
        end = c3.date_input("종료일", value=latest.date(), min_value=minimum, max_value=latest.date())
        st.caption("조정종가 · 소수점 수량 · 종가 경매 전량 체결 · 비용·슬리피지 미반영. 실제 보유 원장과 별도의 연구 결과입니다. 첫 진입 전 MA200 준비 기간을 요구합니다.")
        if start > end:
            st.error("종료일은 시작일 이후여야 합니다.")
        else:
            research = backtest(data, initial, start, end)
            metrics = st.columns(4)
            for col, (label, value) in zip(metrics, [("CAGR", pct(research["stats"]["cagr"])), ("MDD", pct(research["stats"]["mdd"])), ("총수익률", pct(research["stats"]["total_return"])), ("완료 거래", str(len(research["trades"])))]):
                col.metric(label, value)
            eq = research["equity"]
            fig = go.Figure(go.Scatter(x=eq.Date, y=eq.NAV_USD, name="NAV ($)", line={"color": "#167e78", "width": 2}))
            fig.update_layout(height=340, margin=dict(l=0, r=0, t=15, b=0), yaxis_title="USD", paper_bgcolor="rgba(0,0,0,0)")
            st.plotly_chart(fig, width="stretch")
            st.area_chart(eq.set_index("Date")[["Drawdown"]])
            with st.expander("연구 거래·일별 신호"):
                st.dataframe(research["trades"].tail(100), hide_index=True, width="stretch")
                st.dataframe(eq.tail(30), hide_index=True, width="stretch")
                st.download_button("백테스트 거래 CSV", research["trades"].to_csv(index=False).encode("utf-8-sig"), "v12_research_trades.csv", "text/csv")
    with tab_settings:
        st.subheader("SOXL V12 Final · 전략 명세 1.0")
        st.table(pd.DataFrame([
            ("분할", "전일 확정 NAV/6 · 최대 6슬롯 · 별도 강제 현금유보 없음"),
            ("신규 진입", "기본 1슬롯 MOC + 전일 종가×0.91 급락 LOC 1슬롯"),
            ("익절", "진입가×1.032 LOC"),
            ("D2", "D0=매수일 · D2 종가 수익률 ≤−10% → D3 개별 MOC"),
            ("전체청산", "D2 손절 발생 AND 당일 Vol20≥0.10 → 신호일 보유 전 슬롯 다음 거래일 MOC"),
            ("변동성", "20개 일별 로그수익률 표본표준편차(ddof=1), 비연환산"),
            ("D7", "D7 MOC 강제청산"),
            ("우선순위", "전체 MOC → D2 MOC → D7 MOC → LOC"),
            ("매수 제한", "전일 MA200 괴리율<−15% AND −20%≤20일 모멘텀<−5%, 또는 직전 4일 이상 연속 하락"),
            ("현금", "당일 매도대금은 당일 신규매수 재원에서 제외 · TB3 OFF"),
        ], columns=["항목", "규칙"]))
        st.markdown("**메리츠 연결 상태**")
        if broker_mode:
            st.success("메리츠 실계좌 모드 · 예수금·잔고·체결 상세·거래내역 연결")
            st.caption("마지막 정상 조회 " + broker_snapshot["fetched_at"])
        else:
            st.info("사이드바에서 메리츠 실계좌를 선택하면 계좌 기준으로 전환합니다.")
        st.caption("메리츠 조회는 기존 MERITZ_APP_KEY·MERITZ_APP_SECRET·MERITZ_MAC_ADDRESS 설정을 사용합니다. 앱키에 연결된 계좌를 조회하며 증권사 주문은 전송하지 않습니다. 계좌 원본은 GitHub에 저장하지 않습니다.")


if __name__ == "__main__":
    render()
