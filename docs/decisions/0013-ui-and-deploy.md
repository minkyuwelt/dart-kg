# 0013. 화면과 배포: FastAPI + Next.js, Vercel

날짜: 2026-11-25 · 상태: 채택 (사용자 결정, 대안 비교 없이) · 관련: `docs/plans/usecases.md` 4절, 0012

## 결정

- **백엔드 API는 FastAPI** (`src/api/main.py`). 기존 Python 함수(`src/rag/ask`, `src/reports/changes`, `src/loaders/as_of`)를 HTTP로 감싸기만 한다. 새 로직을 API 층에 두지 않는다 — Dagster 자산이 함수를 호출만 하는 것과 같은 원칙.
- **프론트엔드는 Next.js**, **Vercel에 배포**. Phase 4 뒤에 시작하고, 저장소를 같은 곳에 둘지 따로 둘지는 그때 정한다.
- Streamlit은 쓰지 않는다. 처음 계획(phase3.md 7절 11번, usecases.md)은 Streamlit 한 페이지였으나 사용자가 "실제 도구 수준의 화면, Vercel 배포"로 방향을 바꿨다(2026-11-25).

## 이유 (사용자 결정이라 비교표는 쓰지 않는다)

- 사용자가 Vercel 배포를 전제로 "전문적인 화면"을 원했다. Vercel은 프론트엔드와 가벼운 서버 함수를 돌리는 곳이라 Next.js가 기본 선택이고, Streamlit은 올릴 수 없다.
- DuckDB 파일·Neo4j·kure-v3 임베딩은 Vercel에 올릴 수 없으므로 어떤 화면을 고르든 API 서버가 따로 필요하다. 그 서버는 Python 함수를 그대로 감쌀 수 있는 FastAPI가 가장 짧다. 자동 생성되는 OpenAPI 문서(`/docs`)가 포트폴리오에서 "직접 호출해 보세요"로 쓰인다.

## API 설계에서 지킨 것

- **DuckDB는 요청마다 읽기 전용으로 열고 바로 닫는다.** 서버가 연결을 쥐고 있으면 Dagster의 매일 쓰기가 막힌다(CLAUDE.md 잠금 규칙). 잠금에 걸리면 503으로 "잠시 뒤 다시".
- **임베딩 모델(2GB)·Neo4j 드라이버는 서버 시작 때 한 번** 열어 둔다(lifespan). 요청마다 열면 질문 하나에 몇십 초가 든다.
- LLM 클라이언트는 요청마다 만든다. 키가 없으면 503과 함께 이유를 돌려준다. `use_llm=false`면 검색만 하므로 키 없이도 동작한다.
- 임원 응답에 생년월·`person_id`를 넣지 않는다(0006). 모든 근거 행에 DART 뷰어 링크를 붙인다(D3).
- 응답 설명에 "수치는 공시 그대로, 투자 판단용이 아니다"를 적는다(usecases 질문 11 원칙).

## 배포할 때 확인할 것 (노트북에서 먼저 잰다)

| 항목 | 왜 | 어떻게 |
|---|---|---|
| 질문 임베딩 시간, GPU 없는 서버 | kure-v3는 1.1B 모델. CPU면 메모리 4~5GB, 질문 1건에 1~2초로 어림 | `API_EMBED_DEVICE=cpu uvicorn …` 뒤 `/ask` 5번의 응답 시간 |
| 메모리 | 모델 + Neo4j 드라이버 + FastAPI | 노트북에서 `uvicorn` 프로세스 RSS |
| Neo4j·DuckDB를 어디에 두나 | 배포 서버에 Docker(Neo4j)와 DuckDB 파일을 함께 올려야 한다. 재생성은 코드로 가능(CLAUDE.md 원칙) | 서버 후보(작은 VM 1대)는 Phase 4 뒤 결정 |
| CORS | Vercel 도메인에서 API를 부른다 | 프론트 시작 때 `CORSMiddleware`에 그 도메인만 허용 |

측정값은 노트북 확인 뒤 이 문서에 적는다.
