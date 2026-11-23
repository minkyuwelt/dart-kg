"""Dagster 정의: 자산(asset), 품질 검사(asset check), 잡(job), 스케줄.

실행 (저장소 루트에서):
    DAGSTER_HOME=$PWD/dagster_home dagster dev -m src.pipeline.definitions
그다음 브라우저에서 http://localhost:3000

자산 구성
    corp_codes   : corpCode.xml 전체 → corp 테이블 (파티션 없음, API 1건)
    disclosures  : list.json → raw_api_response + disclosure 테이블
                   파티션 = 월(month) × 회사(corp). 파티션 하나가 "한 회사의 한 달치 공시"이고 보통 API 1건

왜 월 × 회사 파티션인가
    - 일 단위로 나누면 5년 백필에 1,825일 × 4곳 = 7,300건을 호출한다. 월 단위면 60개월 × 4곳 = 240건
    - "하나금융지주 2024년 3월만 다시 수집" 같은 재처리를 파티션 하나로 할 수 있다
    - 적재가 upsert라서 같은 파티션을 몇 번 다시 돌려도 중복이 생기지 않는다

이 파일에는 수집 로직을 두지 않는다. 자산은 src/loaders/collect.py의 함수를 호출만 한다.
"""

import calendar
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import dagster as dg

from src.collectors.corp_code import TARGET_CORPS, get_api_key
from src.loaders.collect import collect_corps, collect_disclosures
from src.loaders.duckdb_store import connect

KST = ZoneInfo("Asia/Seoul")

# 2021-01부터 이번 달까지. end_offset=1이 있어야 아직 끝나지 않은 이번 달도 파티션에 포함된다.
month_partitions = dg.MonthlyPartitionsDefinition(start_date="2021-01-01", end_offset=1, timezone="Asia/Seoul")
corp_partitions = dg.StaticPartitionsDefinition(list(TARGET_CORPS))
disclosure_partitions = dg.MultiPartitionsDefinition({"month": month_partitions, "corp": corp_partitions})


def month_range(month_key: str, today: date) -> tuple[str, str]:
    """월 파티션 키('2026-11-01')를 DART 검색 기간(YYYYMMDD, YYYYMMDD)으로 바꾼다.

    이번 달이면 끝 날짜를 오늘로 자른다.
    """
    start = date.fromisoformat(month_key)
    last_day = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
    end = min(last_day, today)
    return start.strftime("%Y%m%d"), end.strftime("%Y%m%d")


@dg.asset(
    group_name="dart",
    description="공시 대상 전체 회사 목록(corpCode.xml) → corp 테이블. API 1건.",
)
def corp_codes() -> dg.MaterializeResult:
    with connect() as con:
        stats = collect_corps(con, get_api_key())
        found = con.execute(
            "SELECT count(*) FROM corp WHERE corp_code IN (SELECT unnest(?::VARCHAR[]))", [list(TARGET_CORPS)]
        ).fetchone()[0]
    return dg.MaterializeResult(metadata={**stats, "targets_found": f"{found}/{len(TARGET_CORPS)}"})


@dg.asset(
    group_name="dart",
    partitions_def=disclosure_partitions,
    deps=[corp_codes],
    description="한 회사의 한 달치 공시 목록(list.json) → raw_api_response + disclosure 테이블.",
)
def disclosures(context: dg.AssetExecutionContext) -> dg.MaterializeResult:
    keys = context.partition_key.keys_by_dimension
    corp_code, month = keys["corp"], keys["month"]
    bgn_de, end_de = month_range(month, datetime.now(KST).date())
    context.log.info(f"{TARGET_CORPS[corp_code]}({corp_code}) {bgn_de}~{end_de} 공시 목록 수집")

    with connect() as con:
        stats = collect_disclosures(con, get_api_key(), corp_code, bgn_de, end_de)
    return dg.MaterializeResult(
        metadata={"corp_name": TARGET_CORPS[corp_code], "period": f"{bgn_de}~{end_de}", **stats}
    )


@dg.asset_check(asset=disclosures, description="모든 공시의 corp_code가 corp 테이블에 있는지 (회사 식별자 무결성)")
def disclosure_corp_code_exists() -> dg.AssetCheckResult:
    with connect() as con:
        orphans = con.execute(
            "SELECT count(*) FROM disclosure d ANTI JOIN corp c USING (corp_code)"
        ).fetchone()[0]
    return dg.AssetCheckResult(passed=orphans == 0, metadata={"orphan_rows": orphans})


@dg.asset_check(
    asset=disclosures,
    description="rcept_no가 14자리 숫자이고 앞 8자리가 접수일(rcept_dt)과 같은지",
)
def disclosure_rcept_no_format() -> dg.AssetCheckResult:
    with connect() as con:
        bad = con.execute(
            """
            SELECT count(*) FROM disclosure
            WHERE NOT regexp_full_match(rcept_no, '[0-9]{14}')
               OR substr(rcept_no, 1, 8) <> strftime(rcept_dt, '%Y%m%d')
            """
        ).fetchone()[0]
    # 형식이 어긋나도 데이터를 못 쓰는 건 아니므로 실패가 아니라 경고로 둔다
    return dg.AssetCheckResult(passed=bad == 0, severity=dg.AssetCheckSeverity.WARN, metadata={"bad_rows": bad})


disclosures_job = dg.define_asset_job("disclosures_job", selection=dg.AssetSelection.assets(disclosures))


@dg.schedule(job=disclosures_job, cron_schedule="0 7 * * *", execution_timezone="Asia/Seoul")
def daily_disclosures(context: dg.ScheduleEvaluationContext):
    """매일 07:00(KST) 이번 달 파티션을 회사별로 다시 수집한다.

    매달 1~7일에는 지난달도 함께 다시 수집한다. 지난달 마지막 날 저녁에 올라온 공시나,
    노트북이 며칠 꺼져 있던 사이에 빠진 공시를 채우기 위해서다.
    일주일 넘게 꺼져 있었다면 Dagster 화면에서 해당 월 파티션을 직접 백필한다.

    API 호출: 하루 4건 (1~7일에는 8건)
    """
    today = context.scheduled_execution_time.astimezone(KST).date()
    this_month = today.replace(day=1)
    months = [this_month]
    if today.day <= 7:
        months.append((this_month - timedelta(days=1)).replace(day=1))
    for month in months:
        for corp_code in TARGET_CORPS:
            yield dg.RunRequest(
                run_key=f"{today.isoformat()}|{month.isoformat()}|{corp_code}",
                partition_key=dg.MultiPartitionKey({"month": month.isoformat(), "corp": corp_code}),
            )


defs = dg.Definitions(
    assets=[corp_codes, disclosures],
    asset_checks=[disclosure_corp_code_exists, disclosure_rcept_no_format],
    jobs=[disclosures_job],
    schedules=[daily_disclosures],
    # 한 run 안에서 자산을 동시에 실행하지 않는다 (DuckDB 쓰기는 한 프로세스만)
    executor=dg.in_process_executor,
)
