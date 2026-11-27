"""수집 대상 목록(target_corp) 생성 테스트. DART API를 호출하지 않는다."""

import unittest
from datetime import date

from src.collectors.corp_code import HOLDING_CORPS
from src.loaders.duckdb_store import connect
from src.loaders.targets import IncompleteTargetsError, latest_business_report_year, rebuild_target_corps
from tests.helpers import TempDuckDBTestCase

# 지주별 자회사 하나씩 (corp_code는 가짜)
SUBSIDIARY_OF = {holding: f"0000010{i}" for i, holding in enumerate(HOLDING_CORPS)}


def seed_targets(con, bsns_year: str = "2025", skip_holdings: tuple[str, ...] = ()) -> None:
    """지주 4곳과 지주마다 자회사 1곳(지분 100%)을 corp·investment에 넣는다. skip_holdings는 자회사를 넣지 않는다."""
    for code, name in HOLDING_CORPS.items():
        con.execute("INSERT OR IGNORE INTO corp VALUES (?, ?, NULL, NULL, now())", [code, name])
    for seq, (holding, sub) in enumerate(SUBSIDIARY_OF.items(), start=1):
        con.execute("INSERT OR IGNORE INTO corp VALUES (?, ?, NULL, NULL, now())", [sub, f"자회사{seq}"])
        if holding in skip_holdings:
            continue
        con.execute(
            """
            INSERT INTO investment (rcept_no, row_seq, investor_corp_code, bsns_year, reprt_code, inv_prm_raw,
                                    investee_name, share_class, investee_corp_code, resolve_method,
                                    trmend_blce_qota_rt, loaded_at)
            VALUES (?, 1, ?, ?, '11011', ?, ?, NULL, ?, 'exact', 100, now())
            """,
            [f"{bsns_year}0301{seq:06d}", holding, bsns_year, f"자회사{seq}", f"자회사{seq}", sub],
        )


class LatestYearTest(unittest.TestCase):
    def test_uses_last_year_from_april(self):
        self.assertEqual(latest_business_report_year(date(2026, 11, 23)), 2025)
        self.assertEqual(latest_business_report_year(date(2026, 4, 1)), 2025)
        self.assertEqual(latest_business_report_year(date(2026, 3, 31)), 2024)


class RebuildTargetsTest(TempDuckDBTestCase):
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
            # 나머지 지주 3곳에도 자회사를 하나씩 넣는다 (자회사 0곳인 지주가 있으면 실패하므로)
            seed_targets(con, skip_holdings=("00688996",))
            stats = rebuild_target_corps(con, 2025)
            rows = con.execute("SELECT corp_code, role, stake_pct FROM target_corp ORDER BY corp_code").fetchall()
        self.assertEqual(stats["holdings"], 4)
        self.assertEqual(stats["subsidiaries"], 4)  # KB의 자회사A + 나머지 지주 3곳의 자회사
        self.assertIn(("00000001", "subsidiary", 100.0), rows)
        self.assertNotIn("00000002", [r[0] for r in rows])  # 지분 20%

    def test_rebuild_is_idempotent(self):
        with connect() as con:
            seed_targets(con)
            first = rebuild_target_corps(con, 2025)
            second = rebuild_target_corps(con, 2025)
        self.assertEqual(first, second)

    def test_keeps_previous_targets_when_a_holding_has_no_subsidiary(self):
        """새 연도에 한 지주의 출자 데이터가 없으면 실패하고, 작년 기준 목록을 그대로 둔다."""
        with connect() as con:
            seed_targets(con, bsns_year="2024")
            rebuild_target_corps(con, 2024)
            before = con.execute("SELECT * EXCLUDE (loaded_at) FROM target_corp ORDER BY corp_code").fetchall()

            # 2025: 하나금융지주의 사업보고서 출자 데이터가 아직 없음
            seed_targets(con, bsns_year="2025", skip_holdings=("00547583",))
            with self.assertRaises(IncompleteTargetsError) as ctx:
                rebuild_target_corps(con, 2025)
            after = con.execute("SELECT * EXCLUDE (loaded_at) FROM target_corp ORDER BY corp_code").fetchall()

        self.assertIn("하나금융지주(00547583)", str(ctx.exception))
        self.assertEqual(before, after)
        self.assertEqual(len(after), 8)  # 지주 4 + 자회사 4 (2024 기준)


if __name__ == "__main__":
    unittest.main()
