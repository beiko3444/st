from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


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


def load_config(path: Path | None = None) -> AppConfig:
    if path is None:
        root = Path(__file__).resolve().parents[1]
        path = root / "config" / "credentials.json"

    raw = json.loads(path.read_text(encoding="utf-8-sig"))

    smartstore_client_id = str(_get(raw, "smartstore", "client_id"))
    smartstore_client_secret = str(_get(raw, "smartstore", "client_secret"))
    smartstore_token_type = str(_get(raw, "smartstore", "token_type"))
    smartstore_store_url = str(_get_optional(raw, "", "smartstore", "store_url")).strip()

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
        monitor_url=str(_get_optional(raw, None, "monitor", "url")) or None,
        fassto_api_url=str(
            _get_optional(raw, "https://fmsapi.fassto.ai", "fassto", "api_url")
        ),
        fassto_api_cd=str(_get_optional(raw, "", "fassto", "api_cd")),
        fassto_api_key=str(_get_optional(raw, "", "fassto", "api_key")),
        fassto_cst_cd=str(_get_optional(raw, "", "fassto", "cst_cd")),
    )
