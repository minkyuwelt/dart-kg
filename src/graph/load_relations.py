"""DuckDB의 관계 테이블(출자·최대주주·임원)을 Neo4j 그래프로 옮긴다 (Phase 4, 결정 0011 결정 2).

원칙 (CLAUDE.md): DuckDB가 원본, Neo4j는 진열대. 여기 코드는 읽어서 넣기만 하고 아무것도 새로 계산하지 않는다.
시간 값(valid_from·valid_to·superseded_by)도 DuckDB 닫기 단계(0005)가 정한 것을 그대로 옮긴다.

노드
    (:Corp {key, corp_code, name, names, role, has_corp_code})
        key = corp_code. corp_code가 없는 출자 대상·최대주주(해외 법인·펀드·국민연금·개인 등)는
              "name:{보고한 회사 corp_code}:{정규화 이름}" (0011 미결 ②·⑤).
              보고한 회사를 키에 넣는 이유: 이름만으로 묶으면 서로 다른 회사 표에 나온 동명이인(개인 주주)이
              한 노드가 된다. 이름으로 잇는 것은 추측이라 하지 않는다.
        names = 공시 목록(disclosure)에 나온 그 회사의 이름들 (옛 이름 포함, 처음 나온 순). 0011 미결 ①
    (:Person {pid, name})
        pid = person_id(이름|생년월|성별, 0006)의 해시 앞 16자리. person_id를 그대로 올리지 않는 이유:
              생년월은 식별에만 쓰고 어디에도 표시하지 않는다(0006·0012). 그래프 질의 결과에 키가 섞여 나가도 생년월이 새지 않게.
관계 (모두 row_key = DuckDB 행의 키. 시간 4개 속성은 DuckDB 값 그대로)
    (Corp)-[:OWNS {source_table: 'investment'|'major_shareholder', pct, shares, book_value, share_class, relate, purpose}]->(Corp)
    (Person)-[:HOLDS_POSITION {title, registered, outside, full_time}]->(Corp)
    (Corp)-[:FILED]->(Document)     Phase 3에서 이미 있는 Document 노드에만 잇는다 (새 Document를 만들지 않음)
    (Corp)-[:FORMERLY {on, reason, evidence_rcept_no}]->(Corp)   config/corp_lineage.csv (사람이 근거와 함께 적음)

다시 만들기: 관계 층(Corp·Person 노드와 그 관계)은 매번 지우고 통째로 다시 넣는다. DuckDB 관계 테이블도 rebuild로
통째로 다시 만들어지므로(행이 사라질 수 있음) MERGE만으로는 사라진 행이 Neo4j에 남는다. 크기가 수만 행이라 몇 초면 된다.
조각 층(Document·Chunk, load_chunks.py)은 건드리지 않는다.

CLI (노트북, Neo4j 컨테이너가 떠 있어야 함):
    python -m src.graph.load_relations build    # 지우고 다시 넣기
    python -m src.graph.load_relations stats    # DuckDB 행 수 ↔ Neo4j 관계 수 대조
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import re
import sys
import time
from pathlib import Path

import duckdb

from src.graph.client import connect, settings, wait_until_ready
from src.loaders.duckdb_store import connect as duck_connect
from src.graph.names import NAME_CHARS, has_name, linked_names
from src.graph.templates import is_ceo
from src.loaders.corp_names import normalize_name

LINEAGE_PATH = Path(__file__).resolve().parents[2] / "config" / "corp_lineage.csv"
LINEAGE_COLUMNS = ["current_corp_code", "former_corp_code", "former_name", "on", "reason", "evidence_rcept_no"]
BATCH = 5000

SCHEMA = [
    "CREATE CONSTRAINT corp_key_unique IF NOT EXISTS FOR (c:Corp) REQUIRE c.key IS UNIQUE",
    "CREATE CONSTRAINT person_pid_unique IF NOT EXISTS FOR (p:Person) REQUIRE p.pid IS UNIQUE",
    "CREATE INDEX corp_corp_code IF NOT EXISTS FOR (c:Corp) ON (c.corp_code)",
] + [
    # 시점 필터(valid_from <= d < valid_to)와 MERGE 키(row_key)에 쓰는 관계 범위 인덱스
    f"CREATE INDEX {rel.lower()}_{prop} IF NOT EXISTS FOR ()-[r:{rel}]-() ON (r.{prop})"
    for rel in ("OWNS", "HOLDS_POSITION") for prop in ("row_key", "valid_from", "valid_to")
]

CLEAR = "MATCH (n) WHERE n:Corp OR n:Person DETACH DELETE n"

CORP_MERGE = """
UNWIND $rows AS row
MERGE (c:Corp {key: row.key})
SET c.corp_code = row.corp_code, c.name = row.name, c.names = row.names, c.role = row.role, c.has_corp_code = row.has_corp_code
"""

PERSON_MERGE = """
UNWIND $rows AS row
MERGE (p:Person {pid: row.pid})
SET p.name = row.name
"""

_TIME_SET = ("r.valid_from = date(row.valid_from), r.valid_to = date(row.valid_to), "
             "r.source_rcept_no = row.source_rcept_no, r.superseded_by = row.superseded_by")

OWNS_MERGE = f"""
UNWIND $rows AS row
MATCH (a:Corp {{key: row.src}})
MATCH (b:Corp {{key: row.dst}})
MERGE (a)-[r:OWNS {{row_key: row.row_key}}]->(b)
SET r.source_table = row.source_table, r.pct = row.pct, r.shares = row.shares, r.book_value = row.book_value,
    r.share_class = row.share_class, r.relate = row.relate, r.purpose = row.purpose, {_TIME_SET}
