"""Neo4j 전문검색(fulltext index)이 한국어 공시 문장을 얼마나 잘 찾는지 비교하는 실험.

배경: docs/decisions/0001-tech-stack.md의 "확인이 필요한 것" 첫 항목. 결과 기록: docs/decisions/0004-korean-fulltext.md

이 스크립트가 하는 일
    1) 실험용 Neo4j 컨테이너를 띄운다 (이미지 버전 고정, 포트는 .env로 변경 가능, 127.0.0.1에만 열기)
    2) 사용 가능한 전문검색 분석기 목록을 기록한다
    3) DART 실제 표기가 들어간 문장을 넣고, 분석기·검색 방식 6가지로 검색어마다
       재현율(정답 중 찾은 비율), 정밀도(찾은 것 중 정답 비율), 엉뚱하게 걸린 결과 수를 잰다
    4) 끝나면 컨테이너와 볼륨을 삭제한다 (--keep이면 남김)

실행 (저장소 루트에서). 형태소 분석 방식(6번)은 kiwipiepy가 필요하다.
프로젝트 의존성에 넣지 않으려고 실험용 가상환경을 따로 만든다:
    python3 -m venv /tmp/neo4j-poc-venv && /tmp/neo4j-poc-venv/bin/pip install kiwipiepy==0.25.1
    /tmp/neo4j-poc-venv/bin/python notebooks/neo4j_korean_fulltext_poc.py --out notebooks/neo4j_korean_fulltext_poc_result.txt
kiwipiepy가 없으면 6번 방식만 건너뛴다.

.env (선택. 다른 프로젝트가 7474·7687을 쓰므로 기본값은 17474·17687)
    NEO4J_POC_HTTP_PORT=17474
    NEO4J_POC_BOLT_PORT=17687

비밀번호는 실행할 때마다 새로 만들어 컨테이너에만 넘기고 출력하지 않는다.
새 패키지를 늘리지 않으려고 Python 드라이버 대신 컨테이너 안의 cypher-shell을 호출한다.
DuckDB와 DART API는 쓰지 않는다.
"""

import argparse
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

IMAGE = "neo4j:2026.06.0-community"  # 2026-11-23 기준. digest sha256:42fd5b9ead4d...
CONTAINER = "dartkg-neo4j-poc"
REPO_ROOT = Path(__file__).resolve().parents[1]

MIDDLE_DOT = "ㆍ"  # 'ㆍ' HANGUL LETTER ARAEA. DART 보고서명의 가운뎃점은 이 문자다 (U+00B7 '·'가 아님)

# 공시 제목(list.json report_nm의 실제 표기)과 사업보고서 본문 같은 문장
DOCS = {
    "d01": "최대주주등소유주식변동신고서",
    "d02": f"임원{MIDDLE_DOT}주요주주특정증권등소유상황보고서",
    "d03": "[기재정정]사업보고서 (2025.12)",
    "d04": "[기재정정]상각형조건부자본증권발행결정(자회사의 주요경영사항)",
    "d05": "대표이사(대표집행임원)변경(안내공시)",
    "d06": f"현금{MIDDLE_DOT}현물배당결정",
    "d07": "주주총회소집공고",
    "d08": "주주총회소집결의",
    "d09": "타법인주식및출자증권취득결정",
    "d10": "기업설명회(IR)개최(안내공시)",
    "d11": "당사의 최대 주주는 KB금융지주이며 지분율은 100%입니다.",
    "d12": "KB금융은 2025년 말 기준 자회사 11곳을 두고 있습니다.",
    "d13": "신한금융지주회사의 자회사인 신한은행은 총자산 500조원 규모입니다.",
    "d14": "우리금융지주는 2019년 1월 주식이전을 통해 설립되었습니다.",
    "d15": "하나금융지주의 사외이사 선임 안건이 정기주주총회에서 가결되었습니다.",
    "d16": "KB국민은행과 SK하이닉스가 공동 사업을 시작했습니다.",
    "d17": "소액주주의 의결권 행사를 돕기 위해 전자투표를 도입했습니다.",
    "d18": "신한DS는 신한금융그룹의 IT 자회사입니다.",
    "d19": "당사는 주요주주인 국민연금공단과 특수관계에 있지 않습니다.",
}

