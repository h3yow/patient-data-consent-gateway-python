# 患者数据共享同意与访问网关（Python / FastAPI）

一套在本地 API 服务之上实现的“患者数据共享同意与访问网关”。用本地内存存储模拟
患者、数据接收方、同意授权与审计记录，目标是让**不同同意状态、访问范围与并发操作
下的授权结论一致且可解释**。

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

字段类别（`FieldCategory`）与患者字段的映射：

| 类别 | 敏感性 | 包含字段 |
| --- --- | --- |
| `identity` | 敏感 | `name`, `id_card` |
| `contact` | 敏感 | `phone`, `email` |
| `profile` | 非敏感 | `gender`, `birth_date` |
| `medical` | 非敏感 | `diagnosis`, `medications` |

同一 `(patient, recipient, purpose)` 三元组允许存在多条历史同意，决策时取
**最近授予的一条**；已撤销 / 已过期的历史同意不会“复活”授权。

## 2. 决策顺序与拒绝原因分类

评估严格按以下固定顺序短路，保证相同输入永远得到相同、可区分的结论
（`POST /access/data` 返回 `denial_reason`；门控类失败返回 HTTP 403）：

| 顺序 | 拒绝原因码 | 触发条件 |
| --- | --- | --- |
| 1 | `consent_not_found` | 该患者从未授予过任何同意（或患者不存在） |
| 2 | `consent_revoked` | 命中的同意 `active=false`，撤销对后续访问立即生效 |
| 3 | `consent_expired` | `expires_at <= now` |
| 4 | `purpose_mismatch` | 存在同意但用途不匹配 |
| 5 | `recipient_mismatch` | 存在同意但接收方不匹配 |
| 6 | `scope_exceeded` | 请求的任一字段类别不在 `allowed_fields` 内（整体拒绝） |
| 7 | `audit_write_failed` | 审计无法写入，**失败关闭**（见第 4 节） |

- 越权或超范围请求**不会返回任何受保护数据**（`data=null`）。
- 请求支持显式传入 `now`（ISO 时间），使过期判定可重复回放。

## 3. 最小必要与去标识化

- 结果只投影被授权类别下的字段；未授权类别整类缺省。
- `identity` / `contact` 为敏感字段，**即使在授权范围内，返回前也强制掩码**：
  - 姓名：保留首字，其余 `*`（`张三丰 -> 张**`）
  - 身份证：仅留后 4 位（`**************7890`）
  - 手机号：保留前 3 / 后 4 位（`138****5678`）
  - 邮箱：保留首字符与域名（`s***@example.com`）
- 响应以 `returned_fields` / `masked_fields` 明示“返回了什么、脱敏了什么”。
- 错误响应体与运行日志只出现患者/同意标识与原因码，**绝不出现未脱敏原始值**。

## 4. 审计与失败关闭

`GET /audit` 返回可重放的有序事件流，事件类型：
`consent_granted`、`consent_revoked`、`access_granted`、`access_denied`。

规则：

1. 授予、撤销、访问成功、访问失败、去标识化结果（字段名 + 脱敏标记）全部落审计。
2. 审计**只记录字段名 / 类别 / 脱敏方式，不记录任何原始敏感值**。
3. 审计写入失败时，授予、撤销与数据访问一律**失败关闭**，返回
   `audit_write_failed`（HTTP 403），且不返回任何数据。
4. 可通过 `POST /test/audit-failure?enabled=true|false` 注入 / 解除审计落盘故障，
   专门用于验证失败关闭行为。

## 5. API 一览

| 方法与路径 | 说明 |
| --- | --- |
| `POST /admin/patients` | 创建患者 |
| `POST /admin/recipients` | 创建数据接收方 |
| `POST /consents` | 授予同意（可带 `expires_at`、`now`） |
| `POST /consents/revoke` | 撤销同意（立即生效） |
| `POST /access/decision` | 仅返回授权结论与范围，不返回数据 |
| `POST /access/data` | 决策 + 最小必要投影 + 脱敏后返回数据 |
| `GET /audit` | 读取审计事件流 |
| `POST /test/audit-failure` | 测试辅助：注入/解除审计故障 |

服务启动时自动写入两个患者（`P-001` 张三丰、`P-002` 李四）与三个接收方
（`R-HOSPITAL`、`R-RESEARCH`、`R-INSURER`）；同意不预建，由调用方授予。

## 6. 本地验证

依赖：Python 3.14、uv（FastAPI 随项目锁定）。

```bash
# 启动服务
uv run uvicorn main:app --reload

# 运行全部测试（30 个用例；-s 打开日志，打印输入与授权判定依据）
uv run pytest

# 仅跑某一类场景
uv run pytest tests/test_consent_decisions.py   # 同意状态/拒绝原因
uv run pytest tests/test_deidentification.py    # 最小必要/脱敏边界
uv run pytest tests/test_audit.py               # 审计/失败关闭
uv run pytest tests/test_concurrency.py         # 并发撤销与访问
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
# 访问：姓名/身份证已掩码，且不含其他类别字段
curl -s -X POST $BASE/access/data -H 'Content-Type: application/json' \
  -d '{"patient_id":"P-001","recipient_id":"R-HOSPITAL","purpose":"treatment","requested_fields":["identity","medical"]}'
```

## 7. 项目结构

```
app/
  models.py       领域模型、用途/字段类别/拒绝原因枚举与 DTO
  errors.py       门控异常（携带可区分拒绝原因）
  storage.py      线程安全的本地内存存储 + 审计故障注入
  audit.py        审计记录（失败转 AUDIT_WRITE_FAILED）
  consent.py      同意授予/撤销与固定顺序的授权决策
  deidentify.py   字段类别映射与脱敏、最小必要投影
  access.py       决策->审计->投影/脱敏 的访问编排（失败关闭）
  main.py         FastAPI 路由、错误响应、种子数据
tests/            通过 HTTP 接口的隔离测试
```
