from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from inventory_app.config import AppConfig, load_config


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)) or default)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _from_mapping(raw: dict[str, Any]) -> AppConfig:
    smartstore = raw.get("smartstore") if isinstance(raw.get("smartstore"), dict) else {}
    stats = raw.get("smartstore_stats") if isinstance(raw.get("smartstore_stats"), dict) else {}
    coupang = raw.get("coupang") if isinstance(raw.get("coupang"), dict) else {}
    request = raw.get("request") if isinstance(raw.get("request"), dict) else {}
    monitor = raw.get("monitor") if isinstance(raw.get("monitor"), dict) else {}
    fassto = raw.get("fassto") if isinstance(raw.get("fassto"), dict) else {}
    card_api = raw.get("card_api") if isinstance(raw.get("card_api"), dict) else {}
    barobill = raw.get("barobill") if isinstance(raw.get("barobill"), dict) else {}
    statement = raw.get("statement") if isinstance(raw.get("statement"), dict) else {}
    supplier = statement.get("supplier") if isinstance(statement.get("supplier"), dict) else {}
    buyer = statement.get("buyer") if isinstance(statement.get("buyer"), dict) else {}

    client_id = str(smartstore.get("client_id") or "")
    client_secret = str(smartstore.get("client_secret") or "")
    token_type = str(smartstore.get("token_type") or "Bearer")

    return AppConfig(
        smartstore_client_id=client_id,
        smartstore_client_secret=client_secret,
        smartstore_token_type=token_type,
        smartstore_stats_client_id=str(stats.get("client_id") or client_id),
        smartstore_stats_client_secret=str(stats.get("client_secret") or client_secret),
        smartstore_stats_token_type=str(stats.get("token_type") or token_type),
        stats_lookback_days=int(raw.get("stats", {}).get("lookback_days", 30))
        if isinstance(raw.get("stats"), dict)
        else 30,
        coupang_vendor_id=str(coupang.get("vendor_id") or ""),
        coupang_access_key=str(coupang.get("access_key") or ""),
        coupang_secret_key=str(coupang.get("secret_key") or ""),
        timeout_seconds=int(request.get("timeout_seconds") or 30),
        max_products=int(request.get("max_products") or 500),
        monitor_url=str(monitor.get("url") or os.environ.get("SMARTINVENTORY_MONITOR_URL", "") or "").strip()
        or None,
        monitor_url_gist=str(monitor.get("url_gist") or ""),
        smartstore_store_url=str(smartstore.get("store_url") or ""),
        fassto_api_url=str(fassto.get("api_url") or "https://fmsapi.fassto.ai"),
        fassto_api_cd=str(fassto.get("api_cd") or ""),
        fassto_api_key=str(fassto.get("api_key") or ""),
        fassto_cst_cd=str(fassto.get("cst_cd") or ""),
        card_api_base_url=str(card_api.get("base_url") or ""),
        card_api_service_token=str(card_api.get("service_token") or ""),
        barobill_certkey=str(barobill.get("certkey") or ""),
        barobill_corp_num=str(barobill.get("corp_num") or ""),
        barobill_user_id=str(barobill.get("id") or ""),
        barobill_use_test=bool(barobill.get("use_test") or False),
        statement_customer_name=str(statement.get("customer_name") or ""),
        statement_supplier_biz_no=str(supplier.get("biz_no") or ""),
        statement_supplier_name=str(supplier.get("name") or ""),
        statement_supplier_ceo=str(supplier.get("ceo") or ""),
        statement_supplier_addr=str(supplier.get("addr") or ""),
        statement_supplier_tel=str(supplier.get("tel") or ""),
        statement_buyer_biz_no=str(buyer.get("biz_no") or ""),
        statement_buyer_name=str(buyer.get("name") or ""),
        statement_buyer_ceo=str(buyer.get("ceo") or ""),
        statement_buyer_addr=str(buyer.get("addr") or ""),
        statement_buyer_tel=str(buyer.get("tel") or ""),
    )


