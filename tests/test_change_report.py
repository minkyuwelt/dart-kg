"""변화 리포트(src/reports/changes.py) 테스트. 관계 테이블에 두 보고서의 스냅샷을 직접 넣고 비교 결과를 확인한다. API 호출 없음.

확인하는 것
1) 임원 신규·퇴임·직위 변경, 최대주주 지분율 변화(문턱 0.1%p)·명단 추가, 출자 대상 추가, 변동 보고, 정정 대체
2) 첫 보고서(직전 스냅샷 없음)는 "비교 대상 없음"으로, 신규로 쏟아내지 않는다
3) 기간·회사 필터, markdown에 생년월이 들어가지 않음, 요약 집계
"""

import unittest
from datetime import date

from src.loaders.duckdb_store import connect, upsert_disclosures
from src.reports.changes import build_report, render_markdown, summarize
from tests.test_report_details import disclosure_row
from tests.helpers import TempDuckDBTestCase

KB = "00688996"
R2024, R2025, R2025_FIX = "20250314000001", "20260313000002", "20260401000003"
D2024, D2025 = date(2024, 12, 31), date(2025, 12, 31)


def add_exec(con, rcept, nm, ofcps, kind, pid, stlm, valid_to=None, superseded_by=None, seq=None):
    con.execute(
        """INSERT INTO executive (source_rcept_no, row_seq, corp_code, bsns_year, reprt_code, nm, ofcps, rgist_exctv_at, birth_ym,
                                  person_id, stlm_dt, valid_from, valid_to, superseded_by, loaded_at)
           VALUES (?, ?, ?, ?, '11011', ?, ?, ?, '1961-06', ?, ?, ?, ?, ?, now())""",
        [rcept, seq if seq is not None else abs(hash((rcept, pid))) % 10000, KB, str(stlm.year), nm, ofcps, kind, pid, stlm, stlm, valid_to, superseded_by],
    )


def add_holder(con, rcept, nm, pct, stlm, valid_to=None, superseded_by=None, seq=0):
    con.execute(
        """INSERT INTO major_shareholder (source_rcept_no, row_seq, corp_code, bsns_year, reprt_code, nm, relate, stock_knd, resolve_method,
                                          trmend_posesn_stock_qota_rt, stlm_dt, valid_from, valid_to, superseded_by, loaded_at)
           VALUES (?, ?, ?, ?, '11011', ?, '본인', '보통주', 'exact', ?, ?, ?, ?, ?, now())""",
        [rcept, seq, KB, str(stlm.year), nm, pct, stlm, stlm, valid_to, superseded_by],
    )


def add_inv(con, rcept, name, pct, stlm, valid_to=None, seq=0):
    con.execute(
        """INSERT INTO investment (rcept_no, row_seq, investor_corp_code, bsns_year, reprt_code, inv_prm_raw, investee_name, resolve_method,
                                   trmend_blce_qota_rt, stlm_dt, valid_from, valid_to, loaded_at)
           VALUES (?, ?, ?, ?, '11011', ?, ?, 'exact', ?, ?, ?, ?, now())""",
        [rcept, seq, KB, str(stlm.year), name, name, pct, stlm, stlm, valid_to],
    )


