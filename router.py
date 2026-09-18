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
      달라질 수 있어서, /chat 이 실제로 받은 헤더를 **전부** 매 요청 로그로 찍는다 (log_headers).
      배포 후 앱 채팅에서 한 번 보내고 컨테이너 로그의 `[total-example] 헤더` 줄을 보면 확정된다.
    ⚠ x-genos-session-id 는 genportal-api → 게이트웨이 → 코드서빙 내부 구간에서만 유지된다.
      외부 인증키로 직접 부르면 게이트웨이(AuthKeyBearer._SUBJECT_SCOPE_HEADERS)가 subject 헤더를
      지우므로 매 요청이 새 대화가 된다. 그래서 외부 클라이언트(hyundaicapital_front)는 스크럽
      목록에 없는 커스텀 헤더 x-hc-session-id 로 세션을 실어 보낸다 — 게이트웨이는 그 외의 헤더는
      그대로 파드까지 흘린다. 같은 방식으로 로그인에서 받은 보안 등급이 x-hc-security-level 로 온다.

    🔴 x-hc-security-level 은 **브라우저가 보내는 값이라 위조할 수 있다.** 화면 분기·로깅까지만
      쓰고, 문서 접근 통제의 근거로 삼으면 안 된다. 실제 집행이 필요해지면 이 코드가 사용자
      토큰으로 /api/admin/security-level/my-level 을 직접 확인해야 한다.

세션 하나 = LangGraph thread 하나. 같은 세션 ID 로 다시 질문하면 이전 messages 에 이어 붙어
멀티턴이 된다 (run_turn ①-b). 세션 ID 가 매 요청 바뀌면 매번 새 대화가 된다.

한 대화의 흐름 (부동산)
    턴1  질문          → 그래프가 pick 앞에서 멈춤                         → 에이전트 선택 UI
    턴2  "estate" 선택 → pick → call_llm(SQL 생성) → approve 앞에서 멈춤    → SQL 승인 UI
    턴3  승인          → approve → run_tools(SQL 실행) → call_llm(해설) → END
         거절          → approve → END
