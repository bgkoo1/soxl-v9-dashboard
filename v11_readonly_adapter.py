"""Read-only V11 result -> next-session dry-run snapshot.

Does not import Streamlit dashboard, access credentials, or send orders.
Requires caller-supplied broker positions and order ledger before treating
output as actionable. Model holdings are synthetic and NOT broker holdings.
"""
from datetime import timedelta
import pandas as pd
from v11_execution_dryrun_v3 import plan


def build_next_session_snapshot(result, *, broker_positions=None, pending_orders=None,
                                broker_cash=None, sizing_reference_price=None,
                                next_session_date=None, allow_buy=False):
    """Produce a diagnostic snapshot; by default no new buys.

    Next session index = last historical Original_Index + 1; this must be
    checked against the exchange calendar before real-world use.
    Risk/pause signals for next session are not available from this result:
    entries are disabled unless explicitly supplied by a separate live signal
    computation (this adapter never authorizes entry).
    """
    daily, equity, signals = result['daily'], result['equity'], result['signal_data']
    if daily.empty or equity.empty or signals.empty:
        raise ValueError('V11 result has no observations')
    last = daily.iloc[-1]
    last_equity = equity.iloc[-1]
    last_signal = signals.iloc[-1]
    index = int(last_signal['Original_Index']) + 1
    positions = []
    source = broker_positions if broker_positions is not None else result['open_positions']
    for p in source:
        if 'entry_original_index' not in p:
            raise ValueError('Missing entry_original_index in position')
        positions.append(dict(slot_id=str(p.get('position_id', p.get('slot_id'))),
                              entry_trading_day_index=int(p['entry_original_index']),
                              entry_price=float(p['entry_price']),
                              quantity=float(p.get('shares', p.get('quantity', 0))),
                              d2_flag=bool(p.get('d2_flag', False))))
    if next_session_date is None:
        # Date is a label, not an exchange-calendar inference.
        date_label='NEXT_SESSION_UNRESOLVED'
    else:
        date_label=str(next_session_date)
    snapshot = dict(date=date_label, trading_day_index=index,
        last_confirmed_close=float(last_signal['Close']),
        previous_close=float(last_signal['Close']),
        previous_nav=float(last_equity['Equity']),
        available_cash=float(last_equity['Cash'] if broker_cash is None else broker_cash),
        sizing_reference_price=float(last_signal['Close'] if sizing_reference_price is None else sizing_reference_price),
        positions=positions, pending_orders=list(pending_orders or []),
        max_slots=int(result['stats'].get('max_positions_limit',7)),
        position_divisor=int(result['stats'].get('position_divisor',7)),
        reserve_ratio=float(result['stats'].get('reserve_ratio',0)),
        target_return=float(result['stats'].get('target_return',0.027)),
        holding_days_limit=int(result['stats'].get('holding_days_limit',7)),
        d2_cut=float(result['stats'].get('d2_cut',-0.10)),
        enable_crash_buy=bool(result['stats'].get('enable_crash_buy',True)),
        crash_loc_multiplier=1+float(result['stats'].get('crash_threshold',-0.09)),
        risk_b=True, pause_4down=True, allow_buy=False)
    # No entry signals may be inferred from yesterday's Risk_State. Setting
    # allow_buy=True here is intentionally not sufficient to bypass this.
    warnings = [
        'Historical/model positions are not verified against broker holdings.',
        'Next-session Risk B and 4-down signals are unresolved; entries disabled.',
        'Next-session MOC/LOC order eligibility and broker cutoff unverified.',
        'Next-session date and trading-day index require exchange-calendar validation.',
        'Current-day LOC exit orders may already exist; missing order ledger is unsafe.',
    ]
    if broker_positions is None: warnings.append('Using synthetic backtest holdings, not actual shares.')
    if pending_orders is None: warnings.append('No broker order ledger supplied; proposed exits are diagnostic only.')
    return snapshot, warnings


def compare_diagnostic(result, **kwargs):
    snapshot, warnings = build_next_session_snapshot(result, **kwargs)
    planned = plan(snapshot)
    planned['warnings'] = warnings
    planned['diagnostic_only'] = True
    return planned
