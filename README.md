# Google Cloud QnA

구글 클라우드 관련 질문에 공식 자료를 기반으로 답변하는 에이전트다.

---

## 시스템 구성

<img src="web/static/architecture.png" />

---

## 폴더 및 파일 구조

```
google-cloud-qna/
├── Dockerfile                  # Cloud Run 배포용 컨테이너 빌드 지침 파일
├── README.md                   # 프로젝트 시스템 설명 및 설명서
├── requirements.txt            # Python 프로젝트 의존성 라이브러리 목록
├── engine/                     # Core 지능형 엔진 및 에이전트 소스 폴더
│   ├── agent.py                # 멀티 에이전트 협업 시스템(마스터 오케스트레이터 및 SSE 비동기 스트리밍 제너레이터)
│   ├── utils.py                # URL 검증, GCS 업로드, Graphviz 다이어그램 컴파일 및 이미지 빌드 등 공통 유틸리티 모듈
│   └── prompts/                # 개별 파일로 분리하여 관리하는 에이전트 프롬프트 마크다운 폴더
│       ├── evaluator_system_prompt.md    # 팩트 체크 및 사실 무결성 검증을 담당하는 평가자 프롬프트
│       ├── gcp_arch_standards.md         # GCP 엔터프라이즈 아키텍처 공식 표준 및 설계 모범사례 가이드라인
│       ├── remediator_system_prompt.md   # 오류 지적 사항에 따라 사실 왜곡 및 링크를 정정/보정하는 보정자 프롬프트
│       ├── synthesizer_system_prompt.md  # 8대 필라 부서 기술 권고안을 종합하여 아키텍처 초안을 생성하는 합성 아키텍트 프롬프트
│       └── pillars/                      # 8대 도메인별 서브 에이전트용 구글 검색 그라운딩 지침 마크다운 폴더
│           ├── apis_applications.md      # API 및 애플리케이션 개발/통합 설계 분야
│           ├── application_modernization.md # 마이크로서비스 및 컨테이너/서버리스 분야
│           ├── artificial_intelligence.md   # Vertex AI 플랫폼 및 GenAI 파이프라인 분야
│           ├── data_analytics.md         # BigQuery 및 실시간 데이터 파이프라인 분야
│           ├── databases.md              # 고가용성 클라우드 데이터베이스 설계 분야
│           ├── infrastructure.md         # 네트워킹, 로드 밸런서 및 인프라 설계 분야
│           ├── productivity_collaboration.md # Google Workspace 및 생산성 도구 분야
│           └── security.md               # 최소 권한 원칙(IAM) 및 기업 보안 아키텍처 분야
└── web/                        # Web 인터페이스 및 서비스 폴더
    ├── deploy.sh               # Google Cloud Build & Run 배포 자동화 및 가비지 리비전 청소 스크립트
    ├── main.py                 # FastAPI 및 Server-Sent Events (SSE) 기반 프론트엔드 연동 엔드포인트 웹서버
    └── static/                 # 프론트엔드 정적 리소스 폴더
        ├── app.js              # 실시간 SSE 렌더링, 서브 에이전트 상태 인터랙션 등 웹 화면 UI 제어 로직
        ├── index.html          # Google Cloud QnA 프리미엄 테마 반응형 웹 메인 포털 UI
        └── style.css           # 고급 다크 모드 및 Glassmorphism이 가미된 모던 스타일시트
```

---

## 퀵 스타트

### 1. 사전 권한 확보 및 환경 변수 설정
에이전트가 정상적으로 구글 검색 그라운딩 및 Gemini API를 호출할 수 있도록 아래 환경 변수를 셋팅하거나 권한을 확보한다.

~/.env
- `GOOGLE_CLOUD_LOCATION`: 가동할 리전 (예: `us-central1`)
- `GOOGLE_CLOUD_PROJECT`: 타겟 GCP 프로젝트 ID
- `GOOGLE_CLOUD_STORAGE`: 다이어그램이 저장되는 GCS 버킷

### 2. 로컬 테스트 및 실행
단일 컨테이너 아키텍처로 통합되어 로컬 테스트도 간단하다.
```bash
# 의존성 패키지 설치
pip install -r requirements.txt

# Uvicorn 서버를 사용한 로컬 구동
uvicorn web.main:app --host 0.0.0.0 --port 8080 --reload
```

### 3. 빌드 및 배포 자동화 실행
복잡한 수동 배포 단계 없이 제공되는 단일 쉘 스크립트를 통해 배포를 끝마친다.
```bash
# 최상위 루트 디렉터리로 컨텍스트를 지정하여 릴리즈 수행
./web/deploy.sh
```

배포가 끝나면 터미널 창에 반환되는 통합 포털 URL 주소로 즉시 접속하여 사용한다.  
- 배포 포털 실주소: [https://google-cloud-qna-gb3apzhmla-uc.a.run.app](https://google-cloud-qna-gb3apzhmla-uc.a.run.app)
