"""LangGraph — 에이전트 선택(HITL) → LLM ↔ 툴 루프 → (부동산은 SQL 실행 전 승인 HITL).

    START ─▶ pick ─▶ call_llm ──┬─ tool_calls 없음 ────────────────────────────▶ END
                        ▲       │
                        │       ├─ tool_calls 있음 · 승인 불필요(법률 검색) ─▶ run_tools ─┐
                        │       │                                                        │
                        │       └─ tool_calls 있음 · 승인 필요(SQL 실행) ─▶ approve ─┬─▶ run_tools ─┤
                        │                                                          └─▶ END (거절)  │
                        └─────────── 툴 결과를 messages 에 넣고 LLM 을 다시 부른다 ───────────────┘

    interrupt_before = ["pick", "approve"]   ← 이 두 노드 **앞에서** 멈춰 사용자 입력을 기다린다

노드 네 개
  pick        사용자가 고른 에이전트를 반영하고 대화(messages) 를 시작한다
  call_llm    LLM 을 한 번 부른다. 텍스트는 바로 흘리고, tool_calls 는 검증해서 state.tool_calls(=pending) 에 담는다
  approve     승인/거절 결과를 알린다 (판단은 사용자가, 반영은 router 가 update_state 로 한다)
  run_tools   pending 툴을 실행하고 결과를 messages 에 tool 메시지로 넣는다 → 다시 call_llm

왜 checkpointer 가 필요한가
  HITL 응답은 **다음 HTTP 요청** 으로 온다. 그 사이 상태(질문·messages·pending 툴) 를 어딘가에
  남겨 둬야 멈춘 노드부터 이어갈 수 있다. thread_id = 채팅 세션 ID 로 저장한다.
  ⚠ MemorySaver 는 프로세스 메모리다. 코드서빙 복제본을 **1 로 고정**해야 한다.
"""
from typing import TypedDict

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import StreamWriter

import db
import llm
import tools

MAX_LLM_CALLS = 4    # LLM ↔ 툴 왕복 상한. LLM 이 툴만 계속 부르는 무한 루프를 막는다.

# 에이전트 정의. 선택 UI(sse.py) 의 선택지도, 시스템 프롬프트도, 쓸 수 있는 툴도 여기서 나온다.
AGENTS = {
    "legal": {
        "label": "법률 (노동법)",
        "desc": "노동법 문서를 검색해 근거와 함께 답합니다",
        "system": "당신은 노동법 전문가입니다. 질문을 받으면 먼저 search_labor_law 툴을 **즉시 호출**해 문서를 찾고, "
                  "그 내용만 근거로 답하세요. 검색할지 사용자에게 묻지 마세요. 문서에 없으면 모른다고 답하세요.",
        "tools": ["search_labor_law"],
    },
    "estate": {
        "label": "부동산 분석 (부산시)",
        "desc": "질문을 SQL 로 바꿔 보여드리고, 승인 후 실행해 분석합니다",
        # ⚠ "바로 툴을 호출하라. 묻지 마라" 를 명시해야 한다. 승인은 시스템(approve 노드)이 받는 것이라
        #   모델이 텍스트로 "실행해도 될까요?" 하고 물으면 tool_calls 가 비어 승인 UI 자체가 뜨지 않는다.
        "system": "당신은 부산시 부동산 데이터 분석가입니다. 아래 테이블을 대상으로 사용자 질문에 답하는 "
                  "MySQL SELECT 문 하나를 만들어 query_real_estate_sql 툴을 **즉시 호출**하세요. "
                  "SQL 을 텍스트로 보여주거나 실행 여부를 사용자에게 묻지 마세요 — 실행 승인은 시스템이 별도로 받습니다. "
                  "집계 결과에는 읽기 쉬운 별칭(AS)을 붙이고, 결과가 많을 수 있으면 ORDER BY 와 LIMIT 을 넣으세요. "
                  "툴 결과를 받은 뒤에는 그 결과를 근거로만 답하고, 금액은 단위(만원)를 붙여 읽기 쉽게 쓰세요.\n\n" + db.TABLE_SCHEMA,
        "tools": ["query_real_estate_sql"],
    },
    "chat": {
        "label": "일반 대화",
        "desc": "검색·조회 없이 바로 답합니다",
        "system": "당신은 친절한 상담원입니다. 편하고 자연스럽게 대화하세요.",
        "tools": [],
    },
}


class State(TypedDict):
    question: str      # 사용자 질문. HITL 응답 턴엔 body 에 question 이 안 오므로 여기 저장해 둔다
    agent: str         # "legal" | "estate" | "chat".  pick 앞에서 멈춘 뒤 router 가 채운다
    messages: list     # OpenAI 형식 대화 기록 [system, user, assistant, tool, assistant, ...]
    tool_calls: list   # LLM 이 제안했고 검증은 끝났지만 **아직 실행하지 않은** 툴 호출 = pending
    approved: bool     # approve 앞에서 멈춘 뒤 router 가 채운다
    llm_calls: int     # call_llm 을 몇 번 돌았는지


