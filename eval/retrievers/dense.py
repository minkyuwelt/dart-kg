"""벡터(임베딩) 검색. chunk_embedding에서 질문 벡터와 코사인 유사도가 높은 조각을 찾는다 (DuckDB 안 전수 비교, Neo4j 전).

--retriever dense:<모델>:<전략>  예: dense:kure-v3:section_path
선택 단계: 회사 필터(--filter corp, 질문의 회사명으로 corp_code를 좁힘), 재순위(--rerank nano, 상위 candidates개를 교차 인코더로 다시 매김).
"""

import duckdb

from eval.metrics import Hit
from eval.retrievers.filters import guess_corp_code
from src.rag.embedding import Embedder
from src.rag.rerank import Reranker, rerank


class DenseRetriever:
    def __init__(self, con: duckdb.DuckDBPyConnection, embedder: Embedder, strategy: str, corp_filter: bool = False,
                 reranker: Reranker | None = None, candidates: int = 30):
        self.con = con
        self.embedder = embedder
        self.strategy = strategy
        self.corp_filter = corp_filter
        self.reranker = reranker
        self.candidates = candidates
        self.name = f"dense-{embedder.name}-{strategy}" + ("-corp" if corp_filter else "") + (f"-rerank_{reranker.name}" if reranker else "")

    def search(self, question: str, k: int) -> list[Hit]:
        qv = self.embedder.encode([question], is_query=True)[0]
        where = "e.model_name = ? AND c.strategy = ?"
        params: list = [qv, self.embedder.name, self.strategy]
        if self.corp_filter:
            corp_code = guess_corp_code(self.con, question)
            if corp_code:
                where += " AND c.corp_code = ?"
                params.append(corp_code)
        n = self.candidates if self.reranker else k
        rows = self.con.execute(
            f"""
            SELECT c.rcept_no, c.file_name, c.section_id, c.path, list_cosine_similarity(e.vector, ?::FLOAT[]) AS score, c.text
            FROM chunk_embedding e JOIN chunk c USING (chunk_id)
            WHERE {where}
            ORDER BY score DESC, c.rcept_no DESC, c.seq
            LIMIT ?
            """,
            params + [n],
        ).fetchall()
        if self.reranker:
            ranked = rerank(self.reranker, question, rows, lambda r: r[5], k)
            return [Hit(r[0], r[1], r[2], r[3], float(s), r[5][:120]) for r, s in ranked]
        return [Hit(r[0], r[1], r[2], r[3], float(r[4]), r[5][:120]) for r in rows]
