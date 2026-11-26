"""Neo4j 위의 검색 셋 (8·9a번 PR). 조각은 load_chunks.py로 들어가 있어야 한다.

- neo4j:vector:<모델>   벡터 인덱스(근사 검색). DuckDB 전수 비교(dense:)와 같은 결과가 나오는지가 8번 PR의 확인 사항
- neo4j:fulltext        cjk 전문검색 (0004). 검색어는 text_normalize.fulltext_query로 만든다 — 색인과 같은 전처리
- neo4j:hybrid:<모델>   벡터 상위 n + 전문검색 상위 n을 RRF로 합친다 (9a, 결정 0010). 선택 단계로 재순위
회사 필터: Neo4j 커뮤니티 벡터 인덱스는 조건을 먼저 걸고 찾는(pre-filter) 기능이 없다 → 후보를 k의 몇 배(candidate_factor) 뽑은 뒤 corp_code로 걸러 k개.

RRF(Reciprocal Rank Fusion): 조각마다 Σ 1/(RRF_K + 그 목록에서의 순위). 두 목록 모두에 있으면 두 항이 더해져 위로 올라간다.
점수 크기가 다른 두 검색(코사인 0~1, Lucene 점수 0~수십)을 **순위만으로** 합치므로 보정이 필요 없다. RRF_K=60은 원 논문의 상수.
"""

from __future__ import annotations

import re

import duckdb

from eval.metrics import Hit
from eval.retrievers.filters import guess_corp_code
from src.graph.load_chunks import FULLTEXT_INDEX, VECTOR_INDEX
from src.graph.text_normalize import fulltext_query, section_terms, section_vocabulary
from src.rag.embedding import Embedder
from src.rag.rerank import Reranker, rerank

RRF_K = 60


def fulltext_with_corp(q: str, corp: str | None) -> str:
    """회사 필터를 Lucene 질의 **안**에 넣는다: corp_code:00382199 AND (…). 인덱스에 corp_code 속성이 있어야 한다(load_chunks.FULLTEXT_PROPERTIES).

    왜(2026-11-26, d033): 필터를 Cypher WHERE로 Lucene 뒤에 걸면, "연혁"처럼 지주 보고서의 계열회사 표에 수천 번 나오는 낱말은
    다른 회사 조각(점수 4 이상)이 상위를 다 채워, 그 회사의 꼬리표 조각(점수 2~3)은 후보를 5,000개로 늘려도 들지 못했다.
    질의 안에서 거르면 회사 조각만 놓고 순위를 매기므로 후보 수도 20으로 충분하다. Cypher WHERE는 안전장치로 남긴다.
    """
    return f"corp_code:{corp} AND ({q})" if corp else q

VECTOR_QUERY = f"""
CALL db.index.vector.queryNodes('{VECTOR_INDEX}', $n, $vec) YIELD node AS c, score
WHERE $corp IS NULL OR c.corp_code = $corp
RETURN c.rcept_no AS rcept_no, c.file_name AS file_name, c.section_id AS section_id, c.path AS path, score, c.text AS text
ORDER BY score DESC LIMIT $k
"""

# $section_words: 질문의 섹션 이름 낱말(text_normalize.section_terms). 섹션 경로에 그 말이 든 조각을 먼저 놓고 그 안에서 점수 순.
# 왜: "연혁"은 연혁 절의 꼬리표에도, 주주 절 본문("최대주주의 주요 연혁")에도 있다. 질문이 섹션 이름을 말했으면 그 섹션의 조각이 답일 가능성이 훨씬 크다
# $prefer_latest: 질문에 연도·날짜가 없으면 최신 보고서 조각을 먼저. 접수번호는 YYYYMMDD로 시작해 문자열 내림차순이 곧 최신순.
# 왜(2026-11-26 d007): "신한라이프의 주요 사업 부문은?"에 5개년 보고서의 같은 "사업의 개요" 조각이 모두 걸려 옛 보고서가 앞자리를 차지했고,
# RRF가 그것을 상위 5개에 섞어 정답(최신 보고서)이 밀렸다. 날짜 없는 질문은 "지금 기준"으로 답하는 것이 이 시스템의 원칙(라우터 기준일도 오늘)
FULLTEXT_QUERY = f"""
CALL db.index.fulltext.queryNodes('{FULLTEXT_INDEX}', $q, {{limit: $n}}) YIELD node AS c, score
WHERE $corp IS NULL OR c.corp_code = $corp
WITH c, score, CASE WHEN $section_words IS NOT NULL AND any(w IN $section_words WHERE c.path CONTAINS w) THEN 1 ELSE 0 END AS in_section,
     CASE WHEN $prefer_latest THEN c.rcept_no ELSE '' END AS recency
RETURN c.rcept_no AS rcept_no, c.file_name AS file_name, c.section_id AS section_id, c.path AS path, score, c.text AS text
ORDER BY in_section DESC, recency DESC, score DESC LIMIT $k
"""

