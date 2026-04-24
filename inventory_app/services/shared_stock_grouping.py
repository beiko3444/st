"""채널 상품의 안정된 product_key 를 계산하는 유틸.

과거 이 모듈은 상품명 패턴 기반 "공유재고" 자동 그룹핑 로직을 담았으나,
마스터 상품 연결(channel_master_links) 기반 수동 매칭으로 대체되며
`product_identity_key` 함수만 남았다. 이 함수는 캐시·링크 테이블에서
채널 상품을 식별하는 단일 진실 공급원 역할을 한다.
"""

from __future__ import annotations

from inventory_app.models import ChannelProduct


def product_identity_key(row: ChannelProduct) -> str:
    """캐시 / 마스터 링크 테이블에서 쓰는 안정적 product_key 생성.

    우선순위:
    1. product_id + item_id (있으면)
    2. product_url (상품 식별 URL)
    3. name (최후 fallback)
    """
    if row.product_id:
        return f"id:{row.product_id}|item:{row.item_id or ''}"
    if row.product_url:
        return f"url:{row.product_url}"
    return f"name:{row.name}"
