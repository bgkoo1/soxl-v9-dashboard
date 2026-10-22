"""SOXL V11 Hybrid Operations Dashboard — READ ONLY.
Independent Streamlit entrypoint. Never writes to GitHub or places orders.
"""
import base64
import json
from io import BytesIO
from datetime import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import requests
import streamlit as st
from cryptography.fernet import Fernet

st.set_page_config(page_title="SOXL V11 | 운용 워크스페이스",page_icon="📊",layout="wide")
st.markdown("""<style>
.block-container{padding-top:1.5rem;max-width:1500px}
div[data-testid="stMetric"]{border:1px solid #e2e8f0;border-radius:12px;padding:14px;background:#f8fafc}
div[data-testid="stDataFrame"]{border:1px solid #e2e8f0;border-radius:9px}
h1,h2,h3{letter-spacing:-.035em}
</style>""",unsafe_allow_html=True)
st.title("SOXL V11  ·  운용 워크스페이스")
st.caption("읽기 전용 · 기존 실전 기록 보존 · 실제 증권사 주문 및 잔고 연동 없음")

def read_saved():
    required=("GITHUB_TOKEN","PORTFOLIO_ENCRYPTION_KEY")
    if any(not st.secrets.get(k) for k in required):
        return None,"GitHub/암호화 Secrets가 없어 저장 기록을 읽지 못했습니다."
    repo=st.secrets.get("GITHUB_REPO","bgkoo1/soxl-v9-dashboard")
    branch=st.secrets.get("GITHUB_BRANCH","main")
    url=f"https://api.github.com/repos/{repo}/contents/live_portfolio.enc"
    try:
        r=requests.get(url,headers={"Authorization":f"Bearer {st.secrets['GITHUB_TOKEN']}",
                "Accept":"application/vnd.github+json"},params={"ref":branch},timeout=15)
        r.raise_for_status()
        encrypted=base64.b64decode(r.json()["content"])
        payload=json.loads(Fernet(str(st.secrets["PORTFOLIO_ENCRYPTION_KEY"]).encode()).decrypt(encrypted))
        if not isinstance(payload,dict):raise ValueError("Invalid saved payload")
        return payload,None
    except Exception:
        return None,"암호화된 거래기록을 읽지 못했습니다. Secrets와 GitHub 권한을 확인하세요."

def saved_table(value,columns=None):
    if isinstance(value,dict):
        rows=[{"슬롯/거래 키":k,**(v if isinstance(v,dict) else {"값":v})} for k,v in value.items()]
    elif isinstance(value,list):
        rows=[v if isinstance(v,dict) else {"값":v} for v in value]
    else:rows=[]
    return pd.DataFrame(rows)

@st.cache_data(ttl=60,show_spinner=False)
def load_prices(repo,branch,token):
    """Always fetch current GitHub CSV; never silently fall back to stale local file."""
    url=f"https://api.github.com/repos/{repo}/contents/SOXL_adjusted.csv"
    response=requests.get(
        url,
        headers={"Authorization":f"Bearer {token}","Accept":"application/vnd.github+json"},
        params={"ref":branch},
        timeout=20,
    )
    response.raise_for_status()
    body=response.json()
    if body.get("encoding")=="base64" and body.get("content"):
        raw=base64.b64decode(body["content"])
    elif body.get("download_url"):
        download=requests.get(body["download_url"],timeout=20)
        download.raise_for_status()
        raw=download.content
    else:
        raise ValueError("GitHub CSV response has no usable content")
    df=pd.read_csv(BytesIO(raw))
    for col in ("Date","Open","High","Low","Close","Volume"):
        if col not in df.columns:
            raise ValueError(f"Missing CSV column: {col}")
    df["Date"]=pd.to_datetime(df["Date"],errors="raise")
    for col in ("Open","High","Low","Close"):
        df[col]=pd.to_numeric(df[col],errors="raise")
    if df.empty or df["Date"].duplicated().any():
        raise ValueError("Empty or duplicate-date SOXL CSV")
    if (df[["Open","High","Low","Close"]]<=0).any().any():
        raise ValueError("Invalid nonpositive OHLC values")
    return df.sort_values("Date").reset_index(drop=True)

