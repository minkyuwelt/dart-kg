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
    rcept_no     VARCHAR     PRIMARY KEY,  -- 접수번호 14자리. 앞 8자리는 날짜 모양이지만 접수일과 다를 수 있음(11%) → 날짜는 rcept_dt 사용
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

-- 타법인 출자현황 (정기보고서 주요정보 otrCprInvstmntSttus.json).
-- raw_api_response에서 src/loaders/investments.py가 통째로 다시 만든다 (rebuild).
-- 출자 대상은 이름으로만 오므로 investee_corp_code는 이름 연결 결과이고, 실패하면 NULL.
CREATE TABLE IF NOT EXISTS investment (
    rcept_no                   VARCHAR     NOT NULL,  -- 출자 내역이 실린 정기보고서 접수번호
    row_seq                    INTEGER     NOT NULL,  -- 응답 안에서의 행 순서. 같은 회사가 취득 시점별로 여러 행일 수 있어 이름 대신 식별자로 씀
    investor_corp_code         VARCHAR     NOT NULL,  -- 출자한 회사
    bsns_year                  VARCHAR     NOT NULL,  -- 사업연도
    reprt_code                 VARCHAR     NOT NULL,  -- 11011 사업, 11012 반기, 11013 1분기, 11014 3분기
    inv_prm_raw                VARCHAR     NOT NULL,  -- 응답에 적힌 출자 대상 이름 그대로
    investee_name              VARCHAR     NOT NULL,  -- 정규화한 이름 (법인 표기·각주·주식 종류 제거)
    share_class                VARCHAR,               -- 보통주/우선주 (표시된 경우만)
    investee_corp_code         VARCHAR,               -- 연결된 corp_code. 실패하면 NULL
    resolve_method             VARCHAR     NOT NULL,  -- override / exact / hangul / ambiguous / not_found / override_unresolvable
    invstmnt_purps             VARCHAR,               -- 출자 목적
    frst_acqs_de               DATE,                  -- 최초 취득일
    trmend_blce_qy             BIGINT,                -- 기말 보유 주식 수
    trmend_blce_qota_rt        DOUBLE,                -- 기말 지분율(%)
    trmend_blce_acntbk_amount  BIGINT,                -- 기말 장부가액(원)
    stlm_dt                    DATE,                  -- 결산 기준일
    loaded_at                  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (rcept_no, row_seq)
);

-- 수집 대상 회사 목록. src/loaders/targets.py가 investment 테이블에서 통째로 다시 만든다.
-- 범위 결정: docs/decisions/0002-target-scope.md (지주 4곳 + 직속 자회사)
CREATE TABLE IF NOT EXISTS target_corp (
    corp_code         VARCHAR     PRIMARY KEY,
    corp_name         VARCHAR     NOT NULL,
    role              VARCHAR     NOT NULL,  -- 'holding' 지주 / 'subsidiary' 직속 자회사
    parent_corp_code  VARCHAR,               -- 자회사의 지주 corp_code
    stake_pct         DOUBLE,                -- 지주의 기말 지분율(%)
    source_rcept_no   VARCHAR,               -- 근거가 된 지주 사업보고서 접수번호
    bsns_year         VARCHAR     NOT NULL,  -- 근거 사업연도
    loaded_at         TIMESTAMPTZ NOT NULL
);

-- ---------------------------------------------------------------------------
-- Phase 2 관계 테이블. 정기보고서 주요정보 API 원본에서 src/loaders/relations.py가 통째로 다시 만든다 (rebuild).
-- 시간 컬럼은 docs/decisions/0005-temporal-model.md의 이중 시간축:
--   valid_from / valid_to      사실이 참이었던 기간 (valid_to NULL = 아직 유효)
--   source_rcept_no / superseded_by  어느 공시로 알게 됐고, 어느 정정 공시가 대체했는가 (NULL = 대체되지 않음)
-- valid_to·superseded_by는 "닫기" 단계에서 채운다 (아직 구현 전). rebuild 직후에는 모두 NULL이다.
-- 주요정보 API는 보고서의 최신 정정본만 주므로, 백필한 과거 보고서는 정정본 하나만 있다.

