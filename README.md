# Google Cloud QnA

구글 클라우드 관련 질문에 공식 자료를 기반으로 답변하는 에이전트다.

---

## 시스템 구성

<img src="web/static/architecture.png" />

---

## 폴더 및 파일 구조

```
google-cloud-qna/
├── Dockerfile
├── engine/
│   └── agent.py
├── README.md
├── requirements.txt
└── web/
    ├── deploy.sh
    ├── main.py
    └── static/
        ├── app.js
        ├── index.html
        └── style.css
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
