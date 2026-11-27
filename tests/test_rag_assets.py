"""Phase 3 Dagster 자산(documents·chunks·chunk_embeddings·neo4j_chunks)과 스케줄 테스트. DART API·Neo4j·GPU 없이 돈다.

확인하는 것
1) 증분 판단 함수: missing_documents_of_report(정정본 각각), unparsed_documents, unchunked_rcept_nos, stale_documents(조각 수 비교)
2) rag_index_job을 실제 DuckDB(임시 파일)에서 끝까지 돌린다 — 임베더와 Neo4j 드라이버만 가짜. 두 번째 실행은 모두 건너뛴다
3) daily_documents는 daily_report_details와 같은 입력(disclosures 메타데이터)으로 documents 파티션 run을 만든다
4) daily_rag_index는 새 원문이 있었거나 지난 색인이 Neo4j 단계에서 멈췄을 때만 run을 만든다
"""

import unittest
from datetime import datetime
from unittest import mock
from zoneinfo import ZoneInfo

import dagster as dg

from src.graph import load_chunks
from src.graph.load_chunks import stale_documents
from src.loaders.chunks import build_chunks, unchunked_rcept_nos
from src.loaders.documents import missing_documents_of_report, unparsed_documents
from src.loaders.duckdb_store import connect, upsert_disclosures
from src.pipeline import definitions
from src.rag.embedding import ModelSpec, embed_chunks
from tests.test_embedding import FakeEmbedder
from tests.helpers import FakeResult, TempDuckDBTestCase
from tests.test_report_details import disclosure_row

KST = ZoneInfo("Asia/Seoul")
KB = "00688996"
BIZ, BIZ_FIX, HALF = "20260313001191", "20260619000667", "20250814000100"


