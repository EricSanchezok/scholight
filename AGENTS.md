# AGENTS.md — Scholight 项目规范

## Git 工作流

- 当前由单人维护，所有开发直接在 `main` 上进行；不要自行创建功能分支。
- 每个完整逻辑变更仍须保持原子提交，并在提交前运行对应测试。

## Design Context

Frontend product decisions are authoritative in [`PRODUCT.md`](PRODUCT.md); visual and interaction decisions are authoritative in [`DESIGN.md`](DESIGN.md). Read both before adding or reshaping frontend UI. Runtime design primitives live in `frontend/src/styles/tokens.css`; do not introduce page-local colors, shadows, typography roles, or motion timing when a semantic token exists.

## 项目概述

Scholight 是面向人工智能领域的学术研究引擎。当前唯一已索引的论文数据源是 arXiv；Zilliz Cloud 保存论文与向量检索数据，PostgreSQL 保存共享身份和 Scholight 产品数据。通用 Web Extract 是独立的读取能力，不等同于论文语料摄入；新增论文来源必须通过独立 connector 接入。

## 目录结构

```
scholight/
├── scholight/                 源码根包
│   ├── __init__.py          版本号
│   ├── config.py            Pydantic Settings（SCHOLIGHT_ 前缀）
│   ├── storage.py           arXiv 数据目录布局（_papers_root 等路径解析）
│   ├── logging/             structlog 日志系统
│   │   ├── __init__.py
│   │   ├── config.py        configure_logging() — ProcessorFormatter 统一门面
│   │   ├── cleanup.py       第三方 logger 静音（pymilvus/httpx/asyncpg…）
│   │   └── middleware.py    FastAPI 请求追踪中间件（RequestContext + Timing）
│   ├── models/              数据模型（依赖根：其他包依赖它，不反向）
│   │   ├── __init__.py
│   │   ├── search.py        SearchRequest / SearchHit / SearchResult
│   │   └── history.py       搜索历史记录模型
│   ├── sources/             数据源连接器（仅 arXiv）
│   │   ├── __init__.py
│   │   └── arxiv.py         ArxivConnector — OAI-PMH 增量 + bulk tar 读取
│   │   └── tests/           test_arxiv_id.py
│   ├── pipeline/            全文解析 + chunking + embedding
│   │   ├── __init__.py      延迟导入门面
│   │   ├── parser.py        PDF → 纯文本（MinerU API）
│   │   ├── pdf_md.py        PDF → Markdown（pymupdf / pymupdf4llm）
│   │   ├── latex_md.py      LaTeX 源码 → Markdown（pandoc 管线）
│   │   ├── embedder.py      HTTP API embedding（tenacity retry）
│   │   └── chunkers/        段落切分
│   │       ├── md_chunker.py     章节检测 + 段落切分（默认）
│   │       └── content_list_chunker.py  备用切分策略
│   │   └── tests/           test_md_chunker.py
│   ├── store/               Zilliz Cloud 交互层
│   │   ├── __init__.py
│   │   ├── client.py        连接管理 + collection 生命周期
│   │   ├── schema.py        Collection schema 定义 + 索引管理
│   │   ├── fields.py        标量 / 向量字段常量
│   │   ├── ingest.py        写入操作（insert / upsert / delete）
│   │   ├── query.py         读取操作（search / query / get）
│   │   ├── concurrent.py    并行 ingest worker（多 MilvusClient 实例）
│   │   ├── export.py        逻辑导出 / 恢复（cursor-scan JSONL）
│   │   ├── health.py        7 层渐进健康检查（HealthChecker）
│   │   └── tests/           test_ingest / test_fields / test_guard / test_health
│   ├── search/              搜索引擎（多阶段召回+重排）
│   │   ├── __init__.py
│   │   ├── engine.py        SearchEngine — 编排管道
│   │   ├── base.py          Phase / Pipeline / PipelineContext ABC
│   │   ├── level1/          Level 1 — 粗排（ANN + BM25）
│   │   │   ├── phases.py    EmbedPhase / AnnSearchPhase / FusionPhase
│   │   │   ├── pipeline.py  Level1 管道构建
│   │   │   └── strategies.py  策略字典
│   │   ├── level2/          Level 2 — 精排（chunk aggregation / RRF / position）
│   │   │   ├── phases.py    ChunkAggPhase / RrfPhase / PositionPhase
│   │   │   ├── pipeline.py  Level2 管道构建
│   │   │   ├── strategies.py  策略字典
│   │   │   └── tests/       test_phases.py
│   │   ├── level3/          占位（预留）
│   │   ├── common/          共享组件
│   │   │   ├── aggregation.py  MaxP / SumP 聚合
│   │   │   ├── fusion.py    RRF 融合 + 矩阵构建
│   │   │   └── tests/       test_aggregation.py
│   │   └── tests/           test_score_fusion.py
│   ├── api/                 FastAPI 搜索接口
│   │   ├── __init__.py
│   │   ├── app.py           FastAPI app 工厂 + lifespan + auth
│   │   ├── deps.py          依赖注入（get_milvus_client, get_search_engine, get_current_user）
│   │   ├── routes/
│   │   │   └── search.py    POST /search
│   │   └── middleware/
│   │       └── cors.py      setup_cors()
│   ├── db/                  PostgreSQL 访问层（asyncpg）
│   │   ├── __init__.py
│   │   ├── client.py        连接池管理（create_pool / get_pool / close_pool）
│   │   ├── migrate.py       数据库迁移
│   │   └── queries_history.py  搜索历史查询
│   ├── scheduler/           摄入编排
│   │   ├── __init__.py
│   │   ├── metadata_sync.py 每日 OAI-PMH/API 元数据同步与任务入队
│   │   ├── ingest_worker.py 单论文下载→解析→切块→Embedding→安全写入
│   │   └── resources.py     确定版本资源下载与安全解包
│   ├── cli/                  Click CLI 子命令集
│   │   ├── __init__.py       CLI 入口注册（lazy-import 子命令）
│   │   ├── search.py         scholight search
│   │   ├── scheduler.py      scholight scheduler（sync / serve-sync / serve-ingest / status / backfill）
│   │   └── store.py          scholight store（init / status / drop / backup / restore / health）
│   └── utils/               公共工具
│       ├── http.py          HTTP 请求重试 / 指数退避
│       └── marker.py        Marker BlockType 转换工具
├── sanchezcloud-identity/              共享 Auth SDK（独立 repo，.gitignore）
├── scripts/                 运维脚本
│   ├── audit_duplicates.py   论文去重审计
│   ├── audit_orphan_pdfs.py  磁盘孤儿 PDF 检测
│   ├── check_env.py          环境快照采集
│   ├── test_extract_pipeline.py  抽取管线对比测试
│   └── benchmark/            检索评测基准（runners / tuning / run.py）
├── migrations/              PostgreSQL 产品迁移（Identity 迁移不在本仓库）
│   ├── 001_scholight_baseline.sql
│   ├── 002_ingestion_queue.sql
│   ├── 003_admin_metrics.sql
│   ├── 004_allow_delegated_usage_actor.sql
│   ├── 005_survey.sql       Survey 首次发布的完整 Schema
│   └── 012_access_keys_all_tools.sql
├── docker/
│   └── scholight-api/       API 服务 Dockerfile + start.py
├── pyproject.toml           依赖 + CLI 入口
├── _typos.toml              代码拼写检查字典
├── global-bundle.pem        AWS RDS SSL 证书
├── .env.example             环境变量模板
├── .pre-commit-config.yaml
├── docker-compose.yml
├── .gitignore
└── AGENTS.md                本文件
```

