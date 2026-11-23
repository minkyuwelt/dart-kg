"""Phase 2 관계 테이블(최대주주·변동·임원) 파서와 rebuild 테스트. DART API를 호출하지 않는다.

표본은 tests/fixtures/*_00688996_2025_11011.json (KB금융 2025 사업보고서 실제 응답, 2026-11-23 저장).
"""

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from src.collectors.disclosure_list import make_request_key
from src.collectors.report_detail import ENDPOINTS
from src.loaders import collect
from src.loaders.duckdb_store import connect, save_raw
from src.loaders.relations import (
    dedupe_changes,
    parse_date,
    parse_executives,
    parse_major_shareholder_changes,
    parse_major_shareholders,
    parse_percent,
    parse_period_months,
    parse_tenure,
    parse_year_month,
    rebuild_relations,
    shift_months,
)

FIXTURES = Path(__file__).parent / "fixtures"
KB = "00688996"
REQUEST_KEY = make_request_key({"corp_code": KB, "bsns_year": "2025", "reprt_code": "11011"})


def fixture(endpoint_name: str) -> dict:
    return json.loads((FIXTURES / f"{endpoint_name}_{KB}_2025_11011.json").read_text(encoding="utf-8"))


class ParserTest(unittest.TestCase):
    def test_parse_date_accepts_three_dart_formats(self):
        self.assertEqual(parse_date("2025-12-31"), date(2025, 12, 31))
        self.assertEqual(parse_date("2019년 12월 31일"), date(2019, 12, 31))
        self.assertEqual(parse_date("2019.01.11"), date(2019, 1, 11))
        self.assertEqual(parse_date("20190111"), date(2019, 1, 11))
        self.assertIsNone(parse_date("-"))
        self.assertIsNone(parse_date(""))
        self.assertIsNone(parse_date(None))
        self.assertIsNone(parse_date("2025년 13월 01일"))

    def test_parse_year_month(self):
        self.assertEqual(parse_year_month("1961년 06월"), "1961-06")
        self.assertEqual(parse_year_month("1966년 5월"), "1966-05")
        self.assertIsNone(parse_year_month("-"))

    def test_parse_percent_with_and_without_sign(self):
        self.assertEqual(parse_percent("9.97%"), 9.97)
        self.assertEqual(parse_percent("8.68"), 8.68)
        self.assertIsNone(parse_percent("-"))

    def test_parse_period_months_handles_whitespace_variants(self):
        self.assertEqual(parse_period_months("2년 \n1개월"), 25)
        self.assertEqual(parse_period_months("1년8개월"), 20)
        self.assertEqual(parse_period_months("3년\n9개월"), 45)
        self.assertEqual(parse_period_months("9개월"), 9)
        self.assertEqual(parse_period_months("5년"), 60)
        self.assertIsNone(parse_period_months("-"))
        self.assertIsNone(parse_period_months(""))
        self.assertIsNone(parse_period_months("미상"))
        self.assertEqual(parse_period_months("3.2년"), 38)
        self.assertEqual(parse_period_months("2.5년\n(초임)"), 30)

    def test_parse_tenure_reads_date_ranges_seen_in_backfill(self):
        # 백필 데이터의 실제 표기 (devlog 2026-11-23). 85%가 기간이 아니라 날짜 범위였다
        d = date(2025, 12, 31)
        cases = {
            "2022.01.07~": (date(2022, 1, 7), None, 47),
            "2021.7.1\n ~\n 기준일": (date(2021, 7, 1), None, 53),
            "'23.3.22\n~ 현재": (date(2023, 3, 22), None, 33),
            "24.05.27.\n ~ 현재": (date(2024, 5, 27), None, 19),
            "2017.09.\n22~현재": (date(2017, 9, 22), None, 99),
            "2019.03.21~현재\n (FY2023 정기주총일)": (date(2019, 3, 21), None, 81),
            "2022.01.01\n ~ 2023.12.31": (date(2022, 1, 1), date(2023, 12, 31), 47),
            "2022. 4. 22\n~ 기준일": (date(2022, 4, 22), None, 44),
            "2023.03.22\n-\n현재": (date(2023, 3, 22), None, 33),
            "2020.12.07": (date(2020, 12, 7), None, 60),
        }
        for raw, (start, end, months) in cases.items():
            with self.subTest(raw=raw):
                t = parse_tenure(raw, d)
                self.assertEqual((t["tenure_start_on"], t["range_end_on"], t["hffc_months"], t["tenure_start_method"]),
                                 (start, end, months, "hffc_pd_range"))
        self.assertEqual(parse_tenure("2년 \n1개월", d)["tenure_start_method"], "stlm_dt_minus_hffc_pd")
        self.assertEqual(parse_tenure("2년 \n1개월", d)["tenure_start_on"], date(2023, 11, 30))
        for raw in ("기재생략", "-", "", "2022.01.00~현재"):
            self.assertIsNone(parse_tenure(raw, d)["tenure_start_on"], raw)

    def test_shift_months_clamps_to_month_end(self):
        self.assertEqual(shift_months(date(2025, 12, 31), -25), date(2023, 11, 30))
        self.assertEqual(shift_months(date(2025, 12, 31), -1), date(2025, 11, 30))
        self.assertEqual(shift_months(date(2024, 1, 31), 1), date(2024, 2, 29))
        self.assertEqual(shift_months(date(2025, 6, 30), -18), date(2023, 12, 30))


