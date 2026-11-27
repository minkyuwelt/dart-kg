# Phase 4 조사: 지식그래프·GraphRAG·시간 그래프 관련 논문과 도구

- 작성: 2026-11-25
- 목적: 0011(그래프 스키마·GraphRAG 방식)을 쓰기 전에, 사용자가 준 논문 5편과 최근 실증 연구를 읽고 **우리 질문(usecases.md 2절)에 무엇이 도움이 되고 무엇이 아닌지**를 정한다. 항목마다 채택·보류·제외와 그 이유를 적어, 나중에 "왜 이 방식인가"를 물으면 이 줄을 가리킬 수 있게 한다
- 읽은 방식: 논문은 arXiv HTML 원문을 내려받아 전문을 읽었다(아래 "전문"). 도구는 공식 저장소 코드·공식 문서를 읽었다("코드"·"문서"). 검색 결과 요약만으로 아는 것은 "※ 미검증"으로 표시했다. 학회 게재 여부는 검색 결과 기준이라 모두 미검증이다
- 짝이 되는 문서: `phase3-research.md`(청킹·임베딩·검색), `phase3.md` 11절(스키마 방향), `usecases.md`(질문 12개·차별점 5개)

## 0. 한 장 요약

1. **그래프는 질문 유형에 따라 득이 되기도, 해가 되기도 한다.** 단일 사실 조회·세부 숫자·"답 없음" 판단은 좋은 하이브리드 RAG가 더 낫고, 2~3홉 관계 경로·전체 구조 요약은 그래프가 낫다(1절). → 질문을 먼저 분류해 **RAG와 그래프로 나눠 보내는 라우터**가 필요하다. usecases 질문 1·7·10·11은 RAG, 2·3·5·9는 그래프.
2. **시점 질문의 이득은 그래프 자체가 아니라 "날짜 속성으로 거르는 것"에서 온다.** 시간 필터를 끄면 정답률이 0.599→0.382로 떨어진 실험이 있다(1.3). 우리 0005 이중 시간축(`valid_from`·`valid_to`)을 Cypher 조건으로 거는 방식이 맞다. Graphiti도 같은 모델이다(2.1).
3. **LLM이 Cypher를 쓰게 하지 않는다.** 최고 모델도 실행 정확도 60~77%, 시간 조건·집계에서 더 낮고 "돌아가지만 틀린 답"이 많다(1.4). 질문 의도별 **고정 Cypher 템플릿** 7~8개로 간다. 상업등기 Agentic GraphRAG(ETH)도 미리 작성된 Cypher 엔드포인트만 쓴다(2.2).
4. **그래프 탐색은 1-hop(경로 질문만 2~3-hop)이면 충분하다.** Microsoft GraphRAG·LightRAG·HippoRAG 2 모두 임베딩으로 시작점을 찍고 1-hop만 간다(3절). 우리 그래프는 회사 59개·사람 수백 명이라 더 깊이 갈 이유가 없다.
5. **LLM 관계 추출을 쓰지 않는 결정(usecases 4절)은 더 굳어졌다.** 텍스트에서 LLM이 뽑은 그래프(GraphRAG·LightRAG)는 공시 QA에서 Pass 42.5%·13.7%로 나빴고(2.4), 추출 트리플을 채점기에 주면 채점이 더 틀렸다(2.3). 상업등기 연구도 LLM 추출 "약한 노드"의 관계 F1이 0.571이었다(2.2).
6. **융합은 RRF로 충분하고, 크로스인코더 재순위는 조심한다.** 보정 융합이 RRF와 유의차가 없었고, 크로스인코더가 멀티홉의 다리 문서를 떨어뜨린 사례가 있다(1.5). 우리 0009 3단계에서 재순위기가 hit@5를 낮춘 결과와 같은 방향이다.

## 1. 그래프가 도움 되는 질문은 무엇인가 (실증 연구)

### 1.1 질문 유형별 결과

| 질문 유형 | 그래프 도움? | 근거(원문 표 수치) |
|---|---|---|
| 단일 사실 조회 ("대표이사 누구", "지분율 얼마") | **아니오** | NQ F1: RAG 68.18 vs GraphRAG-Local 65.44, HippoRAG2 64.03 [R1]. GraphRAG-Bench Fact Retrieval: RAG 60.92 vs HippoRAG2 60.14, MS-GraphRAG 49.29 [R2]. 의료 코퍼스에서 MS-GraphRAG 38.63으로 급락 [R2] |
| 세부 숫자·"답 없음" 판단 | **아니오(해친다)** | 답이 없는 질문: RAG 91.36 vs GraphRAG-Global 13.95 (환각) [R1] |
| 2~3홉 관계 경로 ("A 자회사의 임원", "두 회사를 잇는 사람") | **예** | 2Wiki F1 61.5→71.0 (HippoRAG2 vs 최강 임베딩) [R4]; MultiHop-RAG에서 GraphRAG만 맞힌 질문 13.6%, RAG만 맞힌 질문 11.6% → 상보적 [R1] |
| 시점·시간 순서 ("2023년 3월 기준") | **혼합 → 시간 필터가 핵심** | Temporal 유형: RAG 25.73 vs GraphRAG 49.06~59.18 [R1]. 그러나 TG-RAG에서 시간 필터만 끄면 0.599→0.382 [R5] → 이득은 그래프가 아니라 날짜 조건 |
| 전체 구조 요약 ("지주 체계가 어떻게 생겼나") | **예** | Contextual Summarize: RAG 51.30 vs MS-GraphRAG 64.40 [R2] |
| 수학·선다형 | **아니오** | 교재 벤치마크에서 수학은 모든 GraphRAG가 하락 [R3] |
| 벡터 재현율이 이미 높은 코퍼스 | **이득 없음** | HotpotQA(벡터 95.5%)에서 그래프 융합 0승 0패 [R8] |

