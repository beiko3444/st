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

    def cancel_warehousing(self, items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """PATCH /api/v1/warehousing/cancel/{cstCd} — 입고 요청 취소(가정 규격)."""
        return self.request(
            "PATCH", f"/api/v1/warehousing/cancel/{self.cst_cd}", body=list(items)
        )

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
        """주문 승인 시 출고(택배) 생성. POST /api/v1/delivery/parcel/{cstCd}"""
        return self.request("POST", f"/api/v1/delivery/parcel/{self.cst_cd}", body=list(items))

    def update_delivery_parcel(self, items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """PATCH /api/v1/delivery/parcel/{cstCd} — 출고 정보 수정."""
        return self.request("PATCH", f"/api/v1/delivery/parcel/{self.cst_cd}", body=list(items))

    def cancel_delivery(self, items: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        """PATCH /api/v1/delivery/cancel/{cstCd} — 출고 요청 취소."""
        return self.request("PATCH", f"/api/v1/delivery/cancel/{self.cst_cd}", body=list(items))

    def get_delivery_detail(self, slip_no: str) -> Dict[str, Any]:
        """GET /api/v1/delivery/detail/{cstCd}/{slipNo} — 전표번호 기준 출고 상세."""
        return self.request("GET", f"/api/v1/delivery/detail/{self.cst_cd}/{slip_no}")

    def get_delivery_parcel_list(
        self, start: str, end: str, out_div: str = "1"
    ) -> Dict[str, Any]:
        """GET /api/v1/delivery/parcel/{cstCd}/{start}/{end}/{outDiv} — 택배 출고 목록."""
        return self.request(
            "GET",
            f"/api/v1/delivery/parcel/{self.cst_cd}/{start}/{end}/{out_div}",
        )

    def get_delivery_good_detail_list(self, start: str, end: str) -> Dict[str, Any]:
        """GET /api/v1/delivery/good/detail/list/{cstCd}/{start}/{end} — 출고 상품 상세."""
        return self.request(
            "GET",
            f"/api/v1/delivery/good/detail/list/{self.cst_cd}/{start}/{end}",
        )


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
    # 확장 필드 (파스토 실응답 기준)
    godCd: Optional[str] = None
    godTypeNm: Optional[str] = None
    giftDivNm: Optional[str] = None
    cstNm: Optional[str] = None
    supCd: Optional[str] = None
    supNm: Optional[str] = None
    cateCd: Optional[str] = None
    cateNm: Optional[str] = None
    godPr: float = 0.0
    inPr: float = 0.0
    salPr: float = 0.0
    godWeight: float = 0.0
    godWidth: float = 0.0
    godLength: float = 0.0
    godHeight: float = 0.0
    boxInCnt: float = 0.0
    saleUnitQty: float = 0.0
    safetyStock: float = 0.0
    firstInDt: Optional[str] = None
    distTermMgtYn: Optional[str] = None
    useTermDay: Optional[str] = None
    outCanDay: Optional[str] = None
    origin: Optional[str] = None
    raw: Any = field(default=None, repr=False)


@dataclass
class FasstoStockRow:
    cstGodCd: str
    stockQty: float
    canStockQty: float
    badStockQty: float
    goodsSerialNo: Optional[str]
    # 확장 필드
    godCd: Optional[str] = None
    godNm: Optional[str] = None
    godBarcd: Optional[str] = None
    whCd: Optional[str] = None
    distTermDt: Optional[str] = None
    distTermMgtYn: Optional[str] = None
    giftDiv: Optional[str] = None
    supNm: Optional[str] = None
    slipNo: Optional[str] = None
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
    # 확장 필드
    outDt: Optional[str] = None
    outDivNm: Optional[str] = None
    mapSlipNo: Optional[str] = None
    whCd: Optional[str] = None
    whNm: Optional[str] = None
    shopCd: Optional[str] = None
    shopNm: Optional[str] = None
    salChanel: Optional[str] = None
    sku: float = 0.0
    ordQty: float = 0.0
    addGodOrdQty: float = 0.0
    outWay: Optional[str] = None
    outWayNm: Optional[str] = None
    ordDiv: Optional[str] = None
    custAddr: Optional[str] = None
    custTelNo: Optional[str] = None
    sendNm: Optional[str] = None
    sendTelNo: Optional[str] = None
    updUserNm: Optional[str] = None
    updTime: Optional[str] = None
    supCd: Optional[str] = None
    supNm: Optional[str] = None
    remark: Optional[str] = None
    raw: Any = field(default=None, repr=False)


@dataclass
class FasstoWarehousingRow:
    """입고(Warehousing) 리스트 정규화 — 실응답 wrkStat/supNm 기준."""

    slipNo: str
    ordDt: str
    ordNo: Optional[str]
    whCd: Optional[str]
    whNm: Optional[str]
    supCd: Optional[str]
    supNm: Optional[str]
    sku: float
    ordQty: float
    inQty: float
    tarQty: float
    inWay: Optional[str]
    inWayNm: Optional[str]
    parcelComp: Optional[str]
    parcelInvoiceNo: Optional[str]
    wrkStat: Optional[str]
    wrkStatNm: Optional[str]
    remark: Optional[str]
    raw: Any = field(default=None, repr=False)


@dataclass
class FasstoGoodsElementItem:
    cstGodCd: str
    godCd: Optional[str]
    godBarcd: Optional[str]
    godNm: Optional[str]
    godType: Optional[str]
    godTypeNm: Optional[str]
    qty: float


@dataclass
class FasstoGoodsElementRow:
    """세트/묶음상품 + 구성품 리스트."""

    cstGodCd: str
    godCd: Optional[str]
    godNm: Optional[str]
    useYn: Optional[str]
    elements: List[FasstoGoodsElementItem] = field(default_factory=list)
    raw: Any = field(default=None, repr=False)


@dataclass
class FasstoDeliveryParcelRow:
    """택배 출고 상세 (박스/송장/반품 포함)."""

    slipNo: str
    outOrdSlipNo: Optional[str]
    mapSlipNo: Optional[str]
    ordNo: Optional[str]
    ordSeq: Optional[str]
    packDt: Optional[str]
    boxDiv: Optional[str]
    boxDivNm: Optional[str]
    boxNm: Optional[str]
    boxNo: Optional[str]
    boxTp: Optional[str]
    crgSt: Optional[str]
    crgStNm: Optional[str]
    delayCd: Optional[str]
    delayNm: Optional[str]
    dlvMisYn: Optional[str]
    whCd: Optional[str]
    shopCd: Optional[str]
    shopNm: Optional[str]
    salChanel: Optional[str]
    cstNm: Optional[str]
    godCd: Optional[str]
    godNm: Optional[str]
    custNm: Optional[str]
    custAddr: Optional[str]
    custTelNo: Optional[str]
    invoiceNo: Optional[str]
    parcelCd: Optional[str]
    parcelNm: Optional[str]
    parcelLinkYn: Optional[str]
    packQty: float
    packSeq: Optional[str]
    pickSeq: Optional[str]
    printCnt: Optional[str]
    postYn: Optional[str]
    sku: float
    outDiv: Optional[str]
    outDivNm: Optional[str]
    shipReqTerm: Optional[str]
    rtnOrdDt: Optional[str]
    rtnAddr1: Optional[str]
    rtnAddr2: Optional[str]
    rtnTelNo: Optional[str]
    rtnZipCd: Optional[str]
    rtnCheck: Optional[str]
    rtnEmpNm: Optional[str]
    raw: Any = field(default=None, repr=False)


@dataclass
class FasstoDeliveryGoodDetailRow:
    """출고 상품 상세 — 매출/할인 단가 포함."""

    outDt: str
    slipNo: str
    outOrdSlipNo: Optional[str]
    productOrderNo: Optional[str]
    orderNo: Optional[str]
    ordDiv: Optional[str]
    invoiceNo: Optional[str]
    sellerChannel: Optional[str]
    custNm: Optional[str]
    godCd: Optional[str]
    cstGodCd: Optional[str]
    godDiv: Optional[str]
    godNm: Optional[str]
    outQty: float
    markedPrAmount: float
    sellingPrAmount: float
    dcAmount: float
    sellerDcAmount: float
    naverDcAmount: float
    raw: Any = field(default=None, repr=False)


WAREHOUSING_STATUS_NAMES = {
    "1": "입고요청",
    "2": "검수중",
    "3": "검수완료",
    "4": "입고완료",
    "5": "입고취소",
}


def warehousing_status_name(status_code: Any, status_name: Any = None) -> Optional[str]:
    name = _to_text(status_name)
    if name:
        return name
    code = _to_text(status_code)
    return WAREHOUSING_STATUS_NAMES.get(code) if code else None


def warehousing_cancel_check(status_code: Any, status_name: Any = None) -> Tuple[bool, str]:
    """Return whether an inbound slip is safe to send through the cancel flow."""
    code = _to_text(status_code)
    name = warehousing_status_name(code, status_name) or _to_text(status_name) or code or ""

    if code == "5" or "취소" in name:
        return False, "이미 입고취소 상태입니다."
    if code == "4" or "입고완료" in name:
        return False, "입고완료 상태는 취소할 수 없습니다."
    if code in {"2", "3"} or "검수" in name:
        return False, "검수 진행/완료 상태는 취소할 수 없습니다."
    if code == "1" or any(token in name for token in ("입고요청", "센터도착", "검수 전", "검수전")):
        return True, ""
    return False, f"현재 상태({name or '-'})는 앱에서 취소 가능 여부를 판단할 수 없습니다."


def _serial_to_text(value: Any) -> Optional[str]:
    """goodsSerialNo 가 list로 오는 케이스 대응."""
    if value is None:
        return None
    if isinstance(value, list):
        parts = [str(v).strip() for v in value if v is not None and str(v).strip()]
        return ", ".join(parts) if parts else None
    text = str(value).strip()
    return text or None


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
        # 파스토 실응답은 godBarcd. 과거 호환을 위해 barcode/barCd/godBarcode 도 허용.
        barcode = _to_text(
            _first_defined(
                row.get("godBarcd"),
                row.get("barcode"),
                row.get("barCd"),
                row.get("godBarcode"),
            )
        ) or None
        use_yn = _to_text(_first_defined(row.get("useYn"), row.get("use_yn"))) or None
        result.append(
            FasstoGoodsRow(
                cstGodCd=cst_god_cd,
                godNm=god_nm,
                godType=god_type,
                giftDiv=gift_div,
                barcode=barcode,
                useYn=use_yn,
                godCd=_to_text(row.get("godCd")) or None,
                godTypeNm=_to_text(row.get("godTypeNm")) or None,
                giftDivNm=_to_text(row.get("giftDivNm")) or None,
                cstNm=_to_text(row.get("cstNm")) or None,
                supCd=_to_text(row.get("supCd")) or None,
                supNm=_to_text(row.get("supNm")) or None,
                cateCd=_to_text(row.get("cateCd")) or None,
                cateNm=_to_text(row.get("cateNm")) or None,
                godPr=_to_number(row.get("godPr")),
                inPr=_to_number(row.get("inPr")),
                salPr=_to_number(row.get("salPr")),
                godWeight=_to_number(row.get("godWeight")),
                godWidth=_to_number(row.get("godWidth")),
                godLength=_to_number(row.get("godLength")),
                godHeight=_to_number(row.get("godHeight")),
                boxInCnt=_to_number(row.get("boxInCnt")),
                saleUnitQty=_to_number(row.get("saleUnitQty")),
                safetyStock=_to_number(row.get("safetyStock")),
                firstInDt=_to_text(row.get("firstInDt")) or None,
                distTermMgtYn=_to_text(row.get("distTermMgtYn")) or None,
                useTermDay=_to_text(row.get("useTermDay")) or None,
                outCanDay=_to_text(row.get("outCanDay")) or None,
                origin=_to_text(row.get("origin")) or None,
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
                goodsSerialNo=_serial_to_text(
                    _first_defined(row.get("goodsSerialNo"), row.get("goodsSerno"), row.get("goodsSerialNumber"))
                ),
                godCd=_to_text(row.get("godCd")) or None,
                godNm=_to_text(row.get("godNm")) or None,
                godBarcd=_to_text(_first_defined(row.get("godBarcd"), row.get("barcode"))) or None,
                whCd=_to_text(row.get("whCd")) or None,
                distTermDt=_to_text(row.get("distTermDt")) or None,
                distTermMgtYn=_to_text(row.get("distTermMgtYn")) or None,
                giftDiv=_to_text(row.get("giftDiv")) or None,
                supNm=_to_text(row.get("supNm")) or None,
                slipNo=_to_text(row.get("slipNo")) or None,
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
        # 파스토 실응답은 wrkStat/wrkStatNm. status/crgSt 도 과거 호환으로 유지.
        status_code = _to_text(
            _first_defined(row.get("wrkStat"), row.get("status"), row.get("crgSt"), "")
        )
        status_name = _to_text(
            _first_defined(row.get("wrkStatNm"), row.get("statusNm"), row.get("crgStNm"), row.get("statusName"))
        )
        result.append(
            FasstoDeliveryRow(
                slipNo=slip_no,
                ordNo=ord_no,
                ordDt=_to_text(_first_defined(row.get("ordDt"), row.get("orderDate"), row.get("outReqDt"))),
                status=status_code,
                statusNm=status_name,
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
                outDt=_to_text(row.get("outDt")) or None,
                outDivNm=_to_text(row.get("outDivNm")) or None,
                mapSlipNo=_to_text(row.get("mapSlipNo")) or None,
                whCd=_to_text(row.get("whCd")) or None,
                whNm=_to_text(row.get("whNm")) or None,
                shopCd=_to_text(row.get("shopCd")) or None,
                shopNm=_to_text(row.get("shopNm")) or None,
                salChanel=_to_text(row.get("salChanel")) or None,
                sku=_to_number(row.get("sku")),
                ordQty=_to_number(row.get("ordQty")),
                addGodOrdQty=_to_number(row.get("addGodOrdQty")),
                outWay=_to_text(row.get("outWay")) or None,
                outWayNm=_to_text(row.get("outWayNm")) or None,
                ordDiv=_to_text(row.get("ordDiv")) or None,
                custAddr=_to_text(row.get("custAddr")) or None,
                custTelNo=_to_text(row.get("custTelNo")) or None,
                sendNm=_to_text(row.get("sendNm")) or None,
                sendTelNo=_to_text(row.get("sendTelNo")) or None,
                updUserNm=_to_text(row.get("updUserNm")) or None,
                updTime=_to_text(row.get("updTime")) or None,
                supCd=_to_text(row.get("supCd")) or None,
                supNm=_to_text(row.get("supNm")) or None,
                remark=_to_text(row.get("remark")) or None,
                raw=row,
            )
        )
    return result


def normalize_fassto_warehousings(
    rows: Iterable[Mapping[str, Any]],
) -> List[FasstoWarehousingRow]:
    """입고(Warehousing) 응답 정규화 — 실응답 wrkStat/supNm 기준."""
    result: List[FasstoWarehousingRow] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        slip_no = _to_text(_first_defined(row.get("slipNo"), row.get("fmsSlipNo")))
        if not slip_no:
            continue
        result.append(
            FasstoWarehousingRow(
                slipNo=slip_no,
                ordDt=_to_text(_first_defined(row.get("ordDt"), row.get("inDt"))),
                ordNo=_to_text(row.get("ordNo")) or None,
                whCd=_to_text(row.get("whCd")) or None,
                whNm=_to_text(row.get("whNm")) or None,
                supCd=_to_text(row.get("supCd")) or None,
                supNm=_to_text(row.get("supNm")) or None,
                sku=_to_number(row.get("sku")),
                ordQty=_to_number(row.get("ordQty")),
                inQty=_to_number(row.get("inQty")),
                tarQty=_to_number(row.get("tarQty")),
                inWay=_to_text(row.get("inWay")) or None,
                inWayNm=_to_text(row.get("inWayNm")) or None,
                parcelComp=_to_text(row.get("parcelComp")) or None,
                parcelInvoiceNo=_to_text(row.get("parcelInvoiceNo")) or None,
                wrkStat=_to_text(_first_defined(row.get("wrkStat"), row.get("status"), row.get("crgSt")))
                or None,
                wrkStatNm=warehousing_status_name(
                    _first_defined(row.get("wrkStat"), row.get("status"), row.get("crgSt")),
                    _first_defined(row.get("wrkStatNm"), row.get("statusNm"), row.get("crgStNm")),
                ),
                remark=_to_text(row.get("remark")) or None,
                raw=row,
            )
        )
    return result


def normalize_fassto_goods_elements(
    rows: Iterable[Mapping[str, Any]],
) -> List[FasstoGoodsElementRow]:
    """세트/묶음상품 구성 리스트 정규화."""
    result: List[FasstoGoodsElementRow] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        cst_god_cd = _to_text(_first_defined(row.get("cstGodCd"), row.get("godCd")))
        if not cst_god_cd:
            continue
        elements_raw = row.get("elementList") or row.get("elements") or []
        elements: List[FasstoGoodsElementItem] = []
        if isinstance(elements_raw, list):
            for item in elements_raw:
                if not isinstance(item, Mapping):
                    continue
                child_cst = _to_text(_first_defined(item.get("cstGodCd"), item.get("godCd")))
                if not child_cst:
                    continue
                elements.append(
                    FasstoGoodsElementItem(
                        cstGodCd=child_cst,
                        godCd=_to_text(item.get("godCd")) or None,
                        godBarcd=_to_text(_first_defined(item.get("godBarcd"), item.get("barcode")))
                        or None,
                        godNm=_to_text(item.get("godNm")) or None,
                        godType=_to_text(item.get("godType")) or None,
                        godTypeNm=_to_text(item.get("godTypeNm")) or None,
                        qty=_to_number(item.get("qty")),
                    )
                )
        result.append(
            FasstoGoodsElementRow(
                cstGodCd=cst_god_cd,
                godCd=_to_text(row.get("godCd")) or None,
                godNm=_to_text(row.get("godNm")) or None,
                useYn=_to_text(row.get("useYn")) or None,
                elements=elements,
                raw=row,
            )
        )
    return result


def normalize_fassto_delivery_parcels(
    rows: Iterable[Mapping[str, Any]],
) -> List[FasstoDeliveryParcelRow]:
    """택배 출고 상세 (박스·송장·반품정보)."""
    result: List[FasstoDeliveryParcelRow] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        slip_no = _to_text(_first_defined(row.get("slipNo"), row.get("outOrdSlipNo")))
        if not slip_no:
            continue
        result.append(
            FasstoDeliveryParcelRow(
                slipNo=slip_no,
                outOrdSlipNo=_to_text(row.get("outOrdSlipNo")) or None,
                mapSlipNo=_to_text(row.get("mapSlipNo")) or None,
                ordNo=_to_text(row.get("ordNo")) or None,
                ordSeq=_to_text(row.get("ordSeq")) or None,
                packDt=_to_text(row.get("packDt")) or None,
                boxDiv=_to_text(row.get("boxDiv")) or None,
                boxDivNm=_to_text(row.get("boxDivNm")) or None,
                boxNm=_to_text(row.get("boxNm")) or None,
                boxNo=_to_text(row.get("boxNo")) or None,
                boxTp=_to_text(row.get("boxTp")) or None,
                crgSt=_to_text(row.get("crgSt")) or None,
                crgStNm=_to_text(row.get("crgStNm")) or None,
                delayCd=_to_text(row.get("delayCd")) or None,
                delayNm=_to_text(row.get("delayNm")) or None,
                dlvMisYn=_to_text(row.get("dlvMisYn")) or None,
                whCd=_to_text(row.get("whCd")) or None,
                shopCd=_to_text(row.get("shopCd")) or None,
                shopNm=_to_text(row.get("shopNm")) or None,
                salChanel=_to_text(row.get("salChanel")) or None,
                cstNm=_to_text(row.get("cstNm")) or None,
                godCd=_to_text(row.get("godCd")) or None,
                godNm=_to_text(row.get("godNm")) or None,
                custNm=_to_text(row.get("custNm")) or None,
                custAddr=_to_text(row.get("custAddr")) or None,
                custTelNo=_to_text(row.get("custTelNo")) or None,
                invoiceNo=_to_text(row.get("invoiceNo")) or None,
                parcelCd=_to_text(row.get("parcelCd")) or None,
                parcelNm=_to_text(row.get("parcelNm")) or None,
                parcelLinkYn=_to_text(row.get("parcelLinkYn")) or None,
                packQty=_to_number(row.get("packQty")),
                packSeq=_to_text(row.get("packSeq")) or None,
                pickSeq=_to_text(row.get("pickSeq")) or None,
                printCnt=_to_text(row.get("printCnt")) or None,
                postYn=_to_text(row.get("postYn")) or None,
                sku=_to_number(row.get("sku")),
                outDiv=_to_text(row.get("outDiv")) or None,
                outDivNm=_to_text(row.get("outDivNm")) or None,
                shipReqTerm=_to_text(row.get("shipReqTerm")) or None,
                rtnOrdDt=_to_text(row.get("rtnOrdDt")) or None,
                rtnAddr1=_to_text(row.get("rtnAddr1")) or None,
                rtnAddr2=_to_text(row.get("rtnAddr2")) or None,
                rtnTelNo=_to_text(row.get("rtnTelNo")) or None,
                rtnZipCd=_to_text(row.get("rtnZipCd")) or None,
                rtnCheck=_to_text(row.get("rtnCheck")) or None,
                rtnEmpNm=_to_text(row.get("rtnEmpNm")) or None,
                raw=row,
            )
        )
    return result


def normalize_fassto_delivery_good_details(
    rows: Iterable[Mapping[str, Any]],
) -> List[FasstoDeliveryGoodDetailRow]:
    """출고 상품 상세 — 매출/할인 단가 포함."""
    result: List[FasstoDeliveryGoodDetailRow] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        slip_no = _to_text(row.get("slipNo"))
        out_dt = _to_text(row.get("outDt"))
        if not slip_no and not out_dt:
            continue
        result.append(
            FasstoDeliveryGoodDetailRow(
                outDt=out_dt,
                slipNo=slip_no,
                outOrdSlipNo=_to_text(row.get("outOrdSlipNo")) or None,
                productOrderNo=_to_text(row.get("productOrderNo")) or None,
                orderNo=_to_text(row.get("orderNo")) or None,
                ordDiv=_to_text(row.get("ordDiv")) or None,
                invoiceNo=_to_text(row.get("invoiceNo")) or None,
                sellerChannel=_to_text(row.get("sellerChannel")) or None,
                custNm=_to_text(row.get("custNm")) or None,
                godCd=_to_text(row.get("godCd")) or None,
                cstGodCd=_to_text(row.get("cstGodCd")) or None,
                godDiv=_to_text(row.get("godDiv")) or None,
                godNm=_to_text(row.get("godNm")) or None,
                outQty=_to_number(row.get("outQty")),
                markedPrAmount=_to_number(row.get("markedPrAmount")),
                sellingPrAmount=_to_number(row.get("sellingPrAmount")),
                dcAmount=_to_number(row.get("dcAmount")),
                sellerDcAmount=_to_number(row.get("sellerDcAmount")),
                naverDcAmount=_to_number(row.get("naverDcAmount")),
                raw=row,
            )
        )
    return result


def summarize_delivery_good_details(
    rows: Sequence[FasstoDeliveryGoodDetailRow],
) -> Dict[str, float]:
    """매출·할인 합계 요약."""
    total_qty = 0.0
    gross_amount = 0.0
    selling_amount = 0.0
    discount_amount = 0.0
    seller_discount = 0.0
    naver_discount = 0.0
    for r in rows:
        total_qty += r.outQty
        gross_amount += r.markedPrAmount * r.outQty
        selling_amount += r.sellingPrAmount * r.outQty
        discount_amount += r.dcAmount * r.outQty
        seller_discount += r.sellerDcAmount * r.outQty
        naver_discount += r.naverDcAmount * r.outQty
    return {
        "rowCount": float(len(rows)),
        "totalQty": total_qty,
        "grossAmount": gross_amount,
        "sellingAmount": selling_amount,
        "discountAmount": discount_amount,
        "sellerDiscountAmount": seller_discount,
        "naverDiscountAmount": naver_discount,
    }


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


def build_warehousing_payload(source: Mapping[str, Any]) -> Dict[str, Any]:
    """입고 UI payload -> 파스토 WarehousingParam payload."""
    payload: Dict[str, Any] = {}
    for key in (
        "ordDt",
        "ordNo",
        "inWay",
        "slipNo",
        "parcelComp",
        "parcelInvoiceNo",
        "remark",
        "cstSupCd",
        "distTermDt",
        "makeDt",
        "preArv",
    ):
        value = source.get(key)
        if value not in (None, ""):
            payload[key] = value

    raw_goods = source.get("godCds")
    if raw_goods is None:
        raw_goods = source.get("goods")
    god_cds: List[Dict[str, Any]] = []
    if isinstance(raw_goods, Iterable) and not isinstance(raw_goods, (str, bytes, Mapping)):
        for item in raw_goods:
            if not isinstance(item, Mapping):
                continue
            cst_god_cd = str(item.get("cstGodCd") or "").strip()
            if not cst_god_cd:
                continue
            god_item: Dict[str, Any] = {"cstGodCd": cst_god_cd}
            if item.get("ordQty") not in (None, ""):
                god_item["ordQty"] = int(float(item.get("ordQty")))
            if item.get("distTermDt") not in (None, ""):
                god_item["distTermDt"] = item.get("distTermDt")
            god_cds.append(god_item)
    if god_cds:
        payload["godCds"] = god_cds
    return payload


def build_warehousing_statement_rows(source: Mapping[str, Any]) -> Dict[str, Any]:
    """입고 상세 payload에서 거래명세표 출력용 품목/합계를 추출한다."""
    goods = source.get("goods")
    if not isinstance(goods, list):
        goods = []

    rows: List[Dict[str, Any]] = []
    total_qty = 0
    for index, item in enumerate(goods, start=1):
        if not isinstance(item, Mapping):
            continue
        qty = int(_to_number(_first_defined(item.get("ordQty"), item.get("inQty"), 0)))
        total_qty += qty
        rows.append(
            {
                "no": index,
                "code": _to_text(item.get("cstGodCd")),
                "barcode": _to_text(
                    _first_defined(item.get("godBarcd"), item.get("barcode"), item.get("barCd"))
                ),
                "name": _to_text(item.get("godNm")),
                "qty": qty,
            }
        )
    return {"items": rows, "total_qty": total_qty}


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
    "FasstoWarehousingRow",
    "FasstoGoodsElementItem",
    "FasstoGoodsElementRow",
    "FasstoDeliveryParcelRow",
    "FasstoDeliveryGoodDetailRow",
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
    "normalize_fassto_warehousings",
    "normalize_fassto_goods_elements",
    "normalize_fassto_delivery_parcels",
    "normalize_fassto_delivery_good_details",
    "summarize_delivery_good_details",
    "warehousing_cancel_check",
    "warehousing_status_name",
    "normalize_product_code",
    "build_goods_payload",
    "build_warehousing_payload",
    "build_warehousing_statement_rows",
    "decide_goods_sync",
    "summarize_goods_sync",
    "compare_stock",
    "summarize_stock_compare",
    "run_goods_sync",
    "build_overview",
    "chunked",
]