class ParseFixtureTest(unittest.TestCase):
    """실제 응답 표본으로 행이 기대대로 나오는지."""

    def test_major_shareholders_skip_total_rows_and_resolve_holder(self):
        index = {"국민연금공단": ["99999999"]}  # 연결이 되는 경우를 보기 위한 가짜 색인
        rows = parse_major_shareholders(REQUEST_KEY, fixture("hyslrSttus"), index, {})
        self.assertEqual(len(rows), 1)  # 3행 중 '계' 2행 제외
        row = rows[0]
        self.assertEqual(row["source_rcept_no"], "20260619000667")
        self.assertEqual(row["corp_code"], KB)
        self.assertEqual(row["nm"], "국민연금공단")
        self.assertEqual(row["relate"], "본인 및 특별관계자")
        self.assertEqual((row["holder_corp_code"], row["resolve_method"]), ("99999999", "exact"))
        self.assertEqual(row["trmend_posesn_stock_co"], 33107971)
        self.assertEqual(row["trmend_posesn_stock_qota_rt"], 8.68)
        self.assertEqual(row["stlm_dt"], date(2025, 12, 31))
        self.assertEqual(row["valid_from"], date(2025, 12, 31))
        self.assertIsNone(row["rm"])  # '-'는 NULL
        self.assertEqual((row["bsns_year"], row["reprt_code"]), ("2025", "11011"))

    def test_major_shareholder_unresolved_name_is_kept(self):
        rows = parse_major_shareholders(REQUEST_KEY, fixture("hyslrSttus"), {}, {})
        self.assertEqual((rows[0]["holder_corp_code"], rows[0]["resolve_method"]), (None, "not_found"))

    def test_major_shareholder_changes_have_day_level_dates(self):
        rows = parse_major_shareholder_changes(REQUEST_KEY, fixture("hyslrChgSttus"))
        self.assertEqual(len(rows), 28)
        first = rows[0]
        self.assertEqual(first["change_on"], date(2019, 12, 31))
        self.assertEqual(first["valid_from"], date(2019, 12, 31))
        self.assertEqual(first["mxmm_shrholdr_nm"], "국민연금공단")
        self.assertEqual(first["qota_rt"], 9.97)  # '9.97%'
        self.assertEqual(first["posesn_stock_co"], 41468003)
        self.assertEqual(first["change_cause"], "장내매매")
        self.assertTrue(all(r["source_rcept_no"] == "20260619000667" for r in rows))

    def test_executives_estimate_tenure_start_by_tenure(self):
        rows = parse_executives(REQUEST_KEY, fixture("exctvSttus"))
        self.assertEqual(len(rows), 32)
        chairman = next(r for r in rows if r["nm"] == "양종희")
        self.assertEqual(chairman["birth_ym"], "1961-06")
        self.assertEqual(chairman["person_id"], "양종희|1961-06|남")
        self.assertEqual((chairman["ofcps"], chairman["rgist_exctv_at"], chairman["fte_at"]), ("회장", "사내이사", "상근"))
        self.assertEqual(chairman["hffc_pd_raw"], "2년 \n1개월")
        self.assertEqual(chairman["hffc_months"], 25)
        self.assertEqual(chairman["valid_from"], date(2025, 12, 31))  # 보고서 기준일 (스냅샷 체인)
        self.assertEqual(chairman["tenure_start_on"], date(2023, 11, 30))  # 2025-12-31 - 25개월
        self.assertEqual(chairman["tenure_start_method"], "stlm_dt_minus_hffc_pd")
        self.assertEqual(chairman["tenure_end_on"], date(2026, 11, 20))
        self.assertIsNone(chairman["mxmm_shrholdr_relate"])  # '-'
        self.assertIn("대표이사", chairman["chrg_job"])
        # 모든 행이 재직기간을 읽었고, 선임 시점이 기준일보다 앞선다
        self.assertTrue(all(r["tenure_start_method"] == "stlm_dt_minus_hffc_pd" for r in rows))
        self.assertTrue(all(r["tenure_start_on"] < r["stlm_dt"] for r in rows))

    def test_executive_without_tenure_has_no_start_estimate(self):
        body = {"status": "000", "list": [{"rcept_no": "20260101000001", "corp_code": KB, "nm": "홍길동", "hffc_pd": "-", "stlm_dt": "2025-12-31"}]}
        (row,) = parse_executives(REQUEST_KEY, body)
        self.assertEqual((row["valid_from"], row["tenure_start_on"], row["tenure_start_method"], row["hffc_months"]),
                         (date(2025, 12, 31), None, None, None))
        self.assertEqual(row["person_id"], "홍길동|?|?")

    def test_executive_date_range_gives_exact_start_and_fills_missing_end(self):
        body = {"status": "000", "list": [
            {"rcept_no": "20260101000001", "corp_code": KB, "nm": "홍길동", "hffc_pd": "2022.01.01\n ~ 2023.12.31", "tenure_end_on": "-", "stlm_dt": "2025-12-31"},
            {"rcept_no": "20260101000001", "corp_code": KB, "nm": "김철수", "hffc_pd": "'23.3.22\n~ 현재", "tenure_end_on": "2026년 03월 25일", "stlm_dt": "2025-12-31"},
            {"rcept_no": "20260101000001", "corp_code": KB, "nm": "-", "hffc_pd": "-", "stlm_dt": "2025-12-31"},  # 자리 표시 행은 건너뜀
        ]}
        hong, kim = parse_executives(REQUEST_KEY, body)
        self.assertEqual((hong["tenure_start_on"], hong["tenure_start_method"], hong["tenure_end_on"]),
                         (date(2022, 1, 1), "hffc_pd_range", date(2023, 12, 31)))
        self.assertEqual((kim["tenure_start_on"], kim["tenure_end_on"]), (date(2023, 3, 22), date(2026, 3, 25)))  # API 임기만료일 우선


