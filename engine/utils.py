"""Google Cloud QnA Utilities Module.

이 모듈은 URL 리다이렉션 해결, 404 깨진 링크 HTTP 검증, Graphviz DOT 컴파일 및 GCS 업로드 등
핵심 비즈니스 흐름 외곽의 유틸리티성 함수들을 전담 관리한다.
"""

import os
import re
import uuid
import logging
import asyncio
import tempfile
import urllib.parse
import urllib.request
import shutil
import datetime as dt_lib
from concurrent.futures import ThreadPoolExecutor
import httpx
import graphviz
from google.cloud import storage

# 로깅 설정
logger = logging.getLogger(__name__)

# 기동 시 dot 바이너리 설치 점검 (Suggestion 2)
_has_dot_binary = shutil.which("dot") is not None
if not _has_dot_binary:
  logger.warning("[Warning] 'dot' executable not found in PATH. Graphviz compilation will degrade to error box representation.")


def resolve_redirect_url(url: str) -> str:
  """Resolves redirect URLs, particularly Google/Vertex search redirect URLs and HTTP redirects."""
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
    
    # Dot binary precheck (Suggestion 2)
    if not _has_dot_binary:
      raise RuntimeError("'dot' executable is not installed on this system. Please install graphviz package.")
      
    # 1. UUID-based unique filename (removing hyphens as requested)
    uuid_str = str(uuid.uuid4()).replace("-", "")
    object_name = f"google-cloud-qna/architecture_{uuid_str}.png"
    bucket_name = os.environ.get("GCS_BUCKET")
    if not bucket_name:
      raise RuntimeError("Environment variable 'GCS_BUCKET' is required but not set.")
    
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
      
      # 4. Generate GCS URL (Try signing first for enterprise security, fallback to public URL if local credentials don't support signing - Suggestion 4)
      try:
        # Attempt to sign the URL for 30 minutes
        public_url = blob.generate_signed_url(
          version="v4",
          expiration=dt_lib.timedelta(minutes=30),
          method="GET"
        )
        logger.info(f"Generated GCS Signed URL (30m expiry): {public_url}")
      except Exception as ex:
        # Fallback to public URL if credentials do not support local signing (e.g. ADC without private key)
        public_url = f"https://storage.googleapis.com/{bucket_name}/{object_name}"
        logger.info(f"Signed URL generation failed, falling back to public URL (Error: {ex}): {public_url}")
      
      # 5. Replace with img tag
      img_tag = f'\n<img class="architecture-diagram" src="{public_url}" alt="Technical Advisory Diagram" style="max-width: 100%; border-radius: 8px; margin-top: 15px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);" />\n'
      
      if text_after:
        return text_before + "\n" + img_tag + "\n" + text_after
      else:
        return text_before + "\n" + img_tag
      
  except Exception as e:
    logger.error(f"Failed to generate and upload Graphviz diagram: {e}")
    # 가령 오류가 발생하더라도 사용자 화면에 보기 흉한 원본 DOT 스크립트가 노출되는 것을 완전히 방지하기 위해,
    # 원본 스크립트는 삭제하고 정돈된 에러 안내 박스를 대신 렌더링하도록 확실히 보정합니다.
    error_msg = f'\n<div class="diagram-error" style="padding: 15px; border: 1px solid #f5c6cb; border-radius: 8px; background-color: #f8d7da; color: #721c24; margin-top: 15px;">\n  <strong><i class="fa-solid fa-triangle-exclamation"></i> 시각화 다이어그램 생성 오류</strong><br/>\n  <span style="font-size: 12px; color: #666;">배포 또는 시스템 환경 설정 문제로 이미지를 컴파일하지 못했습니다. (원인: {str(e)})</span>\n</div>\n'
    if text_after:
      return text_before + "\n" + error_msg + "\n" + text_after
    else:
      return text_before + "\n" + error_msg