def _from_env() -> AppConfig:
    raw_json = os.environ.get("SMARTINVENTORY_CONFIG_JSON", "").strip()
    if raw_json:
        parsed = json.loads(raw_json)
        if isinstance(parsed, dict):
            return _from_mapping(parsed)

    return AppConfig(
        smartstore_client_id=os.environ.get("SMARTSTORE_CLIENT_ID", ""),
        smartstore_client_secret=os.environ.get("SMARTSTORE_CLIENT_SECRET", ""),
        smartstore_token_type=os.environ.get("SMARTSTORE_TOKEN_TYPE", "Bearer"),
        smartstore_stats_client_id=os.environ.get(
            "SMARTSTORE_STATS_CLIENT_ID",
            os.environ.get("SMARTSTORE_CLIENT_ID", ""),
        ),
        smartstore_stats_client_secret=os.environ.get(
            "SMARTSTORE_STATS_CLIENT_SECRET",
            os.environ.get("SMARTSTORE_CLIENT_SECRET", ""),
        ),
        smartstore_stats_token_type=os.environ.get(
            "SMARTSTORE_STATS_TOKEN_TYPE",
            os.environ.get("SMARTSTORE_TOKEN_TYPE", "Bearer"),
        ),
        stats_lookback_days=_env_int("SMARTINVENTORY_STATS_LOOKBACK_DAYS", 30),
        coupang_vendor_id=os.environ.get("COUPANG_VENDOR_ID", ""),
        coupang_access_key=os.environ.get("COUPANG_ACCESS_KEY", ""),
        coupang_secret_key=os.environ.get("COUPANG_SECRET_KEY", ""),
        timeout_seconds=_env_int("SMARTINVENTORY_TIMEOUT_SECONDS", 30),
        max_products=_env_int("SMARTINVENTORY_MAX_PRODUCTS", 500),
        monitor_url=(
            os.environ.get("SMARTINVENTORY_MONITOR_URL")
            or os.environ.get("MONITOR_URL")
            or ""
        ).strip()
        or None,
        monitor_url_gist=os.environ.get("SMARTINVENTORY_MONITOR_URL_GIST", ""),
        smartstore_store_url=os.environ.get("SMARTSTORE_STORE_URL", ""),
        fassto_api_url=os.environ.get("FASSTO_API_URL", "https://fmsapi.fassto.ai"),
        fassto_api_cd=os.environ.get("FASSTO_API_CD", ""),
        fassto_api_key=os.environ.get("FASSTO_API_KEY", ""),
        fassto_cst_cd=os.environ.get("FASSTO_CST_CD", ""),
        card_api_base_url=os.environ.get("CARD_API_BASE_URL", ""),
        card_api_service_token=os.environ.get("CARD_API_SERVICE_TOKEN", ""),
        barobill_certkey=os.environ.get("BAROBILL_CERTKEY", ""),
        barobill_corp_num=os.environ.get("BAROBILL_CORP_NUM", ""),
        barobill_user_id=os.environ.get("BAROBILL_USER_ID", ""),
        barobill_use_test=_env_bool("BAROBILL_USE_TEST"),
        statement_customer_name=os.environ.get("STATEMENT_CUSTOMER_NAME", ""),
        statement_supplier_biz_no=os.environ.get("STATEMENT_SUPPLIER_BIZ_NO", ""),
        statement_supplier_name=os.environ.get("STATEMENT_SUPPLIER_NAME", ""),
        statement_supplier_ceo=os.environ.get("STATEMENT_SUPPLIER_CEO", ""),
        statement_supplier_addr=os.environ.get("STATEMENT_SUPPLIER_ADDR", ""),
        statement_supplier_tel=os.environ.get("STATEMENT_SUPPLIER_TEL", ""),
        statement_buyer_biz_no=os.environ.get("STATEMENT_BUYER_BIZ_NO", ""),
        statement_buyer_name=os.environ.get("STATEMENT_BUYER_NAME", ""),
        statement_buyer_ceo=os.environ.get("STATEMENT_BUYER_CEO", ""),
        statement_buyer_addr=os.environ.get("STATEMENT_BUYER_ADDR", ""),
        statement_buyer_tel=os.environ.get("STATEMENT_BUYER_TEL", ""),
    )


def configure_runtime_storage() -> None:
    if os.environ.get("SMARTINVENTORY_CACHE_DB"):
        return
    if os.environ.get("VERCEL"):
        os.environ["SMARTINVENTORY_CACHE_DB"] = "/tmp/smartinventory/channel_cache.sqlite3"


def load_web_config(config_path: str | None = None) -> AppConfig:
    configure_runtime_storage()

    requested = (config_path or os.environ.get("SMARTINVENTORY_CONFIG", "")).strip()
    if requested:
        path = Path(requested).expanduser().resolve()
        if path.exists():
            config = load_config(path)
        else:
            config = _from_env()
    else:
        default_path = Path(__file__).resolve().parents[1] / "config" / "credentials.json"
        if default_path.exists() and not os.environ.get("VERCEL"):
            try:
                config = load_config(default_path)
            except Exception:
                config = _from_env()
        else:
            config = _from_env()

    env_monitor = (
        os.environ.get("SMARTINVENTORY_MONITOR_URL")
        or os.environ.get("MONITOR_URL")
        or ""
    ).strip()
    if env_monitor:
        config.monitor_url = env_monitor.rstrip("/")

    env_gist = os.environ.get("SMARTINVENTORY_MONITOR_URL_GIST", "").strip()
    if env_gist:
        config.monitor_url_gist = env_gist

    return config


def config_status(config: AppConfig) -> dict[str, Any]:
    return {
        "monitorConfigured": bool(config.monitor_url),
        "smartstoreConfigured": bool(config.smartstore_client_id and config.smartstore_client_secret),
        "coupangConfigured": bool(
            config.coupang_vendor_id and config.coupang_access_key and config.coupang_secret_key
        ),
        "fasstoConfigured": bool(config.fassto_api_cd and config.fassto_api_key and config.fassto_cst_cd),
        "cardApiConfigured": bool(config.card_api_base_url),
        "vercel": bool(os.environ.get("VERCEL")),
        "cacheDb": os.environ.get("SMARTINVENTORY_CACHE_DB", ""),
    }
