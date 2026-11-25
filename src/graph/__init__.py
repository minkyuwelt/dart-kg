"""Neo4j 쪽 코드 (Phase 3: 조각 벡터·전문검색 색인, Phase 4: 관계 그래프).

- text_normalize.py : 한국어 전문검색 전처리·검색어 만들기 (0004). 색인과 검색이 같은 함수를 쓴다
- client.py         : .env에서 접속 정보를 읽어 드라이버를 연다. CLI `python -m src.graph.client ping`
- (8번 PR) load_chunks.py : DuckDB chunk/chunk_embedding → Neo4j 노드·인덱스
Neo4j는 DuckDB에서 언제든 다시 만들 수 있어야 한다 (CLAUDE.md). 여기 코드는 "읽어서 넣기"만 하고 원본을 만들지 않는다.
"""
