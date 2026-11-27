"""관계 테이블의 행 수·NULL 수를 한 장으로 찍는다 (읽기 전용). 적재 코드 정리 전후 비교용.

    PYTHONPATH=. python notebooks/table_counts.py > /tmp/before.txt      # 정리 전(main) 상태
    python -m src.loaders.investments rebuild && python -m src.loaders.relations rebuild   # 정리한 코드로 다시 만들기
    PYTHONPATH=. python notebooks/table_counts.py > /tmp/after.txt
    diff /tmp/before.txt /tmp/after.txt                                   # 아무것도 안 나오면 같다 (loaded_at은 세지 않음)
"""

from src.loaders.duckdb_store import connect

# 표마다 (NULL 수를 셀 열들). 이름 연결·사람 식별·닫기 결과가 바뀌면 여기서 드러난다
TABLES = {
    "investment": ["investee_corp_code", "share_class", "frst_acqs_de", "stlm_dt", "valid_from", "valid_to", "superseded_by"],
    "major_shareholder": ["holder_corp_code", "relate", "stlm_dt", "valid_to", "superseded_by"],
    "major_shareholder_change": ["qota_rt", "change_cause", "valid_to"],
    "executive": ["birth_ym", "sexdstn", "tenure_start_on", "tenure_end_on", "hffc_months", "valid_to", "superseded_by"],
    "concurrent_position": ["person_id", "target_corp_code", "appointed_on", "valid_to", "superseded_by"],
    "person": ["birth_ym", "sexdstn", "first_seen", "last_seen"],
}
GROUPS = {
    "investment": "resolve_method",
    "major_shareholder": "resolve_method",
    "executive": "tenure_start_method",
    "concurrent_position": "person_method",
}


def main() -> None:
    with connect(read_only=True) as con:
        for table, cols in TABLES.items():
            n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            print(f"{table}: {n:,}행")
            for c in cols:
                k = con.execute(f"SELECT count(*) FROM {table} WHERE {c} IS NULL").fetchone()[0]
                print(f"  {c} NULL {k:,}")
            if table in GROUPS:
                for value, k in con.execute(f"SELECT {GROUPS[table]}, count(*) FROM {table} GROUP BY 1 ORDER BY 1 NULLS LAST").fetchall():
                    print(f"  {GROUPS[table]}={value}: {k:,}")
            if table != "person":
                h = con.execute(f"SELECT md5(string_agg(row::VARCHAR, '|' ORDER BY row::VARCHAR)) FROM (SELECT * EXCLUDE (loaded_at) FROM {table}) row").fetchone()[0]
                print(f"  내용 해시 {h}")


if __name__ == "__main__":
    main()