-- 최대주주 현황 (hyslrSttus.json). 보고서 기준일(stlm_dt)에 참이었던 최대주주와 지분율.
CREATE TABLE IF NOT EXISTS major_shareholder (
    source_rcept_no             VARCHAR     NOT NULL,  -- 이 행을 뽑은 보고서 접수번호 (응답의 rcept_no)
    row_seq                     INTEGER     NOT NULL,  -- 응답 안 행 순서. 합계 행('계')은 제외
    corp_code                   VARCHAR     NOT NULL,  -- 보고서를 낸 회사
    bsns_year                   VARCHAR     NOT NULL,
    reprt_code                  VARCHAR     NOT NULL,  -- 11011 사업, 11012 반기, 11013 1분기, 11014 3분기
    nm                          VARCHAR     NOT NULL,  -- 주주 이름 그대로 (회사 또는 사람)
    relate                      VARCHAR,               -- 관계 (본인, 특수관계인 등)
    stock_knd                   VARCHAR,               -- 주식 종류
    holder_corp_code            VARCHAR,               -- 주주가 회사면 corp_code (Phase 1 이름 연결 규칙). 실패하면 NULL
    resolve_method              VARCHAR     NOT NULL,  -- override / exact / hangul / ambiguous / not_found
    bsis_posesn_stock_co        BIGINT,                -- 기초 주식 수
    bsis_posesn_stock_qota_rt   DOUBLE,                -- 기초 지분율(%)
    trmend_posesn_stock_co      BIGINT,                -- 기말 주식 수
    trmend_posesn_stock_qota_rt DOUBLE,                -- 기말 지분율(%)
    rm                          VARCHAR,
    stlm_dt                     DATE,                  -- 보고서 기준일
    valid_from                  DATE,                  -- = stlm_dt
    valid_to                    DATE,                  -- 다음 보고서에 없으면 그 보고서의 stlm_dt (닫기 단계)
    superseded_by               VARCHAR,               -- 정정 공시 접수번호 (닫기 단계)
    loaded_at                   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source_rcept_no, row_seq)
);

-- 최대주주 변동현황 (hyslrChgSttus.json). 변동일이 있어 사실 시간이 일 단위다.
-- 보고서마다 과거 변동이 누적되어 실리므로 기본키는 접수번호가 아니라 (회사, 변동일, 주주명)이고,
-- source_rcept_no는 그 변동을 처음 보고한 보고서다.
CREATE TABLE IF NOT EXISTS major_shareholder_change (
    corp_code          VARCHAR     NOT NULL,
    change_on          DATE        NOT NULL,  -- 변동일
    mxmm_shrholdr_nm   VARCHAR     NOT NULL,  -- 최대주주 이름 그대로
    posesn_stock_co    BIGINT,                -- 변동 후 주식 수
    qota_rt            DOUBLE,                -- 변동 후 지분율(%). 응답은 '9.97%'
    change_cause       VARCHAR,               -- 변동 원인
    rm                 VARCHAR,
    source_rcept_no    VARCHAR     NOT NULL,  -- 처음 보고한 보고서 접수번호
    bsns_year          VARCHAR     NOT NULL,  -- 그 보고서의 사업연도
    reprt_code         VARCHAR     NOT NULL,
    stlm_dt            DATE,
    valid_from         DATE        NOT NULL,  -- = change_on
    valid_to           DATE,                  -- 같은 회사의 다음 변동일 (닫기 단계)
    superseded_by      VARCHAR,
    loaded_at          TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (corp_code, change_on, mxmm_shrholdr_nm)
);

-- 임원 현황 (exctvSttus.json). 사람 식별자(person)는 결정 0006(예정)에서 정한다.
-- 선임일 필드가 없어 valid_from은 기준일에서 재직기간(월)을 뺀 근사값이다 (valid_from_method에 표시).
CREATE TABLE IF NOT EXISTS executive (
    source_rcept_no       VARCHAR     NOT NULL,
    row_seq               INTEGER     NOT NULL,
    corp_code             VARCHAR     NOT NULL,
    bsns_year             VARCHAR     NOT NULL,
    reprt_code            VARCHAR     NOT NULL,
    nm                    VARCHAR     NOT NULL,  -- 이름
    sexdstn               VARCHAR,               -- 성별
    birth_ym              VARCHAR,               -- 생년월 'YYYY-MM' (응답은 '1961년 06월')
    ofcps                 VARCHAR,               -- 직위 (회장, 부문장, 사외이사 ...)
    rgist_exctv_at        VARCHAR,               -- 등기임원 여부 (사내이사, 사외이사, 기타비상무이사, 미등기)
    fte_at                VARCHAR,               -- 상근 여부
    chrg_job              VARCHAR,               -- 담당 업무 (줄바꿈 포함)
    main_career           VARCHAR,               -- 주요 경력 (줄바꿈 포함)
    mxmm_shrholdr_relate  VARCHAR,               -- 최대주주와의 관계
    hffc_pd_raw           VARCHAR,               -- 재직기간 원문 ('2년 \n1개월')
    hffc_months           INTEGER,               -- 재직기간(월)
    tenure_end_on         DATE,                  -- 임기 만료일
    stlm_dt               DATE,                  -- 보고서 기준일
    valid_from            DATE,                  -- stlm_dt - hffc_months (월 단위 근사) 또는 stlm_dt
    valid_from_method     VARCHAR     NOT NULL,  -- stlm_dt_minus_hffc_pd / stlm_dt
    valid_to              DATE,                  -- 다음 보고서에 없으면 그 stlm_dt, tenure_end_on이 더 이르면 그 날짜 (닫기 단계)
    superseded_by         VARCHAR,
    loaded_at             TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (source_rcept_no, row_seq)
);
