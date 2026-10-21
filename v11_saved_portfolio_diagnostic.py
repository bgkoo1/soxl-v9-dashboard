"""Read-only saved-position signal view. No broker API and no orders.

Only explicitly persisted quantities count as verified *user records*; they are
not proof of current broker holdings. The strategy engine provides slot ages.
"""
import math
import pandas as pd


def build_rows(open_positions, saved_quantities, entry_prices, excluded, key_fn,
               as_of_index, as_of_close, target_return=0.027,
               holding_days_limit=7, d2_cut=-0.10):
    rows = []
    excluded = set(excluded or [])
    for p in open_positions or []:
        key = key_fn(p)
        if key in excluded:
            continue
        raw_qty = (saved_quantities or {}).get(key)
        valid = False
        qty = None
        if raw_qty is not None:
            try:
                f = float(raw_qty)
                if math.isfinite(f) and f >= 0 and f.is_integer():
                    qty, valid = int(f), True
            except (TypeError, ValueError, OverflowError):
                pass
        if valid and qty == 0:
            continue
        entry = float((entry_prices or {}).get(key, p['entry_price']))
        age = int(as_of_index) - int(p['entry_original_index'])
        flagged = bool(p.get('d2_flag', False))
        if flagged and age >= 3:
            signal = 'D2 손절 → MOC 검토'
        elif age >= holding_days_limit:
            signal = 'TIME → MOC 검토'
        elif flagged:
            signal = 'D2 플래그 → D3 대기'
        elif age == 2 and float(as_of_close)/entry-1 <= d2_cut:
            signal = 'D2 손절 조건 충족 (다음 거래일 검토)'
        else:
            signal = '익절 LOC 목표가 검토'
        rows.append({
            '슬롯 ID': key, '매수일': str(pd.Timestamp(p['entry_date']).date()),
            '저장된 수량(주)': qty if valid else None,
            '수량 출처': '사용자 저장값' if valid else '미확인 (자동 추정 제외)',
            '저장 매수가($)': round(entry, 4),
            '익절 목표가($)': round(entry*(1+target_return), 4),
            '경과 거래일': age, 'D2 플래그': flagged,
            '신호 진단': signal,
        })
    return rows
