"""수집 대상 목록(target_corp) 생성 테스트. DART API를 호출하지 않는다."""

import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from src.loaders.duckdb_store import connect
from src.loaders.targets import latest_business_report_year, rebuild_target_corps


class LatestYearTest(unittest.TestCase):
    def test_uses_last_year_from_april(self):
        self.assertEqual(latest_business_report_year(date(2026, 11, 23)), 2025)
        self.assertEqual(latest_business_report_year(date(2026, 4, 1)), 2025)
        self.assertEqual(latest_business_report_year(date(2026, 3, 31)), 2024)


class RebuildTargetsTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        env = mock.patch.dict(os.environ, {"DUCKDB_PATH": str(Path(tmp.name) / "t.duckdb")})
        env.start()
        self.addCleanup(env.stop)

    def test_selects_resolved_subsidiaries_over_threshold(self):
        with connect() as con:
            con.execute(
                """
                INSERT INTO corp VALUES
                  ('00688996','KB금융',NULL,NULL,now()), ('00382199','신한지주',NULL,NULL,now()),
                  ('00547583','하나금융지주',NULL,NULL,now()), ('01350869','우리금융지주',NULL,NULL,now()),
                  ('00000001','자회사A',NULL,NULL,now()), ('00000002','소수지분B',NULL,NULL,now())
                """
            )
            # 자회사A는 보통주·우선주 2행, 소수지분B는 20%, 해외법인은 corp_code 없음, 2024년 행은 연도가 달라 제외
            con.execute(
                """
                INSERT INTO investment (rcept_no, row_seq, investor_corp_code, bsns_year, reprt_code, inv_prm_raw,
                                        investee_name, share_class, investee_corp_code, resolve_method,
                                        trmend_blce_qota_rt, loaded_at) VALUES
                  ('20260301000001',1,'00688996','2025','11011','자회사A(보통주)','자회사A','보통주','00000001','exact',100,now()),
                  ('20260301000001',2,'00688996','2025','11011','자회사A(우선주)','자회사A','우선주','00000001','exact',60,now()),
                  ('20260301000001',3,'00688996','2025','11011','소수지분B','소수지분B',NULL,'00000002','exact',20,now()),
                  ('20260301000001',4,'00688996','2025','11011','KB Vietnam','KBVietnam',NULL,NULL,'not_found',100,now()),
                  ('20250301000001',1,'00688996','2024','11011','소수지분B','소수지분B',NULL,'00000002','exact',90,now())
                """
            )
            stats = rebuild_target_corps(con, 2025)
            rows = con.execute("SELECT corp_code, role, stake_pct FROM target_corp ORDER BY corp_code").fetchall()
        self.assertEqual(stats["holdings"], 4)
        self.assertEqual(stats["subsidiaries"], 1)
        self.assertIn(("00000001", "subsidiary", 100.0), rows)

    def test_rebuild_is_idempotent(self):
        with connect() as con:
            con.execute("INSERT INTO corp VALUES ('00688996','KB금융',NULL,NULL,now())")
            first = rebuild_target_corps(con, 2025)
            second = rebuild_target_corps(con, 2025)
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
