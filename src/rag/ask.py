"""질문 → 검색(하이브리드) → LLM 답 + 근거(접수번호·섹션·DART 뷰어 링크). Phase 3의 끝 그림 (계획 1절, usecases D3·D5).

    python -m src.rag.ask "2025년 신한은행의 주당 현금배당금은 얼마인가?"
    python -m src.rag.ask "..." --no-llm                 # 검색 결과만 (API 키 없이)
    python -m src.rag.ask "..." --retriever dense:kure-v3:section_path   # Neo4j 없이 DuckDB 벡터 검색으로
    python -m src.rag.ask "..." --k 8 --no-filter

기본 검색기는 neo4j:hybrid:kure-v3 + 회사 필터(0009·0010 결정). 조각은 Neo4j에 들어가 있어야 한다(load_chunks).
근거 출력: 인용된 조각을 먼저, 나머지 검색 결과는 "참고"로. 모든 근거에 DART 뷰어 링크를 붙인다.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field

from eval.metrics import Hit
from src.rag.llm import LLM, NOT_FOUND, Answer, Passage

VIEWER_URL = "https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"


@dataclass
class AskResult:
    question: str
    hits: list[Hit]
    answer: Answer | None = None          # --no-llm이면 None
    labels: list[str] = field(default_factory=list)

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


def ask(question: str, retriever, llm: LLM | None, k: int = 5, con=None) -> AskResult:
    hits = retriever.search(question, k)
    labels = [passage_label(h, con) for h in hits]
    result = AskResult(question, hits, None, labels)
    if llm is None:
        return result
    if not hits:
        result.answer = Answer(NOT_FOUND, [], getattr(llm, "name", ""))
        return result
    passages = [Passage(i + 1, labels[i], h.text or h.snippet) for i, h in enumerate(hits)]
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
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="공시 질의응답")
    ap.add_argument("question")
    ap.add_argument("--retriever", default="neo4j:hybrid:kure-v3", help="eval.run과 같은 이름 (기본 neo4j:hybrid:kure-v3)")
    ap.add_argument("--no-filter", action="store_true", help="회사 필터 끄기 (기본 켬)")
    ap.add_argument("--rerank", choices=["nano"])
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--no-llm", action="store_true", help="검색 결과만 (API 호출 없음)")
    args = ap.parse_args(argv)

    from eval.retrievers import make_retriever
    from src.loaders.duckdb_store import connect

    llm = None
    if not args.no_llm:
        from src.rag.llm import OpenAILLM
        llm = OpenAILLM()
    with connect(read_only=True) as con:
        retriever = make_retriever(args.retriever, con, corp_filter=not args.no_filter, rerank=args.rerank)
        result = ask(args.question, retriever, llm, args.k, con)
    print(format_result(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