class MajorShareholderPlaceholderTest(unittest.TestCase):
    def test_dash_row_is_skipped(self):
        body = {"status": "000", "list": [
            {"rcept_no": "20260101000001", "corp_code": KB, "nm": "-", "relate": "-", "stlm_dt": "2025-12-31"},
            {"rcept_no": "20260101000001", "corp_code": KB, "nm": "국민연금공단", "relate": "최대주주", "trmend_posesn_stock_qota_rt": "8.68", "stlm_dt": "2025-12-31"},
        ]}
        rows = parse_major_shareholders(REQUEST_KEY, body, {}, {})
        self.assertEqual([r["nm"] for r in rows], ["국민연금공단"])


class DedupeChangesTest(unittest.TestCase):
    def test_keeps_earliest_report_for_same_change(self):
        rows = [
            {"corp_code": KB, "change_on": date(2019, 12, 31), "mxmm_shrholdr_nm": "국민연금공단", "source_rcept_no": "20260619000667"},
            {"corp_code": KB, "change_on": date(2019, 12, 31), "mxmm_shrholdr_nm": "국민연금공단", "source_rcept_no": "20200330001234"},
            {"corp_code": KB, "change_on": date(2022, 9, 30), "mxmm_shrholdr_nm": "국민연금공단", "source_rcept_no": "20260619000667"},
        ]
        kept = dedupe_changes(rows)
        self.assertEqual(len(kept), 2)
        by_date = {r["change_on"]: r["source_rcept_no"] for r in kept}
        self.assertEqual(by_date[date(2019, 12, 31)], "20200330001234")
        self.assertEqual(by_date[date(2022, 9, 30)], "20260619000667")

    def test_same_change_with_different_corp_marks_is_one(self):
        # 백필에서 실제로 있었던 사례: 같은 변동이 보고서마다 '하나은행', '(주)하나은행'으로 적힘
        rows = [
            {"corp_code": KB, "change_on": date(2017, 6, 12), "mxmm_shrholdr_nm": "(주)하나은행", "source_rcept_no": "20210517001611"},
            {"corp_code": KB, "change_on": date(2017, 6, 12), "mxmm_shrholdr_nm": "하나은행", "source_rcept_no": "20180402000001"},
            {"corp_code": KB, "change_on": date(2017, 6, 12), "mxmm_shrholdr_nm": "(주)외환은행", "source_rcept_no": "20240814003949"},
        ]
        kept = sorted(dedupe_changes(rows), key=lambda r: r["mxmm_shrholdr_nm"])
        self.assertEqual([(r["mxmm_shrholdr_nm"], r["source_rcept_no"]) for r in kept],
                         [("(주)외환은행", "20240814003949"), ("하나은행", "20180402000001")])  # 다른 이름은 남음