"""

POSITION_MERGE = f"""
UNWIND $rows AS row
MATCH (p:Person {{pid: row.pid}})
MATCH (c:Corp {{key: row.corp}})
MERGE (p)-[r:HOLDS_POSITION {{row_key: row.row_key}}]->(c)
SET r.title = row.title, r.registered = row.registered, r.outside = row.outside, r.full_time = row.full_time,
    r.duty = row.duty, r.ceo = row.ceo, r.source_table = row.source_table, {_TIME_SET}
"""

FORMERLY_MERGE = """
UNWIND $rows AS row
MATCH (a:Corp {key: row.current})
MATCH (b:Corp {key: row.former})
MERGE (a)-[r:FORMERLY]->(b)
SET r.on = date(row.on), r.reason = row.reason, r.evidence_rcept_no = row.evidence_rcept_no
"""

FILED_MERGE = """
MATCH (d:Document) WHERE d.corp_code IS NOT NULL
MATCH (c:Corp {key: d.corp_code})
MERGE (c)-[:FILED]->(d)
"""

NEO4J_COUNTS = """
OPTIONAL MATCH (c:Corp) WITH count(c) AS corps
OPTIONAL MATCH (p:Person) WITH corps, count(p) AS persons
OPTIONAL MATCH ()-[o:OWNS]->() WITH corps, persons, count(o) AS owns
OPTIONAL MATCH ()-[h:HOLDS_POSITION]->() WITH corps, persons, owns, count(h) AS positions
OPTIONAL MATCH ()-[f:FORMERLY]->() WITH corps, persons, owns, positions, count(f) AS formerly
OPTIONAL MATCH ()-[d:FILED]->() RETURN corps, persons, owns, positions, formerly, count(d) AS filed
"""


def person_key(person_id: str) -> str:
    """person_id → 생년월이 보이지 않는 고정 키. 같은 person_id는 언제나 같은 값."""
    return hashlib.sha256(person_id.encode("utf-8")).hexdigest()[:16]


def name_key(reporter_corp_code: str, name: str) -> str:
    """corp_code가 없는 상대의 키. 보고한 회사 안에서만 같은 이름을 한 노드로 본다."""
    return f"name:{reporter_corp_code}:{name}"


def _iso(d) -> str | None:
    return d.isoformat() if d is not None else None


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[dict]:
    cur = con.execute(sql, params or [])
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def load_lineage(path: Path = LINEAGE_PATH) -> list[dict]:
    """config/corp_lineage.csv → 행 목록. 근거 없는 줄은 받지 않는다 (추측으로 잇지 않기 위해)."""
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != LINEAGE_COLUMNS:
            raise ValueError(f"{path.name}의 머리줄은 {','.join(LINEAGE_COLUMNS)} 이어야 합니다: {reader.fieldnames}")
        for i, row in enumerate(reader, 2):
            row = {k: (v or "").strip() for k, v in row.items()}
            where = f"{path.name} {i}번째 줄: "
            for col in ("current_corp_code", "former_corp_code"):
                if not re.fullmatch(r"\d{8}", row[col]):
                    raise ValueError(f"{where}{col}는 8자리 숫자여야 합니다: {row[col]!r}")
            if row["current_corp_code"] == row["former_corp_code"]:
                raise ValueError(f"{where}corp_code가 같으면 FORMERLY가 아니라 이름 이력입니다 (0011 미결 ①)")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", row["on"]):
                raise ValueError(f"{where}on은 YYYY-MM-DD여야 합니다: {row['on']!r}")
            if not row["reason"] or not re.fullmatch(r"\d{14}", row["evidence_rcept_no"]):
                raise ValueError(f"{where}reason과 근거 접수번호(14자리)가 있어야 합니다")
            out.append(row)
    return out


def owns_rows(con: duckdb.DuckDBPyConnection) -> list[dict]:
    """출자 표(출자한 회사가 보고)와 최대주주 표(출자받은 회사가 보고)를 OWNS 행 하나의 모양으로."""
    out = []
    for r in _rows(con, """
        SELECT rcept_no, row_seq, investor_corp_code, investee_name, investee_corp_code, share_class, invstmnt_purps,
               trmend_blce_qy, trmend_blce_qota_rt, trmend_blce_acntbk_amount, valid_from, valid_to, superseded_by
        FROM investment ORDER BY rcept_no, row_seq"""):
        if not has_name(r["investee_name"]):
            continue  # 이름 칸이 '-'인 행 (출자 표의 빈 줄·합계 줄). 회사가 아니므로 노드를 만들지 않는다. duckdb_counts도 같이 뺀다
        dst = r["investee_corp_code"] or name_key(r["investor_corp_code"], r["investee_name"])
        out.append({
            "row_key": f"inv:{r['rcept_no']}:{r['row_seq']}", "source_table": "investment",
            "src": r["investor_corp_code"], "dst": dst, "dst_name": r["investee_name"],
            "src_name": None,
            "pct": r["trmend_blce_qota_rt"], "shares": r["trmend_blce_qy"], "book_value": r["trmend_blce_acntbk_amount"],
            "share_class": r["share_class"], "relate": None, "purpose": r["invstmnt_purps"],
            "valid_from": _iso(r["valid_from"]), "valid_to": _iso(r["valid_to"]),
            "source_rcept_no": r["rcept_no"], "superseded_by": r["superseded_by"],
        })
    for r in _rows(con, """
        SELECT source_rcept_no, row_seq, corp_code, nm, relate, stock_knd, holder_corp_code,
               trmend_posesn_stock_co, trmend_posesn_stock_qota_rt, valid_from, valid_to, superseded_by
        FROM major_shareholder ORDER BY source_rcept_no, row_seq"""):
        name = normalize_name(r["nm"])[0] or r["nm"].strip()
        src = r["holder_corp_code"] or name_key(r["corp_code"], name)
        out.append({
            "row_key": f"msh:{r['source_rcept_no']}:{r['row_seq']}", "source_table": "major_shareholder",
            "src": src, "dst": r["corp_code"], "src_name": name,
            "dst_name": None,
            "pct": r["trmend_posesn_stock_qota_rt"], "shares": r["trmend_posesn_stock_co"], "book_value": None,
            "share_class": r["stock_knd"], "relate": r["relate"], "purpose": None,
            "valid_from": _iso(r["valid_from"]), "valid_to": _iso(r["valid_to"]),
            "source_rcept_no": r["source_rcept_no"], "superseded_by": r["superseded_by"],
        })
    return out


def _one_line(text: str | None) -> str | None:
    return " ".join(text.split()) if text else text


# 지주 겸직 표(concurrent_position, 0014) 중 그래프에 올리는 행: 사람이 이어졌고, 같은 사람·회사·기간이 임원 표(executive)에 이미 없는 것.
# 겸직 회사가 자기 임원 표를 내면(국민은행 등) 임원 표 행이 있으므로 올리지 않는다 → 늘어나는 것은 임원 표가 없는 회사의 자리만. duckdb_counts와 같이 쓴다.
# 직책이 임원이 아닌 행(본부장·센터장·부장, is_officer false)도 올리지 않는다 — 질문은 "임원 겸직"이고 임원 표의 기준과 맞춘다
CONCURRENT_GRAPH_SQL = """
    SELECT c.source_rcept_no, c.row_seq, c.holding_corp_code, c.person_id, c.target_name, c.target_corp_code, c.ofcps, c.fte_at,
           c.valid_from, c.valid_to, c.superseded_by
    FROM concurrent_position c
    WHERE c.person_id IS NOT NULL AND c.is_officer
      AND NOT EXISTS (
        SELECT 1 FROM executive e
        WHERE e.person_id = c.person_id AND e.corp_code = c.target_corp_code AND e.superseded_by IS NULL
          AND e.valid_from <= c.valid_from AND (e.valid_to IS NULL OR e.valid_to > c.valid_from))
