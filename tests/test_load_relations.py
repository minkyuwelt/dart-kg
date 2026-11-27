"""관계 → Neo4j 적재(src/graph/load_relations.py) 테스트. 진짜 Neo4j 없이, 질의를 흉내 내는 가짜 드라이버로 확인한다.

확인하는 것
1) 출자·최대주주·임원 행이 하나도 빠짐없이 관계가 되고(끝점 노드가 모두 있음), stats가 "일치"
2) corp_code 없는 상대는 "name:{보고한 회사}:{이름}" 노드 — 다른 회사 표의 같은 이름과 합쳐지지 않는다 (0011 ②·⑤)
3) Person 키·속성에 생년월이 없다 (0006·0012)
4) 공시에 나온 옛 이름이 Corp.names에 처음 나온 순서로 들어간다 (0011 ①)
5) 두 번 돌려도 수가 같다 (지우고 다시 넣기), 정정으로 대체된 행도 superseded_by와 함께 들어간다
6) corp_lineage.csv: 근거 없는 줄·같은 corp_code는 거부, 올바른 줄은 FORMERLY
"""

import unittest
from datetime import date
from pathlib import Path

from src.graph import load_relations as lr
from src.loaders.duckdb_store import connect, upsert_disclosures
from tests.test_report_details import disclosure_row
from tests.helpers import FakeResult, TempDuckDBTestCase

HOLD, BANK, CARD = "00688996", "00105961", "00241653"
R1, R2 = "20250314000001", "20260313000002"
D1, D2 = date(2024, 12, 31), date(2025, 12, 31)
LINEAGE_HEADER = ",".join(lr.LINEAGE_COLUMNS) + "\n"


class GraphDriver:
    """우리 질의문 몇 가지만 알아듣는 가짜 Neo4j. 노드는 키로, 관계는 (종류, row_key)로 저장한다.
    MATCH가 끝점을 못 찾으면 진짜 Neo4j처럼 그 행은 조용히 버려진다 — 그래서 '빠진 행'을 테스트가 잡을 수 있다."""

    def __init__(self):
        self.corps, self.persons, self.rels = {}, {}, {}
        self.documents = {}

    def execute_query(self, query, database_=None, rows=None, **params):
        q = query.strip()
        if q.startswith("CREATE"):
            return FakeResult([])
        if q == lr.CLEAR:
            self.corps.clear(); self.persons.clear(); self.rels.clear()
        elif "MERGE (c:Corp" in q:
            for r in rows:
                self.corps[r["key"]] = dict(r)
        elif "MERGE (p:Person" in q:
            for r in rows:
                self.persons[r["pid"]] = dict(r)
        elif "[r:OWNS" in q:
            for r in rows:
                if r["src"] in self.corps and r["dst"] in self.corps:
                    self.rels[("OWNS", r["row_key"])] = dict(r)
        elif "[r:HOLDS_POSITION" in q:
            for r in rows:
                if r["pid"] in self.persons and r["corp"] in self.corps:
                    self.rels[("HOLDS_POSITION", r["row_key"])] = dict(r)
        elif "[r:FORMERLY]" in q:
            for r in rows:
                if r["current"] in self.corps and r["former"] in self.corps:
                    self.rels[("FORMERLY", r["current"], r["former"])] = dict(r)
        elif q == lr.FILED_MERGE.strip():
            for rcept, corp in self.documents.items():
                if corp in self.corps:
                    self.rels[("FILED", rcept)] = {}
        elif "count(c) AS corps" in q:
            n = lambda kind: sum(1 for k in self.rels if k[0] == kind)
            return FakeResult([{"corps": len(self.corps), "persons": len(self.persons), "owns": n("OWNS"),
                                "positions": n("HOLDS_POSITION"), "formerly": n("FORMERLY"), "filed": n("FILED")}])
        else:
            raise AssertionError(f"모르는 질의: {q[:60]}")
        return FakeResult([])


