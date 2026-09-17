"""GenOS 코드서빙 진입점. 컨테이너가 `uvicorn main:app --host 0.0.0.0 --port 8080` 으로 띄운다.

파일 읽는 순서 (추천)
    config.py    환경변수 (pydantic-settings)
    schemas.py   요청·응답·HITL UI 스키마 (Pydantic)
    sse.py       gen-portal 이 읽는 SSE 프레임 형식
    llm.py       LLM 스트리밍 호출 + tool_calls 조각 모으기
    rag.py       사전:   임베딩 → Weaviate 하이브리드 검색(파일명 필터) → 복호화
    db.py        부동산: 테이블 스키마, SQL 안전검사, MySQL 실행
    tools.py     툴 스키마(Pydantic → JSON Schema) + 검증 + 실행
    graph.py     LangGraph 노드·간선, interrupt 지점
    router.py    /health, /chat, 턴 처리
    otel.py      파드 안에서 벌어진 일을 Langfuse 로 보낸다 (OTEL_ENABLED=true 일 때만)
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

import otel
from config import settings
from router import router


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    otel.shutdown()      # 아직 안 보낸 span 을 내보내고 내려간다


app = FastAPI(title="total-example", lifespan=lifespan)
app.include_router(router)
otel.init(app)           # ⚠ 미들웨어를 붙이므로 서버가 뜨기 전에 불러야 한다

print(f"[total-example] 준비 완료  LLM={settings.llm_serving_id}  VDB={settings.vdb_index}  "
      f"DB={settings.db_name}@{settings.db_host}", flush=True)
