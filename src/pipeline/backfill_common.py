"""백필 스크립트 세 개(backfill·backfill_reports·backfill_documents)가 같이 쓰는 것 (코드 리뷰 B2, 2026-11-27).

세 스크립트는 "계획 세우기 → 항목마다 API 한 번 → 성공하면 기록"이 같고, 멈추는 조건(호출 상한·DART 한도·연속 실패)도 같다.
전에는 그 루프와 카운터가 세 벌 복사돼 있었고 호출 상한 판정이 `calls >= max`·`calls + 4 > max`·`calls + 1 > max`로 적혀 있었다.
여기서는 하나의 규칙 "다음 호출(cost_per_item건)이 상한을 넘기면 멈춘다"로 쓴다 — 세 스크립트의 현재 값(1·4·1)에서 결과는 같다.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

import dagster as dg

from src.collectors.dart_client import DartApiError, DartRateLimitError
from src.loaders.duckdb_store import connect, is_lock_error

MAX_CONSECUTIVE_FAILURES = 5


def require_dagster_home() -> bool:
    """Dagster 인스턴스를 쓰는 스크립트의 첫 확인. 없으면 안내하고 False."""
    if os.getenv("DAGSTER_HOME"):
        return True
    print("DAGSTER_HOME이 없습니다. DAGSTER_HOME=$PWD/dagster_home 을 붙여 실행하세요.", file=sys.stderr)
    return False


def open_connection(read_only: bool = False):
    """DuckDB 연결. 다른 프로세스가 잠그고 있으면 설명을 찍고 None (그 밖의 오류는 그대로 올린다)."""
    try:
        return connect(read_only=read_only)
    except Exception as e:
        if is_lock_error(e):
            print("DuckDB가 다른 프로세스에서 열려 있습니다. dagster dev·노트북 연결을 닫고 다시 실행하세요.", file=sys.stderr)
            return None
        raise


@dataclass
class BackfillRun:
    calls: int = 0
    succeeded: int = 0
    failures: list[tuple[str, str]] = field(default_factory=list)
    stop_reason: str = "완료"
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed_min(self) -> float:
        return (time.monotonic() - self.started) / 60

    @property
    def ok(self) -> bool:
        return self.stop_reason == "완료" and not self.failures

    def print_tail(self) -> None:
        print(f"종료: {self.stop_reason}")
        for label, err in self.failures[:10]:
            print(f"  실패 {label}: {err}")


def run_backfill(items: Iterable, call_one: Callable, *, cost_per_item: int, max_calls: int, on_success: Callable,
                 label: Callable[[object], str], progress_every: int, progress: Callable[[int, int, BackfillRun], None]) -> BackfillRun:
    """항목마다 call_one(item) → stats(dict, "api_calls" 포함). 성공하면 on_success(item, stats).

    멈춤: 다음 호출이 상한을 넘기기 직전(calls + cost_per_item > max_calls), DART 한도 초과(status 020), 연속 MAX_CONSECUTIVE_FAILURES번 실패.
    DartApiError는 실패로 적고 계속한다. progress(i, n, run)는 성공 i가 progress_every의 배수이거나 마지막일 때.
    """
    items = list(items)
    run = BackfillRun()
    consecutive = 0
    for i, item in enumerate(items, start=1):
        if run.calls + cost_per_item > max_calls:
            run.stop_reason = f"호출 상한 {max_calls:,}건 도달"
            break
        try:
            stats = call_one(item)
        except DartRateLimitError as e:
            run.stop_reason = f"DART 한도 초과: {e}"
            break
        except DartApiError as e:
            run.failures.append((label(item), str(e)))
            consecutive += 1
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                run.stop_reason = f"연속 {MAX_CONSECUTIVE_FAILURES}번 실패 (마지막: {e})"
                break
            continue
        consecutive = 0
        run.calls += stats["api_calls"]
        run.succeeded += 1
        on_success(item, stats)
        if i % progress_every == 0 or i == len(items):
            progress(i, len(items), run)
    return run


def report_check_result(instance: dg.DagsterInstance, asset_key: dg.AssetKey, check_name: str, result: dg.AssetCheckResult) -> tuple[str, bool, dict]:
    """검사 결과를 Dagster에 기록하고 (이름, 통과, 메타데이터 평문)으로 돌려준다. 스크립트가 끝에 출력한다."""
    instance.report_runless_asset_event(
        dg.AssetCheckEvaluation(asset_key=asset_key, check_name=check_name, passed=result.passed,
                                metadata=result.metadata or {}, severity=result.severity)
    )
    metadata = {k: v.value if hasattr(v, "value") else v for k, v in (result.metadata or {}).items()}
    return check_name, result.passed, metadata
