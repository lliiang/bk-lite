# BK-Lite Agent Guide

> 本文件面向 AI 编码助手，汇总仓库结构、技术栈、构建/测试命令、代码规范与安全红线。更详细的业务术语、模块设计与运行约束请按文中链接深入阅读；修改具体模块前，必须对照相关 capability 与 change spec。

## 1. 项目概述

**BK-Lite（Blueking Lite）** 是面向运维管理员的轻量、AI-first 运维平台，属于腾讯蓝鲸开源生态。项目采用多模块 monorepo 结构，包含：

- **Server**：基于 Django 的中心化 API 与业务引擎
- **Web**：基于 Next.js 16 / React 19 的主控制台（Control Console）
- **Mobile**：基于 Next.js 15 / Tauri 2 的移动端与桌面端壳
- **WebChat**：基于 npm monorepo 的嵌入式对话组件库
- **Agents**：Stargazer 采集代理、Ansible 执行器、Webhookd、Sidecar 安装器等
- **Algorithms**：基于 BentoML 的机器学习算法服务集合（异常检测、时序预测、图像/目标检测、文本分类、日志聚类）
- **Deploy**：APM、RUM、K8s 采集器、Workflow 等部署编排与契约夹具

项目主要使用 **Python 3.12+**（后端、算法、代理）与 **TypeScript / Node.js**（前端），数据库支持 PostgreSQL / MySQL / SQLite / 达梦 / GaussDB / GoldenDB / OceanBase，消息与采集传输依赖 NATS JetStream，缓存与任务队列依赖 Redis，对象存储使用 MinIO，图数据库使用 FalkorDB（CMDB）。

---

## 2. 仓库结构与模块划分

```
bk-lite/
├── server/                 # Django 后端（apps/ 下按产品域拆分）
│   ├── apps/               # 业务 App
│   │   ├── base/           # 用户、组织、权限、认证
│   │   ├── core/           # 公共工具、OpenAPI 网关、ASGI、日志
│   │   ├── rpc/            # 内部 RPC 与 AppClient
│   │   ├── system_mgmt/    # 系统管理、许可、通知渠道
│   │   ├── console_mgmt/   # 控制台、菜单、应用能力
│   │   ├── node_mgmt/      # 受管节点、Sidecar、Collector
│   │   ├── cmdb/           # 配置管理、资产模型、自动发现
│   │   ├── monitor/        # 监控、告警策略、仪表盘
│   │   ├── alerts/         # 告警中心、Incident、告警处理动作
│   │   ├── log/            # 日志系统、采集、提取、查询
│   │   ├── apm/            # 应用性能监控、Trace、SLO
│   │   ├── rum/            # 真实用户监控
│   │   ├── job_mgmt/       # 作业平台
│   │   ├── patch_mgmt/     # 补丁管理
│   │   ├── operation_analysis/  # 运营分析、仪表盘、报告订阅
│   │   ├── opspilot/       # 智能运维助手、Wiki、工具、Agent
│   │   ├── mlops/          # MLOps 模型管理
│   │   └── workflow/       # 编排中心（Workflow）
│   ├── config/             # Django split-settings 配置组件
│   ├── nats_client/        # NATS 客户端公共代码
│   ├── support-files/      # 发布脚本、Dockerfile、环境模板
│   └── manage.py           # Django 管理入口
├── web/                    # Next.js 16 + React 19 主控制台
│   ├── src/app/            # App Router 产品域页面
│   ├── src/components/     # 共享组件
│   ├── packages/           # pnpm workspace 子包
│   ├── DESIGN.md           # UI/UX token 唯一真相源
│   └── COMPONENT_GOVERNANCE.md  # 组件治理
├── mobile/                 # Next.js 15 + Tauri 2 移动/桌面端
├── webchat/                # npm monorepo：core/ui/demo
├── agents/                 # 采集与执行代理
│   ├── stargazer/          # 云资源与外部资源采集代理（Sanic）
│   ├── ansible-executor/   # Ansible 执行器
│   ├── webhookd/           # Webhook 接收与渲染
│   └── ...
├── algorithms/             # 机器学习算法服务（BentoML）
├── deploy/                 # 部署编排与契约夹具
├── packages/bklite-rum-sdk/  # RUM SDK
├── specs/capabilities/     # 长期业务/工程/安全/可靠性约束
├── specs/changes/          # 跨会话变更规格
├── docs/                   # 设计文档、ADR、运维 Runbook
├── CONTEXT.md              # 跨模块共享术语
├── PRODUCT.md              # 产品原则
├── DEVELOP.md              # 完整开发/构建/发布/故障 Runbook
└── DESIGN.md               # 设计文档导航
```

