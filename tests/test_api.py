"""FastAPI(src/api/main.py) 테스트. 가짜 LLM과 DuckDB 키워드 검색기(chunk:section_path)로 엔드포인트 6개를 확인한다. 외부 호출 없음.

확인하는 것
1) /health, /corps, /corps/{code}/executives·shareholders (시점 필터, 생년월 없음, 모르는 회사 404)
2) /ask: use_llm=false면 검색만, true면 가짜 LLM 답과 인용 표시, 입력 검증(빈 질문 422)
3) /changes: 변화 리포트 JSON과 요약, since > until 422
"""

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

from src.api.main import create_app
from src.loaders.chunks import build_chunks
from src.loaders.duckdb_store import connect, upsert_disclosures
from tests.test_change_report import D2024, D2025, KB, R2024, R2025, add_exec, add_holder
from tests.test_rag_ask import FakeLLM
from tests.test_report_details import disclosure_row


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(self.tmp.name) / "t.duckdb")})
        env.start()
        self.addCleanup(env.stop)
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
        self.llm = FakeLLM("KB금융의 주당 현금배당금은 보통주 3,000원입니다.", [1])
        app = create_app(llm_factory=lambda: self.llm, retriever_name="chunk:section_path")
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

    def test_shareholders_as_of(self):
        r = self.client.get(f"/corps/{KB}/shareholders", params={"as_of": "2026-01-15"}).json()
        self.assertEqual([(s["nm"], s["pct"]) for s in r["shareholders"]], [("국민연금공단", 7.9)])

    def test_ask_search_only_and_with_llm(self):
        r = self.client.post("/ask", json={"question": "KB금융 주당 현금배당금은?", "use_llm": False}).json()
        self.assertIsNone(r["answer"])
        self.assertEqual(r["hits"][0]["rcept_no"], R2025)
        self.assertFalse(r["hits"][0]["cited"])
        self.assertEqual(self.llm.calls, [])

        r = self.client.post("/ask", json={"question": "KB금융 주당 현금배당금은?"}).json()
        self.assertEqual(r["answer"]["citations"], [1])
        self.assertFalse(r["answer"]["not_found"])
        self.assertTrue(r["hits"][0]["cited"])
        self.assertEqual(len(self.llm.calls), 1)
        self.assertEqual(self.client.post("/ask", json={"question": "a"}).status_code, 422)

    def test_changes(self):
        r = self.client.get("/changes", params={"since": "2026-03-01", "until": "2026-03-31"}).json()
        self.assertEqual(r["summary"]["reports"], 1)
        kinds = {(c["kind"], c["subject"]) for c in r["reports"][0]["changes"]}
        self.assertIn(("executive_new", "박민수 (사외이사)"), kinds)
        self.assertIn(("shareholder_pct", "국민연금공단 보통주"), kinds)
        self.assertEqual(r["reports"][0]["changes"][0]["kind_label"], "임원 신규")
        self.assertEqual(self.client.get("/changes", params={"since": "2026-04-01", "until": "2026-03-01"}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
