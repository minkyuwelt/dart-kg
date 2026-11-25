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
        per_question.append({"id": q.id, "type": q.type, "phase": q.phase, "question": q.question, **scores, "seconds": seconds,
                             "top": [{"rcept_no": h.rcept_no, "section_id": h.section_id, "path": h.path, "score": h.score} for h in hits[:k]]})
    by_type, by_phase = defaultdict(list), defaultdict(list)
    for p in per_question:
        by_type[p["type"]].append(p)
        by_phase[str(p["phase"])].append(p)
    secs = [p["seconds"] for p in per_question]
    return {
        "overall": summarize(per_question),
        # 질문당 검색 시간(모델 로딩 제외). 재순위·필터의 지연 비교용 (0009 3단계). 첫 질문은 GPU 준비 때문에 느릴 수 있어 중앙값도 둔다
        "latency": {"mean_seconds": round(sum(secs) / len(secs), 3) if secs else None,
                    "median_seconds": round(sorted(secs)[len(secs) // 2], 3) if secs else None},
        "by_type": {t: summarize(v) for t, v in sorted(by_type.items())},
        "by_phase": {t: summarize(v) for t, v in sorted(by_phase.items())},
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
    lat = result.get("latency") or {}
    if lat.get("mean_seconds") is not None:
        print(f"질문당 검색 시간: 평균 {lat['mean_seconds']:.2f}초, 중앙값 {lat['median_seconds']:.2f}초")
    misses = [p for p in result["questions"] if not p["hit_doc"]]
    if misses:
        print(f"\n상위 {k}개에 근거 공시가 없는 질문 {len(misses)}개:")
        for p in misses:
            print(f"  {p['id']} {p['question'][:40]}  (첫 정답 순위: {p['first_rank_doc'] or '없음'})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase 3 평가 세트 실행")
    ap.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    ap.add_argument("--retrieval-only", action="store_true", help="검색만 측정 (LLM 호출 없음)")
    ap.add_argument("--retriever", default="keyword",
                    help="검색 방식 (keyword | chunk:<전략> | dense:<모델>:<전략>). 전략 fixed/section/section_path, 모델 kure-v3/kure-v1/bge-m3")
    ap.add_argument("--filter", choices=["corp"], help="corp: 질문의 회사명으로 그 회사 보고서만 (chunk·dense)")
    ap.add_argument("--rerank", choices=["nano"], help="nano: 상위 30개를 KURE-Reranker-nano로 다시 매김 (chunk·dense, 노트북)")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--label", default="", help="결과 파일 이름에 붙일 설정 이름")
    args = ap.parse_args(argv)
    if not args.retrieval_only:
        sys.exit("LLM 단계는 9번 PR(feat/rag-ask)에서 구현합니다. 지금은 --retrieval-only로 실행하세요.")

    questions = load_questions(args.questions)
    target = [q for q in questions if q.is_ready and q.type != "없음"]
    skipped = len(questions) - len(target)
    print(f"질문 {len(questions)}개 중 검색 평가 대상 {len(target)}개 (draft·'없음' {skipped}개 제외)")
    if not target:
        print("ready 상태의 질문이 없습니다. eval/README.md를 보고 질문을 채우세요.")
        return 1

    with connect(read_only=True) as con:
        retriever = make_retriever(args.retriever, con, corp_filter=args.filter == "corp", rerank=args.rerank)
        result = evaluate(target, retriever, args.k)
    result["config"] = {"retriever": retriever.name, "filter": args.filter, "rerank": args.rerank, "k": args.k, "label": args.label, "questions": str(args.questions),
                        "run_at": datetime.now().isoformat(timespec="seconds")}
    print_report(result, args.k)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now():%Y%m%d_%H%M}_{retriever.name}" + (f"_{args.label}" if args.label else "") + ".json"
    out = RESULTS_DIR / name
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n결과 저장: {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
