"""조각(chunk 테이블) 위의 키워드 검색. keyword.py와 점수 규칙이 같다 — 다른 것은 "섹션 하나 = 조각 하나"가 아니라 0008의 조각이라는 것.

용도: 임베딩 전에 청킹 전략(fixed / section / section_path)을 같은 평가 세트·같은 검색 규칙으로 비교한다.
--retriever chunk:section 처럼 전략 이름을 콜론 뒤에 쓴다. 섹션 경로 점수는 path, 본문 점수는 embed_text(꼬리표 포함)에서 본다.
선택 단계: 회사 필터(corp_filter), 재순위(reranker) — dense.py와 같은 규칙이라 "임베딩 vs 키워드"를 같은 조건에서 비교할 수 있다.
"""

import duckdb

from src.rag.retrievers.base import BaseRetriever
from src.rag.retrievers.hit import Hit
from src.rag.retrievers.filters import guess_corp_code
from src.rag.retrievers.keyword import tokenize
from src.rag.rerank import Reranker, rerank


class ChunkKeywordRetriever(BaseRetriever):
    def __init__(self, con: duckdb.DuckDBPyConnection, strategy: str, corp_filter: bool = False,
                 reranker: Reranker | None = None, candidates: int = 30):
        self.con = con
        self.strategy = strategy
        self.corp_filter = corp_filter
        self.reranker = reranker
        self.candidates = candidates
        self.name = f"chunk-{strategy}" + ("-corp" if corp_filter else "") + (f"-rerank_{reranker.name}" if reranker else "")

    def search(self, question: str, k: int) -> list[Hit]:
        terms = tokenize(question)
        if not terms:
            return []
        path_score = " + ".join("CASE WHEN contains(path, ?) THEN 2 ELSE 0 END" for _ in terms)
        body_score = " + ".join("CASE WHEN contains(embed_text, ?) THEN 1 ELSE 0 END" for _ in terms)
        where = "strategy = ?"
        params: list = list(terms) + list(terms) + [self.strategy]
        if self.corp_filter:
            corp_code = guess_corp_code(self.con, question)
            if corp_code:
                where += " AND corp_code = ?"
                params.append(corp_code)
        n = self.candidates if self.reranker else k
        rows = self.con.execute(
            f"""
            SELECT rcept_no, file_name, section_id, path, score, text
            FROM (
                SELECT rcept_no, file_name, section_id, path, char_count, text, seq, ({path_score}) + ({body_score}) AS score
                FROM chunk WHERE {where}
            )
            WHERE score > 0
            ORDER BY score DESC, char_count ASC, rcept_no DESC, seq
            LIMIT ?
            """,
            params + [n],
        ).fetchall()
        if self.reranker:
            ranked = rerank(self.reranker, question, rows, lambda r: r[5], k)
            return [Hit(r[0], r[1], r[2], r[3], float(s), r[5][:120], r[5]) for r, s in ranked]
        return [Hit(r[0], r[1], r[2], r[3], float(r[4]), r[5][:120], r[5]) for r in rows]
