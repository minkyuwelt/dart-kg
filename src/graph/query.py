"""그래프 질의 실행: 라우터 계획 → 템플릿 실행 → 관계 표 + 파생 값 + 근거 조각 → (선택) 공시 글 검색과 RRF (결정 0011 결정 4).

흐름
    질문 ─ route() ─▶ Plan(단계들) ─ execute() ─▶ 경로들 ─ relation_rows() ─▶ 관계 표(사실) + derive() ─▶ 파생 값
                                                        └─ T8 evidence_chunks ─▶ 근거 조각(Hit) ──┐
    질문 ─ (Phase 3 하이브리드 검색) ─────────────────────────────────────────▶ 조각(Hit) ─────────┴─ RRF ─▶ k개

- 사실 층: 관계 표의 행은 그래프에 있는 그대로 (값·유효 기간·근거 접수번호). 여기서 바꾸지 않는다.
- 파생 층: 간접 지분율(경로 지분율의 곱), "자회사를 거쳐서만", 연속 보고서의 재직 기간 합치기. 저장하지 않고 여기서 계산하며
  행마다 rule(어떤 규칙으로 계산했나)을 붙인다 (0011 결정 3).
- 실행 기록(trace): 템플릿 이름·채운 빈칸·경로 수. 답이 틀렸을 때 라우터·템플릿·데이터 중 어디 잘못인지 보려고.

검색기 이름 (eval.run·src.rag.ask의 --retriever):
    graph:only              그래프 근거 조각만 (라우터가 못 고르면 빈 결과)
    graph:hybrid:<모델>      그래프 근거 조각 + neo4j:hybrid:<모델> → RRF. 라우터가 못 고르면 neo4j:hybrid 그대로

CLI (노트북, Neo4j에 조각·관계가 들어가 있어야 함):
    python -m src.graph.query "2025년 12월 31일 기준 우리은행의 최대주주의 최대주주는 누구인가?"
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date

from eval.metrics import Hit
from eval.retrievers.neo4j import rrf_fuse
from src.graph import templates as T
from src.graph.router import Plan, route

MAX_RELATION_ROWS = 50   # LLM에 넘기는 관계 행 상한 (phase4-research 3절 5번 예산)
MAX_DERIVED_ROWS = 80    # LLM에 넘기는 파생 값 줄 상한. 계산(derive)은 전체로 하고 표에만 자른다 (50 → 80: g05 간접만 45곳 + 직접 15곳이 넘침, 2026-11-26)
EVIDENCE_LIMIT = 20
EVIDENCE_PER_DOC = 3   # 보고서 하나에서 가져올 조각 수. 검색 평가가 상위 5개를 보므로 보고서 2곳 이상이 그 안에 들도록


@dataclass
class StepResult:
    template: str
    params: dict
    paths: list[dict]
    note: str | None = None
    scope: list[dict] | None = None   # shared_positions가 0행일 때: 대조한 회사별 임원 수 (position_scope). "없음"의 근거


@dataclass
class GraphContext:
    question: str
    plan: Plan
    steps: list[StepResult]
    relations: list[dict] = field(default_factory=list)
    derived: list[dict] = field(default_factory=list)
    evidence: list[Hit] = field(default_factory=list)
    truncated: int = 0

    def trace(self) -> dict:
        return {"rule": self.plan.rule, "as_of": self.plan.as_of.isoformat(),
                "mentions": [{"corp_code": m.corp_code, "text": m.text, "current": m.current} for m in self.plan.mentions],
                "steps": [{"template": s.template, "params": s.params, "paths": len(s.paths), "note": s.note} for s in self.steps],
                "relations": len(self.relations), "derived": len(self.derived), "evidence": len(self.evidence), "truncated": self.truncated}


def _s(v) -> str | None:
    """Neo4j Date·숫자를 문자열로 (표에 그대로 적기 위해)."""
    if v is None:
        return None
    return v.iso_format() if hasattr(v, "iso_format") else str(v)


# ---------------------------------------------------------------- 앞 단계 결과로 빈칸 채우기

def top_corp_holders(prev: StepResult) -> list[dict]:
    """1단계 위 주주 중 corp_code가 있는 회사, 지분율 큰 순 첫 번째 → {"corp": ...}. 회사 주주가 없으면 빈 목록."""
    best = None
    for p in prev.paths:
        if len(p["nodes"]) < 2:
            continue
        holder, rel = p["nodes"][1], p["rels"][0]
        if holder.get("has_corp_code") and holder.get("label") == "Corp":
            if best is None or (rel.get("pct") or 0) > (best[1].get("pct") or 0):
                best = (holder, rel)
    return [{"corp": best[0]["key"]}] if best else []


FROM_PREV = {"top_corp_holders": top_corp_holders}


def execute(driver, plan: Plan, database: str = "neo4j") -> list[StepResult]:
    out: list[StepResult] = []
    for step in plan.steps:
        params = dict(step.params)
        if step.from_prev:
            fills = FROM_PREV[step.from_prev](out[-1]) if out else []
            if not fills:
                out.append(StepResult(step.template, params, [], note="앞 단계에서 이어 갈 회사를 찾지 못함"))
                continue
            params.update(fills[0])
        result = StepResult(step.template, params, T.run(driver, step.template, params, database))
        if step.template == "shared_positions":
            # 대조 범위를 늘 함께 센다. 0행이면 "없음"인지 "데이터가 없음"인지 가리는 근거(대조한 사람이 0명이면 '없음'이라 말하지 않는다, g10).
            # 행이 있어도 임원 표가 없는 회사(사업보고서를 내지 않는 자회사)가 몇 곳인지 적어야 "N명이 전부"가 아님을 LLM이 안다 (g11, 2026-11-27)
            scope_params = {k: params.get(k) for k in ("as_of", "outside_only", "corps")}
            result.scope = T.run(driver, "position_scope", scope_params, database)
        out.append(result)
    return out


# ---------------------------------------------------------------- 관계 표 (사실)

def _value(rel: dict) -> str:
    t = rel.get("type")
    if t == "OWNS":
        parts = [f"{rel['pct']}%" if rel.get("pct") is not None else "지분율 없음"]
        if rel.get("book_value") is not None:
            parts.append(f"장부가액 {rel['book_value']:,}원")
        if rel.get("relate"):
            parts.append(rel["relate"])
        return ", ".join(parts)
    if t == "HOLDS_POSITION":
        text = " / ".join(x for x in (rel.get("title"), rel.get("registered")) if x)
        if rel.get("ceo") and "대표이사" not in (rel.get("title") or ""):
            text += " (담당업무: 대표이사)"   # 직위 칸에는 '회장'만 있는 경우. LLM이 대표이사를 알아보게
        return text
    if t == "FORMERLY":
        return " ".join(x for x in (_s(rel.get("on")), rel.get("reason")) if x)
    return ""


def relation_rows(steps: list[StepResult]) -> list[dict]:
    """경로들 → 관계 행 (같은 관계는 한 번만). 방향은 관계의 실제 방향(start → end) 그대로."""
    rows, seen = [], set()
    for s in steps:
        for p in s.paths:
            nodes, rels = p["nodes"], p["rels"]
            for i, rel in enumerate(rels):
                key = rel.get("row_key") or (rel.get("type"), nodes[i]["key"], nodes[i + 1]["key"])
                if key in seen:
                    continue
                seen.add(key)
                a, b = nodes[i], nodes[i + 1]
                if rel.get("start_key") == b["key"]:
                    a, b = b, a  # 위로 따라간 경로는 노드 순서가 (회사 ← 주주)라서 관계 방향(주주 → 회사)으로 뒤집는다
                rows.append({"src": a["name"], "src_key": a["key"], "rel": rel.get("type"), "dst": b["name"], "dst_key": b["key"],
                             "dst_has_corp_code": b.get("has_corp_code"), "value": _value(rel),
                             "valid_from": _s(rel.get("valid_from")), "valid_to": _s(rel.get("valid_to")),
                             "source_rcept_no": rel.get("source_rcept_no") or rel.get("evidence_rcept_no"),
                             "source_table": rel.get("source_table") or rel.get("type"), "template": s.template})
    return rows


# ---------------------------------------------------------------- 파생 값 (규칙 계산, 저장하지 않음)

def derive(steps: list[StepResult]) -> list[dict]:
    out: list[dict] = []
    for s in steps:
        if s.template == "descendants":
            out += _derive_descendants(s)
        elif s.template == "person_history":
            out += _derive_tenures(s)
        elif s.template == "shared_positions":
            out += _derive_no_shared(s) if not s.paths else _derive_shared_scope(s)
        elif s.template == "corp_lineage":
            for p in s.paths:
                for n in p["nodes"]:
                    if len(n.get("names") or []) > 1:
                        out.append({"rule": "공시에 나온 이름 목록", "subject": n["name"], "value": " → ".join(n["names"])})
    return out


def _derive_descendants(s: StepResult) -> list[dict]:
    """경로 끝 회사마다: 가장 짧은 경로 길이, 간접 지분율(경로 지분율 곱의 최댓값), 직접/간접, corp_code 유무."""
    best: dict[str, dict] = {}
    for p in s.paths:
        end = p["nodes"][-1]
        pct = 1.0
        for r in p["rels"]:
            pct *= (r.get("pct") or 0) / 100
        via = " → ".join(n["name"] for n in p["nodes"][1:-1])
        cur = best.get(end["key"])
        hops = len(p["rels"])
        if cur is None:
            best[end["key"]] = {"name": end["name"], "min_hops": hops, "pct": pct, "via": via, "has_corp_code": end.get("has_corp_code")}
        else:
            if hops < cur["min_hops"]:
                cur["min_hops"], cur["via"] = hops, via
            cur["pct"] = max(cur["pct"], pct)
    out = []
    for key, b in sorted(best.items(), key=lambda kv: (kv[1]["min_hops"], kv[1]["name"])):
        kind = "직접" if b["min_hops"] == 1 else f"간접만({b['min_hops']}단계, {b['via']} 경유)"
        out.append({"rule": "출자 경로: 직접/간접, 경로 지분율의 곱", "subject": b["name"],
                    "value": f"{kind}, 간접 지분율 {b['pct'] * 100:.2f}%" + ("" if b["has_corp_code"] else ", 국내 공시 대상 아님(corp_code 없음)")})
    hop_counts: dict[int, int] = {}
    for b in best.values():
        hop_counts[b["min_hops"]] = hop_counts.get(b["min_hops"], 0) + 1
    # 직접 출자(1단계) 중 장부가액 최대: "출자한 회사 중 장부가액이 가장 큰 곳" 질문의 답. 표에서 LLM이 최대를 고르다 틀리지 않게 규칙으로 계산
    direct = [(p["rels"][0].get("book_value"), p["nodes"][-1]["name"]) for p in s.paths
              if len(p["rels"]) == 1 and p["rels"][0].get("book_value") is not None]
    if direct:
        bv, name = max(direct)
        out.insert(0, {"rule": "직접 출자 중 장부가액 최대", "subject": s.params.get("corp"),
                       "value": f"{name} {bv:,.0f}원 ({bv / 1_000_000:,.0f}백만원, 직접 출자 {len(direct)}곳 중)"})
    # 합계 줄을 맨 앞에: 회사별 줄이 1,000개를 넘으면 LLM 표(to_text)는 앞쪽만 넘기므로 합계가 잘리지 않게
    out.insert(0, {"rule": "출자 경로: 단계별 회사 수", "subject": s.params.get("corp"),
                   "value": ", ".join(f"{h}단계 {n}곳" for h, n in sorted(hop_counts.items())) + f" (합계 {len(best)}곳, "
                            f"corp_code 없음 {sum(1 for b in best.values() if not b['has_corp_code'])}곳)"})
    return out


def _scope_parts(s: StepResult) -> tuple[list[dict], list[dict], int, str]:
    """(임원이 있는 회사, 임원 표가 없어 0명인 회사, 임원 합계, '사외이사'/'임원')"""
    scope = s.scope or []
    have = [r for r in scope if int(r.get("persons") or 0) > 0]
    none = [r for r in scope if int(r.get("persons") or 0) == 0]
    who = "사외이사" if s.params.get("outside_only") else "임원"
    return have, none, sum(int(r["persons"]) for r in have), who


def _missing_note(none: list[dict], who: str) -> str:
    if not none:
        return ""
    names = ", ".join(r["corp_name"] for r in none)
    return (f" 임원 표가 없는 {len(none)}곳({names})은 셈에서 빠짐 — 사업보고서를 내지 않는 회사(감사보고서만)라 {who} 데이터가 없고,"
            f" 그 회사 쪽 겸직은 그래프가 알 수 없음")


def _derive_shared_scope(s: StepResult) -> list[dict]:
    """겸직 행이 있을 때의 대조 범위. 몇 곳 중 몇 곳의 임원 몇 명을 봤고, 임원 표가 없어 빠진 회사가 있는지 (g11, 2026-11-27).
    `scope`가 없으면(옛 호출) 아무것도 적지 않는다."""
    if not s.scope:
        return []
    have, none, total, who = _scope_parts(s)
    per = ", ".join(f"{r['corp_name']} {r['persons']}명" for r in have)
    return [{"rule": "겸직 대조 범위 (임원 표가 있는 회사만 닫힌 세계)", "subject": "겸직",
             "value": f"기준일 {s.params.get('as_of')} 회사 {len(have) + len(none)}곳 중 임원 표가 있는 {len(have)}곳의 {who} {total}명을 대조"
                      f" (회사별: {per}).{_missing_note(none, who)}"}]


def _derive_no_shared(s: StepResult) -> list[dict]:
    """겸직 템플릿이 0행일 때의 판정. 대조한 임원이 1명 이상이면 '없음'(닫힌 세계: 그 범위의 임원은 전부 적재되어 있다),
    0명이면 '없음'이라 하지 않고 데이터가 없다고 적는다(이름 연결 실패·미적재일 수 있음). 이 구분을 LLM에 맡기지 않는다 (g10, 2026-11-26)."""
    have, none, total, who = _scope_parts(s)
    if total == 0:
        return [{"rule": "겸직 판정 보류: 대조 범위에 임원 데이터 없음", "subject": "겸직",
                 "value": f"판정 불가 — 기준일 {s.params.get('as_of')}에 대조할 {who} 데이터가 없음(회사 {len(s.params.get('corps') or [])}곳). '없음'이라 답하지 말 것"}]
    per = ", ".join(f"{r['corp_name']} {r['persons']}명" for r in have)
    return [{"rule": "겸직 없음 판정: 대조 범위의 임원 전원을 봄(그래프에 전부 적재됨)", "subject": "겸직",
             "value": f"없음 — 기준일 {s.params.get('as_of')} 회사 {len(have)}곳의 {who} {total}명 중 두 곳 이상에 있는 사람 0명 (회사별: {per})."
                      f"{_missing_note(none, who)}"}]


def _derive_tenures(s: StepResult) -> list[dict]:
    """같은 사람·회사·직위가 이어지는 보고서 행들을 기간 하나로. 끝이 열린 행이 있으면 '현재'."""
    spans: dict[tuple, dict] = {}
    for p in s.paths:
        person, corp = p["nodes"][0], p["nodes"][1]
        r = p["rels"][0]
        key = (person["key"], corp["key"], r.get("title"))
        sp = spans.setdefault(key, {"person": person["name"], "corp": corp["name"], "title": r.get("title"), "from": None, "to": None, "open": False})
        vf, vt = _s(r.get("valid_from")), _s(r.get("valid_to"))
        if vf and (sp["from"] is None or vf < sp["from"]):
            sp["from"] = vf
        if vt is None:
            sp["open"] = True
        elif sp["to"] is None or vt > sp["to"]:
            sp["to"] = vt
    out = []
    for sp in sorted(spans.values(), key=lambda x: (x["from"] or "", x["corp"])):
        end = "현재" if sp["open"] else sp["to"]
        out.append({"rule": "연속 보고서 합치기(보고서 기준일 단위)", "subject": sp["person"],
                    "value": f"{sp['corp']} {sp['title']} {sp['from']} ~ {end}"})
    return out


# ---------------------------------------------------------------- 근거 조각 (T8)

def evidence(driver, relations: list[dict], limit: int = EVIDENCE_LIMIT, database: str = "neo4j") -> list[Hit]:
    """관계 행에 나온 순서대로 근거 접수번호를 모아 그 보고서의 해당 섹션 조각을 찾는다. 순서가 곧 그래프 쪽 순위."""
    rcepts, sections = [], []
    for r in relations:
        if r["source_rcept_no"] and r["source_rcept_no"] not in rcepts:
            rcepts.append(r["source_rcept_no"])
        for sec in T.EVIDENCE_SECTIONS.get(r["source_table"], []):
            if sec not in sections:
                sections.append(sec)
    if not rcepts:
        return []
    recs = T.run(driver, "evidence_chunks",
                 {"rcepts": rcepts, "sections": sections or None, "limit": limit, "per_doc": EVIDENCE_PER_DOC}, database)
    order = {r: i for i, r in enumerate(rcepts)}
    recs.sort(key=lambda r: (order.get(r["rcept_no"], len(order)), r.get("seq") or 0))   # Cypher도 같은 순서로 주지만, 가짜 드라이버(테스트)를 위해 한 번 더
    recs = interleave_by_report(recs)
    n = len(recs)
    return [Hit(r["rcept_no"], r["file_name"], r["section_id"], r["path"], float(n - i), (r["text"] or "")[:120], r["text"] or "")
            for i, r in enumerate(recs)]


def interleave_by_report(recs: list[dict]) -> list[dict]:
    """보고서별로 번갈아 놓는다: A1 B1 C1 A2 B2 C2 … (보고서 순서는 처음 나온 순서, 보고서 안 순서는 그대로).

    왜: 그래프가 주는 값은 "어느 보고서를 봐야 하는가"다. 같은 보고서의 조각 3개가 1~3위를 다 차지하면
    RRF에서 두 번째 보고서가 7위쯤으로 밀려 상위 5개에서 빠진다 (2026-11-25 평가 g05·g06: 지주 보고서가 1~3위, 정답인 자회사 보고서가 4~5위).
    조정할 숫자가 없는 방법이라 질문 12개에 맞춘 가중치(과적합)를 피한다.
    """
    by_report: dict[str, list[dict]] = {}
    for r in recs:
        by_report.setdefault(r["rcept_no"], []).append(r)
    out: list[dict] = []
    queues = list(by_report.values())
    while queues:
        queues = [q for q in queues if q]
        for q in queues:
            out.append(q.pop(0))
    return out


def build_context(question: str, driver, con, today: date | None = None, lineage: list[dict] | None = None,
                  picker=None, database: str = "neo4j") -> GraphContext | None:
    plan = route(question, con, today, lineage, picker)
    if plan is None:
        return None
    steps = execute(driver, plan, database)
    rels = relation_rows(steps)
    ctx = GraphContext(question, plan, steps, rels[:MAX_RELATION_ROWS], derive(steps), truncated=max(0, len(rels) - MAX_RELATION_ROWS))
    ctx.evidence = evidence(driver, ctx.relations, database=database)
    return ctx


def to_text(ctx: GraphContext) -> str:
    """LLM에 넘길 관계 표. 유효 기간과 근거 접수번호를 사실 바로 옆에 둔다 (phase4-research 2.1·2.3).

    파생 값을 관계 행보다 **앞에** 둔다. LLM에 넘길 때 글자 상한(ask.GRAPH_MAX_CHARS)으로 뒤가 잘리는데, 계층 질문의 답
    (단계별 회사 수, 간접만 판정, 간접 지분율)은 파생 값에 있다. 2026-11-26 채점표에서 g05는 관계 50행이 상한을 다 써서
    파생 값이 통째로 잘렸고 LLM이 "찾지 못함"이라고 답했다.
    """
    lines = [f"[그래프 관계표] 기준일 {ctx.plan.as_of.isoformat()} · 규칙 '{ctx.plan.rule}'"]
    if ctx.derived:
        lines.append("[파생 값 — 규칙으로 계산, 공시에 그대로 적힌 값 아님]")
        lines += [f"{d['subject']}: {d['value']}  (규칙: {d['rule']})" for d in ctx.derived[:MAX_DERIVED_ROWS]]
        if len(ctx.derived) > MAX_DERIVED_ROWS:
            lines.append(f"(파생 값 {len(ctx.derived) - MAX_DERIVED_ROWS}개 더 있음 — 잘림. 합계는 위 '단계별 회사 수' 줄)")
    lines.append(f"[관계 {len(ctx.relations)}행" + (f", {ctx.truncated}개 더 있음 — 잘림" if ctx.truncated else "") + "]")
    lines.append("출발 | 관계 | 도착 | 값 | 유효시작 | 유효끝 | 근거접수번호")
    for r in ctx.relations:
        # 쉼표가 아니라 " | "로 나눈다: 값 칸에 "100.0%, 최대주주"·"1,000원"처럼 쉼표가 들어가 칸이 밀리기 때문
        lines.append(" | ".join(str(x) if x is not None else "" for x in
                              (r["src"], r["rel"], r["dst"], r["value"], r["valid_from"], r["valid_to"] or "(유효)", r["source_rcept_no"])))
    if not ctx.relations:
        # 0행의 뜻은 둘: 파생 값에 "없음" 판정이 있으면 정말 없음, 없으면 그래프가 답하지 못한 것(이름 연결 실패 등)
        lines.append("(관계 0행 — 위 파생 값에 '없음' 판정이 있으면 그것이 답. 없으면 그래프가 답하지 못한 것)")
    return "\n".join(lines)


# ---------------------------------------------------------------- 검색기 (eval·ask와 같은 모양)

class GraphRetriever:
    """search(question, k) -> list[Hit]. base(Phase 3 검색기)가 있으면 RRF로 합친다. 마지막 실행의 GraphContext는 self.last."""

    def __init__(self, driver, con, base=None, lineage: list[dict] | None = None, fetch: int = 20, picker=None,
                 today: date | None = None, database: str = "neo4j"):
        self.driver, self.con, self.base, self.lineage = driver, con, base, lineage
        self.fetch, self.picker, self.today, self.database = fetch, picker, today, database
        self.name = "graph-" + (base.name if base is not None else "only")
        self.last: GraphContext | None = None
        self.error: str | None = None

    def search(self, question: str, k: int) -> list[Hit]:
        from neo4j.exceptions import Neo4jError

        self.error = None
        try:
            self.last = build_context(question, self.driver, self.con, self.today, self.lineage, self.picker, self.database)
        except Neo4jError as e:
            # 템플릿 질의가 실패해도(예: 관계가 아직 안 들어감, 문법 오류) 답은 RAG로 낸다. 실패는 error로 남겨 응답·로그에서 보이게
            self.last, self.error = None, f"{type(e).__name__}: {getattr(e, 'message', None) or e}"
        graph_hits = self.last.evidence if self.last else []
        if self.base is None:
            return graph_hits[:k]
        rag_hits = self.base.search(question, self.fetch)
        if not graph_hits:
            return rag_hits[:k]
        fused = rrf_fuse([[_as_dict(h) for h in graph_hits], [_as_dict(h) for h in rag_hits]])
        return [Hit(r["rcept_no"], r["file_name"], r["section_id"], r["path"], float(sc), (r["text"] or "")[:120], r["text"] or "")
                for r, sc in fused[:k]]


def _as_dict(h: Hit) -> dict:
    return {"rcept_no": h.rcept_no, "file_name": h.file_name, "section_id": h.section_id, "path": h.path, "text": h.text}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="그래프 질의 (라우터 → 템플릿 → 관계 표). LLM 호출 없음")
    ap.add_argument("question")
    ap.add_argument("--k", type=int, default=5)
    args = ap.parse_args(argv)

    from src.graph.client import connect, settings, wait_until_ready
    from src.graph.load_relations import load_lineage
    from src.loaders.duckdb_store import connect as duck_connect

    s = settings()
    with connect(s) as driver:
        wait_until_ready(driver)
        with duck_connect(read_only=True) as con:
            ctx = build_context(args.question, driver, con, lineage=load_lineage(), database=s.database)
    if ctx is None:
        print("그래프 템플릿에 맞지 않는 질문입니다 → Phase 3 검색(src.rag.ask)으로 답합니다")
        return 0
    import json
    print(json.dumps(ctx.trace(), ensure_ascii=False, indent=2, default=str))
    print(to_text(ctx))
    print(f"\n근거 조각 {len(ctx.evidence)}개:")
    for h in ctx.evidence[: args.k]:
        print(f"  {h.rcept_no}  {h.path}\n      {h.snippet}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