### 关键入口文件

| 用途 | 路径 |
|---|---|
| 领域术语与产品取舍 | `CONTEXT.md`、`PRODUCT.md` |
| 后端编码与高风险规则 | `specs/capabilities/backend-engineering.md` |
| 安全、可靠性与质量红线 | `specs/capabilities/{platform-security,platform-reliability,engineering-quality}.md` |
| 对外 API 暴露与网关规范 | `specs/capabilities/openapi-gateway.md` |
| 当前跨会话变更 | `specs/changes/<feature>/spec.md` |
| UI 与组件约定 | `DESIGN.md`、`web/DESIGN.md`、`web/COMPONENT_GOVERNANCE.md` |
| 开发、验证与运行命令 | `DEVELOP.md` |
| Server 启动顺序与依赖边界 | `docs/operations/server-startup-dependencies.md` |
| Server 模块架构与数据流 | `docs/design-docs/agent-architecture-evidence.md` |
| 长期架构决定 | `docs/adr/` |
| 发布记录 | `docs/changelog/` |

---

## 3. 技术栈

### 3.1 后端（`server/`）

- **框架**：Django 4.2.27 + Django REST framework 3.15.2
- **ASGI**：Uvicorn 0.32.0，`asgi.py` 入口
- **配置**：`django-split-settings`，组件化配置在 `config/components/`
- **包管理/运行**：`uv`（`uv sync`、`uv run`）
- **数据库**：PostgreSQL（默认）、MySQL、SQLite、达梦、GaussDB、GoldenDB、OceanBase
- **任务队列**：Celery 5.4.0 + `django-celery-beat` + `django-celery-results`
- **消息中间件**：NATS JetStream（`nats-py`）
- **缓存/会话**：Redis
- **对象存储**：MinIO（`django-minio-backend`、`minio`）
- **图数据库**：FalkorDB（CMDB，禁 Neo4j 语法）
- **ORM/查询**：Django ORM；**禁止 raw SQL / `.raw()` / `RawSQL` / `cursor.execute`**
- **日志**：统一从 `apps.core.logger` 引入 `{app}_logger as logger`
- **测试**：pytest + pytest-django + pytest-bdd + pytest-cov

### 3.2 前端

| 端 | 框架 | 包管理器 | Node 版本 |
|---|---|---|---|
| `web/` | Next.js 16.3 / React 19 / TypeScript / Ant Design 5 / Tailwind 4 | pnpm（`only-allow` 强制） | 24.22.1+（`.nvmrc`） |
| `mobile/` | Next.js 15.5 / React 18 / Tauri 2（Rust） | pnpm | — |
| `webchat/` | TypeScript / React 18，npm workspaces | npm | ≥18.18.0 |

### 3.3 代理与算法

- **Stargazer**：Sanic 24.6 + `uv` + loguru
- **Ansible Executor / Webhookd / 其他 Agent**：`uv` + Python 3.12+
- **Algorithms**：BentoML + MLflow + scikit-learn/Ultralytics，使用 `uv`

---

## 4. 构建与测试命令

### 4.1 Server（`server/`）

```bash
cd server

# 安装依赖
make install              # uv sync --all-groups --all-extras

# 数据库迁移
make migrate              # makemigrations + migrate + createcachetable

# 开发启动（默认 :8011，--reload）
make dev

# 初始化
make server-init          # python manage.py batch_init
make setup-dev-user       # 创建 admin/password 超管

# 测试
make test                 # uv run pytest
make test-unit            # 仅 unit marker
make test-bdd             # 仅 bdd marker
make test-fast            # 跳过 slow
make test-app APP=cmdb    # 指定 app 测试

# 异步任务
make celery               # Celery worker（默认 threads pool）
make celery-beat          # Beat 调度
make start-nats           # NATS listener

# 其他
make shell                # IPython shell_plus
make collect-static       # 收集静态文件
make init-buckets         # 初始化 MinIO bucket
```

**Cursor Cloud / 无本地中间件环境验证**：

```bash
cd server
DB_ENGINE=sqlite DB_NAME=:memory: SECRET_KEY=cursor-cloud-dev ENABLE_CELERY=true \
  uv run pytest <paths> --no-cov
```

### 4.2 Web（`web/`）

```bash
cd web
pnpm install              # 强制 pnpm
pnpm dev                  # :3000（--turbopack）
pnpm build                # 生产构建
pnpm lint                 # ESLint
pnpm type-check           # TypeScript 检查
pnpm storybook            # :6006
```

