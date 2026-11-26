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


# "2025년", "2025년말", "2025년도", "2025년중" 같은 연도 낱말 (조사가 붙은 "2025년의"도 같은 꼴)
_YEAR_WORD_RE = re.compile(r"^((?:19|20)\d{2})년[가-힣]{0,2}$")
# 낱말 끝의 문장부호 ("무엇인가?", "연혁은," → "무엇인가", "연혁은")
_TRAIL_PUNCT_RE = re.compile(r"[?？!！.,、:;]+$")
# 조사: 긴 것부터 본다. 뗀 뒤 2글자 이상 남을 때만 뗀다 ("회사의" → "회사", "연혁은" → "연혁", "KB금융의" → "KB금융")
_PARTICLES = ("에서는", "에서", "으로", "에게", "부터", "까지", "처럼", "보다", "이며", "이고",
              "의", "은", "는", "이", "가", "을", "를", "과", "와", "도", "로", "에", "만")
_HANGUL_END_RE = re.compile(r"[가-힣]$")
# 질문 끝말·물음말: 공시 글에 나오지 않아 검색어로는 소음. 통째로 버린다
_QUESTION_WORDS = {"무엇인가", "무엇", "무엇이며", "무엇이었나", "누구인가", "누구", "누구였나", "어디인가", "어디", "얼마인가", "얼마",
                   "얼마였나", "어떻게", "어떤", "있는가", "있나", "있었나", "인가", "몇", "몇인가", "언제인가", "언제", "왜", "중"}


def _word_forms(raw: str) -> list[str]:
    """낱말 하나의 검색 꼴들: 문장부호를 떼고, 조사가 붙었으면 뗀 꼴도 함께. 물음말이면 빈 목록."""
    w = _TRAIL_PUNCT_RE.sub("", raw)
    if not w or w in _QUESTION_WORDS:
        return []
    forms = [w]
    if _HANGUL_END_RE.search(w):
        for p in _PARTICLES:
            stem = w[: -len(p)]
            if w.endswith(p) and len(stem) >= 2 and not stem[-1].isdigit():   # "1,400만"의 '만'은 조사가 아니라 단위
                forms.append(stem)
                break
    return forms


def _term(raw: str) -> str | None:
    """낱말 하나 → Lucene 조각. 기본은 전처리한 구절 "…". 꼴이 여럿이면 (a OR b).

    - 조사 뗀 꼴 (2026-11-26, d033): "연혁은"은 조각 꼬리표 "… > 2. 회사의 연혁"과 구절이 맞지 않는다(두 글자 조각 "혁은"이 없음).
      "케이비국민카드의"도 같다. 그래서 조사를 뗀 "연혁", "케이비국민카드"를 OR로 함께 넣는다. 형태소 분석기 없이(0004: nori 없음, kiwi 제외)
      끝 글자 목록으로만 떼므로 틀리게 뗄 수 있지만, 원래 꼴도 함께 들어가고 OR이라 틀린 꼴은 아무것도 안 맞을 뿐이다.
    - 연도 낱말("2025년")은 '("2025 년" OR 2025*)'로 넓힌다. 공시는 연도를 날짜 "2025.02.18"로 적는 곳이 많고(연혁·수상·발행 실적)
      토큰 "2025.02.18"은 구절 "2025 년"과 맞지 않는다. 앞부분 일치 2025*는 둘 다 잡는다. 색인은 그대로라 다시 적재할 필요가 없다.
    """
    pieces = []
    for form in _word_forms(raw):
        norm = normalize_for_fulltext(form)
        if norm:
            pieces.append(f'"{_escape_lucene_phrase(norm)}"')
    m = _YEAR_WORD_RE.match(_TRAIL_PUNCT_RE.sub("", raw))
    if m:
        pieces.append(f"{m.group(1)}*")
    if not pieces:
        return None
    return pieces[0] if len(pieces) == 1 else "(" + " OR ".join(pieces) + ")"


def fulltext_query(question: str) -> str:
    """사용자가 띄어 쓴 낱말마다 **따옴표 안에서** 전처리한 구절 검색, 낱말끼리는 **OR**.

    'KB금융' → '"KB 금융"' (KB와 금융이 붙어 있을 때만), '대표이사 변경' → '"대표이사" OR "변경"'.
    전처리를 따옴표 밖에서 하면 'KB금융' → '"KB" AND "금융"'이 되어 두 조각이 문서 아무 데나 따로 있어도 걸린다(PR #13 리뷰).

    낱말끼리 OR인 이유 (2026-11-26, AND에서 바꿈 — 0004 결정 2 수정): AND는 0004 실험의 낱말 검색어("최대주주", "대표이사 변경")에는 맞았지만,
    실제 질문은 문장이다. "케이비국민카드의 2025년 중 주요 연혁은 무엇인가?"를 AND로 묶으면 "무엇인가?"가 든 조각이 없어 **결과가 0개**였고,
    하이브리드(0010)의 전문검색 쪽은 9a 이후 모든 문장 질문에서 비어 있었다(벡터만의 hit@5와 하이브리드가 같았다). OR이면 많이 맞는 조각이
    BM25로 위에 오고, 정밀도는 낱말마다의 구절 검색과 벡터와의 RRF가 맡는다. Lucene의 기본 연산자도 OR이다.
    빈 낱말은 버린다. 낱말이 하나도 없으면 빈 문자열 — 호출하는 쪽에서 전문검색을 건너뛴다.
    """
    terms = [_term(t) for t in question.split()]
    return " OR ".join(t for t in terms if t)