"""
import asyncio
import uuid

from fastapi import APIRouter, Header, Request
from fastapi.responses import StreamingResponse

import llm
import otel
from graph import AGENTS, GRAPH
from schemas import ChatRequest, ChatResponse, ChatResponseData
from sse import agent_select_frame, approve_frame, sse

router = APIRouter()

# 보안 등급 헤더가 없을 때 쓰는 값. 가장 낮은 등급 = 가장 적게 보인다.
MIN_SECURITY_LEVEL = 0

# 헤더 로그에 찍는 값의 최대 길이. 넘으면 앞부분만 남긴다 — 긴 토큰·JSON 헤더가 로그를 덮는 것을 막는다.
MAX_HEADER_LOG = 120


@router.get("/health")
async def health():
    return {"status": "ok"}


@router.post("/chat")
async def chat(body: ChatRequest, request: Request,
               session_id: str | None = Header(default=None, alias="x-genos-session-id"),
               custom_session_id: str | None = Header(default=None, alias="x-hc-session-id"),
               # 로그인에서 받은 보안 등급. 문서 검색이 이 등급 이하만 보게 한다 (rag.py).
               security_level: int | None = Header(default=None, alias="x-hc-security-level")):
    log_headers(request)
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

    # ①② 스트리밍.
    # x-genos-session-id(genportal 경유) → x-hc-session-id(외부 인증키 직접 호출)
    # → 바디 sessionId(폴백) → 신규 순으로 세션을 정한다.
    # 인증키 경로에서는 게이트웨이가 x-genos-session-id 를 지우므로 그 헤더만 보면
    # 매 턴이 새 대화가 되어 HITL 2턴째가 이어지지 않는다.
    thread_id = (session_id or (custom_session_id or "").strip()
                 or body.sessionId.strip() or str(uuid.uuid4()))

    # 헤더가 없으면 **최저 등급(0)** 으로 본다. 없을 때 전 등급을 열어 주면 헤더를 빼는 것만으로
    # 통제가 풀린다 — 모르면 막는 쪽이 맞다.
    level = MIN_SECURITY_LEVEL if security_level is None else security_level

    return StreamingResponse(
        run_turn(thread_id, body, level),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},   # 앞단 nginx 가 SSE 를 모아 한 번에 보내는 것을 막는다
    )


def log_headers(request: Request) -> None:
    """이 요청에 실려 온 헤더를 **전부** 로그에 남긴다 — 위 목록이 실제로 파드까지 오는지,
    게이트웨이가 무엇을 지우고 무엇을 흘리는지 눈으로 확인하는 용도.
    토큰류(access-token, authorization, cookie)는 값 대신 길이만, 그 외 긴 값은 앞부분만 찍는다.
    컨테이너 로그에서 `[total-example] 헤더` 로 찾는다."""
    def shown(name: str, value: str) -> str:
        if "token" in name or name in ("authorization", "cookie"):
            return f"<{len(value)}자, 값 생략>"
        if len(value) > MAX_HEADER_LOG:
            return f"{value[:MAX_HEADER_LOG]}…<총 {len(value)}자>"
        return value

    seen = {name: shown(name, value) for name, value in request.headers.items()}
    print(f"[total-example] 헤더 {seen}", flush=True)


async def run_turn(session_id: str, body: ChatRequest, security_level: int):
    """한 턴 = HTTP 요청 하나. 그래프를 (이어서) 돌리고 SSE 프레임을 yield 한다.

    security_level 은 턴마다 헤더에서 새로 온다. 체크포인트에 남은 값을 그대로 쓰면
    같은 세션에서 등급이 바뀌었을 때(재로그인) 옛 등급으로 검색하므로 **매 턴 덮어쓴다.**"""
    thread = {"configurable": {"thread_id": session_id}}    # 이 세션의 체크포인트를 가리킨다
    human_input = body.humanInput

    # 이 턴의 root span 은 FastAPI 자동 계측이 이미 열어 뒀다. 새로 만들지 않고 거기에 얹는다.
    # session.id 를 넣어야 Langfuse 에서 한 대화의 여러 턴이 묶여 보인다.
    turn_span = otel.current_span()
    otel.set_attrs(turn_span, {
        "langfuse.session.id": session_id,
        # Langfuse 목록에서 이 서비스 트레이스만 골라 보기 위한 태그. 서비스명을 그대로 쓴다.
        "langfuse.trace.tags": [otel.service_name()],
        # 트레이스 이름은 이 파드가 루트일 때만. 게이트웨이/gen-portal 이 이미 이름을 붙인 트레이스에
        # 끼어 들어간 경우(부모 있음)엔 상위 이름을 보존한다 — 577 law_agent 와 같은 규칙.
        "langfuse.trace.name": otel.service_name() if otel.is_root(turn_span) else None,
        "langfuse.observation.metadata.security_level": security_level,
        "langfuse.observation.input": otel.sanitize(body.question) if otel.collect_io() else None,
        **otel.deployment_metadata(),    # 리비전·배포·커밋 — Langfuse 에서 배포별로 걸러 본다
    })

    try:
        # ── 1. 그래프에 무엇을 넣을지 정한다 ─────────────────────────────────────
        if human_input is None:
            question = body.question.strip()
            snapshot = await GRAPH.aget_state(thread)
            previous = snapshot.values or {}

            if previous.get("agent") and previous.get("messages"):
                # ①-b 같은 세션의 **후속 질문**: 이전 대화를 이어간다.
                #     · messages 를 지우지 않고 user 메시지를 덧붙인다 → LLM 이 앞 턴을 기억한다
                #     · as_node="pick" = pick 을 방금 지난 것으로 표시 → 에이전트를 다시 묻지 않고
                #       call_llm 부터 재개한다 (pick 은 messages 를 덮어쓰므로 통과시키면 안 된다)
                #     · llm_calls 는 턴마다 0 으로 되돌린다. 누적하면 몇 턴 만에 MAX_LLM_CALLS 에 걸린다
                # ponytail: messages 무한 성장. 대화가 길어져 컨텍스트가 넘치면 여기서 잘라낸다
                #           (assistant.tool_calls ↔ tool 메시지 짝을 깨지 않게 턴 단위로).
                await GRAPH.aupdate_state(
                    thread,
                    {"question": question,
                     "messages": previous["messages"] + [{"role": "user", "content": question}],
                     "tool_calls": [], "approved": False, "llm_calls": 0,
                     "security_level": security_level},
                    as_node="pick",
                )
                graph_input = None
            else:
                # ①-a 새 대화: 상태를 처음부터 만든다. pick 앞에서 멈춰 에이전트 선택 UI 를 띄운다.
                graph_input = {"question": question, "agent": "", "messages": [],
                               "tool_calls": [], "approved": False, "llm_calls": 0,
                               "security_level": security_level}
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
                await GRAPH.aupdate_state(thread, {"agent": agent, "security_level": security_level})

            elif stopped_before == "approve":
                await GRAPH.aupdate_state(thread, {"approved": human_input.action == "submit",
                                                   "security_level": security_level})

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

    except (asyncio.CancelledError, GeneratorExit):
        # 사용자가 창을 닫거나 중단 — 실패가 아니다. ERROR 로 찍으면 실패율이 부풀려진다.
        otel.set_attrs(turn_span, {"langfuse.observation.level": "WARNING",
                                   "langfuse.observation.status_message": "cancelled"})
        raise
    except Exception as exc:
        # 여기서 예외를 삼키고 SSE error 프레임으로 바꾸므로 span 이 스스로 ERROR 가 되지 않는다.
        # 직접 찍어야 Langfuse 와 관리자 모니터링의 실패 집계(level != ERROR → 성공)에 잡힌다.
        # repr(exc) 는 LLM 게이트웨이 응답 본문이 섞여 나올 수 있어 마스킹된 메시지를 쓴다.
        otel.set_attrs(turn_span, {"langfuse.observation.level": "ERROR",
                                   "langfuse.observation.status_message": otel._error_message(exc)})
        yield sse("error", f"{type(exc).__name__}: {exc}")

    yield sse("end", None)
