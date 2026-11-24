"""조각(chunk 테이블) 위의 키워드 검색. keyword.py와 점수 규칙이 같다 — 다른 것은 "섹션 하나 = 조각 하나"가 아니라 0008의 조각이라는 것.

용도: 임베딩 전에 청킹 전략(fixed / section / section_path)을 같은 평가 세트·같은 검색 규칙으로 비교한다.
--retriever chunk:section 처럼 전략 이름을 콜론 뒤에 쓴다. 섹션 경로 점수는 path, 본문 점수는 embed_text(꼬리표 포함)에서 본다.
"""

import duckdb

from eval.metrics import Hit
from eval.retrievers.keyword import tokenize


class ChunkKeywordRetriever:
    def __init__(self, con: duckdb.DuckDBPyConnection, strategy: str):
        self.con = con
        self.strategy = strategy
        self.name = f"chunk-{strategy}"

    def search(self, question: str, k: int) -> list[Hit]:
        terms = tokenize(question)
        if not terms:
            return []
        path_score = " + ".join("CASE WHEN contains(path, ?) THEN 2 ELSE 0 END" for _ in terms)
        body_score = " + ".join("CASE WHEN contains(embed_text, ?) THEN 1 ELSE 0 END" for _ in terms)
        rows = self.con.execute(
            f"""
            SELECT rcept_no, file_name, section_id, path, score, substr(text, 1, 120)
            FROM (
                SELECT rcept_no, file_name, section_id, path, char_count, text, seq, ({path_score}) + ({body_score}) AS score
                FROM chunk WHERE strategy = ?
            )
            WHERE score > 0
            ORDER BY score DESC, char_count ASC, rcept_no DESC, seq
            LIMIT ?
            """,
            list(terms) + list(terms) + [self.strategy, k],
        ).fetchall()
        return [Hit(r[0], r[1], r[2], r[3], float(r[4]), r[5]) for r in rows]
