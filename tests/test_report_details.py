"""Phase 2 Dagster 자산(report_details, relations)과 스케줄 테스트. DART API를 호출하지 않는다.

확인하는 것
1) 보고서명 → (사업연도, 보고서 종류) 변환
2) report_details 자산은 disclosure 목록에 보고서가 있을 때만 수집 함수를 부른다
3) daily_report_details 스케줄은 DuckDB를 열지 않고 disclosures 실행 메타데이터로 파티션을 고른다
4) daily_relations 스케줄은 최근 report_details 실행이 있을 때만 run을 만든다
"""

import json
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import dagster as dg

from src.loaders.duckdb_store import connect, save_raw, upsert_disclosures
from src.loaders.periodic_reports import (
    latest_periodic_report,
    periodic_report_key,
    periodic_reports_of,
    report_partition_keys,
)
from src.pipeline import definitions
from tests.test_schedule import HOLD_WRITER  # noqa: F401  (같은 흉내 프로세스를 쓸 수 있게 둔다)
from tests.helpers import TempDuckDBTestCase

KST = ZoneInfo("Asia/Seoul")
KB = "00688996"
FIXTURES = Path(__file__).parent / "fixtures"


def disclosure_row(rcept_no: str, report_nm: str, rcept_dt: str, corp_code: str = KB) -> dict:
    return {"rcept_no": rcept_no, "corp_code": corp_code, "corp_name": "KB금융", "stock_code": "105560",
            "corp_cls": "Y", "report_nm": report_nm, "flr_nm": "KB금융", "rcept_dt": rcept_dt, "rm": ""}


class PeriodicReportKeyTest(unittest.TestCase):
    def test_maps_report_names_to_api_params(self):
        self.assertEqual(periodic_report_key("사업보고서 (2025.12)"), ("2025", "11011"))
        self.assertEqual(periodic_report_key("반기보고서 (2025.06)"), ("2025", "11012"))
        self.assertEqual(periodic_report_key("분기보고서 (2025.03)"), ("2025", "11013"))
        self.assertEqual(periodic_report_key("분기보고서 (2025.09)"), ("2025", "11014"))
        self.assertEqual(periodic_report_key("[기재정정]사업보고서 (2025.12)"), ("2025", "11011"))
        self.assertEqual(periodic_report_key("[첨부추가][기재정정] 분기보고서 (2024.09)"), ("2024", "11014"))

    def test_ignores_other_disclosures(self):
        for name in ("주주총회소집공고", "감사보고서 (2025.12)", "사업보고서제출기한연장신고서", "[기재정정]주요사항보고서(자기주식취득결정)", ""):
            self.assertIsNone(periodic_report_key(name), name)

    def test_partition_keys_cover_four_reports_per_year(self):
        keys = report_partition_keys(2024, 2025)
        self.assertEqual(len(keys), 8)
        self.assertEqual(keys[:4], ["2024-11013", "2024-11012", "2024-11014", "2024-11011"])


class DuckDBTestCase(TempDuckDBTestCase):
    def extra_env(self):
        return {"DART_API_KEY": "FAKEKEY"}

    def setUp(self):
        super().setUp()
        self.instance = self.enterContext(dg.instance_for_test())
        self.instance.add_dynamic_partitions("corp", [KB])


class PeriodicReportsOfTest(DuckDBTestCase):
    def test_lists_reports_in_order_and_latest_correction(self):
        with connect() as con:
            upsert_disclosures(con, [
                disclosure_row("20260313001191", "사업보고서 (2025.12)", "20260313"),
                disclosure_row("20260324000835", "[기재정정]사업보고서 (2025.12)", "20260324"),
                disclosure_row("20260619000667", "[기재정정]사업보고서 (2025.12)", "20260619"),
                disclosure_row("20250514000100", "분기보고서 (2025.03)", "20250514"),
                disclosure_row("20260320000001", "주주총회소집공고", "20260320"),
            ])
            rows = periodic_reports_of(con, KB)
            self.assertEqual([r["rcept_no"] for r in rows], ["20250514000100", "20260313001191", "20260324000835", "20260619000667"])
            self.assertEqual(latest_periodic_report(con, KB, "2025", "11011")["rcept_no"], "20260619000667")
            self.assertEqual(latest_periodic_report(con, KB, "2025", "11013")["rcept_no"], "20250514000100")
            self.assertIsNone(latest_periodic_report(con, KB, "2025", "11012"))
            self.assertEqual([r["rcept_no"] for r in periodic_reports_of(con, KB, "20260601", "20260630")], ["20260619000667"])


class ReportDetailsAssetTest(DuckDBTestCase):
    def materialize(self, report: str):
        return dg.materialize(
            [definitions.report_details],
            partition_key=dg.MultiPartitionKey({"corp": KB, "report": report}),
            instance=self.instance,
        )

    @mock.patch.object(definitions, "collect_report_details")
    def test_skips_without_api_calls_when_report_not_listed(self, collect):
        with connect() as con:
            con.execute("INSERT INTO corp VALUES (?, 'KB금융', '105560', NULL, now())", [KB])
        result = self.materialize("2025-11012")
        self.assertTrue(result.success)
        collect.assert_not_called()
        meta = result.asset_materializations_for_node("report_details")[0].metadata
        self.assertEqual(meta["api_calls"].value, 0)
        self.assertIn("skipped", meta)

    @mock.patch.object(definitions, "collect_report_details",
                       return_value={"api_calls": 4, "statuses": {}, "no_data": 0, "rcept_no": "20260619000667"})
    def test_collects_when_report_listed(self, collect):
        with connect() as con:
            con.execute("INSERT INTO corp VALUES (?, 'KB금융', '105560', NULL, now())", [KB])
            upsert_disclosures(con, [disclosure_row("20260313001191", "사업보고서 (2025.12)", "20260313"),
                                     disclosure_row("20260619000667", "[기재정정]사업보고서 (2025.12)", "20260619")])
        result = self.materialize("2025-11011")
        self.assertTrue(result.success)
        collect.assert_called_once()
        self.assertEqual(collect.call_args.args[2:], (KB, "2025", "11011"))
        meta = result.asset_materializations_for_node("report_details")[0].metadata
        self.assertEqual(meta["listed_rcept_no"].value, "20260619000667")
        self.assertTrue(meta["rcept_no_matches_listing"].value)


