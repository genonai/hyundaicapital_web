# GenOS 코드서빙 종합 예제 — LangGraph(interrupt) + Filter RAG + Text2SQL + HITL 툴 콜링

질문이 들어오면 **에이전트를 선택**하게 하고(HITL 1), 선택에 따라 **툴 콜링**으로 답한다.
부동산 분석은 LLM 이 만든 **SQL 을 사용자에게 보여주고 승인받은 뒤에만 실행**한다(HITL 2).

| 에이전트 | 하는 일 | 툴 | 승인 |
|---|---|---|---|
| 경제금융용어 사전 | Weaviate 하이브리드 검색, 파일명에 `GLOSSARY_FILE_FILTER` 가 들어간 문서만 (Filter RAG) | `search_finance_glossary(query)` | 없음 |
| 부동산 분석 (부산시) | 질문 → MySQL SELECT 생성 → **승인** → 실행 → 결과 해설 (Text2SQL) | `query_real_estate_sql(sql)` | **있음** |
| 일반 대화 | 검색·조회 없이 바로 답한다 | 없음 | 없음 |

```
START ─▶ pick ─▶ call_llm ──┬─ tool_calls 없음 ────────────────────────────▶ END
                    ▲       ├─ tool_calls 있음 · 승인 불필요(사전 검색) ─▶ run_tools ─┐
                    │       └─ tool_calls 있음 · 승인 필요(SQL 실행) ─▶ approve ─┬─▶ run_tools ─┤
                    │                                                          └─▶ END (거절)  │
                    └────────────── 툴 결과를 넣고 LLM 을 다시 부른다 ─────────────────────────┘

interrupt_before = ["pick", "approve"]
  pick 앞     에이전트를 골라 달라        (질문 직후, 아무 작업 전)
  approve 앞  이 SQL 을 실행해도 되겠나   (LLM 이 SQL 을 제안한 직후, 실행 전)
```

| 턴 | 사용자 | 그래프 | LLM | 툴 |
|---|---|---|---|---|
| 1 | 질문 | `pick` 앞에서 중단 → 선택 UI | 0회 | 0회 |
| 2 | 사전 선택 | `pick` → `call_llm` → `run_tools`(검색) → `call_llm` → END | 2회 | 1회 |
| 2 | 부동산 선택 | `pick` → `call_llm`(SQL 생성) → `approve` 앞에서 중단 → SQL 승인 UI | 1회 | **0회** |
| 3 | 승인 | `approve` → `run_tools`(SQL 실행) → `call_llm`(해설) → END | 1회 | 1회 |
| 3 | 거절 | `approve` → END | 0회 | **0회** |
| 2 | 일반 대화 선택 | `pick` → `call_llm` → END | 1회 | 0회 |

`thread_id` = 채팅 세션 ID. 상태가 checkpointer 에 남으므로 재개할 때 **멈춘 노드부터** 이어간다.

## 파일 — 이 순서로 읽으면 된다

| 순서 | 파일 | 역할 |
|---|---|---|
| 1 | `config.py` | 환경변수. `pydantic-settings` 로 한 번에 읽고 검증한다. 필수값이 비면 **부팅 때** 실패 |
| 2 | `schemas.py` | **Pydantic 스키마.** 요청(`ChatRequest`) · 응답(`ChatResponse`) · HITL UI(`ActionData`) |
| 3 | `sse.py` | gen-portal 이 읽는 SSE 프레임 형식. 선택 UI / 승인·거절 UI 를 `schemas` 로 만든다 |
| 4 | `llm.py` | LLM 스트리밍 호출. **tool_calls 조각을 모아 완성하는 부분이 핵심** |
| 5 | `rag.py` | 사전: 임베딩 → Weaviate 하이브리드 검색(파일명 필터) → 복호화 |
| 6 | `db.py` | 부동산: 테이블 스키마(프롬프트용) · **SQL 안전검사** · MySQL 실행 |
| 7 | `tools.py` | 툴 인자 Pydantic 모델 → JSON Schema(LLM 에 전달) / 검증(`validate`) / 실행(`run`) |
| 8 | `graph.py` | LangGraph 노드 4개·간선·interrupt 지점 |
| 9 | `router.py` | `/health`, `/chat`. 턴마다 그래프를 이어서 돌리고 HITL UI 를 붙인다 |
| 10 | `main.py` | FastAPI 앱 생성, 라우터 등록 |

## 툴 콜링이 실제로 흐르는 순서 (부동산)

