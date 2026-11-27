"""시점 질의 (Phase 2 테이블, 0005): "그 날짜에 참이었던 행"만 돌려주는 일반 함수. API(src/api)와 화면이 호출한다.

시점 필터는 notebooks/phase2_questions.py에서 검증한 것과 같다:
    valid_from <= d AND (valid_to IS NULL OR valid_to > d) AND superseded_by IS NULL
임원 행에는 생년월을 넣지 않는다 — 식별(person_id)에만 쓰고 바깥으로 내보내지 않는다 (0006).
"""

from __future__ import annotations

from datetime import date

import duckdb

from src.loaders.periodic_reports import periodic_reports_of

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


# ----- 회사 페이지 (0013 세 번째 페이지) -----
# 화면이 한 번에 받도록 아래 함수들을 corp_page()가 묶는다. 각 표의 행에는 근거 접수번호(source_rcept_no)가 있어 API가 뷰어 링크를 붙인다.

def corp_overview(con: duckdb.DuckDBPyConnection, corp_code: str) -> dict | None:
    """회사 한 줄: 이름·역할(지주/자회사)·지주 이름·지주 지분율·최신 정기보고서. 수집 대상에 없으면 None."""
    rows = _rows(
        con,
        """SELECT t.corp_code, t.corp_name, t.role, t.parent_corp_code, p.corp_name AS parent_corp_name, t.stake_pct, c.stock_code
           FROM target_corp t LEFT JOIN target_corp p ON p.corp_code = t.parent_corp_code
                              LEFT JOIN corp c ON c.corp_code = t.corp_code
           WHERE t.corp_code = ?""",
        [corp_code],
    )
    if not rows:
        return None
    row = rows[0]
    # 최신 정기보고서: 접수일이 가장 늦은 사업·반기·분기보고서(정정본 포함, 감사보고서는 제외). 감사보고서만 내는 회사는 None
    reports = periodic_reports_of(con, corp_code)
    latest = [{"source_rcept_no": r["rcept_no"], "report_nm": r["report_nm"], "rcept_dt": r["rcept_dt"]} for r in reports[-1:]]
    row["latest_report"] = latest[0] if latest else None
    return row


def investments_as_of(con: duckdb.DuckDBPyConnection, corp_code: str, as_of: date) -> list[dict]:
    """as_of 날짜에 이 회사가 가진 타법인 출자 (장부가액 큰 순). investee_corp_code가 있으면 화면이 그 회사 페이지로 이어 준다."""
    return _rows(
        con,
        f"""SELECT investee_name, investee_corp_code, invstmnt_purps, frst_acqs_de, trmend_blce_qy AS shares,
                   trmend_blce_qota_rt AS pct, trmend_blce_acntbk_amount AS book_value,
                   valid_from, valid_to, rcept_no AS source_rcept_no
            FROM investment WHERE investor_corp_code = ? AND {AS_OF_FILTER}
            ORDER BY book_value DESC NULLS LAST, investee_name""",
        [corp_code, as_of, as_of],
    )


def concurrent_positions_as_of(con: duckdb.DuckDBPyConnection, corp_code: str, as_of: date) -> list[dict]:
    """as_of 날짜에 지주 겸직 현황 표(0014)가 이 회사의 자리로 적은 지주 임원. 임원 아닌 직책(is_officer=false)은 빼고,
    임원 표가 없는 회사(KB자산운용 등)에서는 이것이 유일한 임원 정보다. 사람 연결 여부(person_method)를 함께 준다."""
    return _rows(
        con,
        """SELECT c.nm, c.ofcps, c.fte_at, c.appointed_on, c.appointed_raw, c.person_method, h.corp_name AS holding_corp_name,
                   c.valid_from, c.valid_to, c.source_rcept_no
            FROM concurrent_position c JOIN target_corp h ON h.corp_code = c.holding_corp_code
            WHERE c.target_corp_code = ? AND c.is_officer
              AND c.valid_from <= ? AND (c.valid_to IS NULL OR c.valid_to > ?) AND c.superseded_by IS NULL
            ORDER BY c.nm""",
        [corp_code, as_of, as_of],
    )


def stake_history(con: duckdb.DuckDBPyConnection, corp_code: str) -> list[dict]:
    """다른 수집 대상 회사(보통 지주)의 출자 표에 적힌 이 회사의 지분율·장부가액을 기준일순으로 (usecases 질문 6).
    정정본에 밀린 행(superseded_by)은 빼고, 같은 기준일에 보통주·우선주 행이 따로 있으면 둘 다 준다."""
    return _rows(
        con,
        """SELECT i.stlm_dt, i.investor_corp_code, t.corp_name AS investor_corp_name, i.share_class,
                  i.trmend_blce_qy AS shares, i.trmend_blce_qota_rt AS pct, i.trmend_blce_acntbk_amount AS book_value,
                  i.rcept_no AS source_rcept_no
           FROM investment i JOIN target_corp t ON t.corp_code = i.investor_corp_code
           WHERE i.investee_corp_code = ? AND i.superseded_by IS NULL
           ORDER BY i.stlm_dt, i.investor_corp_code, i.share_class""",
        [corp_code],
    )


def corp_page(con: duckdb.DuckDBPyConnection, corp_code: str, as_of: date) -> dict | None:
    """회사 페이지 한 번에: 개요·임원·최대주주·출자·겸직 표 자리·지분 추이. 수집 대상이 아니면 None."""
    overview = corp_overview(con, corp_code)
    if overview is None:
        return None
    return {
        "corp": overview,
        "as_of": as_of,
        "executives": executives_as_of(con, corp_code, as_of),
        "shareholders": shareholders_as_of(con, corp_code, as_of),
        "investments": investments_as_of(con, corp_code, as_of),
        "concurrent_positions": concurrent_positions_as_of(con, corp_code, as_of),
        "stake_history": stake_history(con, corp_code),
    }
