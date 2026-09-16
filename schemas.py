"""API 입출력 스키마 (Pydantic). genportal-api 와 주고받는 데이터 모양이 전부 여기 있다.

  요청   ChatRequest    genportal-api → 코드서빙  POST /chat body
  응답   ChatResponse   stream 이 없을 때의 JSON 응답
         (stream 이 있을 때는 SSE 라서 모델이 아니라 sse.py 의 함수가 문자열을 만든다)
  HITL   ActionData     SSE action 이벤트의 data. 선택 UI / 승인·거절 UI 의 모양
"""
from typing import Literal

from pydantic import BaseModel


# ───────────────────────────── 요청 ─────────────────────────────
class HumanInputValues(BaseModel):
    selected: list[str] = []      # single-select 에서 고른 값. 예) ["finance"]
    customInput: str = ""         # 「직접 입력」 칸에 쓴 글


class HumanInput(BaseModel):
    """HITL 응답. 사용자가 선택지를 고르거나 승인/거절 버튼을 누르면 **다음 요청** 에 담겨 온다."""
    interactionId: str = ""
    action: Literal["submit", "cancel"] = "submit"   # submit = 선택 완료·승인 / cancel = 취소·거절
    values: HumanInputValues = HumanInputValues()


class ChatRequest(BaseModel):
    """genportal-api 가 보내는 body. **body 에는 이 세 필드가 전부다.**

        {"question": "질문", "stream": true}                          ① 새 질문
        {"question": "",     "stream": true, "humanInput": {...}}      ② HITL 응답  ← question 이 빈다!
        {"question": "__verify__"}                                     ③ 「검증」 버튼 (stream 없음)

    세션 ID·사용자 ID 같은 나머지 문맥은 body 가 아니라 **헤더**(x-genos-*) 로 온다.
    어떤 헤더가 오고 이 코드가 무엇을 쓰는지는 router.py 맨 위 주석에 정리해 뒀다.
    """
    question: str = ""
    stream: bool = False
    humanInput: HumanInput | None = None
    sessionId: str = ""    # 외부 인증키 직접 호출용. 게이트웨이가 x-genos-session-id 헤더를 지우므로 바디로 받는다


# ──────────────────────── 응답 (stream 없을 때) ────────────────────────
class ChatResponseData(BaseModel):
    text: str


class ChatResponse(BaseModel):
    """code 0 = 성공, 그 외 = 실패. HTTP 상태 코드는 항상 200 이다."""
    code: int = 0
    errMsg: str = "success"
    data: ChatResponseData


# ─────────────── HITL UI (hitl-ui/v1) — SSE action 이벤트의 data ───────────────
class SelectOption(BaseModel):
    value: str
    label: str
    desc: str = ""


class SingleSelectComponent(BaseModel):
    type: Literal["single-select"] = "single-select"
    title: str                    # 질문 문구. FE 가 여기서 읽어 말풍선에 띄운다
    options: list[SelectOption]   # 끝의 「직접 입력」 칸은 FE 가 알아서 붙인다


class ConfirmComponent(BaseModel):
    type: Literal["confirm"] = "confirm"
    title: str = ""               # approve-button 쪽에만 넣으면 된다


class HitlElement(BaseModel):
    """FE 는 element.type 을 보고 무엇을 그릴지 정한다.
    ⚠ interactionId 는 element **안에** 있어야 한다. 없으면 FE 가 렌더를 거부한다."""
    type: Literal["single-select", "approve-button", "reject-button"]
    interactionId: str
    component: SingleSelectComponent | ConfirmComponent


class ActionData(BaseModel):
    interactionId: str
    elements: list[HitlElement]
