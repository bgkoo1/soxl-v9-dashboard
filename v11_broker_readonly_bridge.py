"""Broker-neutral read-only reconciliation; no network or order transmission."""
from decimal import Decimal, InvalidOperation
from datetime import datetime
ACTIVE = {'PENDING','ACCEPTED','PARTIAL','CANCEL_REQUESTED','OPEN','NEW'}
def _decimal(value, field):
    if isinstance(value,bool): raise ValueError(f'{field}: boolean not allowed')
    try: x=Decimal(str(value))
    except (InvalidOperation,TypeError): raise ValueError(f'{field}: invalid decimal') from None
    if not x.is_finite() or x<0: raise ValueError(f'{field}: must be finite and nonnegative')
    return x
def reconcile(broker_snapshot, *, expected_symbol='SOXL'):
    if not isinstance(broker_snapshot,dict): raise ValueError('snapshot must be an object')
    try: dt=datetime.fromisoformat(broker_snapshot.get('as_of'))
    except (TypeError,ValueError): raise ValueError('as_of must be ISO datetime with timezone') from None
    if dt.tzinfo is None or dt.utcoffset() is None: raise ValueError('as_of must include timezone')
    if not isinstance(broker_snapshot.get('holdings'),list) or not isinstance(broker_snapshot.get('orders'),list):
        raise ValueError('holdings and orders must be lists; missing data is not empty data')
    qty=Decimal('0')
    for h in broker_snapshot['holdings']:
        if not isinstance(h,dict) or not h.get('symbol'): raise ValueError('holding symbol missing')
        q=_decimal(h.get('quantity'),'holding quantity')
        if str(h['symbol']).upper()==expected_symbol: qty+=q
    active_orders=[]
    for o in broker_snapshot['orders']:
        if not isinstance(o,dict) or not o.get('symbol'): raise ValueError('order symbol missing')
        status=str(o.get('status','')).upper()
        if status not in ACTIVE|{'FILLED','CANCELLED','CANCELED','REJECTED','EXPIRED'}:
            raise ValueError('unrecognized order status: fail closed')
        rem=_decimal(o.get('remaining_quantity'),'remaining_quantity')
        if str(o['symbol']).upper()==expected_symbol and status in ACTIVE:
            active_orders.append({'status':status,'remaining_quantity':str(rem)})
    blockers=[]
    if qty!=0: blockers.append('SOXL 실계좌 잔고가 0이 아님')
    if active_orders: blockers.append('SOXL 활성/취소대기 주문이 존재함')
    return {'mode':'READ_ONLY','as_of':dt.isoformat(),'soxl_quantity':str(qty),
            'active_soxl_orders':active_orders,'account_flat_at_snapshot':not blockers,
            'blocking_reasons':blockers,'live_orders_enabled':False,
            'warning':'스냅샷 시점 이후의 변동은 확인할 수 없습니다. 실거래 전 재조회 필수.'}