_DATED_RE = re.compile(r"(?:19|20)\d{2}\s*년|\d{4}[.\-/]\d{1,2}|지난|작년|전년|당시|이전|직전|년도|기준")


def keep_latest_copy(records: list[dict]) -> list[dict]:
    """같은 글(text)이 여러 해 보고서에 있으면 접수번호가 가장 큰(=최신) 것만 남긴다. 순서는 남은 것의 원래 순서.

    왜(2026-11-26 d007): "사업의 개요"처럼 해마다 거의 같은 글은 임베딩도 같아 벡터 상위에 5개년 복사본이 나란히 선다. 날짜 없는 질문에는
    최신 보고서가 답이고, 복사본이 상위 5개 자리를 여럿 차지하는 것도 낭비다. 날짜 있는 질문에는 쓰지 않는다(그 해 보고서를 봐야 하므로).
    """
    latest: dict[str, str] = {}
    for r in records:
        key = r["text"] or ""
        if key not in latest or (r["rcept_no"] or "") > latest[key]:
            latest[key] = r["rcept_no"] or ""
    return [r for r in records if (r["rcept_no"] or "") == latest[r["text"] or ""]]


def has_date(question: str) -> bool:
    """질문이 특정 시점을 말하나. 아니면 전문검색은 최신 보고서를 먼저 놓는다 (FULLTEXT_QUERY의 $prefer_latest)."""
    return bool(_DATED_RE.search(question))


def load_section_words(con: duckdb.DuckDBPyConnection | None) -> set[str] | None:
    """조각 섹션 경로의 낱말 사전 (text_normalize.section_vocabulary). con이 없거나 조각이 없으면 None → 전문검색어는 OR로 간다."""
    if con is None:
        return None
    try:
        paths = [r[0] for r in con.execute("SELECT DISTINCT path FROM chunk WHERE path IS NOT NULL").fetchall()]
    except duckdb.Error:
        return None
    return section_vocabulary(paths) or None


def _hits(records) -> list[Hit]:
    return [Hit(r["rcept_no"], r["file_name"], r["section_id"], r["path"], float(r["score"]), (r["text"] or "")[:120], r["text"] or "") for r in records]


class Neo4jVectorRetriever:
    def __init__(self, driver, embedder: Embedder, con: duckdb.DuckDBPyConnection | None = None, corp_filter: bool = False,
                 candidate_factor: int = 20, database: str = "neo4j"):
        self.driver, self.embedder, self.con, self.corp_filter = driver, embedder, con, corp_filter
        self.candidate_factor, self.database = candidate_factor, database
        self.name = f"neo4j-vector-{embedder.name}" + ("-corp" if corp_filter else "")

    def search(self, question: str, k: int) -> list[Hit]:
        corp = guess_corp_code(self.con, question) if (self.corp_filter and self.con is not None) else None
        n = k * self.candidate_factor if corp else k
        vec = self.embedder.encode([question], is_query=True)[0]
        return _hits(self.driver.execute_query(VECTOR_QUERY, n=n, vec=vec, corp=corp, k=k, database_=self.database).records)


