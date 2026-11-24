"""검색 함수. 모두 같은 모양이다: search(question, k) -> list[Hit] (점수 높은 순).

- keyword: DuckDB document_section 키워드 일치 (기본선, 4번 PR)
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
    raise SystemExit(f"모르는 검색 방식: {name} (가능: keyword)")