## 技术选型

- **检索存储**：Zilliz Cloud（managed Milvus）只保存论文、段落、向量与索引；账户、额度、Usage 和历史保存在 PostgreSQL
- **当前论文数据源**：arXiv（bulk PDF tar + OAI-PMH API）；新增来源通过独立 connector 接入，不把 Web Extract 当作摄入管线
- **部署方式**：正式发布使用 `deploy/ecs/` 的共享 SanchezCloud Fargate 平台；
  `deploy/production/` 仅是冻结的旧 EC2 回退参考。向量数据继续存于 Zilliz Cloud。

## Zilliz Cloud 连接

Zilliz Cloud 是托管的 Milvus 服务。应用通过以下环境变量连接：

```bash
SCHOLIGHT_ZILLIZ_URI=https://in05-d432d46d6c77308.serverless.ali-cn-hangzhou.cloud.zilliz.com.cn
SCHOLIGHT_ZILLIZ_TOKEN=your_api_key
```

这些配置在 `scholight/config.py` 中通过 Pydantic Settings 加载，由 `scholight/store/client.py` 的 `_resolve_uri()`/`_resolve_token()` 解析。

## 代码质量工具链

本仓库配置了完整的代码质量检查体系：

### Pre-commit hooks（`.pre-commit-config.yaml`）

