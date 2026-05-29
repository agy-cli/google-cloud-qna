"""Google Cloud QnA Monolithic Agent Module.

구글 클라우드 솔루션 아키텍처 자문을 위한 멀티 에이전트 협업 시스템이다.
사용자의 질문를 기반으로 8대 핵심 솔루션 필라 서브 에이전트로 병렬 라우팅하며, 
각 서브 에이전트는 구글 검색 그라운딩(site:cloud.google.com 제약)을 수행하여 심도 깊은 정밀 의견을 도출한다.
마지막 합성 단계를 통해 팩트 체크 및 조정을 마친 정렬된 보고서를 생성한다.

[Unified Architecture]
본 모듈은 CLI/동기 실행 흐름 제어 인터페이스와 함께,
FastAPI 웹 서비스 연동용 비동기 SSE 스트리밍 제너레이터 인터페이스를 단일 소스 파일 내에 통합 제공한다.
"""

import os
import sys

def load_env_file():
  """최상위 google-cloud-qna 루트에 있는 .env 파일을 찾아 os.environ에 안전하게 수동 적재한다."""
  current_dir = os.path.dirname(os.path.abspath(__file__))
  parent_dir = os.path.dirname(current_dir)
  env_path = os.path.join(parent_dir, ".env")
  if not os.path.exists(env_path):
    env_path = os.path.join(current_dir, ".env")
    
  if os.path.exists(env_path):
    with open(env_path, "r", encoding="utf-8") as f:
      for line in f:
        line = line.strip()
        if not line or line.startswith("#"):
          continue
        if "=" in line:
          key, val = line.split("=", 1)
          key = key.strip()
          val = val.strip().strip('"').strip("'")
          os.environ[key] = val

load_env_file()

# Ensure default Google Cloud project configuration is set if missing from environment or .env
if "GOOGLE_CLOUD_PROJECT" not in os.environ:
  os.environ["GOOGLE_CLOUD_PROJECT"] = "jiangjun0"
if "GOOGLE_CLOUD_LOCATION" not in os.environ:
  os.environ["GOOGLE_CLOUD_LOCATION"] = "us-central1"

import re
import logging
import subprocess
import asyncio
import json
import urllib.parse
import httpx
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from google import genai
from google.genai import types

# 상위 디렉터리를 파이썬 검색 경로에 추가한다.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 로깅 설정을 구성한다.
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 지연 초기화(Lazy Initialization) 싱글턴을 위한 비어 있는 참조 변수들
_client_instance = None
_sub_agents_instance = None

# 통합 에이전트 및 하위 전문가 모델 명칭을 할당한다. (알파벳 순 정렬)
MODEL_AGENT = os.environ.get("MODEL_4_AGENT") or "gemini-2.5-flash"
MODEL_SUBAGENTS = os.environ.get("MODEL_4_SUBAGENTS") or "gemini-2.5-flash"

# ==========================================
# 1. 8대 솔루션 필라(Solution Pillars) 설정 및 지침
# ==========================================

