"""매일 스케줄(daily_disclosures)이 DuckDB를 열지 않는지 확인하는 테스트. DART API를 호출하지 않는다.

배경: 스케줄은 Dagster 데몬 프로세스에서 평가된다. DuckDB는 다른 프로세스가 쓰는 동안
읽기 전용 연결도 열 수 없어서, 스케줄이 DuckDB를 열면 백필과 겹칠 때 평가가 실패한다.
그래서 스케줄은 target_corps 실행 메타데이터(Dagster 인스턴스 저장소)에서 대상 목록을 읽는다.
"""

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import dagster as dg

from src.collectors.corp_code import HOLDING_CORPS
from src.loaders.duckdb_store import connect
from src.loaders.targets import rebuild_target_corps
from src.pipeline import definitions
from tests.test_targets import SUBSIDIARY_OF, seed_targets

KST = ZoneInfo("Asia/Seoul")

# 쓰기 연결을 열고, 준비됐다고 알린 뒤 stdin이 닫힐 때까지 유지하는 프로세스 (백필 중인 run을 흉내 냄)
HOLD_WRITER = (
    "import duckdb, sys\n"
    "con = duckdb.connect(sys.argv[1])\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


def report_targets(instance: dg.DagsterInstance, codes: list[str]) -> None:
    """target_corps가 실행된 것처럼 메타데이터를 남긴다."""
    instance.report_runless_asset_event(
        dg.AssetMaterialization(
            asset_key=definitions.target_corps.key,
            metadata={definitions.TARGET_CODES_METADATA_KEY: dg.MetadataValue.json(codes)},
        )
    )


def evaluate(instance: dg.DagsterInstance, when: datetime) -> list:
    context = dg.build_schedule_context(instance=instance, scheduled_execution_time=when)
    return list(definitions.daily_disclosures(context))


class DailyScheduleTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "test.duckdb")
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": self.db_path, "DART_API_KEY": "FAKEKEY"})
        env.start()
        self.addCleanup(env.stop)
        # 스케줄 컨텍스트는 인스턴스 참조(instance ref)가 필요해서 ephemeral 대신 임시 폴더 인스턴스를 쓴다
        self.instance = self.enterContext(dg.instance_for_test())

    def hold_writer(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c", HOLD_WRITER, self.db_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self.assertEqual(proc.stdout.readline().strip(), "ready")

        def release():
            proc.stdin.close()
            proc.wait(timeout=10)
            proc.stdout.close()

        self.addCleanup(release)

    def test_skips_when_targets_never_materialized(self):
        results = evaluate(self.instance, datetime(2026, 11, 24, 7, tzinfo=KST))
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], dg.SkipReason)

    def test_uses_targets_that_have_partitions(self):
        # 파티션은 있지만 대상에서 빠진 회사(00000099)와, 대상이지만 파티션이 아직 없는 회사(00000077)는 제외
        self.instance.add_dynamic_partitions("corp", ["00688996", "00149293", "00000099"])
        report_targets(self.instance, ["00688996", "00149293", "00000077"])

        mid_month = evaluate(self.instance, datetime(2026, 11, 24, 7, tzinfo=KST))
        self.assertEqual(
            sorted(r.partition_key for r in mid_month),
            ["00149293|2026-11-01", "00688996|2026-11-01"],
        )
        # 1~7일에는 지난달도 함께
        first_week = evaluate(self.instance, datetime(2026, 12, 1, 7, tzinfo=KST))
        self.assertEqual(len(first_week), 4)

    def test_works_while_another_process_writes_duckdb(self):
        with connect():
            pass  # 파일 생성
        self.hold_writer()
        # 전제 확인: 이 상태에서는 읽기 전용 연결도 열리지 않는다
        with self.assertRaises(Exception):
            connect(read_only=True)

        self.instance.add_dynamic_partitions("corp", ["00688996"])
        report_targets(self.instance, ["00688996"])
        results = evaluate(self.instance, datetime(2026, 11, 24, 7, tzinfo=KST))
        self.assertEqual([r.partition_key for r in results], ["00688996|2026-11-01"])


class TargetCorpsMetadataTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(tmp.name) / "t.duckdb"), "DART_API_KEY": "FAKEKEY"})
        env.start()
        self.addCleanup(env.stop)

    @mock.patch.object(definitions, "today_kst", return_value=datetime(2026, 11, 24).date())
    def test_target_corps_records_codes_and_partitions(self, _today):
        with connect() as con:
            seed_targets(con, bsns_year="2025")
        expected = sorted([*HOLDING_CORPS, *SUBSIDIARY_OF.values()])
        with dg.DagsterInstance.ephemeral() as instance:
            result = dg.materialize([definitions.target_corps], instance=instance)
            self.assertTrue(result.success)
            self.assertEqual(sorted(definitions.target_codes_from_instance(instance)), expected)
            self.assertEqual(sorted(instance.get_dynamic_partitions("corp")), expected)
            self.assertEqual(definitions.latest_target_bsns_year(instance), 2025)

    def test_check_fails_when_a_holding_lost_its_subsidiaries(self):
        """target_corp가 어떤 경로로든 지주 하나의 자회사를 잃으면 검사가 실패한다 (전체 수만 보면 통과했던 경우)."""
        with connect() as con:
            seed_targets(con)
            rebuild_target_corps(con, 2025)
        self.assertTrue(definitions.target_corps_complete().passed)

        with connect() as con:
            con.execute("DELETE FROM target_corp WHERE parent_corp_code = '00547583'")
        result = definitions.target_corps_complete()
        self.assertFalse(result.passed)
        self.assertEqual(result.metadata["subsidiaries_by_holding"].value["하나금융지주"], 0)


