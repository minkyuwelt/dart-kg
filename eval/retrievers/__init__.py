"""검색 함수. 모두 같은 모양이다: search(question, k) -> list[Hit] (점수 높은 순).

- keyword:                DuckDB document_section 키워드 일치 (기본선, 4번 PR)
- chunk:<전략>:           chunk 테이블 위의 같은 키워드 검색 (5번 PR). 예: chunk:section_path
- dense:<모델>:<전략>:    chunk_embedding 벡터 검색 (6번 PR). 예: dense:kure-v3:section_path. 모델은 노트북에서만 돈다
- (8번 PR) Neo4j 벡터 / 전문검색 / 하이브리드
선택 단계(모든 조각 검색기에 공통): corp_filter(질문의 회사명으로 좁힘), reranker(상위 후보를 교차 인코더로 다시 매김).
설정 이름(--retriever)으로 고르므로 run.py는 검색 방식을 모른다.
"""

from typing import Protocol

from eval.metrics import Hit


class Retriever(Protocol):
    name: str

    def search(self, question: str, k: int) -> list[Hit]: ...


def make_retriever(name: str, con, corp_filter: bool = False, rerank: str | None = None, embedder=None, reranker=None):
    """embedder·reranker를 직접 주면 그것을 쓴다 (테스트의 가짜 모델). 없으면 설정 이름으로 로컬 모델을 연다."""
    from src.loaders.chunking import STRATEGIES

    if rerank and reranker is None:
        from src.rag.rerank import LocalReranker
        reranker = LocalReranker(rerank)

    if name == "keyword":
        from eval.retrievers.keyword import KeywordRetriever
        if corp_filter or reranker:
            raise SystemExit("keyword(섹션 기본선)에는 회사 필터·재순위를 붙이지 않습니다. chunk:<전략>을 쓰세요")
        return KeywordRetriever(con)

    if name.startswith("chunk:"):
        from eval.retrievers.chunk import ChunkKeywordRetriever
        strategy = name.split(":", 1)[1]
        if strategy not in STRATEGIES:
            raise SystemExit(f"모르는 청킹 전략: {strategy} (가능: {', '.join(STRATEGIES)})")
        return ChunkKeywordRetriever(con, strategy, corp_filter=corp_filter, reranker=reranker)

    if name.startswith("dense:"):
        from eval.retrievers.dense import DenseRetriever
        from src.rag.embedding import MODELS
        parts = name.split(":")
        if len(parts) != 3:
            raise SystemExit("dense 검색기는 dense:<모델>:<전략> 꼴입니다. 예: dense:kure-v3:section_path")
        _, model, strategy = parts
        if strategy not in STRATEGIES:
            raise SystemExit(f"모르는 청킹 전략: {strategy} (가능: {', '.join(STRATEGIES)})")
        if embedder is None:
            if model not in MODELS:
                raise SystemExit(f"모르는 임베딩 모델: {model} (가능: {', '.join(MODELS)})")
            from src.rag.embedding import LocalEmbedder
            embedder = LocalEmbedder(model)
        return DenseRetriever(con, embedder, strategy, corp_filter=corp_filter, reranker=reranker)

    raise SystemExit(f"모르는 검색 방식: {name} (가능: keyword, chunk:<전략>, dense:<모델>:<전략>)")