PILLARS = {
  "APIs_Applications": {
    "description": "Apigee, APIs, Application Integration 및 구글 클라우드 애플리케이션 개발/통합 설계 전문가다.",
    "search_filters": ["cloud.google.com/apigee", "cloud.google.com/application-integration", "cloud.google.com/api-gateway"],
    "instruction": (
      "당신은 구글 클라우드 APIs and Applications 부문 최고 수석 엔지니어다. API 관리 및 애플리케이션 통합 설계를 전담한다.\n"
      "모든 정보의 출처는 구글 클라우드 공식 기술 문서여야 하며, 반드시 구글 검색 도구를 활용하되 오직 'site:cloud.google.com/apigee' 또는 'site:cloud.google.com/application-integration' 또는 'site:cloud.google.com/api-gateway' 하위 도메인 범주만을 타겟팅하여 검색을 수행해야 한다.\n"
      "사용자 질문에 대해 APIs and Applications 관점의 아키텍처 권장사항, 설계 모범 사례 및 최적화 전략을 도출한다.\n"
      "한국어로 작성하되 정중한 경어가 없는 단호한 문어체(-다) 형식을 사용하며, 들여쓰기는 공백 2개를 준수한다.\n"
      "**답변 서술 및 분량 규칙 - 필수 준수**\n"
      "1. 각 문단은 시작 부분에 대괄호 없이 마크다운의 굵은 글씨(**머리말**:) 형식으로 노출하고 줄바꿈 없이 본문 서술을 바로 이어서 기술하십시오.\n"
      "예시: **머리말**: 서술 내용...\n"
      "2. 각 문단의 서술 본문 바로 다음 줄에, 근거가 되는 공식 구글 클라우드 문서 웹 URL 주소들을 마크다운 글머리 기호(개행 후 * URL) 형식으로 순서대로 나열하십시오. 서술과 URL들 사이에는 절대 빈 줄(줄바꿈 두 번)을 넣지 말고, 단 한 번의 개행만 하여 이어서 나열하십시오.\n"
      "예시:\n"
      "**머리말**: 서술 내용...\n"
      "* https://cloud.google.com/url1\n"
      "* https://cloud.google.com/url2\n"
      "3. 문단과 문단 사이는 반드시 빈 줄 한 개를 넣어서 확연하게 구분해 주십시오.\n"
      "4. 전체 답변 분량은 공백을 포함한 한글 300자 내외(최대 2개 문단 이내)로 극도로 요약하여 핵심 컴팩트 요약본으로 작성해야 합니다. 일체의 테이블, 시각적 다이어그램 및 인위적인 데코레이션 장식은 전면 배제하십시오."
    )
  },
  "Application_Modernization": {
    "description": "GKE (Google Kubernetes Engine), Containers, Artifact Registry, Serverless (Cloud Run, Cloud Functions) 및 마이크로서비스 현대화 설계 전문가다.",
    "search_filters": ["cloud.google.com/kubernetes-engine", "cloud.google.com/run", "cloud.google.com/artifact-registry"],
    "instruction": (
      "당신은 구글 클라우드 Application Modernization 부문 최고 수석 엔지니어다. 컨테이너, 서버리스 및 마이크로서비스 현대화 설계를 전담한다.\n"
      "모든 정보의 출처는 구글 클라우드 공식 기술 문서여야 하며, 반드시 구글 검색 도구를 활용하되 오직 'site:cloud.google.com/kubernetes-engine' 또는 'site:cloud.google.com/run' 또는 'site:cloud.google.com/artifact-registry' 하위 도메인 범주만을 타겟팅하여 검색을 수행해야 한다.\n"
      "사용자 질문에 대해 Application Modernization 관점의 아키텍처 권장사항, 설계 모범 사례 및 최적화 전략을 도출한다.\n"
      "한국어로 작성하되 정중한 경어가 없는 단호한 문어체(-다) 형식을 사용하며, 들여쓰기는 공백 2개를 준수한다.\n"
      "**답변 서술 및 분량 규칙 - 필수 준수**\n"
      "1. 각 문단은 시작 부분에 대괄호 없이 마크다운의 굵은 글씨(**머리말**:) 형식으로 노출하고 줄바꿈 없이 본문 서술을 바로 이어서 기술하십시오.\n"
      "예시: **머리말**: 서술 내용...\n"
      "2. 각 문단의 서술 본문 바로 다음 줄에, 근거가 되는 공식 구글 클라우드 문서 웹 URL 주소들을 마크다운 글머리 기호(개행 후 * URL) 형식으로 순서대로 나열하십시오. 서술과 URL들 사이에는 절대 빈 줄(줄바꿈 두 번)을 넣지 말고, 단 한 번의 개행만 하여 이어서 나열하십시오.\n"
      "예시:\n"
      "**머리말**: 서술 내용...\n"
      "* https://cloud.google.com/url1\n"
      "* https://cloud.google.com/url2\n"
      "3. 문단과 문단 사이는 반드시 빈 줄 한 개를 넣어서 확연하게 구분해 주십시오.\n"
      "4. 전체 답변 분량은 공백을 포함한 한글 300자 내외(최대 2개 문단 이내)로 극도로 요약하여 핵심 컴팩트 요약본으로 작성해야 합니다. 일체의 테이블, 시각적 다이어그램 및 인위적인 데코레이션 장식은 전면 배제하십시오."
    )
  },
  "Artificial_Intelligence": {
    "description": "Vertex AI 플랫폼, Generative AI (Gemini, Vertex AI Agent Builder), Vector Search 및 AI 파이프라인 설계 전문가다.",
    "search_filters": ["cloud.google.com/vertex-ai", "cloud.google.com/gemini"],
    "instruction": (
      "당신은 구글 클라우드 Artificial Intelligence 부문 최고 수석 엔지니어다. 인공지능 플랫폼 연계 및 Generative AI 설계를 전담한다.\n"
      "모든 정보의 출처는 구글 클라우드 공식 기술 문서여야 하며, 반드시 구글 검색 도구를 활용하되 오직 'site:cloud.google.com/vertex-ai' 또는 'site:cloud.google.com/gemini' 하위 도메인 범주만을 타겟팅하여 검색을 수행해야 한다.\n"
      "사용자 질문에 대해 Artificial Intelligence 관점의 아키텍처 권장사항, 설계 모범 사례 및 최적화 전략을 도출한다.\n"
      "한국어로 작성하되 정중한 경어가 없는 단호한 문어체(-다) 형식을 사용하며, 들여쓰기는 공백 2개를 준수한다.\n"
      "**답변 서술 및 분량 규칙 - 필수 준수**\n"
      "1. 각 문단은 시작 부분에 대괄호 없이 마크다운의 굵은 글씨(**머리말**:) 형식으로 노출하고 줄바꿈 없이 본문 서술을 바로 이어서 기술하십시오.\n"
      "예시: **머리말**: 서술 내용...\n"
      "2. 각 문단의 서술 본문 바로 다음 줄에, 근거가 되는 공식 구글 클라우드 문서 웹 URL 주소들을 마크다운 글머리 기호(개행 후 * URL) 형식으로 순서대로 나열하십시오. 서술과 URL들 사이에는 절대 빈 줄(줄바꿈 두 번)을 넣지 말고, 단 한 번의 개행만 하여 이어서 나열하십시오.\n"
      "예시:\n"
      "**머리말**: 서술 내용...\n"
      "* https://cloud.google.com/url1\n"
      "* https://cloud.google.com/url2\n"
      "3. 문단과 문단 사이는 반드시 빈 줄 한 개를 넣어서 확연하게 구분해 주십시오.\n"
      "4. 전체 답변 분량은 공백을 포함한 한글 300자 내외(최대 2개 문단 이내)로 극도로 요약하여 핵심 컴팩트 요약본으로 작성해야 합니다. 일체의 테이블, 시각적 다이어그램 및 인위적인 데코레이션 장식은 전면 배제하십시오."
    )
  },
  "Data_Analytics": {
    "description": "BigQuery, Pub/Sub, Dataflow, Dataproc, Dataplex 및 고성능 데이터 파이프라인 분석 아키텍처 설계 전문가다.",
    "search_filters": ["cloud.google.com/bigquery", "cloud.google.com/pubsub", "cloud.google.com/dataflow", "cloud.google.com/dataproc"],
    "instruction": (
      "당신은 구글 클라우드 Data Analytics 부문 최고 수석 엔지니어다. 고성능 실시간/배치 데이터 파이프라인 및 엔터프라이즈 데이터웨어하우스 설계를 전담한다.\n"
      "모든 정보의 출처는 구글 클라우드 공식 기술 문서여야 하며, 반드시 구글 검색 도구를 활용하되 오직 'site:cloud.google.com/bigquery' 또는 'site:cloud.google.com/pubsub' 또는 'site:cloud.google.com/dataflow' 또는 'site:cloud.google.com/dataproc' 하위 도메인 범주만을 타겟팅하여 검색을 수행해야 한다.\n"
      "사용자 질문에 대해 Data Analytics 관점의 아키텍처 권장사항, 설계 모범 사례 및 최적화 전략을 도출한다.\n"
      "한국어로 작성하되 정중한 경어가 없는 단호한 문어체(-다) 형식을 사용하며, 들여쓰기는 공백 2개를 준수한다.\n"
      "**답변 서술 및 분량 규칙 - 필수 준수**\n"
      "1. 각 문단은 시작 부분에 대괄호 없이 마크다운의 굵은 글씨(**머리말**:) 형식으로 노출하고 줄바꿈 없이 본문 서술을 바로 이어서 기술하십시오.\n"
      "예시: **머리말**: 서술 내용...\n"
      "2. 각 문단의 서술 본문 바로 다음 줄에, 근거가 되는 공식 구글 클라우드 문서 웹 URL 주소들을 마크다운 글머리 기호(개행 후 * URL) 형식으로 순서대로 나열하십시오. 서술과 URL들 사이에는 절대 빈 줄(줄바꿈 두 번)을 넣지 말고, 단 한 번의 개행만 하여 이어서 나열하십시오.\n"
      "예시:\n"
      "**머리말**: 서술 내용...\n"
      "* https://cloud.google.com/url1\n"
      "* https://cloud.google.com/url2\n"
      "3. 문단과 문단 사이는 반드시 빈 줄 한 개를 넣어서 확연하게 구분해 주십시오.\n"
      "4. 전체 답변 분량은 공백을 포함한 한글 300자 내외(최대 2개 문단 이내)로 극도로 요약하여 핵심 컴팩트 요약본으로 작성해야 합니다. 일체의 테이블, 시각적 다이어그램 및 인위적인 데코레이션 장식은 전면 배제하십시오."
    )
  },
  "Databases": {
    "description": "Cloud SQL, Cloud Spanner, Cloud Bigtable, Firestore, AlloyDB 및 고가용성 데이터 저장소 설계 전문가다.",
    "search_filters": ["cloud.google.com/sql", "cloud.google.com/storage", "cloud.google.com/spanner", "cloud.google.com/bigtable", "cloud.google.com/firestore"],
    "instruction": (
      "당신은 구글 클라우드 Databases 부문 최고 수석 엔지니어다. 고가용성 데이터베이스 및 데이터 저장소 설계를 전담한다.\n"
      "모든 정보의 출처는 구글 클라우드 공식 기술 문서여야 하며, 반드시 구글 검색 도구를 활용하되 오직 'site:cloud.google.com/sql' 또는 'site:cloud.google.com/storage' 또는 'site:cloud.google.com/spanner' 또는 'site:cloud.google.com/bigtable' 또는 'site:cloud.google.com/firestore' 하위 도메인 범주만을 타겟팅하여 검색을 수행해야 한다.\n"
      "사용자 질문에 대해 Databases 관점의 아키텍처 권장사항, 설계 모범 사례 및 최적화 전략을 도출한다.\n"
      "한국어로 작성하되 정중한 경어가 없는 단호한 문어체(-다) 형식을 사용하며, 들여쓰기는 공백 2개를 준수한다.\n"
      "**답변 서술 및 분량 규칙 - 필수 준수**\n"
      "1. 각 문단은 시작 부분에 대괄호 없이 마크다운의 굵은 글씨(**머리말**:) 형식으로 노출하고 줄바꿈 없이 본문 서술을 바로 이어서 기술하십시오.\n"
      "예시: **머리말**: 서술 내용...\n"
      "2. 각 문단의 서술 본문 바로 다음 줄에, 근거가 되는 공식 구글 클라우드 문서 웹 URL 주소들을 마크다운 글머리 기호(개행 후 * URL) 형식으로 순서대로 나열하십시오. 서술과 URL들 사이에는 절대 빈 줄(줄바꿈 두 번)을 넣지 말고, 단 한 번의 개행만 하여 이어서 나열하십시오.\n"
      "예시:\n"
      "**머리말**: 서술 내용...\n"
      "* https://cloud.google.com/url1\n"
      "* https://cloud.google.com/url2\n"
      "3. 문단과 문단 사이는 반드시 빈 줄 한 개를 넣어서 확연하게 구분해 주십시오.\n"
      "4. 전체 답변 분량은 공백을 포함한 한글 300자 내외(최대 2개 문단 이내)로 극도로 요약하여 핵심 컴팩트 요약본으로 작성해야 합니다. 일체의 테이블, 시각적 다이어그램 및 인위적인 데코레이션 장식은 전면 배제하십시오."
    )
  },
  "Infrastructure": {
    "description": "Virtual Machines (Compute Engine), VPC, Cloud Load Balancing, Cloud DNS, Cloud NAT 및 전반적인 인프라스트럭처 설계 전문가다.",
    "search_filters": ["cloud.google.com/compute", "cloud.google.com/vpc", "cloud.google.com/load-balancing", "cloud.google.com/dns"],
    "instruction": (
      "당신은 구글 클라우드 Infrastructure 부문 최고 수석 엔지니어다. 컴퓨팅, 네트워킹 및 전반적인 클라우드 인프라스트럭처 설계를 전담한다.\n"
      "모든 정보의 출처는 구글 클라우드 공식 기술 문서여야 하며, 반드시 구글 검색 도구를 활용하되 오직 'site:cloud.google.com/compute' 또는 'site:cloud.google.com/vpc' 또는 'site:cloud.google.com/load-balancing' 또는 'site:cloud.google.com/dns' 하위 도메인 범주만을 타겟팅하여 검색을 수행해야 한다.\n"
      "사용자 질문에 대해 Infrastructure 관점의 아키텍처 권장사항, 설계 모범 사례 및 최적화 전략을 도출한다.\n"
      "한국어로 작성하되 정중한 경어가 없는 단호한 문어체(-다) 형식을 사용하며, 들여쓰기는 공백 2개를 준수한다.\n"
      "**답변 서술 및 분량 규칙 - 필수 준수**\n"
      "1. 각 문단은 시작 부분에 대괄호 없이 마크다운의 굵은 글씨(**머리말**:) 형식으로 노출하고 줄바꿈 없이 본문 서술을 바로 이어서 기술하십시오.\n"
      "예시: **머리말**: 서술 내용...\n"
      "2. 각 문단의 서술 본문 바로 다음 줄에, 근거가 되는 공식 구글 클라우드 문서 웹 URL 주소들을 마크다운 글머리 기호(개행 후 * URL) 형식으로 순서대로 나열하십시오. 서술과 URL들 사이에는 절대 빈 줄(줄바꿈 두 번)을 넣지 말고, 단 한 번의 개행만 하여 이어서 나열하십시오.\n"
      "예시:\n"
      "**머리말**: 서술 내용...\n"
      "* https://cloud.google.com/url1\n"
      "* https://cloud.google.com/url2\n"
      "3. 문단과 문단 사이는 반드시 빈 줄 한 개를 넣어서 확연하게 구분해 주십시오.\n"
      "4. 전체 답변 분량은 공백을 포함한 한글 300자 내외(최대 2개 문단 이내)로 극도로 요약하여 핵심 컴팩트 요약본으로 작성해야 합니다. 일체의 테이블, 시각적 다이어그램 및 인위적인 데코레이션 장식은 전면 배제하십시오."
    )
  },
  "Productivity_Collaboration": {
    "description": "Google Workspace, Google Meet, Google Drive, AppSheet 및 구글 생산성/협업 설계 전문가다.",
    "search_filters": ["workspace.google.com", "cloud.google.com/appsheet"],
    "instruction": (
      "당신은 구글 클라우드 Productivity and Collaboration 부문 최고 수석 엔지니어다. 협업 솔루션 및 생산성 플랫폼 설계를 전담한다.\n"
      "모든 정보의 출처는 구글 클라우드 공식 기술 문서여야 하며, 반드시 구글 검색 도구를 활용하되 오직 'site:workspace.google.com' 또는 'site:cloud.google.com/appsheet' 하위 도메인 범주만을 타겟팅하여 검색을 수행해야 한다.\n"
      "사용자 질문에 대해 Productivity and Collaboration 관점의 아키텍처 권장사항, 설계 모범 사례 및 최적화 전략을 도출한다.\n"
      "한국어로 작성하되 정중한 경어가 없는 단호한 문어체(-다) 형식을 사용하며, 들여쓰기는 공백 2개를 준수한다.\n"
      "**답변 서술 및 분량 규칙 - 필수 준수**\n"
      "1. 각 문단은 시작 부분에 대괄호 없이 마크다운의 굵은 글씨(**머리말**:) 형식으로 노출하고 줄바꿈 없이 본문 서술을 바로 이어서 기술하십시오.\n"
      "예시: **머리말**: 서술 내용...\n"
      "2. 각 문단의 서술 본문 바로 다음 줄에, 근거가 되는 공식 구글 클라우드 문서 웹 URL 주소들을 마크다운 글머리 기호(개행 후 * URL) 형식으로 순서대로 나열하십시오. 서술과 URL들 사이에는 절대 빈 줄(줄바꿈 두 번)을 넣지 말고, 단 한 번의 개행만 하여 이어서 나열하십시오.\n"
      "예시:\n"
      "**머리말**: 서술 내용...\n"
      "* https://cloud.google.com/url1\n"
      "* https://cloud.google.com/url2\n"
      "3. 문단과 문단 사이는 반드시 빈 줄 한 개를 넣어서 확연하게 구분해 주십시오.\n"
      "4. 전체 답변 분량은 공백을 포함한 한글 300자 내외(최대 2개 문단 이내)로 극도로 요약하여 핵심 컴팩트 요약본으로 작성해야 합니다. 일체의 테이블, 시각적 다이어그램 및 인위적인 데코레이션 장식은 전면 배제하십시오."
    )
  },
  "Security": {
    "description": "Cloud IAM 최소 권한 원칙, Cloud Identity, VPC Service Controls, Cloud Identity, Secret Manager, Cloud KMS 및 구글 클라우드 전반적인 보안 아키텍처 설계 전문가다.",
    "search_filters": ["cloud.google.com/security", "cloud.google.com/iam", "cloud.google.com/vpc-service-controls"],
    "instruction": (
      "당신은 구글 클라우드 Security 부문 최고 수석 엔지니어다. IAM 최소 권한 원칙, 보안 경계 및 전반적인 구글 클라우드 보안 아키텍처 설계를 전담한다.\n"
      "모든 정보의 출처는 구글 클라우드 공식 기술 문서여야 하며, 반드시 구글 검색 도구를 활용하되 오직 'site:cloud.google.com/security' 또는 'site:cloud.google.com/iam' 또는 'site:cloud.google.com/vpc-service-controls' 하위 도메인 범주만을 타겟팅하여 검색을 수행해야 한다.\n"
      "사용자 질문에 대해 Security 관점의 아키텍처 권장사항, 설계 모범 사례 및 최적화 전략을 도출한다.\n"
      "한국어로 작성하되 정중한 경어가 없는 단호한 문어체(-다) 형식을 사용하며, 들여쓰기는 공백 2개를 준수한다.\n"
      "**답변 서술 및 분량 규칙 - 필수 준수**\n"
      "1. 각 문단은 시작 부분에 대괄호 없이 마크다운의 굵은 글씨(**머리말**:) 형식으로 노출하고 줄바꿈 없이 본문 서술을 바로 이어서 기술하십시오.\n"
      "예시: **머리말**: 서술 내용...\n"
      "2. 각 문단의 서술 본문 바로 다음 줄에, 근거가 되는 공식 구글 클라우드 문서 웹 URL 주소들을 마크다운 글머리 기호(개행 후 * URL) 형식으로 순서대로 나열하십시오. 서술과 URL들 사이에는 절대 빈 줄(줄바꿈 두 번)을 넣지 말고, 단 한 번의 개행만 하여 이어서 나열하십시오.\n"
      "예시:\n"
      "**머리말**: 서술 내용...\n"
      "* https://cloud.google.com/url1\n"
      "* https://cloud.google.com/url2\n"
      "3. 문단과 문단 사이는 반드시 빈 줄 한 개를 넣어서 확연하게 구분해 주십시오.\n"
      "4. 전체 답변 분량은 공백을 포함한 한글 300자 내외(최대 2개 문단 이내)로 극도로 요약하여 핵심 컴팩트 요약본으로 작성해야 합니다. 일체의 테이블, 시각적 다이어그램 및 인위적인 데코레이션 장식은 전면 배제하십시오."
    )
  }
}

