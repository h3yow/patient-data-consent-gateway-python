# 患者数据共享同意与访问网关（本地版）

一个基于 FastAPI 的本地服务，用进程内存储模拟患者档案、数据接收方授权（同意）与数据共享记录。
目标：在不同**同意状态、共享用途、数据接收方、数据范围与并发操作**下，给出**一致、可解释、可追溯**的授权结论，并在返回前完成最小必要裁剪与敏感字段去标识化。

## 1. 同意模型

一条同意（consent）由以下维度唯一刻画：

| 维度 | 说明 |
| --- | --- |
| `patient_id` | 数据主体（患者） |
| `recipient_id` | 数据接收方（如医院、研究机构、商保） |
| `purpose` | 共享用途：`treatment` / `research` / `marketing` / `payment` |
| `fields` | 授权字段范围（白名单） |
| `expires_at` | 可选的过期时刻（UTC）；为空表示长期有效 |
| `status` | `active` / `revoked` / `expired`，`expired` 由时钟惰性求值落库 |

规则：

- 访问必须在**患者 + 接收方 + 用途**三个维度上精确匹配一条同意；
- 同一三元组可以重复授予，决策取 `granted_at` 最新的一条；
- 撤销只作用于被撤销的那一条同意，不影响其他接收方/用途的授权；
- 撤销在同一把存储锁内与访问判定线性化，**对后续访问立即生效**；
- 过期判断使用可注入的时钟（见测试接口），不依赖墙钟，结论可重复。

## 2. 拒绝原因分类

所有拒绝都返回稳定、机器可读的错误码（响应体 `{"error", "message"}`），审计中以 `deny_reason` 同值记录：

| 错误码 | HTTP | 触发条件 |
| --- | --- | --- |
| `PATIENT_NOT_FOUND` | 404 | 患者不存在 |
| `CONSENT_NOT_FOUND` | 404 | 该患者/接收方/用途从未存在授权 |
| `PURPOSE_MISMATCH` | 403 | 接收方存在授权，但用途不匹配 |
| `RECIPIENT_MISMATCH` | 403 | 该用途已授权给其他接收方 |
| `CONSENT_EXPIRED` | 403 | 精确匹配的同意已超过 `expires_at` |
| `CONSENT_REVOKED` | 403 | 精确匹配的同意已被撤销 |
| `SCOPE_EXCEEDED` | 403 | 请求字段中有任意一个不在授权范围内（整单拒绝，不返回任何字段） |
| `AUDIT_WRITE_FAILED` | 500 | 审计无法写入，失败关闭（授予/撤销/访问均不生效） |
| `INVALID_REQUEST` | 400 | 空字段列表、未知字段名、过期时间在过去等 |
| `CONFLICT` | 409 | 重复建患者、重复撤销、撤销已过期同意 |

**多维不匹配的固定优先级**（保证结论确定、可解释）：精确同意的状态（revoked/expired）> `RECIPIENT_MISMATCH`（同用途存在他人授权）> `PURPOSE_MISMATCH`（该接收方有其他用途授权）> `CONSENT_NOT_FOUND`。

## 3. 最小必要与去标识化

- 访问请求显式声明所需字段；**全部字段都必须落在授权范围内**，否则整单拒绝（all-or-nothing），不返回任何受保护数据。
- 结果只包含被请求且被授权的字段。
- 敏感字段在**返回前**完成去标识化（单向掩码，确定性、不可还原）：

| 字段 | 规则 | 示例 |
| --- | --- | --- |
| `full_name` | 保留姓氏，其余掩码 | `张三丰` → `张**` |
| `phone` | 保留前 3 后 4 | `13812341234` → `138****1234` |
| `id_card` | 保留前 6 后 4 | `110101199003071234` → `110101********1234` |
| `email` | 本地段仅保留首字符 | `zhang.sanfeng@example.com` → `z************@example.com` |
| `address` | 仅保留前 6 个字符 | `北京市海淀区中关村大街1号` → `北京市海淀区****` |
| `date_of_birth` | 仅保留年份 | `1990-03-07` → `1990-**-**` |

非敏感字段（`diagnosis`、`medications`、`blood_type`）在授权范围内原样返回。
错误信息、运行日志与审计记录中**只出现字段名与掩码后的值**，绝不出现原始敏感值（测试通过对全量响应/审计文本做原始片段扫描来保证）。

