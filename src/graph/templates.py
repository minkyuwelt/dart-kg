"""그래프 질의 템플릿 8개 (결정 0011 결정 4). LLM이 Cypher를 쓰지 않는다 — 여기 적힌 질의의 빈칸($이름)만 채워서 돌린다.

왜 고정 템플릿인가 (0011, phase4-research 1.4): LLM이 Cypher를 직접 쓰면 최고 모델도 실행 정확도 60~77%이고,
"돌아가지만 틀린" 질의가 그럴듯한 오답을 낸다. 템플릿은 사람이 검증한 질의라 질의 자체는 틀리지 않는다.
대신 템플릿 밖의 질문에는 그래프가 답하지 않는다 (라우터가 RAG로 넘김).

모든 템플릿(T8 제외)은 같은 모양으로 돌려준다: 경로마다
    nodes = [{key, name, label, has_corp_code, names}]   경로 위 노드 (Person은 key = pid)
    rels  = [{type, start_key, ...관계 속성}]           경로 위 관계 (row_key·시간 4속성 포함). start_key = 관계가 시작하는 노드의 key
                                                         (경로 순서와 관계 방향이 다를 수 있어서: 위로 따라가기·방향 없는 최단 경로)
그래서 직렬화(관계 표 만들기)·근거 찾기(T8)가 템플릿을 몰라도 된다.

시점 필터: 모든 관계 조건에 at("r")를 쓴다. src/loaders/as_of.py의 AS_OF_FILTER와 같은 뜻이다
(그 날짜에 유효했고 정정으로 대체되지 않은 행만).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_DEPTH = 3       # 경로 질문의 최대 깊이 (0011 결정 4, phase4-research 3절)
ROW_LIMIT = 200     # 경로 수 상한. 직렬화에서 관계 행 50개로 다시 자른다
# 출자를 따라 내려가는 T2만 더 크게: 지주 아래 2단계가 1,500곳을 넘는다 (KB금융, 2025-12-31).
# 200에서 자르면 "몇 곳인가"의 답이 200이 된다. 회사 수는 전체로 세고, LLM에 넘기는 표만 query.py에서 자른다
DESCENDANTS_LIMIT = 10000

# 대표이사는 어디에 적히나 (노트북 확인 2026-11-25)
# - 직위 칸: '대표이사 회장', 은행은 '은행장' (은행법상 은행장이 대표이사. 신한은행 정상혁 '은행장' / 사내이사)
# - 담당업무 칸: 금융지주는 직위 '회장', 담당업무 '대표이사'로 적는다 (우리금융지주 2025 사업보고서 임종룡)
# 담당업무에서는 낱말로만 찾는다: 사외이사 담당업무의 '자회사대표이사후보 추천위원회'는 대표이사가 아니다
TITLE_ALIASES = {"대표이사": ("대표이사", "은행장")}
_CEO_DUTY_RE = re.compile(r"(?<![가-힣])대표이사(?![가-힣])")


def is_ceo(title: str | None, duty: str | None = None) -> bool:
    """임원 행 하나가 대표이사인가. 그래프 적재(HOLDS_POSITION.ceo)와 SQL 초안이 같은 함수를 쓴다."""
    if any(w in (title or "") for w in TITLE_ALIASES["대표이사"]):
        return True
    return bool(duty) and bool(_CEO_DUTY_RE.search(" ".join(duty.split())))


def title_matches(title: str | None, want: str | None, duty: str | None = None) -> bool:
    """파이썬 쪽 같은 규칙 (eval/graph_draft.py의 SQL 초안이 쓴다)."""
    if want is None:
        return True
    if want == "대표이사":
        return is_ceo(title, duty)
    return want in (title or "")


def title_ok(r: str) -> str:
    """Cypher 조각: 관계 r이 $title에 해당하는가. 직위(r.title = ofcps)뿐 아니라 등기구분(r.registered = rgist_exctv_at: 사내이사·사외이사·기타비상무이사)도 본다.
    대표이사는 적재 때 is_ceo로 계산한 r.ceo로 본다.

    등기구분을 함께 보는 이유 (2026-11-27, p2-q2): 하나은행 사외이사 6명의 직위는 "이사"이고 "사외이사"는 등기구분에만 있어, 직위 글자만 보면
    `title='사외이사'`로 0행이 나오고 규칙 7(기준일 질문은 관계표 우선)이 LLM을 "찾지 못함"으로 끌었다. 회사마다 직위 칸의 관행이 다르다."""
    return (f"($title IS NULL OR {r}.title CONTAINS $title OR coalesce({r}.registered, '') CONTAINS $title "
            f"OR ($title = '대표이사' AND coalesce({r}.ceo, false)))")


def at(r: str) -> str:
    """관계 변수 r이 $as_of 날짜에 유효했는지. 모든 템플릿이 이 한 조각을 쓴다."""
    return (f"{r}.valid_from <= date($as_of) AND ({r}.valid_to IS NULL OR {r}.valid_to > date($as_of)) "
            f"AND {r}.superseded_by IS NULL")


_NODE = "{key: coalesce(n.key, n.pid), name: n.name, label: labels(n)[0], has_corp_code: n.has_corp_code, names: n.names}"


def return_path(limit: int = ROW_LIMIT) -> str:
    return f"""
