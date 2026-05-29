FROM python:3.11-slim

WORKDIR /app

# 빌드 및 실행에 필요한 전체 통합 의존성 및 시스템 도구(Graphviz) 설치
RUN apt-get update && apt-get install -y --no-install-recommends \
    graphviz \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 에이전트 엔진 모듈 및 웹 게이트웨이 코드를 컨테이너에 전부 패키징
COPY engine/ ./engine/
COPY web/ ./web/

# Cloud Run 포트 자동 바인딩
ENV PORT=8080
# PYTHONPATH 설정을 통해 최상위 패키지 및 모듈 탐색 보장
ENV PYTHONPATH=/app

CMD ["uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "8080"]
