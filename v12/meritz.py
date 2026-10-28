"""Allowlisted Meritz account reads, based on the official REST catalog.

No broker writes except OAuth token issuance. No account payloads or credentials
are logged or persisted. A failed page never produces an empty success snapshot.
"""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import time
from zoneinfo import ZoneInfo
import requests
from v11_meritz_account_probe import _mac

BASE = "https://openapi.imeritz.com:9443"
PATHS = {
    "deposit": "/accounts/v1/me/deposit",
    "currencies": "/accounts/v1/me/overseas/currencies",
    "holdings": "/accounts/v1/me/overseas/holdings",
    "estimate": "/trading/v1/overseas/orders/estimate",
    "history": "/trading/v1/overseas/orders/history",
    "detail": "/trading/v1/overseas/orders/detail",
    "transactions": "/accounts/v1/me/transactions",
}
OK = {"0000", "0001", "5620", "5762", "5766", "5820", "5822"}


class ReadError(ValueError):
    pass


def amount(value, *, signed=False):
    try:
        if isinstance(value, bool) or value is None:
            raise ValueError()
        n = Decimal(str(value).replace(",", ""))
        if not n.is_finite() or (not signed and n < 0):
            raise ValueError()
        return n
    except (ValueError, InvalidOperation, TypeError):
        raise ReadError("계좌 응답의 금액·수량 형식을 확인할 수 없습니다.") from None


def symbol(row):
    return str(row.get("ovrs_stck_symb_iscd") or row.get("iscd") or "").strip().upper().split(".")[0]


