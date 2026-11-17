# 检验报告唯一性保障 + 测值修订服务

为持续补录的检验报告表 `reports` 在线建立 **(实验室号, 报告号)** 唯一索引，防止重复报告入仓；
并为已入仓报告提供**按报告主键提交测值更正**的能力，支持网络重试造成的乱序/重复/并发到达。
技术栈：FastAPI + PostgreSQL（`CREATE UNIQUE INDEX CONCURRENTLY` + advisory lock 串行化修订）。

## 1. 快速启动（Docker Compose）

```bash
docker compose up --build
# API:   http://localhost:8000
# 文档:  http://localhost:8000/docs   （Swagger UI，可直接发起请求）
# OpenAPI: http://localhost:8000/openapi.json
```

- 首次启动 PostgreSQL 自动执行 `sql/01_schema.sql` 与 `sql/04_revisions.sql`（均幂等）。
- API 启动时也会校准 schema：仅当修订表/生效列缺失时才执行迁移 DDL。
- 健康检查：`GET /health`（db 与 api 两个服务均带 healthcheck）。

## 2. 数据模型

`reports`（`sql/01_schema.sql`）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | BIGINT IDENTITY 主键 | 主键流水号（修订按此提交） |
| `lab_no` | TEXT NOT NULL | 实验室号 |
| `report_no` | TEXT NOT NULL | 实验室内报告号 |
| `measured` | INTEGER NOT NULL | **当前已生效**的整数测值 |
| `effective_revision` | INTEGER NOT NULL DEFAULT 0 | 当前连续生效到的修订号，0 表示尚无修订生效 |

目标唯一索引：`uq_reports_lab_no_report_no ON reports (lab_no, report_no)`，
键为「实验室号 + 报告号」，即**实验室内报告号唯一**（不同实验室允许相同报告号）。

`report_revisions`（`sql/04_revisions.sql`）：

| 列 | 类型 | 说明 |
|---|---|---|
| `report_id` | BIGINT REFERENCES reports(id) ON DELETE CASCADE | 报告主键 |
| `revision_no` | INTEGER CHECK (>= 1) | 修订号，从 1 起递增 |
| `measured` | INTEGER NOT NULL | 该修订提交的整数测值 |
| `applied` | BOOLEAN NOT NULL DEFAULT FALSE | 是否已按序生效；false = 缺口后暂存 |
| `created_at` / `applied_at` | TIMESTAMPTZ | 记录时间/生效时间 |

主键 `(report_id, revision_no)` 兜底同号唯一；另有
`report_revisions` 上的部分索引 `idx_report_revisions_pending` 便于读取待补修订。

另有轮次表 `index_build_rounds` 持久化每次索引构建的状态、后端 PID 与错误信息，用于重启接续。

## 3. API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/reports` | 单条补录；唯一索引生效后重复键返回 `409`，原数据不变 |
| POST | `/reports/batch` | 批量补录，重复键逐条跳过，返回成功/跳过明细 |
| GET | `/reports` | 分页查询报告（返回**已生效**测值与 `effective_revision`） |
| POST | `/reports/{report_id}/revisions` | 提交一条测值修订 |
| GET | `/reports/{report_id}/revisions` | 查看生效号、下一待补号与全部修订记录 |
| POST | `/index/build` | 发起/复用 `CREATE UNIQUE INDEX CONCURRENTLY`（202） |
| GET | `/index/status` | 查询轮次状态 + `pg_index` 中索引的真实有效性 |
| POST | `/index/cancel` | 受理取消（202 `cancel_requested`），SQL 确认停止后才 `cancelled` |

### 3.1 测值修订语义

`POST /reports/{id}/revisions`，请求体 `{"revision_no": 2, "measured": 220}`：

- **顺序生效**：修订号从 1 起递增。当前 `effective_revision = k` 时，只有当 `k+1`
  号的修订已经存在才会应用它，并把它与 `reports.measured`、`effective_revision`
  一起推进；若 `k+2`、`k+3` 也已暂存，会在同一事务内继续连续应用。
- **缺口只暂存**：前序缺失时，修订照常保存（`applied=false`），但**不改当前值**，
  也绝不越过缺口。`/reports` 只显示已生效测值。
- **幂等重发**：同号同值重发返回 `outcome="duplicate"`，不重复应用、不新增记录。
- **冲突拒绝**：同号异值返回 `409`，返回已存测值与本次提交值，任何数据都不改。
- **响应**（始终给出重传所需信息）：
  ```json
  {
    "report_id": 1, "revision_no": 2, "measured": 220,
    "outcome": "pending",            // applied | pending | duplicate
    "newly_recorded": true,
    "applied_now": [],               // 本次连续应用到的修订号
    "effective_revision": 0,
    "effective_measured": 100,
    "next_required_revision": 1      // 下一个待补号，供实验室重传
  }
  ```
- 报告主键不存在：`404 report_not_found`；修订号 < 1 或非整数：`422`。

### 3.2 并发与失败一致性

- 每次提交在**单个数据库事务**内完成：事务开始先取该报告的
  `pg_try_advisory_xact_lock(命名空间<<32 | report_id)`（非阻塞、指数退避重试）。
  同一报告的并发提交串行化，不同报告互不阻塞。
  选用 advisory lock 而非 `SELECT … FOR UPDATE`：语义等价，但不会排入行锁等待队列，
  不依赖 PostgreSQL 的锁唤醒信号（受限沙箱会拒绝跨进程信号）。
- 事务内按序推进 `report_revisions.applied`、`reports.effective_revision` 与
  `reports.measured`；任何语句失败整体回滚，**不会留下部分生效**。
