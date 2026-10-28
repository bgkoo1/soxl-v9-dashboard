"""Legacy-layout V12 operator workspace — actual trades are explicitly user-confirmed.

No broker order calls. Only encrypted journal writes on deliberate buttons.
"""
from datetime import datetime,timezone
from decimal import Decimal,ROUND_FLOOR
from html import escape
import pandas as pd
import streamlit as st
from v12_manual_holdings import (load_portfolio_state,save_portfolio_state,
    LedgerError,reopen_slot,validate_ledger)
from v12_trade_management import (update_slot,record_sale,cancel_sale,add_cashflow,
    remove_cashflow,cashflow_summary,realized_summary,REASONS)
from v12_order_center import entry_filter
from v12_portfolio_reconciliation import reconcile_portfolio

BUILD = "V12-FULL-ORDER-20261028"

def _money(v,prefix="$",digits=2):
    if v is None:return "확인 필요"
    try:return prefix+f"{float(v):,.{digits}f}"
    except (TypeError,ValueError):return "확인 필요"

def _apply(state,sha,next_state):
    """Never keep failed edits in memory; encrypted GitHub write is the commit point."""
    save_portfolio_state(st.secrets,next_state,sha)
    st.rerun()

def _stylesheet():
    st.markdown("""<style>
    .block-container{max-width:1580px;padding-top:1.4rem!important}
    .v12-headline{font-size:1.65rem;font-weight:850;color:#172a49;letter-spacing:-.035em;margin-bottom:3px}
    .v12-subline{font-size:.82rem;color:#71839d;margin-bottom:1rem}
    .v12-panel-title{font-size:1rem;font-weight:800;color:#1b345a;margin:2px 0 16px}
    .v12-panel-title small{font-size:.74rem;font-weight:500;color:#8b99ac;margin-left:9px}
    .v12-stat-grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px}
    .v12-stat{background:#f8fbff;border:1px solid #e5eaf3;border-radius:9px;padding:15px 12px;min-width:0}
    .v12-stat-label{color:#73849e;font-size:.74rem;font-weight:700}
    .v12-stat-value{color:#1b3556;font-size:clamp(.86rem,1.25vw,1.35rem);font-weight:850;margin-top:8px}
    .v12-action{border:1px solid #dfe8f3;border-radius:12px;padding:17px 15px;margin:5px 0;background:#fff}
    .v12-action-head{color:#294569;font-weight:800;font-size:.82rem}
    .v12-action-state{color:#0b966d;font-size:1.3rem;font-weight:850;margin:10px 0}
    .v12-action-state.warn{color:#bd6d23}
    .v12-action-values{display:flex;gap:22px;margin:14px 0}
    .v12-action-values small{color:#7b8da5;display:block;font-size:.75rem}
    .v12-action-values strong{font-size:1.32rem;color:#1a385e}
    .v12-muted{color:#798a9e;font-size:.77rem}
    [data-testid="stVerticalBlockBorderWrapper"]{border-radius:12px!important}
    @media(max-width:900px){.v12-stat-grid{grid-template-columns:1fr 1fr}}
    </style>""",unsafe_allow_html=True)

def _panel(text,sub=""):
    st.markdown(f'<div class="v12-panel-title">{escape(text)}<small>{escape(sub)}</small></div>',unsafe_allow_html=True)

def _us_trading_days(data,entry):
    dates=pd.to_datetime(data["Date"]).dt.date
    return int((dates>pd.Timestamp(entry).date()).sum())

