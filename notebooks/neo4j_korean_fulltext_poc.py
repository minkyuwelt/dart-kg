"""Neo4j 전문검색(fulltext index)이 한국어 공시 문장을 얼마나 잘 찾는지 비교하는 실험.

배경: docs/decisions/0001-tech-stack.md의 "확인이 필요한 것" 첫 항목.
Neo4j에 그래프·벡터·전문검색을 모으기로 했는데, 전문검색이 한국어를 제대로 처리하는지 몰랐다.

실험용 Neo4j 띄우기 (다른 수업 프로젝트가 7474·7687을 쓰므로 다른 포트, 127.0.0.1에만 열기):
    docker run -d --name dartkg-neo4j-poc \\
      -p 127.0.0.1:17474:7474 -p 127.0.0.1:17687:7687 \\
      -e NEO4J_AUTH="neo4j/<비밀번호>" \\
      -e NEO4J_server_memory_heap_max__size=512m -e NEO4J_server_memory_pagecache_size=256m \\
      neo4j:2026.06-community

실행 (저장소 루트에서):
    NEO4J_POC_PASSWORD=<비밀번호> python notebooks/neo4j_korean_fulltext_poc.py

끝나면:
    docker rm -f dartkg-neo4j-poc

새 패키지를 설치하지 않으려고 Python 드라이버 대신 컨테이너 안의 cypher-shell을 호출한다.
DuckDB와 DART API는 쓰지 않는다.
"""

import os
import re
import subprocess
import sys

CONTAINER = "dartkg-neo4j-poc"

# 공시 제목(실제 list.json의 report_nm 형식)과 사업보고서 본문 같은 문장
DOCS = {
    "d01": "최대주주등소유주식변동신고서",
    "d02": "임원ㆍ주요주주특정증권등소유상황보고서",
    "d03": "[기재정정]상각형조건부자본증권발행결정(자회사의 주요경영사항)",
    "d04": "대표이사(대표집행임원)변경(안내공시)",
    "d05": "현금ㆍ현물배당결정",
    "d06": "기업설명회(IR)개최(안내공시)",
    "d07": "당사의 최대주주는 KB금융지주이며 지분율은 100%입니다.",
    "d08": "신한금융지주회사의 자회사인 신한은행은 2025년 말 기준 총자산 500조원입니다.",
    "d09": "우리금융지주는 2019년 1월 주식이전을 통해 설립되었습니다.",
    "d10": "하나금융지주의 사외이사 선임 안건이 주주총회에서 가결되었습니다.",
    "d11": "주주총회소집결의",
    "d12": "타법인주식및출자증권취득결정",
    # 영문·숫자가 한글에 붙은 문장 (회사명에 흔함: KB국민은행, SK하이닉스, 신한DS)
    "d13": "KB국민은행과 SK하이닉스가 2025년 공동 사업을 시작했습니다.",
    "d14": "신한DS는 신한금융그룹의 IT 자회사입니다.",
}

# 검색어 → 사람이 정한 정답 문서
QUERIES = {
    "최대주주": {"d01", "d07"},
    "금융지주": {"d07", "d08", "d09", "d10"},
    "금융그룹": {"d14"},
    "자회사": {"d03", "d08", "d14"},
    "배당": {"d05"},
    "임원": {"d02", "d04"},
    "주주총회": {"d10", "d11"},
    "사외이사": {"d10"},
    "출자": {"d12"},
    "대표이사 변경": {"d04"},
    "주식": {"d01", "d09", "d12"},
    "국민은행": {"d13"},
    "하이닉스": {"d13"},
    "KB국민은행": {"d13"},
    "2025년": {"d08", "d13"},
}

# 영문·숫자와 한글이 붙어 있으면 cjk 분석기가 그 덩어리를 쪼개지 않는다 ('KB금융지주이며' 전체가 토큰 하나).
# 저장할 때와 검색할 때 모두 경계에 공백을 넣는다.
BOUNDARY_RE = re.compile(r"(?<=[A-Za-z0-9])(?=[가-힣])|(?<=[가-힣])(?=[A-Za-z0-9])")


def split_script_boundary(text: str) -> str:
    return BOUNDARY_RE.sub(" ", text)


