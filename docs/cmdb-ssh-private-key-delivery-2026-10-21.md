# SSH 私钥下载链路改造（credential_id 引用）

日期：2026-10-21
状态：已实施

## 1. 问题

`host_info.py`（以及全部 `SSHNodeParamsMixin` 派生模型）无法使用系统内置「密钥」
类型凭据。根因不在插件，而在**下发链路**：

- 私钥是 PEM 多行文本；
- 原实现把 `${PRIVATEKEY_private_key_<instance>}` 放进 HTTP header，
  由 Telegraf 展开成真实 PEM；
- 展开后的 header 值含 `\r\n`，Go `net/http` 直接以
  `invalid header field value` 拒绝请求，采集端从未收到请求；
- 同时 Telegraf 的 TOML 字符串也无法承载多行值，`base.child.toml.j2`
  渲染阶段同样会破坏配置。

密码凭据不受影响，因为它是单行文本。

### 1.1 顺带修复的缺陷

`_auth_payload()` 原来只判断 `credential.get("private_key")` 是否存在。
当凭据为 `auth_method=key` 但私钥为空（例如编辑凭据时只切换了认证方式）时，
会静默落到密码分支并下发 `password=""`，把凭据问题伪装成目标主机认证失败。

现在改为按 `auth_method` 判定，并在「既无正文又无引用」时于**下发前**失败。

## 2. 方案

私钥正文不再随 headers 下发，只下发**引用**；Stargazer 在真正执行前回取。
这与仓库既有的 `cmdb_get_custom_script`（脚本正文不进 headers）是同一模式。

```
Server 下发              Telegraf 周期采集                 Stargazer
─────────               ─────────────────                ──────────
set_credential()  ──►   headers: cmdbcredential_id=cred_x
  headers 只含            （单行，无 CRLF）
  credential_id    ──►   POST /api/collect/collect_info ──►  params["credential_id"]
                                                              │
                                                              ▼
                                                     _prepare_exec_params()
                                                              │
                                       NATS RPC ──────────────┘
                                       cmdb_get_collect_credential
                                       {collect_task_id, credential_id}
                                                              │
                                                              ▼
                                                      Server: 校验任务确实
                                                      引用该凭据 → 返回
                                                      {private_key, passphrase}
                                                              │
                                                              ▼
                                                     nats-executor: ssh.execute.<node>
```

### 2.1 服务端

| 文件 | 变更 |
|---|---|
| `server/apps/cmdb/node_configs/ssh/base.py` | `_auth_payload`/`_auth_env` 在私钥认证下返回空；新增 `_uses_private_key`、`_require_private_key_reference` |
| `server/apps/cmdb/nats/collect_credential.py`（新） | `cmdb_get_collect_credential` responder |
| `server/apps/cmdb/nats/nats.py` | 末尾导入新 responder（`apps.py` 只导入 `nats.nats`） |

回取边界：

- 必须同时提供 `collect_task_id` 与 `credential_id`；
- 服务端用 `resolve_task_credential_pool(instance)` 重新解析该任务的凭据池，
  **只返回 credential_id 精确匹配的那一条**；
- 只返回 `private_key` / `passphrase` 两个字段，不回传 username/port 等；
- 日志只记录定位信息，不记录正文。

### 2.2 Stargazer

| 文件 | 变更 |
|---|---|
| `agents/stargazer/service/credential_loader.py`（新） | `load_private_key()` NATS Adapter，失败抛 `CredentialFetchError` |
| `agents/stargazer/plugins/script_executor.py` | `credential_id`/`collect_task_id` 入参；`_ensure_private_key()` + `_prepare_exec_params()`；`probe()` 与 `list_all_resources()` 改用后者 |
| `agents/stargazer/plugins/inputs/config_file/config_file_info.py` | `_execute_script()` 改用 `_prepare_exec_params()` |

`_ensure_private_key()` 是幂等且按需的：

- 已有 `private_key`（旧路径或上一轮已拉取）→ 不发 RPC；
- 无 `credential_id`（密码认证）→ 不发 RPC；
- `node_info` 存在（目标节点本地执行）→ 不发 RPC。

`host_info.py` 无需改动：它继承 `SSHPlugin`，经 `super().list_all_resources()`
自动获得该行为。

