# Scout Production Deployment Spec

Production deployment of Scout on a dedicated AWS account: CloudFormation for AWS infrastructure (including EC2 setup via UserData), Kamal for container orchestration, and AWS Secrets Manager for credential storage.

## Architecture Overview

Four application containers deployed via Kamal onto a single EC2 instance, with RDS PostgreSQL and ElastiCache Redis as managed services. Kamal's built-in proxy handles TLS termination and routing.

| Container | Port | Public? | Description |
|-----------|------|---------|-------------|
| **Django API** | 8000 | No | uvicorn ASGI server |
| **MCP Server** | 8100 | No | FastMCP data access layer |
| **Celery Worker** | -- | No | Worker + Beat for background tasks and periodic scheduling |
| **Frontend** | 3000 | Yes | nginx serving React SPA, proxies `/api/` and `/admin/` to Django |

Only the frontend container is publicly accessible. The API, MCP, and worker are reachable only via the `scout_shared` Docker network.

### Managed Services (AWS)

- **RDS PostgreSQL 16** -- platform database (password auto-managed by RDS)
- **ElastiCache Redis 7** -- caching, rate limiting, Celery broker
- **ECR** -- Docker image registry
- **Secrets Manager** -- all credentials

### Tooling

- **CloudFormation** -- AWS infrastructure (VPC, RDS, ElastiCache, ECR, SGs, IAM, EC2 + UserData)
- **Kamal 2** -- container deploy + proxy + TLS
- **GitHub Actions** -- CI/CD pipeline (builds images, deploys via Kamal)
- **SSM Session Manager** -- operator shell access (no SSH needed for humans)

### Monitoring

- **BetterStack** -- uptime monitoring on `/health/` endpoint
- **Sentry** -- error tracking (add `SENTRY_DSN` to env)

> **Reference:** This spec follows the `data-buddy` deployment pattern (Kamal + AWS Secrets Manager) adapted for Scout's Django/React/MCP stack.

---

## 1. AWS Account Setup

Dedicated AWS account under the organization. Isolates billing, IAM, and blast radius from other projects.

| Setting | Value |
|---------|-------|
| Account Name | `scout-production` |
| Region | `us-east-1` |
| SSO Profile | `scout` (for AWS CLI / Kamal) |

---

## 2. CloudFormation Infrastructure

A single CloudFormation stack provisions all AWS resources, including EC2 instance configuration via UserData (no Ansible needed).

```bash
# Deploy the stack
aws cloudformation deploy \
  --template-file infra/scout-stack.yml \
  --stack-name scout-production \
  --parameter-overrides \
    EC2KeyPairName=scout-deploy \
  --capabilities CAPABILITY_NAMED_IAM \
  --profile scout

# View outputs (EC2 IP, RDS endpoint, Redis endpoint, etc.)
aws cloudformation describe-stacks \
  --stack-name scout-production \
  --query 'Stacks[0].Outputs' \
  --profile scout
```

### `infra/scout-stack.yml`

