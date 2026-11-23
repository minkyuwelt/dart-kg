"""Dagster 정의: 자산(asset), 품질 검사(asset check), 잡(job), 스케줄.

실행 (저장소 루트에서):
    DAGSTER_HOME=$PWD/dagster_home dagster dev -m src.pipeline.definitions
그다음 브라우저에서 http://localhost:3000

자산 구성 (화살표는 의존 관계)
    corp_codes ─▶ holding_investments ─▶ target_corps ─▶ disclosures
                                              └────────▶ report_details ─▶ relations  (Phase 2)

    corp_codes          : corpCode.xml 전체 → corp 테이블 (API 1건)
    holding_investments : 지주 4곳의 최신 사업보고서 타법인 출자현황 → investment 테이블 (API 4건)
    target_corps        : investment에서 수집 대상(지주 + 직속 자회사)을 뽑아 target_corp 테이블에 넣고,
                          새 회사를 disclosures의 회사 파티션에 추가한다 (API 0건)
    disclosures         : list.json → raw_api_response + disclosure 테이블
                          파티션 = 월(month) × 회사(corp). 파티션 하나가 "한 회사의 한 달치 공시"이고 보통 API 1건
    report_details      : (Phase 2) 한 회사의 한 정기보고서에서 주요정보 4종(출자·최대주주·변동·임원) 원본 저장
                          파티션 = 회사(corp) × 보고서(report: '2025-11011' = 사업연도-보고서종류). API 4건.
                          disclosure 목록에 그 보고서가 없으면 API를 부르지 않고 건너뛴다 (0건)
    relations           : (Phase 2) 원본 전체에서 investment·major_shareholder·major_shareholder_change·executive
                          테이블을 다시 만든다. API 0건. 시간 닫기(valid_to)는 아직 없다 (0005)

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

from src.collectors.corp_code import HOLDING_CORPS, get_api_key
from src.loaders.collect import collect_corps, collect_disclosures, collect_investments, collect_report_details
from src.loaders.duckdb_store import connect, is_lock_error
from src.loaders.investments import rebuild_investments
from src.loaders.periodic_reports import (
    latest_periodic_report,
    periodic_reports_of,
    report_partition_key,
    report_partition_keys,
    split_report_partition_key,
)
from src.loaders.relations import rebuild_relations
from src.loaders.targets import latest_business_report_year, rebuild_target_corps

# DuckDB 파일 잠금에 걸렸을 때 다시 시도하는 횟수와 간격(초).
# 노트북에서 DuckDB를 잠깐 열어 둔 경우 몇 분 뒤에는 풀리는 경우가 대부분이다.
LOCK_RETRY_MAX = 3
LOCK_RETRY_WAIT_SEC = 300

KST = ZoneInfo("Asia/Seoul")

# 2021-01부터 이번 달까지. end_offset=1이 있어야 아직 끝나지 않은 이번 달도 파티션에 포함된다.
month_partitions = dg.MonthlyPartitionsDefinition(start_date="2021-01-01", end_offset=1, timezone="Asia/Seoul")
corp_partitions = dg.DynamicPartitionsDefinition(name="corp")
disclosure_partitions = dg.MultiPartitionsDefinition({"month": month_partitions, "corp": corp_partitions})

# Phase 2: 정기보고서 파티션. 2021 사업연도부터 올해까지 연 4개(1분기·반기·3분기·사업). 키는 '2025-11011'.
# 회사 × 보고서 조합 중 실제로 보고서가 있는 것만 API를 부르므로, 없는 조합을 백필해도 호출이 늘지 않는다.
FIRST_REPORT_YEAR = 2021
report_partitions = dg.StaticPartitionsDefinition(report_partition_keys(FIRST_REPORT_YEAR, datetime.now(ZoneInfo("Asia/Seoul")).year))
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


TARGET_CODES_METADATA_KEY = "target_codes"
PERIODIC_REPORTS_METADATA_KEY = "periodic_reports"


def target_codes_from_instance(instance: dg.DagsterInstance) -> list[str] | None:
    """가장 최근 target_corps 실행이 메타데이터로 남긴 수집 대상 corp_code 목록.

    스케줄은 DuckDB를 열지 않고 이 함수로 대상을 읽는다. 스케줄은 Dagster 데몬 프로세스에서
    평가되는데, DuckDB는 다른 프로세스가 쓰는 동안 읽기 전용 연결도 열 수 없다. 그 시각에 백필 같은
    run이 쓰고 있으면 스케줄 평가가 잠금에 걸려 실패하고, 스케줄에는 재시도가 없어 그날 수집이 빠진다.
    Dagster 인스턴스 저장소(dagster_home)는 DuckDB와 별개라 이 문제가 없다.

    target_corps를 한 번도 실행하지 않았거나 메타데이터가 없으면 None.
    """
    event = instance.get_latest_materialization_event(target_corps.key)
    if event is None or event.asset_materialization is None:
        return None
    value = event.asset_materialization.metadata.get(TARGET_CODES_METADATA_KEY)
    return list(value.value) if value is not None else None


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
        row = con.execute("SELECT corp_name FROM corp WHERE corp_code = ?", [corp_code]).fetchone()
        corp_name = row[0] if row else corp_code
        context.log.info(f"{corp_name}({corp_code}) {bgn_de}~{end_de} 공시 목록 수집")
        stats = collect_disclosures(con, get_api_key(), corp_code, bgn_de, end_de)
        # 이 달에 접수된 정기보고서(정정본 포함). daily_report_details 스케줄이 DuckDB를 열지 않고 읽는 곳
        reports = sorted({report_partition_key(r["bsns_year"], r["reprt_code"]) for r in periodic_reports_of(con, corp_code, bgn_de, end_de)})
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
        row = con.execute("SELECT corp_name FROM corp WHERE corp_code = ?", [corp_code]).fetchone()
        corp_name = row[0] if row else corp_code
        listed = latest_periodic_report(con, corp_code, bsns_year, reprt_code)
        if listed is None:
            context.log.info(f"{corp_name}({corp_code}) {report}: 공시 목록에 보고서가 없어 건너뜀")
            return dg.MaterializeResult(metadata={"corp_code": corp_code, "corp_name": corp_name, "report": report,
                                                  "skipped": "공시 목록에 보고서 없음", "api_calls": 0})
        context.log.info(f"{corp_name}({corp_code}) {listed['report_nm']} ({listed['rcept_no']}) 주요정보 수집")
        stats = collect_report_details(con, get_api_key(), corp_code, bsns_year, reprt_code)
    # 응답의 rcept_no가 목록의 최신 접수번호와 다르면 목록이 오래됐거나(정정본이 아직 목록에 없음) API가 다른 버전을 준 것
    return dg.MaterializeResult(
        metadata={"corp_code": corp_code, "corp_name": corp_name, "report": report,
                  "listed_rcept_no": listed["rcept_no"], "listed_report_nm": listed["report_nm"], **stats,
                  "rcept_no_matches_listing": stats["rcept_no"] == listed["rcept_no"]}
    )


@dg.asset(
    group_name="dart",
    deps=[report_details, holding_investments],
    description="(Phase 2) 원본 전체에서 관계 테이블(investment·최대주주·변동·임원)을 다시 만든다. API 0건.",
)
def relations() -> dg.MaterializeResult:
    with write_connection() as con:
        inv = rebuild_investments(con)
        rel = rebuild_relations(con)
    return dg.MaterializeResult(metadata={"investment": inv["rows"], **{k: v for k, v in rel.items() if k != "holder_by_method"},
                                          "holder_by_method": dg.MetadataValue.json(rel["holder_by_method"])})


RELATION_TABLES = ("major_shareholder", "major_shareholder_change", "executive")


@dg.asset_check(asset=relations, description="관계 행의 근거 공시(source_rcept_no)가 disclosure 목록에 있는지")
def relations_source_known() -> dg.AssetCheckResult:
    # 주요정보 응답의 rcept_no는 최신 정정본이다. 정정 공시가 접수된 날 disclosures보다 먼저 돌면 잠깐 어긋날 수 있어 경고로 둔다
    with connect(read_only=True) as con:
        missing = {
            t: con.execute(f"SELECT count(DISTINCT source_rcept_no) FROM {t} r ANTI JOIN disclosure d ON d.rcept_no = r.source_rcept_no").fetchone()[0]
            for t in RELATION_TABLES
        }
    return dg.AssetCheckResult(passed=sum(missing.values()) == 0, severity=dg.AssetCheckSeverity.WARN,
                               metadata={"unknown_sources": dg.MetadataValue.json(missing)})


@dg.asset_check(asset=relations, description="모든 관계 행에 valid_from이 있고 보고서 기준일보다 늦지 않은지 (0005)")
def relations_valid_from_sane() -> dg.AssetCheckResult:
    with connect(read_only=True) as con:
        bad = {
            t: con.execute(f"SELECT count(*) FROM {t} WHERE valid_from IS NULL OR (stlm_dt IS NOT NULL AND valid_from > stlm_dt)").fetchone()[0]
            for t in RELATION_TABLES
        }
    return dg.AssetCheckResult(passed=sum(bad.values()) == 0, metadata={"bad_rows": dg.MetadataValue.json(bad)})


targets_job = dg.define_asset_job(
    "targets_job", selection=dg.AssetSelection.assets(corp_codes, holding_investments, target_corps)
)
disclosures_job = dg.define_asset_job("disclosures_job", selection=dg.AssetSelection.assets(disclosures))
report_details_job = dg.define_asset_job("report_details_job", selection=dg.AssetSelection.assets(report_details))
relations_job = dg.define_asset_job("relations_job", selection=dg.AssetSelection.assets(relations))


def latest_target_bsns_year(instance: dg.DagsterInstance) -> int | None:
    """가장 최근 target_corps 실행이 메타데이터로 남긴 기준 사업연도. 실행 기록이 없으면 None.

    target_codes_from_instance와 같은 이유로 DuckDB를 열지 않고 Dagster 인스턴스에서 읽는다.
    """
    event = instance.get_latest_materialization_event(target_corps.key)
    if event is None or event.asset_materialization is None:
        return None
    value = event.asset_materialization.metadata.get("bsns_year")
    return int(value.value) if value is not None else None


@dg.schedule(job=targets_job, cron_schedule="30 6 * * *", execution_timezone="Asia/Seoul")
def targets_freshness(context: dg.ScheduleEvaluationContext):
    """매일 06:30(KST), 수집 대상이 최신 사업보고서 기준인지 확인하고 아니면 targets_job을 실행한다.

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