def _snapshot_values(data,state):
    """Broker snapshot is optional; manual values are separately labelled as estimates."""
    close=float(data["Close"].iloc[-1])
    prices=pd.to_numeric(data["Close"],errors="coerce").dropna()
    filt=entry_filter(prices.tolist())
    updated=(datetime.now(timezone.utc).date()-pd.Timestamp(data["Date"].iloc[-1]).date()).days
    stale=updated<0 or updated>4
    broker=st.session_state.get("v12_recent_broker_snapshot")
    quote=st.session_state.get("v12_recent_soxl_quote")
    fx=st.session_state.get("v12_recent_fx_quote")
    broker_timestamp=st.session_state.get("v12_recent_broker_read_at")
    fresh_broker=False
    try:
        fresh_broker=0<=(datetime.now(timezone.utc)-datetime.fromisoformat(broker_timestamp)).total_seconds()<=300
    except (TypeError,ValueError):
        pass
    orders=st.session_state.get("v12_recent_order_snapshot")
    can_use_broker=bool(broker and fresh_broker)
    position_qty=sum(Decimal(p["quantity"]) for p in state["positions"])
    if can_use_broker:
        bqty=Decimal(str(broker["SOXL 보유 수량(주)"]))
        cash=Decimal(str(broker["USD 외화 예수금($)"]))
        krw=Decimal(str(broker["원화 주문 가능 현금(원)"]))
        mark=Decimal(str(quote["price"])) if quote and quote.get("fresh") else None
        rate=Decimal(str(fx["price"])) if fx and fx.get("fresh") else None
        origin="메리츠 잔고 조회"
        consistent=bqty==position_qty
        broker_order_check=bool(orders is not None and not orders.get("orders"))
    else:
        bqty=position_qty
        cash=Decimal(str(st.session_state.get("v12_manual_usd_cash","0")))
        krw=Decimal(str(st.session_state.get("v12_manual_krw_cash","0")))
        mark=Decimal(str(st.session_state.get("v12_manual_price",close)))
        rate=Decimal(str(st.session_state.get("v12_manual_fx","0")))
        if rate<=0:rate=None
        origin="수동 입력·미검증 참고값"
        consistent=bool(state["positions"] or bqty==0)
        broker_order_check=False
    nav=(cash+bqty*mark+krw/rate) if mark is not None and rate is not None else None
    target=nav/6 if nav is not None else None
    order_budget=min(target,cash) if target is not None else None
    blocked=bool(filt["blocked"] or stale)
    amount=order_budget if order_budget is not None and not blocked and len(state["positions"])<6 else None
    qty=int((amount/mark).to_integral_value(rounding=ROUND_FLOOR)) if amount is not None and mark and mark>0 else None
    extra_budget=min(target,max(Decimal(0),cash-(amount or Decimal(0)))) if target is not None and len(state["positions"])<=4 else None
    crash=Decimal(str(close))*Decimal(".91")
    extra_qty=int((extra_budget/crash).to_integral_value(rounding=ROUND_FLOOR)) if extra_budget is not None and crash>0 else None
    # Do not advertise a broker-executable buy without confirmed complete data.
    ready=bool(can_use_broker and consistent and broker_order_check
               and mark and rate and not blocked and len(state["positions"])<6
               and quote and quote.get("fresh") and fx and fx.get("fresh"))
    return dict(close=close,mark=mark,fx=rate,usd=cash,krw=krw,bqty=bqty,qty_book=position_qty,
        nav=nav,target=target,amount=amount,qty=qty,extra_budget=extra_budget,
        extra_qty=extra_qty,crash=crash,source=origin,ready=ready,stale=stale,
        reason=filt["reason"],consistent=consistent,broker_active=can_use_broker,blocked=blocked)

def _rows(state,values,data):
    rows=[]
    for n,p in enumerate(sorted(state["positions"],key=lambda x:(x["entry_date"],x["slot_id"])),1):
        q=Decimal(p["quantity"]);price=Decimal(p["entry_price"]);target=price*Decimal("1.032")
        age=_us_trading_days(data,p["entry_date"])
        mark=values["mark"]
        pnl=q*(mark-price) if mark is not None else None
        status="D7 확인 필요" if age>=7 else ("D2 손절 조건 확인" if age==2 and mark is not None and mark/price-1<=Decimal("-.10") else "보유")
        rows.append({"슬롯":n,"ID":p["slot_id"],"매수일":p["entry_date"],"보유수량":float(q),
          "매수가($)":float(price),"매수금액($)":float(q*price),
          "LOC 목표매도가($)":float(target),"목표매도금액($)":float(q*target),
          "평가액($)":float(q*mark) if mark is not None else None,
          "평가손익($)":float(pnl) if pnl is not None else None,
          "경과 거래일":age,"상태":status})
    return rows