```yaml
AWSTemplateFormatVersion: '2010-09-09'
Description: Scout production infrastructure

Parameters:
  EC2KeyPairName:
    Type: AWS::EC2::KeyPair::KeyName
    Description: SSH key pair for EC2 instance (used by Kamal for deploys)

  EC2InstanceType:
    Type: String
    Default: t3.medium
    AllowedValues: [t3.micro, t3.small, t3.medium, t3.large]

  DBInstanceClass:
    Type: String
    Default: db.t4g.micro
    AllowedValues: [db.t4g.micro, db.t4g.small, db.t4g.medium]

  CacheNodeType:
    Type: String
    Default: cache.t4g.micro
    AllowedValues: [cache.t4g.micro, cache.t4g.small, cache.t4g.medium]

Resources:

  # ── VPC & Networking ──────────────────────────────────────────────

  VPC:
    Type: AWS::EC2::VPC
    Properties:
      CidrBlock: 10.0.0.0/16
      EnableDnsSupport: true
      EnableDnsHostnames: true
      Tags:
        - Key: Name
          Value: scout-vpc

  InternetGateway:
    Type: AWS::EC2::InternetGateway

  GatewayAttachment:
    Type: AWS::EC2::VPCGatewayAttachment
    Properties:
      VpcId: !Ref VPC
      InternetGatewayId: !Ref InternetGateway

  PublicSubnetA:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref VPC
      CidrBlock: 10.0.1.0/24
      AvailabilityZone: !Select [0, !GetAZs '']
      MapPublicIpOnLaunch: true
      Tags:
        - Key: Name
          Value: scout-public-a

  PublicSubnetB:
    Type: AWS::EC2::Subnet
    Properties:
      VpcId: !Ref VPC
      CidrBlock: 10.0.2.0/24
      AvailabilityZone: !Select [1, !GetAZs '']
      MapPublicIpOnLaunch: true
      Tags:
        - Key: Name
          Value: scout-public-b

  PublicRouteTable:
    Type: AWS::EC2::RouteTable
    Properties:
      VpcId: !Ref VPC

  PublicRoute:
    Type: AWS::EC2::Route
    DependsOn: GatewayAttachment
    Properties:
      RouteTableId: !Ref PublicRouteTable
      DestinationCidrBlock: 0.0.0.0/0
      GatewayId: !Ref InternetGateway

  SubnetARouteTableAssoc:
    Type: AWS::EC2::SubnetRouteTableAssociation
    Properties:
      SubnetId: !Ref PublicSubnetA
      RouteTableId: !Ref PublicRouteTable

  SubnetBRouteTableAssoc:
    Type: AWS::EC2::SubnetRouteTableAssociation
    Properties:
      SubnetId: !Ref PublicSubnetB
      RouteTableId: !Ref PublicRouteTable

  # ── Security Groups ──────────────────────────────────────────────

  EC2SecurityGroup:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: Scout EC2 instance
      VpcId: !Ref VPC
      SecurityGroupIngress:
        - IpProtocol: tcp
          FromPort: 22
          ToPort: 22
          CidrIp: 0.0.0.0/0
          Description: SSH (key-only auth, required by Kamal)
        - IpProtocol: tcp
          FromPort: 80
          ToPort: 80
          CidrIp: 0.0.0.0/0
          Description: HTTP (Kamal proxy redirect)
        - IpProtocol: tcp
          FromPort: 443
          ToPort: 443
          CidrIp: 0.0.0.0/0
          Description: HTTPS (Kamal proxy)
      Tags:
        - Key: Name
          Value: scout-ec2-sg

  RDSSecurityGroup:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: Scout RDS access
      VpcId: !Ref VPC
      SecurityGroupIngress:
        - IpProtocol: tcp
          FromPort: 5432
          ToPort: 5432
          SourceSecurityGroupId: !Ref EC2SecurityGroup
          Description: PostgreSQL from EC2 only
      Tags:
        - Key: Name
          Value: scout-rds-sg

  RedisSecurityGroup:
    Type: AWS::EC2::SecurityGroup
    Properties:
      GroupDescription: Scout ElastiCache access
      VpcId: !Ref VPC
      SecurityGroupIngress:
        - IpProtocol: tcp
          FromPort: 6379
          ToPort: 6379
          SourceSecurityGroupId: !Ref EC2SecurityGroup
          Description: Redis from EC2 only
      Tags:
        - Key: Name
          Value: scout-redis-sg

  # ── EC2 Instance ─────────────────────────────────────────────────

  EC2InstanceProfile:
    Type: AWS::IAM::InstanceProfile
    Properties:
      Roles:
        - !Ref EC2Role

  EC2Role:
    Type: AWS::IAM::Role
    Properties:
      RoleName: scout-ec2-role
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal:
              Service: ec2.amazonaws.com
            Action: sts:AssumeRole
      ManagedPolicyArns:
        - arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

  EC2Instance:
    Type: AWS::EC2::Instance
    Properties:
      InstanceType: !Ref EC2InstanceType
      ImageId: !Sub '{{resolve:ssm:/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id}}'
      KeyName: !Ref EC2KeyPairName
      SubnetId: !Ref PublicSubnetA
      SecurityGroupIds:
        - !Ref EC2SecurityGroup
      IamInstanceProfile: !Ref EC2InstanceProfile
      BlockDeviceMappings:
        - DeviceName: /dev/sda1
          Ebs:
            VolumeSize: 50
            VolumeType: gp3
      UserData:
        Fn::Base64: |
          #!/bin/bash
          set -euo pipefail

          # System packages
          apt-get update
          apt-get install -y apt-transport-https ca-certificates curl \
            software-properties-common gnupg unzip \
            postgresql-client-common postgresql-client-16

          # Docker
          install -m 0755 -d /etc/apt/keyrings
          curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
            -o /etc/apt/keyrings/docker.asc
          echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.asc] \
            https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
            > /etc/apt/sources.list.d/docker.list
          apt-get update
          apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin

          # Deploy user
          useradd -m -s /bin/bash scout
          usermod -aG docker scout

          # uv (Python package manager)
          curl -LsSf https://astral.sh/uv/install.sh | sudo -u scout bash
      Tags:
        - Key: Name
          Value: scout-web

  ElasticIP:
    Type: AWS::EC2::EIP
    Properties:
      InstanceId: !Ref EC2Instance

  # ── RDS PostgreSQL ───────────────────────────────────────────────

  DBSubnetGroup:
    Type: AWS::RDS::DBSubnetGroup
    Properties:
      DBSubnetGroupDescription: Scout RDS subnets
      SubnetIds:
        - !Ref PublicSubnetA
        - !Ref PublicSubnetB

  RDSInstance:
    Type: AWS::RDS::DBInstance
    DeletionPolicy: Snapshot
    Properties:
      DBInstanceIdentifier: scout-db
      Engine: postgres
      EngineVersion: '16'
      DBInstanceClass: !Ref DBInstanceClass
      AllocatedStorage: 20
      MaxAllocatedStorage: 100
      StorageType: gp3
      DBName: agent_platform
      MasterUsername: platform
      ManageMasterUserPassword: true
      DBSubnetGroupName: !Ref DBSubnetGroup
      VPCSecurityGroups:
        - !Ref RDSSecurityGroup
      BackupRetentionPeriod: 7
      StorageEncrypted: true
      PubliclyAccessible: false
      Tags:
        - Key: Name
          Value: scout-db

  # ── ElastiCache Redis ────────────────────────────────────────────

  RedisSubnetGroup:
    Type: AWS::ElastiCache::SubnetGroup
    Properties:
      Description: Scout Redis subnets
      SubnetIds:
        - !Ref PublicSubnetA
        - !Ref PublicSubnetB

  RedisCluster:
    Type: AWS::ElastiCache::CacheCluster
    Properties:
      ClusterName: scout-redis
      Engine: redis
      EngineVersion: '7.1'
      CacheNodeType: !Ref CacheNodeType
      NumCacheNodes: 1
      CacheSubnetGroupName: !Ref RedisSubnetGroup
      VpcSecurityGroupIds:
        - !Ref RedisSecurityGroup
      Tags:
        - Key: Name
          Value: scout-redis

  # ── ECR Repositories ─────────────────────────────────────────────

  ECRApi:
    Type: AWS::ECR::Repository
    Properties:
      RepositoryName: scout/api
      ImageTagMutability: MUTABLE
      LifecyclePolicy:
        LifecyclePolicyText: |
          {
            "rules": [{
              "rulePriority": 1,
              "description": "Keep last 10 images",
              "selection": {
                "tagStatus": "any",
                "countType": "imageCountMoreThan",
                "countNumber": 10
              },
              "action": { "type": "expire" }
            }]
          }

  ECRMcp:
    Type: AWS::ECR::Repository
    Properties:
      RepositoryName: scout/mcp
      ImageTagMutability: MUTABLE
      LifecyclePolicy:
        LifecyclePolicyText: |
          {
            "rules": [{
              "rulePriority": 1,
              "description": "Keep last 10 images",
              "selection": {
                "tagStatus": "any",
                "countType": "imageCountMoreThan",
                "countNumber": 10
              },
              "action": { "type": "expire" }
            }]
          }

  ECRFrontend:
    Type: AWS::ECR::Repository
    Properties:
      RepositoryName: scout/frontend
      ImageTagMutability: MUTABLE
      LifecyclePolicy:
        LifecyclePolicyText: |
          {
            "rules": [{
              "rulePriority": 1,
              "description": "Keep last 10 images",
              "selection": {
                "tagStatus": "any",
                "countType": "imageCountMoreThan",
                "countNumber": 10
              },
              "action": { "type": "expire" }
            }]
          }

  # ── GitHub OIDC ──────────────────────────────────────────────────

  GitHubOIDCProvider:
    Type: AWS::IAM::OIDCProvider
    Properties:
      Url: https://token.actions.githubusercontent.com
      ClientIdList:
        - sts.amazonaws.com
      ThumbprintList:
        - 6938fd4d98bab03faadb97b34396831e3780aea1

  GitHubDeployRole:
    Type: AWS::IAM::Role
    Properties:
      RoleName: scout-github-deploy
      AssumeRolePolicyDocument:
        Version: '2012-10-17'
        Statement:
          - Effect: Allow
            Principal:
              Federated: !Ref GitHubOIDCProvider
            Action: sts:AssumeRoleWithWebIdentity
            Condition:
              StringEquals:
                token.actions.githubusercontent.com:aud: sts.amazonaws.com
              StringLike:
                token.actions.githubusercontent.com:sub: repo:dimagi-rad/scout:ref:refs/heads/main
      Policies:
        - PolicyName: scout-deploy-policy
          PolicyDocument:
            Version: '2012-10-17'
            Statement:
              - Effect: Allow
                Action:
                  - ecr:GetAuthorizationToken
                Resource: '*'
              - Effect: Allow
                Action:
                  - ecr:BatchCheckLayerAvailability
                  - ecr:GetDownloadUrlForLayer
                  - ecr:BatchGetImage
                  - ecr:PutImage
                  - ecr:InitiateLayerUpload
                  - ecr:UploadLayerPart
                  - ecr:CompleteLayerUpload
                Resource:
                  - !GetAtt ECRApi.Arn
                  - !GetAtt ECRMcp.Arn
                  - !GetAtt ECRFrontend.Arn
              - Effect: Allow
                Action:
                  - secretsmanager:GetSecretValue
                Resource:
                  - !Sub 'arn:aws:secretsmanager:${AWS::Region}:${AWS::AccountId}:secret:scout/*'

Outputs:
  EC2PublicIP:
    Description: EC2 Elastic IP (use in Kamal configs and DNS)
    Value: !Ref ElasticIP

  RDSEndpoint:
    Description: RDS endpoint (use in DATABASE_URL)
    Value: !GetAtt RDSInstance.Endpoint.Address

  RDSSecretArn:
    Description: RDS-managed secret ARN (contains master password)
    Value: !GetAtt RDSInstance.MasterUserSecret.SecretArn

  RedisEndpoint:
    Description: ElastiCache endpoint (use in REDIS_URL)
    Value: !GetAtt RedisCluster.RedisEndpoint.Address

  ECRRegistry:
    Description: ECR registry URL (use in Kamal registry.server)
    Value: !Sub '${AWS::AccountId}.dkr.ecr.${AWS::Region}.amazonaws.com'

  GitHubDeployRoleArn:
    Description: IAM role ARN for GitHub Actions OIDC (use in workflow)
    Value: !GetAtt GitHubDeployRole.Arn
```