class ChangeReportTest(TempDuckDBTestCase):
    def setUp(self):
        super().setUp()
        self.con = connect()
        self.addCleanup(self.con.close)
        con = self.con
        upsert_disclosures(con, [
            disclosure_row(R2024, "사업보고서 (2024.12)", "20250314"),
            disclosure_row(R2025, "사업보고서 (2025.12)", "20260313"),
            disclosure_row(R2025_FIX, "[기재정정]사업보고서 (2025.12)", "20260401"),
            disclosure_row("20260320000009", "주주총회소집공고", "20260320"),
        ])
        # 2024 스냅샷 (2025 보고서 기준일에 닫힘)
        add_exec(con, R2024, "양종희", "회장", "사내이사", "양종희|1961-06|남", D2024, valid_to=D2025, seq=1)
        add_exec(con, R2024, "김철수", "부사장", "미등기", "김철수|1965-01|남", D2024, valid_to=D2025, seq=2)
        add_exec(con, R2024, "이영희", "사외이사", "사외이사", "이영희|1960-03|여", D2024, valid_to=D2025, seq=3)
        add_holder(con, R2024, "국민연금공단", 8.3, D2024, valid_to=D2025)
        add_inv(con, R2024, "KB국민은행", 100.0, D2024, valid_to=D2025)
        # 2025 스냅샷
        add_exec(con, R2025, "양종희", "회장", "사내이사", "양종희|1961-06|남", D2025, seq=1)       # 그대로
        add_exec(con, R2025, "김철수", "사장", "사내이사", "김철수|1965-01|남", D2025, seq=2)       # 직위 변경
        add_exec(con, R2025, "박민수", "사외이사", "사외이사", "박민수|1970-07|남", D2025, seq=3)  # 신규 (이영희 퇴임)
        add_holder(con, R2025, "국민연금공단", 7.9, D2025)                                        # -0.40%p
        add_holder(con, R2025, "JP모간체이스", 5.1, D2025, seq=1)                                  # 명단 추가
        add_inv(con, R2025, "KB국민은행", 100.0, D2025)
        add_inv(con, R2025, "KB라이프생명", 100.0, D2025, seq=1)                                   # 출자 추가
        con.execute(
            """INSERT INTO major_shareholder_change (corp_code, change_on, mxmm_shrholdr_nm, qota_rt, change_cause, source_rcept_no,
                                                     bsns_year, reprt_code, stlm_dt, valid_from, loaded_at)
               VALUES (?, '2025-06-10', '국민연금공단', 7.9, '장내매도', ?, '2025', '11011', ?, '2025-06-10', now())""",
            [KB, R2025, D2025],
        )

    def test_detects_each_change_kind(self):
        results = build_report(self.con, date(2026, 3, 1), date(2026, 3, 31))
        self.assertEqual([r.rcept_no for r in results], [R2025])  # 주주총회소집공고는 관계 근거가 아니라 제외
        r = results[0]
        self.assertEqual((r.stlm_dt, r.prev_stlm_dt), (D2025, D2024))
        kinds = {(c.kind, c.subject) for c in r.changes}
        self.assertIn(("executive_new", "박민수 (사외이사)"), kinds)
        self.assertIn(("executive_gone", "이영희 (사외이사)"), kinds)
        self.assertIn(("executive_title", "김철수"), kinds)
        self.assertIn(("shareholder_pct", "국민연금공단 보통주"), kinds)
        self.assertIn(("shareholder_new", "JP모간체이스 보통주"), kinds)
        self.assertIn(("shareholder_change_event", "국민연금공단"), kinds)
        self.assertIn(("investment_new", "KB라이프생명"), kinds)
        self.assertEqual(r.count("investment_pct"), 0)  # 100% → 100%
        pct = next(c for c in r.changes if c.kind == "shareholder_pct")
        self.assertEqual((pct.before, pct.after, pct.note), ("8.30%", "7.90%", "-0.40%p"))
        self.assertEqual(summarize(results)["changes"], 7)

    def test_investment_table_absent_is_not_a_change(self):
        # 분기보고서가 출자 표를 싣지 않으면(자리 표시 행 '-' 하나) 직전 사업보고서의 출자가 전부 "제외"로 잡히면 안 된다 (2026-11-27)
        con = self.con
        RQ = "20261114000001"
        DQ = date(2026, 9, 30)
        upsert_disclosures(con, [disclosure_row(RQ, "분기보고서 (2026.09)", "20261114")])
        add_exec(con, RQ, "양종희", "회장", "사내이사", "양종희|1961-06|남", DQ, seq=1)
        add_inv(con, RQ, "-", None, DQ)
        r = next(x for x in build_report(con, date(2026, 11, 1), date(2026, 11, 30)) if x.rcept_no == RQ)
        self.assertEqual([c for c in r.changes if c.kind.startswith("investment")], [])
        self.assertEqual(r.count("executive_gone"), 2)   # 임원 표는 있으니 그쪽 비교는 그대로 (김철수·박민수 퇴임)

    def test_first_report_has_nothing_to_compare(self):
        results = build_report(self.con, date(2025, 3, 1), date(2025, 3, 31))
        self.assertEqual([r.rcept_no for r in results], [R2024])
        self.assertIsNone(results[0].prev_stlm_dt)
        self.assertEqual(results[0].changes, [])  # 신규 3명으로 쏟아내지 않는다
        self.assertIn("첫 보고서라 비교 대상 없음", render_markdown(results, date(2025, 3, 1), date(2025, 3, 31)))

    def test_revision_replaces_rows_and_is_counted_once(self):
        con = self.con
        # 정정본이 2025 임원 표를 대체: 원본 3행에 superseded_by, 정정본 행 3개(김철수 직위만 다시 '부사장')
        con.execute("UPDATE executive SET superseded_by = ? WHERE source_rcept_no = ?", [R2025_FIX, R2025])
        add_exec(con, R2025_FIX, "양종희", "회장", "사내이사", "양종희|1961-06|남", D2025, seq=1)
        add_exec(con, R2025_FIX, "김철수", "부사장", "미등기", "김철수|1965-01|남", D2025, seq=2)
        add_exec(con, R2025_FIX, "박민수", "사외이사", "사외이사", "박민수|1970-07|남", D2025, seq=3)
        results = build_report(self.con, date(2026, 3, 1), date(2026, 4, 30))
        by_rcept = {r.rcept_no: r for r in results}
        self.assertEqual(set(by_rcept), {R2025, R2025_FIX})
        fix = by_rcept[R2025_FIX]
        self.assertEqual(fix.count("revision"), 1)
        self.assertEqual(fix.count("executive_title"), 0)  # 정정본 기준으로는 직위가 그대로
        self.assertEqual(fix.count("executive_new"), 1)
        # 원본 보고서의 임원 변화는 정정본으로 옮겨 가고, 최대주주·출자 변화는 원본에 남는다
        self.assertEqual(by_rcept[R2025].count("executive_new"), 0)
        self.assertEqual(by_rcept[R2025].count("shareholder_pct"), 1)

    def test_markdown_has_links_and_no_birth_dates(self):
        results = build_report(self.con, date(2026, 3, 1), date(2026, 3, 31), corp_codes=[KB])
        md = render_markdown(results, date(2026, 3, 1), date(2026, 3, 31))
        self.assertIn(f"rcpNo={R2025}", md)
        self.assertIn("| 박민수 (사외이사) |", md)
        self.assertNotIn("1961", md)
        self.assertNotIn("1970-07", md)
        self.assertEqual(build_report(self.con, date(2026, 3, 1), date(2026, 3, 31), corp_codes=["00000000"]), [])


if __name__ == "__main__":
    unittest.main()
