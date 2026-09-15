"""HTTP 엔드포인트 — 코드서빙 계약이 여기 있다. 요청·응답 모양은 schemas.py 참고.

GET  /health   startupProbe 가 부른다. 없으면 배포가 실패한다. LLM·DB 를 부르면 안 된다.
POST /chat     「컨테이너 서비스 > 워크플로우로 사용」 에 등록하는 경로.

body 는 question / stream / humanInput 셋이 전부고, 나머지 문맥은 **헤더** 로 온다.
genportal-api 가 붙인 헤더를 게이트웨이가 화이트리스트로 골라 파드까지 전달한다
(gateway-api/src/utils/workflow_client.py WorkflowClient.__init__). 파드에 도착하는 것:

    x-genos-session-id         채팅 세션 ID        ★ 이 코드가 쓰는 유일한 헤더. LangGraph thread_id 가 된다
    x-genos-user-id            사용자 ID           안 씀. 사용자별 처리가 필요하면 Header(alias=...) 로 받으면 된다
    x-genos-group-id           그룹 ID             안 씀
    x-genos-is-genportal       "true"              안 씀. 채팅 앱 경유 호출인지 구분할 때 쓴다
    x-genos-workflow-build-id  워크플로우 빌드 ID  안 씀
    x-genos-auth-key-id        인증키 ID           안 씀. 외부 인증키로 직접 호출했을 때만 있다
    x-genos-access-token       서비스 토큰         안 씀. 게이트웨이 호출에 Bearer 가 있었을 때만 있다.
                                                   코드서빙 안에서 GenOS 내부 API 를 부를 때 그대로 넘기는 용도
    traceparent / baggage      OTel 추적           안 씀. aiohttp 계측이 자동으로 붙인다

    ⚠ 위 목록은 소스(GenOS 2026-05 기준)에서 읽은 것이다. 버전에 따라 x-genos-chat-service-id 가 추가되는 등
      달라질 수 있어서, /chat 이 실제로 받은 x-genos-* 헤더를 매 요청 로그로 찍는다 (log_genos_headers).
      배포 후 앱 채팅에서 한 번 보내고 컨테이너 로그의 `[total-example] 헤더` 줄을 보면 확정된다.
    ⚠ x-genos-session-id 는 genportal-api → 게이트웨이 → 코드서빙 내부 구간에서만 유지된다.
      외부 인증키로 직접 부르면 게이트웨이가 subject 헤더를 지우므로 매 요청이 새 대화가 된다.

한 대화의 흐름 (부동산)
    턴1  질문          → 그래프가 pick 앞에서 멈춤                         → 에이전트 선택 UI
    턴2  "estate" 선택 → pick → call_llm(SQL 생성) → approve 앞에서 멈춤    → SQL 승인 UI
    턴3  승인          → approve → run_tools(SQL 실행) → call_llm(해설) → END
         거절          → approve → END
"""
import uuid

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

import llm
from graph import AGENTS, GRAPH
from schemas import ChatRequest, ChatResponse, ChatResponseData
from sse import agent_select_frame, approve_frame, sse

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.post("/chat")
async def chat(body: ChatRequest, request: Request,
               session_id: str | None = Header(default=None, alias="x-genos-session-id")):
    log_genos_headers(request)
    question = body.question.strip()

    # ③ 「검증」 버튼. 이 응답만 보고 통과 여부를 정한다.
    if question == "__verify__":
        return ChatResponse(data=ChatResponseData(text="verified"))

    # 어드민 「워크플로우 테스트」 는 stream 을 안 보낸다. action(HITL) 은 SSE 전용이므로
    # 이 경로에서는 에이전트 선택 없이 일반 대화로 바로 답한다.
    if not body.stream:
        try:
            messages = [{"role": "system", "content": AGENTS["chat"]["system"]},
                        {"role": "user", "content": question}]
            text = ""
            async for event in llm.stream_chat(messages):
                if event["type"] == "token":
                    text += event["text"]
            return ChatResponse(data=ChatResponseData(text=text))
        except Exception as exc:
            return ChatResponse(code=1, errMsg=str(exc), data=ChatResponseData(text=""))

    # ①② 스트리밍. 세션 ID = 헤더. 외부에서 직접 부르면 헤더가 없으니 그때만 새로 만든다.
    return StreamingResponse(
        run_turn(session_id or str(uuid.uuid4()), body),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},   # 앞단 nginx 가 SSE 를 모아 한 번에 보내는 것을 막는다
    )