인용: "RAG performs better on single-hop questions and those requiring fine-grained details, whereas GraphRAG is more effective for multi-hop and reasoning-intensive questions." [R1] / "all previous structure-augmented methods underperform against the strongest embedding-based RAG methods available on all three benchmark types." [R4]

**우리에게:** usecases 질문 12개를 둘로 나눈다. RAG가 맡는 것(1 시점 숫자·7 서술·10 정형표·11 지표)과 그래프가 맡는 것(2 임원 이력·3 겸직·5 간접 지분·9 사명 변경). 질문 4·6·8(변화)은 변화 리포트(0012)가 맡는다. **그래프 질문도 벡터 검색을 함께 돌려 RRF로 합친다** — 상보적이라는 결과 때문이다.

### 1.2 라우팅

의도를 먼저 분류해 RAG와 그래프로 나눠 보내는 방식이 두 연구에서 공통 처방이다 [R1][R6]. 상업등기 Agentic GraphRAG도 의도 5종 라우터를 두었고, 라우터의 기여는 "평균 점수 상승이 아니라 심각한 검색 실패를 줄이는 것"이었다(실패율 0.280→0.073) [P1].

**채택:** 질문 → 의도(시점 숫자 / 서술 / 관계 경로 / 변화 / 답 없음 후보) 분류 → 의도별 검색 경로. 분류기는 1차로 **규칙**(회사명·사람명·날짜·"겸직"·"지분" 같은 단어)으로 시작하고, 평가 세트에서 규칙이 놓치는 질문이 보이면 LLM 분류를 더한다. 이유: 규칙은 돌릴 때마다 같고(재현), 비용이 0이며, 우리 질문 12개 범위가 좁다.

### 1.3 시간 조건

- TG-RAG [R5]: 사실마다 시각을 붙이고 같은 엔티티 쌍의 다른 시점 사실을 **별도 엣지**로 보존, 질의 시 시간 범위 밖 엣지를 **필터**한 뒤 탐색. 실적발표록 QA에서 Correct 0.599 (NaiveRAG 0.385, GraphRAG 0.405, HippoRAG2 0.410). 시간 필터를 끄면 0.382, 거부율 0.191→0.423. 인용: "when Temporal Retrieval is disabled, Correct score drops substantially from 0.599 to 0.382 ... highlighting the importance of temporal filtering for handling time-sensitive queries."
- MRAG [R7]: 그래프 없이, 질문을 "내용 + 시간 제약"으로 쪼개 의미 점수와 기호적 시간 점수를 따로 매긴다. 시간 표현만 바꾼 질문에서 재순위기 top-1 재현율이 85.8→54.7로 급락 — **LLM이 글 속 날짜를 읽는 데 의존하면 취약하다**는 수치.

**채택(이미 있음):** 0005의 `valid_from`·`valid_to`·`source_rcept_no`·`superseded_by`가 TG-RAG의 "시점별 별도 엣지 + 필터"와 같은 구조다. Phase 4에서는 모든 관계 패턴에 `r.valid_from <= $asof AND (r.valid_to IS NULL OR r.valid_to > $asof) AND r.superseded_by IS NULL`을 붙이고, `$asof`는 질문의 날짜를 **규칙으로 파싱**(없으면 최신)한다. LLM에게 날짜를 읽게 하지 않는다. 이유: 위 두 연구의 수치, 그리고 Phase 2에서 이미 검증된 시점 필터(phase2_questions, `src/loaders/as_of.py`)를 그대로 쓸 수 있다.

### 1.4 LLM이 Cypher를 쓰는 방식(Text2Cypher)의 신뢰도

| 벤치마크 | 제로샷 실행 정확도 | 비고 |
|---|---|---|
| CypherBench [R9] | claude-3.5-sonnet 61.58%, gpt-4o 60.18%, gpt-4o-mini 31.43%, 10B 미만 <20% | 실행 가능 비율은 94.9% → **"돌아가지만 틀린 답"이 주류**. 실패: 관계 방향 반전, 엔티티 링킹, 동명이인 병합, 집계 단위. "gpt-4o struggles with time-sensitive questions" |
| Mind the Query [R10] | DeepSeek-V3 76.75%, Qwen2.5-72B 66.32%, Llama-3.1-8B 30.99%(미세조정 후 70.15%) | 복잡 집계는 단순 질의보다 평균 37pt 낮음. "Structural Hallucination: confusing relationships such as PLAYED_IN vs. PLAYED_FOR" |