## 4. 审计规则（可追溯 + 失败关闭）

- 审计为**仅追加（append-only）**，带单调递增序号 `seq` 与时间戳，可按序号顺序重放。
- 记录事件：`consent_granted`、`consent_revoked`、`access_allowed`、`access_denied`。
- 访问记录包含：请求字段、授权字段、被掩码字段、**掩码后的结果值**、拒绝原因；不含原始敏感值。
- 撤销原因属于操作者自由文本，可能携带敏感信息，审计中只记录是否提供（`reason_provided` / `no_reason`），同意视图中同样以 `redacted` 占位。
- **先写审计、后生效/返回**：授予先落审计再建同意；撤销先落审计再改状态；访问先落审计再返回数据。审计抛错时一律返回 `AUDIT_WRITE_FAILED`（500），不产生授权、不返回数据。

并发模型：所有“读状态 → 判定 → 写审计 → 改状态”在存储的同一把可重入锁内完成，因此撤销与访问的交错等价于某个串行顺序，且该顺序就是审计顺序。

## 5. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/patients` | 录入患者（模拟本地数据） |
| POST | `/consents` | 授予同意，返回 `consent_id` |
| POST | `/consents/revoke` | 撤销同意（接收方维度校验） |
| GET | `/patients/{id}/consents` | 查看患者的同意与当前状态 |
| POST | `/access` | 按用途/接收方/字段访问数据 |
| GET | `/audit` | 重放全部审计条目 |
| POST | `/internal/clock/advance` | 【测试】推进虚拟时钟 |
| POST | `/internal/audit/failing` | 【测试】注入审计不可写故障 |
| POST | `/internal/reset` | 【测试】清空全部数据，用例隔离 |

`/internal/*` 仅用于本地验证，生产部署应关闭。

快速体验：

```bash
curl -s -X POST localhost:8000/internal/reset
curl -s -X POST localhost:8000/patients -H 'content-type: application/json' -d '{
  "patient_id":"P-0001","full_name":"张三丰","phone":"13812341234",
  "id_card":"110101199003071234","email":"zhang.sanfeng@example.com",
  "address":"北京市海淀区中关村大街1号","date_of_birth":"1990-03-07",
  "diagnosis":"原发性高血压","medications":["氨氯地平"],"blood_type":"O"}'
curl -s -X POST localhost:8000/consents -H 'content-type: application/json' -d '{
  "patient_id":"P-0001","recipient_id":"R-INS","purpose":"payment",
  "fields":["full_name","diagnosis"]}'
curl -s -X POST localhost:8000/access -H 'content-type: application/json' -d '{
  "patient_id":"P-0001","recipient_id":"R-INS","purpose":"payment",
  "fields":["full_name","diagnosis"]}'
```

## 6. 本地验证

```bash
uv run fastapi dev main.py          # 启动服务
uv run pytest -q                    # 全量测试（19 个用例）
uv run pytest -q -s                 # 同时查看输入与授权判定日志
```

测试全部通过服务对外接口（含真实多线程 HTTP 调用）验证，用例间通过 `/internal/reset` 相互隔离，覆盖：

- 并发撤销与访问的线性化一致性（6 访问线程 × 100 次 + 1 撤销线程，结论与审计顺序对齐，重复运行稳定）；
- 同意未到期 / 到期 / 过去时间授予；
- 跨用途、跨接收方、双维不匹配的优先级，以及从未授权；
- 越权字段整单拒绝、响应与审计均不含任何原始数据；
- 六类敏感字段的去标识化边界；
- 审计可重放、只含脱敏值，以及审计不可写时授予/撤销/访问全部失败关闭。

## 7. 代码结构

```
app/
  models.py          # 领域模型、字段清单、敏感字段集合
  errors.py          # 错误码与网关异常
  clock.py           # 可推进的测试时钟
  deidentification.py# 敏感字段掩码规则
  storage.py         # 线程安全内存存储（患者/同意）
  audit.py           # 仅追加审计日志与故障注入
  service.py         # 授权决策、最小必要裁剪、失败关闭编排
  api.py             # HTTP 路由与错误映射
tests/               # 经接口验证的隔离用例
main.py              # FastAPI 装配
```
