# dart-kg

DART 공시를 자동으로 모아 **기업·주주·임원 관계를 지식그래프**로 만들고, 자연어 질문에 **근거 공시와 함께** 답하는 개인 프로젝트입니다.

- 대상: 4대 금융지주(KB금융, 신한지주, 하나금융지주, 우리금융지주)와 자회사
- 데이터 출처: [OpenDART API](https://opendart.fss.or.kr)

## 로드맵

| 단계 | 내용 | 상태 |
|---|---|---|
| Phase 1 수집·적재 | 공시 목록·기업개황 수집 → Dagster 자산 → DuckDB | 진행 중 |
| Phase 2 관계 데이터 | 최대주주·임원·타법인 출자 현황 테이블 | 예정 |
| Phase 3 공시 RAG | 공시 원문 청크·임베딩 → Neo4j 벡터 인덱스 → Q&A | 예정 |
| Phase 4 지식그래프 | DuckDB → Neo4j 관계 그래프 적재 → GraphRAG | 예정 |

DuckDB 파일이 원본 데이터(Source of Truth)이고, Neo4j는 DuckDB에서 언제든 다시 만들 수 있게 설계합니다. 모든 적재는 다시 실행해도 중복이 생기지 않도록 멱등성을 유지합니다.

## 기술 스택

| 영역 | 기술 | 이유 |
|---|---|---|
| 원본 저장소 | DuckDB | 정형 테이블과 API 원본 응답을 파일 하나에 보관하고, 원본 JSON을 SQL로 다시 정규화 |
| 파이프라인 | Dagster | 원본 → 정규화 테이블 → 그래프·임베딩으로 파생되는 구조를 자산 의존 관계로 관리 |
| 그래프·벡터·전문검색 | Neo4j | 벡터로 찾은 공시 청크에서 회사·임원·지분 경로까지 질의 하나로 탐색 |
| 언어 | Python (requests, python-dotenv, pandas) | |

후보 비교와 자세한 근거는 [docs/decisions/0001-tech-stack.md](docs/decisions/0001-tech-stack.md)에 있습니다.

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
```

| 자산 | 내용 | API 호출 |
|---|---|---|
| `corp_codes` | 공시 대상 전체 회사 목록 → `corp` 테이블 | 1건 |
| `holding_investments` | 지주 4곳의 최신 사업보고서 타법인 출자현황 → `investment` 테이블 (출자 대상 이름을 corp_code로 연결) | 4건 |
| `target_corps` | 수집 대상(지주 + 직속 자회사, 2025 기준 59곳) → `target_corp` 테이블, `disclosures`의 회사 파티션 추가 | 0건 |
| `disclosures` | 한 회사의 한 달치 공시 목록 → `raw_api_response`(원본) + `disclosure` 테이블. 파티션 = 월 × 회사 | 파티션당 보통 1건 |

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

   완료한 파티션은 Dagster에 기록되므로 중간에 멈춰도 다시 실행하면 이어서 합니다. `--from`, `--to`, `--corp`로 범위를 좁힐 수 있고, `--max-calls`(기본 8,000) 또는 DART 한도 초과 응답에서 멈춥니다.

4. **매일 수집** — 화면의 Automation에서 `daily_disclosures` 스케줄을 켭니다. 매일 07:00에 이번 달 파티션을 다시 수집합니다(하루 59건, 매달 1~7일은 지난달까지 118건). `yearly_targets`는 매년 4월 15일에 새 사업보고서로 대상을 갱신합니다. 스케줄은 `dagster dev`가 떠 있을 때만 동작합니다.

화면 없이 터미널에서 실행할 수도 있습니다.

```bash
DAGSTER_HOME=$PWD/dagster_home dagster asset materialize -m src.pipeline.definitions --select disclosures --partition "00688996|2026-11-01"
```

Dagster 없이 같은 작업을 하려면:

```bash
python -m src.loaders.collect disclosures 00688996 20261101 20261130
python -m src.loaders.collect investments 2025 00688996 00382199
python -m src.loaders.investments rebuild   # 원본 응답에서 investment 테이블 재생성 (API 호출 없음)
```

> DuckDB는 파일을 프로세스 단위로 잠급니다. 노트북에서 읽기 전용으로 열어 둔 연결도 파이프라인의 쓰기를 막습니다. 탐색할 때는 아래처럼 열고 블록이 끝나면 바로 닫으세요. 파이프라인이 잠금에 걸리면 5분 간격으로 최대 3번 다시 시도합니다.
>
> ```python
> from src.loaders.duckdb_store import connect
> with connect(read_only=True) as con:
>     df = con.sql("SELECT * FROM disclosure ORDER BY rcept_dt DESC LIMIT 20").df()
> ```

> OpenDART API는 하루 약 20,000건까지 호출할 수 있습니다. 대상 회사를 늘릴 때는 호출 수를 먼저 계산하세요.
