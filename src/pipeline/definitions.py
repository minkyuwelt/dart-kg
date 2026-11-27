"""Dagster 정의: 자산(asset), 품질 검사(asset check), 잡(job), 스케줄.

실행 (저장소 루트에서):
    DAGSTER_HOME=$PWD/dagster_home dagster dev -m src.pipeline.definitions
그다음 브라우저에서 http://localhost:3000

자산 구성 (화살표는 의존 관계)
    corp_codes ─▶ holding_investments ─▶ target_corps ─▶ disclosures
                                              ├────────▶ report_details ─▶ relations  (Phase 2)
                                              └────────▶ documents ─▶ chunks ─▶ chunk_embeddings ─▶ neo4j_chunks  (Phase 3)
    relations ─▶ neo4j_relations  (Phase 4)

    corp_codes          : corpCode.xml 전체 → corp 테이블 (API 1건)
    holding_investments : 지주 4곳의 최신 사업보고서 타법인 출자현황 → investment 테이블 (API 4건)
    target_corps        : investment에서 수집 대상(지주 + 직속 자회사)을 뽑아 target_corp 테이블에 넣고,
                          새 회사를 disclosures의 회사 파티션에 추가한다 (API 0건)
    disclosures         : list.json → raw_api_response + disclosure 테이블
                          파티션 = 월(month) × 회사(corp). 파티션 하나가 "한 회사의 한 달치 공시"이고 보통 API 1건
    report_details      : (Phase 2) 한 회사의 한 정기보고서에서 주요정보 4종(출자·최대주주·변동·임원) 원본 저장
                          파티션 = 회사(corp) × 보고서(report: '2025-11011' = 사업연도-보고서종류). API 4건.
                          disclosure 목록에 그 보고서가 없으면 API를 부르지 않고 건너뛴다 (0건)
    relations           : (Phase 2) 원본 전체에서 investment·major_shareholder·major_shareholder_change·executive·person
                          테이블을 다시 만들고 시간을 닫는다(valid_to·superseded_by, 0005). API 0건
    change_report       : (Phase 3, 0012) 최근 접수된 정기보고서가 임원·최대주주·출자에 만든 변화 → data/reports/changes_날짜.md. API 0건
    documents           : (Phase 3) 한 회사의 한 정기보고서 원문 ZIP(document.xml) 저장. 파티션은 report_details와 같은
                          회사 × 보고서. 정정본은 접수번호가 달라 각각 받는다. 아직 없는 접수번호만 받으므로 API는 보통 0~1건
    chunks              : (Phase 3) 새 원문을 파싱(document_section)하고 아직 조각이 없는 보고서만 자른다(chunk). API 0건
    chunk_embeddings    : (Phase 3) 벡터가 없거나 본문 해시가 바뀐 조각만 노트북 GPU로 임베딩(chunk_embedding). API 0건
    neo4j_chunks        : (Phase 3) DuckDB와 조각 수가 다른 보고서만 Neo4j에 MERGE. Neo4j(Docker)가 꺼져 있으면 실패하고
                          다음 날 스케줄이 다시 시도한다 (실패가 정상 상태)
    neo4j_relations     : (Phase 4, 0011) 관계 테이블 → Neo4j Corp·Person·OWNS·HOLDS_POSITION·FORMERLY·FILED. 지우고 통째로 다시 넣는다.
                          스케줄 daily_graph 11:45 (relations가 최근 바뀌었을 때만)

왜 월 × 회사 파티션인가
    - 일 단위로 나누면 5년 백필 호출이 30배가 된다 (59곳 기준 일 단위 약 107,000건 vs 월 단위 3,540건)
    - "하나금융지주 2024년 3월만 다시 수집" 같은 재처리를 파티션 하나로 할 수 있다
    - 적재가 upsert라서 같은 파티션을 몇 번 다시 돌려도 중복이 생기지 않는다

왜 회사 파티션이 동적(DynamicPartitions)인가
    - 대상 자회사는 지주의 사업보고서에서 뽑기 때문에 코드를 고치지 않아도 해마다 바뀔 수 있다
    - target_corps가 실행될 때 새 회사를 파티션에 추가한다. 빠진 회사의 파티션은 지우지 않는다
      (이미 수집한 공시는 남겨 두고, 매일 스케줄에서만 뺀다)

이 파일에는 수집 로직을 두지 않는다. 자산은 src/loaders/의 함수를 호출만 한다.
"""

import calendar
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import dagster as dg
import duckdb

from src.collectors.corp_code import HOLDING_CORPS, get_api_key
from src.graph.client import connect as graph_connect
from src.graph.client import wait_until_ready
from src.graph.load_chunks import DEFAULT_MODEL, DEFAULT_STRATEGY, pending_count, stale_documents
from src.graph.load_chunks import build as build_neo4j_chunks
from src.graph.load_chunks import stats as neo4j_chunk_stats
from src.graph.load_relations import build as build_neo4j_relations
from src.graph.load_relations import stats as neo4j_relation_stats
from src.loaders.chunks import build_chunks, parsed_rcept_nos, unchunked_rcept_nos
from src.loaders.collect import collect_corps, collect_disclosures, collect_document, collect_investments, collect_report_details
from src.loaders.documents import missing_documents, missing_documents_of_report, rebuild_sections, target_corp_codes, unparsed_documents
from src.loaders.duckdb_store import connect, is_lock_error
from src.loaders.investments import rebuild_investments
from src.loaders.periodic_reports import (
    FIRST_REPORT_YEAR,
    latest_periodic_report,
    periodic_reports_of,
    report_partition_key,
    report_partition_keys,
    split_report_partition_key,
)
from src.loaders.relations import rebuild_relations
from src.pipeline.instance_reads import (
    LOOKBACK_DAYS,
    latest_materialization_time,
    latest_metadata,
    partition_metadata,
    recent_materializations,
    sum_recent_metadata,
)
from src.loaders.targets import latest_business_report_year, rebuild_target_corps
from src.rag.embedding import embed_chunks
from src.reports.changes import build_report, render_markdown, summarize, write_report

# DuckDB 파일 잠금에 걸렸을 때 다시 시도하는 횟수와 간격(초).
# 노트북에서 DuckDB를 잠깐 열어 둔 경우 몇 분 뒤에는 풀리는 경우가 대부분이다.
LOCK_RETRY_MAX = 3
LOCK_RETRY_WAIT_SEC = 300

KST = ZoneInfo("Asia/Seoul")

# 2021-01부터 이번 달까지. end_offset=1이 있어야 아직 끝나지 않은 이번 달도 파티션에 포함된다.
month_partitions = dg.MonthlyPartitionsDefinition(start_date="2021-01-01", end_offset=1, timezone="Asia/Seoul")
corp_partitions = dg.DynamicPartitionsDefinition(name="corp")
disclosure_partitions = dg.MultiPartitionsDefinition({"month": month_partitions, "corp": corp_partitions})

