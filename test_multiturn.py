"""run_turn 멀티턴 self-check — LLM·DB·VDB 없이 그래프 재개 경로만 본다.
실행: python test_multiturn.py   (성공하면 "OK" 만 찍는다)"""
import asyncio, os, sys, types

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# config/db/rag/tools 는 실제 자격증명을 요구하므로 가짜로 채운다.
for k, v in dict(GENOS_URL="x", LLM_SERVING_ID="1", LLM_BEARER_TOKEN="t", EMBEDDING_SERVING_ID="1",
                 EMBEDDING_BEARER_TOKEN="t", WEAVIATE_API_KEY="k", VDB_INDEX="i",
                 DB_HOST="h", DB_USER="u", DB_PASSWORD="p", DB_NAME="n").items():
    os.environ.setdefault(k, v)

stub_db = types.ModuleType("db"); stub_db.TABLE_SCHEMA = ""
sys.modules["db"] = stub_db
stub_tools = types.ModuleType("tools")
stub_tools.TOOLS = {}; stub_tools.NEEDS_APPROVAL = set()
stub_tools.validate = lambda tc: tc; stub_tools.run = None
sys.modules["tools"] = stub_tools

stub_llm = types.ModuleType("llm")
seen = []            # call_llm 이 매번 받은 messages 를 기록한다


async def stream_chat(messages, tool_schemas=None):
    seen.append(list(messages))
    yield {"type": "token", "text": "답"}

stub_llm.stream_chat = stream_chat
sys.modules["llm"] = stub_llm

import router
from schemas import ChatRequest, HumanInput, HumanInputValues


async def turn(session, **body):
    return [f async for f in router.run_turn(session, ChatRequest(stream=True, **body))]


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

    other = [f async for f in router.run_turn("sess-2", ChatRequest(stream=True, question="딴 세션"))]
    assert any('"action"' in f for f in other), "새 세션은 에이전트 선택부터 시작해야 한다"
    print("OK")

asyncio.run(main())
