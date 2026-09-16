"""SSE 프레임 만들기 — gen-portal 채팅창이 읽는 형식.

형식은 딱 하나다.
    data: {"event": "<이벤트명>", "data": <내용>}\\n\\n

⚠ 반드시 `data: ` 로 시작해야 한다. genportal-api 파서는 그 외 줄을 전부 버린다.
   워크플로우 API 문서의 `token: ` / `result: ` 형식은 Flowise 컨테이너 전용이라 여기선 안 통한다.
⚠ 최종 답변은 genportal-api 가 token 을 전부 이어붙여 만든다. 따로 result 이벤트는 없다.

이벤트 종류
    token            답변 텍스트 조각
    sourceDocuments  출처 목록 [{"pageContent": "...", "metadata": {...}}]  → 채팅창 하단 「출처」
    action           HITL UI. data 의 모양은 schemas.ActionData. 아래 두 함수가 만든다
    error            에러 문구
    end              스트림 끝
"""
import json
import uuid

import tools
from graph import AGENTS
from schemas import (ActionData, ConfirmComponent, HitlElement, SelectOption,
                     SingleSelectComponent)


def sse(event: str, data) -> str:
    return "data: " + json.dumps({"event": event, "data": data}, ensure_ascii=False) + "\n\n"


def agent_select_frame() -> str:
    """에이전트 선택 UI. 사용자가 고르면 다음 요청 body 에 이렇게 담겨 온다 (schemas.HumanInput).
        {"question": "", "stream": true,
         "humanInput": {"interactionId": "...", "action": "submit", "values": {"selected": ["finance"]}}}
    우리는 대화를 세션 헤더로 찾으므로 interactionId 는 매번 새로 만들어도 된다."""
    interaction_id = str(uuid.uuid4())
    element = HitlElement(
        type="single-select",
        interactionId=interaction_id,
        component=SingleSelectComponent(
            title="사용하고자 하는 에이전트를 선택해 주세요.",
            options=[SelectOption(value=key, label=agent["label"], desc=agent["desc"])
                     for key, agent in AGENTS.items()],
        ),
    )
    return sse("action", ActionData(interactionId=interaction_id, elements=[element]).model_dump())


def approve_frame(tool_calls: list) -> str:
    """툴 실행 승인 UI.
    ⚠ element 가 **두 개** 필요하다 — approve-button 과 reject-button. FE 가 element.type 으로 버튼을 고른다.
      버튼 글자는 FE 가 「답변 진행」/「답변 거절」로 넣는다. 누르면 humanInput.action 이 submit / cancel 로 온다."""
    interaction_id = str(uuid.uuid4())
    title = "아래 작업을 실행할까요?\n\n" + "\n\n".join(tools.describe(call) for call in tool_calls)
    elements = [
        HitlElement(type="approve-button", interactionId=interaction_id, component=ConfirmComponent(title=title)),
        HitlElement(type="reject-button", interactionId=interaction_id, component=ConfirmComponent()),
    ]
    return sse("action", ActionData(interactionId=interaction_id, elements=elements).model_dump())
