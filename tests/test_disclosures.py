"""공시 목록 수집·적재 오프라인 테스트. DART API를 호출하지 않는다.

실행 (저장소 루트에서):
    python -m unittest discover -s tests -v

requests.get을 가짜 응답으로 바꾸고, DuckDB는 임시 폴더의 파일을 쓴다.
"""

import unittest
from datetime import date
from unittest import mock

import requests

from src.collectors import dart_client
from src.collectors.disclosure_list import fetch_disclosures, parse_disclosures
from src.loaders.collect import collect_disclosures
from src.loaders.duckdb_store import connect
from src.pipeline.definitions import month_range
from tests.helpers import TempDuckDBTestCase

FAKE_KEY = "FAKEKEY1234567890FAKEKEY1234567890ABCDEF"


def item(rcept_no: str, rcept_dt: str, report_nm: str = "임원ㆍ주요주주특정증권등소유상황보고서") -> dict:
    return {
        "corp_code": "00688996", "corp_name": "KB금융", "stock_code": "105560", "corp_cls": "Y",
        "report_nm": report_nm, "rcept_no": rcept_no, "flr_nm": "KB금융", "rcept_dt": rcept_dt, "rm": "",
    }


def fake_response(body: dict) -> mock.Mock:
    response = mock.Mock()
    response.json.return_value = body
    response.raise_for_status.return_value = None
    return response


class DartClientTest(unittest.TestCase):
    def setUp(self):
        # 테스트가 호출 간격 때문에 느려지지 않게 한다
        patcher = mock.patch.object(dart_client, "MIN_INTERVAL_SEC", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_http_error_message_hides_api_key(self):
        error = requests.HTTPError(f"500 Server Error for url: https://opendart.fss.or.kr/api/list.json?crtfc_key={FAKE_KEY}")
        with mock.patch("requests.get", side_effect=error):
            with self.assertRaises(dart_client.DartApiError) as ctx:
                dart_client.get_json("list.json", FAKE_KEY, {})
        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertIsNone(ctx.exception.__cause__)  # 원래 예외(URL 포함)를 연결하지 않음
        self.assertTrue(ctx.exception.__suppress_context__)

    def test_rate_limit_is_separate_error(self):
        with mock.patch("requests.get", return_value=fake_response({"status": "020", "message": "요청 제한을 초과하였습니다."})):
            with self.assertRaises(dart_client.DartRateLimitError):
                dart_client.get_json("list.json", FAKE_KEY, {})

    def test_fetch_follows_pages_and_dedupes(self):
        page1 = {"status": "000", "total_page": 2, "list": [item("20261102000001", "20261102"), item("20261103000002", "20261103")]}
        # 페이지를 넘기는 사이 새 공시가 올라와 2페이지에 같은 행이 또 나온 경우
        page2 = {"status": "000", "total_page": 2, "list": [item("20261103000002", "20261103"), item("20261104000003", "20261104")]}
        with mock.patch("requests.get", side_effect=[fake_response(page1), fake_response(page2)]) as get:
            pages = fetch_disclosures(FAKE_KEY, "00688996", "20261101", "20261130")
        self.assertEqual(get.call_count, 2)
        self.assertEqual([p.request_key.split("page_no=")[1][0] for p in pages], ["1", "2"])
        self.assertNotIn(FAKE_KEY, pages[0].request_key)
        self.assertEqual(len(parse_disclosures(pages)), 3)

    def test_no_data_returns_single_page(self):
        with mock.patch("requests.get", return_value=fake_response({"status": "013", "message": "조회된 데이타가 없습니다."})):
            pages = fetch_disclosures(FAKE_KEY, "00688996", "20210101", "20210131")
        self.assertEqual(len(pages), 1)
        self.assertEqual(parse_disclosures(pages), [])


class LoadIdempotencyTest(TempDuckDBTestCase):
    def setUp(self):
        super().setUp()
        interval = mock.patch.object(dart_client, "MIN_INTERVAL_SEC", 0)
        interval.start()
        self.addCleanup(interval.stop)

    def run_collect(self, body: dict) -> dict:
        with mock.patch("requests.get", return_value=fake_response(body)):
            with connect() as con:
                return collect_disclosures(con, FAKE_KEY, "00688996", "20261101", "20261130")

    def test_same_partition_twice_keeps_row_count(self):
        body = {"status": "000", "total_page": 1, "list": [item("20261102000001", "20261102"), item("20261103000002", "20261103")]}
        first = self.run_collect(body)
        second = self.run_collect(body)
        self.assertEqual(first["rows_in_period"], 2)
        self.assertEqual(second["rows_in_period"], 2)
        with connect(read_only=True) as con:
            self.assertEqual(con.execute("SELECT count(*) FROM disclosure").fetchone()[0], 2)
            self.assertEqual(con.execute("SELECT count(*) FROM raw_api_response").fetchone()[0], 1)

    def test_rerun_updates_changed_row(self):
        self.run_collect({"status": "000", "total_page": 1, "list": [item("20261102000001", "20261102", "기존 보고서명")]})
        self.run_collect({"status": "000", "total_page": 1, "list": [item("20261102000001", "20261102", "바뀐 보고서명")]})
        with connect(read_only=True) as con:
            self.assertEqual(con.execute("SELECT report_nm FROM disclosure").fetchall(), [("바뀐 보고서명",)])


class MonthRangeTest(unittest.TestCase):
    def test_past_month_uses_last_day(self):
        self.assertEqual(month_range("2024-02-01", date(2026, 11, 23)), ("20240201", "20240229"))

    def test_current_month_is_cut_at_today(self):
        self.assertEqual(month_range("2026-11-01", date(2026, 11, 23)), ("20261101", "20261123"))


if __name__ == "__main__":
    unittest.main()
