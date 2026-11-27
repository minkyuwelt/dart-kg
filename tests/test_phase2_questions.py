"""notebooks/phase2_questions.py(F 단계 검증 스크립트)가 가짜 데이터에서 끝까지 돌고, 시점 조회가 맞는 행을 고르는지 본다.

tests/test_temporal.py의 가짜 보고서 4개를 그대로 쓴다 (KB금융 A: 2024 사업 → 2025 반기 → 2025 사업 원본·정정본, 국민은행 B).
DART API를 호출하지 않는다.
"""

import io
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from unittest import mock

from src.collectors.disclosure_list import make_request_key
from src.collectors.report_detail import ENDPOINTS
from src.loaders.duckdb_store import connect, save_raw
from src.loaders.investments import ENDPOINT as INVESTMENT_ENDPOINT, rebuild_investments
from src.loaders.relations import rebuild_relations
from tests.test_temporal import A, B, CHANGES, EXECUTIVES, INVESTMENTS, REPORTS, SHAREHOLDERS
from tests.helpers import TempDuckDBTestCase

from notebooks import phase2_questions as pq


class Phase2QuestionsTest(TempDuckDBTestCase):
    def extra_env(self):
        return {"DART_API_KEY": "FAKEKEY"}

    def setUp(self):
        super().setUp()
        with connect() as con:
            con.execute("INSERT INTO corp VALUES (?, '국민은행', NULL, NULL, now())", [B])
            con.execute("INSERT INTO target_corp VALUES (?, 'KB금융', 'holding', NULL, NULL, NULL, '2025', now())", [A])
            con.execute("INSERT INTO target_corp VALUES (?, '국민은행', 'subsidiary', ?, 100, '20260619000001', '2025', now())", [B, A])
            con.execute("INSERT INTO disclosure VALUES ('20260619000001', ?, 'KB금융', NULL, 'Y', '[기재정정]사업보고서 (2025.12)', 'KB금융', DATE '2026-06-19', '연', now())", [A])
            for corp, year, code, rcept, _ in REPORTS:
                key = make_request_key({"corp_code": corp, "bsns_year": year, "reprt_code": code, "rcept_no": rcept})
                for endpoint, rows in ((ENDPOINTS["executive"], EXECUTIVES), (ENDPOINTS["major_shareholder"], SHAREHOLDERS),
                                       (ENDPOINTS["major_shareholder_change"], CHANGES), (INVESTMENT_ENDPOINT, INVESTMENTS)):
                    if rcept in rows:
                        save_raw(con, endpoint, key, {"status": "000", "list": rows[rcept]})
            rebuild_investments(con)
            rebuild_relations(con)

    def run_script(self, out_path=None) -> str:
        buf = io.StringIO()
        out = pq.Report(out_path)
        with redirect_stdout(buf), connect(read_only=True) as con:
            pq.run(con, out)
        out.close()
        return buf.getvalue()

    def test_runs_end_to_end_and_writes_file(self):
        out_path = str(Path(self.tmp.name) / "result.txt")
        text = self.run_script(out_path)
        for marker in ("[1-1]", "[1-4]", "[Q1]", "[Q2]", "[Q3]", "[Q4]", "[Q5]"):
            self.assertIn(marker, text)
        self.assertEqual(Path(out_path).read_text(encoding="utf-8"), text)
        # 질문의 대상 회사가 target_corp에 없으면 건너뛴다고 적고 멈추지 않는다
        self.assertIn("'신한은행'이 없어 건너뜀", text)

    def test_as_of_query_picks_one_report(self):
        # 국민은행을 Q1·Q2 대상으로 바꾸면 2024 사업보고서(기준일 2024-12-31) 행만 나와야 한다
        with mock.patch.multiple(pq, Q1_BANK="국민은행", Q1_AS_OF=date(2025, 3, 1), Q2_BANK="국민은행", Q2_AS_OF=date(2025, 3, 1)):
            text = self.run_script()
        self.assertIn("KB금융지주", text)          # Q1: 국민은행 최대주주
        self.assertIn("20250315000002", text)      # 근거 접수번호
        self.assertIn(pq.DART_VIEWER + "20250315000002", text)
        self.assertIn("두 번 나온 사람: 없음", text)

    def test_corrections_section_shows_diff(self):
        text = self.run_script()
        # 정정본에서 지분율 8.60 → 8.68, 이재근이 빠짐
        self.assertIn("8.6", text)
        self.assertIn("8.68", text)
        self.assertIn("이재근|1966-05|남", text)
        self.assertIn("20260619000001", text)


if __name__ == "__main__":
    unittest.main()
