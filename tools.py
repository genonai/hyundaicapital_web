"""툴 — LLM 에게 보여줄 스키마(TOOLS)와, LLM 이 고른 툴을 검증(validate)하고 실행(run)하는 코드.

  search_finance_glossary(query)  경제금융용어 사전 에이전트. Filter RAG  → rag.py
  query_real_estate_sql(sql)    부동산 에이전트. Text2SQL (MySQL)      → db.py   ← 실행 전 사용자 승인

인자 모양은 Pydantic 모델 하나로 정의하고 두 곳에서 쓴다.
  · Model.model_json_schema()      → LLM 에 보내는 tools[].function.parameters (JSON Schema)
  · Model.model_validate_json()    → LLM 이 문자열로 준 arguments 검증

흐름: LLM 이 tool_call 을 만든다 → validate 로 검증·정규화 → (승인) → run 으로 실행
"""
import asyncio

from pydantic import BaseModel, Field, ValidationError

import db
import otel
import rag
from config import settings


# ───────────────────────── 툴 인자 (LLM 이 채운다) ─────────────────────────
class SearchFinanceGlossaryArgs(BaseModel):
    query: str = Field(description="검색할 경제·금융 용어. 용어·개념이 드러나게 쓴다.")


class QueryRealEstateSqlArgs(BaseModel):
    sql: str = Field(description="실행할 MySQL SELECT 문 하나. 세미콜론·주석·마크다운 코드블록 없이.")


# ───────────────────────── LLM 에 보내는 툴 스키마 ─────────────────────────
TOOLS = {
    "search_finance_glossary": {
        "type": "function",
        "function": {
            "name": "search_finance_glossary",
            "description": f"경제금융용어 사전을 검색한다. 파일명에 '{settings.glossary_file_filter}' 가 들어간 문서만 본다.",
            "parameters": SearchFinanceGlossaryArgs.model_json_schema(),
        },
    },
    "query_real_estate_sql": {
        "type": "function",
        "function": {
            "name": "query_real_estate_sql",
            # ⚠ 여기에 "실행 전 사용자 승인을 받는다" 같은 말을 쓰면 안 된다. 모델이 그걸 읽고
            #   툴을 부르는 대신 "실행해도 될까요?" 라고 텍스트로 물어버린다. 승인은 시스템이 받는다.
            "description": "부산 부동산 실거래 테이블(real_estate_transactions)에 SELECT 쿼리를 실행하고 결과를 돌려준다.",
            "parameters": QueryRealEstateSqlArgs.model_json_schema(),
        },
    },
}

# 툴 이름 → 인자 모델
ARGS_MODEL = {
    "search_finance_glossary": SearchFinanceGlossaryArgs,
    "query_real_estate_sql": QueryRealEstateSqlArgs,
}

# 실행 전에 사용자 승인(HITL) 을 받아야 하는 툴
NEEDS_APPROVAL = {"query_real_estate_sql"}


class ToolResult(BaseModel):
    content: str                  # LLM 에 돌려줄 텍스트 → tool 메시지의 content
    display: str = ""             # 사용자 화면에 보여줄 텍스트 → token 이벤트
    documents: list[dict] = []    # 출처 → sourceDocuments 이벤트


# ───────────────────────────── 검증 ─────────────────────────────
def validate(tool_call: dict) -> str:
    """LLM 이 만든 arguments 를 검증하고 정규화한 JSON 문자열을 돌려준다. 문제가 있으면 ValueError.
    SQL 은 여기서 조회 전용 검사 + LIMIT 부착까지 끝낸다. 승인 UI 에 뜨는 SQL 이 바로 이 값이다."""
    name = tool_call["function"]["name"]
    arguments = tool_call["function"]["arguments"]
    if name not in ARGS_MODEL:
        raise ValueError(f"알 수 없는 툴: {name}")

    # 1) 인자 JSON 이 스키마와 맞는가 (Pydantic)
    try:
        args = ARGS_MODEL[name].model_validate_json(arguments)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ValueError(f"인자가 스키마와 다릅니다 — {'.'.join(map(str, first['loc'])) or '(전체)'}: {first['msg']}") from exc

    # 2) SQL 은 조회 전용인지 검사하고 LIMIT 을 붙인다
    if name == "query_real_estate_sql":
        args.sql = db.sanitize(args.sql)

    return args.model_dump_json()


def describe(tool_call: dict) -> str:
    """승인 UI 에 보여줄 문구."""
    name = tool_call["function"]["name"]
    args = ARGS_MODEL[name].model_validate_json(tool_call["function"]["arguments"])
    if name == "query_real_estate_sql":
        return "실행할 SQL:\n" + args.sql
    return f"{name}({args.model_dump_json()})"


# ───────────────────────────── 실행 ─────────────────────────────
async def run(tool_call: dict, security_level: int) -> ToolResult:
    """검증을 통과한(그리고 필요하면 승인받은) tool_call 하나를 실행한다.

    security_level 은 로그인한 사용자의 보안 등급이다. 문서 검색은 이 등급 **이하**만 본다.
    기본값을 두지 않는다 — 빠뜨린 호출이 조용히 전 등급을 검색하는 것이 최악이다."""
    name = tool_call["function"]["name"]
    args = ARGS_MODEL[name].model_validate_json(tool_call["function"]["arguments"])

    # Weaviate(gRPC)·pymysql 은 자동 계측 대상이 아니라서, 이 span 이 없으면 검색·SQL 이
    # 몇 초 걸렸는지 아무 데도 안 남는다. 원문류(query·SQL)는 전부 collect_io() 뒤에 둔다.
    with otel.span(f"tool.{name}", {
        "langfuse.observation.type": "tool",
        "langfuse.observation.metadata.security_level": security_level,
        "langfuse.observation.input": args.model_dump_json() if otel.collect_io() else None,
    }) as sp:
        if name == "search_finance_glossary":
            docs = await rag.hybrid_search(args.query, settings.glossary_file_filter, settings.top_k,
                                           security_level)
            otel.set_attrs(sp, {"langfuse.observation.metadata.doc_count": len(docs)})
            return ToolResult(
                content=rag.docs_to_text(docs),
                display=f"🔎 경제금융용어 사전 검색: {args.query}\n\n",
                documents=docs,
            )

        if name == "query_real_estate_sql":
            rows = await asyncio.to_thread(db.run_select, args.sql)     # pymysql 은 동기 → 스레드에서 돈다
            table = db.rows_to_markdown(rows)
            otel.set_attrs(sp, {
                "langfuse.observation.metadata.row_count": len(rows),
                "langfuse.observation.metadata.sql": args.sql if otel.collect_io() else None,
            })
            return ToolResult(
                content=f"실행한 SQL:\n{args.sql}\n\n결과 ({len(rows)}행):\n{table}",
                display=f"📊 {len(rows)}행 조회\n\n{table}\n\n",
            )

        raise ValueError(f"알 수 없는 툴: {name}")