### Stack Outputs

After deploying the stack, the outputs provide the values needed by Kamal configs and `.kamal/secrets`:

| Output | Used In |
|--------|---------|
| `EC2PublicIP` | Kamal `servers.web.hosts`, DNS A record |
| `RDSEndpoint` | `.kamal/secrets` → `DATABASE_URL` |
| `RDSSecretArn` | `.kamal/secrets` → fetch DB password from RDS-managed secret |
| `RedisEndpoint` | Kamal `env.clear.REDIS_URL` |
| `ECRRegistry` | Kamal `registry.server` |
| `GitHubDeployRoleArn` | GitHub Actions `role-to-assume` |

### Updating Infrastructure

```bash
# Preview changes
aws cloudformation deploy \
  --template-file infra/scout-stack.yml \
  --stack-name scout-production \
  --no-execute-changeset \
  --profile scout

# Scale up RDS
aws cloudformation deploy \
  --template-file infra/scout-stack.yml \
  --stack-name scout-production \
  --parameter-overrides DBInstanceClass=db.t4g.small \
  --capabilities CAPABILITY_NAMED_IAM \
  --profile scout
```

### Operator Access

SSH is open for Kamal deploys only (key-based auth, no password). For interactive shell access, use SSM Session Manager:

```bash
# Shell into EC2 (no SSH key needed, IAM-authenticated)
aws ssm start-session --target <instance-id> --profile scout

# Port forward to RDS for local psql access
aws ssm start-session --target <instance-id> \
  --document-name AWS-StartPortForwardingSessionToRemoteHost \
  --parameters '{"host":["<RDSEndpoint>"],"portNumber":["5432"],"localPortNumber":["5432"]}' \
  --profile scout
```

