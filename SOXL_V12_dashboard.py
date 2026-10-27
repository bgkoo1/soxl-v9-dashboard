"""SOXL V12 research workspace — broker READ ONLY. User-approved corrections are saved only to separate encrypted V12 ledger."""
from pathlib import Path
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import streamlit as st
from SOXL_V12_backtest import run_v12_backtest
from v12_manual_holdings import load_ledger, save_ledger, reopen_slot, close_manual_slot, reconcile_total, LedgerError
from decimal import Decimal
from v11_meritz_readonly_adapter import get_readonly_connection_status

NAME = "SOXL V12 Final · 6분할"
RULES = {
    "분할": "6분할 / 최대 6슬롯",
    "익절": "매수가 대비 LOC +3.2%",
    "손절": "D2 종가 -10% 이하 → D3 MOC",
    "조건부 전체 청산": "D2 손절 신호 + 20일 로그수익률 변동성 ≥ 10% → D3 MOC",
    "최대 보유": "D7 MOC",
    "급락 추가매수": "전일 종가 × 0.91 LOC (추가 1슬롯)",
    "매수 제한": "Risk B / 직전 4거래일 연속 하락",
    "TB3": "OFF",
}

@st.cache_data(ttl=180, show_spinner=False)
def _read_prices():
    from backtest_engine import load_data
    p = Path(__file__).resolve().parent / "SOXL_adjusted.csv"
    df = load_data(p)
    if df.empty:
        raise ValueError("SOXL 가격 데이터가 비어 있습니다")
    return df

@st.cache_data(ttl=180, show_spinner=False)
def _run_research(data, capital):
    # Historical whole-series model. Does not inherit legacy V10/V11 slots.
    return run_v12_backtest(raw_df=data, initial_capital=capital)

