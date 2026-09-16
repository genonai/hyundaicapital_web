"""self-check — LLM·DB·VDB 없이 그래프 재개 경로와 보안 등급 전달을 본다.
실행: python test_multiturn.py   (성공하면 "OK" 만 찍는다)

  ① 같은 세션의 후속 질문이 이전 대화를 이어받는가 (멀티턴)
  ② 다른 세션은 새 대화로 시작하는가
  ③ 헤더의 보안 등급이 rag.hybrid_search 까지 그대로 도달하는가
  ④ 보안 등급 헤더가 없으면 최저 등급(0)으로 막는가 (fail-closed)"""
import asyncio, os, sys, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# config/db/rag/tools 는 실제 자격증명을 요구하므로 가짜로 채운다.
for k, v in dict(GENOS_URL="x", LLM_SERVING_ID="1", LLM_BEARER_TOKEN="t", EMBEDDING_SERVING_ID="1",
                 EMBEDDING_BEARER_TOKEN="t", WEAVIATE_API_KEY="k", VDB_INDEX="i",
                 DB_HOST="h", DB_USER="u", DB_PASSWORD="p", DB_NAME="n").items():
    os.environ.setdefault(k, v)

stub_db = types.ModuleType("db"); stub_db.TABLE_SCHEMA = ""
sys.modules["db"] = stub_db
searched = []        # rag.hybrid_search 가 받은 (검색어, 보안등급)


class _ToolResult:
    content = "결과"; display = ""; documents = []


stub_tools = types.ModuleType("tools")
stub_tools.TOOLS = {"search_finance_glossary": {}}; stub_tools.NEEDS_APPROVAL = set()
stub_tools.validate = lambda tc: tc


async def tool_run(tool_call, security_level):
    searched.append((tool_call["function"]["name"], security_level))
    return _ToolResult()


stub_tools.run = tool_run
sys.modules["tools"] = stub_tools

stub_llm = types.ModuleType("llm")
seen = []            # call_llm 이 매번 받은 messages 를 기록한다


call_tool_once = []   # 비우면 툴을 부르지 않는다


async def stream_chat(messages, tool_schemas=None):
    seen.append(list(messages))
    yield {"type": "token", "text": "답"}
    if call_tool_once:
        yield {"type": "tool_calls", "tool_calls": [call_tool_once.pop()]}

stub_llm.stream_chat = stream_chat
sys.modules["llm"] = stub_llm

import router
from schemas import ChatRequest, HumanInput, HumanInputValues


async def turn(session, level=6, **body):
    return [f async for f in router.run_turn(session, ChatRequest(stream=True, **body), level)]


async def main():
    s = "sess-1"
    await turn(s, question="첫 질문")                                   # 턴1 → pick 앞에서 멈춤
    await turn(s, question="", humanInput=HumanInput(                   # 턴2 → 에이전트 선택
        action="submit", values=HumanInputValues(selected=["chat"])))
    assert seen, "call_llm 이 실행되지 않았다"
    assert [m["content"] for m in seen[-1] if m["role"] == "user"] == ["첫 질문"], seen[-1]

    await turn(s, question="후속 질문")                                  # 턴3 → 후속 질문
    users = [m["content"] for m in seen[-1] if m["role"] == "user"]
    assert users == ["첫 질문", "후속 질문"], f"이전 대화가 끊겼다: {users}"
    assert len(seen) == 2, f"후속 질문에서 pick 이 다시 걸렸다: {len(seen)}"

    other = [f async for f in router.run_turn("sess-2", ChatRequest(stream=True, question="딴 세션"), 6)]
    assert any('"action"' in f for f in other), "새 세션은 에이전트 선택부터 시작해야 한다"

    # ③ 헤더 등급이 rag 검색까지 도달하는가 — 툴을 한 번 부르게 만든다
    call_tool_once.append({"id": "c1", "function": {"name": "search_finance_glossary", "arguments": "{}"}})
    await turn("sess-3", level=3, question="등급 확인")
    await turn("sess-3", level=3, question="", humanInput=HumanInput(
        action="submit", values=HumanInputValues(selected=["finance"])))
    assert searched == [("search_finance_glossary", 3)], f"등급이 전달되지 않았다: {searched}"

    # ④ 헤더 없이 들어온 요청은 최저 등급으로 검색해야 한다 (헤더를 빼서 통제를 푸는 것 방지)
    searched.clear()
    call_tool_once.append({"id": "c2", "function": {"name": "search_finance_glossary", "arguments": "{}"}})
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI(); app.include_router(router.router)
    client = TestClient(app)
    client.post("/chat", json={"question": "등급 헤더 없음", "stream": True},
                headers={"x-hc-session-id": "sess-4"})
    client.post("/chat", headers={"x-hc-session-id": "sess-4"},
                json={"question": "", "stream": True,
                      "humanInput": {"action": "submit", "values": {"selected": ["finance"]}}})
    assert searched == [("search_finance_glossary", router.MIN_SECURITY_LEVEL)], \
        f"헤더가 없으면 최저 등급이어야 한다: {searched}"

    print("OK")

asyncio.run(main())
