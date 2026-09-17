"""OpenTelemetry — 이 코드서빙 **안에서** 벌어지는 일을 Langfuse(OTLP/HTTP)로 보낸다.

게이트웨이(CodeServingOTelFilter)는 코드서빙 호출의 **입구와 출구만** 기록한다. 그래서 이 파일이
없으면 파드 안에서 LLM 을 몇 번 불렀는지, SQL 이 몇 초 걸렸는지, 어디서 터졌는지가 전부 안 보인다.

게이트웨이가 traceparent 헤더를 실어 보내므로(aiohttp 자동 계측), 여기서 만드는 span 은 게이트웨이의
`code_serving` span **밑에 붙는다.** 별도 트레이스가 새로 생기지 않는다.

    code_serving                       ← 게이트웨이가 만든다
      └── POST http://code-serving-…
            └── POST /chat             ← FastAPI 자동 계측. router.run_turn 이 여기에
                  │                       session.id·tags 를 얹는다 (span 을 새로 만들지 않는다)
                  ├── llm              ← graph.call_llm   (+ httpx 자동 span)
                  └── tool.xxx         ← tools.run

설계
  · 기본 꺼짐 — OTEL_ENABLED=true 일 때만 초기화한다. 그 외에는 모든 헬퍼가 no-op 이라
    호출부에 `if` 를 넣을 필요가 없다.
  · guarded import — opentelemetry 가 안 깔려 있어도 부팅은 된다. 코드서빙 pip 미러에 패키지가
    없을 때 앱 전체가 죽는 것이 제일 나쁘다.
  · 트레이싱 실패는 절대 응답에 영향을 주지 않는다. 전부 삼킨다.

env (코드서빙 리비전 > 환경 변수)
  OTEL_ENABLED                 기본 false
  OTEL_SERVICE_NAME            기본 hyundaicapital. Langfuse trace 태그(langfuse.trace.tags)로도 쓴다
  OTEL_EXPORTER_OTLP_ENDPOINT  기본 http://langfuse-web:3000/api/public/otel — 뒤에 /v1/traces 를 붙인다
                               ⚠ 환경마다 langfuse-web / langfuse-helm-web 로 갈린다. 확인 후 넣을 것
  LANGFUSE_PUBLIC_KEY          Basic auth. SECRET 과 둘 다 있을 때만 헤더를 붙인다
  LANGFUSE_SECRET_KEY
  OTEL_COLLECT_INPUT_OUTPUT    기본 false. true 면 질문·답변 **원문**이 Langfuse 에 그대로 쌓인다
"""
import base64
import os
import threading
from contextlib import contextmanager

# 이 앱은 logging 을 설정하지 않아 logger.info 가 컨테이너 로그에 안 찍힌다.
# 나머지 파일과 같이 print(flush=True) 로 남긴다 — `[total-example]` 로 grep 된다.
_TRUE = {"1", "true", "yes", "on"}
_DEFAULT_ENDPOINT = "http://langfuse-web:3000/api/public/otel"

_tracer = None
_provider = None
_lock = threading.Lock()
_initialized = False


def _env_true(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE


def _enabled() -> bool:
    return _env_true("OTEL_ENABLED")


def collect_io() -> bool:
    """질문·답변 원문을 span 에 실을지. 호출부가 이 값으로 분기한다.

    ponytail: 켜면 평문이 Langfuse 에 그대로 적재된다. GenOS 본체는 같은 자리에서
              G__ENCRYPT__SVC_LOG 로 암호화하는데 여기는 안 한다 — 개발망 디버깅용으로만 켤 것.
              운영에서 상시로 켜야 하면 그때 crypter 를 붙인다."""
    return _env_true("OTEL_COLLECT_INPUT_OUTPUT")


def service_name() -> str:
    return os.environ.get("OTEL_SERVICE_NAME", "hyundaicapital")


def _endpoint() -> str:
    return os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", _DEFAULT_ENDPOINT).rstrip("/")


def _build_tracer():
    """TracerProvider + OTLPSpanExporter 초기화. opentelemetry 는 여기서만 import 한다(guarded)."""
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    global _provider
    kwargs = {"endpoint": f"{_endpoint()}/v1/traces"}
    pub, sec = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if pub and sec:
        kwargs["headers"] = {"Authorization": "Basic " + base64.b64encode(f"{pub}:{sec}".encode()).decode()}

    provider = TracerProvider(resource=Resource.create({"service.name": service_name()}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**kwargs)))
    trace.set_tracer_provider(provider)
    _provider = provider
    return trace.get_tracer(service_name())


