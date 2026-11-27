"""질문 → 검색(하이브리드) → LLM 답 + 근거(접수번호·섹션·DART 뷰어 링크). Phase 3의 끝 그림 (계획 1절, usecases D3·D5).

    python -m src.rag.ask "2025년 신한은행의 주당 현금배당금은 얼마인가?"
    python -m src.rag.ask "..." --no-llm                 # 검색 결과만 (API 키 없이)
    python -m src.rag.ask "..." --retriever dense:kure-v3:section_path   # Neo4j 없이 DuckDB 벡터 검색으로
    python -m src.rag.ask "..." --k 8 --no-filter
    python -m src.rag.ask "..." --neighbors 0               # 이웃 조각 끄기 (비교용)

기본 검색기는 graph:hybrid:kure-v3 + 회사 필터: 그래프 질문이면 관계표와 근거 조각을 RRF로 합치고(0011), 아니면 neo4j:hybrid:kure-v3(0009·0010)와 같다. 조각은 Neo4j에 들어가 있어야 한다(load_chunks).
근거 출력: 인용된 조각을 먼저, 나머지 검색 결과는 "참고"로. 모든 근거에 DART 뷰어 링크를 붙인다.
이웃 조각(0011 "같은 절의 앞뒤 조각", 2026-11-27): 질문이 절 이름을 말하면(예: "연혁" — 전문검색과 같은 섹션 사전 text_normalize.section_terms),
그 절에 있는 검색 상위 조각의 바로 앞·뒤 조각(같은 보고서·같은 섹션 경로)을 최대 NEIGHBOR_MAX개 LLM에 함께 넘긴다.
연혁처럼 한 절이 여러 조각으로 잘린 답(d033)은 상위 5개 안에 절의 일부만 들어오는데, k를 늘리면 다른 보고서·다른 절 조각(소음)이 들어오고
이웃 조각은 "질문이 말한 절의 이어지는 글"만 더한다. 처음(0010 8차 1회)에는 순위대로 모든 조각의 이웃을 붙였는데 상한 4개가 1·2위(다른 절)의
이웃에 먼저 쓰여 연혁 절 이웃은 못 들어오고 상품 이야기가 답에 섞였다 → 절 이름이 든 조각만. 질문에 절 이름이 없으면 붙이지 않는다.
검색 평가(hit@k)는 retriever.search 결과만 보므로 바뀌지 않는다.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from eval.metrics import Hit
from src.rag.llm import LLM, NOT_FOUND, Answer, Passage

VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


GRAPH_LABEL = "그래프 관계표 (공시의 지분·임원 표를 그래프로 따라간 결과, 파생 값은 규칙 계산)"
NEIGHBOR_MAX = 4          # 덧붙이는 이웃 조각 상한. 조각 1,000자 상한이 그대로라 최악 +4,000자(≈ 3,000 토큰) — d033 7,531 토큰에 더해도 0011 예산(약 12k) 안
CHUNK_STRATEGY = "section_path"   # Neo4j에 올린 조각 전략 (src/graph/load_chunks.DEFAULT_STRATEGY). 이웃을 DuckDB chunk 표에서 같은 전략으로 찾는다
GRAPH_MAX_CHARS = 9000   # 파생 값 80줄 + 관계 행 50개가 들어가는 길이. 6000에서는 g05의 파생 값이 잘렸다(2026-11-26). 0011 예산(총 약 12k 토큰) 안


@dataclass
class AskResult:
    question: str
    hits: list[Hit]
    answer: Answer | None = None          # --no-llm이면 None
    labels: list[str] = field(default_factory=list)
    graph: object | None = None           # GraphContext (graph: 검색기가 템플릿을 썼을 때). LLM에는 조각 뒤 마지막 번호로 넘긴다
    graph_error: str | None = None

    @property
    def graph_number(self) -> int | None:
        return len(self.hits) + 1 if self.graph is not None else None

    @property
    def graph_cited(self) -> bool:
        return bool(self.answer and self.graph_number and self.graph_number in self.answer.citations)

    @property
    def cited_hits(self) -> list[tuple[int, Hit]]:
        if not self.answer:
            return []
        return [(n, self.hits[n - 1]) for n in self.answer.citations if 1 <= n <= len(self.hits)]


def passage_label(hit: Hit, con=None) -> str:
    """'회사명 | 보고서 | 섹션 경로'. 조각 글(embed_text 꼬리표)에 이미 있지만 LLM에는 번호와 함께 따로 보여 준다. con이 있으면 disclosure에서 회사·보고서 이름."""
    if con is not None:
        row = con.execute("SELECT corp_name, report_nm FROM disclosure WHERE rcept_no = ?", [hit.rcept_no]).fetchone()
        if row:
            return f"{row[0]} | {row[1]} | {hit.path}"
    return f"접수번호 {hit.rcept_no} | {hit.path}"


def expand_neighbors(question: str, hits: list[Hit], con, limit: int = NEIGHBOR_MAX, section_words: set[str] | None = None) -> list[tuple[Hit, int, bool]]:
    """질문이 말한 절(섹션 경로에 질문의 섹션 사전 낱말이 든 조각)의 검색 상위 조각에 대해, 같은 보고서·파일·섹션 경로에서 seq가 바로 앞·뒤인 조각을 DuckDB chunk 표에서 찾는다.

    순위대로 돌며(그 절의 1위의 앞·뒤, 다음 순위의 앞·뒤, …) limit개가 차면 멈춘다. 이미 검색된 조각과 중복은 건너뛴다.
    질문에 섹션 사전 낱말이 없으면 빈 목록(어느 절이 답인지 모르면 이웃을 붙이지 않는다 — 다른 절의 이웃은 소음이었다, 0010 8차).
    section_words를 안 주면 chunk 표의 섹션 경로에서 사전을 만든다(전문검색기와 같은 load_section_words).
    Hit에는 seq가 없어 (접수번호, 파일, 글)로 chunk 행을 찾는다 — Neo4j 노드의 text는 chunk.text 그대로다.
    돌려주는 것: (이웃 Hit, 어느 검색 조각의 번호(1부터)인지, 뒤 조각인가(True) 앞 조각인가(False)). 점수는 0(검색된 것이 아님).
    """
    out: list[tuple[Hit, int, bool]] = []
    if con is None or limit <= 0 or not hits:
        return out
    from eval.retrievers.neo4j import load_section_words
    from src.graph.text_normalize import section_terms

    words = section_words or load_section_words(con)
    terms = section_terms(question, words) if words else []
    if not terms:
        return out
    seen = {(h.rcept_no, h.file_name, h.text) for h in hits}
    for i, h in enumerate(hits):
        if len(out) >= limit:
            break
        if not any(t in h.path for t in terms):
            continue
        row = con.execute("SELECT seq FROM chunk WHERE strategy = ? AND rcept_no = ? AND file_name = ? AND text = ? LIMIT 1",
                          [CHUNK_STRATEGY, h.rcept_no, h.file_name, h.text]).fetchone()
        if row is None:
            continue
        seq = row[0]
        rows = con.execute(
            "SELECT rcept_no, file_name, section_id, path, text, seq FROM chunk "
            "WHERE strategy = ? AND rcept_no = ? AND file_name = ? AND path = ? AND seq IN (?, ?) ORDER BY seq",
            [CHUNK_STRATEGY, h.rcept_no, h.file_name, h.path, seq - 1, seq + 1]).fetchall()
        for r in rows:
            key = (r[0], r[1], r[4])
            if key in seen:
                continue
            seen.add(key)
            out.append((Hit(r[0], r[1], r[2], r[3], 0.0, r[4][:120], r[4]), i + 1, r[5] > seq))
            if len(out) >= limit:
                break
    return out


def ask(question: str, retriever, llm: LLM | None, k: int = 5, con=None, neighbors: int = NEIGHBOR_MAX) -> AskResult:
    """검색 → 이웃 조각 → (그래프 관계표) → LLM. 그래프 검색기(src/graph/query.GraphRetriever)면 retriever.last에 관계표가 남아 있다.

    이웃 조각은 질문에 절 이름이 있을 때, LLM에 넘길 때만 붙인다(--no-llm·use_llm=False의 검색 결과는 검색기가 돌려준 그대로). 번호는 검색 조각 뒤에 이어 붙이고
    꼬리표에 "(이웃: [n]의 다음 조각)"을 적어 답의 인용·화면에서 검색된 조각과 구별된다. 그래프 관계표는 여전히 마지막 번호.
    """
    hits = retriever.search(question, k)
    labels = [passage_label(h, con) for h in hits]
    graph = getattr(retriever, "last", None)
    result = AskResult(question, hits, None, labels, graph, getattr(retriever, "error", None))
    if llm is None:
        return result
    has_graph = graph is not None and bool(graph.relations or graph.derived)   # 겸직 0행의 '없음' 판정은 파생 값에만 있다
    if not hits and not has_graph:
        result.answer = Answer(NOT_FOUND, [], getattr(llm, "name", ""))
        return result
    base = getattr(retriever, "base", retriever)   # GraphRetriever는 안에 전문검색기(base)를 품는다. 그쪽 섹션 사전을 다시 만들지 않고 쓴다
    for nh, n, after in expand_neighbors(question, hits, con, neighbors, getattr(base, "section_words", None)):
        hits.append(nh)
        labels.append(f"{passage_label(nh, con)} (이웃: [{n}]의 {'다음' if after else '앞'} 조각)")
    passages = [Passage(i + 1, labels[i], h.text or h.snippet) for i, h in enumerate(hits)]
    if graph is not None:
        from src.graph.query import to_text
        passages.append(Passage(len(hits) + 1, GRAPH_LABEL, to_text(graph), max_chars=GRAPH_MAX_CHARS))
        # 관계표가 붙은 답은 길다(회사 목록·경유·이력). 기본 상한(600→1000)에서 g05·g12가 잘렸다(2026-11-26) → 그래프 상한(4,000)을 따로 쓴다
        result.answer = llm.answer(question, passages, max_output_tokens=getattr(llm, "max_output_tokens_graph", None))
    else:
        result.answer = llm.answer(question, passages)
    return result


def format_result(r: AskResult) -> str:
    lines = [f"질문: {r.question}", ""]
    if r.answer is not None:
        lines += ["답: " + r.answer.text, ""]
        cited = r.cited_hits
        if cited:
            lines.append("근거:")
            for n, h in cited:
                lines.append(f"  [{n}] {r.labels[n - 1]}")
                lines.append(f"      {VIEWER_URL.format(rcept_no=h.rcept_no)}")
        if r.graph is not None:
            lines.append(f"  [{r.graph_number}] 그래프 관계표" + (" (인용됨)" if r.graph_cited else ""))
        rest = [(i + 1, h) for i, h in enumerate(r.hits) if (i + 1) not in {n for n, _ in cited}]
        if rest:
            lines.append("참고(검색은 됐지만 인용되지 않음):" if cited else "검색된 조각(답에 쓰이지 않음):")
            for n, h in rest:
                lines.append(f"  [{n}] {r.labels[n - 1]}  {VIEWER_URL.format(rcept_no=h.rcept_no)}")
        if r.answer.model:
            lines += ["", f"모델 {r.answer.model}, 토큰 입력 {r.answer.input_tokens:,} / 출력 {r.answer.output_tokens:,}"]
    else:
        lines.append(f"검색 결과 {len(r.hits)}개 (LLM 없음):")
        for i, h in enumerate(r.hits, 1):
            lines.append(f"  [{i}] {r.labels[i - 1]}  점수 {h.score:.3f}")
            lines.append(f"      {h.snippet}")
            lines.append(f"      {VIEWER_URL.format(rcept_no=h.rcept_no)}")
    if r.graph is not None:
        from src.graph.query import to_text
        lines += ["", to_text(r.graph)]
    elif r.graph_error:
        lines += ["", f"(그래프 질의 실패 → 공시 검색만 사용: {r.graph_error})"]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="공시 질의응답")
    ap.add_argument("question")
    ap.add_argument("--retriever", default="graph:hybrid:kure-v3",
                    help="eval.run과 같은 이름 (기본 graph:hybrid:kure-v3 — 그래프 질문이 아니면 neo4j:hybrid와 같다)")
    ap.add_argument("--no-filter", action="store_true", help="회사 필터 끄기 (기본 켬)")
    ap.add_argument("--rerank", choices=["nano"])
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--no-llm", action="store_true", help="검색 결과만 (API 호출 없음)")
    ap.add_argument("--neighbors", type=int, default=NEIGHBOR_MAX, help=f"상위 조각과 같은 절의 앞뒤 조각을 LLM에 함께 넘기는 상한 (기본 {NEIGHBOR_MAX}, 0이면 끔)")
    args = ap.parse_args(argv)

    from eval.retrievers import make_retriever
    from src.loaders.duckdb_store import connect

    llm = None
    if not args.no_llm:
        from src.rag.llm import OpenAILLM
        llm = OpenAILLM()
    with connect(read_only=True) as con:
        retriever = make_retriever(args.retriever, con, corp_filter=not args.no_filter, rerank=args.rerank)
        if llm is not None and hasattr(retriever, "picker"):
            from src.graph.picker import LLMPicker
            retriever.picker = LLMPicker()   # 규칙이 못 고른 질문만 LLM이 템플릿 번호를 고른다
        result = ask(args.question, retriever, llm, args.k, con, neighbors=args.neighbors)
    print(format_result(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
