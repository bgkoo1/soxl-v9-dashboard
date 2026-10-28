"""Read-only V12 actual-slot exit signals, never writes or transmits orders."""
from datetime import date
from decimal import Decimal
import math
import pandas as pd

def evaluate_actual_slot(slot,data):
    """Use trading-day close history only; never assume simulated exit == real fill."""
    entry=date.fromisoformat(slot["entry_date"])
    df=data[["Date","Close"]].copy()
    df["Date"]=pd.to_datetime(df["Date"]).dt.date
    df=df.sort_values("Date").drop_duplicates("Date")
    closes=df.loc[df["Date"]>entry,"Close"]
    elapsed=len(closes)
    last_close=float(df["Close"].iloc[-1])
    entry_price=float(Decimal(slot["entry_price"]))
    progress=last_close/entry_price-1
    if entry>df["Date"].iloc[-1]:
        return {"elapsed":0,"status":"매수일 이후 가격자료 없음","sell_watch":False,
                "last_return":progress,"reason":"데이터 확인"}
    if elapsed>=7:
        return {"elapsed":elapsed,"status":"D7 기한 경과 · 실제 보유 확인",
                "sell_watch":True,"last_return":progress,"reason":"D7 overdue"}
    if elapsed==6:
        return {"elapsed":elapsed,"status":"다음 거래일 D7 MOC 검토",
                "sell_watch":True,"last_return":progress,"reason":"D7 upcoming"}
    if elapsed==2 and progress<=-.10:
        lret=(pd.Series(df["Close"],dtype=float).pct_change()).dropna()
        logrets=pd.Series([math.log(float(df["Close"].iloc[i])/float(df["Close"].iloc[i-1]))
                           for i in range(1,len(df))])
        vol=float(logrets.tail(20).std(ddof=1)) if len(logrets)>=20 else None
        conditional=bool(vol is not None and vol>=.10)
        return {"elapsed":elapsed,"status":"D2 손절(-10%) · 다음 거래일 MOC 검토"+
                (" / 전체청산 조건 가능" if conditional else ""),
                "sell_watch":True,"last_return":progress,
                "reason":"D2 cut, next session","full_exit_condition":conditional}
    if progress>=.032:
        return {"elapsed":elapsed,"status":"LOC +3.2% 충족 · 실제 체결 확인",
                "sell_watch":True,"last_return":progress,"reason":"LOC eligible"}
    return {"elapsed":elapsed,"status":"LOC 매도 대기","sell_watch":False,
            "last_return":progress,"reason":"wait"}
