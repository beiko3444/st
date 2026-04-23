"""공유 재고(묶음 상품) 그룹 자동 제안 + 마스터 집계 유틸.

사용 흐름:
1. extract_pack_size(name)  — 상품명에서 pack_size(1/4/10팩 등) 추출
2. extract_group_key(name)  — 상품명에서 pack_size 토큰 제거한 정규화 키
3. suggest_groups(rows)     — 같은 group_key가 2개 이상인 걸 묶어 SharedStockRule 제안
4. apply_master_aggregation(rows, rules)
   — 비마스터의 today_sales / sales × pack_size 를 마스터 row에 합산
   — 비마스터 row 는 today_sales=0, sales=0 로 세팅

"마스터에 몰아서" 정책:
- 마스터 = 그룹 내 최소 pack_size (보통 1팩)
- 마스터의 today_sales = Σ (회원 row.today_sales × pack_size)
- 마스터의 sales       = Σ (회원 row.sales × pack_size)
- 비마스터 = 0
- price 는 건드리지 않음(SKU 단가 정보는 원본 유지)

과거 데이터 재계산:
- fetch_cached 호출 시점에 rules 를 다시 적용하므로 캐시에 저장된 예전 값도
  현재 규칙 기준으로 재집계됨.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import defaultdict
from typing import Callable, Dict, Iterable, List, Tuple

from inventory_app.models import ChannelProduct, SharedStockRule


# ---------------------------------------------------------------------------
# Pack size / group key extraction
# ---------------------------------------------------------------------------

# 우선순위 순서 (처음 매칭하는 것으로 결정)
_PACK_SIZE_PATTERNS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"(\d+)\s*팩"),          # 4팩, 10 팩
    re.compile(r"(\d+)\s*개입"),        # 4개입
    re.compile(r"(\d+)\s*세트"),        # 10세트
    re.compile(r"세트\s*(\d+)"),         # 세트 10
    re.compile(r"(\d+)\s*묶음"),        # 4묶음
    re.compile(r"(\d+)\s*pk", re.I),   # 4pk
    re.compile(r"x\s*(\d+)", re.I),    # x4, X 10 (곱하기 표기)
    # "4개" — "4개입"/"4개월" 등과 겹치지 않도록 조심 (look-ahead 제한)
    re.compile(r"(\d+)\s*개(?!\s*입|\s*월|\s*년|\s*차)"),
)

# group_key 생성 시 제거할 전체 pack_size 토큰
_PACK_SIZE_TOKEN_PATTERNS: Tuple[re.Pattern[str], ...] = tuple(_PACK_SIZE_PATTERNS)

# 추가로 제거할 불용 토큰 (가격/용량/수량 외 부수 정보)
_STRIP_TOKENS: Tuple[re.Pattern[str], ...] = (
    re.compile(r"\[[^\]]*\]"),       # [이벤트], [NEW]
    re.compile(r"\([^)]*\)"),        # (무료배송), (정품)
    re.compile(r"[/\-_|·,]+"),       # 구분자
    re.compile(r"\s+"),                # 다중 공백
)


def extract_pack_size(name: str) -> int:
    """상품명에서 pack_size 추출. 못 찾으면 1."""
    text = str(name or "")
    for pat in _PACK_SIZE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            value = int(m.group(1))
        except (TypeError, ValueError):
            continue
        if value >= 1:
            return value
    return 1


def extract_group_key(name: str) -> str:
    """그룹핑용 정규화 키. 같은 물리재고 후보를 같은 key 로 맞추기 위함.

    - pack_size 토큰 제거
    - 대괄호/괄호 제거
    - 구분자/공백 정리
    - NFC 정규화, 소문자화
    """
    text = unicodedata.normalize("NFC", str(name or ""))
    for pat in _PACK_SIZE_TOKEN_PATTERNS:
        text = pat.sub(" ", text)
    # 대괄호/괄호 제거
    text = _STRIP_TOKENS[0].sub(" ", text)
    text = _STRIP_TOKENS[1].sub(" ", text)
    # 구분자 → 공백
    text = _STRIP_TOKENS[2].sub(" ", text)
    # 다중 공백 정리
    text = _STRIP_TOKENS[3].sub(" ", text).strip().lower()
    return text


# ---------------------------------------------------------------------------
# Product key — SharedStockRule 테이블의 product_key 와 동일 규칙
# UI 쪽 _name_override_key 와 동일해야 함
# ---------------------------------------------------------------------------


def product_identity_key(row: ChannelProduct) -> str:
    if row.product_id:
        return f"id:{row.product_id}|item:{row.item_id or ''}"
    if row.product_url:
        return f"url:{row.product_url}"
    return f"name:{row.name}"


# ---------------------------------------------------------------------------
# Auto group suggestion
# ---------------------------------------------------------------------------


def _make_group_id(group_key: str) -> str:
    """group_key 로부터 안정적인 group_id 생성.

    앞 40자를 대표 식별 문자열로 두고 해시 suffix 붙여 중복 방지.
    """
    slug = re.sub(r"\s+", "-", group_key).strip("-")
    slug = slug[:40].rstrip("-")
    digest = hashlib.md5(group_key.encode("utf-8")).hexdigest()[:8]
    if not slug:
        return f"grp-{digest}"
    return f"{slug}-{digest}"


def suggest_groups(
    rows: Iterable[ChannelProduct],
    *,
    product_key_fn: Callable[[ChannelProduct], str] = product_identity_key,
    min_members: int = 2,
) -> Dict[str, SharedStockRule]:
    """상품명 패턴 매칭으로 공유재고 그룹 제안.

    반환: {product_key: SharedStockRule}
    - 같은 group_key 를 가진 SKU 가 min_members 이상일 때만 그룹으로 채택
    - 그룹 내 최소 pack_size 를 마스터로 지정 (동률이면 상품명 오름차순 첫 번째)
    """
    grouped: Dict[str, List[Tuple[str, int, str]]] = defaultdict(list)
    for row in rows:
        name = str(row.name or "").strip()
        if not name:
            continue
        key = extract_group_key(name)
        if not key:
            continue
        pack = extract_pack_size(name)
        pk = product_key_fn(row)
        if not pk:
            continue
        grouped[key].append((pk, pack, name))

    rules: Dict[str, SharedStockRule] = {}
    for group_key, members in grouped.items():
        if len(members) < min_members:
            continue
        group_id = _make_group_id(group_key)
        min_pack = min(m[1] for m in members)
        # 동률일 때 어떤 걸 마스터로 할지: 상품명 오름차순 첫 번째
        master_assigned = False
        sorted_members = sorted(members, key=lambda m: (m[1], m[2]))
        for pk, pack, _name in sorted_members:
            is_master = (pack == min_pack) and not master_assigned
            if is_master:
                master_assigned = True
            rules[pk] = SharedStockRule(
                group_id=group_id,
                pack_size=max(1, pack),
                is_master=is_master,
            )
    return rules


# ---------------------------------------------------------------------------
# Master aggregation
# ---------------------------------------------------------------------------


def apply_master_aggregation(
    rows: List[ChannelProduct],
    rules: Dict[str, SharedStockRule],
    *,
    product_key_fn: Callable[[ChannelProduct], str] = product_identity_key,
) -> None:
    """in-place로 rows 에 마스터 집계 적용.

    - 규칙 있는 group 소속 row:
        * 마스터 = Σ(member.today_sales × pack_size), Σ(member.sales × pack_size)
        * 비마스터 = today_sales=0, sales=0
    - 규칙 없는 row: 변경 없음
    - price 는 절대 수정 안 함

    None 값 처리: 멤버 중 today_sales/sales 가 None 이면 0 으로 간주하여 합산.
    마스터에 담을 값이 0 이고 규칙상 데이터가 없었으면 None 유지.
    """
    if not rules:
        return

    # 그룹별로 rows에 실제로 매칭된 member 목록 구성.
    # "마스터 없는 그룹"을 감지하기 위해 먼저 group_has_master를 계산한다.
    group_has_master: Dict[str, bool] = defaultdict(bool)
    for row in rows:
        rule = rules.get(product_key_fn(row))
        if rule is not None and rule.is_master:
            group_has_master[rule.group_id] = True

    # group_id 단위 집계 — 마스터가 있는 그룹만.
    totals_today: Dict[str, int] = defaultdict(int)
    totals_sales: Dict[str, int] = defaultdict(int)
    totals_today_has_data: Dict[str, bool] = defaultdict(bool)
    totals_sales_has_data: Dict[str, bool] = defaultdict(bool)

    for row in rows:
        pk = product_key_fn(row)
        rule = rules.get(pk)
        if rule is None:
            continue
        if not group_has_master.get(rule.group_id):
            # 마스터가 없는 그룹은 집계를 신뢰할 수 없으므로 건드리지 않음.
            continue
        pack = max(1, int(rule.pack_size))

        if row.today_sales is not None:
            totals_today[rule.group_id] += max(0, int(row.today_sales)) * pack
            totals_today_has_data[rule.group_id] = True
        if row.sales is not None:
            totals_sales[rule.group_id] += max(0, int(row.sales)) * pack
            totals_sales_has_data[rule.group_id] = True

    # rows 에 적용 — 마스터 있는 그룹에 한해서만 덮어씀.
    for row in rows:
        pk = product_key_fn(row)
        rule = rules.get(pk)
        if rule is None:
            continue
        if not group_has_master.get(rule.group_id):
            # 안전장치: 마스터 미지정 그룹은 원본 보존 (판매량 사라지지 않음).
            continue
        if rule.is_master:
            row.today_sales = (
                totals_today[rule.group_id]
                if totals_today_has_data[rule.group_id]
                else row.today_sales
            )
            row.sales = (
                totals_sales[rule.group_id]
                if totals_sales_has_data[rule.group_id]
                else row.sales
            )
        else:
            row.today_sales = 0
            row.sales = 0


__all__ = [
    "extract_pack_size",
    "extract_group_key",
    "product_identity_key",
    "suggest_groups",
    "apply_master_aggregation",
]
