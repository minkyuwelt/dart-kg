"""전문검색 진단 (노트북, Neo4j 필요): "연혁"이 연혁 절 조각의 꼬리표에 왜 안 맞는지 본다 (2026-11-26, d033).

사용: python notebooks/fulltext_diag.py [접수번호] [섹션 글자] [검색어]
기본: 20260313000876 연혁 연혁
찍는 것: ① 전문검색 인덱스 설정(분석기) ② 그 보고서에서 섹션 경로에 글자가 든 조각의 text_ft 앞부분
③ 검색어로 전문검색을 크게 돌려 그 조각들이 걸리는지(걸린 수 / 전체 수)
"""
import sys

from src.graph.client import connect, settings

rcept = sys.argv[1] if len(sys.argv) > 1 else "20260313000876"
sec = sys.argv[2] if len(sys.argv) > 2 else "연혁"
term = sys.argv[3] if len(sys.argv) > 3 else "연혁"
s = settings()
with connect(s) as driver:
    def q(cypher, **p):
        return driver.execute_query(cypher, database_=s.database, **p).records

    print("## 인덱스")
    for r in q("SHOW FULLTEXT INDEXES YIELD name, properties, options RETURN name, properties, options"):
        print(" ", r["name"], r["properties"], r["options"])

    rows = q("MATCH (c:Chunk {rcept_no: $r}) WHERE c.path CONTAINS $sec RETURN c.seq AS seq, c.path AS path, c.text_ft AS ft ORDER BY seq", r=rcept, sec=sec)
    print(f"\n## {rcept} 섹션 경로에 '{sec}'가 든 조각 {len(rows)}개 (text_ft 앞 120자)")
    for r in rows:
        ft = r["ft"] or ""
        print(f"  seq {r['seq']}: {ft[:120]!r}  … '{term}' 포함: {term in ft}")

    hits = q("CALL db.index.fulltext.queryNodes('chunk_text', $t, {limit: 200000}) YIELD node AS c, score "
             "WHERE c.rcept_no = $r RETURN c.seq AS seq, c.path AS path, score ORDER BY score DESC", t=f'"{term}"', r=rcept)
    print(f"\n## 전문검색 '\"{term}\"' → 이 보고서에서 걸린 조각 {len(hits)}개")
    for h in hits[:30]:
        print(f"  seq {h['seq']} {h['score']:.2f} {h['path']}")
    want = {r["seq"] for r in rows}
    got = {h["seq"] for h in hits}
    print(f"\n섹션 조각 {len(want)}개 중 걸린 것 {len(want & got)}개, 안 걸린 seq: {sorted(want - got)}")

    # 파서가 OR/AND/괄호/필드를 어떻게 읽는지 — 질의 꼴마다 이 보고서에서 걸린 조각 수와 섹션 조각(want) 중 걸린 수
    corp = q("MATCH (c:Chunk {rcept_no: $r}) RETURN c.corp_code AS c LIMIT 1", r=rcept)[0]["c"]
    R = f'("{term}은" OR "{term}")'
    forms = [
        f'"{term}"',
        f'"{term}은"',
        R,
        f'"{term}" OR "주요"',
        f'"{term}" AND "주요"',
        f'{R} OR ({R} AND "주요")',
        f'{R} OR ({R} AND ("2025 년" OR 2025*))',
        f'{R} OR ({R} AND ("케이비국민카드의" OR "케이비국민카드"))',
        f'corp_code:{corp}',
        f'corp_code:{corp} AND "{term}"',
        f'corp_code:{corp} AND ({R})',
        f'corp_code:{corp} AND ({R} OR ({R} AND "주요"))',
    ]
    print(f"\n## 질의 꼴별 결과 (이 보고서 {rcept}, 섹션 조각 {len(want)}개 기준)")
    for f in forms:
        try:
            hs = q("CALL db.index.fulltext.queryNodes('chunk_text', $t, {limit: 200000}) YIELD node AS c, score "
                   "WHERE c.rcept_no = $r RETURN c.seq AS seq", t=f, r=rcept)
            seqs = {h["seq"] for h in hs}
            print(f"  {len(hs):4d}개, 섹션 조각 {len(seqs & want)}/{len(want)}  ←  {f}")
        except Exception as e:  # noqa: BLE001 — 진단이라 파서 오류도 그대로 보여 준다
            print(f"  오류 {type(e).__name__}: {str(e)[:100]}  ←  {f}")