neo4j-graphrag 패키지의 Text2CypherRetriever 공식 문서도 "The LLM-generated query is not guaranteed to be syntactically correct."라고만 적고 정확도 수치는 없다 [T2]. 상업등기 Agentic GraphRAG는 자유 Cypher 도구를 두었지만 **의도별로 미리 작성한 Cypher 엔드포인트**가 주 경로이고, 적재도 "we chose this approach over LLM-generated database queries to ensure consistent schema formats and reduce the potential for hallucinations"(Graphiti 논문 §2.2.1)와 같은 이유로 고정 Cypher다 [P1][T1].

**채택:** 질문 의도별 **파라미터화 Cypher 템플릿**(회사 as-of 임원 / 사람의 직위 이력 / 겸직 / 간접 지분 경로 / 두 노드 사이 경로 / 사명 변경 이력 / 회사의 근거 공시). LLM은 템플릿을 고르고 파라미터(corp_code·person_id·날짜)를 채우는 데에만 쓰거나, 그것도 규칙으로 되면 LLM을 쓰지 않는다. **자유 Text2Cypher는 넣지 않는다**(틀린 Cypher가 그럴듯한 답을 만드는 것이 지배구조 데이터에서 가장 나쁜 실패). 템플릿 밖 질문은 "이 질문은 아직 답할 수 없습니다"로 돌려준다. 이유: 위 수치와 D5(추측이 데이터에 섞이지 않음).

### 1.5 그래프 결과와 벡터 결과의 융합

- PhaseGraph [R8]: 코사인·PPR 점수를 백분위로 보정해 융합했으나 **RRF와 유의차 없음**(7승 11패). 벡터가 충분한 코퍼스에서는 이득 0. 크로스인코더 재순위가 멀티홉 다리 문서를 떨어뜨려 78.8→9.1로 붕괴. "graph fusion only helps when vector retrieval is insufficient."
- 기업 문서 하이브리드 [R11]: 벡터·BM25·그래프 이웃을 가중 RRF(k=60)로 합친 뒤 크로스인코더 0.65 + RRF 0.35로 혼합. 그래프 확장 제거 시 근거율 89.6→85.2. "Retaining the RRF component prevents the cross-encoder from completely discarding evidence that multiple retrieval signals agreed on." (단일 산업 사례)

**채택:** 그래프 탐색으로 얻은 관계 행과 벡터·전문검색 조각을 **RRF(k=60)로 합친다**(0010의 하이브리드와 같은 식, 리스트가 하나 더 늘어남). 크로스인코더는 쓰지 않는다 — 0009 3단계에서 재순위기가 hit@5를 0.889→0.778로 낮췄고, 위 연구가 같은 위험을 보인다. 다시 시험할 때는 RRF 점수를 남기는 혼합(0.65/0.35)만 후보로.

## 2. 사용자가 준 논문 5편 (전문 확인)

### 2.1 Zep/Graphiti: 에이전트 기억용 이중 시간 그래프 [T1]

- **네 시각을 엣지에 둔다.** "the system tracks four timestamps: t′created and t′expired monitor when facts are created or invalidated in the system, while tvalid and tinvalid track the temporal range during which facts held true. These temporal data points are stored on edges alongside other fact information." (§2.2.3) 코드(`EntityEdge`)의 `valid_at`·`invalid_at`·`created_at`·`expired_at`과 같다.
- **지우지 않고 닫는다.** 새 사실과 모순이면 `edge.invalid_at = new.valid_at`, `edge.expired_at = now`. "Graphiti consistently prioritizes new information when determining edge invalidation." 모순 판단은 **LLM**이 한다(같은 엔티티 쌍 안에서만).
- **시점 질의용 Cypher는 없다.** `SearchFilters`로 `valid_at <= T AND (invalid_at IS NULL OR invalid_at > T)`를 사용자가 조합한다. 검색 결과에는 유효 기간을 사실 옆에 같이 넣어 LLM에 보낸다.
- 저장: 관계 속성 `(:Entity)-[:RELATES_TO {fact, fact_embedding, valid_at, ...}]->(:Entity)`. 시간 필드마다 범위 인덱스. **벡터 인덱스 없음** — 관계에는 Neo4j 벡터 인덱스를 못 걸어 `vector.similarity.cosine` 전수 계산. 검색은 코사인 + BM25 + BFS 병렬 → RRF·MMR·언급 수·노드 거리·크로스인코더 중 택일.
- 평가는 벤더 자체(DMR 94.8% vs MemGPT 93.4%; 논문 스스로 "each conversation contains only 60 messages, easily fitting within current LLM context windows."라고 한계를 적음).