@dg.schedule(job=disclosures_job, cron_schedule="0 7 * * *", execution_timezone="Asia/Seoul")
def daily_disclosures(context: dg.ScheduleEvaluationContext):
    """매일 07:00(KST) 이번 달 파티션을 대상 회사별로 다시 수집한다.

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


def _recent_materializations(instance: dg.DagsterInstance, asset_key: dg.AssetKey, since: float) -> list:
    """since(epoch 초) 이후의 자산 실행 기록. 스케줄이 DuckDB 대신 읽는 곳."""
    return list(instance.fetch_materializations(dg.AssetRecordsFilter(asset_key=asset_key, after_timestamp=since), limit=5000).records)


LOOKBACK_DAYS = 2  # 스케줄이 "최근"으로 보는 범위. 하루 꺼져 있어도 다음 날 따라잡는다. run_key가 같은 날 중복을 막는다


@dg.schedule(job=report_details_job, cron_schedule="30 7 * * *", execution_timezone="Asia/Seoul")
def daily_report_details(context: dg.ScheduleEvaluationContext):
    """매일 07:30(KST), 최근 disclosures 실행이 새로 본 정기보고서(정정본 포함)의 주요정보를 수집한다.

    07:00 daily_disclosures가 남긴 실행 메타데이터(periodic_reports)를 Dagster 인스턴스에서 읽는다.
    DuckDB는 열지 않는다. 정기보고서가 없는 날은 건너뛰므로 평소 API 0건, 보고서 접수 시기에 보고서당 4건.
    """
    now = context.scheduled_execution_time
    today = now.astimezone(KST).date()
    wanted: set[tuple[str, str]] = set()
    for record in _recent_materializations(context.instance, disclosures.key, now.timestamp() - LOOKBACK_DAYS * 86400):
        meta = record.asset_materialization.metadata
        corp = meta.get("corp_code")
        reports = meta.get(PERIODIC_REPORTS_METADATA_KEY)
        if corp is None or reports is None:
            continue
        wanted.update((corp.value, r) for r in reports.value)
    if not wanted:
        yield dg.SkipReason(f"최근 {LOOKBACK_DAYS}일 공시 수집에서 새 정기보고서가 없습니다.")
        return
    known = set(context.instance.get_dynamic_partitions("corp"))
    for corp_code, report in sorted(wanted):
        if corp_code not in known or report not in report_partitions.get_partition_keys():
            context.log.warning(f"파티션에 없는 조합 건너뜀: {corp_code} {report}")
            continue
        yield dg.RunRequest(
            run_key=f"{today.isoformat()}|{corp_code}|{report}",
            partition_key=dg.MultiPartitionKey({"corp": corp_code, "report": report}),
        )


@dg.schedule(job=relations_job, cron_schedule="30 8 * * *", execution_timezone="Asia/Seoul")
def daily_relations(context: dg.ScheduleEvaluationContext):
    """매일 08:30(KST), 최근에 report_details가 실행됐으면 관계 테이블을 다시 만든다 (API 0건)."""
    now = context.scheduled_execution_time
    today = now.astimezone(KST).date()
    if not _recent_materializations(context.instance, report_details.key, now.timestamp() - LOOKBACK_DAYS * 86400):
        yield dg.SkipReason(f"최근 {LOOKBACK_DAYS}일에 report_details 실행이 없습니다.")
        return
    yield dg.RunRequest(run_key=f"relations|{today.isoformat()}")


defs = dg.Definitions(
    assets=[corp_codes, holding_investments, target_corps, disclosures, report_details, relations],
    asset_checks=[target_corps_complete, disclosure_corp_code_exists, disclosure_rcept_no_format,
                  relations_source_known, relations_valid_from_sane],
    jobs=[targets_job, disclosures_job, report_details_job, relations_job],
    schedules=[targets_freshness, daily_disclosures, daily_report_details, daily_relations],
    # 한 run 안에서 자산을 동시에 실행하지 않는다 (DuckDB 쓰기는 한 프로세스만)
    executor=dg.in_process_executor,
)
