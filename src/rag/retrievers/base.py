"""검색기의 공통 모양 (코드 리뷰 A4, 2026-11-27).

전에는 ask()가 getattr(retriever, "last")·"error"·"base"로 그래프 검색기의 속을 추측했다. 이제 모든 검색기가 retrieve()로
Retrieval(조각 + 그래프 관계표 + 실패 사유 + 섹션 사전)을 돌려주고, ask()는 그 안만 본다. search()는 평가(hit@5)가 쓰는 조각 목록이고 그대로다.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from src.rag.retrievers.hit import Hit

if TYPE_CHECKING:
    from src.graph.query import GraphContext


@dataclass
class Retrieval:
    hits: list[Hit]
    graph: GraphContext | None = None          # 그래프 검색기가 템플릿을 썼을 때의 관계표
    graph_error: str | None = None             # 템플릿 질의가 실패해 RAG만 쓴 경우 그 사유
    section_words: set[str] | None = None      # 전문검색의 섹션 사전 (이웃 조각 찾기가 같은 사전을 쓴다)


class BaseRetriever:
    """search(question, k) -> list[Hit] 를 구현하면 retrieve()는 공짜다. name은 평가 결과 파일·API 응답에 적힌다."""

    name: str = ""
    section_words: set[str] | None = None

    def search(self, question: str, k: int) -> list[Hit]:
        raise NotImplementedError

    def retrieve(self, question: str, k: int) -> Retrieval:
        return Retrieval(self.search(question, k), section_words=self.section_words)