class TargetsFreshnessScheduleTest(unittest.TestCase):
    """매일 10:00 스케줄이 기준 사업연도가 바뀌었을 때만 targets_job을 요청하는지."""

    def setUp(self):
        self.instance = self.enterContext(dg.instance_for_test())

    def report_bsns_year(self, year: int) -> None:
        self.instance.report_runless_asset_event(
            dg.AssetMaterialization(asset_key=definitions.target_corps.key, metadata={"bsns_year": year})
        )

    def evaluate(self, when: datetime) -> list:
        context = dg.build_schedule_context(instance=self.instance, scheduled_execution_time=when)
        return list(definitions.targets_freshness(context))

    def test_runs_when_never_materialized(self):
        results = self.evaluate(datetime(2026, 11, 24, 6, 30, tzinfo=KST))
        self.assertIsInstance(results[0], dg.RunRequest)

    def test_skips_when_up_to_date(self):
        self.report_bsns_year(2025)
        results = self.evaluate(datetime(2026, 11, 24, 6, 30, tzinfo=KST))
        self.assertIsInstance(results[0], dg.SkipReason)

    def test_runs_when_new_business_year_starts_and_retries_next_day(self):
        self.report_bsns_year(2025)
        # 3월 31일: 아직 2025 기준이 최신 → 건너뜀
        self.assertIsInstance(self.evaluate(datetime(2027, 3, 31, 6, 30, tzinfo=KST))[0], dg.SkipReason)
        # 4월 1일: 2026 사업보고서 기준으로 갱신 요청. 4월 15일을 놓쳐도 이후 아무 날에나 잡힌다
        first = self.evaluate(datetime(2027, 4, 1, 6, 30, tzinfo=KST))[0]
        self.assertIsInstance(first, dg.RunRequest)
        # 갱신이 실패해(지주 사업보고서 미제출) 기록이 그대로면 다음 날 다른 run_key로 다시 요청
        second = self.evaluate(datetime(2027, 4, 2, 6, 30, tzinfo=KST))[0]
        self.assertIsInstance(second, dg.RunRequest)
        self.assertNotEqual(first.run_key, second.run_key)


if __name__ == "__main__":
    unittest.main()


class NewPeriodicReportsScheduleTest(unittest.TestCase):
    """daily_report_details·daily_documents: 정기보고서의 최신 접수번호를 그 자산이 아직 처리하지 않은 것만 run (2026-11-27).
    전에는 최근 2일 목록의 보고서를 매일 전부 돌려 29개 × 4 호출이 11월 내내 매일 나갔다."""

    CORP, REPORT, RCEPT = "00688996", "2026-11014", "20261114000111"

    def setUp(self):
        self.instance = self.enterContext(dg.instance_for_test())
        self.instance.add_dynamic_partitions("corp", [self.CORP])

    def disclosures_seen(self, reports):
        """disclosures 자산이 실행된 것처럼: 보고서 키 → 최신 접수번호 (옛 꼴은 키 목록)."""
        self.instance.report_runless_asset_event(dg.AssetMaterialization(
            asset_key=definitions.disclosures.key, partition=f"{self.CORP}|2026-11-01",
            metadata={"corp_code": self.CORP, definitions.PERIODIC_REPORTS_METADATA_KEY: dg.MetadataValue.json(reports)}))

    def processed(self, asset, rcept_no):
        """그 자산이 그 파티션을 그 접수번호로 처리한 기록."""
        self.instance.report_runless_asset_event(dg.AssetMaterialization(
            asset_key=asset.key, partition=f"{self.CORP}|{self.REPORT}",
            metadata={"corp_code": self.CORP, definitions.LISTED_RCEPT_NO_METADATA_KEY: rcept_no}))

    def evaluate(self, schedule):
        when = datetime.now(tz=KST)   # 실행 기록 시각은 '지금'이라 평가 시각도 지금이어야 최근 2일 안에 든다
        context = dg.build_schedule_context(instance=self.instance, scheduled_execution_time=when)
        return list(schedule(context))

    def test_runs_new_report_then_skips_once_processed_then_reruns_correction(self):
        self.disclosures_seen({self.REPORT: self.RCEPT})
        runs = self.evaluate(definitions.daily_report_details)
        self.assertEqual([r.partition_key for r in runs if isinstance(r, dg.RunRequest)], [f"{self.CORP}|{self.REPORT}"])
        # 주요정보가 그 접수번호로 처리됨 → 건너뜀 (원문은 아직이라 documents 스케줄은 run)
        self.processed(definitions.report_details, self.RCEPT)
        out = self.evaluate(definitions.daily_report_details)
        self.assertEqual(len(out), 1)
        self.assertIsInstance(out[0], dg.SkipReason)
        self.assertIn("이미 처리", str(out[0].skip_message))
        docs = self.evaluate(definitions.daily_documents)
        self.assertEqual([r.run_key for r in docs if isinstance(r, dg.RunRequest)][0][:10], "documents|")
        # 정정본(새 접수번호)이 목록에 들어오면 다시 run
        self.disclosures_seen({self.REPORT: "20261127000999"})
        runs = self.evaluate(definitions.daily_report_details)
        self.assertEqual(len([r for r in runs if isinstance(r, dg.RunRequest)]), 1)

    def test_old_style_metadata_without_rcept_no_still_runs(self):
        self.disclosures_seen([self.REPORT])      # 11-27 전의 기록: 키 목록만
        self.processed(definitions.report_details, self.RCEPT)
        runs = self.evaluate(definitions.daily_report_details)
        self.assertEqual(len([r for r in runs if isinstance(r, dg.RunRequest)]), 1)

    def test_skips_when_nothing_seen(self):
        out = self.evaluate(definitions.daily_report_details)
        self.assertIsInstance(out[0], dg.SkipReason)
