"""키워드 기본선. DuckDB document_section에서 질문의 낱말이 들어 있는 섹션을 찾는다.

임베딩·Neo4j가 없어도 평가 세트를 만든 즉시 숫자가 나오고, 나중에 "임베딩이 키워드보다 얼마나 나은가"를 말할 수 있다.
한국어라 띄어쓰기 단위 일치는 약하다 (조사·활용). 간단한 조사 떼기만 하고, 그 이상의 형태소 분석은 하지 않는다 — 기본선이다.

점수 = Σ(낱말이 제목에 있으면 2) + Σ(낱말이 본문 또는 표에 있으면 1). 같은 점수면 짧은 섹션 먼저 (답이 바로 보이는 쪽).
조각 단위는 아직 없으므로(0008 미정) 섹션 하나가 조각 하나다.
"""

import re

import duckdb

from eval.metrics import Hit

TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
# 낱말 끝의 조사. 긴 것부터 떼야 "에서"가 "에"+"서"로 남지 않는다. 두 글자 이하 낱말은 떼지 않는다 (예: "은행"의 "은")
PARTICLES = ("에서는", "으로는", "에게", "에서", "으로", "부터", "까지", "처럼", "보다", "이란", "이라", "의", "은", "는", "이", "가", "을", "를", "에", "와", "과", "로", "도")
STOPWORDS = {"기준", "무엇", "누구", "어디", "얼마", "언제", "어떻게", "대한", "관한", "있는", "있나", "경우", "현재", "말", "년", "월", "일"}
QUESTION_ENDING_RE = re.compile(r"(인가요|입니까|인가|인지|일까|였나|이었나|인데)$")  # "누구인가" → "누구"
MAX_TERMS = 12


def strip_particle(token: str) -> str:
    if len(token) <= 2:
        return token
    for p in PARTICLES:
        if token.endswith(p) and len(token) - len(p) >= 2:
            return token[: -len(p)]
    return token


def tokenize(question: str) -> list[str]:
    """질문 → 검색 낱말. 숫자 뒤 단위(2023년 → 2023)를 떼고, 조사를 떼고, 물음말·중복을 뺀다."""
    terms = []
    for tok in TOKEN_RE.findall(question):
        tok = QUESTION_ENDING_RE.sub("", tok) or tok   # 누구인가 → 누구
        if tok in STOPWORDS:
            continue
        tok = strip_particle(tok)                        # 신한은행의 → 신한은행, 2025년에 → 2025년
        tok = re.sub(r"^(\d+)(년|월|일|분기|기)$", r"\1", tok)  # 2025년 → 2025
        if len(tok) < 2 or tok in STOPWORDS or tok in terms:
            continue
        terms.append(tok)
    return terms[:MAX_TERMS]


class KeywordRetriever:
    name = "keyword"

    def __init__(self, con: duckdb.DuckDBPyConnection):
        self.con = con

    def search(self, question: str, k: int) -> list[Hit]:
        terms = tokenize(question)
        if not terms:
            return []
        title_score = " + ".join("CASE WHEN contains(title, ?) THEN 2 ELSE 0 END" for _ in terms)
        body_score = " + ".join("CASE WHEN contains(text, ?) OR contains(CAST(tables AS VARCHAR), ?) THEN 1 ELSE 0 END" for _ in terms)
        params = list(terms) + [t for t in terms for _ in (0, 1)]
        rows = self.con.execute(
            f"""
            SELECT rcept_no, file_name, section_id, path, score, substr(text, 1, 120)
            FROM (
                SELECT rcept_no, file_name, section_id, path, char_count, text, ({title_score}) + ({body_score}) AS score
                FROM document_section
                WHERE char_count > 0 OR table_count > 0
            )
            WHERE score > 0
            ORDER BY score DESC, char_count ASC, rcept_no DESC, section_id
            LIMIT ?
            """,
            params + [k],
        ).fetchall()
        return [Hit(r[0], r[1], r[2], r[3], float(r[4]), r[5]) for r in rows]
