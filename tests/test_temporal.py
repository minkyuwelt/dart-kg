"""이중 시간축 닫기(valid_to·superseded_by)와 사람 식별(person) 테스트. DART API를 호출하지 않는다.

보고서 4개를 가짜로 만들어 rebuild한 뒤, 0005의 규칙대로 닫혔는지와 시점 조회가 행 하나씩만 돌려주는지 본다.

    KB금융(A)  2024 사업보고서(기준일 2024-12-31)  → 2025 반기(2025-06-30) → 2025 사업보고서 원본·정정본(2025-12-31)
    국민은행(B) 2024 사업보고서(2024-12-31)         이재근이 A·B 양쪽에 나온다 (겸직)
"""

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

import duckdb

from src.collectors.disclosure_list import make_request_key
from src.collectors.report_detail import ENDPOINTS
from src.loaders.duckdb_store import connect, save_raw
from src.loaders.investments import ENDPOINT as INVESTMENT_ENDPOINT, rebuild_investments
from src.loaders.relations import make_person_id, rebuild_relations
from src.loaders.temporal import apply_person_merges, chain_changes, load_person_merges
from src.pipeline.definitions import check_relations_valid_from_sane
from tests.helpers import TempDuckDBTestCase

A, B = "00688996", "00401731"
YANG = ("양종희", "1961년 06월", "남")
LEE = ("이재근", "1966년 05월", "남")
HONG = ("홍길동", "1970년 01월", "남")


def executive(person, ofcps, hffc_pd, tenure_end_on, stlm_dt, rcept_no, corp_code=A) -> dict:
    nm, birth, sex = person
    return {"rcept_no": rcept_no, "corp_code": corp_code, "nm": nm, "sexdstn": sex, "birth_ym": birth, "ofcps": ofcps,
            "rgist_exctv_at": "사내이사", "fte_at": "상근", "hffc_pd": hffc_pd, "tenure_end_on": tenure_end_on, "stlm_dt": stlm_dt}


def shareholder(nm, rate, stlm_dt, rcept_no, corp_code=A) -> dict:
    return {"rcept_no": rcept_no, "corp_code": corp_code, "nm": nm, "relate": "본인", "stock_knd": "보통주",
            "trmend_posesn_stock_qota_rt": rate, "stlm_dt": stlm_dt}


def change(change_on, nm, rate, rcept_no, corp_code=A) -> dict:
    return {"rcept_no": rcept_no, "corp_code": corp_code, "change_on": change_on, "mxmm_shrholdr_nm": nm, "qota_rt": rate,
            "stlm_dt": "2025-12-31"}


def investment(inv_prm, rate, stlm_dt, rcept_no, corp_code=A) -> dict:
    return {"rcept_no": rcept_no, "corp_code": corp_code, "inv_prm": inv_prm, "frst_acqs_de": "2019.01.11",
            "trmend_blce_qota_rt": rate, "stlm_dt": stlm_dt}


