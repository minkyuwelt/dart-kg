"""질문별 순위 진단 (노트북, Neo4j·임베딩 모델 필요): 정답 섹션 조각이 벡터·전문검색·하이브리드에서 몇 등인지 본다 (2026-11-26, d005·d033).

사용: PYTHONPATH=. python notebooks/rank_diag.py d005 d033 [--grep "1,400만" --grep "KB Pay"]
찍는 것 (질문마다):
① 질문 정보: 회사 추정(corp_code), 날짜 있음 여부, 섹션 필수 낱말, Lucene 질의
② 정답 조각: 근거 접수번호 + 경로에 evidence_section이 든 조각 목록 (seq·경로·글 앞부분)
③ 벡터 순위: 그 회사 조각 전부와 질문 벡터의 코사인을 **전수로** 계산해, 정답 조각이 회사 안에서 몇 등인지 (인덱스 근사가 아님)
④ 전문검색 순위: 실제 검색기와 같은 질의·같은 정렬(섹션 경로 먼저 → 최신 먼저 → 점수)을 크게 돌려 정답 조각이 몇 등인지, 아예 안 걸리는지
⑤ 하이브리드(실제 검색기, 회사 필터 켬) 상위 10개: ★ 정답 섹션, ○ 정답 공시의 다른 섹션
--grep: 정답 공시 안에서 그 글자가 든 조각(예: d033의 본문 항목)을 따로 찾아 ③·④ 순위를 같이 찍는다
DB를 바꾸지 않는다. LLM 호출 없음.
"""
import argparse

from eval.questions import load_questions
from src.rag.retrievers import make_retriever
from src.rag.retrievers.filters import guess_corp_code
from src.rag.retrievers.neo4j import FULLTEXT_LATEST_FACTOR, has_date, load_section_words, question_year
from src.graph.client import connect, settings
from src.graph.text_normalize import fulltext_query, section_terms
from src.loaders.duckdb_store import connect as duck_connect
from src.rag.embedding import LocalEmbedder

VEC_ALL = """
MATCH (c:Chunk {corp_code: $corp})
WITH c, vector.similarity.cosine(c.embedding, $vec) AS s
ORDER BY s DESC
RETURN c.rcept_no AS rcept_no, c.seq AS seq, c.path AS path, s AS score
"""

FT_ALL = """
CALL db.index.fulltext.queryNodes('chunk_text', $q, {limit: $n}) YIELD node AS c, score
WHERE $corp IS NULL OR c.corp_code = $corp
WITH c, score, CASE WHEN $words IS NOT NULL AND any(w IN $words WHERE c.path CONTAINS w) THEN 1 ELSE 0 END AS in_section,
     CASE WHEN $latest THEN c.rcept_no ELSE '' END AS recency,
     CASE WHEN $year IS NULL THEN 0 ELSE size(split(c.text, $year)) - 1 END AS year_hits
RETURN c.rcept_no AS rcept_no, c.seq AS seq, c.path AS path, score, in_section, year_hits
ORDER BY in_section DESC, year_hits DESC, recency DESC, score DESC
"""


