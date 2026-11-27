"""라우팅 정확도(eval/routing.py) 테스트. 작은 DuckDB에 회사 사전만 넣고 라우터를 돌린다. LLM 호출 없음.

확인하는 것
1) 그래프 질문 12개의 기대 템플릿이 0011 결정 4 표와 같다
2) 규칙만으로 그래프 질문은 맞고, 일반 질문은 그래프로 가지 않는다 → 요약 수
3) picker는 규칙이 못 고른 질문에서만 불리고, 일반 질문을 그래프로 보내면 틀린 것으로 센다
4) markdown 표는 틀린 줄과 그래프 질문만 적는다
"""

import unittest
from datetime import date

from eval import routing as R
from eval.graph_draft import SPECS
from eval.questions import Question
from src.graph.router import Plan, Step
from src.loaders.duckdb_store import connect, upsert_disclosures
from tests.test_graph_query import HNH, KBBANK, KBCARD, KBH, SHH, SHLIFE, SHSEC, WRBANK, WRH, WRSEC
from tests.helpers import TempDuckDBTestCase
from tests.test_report_details import disclosure_row

TODAY = date(2026, 11, 26)


def q(id_, text, type_="서술", graph_kind=None):
    return Question(id_, text, type_, 4 if graph_kind else 3, "ready", "답", graph_kind=graph_kind)


