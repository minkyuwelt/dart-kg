"""적재 공통 함수 테스트: 값 파싱(parsing.py), 비우고 다시 채우기·트랜잭션(duckdb_store.py), 스냅샷 닫기 한 함수(temporal.py).

코드 리뷰 B3 정리에서 세 모듈이 각자 들고 있던 것을 한곳으로 모았다. 모은 뒤에도 전과 같이 동작하는지 여기서 고정한다. DART API를 호출하지 않는다.
"""

import unittest
from datetime import date

import duckdb

from src.loaders.duckdb_store import replace_rows, transaction
from src.loaders.parsing import is_placeholder, parse_date, parse_percent, text_or_none, to_number
from src.loaders.temporal import SNAPSHOT_TABLES, close_snapshot
from tests.helpers import TempDuckDBTestCase


class ParsingTest(unittest.TestCase):
    def test_placeholder_rows_are_recognised_with_or_without_spaces(self):
        for name in ("-", "해당없음", "해당 사항 없음", "기재 생략", "없음"):
            self.assertTrue(is_placeholder(name), name)
        for name in ("양종희", "국민연금공단", "없음상사"):
            self.assertFalse(is_placeholder(name), name)

    def test_text_or_none_treats_dash_as_empty(self):
        self.assertIsNone(text_or_none(" - "))
        self.assertIsNone(text_or_none(None))
        self.assertEqual(text_or_none(" 본인 "), "본인")

    def test_number_date_percent(self):
        self.assertEqual(to_number("33,107,971"), 33107971.0)
        self.assertEqual(parse_date("2019.1.11"), date(2019, 1, 11))
        self.assertEqual(parse_percent("9.97%"), 9.97)


class ReplaceRowsTest(unittest.TestCase):
    def setUp(self):
        self.con = duckdb.connect()
        self.con.execute("CREATE TABLE t (a VARCHAR, n BIGINT, d DATE, loaded_at TIMESTAMP)")

    def test_replaces_everything_and_applies_sql_expressions(self):
        self.con.execute("INSERT INTO t VALUES ('old', 1, NULL, now())")
        rows = [{"a": "x", "n": "1,000", "d": "2019.01.11"}, {"a": "y", "n": "-", "d": "-"}]
        n = replace_rows(self.con, "t", rows, ["a", "n", "d"],
                         {"n": "TRY_CAST(REPLACE(NULLIF(n, '-'), ',', '') AS BIGINT)", "d": "TRY_STRPTIME(NULLIF(d, '-'), '%Y.%m.%d')::DATE"})
        self.assertEqual(n, 2)
        self.assertEqual(self.con.execute("SELECT a, n, d FROM t ORDER BY a").fetchall(), [("x", 1000, date(2019, 1, 11)), ("y", None, None)])
        self.assertEqual(self.con.execute("SELECT count(*) FROM t WHERE loaded_at IS NULL").fetchone()[0], 0)

    def test_empty_rows_only_clears(self):
        self.con.execute("INSERT INTO t VALUES ('old', 1, NULL, now())")
        self.assertEqual(replace_rows(self.con, "t", [], ["a", "n", "d"]), 0)
        self.assertEqual(self.con.execute("SELECT count(*) FROM t").fetchone()[0], 0)

    def test_python_dates_and_nones_go_in_as_is(self):
        rows = [{"a": "x", "n": 3, "d": date(2025, 12, 31)}, {"a": None, "n": None, "d": None}]
        replace_rows(self.con, "t", rows, ["a", "n", "d"])
        self.assertEqual(self.con.execute("SELECT a, n, d FROM t ORDER BY a NULLS LAST").fetchall(), [("x", 3, date(2025, 12, 31)), (None, None, None)])


class TransactionTest(unittest.TestCase):
    def test_commit_and_rollback(self):
        con = duckdb.connect()
        con.execute("CREATE TABLE t (a INTEGER)")
        with transaction(con):
            con.execute("INSERT INTO t VALUES (1)")
        self.assertEqual(con.execute("SELECT count(*) FROM t").fetchone()[0], 1)
        with self.assertRaises(RuntimeError):
            with transaction(con):
                con.execute("INSERT INTO t VALUES (2)")
                raise RuntimeError("중간에 실패")
        self.assertEqual(con.execute("SELECT count(*) FROM t").fetchone()[0], 1)  # 2는 되돌려짐


class CloseSnapshotTest(TempDuckDBTestCase):
    """SNAPSHOT_TABLES의 네 표가 모두 실제 스키마에 있고, close_snapshot이 정정본·다음 기준일을 전과 같이 적는다."""

    def test_every_snapshot_table_exists_with_its_columns(self):
        from src.loaders.duckdb_store import connect
        with connect() as con:
            for table, (rcept_col, corp_col) in SNAPSHOT_TABLES.items():
                cols = {r[0] for r in con.execute(f"DESCRIBE {table}").fetchall()}
                self.assertTrue({rcept_col, corp_col, "bsns_year", "reprt_code", "stlm_dt", "valid_to", "superseded_by"} <= cols, table)

    def test_close_snapshot_marks_superseded_and_chains_valid_to(self):
        from src.loaders.duckdb_store import connect
        from src.loaders.investments import INVESTMENT_COLUMNS
        base = {"row_seq": 1, "investor_corp_code": "A", "inv_prm_raw": "x", "investee_name": "x", "share_class": None,
                "investee_corp_code": None, "resolve_method": "not_found", "invstmnt_purps": None, "frst_acqs_de": "-",
                "trmend_blce_qy": None, "trmend_blce_qota_rt": None, "trmend_blce_acntbk_amount": None}
        rows = [
            {**base, "rcept_no": "20250315000001", "bsns_year": "2024", "reprt_code": "11011", "stlm_dt": "2024-12-31", "valid_from": "2024-12-31"},
            {**base, "rcept_no": "20260313000001", "bsns_year": "2025", "reprt_code": "11011", "stlm_dt": "2025-12-31", "valid_from": "2025-12-31"},
            {**base, "rcept_no": "20260619000001", "bsns_year": "2025", "reprt_code": "11011", "stlm_dt": "2025-12-31", "valid_from": "2025-12-31"},  # 정정본
        ]
        from src.loaders.investments import INVESTMENT_EXPRESSIONS
        with connect() as con, transaction(con):
            replace_rows(con, "investment", rows, INVESTMENT_COLUMNS, INVESTMENT_EXPRESSIONS)
            self.assertEqual(close_snapshot(con, "investment"), 1)
            got = con.execute("SELECT rcept_no, superseded_by, valid_to FROM investment ORDER BY rcept_no").fetchall()
        self.assertEqual(got, [("20250315000001", None, date(2025, 12, 31)),
                               ("20260313000001", "20260619000001", None),
                               ("20260619000001", None, None)])


if __name__ == "__main__":
    unittest.main()
