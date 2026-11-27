"""평가 실행. 질문마다 검색을 돌려 hit@k·MRR을 계산하고 결과를 파일로 남긴다.

    python -m eval.run --retrieval-only                       # 키워드 기본선, 상위 5개
    python -m eval.run --retrieval-only --retriever keyword --k 10 --label baseline
    python -m eval.run                                        # LLM까지 (9번 PR에서 구현)

결과: eval/results/{날짜}_{검색방식}_{label}.json (커밋하지 않음). 설정끼리 비교할 때 이 파일을 본다.
status가 draft인 질문과 type이 "없음"인 질문은 검색 지표에서 뺀다 (없음 유형은 LLM 단계에서 "모른다"가 정답).
DART API를 호출하지 않고, DuckDB는 읽기 전용으로 연다.
"""

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from eval.metrics import score_question, summarize
from eval.questions import DEFAULT_QUESTIONS, load_questions
from eval.retrievers import make_retriever
from src.loaders.duckdb_store import REPO_ROOT, connect

RESULTS_DIR = REPO_ROOT / "eval" / "results"


def evaluate(questions, retriever, k: int) -> dict:
    """ready이고 '없음'이 아닌 질문마다 검색 → 지표. 결과는 전체·유형별·phase별 요약과 질문별 상세."""
    per_question = []
    for q in questions:
        t0 = time.perf_counter()
        hits = retriever.search(q.question, k)
        seconds = round(time.perf_counter() - t0, 3)
        scores = score_question(hits, q.evidence_rcept_no, q.evidence_section, k)
        per_question.append({"id": q.id, "type": q.type, "phase": q.phase, "graph_kind": q.graph_kind, "question": q.question, **scores, "seconds": seconds,
                             "top": [{"rcept_no": h.rcept_no, "section_id": h.section_id, "path": h.path, "score": h.score} for h in hits[:k]]})
    by_type, by_phase, by_kind = defaultdict(list), defaultdict(list), defaultdict(list)
    for p in per_question:
        by_type[p["type"]].append(p)
        by_phase[str(p["phase"])].append(p)
        if p["graph_kind"]:
            by_kind[p["graph_kind"]].append(p)
    secs = [p["seconds"] for p in per_question]
    return {
        "overall": summarize(per_question),
        # 질문당 검색 시간(모델 로딩 제외). 재순위·필터의 지연 비교용 (0009 3단계). 첫 질문은 GPU 준비 때문에 느릴 수 있어 중앙값도 둔다
        "latency": {"mean_seconds": round(sum(secs) / len(secs), 3) if secs else None,
                    "median_seconds": round(sorted(secs)[len(secs) // 2], 3) if secs else None},
        "by_type": {t: summarize(v) for t, v in sorted(by_type.items())},
        "by_phase": {t: summarize(v) for t, v in sorted(by_phase.items())},
        "by_graph_kind": {t: summarize(v) for t, v in sorted(by_kind.items())},   # 그래프 질문 갈래별 (0011)
        "questions": per_question,
    }


def print_report(result: dict, k: int) -> None:
    def row(name, s):
        print(f"{name:<12}{s['n']:>4}{s['hit_doc']:>10.3f}{s['hit_section']:>10.3f}{s['mrr_doc']:>8.3f}{s['mrr_section']:>8.3f}")

    print(f"{'구분':<12}{'질문':>4}{f'hit@{k} 공시':>10}{f'hit@{k} 섹션':>10}{'MRR 공시':>8}{'MRR 섹션':>8}")
    row("전체", result["overall"])
    for t, s in result["by_type"].items():
        row(f"  {t}", s)
    for t, s in result["by_phase"].items():
        row(f"  phase {t}", s)
    for t, s in result.get("by_graph_kind", {}).items():
        row(f"  그래프 {t}", s)
    lat = result.get("latency") or {}
    if lat.get("mean_seconds") is not None:
        print(f"질문당 검색 시간: 평균 {lat['mean_seconds']:.2f}초, 중앙값 {lat['median_seconds']:.2f}초")
    misses = [p for p in result["questions"] if not p["hit_doc"]]
    if misses:
        print(f"\n상위 {k}개에 근거 공시가 없는 질문 {len(misses)}개:")
        for p in misses:
            print(f"  {p['id']} {p['question'][:40]}  (첫 정답 순위: {p['first_rank_doc'] or '없음'})")


def answers_markdown(rows: list[dict], config: dict) -> str:
    """사람 채점용 표. 질문·정답·LLM 답·인용 근거를 나란히. 채점 칸은 비워 둔다 (맞음/부분/틀림/근거없는주장)."""
    out = [f"# LLM 답변 채점표 ({config['run_at']})", "", f"설정: {config}", "",
           "채점 기준: 맞음 / 부분 / 틀림 / 근거없는주장(조각에 없는 말을 함). '없음' 유형은 '공시에서 찾지 못했습니다'가 정답.", ""]
    for r in rows:
        out += [f"## {r['id']} ({r['type']}) {r['question']}", "", f"- 정답: {r['gold']}", f"- LLM 답: {r['answer']}",
                f"- 인용: {', '.join(r['cited']) or '(없음)'}", f"- 근거 공시 맞음(검색): {'예' if r['hit_doc'] else '아니오'}",
                f"- 그래프: {', '.join(r.get('templates') or []) or ('관계표 있음' if r.get('graph') else '안 씀')}",
                f"- 토큰: 입력 {r['input_tokens']:,} / 출력 {r['output_tokens']:,}" + (" **(상한에 잘림)**" if r.get("truncated") else ""), "- 채점: ", ""]
    return "\n".join(out)


def picker_on(choice: str, llm_mode: bool) -> bool:
    """--picker auto는 LLM 답을 만들 때만 켠다. src.rag.ask CLI·API가 LLM이 있을 때 고르기를 붙이는 것과 같게 맞춘다."""
    return llm_mode if choice == "auto" else choice == "on"


def attach_picker(retriever, choice: str, llm_mode: bool) -> None:
    """그래프 검색기(picker 속성이 있음)에만 LLM 고르기를 붙인다. 다른 검색기는 그대로."""
    if picker_on(choice, llm_mode) and hasattr(retriever, "picker"):
        from src.graph.picker import LLMPicker
        retriever.picker = LLMPicker()


def run_with_llm(args, target, llm=None, retriever=None) -> int:
    """검색 + LLM 답을 만들어 채점표(markdown)와 JSON으로 남긴다. 채점은 사람이 한다 (계획 4절: LLM 채점은 사람 채점과 일치율 확인 뒤에만)."""
    from eval.metrics import score_question as _score
    from src.rag.ask import NEIGHBOR_MAX, VIEWER_URL, ask
    from src.rag.llm import OpenAILLM

    llm = llm or OpenAILLM()
    rows, t_in, t_out = [], 0, 0
    with connect(read_only=True) as con:
        retriever = retriever or make_retriever(args.retriever, con, corp_filter=args.filter == "corp", rerank=args.rerank)
        attach_picker(retriever, getattr(args, "picker", "auto"), llm_mode=True)
        for i, q in enumerate(target, 1):
            res = ask(q.question, retriever, llm, args.k, con, neighbors=getattr(args, "neighbors", NEIGHBOR_MAX))
            sc = _score(res.hits, q.evidence_rcept_no, q.evidence_section, args.k) if q.type != "없음" else {"hit_doc": None}
            a = res.answer
            cited = [f"[{n}] {res.labels[n - 1]} {VIEWER_URL.format(rcept_no=h.rcept_no)}" for n, h in res.cited_hits]
            rows.append({"id": q.id, "type": q.type, "question": q.question, "gold": q.answer, "answer": a.text, "citations": a.citations, "cited": cited,
                         "not_found": a.not_found, "answered_none": a.text.strip().startswith("없음"), "hit_doc": sc.get("hit_doc"), "input_tokens": a.input_tokens, "output_tokens": a.output_tokens,
                         "truncated": a.truncated, "graph": res.graph is not None,
                         "templates": [s.template for s in res.graph.plan.steps] if res.graph is not None and getattr(res.graph, "plan", None) else []})
            t_in += a.input_tokens
            t_out += a.output_tokens
            print(f"  {i}/{len(target)} {q.id} {'(없음)' if a.not_found else '답 ' + str(len(a.citations)) + '개 인용'}  토큰 {a.input_tokens:,}/{a.output_tokens:,}")
    config = {"retriever": retriever.name, "filter": args.filter, "rerank": args.rerank, "k": args.k, "neighbors": getattr(args, "neighbors", NEIGHBOR_MAX), "label": args.label, "llm": llm.name,
              "picker": picker_on(getattr(args, "picker", "auto"), llm_mode=True), "questions": str(args.questions), "run_at": datetime.now().isoformat(timespec="seconds")}
    n_none = sum(1 for r in rows if r["type"] == "없음")
    none_ok = sum(1 for r in rows if r["type"] == "없음" and r["not_found"])
    false_none = sum(1 for r in rows if r["type"] != "없음" and r["not_found"])
    n_answered_none = sum(1 for r in rows if r.get("answered_none"))   # "없음"으로 시작하는 답(그래프 '없음' 판정, '-' 배당). "찾지 못함"(모름)과 다르다
    n_trunc = sum(1 for r in rows if r["truncated"])
    n_graph = sum(1 for r in rows if r["graph"])
    print(f"\n질문 {len(rows)}개, 토큰 입력 {t_in:,} / 출력 {t_out:,}. '없음' {n_none}개 중 '찾지 못함'으로 답한 것 {none_ok}개, 답이 있는 질문인데 '찾지 못함' {false_none}개, '없음'으로 답한 것 {n_answered_none}개, "
          f"출력 상한에 잘린 답 {n_trunc}개, 그래프 관계표가 붙은 질문 {n_graph}개")
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"{datetime.now():%Y%m%d_%H%M}_llm_{retriever.name}" + (f"_{args.label}" if args.label else "")
    (RESULTS_DIR / f"{stem}.json").write_text(json.dumps({"config": config, "rows": rows, "tokens": {"input": t_in, "output": t_out}}, ensure_ascii=False, indent=1), encoding="utf-8")
    md = RESULTS_DIR / f"{stem}.md"
    md.write_text(answers_markdown(rows, config), encoding="utf-8")
    shown = md.relative_to(REPO_ROOT) if md.is_relative_to(REPO_ROOT) else md
    print(f"채점표: {shown}  (채점 칸을 채운 뒤 결과를 0010에 적는다)")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase 3 평가 세트 실행")
    ap.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    ap.add_argument("--retrieval-only", action="store_true", help="검색만 측정 (LLM 호출 없음). 없으면 LLM 답까지 만들어 채점표(markdown)를 남긴다 — 노트북, OPENAI_API_KEY 필요")
    ap.add_argument("--retriever", default="keyword",
                    help="검색 방식 (keyword | chunk:<전략> | dense:<모델>:<전략> | neo4j:vector:<모델> | neo4j:hybrid:<모델> | neo4j:fulltext)")
    ap.add_argument("--filter", choices=["corp"], help="corp: 질문의 회사명으로 그 회사 보고서만 (chunk·dense)")
    ap.add_argument("--rerank", choices=["nano"], help="nano: 상위 30개를 KURE-Reranker-nano로 다시 매김 (chunk·dense, 노트북)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--neighbors", type=int, default=4, help="LLM 모드에서 상위 조각과 같은 절의 앞뒤 조각을 함께 넘기는 상한 (기본 4, 0이면 끔 — 비교용). 검색 지표에는 영향 없음")
    ap.add_argument("--label", default="", help="결과 파일 이름에 붙일 설정 이름")
    ap.add_argument("--include-none", action="store_true", help="LLM 평가에 '없음' 유형도 넣는다 (정답은 '공시에서 찾지 못했습니다')")
    ap.add_argument("--id", nargs="*", help="질문 id만 (예: --id d055 d068). 고친 뒤 몇 개만 다시 볼 때 — LLM 호출을 그 수만큼으로 줄인다")
    ap.add_argument("--picker", choices=["auto", "on", "off"], default="auto",
                    help="그래프 검색기의 LLM 템플릿 고르기(규칙이 못 고른 질문만, 질문당 OpenAI 1건). auto: LLM 답을 만들 때는 켬(src.rag.ask·API와 같게), --retrieval-only면 끔")
    args = ap.parse_args(argv)

    questions = load_questions(args.questions)
    with_none = (not args.retrieval_only) and args.include_none
    target = [q for q in questions if q.is_ready and (with_none or q.type != "없음") and (not args.id or q.id in args.id)]
    skipped = len(questions) - len(target)
    print(f"질문 {len(questions)}개 중 평가 대상 {len(target)}개 (draft{'' if with_none else "·'없음'"} {skipped}개 제외)")
    if not target:
        print("ready 상태의 질문이 없습니다. eval/README.md를 보고 질문을 채우세요.")
        return 1

    if not args.retrieval_only:
        return run_with_llm(args, target)

    with connect(read_only=True) as con:
        retriever = make_retriever(args.retriever, con, corp_filter=args.filter == "corp", rerank=args.rerank)
        attach_picker(retriever, args.picker, llm_mode=False)
        result = evaluate(target, retriever, args.k)
    result["config"] = {"retriever": retriever.name, "filter": args.filter, "rerank": args.rerank, "k": args.k, "label": args.label, "questions": str(args.questions),
                        "picker": picker_on(args.picker, llm_mode=False), "run_at": datetime.now().isoformat(timespec="seconds")}
    print_report(result, args.k)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now():%Y%m%d_%H%M}_{retriever.name}" + (f"_{args.label}" if args.label else "") + ".json"
    out = RESULTS_DIR / name
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n결과 저장: {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