def add_inv(con, rcept, seq, investor, name, code, pct, stlm, valid_to=None, superseded_by=None):
    con.execute(
        """INSERT INTO investment (rcept_no, row_seq, investor_corp_code, bsns_year, reprt_code, inv_prm_raw, investee_name,
                                   investee_corp_code, resolve_method, trmend_blce_qota_rt, stlm_dt, valid_from, valid_to, superseded_by, loaded_at)
           VALUES (?, ?, ?, ?, '11011', ?, ?, ?, ?, ?, ?, ?, ?, ?, now())""",
        [rcept, seq, investor, str(stlm.year), name, name, code, "exact" if code else "not_found", pct, stlm, stlm, valid_to, superseded_by],
    )


def add_holder(con, rcept, seq, corp, nm, holder_code, pct, stlm, valid_to=None):
    con.execute(
        """INSERT INTO major_shareholder (source_rcept_no, row_seq, corp_code, bsns_year, reprt_code, nm, relate, stock_knd,
                                          holder_corp_code, resolve_method, trmend_posesn_stock_qota_rt, stlm_dt, valid_from, valid_to, loaded_at)
           VALUES (?, ?, ?, ?, '11011', ?, '본인', '보통주', ?, ?, ?, ?, ?, ?, now())""",
        [rcept, seq, corp, str(stlm.year), nm, holder_code, "exact" if holder_code else "not_found", pct, stlm, stlm, valid_to],
    )


def add_exec(con, rcept, seq, corp, nm, kind, pid, stlm, valid_to=None):
    con.execute(
        """INSERT INTO executive (source_rcept_no, row_seq, corp_code, bsns_year, reprt_code, nm, ofcps, rgist_exctv_at, birth_ym,
                                  person_id, stlm_dt, valid_from, valid_to, loaded_at)
           VALUES (?, ?, ?, ?, '11011', ?, ?, ?, '1961-06', ?, ?, ?, ?, now())""",
        [rcept, seq, corp, str(stlm.year), nm, kind, kind, pid, stlm, stlm, valid_to],
    )