class Client:
    def __init__(self, settings, session=None):
        self.key = str(settings.get("MERITZ_APP_KEY", "")).strip()
        self.secret = str(settings.get("MERITZ_APP_SECRET", "")).strip()
        try:
            self.mac = _mac(settings)
        except ValueError:
            raise ReadError("MERITZ_MAC_ADDRESS 설정이 필요합니다.") from None
        if not self.key or not self.secret:
            raise ReadError("MERITZ_APP_KEY·MERITZ_APP_SECRET 설정이 필요합니다.")
        self.session = session if session is not None else requests.Session()
        self.token, self.expiry = None, 0.
        self.last_request = 0.

    def _json(self, response, stage):
        try:
            body = response.json()
        except (ValueError, TypeError):
            raise ReadError(f"{stage}: JSON 응답을 확인할 수 없습니다.") from None
        if not response.ok or not isinstance(body, dict):
            raise ReadError(f"{stage}: 요청 실패 (HTTP {response.status_code}).")
        return body

    def _headers(self):
        if not self.token or time.monotonic() >= self.expiry:
            try:
                r = self.session.post(BASE + "/oauth2/token", data={
                    "grant_type": "client_credentials", "client_id": self.key,
                    "client_secret": self.secret, "scope": "login"},
                    headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=12)
                body = self._json(r, "인증")
                token = body.get("access_token")
                if not isinstance(token, str) or not token:
                    raise ReadError("인증: 접근토큰을 발급받지 못했습니다.")
                self.token = token
                ttl = float(body.get("expires_in", 300))
                self.expiry = time.monotonic() + max(0, min(ttl - 60, 3600))
            except requests.RequestException:
                raise ReadError("인증: 네트워크 연결 또는 시간 초과입니다.") from None
        return {"authorization": "Bearer " + self.token, "mac_address": self.mac,
                "User-Agent": "SOXL-V12-Account-Reader/1.0"}

    def get(self, name, params=None):
        if name not in PATHS:
            raise ReadError("허용되지 않은 조회입니다.")
        try:
            for attempt in range(2):
                headers = self._headers()
                delay = .12 - (time.monotonic() - self.last_request)
                if delay > 0:
                    time.sleep(delay)
                self.last_request = time.monotonic()
                response = self.session.get(BASE + PATHS[name], headers=headers, params=params, timeout=15)
                if attempt == 0 and response.status_code == 401:
                    self.token = None
                    continue
                body = self._json(response, name)
                if attempt == 0 and str(body.get("rsp_cd")) in {"EGW00121", "EGW00123"}:
                    self.token = None
                    continue
                break
        except requests.RequestException:
            raise ReadError(f"{name}: 네트워크 연결 또는 시간 초과입니다.") from None
        code = str(body.get("rsp_cd", ""))
        if code not in OK:
            # Never echo rsp_msg, which can contain account numbers or secrets.
            raise ReadError(f"{name}: 업무 응답이 정상 조회로 확인되지 않았습니다.")
        return body

    def rows(self, name, params=None, *, paginated=False):
        params, seen, out = dict(params or {}), set(), []
        for _ in range(200):
            body = self.get(name, params)
            rows = body.get("data")
            if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
                raise ReadError(f"{name}: 목록 자료 형식 오류입니다.")
            out.extend(rows)
            more = str(body.get("rsp_cd")) == "5762" or str(body.get("tr_cont", "0")) == "1"
            if not more:
                return out
            if not paginated:
                raise ReadError(f"{name}: 미지원 연속조회 응답입니다.")
            cursor = body.get("tr_cont_key")
            if not isinstance(cursor, str) or not cursor.strip() or cursor in seen:
                raise ReadError(f"{name}: 연속조회 키 누락·반복입니다.")
            seen.add(cursor)
            params.update(tr_cont="1", tr_cont_key=cursor)  # preserve trailing spaces
        raise ReadError(f"{name}: 조회 페이지 한도를 초과했습니다.")

    def snapshot(self, start="00000000", end=None):
        end = end or datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
        for value in (start, end):
            if value != "00000000":
                try:
                    datetime.strptime(value, "%Y%m%d")
                except (ValueError, TypeError):
                    raise ReadError("조회 기간 형식을 확인하세요.") from None
        if end == "00000000" or start > end:
            raise ReadError("조회 기간을 확인하세요.")
        dep = self.get("deposit").get("data")
        if not isinstance(dep, dict):
            raise ReadError("원화 예수금 응답 형식 오류입니다.")
        krw = amount(dep.get("oder_able_cash_amt"))
        currencies = self.rows("currencies")
        estimate = self.rows("estimate", {"intg_marg_iqry_yn": "N"})
        usd = [r for r in currencies if str(r.get("crcd")).upper() == "USD"]
        power = [r for r in estimate if str(r.get("crcd")).upper() == "USD"]
        if len(usd) != 1 or len(power) != 1:
            raise ReadError("USD 잔고·주문가능금액을 단일 통화 행으로 확인하지 못했습니다.")
        cash = amount(usd[0].get("frcr_amt"), signed=True)
        est_cash = amount(usd[0].get("estm_frcr_amt"), signed=True)
        fx = amount(usd[0].get("vltn_exrt"))
        if fx <= 0:
            raise ReadError("평가환율이 0입니다.")
        buying = amount(power[0].get("oder_crnc_oder_able_camt"), signed=True)
        reusable = amount(power[0].get("camt"))
        holdings = []
        for r in self.rows("holdings", paginated=True):
            ticker, qty = symbol(r), amount(r.get("rmnd_dqty"))
            if not ticker:
                raise ReadError("잔고 종목 식별값이 없습니다.")
            holdings.append({"symbol": ticker, "currency": str(r.get("crcd", "")).upper(),
                "quantity": str(qty), "sellable": str(amount(r.get("seln_able_dqty"))),
                "average_price": str(amount(r.get("avrg_unpr"))),
                "price": str(amount(r.get("psnt_prc"))),
                "value": str(amount(r.get("frcr_vltn_amt"))),
                "unrealized": str(amount(r.get("vltn_prls_camt"), signed=True))})
        history = self.rows("history", {"oder_sta_date": start, "oder_fin_date": end,
            "seln_byov_clcd": "0", "cntg_cls_code": "0"}, paginated=True)
        order_keys, outstanding = {}, []
        for row in history:
            qty = amount(row.get("cntg_dqty"))
            remaining = amount(row.get("ncnt_odqt"))
            if remaining:
                outstanding.append({"symbol": symbol(row), "quantity": str(remaining),
                    "order_no": str(row.get("oder_no")), "order_date": str(row.get("oder_date"))})
            if qty > 0:
                k = (str(row.get("oder_date", "")), str(row.get("oder_no", "")))
                if not k[0] or not k[1] or k[1] == "0":
                    raise ReadError("체결 주문 식별값이 누락됐습니다.")
                order_keys[k] = max(qty, order_keys.get(k, Decimal(0)))
        if len(order_keys) > 2000:
            raise ReadError("체결 주문이 2,000건을 넘습니다. 조회 시작일을 좁히세요.")
        fills = {}
        for (d, no), minimum in order_keys.items():
            detail = self.rows("detail", {"oder_sta_date": d, "oder_no": no})
            order_sum = Decimal(0)
            order_seen = set()
            for r in detail:
                qty = amount(r.get("cntg_dqty"))
                if qty == 0:
                    continue
                ident = str(r.get("cntg_no", ""))
                side = str(r.get("seln_byov_clcd", ""))
                price = amount(r.get("cntg_crnc_prc"))
                stamp = str(r.get("cntg_ymdh", ""))
                if not ident.isdecimal() or int(ident) == 0 or not no.isdecimal() or side not in {"1", "2"} or price <= 0:
                    raise ReadError("체결 상세의 번호·매수매도·가격을 확인할 수 없습니다.")
                try:
                    datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    raise ReadError("체결 상세의 시각을 확인할 수 없습니다.") from None
                key = f"{d}:{no}:{ident}:{symbol(r)}:{side}"
                item = {"id": key, "order_key": f"{d}:{no}", "order_date": d,
                        "order_no": no, "execution_no": ident, "timestamp": stamp,
                        "symbol": symbol(r), "currency": str(r.get("crcd", "")).upper(),
                        "side": "BUY" if side == "2" else "SELL",
                        "quantity": str(qty), "price": str(price)}
                if key in fills and fills[key] != item:
                    raise ReadError("동일 체결번호의 내용이 서로 다릅니다.")
                fills[key] = item
                if key not in order_seen:
                    order_sum += qty
                    order_seen.add(key)
            if order_sum < minimum:
                raise ReadError("체결 상세 수량이 주문 이력보다 적습니다. 불완전 조회를 반영하지 않습니다.")
        transactions = self.rows("transactions", {"from": start, "to": end, "sort": "ASC"}, paginated=True)
        tx = [{k: r.get(k) for k in ("tr_date", "tran_srno", "smcd_name", "iscd", "cncl_yn",
                "byng_unpr", "if_tr_qty", "tr_amt", "frcr_tr_amt", "ovsc_deal_fee")}
              for r in transactions]
        return {"fetched_at": datetime.now(timezone.utc).isoformat(), "start": start, "end": end,
                "cash_usd": str(cash), "estimated_cash_usd": str(est_cash),
                "buying_power_usd": str(buying), "reusable_usd": str(reusable),
                "fx": str(fx), "krw_buying_power": str(krw), "holdings": holdings,
                "fills": sorted(fills.values(), key=lambda f: (f["timestamp"], f["order_no"], f["execution_no"])),
                "outstanding": outstanding, "transactions": tx}