### 4.3 Mobile（`mobile/`）

```bash
cd mobile
pnpm dev                  # :3001
pnpm dev:tauri            # Tauri 桌面开发
pnpm test                 # Node 测试 + Rust 单测
pnpm build                # Web 产物
pnpm build:android        # Android release
pnpm build:aab            # AAB
```

### 4.4 WebChat（`webchat/`）

```bash
cd webchat
npm install
npm run dev
npm run build
npm run test
```

### 4.5 Stargazer（`agents/stargazer/`）

```bash
cd agents/stargazer
make install              # uv sync --all-groups --all-extras
make run                  # Sanic :8083
make lint                 # pre-commit
make build                # Docker 构建
```

### 4.6 Algorithms（`algorithms/<svc>/`）

```bash
cd algorithms/classify_timeseries_server   # 或其他服务
make install              # uv sync --all-groups --all-extras
make serving              # BentoML :3000
uv run pytest             # 测试
```

### 4.7 APM 数据面（`deploy/apm/`）

```bash
cd deploy/apm
make up                   # 启动契约验证夹具
make test                 # Collector 单元测试
make validate             # Compose 与配置校验
make contract             # 真实 SDK 全链路容器契约
make down                 # 停止
```

---

## 5. 代码风格与工程约定

### 5.1 Python（Server / Agents）

- **格式化**：Black，行宽 **150**
- **导入排序**：isort，配置在 `server/.isort.cfg`
- **Linter**：Flake8，配置在 `server/.flake8`
- **pre-commit**：`server/.pre-commit-config.yaml` 包含 `black`、`isort`、`flake8`、`check_migrate`、`check_requirements`
- **日志**：`server/apps/<app>/` 内统一使用：
  ```python
  from apps.core.logger import {app_name}_logger as logger
  ```
  禁止 `loguru`、就地 `logging.getLogger`、或其他 app 的 `*_logger`。
- **数据库**：使用 Django ORM；**禁止 raw SQL / `.raw()` / `RawSQL` / `cursor.execute`**
- **序列化器**：禁止 `fields = "__all__"`，敏感字段设 `write_only=True`
- **异常**：禁止空 `except` 块；输入校验失败返回 400，鉴权失败返回 403
- **事务**：多步写包 `transaction.atomic()`，通知/外部副作用放 `transaction.on_commit`
- **CMDB 图查询**：使用 FalkorDB 语法，**禁止 Neo4j 语法**

### 5.2 TypeScript / 前端

- **接口**：优先 `interface`，**禁用 `any`**，不可信输入用 `unknown`
- **包管理器**：`web/` 与 `mobile/` 强制 pnpm；`webchat/` 使用 npm
- **i18n**：`web/` 与 `mobile/` 使用 `react-intl`，禁止组件中硬编码中文字符串
- **样式**：颜色/间距取自 `web/DESIGN.md` 语义 token；布局优先 Tailwind `className`
- **组件**：优先复用 Ant Design、现有组件和 Storybook；共享组件升 shared 须 ≥2 真实 app

### 5.3 提交前门禁

仓库根目录 `.husky/pre-commit` 是唯一入口：

- Python 文件变更：触发 `server/.pre-commit-config.yaml`
- `web/` 变更：staged 文件 ESLint + `pnpm type-check`
- `mobile/` 变更：staged 文件 ESLint + `pnpm type-check` + `pnpm test:i18n`
- 运营分析 i18n 相关变更：额外跑 `pnpm test:ops-analysis-i18n`

---

## 6. 测试策略

### 6.1 Server

- **框架**：pytest，配置见 `server/pytest.ini`
- **Marker**：`unit`（无 DB/IO）、`integration`（含 DB/外部依赖）、`bdd`（pytest-bdd 中文 Gherkin）、`slow`
- **全局 fixture**：`authenticated_user`、`api_client`、`request_factory`（`server/conftest.py`）
- **分层测试文件后缀**：`_pure`、`_service`、`_views`
- **覆盖率**：改动代码覆盖率 **≥75%**；关键/安全路径必须覆盖
- **TDD**：新功能/bugfix 按红-绿-重构推进，bugfix 必须带回归测试
- **编排中心 Workflow**：使用独立覆盖率门禁，运行 `scripts/test_workflow.sh`，要求生产代码行覆盖率不低于 80%

### 6.2 Web / Mobile / WebChat

- **Web**：`pnpm lint`、`pnpm type-check`、大量 `test:*` 脚本覆盖关键交互、Playwright E2E
- **Mobile**：Node `--test` 脚本 + `cargo test` for Tauri Rust
- **WebChat**：`npm run build && npm run test`