在每次 `git commit` 前自动运行，安装方式：

```bash
uv run pre-commit install    # pre-commit 阶段
uv run pre-commit install --hook-type commit-msg   # 提交信息检查
```

共 20 个 hook：

| Hook                         | 说明                                                                  |
| ---------------------------- | --------------------------------------------------------------------- |
| `pre-commit-hooks`           | 基础卫生：YAML/TOML/JSON 验证、行尾空格、合并冲突、大文件、私钥检测等 |
| `typos`                      | 代码拼写检查                                                          |
| `ruff-check` + `ruff-format` | Lint + 格式化（line-length=100）                                      |
| `mypy`                       | 静态类型检查（`strict = true`）                                       |
| `bandit`                     | AST 安全扫描                                                          |
| `vulture`                    | 死代码检测                                                            |
| `gitlint`                    | 提交信息规范（feat/fix/docs/refactor/perf/test/chore/ci）             |

### Ruff 规则集（pyproject.toml）

启用 18 组规则：`E, W, F, I, UP, N, B, C4, SIM, PIE, RUF, ARG, RET, RSE, T20, SLF, TCH, EM`

部分规则已按场景豁免：

- `__init__.py`：允许未使用的导入（用于 re-export）
- `tests/`：允许访问私有成员、嵌套 with 语句
- `scripts/`：允许 `print()`、中文注释
- 全局忽略：大写数学变量名（如 `T`/`N`）、异常字符串字面量、`__all__` 逻辑分组排序

`scripts/archive/` 已在 ruff/bandit/vulture 中全局排除。

### 手动检查命令

```bash
uv run ruff check              # Lint
uv run ruff format --check     # 格式化检查
uv run mypy scholight            # 类型检查
uv run bandit -c pyproject.toml -r scholight/   # 安全扫描
uv run vulture scholight/         # 死代码检测
uv run pip-audit               # 依赖漏洞扫描（需网络）
```

## 日志系统

日志系统基于 `structlog` + `ProcessorFormatter` 统一门面，具体架构：

- **`configure_logging()`** — 一次调用配置全应用，支持 JSON/Console 双模式自动检测
- **JSON 模式自动启用**：`SCHOLIGHT_LOG_JSON=1` 或 stdout 非 TTY 时（生产/容器环境）
- **请求追踪**：`RequestContextMiddleware` 自动注入 `request_id`/`method`/`path`，通过 `contextvars` 传播到所有下游调用
- **第三方库静音**：`cleanup.py` 默认静音 pymilvus/httpx/asyncpg/uvicorn，可通过 `SCHOLIGHT_LOG_{LIBRARY}=DEBUG` 覆盖
- **使用方式**：`logger = structlog.get_logger(__name__)`，用 keyword args 绑定上下文

