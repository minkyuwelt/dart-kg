# dart-kg

DART 공시를 자동으로 모아 **기업·주주·임원 관계를 지식그래프**로 만들고, 자연어 질문에 **근거 공시와 함께** 답하는 개인 프로젝트입니다.

- 대상: 4대 금융지주(KB금융, 신한지주, 하나금융지주, 우리금융지주)와 자회사
- 데이터 출처: [OpenDART API](https://opendart.fss.or.kr)

## 해결할 문제

금융지주 하나가 자회사 10여 곳을 거느리고, 자회사가 다시 손자회사를 두고, 임원 한 사람이 지주와 자회사에 동시에 적을 두는 구조는 **공시 한 건을 읽어서는 보이지 않습니다.** "2023년 3월 31일 기준 신한은행의 최대주주는 누구였고, 근거 공시는 무엇인가" 같은 질문에 답하려면 여러 회사의 여러 보고서를 시점에 맞춰 이어 붙여야 합니다. DART 공시를 그 형태로 바꾸는 데에는 네 가지 걸림돌이 있습니다.

| # | 걸림돌 | 실제로 부딪힌 모습 | 이 프로젝트의 답 |
|---|---|---|---|
| 1 | **수집 범위와 한도** | 대상 59곳의 공시 목록 5년치(71개월 × 59곳 = 4,189 파티션)와 정기보고서 618개 × API 4종을 받아야 하고, OpenDART는 하루 약 20,000건까지만 허용. 수집은 수업 중 노트북에서 돌리므로 매일 켜져 있지 않음 | 대상 목록은 코드에 적지 않고 지주의 출자현황에서 뽑는다([0002](docs/decisions/0002-target-scope.md)). 날짜·회사 파티션으로 잘라 빠진 날만 채우고, 호출 수를 먼저 계산한다. 원본 응답을 그대로 보관해 스키마가 바뀌어도 다시 부르지 않는다 |
| 2 | **시점** | 최대주주·임원 데이터는 보고서마다 "그 시점의 스냅샷"으로만 온다. 정정 공시가 나오면 API는 최신 정정본만 돌려준다. 임원 재직기간은 '2022.01.07~', "'23.3.22~현재", '2년 1개월' 등 형식이 제각각 | 모든 관계 행에 사실 시간(`valid_from`·`valid_to`)과 기록 시간(`source_rcept_no`·`superseded_by`)을 적는다. 끝난 관계는 지우지 않고 닫는다. 시간 값은 공시가 주는 날짜에서 규칙으로 얻고 LLM으로 추측하지 않는다([0005](docs/decisions/0005-temporal-model.md)) |
| 3 | **식별** | 출자 대상·최대주주는 corp_code 없이 이름만 온다('㈜우리은행 ', '신한금융지주회사', 과거 동명 법인). 임원도 이름뿐이라 동명이인이 있고, 같은 사람의 생년월이 보고서마다 다르게 적힌 경우(17쌍)도 있다 | 회사는 정규화 후 corp_code로 연결하고, 후보가 여럿이면 추측하지 않고 근거를 적어 `config/corp_name_overrides.csv`에 둔다. 사람은 이름\|생년월\|성별로 묶고, 원문 오타로 갈라진 쌍은 사람이 확인해 `config/person_merges.csv`에 적는다([0006](docs/decisions/0006-person-identity.md)) |
| 4 | **근거 있는 답변** | 자연어로 물으면 LLM이 그럴듯하게 답하지만, 어느 공시의 어느 줄에서 나온 값인지 없으면 금융 데이터로는 쓸 수 없다 | 모든 행이 접수번호(`rcept_no`)를 들고 다닌다. Phase 3~4에서는 벡터로 찾은 공시 청크에서 회사·임원·지분 경로로 이어 가며, 답에 근거 공시 링크를 붙인다 |

한 줄로 줄이면, **"DART 공시를 시점과 근거를 잃지 않는 관계 데이터로 바꾸고, 그 위에서 질문에 답하게 한다"**가 이 프로젝트가 푸는 문제입니다.

## 처리 흐름

```
 OpenDART API
     │  (1) 수집: src/collectors  — 모든 호출은 dart_client.py 하나를 거침
     ▼
 raw_api_response (원본 JSON 그대로)          ─┐
     │  (2) 정규화: src/loaders                  │ DuckDB 파일 하나
     ▼                                           │ data/dartkg.duckdb
 corp · target_corp · disclosure                 │ (Source of Truth)
 investment · major_shareholder                  │
 major_shareholder_change · executive · person  ─┘
     │  (3) 파생 (Phase 3~4): src/graph, src/rag
     ▼
 Neo4j — 관계 그래프 + 공시 청크 벡터 + 전문검색
     │  (4) 질의
     ▼
 자연어 질문 → 근거 공시(rcept_no)와 함께 답변
```

| 단계 | 하는 일 | 다시 실행하면 |
|---|---|---|
| (1) 수집 | 공시 목록(월 × 회사 파티션), 기업개황, 정기보고서 주요정보 4종을 받아 원본 그대로 저장 | 완료한 파티션은 건너뜀. 한도 초과 응답이 오면 멈춤 |
| (2) 정규화 | 원본 JSON에서 테이블을 만들고, 이름 → corp_code 연결, 사람 식별, 시간 컬럼 계산 | **API 호출 없이** 원본에서 전부 재생성. 규칙을 고치면 이 단계만 다시 돌림 |
| 품질 검사 | 대상 목록 완전성, 회사 식별자 무결성, 접수번호 형식, 출처 보고서 존재, `valid_from` 범위, 구간 방향, 현재 상태 유일성 | Dagster asset check로 자산에 붙어 실행마다 기록 |
| (3) 파생 | DuckDB → Neo4j 노드·관계, 공시 원문 청크·임베딩 | DuckDB에서 언제든 다시 만들 수 있음 |
| (4) 질의 | 벡터 검색으로 공시 청크를 찾고, 그래프를 타고 회사·임원·지분 경로로 확장 | — |

단계를 자르는 기준은 **"다시 만들 수 있는가"**입니다. 원본(1)은 API 한도 때문에 한 번 받으면 지키고, 그 아래(2)(3)는 코드만으로 다시 만들 수 있어야 합니다. 그래서 노트북을 반납해 DuckDB 파일을 잃어도 하루(약 6,600건)면 복구됩니다.

매일 운영은 Dagster 스케줄이 맡습니다. 06:30 대상 목록 확인 → 07:00 이번 달 공시 목록 → 07:30 그날 접수된 정기보고서 → 08:30 관계 테이블 재생성. 노트북이 꺼져 있던 날은 다음에 켜진 날 따라잡습니다.

## 내 역할

혼자 하는 프로젝트이고, 코드 작성에는 AI 코딩 도구(Claude Code)를 썼습니다. 그래서 "내가 한 일"을 코드 줄 수가 아니라 **결정과 검증**으로 적습니다.

- **문제를 요구사항으로 바꾸기.** "금융지주 지식그래프"라는 주제를 기능(F1~F5)·비기능(N1~N4)·제약(C1~C2) 요구사항 ID로 쪼갰습니다. 이후 모든 기술 결정은 이 ID를 인용해야 통과합니다. 인용할 수 없으면 "많이 쓰이니까"라도 고르지 않았습니다 (Airflow·PostgreSQL을 빼고, Kafka를 넣지 않은 이유가 그것입니다)
- **기술 결정과 기록.** 결정마다 후보 2~3개를 같은 기준으로 비교해 `docs/decisions/`에 남겼습니다. 그리고 결정이 틀렸을 때 고친 기록도 지우지 않고 "정정"으로 덧붙였습니다 (예: DuckDB 읽기 전용 연결이 쓰기를 막는다는 사실을 실행 중에 발견해 0001을 정정)
- **데이터에서 판단하기.** 코드가 추측하면 안 되는 지점은 사람이 근거를 보고 정했습니다. 동명 법인 5곳은 최근 공시 유무로, 생년월이 갈라진 임원 17쌍은 보고서 원문을 하나씩 대조해 확인한 뒤 CSV에 근거와 함께 적었습니다
- **실행·검증.** 웹에서 만든 코드는 노트북에서 실제 API로 돌려 보기 전까지 "미검증"으로 두고, 백필 4,189 파티션(29분)과 정기보고서 618개를 직접 돌리며 생긴 오류(DuckDB 잠금 충돌, 재직기간 파싱 실패 85%, 품질 검사 실패 13건)를 원인까지 추적해 고쳤습니다. 끝나면 시점 질의 5개를 근거 공시와 대조해 맞는지 확인했습니다
- **작업 방식 설계.** 웹(코드 작성·PR)과 노트북(실행·검증)이 GitHub만을 통해 주고받도록 규칙을 세우고, PR마다 CI가 오프라인 테스트를 돌리게 했습니다. `CLAUDE.md`가 그 규칙을 담고 있습니다

## 기술 선택 이유

기술은 요구사항 ID를 인용해 고릅니다. 아래는 요약이고, 후보 비교표와 "다른 후보가 나았을 조건"은 [0001](docs/decisions/0001-tech-stack.md)에 있습니다.

**요구사항 중 기술을 가른 것**

| ID | 내용 | 기술에 준 영향 |
|---|---|---|
| F3 | 원문 → 청크 → 임베딩 → 검색. 단계가 길고 임베딩은 비쌈 | 실패한 단계만 다시 실행할 수 있어야 함 |
| F5 | 자연어 질문에 근거 공시와 함께 답변 (핵심) | 텍스트 근거와 관계 그래프를 **함께** 탐색 |
| N1 | 규모가 작음 (회사 59곳, 정기보고서 약 600건/5년) | 대용량 전용 도구가 필요 없음 |
| N4 | API 한도 하루 약 20,000건 | 원본 응답을 보관해 재호출 없이 재정규화 |
| C1 | 학원 노트북. 항상 켜져 있지 않고, 다른 수업 컨테이너가 5432·8080·7474·7687 포트 사용 중 | 서버를 띄울수록 운영 부담과 포트 충돌이 늘어남 |
| C2 | 과정이 끝나면 노트북 반납 | 로컬 데이터는 코드만으로 다시 만들 수 있어야 함 |

**결정 1. 벡터와 그래프를 Neo4j 한곳에 둔다 (F5).** 초기안은 PostgreSQL(pgvector)에서 벡터 검색을 하고 corp_code로 Neo4j를 따로 조회해 앱에서 합치는 구조였습니다. F5는 "벡터로 찾은 공시 청크 → 거기 언급된 회사·임원 → 지분 경로"로 이어 가는 검색이라, 둘이 한 저장소에 있으면 Cypher 질의 하나로 끝납니다. Neo4j가 이 패턴을 공식 라이브러리(neo4j-graphrag)로 지원하고 Community 에디션에서도 벡터 인덱스를 쓸 수 있습니다. 한국어 전문검색은 형태소 분석기가 없어 실험으로 확인했습니다. `cjk` 분석기 + 경계 전처리로 검색어 19개에서 재현율 97%, 정밀도 93% ([0004](docs/decisions/0004-korean-fulltext.md)).

**결정 2. 원본 저장소는 DuckDB (N1·N4·C1).** 벡터를 Neo4j로 보내면 PostgreSQL에만 있는 기능을 쓸 일이 없습니다. 기능이 같다면 서버가 없는 파일 DB가 낫습니다(C1). SQLite와 DuckDB 중에서는 원본 JSON을 SQL로 다시 정규화하는 작업(`read_json`, 중첩 타입)과 시점별 지분 추이 같은 분석 질의에 강한 DuckDB를 골랐습니다. 차이는 작습니다. 단점은 파일을 프로세스 단위로 잠가 **읽기 전용 연결이 열려 있어도 쓰기가 막힌다**는 것인데, 이 프로젝트의 쓰기는 하루 한 번 몇 초뿐이라 "쓰는 run은 하나씩, 잠기면 재시도"로 감당합니다. 실제로 백필 중 품질 검사가 같은 프로세스에서 읽기 전용 연결을 열려다 충돌해, 검사 함수가 연결을 넘겨받도록 고쳤습니다.

**결정 3. 파이프라인은 Dagster (F3·C1·C2).** 이 프로젝트의 데이터는 원본 → 정규화 테이블 → 그래프·임베딩으로 **파생**됩니다. "정규화 규칙을 고쳤으니 영향받는 그래프·임베딩만 다시 만든다"를 표현하는 데에는 작업 순서(Airflow의 task)보다 자산 의존 관계(Dagster의 asset)가 직접적입니다. 날짜 × 회사 파티션으로 노트북이 꺼져 있던 날만 채울 수 있고(C1), 품질 검사를 asset check로 자산에 붙일 수 있습니다. Airflow는 노트북에서 실측 RAM 1.2GB에 메타DB 서버·JWT·컨테이너가 따라와 C1에 불리했고, 실제로 docker-compose까지 만들어 검증한 뒤 닫았습니다(PR #5). cron은 Phase 1만 보면 충분하지만 Phase 3~4의 파생 재생성을 직접 구현해야 합니다. Dagster Labs가 Prefect에 인수된 리스크에는 "수집·적재 로직은 Dagster에 의존하지 않는 일반 함수로 만들고 Dagster는 호출만 한다"로 대비했습니다. 모든 작업은 CLI로도 실행됩니다.

**결정 4. CI는 GitHub Actions ([0003](docs/decisions/0003-ci.md)).** 웹과 노트북이 GitHub만으로 주고받는 작업 방식이라, PR마다 오프라인 테스트 85개와 `dagster definitions validate`를 돌려 "노트북에서 돌려 보기 전에 깨진 코드"를 거릅니다. API 키가 없으니 실제 DART 호출은 여전히 노트북에서 합니다.

**고르지 않은 것과 그 이유**

| 후보 | 고르지 않은 이유 | 다시 판단할 조건 |
|---|---|---|
| PostgreSQL + pgvector | 벡터를 Neo4j에 두면 남는 일이 작은 정형 테이블 보관뿐. 서버 하나가 늘어남(C1) | 벡터를 원본 저장소에 둘 때, 쓰는 프로세스가 여럿일 때 |
| Airflow | 작업 중심이라 파생 데이터 재생성을 따로 관리. RAM 1.2GB + 컨테이너(C1) | — |
| cron + 스크립트 | Phase 3~4의 "영향받는 파생만 재생성"을 직접 구현해야 함 | Phase 3~4를 하지 않을 때 |
| Kafka | 이벤트가 흘러오지 않음(DART는 하루 한 번 폴링). 생산자·소비자가 각 하나. 브로커 서버(C1). 인용할 요구사항이 없음 | 대상이 상장사 전체로 늘고 분 단위 알림이 필요할 때 |
| dbt | 변환 모델이 10개 안팎, 테스트는 asset check로 충분 | 변환 모델 20개 초과 |
| Kùzu (임베디드 그래프) | 2025년 10월 개발 중단 | — |
| DuckDB vss (DuckDB 안 벡터 검색) | 인덱스 영속화가 실험 기능 | 안정화되면 |

| 영역 | 기술 |
|---|---|
| 원본 저장소 | DuckDB 1.5 |
| 파이프라인 | Dagster 1.13 |
| 그래프·벡터·전문검색 | Neo4j Community (Phase 3~, Docker) |
| 언어·라이브러리 | Python 3.12, requests, python-dotenv, pandas, duckdb |
| CI | GitHub Actions |

## 로드맵

| 단계 | 내용 | 상태 |
|---|---|---|
| Phase 1 수집·적재 | 공시 목록·기업개황 수집 → Dagster 자산 → DuckDB | 완료 (일일 스케줄 운영 중) |
| Phase 2 관계 데이터 | 최대주주·임원·타법인 출자 현황 테이블 (이중 시간축, 사람 식별) | 완료 (2021~2026년 보고서 618개, 임원 15,546행, 사람 1,693명) |
| Phase 3 공시 RAG | 공시 원문 청크·임베딩 → Neo4j 벡터 인덱스 → Q&A | 진행 중 (계획: [docs/plans/phase3.md](docs/plans/phase3.md)) |
| Phase 4 지식그래프 | DuckDB → Neo4j 관계 그래프 적재 → GraphRAG | 예정 |

DuckDB 파일이 원본 데이터(Source of Truth)이고, Neo4j는 DuckDB에서 언제든 다시 만들 수 있게 설계합니다. 모든 적재는 다시 실행해도 중복이 생기지 않도록 멱등성을 유지합니다.

## 폴더 구조

```
src/collectors/    DART API 호출
src/loaders/       DuckDB 적재
src/pipeline/      Dagster 자산·스케줄 정의
src/graph/         (Phase 3~4) Neo4j 적재
src/rag/           (Phase 3) 검색·생성
sql/               DuckDB 테이블 정의 (schema.sql)
notebooks/         탐색·실험용
docs/decisions/    기술 결정 기록
docs/devlog/       날짜별 개발 일지
```

## 로컬 실행 방법

1. **환경변수 설정** — `.env.example`을 복사해 `.env`를 만들고 OpenDART에서 발급받은 키를 넣습니다. `.env`는 `.gitignore`에 들어 있어 커밋되지 않습니다.

   ```bash
   cp .env.example .env
   ```

   ```
   DART_API_KEY=발급받은_키
   ```

2. **패키지 설치** — 가상환경을 만든 뒤 의존성을 설치합니다.

   ```bash
   python3 -m venv .venv
   ```

   ```bash
   source .venv/bin/activate
   ```

   ```bash
   pip install -r requirements.txt
   ```

3. **스크립트 실행** — 저장소 루트에서 실행하면 4대 금융지주의 corp_code가 출력됩니다. (API 1건 호출)

   ```bash
   python -m src.collectors.corp_code
   ```

4. **오프라인 테스트** — DART API를 호출하지 않습니다.

   ```bash
   python -m unittest discover -s tests -t . -v
   ```

## 파이프라인 실행하기 (Dagster + DuckDB)

데이터는 `data/dartkg.duckdb` 파일 하나에 쌓입니다. Docker는 필요 없습니다.

```
corp_codes ─▶ holding_investments ─▶ target_corps ─▶ disclosures
                                          └────────▶ report_details ─▶ relations   (Phase 2)
```

| 자산 | 내용 | API 호출 |
|---|---|---|
| `corp_codes` | 공시 대상 전체 회사 목록 → `corp` 테이블 | 1건 |
| `holding_investments` | 지주 4곳의 최신 사업보고서 타법인 출자현황 → `investment` 테이블 (출자 대상 이름을 corp_code로 연결) | 4건 |
| `target_corps` | 수집 대상(지주 + 직속 자회사, 2025 기준 59곳) → `target_corp` 테이블, `disclosures`의 회사 파티션 추가 | 0건 |
| `disclosures` | 한 회사의 한 달치 공시 목록 → `raw_api_response`(원본) + `disclosure` 테이블. 파티션 = 월 × 회사 | 파티션당 보통 1건 |
| `report_details` | (Phase 2) 한 회사의 한 정기보고서에서 주요정보 4종(타법인 출자·최대주주·최대주주 변동·임원) 원본 저장. 파티션 = 회사 × 보고서(`2025-11011` = 사업연도-보고서종류). 공시 목록에 그 보고서가 없으면 API 없이 건너뜀 | 보고서당 4건 |
| `relations` | (Phase 2) 원본 전체에서 `investment`·`major_shareholder`·`major_shareholder_change`·`executive` 테이블 재생성. 시간 컬럼은 [0005](docs/decisions/0005-temporal-model.md) | 0건 |

대상 범위를 정한 근거는 [docs/decisions/0002-target-scope.md](docs/decisions/0002-target-scope.md)에 있습니다.

1. **화면 띄우기** — 실행 후 http://localhost:3000 에 접속합니다.

   ```bash
   DAGSTER_HOME=$PWD/dagster_home dagster dev -m src.pipeline.definitions
   ```

2. **처음 한 번** — 화면에서 `targets_job`(corp_codes → holding_investments → target_corps)을 먼저 실행합니다.

3. **과거 공시 백필** — 2021-01부터 이번 달까지(71개월 × 59곳 = 4,189개 파티션, 최소 4,189건)를 채웁니다. Dagster 화면의 백필은 파티션마다 프로세스를 띄워 몇 시간이 걸리므로, 프로세스 하나에서 도는 스크립트를 씁니다. `dagster dev`는 꺼 둔 상태로 실행합니다.

   ```bash
   DAGSTER_HOME=$PWD/dagster_home python -m src.pipeline.backfill --dry-run   # 계획만 (API 호출 없음)
   DAGSTER_HOME=$PWD/dagster_home python -m src.pipeline.backfill
   ```

   2026-11-23 실제 실행: 4,189개 파티션, API 호출 4,661건(페이지 넘김 포함), 공시 86,893건, 약 29.0분. 완료한 파티션은 Dagster에 기록되므로 중간에 멈춰도 다시 실행하면 이어서 합니다. `--from`, `--to`, `--corp`로 범위를 좁힐 수 있고, `--max-calls`(기본 8,000) 또는 DART 한도 초과 응답에서 멈춥니다.

4. **매일 수집** — 화면의 Automation에서 `targets_freshness`와 `daily_disclosures` 스케줄을 켭니다. `daily_disclosures`는 매일 07:00에 이번 달 파티션을 다시 수집합니다(하루 59건, 매달 1~7일은 지난달까지 118건). `targets_freshness`는 매일 06:30에 수집 대상이 최신 사업보고서 기준인지 확인하고, 기준 연도가 바뀌었을 때만(보통 4월 1일, API 5건) `targets_job`을 실행합니다. 노트북이 꺼져 있던 날이 있어도 다음에 켜진 날 따라잡습니다. 스케줄은 `dagster dev`가 떠 있을 때만 동작합니다.

5. **(Phase 2) 관계 데이터 백필** — 공시 목록 백필이 끝난 뒤 실행합니다. 공시 목록에 있는 정기보고서만 부르므로(사업보고서 제출 30곳 기준 2021년~올해 약 600개 보고서 × 4건 = 약 2,400건) 하루 안에 끝납니다. 끝나면 관계 테이블을 다시 만들고 품질 검사까지 기록합니다. `dagster dev`는 꺼 둔 상태로 실행합니다.

   ```bash
   DAGSTER_HOME=$PWD/dagster_home python -m src.pipeline.backfill_reports --dry-run   # 계획만 (API 호출 없음)
   DAGSTER_HOME=$PWD/dagster_home python -m src.pipeline.backfill_reports
   DAGSTER_HOME=$PWD/dagster_home python -m src.pipeline.backfill_reports --rebuild-only   # 수집 없이 재생성·검사만
   ```

   이후에는 Automation에서 `daily_report_details`(07:30, 그날 새로 접수된 정기보고서만 수집)와 `daily_relations`(08:30, 관계 테이블 재생성)를 켭니다.

6. **(Phase 2) 관계 데이터 조회** — 관계 테이블(`investment`, `major_shareholder`, `major_shareholder_change`, `executive`)의 행은 "보고서 기준일(`valid_from`)부터 다음 보고서 기준일(`valid_to`)까지 참"입니다. 정정된 행은 `superseded_by`에 정정본 접수번호가 있습니다([0005](docs/decisions/0005-temporal-model.md)). 사람은 `person_id`(이름|생년월|성별, [0006](docs/decisions/0006-person-identity.md))로 묶입니다.

   ```python
   from src.loaders.duckdb_store import connect
   with connect(read_only=True) as con:
       # 지금 KB금융 임원 (현재 상태 뷰: *_current)
       now = con.sql("SELECT nm, ofcps, source_rcept_no FROM executive_current WHERE corp_code = '00688996'").df()
       # 2023년 3월 31일 기준 신한은행 최대주주 (시점 조회)
       then = con.sql("""
           SELECT nm, trmend_posesn_stock_qota_rt, source_rcept_no FROM major_shareholder
           WHERE corp_code = '00149293' AND valid_from <= DATE '2023-03-31'
             AND (valid_to IS NULL OR valid_to > DATE '2023-03-31') AND superseded_by IS NULL
       """).df()
       # 두 회사 이상에서 임원인 사람 (겸직 후보)
       multi = con.sql("SELECT person_id, corp_count FROM person WHERE corp_count >= 2").df()
   ```

7. **(Phase 2) 시점 질의 검증** — 백필이 끝난 뒤, 관계 테이블이 질문에 맞는 답을 주는지 확인합니다. 데이터 상태(행 수, 회사별 보고서 수, 정정본, 생년월 없는 사람 비율)와 질문 5개(시점 기준 최대주주·사외이사, 지주·자회사 겸직, 최대주주 변동 이력, 정정 전후 차이)의 결과를 근거 공시 링크와 함께 출력합니다. DuckDB를 읽기 전용으로만 열고 API는 호출하지 않습니다.

   ```bash
   python notebooks/phase2_questions.py --out notebooks/phase2_questions_result.txt
   ```

8. **(Phase 3) 공시 원문 수집** — 정기보고서 원문 ZIP을 `data/documents/{rcept_no}.zip`에 보관하고 `document` 테이블에 메타데이터를 적습니다. 보고서 1건 = API 1건. 먼저 1건만 받아 ZIP 안 파일과 인코딩을 확인한 뒤 백필합니다. 정정본은 접수번호가 달라 각각 받습니다.

   ```bash
   python -m src.loaders.collect document 20240318000xxx          # 1건 (API 1건)
   python -m src.loaders.documents inspect 20240318000xxx         # ZIP 안 파일 목록·인코딩·XML 앞부분 (API 없음)
   python -m src.pipeline.backfill_documents --dry-run            # 계획만
   python -m src.pipeline.backfill_documents --kind 11011         # 사업보고서만 (약 150건)
   python -m src.loaders.documents status                         # 회사별 보관 현황
   ```

9. **(Phase 3) 원문 파싱** — 저장된 ZIP의 XML(본문 + 첨부)을 섹션(목차 항목) 단위로 풀어 `document_section`에, 파일별 성공·실패를 `document_parse`에 적습니다. 표는 문장으로 펴지 않고 2차원 배열(JSON)로 둡니다. API 호출 없음. 규칙은 `src/loaders/document_parser.py`.

   ```bash
   python -m src.loaders.documents tree 20260313001191     # 한 원문의 섹션 트리를 눈으로 확인 (DB에 쓰지 않음)
   python -m src.loaders.documents parse                   # 저장된 ZIP 전체 재생성
   python -m src.loaders.documents why-lxml                # 복구 파서(lxml)로 간 파일들의 원인을 종류별로 출력
   ```

10. **(Phase 3) 평가 세트** — 임베딩·청킹·프롬프트를 비교할 질문 30개. 접수번호와 근거 섹션은 데이터에서 초안을 만들고, 정답은 사람이 DART 뷰어에서 확인해 적습니다 (`eval/README.md`). 키워드 기본선이 들어 있어 Neo4j 없이도 hit@5·MRR이 나옵니다.

   ```bash
   python -m eval.draft --seed                  # 질문 후보 초안 → eval/questions.draft.jsonl (정답은 비움)
   python -m eval.review                        # 후보를 한 개씩 원문과 함께 보고 정답 입력 → questions.jsonl
   python -m eval.locate 20260313001191 "국민연금공단"   # 정답 글자가 든 섹션 경로 찾기
   python -m eval.run --retrieval-only --label baseline  # 키워드 기본선 hit@5·MRR → eval/results/
   ```

화면 없이 터미널에서 실행할 수도 있습니다.

```bash
DAGSTER_HOME=$PWD/dagster_home dagster asset materialize -m src.pipeline.definitions --select disclosures --partition "00688996|2026-11-01"
```

Dagster 없이 같은 작업을 하려면:

```bash
python -m src.loaders.collect disclosures 00688996 20261101 20261130
python -m src.loaders.collect investments 2025 00688996 00382199
python -m src.loaders.investments rebuild   # 원본 응답에서 investment 테이블 재생성 (API 호출 없음)
python -m src.loaders.collect report_details 00688996 2025 11011   # (Phase 2) 한 보고서의 주요정보 4종 원본 저장 (API 4건)
python -m src.loaders.relations rebuild      # (Phase 2) 원본에서 최대주주·변동·임원 테이블 재생성 (API 호출 없음)
python -m src.loaders.collect document 20240318000xxx   # (Phase 3) 한 공시의 원문 ZIP 저장 (API 1건)
python -m src.loaders.documents parse        # (Phase 3) 저장된 원문 ZIP에서 섹션 테이블 재생성 (API 호출 없음)
```

> DuckDB는 파일을 프로세스 단위로 잠급니다. 노트북에서 읽기 전용으로 열어 둔 연결도 파이프라인의 쓰기를 막습니다. 탐색할 때는 아래처럼 열고 블록이 끝나면 바로 닫으세요. 파이프라인이 잠금에 걸리면 5분 간격으로 최대 3번 다시 시도합니다.
>
> ```python
> from src.loaders.duckdb_store import connect
> with connect(read_only=True) as con:
>     df = con.sql("SELECT * FROM disclosure ORDER BY rcept_dt DESC LIMIT 20").df()
> ```

> OpenDART API는 하루 약 20,000건까지 호출할 수 있습니다. 대상 회사를 늘릴 때는 호출 수를 먼저 계산하세요.
