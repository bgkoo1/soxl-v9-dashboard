"""V12 actual trade/slot editor. Manual, encrypted, broker READ ONLY."""
from datetime import date
from decimal import Decimal
import pandas as pd
import streamlit as st
from v12_manual_holdings import (
    LedgerError, load_portfolio_state, save_portfolio_state, validate_ledger,
    reopen_slot, normalize_slot)
from v12_trade_management import (
    update_slot,record_sale,cancel_sale,add_cashflow,remove_cashflow,
    realized_summary,cashflow_summary,REASONS)

def _commit(state, mutator):
    """Read fresh SHA before any user-authorized mutation; reject stale UIs."""
    fresh,sha=load_portfolio_state(st.secrets)
    before=state.get("_sha")
    if before!=sha:
        raise LedgerError("다른 사용자가 기록을 수정했습니다. 페이지를 새로고침한 뒤 확인하세요.")
    updated=mutator(fresh)
    save_portfolio_state(st.secrets,updated,sha)
    st.toast("실제 운용 기록을 암호화해 저장했습니다.")
    st.rerun()

def _position_table(positions,price=None,fx=None):
    rows=[]
    for p in positions:
        q=Decimal(p["quantity"]);entry=Decimal(p["entry_price"])
        target=entry*Decimal("1.032")
        mark=q*Decimal(str(price)) if price is not None else None
        rows.append({
            "선택 ID":p["slot_id"],"매수일":p["entry_date"],
            "보유수량(주)":float(q),"실제 매수가($)":float(entry),
            "매수금액($)":float(q*entry),"LOC 목표가($)":float(target),
            "목표매도금액($)":float(target*q),
            "평가액($)":float(mark) if mark is not None else None,
            "평가손익($)":float(mark-q*entry) if mark is not None else None,
            "상태":"실제 미매도 보유",
        })
    return pd.DataFrame(rows)