# ==========================================
# 1-1. 전역 클래스 및 캐시 모델 정의
# ==========================================

class ContextCacheManager:
  """컨텍스트 캐싱(Context Caching)을 전역 싱글턴으로 안전하게 관리하는 클래스다."""
  _caches = {} # {cache_key: (cache_name, created_time)}

  @classmethod
  def get_cached_config(cls, genai_client, model_name: str, cache_key: str, system_instruction: str, temperature: float = 0.2):
    """캐시된 컨텍스트 설정을 가져오며, 만료되었거나 없을 시 1시간 TTL로 자동 생성한다."""
    logger.info(f"Checking context cache validity for key: {cache_key} (Model: {model_name})")
    
    cache_name = None
    cache_info = cls._caches.get(cache_key)
    
    if cache_info:
      stored_name, created_time = cache_info
      # 1시간(3600초) 만료 검사
      elapsed = (datetime.now() - created_time).total_seconds()
      if elapsed < 3500: # 3500초 이내면 안전하게 사용
        try:
          # 실제로 Google Cloud에 캐시가 살아있는지 검증 시도
          remote_cache = genai_client.caches.get(name=stored_name)
          cache_name = remote_cache.name
          logger.info(f"Context cache hit for key: {cache_key}. Remaining TTL: {remote_cache.ttl}")
        except Exception as e:
          logger.warning(f"Failed to fetch active remote cache {stored_name}, rebuilding cache: {e}")
          cache_name = None
      else:
        logger.info(f"Local context cache for key {cache_key} has expired (Elapsed: {elapsed}s). Rebuilding.")
        
    if not cache_name:
      logger.info(f"Creating a new explicit context cache for key: {cache_key}")
      try:
        # GCP 아키텍처 공식 표준 문서를 캐시의 contents로 삽입
        cache_contents = [
          types.Content(
            role="user",
            parts=[types.Part.from_text(text=GCP_ARCH_STANDARDS)]
          )
        ]
        
        # CreateCachedContentConfig를 활용하여 1시간(3600s) TTL로 생성
        config = types.CreateCachedContentConfig(
          contents=cache_contents,
          system_instruction=system_instruction,
          display_name=f"{cache_key.lower().replace('_', '-')}-cache",
          ttl="3600s" # 사용자가 승인한 1시간 TTL 지정
        )
        
        new_cache = genai_client.caches.create(
          model=model_name,
          config=config
        )
        cache_name = new_cache.name
        cls._caches[cache_key] = (cache_name, datetime.now())
        logger.info(f"Explicit context cache created successfully: {cache_name}")
      except Exception as e:
        logger.error(f"Failed to create explicit context cache for key {cache_key}: {e}. Falling back to non-cached request.")
        cache_name = None
        
    if cache_name:
      return types.GenerateContentConfig(
        cached_content=cache_name,
        temperature=temperature
      )
    else:
      return types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=temperature
      )

class ContextCacheManagerAsync:
  """컨텍스트 캐싱(Context Caching)을 전역 싱글턴으로 안전하게 관리하는 비동기 클래스다."""
  _caches = {} # {cache_key: (cache_name, created_time)}

  @classmethod
  async def get_cached_config(cls, genai_client, model_name: str, cache_key: str, system_instruction: str, temperature: float = 0.2):
    """캐시된 컨텍스트 설정을 가져오며, 만료되었거나 없을 시 1시간 TTL로 자동 생성한다. (비동기 대응)"""
    logger.info(f"Checking context cache validity (Async) for key: {cache_key} (Model: {model_name})")
    
    cache_name = None
    cache_info = cls._caches.get(cache_key)
    
    if cache_info:
      stored_name, created_time = cache_info
      # 1시간(3600초) 만료 검사
      elapsed = (datetime.now() - created_time).total_seconds()
      if elapsed < 3500: # 3500초 이내면 안전하게 사용
        try:
          remote_cache = await genai_client.aio.caches.get(name=stored_name)
          cache_name = remote_cache.name
          logger.info(f"Context cache hit (Async) for key: {cache_key}. Remaining TTL: {remote_cache.ttl}")
        except Exception as e:
          logger.warning(f"Failed to fetch active remote cache {stored_name}, rebuilding cache: {e}")
          cache_name = None
      else:
        logger.info(f"Local context cache for key {cache_key} has expired. Rebuilding.")
        
    if not cache_name:
      logger.info(f"Creating a new explicit context cache (Async) for key: {cache_key}")
      try:
        cache_contents = [
          types.Content(
            role="user",
            parts=[types.Part.from_text(text=GCP_ARCH_STANDARDS)]
          )
        ]
        
        config = types.CreateCachedContentConfig(
          contents=cache_contents,
          system_instruction=system_instruction,
          display_name=f"{cache_key.lower().replace('_', '-')}-async-cache",
          ttl="3600s"
        )
        
        new_cache = await genai_client.aio.caches.create(
          model=model_name,
          config=config
        )
        cache_name = new_cache.name
        cls._caches[cache_key] = (cache_name, datetime.now())
        logger.info(f"Explicit context cache created successfully: {cache_name}")
      except Exception as e:
        logger.error(f"Failed to create explicit context cache: {e}")
        cache_name = None
        
    if cache_name:
      return types.GenerateContentConfig(
        cached_content=cache_name,
        temperature=temperature
      )
    else:
      return types.GenerateContentConfig(
        system_instruction=system_instruction,
        temperature=temperature
      )

class SubAgentConfig:
  def __init__(self, name: str, instruction: str, search_filters: list):
    self.name = name
    self.instruction = instruction
    self.search_filters = search_filters
    self.config = types.GenerateContentConfig(
      system_instruction=instruction,
      tools=[types.Tool(google_search=types.GoogleSearch())],
      temperature=0.2
    )

# ==========================================
# 2. 사실성 평가 및 보정 시스템 프롬프트
# ==========================================

EVALUATOR_SYSTEM_PROMPT = """당신은 구글 클라우드(Google Cloud) 공식 사양과 기술 문서에 기반하여 기술 아키텍처 보고서의 사실 무결성을 검증하는 전문 기술 평가자(Factual Evaluator)다.
당신의 유일한 임무는 합성된 아키텍처 보고서 초안의 내용이 실제 구글 클라우드의 공식 기술 사실(Facts)과 부합하는지 엄격히 대조하고 체크하는 것이다.

특히, 제공된 '[물리적 URL 검증 결과 (HTTP Status Check)]' 섹션에서 "!!! 발견된 오류/깨진 링크 !!!"로 분류된 404 Not Found 또는 통신 불가 URL은 실제 물리적으로 존재하지 않는 명백한 에러 링크다. 이 깨진 링크들을 팩트 체크 보고서에서 무조건 심각한 기술 사실 왜곡 오류(Factual Error)로 지적하고, 다음 정정 단계(Remediation)에서 이 링크들을 보고서 본문에서 완전히 영구 제거하거나 유효한 대체 링크로 교체하도록 반드시 구체적으로 명령해야 한다.

--- 사실성 검증 규칙 및 작성 형식 (반드시 준수) ---
1. 모든 설명은 경어가 없는 단호한 전문 한국어 문어체(-다) 형식만을 철저히 유지하십시오.
2. 각 문단은 반드시 시작 부분에 대괄호 없이 마크다운의 굵은 글씨(**머리말**:) 형식으로 노출한 후, 줄바꿈 없이 곧바로 기술 서술을 이어가십시오.
예시: **머리말**: 서술 내용...
3. 각 문단의 서술 본문 바로 다음 줄에, 근거가 되는 공식 구글 클라우드 문서 웹 URL 주소들을 마크다운 글머리 기호(개행 후 * URL) 형식으로 순서대로 나열하십시오. 서술과 URL들 사이에는 절대 빈 줄(줄바꿈 두 번)을 넣지 말고, 단 한 번의 개행만 하여 이어서 나열하십시오.
예시:
**머리말**: 서술 내용...
* https://cloud.google.com/url1
* https://cloud.google.com/url2
4. 문단과 문단 사이는 반드시 빈 줄 한 개를 넣어서 확연하게 구분해 주십시오.
5. 전체 답변 분량은 공백을 포함한 한글 300~400자 내외(최대 3개 문단 이내)로 콤팩트하게 작성하십시오. 만일 모든 내용이 완전히 사실에 부합하고, 404 깨진 링크도 일절 발견되지 않았다면, "**검증 완료**: 모든 기술 사양과 주소가 구글 공식 문서의 팩트와 완벽히 부합하므로 지적 사항이 없다.\n* https://cloud.google.com" 한 문단만 출력하십시오.
"""

