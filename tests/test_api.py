"""FastAPI(src/api/main.py) 테스트. 가짜 LLM과 DuckDB 키워드 검색기(chunk:section_path)로 엔드포인트 6개를 확인한다. 외부 호출 없음.

확인하는 것
1) /health, /corps, /corps/{code}/executives·shareholders (시점 필터, 생년월 없음, 모르는 회사 404)
1b) /corps/{code}: 회사 페이지 묶음 — 개요·최신 정기보고서·임원·최대주주·출자·겸직 표 자리·지분 추이, 모든 행에 url, 생년월 없음
2) /ask: use_llm=false면 검색만, true면 가짜 LLM 답과 인용 표시, 입력 검증(빈 질문 422)
3) /changes: 변화 리포트 JSON과 요약, since > until 422. /changes/text: 문장 변화(전년 같은 종류 보고서와 절 비교, section 낱말)
"""

import os
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from src.api.main import create_app
from src.loaders.chunks import build_chunks
from src.loaders.duckdb_store import connect, upsert_disclosures
from tests.test_change_report import D2024, D2025, KB, R2024, R2025, add_exec, add_holder, add_inv
from tests.helpers import TempDuckDBTestCase
from tests.test_rag_ask import FakeLLM
from tests.test_report_details import disclosure_row


