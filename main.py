"""GenOS 코드서빙 진입점. 컨테이너가 `uvicorn main:app --host 0.0.0.0 --port 8080` 으로 띄운다.

파일 읽는 순서 (추천)
    config.py    환경변수 (pydantic-settings)
    schemas.py   요청·응답·HITL UI 스키마 (Pydantic)
    sse.py       gen-portal 이 읽는 SSE 프레임 형식
    llm.py       LLM 스트리밍 호출 + tool_calls 조각 모으기
    rag.py       법률:   임베딩 → Weaviate 하이브리드 검색(파일명 필터) → 복호화
    db.py        부동산: 테이블 스키마, SQL 안전검사, MySQL 실행
    tools.py     툴 스키마(Pydantic → JSON Schema) + 검증 + 실행
    graph.py     LangGraph 노드·간선, interrupt 지점
    router.py    /health, /chat, 턴 처리
"""
from fastapi import FastAPI

from config import settings
from router import router

app = FastAPI(title="total-example")
app.include_router(router)

print(f"[total-example] 준비 완료  LLM={settings.llm_serving_id}  VDB={settings.vdb_index}  "
      f"DB={settings.db_name}@{settings.db_host}", flush=True)
