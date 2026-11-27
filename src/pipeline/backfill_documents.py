"""공시 원문(document.xml) 백필을 프로세스 하나에서 실행한다 (Phase 3).

실행 (저장소 루트에서, dagster dev는 꺼 둔 상태로, 공시 목록 백필이 끝난 뒤):
    python -m src.pipeline.backfill_documents --dry-run                 # 계획만 (API 호출 없음)
    python -m src.pipeline.backfill_documents --kind 11011              # 사업보고서만 (약 150건)
    python -m src.pipeline.backfill_documents                           # 정기보고서 전체 (약 618건 + 정정본)

backfill_reports.py와 같은 방식이다. 다른 점 둘:
- 보고서 1건 = API 1건 (ZIP 하나)
- 완료 기록을 Dagster가 아니라 DuckDB document 테이블로 본다. 원문은 접수번호 단위라 "이미 받은 접수번호"가
  곧 완료 기록이다. Dagster documents 자산(definitions.py)은 이 테이블을 읽어 같은 판단을 한다

동작
    - 대상 회사: target_corp 테이블 (--corp로 좁힐 수 있음)
    - 보고서: disclosure 목록의 정기보고서. 정정본은 접수번호가 다르므로 각각 받는다. --from-year, --kind로 제한
    - 이미 document에 있는 접수번호는 건너뛴다 → 중간에 멈춰도 다시 실행하면 이어서 한다
    - 최근 접수가 먼저
    - 멈추는 조건: --max-calls, DART 한도 초과(020), 연속 5번 실패

DuckDB 쓰기 연결을 처음부터 끝까지 하나만 연다. 실행 중에는 노트북·dagster dev에서 DuckDB를 열지 말 것.
"""

import argparse
import sys
import time

from src.collectors.corp_code import get_api_key
from src.collectors.dart_client import DartApiError, DartRateLimitError
from src.collectors.investment import REPORT_CODES
from src.loaders.collect import collect_document
from src.loaders.documents import missing_documents, target_corp_codes
from src.loaders.duckdb_store import connect, is_lock_error

MAX_CONSECUTIVE_FAILURES = 5
FIRST_REPORT_YEAR = 2021


def run(args: argparse.Namespace) -> int:
    try:
        con = connect(read_only=args.dry_run)
    except Exception as e:
        if is_lock_error(e):
            print("DuckDB가 다른 프로세스에서 열려 있습니다. dagster dev·노트북 연결을 닫고 다시 실행하세요.", file=sys.stderr)
            return 1
        raise

    with con:
        codes = target_corp_codes(con)
        if not codes:
            print("target_corp 테이블이 비어 있습니다. 먼저 targets_job을 실행하세요.", file=sys.stderr)
            return 2
        if args.corp:
            codes = [c for c in codes if c in set(args.corp)]
        plan = missing_documents(con, codes, args.from_year, set(args.kind) if args.kind else None)
        corps = {r["corp_code"] for r in plan}
        print(f"받을 원문 {len(plan):,}개 (회사 {len(corps)}곳, 이미 저장된 것 제외), API 호출 {len(plan):,}건")
        print(f"호출 상한 --max-calls {args.max_calls:,}건")
        if args.dry_run or not plan:
            for r in plan[:5]:
                print(f"  예: {r['rcept_no']} {r['corp_code']} {r['report_nm']} ({r['rcept_dt']})")
            return 0

        api_key = get_api_key()
        calls = done = no_file = 0
        total_bytes = 0
        failures: list[tuple[str, str]] = []
        consecutive_failures = 0
        started = time.monotonic()
        stop_reason = "완료"
        for i, r in enumerate(plan, start=1):
            if calls + 1 > args.max_calls:
                stop_reason = f"호출 상한 {args.max_calls:,}건 도달"
                break
            try:
                stats = collect_document(con, api_key, r["rcept_no"])
            except DartRateLimitError as e:
                stop_reason = f"DART 한도 초과: {e}"
                break
            except DartApiError as e:
                failures.append((r["rcept_no"], str(e)))
                consecutive_failures += 1
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    stop_reason = f"연속 {MAX_CONSECUTIVE_FAILURES}번 실패 (마지막: {e})"
                    break
                continue
            consecutive_failures = 0
            calls += stats["api_calls"]
            if stats.get("no_file"):
                no_file += 1  # [첨부정정] 등 본문 원문이 없는 공시. 기록되어 다음 백필에서 빠진다
                continue
            done += 1
            total_bytes += stats["file_size"]
            if i % 25 == 0 or i == len(plan):
                print(f"  {i:,}/{len(plan):,} 원문, {total_bytes / 1e6:,.0f}MB, {(time.monotonic() - started) / 60:.1f}분", flush=True)

        print(f"종료: {stop_reason}")
        print(f"저장 {done:,}개 ({total_bytes / 1e6:,.0f}MB), 원문 없음(014) {no_file}개, API 호출 {calls:,}건, 실패 {len(failures)}개")
        for rcept_no, err in failures[:10]:
            print(f"  실패 {rcept_no}: {err}")
    return 0 if stop_reason == "완료" and not failures else 1


def main() -> None:
    parser = argparse.ArgumentParser(description="공시 원문(document.xml) 백필 (프로세스 하나에서 실행)")
    parser.add_argument("--from-year", type=int, default=FIRST_REPORT_YEAR, help=f"시작 사업연도 (기본 {FIRST_REPORT_YEAR})")
    parser.add_argument("--corp", action="append", help="특정 corp_code만 (여러 번 지정 가능)")
    parser.add_argument("--kind", action="append", choices=sorted(REPORT_CODES),
                        help="보고서 종류 reprt_code (11011 사업, 11012 반기, 11013 1분기, 11014 3분기. 여러 번 지정 가능)")
    parser.add_argument("--max-calls", type=int, default=1000, help="이 호출 수에 닿으면 멈춤 (기본 1000)")
    parser.add_argument("--dry-run", action="store_true", help="계획만 출력하고 API를 호출하지 않음")
    sys.exit(run(parser.parse_args()))


if __name__ == "__main__":
    main()
