"""Google Cloud QnA Monolithic Agent Module.

구글 클라우드 솔루션 기술 자문을 위한 멀티 에이전트 협업 시스템이다.
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
  """최상위 google-cloud-qna 루트 및 홈 디렉터리에 있는 .env 파일을 찾아 os.environ에 안전하게 수동 적재한다."""
  current_dir = os.path.dirname(os.path.abspath(__file__))
  parent_dir = os.path.dirname(current_dir)
  
  env_paths = [
    os.path.expanduser("~/.env"),
    os.path.join(parent_dir, ".env"),
    os.path.join(current_dir, ".env")
  ]
  
  for env_path in env_paths:
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

# Ensure required Google Cloud configuration is present without leaking private default fallbacks
if not os.environ.get("GCP_PROJECT"):
  raise RuntimeError("Environment variable 'GCP_PROJECT' is required but not set. Please define it in your environment or .env file.")
if not os.environ.get("GCP_REGION"):
  raise RuntimeError("Environment variable 'GCP_REGION' is required but not set. Please define it in your environment or .env file.")
if not os.environ.get("GCS_BUCKET"):
  raise RuntimeError("Environment variable 'GCS_BUCKET' is required but not set. Please define it in your environment or .env file.")
if "MODEL_LOCATION" not in os.environ:
  os.environ["MODEL_LOCATION"] = "global"

import re
import logging
import subprocess
import asyncio
import json
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from google import genai
from google.genai import types

# 유틸리티 함수 임포트
from engine.utils import (
  resolve_redirect_url,
  resolve_all_urls_in_text,
  extract_urls_from_text,
  verify_urls_sync,
  verify_urls_async,
  extract_dot_code,
  generate_and_upload_diagram
)

# 상위 디렉터리를 파이썬 검색 경로에 추가한다.
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 로깅 설정을 구성한다.
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# 지연 초기화(Lazy Initialization) 싱글턴을 위한 비어 있는 참조 변수들
_client_instance = None
_sub_agents_instance = None

# 통합 에이전트 및 하위 전문가 모델 명칭을 할당한다. (알파벳 순 정렬)
MODEL_AGENT = os.environ.get("MODEL_AGENT") or "gemini-3.5-flash"
MODEL_SUBAGENTS = os.environ.get("MODEL_SUBAGENTS") or "gemini-3.5-flash"

# ==========================================
# 1. 프롬프트 로딩 헬퍼 및 8대 솔루션 필라(Solution Pillars) 설정
# ==========================================

def load_prompt_file(filepath: str) -> str:
  """지정한 경로의 마크다운 파일 프롬프트를 열어 텍스트로 안전하게 반환한다."""
  current_dir = os.path.dirname(os.path.abspath(__file__))
  if not os.path.isabs(filepath):
    abs_path = os.path.join(current_dir, filepath)
  else:
    abs_path = filepath
    
  if os.path.exists(abs_path):
    with open(abs_path, "r", encoding="utf-8") as f:
      return f.read().strip()
  
  logger.error(f"Prompt file not found at: {abs_path}")
  return ""


PILLARS = {
  "APIs_Applications": {
    "description": "Apigee, APIs, Application Integration 및 구글 클라우드 애플리케이션 개발/통합 설계 전문가다.",
    "search_filters": ["cloud.google.com/apigee", "cloud.google.com/application-integration", "cloud.google.com/api-gateway"],
    "instruction": load_prompt_file("prompts/pillars/apis_applications.md")
  },
  "Application_Modernization": {
    "description": "GKE (Google Kubernetes Engine), Containers, Artifact Registry, Serverless (Cloud Run, Cloud Functions) 및 마이크로서비스 현대화 설계 전문가다.",
    "search_filters": ["cloud.google.com/kubernetes-engine", "cloud.google.com/run", "cloud.google.com/artifact-registry"],
    "instruction": load_prompt_file("prompts/pillars/application_modernization.md")
  },
  "Artificial_Intelligence": {
    "description": "Vertex AI 플랫폼, Generative AI (Gemini, Vertex AI Agent Builder), Vector Search 및 AI 파이프라인 설계 전문가다.",
    "search_filters": ["cloud.google.com/vertex-ai", "cloud.google.com/gemini"],
    "instruction": load_prompt_file("prompts/pillars/artificial_intelligence.md")
  },
  "Data_Analytics": {
    "description": "BigQuery, Pub/Sub, Dataflow, Dataproc, Dataplex 및 고성능 데이터 파이프라인 분석 솔루션 설계 전문가다.",
    "search_filters": ["cloud.google.com/bigquery", "cloud.google.com/pubsub", "cloud.google.com/dataflow", "cloud.google.com/dataproc"],
    "instruction": load_prompt_file("prompts/pillars/data_analytics.md")
  },
  "Databases": {
    "description": "Cloud SQL, Cloud Spanner, Cloud Bigtable, Firestore, AlloyDB 및 고가용성 데이터 저장소 설계 전문가다.",
    "search_filters": ["cloud.google.com/sql", "cloud.google.com/storage", "cloud.google.com/spanner", "cloud.google.com/bigtable", "cloud.google.com/firestore"],
    "instruction": load_prompt_file("prompts/pillars/databases.md")
  },
  "Infrastructure": {
    "description": "Virtual Machines (Compute Engine), VPC, Cloud Load Balancing, Cloud DNS, Cloud NAT 및 전반적인 인프라스트럭처 설계 전문가다.",
    "search_filters": ["cloud.google.com/compute", "cloud.google.com/vpc", "cloud.google.com/load-balancing", "cloud.google.com/dns"],
    "instruction": load_prompt_file("prompts/pillars/infrastructure.md")
  },
  "Productivity_Collaboration": {
    "description": "Google Workspace, Google Meet, Google Drive, AppSheet 및 구글 생산성/협업 설계 전문가다.",
    "search_filters": ["workspace.google.com", "cloud.google.com/appsheet"],
    "instruction": load_prompt_file("prompts/pillars/productivity_collaboration.md")
  },
  "Security": {
    "description": "Cloud IAM 최소 권한 원칙, Cloud Identity, VPC Service Controls, Cloud Identity, Secret Manager, Cloud KMS 및 구글 클라우드 전반적인 보안 솔루션 설계 전문가다.",
    "search_filters": ["cloud.google.com/security", "cloud.google.com/iam", "cloud.google.com/vpc-service-controls"],
    "instruction": load_prompt_file("prompts/pillars/security.md")
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

EVALUATOR_SYSTEM_PROMPT = load_prompt_file("prompts/evaluator_system_prompt.md")
GCP_ARCH_STANDARDS = load_prompt_file("prompts/gcp_arch_standards.md")
REMEDIATOR_SYSTEM_PROMPT = load_prompt_file("prompts/remediator_system_prompt.md")
SYNTHESIZER_SYSTEM_PROMPT = load_prompt_file("prompts/synthesizer_system_prompt.md")


# Note: 공통 헬퍼 함수들은 engine/utils.py 로 완전히 이동 및 분리되었습니다.

def get_genai_client():
  """Google Gen AI 클라이언트를 지연 초기화(Lazy Initialization)하여 싱글턴 인스턴스로 반환한다."""
  global _client_instance
  if _client_instance is None:
    proj_id = os.environ.get("GCP_PROJECT")
    if not proj_id:
      raise RuntimeError("Environment variable 'GCP_PROJECT' is required but not set.")
    loc = os.environ.get("MODEL_LOCATION") or "global"
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
  print_thinking("4단계: 종합 원안을 기반으로 권고 보고서 초안 합성을 시작한다.")
  synthesis_payload = (
    f"User Inquiry: \"{query}\"\n\n"
    "Here are the detailed technical advices generated by our specialist sub-agents with official search grounding:\n\n"
    f"{''.join(compiled_advices)}\n"
    "Please synthesize them into a single coherent 'Technical Advisory Report' following the SYNTHESIZER_SYSTEM_PROMPT."
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

