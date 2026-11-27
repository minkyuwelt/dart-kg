"""Dagster 인스턴스(자산 실행 기록과 그 메타데이터) 읽기. 스케줄이 DuckDB를 열지 않고 "무엇이 최근에 돌았나"를 보는 창 (CLAUDE.md 아키텍처 원칙).

왜 DuckDB가 아니라 여기인가: 스케줄은 Dagster 데몬 프로세스에서 평가되는데, DuckDB는 다른 프로세스가 쓰는 동안 읽기 전용 연결도 열 수 없다.
그 시각에 백필 같은 run이 쓰고 있으면 스케줄 평가가 잠금에 걸려 실패하고, 스케줄에는 재시도가 없어 그날 수집이 빠진다.
Dagster 인스턴스 저장소(dagster_home)는 DuckDB와 별개라 이 문제가 없다.

definitions.py에 제각각 있던 읽기 함수 다섯 개를 모은 것 (코드 리뷰 B1, 2026-11-27). 자산 키는 부르는 쪽이 넘긴다 — 이 모듈은 definitions를 모른다.
"""

from __future__ import annotations

from datetime import datetime

import dagster as dg

LOOKBACK_DAYS = 2  # 스케줄이 "최근"으로 보는 범위. 하루 꺼져 있어도 다음 날 따라잡는다. run_key가 같은 날 중복을 막는다


def latest_metadata(instance: dg.DagsterInstance, asset_key: dg.AssetKey, key: str):
    """그 자산의 가장 최근 실행 기록에 적힌 메타데이터 값. 기록이 없거나 그 칸이 없으면 None."""
    event = instance.get_latest_materialization_event(asset_key)
    if event is None or event.asset_materialization is None:
        return None
    value = event.asset_materialization.metadata.get(key)
    return value.value if value is not None else None


def latest_materialization_time(instance: dg.DagsterInstance, asset_key: dg.AssetKey) -> float | None:
    """그 자산이 마지막으로 만들어진 시각(epoch 초). 기록이 없으면 None."""
    event = instance.get_latest_materialization_event(asset_key)
    return event.timestamp if event is not None else None


def recent_materializations(instance: dg.DagsterInstance, asset_key: dg.AssetKey, now: datetime, days: int = LOOKBACK_DAYS) -> list:
    """now 기준 최근 days일 안의 자산 실행 기록 (최대 5,000개)."""
    since = now.timestamp() - days * 86400
    return list(instance.fetch_materializations(dg.AssetRecordsFilter(asset_key=asset_key, after_timestamp=since), limit=5000).records)


def partition_metadata(instance: dg.DagsterInstance, asset_key: dg.AssetKey, partition: str, key: str):
    """그 자산의 그 파티션의 마지막 실행 기록에 적힌 메타데이터 값. 기록이 없거나 값이 없으면 None."""
    records = instance.fetch_materializations(dg.AssetRecordsFilter(asset_key=asset_key, asset_partitions=[partition]), limit=1).records
    if not records:
        return None
    value = records[0].asset_materialization.metadata.get(key)
    return value.value if value is not None else None


def sum_recent_metadata(instance: dg.DagsterInstance, asset_key: dg.AssetKey, now: datetime, key: str, days: int = LOOKBACK_DAYS) -> int:
    """최근 days일 실행 기록의 정수 메타데이터 key 합. 칸이 없는 기록은 0."""
    total = 0
    for record in recent_materializations(instance, asset_key, now, days):
        value = record.asset_materialization.metadata.get(key)
        total += int(value.value) if value is not None else 0
    return total