def rank_of(rows, key):
    for i, r in enumerate(rows, 1):
        if (r["rcept_no"], r["seq"]) == key:
            return i, r["score"]
    return None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+")
    ap.add_argument("--grep", action="append", default=[], help="정답 공시 안에서 이 글자가 든 조각도 순위를 본다")
    args = ap.parse_args()

    qs = {q.id: q for q in load_questions()}
    s = settings()
    embedder = LocalEmbedder("kure-v3", fp16=True)
    with connect(s) as driver, duck_connect(read_only=True) as con:
        def q(cypher, **p):
            return driver.execute_query(cypher, database_=s.database, **p).records

        vocab = load_section_words(con) or set()
        hybrid = make_retriever("neo4j:hybrid:kure-v3", con, corp_filter=True, embedder=embedder, driver=driver)
        for qid in args.ids:
            item = qs[qid]
            corp = guess_corp_code(con, item.question)
            words = section_terms(item.question, vocab)
            lucene = fulltext_query(item.question, vocab)
            dated = has_date(item.question)
            print(f"\n{'=' * 80}\n# {qid}: {item.question}")
            print(f"  근거: {item.evidence_rcept_no} / {item.evidence_section}")
            print(f"  회사 추정: {corp} · 날짜 있음: {dated} · 연도: {question_year(item.question)} · 섹션 필수 낱말: {words}")
            print(f"  Lucene: {lucene or '(비어 있음 → 전문검색 건너뜀, 벡터만)'}")

            gold = q("MATCH (c:Chunk) WHERE c.rcept_no IN $r AND ($sec IS NULL OR c.path CONTAINS $sec) "
                     "RETURN c.rcept_no AS rcept_no, c.seq AS seq, c.path AS path, c.text AS text ORDER BY seq",
                     r=item.evidence_rcept_no, sec=item.evidence_section)
            extra = []
            for g in args.grep:
                extra += q("MATCH (c:Chunk) WHERE c.rcept_no IN $r AND c.text CONTAINS $g "
                           "RETURN c.rcept_no AS rcept_no, c.seq AS seq, c.path AS path, c.text AS text ORDER BY seq",
                           r=item.evidence_rcept_no, g=g)
            print(f"\n## 정답 섹션 조각 {len(gold)}개" + (f" + --grep 조각 {len(extra)}개" if args.grep else ""))

            vec = embedder.encode([item.question], is_query=True)[0]
            vrows = q(VEC_ALL, corp=corp, vec=vec) if corp else []
            # 실제 검색기처럼 Lucene이 점수 순으로 n개를 먼저 자른 뒤 Cypher가 정렬한다 (n = 20, 최신 먼저면 ×FULLTEXT_LATEST_FACTOR)
            n_real = 20 * FULLTEXT_LATEST_FACTOR if not dated else 20
            frows = q(FT_ALL, q=(f"corp_code:{corp} AND ({lucene})" if corp else lucene), corp=corp, words=words or None,
                      latest=not dated, year=question_year(item.question), n=n_real) if lucene else []
            print(f"  (회사 조각 {len(vrows)}개 중 벡터 순위 / 전문검색이 Lucene 상위 {n_real}개를 받아 정렬한 {len(frows)}개 중 순위. '전문 -'는 Lucene 상위에 못 든 것)")
            gold_keys = {(r["rcept_no"], r["seq"]) for r in gold}
            grep_keys = {(r["rcept_no"], r["seq"]) for r in extra}
            for base, rows in (("정답", gold), ("grep", extra)):
                for r in rows:
                    key = (r["rcept_no"], r["seq"])
                    if base == "grep" and key in gold_keys:
                        continue                                    # 정답 줄에서 이미 '정답+grep'으로 찍었다
                    tag = "정답+grep" if (base == "정답" and key in grep_keys) else base   # 정답 섹션 안에 grep 글자도 든 조각 — 답에 필요한 조각
                    vr, vs = rank_of(vrows, key)
                    fr, fs = rank_of(frows, key)
                    text = (r["text"] or "").replace("\n", " ")[:70]
                    vtxt = f"벡터 {vr}등({vs:.3f})" if vr else "벡터 -"
                    ftxt = f"전문 {fr}등({fs:.2f})" if fr else "전문 -"
                    print(f"  [{tag}] seq {r['seq']:>4} {vtxt:<18} {ftxt:<18} {r['path'][-40:]} | {text}")

            print("\n## 벡터 상위 5 (회사 안 전수)")
            for r in vrows[:5]:
                print(f"  {r['score']:.3f} {r['rcept_no']} seq {r['seq']} {r['path']}")
            print("## 전문검색 상위 5")
            for r in frows[:5]:
                print(f"  {r['score']:.2f} sec={r['in_section']} year={r['year_hits']} {r['rcept_no']} seq {r['seq']} {r['path']}")

            print("\n## 하이브리드 상위 10 (★ 정답 섹션, ○ 정답 공시)")
            for i, h in enumerate(hybrid.search(item.question, 10), 1):
                mark = "★" if h.rcept_no in item.evidence_rcept_no and (not item.evidence_section or item.evidence_section in h.path) else (
                    "○" if h.rcept_no in item.evidence_rcept_no else " ")
                print(f"  {i:>2} {mark} {h.score:.4f} {h.rcept_no} {h.path} | {h.snippet[:50]!r}")


if __name__ == "__main__":
    main()