**우리와:** 0005와 1:1 대응이다 — `valid_from/valid_to` = `tvalid/tinvalid`, `source_rcept_no/superseded_by` = 기록 시간축. 다른 점은 둘: Graphiti는 모순을 LLM이 판단하고 우리는 **규칙**(다음 보고서 기준일, 정정본 접수번호)으로 정한다(우리 쪽이 재현 가능); Graphiti는 사실 문장에 임베딩을 두지만 우리 관계는 정형 값이라 임베딩이 필요 없다. **가져올 것:** (a) 시간 필드마다 범위 인덱스, (b) 검색 결과에 유효 기간을 사실 옆에 적어 LLM에 넘기는 직렬화, (c) 관계가 아닌 **노드에만 벡터 인덱스**(우리는 Chunk 노드만 벡터, 관계는 필터로 찾음 — 이미 그렇게 설계됨). 결론: **채택(이미 구현된 모델의 확인)**.

### 2.2 Agentic Graph RAG for Auditable Commercial Registry Analysis (ETH Zurich) [P1]

- 스위스 상업공보 7년치, Neo4j 노드 524만. 노드 Company·Person·Event·NameHub. **강/약 노드**: 구조화 필드에서 결정적으로 만든 strong, GPT-4o mini가 공보 본문에서 뽑은 weak(청산인·채권자 등). Table S2에 유형별로 따로 집계(Person strong 57,508 / weak 606,392). 신뢰도 점수는 저장하지 않음.
- Person–Company 직접 엣지가 없고 **항상 Event(공보)를 거친다**. 관계 타입은 역할에서 결정적으로 매핑.
- 동일인 식별은 **이름 정규화 키의 바이트 일치만** ("This mechanism is intentionally strict ... no fuzzy tolerance is introduced." §5.1.3). 회사 식별 진단 정밀도 0.733·재현율 0.422 — 보수적이어서 놓치는 쪽.
- **시간은 Event 속성만.** 관계 유효기간·정정 처리 없음. 평가에서 "청산된 법인을 별개 기업으로 처리" 같은 상태 변화 오류가 보고됨.
- 흐름: 의도 라우터(5종) → 반영 루프 ≤4회, 도구 7개는 **미리 작성된 Cypher 엔드포인트**(벡터 검색은 에이전트 도구에 없음, 전문검색 폴백만) → 상태기계 + 합성. LLM 2~6회, 지연 12.5초.
- 평가(수작업 60문항 정답률): Dense 0.143 / Lexical 0.237 / Hybrid(RRF) 0.262 / Structured-only(약한 노드 제외) 0.545 / 라우터 없음 0.708 / Full 0.828. 실패는 동일인 식별(L2)에 집중. weak 추출 관계 F1 0.571(120건 표본). 자동 벤치마크는 GPT-5가 생성·판정해 순환성이 있음(저자 명시).
- "Auditable"의 뜻: 답 아래 **실행 트레이스**(도구·인자·반환 레코드)와 대시보드. "The interface itself does not make the agent intrinsically trustworthy. Instead, it supports auditability by providing the evidence, graph neighborhoods, event histories, and execution traces used to produce an answer." (§5.3)

**우리와:** 같은 생각 — 정형 필드는 결정적으로, 사람은 식별자로, Cypher는 미리 작성, 공시(Event)를 근거 노드로. 다른 점 — 그들은 약한(LLM 추출) 노드를 그래프에 **넣고** 유형별 집계로만 구분하며 신뢰도를 저장하지 않는다. 우리 phase3.md 11절의 질문("약한 노드를 넣되 구분 표시가 나은가")에 대한 답: **넣지 않는다(기존 결정 유지)**. 이유: 그들의 수치에서 약한 노드가 올린 것은 0.545→0.828이지만 그 대부분은 라우터·반영(0.545→0.708)이고, 약한 노드의 관계 F1 0.571은 지배구조 데이터에 쓸 수 없는 수준이다. 우리 관계는 이미 정형 표에서 다 나온다. **가져올 것:** (a) 의도 라우터(1.2), (b) Person–Company를 Document 경유로도 이을 수 있게 `HOLDS_POSITION`에 `source_rcept_no`를 두는 것(이미 그렇게 함), (c) **실행 트레이스를 답과 함께 돌려주는 API 응답**(어느 템플릿·어느 파라미터·몇 행), (d) 동일인 식별을 보수적으로 두고 틀린 병합보다 놓침을 택하는 원칙(0006과 같음). **제외:** 반영 루프(비용·지연 대비 우리 질문 범위가 좁다), 약한 노드.

### 2.3 FinReflectKG - HalluBench (Domyn Inc, 2026-03) [P2]

