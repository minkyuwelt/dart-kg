"""DART 주요정보 API의 값 문자열을 파이썬 값으로. 표마다 같은 꼴이라 여기 한곳에 둔다 (출자·최대주주·임원·겸직 표가 같이 쓴다).

응답 형식에서 확인한 것 (tests/fixtures/*_00688996_2025_11011.json, 2026-11-23)

| 항목     | 형식                                                    | 함수                          |
|----------|---------------------------------------------------------|-------------------------------|
| 날짜     | '2025-12-31', '2019년 12월 31일', '2019.01.11', '20190111' | parse_date                    |
| 생년월   | '1961년 06월', '1961.06'                                 | parse_year_month → '1961-06'  |
| 수       | '33,107,971', '-'                                        | to_number (쉼표 제거, '-'는 None) |
| 지분율   | 현황 '8.68', 변동현황 '9.97%'                            | parse_percent (% 제거)        |
| 빈 값    | '', '-'                                                  | text_or_none → None           |
| 자리 표시 | '-', '해당없음', '기재생략' (사람·회사가 아닌 행)         | is_placeholder                |

출자 표의 frst_acqs_de·stlm_dt는 아직 SQL(TRY_STRPTIME, investments.py)로 바꾼다. 받는 형식이 '2019.01.11' 하나뿐인지
노트북에서 확인한 뒤 parse_date로 옮길 것 (0005 "다시 볼 것").
"""

import re
from datetime import date

DATE_PATTERNS = (
    re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$"),  # 2025-12-31
    re.compile(r"^(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일$"),  # 2019년 12월 31일
    re.compile(r"^(\d{4})\.(\d{1,2})\.(\d{1,2})$"),  # 2019.01.11
    re.compile(r"^(\d{4})(\d{2})(\d{2})$"),  # 20190111
)
YEAR_MONTH_PATTERNS = (
    re.compile(r"^(\d{4})\s*년\s*(\d{1,2})\s*월$"),  # 1961년 06월
    re.compile(r"^(\d{4})[.-](\d{1,2})$"),  # 1961.06, 1961-06
)
# 임원 현황에서 사람이 아닌 자리 표시 행
PLACEHOLDER_NAMES = {"-", "해당없음", "해당사항없음", "없음", "기재생략"}


def is_placeholder(name: str) -> bool:
    """사람·회사 이름 칸이 '-', '해당없음', '기재 생략'처럼 자리 표시인가. 공백은 떼고 비교한다 ('해당 사항 없음')."""
    return re.sub(r"\s+", "", name) in PLACEHOLDER_NAMES


def to_number(text: str | None) -> float | None:
    """'1,234,567' → 1234567.0, '-' 또는 '' → None"""
    if text is None:
        return None
    s = text.replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def clean(text: str | None) -> str:
    return (text or "").strip()


def text_or_none(text: str | None) -> str | None:
    """비어 있거나 '-'(DART의 "해당 없음" 표기)이면 None, 아니면 앞뒤 공백을 뗀 문자열."""
    s = clean(text)
    return None if s in ("", "-") else s


def parse_date(text: str | None) -> date | None:
    """DART 주요정보 API의 날짜 세 형식을 date로 바꾼다. 비어 있거나 '-'이면 None."""
    s = clean(text)
    if s in ("", "-"):
        return None
    for pattern in DATE_PATTERNS:
        m = pattern.match(s)
        if m:
            try:
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except ValueError:
                return None
    return None


def parse_year_month(text: str | None) -> str | None:
    """'1961년 06월' → '1961-06'. 비어 있으면 None."""
    s = clean(text)
    for pattern in YEAR_MONTH_PATTERNS:
        m = pattern.match(s)
        if m:
            return f"{int(m.group(1)):04d}-{int(m.group(2)):02d}"
    return None


def parse_percent(text: str | None) -> float | None:
    """'9.97%' → 9.97, '8.68' → 8.68, '-' → None."""
    return to_number(clean(text).rstrip("%"))
