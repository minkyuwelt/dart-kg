"""Dagster 정의: 자산(asset), 품질 검사(asset check), 잡(job), 스케줄.

실행 (저장소 루트에서):
    DAGSTER_HOME=$PWD/dagster_home dagster dev -m src.pipeline.definitions
그다음 브라우저에서 http://localhost:3000

자산 구성 (화살표는 의존 관계)
    corp_codes ─▶ holding_investments ─▶ target_corps ─▶ disclosures

    corp_codes          : corpCode.xml 전체 → corp 테이블 (API 1건)
    holding_investments : 지주 4곳의 최신 사업보고서 타법인 출자현황 → investment 테이블 (API 4건)
    target_corps        : investment에서 수집 대상(지주 + 직속 자회사)을 뽑아 target_corp 테이블에 넣고,
                          새 회사를 disclosures의 회사 파티션에 추가한다 (API 0건)
    disclosures         : list.json → raw_api_response + disclosure 테이블
                          파티션 = 월(month) × 회사(corp). 파티션 하나가 "한 회사의 한 달치 공시"이고 보통 API 1건

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
from src.loaders.collect import collect_corps, collect_disclosures, collect_investments
from src.loaders.duckdb_store import connect, is_lock_error
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


@dg.asset_check(asset=target_corps, description="지주 4곳이 모두 있고 자회사가 1곳 이상인지")
def target_corps_complete() -> dg.AssetCheckResult:
    with connect(read_only=True) as con:
        holdings, subs = con.execute(
            "SELECT count(*) FILTER (WHERE role='holding'), count(*) FILTER (WHERE role='subsidiary') FROM target_corp"
        ).fetchone()
    return dg.AssetCheckResult(
        passed=holdings == len(HOLDING_CORPS) and subs > 0, metadata={"holdings": holdings, "subsidiaries": subs}
    )


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
    return dg.MaterializeResult(metadata={"corp_name": corp_name, "period": f"{bgn_de}~{end_de}", **stats})


@dg.asset_check(asset=disclosures, description="모든 공시의 corp_code가 corp 테이블에 있는지 (회사 식별자 무결성)")
def disclosure_corp_code_exists() -> dg.AssetCheckResult:
    with connect(read_only=True) as con:
        orphans = con.execute("SELECT count(*) FROM disclosure d ANTI JOIN corp c USING (corp_code)").fetchone()[0]
    return dg.AssetCheckResult(passed=orphans == 0, metadata={"orphan_rows": orphans})


@dg.asset_check(
    asset=disclosures,
    description="rcept_no가 14자리 숫자이고 앞 8자리가 접수일(rcept_dt)과 같은지",
)
def disclosure_rcept_no_format() -> dg.AssetCheckResult:
    with connect(read_only=True) as con:
        bad = con.execute(
            """
            SELECT count(*) FROM disclosure
            WHERE NOT regexp_full_match(rcept_no, '[0-9]{14}')
               OR substr(rcept_no, 1, 8) <> strftime(rcept_dt, '%Y%m%d')
            """
        ).fetchone()[0]
    # 형식이 어긋나도 데이터를 못 쓰는 건 아니므로 실패가 아니라 경고로 둔다
    return dg.AssetCheckResult(passed=bad == 0, severity=dg.AssetCheckSeverity.WARN, metadata={"bad_rows": bad})


targets_job = dg.define_asset_job(
    "targets_job", selection=dg.AssetSelection.assets(corp_codes, holding_investments, target_corps)
)
disclosures_job = dg.define_asset_job("disclosures_job", selection=dg.AssetSelection.assets(disclosures))


@dg.schedule(job=targets_job, cron_schedule="0 8 15 4 *", execution_timezone="Asia/Seoul")
def yearly_targets():
    """매년 4월 15일 08:00, 새 사업보고서(3월 말 제출)로 회사 목록과 수집 대상을 갱신한다. API 5건."""
    return dg.RunRequest()


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


defs = dg.Definitions(
    assets=[corp_codes, holding_investments, target_corps, disclosures],
    asset_checks=[target_corps_complete, disclosure_corp_code_exists, disclosure_rcept_no_format],
    jobs=[targets_job, disclosures_job],
    schedules=[yearly_targets, daily_disclosures],
    # 한 run 안에서 자산을 동시에 실행하지 않는다 (DuckDB 쓰기는 한 프로세스만)
    executor=dg.in_process_executor,
)
