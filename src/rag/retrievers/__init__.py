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

from dataclasses import dataclass
from typing import Protocol

from src.rag.retrievers.base import BaseRetriever, Retrieval  # noqa: F401 — 패키지 밖은 여기서 가져간다
from src.rag.retrievers.hit import Hit


class Retriever(Protocol):
    name: str

    def search(self, question: str, k: int) -> list[Hit]: ...

    def retrieve(self, question: str, k: int) -> Retrieval: ...


@dataclass(frozen=True)
class RetrieverSpec:
    """검색기 이름 문법을 한 번만 해석한다 (코드 리뷰 A3). API는 이걸 보고 임베딩 모델·Neo4j를 미리 열지 정한다."""
    name: str
    kind: str                  # keyword | chunk | dense | neo4j | graph
    model: str | None = None   # 임베딩 모델 설정 이름 (dense·neo4j:vector·neo4j:hybrid·graph:hybrid)
    strategy: str | None = None
    sub: str | None = None     # neo4j: vector | fulltext | hybrid. graph: only | hybrid

    @property
    def needs_neo4j(self) -> bool:
        return self.kind in ("neo4j", "graph")

    @property
    def needs_embedder(self) -> bool:
        return self.model is not None


def parse_retriever_name(name: str) -> RetrieverSpec:
    parts = name.split(":")
    if name == "keyword":
        return RetrieverSpec(name, "keyword")
    if parts[0] == "chunk" and len(parts) == 2:
        return RetrieverSpec(name, "chunk", strategy=parts[1])
    if parts[0] == "dense":
        if len(parts) != 3:
            raise SystemExit("dense 검색기는 dense:<모델>:<전략> 꼴입니다. 예: dense:kure-v3:section_path")
        return RetrieverSpec(name, "dense", model=parts[1], strategy=parts[2])
    if parts[0] == "graph":
        if parts[1:] == ["only"]:
            return RetrieverSpec(name, "graph", sub="only")
        if len(parts) == 3 and parts[1] == "hybrid":
            return RetrieverSpec(name, "graph", model=parts[2], sub="hybrid")
        raise SystemExit("graph 검색기는 graph:only 또는 graph:hybrid:<모델> 꼴입니다")
    if parts[0] == "neo4j":
        if parts[1:] == ["fulltext"]:
            return RetrieverSpec(name, "neo4j", sub="fulltext")
        if len(parts) == 3 and parts[1] in ("vector", "hybrid"):
            return RetrieverSpec(name, "neo4j", model=parts[2], sub=parts[1])
    raise SystemExit(f"모르는 검색기: {name} (keyword / chunk:<전략> / dense:<모델>:<전략> / neo4j:vector:<모델> / neo4j:fulltext / neo4j:hybrid:<모델> / graph:only / graph:hybrid:<모델>)")


def make_retriever(name: str, con, corp_filter: bool = False, rerank: str | None = None, embedder=None, reranker=None, driver=None, picker=None):
    """embedder·reranker·driver를 직접 주면 그것을 쓴다 (테스트의 가짜 모델·가짜 Neo4j). 없으면 설정 이름으로 로컬 모델·.env의 Neo4j를 연다.
    picker는 그래프 검색기에만 뜻이 있다 (규칙이 못 고른 질문에서 LLM이 템플릿 번호를 고름). 다른 검색기에 주면 무시."""
    from src.loaders.chunking import STRATEGIES

    spec = parse_retriever_name(name)
    if rerank and reranker is None:
        from src.rag.rerank import LocalReranker
        reranker = LocalReranker(rerank)
    if spec.strategy is not None and spec.strategy not in STRATEGIES:
        raise SystemExit(f"모르는 청킹 전략: {spec.strategy} (가능: {', '.join(STRATEGIES)})")

    def local_embedder(fp16: bool = False):
        if embedder is not None:
            return embedder
        from src.rag.embedding import MODELS, LocalEmbedder
        if spec.model not in MODELS:
            raise SystemExit(f"모르는 임베딩 모델: {spec.model} (가능: {', '.join(MODELS)})")
        return LocalEmbedder(spec.model, fp16=fp16)

    def neo4j_driver():
        if driver is not None:
            return driver
        from src.graph.client import connect
        return connect()

    if spec.kind == "keyword":
        from src.rag.retrievers.keyword import KeywordRetriever
        if corp_filter or reranker:
            raise SystemExit("keyword(섹션 기본선)에는 회사 필터·재순위를 붙이지 않습니다. chunk:<전략>을 쓰세요")
        return KeywordRetriever(con)

    if spec.kind == "chunk":
        from src.rag.retrievers.chunk import ChunkKeywordRetriever
        return ChunkKeywordRetriever(con, spec.strategy, corp_filter=corp_filter, reranker=reranker)

    if spec.kind == "dense":
        from src.rag.retrievers.dense import DenseRetriever
        return DenseRetriever(con, local_embedder(), spec.strategy, corp_filter=corp_filter, reranker=reranker)

    if spec.kind == "graph":
        from src.graph.load_relations import load_lineage
        from src.graph.query import GraphRetriever
        if reranker:
            raise SystemExit("graph 검색기에는 재순위를 붙이지 않습니다 (0011: 크로스인코더 제외)")
        d = neo4j_driver()
        base = make_retriever(f"neo4j:hybrid:{spec.model}", con, corp_filter=corp_filter, embedder=embedder, driver=d) if spec.sub == "hybrid" else None
        return GraphRetriever(d, con, base=base, lineage=load_lineage(), picker=picker)

    from src.rag.retrievers.neo4j import Neo4jFulltextRetriever, Neo4jHybridRetriever, Neo4jVectorRetriever
    if reranker and spec.sub != "hybrid":
        raise SystemExit("neo4j 검색기의 재순위는 neo4j:hybrid:<모델>에만 붙입니다")
    d = neo4j_driver()
    if spec.sub == "fulltext":
        return Neo4jFulltextRetriever(d, con, corp_filter=corp_filter)
    if spec.sub == "vector":
        return Neo4jVectorRetriever(d, local_embedder(fp16=True), con, corp_filter=corp_filter)
    return Neo4jHybridRetriever(d, local_embedder(fp16=True), con, corp_filter=corp_filter, reranker=reranker)