class RebuildTest(unittest.TestCase):
    """임시 DuckDB에 원본 응답을 넣고 관계 테이블을 다시 만든다."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(self.tmp.name) / "test.duckdb"), "DART_API_KEY": "FAKEKEY"})
        env.start()
        self.addCleanup(env.stop)

    def seed_raw(self, con):
        for name, endpoint in ENDPOINTS.items():
            save_raw(con, endpoint, REQUEST_KEY, fixture(endpoint.removesuffix(".json")))

    def test_rebuild_fills_three_tables_and_is_idempotent(self):
        with connect() as con:
            self.seed_raw(con)
            # 2024 보고서에도 같은 변동(2019-12-31)이 실렸다고 가정: 먼저 보고한 2024 접수번호가 남아야 한다
            older = {"status": "000", "list": [dict(fixture("hyslrChgSttus")["list"][0], rcept_no="20250314000100")]}
            save_raw(con, ENDPOINTS["major_shareholder_change"],
                     make_request_key({"corp_code": KB, "bsns_year": "2024", "reprt_code": "11011"}), older)
            # 데이터 없음(013) 응답은 무시된다
            save_raw(con, ENDPOINTS["executive"],
                     make_request_key({"corp_code": "00000001", "bsns_year": "2025", "reprt_code": "11011"}),
                     {"status": "013", "message": "조회된 데이타가 없습니다."})

            first = rebuild_relations(con)
            second = rebuild_relations(con)
            self.assertEqual(first, second)
            self.assertEqual(first["major_shareholder"], 1)
            self.assertEqual(first["major_shareholder_change"], 28)
            self.assertEqual(first["executive"], 32)
            self.assertEqual(first["holder_by_method"], {"not_found": 1})

            self.assertEqual(
                con.execute("SELECT source_rcept_no FROM major_shareholder_change WHERE change_on = DATE '2019-12-31'").fetchone()[0],
                "20250314000100",
            )
            self.assertEqual(con.execute("SELECT count(*) FROM executive WHERE valid_to IS NOT NULL OR superseded_by IS NOT NULL").fetchone()[0], 0)
            self.assertEqual(
                con.execute("SELECT valid_from, tenure_start_on, tenure_end_on FROM executive WHERE nm = '양종희'").fetchone(),
                (date(2025, 12, 31), date(2023, 11, 30), date(2026, 11, 20)),
            )
            self.assertEqual(first["person"], 32)

    def test_rebuild_keeps_each_report_version_once(self):
        """같은 보고서를 파라미터 키와 rcept_no 키로 두 번 저장해도 한 번만, 정정본(다른 rcept_no)은 따로 남는다."""
        with connect() as con:
            self.seed_raw(con)
            versioned = make_request_key({"corp_code": KB, "bsns_year": "2025", "reprt_code": "11011", "rcept_no": "20260619000667"})
            save_raw(con, ENDPOINTS["executive"], versioned, fixture("exctvSttus"))
            self.assertEqual(rebuild_relations(con)["executive"], 32)
            original = {"status": "000", "list": [dict(r, rcept_no="20260313001191") for r in fixture("exctvSttus")["list"][:5]]}
            save_raw(con, ENDPOINTS["executive"],
                     make_request_key({"corp_code": KB, "bsns_year": "2025", "reprt_code": "11011", "rcept_no": "20260313001191"}), original)
            self.assertEqual(rebuild_relations(con)["executive"], 37)
            self.assertEqual(
                con.execute("SELECT source_rcept_no, count(*) FROM executive GROUP BY 1 ORDER BY 1").fetchall(),
                [("20260313001191", 5), ("20260619000667", 32)],
            )

    def test_rebuild_resolves_holder_against_corp_table(self):
        with connect() as con:
            con.execute("INSERT INTO corp VALUES ('00688996', 'KB금융', '105560', NULL, now())")
            # 한글 읽기 매칭(㈜KB손해보험 → 케이비손해보험)과 수동 매핑(KB금융지주 → 00688996, config/corp_name_overrides.csv)
            body = {"status": "000", "list": [
                {"rcept_no": "20260320000001", "corp_code": "00401731", "nm": "㈜KB손해보험", "relate": "본인",
                 "trmend_posesn_stock_qota_rt": "100", "stlm_dt": "2025-12-31"},
                {"rcept_no": "20260320000001", "corp_code": "00401731", "nm": "주식회사KB금융지주", "relate": "기타",
                 "trmend_posesn_stock_qota_rt": "0", "stlm_dt": "2025-12-31"},
            ]}
            con.execute("INSERT INTO corp VALUES ('00000002', '케이비손해보험', NULL, NULL, now())")
            save_raw(con, ENDPOINTS["major_shareholder"],
                     make_request_key({"corp_code": "00401731", "bsns_year": "2025", "reprt_code": "11011"}), body)
            rebuild_relations(con)
            self.assertEqual(
                con.execute("SELECT holder_corp_code, resolve_method, trmend_posesn_stock_qota_rt FROM major_shareholder ORDER BY row_seq").fetchall(),
                [("00000002", "hangul", 100.0), ("00688996", "override", 0.0)],
            )


class CollectReportDetailsTest(unittest.TestCase):
    """collect_report_details가 API 4건을 부르고 원본 4건을 저장하는지 (requests를 가짜로 바꿈)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(self.tmp.name) / "test.duckdb")})
        env.start()
        self.addCleanup(env.stop)

    def test_saves_four_raw_responses_in_one_call(self):
        bodies = {
            "otrCprInvstmntSttus.json": {"status": "013", "message": "조회된 데이타가 없습니다."},
            "hyslrSttus.json": fixture("hyslrSttus"),
            "hyslrChgSttus.json": fixture("hyslrChgSttus"),
            "exctvSttus.json": fixture("exctvSttus"),
        }

        def fake_get_json(endpoint, api_key, params):
            self.assertEqual(params, {"corp_code": KB, "bsns_year": "2025", "reprt_code": "11011"})
            return bodies[endpoint]

        with mock.patch("src.collectors.investment.get_json", side_effect=fake_get_json), \
             mock.patch("src.collectors.report_detail.get_json", side_effect=fake_get_json), \
             connect() as con:
            result = collect.collect_report_details(con, "FAKEKEY", KB, "2025", "11011")
            self.assertEqual(result["api_calls"], 4)
            self.assertEqual(result["no_data"], 1)
            self.assertEqual(result["statuses"]["executive"], "000")
            self.assertEqual(result["rcept_no"], "20260619000667")
            saved = con.execute("SELECT endpoint, status, request_key FROM raw_api_response ORDER BY endpoint").fetchall()
            self.assertEqual([(e, s) for e, s, _ in saved], [("exctvSttus.json", "000"), ("hyslrChgSttus.json", "000"),
                                                             ("hyslrSttus.json", "000"), ("otrCprInvstmntSttus.json", "013")])
            # 정상 응답은 rcept_no가 키에 들어가 정정본과 구분되고, 013 응답은 파라미터만
            self.assertTrue(all("rcept_no=20260619000667" in k for _, s, k in saved if s == "000"))
            self.assertEqual(saved[-1][2], REQUEST_KEY)


if __name__ == "__main__":
    unittest.main()