def render_v12_dashboard():
    st.markdown("""
    <style>
    .v12-hero{padding:20px 24px;border:1px solid #2a3950;border-radius:18px;background:linear-gradient(110deg,#102033,#203b58);color:white;margin-bottom:12px}
    .v12-hero h2{margin:0 0 6px 0;color:white;font-size:1.8rem}
    .v12-hero p{margin:0;color:#e2e8f0;font-size:.95rem}
    div[data-testid="stMetric"]{border:1px solid #e2e8f0;border-radius:12px;padding:13px}
    </style>
    """, unsafe_allow_html=True)
    st.markdown('<div class="v12-hero"><h2>SOXL V12 Final · 6분할</h2><p>연구 백테스트 · 실제 보유 슬롯 복구 · 메리츠 조회 전용 | 실거래 주문 비활성화</p></div>', unsafe_allow_html=True)
    st.caption("과거 V10/V11 원장 및 live_portfolio.enc는 보존합니다. 가상 매도 취소는 별도의 암호화된 V12 실제 보유 원장에만 저장됩니다.")
    with st.sidebar:
        st.markdown("### V12 Final")
        st.success("기본 전략: 6분할 고정")
        st.caption("메리츠 READ ONLY · 자동주문 OFF · 사용자 확인 시 V12 보유 원장만 저장")
        capital = st.number_input("연구 초기자산 (원)", min_value=1000000,
                                  max_value=10000000000, value=100000000, step=10000000)
        show_broker = st.checkbox("메리츠 조회 전용 패널", value=True)
        st.divider()
        st.markdown("**전략 명세**")
        for k,v in RULES.items():
            st.caption(k+" · "+v)
    try:
        data=_read_prices()
        result=_run_research(data,float(capital))
    except Exception as exc:
        st.error("V12 데이터 또는 엔진을 불러오지 못했습니다. 데이터 형식과 배포 로그를 확인하세요.")
        st.stop()
    eq=result["equity"].copy()
    stats=result["stats"]
    last_date=pd.Timestamp(eq["Date"].iloc[-1])
    if last_date.date() < pd.Timestamp.now(tz="Asia/Seoul").date() - pd.Timedelta(days=5):
        st.warning(f"가격 데이터 최신일: {last_date.date()} — 장기 지연 가능성을 확인하세요.")
    c1,c2,c3,c4=st.columns(4)
    def pct(value):
        return f"{100*float(value):,.2f}%" if value is not None and np.isfinite(float(value)) else "—"
    c1.metric("연구 CAGR",pct(stats.get("cagr")))
    c2.metric("연구 MDD",pct(stats.get("mdd")))
    c3.metric("연구 Calmar",f"{float(stats.get('calmar',0)):,.3f}")
    c4.metric("모델 현재 슬롯",f"{len(result.get('open_positions', []))} / 6")
    st.caption(f"연구 기간 {pd.Timestamp(eq['Date'].iloc[0]).date()} ~ {last_date.date()} · 조정종가 기준 · 이상적 LOC/MOC 전량 체결 가정 · 비용 별도")
    # Broker remains GET-only; the corrected manual ledger is separately encrypted.
    ledger_error=None
    ledger_sha=None
    manual_positions=[]
    try:
        manual_positions,ledger_sha=load_ledger(st.secrets)
    except LedgerError as exc:
        ledger_error=str(exc)
    t1,t2,t3,t4=st.tabs(["NAV·낙폭", "보유 슬롯·다음 주문", "가상 매도 취소·실제 보유", "거래·방어 신호"])
    with t1:
        dates=eq["Date"]
        nav=eq["Equity"].astype(float)
        dd=(nav/nav.cummax()-1)*100
        fig=go.Figure()
        fig.add_trace(go.Scatter(x=dates,y=nav,mode="lines",name="연구 NAV",line=dict(width=2)))
        fig.update_layout(title="V12 연구 NAV (원화 가상자본)",height=320,margin=dict(l=10,r=10,t=50,b=10))
        st.plotly_chart(fig,use_container_width=True)
        fig=go.Figure()
        fig.add_trace(go.Scatter(x=dates,y=dd,fill="tozeroy",mode="lines",name="Drawdown"))
        fig.update_layout(title="NAV Drawdown (%)",height=250,margin=dict(l=10,r=10,t=50,b=10))
        st.plotly_chart(fig,use_container_width=True)
    with t2:
        positions=result.get("open_positions",[])
        if positions:
            table=pd.DataFrame([{
                "Slot ID":p["position_id"],
                "진입일":str(pd.Timestamp(p["entry_date"]).date()),
                "진입가($)":round(float(p["entry_price"]),4),
                "LOC 목표가($)":round(float(p["entry_price"])*1.032,4),
                "보유수량(연구용)":round(float(p["shares"]),4),
                "D2 손절 대기":bool(p.get("d2_flag",False)),
                "전체청산 MOC 대기":bool(p.get("v12_full_pending",False)),
            } for p in positions])
            st.dataframe(table,use_container_width=True,hide_index=True)
        else:
            st.info("연구 엔진 기준 미청산 모델 슬롯이 없습니다.")
        recent=result["daily"].iloc[-1]
        latest_close=float(result["signal_data"]["Close"].iloc[-1])
        latest_sig=result["signal_data"].iloc[-1]
        closes=data["Close"].astype(float).to_numpy()
        last4=0
        for j in range(len(closes)-1,0,-1):
            if closes[j] < closes[j-1]:
                last4 += 1
            else:
                break
        next_ma200=float(np.mean(closes[-200:])) if len(closes)>=200 else np.nan
        next_mom20=float(closes[-1]/closes[-21]-1) if len(closes)>=21 else np.nan
        next_risk=(np.isfinite(next_ma200) and np.isfinite(next_mom20)
                   and closes[-1]/next_ma200-1 < -.15
                   and -.20 <= next_mom20 < -.05)
        blocked=bool(next_risk or last4>=4)
        pending=any(p.get("v12_full_pending") for p in positions)
        st.markdown("#### 다음 거래일 주문 참고값")
        st.caption("모델의 전일 확정 NAV를 기준으로 생성한 **조회용 참고값**입니다. 실제 주문금액·증권사 체결가가 아닙니다.")
        one=float(eq["Equity"].iloc[-1])/6
        st.write(f"모델 NAV/6: {one:,.0f}원 · 급락 LOC 지정가: **${latest_close*.91:,.4f}**")
        st.write(f"매수 차단 (Risk B/4일 하락): {'예' if blocked else '아니오'} · 전체 청산 대기: {'예' if pending else '아니오'}")
        if pending:
            st.warning("D2 전체 청산 신호: 예약 대상 슬롯을 다음 거래일 MOC로 청산하는 연구 규칙입니다.")
        st.warning("기본 MOC 매수는 API 주문코드 존재와 실제 허용 여부가 다를 수 있어 실주문을 생성하지 않습니다.")
    with t3:
        st.markdown("### 가상 매도 취소 · 실제 보유 슬롯 복구")
        st.info("전략이 자동으로 종료한 슬롯이라도 증권사에서 실제 매도 체결되지 않았다면 보유 원장으로 복구할 수 있어요. 연구용 매도 기록·CAGR은 바꾸지 않습니다.")
        if ledger_error:
            st.error(ledger_error+" 기존 저장 원장을 보존하기 위해 복구·삭제를 차단했습니다.")
        else:
            total_qty=sum((Decimal(s["quantity"]) for s in manual_positions),Decimal("0"))
            c1,c2=st.columns(2)
            c1.metric("복구해 관리 중인 실제 보유 슬롯",len(manual_positions))
            c2.metric("복구 원장에 기록된 SOXL 수량",f"{total_qty:,}주")
            st.caption("복구 원장은 사용자 입력에 따른 미검증 보유 정보입니다. 실제 보유 여부는 메리츠 잔고·체결내역과 반드시 대조하세요.")
            if manual_positions:
                slots_table=pd.DataFrame([{
                    "슬롯 ID":x["slot_id"],"매수일":x["entry_date"],
                    "실제 매수가($)":float(x["entry_price"]),
                    "보유수량(주)":float(x["quantity"]),
                    "현재 LOC 지정가($)":round(float(x["entry_price"])*1.032,4),
                    "기존 가상 청산":x["model_exit"],
                    "상태":"실제 보유(사용자 등록)",
                } for x in manual_positions])
                st.dataframe(slots_table,use_container_width=True,hide_index=True)
            trades=result["trades"]
            st.markdown("#### 1. 매도하지 않은 슬롯 복구")
            closed=[]
            if not trades.empty and "Position_ID" in trades.columns:
                recent_trades=trades.dropna(subset=["Position_ID"]).iloc[::-1]
                for _,trade in recent_trades.iterrows():
                    key=str(trade["Position_ID"])
                    if key not in {x["slot_id"] for x in manual_positions}:
                        closed.append((key,trade))
                    if len(closed)>=150:
                        break
            source_mode=st.radio("복구 대상 찾기",["V12 가상 매도 기록에서 선택","기존 V10/V11·수동 매수 슬롯 직접 등록"],horizontal=True,key="v12_reopen_source")
            candidate=None
            if source_mode.startswith("V12"):
                if closed:
                    choices={f"{pd.Timestamp(t['Entry_Date']).date()} 매수 · {pd.Timestamp(t['Exit_Date']).date()} 가상 매도 · {str(t['Exit_Type'])} · {key}":(key,t) for key,t in closed}
                    name=st.selectbox("가상 매도된 슬롯",list(choices),key="v12_reopen_trade")
                    key,candidate=choices[name]
                else:
                    st.info("최근 가상 매도 중 복구 가능한 슬롯이 없습니다. 직접 등록을 이용하세요.")
            with st.form("v12_restore_slot_form",clear_on_submit=False):
                default_key=str(candidate["Position_ID"]) if candidate is not None else ""
                custom_key=st.text_input("슬롯 식별자",value=default_key,help="기존 V10/V11 실제 보유 슬롯은 고유한 ID를 입력하세요. 예: legacy-2026-10-20-01")
                day=pd.Timestamp(candidate["Entry_Date"]).date() if candidate is not None else last_date.date()
                buy_date=st.date_input("실제 매수일",value=day,max_value=pd.Timestamp.now(tz="Asia/Seoul").date())
                price=float(candidate["Entry_Price"]) if candidate is not None else float(data["Close"].iloc[-1])
                buy_price=st.number_input("실제 체결 매수가($)",min_value=0.0001,value=max(0.0001,price),format="%.4f")
                quantity=st.number_input("실제 미매도 보유수량(주)",min_value=1,step=1,value=1)
                notes=st.text_input("메모(선택)",max_chars=160)
                checked=st.checkbox("해당 슬롯의 실제 매도 체결이 없으며, 지금도 계좌에 보유 중임을 확인했습니다.")
                submitted=st.form_submit_button("가상 매도 취소 · 실제 보유 슬롯 복구",type="primary")
            if submitted:
                if not checked:
                    st.error("증권사에서 매도되지 않았다는 확인을 선택해야 합니다.")
                elif not custom_key.strip():
                    st.error("슬롯 ID를 입력하세요.")
                else:
                    try:
                        new_row={"slot_id":custom_key,"entry_date":buy_date.isoformat(),
                                 "entry_price":str(buy_price),"quantity":str(quantity),
                                 "source":"model_closed" if candidate is not None else "manual",
                                 "model_exit":str(candidate["Exit_Type"]) if candidate is not None else "기존 기록/직접 등록",
                                 "note":notes}
                        updated=reopen_slot(manual_positions,new_row)
                        save_ledger(st.secrets,updated,ledger_sha)
                        st.success("복구 기록을 별도 암호화 원장에 저장했습니다. 증권사 주문은 전송되지 않았습니다.")
                        st.rerun()
                    except LedgerError as exc:
                        st.error(str(exc))
            if manual_positions:
                st.markdown("#### 2. 복구 원장 정정")
                st.caption("실제 매도가 완료되었거나 잘못 복구한 슬롯만 원장에서 제외하세요. 이 동작은 증권사 매도가 아닙니다.")
                labels={f"{x['entry_date']} · {x['slot_id']} · {x['quantity']}주":x["slot_id"] for x in manual_positions}
                with st.form("v12_remove_manual_form"):
                    selected=st.selectbox("원장에서 제외할 슬롯",list(labels))
                    confirmation=st.checkbox("실제 매도 완료 또는 잘못된 복구 기록임을 확인합니다.")
                    remove=st.form_submit_button("복구 기록 원장 제외")
                if remove:
                    if not confirmation:
                        st.error("제외 사유를 확인해야 합니다.")
                    else:
                        try:
                            next_rows=close_manual_slot(manual_positions,labels[selected])
                            save_ledger(st.secrets,next_rows,ledger_sha)
                            st.success("V12 복구 원장에서만 제외했습니다. 연구 기록과 증권사 주문은 변경되지 않았습니다.")
                            st.rerun()
                        except LedgerError as exc:
                            st.error(str(exc))
            st.caption("V12 연구 모델이 다시 가상 매도를 기록하더라도 이미 복구한 슬롯은 별도 원장에서 자동 삭제되지 않습니다.")
    with t4:
        d=result["daily"].copy()
        showcols=[c for c in ["Date","Risk_State","Pause_State","Crash_State",
                     "V12_Volatility_20D","V12_Full_Liquidation_Trigger",
                     "V12_Full_Liquidation_Pending","Buy_Slots","Equity"] if c in d.columns]
        st.dataframe(d[showcols].tail(30).iloc[::-1],use_container_width=True,hide_index=True)
        st.metric("전체 청산 신호 발생일",int(stats.get("v12_full_liquidation_signals",0)))
        st.caption("D2 전체청산은 신호 확정일 다음 거래일에 실행하는 모델입니다.")
        trades=result["trades"]
        if not trades.empty:
            st.dataframe(trades.tail(40).iloc[::-1],use_container_width=True,hide_index=True)
    if show_broker:
        st.divider()
        st.subheader("메리츠 실계좌 · 조회 전용")
        status=get_readonly_connection_status(st.secrets)
        st.caption(status["message"])
        if st.button("메리츠 잔고 새로 조회 (READ ONLY)",key="v12_broker_refresh"):
            from v11_meritz_account_preview import preview_account
            try:
                snapshot=preview_account(st.secrets)
                b1,b2,b3=st.columns(3)
                b1.metric("SOXL 실제 보유수량",snapshot["SOXL 보유 수량(주)"]+"주")
                b2.metric("USD 예수금",snapshot["USD 외화 예수금($)"]+" USD")
                b3.metric("원화 주문가능현금",snapshot["원화 주문 가능 현금(원)"]+"원")
                st.caption(snapshot["status"])
                if not ledger_error:
                    comparison=reconcile_total(manual_positions,snapshot["SOXL 보유 수량(주)"])
                    if comparison["matched"]:
                        st.success("복구 원장의 SOXL 보유 수량과 증권사 잔고 수량이 일치합니다. 개별 매수 이력은 별도 확인이 필요합니다.")
                    else:
                        st.warning(f"복구 원장 {comparison['manual']}주 / 메리츠 {comparison['broker']}주 / 차이 {comparison['difference']}주. 기존 V10/V11 보유 등 미등록 슬롯이 있을 수 있습니다.")
                st.warning("실제 보유잔고는 V12 연구용 슬롯에 자동 편입하지 않습니다. 주문 전 별도 재조정이 필요합니다.")
            except Exception:
                st.error("메리츠 계좌 조회가 실패했습니다. 자격정보 및 API 상태를 확인하세요. 주문은 전송되지 않았습니다.")
    st.caption("V12 Broker READ ONLY · 기존 V10/V11, live_portfolio.enc 보존 · V12 복구 원장만 사용자 확인 후 GitHub 암호화 저장 · 자동주문 불가")