class RoutingTest(TempDuckDBTestCase):
    def setUp(self):
        super().setUp()
        con = self.con = connect()
        self.addCleanup(con.close)
        corps = [(KBH, "KB금융지주"), (SHH, "신한지주"), (HNH, "하나금융지주"), (WRH, "우리금융지주"), (KBBANK, "KB국민은행"), (KBCARD, "KB국민카드"),
                 (WRBANK, "우리은행"), (SHLIFE, "신한라이프생명보험"), (SHSEC, "신한투자증권"), (WRSEC, "우리투자증권"), ("00000023", "신한은행"),
                 ("00000099", "KB생명보험"), ("00000043", "우리종합금융")]
        for code, name in corps:
            con.execute("INSERT INTO corp VALUES (?, ?, NULL, NULL, now())", [code, name])
        for code, name in corps[:4]:
            con.execute("INSERT INTO target_corp VALUES (?, ?, 'holding', NULL, NULL, NULL, '2025', now())", [code, name])
        for code, name, parent in [(KBBANK, "KB국민은행", KBH), (KBCARD, "KB국민카드", KBH), (WRBANK, "우리은행", WRH), (SHLIFE, "신한라이프생명보험", SHH),
                                   (SHSEC, "신한투자증권", SHH), (WRSEC, "우리투자증권", WRH), ("00000023", "신한은행", SHH)]:
            con.execute("INSERT INTO target_corp VALUES (?, ?, 'subsidiary', ?, 100.0, NULL, '2025', now())", [code, name, parent])
        upsert_disclosures(con, [disclosure_row("20220301000001", "사업보고서 (2021.12)", "20220301", SHSEC) | {"corp_name": "신한금융투자"}])
        self.graph_qs = [q(s.id, s.question, graph_kind=s.graph_kind) for s in SPECS]
        self.general_qs = [q("d001", "2025년 KB금융지주의 배당 성향은 얼마인가?"), q("d002", "신한은행의 2024년 BIS 비율은?"),
                           q("n001", "우리은행 사업보고서에 직원 평균 연봉이 나오나?", type_="없음"),
                           q("d025", "2025년 말 기준 우리은행의 최대주주는 누구이고 지분율은 몇 %인가?", type_="정형표"),
                           q("n002", "우리은행 사업보고서에 최대주주의 취미가 나오나?", type_="없음"),
                           q("d055", "2025년 말 기준 KB금융지주가 출자한 회사 중 기말 장부가액이 가장 큰 곳은 어디인가?", type_="정형표")]

    def test_expected_templates_follow_0011_table(self):
        self.assertEqual(R.expected_templates(self.graph_qs[0]), ["holder_chain"])                      # g01
        self.assertEqual(R.expected_templates(self.graph_qs[1]), ["holder_chain", "descendants"])       # g02
        self.assertEqual(R.expected_templates(self.graph_qs[2]), ["holder_chain", "facts_at"])          # g03
        self.assertEqual([R.expected_templates(x) for x in self.graph_qs[3:6]], [["descendants"]] * 3)  # g04~g06
        self.assertEqual(R.expected_templates(self.graph_qs[7]), ["corp_lineage", "facts_at"])         # g08
        self.assertEqual([R.expected_templates(x) for x in self.graph_qs[9:11]], [["shared_positions"]] * 2)
        self.assertEqual(R.expected_templates(self.graph_qs[11]), ["person_history"])
        self.assertEqual(R.expected_templates(self.general_qs[0]), [])

    def test_rules_only_counts(self):
        rows = R.route_all(self.graph_qs + self.general_qs, self.con, lineage=[], today=TODAY)
        s = R.summarize(rows)
        self.assertEqual((s["graph_total"], s["general_total"]), (12, 6))
        self.assertEqual(s["graph_ok"], 12, [(r.id, r.got) for r in rows if r.expected and not r.ok])
        # d055("출자한 회사 중")는 규칙이 descendants 깊이 1로 보낸다 → 허용
        self.assertEqual((s["general_ok"], s["general_allowed"], s["general_wrong"], s["picker_calls"]), (5, 1, 0, 0))
        d055 = next(r for r in rows if r.id == "d055")
        self.assertEqual((d055.got, d055.rule, d055.verdict), (["descendants"], "출자한 회사", "허용"))
        self.assertTrue(all(r.rule for r in rows if r.expected))

    def test_picker_called_only_on_rule_misses_and_wrong_pick_counts_as_error(self):
        def picker(question, as_of, mentions):
            corp = mentions[0].corp_code if mentions else KBH
            return Plan("LLM 고르기: holder_chain", [Step("holder_chain", {"corp": corp, "as_of": as_of.isoformat(), "depth": 2})], as_of, mentions)
        rows = R.route_all(self.graph_qs + self.general_qs, self.con, lineage=[], today=TODAY, picker=picker)
        s = R.summarize(rows)
        self.assertEqual(s["picker_calls"], 5)                 # 규칙이 못 고른 일반 질문 5개에서만 (d055는 규칙이 골라 picker 안 감)
        self.assertEqual((s["graph_ok"], s["general_ok"], s["general_allowed"], s["general_wrong"]), (12, 0, 1, 5))  # holder_chain은 허용이 아니다
        self.assertTrue(all(r.used_picker for r in rows if not r.expected and r.id != "d055"))   # d055는 규칙이 골라 picker 안 감

    def test_facts_at_is_allowed_only_for_point_in_time_holder_or_executive_questions(self):
        def picker(question, as_of, mentions):
            corp = mentions[0].corp_code if mentions else KBH
            return Plan("LLM 고르기: facts_at", [Step("facts_at", {"corp": corp, "as_of": as_of.isoformat(), "kind": None, "title": None})], as_of, mentions)
        rows = {r.id: r for r in R.route_all(self.general_qs[:5], self.con, lineage=[], today=TODAY, picker=picker)}
        self.assertEqual(rows["d025"].verdict, "허용")   # 한 시점 최대주주 → T7 허용
        self.assertEqual(rows["d001"].verdict, "틀림")   # 배당 성향은 관계 표에 없다
        self.assertEqual(rows["n002"].verdict, "틀림")   # '없음' 유형은 단어가 있어도 허용하지 않는다
        s = R.summarize(list(rows.values()))
        self.assertEqual((s["general_ok"], s["general_allowed"], s["general_wrong"]), (0, 1, 4))
        md = R.markdown(list(rows.values()), "규칙+LLM 고르기")
        self.assertIn("허용(최대주주·임원 → facts_at, 출자한 회사 → descendants) 1, 틀림 4", md)
        self.assertIn("| d025 | 정형표 | - | facts_at | LLM 고르기: facts_at (LLM) | 허용 |", md)

    def test_markdown_lists_graph_rows_and_errors_only(self):
        rows = R.route_all(self.graph_qs + self.general_qs, self.con, lineage=[], today=TODAY)
        md = R.markdown(rows, "규칙만")
        self.assertIn("그래프 질문: 12/12", md)
        self.assertIn("일반 질문: 그래프 안 씀 5/6", md)
        self.assertIn("| g04 | 계층 | descendants | descendants |", md)
        self.assertNotIn("d001", md)   # 맞은 일반 질문은 표에서 뺀다


if __name__ == "__main__":
    unittest.main()
