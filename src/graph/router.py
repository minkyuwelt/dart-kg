"""질문 → 그래프 템플릿 계획 (결정 0011 결정 4의 "라우터").

하는 일 두 가지
1) 어느 템플릿인가: 단어 패턴(규칙)으로 고른다. 규칙은 빠르고 공짜이며 결과가 늘 같다.
   규칙으로 못 고르면 picker(선택, LLM이 "템플릿 1~8 중 몇 번"만 고름)에게 묻고, 그래도 없으면 None → 그래프 없이 RAG만.
   (picker의 실제 LLM 구현은 다음 PR. 지금은 넣지 않으면 규칙만 쓴다)
2) 빈칸에 무엇을 넣나: 회사 이름 → corp_code는 DuckDB 사전(target_corp·공시에 나온 이름·corp 테이블·corp_lineage.csv)으로,
   날짜는 "2024년 말" → 2024-12-31처럼 규칙으로. 추측하지 않는다 — 회사를 못 찾으면 그래프를 쓰지 않는다.

계획(Plan)은 단계(Step)의 목록이다. 뒤 단계가 앞 단계 결과를 쓰는 경우(최대주주의 대표이사)는 `from_prev`로 적는다.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date

import duckdb

from src.graph.names import linked_names
from src.graph.templates import MAX_DEPTH

_STRIP_RE = re.compile(r"주식회사|\(주\)|㈜|\s+")
# corp 테이블(전체 회사)에서 이름을 찾아볼 후보. 수집 대상이 아닌 회사(합병으로 없어진 회사 등)를 위해서만 쓴다
_NAME_CANDIDATE_RE = re.compile(r"[가-힣A-Za-z0-9]+(?:은행|증권|보험|생명보험|손해보험|생명|카드|캐피탈|금융지주|금융투자|자산운용|저축은행|신탁|투자증권)")
TITLES = ("대표이사", "사외이사", "회장", "사장", "감사위원", "이사회 의장")


@dataclass(frozen=True)
class CorpMention:
    corp_code: str
    text: str      # 질문에 적힌 그대로
    current: str   # 지금 이름 (corp/target_corp). 질문의 이름과 다르면 옛 이름으로 물은 것


@dataclass
class Step:
    template: str
    params: dict
    from_prev: str | None = None   # 앞 단계 결과에서 빈칸을 채우는 방법 이름 (query.py의 FROM_PREV)


@dataclass
class Plan:
    rule: str                      # 어느 규칙이 골랐나 (실행 기록)
    steps: list[Step]
    as_of: date
    mentions: list[CorpMention] = field(default_factory=list)


# ---------------------------------------------------------------- 날짜

_DATE_PATTERNS = [
    (re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"), lambda y, m, d: date(int(y), int(m), int(d))),
    (re.compile(r"(\d{4})[.-](\d{1,2})[.-](\d{1,2})"), lambda y, m, d: date(int(y), int(m), int(d))),
    (re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*말?"), lambda y, m: date(int(y), int(m), calendar.monthrange(int(y), int(m))[1])),
    (re.compile(r"(\d{4})\s*년\s*(?:말|연말|기말)"), lambda y: date(int(y), 12, 31)),
]


def parse_as_of(question: str, today: date | None = None) -> tuple[date, str]:
    """(기준일, 어떻게 정했나). 날짜가 없거나 '현재·지금'이면 오늘. 'YYYY년'만 있으면 그해 말로 보지 않는다(뜻이 둘이라) → 오늘."""
    for pattern, make in _DATE_PATTERNS:
        m = pattern.search(question)
        if m:
            return make(*m.groups()), "질문의 날짜"
    return today or date.today(), "날짜 없음 → 오늘"


# ---------------------------------------------------------------- 회사 이름

def _bare(name: str) -> str:
    return _STRIP_RE.sub("", name or "")


def name_dictionary(con: duckdb.DuckDBPyConnection, lineage: list[dict] | None = None) -> dict[str, str]:
    """이름(공백·법인 표기 뗀 꼴) → corp_code. 같은 이름이 두 corp_code를 가리키면 그 이름은 뺀다 (추측하지 않음).
    출처: target_corp, src/graph/names.py의 이름 목록(공시 목록·출자 표·최대주주 표에서 corp_code로 연결된 이름),
    corp_lineage.csv의 옛 이름."""
    pairs = con.execute("SELECT corp_code, corp_name FROM target_corp").fetchall() + linked_names(con)
    pairs += [(r["former_corp_code"], r["former_name"]) for r in (lineage or []) if r.get("former_name")]
    seen: dict[str, set[str]] = {}
    for code, name in pairs:
        bare = _bare(name)
        if len(bare) >= 2:
            seen.setdefault(bare, set()).add(code)
    return {name: next(iter(codes)) for name, codes in seen.items() if len(codes) == 1}


def find_corps(con: duckdb.DuckDBPyConnection, question: str, lineage: list[dict] | None = None) -> list[CorpMention]:
    """질문에 나온 회사들 (나온 순서). 긴 이름부터 찾고 겹치는 자리는 다시 쓰지 않는다 ('KB국민은행' 안의 '국민은행')."""
    bare_q = _bare(question)
    names = name_dictionary(con, lineage)
    taken = [False] * len(bare_q)
    found: list[tuple[int, str, str]] = []
    for name in sorted(names, key=len, reverse=True):
        start = bare_q.find(name)
        while start >= 0:
            if not any(taken[start:start + len(name)]):
                for i in range(start, start + len(name)):
                    taken[i] = True
                found.append((start, name, names[name]))
            start = bare_q.find(name, start + 1)
    # 사전에 없는 이름은 corp 테이블에서 정확히 같은 이름이 하나일 때만 (합병으로 없어진 회사 등)
    for m in _NAME_CANDIDATE_RE.finditer(bare_q):
        if any(taken[m.start():m.end()]):
            continue
        rows = con.execute("SELECT corp_code FROM corp WHERE replace(corp_name, ' ', '') = ?", [m.group(0)]).fetchall()
        if len(rows) == 1:
            found.append((m.start(), m.group(0), rows[0][0]))
    current = dict(con.execute("SELECT corp_code, corp_name FROM corp").fetchall())
    current.update(dict(con.execute("SELECT corp_code, corp_name FROM target_corp").fetchall()))
    out, codes = [], set()
    for _, text, code in sorted(found):
        if code not in codes:
            codes.add(code)
            out.append(CorpMention(code, text, current.get(code, text)))
    return out


def holdings(con: duckdb.DuckDBPyConnection) -> list[str]:
    return [r[0] for r in con.execute("SELECT corp_code FROM target_corp WHERE role = 'holding' ORDER BY corp_code").fetchall()]


def group_of(con: duckdb.DuckDBPyConnection, holding: str) -> list[str]:
    """지주 + 그 직속 자회사 (target_corp 기준)."""
    rows = con.execute("SELECT corp_code FROM target_corp WHERE corp_code = ? OR parent_corp_code = ? ORDER BY corp_code", [holding, holding]).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------- 규칙

def _title(question: str) -> str | None:
    for t in TITLES:
        if t in question:
            return t
    return None


def _depth(question: str, default: int) -> int:
    m = re.search(r"(\d)\s*단계", question)
    if m:
        return max(1, min(MAX_DEPTH, int(m.group(1))))
    if "증손" in question:
        return 3
    if "손자" in question or "거쳐" in question or "간접" in question:
        return 2
    return default


def route(question: str, con: duckdb.DuckDBPyConnection, today: date | None = None, lineage: list[dict] | None = None,
          picker=None) -> Plan | None:
    """질문 → Plan. 그래프로 답할 질문이 아니거나 회사를 못 찾으면 None (RAG만 쓴다).

    규칙의 순서가 중요하다: 더 구체적인 꼴("최대주주의 최대주주")을 먼저 본다."""
    q = question
    as_of, _ = parse_as_of(q, today)
    mentions = find_corps(con, q, lineage)
    corp = mentions[0].corp_code if mentions else None
    title = _title(q)
    iso = as_of.isoformat()

    def plan(rule: str, *steps: Step) -> Plan:
        return Plan(rule, list(steps), as_of, mentions)

    # 통합: 겸직 (회사가 없어도 "4대 금융지주"면 지주 넷)
    if re.search(r"두\s*곳\s*이상|동시에|겸(직|하|임)|함께\s*맡", q):
        outside = "사외이사" in q
        if re.search(r"4대\s*금융지주|금융지주\s*중|지주\s*중", q) and not corp:
            return plan("겸직: 지주 넷", Step("shared_positions", {"as_of": iso, "outside_only": outside, "corps": holdings(con), "must": None}))
        if corp:
            scope = group_of(con, corp) if re.search(r"자회사|계열", q) else [m.corp_code for m in mentions]
            if len(scope) >= 2:
                return plan("겸직: 회사 묶음", Step("shared_positions", {"as_of": iso, "outside_only": outside, "corps": scope, "must": corp}))
        return None
    if not corp:
        return _pick(picker, q, as_of, mentions)

    # 추론: 최대주주의 최대주주 / 최대주주가 가진 회사 / 최대주주의 임원
    if re.search(r"최대주주의\s*최대주주", q):
        return plan("최대주주 체인", Step("holder_chain", {"corp": corp, "as_of": iso, "depth": 2}))
    if re.search(r"최대주주(가|이)\s*.*(가진|보유|출자)", q):
        pct = re.search(r"(\d+(?:\.\d+)?)\s*%", q)
        return plan("최대주주 → 그 회사의 출자",
                    Step("holder_chain", {"corp": corp, "as_of": iso, "depth": 1}),
                    Step("descendants", {"as_of": iso, "depth": 1, "min_pct": float(pct.group(1)) if pct else None}, from_prev="top_corp_holders"))
    if re.search(r"최대주주의\s*(대표이사|사외이사|임원|회장|사장|이사)", q):
        return plan("최대주주 → 그 회사의 임원",
                    Step("holder_chain", {"corp": corp, "as_of": iso, "depth": 1}),
                    Step("facts_at", {"as_of": iso, "kind": "positions", "title": title}, from_prev="top_corp_holders"))

    # 의미: 이름이 바뀐 회사 (질문의 이름 ≠ 지금 이름, 또는 그런 뜻의 말)
    said, now = _bare(mentions[0].text), _bare(mentions[0].current)
    renamed = said not in now and now not in said   # 'KB금융' ↔ 'KB금융지주'는 줄임말이지 옛 이름이 아니다
    if re.search(r"지금\s*어떤\s*회사|이어지|이어\s*보|옛\s*이름|이름이\s*바뀐|사명", q) or renamed:
        steps = [Step("corp_lineage", {"corp": corp, "name": mentions[0].text})]
        if "최대주주" in q:
            steps.append(Step("facts_at", {"corp": corp, "as_of": iso, "kind": "holders", "title": None}))
        elif title or "임원" in q:
            steps.append(Step("facts_at", {"corp": corp, "as_of": iso, "kind": "positions", "title": title}))
        return plan("이름 이력", *steps)

    # 계층: 아래로 (자회사·손자회사·간접)
    if re.search(r"손자|아래|거쳐|간접|자회사|출자\s*대상", q):
        return plan("출자 아래로", Step("descendants", {"corp": corp, "as_of": iso, "depth": _depth(q, 2), "min_pct": None}))

    # 통합: 임원 이력
    if re.search(r"거쳤|거쳐\s*왔|이력|경력|어느\s*계열사", q):
        return plan("임원 이력", Step("person_history", {"corp": corp, "as_of": iso, "title": title, "person": None}))

    # 추론: 두 회사 사이 경로
    if len(mentions) >= 2 and re.search(r"경로|어떻게\s*(이어|연결)", q):
        return plan("두 회사 경로", Step("ownership_path", {"corp": corp, "other": mentions[1].corp_code, "as_of": iso}))

    return _pick(picker, q, as_of, mentions)


def _pick(picker, question: str, as_of: date, mentions: list[CorpMention]) -> Plan | None:
    """규칙으로 못 고른 질문. picker가 있으면 Plan을 받고(고르기만), 없으면 None."""
    if picker is None:
        return None
    return picker(question, as_of, mentions)