- 持锁超过 `LOCK_WAIT_TIMEOUT_S`（默认 15s）返回
  `503 revision_busy` + `Retry-After: 1`，此时未写入任何数据，可直接重传。
- 数据库错误返回 `503 revision_submit_failed`，同样已整体回滚。
- 连接池大小/取连接等待时间由 `DB_POOL_MIN_SIZE`（默认 2）、`DB_POOL_MAX_SIZE`
  （默认 10）、`DB_POOL_TIMEOUT_S`（默认 15）控制；池超时返回
  `503 revision_pool_exhausted`（未写入，可重传）。

## 4. 索引构建关键设计

- **构建期间补录不阻塞**：使用 `CONCURRENTLY`，不持表锁，普通 INSERT/UPDATE 正常提交。
- **同一时刻只运行一轮，重复发起复用**：
  数据库层用部分唯一索引 `uq_index_rounds_one_active` 保证全局最多一个活跃轮次；
  应用层再次发起时返回同一轮（`reused=true`），不新开。
- **重复键失败保留原数据、去重后可重试**：失败只产生无效索引，`reports` 一行不动；
  系统安全清理无效索引后，用 `sql/03_dedupe.sql` 去重即可重新发起。
- **取消两阶段、不误伤**：
  1. `POST /index/cancel` 先落 `cancel_requested` 并立即返回 202；
  2. 仅对轮次记录在案的构建后端 PID 调用 `pg_cancel_backend`（不触碰补录连接）；
  3. watcher 轮询 `pg_stat_activity`，**确认该后端的 CREATE INDEX 真正停止**后才报
     `cancelled`；若取消未生效而索引已建成，则如实报 `complete` 并保留索引。
- **API 重启接续，不重跑、不虚报**：启动时以系统目录为准对账——
  构建仍在跑则只挂 watcher 接续跟踪（`restarted=true`，同一 backend PID，不重发 DDL）；
  后端已停且索引无效则裁决 `failed/cancelled` 并清理；索引有效则报 `complete`。
- **失败残留安全清理**：只删除经 `pg_index` 确认 `indisvalid=false` 的索引；
  有效唯一索引永远不会被删除。DROP 带 `lock_timeout` 重试，不被长事务无限挂起。

构建轮次状态机：

```
running ──索引有效──▶ complete
running ──重复键等失败──▶ failed（安全清理无效索引，reports 原数据不动，去重后可重试）
running ──受理取消──▶ cancel_requested ──后端 SQL 确认停止──▶ cancelled
索引已有效时再发起/取消：直接报 complete，绝不删除有效索引
```

## 5. 独立 SQL

- `sql/02_backfill_examples.sql`：不依赖 API 的补录示例（单条/批量/外部表，含 `ON CONFLICT DO NOTHING`）。
- `sql/03_dedupe.sql`：重复键排查与去重（保留每组最早一条）。
- `sql/04_revisions.sql`：修订表与生效号列（幂等迁移；已有库也可直接执行）。

## 6. 演示

```bash
# 前置：一个运行中的 API 与 PostgreSQL（本地或 Compose）。
# 本地无 Docker 时可用环境变量覆盖连接，见 demo/common.sh。
bash demo/01_long_transaction.sh        # 长事务拖慢构建 + 构建期补录照常提交
bash demo/02_duplicate_key.sh           # 重复键失败->原数据保留->去重重试成功->409
bash demo/03_restart_recovery.sh        # API 重启接续跟踪，不重跑、不虚报
bash demo/04_cancel.sh                  # 取消受理/确认、不误伤补录、有效索引不删除
bash demo/05_revisions.sh               # 乱序 3/2/1、幂等重发、同号异值 409、连续生效
bash demo/06_concurrent_revisions.sh    # 3 份报告 x 30 修订 x 2 次重发 并发乱序，核验不变量
```

> 受限沙箱若禁止进程信号，`pg_cancel_backend` 无法中止后端；演示 4 会如实展示
> “取消已受理、索引最终建成则报 complete 且保留”，并用等价的「后端已停止」现场
> 演示 `cancelled` 裁决与清理。标准 Docker/宿主环境中信号正常，可直接得到 `cancelled`。

## 7. 运行测试

```bash
export DATABASE_URL=postgresql://postgres@<host>:<port>/<db>
pip install -r requirements.txt pytest httpx a2wsgi
pytest -q
```

测试会在 `DATABASE_URL` 指向的实例中为每个用例新建独立 database 并自动清库，
覆盖：补录/唯一索引构建全流程、单轮复用、取消两阶段、重启接续，以及修订的
乱序、缺口、幂等、冲突、并发提交、锁超时 503、注入失败回滚、与建索引共存。
不依赖 HTTP 的快速自检：`DATABASE_URL=... python3 -m app.revisions_selfcheck`。

## 8. 目录结构

```
app/            FastAPI 应用（main 路由、service 索引编排、revisions 修订服务、
                db 访问层、schemas、responses）
sql/            建表（01）、补录示例（02）、去重（03）、修订表迁移（04）
demo/           六个端到端演示 + 长事务持有助手
tests/          pytest 关键功能自测
Dockerfile, docker-compose.yml, requirements.txt
```

## 9. 已知限制

- 修订号必须从 1 开始；若历史上从未提交过 1 号修订，报告将一直停在
  `effective_revision=0`，响应会持续提示 `next_required_revision=1`。
- 每份报告的修订提交串行执行；极端热点报告（大量并发修订）吞吐受单报告锁限制，
  这是保证顺序与一致性的必要代价。
- `revision_lock_key` 只取 `report_id` 低 32 位；超过 2^32 的主键可能与其它报告共用
  一把锁（仅降低并发度，不影响正确性）。
