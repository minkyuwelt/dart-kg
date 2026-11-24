# Phase 3 조사: 청킹·임베딩·검색 관련 논문과 최신 기술

- 작성: 2026-11-24
- 목적: 0007(청킹)·0008(임베딩)·0009(생성)를 정하기 전에, 후보를 빠뜨리지 않았는지와 실험 설계가 알려진 결과와 어긋나지 않는지 확인
- 한계: 웹 작업 환경에서 arxiv.org·huggingface.co에 접근할 수 없어 논문 본문은 못 읽었다. 요약은 검색 결과와 2차 자료 기준이고, 숫자를 결정 근거로 쓰기 전에 **노트북에서 원문을 열어 확인**한다. "확인 필요" 표시가 그 항목이다

## 1. 청킹

| 결과 | 출처 | 우리 계획에 주는 영향 |
|---|---|---|
| **의미 기반 청킹(semantic chunking)은 비용 대비 이득이 불확실하다.** 재현율은 오르지만 끝까지(답변 정확도) 보면 재귀 분할(recursive splitting)보다 낮은 경우가 보고됨. 조각이 평균 43토큰으로 너무 짧아지는 것이 원인으로 지목 | Qu, Tu, Bao, "Is Semantic Chunking Worth the Computational Cost?" NAACL 2025 Findings (확인 필요); Chroma·FloTorch·Vecta 벤치마크(블로그) | 의미 기반 청킹을 후보에서 뺀다. 1차 후보는 구조 기반(섹션)과 고정 길이 둘 |
| **고정 길이 약 512토큰 재귀 분할이 기본선(baseline)으로 널리 권장됨** | 2026 블로그 가이드 다수 | 실험에 "섹션 무시, 512토큰 고정 분할"을 기본선으로 넣어 섹션 청킹이 정말 나은지 비교한다 |
| **금융 보고서는 문서 요소(제목·문단·표) 단위로 자르는 것이 고정 길이보다 낫다.** 표를 한 조각으로 유지하는 "표 인식 청킹"만으로 Naive RAG가 약 2.7% 개선 | Jimeno Yepes 외, "Financial Report Chunking for Effective RAG" 2024 (arXiv 2402.05131, 확인 필요); MimirRAG 2026 (arXiv 2605.25030); "Empirical Evaluation of PDF Parsing and Chunking for Financial QA" 2026 (arXiv 2604.12047, TableQuest 벤치마크) | 우리 1차 후보(섹션 단위 + 표는 쪼개지 않음)와 같은 방향. DART XML은 PDF가 아니라 `TITLE`·`P`·`TABLE` 태그가 이미 있어 요소 단위 분할이 더 쉽다 |
| **조각 앞에 제목 경로(breadcrumb)를 붙이는 것**은 LLM 호출 없이 되는 저비용 방법. 다만 독립 벤치마크는 못 찾음 | DEV Community 글, "StrucChunk" 2026 (법률 문서) | 후보 유지. 실험 변수로 넣어 효과를 직접 잰다 |
| **LLM으로 조각마다 문맥 설명을 생성해 붙이는 방식(Contextual Retrieval)**은 원 발표(Anthropic 블로그)에서 상위 20 검색 실패율 35% 감소를 보고했지만, 2026년 위약 대조 연구는 "독립 평가에서는 미미한 이득"이라고 보고 | Anthropic 블로그 2024; Merola & Singh, "Does Document Structure Help Dense Retrieval? A Placebo-Controlled Ablation" 2026 (arXiv 2610.10170, 확인 필요) | 1차에서 하지 않는다. 조각 30만 개 × LLM 호출은 비용도 크다. 제목 경로 붙이기로 충분한지 먼저 본다 |
| **Late chunking**(문서 전체를 임베딩한 뒤 토큰 벡터를 조각 단위로 평균)은 일부 코퍼스에서 nDCG@10 +6.5점, 짧은 문서에서는 이득 없음. 긴 입력을 받는 임베딩 모델 필요 | Jina AI 2024 (확인 필요) | 2차 후보. KURE-v3·bge-m3는 8,192토큰을 받으므로 가능하지만, 구현이 복잡해 1차 결과를 본 뒤 판단 |

**결론:** 실험 설계는 "① 512토큰 고정 분할(기본선) ② 섹션 단위, 표는 쪼개지 않음 ③ ②에 제목 경로 붙임" 세 가지로 좁힌다. 계획 초안의 "문단 단위"는 짧은 조각 문제(위 첫 줄)와 같은 방향이라 뺀다.

## 2. 임베딩 모델

출처: nlpai-lab/KURE GitHub README (2026-11-24 열람). MTEB(kor, v2) Retrieval 9개 과제 평균. 자체 보고 수치라 독립 검증은 아니다.

