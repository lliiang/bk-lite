# -- coding: utf-8 --
# @File: collect_credential.py
"""按凭据引用向采集侧回发 SSH 私钥。

私钥是 PEM 多行文本，无法随 HTTP header 下发（header 禁止 CRLF），
Telegraf 的 TOML 字符串也容纳不下换行。因此下发时 headers 只携带
`credential_id`，Stargazer 在真正执行前经本 RPC 回取私钥正文。

信任级别与 `get_nodes_by_ips` / `cmdb_get_custom_script` 一致：
NATS 内网平面，且只对「该采集任务确实引用了该 credential_id」的凭据返回内容。
"""

import nats_client

from apps.cmdb.models.collect_model import CollectModels
from apps.core.logger import cmdb_logger as logger


@nats_client.register
def cmdb_get_collect_credential(query_data):
    """回取采集任务的 SSH 私钥。

    params={"collect_task_id": 123, "credential_id": "cred_xxx"}
    -> {"private_key": "...", "passphrase": "..."}
    """
    if not isinstance(query_data, dict):
        raise ValueError("query_data must be an object")
    collect_task_id = query_data.get("collect_task_id")
    credential_id = str(query_data.get("credential_id") or "").strip()
    if collect_task_id in (None, ""):
        raise ValueError("collect_task_id is required")
    if not credential_id:
        raise ValueError("credential_id is required")

    instance = CollectModels.objects.filter(id=collect_task_id).first()
    if instance is None:
        raise ValueError("采集任务不存在")

    from apps.cmdb.services.collect_vault_resolver import resolve_task_credential_pool

    pool = resolve_task_credential_pool(instance)
    matched = [item for item in pool if str(item.get("credential_id") or "").strip() == credential_id]
    if not matched:
        raise ValueError("采集任务未引用该凭据")

    private_key = str(matched[0].get("private_key") or "")
    if not private_key.strip():
        raise ValueError("凭据缺少 SSH 私钥，请在凭据管理中重新提交私钥")

    # 只记录定位信息，绝不记录私钥正文。
    logger.info(
        "event=cmdb_collect_credential_fetched collect_task_id=%s credential_id=%s",
        collect_task_id,
        credential_id,
    )
    return {
        "private_key": private_key,
        "passphrase": str(matched[0].get("passphrase") or ""),
    }