```
call_llm 노드                                              llm.stream_chat
  │  messages(시스템 프롬프트에 테이블 스키마) + tools ─────▶  POST /v1/chat/completions (stream)
  │                                                         │
  │  ◀── {"type":"token","text":"조회해 보겠"} ────────────   delta.content    → 즉시 yield (사용자에게 흘러감)
  │                                                         delta.tool_calls 조각 ─┐
  │                                                         delta.tool_calls 조각 ─┼─▶ pending 버퍼에 이어붙임
  │                                                         delta.tool_calls 조각 ─┘  (아직 실행 안 함)
  │  ◀── {"type":"tool_calls","tool_calls":[{sql:...}]} ───   data: [DONE] → 완성본 한 번 yield
  │
  ├─▶ tools.validate()   Pydantic 으로 인자 검증 → db.sanitize() 로 SELECT 만 허용 + LIMIT 부착
  │                      실패하면 사유를 사용자에게 알리고 END (승인 UI 를 띄우지 않는다)
  └─▶ state.tool_calls = [정제된 SQL]   ← pending
        │
        └─ 승인 필요 ─▶ approve 앞에서 멈춤 → 승인 UI 에 **정제된 SQL 그대로** 표시
                          → (다음 HTTP 요청, 승인) → approve → run_tools: tools.run() → db.run_select()
                          → 결과 표를 사용자에게 보여주고 tool 메시지로 LLM 에 전달 → call_llm (해설)
```

사용자가 승인 UI 에서 본 SQL 과 실제 실행되는 SQL 이 **같은 문자열**이다. `validate` 가 정제한 값을
`state.tool_calls` 에 저장하고, 승인 UI 도 실행도 그 값을 읽는다.

## Pydantic 스키마가 쓰이는 곳

| 모델 | 어디서 | 왜 |
|---|---|---|
| `ChatRequest` | `router.chat(body: ChatRequest)` | genportal-api 가 보내는 body 검증. 모양이 다르면 FastAPI 가 422 로 거절 |
| `HumanInput` | `ChatRequest.humanInput` | `action` 은 `submit` / `cancel` 둘뿐임을 타입으로 고정 |
| `ChatResponse` | stream 없는 응답 | `{"code", "errMsg", "data": {"text"}}` 규약 |
| `ActionData` · `HitlElement` | `sse.py` | HITL UI 의 모양(element type, `interactionId` 위치)을 타입으로 문서화 |
| `SearchFinanceGlossaryArgs` · `QueryRealEstateSqlArgs` | `tools.py` | `model_json_schema()` 로 LLM 에 보내는 파라미터 스키마 생성, `model_validate_json()` 으로 LLM 출력 검증 |
| `ToolResult` | `tools.run()` → `graph.run_tools` | 툴 결과의 세 갈래(LLM 에 줄 텍스트 / 화면에 보일 텍스트 / 출처)를 명시 |
| `Settings` | `config.py` | 환경변수 타입 변환(`top_k: int`)과 필수값 검증 |

## 핵심 계약 5가지

**SSE 는 `data: ` 로 시작해야 한다.** genportal-api 파서가 그 외 줄을 전부 버린다.
워크플로우 API 문서의 `token: ` / `result: ` 형식은 Flowise 컨테이너 전용이라 여기서는 안 통한다.

```
data: {"event":"token","data":"안"}\n\n
data: {"event":"sourceDocuments","data":[{"pageContent":"...","metadata":{...}}]}\n\n
data: {"event":"action","data":{"interactionId":"...","elements":[...]}}\n\n
data: {"event":"end","data":null}\n\n
```

최종 답변은 genportal-api 가 `token` 을 누적해 만든다. `result` 이벤트는 없다.

**세션은 바디가 아니라 헤더로 온다.** 바디에는 `question`, `stream`, `humanInput` 뿐이고
HITL 응답 턴에는 `question` 이 빈 문자열이다. 나머지 문맥은 genportal-api 가 헤더로 붙이고,
게이트웨이가 화이트리스트로 골라 파드까지 전달한다 (`gateway-api/src/utils/workflow_client.py`).

| 헤더 | 내용 | 이 코드 |
|---|---|---|
| `x-genos-session-id` | 채팅 세션 ID | **쓴다.** LangGraph `thread_id` |
| `x-genos-user-id` | 사용자 ID | 안 씀 |
| `x-genos-group-id` | 그룹 ID | 안 씀 |
| `x-genos-is-genportal` | `"true"` — 채팅 앱 경유 표시 | 안 씀 |
| `x-genos-workflow-build-id` | 워크플로우 빌드 ID | 안 씀 |
| `x-genos-auth-key-id` | 인증키 ID (외부 인증키 호출일 때만) | 안 씀 |
| `x-genos-access-token` | 서비스 토큰 (게이트웨이 호출에 Bearer 가 있을 때만). 내부 API 호출 시 전달용 | 안 씀 |
| `traceparent` / `tracestate` | W3C 추적 컨텍스트 | 코드가 직접 읽진 않지만 **FastAPI 자동계측이 받아 트레이스를 잇는다** |
| `baggage` | OTel baggage | 게이트웨이가 지운다 (`AuthKeyBearer._SUBJECT_SCOPE_HEADERS`) |