class Neo4jFulltextRetriever:
    def __init__(self, driver, con: duckdb.DuckDBPyConnection | None = None, corp_filter: bool = False,
                 candidate_factor: int = 20, database: str = "neo4j"):
        self.driver, self.con, self.corp_filter = driver, con, corp_filter
        self.candidate_factor, self.database = candidate_factor, database
        self.name = "neo4j-fulltext" + ("-corp" if corp_filter else "")
        self.section_words = load_section_words(con)

    def search(self, question: str, k: int) -> list[Hit]:
        q = fulltext_query(question, self.section_words)
        if not q:
            return []
        corp = guess_corp_code(self.con, question) if (self.corp_filter and self.con is not None) else None
        words = section_terms(question, self.section_words) if self.section_words else None
        return _hits(self.driver.execute_query(FULLTEXT_QUERY, q=fulltext_with_corp(q, corp), n=k, corp=corp, k=k, section_words=words or None,
                                               prefer_latest=not has_date(question), database_=self.database).records)


def rrf_fuse(ranked_lists: list[list[dict]], key=lambda r: (r["rcept_no"], r["file_name"], r["section_id"], r["path"], r["text"])) -> list[tuple[dict, float]]:
    """여러 순위 목록을 RRF 점수로 합쳐 (레코드, 점수) 내림차순. 같은 조각은 chunk_id가 없어도 (접수번호·파일·섹션·경로·글)로 같다고 본다."""
    scores: dict = {}
    first: dict = {}
    for ranked in ranked_lists:
        for rank, rec in enumerate(ranked, 1):
            k = key(rec)
            scores[k] = scores.get(k, 0.0) + 1.0 / (RRF_K + rank)
            first.setdefault(k, rec)
    return sorted(((first[k], sc) for k, sc in scores.items()), key=lambda x: -x[1])


class Neo4jHybridRetriever:
    """벡터 n개 + 전문검색 n개 → RRF → (재순위) → k개. n = fetch(기본 20). 전문검색어가 비면(섹션 낱말 없는 질문) 벡터만."""

    def __init__(self, driver, embedder: Embedder, con: duckdb.DuckDBPyConnection | None = None, corp_filter: bool = False,
                 fetch: int = 20, candidate_factor: int = 20, reranker: Reranker | None = None, rerank_candidates: int = 30, database: str = "neo4j"):
        self.driver, self.embedder, self.con, self.corp_filter = driver, embedder, con, corp_filter
        self.fetch, self.candidate_factor, self.reranker, self.rerank_candidates, self.database = fetch, candidate_factor, reranker, rerank_candidates, database
        self.name = f"neo4j-hybrid-{embedder.name}" + ("-corp" if corp_filter else "") + (f"-rerank_{reranker.name}" if reranker else "")
        self.section_words = load_section_words(con)   # 섹션 낱말이 없는 질문은 전문검색을 건너뛰고 벡터만 (0004 결정 2, 2026-11-26)

    def search(self, question: str, k: int) -> list[Hit]:
        corp = guess_corp_code(self.con, question) if (self.corp_filter and self.con is not None) else None
        n = self.fetch * self.candidate_factor if corp else self.fetch
        vec = self.embedder.encode([question], is_query=True)[0]
        lists = [[dict(r) for r in self.driver.execute_query(VECTOR_QUERY, n=n, vec=vec, corp=corp, k=self.fetch, database_=self.database).records]]
        q = fulltext_query(question, self.section_words)
        if q:
            words = section_terms(question, self.section_words) if self.section_words else None
            lists.append([dict(r) for r in self.driver.execute_query(FULLTEXT_QUERY, q=fulltext_with_corp(q, corp), n=self.fetch, corp=corp, k=self.fetch,
                                                                    section_words=words or None, prefer_latest=not has_date(question),
                                                                    database_=self.database).records])
        if not has_date(question):
            lists = [keep_latest_copy(lst) for lst in lists]   # 날짜 없는 질문: 같은 글의 여러 해 복사본 중 최신만 (d007)
        fused = rrf_fuse(lists)
        if self.reranker:
            top = [rec for rec, _ in fused[: self.rerank_candidates]]
            ranked = rerank(self.reranker, question, top, lambda r: r["text"] or "", k)
            return [Hit(r["rcept_no"], r["file_name"], r["section_id"], r["path"], float(s), (r["text"] or "")[:120], r["text"] or "") for r, s in ranked]
        return [Hit(r["rcept_no"], r["file_name"], r["section_id"], r["path"], float(s), (r["text"] or "")[:120], r["text"] or "") for r, s in fused[:k]]
