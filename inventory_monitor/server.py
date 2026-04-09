#!/usr/bin/env python3
"""
재고 조회 HTTP API 서버 — 라즈베리파이에서 실행.
최신 재고 데이터를 JSON으로 반환.
"""
from __future__ import annotations

import json
import logging
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

        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        from urllib.parse import urlparse

        parsed = urlparse(self.path)
        path = parsed.path

        if path != "/shared-stock":
            self._send_json({"error": "not found"}, 404)
            return

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

    def do_DELETE(self):
        from urllib.parse import urlparse, parse_qs

        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path != "/shared-stock":
            self._send_json({"error": "not found"}, 404)
            return

        try:
            channel = str((qs.get("channel", [None])[0] or "")).strip().lower()
            product_key = str((qs.get("product_key", [None])[0] or "")).strip()
            db.delete_shared_stock_rule(channel=channel, product_key=product_key)
            self._send_json({"ok": True})
        except Exception as e:
            self._send_json({"error": str(e)}, 400)


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
