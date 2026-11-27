"""검색 함수. 모두 같은 모양이다: search(question, k) -> list[Hit] (점수 높은 순).

- keyword:                DuckDB document_section 키워드 일치 (기본선, 4번 PR)
- chunk:<전략>:           chunk 테이블 위의 같은 키워드 검색 (5번 PR). 예: chunk:section_path
- dense:<모델>:<전략>:    chunk_embedding 벡터 검색 (6번 PR). 예: dense:kure-v3:section_path. 모델은 노트북에서만 돈다
- neo4j:vector:<모델>:    Neo4j 벡터 인덱스 (8번 PR). 예: neo4j:vector:kure-v3. 조각은 src/graph/load_chunks.py로 들어가 있어야 한다
- neo4j:fulltext:         Neo4j cjk 전문검색 (0004). 검색어는 text_normalize.fulltext_query
- neo4j:hybrid:<모델>:    벡터 20 + 전문검색 20 → RRF (9a번 PR, 0010). 재순위는 여기에만 붙는다
- graph:only:             (Phase 4, 0011) 라우터 → 그래프 템플릿 → 근거 조각만. 템플릿에 안 맞는 질문은 빈 결과
- graph:hybrid:<모델>:    그래프 근거 조각 + neo4j:hybrid:<모델> → RRF. 템플릿에 안 맞으면 neo4j:hybrid 그대로
선택 단계(모든 조각 검색기에 공통): corp_filter(질문의 회사명으로 좁힘), reranker(상위 후보를 교차 인코더로 다시 매김).
설정 이름(--retriever)으로 고르므로 eval/run.py는 검색 방식을 모른다.
2026-11-27 `eval/retrievers/`에서 여기로 옮김: API·답변(src)이 실제로 쓰는 검색이 평가 폴더에 있어 의존 방향이 거꾸로였다. 평가는 이것을 가져다 쓰기만 한다.
"""

from typing import Protocol

from src.rag.retrievers.hit import Hit


class Retriever(Protocol):
    name: str

    def search(self, question: str, k: int) -> list[Hit]: ...


def make_retriever(name: str, con, corp_filter: bool = False, rerank: str | None = None, embedder=None, reranker=None, driver=None):
    """embedder·reranker·driver를 직접 주면 그것을 쓴다 (테스트의 가짜 모델·가짜 Neo4j). 없으면 설정 이름으로 로컬 모델·.env의 Neo4j를 연다."""
    from src.loaders.chunking import STRATEGIES

    if rerank and reranker is None:
        from src.rag.rerank import LocalReranker
        reranker = LocalReranker(rerank)

    if name == "keyword":
        from src.rag.retrievers.keyword import KeywordRetriever
        if corp_filter or reranker:
            raise SystemExit("keyword(섹션 기본선)에는 회사 필터·재순위를 붙이지 않습니다. chunk:<전략>을 쓰세요")
        return KeywordRetriever(con)

    if name.startswith("chunk:"):
        from src.rag.retrievers.chunk import ChunkKeywordRetriever
        strategy = name.split(":", 1)[1]
        if strategy not in STRATEGIES:
            raise SystemExit(f"모르는 청킹 전략: {strategy} (가능: {', '.join(STRATEGIES)})")
        return ChunkKeywordRetriever(con, strategy, corp_filter=corp_filter, reranker=reranker)

    if name.startswith("dense:"):
        from src.rag.retrievers.dense import DenseRetriever
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

    if name == "graph:only" or name.startswith("graph:hybrid:"):
        from src.graph.load_relations import load_lineage
        from src.graph.query import GraphRetriever
        if reranker:
            raise SystemExit("graph 검색기에는 재순위를 붙이지 않습니다 (0011: 크로스인코더 제외)")
        if driver is None:
            from src.graph.client import connect
            driver = connect()
        base = None
        if name.startswith("graph:hybrid:"):
            base = make_retriever("neo4j:hybrid:" + name.split(":", 2)[2], con, corp_filter=corp_filter, embedder=embedder, driver=driver)
        return GraphRetriever(driver, con, base=base, lineage=load_lineage())

    if name.startswith("neo4j:"):
        from src.rag.retrievers.neo4j import Neo4jFulltextRetriever, Neo4jHybridRetriever, Neo4jVectorRetriever
        from src.rag.embedding import MODELS
        parts = name.split(":")
        if reranker and parts[1:2] != ["hybrid"]:
            raise SystemExit("neo4j 검색기의 재순위는 neo4j:hybrid:<모델>에만 붙입니다")
        if driver is None:
            from src.graph.client import connect
            driver = connect()
        if parts[1:] == ["fulltext"]:
            return Neo4jFulltextRetriever(driver, con, corp_filter=corp_filter)
        if len(parts) == 3 and parts[1] in ("vector", "hybrid"):
            model = parts[2]
            if embedder is None:
                if model not in MODELS:
                    raise SystemExit(f"모르는 임베딩 모델: {model} (가능: {', '.join(MODELS)})")
                from src.rag.embedding import LocalEmbedder
                embedder = LocalEmbedder(model, fp16=True)
            if parts[1] == "vector":
                return Neo4jVectorRetriever(driver, embedder, con, corp_filter=corp_filter)
            return Neo4jHybridRetriever(driver, embedder, con, corp_filter=corp_filter, reranker=reranker)
        raise SystemExit("neo4j 검색기는 neo4j:vector:<모델>, neo4j:hybrid:<모델> 또는 neo4j:fulltext 꼴입니다")

    raise SystemExit(f"모르는 검색 방식: {name} (가능: keyword, chunk:<전략>, dense:<모델>:<전략>, neo4j:vector:<모델>, neo4j:hybrid:<모델>, neo4j:fulltext, graph:only, graph:hybrid:<모델>)")
