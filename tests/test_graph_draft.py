"""그래프 질문 정답 초안(eval/graph_draft.py) 테스트. DuckDB에 작은 관계 데이터를 넣고 12개 질문을 SQL로 푼다. 외부 호출 없음.

확인하는 것
1) 질문 12개가 모두 회사를 찾고 초안 답·근거 접수번호를 만든다 (생년월은 답에 없음)
2) 계층 질문의 수: 그래프와 같은 키 규칙(corp_code 없으면 출자한 회사별로 따로)
3) 초안 파일: g01~g12만 바꿔 쓰고 다른 후보는 그대로, 이미 ready인 질문은 건너뜀. graph_kind 칸 검사
4) 그래프 대조: 이름·수가 맞으면 일치, 다르면 이유
5) review의 a 키: 초안 답을 그대로 ready로
"""

import json
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from eval import graph_draft as G
from eval.questions import QuestionFormatError, load_questions, parse_question
from eval.review import review
from src.graph import query as Q
from src.loaders.duckdb_store import connect, upsert_disclosures
from tests.test_graph_query import TemplateDriver, node, owns
from tests.test_report_details import disclosure_row

KBH, SHH, HNH, WRH = "00000001", "00000002", "00000003", "00000004"
KBBANK, KBCARD, WRBANK, SHLIFE, SHSEC, WRSEC, SHBANK, KBLIFE = (
    "00000011", "00000012", "00000041", "00000021", "00000022", "00000042", "00000023", "00000099")
D25, D24, D23, D22, D21 = date(2025, 12, 31), date(2024, 12, 31), date(2023, 12, 31), date(2022, 12, 31), date(2021, 12, 31)
TODAY = date(2026, 11, 25)
R = {"wr": "20260301000001", "wrh": "20260301000002", "kbh": "20260301000003", "kbbank": "20260301000004", "shh": "20260301000005",
     "shlife": "20260301000006", "shsec": "20260301000007", "card23": "20230515000008", "kbh23": "20230515000009", "shsec21": "20220310000010",
     "hn": "20260301000011", "shbank": "20260301000012", "shbank24": "20250301000013", "shcard": "20240301000014"}
_seq = iter(range(1, 10_000))


def holder(con, rcept, corp, nm, code, pct, vf, vt=None):
    con.execute("""INSERT INTO major_shareholder (source_rcept_no, row_seq, corp_code, bsns_year, reprt_code, nm, relate, stock_knd,
                   holder_corp_code, resolve_method, trmend_posesn_stock_qota_rt, stlm_dt, valid_from, valid_to, loaded_at)
                   VALUES (?, ?, ?, '2025', '11011', ?, '본인', '보통주', ?, 'exact', ?, ?, ?, ?, now())""",
                [rcept, next(_seq), corp, nm, code, pct, vf, vf, vt])


def inv(con, rcept, investor, name, code, pct, vf=D25, vt=None):
    con.execute("""INSERT INTO investment (rcept_no, row_seq, investor_corp_code, bsns_year, reprt_code, inv_prm_raw, investee_name,
                   investee_corp_code, resolve_method, trmend_blce_qota_rt, stlm_dt, valid_from, valid_to, loaded_at)
                   VALUES (?, ?, ?, '2025', '11011', ?, ?, ?, 'exact', ?, ?, ?, ?, now())""",
                [rcept, next(_seq), investor, name, name, code, pct, vf, vf, vt])


def ex(con, rcept, corp, nm, ofcps, kind, pid, vf, vt=None):
    con.execute("""INSERT INTO executive (source_rcept_no, row_seq, corp_code, bsns_year, reprt_code, nm, ofcps, rgist_exctv_at, birth_ym,
                   person_id, stlm_dt, valid_from, valid_to, loaded_at)
                   VALUES (?, ?, ?, '2025', '11011', ?, ?, ?, '1961-06', ?, ?, ?, ?, now())""",
                [rcept, next(_seq), corp, nm, ofcps, kind, pid, vf, vf, vt])


class GraphDraftTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(self.tmp.name) / "t.duckdb")})
        env.start()
        self.addCleanup(env.stop)
        con = self.con = connect()
        self.addCleanup(con.close)
        names = {KBH: "KB금융지주", SHH: "신한지주", HNH: "하나금융지주", WRH: "우리금융지주", KBBANK: "KB국민은행", KBCARD: "KB국민카드",
                 "00000043": "우리종합금융", WRBANK: "우리은행", SHLIFE: "신한라이프생명보험", SHSEC: "신한투자증권", WRSEC: "우리투자증권", SHBANK: "신한은행", KBLIFE: "KB생명보험"}
        for code, name in names.items():
            con.execute("INSERT INTO corp VALUES (?, ?, NULL, NULL, now())", [code, name])
        for code in (KBH, SHH, HNH, WRH):
            con.execute("INSERT INTO target_corp VALUES (?, ?, 'holding', NULL, NULL, NULL, '2025', now())", [code, names[code]])
        for code, parent in [(KBBANK, KBH), (KBCARD, KBH), (WRBANK, WRH), (WRSEC, WRH), (SHLIFE, SHH), (SHSEC, SHH), (SHBANK, SHH)]:
            con.execute("INSERT INTO target_corp VALUES (?, ?, 'subsidiary', ?, 100.0, NULL, '2025', now())", [code, names[code], parent])
        upsert_disclosures(con, [
            disclosure_row(R["shsec21"], "사업보고서 (2021.12)", "20220310", SHSEC) | {"corp_name": "신한금융투자"},
            disclosure_row(R["shsec"], "사업보고서 (2025.12)", "20260301", SHSEC) | {"corp_name": "신한투자증권"},
            disclosure_row("20240301000020", "사업보고서 (2023.12)", "20240301", WRSEC) | {"corp_name": "한국포스증권"},
            disclosure_row("20260301000021", "사업보고서 (2025.12)", "20260301", WRSEC) | {"corp_name": "우리투자증권"},
        ])
        # 추론
        holder(con, R["wr"], WRBANK, "우리금융지주", WRH, 100.0, D25)
        holder(con, R["wrh"], WRH, "우리사주조합", None, 9.5, D25)
        holder(con, R["wrh"], WRH, "우리은행우리사주조합", None, 2.01, D25)   # 특수관계인 → 최대주주 표 합계 11.51%
        holder(con, R["shlife"], SHLIFE, "신한지주", SHH, 100.0, D25)
        holder(con, R["card23"], KBCARD, "KB금융지주", KBH, 100.0, D22, D23)
        ex(con, R["kbh23"], KBH, "윤종규", "회장", "사내이사", "윤종규|1955-10|남", D22, D23)   # 대표이사는 담당업무 칸에만
        con.execute("UPDATE executive SET chrg_job = '대표이사' WHERE nm = '윤종규'")
        holder(con, R["shsec21"], SHSEC, "신한지주", SHH, 100.0, D21, D22)
        # 계층
        inv(con, R["kbh"], KBH, "KB국민은행", KBBANK, 100.0)
        inv(con, R["kbh"], KBH, "KB국민카드", KBCARD, 100.0)
        inv(con, R["kbbank"], KBBANK, "KB Cambodia", None, 70.0)
        inv(con, R["shh"], SHH, "신한라이프생명보험", SHLIFE, 100.0)
        inv(con, R["shh"], SHH, "신한투자증권", SHSEC, 100.0)
        inv(con, R["shh"], SHH, "신한펀드A", None, 10.0)
        inv(con, R["shlife"], SHLIFE, "신한펀드A", None, 40.0)   # 지주도 직접 가짐 → 간접만 아님? 키가 달라 별개 (0011 ②)
        inv(con, R["shsec"], SHSEC, "신한펀드B", None, 20.0)
        # 통합
        ex(con, R["kbh"], KBH, "양종희", "대표이사 회장", "사내이사", "양종희|1961-06|남", D25)
        ex(con, R["kbh"], KBH, "이영희", "사외이사", "사외이사", "이영희|1960-03|여", D25)
        ex(con, R["kbbank"], KBBANK, "양종희", "기타비상무이사", "기타비상무이사", "양종희|1961-06|남", D25)
        ex(con, R["hn"], HNH, "박겸직", "사외이사", "사외이사", "박겸직|1958-02|남", D25)
        ex(con, R["shh"], SHH, "박겸직", "사외이사", "사외이사", "박겸직|1958-02|남", D25)
        ex(con, R["shcard"], "00000024", "정상혁", "부사장", "미등기", "정상혁|1964-01|남", D22, D23)
        ex(con, R["shbank24"], SHBANK, "정상혁", "대표이사 은행장", "사내이사", "정상혁|1964-01|남", D23, D24)
        ex(con, R["shbank"], SHBANK, "정상혁", "대표이사 은행장", "사내이사", "정상혁|1964-01|남", D24)
        con.execute("INSERT INTO corp VALUES ('00000024', '신한카드', NULL, NULL, now())")
        self.drafts = {s.id: G.solve(con, s, [], today=TODAY) for s in G.SPECS}

    def test_all_twelve_have_answers(self):
        for s in G.SPECS:
            with self.subTest(id=s.id):
                d = self.drafts[s.id]
                self.assertNotIn("회사를 찾지 못함", d.answer)
                self.assertNotIn("1961", d.answer + d.note)
                q = G.to_question(s, d)
                parse_question(q.to_dict())   # 형식 검사 통과 (draft라 정답 확인 전)
                self.assertEqual(q.graph_kind, s.graph_kind)
        d = self.drafts
        self.assertIn("우리금융지주 (100.0%)", d["g01"].answer)
        self.assertIn("우리사주조합 (9.5%, 특수관계인 포함 최대주주 표 합계 11.51%", d["g01"].answer)
        self.assertIn("우리금융지주 (100.0%)", d["g01"].answer)                 # 특수관계인이 없으면 합계를 따로 적지 않음
        self.assertEqual(d["g01"].evidence, [R["wr"], R["wrh"]])
        self.assertEqual(d["g02"].count, 2)
        self.assertIn("윤종규(직위 회장, 담당업무 대표이사)", d["g03"].answer)
        self.assertEqual(d["g03"].evidence[0], R["kbh23"])                  # 답이 있는 보고서(임원 표)가 먼저
        self.assertIn("2021-12-31 최대주주: 신한지주", d["g08"].answer)
        self.assertIn("신한금융투자 → 신한투자증권", d["g08"].answer)
        self.assertIn("우리종합금융", d["g07"].answer)                       # corp 테이블에만 있는 없어진 회사도 찾는다
        self.assertIn("corp_lineage.csv에 적어야", d["g07"].note)
        self.assertIn("corp_lineage.csv에 적어야", d["g09"].note)  # 공시 목록에도 CSV에도 없음
        self.assertIn("박겸직", d["g10"].answer)
        self.assertEqual(d["g10"].count, 1)
        self.assertIn("양종희", d["g11"].answer)
        self.assertNotIn("이영희", d["g11"].answer)
        self.assertIn("신한카드 부사장 2022-12-31 ~ 2023-12-31", d["g12"].answer)
        self.assertIn("신한은행 대표이사 은행장 2023-12-31 ~ 현재", d["g12"].answer)

    def test_hierarchy_counts_use_graph_keys(self):
        d = self.drafts
        self.assertEqual(d["g04"].answer, "1단계 2곳, 2단계 1곳, 합계 3곳 (corp_code 없는 대상 포함)")
        self.assertEqual(d["g05"].count, 2)                       # 신한펀드A(라이프 경유)와 신한펀드B — 지주의 신한펀드A와는 다른 키
        self.assertIn("신한펀드B (신한투자증권 경유)", d["g05"].answer)
        self.assertEqual(d["g06"].count, 0)                       # 우리금융은 출자 데이터 없음
        self.assertEqual(G.walk_down(self.con, SHH, D25, 1).keys(), {SHLIFE, SHSEC, f"name:{SHH}:신한펀드A"})

    def test_lineage_csv_is_used(self):
        lineage = [{"current_corp_code": "00000088", "former_corp_code": KBLIFE, "former_name": "KB생명보험", "on": "2023-01-01",
                    "reason": "합병", "evidence_rcept_no": "20230102000001"}]
        self.con.execute("INSERT INTO corp VALUES ('00000088', 'KB라이프생명보험', NULL, NULL, now())")
        d = G.solve(self.con, G.SPECS[8], lineage, today=TODAY)
        self.assertIn("KB생명보험(00000099) → KB라이프생명보험 2023-01-01 합병", d.answer)
        self.assertEqual(d.evidence, ["20230102000001"])
        wr = [{"current_corp_code": WRSEC, "former_corp_code": "00000043", "former_name": "우리종합금융", "on": "2024-08-01",
               "reason": "흡수합병", "evidence_rcept_no": "20260313001235"}]
        d7 = G.solve(self.con, G.SPECS[6], wr, today=TODAY)
        self.assertIn("우리종합금융(00000043) → 우리투자증권 2024-08-01 흡수합병", d7.answer)
        self.assertIn("우리투자증권", d7.names)                                  # 이어지는 회사 이름도 대조에 쓴다

    def test_write_drafts_keeps_other_lines_and_skips_ready(self):
        draft = Path(self.tmp.name) / "draft.jsonl"
        qs = Path(self.tmp.name) / "questions.jsonl"
        draft.write_text('{"id": "d900", "question": "다른 후보", "type": "정형표", "phase": 3, "status": "draft", "answer": null}\n'
                         '{"id": "g01", "question": "옛 초안", "type": "다중경로", "phase": 4, "status": "draft", "answer": null}\n', encoding="utf-8")
        qs.write_text(json.dumps({"id": "g02", "question": "확인됨", "type": "다중경로", "phase": 4, "status": "ready", "answer": "2곳",
                                  "evidence_rcept_no": [R["shh"]], "graph_kind": "추론"}, ensure_ascii=False) + "\n", encoding="utf-8")
        questions = [G.to_question(s, self.drafts[s.id]) for s in G.SPECS]
        self.assertEqual(G.write_drafts(questions, draft, qs), (11, 1))
        ids = [q.id for q in load_questions(draft)]
        self.assertEqual(ids[0], "d900")
        self.assertEqual(sorted(ids[1:]), [s.id for s in G.SPECS if s.id != "g02"])
        self.assertEqual(G.write_drafts(questions, draft, qs), (11, 1))   # 다시 돌려도 중복 없음
        self.assertEqual(len(load_questions(draft)), 12)
        with self.assertRaisesRegex(QuestionFormatError, "graph_kind"):
            parse_question({"id": "x", "question": "q", "type": "다중경로", "phase": 4, "status": "draft", "graph_kind": "추측"})

    def test_compare_with_graph(self):
        wr, wrh, union = node(WRBANK, "우리은행"), node(WRH, "우리금융지주"), node(f"name:{WRH}:우리사주조합", "우리사주조합", has=False)
        driver = TemplateDriver({"holder_chain": [
            {"nodes": [wr, wrh], "rels": [owns(WRH, 100.0, R["wr"], "major_shareholder", "m1")]},
            {"nodes": [wr, wrh, union], "rels": [owns(WRH, 100.0, R["wr"], "major_shareholder", "m1"), owns(union["key"], 9.5, R["wrh"], "major_shareholder", "m2")]},
        ]})
        spec = G.SPECS[0]
        ctx = Q.build_context(spec.question, driver, self.con, today=TODAY)
        self.assertTrue(G.compare(spec, self.drafts["g01"], ctx)["ok"])
        ctx.relations = ctx.relations[:1]       # 그래프가 두 번째 단계를 못 찾은 경우 (경로도 첫 번째만)
        ctx.steps[0].paths = ctx.steps[0].paths[:1]
        r = G.compare(spec, self.drafts["g01"], ctx)
        self.assertFalse(r["ok"])
        self.assertEqual(r["missing_names"], ["우리사주조합"])
        self.assertEqual(G.compare(spec, self.drafts["g01"], None)["why"], "라우터가 그래프 템플릿을 고르지 않음")

        kbh, bank, card, camb = node(KBH, "KB금융지주"), node(KBBANK, "KB국민은행"), node(KBCARD, "KB국민카드"), node(f"name:{KBBANK}:KB Cambodia", "KB Cambodia", has=False)
        d2 = TemplateDriver({"descendants": [
            {"nodes": [kbh, bank], "rels": [owns(KBH, 100.0, R["kbh"], "investment", "i1")]},
            {"nodes": [kbh, card], "rels": [owns(KBH, 100.0, R["kbh"], "investment", "i2")]},
        ]})
        spec4 = G.SPECS[3]
        r = G.compare(spec4, self.drafts["g04"], Q.build_context(spec4.question, d2, self.con, today=TODAY))
        self.assertEqual((r["ok"], r["why"]), (False, "수가 다름: SQL 3 vs 그래프 2"))

    def test_review_a_accepts_draft_answer(self):
        draft = Path(self.tmp.name) / "draft.jsonl"
        qs = Path(self.tmp.name) / "questions.jsonl"
        qs.write_text("", encoding="utf-8")
        q = G.to_question(G.SPECS[0], self.drafts["g01"])
        G.write_drafts([q], draft, qs)
        out = []
        stats = review(self.con, qs, draft, load_questions(draft), input_fn=lambda _: "a", print_fn=out.append)
        self.assertEqual(stats["accepted"], 1)
        saved = load_questions(qs)[0]
        self.assertEqual((saved.id, saved.status, saved.answer, saved.graph_kind), ("g01", "ready", self.drafts["g01"].answer, "추론"))
        self.assertTrue(any("초안 답 (DuckDB에서 만든 것, 미확인)" in line for line in out))
        self.assertEqual(load_questions(draft), [])


if __name__ == "__main__":
    unittest.main()
