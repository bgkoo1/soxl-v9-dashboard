"""SOXL V12 research workspace — broker READ ONLY. No persisted state is mutated."""
from pathlib import Path
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import streamlit as st
from SOXL_V12_backtest import run_v12_backtest
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
    st.markdown('<div class="v12-hero"><h2>SOXL V12 Final · 6분할</h2><p>연구 백테스트 · 주문 미리보기 · 실계좌 조회 전용 | 실거래 주문 비활성화</p></div>', unsafe_allow_html=True)
    st.caption("기존 V10/V11 원장 및 암호화된 거래기록을 수정하지 않습니다. V12 모델 슬롯은 증권사 실제 보유 슬롯과 별개입니다.")
    with st.sidebar:
        st.markdown("### V12 Final")
        st.success("기본 전략: 6분할 고정")
        st.caption("READ ONLY · 자동주문 OFF")
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
    t1,t2,t3=st.tabs(["NAV·낙폭", "보유 슬롯·다음 주문", "거래·방어 신호"])
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
                st.warning("실제 보유잔고는 V12 연구용 슬롯에 자동 편입하지 않습니다. 주문 전 별도 재조정이 필요합니다.")
            except Exception:
                st.error("메리츠 계좌 조회가 실패했습니다. 자격정보 및 API 상태를 확인하세요. 주문은 전송되지 않았습니다.")
    st.caption("V12 Research READ ONLY · 원본 V10/V11 및 live_portfolio.enc 보존 · 자동주문 불가")
