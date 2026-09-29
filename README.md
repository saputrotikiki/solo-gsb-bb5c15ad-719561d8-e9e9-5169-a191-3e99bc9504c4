# 实验室样品分装谱系 API

使用 **Python + FastAPI + SQLite** 实现的样品分装 / 谱系追踪服务。

登记带体积的**初始样品**后，可以**批量**提交一批容器间转移；系统按**源容器当时的混合比例**
传播其中**全部祖先来源**，所有体积运算使用标准库 [`decimal.Decimal`](https://docs.python.org/3/library/decimal.html)
完成，全程不经过 `float`，杜绝二进制浮点误差。

- 一批转移**严格依次发生**：后面的转移能看到同批中前面转移的结果。
- 整批是一个 SQLite 事务：**源液不足、未知容器、自转移、目标容量超限**（或任何参数错误）都会导致
  **整批拒绝、状态完全不变**。
- 可为容器设置**容量上限**（`PUT /containers/{id}/capacity`，空值表示不限制，
  上限必须不小于当前体积）：批量提交与预演在顺序模拟的每一步检查
  “目标当前体积 + 转入量”，任一目标超上限即整批拒绝并返回**条目序号、目标与预计超出量**；
  同批先转出再转入按当时余量判断。容器查询返回**容量、剩余空间与利用率**。
- 可维护一份**祖先来源不相容规则**（`PUT/GET /compatibility/rules`，无向禁配对，
  原子全量替换，A-B 与 B-A 去重，拒绝自配对与未登记为初始样品的 ID）：
  批量提交与预演复用同一顺序模拟，在**每一步分摊后检查目标容器的非零祖先集合**，
  命中禁配对即整批拒绝（`422 incompatible_sources`，返回**条目序号、目标容器与
  排序后的全部冲突对**）；同批前序混合产生的隐性祖先同样参与后续检查。
- 可用 `POST /transfers/preview` **预演**同一批转移：与真实提交完全相同的校验与分摊规则，
  返回每一步的扣取明细与受影响容器的最终状态，但**绝不写入数据库**。
- 可选的**乐观并发保护**：每个容器持久化一个整数 `version`（创建时为 0），
  `GET /containers/{id}` 返回当前版本；批量提交/预演可携带 `expected_versions`
  做版本校验，成功提交后每个被触碰容器的版本恰好加 1（同批多次出现也只加 1）。
- 可选的**幂等键** `idempotency_key`（仅 `/transfers/batch` 生效）：成功请求的
  请求体指纹与完整响应与转移**写入同一 SQLite 事务**；同键同请求体重放首次结果
  （不重复扣减体积、不重复递增 `version`），同键但请求体不同返回
  `409 idempotency_conflict`。预演接口不写幂等记录；不携带键时行为与旧版本完全一致。
- **只追加审计链**：每个**成功提交**的批次在同一事务内追加一条审计记录，包含
  批次摘要（条数、转移总量、受影响容器、幂等键）、逐步扣取明细、每个受影响容器
  提交前后的版本/体积与提交后来源构成、以及结果版本；记录的哈希把**前一条记录的
  摘要**纳入计算，形成可逐条校验的哈希链。审计表由数据库触发器禁止
  UPDATE/DELETE；失败批次、幂等重放与预演**绝不产生记录**。提供按容器、按序号
  范围查询审计记录与一键校验整条链完整性的 API。
- 可查询任一容器的**当前体积**以及各祖先初始样品的**来源占比**；也可以反向追踪——
  `GET /sources/{sample_id}/containers` 按当前组件表找出**所有仍含某初始样品非零份额**
  的容器（容器体积、该来源绝对份额与占比），支持 `min_amount` 过滤与 `limit` /
  不透明 `cursor` **稳定分页**（按 `container_id` 字典序）。游标携带全局
  **`transfer_revision`**：仅成功批次与成功损耗核销在其写事务内原子 +1（预演、
  失败、幂等重放不递增），翻页期间修订号变化返回 `409 stale_cursor`；查询在
  **只读事务**中执行。
- **样品损耗核销**（`POST /containers/{id}/losses`）：把容器体积一次性核销到
  “核销后实测体积”，差额（总损耗）按**核销前各祖先份额比例**，沿用现有定点精度
  与最大余额法从各来源扣减——各来源扣减之和**严格等于总损耗**且不产生负数。
  组件扣减、容器 `version` 与全局 `transfer_revision` 在**单个事务**内各加 1
  （既有来源反查游标随之失效），并在同事务写入由数据库触发器保护的
  **只追加损耗记录**（`GET /containers/{id}/losses` 按新到旧返回）；实测值必须
  介于 0 与当前体积之间，版本冲突或任一校验失败整体回滚，组件、版本、修订号与
  记录均不变。
- **最低留存份额锁定**（`PUT/GET /containers/{id}/retention`）：可为某个容器
  锁定某祖先初始样品的**最低留存体积**（`null` 解除；正值必须不超过该来源当前
  份额，来源须已登记为初始样品，并带容器 `expected_version`）。每次**实际变更**
  使该容器 `version` +1，但**不**改变全局 `transfer_revision`；版本过期或输入
  非法时规则、版本都不变。批量转移提交与预演在顺序模拟的**每一步按既有定点
  分摊后**检查**源容器**各留存份额，任一步低于下限即 422 `retention_breach`
  **整批拒绝**（即使同批后续会转回也一样）；损耗核销按其**实际来源扣减**执行
  同一约束；拒绝时份额、`version`、修订号与记录均不变。

---

## 一条命令启动

需要已安装 Docker 与 Docker Compose v2：

```bash
docker compose up --build
```

启动后服务监听：**http://localhost:8000**

- Swagger 交互文档：http://localhost:8000/docs
- OpenAPI 描述：http://localhost:8000/openapi.json
- 健康检查：http://localhost:8000/health

快速体验祖先来源反向追踪（详见下文 [第 8 节](#8-祖先来源反向追踪仍含某初始样品的全部容器)）：

```bash
curl -X POST localhost:8000/samples -H 'Content-Type: application/json' \
  -d '{"sample_id":"S1","volume":"100"}'
curl -X POST localhost:8000/containers -H 'Content-Type: application/json' \
  -d '{"container_id":"V1"}'
curl -X POST localhost:8000/transfers/batch -H 'Content-Type: application/json' \
  -d '{"transfers":[{"source":"S1","destination":"V1","volume":"40"}]}'
curl 'localhost:8000/sources/S1/containers'
# {"sample_id":"S1","transfer_revision":1,...,"containers":[
#   {"container_id":"S1","volume":"60.000000000","source_amount":"60.000000000",...},
#   {"container_id":"V1","volume":"40.000000000","source_amount":"40.000000000",...}]}
```

容器启动时（`docker-entrypoint.sh`）会：

1. 安装 Python 依赖（`requirements.txt`）；
2. 在 `DATABASE_PATH` 处创建 SQLite 数据库与表（已存在则保持不变，幂等）；
3. 启动 uvicorn。

SQLite 数据保存在 Docker 命名卷 `lineage-data`（容器内 `/data`）中，重启不丢失。

### 旧版本数据库的自动迁移

直接用新镜像启动挂载了**旧数据卷**的容器即可，无需手工迁移——启动时的
`init_db()` 全部是幂等 DDL：

- 旧库缺少 `containers.version` 列时自动 `ALTER TABLE ... ADD COLUMN` 补齐，
  已有容器的版本一律视为 `0`；
- 旧库缺少 `containers.capacity` 列（容量上限）时自动补齐，已有容器一律为
  `NULL`（不限制容量），与未设置容量的新容器行为一致；
- 旧库缺少幂等表时自动创建 `idempotency_keys`
  （`idempotency_key` 主键、`request_sha256` 请求体指纹、`status_code`、
  `response_body` 完整响应、`created`），历史数据不受影响，旧客户端不带键
  请求的行为也完全不变；
- 旧库缺少 `containers.is_sample` 列（是否初始样品）时自动补齐：以
  “当前仍持有自身来源份额”（`components` 中存在 `container_id = source_id`
  的自指行）回填为初始样品，空容器回填为否；已完全转空的旧样品无法被自动
  识别，如需在不相容规则中引用请先人工核对；
- 旧库缺少不相容规则表时自动创建 `compatibility_rules`
  （`source_a`、`source_b` 主键，`CHECK (source_a < source_b)` 规范化存储），
  初始为空表（无任何禁配约束），历史数据不受影响；
- 旧库缺少审计表时自动创建 `audit_records`（`seq` 序号、`prev_hash`、
  `entry_hash`、`payload_json` 规范化载荷、`created`）与反查索引
  `audit_record_containers`，并幂等安装 4 个**只追加触发器**（两表各一个
  `BEFORE UPDATE` / `BEFORE DELETE`，命中即 `RAISE`）。升级前的历史批次没有
  审计记录，审计链从升级后**第一个成功批次**开始，首条 `seq=1`、`prev_hash`
  为 `sha256("")`；
- 旧库自动补出全局修订号单行表 `transfer_revision`（修订号初始为 `0`，
  历史批次不追溯计数）；同时把 `components` 上的旧单列索引
  `idx_components_source` 替换为两个覆盖索引
  `idx_components_source_container(source_id, container_id, amount)`
  （反向追踪按来源的有序仅索引扫描）与
  `idx_components_container_source(container_id, source_id, amount)`
  （正向查询 / 体积聚合按容器的仅索引扫描）；
- 旧库缺少损耗核销表时自动创建 `loss_records`（核销记录：实测体积、总损耗、
  核销前后版本、备注）与 `loss_record_sources`（各来源扣减明细）及按容器的
  反查索引，并幂等安装 4 个**只追加触发器**（两表各一个 `BEFORE UPDATE` /
  `BEFORE DELETE`，命中即 `RAISE`）；历史容器没有任何损耗记录，从首次成功
  核销开始追加；
- 旧库缺少最低留存份额表时自动创建 `retention_locks`
  （`container_id` / `source_id` 主键、`min_volume`）及按容器的覆盖索引，
  初始为空（不限制任何来源）；留存规则是普通可变数据（非只追加），可随时
  修改 / 解除。

升级步骤（保留数据）：

```bash
git pull                                   # 或重新构建镜像
docker compose up --build -d               # 复用 lineage-data 卷，启动即自动迁移
docker compose logs -f api                 # 看到 “database ready” 即迁移完成
```

如需确认迁移结果，可在容器内查询：

```bash
docker compose exec api python -c "
from app.db import connect
c = connect()
print([r[0] for r in c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")])
print([r[1] for r in c.execute('PRAGMA table_info(containers)')])
"
# 应包含 idempotency_keys、audit_records、audit_record_containers、
# compatibility_rules、transfer_revision、loss_records、loss_record_sources、
# retention_locks 表，
# containers 列中应包含 version、capacity 与 is_sample；另应存在 4 个 audit_*
# 触发器、4 个 loss_record* 触发器与
# idx_components_source_container / idx_components_container_source 覆盖索引
```

停止与清理：

```bash
docker compose down          # 停止并删除容器（保留数据卷）
docker compose down -v       # 同时删除数据卷
```

---

## 配置项

全部配置通过环境变量注入（见 `docker-compose.yml` 的 `environment`）：

| 环境变量 | 默认值 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `/data/lineage.db` | SQLite 数据库文件路径，目录不存在会自动创建 |
| `PORT` | `8000` | 容器内监听端口（compose 已映射 `8000:8000`；如改端口需同步修改端口映射） |
| `DECIMAL_SCALE` | `9` | 体积与各来源份额保留的小数位数；请求体积超出该精度会被拒绝（422） |
| `PROPORTION_SCALE` | `6` | 查询结果中来源占比（proportion）的小数位数 |
| `DECIMAL_PRECISION` | `50` | Decimal 中间运算（除法等）的有效数字精度，最小 28 |

---

## 数据模型

- **初始样品**：登记一个带初始体积的容器，它是自身唯一的祖先来源。
- **空容器**：预先登记的空容器，可作为转移目标。
- 每个容器的状态记录为若干条 `{祖先初始样品 ID -> 绝对体积份额}`，份额之和即当前体积。
  Decimal 在数据库中以**字符串**存储。
- 每个容器额外持久化一个整数 **`version`**：创建（样品/空容器）时为 `0`，每次成功的
  批量转移后被触碰的容器加 1（同批多次触碰只加 1），用于 `expected_versions` 乐观并发控制。
  服务启动时会自动为旧版本数据库补齐该列，已有容器的版本视为 `0`，无需手工迁移。
- 每个容器可持久化一个可选的 **`capacity`**（容量上限，`containers.capacity` 列）：
  `NULL` 表示不限制（新登记容器与旧库迁移来的容器默认如此）；非 NULL 为 Decimal 字符串，
  设置时必须不小于容器当前体积。批量提交与预演在顺序模拟的每一步检查
  “目标当前体积 + 本步转入量 ≤ 容量上限”，任一目标超限即整批拒绝
  （`422 capacity_exceeded`）。服务启动时会自动为旧库补齐该列。
  容量修改不是批量转移：不递增 `version`、不产生审计记录。
- 每个容器持久化一个 **`is_sample`** 标记（`containers.is_sample` 列）：
  `POST /samples` 登记的初始样品为 `1`，`POST /containers` 登记的空容器为 `0`。
  不相容规则只允许引用初始样品 ID（祖先来源按构造都是初始样品）。
  服务启动时会自动为旧库补齐该列并按自指份额行回填。
- 表 **`compatibility_rules`** 保存祖先来源不相容规则（无向禁配对）：每行一对
  初始样品 ID，按字典序规范化存储（`source_a < source_b`，主键去重）。
  只能整体替换（`PUT /compatibility/rules`），没有逐条增删接口；规则变更不是
  批量转移：不递增 `version`、不产生审计记录，且只约束其后的转移、不回溯既有容器。
  服务启动时会自动为旧库创建该表（初始为空）。
- 表 **`idempotency_keys`** 仅服务于 `/transfers/batch` 的可选幂等：只在批次成功提交时
  写入一行，记录幂等键、原始请求体的 SHA-256 指纹、首次响应的 HTTP 状态码与完整响应体；
  该行与容器扣减、`version` 递增在**同一个 SQLite 事务**内落库。预演接口不写此表，
  失败的批次（4xx，包括业务错误、`stale_container`、`idempotency_conflict`）也不写。
- 表 **`audit_records`** + **`audit_record_containers`** 是只追加审计链（详见下文
  “审计链”一节）：每个成功批次在转移写入的同一事务内追加一行；序号 `seq` 从 1
  连续递增，`prev_hash` 指向前一条记录、首条为 `sha256("")`，`entry_hash` 覆盖
  前一条哈希、序号与规范化载荷。两表均有 `BEFORE UPDATE` / `BEFORE DELETE`
  触发器，任何应用连接都无法修改或删除既有审计行；失败批次、幂等重放、预演不写。
- 单行表 **`transfer_revision`**（`id=1`，`revision` 初始为 `0`）是全局转移修订号：
  每个**成功批次**与每次**成功损耗核销**都在组件扣减 / `version` 递增 /
  记录追加所在的**同一事务**内原子 `+1`（一批多条转移也只加 1，一次核销加 1）；
  预演、失败批次、失败核销与幂等重放不递增。
  初始样品登记、空容器登记、容量修改与不相容规则替换均不是转移，不触碰修订号。
  祖先来源反向追踪（`GET /sources/{id}/containers`）的分页游标携带查询时刻的
  修订号，用于跨页一致性检测——因此成功核销也会使翻页中的旧游标失效。
- 表 **`loss_records`** + **`loss_record_sources`** 是只追加的样品损耗核销记录
  （详见下文“样品损耗核销”一节）：每次成功核销在扣减事务内追加 `loss_records`
  一行（`id` 全局递增、容器、核销后实测体积、总损耗、核销前后 `version`、
  可选备注、追加时间）与 `loss_record_sources` 每个被扣减来源一行
  （`source_id` / `amount`，按 `source_id` 排序）；各来源扣减之和严格等于
  `loss_volume`。两表均有 `BEFORE UPDATE` / `BEFORE DELETE` 触发器，任何应用
  连接都无法修改或删除既有行；应用只做 INSERT，不提供修改 / 删除接口。
  版本冲突或校验失败的核销整批回滚，不产生记录。
- 表 **`retention_locks`** 保存容器对祖先初始样品的**最低留存份额**
  （`PUT/GET /containers/{id}/retention`）：每行一个
  `(container_id, source_id)` → `min_volume`（Decimal 定点字符串，始终为正，
  设置时不超过该来源当前份额），按 `source_id` 排序返回。这是**普通可变表**
  （非只追加）：可设置、改值或解除（`null`）。仅在**实际变更**（新增 / 改成
  不同值 / 解除一条存在的规则）时把该容器 `version` +1；幂等重写同值、解除
  不存在的规则不递增版本。留存变更**不**递增 `transfer_revision`、不产生
  审计记录。批量提交 / 预演在每一步定点分摊后检查**源容器**的全部留存规则，
  损耗核销在按实际来源扣减后复用同一检查；任一来源剩余份额**严格小于**下限
  即 422 `retention_breach`（等于下限合法）。服务启动时自动为旧库建表，
  初始为空。
- 表 **`components`** 上有两个覆盖索引：
  **`idx_components_source_container(source_id, container_id, amount)`**
  支撑祖先来源反向追踪（按 `source_id` 一次有序范围扫描即可取到仍含该来源的
  容器 ID 与份额，仅索引扫描），配合 `container_id` 键集分页无需 OFFSET；
  **`idx_components_container_source(container_id, source_id, amount)`**
  支撑正向容器查询与反向追踪中的容器体积聚合（按容器 `SUM(amount)` 仅索引扫描）。

---

## API 一览

> 请求 / 响应中的体积字段接受 JSON 数字或字符串，**推荐使用字符串**（如 `"40.50"`），
> 以避免 JSON 数字经 float 解析；响应统一输出为字符串。

### 1. 登记初始样品

```bash
curl -X POST http://localhost:8000/samples \
  -H 'Content-Type: application/json' \
  -d '{"sample_id":"S1","volume":"100"}'
# 201 -> {"sample_id":"S1","volume":"100.000000000"}
```

### 2. 登记空容器

```bash
curl -X POST http://localhost:8000/containers \
  -H 'Content-Type: application/json' \
  -d '{"container_id":"V1"}'
# 201 -> {"container_id":"V1"}
```

重复登记返回 `409 container_exists`。

### 3. 设置 / 修改容器容量上限

```bash
# 设置容量上限为 50
curl -X PUT http://localhost:8000/containers/V1/capacity \
  -H 'Content-Type: application/json' \
  -d '{"capacity":"50"}'
# 200 -> {"container_id":"V1","version":0,"volume":"0",
#        "capacity":"50.000000000","remaining":"50.000000000","utilization":"0.000000",
#        "sources":[],"proportion_scale":6}

# 取消限制：显式传 null
curl -X PUT http://localhost:8000/containers/V1/capacity \
  -H 'Content-Type: application/json' \
  -d '{"capacity":null}'
# 200 -> {"container_id":"V1",...,"capacity":null,"remaining":null,"utilization":null,...}
```

- `capacity` 字段必须显式提供：数值（推荐字符串）设置/修改上限，`null` 表示**不限制**；
- 上限必须**不小于容器当前体积**（等于当前体积合法，此后任何转入都会被拒绝），
  否则返回 `422 capacity_below_volume`；负数、超出 `DECIMAL_SCALE` 精度返回
  `422 invalid_volume`；容器不存在返回 `404 unknown_container`；
- 响应为设置后的完整容器状态（同 `GET /containers/{id}`）；
- 容量修改**不是**批量转移：不递增 `version`、不产生审计记录；
- 未设置容量的容器（含旧库迁移来的容器）一律不限制。

### 4. 祖先来源不相容规则（无向禁配对）

某些初始样品（祖先来源）不允许在同一容器内混合。规则表是一份**全局禁配对集合**，
只能**原子全量替换**（没有逐条增删接口）：

```bash
# 全量替换：S1 与 S2 禁配、S1 与 S3 禁配（每对两端顺序无关）
curl -X PUT http://localhost:8000/compatibility/rules \
  -H 'Content-Type: application/json' \
  -d '{"rules":[
    {"source_a":"S1","source_b":"S2"},
    {"source_a":"S3","source_b":"S1"}
  ]}'
# 200 -> {"rules":[{"source_a":"S1","source_b":"S2"},
#                  {"source_a":"S1","source_b":"S3"}]}

# 查询当前生效的规则（始终为规范化排序后的视图）
curl http://localhost:8000/compatibility/rules
# 200 -> {"rules":[{"source_a":"S1","source_b":"S2"},
#                  {"source_a":"S1","source_b":"S3"}]}

# 清空全部规则：显式传空列表
curl -X PUT http://localhost:8000/compatibility/rules \
  -H 'Content-Type: application/json' \
  -d '{"rules":[]}'
# 200 -> {"rules":[]}
```

- **规范化与去重**：每对两个 ID 顺序无关，`A-B` 与 `B-A` 视为同一条规则；
  存储与返回都按字典序排成 `source_a < source_b`，整表按 `(source_a, source_b)` 排序；
- **自配对**（两端相同）拒绝 `422 self_pair`；
- 引用的 ID 必须**已登记为初始样品**（`POST /samples`）：空容器或未知 ID 拒绝
  `422 unknown_sample`（`details.unknown_samples` 列出全部未登记 ID）；
- **原子替换**：校验（自配对 / 未登记 ID）与“清空 + 写入”在同一个事务内，
  任一失败旧规则完全不变；空列表表示清空全部规则；
- 规则变更**不是**批量转移：不递增 `version`、不产生审计记录；
- 规则只约束**其后的转移**，不回溯既有容器（见下文“不相容校验”）。

#### 不相容校验（批量提交与预演）

规则生效后，`POST /transfers/batch` 与 `POST /transfers/preview` 在顺序模拟的
**每一步分摊后**检查目标容器的**非零祖先集合**：若同时含有某一禁配对的双方，
整批拒绝（预演本来就不落库）：

```bash
# 规则：S1 与 S2 禁配
curl -X PUT http://localhost:8000/compatibility/rules \
  -H 'Content-Type: application/json' \
  -d '{"rules":[{"source_a":"S1","source_b":"S2"}]}'

# 第二条使 V1 同时含有 S1 与 S2：整批拒绝，V1 仍为空
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[
    {"source":"S1","destination":"V1","volume":"5"},
    {"source":"S2","destination":"V1","volume":"5"}
  ]}'
# 422 -> {"error":{"code":"incompatible_sources",
#         "message":"第 2 条转移使目标 'V1' 的非零祖先集合同时含有不相容来源 (S1, S2)，整批拒绝",
#         "details":{"index":2,"destination":"V1",
#                    "conflicts":[{"source_a":"S1","source_b":"S2"}]}}}
```

- `details` 给出**条目序号**（`index`）、**目标容器**（`destination`）与
  **排序后的全部冲突对**（`conflicts`，按 `(source_a, source_b)` 字典序）；
- **隐性祖先参与检查**：同批前序步骤把 S1+S2 混入 V1 后，再 `V1 -> V2`，
  S1 会作为隐性祖先随混合进入 V2，与 V2 中已有的 S3 构成禁配时同样被拒绝
  （即使 S1 从未直接转入 V2）；
- **非零祖先集合**：最大余额法可能给极小份额分出 0 个刻度，0 份额的来源
  不计入检查；
- 目标容器在规则生效前已含有禁配对的，不追溯处罚；但其后任何使其仍含该对的
  转入（以及把该混合液转入其他容器）都会被拒绝；
- 与容量校验一样，失败时容器状态、`version`、幂等记录与审计链均不变化；
  已成功请求的幂等重放直接返回首次结果，不会用新规则重新校验。

### 5. 批量转移（整批原子，按数组顺序依次执行）

```bash
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[
    {"source":"S1","destination":"V1","volume":"40"},
    {"source":"S2","destination":"V1","volume":"60"}
  ]}'
# 200 -> {"applied":2,"message":"已依次提交 2 条转移",
#        "versions":{"S1":1,"S2":1,"V1":1}}
```

转移时从源容器按其**当时**各祖先来源的混合比例取出，累加到目标容器的同名来源上，
源容器对应份额同步扣减。

响应中的 `versions` 是本批**每个被触碰容器提交后的版本**：容器创建时 `version=0`，
每次成功的批量转移后被触碰的容器 `version` 恰好加 1，即使同一容器在本批中多次出现
（如先作为目标、后作为源的链式转移）也只加 1；未参与本批的容器版本不变。

#### 可选：乐观并发保护（expected_versions）

请求体可选携带 `expected_versions`（容器 ID → 客户端预期版本，版本取自上次
`GET /containers/{id}` 或上一批响应中的 `versions`）：

- **覆盖校验**：一旦提供，键集合必须**恰好**覆盖本批全部源和目标容器（去重后不多不少），
  否则返回 `422 invalid_expected_versions`，消息中列明缺少 / 多余的容器；
- **版本校验**：覆盖正确时，服务在**同一个写事务内**读取并比对每个被触碰容器的当前版本，
  任一不符即返回 `409 stale_container`，**整批回滚、绝不落库**（任何容器的体积与版本都不变）；
- 校验通过并成功提交后，返回各容器递增后的新版本（`versions`）；
- **不提供** `expected_versions` 时完全保持原有行为（无版本校验），新旧客户端可混用。

典型用法：先 `GET` 取得各容器版本 → 带版本提交 → 收到 `409 stale_container` 说明期间
被其他客户端改过，重新 `GET` / `preview` 后用新版本重试。

```bash
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[{"source":"S1","destination":"V1","volume":"40"}],
       "expected_versions":{"S1":0,"V1":0}}'
# 200 -> {"applied":1,"message":"已依次提交 1 条转移","versions":{"S1":1,"V1":1}}
```

#### 可选：幂等键（idempotency_key）

请求体可选携带 `idempotency_key`（非空白字符串，最长 255；键两侧空白会被去掉后使用）。
键仅对 `/transfers/batch` 生效，建议由客户端为每个“意图”生成一个稳定的唯一值
（如 UUID），在网络超时、收到 5xx 等不确定结果时用**同一个键、同一个请求体**安全重试：

- **首次成功**：请求体指纹（未经解析的**原始请求体字节**的 SHA-256）、HTTP 状态码与
  **完整响应**与本批转移写入**同一个 SQLite 事务**——幂等记录与扣减要么同时可见，
  要么一起回滚；
- **同键同请求体重放**：直接返回首次响应（响应体逐字节一致），并带响应头
  `Idempotency-Replayed: true`；**不会再次扣减体积、不会再次递增 `version`**，
  也不会重新执行业务校验（例如首次之后 `expected_versions` 已过期，重放仍返回首次结果）；
- **同键但请求体不同**：返回 `409 idempotency_conflict`，整批拒绝且不落库
  （注意指纹以**原始字节**为准：即使语义相同，JSON 空白、键顺序、数字 `40` 与
  字符串 `"40"` 的差异也算不同请求体；请直接重发首次的原始报文）；
- **失败请求不留痕**：任何 4xx（参数错误、源液不足、未知容器、`stale_container` 等）
  都不会写入幂等记录，同一个键可以修正请求后继续重试；
- 预演接口 `/transfers/preview` **不查、不写**幂等记录（请求体里带了键也会被忽略），
  不会“消费”键；
- **不携带** `idempotency_key` 时与旧版本行为完全一致（每批正常执行一次）。

```bash
# 首次提交
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[{"source":"S1","destination":"V1","volume":"40"}],
       "idempotency_key":"b7f2e1a9-4f1c-4d6a-9c23-7a8e0d6b3f11"}'
# 200 -> {"applied":1,"message":"已依次提交 1 条转移","versions":{"S1":1,"V1":1}}

# 同样的键 + 完全相同的请求体重试（如超时后）：重放，不重复扣减、不重复 +1 版本
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[{"source":"S1","destination":"V1","volume":"40"}],
       "idempotency_key":"b7f2e1a9-4f1c-4d6a-9c23-7a8e0d6b3f11"}' \
  -D -
# HTTP/1.1 200
# idempotency-replayed: true
# {"applied":1,"message":"已依次提交 1 条转移","versions":{"S1":1,"V1":1}}

# 同一个键但请求体不同（体积改成 41）：
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[{"source":"S1","destination":"V1","volume":"41"}],
       "idempotency_key":"b7f2e1a9-4f1c-4d6a-9c23-7a8e0d6b3f11"}'
# 409 -> {"error":{"code":"idempotency_conflict",
#         "message":"幂等键 'b7f2e1a9-...' 已用于另一个请求体，整批拒绝且不落库"}}
```

幂等键与 `expected_versions` 可以同时使用：`expected_versions` 只是请求体的一部分，
首次提交时正常校验；重放时直接返回首次响应、不再校验版本。

#### 容量上限校验

容器设置了容量上限后，批量提交在顺序模拟的**每一步**检查
“目标当前体积 + 本步转入量 ≤ 容量上限”（未设置容量的容器不受限）：

```bash
# V1 容量设为 10
curl -X PUT http://localhost:8000/containers/V1/capacity \
  -H 'Content-Type: application/json' -d '{"capacity":"10"}'

# 先转入 8（成功）
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[{"source":"S1","destination":"V1","volume":"8"}]}'

# 再转入 5：8 + 5 = 13 > 10，整批拒绝，V1 仍为 8
curl -X POST http://localhost:8000/transfers/batch \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[{"source":"S1","destination":"V1","volume":"5"}]}'
# 422 -> {"error":{"code":"capacity_exceeded",
#         "message":"第 1 条转移目标容量超限：'V1' 当前 8.000000000，转入 5.000000000 后预计 13.000000000，超过容量上限 10.000000000，预计超出 3.000000000，整批拒绝",
#         "details":{"index":1,"destination":"V1","current_volume":"8.000000000",
#                    "incoming":"5.000000000","projected_volume":"13.000000000",
#                    "capacity":"10.000000000","excess":"3.000000000"}}}
```

同一批内“先转出再转入”按**当时余量**判断：例如 V1（容量 10、当前 8）在同一批中
先转出 5 再转入 6，转入时余量按 10 − 3 = 7 计算，合法；恰好达到上限（转入后
体积 == 容量）也合法。容量校验失败与成功请求的幂等重放互不影响：已成功的请求
重放时直接返回首次结果，不会用新的容量上限重新校验。

**整批拒绝**的情形（HTTP 状态码与错误码）：

| 情形 | 状态码 | `error.code` |
| --- | --- | --- |
| 源 / 目标容器不存在 | 404 | `unknown_container` |
| `source` 与 `destination` 相同 | 422 | `self_transfer` |
| 转移体积大于源容器当前体积 | 422 | `insufficient_volume` |
| 目标当前体积 + 转入量超过其容量上限 | 422 | `capacity_exceeded` |
| 目标分摊后的非零祖先集合命中不相容禁配对 | 422 | `incompatible_sources` |
| 源容器分摊后某来源份额低于其最低留存下限 | 422 | `retention_breach` |
| 体积为 0、为负、非有限数，或小数位超过 `DECIMAL_SCALE` | 422 | `invalid_volume` |
| `expected_versions` 未恰好覆盖本批全部源/目标（含空对象） | 422 | `invalid_expected_versions` |
| `expected_versions` 中某容器版本与当前版本不符 | 409 | `stale_container` |
| 同一 `idempotency_key` 已用于另一个请求体（指纹不同） | 409 | `idempotency_conflict` |

错误响应形如：

```json
{"error":{"code":"insufficient_volume","message":"第 2 条转移源液不足：'V1' 当前 75.000000000，请求 9999.000000000，整批拒绝"}}
```

容量超限（`capacity_exceeded`）时，错误响应额外携带结构化 `details`，给出
**条目序号、目标容器与预计超出量**（体积为定点字符串），便于客户端直接定位与展示：

```json
{
  "error": {
    "code": "capacity_exceeded",
    "message": "第 2 条转移目标容量超限：'V2' 当前 0，转入 20.000000000 后预计 20.000000000，超过容量上限 10.000000000，预计超出 10.000000000，整批拒绝",
    "details": {
      "index": 2,
      "destination": "V2",
      "current_volume": "0",
      "incoming": "20.000000000",
      "projected_volume": "20.000000000",
      "capacity": "10.000000000",
      "excess": "10.000000000"
    }
  }
}
```

容量校验在顺序模拟的每一步进行：同批前序转移改变目标余量后，后续步骤按**当时**
余量判断（先转出腾出空间再转入是合法的）；任一目标超限即整批回滚——容器状态、
`version`、幂等记录与审计链均不变化。设置容量上限本身也可能失败：
上限小于当前体积返回 `422 capacity_below_volume`（`details` 含
`container_id` / `capacity` / `current_volume`）。

不相容校验（`incompatible_sources`）同样在每一步**分摊后**进行，`details`
给出条目序号、目标容器与排序后的全部冲突对（详见上文“祖先来源不相容规则”）：

```json
{
  "error": {
    "code": "incompatible_sources",
    "message": "第 2 条转移使目标 'V1' 的非零祖先集合同时含有不相容来源 (S1, S2)，整批拒绝",
    "details": {
      "index": 2,
      "destination": "V1",
      "conflicts": [{"source_a": "S1", "source_b": "S2"}]
    }
  }
}
```

审计查询接口（`GET /audit/records`）的参数错误使用同一错误响应结构：
未知容器 `404 unknown_container`、序号范围非法（`<1` 或 `end<start`）
`422 invalid_seq_range`、`limit` 越界 `422 invalid_limit`；
`GET /audit/verify` 不返回错误码——链是否完好始终是 200 响应中的 `valid` 字段。

### 6. 预演批量转移（不落库）

`POST /transfers/preview` 使用与批量提交**完全相同的请求结构**（同样接受可选的
`expected_versions`；`idempotency_key` 字段虽可出现但会被**忽略**——预演不查重、
不重放、不写幂等记录），在数据库当前状态上按数组顺序整批模拟（链式转移使用本批前序
模拟结果），返回：

- `steps`：每一步的**条目序号**与从各祖先来源**实际扣取的精确份额**（`taken` 之和
  精确等于该步请求体积，同样采用最大余额法）；
- `containers`：全部受影响容器（本批所有源 / 目标容器）在预演结束时的**最终体积、
  来源构成与 `version`**，结构与 `GET /containers/{id}` 一致。其中 `version` 是
  **若按本批真实提交后将变成的版本**（当前版本 + 1，每个被触碰容器只加 1 次）。

预演与真实提交**共用同一套校验、版本校验与分摊代码**：提供 `expected_versions` 时，
覆盖错误同样返回 `422 invalid_expected_versions`、版本不符同样返回
`409 stale_container`，目标容量超限同样返回 `422 capacity_exceeded`（含 `details`），
命中不相容禁配对同样返回 `422 incompatible_sources`（含 `details`），
源容器分摊后跌破最低留存下限同样返回 `422 retention_breach`（含 `details`），
HTTP 状态码、`error.code` 以及消息中的条目序号（“第 N 条转移”）
均与 `/transfers/batch` 完全一致。预演**只读不写、不递增版本**，成功或失败都不会改变
数据库，可以安全地重复调用；因此可用于“先核对预期版本、确认提交后版本，再正式提交”。

```bash
curl -X POST http://localhost:8000/transfers/preview \
  -H 'Content-Type: application/json' \
  -d '{"transfers":[
    {"source":"S1","destination":"V1","volume":"40"},
    {"source":"S2","destination":"V1","volume":"60"},
    {"source":"V1","destination":"V2","volume":"25"}
  ]}'
```

```json
{
  "steps": [
    {"index": 1, "source": "S1", "destination": "V1", "volume": "40.000000000",
     "taken": [{"source_id": "S1", "amount": "40.000000000"}]},
    {"index": 2, "source": "S2", "destination": "V1", "volume": "60.000000000",
     "taken": [{"source_id": "S2", "amount": "60.000000000"}]},
    {"index": 3, "source": "V1", "destination": "V2", "volume": "25.000000000",
     "taken": [
       {"source_id": "S1", "amount": "10.000000000"},
       {"source_id": "S2", "amount": "15.000000000"}
     ]}
  ],
  "containers": [
    {"container_id": "S1", "version": 1, "volume": "60.000000000",
     "capacity": null, "remaining": null, "utilization": null,
     "sources": [{"source_id": "S1", "amount": "60.000000000", "proportion": "1.000000"}],
     "proportion_scale": 6},
    {"container_id": "S2", "version": 1, "volume": "40.000000000",
     "capacity": null, "remaining": null, "utilization": null,
     "sources": [{"source_id": "S2", "amount": "40.000000000", "proportion": "1.000000"}],
     "proportion_scale": 6},
    {"container_id": "V1", "version": 1, "volume": "75.000000000",
     "capacity": null, "remaining": null, "utilization": null,
     "sources": [
       {"source_id": "S1", "amount": "30.000000000", "proportion": "0.400000"},
       {"source_id": "S2", "amount": "45.000000000", "proportion": "0.600000"}
     ],
     "proportion_scale": 6},
    {"container_id": "V2", "version": 1, "volume": "25.000000000",
     "capacity": null, "remaining": null, "utilization": null,
     "sources": [
       {"source_id": "S1", "amount": "10.000000000", "proportion": "0.400000"},
       {"source_id": "S2", "amount": "15.000000000", "proportion": "0.600000"}
     ],
     "proportion_scale": 6}
  ]
}
```

上例中第 3 步从混合容器 `V1` 取 25：预演按前两步形成的 40/60 混合比分摊为
S1 取 10、S2 取 15；调用后数据库中 S1/S2 仍为 100、V1/V2 仍为空，版本也都保持 0；
各容器返回的 `"version": 1` 是**预测**真实提交后的版本（当前 0 + 1）。

### 7. 查询容器当前体积、容量与来源占比

```bash
curl http://localhost:8000/containers/V1
```

```json
{
  "container_id": "V1",
  "version": 2,
  "volume": "75.000000000",
  "capacity": "100.000000000",
  "remaining": "25.000000000",
  "utilization": "0.750000",
  "sources": [
    {"source_id": "S1", "amount": "30.000000000", "proportion": "0.400000"},
    {"source_id": "S2", "amount": "45.000000000", "proportion": "0.600000"}
  ],
  "proportion_scale": 6
}
```

- `version`：容器的乐观并发版本，创建时为 `0`，每次被成功批次触碰后加 1
  （同批多次出现只加 1）；可作为 `/transfers/batch`、`/transfers/preview` 的
  `expected_versions` 入参。
- `capacity`：容量上限；`null` 表示不限制（默认）。
- `remaining`：剩余可转入空间（容量 − 当前体积）；不限容量时为 `null`。
- `utilization`：利用率（当前体积 / 容量），按 `PROPORTION_SCALE` 四舍五入；
  不限容量或容量为 `0` 时为 `null`。
- `amount`：该祖先来源在容器中的**绝对体积**（满足体积守恒，可跨容器累加核对）。
- `proportion`：占容器当前体积的比例，按 `PROPORTION_SCALE` 四舍五入，仅用于展示。
- 空容器返回 `"version":0`、`"volume":"0"`、`"sources":[]`；未知容器返回 `404 unknown_container`。

### 8. 祖先来源反向追踪（仍含某初始样品的全部容器）

`GET /sources/{sample_id}/containers` 是容器来源占比查询的**反向**版本：给定一个
**初始样品**，按当前 `components` 表找出所有**仍含该样品非零份额**的容器（样品自身
剩余、直接转入的容器、以及经多代混合传播的下游容器都会列出），按 `container_id`
**字典序**稳定分页返回每个容器的当前体积、该来源的**绝对份额**与**占比**。

```bash
# 接前文：S1 100 / S2 100 -> V1(40/60) -> V2 取 25（S1=10, S2=15）
curl 'http://localhost:8000/sources/S1/containers'
```

```json
{
  "sample_id": "S1",
  "transfer_revision": 1,
  "min_amount": "0.000000000",
  "limit": 100,
  "count": 3,
  "next_cursor": null,
  "containers": [
    {"container_id": "S1", "volume": "60.000000000",
     "source_amount": "60.000000000", "proportion": "1.000000"},
    {"container_id": "V1", "volume": "75.000000000",
     "source_amount": "30.000000000", "proportion": "0.400000"},
    {"container_id": "V2", "volume": "25.000000000",
     "source_amount": "10.000000000", "proportion": "0.400000"}
  ]
}
```

- `volume`：容器**当前总体积**（全部祖先份额之和）；
- `source_amount`：该初始样品在容器中的**绝对体积份额**（精确，可跨容器累加核对
  守恒：全部持有容器的该字段之和恒等于初始样品体积）；
- `proportion`：`source_amount / volume`，按 `PROPORTION_SCALE` 四舍五入的展示值；
- 样品已**完全转空**时其自身不再出现，但持有其份额的下游容器照常返回；
- `transfer_revision`：查询时刻的全局转移修订号（见“数据模型”）。

查询参数：

| 参数 | 说明 |
| --- | --- |
| `min_amount` | 可选（数字或字符串，推荐字符串）。只保留 `source_amount >= min_amount` 的容器；非负、小数位受 `DECIMAL_SCALE` 限制，非法返回 `422 invalid_min_amount`；响应回显规范化后的值 |
| `limit` | 可选，页大小，`1..100`，默认 `100`；越界返回 `422 invalid_limit` |
| `cursor` | 可选，上一页响应中的 `next_cursor`（不透明字符串）；翻页时不得更换 `min_amount` |

#### 分页与跨页一致性（cursor / transfer_revision）

响应中的 `next_cursor` 为不透明游标（base64 编码的 JSON），携带：游标结构版本、
**全局 `transfer_revision`**、所属样品 ID、本页最后一个 `container_id`（字典序
键集分页位置）与 `min_amount`。末页 `next_cursor` 为 `null`。

```bash
curl 'http://localhost:8000/sources/S1/containers?limit=2'
# {"count":2,"transfer_revision":1,
#  "next_cursor":"eyJ2IjoxLCJyZXYiOjEsInNhbXBsZSI6IlMxIiwiYWZ0ZXIiOiJWMSIsIm1pbl9hbW91bnQiOiIwLjAwMDAwMDAwMCJ9",
#  "containers":[{"container_id":"S1",...},{"container_id":"V1",...}]}

curl 'http://localhost:8000/sources/S1/containers?limit=2&cursor=eyJ2Ij...'
# {"count":1,"next_cursor":null,"containers":[{"container_id":"V2",...}]}
```

修订号在**成功批次**提交与**成功损耗核销**时原子 +1，因此它恰好刻画
“组件表内容的版本”：

- 翻页期间有成功批次提交或成功核销，再用旧游标请求 -> **`409 stale_cursor`**
  （`details` 含 `cursor_revision` 与 `current_revision`），客户端应丢弃旧游标，
  从第一页重新开始；
- 翻页期间只有**预演、失败批次、失败核销或幂等重放** -> 修订号不变，游标继续有效；
- 游标损坏 / 无法解码 / 版本不受支持 -> **`400 invalid_cursor`**；
- 游标属于**另一个样品**、或翻页时改变了 **`min_amount`** ->
  **`400 cursor_parameter_mismatch`**；
- 样品 ID 未登记为初始样品（未知 ID，或该 ID 只是个空容器）->
  **`404 unknown_sample`**。

```json
{
  "error": {
    "code": "stale_cursor",
    "message": "cursor 基于 transfer_revision=1，当前修订号已变为 2（翻页期间有成功批次提交），请丢弃旧游标后重新查询",
    "details": {"cursor_revision": 1, "current_revision": 2}
  }
}
```

查询在服务端通过**只读 SQLite 连接**（`mode=ro` + `query_only` 双重保险）与显式
只读事务执行：整次请求读取同一一致性快照，且物理上无法写入；所有体积 / 份额 /
比例字段均为 `Decimal` 计算并以**定点字符串**序列化，不经过 `float`。

---

### 9. 样品损耗核销（只追加损耗记录）

容器在存储 / 转移过程中可能发生蒸发、挂壁等损耗。核销接口把容器体积一次性
**核销到实测值**，差额（总损耗 = 核销前体积 − 实测体积）按**核销前各祖先份额
比例**从各来源扣减：

```bash
# 接前文：V1 当前 100（S1=40 / S2=60），version=1；实测只剩 75
curl -X POST http://localhost:8000/containers/V1/losses \
  -H 'Content-Type: application/json' \
  -d '{"measured_volume":"75","expected_version":1,"note":"蒸发"}'
# 200 -> {"loss_id":1,"container_id":"V1","version":2,
#        "volume_before":"100.000000000","measured_volume":"75.000000000",
#        "loss_volume":"25.000000000",
#        "deductions":[{"source_id":"S1","amount":"10.000000000"},
#                      {"source_id":"S2","amount":"15.000000000"}],
#        "note":"蒸发"}

# 查询该容器的全部损耗记录（按新到旧）
curl http://localhost:8000/containers/V1/losses
# 200 -> {"container_id":"V1","count":1,"losses":[
#   {"loss_id":1,"measured_volume":"75.000000000","loss_volume":"25.000000000",
#    "deductions":[{"source_id":"S1","amount":"10.000000000"},
#                  {"source_id":"S2","amount":"15.000000000"}],
#    "note":"蒸发","created":"2026-09-26T12:00:00.000Z"}]}
```

请求体字段：

| 字段 | 说明 |
| --- | --- |
| `measured_volume` | 必填。核销后实测体积：必须满足 `0 ≤ 实测值 ≤ 容器当前体积`，小数位受 `DECIMAL_SCALE` 限制（推荐字符串） |
| `expected_version` | 必填。客户端预期的容器 `version`（取自 `GET /containers/{id}`），乐观并发保护 |
| `note` | 可选。备注（最长 1000 字符），未提供时记录为 `null` |

行为约定：

- **按比例精确扣减**：总损耗按核销前各祖先份额比例，用与批量转移**相同的定点
  精度与最大余额法**分摊到各来源——各来源扣减之和**严格等于**总损耗，每份满足
  `0 ≤ 扣减 ≤ 核销前份额`，不产生负数；上例 25 按 40/60 分摊为 S1=10、S2=15；
- **单事务**：组件扣减、容器 `version` +1、全局 `transfer_revision` +1 与损耗
  记录写入在同一个 SQLite 事务内完成；`transfer_revision` 递增会使既有的来源
  反查分页游标失效（翻页返回 `409 stale_cursor`）；
- **零损耗也写记录**：`measured_volume` 等于当前体积时总损耗为 0
  （`deductions` 为空），同样递增 `version` 与 `transfer_revision` 并追加记录；
  `measured_volume` 为 0 时整容器核销为空（构成清空，容器保留）；
- **只追加记录**：成功核销写入 `loss_records` + `loss_record_sources`，两表由
  数据库触发器禁止 UPDATE/DELETE；`GET /containers/{id}/losses` 按 `loss_id`
  倒序（新到旧）返回，容器不存在返回 `404 unknown_container`，无记录返回空列表；
- **失败不留痕**：任何失败整体回滚——组件、`version`、`transfer_revision` 与
  损耗记录均不变。核销**不是**批量转移：不产生审计记录、不支持幂等键。

**核销失败**的情形（HTTP 状态码与错误码）：

| 情形 | 状态码 | `error.code` |
| --- | --- | --- |
| 容器不存在 | 404 | `unknown_container` |
| `expected_version` 与当前版本不符 | 409 | `stale_container` |
| 实测体积大于当前体积 | 422 | `measured_exceeds_volume` |
| 按实际来源扣减后某来源跌破其最低留存下限 | 422 | `retention_breach` |
| 实测体积为负、非有限数，或小数位超过 `DECIMAL_SCALE` | 422 | `invalid_volume` |

`measured_exceeds_volume` 的 `details` 含 `container_id` / `measured_volume` /
`current_volume`（定点字符串），便于客户端直接定位与展示。

核销还会执行与批量转移相同的**最低留存份额**约束（见下一节）：按实际来源
扣减后任一被锁定来源的剩余份额低于下限，返回 `422 retention_breach`。

---

## 最低留存份额锁定（容器 × 祖先初始样品）

实验室需要保证某个**祖先初始样品**在容器中始终留下不少于一定体积的份额。
留存规则是 `(容器, 来源) → 最低留存体积` 的可变映射，用
`PUT /containers/{id}/retention` 按来源逐条设置 / 修改 / 解除：

```bash
# 接前文：V1 当前 100（S1=40 / S2=60），version=1；锁定 S1 至少留存 30
curl -X PUT http://localhost:8000/containers/V1/retention \
  -H 'Content-Type: application/json' \
  -d '{"source_id":"S1","min_volume":"30","expected_version":1}'
# 200 -> {"container_id":"V1","version":2,"retention":[
#   {"source_id":"S1","min_volume":"30.000000000","current_share":"40.000000000"}]}

# 查询容器当前的全部留存规则（按 source_id 排序，含各来源当前份额）
curl http://localhost:8000/containers/V1/retention
# 200 -> {"container_id":"V1","version":2,"retention":[...]}

# 解除：min_volume 显式传 null（V1 当前 version=2）
curl -X PUT http://localhost:8000/containers/V1/retention \
  -H 'Content-Type: application/json' \
  -d '{"source_id":"S1","min_volume":null,"expected_version":2}'
# 200 -> {"container_id":"V1","version":3,"retention":[]}
```

请求体字段：

| 字段 | 说明 |
| --- | --- |
| `source_id` | 必填。祖先初始样品 ID，必须已由 `POST /samples` 登记（未知 ID / 空容器返回 `422 unknown_sample`） |
| `min_volume` | 必填。正数（小数位受 `DECIMAL_SCALE` 限制）设置 / 修改下限，且**不得超过该来源当前在容器中的份额**；显式 `null` 表示**解除**该来源的规则。`0` 不是合法下限（请用 `null` 解除），返回 `422 invalid_volume` |
| `expected_version` | 必填。客户端预期的容器 `version`（取自 `GET /containers/{id}`）；与当前版本不符返回 `409 stale_container`，规则与版本均不变 |

行为约定：

- **实际变更才递增版本**：新增规则、把规则改成**不同的值**、解除一条**存在**
  的规则都会使容器 `version` +1；幂等重写同一个值（如 `"30"` 与 `"30.0"`）、
  解除一条本就不存在的规则**不是**实际变更，版本保持不变。响应中的 `version`
  始终是操作完成后的（即新版本或当前版本），因此可以直接作为下一次操作的
  `expected_version`；
- **不触碰转移修订号 / 审计**：留存规则变更**不**递增全局
  `transfer_revision`（来源反查翻页游标不会因此失效），也不产生审计记录；
- **回显当前份额**：响应与 `GET` 中每条规则都带 `current_share`（该来源当前在
  容器中的绝对份额，容器中已无该来源份额时为 `"0.000000000"`）；从未设置过
  规则的容器返回空列表，容器不存在返回 `404 unknown_container`；
- 下限设置成功后，该来源在容器中的份额一定 `≥ min_volume > 0`（设置时刻已
  校验，后续取走 / 核销又被下方约束拦住），因此规则不会“悬空”在 0 份额上。

设置规则失败的情形：

| 情形 | 状态码 | `error.code` |
| --- | --- | --- |
| 容器不存在 | 404 | `unknown_container` |
| 来源未登记为初始样品（未知 ID 或空容器） | 422 | `unknown_sample` |
| `min_volume` 为 0、为负、非有限数，或小数位超过 `DECIMAL_SCALE` | 422 | `invalid_volume` |
| 正值超过该来源当前份额（含容器当前不含该来源） | 422 | `retention_exceeds_share` |
| `expected_version` 与当前版本不符 | 409 | `stale_container` |

`retention_exceeds_share` 的 `details` 含 `container_id` / `source_id` /
`min_volume` / `current_share`。任何失败都整体回滚：规则、容器 `version` 与
`transfer_revision` 均不变。

### 留存约束（批量转移 / 预演 / 损耗核销）

规则生效后，取走份额的两条路径都执行**同一条**检查——
`POST /transfers/batch` 与 `POST /transfers/preview` 在顺序模拟的**每一步按既有
定点分摊之后**，检查**源容器**的全部留存规则：

```bash
# V1(S1=40/S2=60) 锁 S1 留存 30；转出 26：S1 按 40% 被扣 10.4，剩 29.6 < 30
curl -X POST http://localhost:8000/transfers/batch -H 'Content-Type: application/json' \
  -d '{"transfers":[{"source":"V1","destination":"V2","volume":"26"}]}'
# 422 -> {"error":{"code":"retention_breach",
#   "message":"第 1 条转移使源容器 'V1' 中来源 'S1' 的留存份额 29.600000000 低于最低留存下限 30.000000000，整批拒绝",
#   "details":{"index":1,"source":"V1","container_id":"V1","source_id":"S1",
#              "remaining":"29.600000000","min_volume":"30.000000000"}}}
```

- **分摊后检查、严格小于才算违规**：用该步**最大余额法实际扣取**后的源容器
  份额与下限比较；剩余份额**恰好等于**下限合法，**严格小于**下限才拒绝。
  `details` 给出条目序号 `index`、源容器 `source` / `container_id`、来源
  `source_id`、分摊后剩余 `remaining` 与下限 `min_volume`（定点字符串）；
- **逐步检查、后续转回也不救**：任一步先跌破即整批拒绝，即使同批后续转移会
  把该来源份额转回源容器也一样（消息中的 `index` 是第一条跌破的步骤）；
- **只约束取走、不约束转入**：向容器转入只会增加份额，目标容器的留存规则不
  参与检查；隐性祖先随混合传播后，从混合容器取走时同样按其留存规则检查；
- **损耗核销同约束**：`POST /containers/{id}/losses` 在按实际来源扣减后检查
  同一容器的全部留存规则，违反即 `422 retention_breach`（`details` 含
  `container_id` / `source_id` / `remaining` / `min_volume`，无 `index`），
  整体回滚；零损耗核销不改变份额，天然通过；
- **失败不留痕**：留存约束拒绝与容量 / 不相容 / 源液不足等失败完全一致——
  份额、容器 `version`、`transfer_revision`、幂等记录、审计链与损耗记录都
  不变；预演只读不写。**已成功请求的幂等重放直接返回首次结果**，不会用新设置
  的留存规则重新校验（与容量上限、不相容规则的重放语义一致）；
- 预演与真实提交共用同一套检查，错误码、HTTP 状态码与 `details` 完全一致，
  可先预演确认“不会跌破留存下限”再正式提交。

---

## 审计链（只追加 + 哈希链）

每个**成功提交**的批次（无论是否携带 `idempotency_key` / `expected_versions`）都会在
容器扣减、`version` 递增、幂等行所在的**同一个 SQLite 事务**内追加一条审计记录，
因此审计记录与业务状态要么同时可见、要么一起回滚：

- **失败不留痕**：任何 4xx（参数错误、源液不足、未知容器、自转移、`stale_container`、
  `idempotency_conflict` 等）整批回滚，审计行一起回滚；
- **预演不留痕**：`/transfers/preview` 只读，成功失败都不写审计；
- **幂等重放不留痕**：同键同请求体重放直接返回首次响应，不执行转移也不追加审计记录。

### 记录内容

每条记录（`audit_records` 一行 + `audit_record_containers` 每个受影响容器一行）含：

| 字段 | 说明 |
| --- | --- |
| `seq` | 链上序号，从 `1` 开始连续递增、无空洞（由写事务内 `max(seq)+1` 分配） |
| `prev_hash` | 前一条记录的 `entry_hash`；首条为 **`sha256("")`**（空字符串的 SHA-256，即 `e3b0c4…b855`） |
| `entry_hash` | `SHA-256(prev_hash + "\n" + seq + "\n" + 规范化载荷)` 的小写 hex |
| `created` | 追加时间（UTC，`strftime('%Y-%m-%dT%H:%M:%fZ','now')`） |
| `summary` | 批次摘要：`transfer_count`、`total_volume`（全部转移体积之和）、`containers`（受影响容器，去重排序）、`idempotency_key`（未携带为 `null`） |
| `transfers` | 逐步明细：`index` / `source` / `destination` / `volume` 与 `taken`（该步按最大余额法从各祖先来源实际扣取的精确份额） |
| `containers` | 每个受影响容器的 `version_before` / `version_after`（恰好 +1）、`volume_before` / `volume_after`、`sources_after`（提交后各祖先来源绝对份额） |
| `result_versions` | 提交后各受影响容器版本，与批量响应 `versions` 一致 |

“规范化载荷”指 `json.dumps(payload, sort_keys=True, separators=(",",":"), ensure_ascii=False)`
的输出；库中 `payload_json` 与哈希输入逐字节相同。键排序保证同一语义只有一种字节表示，
因此任何对载荷的改动（包括重新格式化）都会导致哈希校验失败。

**只追加**由数据库层强制：`audit_records` 与 `audit_record_containers` 各有一对
`BEFORE UPDATE` / `BEFORE DELETE` 触发器，任何 UPDATE/DELETE 都会得到
`sqlite3.DatabaseError: ... 只追加 ...`（在触发器内 `RAISE(FAIL, ...)`）。应用只做
INSERT，不提供修改/删除审计记录的 API。

> 安全模型说明：触发器防止通过本应用 / 普通数据库连接的意外修改，并能让
> `/audit/verify` 检出**外部直接改库文件**（绕过触发器）的篡改。但拥有库文件写权限、
> 又能 DROP TRIGGER 的攻击者仍可重写整条链——哈希链提供的是**篡改可检测性**，不是
> 防写保护；需要更强保证时可定期把 `GET /audit/verify` 与最后一条 `entry_hash`
> 存放到带外（如离线 WORM / 另一台主机）。

### 10. 查询审计记录：按容器与序号范围

```bash
# 全部记录（按 seq 升序，默认 limit=100，最大 1000）
curl 'http://localhost:8000/audit/records'

# 仅查容器 V1 作为源 / 目标出现过的批次（JOIN audit_record_containers）
curl 'http://localhost:8000/audit/records?container_id=V1'

# 序号闭区间（含端点），可与 container_id、limit 任意组合
curl 'http://localhost:8000/audit/records?start_seq=2&end_seq=10&limit=20'
```

查询参数：

| 参数 | 说明 |
| --- | --- |
| `container_id` | 可选。只返回该容器作为源 / 目标出现过的批次；容器不存在返回 `404 unknown_container`；容器存在但从未参与批次返回空列表 |
| `start_seq` / `end_seq` | 可选，闭区间，必须 ≥ 1 且 `end_seq ≥ start_seq`，否则 `422 invalid_seq_range` |
| `limit` | 可选，默认 `100`、范围 `1..1000`，越界返回 `422 invalid_limit`；超出部分用 `start_seq=上页末序号+1` 翻页 |

响应：

```json
{
  "count": 1,
  "limit": 100,
  "filters": {"container_id": null, "start_seq": null, "end_seq": null},
  "records": [
    {
      "seq": 1,
      "prev_hash": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
      "entry_hash": "6b292c…",
      "created": "2026-09-21T12:00:00.000Z",
      "summary": {
        "transfer_count": 2,
        "total_volume": "100.000000000",
        "containers": ["S1", "S2", "V1"],
        "idempotency_key": null
      },
      "transfers": [
        {"index": 1, "source": "S1", "destination": "V1", "volume": "40.000000000",
         "taken": [{"source_id": "S1", "amount": "40.000000000"}]},
        {"index": 2, "source": "S2", "destination": "V1", "volume": "60.000000000",
         "taken": [{"source_id": "S2", "amount": "60.000000000"}]}
      ],
      "containers": [
        {"container_id": "S1", "version_before": 0, "version_after": 1,
         "volume_before": "100.000000000", "volume_after": "60.000000000",
         "sources_after": [{"source_id": "S1", "amount": "60.000000000"}]},
        {"container_id": "S2", "version_before": 0, "version_after": 1,
         "volume_before": "100.000000000", "volume_after": "40.000000000",
         "sources_after": [{"source_id": "S2", "amount": "40.000000000"}]},
        {"container_id": "V1", "version_before": 0, "version_after": 1,
         "volume_before": "0", "volume_after": "100.000000000",
         "sources_after": [
           {"source_id": "S1", "amount": "40.000000000"},
           {"source_id": "S2", "amount": "60.000000000"}]}
      ],
      "result_versions": {"S1": 1, "S2": 1, "V1": 1}
    }
  ]
}
```

### 11. 校验整条审计链

```bash
curl http://localhost:8000/audit/verify
```

无论链是否完好都返回 **200**（校验是正常查询，链损坏不是服务错误），结果在
`valid` 字段：

```json
{
  "valid": true,
  "records": 3,
  "first_seq": 1,
  "max_seq": 3,
  "break": null,
  "checks": {
    "sequence":          {"ok": true, "detail": null},
    "hash_chain":        {"ok": true, "detail": null},
    "payload_integrity": {"ok": true, "detail": null},
    "container_index":   {"ok": true, "detail": null}
  }
}
```

四项检查：

1. **`sequence`**：所有行序号必须恰好是 `1..N`（从 1 开始、连续、无重复）——
   能发现中间删除 / 插入记录；
2. **`hash_chain`**：每行 `prev_hash` 必须等于前一行 `entry_hash`
   （首行等于 `sha256("")`），且用存储载荷重算的哈希必须等于 `entry_hash`——
   能发现载荷篡改、哈希篡改、记录替换；
3. **`payload_integrity`**：存储载荷重新规范化后必须与 `payload_json` 逐字节一致，
   且载荷内 `seq` 与行序号一致——能发现被改写但未重算哈希的载荷；
4. **`container_index`**：载荷中的受影响容器必须与反查索引表
   `audit_record_containers`（按 `position` 排序）完全一致。

任一项失败时 `valid=false`，`break` 按
`sequence → hash_chain → payload_integrity → container_index` 的优先级给出
第一处断裂：`{"seq": <序号>, "reason": <原因码>, "detail": <说明>}`。
即使攻击者篡改载荷后**重算**了该行自身的 `entry_hash`，下一行的 `prev_hash`
链接仍会断裂并被检出（除非整条链从断点起全部重写）。空链（没有任何成功批次）
视为有效：`records=0, valid=true`。

---

## 精确性说明

转移量在各祖先来源之间的分摊采用**最大余额法**（Hare quota + Largest remainder）：
每份先向下取整到 `10^-DECIMAL_SCALE`，再把剩余的整数个刻度按被舍掉的余额从大到小补回。
由此保证：

- 各来源取出额之和**精确等于**请求转移体积；
- 每一份额满足 `0 ≤ 取出额 ≤ 源容器中该来源的份额`，因此不会出现“取超了”或负值；
- 与真实比例的偏差不超过 1 个体积刻度；
- 整个系统体积守恒：所有容器的 `amount` 总和始终等于初始样品体积总和。

样品损耗核销（`POST /containers/{id}/losses`）对总损耗在各来源之间的分摊复用
**同一个**最大余额法实现：各来源扣减之和精确等于总损耗、不产生负数；核销后
全系统体积总和相应减少（减少量即累计损耗，明细见 `loss_record_sources`）。

注意 `proportion` 是四舍五入后的展示值，各比例之和可能不严格等于 1；需要精确核对时请使用
`amount` 字段。

---

## 本地开发（不使用 Docker）

要求 Python 3.11+：

```bash
pip install -r requirements.txt

# 启动（默认在 ./lineage.db 建库；可用环境变量覆盖）
DATABASE_PATH=./lineage.db uvicorn app.main:app --reload --port 8000

# 运行测试
pip install -r requirements-dev.txt
pytest -q
```

## 项目结构

```
app/
  config.py     # 环境变量配置
  db.py         # SQLite 连接（含只读连接）、建库（启动时执行，含审计表、规则表、
                #   transfer_revision、损耗记录表、留存份额表、覆盖索引与只追加触发器迁移）
  schemas.py    # Pydantic 请求/响应模型（Decimal <-> 字符串）
  service.py    # 核心逻辑：登记、批量转移事务、比例分摊、容量/不相容/最低留存校验、
                #   查询、祖先来源反向追踪与游标分页、样品损耗核销事务、留存规则事务
  audit.py      # 只追加审计链：载荷规范化、哈希、同事务追加、查询、链校验
  loss.py       # 只追加损耗核销记录：建表、触发器、同事务追加、按容器查询（新到旧）
  retention.py  # 最低留存份额：建表、规则读写与响应组装（校验/版本/事务在 service.py）
  main.py       # FastAPI 路由与错误处理（含 /compatibility/rules、/sources/*、
                #   /audit/*、/containers/{id}/losses、/containers/{id}/retention）
tests/          # pytest 端到端、分摊算法、幂等、容量、不相容规则、反向追踪、
                #   审计链、损耗核销与最低留存份额测试
Dockerfile
docker-compose.yml
docker-entrypoint.sh
```