def render_management(state,market_price=None,fx=None):
    st.markdown("### 상세 주문 · 슬롯 편집 · 실제 체결가 관리")
    st.caption("기존처럼 실제 체결수량·매수가를 수정하고 매도 체결을 등록·취소할 수 있습니다. 가상 매도는 실제 체결로 기록하지 않습니다.")
    if state is None:
        st.error("암호화 원장을 불러오지 못했으므로 실제 체결 편집을 차단했습니다.")
        return
    slots=state["positions"]
    trades=state["closed_trades"]
    cashflows=state["cashflows"]
    t_all,t_open,t_closed,t_cash=st.tabs(["전체","보유 슬롯","최근 종료·매도 취소","입출금"])
    with t_all:
        left,right=st.columns(2)
        left.metric("실제 보유 슬롯",f"{len(slots)} / 6")
        right.metric("등록 실현손익(USD)",f"${realized_summary(state):,.2f}")
        if slots:
            st.dataframe(_position_table(slots,market_price,fx),hide_index=True,use_container_width=True)
        else:
            st.info("실제 보유 슬롯이 등록되지 않았습니다.")
        if trades:
            st.markdown("#### 실제 매도 거래")
            df=pd.DataFrame(trades)
            st.dataframe(df.iloc[::-1],hide_index=True,use_container_width=True)
        st.caption("표의 매수가·매도가는 사용자 등록값입니다. 모든 거래는 메리츠 주문·체결 내역과 별도로 대조해야 합니다.")
    with t_open:
        st.markdown("#### 실제 보유 슬롯 수정")
        if slots:
            names={f"{x['entry_date']} · {x['slot_id']} · {x['quantity']}주":x for x in slots}
            pick=st.selectbox("보유 슬롯 선택",list(names),key="v12_selected_open")
            lot=names[pick]
            with st.form("v12_update_actual_position"):
                d=st.date_input("실제 매수일",value=date.fromisoformat(lot["entry_date"]))
                p=st.number_input("실제 매수체결가($)",min_value=.0001,value=float(lot["entry_price"]),format="%.4f")
                q=st.number_input("실제 보유수량(주)",min_value=1.0,value=float(lot["quantity"]),step=1.0)
                note=st.text_input("메모",value=lot.get("note",""),max_chars=160)
                accept=st.checkbox("메리츠 실제 체결·보유내역을 확인하고 수정합니다.")
                save=st.form_submit_button("💾 실제 수량·체결가 저장",type="primary")
            if save:
                if not accept:st.error("확인란을 체크해야 합니다.")
                else:
                    try:_commit(state,lambda fresh:update_slot(fresh,lot["slot_id"],entry_date=d.isoformat(),entry_price=p,quantity=q,note=note))
                    except LedgerError as exc:st.error(str(exc))
            st.markdown("#### 실제 매도 체결 등록")
            with st.form("v12_actual_sale"):
                s_qty=st.number_input("실제로 매도한 수량(주)",min_value=1.0,max_value=float(lot["quantity"]),value=1.0,step=1.0)
                exit_day=st.date_input("실제 매도일",value=date.today(),key="v12_actual_exit_day")
                exit_price=st.number_input("실제 매도체결가($)",min_value=.0001,value=max(.0001,float(lot["entry_price"])),format="%.4f")
                reason=st.selectbox("매도 유형",["LOC +3.2%","D2 손절","D7 MOC","조건부 전체 MOC","수동 매도","기타"])
                sell_note=st.text_input("체결 메모(선택)")
                confirmed=st.checkbox("메리츠에서 실제 매도 체결된 수량·가격·날짜를 확인했습니다.")
                sell=st.form_submit_button("실제 매도 체결 저장")
            if sell:
                if not confirmed:st.error("실제 체결을 확인한 경우에만 기록할 수 있습니다.")
                else:
                    try:_commit(state,lambda fresh:record_sale(fresh,lot["slot_id"],quantity=s_qty,exit_date=exit_day.isoformat(),exit_price=exit_price,exit_strategy=reason,note=sell_note))
                    except LedgerError as exc:st.error(str(exc))
        else:st.info("아래 가상 매도 취소·실제 보유 탭에서 기존 매수 슬롯을 등록하거나 복구하세요.")
    with t_closed:
        st.markdown("#### 실제 매도 기록 · 매도 처리 취소")
        if not trades:
            st.info("등록된 실제 매도 거래가 없습니다.")
        else:
            table=pd.DataFrame([{
                "거래 ID":t["id"],"매수일":t["entry_date"],"매도일":t["exit_date"],
                "슬롯 ID":t["slot_id"],"매도수량":float(t["quantity"]),
                "실제 매도가($)":float(t["exit_price"]),
                "실현손익($)":float((Decimal(t["exit_price"])-Decimal(t["entry_price"]))*Decimal(t["quantity"])),
                "매도 유형":t["exit_strategy"],
            } for t in trades])
            st.dataframe(table.iloc[::-1],use_container_width=True,hide_index=True)
            labels={f"{t['exit_date']} · {t['slot_id']} · {t['quantity']}주 · {t['exit_strategy']}":t["id"] for t in trades}
            with st.form("v12_reverse_actual_sale"):
                selected=st.selectbox("취소할 매도 거래",list(labels))
                check=st.checkbox("실제 매도하지 않았거나 잘못 기록한 거래임을 확인합니다. 취소 시 미매도 슬롯으로 수량이 복구됩니다.")
                reverse=st.form_submit_button("↩️ 선택 매도 취소 · 보유 슬롯 복구")
            if reverse:
                if not check:st.error("취소 사유 확인이 필요합니다.")
                else:
                    try:_commit(state,lambda fresh:cancel_sale(fresh,labels[selected]))
                    except LedgerError as exc:st.error(str(exc))
    with t_cash:
        st.markdown("#### 실제 입금 · 출금 기록")
        summary=cashflow_summary(state)
        x,y=st.columns(2)
        x.metric("USD 누적 순입출금",f"${summary['USD']:,.2f}")
        y.metric("원화 누적 순입출금",f"₩{summary['KRW']:,.0f}")
        if cashflows:
            st.dataframe(pd.DataFrame(cashflows).iloc[::-1],hide_index=True,use_container_width=True)
        with st.form("v12_new_cashflow"):
            d=st.date_input("입출금 일자",value=date.today(),key="v12_flow_day")
            kind=st.radio("구분",["입금","출금"],horizontal=True)
            currency=st.selectbox("통화",["USD","KRW"])
            amount=st.number_input("금액",min_value=.01,value=100.0,step=100.0)
            memo=st.text_input("입출금 메모")
            ok=st.checkbox("실제 계좌 입출금 내역을 확인했습니다.")
            create=st.form_submit_button("입출금 기록 저장")
        if create:
            if not ok:st.error("실제 내역 확인이 필요합니다.")
            else:
                try:_commit(state,lambda fresh:add_cashflow(fresh,date_value=d.isoformat(),flow_type=kind,currency=currency,amount=amount,note=memo))
                except LedgerError as exc:st.error(str(exc))
        if cashflows:
            labels={f"{r['date']} · {r['type']} {r['amount']} {r['currency']} · {r['id'][:8]}":r["id"] for r in cashflows}
            with st.form("v12_remove_cashflow"):
                target=st.selectbox("잘못 등록한 입출금",list(labels))
                check=st.checkbox("이 입출금 기록이 잘못 등록되었음을 확인합니다.")
                remove=st.form_submit_button("선택 입출금 기록 취소")
            if remove:
                if not check:st.error("취소 확인이 필요합니다.")
                else:
                    try:_commit(state,lambda fresh:remove_cashflow(fresh,labels[target]))
                    except LedgerError as exc:st.error(str(exc))
