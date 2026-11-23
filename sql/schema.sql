-- dart-kg DuckDB 테이블 정의
--
-- src/loaders/duckdb_store.py의 connect()가 쓰기 연결을 열 때마다 실행한다.
-- 모두 IF NOT EXISTS라서 몇 번을 실행해도 결과가 같다.
-- 컬럼을 바꿀 때는 data/dartkg.duckdb를 지우고 다시 수집하거나, ALTER 문을 따로 실행한다.

-- DART API 원본 응답 보관.
-- 정규화 규칙(아래 테이블 구조)을 바꿔도 API를 다시 호출하지 않고 여기서 다시 만들 수 있게 한다.
-- 같은 요청을 다시 보내면 최신 응답으로 덮어쓴다 (endpoint + request_key가 기본키).
CREATE TABLE IF NOT EXISTS raw_api_response (
    endpoint     VARCHAR     NOT NULL,  -- 예: 'list.json'
    request_key  VARCHAR     NOT NULL,  -- API 키를 뺀 요청 파라미터 (이름순 정렬된 쿼리 문자열)
    status       VARCHAR,               -- DART status ('000' 정상, '013' 데이터 없음)
    body         JSON        NOT NULL,
    fetched_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (endpoint, request_key)
);

-- 공시 대상 전체 회사 (corpCode.xml). 상장 여부와 관계없이 모두 넣는다.
-- Phase 2에서 최대주주·출자 대상의 회사명을 corp_code로 연결할 때 이 테이블을 쓴다.
-- corpCode.xml은 ZIP(XML)이라 raw_api_response에 넣지 않는다. 1건 호출로 언제든 다시 받을 수 있다.
CREATE TABLE IF NOT EXISTS corp (
    corp_code    VARCHAR     PRIMARY KEY,  -- 8자리 고유번호
    corp_name    VARCHAR     NOT NULL,
    stock_code   VARCHAR,                  -- 비상장사는 NULL. 상장폐지된 회사도 값이 남아 있을 수 있음
    modify_date  DATE,
    loaded_at    TIMESTAMPTZ NOT NULL
);

-- 공시 목록 (list.json). 공시 하나 = 행 하나.
CREATE TABLE IF NOT EXISTS disclosure (
    rcept_no     VARCHAR     PRIMARY KEY,  -- 접수번호 14자리. 앞 8자리는 접수일
    corp_code    VARCHAR     NOT NULL,
    corp_name    VARCHAR,                  -- 공시 당시 회사명 (조인에는 쓰지 않는다)
    stock_code   VARCHAR,
    corp_cls     VARCHAR,                  -- Y 유가증권, K 코스닥, N 코넥스, E 기타
    report_nm    VARCHAR     NOT NULL,     -- 보고서명. 예: '사업보고서 (2025.12)'
    flr_nm       VARCHAR,                  -- 공시 제출인
    rcept_dt     DATE        NOT NULL,     -- 접수일
    rm           VARCHAR,                  -- 비고 (예: '유' 유가증권시장본부 소관, '정' 정정)
    loaded_at    TIMESTAMPTZ NOT NULL
);
