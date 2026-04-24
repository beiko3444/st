"""마스터 상품 집계 서비스.

채널별 raw rows + channel_master_links → 마스터별 집계 view 생성.

multiplier 의미:
- 마스터 = 사용자가 정의하는 1 단위
- 채널 상품 링크의 multiplier = 채널 상품 1 을 마스터 단위로 환산한 수량
  (예: 마스터 = 1개, 네이버 "10팩 상품" 링크 multiplier=10 → 10팩 1건 팔리면 마스터 10개)

합산 규칙:
- stock, sales, today_sales 는 multiplier 곱해 마스터 단위로 합산
- None 값: 그 채널에 데이터가 하나도 없으면 None 유지. 일부 None/일부 값이면 None 을 0 으로 간주.
- price 는 곱하지 않음 (pack 당 가격). master row 에 별도 집계 하지 않고 link 레벨에서만 노출.
- image_url: 대표 링크(master.representative_*) 의 이미지 우선, 없으면 첫 링크.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

from inventory_app.models import ChannelMasterLink, ChannelProduct, MasterProduct
from inventory_app.services.local_cache import ChannelProductCache
from inventory_app.services.master_remote_client import (
    MasterRemoteClient,
    MasterRemoteError,
)
from inventory_app.services.shared_stock_grouping import product_identity_key


@dataclass
class LinkedChannelView:
    """마스터에 연결된 채널 상품 1건의 view (multiplier 적용 전 원본 값 포함)."""
    channel: str
    product_key: str
    name: str
    image_url: Optional[str]
    product_url: Optional[str]
    stock: Optional[int]
    sales: Optional[int]
    today_sales: Optional[int]
    price: Optional[int]
    multiplier: int
    synced_at: datetime


@dataclass
class MasterProductRow:
    """마스터 1개 + 연결된 채널 상품 집계 결과."""
    master: MasterProduct
    naver_stock: Optional[int] = None
    coupang_stock: Optional[int] = None
    naver_sales: Optional[int] = None
    coupang_sales: Optional[int] = None
    naver_today_sales: Optional[int] = None
    coupang_today_sales: Optional[int] = None
    image_url: Optional[str] = None
    naver_url: Optional[str] = None
    coupang_url: Optional[str] = None
    linked: List[LinkedChannelView] = field(default_factory=list)

    @property
    def total_stock(self) -> Optional[int]:
        return _sum_channels(self.naver_stock, self.coupang_stock)

    @property
    def total_sales(self) -> Optional[int]:
        return _sum_channels(self.naver_sales, self.coupang_sales)

    @property
    def total_today_sales(self) -> Optional[int]:
        return _sum_channels(self.naver_today_sales, self.coupang_today_sales)


@dataclass
class MasterAggregation:
    """전체 집계 결과."""
    masters: List[MasterProductRow]
    unlinked_by_channel: Dict[str, List[ChannelProduct]]
    synced_at: datetime


def _sum_channels(*values: Optional[int]) -> Optional[int]:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return sum(present)


def _apply_multiplier(value: Optional[int], multiplier: int) -> Optional[int]:
    if value is None:
        return None
    return int(value) * max(1, int(multiplier))


def _accumulate(
    current: Optional[int],
    addition: Optional[int],
) -> Optional[int]:
    """None 처리: 둘 다 None 이면 None. 한쪽만 None 이면 0 으로 간주."""
    if current is None and addition is None:
        return None
    return int(current or 0) + int(addition or 0)


class MasterProductService:
    """마스터 상품 + 채널 링크 기반 집계 서비스.

    - UI 레이어에서 채널별 raw rows (shared_stock 적용 전) 를 공급하면
      cache 에서 masters/links 를 읽어 MasterAggregation 반환.
    """

    def __init__(
        self,
        cache: Optional[ChannelProductCache] = None,
        remote: Optional[MasterRemoteClient] = None,
    ) -> None:
        self.cache = cache or ChannelProductCache()
        self.remote = remote  # None 이면 로컬 단독 (레거시) 모드

    def has_remote(self) -> bool:
        return self.remote is not None

    # ------------------------------------------------------------------
    # 원격 → 로컬 캐시 동기화
    # ------------------------------------------------------------------

    def refresh_from_remote(self) -> None:
        """Pi 의 마스터/링크 전량을 로컬 캐시에 적재.
        Pi 미설정/오류 시 MasterRemoteError 가 raise 되며 로컬 캐시는 건드리지 않는다.
        """
        if self.remote is None:
            return
        masters = self.remote.list_masters()
        links = self.remote.list_all_links()
        self.cache.replace_all_masters_and_links(masters, links)

    # ------------------------------------------------------------------
    # Master CRUD (write-through: remote 우선, 성공 시 로컬 캐시 반영)
    # ------------------------------------------------------------------

    def list_masters(self) -> List[MasterProduct]:
        # 읽기는 로컬 캐시에서만 (속도/오프라인 내성). 신선도는 refresh_from_remote() 로 관리.
        return self.cache.list_masters()

    def create_master(
        self,
        name: str,
        unit_cost: int | None = None,
        memo: str | None = None,
    ) -> MasterProduct:
        if self.remote is not None:
            master = self.remote.create_master(name, unit_cost=unit_cost, memo=memo)
            self.cache.upsert_master_row(master)
            return master
        return self.cache.create_master(name, unit_cost=unit_cost, memo=memo)

    def update_master(self, master_id: int, **kwargs) -> None:
        if self.remote is not None:
            master = self.remote.update_master(master_id, **kwargs)
            self.cache.upsert_master_row(master)
            return
        self.cache.update_master(master_id, **kwargs)

    def delete_master(self, master_id: int) -> None:
        if self.remote is not None:
            self.remote.delete_master(master_id)
            # 로컬도 삭제 (FK CASCADE 로 링크도 같이 사라짐)
            self.cache.delete_master(master_id)
            return
        self.cache.delete_master(master_id)

    def set_representative(
        self,
        master_id: int,
        channel: str | None,
        product_key: str | None,
    ) -> None:
        if self.remote is not None:
            master = self.remote.set_master_representative(
                master_id, channel, product_key
            )
            self.cache.upsert_master_row(master)
            return
        self.cache.set_master_representative(master_id, channel, product_key)

    # ------------------------------------------------------------------
    # Link CRUD (write-through)
    # ------------------------------------------------------------------

    def link(
        self,
        channel: str,
        product_key: str,
        master_id: int,
        multiplier: int = 1,
    ) -> None:
        if self.remote is not None:
            link = self.remote.link(channel, product_key, master_id, multiplier)
            self.cache.upsert_link_row(link)
            return
        self.cache.link_channel_product(channel, product_key, master_id, multiplier)

    def unlink(self, channel: str, product_key: str) -> None:
        if self.remote is not None:
            self.remote.unlink(channel, product_key)
            self.cache.unlink_channel_product(channel, product_key)
            return
        self.cache.unlink_channel_product(channel, product_key)

    def set_multiplier(self, channel: str, product_key: str, multiplier: int) -> None:
        if self.remote is not None:
            link = self.remote.set_link_multiplier(channel, product_key, multiplier)
            self.cache.upsert_link_row(link)
            return
        self.cache.set_link_multiplier(channel, product_key, multiplier)

    def get_link(self, channel: str, product_key: str) -> ChannelMasterLink | None:
        return self.cache.get_link(channel, product_key)

    # ------------------------------------------------------------------
    # Aggregation
    # ------------------------------------------------------------------

    def aggregate(
        self,
        rows_by_channel: Dict[str, Iterable[ChannelProduct]],
    ) -> MasterAggregation:
        """채널별 raw rows 를 받아 마스터별 집계 결과 반환.

        rows_by_channel: {"naver": [...], "coupang": [...]} 형태.
        shared_stock 이 적용된 rows 를 넣으면 중복집계 되므로 raw rows 를 줘야 함.
        """
        masters = self.cache.list_masters()
        links = self.cache.load_all_links()

        master_by_id: Dict[int, MasterProductRow] = {
            m.id: MasterProductRow(master=m) for m in masters
        }

        unlinked: Dict[str, List[ChannelProduct]] = {}

        for channel, rows in rows_by_channel.items():
            channel_rows = list(rows)
            unlinked_rows: List[ChannelProduct] = []
            for row in channel_rows:
                product_key = product_identity_key(row)
                link = links.get((channel, product_key))
                if link is None or link.master_id not in master_by_id:
                    unlinked_rows.append(row)
                    continue
                master_row = master_by_id[link.master_id]
                self._apply_link_to_master(master_row, channel, row, link)
            unlinked[channel] = unlinked_rows

        # 대표이미지/URL 결정 — 대표 링크 지정값 우선, 없으면 첫 linked
        for master_row in master_by_id.values():
            self._resolve_representative_assets(master_row)

        # 정렬: 이름 기준
        ordered = sorted(
            master_by_id.values(),
            key=lambda mr: (mr.master.name, mr.master.id),
        )

        return MasterAggregation(
            masters=ordered,
            unlinked_by_channel=unlinked,
            synced_at=datetime.now(),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _apply_link_to_master(
        self,
        master_row: MasterProductRow,
        channel: str,
        row: ChannelProduct,
        link: ChannelMasterLink,
    ) -> None:
        multiplier = max(1, int(link.multiplier))
        linked_view = LinkedChannelView(
            channel=channel,
            product_key=link.product_key,
            name=row.name,
            image_url=row.image_url,
            product_url=row.product_url,
            stock=row.stock,
            sales=row.sales,
            today_sales=row.today_sales,
            price=row.price,
            multiplier=multiplier,
            synced_at=row.synced_at,
        )
        master_row.linked.append(linked_view)

        stock_add = _apply_multiplier(row.stock, multiplier)
        sales_add = _apply_multiplier(row.sales, multiplier)
        today_add = _apply_multiplier(row.today_sales, multiplier)

        if channel == "naver":
            master_row.naver_stock = _accumulate(master_row.naver_stock, stock_add)
            master_row.naver_sales = _accumulate(master_row.naver_sales, sales_add)
            master_row.naver_today_sales = _accumulate(
                master_row.naver_today_sales, today_add
            )
            if not master_row.naver_url and row.product_url:
                master_row.naver_url = row.product_url
        elif channel == "coupang":
            master_row.coupang_stock = _accumulate(master_row.coupang_stock, stock_add)
            master_row.coupang_sales = _accumulate(master_row.coupang_sales, sales_add)
            master_row.coupang_today_sales = _accumulate(
                master_row.coupang_today_sales, today_add
            )
            if not master_row.coupang_url and row.product_url:
                master_row.coupang_url = row.product_url

    def _resolve_representative_assets(self, master_row: MasterProductRow) -> None:
        if not master_row.linked:
            return

        rep_channel = master_row.master.representative_channel
        rep_key = master_row.master.representative_product_key
        representative: LinkedChannelView | None = None
        if rep_channel and rep_key:
            for link in master_row.linked:
                if link.channel == rep_channel and link.product_key == rep_key:
                    representative = link
                    break

        if representative is None:
            # 대표 미지정 시 이미지가 있는 첫 링크를 fallback
            for link in master_row.linked:
                if link.image_url:
                    representative = link
                    break
        if representative is None:
            representative = master_row.linked[0]

        master_row.image_url = representative.image_url
