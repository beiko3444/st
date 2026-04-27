"""Pi DB 의 쿠팡 구매내역 중 order_no 없는 행 일괄 삭제.

사용:
  python3 scripts/delete_pi_coupang_no_orderno.py

전제: Pi 서버에 최신 server.py 배포되어 DELETE /purchase-records 엔드포인트 활성화.
배포: ./inventory_monitor/update_remote.sh
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from inventory_app.services.pi_data_client import PiDataClient, PiDataError


def main() -> int:
    cred_path = Path(__file__).resolve().parents[1] / "config" / "credentials.json"
    creds = json.loads(cred_path.read_text(encoding="utf-8"))
    monitor_url = (creds.get("monitor") or {}).get("url") or ""
    if not monitor_url:
        print("monitor.url 미설정")
        return 1

    client = PiDataClient(monitor_url)
    try:
        deleted = client.delete_purchase_records(channel="coupang", only_missing_order_no=True)
    except PiDataError as exc:
        print(f"Pi 호출 실패: {exc}")
        return 2

    print(f"✓ 쿠팡 + 주문번호 없는 행 삭제: {deleted}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