# 검색어 → 사람이 정한 정답 문서.
# 판단 기준: 검색어가 가리키는 "개념"에 대한 문서인가. 예를 들어 '주주'(주식을 가진 사람)는
# 주주총회(회의)와 다른 개념이라 주주총회 문서는 정답이 아니다 → 걸리면 "엉뚱한 결과"
QUERIES = {
    "최대주주": {"d01", "d11"},
    "최대 주주": {"d01", "d11"},
    "주주": {"d01", "d02", "d11", "d17", "d19"},
    "주요주주": {"d02", "d19"},
    "주주총회": {"d07", "d08", "d15"},
    "금융지주": {"d11", "d13", "d14", "d15"},
    "KB금융": {"d11", "d12"},
    "자회사": {"d04", "d12", "d13", "d18"},
    "배당": {"d06"},
    "임원": {"d02", "d05"},
    "사외이사": {"d15"},
    "출자": {"d09"},
    "대표이사 변경": {"d05"},
    "주식": {"d01", "d09", "d14"},
    "기재정정": {"d03", "d04"},
    "사업보고서": {"d03"},
    "국민은행": {"d16"},
    "하이닉스": {"d16"},
    "2025년": {"d12"},
}

# --- 전처리 ---------------------------------------------------------------

# 영문·숫자와 한글이 붙어 있으면 cjk 분석기가 그 덩어리를 쪼개지 않는다 ('KB금융지주이며' 전체가 토큰 하나)
BOUNDARY_RE = re.compile(r"(?<=[A-Za-z0-9])(?=[가-힣])|(?<=[가-힣])(?=[A-Za-z0-9])")
# 가운뎃점은 한글 자모로 분류되어 앞뒤 글자와 한 덩어리가 되므로 공백으로 바꾼다
SEPARATOR_RE = re.compile(f"[{MIDDLE_DOT}·•・]")


def preprocess(text: str) -> str:
    """색인과 검색에 똑같이 적용하는 전처리: 가운뎃점 → 공백, 영문·숫자와 한글 경계에 공백."""
    return BOUNDARY_RE.sub(" ", SEPARATOR_RE.sub(" ", text))


def phrase_query(q: str) -> str:
    """낱말마다 큰따옴표로 묶은 구절 검색, 낱말끼리는 AND."""
    return " AND ".join(f'"{t}"' for t in q.split())


def phrase_query_pre(q: str) -> str:
    """사용자가 띄어 쓴 낱말 단위로 따옴표를 치고, 전처리는 따옴표 안에서 한다.

    'KB금융' → "KB 금융" (cjk가 KB와 금융이 붙어 있을 때만 찾음), '대표이사 변경' → "대표이사" AND "변경".
    전처리를 따옴표 밖에서 하면 'KB금융' → "KB" AND "금융"이 되어, 두 조각이 문서 아무 데나
    따로 있어도 걸린다. 긴 문서일수록 정밀도가 샌다 (PR #13 리뷰).
    """
    return " AND ".join(f'"{preprocess(t)}"' for t in q.split())


def load_kiwi():
    try:
        from kiwipiepy import Kiwi
    except ImportError:
        return None
    return Kiwi()


KIWI = load_kiwi()
# 색인에 남길 품사: 일반명사, 고유명사, 외국어, 숫자, 한자. 의존명사('등')·조사·어미는 뺀다
MORPH_TAGS = {"NNG", "NNP", "SL", "SN", "SH"}


def morph_tokens(text: str) -> list[str]:
    return [t.form.lower() for t in KIWI.tokenize(text) if t.tag in MORPH_TAGS]


def morph_query(q: str) -> str:
    tokens = morph_tokens(q)
    return " AND ".join(f'"{t}"' for t in tokens) if tokens else '""'