class LoadRelationsTest(TempDuckDBTestCase):
    def setUp(self):
        super().setUp()
        self.con = connect()
        self.addCleanup(self.con.close)
        con = self.con
        self.lineage = Path(self.tmp.name) / "corp_lineage.csv"
        self.lineage.write_text(LINEAGE_HEADER, encoding="utf-8")

        con.execute("INSERT INTO corp VALUES (?, 'KB금융지주', '105560', NULL, now()), (?, 'KB국민은행', NULL, NULL, now()), (?, 'KB국민카드', NULL, NULL, now())",
                    [HOLD, BANK, CARD])
        con.execute("INSERT INTO target_corp VALUES (?, 'KB금융지주', 'holding', NULL, NULL, NULL, '2025', now()), "
                    "(?, 'KB국민은행', 'subsidiary', ?, 100.0, ?, '2025', now())", [HOLD, BANK, HOLD, R2])
        # 공시 목록: 은행이 옛 이름 '국민은행'으로 먼저, 지금 이름으로 나중에
        upsert_disclosures(con, [
            disclosure_row("20240101000001", "분기보고서 (2023.09)", "20240101", BANK) | {"corp_name": "국민은행"},
            disclosure_row("20250314000009", "사업보고서 (2024.12)", "20250314", BANK) | {"corp_name": "KB국민은행"},
            disclosure_row(R2, "사업보고서 (2025.12)", "20260313", HOLD),
        ])
        # 지주 출자: 은행(연결됨) 2년, 해외 법인(연결 안 됨) 1년. 2024 행 하나는 정정으로 대체됨
        add_inv(con, R1, 1, HOLD, "KB국민은행", BANK, 100.0, D1, valid_to=D2)
        add_inv(con, R1, 2, HOLD, "KB Cambodia", None, 70.0, D1, valid_to=D2, superseded_by="20250401000003")
        add_inv(con, R2, 1, HOLD, "KB국민은행", BANK, 100.0, D2)
        # 은행 출자: 같은 이름 'KB Cambodia'지만 다른 회사의 표 → 다른 노드여야 함
        add_inv(con, R2, 2, BANK, "KB Cambodia", None, 30.0, D2)
        # 최대주주: 은행의 최대주주 = 지주(연결됨), 지주의 최대주주 = 국민연금(연결 안 됨)
        add_holder(con, R2, 1, BANK, "(주)KB금융지주", HOLD, 100.0, D2)
        add_holder(con, R2, 2, HOLD, "국민연금공단", None, 8.3, D2)
        # 임원: 한 사람이 지주·은행 겸직, person_id 없는 행 하나
        add_exec(con, R2, 1, HOLD, "양종희", "사내이사", "양종희|1961-06|남", D2)
        add_exec(con, R2, 2, BANK, "양종희", "기타비상무이사", "양종희|1961-06|남", D2)
        add_exec(con, R2, 3, BANK, "이영희", "사외이사", "이영희|1960-03|여", D2)
        add_exec(con, R2, 4, CARD, "무명", "미등기", None, D2)
        add_exec(con, R2, 5, CARD, "이영희", "사외이사", "이영희|1960-03|여", D2)
        con.execute("INSERT INTO person (person_id, nm, birth_ym, sexdstn, loaded_at) VALUES "
                    "('양종희|1961-06|남', '양종희', '1961-06', '남', now()), ('이영희|1960-03|여', '이영희', '1960-03', '여', now())")

    def build(self, driver=None):
        driver = driver or GraphDriver()
        driver.documents = {R2: HOLD, "20250314000009": BANK}
        out = lr.build(self.con, driver, lineage_path=self.lineage, log=lambda *a, **k: None)
        return driver, out

    def test_every_row_becomes_a_relation(self):
        driver, out = self.build()
        self.assertEqual(out["owns"], 6)
        self.assertEqual(out["positions"], 4)
        self.assertEqual(out["positions_skipped_no_person_id"], 1)
        s = lr.stats(self.con, driver, lineage_path=self.lineage)
        self.assertTrue(s["match"], s)
        self.assertEqual(s["neo4j"]["filed"], 2)
        corrected = driver.rels[("OWNS", f"inv:{R1}:2")]
        self.assertEqual(corrected["superseded_by"], "20250401000003")
        self.assertEqual(corrected["valid_to"], "2025-12-31")

    def test_unresolved_names_are_scoped_to_the_reporting_corp(self):
        driver, out = self.build()
        name_keys = sorted(k for k in driver.corps if k.startswith("name:"))
        self.assertEqual(name_keys, [f"name:{BANK}:KB Cambodia", f"name:{HOLD}:KB Cambodia", f"name:{HOLD}:국민연금공단"])
        self.assertEqual(out["name_only_corps"], 3)
        self.assertFalse(driver.corps[f"name:{HOLD}:국민연금공단"]["has_corp_code"])
        # 연결된 최대주주는 이름이 '(주)KB금융지주'여도 corp_code 노드로
        self.assertEqual(driver.rels[("OWNS", f"msh:{R2}:1")]["src"], HOLD)

    def test_person_has_no_birth_month(self):
        driver, _ = self.build()
        self.assertEqual(sorted(p["name"] for p in driver.persons.values()), ["양종희", "이영희"])
        for pid, p in driver.persons.items():
            self.assertNotIn("1961", pid + str(p))
            self.assertNotIn("1960", pid + str(p))
        for key, rel in driver.rels.items():
            if key[0] == "HOLDS_POSITION":
                self.assertNotIn("1961", str(rel))
        yang = lr.person_key("양종희|1961-06|남")
        self.assertEqual(sum(1 for k, r in driver.rels.items() if k[0] == "HOLDS_POSITION" and r["pid"] == yang), 2)
        outside = [r for k, r in driver.rels.items() if k[0] == "HOLDS_POSITION" and r["outside"]]
        self.assertEqual(len(outside), 2)

    def test_names_keep_former_names_in_order(self):
        driver, _ = self.build()
        bank = driver.corps[BANK]
        self.assertEqual(bank["name"], "KB국민은행")
        self.assertEqual(bank["names"], ["국민은행", "KB국민은행"])
        self.assertEqual(driver.corps[HOLD]["role"], "holding")
        self.assertIn(CARD, driver.corps)  # 임원 행에만 나오는 회사도 노드 (person_id 없는 행은 건너뛰지만 같은 회사의 다른 행으로)

    def test_blank_name_rows_and_linked_names(self):
        add_inv(self.con, R2, 9, HOLD, "-", None, None, D2)                 # 출자 표의 빈 줄: 회사가 아니다
        add_inv(self.con, R2, 10, HOLD, "국민카드(지주 표기)", CARD, 100.0, D2)   # 지주가 부르는 다른 이름 → Corp.names
        driver, out = self.build()
        self.assertEqual(out["owns"], 7)                                    # 6 + 국민카드 행. '-' 행은 빠짐
        self.assertTrue(lr.stats(self.con, driver, lineage_path=self.lineage)["match"])   # DuckDB 쪽 수도 같은 규칙으로 뺀다
        self.assertFalse(any(k.endswith(":-") for k in driver.corps))
        self.assertIn("국민카드(지주 표기)", driver.corps[CARD]["names"])

    def test_ceo_from_duty_column(self):
        # 금융지주는 직위 '회장', 담당업무 '대표이사'로 적는다 → HOLDS_POSITION.ceo
        self.con.execute("UPDATE executive SET ofcps = '회장', chrg_job = '대표이사' WHERE nm = '양종희' AND corp_code = ?", [HOLD])
        self.con.execute("UPDATE executive SET chrg_job = '사외이사 (자회사대표이사후보 추천위원회)' WHERE nm = '이영희'")
        driver, _ = self.build()
        rels = {r["title"]: r for k, r in driver.rels.items() if k[0] == "HOLDS_POSITION"}
        self.assertTrue(rels["회장"]["ceo"])
        self.assertEqual(rels["회장"]["duty"], "대표이사")
        self.assertFalse(rels["사외이사"]["ceo"])

    def test_titles_are_one_line(self):
        self.assertEqual(lr._one_line("기타\n비상무이사"), "기타 비상무이사")
        self.assertIsNone(lr._one_line(None))

    def test_rebuild_is_idempotent(self):
        driver, _ = self.build()
        first = lr.stats(self.con, driver, lineage_path=self.lineage)["neo4j"]
        self.con.execute("DELETE FROM investment WHERE row_seq = 2 AND rcept_no = ?", [R2])  # DuckDB rebuild로 행이 사라진 경우
        self.build(driver)
        second = lr.stats(self.con, driver, lineage_path=self.lineage)
        self.assertEqual(second["neo4j"]["owns"], first["owns"] - 1)
        self.assertTrue(second["match"])
        self.assertNotIn(f"name:{BANK}:KB Cambodia", driver.corps)

    def test_lineage_csv(self):
        self.lineage.write_text(LINEAGE_HEADER + f"{BANK},00999999,옛은행,2023-01-02,합병,\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "근거"):
            lr.load_lineage(self.lineage)
        self.lineage.write_text(LINEAGE_HEADER + f"{BANK},{BANK},같음,2023-01-02,사명 변경,20230102000001\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "이름 이력"):
            lr.load_lineage(self.lineage)
        self.lineage.write_text("a,b\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "머리줄"):
            lr.load_lineage(self.lineage)
        self.lineage.write_text(LINEAGE_HEADER + f"{BANK},00999999,옛은행,2023-01-02,합병,20230102000001\n", encoding="utf-8")
        driver, out = self.build()
        self.assertEqual(out["formerly"], 1)
        self.assertEqual(driver.corps["00999999"]["name"], "옛은행")  # corp 테이블에 없는 옛 회사는 CSV의 이름으로
        self.assertTrue(lr.stats(self.con, driver, lineage_path=self.lineage)["match"])

    def test_bad_lineage_stops_before_clearing(self):
        driver, _ = self.build()
        before = len(driver.rels)
        self.lineage.write_text(LINEAGE_HEADER + "x,y,z,2023-01-02,r,1\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.build(driver)
        self.assertEqual(len(driver.rels), before)

    def test_repo_lineage_file_is_valid(self):
        lr.load_lineage()  # 저장소의 config/corp_lineage.csv 형식 확인 (지금은 머리줄만)


if __name__ == "__main__":
    unittest.main()