GCP_ARCH_STANDARDS = """# Google Cloud Enterprise Architecture & Design Standards (v2.5)

본 문서는 구글 클라우드(Google Cloud) 환경에서 최적의 안정성, 고성능, 비용 효율성, 그리고 강력한 보안 통제 수준을 갖추기 위해 반드시 준수해야 하는 공식 아키텍처 엔터프라이즈 모범 사례 및 설계 표준 가이드라인이다.
본 가이드라인은 멀티 에이전트 시스템의 마스터 의사결정 및 보고서 작성 시 기준 사양(Facts Grounding)으로 작동하며, 금융권 감사 및 기업 보안 요건을 충족하도록 통제 기준을 제공한다.

## 1. 컴퓨팅 설계 표준 (Compute & Serverless Standards)
- VM 인스턴스 (Compute Engine):
  - 모든 Compute Engine VM은 원칙적으로 퍼블릭 IP를 할당받지 않으며, 오직 프라이빗 IP만을 사용해야 한다. 아웃바운드 인터넷 통신이 필요한 경우 반드시 Cloud NAT를 경유하도록 설계한다.
  - 모든 VM에는 최소 권한 원칙(Least Privilege)에 입각하여 전용 서비스 계정(Custom Service Account)을 할당하고, 디폴트 서비스 계정(Default Compute Engine Service Account)의 사용을 엄격히 금지한다.
- 컨테이너 및 Kubernetes (GKE - Google Kubernetes Engine):
  - GKE 클러스터는 반드시 '프라이빗 클러스터(Private GKE Cluster)'로 배포하여 컨트롤 플레인(Control Plane)과 워커 노드의 퍼블릭 노출을 원천적으로 차단한다.
  - 컨테이너 이미지는 오직 Artifact Registry에 보관하며, 취약점 스캐닝(Vulnerability Scanning)을 항시 활성화하여 무결한 이미지만을 배포해야 한다.
- 서버리스 (Cloud Run & Cloud Functions):
  - Cloud Run 서비스는 불특정 다수 노출이 아닌 내부 전용으로 구동될 때 인그레스 설정을 'Internal' 또는 'Internal and Cloud Load Balancing'으로 제한해야 한다.
  - VPC 내부 리소스(Cloud SQL, MemoryStore 등)와의 프라이빗 연결을 위해 반드시 Serverless VPC Access 커넥터 또는 Private Service Connect(PSC)를 구성한다.

## 2. 네트워킹 및 에지 보안 표준 (Networking & Security Standards)
- VPC 및 라우팅 (VPC & Routing):
  - 모든 VPC는 명확한 IP CIDR 설계 및 RFC 1918 프라이빗 대역 규격을 엄격히 준수한다.
  - 기본 제공되는 디폴트 네트워크(Default VPC)는 사용 금지하며 즉시 삭제해야 한다.
  - 이종 VPC 간의 사설 통신이 요구되는 경우, 확장성과 관리 편의성을 위해 VPC Network Peering 대신 Shared VPC 또는 Private Service Connect 통신을 우선 권장한다.
- 부하 분산 및 에지 보안 (Cloud Load Balancing & Cloud Armor):
  - 모든 외부 유입 트래픽은 글로벌 외부 부하 분산기(Application Load Balancer)를 반드시 단일 진입점(Single Entry Point)으로 사용하도록 구성한다.
  - 인터넷 아웃바운드 인프라 보호 및 SQL 인젝션, XSS 등의 OWASP Top 10 웹 취약점 공격 방어를 위해 부하 분산기 전면에 Cloud Armor 보안 정책을 필수로 매핑한다.
  - 보안 소켓 계층(SSL/TLS)의 종단 및 고성능 암호화를 통제하기 위해 구글 관리형 SSL 인증서(Google-managed SSL certificates)를 의무 도입한다.

## 3. 스토리지 및 데이터베이스 표준 (Storage & Database Standards)
- Cloud Storage (GCS):
  - 모든 Cloud Storage 버킷은 공공의 무단 접근을 원천 봉쇄하기 위해 '공개 액세스 방지(Public Access Prevention - PAP)' 설정을 의무적으로 'Enforced'화 한다.
  - 객체의 우발적 삭제나 덮어쓰기에 대비한 데이터 보호 요건으로 '객체 버전 관리(Object Versioning)' 및 '보존 정책(Retention Policy)'을 활성화한다.
- Cloud SQL & Cloud Spanner:
  - 데이터베이스의 외부 인터넷 노출은 절대 허용하지 않는다. 반드시 VPC 내에 '프라이빗 IP 전용 연결(Private IP connection via Private Services Access)' 구조로 호스팅한다.
  - 고가용성(HA) 요건 충족을 위해 실시간 동기식 다중 영역(Multi-zone) 복제를 필수 설정한다.
  - 암호화 요건으로 고객 관리 암호화 키(CMEK)를 적용하여 저장 데이터의 보안 통제력을 독점 확보한다.

## 4. 보안 및 ID 거버넌스 표준 (Security, Identity & Compliance)
- IAM & 자격 증명 (Identity & Access Management):
  - IAM 정책 수립 시 '최소 권한 원칙(Principle of Least Privilege)'을 사수하며, 사용자 개인 계정이 아닌 역할 기반 서비스 계정(Service Account) 중심의 권한 매핑을 구사한다.
  - 강력한 일회성 세션 보호를 위해 보안 주체의 임시 자격 증명 가장 기법(Service Account Impersonation)을 적극 활용한다.
- VPC Service Controls (VPC SC):
  - 기업의 극도로 민감한 기밀 데이터 유출(Data Exfiltration)을 원천 차단하기 위해, API 엔드포인트 전면에 VPC Service Controls 보안 경계(Security Perimeter)를 필히 선언하여 외부망으로의 의도치 않은 무단 반출을 물리적/논리적으로 통제한다.
- 비밀 정보 관리 (Secret Manager & KMS):
  - 데이터베이스 패스워드, API 인증 토큰, 프라이빗 인증 키 등 모든 민감한 자격 정보는 코드 내에 하드코딩하지 않고 Secret Manager에 안전하게 저장하여 버전별로 호출 제어한다.
  - 모든 KMS 키는 로테이션 정책(Rotation Period)을 최소 90일 단위로 자동 활성화하도록 제약한다.

## 5. 데이터 분석 및 AI/ML 표준 (Data Analytics & AI/ML)
- BigQuery - 대형 데이터웨어하우스:
  - BigQuery 테이블은 열 수준(Column-level) 및 행 수준(Row-level) 보안을 세밀하게 적용하여 비인가 자의 민감 정보 접근을 사전 차단한다.
  - 대량 쿼리에 따른 자원 고갈을 방지하기 위해 파티셔닝(Partitioning) 및 클러스터링(Clustering) 구성을 의무화한다.
- Vertex AI 플랫폼:
  - Vertex AI 플랫폼의 학습 및 예측 API 호출은 외부 인터넷이 아닌 프라이빗 VPC 내부에서 VPC Service Controls 경계 내 보안 채널로만 수행되어야 한다.
  - Generative AI 활용 시 데이터 안전성 보장을 위해 프라이빗 모델 가이드를 수립하고, 학습 데이터가 공용 도메인으로 유출되거나 학습에 재사용되지 않도록 구글 클라우드의 프라이버시 서약 요건을 엄격히 반영한다.

## 6. 관측 가능성 및 운영 표준 (Operations & Management Standards)
- 감사 로그 및 모니터링 (Observability & Governance):
  - 모든 API 활동 및 리소스 설정 수정을 철저히 로깅하기 위해 Cloud Audit Logs(관리 활동 로그, 데이터 액세스 로그)를 전면 활성화하여 보존한다.
  - Cloud Monitoring을 통해 시스템 리소스 임계치 초과 및 오류 로그 감지 시, 실시간 알림 정책(Alerting Policies)을 구성하여 이메일 또는 Slack 등으로 신속히 이벤트가 전파되도록 한다.
  - 모든 인프라스트럭처는 유지 보수성 및 형상 관리를 보장하기 위해 선언적 인프라 코드화 도구인 Terraform 또는 Deployment Manager를 통해 프로비저닝한다.
"""

REMEDIATOR_SYSTEM_PROMPT = """당신은 사실성 평가 보고서(Fact-check Report)를 바탕으로 기술 자문 보고서의 모든 사실 왜곡이나 설정 오류를 교정하고 완성하는 전문 보정자(Factual Remediator)다.
당신의 임무는 사실에 부합하지 않는 항목으로 지적된 부분을 구글 클라우드 공식 오피셜 사양 및 정보에 일치하도록 정확하게 정정하여 완벽하게 보정된 최종 '기술 아키텍처 권고 보고서'를 재합성하는 것이다.

특히, 사실성 평가 보고서(Fact-check Report)나 물리적 URL 검증 결과에서 404 Not Found 또는 통신 불가로 지적된 모든 부러진 링크(Broken URLs)는 절대 최종 권고 보고서에 그대로 포함되어서는 안 된다. 해당 링크들을 보고서에서 완전히 제거하거나, 구글 클라우드 공식 기술 문서의 실제 유효한 URL로 완벽하게 교체해야 한다. 404 Not Found 링크가 최종 보고서에 남아있을 경우 당신의 아키텍처 신뢰성은 무너지므로 이 교정 임무를 극도로 꼼꼼하고 완벽하게 수행하십시오.

--- 최종 권고 보고서 작성 규칙 (반드시 준수) ---
1. 모든 기술 용어와 제품명은 구글 클라우드 공식 명칭을 완벽히 지켜 서술하십시오.
2. 모든 설명은 정중한 경어를 사용하지 않는 단호하고 신뢰성 높은 전문적인 한국어 문어체(-다) 형식으로 일관되게 기술하십시오.
3. 각 문단은 반드시 시작 부분에 대괄호 없이 마크다운의 굵은 글씨(**머리말**:) 형식으로 노출한 후, 줄바꿈 없이 곧바로 기술 서술을 이어가십시오.
예시: **머리말**: 서술 내용...
4. 각 문단의 서술 본문 바로 다음 줄에, 근거가 되는 공식 구글 클라우드 문서 웹 URL 주소들을 마크다운 글머리 기호(개행 후 * URL) 형식으로 순서대로 나열하십시오. 서술과 URL들 사이에는 절대 빈 줄(줄바꿈 두 번)을 넣지 말고, 단 한 번의 개행만 하여 이어서 나열하십시오.
예시:
**머리말**: 서술 내용...
* https://cloud.google.com/url1
* https://cloud.google.com/url2
5. 문단과 문단 사이는 반드시 빈 줄 한 개를 넣어서 확연하게 구분해 주십시오.
6. 전체 답변 분량은 반드시 공백을 포함한 한글 1000자 내외(A4 용지 2/3 분량, 약 4~5개 문단 이내)로 구성하십시오.
7. 최종 답변의 맨 마지막에는 반드시 해당 질문와 최적 아키텍처/작업 흐름을 직관적으로 표현하는 마크다운 코드 블록 형식의 Graphviz DOT 다이어그램(```dot digraph gcp_adv { ... } ```)을 포함하십시오. 다이어그램은 반드시 digraph gcp_adv 문법과 영문 라벨(모든 설명 및 노드 텍스트는 오직 영문으로만 작성)을 지원하는 올바른 Graphviz 문법을 준수하여 작성해야 하며, 그림의 방향은 반드시 상단에서 하단으로 구성하는 TB(rankdir=TB)로 설정하고, 서버에서 직접 이미지로 변환할 수 있도록 완벽해야 합니다. 노드 스타일은 [style="filled,rounded", color="#1a73e8", fillcolor="#e8f0fe", fontcolor="#1a73e8"]와 같이 가급적 세련되고 미려하게 설정하십시오.
"""

