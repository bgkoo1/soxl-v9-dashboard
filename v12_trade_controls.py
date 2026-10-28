"""Real V12 buy/sell/undo/cashflow controls for the original-style order panel."""
from datetime import date
from decimal import Decimal
import pandas as pd
import streamlit as st
from v12_manual_holdings import LedgerError,reopen_slot,save_portfolio_state
from v12_trade_management import (
    update_slot,record_sale,edit_sale_record,cancel_sale,add_cashflow,remove_cashflow,REASONS)

def _save(state,sha,message):
    try:
        save_portfolio_state(st.secrets,state,sha)
        st.toast(message)
        st.rerun()
    except LedgerError as exc:
        st.error(str(exc))

def render_trade_controls(state,sha,error,latest_close):
    st.divider()
    if error:
        st.error("암호화 보유 원장을 확인할 수 없어 편집·매도·저장을 중단합니다.")
        return
    lots=state["positions"];sales=state["closed_trades"];flows=state["cashflows"]
    tab_buy,tab_edit,tab_sell,tab_sale_edit,tab_undo,tab_cash=st.tabs(
        ["매수 등록","보유 슬롯 수정","실제 매도 등록","청산 거래 수정","매도 취소·복구","입출금 관리"])
    with tab_buy:
        st.caption("실제로 체결된 매수만 등록하세요. 전략 가상매수와 혼동하지 않습니다.")
        with st.form("v12_real_buy"):
            lot_id=st.text_input("슬롯 ID",placeholder="예: actual-2026-10-28-01")
            buy_day=st.date_input("실제 매수일",value=date.today(),key="v12_buy_day")
            entry=st.number_input("매수 체결가 (USD)",min_value=.0001,value=float(latest_close),format="%.4f",key="v12_new_buy_price")
            quantity=st.number_input("매수수량 (주)",min_value=1,value=1,step=1,key="v12_new_buy_qty")
            note=st.text_input("매수 메모",key="v12_new_buy_note")
            submitted=st.form_submit_button("실제 매수 슬롯 저장",type="primary")
        if submitted:
            try:
                new={"slot_id":lot_id,"entry_date":buy_day.isoformat(),"entry_price":str(entry),
                     "quantity":str(quantity),"source":"manual","model_exit":"직접 매수","note":note}
                result={**state,"positions":reopen_slot(lots,new)}
                _save(result,sha,"실제 매수 슬롯 저장 완료")
            except LedgerError as exc:
                st.error(str(exc))
        st.divider()
        st.markdown("#### 기존 V10/V11 저장 슬롯 가져오기")
        st.caption("기존 암호화 원장의 저장 수량은 이미 매도됐을 수도 있습니다. 직접 확인한 미매도 슬롯만 V12로 복사하세요. 이전 원장은 수정하지 않습니다.")
        if st.button("기존 V10/V11 저장 슬롯 조회 (읽기 전용)",key="v12_legacy_import_refresh"):
            from v12_legacy_import import load_legacy_candidates
            try:
                st.session_state["v12_legacy_import_candidates"]=load_legacy_candidates(st.secrets)
            except LedgerError as exc:
                st.error(str(exc))
        candidates=st.session_state.get("v12_legacy_import_candidates",[])
        available=[x for x in candidates if x["slot_id"] not in {p["slot_id"] for p in lots}]
        if available:
            names={f"{x['entry_date']} · 수량 {x['saved_quantity']}주 · {x['old_key']}":x for x in available}
            picked=st.selectbox("가져올 기존 슬롯",list(names),key="v12_legacy_selected")
            found=names[picked]
            with st.form("v12_legacy_import_form"):
                dt=st.date_input("실제 매수일",value=date.fromisoformat(found["entry_date"]))
                default_price=float(found["saved_entry_price"]) if found["saved_entry_price"] else float(latest_close)
                price=st.number_input("실제 매수가 (USD)",min_value=.0001,value=default_price,format="%.4f")
                qty=st.number_input("현재 미매도 수량(주)",min_value=1,
                     value=max(1,int(Decimal(found["saved_quantity"]))),step=1)
                acknowledged=st.checkbox("기존 저장기록을 실제 계좌와 대조했으며, 지금도 미매도 상태로 보유 중임을 확인했습니다.")
                do_import=st.form_submit_button("선택 슬롯만 V12 실제 보유로 복사",type="primary")
            if do_import:
                if not acknowledged:
                    st.error("미매도 상태와 체결가·수량을 확인해야 합니다.")
                else:
                    try:
                        row={"slot_id":found["slot_id"],"entry_date":dt.isoformat(),
                             "entry_price":str(price),"quantity":str(qty),
                             "source":"manual","model_exit":"V10/V11 기존 기록 가져오기",
                             "note":"기존 저장 원장에서 사용자 확인 후 복사"}
                        _save({**state,"positions":reopen_slot(lots,row)},sha,"선택한 기존 슬롯 복사 완료 · 기존 원장 보존")
                    except LedgerError as exc:
                        st.error(str(exc))
        elif candidates:
            st.caption("이미 V12로 복사한 슬롯만 있거나 가져올 후보가 없습니다.")
    with tab_edit:
        if not lots:
            st.caption("수정할 실제 보유 슬롯이 없습니다.")
        else:
            items={f"{x['entry_date']} · {x['slot_id']} · {x['quantity']}주":x for x in lots}
            chosen=st.selectbox("수정할 슬롯",list(items),key="v12_actual_edit_selector")
            x=items[chosen]
            with st.form("v12_actual_edit_form"):
                day=st.date_input("매수일",value=date.fromisoformat(x["entry_date"]))
                price=st.number_input("실제 매수 체결가 (USD)",min_value=.0001,
                        value=float(x["entry_price"]),format="%.4f",key="v12_actual_edit_price")
                qty=st.number_input("현재 실제 미매도 수량",min_value=1,
                        value=int(Decimal(x["quantity"])),step=1,key="v12_actual_edit_quantity")
                memo=st.text_input("메모",value=x.get("note",""),key="v12_actual_edit_memo")
                confirmed=st.checkbox("실제 보유수량·체결가를 확인했습니다.")
                saved=st.form_submit_button("💾 실제 수량·체결가 저장")
            if saved:
                if not confirmed:
                    st.error("실제 거래내역을 확인한 후 저장하세요.")
                else:
                    try:
                        result=update_slot(state,x["slot_id"],entry_date=day.isoformat(),
                                entry_price=price,quantity=qty,note=memo)
                        _save(result,sha,"실제 체결가·수량 수정 완료")
                    except LedgerError as exc:
                        st.error(str(exc))
    with tab_sell:
        if not lots:
            st.caption("실제 매도 처리할 보유 슬롯이 없습니다.")
        else:
            items={f"{x['entry_date']} · {x['slot_id']} · {x['quantity']}주":x for x in lots}
            chosen=st.selectbox("매도한 슬롯",list(items),key="v12_actual_sell_selector")
            x=items[chosen]
            with st.form("v12_actual_sell_form"):
                sold=st.number_input("실제 매도 수량 (부분매도 가능)",min_value=1,
                     max_value=int(Decimal(x["quantity"])),value=int(Decimal(x["quantity"])),
                     step=1,key="v12_actual_sale_qty")
                day=st.date_input("실제 매도일",value=date.today(),key="v12_actual_sale_date")
                price=st.number_input("실제 매도가 (USD)",min_value=.0001,
                     value=float(Decimal(x["entry_price"])*Decimal("1.032")),
                     format="%.4f",key="v12_actual_sale_price")
                reason=st.selectbox("매도 유형",["LOC +3.2%","D2 손절","D7 MOC","조건부 전체 MOC","수동 매도","기타"])
                memo=st.text_input("메모",key="v12_actual_sale_note")
                confirmed=st.checkbox("증권사에서 실제로 매도 체결된 수량과 단가를 확인했습니다.")
                submitted=st.form_submit_button("매도 체결 등록",type="primary")
            if submitted:
                if not confirmed:
                    st.error("증권사 실제 체결 확인이 필요합니다.")
                else:
                    try:
                        result=record_sale(state,x["slot_id"],quantity=sold,
                                 exit_date=day.isoformat(),exit_price=price,
                                 exit_strategy=reason,note=memo)
                        _save(result,sha,"실제 매도 등록 · 잔여 보유수량 반영 완료")
                    except LedgerError as exc:
                        st.error(str(exc))
    with tab_sale_edit:
        if not sales:
            st.caption("수정할 실제 매도기록이 없습니다.")
        else:
            options={f"{x['exit_date']} · {x['slot_id']} · {x['quantity']}주":x for x in sales}
            picked=st.selectbox("수정할 청산 거래",list(options),key="v12_sale_edit_selector")
            x=options[picked]
            with st.form("v12_sale_edit_form"):
                sale_day=st.date_input("실제 매도일",value=date.fromisoformat(x["exit_date"]),key="v12_sale_edit_day")
                sale_price=st.number_input("실제 매도 체결가 (USD)",min_value=.0001,
                      value=float(x["exit_price"]),format="%.4f",key="v12_sale_edit_price")
                choices=["LOC +3.2%","D2 손절","D7 MOC","조건부 전체 MOC","수동 매도","기타"]
                reason=st.selectbox("매도 유형",choices,index=choices.index(x["exit_strategy"]),key="v12_sale_edit_reason")
                memo=st.text_input("메모",value=x.get("note",""),key="v12_sale_edit_note")
                confirmed=st.checkbox("실제 매도 체결내역을 확인했습니다.",key="v12_sale_edit_confirm")
                submitted=st.form_submit_button("실제 매도 체결가·거래일 수정 저장")
            if submitted:
                if not confirmed:
                    st.error("수정할 실제 매도내역을 확인해 주세요.")
                else:
                    try:
                        result=edit_sale_record(state,x["id"],exit_date=sale_day.isoformat(),
                            exit_price=sale_price,exit_strategy=reason,note=memo)
                        _save(result,sha,"실제 청산 거래 기록 수정 완료")
                    except LedgerError as exc:
                        st.error(str(exc))
            st.caption("매도수량이 잘못 입력된 경우에는 '매도 취소·복구'에서 취소 후 정확한 수량으로 다시 등록하세요.")
    with tab_undo:
        if not sales:
            st.info("사용자가 직접 등록한 실제 매도기록이 없습니다. 전략의 가상 자동매도 취소는 하단 ‘가상 매도 취소·실제 보유’ 탭에서 진행하세요.")
        else:
            items={f"{x['exit_date']} · {x['slot_id']} · {x['quantity']}주 · 매도가 {x['exit_price']} USD":x for x in sales}
            picked=st.selectbox("취소할 실제 매도 기록",list(items),key="v12_actual_undo_selector")
            x=items[picked]
            with st.form("v12_actual_undo_form"):
                confirmed=st.checkbox("이 매도는 실제로 체결되지 않았으며, 원래 보유수량으로 복원해야 합니다.")
                submitted=st.form_submit_button("↩ 매도 취소 · 수량 복원",type="primary")
            if submitted:
                if not confirmed:
                    st.error("실제 미체결 여부를 확인해야 합니다.")
                else:
                    try:
                        result=cancel_sale(state,x["id"])
                        _save(result,sha,"매도 취소 · 원래 슬롯으로 복원 완료")
                    except LedgerError as exc:
                        st.error(str(exc))
    with tab_cash:
        st.caption("입출금은 별도 거래장부로 관리합니다. 메리츠 현금 잔고에 중복 합산하지 않습니다.")
        with st.form("v12_actual_cash_form"):
            day=st.date_input("입출금일",value=date.today(),key="v12_cash_day")
            typ=st.selectbox("구분",["입금","출금"],key="v12_cash_type")
            currency=st.selectbox("통화",["USD","KRW"],key="v12_cash_currency")
            value=st.number_input("금액",min_value=.01,value=100.0,key="v12_cash_amount")
            note=st.text_input("메모",key="v12_cash_memo")
            submitted=st.form_submit_button("입출금 기록 추가")
        if submitted:
            try:
                result=add_cashflow(state,date_value=day.isoformat(),flow_type=typ,
                          currency=currency,amount=value,note=note)
                _save(result,sha,"입출금 기록 저장 완료")
            except LedgerError as exc:
                st.error(str(exc))
        if flows:
            table=pd.DataFrame([{"일자":x["date"],"구분":x["type"],"통화":x["currency"],
                        "금액":float(x["amount"]),"메모":x["note"]} for x in flows])
            st.dataframe(table,hide_index=True,use_container_width=True)
            options={f"{x['date']} · {x['type']} {x['amount']} {x['currency']} · {x['id'][:8]}":x for x in flows}
            chosen=st.selectbox("잘못 입력한 거래 선택",list(options),key="v12_cash_remove_selector")
            with st.form("v12_cash_remove_form"):
                checked=st.checkbox("해당 입출금 기록이 잘못 등록되었음을 확인했습니다.")
                delete=st.form_submit_button("선택 기록 삭제")
            if delete:
                if not checked:st.error("삭제 확인을 선택하세요.")
                else:
                    try:_save(remove_cashflow(state,options[chosen]["id"]),sha,"입출금 기록 삭제 완료")
                    except LedgerError as exc:st.error(str(exc))
