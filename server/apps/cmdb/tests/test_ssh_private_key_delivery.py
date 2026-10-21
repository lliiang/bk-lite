# -*- coding: utf-8 -*-
"""SSH 私钥下发契约。

私钥是 PEM 多行文本，HTTP header 禁止 CRLF，Telegraf TOML 字符串也容纳
不下换行。因此 headers 只携带 credential_id，正文由 Stargazer 运行期经
NATS RPC 回取。本文件锁定「正文不进 headers」与「回取边界」两件事。
"""

from types import SimpleNamespace

import pytest

from apps.cmdb.node_configs.ssh.host import HostNodeParams

PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3Nza\n-----END OPENSSH PRIVATE KEY-----"


def _host_params(credential):
    instance = SimpleNamespace(
        id=93,
        model_id="host",
        driver_type="job",
        decrypt_credentials=credential,
        params={},
        timeout=60,
        access_point=[{"id": 3}],
        instances=[{"ip_addr": "10.0.0.9"}],
        ip_range="",
    )
    return HostNodeParams(instance)


def test_private_key_credential_only_ships_reference_in_headers():
    node = _host_params(
        [
            {
                "credential_id": "cred_key01",
                "username": "ops",
                "auth_method": "key",
                "private_key": PEM,
                "passphrase": "pp",
                "port": 22,
            }
        ]
    )

    headers = node.custom_headers()

    assert headers["cmdbcredential_id"] == "cred_key01"
    assert "cmdbprivate_key" not in headers
    assert "cmdbpassphrase" not in headers
    # 多行 PEM 既不能进 header，也不再需要注入 env。
    assert node.env_config() == {}
    assert PEM not in str(headers)


def test_private_key_without_reference_keeps_legacy_env_channel():
    """历史直填私钥（无 credential_id）没有可回取的引用，继续走 env。"""
    node = _host_params([{"username": "ops", "private_key": PEM, "port": 22}])

    headers = node.custom_headers()

    assert headers["cmdbprivate_key"] == "${PRIVATEKEY_private_key_cmdb_93}"
    assert node.env_config() == {"PRIVATEKEY_private_key_cmdb_93": PEM}


def test_key_auth_without_any_secret_fails_before_dispatch():
    """auth_method=key 但既无私钥也无引用时，必须显式失败。

    否则会下发「无密码、无密钥」的请求，把凭据问题伪装成目标主机认证失败。
    """
    node = _host_params([{"username": "ops", "auth_method": "key", "port": 22}])

    with pytest.raises(ValueError, match="私钥"):
        node.set_credential()


def _patch_task(monkeypatch, collect_credential, instance, pool):
    monkeypatch.setattr(
        collect_credential,
        "CollectModels",
        SimpleNamespace(objects=SimpleNamespace(filter=lambda **kwargs: SimpleNamespace(first=lambda: instance))),
    )
    monkeypatch.setattr(
        "apps.cmdb.services.collect_vault_resolver.resolve_task_credential_pool",
        lambda target: pool,
    )


def test_nats_handler_returns_only_the_referenced_private_key(monkeypatch):
    from apps.cmdb.nats import collect_credential

    _patch_task(
        monkeypatch,
        collect_credential,
        SimpleNamespace(id=42),
        [
            {"credential_id": "cred_key01", "private_key": PEM, "passphrase": "pp"},
            {"credential_id": "cred_other", "private_key": "OTHER-PEM"},
        ],
    )

    result = collect_credential.cmdb_get_collect_credential({"collect_task_id": 42, "credential_id": "cred_key01"})

    assert result == {"private_key": PEM, "passphrase": "pp"}


def test_nats_handler_rejects_credential_not_bound_to_task(monkeypatch):
    from apps.cmdb.nats import collect_credential

    _patch_task(
        monkeypatch,
        collect_credential,
        SimpleNamespace(id=42),
        [{"credential_id": "cred_key01", "private_key": PEM}],
    )

    with pytest.raises(ValueError, match="未引用该凭据"):
        collect_credential.cmdb_get_collect_credential({"collect_task_id": 42, "credential_id": "cred_other"})


def test_nats_handler_rejects_empty_private_key(monkeypatch):
    from apps.cmdb.nats import collect_credential

    _patch_task(
        monkeypatch,
        collect_credential,
        SimpleNamespace(id=42),
        [{"credential_id": "cred_key01", "private_key": "   "}],
    )

    with pytest.raises(ValueError, match="缺少 SSH 私钥"):
        collect_credential.cmdb_get_collect_credential({"collect_task_id": 42, "credential_id": "cred_key01"})
