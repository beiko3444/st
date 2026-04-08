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

    def do_GET(self):
        if self.path == "/inventory":
            try:
                naver = db.get_latest_snapshot("naver")
                coupang = db.get_latest_snapshot("coupang")
                self._send_json({
                    "naver": [
                        {"product_id": r[0], "name": r[1], "stock": r[2], "recorded_at": r[3]}
                        for r in naver
                    ],
                    "coupang": [
                        {"product_id": r[0], "name": r[1], "stock": r[2], "recorded_at": r[3]}
                        for r in coupang
                    ],
                })
            except Exception as e:
                self._send_json({"error": str(e)}, 500)

        elif self.path == "/health":
            self._send_json({"status": "ok", "records": db.count_records()})

        else:
            self._send_json({"error": "not found"}, 404)


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