`x-genos-session-id` 는 genportal-api → 게이트웨이 → 코드서빙 내부 구간에서만 유지된다.
외부 인증키로 직접 부르면 게이트웨이가 지우므로 매 요청이 새 대화가 된다.

`/chat` 은 받은 헤더를 **전부** 로그로 찍는다 (`router.log_headers`). 컨테이너 로그에서
`[total-example] 헤더` 로 찾으면 무엇이 실제로 파드까지 오는지 바로 보인다.

**게이트웨이는 `traceparent` 를 만들어 주지 않는다 — 흘려보내기만 한다.** 실제 헤더 덤프로 확인했다.
아무도 안 만들면 게이트웨이의 `code_serving` span(= admin「코드서빙 이용로그」)과 이 파드의
`llm`·`tool.*` span 이 **서로 다른 트레이스**로 갈라져, 이용로그를 눌러도 파드 안이 안 보인다.
그래서 호출자인 Vercel 프록시(`hyundaicapital_front/api/chat.js`)가 요청마다 `traceparent` 를 만들어 넣는다.
게이트웨이는 이 헤더를 스크럽하지 않으므로(`gateway-api/src/utils/http.py` `get_excluded_headers`) 파드까지 그대로 온다.

> 헤더에 `x-b3-traceid` 도 같이 오지만 Istio/Envoy 것이고 `x-b3-sampled: 0` 이다. Langfuse 트레이스와 무관하니 쓰지 말 것.

**HITL 요소는 종류마다 모양이 다르다** (`schemas.HitlElement`).

| 종류 | elements |
|---|---|
| 선택 | `single-select` 한 개. `component.title` 이 질문, `component.options` 가 선택지 |
| 승인·거절 | `approve-button` 과 `reject-button` **두 개**. 제목은 approve 쪽 `component.title`. 라벨은 FE 가 「답변 진행」/「답변 거절」로 넣는다 |

`interactionId` 는 element 안에 있어야 한다. 없으면 FE 가 렌더를 거부한다.
응답은 다음 요청 바디의 `humanInput: {interactionId, action: "submit"|"cancel", values}` 로 온다.

**스트리밍에서 `tool_calls` 는 조각으로 쪼개져 온다.** `index` 별로 `id`·`name` 을 채우고
`arguments` 문자열을 이어 붙여야 완성된다. 완성되기 전에는 실행할 수 없으니 버퍼(pending)에 둔다.

**노드 안의 토큰은 `writer` 로 흘린다.** `astream(stream_mode="custom")` 이 그걸 실시간으로
받아 SSE 로 바꾼다. 이 방식이라야 "LLM 이 SQL 제안 → 사용자 승인 → 실행" 순서가 지켜진다.

## SQL 안전검사 (`db.sanitize`)

LLM 이 만든 SQL 을 그대로 실행하므로 이 함수가 유일한 방어선이다.

| 검사 | 예 |
|---|---|
| 마크다운 코드블록 제거 | ` ```sql ... ``` ` → 알맹이만 |
| `SELECT` / `WITH` 로 시작하는지 | `DELETE FROM t` → 차단 |
| 세미콜론으로 문장 여러 개 | `SELECT 1; DROP TABLE t` → 차단 |
| 금지어 | `INSERT UPDATE DELETE DROP ALTER CREATE TRUNCATE GRANT OUTFILE SLEEP BENCHMARK ...` → 차단 |
| `LIMIT` 없으면 부착 | `... LIMIT 200` |

차단되면 승인 UI 를 띄우지 않고 사유를 사용자에게 보여준다. 그 위에 `read_timeout=30`,
`fetchmany(200)` 으로 응답 시간·크기도 막는다. **DB 계정 자체는 SELECT 권한만 있는 계정을 쓰는 게 맞다.**

## 복제본은 1로 둔다

`MemorySaver` 는 프로세스 메모리다. 복제본이 2개 이상이면 턴 1 을 받은 파드와 턴 2 를 받은
파드가 달라 대화를 못 찾는다. 코드서빙은 KEDA 오토스케일 대상이므로 리비전의 **복제본을 1**로,
`maxReplicaCount` 도 1로 둬야 한다.

