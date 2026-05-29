# Google Cloud Enterprise Architecture & Design Standards (v2.5)

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
