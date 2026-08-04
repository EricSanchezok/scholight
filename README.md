# Scholight

AI 学术研究引擎——当前索引 arXiv 语料，提供段落级论文检索、多阶段重排与通用 Web Extract。

前端产品原则以 [`PRODUCT.md`](PRODUCT.md) 为准，视觉与交互系统以 [`DESIGN.md`](DESIGN.md) 为准；新增界面前应先读取两者。
本地端口、共享 PostgreSQL、启动 profile 和远程依赖规则以 [`DEVELOPMENT.md`](DEVELOPMENT.md) 为准。
共享身份接入、数据库角色、升级和排障的权威规范见
[`sanchezcloud-identity` engineering handbook](https://github.com/EricSanchezok/sanchezcloud-identity/blob/main/docs/README.md)；本仓库只维护 Scholight 特有规则。

## 架构概览

```
scholight/
├── scholight/
│   ├── api/          FastAPI 搜索接口（含 sanchezcloud-identity 认证）
│   ├── search/       多阶段检索与融合重排管线
│   ├── store/        Zilliz Cloud 交互层（论文、段落、索引管理）
│   ├── pipeline/     PDF/LaTeX 解析、段落切分、embedding
│   ├── sources/      arXiv 数据源连接器
│   ├── web_extract/  通用网页/文档抓取、渲染与正文抽取
│   ├── scheduler/    摄入编排与每日同步
│   ├── cli/          Click CLI（search / scheduler / store）
│   ├── models/       Pydantic 数据模型
│   └── db/           PostgreSQL 查询层（Scholight 产品数据）
├── scripts/          运维与评测脚本
├── docker/           Docker 部署
└── migrations/       PostgreSQL 迁移文件
```

### 外部依赖

| 组件                 | 用途                                                       |
| -------------------- | ---------------------------------------------------------- |
| Zilliz Cloud         | 向量数据库（Milvus 兼容），存储 303 万篇论文 + 1.72 亿段落 |
| PostgreSQL | 本地共享 PG；生产使用 AWS RDS。保存 `auth.*` 与 `scholight.*` |
| Embedding API        | 文本向量化（Qwen3-Embedding-0.6B，硅基流动 / faro-hosted） |

---

## 快速上手

```bash
git clone git@github.com:EricSanchezok/scholight.git
cd scholight
cp .env.example .env.local   # 只填本地运行密码、应用 secret 和只读 Zilliz key
chmod 600 .env.local
uv sync --all-extras
npm --prefix frontend install
./scripts/dev.sh             # Web 7200 / API 7201
```

不要使用根目录遗留 `.env` 启动本地 API。开发脚本会显式加载 `.env.local`、禁用隐式
dotenv，并拒绝非 `127.0.0.1:55432` 的 PostgreSQL。Zilliz 只允许只读搜索；详细规则见
本地开发手册。

程序化接入与 MCP 配置请从站内 `/docs` 开始。

### Web Extract

`POST /extract` 与 MCP `extract_url` 提供同一套通用读取能力：HTTP/HTTPS GET、公开任意端口、自定义目标请求头、无状态 Cookie、静态/Chromium 自动切换，以及 HTML、JSON、XML、PDF 到 Markdown/文本/原始 HTML 的转换。长内容用短期、身份绑定的 `next_cursor` 延续，不会重新抓取源站。

Web Extract 要求 Access Key，但不消耗搜索日额度。带目标请求头或 Cookie 的调用不进入共享缓存。v1 暂不支持目标 POST、用户代理、自定义 JavaScript 与非 HTTP(S) URL。

抽取 sidecar 仅记录静态/浏览器路径、稳定错误码、耗时、下载/输出字节数与缓存命中指标；目标 URL、Authorization、Cookie 和响应正文不会进入日志或指标。

---

## 配置

所有配置通过 `SCHOLIGHT_` 前缀的环境变量注入，模板在 `.env.example`。关键变量：

| 变量                                                        |  必填  | 说明                                                                                |
| ----------------------------------------------------------- | :----: | ----------------------------------------------------------------------------------- |
| `SCHOLIGHT_ZILLIZ_URI`                                      |   ✅   | Zilliz Cloud 集群地址                                                               |
| `SCHOLIGHT_ZILLIZ_TOKEN`                                    |   ✅   | Zilliz Cloud API 密钥                                                               |
| `SCHOLIGHT_EMBEDDING_API_KEY`                               |   ✅   | Embedding API 密钥                                                                  |
| `SCHOLIGHT_EMBEDDING_BASE_URL`                              |   ✅   | Embedding API 端点                                                                  |
| `SCHOLIGHT_PG_HOST/PORT/DATABASE/USER/PASSWORD`             |   ✅   | PostgreSQL 连接                                                                     |
| `SCHOLIGHT_AUTH_JWT_SECRET`                                 | API ✅ | 固定 JWT 密钥，API 启动要求至少 32 UTF-8 bytes                                      |
| `SCHOLIGHT_PUBLIC_WEB_URL`                                  | API ✅ | 邮箱验证和密码重置邮件返回的产品公开地址                                             |
| `SCHOLIGHT_ANONYMOUS_QUOTA_HMAC_SECRET`                     | API ✅ | 匿名 IP 摘要密钥，至少 32 UTF-8 bytes，独立于 JWT 密钥并跨实例/重启保持一致         |
| `SCHOLIGHT_ACCESS_KEY_HMAC_SECRET`                          | API ✅ | Access Key HMAC-SHA256 密钥，至少 32 UTF-8 bytes；必须独立生成并跨实例/重启保持一致 |
| `SCHOLIGHT_EXTRACT_INTERNAL_TOKEN`                          | API/Extract ✅ | API 与内部抽取 sidecar 之间的共享随机令牌，至少 32 UTF-8 bytes                  |
| `SCHOLIGHT_EXTRACT_SERVICE_URL`                             | API ✅ | 内部抽取 sidecar 地址，Compose 默认 `http://extract:8001`                         |
| `SCHOLIGHT_ANONYMOUS_RATE_LIMIT_PER_MINUTE`                 |        | 匿名共享分钟桶，默认 30 attempts/IP                                                 |
| `SCHOLIGHT_ANONYMOUS_STANDARD_DAILY_LIMIT`                  |        | 匿名 Standard UTC 日额度，默认 100/IP                                               |
| `SCHOLIGHT_ANONYMOUS_THOROUGH_DAILY_LIMIT`                  |        | 匿名 Thorough UTC 日额度，默认 30/IP                                                |
| `SCHOLIGHT_AUTHENTICATED_STANDARD_DAILY_LIMIT`              |        | 登录用户 Standard UTC 日默认额度，默认 1000                                         |
| `SCHOLIGHT_AUTHENTICATED_THOROUGH_DAILY_LIMIT`              |        | 登录用户 Thorough UTC 日默认额度，默认 1000                                         |
| `SCHOLIGHT_CORS_ALLOW_ORIGINS`                              | API ✅ | 明确的前端 origin JSON 列表；生产环境禁止 `*`                                       |
| `SCHOLIGHT_PROXY_HEADERS` / `SCHOLIGHT_FORWARDED_ALLOW_IPS` | API ✅ | 反向代理信任设置；启用时必须列出明确代理 IP/CIDR，禁止 `*`                          |
| `SCHOLIGHT_DATA_ROOT`                                       |        | 论文 PDF 和日志的本地存储路径（默认 `./data`）                                      |

API-only 校验只在 `create_app()` 执行；migration、scheduler 和内部 CLI 不需要匿名 HMAC 密钥。

---

## 搜索系统

### 两级检索管线

|              | Standard（内部 Level 1）                      | Thorough（内部 Level 2）                                         |
| ------------ | --------------------------------------------- | ---------------------------------------------------------------- |
| **范围**     | 论文元数据（标题 + 摘要）                     | 论文元数据 + 段落全文                                            |
| **集合**     | `arxiv_papers`（303 万）                      | `arxiv_papers` + `arxiv_chunks`（1.72 亿）                       |
| **算法**     | Dense + BM25 hybrid → WeightedRanker(0.6/0.4) | Standard 全部 + Chunk 粗召回 → Dense 精排 → MaxP 聚合 → RRF 融合 |
| **延迟**     | ~300ms                                        | ~1s                                                              |
| **适用场景** | 日常检索                                      | 深度全文检索                                                     |

Thorough 是严格模式：Level 2 或其核心 metadata backfill 未完整成功时返回 `503`，不会回退并伪装成 Standard 成功。CLI 和 benchmark 仍使用内部 `level/top_k` DTO；只有 HTTP API 使用下述公共契约。

### 调用方式

**CLI：**

```bash
uv run scholight search -q "your query"              # Level 1，10 条结果
uv run scholight search -q "your query" --level 2    # Level 2，内部诊断可含段落证据
uv run scholight search -q "your query" -k 20        # 20 条结果
uv run scholight search -q "your query" --json       # JSON 输出
```

**公共 API（`POST /search`）：**

`Authorization` 完全缺失时按匿名搜索处理；有效 active access Bearer token 使用登录用户额度。Header 存在但无效、过期、不是 Bearer 或是 refresh token 时返回 `401`，不会降级为匿名。

```json
{
  "query": "retrieval augmented generation",
  "strength": "standard",
  "limit": 10,
  "filters": {
    "categories": ["cs.AI", "cs.IR"],
    "authors": [],
    "date_from": "2020-01-01",
    "date_to": null
  }
}
```

这是 breaking contract：旧 HTTP 字段 `level`、`top_k`、`strategy`、`enable_fusion`、vectors 等均返回 `422`。调用方必须与后端同一 release 切换，不提供双轨解析或 `/v1/search`。

```json
{
  "query": "retrieval augmented generation",
  "strength": "standard",
  "degraded": false,
  "hits": [
    {
      "rank": 1,
      "score": 12.75,
      "arxiv_id": "2401.12345",
      "title": "A Paper About Retrieval",
      "authors": ["Example Author"],
      "abstract": "The full abstract...",
      "categories": ["cs.AI", "cs.IR"],
      "submitted_at": "2024-01-20T00:00:00Z",
      "updated_at": "2024-03-05T00:00:00Z",
      "version": 2,
      "arxiv_url": "https://arxiv.org/abs/2401.12345",
      "pdf_url": "https://arxiv.org/pdf/2401.12345"
    }
  ],
  "result_count": 1,
  "elapsed_ms": 842.37
}
```

`rank` 是响应内的权威顺序。`score` 是未归一化原始信号，只能在当前响应内比较，不能跨 query、strength、索引、模型或时间比较。最终 abstract enrichment 失败或缺行时仍返回 `200`，对应 `abstract=null` 且 `degraded=true`；内部 chunks、vectors、strategy 和阶段诊断不会公开。

### 匿名额度与错误

- Standard/Thorough 共享 `30 attempts/min/IP`；分钟桶统计尝试，失败或验证错误不回滚。
- UTC 日额度独立分桶：Standard 默认 100/IP，Thorough 默认 30/IP。
- 匿名 IP 仅以 HMAC-SHA256 摘要进入 PostgreSQL；原始 IP、完整摘要和密钥不得进入日志或 metrics。
- 分钟或日额度耗尽返回结构化 `429` 并带 `Retry-After`；依赖暂不可用返回结构化 `503`，默认 `Retry-After: 5`。
- 搜索执行或公开响应组装失败会 best-effort 补偿一次日额度；历史和 Usage 的后台持久化失败不会改变已经返回的搜索响应或额度。

### 搜索历史 API

所有历史 endpoint 均要求 active Bearer token：

- `GET /search/history?limit=20&offset=0&q=retrieval`：返回 `{items,total,limit,offset}`。`q` 是 query 文本的大小写不敏感 literal substring；total 与 page 来自同一只读 `REPEATABLE READ` snapshot。
- `POST /search/history/bulk-delete`，body `{"ids":[1,2,3]}`：单条 owner-scoped SQL 批量软删除，返回 `{"deleted":2}`；未知、其他用户或已删除 ID 不计数，重放返回 0。
- `DELETE /search/history/{entry_id}`：保留原有 `200 {"message":"Deleted"}` 和 404 行为。

登录搜索只有在最终响应为 `200` 时才安排历史写。历史写是最终一致的；写入失败不会改变搜索响应或额度，并会被后台任务消费和结构化记录。关闭 API 时会先 drain 未完成历史任务再关闭 PostgreSQL pool。

### Access Key、Usage 与 Session API

以下管理接口只接受登录 JWT。Access Key 固定拥有 `all` 工具权限，不做逐工具 scope；它仍不能访问账户、历史、Usage、Session 或 Key 管理接口。完整 Key 仅在创建响应中出现一次，服务端只保存 HMAC-SHA256 digest。

```bash
API=https://your-scholight.example/api
JWT=your-login-access-token

# 创建 Access Key（每个用户最多 10 个 active keys）
curl -sS -X POST "$API/user/access-keys" \
  -H "Authorization: Bearer $JWT" \
  -H 'Content-Type: application/json' \
  -d '{"name":"literature-review","scopes":["all"],"expires_at":null}'

# 列表不会返回 key 或 digest
curl -sS "$API/user/access-keys" -H "Authorization: Bearer $JWT"

# 使用 Access Key 搜索；扣减 Key 所属用户的额度
ACCESS_KEY=sk_live_xxx
curl -sS -X POST "$API/search" \
  -H "Authorization: Bearer $ACCESS_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"query":"retrieval augmented generation","strength":"standard","limit":10,"filters":{}}'

# 修改名称/有效期及立即撤销
curl -sS -X PATCH "$API/user/access-keys/KEY_UUID" \
  -H "Authorization: Bearer $JWT" -H 'Content-Type: application/json' \
  -d '{"name":"new-name"}'
curl -sS -X DELETE "$API/user/access-keys/KEY_UUID" \
  -H "Authorization: Bearer $JWT"
```

Usage 只保存计量和 server search time，不保存 query、标题、摘要、IP、完整 Key 或内部检索诊断。当天登录用户额度来自 `scholight.user_daily_search_usage`，覆盖值来自 `scholight.user_quota_overrides`，趋势来自幂等的 `scholight.usage_events`；sanchezcloud-identity 不参与额度或 Usage。

```bash
curl -sS "$API/user/usage/summary" -H "Authorization: Bearer $JWT"
curl -sS "$API/user/usage/volume?from=2026-07-01&to=2026-07-31" \
  -H "Authorization: Bearer $JWT"
curl -sS "$API/user/usage/latency?from=2026-07-01&to=2026-07-31" \
  -H "Authorization: Bearer $JWT"
curl -sS "$API/user/usage/records?limit=20&outcome=success" \
  -H "Authorization: Bearer $JWT"
curl -sS -OJ "$API/user/usage/export.csv?from=2026-07-01&to=2026-07-31" \
  -H "Authorization: Bearer $JWT"
```

Session 以 sanchezcloud-identity 的、按 `client_id=scholight` 隔离的 refresh-token family 为单位。Access JWT 必须带 `aud=scholight` 和 `sid`；缺少任一字段均拒绝。浏览器 Refresh Token 仅存在 `Secure + HttpOnly + SameSite=Strict` Cookie，Access Token 只存在内存。

```bash
curl -sS "$API/auth/sessions" -H "Authorization: Bearer $JWT"
curl -sS -X DELETE "$API/auth/sessions/SESSION_ID" -H "Authorization: Bearer $JWT"
curl -sS -X POST "$API/auth/sessions/revoke-others" -H "Authorization: Bearer $JWT"
```

当前不提供自助删除共享身份。产品封禁只修改 `scholight.user_profiles`，不会禁用同一用户在其他 SanchezCloud 产品中的身份。

### 可调超参数

| 参数                                       | 默认值 | 说明                                     |
| ------------------------------------------ | ------ | ---------------------------------------- |
| `SCHOLIGHT_SEARCH_HYBRID_DENSE_WEIGHT`     | 0.60   | 论文级 Dense 权重                        |
| `SCHOLIGHT_SEARCH_HYBRID_BM25_WEIGHT`      | 0.40   | 论文级 BM25 权重                         |
| `SCHOLIGHT_SEARCH_CHUNK_AGGREGATION_ALPHA` | 0.5    | MaxP/SumP 混合比（0=纯 SumP，1=纯 MaxP） |
| `SCHOLIGHT_SEARCH_POSITION_WEIGHT_BETA`    | 0.3    | 段落位置加权（靠后的结论段得 boost）     |
| `SCHOLIGHT_SEARCH_RRF_K`                   | 60     | RRF 平滑常数                             |
| `SCHOLIGHT_BM25_COARSE_TOP_K`              | 30     | Level 2 BM25 粗召回 Top-K                |
| `SCHOLIGHT_DENSE_REFINE_TOP_K`             | 256    | Level 2 Dense 精排 Top-K                 |
| `SCHOLIGHT_SEARCH_LEVEL`                   | 3      | 论文级 AUTOINDEX 召回率（3≈95%）         |

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
uv run scholight scheduler sync                         # 单次连续元数据同步
uv run scholight scheduler serve-sync                   # 每日元数据服务
uv run scholight scheduler serve-ingest                 # 单论文统一摄入 worker
uv run scholight scheduler status --json                # PostgreSQL 队列状态
uv run scholight scheduler enqueue-backfill \
  --from 2024-01-01 --to 2024-01-31 --limit 500         # 默认 dry-run
```

---

## 监控与健康检查

`scholight store health` 执行 7 层诊断：

| 层             | 检查内容                   | Zilliz Cloud 适配        |
| -------------- | -------------------------- | ------------------------ |
| L0 Connection  | 连通性、服务器版本         | ✅                       |
| L1 Collections | Collection 存在性、schema  | ✅                       |
| L2 Indexes     | 每个索引状态、pending 行数 | ✅                       |
| L3 Segments    | loaded/persistent segments | 自动跳过（Cloud 不暴露） |
| L4 Data Stats  | 行数、年份分布             | ✅                       |
| L5 Resources   | Pipeline flag 覆盖率       | ✅                       |
| L6 Vectors     | 零向量比例                 | ✅                       |
| L7 Consistency | papers ↔ chunks 交叉校验   | ✅                       |

所有层在 quick 模式（默认）下 5 秒内完成。`--deep` 模式会做全表扫描，使用前会弹出成本确认。

---

## 生产部署

正式环境的唯一主线是 [`deploy/ecs/`](deploy/ecs/README.md)：五个职责明确的不可变
镜像运行在共享 SanchezCloud Fargate 平台，发布清单固定全部 digest。旧的
[`deploy/production/`](deploy/production/README.md) 单机 Compose 包已冻结，只在迁移
观察期作为旧 EC2 回退参考，不再接收功能或发布流程变更。

sanchezcloud-identity 由其受保护工作流独立迁移 `auth.*`；Scholight 发布只校验
Identity schema 版本，产品 migration workflow 只迁移 `scholight.*`。
完整边界及新产品接入规则见
[`docs/architecture/data-ownership.md`](docs/architecture/data-ownership.md)。

根 Compose 不是日常本地开发入口，且不得读取 `.env.local`。它不再是正式发布
入口；本地开发统一使用 `./scripts/dev.sh`，生产操作遵循 `deploy/ecs/README.md`。

反向代理只应信任明确的 Caddy IP/CIDR，不能把 `SCHOLIGHT_FORWARDED_ALLOW_IPS` 设为 `*`；不要公开路由 `/livez` 或 `/readyz`。生产 CORS 必须使用实际前端 origin 列表并允许凭据。匿名和 Access Key HMAC 配置只注入 API service，不注入 migrate 或 scheduler。

PostgreSQL 的 expand-only migration 保存每日连续游标、摄入任务、重试与租约。
`auth_migrator` 和 `scholight_migrator` 分别拥有自己的 schema，均没有数据库级
`CREATE`；两个 runner 都会验证 schema 已由基础设施预置且归当前角色所有。

ECS 使用独立 ingest digest 运行 `metadata-sync` 和有界 `paper-ingest` tasks。
后者按单篇论文依次完成下载、解析、切块、Embedding 与安全写入；修订时始终先
upsert 完整新 chunks，再按已核验的明确主键差集清理旧 chunks。部署和 migration
不会 drop、重建或批量回填现有 Zilliz collection。

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
