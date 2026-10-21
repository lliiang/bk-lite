# -*- coding: utf-8 -*-
"""SSH 私钥运行期回取契约。

私钥是 PEM 多行文本，HTTP header 禁止 CRLF，Telegraf 的 TOML 字符串也无法
承载换行。因此下发链路只传 credential_id，SSHPlugin 在执行前经 NATS RPC
向 Server 回取私钥正文。本文件锁定该契约。
"""

import json

import pytest
from plugins.script_executor import SSHPlugin


def _plugin(**overrides):
    params = {
        "node_id": "node-1",
        "host": "10.0.0.8",
        "script_content": "true",
        "model_id": "host",
        "username": "ops",
        "port": "22",
        "collect_task_id": "42",
    }
    params.update(overrides)
    return SSHPlugin(params)


@pytest.mark.asyncio
async def test_prepare_exec_params_fetches_private_key_by_credential_id(monkeypatch):
    calls = []

    async def fake_nats(subject, payload=None, timeout=None):
        calls.append((subject, json.loads(payload)["args"][0]))
        return {"success": True, "result": {"private_key": "PEM-BODY", "passphrase": "pp"}}

    monkeypatch.setattr("service.credential_loader.nats_request", fake_nats)
    plugin = _plugin(credential_id="cred_abc123")

    exec_params = await plugin._prepare_exec_params("true")

    assert exec_params["private_key"] == "PEM-BODY"
    assert exec_params["passphrase"] == "pp"
    assert len(calls) == 1
    assert calls[0][0].endswith(".cmdb_get_collect_credential")
    assert calls[0][1] == {"collect_task_id": "42", "credential_id": "cred_abc123"}


@pytest.mark.asyncio
async def test_prepare_exec_params_skips_fetch_when_private_key_already_present(monkeypatch):
    async def fake_nats(*args, **kwargs):
        raise AssertionError("已经拿到私钥时不应再发起 RPC")

    monkeypatch.setattr("service.credential_loader.nats_request", fake_nats)
    plugin = _plugin(credential_id="cred_abc123", private_key="INLINE-PEM")

    exec_params = await plugin._prepare_exec_params("true")

    assert exec_params["private_key"] == "INLINE-PEM"


@pytest.mark.asyncio
async def test_password_credential_does_not_call_credential_rpc(monkeypatch):
    async def fake_nats(*args, **kwargs):
        raise AssertionError("密码认证不应触发私钥回取")

    monkeypatch.setattr("service.credential_loader.nats_request", fake_nats)
    plugin = _plugin(password="secret")

    exec_params = await plugin._prepare_exec_params("true")

    assert "private_key" not in exec_params


@pytest.mark.asyncio
async def test_fetch_failure_surfaces_as_credential_error(monkeypatch):
    async def fake_nats(subject, payload=None, timeout=None):
        return {"success": False, "error": "凭据缺少 SSH 私钥，请在凭据管理中重新提交私钥"}

    monkeypatch.setattr("service.credential_loader.nats_request", fake_nats)
    plugin = _plugin(credential_id="cred_abc123")

    result = await plugin.list_all_resources()

    assert result["success"] is False
    assert "凭据缺少 SSH 私钥" in result["result"]["cmdb_collect_error"]
