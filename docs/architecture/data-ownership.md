# SanchezCloud 身份与产品数据边界

## 三个仓库的唯一职责

| 仓库 | 管理内容 | PostgreSQL 所有权 | 明确不管理 |
| --- | --- | --- | --- |
| `cloud-auth` | 共享邮箱身份、密码、验证、全局账号状态、登录锁定、按产品隔离的 Refresh Session、JWT 签发与校验 | `auth.users`、`auth.refresh_tokens`、`auth.schema_migrations` | 产品角色、产品封禁、订阅、额度、Usage、Access Key、业务历史 |
| `scholight` | 学术搜索、Scholight 产品准入/封禁、搜索额度、Access Key、Usage、搜索历史；arXiv/Zilliz 数据管线 | `scholight.*`、`scholight.schema_migrations` | auth migration、其他产品数据；任何身份外键以外的跨 schema 写入 |
| `scholens` | Scholens 文档与协作业务、产品角色/管理员、产品准入/封禁、产品订阅与 Usage | `scholens.*`、`scholens.schema_migrations` | auth migration、Scholight 数据 |

三个产品共用一个数据库 `sanchezcloud`，但不共用产品表。`public` 不存放应用表。
共享身份由 `auth.users.id` 表示；每个产品通过自己 schema 中以 `user_id` 为主键或外键的表扩展身份。

## 登录与 Session

- 用户在不同产品分别登录，不提供跨站 SSO。
- 每个产品使用稳定且唯一的 `client_id` 和至少 32 bytes 的独立 JWT secret。
- Access Token 必须同时包含该产品的 `aud` 和当前 Session 的 `sid`。
- `auth.refresh_tokens.client_id` 隔离各产品 Session；一个产品只能列出或撤销自己的 Session。
- 浏览器 Refresh Token 只使用产品命名的 HttpOnly Cookie；Access Token 只保存在内存。
- 密码修改/重置是全局身份安全事件，因此撤销该身份的全部产品 Session。
- 产品封禁只修改产品 profile；全局禁用才修改 `auth.users.status`。

## 迁移和数据库角色

基础设施预置并拥有单一数据库，随后创建三个最小权限角色：

- `auth_migrator` 只拥有 `auth`；
- `scholight_migrator` 只拥有 `scholight`；
- `scholight_app` 对运行所需的 `auth`、`scholight` 表拥有 DML，但不能执行 DDL。

cloud-auth 的受保护工作流独立迁移 `auth.*`。Scholight migration 先调用
`assert_schema_compatible()`，只读 `auth.schema_migrations`，然后迁移自己的 schema。
两个 migrator 都没有数据库级 `CREATE`，runner 会拒绝缺失或不归自己所有的 schema。

初始顺序：

1. 数据库 owner 创建角色，并运行 `deploy/production/bootstrap-db.sql` 预置 schema。
2. cloud-auth workflow 运行 `cloud-auth migrate`。
3. 数据库 owner 再运行 bootstrap，授予产品 migrator 对 `auth.users` 的 `REFERENCES` 和 schema ledger 的只读权限。
4. Scholight 运行 `scholight store migrate`。
5. 数据库 owner最后运行 bootstrap，授予应用运行时 DML。

产品部署永远不携带或执行 cloud-auth migration。

## 新产品接入

新增产品 `example` 时，只做以下事项：

1. 在产品仓库定义稳定 `client_id="example"`，生成产品独立 JWT secret 和 `example_refresh` Cookie。
2. 由基础设施创建 `example` schema、`example_migrator` 和 `example_app`，不授予数据库级 `CREATE`。
3. 产品仓库提供自己的 `example.schema_migrations` 与干净 baseline。
4. 产品表通过 `user_id REFERENCES auth.users(id)` 关联共享身份；产品 profile、角色、管理员、封禁、订阅、额度和 Usage 全部留在 `example.*`。
5. 产品启动和 migration 只校验 `auth.schema_migrations`；不得导入、复制或执行 auth SQL。
6. 为 Web 登录配置产品命名 HttpOnly Refresh Cookie；通过 cloud-auth `UserManager` 管理 Session，不直接查询 `auth.refresh_tokens`。
7. CI 从空 PostgreSQL 验证 schema owner、ledger、跨 schema 最小权限及 `public` 无应用表。

如果某项能力只影响一个产品，它默认属于产品 schema；只有跨所有产品都必须一致的身份或全局安全语义，才允许进入 cloud-auth。

## Zilliz 边界

以上重构只允许重建 PostgreSQL。Scholight 的 `arxiv_papers`、`arxiv_chunks`、索引和已有向量数据不参与迁移，任何部署/bootstrap 脚本都不得调用 Zilliz drop、init、restore 或回填操作。
