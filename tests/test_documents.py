"""Phase 3 공시 원문 수집·보관 테스트. DART API를 호출하지 않는다 (get_raw를 흉내 낸다).

확인하는 것
1) fetch_document: ZIP 응답은 그대로 돌려주고, 오류 JSON 응답은 status에 따라 예외로 바꾼다
2) inspect_zip: ZIP 안 파일 목록과 XML 선언 인코딩을 읽는다 (utf-8, euc-kr, 선언 없음)
3) save_document: 파일을 쓰고 document 행을 upsert한다. 다시 저장해도 행은 하나
4) missing_documents: 정기보고서 중 원문이 없는 것만, 정정본은 따로, --kind 제한
"""

import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from src.collectors.dart_client import DartApiError, DartRateLimitError
from src.collectors.document import fetch_document, inspect_zip
from src.loaders.documents import document_path, missing_documents, read_xml_head, save_document
from src.loaders.duckdb_store import connect, upsert_disclosures
from tests.test_report_details import disclosure_row

KB, SHINHAN = "00688996", "00382199"


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


UTF8_XML = '<?xml version="1.0" encoding="utf-8"?>\n<DOCUMENT><TITLE>사업보고서</TITLE><P>본문</P></DOCUMENT>'.encode("utf-8")
EUCKR_XML = '<?xml version="1.0" encoding="euc-kr"?>\n<DOCUMENT><TITLE>사업보고서</TITLE></DOCUMENT>'.encode("euc-kr")
NO_DECL_XML = "<DOCUMENT><TITLE>선언 없음</TITLE></DOCUMENT>".encode("cp949")


class FakeResponse:
    def __init__(self, content: bytes):
        self.content = content
        self.text = content.decode("utf-8", errors="replace")


class FetchDocumentTest(unittest.TestCase):
    def test_returns_zip_bytes(self):
        zip_bytes = make_zip({"20240318000001.xml": UTF8_XML})
        with mock.patch("src.collectors.document.get_raw", return_value=FakeResponse(zip_bytes)) as get_raw:
            self.assertEqual(fetch_document("KEY", "20240318000001"), zip_bytes)
        get_raw.assert_called_once_with("document.xml", "KEY", {"rcept_no": "20240318000001"})

    def test_error_json_becomes_dart_error(self):
        body = '{"status":"013","message":"조회된 데이타가 없습니다."}'.encode("utf-8")
        with mock.patch("src.collectors.document.get_raw", return_value=FakeResponse(body)):
            with self.assertRaises(DartApiError) as ctx:
                fetch_document("KEY", "20240318000001")
        self.assertEqual(ctx.exception.status, "013")
        self.assertNotIsInstance(ctx.exception, DartRateLimitError)

    def test_rate_limit_is_separate_exception(self):
        body = b'<result><status>020</status><message>limit</message></result>'
        with mock.patch("src.collectors.document.get_raw", return_value=FakeResponse(body)):
            with self.assertRaises(DartRateLimitError):
                fetch_document("KEY", "20240318000001")

    def test_rejects_bad_rcept_no_without_calling_api(self):
        with mock.patch("src.collectors.document.get_raw") as get_raw:
            with self.assertRaises(ValueError):
                fetch_document("KEY", "2024-03-18")
        get_raw.assert_not_called()


class InspectZipTest(unittest.TestCase):
    def test_reads_names_sizes_and_declared_encodings(self):
        zip_bytes = make_zip({"a.xml": UTF8_XML, "b.xml": EUCKR_XML, "c.xml": NO_DECL_XML, "img/logo.png": b"\x89PNG"})
        entries = inspect_zip(zip_bytes)
        self.assertEqual([e.name for e in entries], ["a.xml", "b.xml", "c.xml", "img/logo.png"])
        self.assertEqual([e.encoding for e in entries], ["utf-8", "euc-kr", None, None])
        self.assertEqual(entries[0].size, len(UTF8_XML))


class SaveDocumentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = mock.patch.dict(os.environ, {
            "DUCKDB_PATH": str(Path(self.tmp.name) / "test.duckdb"),
            "DOCUMENTS_PATH": str(Path(self.tmp.name) / "docs"),
        })
        env.start()
        self.addCleanup(env.stop)

    def test_writes_file_and_upserts_row_once(self):
        zip1 = make_zip({"20240318000001.xml": UTF8_XML})
        zip2 = make_zip({"20240318000001.xml": EUCKR_XML, "extra.xml": UTF8_XML})
        with connect() as con:
            upsert_disclosures(con, [disclosure_row("20240318000001", "사업보고서 (2023.12)", "20240318")])
            first = save_document(con, "20240318000001", zip1)
            second = save_document(con, "20240318000001", zip2)  # 다시 받으면 덮어씀
            rows = con.execute("SELECT rcept_no, corp_code, report_nm, file_size, entry_count, xml_encoding FROM document").fetchall()
        self.assertEqual(first["entries"], 1)
        self.assertEqual(second["entries"], 2)
        self.assertEqual(rows, [("20240318000001", KB, "사업보고서 (2023.12)", len(zip2), 2, "euc-kr,utf-8")])
        self.assertEqual(document_path("20240318000001").read_bytes(), zip2)
        self.assertTrue(str(document_path("20240318000001")).startswith(self.tmp.name))

    def test_read_xml_head_decodes_declared_encoding(self):
        with connect() as con:
            save_document(con, "20240318000002", make_zip({"doc.xml": EUCKR_XML}))
        entry, head = read_xml_head("20240318000002")
        self.assertEqual(entry.name, "doc.xml")
        self.assertIn("<TITLE>사업보고서</TITLE>", head)

    def test_missing_documents_lists_each_rcept_no_including_corrections(self):
        with connect() as con:
            upsert_disclosures(con, [
                disclosure_row("20240318000001", "사업보고서 (2023.12)", "20240318"),
                disclosure_row("20240620000002", "[기재정정]사업보고서 (2023.12)", "20240620"),  # 정정본은 별도 원문
                disclosure_row("20230515000003", "분기보고서 (2023.03)", "20230515"),
                disclosure_row("20200330000004", "사업보고서 (2019.12)", "20200330"),  # from_year 밖
                disclosure_row("20240320000005", "주주총회소집공고", "20240320"),      # 정기보고서 아님
                disclosure_row("20240318000006", "사업보고서 (2023.12)", "20240318", corp_code=SHINHAN),
            ])
            save_document(con, "20230515000003", make_zip({"q.xml": UTF8_XML}))  # 이미 받은 것

            plan = missing_documents(con, [KB, SHINHAN], 2021)
            self.assertEqual([r["rcept_no"] for r in plan], ["20240620000002", "20240318000006", "20240318000001"])

            only_annual_kb = missing_documents(con, [KB], 2021, {"11011"})
            self.assertEqual([r["rcept_no"] for r in only_annual_kb], ["20240620000002", "20240318000001"])
            self.assertEqual(missing_documents(con, [KB], 2021, {"11013"}), [])  # 분기는 이미 받음
