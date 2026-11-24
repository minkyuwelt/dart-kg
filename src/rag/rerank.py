"""재순위 (0009 실험의 선택 단계). 검색 상위 n개를 교차 인코더로 다시 매긴다.

교차 인코더는 질문과 조각을 **함께** 읽어 점수를 내므로 임베딩(따로 읽고 거리 비교)보다 정확하지만 느리다.
그래서 전체 조각이 아니라 상위 20~50개에만 쓴다. 후보: KURE-Reranker-nano (149M). 패키지는 노트북 전용(requirements-ml.txt), 함수 안 import.
"""

from __future__ import annotations

from typing import Protocol

RERANKERS = {
    "nano": "nlpai-lab/KURE-Reranker-nano",
}


class Reranker(Protocol):
    name: str

    def score(self, query: str, texts: list[str]) -> list[float]: ...


class LocalReranker:
    def __init__(self, name: str, device: str | None = None):
        if name not in RERANKERS:
            raise ValueError(f"모르는 재순위 모델: {name} (가능: {', '.join(RERANKERS)})")
        self.name = name
        from sentence_transformers import CrossEncoder  # 노트북 전용 패키지

        self.model = CrossEncoder(RERANKERS[name], device=device)

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        return [float(s) for s in self.model.predict([(query, t) for t in texts], show_progress_bar=False)]


def rerank(reranker: Reranker, query: str, items: list, text_of, k: int) -> list:
    """items를 reranker 점수로 다시 정렬해 상위 k개. text_of(item)이 조각 글."""
    scores = reranker.score(query, [text_of(it) for it in items])
    order = sorted(range(len(items)), key=lambda i: scores[i], reverse=True)
    return [(items[i], scores[i]) for i in order[:k]]