# Phase 2: 정기보고서 파티션. FIRST_REPORT_YEAR(2021) 사업연도부터 올해까지 연 4개(1분기·반기·3분기·사업). 키는 '2025-11011'.
# 회사 × 보고서 조합 중 실제로 보고서가 있는 것만 API를 부르므로, 없는 조합을 백필해도 호출이 늘지 않는다.
report_partitions = dg.StaticPartitionsDefinition(report_partition_keys(FIRST_REPORT_YEAR, datetime.now(KST).year))
report_detail_partitions = dg.MultiPartitionsDefinition({"corp": corp_partitions, "report": report_partitions})


def today_kst() -> date:
    return datetime.now(KST).date()


def month_range(month_key: str, today: date) -> tuple[str, str]:
    """월 파티션 키('2026-11-01')를 DART 검색 기간(YYYYMMDD, YYYYMMDD)으로 바꾼다.

    이번 달이면 끝 날짜를 오늘로 자른다.
    """
    start = date.fromisoformat(month_key)
    last_day = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    end = min(last_day, today)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


@contextmanager
def write_connection() -> Iterator:
    """자산에서 쓰는 DuckDB 쓰기 연결. 파일 잠금에 걸리면 Dagster에 "잠시 뒤 다시 실행"을 요청한다.

    DuckDB는 다른 프로세스가 파일을 열고 있으면(읽기 전용이라도) 쓰기 연결을 열 수 없다.
    예: Jupyter에서 connect(read_only=True)로 열어 둔 채 잊어버린 경우.
    이때 바로 실패시키면 그날 수집이 빠지므로, RetryRequested로 LOCK_RETRY_WAIT_SEC초 뒤 다시 시도한다.

    연결은 DART API를 호출하기 전에 연다. 그래서 잠금 때문에 다시 시도해도 API 호출 수는 늘지 않는다.
    잠금이 아닌 에러(디스크 부족 등)는 그대로 실패시킨다.
    """
    try:
        con = connect()
    except Exception as e:
        if is_lock_error(e):
            raise dg.RetryRequested(max_retries=LOCK_RETRY_MAX, seconds_to_wait=LOCK_RETRY_WAIT_SEC) from e
        raise
    with con:
        yield con


# 자산이 실행 기록 메타데이터에 남기고 스케줄·백필이 읽는 칸 이름. 스케줄이 DuckDB를 열지 않는 이유는 src/pipeline/instance_reads.py
TARGET_CODES_METADATA_KEY = "target_codes"          # target_corps: 수집 대상 corp_code 목록
PERIODIC_REPORTS_METADATA_KEY = "periodic_reports"  # disclosures: 그 달 정기보고서 키 → 최신 접수번호
LISTED_RCEPT_NO_METADATA_KEY = "listed_rcept_no"    # report_details·documents: "이 접수번호를 처리했다"
DOCUMENTS_SAVED_METADATA_KEY = "saved"              # documents: 실제로 저장한 원문 수 (daily_rag_index가 "새 원문이 있었나"로 읽음)


def target_codes_from_instance(instance: dg.DagsterInstance) -> list[str] | None:
    """가장 최근 target_corps 실행이 메타데이터로 남긴 수집 대상 corp_code 목록. 실행 기록이 없으면 None."""
    value = latest_metadata(instance, target_corps.key, TARGET_CODES_METADATA_KEY)
    return list(value) if value is not None else None


def latest_target_bsns_year(instance: dg.DagsterInstance) -> int | None:
    """가장 최근 target_corps 실행이 메타데이터로 남긴 기준 사업연도. 실행 기록이 없으면 None."""
    value = latest_metadata(instance, target_corps.key, "bsns_year")
    return int(value) if value is not None else None


def to_metadata(stats: dict) -> dict:
    """적재 함수가 돌려준 통계를 MaterializeResult 메타데이터로. dict·list 값은 JSON으로 감싼다 (Dagster는 평문 값만 받는다)."""
    return {k: dg.MetadataValue.json(v) if isinstance(v, (dict, list)) else v for k, v in stats.items()}


def corp_name_of(con: duckdb.DuckDBPyConnection, corp_code: str) -> str:
    """로그·메타데이터용 회사 이름. corp 테이블에 없으면 코드 그대로."""
    row = con.execute("SELECT corp_name FROM corp WHERE corp_code = ?", [corp_code]).fetchone()
    return row[0] if row else corp_code


@dg.asset(
    group_name="dart",
    description="공시 대상 전체 회사 목록(corpCode.xml) → corp 테이블. API 1건.",
)
def corp_codes() -> dg.MaterializeResult:
    with write_connection() as con:
        stats = collect_corps(con, get_api_key())
        found = con.execute(
            "SELECT count(*) FROM corp WHERE corp_code IN (SELECT unnest(?::VARCHAR[]))", [list(HOLDING_CORPS)]
        ).fetchone()[0]
    return dg.MaterializeResult(metadata={**stats, "holdings_found": f"{found}/{len(HOLDING_CORPS)}"})


@dg.asset(
    group_name="dart",
    deps=[corp_codes],
    description="지주 4곳의 최신 사업보고서 타법인 출자현황 → investment 테이블. API 4건.",
)
def holding_investments() -> dg.MaterializeResult:
    bsns_year = latest_business_report_year(today_kst())
    with write_connection() as con:
        stats = collect_investments(con, get_api_key(), list(HOLDING_CORPS), str(bsns_year))
    return dg.MaterializeResult(metadata={"bsns_year": bsns_year, **stats})


