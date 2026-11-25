# 0009. 임베딩 모델과 검색 보조 단계 (재순위·회사 필터)

- 상태: **채택** (2026-11-25). 임베딩 `kure-v3`(fp16, 배치 8, query:/passage: 접두어, 1,024차원) × 조각 `section_path` × 회사 필터 켬 × 재순위 없음. 근거는 아래 검증 결과 1~3단계
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
fp32일 때 GPU 전력이 상한 90W의 절반(40W)에 머문 것은 fp32 계산이 텐서 코어를 쓰지 않기 때문이라, fp16은 2배보다 더 빨라질 수 있다.

배치 크기(`--batch`)는 8GB VRAM에 맞춘다: fp32 배치 32는 긴 조각 구간에서 메모리를 넘겨 7배 느려졌다(아래 검증 결과 1단계). 기본값 8.

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
python -m src.rag.embedding build --model kure-v1 --strategy section_path                  # fp32 기준선 (끝남)
python -m eval.run --retrieval-only --retriever dense:kure-v1:section_path --label kure-v1
python -m src.rag.embedding build --model bge-m3 --strategy section_path --fp16 --batch 16  # fp16 속도 확인
python -m eval.run --retrieval-only --retriever dense:bge-m3:section_path --label bge-m3
python -m src.rag.embedding build --model kure-v3 --strategy section_path --fp16 --batch 8  # 1.1B라 fp16이어도 배치 8
python -m eval.run --retrieval-only --retriever dense:kure-v3:section_path --label kure-v3
```
각 build 출력의 "1,000개당 N초"를 함께 기록한다 (188개 보고서 30만 조각 → 몇 시간인지 어림). 최근 구간 속도가 누적의 몇 배로
튀면 메모리 넘침이니 `nvidia-smi --query-gpu=memory.used,memory.total --format=csv`를 보고 배치를 줄인다.

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

조각: 0008 2차의 24개 보고서 37,821개(`section_path`). 질문: ready 41개 중 근거가 있는 36개(`없음` 5개는 검색 점수에서 빠짐).
비교 기준(같은 조각, 키워드 검색 `chunk:section_path`): hit@5 공시 0.750 / 섹션 0.444.

### 1단계 — 모델 (2026-11-24)

| 모델 | 정밀도·배치 | 1,000개당 초 | hit@5 공시 | hit@5 섹션 | MRR 공시 | MRR 섹션 | 비고 |
|---|---|---|---|---|---|---|---|
| kure-v1 | fp32 · 32→8 | 50~54 (정상 구간) | **0.972** | 0.556 | 0.972 | 0.391 | 틀린 1개는 p2-q1(2023년 1분기 보고서 미수집 — 검색 문제 아님) |
| bge-m3 | fp16 · 16 | **16.2** (전체 10분) | **0.972** | 0.556 | 0.926 | 0.406 | kure-v1과 동률. 서술 섹션 0.857(> kure-v1 0.714), 정형표 섹션 0.474(< 0.526). fp16이 fp32의 3배 이상 빠름 |
| kure-v3 (1차, 조각 접두어 누락) | fp16 · 8 | 56.1 (전체 35분) | 0.833 | 0.222 | 0.502 | 0.136 | **실행 조건 오류** — 아래. 모델 성능으로 읽지 않는다 |
| **kure-v3 (2차, 접두어 수정)** | fp16 · 8 | 54.7 (전체 34분) | **0.972** | **0.861** | 0.958 | **0.562** | 서술 7개 섹션 1.000, 시점 0.800, 정형표 0.842. 틀린 1개는 p2-q1(미수집) |

유형별(kure-v1): 서술 7개 공시 1.000/섹션 0.714, 시점 10개 0.900/0.500, 정형표 19개 1.000/0.526.

읽는 법: 보고서 24개짜리 말뭉치라 "공시 맞추기"는 쉬운 편이다(키워드도 0.75). 모델 차이는 **섹션** 점수에서 봐야 한다.
임베딩만으로 키워드보다 공시 +0.22, 섹션 +0.11.

**메모리 넘침 사건**: fp32 배치 32로 돌리다 24,832개 지점부터 구간 속도가 1,000개당 380초로 떨어졌다(누적 평균 50→104초).
`nvidia-smi` 온도 55도·클럭 2,565MHz·사용률 100%는 정상이었고 메모리가 7,925/8,188MiB였다. 긴 표 조각이 모인 배치가 VRAM을 넘겨
Windows가 일반 RAM으로 넘긴 것. Ctrl+C 뒤 배치 8로 이어 돌리니 남은 9,917개가 54초/1,000개(537초). 배치마다 커밋하므로 끊어도 손실 없음.
→ 기본 배치 8, 진행 줄에 최근 구간 속도 추가.

**kure-v3 1차가 무너진 원인 (2026-11-25)**: 모델 설정에 접두어 `query: `(질문)·`passage: `(문서)가 있는데 코드는 질문에만 붙이고 조각에는 안 붙였다.
Mistral 계열 디코더(`Ministral3Model`, 2,048차원)라 접두어 유무로 벡터가 크게 달라져 질문과 조각이 서로 안 맞았다. 진단(노트북 1분):
NaN 없음(fp16·bf16 모두), 2,048→1,024 자르기는 유사도 0.680→0.676으로 무해, 접두어 있음/없음 비교에서 정답·오답 차이가 벌어짐.
→ `LocalEmbedder`가 `prompts.document`/`passage`를 조각에 붙이도록 수정, `--rebuild`(모델 임베딩 지우고 전부 다시) 추가. bge-m3·kure-v1은 접두어가 없어 영향 없음.
교훈: 모델마다 **질문·문서 접두어를 둘 다** 확인하고 한 쌍으로 붙인다. 모델 카드를 못 본 채 "자동 감지"에 맡기면 반쪽만 붙는 일이 생긴다.

**1단계 결론 (2026-11-25): `kure-v3`.** 공시 단위는 세 모델이 0.972로 같지만(24개 보고서라 천장), 섹션 단위에서 kure-v3가 0.861로 다른 둘(0.556)보다
36개 중 11개를 더 맞춘다. "차이가 0.05 안이면 빠른 모델" 조건에 전혀 해당하지 않는다. 속도는 bge-m3의 3.5배(55초/1,000개)지만 30만 조각에 약 4시간 40분으로
하루 밤 안이고 한 번만 하는 작업이다. 조건: **fp16 · 배치 8 · 접두어 query:/passage: 한 쌍 · 2,048→1,024 자르기**(자르기 무해는 진단으로 확인, 0.680→0.676).
읽는 법 주의: 24개 보고서 위의 숫자라 188개 전체에서는 공시 단위도 떨어질 것이다. 전체 임베딩 뒤 같은 세트로 다시 재어 적는다.

### 3단계 — 보조 단계 (2026-11-25, kure-v3 `section_path`, 다시 임베딩 없음)

| 설정 | hit@5 공시 | hit@5 섹션 | MRR 공시 | MRR 섹션 | 비고 |
|---|---|---|---|---|---|
| kure-v3만 (1단계) | 0.972 | 0.861 | 0.958 | 0.562 | 기준 |
| + `--filter corp` | 0.972 | **0.889** | 0.972 | **0.583** | 질문 1개 더, 순위도 올라감. 정형표 섹션 0.842→0.895 |
| + `--rerank nano` | 0.972 | 0.778 | 0.856 | 0.457 | **나빠짐** — 서술 1.000→0.714, 정형표 0.842→0.789 |
| + corp + rerank | 0.972 | 0.917 | 0.958 | 0.544 | hit@5 최고지만 MRR은 필터만보다 낮음: 5위 안엔 넣되 1위로 못 올림 |
| `chunk:section_path --filter corp` (키워드) | 0.972 | 0.694 | 0.926 | 0.569 | 키워드 0.444→0.694. **서술 MRR 섹션 0.905**로 임베딩(0.679)보다 높음 |

**3단계 결론**
- **회사 필터는 켠다.** 규칙 기반(`guess_corp_code`), 비용 0, 임베딩·키워드 모두 올림. 회사명이 없는 질문은 자동으로 필터 없이 → 안전. PR 8의 Neo4j 검색에 `corp_code` 조건을 기본으로 넣는다(다시 판단할 조건 3번 충족)
- **재순위 nano는 지금은 뺀다.** 149M 교차 인코더가 1.1B 임베딩의 순서를 되레 흐린다(혼자 쓰면 질문 3개 잃음). 필터와 함께면 hit@5는 오르나 MRR이 내려가 "후보 안에 넣기"는 하고 "1위로 올리기"는 못 한다.
  36개에서 1~2개 차이는 우연 범위라 확정하지 않고, 0010에서 더 큰 재순위 모델(KURE-Reranker 상위 모델 또는 bge-reranker-v2-m3)과 188개 말뭉치로 다시 본다. 질문당 지연은 이번에 재지 않았음(eval.run에 시간 출력 추가 예정)
- **하이브리드의 근거.** 키워드+필터가 서술 질문 순위에서 임베딩보다 낫고, 임베딩은 정형표·시점에서 낫다 → 둘이 다른 질문을 맞춘다. PR 9의 RRF(벡터 상위 20 + 전문검색 상위 20)가 효과를 볼 근거

### 2단계 — 전략 (2026-11-25, kure-v3 fp16, `section` 임베딩 51.7초/1,000개)

| 전략 | hit@5 공시 | hit@5 섹션 | MRR 공시 | MRR 섹션 |
|---|---|---|---|---|
| `section` | 0.889 | 0.417 | 0.634 | 0.217 |
| `section` + corp | 0.972 | 0.639 | 0.958 | 0.426 |
| `section_path` | 0.972 | **0.861** | 0.958 | **0.562** |
| `section_path` + corp | 0.972 | **0.889** | 0.972 | **0.583** |

**2단계 결론**: 꼬리표(회사명 | 보고서 | 섹션 경로)는 임베딩에서도 섹션 단위 0.417 → 0.861. 회사 필터로도 메워지지 않는다(0.639) → 꼬리표는 회사명 이상의 맥락을 준다. 0008 확정.

## 최종 결정 (2026-11-25)

| 항목 | 결정 | 근거 |
|---|---|---|
| 임베딩 모델 | `kure-v3` (nlpai-lab/KURE-v3), fp16, 배치 8, 접두어 `query: `/`passage: `, 2,048→1,024 자르기 | 1단계: 섹션 hit@5 0.861 vs 0.556. 자르기 무해(0.680→0.676). 55초/1,000개 → 30만 조각 4시간 40분 |
| 조각 | `section_path` 1,000자 (0008) | 2단계: 0.861 vs 0.417 |
| 회사 필터 | 켬 (`guess_corp_code`, 회사명 없으면 자동 해제) | 3단계: 0.861 → 0.889, 키워드 0.444 → 0.694 |
| 재순위 | 없음 (0010에서 큰 모델로 재검토) | 3단계: nano 혼자 0.861 → 0.778 |
| 다음 | 188개 전체 임베딩(약 5시간, `--fp16 --batch 8`), 같은 세트로 재측정 → 7·8번 PR(Neo4j), 9번 PR 하이브리드(RRF)의 근거는 3단계 키워드+필터 서술 MRR 0.905 | |

## 다시 판단할 조건

- ~~세 모델의 hit@5 차이가 질문 2개(0.05) 안이면 → 가장 빠른 모델을 쓴다 (C1)~~ → 섹션 0.305 차이로 kure-v3 (2026-11-25)
- ~~kure-v3가 VRAM을 넘거나 1,000개당 60초를 넘어 30만 조각이 하루 밤을 넘기면 → kure-v1~~ → fp16·배치 8로 55초, 7,925MiB 넘침 없음 (2026-11-25)
- 188개 전체 임베딩 뒤 평가 세트에서 섹션 hit@5가 0.7 아래로 떨어지면 → 재순위·회사 필터(3단계)를 기본으로 켠다
- ~~회사 필터가 큰 폭으로 올리면~~ → 켠다(2026-11-25: 섹션 0.861→0.889, 키워드 0.444→0.694). 8번 PR의 Neo4j 검색에 `corp_code` 조건을 기본으로 넣고, 질문에서 회사명을 뽑는 규칙을 다듬는다
- ~~재순위가 hit@5를 올리지 못하거나 질문당 2초를 넘으면~~ → nano는 혼자서 hit@5를 내림(0.861→0.778) → 빼고 0010/9번 PR에서 더 큰 모델로 다시 본다 (2026-11-25)
