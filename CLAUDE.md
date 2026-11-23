# dart-kg

DART 공시를 자동 수집해 기업·주주·임원 관계를 지식그래프로 만들고, 자연어 질문에 근거 공시와 함께 답하는 개인 프로젝트.

## 범위
- 대상: 4대 금융지주(KB금융, 신한지주, 하나금융지주, 우리금융지주)와 자회사
- 데이터 출처: OpenDART API (https://opendart.fss.or.kr)
- API 호출 한도(일 약 20,000건)를 넘지 않도록, 대상 회사를 늘릴 때는 호출 수를 먼저 계산할 것

## 단계
1. **Phase 1 수집·적재**: 공시 목록·기업개황 → Airflow DAG → PostgreSQL
2. **Phase 2 관계 데이터**: 최대주주·임원·타법인 출자 현황 테이블
3. **Phase 3 공시 RAG**: 공시 원문 임베딩(pgvector) → Q&A
4. **Phase 4 지식그래프**: PostgreSQL → Neo4j 적재 → GraphRAG

현재 단계: Phase 1

## 아키텍처 원칙
- PostgreSQL이 원본(Source of Truth). Neo4j는 PostgreSQL에서 언제든 재생성할 수 있어야 함
- 모든 적재는 멱등성 유지: 재실행해도 중복이 생기지 않게 (예: `rcept_no` 기본키 + `ON CONFLICT`)
- corp_code(8자리 고유번호)를 회사 식별자로 사용. 회사명으로 조인하지 않기

## 저장소 구조
```
dags/              Airflow DAG
src/collectors/    DART API 호출
src/loaders/       PostgreSQL 적재
src/graph/         (Phase 4) Neo4j 적재
src/rag/           (Phase 3) 검색·생성
sql/schema.sql     테이블 정의
notebooks/         탐색·실험용
docs/devlog/       날짜별 개발 일지 (YYYY-MM-DD.md)
```

## 개발 환경
- 실행 환경: 학원 Windows 노트북 + Docker Desktop(WSL2). Airflow·PostgreSQL·Neo4j는 docker-compose로 실행
- 프로젝트 폴더는 WSL 파일시스템 안에 둠
- 줄바꿈은 LF로 통일 (`.gitattributes`)
- 노트북은 과정 종료 후 반납 → 로컬에만 남는 것이 없도록 할 것

## 작업 방식
- 웹 Claude Code: 코드·문서 작성, 브랜치와 PR 생성
- VS Code Claude Code: 노트북에서 실제 실행, 테스트, 에러 수정
- 작업 단위마다 브랜치를 새로 만들고 PR로 main에 병합
- 작업을 시작하기 전에 `git pull`로 최신 상태 맞추기

## 규칙
- API 키는 `.env`의 `DART_API_KEY`로만 읽음 (`python-dotenv`). `.env`는 절대 커밋하지 않음. 새 환경변수를 추가하면 `.env.example`도 함께 갱신
- 코드 설명은 한국어로, 코드 주석보다 자세하게
- 의미 있는 작업이 끝나면 `docs/devlog/`에 오늘 한 일, 막힌 점, 해결 방법을 짧게 기록