saved,error=read_saved()
try:
    prices=load_prices(
        st.secrets.get("GITHUB_REPO","bgkoo1/soxl-v9-dashboard"),
        st.secrets.get("GITHUB_BRANCH","main"),
        st.secrets.get("GITHUB_TOKEN",""),
    )
except Exception:
    st.error("GitHub 최신 SOXL 가격 데이터를 가져오지 못했습니다. 오래된 로컬 가격으로 대체하지 않습니다.")
    st.stop()
last=prices.iloc[-1]
if error:
    st.error(error)
    st.info("새 Streamlit 앱의 Settings → Secrets에 기존 앱의 GITHUB_TOKEN 및 PORTFOLIO_ENCRYPTION_KEY를 동일하게 등록하세요. 인증정보를 채팅에 보내지 마세요.")
else:
    st.success("암호화된 기존 거래기록 읽기 성공 · 저장 파일은 수정하지 않습니다.")
from data_updater import get_expected_latest_trading_day
expected=get_expected_latest_trading_day().date()
if pd.Timestamp(last["Date"]).date()<expected:
    st.error(f"가격 데이터 갱신 지연: 마지막 저장 {pd.Timestamp(last['Date']).date()} / 마지막 확정 예상 거래일 {expected}. 해당 데이터로 매매 판단 금지.")
st.caption("가격 데이터 출처: GitHub main 브랜치의 최신 SOXL_adjusted.csv · 최대 60초 캐시")
st.caption(f"SOXL 저장 일봉: {pd.Timestamp(last['Date']).date()} · 종가 ${float(last['Close']):,.4f} · 출처 검증 상태: 미확인")

q=(saved or {}).get("quantities",{})
q=q if isinstance(q,dict) else {}
valid_q={}
for k,v in q.items():
    try:
        n=int(v)
        if float(v)==n and n>=0:valid_q[k]=n
    except (TypeError,ValueError,OverflowError):pass
# Saved quantities are historical overrides, NOT a live holdings ledger.
# Never infer active slots from these keys without strategy-engine reconciliation.
held={}
historical_qty={k:v for k,v in valid_q.items() if v>0 and k not in set((saved or {}).get("excluded_positions",[]))}
manual=(saved or {}).get("manual_trades",[])
overrides=(saved or {}).get("trade_overrides",{})
flows=(saved or {}).get("cashflows",[])
if saved is None:
    st.warning("저장 기록 미연결: 아래 수치는 0이 아니라 확인 불가로 표시합니다.")
a,b,c,d=st.columns(4)
a.metric("현재 보유 슬롯","미확인",help="전략 엔진의 미청산 슬롯과 증권사 잔고 대조 전에는 표시하지 않습니다.")
b.metric("현재 보유 수량","미확인")
c.metric("저장된 수동 청산 기록",f"{len(manual) if isinstance(manual,list) else 0}건" if saved is not None else "확인 불가")
d.metric("저장된 체결 수정 기록",f"{len(overrides) if isinstance(overrides,dict) else 0}건" if saved is not None else "확인 불가")

tabs=st.tabs(["운용 현황","거래 원장","슬롯 관리","자동매매 관리"])
with tabs[0]:
    st.subheader("가격 추이")
    st.line_chart(prices.tail(90).set_index("Date")["Close"],height=270)
    if pd.Timestamp(last["Date"]).date()<expected:
        st.warning("위 차트는 GitHub에 저장된 지연된 데이터이며 최신 가격이 아닙니다.")
    st.subheader("보유 슬롯 요약")
    st.info("현재 보유 슬롯은 전략 엔진의 미청산 상태와 실제 증권사 잔고 대조 전까지 표시하지 않습니다.")
    st.caption(f"참고: 과거 수량 오버라이드 {len(historical_qty)}건은 현재 보유 수량이 아닙니다.")
    st.caption("예수금·총평가자산·실현손익은 계좌 조회 또는 체결원장 검증 전까지 표시하지 않습니다.")
