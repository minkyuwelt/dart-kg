"""매일 운영 상태 한 장: `DAGSTER_HOME=$PWD/dagster_home python -m src.pipeline.status` (노트북, 읽기만, DART API 호출 없음).

왜: 스케줄 7개가 어제 돌았는지, 어디서 건너뛰었는지, 자산이 언제 마지막으로 만들어졌는지를 Dagster 화면에서 하나씩 눌러 봐야 알 수 있었다.
매일 아침 한 줄로 "어제 돌았나, 어디서 멈췄나"가 나와야 운영 확인이 습관이 된다 (2026-11-27).

보여 주는 것:
① 스케줄마다: 켜짐/꺼짐, 마지막 평가(tick) 시각과 결과(실행 요청 N건 / 건너뜀 이유 / 오류)
② 작업(job)마다: 마지막 실행 시각·상태
③ 자산마다: 마지막 실행 시각 (파티션 자산은 어느 파티션인지)
④ DuckDB ↔ Neo4j 수 (--no-neo4j로 끌 수 있다. Neo4j가 꺼져 있으면 그 줄만 "연결 안 됨")

판정 규칙(코드가 추측하지 않는다 — 기준은 모두 숫자로 적어 둔다):
- 스케줄이 꺼져 있으면 "꺼짐" (코드 기본은 켜짐. 화면 Automation에서 끈 것)
- 스케줄은 켜진 시각보다 앞선 평가는 만들지 않는다 (Dagster 규칙). 그래서 처음 켠 날은 그 뒤 시각의 스케줄만 돈다. 따라잡기(놓친 평가 중 가장 최근 하나)는 켜진 뒤 데몬이 꺼져 있던 동안에만
- 마지막 평가가 STALE_HOURS(36시간)보다 오래됐으면 "오래됨" — 데몬이 그동안 안 돌았다는 뜻 (노트북이 꺼져 있었거나 dagster dev를 안 켰다)
- 마지막 평가가 FAILURE면 오류 메시지 앞부분을 보여 준다
Dagster 인스턴스만 읽는다 (스케줄과 같은 이유로 DuckDB를 열지 않아도 ①~③은 나온다). ④만 DuckDB 읽기 전용 + Neo4j.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import dagster as dg
from dagster._core.scheduler.instigation import InstigatorStatus, InstigatorType, TickStatus

KST = ZoneInfo("Asia/Seoul")
STALE_HOURS = 36      # 하루 한 번 도는 스케줄이 이보다 오래 평가되지 않았으면 데몬이 안 돈 것


def _fmt(ts: float | None) -> str:
    if ts is None:
        return "없음"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(KST).strftime("%m-%d %H:%M")


def _hours_ago(ts: float, now: float) -> float:
    return (now - ts) / 3600


def schedule_rows(instance: dg.DagsterInstance, schedule_names: list[str], now: float) -> list[dict]:
    """스케줄마다 상태와 마지막 평가. 코드에 있는데 인스턴스에 기록이 없는 스케줄은 '기록 없음'(한 번도 켠 적 없음)."""
    states = {s.instigator_name: s for s in instance.all_instigator_state(instigator_type=InstigatorType.SCHEDULE)}
    rows = []
    for name in schedule_names:
        st = states.get(name)
        row = {"name": name, "running": None, "tick_at": None, "tick": "기록 없음", "stale": False}
        if st is None:
            rows.append(row)
            continue
        # DECLARED_IN_CODE = 코드의 default_status를 따른다. 우리 스케줄은 모두 기본 켜짐(definitions.SCHEDULE_DEFAULT)이라 켜진 것.
        # 화면에서 끄면 STOPPED, 화면에서 켜면 RUNNING으로 바뀐다 (2026-11-27 첫 실행에서 7개가 "꺼짐"으로 잘못 나와 고침)
        row["running"] = st.status in (InstigatorStatus.RUNNING, InstigatorStatus.AUTOMATICALLY_RUNNING, InstigatorStatus.DECLARED_IN_CODE)
        ticks = instance.get_ticks(st.instigator_origin_id, st.selector_id, limit=1)
        if ticks:
            t = ticks[0]
            row["tick_at"] = t.timestamp
            row["stale"] = _hours_ago(t.timestamp, now) > STALE_HOURS
            if t.status == TickStatus.SUCCESS:
                row["tick"] = f"실행 요청 {len(t.run_ids)}건"
            elif t.status == TickStatus.SKIPPED:
                row["tick"] = f"건너뜀 — {t.skip_reason or ''}"
            elif t.status == TickStatus.FAILURE:
                msg = (t.error.message if t.error else "").strip().splitlines()
                row["tick"] = f"오류 — {msg[0][:120] if msg else '?'}"
            else:
                row["tick"] = t.status.value
        else:
            row["tick"] = "평가 기록 없음"
        rows.append(row)
    return rows


def job_rows(instance: dg.DagsterInstance, job_names: list[str]) -> list[dict]:
    rows = []
    for name in job_names:
        runs = instance.get_run_records(dg.RunsFilter(job_name=name), limit=1)
        if not runs:
            rows.append({"name": name, "at": None, "status": "실행 없음", "partition": None})
            continue
        r = runs[0]
        rows.append({"name": name, "at": r.end_time or r.start_time or r.create_timestamp.timestamp(), "status": r.dagster_run.status.value,
                     "partition": r.dagster_run.tags.get("dagster/partition")})
    return rows


def asset_rows(instance: dg.DagsterInstance, asset_keys: list[dg.AssetKey]) -> list[dict]:
    events = instance.get_latest_materialization_events(asset_keys)
    rows = []
    for key in asset_keys:
        ev = events.get(key)
        if ev is None or ev.asset_materialization is None:
            rows.append({"name": key.to_user_string(), "at": None, "partition": None})
        else:
            rows.append({"name": key.to_user_string(), "at": ev.timestamp, "partition": ev.asset_materialization.partition})
    return rows


def render(schedules: list[dict], jobs: list[dict], assets: list[dict], now: float) -> str:
    out = [f"# 운영 상태 {_fmt(now)} (KST)", "", "## 스케줄 (켜짐? · 마지막 평가 · 결과)"]
    problems = []
    for r in schedules:
        flag = "켜짐" if r["running"] else ("꺼짐" if r["running"] is False else "기록 없음")
        stale = "  ⚠ 오래됨" if r["stale"] else ""
        out.append(f"  {r['name']:<22} {flag:<5} {_fmt(r['tick_at']):<12} {r['tick']}{stale}")
        if r["running"] is not True:
            problems.append(f"{r['name']} 스케줄이 꺼져 있다 (코드 기본은 켜짐 → 화면 Automation에서 끈 것. 다시 켜거나, 끈 이유를 devlog에)")
        elif r["stale"]:
            problems.append(f"{r['name']} 마지막 평가가 {_hours_ago(r['tick_at'], now):.0f}시간 전 → 그동안 dagster dev(데몬)가 돌지 않았다")
        elif r["tick"].startswith("오류"):
            problems.append(f"{r['name']} 마지막 평가가 오류")
    out += ["", "## 작업 (마지막 실행)"]
    for r in jobs:
        part = f"  파티션 {r['partition']}" if r["partition"] else ""
        out.append(f"  {r['name']:<22} {_fmt(r['at']):<12} {r['status']}{part}")
        if r["status"] in ("FAILURE", "CANCELED"):
            problems.append(f"{r['name']} 마지막 실행이 {r['status']}")
    out += ["", "## 자산 (마지막 실행)"]
    for r in assets:
        part = f"  파티션 {r['partition']}" if r["partition"] else ""
        out.append(f"  {r['name']:<22} {_fmt(r['at']):<12}{part}")
        if r["at"] is None:
            problems.append(f"{r['name']} 자산이 한 번도 만들어지지 않았다")
    out += ["", "## 판정"]
    out += [f"  - {p}" for p in problems] if problems else ["  이상 없음"]
    return "\n".join(out)


def render_sync(chunk_stats: dict | None, relation_stats: dict | None, error: str | None) -> str:
    out = ["", "## DuckDB ↔ Neo4j"]
    if error:
        # DuckDB 잠금은 Neo4j 문제가 아니다: 쓰는 run(relations·rag_index 등)이 돌고 있으면 읽기 전용 연결도 막힌다 (CLAUDE.md). 끝나면 다시
        if "Could not set lock" in error or "Conflicting lock" in error:
            out.append("  DuckDB가 쓰는 중(위 작업 중 STARTED인 것) — 끝난 뒤 다시 보면 된다")
        else:
            out.append(f"  Neo4j 연결 안 됨 — {error}  (docker compose up -d 뒤 다시)")
        return "\n".join(out)
    d, n = chunk_stats["duckdb"], chunk_stats["neo4j"]
    ok = d["documents"] == n["documents"] and d["chunks_with_embedding"] == n["chunks"] == n["rels"]
    out.append(f"  조각  DuckDB 보고서 {d['documents']} · 벡터 있는 조각 {d['chunks_with_embedding']:,} (없는 조각 {d['chunks_without_embedding']:,})"
               f" | Neo4j Document {n['documents']} · Chunk {n['chunks']:,} → {'일치' if ok else '불일치 → python -m src.graph.load_chunks build'}")
    w, h = relation_stats["duckdb"], relation_stats["neo4j"]
    out.append(f"  관계  DuckDB OWNS {w['owns']:,} · HOLDS_POSITION {w['positions']:,} · FORMERLY {w['formerly']}"
               f" | Neo4j {h['owns']:,} · {h['positions']:,} · {h['formerly']} → {'일치' if relation_stats['match'] else '불일치 → graph_job 실행 (python -m src.graph.load_relations build)'}")
    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="매일 운영 상태 (읽기만)")
    ap.add_argument("--no-neo4j", action="store_true", help="DuckDB ↔ Neo4j 비교를 건너뛴다 (Dagster 기록만)")
    args = ap.parse_args(argv)

    from src.pipeline import definitions as d
    now = datetime.now(tz=timezone.utc).timestamp()
    instance = dg.DagsterInstance.get()
    schedules = schedule_rows(instance, [s.name for s in d.defs.schedules], now)
    jobs = job_rows(instance, [j.name for j in d.defs.jobs])
    assets = asset_rows(instance, [a.key for a in d.defs.assets])
    print(render(schedules, jobs, assets, now))

    if not args.no_neo4j:
        from src.graph.client import connect as graph_connect
        from src.graph.client import settings
        from src.graph.load_chunks import stats as chunk_stats
        from src.graph.load_relations import stats as relation_stats
        from src.loaders.duckdb_store import connect
        try:
            s = settings()
            with graph_connect(s) as driver, connect(read_only=True) as con:
                cs = chunk_stats(con, driver, database=s.database)
                rs = relation_stats(con, driver, database=s.database)
            print(render_sync(cs, rs, None))
        except Exception as e:  # noqa: BLE001 — Neo4j가 꺼져 있는 것은 운영 상태의 일부라 보고하고 끝낸다
            print(render_sync(None, None, f"{type(e).__name__}: {str(e)[:100]}"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
