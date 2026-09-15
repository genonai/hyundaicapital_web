"""MySQL (부동산 에이전트) — 부산 부동산 실거래 테이블에 LLM 이 만든 SQL 을 검사하고 실행한다.

Text2SQL 흐름에서 이 파일이 맡는 것
  TABLE_SCHEMA   LLM 시스템 프롬프트에 넣는 테이블 설명. 실제 DB 를 조회해 적은 값이다(추측 아님).
  sanitize()     LLM 이 만든 SQL 을 실행하기 전 **유일한 방어선**. 조회 전용인지 검사하고 LIMIT 을 붙인다.
                 승인 UI 에 뜨는 SQL 도 이 함수를 거친 값이다 → 사용자가 본 것과 실행되는 것이 같다.
  run_select()   실행. pymysql 은 동기 라이브러리라 호출부(tools.py)가 스레드로 뺀다.
"""
import re

import pymysql

from config import settings

MAX_ROWS = 200

TABLE_SCHEMA = """테이블: real_estate_transactions — 부산광역시 2024년 부동산 실거래 (2000행)

  id                   BIGINT        거래ID (PK)
  deal_date            DATE          거래일자. 2024-01-01 ~ 2024-12-31
  deal_year            INT           거래연도. 전부 2024
  deal_month           INT           거래월 1~12
  deal_day             INT           거래일
  sido                 VARCHAR(30)   시도. 전부 '부산광역시'
  sigungu              VARCHAR(50)   시군구. '해운대구','수영구','남구','동래구','부산진구',
                                     '연제구','금정구','사하구','북구','기장군'
  eup_myeon_dong       VARCHAR(50)   읍면동 (11종)
  legal_dong           VARCHAR(50)   법정동
  jibun                VARCHAR(30)   지번
  property_type        VARCHAR(20)   부동산유형. '아파트','오피스텔','연립다세대','단독다가구','상가주택'
  complex_name         VARCHAR(100)  단지명/건물명 (532종)
  exclusive_area_sqm   DECIMAL(8,2)  전용면적 m². 18.50 ~ 180.30
  floor                INT           층
  built_year           INT           건축년도. 1988 ~ 2024
  deal_amount_manwon   INT           거래금액(만원). 11063 ~ 284883, 평균 81977
  price_per_sqm_manwon DECIMAL(10,2) m²당 가격(만원)
  contract_type        VARCHAR(20)   계약유형. '중개거래','직거래'
  buyer_type           VARCHAR(20)   매수자유형. 전부 '개인'
  seller_type          VARCHAR(20)   매도자유형. '개인','법인','기타'
  cancelled_yn         CHAR(1)       해제여부. 'N' 정상거래, 'Y' 해제
  data_source          VARCHAR(50)   데이터출처

주의:
- 금액 단위는 만원이다. "3억" 은 30000, "10억" 은 100000.
- 평(坪) 환산은 exclusive_area_sqm / 3.3058.
- 정상 거래만 볼 때는 cancelled_yn = 'N' 조건을 넣는다.
- 2024년 부산 데이터만 있으므로 다른 연도·지역 질문에는 데이터가 없다고 답해야 한다."""

# 조회 외 구문. 하나라도 들어 있으면 실행하지 않는다.
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|rename|grant|revoke|replace|"
    r"call|handler|lock|unlock|set|commit|rollback|load_file|outfile|dumpfile|sleep|benchmark)\b",
    re.IGNORECASE,
)


def sanitize(raw_sql: str) -> str:
    """LLM 이 만든 SQL → 실행해도 되는 SQL. 문제가 있으면 ValueError (승인 UI 를 띄우지 않고 사유를 알린다)."""
    sql = raw_sql.strip()
    if sql.startswith("```"):                              # 마크다운 코드블록을 벗긴다
        sql = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", sql, flags=re.S).strip()
    sql = sql.rstrip(";").strip()

    if not sql:
        raise ValueError("SQL 이 비어 있습니다")
    if ";" in sql:
        raise ValueError("한 번에 여러 문장을 실행할 수 없습니다")
    if not re.match(r"^(select|with)\b", sql, re.IGNORECASE):
        raise ValueError("SELECT 또는 WITH 로 시작하는 조회 쿼리만 허용됩니다")
    found = FORBIDDEN.search(sql)
    if found:
        raise ValueError(f"조회 외 구문이 포함됐습니다: {found.group(0).upper()}")
    if not re.search(r"\blimit\s+\d+", sql, re.IGNORECASE):
        sql += f" LIMIT {MAX_ROWS}"                         # 결과 크기 상한
    return sql


def run_select(sql: str) -> list[dict]:
    """검사를 통과한 SELECT 를 실행한다. 최대 MAX_ROWS 행."""
    connection = pymysql.connect(
        host=settings.db_host, port=3306,
        user=settings.db_user, password=settings.db_password, database=settings.db_name,
        ssl={"ssl": {}},                                   # Azure Database for MySQL 은 TLS 필수
        connect_timeout=10, read_timeout=30,
        cursorclass=pymysql.cursors.DictCursor,           # 행을 {컬럼: 값} dict 로 받는다
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return cursor.fetchmany(MAX_ROWS)
    finally:
        connection.close()


def rows_to_markdown(rows: list[dict], limit: int = 50) -> str:
    """조회 결과를 마크다운 표로. 사용자 화면에도 보여주고 LLM 에도 그대로 넘긴다."""
    if not rows:
        return "(결과 없음)"
    columns = list(rows[0].keys())
    lines = ["| " + " | ".join(columns) + " |",
             "|" + "---|" * len(columns)]
    for row in rows[:limit]:
        lines.append("| " + " | ".join(str(row.get(c, "")) for c in columns) + " |")
    if len(rows) > limit:
        lines.append(f"\n... 총 {len(rows)}행 중 {limit}행만 표시")
    return "\n".join(lines)
