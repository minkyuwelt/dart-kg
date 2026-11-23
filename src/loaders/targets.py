"""수집 대상 회사 목록(target_corp)을 investment 테이블에서 만든다.

범위 결정: docs/decisions/0002-target-scope.md
- 지주 4곳 (src/collectors/corp_code.py의 HOLDING_CORPS)
- 직속 자회사: 지주의 해당 연도 사업보고서 타법인 출자현황 중
  corp_code로 연결되고 기말 지분율 30% 이상인 곳
  (30%는 금융지주회사법의 상장 자회사 지분 요건. 2025 기준 실제 최소값은 제주은행 64%)

해외 자회사·펀드처럼 corp_code가 없는 곳은 DART에서 수집할 수 없으므로 대상에 넣지 않는다.
이들은 모회사의 출자 데이터(investment)에 그래프 노드로만 들어간다.

손자회사는 넣지 않는다. 2025 기준 국내 손자회사(자회사 지분 50% 이상) 13곳 모두 사업보고서를
내지 않아 지분·임원 데이터가 없고, 자회사의 출자 데이터에 노드로 이미 들어가기 때문이다.
"""

from datetime import date

import duckdb

from src.collectors.corp_code import HOLDING_CORPS

MIN_SUBSIDIARY_STAKE = 30.0


def latest_business_report_year(today: date) -> int:
    """오늘 기준으로 사업보고서가 나와 있을 가장 최근 사업연도.

    12월 결산 법인의 사업보고서 제출 기한은 다음 해 3월 말이다. 4월부터는 작년 것을 쓴다.
    """
    return today.year - 1 if today.month >= 4 else today.year - 2


def rebuild_target_corps(con: duckdb.DuckDBPyConnection, bsns_year: int) -> dict:
    """target_corp 테이블을 다시 만든다. investment 테이블에 해당 연도 지주 사업보고서 데이터가 있어야 한다."""
    holdings = list(HOLDING_CORPS)
    con.begin()
    try:
        con.execute("DELETE FROM target_corp")
        con.execute(
            """
            INSERT INTO target_corp (corp_code, corp_name, role, parent_corp_code, stake_pct, source_rcept_no, bsns_year, loaded_at)
            SELECT corp_code, corp_name, 'holding', NULL, NULL, NULL, ?, now()
            FROM corp WHERE corp_code IN (SELECT unnest(?::VARCHAR[]))
            """,
            [str(bsns_year), holdings],
        )
        # 보통주·우선주처럼 한 회사가 여러 행이면 지분율이 가장 큰 행을 쓴다
        con.execute(
            """
            INSERT INTO target_corp (corp_code, corp_name, role, parent_corp_code, stake_pct, source_rcept_no, bsns_year, loaded_at)
            SELECT i.investee_corp_code, c.corp_name, 'subsidiary', i.investor_corp_code,
                   i.trmend_blce_qota_rt, i.rcept_no, i.bsns_year, now()
            FROM investment i
            JOIN corp c ON c.corp_code = i.investee_corp_code
            WHERE i.investor_corp_code IN (SELECT unnest(?::VARCHAR[]))
              AND i.bsns_year = ? AND i.reprt_code = '11011'
              AND i.investee_corp_code IS NOT NULL
              AND i.trmend_blce_qota_rt >= ?
              AND i.investee_corp_code NOT IN (SELECT unnest(?::VARCHAR[]))
            QUALIFY row_number() OVER (PARTITION BY i.investee_corp_code ORDER BY i.trmend_blce_qota_rt DESC) = 1
            """,
            [holdings, str(bsns_year), MIN_SUBSIDIARY_STAKE, holdings],
        )
        con.commit()
    except Exception:
        con.rollback()
        raise
    counts = dict(con.execute("SELECT role, count(*) FROM target_corp GROUP BY 1").fetchall())
    return {"bsns_year": bsns_year, "holdings": counts.get("holding", 0), "subsidiaries": counts.get("subsidiary", 0)}
