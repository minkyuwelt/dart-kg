"""라우팅 정확도: 질문마다 라우터가 고른 템플릿이 기대와 맞는지 센다 (0011 "남은 평가").

왜 따로 세나: 채점표(eval.run)만 보면 답이 틀렸을 때 "라우터가 잘못 골랐는지, 템플릿 결과가 틀렸는지, 검색이 못 찾았는지"를
구분할 수 없다. 라우팅 표를 먼저 만들어 두면 답이 틀린 질문에서 어느 단계 책임인지 바로 보인다.

기대값 (판정은 맞음 / 허용 / 틀림 셋)
- 그래프 질문(g01~g12): 0011 결정 4의 표. `eval/graph_draft.py`의 SPECS가 쓰는 풀이 이름(solver)으로 템플릿 목록을 정한다 (`SOLVER_TEMPLATES`).
- 일반 질문(Phase 3, '없음' 포함): 템플릿 없음(그래프를 쓰지 않음)이 맞음. 그래프로 가면 틀림.
  단, **한 회사의 한 시점 최대주주·임원**을 묻는 일반 질문(예: "2025년 말 기준 우리은행의 최대주주는?")이 `facts_at`(T7)로 가면 '허용'이다.
  T7은 바로 그 질문용 템플릿이고 관계 표에 답이 그대로 있다. 2026-11-26 첫 측정에서 LLM 고르기가 이런 질문 19개를 모두 T7로 보냈고,
  배당·연혁·직원 수처럼 관계 표에 없는 22개는 보내지 않았다. 허용은 "검색만으로도 답할 수 있어 그래프가 필수는 아니다"라는 뜻이다.

두 단계로 센다
1) 규칙만: `route(picker=None)`. LLM 호출 없음.
2) 규칙 + LLM 고르기: 규칙이 못 고른 질문만 `LLMPicker`가 번호를 고른다 (`--picker`, 노트북, OPENAI_API_KEY). 호출 수 = 규칙이 None을 낸 질문 수.

사용 (노트북): python -m eval.routing [--picker] [--questions 경로]
결과는 화면과 eval/results/<시각>_routing.md (커밋하지 않음). 숫자는 0011 평가 절에 옮겨 적는다.
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import duckdb

from eval.graph_draft import SPECS
from eval.questions import DEFAULT_QUESTIONS, Question, load_questions
from src.graph.load_relations import load_lineage
from src.graph.router import Plan, route
from src.loaders.duckdb_store import REPO_ROOT, connect

RESULTS_DIR = REPO_ROOT / "eval" / "results"

# graph_draft의 풀이 이름 → 라우터가 골라야 하는 템플릿 순서 (0011 결정 4 표)
SOLVER_TEMPLATES: dict[str, list[str]] = {
    "holder_chain": ["holder_chain"],
    "holder_then_owned": ["holder_chain", "descendants"],
    "holder_then_title": ["holder_chain", "facts_at"],
    "descendants_count": ["descendants"],
    "indirect_only": ["descendants"],
    "unresolved_count": ["descendants"],
    "lineage": ["corp_lineage"],
    "lineage_holders": ["corp_lineage", "facts_at"],
    "shared_outside_holdings": ["shared_positions"],
    "shared_in_group": ["shared_positions"],
    "person_history": ["person_history"],
}
EXPECTED_BY_ID: dict[str, list[str]] = {s.id: SOLVER_TEMPLATES[s.solver] for s in SPECS}


# 일반 질문 중 facts_at(T7)로 가도 괜찮은 꼴: 한 회사의 한 시점 최대주주·대표이사·사외이사·임원
_FACTS_AT_OK_RE = re.compile(r"최대주주|대표이사|사외이사|임원")


@dataclass
class RoutingRow:
    id: str
    kind: str                 # 그래프 질문이면 graph_kind(추론·계층·의미·통합), 아니면 질문 유형(정형표·서술·시점·없음)
    expected: list[str]
    got: list[str]
    rule: str | None          # Plan.rule (어느 규칙이 골랐나). None이면 그래프 안 씀
    facts_at_ok: bool = False  # 일반 질문인데 facts_at으로 가면 허용인가
    used_picker: bool = False
    verdict: str = field(init=False)   # 맞음 / 허용 / 틀림

    def __post_init__(self):
        if self.got == self.expected:
            self.verdict = "맞음"
        elif not self.expected and self.facts_at_ok and self.got == ["facts_at"]:
            self.verdict = "허용"
        else:
            self.verdict = "틀림"

    @property
    def ok(self) -> bool:
        return self.verdict != "틀림"


def expected_templates(q: Question) -> list[str]:
    """그래프 질문은 0011 표, 그 밖은 빈 목록(그래프를 쓰지 않아야 함)."""
    return list(EXPECTED_BY_ID.get(q.id, []))


def facts_at_allowed(q: Question) -> bool:
    """일반 질문 중 한 시점의 최대주주·임원을 묻는 것. '없음' 유형은 공시에 없는 것을 묻는 질문이라 허용하지 않는다."""
    return q.id not in EXPECTED_BY_ID and q.type != "없음" and bool(_FACTS_AT_OK_RE.search(q.question))


def plan_templates(plan: Plan | None) -> list[str]:
    return [s.template for s in plan.steps] if plan else []


class CountingPicker:
    """picker를 감싸 호출 수를 센다. 규칙이 못 고른 질문에서만 불린다."""

    def __init__(self, picker):
        self.picker, self.calls = picker, 0

    def __call__(self, question, as_of, mentions):
        self.calls += 1
        return self.picker(question, as_of, mentions)


def route_all(questions: list[Question], con: duckdb.DuckDBPyConnection, lineage: list[dict] | None = None,
              picker=None, today: date | None = None) -> list[RoutingRow]:
    """질문마다 route()를 돌려 기대 템플릿과 비교한다. picker가 있으면 규칙이 못 고른 질문만 picker로 넘어간다."""
    counting = CountingPicker(picker) if picker else None
    rows = []
    for q in questions:
        before = counting.calls if counting else 0
        plan = route(q.question, con, today=today, lineage=lineage, picker=counting)
        rows.append(RoutingRow(q.id, q.graph_kind or q.type, expected_templates(q), plan_templates(plan),
                               plan.rule if plan else None, facts_at_ok=facts_at_allowed(q),
                               used_picker=bool(counting and counting.calls > before)))
    return rows


def summarize(rows: list[RoutingRow]) -> dict:
    graph = [r for r in rows if r.expected]
    general = [r for r in rows if not r.expected]
    return {
        "graph_total": len(graph), "graph_ok": sum(r.ok for r in graph),
        "general_total": len(general), "general_ok": sum(r.verdict == "맞음" for r in general),
        "general_allowed": sum(r.verdict == "허용" for r in general),   # 한 시점 최대주주·임원 질문이 facts_at으로 감
        "general_wrong": sum(r.verdict == "틀림" for r in general),     # 그 밖의 일반 질문이 그래프로 감
        "graph_missed": sum(1 for r in graph if not r.got),        # 그래프 질문인데 그래프를 안 씀
        "graph_wrong": sum(1 for r in graph if r.got and not r.ok),  # 그래프로 갔지만 다른 템플릿
        "picker_calls": sum(r.used_picker for r in rows),
    }


def markdown(rows: list[RoutingRow], label: str) -> str:
    s = summarize(rows)
    out = [f"# 라우팅 정확도 ({label}, {datetime.now():%Y-%m-%d %H:%M})", "",
           f"- 그래프 질문: {s['graph_ok']}/{s['graph_total']} (그래프 안 씀 {s['graph_missed']}, 다른 템플릿 {s['graph_wrong']})",
           f"- 일반 질문: 그래프 안 씀 {s['general_ok']}/{s['general_total']}, 허용(한 시점 최대주주·임원 → facts_at) {s['general_allowed']}, 틀림 {s['general_wrong']}",
           f"- LLM 고르기 호출: {s['picker_calls']}건", "",
           "| id | 구분 | 기대 | 고른 것 | 규칙 | 판정 |", "|---|---|---|---|---|---|"]
    for r in rows:
        if r.verdict == "맞음" and not r.expected:
            continue   # 일반 질문이 그래프를 안 쓴 것은 정상이라 표에서 뺀다 (수만 위에 적음)
        out.append(f"| {r.id} | {r.kind} | {', '.join(r.expected) or '-'} | {', '.join(r.got) or '-'} | {r.rule or '-'}"
                   f"{' (LLM)' if r.used_picker else ''} | {r.verdict} |")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="라우팅 정확도 (0011 남은 평가)")
    ap.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    ap.add_argument("--picker", action="store_true", help="규칙이 못 고른 질문에 LLM 고르기를 돌린다 (노트북, OPENAI_API_KEY, 호출 수 = 그 질문 수)")
    args = ap.parse_args(argv)
    questions = [q for q in load_questions(args.questions) if q.is_ready]
    picker = None
    if args.picker:
        from src.graph.picker import LLMPicker
        picker = LLMPicker()
    with connect(read_only=True) as con:
        rows = route_all(questions, con, load_lineage(), picker=picker)
    label = "규칙+LLM 고르기" if args.picker else "규칙만"
    text = markdown(rows, label)
    print(text)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    path = RESULTS_DIR / f"{datetime.now():%Y%m%d_%H%M}_routing{'_picker' if args.picker else ''}.md"
    path.write_text(text, encoding="utf-8")
    print(f"저장: {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
