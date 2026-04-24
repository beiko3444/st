#!/usr/bin/env python3
"""
재고 조회 HTTP API 서버 — 라즈베리파이에서 실행.
최신 재고 데이터를 JSON으로 반환.
"""
from __future__ import annotations

import json
import logging
import re
import sys
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

        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        from urllib.parse import urlparse

        parsed = urlparse(self.path)
        path = parsed.path

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

        self._send_json({"error": "not found"}, 404)

    def do_PATCH(self):
        from urllib.parse import urlparse

        parsed = urlparse(self.path)
        path = parsed.path

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

        self._send_json({"error": "not found"}, 404)


_MASTER_ID_RE = re.compile(r"^/masters/(\d+)$")
_MASTER_REP_RE = re.compile(r"^/masters/(\d+)/representative$")


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