---

## 3. AWS Secrets Manager

All secrets stored in AWS Secrets Manager. Kamal fetches them at deploy time via the `aws_secrets_manager` adapter.

### Secrets Layout

| Secret Group | Keys | Used By |
|-------------|------|---------|
| *(RDS-managed)* | Auto-generated JSON with `username`, `password`, `host`, `port` | API, MCP, Worker (composed into DATABASE_URL) |
| `scout/django` | `DJANGO_SECRET_KEY`, `DB_CREDENTIAL_KEY` | API, MCP, Worker |
| `scout/api-keys` | `ANTHROPIC_API_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_PUBLIC_KEY`, `SENTRY_DSN` | API, Worker |
| `scout/oauth` | `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, `GITHUB_OAUTH_CLIENT_ID`, `GITHUB_OAUTH_CLIENT_SECRET`, `COMMCARE_OAUTH_CLIENT_ID`, `COMMCARE_OAUTH_CLIENT_SECRET` | Bootstrap only (not injected at runtime) |

> **RDS-managed secret:** RDS automatically creates and rotates the master password. The `.kamal/secrets` script fetches this secret and parses the JSON to extract the password and host for `DATABASE_URL`.

> **OAuth credentials** are stored in Secrets Manager but are **not injected into containers at runtime**. They are only used by `scripts/setup_oauth_apps.py`, which reads them from env vars and writes `SocialApp` rows into the database. At runtime, django-allauth reads OAuth credentials from the database. Run the setup script on first deploy and after credential rotation.

### `.kamal/secrets`

```bash
# RDS-managed password (JSON: {"username":"platform","password":"...","host":"...","port":5432})
RDS_SECRET=$(aws secretsmanager get-secret-value \
  --secret-id <RDSSecretArn from stack output> \
  --query SecretString --output text \
  --profile scout)