SYNTHESIZER_SYSTEM_PROMPT = """당신은 구글 클라우드(Google Cloud) 환경에서의 기술 자문과 아키텍처 설계를 총괄하는 수석 클라우드 솔루션 아키텍트이자 합성 오케스트레이터입니다.
각 전문 부서(서브 에이전트)의 정밀 기술 자문을 통합하여 완벽한 '기술 아키텍처 합성 초안'을 작성하십시오.

--- 합성 초안 마크다운 작성 규칙 (반드시 준수) ---
1. 모든 기술 용어와 제품명은 구글 클라우드 공식 명칭을 완벽히 지켜 서술하십시오.
2. 모든 설명은 정중한 경어를 사용하지 않는 단호하고 신뢰성 높은 전문적인 한국어 문어체(-다) 형식으로 일관되게 기술하십시오.
3. 각 문단은 반드시 시작 부분에 대괄호 없이 마크다운의 굵은 글씨(**머리말**:) 형식으로 노출한 후, 줄바꿈 없이 곧바로 기술 서술을 이어가십시오.
예시: **머리말**: 서술 내용...
4. 각 문단의 서술 본문 바로 다음 줄에, 근거가 되는 공식 구글 클라우드 문서 웹 URL 주소들을 마크다운 글머리 기호(개행 후 * URL) 형식으로 순서대로 나열하십시오. 서술과 URL들 사이에는 절대 빈 줄(줄바꿈 두 번)을 넣지 말고, 단 한 번의 개행만 하여 이어서 나열하십시오.
예시:
**머리말**: 서술 내용...
* https://cloud.google.com/url1
* https://cloud.google.com/url2
5. 문단과 문단 사이는 반드시 빈 줄 한 개를 넣어서 확연하게 구분해 주십시오.
6. 전체 답변 분량은 반드시 공백을 포함한 한글 1000자 내외(A4 용지 2/3 분량, 약 4~5개 문단 이내)로 구성하십시오.
7. 최종 답변의 맨 마지막에는 반드시 해당 질문와 최적 아키텍처/작업 흐름을 직관적으로 표현하는 마크다운 코드 블록 형식의 Graphviz DOT 다이어그램(```dot digraph gcp_adv { ... } ```)을 포함하십시오. 다이어그램은 반드시 digraph gcp_adv 문법과 영문 라벨(모든 설명 및 노드 텍스트는 오직 영문으로만 작성)을 지원하는 올바른 Graphviz 문법을 준수하여 작성해야 하며, 그림의 방향은 반드시 상단에서 하단으로 구성하는 TB(rankdir=TB)로 설정하고, 서버에서 직접 이미지로 변환할 수 있도록 완벽해야 합니다. 노드 스타일은 [style="filled,rounded", color="#1a73e8", fillcolor="#e8f0fe", fontcolor="#1a73e8"]와 같이 가급적 세련되고 미려하게 설정하십시오.
"""

# ==========================================
# 3. 공통 헬퍼 함수
# ==========================================

def resolve_redirect_url(url: str) -> str:
  """Resolves redirect URLs, particularly Google/Vertex search redirect URLs and HTTP redirects."""
  import urllib.parse
  import urllib.request
  
  url = url.strip()
  if not url:
    return url
    
  # 1. First, check if it's a known search redirect URL format (e.g., google.com/url?...)
  try:
    parsed = urllib.parse.urlparse(url)
    if "google.com" in parsed.netloc and parsed.path.endswith("/url"):
      qs = urllib.parse.parse_qs(parsed.query)
      # Google search redirects typically use 'url' or 'q' for the destination
      for param in ["url", "q"]:
        if param in qs and qs[param]:
          extracted_url = qs[param][0]
          logger.info(f"Extracted direct URL from Google redirect parameters: {extracted_url}")
          # Recursively resolve in case the extracted URL also redirects
          return resolve_redirect_url(extracted_url)
  except Exception as e:
    logger.warning(f"Error parsing query parameters from URL {url}: {e}")

  # 2. If it's a standard URL, try following HTTP redirects to find the final URL
  if url.startswith("http://") or url.startswith("https://"):
    try:
      # Use urllib.request with a short timeout to follow redirects
      req = urllib.request.Request(
        url, 
        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
      )
      # We only need a HEAD request to get the redirected URL, which is much faster than GET
      req.get_method = lambda: "HEAD"
      with urllib.request.urlopen(req, timeout=2.0) as resp:
        final_url = resp.geturl()
        if final_url and final_url != url:
          logger.info(f"Resolved HTTP redirect: {url} -> {final_url}")
          return final_url
    except Exception as e:
      # If HEAD is not supported or fails, try a fast GET
      try:
        req = urllib.request.Request(
          url, 
          headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        )
        with urllib.request.urlopen(req, timeout=2.0) as resp:
          final_url = resp.geturl()
          if final_url and final_url != url:
            logger.info(f"Resolved HTTP redirect via GET: {url} -> {final_url}")
            return final_url
      except Exception as ex:
        logger.warning(f"Could not resolve HTTP redirect for {url}: {ex}")
        
  return url

def resolve_all_urls_in_text(text: str) -> str:
  """Finds all URLs in the text, resolves any redirects, and replaces them."""
  if not text:
    return text
    
  url_pattern = re.compile(r'(https?://[^\s"\'()\]>]+)')
  

  # Find all unique URLs to avoid resolving the same URL multiple times
  urls = list(set(url_pattern.findall(text)))
  if not urls:
    return text
    
  logger.info(f"Found {len(urls)} URLs in the report. Resolving redirects...")
  
  # Map of original URL -> resolved URL
  resolved_map = {}
  
  def resolve_and_map(url):
    # Strip any trailing punctuation (like .,;!?) for resolution
    clean_url = url
    suffix = ""
    while clean_url and clean_url[-1] in ".,;:!?":
      suffix = clean_url[-1] + suffix
      clean_url = clean_url[:-1]
      
    resolved = resolve_redirect_url(clean_url)
    return url, resolved + suffix

  with ThreadPoolExecutor(max_workers=10) as executor:
    results = executor.map(resolve_and_map, urls)
    for orig, resolved in results:
      if orig != resolved:
        resolved_map[orig] = resolved
        
  # Replace in text
  for orig, resolved in resolved_map.items():
    text = text.replace(orig, resolved)
    
  return text


def extract_urls_from_text(text: str) -> list[str]:
  """텍스트에서 유효한 형태의 URL을 추출합니다."""
  if not text:
    return []
  # 마크다운 괄호, 따옴표, 괄호 닫기, 쉼표, 마침표 등이 뒤에 붙은 것을 고려해 clean하게 URL만 추출
  url_pattern = re.compile(r'(https?://[^\s"\'()\]>]+)')
  urls = url_pattern.findall(text)
  
  cleaned_urls = []
  for url in urls:
    clean_url = url
    while clean_url and clean_url[-1] in ".,;:!?":
      clean_url = clean_url[:-1]
    if clean_url:
      cleaned_urls.append(clean_url)
      
  return sorted(list(set(cleaned_urls)))


