"""백필 계획(plan_partitions) 테스트. DART API와 실제 DuckDB를 쓰지 않는다."""

import unittest

import dagster as dg

from src.pipeline import definitions
from src.pipeline.backfill import plan_partitions


def report_targets(instance: dg.DagsterInstance, codes: list[str]) -> None:
    instance.report_runless_asset_event(
        dg.AssetMaterialization(
            asset_key=definitions.target_corps.key,
            metadata={definitions.TARGET_CODES_METADATA_KEY: dg.MetadataValue.json(codes)},
        )
    )


class PlanPartitionsTest(unittest.TestCase):
    def setUp(self):
        self.instance = self.enterContext(dg.instance_for_test())

    def test_requires_target_corps_record(self):
        with self.assertRaises(SystemExit):
            plan_partitions(self.instance, None, None, None)

    def test_skips_done_and_orders_recent_first(self):
        self.instance.add_dynamic_partitions("corp", ["00688996", "00149293"])
        report_targets(self.instance, ["00688996", "00149293"])
        # 이미 완료된 파티션 하나
        self.instance.report_runless_asset_event(
            dg.AssetMaterialization(asset_key=definitions.disclosures.key, partition="00688996|2026-10-01")
        )
        keys = [str(k) for k in plan_partitions(self.instance, "2026-09-01", "2026-10-01", None)]
        self.assertEqual(keys, ["00149293|2026-10-01", "00149293|2026-09-01", "00688996|2026-09-01"])

    def test_corp_filter_and_targets_without_partition(self):
        # 00000077은 대상이지만 파티션이 없어서 제외 (스케줄과 같은 기준)
        self.instance.add_dynamic_partitions("corp", ["00688996", "00149293"])
        report_targets(self.instance, ["00688996", "00149293", "00000077"])
        keys = [str(k) for k in plan_partitions(self.instance, "2026-11-01", "2026-11-01", ["00149293", "00000077"])]
        self.assertEqual(keys, ["00149293|2026-11-01"])


if __name__ == "__main__":
    unittest.main()
