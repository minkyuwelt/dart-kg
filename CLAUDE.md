# dart-kg

DART 공시를 자동 수집해 기업·주주·임원 관계를 지식그래프로 만들고, 자연어 질문에 근거 공시와 함께 답하는 개인 프로젝트.

## 범위
- 대상: 4대 금융지주(KB금융, 신한지주, 하나금융지주, 우리금융지주)와 직속 자회사 (2025 기준 59곳, `docs/decisions/0002-target-scope.md`)
  - 대상 목록은 코드에 적지 않는다. 지주의 최신 사업보고서 타법인 출자현황에서 `target_corp` 테이블로 뽑는다
  - 해외 자회사·펀드·손자회사는 수집 대상이 아니고, 모회사 출자 데이터에 그래프 노드로만 들어간다
  - 출자 대상 이름 → corp_code 연결에서 후보가 여럿이면 추측하지 말고, 근거를 확인해 `config/corp_name_overrides.csv`에 적는다
  - 사람(임원)은 `person_id` = 이름|생년월|성별로 묶는다 (`docs/decisions/0006-person-identity.md`). 생년월이 없는 표(지주 겸직 현황, 0014)는 같은 지주 임원 표의 같은 이름이 1명일 때만 잇는다. 같은 조합이 다른 사람으로 확인되면 추측하지 말고 0006에 분리 규칙을 적는다. 원문 오타로 한 사람이 둘로 갈라진 경우는 `config/person_merges.csv`에 쌍과 근거를 적는다 (코드가 추측해 합치지 않는다)