def _edit_forms(state,sha,data):
    positions=state["positions"]
    if not positions:
        st.info("실제 보유 슬롯이 없습니다. 아래 '매수 등록·가상 매도 복구'에서 등록하세요.")
        return
    labels={p["slot_id"]:f"{p['entry_date']} · {p['quantity']}주 · ${p['entry_price']} · {p['slot_id']}" for p in positions}
    left,right=st.columns(2,gap="medium")
    with left:
        _panel("실제 매수가·수량 수정","저장 시 암호화 원장에 반영")
        select=st.selectbox("수정할 슬롯",list(labels),format_func=lambda x:labels[x],key="v12_edit_pick")
        p=next(x for x in positions if x["slot_id"]==select)
        with st.form("v12_full_edit"):
            date_value=st.date_input("실제 매수일",pd.Timestamp(p["entry_date"]).date(),key=f"v12_edit_date_{select}")
            price=st.number_input("실제 체결 매수가($)",min_value=.0001,value=float(p["entry_price"]),format="%.4f",key=f"v12_edit_price_{select}")
            qty=st.number_input("실제 보유수량(주)",min_value=.00000001,value=float(p["quantity"]),format="%.8f",key=f"v12_edit_qty_{select}")
            note=st.text_input("메모",value=p.get("note",""),max_chars=160,key=f"v12_edit_note_{select}")
            confirm=st.checkbox("실제 체결 자료와 대조했습니다.",key=f"v12_edit_confirm_{select}")
            submit=st.form_submit_button("실제 수량·체결가 저장",type="primary")
        if submit:
            if not confirm:st.error("수정 내용 확인이 필요합니다.")
            else:
                try:_apply(state,sha,update_slot(state,select,entry_date=date_value,entry_price=price,quantity=qty,note=note))
                except LedgerError as exc:st.error(str(exc))
    with right:
        _panel("실제 매도 등록","부분매도 가능 · 실거래만 등록")
        selected=st.selectbox("매도한 슬롯",list(labels),format_func=lambda x:labels[x],key="v12_sell_pick")
        p=next(x for x in positions if x["slot_id"]==selected)
        with st.form("v12_full_sell"):
            exit_date=st.date_input("실제 매도일",value=datetime.now(timezone.utc).date(),key="v12_exit_date")
            exit_price=st.number_input("실제 체결 매도가($)",min_value=.0001,value=float(p["entry_price"])*1.032,format="%.4f")
            sold=st.number_input("실제 매도수량(주)",min_value=.00000001,max_value=float(p["quantity"]),value=float(p["quantity"]),format="%.8f")
            reason=st.selectbox("매도유형",sorted(REASONS),index=sorted(REASONS).index("LOC +3.2%"))
            memo=st.text_input("체결 메모",max_chars=160)
            checked=st.checkbox("증권사에서 실제 매도 체결이 완료됐음을 확인했습니다.")
            submit=st.form_submit_button("실제 매도 체결 저장",type="primary")
        if submit:
            if not checked:st.error("실제 매도 체결 확인이 필요합니다.")
            else:
                try:_apply(state,sha,record_sale(state,selected,quantity=sold,exit_date=exit_date,exit_price=exit_price,exit_strategy=reason,note=memo))
                except LedgerError as exc:st.error(str(exc))

def _restore_form(state,sha,data,research):
    _panel("신규 매수 등록 · 가상 자동매도 취소")
    recent=research.get("trades",pd.DataFrame())
    choice=st.radio("등록 유형",["실제 신규매수·기존 미등록 슬롯","가상 자동매도 취소 후 실제 보유 복구"],horizontal=True,key="v12_full_entry_mode")
    candidate=None
    if choice.startswith("가상") and isinstance(recent,pd.DataFrame) and not recent.empty and "Position_ID" in recent:
        candidate_rows=recent.dropna(subset=["Position_ID"]).tail(300).iloc[::-1]
        options={}
        booked={p["slot_id"] for p in state["positions"]}
        for _,r in candidate_rows.iterrows():
            sid=str(r["Position_ID"])
            if sid not in booked:
                options[f"{pd.Timestamp(r['Entry_Date']).date()} · {pd.Timestamp(r['Exit_Date']).date()} · {sid}"]=r
        if options:
            label=st.selectbox("연구용 가상 매도",list(options),key="v12_full_sim_pick")
            candidate=options[label]
        else:st.info("미등록된 가상 매도 항목이 없습니다. 직접 등록을 이용하세요.")
    with st.form("v12_full_new"):
        default_id=str(candidate["Position_ID"]) if candidate is not None else ""
        slot_id=st.text_input("슬롯 고유 ID",default_id,placeholder="예: actual-2026-10-28-1")
        default_date=pd.Timestamp(candidate["Entry_Date"]).date() if candidate is not None else datetime.now(timezone.utc).date()
        date_value=st.date_input("실제 매수일",default_date)
        price=st.number_input("실제 매수가($)",min_value=.0001,value=float(candidate["Entry_Price"]) if candidate is not None else float(data["Close"].iloc[-1]),format="%.4f")
        qty=st.number_input("매수 후 실제 보유수량(주)",min_value=.00000001,value=1.0,format="%.8f")
        memo=st.text_input("메모",max_chars=160)
        check=st.checkbox("실제로 매수했고 현재 해당 수량을 보유 중임을 확인합니다.")
        submit=st.form_submit_button("실제 보유 슬롯 등록",type="primary")
    if submit:
        if not check:st.error("실제 보유 확인을 선택하세요.")
        else:
            try:
                candidate_slot={"slot_id":slot_id,"entry_date":date_value.isoformat(),
                    "entry_price":str(price),"quantity":str(qty),
                    "source":"model_closed" if candidate is not None else "manual",
                    "model_exit":str(candidate["Exit_Type"]) if candidate is not None else "직접 등록",
                    "note":memo}
                next_state={**state,"positions":reopen_slot(state["positions"],candidate_slot)}
                _apply(state,sha,next_state)
            except LedgerError as exc:st.error(str(exc))