- 과제는 답변 생성이 아니라 **환각 탐지기 평가**. S&P100 10-K에서 LLM이 뽑은 트리플(검증 안 함)로 QA 1,302쌍 → 사람 9명(1인 레이블) → 755개(Grounded 68%).
- Grounded 정의: "an answer is considered Grounded only if it is correct with respect to the source chunk and is supported by both (a) the supporting triplet identified by the generator, and (b) the supporting lines (textual snippet(s)) from the chunk." (§4.1)
- 어노테이터 거부 규칙(§5.2)이 실질 택소노미: ① 회사·연도·지표를 특정하지 않은 질문 ② 단위 누락 ③ 단위 가정 ④ 맥락에 없거나 트리플 값이 틀림 ⑤ "Answer might be correct but it cannot be determined from the given context". KG 트리플 오류는 **시점 불일치**가 가장 흔함, 그다음 가짜 엔티티·숫자 오류·교차 오염.
- **핵심 결과:** 트리플을 채점기에 주면 채점이 **나빠진다**(Qwen judge MCC 0.505→0.205, Net −73). "LLM-based judges exhibit preferential trust of structured signals over textual evidence, anchoring on KG format even when contradicting prose–a structural bias that amplifies extraction errors." (§6.2)

**우리와(채택, 평가 쪽):** 0010 사람 채점표를 보강한다 — "근거 없는 주장"을 (a) 지문과 모순 / (b) 맞을 수 있으나 지문만으로 확정 불가로 나눔, 단위·기간 누락을 "부분"의 명시 기준으로, **시점 불일치**(다른 보고서 기준일 값)와 **교차 오염**(같은 조각 안 다른 회사 값) 태그 추가, 레이블 + 이유 + 범주 3칸. 회사·기준일이 특정되지 않은 질문은 평가 세트에서 뺀다(이미 corp_code·rcept_no를 박는 원칙과 같음). 자동 채점(LLM judge)을 넣을 때는 **지문만** 주는 조건을 기준선으로 하고 그래프 관계를 채점기에 넣지 않는다. 이 결과는 "그래프 사실을 LLM 답에 섞을 때 LLM이 그래프를 과신한다"는 경고이기도 하다 → Phase 4 답변에서 관계 행을 넣을 때 **근거 공시 조각도 함께** 넣는다.

### 2.4 FinSage (2025-08, 산업 협력) [P3]

- 공시 QA 파이프라인: MinerU 파싱, 표·그림을 GPT-4o로 문장 재서술, 소제목 안 200~256토큰 청킹, **4경로(BM25·BGE-M3·섹션요약 metadata·HyDE) 단순 합집합 → DPO 미세조정 리랭커**(+최신 공시 가산) → top-5. "the rapid degradation in these metrics between Top-10 and Top-15 aligns with previous studies suggesting that retrieving only 5 to 10 chunks is generally sufficient" (§5.2)
- **GraphRAG·LightRAG 비교**(자체 75문항, GPT-4o 채점): Pass% FinSage 82.67 / GraphRAG 42.50 / LightRAG 13.67. "the additional complexity introduced by graph-based representation does not lead to improved response quality in our experimental context" (§5.4). 단, 그래프 쪽은 기본 설정이고 FinSage는 같은 데이터로 학습해 공정 비교가 아니며, 원인 분석도 없다. plain RAG와의 끝까지 비교는 없다.
- 실서비스 피드백: 지분구조 4.22, **재무상태·실적 3.88 최저**("precise numerical data retrieval and temporal reasoning across multiple chunks"가 필요).
- 단일 경로 재현율: 섹션요약 metadata 0.857 최고, dense 0.82; 리랭커 없는 4경로 합집합 정밀도 0.13; 앞뒤 청크 번들 확장은 이득 없음(0.819→0.810).

**우리와:** 그래프에 대한 경고(제외할 것은 **텍스트 추출 그래프**이며 우리 계획과는 다른 접근) + Phase 3 검색에 대한 힌트 셋. (a) **채택 후보:** 섹션 요약을 청크 꼬리표로 — 우리 `section_path`가 이미 섹션 경로를 붙이고 있어 비용이 작다, 0008 재측정 시 비교. (b) **채택:** 같은 내용이 여러 연도에 반복될 때 최신 공시 가산(`rcept_dt`)을 RRF 뒤 규칙으로 — 시점 질문 아닌 경우에만. (c) **보류:** 리랭커 재시험은 다국어 모델(bge-reranker-v2-m3)로, 0009 3단계 결과(nano 재순위 하락)와 1.5의 위험을 함께 보고. (d) **제외:** 번들 확장, 표 재서술(DART는 정형 API가 있음).

### 2.5 Knowledge Graph Construction for Stock Markets (CIKM'25, 하나금융 계열 저자) [P4]

- 노드 Company·Sector·Indicator·StockPrice·FinancialStatements·Date·Quarter·Year, 관계 HAS_*·BELONGS_TO·COMPETES_WITH. 식별자는 **6자리 종목코드**(상장사만). 출처 KIS API·OpenDART 재무제표·KRX. 구축은 전부 규칙, LLM은 Text-to-Cypher(GPT-4.1) 답변에만. 정량 평가 없음(케이스 스터디 2건).
- 저자 한계: "The analysis was restricted to the competitive relationships among companies, which limits the exploration of other potentially informative connections, such as supply chain links, shareholder networks, or cross-sector influences." (§5)

