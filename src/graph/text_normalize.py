"""한국어 전문검색 전처리 (결정 0004). Neo4j의 `cjk` 분석기 + 구절 검색에 맞춘다.

왜 필요한가: Neo4j에는 한국어 형태소 분석기가 없다. `cjk` 분석기는 글자를 두 글자씩 겹쳐 자르는데,
영문·숫자가 한글에 붙은 덩어리("KB금융지주이며")는 쪼개지 않아 "금융지주"를 못 찾고, 가운뎃점(ㆍ)은
한글 자모로 분류되어 앞뒤 글자와 한 덩어리가 된다. 그래서 색인할 글과 검색어 **둘 다** 같은 전처리를 한다.
한쪽만 바꾸면 검색이 조용히 실패한다 — 그래서 이 함수가 유일한 전처리 자리다 (0004 실험 스크립트도 여기서 import).

0004 실험(검색어 19개): cjk 그대로 재현율 87%/정밀도 49% → 구절 검색 + 이 전처리 97%/93%.
"""

from __future__ import annotations

import re

# DART 보고서명의 가운뎃점은 'ㆍ'(U+318D HANGUL LETTER ARAEA)다. 'ㆍ'·'·'(U+00B7)·'•'·'・' 모두 공백으로
_SEPARATOR_RE = re.compile("[ㆍ·•・]")
# 영문·숫자 ↔ 한글 경계에 공백: "KB국민은행" → "KB 국민은행", "2025년" → "2025 년"
_BOUNDARY_RE = re.compile(r"(?<=[A-Za-z0-9])(?=[가-힣])|(?<=[가-힣])(?=[A-Za-z0-9])")
_SPACES_RE = re.compile(r"\s+")


def normalize_for_fulltext(text: str) -> str:
    """색인할 글(`Chunk.text_ft`)과 검색어 낱말에 똑같이 적용한다. 가운뎃점 → 공백, 영문·숫자와 한글 사이에 공백, 공백 정리."""
    return _SPACES_RE.sub(" ", _BOUNDARY_RE.sub(" ", _SEPARATOR_RE.sub(" ", text))).strip()


def _escape_lucene_phrase(s: str) -> str:
    # 큰따옴표 안이라 특수문자 대부분은 글자로 취급되지만, 따옴표와 역슬래시는 깨뜨린다
    return s.replace("\\", "\\\\").replace('"', '\\"')


def fulltext_query(question: str) -> str:
    """사용자가 띄어 쓴 낱말마다 **따옴표 안에서** 전처리한 구절 검색, 낱말끼리는 AND.

    'KB금융' → '"KB 금융"' (KB와 금융이 붙어 있을 때만), '대표이사 변경' → '"대표이사" AND "변경"'.
    전처리를 따옴표 밖에서 하면 'KB금융' → '"KB" AND "금융"'이 되어 두 조각이 문서 아무 데나 따로 있어도 걸린다(PR #13 리뷰).
    빈 낱말은 버린다. 낱말이 하나도 없으면 빈 문자열 — 호출하는 쪽에서 전문검색을 건너뛴다.
    """
    terms = [normalize_for_fulltext(t) for t in question.split()]
    return " AND ".join(f'"{_escape_lucene_phrase(t)}"' for t in terms if t)
