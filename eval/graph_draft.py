"""그래프 질문 12개(결정 0011 결정 1)의 정답 **초안**을 DuckDB 관계 테이블에서 SQL로 만들고, 그래프 답과 대조한다.

    python -m eval.graph_draft                 # 초안 12개 → eval/questions.draft.jsonl (g01~g12만 바꿔 씀)
    python -m eval.graph_draft --compare       # + 같은 질문을 그래프(src.graph.query)로도 풀어 SQL 답과 대조 (Neo4j 필요)
    python -m eval.review --type 다중경로        # 초안 답을 보며 뷰어로 확인 → a(초안 그대로) 또는 고쳐 친 답으로 ready

왜 SQL인가
- 정답은 Phase 2에서 검증한 관계 테이블(최대주주·임원·출자, 이중 시간축)에 이미 있다. 그래프를 거치지 않고 표를 직접 읽으면
  "그래프가 맞게 옮겨졌나"와 상관없는 기준 답이 된다. 그래서 --compare가 그래프 적재·템플릿의 버그를 잡는 시험이 된다.
- 그래도 초안이다 (status draft). 사람이 근거 접수번호로 뷰어를 열어 확인한 것만 ready가 된다 (CLAUDE.md 규칙).

한계
- "의미" 갈래(사명 변경·합병)는 연혁 글에 있다. SQL은 공시 목록에 나온 이름들과 corp_lineage.csv까지만 보여 준다.
  합병 관계는 뷰어의 연혁으로 확인해 config/corp_lineage.csv에 적어야 그래프가 답한다.
- 생년월은 어디에도 내보내지 않는다 (사람은 이름만).
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import duckdb

from eval.draft import DEFAULT_OUT
from eval.questions import DEFAULT_QUESTIONS, Question, load_questions
from eval.review import line_id, read_lines, write_lines
from src.graph.load_relations import load_lineage
from src.graph.names import NAME_CHARS
from src.graph.router import _bare, find_corps, group_of, holdings, parse_as_of
from src.graph.templates import title_matches
from src.loaders.as_of import AS_OF_FILTER
from src.loaders.duckdb_store import connect


@dataclass(frozen=True)
class Spec:
    id: str
    graph_kind: str
    question: str
    solver: str
    section: str | None   # 근거 섹션 이름의 일부 (review가 원문을 띄울 때)
    check: str            # 사람이 뷰어에서 확인할 것


SPECS = [
    Spec("g01", "추론", "2025년 12월 31일 기준 우리은행의 최대주주의 최대주주는 누구인가?", "holder_chain", "최대주주", "두 회사 보고서의 최대주주 표에서 기말 지분율 최대 행"),
    Spec("g02", "추론", "2025년 12월 31일 기준 신한라이프생명보험의 최대주주가 지분 100%를 가진 회사는 몇 곳인가?", "holder_then_owned", "출자", "최대주주(회사)의 타법인출자 표에서 기말 지분율 100%인 행 수"),
    Spec("g03", "추론", "2023년 3월 31일 기준 KB국민카드 최대주주의 대표이사는 누구인가?", "holder_then_title", "임원", "최대주주 회사의 그 시점 보고서 임원 표에서 대표이사"),
    Spec("g04", "계층", "2025년 12월 31일 기준 KB금융지주 아래 손자회사까지(2단계) 포함하면 회사는 모두 몇 곳인가?", "descendants_count", "출자", "지주 출자 표 + 자회사들의 출자 표. 같은 회사가 두 경로로 나오면 한 번만"),
    Spec("g05", "계층", "2025년 12월 31일 기준 신한지주가 자회사를 거쳐서만 지분을 가진 회사는 어디이고, 각각 어느 자회사를 거치나?", "indirect_only", "출자", "자회사 출자 표에만 있고 지주 출자 표에는 없는 회사"),
    Spec("g06", "계층", "2025년 12월 31일 기준 우리금융지주 아래 2단계까지의 출자 대상 중 국내 공시 대상이 아닌 곳(해외 법인·펀드 등)은 몇 곳인가?", "unresolved_count", "출자", "corp_code가 없는 출자 대상. 두 자회사가 같은 펀드에 출자하면 그래프는 둘로 센다(0011 ②)"),
    Spec("g07", "의미", "한국포스증권은 지금 어떤 회사이고, 그 공시는 어느 회사 이름으로 이어 보나?", "lineage", "연혁", "연혁에서 합병·사명 변경 날짜. corp_code가 바뀌었으면 corp_lineage.csv에 근거와 함께"),
    Spec("g08", "의미", "신한금융투자의 2021년 12월 31일 기준 최대주주는 누구였나?", "lineage_holders", "최대주주", "옛 이름 시절 보고서의 최대주주 표"),
    Spec("g09", "의미", "KB생명보험의 공시는 지금 어느 회사로 이어지나?", "lineage", "연혁", "KB라이프생명보험 연혁의 합병 기재. 수집 대상이 아니므로 corp_lineage.csv가 있어야 그래프가 답함"),
    Spec("g10", "통합", "2025년 12월 31일 기준 4대 금융지주 중 두 곳 이상에서 동시에 사외이사인 사람이 있는가?", "shared_outside_holdings", "임원", "네 지주의 사외이사 명단. 없으면 '없음'이 정답"),
    Spec("g11", "통합", "2025년 12월 31일 기준 KB금융지주 임원 중 KB 계열 자회사 임원을 함께 맡은 사람은 누구인가?", "shared_in_group", "임원", "지주 임원 표와 자회사 임원 표에 같은 사람(이름·생년월·성별)"),
    Spec("g12", "통합", "신한은행의 현재 대표이사는 그동안 어느 계열사에서 어떤 직위를 거쳤나?", "person_history", "임원", "그래프 이력은 수집 기간 안만. 그 이전은 임원 표의 '주요 경력' 글"),
]


@dataclass
class Draft:
    answer: str
    evidence: list[str] = field(default_factory=list)   # 근거 접수번호 (나온 순서)
    names: set[str] = field(default_factory=set)        # 답에 나오는 회사·사람 이름 (그래프 대조용)
    count: int | None = None                            # 몇 곳인가 질문의 수 (그래프 대조용)
    note: str = ""

    def cite(self, *rcepts):
        for r in rcepts:
            if r and r not in self.evidence:
                self.evidence.append(r)


def _rows(con, sql: str, params: list) -> list[dict]:
    cur = con.execute(sql, params)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def holders_at(con, corp: str, as_of: date) -> list[dict]:
    return _rows(con, f"""SELECT nm, holder_corp_code, trmend_posesn_stock_qota_rt AS pct, relate, source_rcept_no
                          FROM major_shareholder WHERE corp_code = ? AND {AS_OF_FILTER}
                          ORDER BY pct DESC NULLS LAST, row_seq""", [corp, as_of, as_of])


def top_holder(con, corp: str, as_of: date, corp_only: bool = False) -> dict | None:
    rows = [r for r in holders_at(con, corp, as_of) if r["holder_corp_code"] or not corp_only]
    return rows[0] if rows else None


def investments_at(con, investor: str, as_of: date) -> list[dict]:
    return _rows(con, f"""SELECT investee_name, investee_corp_code, trmend_blce_qota_rt AS pct, rcept_no
                          FROM investment WHERE investor_corp_code = ? AND {AS_OF_FILTER}
                            AND regexp_matches(investee_name, '{NAME_CHARS}')  -- '-' 같은 빈 이름 행은 그래프에도 없다
                          ORDER BY row_seq""", [investor, as_of, as_of])


def execs_at(con, corp: str, as_of: date) -> list[dict]:
    # 직위 칸의 줄바꿈은 공백 하나로 (그래프 적재와 같게)
    return _rows(con, f"""SELECT nm, person_id, regexp_replace(ofcps, '\\s+', ' ', 'g') AS ofcps,
                                 regexp_replace(rgist_exctv_at, '\\s+', ' ', 'g') AS rgist_exctv_at, source_rcept_no
                          FROM executive WHERE corp_code = ? AND {AS_OF_FILTER} ORDER BY row_seq""", [corp, as_of, as_of])


def corp_name(con, code: str) -> str:
    row = con.execute("SELECT corp_name FROM target_corp WHERE corp_code = ? UNION ALL SELECT corp_name FROM corp WHERE corp_code = ? LIMIT 1",
                      [code, code]).fetchone()
    return row[0] if row else code


def _pct(v) -> str:
    return f"{v}%" if v is not None else "지분율 없음"


def walk_down(con, root: str, as_of: date, depth: int = 2) -> dict[str, dict]:
    """출자를 따라 아래로. 그래프와 같은 키(corp_code, 없으면 name:{출자한 회사}:{이름})로 끝 회사별 최소 단계·경유."""
    best: dict[str, dict] = {}
    frontier = [(root, 0, [])]
    while frontier:
        code, d, via = frontier.pop(0)
        if d >= depth:
            continue
        for r in investments_at(con, code, as_of):
            key = r["investee_corp_code"] or f"name:{code}:{r['investee_name']}"
            hops = d + 1
            if key not in best or hops < best[key]["hops"]:
                best[key] = {"name": r["investee_name"], "hops": hops, "via": via, "has_code": bool(r["investee_corp_code"]), "rcept_no": r["rcept_no"]}
            if r["investee_corp_code"] and key != root:
                frontier.append((r["investee_corp_code"], hops, via + [r["investee_name"]]))
    best.pop(root, None)
    return best


# ---------------------------------------------------------------- 질문별 풀이

def solve(con, spec: Spec, lineage: list[dict], today: date | None = None) -> Draft:
    as_of, _ = parse_as_of(spec.question, today)
    mentions = find_corps(con, spec.question, lineage)
    corp = mentions[0].corp_code if mentions else None
    fn = SOLVERS[spec.solver]
    if corp is None and spec.solver not in ("shared_outside_holdings",):
        return Draft("(회사를 찾지 못함 — 질문의 회사 이름이 DuckDB 사전에 없음)", note="회사 이름 확인 필요")
    return fn(con, corp, as_of, mentions, lineage, spec.question)


def _holder_chain(con, corp, as_of, mentions, lineage, question: str = "") -> Draft:
    d = Draft("")
    h1 = top_holder(con, corp, as_of)
    if not h1:
        return Draft("(그 시점 최대주주 행 없음)")
    d.cite(h1["source_rcept_no"])
    d.names |= {h1["nm"]}
    parts = [f"{corp_name(con, corp)}의 최대주주: {h1['nm']} ({_pct(h1['pct'])})"]
    if h1["holder_corp_code"]:
        h2 = top_holder(con, h1["holder_corp_code"], as_of)
        if h2:
            d.cite(h2["source_rcept_no"])
            d.names |= {h2["nm"]}
            parts.append(f"{h1['nm']}의 최대주주: {h2['nm']} ({_pct(h2['pct'])}, {h2['relate'] or ''})")
        else:
            parts.append(f"{h1['nm']}의 최대주주 행 없음")
    else:
        parts.append("최대주주가 회사(corp_code)가 아니라 더 따라갈 수 없음")
    d.answer = " → ".join(parts)
    return d


def _holder_then_owned(con, corp, as_of, mentions, lineage, question: str = "") -> Draft:
    h1 = top_holder(con, corp, as_of, corp_only=True)
    if not h1:
        return Draft("(회사 최대주주 없음)")
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", question)
    pct = float(m.group(1)) if m else 100.0
    owned = [r for r in investments_at(con, h1["holder_corp_code"], as_of) if (r["pct"] or 0) >= pct]
    d = Draft(f"최대주주 {h1['nm']}가 {pct:g}% 이상 가진 회사 {len(owned)}곳: " + ", ".join(r["investee_name"] for r in owned), count=len(owned))
    d.cite(h1["source_rcept_no"], *(r["rcept_no"] for r in owned[:1]))
    d.names |= {h1["nm"], *(r["investee_name"] for r in owned)}
    return d


def _holder_then_title(con, corp, as_of, mentions, lineage, question: str = "") -> Draft:
    h1 = top_holder(con, corp, as_of, corp_only=True)
    if not h1:
        return Draft("(회사 최대주주 없음)")
    ceos = [e for e in execs_at(con, h1["holder_corp_code"], as_of) if title_matches(e["ofcps"], "대표이사")]
    d = Draft(f"최대주주 {h1['nm']}의 대표이사: " + (", ".join(f"{e['nm']}({e['ofcps']})" for e in ceos) or "그 시점 임원 표에 대표이사 없음"))
    d.cite(h1["source_rcept_no"], *(e["source_rcept_no"] for e in ceos[:1]))
    d.names |= {h1["nm"], *(e["nm"] for e in ceos)}
    return d


def _descendants_count(con, corp, as_of, mentions, lineage, question: str = "") -> Draft:
    best = walk_down(con, corp, as_of, 2)
    by_hop = {h: sum(1 for b in best.values() if b["hops"] == h) for h in (1, 2)}
    d = Draft(f"1단계 {by_hop[1]}곳, 2단계 {by_hop[2]}곳, 합계 {len(best)}곳 (corp_code 없는 대상 포함)", count=len(best))
    d.cite(*(b["rcept_no"] for b in best.values()))
    return d


def _indirect_only(con, corp, as_of, mentions, lineage, question: str = "") -> Draft:
    best = walk_down(con, corp, as_of, 2)
    only = {k: b for k, b in best.items() if b["hops"] == 2}
    d = Draft(f"간접만 {len(only)}곳: " + "; ".join(f"{b['name']} ({' → '.join(b['via'])} 경유)" for b in only.values()), count=len(only))
    d.cite(*(b["rcept_no"] for b in only.values()))
    d.names |= {b["name"] for b in only.values()}
    return d


def _unresolved_count(con, corp, as_of, mentions, lineage, question: str = "") -> Draft:
    best = walk_down(con, corp, as_of, 2)
    no_code = [b for b in best.values() if not b["has_code"]]
    d = Draft(f"corp_code 없는 출자 대상 {len(no_code)}곳 (출자한 회사별로 따로 셈): " + ", ".join(sorted(b["name"] for b in no_code)[:30]),
              count=len(no_code))
    d.cite(*(b["rcept_no"] for b in no_code))
    return d


def _lineage(con, corp, as_of, mentions, lineage, question: str = "") -> Draft:
    names = [r[0] for r in con.execute(
        """SELECT corp_name FROM disclosure WHERE corp_code = ? AND corp_name IS NOT NULL
           GROUP BY corp_name ORDER BY min(rcept_dt)""", [corp]).fetchall()]
    links = [r for r in lineage if corp in (r["current_corp_code"], r["former_corp_code"])]
    parts = [f"질문의 이름 '{mentions[0].text}' → corp_code {corp} (지금 이름 {mentions[0].current})"]
    if names:
        parts.append("공시 목록의 이름 순서: " + " → ".join(names))
    for r in links:
        parts.append(f"corp_lineage.csv: {r['former_name']}({r['former_corp_code']}) → {corp_name(con, r['current_corp_code'])} {r['on']} {r['reason']}")
    d = Draft("; ".join(parts), note="합병·사명 변경 날짜는 연혁에서 확인")
    latest = con.execute("""SELECT rcept_no FROM disclosure WHERE corp_code = ? AND report_nm LIKE '%사업보고서%'
                            ORDER BY rcept_dt DESC LIMIT 1""", [corp]).fetchone()
    for r in links:
        d.cite(r["evidence_rcept_no"])
    if latest:
        d.cite(latest[0])
    if not names and not links:
        d.note = "공시 목록에도 corp_lineage.csv에도 없음 → 뷰어 연혁으로 확인해 corp_lineage.csv에 적어야 그래프가 답한다"
    d.names |= {mentions[0].current}
    return d


def _lineage_holders(con, corp, as_of, mentions, lineage, question: str = "") -> Draft:
    d = _lineage(con, corp, as_of, mentions, lineage, question)
    rows = holders_at(con, corp, as_of)
    if rows:
        d.answer = f"{as_of.isoformat()} 최대주주: {rows[0]['nm']} ({_pct(rows[0]['pct'])}). " + d.answer
        d.evidence.insert(0, rows[0]["source_rcept_no"])
        d.names |= {rows[0]["nm"]}
    else:
        d.answer = "(그 시점 최대주주 행 없음 — 백필 범위 밖일 수 있음). " + d.answer
    return d


def _shared(con, corps: list[str], as_of: date, outside_only: bool, must: str | None) -> Draft:
    people: dict[str, dict] = {}
    for c in corps:
        for e in execs_at(con, c, as_of):
            if not e["person_id"] or (outside_only and e["rgist_exctv_at"] != "사외이사"):
                continue
            p = people.setdefault(e["person_id"], {"nm": e["nm"], "roles": [], "rcepts": []})
            p["roles"].append(f"{corp_name(con, c)} {e['ofcps']}")
            p["rcepts"].append(e["source_rcept_no"])
            p.setdefault("corps", set()).add(c)
    hit = [p for p in people.values() if len(p["corps"]) >= 2 and (must is None or must in p["corps"])]
    if not hit:
        d = Draft("없음 (그 시점 두 곳 이상에서 임원인 사람 없음)", count=0)
        for c in corps:
            rows = execs_at(con, c, as_of)
            if rows:
                d.cite(rows[0]["source_rcept_no"])
        return d
    d = Draft(f"{len(hit)}명: " + "; ".join(f"{p['nm']} ({', '.join(p['roles'])})" for p in hit), count=len(hit))
    for p in hit:
        d.cite(*p["rcepts"])
        d.names.add(p["nm"])
    return d


def _shared_outside_holdings(con, corp, as_of, mentions, lineage, question: str = "") -> Draft:
    return _shared(con, holdings(con), as_of, True, None)


def _shared_in_group(con, corp, as_of, mentions, lineage, question: str = "") -> Draft:
    return _shared(con, group_of(con, corp), as_of, False, corp)


def _person_history(con, corp, as_of, mentions, lineage, question: str = "") -> Draft:
    ceos = [e for e in execs_at(con, corp, as_of) if title_matches(e["ofcps"], "대표이사") and e["person_id"]]
    if not ceos:
        return Draft("(그 시점 대표이사 행 없음)")
    pid = ceos[0]["person_id"]
    rows = _rows(con, """SELECT e.corp_code, e.ofcps, min(e.valid_from) AS start, max(e.valid_to) AS end_, bool_or(e.valid_to IS NULL) AS open_,
                                any_value(e.source_rcept_no) AS rcept
                         FROM executive e WHERE e.person_id = ? AND e.superseded_by IS NULL
                         GROUP BY e.corp_code, e.ofcps ORDER BY start""", [pid])
    hist = "; ".join(f"{corp_name(con, r['corp_code'])} {r['ofcps']} {r['start']} ~ {'현재' if r['open_'] else r['end_']}" for r in rows)
    d = Draft(f"{ceos[0]['nm']}: {hist}", note="수집 기간 밖 경력은 임원 표의 주요 경력 글로 확인")
    d.cite(ceos[0]["source_rcept_no"], *(r["rcept"] for r in rows))
    d.names |= {ceos[0]["nm"], *(corp_name(con, r["corp_code"]) for r in rows)}
    return d


SOLVERS = {
    "holder_chain": _holder_chain, "holder_then_owned": _holder_then_owned, "holder_then_title": _holder_then_title,
    "descendants_count": _descendants_count, "indirect_only": _indirect_only, "unresolved_count": _unresolved_count,
    "lineage": _lineage, "lineage_holders": _lineage_holders, "shared_outside_holdings": _shared_outside_holdings,
    "shared_in_group": _shared_in_group, "person_history": _person_history,
}


# ---------------------------------------------------------------- 초안 파일

def to_question(spec: Spec, d: Draft) -> Question:
    note = f"SQL 초안(미확인). 확인할 것: {spec.check}" + (f" / {d.note}" if d.note else "")
    return Question(id=spec.id, question=spec.question, type="다중경로", phase=4, status="draft", answer=d.answer,
                    evidence_rcept_no=d.evidence[:5], evidence_section=spec.section, source="graph-draft-sql", note=note,
                    graph_kind=spec.graph_kind)


def write_drafts(questions: list[Question], draft_path: Path, questions_path: Path) -> tuple[int, int]:
    """(쓴 수, 이미 ready라 건너뛴 수). 초안 파일에서 같은 id 줄만 바꾸고 다른 후보는 그대로 둔다."""
    ready = {q.id for q in load_questions(questions_path) if q.is_ready} if questions_path.exists() else set()
    todo = [q for q in questions if q.id not in ready]
    ids = {q.id for q in todo}
    lines = [l for l in read_lines(draft_path) if line_id(l) not in ids]
    lines += [json.dumps(q.to_dict(), ensure_ascii=False) for q in todo]
    write_lines(draft_path, lines)
    return len(todo), len(questions) - len(todo)


# ---------------------------------------------------------------- 그래프와 대조

_TOTAL_RE = re.compile(r"합계 (\d+)곳")
_NO_CODE_RE = re.compile(r"corp_code 없음 (\d+)곳")


def _same(name: str) -> str:
    """이름 대조용: 띄어쓰기·법인 표기·전각을 뗀 꼴 ('우리금융지주 우리사주조합' = '우리금융지주우리사주조합')."""
    return _bare(unicodedata.normalize("NFKC", name or ""))


def graph_view(ctx) -> tuple[set[str], int | None, int | None]:
    """GraphContext → (나온 이름들, 합계 곳 수, corp_code 없는 곳 수).
    이름은 관계 표뿐 아니라 경로 위 노드의 names(공시에 나온 다른 이름, '신한금융지주회사' = 신한지주)까지 본다."""
    names = set()
    for r in ctx.relations:
        names |= {r["src"], r["dst"]}
    for st in ctx.steps:
        for p in st.paths:
            for n in p.get("nodes", []):
                names |= {n.get("name") or "", *(n.get("names") or [])}
    total = no_code = None
    for d in ctx.derived:
        names.add(str(d["subject"]))
        for part in d["value"].split(";"):
            names |= {w.strip() for w in re.split(r"[:,()→]", part) if w.strip()}
        if (m := _TOTAL_RE.search(d["value"])):
            total = int(m.group(1))
        if (m := _NO_CODE_RE.search(d["value"])):
            no_code = int(m.group(1))
    return {_same(n) for n in names if n}, total, no_code


def compare(spec: Spec, d: Draft, ctx) -> dict:
    """SQL 초안과 그래프 결과가 맞나. 이름은 'SQL 답의 이름이 그래프에 다 나오나', 수는 같은가."""
    if ctx is None:
        return {"id": spec.id, "ok": False, "why": "라우터가 그래프 템플릿을 고르지 않음"}
    names, total, no_code = graph_view(ctx)
    if not d.names and not d.count and not ctx.relations:
        # 둘 다 아무것도 못 찾음: "일치"로 세지 않는다 (둘 다 틀렸을 수 있다 — 예: 은행장을 대표이사로 못 찾던 g12)
        return {"id": spec.id, "ok": None, "templates": [s.template for s in ctx.steps], "graph_relations": 0,
                "why": "SQL·그래프 둘 다 결과 없음 — 사람이 확인"}
    missing = sorted(n for n in d.names if _same(n) not in names)
    out = {"id": spec.id, "templates": [s.template for s in ctx.steps], "graph_relations": len(ctx.relations), "missing_names": missing}
    if d.count is not None:
        graph_count = no_code if spec.solver == "unresolved_count" else total
        if spec.solver in ("shared_outside_holdings", "shared_in_group"):
            graph_count = len({r["src_key"] for r in ctx.relations if r["rel"] == "HOLDS_POSITION"})
        elif spec.solver in ("indirect_only", "holder_then_owned"):
            graph_count = None  # 그래프 표에서 바로 세기 어려움 → 이름 대조로만
        out["sql_count"], out["graph_count"] = d.count, graph_count
        if graph_count is not None and graph_count != d.count:
            out["ok"], out["why"] = False, f"수가 다름: SQL {d.count} vs 그래프 {graph_count}"
            return out
    out["ok"] = not missing
    out["why"] = "" if not missing else "SQL 답의 이름이 그래프 결과에 없음"
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="그래프 질문 12개의 정답 초안(SQL) 만들기 + 그래프 답과 대조")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    ap.add_argument("--compare", action="store_true", help="그래프로도 풀어 대조 (Neo4j에 조각·관계가 들어가 있어야 함)")
    ap.add_argument("--id", nargs="*", help="질문 id만 (예: g01 g04)")
    args = ap.parse_args(argv)
    specs = [s for s in SPECS if not args.id or s.id in args.id]
    lineage = load_lineage()
    with connect(read_only=True) as con:
        drafts = [(s, solve(con, s, lineage)) for s in specs]
        for s, d in drafts:
            print(f"{s.id} [{s.graph_kind}] {s.question}\n    초안: {d.answer}\n    근거: {', '.join(d.evidence[:5]) or '(없음)'}"
                  + (f"\n    메모: {d.note}" if d.note else ""))
        if args.compare:
            from src.graph.client import connect as graph_connect
            from src.graph.client import settings, wait_until_ready
            from src.graph.query import build_context

            st = settings()
            results = []
            with graph_connect(st) as driver:
                wait_until_ready(driver)
                for s, d in drafts:
                    results.append(compare(s, d, build_context(s.question, driver, con, lineage=lineage, database=st.database)))
            print("\n그래프 대조:")
            for r in results:
                mark = {True: "일치", False: "다름", None: "둘다없음"}[r["ok"]]
                extra = f"  ({r['why']})" if r.get("why") else ""
                if r.get("missing_names"):
                    extra += f"  그래프에 없는 이름: {', '.join(r['missing_names'][:5])}"
                print(f"  {r['id']} {mark}  템플릿 {r.get('templates')}  관계 {r.get('graph_relations')}{extra}")
            print(f"일치 {sum(r['ok'] is True for r in results)}/{len(results)}, 둘 다 결과 없음 {sum(r['ok'] is None for r in results)}")
    wrote, skipped = write_drafts([to_question(s, d) for s, d in drafts], args.out, args.questions)
    print(f"\n초안 {wrote}개를 {args.out.name}에 썼습니다 (이미 ready {skipped}개는 건너뜀). 다음: python -m eval.review --type 다중경로")
    return 0


if __name__ == "__main__":
    sys.exit(main())
