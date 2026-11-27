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


def _pieces(raw: str) -> list[str]:
    """낱말 하나 → Lucene 조각들(구절 꼴들 + 연도 앞부분 일치). 비면 빈 목록."""
    pieces = []
    for form in _word_forms(raw):
        norm = normalize_for_fulltext(form)
        if norm:
            pieces.append(f'"{_escape_lucene_phrase(norm)}"')
    m = _YEAR_WORD_RE.match(_TRAIL_PUNCT_RE.sub("", raw))
    if m:
        pieces.append(f"{m.group(1)}*")
    return pieces


def _term(raw: str) -> str | None:
    """낱말 하나 → Lucene 조각. 기본은 전처리한 구절 "…". 꼴이 여럿이면 (a OR b).

    - 조사 뗀 꼴 (2026-11-26, d033): "연혁은"은 조각 꼬리표 "… > 2. 회사의 연혁"과 구절이 맞지 않는다(두 글자 조각 "혁은"이 없음).
      "케이비국민카드의"도 같다. 그래서 조사를 뗀 "연혁", "케이비국민카드"를 OR로 함께 넣는다. 형태소 분석기 없이(0004: nori 없음, kiwi 제외)
      끝 글자 목록으로만 떼므로 틀리게 뗄 수 있지만, 원래 꼴도 함께 들어가고 OR이라 틀린 꼴은 아무것도 안 맞을 뿐이다.
    - 연도 낱말("2025년")은 '("2025 년" OR 2025*)'로 넓힌다. 공시는 연도를 날짜 "2025.02.18"로 적는 곳이 많고(연혁·수상·발행 실적)
      토큰 "2025.02.18"은 구절 "2025 년"과 맞지 않는다. 앞부분 일치 2025*는 둘 다 잡는다. 색인은 그대로라 다시 적재할 필요가 없다.
    """
    pieces = _pieces(raw)
    if not pieces:
        return None
    return pieces[0] if len(pieces) == 1 else "(" + " OR ".join(pieces) + ")"


def is_section_word(raw: str, section_words: set[str]) -> bool:
    """낱말(또는 조사 뗀 꼴)이 섹션 이름 사전의 낱말을 **품고** 있나. 사전은 section_vocabulary()로 만든다.

    같음이 아니라 품음으로 보는 이유: 섹션 이름은 "배당에 관한 사항"인데 질문은 "현금배당금"·"배당금은"이라고 쓴다. 사전 낱말이 2글자 이상이라
    "배당"·"임원"·"연혁"처럼 섹션을 가리키는 말이 질문 낱말 안에 들어 있으면 그 섹션을 묻는 것으로 본다.
    """
    forms = [f for f in _word_forms(raw) if len(f) >= 2]
    return any(w in f and not _is_verb_form(f, w) for f in forms for w in section_words)


# 명사 뒤에 붙어 동사를 만드는 어미의 첫 글자. "측정하고"·"관리하는가"·"변동되었나"·"상장시켰나"
_VERB_SUFFIX_RE = re.compile(r"^(?:하|해|했|되|돼|됐|시키|시켜|시켰)")


def _is_verb_form(form: str, word: str) -> bool:
    """사전 낱말 바로 뒤가 '하·되·시키'로 이어지면 동사꼴로 본다 (2026-11-27, d005).

    왜: 섹션 사전에 "측정"(공정가치 측정)·"관리"(위험관리)가 있어 "측정하고"·"관리하는가"까지 섹션 낱말이 되었고, 셋이 AND로 묶여
    전문검색이 0개였다. 섹션 이름은 명사로 쓰고, 질문의 "명사+하다"는 동작을 묻는 말이라 공시 글에 글자 그대로 나오는 일이 드물다.
    동사꼴은 필수에서 빼고 선택 낱말로만 둔다. 사전 낱말이 낱말 끝에 있으면("위험관리") 명사다.
    """
    i = form.find(word)
    while i >= 0:
        tail = form[i + len(word):]
        if not tail or not _VERB_SUFFIX_RE.match(tail):
            return False
        i = form.find(word, i + 1)
    return True


def section_terms(question: str, section_words: set[str]) -> list[str]:
    """질문에서 섹션 사전 낱말을 품은 낱말들의 꼴(조사 뗀 꼴 포함, 2글자 이상). 검색기가 '섹션 경로에 이 말이 든 조각을 먼저' 놓는 데 쓴다."""
    out: list[str] = []
    for w in question.split():
        if is_section_word(w, section_words):
            out += [f for f in _word_forms(w) if len(f) >= 2 and f not in out]
    return out