def _get_tracer():
    global _tracer, _initialized
    if _initialized:
        return _tracer
    with _lock:
        if _initialized:
            return _tracer
        if not _enabled():
            print("[total-example] OTel 꺼짐 (OTEL_ENABLED 미설정)", flush=True)
            _initialized = True
            return None
        try:
            _tracer = _build_tracer()
            pub = "O" if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY") else "X"
            print(f"[total-example] OTel 켜짐  service={service_name()}  "
                  f"endpoint={_endpoint()}  auth={pub}", flush=True)
        except Exception as exc:    # 미설치·초기화 실패 → 트레이싱만 끄고 서비스는 계속 뜬다
            print(f"[total-example] OTel 초기화 실패 — 트레이싱만 끕니다: {exc!r}", flush=True)
            _tracer = None
        _initialized = True
        return _tracer


def init(app) -> None:
    """앱 부팅 시 1회. FastAPI·httpx 자동 계측을 붙인다.

    ⚠ FastAPI 계측은 미들웨어를 추가하므로 **서버가 뜨기 전에** 불러야 한다 (main.py 모듈 레벨).

    httpx 계측이 llm.stream_chat 의 LLM 호출과 rag.embed 의 임베딩 호출을 자동으로 잡는다 —
    그 둘은 따로 span 을 안 넣어도 된다."""
    if _get_tracer() is None:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        FastAPIInstrumentor.instrument_app(app, excluded_urls="health")
        HTTPXClientInstrumentor().instrument()
        print("[total-example] OTel 자동계측 완료 (FastAPI, httpx)", flush=True)
    except Exception as exc:
        print(f"[total-example] OTel 자동계측 실패: {exc!r}", flush=True)


@contextmanager
def span(name: str, attributes: dict | None = None):
    """트레이싱 span 컨텍스트. OTel 이 꺼져 있거나 미설치면 no-op(yield None).

    안에서 예외가 나면 Langfuse 가 읽는 level=ERROR 로 표시하고 그대로 다시 올린다 —
    GenOS 본체(genos_otel/middleware.py)와 같은 4줄이다."""
    tracer = _get_tracer()
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name) as sp:
        set_attrs(sp, attributes)
        try:
            yield sp
        except Exception as exc:
            from opentelemetry.trace import StatusCode
            try:
                sp.set_status(StatusCode.ERROR, str(exc))
                sp.record_exception(exc)
            except Exception:
                pass
            set_attrs(sp, {"langfuse.observation.level": "ERROR",
                           "langfuse.observation.status_message": repr(exc)})
            raise


def set_attrs(sp, attributes: dict | None) -> None:
    """span 에 attribute 를 건다. None span·None 값은 조용히 무시한다."""
    if sp is None or not attributes:
        return
    for key, value in attributes.items():
        if value is None:
            continue
        try:
            if isinstance(value, (list, tuple)):
                # OTel 은 동일 타입 시퀀스를 허용한다. langfuse.trace.tags 가 문자열 배열을
                # 요구하므로 str() 로 뭉개면 안 된다 — "['a']" 가 태그 이름이 되어 버린다.
                value = [str(v) for v in value]
            elif not isinstance(value, (str, bool, int, float)):
                value = str(value)
            sp.set_attribute(key, value)
        except Exception:    # 트레이싱은 응답에 영향을 주지 않는다
            pass


def current_span():
    """지금 열려 있는 span. OTel 이 꺼져 있으면 None.

    FastAPI 자동 계측이 요청마다 span 을 하나 열어 두므로, 새 span 을 만들지 않고
    거기에 attribute 만 얹고 싶을 때 쓴다."""
    if _get_tracer() is None:
        return None
    from opentelemetry import trace
    return trace.get_current_span()


def shutdown() -> None:
    """BatchSpanProcessor flush — 파드가 내려갈 때 아직 안 보낸 span 을 잃지 않는다."""
    if _provider is not None:
        try:
            _provider.shutdown()
        except Exception:
            pass
