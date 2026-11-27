"""공시 목록(disclosure 테이블)에서 정기보고서를 찾아 주요정보 API 파라미터(bsns_year, reprt_code)로 바꾼다.

Phase 2 수집은 "어느 회사가 어느 보고서를 냈는가"를 추측하지 않는다. Phase 1 백필로 모든 대상 회사의
공시 목록이 disclosure 테이블에 있으므로, 거기에 있는 정기보고서만 주요정보 API를 부른다.
사업보고서를 내지 않는 자회사(0002의 29곳)는 자동으로 빠지고, "데이터 없음" 호출이 생기지 않는다.

보고서명 → 파라미터 (12월 결산 기준. 4대 금융지주와 금융 자회사는 모두 12월 결산이다)

| report_nm                        | bsns_year | reprt_code |
|----------------------------------|-----------|------------|
| 사업보고서 (2025.12)             | 2025      | 11011      |
| 반기보고서 (2025.06)             | 2025      | 11012      |
| 분기보고서 (2025.03)             | 2025      | 11013 (1분기) |
| 분기보고서 (2025.09)             | 2025      | 11014 (3분기) |
| [기재정정]사업보고서 (2025.12)   | 2025      | 11011 (같은 보고서의 정정본) |

정정본은 원본과 같은 파라미터를 가진다. 주요정보 API는 파라미터로 조회하면 최신 정정본을 주므로
(0005 확인 결과), 정정 공시가 목록에 들어오면 같은 파티션을 다시 수집해서 새 버전을 받는다.
"""

import re

import duckdb

PERIODIC_REPORT_RE = re.compile(r"^(?:\[[^\]]*\]\s*)*(사업|반기|분기)보고서\s*\((\d{4})\.(\d{2})\)")

REPORT_CODE_BY_KIND = {"사업": "11011", "반기": "11012"}


def periodic_report_key(report_nm: str) -> tuple[str, str] | None:
    """보고서명 → (bsns_year, reprt_code). 정기보고서가 아니면 None."""
    m = PERIODIC_REPORT_RE.match((report_nm or "").strip())
    if not m:
        return None
    kind, year, month = m.group(1), m.group(2), int(m.group(3))
    if kind == "분기":
        reprt_code = "11013" if month <= 6 else "11014"
    else:
        reprt_code = REPORT_CODE_BY_KIND[kind]
    return year, reprt_code


def report_partition_key(bsns_year: str, reprt_code: str) -> str:
    """Dagster 보고서 파티션 키. 예: '2025-11011'"""
    return f"{bsns_year}-{reprt_code}"


def split_report_partition_key(key: str) -> tuple[str, str]:
    bsns_year, reprt_code = key.split("-")
    return bsns_year, reprt_code


FIRST_REPORT_YEAR = 2021   # 정기보고서를 수집하는 첫 사업연도 (Dagster 파티션·백필 스크립트가 같이 쓴다)


def report_partition_keys(first_year: int, last_year: int) -> list[str]:
    """first_year부터 last_year까지 모든 (사업연도, 보고서 종류) 파티션 키. 연도 안에서는 결산 순서."""
    return [
        report_partition_key(str(year), code)
        for year in range(first_year, last_year + 1)
        for code in ("11013", "11012", "11014", "11011")
    ]


def periodic_reports_of(con: duckdb.DuckDBPyConnection, corp_code: str, bgn_de: str | None = None,
                        end_de: str | None = None) -> list[dict]:
    """한 회사의 공시 목록에서 정기보고서(정정본 포함)를 접수일 순으로 돌려준다.

    bgn_de·end_de(YYYYMMDD)를 주면 그 접수 기간만 본다. 각 행: rcept_no, rcept_dt, report_nm, bsns_year, reprt_code
    """
    sql = "SELECT rcept_no, rcept_dt, report_nm FROM disclosure WHERE corp_code = ? AND report_nm LIKE '%보고서 (%'"
    params: list = [corp_code]
    if bgn_de and end_de:
        sql += " AND rcept_dt BETWEEN STRPTIME(?, '%Y%m%d') AND STRPTIME(?, '%Y%m%d')"
        params += [bgn_de, end_de]
    rows = []
    for rcept_no, rcept_dt, report_nm in con.execute(sql + " ORDER BY rcept_dt, rcept_no", params).fetchall():
        key = periodic_report_key(report_nm)
        if key:
            rows.append({"rcept_no": rcept_no, "rcept_dt": rcept_dt, "report_nm": report_nm,
                         "bsns_year": key[0], "reprt_code": key[1]})
    return rows


def latest_periodic_report(con: duckdb.DuckDBPyConnection, corp_code: str, bsns_year: str, reprt_code: str) -> dict | None:
    """한 회사의 한 보고서(정정본이 있으면 최신 정정본)의 공시 행. 목록에 없으면 None."""
    matches = [r for r in periodic_reports_of(con, corp_code) if (r["bsns_year"], r["reprt_code"]) == (bsns_year, reprt_code)]
    return matches[-1] if matches else None
