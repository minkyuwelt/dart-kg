# dart-kg

DART 공시를 자동 수집해 기업·주주·임원 관계를 지식그래프로 만들고, 자연어 질문에 근거 공시와 함께 답하는 개인 프로젝트.

## 범위
- 대상: 4대 금융지주(KB금융, 신한지주, 하나금융지주, 우리금융지주)와 직속 자회사 (2025 기준 59곳, `docs/decisions/0002-target-scope.md`)
  - 대상 목록은 코드에 적지 않는다. 지주의 최신 사업보고서 타법인 출자현황에서 `target_corp` 테이블로 뽑는다
  - 해외 자회사·펀드·손자회사는 수집 대상이 아니고, 모회사 출자 데이터에 그래프 노드로만 들어간다
  - 출자 대상 이름 → corp_code 연결에서 후보가 여럿이면 추측하지 말고, 근거를 확인해 `config/corp_name_overrides.csv`에 적는다
  - 사람(임원)은 `person_id` = 이름|생년월|성별로 묶는다 (`docs/decisions/0006-person-identity.md`). 같은 조합이 다른 사람으로 확인되면 추측하지 말고 0006에 분리 규칙을 적는다. 원문 오타로 한 사람이 둘로 갈라진 경우는 `config/person_merges.csv`에 쌍과 근거를 적는다 (코드가 추측해 합치지 않는다)
