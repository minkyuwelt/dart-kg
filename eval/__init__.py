"""Phase 3 평가 세트 (docs/plans/phase3.md 4절).

임베딩 모델·청킹·프롬프트는 모두 이 세트의 점수로 비교한다. 세트가 없으면 "느낌상 좋아졌다"밖에 말할 수 없다.

- eval/questions.jsonl   질문 (사람이 DART 뷰어에서 정답·근거를 확인한 것만 status "ready")
- eval/questions.py      질문 파일 읽기·형식 검사
- eval/metrics.py        hit@k, MRR (접수번호 기준·섹션 기준)
- eval/retrievers/       "질문 → 조각 목록" 검색 함수. keyword(DuckDB 키워드 기본선) → 8번 PR에서 Neo4j 하이브리드 추가
- eval/run.py            실행: python -m eval.run --retrieval-only
- eval/locate.py         근거 섹션 경로 찾기: python -m eval.locate <접수번호> "<정답에 나오는 글자>"
- eval/draft.py          질문 후보 초안 만들기 (접수번호·섹션은 데이터에서, 정답은 사람이)
"""
