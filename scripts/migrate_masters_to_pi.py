"""로컬 SQLite 의 master_products / channel_master_links 를 라즈베리파이로 1회 이전.

사용:
  .venv/bin/python scripts/migrate_masters_to_pi.py --dry-run   # 미리보기
  .venv/bin/python scripts/migrate_masters_to_pi.py             # 실제 전송

Pi 의 id 는 재발급되기 때문에, 로컬 master_id → Pi master_id 매핑을 만들어
링크 POST 시 그 매핑을 적용한다.

완료 후 로컬 캐시는 Pi 에서 다시 fetch 해서 replace 된다 (MasterProductService.refresh_from_remote).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from inventory_app.config import load_config
from inventory_app.services.local_cache import ChannelProductCache
from inventory_app.services.master_remote_client import (
    MasterRemoteClient,
    MasterRemoteError,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="실제 전송 없이 계획만 출력")
    parser.add_argument(
        "--monitor-url",
        default=None,
        help="override monitor URL (기본: credentials.json 의 monitor.url)",
    )
    args = parser.parse_args()

    config = load_config()
    monitor_url = args.monitor_url or config.monitor_url
    if not monitor_url:
        print("❌ monitor.url 이 설정되어 있지 않습니다.", file=sys.stderr)
        return 1

    cache = ChannelProductCache()
    local_masters = cache.list_masters()
    local_links = list(cache.load_all_links().values())
    print(f"로컬: 마스터 {len(local_masters)}개, 링크 {len(local_links)}개")

    remote = MasterRemoteClient(monitor_url, timeout=20.0)
    try:
        existing_remote = remote.list_masters()
    except MasterRemoteError as exc:
        print(f"❌ Pi 연결 실패: {exc}", file=sys.stderr)
        return 2

    if existing_remote and not args.dry_run:
        print(
            f"⚠ Pi 에 이미 {len(existing_remote)}개의 마스터가 존재합니다. "
            "중복 생성될 수 있으니, 이전 데이터 정리 후 다시 시도하거나 --dry-run 으로 확인하세요."
        )
        reply = input("그래도 진행할까요? [y/N] ").strip().lower()
        if reply != "y":
            print("취소")
            return 0

    print("\n=== 계획 ===")
    for m in local_masters:
        print(f"  master#{m.id} '{m.name}' cost={m.unit_cost} rep={m.representative_channel}/{m.representative_product_key}")
    for lk in local_links:
        print(f"  link {lk.channel}/{lk.product_key} -> master#{lk.master_id} x{lk.multiplier}")

    if args.dry_run:
        print("\n(dry-run) 실제 전송하지 않음.")
        return 0

    id_map: dict[int, int] = {}

    print("\n=== 마스터 생성 ===")
    for m in local_masters:
        try:
            created = remote.create_master(name=m.name, unit_cost=m.unit_cost, memo=m.memo)
        except MasterRemoteError as exc:
            print(f"  ❌ master#{m.id} '{m.name}': {exc}", file=sys.stderr)
            continue
        id_map[m.id] = created.id
        print(f"  ✅ local#{m.id} -> pi#{created.id} '{created.name}'")

    print("\n=== 링크 생성 ===")
    for lk in local_links:
        new_master_id = id_map.get(lk.master_id)
        if new_master_id is None:
            print(f"  ⚠ link {lk.channel}/{lk.product_key}: 매핑된 마스터 없음 (local#{lk.master_id}), 건너뜀")
            continue
        try:
            remote.link(lk.channel, lk.product_key, new_master_id, multiplier=lk.multiplier)
            print(f"  ✅ {lk.channel}/{lk.product_key} -> pi#{new_master_id} x{lk.multiplier}")
        except MasterRemoteError as exc:
            print(f"  ❌ {lk.channel}/{lk.product_key}: {exc}", file=sys.stderr)

    print("\n=== 대표 설정 ===")
    for m in local_masters:
        if not m.representative_channel or not m.representative_product_key:
            continue
        new_id = id_map.get(m.id)
        if new_id is None:
            continue
        try:
            remote.set_master_representative(
                new_id, m.representative_channel, m.representative_product_key
            )
            print(f"  ✅ pi#{new_id} rep={m.representative_channel}/{m.representative_product_key}")
        except MasterRemoteError as exc:
            print(f"  ❌ pi#{new_id} rep: {exc}", file=sys.stderr)

    print("\n=== 확인 ===")
    try:
        after = remote.list_masters()
        print(f"Pi 현재 마스터 수: {len(after)}")
    except MasterRemoteError as exc:
        print(f"확인 실패: {exc}", file=sys.stderr)
        return 3

    print("\n완료. 앱을 다시 실행하면 로컬 캐시가 Pi 스냅샷으로 갱신됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
