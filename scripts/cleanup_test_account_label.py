"""테스트용 'account_label 검증' 더미 데이터 삭제.

로컬 DB (inventory_app.db) 와 Pi DB 양쪽에서 다음을 제거:
- order_no LIKE 'ZZZ_%' / title LIKE '[TEST]%' / title LIKE '%account_label 검증%'

Pi 정리는 서버에 새 필터(order_no_like, title_like) 가 배포된 이후 동작합니다.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def cleanup_local() -> None:
    db_path = ROOT / "inventory_app.db"
    if not db_path.exists():
        print(f"local DB 없음: {db_path}")
        return
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    targets = [
        ("purchase_records", "DELETE FROM purchase_records WHERE order_no LIKE 'ZZZ_%' OR title LIKE '[TEST]%' OR title LIKE '%account_label 검증%'"),
        ("purchase_orders", "DELETE FROM purchase_orders WHERE order_no LIKE 'ZZZ_%' OR raw_text LIKE '[TEST]%' OR raw_text LIKE '%account_label 검증%'"),
        ("card_usages", "DELETE FROM card_usages WHERE memo LIKE '[TEST]%' OR memo LIKE '%account_label 검증%'"),
    ]
    for table, sql in targets:
        try:
            n = cur.execute(sql).rowcount
            print(f"  {table}: {n} 행 삭제")
        except sqlite3.OperationalError as exc:
            print(f"  {table}: skip ({exc})")
    conn.commit()
    conn.close()


def cleanup_pi(monitor_url: str) -> None:
    import httpx

    base = monitor_url.rstrip("/")
    print(f"Pi 정리: {base}")
    patterns = [
        ("order_no_like", "ZZZ_%"),
        ("title_like", "[TEST]%"),
        ("title_like", "%account_label 검증%"),
    ]
    with httpx.Client(base_url=base, timeout=10.0) as client:
        for key, val in patterns:
            try:
                r = client.delete("/purchase-records", params={key: val})
                print(f"  {key}={val}: {r.status_code} {r.text[:120]}")
            except Exception as exc:  # noqa: BLE001
                print(f"  {key}={val} 실패: {exc}")


if __name__ == "__main__":
    cleanup_local()
    target = None
    if len(sys.argv) > 1:
        target = sys.argv[1]
    else:
        try:
            from inventory_app.config import load_config
            target = load_config().monitor_url
        except Exception:
            target = None
    if target:
        cleanup_pi(target)
    else:
        print("(Pi monitor_url 없음 → 로컬만 정리)")
    print("완료")