def fulltext_query(question: str, section_words: set[str] | None = None) -> str:
    """사용자가 띄어 쓴 낱말마다 **따옴표 안에서** 전처리한 구절 검색.

    'KB금융' → '"KB 금융"' (KB와 금융이 붙어 있을 때만). 전처리를 따옴표 밖에서 하면 'KB금융' → '"KB" AND "금융"'이 되어
    두 조각이 문서 아무 데나 따로 있어도 걸린다(PR #13 리뷰).

    낱말을 잇는 방식 (2026-11-26, d033 진단으로 바꿈 — 0004 결정 2 수정):
    - AND(처음 방식)는 0004 실험의 낱말 검색어에는 맞았지만 실제 질문은 문장이다. "…연혁은 무엇인가?"는 "무엇인가?"가 든 조각이 없어
      **결과가 0개**였고, 하이브리드(0010)의 전문검색 쪽은 9a 이후 모든 문장 질문에서 비어 있었다(벡터만의 hit@5와 같았다).
    - 전부 OR로 바꾸자 결과는 나왔지만 너무 느슨했다: RRF는 두 목록의 1위를 같은 점수로 놓아 상위 5개의 절반이 전문검색에서 오는데,
      "케이비국민카드"·"2025년"·"주요"가 같은 비중이라 연혁 조각이 재무 표 조각과 구분되지 않았고 서술 질문의 섹션 hit@5가 0.857 → 0.429로 떨어졌다.
    - 그래서 **섹션 이름 낱말만 필수(+), 나머지는 선택**: `section_words`(조각 섹션 경로의 낱말 사전)에 있는 낱말은 `+("연혁은" OR "연혁")`,
      그 밖("2025년", "주요")은 있으면 점수만 올린다. 필수 낱말이 하나도 없으면 빈 문자열을 돌려주어 전문검색을 건너뛴다(벡터만).
      뜻: 전문검색은 "질문이 어느 섹션을 말하는지 알 때, 그 섹션 안에서 나머지 낱말로 순위를 매기는 용도"다.
    - `section_words`가 None이면(사전을 만들 DuckDB가 없을 때) 낱말끼리 OR.
    """
    words = question.split()
    if section_words is None:
        terms = [_term(w) for w in words]
        return " OR ".join(t for t in terms if t)
    required, optional = [], []
    for w in words:
        t = _term(w)
        if t:
            (required if is_section_word(w, section_words) else optional).append(t)
    if not required:
        return ""
    # 필수는 AND로 묶고, 선택 낱말은 "(필수) OR ((필수) AND 선택)" 꼴로 덧붙인다. 결과는 모두 필수를 품고, 선택을 더 맞춘 조각은 점수가 높다.
    # 왜 Lucene의 +/빈 낱말(SHOULD) 문법을 안 쓰나: 빈 낱말의 뜻이 파서의 기본 연산자(OR/AND)에 달려 있고, 노트북(Neo4j 2026.06)에서
    # '+("연혁") "주요" …'가 모든 낱말을 요구하는 것처럼 동작했다(2026-11-26 d033: 연혁 절 조각이 하나도 안 나옴). 명시적 OR/AND는 기본값과 무관하다
    req = required[0] if len(required) == 1 else "(" + " AND ".join(required) + ")"
    if not optional:
        return req
    return " OR ".join([req] + [f"({req} AND {t})" for t in optional])


_PATH_SPLIT_RE = re.compile(r"[^가-힣]+")
# 섹션 경로에 흔한데 어느 섹션인지 가리키지 못하는 낱말. 필수로 삼으면 소음만 늘린다
# "주요"는 목차의 "주요 제품 및 서비스"·"주요 계약"에서 들어오는데, 질문의 "주요 연혁"·"주요 사업"에서 수식어일 뿐이다. 이것이 필수가 되자
# "(연혁) AND (주요)"가 되어 연혁 절 조각(본문에 "주요"가 없음)이 모두 빠졋다 (2026-11-26 d033 진단, notebooks/fulltext_diag.py). 수식어·형식 낱말은 전부 뺀다
_PATH_STOP = {"관한", "사항", "등", "등의", "기타", "밖에", "및", "그", "위하여", "필요한", "상세", "상세표", "개요",
              "주요", "현황", "내용", "기준", "참고", "참고사항", "요약", "전체", "일반", "관련", "구분", "변동", "현재", "최근", "기재", "작성", "이후", "발생"}


def section_vocabulary(paths) -> set[str]:
    """섹션 경로들("I. 회사의 개요 > 2. 회사의 연혁") → 낱말 사전 {"회사의", "회사", "연혁", …}. 조사 뗀 꼴도 넣는다.

    길이 2 이상의 한글 낱말만. 질문의 낱말이 이 사전에 있으면 그 낱말은 섹션을 가리키는 것으로 보고 전문검색에서 필수로 삼는다.
    """
    vocab: set[str] = set()
    for path in paths:
        for tok in _PATH_SPLIT_RE.split(path or ""):
            if len(tok) >= 2 and tok not in _PATH_STOP:
                vocab.update(f for f in _word_forms(tok) if len(f) >= 2 and f not in _PATH_STOP)
    return vocab