# ───────────────────────────── 노드 ─────────────────────────────
def pick(state: State, writer: StreamWriter) -> dict:
    """[interrupt_before] 사용자가 에이전트를 고를 때까지 이 노드 **앞에서** 멈춘다.
    재개되면 state["agent"] 가 채워져 있다. 그걸로 대화를 시작한다."""
    agent = AGENTS[state["agent"]]
    writer({"event": "token", "data": f"「{agent['label']}」 에이전트로 진행합니다.\n\n"})
    messages = [
        {"role": "system", "content": agent["system"]},
        {"role": "user", "content": state["question"]},
    ]
    return {"messages": messages}


async def call_llm(state: State, writer: StreamWriter) -> dict:
    """LLM 을 한 번 부른다. 텍스트 조각은 writer 로 바로 흘리고, 툴 호출은 검증해서 pending 에 담는다."""
    agent = AGENTS[state["agent"]]
    tool_schemas = [tools.TOOLS[name] for name in agent["tools"]] or None

    # 1) 스트리밍. 텍스트는 즉시 밖으로, tool_calls 는 스트림 끝에 완성본이 한 번 온다.
    answer_text = ""
    tool_calls = []
    async for event in llm.stream_chat(state["messages"], tool_schemas):
        if event["type"] == "token":
            answer_text += event["text"]
            writer({"event": "token", "data": event["text"]})      # → router 가 SSE 로 내보낸다
        elif event["type"] == "tool_calls":
            tool_calls = event["tool_calls"]

    # 2) LLM 이 만든 인자를 검증한다 (Pydantic). SQL 은 조회 전용 검사 + LIMIT 부착까지.
    #    통과하지 못한 호출은 버리고 사유를 사용자에게 알린다 → 승인 UI 없이 그냥 끝난다.
    valid_tool_calls = []
    for tool_call in tool_calls:
        try:
            tool_call["function"]["arguments"] = tools.validate(tool_call)
            valid_tool_calls.append(tool_call)
        except ValueError as exc:
            writer({"event": "token",
                    "data": f"\n\n⚠ `{tool_call['function']['name']}` 호출을 실행할 수 없습니다: {exc}\n"})

    # 3) LLM 의 이번 답을 대화 기록에 남긴다. 툴을 불렀으면 tool_calls 도 같이 (OpenAI 규격).
    assistant_message = {"role": "assistant", "content": answer_text}
    if valid_tool_calls:
        assistant_message["tool_calls"] = valid_tool_calls

    return {
        "messages": state["messages"] + [assistant_message],
        "tool_calls": valid_tool_calls,        # 비어 있으면 최종 답변까지 끝난 것
        "llm_calls": state["llm_calls"] + 1,
    }


def approve(state: State, writer: StreamWriter) -> dict:
    """[interrupt_before] 사용자가 승인/거절할 때까지 이 노드 **앞에서** 멈춘다.
    재개되면 state["approved"] 가 채워져 있다. 결과를 알리기만 한다."""
    if state["approved"]:
        writer({"event": "token", "data": "승인되었습니다. 실행합니다.\n\n"})
    else:
        writer({"event": "token", "data": "거절되었습니다. 실행하지 않았습니다."})
    return {"approved": state["approved"]}    # 0.2.56 은 빈 dict 반환을 거부해서 그대로 되돌려준다


async def run_tools(state: State, writer: StreamWriter) -> dict:
    """pending 툴을 하나씩 실행하고 결과를 tool 메시지로 넣는다. 다음은 다시 call_llm 이다."""
    messages = list(state["messages"])
    for tool_call in state["tool_calls"]:
        result = await tools.run(tool_call)

        if result.display:
            writer({"event": "token", "data": result.display})               # 사용자 화면
        if result.documents:
            writer({"event": "sourceDocuments", "data": result.documents})   # 채팅창 하단 「출처」
        messages.append({
            "role": "tool",
            "tool_call_id": tool_call["id"],                                  # 어느 호출의 결과인지 짝을 맞춘다
            "content": result.content,                                        # LLM 이 읽는 부분
        })
    return {"messages": messages, "tool_calls": []}                           # 실행했으니 pending 을 비운다


# ─────────────────────────── 분기 조건 ───────────────────────────
def after_call_llm(state: State) -> str:
    if not state["tool_calls"]:
        return END                       # 툴 호출 없음 = 최종 답변까지 다 흘렸다
    if state["llm_calls"] >= MAX_LLM_CALLS:
        return END                       # 안전장치
    for tool_call in state["tool_calls"]:
        if tool_call["function"]["name"] in tools.NEEDS_APPROVAL:
            return "approve"             # 승인이 필요한 툴이 하나라도 있으면 멈춘다
    return "run_tools"


def after_approve(state: State) -> str:
    return "run_tools" if state["approved"] else END


# ───────────────────────────── 조립 ─────────────────────────────
builder = StateGraph(State)
builder.add_node("pick", pick)
builder.add_node("call_llm", call_llm)
builder.add_node("approve", approve)
builder.add_node("run_tools", run_tools)

builder.add_edge(START, "pick")
builder.add_edge("pick", "call_llm")
builder.add_conditional_edges("call_llm", after_call_llm,
                              {"approve": "approve", "run_tools": "run_tools", END: END})
builder.add_conditional_edges("approve", after_approve, {"run_tools": "run_tools", END: END})
builder.add_edge("run_tools", "call_llm")

# interrupt_before 는 langgraph 0.1 부터 있는 안정 API 다. 동적 interrupt() 는 0.2.57+ 라 안 쓴다.
GRAPH = builder.compile(checkpointer=MemorySaver(), interrupt_before=["pick", "approve"])