@dg.asset(
    group_name="dart",
    deps=[holding_investments],
    description="수집 대상(지주 + 지분 30% 이상 직속 자회사) → target_corp 테이블, disclosures 회사 파티션 추가. API 0건.",
)
def target_corps(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    bsns_year = latest_business_report_year(today_kst())
    with write_connection() as con:
        stats = rebuild_target_corps(con, bsns_year)
        unresolved = con.execute(
            """
            SELECT investee_name, resolve_method FROM investment
            WHERE investor_corp_code IN (SELECT unnest(?::VARCHAR[])) AND bsns_year = ? AND reprt_code = '11011'
              AND investee_corp_code IS NULL
            """,
            [list(HOLDING_CORPS), str(bsns_year)],
        ).fetchall()
        codes = [r[0] for r in con.execute("SELECT corp_code FROM target_corp ORDER BY role, corp_code").fetchall()]
    existing = set(context.instance.get_dynamic_partitions("corp"))
    new = sorted(set(codes) - existing)
    if new:
        context.instance.add_dynamic_partitions("corp", new)
    return dg.MaterializeResult(
        metadata={
            **stats,
            "new_partitions": len(new),
            "dropped_from_targets": len(existing - set(codes)),
            # 지주의 직접 출자 중 corp_code로 연결하지 못한 곳. config/corp_name_overrides.csv를 확인할 것
            "unresolved": ", ".join(f"{n}({m})" for n, m in unresolved) or "없음",
            # 매일 스케줄이 DuckDB를 열지 않고 대상 목록을 읽는 곳 (target_codes_from_instance)
            TARGET_CODES_METADATA_KEY: dg.MetadataValue.json(codes),
        }
    )


@dg.asset_check(asset=target_corps, description="지주 4곳이 모두 있고, 지주마다 자회사가 1곳 이상인지")
def target_corps_complete() -> dg.AssetCheckResult:
    # 전체 자회사 수만 보면 한 지주의 자회사가 통째로 빠져도 통과한다. 지주별로 센다.
    with connect(read_only=True) as con:
        rows = con.execute(
            """
            SELECT h.code,
                   EXISTS (SELECT 1 FROM target_corp t WHERE t.role = 'holding' AND t.corp_code = h.code) AS present,
                   (SELECT count(*) FROM target_corp t WHERE t.role = 'subsidiary' AND t.parent_corp_code = h.code) AS subs
            FROM unnest(?::VARCHAR[]) AS h(code)
            ORDER BY 1
            """,
            [list(HOLDING_CORPS)],
        ).fetchall()
    per_holding = {HOLDING_CORPS[code]: subs for code, _, subs in rows}
    passed = all(present and subs > 0 for _, present, subs in rows)
    return dg.AssetCheckResult(passed=passed, metadata={"subsidiaries_by_holding": dg.MetadataValue.json(per_holding)})


@dg.asset(
    group_name="dart",
    partitions_def=disclosure_partitions,
    deps=[target_corps],
    description="한 회사의 한 달치 공시 목록(list.json) → raw_api_response + disclosure 테이블.",
)
def disclosures(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    keys = context.partition_key.keys_by_dimension
    corp_code, month = keys["corp"], keys["month"]
    bgn_de, end_de = month_range(month, today_kst())

    with write_connection() as con:
        corp_name = corp_name_of(con, corp_code)
        context.log.info(f"{corp_name}({corp_code}) {bgn_de}~{end_de} 공시 목록 수집")
        stats = collect_disclosures(con, get_api_key(), corp_code, bgn_de, end_de)
        # 이 달에 접수된 정기보고서(정정본 포함)의 보고서 키 → 최신 접수번호. daily_report_details·daily_documents 스케줄이 DuckDB를 열지 않고
        # 읽는 곳. 접수번호를 함께 남겨 스케줄이 "이 접수번호는 이미 처리했다"를 자산 실행 기록과 견줄 수 있다 (2026-11-27, 전에는 키 목록만)
        reports = {}
        for r in periodic_reports_of(con, corp_code, bgn_de, end_de):   # 접수일 순이라 마지막 것이 최신(정정본)
            reports[report_partition_key(r["bsns_year"], r["reprt_code"])] = r["rcept_no"]
    return dg.MaterializeResult(
        metadata={
            "corp_code": corp_code,
            "corp_name": corp_name,
            "period": f"{bgn_de}~{end_de}",
            **stats,
            PERIODIC_REPORTS_METADATA_KEY: dg.MetadataValue.json(reports),
        }
    )


@dg.asset_check(asset=disclosures, description="모든 공시의 corp_code가 corp 테이블에 있는지 (회사 식별자 무결성)")
def disclosure_corp_code_exists() -> dg.AssetCheckResult:
    with connect(read_only=True) as con:
        orphans = con.execute("SELECT count(*) FROM disclosure d ANTI JOIN corp c USING (corp_code)").fetchone()[0]
    return dg.AssetCheckResult(passed=orphans == 0, metadata={"orphan_rows": orphans})


@dg.asset_check(asset=disclosures, description="rcept_no가 14자리 숫자인지")
def disclosure_rcept_no_format() -> dg.AssetCheckResult:
    # "앞 8자리 = 접수일(rcept_dt)" 검사는 두지 않는다. 2026-11-23 백필(2021~2026, 86,621건)에서
    # 9,695건(11%)이 하루 이상 어긋났다. 대부분 +1일이고 자산운용사의 '효력발생안내'처럼 문서가 만들어진
    # 다음 날 접수되는 경우다. 앞 8자리는 접수일과 같다는 보장이 없으므로 날짜는 rcept_dt만 쓴다.
    with connect(read_only=True) as con:
        bad = con.execute(
            "SELECT count(*) FROM disclosure WHERE NOT regexp_full_match(rcept_no, '[0-9]{14}')"
        ).fetchone()[0]
    # 형식이 어긋나도 데이터를 못 쓰는 건 아니므로 실패가 아니라 경고로 둔다
    return dg.AssetCheckResult(passed=bad == 0, severity=dg.AssetCheckSeverity.WARN, metadata={"bad_rows": bad})


@dg.asset(
    group_name="dart",
    partitions_def=report_detail_partitions,
    deps=[target_corps],
    description="(Phase 2) 한 회사의 한 정기보고서 주요정보 4종 원본 저장. 목록에 보고서가 있을 때만 API 4건.",
)
def report_details(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """disclosure 목록에 그 보고서가 있으면 주요정보 4종을 받아 원본으로 저장한다.

    목록에 없으면(사업보고서를 내지 않는 자회사, 아직 접수 전) API를 부르지 않고 건너뛴다.
    그래서 전체 파티션을 백필해도 호출 수는 실제 보고서 수 × 4를 넘지 않는다.
    정정본이 접수되면 같은 파티션을 다시 돌린다. 응답의 rcept_no가 바뀌어 원본이 따로 남는다 (collect_report_details).
    """
    keys = context.partition_key.keys_by_dimension
    corp_code, report = keys["corp"], keys["report"]
    bsns_year, reprt_code = split_report_partition_key(report)

    with write_connection() as con:
        corp_name = corp_name_of(con, corp_code)
        base = {"corp_code": corp_code, "corp_name": corp_name, "report": report}
        listed = latest_periodic_report(con, corp_code, bsns_year, reprt_code)
        if listed is None:
            context.log.info(f"{corp_name}({corp_code}) {report}: 공시 목록에 보고서가 없어 건너뜀")
            return dg.MaterializeResult(metadata={**base, "skipped": "공시 목록에 보고서 없음", "api_calls": 0})
        context.log.info(f"{corp_name}({corp_code}) {listed['report_nm']} ({listed['rcept_no']}) 주요정보 수집")
        stats = collect_report_details(con, get_api_key(), corp_code, bsns_year, reprt_code)
    # 응답의 rcept_no가 목록의 최신 접수번호와 다르면 목록이 오래됐거나(정정본이 아직 목록에 없음) API가 다른 버전을 준 것
    return dg.MaterializeResult(
        metadata={**base, LISTED_RCEPT_NO_METADATA_KEY: listed["rcept_no"], "listed_report_nm": listed["report_nm"], **stats,
                  "rcept_no_matches_listing": stats["rcept_no"] == listed["rcept_no"]}
    )


@dg.asset(
    group_name="dart",
    deps=[report_details, holding_investments],
    description="(Phase 2) 원본 전체에서 관계 테이블(investment·최대주주·변동·임원·person)을 다시 만들고 시간을 닫는다. API 0건.",
)
def relations() -> dg.MaterializeResult:
    with write_connection() as con:
        inv = rebuild_investments(con)
        rel = rebuild_relations(con)
    return dg.MaterializeResult(metadata=relations_metadata(inv, rel))


def relations_metadata(inv: dict, rel: dict) -> dict:
    """relations 자산과 backfill_reports가 같은 모양으로 남긴다."""
    return {"investment": inv["rows"], "investment_superseded": inv["investment_superseded"], **to_metadata(rel)}


CHANGE_REPORT_DAYS = 7  # 변화 리포트가 돌아보는 접수 기간. 노트북이 며칠 꺼져 있어도 놓치지 않을 만큼


@dg.asset(
    group_name="dart",
    deps=[relations],
    description="(Phase 3, 0012) 최근 7일 접수된 정기보고서가 임원·최대주주·출자에 만든 변화를 markdown으로. API 0건.",
)
def change_report(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """관계 테이블의 스냅샷 차이를 읽기만 하므로 읽기 전용 연결을 쓴다. 같은 run의 relations가 쓰기 연결을 닫은 뒤에 실행된다."""
    until = today_kst()
    since = until - timedelta(days=CHANGE_REPORT_DAYS)
    with connect(read_only=True) as con:
        results = build_report(con, since, until)
    path = write_report(render_markdown(results, since, until), until)
    s = summarize(results)
    context.log.info(f"{path}: 보고서 {s['reports']}개, 변화 {s['changes']}건")
    return dg.MaterializeResult(metadata={"period": f"{since}~{until}", "path": str(path), **to_metadata(s)})


RELATION_TABLES = ("major_shareholder", "major_shareholder_change", "executive")


def check_relations_source_known(con: duckdb.DuckDBPyConnection) -> dg.AssetCheckResult:
    # 주요정보 응답의 rcept_no는 최신 정정본이다. 정정 공시가 접수된 날 disclosures보다 먼저 돌면 잠깐 어긋날 수 있어 경고로 둔다
    missing = {
        t: con.execute(f"SELECT count(DISTINCT source_rcept_no) FROM {t} r ANTI JOIN disclosure d ON d.rcept_no = r.source_rcept_no").fetchone()[0]
        for t in RELATION_TABLES
    }
    return dg.AssetCheckResult(passed=sum(missing.values()) == 0, severity=dg.AssetCheckSeverity.WARN,
                               metadata={"unknown_sources": dg.MetadataValue.json(missing)})


def check_relations_valid_from_sane(con: duckdb.DuckDBPyConnection) -> dg.AssetCheckResult:
    # 스냅샷 테이블은 valid_from = 기준일이라 기준일보다 늦을 수 없다. 변동 테이블은 valid_from = 변동일이고,
    # 사업보고서는 기준일 석 달 뒤에 내므로 그 사이 변동(예: 기준일 2021-12-31, 변동 2022-01-27)이 실린다 → 있는지만 본다
    bad = {
        t: con.execute(f"SELECT count(*) FROM {t} WHERE valid_from IS NULL OR (stlm_dt IS NOT NULL AND valid_from > stlm_dt)").fetchone()[0]
        for t in ("major_shareholder", "executive")
    }
    bad["major_shareholder_change"] = con.execute("SELECT count(*) FROM major_shareholder_change WHERE valid_from IS NULL").fetchone()[0]
    return dg.AssetCheckResult(passed=sum(bad.values()) == 0, metadata={"bad_rows": dg.MetadataValue.json(bad)})


def check_relations_intervals_sane(con: duckdb.DuckDBPyConnection) -> dg.AssetCheckResult:
    bad = {
        t: con.execute(f"SELECT count(*) FROM {t} WHERE (valid_to IS NOT NULL AND valid_to <= valid_from) "
                       f"OR (superseded_by IS NOT NULL AND valid_to IS NOT NULL)").fetchone()[0]
        for t in (*RELATION_TABLES, "investment")
    }
    return dg.AssetCheckResult(passed=sum(bad.values()) == 0, metadata={"bad_rows": dg.MetadataValue.json(bad)})


def check_relations_current_unique(con: duckdb.DuckDBPyConnection) -> dg.AssetCheckResult:
    dup = {
        "executive": con.execute("SELECT count(*) FROM (SELECT corp_code, person_id, ofcps FROM executive_current "
                                 "GROUP BY 1, 2, 3 HAVING count(*) > 1)").fetchone()[0],
        "major_shareholder": con.execute("SELECT count(*) FROM (SELECT corp_code, nm, stock_knd FROM major_shareholder_current "
                                         "GROUP BY 1, 2, 3 HAVING count(*) > 1)").fetchone()[0],
        # 현재 행은 회사마다 가장 최근 보고서 하나에서만 나와야 한다
        "reports_per_corp": con.execute("SELECT count(*) FROM (SELECT corp_code FROM major_shareholder_current "
                                        "GROUP BY 1 HAVING count(DISTINCT stlm_dt) > 1)").fetchone()[0],
    }
    return dg.AssetCheckResult(passed=sum(dup.values()) == 0, severity=dg.AssetCheckSeverity.WARN,
                               metadata={"duplicates": dg.MetadataValue.json(dup)})


# 검사 본체는 연결을 받는 일반 함수로 두고, Dagster 검사는 읽기 전용 연결을 열어 호출만 한다.
# 백필 스크립트는 쓰기 연결을 이미 열고 있어 같은 프로세스에서 읽기 전용 연결을 또 열 수 없으므로
# (DuckDB: "different configuration than existing connections") 이 목록을 돌며 본체 함수를 자기 연결로 호출한다.
RELATION_CHECK_SPECS = (
    ("relations_source_known", "관계 행의 근거 공시(source_rcept_no)가 disclosure 목록에 있는지", check_relations_source_known),
    ("relations_valid_from_sane", "모든 관계 행에 valid_from이 있고, 스냅샷 행은 보고서 기준일보다 늦지 않은지 (0005)", check_relations_valid_from_sane),
    ("relations_intervals_sane", "닫힌 구간이 거꾸로가 아니고(valid_to > valid_from), 정정된 행은 열려 있지 않은지", check_relations_intervals_sane),
    ("relations_current_unique", "현재 상태 뷰에서 같은 키의 행이 하나뿐인지 (시점 조회가 행 하나를 돌려주는 전제)", check_relations_current_unique),
)


def _relation_check(name: str, description: str, fn):
    @dg.asset_check(asset=relations, name=name, description=description)
    def check() -> dg.AssetCheckResult:
        with connect(read_only=True) as con:
            return fn(con)
    return check


RELATION_CHECKS = tuple(_relation_check(*spec) for spec in RELATION_CHECK_SPECS)
relations_source_known, relations_valid_from_sane, relations_intervals_sane, relations_current_unique = RELATION_CHECKS


# 스케줄 7개는 코드에서 기본 켜짐: dagster dev를 켜는 순간 함께 켜지고 새 환경에서도 같다. 화면에서 끄면 꺼진 상태가 유지된다.
# 시각은 노트북을 켜는 시각(보통 09:30) 뒤인 10:00~12:00에 순서대로: 10:00 대상 → 10:05 공시 목록(59 run, 10분쯤) → 10:45 주요정보 → 11:00 원문
# → 11:30 관계 테이블 → 11:45 그래프 → 12:00 색인. 노트북을 그보다 늦게 켜면 "따라잡기"(놓친 평가 중 가장 최근 하나)로 7개가 같은 시각에 평가되어
# 뒤 단계가 앞 단계의 그날 결과를 못 보고 다음 날 따라잡는다(하루 지연). 왜 기본 켜짐인지와 첫 가동 경과는 docs/devlog/2026-11-27.md "매일 운영 첫 가동".
SCHEDULE_DEFAULT = dg.DefaultScheduleStatus.RUNNING

targets_job = dg.define_asset_job(
    "targets_job", selection=dg.AssetSelection.assets(corp_codes, holding_investments, target_corps)
)
disclosures_job = dg.define_asset_job("disclosures_job", selection=dg.AssetSelection.assets(disclosures))
report_details_job = dg.define_asset_job("report_details_job", selection=dg.AssetSelection.assets(report_details))
relations_job = dg.define_asset_job("relations_job", selection=dg.AssetSelection.assets(relations, change_report))


@dg.schedule(job=targets_job, cron_schedule="0 10 * * *", execution_timezone="Asia/Seoul", default_status=SCHEDULE_DEFAULT)
def targets_freshness(context: dg.ScheduleEvaluationContext):
    """매일 10:00(KST), 수집 대상이 최신 사업보고서 기준인지 확인하고 아니면 targets_job을 실행한다.

    예전에는 매년 4월 15일 한 번만 돌았다(yearly_targets). Dagster는 놓친 스케줄을 따라잡지 않아서
    그날 노트북이 꺼져 있으면 1년 동안 작년 목록으로 수집하게 된다. 그래서 매일 확인하되,
    실제 실행은 기준 연도가 바뀌었을 때만 한다 (보통 4월 1일, 1년에 한 번, API 5건).

    - 실행 기록이 없으면(새 환경) 바로 실행한다
    - 4월 초에 아직 사업보고서를 내지 않은 지주가 있으면 target_corps가 실패하고(IncompleteTargetsError)
      기존 목록이 유지된다. run_key에 날짜가 들어 있어 다음 날 다시 시도한다
    - DuckDB는 열지 않는다 (CLAUDE.md 아키텍처 원칙)
    """
    today = context.scheduled_execution_time.astimezone(KST).date()
    wanted = latest_business_report_year(today)
    current = latest_target_bsns_year(context.instance)
    if current is not None and current >= wanted:
        yield dg.SkipReason(f"수집 대상이 이미 {current} 사업보고서 기준입니다.")
        return
    context.log.info(f"수집 대상 기준 연도 {current} → {wanted} 갱신 요청")
    yield dg.RunRequest(run_key=f"targets|{wanted}|{today.isoformat()}")


@dg.schedule(job=disclosures_job, cron_schedule="5 10 * * *", execution_timezone="Asia/Seoul", default_status=SCHEDULE_DEFAULT)
def daily_disclosures(context: dg.ScheduleEvaluationContext):
    """매일 10:05(KST) 이번 달 파티션을 대상 회사별로 다시 수집한다.

    매달 1~7일에는 지난달도 함께 다시 수집한다. 지난달 마지막 날 저녁에 올라온 공시나,
    노트북이 며칠 꺼져 있던 사이에 빠진 공시를 채우기 위해서다.
    일주일 넘게 꺼져 있었다면 Dagster 화면에서 해당 월 파티션을 직접 백필한다.

    API 호출: 하루 대상 회사 수만큼 (59곳 기준 59건, 1~7일에는 118건)

    DuckDB는 열지 않는다. 대상 목록은 target_corps의 최근 실행 메타데이터에서 읽는다
    (이유는 target_codes_from_instance 참고).
    """
    codes = target_codes_from_instance(context.instance)
    if codes is None:
        yield dg.SkipReason(
            "target_corps 실행 기록에 대상 목록(target_codes)이 없습니다. target_corps 또는 targets_job을 실행하세요."
        )
        return
    today = context.scheduled_execution_time.astimezone(KST).date()
    this_month = today.replace(day=1)
    months = [this_month]
    if today.day <= 7:
        months.append((this_month - timedelta(days=1)).replace(day=1))
    # 파티션에는 있지만 대상에서 빠진 회사(자회사 매각 등)는 매일 수집하지 않는다
    targets = set(codes) & set(context.instance.get_dynamic_partitions("corp"))
    for month in months:
        for corp_code in sorted(targets):
            yield dg.RunRequest(
                run_key=f"{today.isoformat()}|{month.isoformat()}|{corp_code}",
                partition_key=dg.MultiPartitionKey({"month": month.isoformat(), "corp": corp_code}),
            )


@dg.schedule(job=report_details_job, cron_schedule="45 10 * * *", execution_timezone="Asia/Seoul", default_status=SCHEDULE_DEFAULT)
def daily_report_details(context: dg.ScheduleEvaluationContext):
    """매일 10:45(KST), 최근 disclosures 실행이 본 정기보고서(정정본 포함) 중 아직 그 접수번호로 처리하지 않은 것의 주요정보를 수집한다.

    10:05 daily_disclosures가 남긴 실행 메타데이터(periodic_reports: 보고서 키 → 최신 접수번호)를 Dagster 인스턴스에서 읽고,
    report_details 파티션의 마지막 실행 기록(listed_rcept_no)과 견준다. DuckDB는 열지 않는다.
    새 접수번호가 없는 날은 건너뛰므로 평소 API 0건, 보고서 접수 시기에 새 보고서당 4건.
    """
    yield from _runs_for_new_periodic_reports(context, report_details.key, run_prefix="")


def _runs_for_new_periodic_reports(context: dg.ScheduleEvaluationContext, asset_key: dg.AssetKey, run_prefix: str):
    """최근 disclosures 실행 메타데이터(periodic_reports)의 정기보고서 중, 그 자산(asset_key)이 아직 그 접수번호로 돌지 않은 것만 회사 × 보고서 run으로.

    report_details(주요정보)와 documents(원문)가 같은 파티션을 쓰므로 둘이 공유한다. run_key 앞에 run_prefix를 붙여 두 스케줄의 run이 구분된다.
    "이미 처리했다"의 판단: 그 파티션의 마지막 자산 실행 기록 메타데이터 `listed_rcept_no`가 목록의 최신 접수번호와 같으면 건너뛴다.
    기록이 없으면(새 보고서, 또는 어제 run이 실패) 돌리고, 접수번호가 다르면(정정본) 다시 돌린다. DuckDB는 열지 않는다.
    옛 메타데이터(접수번호 없는 키 목록)는 접수번호를 모르니 돌린다. 접수번호로 거르는 이유는 devlog 2026-11-27(매일 116 호출이 헛돌던 문제).
    """
    now = context.scheduled_execution_time
    today = now.astimezone(KST).date()
    wanted = wanted_periodic_reports(recent_materializations(context.instance, disclosures.key, now))
    if not wanted:
        yield dg.SkipReason(f"최근 {LOOKBACK_DAYS}일 공시 수집에서 새 정기보고서가 없습니다.")
        return
    known_corps = set(context.instance.get_dynamic_partitions("corp"))
    known_reports = set(report_partitions.get_partition_keys())
    done = 0
    for (corp_code, report), rcept_no in sorted(wanted.items()):
        if corp_code not in known_corps or report not in known_reports:
            context.log.warning(f"파티션에 없는 조합 건너뜀: {corp_code} {report}")
            continue
        if rcept_no is not None and partition_metadata(context.instance, asset_key, f"{corp_code}|{report}", LISTED_RCEPT_NO_METADATA_KEY) == rcept_no:
            done += 1
            continue
        yield dg.RunRequest(
            run_key=f"{run_prefix}{today.isoformat()}|{corp_code}|{report}",
            partition_key=dg.MultiPartitionKey({"corp": corp_code, "report": report}),
        )
    if done == len(wanted):
        yield dg.SkipReason(f"정기보고서 {done}개 모두 최신 접수번호로 이미 처리됐습니다.")


def wanted_periodic_reports(records: list) -> dict[tuple[str, str], str | None]:
    """disclosures 실행 기록들의 periodic_reports 메타데이터를 합쳐 {(corp_code, 보고서 키): 최신 접수번호}. 순수 함수(인스턴스를 안 본다).
    같은 조합이 여러 기록에 있으면 가장 큰 접수번호(= 최신 정정본). 옛 형식(접수번호 없는 키 목록)은 None — 접수번호를 모르니 돌린다."""
    wanted: dict[tuple[str, str], str | None] = {}
    for record in records:
        meta = record.asset_materialization.metadata
        corp = meta.get("corp_code")
        reports = meta.get(PERIODIC_REPORTS_METADATA_KEY)
        if corp is None or reports is None:
            continue
        value = reports.value
        items = value.items() if isinstance(value, dict) else ((r, None) for r in value)
        for report, rcept_no in items:
            key = (corp.value, report)
            if wanted.get(key) is None or (rcept_no or "") > (wanted[key] or ""):
                wanted[key] = rcept_no
    return wanted


def _schedule_after(name: str, job, cron: str, trigger_asset, run_prefix: str, doc: str):
    """\"자산 A가 최근 LOOKBACK_DAYS일 안에 돌았으면 job J를 하루 한 번\" 꼴의 스케줄. daily_relations·daily_graph가 같은 모양이다. DuckDB는 열지 않는다."""
    @dg.schedule(name=name, job=job, cron_schedule=cron, execution_timezone="Asia/Seoul", default_status=SCHEDULE_DEFAULT, description=doc)
    def schedule(context: dg.ScheduleEvaluationContext):
        now = context.scheduled_execution_time
        today = now.astimezone(KST).date()
        if not recent_materializations(context.instance, trigger_asset.key, now):
            yield dg.SkipReason(f"최근 {LOOKBACK_DAYS}일에 {trigger_asset.key.to_user_string()} 실행이 없습니다.")
            return
        yield dg.RunRequest(run_key=f"{run_prefix}|{today.isoformat()}")
    return schedule


daily_relations = _schedule_after("daily_relations", relations_job, "30 11 * * *", report_details, "relations",
                                  "매일 11:30(KST), 최근에 report_details가 실행됐으면 관계 테이블을 다시 만든다 (API 0건).")


# ───────────────────────────── Phase 3: 공시 원문 RAG 색인 ─────────────────────────────
# 임베딩 모델·청킹 전략은 결정 문서(0008 section_path 1,000자, 0009 kure-v3 fp16 배치 8)를 따른다.
# 바꾸려면 여기 상수와 결정 문서를 함께 고친다. 모델을 바꾸면 전체를 다시 임베딩한다 (26만 조각 ≈ 3시간 반, 2026-11-25 측정)
RAG_MODEL = DEFAULT_MODEL
RAG_STRATEGY = DEFAULT_STRATEGY
RAG_EMBED_BATCH = 8
NEO4J_READY_TIMEOUT_SEC = 30  # 컨테이너가 떠 있으면 바로 연결된다. 꺼져 있으면 이만큼 기다렸다 실패한다
@dg.asset(
    group_name="rag",
    partitions_def=report_detail_partitions,
    deps=[target_corps],
    description="(Phase 3) 한 회사의 한 정기보고서 원문 ZIP 저장(정정본 각각). 아직 없는 접수번호만 받으므로 보통 API 0~1건.",
)
def documents(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    """disclosure 목록에서 이 보고서의 접수번호(정정본 포함)를 찾아, document 테이블에 없는 것만 받는다.

    완료 기록은 Dagster가 아니라 DuckDB document 테이블이다 (backfill_documents 스크립트와 같은 판단).
    그래서 스크립트로 백필한 보고서는 이 자산이 돌아도 API를 부르지 않는다.
    파싱은 여기서 하지 않는다 — ZIP을 지키는 일과 파싱 규칙(0007)은 수명이 다르다. 파싱은 chunks 자산이 한다.
    """
    keys = context.partition_key.keys_by_dimension
    corp_code, report = keys["corp"], keys["report"]
    bsns_year, reprt_code = split_report_partition_key(report)
    with write_connection() as con:
        listed = latest_periodic_report(con, corp_code, bsns_year, reprt_code)
        base = {"corp_code": corp_code, "report": report}
        if listed:
            base[LISTED_RCEPT_NO_METADATA_KEY] = listed["rcept_no"]   # daily_documents 스케줄이 "이 접수번호는 처리했다"로 읽는다
        todo = missing_documents_of_report(con, corp_code, bsns_year, reprt_code)
        if not todo:
            return dg.MaterializeResult(metadata={**base, "api_calls": 0, DOCUMENTS_SAVED_METADATA_KEY: 0, "skipped": "받을 원문 없음 (목록에 없거나 이미 저장)"})
        saved, no_file, api_calls = [], [], 0
        for r in todo:
            context.log.info(f"{corp_code} {r['report_nm']} ({r['rcept_no']}) 원문 수신")
            result = collect_document(con, get_api_key(), r["rcept_no"])
            api_calls += result["api_calls"]
            (no_file if result.get("no_file") else saved).append(r["rcept_no"])
    return dg.MaterializeResult(metadata={**base, "api_calls": api_calls, DOCUMENTS_SAVED_METADATA_KEY: len(saved),
                                          "saved_rcept_no": ", ".join(saved) or "없음", "no_file": ", ".join(no_file) or "없음"})


@dg.asset_check(asset=documents, description="대상 회사의 정기보고서(2021~) 중 원문이 없는 것이 몇 개인지")
def documents_complete() -> dg.AssetCheckResult:
    # 범위를 사업보고서만으로 줄여 둔 동안은 계속 경고가 뜬다. 채우려면 python -m src.pipeline.backfill_documents
    with connect(read_only=True) as con:
        missing = missing_documents(con, target_corp_codes(con), FIRST_REPORT_YEAR)
    by_kind: dict[str, int] = {}
    for r in missing:
        by_kind[r["reprt_code"]] = by_kind.get(r["reprt_code"], 0) + 1
    return dg.AssetCheckResult(passed=not missing, severity=dg.AssetCheckSeverity.WARN,
                               metadata={"missing": len(missing), "missing_by_reprt_code": dg.MetadataValue.json(by_kind)})


class ChunksConfig(dg.Config):
    # 청킹 규칙(0008)을 고쳤을 때 true. 조각을 전부 다시 만들고, 본문 해시가 바뀐 조각은 chunk_embeddings가 다시 임베딩한다 (오래 걸림)
    full_rebuild: bool = False


@dg.asset(
    group_name="rag",
    deps=[documents],
    description="(Phase 3) 새 원문을 파싱하고 조각이 없는 보고서만 자른다. full_rebuild=true면 전체 다시. API 0건.",
)
def chunks(context: dg.AssetExecutionContext, config: ChunksConfig) -> dg.MaterializeResult:
    with write_connection() as con:
        to_parse = unparsed_documents(con)
        parse = rebuild_sections(con, to_parse)                 # 빈 목록이면 0만 돌려준다
        rcept_nos = parsed_rcept_nos(con, None) if config.full_rebuild else unchunked_rcept_nos(con, RAG_STRATEGY)
        context.log.info(f"파싱 {len(to_parse)}개, 자를 보고서 {len(rcept_nos)}개 ({'전체' if config.full_rebuild else '조각 없는 것만'})")
        built = build_chunks(con, rcept_nos, [RAG_STRATEGY])    # 빈 목록이면 0만 돌려준다
        total = con.execute("SELECT count(*) FROM chunk WHERE strategy = ?", [RAG_STRATEGY]).fetchone()[0]
    return dg.MaterializeResult(metadata={"strategy": RAG_STRATEGY, "parsed_documents": len(to_parse), "parsed_files": parse["files"],
                                          "parse_failed_files": parse["files_failed"], "chunked_documents": len(rcept_nos),
                                          "new_chunks": built["chunks"][RAG_STRATEGY], "no_sections": len(built["no_sections"]),
                                          "total_chunks": total})


@dg.asset_check(asset=chunks, description="조각 본문이 비어 있지 않은지")
def chunks_nonempty() -> dg.AssetCheckResult:
    with connect(read_only=True) as con:
        empty = con.execute("SELECT count(*) FROM chunk WHERE strategy = ? AND (trim(text) = '' OR trim(embed_text) = '')",
                            [RAG_STRATEGY]).fetchone()[0]
    return dg.AssetCheckResult(passed=empty == 0, metadata={"empty_chunks": empty})


@dg.asset_check(asset=chunks, description="파싱된 보고서마다 조각이 있는지")
def chunks_cover_parsed() -> dg.AssetCheckResult:
    with connect(read_only=True) as con:
        missing = unchunked_rcept_nos(con, RAG_STRATEGY)
    return dg.AssetCheckResult(passed=not missing, severity=dg.AssetCheckSeverity.WARN,
                               metadata={"parsed_without_chunks": len(missing), "examples": ", ".join(missing[:5]) or "없음"})


def make_embedder():
    """임베딩 모델을 연다. 모델 로딩(torch, 2GB)은 비싸므로 임베딩할 조각이 있을 때만 부른다. 테스트는 이 함수를 가짜로 바꾼다."""
    from src.rag.embedding import LocalEmbedder  # torch·sentence-transformers는 노트북에만 설치(requirements-ml.txt)

    return LocalEmbedder(RAG_MODEL, fp16=True)


@dg.asset(
    group_name="rag",
    deps=[chunks],
    description="(Phase 3) 벡터가 없거나 해시가 바뀐 조각만 임베딩(노트북 GPU). API 0건.",
)
def chunk_embeddings(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    with write_connection() as con:
        have, pending = pending_count(con, RAG_MODEL, RAG_STRATEGY)
        if pending == 0:
            return dg.MaterializeResult(metadata={"model": RAG_MODEL, "pending": 0, "embedded": 0, "total": have})
        context.log.info(f"임베딩할 조각 {pending:,}개 (1,000개당 약 55초)")
        stats = embed_chunks(con, make_embedder(), RAG_STRATEGY, batch_size=RAG_EMBED_BATCH, log=context.log.info)
        have, pending = pending_count(con, RAG_MODEL, RAG_STRATEGY)
    return dg.MaterializeResult(metadata={**{k: v for k, v in stats.items() if v is not None}, "total": have, "still_pending": pending})


@dg.asset_check(asset=chunk_embeddings, description="벡터 없는(또는 본문이 바뀐) 조각이 0개인지")
def chunk_embeddings_complete() -> dg.AssetCheckResult:
    with connect(read_only=True) as con:
        have, pending = pending_count(con, RAG_MODEL, RAG_STRATEGY)
    return dg.AssetCheckResult(passed=pending == 0, metadata={"embedded": have, "pending": pending})


@dg.asset(
    group_name="rag",
    deps=[chunk_embeddings],
    description="(Phase 3) 조각 수가 다른 보고서만 Neo4j에 MERGE. Neo4j가 꺼져 있으면 실패 → 다음 날 다시.",
)
def neo4j_chunks(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    # Neo4j를 먼저 연다. 꺼져 있으면 DuckDB를 건드리지 않고 바로 실패한다
    with graph_connect() as driver:
        wait_until_ready(driver, timeout=NEO4J_READY_TIMEOUT_SEC, log=context.log.info)
        with write_connection() as con:
            todo = stale_documents(con, RAG_MODEL, RAG_STRATEGY, driver)
            if not todo:
                counts = neo4j_chunk_stats(con, driver, RAG_MODEL, RAG_STRATEGY)
                return dg.MaterializeResult(metadata={"documents": 0, "chunks": 0, "neo4j_chunks": counts["neo4j"]["chunks"],
                                                      "skipped": "Neo4j와 DuckDB의 조각 수가 모두 같음"})
            context.log.info(f"Neo4j에 넣을 보고서 {len(todo)}개")
            stats = build_neo4j_chunks(con, driver, RAG_MODEL, RAG_STRATEGY, rcept_nos=todo, log=context.log.info)
            counts = neo4j_chunk_stats(con, driver, RAG_MODEL, RAG_STRATEGY)
    return dg.MaterializeResult(metadata={**stats, "neo4j_chunks": counts["neo4j"]["chunks"],
                                          "duckdb_chunks_with_embedding": counts["duckdb"]["chunks_with_embedding"]})


@dg.asset_check(asset=neo4j_chunks, description="Neo4j 조각 수 = DuckDB의 벡터 있는 조각 수인지 (Neo4j가 꺼져 있으면 경고)")
def neo4j_chunks_match() -> dg.AssetCheckResult:
    from neo4j.exceptions import ServiceUnavailable

    try:
        with graph_connect() as driver, connect(read_only=True) as con:
            driver.verify_connectivity()
            s = neo4j_chunk_stats(con, driver, RAG_MODEL, RAG_STRATEGY)
    except (ServiceUnavailable, OSError) as e:
        return dg.AssetCheckResult(passed=False, severity=dg.AssetCheckSeverity.WARN, metadata={"error": f"Neo4j에 연결하지 못함: {type(e).__name__}"})
    want, have = s["duckdb"]["chunks_with_embedding"], s["neo4j"]["chunks"]
    return dg.AssetCheckResult(passed=want == have, severity=dg.AssetCheckSeverity.WARN,
                               metadata={"duckdb": want, "neo4j": have, "indexes": dg.MetadataValue.json(s["neo4j"]["indexes"])})


@dg.asset(
    group_name="graph",
    deps=[relations],
    description="(Phase 4, 0011) 관계 테이블 → Neo4j 그래프. 관계 층만 지우고 다시 넣는다(조각 층은 그대로). Neo4j가 꺼져 있으면 실패.",
)
def neo4j_relations(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    with graph_connect() as driver:
        wait_until_ready(driver, timeout=NEO4J_READY_TIMEOUT_SEC, log=context.log.info)
        with write_connection() as con:
            out = build_neo4j_relations(con, driver, log=context.log.info)
    return dg.MaterializeResult(metadata=out)


@dg.asset_check(asset=neo4j_relations, description="Neo4j OWNS·HOLDS_POSITION·FORMERLY 수 = DuckDB 행 수인지 (Neo4j가 꺼져 있으면 경고)")
def neo4j_relations_match() -> dg.AssetCheckResult:
    from neo4j.exceptions import ServiceUnavailable

    try:
        with graph_connect() as driver, connect(read_only=True) as con:
            driver.verify_connectivity()
            s = neo4j_relation_stats(con, driver)
    except (ServiceUnavailable, OSError) as e:
        return dg.AssetCheckResult(passed=False, severity=dg.AssetCheckSeverity.WARN, metadata={"error": f"Neo4j에 연결하지 못함: {type(e).__name__}"})
    return dg.AssetCheckResult(passed=s["match"], metadata={"duckdb": dg.MetadataValue.json(s["duckdb"]), "neo4j": dg.MetadataValue.json(s["neo4j"])})


RAG_CHECKS = (documents_complete, chunks_nonempty, chunks_cover_parsed, chunk_embeddings_complete, neo4j_chunks_match)

documents_job = dg.define_asset_job("documents_job", selection=dg.AssetSelection.assets(documents))
rag_index_job = dg.define_asset_job("rag_index_job", selection=dg.AssetSelection.assets(chunks, chunk_embeddings, neo4j_chunks))
graph_job = dg.define_asset_job("graph_job", selection=dg.AssetSelection.assets(neo4j_relations))


@dg.schedule(job=documents_job, cron_schedule="0 11 * * *", execution_timezone="Asia/Seoul", default_status=SCHEDULE_DEFAULT)
def daily_documents(context: dg.ScheduleEvaluationContext):
    """매일 11:00(KST), 최근 disclosures가 새로 본 정기보고서(정정본 포함)의 원문을 받는다. daily_report_details와 같은 입력.

    DuckDB는 열지 않는다. 평소 API 0건, 보고서 접수 시기에 보고서당 1건.
    """
    yield from _runs_for_new_periodic_reports(context, documents.key, run_prefix="documents|")


@dg.schedule(job=rag_index_job, cron_schedule="0 12 * * *", execution_timezone="Asia/Seoul", default_status=SCHEDULE_DEFAULT)
def daily_rag_index(context: dg.ScheduleEvaluationContext):
    """매일 12:00(KST), 색인(chunks → chunk_embeddings → neo4j_chunks)을 돌릴 이유가 있으면 rag_index_job을 실행한다.

    이유 둘 중 하나:
    1) 최근 documents 실행이 새 원문을 저장했다 (메타데이터 saved > 0)
    2) 지난 색인이 끝까지 가지 못했다 — neo4j_chunks의 마지막 성공이 chunk_embeddings보다 오래됐다
       (Neo4j 컨테이너가 꺼져 있어 마지막 단계만 실패한 경우. 다음 날 다시 시도한다)
    DuckDB는 열지 않는다.
    """
    now = context.scheduled_execution_time
    today = now.astimezone(KST).date()
    saved = sum_recent_metadata(context.instance, documents.key, now, DOCUMENTS_SAVED_METADATA_KEY)   # 새로 저장한 원문 수
    embedded_at = latest_materialization_time(context.instance, chunk_embeddings.key)
    indexed_at = latest_materialization_time(context.instance, neo4j_chunks.key)
    unfinished = embedded_at is not None and (indexed_at is None or indexed_at < embedded_at)
    if saved == 0 and not unfinished:
        yield dg.SkipReason(f"최근 {LOOKBACK_DAYS}일에 새 원문이 없고 지난 색인도 끝까지 갔습니다.")
        return
    context.log.info(f"색인 실행: 새 원문 {saved}개, 지난 색인 미완료 {unfinished}")
    yield dg.RunRequest(run_key=f"rag_index|{today.isoformat()}")


# 11:30 관계 테이블이 바뀌면 그래프도 같은 날 따라가야 그래프 질문이 옛 답을 하지 않는다 (조각 층은 그대로)
daily_graph = _schedule_after("daily_graph", graph_job, "45 11 * * *", relations, "graph",
                              "매일 11:45(KST), 최근에 relations(관계 테이블)가 다시 만들어졌으면 Neo4j 관계 그래프를 다시 올린다 (API 0건).")


defs = dg.Definitions(
    assets=[corp_codes, holding_investments, target_corps, disclosures, report_details, relations, change_report,
            documents, chunks, chunk_embeddings, neo4j_chunks, neo4j_relations],
    asset_checks=[target_corps_complete, disclosure_corp_code_exists, disclosure_rcept_no_format, *RELATION_CHECKS, *RAG_CHECKS,
                  neo4j_relations_match],
    jobs=[targets_job, disclosures_job, report_details_job, relations_job, documents_job, rag_index_job, graph_job],
    schedules=[targets_freshness, daily_disclosures, daily_report_details, daily_relations, daily_documents, daily_rag_index, daily_graph],
    # 한 run 안에서 자산을 동시에 실행하지 않는다 (DuckDB 쓰기는 한 프로세스만)
    executor=dg.in_process_executor,
)
