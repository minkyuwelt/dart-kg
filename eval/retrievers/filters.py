"""질문에서 회사를 알아내 후보를 그 회사 보고서로 좁힌다 (0009 실험의 "회사 필터" 변수).

규칙만 쓴다: target_corp의 회사명(과 "주식회사"·"(주)"·"㈜"를 뗀 꼴)이 질문에 들어 있으면 그 corp_code. 여럿이면 가장 긴 이름.
키워드 기본선이 "주당 현금배당금"처럼 모든 회사 보고서에 같은 말이 있는 질문에서 회사를 못 가리던 문제(0008 1차)에 대응한다.
질문에 회사명이 없으면 None → 필터 없이 검색.
"""

import re

import duckdb

_STRIP_RE = re.compile(r"주식회사|\(주\)|㈜|\s+")


def _bare(name: str) -> str:
    return _STRIP_RE.sub("", name or "")


def corp_names(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """[(corp_code, 이름)] — target_corp의 이름과 disclosure에 적힌 공시 당시 이름을 모두."""
    rows = con.execute(
        """SELECT corp_code, corp_name FROM target_corp
           UNION SELECT DISTINCT corp_code, corp_name FROM disclosure WHERE corp_name IS NOT NULL"""
    ).fetchall()
    return [(r[0], r[1]) for r in rows if r[1]]


def guess_corp_code(con: duckdb.DuckDBPyConnection, question: str) -> str | None:
    q = _bare(question)
    best: tuple[int, str] | None = None
    for corp_code, name in corp_names(con):
        bare = _bare(name)
        if len(bare) >= 2 and bare in q and (best is None or len(bare) > best[0]):
            best = (len(bare), corp_code)
    return best[1] if best else None
