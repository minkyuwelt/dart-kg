"""검색 함수. 모두 같은 모양이다: search(question, k) -> list[Hit] (점수 높은 순).

- keyword:        DuckDB document_section 키워드 일치 (기본선, 4번 PR)
- chunk:<전략>:   chunk 테이블 위의 같은 키워드 검색 (5번 PR). 예: chunk:section — 청킹 전략을 임베딩 전에 비교하는 용도
- (8번 PR) Neo4j 벡터 / 전문검색 / 하이브리드
설정 이름(--retriever)으로 고르므로 run.py는 검색 방식을 모른다.
"""

from typing import Protocol

from eval.metrics import Hit


class Retriever(Protocol):
    name: str

    def search(self, question: str, k: int) -> list[Hit]: ...


def make_retriever(name: str, con):
    if name == "keyword":
        from eval.retrievers.keyword import KeywordRetriever
        return KeywordRetriever(con)
    if name.startswith("chunk:"):
        from src.loaders.chunking import STRATEGIES
        from eval.retrievers.chunk import ChunkKeywordRetriever
        strategy = name.split(":", 1)[1]
        if strategy not in STRATEGIES:
            raise SystemExit(f"모르는 청킹 전략: {strategy} (가능: {', '.join(STRATEGIES)})")
        return ChunkKeywordRetriever(con, strategy)
    raise SystemExit(f"모르는 검색 방식: {name} (가능: keyword, chunk:<전략>)")