"""


def position_rows(con: duckdb.DuckDBPyConnection) -> tuple[list[dict], list[dict], int]:
    """(HOLDS_POSITION 행, Person 행, person_id가 없어 건너뛴 행 수). 생년월·성별은 여기서 버린다.
    임원 표(executive) 행 + 지주 겸직 표(concurrent_position) 행 (0014, 임원 표에 같은 자리가 없는 것만)."""
    rows = _rows(con, """
        SELECT source_rcept_no, row_seq, corp_code, person_id, ofcps, rgist_exctv_at, fte_at, chrg_job, valid_from, valid_to, superseded_by
        FROM executive ORDER BY source_rcept_no, row_seq""")
    names = dict(con.execute("SELECT person_id, nm FROM person").fetchall())
    out, persons, skipped = [], {}, 0
    for r in rows:
        if not r["person_id"]:
            skipped += 1
            continue
        pid = person_key(r["person_id"])
        persons[pid] = names.get(r["person_id"]) or r["person_id"].split("|", 1)[0]
        out.append({
            "row_key": f"exe:{r['source_rcept_no']}:{r['row_seq']}", "pid": pid, "corp": r["corp_code"], "source_table": "executive",
            # 직위 칸의 줄바꿈('기타\n비상무이사')은 공백 하나로. 원문은 DuckDB에 그대로 있다
            "title": _one_line(r["ofcps"]), "registered": _one_line(r["rgist_exctv_at"]), "outside": _one_line(r["rgist_exctv_at"]) == "사외이사",
            "full_time": r["fte_at"],
            # 담당업무(한 줄로)와 대표이사 여부. 금융지주는 대표이사를 담당업무 칸에 적는다 (templates.is_ceo)
            "duty": _one_line(r["chrg_job"]), "ceo": is_ceo(r["ofcps"], r["chrg_job"]),
            "valid_from": _iso(r["valid_from"]), "valid_to": _iso(r["valid_to"]),
            "source_rcept_no": r["source_rcept_no"], "superseded_by": r["superseded_by"],
        })
    for r in _rows(con, CONCURRENT_GRAPH_SQL + " ORDER BY c.source_rcept_no, c.row_seq"):
        pid = person_key(r["person_id"])
        persons.setdefault(pid, names.get(r["person_id"]) or r["person_id"].split("|", 1)[0])
        title = _one_line(r["ofcps"])
        out.append({
            "row_key": f"cp:{r['source_rcept_no']}:{r['row_seq']}", "pid": pid, "source_table": "concurrent_position",
            # 겸직 회사가 corp_code로 안 이어지면(해외 법인 등) 출자 표와 같은 이름 노드
            "corp": r["target_corp_code"] or name_key(r["holding_corp_code"], r["target_name"]), "corp_name": r["target_name"],
            "title": title, "registered": None, "outside": bool(title and "사외이사" in title), "full_time": r["fte_at"],
            "duty": None, "ceo": is_ceo(r["ofcps"], None),
            "valid_from": _iso(r["valid_from"]), "valid_to": _iso(r["valid_to"]),
            "source_rcept_no": r["source_rcept_no"], "superseded_by": r["superseded_by"],
        })
    return out, [{"pid": k, "name": v} for k, v in sorted(persons.items())], skipped


def corp_rows(con: duckdb.DuckDBPyConnection, owns: list[dict], positions: list[dict], lineage: list[dict]) -> list[dict]:
    """관계 양 끝에 나오는 회사 전부를 노드로. 관계에서 노드를 뽑으므로 MATCH가 끝점을 못 찾는 일이 없다."""
    codes: set[str] = set()
    name_nodes: dict[str, str] = {}
    for o in owns:
        for side in ("src", "dst"):
            key = o[side]
            if key.startswith("name:"):
                name_nodes[key] = o[f"{side}_name"]
            else:
                codes.add(key)
    for p in positions:
        if p["corp"].startswith("name:"):
            name_nodes[p["corp"]] = p.get("corp_name") or p["corp"].rsplit(":", 1)[-1]
        else:
            codes.add(p["corp"])
    lineage_names = {}
    for row in lineage:
        codes.update((row["current_corp_code"], row["former_corp_code"]))
        lineage_names[row["former_corp_code"]] = row["former_name"]
    codes.update(c for (c,) in con.execute("SELECT corp_code FROM target_corp").fetchall())

    current = dict(con.execute("SELECT corp_code, corp_name FROM corp").fetchall())
    roles = dict(con.execute("SELECT corp_code, role FROM target_corp").fetchall())
    history: dict[str, list[str]] = {}
    for code, name in con.execute(
        """SELECT corp_code, corp_name FROM disclosure WHERE corp_name IS NOT NULL
           GROUP BY corp_code, corp_name ORDER BY corp_code, min(rcept_dt), corp_name""").fetchall():
        history.setdefault(code, []).append(name)
    # 공시 목록 다음에 출자 표·최대주주 표의 다른 이름 ('KB국민카드', '신한금융지주회사'). 라우터 사전과 같은 출처 (names.py)
    for code, name in sorted(linked_names(con)):
        if name not in history.setdefault(code, []):
            history[code].append(name)
    # 출자 표에 corp_code로 연결된 이름 (corp 테이블에 없는 회사의 이름 대신)
    inv_names = dict(con.execute(
        "SELECT investee_corp_code, any_value(investee_name) FROM investment WHERE investee_corp_code IS NOT NULL GROUP BY 1").fetchall())

    out = []
    for code in sorted(codes):
        name = current.get(code) or lineage_names.get(code) or inv_names.get(code) or code
        names = list(history.get(code, []))
        if name not in names:
            names.append(name)
        out.append({"key": code, "corp_code": code, "name": name, "names": names, "role": roles.get(code), "has_corp_code": True})
    for key, name in sorted(name_nodes.items()):
        out.append({"key": key, "corp_code": None, "name": name, "names": [name], "role": None, "has_corp_code": False})
    return out


def _batches(rows: list[dict], size: int = BATCH):
    for i in range(0, len(rows), size):
        yield rows[i:i + size]


def _strip(rows: list[dict], keep: tuple[str, ...]) -> list[dict]:
    return [{k: r[k] for k in keep} for r in rows]


OWNS_KEYS = ("row_key", "source_table", "src", "dst", "pct", "shares", "book_value", "share_class", "relate", "purpose",
             "valid_from", "valid_to", "source_rcept_no", "superseded_by")


def build(con: duckdb.DuckDBPyConnection, driver, database: str = "neo4j", lineage_path: Path = LINEAGE_PATH, log=print) -> dict:
    """관계 층을 지우고 다시 넣는다. 돌려주는 값은 넣은 수와 걸린 시간."""
    t0 = time.perf_counter()
    lineage = load_lineage(lineage_path)  # CSV가 틀렸으면 Neo4j를 지우기 전에 멈춘다
    owns = owns_rows(con)
    positions, persons, skipped = position_rows(con)
    corps = corp_rows(con, owns, positions, lineage)

    for stmt in SCHEMA:
        driver.execute_query(stmt, database_=database)
    driver.execute_query(CLEAR, database_=database)
    for rows in _batches(corps):
        driver.execute_query(CORP_MERGE, rows=rows, database_=database)
    for rows in _batches(persons):
        driver.execute_query(PERSON_MERGE, rows=rows, database_=database)
    for rows in _batches(owns):
        driver.execute_query(OWNS_MERGE, rows=_strip(rows, OWNS_KEYS), database_=database)
    for rows in _batches(positions):
        driver.execute_query(POSITION_MERGE, rows=rows, database_=database)
    if lineage:
        driver.execute_query(FORMERLY_MERGE, database_=database, rows=[
            {"current": r["current_corp_code"], "former": r["former_corp_code"], "on": r["on"], "reason": r["reason"],
             "evidence_rcept_no": r["evidence_rcept_no"]} for r in lineage])
    driver.execute_query(FILED_MERGE, database_=database)
    out = {"corps": len(corps), "name_only_corps": sum(1 for c in corps if not c["has_corp_code"]), "persons": len(persons),
           "owns": len(owns), "positions": len(positions), "positions_skipped_no_person_id": skipped, "formerly": len(lineage),
           "seconds": round(time.perf_counter() - t0, 1)}
    log(f"Corp {out['corps']:,} (이름만 {out['name_only_corps']:,}), Person {out['persons']:,}, OWNS {out['owns']:,}, "
        f"HOLDS_POSITION {out['positions']:,}, FORMERLY {out['formerly']}  {out['seconds']}초")
    return out


def duckdb_counts(con: duckdb.DuckDBPyConnection, lineage_path: Path = LINEAGE_PATH) -> dict:
    inv = con.execute(f"SELECT count(*) FROM investment WHERE regexp_matches(investee_name, '{NAME_CHARS}')").fetchone()[0]
    msh = con.execute("SELECT count(*) FROM major_shareholder").fetchone()[0]
    exe = con.execute("SELECT count(*) FROM executive WHERE person_id IS NOT NULL").fetchone()[0]
    cp = con.execute(f"SELECT count(*) FROM ({CONCURRENT_GRAPH_SQL})").fetchone()[0]   # 지주 겸직 표 중 그래프에 올리는 행 (0014)
    return {"owns": inv + msh, "positions": exe + cp, "formerly": len(load_lineage(lineage_path))}


def stats(con: duckdb.DuckDBPyConnection, driver, database: str = "neo4j", lineage_path: Path = LINEAGE_PATH) -> dict:
    want = duckdb_counts(con, lineage_path)
    rec = driver.execute_query(NEO4J_COUNTS, database_=database).records[0]
    have = {k: rec[k] for k in ("corps", "persons", "owns", "positions", "formerly", "filed")}
    match = all(want[k] == have[k] for k in ("owns", "positions", "formerly"))
    return {"duckdb": want, "neo4j": have, "match": match}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="DuckDB 관계 테이블 → Neo4j 그래프 (0011)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    sub.add_parser("stats")
    args = ap.parse_args(argv)
    s = settings()
    with connect(s) as driver:
        wait_until_ready(driver)
        with duck_connect(read_only=True) as con:
            if args.cmd == "build":
                print(build(con, driver, s.database))
            out = stats(con, driver, s.database)
    d, n = out["duckdb"], out["neo4j"]
    print(f"DuckDB  OWNS {d['owns']:,}  HOLDS_POSITION {d['positions']:,}  FORMERLY {d['formerly']}")
    print(f"Neo4j   OWNS {n['owns']:,}  HOLDS_POSITION {n['positions']:,}  FORMERLY {n['formerly']}  "
          f"(Corp {n['corps']:,}, Person {n['persons']:,}, FILED {n['filed']:,})")
    print("일치" if out["match"] else "불일치 — build를 다시 돌린다. 계속 다르면 끝점(Corp·Person)이 빠진 행이 있는지 본다")
    return 0 if out["match"] else 1


if __name__ == "__main__":
    sys.exit(main())
