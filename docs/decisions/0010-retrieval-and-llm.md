# 0010. 검색 합치기(하이브리드)와 LLM 답변 생성

- 상태: **초안** (2026-11-25). 검색 절은 9a번 PR, 생성 절은 9b번 PR에서 노트북 숫자를 보고 확정
- 관련: `docs/plans/phase3.md` (E)절, `0009-embedding.md`(모델 kure-v3·회사 필터 켬·재순위 nano 뺌), `0004-korean-fulltext.md`(cjk 구절 검색), `docs/plans/usecases.md` D3(근거 링크)·D5(추측 없음)

## 1. 검색 합치기 (9a)

### 배경
0009 3단계에서 임베딩과 키워드가 **다른 질문을 잘 맞췄다**: 임베딩(kure-v3)은 정형표·시점에서, 키워드+회사 필터는 서술 질문 순위에서(MRR 섹션 0.905 vs 0.679) 나았다.
둘을 합치면 각자의 강점이 더해질 수 있다. Neo4j에는 벡터 인덱스와 전문검색 인덱스가 같은 `Chunk` 노드에 있어(8번 PR) 합치기가 쉽다.

### 후보
| 방법 | 설명 | 장점 | 단점 |
|---|---|---|---|
| **RRF** (Reciprocal Rank Fusion) | 조각마다 Σ 1/(60+순위)를 두 목록에서 더해 정렬 | 점수 크기가 다른 두 검색(코사인 0~1, Lucene 0~수십)을 보정 없이 합침. 한 줄 공식, 매개변수 없음 | 두 목록 모두에 있는 조각이 과하게 유리할 수 있음 |
| 점수 가중합 | 각 점수를 0~1로 정규화해 α·벡터 + (1-α)·전문검색 | 가중치로 조정 가능 | α를 평가 세트로 맞춰야 하고(과적합), 정규화 방식에 민감 |
| Neo4j `HybridRetriever`(neo4j-graphrag) | 패키지가 두 인덱스를 질의해 합침 | 코드 적음 | 전문검색어 전처리(0004)를 끼워 넣기 어려움 → 계획 (E)절대로 처음엔 Cypher 직접 |

**선택: RRF.** 매개변수가 없어 36~60개 평가 세트로도 과적합 걱정이 없다. 구현: `eval/retrievers/neo4j.py` `Neo4jHybridRetriever`(벡터 20 + 전문검색 20 → RRF → k), `--retriever neo4j:hybrid:kure-v3`.
회사 필터는 양쪽 검색에 같이 건다(0009 결정). 재순위는 하이브리드 위에만 붙여 한 번 더 잰다(`--rerank nano`): 후보가 벡터·글자 섞인 40개가 되면 0009 3단계(벡터만 30개)와 결과가 다를 수 있다.

### 잴 것 (노트북, 188개 보고서 26만 조각, 평가 세트 늘린 뒤)
```
python -m eval.run --retrieval-only --retriever neo4j:vector:kure-v3 --filter corp --label v
python -m eval.run --retrieval-only --retriever neo4j:fulltext --filter corp --label f
python -m eval.run --retrieval-only --retriever neo4j:hybrid:kure-v3 --filter corp --label h
python -m eval.run --retrieval-only --retriever neo4j:hybrid:kure-v3 --filter corp --rerank nano --label h-rerank
```
기대: 하이브리드 섹션 hit@5 ≥ 벡터(24개 보고서에서 0.889), 서술 MRR 상승. 질문당 시간(`eval.run`이 출력)도 적는다.

### 다시 판단할 조건
- 하이브리드가 벡터만보다 낮으면 → 전문검색 상위 수(fetch)를 10으로 줄이거나, 전문검색을 "후보 넓히기"로만 쓰고 순위는 벡터로
- 두 목록이 거의 겹치지 않아(RRF 동점 다수) 순위가 불안하면 → 점수 가중합을 한 번 비교

### 검증 결과
(기록 예정)

## 2. LLM 답변 생성 (9b)

(계획 (E)절 "생성" 그대로. 모델 `gpt-6-luna`, `.env` `OPENAI_API_KEY`·`LLM_MODEL`, 구조화 출력(답 본문 + 인용 번호), 조각에 없는 내용은 "공시에서 찾지 못했습니다",
답마다 DART 뷰어 링크(usecases D3). 비용 상한: 질문당 토큰 상한·하루 호출 한도. 9b번 PR에서 채운다)
