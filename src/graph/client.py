"""Neo4j 접속. `.env`의 NEO4J_BOLT_PORT·NEO4J_PASSWORD로 드라이버를 연다 (docker-compose.yml과 같은 값).

    from src.graph.client import connect
    with connect() as driver:
        driver.execute_query("RETURN 1")

CLI (노트북, 컨테이너가 떠 있어야 함):
    python -m src.graph.client ping      # 연결·버전·노드 수 확인
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class Neo4jSettings:
    uri: str
    user: str
    password: str
    database: str = "neo4j"


def settings() -> Neo4jSettings:
    """비밀번호가 없으면 바로 멈춘다 — 빈 비밀번호로 연결을 시도해 알 수 없는 에러를 보는 것보다 낫다."""
    load_dotenv()
    password = os.getenv("NEO4J_PASSWORD", "")
    if not password:
        raise SystemExit("NEO4J_PASSWORD가 .env에 없습니다. .env.example을 보고 적어 주세요 (docker compose도 같은 값을 씁니다)")
    port = os.getenv("NEO4J_BOLT_PORT") or "17687"
    return Neo4jSettings(uri=f"bolt://127.0.0.1:{port}", user=os.getenv("NEO4J_USER") or "neo4j", password=password)


def connect(s: Neo4jSettings | None = None):
    """드라이버를 돌려준다. `with connect() as driver:`로 쓰면 끝날 때 닫힌다. 비밀번호는 에러 메시지에 나오지 않는다."""
    from neo4j import GraphDatabase  # 노트북·CI 모두 설치(requirements.txt)되지만, import 비용 때문에 함수 안에서

    s = s or settings()
    # UNRECOGNIZED 경고("관계 종류 FORMERLY가 없음", "속성 superseded_by가 없음")는 끈다.
    # 값이 아직 하나도 없을 때(이력 CSV가 비었거나, 정정된 관계가 없을 때) 나오는 정상 상황이라 출력만 가린다.
    # 성능 경고 등 다른 종류는 그대로 둔다
    driver = GraphDatabase.driver(s.uri, auth=(s.user, s.password), notifications_disabled_classifications=["UNRECOGNIZED"])
    return driver


def wait_until_ready(driver, timeout: float = 90.0, log=print) -> None:
    """`docker compose up -d`가 끝나도 Neo4j는 안에서 30초~1분 더 준비한다. 그동안은 연결이 끊기므로(Connection reset) 될 때까지 기다린다."""
    from neo4j.exceptions import ServiceUnavailable

    deadline = time.monotonic() + timeout
    waited = False
    while True:
        try:
            driver.verify_connectivity()
            if waited:
                log("")
            return
        except ServiceUnavailable:
            if time.monotonic() > deadline:
                raise SystemExit(f"Neo4j가 {timeout:.0f}초 안에 준비되지 않았습니다. `docker compose ps`로 상태를, `docker compose logs --tail 30 neo4j`로 로그를 확인하세요")
            if not waited:
                log("Neo4j 준비 중", end="", flush=True)
                waited = True
            log(".", end="", flush=True)
            time.sleep(3)


def ping(s: Neo4jSettings | None = None) -> dict:
    s = s or settings()
    with connect(s) as driver:
        wait_until_ready(driver)
        version = driver.execute_query(
            "CALL dbms.components() YIELD name, versions, edition RETURN name, versions[0] AS version, edition",
            database_=s.database,
        ).records[0]
        nodes = driver.execute_query("MATCH (n) RETURN count(n) AS n", database_=s.database).records[0]["n"]
    return {"uri": s.uri, "name": version["name"], "version": version["version"], "edition": version["edition"], "nodes": nodes}


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Neo4j 연결 확인")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ping")
    args = ap.parse_args(argv)
    if args.cmd == "ping":
        info = ping()
        print(f"연결됨 {info['uri']}  {info['name']} {info['version']} ({info['edition']})  노드 {info['nodes']:,}개")
    return 0


if __name__ == "__main__":
    sys.exit(main())