with tabs[1]:
    st.subheader("거래 원장 · 저장 기록 조회")
    st.caption("암호화 파일에 저장된 사용자 기록만 표시합니다. 백테스트 가상 체결과 증권사 실제 체결을 혼합하지 않습니다.")
    source=st.radio("기록 유형",["수동 청산","체결가 수정","입출금"],horizontal=True)
    datasets={"수동 청산":manual,"체결가 수정":overrides,"입출금":flows}
    ledger=saved_table(datasets[source])
    if ledger.empty:
        st.info("해당 유형의 저장 기록이 없습니다.")
    else:
        date_columns=[c for c in ledger.columns if any(w in str(c).lower() for w in ("date","날짜","일시","time"))]
        search=st.text_input("원장 검색",placeholder="슬롯 ID, 날짜, 가격 등",key="ledger_search")
        if search.strip():
            matches=ledger.astype(str).apply(lambda col:col.str.contains(search.strip(),case=False,regex=False,na=False)).any(axis=1)
            ledger=ledger.loc[matches]
        if date_columns:
            date_col=st.selectbox("날짜 필터 기준",["전체"]+date_columns)
            if date_col!="전체":
                parsed=pd.to_datetime(ledger[date_col],errors="coerce")
                available=parsed.dropna()
                if not available.empty:
                    lower,upper=available.min().date(),available.max().date()
                    chosen=st.date_input("조회 기간",(lower,upper),min_value=lower,max_value=upper)
                    if isinstance(chosen,(list,tuple)) and len(chosen)==2:
                        ledger=ledger.loc[parsed.dt.date.between(chosen[0],chosen[1])]
        st.caption(f"검색 결과 {len(ledger):,}건 · 전체 원본 {len(saved_table(datasets[source])):,}건")
        st.dataframe(ledger,use_container_width=True,hide_index=True,height=420)
        st.download_button("현재 검색 결과 CSV 내려받기",ledger.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"SOXL_V11_{source}_readonly.csv",mime="text/csv")
with tabs[2]:
    st.subheader("V11 모델 슬롯 진단 · 실계좌 미검증")
    if saved is not None:
        try:
            from v11_hybrid_model_slots import reconstruct
            model_rows = reconstruct(prices, saved)
            st.dataframe(pd.DataFrame(model_rows), use_container_width=True, hide_index=True)
            st.caption("모델 미청산 슬롯은 실제 증권사 보유와 다를 수 있습니다.")
        except Exception as exc:
            st.warning(f"모델 재구성 보류: {type(exc).__name__}: {exc}")
    st.subheader("슬롯별 과거 저장 기록")
    st.warning("현재 보유 여부는 아직 확인되지 않았습니다. 아래는 저장된 과거 수량 기록이며 실계좌 보유 목록이 아닙니다.")
    entry=(saved or {}).get("entry_prices",{})
    entry=entry if isinstance(entry,dict) else {}
    excluded=(saved or {}).get("excluded_positions",[])
    excluded=set(excluded) if isinstance(excluded,list) else set()
    keys=sorted(set(valid_q)|set(entry))
    rows=[{
        "슬롯 키":key,
        "저장 수량(주)":valid_q.get(key),
        "저장 매수가($)":entry.get(key),
        "제외 기록":"있음" if key in excluded else "없음",
        "현재 보유 여부":"미검증",
    } for key in keys]
    history=pd.DataFrame(rows,columns=["슬롯 키","저장 수량(주)","저장 매수가($)","제외 기록","현재 보유 여부"])
    term=st.text_input("슬롯 키 검색",key="slot_search")
    if term.strip():
        history=history[history["슬롯 키"].astype(str).str.contains(term.strip(),case=False,regex=False)]
    st.caption(f"표시 {len(history):,}건 · 저장 키 전체 {len(keys):,}건")
    st.dataframe(history,use_container_width=True,hide_index=True,height=420)
    st.caption("현재 슬롯(최대 7개), D2/D3 및 TIME 상태는 V11 엔진 재구성과 실제 체결·잔고 대조를 마친 후 표시합니다.")
with tabs[3]:
    st.subheader("자동매매 안전 상태")
    st.warning("실거래 비활성화 · 기존 슬롯 정리 및 계좌 대조 전까지 자동주문 금지")
    st.write("메리츠 계좌 인증: **미검증**")
    st.write("실계좌 잔고 및 미체결 주문: **미조회**")
    st.write("SOXL 확정 종가 교차검증: **별도 확인 필요**")
    st.write("자동 주문 전송: **미구현 / 비활성화**")
    st.caption("이 앱에는 주문 API 호출, 주문 버튼, 저장 파일 수정 기능이 없습니다.")