DB_PASSWORD=$(echo "$RDS_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")
DB_HOST=$(echo "$RDS_SECRET" | python3 -c "import sys,json; print(json.load(sys.stdin)['host'])")
DATABASE_URL=postgresql://platform:$DB_PASSWORD@$DB_HOST:5432/agent_platform

# Django secrets
DJANGO_SECRETS=$(kamal secrets fetch \
  --adapter aws_secrets_manager \
  --account scout \
  SCOUT_DJANGO_SECRET_KEY SCOUT_DB_CREDENTIAL_KEY)

DJANGO_SECRET_KEY=$(kamal secrets extract SCOUT_DJANGO_SECRET_KEY $DJANGO_SECRETS)
DB_CREDENTIAL_KEY=$(kamal secrets extract SCOUT_DB_CREDENTIAL_KEY $DJANGO_SECRETS)

# API keys
API_SECRETS=$(kamal secrets fetch \
  --adapter aws_secrets_manager \
  --account scout \
  SCOUT_ANTHROPIC_API_KEY SCOUT_SENTRY_DSN)

ANTHROPIC_API_KEY=$(kamal secrets extract SCOUT_ANTHROPIC_API_KEY $API_SECRETS)
SENTRY_DSN=$(kamal secrets extract SCOUT_SENTRY_DSN $API_SECRETS)
```

---

## 4. Kamal Deployment

Four separate Kamal configs. All services share the `scout_shared` Docker network. CI builds and pushes images to ECR; Kamal deploys pre-built images by tag.

### `.kamal/hooks/pre-deploy`

```bash
#!/bin/bash
# Run migrations in the newly-built API container before swapping
kamal app exec --version=$KAMAL_VERSION -- python manage.py migrate
```

### `.kamal/hooks/pre-connect`

```bash
#!/bin/bash
# Ensure shared Docker network exists before any service deploys
ssh ${SSH_USER}@${KAMAL_HOSTS} \
  "docker network create scout_shared 2>/dev/null || true"
```

### `config/deploy.yml` -- Django API (Primary)

```yaml
service: scout

image: scout/api

servers:
  web:
    hosts:
      - <EC2_IP>
    options:
      network: "scout_shared"
    cmd: >
      uvicorn config.asgi:application
        --host 0.0.0.0 --port 8000
        --workers 4 --timeout-keep-alive 120

registry:
  server: <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
  username: AWS
  password: <%= %x(./registry_password.sh) %>