- 데이터 출처: OpenDART API (https://opendart.fss.or.kr)
- API 호출 한도(일 약 20,000건)를 넘지 않도록, 대상 회사를 늘릴 때는 호출 수를 먼저 계산할 것

## 단계
1. **Phase 1 수집·적재**: 공시 목록·기업개황 → Dagster 자산 → DuckDB
2. **Phase 2 관계 데이터**: 최대주주·임원·타법인 출자 현황 테이블 (DuckDB)
3. **Phase 3 공시 RAG**: 공시 원문 청크·임베딩 → Neo4j 벡터 인덱스 → Q&A
4. **Phase 4 지식그래프**: DuckDB → Neo4j 관계 그래프 적재 → GraphRAG (벡터로 찾은 공시 청크에서 회사·임원·지분 경로로 탐색)

기술 선정 근거: `docs/decisions/0001-tech-stack.md`, 한국어 전문검색 방식: `docs/decisions/0004-korean-fulltext.md`

현재 단계: Phase 2 완료 (2026-11-23. 백필 618개 보고서, 품질 검사 통과, 시점 질의 5개 검증 → `docs/devlog/2026-11-23.md`). 다음: Phase 3 (공시 원문 RAG). 계획: `docs/plans/phase3.md`

## 아키텍처 원칙
- DuckDB 파일(`data/dartkg.duckdb`)이 원본(Source of Truth). DART API 원본 응답도 함께 보관해서, 스키마를 바꿔도 API를 다시 호출하지 않고 다시 정규화할 수 있게 함
- Neo4j(그래프·벡터·전문검색)는 DuckDB에서 언제든 재생성할 수 있어야 함
- 관계 데이터(Phase 2 이후: 최대주주·임원·출자)는 이중 시간축으로 적는다: 사실 시간(`valid_from`·`valid_to`)과 기록 시간(`source_rcept_no`·`superseded_by`). 끝난 관계·정정된 공시는 지우지 않고 닫는다. 시간 값은 공시가 주는 날짜에서 규칙으로 얻고 LLM으로 추측하지 않는다 (`docs/decisions/0005-temporal-model.md`)
- 모든 적재는 멱등성 유지: 재실행해도 중복이 생기지 않게 (예: `rcept_no` 기본키 + `ON CONFLICT`)
- corp_code(8자리 고유번호)를 회사 식별자로 사용. 회사명으로 조인하지 않기
- 수집·적재 로직은 Dagster에 의존하지 않는 일반 함수로 만들고, Dagster 자산은 그 함수를 호출만 한다 (CLI로도 실행 가능하게)
- DuckDB는 파일을 프로세스 단위로 잠근다: "쓰는 프로세스 하나" 또는 "읽기 전용 프로세스 여러 개"만 동시에 열 수 있고, 읽기 전용 연결이 열려 있으면 쓰기도 막힌다. Dagster에서 DuckDB에 쓰는 run은 하나씩만 돌리고(잠금에 걸리면 다시 시도), 노트북·탐색 코드는 `with connect(read_only=True) as con:`으로 열고 바로 닫는다
- Dagster 스케줄·센서는 DuckDB를 열지 않는다 (데몬 프로세스에서 평가되어 run의 쓰기와 잠금이 겹치고, 재시도도 없다). 스케줄에 필요한 값은 자산 실행 메타데이터나 동적 파티션 등 Dagster 인스턴스에서 읽는다

## 저장소 구조
```
src/collectors/    DART API 호출
src/loaders/       DuckDB 적재
src/pipeline/      Dagster 자산·스케줄 정의
config/            수동 매핑 등 사람이 관리하는 설정 (corp_name_overrides.csv)
src/graph/         (Phase 3~4) Neo4j 적재
src/rag/           (Phase 3) 검색·생성
sql/schema.sql     DuckDB 테이블 정의
tests/             오프라인 테스트 (DART API를 호출하지 않음, `python -m unittest discover -s tests -t .`)
dagster_home/      Dagster 인스턴스 설정 (dagster.yaml만 커밋)
notebooks/         탐색·실험용
docs/decisions/    기술 결정 기록 (NNNN-제목.md)
docs/plans/        단계별 계획 (phase3.md)
.github/workflows/ CI (PR마다 오프라인 테스트)
docs/devlog/       날짜별 개발 일지 (YYYY-MM-DD.md)
```

## 개발 환경
- 실행 환경: 학원 Windows 노트북 + Docker Desktop(WSL2)
- Dagster와 DuckDB는 `.venv`에서 바로 실행 (`dagster dev`). DuckDB 파일은 `data/` 아래에 두며 커밋하지 않음
- Docker는 Neo4j에만 사용 (Phase 3부터, docker-compose)
- 노트북에서 다른 수업 프로젝트 컨테이너가 5432·8080·7474·7687 포트를 쓰고 있음 → Docker로 띄우는 서비스의 노트북 쪽 포트는 `.env`로 바꿀 수 있게 할 것
- 프로젝트 폴더는 WSL 파일시스템 안에 둠
- 줄바꿈은 LF로 통일 (`.gitattributes`)
- 노트북은 과정 종료 후 반납 → 코드·문서는 모두 GitHub에 push. `data/`의 DuckDB 파일은 로컬에만 있지만 코드로 다시 만들 수 있어야 함 (전체 재수집 약 5,000건 추정, 하루 한도 안)

## 작업 방식
- 웹 Claude Code: 코드·문서 작성, 브랜치와 PR 생성
- VS Code Claude Code: 노트북에서 실제 실행, 테스트, 에러 수정
- 작업 단위마다 브랜치를 새로 만들고 PR로 main에 병합
- 작업을 시작하기 전에 `git pull`로 최신 상태 맞추기

### 웹 ↔ VS Code 협업 규칙
GitHub가 유일한 중계 지점이다. 웹과 노트북은 서로의 파일을 직접 볼 수 없고, push된 것만 공유된다.
- 한 브랜치는 한 번에 한쪽에서만 작업한다. 웹이 연 PR을 VS Code가 이어받으면, 그동안 웹은 그 브랜치를 건드리지 않는다
- 이어받는 쪽은 작업 전에 반드시 `git fetch` 후 해당 브랜치를 받고, 수정은 같은 브랜치에 push해 PR에 반영한다
- 기존 웹 세션을 이어서 쓸 때는 노트북에서 push한 내용이 있을 수 있으므로 먼저 `git pull`한다
- 새 작업은 항상 최신 main에서 브랜치를 만든다. 병합된 브랜치는 다시 쓰지 않는다
- 브랜치 이름: `feat/`, `fix/`, `chore/`, `docs/` 접두어 + 작업 내용 (예: `feat/disclosure-list`)
- 실제 DART API 호출과 Docker 실행은 VS Code(노트북)에서만 한다. 웹 환경에는 `.env`가 없다
- 노트북에서 고친 내용은 그날 push한다. 커밋하지 않은 변경을 남겨둔 채 작업을 끝내지 않는다
- 웹에서 만든 코드는 노트북에서 실행 확인 전까지 "미검증"이다. PR 설명에 검증 여부를 적는다
- PR마다 GitHub Actions(`tests`)가 오프라인 테스트와 `dagster definitions validate`를 돌린다. 체크가 실패한 PR은 병합하지 않는다. CI는 DART API를 호출하지 않으므로, 실제 API 검증은 여전히 노트북에서 한다

## 규칙
- API 키는 `.env`의 `DART_API_KEY`로만 읽음 (`python-dotenv`). `.env`는 절대 커밋하지 않음. 새 환경변수를 추가하면 `.env.example`도 함께 갱신
- 코드 설명은 한국어로, 코드 주석보다 자세하게
- DART API 호출은 반드시 `src/collectors/dart_client.py`를 거친다 (에러 메시지에서 API 키를 가리고, DART status 코드를 처리함). `requests`로 DART를 직접 호출하지 않기
- 의미 있는 작업이 끝나면 `docs/devlog/`에 오늘 한 일, 막힌 점, 해결 방법을 짧게 기록
- 기술(DB, 도구, 주요 라이브러리)을 추가하거나 바꿀 때는 먼저 `docs/decisions/`에 요구사항 근거와 대안 비교를 기록. "많이 쓰여서", "써 봐서/안 써 봐서"는 근거가 아님