**우리와:** 같은 한국·Neo4j·OpenDART. 그들이 **future work로 둔 shareholder network와 임원·공시 근거가 우리 핵심**이다. 종목코드 식별은 우리 대상 59곳 중 다수가 비상장이어서 쓸 수 없다(corp_code 유지). **가져올 것:** (a) 스키마 속성 사전(Table 2 수준)을 LLM 프롬프트에 주는 방식 — 템플릿 선택 프롬프트에 쓸 수 있음, (b) **Year/Quarter 노드를 집계 보조로 두는 패턴** — 엣지 시간 속성과 병행 가능, 0011에서 "변화 리포트 2차·연도 비교 질문에 필요해지면"으로 보류. **제외:** Text-to-Cypher 단일 경로(1.4).

### 2.6 FinKario (ACL 2026 Long, arXiv 2508.00961) [P5]

- 중국 증권사 리서치 보고서에서 **전부 LLM(GPT-4o-mini)으로** 뽑은 이중 구조: Attribute Graph("relatively stable entity-level properties") + Event Graph(지표 변동의 원인 사건, FIBO 기반 하위 온톨로지). 정제 단계에서 **엔터티 정규화와 수치를 외부 정형 데이터(Tushare)로 덮어씀**. 시간은 보고서 날짜 하나("entities are extracted at each timestamp" §3.1.3), 유효기간·충돌 처리 없음. 평가는 주가 백테스트(ACC 0.581).

**우리와:** 출발점이 다르다(비정형→LLM 추출, 주가 예측). 같은 방향 하나 — LLM 값을 불신하고 정형 소스로 덮어쓰는 정제. **가져올 것:** 속성(정적)/사건(동적) 분리는 우리 "Corp 속성 vs 시점 있는 관계 엣지"에 대응하는 확인. **제외:** 이벤트 그래프(우리는 그 영역을 Phase 3 원문 청크가 맡고, LLM 추출을 그래프에 넣지 않음), 주가 예측 평가. 우리 0005가 이 논문보다 시간 모델이 엄격하다는 점은 0011에 차별점으로 적을 수 있다.

## 3. "검색 뒤 그래프 탐색"은 어떻게 하나 (코드 확인)

| 시스템 | 시작점 | 탐색 | LLM에 넘기는 형태·예산 |
|---|---|---|---|
| Microsoft GraphRAG local [G1] | 질문 임베딩 → 엔티티 10개 | **1-hop**. 선택 엔티티 사이 관계는 전부, 바깥 관계는 "선택 엔티티 몇 개와 연결됐나(links)" 순으로 10개 | `\|` 구분 표(엔티티·관계·커뮤니티 리포트·청크). 12,000토큰, 청크 50%·커뮤니티 15~25% |
| LightRAG [G2] | LLM이 뽑은 엔티티/주제 키워드 → 엔티티·관계 벡터 top 40 | **1-hop** 인접 엣지, 차수·weight 순. mix 모드는 청크 벡터 20개를 라운드로빈 병합 | 표 + 청크. 엔티티 6k·관계 8k·총 30k 토큰. 원문 청크를 빼도(-Origin) 하락이 거의 없음 |
| G-Retriever [G3] | 노드 top-3·엣지 top-5 | PCST(상금 수집 슈타이너 트리)로 **다리 노드**까지 연결 부분그래프 | 노드 CSV + (src, 관계, dst) CSV. 검색 정확도 top-k 트리플 60.8 vs PCST 70.5 |
| HippoRAG 2 [G4] | 트리플 벡터 top-5 → LLM 필터 → 구 노드 가중 + 청크 노드 가중 0.05 | **PPR**(damping 0.5) | 청크 5개 본문. Recall@5 평균 73.4→78.2(최강 임베딩 대비); 청크 노드 제거 시 87.1→81.0 |

공통: **임베딩으로 시작점을 찍고 그래프는 1-hop(또는 PPR)만** 간다. LLM이 질의를 쓰지 않는다.

**우리 설계에 옮기는 것(채택):**
1. **GraphRAG local 패턴을 고정 Cypher로.** Chunk 벡터 top-k → `(:Chunk)<-[:HAS_CHUNK]-(:Document)<-[:FILED]-(:Corp)` → Corp에서 `OWNS`·`HOLDS_POSITION` 1-hop, 모든 관계에 시점 필터(1.3). 질문에 회사·사람 이름이 있으면 DuckDB 사전(`target_corp`·`person`)으로 **직접 진입**도 함께(LightRAG의 이중 진입을 LLM 없이).
2. **in/out-network 분리.** 찍힌 Corp 사이 관계는 전부, 바깥 노드는 "찍힌 Corp 여럿과 연결된 노드 우선" `ORDER BY links DESC LIMIT` — 겸직(질문 3)·교차 출자에 맞는 순서.
3. **경로 질문**(질문 2·5·9)은 `shortestPath` 또는 가변 길이 `[:OWNS|HOLDS_POSITION*1..3]`에 시점 필터. PCST·PPR은 우리 규모(회사 59·사람 수백)에서 필요 없다 — **보류**, 1-hop이 평가 세트에서 모자랄 때만.
4. **직렬화**는 G-Retriever 식 CSV 두 장: 노드(id, 이름, 유형), 관계(src, 관계, dst, 값, `valid_from`, `valid_to`, `source_rcept_no`). 근거 공시가 열에 그대로 붙는다(D3). 그래프 관계 행과 함께 **그 공시의 조각**도 넣는다(2.3의 경고).
5. **예산:** Chunk 10~20, 진입 Corp/Person ≤10, 홉 ≤2(경로만 3), 관계 행 ≤50, 총 약 12k 토큰(청크 50%·관계 표 35%·회사 요약 15%). 잘린 것은 "더 있음" 행으로 표시.

