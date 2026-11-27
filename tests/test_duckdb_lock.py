"""DuckDB 파일 잠금 처리 오프라인 테스트. DART API를 호출하지 않는다.

확인하는 것
1) 다른 프로세스가 읽기 전용으로 열어 두기만 해도 쓰기 연결이 막힌다 (문서에 적은 전제)
2) 그때 Dagster 자산이 바로 실패하지 않고 정해진 횟수만큼 다시 시도한 뒤 실패한다
3) 잠금이 없으면 자산이 정상 실행된다

다른 프로세스는 subprocess로 띄워서 실제 노트북(Jupyter)에서 연결을 열어 둔 상황을 흉내 낸다.
"""

import subprocess
import sys
import unittest
from unittest import mock

import dagster as dg

from src.loaders.duckdb_store import connect, is_lock_error
from src.pipeline import definitions
from tests.helpers import TempDuckDBTestCase

# 읽기 전용으로 파일을 열고, 준비됐다고 알린 뒤 stdin이 닫힐 때까지 연결을 유지하는 프로세스
HOLD_READ_ONLY = (
    "import duckdb, sys\n"
    "con = duckdb.connect(sys.argv[1], read_only=True)\n"
    "print('ready', flush=True)\n"
    "sys.stdin.read()\n"
)


class DuckDBLockTest(TempDuckDBTestCase):
    def extra_env(self):
        return {"DART_API_KEY": "FAKEKEY"}

    def setUp(self):
        super().setUp()
        # 파일과 테이블을 먼저 만들어 둔다
        with connect():
            pass

    def hold_read_only(self) -> subprocess.Popen:
        """다른 프로세스에서 읽기 전용 연결을 열어 둔다. 테스트가 끝나면 닫는다."""
        proc = subprocess.Popen(
            [sys.executable, "-c", HOLD_READ_ONLY, self.db_path],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )
        self.assertEqual(proc.stdout.readline().strip(), "ready")

        def release():
            proc.stdin.close()
            proc.wait(timeout=10)
            proc.stdout.close()

        self.addCleanup(release)
        return proc

    def test_read_only_connection_in_other_process_blocks_writer(self):
        self.hold_read_only()
        with self.assertRaises(Exception) as ctx:
            connect()
        self.assertTrue(is_lock_error(ctx.exception))

    def test_write_connection_requests_retry_on_lock(self):
        self.hold_read_only()
        with self.assertRaises(dg.RetryRequested):
            with definitions.write_connection():
                pass

    @mock.patch.object(definitions, "LOCK_RETRY_WAIT_SEC", 0)
    @mock.patch.object(definitions, "collect_corps", return_value={"api_calls": 1, "rows": 0})
    def test_asset_retries_then_fails_while_locked(self, collect_corps):
        self.hold_read_only()
        result = dg.materialize([definitions.corp_codes], raise_on_error=False)

        self.assertFalse(result.success)
        # 처음 1번 + 다시 시도 LOCK_RETRY_MAX번
        restarts = [e for e in result.all_events if e.event_type_value == "STEP_RESTARTED"]
        self.assertEqual(len(restarts), definitions.LOCK_RETRY_MAX)
        # 잠금은 DART 호출 전에 걸리므로 수집 함수(=API 호출)는 한 번도 실행되지 않는다
        collect_corps.assert_not_called()

    @mock.patch.object(definitions, "collect_corps", return_value={"api_calls": 1, "rows": 0})
    def test_asset_succeeds_without_lock(self, collect_corps):
        result = dg.materialize([definitions.corp_codes])
        self.assertTrue(result.success)
        collect_corps.assert_called_once()


if __name__ == "__main__":
    unittest.main()
