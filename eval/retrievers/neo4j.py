"""Neo4j 위의 검색 둘 (8번 PR). 조각은 load_chunks.py로 들어가 있어야 한다.

- neo4j:vector:<모델>   벡터 인덱스(근사 검색). DuckDB 전수 비교(dense:)와 같은 결과가 나오는지가 8번 PR의 확인 사항
- neo4j:fulltext        cjk 전문검색 (0004). 검색어는 text_normalize.fulltext_query로 만든다 — 색인과 같은 전처리
회사 필터: Neo4j 커뮤니티 벡터 인덱스는 조건을 먼저 걸고 찾는(pre-filter) 기능이 없다 → 후보를 k의 몇 배(candidate_factor) 뽑은 뒤 corp_code로 걸러 k개.
"""

from __future__ import annotations

import duckdb

from eval.metrics import Hit
from eval.retrievers.filters import guess_corp_code
from src.graph.load_chunks import FULLTEXT_INDEX, VECTOR_INDEX
from src.graph.text_normalize import fulltext_query
from src.rag.embedding import Embedder

VECTOR_QUERY = f"""
CALL db.index.vector.queryNodes('{VECTOR_INDEX}', $n, $vec) YIELD node AS c, score
WHERE $corp IS NULL OR c.corp_code = $corp
RETURN c.rcept_no AS rcept_no, c.file_name AS file_name, c.section_id AS section_id, c.path AS path, score, c.text AS text
ORDER BY score DESC LIMIT $k
"""

FULLTEXT_QUERY = f"""
CALL db.index.fulltext.queryNodes('{FULLTEXT_INDEX}', $q, {{limit: $n}}) YIELD node AS c, score
WHERE $corp IS NULL OR c.corp_code = $corp
RETURN c.rcept_no AS rcept_no, c.file_name AS file_name, c.section_id AS section_id, c.path AS path, score, c.text AS text
ORDER BY score DESC LIMIT $k
"""


def _hits(records) -> list[Hit]:
    return [Hit(r["rcept_no"], r["file_name"], r["section_id"], r["path"], float(r["score"]), (r["text"] or "")[:120]) for r in records]


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

    def search(self, question: str, k: int) -> list[Hit]:
        q = fulltext_query(question)
        if not q:
            return []
        corp = guess_corp_code(self.con, question) if (self.corp_filter and self.con is not None) else None
        n = k * self.candidate_factor if corp else k
        return _hits(self.driver.execute_query(FULLTEXT_QUERY, q=q, n=n, corp=corp, k=k, database_=self.database).records)