# (회사, 사업연도, 보고서종류, 접수번호, 기준일)
REPORTS = [
    (A, "2024", "11011", "20250315000001", "2024-12-31"),
    (A, "2025", "11012", "20250814000001", "2025-06-30"),
    (A, "2025", "11011", "20260313000001", "2025-12-31"),  # 원본
    (A, "2025", "11011", "20260619000001", "2025-12-31"),  # 정정본 (접수번호가 더 큼)
    (B, "2024", "11011", "20250315000002", "2024-12-31"),
]
EXECUTIVES = {
    "20250315000001": [executive(YANG, "회장", "1년 1개월", "2026년 11월 20일", "2024-12-31", "20250315000001"),
                       executive(LEE, "기타비상무이사", "2년", "2024년 12월 31일", "2024-12-31", "20250315000001"),
                       executive(HONG, "사외이사", "3년", "2025년 03월 25일", "2024-12-31", "20250315000001")],
    "20250814000001": [executive(YANG, "회장", "1년 7개월", "2026년 11월 20일", "2025-06-30", "20250814000001"),
                       executive(LEE, "부문장", "6개월", "2025년 12월 31일", "2025-06-30", "20250814000001")],
    "20260313000001": [executive(YANG, "회장", "2년 1개월", "2026년 11월 20일", "2025-12-31", "20260313000001"),
                       executive(LEE, "부문장", "1년", "2025년 12월 31일", "2025-12-31", "20260313000001")],
    "20260619000001": [executive(YANG, "회장", "2년 1개월", "2026년 11월 20일", "2025-12-31", "20260619000001")],
    "20250315000002": [executive(LEE, "은행장", "3년", "2024년 12월 31일", "2024-12-31", "20250315000002", corp_code=B)],
}
SHAREHOLDERS = {
    "20250315000001": [shareholder("국민연금공단", "8.41", "2024-12-31", "20250315000001")],
    "20250814000001": [shareholder("국민연금공단", "8.50", "2025-06-30", "20250814000001")],
    "20260313000001": [shareholder("국민연금공단", "8.60", "2025-12-31", "20260313000001")],
    "20260619000001": [shareholder("국민연금공단", "8.68", "2025-12-31", "20260619000001")],
    "20250315000002": [shareholder("KB금융지주", "100", "2024-12-31", "20250315000002", corp_code=B)],
}
CHANGES = {
    "20260619000001": [change("2019년 12월 31일", "국민연금공단", "9.97%", "20260619000001"),
                       change("2022년 09월 30일", "국민연금공단", "7.97%", "20260619000001")],
}
INVESTMENTS = {
    "20250315000001": [investment("㈜국민은행", "100", "2024-12-31", "20250315000001")],
    "20260313000001": [investment("㈜국민은행", "100", "2025-12-31", "20260313000001")],
    "20260619000001": [investment("㈜국민은행", "99.9", "2025-12-31", "20260619000001")],
}