## 3. 兼容性

| 场景 | 行为 |
|---|---|
| 内置凭据（vault）+ 私钥 | headers 只含 `credential_id`；回取时经系统管理解析 |
| 混合池（含 vault 条目）+ inline 私钥 | 同左。`normalize_pool()` 对全池 `setdefault("credential_id")`，inline 候选也获得引用 |
| 纯 inline 私钥（历史直填，池中无 vault 条目） | 仍走 legacy `PRIVATEKEY_*` 占位符 + `env_config`；见下方「已知限制」 |
| 内置凭据 + 密码 | 不变（单行，仍走 `PASSWORD_*` 占位符 + env） |
| 自定义脚本 / 配置文件采集 | 继承 `SSHPlugin`，自动生效 |

`resolve_task_credential_pool()` 的补齐行为有条件：它先在
`collect_vault_resolver.py:70` 判断池中是否存在 `credential_source == "vault"`
的条目，**没有就直接返回原始候选**，不会调用
`CollectCredentialPoolService.normalize_pool()`。也就是说：

- 池中至少有一个 vault 凭据 → `normalize_pool()` 对**所有**候选
  （含 inline）执行 `setdefault("credential_id")`，每个私钥候选都有引用；
- 池中全是 inline 凭据 → 不补引用，历史任务的私钥仍只能走
  `PRIVATEKEY_*` 占位符。

`_auth_payload()` 保留的 legacy env 分支因此不只是防御，而是存量
inline 私钥任务的实际路径（`test_pc_node_params.py::
test_pc_node_params_macos_inline_private_key_keeps_legacy_env_channel`
锁定该行为）。

## 4. 安全收益

- 私钥不再出现在 Telegraf 配置、HTTP headers、节点配置快照中；
- 私钥不再随 header 进入任何访问日志；
- 回取需要「任务 ID + 该任务确实引用的 credential_id」双重条件，
  credential_id 单独泄露不足以取到私钥。

与 `get_nodes_by_ips`、`cmdb_get_custom_script` 同级：NATS 内网平面，
不做用户态过滤，任务自身 team 边界即隔离。

## 5. 已知限制

- 每次执行前多一次 NATS RPC（约 1 次/任务/轮次）。私钥未缓存到磁盘，
  这是刻意取舍；
- `resolve_task_credential_pool()` 会解析整个凭据池，多凭据任务里若有
  其他凭据失效，本次回取会一起失败（fail-closed）；
- **纯 inline 私钥（存量任务）仍会撞 CRLF 限制。** 这类任务池中没有
  vault 条目，`resolve_task_credential_pool()` 提前返回、不补
  `credential_id`，于是私钥继续走 `PRIVATEKEY_*` 占位符；Telegraf 展开后
  header 变多行，请求仍会被拒。修复方式是把凭据迁移到凭据管理
  （产生 `credential_source=vault` / `credential_id`）后重新下发配置。
  本次改动保证新路径可用且旧路径不进一步退化，未做存量自动迁移；
- 计划中的后续项：把 private_key 的 TTL 缓存写入 Redis，减少 vault 往返。

## 6. 验证

- `server/apps/cmdb/tests/test_ssh_private_key_delivery.py`（新增）：
  header 不含 PEM、旧路径保留、`auth_method=key` 无密钥失败、回取边界；
- `server/apps/cmdb/tests/test_node_params_multicred.py`：
  `test_host_node_params_multicred_private_key_ships_reference_only`
  锁定多凭据池里私钥条目只下发 `credential_id`；
- `server/apps/cmdb/tests/test_pc_node_params.py`：
  `test_pc_node_params_macos_private_key_contract`（有引用 → 只发
  `credential_id`）与
  `test_pc_node_params_macos_inline_private_key_keeps_legacy_env_channel`
  （无引用 → 保留 legacy env 通道）；
- `agents/stargazer/tests/test_ssh_credential_fetch.py`（新增）：
  按引用回取、已持有私钥不重复回取、密码凭据不触发 RPC、回取失败暴露为
  `cmdb_collect_error`。

验收仍以 `docs/reviews/cmdb-pc-discovery-2026-07-22/03-macos-acceptance.md`
中 M2/M3 的 PEM 认证项为准。
