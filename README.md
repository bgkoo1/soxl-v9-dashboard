# SOXL V12 Final · Quant Dashboard

V12 명세 1.0의 독립 엔진과 Streamlit 운용 대시보드입니다.
공식 진입점은 `streamlit_app.py`입니다. 이전 대시보드 진입점도 새 앱을 실행합니다.
새 앱은 이전 전략 엔진·대시보드를 불러오지 않습니다.

## 실행과 배포

```bash
python -m pip install -r requirements.txt
python -m streamlit run streamlit_app.py
```

Streamlit Community Cloud: 저장소 `bgkoo1/soxl-v9-dashboard`,
이 변경이 포함된 브랜치, Main file path `streamlit_app.py`, Python 3.12.
App settings의 Secrets에 아래 항목을 설정합니다. 실제 값은 코드에 넣지 않습니다.

```toml
DASHBOARD_PASSWORD = "운용자가 직접 정하는 긴 비밀번호"
GITHUB_REPO = "bgkoo1/soxl-v9-dashboard"
GITHUB_BRANCH = "main"
GITHUB_TOKEN = "해당 저장소 contents 읽기/쓰기 토큰"
PORTFOLIO_ENCRYPTION_KEY = "기존 V12 원장과 동일한 Fernet 키"
# 메리츠 조회는 기존 MERITZ_* Secrets 설정을 유지합니다.
```

실계좌 또는 저장 토큰 설정 시 로그인 후 화면을 엽니다. Community Cloud의
앱 접근 권한도 운용 목적에 맞게 설정하세요. 원장 `v12_manual_holdings.enc`는
기존 암호화 형식과 SHA 충돌 검사를 유지합니다. 읽기·복호화 오류 시 빈 원장으로
덮어쓰지 않습니다. Secrets가 없으면 빈 세션 모드로 실행합니다.
USD 예수금·평가환율은 직접 입력값이고 세션에서만 유지됩니다.
계좌 잔고·환율 자동 연동 또는 자동 주문 실행을 뜻하지 않습니다.

## 화면

- 오늘의 주문계획: 매도 우선순위, 매수 예산·참고수량, CSV.
- 실제 보유·체결: 매수 등록, 부분매도, 보유 수정, 잘못된 매도 취소·복구.
- 매매일지·입출금: 실제 기록과 JSON 내보내기. 입출금은 예수금에 자동 합산하지 않음.
- 백테스트: USD NAV, 낙폭, CAGR·MDD, 거래 기록.
- 전략·연결 상태: 고정 규칙과 메리츠 조회 전용 잔고 대조.

## V12 Final 1.0 규칙

- 최대 6슬롯, 전일 확정 NAV/6, 별도 강제 현금유보 없음.
- 기본 1슬롯 MOC + 전일 종가×0.91 급락 LOC 추가 1슬롯.
- D0=매수일. D2 종가 수익률≤−10% → D3 개별 MOC.
- D2 손절 발생 AND 해당일 Vol20≥0.10 → 신호일 보유 전 슬롯 다음 거래일 MOC.
- Vol20: 20개 일별 로그수익률 표본표준편차(ddof=1), 비연환산.
- 청산 순서: 전체 MOC → D2 개별 MOC → D7 MOC → 진입가×1.032 LOC.
- Risk B: 전일 MA200 괴리율<−15% AND −20%≤20일 모멘텀<−5%.
- Risk B 또는 직전 4거래일 이상 연속 하락이면 신규매수 중단. TB3 OFF.
- 당일 매도대금은 당일 매수 재원에서 제외.

연구는 USD 자본·USD 조정종가·소수점 수량·종가 전량 체결을 가정하며
비용·슬리피지를 미반영합니다. 첫 진입 전에 MA200 준비 기간을 요구합니다.
실제 원장과 연구 수익률은 별도입니다. 이전 엔진의 청산 순서를 수정했으므로
과거에 보고된 CAGR·MDD 수치를 강제로 재현하지 않습니다.

실제 신호는 입력한 체결가와 조정종가로 계산합니다. 분할·병합 전 명목 체결가를
그대로 입력하면 가격 기준이 달라지므로 기업행동이 있던 보유분은 별도로 대조해야 합니다.
증권사의 주문 접수·미체결·분할 체결을 자동 추적하지 않습니다. 미체결 강제청산은
실제 매도 기록 전까지 대기로 남습니다. 매수 계획은 예정 매도 슬롯을 미리 비우지
않으며 MOC 참고수량은 확정 종가 기준 추정치입니다.

NYSE 캘린더로 확정 종가일·다음 주문일을 계산합니다. 가격이 지연되면 주문계획을
숨깁니다. GitHub Actions는 평일 한국시간 08:35·09:35 업데이트를 시도합니다.
GitHub 스케줄 지연에 대비해 화면의 데이터 기준일을 확인하세요.

## 검증

```bash
python -m unittest discover -s tests -p 'test_rebuild*.py' -v
python -m unittest discover -s tests -p 'test_v12_manual_holdings.py' -v
python -m unittest discover -s tests -p 'test_v12_trade_management.py' -v
```
