# 현대캐피탈 3차 세션 진행안

3차 세션은 전달주신 3가지 케이스를 기반으로, 외부 개발한 Agent를 Code Serving으로 배포·서빙하고 운영 단계까지 이어지는 전체 흐름을 확인하는 방향으로 준비하고자 합니다.

기존 예제 Agent를 활용하여 Git → 외부 연동 → 배포/서빙 → 운영 확인까지 하나의 케이스로 연결해 시연할 예정입니다.

## 1. 전체 구성

### 외부 환경


Frontend에서 GenOS Code Serving Endpoint 호출

### GenOS 내부 환경

1. 외부에서 구성된 Agent 이관
2. Agent API를 Code Serving을 통해 배포
3. Code Serving Endpoint를 통한 외부 요청 처리
4. Agent에서 GenOS 내/외부 Resource(RAG,RDB) 활용

Text2SQL(외부DB), RAG(내부 VDB), HITL, Multi-turn 등의 Agent 기능 구성 및 시연

전체적인 요청 흐름은 아래와 같습니다.

> 사용자 → 외부 Frontend → Code Serving Endpoint → Agent → GenOS 내/외부 Resource(RAG) → Agent → Streaming Response → Frontend

## 2. 주요 확인 내용

### ① 코드서빙 기반 Agent 구성 및 배포

외부에서 Agent를 구성하고, Code Serving을 이용해 Agent API 형태로 배포하는 전체 과정을 확인합니다.

이를 통해 현대캐피탈에서 기존에 개발한 Agent를 GenOS 환경에서 서비스하기 위해 필요한 코드 구조, 환경 설정 및 배포 방식을 함께 설명드릴 예정입니다.

### ② 외부 Frontend와 Code Serving 연동

외부에 배포된 Frontend에서 GenOS 내부 Code Serving Endpoint를 호출하여 Agent와 통신하는 구조를 구성합니다.

특히 실제 서비스 연동을 고려하여 요청 및 응답 구조를 Frontend에서 처리하는 과정을 확인할 예정입니다.

### ③ Session 기반 Multi-turn 구성

Frontend에서 Agent API를 호출할 때 Session ID와 같이 대화를 식별하기 위한 값을 Header 또는 요청 데이터에 전달할 수 있는지 확인하고, 해당 값을 Agent에서 활용하여 Multi-turn 구성 방안을 확인합니다.

이 과정에서 다음 내용을 중점적으로 확인할 예정입니다.

1. Code Serving 호출 시 Custom Header 전달 가능 여부
2. 동일 Session의 후속 요청에서 이전 대화를 이어가는 방식(Multi-turn)

### ④ Text2SQL / RAG / HITL 동작 확인

전달주신 케이스를 기준으로 다음 기능들을 함께 시연할 예정입니다.

* Text2SQL: 자연어 요청을 기반으로 내부 데이터 조회 및 결과 활용
* RAG(AI Drive): GenOS 내부 데이터/리소스를 검색하여 Agent 응답 생성 및 사용자의 보안에 따른 검색 필터링
* HITL: Agent 실행 과정에서 사용자 확인 또는 승인이 필요한 경우 실행을 제어하고 이후 프로세스를 이어가는 방식
* Multi-turn: 이전 대화 Context를 활용한 연속적인 질의응답




### ⑤ 로그 모니터링

외부 Frontend의 요청이 Code Serving을 통해 Agent로 전달된 이후, Agent의 내/외부
Resource 활용 내역을 추적하고 모니터링하는 방법을 확인할 예정입니다.

* 내부 Resource의 호출 및 GenOS 외부 통신에 대한 로그/Trace 확인


## 3. 3차 세션 목표

이번 세션을 통해 최종적으로

>「외부 Agent 이관 → → 외부 서비스 연동 → Code Serving 배포  → Session 기반 Multi-turn → Text2SQL/RAG/HITL 활용  → 운영 단계 모니터링」

까지의 전체 흐름을 확인하고, 현대캐피탈에서 기존에 개발한 Agent를 GenOS 환경에서 실제 서비스 형태로 구성할 수 있도록 하는 것을 목표로 하고 있습니다.

위와 같은 방향으로 준비하고자 하며, 추가로 확인이 필요하거나 세션에서 함께 다루었으면 하는 내용이 있으시면 말씀 부탁드립니다.