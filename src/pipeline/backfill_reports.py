"""report_details 자산(Phase 2 주요정보) 백필을 프로세스 하나에서 실행하고, 끝나면 관계 테이블을 다시 만든다.

실행 (저장소 루트에서, dagster dev는 꺼 둔 상태로, 공시 목록 백필이 끝난 뒤):
    DAGSTER_HOME=$PWD/dagster_home python -m src.pipeline.backfill_reports --dry-run   # 계획만 (API 호출 없음)
    DAGSTER_HOME=$PWD/dagster_home python -m src.pipeline.backfill_reports             # 실행

src/pipeline/backfill.py(공시 목록)와 같은 방식이다. 다른 점은 계획을 Dagster 파티션 전체가 아니라
DuckDB의 disclosure 목록에서 세운다는 것이다. 대상 회사 × 보고서 조합 중 실제로 접수된 정기보고서만
고르므로 "데이터 없음" 호출이 없다. 보고서 하나 = 파티션 하나 = API 4건(출자·최대주주·변동·임원).

동작
    - 대상 회사: 가장 최근 target_corps 실행 기록(target_codes)과 동적 파티션의 교집합
    - 보고서: disclosure 목록의 정기보고서(정정본은 최신 것 하나). --from-year로 시작 연도 제한
    - 이미 완료로 기록된 파티션은 건너뛴다 → 중간에 멈춰도 다시 실행하면 이어서 한다
    - 최근 보고서부터 거꾸로 수집한다
    - 멈추는 조건: --max-calls, DART 한도 초과(020), 연속 5번 실패
    - 끝나면 relations(관계 테이블 재생성, API 0건)를 실행하고 품질 검사 4개 결과를 Dagster에 기록한다
    - --rebuild-only: 수집 없이 이 마지막 단계만 (수집은 끝났는데 재생성·검사만 다시 할 때)

DuckDB 쓰기 연결을 처음부터 끝까지 하나만 연다. 실행 중에는 노트북·dagster dev에서 DuckDB를 열지 말 것.
"""

import argparse
import sys

import dagster as dg
import duckdb

from src.collectors.corp_code import get_api_key
from src.loaders.collect import collect_report_details
from src.loaders.investments import rebuild_investments
from src.loaders.periodic_reports import FIRST_REPORT_YEAR, periodic_reports_of, report_partition_key, split_report_partition_key
from src.loaders.relations import rebuild_relations
from src.pipeline.backfill_common import open_connection, report_check_result, require_dagster_home, run_backfill
from src.pipeline.definitions import (
    RELATION_CHECK_SPECS,
    relations,
    relations_metadata,
    report_details,
    report_partitions,
    target_codes_from_instance,
)


def plan_partitions(
    instance: dg.DagsterInstance, con: duckdb.DuckDBPyConnection, from_year: int, corps: list[str] | None
) -> list[dg.MultiPartitionKey]:
    """백필할 파티션. disclosure 목록에 있는 정기보고서만, 완료된 것은 제외, 최근 보고서가 먼저."""
    targets = target_codes_from_instance(instance)
    if targets is None:
        raise SystemExit("target_corps 실행 기록이 없습니다. 먼저 targets_job(또는 target_corps)을 실행하세요.")
    codes = sorted(set(targets) & set(instance.get_dynamic_partitions("corp")))
    if corps:
        codes = [c for c in codes if c in set(corps)]

    valid_reports = set(report_partitions.get_partition_keys())
    done = set(instance.get_materialized_partitions(report_details.key))
    keys = []
    for corp_code in codes:
        reports = {report_partition_key(r["bsns_year"], r["reprt_code"]) for r in periodic_reports_of(con, corp_code)
                   if int(r["bsns_year"]) >= from_year}
        for report in sorted(reports & valid_reports, reverse=True):
            key = dg.MultiPartitionKey({"corp": corp_code, "report": report})
            if str(key) not in done:
                keys.append(key)
    # 최근 보고서가 먼저 오게 (report_partitions가 결산 순서라 그 순서의 역순), 같은 보고서는 회사 역순
    order = {key: i for i, key in enumerate(report_partitions.get_partition_keys())}
    return sorted(keys, key=lambda k: (order[k.keys_by_dimension["report"]], k.keys_by_dimension["corp"]), reverse=True)