RETURN [n IN nodes(p) | {_NODE}] AS nodes,
       [r IN relationships(p) | r{{.*, type: type(r), start_key: coalesce(startNode(r).key, startNode(r).pid)}}] AS rels
LIMIT {limit}
"""


RETURN_PATH = return_path()


@dataclass(frozen=True)
class Template:
    name: str
    title: str           # 사람이 읽는 설명 (라우터 LLM 고르기·실행 기록에 쓴다)
    cypher: str
    params: tuple[str, ...]                     # 반드시 채워야 하는 빈칸
    optional: tuple[str, ...] = field(default=())  # 비워도 되는 빈칸 (None을 넣는다)

    def bind(self, values: dict) -> dict:
        missing = [p for p in self.params if values.get(p) is None]
        if missing:
            raise ValueError(f"{self.name}: 빈칸이 비었습니다: {', '.join(missing)}")
        allowed = set(self.params) | set(self.optional)
        extra = set(values) - allowed
        if extra:
            raise ValueError(f"{self.name}: 모르는 빈칸: {', '.join(sorted(extra))}")
        return {p: values.get(p) for p in (*self.params, *self.optional)}


T1 = Template(
    "holder_chain", "최대주주를 따라 위로 (최대주주의 최대주주)",
    f"""
MATCH p = (c:Corp {{key: $corp}})<-[:OWNS*1..{MAX_DEPTH}]-(:Corp)
WHERE length(p) <= $depth
  AND all(r IN relationships(p) WHERE r.source_table = 'major_shareholder' AND {at('r')})
{RETURN_PATH}""",
    ("corp", "as_of", "depth"),
)

T2 = Template(
    "descendants", "출자를 따라 아래로 (자회사·손자회사, 간접 지분율)",
    f"""
MATCH p = (c:Corp {{key: $corp}})-[:OWNS*1..{MAX_DEPTH}]->(:Corp)
WHERE length(p) <= $depth
  AND all(r IN relationships(p) WHERE r.source_table = 'investment' AND {at('r')}
                                  AND ($min_pct IS NULL OR r.pct >= $min_pct))
{return_path(DESCENDANTS_LIMIT)}""",
    ("corp", "as_of", "depth"), ("min_pct",),
)

T3 = Template(
    "ownership_path", "두 회사 사이 지분 경로",
    f"""
MATCH p = shortestPath((a:Corp {{key: $corp}})-[:OWNS*..{MAX_DEPTH}]-(b:Corp {{key: $other}}))
WHERE all(r IN relationships(p) WHERE {at('r')})
{RETURN_PATH}""",
    ("corp", "other", "as_of"),
)

T4 = Template(
    "shared_positions", "같은 시점에 두 회사 이상에서 임원인 사람 (겸직)",
    f"""
MATCH p = (pe:Person)-[r:HOLDS_POSITION]->(c:Corp)
WHERE {at('r')} AND ($corps IS NULL OR c.key IN $corps) AND (NOT $outside_only OR r.outside)
WITH pe, collect(p) AS ps, collect(DISTINCT c.key) AS cs
WHERE size(cs) >= 2 AND ($must IS NULL OR $must IN cs)
UNWIND ps AS p
{RETURN_PATH}""",
    ("as_of", "outside_only"), ("corps", "must"),
)

# T4의 짝. T4가 0행일 때 "없음"이라고 말할 근거: 같은 범위(기준일·회사·사외이사 여부)에서 대조한 임원이 몇 명인지.
# 그래프는 닫힌 세계(기준일의 대상 회사 임원은 전부 적재)라 "대조한 M명 중 겹치는 사람 0명"은 사실이지만,
# 조각 검색(상위 k개)에서 못 찾은 것은 "없음"이 아니라 "모름"이다 — 이 둘을 가르는 숫자를 LLM이 아니라 코드가 적는다 (2026-11-26, g10)
T4_SCOPE = Template(
    "position_scope", "겸직 대조 범위: 그 시점 회사별 임원 수 (T4가 0행일 때 '없음'의 근거)",
    f"""
MATCH (pe:Person)-[r:HOLDS_POSITION]->(c:Corp)
WHERE {at('r')} AND ($corps IS NULL OR c.key IN $corps) AND (NOT $outside_only OR r.outside)
RETURN c.key AS corp_key, c.name AS corp_name, count(DISTINCT pe) AS persons
ORDER BY c.name""",
    ("as_of", "outside_only"), ("corps",),
)

T5 = Template(
    "person_history", "한 사람의 임원 이력 (시간순)",
    f"""