def _history(state,sha):
    _panel("실제 매도 내역 · 매도 취소","가상 매도가 아닌 사용자 확정 체결 기록")
    trades=state["closed_trades"]
    if trades:
        rows=[]
        for t in sorted(trades,key=lambda x:x["exit_date"],reverse=True):
            profit=(Decimal(t["exit_price"])-Decimal(t["entry_price"]))*Decimal(t["quantity"])
            rows.append({"거래 ID":t["id"],"슬롯":t["slot_id"],"매수일":t["entry_date"],
              "매도일":t["exit_date"],"매도수량":float(t["quantity"]),
              "실제 매도가($)":float(t["exit_price"]),"실현손익($)":float(profit),
              "유형":t["exit_strategy"]})
        st.dataframe(pd.DataFrame(rows),hide_index=True,use_container_width=True)
        labels={t["id"]:f"{t['exit_date']} · {t['slot_id']} · {t['quantity']}주 · ${t['exit_price']}" for t in trades}
        with st.form("v12_undo_actual_sell"):
            chosen=st.selectbox("실제 매도 기록 취소",list(labels),format_func=lambda x:labels[x])
            verified=st.checkbox("실제 매도 미체결 또는 입력 오류를 확인했습니다. 해당 수량을 보유 슬롯에 다시 더합니다.")
            submit=st.form_submit_button("매도 처리 취소 · 보유 슬롯 복구")
        if submit:
            if not verified:st.error("취소 사유 확인이 필요합니다.")
            else:
                try:_apply(state,sha,cancel_sale(state,chosen))
                except LedgerError as exc:st.error(str(exc))
    else:st.info("실제 매도 체결로 등록된 거래가 없습니다.")
    _panel("입출금 관리","메리츠 현재 현금에 중복 가산하지 않음")
    flows=state["cashflows"]
    if flows:
        st.dataframe(pd.DataFrame(flows)[["date","type","currency","amount","note","id"]],
                     hide_index=True,use_container_width=True)
    totals=cashflow_summary(state)
    st.caption(f"기록된 순입출금 USD {totals['USD']:+,.2f} / KRW {totals['KRW']:+,.0f} · 실시간 현금잔고에는 이미 반영된 기록일 수 있어 재가산하지 않습니다.")
    l,r=st.columns([3,2])
    with l:
        with st.form("v12_add_flow"):
            date_value=st.date_input("입출금 일자",datetime.now(timezone.utc).date(),key="v12_flow_date")
            c1,c2,c3=st.columns(3)
            typ=c1.selectbox("구분",["입금","출금"])
            currency=c2.selectbox("통화",["USD","KRW"])
            amount=c3.number_input("금액",min_value=.01,value=100.0,format="%.2f")
            note=st.text_input("입출금 메모",max_chars=160)
            submitted=st.form_submit_button("입출금 기록 저장")
        if submitted:
            try:_apply(state,sha,add_cashflow(state,date_value=date_value,flow_type=typ,currency=currency,amount=amount,note=note))
            except LedgerError as exc:st.error(str(exc))
    with r:
        if flows:
            with st.form("v12_remove_flow"):
                labels={r["id"]:f"{r['date']} · {r['type']} {r['amount']} {r['currency']}" for r in flows}
                chosen=st.selectbox("잘못 입력한 입출금 기록",list(labels),format_func=lambda x:labels[x])
                confirm=st.checkbox("원장 기록만 삭제하고 증권사 잔고는 변경하지 않음을 확인합니다.")
                submit=st.form_submit_button("입출금 기록 삭제")
            if submit:
                if not confirm:st.error("삭제 확인이 필요합니다.")
                else:
                    try:_apply(state,sha,remove_cashflow(state,chosen))
                    except LedgerError as exc:st.error(str(exc))

