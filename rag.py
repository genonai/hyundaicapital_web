"""Filter RAG (경제금융용어 사전 에이전트) — 질문을 임베딩하고, Weaviate 에서 **파일명 필터를 건 하이브리드 검색**을 한다.

  hybrid_search("기준금리", file_name_filter="경제금융용어", top_k=5, security_level=6)
    1) 질문 임베딩        GenOS 임베딩 서빙   POST /v1/embeddings          (httpx, 비동기)
    2) 하이브리드 검색    Weaviate gRPC       collection.query.hybrid(...)  (weaviate-client v4, 동기 → 스레드)
    3) 복호화             is_encrypted 인 text 를 AES-GCM 으로 푼다
    → [{"pageContent": "...", "metadata": {...}}, ...]    채팅창 「출처」(sourceDocuments) 형식 그대로

왜 GraphQL(HTTP) 이 아니라 gRPC(weaviate-client) 인가
  GenOS Weaviate 는 RBAC 가 켜져 있고, VDB 상세에서 발급한 Read Key 는 **그 컬렉션 하나**만 읽을 수 있다.
  GraphQL 은 쿼리를 파싱할 때 전체 스키마를 훑어야(introspection) 해서 `read_collections *` 권한을
  요구한다 → 이 키로는 403. gRPC 검색은 해당 컬렉션의 read_data 만 확인하므로 Read Key 로 된다.
  (weaviate-client 는 grpcio 가 필요한데, 빌드 커맨드가 공개 PyPI 를 함께 보므로 설치된다.)
"""
import asyncio
import base64
import datetime

import httpx
import weaviate
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from weaviate.classes.init import Auth
from weaviate.classes.query import Filter, MetadataQuery

from config import settings

# 클러스터 내부 Weaviate 주소. 환경변수로 받지 않는다 — 쿠버네티스가 `WEAVIATE_GRPC_PORT=tcp://...` 처럼
# 같은 이름의 변수를 자동 주입해서 값이 덮이기 때문 (config.py 주석 참고). 바뀔 일 없는 고정값이다.
WEAVIATE_HOST = "llmops-weaviate-service"
WEAVIATE_HTTP_PORT = 8080
WEAVIATE_GRPC_PORT = 50051

# 복호화 키. 적재 파이프라인과 같은 방식(PBKDF2-SHA256, salt "genos", 10만 회)으로 만든다.
# 10만 회 반복은 느리므로 부팅 때 한 번만 계산해 둔다.
AES_KEY = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b"genos",
                     iterations=100_000).derive(settings.decrypt_key.encode())


# ─────────────────────────── 1) 임베딩 ───────────────────────────
async def embed(text: str) -> list[float]:
    """질문 하나를 벡터로 바꾼다. model 은 안 싣는다 — 서빙에 모델이 이미 고정돼 있다."""
    url = f"{settings.genos_url}/api/gateway/rep/serving/{settings.embedding_serving_id}/v1/embeddings"
    headers = {"Authorization": f"Bearer {settings.embedding_bearer_token}"}

    async with httpx.AsyncClient(timeout=60) as client:
        response = await client.post(url, headers=headers, json={"input": [text]})

    if response.status_code != 200:
        raise RuntimeError(f"임베딩 실패 HTTP {response.status_code}: {response.text[:300]}")
    return response.json()["data"][0]["embedding"]


# ─────────────────────────── 2) Weaviate ───────────────────────────
_client = None


def client() -> weaviate.WeaviateClient:
    """Weaviate 접속. 처음 부를 때 한 번 연결하고 재사용한다. Read Key 를 API Key 로 쓴다.
    skip_init_checks — 접속 시 /v1/meta 등을 확인하는 단계를 건너뛴다 (Read Key 권한 밖일 수 있다)."""
    global _client
    if _client is None or not _client.is_connected():
        _client = weaviate.connect_to_custom(
            http_host=WEAVIATE_HOST, http_port=WEAVIATE_HTTP_PORT, http_secure=False,
            grpc_host=WEAVIATE_HOST, grpc_port=WEAVIATE_GRPC_PORT, grpc_secure=False,
            auth_credentials=Auth.api_key(settings.weaviate_api_key),
            skip_init_checks=True,
        )
    return _client