MATCH (pe:Person)-[r0:HOLDS_POSITION]->(:Corp {{key: $corp}})
WHERE {at('r0')} AND {title_ok('r0')} AND ($person IS NULL OR pe.name = $person)
WITH DISTINCT pe
MATCH p = (pe)-[r:HOLDS_POSITION]->(:Corp)
WHERE r.superseded_by IS NULL
WITH p, r ORDER BY r.valid_from
{RETURN_PATH}""",
    ("corp", "as_of"), ("title", "person"),
)

T6 = Template(
    "corp_lineage", "회사 이름 이력과 합병 전 회사 (FORMERLY)",
    f"""
MATCH (c:Corp)
WHERE c.key = $corp OR ($name IS NOT NULL AND $name IN c.names)
OPTIONAL MATCH q = (c)-[:FORMERLY*1..2]-(:Corp)
WITH c, q
WITH CASE WHEN q IS NULL THEN [c] ELSE nodes(q) END AS ns, CASE WHEN q IS NULL THEN [] ELSE relationships(q) END AS rs
RETURN [n IN ns | {_NODE}] AS nodes, [r IN rs | r{{.*, type: type(r), start_key: coalesce(startNode(r).key, startNode(r).pid)}}] AS rels
LIMIT {ROW_LIMIT}
""",
    (), ("corp", "name"),
)

T7 = Template(
    "facts_at", "한 회사의 그 시점 최대주주·임원 (1단계)",
    f"""
MATCH p = (x)-[r:OWNS|HOLDS_POSITION]->(c:Corp {{key: $corp}})
WHERE {at('r')}
  AND ((type(r) = 'OWNS' AND r.source_table = 'major_shareholder' AND ($kind IS NULL OR $kind = 'holders'))
       OR (type(r) = 'HOLDS_POSITION' AND ($kind IS NULL OR $kind = 'positions') AND {title_ok('r')}))
{RETURN_PATH}""",
    ("corp", "as_of"), ("kind", "title"),
)

# T8은 모양이 다르다: 관계 행의 근거 접수번호 → 그 보고서의 해당 섹션 조각. 그래프 결과를 공시 원문과 잇는 다리
# $rcepts는 관계 행에 나온 순서. 그 순서대로 보고서마다 앞 조각 $per_doc개씩 고른 뒤 전체 $limit개에서 자른다.
# (전에는 접수번호 순으로 $limit개를 먼저 잘라, 임원 표처럼 조각이 많은 오래된 보고서 하나가 칸을 다 채웠다 — 2026-11-25 평가 g11·g12)
T8 = Template(
    "evidence_chunks", "관계 행의 근거 공시 → 그 섹션의 조각",
    """
UNWIND range(0, size($rcepts) - 1) AS i
MATCH (d:Document {rcept_no: $rcepts[i]})-[:HAS_CHUNK]->(ch:Chunk)
WHERE $sections IS NULL OR any(s IN $sections WHERE ch.path CONTAINS s)
WITH i, ch ORDER BY i, ch.seq
WITH i, collect(ch)[..$per_doc] AS chs
UNWIND chs AS ch
RETURN ch.rcept_no AS rcept_no, ch.file_name AS file_name, ch.section_id AS section_id, ch.path AS path, ch.text AS text, ch.seq AS seq,
       i AS doc_order
ORDER BY doc_order, seq
LIMIT $limit
""",
    ("rcepts", "limit", "per_doc"), ("sections",),
)

TEMPLATES: dict[str, Template] = {t.name: t for t in (T1, T2, T3, T4, T4_SCOPE, T5, T6, T7, T8)}   # T4_SCOPE는 답 템플릿이 아니라 T4의 보조 질의

# 관계의 출처 표 → 근거 조각을 찾을 섹션 이름의 일부 (사업보고서 목차 기준)
EVIDENCE_SECTIONS = {
    "investment": ["출자"],
    "major_shareholder": ["최대주주", "주주에 관한"],
    "HOLDS_POSITION": ["임원"],
    "FORMERLY": ["연혁", "회사의 개요"],
}

_PARAM_RE = re.compile(r"\$([A-Za-z_]\w*)")


def cypher_params(t: Template) -> set[str]:
    """질의문에 실제로 쓰인 빈칸 이름 (테스트가 params·optional과 맞는지 본다)."""
    return set(_PARAM_RE.findall(t.cypher))


def run(driver, name: str, values: dict, database: str = "neo4j") -> list[dict]:
    t = TEMPLATES[name]
    params = t.bind(values)
    return [dict(r) for r in driver.execute_query(t.cypher, database_=database, **params).records]
