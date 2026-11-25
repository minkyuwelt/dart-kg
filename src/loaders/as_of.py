"""시점 질의 (Phase 2 테이블, 0005): "그 날짜에 참이었던 행"만 돌려주는 일반 함수. API(src/api)와 화면이 호출한다.

시점 필터는 notebooks/phase2_questions.py에서 검증한 것과 같다:
    valid_from <= d AND (valid_to IS NULL OR valid_to > d) AND superseded_by IS NULL
임원 행에는 생년월을 넣지 않는다 — 식별(person_id)에만 쓰고 바깥으로 내보내지 않는다 (0006).
"""

from __future__ import annotations

from datetime import date

import duckdb

AS_OF_FILTER = "valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) AND superseded_by IS NULL"


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list) -> list[dict]:
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def target_corps(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """수집 대상 회사 목록 (지주 먼저, 그다음 자회사 이름순)."""
    return _rows(con, "SELECT corp_code, corp_name, role, parent_corp_code, stake_pct FROM target_corp ORDER BY role DESC, corp_name", [])


def executives_as_of(con: duckdb.DuckDBPyConnection, corp_code: str, as_of: date, outside_only: bool = False) -> list[dict]:
    """as_of 날짜에 재임 중이던 임원. outside_only면 사외이사만."""
    extra = " AND rgist_exctv_at = '사외이사'" if outside_only else ""
    return _rows(
        con,
        f"""SELECT nm, ofcps, rgist_exctv_at, fte_at, chrg_job, tenure_start_on, tenure_start_method, tenure_end_on,
                   valid_from, valid_to, source_rcept_no
            FROM executive WHERE corp_code = ? AND {AS_OF_FILTER}{extra} ORDER BY rgist_exctv_at, nm""",
        [corp_code, as_of, as_of],
    )


def shareholders_as_of(con: duckdb.DuckDBPyConnection, corp_code: str, as_of: date) -> list[dict]:
    """as_of 날짜 기준 최대주주·특수관계인과 지분율 (지분율 큰 순)."""
    return _rows(
        con,
        f"""SELECT nm, relate, stock_knd, holder_corp_code, trmend_posesn_stock_co AS shares, trmend_posesn_stock_qota_rt AS pct,
                   valid_from, valid_to, source_rcept_no
            FROM major_shareholder WHERE corp_code = ? AND {AS_OF_FILTER} ORDER BY pct DESC NULLS LAST, nm""",
        [corp_code, as_of, as_of],
    )
