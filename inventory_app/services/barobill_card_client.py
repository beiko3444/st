"""바로빌 SOAP 카드 API 직접 호출.

참고: beico-app/lib/barobillCard.ts 의 호출 흐름을 Python 으로 포팅.

운영 서버: https://ws.baroservice.com/CARD.asmx
테스트 서버: https://testws.baroservice.com/CARD.asmx

주요 메소드:
- GetCardEx2: 등록된 카드번호 목록
- RefreshCard: 특정 카드 데이터 새로 받기
- GetPeriodCardApprovalLog: 기간별 카드승인내역 (메인)
- GetPeriodCardLogEx3: 레거시 (백업, Approval 응답에 금액 누락 시 보완)

인증:
- CERTKEY (인증키, 운영/테스트 환경별 다름)
- CorpNum (사업자번호, '-' 제거)
- ID (바로빌 계정 ID)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from inventory_app.models import CardUsage


PRODUCTION_URL = "https://ws.baroservice.com/CARD.asmx"
TEST_URL = "https://testws.baroservice.com/CARD.asmx"
NS = "http://ws.baroservice.com/"


class BarobillError(RuntimeError):
    def __init__(self, message: str, *, code: int = 0, action: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.action = action


def _escape_xml(s: str) -> str:
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _decode_xml(s: str) -> str:
    if not s:
        return ""
    return (
        s.replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&apos;", "'")
        .replace("&amp;", "&")
        .strip()
    )


def _to_ymd(value: str) -> str:
    raw = (value or "").strip()
    if re.match(r"^\d{8}$", raw):
        return raw
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw.replace("-", "")
    raise ValueError(f"날짜 형식 오류: {value} (YYYY-MM-DD 또는 YYYYMMDD)")


def _extract_tag(xml: str, tag: str) -> Optional[str]:
    pattern = rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>([\s\S]*?)</(?:\w+:)?{tag}>"
    m = re.search(pattern, xml, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _extract_blocks(xml: str, tag: str) -> List[str]:
    pattern = rf"<(?:\w+:)?{tag}(?:\s[^>]*)?>([\s\S]*?)</(?:\w+:)?{tag}>"
    return [m.group(1) for m in re.finditer(pattern, xml, re.IGNORECASE)]


def _to_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    text = str(value).replace(",", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return None
    try:
        return round(float(m.group(0)))
    except ValueError:
        return None


def _parse_use_dt(use_dt: str) -> Optional[str]:
    """YYYYMMDDHHMMSS / YYYYMMDD → ISO datetime 문자열."""
    raw = (use_dt or "").strip()
    if not raw:
        return None
    if re.match(r"^\d{14}$", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}T{raw[8:10]}:{raw[10:12]}:{raw[12:14]}"
    if re.match(r"^\d{8}$", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


@dataclass
class BarobillCardConfig:
    certkey: str
    corp_num: str
    user_id: str
    use_test: bool = False

    @property
    def soap_url(self) -> str:
        return TEST_URL if self.use_test else PRODUCTION_URL

    def is_valid(self) -> bool:
        return bool(self.certkey and self.corp_num and self.user_id)

    def missing_fields(self) -> List[str]:
        miss: List[str] = []
        if not self.certkey:
            miss.append("certkey")
        if not self.corp_num:
            miss.append("corp_num")
        if not self.user_id:
            miss.append("id")
        return miss


class BarobillCardClient:
    """바로빌 SOAP 카드 API 클라이언트."""

    def __init__(self, config: BarobillCardConfig, *, timeout_seconds: int = 60) -> None:
        self.config = config
        self.timeout_seconds = max(5, int(timeout_seconds))
        self._client: Optional[httpx.Client] = None

    @classmethod
    def from_app_config(cls, app_config: Any) -> "BarobillCardClient":
        cfg = BarobillCardConfig(
            certkey=str(getattr(app_config, "barobill_certkey", "") or "").strip(),
            corp_num=re.sub(r"[^0-9]", "", str(getattr(app_config, "barobill_corp_num", "") or "")),
            user_id=str(getattr(app_config, "barobill_user_id", "") or "").strip(),
            use_test=bool(getattr(app_config, "barobill_use_test", False)),
        )
        return cls(cfg, timeout_seconds=int(getattr(app_config, "timeout_seconds", 30) or 30))

    def is_configured(self) -> bool:
        return self.config.is_valid()

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None

    def __enter__(self) -> "BarobillCardClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ----- SOAP 호출 -----

    def _call(self, action: str, body_inner: str) -> str:
        if not self.is_configured():
            miss = ", ".join(self.config.missing_fields())
            raise BarobillError(
                f"바로빌 설정 누락: {miss}\n"
                "credentials.json 의 barobill 섹션에 certkey/corp_num/id 를 채우세요.",
                action=action,
            )

        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>\n'
            '<soap:Envelope xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" '
            'xmlns:xsd="http://www.w3.org/2001/XMLSchema" '
            'xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">\n'
            "  <soap:Body>\n"
            f"{body_inner}\n"
            "  </soap:Body>\n"
            "</soap:Envelope>"
        )
        try:
            resp = self.client.post(
                self.config.soap_url,
                content=envelope.encode("utf-8"),
                headers={
                    "Content-Type": "text/xml; charset=utf-8",
                    "SOAPAction": f"{NS}{action}",
                },
            )
        except httpx.RequestError as exc:
            raise BarobillError(f"바로빌 통신 실패: {exc}", action=action) from exc

        text = resp.text or ""
        if resp.status_code >= 400:
            raise BarobillError(
                f"바로빌 HTTP {resp.status_code}: {text[:300]}",
                code=resp.status_code, action=action,
            )
        fault = _extract_tag(text, "faultstring")
        if fault:
            raise BarobillError(f"SOAP Fault: {_decode_xml(fault)}", action=action)
        return text

    # ----- API: 카드 목록 -----

    def get_card_list(self, available_only: bool = True) -> List[str]:
        """등록된 카드번호 목록.

        - 응답이 단일 음수 코드(예: -10002)면 에러로 던짐
        """
        body = (
            f'<GetCardEx2 xmlns="{NS}">\n'
            f"  <CERTKEY>{_escape_xml(self.config.certkey)}</CERTKEY>\n"
            f"  <CorpNum>{_escape_xml(self.config.corp_num)}</CorpNum>\n"
            f"  <AvailOnly>{1 if available_only else 0}</AvailOnly>\n"
            f"</GetCardEx2>"
        )
        xml = self._call("GetCardEx2", body)
        result = _extract_tag(xml, "GetCardEx2Result") or ""

        # 결과 내 CardEx 블록들에서 CardNum 추출
        blocks = _extract_blocks(result, "CardEx")
        numbers: List[str] = []
        for blk in blocks:
            num = _decode_xml(_extract_tag(blk, "CardNum") or "")
            if num:
                numbers.append(num.strip())

        # 음수 코드 응답
        if numbers and re.match(r"^-\d{5}$", numbers[0]):
            raise BarobillError(
                f"GetCardEx2 실패: {numbers[0]}", code=int(numbers[0]), action="GetCardEx2"
            )
        # 중복 제거
        seen = []
        for n in numbers:
            if n and n not in seen:
                seen.append(n)
        return seen

    # ----- API: 카드 갱신 -----

    def refresh_card(self, card_num: str) -> int:
        body = (
            f'<RefreshCard xmlns="{NS}">\n'
            f"  <CERTKEY>{_escape_xml(self.config.certkey)}</CERTKEY>\n"
            f"  <CorpNum>{_escape_xml(self.config.corp_num)}</CorpNum>\n"
            f"  <ID>{_escape_xml(self.config.user_id)}</ID>\n"
            f"  <CardNum>{_escape_xml(card_num)}</CardNum>\n"
            f"</RefreshCard>"
        )
        xml = self._call("RefreshCard", body)
        result_text = _extract_tag(xml, "RefreshCardResult") or ""
        try:
            return int(result_text)
        except ValueError as exc:
            raise BarobillError(f"RefreshCard 결과 파싱 실패: {result_text}", action="RefreshCard") from exc

    # ----- API: 기간별 카드승인내역 -----

    def get_period_card_approval_log(
        self,
        *,
        card_num: str,
        start_date: str,
        end_date: str,
        count_per_page: int = 500,
        order_direction: int = 1,
    ) -> List[CardUsage]:
        sd = _to_ymd(start_date)
        ed = _to_ymd(end_date)
        cpp = max(1, min(1000, int(count_per_page)))
        cur = 1
        max_page = 1
        all_logs: List[CardUsage] = []

        while cur <= max_page:
            body = (
                f'<GetPeriodCardApprovalLog xmlns="{NS}">\n'
                f"  <CERTKEY>{_escape_xml(self.config.certkey)}</CERTKEY>\n"
                f"  <CorpNum>{_escape_xml(self.config.corp_num)}</CorpNum>\n"
                f"  <ID>{_escape_xml(self.config.user_id)}</ID>\n"
                f"  <CardNum>{_escape_xml(card_num)}</CardNum>\n"
                f"  <StartDate>{sd}</StartDate>\n"
                f"  <EndDate>{ed}</EndDate>\n"
                f"  <CountPerPage>{cpp}</CountPerPage>\n"
                f"  <CurrentPage>{cur}</CurrentPage>\n"
                f"  <OrderDirection>{int(order_direction)}</OrderDirection>\n"
                f"</GetPeriodCardApprovalLog>"
            )
            xml = self._call("GetPeriodCardApprovalLog", body)
            result_body = _extract_tag(xml, "GetPeriodCardApprovalLogResult")
            if not result_body:
                if re.search(
                    r"GetPeriodCardApprovalLogResult[^>]*xsi:nil=['\"]true['\"]", xml, re.I
                ):
                    return all_logs
                raise BarobillError(
                    "GetPeriodCardApprovalLogResult 파싱 실패", action="GetPeriodCardApprovalLog"
                )
            cp = _to_int(_extract_tag(result_body, "CurrentPage"))
            if cp is not None and cp < 0:
                raise BarobillError(
                    f"GetPeriodCardApprovalLog 실패: {cp}", code=cp, action="GetPeriodCardApprovalLog"
                )
            mp = _to_int(_extract_tag(result_body, "MaxPageNum")) or 1
            max_page = max(1, mp)

            list_xml = _extract_tag(result_body, "CardLogList") or ""
            for blk in _extract_blocks(list_xml, "CardApprovalLog"):
                all_logs.append(_parse_card_approval_log(blk))

            cur += 1
            if cur > 2000:
                raise BarobillError("페이지네이션 안전 한도(2000) 초과")

        return all_logs

    # ----- 종합: fetch_card_usages -----

    def fetch_card_usages(
        self,
        *,
        start_date: str,
        end_date: str,
        card_num: Optional[str] = None,
        refresh_before_fetch: bool = False,
    ) -> Dict[str, Any]:
        """기간/카드 별 카드승인내역 조회.

        card_num 지정 안 하면 GetCardEx2 로 전체 카드 목록 받고 각 카드별 조회.
        refresh_before_fetch=True 면 RefreshCard 먼저 호출.
        """
        target_cards: List[str]
        if card_num and card_num.strip():
            target_cards = [card_num.strip()]
        else:
            target_cards = self.get_card_list(available_only=True)

        refresh_results: List[Dict[str, Any]] = []
        all_logs: List[CardUsage] = []

        for cn in target_cards:
            if refresh_before_fetch:
                try:
                    code = self.refresh_card(cn)
                    refresh_results.append({"cardNum": cn, "resultCode": code, "ok": code > 0})
                except BarobillError as exc:
                    refresh_results.append(
                        {"cardNum": cn, "resultCode": exc.code, "ok": False, "error": str(exc)}
                    )

            try:
                logs = self.get_period_card_approval_log(
                    card_num=cn, start_date=start_date, end_date=end_date,
                )
                all_logs.extend(logs)
            except BarobillError as exc:
                # 한 카드 실패해도 나머지 진행
                refresh_results.append({"cardNum": cn, "resultCode": exc.code, "ok": False, "error": str(exc)})

        return {
            "targetCards": target_cards,
            "logs": all_logs,
            "refreshResults": refresh_results,
        }


def _parse_card_approval_log(block: str) -> CardUsage:
    """<CardApprovalLog> 1개 블록을 CardUsage 로 파싱."""

    def t(*tags: str) -> str:
        for tag in tags:
            v = _extract_tag(block, tag)
            if v:
                return _decode_xml(v)
        return ""

    corp_num = t("CorpNum")
    card_num = t("CardNum")
    use_dt = t("UseDT")
    approval_num = t("ApprovalNum", "CardApprovalNum")
    approval_type_raw = t("ApprovalType", "CardApprovalType")
    use_key_raw = t("UseKey")

    approval_amount = _to_int(t("ApprovalAmount", "CardApprovalCost", "ApprovalCost"))
    amount = _to_int(t("Amount")) if t("Amount") else None
    if amount is None:
        amount = approval_amount
    tax = _to_int(t("Tax")) if t("Tax") else None
    service_charge = _to_int(t("ServiceCharge")) if t("ServiceCharge") else None
    raw_total = _to_int(t("TotalAmount")) if t("TotalAmount") else None
    summed = None
    if amount is not None or tax is not None or service_charge is not None:
        summed = (amount or 0) + (tax or 0) + (service_charge or 0)
    if raw_total is not None and raw_total > 0:
        total_amount = raw_total
    elif summed is not None and summed > 0:
        total_amount = summed
    else:
        total_amount = raw_total if raw_total is not None else approval_amount

    # 취소 판정
    type_compact = re.sub(r"[\s_\-]", "", (approval_type_raw or "").upper())
    is_canceled = (
        "CANCEL" in type_compact or "취소" in approval_type_raw or type_compact in ("2", "C")
    )
    if not is_canceled:
        # 금액이 음수면 취소
        for v in (total_amount, approval_amount, amount):
            if isinstance(v, int) and v < 0:
                is_canceled = True
                break

    # useKey 결정
    fallback = f"{card_num}:{use_dt}:{approval_num}:{total_amount or 0}"
    use_key = (use_key_raw or fallback).strip()
    if is_canceled and not use_key.endswith(":CANCEL"):
        use_key = f"{use_key}:CANCEL"

    final_amount: Optional[int] = total_amount or approval_amount
    if is_canceled and final_amount is not None and final_amount > 0:
        final_amount = -final_amount  # 음수로 표시

    used_at_iso = _parse_use_dt(use_dt)
    store_name = t("UseStoreName")
    store_addr = t("UseStoreAddr")

    raw: Dict[str, Any] = {
        "CorpNum": corp_num,
        "CardNum": card_num,
        "UseKey": use_key,
        "UseDT": use_dt,
        "ApprovalType": approval_type_raw,
        "ApprovalNum": approval_num,
        "ApprovalAmount": approval_amount,
        "Amount": amount,
        "Tax": tax,
        "ServiceCharge": service_charge,
        "TotalAmount": total_amount,
        "UseStoreNum": t("UseStoreNum"),
        "UseStoreCorpNum": t("UseStoreCorpNum"),
        "UseStoreName": store_name,
        "UseStoreCeo": t("UseStoreCeo"),
        "UseStoreAddr": store_addr,
        "UseStoreBizType": t("UseStoreBizType"),
        "UseStoreTel": t("UseStoreTel"),
        "PaymentPlan": t("PaymentPlan"),
        "InstallmentMonths": t("InstallmentMonths"),
        "CurrencyCode": t("CurrencyCode", "Currency"),
        "Memo": t("Memo"),
    }

    return CardUsage(
        id=use_key,
        corp_num=corp_num or None,
        card_num=card_num or None,
        use_key=use_key,
        used_at=used_at_iso,
        store_name=store_name or None,
        amount=final_amount,
        category=None,
        memo=None,
        reviewed=False,
        coupang_purchase_id=None,
        raw=raw,
    )


__all__ = [
    "BarobillCardClient",
    "BarobillCardConfig",
    "BarobillError",
    "PRODUCTION_URL",
    "TEST_URL",
]
