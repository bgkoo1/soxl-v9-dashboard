"""Full V12 operational dashboard in the original Streamlit layout.
User-confirmed actual holdings remain separate from simulated model slots.
"""
from pathlib import Path
from decimal import Decimal,ROUND_FLOOR
from datetime import datetime,timezone
from html import escape
import pandas as pd
import streamlit as st
from v12_trade_management import realized_summary,cashflow_summary
from v12_order_center import entry_filter,_num
from v12_portfolio_reconciliation import reconcile_portfolio
from v12_manual_holdings import LedgerError
from v12_trade_controls import render_trade_controls

def _h(v):
    return escape(str(v),quote=True)

def _money(v,prefix="USD ",decimals=2):
    if v is None:return "조회 필요"
    return f"{prefix}{float(v):,.{decimals}f}"

def _panel(title,subtitle=""):
    st.markdown('<div class="ref-panel-head"><span>'+_h(title)+'</span><small>'+
                _h(subtitle)+'</small></div>',unsafe_allow_html=True)

def _df(df):
    if df.empty:
        st.caption("표시할 자료가 없습니다.")
    else:
        st.dataframe(df,use_container_width=True,hide_index=True)

def _model_context(data,state,error):
    closes=pd.to_numeric(data["Close"],errors="coerce").dropna()
    close=Decimal(str(float(closes.iloc[-1])))
    last_date=pd.Timestamp(data["Date"].iloc[-1]).date()
    signal=entry_filter(closes.tolist())
    days=(datetime.now(timezone.utc).date()-last_date).days
    if days<0 or days>4:
        signal={"blocked":True,"reason":"최신 일봉 미확인"}
    sess=st.session_state
    broker=sess.get("v12_recent_broker_snapshot")
    quote=sess.get("v12_recent_soxl_quote")
    fx=sess.get("v12_recent_fx_quote")
    orders=sess.get("v12_recent_order_snapshot")
    price=_num(quote.get("price")) if isinstance(quote,dict) and quote.get("fresh") else None
    rate=_num(fx.get("price")) if isinstance(fx,dict) and fx.get("fresh") else None
    stamp=sess.get("v12_recent_broker_read_at")
    fresh=False
    try:
        fresh=bool(stamp and 0<=(datetime.now(timezone.utc)-datetime.fromisoformat(stamp)).total_seconds()<=300)
    except (ValueError,TypeError):
        pass
    rec=None
    if broker and not error:
        try:
            rec=reconcile_portfolio(state["positions"],broker,
                soxl_close=price,fx_krw_per_usd=rate,
                risk_blocked=signal["blocked"],broker_fresh=fresh,
                quote_fresh=price is not None,fx_fresh=rate is not None,
                orders_verified_empty=bool(orders is not None and not orders.get("orders")))
        except (LedgerError,TypeError,ValueError):
            pass
    return {"close":close,"last_date":last_date,"signal":signal,
            "broker":broker,"quote":price,"fx":rate,"rec":rec,"fresh":fresh}

