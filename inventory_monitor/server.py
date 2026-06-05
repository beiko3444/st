#!/usr/bin/env python3
"""
재고 조회 HTTP API 서버 — 라즈베리파이에서 실행.
최신 재고 데이터를 JSON으로 반환.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import hmac
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from inventory_monitor.history_db import InventoryHistoryDB

PORT = 8765

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("inventory-server")

db = InventoryHistoryDB()
_sync_guard = threading.Lock()
_sync_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "result": None,
    "error": None,
}


def _load_sms_api_token() -> str:
    token = os.environ.get("SMARTINVENTORY_SMS_API_TOKEN", "").strip()
    if token:
        return token
    config_path = _PROJECT_ROOT / "config" / "credentials.json"
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(raw, dict):
        return ""
    candidates = [
        raw.get("sms_api_token"),
        raw.get("sms_token"),
    ]
    monitor = raw.get("monitor")
    if isinstance(monitor, dict):
        candidates.extend([
            monitor.get("sms_api_token"),
            monitor.get("sms_token"),
        ])
    for value in candidates:
        text = str(value or "").strip()
        if text:
            return text
    return ""


SMS_API_TOKEN = _load_sms_api_token()


def _constant_time_eq(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def _run_inventory_sync() -> dict:
    from inventory_monitor.monitor import collect_inventory_once

    with _sync_guard:
        if _sync_state["running"]:
            return {"accepted": False, "running": True, "message": "inventory sync already running"}
        _sync_state.update(
            {
                "running": True,
                "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "finished_at": None,
                "result": None,
                "error": None,
            }
        )
    try:
        result = collect_inventory_once(db)
        with _sync_guard:
            _sync_state.update(
                {
                    "running": False,
                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "result": result,
                    "error": None,
                }
            )
        return {"accepted": True, "running": False, "result": result}
    except Exception as exc:  # noqa: BLE001
        with _sync_guard:
            _sync_state.update(
                {
                    "running": False,
                    "finished_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "result": None,
                    "error": str(exc),
                }
            )
        raise


def _sync_status() -> dict:
    with _sync_guard:
        return dict(_sync_state)


def _jsonable(value):
    from dataclasses import asdict, is_dataclass
    from datetime import date as _date, datetime as _datetime

    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, (_datetime, _date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value


def _cache_or_fetch(cache_key: str, refresh: bool, fetch_fn) -> dict:
    if not refresh:
        cached = db.get_api_cache(cache_key)
        if cached is not None:
            return {
                "cache_key": cache_key,
                "cached": True,
                "updated_at": cached["updated_at"],
                **(cached["payload"] if isinstance(cached["payload"], dict) else {"data": cached["payload"]}),
            }
    payload = _jsonable(fetch_fn())
    if not isinstance(payload, dict):
        payload = {"data": payload}
    saved = db.set_api_cache(cache_key, payload)
    return {
        "cache_key": cache_key,
        "cached": False,
        "updated_at": saved["updated_at"],
        **payload,
    }


def _load_app_config():
    from inventory_app.config import load_config

    return load_config()


def _fetch_revenue(period_days: int) -> dict:
    from inventory_app.services.revenue_services import RevenueComparisonService

    snapshot, warnings = RevenueComparisonService(_load_app_config()).fetch(period_days)
    return {"snapshot": snapshot, "warnings": warnings}


def _fetch_keywords(period_days: int) -> dict:
    from inventory_app.services.keyword_services import NaverKeywordRevenueService

    snapshot, warnings = NaverKeywordRevenueService(_load_app_config()).fetch(period_days)
    return {"snapshot": snapshot, "warnings": warnings}


def _fassto_connector():
    from inventory_app.connectors.fassto import FasstoConnector

    cfg = _load_app_config()
    return FasstoConnector(
        api_cd=cfg.fassto_api_cd,
        api_key=cfg.fassto_api_key,
        cst_cd=cfg.fassto_cst_cd,
        api_url=cfg.fassto_api_url,
        timeout_seconds=cfg.timeout_seconds,
    )


def _fassto_query(kind: str, qs: dict) -> dict:
    from datetime import date, timedelta

    today = date.today()
    start = (qs.get("start", [None])[0] or (today - timedelta(days=30)).strftime("%Y%m%d"))
    end = (qs.get("end", [None])[0] or today.strftime("%Y%m%d"))
    with _fassto_connector() as connector:
        if kind == "config":
            return connector.config_summary()
        if kind == "goods":
            return connector.get_goods_list()
        if kind == "elements":
            return connector.get_goods_elements()
        if kind == "stock":
            return connector.get_stock_list()
        if kind == "warehousing":
            return connector.get_warehousing_list(start, end)
        if kind == "delivery":
            status = qs.get("status", ["ALL"])[0] or "ALL"
            out_div = qs.get("out_div", ["1"])[0] or "1"
            return connector.get_delivery_list(start, end, status=status, out_div=out_div)
        if kind == "parcels":
            out_div = qs.get("out_div", ["1"])[0] or "1"
            return connector.get_delivery_parcel_list(start, end, out_div=out_div)
        if kind == "revenue":
            return connector.get_delivery_good_detail_list(start, end)
    raise ValueError(f"unknown fassto kind: {kind}")


def _sync_card_usages(body: dict) -> dict:
    from datetime import date, timedelta

    from inventory_app.services.card_api_client import CardApiClient

    cfg = _load_app_config()
    start = body.get("start_date") or body.get("startDate")
    end = body.get("end_date") or body.get("endDate")
    if not start:
        start = (date.today() - timedelta(days=30)).isoformat()
    if not end:
        end = date.today().isoformat()
    card_num = body.get("card_num") or body.get("cardNum")
    with CardApiClient.from_config(cfg) as client:
        sync_result = client.sync_card_usages(
            start_date=start,
            end_date=end,
            card_num=card_num,
            refresh_before_fetch=bool(body.get("refresh_before_fetch") or body.get("refreshBeforeFetch")),
        )
        page = client.list_card_usages(
            page=1,
            page_size=min(500, int(body.get("page_size") or body.get("pageSize") or 500)),
            card_num=card_num,
            start_date=start,
            end_date=end,
        )
    changed = db.upsert_card_usages(_jsonable(page.items))
    return {
        "sync": sync_result,
        "changed": changed,
        "fetched": len(page.items),
        "page": page.page,
        "total_count": page.total_count,
    }


def _match_coupang_purchases(body: dict) -> dict:
    from datetime import date, timedelta

    from inventory_app.services.card_api_client import CardApiClient

    cfg = _load_app_config()
    start = body.get("start_date") or body.get("startDate") or (date.today() - timedelta(days=30)).isoformat()
    end = body.get("end_date") or body.get("endDate") or date.today().isoformat()
    with CardApiClient.from_config(cfg) as client:
        return client.match_coupang_purchases(start_date=start, end_date=end)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        log.info("%s - %s", self.address_string(), format % args)

    def _send_json(self, data: object, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        payload = json.loads(raw.decode("utf-8"))
        if isinstance(payload, dict):
            return payload
        return {}

    def _authorized_sms_post(self) -> bool:
        if not SMS_API_TOKEN:
            return True
        auth = self.headers.get("Authorization", "").strip()
        provided = ""
        if auth.lower().startswith("bearer "):
            provided = auth[7:].strip()
        if not provided:
            provided = self.headers.get("X-Api-Token", "").strip()
        return bool(provided) and _constant_time_eq(provided, SMS_API_TOKEN)

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/inventory":
            try:
                naver = db.get_latest_snapshot("naver")
                coupang = db.get_latest_snapshot("coupang")
                self._send_json({
                    "naver": naver,
                    "coupang": coupang,
                })
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path in ("/health", "/status"):
            self._send_json({
                "status": "ok",
                "records": db.count_records(),
                "naver_last_updated": db.get_last_updated("naver"),
                "coupang_last_updated": db.get_last_updated("coupang"),
                "naver_collections": db.get_collection_count("naver"),
                "coupang_collections": db.get_collection_count("coupang"),
                "inventory_sync": _sync_status(),
                "sms_auth_enabled": bool(SMS_API_TOKEN),
            })

        elif path == "/sales":
            # /sales?date=2026-04-08
            date_str = qs.get("date", [None])[0]
            if not date_str:
                self._send_json({"error": "date parameter required"}, 400)
                return
            try:
                sales = db.get_sales_for_date(date_str)
                summary = db.get_daily_summary(date_str)
                self._send_json({"summary": summary, "sales": sales})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/sales/totals":
            # /sales/totals?days=30 → 최근 N일 재고차감 누적 (SKU별)
            raw_days = qs.get("days", ["30"])[0]
            try:
                n_days = max(1, min(365, int(raw_days)))
            except (TypeError, ValueError):
                n_days = 30
            try:
                totals = db.get_sales_totals_rolling(n_days)
                self._send_json({"days": n_days, "totals": totals})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/sales/series":
            # /sales/series?start=YYYY-MM-DD&end=YYYY-MM-DD
            start = qs.get("start", [None])[0]
            end = qs.get("end", [None])[0]
            if not start or not end:
                self._send_json({"error": "start and end parameters required"}, 400)
                return
            try:
                rows = db.get_daily_sales_series(start, end)
                self._send_json({"start": start, "end": end, "rows": rows})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/sales/dates":
            # 판매가 있었던 날짜 목록
            try:
                dates = db.get_sales_dates()
                self._send_json({"dates": dates})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/reviews":
            # 최신 리뷰 수
            try:
                naver = db.get_latest_reviews("naver")
                coupang = db.get_latest_reviews("coupang")
                self._send_json({"naver": naver, "coupang": coupang})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/revenue":
            try:
                days = max(1, min(365, int(qs.get("period_days", ["30"])[0] or 30)))
                refresh = qs.get("refresh", ["0"])[0] in ("1", "true", "True", "yes")
                self._send_json(
                    _cache_or_fetch(
                        f"revenue:{days}",
                        refresh,
                        lambda: _fetch_revenue(days),
                    )
                )
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/keywords":
            try:
                days = max(1, min(365, int(qs.get("period_days", ["30"])[0] or 30)))
                refresh = qs.get("refresh", ["0"])[0] in ("1", "true", "True", "yes")
                self._send_json(
                    _cache_or_fetch(
                        f"keywords:{days}",
                        refresh,
                        lambda: _fetch_keywords(days),
                    )
                )
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/fassto/config":
            try:
                self._send_json(_fassto_query("config", qs))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path.startswith("/fassto/warehousing/"):
            slip_no = path.rsplit("/", 1)[-1]
            try:
                with _fassto_connector() as connector:
                    self._send_json(connector.get_warehousing_detail(slip_no))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path.startswith("/fassto/delivery/"):
            slip_no = path.rsplit("/", 1)[-1]
            try:
                with _fassto_connector() as connector:
                    self._send_json(connector.get_delivery_detail(slip_no))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path.startswith("/fassto/"):
            kind = path.rsplit("/", 1)[-1]
            try:
                refresh = qs.get("refresh", ["0"])[0] in ("1", "true", "True", "yes")
                cache_parts = [f"fassto:{kind}"]
                for key in ("start", "end", "status", "out_div"):
                    value = qs.get(key, [""])[0] or ""
                    if value:
                        cache_parts.append(f"{key}={value}")
                self._send_json(
                    _cache_or_fetch(
                        "|".join(cache_parts),
                        refresh,
                        lambda: _fassto_query(kind, qs),
                    )
                )
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/shared-stock":
            channel = (qs.get("channel", [None])[0] or "").strip().lower() or None
            try:
                rules = db.get_shared_stock_rules(channel)
                self._send_json({"channel": channel, "rules": rules})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/masters":
            try:
                self._send_json({"masters": db.list_masters()})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif _match_master_id(path) is not None:
            master_id = _match_master_id(path)
            try:
                m = db.get_master(master_id)
                if not m:
                    self._send_json({"error": "not found"}, 404)
                else:
                    self._send_json({"master": m})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/master-links":
            try:
                self._send_json({"links": db.list_all_links()})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/stock-inbounds":
            channel = (qs.get("channel", [None])[0] or "").strip().lower() or None
            raw_master_id = (qs.get("master_id", [None])[0] or "").strip() or None
            master_id = None
            if raw_master_id is not None:
                try:
                    master_id = int(raw_master_id)
                except (TypeError, ValueError):
                    self._send_json({"error": "invalid master_id"}, 400)
                    return
            try:
                self._send_json(
                    {
                        "items": db.list_stock_inbounds(
                            master_id=master_id,
                            channel=channel,
                        ),
                        "summaries": db.list_stock_inbound_summaries(
                            master_id=master_id,
                            channel=channel,
                        ),
                    }
                )
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/purchase-records":
            channel = (qs.get("channel", [None])[0] or "").strip().lower() or None
            try:
                limit = max(1, min(20000, int(qs.get("limit", ["2000"])[0])))
            except (TypeError, ValueError):
                limit = 2000
            try:
                rows = db.list_purchase_records(channel=channel, limit=limit)
                self._send_json({"records": rows})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/purchase-orders":
            channel = (qs.get("channel", [None])[0] or "").strip().lower() or None
            try:
                limit = max(1, min(20000, int(qs.get("limit", ["2000"])[0])))
            except (TypeError, ValueError):
                limit = 2000
            try:
                rows = db.list_purchase_orders(channel=channel, limit=limit)
                self._send_json({"orders": rows})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/card-usages":
            start_date = qs.get("start_date", [None])[0]
            end_date = qs.get("end_date", [None])[0]
            card_num = qs.get("card_num", [None])[0]
            try:
                limit = max(1, min(50000, int(qs.get("limit", ["5000"])[0])))
            except (TypeError, ValueError):
                limit = 5000
            try:
                rows = db.list_card_usages(
                    start_date=start_date,
                    end_date=end_date,
                    card_num=card_num,
                    limit=limit,
                )
                self._send_json({"items": rows})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/coupang-credentials":
            try:
                rows = db.list_coupang_credentials()
                self._send_json({"credentials": rows})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/ui-prefs":
            key = qs.get("key", [None])[0]
            try:
                if not key:
                    self._send_json({"error": "key required"}, 400)
                else:
                    raw = db.get_ui_pref(key)
                    value = None
                    if raw is not None:
                        try:
                            value = json.loads(raw)
                        except Exception:
                            value = raw
                    self._send_json({"key": key, "value": value})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/sms-messages":
            start_date = qs.get("start_date", [None])[0]
            end_date = qs.get("end_date", [None])[0]
            sender = qs.get("sender", [None])[0]
            contains = qs.get("contains", [None])[0]
            try:
                limit = max(1, min(50000, int(qs.get("limit", ["1000"])[0])))
            except (TypeError, ValueError):
                limit = 1000
            try:
                rows = db.list_sms_messages(
                    start_date=start_date,
                    end_date=end_date,
                    sender=sender,
                    contains=contains,
                    limit=limit,
                )
                self._send_json({"items": rows})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif path == "/fixed-costs":
            try:
                rows = db.list_fixed_costs()
                self._send_json({"items": rows})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/sync/inventory":
            wait = (qs.get("wait", ["0"])[0] in ("1", "true", "True", "yes"))
            if wait:
                try:
                    result = _run_inventory_sync()
                    status = 200 if result.get("accepted") else 202
                    self._send_json(result, status)
                except Exception as e:
                    self._send_json({"accepted": False, "error": str(e), "state": _sync_status()}, 500)
                return

            def _worker() -> None:
                try:
                    _run_inventory_sync()
                except Exception:
                    log.exception("inventory sync failed")

            with _sync_guard:
                already_running = bool(_sync_state["running"])
            if not already_running:
                threading.Thread(target=_worker, daemon=True, name="inventory-sync").start()
            self._send_json({"accepted": not already_running, "running": True, "state": _sync_status()}, 202)
            return

        if path == "/sync/revenue":
            try:
                body = self._read_json_body()
                days = max(1, min(365, int(body.get("period_days") or qs.get("period_days", ["30"])[0] or 30)))
                payload = _cache_or_fetch(f"revenue:{days}", True, lambda: _fetch_revenue(days))
                self._send_json(payload)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/sync/keywords":
            try:
                body = self._read_json_body()
                days = max(1, min(365, int(body.get("period_days") or qs.get("period_days", ["30"])[0] or 30)))
                payload = _cache_or_fetch(f"keywords:{days}", True, lambda: _fetch_keywords(days))
                self._send_json(payload)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/sync/card-usages":
            try:
                self._send_json(_sync_card_usages(self._read_json_body()))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/sync/coupang-purchases/match":
            try:
                self._send_json(_match_coupang_purchases(self._read_json_body()))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/fassto/warehousing":
            try:
                from inventory_app.connectors.fassto import build_warehousing_payload

                body = self._read_json_body()
                payload = [build_warehousing_payload(item) for item in body.get("items", [body])]
                with _fassto_connector() as connector:
                    self._send_json(connector.create_warehousing(payload))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/fassto/warehousing/cancel":
            try:
                body = self._read_json_body()
                with _fassto_connector() as connector:
                    self._send_json(connector.cancel_warehousing(body.get("items", [body])))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/fassto/delivery":
            try:
                body = self._read_json_body()
                with _fassto_connector() as connector:
                    self._send_json(connector.create_delivery_parcel(body.get("items", [body])))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/fassto/delivery/cancel":
            try:
                body = self._read_json_body()
                with _fassto_connector() as connector:
                    self._send_json(connector.cancel_delivery(body.get("items", [body])))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/shared-stock":
            try:
                body = self._read_json_body()
                channel = str(body.get("channel") or "").strip().lower()
                product_key = str(body.get("product_key") or "").strip()
                group_id = str(body.get("group_id") or "").strip()
                pack_size = int(body.get("pack_size") or 1)
                is_master = bool(body.get("is_master"))
                db.upsert_shared_stock_rule(
                    channel=channel,
                    product_key=product_key,
                    group_id=group_id,
                    pack_size=pack_size,
                    is_master=is_master,
                )
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
            return

        if path == "/masters":
            try:
                body = self._read_json_body()
                m = db.create_master(
                    name=str(body.get("name") or ""),
                    unit_cost=_opt_int(body.get("unit_cost")),
                    memo=body.get("memo"),
                )
                self._send_json({"master": m}, 201)
            except ValueError as e:
                self._send_json({"error": str(e)}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/master-links":
            try:
                body = self._read_json_body()
                link = db.link_channel_product(
                    channel=str(body.get("channel") or ""),
                    product_key=str(body.get("product_key") or ""),
                    master_id=int(body.get("master_id")),
                    multiplier=int(body.get("multiplier") or 1),
                )
                self._send_json({"link": link}, 201)
            except (ValueError, TypeError) as e:
                self._send_json({"error": str(e)}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/stock-inbounds":
            try:
                body = self._read_json_body()
                row = db.add_stock_inbound(
                    receipt_date=str(body.get("receipt_date") or ""),
                    master_id=int(body.get("master_id") or 0),
                    channel=str(body.get("channel") or ""),
                    quantity=int(body.get("quantity") or 0),
                )
                self._send_json({"item": row}, 201)
            except (ValueError, TypeError) as e:
                self._send_json({"error": str(e)}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/stock-inbounds/reconcile":
            try:
                body = self._read_json_body()
                items = body.get("items") or []
                if not isinstance(items, list):
                    self._send_json({"error": "items must be a list"}, 400)
                    return
                self._send_json(db.reconcile_stock_inbounds(items))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/purchase-records":
            try:
                body = self._read_json_body()
                records = body.get("records") or []
                if not isinstance(records, list):
                    self._send_json({"error": "records must be a list"}, 400)
                    return
                inserted = db.upsert_purchase_records(records)
                self._send_json({"inserted": inserted, "received": len(records)})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/purchase-orders":
            try:
                body = self._read_json_body()
                orders = body.get("orders") or []
                if not isinstance(orders, list):
                    self._send_json({"error": "orders must be a list"}, 400)
                    return
                changed = db.upsert_purchase_orders(orders)
                self._send_json({"changed": changed, "received": len(orders)})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/card-usages":
            try:
                body = self._read_json_body()
                items = body.get("items") or []
                if not isinstance(items, list):
                    self._send_json({"error": "items must be a list"}, 400)
                    return
                changed = db.upsert_card_usages(items)
                self._send_json({"changed": changed, "received": len(items)})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/coupang-credentials":
            try:
                body = self._read_json_body()
                label = str(body.get("label") or "").strip()
                email = str(body.get("email") or "").strip()
                password_obf = str(body.get("password_obf") or "").strip()
                if not label or not email or not password_obf:
                    self._send_json({"error": "label, email, password_obf 필수"}, 400)
                    return
                db.upsert_coupang_credential(label, email, password_obf)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/ui-prefs":
            try:
                body = self._read_json_body()
                key = str(body.get("key") or "").strip()
                if not key:
                    self._send_json({"error": "key required"}, 400)
                    return
                value = body.get("value")
                # 숫자/객체/리스트는 JSON 문자열로 직렬화해서 저장
                if isinstance(value, (dict, list, int, float, bool)) or value is None:
                    raw = json.dumps(value, ensure_ascii=False)
                else:
                    raw = str(value)
                db.set_ui_pref(key, raw)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/sms-messages":
            try:
                if not self._authorized_sms_post():
                    self._send_json({"error": "unauthorized"}, 401)
                    return
                body = self._read_json_body()
                items = body.get("items")
                if items is None and ("sender" in body or "body" in body or "received_at" in body):
                    # 단건 페이로드도 허용 (안드로이드 워커가 메시지 1건씩 전송)
                    items = [body]
                if not isinstance(items, list):
                    self._send_json({"error": "items must be a list"}, 400)
                    return
                changed = db.upsert_sms_messages(items)
                self._send_json({"changed": changed, "received": len(items)})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/fixed-costs":
            try:
                body = self._read_json_body()
                items = body.get("items") if isinstance(body, dict) else None
                if items is None and isinstance(body, dict) and body.get("id") is not None:
                    items = [body]
                if not isinstance(items, list):
                    self._send_json({"error": "items must be a list"}, 400)
                    return
                changed = db.upsert_fixed_costs(items)
                self._send_json({"changed": changed, "received": len(items)})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        self._send_json({"error": "not found"}, 404)

    def do_PATCH(self):
        from urllib.parse import urlparse

        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/fassto/warehousing":
            try:
                from inventory_app.connectors.fassto import build_warehousing_payload

                body = self._read_json_body()
                payload = [build_warehousing_payload(item) for item in body.get("items", [body])]
                with _fassto_connector() as connector:
                    self._send_json(connector.update_warehousing(payload))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/fassto/delivery":
            try:
                body = self._read_json_body()
                with _fassto_connector() as connector:
                    self._send_json(connector.update_delivery_parcel(body.get("items", [body])))
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        # /card-usages/<use_key>
        cu_match = _CARD_USAGE_RE.match(path)
        if cu_match is not None:
            use_key = cu_match.group(1)
            try:
                body = self._read_json_body()
                row = db.update_card_usage_fields(
                    use_key,
                    memo=body.get("memo") if "memo" in body and not body.get("clear_memo") else None,
                    category=body.get("category") if "category" in body else None,
                    reviewed=(bool(body["reviewed"]) if "reviewed" in body else None),
                    coupang_purchase_id=(
                        body.get("coupang_purchase_id")
                        if "coupang_purchase_id" in body
                        and not body.get("clear_coupang_match")
                        else None
                    ),
                    clear_memo=bool(body.get("clear_memo")),
                    clear_coupang_match=bool(body.get("clear_coupang_match")),
                )
                self._send_json({"item": row, "ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        master_id = _match_master_id(path)
        if master_id is None:
            self._send_json({"error": "not found"}, 404)
            return

        try:
            body = self._read_json_body()
            m = db.update_master(
                master_id,
                name=body.get("name") if body.get("name") is not None else None,
                unit_cost=_opt_int(body.get("unit_cost")),
                memo=body.get("memo"),
                clear_unit_cost=bool(body.get("clear_unit_cost")),
                clear_memo=bool(body.get("clear_memo")),
            )
            if not m:
                self._send_json({"error": "not found"}, 404)
            else:
                self._send_json({"master": m})
        except ValueError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:
            self._send_json({"error": str(e)}, 500)

    def do_PUT(self):
        from urllib.parse import urlparse

        parsed = urlparse(self.path)
        path = parsed.path

        rep_match = _MASTER_REP_RE.match(path)
        if rep_match is not None:
            master_id = int(rep_match.group(1))
            try:
                body = self._read_json_body()
                channel = body.get("channel")
                product_key = body.get("product_key")
                m = db.set_master_representative(
                    master_id,
                    (str(channel) if channel else None),
                    (str(product_key) if product_key else None),
                )
                if not m:
                    self._send_json({"error": "not found"}, 404)
                else:
                    self._send_json({"master": m})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/master-links/multiplier":
            try:
                body = self._read_json_body()
                link = db.set_link_multiplier(
                    channel=str(body.get("channel") or ""),
                    product_key=str(body.get("product_key") or ""),
                    multiplier=int(body.get("multiplier") or 1),
                )
                if not link:
                    self._send_json({"error": "not found"}, 404)
                else:
                    self._send_json({"link": link})
            except (ValueError, TypeError) as e:
                self._send_json({"error": str(e)}, 400)
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        self._send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        inbound_match = _STOCK_INBOUND_RE.match(path)
        if inbound_match is not None:
            try:
                deleted = db.delete_stock_inbound(int(inbound_match.group(1)))
                if deleted <= 0:
                    self._send_json({"error": "not found"}, 404)
                else:
                    self._send_json({"deleted": deleted})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/shared-stock":
            try:
                channel = str((qs.get("channel", [None])[0] or "")).strip().lower()
                product_key = str((qs.get("product_key", [None])[0] or "")).strip()
                db.delete_shared_stock_rule(channel=channel, product_key=product_key)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
            return

        master_id = _match_master_id(path)
        if master_id is not None:
            try:
                db.delete_master(master_id)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/master-links":
            try:
                channel = str((qs.get("channel", [None])[0] or "")).strip()
                product_key = str((qs.get("product_key", [None])[0] or "")).strip()
                db.unlink_channel_product(channel=channel, product_key=product_key)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 400)
            return

        if path == "/purchase-records":
            try:
                channel = (qs.get("channel", [None])[0] or "").strip().lower() or None
                missing_order_no = (qs.get("missing_order_no", ["0"])[0] in ("1", "true", "True"))
                order_no_like = (qs.get("order_no_like", [None])[0] or "").strip() or None
                title_like = (qs.get("title_like", [None])[0] or "").strip() or None
                deleted = db.delete_purchase_records(
                    channel=channel,
                    only_missing_order_no=missing_order_no,
                    order_no_like=order_no_like,
                    title_like=title_like,
                )
                self._send_json({"deleted": deleted})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        if path == "/coupang-credentials":
            try:
                label = str((qs.get("label", [None])[0] or "")).strip()
                if not label:
                    self._send_json({"error": "label 필수"}, 400)
                    return
                db.delete_coupang_credential(label)
                self._send_json({"ok": True})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        # /fixed-costs/<id>
        fc_match = _FIXED_COST_RE.match(path)
        if fc_match is not None:
            try:
                deleted = db.delete_fixed_cost(int(fc_match.group(1)))
                self._send_json({"deleted": deleted})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return

        self._send_json({"error": "not found"}, 404)


_MASTER_ID_RE = re.compile(r"^/masters/(\d+)$")
_MASTER_REP_RE = re.compile(r"^/masters/(\d+)/representative$")
_STOCK_INBOUND_RE = re.compile(r"^/stock-inbounds/(\d+)$")
_CARD_USAGE_RE = re.compile(r"^/card-usages/(.+)$")
_FIXED_COST_RE = re.compile(r"^/fixed-costs/(\d+)$")


def _match_master_id(path: str) -> int | None:
    m = _MASTER_ID_RE.match(path)
    return int(m.group(1)) if m else None


def _opt_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def main() -> None:
    log.info("=== 재고 API 서버 시작 (port %d) ===", PORT)
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    log.info("=== 서버 종료 ===")


if __name__ == "__main__":
    main()