## 4. 도구: neo4j-graphrag 패키지 [T2]

- v1.24.0, Neo4j ≥5.18.1(2026.01+면 인덱스 내 필터 `SEARCH` 절), Python 3.10~3.14. 리트리버: Vector·VectorCypher·Hybrid·HybridCypher·Tools·Text2Cypher·외부 벡터DB 3종.
- `VectorCypherRetriever`: 벡터 인덱스 hit 뒤 `retrieval_query` Cypher를 이어 붙여 그래프를 확장하고 **속성만 반환** ("It is recommended that the retrieval query returns node properties, as opposed to nodes.").
- 하이브리드 융합은 max 정규화 또는 선형합만, **RRF 없음**. "Filters are implemented for all retrievers except the Hybrid retrievers." 2026.01 이전 버전에서 필터는 벡터 인덱스를 우회해 전수 비교.

**판단: 패키지는 쓰지 않는다(제외).** 이유: 우리에게 필요한 (a) RRF 융합, (b) 하이브리드 + 시점 필터, (c) 한국어 전문검색 분석기(0004) 중 둘을 지원하지 않고, 이미 `src/rag/retrievers/neo4j.py`(당시 `eval/retrievers/`)에 드라이버 + 직접 Cypher로 하이브리드가 있다. **가져올 것:** `retrieval_query`의 "인덱스 hit → 그래프 확장 → 속성만 반환" 2단계 구조(3절 1번과 같다). 이 판단은 0011에 "대안 비교" 절로 옮긴다.

## 5. 결정 0011에 넣을 것 (초안 뼈대)

1. 노드·관계: phase3.md 11절 그대로(Corp·Person·Document·Chunk / OWNS·HOLDS_POSITION·FILED·HAS_CHUNK·FORMERLY). 변경 없음 — 조사에서 이 범위를 넘는 필요가 나오지 않았다.
2. 시간: 모든 관계에 `valid_from`·`valid_to`·`source_rcept_no`·`superseded_by`, 각 시간 필드에 범위 인덱스(2.1). 시점 필터 Cypher 조각 하나를 상수로 두고 모든 템플릿이 재사용.
3. 질의 방식: **의도 라우터(규칙 → 필요 시 LLM) + 고정 Cypher 템플릿 7~8개 + 벡터·전문검색과 RRF 융합**. 자유 Text2Cypher 없음(1.4). 템플릿 밖은 "답할 수 없음".
4. 탐색 깊이: 1-hop 기본, 경로 질문만 ≤3(3절). 예산 표.
5. 답변 직렬화: 관계 CSV + 근거 조각, 유효 기간을 사실 옆에(2.1·2.3). API 응답에 **실행 트레이스**(템플릿 이름·파라미터·행 수) 포함(2.2).
6. 하지 않는 것과 이유: LLM 관계 추출·약한 노드(2.2·2.4·usecases 4절), Text2Cypher(1.4), 크로스인코더 재순위(1.5), neo4j-graphrag 패키지(4절), PCST/PPR(3절, 보류).
7. 평가: 그래프 질문 12개(usecases 2·3·5·9 기준)를 먼저 만들고, **RAG만 / 그래프만 / RRF 결합** 셋을 같은 세트로 잰다(1.1의 상보성 때문에 결합이 가장 좋을 것으로 예상하지만 측정 전에는 적지 않음). 채점표는 2.3의 보강판.
8. 미결(0011에서 의견을 묻는다): phase3.md 11절의 ① 사명 변경 ② corp_code 없는 출자 대상 ③ 특수관계인 + ④ Year/Quarter 보조 노드(2.5) 필요 여부.

## 6. 이 조사가 바꾼 것 / 확인한 것

| 항목 | 전 | 후 | 근거 |
|---|---|---|---|
| 질의 방식 | 미정(Text2Cypher 가능성 열어 둠) | 고정 템플릿 + 라우터, Text2Cypher 제외 | 1.4, 2.2 |
| 약한 노드(LLM 추출) | "넣되 구분 표시가 나은지 다시 본다" | 넣지 않음(유지) | 2.2 F1 0.571, 2.3 채점 편향, 2.4 Pass 42.5% |
| 탐색 깊이 | 미정 | 1-hop 기본, 경로만 ≤3 | 3절 |
| 융합 | 하이브리드 RRF(0010) | 그래프 행도 같은 RRF에, 크로스인코더 제외 | 1.5, 0009 3단계 |
| 시간 | 0005 이중 시간축 | 유지 + 범위 인덱스 + 질문 날짜 규칙 파싱 | 1.3, 2.1 |
| 평가 채점표 | 맞음/부분/틀림/근거 없음 | 근거 없음 2분할, 단위·기간, 시점 불일치·교차 오염 태그 | 2.3 |
| neo4j-graphrag | 검토 대상 | 쓰지 않음, 구조만 차용 | 4절 |
| Phase 3 검색 힌트 | — | 섹션 요약 꼬리표·최신 공시 가산 후보, 번들 확장 제외 | 2.4 |