def render_v12_full_console(data,result,state,sha,error):
    css=Path(__file__).with_name("v12_legacy_design.css").read_text(encoding="utf-8")
    st.markdown("<style>"+css+"</style>",unsafe_allow_html=True)
    ctx=_model_context(data,state,error)
    lots=state["positions"]
    sales=state["closed_trades"]
    rec=ctx["rec"]
    px=ctx["quote"] or ctx["close"]
    held=sum((Decimal(x["quantity"]) for x in lots),Decimal(0))
    st.markdown('<div class="redesign-header"><div class="redesign-logo">◈</div><div>'
                '<div class="redesign-headline">SOXL V12 Final</div>'
                '<div class="redesign-subline">기존 주문·슬롯 관리 화면 복원 · 6분할 · 메리츠 조회 전용</div>'
                '</div></div>',unsafe_allow_html=True)
    st.markdown('<div class="redesign-side-links" style="flex-direction:row;flex-wrap:wrap">'
                '<a href="#redesign-overview">오늘 주문</a>'
                '<a href="#redesign-slot-preview">보유 슬롯</a>'
                '<a href="#redesign-order-management">매수·매도 관리</a>'
                '<a href="#redesign-performance">성과 리포트</a></div>',unsafe_allow_html=True)
    if error:
        st.error("V12 보유 원장 접근 오류: "+error+" · 모든 매수·매도 기록 저장 중단")
    st.markdown("<div id='redesign-overview'></div>",unsafe_allow_html=True)
    with st.expander("메리츠 잔고 오류 시 수동 주문수량 계산 (실제 주문 아님)",expanded=not bool(ctx["broker"])):
        c1,c2,c3=st.columns(3)
        manual_cash=c1.number_input("USD 현금 직접 입력",min_value=0.0,value=0.0,step=100.0,key="v12_op_manual_cash")
        manual_fx=c2.number_input("USD/KRW 직접 입력",min_value=0.0,value=0.0,step=1.0,key="v12_op_manual_fx")
        manual_price=c3.number_input("SOXL 현재가 직접 입력",min_value=0.0,value=0.0,step=.01,key="v12_op_manual_price")
        manual_on=st.checkbox("수동 참고값으로 예상수량 표시",value=False,key="v12_op_manual_on")
        st.caption("별도로 입력한 현금·가격은 화면에서만 사용하고 GitHub와 증권사에 저장하지 않습니다.")
    broker_ready=bool(rec and rec["buy_preview_ready"] and not error)
    manual_ready=bool(manual_on and manual_cash>0 and manual_fx>0 and manual_price>0
                      and not ctx["signal"]["blocked"] and not error and len(lots)<=6)
    if broker_ready:
        cash=Decimal(rec["usd_cash"]);nav=Decimal(rec["nav_usd_estimate"])
        reference=ctx["quote"];rate=ctx["fx"];scope="메리츠 조회 기준 참고"
    elif manual_ready:
        cash=Decimal(str(manual_cash));reference=Decimal(str(manual_price))
        rate=Decimal(str(manual_fx));nav=cash+held*reference;scope="수동 입력 기준 참고"
    else:
        cash=_num(rec.get("usd_cash")) if rec else None
        nav=_num(rec.get("nav_usd_estimate")) if rec else None
        reference=px;rate=ctx["fx"];scope="잔고·원장 검증 필요"
    slots_free=max(0,6-len(lots))
    amount=min(nav/6,cash) if (broker_ready or manual_ready) and slots_free>0 and not ctx["signal"]["blocked"] else None
    qty=int((amount/reference).to_integral_value(rounding=ROUND_FLOOR)) if amount is not None and reference>0 else None
    crash=ctx["close"]*Decimal(".91")
    extra_amt=min(nav/6,max(Decimal(0),cash-amount)) if amount is not None and slots_free>=2 else None
    extra_qty=int((extra_amt/crash).to_integral_value(rounding=ROUND_FLOOR)) if extra_amt is not None else None
    left,right=st.columns(2,gap="small")
    with left:
        with st.container(border=True):
            _panel("TODAY'S ACTION · 오늘 주문","V12 6분할")
            action="BUY" if qty is not None and qty>0 else ("WAIT" if qty==0 else "CHECK DATA")
            klass={"BUY":"is-buy","WAIT":"is-wait","CHECK DATA":"is-check"}[action]
            st.markdown('<section class="action-essential '+klass+'"><div class="action-essential-state"><strong>'+
                        _h(action)+'</strong><span>'+_h(scope)+'</span></div>'+
                        '<div class="action-essential-numbers">'+
                        '<div class="action-essential-item"><small>오늘 예상 매수수량</small><strong>'+
                        _h(f"{qty:,}주" if qty is not None else "확인 필요")+'</strong></div>'+
                        '<div class="action-essential-item"><small>기본 1슬롯 매수금액</small><strong>'+
                        _h(_money(amount))+'</strong></div></div></section>',unsafe_allow_html=True)
            st.caption(f"급락 LOC 진입가 USD {crash:,.2f} · 추가 예상 {extra_qty if extra_qty is not None else '확인 필요'}주 · 남은 슬롯 {slots_free}개")
            st.caption("시장 제한: "+ctx["signal"]["reason"]+" · 확정 데이터 "+str(ctx["last_date"]))
    with right:
        with st.container(border=True):
            _panel("현재 계좌 · LIVE STATUS","메리츠 조회 / 사용자 등록")
            broker_cash=_num(ctx["broker"].get("USD 외화 예수금($)")) if ctx["broker"] else None
            labels=[("USD 현금",_money(broker_cash) if broker_cash is not None else "미조회"),
                    ("보유 평가액",_money(held*px)),
                    ("계좌 NAV",_money(nav) if nav is not None else "검증 필요")]
            st.markdown('<div class="ref-account-grid">'+''.join(
                '<div class="ref-account-item"><div class="ref-account-name">'+_h(k)+
                '</div><div class="ref-account-value">'+_h(v)+'</div></div>' for k,v in labels)+
                '</div>',unsafe_allow_html=True)
            st.caption(f"실제 등록 슬롯 {len(lots)}/6 · 등록 SOXL {held}주 · 종가 USD {ctx['close']:,.2f}")
            if ctx["broker"]:
                st.caption("메리츠 조회 상태: "+("최근 5분 이내" if ctx["fresh"] else "오래된 조회"))
    st.markdown("<div id='redesign-slot-preview'></div>",unsafe_allow_html=True)
    with st.container(border=True):
        _panel("보유 슬롯 미리보기 · 목표 매도","실제 매수가 · 총매수금액 · LOC 목표가")
        if lots:
            rows=[]
            for i,p in enumerate(sorted(lots,key=lambda x:x["entry_date"]),1):
                q=Decimal(p["quantity"]);entry=Decimal(p["entry_price"])
                target=entry*Decimal("1.032")
                rows.append({"슬롯":i,"매수일":p["entry_date"],"보유수량":str(q),
                             "매수가":_money(entry),"매수금액":_money(entry*q),
                             "LOC 목표가":_money(target),"목표 매도금액":_money(target*q),
                             "현재 평가액":_money(q*px),
                             "목표 매도액(원)":_money(target*q*rate,"₩",0) if rate else "환율 확인",
                             "상태":"실제 보유"})
            st.dataframe(pd.DataFrame(rows),use_container_width=True,hide_index=True)
        else:
            st.info("등록된 실제 보유 슬롯이 없습니다. 상세 주문에서 매수를 등록하거나 아래 가상 매도 취소 탭에서 복구하세요.")
        st.caption("실제 매수가 × 실제 보유수량으로 계산하며, 모델의 가상 매도 처리만으로 보유 슬롯을 삭제하지 않습니다.")
    st.markdown("<div id='redesign-order-management'></div>",unsafe_allow_html=True)
    col_left,col_right=st.columns([1.65,1],gap="small")
    with col_left:
        with st.container(border=True):
            _panel("상세 주문 · 슬롯 편집 · 실제 체결가 관리","V12 실제 거래 원장")
            all_tab,hold_tab,sold_tab=st.tabs(["전체","보유 슬롯","최근 종료"])
            with all_tab:
                records=[{"상태":"보유","슬롯 ID":p["slot_id"],"매수일":p["entry_date"],"매수가":float(p["entry_price"]),
                          "수량":float(p["quantity"]),"매도가":None,"매도일":None} for p in lots]
                records += [{"상태":"실제 매도","슬롯 ID":p["slot_id"],"매수일":p["entry_date"],"매수가":float(p["entry_price"]),
                          "수량":float(p["quantity"]),"매도가":float(p["exit_price"]),"매도일":p["exit_date"]} for p in sales]
                st.dataframe(pd.DataFrame(records),use_container_width=True,hide_index=True) if records else st.caption("실제 거래 기록이 없습니다.")
            with hold_tab:
                st.dataframe(pd.DataFrame(lots),use_container_width=True,hide_index=True) if lots else st.caption("실제 보유 슬롯이 없습니다.")
            with sold_tab:
                st.dataframe(pd.DataFrame(sales[-30:]),use_container_width=True,hide_index=True) if sales else st.caption("실제 매도 기록이 없습니다.")
            render_trade_controls(state,sha,error,ctx["close"])
    with col_right:
        with st.container(border=True):
            st.markdown("<div id='redesign-performance'></div>",unsafe_allow_html=True)
            _panel("전략 성과 요약","V12 연구 백테스트")
            stats=result["stats"];curve=result["equity"]
            total=float(curve["Equity"].iloc[-1]/curve["Equity"].iloc[0]-1) if len(curve)>1 else 0
            cells=[("누적 수익률",f"{total:+.1%}"),("CAGR",f"{float(stats['cagr']):.1%}"),
                   ("MDD",f"{float(stats['mdd']):.1%}"),("Calmar",f"{float(stats['calmar']):.2f}")]
            st.markdown('<div class="ref-perf-grid">'+''.join('<div><small>'+_h(k)+
                        '</small><strong>'+_h(v)+'</strong></div>' for k,v in cells)+'</div>',unsafe_allow_html=True)
            st.caption("위 CAGR·MDD는 이상적 가상 체결을 가정한 백테스트 결과입니다.")
            st.metric("실제 등록 매도 실현손익 (수수료 전)",_money(realized_summary(state)))
            totals=cashflow_summary(state)
            st.caption(f"입출금 기록 순액: USD {totals['USD']:,.2f} / KRW {totals['KRW']:,.0f} · 메리츠 현금에 이중 가산하지 않음")
        with st.container(border=True):
            _panel("V12 운용 규칙","기본 전략 고정")
            st.write("6분할 · LOC +3.2% · D2 -10% → D3 종가 청산 · D7")
            st.write("D2 조건 + 20일 변동성 ≥10% → 다음 거래일 전체 청산")
            st.write("Risk B 및 전일 4거래일 연속 하락 신규매수 유예")
            st.caption("V10/V11 기록과 별도 관리 · 메리츠 READ ONLY · 주문 전송 OFF")
