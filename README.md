# 患者数据共享同意与访问网关（Python / FastAPI）

一套在本地 API 服务之上实现的“患者数据共享同意与访问网关”。用本地内存存储模拟
患者、数据接收方、同意授权与审计记录，目标是让**不同同意状态、访问范围与并发操作
下的授权结论一致且可解释**。

除授予、撤销、访问、审计外，本版新增三项能力：

- **范围收窄**：在患者 / 接收方 / 用途都不变的前提下，把一条同意可访问的字段类别
  改小；只减不增，立即生效，每次变更前后范围都可追溯。
- **暂停 / 恢复**：把一条同意临时暂停（区别于撤销与过期），恢复后同一条同意按原
  有效期与原范围继续生效；暂停与恢复都可追溯。
- **全写操作失败关闭**：授予、撤销、收窄、暂停、恢复与数据访问一样，追溯记录写不
  进去时整体失败，且不留下半生效状态。

## 1. 同意模型

一条同意授权（`Consent`）由以下要素唯一刻画：

| 要素 | 说明 |
| --- | --- |
| `patient_id` | 数据主体（患者） |
| `recipient_id` | 数据接收方（医院 / 研究机构 / 保险公司等） |
| `purpose` | 共享用途：`treatment` / `research` / `payment` / `marketing` |
| `allowed_fields` | 授权的**字段类别**范围（最小必要粒度，见下表） |
| `granted_at` / `expires_at` | 授予时间与可选的过期时间 |
| `active` / `revoked_at` | 撤销标记与撤销时间，撤销立即生效 |
| `suspended` / `suspended_at` / `resumed_at` | 暂停标记与暂停/恢复时间；暂停时 `active` 仍为 `true`，但访问一律拒绝 |

字段类别（`FieldCategory`）与患者字段的映射：

| 类别 | 敏感性 | 包含字段 |
| --- | --- | --- |
| `identity` | 敏感 | `name`, `id_card` |
| `contact` | 敏感 | `phone`, `email` |
| `profile` | 非敏感 | `gender`, `birth_date` |
| `medical` | 非敏感 | `diagnosis`, `medications` |

同一 `(patient, recipient, purpose)` 三元组允许存在多条历史同意，决策时取
**最近授予的一条**；已撤销 / 已过期的历史同意不会“复活”授权。

## 2. 范围收窄与暂停 / 恢复语义

### 2.1 范围收窄（`POST /consents/narrow`）

- 请求体只含 `consent_id` 与新的 `allowed_fields`；患者、接收方、用途均不变。
- **只减不增**：新范围必须是当前范围的**严格真子集**（至少移除一个类别）。
  - 新范围里出现当前没有的类别：返回 HTTP 400，错误码 `scope_widening_rejected`，
    当前范围保持不变；需要更多字段时应**重新授予一条新同意**。
  - 新范围与当前范围相同（未移除任何类别）：返回 HTTP 400，错误码 `scope_unchanged`。
  - 允许一路收窄到空范围（之后任何字段类别请求都按 `scope_exceeded` 拒绝）。
- 收窄**对后续访问立即生效**：未被保留的类别之后再来请求按 `scope_exceeded`
  整体拒绝，不返回任何受保护数据；被保留类别照常访问。
- 每次收窄落一条 `consent_scope_narrowed` 审计，其中 `previous_fields` 记录变更前
  范围、`allowed_fields` 记录变更后范围，可完整重放范围是怎么一步步变小的。
- 已撤销的同意不能再收窄（HTTP 400，`error=consent_revoked`）。

### 2.2 暂停 / 恢复（`POST /consents/suspend`、`/consents/resume`）

- 暂停期间，该接收方对该用途的访问**一律拒绝**，原因码为独立的
  `consent_suspended`，与没授权 / 已撤销 / 已过期 / 用途不对 / 接收方不对 /
  超范围都能分开，且不返回任何数据。
- 暂停**不是撤销**：`active` 保持 `true`，只是临时挂起；恢复的是**同一条同意**，
  `consent_id`、范围都不变。
- 暂停**不改变有效期**：有效期不会因暂停而顺延，暂停期间时间照常流逝。
  - 恢复后若同意仍在有效期内，按原范围继续生效；
  - **已过期的同意即使恢复，也仍按 `consent_expired` 处理**；
  - **已撤销的同意不能被恢复**（HTTP 400，`error=consent_revoked`）。
- 暂停、恢复各落一条审计（`consent_suspended` / `consent_resumed`），均含时间戳。
- 暂停 / 恢复对重复调用幂等：对已暂停同意再暂停、对未暂停同意再恢复，不报错也不
  重复落审计。
- 已撤销的同意不能再暂停。

## 3. 决策顺序与拒绝原因分类

评估严格按以下固定顺序短路，保证相同输入永远得到相同、可区分的结论
（`POST /access/data` 返回 `denial_reason`；门控类失败返回 HTTP 403）：

