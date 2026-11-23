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
    - 끝나면 relations(관계 테이블 재생성, API 0건)를 실행하고 품질 검사 2개 결과를 Dagster에 기록한다

DuckDB 쓰기 연결을 처음부터 끝까지 하나만 연다. 실행 중에는 노트북·dagster dev에서 DuckDB를 열지 말 것.
"""

import argparse
import os
import sys
import time

import dagster as dg
import duckdb

from src.collectors.corp_code import get_api_key
from src.collectors.dart_client import DartApiError, DartRateLimitError
from src.loaders.collect import collect_report_details
from src.loaders.duckdb_store import connect, is_lock_error
from src.loaders.investments import rebuild_investments
from src.loaders.periodic_reports import periodic_reports_of, report_partition_key
from src.loaders.relations import rebuild_relations
from src.pipeline.definitions import (
    FIRST_REPORT_YEAR,
    relations,
    relations_source_known,
    relations_valid_from_sane,
    report_details,
    report_partitions,
    target_codes_from_instance,
)

MAX_CONSECUTIVE_FAILURES = 5


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
        dg.AssetMaterialization(
            asset_key=relations.key,
            metadata={"investment": inv["rows"], **{k: v for k, v in rel.items() if k != "holder_by_method"},
                      "holder_by_method": dg.MetadataValue.json(rel["holder_by_method"]), "source": "src.pipeline.backfill_reports"},
        )
    )
    results = []
    for check in (relations_source_known, relations_valid_from_sane):
        spec = next(iter(check.check_specs))
        result = check()
        metadata = {k: v.value if hasattr(v, "value") else v for k, v in (result.metadata or {}).items()}
        instance.report_runless_asset_event(
            dg.AssetCheckEvaluation(asset_key=relations.key, check_name=spec.name, passed=result.passed,
                                    metadata=result.metadata or {}, severity=result.severity)
        )
        results.append((spec.name, result.passed, metadata))
    return results


def run(args: argparse.Namespace) -> int:
    if not os.getenv("DAGSTER_HOME"):
        print("DAGSTER_HOME이 없습니다. DAGSTER_HOME=$PWD/dagster_home 을 붙여 실행하세요.", file=sys.stderr)
        return 2
    instance = dg.DagsterInstance.get()
    try:
        con = connect(read_only=args.dry_run)
    except Exception as e:
        if is_lock_error(e):
            print("DuckDB가 다른 프로세스에서 열려 있습니다. dagster dev·노트북 연결을 닫고 다시 실행하세요.", file=sys.stderr)
            return 1
        raise

    with con:
        keys = plan_partitions(instance, con, args.from_year, args.corp)
        corps = sorted({k.keys_by_dimension["corp"] for k in keys})
        print(f"백필할 보고서 {len(keys):,}개 (회사 {len(corps)}곳, 완료된 파티션 제외), API 호출 {len(keys) * 4:,}건")
        print(f"호출 상한 --max-calls {args.max_calls:,}건")
        if args.dry_run or not keys:
            return 0

        api_key = get_api_key()
        calls = done = 0
        failures: list[tuple[str, str]] = []
        consecutive_failures = 0
        started = time.monotonic()
        stop_reason = "완료"
        for i, key in enumerate(keys, start=1):
            if calls + 4 > args.max_calls:
                stop_reason = f"호출 상한 {args.max_calls:,}건 도달"
                break
            corp_code, report = key.keys_by_dimension["corp"], key.keys_by_dimension["report"]
            bsns_year, reprt_code = report.split("-")
            try:
                stats = collect_report_details(con, api_key, corp_code, bsns_year, reprt_code)
            except DartRateLimitError as e:
                stop_reason = f"DART 한도 초과: {e}"
                break
            except DartApiError as e:
                failures.append((str(key), str(e)))
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    stop_reason = f"연속 {MAX_CONSECUTIVE_FAILURES}번 실패 (마지막: {e})"
                    break
                continue
            consecutive_failures = 0
            calls += stats["api_calls"]
            done += 1
            instance.report_runless_asset_event(
                dg.AssetMaterialization(
                    asset_key=report_details.key,
                    partition=str(key),
                    metadata={"corp_code": corp_code, "report": report, "source": "src.pipeline.backfill_reports",
                              **{k: v for k, v in stats.items() if k != "statuses"}},
                )
            )
            if i % 50 == 0 or i == len(keys):
                print(f"  {i:,}/{len(keys):,} 보고서, 호출 {calls:,}건, {(time.monotonic() - started) / 60:.1f}분", flush=True)

        print(f"종료: {stop_reason}")
        print(f"완료 보고서 {done:,}개, API 호출 {calls:,}건, 실패 {len(failures)}개")
        for key, err in failures[:10]:
            print(f"  실패 {key}: {err}")
        if done:
            print("관계 테이블 재생성(API 0건)...")
            for name, passed, metadata in rebuild_and_check(instance, con):
                print(f"품질 검사 {name}: {'통과' if passed else '실패'} {metadata}")
    return 0 if stop_reason == "완료" and not failures else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="report_details 자산 백필 (프로세스 하나에서 실행)")
    parser.add_argument("--from-year", type=int, default=FIRST_REPORT_YEAR, help=f"시작 사업연도 (기본 {FIRST_REPORT_YEAR})")
    parser.add_argument("--corp", action="append", help="특정 corp_code만 (여러 번 지정 가능)")
    parser.add_argument("--max-calls", type=int, default=8000, help="이 호출 수에 닿으면 멈춤 (기본 8000)")
    parser.add_argument("--dry-run", action="store_true", help="계획만 출력하고 API를 호출하지 않음")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
