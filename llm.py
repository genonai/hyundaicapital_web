"""GenOS LLM 서빙 호출 — OpenAI 호환 /v1/chat/completions, 스트리밍, 툴 콜링.

흐름
  1. httpx 비동기 클라이언트를 열고 stream=true 로 POST 한다.
  2. 응답은 SSE 로 한 줄씩 온다:   data: {"choices":[{"delta":{...}}]}
  3. delta 안에는 두 가지가 섞여 온다.
       delta.content     답변 텍스트 조각 → 받는 즉시 밖으로 내보낸다 (yield)
       delta.tool_calls  툴 호출 조각     → 실행하지 않고 pending 버퍼에 모아 둔다
  4. data: [DONE] 이 오면 스트림 끝. pending 에 모인 툴 호출이 있으면 그때 한 번 내보낸다.

⚠ tool_calls 는 한 번에 오지 않는다. 이렇게 잘게 쪼개져서 온다.
     {"index":0, "id":"call_x1", "function":{"name":"search_finance_glossary", "arguments":""}}
     {"index":0, "function":{"arguments":"{\\"que"}}
     {"index":0, "function":{"arguments":"ry\\": \\"연차"}}
     {"index":0, "function":{"arguments":" 휴가\\"}"}}
   index 가 같은 조각의 arguments 를 이어붙여야 {"query": "연차 휴가"} 가 완성된다.
   그래서 완성될 때(스트림 끝)까지는 실행할 수 없고, 버퍼에 담아 두는 것이다.

이 함수가 yield 하는 것
     {"type": "token",      "text": "조각"}
     {"type": "tool_calls", "tool_calls": [ {"id", "type", "function": {"name", "arguments"}}, ... ]}
"""
import json

import httpx

from config import settings


async def stream_chat(messages: list, tools: list | None = None):
    url = f"{settings.genos_url}/api/gateway/rep/serving/{settings.llm_serving_id}/v1/chat/completions"
    headers = {"Authorization": f"Bearer {settings.llm_bearer_token}"}
    body = {"messages": messages, "stream": True, "temperature": 0}
    if settings.llm_model:
        body["model"] = settings.llm_model
    if tools:
        body["tools"] = tools            # LLM 에게 "이런 툴이 있다" 고 알려준다
        body["tool_choice"] = "auto"     # 쓸지 말지는 LLM 이 정한다

    # 아직 완성되지 않은 툴 호출 조각을 모아 두는 곳.  index → 툴 호출 하나
    pending_tool_calls: dict[int, dict] = {}

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream("POST", url, headers=headers, json=body) as response:

            if response.status_code != 200:
                detail = (await response.aread()).decode("utf-8", "replace")
                raise RuntimeError(f"LLM 호출 실패 HTTP {response.status_code}: {detail[:300]}")

            async for line in response.aiter_lines():
                # ── SSE 한 줄 해석 ──
                line = line.strip()
                if not line.startswith("data:"):
                    continue                                   # 빈 줄, `: keepalive` 주석은 버린다
                payload = line[len("data:"):].strip()
                if payload == "[DONE]":
                    break                                      # 스트림 끝
                try:
                    chunk = json.loads(payload)
                except ValueError:
                    continue                                   # 깨진 줄은 버린다
                choices = chunk.get("choices") or []
                if not choices:
                    continue                                   # usage 만 담긴 마지막 청크 등
                delta = choices[0].get("delta") or {}

                # ── (a) 텍스트 조각 → 바로 내보낸다 ──
                if delta.get("content"):
                    yield {"type": "token", "text": delta["content"]}

                # ── (b) 툴 호출 조각 → pending 에 이어붙인다 (아직 실행하지 않는다) ──
                for piece in delta.get("tool_calls") or []:
                    index = piece.get("index", 0)
                    if index not in pending_tool_calls:
                        pending_tool_calls[index] = {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        }
                    call = pending_tool_calls[index]
                    function = piece.get("function") or {}
                    if piece.get("id"):
                        call["id"] = piece["id"]
                    if function.get("name"):
                        call["function"]["name"] += function["name"]
                    if function.get("arguments"):
                        call["function"]["arguments"] += function["arguments"]

    # ── 스트림이 끝났다. 툴 호출이 모였으면 완성본을 한 번에 내보낸다 ──
    if pending_tool_calls:
        completed = [pending_tool_calls[i] for i in sorted(pending_tool_calls)]
        yield {"type": "tool_calls", "tool_calls": completed}