| 顺序 | 拒绝原因码 | 触发条件 |
| --- | --- | --- |
| 1 | `consent_not_found` | 该患者从未授予过任何同意（或患者不存在） |
| 2 | `consent_revoked` | 命中的同意 `active=false`，撤销对后续访问立即生效 |
| 3 | `consent_suspended` | 命中的同意处于暂停状态（暂停中，未撤销） |
| 4 | `consent_expired` | `expires_at <= now`（含暂停期间到期、恢复后仍过期） |
| 5 | `purpose_mismatch` | 存在同意但用途不匹配 |
| 6 | `recipient_mismatch` | 存在同意但接收方不匹配 |
| 7 | `scope_exceeded` | 请求的任一字段类别不在 `allowed_fields` 内（整体拒绝） |
| 8 | `audit_write_failed` | 审计无法写入，**失败关闭**（见第 5 节） |

> 暂停排在过期之前：暂停中先报 `consent_suspended`；恢复之后才会暴露过期结论。

- 越权或超范围请求**不会返回任何受保护数据**（`data=null`）。
- 请求支持显式传入 `now`（ISO 时间），使过期判定可重复回放。
- 状态变更用法错误（扩大范围、空收窄、撤销后恢复/暂停等）返回 HTTP 400，
  响应体形如 `{"error": "<错误码>", "message": "..."}`，与门控拒绝（403 +
  `denial_reason`）区分。

## 4. 最小必要与去标识化

- 结果只投影被授权类别下的字段；未授权类别整类缺省。
- `identity` / `contact` 为敏感字段，**即使在授权范围内，返回前也强制掩码**：
  - 姓名：保留首字，其余 `*`（`张三丰 -> 张**`）
  - 身份证：仅留后 4 位（`**************7890`）
  - 手机号：保留前 3 / 后 4 位（`138****5678`）
  - 邮箱：保留首字符与域名（`s***@example.com`）
- 响应以 `returned_fields` / `masked_fields` 明示“返回了什么、脱敏了什么”。
- 错误响应体与运行日志只出现患者/同意标识与原因码，**绝不出现未脱敏原始值**。

## 5. 审计、失败关闭与并发自洽

`GET /audit` 返回可重放的有序事件流，事件类型：
`consent_granted`、`consent_revoked`、`consent_scope_narrowed`、
`consent_suspended`、`consent_resumed`、`access_granted`、`access_denied`。

规则：

1. 授予、撤销、收窄（含前后范围）、暂停、恢复、访问成功、访问失败、去标识化结果
   （字段名 + 脱敏标记）全部落审计。
2. 审计**只记录字段名 / 类别 / 脱敏方式，不记录任何原始敏感值**。
3. **失败关闭覆盖所有会改变同意状态的写操作**：授予、撤销、收窄、暂停、恢复与
   数据访问一样，审计写不进去时一律失败，返回 `audit_write_failed`（HTTP 403）。
   - 状态变更采用“**在副本上修改 → 审计先落盘 → 状态后提交**”的顺序：审计失败时
     存储内原对象保持变更前状态，**不会出现接口报错但同意已经生效 / 已经失效 /
     范围已经变小 / 已经暂停或恢复的半生效结果**；失败前后同一访问请求拿到的结论
     完全一致。
4. **变更与访问并发的自洽性**：整条访问路径（决策 → 审计 → 投影/脱敏）与全部状态
   变更共用同一把存储锁、互斥执行。每个访问看到的要么全是变更前状态、要么全是
   变更后状态：
   - 收窄并发访问时，放行响应只可能返回完整旧范围，被拒响应一律 `scope_exceeded`
     且 `data=null`，绝不会返回已被移除类别的字段；
   - 暂停并发访问时，要么带数据放行，要么以 `consent_suspended` 拒绝且无数据；
   - 变更单向生效后结论不反复（首个拒绝 / 放行之后，后续结论保持一致）。
5. 可通过 `POST /test/audit-failure?enabled=true|false` 注入 / 解除审计落盘故障，
   专门用于验证失败关闭行为。

## 6. API 一览

| 方法与路径 | 说明 |
| --- | --- |
| `POST /admin/patients` | 创建患者 |
| `POST /admin/recipients` | 创建数据接收方 |
| `POST /consents` | 授予同意（可带 `expires_at`、`now`） |
| `POST /consents/revoke` | 撤销同意（立即生效） |
| `POST /consents/narrow` | 收窄范围（只减不增；扩大/空收窄返回 400） |
| `POST /consents/suspend` | 暂停同意（独立拒绝原因，不改有效期） |
| `POST /consents/resume` | 恢复同意（已撤销不可恢复，过期仍按过期） |
| `POST /access/decision` | 仅返回授权结论与范围，不返回数据 |
| `POST /access/data` | 决策 + 最小必要投影 + 脱敏后返回数据 |
| `GET /audit` | 读取审计事件流 |
| `POST /test/audit-failure` | 测试辅助：注入/解除审计故障 |

