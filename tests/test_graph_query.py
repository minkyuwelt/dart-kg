"""그래프 질의(라우터·템플릿·관계 표·파생 값·RRF) 테스트. Neo4j 없이: 라우터는 DuckDB 사전만, 실행은 템플릿 이름별로 정해 둔 경로를 돌려주는 가짜 드라이버.

확인하는 것
1) 0011의 질문 12개가 각각 의도한 템플릿(과 빈칸)으로 간다. 템플릿에 안 맞는 질문은 None(RAG만)
2) 날짜 규칙, 회사 이름 찾기(긴 이름 먼저, 두 회사를 가리키는 이름은 쓰지 않음, 옛 이름·없어진 회사)
3) 템플릿 질의문의 빈칸 = 선언한 빈칸, 시점 필터 조각 사용, 빈칸 검사
4) 경로 → 관계 표(방향·근거 접수번호), 파생 값(간접만·지분율 곱·재직 기간 합치기), 앞 단계 결과로 다음 단계 채우기
5) 그래프 근거 조각과 RAG 조각을 RRF로 합치기, 라우터가 못 고르면 RAG 그대로
"""

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from eval.metrics import Hit
from src.graph import query as Q
from src.graph import templates as T
from src.graph.router import find_corps, parse_as_of, route
from src.loaders.duckdb_store import connect, upsert_disclosures
from tests.test_report_details import disclosure_row

KBH, SHH, HNH, WRH = "00000001", "00000002", "00000003", "00000004"
KBBANK, KBCARD, WRBANK, SHLIFE, SHSEC, WRSEC, KBLIFE_OLD = "00000011", "00000012", "00000041", "00000021", "00000022", "00000042", "00000099"
SHBANK = "00000023"
TODAY = date(2026, 11, 25)


class FakeResult:
    def __init__(self, records):
        self.records = records


def node(key, name, label="Corp", has=True, names=None):
    return {"key": key, "name": name, "label": label, "has_corp_code": has, "names": names or [name]}


def owns(start, pct, rcept, table, row, vf="2025-12-31", vt=None):
    return {"type": "OWNS", "start_key": start, "pct": pct, "source_table": table, "row_key": row,
            "valid_from": vf, "valid_to": vt, "source_rcept_no": rcept, "superseded_by": None}


def pos(start, title, rcept, row, vf, vt=None, registered="사내이사"):
    return {"type": "HOLDS_POSITION", "start_key": start, "title": title, "registered": registered, "row_key": row,
            "valid_from": vf, "valid_to": vt, "source_rcept_no": rcept, "superseded_by": None}


class TemplateDriver:
    """질의문으로 템플릿을 알아보고, 그 템플릿에 정해 둔 경로를 돌려준다. 받은 빈칸은 calls에 남긴다."""

    def __init__(self, answers: dict):
        self.answers, self.calls = answers, []

    def execute_query(self, query, database_=None, **params):
        name = next(t.name for t in T.TEMPLATES.values() if t.cypher == query)
        self.calls.append((name, params))
        ans = self.answers.get(name, [])
        return FakeResult(ans(params) if callable(ans) else ans)


class GraphQueryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(self.tmp.name) / "t.duckdb")})
        env.start()
        self.addCleanup(env.stop)
        self.con = connect()
        self.addCleanup(self.con.close)
        con = self.con
        corps = [(KBH, "KB금융지주"), (SHH, "신한지주"), (HNH, "하나금융지주"), (WRH, "우리금융지주"), (KBBANK, "KB국민은행"),
                 (KBCARD, "KB국민카드"), (WRBANK, "우리은행"), (SHLIFE, "신한라이프생명보험"), (SHSEC, "신한투자증권"),
                 (WRSEC, "우리투자증권"), (SHBANK, "신한은행"), (KBLIFE_OLD, "KB생명보험"), ("00000043", "우리종합금융"), ("00000098", "중복은행"), ("00000097", "중복은행")]
        for code, name in corps:
            con.execute("INSERT INTO corp VALUES (?, ?, NULL, NULL, now())", [code, name])
        for code, name in corps[:4]:
            con.execute("INSERT INTO target_corp VALUES (?, ?, 'holding', NULL, NULL, NULL, '2025', now())", [code, name])
        for code, name, parent in [(KBBANK, "KB국민은행", KBH), (KBCARD, "KB국민카드", KBH), (WRBANK, "우리은행", WRH),
                                   (SHLIFE, "신한라이프생명보험", SHH), (SHSEC, "신한투자증권", SHH), (WRSEC, "우리투자증권", WRH), (SHBANK, "신한은행", SHH)]:
            con.execute("INSERT INTO target_corp VALUES (?, ?, 'subsidiary', ?, 100.0, NULL, '2025', now())", [code, name, parent])
        upsert_disclosures(con, [
            disclosure_row("20220301000001", "사업보고서 (2021.12)", "20220301", SHSEC) | {"corp_name": "신한금융투자"},
            disclosure_row("20240301000002", "사업보고서 (2023.12)", "20240301", WRSEC) | {"corp_name": "한국포스증권"},
            disclosure_row("20260301000003", "사업보고서 (2025.12)", "20260301", KBH) | {"corp_name": "KB금융"},
        ])

    def plan(self, q):
        return route(q, self.con, today=TODAY)

    # ------------------------------------------------------------ 1) 질문 12개

    def test_twelve_questions_route_to_templates(self):
        cases = {
            "2025년 12월 31일 기준 우리은행의 최대주주의 최대주주는 누구인가?": [("holder_chain", {"corp": WRBANK, "depth": 2})],
            "2025년 12월 31일 기준 신한라이프생명보험의 최대주주가 지분 100%를 가진 회사는 몇 곳인가?":
                [("holder_chain", {"corp": SHLIFE, "depth": 1}), ("descendants", {"depth": 1, "min_pct": 100.0})],
            "2023년 3월 31일 기준 KB국민카드 최대주주의 대표이사는 누구인가?":
                [("holder_chain", {"corp": KBCARD, "as_of": "2023-03-31"}), ("facts_at", {"kind": "positions", "title": "대표이사"})],
            "2025년 12월 31일 기준 KB금융지주 아래 손자회사까지(2단계) 포함하면 회사는 모두 몇 곳인가?": [("descendants", {"corp": KBH, "depth": 2})],
            "2025년 12월 31일 기준 신한지주가 자회사를 거쳐서만 지분 50% 이상을 가진 회사는 어디이고, 각각 어느 자회사를 거치나?":
                [("descendants", {"corp": SHH, "depth": 2, "min_pct": 50.0})],
            "2025년 12월 31일 기준 KB금융지주 아래 손자회사까지 몇 곳?": [("descendants", {"corp": KBH, "min_pct": None})],   # 기준 없으면 None
            "2025년 말 기준 KB금융지주가 출자한 회사 중 기말 장부가액이 가장 큰 곳은 어디인가?": [("descendants", {"corp": KBH, "depth": 1, "as_of": "2025-12-31"})],  # 출자한 회사 → 1단계
            "2025년 12월 31일 기준 우리금융지주 아래 2단계까지의 출자 대상 중 국내 공시 대상이 아닌 곳(해외 법인·펀드 등)은 몇 곳인가?":
                [("descendants", {"corp": WRH, "depth": 2})],
            "우리종합금융은 지금 어느 회사로 이어지고, 그 공시는 어느 회사 이름으로 이어 보나?": [("corp_lineage", {"corp": "00000043", "name": "우리종합금융"})],
            "신한금융투자의 2021년 12월 31일 기준 최대주주는 누구였나?":
                [("corp_lineage", {"corp": SHSEC}), ("facts_at", {"corp": SHSEC, "kind": "holders", "as_of": "2021-12-31"})],
            "KB생명보험의 공시는 지금 어느 회사로 이어지나?": [("corp_lineage", {"corp": KBLIFE_OLD})],
            "2025년 12월 31일 기준 4대 금융지주 중 두 곳 이상에서 동시에 사외이사인 사람이 있는가?":
                [("shared_positions", {"corps": [KBH, SHH, HNH, WRH], "outside_only": True, "must": None})],
            "2025년 12월 31일 기준 KB금융지주 임원 중 KB 계열 자회사 임원을 함께 맡은 사람은 누구인가?":
                [("shared_positions", {"corps": [KBH, KBBANK, KBCARD], "must": KBH, "outside_only": False})],
            "신한은행의 현재 대표이사는 그동안 어느 계열사에서 어떤 직위를 거쳤나?": [("person_history", {"corp": SHBANK, "title": "대표이사"})],
        }
        for question, want in cases.items():
            with self.subTest(question=question):
                p = self.plan(question)
                self.assertIsNotNone(p, question)
                self.assertEqual([s.template for s in p.steps], [t for t, _ in want])
                for step, (_, params) in zip(p.steps, want):
                    for k, v in params.items():
                        self.assertEqual(step.params.get(k), v, f"{step.template}.{k}")
                for step in p.steps:
                    T.TEMPLATES[step.template].bind({**step.params, **({"corp": "x"} if step.from_prev else {})})

    def test_non_graph_questions_go_to_rag(self):
        self.assertIsNone(self.plan("2025년 신한은행의 주당 현금배당금은 얼마인가?"))
        self.assertIsNone(self.plan("금융자산 공정가치는 어떻게 측정하나?"))
        self.assertIsNone(self.plan("두 곳 이상에서 동시에 임원인 사람은?"))  # 범위(회사)가 없으면 추측하지 않음

    def test_picker_is_asked_only_when_rules_fail(self):
        asked = []
        picker = lambda q, as_of, mentions: asked.append(q)
        self.assertIsNone(route("신한은행의 배당 성향은?", self.con, today=TODAY, picker=picker))
        route("2025년 12월 31일 기준 우리은행의 최대주주의 최대주주는?", self.con, today=TODAY, picker=picker)
        self.assertEqual(asked, ["신한은행의 배당 성향은?"])

    # ------------------------------------------------------------ 2) 날짜·회사

    def test_parse_as_of(self):
        self.assertEqual(parse_as_of("2023년 3월 31일 기준", TODAY)[0], date(2023, 3, 31))
        self.assertEqual(parse_as_of("2024년 말 기준", TODAY)[0], date(2024, 12, 31))
        self.assertEqual(parse_as_of("2024년 6월 기준", TODAY)[0], date(2024, 6, 30))
        self.assertEqual(parse_as_of("2025.09.30 현재", TODAY)[0], date(2025, 9, 30))
        self.assertEqual(parse_as_of("지금 대표이사는?", TODAY), (TODAY, "날짜 없음 → 오늘"))
        self.assertEqual(parse_as_of("2024년 배당은?", TODAY)[0], TODAY)  # 연도만은 그해 말로 보지 않음
        self.assertEqual(parse_as_of("2024년 사업보고서 기준 국민은행의 대표이사는?", TODAY)[0], date(2024, 12, 31))  # 사업연도 말
        self.assertEqual(parse_as_of("2023년 3월 31일 기준 2024년 사업보고서의?", TODAY)[0], date(2023, 3, 31))  # 날짜가 있으면 그것이 먼저

    def test_find_corps(self):
        m = find_corps(self.con, "KB국민은행과 신한지주")
        self.assertEqual([x.corp_code for x in m], [KBBANK, SHH])
        self.assertEqual(find_corps(self.con, "신한금융투자의 최대주주")[0].current, "신한투자증권")
        self.assertEqual(find_corps(self.con, "중복은행의 최대주주"), [])   # corp 테이블에 같은 이름이 둘 → 추측하지 않음
        self.assertEqual(find_corps(self.con, "KB생명보험")[0].corp_code, KBLIFE_OLD)  # 수집 대상이 아닌 없어진 회사
        lineage = [{"former_corp_code": "00000077", "former_name": "옛날은행"}]
        self.assertEqual(find_corps(self.con, "옛날은행은?", lineage)[0].corp_code, "00000077")

    def test_find_corps_uses_names_linked_in_investment_tables(self):
        # DART 등록명이 '케이비국민카드'여도 지주 출자 표에 corp_code로 연결된 'KB국민카드'로 찾는다 (src/graph/names.py)
        self.con.execute("UPDATE corp SET corp_name = '케이비국민카드' WHERE corp_code = ?", [KBCARD])
        self.con.execute("UPDATE target_corp SET corp_name = '케이비국민카드' WHERE corp_code = ?", [KBCARD])
        self.con.execute("DELETE FROM disclosure WHERE corp_code = ?", [KBCARD])
        self.con.execute(
            """INSERT INTO investment (rcept_no, row_seq, investor_corp_code, bsns_year, reprt_code, inv_prm_raw, investee_name,
                                       investee_corp_code, resolve_method, loaded_at)
               VALUES ('20220301000001', 1, ?, '2021', '11011', 'KB국민카드', 'KB국민카드', ?, 'hangul', now()),
                      ('20220301000001', 2, ?, '2021', '11011', '-', '-', NULL, 'not_found', now())""", [KBH, KBCARD, KBH])
        self.assertEqual([m.corp_code for m in find_corps(self.con, "KB국민카드 최대주주의 대표이사")], [KBCARD])

    def test_title_aliases(self):
        self.assertTrue(T.title_matches("은행장", "대표이사"))      # 은행은 대표이사를 은행장으로 적는다
        self.assertFalse(T.title_matches("부행장", "대표이사"))
        self.assertTrue(T.title_matches("대표이사 사장", "대표이사"))
        self.assertTrue(T.title_matches("아무 직위", None))
        self.assertTrue(T.is_ceo("회장", "대표이사"))                 # 금융지주: 직위 '회장', 담당업무 '대표이사'
        self.assertTrue(T.is_ceo("회장", "대표이사 (이사회 의장)"))
        self.assertFalse(T.is_ceo("이사", "사외이사 (자회사대표이사후보 추천위원회)"))   # 위원회 이름 안의 말은 아님
        self.assertTrue(T.title_matches("회장", "대표이사", "대표이사"))
        self.assertIn("r0.ceo", T.TEMPLATES["person_history"].cypher)
        self.assertIn(f"LIMIT {T.DESCENDANTS_LIMIT}", T.TEMPLATES["descendants"].cypher)

    def test_abbreviation_is_not_rename(self):
        p = self.plan("2025년 12월 31일 기준 KB금융 아래 손자회사까지 포함하면 몇 곳인가?")
        self.assertEqual([s.template for s in p.steps], ["descendants"])

    # ------------------------------------------------------------ 3) 템플릿 질의문

    def test_template_params_match_cypher(self):
        for t in T.TEMPLATES.values():
            with self.subTest(template=t.name):
                self.assertEqual(T.cypher_params(t), set(t.params) | set(t.optional))
                if t.name not in ("corp_lineage", "evidence_chunks"):
                    self.assertIn("superseded_by IS NULL", t.cypher)
                self.assertNotIn("birth", t.cypher)
        with self.assertRaisesRegex(ValueError, "빈칸이 비었"):
            T.TEMPLATES["holder_chain"].bind({"corp": "x", "as_of": "2025-12-31"})
        with self.assertRaisesRegex(ValueError, "모르는 빈칸"):
            T.TEMPLATES["holder_chain"].bind({"corp": "x", "as_of": "2025-12-31", "depth": 2, "cypher": "MATCH"})

    # ------------------------------------------------------------ 4) 실행·관계 표·파생

    def test_evidence_keeps_relation_order_and_caps_chunks_per_report(self):
        # 2026-11-25 평가: 접수번호 순으로 먼저 잘라, 조각 많은 오래된 보고서가 칸을 다 채우던 문제 (g11·g12)
        rels = [{"source_rcept_no": r, "source_table": "HOLDS_POSITION"} for r in ("R2026", "R2021", "R2026", "R2023")]
        driver = TemplateDriver({"evidence_chunks": lambda p: [
            {"rcept_no": r, "file_name": "f", "section_id": 1, "path": "임원", "text": f"{r}-{n}", "seq": n}
            for r in p["rcepts"] for n in range(p["per_doc"])]})
        hits = Q.evidence(driver, rels)
        params = driver.calls[0][1]
        self.assertEqual(params["rcepts"], ["R2026", "R2021", "R2023"])   # 관계에 나온 순서, 중복 없이 (접수번호 순이 아님)
        self.assertEqual(params["per_doc"], Q.EVIDENCE_PER_DOC)
        # 보고서별로 번갈아: 세 보고서의 첫 조각이 1~3위, 그다음 둘째 조각 (2026-11-26, g05·g06에서 지주 보고서가 1~3위를 다 차지하던 문제)
        self.assertEqual([h.rcept_no for h in hits[:6]], ["R2026", "R2021", "R2023"] * 2)
        self.assertEqual([h.text for h in hits[:3]], ["R2026-0", "R2021-0", "R2023-0"])
        self.assertIn("collect(ch)[..$per_doc]", T.TEMPLATES["evidence_chunks"].cypher)

    def test_interleave_keeps_first_seen_report_order_and_handles_uneven_counts(self):
        recs = [{"rcept_no": r, "n": i} for i, r in enumerate(["A", "A", "A", "B", "C", "C"])]
        got = [(r["rcept_no"], r["n"]) for r in Q.interleave_by_report(recs)]
        self.assertEqual(got, [("A", 0), ("B", 3), ("C", 4), ("A", 1), ("C", 5), ("A", 2)])
        self.assertEqual(Q.interleave_by_report([]), [])

    def test_holder_chain_then_positions(self):
        card, kbh, nps = node(KBCARD, "KB국민카드"), node(KBH, "KB금융지주"), node(f"name:{KBH}:국민연금공단", "국민연금공단", has=False)
        driver = TemplateDriver({
            "holder_chain": [{"nodes": [card, kbh], "rels": [owns(KBH, 100.0, "R_CARD", "major_shareholder", "msh:1")]},
                             {"nodes": [card, nps], "rels": [owns(nps["key"], 0.0, "R_CARD", "major_shareholder", "msh:2")]}],
            "facts_at": [{"nodes": [node("p1", "양종희", "Person", None), kbh], "rels": [pos("p1", "대표이사 회장", "R_KBH", "exe:1", "2022-12-31")]}],
            "evidence_chunks": lambda p: [{"rcept_no": r, "file_name": "f", "section_id": 1, "path": "임원", "text": f"{r} 조각", "seq": 0} for r in p["rcepts"]],
        })
        ctx = Q.build_context("2023년 3월 31일 기준 KB국민카드 최대주주의 대표이사는 누구인가?", driver, self.con, today=TODAY)
        self.assertEqual([c[0] for c in driver.calls], ["holder_chain", "facts_at", "evidence_chunks"])
        self.assertEqual(driver.calls[1][1]["corp"], KBH)   # 지분율이 큰 회사 주주로 이어 감 (이름 노드는 건너뜀)
        r0 = ctx.relations[0]
        self.assertEqual((r0["src"], r0["rel"], r0["dst"]), ("KB금융지주", "OWNS", "KB국민카드"))  # 주주 → 회사 방향
        self.assertEqual(ctx.relations[2]["src"], "양종희")
        self.assertEqual(driver.calls[2][1]["rcepts"], ["R_CARD", "R_KBH"])
        self.assertEqual([h.rcept_no for h in ctx.evidence], ["R_CARD", "R_KBH"])
        text = Q.to_text(ctx)
        self.assertIn("KB금융지주 | OWNS | KB국민카드 | 100.0% | ", text)
        self.assertIn("(유효)", text)
        trace = ctx.trace()
        self.assertEqual(trace["steps"][1]["params"]["corp"], KBH)
        self.assertEqual(trace["rule"], "최대주주 → 그 회사의 임원")

    def test_chain_stops_when_no_corp_holder(self):
        nps = node("name:x:국민연금공단", "국민연금공단", has=False)
        driver = TemplateDriver({"holder_chain": [{"nodes": [node(KBCARD, "KB국민카드"), nps], "rels": [owns(nps["key"], 9.0, "R", "major_shareholder", "m")]}]})
        ctx = Q.build_context("2023년 3월 31일 기준 KB국민카드 최대주주의 대표이사는 누구인가?", driver, self.con, today=TODAY)
        self.assertEqual(ctx.steps[1].note, "앞 단계에서 이어 갈 회사를 찾지 못함")
        self.assertNotIn("facts_at", [c[0] for c in driver.calls])

    def test_descendants_derive_indirect_only_and_product(self):
        shh, life, sec, fund = node(SHH, "신한지주"), node(SHLIFE, "신한라이프생명보험"), node(SHSEC, "신한투자증권"), node("name:x:신한펀드", "신한펀드", has=False)
        driver = TemplateDriver({"descendants": [
            {"nodes": [shh, life], "rels": [owns(SHH, 100.0, "R1", "investment", "i1")]},
            {"nodes": [shh, sec], "rels": [owns(SHH, 100.0, "R1", "investment", "i2")]},
            {"nodes": [shh, life, fund], "rels": [owns(SHH, 100.0, "R1", "investment", "i1"), owns(SHLIFE, 40.0, "R2", "investment", "i3")]},
            {"nodes": [shh, sec, fund], "rels": [owns(SHH, 100.0, "R1", "investment", "i2"), owns(SHSEC, 20.0, "R3", "investment", "i4")]},
        ]})
        ctx = Q.build_context("2025년 12월 31일 기준 신한지주가 자회사를 거쳐서만 지분을 가진 회사는 어디인가?", driver, self.con, today=TODAY)
        self.assertEqual(len(ctx.relations), 4)  # i1·i2는 두 경로에 나오지만 한 번만
        derived = {d["subject"]: d["value"] for d in ctx.derived}
        self.assertTrue(derived["신한라이프생명보험"].startswith("직접"))
        self.assertIn("간접만(2단계", derived["신한펀드"])
        self.assertIn("40.00%", derived["신한펀드"])       # 두 경로 중 큰 값 (100%×40%)
        self.assertIn("국내 공시 대상 아님", derived["신한펀드"])
        self.assertIn("1단계 2곳, 2단계 1곳 (합계 3곳, corp_code 없음 1곳)", derived[SHH])
        self.assertNotIn("장부가액 최대", str(ctx.derived))   # 관계에 장부가액이 없으면 그 줄은 없다

    def test_descendants_derive_max_book_value_among_direct(self):
        kbh, bank, card = node(KBH, "KB금융지주"), node(KBBANK, "KB국민은행"), node(KBCARD, "KB국민카드")
        driver = TemplateDriver({"descendants": [
            {"nodes": [kbh, bank], "rels": [owns(KBH, 100.0, "R1", "investment", "i1") | {"book_value": 14_821_721_000_000}]},
            {"nodes": [kbh, card], "rels": [owns(KBH, 100.0, "R1", "investment", "i2") | {"book_value": 1_000_000_000}]},
        ]})
        ctx = Q.build_context("2025년 말 기준 KB금융지주가 출자한 회사 중 기말 장부가액이 가장 큰 곳은 어디인가?", driver, self.con, today=TODAY)
        self.assertEqual(ctx.plan.rule, "출자한 회사")
        self.assertEqual(ctx.steps[0].params["depth"], 1)
        rows = {d["rule"]: d["value"] for d in ctx.derived}
        self.assertEqual(rows["직접 출자 중 장부가액 최대"], "KB국민은행 14,821,721,000,000원 (14,821,721백만원, 직접 출자 2곳 중)")
        text = Q.to_text(ctx)
        self.assertLess(text.index("장부가액 최대"), text.index("출발 | 관계"))   # 파생 값이 관계 행보다 앞
        text = Q.to_text(ctx)
        self.assertIn("파생 값", text)
        self.assertLess(text.index("[파생 값"), text.index("출발 | 관계 | 도착"))   # 파생 값이 관계 행보다 앞 (글자 상한에 잘리지 않게)
        self.assertIn("[관계 ", text)

    def test_person_history_merges_consecutive_reports(self):
        p, bank, card = node("p9", "정상혁", "Person", None), node(SHBANK, "신한은행"), node("00000024", "신한카드")
        driver = TemplateDriver({"person_history": [
            {"nodes": [p, card], "rels": [pos("p9", "부사장", "R0", "e0", "2022-12-31", "2023-12-31", "미등기")]},
            {"nodes": [p, bank], "rels": [pos("p9", "대표이사", "R1", "e1", "2023-12-31", "2024-12-31")]},
            {"nodes": [p, bank], "rels": [pos("p9", "대표이사", "R2", "e2", "2024-12-31", None)]},
        ]})
        ctx = Q.build_context("신한은행의 현재 대표이사는 그동안 어느 계열사에서 어떤 직위를 거쳤나?", driver, self.con, today=TODAY)
        values = [d["value"] for d in ctx.derived]
        self.assertEqual(values, ["신한카드 부사장 2022-12-31 ~ 2023-12-31", "신한은행 대표이사 2023-12-31 ~ 현재"])

    # ------------------------------------------------------------ 5) RRF

    def test_graph_retriever_fuses_with_base(self):
        wr, wrh = node(WRBANK, "우리은행"), node(WRH, "우리금융지주")
        driver = TemplateDriver({
            "holder_chain": [{"nodes": [wr, wrh], "rels": [owns(WRH, 100.0, "RG", "major_shareholder", "m1")]}],
            "evidence_chunks": [{"rcept_no": "RG", "file_name": "f", "section_id": 1, "path": "최대주주", "text": "그래프 근거", "seq": 0},
                                {"rcept_no": "RB", "file_name": "f", "section_id": 2, "path": "주주", "text": "둘 다", "seq": 1}],
        })

        class Base:
            name = "neo4j-hybrid-fake"

            def search(self, question, k):
                return [Hit("RX", "f", 9, "다른", 1.0, "rag만", "rag만"), Hit("RB", "f", 2, "주주", 0.9, "둘 다", "둘 다")]

        r = Q.GraphRetriever(driver, self.con, base=Base(), today=TODAY)
        hits = r.search("2025년 12월 31일 기준 우리은행의 최대주주의 최대주주는?", 3)
        self.assertEqual(hits[0].text, "둘 다")           # 두 목록 모두에 있는 조각이 맨 위
        self.assertEqual({h.text for h in hits}, {"둘 다", "그래프 근거", "rag만"})
        self.assertEqual(r.last.plan.rule, "최대주주 체인")
        # 템플릿에 안 맞는 질문은 RAG 결과 그대로
        self.assertEqual([h.rcept_no for h in r.search("주당 현금배당금은?", 2)], ["RX", "RB"])
        self.assertIsNone(r.last)
        only = Q.GraphRetriever(driver, self.con, today=TODAY)
        self.assertEqual(only.search("주당 현금배당금은?", 2), [])


if __name__ == "__main__":
    unittest.main()
