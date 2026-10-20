"""V11 dry-run order planner v3. No brokerage API calls or real orders.

Snapshot values must come from V11. Does not calculate strategy signals.
An existing active buy is checked by order purpose (BASE vs CRASH_EXTRA),
not globally. Reserved slots/cash include active buys; partial fills must be
reconciled externally before re-planning. Conservative MOC sizing uses buffer.
"""
import argparse
import json
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from pathlib import Path

D = lambda v: Decimal(str(v))
def up_cents(v):
    return str(D(v).quantize(D('0.01'), rounding=ROUND_UP))

def plan(s):
    idx = int(s['trading_day_index'])
    close, prev = D(s['last_confirmed_close']), D(s['previous_close'])
    if min(close, prev) <= 0:
        raise ValueError('prices must be positive')
    positions = s.get('positions', [])
    pending = s.get('pending_orders', [])
    max_slots = int(s.get('max_slots', 7))
    if not 1 <= max_slots <= 100:
        raise ValueError('invalid max_slots')
    ids = [str(p['slot_id']) for p in positions]
    if len(ids) != len(set(ids)) or len(ids) > max_slots:
        raise ValueError('duplicate or excess occupied slots')
    active = [o for o in pending if o.get('status') in ('PENDING', 'ACCEPTED', 'PARTIAL', 'CANCEL_REQUESTED')]
    result = dict(mode='DRY_RUN_NO_BROKER', date=s['date'], orders=[], cancellations=[], d2_flags_to_save=[], notes=[])
    def add(slot, side, kind, price, quantity, reason):
        result['orders'].append(dict(slot_id=slot, side=side, type=kind, price=str(price), quantity=str(quantity), reason=reason))
    for p in positions:
        slot = str(p['slot_id'])
        age = idx - int(p['entry_trading_day_index'])
        qty, entry = D(p['quantity']), D(p['entry_price'])
        if qty <= 0 or entry <= 0 or age < 0:
            raise ValueError('invalid position: '+slot)
        sells = [o for o in active if str(o.get('slot_id')) == slot and o.get('side') == 'SELL']
        flagged = bool(p.get('d2_flag', False))
        if age == 2 and not flagged and close / entry - 1 <= D(s.get('d2_cut', '-0.10')):
            result['d2_flags_to_save'].append(dict(slot_id=slot, d2_flag=True, effective_next_trading_day_index=idx+1))
            flagged = True
        reason = 'D2_CUT_NEXT_MOC' if age >= 3 and flagged else ('TIME' if age >= int(s.get('holding_days_limit', 7)) else None)
        if reason:
            if sells:
                for o in sells:
                    if o['status'] != 'CANCEL_REQUESTED':
                        result['cancellations'].append(dict(slot_id=slot, order_id=o['order_id'], action='REQUEST_CANCEL', dry_run=True))
                result['notes'].append(f'{slot}: MOC blocked pending confirmation of existing sell cancellation')
            else:
                add(slot, 'SELL', 'MOC', '0', qty, reason)
        elif flagged:
            result['notes'].append(f'{slot}: D2 flagged; wait for D3 MOC')
        elif not sells:
            add(slot, 'SELL', 'LOC', up_cents(entry*(1+D(s.get('target_return','0.027')))), qty, 'NORMAL_TARGET')
    if s.get('risk_b') or s.get('pause_4down') or not s.get('allow_buy', False):
        result['notes'].append('New entries blocked or disabled')
        return result
    # Active buy order reservations are supplied from the order ledger.
    active_buys = [o for o in active if o.get('side') == 'BUY']
    purposes = [o.get('reason') for o in active_buys]
    if any(p not in ('BASE','CRASH_EXTRA') for p in purposes):
        result['notes'].append('Unknown active buy purpose; entry blocked for safety')
        return result
    if len(set(purposes)) != len(purposes):
        result['notes'].append('Duplicate active buy purpose in ledger; entry blocked')
        return result
    free = max_slots - len(positions) - len(active_buys)
    if free <= 0:
        result['notes'].append('No unreserved slot')
        return result
    nav, cash, reference = D(s['previous_nav']), D(s['available_cash']), D(s['sizing_reference_price'])
    if min(nav, reference) <= 0 or cash < 0:
        raise ValueError('invalid sizing values')
    reserve = nav * D(s.get('reserve_ratio','0'))
    usable = max(D(0), cash-reserve)
    buffer = D(s.get('moc_sizing_buffer','0.05'))
    if not 0 <= buffer < 1:
        raise ValueError('invalid buffer')
    divisor = D(s.get('position_divisor',7))
    if divisor <= 0:
        raise ValueError('invalid divisor')
    # If the snapshot's available_cash does not exclude existing pending BUY
    # reservations, subtract them here using their conservative max cost.
    if not s.get('available_cash_excludes_pending_buys', False):
        for o in active_buys:
            if 'reserved_cash' not in o:
                result['notes'].append('Missing reserved_cash for active buy; entry blocked')
                return result
            usable -= D(o['reserved_cash'])
        usable = max(D(0), usable)
    if 'BASE' not in purposes and free:
        allocation = min(usable, nav/divisor)
        q = int((allocation/(reference*(1+buffer))).to_integral_value(rounding=ROUND_DOWN))
        if q > 0:
            add('NEW_BASE','BUY','MOC','0',q,'BASE')
            usable -= D(q)*reference*(1+buffer)
            free -= 1
        else:
            result['notes'].append('Base MOC cannot size one share')
    if 'CRASH_EXTRA' not in purposes and free > 0 and s.get('enable_crash_buy', True):
        threshold = prev * D(s.get('crash_loc_multiplier','0.91'))
        # Round the limit UP to a cent, then size at that rounded limit.
        limit = D(up_cents(threshold))
        allocation = min(usable, nav/divisor)
        q = int((allocation/limit).to_integral_value(rounding=ROUND_DOWN))
        if q > 0:
            add('NEW_CRASH','BUY','LOC',limit,q,'CRASH_EXTRA')
        else:
            result['notes'].append('Crash LOC cannot size one share')
    result['notes'].append('Dry-run only. No live cutoff, broker order code, exchange, fill, or price tick validation.')
    return result

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('snapshot'); ap.add_argument('--output')
    args=ap.parse_args()
    res=plan(json.loads(Path(args.snapshot).read_text(encoding='utf-8')))
    out=json.dumps(res,ensure_ascii=False,indent=2)
    if args.output: Path(args.output).write_text(out+'\n',encoding='utf-8')
    print(out)
if __name__=='__main__': main()