### 6.3 Algorithms

- 各服务使用 `uv run pytest`
- 传统 ML（异常/时序/日志/文本）最终训练前合并 train+val；深度学习（图像/目标检测）train/val 分离

---

## 7. 安全与可靠性红线

### 7.1 安全

- **凭据**：仅通过 `*.example` / `*.template` 提供样例；真实密钥、`.env`、keystore 永不提交
- **鉴权**：每个 DRF view/action 显式权限校验；NATS handler 校验调用方与 org
- **输入边界**：可膨胀输入限制编码大小、条目数、解码后尺寸/像素与预计内存
- **出站连接**：用户可配置出站目标须经过统一策略校验 DNS、重定向、实际连接
- **日志**：禁止记录密码、token、cookie、payload、响应正文、凭据等敏感信息
- **CMDB**：图查询参数化，禁 Neo4j 语法
- **OpenAPI 网关**：新增对外 API 一律经 OpenAPI 网关暴露（`@openapi_expose`），禁散落 `open_api` 端点

### 7.2 可靠性

- **启动安全**：`batch_init` 完成前，同容器 Supervisor 管理的进程视为未就绪；非关键初始化失败不得阻断服务启动
- **下发不伤宿主**：插件/作业下发须有资源边界、幂等、可回滚；不可逆操作预检
- **异步任务**：使用状态机与 fencing token；持久化成功后执行外部副作用，具备幂等键与失败补偿
- **并发判定**：锁内重读判定依据，不得复用锁前 ORM 对象快照
- **批量遍历**：使用稳定 keyset 游标，避免 offset 导致遗漏或重复

---

## 8. 关键运行时环境变量

| 变量 | 说明 |
|---|---|
| `DB_ENGINE` | 数据库类型：postgresql / mysql / sqlite / dameng / gaussdb / goldendb / oceanbase |
| `DB_NAME/USER/PASSWORD/HOST/PORT` | 数据库连接 |
| `INSTALL_APPS` | 逗号分隔的加载 app（空=全加载） |
| `NATS_SERVERS` / `NATS_NAMESPACE` | NATS 连接与命名空间 |
| `REDIS_CACHE_URL` | Redis 缓存 |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | Celery 队列与结果后端 |
| `NEXTAPI_URL` | Web 前端访问后端的 API 地址 |
| `SECRET_KEY` | Django 密钥 |
| `SELF_BASE_URL` | alerts 回调地址（须对 job_mgmt 网络可达） |
| `WEB_BASE_URL` | 前端入口（告警详情“查看作业”链接） |

模板文件：

- `server/envs/.env.example`
- `server/support-files/env/*.example`
- `web/.env.example`
- `agents/stargazer/.env.example`

> 新增 env 走 `os.getenv` 默认值，**不修改 `.env.example`**（易冲突）。

---

## 9. 工作流与交付约定

- **中文交流和提交；代码标识符遵循现有项目风格**
- **只改任务范围**，保留无关工作区状态，不做全仓格式化
- **拒绝预防性过度设计**：不为未来重构或假想风险新增重复契约清单、兼容层、预留抽象
- 日常改动走快速路径：`核对事实 -> 实现 -> 聚焦验证 -> commit/push`
- 跨域、难回滚、破坏性迁移走 Grill 路径，按需创建 `specs/changes/<feature>/spec.md`
- 修改 `server/` 或 `agents/` 前，按影响范围阅读后端工程、安全、可靠性和质量 capability
- 修改 `web/src/**` 页面/组件/样式时遵守「Web UI 硬约束」短清单，按需深读 `web/DESIGN.md`
- 修改启动脚本、初始化命令、服务依赖或部署配置前，必须阅读 `docs/operations/server-startup-dependencies.md`

---

## 10. 常用验证速查

| 改动落在 | 提交前必跑 |
|---|---|
| `server/` | `cd server && make test`（或 sqlite 快速通道） |
| `web/` | `cd web && pnpm lint && pnpm type-check` |
| `mobile/` | `cd mobile && pnpm lint && pnpm type-check && pnpm test:i18n` |
| `agents/stargazer/` | `cd agents/stargazer && make lint` |
| `webchat/` | `cd webchat && npm run build && npm run test` |
| `algorithms/<svc>` | `cd algorithms/<svc> && uv run pytest` |

---

*本文件由项目实际代码、配置和文档生成。如需了解具体模块细节，请按上文链接深入阅读对应 capability、change spec 与 DEVELOP.md。*
