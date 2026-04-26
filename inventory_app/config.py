from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict

import httpx


@dataclass
class AppConfig:
    smartstore_client_id: str
    smartstore_client_secret: str
    smartstore_token_type: str
    smartstore_stats_client_id: str
    smartstore_stats_client_secret: str
    smartstore_stats_token_type: str
    stats_lookback_days: int
    coupang_vendor_id: str
    coupang_access_key: str
    coupang_secret_key: str
    timeout_seconds: int
    max_products: int
    # 이하 default 필드들
    monitor_url: str | None = None  # 라즈베리파이 재고 API URL
    smartstore_store_url: str = ""  # 예: "https://smartstore.naver.com/baikoapp"
    fassto_api_url: str = "https://fmsapi.fassto.ai"
    fassto_api_cd: str = ""
    fassto_api_key: str = ""
    fassto_cst_cd: str = ""
    card_api_base_url: str = ""              # 외부 card-api-service base URL (옵션)
    card_api_service_token: str = ""         # Bearer token (디자인 §6)
    barobill_certkey: str = ""               # 바로빌 SOAP CERTKEY
    barobill_corp_num: str = ""              # 사업자번호 ('-' 제거)
    barobill_user_id: str = ""               # 바로빌 계정 ID
    barobill_use_test: bool = False          # 테스트 서버 사용 여부


def _get(config: Dict[str, Any], *keys: str) -> Any:
    current: Any = config
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            raise KeyError(f"Missing config key: {'.'.join(keys)}")
        current = current[key]
    return current


def _get_optional(config: Dict[str, Any], default: Any, *keys: str) -> Any:
    current: Any = config
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def _resolve_monitor_url_from_gist(gist_raw_url: str, timeout: float = 5.0) -> str | None:
    """Secret gist 의 monitor.json 에서 최신 Pi tunnel URL 을 가져온다.

    Pi 부팅 때마다 cloudflared quick tunnel hostname 이 바뀌기 때문에
    Pi 쪽 publisher 스크립트가 매번 gist 를 업데이트하고,
    앱은 시작 시 이 raw URL 에서 현재 URL 을 읽어온다.

    raw.githubusercontent.com 은 CDN 캐시가 있어 최신 커밋 반영까지
    지연이 있으므로 쿼리스트링 cache buster + no-cache 헤더로 우회한다.

    실패 시 None 반환 → 호출부에서 credentials.json 의 monitor.url 로 fallback.
    """
    if not gist_raw_url:
        return None
    import time
    separator = "&" if "?" in gist_raw_url else "?"
    bust = f"{gist_raw_url}{separator}t={int(time.time())}"
    try:
        resp = httpx.get(
            bust,
            timeout=timeout,
            follow_redirects=True,
            headers={"Cache-Control": "no-cache"},
        )
        if resp.status_code != 200:
            return None
        payload = resp.json()
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None
    url = str(payload.get("url") or "").strip()
    return url or None


def load_config(path: Path | None = None) -> AppConfig:
    if path is None:
        root = Path(__file__).resolve().parents[1]
        path = root / "config" / "credentials.json"

    raw = json.loads(path.read_text(encoding="utf-8-sig"))

    smartstore_client_id = str(_get(raw, "smartstore", "client_id"))
    smartstore_client_secret = str(_get(raw, "smartstore", "client_secret"))
    smartstore_token_type = str(_get(raw, "smartstore", "token_type"))
    smartstore_store_url = str(_get_optional(raw, "", "smartstore", "store_url")).strip()

    # Pi tunnel URL 해상도:
    # 1) monitor.url_gist 가 있으면 gist 에서 최신 URL fetch (Pi 재부팅에도 무관)
    # 2) 실패하거나 미설정이면 monitor.url 을 그대로 사용
    gist_raw_url = str(_get_optional(raw, "", "monitor", "url_gist")).strip()
    fallback_monitor_url = str(_get_optional(raw, "", "monitor", "url")).strip()
    resolved_monitor_url: str | None = None
    if gist_raw_url:
        resolved_monitor_url = _resolve_monitor_url_from_gist(gist_raw_url)
    if not resolved_monitor_url:
        resolved_monitor_url = fallback_monitor_url or None

    return AppConfig(
        smartstore_client_id=smartstore_client_id,
        smartstore_client_secret=smartstore_client_secret,
        smartstore_token_type=smartstore_token_type,
        smartstore_store_url=smartstore_store_url,
        smartstore_stats_client_id=str(
            _get_optional(raw, smartstore_client_id, "smartstore_stats", "client_id")
        ),
        smartstore_stats_client_secret=str(
            _get_optional(raw, smartstore_client_secret, "smartstore_stats", "client_secret")
        ),
        smartstore_stats_token_type=str(
            _get_optional(raw, smartstore_token_type, "smartstore_stats", "token_type")
        ),
        stats_lookback_days=int(_get_optional(raw, 30, "stats", "lookback_days")),
        coupang_vendor_id=str(_get(raw, "coupang", "vendor_id")),
        coupang_access_key=str(_get(raw, "coupang", "access_key")),
        coupang_secret_key=str(_get(raw, "coupang", "secret_key")),
        timeout_seconds=int(_get(raw, "request", "timeout_seconds")),
        max_products=int(_get(raw, "request", "max_products")),
        monitor_url=resolved_monitor_url,
        fassto_api_url=str(
            _get_optional(raw, "https://fmsapi.fassto.ai", "fassto", "api_url")
        ),
        fassto_api_cd=str(_get_optional(raw, "", "fassto", "api_cd")),
        fassto_api_key=str(_get_optional(raw, "", "fassto", "api_key")),
        fassto_cst_cd=str(_get_optional(raw, "", "fassto", "cst_cd")),
        card_api_base_url=str(_get_optional(raw, "", "card_api", "base_url")).strip(),
        card_api_service_token=str(_get_optional(raw, "", "card_api", "service_token")).strip(),
        barobill_certkey=str(_get_optional(raw, "", "barobill", "certkey")).strip(),
        barobill_corp_num=str(_get_optional(raw, "", "barobill", "corp_num")).strip(),
        barobill_user_id=str(_get_optional(raw, "", "barobill", "id")).strip(),
        barobill_use_test=bool(_get_optional(raw, False, "barobill", "use_test")),
    )
