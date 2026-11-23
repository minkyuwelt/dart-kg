"""report_details 백필 계획(plan_partitions) 테스트. DART API를 호출하지 않는다."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import dagster as dg

from src.loaders.duckdb_store import connect, upsert_disclosures
from src.pipeline import definitions
from src.pipeline.backfill_reports import plan_partitions
from tests.test_backfill import report_targets
from tests.test_report_details import disclosure_row

KB, SHINHAN = "00688996", "00382199"


class PlanReportPartitionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(self.tmp.name) / "test.duckdb"), "DART_API_KEY": "FAKEKEY"})
        env.start()
        self.addCleanup(env.stop)
        self.instance = self.enterContext(dg.instance_for_test())
        self.instance.add_dynamic_partitions("corp", [KB, SHINHAN])
        report_targets(self.instance, [KB, SHINHAN, "00000077"])

    def test_plans_only_listed_reports_latest_first_and_skips_done(self):
        with connect() as con:
            upsert_disclosures(con, [
                disclosure_row("20250514000100", "분기보고서 (2025.03)", "20250514"),
                disclosure_row("20260313001191", "사업보고서 (2025.12)", "20260313"),
                disclosure_row("20260619000667", "[기재정정]사업보고서 (2025.12)", "20260619"),  # 같은 보고서, 한 번만
                disclosure_row("20200330000001", "사업보고서 (2019.12)", "20200330"),  # 파티션 범위(2021~) 밖
                disclosure_row("20260313002000", "사업보고서 (2025.12)", "20260313", corp_code=SHINHAN),
                disclosure_row("20260320000001", "주주총회소집공고", "20260320"),
            ])
            self.instance.report_runless_asset_event(
                dg.AssetMaterialization(asset_key=definitions.report_details.key, partition=f"{SHINHAN}|2025-11011")
            )
            keys = [str(k) for k in plan_partitions(self.instance, con, 2021, None)]
            self.assertEqual(keys, [f"{KB}|2025-11011", f"{KB}|2025-11013"])

            keys = [str(k) for k in plan_partitions(self.instance, con, 2021, [SHINHAN, "00000077"])]
            self.assertEqual(keys, [])  # 신한은 완료, 00000077은 파티션 없음

            keys = [str(k) for k in plan_partitions(self.instance, con, 2026, None)]
            self.assertEqual(keys, [])  # 2025 사업연도는 --from-year 2026에서 제외

    def test_requires_target_corps_record(self):
        with dg.instance_for_test() as empty, connect() as con:
            with self.assertRaises(SystemExit):
                plan_partitions(empty, con, 2021, None)


if __name__ == "__main__":
    unittest.main()