class TemporalCloseTest(TempDuckDBTestCase):
    def extra_env(self):
        return {"DART_API_KEY": "FAKEKEY"}

    def setUp(self):
        super().setUp()
        with connect() as con:
            con.execute("INSERT INTO corp VALUES (?, '국민은행', NULL, NULL, now())", [B])
            for corp, year, code, rcept, _ in REPORTS:
                key = make_request_key({"corp_code": corp, "bsns_year": year, "reprt_code": code, "rcept_no": rcept})
                for endpoint, rows in ((ENDPOINTS["executive"], EXECUTIVES), (ENDPOINTS["major_shareholder"], SHAREHOLDERS),
                                       (ENDPOINTS["major_shareholder_change"], CHANGES), (INVESTMENT_ENDPOINT, INVESTMENTS)):
                    if rcept in rows:
                        save_raw(con, endpoint, key, {"status": "000", "list": rows[rcept]})
            self.inv_stats = rebuild_investments(con)
            self.stats = rebuild_relations(con)

    def rows(self, sql, *params):
        with connect(read_only=True) as con:
            return con.execute(sql, list(params)).fetchall()

    def test_superseded_rows_point_to_latest_version(self):
        self.assertEqual(self.stats["executive_superseded"], 2)
        self.assertEqual(self.stats["major_shareholder_superseded"], 1)
        self.assertEqual(self.inv_stats["investment_superseded"], 1)
        self.assertEqual(
            self.rows("SELECT DISTINCT superseded_by FROM executive WHERE source_rcept_no = '20260313000001'"),
            [("20260619000001",)],
        )
        self.assertEqual(self.rows("SELECT count(*) FROM executive WHERE source_rcept_no = '20260619000001' AND superseded_by IS NOT NULL"), [(0,)])

    def test_snapshot_rows_chain_to_next_report(self):
        # 국민연금공단: 2024-12-31 → 2025-06-30 → 2025-12-31(정정본) → 열림
        self.assertEqual(
            self.rows("SELECT valid_from, valid_to, trmend_posesn_stock_qota_rt FROM major_shareholder "
                      "WHERE corp_code = ? AND superseded_by IS NULL ORDER BY valid_from", A),
            [(date(2024, 12, 31), date(2025, 6, 30), 8.41), (date(2025, 6, 30), date(2025, 12, 31), 8.5),
             (date(2025, 12, 31), None, 8.68)],
        )
        self.assertEqual(self.rows("SELECT trmend_posesn_stock_qota_rt FROM major_shareholder_current WHERE corp_code = ?", A), [(8.68,)])
        # 출자도 같은 방식. 정정본(99.9)이 현재
        self.assertEqual(
            self.rows("SELECT valid_from, valid_to, trmend_blce_qota_rt FROM investment WHERE superseded_by IS NULL ORDER BY valid_from"),
            [(date(2024, 12, 31), date(2025, 12, 31), 100.0), (date(2025, 12, 31), None, 99.9)],
        )

    def test_executive_absent_from_next_report_closes_at_tenure_end(self):
        # 홍길동: 2024 보고서에만 있고 임기만료 2025-03-25 < 다음 기준일 2025-06-30 → 임기만료일로 닫힘
        self.assertEqual(self.rows("SELECT valid_to FROM executive WHERE nm = '홍길동'"), [(date(2025, 3, 25),)])
        self.assertEqual(self.stats["executive_closed_by_tenure_end"], 1)
        # 양종희: 보고서마다 있으므로 기준일끼리 이어지고 마지막은 열림 (임기만료 2026-11-20은 미래)
        self.assertEqual(
            self.rows("SELECT valid_from, valid_to FROM executive WHERE nm = '양종희' AND superseded_by IS NULL ORDER BY valid_from"),
            [(date(2024, 12, 31), date(2025, 6, 30)), (date(2025, 6, 30), date(2025, 12, 31)), (date(2025, 12, 31), None)],
        )
        # 이재근: 정정본에서 빠짐 → 반기 행이 2025-12-31에 닫히고 현재 행 없음
        self.assertEqual(self.rows("SELECT count(*) FROM executive_current WHERE nm = '이재근' AND corp_code = ?", A), [(0,)])

    def test_as_of_query_returns_one_row_per_person(self):
        sql = ("SELECT nm, ofcps FROM executive WHERE corp_code = ? AND valid_from <= ? AND (valid_to IS NULL OR valid_to > ?) "
               "AND superseded_by IS NULL ORDER BY nm")
        self.assertEqual(self.rows(sql, A, date(2025, 5, 1), date(2025, 5, 1)), [("양종희", "회장"), ("이재근", "기타비상무이사")])
        self.assertEqual(self.rows(sql, A, date(2025, 9, 1), date(2025, 9, 1)), [("양종희", "회장"), ("이재근", "부문장")])
        self.assertEqual(self.rows(sql, A, date(2025, 2, 1), date(2025, 2, 1)),
                         [("양종희", "회장"), ("이재근", "기타비상무이사"), ("홍길동", "사외이사")])

    def test_tenure_start_is_separate_from_valid_from(self):
        self.assertEqual(
            self.rows("SELECT valid_from, tenure_start_on, tenure_start_method FROM executive WHERE nm = '양종희' AND source_rcept_no = '20260619000001'"),
            [(date(2025, 12, 31), date(2023, 11, 30), "stlm_dt_minus_hffc_pd")],
        )

    def test_changes_chain_by_change_date(self):
        self.assertEqual(
            self.rows("SELECT change_on, valid_to FROM major_shareholder_change ORDER BY change_on"),
            [(date(2019, 12, 31), date(2022, 9, 30)), (date(2022, 9, 30), None)],
        )

    def test_person_identity_links_same_person_across_companies(self):
        self.assertEqual(make_person_id("이재근", "1966-05", "남"), "이재근|1966-05|남")
        self.assertEqual(make_person_id("홍길동", None, None), "홍길동|?|?")
        self.assertEqual(self.stats["person"], 3)
        self.assertEqual(
            self.rows("SELECT person_id, corp_count, first_seen FROM person ORDER BY nm"),
            [("양종희|1961-06|남", 1, date(2023, 11, 30)), ("이재근|1966-05|남", 2, date(2021, 12, 31)), ("홍길동|1970-01|남", 1, date(2021, 12, 31))],
        )
        # 겸직: 같은 사람이 두 회사의 임원
        self.assertEqual(self.rows("SELECT DISTINCT corp_code FROM executive WHERE person_id = '이재근|1966-05|남' ORDER BY 1"), [(B,), (A,)])

    def test_rebuild_is_idempotent(self):
        with connect() as con:
            again = rebuild_relations(con)
            rebuild_investments(con)
        self.assertEqual(again, self.stats)