## 출처

논문(전문 확인):
- [P1] Capozzi, Helbing. "Agentic Graph Retrieval-Augmented Generation for Auditable Commercial Registry Analysis." arXiv 2605.18770v3 (2026-09). ETH Zurich
- [P2] Kumar, Sarmah, Pasquali. "FinReflectKG - HalluBench: GraphRAG Hallucination Benchmark for Financial Question Answering Systems." arXiv 2603.20252 (2026-03). Domyn
- [P3] Wang et al. "FinSage: A Multi-aspect RAG System for Financial Filings Question Answering." arXiv 2504.14493v4 (2025-08)
- [P4] Lee et al. "Knowledge Graph Construction for Stock Markets with LLM-Based Explainable Reasoning." arXiv 2601.11528, CIKM'25 (2025-11)
- [P5] Li et al. "FinKario: Event-Enhanced Automated Construction of Financial Knowledge Graph." arXiv 2508.00961 (2025-08); ACL 2026 Long(※ 게재는 검색 결과 기준)
- [T1] Rasmussen et al. "Zep: A Temporal Knowledge Graph Architecture for Agent Memory." arXiv 2501.13956 (2025-01) + getzep/graphiti 소스코드·README
- [G1] Edge et al. "From Local to Global: A Graph RAG Approach to Query-Focused Summarization." arXiv 2404.16130 + microsoft/graphrag 코드·문서(local/drift search), LazyGraphRAG 블로그
- [G2] Guo et al. "LightRAG: Simple and Fast Retrieval-Augmented Generation." arXiv 2410.05779 + HKUDS/LightRAG 코드·README
- [G3] He et al. "G-Retriever: Retrieval-Augmented Generation for Textual Graph Understanding and Question Answering." arXiv 2402.07630 + 코드
- [G4] Gutiérrez et al. "From RAG to Memory: Non-Parametric Continual Learning for Large Language Models" (HippoRAG 2). arXiv 2502.14802v2 + OSU-NLP-Group/HippoRAG 코드
- [R1] Han et al. "RAG vs. GraphRAG: A Systematic Evaluation and Key Insights." arXiv 2502.11371v2 (2025-10)
- [R2] "When to use Graphs in RAG: A Comprehensive Analysis for Graph Retrieval-Augmented Generation" (GraphRAG-Bench). arXiv 2506.05690v3 (2026-02)
- [R3] "GraphRAG-Bench: Challenging Domain-Specific Reasoning for Evaluating Graph Retrieval-Augmented Generation." arXiv 2506.02404 (2025). 표 수치는 HTML 변환에서 유실 — 결론 문장만
- [R4] = [G4]
- [R5] "RAG Meets Temporal Graphs: Time-Sensitive Modeling and Retrieval for Evolving Knowledge" (TG-RAG). arXiv 2510.13590 (2025-10)
- [R6] EA-GraphRAG. arXiv 2602.03578 (2026). 선행연구 수치 인용만 확인, 자체 표는 미확인
- [R7] "MRAG: A Modular Retrieval Framework for Time-Sensitive Question Answering" (TempRAGEval). arXiv 2412.15540v2 (2025-05)
- [R8] "Calibrated Fusion for Heterogeneous Graph-Vector Retrieval in Multi-Hop QA" (PhaseGraph). arXiv 2603.28886 (2026). 피어리뷰 여부 불명
- [R9] "CypherBench: Towards Precise Retrieval over Full-scale Modern Knowledge Graphs in the LLM Era." arXiv 2412.18702v2 (2025-04)
- [R10] "Mind the Query." EMNLP 2025 Industry (aclanthology.org/2025.emnlp-industry.133)
- [R11] "Hybrid RAG with Knowledge Graph Expansion, RRF Fusion, and Per-Chunk Grounded Evaluation for Enterprise Document Search." arXiv 2609.01617 (2026). 단일 산업 사례, 피어리뷰 여부 불명

도구(공식 문서·코드):
- [T2] neo4j-graphrag-python 1.24.0: neo4j.com/docs/neo4j-graphrag-python/current/ (index, user guide RAG), GitHub neo4j/neo4j-graphrag-python

읽지 못한 것: Graphiti 공식 문서 사이트(help.getzep.com), 각 논문의 학회 게재 페이지(openreview·ACL 2026 anthology). 논문 PDF 안 그림·수식의 숫자 일부(HTML 변환 유실)는 "미확인"으로 두었다.
