"""환경변수 — pydantic-settings 로 한 번에 읽고 검증한다.

· 환경변수 이름은 대문자(GENOS_URL), 여기 필드는 소문자(genos_url). 대소문자는 구분하지 않는다.
· 기본값이 없는 필드는 **필수**다. 비어 있으면 부팅 시점에 바로 실패하고, 어떤 변수가 빠졌는지
  에러 메시지에 그대로 나온다. (요청이 올 때까지 기다렸다 실패하면 원인 찾기가 어렵다)
· 로컬에서는 .env 파일로도 읽는다. .env 는 .gitignore 에 있다.

코드서빙 리비전 화면에서
  · 토큰·비밀번호(*_TOKEN, *_KEY, DB_PASSWORD) → 「환경 변수」 행 (암호화 저장)
  · 나머지 → 시작 커맨드 앞에 `KEY=값 ` 으로 붙여도 되고 환경 변수 행에 넣어도 된다
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── GenOS 게이트웨이 ── 모든 모델 호출은 게이트웨이를 Bearer 토큰으로 지나간다 (인증·과금·가드레일)
    genos_url: str                    # 예) https://genos.genon.ai
    llm_serving_id: str               # LLM 모델서빙 ID
    llm_bearer_token: str             # 그 서빙의 인증 키
    llm_model: str = ""               # 비우면 body 에 model 을 싣지 않는다
    embedding_serving_id: str         # 임베딩 모델서빙 ID
    embedding_bearer_token: str       # 그 서빙의 인증 키

    # ── Weaviate ── 경제금융용어 사전 에이전트의 Filter RAG
    #   호스트·포트는 여기 두지 않고 rag.py 에 상수로 박아 뒀다. 쿠버네티스가 네임스페이스 안 서비스마다
    #   `<서비스명>_PORT=tcp://10.x.x.x:50051` 같은 환경변수를 자동 주입하는데, WEAVIATE_GRPC_PORT 가
    #   정확히 그 이름과 겹쳐서 int 파싱에 실패하며 부팅이 죽었다 (실제로 겪음).
    weaviate_api_key: str             # VDB 상세 > 인증 키 > Read Key
    vdb_index: str                    # 컬렉션 이름. 예) H05e9c8d715b9...
    glossary_file_filter: str = "경제금융용어"  # 파일명에 이 글자가 들어간 문서만 검색한다
    top_k: int = 5                    # 검색 문서 수
    hybrid_alpha: float = 0.5         # 1.0 = 벡터만, 0.0 = 키워드만
    decrypt_key: str = "mnc"          # 암호화 적재 VDB 의 복호화 키

    # ── MySQL ── 부동산 에이전트의 Text2SQL. 포트는 3306 고정 (Azure Database for MySQL)
    db_host: str                      # 예) xxx.mysql.database.azure.com
    db_user: str
    db_password: str
    db_name: str


settings = Settings()