服务启动时自动写入两个患者（`P-001` 张三丰、`P-002` 李四）与三个接收方
（`R-HOSPITAL`、`R-RESEARCH`、`R-INSURER`）；同意不预建，由调用方授予。

### 兼容边界

- 对现有调用方完全向后兼容：`Consent` 仅新增 `suspended` / `suspended_at` /
  `resumed_at` 字段（默认未暂停），既有授予 / 撤销 / 访问接口的请求与响应结构不变。
- 决策顺序仅在“已撤销”与“已过期”之间插入了新的 `consent_suspended`；未使用暂停
  能力的同意，其拒绝原因分类与旧版完全一致。
- 新增的状态变更用法错误使用 HTTP 400 + `error` 码，不复用门控拒绝（403 +
  `denial_reason`），避免与访问拒绝原因混淆。
- 收窄不能扩大范围；想在收窄后拿回更多字段，唯一方式是重新授予一条新同意
 （同三元组下最近授予的一条优先生效）。
- 暂停不延长有效期，也不能复活已撤销 / 已过期的同意。

## 7. 本地验证

依赖：Python 3.14、uv（FastAPI 随项目锁定）。

```bash
# 启动服务
uv run uvicorn main:app --reload

# 运行全部测试（56 个用例；-s 打开日志，打印输入与授权判定依据）
uv run pytest

# 仅跑某一类场景
uv run pytest tests/test_consent_decisions.py   # 同意状态/拒绝原因
uv run pytest tests/test_deidentification.py    # 最小必要/脱敏边界
uv run pytest tests/test_audit.py               # 审计/访问失败关闭
uv run pytest tests/test_narrowing.py           # 收窄生效与只减不增
uv run pytest tests/test_suspend_resume.py      # 暂停/恢复状态流转
uv run pytest tests/test_fail_closed_writes.py  # 全部写操作失败关闭一致性
uv run pytest tests/test_concurrent_changes.py  # 收窄/暂停/恢复与访问并发自洽
uv run pytest tests/test_concurrency.py         # 并发撤销与访问（回归）
uv run pytest tests/test_edge_cases.py          # 边界与用例隔离
```

测试全部通过服务的 HTTP 对外接口验证；`tests/conftest.py` 为每个用例构造
**全新的应用与内存存储**，用例之间相互隔离、可重复执行。

典型手工验证：

```bash
BASE=http://127.0.0.1:8000
# 授予：医院、治疗用途、仅 identity+medical
curl -s -X POST $BASE/consents -H 'Content-Type: application/json' \
  -d '{"patient_id":"P-001","recipient_id":"R-HOSPITAL","purpose":"treatment","allowed_fields":["identity","medical"]}'
# 收窄到只剩 medical（响应里 consent_id 替换成上一步返回值）
curl -s -X POST $BASE/consents/narrow -H 'Content-Type: application/json' \
  -d '{"consent_id":"<CID>","allowed_fields":["medical"]}'
# 再请求 identity：scope_exceeded 且 data=null
curl -s -X POST $BASE/access/data -H 'Content-Type: application/json' \
  -d '{"patient_id":"P-001","recipient_id":"R-HOSPITAL","purpose":"treatment","requested_fields":["identity"]}'
# 暂停后访问：consent_suspended；恢复后 medical 重新可访问
curl -s -X POST $BASE/consents/suspend -H 'Content-Type: application/json' -d '{"consent_id":"<CID>"}'
curl -s -X POST $BASE/consents/resume  -H 'Content-Type: application/json' -d '{"consent_id":"<CID>"}'
# 审计流中可看到收窄前后范围、暂停与恢复
curl -s $BASE/audit
```

失败关闭手工验证：先 `POST /test/audit-failure?enabled=true`，再调用授予 / 撤销 /
收窄 / 暂停 / 恢复，均应返回 403 且 `denial_reason=audit_write_failed`；解除故障后
检查同意状态与故障前完全一致（范围未变、未撤销、暂停态未翻转）。

## 8. 项目结构

```
app/
  models.py       领域模型、用途/字段类别/拒绝原因/审计事件枚举与 DTO
  errors.py       门控异常(AUDIT_WRITE_FAILED 等)与状态变更用法异常(400)
  storage.py      线程安全的本地内存存储 + 审计故障注入（单一锁串行化读写）
  audit.py        审计记录（失败转 AUDIT_WRITE_FAILED）
  consent.py      授予/撤销/收窄/暂停/恢复与固定顺序决策（副本修改+审计先落盘）
  deidentify.py   字段类别映射与脱敏、最小必要投影
  access.py       锁内 决策->审计->投影/脱敏 的访问编排（失败关闭）
  main.py         FastAPI 路由、错误响应、种子数据
tests/             通过 HTTP 接口的隔离测试
```