복제본을 늘리려면 checkpointer 를 외부 저장소로 바꿔야 한다. 공식 `langgraph-checkpoint-redis` 는
쓸 수 없다 — `0.5.2` 는 `langgraph-checkpoint>=4.1.1` 을 요구해 `0.2.56` 과 충돌하고, 버전이 맞는
`0.1.0` 은 RediSearch 모듈이 필요한데 GenOS Redis 는 순정 `redis:7.4.6` 이다.

## Weaviate 는 HTTP GraphQL 로 부른다

`weaviate-client` v4 는 검색을 gRPC 로 보내서 `grpcio` 가 필요하다. GraphQL 은 `httpx` 만으로
되고 `httpx` 는 이미지에 이미 있다.

```graphql
{ Get { <Index>(
    hybrid: { query: "연장근로 한도", vector: [...], alpha: 0.5 }
    where:  { path: ["file_name"], operator: Like, valueText: "*경제금융용어*" }
    limit: 5
  ) { text file_name i_page chunk_bboxes is_encrypted _additional { score } } } }
```

GraphQL 은 '전체 선택'이 없어 필드를 다 적어야 한다. `GET /v1/schema/{Index}` 로 프로퍼티 목록을
받아 그대로 적는다. 하드코딩하면 뷰어가 쓰는 `chunk_bboxes`·`i_page` 를 빠뜨리기 쉽다.

## langgraph 0.2.56 함정 두 가지

- **노드가 `{}` 를 반환하면 `InvalidUpdateError`** 다. 모든 노드가 State 의 키를 최소 하나
  돌려줘야 한다. 그래서 알림만 하는 `approve` 가 `{"approved": ...}` 로 기존 값을 다시 쓴다.
- 동적 `interrupt()` 함수는 `0.2.57+` 에 들어왔다. 여기서는 모든 버전에서 안정적인
  `interrupt_before` 를 쓴다.

## 빌드 커맨드 vs 시작 커맨드

| | 빌드 (Build) | 시작 (Run) |
|---|---|---|
| 실행 스크립트 | `scripts/init.sh` | `scripts/entrypoint.sh` |
| 시점 | git clone 직후, **커밋당 한 번** | 매 부팅, 프로세스로 `exec` |
| 용도 | 의존성 설치 | 서버 실행 |
| 비웠을 때 | `requirements.txt` 있으면 `pip install -r` (사내 미러만 봄) | `main.py` 있으면 `uvicorn main:app --host 0.0.0.0 --port $PORT` |
| 실패하면 | 경고만 남기고 계속 진행 → import 에러로 죽는다. **로그를 봐야 원인이 보인다** | 컨테이너가 뜨지 않는다 |

`/app` 은 임시 파일시스템이라 빌드 marker(`/app/.init_done.<커밋해시>`)도 파드와 함께 사라진다.
빌드가 실패하면 **파드를 재시작하면 다시 시도**한다. 커밋을 새로 밀 필요는 없다.

## 배포 설정 (그대로 복사)

**빌드 커맨드** — `langgraph`·`cryptography` 의 의존성 일부가 사내 미러에 없어 공개 PyPI 를 함께 본다.

```
pip install --no-cache-dir --extra-index-url https://pypi.org/simple -r requirements.txt
```

**시작 커맨드** — 비밀이 아닌 설정을 환경변수로 앞에 붙인다.

```
GENOS_URL=https://genos.genon.ai LLM_SERVING_ID=1183 LLM_MODEL=GLM-5.3-Flash EMBEDDING_SERVING_ID=10 VDB_INDEX=H05e9c8d715b9478f86293fddde5407c0 GLOSSARY_FILE_FILTER=경제금융용어 DB_HOST=dwmyoung-mysql9.mysql.database.azure.com DB_USER=dwmyoung DB_NAME=db_template uvicorn main:app --host 0.0.0.0 --port $PORT
```

**환경 변수** — 토큰·비밀번호는 시작 커맨드가 아니라 이 행에 넣는다(암호화 저장된다).

| 변수명 | 값 |
|---|---|
| `LLM_BEARER_TOKEN` | LLM 서빙 인증 키 |
| `EMBEDDING_BEARER_TOKEN` | 임베딩 서빙 인증 키 |
| `WEAVIATE_API_KEY` | VDB 상세 → 인증 키 탭에서 발급 |
| `DB_PASSWORD` | MySQL 비밀번호 |

**복제본** 1

## 환경 변수 전체 (`config.py`)