| 모델 | 종류 | 크기 | 차원 | nDCG@10 | Recall@10 | 비고 |
|---|---|---|---|---|---|---|
| KURE-v2 (2026-08) | **다중 벡터**(late interaction, 토큰마다 128차원) | 154M | 토큰 × 128 | 0.8160 | 0.8921 | 가장 높지만 조각 하나가 벡터 수백 개 → **Neo4j 벡터 인덱스(조각 하나 = 벡터 하나)에 못 올림.** 후보 제외 |
| **KURE-v3 (2026-10)** | 단일 벡터 | 1.1B | 2048 (Matryoshka로 1024·512·256 가능, 1024에서 성능 99% 유지) | 0.8027 | 0.8857 | 8,192토큰. nvidia Nemotron-3-Embed-1B 기반. MIT. **새 1순위 후보** |
| Qwen3-Embedding-8B | 단일 벡터 | 7.6B | 4096 | 0.7826 | 0.8815 | 노트북 GPU에 무리 |
| KURE-v1 (2024-12) | 단일 벡터 | 568M | 1024 | 0.7616 | 0.8629 | bge-m3 기반. 계획 초안의 1순위였음 → 2순위로 |
| BAAI/bge-m3 | 단일 벡터 | 568M | 1024 | 0.7509 | 0.8588 | 다국어 기준선 |
| KoE5 | 단일 벡터 | 560M | 1024 | 0.7337 | 0.8300 | multilingual-e5-large 기반. 입력 512토큰. 후보 제외 |
| KURE-Sparse-base (2026-10) | 학습된 희소 벡터 | 593M | 어휘 크기 | 0.7984 | — | BM25 자리를 대신하는 방식. Neo4j 전문검색(cjk)과 역할이 겹치고 저장 방식이 다름 → Phase 3에서는 제외, 전문검색 정밀도가 안 나오면 재검토 |
| OpenAI text-embedding-3-small/large | API | — | 1536/3072 | 이 표에 없음 | — | 같은 평가 세트에서 직접 비교해야 함 |

**금융 도메인 주의:** TWICE 논문(Hwang 외, ICLR 2025)은 한국어 금융 텍스트 벤치마크 KorFinMTEB(7과제 26데이터셋)를 공개했고, 일반 벤치마크 점수가 높은 모델도 금융 원문에서는 점수가 떨어진다고 보고했다. **일반 리더보드 순위를 그대로 믿지 말고 우리 평가 세트로 재야 하는 이유**다. KorFinMTEB 데이터는 공개되어 있어, 우리 평가 세트(30개)가 작을 때 보조 지표로 쓸 수 있다 (확인 필요: 데이터셋 접근 방법).

**결론:** 0008 후보를 **KURE-v3(1024차원으로 잘라 사용) / KURE-v1 / bge-m3 / OpenAI API** 넷으로 바꾼다. KURE-v3는 1.1B라 VRAM이 4GB 이상이어야 편하다(확인 필요: 노트북 VRAM). 1024차원으로 자르면 Neo4j 인덱스 크기는 v1과 같다.

## 3. 검색: 하이브리드와 재순위

| 결과 | 출처 | 영향 |
|---|---|---|
| BM25 + 밀집 벡터를 RRF로 합치면 **모든 지표·모든 데이터 부분집합에서 둘 각각보다 낫다** (텍스트+표 문서 벤치마크) | "From BM25 to Corrective RAG: Benchmarking Retrieval Strategies for Text-and-Table Documents" 2026 (arXiv 2604.01733) | 계획의 RRF 하이브리드 유지 |
| 하이브리드 위에 **교차 인코더 재순위(reranker)**를 얹는 것이 단일 개선으로 가장 크다. 같은 벤치마크에서 nDCG@10 0.551 → 0.683 | 같은 논문 (Cohere reranker 사용) | **(E)에 재순위 단계를 선택 항목으로 추가.** 상위 20~50개를 재순위해 5개를 고른다 |
| 한국어 재순위 모델: KURE-Reranker-nano(149M, 2026-10), KURE-Reranker-base(1.7B), bge-reranker-v2-m3(다국어) | KURE README | nano는 노트북에서 질문당 수십 ms 수준으로 돌 것으로 보임(확인 필요). 실험 변수로 "재순위 없음 / nano"를 넣는다 |

## 4. 계획에 반영한 변경

1. 청킹 실험 후보를 셋으로 고정: 512토큰 고정(기본선) / 섹션+표 보존 / 섹션+표 보존+제목 경로. "문단 단위"와 "의미 기반"은 제외
2. 임베딩 1순위를 KURE-v1 → **KURE-v3(1024차원)**으로. KoE5·multilingual-e5-large 제외, 후보는 넷
3. (E)에 **재순위 단계(KURE-Reranker-nano)**를 선택 항목으로 추가. 실험에서 효과를 잰다
4. LLM으로 조각 문맥을 생성하는 방식은 1차에서 하지 않는다 (근거: 독립 평가에서 이득 미미, 비용 큼)
5. 노트북에서 확인할 것 추가: VRAM(KURE-v3 1.1B), arXiv 원문 4편 확인, KorFinMTEB 접근

## 출처

- KURE GitHub: https://github.com/nlpai-lab/KURE
- Qu, Tu, Bao. Is Semantic Chunking Worth the Computational Cost? NAACL 2025 Findings
- Jimeno Yepes 외. Financial Report Chunking for Effective RAG. https://arxiv.org/abs/2402.05131
- Empirical Evaluation of PDF Parsing and Chunking for Financial QA with RAG. https://arxiv.org/abs/2604.12047
- MimirRAG. https://arxiv.org/abs/2605.25030
- Merola & Singh. Does Document Structure Help Dense Retrieval? A Placebo-Controlled Ablation. https://arxiv.org/abs/2610.10170
- Hwang 외. TWICE (KorFinMTEB). ICLR 2025. https://arxiv.org/abs/2502.07131
- From BM25 to Corrective RAG: Benchmarking Retrieval Strategies for Text-and-Table Documents. https://arxiv.org/abs/2604.01733
- Anthropic, Introducing Contextual Retrieval (2024 블로그)
- 청킹 벤치마크 블로그(Chroma, FloTorch, Vecta 인용): https://www.premai.io/blog/rag-chunking-strategies-the-2026-benchmark-guide/
