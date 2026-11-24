"""수집(collectors) + 적재(loaders)를 묶은 실행 단위.

Dagster 자산은 이 함수들을 호출만 한다. 그래서 같은 작업을 Dagster 없이 CLI로도 할 수 있다.

    # 기업 목록 전체 적재 (API 1건)
    python -m src.loaders.collect corps

    # 한 회사의 기간별 공시 목록 적재 (API: 페이지 수만큼, 보통 1건)
    python -m src.loaders.collect disclosures 00688996 20261101 20261130

    # 타법인 출자현황 적재 (API: 회사 수만큼)
    python -m src.loaders.collect investments 2025 00688996 00382199

    # 한 회사의 한 보고서에서 주요정보 4종(출자·최대주주·변동·임원) 원본 저장 (API 4건, Phase 2)
    python -m src.loaders.collect report_details 00688996 2025 11011
    # 원본에서 관계 테이블 다시 만들기 (API 0건)
    python -m src.loaders.relations rebuild

    # 한 공시의 원문 ZIP 저장 (API 1건, Phase 3). 저장 후 형식 확인: python -m src.loaders.documents inspect <rcept_no>
    python -m src.loaders.collect document 20240318000xxx

Dagster가 도는 중에는 DuckDB 쓰기 잠금이 겹치므로 CLI를 함께 실행하지 않는다.
"""

import sys

import duckdb

from src.collectors.corp_code import download_corp_code_xml, get_api_key, parse_corps
from src.collectors.disclosure_list import fetch_disclosures, make_request_key, parse_disclosures
from src.collectors.document import fetch_document
from src.collectors.investment import fetch_investments
from src.collectors.report_detail import ENDPOINTS, fetch_report_detail
from src.loaders.documents import save_document
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


def collect_report_details(
    con: duckdb.DuckDBPyConnection, api_key: str, corp_code: str, bsns_year: str, reprt_code: str
) -> dict:
    """한 회사의 한 정기보고서에서 주요정보 4종 원본을 받아 저장한다. API 4건.

    원본의 request_key에는 요청 파라미터에 더해 응답이 알려 준 접수번호(rcept_no)를 넣는다.
    주요정보 API는 같은 파라미터로 조회해도 정정본이 나오면 다른 rcept_no의 응답을 주는데,
    파라미터만 키로 쓰면 정정본이 원본을 덮어써서 "정정 전에 뭐라고 적혀 있었나"(0005의 기록 시간)가 사라진다.
    rcept_no를 키에 넣으면 원본과 정정본이 각각 남는다. 데이터 없음(013) 응답은 rcept_no가 없으므로 파라미터만 쓴다.

    정규화 테이블은 여기서 만들지 않는다. 관계 테이블은 "다음 보고서"를 봐야 시간을 닫을 수 있어서
    보고서 하나씩이 아니라 원본 전체로 다시 만든다 (src/loaders/relations.py rebuild, investments.py rebuild).
    """
    params = {"corp_code": corp_code, "bsns_year": bsns_year, "reprt_code": reprt_code}
    pages = {"investment": fetch_investments(api_key, corp_code, bsns_year, reprt_code)}
    for name in ENDPOINTS:
        pages[name] = fetch_report_detail(api_key, name, corp_code, bsns_year, reprt_code)
    rcept_nos = set()
    con.begin()
    try:
        for p in pages.values():
            items = p.body.get("list") or []
            rcept_no = items[0].get("rcept_no") if items else None
            key = make_request_key({**params, "rcept_no": rcept_no}) if rcept_no else p.request_key
            if rcept_no:
                rcept_nos.add(rcept_no)
            save_raw(con, p.endpoint, key, p.body)
        con.commit()
    except Exception:
        con.rollback()
        raise
    statuses = {name: p.body.get("status") for name, p in pages.items()}
    return {
        "api_calls": len(pages),
        "statuses": statuses,
        "no_data": sum(s == "013" for s in statuses.values()),
        "rcept_no": ", ".join(sorted(rcept_nos)) or None,
    }


def collect_document(con: duckdb.DuckDBPyConnection, api_key: str, rcept_no: str) -> dict:
    """한 공시의 원문 ZIP을 받아 파일로 저장하고 document 테이블에 적는다. API 1건 (Phase 3)."""
    zip_bytes = fetch_document(api_key, rcept_no)
    return {"api_calls": 1, **save_document(con, rcept_no, zip_bytes)}


def main(argv: list[str]) -> None:
    usage = (
        "사용법: python -m src.loaders.collect corps"
        " | disclosures <corp_code> <bgn_de> <end_de>"
        " | investments <bsns_year> <corp_code> [<corp_code> ...]"
        " | report_details <corp_code> <bsns_year> <reprt_code>"
        " | document <rcept_no>"
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
        elif argv[0] == "report_details" and len(argv) == 4:
            print(collect_report_details(con, api_key, *argv[1:]))
        elif argv[0] == "document" and len(argv) == 2:
            print(collect_document(con, api_key, argv[1]))
        else:
            sys.exit(usage)


if __name__ == "__main__":
    main(sys.argv[1:])
