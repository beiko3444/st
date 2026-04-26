"""카드 사용내역 자동 카테고리 분류.

beico-app/lib/cardCategory.ts 의 규칙을 Python 으로 포팅.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional


@dataclass(frozen=True)
class CategoryMeta:
    code: str
    label: str
    emoji: str
    bg_color: str


DEFAULT_CATEGORIES: List[CategoryMeta] = [
    CategoryMeta("CAFE",          "카페",     "☕",  "#FFF3E0"),
    CategoryMeta("FOOD",          "음식",     "🍽️", "#FFF8E1"),
    CategoryMeta("BAKERY",        "베이커리", "🥖",  "#FFF8E1"),
    CategoryMeta("TRANSPORT",     "교통",     "🚆",  "#E3F2FD"),
    CategoryMeta("SHOPPING",      "쇼핑",     "🛒",  "#E8F5E9"),
    CategoryMeta("CONVENIENCE",   "편의점",   "🛍️", "#E8F5E9"),
    CategoryMeta("FUEL",          "주유",     "⛽",  "#FFF3E0"),
    CategoryMeta("FINANCE",       "금융",     "💳",  "#F3E5F5"),
    CategoryMeta("TELECOM",       "통신",     "📱",  "#E8EAF6"),
    CategoryMeta("OFFICE",        "사무",     "🖨️", "#ECEFF1"),
    CategoryMeta("MEDICAL",       "의료",     "🏥",  "#E8F5E9"),
    CategoryMeta("EDUCATION",     "교육",     "📚",  "#E3F2FD"),
    CategoryMeta("ENTERTAINMENT", "문화",     "🎬",  "#FCE4EC"),
    CategoryMeta("OTHER",         "기타",     "📦",  "#F5F5F5"),
]

CATEGORY_MAP = {c.code: c for c in DEFAULT_CATEGORIES}


# 분류 규칙 — 순서 중요 (먼저 매칭이 우선)
_RULES: List[tuple[str, re.Pattern[str]]] = [
    ("CAFE", re.compile(
        r"카페|커피|cafe|coffee|미루|스타벅스|이디야|투썸|빽다방|메가|컴포즈|할리스|"
        r"엔제리너스|폴바셋|텐퍼센트|공차|파스쿠찌|드롭탑|빈스빈스|매머드|더벤티|바나프레소|로스터리",
        re.I,
    )),
    ("BAKERY", re.compile(
        r"베이커리|빵|파리바게뜨|뚜레쥬르|성심당|크로와상|도넛|던킨|크리스피|베이글|제과",
        re.I,
    )),
    ("FOOD", re.compile(
        r"요리사|식당|레스토랑|밥|치킨|피자|버거|맥도날드|롯데리아|bbq|bhc|교촌|한솥|"
        r"김밥|떡볶이|분식|맘스|배달|요기요|배민|쿠팡이츠|써브웨이|subway|오스루|음식|"
        r"반찬|도시락|초밥|스시|라멘|우동|냉면|국수|불고기|삼겹|쭈꾸미|족발|보쌈|순대|"
        r"곱창|감자탕|찌개|탕|중국집|짜장|짬뽕|볶음|한식|일식|양식|중식",
        re.I,
    )),
    ("TRANSPORT", re.compile(
        r"코레일|ktx|srt|기차|철도|택시|카카오T|카카오택시|타다|고속|시외|버스|"
        r"항공|대한항공|아시아나|제주항공|티웨이|진에어|에어부산|에어서울|이스타|"
        r"주차|파킹|톨게이트|하이패스",
        re.I,
    )),
    ("CONVENIENCE", re.compile(
        r"편의점|cu\b|gs25|세븐일레븐|이마트24|미니스톱|씨유|지에스",
        re.I,
    )),
    ("SHOPPING", re.compile(
        r"네이버|쿠팡|gmarket|옥션|11번가|위메프|tmon|아마존|amazon|쇼핑|마트|"
        r"이마트|홈플러스|코스트코|롯데마트|다이소|올리브영|무신사|ssg|인터파크",
        re.I,
    )),
    ("FUEL", re.compile(
        r"주유|gs칼텍스|sk에너지|s-oil|현대오일|충전|오일뱅크",
        re.I,
    )),
    ("FINANCE", re.compile(
        r"헥토|바로빌|은행|보험|카드|금융|증권|투자|자산|펀드|대출|신용|"
        r"국민|우리|하나|신한|농협|기업은행|수협|산업|수출입|새마을|우체국|"
        r"카카오뱅크|토스",
        re.I,
    )),
    ("TELECOM", re.compile(
        r"skt|sk텔레|케이티|kt\b|lg유플|알뜰|통신|인터넷|와이파이",
        re.I,
    )),
    ("OFFICE", re.compile(
        r"사무|문구|오피스|프린트|복사|인쇄|잉크|토너|알파문구|모닝글로리",
        re.I,
    )),
    ("MEDICAL", re.compile(
        r"병원|의원|약국|클리닉|치과|안과|피부과|내과|외과|정형|한의|한방|"
        r"건강검진|약사|메디|팜",
        re.I,
    )),
    ("EDUCATION", re.compile(
        r"학원|교육|학교|대학|강의|인강|클래스|아카데미|수강|입시",
        re.I,
    )),
    ("ENTERTAINMENT", re.compile(
        r"영화|CGV|메가박스|롯데시네마|극장|공연|콘서트|노래방|PC방|게임|"
        r"볼링|당구|헬스|피트니스|짐|gym|스크린골프|골프|스파|사우나|찜질방|마사지",
        re.I,
    )),
]


def classify_category(store_name: Optional[str], biz_type: Optional[str] = None) -> str:
    """가맹점명/업종에서 카테고리 코드 추출. 매칭 안 되면 OTHER."""
    text = (store_name or "").strip()
    if biz_type:
        text = f"{text} {biz_type}"
    if not text:
        return "OTHER"
    for code, pattern in _RULES:
        if pattern.search(text):
            return code
    return "OTHER"


def category_meta(code: str) -> CategoryMeta:
    return CATEGORY_MAP.get(code, CATEGORY_MAP["OTHER"])


__all__ = [
    "CategoryMeta",
    "DEFAULT_CATEGORIES",
    "CATEGORY_MAP",
    "classify_category",
    "category_meta",
]