# (이름, 인덱스, 검색어 → Lucene 질의)
METHODS = [
    ("1 standard 그대로", "poc_std", lambda q: q),
    ("2 standard 앞부분(*)", "poc_std", lambda q: " ".join(f"{t}*" for t in q.split())),
    ("3 cjk 그대로", "poc_cjk", lambda q: q),
    ("4 cjk 구절", "poc_cjk", phrase_query),
    ("5 cjk 구절+전처리", "poc_cjk_pre", phrase_query_pre),
]
if KIWI:
    METHODS.append(("6 형태소+whitespace", "poc_morph", morph_query))

# --- Neo4j 컨테이너 --------------------------------------------------------


def read_env_ports() -> tuple[str, str]:
    """.env에서 실험용 포트를 읽는다 (python-dotenv 없이 KEY=VALUE만 해석). 환경변수가 우선."""
    values = {}
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    http = os.getenv("NEO4J_POC_HTTP_PORT") or values.get("NEO4J_POC_HTTP_PORT") or "17474"
    bolt = os.getenv("NEO4J_POC_BOLT_PORT") or values.get("NEO4J_POC_BOLT_PORT") or "17687"
    return http, bolt


class Neo4jPoc:
    def __init__(self):
        self.password = secrets.token_urlsafe(18)

    def start(self) -> None:
        http, bolt = read_env_ports()
        subprocess.run(["docker", "rm", "-f", "-v", CONTAINER], capture_output=True)
        subprocess.run(
            [
                "docker", "run", "-d", "--name", CONTAINER,
                "-p", f"127.0.0.1:{http}:7474", "-p", f"127.0.0.1:{bolt}:7687",
                "-e", f"NEO4J_AUTH=neo4j/{self.password}",
                "-e", "NEO4J_server_memory_heap_max__size=512m",
                "-e", "NEO4J_server_memory_pagecache_size=256m",
                IMAGE,
            ],
            check=True, capture_output=True,
        )
        for _ in range(60):
            try:
                self.cypher("RETURN 1;")
                return
            except RuntimeError:
                time.sleep(2)
        raise RuntimeError("Neo4j가 2분 안에 준비되지 않았습니다")

    def stop(self) -> None:
        # -v: 이미지가 선언한 /data, /logs 이름 없는 볼륨까지 함께 지운다
        subprocess.run(["docker", "rm", "-f", "-v", CONTAINER], check=True, capture_output=True)

    def cypher(self, query: str) -> str:
        result = subprocess.run(
            ["docker", "exec", "-i", CONTAINER, "cypher-shell", "-u", "neo4j", "-p", self.password, "--format", "plain"],
            input=query, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip().replace(self.password, "***"))
        return result.stdout


def cypher_string(s: str) -> str:
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def setup(db: Neo4jPoc) -> None:
    rows = []
    for k, v in DOCS.items():
        morph = " ".join(morph_tokens(v)) if KIWI else ""
        rows.append(
            f"{{id: {cypher_string(k)}, text: {cypher_string(v)}, "
            f"text_pre: {cypher_string(preprocess(v))}, text_morph: {cypher_string(morph)}}}"
        )
    # Neo4j는 같은 라벨·속성에 전문검색 인덱스를 두 개 만들 수 없다 (분석기가 달라도 "equivalent index already exists").
    # 그래서 원문(text)에 인덱스 두 개를 걸 때는 라벨을 나눈다
    db.cypher(f"UNWIND [{', '.join(rows)}] AS r CREATE (:PocDoc:PocStd:PocCjk {{id: r.id, text: r.text, text_pre: r.text_pre, text_morph: r.text_morph}});")
    db.cypher(
        "CREATE FULLTEXT INDEX poc_std FOR (n:PocStd) ON EACH [n.text] "
        "OPTIONS {indexConfig: {`fulltext.analyzer`: 'standard-no-stop-words'}};"
        "CREATE FULLTEXT INDEX poc_cjk FOR (n:PocCjk) ON EACH [n.text] OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}};"
        "CREATE FULLTEXT INDEX poc_cjk_pre FOR (n:PocDoc) ON EACH [n.text_pre] OPTIONS {indexConfig: {`fulltext.analyzer`: 'cjk'}};"
        "CREATE FULLTEXT INDEX poc_morph FOR (n:PocDoc) ON EACH [n.text_morph] "
        "OPTIONS {indexConfig: {`fulltext.analyzer`: 'whitespace'}};"
        "CALL db.awaitIndexes(60);"
    )


