"""FastAPI Server for Google Cloud QnA Web Dashboard.

Vertex AI Agent Engine 오케스트레이터를 로컬 컨테이너 내부에서 직접 기동하고,
실시간 에이전트 오케스트레이션 결과를 Server-Sent Events(SSE)로 비동기 스트리밍 중계하며, 정적 자원을 서빙한다.
"""

import os
import sys

# 상위 디렉터리를 PYTHONPATH에 추가하여 패키지 구조에서 engine을 원활히 가져오도록 지원한다.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.utils import load_env_file
load_env_file()

import json
import logging
from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

# 로컬 엔진에서 비동기 오케스트레이터 직접 임포트
from engine.agent import run_orchestrator_sse_async

# 로깅 설정
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# Lifespan 컨텍스트 매니저를 통해 구동 시 포트 8080 조기 바인딩 보장
@asynccontextmanager
async def lifespan(app: FastAPI):
  logger.info("[Web] Application startup completed. Port 8080 open with local agent engine.")
  yield

app = FastAPI(
  title="Google Cloud QnA Web Dashboard",
  description="Multi-Agent Collaboration Visualizer Integrated Local Gateway",
  version="4.0.0",
  lifespan=lifespan
)

# CORS 미들웨어 구성
app.add_middleware(
  CORSMiddleware,
  allow_origins=["*"],
  allow_credentials=True,
  allow_methods=["*"],
  allow_headers=["*"],
)

# static 디렉토리 매핑을 위해 존재 유무 파악 후 폴더 생성
static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(static_dir, exist_ok=True)

# API 엔드포인트: 로컬 에이전트 기동 후 비동기 오케스트레이션 스트림 다이렉트 중계 (SSE)
@app.get("/api/stream")
async def stream_orchestrator(query: str = Query(..., description="사용자 기술 질의")):
  logger.info(f"[Web] Invoking local integrated agent engine for query: '{query}'")
  
  async def event_generator():
    try:
      # 로컬 비동기 엔진 오케스트레이터를 직접 구동 및 실시간 SSE 반환
      async for event in run_orchestrator_sse_async(query):
        if event:
          yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            
    except Exception as e:
      logger.error(f"[Web] Unexpected exception in local integrated agent stream generator: {e}", exc_info=True)
      yield f"data: {json.dumps({'event': 'error', 'message': f'로컬 에이전트 엔진 구동에 실패했습니다: {str(e)}'}, ensure_ascii=False)}\n\n"
      
  return StreamingResponse(event_generator(), media_type="text/event-stream")

# 루트 페이지 서빙 (static/index.html)
@app.get("/")
async def get_index():
  index_path = os.path.join(static_dir, "index.html")
  if os.path.exists(index_path):
    return FileResponse(index_path)
  return {"message": "Google Cloud QnA Web Dashboard Gateway is running locally integrated. (Please create index.html in static folder)"}

# 정적 파일 마운트
app.mount("/static", StaticFiles(directory=static_dir), name="static")

if __name__ == "__main__":
  import uvicorn
  port = int(os.environ.get("PORT", "8080"))
  logger.info(f"Starting Gateway FastAPI App on port {port}...")
  uvicorn.run("web.main:app", host="0.0.0.0", port=port, reload=True)