def _search(query: str, vector: list[float], file_name_filter: str, top_k: int,
            security_level: int) -> list[dict]:
    """동기 함수. hybrid_search 가 스레드에서 돌린다 (weaviate-client 는 동기 라이브러리)."""
    collection = client().collections.get(settings.vdb_index)

    # hybrid  = 벡터 검색 + 키워드(BM25) 검색을 alpha 로 섞는다. 1.0 = 벡터만, 0.0 = 키워드만
    # filters = ★ Filter RAG 의 핵심. 두 조건을 AND 로 건다.
    #   · file_name       필터 문자열이 들어간 문서만
    #   · security_level  사용자 등급 **이하**만 (레벨 6 이면 0~6). 등급이 높을수록 민감한 문서다.
    #     security_level 프로퍼티가 없는(=null) 청크는 less_or_equal 에 걸리지 않아 **제외**된다.
    #     검색 결과가 통째로 비면 VDB 에 그 프로퍼티가 적재됐는지부터 본다.
    result = collection.query.hybrid(
        query=query,
        vector=vector,
        alpha=settings.hybrid_alpha,
        limit=top_k,
        filters=(Filter.by_property("file_name").like(f"*{file_name_filter}*")
                 & Filter.by_property("security_level").less_or_equal(security_level)),
        return_metadata=MetadataQuery(score=True),
    )

    # 3) 채팅창 「출처」 형식으로 바꾼다.  text → pageContent, 나머지 프로퍼티 → metadata
    #    (프로퍼티는 기본으로 전부 돌아온다 — 뷰어가 쓰는 chunk_bboxes·i_page 도 포함)
    docs = []
    for obj in result.objects:
        props = dict(obj.properties)
        text = str(props.pop("text", "") or "")
        if props.get("is_encrypted"):
            text = decrypt(text)
        metadata = {key: _json_safe(value) for key, value in props.items() if value is not None}
        metadata["score"] = obj.metadata.score
        docs.append({"pageContent": text, "metadata": metadata})
    return docs


async def hybrid_search(query: str, file_name_filter: str, top_k: int,
                        security_level: int) -> list[dict]:
    """security_level 은 기본값을 두지 않는다 — 빠뜨린 호출이 조용히 전 등급을 검색하면 안 된다."""
    vector = await embed(query)
    return await asyncio.to_thread(_search, query, vector, file_name_filter, top_k, security_level)


def _json_safe(value):
    """Weaviate 는 날짜를 datetime, id 를 UUID 객체로 돌려준다. 그대로는 JSON 으로 못 나가니 문자열로."""
    if isinstance(value, (datetime.datetime, datetime.date)):
        return value.isoformat()
    if isinstance(value, (str, int, float, bool, list, dict)):
        return value
    return str(value)


# ─────────────────────────── 3) 복호화 ───────────────────────────
def decrypt(ciphertext_b64: str) -> str:
    """AES-256-GCM 복호화. 저장 형식은 base64( IV 12바이트 + Tag 16바이트 + 암호문 ) 이다."""
    raw = base64.b64decode(ciphertext_b64)
    iv, tag, ciphertext = raw[:12], raw[12:28], raw[28:]
    try:
        decryptor = Cipher(algorithms.AES(AES_KEY), modes.GCM(iv, tag)).decryptor()
        return (decryptor.update(ciphertext) + decryptor.finalize()).decode("utf-8")
    except Exception as exc:
        raise RuntimeError(f"청크 복호화 실패 — DECRYPT_KEY 가 맞는지 확인: {exc}") from exc


def docs_to_text(docs: list[dict]) -> str:
    """검색 결과를 LLM 에 돌려줄 문자열로 만든다. 청크가 길면 앞 1500자만 넣는다(컨텍스트 보호)."""
    if not docs:
        return "검색 결과가 없습니다."
    lines = []
    for i, doc in enumerate(docs, start=1):
        meta = doc["metadata"]
        lines.append(f"[{i}] {meta.get('file_name', '')} p.{meta.get('i_page', '')}\n"
                     f"{doc['pageContent'][:1500]}")
    return "\n\n".join(lines)
