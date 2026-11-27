"""회사 이름 ↔ corp_code를 잇는 이름 목록과, 질문에서 회사를 찾는 함수 (라우터·검색 회사 필터·그래프 Corp 노드의 names가 같은 출처를 쓴다).

질문 속 회사 찾기(find_corps)는 한 벌만 둔다 (2026-11-27, 코드 리뷰 A2): 전에는 라우터(router.find_corps)와 검색 회사 필터
(retrievers/filters.guess_corp_code)가 서로 다른 사전으로 회사를 골라, /ask 한 번에 그래프와 검색이 다른 회사를 볼 수 있었다.

출처 세 가지. 모두 공시에 적힌 이름이고, corp_code 연결은 Phase 1~2의 규칙(정확히 같은 이름, 알파벳 한글 읽기,
config/corp_name_overrides.csv)으로 이미 끝난 것만 쓴다. 여기서 새로 추측해 잇지 않는다.
1) 공시 목록(disclosure)의 회사명
2) 출자 표(investment)에서 corp_code로 연결된 출자 대상 이름. 지주가 자회사를 'KB국민카드'처럼 부르는 이름
   (DART 등록명은 '케이비국민카드'), 합병 전 회사 이름('KB생명보험')이 여기서 들어온다
3) 최대주주 표(major_shareholder)에서 corp_code로 연결된 주주 이름 ('신한금융지주회사' = 신한지주)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import duckdb

# 이름 칸에 글자·숫자가 하나도 없으면('-' 등) 출자 대상이 아니라 빈 칸이다. 전각 글자(ＰＯＳＣＯ)도 글자로 본다.
# 파이썬(re)과 DuckDB(regexp_matches)에서 같은 식을 쓴다
NAME_CHARS = "[가-힣A-Za-z0-9Ａ-Ｚａ-ｚ０-９]"
_NAME_CHARS_RE = re.compile(NAME_CHARS)


def has_name(name: str | None) -> bool:
    return bool(name) and bool(_NAME_CHARS_RE.search(name))


LINKED_NAMES_SQL = f"""
SELECT corp_code, corp_name AS name FROM disclosure WHERE corp_name IS NOT NULL
UNION SELECT investee_corp_code, investee_name FROM investment
      WHERE investee_corp_code IS NOT NULL AND regexp_matches(investee_name, '{NAME_CHARS}')
UNION SELECT holder_corp_code, nm FROM major_shareholder WHERE holder_corp_code IS NOT NULL
"""


def linked_names(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """[(corp_code, 공시에 적힌 이름)] — 같은 이름이 여러 번 나와도 한 번."""
    return con.execute(LINKED_NAMES_SQL).fetchall()


# ---------------------------------------------------------------- 질문 속 회사 찾기

_STRIP_RE = re.compile(r"주식회사|\(주\)|㈜|\s+")
# corp 테이블(전체 회사)에서 이름을 찾아볼 후보. 수집 대상이 아닌 회사(합병으로 없어진 회사 등)를 위해서만 쓴다
_NAME_CANDIDATE_RE = re.compile(r"[가-힣A-Za-z0-9]+(?:은행|증권|보험|생명보험|손해보험|생명|카드|캐피탈|금융지주|금융투자|종합금융|자산운용|저축은행|신탁|투자증권)")


@dataclass(frozen=True)
class CorpMention:
    corp_code: str
    text: str      # 질문에 적힌 그대로
    current: str   # 지금 이름 (corp/target_corp). 질문의 이름과 다르면 옛 이름으로 물은 것


def bare(name: str) -> str:
    """공백과 법인 표기('주식회사'·'(주)'·'㈜')를 뗀 꼴. 사전과 질문을 같은 꼴로 만들어 비교한다."""
    return _STRIP_RE.sub("", name or "")


def name_dictionary(con: duckdb.DuckDBPyConnection, lineage: list[dict] | None = None) -> dict[str, str]:
    """이름(공백·법인 표기 뗀 꼴) → corp_code. 같은 이름이 두 corp_code를 가리키면 그 이름은 뺀다 (추측하지 않음).
    출처: target_corp, 위 이름 목록(공시 목록·출자 표·최대주주 표에서 corp_code로 연결된 이름), corp_lineage.csv의 옛 이름."""
    pairs = con.execute("SELECT corp_code, corp_name FROM target_corp").fetchall() + linked_names(con)
    pairs += [(r["former_corp_code"], r["former_name"]) for r in (lineage or []) if r.get("former_name")]
    seen: dict[str, set[str]] = {}
    for code, name in pairs:
        b = bare(name)
        if len(b) >= 2:
            seen.setdefault(b, set()).add(code)
    return {name: next(iter(codes)) for name, codes in seen.items() if len(codes) == 1}


def find_corps(con: duckdb.DuckDBPyConnection, question: str, lineage: list[dict] | None = None) -> list[CorpMention]:
    """질문에 나온 회사들 (나온 순서). 긴 이름부터 찾고 겹치는 자리는 다시 쓰지 않는다 ('KB국민은행' 안의 '국민은행')."""
    bare_q = bare(question)
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


def guess_corp_code(con: duckdb.DuckDBPyConnection, question: str) -> str | None:
    """검색 회사 필터용: 질문의 회사 하나. 여럿이면 질문에 가장 길게 적힌 이름의 회사(전 filters.guess_corp_code와 같은 기준). 없으면 None."""
    mentions = find_corps(con, question)
    if not mentions:
        return None
    return max(mentions, key=lambda m: len(bare(m.text))).corp_code
