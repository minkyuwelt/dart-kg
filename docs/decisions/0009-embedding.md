# 0009. 임베딩 모델과 검색 보조 단계 (재순위·회사 필터)

- 상태: **초안** (2026-11-24). 확정은 노트북 실험 숫자를 보고
- 관련: `docs/plans/phase3.md` (C)·(E)절, `docs/plans/phase3-research.md` 2~3절(모델 조사), `0008-chunking.md`(조각 단위 `section_path` 1,000자)

## 배경

조각(0008)을 "뜻이 비슷한가"로 찾으려면 조각과 질문을 벡터로 바꾸는 임베딩 모델이 필요하다. 한국어 공시 글에 어떤 모델이 맞는지는
리더보드가 아니라 **우리 평가 세트(41개)** 로 정한다(한국어 금융 텍스트에서 순위가 바뀐다는 보고가 있음, research 2절). 노트북 GPU는
RTX 4070 노트북 8GB(2026-11-24 `nvidia-smi` 확인)라 1.1B 모델까지 로컬에서 돈다.

## 결정할 것과 후보

### 1. 임베딩 모델 (로컬 셋. OpenAI 임베딩 API는 1차에서 제외 — 사용자 결정 2026-11-24: 유료, 공시 본문 외부 전송, GPU가 있어 필요 없음)

| 설정 이름 | 모델 | 크기 | 저장 차원 | 기대 | 걱정 |
|---|---|---|---|---|---|
| `kure-v3` | `nlpai-lab/KURE-v3` | 1.1B | 1024 (2048을 잘라서) | MTEB(kor,v2) 검색 1위 단일 벡터 | 시간. 질문용 프롬프트가 있는지 모델 카드 확인(코드는 모델 설정의 `prompts.query`가 있으면 자동으로 붙임) |
| `kure-v1` | `nlpai-lab/KURE-v1` | 568M | 1024 | 작고 검증됨 | v3보다 낮은 점수 |
| `bge-m3` | `BAAI/bge-m3` | 568M | 1024 | 다국어 기준선 | 한국어 점수는 KURE 둘보다 낮음 |

입력 길이는 1,024토큰으로 제한한다(조각이 1,000자 ≈ 600토큰이라 충분. 길이를 줄이면 빨라짐). 벡터는 정규화해 저장(코사인 = 내적).
`--fp16`(16비트 계산)은 속도 변수: 2026-11-24 kure-v1 fp32가 RTX 4070 노트북에서 **1,000개당 50초**(37,821개 ≈ 32분, 30만 조각이면 4시간,
kure-v3는 8시간 어림)라 하루 밤 경계에 걸린다. fp16으로 2배 안팎 빨라지는지, hit@5가 같은지를 함께 잰다.

### 2. 재순위 (선택 단계)

하이브리드/벡터 상위 30개를 교차 인코더 `KURE-Reranker-nano`(149M)로 다시 매겨 상위 5개. 텍스트+표 문서 벤치마크에서 가장 큰 단일 개선(research 3절).
변수: 없음 / nano. 잴 것: hit@5·MRR 변화와 질문당 지연.

### 3. 회사 필터 (선택 단계)

질문에 든 회사명(`target_corp`·`disclosure`의 이름, "(주)"·"주식회사" 무시, 가장 긴 것)으로 `corp_code`를 알아내 그 회사 보고서만 뒤진다.
0008 1차에서 틀린 질문의 대부분이 "주당 현금배당금"처럼 모든 회사 보고서에 같은 말이 있는 것이었다 → 회사만 맞으면 풀리는 문제인지 잰다.
변수: 없음 / corp. 규칙 기반이라 질문에 회사명이 없으면 필터 없이 검색(안전).

## 구현

- `src/rag/embedding.py`: `MODELS`(설정 이름 → HF id·차원), `LocalEmbedder`(sentence-transformers, GPU 자동, 함수 안 import), `embed_chunks`(해시가 바뀐 조각만, 배치마다 커밋, 1,000개당 초 출력), CLI `build`/`stats`
- `src/rag/rerank.py`: `LocalReranker`(CrossEncoder), `rerank()`
- `eval/retrievers/dense.py`: `--retriever dense:<모델>:<전략>`. `eval/retrievers/filters.py`: `guess_corp_code`. `--filter corp`, `--rerank nano`는 chunk·dense 검색기 공통
- `sql/schema.sql`: `chunk_embedding(chunk_id, model_name, text_hash, dim, vector FLOAT[])`. 모델마다 행, 옛 모델 행은 남김
- `requirements-ml.txt`: torch 2.14.1, sentence-transformers 6.2.0 — **노트북 전용**. CI와 웹은 설치하지 않고 테스트는 가짜 임베더로

## 실험 계획 (노트북, 조각은 0008 2차의 24개 보고서 37,821개)

```
pip install -r requirements-ml.txt
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

1단계 — 모델 고르기 (`section_path`):
```
python -m src.rag.embedding build --model kure-v1 --strategy section_path
python -m eval.run --retrieval-only --retriever dense:kure-v1:section_path --label kure-v1
python -m src.rag.embedding build --model bge-m3 --strategy section_path
python -m eval.run --retrieval-only --retriever dense:bge-m3:section_path --label bge-m3
python -m src.rag.embedding build --model kure-v3 --strategy section_path
python -m eval.run --retrieval-only --retriever dense:kure-v3:section_path --label kure-v3
```
각 build 출력의 "1,000개당 N초"를 함께 기록한다 (188개 보고서 30만 조각 → 몇 시간인지 어림).

2단계 — 꼬리표가 임베딩에서도 도움이 되나 (1단계에서 고른 모델로 `section`):
```
python -m src.rag.embedding build --model <모델> --strategy section
python -m eval.run --retrieval-only --retriever dense:<모델>:section --label <모델>-section
```

3단계 — 보조 단계:
```
python -m eval.run --retrieval-only --retriever dense:<모델>:section_path --filter corp --label <모델>-corp
python -m eval.run --retrieval-only --retriever dense:<모델>:section_path --rerank nano --label <모델>-rerank
python -m eval.run --retrieval-only --retriever dense:<모델>:section_path --filter corp --rerank nano --label <모델>-corp-rerank
python -m eval.run --retrieval-only --retriever chunk:section_path --filter corp --label chunk-corp   # 키워드에도 필터만 붙이면?
```

## 검증 결과

(노트북 실행 뒤 기록. 표: 모델 × 전략 × 필터 × 재순위 → hit@5 공시/섹션, MRR, 임베딩 1,000개당 초, 질문당 검색 시간)

## 다시 판단할 조건

- 세 모델의 hit@5 차이가 질문 2개(0.05) 안이면 → 가장 빠른 모델을 쓴다 (C1)
- kure-v3가 VRAM을 넘거나 1,000개당 60초를 넘어 30만 조각이 하루 밤을 넘기면 → kure-v1
- 회사 필터가 큰 폭으로 올리면 → 8번 PR의 Neo4j 검색에 `corp_code` 조건을 기본으로 넣고, 질문에서 회사명을 뽑는 규칙을 다듬는다
- 재순위가 hit@5를 올리지 못하거나 질문당 2초를 넘으면 → 빼고 9번 PR에서 다시 본다