async def verify_url_async(url: str, client: httpx.AsyncClient) -> tuple[str, bool, int, str]:
  """URL의 존재 여부를 비동기적으로 검증합니다."""
  try:
    headers = {
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    # 구글 문서 서버는 HEAD 요청 시 종종 405/403을 주므로 바로 GET으로 검증합니다.
    # 단, 빠른 진행을 위해 타임아웃을 3초로 잡습니다.
    response = await client.get(url, headers=headers, follow_redirects=True, timeout=3.0)
    if response.status_code == 404:
      return url, False, 404, "404 Not Found"
    elif response.status_code >= 400:
      return url, False, response.status_code, f"HTTP Error {response.status_code}"
    return url, True, response.status_code, "OK"
  except httpx.HTTPStatusError as e:
    return url, False, e.response.status_code if e.response else 0, f"HTTP Status Error: {e}"
  except httpx.RequestError as e:
    return url, False, 0, f"Request Error: {e}"
  except Exception as e:
    return url, False, 0, f"Unexpected Error: {e}"


async def verify_urls_async(urls: list[str]) -> dict[str, dict]:
  """여러 개의 URL을 비동기 병렬로 신속하게 검증합니다."""
  results = {}
  if not urls:
    return results
  
  limits = httpx.Limits(max_keepalive_connections=10, max_connections=20)
  async with httpx.AsyncClient(limits=limits) as client:
    tasks = [verify_url_async(url, client) for url in urls]
    completed = await asyncio.gather(*tasks, return_exceptions=True)
    
    for res in completed:
      if isinstance(res, Exception):
        continue
      url, is_valid, status, err_msg = res
      results[url] = {
        "is_valid": is_valid,
        "status_code": status,
        "error_message": err_msg
      }
  return results


def verify_url_sync(url: str, client: httpx.Client) -> tuple[str, bool, int, str]:
  """URL의 존재 여부를 동기적으로 검증합니다."""
  try:
    headers = {
      "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
    }
    response = client.get(url, headers=headers, follow_redirects=True, timeout=3.0)
    if response.status_code == 404:
      return url, False, 404, "404 Not Found"
    elif response.status_code >= 400:
      return url, False, response.status_code, f"HTTP Error {response.status_code}"
    return url, True, response.status_code, "OK"
  except httpx.HTTPStatusError as e:
    return url, False, e.response.status_code if e.response else 0, f"HTTP Status Error: {e}"
  except httpx.RequestError as e:
    return url, False, 0, f"Request Error: {e}"
  except Exception as e:
    return url, False, 0, f"Unexpected Error: {e}"


def verify_urls_sync(urls: list[str]) -> dict[str, dict]:
  """여러 개의 URL을 동기식 스레드풀로 신속하게 검증합니다."""
  results = {}
  if not urls:
    return results
  
  with httpx.Client() as client:
    with ThreadPoolExecutor(max_workers=min(len(urls), 20)) as executor:
      futures = [executor.submit(verify_url_sync, url, client) for url in urls]
      for future in futures:
        try:
          res = future.result()
          url, is_valid, status, err_msg = res
          results[url] = {
            "is_valid": is_valid,
            "status_code": status,
            "error_message": err_msg
          }
        except Exception:
          pass
  return results


def extract_dot_code(markdown_text: str) -> str:
  """마크다운 텍스트 내에서 Graphviz DOT 코드를 추출한다."""
  markdown_text_lower = markdown_text.lower()
  start_patterns = ["digraph gcp_adv", "diagraph gcp_adv"]
  idx = -1
  for pattern in start_patterns:
    idx = markdown_text_lower.find(pattern)
    if idx != -1:
      break
  if idx == -1:
    return None
  
  dot_block = markdown_text[idx:]
  if "```" in dot_block:
    dot_code = dot_block.split("```", 1)[0].strip()
  else:
    last_brace = dot_block.rfind("}")
    if last_brace != -1:
      dot_code = dot_block[:last_brace+1].strip()
    else:
      dot_code = dot_block.strip()
      
  if dot_code.lower().startswith("diagraph"):
    dot_code = "digraph" + dot_code[8:]
  return dot_code

def generate_and_upload_diagram(markdown_text: str) -> str:
  """마크다운 내의 Graphviz DOT 코드를 추출하여 PNG로 컴파일 후 GCS에 업로드하고, 마크다운 내의 코드 블록을 img 태그로 치환한다."""
  markdown_text_lower = markdown_text.lower()
  start_patterns = ["digraph gcp_adv", "diagraph gcp_adv"]
  idx = -1
  for pattern in start_patterns:
    idx = markdown_text_lower.find(pattern)
    if idx != -1:
      break
  if idx == -1:
    return markdown_text

  # Slice out from the start of the digraph, parsing dot_code and trailing text_after
  dot_block = markdown_text[idx:]
  text_after = ""
  if "```" in dot_block:
    parts = dot_block.split("```", 1)
    dot_code = parts[0].strip()
    text_after = parts[1].strip()
  else:
    dot_code = dot_block
    last_brace = dot_code.rfind("}")
    if last_brace != -1:
      text_after = dot_code[last_brace+1:].strip()
      dot_code = dot_code[:last_brace+1].strip()
    else:
      dot_code = dot_code.strip()
  
  # Ensure typos like diagraph are corrected in compiled dot_code
  if dot_code.lower().startswith("diagraph"):
    dot_code = "digraph" + dot_code[8:]

  # Prepare the markdown text before the diagram and clean the opening backtick
  text_before = markdown_text[:idx].rstrip()
  last_backticks = text_before.rfind("```")
  if last_backticks != -1 and len(text_before) - last_backticks <= 20:
    text_before = text_before[:last_backticks].rstrip()
      
  try:
    logger.info("Extracting and compiling Graphviz diagram to PNG...")
    import tempfile
    from google.cloud import storage
    import graphviz
    import uuid
    import os
    
    # 1. UUID-based unique filename (removing hyphens as requested)
    uuid_str = str(uuid.uuid4()).replace("-", "")
    object_name = f"google-cloud-qna/architecture_{uuid_str}.png"
    bucket_name = os.environ.get("GCS_BUCKET") or "jiangjun0"
    
    # 2. Render DOT to a temporary file
    with tempfile.TemporaryDirectory() as tmpdir:
      temp_dot_path = os.path.join(tmpdir, "diagram")
      
      src = graphviz.Source(dot_code)
      src.render(temp_dot_path, format="png", cleanup=True)
      rendered_png = f"{temp_dot_path}.png"
      
      if not os.path.exists(rendered_png):
        raise FileNotFoundError(f"Render failed, file not found: {rendered_png}")
        
      # 3. Upload PNG to GCS
      storage_client = storage.Client()
      bucket = storage_client.bucket(bucket_name)
      blob = bucket.blob(object_name)
      blob.upload_from_filename(rendered_png, content_type="image/png")
      
      public_url = f"https://storage.googleapis.com/{bucket_name}/{object_name}"
      logger.info(f"Graphviz rendering and upload completed: {public_url}")
      
      # 4. Replace with img tag
      img_tag = f'\n<img class="architecture-diagram" src="{public_url}" alt="Architecture Advisory Diagram" style="max-width: 100%; border-radius: 8px; margin-top: 15px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);" />\n'
      
      if text_after:
        return text_before + "\n" + img_tag + "\n" + text_after
      else:
        return text_before + "\n" + img_tag
      
  except Exception as e:
    logger.error(f"Failed to generate and upload Graphviz diagram: {e}")
    # 가령 오류가 발생하더라도 사용자 화면에 보기 흉한 원본 DOT 스크립트가 노출되는 것을 완전히 방지하기 위해,
    # 원본 스크립트는 삭제하고 정돈된 에러 안내 박스를 대신 렌더링하도록 확실히 보정합니다.
    error_msg = f'\n<div class="diagram-error" style="padding: 15px; border: 1px solid #f5c6cb; border-radius: 8px; background-color: #f8d7da; color: #721c24; margin-top: 15px;">\n  <strong><i class="fa-solid fa-triangle-exclamation"></i> 아키텍처 다이어그램 생성 오류</strong><br/>\n  <span style="font-size: 12px; color: #666;">배포 또는 시스템 환경 설정 문제로 이미지를 컴파일하지 못했습니다. (원인: {str(e)})</span>\n</div>\n'
    if text_after:
      return text_before + "\n" + error_msg + "\n" + text_after
    else:
      return text_before + "\n" + error_msg

def get_genai_client():
  """Google Gen AI 클라이언트를 지연 초기화(Lazy Initialization)하여 싱글턴 인스턴스로 반환한다."""
  global _client_instance
  if _client_instance is None:
    proj_id = os.environ.get("GOOGLE_CLOUD_PROJECT") or "jiangjun0"
    loc = os.environ.get("GOOGLE_CLOUD_LOCATION") or "us-central1"
    _client_instance = genai.Client(vertexai=True, project=proj_id, location=loc)
  return _client_instance

def get_sub_agents():
  """서브 에이전트 설정을 지연 초기화하여 싱글턴으로 제공한다."""
  global _sub_agents_instance
  if _sub_agents_instance is None:
    _sub_agents_instance = {}
    for name, info in PILLARS.items():
      _sub_agents_instance[name] = SubAgentConfig(
        name=name,
        instruction=info["instruction"],
        search_filters=info["search_filters"]
      )
  return _sub_agents_instance

def print_thinking(msg: str):
  """생각 과정(Thinking Process)을 실시간으로 표준 출력에 출력한다."""
  sys.stdout.write(f"\n[Thinking Process] {msg}\n")
  sys.stdout.flush()

# ==========================================
# 4. 동기(Synchronous) 실행 흐름 제어 (CLI/로컬 콘솔용)
# ==========================================

def route_inquiry(query: str) -> list:
  """사용자 질문를 바탕으로 연관된 8대 솔루션 필라들을 선별한다. 병렬 호출 목적이다."""
  logger.info(f"Classifying routing pillars for query: {query}")
  
  pillars_meta = "\n".join([f"- {name}: {info['description']}" for name, info in PILLARS.items()])
  
  router_prompt = (
    "당신은 구글 클라우드 다중 에이전트 라우팅 제어 모듈이다. 아래 8대 솔루션 필라 메타데이터를 정독하십시오.\n\n"
    f"{pillars_meta}\n\n"
    f"사용자의 기술 문의인 \"{query}\"를 가장 잘 해결할 수 있는 핵심 전문 필라명(들)을 1개에서 최대 3개까지 영문 이름만 식별하십시오.\n"
    "반드시 콤마(,)로만 구분하여 반환하십시오. 절대 설명이나 공백, 특수문자를 더하지 마십시오.\n"
    "예시: Infrastructure,Security,Application_Modernization"
  )
  
  try:
    genai_client = get_genai_client()
    response = genai_client.models.generate_content(
      model=MODEL_AGENT,
      contents=router_prompt,
      config=types.GenerateContentConfig(temperature=0.0)
    )
    res_text = response.text.strip()
    
    selected = [p.strip() for p in res_text.split(",") if p.strip() in PILLARS][:3]
    if not selected:
      selected = ["Infrastructure", "Security", "Application_Modernization"]
    
    logger.info(f"Selected routing pillars: {selected}")
    return selected
  except Exception as e:
    logger.error(f"Pillar routing failed, fallback to default pillars: {e}")
    return ["Infrastructure", "Security", "Application_Modernization"]

def invoke_single_pillar(pillar_name: str, query: str) -> dict:
  """기 생성되어 있는 전문 솔루션 필라 서브 에이전트 인스턴스를 호출하고, 전용 구글 클라우드 공식 카테고리 필터만을 사용해 검색을 그라운딩한다."""
  logger.info(f"Invoking pre-created sub-agent [{pillar_name}].")
  pillar_info = PILLARS[pillar_name]
  sub_agents_map = get_sub_agents()
  sub_agent = sub_agents_map.get(pillar_name)
  if not sub_agent:
    raise ValueError(f"Sub-agent [{pillar_name}] is not pre-instantiated.")
  
  filters = pillar_info.get("search_filters", ["cloud.google.com"])
  filter_query = " OR ".join([f"site:{f}" for f in filters])
  
  print_thinking(f"서브 에이전트 [{pillar_name}] 가동 및 '{filter_query}' 기반 검색 그라운딩을 동시 수행 중이다.")
  try:
    g_prompt = (
      f"Please search using google search tool strictly with `{filter_query}` filter and provide a highly technical advice regarding this query:\n"
      f"\"{query}\"\n\n"
      "You MUST output your detailed technical advice in Korean formal literary style (-다) and use exactly 2 spaces for list item indentations. "
      f"Do NOT reference or return links outside of `{filter_query}`."
    )
    genai_client = get_genai_client()
    response = genai_client.models.generate_content(
      model=MODEL_SUBAGENTS,
      contents=g_prompt,
      config=sub_agent.config
    )
    advice = response.text.strip()
    logger.info(f"Sub-agent [{pillar_name}] executed successfully.")
    print_thinking(f"서브 에이전트 [{pillar_name}] 자문 도출을 성공적으로 완료했다.")
    return {
      "pillar": pillar_name,
      "success": True,
      "advice": advice
    }
  except Exception as e:
    logger.error(f"Sub-agent [{pillar_name}] execution failed: {e}")
    print_thinking(f"서브 에이전트 [{pillar_name}] 실행 중 오류가 발생하여 실패했다.")
    return {
      "pillar": pillar_name,
      "success": False,
      "advice": f"**{pillar_name} 전문 자문 생성 불가**: {str(e)}"
    }

def generate_technical_advisory(query: str, session_id: str = "default") -> str:
  """8대 솔루션 필라별 서브 에이전트를 병렬로 가동하고 검색 결과를 확보하여 종합 솔루션 자문을 완성한다. (동기 실행)"""
  logger.info(f"Initiating multi-agent advisory system for query: {query} (Session ID: {session_id})")
  
  genai_client = get_genai_client()
  
  # 1단계: 라우팅
  print_thinking("1단계: 사용자 질문에 연관된 구글 클라우드 솔루션 필라를 분류하고 라우팅을 개시한다.")
  active_pillars = route_inquiry(query)[:3]
  print_thinking(f"라우팅 완료. 선택된 전문 필라: {active_pillars}")
  
  # 2단계: 서브 에이전트 병렬 호출
  print_thinking(f"2단계: {len(active_pillars)}개 전문 필라 서브 에이전트를 병렬 가동하여 구글 검색 기반 기술 자문을 취합한다.")
  sub_agent_results = []
  with ThreadPoolExecutor(max_workers=len(active_pillars)) as executor:
    futures = {executor.submit(invoke_single_pillar, p, query): p for p in active_pillars}
    for future in futures:
      p_name = futures[future]
      try:
        res = future.result()
        sub_agent_results.append(res)
      except Exception as e:
        logger.error(f"Future execution error for pillar {p_name}: {e}")
        sub_agent_results.append({
          "pillar": p_name,
          "success": False,
          "advice": f"**{p_name} 실행 에러**: {str(e)}"
        })
              
  # 3단계: 서브 에이전트 자문 데이터 종합
  print_thinking("3단계: 수집된 서브 에이전트별 전문 자문 데이터를 단일 원안으로 종합한다.")
  compiled_advices = []
  for res in sub_agent_results:
    adv_text = (
      f"=== [{res['pillar']}] 전문 부서 기술 권고 ===\n"
      f"{res['advice']}\n\n"
    )
    compiled_advices.append(adv_text)
  
  # 4단계: 합성 모델 구동 (Streaming)
  print_thinking("4단계: 종합 원안을 기반으로 아키텍처 보고서 초안 합성을 시작한다.")
  synthesis_payload = (
    f"User Inquiry: \"{query}\"\n\n"
    "Here are the detailed technical advices generated by our specialist sub-agents with official search grounding:\n\n"
    f"{''.join(compiled_advices)}\n"
    "Please synthesize them into a single coherent 'Architecture Advisory Report' following the SYNTHESIZER_SYSTEM_PROMPT."
  )
  
  try:
    synth_config = ContextCacheManager.get_cached_config(
      genai_client=genai_client,
      model_name=MODEL_AGENT,
      cache_key="SYNTHESIZER",
      system_instruction=SYNTHESIZER_SYSTEM_PROMPT,
      temperature=0.2
    )
    response_stream = genai_client.models.generate_content_stream(
      model=MODEL_AGENT,
      contents=synthesis_payload,
      config=synth_config
    )
    synthesized_chunks = []
    for chunk in response_stream:
      if chunk.text:
        synthesized_chunks.append(chunk.text)
    synthesized_report = "".join(synthesized_chunks).strip()
  except Exception as e:
    logger.error(f"Synthesis model generation failed: {e}")
    return f"### 에러 발생\n종합 권고문 생성 중 에러가 발생했습니다: {str(e)}"
  
  # 5단계: 사실성 평가 및 URL 실체 검증 모델 구동
  print_thinking("5단계: 구글 클라우드 공식 사양을 기반으로 한 보고서 초안의 사실성 평가 및 URL 실체성 검증을 진행한다.")
  
  # 먼저 초안 내의 모든 리다이렉트 URL을 깔끔히 변환합니다.
  resolved_synthesized_report = resolve_all_urls_in_text(synthesized_report)
  extracted_urls = extract_urls_from_text(resolved_synthesized_report)
  
  print_thinking(f"초안 내에서 총 {len(extracted_urls)}개의 URL을 발견했습니다. 실제 서버와 통신해 404 에러 링크가 있는지 검사합니다...")
  url_validation_results = verify_urls_sync(extracted_urls)
  
  broken_urls_report = []
  valid_urls_report = []
  for url, res in url_validation_results.items():
    if not res["is_valid"]:
      broken_urls_report.append(f"- {url}: {res['error_message']} (지적 대상 - 최종 권고안에서 제거 또는 대체 필수)")
    else:
      valid_urls_report.append(f"- {url}: 존재함 ({res['status_code']} OK)")
      
  url_check_summary = "[물리적 URL 검증 결과 (HTTP Status Check)]\n"
  if broken_urls_report:
    url_check_summary += "!!! 발견된 오류/깨진 링크 (404 Not Found 또는 통신 불가) !!!\n" + "\n".join(broken_urls_report) + "\n"
  else:
    url_check_summary += "모든 URL의 물리적 연결이 정상인 것으로 확인되었습니다.\n"
    
  if valid_urls_report:
    url_check_summary += "\n[정상 링크 목록]\n" + "\n".join(valid_urls_report) + "\n"

  evaluation_payload = (
    f"Synthesized Report Draft:\n\n{resolved_synthesized_report}\n\n"
    f"{url_check_summary}\n\n"
    "Please review the above draft report and evaluate whether its contents are factually correct. "
    "CRITICAL REQUIREMENT: If there are any broken/404 URLs listed in the '[물리적 URL 검증 결과]' section above, "
    "you MUST explicitly list them as errors and demand their complete deletion or replacement in the Fact-check Report. "
    "Ensure no broken URLs can survive the next remediation step. "
    "Check all product names, features, integration specifications, and markdown links/URLs against official specifications. "
    "Generate a precise 'Fact-check Report' identifying any non-factual or inaccurate elements."
  )
  try:
    eval_config = ContextCacheManager.get_cached_config(
      genai_client=genai_client,
      model_name=MODEL_AGENT,
      cache_key="EVALUATOR",
      system_instruction=EVALUATOR_SYSTEM_PROMPT,
      temperature=0.1
    )
    response_eval_stream = genai_client.models.generate_content_stream(
      model=MODEL_AGENT,
      contents=evaluation_payload,
      config=eval_config
    )
    eval_chunks = []
    for chunk in response_eval_stream:
      if chunk.text:
        eval_chunks.append(chunk.text)
    evaluation_report = "".join(eval_chunks).strip()
  except Exception as e:
    logger.error(f"Factual evaluation failed: {e}")
    evaluation_report = "지적 사항 없음 (평가 모델 가동 에러에 의한 Fallback)"
  
  # 6단계: 사실성 보정 모델 구동
  print_thinking("6단계: 사실성 평가 보고서의 지적 사항을 바탕으로 한 오류 보정 및 최종 보고서 재합성을 가동한다.")
  remediation_payload = (
    f"Original Draft:\n\n{synthesized_report}\n\n"
    f"Fact-check Report:\n\n{evaluation_report}\n\n"
    "Based on the Fact-check Report, correct all non-factual or inaccurate elements in the draft, "
    "and output the fully remediated and perfected final 'Architecture Advisory Report'."
  )
  try:
    remed_config = ContextCacheManager.get_cached_config(
      genai_client=genai_client,
      model_name=MODEL_AGENT,
      cache_key="REMEDIATOR",
      system_instruction=REMEDIATOR_SYSTEM_PROMPT,
      temperature=0.2
    )
    response_remed_stream = genai_client.models.generate_content_stream(
      model=MODEL_AGENT,
      contents=remediation_payload,
      config=remed_config
    )
    remed_chunks = []
    for chunk in response_remed_stream:
      if chunk.text:
        remed_chunks.append(chunk.text)
    final_report = "".join(remed_chunks).strip()
  except Exception as e:
    logger.error(f"Factual remediation failed: {e}")
    final_report = synthesized_report
  
  # 7단계: 최종 보고서 줄바꿈 정렬 및 포맷 가공
  print_thinking("7단계: 최종 기술 자문 보고서 마크다운 문서를 미학적으로 가공하고 줄바꿈 정렬을 최종 수행한다.")
  final_report_resolved = resolve_all_urls_in_text(final_report)
  final_md = generate_and_upload_diagram(final_report_resolved)
          
  lines = final_md.split("\n")
  processed_lines = []
  for line in lines:
    match = re.match(r"^(\s+)([-*+]\s|\d+\.\s)(.*)", line)
    if match:
      indent, bullet, content = match.groups()
      new_indent_len = max(2, (len(indent) // 2) * 2) if len(indent) >= 4 else len(indent)
      processed_lines.append(" " * new_indent_len + bullet + content)
    else:
      processed_lines.append(line)
          
  return "\n".join(processed_lines)

# ==========================================
# 5. 비동기(Asynchronous) 실행 흐름 제어 (FastAPI Web/SSE 스트리밍 전용)
# ==========================================

async def route_inquiry_async(client, query: str) -> list:
  """비동기 방식으로 사용자 질문에 연관된 8대 솔루션 필라를 선별한다."""
  logger.info(f"Classifying routing pillars (Async) for query: {query}")
  
  pillars_meta = "\n".join([f"- {name}: {info['description']}" for name, info in PILLARS.items()])
  
  router_prompt = (
    "당신은 구글 클라우드 다중 에이전트 라우팅 제어 모듈이다. 아래 8대 솔루션 필라 메타데이터를 정독하십시오.\n\n"
    f"{pillars_meta}\n\n"
    f"사용자의 기술 문의인 \"{query}\"를 가장 잘 해결할 수 있는 핵심 전문 필라명(들)을 1개에서 최대 3개까지 영문 이름만 식별하십시오.\n"
    "반드시 콤마(,)로만 구분하여 반환하십시오. 절대 설명이나 공백, 특수문자를 더하지 마십시오.\n"
    "예시: Infrastructure,Security,Application_Modernization"
  )
  
  try:
    response = await client.aio.models.generate_content(
      model=MODEL_AGENT,
      contents=router_prompt,
      config=types.GenerateContentConfig(temperature=0.0)
    )
    res_text = response.text.strip()
    selected = [p.strip() for p in res_text.split(",") if p.strip() in PILLARS][:3]
    if not selected:
      selected = ["Infrastructure", "Security", "Application_Modernization"]
    return selected
  except Exception as e:
    logger.error(f"Pillar routing async failed: {e}")
    return ["Infrastructure", "Security", "Application_Modernization"]

async def invoke_single_pillar_async(client, pillar_name: str, query: str) -> dict:
  """비동기 방식으로 8대 솔루션 필라 서브 에이전트를 가동하고 구글 검색을 그라운딩한다."""
  logger.info(f"Invoking sub-agent [{pillar_name}] (Async)")
  pillar_info = PILLARS[pillar_name]
  sub_agents_map = get_sub_agents()
  sub_agent = sub_agents_map.get(pillar_name)
  
  if not sub_agent:
    return {"pillar": pillar_name, "success": False, "advice": "서브 에이전트 정보 없음"}
    
  filters = pillar_info.get("search_filters", ["cloud.google.com"])
  filter_query = " OR ".join([f"site:{f}" for f in filters])
  
  try:
    g_prompt = (
      f"Please search using google search tool strictly with `{filter_query}` filter and provide a highly technical advice regarding this query:\n"
      f"\"{query}\"\n\n"
      "You MUST output your detailed technical advice in Korean formal literary style (-다) and use exactly 2 spaces for list item indentations. "
      f"Do NOT reference or return links outside of `{filter_query}`."
    )
    
    response = await client.aio.models.generate_content(
      model=MODEL_SUBAGENTS,
      contents=g_prompt,
      config=sub_agent.config
    )
    advice = response.text.strip()
    return {
      "pillar": pillar_name,
      "success": True,
      "advice": advice
    }
  except Exception as e:
    logger.error(f"Sub-agent [{pillar_name}] async failed: {e}")
    return {
      "pillar": pillar_name,
      "success": False,
      "advice": f"**{pillar_name} 전문 자문 생성 불가**: {str(e)}"
    }

async def run_orchestrator_sse_async(query: str):
  """사용자 질문를 입력받아 각 에이전트 단계 및 생성 토큰을 SSE 규격 딕셔너리로 순차 생성(yield)하는 비동기 제너레이터다."""
  client = get_genai_client()
  
  # --- 1단계: 라우팅 개시 ---
  yield {"event": "status", "message": "1단계: 사용자 질문와 부합하는 구글 클라우드 솔루션 필라를 라우팅하고 있습니다..."}
  yield {"event": "phase_change", "phase": "routing", "status": "active"}
  
  active_pillars = (await route_inquiry_async(client, query))[:3]
  
  yield {"event": "routing_done", "pillars": active_pillars}
  yield {"event": "status", "message": f"라우팅 완료. 선택된 전문 필라 에이전트: {', '.join(active_pillars)}"}
  yield {"event": "phase_change", "phase": "routing", "status": "completed"}
  
  # --- 2단계: 전문 필라 병렬 가동 ---
  yield {"event": "status", "message": "2단계: 전문 필라 서브 에이전트를 병렬 가동하여 구글 검색 기반 정밀 자문을 도출하고 있습니다..."}
  yield {"event": "phase_change", "phase": "subagents", "status": "active"}
  
  tasks = [invoke_single_pillar_async(client, pillar, query) for pillar in active_pillars]
  compiled_advices = []
  
  for future in asyncio.as_completed(tasks):
    res = await future
    pillar_name = res["pillar"]
    success = res["success"]
    advice = res["advice"]
    
    yield {"event": "subagent_done", "pillar": pillar_name, "success": success, "content": advice}
    yield {"event": "status", "message": f"서브 에이전트 [{pillar_name}] 자문 도출 완료."}
    
    adv_text = (
      f"=== [{pillar_name}] 전문 부서 기술 권고 ===\n"
      f"{advice}\n\n"
    )
    compiled_advices.append(adv_text)
    
  yield {"event": "phase_change", "phase": "subagents", "status": "completed"}
  
  # --- 3단계: 종합 초안 합성 개시 (Stream) ---
  yield {"event": "status", "message": "3단계: 수집된 개별 분야의 전문 의견을 바탕으로 마스터 초안 보고서와 다이어그램 코드를 합성합니다..."}
  yield {"event": "phase_change", "phase": "synthesis", "status": "active"}
  
  synthesis_payload = (
    f"User Inquiry: \"{query}\"\n\n"
    "Here are the detailed technical advices generated by our specialist sub-agents with official search grounding:\n\n"
    f"{''.join(compiled_advices)}\n"
    "Please synthesize them into a single coherent 'Architecture Advisory Report' following the SYNTHESIZER_SYSTEM_PROMPT."
  )
  
  synthesized_report = ""
  try:
    synth_config = await ContextCacheManagerAsync.get_cached_config(
      genai_client=client,
      model_name=MODEL_AGENT,
      cache_key="SYNTHESIZER",
      system_instruction=SYNTHESIZER_SYSTEM_PROMPT,
      temperature=0.2
    )
    
    response_stream = await client.aio.models.generate_content_stream(
      model=MODEL_AGENT,
      contents=synthesis_payload,
      config=synth_config
    )
    
    async for chunk in response_stream:
      if chunk.text:
        synthesized_report += chunk.text
        yield {"event": "synthesis_chunk", "text": chunk.text}
        
  except Exception as e:
    logger.error(f"SSE Synthesis model stream failed: {e}")
    synthesized_report = f"### 에러 발생\n종합 권고문 생성 중 에러가 발생했습니다: {str(e)}"
    yield {"event": "synthesis_chunk", "text": synthesized_report}
    
  yield {"event": "phase_change", "phase": "synthesis", "status": "completed"}
  
  # --- 4단계: 사실 무결성 검증 (Factual Evaluator) (Stream) ---
  yield {"event": "status", "message": "4단계: 구글 클라우드 최신 공식 사양과 대조하여 사실 무결성 검증(Fact-checking) 및 실시간 URL 존재 여부 확인(404 에러 검사)을 시작합니다..."}
  yield {"event": "phase_change", "phase": "evaluation", "status": "active"}
  
  # 먼저 초안 내의 모든 리다이렉트 URL을 깔끔히 변환합니다.
  resolved_synthesized_report = resolve_all_urls_in_text(synthesized_report)
  extracted_urls = extract_urls_from_text(resolved_synthesized_report)
  
  # 비동기 병렬로 매우 신속하게 URL의 HTTP 상태를 확인합니다.
  url_validation_results = await verify_urls_async(extracted_urls)
  
  broken_urls_report = []
  valid_urls_report = []
  for url, res in url_validation_results.items():
    if not res["is_valid"]:
      broken_urls_report.append(f"- {url}: {res['error_message']} (지적 대상 - 최종 권고안에서 제거 또는 대체 필수)")
    else:
      valid_urls_report.append(f"- {url}: 존재함 ({res['status_code']} OK)")
      
  url_check_summary = "[물리적 URL 검증 결과 (HTTP Status Check)]\n"
  if broken_urls_report:
    url_check_summary += "!!! 발견된 오류/깨진 링크 (404 Not Found 또는 통신 불가) !!!\n" + "\n".join(broken_urls_report) + "\n"
  else:
    url_check_summary += "모든 URL의 물리적 연결이 정상인 것으로 확인되었습니다.\n"
    
  if valid_urls_report:
    url_check_summary += "\n[정상 링크 목록]\n" + "\n".join(valid_urls_report) + "\n"

  evaluation_payload = (
    f"Synthesized Report Draft:\n\n{resolved_synthesized_report}\n\n"
    f"{url_check_summary}\n\n"
    "Please review the above draft report and evaluate whether its contents are factually correct. "
    "CRITICAL REQUIREMENT: If there are any broken/404 URLs listed in the '[물리적 URL 검증 결과]' section above, "
    "you MUST explicitly list them as errors and demand their complete deletion or replacement in the Fact-check Report. "
    "Ensure no broken URLs can survive the next remediation step. "
    "Check all product names, features, integration specifications, and markdown links/URLs against official specifications. "
    "Generate a precise 'Fact-check Report' identifying any non-factual or inaccurate elements."
  )
  
  evaluation_report = ""
  try:
    eval_config = await ContextCacheManagerAsync.get_cached_config(
      genai_client=client,
      model_name=MODEL_AGENT,
      cache_key="EVALUATOR",
      system_instruction=EVALUATOR_SYSTEM_PROMPT,
      temperature=0.1
    )
    
    response_eval_stream = await client.aio.models.generate_content_stream(
      model=MODEL_AGENT,
      contents=evaluation_payload,
      config=eval_config
    )
    
    async for chunk in response_eval_stream:
      if chunk.text:
        evaluation_report += chunk.text
        yield {"event": "evaluation_chunk", "text": chunk.text}
        
  except Exception as e:
    logger.error(f"SSE Factual evaluation failed: {e}")
    evaluation_report = "지적 사항 없음 (평가 모델 가동 에러에 의한 Fallback)"
    yield {"event": "evaluation_chunk", "text": evaluation_report}
    
  yield {"event": "phase_change", "phase": "evaluation", "status": "completed"}
  
  # --- 5단계: 정정 및 보정 재합성 (Factual Remediator) (Stream) ---
  yield {"event": "status", "message": "5단계: 지적 사항에 의거해 기술적인 결함이나 비사실적 설정을 최종 교정하고 보정된 완성본을 조율 중입니다..."}
  yield {"event": "phase_change", "phase": "remediation", "status": "active"}
  
  remediation_payload = (
    f"Original Draft:\n\n{synthesized_report}\n\n"
    f"Fact-check Report:\n\n{evaluation_report}\n\n"
    "Based on the Fact-check Report, correct all non-factual or inaccurate elements in the draft, "
    "and output the fully remediated and perfected final 'Architecture Advisory Report'."
  )
  
  final_report = ""
  try:
    remed_config = await ContextCacheManagerAsync.get_cached_config(
      genai_client=client,
      model_name=MODEL_AGENT,
      cache_key="REMEDIATOR",
      system_instruction=REMEDIATOR_SYSTEM_PROMPT,
      temperature=0.2
    )
    
    response_remed_stream = await client.aio.models.generate_content_stream(
      model=MODEL_AGENT,
      contents=remediation_payload,
      config=remed_config
    )
    
    async for chunk in response_remed_stream:
      if chunk.text:
        final_report += chunk.text
        yield {"event": "remediation_chunk", "text": chunk.text}
        
  except Exception as e:
    logger.error(f"SSE Factual remediation failed: {e}")
    final_report = synthesized_report
    yield {"event": "remediation_chunk", "text": final_report}
    
  yield {"event": "phase_change", "phase": "remediation", "status": "completed"}
  
  # --- 6단계: 보고서 최종 정밀 포맷 가공 ---
  yield {"event": "status", "message": "6단계: 완비된 최종 기술 자문 보고서 마크다운 문서를 미학적으로 가공하고 줄바꿈 정렬을 최종 수행합니다..."}
  
  final_report_resolved = resolve_all_urls_in_text(final_report)
  final_md = generate_and_upload_diagram(final_report_resolved)
  lines = final_md.split("\n")
  processed_lines = []
  for line in lines:
    match = re.match(r"^(\s+)([-*+]\s|\d+\.\s)(.*)", line)
    if match:
      indent, bullet, content = match.groups()
      new_indent_len = max(2, (len(indent) // 2) * 2) if len(indent) >= 4 else len(indent)
      processed_lines.append(" " * new_indent_len + bullet + content)
    else:
      processed_lines.append(line)
  final_md_cleaned = "\n".join(processed_lines)
  
  yield {
    "event": "final_report", 
    "report": final_md_cleaned, 
    "image_url": "",
    "dot_code": ""
  }
  yield {"event": "status", "message": "모든 에이전트 자문 워크플로가 무결히 완료되었습니다."}
  yield {"event": "done"}

if __name__ == "__main__":
  logger.info("Local engine module loaded.")