class ApiTest(TempDuckDBTestCase):
    def setUp(self):
        super().setUp()
        with connect() as con:
            con.execute("INSERT INTO target_corp VALUES (?, 'KB금융', 'holding', NULL, NULL, NULL, '2025', now())", [KB])
            upsert_disclosures(con, [disclosure_row(R2024, "사업보고서 (2024.12)", "20250314"), disclosure_row(R2025, "사업보고서 (2025.12)", "20260313")])
            add_exec(con, R2024, "양종희", "회장", "사내이사", "양종희|1961-06|남", D2024, valid_to=D2025, seq=1)
            add_exec(con, R2024, "이영희", "사외이사", "사외이사", "이영희|1960-03|여", D2024, valid_to=D2025, seq=2)
            add_exec(con, R2025, "양종희", "회장", "사내이사", "양종희|1961-06|남", D2025, seq=1)
            add_exec(con, R2025, "박민수", "사외이사", "사외이사", "박민수|1970-07|남", D2025, seq=2)
            add_holder(con, R2024, "국민연금공단", 8.3, D2024, valid_to=D2025)
            add_holder(con, R2025, "국민연금공단", 7.9, D2025)
            text = "KB금융 주당 현금배당금은 보통주 3,000원입니다."
            con.execute(
                "INSERT INTO document_section VALUES (?, ? || '.xml', '11011', '사업보고서', 1, NULL, 1, NULL, NULL, NULL, ?, ?, false, true, ?, '[]', ?, 0)",
                [R2025, R2025, "배당에 관한 사항", "III. 재무 > 6. 배당에 관한 사항", text, len(text)],
            )
            build_chunks(con, [R2025], ["section_path"])
            # 문장 변화(/changes/text)용: 두 해 사업보고서에 같은 '위험관리' 절. 금액은 바뀌고, 한 문장은 글이 바뀜
            for rcept, body in ((R2024, "신용위험은 내부등급법으로 측정합니다. 2024년 말 위험가중자산은 1,000억원입니다."),
                                (R2025, "신용위험은 내부등급법과 스트레스 테스트로 측정합니다. 2025년 말 위험가중자산은 1,200억원입니다.")):
                con.execute(
                    "INSERT INTO document_section VALUES (?, ? || '.xml', '11011', '사업보고서', 2, NULL, 1, NULL, NULL, NULL, ?, ?, false, true, ?, '[]', ?, 0)",
                    [rcept, rcept, "위험관리", "III. 재무 > 7. 위험관리", body, len(body)],
                )
        self.llm = FakeLLM("KB금융의 주당 현금배당금은 보통주 3,000원입니다.", [1])
        self.keys_seen: list[str | None] = []   # 요청 헤더의 OpenAI 키가 LLM 공장에 닿는지
        def llm_factory(api_key=None):
            self.keys_seen.append(api_key)
            return self.llm
        app = create_app(llm_factory=llm_factory, retriever_name="chunk:section_path")
        self.client = self.enterContext(TestClient(app))

    def test_cors_for_web(self):
        # 화면(web/, Vite 개발 서버)에서 부를 수 있어야 한다 (0013). 허용 안 된 출처에는 헤더를 주지 않는다
        r = self.client.get("/health", headers={"Origin": "http://localhost:5173"})
        self.assertEqual(r.headers.get("access-control-allow-origin"), "http://localhost:5173")
        r = self.client.get("/health", headers={"Origin": "https://evil.example"})
        self.assertIsNone(r.headers.get("access-control-allow-origin"))
        from src.api.main import cors_origins
        with mock.patch.dict(os.environ, {"API_CORS_ORIGINS": "https://dart-kg.vercel.app/, http://localhost:5173"}):
            self.assertEqual(cors_origins(), ["https://dart-kg.vercel.app", "http://localhost:5173"])

    def test_health_and_corps(self):
        h = self.client.get("/health").json()
        self.assertEqual((h["duckdb"], h["documents_with_chunks"], h["retriever"]), ("ok", 1, "chunk:section_path"))
        corps = self.client.get("/corps").json()
        self.assertEqual([c["corp_code"] for c in corps], [KB])

    def test_executives_as_of_hides_birth_and_filters_time(self):
        r = self.client.get(f"/corps/{KB}/executives", params={"as_of": "2025-06-30"}).json()
        self.assertEqual(sorted(e["nm"] for e in r["executives"]), ["양종희", "이영희"])  # 2024 스냅샷이 유효한 날짜
        self.assertNotIn("birth_ym", r["executives"][0])
        self.assertNotIn("person_id", r["executives"][0])
        self.assertIn("rcpNo=", r["executives"][0]["url"])
        r = self.client.get(f"/corps/{KB}/executives", params={"as_of": "2026-03-31", "outside_only": "true"}).json()
        self.assertEqual([e["nm"] for e in r["executives"]], ["박민수"])
        self.assertEqual(self.client.get("/corps/00000000/executives").status_code, 404)

    def test_corp_page(self):
        # 자회사 하나(국민은행)를 더 두고: KB가 출자(2024 100% → 2025 100%, 장부가액 다름), KB 겸직 표에 국민은행 자리 1행(임원) + 1행(비임원)
        BANK = "00386937"
        with connect() as con:
            con.execute("INSERT INTO target_corp VALUES (?, '국민은행', 'subsidiary', ?, 100.0, ?, '2025', now())", [BANK, KB, R2025])
            add_inv(con, R2024, "국민은행", 100.0, D2024, valid_to=D2025, seq=1)
            add_inv(con, R2025, "국민은행", 100.0, D2025, seq=1)
            con.execute("UPDATE investment SET investee_corp_code = ?, trmend_blce_acntbk_amount = CASE WHEN rcept_no = ? THEN 100 ELSE 120 END", [BANK, R2024])
            for seq, (nm, ofcps, officer) in enumerate([("양종희", "기타비상무이사", True), ("홍길동", "ESG본부장", False)]):
                con.execute(
                    """INSERT INTO concurrent_position (source_rcept_no, row_seq, holding_corp_code, bsns_year, reprt_code, nm, person_id, person_method,
                       target_name, target_corp_code, resolve_method, ofcps, appointed_raw, appointed_on, appointed_method, fte_at, is_officer,
                       stlm_dt, valid_from, valid_to, superseded_by, loaded_at)
                       VALUES (?, ?, ?, '2025', '11011', ?, ?, 'holding_executive', '국민은행', ?, 'override', ?, '2025. 1월', DATE '2025-01-01', 'month', '비상근', ?,
                               ?, ?, NULL, NULL, now())""",
                    [R2025, seq, KB, nm, f"{nm}|1961-06|남", BANK, ofcps, officer, D2025, D2025],
                )
        page = self.client.get(f"/corps/{KB}", params={"as_of": "2026-03-31"}).json()
        self.assertEqual((page["corp"]["corp_name"], page["corp"]["role"], page["corp"]["parent_corp_name"]), ("KB금융", "holding", None))
        self.assertEqual(page["corp"]["latest_report"]["report_nm"], "사업보고서 (2025.12)")
        self.assertIn("rcpNo=" + R2025, page["corp"]["latest_report"]["url"])
        self.assertEqual([e["nm"] for e in page["executives"]], ["양종희", "박민수"])
        self.assertEqual([(s["nm"], s["pct"]) for s in page["shareholders"]], [("국민연금공단", 7.9)])
        self.assertEqual([(i["investee_name"], i["investee_corp_code"], i["book_value"]) for i in page["investments"]], [("국민은행", BANK, 120)])
        self.assertEqual(page["concurrent_positions"], [])   # KB 자신의 자리로 적힌 겸직은 없음
        self.assertEqual(page["stake_history"], [])          # KB에 출자한 수집 대상 회사는 없음
        for key in ("executives", "shareholders", "investments"):
            for row in page[key]:
                self.assertIn("rcpNo=", row["url"])
                self.assertNotIn("birth_ym", row)
                self.assertNotIn("person_id", row)
        # 자회사 쪽: 지주 겸직 표의 임원 자리만(비임원 제외), 지분 추이는 기준일순 두 행, 임원 표는 없음
        bank = self.client.get(f"/corps/{BANK}", params={"as_of": "2026-03-31"}).json()
        self.assertEqual((bank["corp"]["parent_corp_name"], bank["corp"]["stake_pct"], bank["corp"]["latest_report"]), ("KB금융", 100.0, None))
        self.assertEqual([(c["nm"], c["ofcps"], c["holding_corp_name"]) for c in bank["concurrent_positions"]], [("양종희", "기타비상무이사", "KB금융")])
        self.assertNotIn("person_id", bank["concurrent_positions"][0])
        self.assertEqual([(h["stlm_dt"], h["pct"], h["book_value"], h["investor_corp_name"]) for h in bank["stake_history"]],
                         [("2024-12-31", 100.0, 100, "KB금융"), ("2025-12-31", 100.0, 120, "KB금융")])
        self.assertEqual(bank["executives"], [])
        self.assertEqual(self.client.get("/corps/00000000").status_code, 404)

    def test_shareholders_as_of(self):
        r = self.client.get(f"/corps/{KB}/shareholders", params={"as_of": "2026-01-15"}).json()
        self.assertEqual([(s["nm"], s["pct"]) for s in r["shareholders"]], [("국민연금공단", 7.9)])

    def test_ask_search_only_and_with_llm(self):
        r = self.client.post("/ask", json={"question": "KB금융 주당 현금배당금은?", "use_llm": False}).json()
        self.assertIsNone(r["answer"])
        self.assertEqual(r["hits"][0]["rcept_no"], R2025)
        self.assertEqual((r["hits"][0]["corp_code"], r["hits"][0]["corp_name"]), (KB, "KB금융"))   # 화면이 회사 페이지로 잇는다
        self.assertFalse(r["hits"][0]["cited"])
        self.assertEqual(self.llm.calls, [])

        r = self.client.post("/ask", json={"question": "KB금융 주당 현금배당금은?"}).json()
        self.assertEqual(r["answer"]["citations"], [1])
        self.assertFalse(r["answer"]["not_found"])
        self.assertTrue(r["hits"][0]["cited"])
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.client.post("/ask", json={"question": "a"}).status_code, 422)

    def test_ask_with_viewer_key_header(self):
        # 보는 사람의 키(X-OpenAI-Key)는 그 요청의 LLM에만 쓰이고 응답·로그에 남지 않는다 (0013). 없으면 None → 서버 .env 키 차례
        self.client.post("/ask", json={"question": "KB금융 주당 현금배당금은?"}, headers={"X-OpenAI-Key": " sk-viewer "})
        self.client.post("/ask", json={"question": "KB금융 주당 현금배당금은?"})
        self.assertEqual(self.keys_seen, ["sk-viewer", None])
        r = self.client.post("/ask", json={"question": "KB금융 주당 현금배당금은?"}, headers={"X-OpenAI-Key": "sk-viewer"})
        self.assertNotIn("sk-viewer", r.text)
        # CORS 사전 요청이 이 헤더를 허용해야 브라우저가 보낼 수 있다
        r = self.client.options("/ask", headers={"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
                                                 "Access-Control-Request-Headers": "x-openai-key"})
        self.assertIn("x-openai-key", r.headers.get("access-control-allow-headers", "").lower())

    def test_ask_without_any_key_is_503(self):
        # 가짜 LLM 공장 없이 실제 경로: .env 키도 헤더도 없으면 503과 안내. OpenAI는 호출되지 않는다
        app = create_app(retriever_name="chunk:section_path")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": ""}), TestClient(app) as c:
            r = c.post("/ask", json={"question": "KB금융 주당 현금배당금은?"})
        self.assertEqual(r.status_code, 503)
        self.assertIn("OpenAI 키", r.json()["detail"])

    def test_changes(self):
        r = self.client.get("/changes", params={"since": "2026-03-01", "until": "2026-03-31"}).json()
        self.assertEqual(r["summary"]["reports"], 1)
        kinds = {(c["kind"], c["subject"]) for c in r["reports"][0]["changes"]}
        self.assertIn(("executive_new", "박민수 (사외이사)"), kinds)
        self.assertIn(("shareholder_pct", "국민연금공단 보통주"), kinds)
        self.assertEqual(r["reports"][0]["changes"][0]["kind_label"], "임원 신규")
        self.assertEqual(self.client.get("/changes", params={"since": "2026-04-01", "until": "2026-03-01"}).status_code, 422)

    def test_text_changes(self):
        r = self.client.get("/changes/text", params={"since": "2026-03-01", "until": "2026-03-31"}).json()
        self.assertEqual(r["section_words"], ["위험", "회계정책", "경영진단"])
        self.assertEqual(r["summary"], {"reports": 1, "compared": 1, "sections_changed": 1, "text_changes": 1, "numbers_only": 1})
        rep = r["reports"][0]
        self.assertEqual((rep["rcept_no"], rep["prev_rcept_no"]), (R2025, R2024))
        sec = rep["sections"][0]
        self.assertEqual((sec["path"], sec["status"], sec["numbers_only"]), ("III. 재무 > 7. 위험관리", "changed", 1))
        self.assertEqual(sec["replaced"], [["신용위험은 내부등급법으로 측정합니다.", "신용위험은 내부등급법과 스트레스 테스트로 측정합니다."]])
        # 절 낱말을 바꾸면 그 절만. '배당' 절은 2025에만 있어 '추가'
        r2 = self.client.get("/changes/text", params={"since": "2026-03-01", "until": "2026-03-31", "section": ["배당"]}).json()
        self.assertEqual([(s["path"], s["status"]) for s in r2["reports"][0]["sections"]], [("III. 재무 > 6. 배당에 관한 사항", "added")])


if __name__ == "__main__":
    unittest.main()