env:
  clear:
    DJANGO_SETTINGS_MODULE: config.settings.production
    DJANGO_ALLOWED_HOSTS: "scout.example.com"
    CSRF_TRUSTED_ORIGINS: "https://scout.example.com"
    SECURE_SSL_REDIRECT: "False"
    MCP_SERVER_URL: "http://scout-mcp-web:8100/mcp"
    REDIS_URL: "redis://<ELASTICACHE_ENDPOINT>:6379/0"
    EMBED_ALLOWED_ORIGINS: "https://labs.connect.dimagi.com"
    MAX_CONNECTIONS_PER_PROJECT: 5
    MAX_QUERIES_PER_MINUTE: 60
  secret:
    - DATABASE_URL
    - DJANGO_SECRET_KEY
    - DB_CREDENTIAL_KEY
    - ANTHROPIC_API_KEY
    - SENTRY_DSN

ssh:
  user: scout

# No proxy — API is internal only, reached via nginx in frontend container
```

### `config/deploy-mcp.yml` -- MCP Server

```yaml
service: scout-mcp

image: scout/mcp

servers:
  web:
    hosts:
      - <EC2_IP>
    options:
      network: "scout_shared"
      network-alias: "scout-mcp-web"
    cmd: >
      python -m mcp_server
        --transport streamable-http
        --host 0.0.0.0 --port 8100

registry:
  server: <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
  username: AWS
  password: <%= %x(./registry_password.sh) %>

env:
  clear:
    DJANGO_SETTINGS_MODULE: config.settings.production
  secret:
    - DATABASE_URL
    - DB_CREDENTIAL_KEY
    - DJANGO_SECRET_KEY

ssh:
  user: scout

# No proxy — MCP is internal only, reached by API via Docker network
```

### `config/deploy-worker.yml` -- Celery Worker + Beat

```yaml
service: scout-worker

image: scout/api

servers:
  web:
    hosts:
      - <EC2_IP>
    options:
      network: "scout_shared"
    cmd: >
      celery -A config worker --beat -l info
        --scheduler django_celery_beat.schedulers:DatabaseScheduler

registry:
  server: <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
  username: AWS
  password: <%= %x(./registry_password.sh) %>

env:
  clear:
    DJANGO_SETTINGS_MODULE: config.settings.production
    REDIS_URL: "redis://<ELASTICACHE_ENDPOINT>:6379/0"
  secret:
    - DATABASE_URL
    - DJANGO_SECRET_KEY
    - DB_CREDENTIAL_KEY

ssh:
  user: scout

# No proxy — worker has no HTTP interface
```

### `config/deploy-frontend.yml` -- Frontend

```yaml
service: scout-frontend

image: scout/frontend

servers:
  web:
    hosts:
      - <EC2_IP>
    options:
      network: "scout_shared"
      network-alias: "scout-frontend-web"

registry:
  server: <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
  username: AWS
  password: <%= %x(./registry_password.sh) %>

ssh:
  user: scout

proxy:
  host: scout.example.com
  app_port: 3000
  ssl: true
  forward_headers: true
  healthcheck:
    interval: 5
    path: /
    timeout: 3
```

### `registry_password.sh`

```bash
#!/bin/bash
# ECR auth token for Kamal (same pattern as data-buddy)
AWS_PROFILE=${AWS_PROFILE:-scout}

# Check if we have valid credentials
if ! aws sts get-caller-identity --profile "$AWS_PROFILE" &>/dev/null 2>&1; then
  aws sso login --profile "$AWS_PROFILE" >&2
fi

aws ecr get-login-password \
  --region us-east-1 \
  --profile "$AWS_PROFILE"
```

### Deploy Commands

```bash
# Deploy all four services (order matters on first deploy)
kamal deploy -c config/deploy-mcp.yml           # MCP server (first — API connects to it)
kamal deploy                                    # API (runs migrations via pre-deploy hook)
kamal deploy -c config/deploy-worker.yml        # Worker (needs DB migrations applied)
kamal deploy -c config/deploy-frontend.yml      # Frontend (last — proxies to API)

# Redeploy without rebuild
kamal redeploy
kamal redeploy -c config/deploy-mcp.yml

# Logs
kamal app logs
kamal app logs -c config/deploy-mcp.yml
kamal app logs -c config/deploy-worker.yml