| 이름 | 필수 | 설명 |
|---|---|---|
| `GENOS_URL` | O | 예 `https://genos.genon.ai`. 코드가 `/api/gateway/rep/serving/...` 를 붙인다 |
| `LLM_SERVING_ID` | O | LLM 모델서빙 ID |
| `LLM_BEARER_TOKEN` | O | 그 서빙의 인증 키 |
| `LLM_MODEL` | | 비우면 body 에 `model` 을 싣지 않는다 |
| `EMBEDDING_SERVING_ID` | O | 임베딩 모델서빙 ID |
| `EMBEDDING_BEARER_TOKEN` | O | 그 서빙의 인증 키 |
| `WEAVIATE_URL` | | 기본 `http://llmops-weaviate-service:8080` (클러스터 내부) |
| `WEAVIATE_API_KEY` | O | VDB 인증 키 (Read Key) |
| `VDB_INDEX` | O | VDB 컬렉션 이름 |
| `GLOSSARY_FILE_FILTER` | | 사전 검색 파일명 필터. 기본 `경제금융용어` |
| `TOP_K` | | 검색 문서 수. 기본 5 |
| `HYBRID_ALPHA` | | 하이브리드 가중치. 1=벡터만, 0=키워드만. 기본 0.5 |
| `DECRYPT_KEY` | | 암호화 적재 VDB 복호화 키(`G__ENCRYPT__KEY`). 기본 `mnc` |
| `DB_HOST` | O | MySQL 호스트. 포트는 3306 고정 |
| `DB_USER` | O | MySQL 사용자 |
| `DB_PASSWORD` | O | MySQL 비밀번호 |
| `DB_NAME` | O | 데이터베이스 이름 (테이블 `real_estate_transactions` 가 있어야 한다) |

필수값이 하나라도 비면 부팅 때 `pydantic_core.ValidationError` 로 죽고, 로그에 빠진 변수 이름이 나온다.

## 배포 순서

1. 배포 → 서빙 → 코드 서빙 → 생성. 저장소 유형 **외부 Git** 선택 후 URL·ID·PAT 입력
2. 이 레포를 그 저장소에 push
3. 리비전 추가 → 도커 이미지·인스턴스 타입 선택, **복제본 1** → 위 빌드·시작 커맨드와
   환경 변수 입력 → 배포 → 승인
4. 리비전 상세 → 컨테이너 서비스 → 「워크플로우로 사용」 → 엔드포인트 `/chat` → 검증 → 저장
5. 채팅 애플리케이션을 만들어 이 워크플로우를 연결

## 확인 순서

```bash
# 1) 검증 규약
curl -X POST $BASE/chat -H 'Content-Type: application/json' -d '{"question":"__verify__"}'
# → {"code":0,"errMsg":"success","data":{"text":"verified"}}

# 2) 살아 있나
curl $BASE/health          # → {"status":"ok"}

# 3) HITL 프레임
curl -N -X POST $BASE/chat -H 'Content-Type: application/json' \
  -d '{"question":"구별 평균 거래가 알려줘","stream":true}'
# → data: {"event":"action", ...} 한 줄 + data: {"event":"end", ...}
```

`$BASE` 는 `https://genos.genon.ai/api/gateway/code_serving/{code_serving_id}` 이고
`Authorization: Bearer {코드서빙 인증키}` 가 필요하다.

부팅 로그에 `[total-example] 준비 완료 ...` 가 찍히는지 먼저 본다.
`ValidationError` 가 찍히면 환경 변수가 빠진 것이다.

## 알려진 제약

- 코드서빙 파생 워크플로우는 VirtualService 가 만들어지지 않아
  `/api/gateway/workflow/{id}/run/v2` 로 **외부 호출이 안 된다.**
  `/api/gateway/code_serving/{id}/chat` 으로 호출하고, 채팅 앱 연동은 정상 동작한다.
- 데이터는 **부산광역시 2024년 2000행**뿐이다. 다른 지역·연도를 물으면 데이터가 없다고 답하도록
  프롬프트(`db.TABLE_SCHEMA`)에 명시해 뒀다.
- `file_name` 은 `kagome_kr` 토크나이즈라 `Like` 가 형태소 토큰 단위로 매칭된다.
- 어드민 「워크플로우 테스트」는 `stream` 을 안 보낸다. `action` 은 SSE 전용이라
  그 경로에서는 HITL 을 생략하고 일반 대화로 답한다.
- 외부 API 로 직접 호출하면 세션 헤더가 게이트웨이에서 제거되므로 매 요청이 새 대화가 된다.
  HITL 이어가기는 채팅 앱 경유로 확인한다.
- 새로고침하면 선택·승인 UI 가 복원되지 않는다. 코드서빙이 직접 낸 `action` 은 라이브 스트림에만 있다.
