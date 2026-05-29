#!/bin/bash
# ==========================================
# Google Cloud QnA Integrated Single Deployment Script
# 지능형 단일 통합 배포 파이프라인: 로컬 에이전트 내장형 Cloud Run 단일 서비스 배포
# ==========================================

set -e

# 실행 경로와 무관하게 최상위 리포지토리 루트로 이동하여 빌드 컨텍스트 무결성 확보
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "${SCRIPT_DIR}")"
cd "${ROOT_DIR}"

# 로컬 .env 또는 ~/.env 파일이 존재하면 로드
if [ -f .env ]; then
  echo ".env 파일을 로드합니다."
  export $(grep -v '^#' .env | xargs)
elif [ -f ~/.env ]; then
  echo "~/.env 파일을 로드합니다."
  export $(grep -v '^#' ~/.env | xargs)
fi

# 필수 환경 변수 검증 (개인 기본값 노출 방지)
if [ -z "${GCP_PROJECT}" ]; then
  echo "오류: GCP_PROJECT 환경 변수가 설정되어 있지 않습니다." >&2
  echo ".env 파일이나 환경 변수를 확인해 주세요." >&2
  exit 1
fi

if [ -z "${GCP_REGION}" ]; then
  echo "오류: GCP_REGION 환경 변수가 설정되어 있지 않습니다." >&2
  echo ".env 파일이나 환경 변수를 확인해 주세요." >&2
  exit 1
fi

if [ -z "${GCS_BUCKET}" ]; then
  echo "오류: GCS_BUCKET 환경 변수가 설정되어 있지 않습니다." >&2
  echo ".env 파일이나 환경 변수를 확인해 주세요." >&2
  exit 1
fi

PROJECT_ID="${GCP_PROJECT}"
REGION="${GCP_REGION}"
MODEL_LOCATION_VAL="${MODEL_LOCATION:-global}"
REPO_NAME="gcp-advisor"

WEB_SERVICE_NAME="google-cloud-qna"
WEB_IMAGE_TAG="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${WEB_SERVICE_NAME}:latest"

# 1. Artifact Registry Docker 리포지토리 자동 생성 보장 (있으면 즉시 패스)
gcloud artifacts repositories describe "${REPO_NAME}" --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud artifacts repositories create "${REPO_NAME}" \
  --repository-format=docker \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --quiet

# 2. [Frontend Web + Backend Engine 통합 빌드]
echo "==========================================="
# 최상위 루트 디렉터리에 위치한 Dockerfile을 기반으로 전체 컨텍스트 빌드 수행
gcloud builds submit --tag "${WEB_IMAGE_TAG}" --project="${PROJECT_ID}" .
echo "==========================================="

# 3. [Cloud Run 통합 서비스 단일 배포]
echo "==========================================="
# 로컬에서 에이전트를 자율 기동하므로 외부 REASONING_ENGINE_ID 환경변수 주입은 제거
gcloud run deploy "${WEB_SERVICE_NAME}" \
  --image "${WEB_IMAGE_TAG}" \
  --platform managed \
  --region "${REGION}" \
  --allow-unauthenticated \
  --project="${PROJECT_ID}" \
  --update-env-vars GCP_PROJECT="${PROJECT_ID}",GCP_REGION="${REGION}",GCS_BUCKET="${PROJECT_ID}",MODEL_AGENT="${MODEL_AGENT:-gemini-3.5-flash}",MODEL_LOCATION="${MODEL_LOCATION_VAL}",MODEL_SUBAGENTS="${MODEL_SUBAGENTS:-gemini-3.5-flash}"
echo "==========================================="

# 4. [Garbage Cleanup] 구형 가비지 리비전 청소
echo "==========================================="
echo "마이크로서비스 리비전 청소 및 리소스 정리..."
echo "==========================================="

cleanup_garbage_revisions() {
  local svc_name=$1
  echo "서비스 [${svc_name}] 의 옛 가비지 리비전 청소 중..."
  
  local active_rev
  active_rev=$(gcloud run services describe "${svc_name}" \
    --platform managed \
    --region "${REGION}" \
    --project="${PROJECT_ID}" \
    --format="value(status.latestReadyRevisionName)")
  
  echo "현재 활성화된 리비전: ${active_rev}"
  
  local revisions
  revisions=$(gcloud run revisions list \
    --service "${svc_name}" \
    --platform managed \
    --region "${REGION}" \
    --project="${PROJECT_ID}" \
    --format="value(metadata.name)")
    
  for rev in $revisions; do
    if [ "${rev}" != "${active_rev}" ]; then
      echo "삭제할 리비전 발견: ${rev}"
      gcloud run revisions delete "${rev}" \
        --platform managed \
        --region "${REGION}" \
        --project="${PROJECT_ID}" \
        --quiet || true
    fi
  done
}

cleanup_garbage_revisions "${WEB_SERVICE_NAME}"

echo "==========================================="
echo "단일 에이전트 내장형 통합 릴리즈 및 리소스 정리가 완수되었습니다!"
echo "통합 포털 URL: $(gcloud run services describe "${WEB_SERVICE_NAME}" --platform managed --region "${REGION}" --project="${PROJECT_ID}" --format="value(status.url)")"
echo "==========================================="
