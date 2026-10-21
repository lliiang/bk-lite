"""采集私钥运行期回取 Adapter。

私钥是多行 PEM，不能放进 HTTP header（header 禁止 CRLF），也无法通过 Telegraf
的 TOML 字符串承载。下发链路因此只传 `credential_id`，真正的私钥正文由本模块
在每次执行前经 NATS RPC 向 Server 回取。

调用方用 `load_private_key()`；失败一律抛 `CredentialFetchError`，
让采集以「凭据失败」而不是「目标主机认证失败」的形态暴露出来。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from core.infra.nats_utils import nats_request


class CredentialFetchError(RuntimeError):
    """按 credential_id 回取私钥失败。"""


async def load_private_key(
    collect_task_id: Any,
    credential_id: str,
    *,
    timeout_seconds: float = 15.0,
) -> Mapping[str, Any]:
    """返回 {"private_key": str, "passphrase": str}。"""
    credential_id = str(credential_id or "").strip()
    if collect_task_id in (None, "") or not credential_id:
        raise CredentialFetchError("CREDENTIAL_FETCH_INVALID_REQUEST")

    namespace = os.getenv("NATS_NAMESPACE", "bklite")
    query = {"collect_task_id": collect_task_id, "credential_id": credential_id}
    payload = json.dumps({"args": [query], "kwargs": {}}).encode()
    response = await nats_request(
        f"{namespace}.cmdb_get_collect_credential",
        payload=payload,
        timeout=timeout_seconds,
    )
    if not isinstance(response, Mapping) or not response.get("success"):
        detail = response.get("error") if isinstance(response, Mapping) else "invalid response"
        raise CredentialFetchError(f"CREDENTIAL_FETCH_FAILED: {detail}")
    result = response.get("result")
    if not isinstance(result, Mapping) or not str(result.get("private_key") or "").strip():
        raise CredentialFetchError("CREDENTIAL_FETCH_EMPTY")
    return {
        "private_key": result["private_key"],
        "passphrase": str(result.get("passphrase") or ""),
    }
