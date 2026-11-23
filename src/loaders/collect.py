"""수집(collectors) + 적재(loaders)를 묶은 실행 단위.

Dagster 자산은 이 함수들을 호출만 한다. 그래서 같은 작업을 Dagster 없이 CLI로도 할 수 있다.

    # 기업 목록 전체 적재 (API 1건)
    python -m src.loaders.collect corps

    # 한 회사의 기간별 공시 목록 적재 (API: 페이지 수만큼, 보통 1건)
    python -m src.loaders.collect disclosures 00688996 20261101 20261130

    # 타법인 출자현황 적재 (API: 회사 수만큼)
    python -m src.loaders.collect investments 2025 00688996 00382199

Dagster가 도는 중에는 DuckDB 쓰기 잠금이 겹치므로 CLI를 함께 실행하지 않는다.
"""

import sys

import duckdb

from src.collectors.corp_code import download_corp_code_xml, get_api_key, parse_corps
from src.collectors.disclosure_list import fetch_disclosures, parse_disclosures
from src.collectors.investment import fetch_investments
from src.loaders.duckdb_store import connect, save_raw, upsert_corps, upsert_disclosures
from src.loaders.investments import rebuild_investments


def collect_corps(con: duckdb.DuckDBPyConnection, api_key: str) -> dict:
    """corpCode.xml 전체를 corp 테이블에 적재한다."""
    corps = parse_corps(download_corp_code_xml(api_key))
    rows = upsert_corps(con, list(corps.values()))
    return {"api_calls": 1, "rows": rows}


def collect_disclosures(con: duckdb.DuckDBPyConnection, api_key: str, corp_code: str, bgn_de: str, end_de: str) -> dict:
    """한 회사의 기간 내 공시 목록을 받아 원본과 정규화 테이블에 함께 적재한다.

    원본 저장과 upsert를 한 트랜잭션으로 묶는다. 중간에 실패하면 둘 다 반영되지 않는다.
    """
    pages = fetch_disclosures(api_key, corp_code, bgn_de, end_de)
    rows = parse_disclosures(pages)
    con.begin()
    try:
        for page in pages:
            save_raw(con, page.endpoint, page.request_key, page.body)
        loaded = upsert_disclosures(con, rows)
        con.commit()
    except Exception:
        con.rollback()
        raise
    in_period = con.execute(
        """
        SELECT count(*) FROM disclosure
        WHERE corp_code = ? AND rcept_dt BETWEEN STRPTIME(?, '%Y%m%d') AND STRPTIME(?, '%Y%m%d')
        """,
        [corp_code, bgn_de, end_de],
    ).fetchone()[0]
    return {"api_calls": len(pages), "fetched_rows": loaded, "rows_in_period": in_period}


def collect_investments(
    con: duckdb.DuckDBPyConnection, api_key: str, corp_codes: list[str], bsns_year: str, reprt_code: str = "11011"
) -> dict:
    """여러 회사의 타법인 출자현황 원본을 받아 저장한 뒤 investment 테이블을 다시 만든다. API: 회사 수만큼."""
    statuses = []
    for corp_code in corp_codes:
        page = fetch_investments(api_key, corp_code, bsns_year, reprt_code)
        save_raw(con, page.endpoint, page.request_key, page.body)
        statuses.append(page.body.get("status"))
    stats = rebuild_investments(con)
    return {"api_calls": len(corp_codes), "no_data": statuses.count("013"), "investment_rows": stats["rows"]}


def main(argv: list[str]) -> None:
    usage = (
        "사용법: python -m src.loaders.collect corps"
        " | disclosures <corp_code> <bgn_de> <end_de>"
        " | investments <bsns_year> <corp_code> [<corp_code> ...]"
    )
    if not argv:
        sys.exit(usage)
    api_key = get_api_key()
    with connect() as con:
        if argv[0] == "corps" and len(argv) == 1:
            print(collect_corps(con, api_key))
        elif argv[0] == "disclosures" and len(argv) == 4:
            print(collect_disclosures(con, api_key, *argv[1:]))
        elif argv[0] == "investments" and len(argv) >= 3:
            print(collect_investments(con, api_key, argv[2:], argv[1]))
        else:
            sys.exit(usage)


if __name__ == "__main__":
    main(sys.argv[1:])
