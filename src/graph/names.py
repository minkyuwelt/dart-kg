"""회사 이름 ↔ corp_code를 잇는 이름 목록 (라우터의 이름 사전과 그래프 Corp 노드의 names가 같은 출처를 쓴다).

출처 세 가지. 모두 공시에 적힌 이름이고, corp_code 연결은 Phase 1~2의 규칙(정확히 같은 이름, 알파벳 한글 읽기,
config/corp_name_overrides.csv)으로 이미 끝난 것만 쓴다. 여기서 새로 추측해 잇지 않는다.
1) 공시 목록(disclosure)의 회사명
2) 출자 표(investment)에서 corp_code로 연결된 출자 대상 이름. 지주가 자회사를 'KB국민카드'처럼 부르는 이름
   (DART 등록명은 '케이비국민카드'), 합병 전 회사 이름('KB생명보험')이 여기서 들어온다
3) 최대주주 표(major_shareholder)에서 corp_code로 연결된 주주 이름 ('신한금융지주회사' = 신한지주)
"""

from __future__ import annotations

import re

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