def render_full_workspace(data,research):
    _stylesheet()
    st.markdown('<div class="v12-headline">SOXL V12 Final · 운용 대시보드</div>',unsafe_allow_html=True)
    st.markdown(f'<div class="v12-subline">6분할 · LOC +3.2% · D2 손절 / D7 MOC · 메리츠 READ ONLY · {BUILD}</div>',unsafe_allow_html=True)
    try:state,sha=load_portfolio_state(st.secrets)
    except LedgerError as exc:
        st.error("실제 거래기록 로드 실패: "+str(exc))
        st.warning("암호화 원장 조회 오류가 해결될 때까지 실제 보유수량 수정·매도·입출금 저장을 중지합니다.")
        return
    values=_snapshot_values(data,state)
    actual_rows=_rows(state,values,data)
    l,r=st.columns([1.05,1],gap="medium")
    with l:
        with st.container(border=True):
            _panel("오늘의 주문","미국장 최근 확정 종가 기준")
            ready=values["ready"]
            # Manual preview helps the operator with old saved trades, but is never named verified.
            action="BUY 참고" if values["amount"] is not None and not values["blocked"] else "WAIT / 확인"
            state_class="" if ready else "warn"
            amount=_money(values["amount"]) if values["amount"] is not None else "확인 필요"
            qty=f"{values['qty']:,}주" if values["qty"] is not None else "확인 필요"
            st.markdown(f'<div class="v12-action">'
                f'<div class="v12-action-head">TODAY\'S ACTION</div>'
                f'<div class="v12-action-state {state_class}">{escape(action)}</div>'
                f'<div class="v12-action-values"><div><small>예상 매수수량</small><strong>{escape(qty)}</strong></div>'
                f'<div><small>예상 매수금액</small><strong>{escape(amount)}</strong></div></div>'
                f'<div class="v12-muted">{escape(values["source"])} · 주문 전송 없음</div></div>',unsafe_allow_html=True)
            if values["blocked"]:st.warning(values["reason"]+" · 신규매수 신호 제한")
            elif not ready:st.caption("실계좌 잔고·슬롯 수량·시세·미체결 확인을 완료하지 않아 참고값이며 실주문 가능액이 아닙니다.")
            st.caption(f"급락 추가 LOC 기준가 ${values['crash']:,.2f} · 참고 추가수량 {values['extra_qty'] if values['extra_qty'] is not None else '확인 필요'}주 · 슬롯 최대 6개")
    with r:
        with st.container(border=True):
            _panel("현재 계좌","현금·투자 평가액·총자산")
            nav=values["nav"];held=values["bqty"]*values["mark"] if values["mark"] else None
            st.markdown('<div class="v12-stat-grid">'
               +''.join('<div class="v12-stat"><div class="v12-stat-label">'+escape(label)+'</div>'
               +'<div class="v12-stat-value">'+escape(_money(val))+'</div></div>'
               for label,val in [("현금(USD)",values["usd"]),("SOXL 평가액",held),("추정 NAV",nav)])
               +'</div>',unsafe_allow_html=True)
            st.caption(f"{values['source']} · 실제 등록 슬롯 {len(state['positions'])}/6 · 등록 수량 {values['qty_book']}주 · 계좌 수량 {values['bqty']}주")
            if not values["consistent"]:st.error("실제 보유 원장과 증권사 SOXL 수량이 일치하지 않습니다.")
            if values["fx"] is None:st.caption("원화 환율 미확인: USD NAV 미산정")
    with st.container(border=True):
        _panel("보유 슬롯 미리보기","수량·매수가·실제 목표매도 금액")
        if actual_rows:
            preview=pd.DataFrame(actual_rows)
            preferred=["슬롯","매수일","보유수량","매수가($)","매수금액($)","LOC 목표매도가($)","목표매도금액($)","평가손익($)","상태"]
            st.dataframe(preview[preferred],hide_index=True,use_container_width=True)
        else:st.info("등록된 실제 보유 슬롯이 없습니다. 아래에서 기존 실매수 슬롯을 등록할 수 있습니다.")
    left,right=st.columns([1.65,1],gap="small")
    with left:
        with st.container(border=True):
            _panel("상세 주문 · 슬롯 편집 · 실제 체결가 관리")
            t1,t2,t3=st.tabs(["전체","보유 슬롯","최근 종료"])
            with t1:
                if actual_rows:st.dataframe(pd.DataFrame(actual_rows),hide_index=True,use_container_width=True)
                else:st.caption("실제 등록된 보유 슬롯이 없습니다.")
            with t2:
                if actual_rows:st.dataframe(pd.DataFrame(actual_rows)[["ID","매수일","보유수량","매수가($)","LOC 목표매도가($)","경과 거래일","상태"]],hide_index=True,use_container_width=True)
                else:st.caption("보유 슬롯 없음")
            with t3:
                if state["closed_trades"]:
                    st.dataframe(pd.DataFrame(state["closed_trades"]).tail(20),hide_index=True,use_container_width=True)
                else:st.caption("실제 체결 등록된 청산 내역 없음")
            st.caption("가상 종료 기록과 실제 매도 체결은 구분합니다. 데이터 아래쪽의 편집·매도 등록 기능을 사용하세요.")
    with right:
        with st.container(border=True):
            _panel("전략 성과 요약","V12 연구 백테스트")
            stats=research["stats"]
            def pct(key):
                v=stats.get(key)
                return f"{float(v)*100:.1f}%" if v is not None else "—"
            st.markdown('<div class="v12-stat-grid">'
                +''.join('<div class="v12-stat"><div class="v12-stat-label">'+escape(label)+'</div>'
                +'<div class="v12-stat-value">'+escape(val)+'</div></div>' for label,val in [
                    ("CAGR",pct("cagr")),("MDD",pct("mdd")),("Calmar",f"{float(stats.get('calmar',0)):.2f}")])
                +'</div>',unsafe_allow_html=True)
            st.caption("연구용 성과이며 실제 보유·체결 손익과 다릅니다.")
            st.metric("실제 등록 매도 실현손익",_money(realized_summary(state)))
    st.divider()
    _panel("실제 매매 및 슬롯 관리","저장 시 V12 암호화 원장만 수정 · 메리츠 주문 전송 없음")
    tabs=st.tabs(["✏️ 보유 슬롯·실제 매도","↩️ 매수 등록·가상 매도 취소","🧾 실제 거래·매도 취소·입출금","💵 계좌 입력·대조"])
    with tabs[0]:_edit_forms(state,sha,data)
    with tabs[1]:_restore_form(state,sha,data,research)
    with tabs[2]:_history(state,sha)
    with tabs[3]:
        _panel("실계좌 현금·시세 입력","메리츠 조회 실패 시 참고값, 자동 보정 없음")
        st.warning("수동 입력값은 이 브라우저 세션의 참고용이고 증권사 잔고를 대체하지 않습니다. 실제 주문을 실행하지 않습니다.")
        with st.form("v12_manual_account_input"):
            usd=st.number_input("USD 현금잔고",min_value=0.0,value=float(st.session_state.get("v12_manual_usd_cash",0.0)),format="%.2f")
            krw=st.number_input("KRW 현금잔고",min_value=0.0,value=float(st.session_state.get("v12_manual_krw_cash",0.0)),format="%.0f")
            price=st.number_input("SOXL 참고 시세($)",min_value=.0001,value=float(st.session_state.get("v12_manual_price",data["Close"].iloc[-1])),format="%.4f")
            fx=st.number_input("USD/KRW 참고환율",min_value=.0001,value=float(st.session_state.get("v12_manual_fx",1350.0)),format="%.2f")
            submit=st.form_submit_button("참고 계좌값 적용")
        if submit:
            st.session_state.update(v12_manual_usd_cash=str(usd),v12_manual_krw_cash=str(krw),
                v12_manual_price=str(price),v12_manual_fx=str(fx))
            st.rerun()
        st.caption("메리츠 잔고 최신 조회는 아래의 기존 READ ONLY 패널에서 수행할 수 있습니다.")
    st.caption(f"{BUILD} · 기존 V10/V11 및 live_portfolio.enc 보존 · V12 별도 암호화 거래 원장 · 매매 주문 비활성화")
