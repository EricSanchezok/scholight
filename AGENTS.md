# AGENTS.md — Scholight 项目规范

## 项目概述

Scholight 是面向人工智能领域的学术论文搜索引擎，**以 arXiv 为唯一数据源**，使用 **Zilliz Cloud** 作为唯一存储引擎。

## 目录结构

```
scholight/
├── scholight/                 源码根包
│   ├── __init__.py          版本号
│   ├── config.py            Pydantic Settings（SCHOLIGHT_ 前缀）
│   ├── constants.py         全局常量（Embedding 维度、默认 topK 等）
│   ├── logging/             structlog 日志系统
│   │   ├── __init__.py
│   │   ├── config.py        configure_logging() — ProcessorFormatter 统一门面
│   │   ├── cleanup.py       第三方 logger 静音（pymilvus/httpx/asyncpg…）
│   │   └── middleware.py    FastAPI 请求追踪中间件（RequestContext + Timing）
│   ├── models/              数据模型（依赖根：其他包依赖它，不反向）
│   │   ├── __init__.py
│   │   ├── paper.py         PaperRecord — 论文元数据
│   │   ├── chunk.py         Chunk — 段落块（文本 + 章节路径 + embedding ref）
│   │   ├── citation.py      Citation — 引用关系
│   │   └── search.py        SearchRequest / SearchHit / SearchResult
│   ├── sources/             数据源连接器（仅 arXiv）
│   │   ├── __init__.py
│   │   ├── base.py          SourceConnector ABC
│   │   └── arxiv.py         ArxivConnector — bulk tar 读取 + OAI-PMH 增量
│   ├── pipeline/            全文解析 + chunking + embedding
│   │   ├── __init__.py
│   │   ├── parser.py        PDF → 纯文本 PDF 解析
│   │   ├── chunker.py       章节检测 + 段落切分
│   │   └── embedder.py      HTTP API embedding（tenacity retry）
│   ├── store/               Milvus 交互层
│   │   ├── __init__.py
│   │   ├── client.py        连接管理 + collection 生命周期
│   │   ├── schema.py        Collection schema 定义（papers / chunks / citations）
│   │   ├── ingest.py        写入操作（insert / upsert / delete）
│   │   └── query.py         读取操作（search / query / get）
│   ├── search/              搜索引擎（多阶段召回+重排）
│   │   ├── __init__.py
│   │   ├── engine.py        SearchEngine — 编排多阶段管线
│   │   ├── retriever.py     Retriever — ANN 粗排 + 标量过滤
│   │   └── reranker.py      Reranker — Cross-encoder 精排 HTTP API
│   ├── api/                 FastAPI 搜索接口
│   │   ├── __init__.py
│   │   ├── app.py           FastAPI app 工厂 + lifespan
│   │   ├── routes.py        POST /search 路由
│   │   └── deps.py          依赖注入（get_milvus_client, get_search_engine）
│   ├── scheduler/           摄入编排
│   │   ├── __init__.py
│   │   ├── arxiv_paper_sync.py  每日 OAI-PMH 元数据同步（双源容错）
│   │   ├── orchestrator.py  ingest_paper() — 全管线串联
│   │   └── monitor.py       数据目录监听 + 增量触发
│   ├── cli/                  Click CLI 子命令集
│   │   ├── __init__.py       CLI 入口注册
│   │   ├── search.py         scholight search
│   │   ├── scheduler.py      scholight scheduler (paper-sync/pdf-daemon/md-daemon/chunk-daemon/status)
│   │   └── store.py          scholight store (init/status/drop)
├── scripts/                 运维脚本
│   ├── audit_duplicates.py   论文去重审计
│   ├── audit_orphan_pdfs.py  磁盘孤儿 PDF 检测
│   ├── import_pre2007.py     从 arxiv_archive 导入 1991-2006 元数据
│   ├── check_env.py          环境快照采集
│   ├── test_extract_pipeline.py  抽取管线对比测试
│   └── benchmark/            检索评测基准
├── tests/
│   ├── conftest.py          共享 fixtures
│   ├── unit/                单元测试
│   └── integration/         集成测试
│   └── 各子包 tests/：      scholight/store/tests/, scholight/search/tests/ 等
├── docker/
│   ├── milvus/              已归档 — 自建 Milvus 2.6 镜像（迁移至 Zilliz Cloud）
│   └── scholight/             应用镜像（后续）
├── docs/
│   ├── research/            调研材料（.gitignore 排除）
│   └── schema/              Milvus Collection Schema 设计文档
├── pyproject.toml           依赖 + CLI 入口
├── .env.example             环境变量模板
├── .gitignore
└── AGENTS.md                本文件
```

## 技术选型

- **唯一数据库**：Zilliz Cloud（managed Milvus），不引入其他数据库
- **唯一数据源**：arXiv（bulk PDF tar + OAI-PMH API），不引入其他数据源
- **部署方式**：启智平台 notebook 容器，向量数据存于 Zilliz Cloud

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

共 11 个 hook：

| Hook | 说明 |
|---|---|
| `ruff-check` + `ruff-format` | Lint + 格式化（line-length=100） |
| `mypy` | 静态类型检查（`strict = true`） |
| `typos` | 代码拼写检查 |
| `bandit` | AST 安全扫描 |
| `vulture` | 死代码检测 |
| `gitlint` | 提交信息规范（feat/fix/docs/refactor/perf/test/chore/ci） |
| `pre-commit-hooks` | 基础卫生：YAML/TOML 验证、行尾空格、合并冲突、大文件、私钥检测等 |

### Ruff 规则集（pyproject.toml）

启用 18 组规则：`E, W, F, I, UP, N, B, C4, SIM, PIE, RUF, ARG, RET, RSE, T20, SLF, TCH, EM`

部分规则已按场景豁免：
- `__init__.py`：允许未使用的导入（用于 re-export）
- `tests/`：允许访问私有成员、嵌套 with 语句
- `scripts/`：允许 `print()`、中文注释
- 全局忽略：大写数学变量名（如 `T`/`N`）、异常字符串字面量、`__all__` 逻辑分组排序

### 手动检查命令

```bash
uv run ruff check              # Lint
uv run ruff format --check     # 格式化检查
uv run mypy scholight            # 类型检查
uv run bandit -c pyproject.toml -r scholight/   # 安全扫描
uv run vulture scholight/ scripts/   # 死代码检测
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
- **运行时**：所有命令通过 `uv run` 执行（如 `uv run scholight scheduler paper-sync`），自动激活 `.venv`。
- **CLI 入口**：`scholight` 命令由 `pyproject.toml` 的 `[project.scripts]` 注册，指向 `scholight.cli:cli`。
- **依赖管理**：全部依赖声明在 `pyproject.toml`，`uv.lock` 锁定版本，不单独使用 `requirements.txt`。
- **环境变量**：配置通过 `SCHOLIGHT_` 前缀的环境变量注入，模板在 `.env.example`。

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
