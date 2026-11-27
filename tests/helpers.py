"""테스트 공용 보조. 25개 파일에 복사돼 있던 임시 DuckDB setUp과 가짜 Neo4j 결과를 한곳에 (코드 리뷰 B9).

TempDuckDBTestCase
    setUp에서 임시 폴더를 만들고 DUCKDB_PATH(와 extra_env()가 주는 것)를 환경변수로 패치한다. 테스트가 끝나면 둘 다 되돌린다.
    self.tmp(TemporaryDirectory)·self.db_path를 쓸 수 있다. 추가 환경변수(DART_API_KEY, DOCUMENTS_PATH 등)는 extra_env()를 덮어써 돌려준다.
    연결은 각 테스트가 연다 — 어떤 테스트는 setUp에서 한 번 쓰고 닫고, 어떤 테스트는 self.con을 열어 둔다.

FakeResult
    driver.execute_query 가짜가 돌려주는 결과 모양 (.records만 있으면 된다).
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock


class TempDuckDBTestCase(unittest.TestCase):
    DB_FILE = "test.duckdb"

    def extra_env(self) -> dict[str, str]:
        """DUCKDB_PATH 외에 같이 패치할 환경변수. 필요한 테스트가 덮어쓴다 (self.tmp를 써도 된다)."""
        return {}

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / self.DB_FILE)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": self.db_path, **self.extra_env()})
        env.start()
        self.addCleanup(env.stop)


class FakeResult:
    def __init__(self, records):
        self.records = records
