"""0014: 지주 사업보고서 IX절 "계열회사간 임원 겸직 현황" 표 → concurrent_position → HOLDS_POSITION.

네 지주의 열 이름이 다 다르다(KB 성명/대상 계열사/직책/선임시기/비 고, 신한 대상회사/직위명/선임일/비고, 하나 겸직회사명/직위/선임일/상근여부,
우리 대상회사/직위/겸임일자/상근\\/비상근 여부). 사람은 지주 임원 표의 같은 이름(1명일 때만), 회사는 출자 표와 같은 이름 연결.
"""

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from src.graph.load_relations import duckdb_counts, position_rows
from src.loaders import concurrent_positions as CP
from src.loaders.duckdb_store import connect, upsert_disclosures
from tests.test_change_report import add_exec
from tests.test_report_details import disclosure_row

KB, SHH = "00688996", "00382199"
KBAM, KBBANK = "00104500", "00386937"   # KB자산운용(임원 표 없음), 국민은행(임원 표 있음, config 오버라이드가 이 번호로 잇는다)
R2024, R2025, R2025_FIX = "20250314000001", "20260313000002", "20260619000003"
D2024, D2025 = date(2024, 12, 31), date(2025, 12, 31)


def table(header: list[str], rows: list[list[str]]) -> dict:
    return {"caption": None, "unit": None, "header_rows": 1, "rows": [header, *rows]}


KB_TABLE = table(["성 명", "대상 계열사", "직책", "선임시기", "비 고"],
                 [["이환주", "국민은행", "은행장", "2025. 1월", "상근"],
                  ["박효익", "KB자산운용", "기타비상무이사", "2025. 3월", "비상근"],
                  ["김동명", "KB자산운용", "기타비상무이사", "2025. 3월", "비상근"],      # 지주 임원 표에 동명이인 2명 → 안 이음
                  ["홍길동", "KB자산운용", "기타비상무이사", "2025. 3월", "비상근"],      # 지주 임원 표에 없음 → 안 이음
                  ["-", "-", "-", "-", "-"]])
KB_AFTER = table(["성명", "대상 계열사", "직책", "선임시기", "비 고"], [["새사람", "KB자산운용", "이사", "2026. 3월", "비상근"]])   # 작성기준일 이후 표 → 무시
SHH_TABLE = table(["성명", "대상회사", "직위명", "선임일", "비고"], [["정상혁", "신한은행", "은행장", "2023.02.15", "상근"]])
HANA_TABLE = table(["성명", "겸직회사명", "직위", "선임일", "상근여부"], [["서중근", "KEB Hana Global Finance Ltd", "이사", "2024.07.05", "비상근"]])
WOORI_TABLE = table(["성명", "대상회사", "직위", "겸임일자", "상근/비상근 여부"], [["옥일진", "우리은행", "부행장", "2022.02.25", "상근"]])
OTHER_TABLE = table(["회사명", "출자회사수"], [["경영참여", "11"]])


class ConcurrentPositionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(self.tmp.name) / "t.duckdb")})
        env.start()
        self.addCleanup(env.stop)
        self.con = connect()
        self.addCleanup(self.con.close)
        con = self.con
        con.executemany("INSERT INTO corp VALUES (?, ?, NULL, NULL, now())", [[KB, "KB금융"], [KBAM, "KB자산운용"], [KBBANK, "국민은행"]])
        con.executemany("INSERT INTO target_corp VALUES (?, ?, ?, ?, NULL, NULL, '2025', now())",
                        [[KB, "KB금융", "holding", None], [KBAM, "KB자산운용", "subsidiary", KB], [KBBANK, "국민은행", "subsidiary", KB]])
        upsert_disclosures(con, [disclosure_row(R2024, "사업보고서 (2024.12)", "20250314"),
                                 disclosure_row(R2025, "사업보고서 (2025.12)", "20260313"),
                                 disclosure_row(R2025_FIX, "[기재정정]사업보고서 (2025.12)", "20260619")])
        # 지주 임원 표: 이환주·박효익은 1명, 김동명은 생년월이 다른 2명(동명이인), 홍길동은 없음
        add_exec(con, R2025, "이환주", "부회장", "미등기", "이환주|1964-01|남", D2025, seq=1)
        add_exec(con, R2025, "박효익", "전무", "미등기", "박효익|1968-05|남", D2025, seq=2)
        add_exec(con, R2025, "김동명", "전무", "미등기", "김동명|1970-01|남", D2025, seq=3)
        add_exec(con, R2025, "김동명", "상무", "미등기", "김동명|1975-02|남", D2025, seq=4)
        add_exec(con, R2025_FIX, "이환주", "부회장", "미등기", "이환주|1964-01|남", D2025, seq=1)
        add_exec(con, R2025_FIX, "박효익", "전무", "미등기", "박효익|1968-05|남", D2025, seq=2)
        add_exec(con, R2024, "박효익", "전무", "미등기", "박효익|1968-05|남", D2024, valid_to=D2025, seq=2)
        # 국민은행 자기 임원 표에 이환주가 이미 있다 → 겸직 표의 국민은행 행은 그래프에 올리지 않는다
        con.execute("""INSERT INTO executive (source_rcept_no, row_seq, corp_code, bsns_year, reprt_code, nm, ofcps, rgist_exctv_at, person_id,
                       stlm_dt, valid_from, loaded_at) VALUES ('20260320000009', 1, ?, '2025', '11011', '이환주', '은행장', '사내이사', '이환주|1964-01|남', ?, ?, now())""",
                    [KBBANK, D2025, D2025])
        con.execute("INSERT INTO person VALUES ('이환주|1964-01|남', '이환주', '1964-01', '남', NULL, NULL, 2, now())")
        con.execute("INSERT INTO person VALUES ('박효익|1968-05|남', '박효익', '1968-05', '남', NULL, NULL, 1, now())")

    def add_section(self, rcept, tables, path="IX. 계열회사 등에 관한 사항", section_id=90, file_name=None):
        self.con.execute("INSERT INTO document_section VALUES (?, ?, NULL, '사업보고서', ?, NULL, 1, NULL, NULL, NULL, ?, ?, false, true, '', ?, 0, ?)",
                         [rcept, file_name or f"{rcept}.xml", section_id, path.split(" > ")[-1], path, json.dumps(tables, ensure_ascii=False), len(tables)])

    def test_header_map_matches_all_four_holdings_and_skips_other_tables(self):
        for t in (KB_TABLE, SHH_TABLE, HANA_TABLE, WOORI_TABLE):
            m = CP.header_map(t)
            self.assertEqual(sorted(m), ["appointed", "fte", "nm", "ofcps", "target"], t["rows"][0])
        self.assertIsNone(CP.header_map(OTHER_TABLE))
        self.assertEqual(CP.concurrent_table([OTHER_TABLE, KB_TABLE, KB_AFTER])[0], KB_TABLE)   # 첫 겸직 표만
        rows = CP.parse_rows(WOORI_TABLE, CP.header_map(WOORI_TABLE))
        self.assertEqual(rows[0]["target_name"], "우리은행")
        self.assertEqual((rows[0]["appointed_on"], rows[0]["appointed_method"], rows[0]["fte_at"]), (date(2022, 2, 25), "date", "상근"))
        self.assertEqual(CP.parse_appointed("2025. 1월"), (date(2025, 1, 1), "month"))
        self.assertEqual(CP.parse_appointed("-"), (None, None))
        self.assertEqual(CP.stlm_dt_of("사업보고서 (2025.12)"), date(2025, 12, 31))
        self.assertEqual(CP.stlm_dt_of("반기보고서 (2026.06)"), date(2026, 6, 30))

    def test_rebuild_links_people_resolves_corps_and_closes_snapshots(self):
        self.add_section(R2024, [OTHER_TABLE, table(["성명", "대상 계열사", "직책", "선임시기", "비 고"], [["박효익", "KB자산운용", "기타비상무이사", "2024. 3월", "비상근"]])])
        self.add_section(R2025, [OTHER_TABLE, KB_TABLE, KB_AFTER])
        self.add_section(R2025_FIX, [OTHER_TABLE, KB_TABLE, KB_AFTER])
        self.con.begin()
        stats = CP.rebuild_concurrent_positions(self.con)
        self.con.commit()
        self.assertEqual(stats["concurrent_reports"], 3)
        self.assertEqual(stats["concurrent_person_by_method"], {"holding_executive": 5, "ambiguous": 2, "not_found": 2})   # '-' 행은 빠짐
        rows = self.con.execute("""SELECT source_rcept_no, nm, person_id, person_method, target_corp_code, resolve_method, appointed_on, appointed_method,
                                          valid_from, valid_to, superseded_by FROM concurrent_position ORDER BY source_rcept_no, row_seq""").fetchall()
        by = {(r[0], r[1]): r for r in rows}
        self.assertEqual(by[(R2025_FIX, "박효익")][2:6], ("박효익|1968-05|남", "holding_executive", KBAM, "exact"))
        self.assertEqual(by[(R2025_FIX, "박효익")][6:8], (date(2025, 3, 1), "month"))
        self.assertEqual(by[(R2025_FIX, "김동명")][2:4], (None, "ambiguous"))
        self.assertEqual(by[(R2025_FIX, "홍길동")][2:4], (None, "not_found"))
        self.assertNotIn((R2025_FIX, "새사람"), by)                                      # 작성기준일 이후 표는 안 읽음
        # 정정본이 원본을 대체하고, 2024 스냅샷은 2025 기준일에 닫힘
        self.assertEqual(by[(R2025, "박효익")][10], R2025_FIX)
        self.assertEqual(by[(R2024, "박효익")][8:11], (D2024, D2025, None))
        self.assertIsNone(by[(R2025_FIX, "박효익")][9])
        # 그래프 행: 박효익→KB자산운용은 올라가고, 이환주→국민은행은 국민은행 임원 표에 이미 있어 안 올라간다. 수는 duckdb_counts와 같다
        positions, persons, _ = position_rows(self.con)
        cp = [p for p in positions if p["source_table"] == "concurrent_position"]
        self.assertEqual(sorted((p["corp"], p["valid_from"]) for p in cp), [(KBAM, "2024-12-31"), (KBAM, "2025-12-31"), (KBAM, "2025-12-31")])
        self.assertTrue(all(p["row_key"].startswith("cp:") and p["title"] == "기타비상무이사" and p["registered"] is None for p in cp))
        self.assertEqual(duckdb_counts(self.con)["positions"], len(positions))
        self.assertIn("박효익", [p["name"] for p in persons])

    def test_unresolved_target_becomes_name_node(self):
        self.add_section(R2025, [table(["성명", "겸직회사명", "직위", "선임일", "상근여부"], [["박효익", "KEB Hana Global Finance Ltd", "이사", "2024.07.05", "비상근"]])])
        self.con.begin(); CP.rebuild_concurrent_positions(self.con); self.con.commit()
        row = self.con.execute("SELECT target_corp_code, resolve_method FROM concurrent_position").fetchone()
        self.assertEqual(row, (None, "not_found"))
        positions, _, _ = position_rows(self.con)
        cp = [p for p in positions if p["source_table"] == "concurrent_position"][0]
        self.assertEqual(cp["corp"], f"name:{KB}:KEB Hana Global Finance Ltd")
        self.assertEqual(cp["corp_name"], "KEB Hana Global Finance Ltd")