# Shell access (prefer SSM for interactive use)
kamal app exec -i -- bash

# Rollback
kamal rollback
kamal rollback -c config/deploy-mcp.yml
```

---

## 5. Service Configuration

### Environment Variables

| Variable | Source | Services | Value / Notes |
|----------|--------|----------|---------------|
| `DJANGO_SETTINGS_MODULE` | clear | API, MCP, Worker | `config.settings.production` |
| `DJANGO_ALLOWED_HOSTS` | clear | API | Production domain |
| `CSRF_TRUSTED_ORIGINS` | clear | API | `https://scout.example.com` |
| `SECURE_SSL_REDIRECT` | clear | API | `False` (Kamal proxy handles HTTPS redirect) |
| `EMBED_ALLOWED_ORIGINS` | clear | API | `https://labs.connect.dimagi.com` |
| `DATABASE_URL` | secret | API, MCP, Worker | RDS connection string (from RDS-managed secret) |
| `DJANGO_SECRET_KEY` | secret | API, MCP, Worker | Randomly generated |
| `DB_CREDENTIAL_KEY` | secret | API, MCP, Worker | Fernet key for project DB encryption |
| `ANTHROPIC_API_KEY` | secret | API | Claude API key |
| `SENTRY_DSN` | secret | API, Worker | Sentry error tracking |
| `REDIS_URL` | clear | API, Worker | ElastiCache endpoint |
| `MCP_SERVER_URL` | clear | API | `http://scout-mcp-web:8100/mcp` |
| `LANGFUSE_*` | secret | API | Optional tracing |
| `*_OAUTH_*` | Secrets Manager | Bootstrap only | Not in Kamal env -- used by `setup_oauth_apps.py` to write SocialApp rows to DB |

### Docker Networking

All four containers join the `scout_shared` Docker network. Internal DNS names:

| Alias | Service | Port |
|-------|---------|------|
| `scout-web` | API | 8000 |
| `scout-mcp-web` | MCP | 8100 |
| `scout-worker` | Celery | -- |
| `scout-frontend-web` | nginx | 3000 |

### Nginx Configuration

The frontend nginx is the sole public entry point. It proxies `/api/` and `/admin/` to Django via the Docker network. Key change from dev: `proxy_pass http://scout-web:8000` instead of `http://api:8000`.

```nginx
# frontend/nginx.prod-kamal.conf
server {
    listen 3000;
    server_name _;
    root /usr/share/nginx/html;
    index index.html;

    location / {
        try_files $uri $uri/ /index.html;
    }

    location /api/ {
        proxy_pass http://scout-web:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_buffering off;
        proxy_cache off;
        chunked_transfer_encoding on;
    }

    location /admin/ {
        proxy_pass http://scout-web:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    location /health/ {
        proxy_pass http://scout-web:8000;
    }

    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml;
    gzip_min_length 256;
}
```

### Dockerfile Changes

Add `collectstatic` to the API Dockerfile so it runs at build time, not on every container start:

```dockerfile
# After COPY . . and project install
RUN DJANGO_SECRET_KEY=build-placeholder python manage.py collectstatic --noinput
```

---

## 6. DNS & SSL

### DNS Records

| Record | Type | Value |
|--------|------|-------|
| `scout.example.com` | A | `<EC2PublicIP>` |

MCP server, API, and worker are internal only -- no public DNS needed.

### SSL/TLS

Kamal proxy handles TLS termination with auto-provisioned Let's Encrypt certificates. `forward_headers: true` in the proxy config sends `X-Forwarded-Proto: https` to Django, so `SECURE_PROXY_SSL_HEADER` works correctly for secure cookies. `SECURE_SSL_REDIRECT` is set to `False` because Kamal proxy handles the HTTP→HTTPS redirect at the edge.

### Iframe Embedding

Production settings should use `X_FRAME_OPTIONS = "SAMEORIGIN"` (not `"DENY"`) and set `EMBED_ALLOWED_ORIGINS` to allow embedding from `https://labs.connect.dimagi.com`.

---

## 7. CI/CD Pipeline

GitHub Actions workflow. On merge to `main`, build Docker images, push to ECR, deploy via Kamal. Uses OIDC for AWS authentication (no long-lived access keys).