def phrase_query(q: str) -> str:
    return " AND ".join(f'"{t}"' for t in q.split())


# (이름, 인덱스, 검색어를 Lucene 질의로 바꾸는 방법)
METHODS = [
    ("standard 그대로", "poc_std", lambda q: q),
    ("standard 앞부분 일치(*)", "poc_std", lambda q: " ".join(f"{t}*" for t in q.split())),
    ("cjk 그대로", "poc_cjk", lambda q: q),
    ("cjk 구절(\"\")", "poc_cjk", phrase_query),
    ("cjk 구절 + 경계 공백", "poc_cjk_pre", lambda q: phrase_query(split_script_boundary(q))),
]


def cypher(query: str) -> str:
    password = os.environ["NEO4J_POC_PASSWORD"]
    result = subprocess.run(
        ["docker", "exec", "-i", CONTAINER, "cypher-shell", "-u", "neo4j", "-p", password, "--format", "plain"],
        input=query, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip())
    return result.stdout


def cypher_string(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def setup() -> None:
    cypher(
        "MATCH (n:PocDoc) DETACH DELETE n; MATCH (n:PocMix) DETACH DELETE n;"
        "DROP INDEX poc_std IF EXISTS; DROP INDEX poc_cjk IF EXISTS; DROP INDEX poc_cjk_pre IF EXISTS;"
    )
    rows = ", ".join(
        f"{{id: {cypher_string(k)}, text: {cypher_string(v)}, text_pre: {cypher_string(split_script_boundary(v))}}}"
        for k, v in DOCS.items()
    )
    cypher(f"UNWIND [{rows}] AS r CREATE (:PocDoc:PocStd:PocCjk:PocCjkPre {{id: r.id, text: r.text, text_pre: r.text_pre}});")
    # 같은 속성에 분석기만 다른 인덱스 2개를 만들려고 라벨을 나눴다
    cypher(
        "CREATE FULLTEXT INDEX poc_std FOR (n:PocStd) ON EACH [n.text] "
        "OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-no-stop-words'}};"
        "CREATE FULLTEXT INDEX poc_cjk FOR (n:PocCjk) ON EACH [n.text] "
        "OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}};"
        "CREATE FULLTEXT INDEX poc_cjk_pre FOR (n:PocCjkPre) ON EACH [n.text_pre] "
        "OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}};"
        "CALL db.awaitIndexes(60);"
    )


def search(index: str, lucene_query: str) -> set[str]:
    out = cypher(
        f"CALL db.index.fulltext.queryNodes({cypher_string(index)}, {cypher_string(lucene_query)}) "
        "YIELD node RETURN node.id AS id;"
    )
    return {line.strip().strip('"') for line in out.splitlines()[1:] if line.strip()}


def main() -> None:
    if "NEO4J_POC_PASSWORD" not in os.environ:
        sys.exit("NEO4J_POC_PASSWORD 환경변수가 필요합니다.")
    setup()
    totals = {}
    print(f"{'검색어':<10} {'방법':<22} {'찾음':<28} 재현율 정밀도  놓침 / 잘못 찾음")
    for query, relevant in QUERIES.items():
        for name, index, to_lucene in METHODS:
            found = search(index, to_lucene(query))
            hit = found & relevant
            recall = len(hit) / len(relevant)
            precision = len(hit) / len(found) if found else 0.0
            t = totals.setdefault(name, [0, 0, 0])
            t[0] += len(hit)
            t[1] += len(relevant)
            t[2] += len(found)
            print(
                f"{query:<10} {name:<22} {','.join(sorted(found)) or '-':<28} {recall:5.0%} {precision:6.0%}  "
                f"{','.join(sorted(relevant - found)) or '-'} / {','.join(sorted(found - relevant)) or '-'}"
            )
    print(f"\n== 합계 (검색어 {len(QUERIES)}개)")
    for name, (hit, relevant, found) in totals.items():
        print(f"{name:<22} 재현율 {hit / relevant:5.0%} ({hit}/{relevant})  정밀도 {hit / found if found else 0:5.0%} ({hit}/{found})")


if __name__ == "__main__":
    main()