class RelationsAssetTest(DuckDBTestCase):
    def test_rebuilds_tables_from_raw_and_checks_pass(self):
        from src.collectors.disclosure_list import make_request_key
        from src.collectors.report_detail import ENDPOINTS

        key = make_request_key({"corp_code": KB, "bsns_year": "2025", "reprt_code": "11011", "rcept_no": "20260619000667"})
        with connect() as con:
            upsert_disclosures(con, [disclosure_row("20260619000667", "[기재정정]사업보고서 (2025.12)", "20260619")])
            for name, endpoint in ENDPOINTS.items():
                body = json.loads((FIXTURES / f"{endpoint.removesuffix('.json')}_{KB}_2025_11011.json").read_text(encoding="utf-8"))
                save_raw(con, endpoint, key, body)
        result = dg.materialize([definitions.relations, *definitions.RELATION_CHECKS], instance=self.instance)
        self.assertTrue(result.success)
        meta = result.asset_materializations_for_node("relations")[0].metadata
        self.assertEqual((meta["major_shareholder"].value, meta["major_shareholder_change"].value, meta["executive"].value), (1, 28, 32))
        self.assertTrue(all(ev.passed for ev in result.get_asset_check_evaluations()))
        with connect(read_only=True) as con:
            self.assertEqual(con.execute("SELECT valid_from, tenure_start_on FROM executive WHERE nm = '양종희'").fetchone(),
                             (date(2025, 12, 31), date(2023, 11, 30)))
            self.assertEqual(con.execute("SELECT count(*) FROM executive_current").fetchone()[0], 32)
        self.assertEqual(meta["person"].value, 32)


def report_disclosures_run(instance: dg.DagsterInstance, corp_code: str, month: str, reports: list[str]) -> None:
    """disclosures 파티션이 실행된 것처럼 메타데이터를 남긴다."""
    instance.report_runless_asset_event(
        dg.AssetMaterialization(
            asset_key=definitions.disclosures.key,
            partition=f"{corp_code}|{month}",
            metadata={"corp_code": corp_code, definitions.PERIODIC_REPORTS_METADATA_KEY: dg.MetadataValue.json(reports)},
        )
    )


class DailyReportDetailsScheduleTest(DuckDBTestCase):
    def evaluate(self, schedule, when: datetime) -> list:
        context = dg.build_schedule_context(instance=self.instance, scheduled_execution_time=when)
        return list(schedule(context))

    def test_skips_when_no_recent_periodic_reports(self):
        report_disclosures_run(self.instance, KB, "2026-06-01", [])
        results = self.evaluate(definitions.daily_report_details, datetime(2026, 6, 20, 7, 30, tzinfo=KST))
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], dg.SkipReason)

    def test_requests_partitions_from_disclosures_metadata_without_opening_duckdb(self):
        report_disclosures_run(self.instance, KB, "2026-06-01", ["2025-11011"])
        report_disclosures_run(self.instance, KB, "2026-05-01", ["2025-11013", "2025-11011"])  # 같은 보고서가 두 달에 걸쳐도 한 번
        report_disclosures_run(self.instance, "99999999", "2026-06-01", ["2025-11011"])  # 파티션에 없는 회사는 건너뜀
        # 다른 프로세스가 DuckDB에 쓰는 중이어도 스케줄 평가는 성공해야 한다
        import subprocess, sys
        proc = subprocess.Popen([sys.executable, "-c", HOLD_WRITER, self.db_path], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.assertEqual(proc.stdout.readline().strip(), "ready")
        try:
            results = self.evaluate(definitions.daily_report_details, datetime(2026, 6, 20, 7, 30, tzinfo=KST))
        finally:
            proc.stdin.close()
            proc.wait(timeout=10)
            proc.stdout.close()
        requests = [r for r in results if isinstance(r, dg.RunRequest)]
        self.assertEqual(sorted(r.partition_key for r in requests), [f"{KB}|2025-11011", f"{KB}|2025-11013"])
        self.assertEqual(sorted(r.run_key for r in requests), [f"2026-06-20|{KB}|2025-11011", f"2026-06-20|{KB}|2025-11013"])

    def test_relations_schedule_runs_only_after_report_details(self):
        when = datetime(2026, 6, 20, 8, 30, tzinfo=KST)
        results = self.evaluate(definitions.daily_relations, when)
        self.assertIsInstance(results[0], dg.SkipReason)
        self.instance.report_runless_asset_event(
            dg.AssetMaterialization(asset_key=definitions.report_details.key, partition=f"{KB}|2025-11011")
        )
        results = self.evaluate(definitions.daily_relations, when)
        self.assertIsInstance(results[0], dg.RunRequest)
        self.assertEqual(results[0].run_key, "relations|2026-06-20")


if __name__ == "__main__":
    unittest.main()