```yaml
# .github/workflows/deploy.yml
name: Deploy Scout

on:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  id-token: write
  contents: read

jobs:
  deploy:
    runs-on: ubuntu-latest
    env:
      IMAGE_TAG: ${{ github.sha }}
      ECR_REGISTRY: <ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials (OIDC)
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: <GitHubDeployRoleArn from stack output>
          aws-region: us-east-1

      - name: Login to ECR
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build and push API image
        run: |
          docker build -t $ECR_REGISTRY/scout/api:$IMAGE_TAG .
          docker push $ECR_REGISTRY/scout/api:$IMAGE_TAG

      - name: Build and push Frontend image
        run: |
          docker build -t $ECR_REGISTRY/scout/frontend:$IMAGE_TAG \
            -f Dockerfile.frontend .
          docker push $ECR_REGISTRY/scout/frontend:$IMAGE_TAG

      - name: Install Kamal
        run: gem install kamal

      - name: Setup SSH
        uses: webfactory/ssh-agent@v0.9.0
        with:
          ssh-private-key: ${{ secrets.SSH_PRIVATE_KEY }}

      - name: Deploy MCP
        run: kamal deploy -c config/deploy-mcp.yml --version=$IMAGE_TAG

      - name: Deploy API
        run: kamal deploy --version=$IMAGE_TAG

      - name: Deploy Worker
        run: kamal deploy -c config/deploy-worker.yml --version=$IMAGE_TAG

      - name: Deploy Frontend
        run: kamal deploy -c config/deploy-frontend.yml --version=$IMAGE_TAG
```

> **Note:** The API and MCP images are the same Docker image (`scout/api`). The MCP container just runs a different command. Only two images need to be built: API and Frontend.

---

## 8. Launch Checklist

### AWS Setup
- [ ] Create dedicated AWS account
- [ ] Configure SSO profile `scout`
- [ ] Create EC2 key pair `scout-deploy`
- [ ] Deploy CloudFormation stack (`infra/scout-stack.yml`)
- [ ] Note stack outputs (EC2 IP, RDS endpoint, RDS secret ARN, Redis endpoint, ECR registry, OIDC role ARN)
- [ ] Store `GitHubDeployRoleArn` and SSH private key in GitHub Actions secrets

### Secrets
- [ ] Generate `DJANGO_SECRET_KEY` and store in Secrets Manager as `scout/django`
- [ ] Generate `DB_CREDENTIAL_KEY` (Fernet) and store in `scout/django`
- [ ] Store `ANTHROPIC_API_KEY` in `scout/api-keys`
- [ ] Store `SENTRY_DSN` in `scout/api-keys`
- [ ] Store OAuth credentials in `scout/oauth` (Google, GitHub, CommCare)
- [ ] Store Langfuse keys in `scout/api-keys` (optional)
- [ ] Verify RDS-managed secret exists (auto-created by CloudFormation)
- [ ] Test `.kamal/secrets` script locally

### Deploy & Verify
- [ ] Point DNS A record to `EC2PublicIP`
- [ ] Update Kamal configs with stack outputs (hosts, registry, Redis URL)
- [ ] Update `.kamal/secrets` with `RDSSecretArn`
- [ ] Update `DJANGO_ALLOWED_HOSTS` and `CSRF_TRUSTED_ORIGINS` with actual domain
- [ ] Create `frontend/nginx.prod-kamal.conf` with `proxy_pass http://scout-web:8000`
- [ ] Add `collectstatic` to Dockerfile
- [ ] Run first deploy: MCP → API → Worker → Frontend
- [ ] Verify `/health/` endpoint returns 200
- [ ] Create superuser: `kamal app exec -- python manage.py createsuperuser`
- [ ] Update Django `Site` record domain from `localhost:8000` to production domain (via admin or `kamal app exec`)
- [ ] Bootstrap OAuth: fetch creds from Secrets Manager, run `kamal app exec -- env GOOGLE_OAUTH_CLIENT_ID=... python scripts/setup_oauth_apps.py`
- [ ] Set up BetterStack uptime monitor on `https://scout.example.com/health/`
- [ ] Test login + chat flow end to end

---

## Day 2 Considerations

- Log aggregation (CloudWatch agent or Datadog)
- Database migrations strategy for zero-downtime deploys (expand/contract pattern)
- Backup verification testing (restore RDS snapshot to verify)
- Scaling: second EC2 instance behind ALB if single-instance risk becomes unacceptable
- Email: configure SES if email/password login or notifications are needed
