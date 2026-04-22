"""
FASSTO Fulfillment API Connector (Python port)

가이드: fassto-fulfillment-mvp-porting-guide.md
원본: Next.js/TypeScript -> 이 파일은 Python/httpx 기반 동등 구현

주요 기능:
- 토큰 발급 + 캐시 (만료 1분 전 재발급)
- 공통 요청 (비즈니스 에러 + HTTP 에러 통합 처리)
- 401/INVALID_ACCESS 시 토큰 캐시 초기화
- Goods (상품) CRUD
- Stock (재고) 조회
- Warehousing (입고) CRUD/상세
- Delivery (출고) 조회
- 응답 정규화 유틸 (goods/stock/delivery)
- 로컬 상품 vs 파스토 상품/재고 비교 동기화 판정
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import quote_plus

import httpx


DEFAULT_API_URL = "https://fmsapi.fassto.ai"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class FasstoApiError(RuntimeError):
    """파스토 API 에러 (HTTP 에러 + 비즈니스 에러 통합)"""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        path: str,
        method: str,
        error_code: Optional[str] = None,
        details: Optional[Sequence[Any]] = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.path = path
        self.method = method
        self.error_code = error_code
        self.details = list(details) if details else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error": str(self),
            "errorCode": self.error_code,
            "details": self.details,
            "path": self.path,
            "method": self.method,
            "status": self.status,
        }


# ---------------------------------------------------------------------------
# Envelope parsing
# ---------------------------------------------------------------------------


def _parse_expire_datetime(expr: str) -> datetime:
    """yyyyMMddHHmmss -> datetime (local)."""
    y, m, d = int(expr[0:4]), int(expr[4:6]), int(expr[6:8])
    h, mi, s = int(expr[8:10]), int(expr[10:12]), int(expr[12:14])
    return datetime(y, m, d, h, mi, s)


def _resolve_error_message(
    method: str, path: str, status: int, json: Optional[Mapping[str, Any]]
) -> str:
    if isinstance(json, Mapping):
        err = json.get("errorInfo")
        if isinstance(err, Mapping):
            msg = err.get("errorMessage")
            if isinstance(msg, str) and msg.strip():
                return msg
        header = json.get("header")
        if isinstance(header, Mapping):
            hmsg = header.get("msg")
            if isinstance(hmsg, str) and hmsg.strip():
                return hmsg
    return f"Fassto API 오류 ({method} {path}, status {status})"


# ---------------------------------------------------------------------------
# Connector
# ---------------------------------------------------------------------------


class FasstoConnector:
    """파스토 풀필먼트 API 클라이언트.

    환경변수 대신 생성자 인자로 설정을 받습니다.
    (프로젝트의 다른 connectors와 동일한 패턴)
    """

    def __init__(
        self,
        api_cd: str,
        api_key: str,
        cst_cd: str,
        *,
        api_url: str = DEFAULT_API_URL,
        timeout_seconds: int = 30,
    ) -> None:
        self.api_cd = (api_cd or "").strip()
        self.api_key = (api_key or "").strip()
        self.cst_cd = (cst_cd or "").strip()
        self.api_url = (api_url or DEFAULT_API_URL).strip().rstrip("/")
        self.timeout_seconds = max(3, int(timeout_seconds))

        self._client: Optional[httpx.Client] = None
        self._cached_token: Optional[str] = None
        self._token_expiry: Optional[datetime] = None
        self._token_lock = threading.Lock()

    # -- lifecycle -------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(base_url=self.api_url, timeout=self.timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> "FasstoConnector":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- config ---------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(self.api_url and self.api_cd and self.api_key and self.cst_cd)

    def config_summary(self) -> Dict[str, Any]:
        return {
            "apiUrl": self.api_url,
            "cstCd": self.cst_cd,
            "configured": self.is_configured(),
            "hasApiCd": bool(self.api_cd),
            "hasApiKey": bool(self.api_key),
            "hasCstCd": bool(self.cst_cd),
        }

    def _assert_configured(self) -> None:
        missing: List[str] = []
        if not self.api_url:
            missing.append("FASSTO_API_URL")
        if not self.api_cd:
            missing.append("FASSTO_API_CD")
        if not self.api_key:
            missing.append("FASSTO_API_KEY")
        if not self.cst_cd:
            missing.append("FASSTO_CST_CD")
        if missing:
            raise RuntimeError(f"FASSTO 설정이 비어 있습니다: {', '.join(missing)}")

    # -- token ----------------------------------------------------------

    def clear_token_cache(self) -> None:
        with self._token_lock:
            self._cached_token = None
            self._token_expiry = None

    def _is_token_valid(self) -> bool:
        if not self._cached_token or not self._token_expiry:
            return False
        # 만료 1분 전까지는 유효 판정
        return time.time() < (self._token_expiry.timestamp() - 60.0)

    def get_access_token(self) -> str:
        self._assert_configured()
        with self._token_lock:
            if self._is_token_valid():
                return self._cached_token  # type: ignore[return-value]

            path = (
                f"/api/v1/auth/connect?apiCd={quote_plus(self.api_cd)}"
                f"&apiKey={quote_plus(self.api_key)}"
            )
            try:
                response = self.client.post(path)
            except httpx.RequestError as exc:
                raise FasstoApiError(
                    f"파스토 인증 요청 실패: {exc}",
                    status=0,
                    path=path,
                    method="POST",
                ) from exc

            json_body = self._safe_json(response)
            data = json_body.get("data") if isinstance(json_body, Mapping) else None
            access_token = data.get("accessToken") if isinstance(data, Mapping) else None
            expire_str = data.get("expreDatetime") if isinstance(data, Mapping) else None

            if response.status_code >= 400 or not access_token or not expire_str:
                err_info = json_body.get("errorInfo") if isinstance(json_body, Mapping) else None
                raise FasstoApiError(
                    _resolve_error_message("POST", path, response.status_code, json_body),
                    status=response.status_code,
                    path=path,
                    method="POST",
                    error_code=(err_info or {}).get("errorCode") if isinstance(err_info, Mapping) else None,
                    details=(err_info or {}).get("errorData") if isinstance(err_info, Mapping) else None,
                )

            self._cached_token = str(access_token)
            try:
                self._token_expiry = _parse_expire_datetime(str(expire_str))
            except Exception:  # noqa: BLE001
                # 파싱 실패 시 보수적으로 30분만 유효
                self._token_expiry = datetime.fromtimestamp(time.time() + 1800)
            return self._cached_token

    # -- core request ---------------------------------------------------

    @staticmethod
    def _safe_json(response: httpx.Response) -> Optional[Dict[str, Any]]:
        text = response.text
        if not text:
            return None
        try:
            data = response.json()
            if isinstance(data, dict):
                return data
            return {"data": data}
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"Fassto 응답 JSON 파싱 실패: {text[:200]}"
            ) from exc

    def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
    ) -> Dict[str, Any]:
        """공통 요청.

        - accessToken 자동 주입
        - HTTP 에러 + 비즈니스 에러(errorInfo.errorCode) 통합 처리
        - 401/INVALID_ACCESS 발생 시 토큰 캐시 초기화
        """
        self._assert_configured()
        token = self.get_access_token()

        headers = {
            "Content-Type": "application/json",
            "accessToken": token,
        }

        try:
            response = self.client.request(
                method.upper(),
                path,
                headers=headers,
                json=body if body is not None else None,
            )
        except httpx.RequestError as exc:
            raise FasstoApiError(
                f"파스토 요청 실패: {exc}",
                status=0,
                path=path,
                method=method.upper(),
            ) from exc

        json_body = self._safe_json(response)

        err_code: Optional[str] = None
        details: Optional[Sequence[Any]] = None
        if isinstance(json_body, Mapping):
            err = json_body.get("errorInfo")
            if isinstance(err, Mapping):
                err_code = err.get("errorCode") if isinstance(err.get("errorCode"), str) else None
                raw_details = err.get("errorData")
                if isinstance(raw_details, Sequence) and not isinstance(raw_details, (str, bytes)):
                    details = list(raw_details)

        has_business_error = bool(err_code)

        if response.status_code >= 400 or has_business_error:
            if response.status_code == 401 or err_code == "INVALID_ACCESS":
                self.clear_token_cache()

            raise FasstoApiError(
                _resolve_error_message(method.upper(), path, response.status_code, json_body),
                status=response.status_code,
                path=path,
                method=method.upper(),
                error_code=err_code,
                details=details,
            )

        return json_body or {}

    # -- high-level APIs ------------------------------------------------

    # Goods ----------------------------------------------------------
    def get_goods_list(self) -> Dict[str, Any]:
        return self.request("GET", f"/api/v1/goods/{self.cst_cd}")

    def create_goods(self, items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        return self.request("POST", f"/api/v1/goods/{self.cst_cd}", body=list(items))

    def update_goods(self, items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        return self.request("PATCH", f"/api/v1/goods/{self.cst_cd}", body=list(items))

    def get_goods_elements(self) -> Dict[str, Any]:
        return self.request("GET", f"/api/v1/goods/element/{self.cst_cd}")

    # Stock ----------------------------------------------------------
    def get_stock_list(self) -> Dict[str, Any]:
        return self.request("GET", f"/api/v1/stock/list/{self.cst_cd}")

    # Warehousing ---------------------------------------------------
    def create_warehousing(self, items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        return self.request("POST", f"/api/v1/warehousing/{self.cst_cd}", body=list(items))

    def update_warehousing(self, items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        return self.request("PATCH", f"/api/v1/warehousing/{self.cst_cd}", body=list(items))

    def get_warehousing_list(self, start: str, end: str) -> Dict[str, Any]:
        return self.request("GET", f"/api/v1/warehousing/{self.cst_cd}/{start}/{end}")

    def get_warehousing_detail(self, slip_no: str) -> Dict[str, Any]:
        return self.request("GET", f"/api/v1/warehousing/detail/{self.cst_cd}/{slip_no}")

    # Delivery ------------------------------------------------------
    def get_delivery_list(
        self,
        start: str,
        end: str,
        status: str = "ALL",
        out_div: str = "1",
    ) -> Dict[str, Any]:
        return self.request(
            "GET",
            f"/api/v1/delivery/{self.cst_cd}/{start}/{end}/{status}/{out_div}",
        )

    def create_delivery_parcel(self, items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """2차 확장: 주문 승인 시 출고(택배) 생성.

        가이드 12절 참고: POST /api/v1/delivery/parcel/{cstCd}
        """
        return self.request("POST", f"/api/v1/delivery/parcel/{self.cst_cd}", body=list(items))


# ---------------------------------------------------------------------------
# Normalization helpers (lib/fassto-data.ts 동등 구현)
# ---------------------------------------------------------------------------


def _first_defined(*values: Any) -> Any:
    for v in values:
        if v is None:
            continue
        if isinstance(v, str) and not v.strip():
            continue
        return v
    return None


def _to_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _to_number(value: Any) -> float:
    if isinstance(value, (int, float)):
        try:
            return float(value) if value == value else 0.0  # NaN check
        except Exception:  # noqa: BLE001
            return 0.0
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0.0


def extract_fassto_list(data: Any) -> List[Any]:
    """응답 envelope에서 리스트 추출.

    data 자체가 리스트일 수도 있고, {data: [...]} 혹은
    {data: {list/rows/items/contents/content/result/data: [...]}} 형태일 수도 있음.
    """
    if isinstance(data, list):
        return data
    if isinstance(data, Mapping):
        # envelope 가 통째로 들어온 경우 먼저 data 필드를 들여다본다
        inner = data.get("data") if "data" in data else None
        if isinstance(inner, list):
            return inner
        target: Mapping[str, Any] = inner if isinstance(inner, Mapping) else data
        for key in ("list", "rows", "items", "contents", "content", "result", "data"):
            value = target.get(key)
            if isinstance(value, list):
                return value
        for value in target.values():
            if isinstance(value, list):
                return value
    return []


@dataclass
class FasstoGoodsRow:
    cstGodCd: str
    godNm: str
    godType: str
    giftDiv: str
    barcode: Optional[str]
    useYn: Optional[str]
    raw: Any = field(default=None, repr=False)


@dataclass
class FasstoStockRow:
    cstGodCd: str
    stockQty: float
    canStockQty: float
    badStockQty: float
    goodsSerialNo: Optional[str]
    raw: Any = field(default=None, repr=False)


@dataclass
class FasstoDeliveryRow:
    slipNo: str
    ordNo: str
    ordDt: str
    status: str
    statusNm: str
    outDiv: str
    custNm: str
    invoiceNo: Optional[str]
    parcelCd: Optional[str]
    parcelNm: Optional[str]
    raw: Any = field(default=None, repr=False)


def normalize_fassto_goods(rows: Iterable[Mapping[str, Any]]) -> List[FasstoGoodsRow]:
    result: List[FasstoGoodsRow] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        cst_god_cd = _to_text(
            _first_defined(row.get("cstGodCd"), row.get("godCd"), row.get("goodsCd"), row.get("itemCd"))
        )
        if not cst_god_cd:
            continue
        god_nm = _to_text(
            _first_defined(row.get("godNm"), row.get("goodsNm"), row.get("itemNm"), row.get("godName"))
        )
        god_type = _to_text(_first_defined(row.get("godType"), row.get("goodsType"), "1")) or "1"
        gift_div = _to_text(_first_defined(row.get("giftDiv"), "01")) or "01"
        barcode = _to_text(_first_defined(row.get("barcode"), row.get("barCd"), row.get("godBarcode"))) or None
        use_yn = _to_text(_first_defined(row.get("useYn"), row.get("use_yn"))) or None
        result.append(
            FasstoGoodsRow(
                cstGodCd=cst_god_cd,
                godNm=god_nm,
                godType=god_type,
                giftDiv=gift_div,
                barcode=barcode,
                useYn=use_yn,
                raw=row,
            )
        )
    return result


def normalize_fassto_stocks(rows: Iterable[Mapping[str, Any]]) -> List[FasstoStockRow]:
    result: List[FasstoStockRow] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        cst_god_cd = _to_text(
            _first_defined(row.get("cstGodCd"), row.get("godCd"), row.get("goodsCd"), row.get("itemCd"))
        )
        if not cst_god_cd:
            continue
        result.append(
            FasstoStockRow(
                cstGodCd=cst_god_cd,
                stockQty=_to_number(
                    _first_defined(row.get("stockQty"), row.get("stockQnt"), row.get("stock"), 0)
                ),
                canStockQty=_to_number(
                    _first_defined(row.get("canStockQty"), row.get("canStockQnt"), row.get("canStock"), 0)
                ),
                badStockQty=_to_number(
                    _first_defined(row.get("badStockQty"), row.get("badStockQnt"), row.get("badStock"), 0)
                ),
                goodsSerialNo=_to_text(
                    _first_defined(row.get("goodsSerialNo"), row.get("goodsSerno"), row.get("goodsSerialNumber"))
                )
                or None,
                raw=row,
            )
        )
    return result


def normalize_fassto_deliveries(rows: Iterable[Mapping[str, Any]]) -> List[FasstoDeliveryRow]:
    result: List[FasstoDeliveryRow] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        slip_no = _to_text(_first_defined(row.get("slipNo"), row.get("fmsSlipNo"), row.get("outReqNo")))
        ord_no = _to_text(_first_defined(row.get("ordNo"), row.get("orderNo"), row.get("custOrdNo")))
        if not slip_no and not ord_no:
            continue
        result.append(
            FasstoDeliveryRow(
                slipNo=slip_no,
                ordNo=ord_no,
                ordDt=_to_text(_first_defined(row.get("ordDt"), row.get("orderDate"), row.get("outReqDt"))),
                status=_to_text(_first_defined(row.get("status"), row.get("crgSt"), row.get("wrkStat"), "")),
                statusNm=_to_text(_first_defined(row.get("statusNm"), row.get("crgStNm"), row.get("statusName"))),
                outDiv=_to_text(_first_defined(row.get("outDiv"), row.get("deliveryDiv"), row.get("outType"))),
                custNm=_to_text(_first_defined(row.get("custNm"), row.get("customerName"), row.get("recvNm"))),
                invoiceNo=_to_text(
                    _first_defined(row.get("invoiceNo"), row.get("parcelInvoiceNo"), row.get("waybillNo"))
                )
                or None,
                parcelCd=_to_text(_first_defined(row.get("parcelCd"), row.get("deliveryCd"))) or None,
                parcelNm=_to_text(
                    _first_defined(row.get("parcelNm"), row.get("deliveryNm"), row.get("parcelComp"))
                )
                or None,
                raw=row,
            )
        )
    return result


# ---------------------------------------------------------------------------
# Sync decision helpers (lib/fassto-sync.ts 동등 구현)
# ---------------------------------------------------------------------------


SYNC_STATUS_MISSING_CODE = "MISSING_CODE"
SYNC_STATUS_DUPLICATE_CODE = "DUPLICATE_CODE"
SYNC_STATUS_CREATE = "CREATE"
SYNC_STATUS_UPDATE = "UPDATE"
SYNC_STATUS_SYNCED = "SYNCED"


def normalize_product_code(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper()
    return text or None


def _desired_use_yn(product: Mapping[str, Any]) -> str:
    return "Y" if product.get("wholesaleAvailable") else "N"


def build_goods_payload(product: Mapping[str, Any]) -> Dict[str, Any]:
    """로컬 상품 -> 파스토 goods 등록/수정 payload."""
    code = normalize_product_code(product.get("productCode"))
    if not code:
        raise ValueError(f"상품코드가 없어 파스토로 보낼 수 없습니다: {product.get('name')}")

    barcode = str(product.get("barcode") or "").strip() or None
    payload: Dict[str, Any] = {
        "cstGodCd": code,
        "godNm": str(product.get("name") or "").strip(),
        "giftDiv": "01",
        "godType": "1",
        "useYn": _desired_use_yn(product),
    }
    if barcode:
        payload["barcode"] = barcode
    return payload


@dataclass
class GoodsSyncDecision:
    product: Mapping[str, Any]
    code: Optional[str]
    status: str  # MISSING_CODE | DUPLICATE_CODE | CREATE | UPDATE | SYNCED
    remote: Optional[FasstoGoodsRow] = None
    diff: Dict[str, Tuple[Any, Any]] = field(default_factory=dict)


def _count_duplicate_codes(products: Iterable[Mapping[str, Any]]) -> Dict[str, int]:
    counter: Dict[str, int] = {}
    for product in products:
        code = normalize_product_code(product.get("productCode"))
        if not code:
            continue
        counter[code] = counter.get(code, 0) + 1
    return counter


def decide_goods_sync(
    local_products: Sequence[Mapping[str, Any]],
    remote_goods: Sequence[FasstoGoodsRow],
) -> List[GoodsSyncDecision]:
    """로컬 상품 vs 파스토 상품 동기화 상태 판정."""
    dup_counts = _count_duplicate_codes(local_products)
    remote_by_code = {row.cstGodCd.upper(): row for row in remote_goods}

    decisions: List[GoodsSyncDecision] = []
    for product in local_products:
        code = normalize_product_code(product.get("productCode"))
        if not code:
            decisions.append(
                GoodsSyncDecision(product=product, code=None, status=SYNC_STATUS_MISSING_CODE)
            )
            continue
        if dup_counts.get(code, 0) > 1:
            decisions.append(
                GoodsSyncDecision(product=product, code=code, status=SYNC_STATUS_DUPLICATE_CODE)
            )
            continue

        remote = remote_by_code.get(code)
        if remote is None:
            decisions.append(
                GoodsSyncDecision(product=product, code=code, status=SYNC_STATUS_CREATE)
            )
            continue

        diff: Dict[str, Tuple[Any, Any]] = {}
        local_name = str(product.get("name") or "").strip()
        if local_name and remote.godNm and local_name != remote.godNm:
            diff["godNm"] = (remote.godNm, local_name)

        local_barcode = str(product.get("barcode") or "").strip() or None
        if (local_barcode or None) != (remote.barcode or None):
            diff["barcode"] = (remote.barcode, local_barcode)

        local_use_yn = _desired_use_yn(product)
        if (remote.useYn or "").upper() != local_use_yn:
            diff["useYn"] = (remote.useYn, local_use_yn)

        if diff:
            decisions.append(
                GoodsSyncDecision(
                    product=product,
                    code=code,
                    status=SYNC_STATUS_UPDATE,
                    remote=remote,
                    diff=diff,
                )
            )
        else:
            decisions.append(
                GoodsSyncDecision(
                    product=product,
                    code=code,
                    status=SYNC_STATUS_SYNCED,
                    remote=remote,
                )
            )
    return decisions


def summarize_goods_sync(decisions: Sequence[GoodsSyncDecision]) -> Dict[str, int]:
    summary = {
        "totalProducts": len(decisions),
        "createCount": 0,
        "updateCount": 0,
        "syncedCount": 0,
        "missingCodeCount": 0,
        "duplicateCodeCount": 0,
    }
    for d in decisions:
        if d.status == SYNC_STATUS_CREATE:
            summary["createCount"] += 1
        elif d.status == SYNC_STATUS_UPDATE:
            summary["updateCount"] += 1
        elif d.status == SYNC_STATUS_SYNCED:
            summary["syncedCount"] += 1
        elif d.status == SYNC_STATUS_MISSING_CODE:
            summary["missingCodeCount"] += 1
        elif d.status == SYNC_STATUS_DUPLICATE_CODE:
            summary["duplicateCodeCount"] += 1
    return summary


# ---- Stock compare --------------------------------------------------------


STOCK_STATUS_MATCH = "MATCH"
STOCK_STATUS_MISMATCH = "MISMATCH"
STOCK_STATUS_NOT_REGISTERED = "NOT_REGISTERED"
STOCK_STATUS_NO_STOCK_ROW = "NO_STOCK_ROW"
STOCK_STATUS_MISSING_CODE = "MISSING_CODE"
STOCK_STATUS_DUPLICATE_CODE = "DUPLICATE_CODE"


@dataclass
class StockCompareRow:
    product: Mapping[str, Any]
    code: Optional[str]
    localStock: float
    remoteCanStock: Optional[float]
    status: str
    diff: Optional[float] = None


def compare_stock(
    local_products: Sequence[Mapping[str, Any]],
    remote_goods: Sequence[FasstoGoodsRow],
    remote_stocks: Sequence[FasstoStockRow],
) -> List[StockCompareRow]:
    """로컬 재고 vs 파스토 canStockQty 비교.

    전제: 재고 비교 기준은 `canStockQty` (가이드 11절).
    """
    dup_counts = _count_duplicate_codes(local_products)
    goods_codes = {row.cstGodCd.upper() for row in remote_goods}
    stock_by_code = {row.cstGodCd.upper(): row for row in remote_stocks}

    rows: List[StockCompareRow] = []
    for product in local_products:
        local_stock = _to_number(product.get("stock"))
        code = normalize_product_code(product.get("productCode"))
        if not code:
            rows.append(
                StockCompareRow(
                    product=product,
                    code=None,
                    localStock=local_stock,
                    remoteCanStock=None,
                    status=STOCK_STATUS_MISSING_CODE,
                )
            )
            continue
        if dup_counts.get(code, 0) > 1:
            rows.append(
                StockCompareRow(
                    product=product,
                    code=code,
                    localStock=local_stock,
                    remoteCanStock=None,
                    status=STOCK_STATUS_DUPLICATE_CODE,
                )
            )
            continue
        if code not in goods_codes:
            rows.append(
                StockCompareRow(
                    product=product,
                    code=code,
                    localStock=local_stock,
                    remoteCanStock=None,
                    status=STOCK_STATUS_NOT_REGISTERED,
                )
            )
            continue
        stock_row = stock_by_code.get(code)
        if stock_row is None:
            rows.append(
                StockCompareRow(
                    product=product,
                    code=code,
                    localStock=local_stock,
                    remoteCanStock=None,
                    status=STOCK_STATUS_NO_STOCK_ROW,
                )
            )
            continue
        can_stock = stock_row.canStockQty
        diff = local_stock - can_stock
        status = STOCK_STATUS_MATCH if diff == 0 else STOCK_STATUS_MISMATCH
        rows.append(
            StockCompareRow(
                product=product,
                code=code,
                localStock=local_stock,
                remoteCanStock=can_stock,
                status=status,
                diff=diff,
            )
        )
    return rows


def summarize_stock_compare(rows: Sequence[StockCompareRow]) -> Dict[str, int]:
    summary = {
        "totalProducts": len(rows),
        "matchedCount": 0,
        "mismatchCount": 0,
        "notRegisteredCount": 0,
        "noStockRowCount": 0,
        "missingCodeCount": 0,
        "duplicateCodeCount": 0,
    }
    for r in rows:
        if r.status == STOCK_STATUS_MATCH:
            summary["matchedCount"] += 1
        elif r.status == STOCK_STATUS_MISMATCH:
            summary["mismatchCount"] += 1
        elif r.status == STOCK_STATUS_NOT_REGISTERED:
            summary["notRegisteredCount"] += 1
        elif r.status == STOCK_STATUS_NO_STOCK_ROW:
            summary["noStockRowCount"] += 1
        elif r.status == STOCK_STATUS_MISSING_CODE:
            summary["missingCodeCount"] += 1
        elif r.status == STOCK_STATUS_DUPLICATE_CODE:
            summary["duplicateCodeCount"] += 1
    return summary


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------


def chunked(items: Sequence[Any], size: int) -> Iterable[List[Any]]:
    if size <= 0:
        yield list(items)
        return
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def run_goods_sync(
    connector: FasstoConnector,
    decisions: Sequence[GoodsSyncDecision],
    *,
    chunk_size: int = 50,
) -> Dict[str, Any]:
    """가이드 8-3: CREATE/UPDATE 만 실행, 50건 단위 chunk, 실패 배치 응답 반환."""
    creates = [d for d in decisions if d.status == SYNC_STATUS_CREATE]
    updates = [d for d in decisions if d.status == SYNC_STATUS_UPDATE]

    create_results: List[Dict[str, Any]] = []
    update_results: List[Dict[str, Any]] = []
    failed_batches: List[Dict[str, Any]] = []

    for batch in chunked(creates, chunk_size):
        payload = [build_goods_payload(d.product) for d in batch]
        try:
            resp = connector.create_goods(payload)
            create_results.append({"count": len(payload), "response": resp})
        except FasstoApiError as exc:
            failed_batches.append(
                {
                    "op": "CREATE",
                    "count": len(payload),
                    "error": exc.to_dict(),
                    "codes": [p["cstGodCd"] for p in payload],
                }
            )

    for batch in chunked(updates, chunk_size):
        payload = [build_goods_payload(d.product) for d in batch]
        try:
            resp = connector.update_goods(payload)
            update_results.append({"count": len(payload), "response": resp})
        except FasstoApiError as exc:
            failed_batches.append(
                {
                    "op": "UPDATE",
                    "count": len(payload),
                    "error": exc.to_dict(),
                    "codes": [p["cstGodCd"] for p in payload],
                }
            )

    return {
        "createBatches": create_results,
        "updateBatches": update_results,
        "failedBatches": failed_batches,
        "summary": summarize_goods_sync(decisions),
    }


# ---------------------------------------------------------------------------
# Overview (가이드 8-1 동등 구현)
# ---------------------------------------------------------------------------


def build_overview(
    connector: FasstoConnector,
    local_products: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    """개요 응답 생성. 연결 실패해도 config/localSummary는 항상 반환."""
    config = connector.config_summary()

    # 로컬 요약
    dup_counts = _count_duplicate_codes(local_products)
    missing_code = 0
    with_code = 0
    low_stock = 0
    for p in local_products:
        code = normalize_product_code(p.get("productCode"))
        if code:
            with_code += 1
        else:
            missing_code += 1
        safety = _to_number(p.get("safetyStock"))
        if _to_number(p.get("stock")) <= safety:
            low_stock += 1

    local_summary = {
        "totalProducts": len(local_products),
        "productsWithCode": with_code,
        "productsMissingCode": missing_code,
        "lowStockProducts": low_stock,
        "duplicateProductCodes": sum(1 for c in dup_counts.values() if c > 1),
    }

    warnings = [
        "파스토 실운영 연결은 허용 고정 IP, api_cd, api_key, cstCd가 모두 맞아야 합니다."
    ]

    if not config["configured"]:
        return {
            "config": config,
            "connection": {"ok": False, "message": "환경설정이 비어 있습니다."},
            "localSummary": local_summary,
            "remoteSummary": None,
            "syncSummary": None,
            "stockSummary": None,
            "warnings": warnings,
        }

    try:
        goods_env = connector.get_goods_list()
        stock_env = connector.get_stock_list()
    except FasstoApiError as exc:
        return {
            "config": config,
            "connection": {"ok": False, "message": str(exc), "error": exc.to_dict()},
            "localSummary": local_summary,
            "remoteSummary": None,
            "syncSummary": None,
            "stockSummary": None,
            "warnings": warnings,
        }

    remote_goods = normalize_fassto_goods(extract_fassto_list(goods_env))
    remote_stocks = normalize_fassto_stocks(extract_fassto_list(stock_env))

    decisions = decide_goods_sync(local_products, remote_goods)
    stock_rows = compare_stock(local_products, remote_goods, remote_stocks)

    return {
        "config": config,
        "connection": {"ok": True, "message": "파스토 상품/재고 조회에 성공했습니다."},
        "localSummary": local_summary,
        "remoteSummary": {
            "goodsCount": len(remote_goods),
            "stockRows": len(remote_stocks),
        },
        "syncSummary": summarize_goods_sync(decisions),
        "stockSummary": summarize_stock_compare(stock_rows),
        "warnings": warnings,
    }


__all__ = [
    "DEFAULT_API_URL",
    "FasstoApiError",
    "FasstoConnector",
    "FasstoGoodsRow",
    "FasstoStockRow",
    "FasstoDeliveryRow",
    "GoodsSyncDecision",
    "StockCompareRow",
    "SYNC_STATUS_CREATE",
    "SYNC_STATUS_UPDATE",
    "SYNC_STATUS_SYNCED",
    "SYNC_STATUS_MISSING_CODE",
    "SYNC_STATUS_DUPLICATE_CODE",
    "STOCK_STATUS_MATCH",
    "STOCK_STATUS_MISMATCH",
    "STOCK_STATUS_NOT_REGISTERED",
    "STOCK_STATUS_NO_STOCK_ROW",
    "STOCK_STATUS_MISSING_CODE",
    "STOCK_STATUS_DUPLICATE_CODE",
    "extract_fassto_list",
    "normalize_fassto_goods",
    "normalize_fassto_stocks",
    "normalize_fassto_deliveries",
    "normalize_product_code",
    "build_goods_payload",
    "decide_goods_sync",
    "summarize_goods_sync",
    "compare_stock",
    "summarize_stock_compare",
    "run_goods_sync",
    "build_overview",
    "chunked",
]
