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
   python -m venv .venv
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

> OpenDART API는 하루 약 20,000건까지 호출할 수 있습니다. 대상 회사를 늘릴 때는 호출 수를 먼저 계산하세요.