class RecordingDriver:
    """execute_query를 기록하고, 보고서별 조각 수 질의에는 미리 정한 값을 돌려주는 가짜 Neo4j 드라이버."""

    def __init__(self):
        self.calls = []
        self.per_document: dict[str, int] = {}

    def execute_query(self, query, database_=None, **params):
        self.calls.append((query.strip(), params))
        if "RETURN d.rcept_no AS rcept_no, count(c) AS n" in query:
            return FakeResult([{"rcept_no": r, "n": n} for r, n in self.per_document.items()])
        if "UNWIND $rows AS row" in query and "Chunk" in query:  # 조각 MERGE → 가짜 저장소에 반영
            for row in params["rows"]:
                self.per_document[row["rcept_no"]] = self.per_document.get(row["rcept_no"], 0) + 1
            return FakeResult([])
        if "count(d) AS docs" in query:
            return FakeResult([{"docs": len(self.per_document), "chunks": sum(self.per_document.values()), "rels": sum(self.per_document.values())}])
        return FakeResult([])

    def verify_connectivity(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def seed_sections(con, rcept_no: str, texts: list[str]) -> None:
    for sid, text in enumerate(texts, start=1):
        con.execute(
            "INSERT INTO document_section VALUES (?, ? || '.xml', '11011', '사업보고서', ?, NULL, 1, NULL, NULL, NULL, ?, ?, false, true, ?, '[]', ?, 0)",
            [rcept_no, rcept_no, sid, f"섹션 {sid}", f"I. 개요 > 섹션 {sid}", text, len(text)],
        )


class IncrementalHelpersTest(TempDuckDBTestCase):
    def setUp(self):
        super().setUp()
        self.con = connect()
        self.addCleanup(self.con.close)
        upsert_disclosures(self.con, [
            disclosure_row(BIZ, "사업보고서 (2025.12)", "20260313"),
            disclosure_row(BIZ_FIX, "[기재정정]사업보고서 (2025.12)", "20260619"),
            disclosure_row(HALF, "반기보고서 (2025.06)", "20250814"),
        ])

    def test_missing_documents_of_report_lists_each_revision(self):
        con = self.con
        self.assertEqual([r["rcept_no"] for r in missing_documents_of_report(con, KB, "2025", "11011")], [BIZ, BIZ_FIX])
        con.execute("INSERT INTO document (rcept_no, file_path, file_size, sha256, entry_count, entries, fetched_at) "
                    "VALUES (?, 'x.zip', 1, 'h', 1, '[]', now())", [BIZ])
        self.assertEqual([r["rcept_no"] for r in missing_documents_of_report(con, KB, "2025", "11011")], [BIZ_FIX])
        self.assertEqual(missing_documents_of_report(con, KB, "2025", "11013"), [])  # 목록에 없는 보고서

    def test_unparsed_documents_only_status_000_without_parse_rows(self):
        con = self.con
        for rcept, status in [(BIZ, "000"), (HALF, "000"), (BIZ_FIX, "014")]:
            con.execute("INSERT INTO document (rcept_no, file_path, file_size, sha256, entry_count, entries, fetched_at, status) "
                        "VALUES (?, 'x.zip', 1, 'h', 1, '[]', now(), ?)", [rcept, status])
        con.execute("INSERT INTO document_parse (rcept_no, file_name, ok, parsed_at) VALUES (?, ?, true, now())", [BIZ, BIZ + ".xml"])
        self.assertEqual(unparsed_documents(con), [HALF])  # BIZ는 파싱됨, BIZ_FIX는 원문 없음(014)

    def test_unchunked_and_stale_documents(self):
        con = self.con
        seed_sections(con, BIZ, ["KB금융지주의 최대주주는 국민연금공단입니다.", "주당 현금배당금은 보통주 3,000원입니다."])
        seed_sections(con, HALF, ["반기 요약 재무정보입니다."])
        self.assertEqual(unchunked_rcept_nos(con, "section_path"), [HALF, BIZ])
        build_chunks(con, [BIZ], ["section_path"])
        self.assertEqual(unchunked_rcept_nos(con, "section_path"), [HALF])

        build_chunks(con, [HALF], ["section_path"])
        embed_chunks(con, FakeEmbedder(), "section_path", log=lambda *_: None)
        n_biz = con.execute("SELECT count(*) FROM chunk WHERE rcept_no = ?", [BIZ]).fetchone()[0]
        driver = RecordingDriver()
        self.assertEqual(stale_documents(con, "fake", "section_path", driver), [HALF, BIZ])  # Neo4j가 비어 있음
        driver.per_document = {BIZ: n_biz}
        self.assertEqual(stale_documents(con, "fake", "section_path", driver), [HALF])
        driver.per_document = {BIZ: n_biz - 1, HALF: 1}  # 수가 다르면 다시
        self.assertEqual(stale_documents(con, "fake", "section_path", driver), [BIZ])


class RagIndexJobTest(TempDuckDBTestCase):
    """chunks → chunk_embeddings → neo4j_chunks를 실제 DuckDB에서 끝까지. 임베더·Neo4j만 가짜."""

    def extra_env(self):
        return {"DART_API_KEY": "FAKEKEY"}

    def setUp(self):
        super().setUp()
        self.instance = self.enterContext(dg.instance_for_test())
        self.driver = RecordingDriver()
        # 가짜 임베더는 모델 이름 'fake'·64차원. 자산이 보는 모델 상수와 Neo4j 적재의 모델 표를 함께 바꾼다
        self.enterContext(mock.patch.object(definitions, "RAG_MODEL", "fake"))
        self.enterContext(mock.patch.dict(load_chunks.MODELS, {"fake": ModelSpec("fake", 64, 64, "테스트")}))
        self.enterContext(mock.patch.object(definitions, "make_embedder", FakeEmbedder))
        self.enterContext(mock.patch.object(definitions, "graph_connect", lambda: self.driver))
        self.enterContext(mock.patch.object(definitions, "wait_until_ready", lambda driver, timeout, log: None))
        with connect() as con:
            upsert_disclosures(con, [disclosure_row(BIZ, "사업보고서 (2025.12)", "20260313")])
            seed_sections(con, BIZ, ["KB금융지주의 최대주주는 국민연금공단입니다.", "주당 현금배당금은 보통주 3,000원입니다."])

    def run_job(self):
        result = dg.materialize([definitions.chunks, definitions.chunk_embeddings, definitions.neo4j_chunks], instance=self.instance)
        self.assertTrue(result.success)
        return {e.event_specific_data.materialization.asset_key.path[-1]: e.event_specific_data.materialization.metadata
                for e in result.get_asset_materialization_events()}

    def test_first_run_indexes_everything_second_run_skips(self):
        meta = self.run_job()
        self.assertEqual(meta["chunks"]["chunked_documents"].value, 1)
        self.assertGreater(meta["chunks"]["new_chunks"].value, 0)
        self.assertEqual(meta["chunk_embeddings"]["embedded"].value, meta["chunks"]["new_chunks"].value)
        self.assertEqual(meta["chunk_embeddings"]["still_pending"].value, 0)
        self.assertEqual(meta["neo4j_chunks"]["documents"].value, 1)
        self.assertEqual(meta["neo4j_chunks"]["chunks"].value, meta["chunks"]["new_chunks"].value)
        self.assertEqual(self.driver.per_document, {BIZ: meta["chunks"]["new_chunks"].value})

        meta = self.run_job()
        self.assertEqual(meta["chunks"]["chunked_documents"].value, 0)
        self.assertEqual(meta["chunk_embeddings"]["embedded"].value, 0)
        self.assertEqual(meta["neo4j_chunks"]["chunks"].value, 0)
        self.assertIn("skipped", meta["neo4j_chunks"])

    def test_new_document_is_indexed_incrementally(self):
        self.run_job()
        with connect() as con:
            upsert_disclosures(con, [disclosure_row(HALF, "반기보고서 (2025.06)", "20250814")])
            seed_sections(con, HALF, ["반기 요약 재무정보입니다."])
        meta = self.run_job()
        self.assertEqual(meta["chunks"]["chunked_documents"].value, 1)
        self.assertEqual(meta["neo4j_chunks"]["documents"].value, 1)  # 반기보고서만 다시 적재
        self.assertEqual(set(self.driver.per_document), {BIZ, HALF})

    def test_full_rebuild_rechunks_everything_without_reembedding(self):
        self.run_job()
        result = dg.materialize([definitions.chunks, definitions.chunk_embeddings], instance=self.instance,
                                run_config={"ops": {"chunks": {"config": {"full_rebuild": True}}}})
        self.assertTrue(result.success)
        mats = {e.event_specific_data.materialization.asset_key.path[-1]: e.event_specific_data.materialization.metadata
                for e in result.get_asset_materialization_events()}
        self.assertEqual(mats["chunks"]["chunked_documents"].value, 1)
        self.assertEqual(mats["chunk_embeddings"]["embedded"].value, 0)  # 본문 해시가 같으면 다시 임베딩하지 않는다


def schedule_context(instance, when):
    return dg.build_schedule_context(instance=instance, scheduled_execution_time=when)


class RagSchedulesTest(TempDuckDBTestCase):
    def extra_env(self):
        return {"DART_API_KEY": "FAKEKEY"}

    def setUp(self):
        super().setUp()
        self.instance = self.enterContext(dg.instance_for_test())
        self.now = datetime(2026, 11, 25, 9, tzinfo=KST)

    def report(self, asset, metadata=None, partition=None):
        self.instance.report_runless_asset_event(dg.AssetMaterialization(asset_key=asset.key, metadata=metadata or {}, partition=partition))

    def test_daily_documents_follows_new_periodic_reports(self):
        self.instance.add_dynamic_partitions("corp", [KB])
        self.report(definitions.disclosures, partition=f"{KB}|2026-11-01",
                    metadata={"corp_code": KB, definitions.PERIODIC_REPORTS_METADATA_KEY: dg.MetadataValue.json(["2025-11011"])})
        runs = list(definitions.daily_documents(schedule_context(self.instance, self.now)))
        self.assertEqual([r.partition_key for r in runs], [f"{KB}|2025-11011"])
        self.assertTrue(runs[0].run_key.startswith("documents|"))
        # 같은 입력으로 report_details 스케줄도 run을 만들지만 run_key가 다르다
        details = list(definitions.daily_report_details(schedule_context(self.instance, self.now)))
        self.assertEqual(details[0].partition_key, runs[0].partition_key)
        self.assertNotEqual(details[0].run_key, runs[0].run_key)

    def test_daily_rag_index_skips_without_new_documents(self):
        self.report(definitions.documents, partition=f"{KB}|2025-11011", metadata={definitions.DOCUMENTS_SAVED_METADATA_KEY: 0})
        self.report(definitions.chunk_embeddings)
        self.report(definitions.neo4j_chunks)
        results = list(definitions.daily_rag_index(schedule_context(self.instance, self.now)))
        self.assertIsInstance(results[0], dg.SkipReason)

    def test_daily_rag_index_runs_when_documents_saved(self):
        self.report(definitions.documents, partition=f"{KB}|2025-11011", metadata={definitions.DOCUMENTS_SAVED_METADATA_KEY: 2})
        results = list(definitions.daily_rag_index(schedule_context(self.instance, self.now)))
        self.assertIsInstance(results[0], dg.RunRequest)
        self.assertEqual(results[0].run_key, "rag_index|2026-11-25")

    def test_daily_rag_index_retries_when_neo4j_step_never_finished(self):
        # 임베딩은 됐는데 Neo4j 적재 기록이 없다 (컨테이너가 꺼져 있어 실패) → 다시 시도
        self.report(definitions.chunk_embeddings)
        results = list(definitions.daily_rag_index(schedule_context(self.instance, self.now)))
        self.assertIsInstance(results[0], dg.RunRequest)
        # Neo4j 적재가 그 뒤에 성공했으면 건너뜀
        self.report(definitions.neo4j_chunks)
        results = list(definitions.daily_rag_index(schedule_context(self.instance, self.now)))
        self.assertIsInstance(results[0], dg.SkipReason)


if __name__ == "__main__":
    unittest.main()