def log_genos_headers(request: Request) -> None:
    """이 요청에 실려 온 x-genos-* 헤더를 로그에 남긴다 — 위 목록이 실제로 파드까지 오는지 눈으로 확인하는 용도.
    토큰류(access-token, authorization)는 값 대신 길이만 찍는다. 컨테이너 로그에서 `[total-example] 헤더` 로 찾는다."""
    seen = {}
    for name, value in request.headers.items():
        if name.startswith("x-genos-") or name in ("traceparent", "baggage", "authorization"):
            seen[name] = f"<{len(value)}자, 값 생략>" if ("token" in name or name == "authorization") else value
    print(f"[total-example] 헤더 {seen}", flush=True)


async def run_turn(session_id: str, body: ChatRequest):
    """한 턴 = HTTP 요청 하나. 그래프를 (이어서) 돌리고 SSE 프레임을 yield 한다."""
    thread = {"configurable": {"thread_id": session_id}}    # 이 세션의 체크포인트를 가리킨다
    human_input = body.humanInput

    try:
        # ── 1. 그래프에 무엇을 넣을지 정한다 ─────────────────────────────────────
        if human_input is None:
            # ① 새 질문: 상태를 처음부터 만든다. (같은 세션의 이전 대화는 덮어쓴다)
            graph_input = {"question": body.question.strip(), "agent": "", "messages": [],
                           "tool_calls": [], "approved": False, "llm_calls": 0}
        else:
            # ② HITL 응답: 어느 노드 앞에서 멈춰 있었는지 보고, 사용자 입력을 채워 넣는다.
            snapshot = await GRAPH.aget_state(thread)
            stopped_before = snapshot.next[0] if snapshot.next else None

            if stopped_before is None:
                yield sse("token", "이전 대화를 찾을 수 없습니다. 질문을 다시 입력해 주세요.")
                yield sse("end", None)
                return

            if stopped_before == "pick":
                if human_input.action == "cancel":
                    yield sse("token", "에이전트 선택을 취소했습니다.")
                    yield sse("end", None)
                    return
                selected = human_input.values.selected
                agent = selected[0] if selected and selected[0] in AGENTS else "chat"
                await GRAPH.aupdate_state(thread, {"agent": agent})

            elif stopped_before == "approve":
                await GRAPH.aupdate_state(thread, {"approved": human_input.action == "submit"})

            graph_input = None    # None = 새 입력 없이, 멈춘 지점부터 이어서 실행한다

        # ── 2. 그래프 실행. 노드가 writer 로 내보낸 이벤트를 SSE 로 바꿔 그대로 흘린다 ──
        #      다음 interrupt 지점 앞에서 멈추거나 END 에 닿으면 이 루프가 끝난다.
        async for event in GRAPH.astream(graph_input, thread, stream_mode="custom"):
            yield sse(event["event"], event["data"])

        # ── 3. 어디서 멈췄는지 보고 HITL UI 를 붙인다 ─────────────────────────────
        snapshot = await GRAPH.aget_state(thread)
        stopped_before = snapshot.next[0] if snapshot.next else None
        if stopped_before == "pick":
            yield agent_select_frame()
        elif stopped_before == "approve":
            yield approve_frame(snapshot.values["tool_calls"])

    except Exception as exc:
        yield sse("error", f"{type(exc).__name__}: {exc}")

    yield sse("end", None)
