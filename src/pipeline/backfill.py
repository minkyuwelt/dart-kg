"""disclosures 자산 백필을 프로세스 하나에서 빠르게 실행한다.

실행 (저장소 루트에서, dagster dev는 꺼 둔 상태로):
    DAGSTER_HOME=$PWD/dagster_home python -m src.pipeline.backfill --dry-run   # 계획만 출력 (API 호출 없음)
    DAGSTER_HOME=$PWD/dagster_home python -m src.pipeline.backfill             # 실행

왜 Dagster 화면의 백필 대신 이 스크립트인가
    Dagster 백필은 파티션마다 run(프로세스)을 새로 띄운다. run 하나에 몇 초씩 걸려서
    59곳 × 71개월 = 4,189개 파티션이면 몇 시간이 걸린다. 이 스크립트는 같은 수집 함수
    (src/loaders/collect.py의 collect_disclosures)를 한 프로세스에서 차례로 호출하고,
    파티션 하나가 끝날 때마다 Dagster에 "이 파티션 완료"를 기록한다(report_runless_asset_event).
    그래서 Dagster 화면에는 정상적으로 채워진 파티션으로 보이고, 매일 스케줄과도 그대로 이어진다.

동작
    - 대상 회사: 가장 최근 target_corps 실행 기록(target_codes)과 동적 파티션의 교집합 (스케줄과 같은 기준)
    - 이미 완료로 기록된 파티션은 건너뛴다 → 중간에 멈춰도 다시 실행하면 이어서 한다
    - 최근 월부터 거꾸로 수집한다 (중간에 멈춰도 최신 데이터가 먼저 채워지도록)
    - 멈추는 조건: API 호출 수가 --max-calls를 넘기 직전, DART 한도 초과(status 020), 연속 5번 실패
    - 끝나면 disclosures의 품질 검사 2개를 실행해 결과도 Dagster에 기록한다

DuckDB 쓰기 연결을 처음부터 끝까지 하나만 연다. 실행 중에는 노트북·dagster dev에서 DuckDB를 열지 말 것.
"""

import argparse
import sys

import dagster as dg

from src.collectors.corp_code import get_api_key
from src.loaders.collect import collect_disclosures
from src.pipeline.backfill_common import open_connection, report_check_result, require_dagster_home, run_backfill
from src.pipeline.definitions import (
    disclosure_corp_code_exists,
    disclosure_rcept_no_format,
    disclosures,
    month_partitions,
    month_range,
    target_codes_from_instance,
    today_kst,
)


def plan_partitions(
    instance: dg.DagsterInstance, month_from: str | None, month_to: str | None, corps: list[str] | None
) -> list[dg.MultiPartitionKey]:
    """백필할 파티션 목록. 이미 완료된 파티션은 뺀다. 최근 월이 먼저 오게 정렬한다."""
    targets = target_codes_from_instance(instance)
    if targets is None:
        raise SystemExit("target_corps 실행 기록이 없습니다. 먼저 targets_job(또는 target_corps)을 실행하세요.")
    codes = sorted(set(targets) & set(instance.get_dynamic_partitions("corp")))
    if corps:
        codes = [c for c in codes if c in set(corps)]

    months = month_partitions.get_partition_keys()
    if month_from:
        months = [m for m in months if m >= month_from]
    if month_to:
        months = [m for m in months if m <= month_to]

    done = set(instance.get_materialized_partitions(disclosures.key))
    keys = [
        dg.MultiPartitionKey({"month": m, "corp": c}) for m in sorted(months, reverse=True) for c in codes
    ]
    return [k for k in keys if str(k) not in done]


def report_checks(instance: dg.DagsterInstance) -> list[tuple[str, bool, dict]]:
    """disclosures 품질 검사 2개를 실행하고 결과를 Dagster에 기록한다."""
    results = []
    for check in (disclosure_corp_code_exists, disclosure_rcept_no_format):
        spec = next(iter(check.check_specs))
        results.append(report_check_result(instance, disclosures.key, spec.name, check()))  # 검사 함수는 인자가 없고 read_only로 DuckDB를 연다
    return results


def run(args: argparse.Namespace) -> int:
    if not require_dagster_home():
        return 2

    instance = dg.DagsterInstance.get()
    keys = plan_partitions(instance, args.month_from, args.month_to, args.corp)
    corps = sorted({k.keys_by_dimension["corp"] for k in keys})
    months = sorted({k.keys_by_dimension["month"] for k in keys})
    print(f"백필할 파티션 {len(keys):,}개 (회사 {len(corps)}곳 × 월 {len(months)}개, 완료된 파티션 제외)")
    if keys:
        print(f"기간 {months[0]} ~ {months[-1]}, 최소 API 호출 {len(keys):,}건 (한 달 공시가 100건을 넘으면 페이지만큼 추가)")
    print(f"호출 상한 --max-calls {args.max_calls:,}건")
    if args.dry_run or not keys:
        return 0

    con = open_connection()
    if con is None:
        return 1
    api_key = get_api_key()
    today = today_kst()
    rows = 0

    def call_one(key):
        corp_code, month = key.keys_by_dimension["corp"], key.keys_by_dimension["month"]
        bgn_de, end_de = month_range(month, today)
        stats = collect_disclosures(con, api_key, corp_code, bgn_de, end_de)
        stats["period"] = f"{bgn_de}~{end_de}"
        return stats

    def on_success(key, stats):
        nonlocal rows
        rows += stats["fetched_rows"]
        instance.report_runless_asset_event(
            dg.AssetMaterialization(asset_key=disclosures.key, partition=str(key), metadata={"source": "src.pipeline.backfill", **stats})
        )

    def progress(i, n, run):
        print(f"  {i:,}/{n:,} 파티션, 호출 {run.calls:,}건, 공시 {rows:,}건, {run.elapsed_min:.1f}분", flush=True)

    with con:
        result = run_backfill(keys, call_one, cost_per_item=1, max_calls=args.max_calls, on_success=on_success, label=str,
                              progress_every=100, progress=progress)

    result.print_tail()
    print(f"완료 파티션 {result.succeeded:,}개, API 호출 {result.calls:,}건, 받은 공시 {rows:,}건, 실패 {len(result.failures)}개")
    for name, passed, metadata in report_checks(instance):
        print(f"품질 검사 {name}: {'통과' if passed else '실패'} {metadata}")
    return 0 if result.ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="disclosures 자산 백필 (프로세스 하나에서 실행)")
    parser.add_argument("--from", dest="month_from", help="시작 월 파티션 키 (예: 2021-01-01)")
    parser.add_argument("--to", dest="month_to", help="끝 월 파티션 키 (예: 2026-11-01)")
    parser.add_argument("--corp", action="append", help="특정 corp_code만 (여러 번 지정 가능)")
    parser.add_argument("--max-calls", type=int, default=8000, help="이 호출 수에 닿으면 멈춤 (기본 8000)")
    parser.add_argument("--dry-run", action="store_true", help="계획만 출력하고 API를 호출하지 않음")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
