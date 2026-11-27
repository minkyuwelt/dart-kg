"""운영 상태 명령(src.pipeline.status)의 판정 규칙. Dagster 인스턴스만 쓰고 DART API·Neo4j·DuckDB는 열지 않는다."""

import logging
import time
import unittest
from types import SimpleNamespace

import dagster as dg
from dagster._core.scheduler.instigation import InstigatorStatus, InstigatorType, TickStatus

from src.pipeline import status
from src.pipeline.status import STALE_HOURS, asset_rows, job_rows, render, render_sync, schedule_rows


class FakeInstance:
    """all_instigator_state·get_ticks만 흉내 낸다. 진짜 스케줄 origin을 만들려면 저장소 로딩이 필요해 단위 테스트에서는 이 둘만 가짜로."""

    def __init__(self, states, ticks):
        self._states, self._ticks = states, ticks

    def all_instigator_state(self, instigator_type=None):
        assert instigator_type == InstigatorType.SCHEDULE
        return self._states

    def get_ticks(self, origin_id, selector_id, limit=None):
        return self._ticks.get(origin_id, [])[:limit]


def state(name, status):
    return SimpleNamespace(instigator_name=name, status=status, instigator_origin_id=f"o-{name}", selector_id=f"s-{name}")


def tick(ts, status, run_ids=(), skip_reason=None, error=None):
    return SimpleNamespace(timestamp=ts, status=status, run_ids=list(run_ids), skip_reason=skip_reason,
                           error=SimpleNamespace(message=error) if error else None)


class ScheduleRowsTest(unittest.TestCase):
    def test_states_and_last_tick(self):
        now = time.time()
        inst = FakeInstance(
            [state("daily_disclosures", InstigatorStatus.RUNNING), state("daily_relations", InstigatorStatus.STOPPED),
             state("daily_rag_index", InstigatorStatus.RUNNING), state("daily_documents", InstigatorStatus.DECLARED_IN_CODE)],   # 코드 기본 켜짐 = 켜짐
            {"o-daily_disclosures": [tick(now - 3600, TickStatus.SUCCESS, run_ids=["a", "b"])],
             "o-daily_rag_index": [tick(now - (STALE_HOURS + 5) * 3600, TickStatus.SKIPPED, skip_reason="최근 2일에 새 원문이 없고 지난 색인도 끝까지 갔습니다.")],
             "o-daily_documents": [tick(now - 600, TickStatus.FAILURE, error="ConnectionError: boom\n  trace")]},
        )
        rows = {r["name"]: r for r in schedule_rows(inst, ["daily_disclosures", "daily_relations", "daily_rag_index", "daily_documents", "targets_freshness"], now)}
        self.assertEqual((rows["daily_disclosures"]["running"], rows["daily_disclosures"]["tick"], rows["daily_disclosures"]["stale"]), (True, "실행 요청 2건", False))
        self.assertEqual((rows["daily_relations"]["running"], rows["daily_relations"]["tick"]), (False, "평가 기록 없음"))
        self.assertTrue(rows["daily_rag_index"]["stale"])                       # 36시간 넘게 평가 없음 → 데몬이 안 돈 것
        self.assertTrue(rows["daily_rag_index"]["tick"].startswith("건너뜀 — 최근 2일"))
        self.assertEqual((rows["daily_documents"]["running"], rows["daily_documents"]["tick"]), (True, "오류 — ConnectionError: boom"))
        self.assertEqual((rows["targets_freshness"]["running"], rows["targets_freshness"]["tick"]), (None, "기록 없음"))   # 켠 적 없음

        text = render(list(rows.values()), [], [], now)
        self.assertIn("daily_relations 스케줄이 꺼져 있다", text)
        self.assertIn("targets_freshness 스케줄이 꺼져 있다", text)
        self.assertIn("daily_rag_index 마지막 평가가 41시간 전", text)
        self.assertIn("daily_documents 마지막 평가가 오류", text)
        self.assertNotIn("daily_disclosures 스케줄", text)
        self.assertIn("⚠ 오래됨", text)


class RunsAndAssetsTest(unittest.TestCase):
    def test_last_run_and_materialization(self):
        with dg.instance_for_test() as instance:
            @dg.op
            def noop():
                return 1

            @dg.job(name="relations_job")
            def relations_job():
                noop()

            self.assertEqual(job_rows(instance, ["relations_job"])[0]["status"], "실행 없음")
            logging.disable(logging.CRITICAL)          # 가짜 job 실행 로그가 테스트 출력을 덮지 않게
            try:
                relations_job.execute_in_process(instance=instance)
            finally:
                logging.disable(logging.NOTSET)
            rows = job_rows(instance, ["relations_job", "graph_job"])
            self.assertEqual((rows[0]["status"], rows[0]["at"] is not None, rows[1]["status"]), ("SUCCESS", True, "실행 없음"))

            key = dg.AssetKey("disclosures")
            self.assertIsNone(asset_rows(instance, [key])[0]["at"])
            instance.report_runless_asset_event(dg.AssetMaterialization(asset_key=key, partition="2026-11-01|00126380"))
            a = asset_rows(instance, [key, dg.AssetKey("neo4j_relations")])
            self.assertEqual((a[0]["partition"], a[0]["at"] is not None, a[1]["at"]), ("2026-11-01|00126380", True, None))

            text = render([], rows, a, time.time())
            self.assertIn("neo4j_relations 자산이 한 번도 만들어지지 않았다", text)
            self.assertIn("파티션 2026-11-01|00126380", text)

    def test_render_sync(self):
        cs = {"duckdb": {"documents": 10, "chunks_with_embedding": 100, "chunks_without_embedding": 0}, "neo4j": {"documents": 10, "chunks": 100, "rels": 100}}
        rs = {"duckdb": {"owns": 5, "positions": 7, "formerly": 1}, "neo4j": {"owns": 5, "positions": 6, "formerly": 1}, "match": False}
        text = render_sync(cs, rs, None)
        self.assertIn("조각", text); self.assertIn("→ 일치", text)
        self.assertIn("관계", text); self.assertIn("불일치 → graph_job 실행", text)
        self.assertIn("Neo4j 연결 안 됨 — ServiceUnavailable", render_sync(None, None, "ServiceUnavailable: x"))
        # DuckDB 잠금(쓰는 run이 도는 중)은 Neo4j 안내가 아니라 "쓰는 중"으로 (2026-11-27 12:02 색인 run 중에 봄)
        lock = render_sync(None, None, 'IOException: IO Error: Could not set lock on file "data/dartkg.duckdb": Conflicting lock')
        self.assertIn("DuckDB가 쓰는 중", lock)
        self.assertNotIn("docker compose", lock)

    def test_definitions_names_exist(self):
        # status는 definitions의 스케줄·작업·자산 이름을 그대로 쓴다 — 이름이 바뀌어도 코드가 깨지지 않는지 (인스턴스 없이 목록만)
        from src.pipeline import definitions as d
        self.assertEqual(len(list(d.defs.schedules)), 7)
        self.assertIn("graph_job", [j.name for j in d.defs.jobs])
        self.assertTrue(hasattr(status, "main"))


if __name__ == "__main__":
    unittest.main()