- 데이터 출처: OpenDART API (https://opendart.fss.or.kr)
- API 호출 한도(일 약 20,000건)를 넘지 않도록, 대상 회사를 늘릴 때는 호출 수를 먼저 계산할 것

## 단계
1. **Phase 1 수집·적재**: 공시 목록·기업개황 → Dagster 자산 → DuckDB
2. **Phase 2 관계 데이터**: 최대주주·임원·타법인 출자 현황 테이블 (DuckDB)
3. **Phase 3 공시 RAG**: 공시 원문 청크·임베딩 → Neo4j 벡터 인덱스 → Q&A
4. **Phase 4 지식그래프**: DuckDB → Neo4j 관계 그래프 적재 → GraphRAG (벡터로 찾은 공시 청크에서 회사·임원·지분 경로로 탐색)

기술 선정 근거: `docs/decisions/0001-tech-stack.md`, 한국어 전문검색 방식: `docs/decisions/0004-korean-fulltext.md`

현재 단계: **Phase 3·4 완료** (2026-11-26, 개선 2026-11-27). Phase 2는 2026-11-23(백필 618개 보고서, 시점 질의 5개 검증). Phase 3·4는 평가까지 끝남 — 검색 hit@5 공시 1.000·섹션 0.729(48개), LLM 답 맞음 49/부분 2/틀림 0(53개), 라우팅 규칙만 53/53(일반 4개는 T2 허용) → `docs/decisions/0010`·`0011`, `docs/devlog/2026-11-27.md`. 검색이 놓친 질문은 `PYTHONPATH=. python notebooks/rank_diag.py <id>`로 정답 조각의 벡터·전문검색·하이브리드 순위를 먼저 본다
다음: 평가에서 나온 개선 후보(0010·0011 "개선 후보" — 이웃 조각은 11-27 완료), 매일 운영 확인(`src.pipeline.status`), 화면(Vite + React `web/`, 0013 — 질문·변화 리포트·회사 페이지 11-27, 질문 페이지의 회사 이름 → 회사 페이지 링크까지. 다음은 질문 이력). 계획 문서: `docs/plans/phase3.md`(완료 표시), 0011 "다시 판단할 조건"
방향: `docs/plans/usecases.md` — 누가 쓰나, 업무 질문 11개, 비슷한 서비스와 차별점 5개. 기능을 넣을지는 이 문서의 질문을 돕는지로 판단한다
Phase 4 설계 전 조사: `docs/plans/phase4-research.md` — 논문·도구마다 채택/보류/제외와 이유. 0011은 이 문서 5절의 뼈대로 쓴다

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
config/            수동 매핑 등 사람이 관리하는 설정 (corp_name_overrides.csv, person_merges.csv, corp_lineage.csv)
src/graph/         Neo4j 적재(조각·관계)와 그래프 질의(라우터·템플릿·picker, 0011)
src/rag/           검색·임베딩·LLM 답변 (0009·0010). 진입점 `python -m src.rag.ask "질문"`
src/reports/       변화 리포트 (0012)
src/api/           FastAPI — 기존 함수를 HTTP로 감싼다 (0013)
web/               화면 — Vite + React SPA (0013). API 응답을 그대로 보여 주고 새 로직을 두지 않는다. `cd web && npm run dev`, CI가 lint·build
sql/schema.sql     DuckDB 테이블 정의
tests/             오프라인 테스트 (DART API를 호출하지 않음, `python -m unittest discover -s tests -t .`)
eval/              평가 세트: questions.jsonl(ready 53개), run.py(검색 hit@5·LLM 채점표), routing.py(라우팅 정확도), 검색 함수(retrievers/), 질문 초안 도구. 정답은 사람이 뷰어에서 확인한 것만
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
  - 노트북을 켜고 작업을 시작할 때 `DAGSTER_HOME=$PWD/dagster_home python -m src.pipeline.status`로 스케줄·작업·자산의 마지막 실행과 DuckDB ↔ Neo4j 수를 한 장으로 본다(읽기만). 판정에 문제가 나오면 그 줄의 안내대로. 조각 수만 보려면 `python -m src.graph.load_chunks stats`. 전문검색 인덱스 속성이 코드와 다르다고 나오면 `python -m src.graph.load_chunks reindex-fulltext`(노드·벡터 그대로, 몇 분). Neo4j가 0이면 Docker Desktop 재시작 때 `data/neo4j/` 폴더가 컨테이너에 안 붙어 빈 DB가 새로 만들어진 것 → `docker compose down && docker compose up -d`로 다시 붙인다 (2026-11-26, 데이터는 그대로 남아 있음. `-v`는 붙이지 않는다)
- 노트북에서 다른 수업 프로젝트 컨테이너가 5432·8080·7474·7687 포트를 쓰고 있음 → Docker로 띄우는 서비스의 노트북 쪽 포트는 `.env`로 바꿀 수 있게 할 것
- 프로젝트 폴더는 WSL 파일시스템 안에 둠
- Node(화면 `web/`)는 WSL 안의 nvm 것(22)을 쓴다. WSL PATH에 Windows의 `/mnt/c/Program Files/nodejs/npm`이 섞여 있어, Linux Node가 없으면 `npm run dev`가 CMD.EXE로 떠서 파일 감시가 `C:\Windows`를 보는 오류가 난다 (2026-11-27). `which node`가 `~/.nvm/…`인지 확인
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
- 웹은 PR을 한 번에 하나만 연다. 기다릴 수 없으면 앞 PR 브랜치 위에 쌓아서 열고 PR 설명에 병합 순서를 적는다. 하나가 병합되면 웹이 남은 PR에 main을 먼저 합쳐 두어, 사용자는 충돌을 보지 않는다
- devlog(`docs/devlog/`)는 여러 PR이 같은 파일 끝에 줄을 붙이면 반드시 충돌한다 → 그날 마지막 PR에 한 번만 적는다. 중간 PR은 PR 설명으로 충분하다
- PR마다 GitHub Actions(`tests`)가 오프라인 테스트와 `dagster definitions validate`를 돌린다. 체크가 실패한 PR은 병합하지 않는다. CI는 DART API를 호출하지 않으므로, 실제 API 검증은 여전히 노트북에서 한다

## 규칙
- API 키는 `.env`의 `DART_API_KEY`로만 읽음 (`python-dotenv`). `.env`는 절대 커밋하지 않음. 새 환경변수를 추가하면 `.env.example`도 함께 갱신
- 코드 설명은 한국어로, 코드 주석보다 자세하게
- DART API 호출은 반드시 `src/collectors/dart_client.py`를 거친다 (에러 메시지에서 API 키를 가리고, DART status 코드를 처리함). `requests`로 DART를 직접 호출하지 않기
- 의미 있는 작업이 끝나면 `docs/devlog/`에 오늘 한 일, 막힌 점, 해결 방법을 짧게 기록
- 기술(DB, 도구, 주요 라이브러리)을 추가하거나 바꿀 때는 먼저 `docs/decisions/`에 요구사항 근거와 대안 비교를 기록. "많이 쓰여서", "써 봐서/안 써 봐서"는 근거가 아님
