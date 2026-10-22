"""Read-only model-slot reconstruction. Synthetic slots are never broker positions."""
import pandas as pd
from SOXL_V11_backtest import run_v11_live_transition

def position_key(p):
    pid=p.get("position_id")
    if pid is not None and str(pid).startswith("v11-"):
        return "v11|"+str(pid).strip()
    if pid is not None and str(pid).strip() and str(pid).lower()!="nan":
        return "v10|"+str(pid).strip()
    return f"{pd.Timestamp(p['entry_date']).date().isoformat()}|{float(p['entry_price']):.6f}"

def reconstruct(prices, saved, transition_date="2026-10-19"):
    settings=saved.get("settings",{}) if isinstance(saved.get("settings"),dict) else {}
    start=settings.get("start_date")
    if not start:
        raise ValueError("저장된 운용 시작일이 없습니다. 모델 슬롯 재구성을 중단합니다.")
    capital=float(settings.get("initial_capital",0))
    if not 1_000_000<=capital<=10_000_000_000:
        raise ValueError("운용 초기 투자금 설정이 유효하지 않습니다.")
    result=run_v11_live_transition(prices,initial_capital=capital,start_date=start,
        end_date=prices["Date"].max(),transition_date=transition_date)
    positions=result.get("open_positions",[])
    if len(positions)>7:
        raise ValueError("모델 슬롯이 7개를 초과하여 표시를 중단합니다.")
    qty=saved.get("quantities",{})
    qty=qty if isinstance(qty,dict) else {}
    entry=saved.get("entry_prices",{})
    entry=entry if isinstance(entry,dict) else {}
    excluded=set(saved.get("excluded_positions",[]) or [])
    rows=[]
    for p in positions:
        key=position_key(p)
        if key in excluded:
            continue
        value=qty.get(key)
        actual_qty=None
        try:
            if value is not None and float(value).is_integer() and float(value)>=0:
                actual_qty=int(value)
        except (ValueError,TypeError,OverflowError):
            pass
        if actual_qty==0:
            continue
        price=float(entry.get(key,p["entry_price"]))
        rows.append({"슬롯 ID":key,"모델 진입일":str(pd.Timestamp(p["entry_date"]).date()),
            "저장 수량(주)":actual_qty,"저장/모델 매수가($)":round(price,4),
            "모델 평가손익($)":round(actual_qty*(float(prices["Close"].iloc[-1])-price),2) if actual_qty is not None else None,
            "검증 상태":"저장 수량 있음 · 증권사 미대조" if actual_qty is not None else "수량 미확인 · 증권사 미대조"})
    return rows


def audit_saved_vs_model(prices, saved):
    """Diagnostic only: reconcile stored keys against reconstructed model keys."""
    rows=reconstruct(prices,saved)
    model={r["슬롯 ID"]:r for r in rows}
    quantities=saved.get("quantities",{})
    quantities=quantities if isinstance(quantities,dict) else {}
    entries=saved.get("entry_prices",{})
    entries=entries if isinstance(entries,dict) else {}
    excluded=saved.get("excluded_positions",[])
    excluded=set(excluded) if isinstance(excluded,list) else set()
    records=[]
    for key in sorted(set(model)|set(quantities)|set(entries)|excluded):
        m=model.get(key)
        raw=quantities.get(key)
        valid=False
        quantity=None
        try:
            if raw is not None:
                f=float(raw)
                if __import__("math").isfinite(f) and f.is_integer() and f>=0:
                    quantity=int(f)
                    valid=True
        except (ValueError,TypeError,OverflowError):
            pass
        if key in excluded:
            status="저장된 제외 기록 · 현재 보유 판단 불가"
        elif m is None:
            status="모델 미청산 목록에 없음 · 과거 기록 가능"
        elif not valid:
            status="모델 슬롯 존재 · 저장 수량 없음/오류"
        elif quantity==0:
            status="저장 수량 0 · 모델과 확인 필요"
        else:
            status="모델 슬롯·저장 수량 연결 · 증권사 미검증"
        records.append({
            "슬롯 ID":key,
            "모델 미청산": "예" if m is not None else "아니오",
            "저장 수량(주)":quantity,
            "저장 매수가($)":entries.get(key),
            "저장 제외": "예" if key in excluded else "아니오",
            "진단":status,
        })
    return records