def rebuild_and_check(instance: dg.DagsterInstance, con: duckdb.DuckDBPyConnection) -> list[tuple[str, bool, dict]]:
    """relations 자산과 같은 일을 하고 결과를 Dagster에 기록한다."""
    inv = rebuild_investments(con)
    rel = rebuild_relations(con)
    instance.report_runless_asset_event(
        dg.AssetMaterialization(asset_key=relations.key, metadata={**relations_metadata(inv, rel), "source": "src.pipeline.backfill_reports"})
    )
    # 쓰기 연결을 그대로 쓴다 (같은 프로세스에서 읽기 전용 연결을 또 열 수 없음) — 그래서 Dagster 검사가 아니라 본체 함수를 부른다
    return [report_check_result(instance, relations.key, name, fn(con)) for name, _, fn in RELATION_CHECK_SPECS]


def run(args: argparse.Namespace) -> int:
    if not require_dagster_home():
        return 2
    instance = dg.DagsterInstance.get()
    con = open_connection(read_only=args.dry_run)
    if con is None:
        return 1

    with con:
        if args.rebuild_only:
            print("관계 테이블 재생성(API 0건)...")
            for name, passed, metadata in rebuild_and_check(instance, con):
                print(f"품질 검사 {name}: {'통과' if passed else '실패'} {metadata}")
            return 0

        keys = plan_partitions(instance, con, args.from_year, args.corp)
        corps = sorted({k.keys_by_dimension["corp"] for k in keys})
        print(f"백필할 보고서 {len(keys):,}개 (회사 {len(corps)}곳, 완료된 파티션 제외), API 호출 {len(keys) * 4:,}건")
        print(f"호출 상한 --max-calls {args.max_calls:,}건")
        if args.dry_run or not keys:
            return 0

        api_key = get_api_key()

        def call_one(key):
            corp_code, report = key.keys_by_dimension["corp"], key.keys_by_dimension["report"]
            bsns_year, reprt_code = split_report_partition_key(report)
            return collect_report_details(con, api_key, corp_code, bsns_year, reprt_code)

        def on_success(key, stats):
            corp_code, report = key.keys_by_dimension["corp"], key.keys_by_dimension["report"]
            instance.report_runless_asset_event(
                dg.AssetMaterialization(asset_key=report_details.key, partition=str(key),
                                        metadata={"corp_code": corp_code, "report": report, "source": "src.pipeline.backfill_reports",
                                                  **{k: v for k, v in stats.items() if k != "statuses"}})
            )

        def progress(i, n, run):
            print(f"  {i:,}/{n:,} 보고서, 호출 {run.calls:,}건, {run.elapsed_min:.1f}분", flush=True)

        result = run_backfill(keys, call_one, cost_per_item=4, max_calls=args.max_calls, on_success=on_success, label=str,
                              progress_every=50, progress=progress)
        result.print_tail()
        print(f"완료 보고서 {result.succeeded:,}개, API 호출 {result.calls:,}건, 실패 {len(result.failures)}개")
        if result.succeeded:
            print("관계 테이블 재생성(API 0건)...")
            for name, passed, metadata in rebuild_and_check(instance, con):
                print(f"품질 검사 {name}: {'통과' if passed else '실패'} {metadata}")
    return 0 if result.ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="report_details 자산 백필 (프로세스 하나에서 실행)")
    parser.add_argument("--from-year", type=int, default=FIRST_REPORT_YEAR, help=f"시작 사업연도 (기본 {FIRST_REPORT_YEAR})")
    parser.add_argument("--corp", action="append", help="특정 corp_code만 (여러 번 지정 가능)")
    parser.add_argument("--max-calls", type=int, default=8000, help="이 호출 수에 닿으면 멈춤 (기본 8000)")
    parser.add_argument("--dry-run", action="store_true", help="계획만 출력하고 API를 호출하지 않음")
    parser.add_argument("--rebuild-only", action="store_true", help="수집 없이 관계 테이블 재생성과 품질 검사만 (API 호출 없음)")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