入口点调用示例：

```python
from scholight.logging import configure_logging
configure_logging(log_level="INFO", use_json=True)  # server
configure_logging(log_level="DEBUG", use_json=False) # CLI
configure_logging(log_level="INFO", use_json=True, file_handler=("app.log", 50_000_000, 20))
```

## 调研规范

- **所有调研材料统一存放**：调研文档、论文、GitHub repo 分析等放入 `docs/research/` 目录
- **目录结构**：`docs/research/` 下按主题分类
- **调研可追溯**：在每个调研文档中记录来源（URL、DOI、日期），方便后续回查
- **git ignore**：`docs/research/` 不计入版本控制

## 变更提交规范

- **每次变更后立即 commit**：任何代码修改、文档更新、配置变更完成后，应立即用 `git commit` 提交
- **commit message 要求**：简洁明确，概括变更内容和原因（1-2 句）
- **原子提交**：每个 commit 聚焦一个逻辑变更
- **防止代码丢失**：频繁提交确保代码不丢失，任何阶段性成果都应及时落地
- **测试数据不丢 /tmp**：MinerU 解析结果、中间产物等产出文件统一存放到 `data/` 目录，禁止散落在 `/tmp`

## Python 环境规范

- **虚拟环境**：项目使用 `.venv` 目录（`uv venv`），**禁止污染系统 Python**。
- **运行时**：所有命令通过 `uv run` 执行（如 `uv run scholight scheduler sync`），自动激活 `.venv`。
- **CLI 入口**：`scholight` 命令由 `pyproject.toml` 的 `[project.scripts]` 注册，指向 `scholight.cli:cli`。
- **依赖管理**：全部依赖声明在 `pyproject.toml`，`uv.lock` 锁定版本，不单独使用 `requirements.txt`。
- **环境变量**：配置通过 `SCHOLIGHT_` 前缀的环境变量注入，模板在 `.env.example`。
- **本地开发权威手册**：端口、共享 PostgreSQL、启动 profile 与远程依赖边界统一由
  [`DEVELOPMENT.md`](DEVELOPMENT.md) 定义，并服从 sanchezcloud-identity handbook。
- **固定端口块**：Scholight 使用 `7200-7299`；Frontend `7200`、API `7201`、Extract
  调试入口 `7202`。共享 PostgreSQL `55432`、MinIO `59000/59001`，全部绑定
  `127.0.0.1`，端口占用时必须失败，不得自动换端口。
- **启动无副作用**：`./scripts/dev.sh` 只启动 Frontend/API；不得安装依赖、执行迁移、
  修复 grant 或启动摄入。迁移只能显式使用 `scholight_migrator`，运行时只能使用
  `scholight_app`。

## 本地混合集成环境

开发和验收 Survey、认证、Quota、Usage、History 或其他 PostgreSQL 业务功能时，默认使用以下隔离拓扑：

```text
本地 Frontend → 本地 Scholight API
                    ├── 本地 PostgreSQL 16（auth + scholight Schema）
                    ├── 本地 MinIO（S3-compatible Survey Artifact）
                    └── 远端 Zilliz（仅论文搜索，只读）
```

- **PostgreSQL 必须本地隔离**：使用临时 Docker PostgreSQL 16，依次运行 sanchezcloud-identity 和 Scholight migrations；不得读取项目中指向生产 RDS 的 `.env`，不得复制生产用户数据。
- **Artifact 必须本地隔离**：本地使用 MinIO，而不是生产 AWS S3。通过 `SCHOLIGHT_SURVEY_S3_ENDPOINT_URL` 指向 MinIO，并使用专用本地 Bucket 和测试凭据。MinIO 实现 S3 API，因此报告、Manifest、presigned URL、SHA 校验与 cleanup 流程仍使用真实对象存储协议。
- **Zilliz 仅限只读搜索**：本地可连接远端 Zilliz 以获得真实论文搜索结果；优先使用 collection-scoped/read-only Key。不得在该环境启动 `metadata-sync`、`paper-ingest`、backfill、scheduler sync、store 维护或任何可能写入/删除 Zilliz 的命令。
- **本地启动不得隐式读取旧 `.env`**：必须由开发脚本显式加载 `.env.local` 并设置
  `SCHOLIGHT_DISABLE_DOTENV=1`；发现非 `127.0.0.1:55432` PostgreSQL 时立即退出。
