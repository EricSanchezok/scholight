# Scholight

AI 学术论文搜索引擎——arXiv 单一数据源，段落级向量检索 + 多阶段重排。

## 架构概览

```
scholight/
├── scholight/
│   ├── api/          FastAPI 搜索接口（含 cloud-auth 认证）
│   ├── search/       多阶段检索与融合重排管线
│   ├── store/        Zilliz Cloud 交互层（论文、段落、索引管理）
│   ├── pipeline/     PDF/LaTeX 解析、段落切分、embedding
│   ├── sources/      arXiv 数据源连接器
│   ├── scheduler/    摄入编排与每日同步
│   ├── cli/          Click CLI（search / scheduler / store）
│   ├── models/       Pydantic 数据模型
│   └── db/           PostgreSQL 查询层（搜索历史）
├── cloud-auth/       共享 Auth SDK（独立仓库）
├── scripts/          运维与评测脚本
├── docker/           Docker 部署
└── migrations/       PostgreSQL 迁移文件
```

### 外部依赖

| 组件 | 用途 |
|------|------|
| Zilliz Cloud | 向量数据库（Milvus 兼容），存储 303 万篇论文 + 1.72 亿段落 |
| PostgreSQL (AWS RDS) | 用户认证（cloud-auth） + 搜索历史 |
| Embedding API | 文本向量化（Qwen3-Embedding-0.6B，硅基流动 / faro-hosted） |

---

## 快速上手

```bash
git clone git@github.com:EricSanchezok/scholight.git
cd scholight
cp .env.example .env   # 填入 API key 和密码

uv sync
uv run scholight store health   # 验证 Zilliz Cloud 连通性
uv run scholight search -q "attention mechanism"   # 测试搜索
```

---

## 配置

所有配置通过 `SCHOLIGHT_` 前缀的环境变量注入，模板在 `.env.example`。关键变量：

| 变量 | 必填 | 说明 |
|------|:--:|------|
| `SCHOLIGHT_ZILLIZ_URI` | ✅ | Zilliz Cloud 集群地址 |
| `SCHOLIGHT_ZILLIZ_TOKEN` | ✅ | Zilliz Cloud API 密钥 |
| `SCHOLIGHT_EMBEDDING_API_KEY` | ✅ | Embedding API 密钥 |
| `SCHOLIGHT_EMBEDDING_BASE_URL` | ✅ | Embedding API 端点 |
| `SCHOLIGHT_PG_HOST/PORT/DATABASE/USER/PASSWORD` | ✅ | PostgreSQL 连接 |
| `SCHOLIGHT_AUTH_JWT_SECRET` | ✅ | JWT 签名密钥（留空则每次重启自动生成） |
| `SCHOLIGHT_DATA_ROOT` | | 论文 PDF 和日志的本地存储路径（默认 `./data`） |

---

## 搜索系统

### 两级检索管线

| | Level 1（默认） | Level 2 |
|---|---|---|
| **范围** | 论文元数据（标题 + 摘要） | 论文元数据 + 段落全文 |
| **集合** | `arxiv_papers`（303 万） | `arxiv_papers` + `arxiv_chunks`（1.72 亿） |
| **算法** | Dense + BM25 hybrid → WeightedRanker(0.6/0.4) | Level 1 全部 + Chunk 粗召回 → Dense 精排 → MaxP 聚合 → RRF 融合 |
| **延迟** | ~300ms | ~1s |
| **适用场景** | 日常检索 | 深度搜索、需要段落证据 |

Level 2 能发现标题/摘要不匹配但正文强烈相关的论文（chunk-only discover），并附带每个命中的 top-3 段落作为佐证。

### 调用方式

**CLI：**

```bash
uv run scholight search -q "your query"              # Level 1，10 条结果
uv run scholight search -q "your query" --level 2    # Level 2，带段落证据
uv run scholight search -q "your query" -k 20         # 20 条结果
uv run scholight search -q "your query" --json         # JSON 输出
```

**API（POST /search）：**

```json
{ "query": "attention mechanism", "level": 1, "top_k": 10 }
```

Level 2 的返回在 `SearchHit.chunks` 中附带段落证据（`[{chunk_id, chunk_idx, score}]`），Level 1 的 `chunks` 为空列表。

### 可调超参数

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `SCHOLIGHT_SEARCH_HYBRID_DENSE_WEIGHT` | 0.60 | 论文级 Dense 权重 |
| `SCHOLIGHT_SEARCH_HYBRID_BM25_WEIGHT` | 0.40 | 论文级 BM25 权重 |
| `SCHOLIGHT_SEARCH_CHUNK_AGGREGATION_ALPHA` | 0.5 | MaxP/SumP 混合比（0=纯 SumP，1=纯 MaxP） |
| `SCHOLIGHT_SEARCH_POSITION_WEIGHT_BETA` | 0.3 | 段落位置加权（靠后的结论段得 boost） |
| `SCHOLIGHT_SEARCH_RRF_K` | 60 | RRF 平滑常数 |
| `SCHOLIGHT_BM25_COARSE_TOP_K` | 30 | Level 2 BM25 粗召回 Top-K |
| `SCHOLIGHT_DENSE_REFINE_TOP_K` | 256 | Level 2 Dense 精排 Top-K |
| `SCHOLIGHT_SEARCH_LEVEL` | 3 | 论文级 AUTOINDEX 召回率（3≈95%） |

---

## CLI 命令

### 搜索

```bash
uv run scholight search -q <query> [-k 10] [--level 2] [--json]
```

### Zilliz Cloud 管理

```bash
uv run scholight store status        # 快速查看连接与数据量
uv run scholight store health        # 7 层渐进诊断
uv run scholight store health -d indexes -d vectors   # 只看索引和向量维度
uv run scholight store health --deep --yes             # 全表深度扫描（耗 CU，谨慎）
uv run scholight store init          # 创建 collection + 索引（首次部署）
```

### 摄入调度

```bash
uv run scholight scheduler paper-sync     # OAI-PMH 元数据同步
uv run scholight scheduler status         # 调度任务状态
```

---

## 监控与健康检查

`scholight store health` 执行 7 层诊断：

| 层 | 检查内容 | Zilliz Cloud 适配 |
|----|---------|-------------------|
| L0 Connection | 连通性、服务器版本 | ✅ |
| L1 Collections | Collection 存在性、schema | ✅ |
| L2 Indexes | 每个索引状态、pending 行数 | ✅ |
| L3 Segments | loaded/persistent segments | 自动跳过（Cloud 不暴露） |
| L4 Data Stats | 行数、年份分布 | ✅ |
| L5 Resources | Pipeline flag 覆盖率 | ✅ |
| L6 Vectors | 零向量比例 | ✅ |
| L7 Consistency | papers ↔ chunks 交叉校验 | ✅ |

所有层在 quick 模式（默认）下 5 秒内完成。`--deep` 模式会做全表扫描，使用前会弹出成本确认。

---

## Docker 部署

```bash
cp .env.example .env   # 填入所有必填配置
docker compose --env-file .env build
docker compose --env-file .env up -d
```

容器只运行 FastAPI 服务器。arXiv 同步守护进程（paper-sync、PDF 下载、段落切分、向量入库）运行在宿主机上，不进入容器。

---

## 开发

```bash
uv run pre-commit install
uv run ruff check          # Lint
uv run ruff format --check # 格式检查
uv run mypy scholight        # 类型检查（strict mode）
uv run pytest scholight/ -v  # 测试
```

---

## License

Internal use — SanchezCloud