def search(db: Neo4jPoc, index: str, lucene_query: str) -> set[str]:
    out = db.cypher(
        f"CALL db.index.fulltext.queryNodes({cypher_string(index)}, {cypher_string(lucene_query)}) "
        "YIELD node RETURN node.id AS id;"
    )
    return {line.strip().strip('"') for line in out.splitlines()[1:] if line.strip()}


def run(db: Neo4jPoc, emit) -> None:
    version = db.cypher("CALL dbms.components() YIELD versions, edition RETURN versions[0] + ' ' + edition;").splitlines()[1]
    emit(f"Neo4j {version.strip(chr(34))} ({IMAGE})")
    analyzers = db.cypher(
        "CALL db.index.fulltext.listAvailableAnalyzers() YIELD analyzer RETURN analyzer ORDER BY analyzer;"
    ).splitlines()[1:]
    analyzers = [a.strip().strip('"') for a in analyzers if a.strip()]
    emit(f"\n== 사용 가능한 전문검색 분석기 {len(analyzers)}개 (db.index.fulltext.listAvailableAnalyzers)")
    emit(", ".join(analyzers))
    emit(f"한국어 전용 분석기(nori/korean) 있음: {any(a in ('nori', 'korean') for a in analyzers)}")
    if KIWI:
        import kiwipiepy
        emit(f"형태소 분석: kiwipiepy {kiwipiepy.__version__}, 색인 품사 {sorted(MORPH_TAGS)}")
    else:
        emit("형태소 분석: kiwipiepy 없음 → 6번 방식 건너뜀")

    setup(db)
    emit(f"\n== 검색어별 결과 (문서 {len(DOCS)}개, 검색어 {len(QUERIES)}개)")
    emit(f"{'검색어':<10} {'방법':<20} {'찾음':<36} 재현율 정밀도 엉뚱  놓침 / 엉뚱하게 걸림")
    totals = {name: [0, 0, 0] for name, _, _ in METHODS}
    for query, relevant in QUERIES.items():
        for name, index, to_lucene in METHODS:
            found = search(db, index, to_lucene(query))
            hit, wrong, missed = found & relevant, found - relevant, relevant - found
            t = totals[name]
            t[0] += len(hit)
            t[1] += len(relevant)
            t[2] += len(found)
            emit(
                f"{query:<10} {name:<20} {','.join(sorted(found)) or '-':<36} "
                f"{len(hit) / len(relevant):5.0%} {len(hit) / len(found) if found else 0:6.0%} {len(wrong):>4}  "
                f"{','.join(sorted(missed)) or '-'} / {','.join(sorted(wrong)) or '-'}"
            )
    emit(f"\n== 합계 (검색어 {len(QUERIES)}개, 정답 {sum(len(v) for v in QUERIES.values())}개)")
    emit(f"{'방법':<20} {'재현율':>14} {'정밀도':>14} {'엉뚱한 결과':>8}")
    for name, (hit, relevant, found) in totals.items():
        emit(
            f"{name:<20} {hit / relevant:6.0%} ({hit:>2}/{relevant:>2}) "
            f"{hit / found if found else 0:6.0%} ({hit:>2}/{found:>2}) {found - hit:>6}건"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", help="결과를 이 파일에도 저장")
    parser.add_argument("--keep", action="store_true", help="끝나도 컨테이너를 지우지 않음")
    args = parser.parse_args()

    lines: list[str] = []

    def emit(line: str = "") -> None:
        print(line, flush=True)
        lines.append(line)

    db = Neo4jPoc()
    db.start()
    try:
        run(db, emit)
    finally:
        if not args.keep:
            db.stop()
            print(f"\n컨테이너 {CONTAINER}와 볼륨 삭제함", file=sys.stderr)
    if args.out:
        Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