class SchemaMigrationTest(unittest.TestCase):
    """B 단계(PR #17)로 만들어진 DuckDB 파일을 열면 executive·investment 컬럼이 이전된다."""

    def test_old_file_gets_new_columns_on_connect(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = str(Path(tmp.name) / "old.duckdb")
        con = duckdb.connect(path)
        con.execute("CREATE TABLE executive (source_rcept_no VARCHAR, row_seq INTEGER, corp_code VARCHAR, nm VARCHAR, "
                    "valid_from DATE, valid_from_method VARCHAR NOT NULL, valid_to DATE, superseded_by VARCHAR, loaded_at TIMESTAMPTZ)")
        con.execute("CREATE TABLE investment (rcept_no VARCHAR, row_seq INTEGER, investor_corp_code VARCHAR, stlm_dt DATE, loaded_at TIMESTAMPTZ)")
        con.close()
        with mock.patch.dict(os.environ, {"DUCKDB_PATH": path}), connect() as con:
            exec_cols = {r[0] for r in con.execute("DESCRIBE executive").fetchall()}
            inv_cols = {r[0] for r in con.execute("DESCRIBE investment").fetchall()}
            self.assertNotIn("valid_from_method", exec_cols)
            self.assertTrue({"tenure_start_on", "tenure_start_method", "person_id"} <= exec_cols)
            self.assertTrue({"valid_from", "valid_to", "superseded_by"} <= inv_cols)
            self.assertEqual(con.execute("SELECT count(*) FROM executive_current").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()


class ChangeChainingTest(TempDuckDBTestCase):
    """변동 테이블만 따로: 같은 날 행이 여럿이면 다음 '날짜'로 닫히고, 변동일이 기준일보다 늦어도 검사를 통과한다."""

    def test_same_day_rows_close_at_next_date_not_same_day(self):
        with connect() as con:
            for change_on, nm, rcept in (("2015-02-12", "(주)외환은행", "20240814003949"), ("2015-02-12", "(주)하나은행", "20210517001611"),
                                        ("2017-06-12", "(주)하나은행", "20210517001611")):
                con.execute("INSERT INTO major_shareholder_change (corp_code, change_on, mxmm_shrholdr_nm, source_rcept_no, bsns_year, "
                            "reprt_code, stlm_dt, valid_from, loaded_at) VALUES (?, ?, ?, ?, '2020', '11011', DATE '2020-12-31', ?, now())",
                            [B, change_on, nm, rcept, change_on])
            chain_changes(con)
            rows = con.execute("SELECT change_on, mxmm_shrholdr_nm, valid_to FROM major_shareholder_change ORDER BY 1, 2").fetchall()
        self.assertEqual(rows, [(date(2015, 2, 12), "(주)외환은행", date(2017, 6, 12)), (date(2015, 2, 12), "(주)하나은행", date(2017, 6, 12)),
                                (date(2017, 6, 12), "(주)하나은행", None)])

    def test_change_after_report_date_passes_valid_from_check(self):
        # KB금융 2021 사업보고서(기준일 2021-12-31)에 실린 2022-01-27 변동. 정상이다
        with connect() as con:
            con.execute("INSERT INTO major_shareholder_change (corp_code, change_on, mxmm_shrholdr_nm, source_rcept_no, bsns_year, "
                        "reprt_code, stlm_dt, valid_from, loaded_at) VALUES (?, DATE '2022-01-27', '국민연금공단', '20220325001281', "
                        "'2021', '11011', DATE '2021-12-31', DATE '2022-01-27', now())", [A])
            result = check_relations_valid_from_sane(con)
        self.assertTrue(result.passed)


class PersonMergeTest(TempDuckDBTestCase):
    """config/person_merges.csv로 오타 때문에 갈라진 사람을 합친다 (0006 개정)."""

    def insert(self, con, person_id, stlm_dt, rcept, corp=A):
        nm, birth, sex = person_id.split("|")
        con.execute("INSERT INTO executive (source_rcept_no, row_seq, corp_code, bsns_year, reprt_code, nm, birth_ym, sexdstn, person_id, "
                    "stlm_dt, valid_from, loaded_at) VALUES (?, 1, ?, '2025', '11011', ?, ?, ?, ?, ?, ?, now())",
                    [rcept, corp, nm, birth, sex, person_id, stlm_dt, stlm_dt])

    def test_loads_pairs_into_groups_and_chains(self):
        path = Path(self.tmp.name) / "merges.csv"
        path.write_text("person_id_a,person_id_b,reason\n홍길동|1970-01|남,홍길동|1970-10|남,x\n홍길동|1970-10|남,홍길동|1970-01|여,y\n", encoding="utf-8")
        self.assertEqual(load_person_merges(path), [{"홍길동|1970-01|남", "홍길동|1970-10|남", "홍길동|1970-01|여"}])
        self.assertEqual(load_person_merges(Path(self.tmp.name) / "none.csv"), [])

    def test_canonical_is_majority_then_latest(self):
        with connect() as con:
            self.insert(con, "한윤철|1968-10|남", "2024-12-31", "20250301000001")
            self.insert(con, "한윤철|1968-10|남", "2025-06-30", "20250801000001")
            self.insert(con, "한윤철|1968-01|남", "2025-12-31", "20260301000001")
            # 동수면 더 최근 보고서 쪽
            self.insert(con, "민세진|1974-04|남", "2024-06-30", "20240801000001", corp=B)
            self.insert(con, "민세진|1974-04|여", "2024-12-31", "20250301000002", corp=B)
            changed = apply_person_merges(con, [{"한윤철|1968-10|남", "한윤철|1968-01|남"}, {"민세진|1974-04|남", "민세진|1974-04|여"}])
            ids = con.execute("SELECT DISTINCT person_id FROM executive ORDER BY 1").fetchall()
        self.assertEqual(changed, 2)
        self.assertEqual(ids, [("민세진|1974-04|여",), ("한윤철|1968-10|남",)])

    def test_rebuild_person_reads_attributes_from_person_id(self):
        with connect() as con:
            self.insert(con, "한윤철|1968-10|남", "2024-12-31", "20250301000001")
            self.insert(con, "한윤철|1968-01|남", "2025-12-31", "20260301000001")
            apply_person_merges(con, [{"한윤철|1968-10|남", "한윤철|1968-01|남"}])
            from src.loaders.temporal import rebuild_person
            self.assertEqual(rebuild_person(con), 1)
            # 1행씩 동수 → 더 최근 보고서(2025-12-31)의 표기가 기준
            self.assertEqual(con.execute("SELECT person_id, birth_ym, sexdstn, corp_count FROM person").fetchone(),
                             ("한윤철|1968-01|남", "1968-01", "남", 1))

    def test_real_config_file_parses(self):
        groups = load_person_merges()
        self.assertGreaterEqual(len(groups), 17)
        self.assertTrue(all(len(g) >= 2 for g in groups))