- **允许启动的服务**：Frontend、API、Survey Draft worker、Survey worker，以及本地 PostgreSQL/MinIO。论文摄入服务默认保持停止。
- **模型按测试层级选择**：日常和 CI 使用固定假模型，不消耗真实 Token；最终人工验收可显式注入真实 `DEEPSEEK_API_KEY` 和 `IMAGE_GEN_API_KEY`。Secret 不得写入 Compose、测试产物、日志或 Git。
- **本地模型凭据单一来源**：真实模型人工测试从项目根目录、Git 忽略且权限为 `0600` 的 `.env` 读取 `DEEPSEEK_API_KEY` 和 `IMAGE_GEN_API_KEY`；生产仍只从 Parameter Store 读取。不得把完整生产 `runtime.env` 复制到本机或提交任何 Secret。
- **生产边界不混用**：本地环境不得连接生产 RDS 或生产 Survey S3 Bucket；生产运行时不得配置 MinIO endpoint。任何需要远端写操作的测试必须另行获得明确授权。
- **环境可重建**：本地 PostgreSQL、MinIO Bucket 和测试账户都视为可丢弃状态；测试结果不得依赖手工修改后的持久容器。

## 测试规范（TDD）

项目遵循 **测试驱动开发**：先写测试 → 确认测试失败 → 实现代码 → 测试通过。

### 测试文件位置

每个子包都有自己的 `tests/` 目录，与源码同层：

```
scholight/store/
├── client.py
├── ingest.py
├── ...
└── tests/
    ├── __init__.py
    ├── test_ingest.py
    └── test_schema.py
```

### 测试原则

- **外部依赖隔离**：测试不连接真实 Milvus 或 arXiv API。用 monkeypatch 替换客户端方法。
- **轻量优先**：单个测试文件应在 **5 秒内** 跑完，禁止重量级 setup/teardown。
- **少 mock，多 stub**：只替换外部边界（`get_client()`、`httpx.AsyncClient.get()`），内部逻辑用真实调用。
- **一个测试一个断言**：`test_mark_pending_inserts_row` / `test_mark_pending_skips_same_version` / `test_mark_pending_updates_changed_version`，每个函数只测一个场景。
- **语言**：测试函数名用英文，与代码库一致。

### 运行测试

```bash
uv run pytest scholight/store/tests/ -v      # 某个子包的测试
uv run pytest scholight/ -v                  # 全部测试
uv run pytest scholight/ -v --tb=short       # 失败时简洁 traceback
```

### 示例：`maybe_mark_arxiv_pending` 的 TDD 流程

1. 创建 `scholight/store/tests/test_ingest.py`
2. 写 5 个测试函数，每个 mock `get_client()` 返回假的 `query()`/`insert()`：
   - `test_insert_new_arxiv_id` — 首次插入返回 True
   - `test_skip_same_version` — 相同 updated 返回 False
   - `test_update_different_version` — 不同 updated 删除再插入
   - `test_query_failure_raises` — Milvus 异常时 raise StoreError
   - `test_insert_failure_after_delete` — delete 成功但 insert 失败时的行为
3. `uv run pytest scholight/store/tests/test_ingest.py -v` → 5 个 FAIL
4. 实现 `maybe_mark_arxiv_pending` 直到 5 个 PASS
5. Commit（实现 + 测试一起提交）
