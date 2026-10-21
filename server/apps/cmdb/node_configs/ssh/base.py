# -- coding: utf-8 --
# @File: base.py
# @Time: 2025/11/13 14:25
# @Author: windyzhao


class SSHNodeParamsMixin:
    supported_model_id = ""
    plugin_name = f"{supported_model_id}_info"
    interval = 300  # 默认采集间隔：300秒
    host_field = "ip_addr"
    executor_type = "job"  # 默认执行器类型
    # 凭据 schema 的 auth_method 取值（password / key）；别名一并容错。
    PRIVATE_KEY_AUTH_METHODS = frozenset({"key", "private_key", "publickey", "public_key"})

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.executor_type = "job"

    def set_credential(self, *args, **kwargs):
        credential_data = {
            "node_id": self.instance.access_point[0]["id"],
            # 脚本执行上限由 agent 侧硬编码（默认 60s）；表单 timeout 仅作单对象采集预算。
        }
        if self.credential:
            self._require_private_key_reference(self.credential)
            credential_data.update(self._auth_payload(self.credential))
            credential_data["port"] = self.credential.get("port", 22)
            credential_data["username"] = self.credential.get("username", self.credential.get("user", ""))
            if self.credential.get("credential_id"):
                credential_data["credential_id"] = self.credential.get("credential_id")
        return credential_data

    def env_config(self, *args, **kwargs):
        if not self.credential:
            return {}
        env = {}
        if self.has_multiple_credentials:
            for index, credential in enumerate(self.credential_pool or []):
                env.update(self._auth_env(credential, index))
        else:
            env.update(self._auth_env(self.credential))
        return env

    def build_credentials_pool(self):
        if not self.has_multiple_credentials:
            return []
        pool = []
        for index, credential in enumerate(self.credential_pool or []):
            self._require_private_key_reference(credential)
            item = {
                "node_id": self.instance.access_point[0]["id"],
                "port": credential.get("port", 22),
                "username": credential.get("username", credential.get("user", "")),
            }
            item.update(self._auth_payload(credential, index))
            if credential.get("credential_id"):
                item["credential_id"] = credential.get("credential_id")
            pool.append(item)
        return pool

    def _require_private_key_reference(self, credential):
        """私钥既没有正文也没有引用时，必须在下发前失败。

        否则请求会以「无密码、无密钥」的形态到达执行器，凭据问题最终表现为
        目标主机认证失败，排查成本远高于一次明确的下发错误。
        """
        if not self._uses_private_key(credential):
            return
        if credential.get("credential_id") or credential.get("private_key"):
            return
        raise ValueError("SSH 私钥凭据缺少私钥内容，请在凭据管理中重新提交")

    def _auth_payload(self, credential, index=None):
        if self._uses_private_key(credential):
            # 私钥是 PEM 多行文本：HTTP header 禁止 CRLF，Telegraf TOML 字符串也容纳不下换行，
            # 一旦把正文（或正文占位符）塞进 headers，请求在下发链路就会被拒。
            # 因此 headers 只下发 credential_id，Stargazer 运行期经 NATS RPC 回取私钥。
            if credential.get("credential_id"):
                return {}
            # 兼容历史直填私钥（无 credential_id）的任务，仍走环境变量通道。
            payload = {"private_key": "${" + self._private_key_env_name(index) + "}"}
            if credential.get("passphrase"):
                payload["passphrase"] = "${" + self._passphrase_env_name(index) + "}"
            return payload
        return {"password": "${" + self._password_env_name(index) + "}"}

    def _auth_env(self, credential, index=None):
        if self._uses_private_key(credential):
            if credential.get("credential_id"):
                return {}
            env = {self._private_key_env_name(index): credential.get("private_key") or ""}
            if credential.get("passphrase"):
                env[self._passphrase_env_name(index)] = credential["passphrase"]
            return env
        return {self._password_env_name(index): credential.get("password", "")}

    def _uses_private_key(self, credential):
        """按 auth_method 判定认证方式；缺失时回退到「是否填了私钥」。

        只看 private_key 是否存在会让 auth_method=key 但私钥为空的任务静默降级成
        空密码认证，凭据问题会伪装成网络/网络层失败，所以这里显式以认证方式为准。
        """
        method = str(credential.get("auth_method") or "").strip().lower()
        if method:
            return method in self.PRIVATE_KEY_AUTH_METHODS
        return bool(credential.get("private_key"))

    def _password_env_name(self, index=None):
        if index is None:
            return "PASSWORD_password_{end_start}".format(end_start=self._instance_id)
        return "PASSWORD_password_{end_start}_{index}".format(end_start=self._instance_id, index=index)

    def _private_key_env_name(self, index=None):
        suffix = "" if index is None else f"_{index}"
        return f"PRIVATEKEY_private_key_{self._instance_id}{suffix}"

    def _passphrase_env_name(self, index=None):
        suffix = "" if index is None else f"_{index}"
        return f"PASSPHRASE_passphrase_{self._instance_id}{suffix}"
