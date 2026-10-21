# -- coding: utf-8 --
# @File: physcial_server_info.py.py
# @Time: 2025/12/25 11:14
# @Author: windyzhao
import asyncio
import json
import time
from typing import Any, Dict

from plugins.inputs.physcial_server.redfish_info import PhyscialServerRedfishInfo
from plugins.inputs.physcial_server.server_info_parse import parse_server_info
from plugins.script_executor import SSHPlugin
from sanic.log import logger


class PhyscialServerProtocolInfo:
    """物理服务器带外协议入口；旧任务默认走 IPMI。"""

    def __init__(self, kwargs, *, transport=None):
        protocol = str(kwargs.get("collection_protocol") or "ipmi").strip().lower()
        logger.debug(
            "event=physcial_server_protocol_selected protocol=%s host=%s task_id=%s",
            protocol,
            kwargs.get("host"),
            kwargs.get("collection_task_id") or kwargs.get("task_id") or "",
        )
        if protocol == "redfish":
            self.collector = PhyscialServerRedfishInfo(kwargs, transport=transport)
        elif protocol == "ipmi":
            self.collector = PhyscialServerIPMIInfo(kwargs)
        else:
            raise ValueError("physical server protocol must be ipmi or redfish")

    async def list_all_resources(self):
        return await self.collector.list_all_resources()

    async def probe(self):
        probe = getattr(self.collector, "probe", None)
        if probe is None:
            from core.collection.contracts import AccessProbeResult, AccessProbeStatus

            return AccessProbeResult(status=AccessProbeStatus.NOT_SUPPORTED)
        return await probe()


class PhyscialServerInfo(SSHPlugin):
    async def list_all_resources(self, need_raw=False):
        """
        Convert collected data to a standard format.
        """
        model_id = self.model_id or "physcial_server"
        started_at = time.monotonic()
        try:
            logger.debug(
                "event=physcial_server_collect_started host=%s model_id=%s task_id=%s",
                self.host,
                self.model_id,
                self.collection_task_id,
            )
            data = await super().list_all_resources(need_raw=True)
            if "===" in data.get("result", ""):
                parsed_data = parse_server_info(data.get("result", ""))
                self_device = self.host
                disk_info = parsed_data.pop("disk")
                mem_info = parsed_data.pop("memory")
                nic_info = parsed_data.pop("nic")
                gpu_info = parsed_data.pop("gpu")
                return_data = {
                    model_id: [parsed_data],
                    "disk": [{**i, "self_device": self_device} for i in disk_info],
                    "memory": [{**i, "self_device": self_device} for i in mem_info],
                    "nic": [{**i, "self_device": self_device} for i in nic_info],
                    "gpu": [{**i, "self_device": self_device} for i in gpu_info],
                }
                result = {"success": True, "result": return_data}
                logger.debug(
                    "event=physcial_server_sync_target_succeeded transport=ssh host=%s model_id=%s task_id=%s "
                    "duration_ms=%s raw_bytes=%s disk=%s memory=%s nic=%s gpu=%s",
                    self.host,
                    self.model_id,
                    self.collection_task_id,
                    round((time.monotonic() - started_at) * 1000, 2),
                    len(data.get("result") or ""),
                    len(disk_info),
                    len(mem_info),
                    len(nic_info),
                    len(gpu_info),
                )
            else:
                # 远端脚本失败时 result 是 {"cmdb_collect_error": ...}，这里只补可见性，不改变既有返回结构。
                remote_result = data.get("result")
                logger.debug(
                    "event=physcial_server_sync_target_empty transport=ssh host=%s model_id=%s task_id=%s "
                    "duration_ms=%s raw_bytes=%s remote_success=%s remote_error=%s",
                    self.host,
                    self.model_id,
                    self.collection_task_id,
                    round((time.monotonic() - started_at) * 1000, 2),
                    len(remote_result) if isinstance(remote_result, str) else 0,
                    data.get("success"),
                    str(remote_result.get("cmdb_collect_error") or "") if isinstance(remote_result, dict) else "",
                )
                result = {"success": True, "result": {model_id: [{}]}}
        except Exception as err:
            logger.exception(
                "event=physcial_server_collect_failed host=%s model_id=%s task_id=%s failed_stage=%s error_type=%s",
                self.host,
                self.model_id,
                self.collection_task_id,
                "list_all_resources",
                type(err).__name__,
            )
            result = {"result": {"cmdb_collect_error": str(err)}, "success": False}
        return result


# pyghmi 的 Command 要求 privlevel 为整数，而 CMDB 凭据里的 privilege 通常是字符串名称。
_IPMI_PRIVILEGE_MAP = {
    "callback": 1,
    "user": 2,
    "operator": 3,
    "administrator": 4,
}


def _resolve_ipmi_privilege(privilege) -> int:
    """把 IPMI 权限名称/数字字符串解析为 pyghmi 需要的整数 privlevel。"""
    if isinstance(privilege, int):
        return privilege
    text = str(privilege or "administrator").strip().lower()
    if text.isdigit():
        return int(text)
    return _IPMI_PRIVILEGE_MAP.get(text, _IPMI_PRIVILEGE_MAP["administrator"])


def _coerce_ipmi_port(raw, default: int = 623) -> int:
    """表单留空时 header 会把 port 打成 ""，int("") 会直接炸掉 __init__。"""
    if raw in (None, ""):
        return default
    port = int(raw)
    if not 1 <= port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    return port


def _coerce_ipmi_timeout(raw, default: int = 30) -> float:
    """单目标 IPMI 会话超时；pyghmi 自身不暴露超时参数，只能在外层掐断。"""
    if raw in (None, ""):
        return float(default)
    timeout = float(raw)
    if timeout <= 0:
        return float(default)
    return timeout


# 只要有一项非空即认为 FRU 采集有效，避免"成功但全空"被当成采集成功。
_IPMI_IDENTITY_FIELDS = (
    "serial_number",
    "model",
    "brand",
    "asset_code",
    "board_vendor",
    "board_model",
    "board_serial",
)


def _extract_system_inventory(inventory: dict) -> dict:
    """取出 System 段；部分 pyghmi/厂商版本 key 大小写或层级不同。"""
    for key in ("System", "system", "SYSTEM"):
        section = inventory.get(key)
        if isinstance(section, dict) and section:
            return section
    return {}


class PhyscialServerIPMIInfo:
    def __init__(self, kwargs):
        # SSH 路径优先用 connect_ip（NAT/跳板场景），IPMI 也要跟随同一个可达地址。
        self.host = str(kwargs.get("host") or kwargs.get("connect_ip") or "").strip()
        self.port = _coerce_ipmi_port(kwargs.get("port"))
        self.username = kwargs.get("username") or kwargs.get("user") or kwargs.get("userid") or ""
        self.password = kwargs.get("password") or kwargs.get("passwd") or ""
        self.privilege = _resolve_ipmi_privilege(kwargs.get("privilege"))
        self.timeout = _coerce_ipmi_timeout(kwargs.get("timeout"))
        self.model_id = kwargs.get("model_id", "physcial_server")
        self.collection_task_id = kwargs.get("collection_task_id") or kwargs.get("task_id") or ""

    async def list_all_resources(self):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(self._list_all_resources_sync),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            # to_thread 无法真正取消底层 socket，只能先让目标失败，避免整批任务被拖死。
            logger.error(
                "event=physcial_server_sync_target_failed transport=ipmi host=%s port=%s model_id=%s task_id=%s "
                "privilege=%s failed_stage=ipmi_timeout error_type=TimeoutError action=fail",
                self.host,
                self.port,
                self.model_id,
                self.collection_task_id,
                self.privilege,
            )
            return {
                "result": {"cmdb_collect_error": f"IPMI 采集超时: {self.host}:{self.port} 在 {self.timeout}s 内未返回"},
                "success": False,
            }

    def _list_all_resources_sync(self):
        started_at = time.monotonic()
        try:
            try:
                from pyghmi.ipmi.command import Command
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError("pyghmi is required for physcial_server IPMI collection") from exc

            logger.debug(
                "event=physcial_server_ipmi_collect_started host=%s port=%s model_id=%s task_id=%s privilege=%s",
                self.host,
                self.port,
                self.model_id,
                self.collection_task_id,
                self.privilege,
            )

            # 这里只做带外 identity / FRU 风格数据采集，不尝试补齐 SSH 路径负责的 disk/memory/nic/gpu。
            if not self.host:
                raise ValueError("physcial_server IPMI 采集缺少目标地址（host/connect_ip 均为空）")
            if not self.username or not self.password:
                raise ValueError("physcial_server IPMI 采集缺少用户名或密码（检查凭据绑定与环境变量注入）")

            command = Command(
                bmc=self.host,
                userid=self.username,
                password=self.password,
                port=self.port,
                privlevel=self.privilege,
            )
            inventory = dict(command.get_inventory() or {})
            system_info = _extract_system_inventory(inventory)
            if not system_info:
                # 静默返回空 item 会让 CMDB 写入一条全空记录，比直接失败更难排查。
                raise RuntimeError(f"IPMI inventory 未返回 System 段: sections={sorted(inventory)}")

            result_data = {
                # 返回字段直接对齐 physcial_server IPMI 白名单，便于后续 formatter 做最小映射。
                "ip_addr": self.host,
                "port": self.port,
                "serial_number": first_value(system_info, "Serial Number", "Product Serial Number"),
                "model": first_value(system_info, "Model", "Product name", "Product Name"),
                "brand": first_value(system_info, "Manufacturer", "Product Manufacturer"),
                # IPMI FRU 规范里资产标签叫 Asset Tag，pyghmi 少数版本才给 Asset Number。
                "asset_code": first_value(system_info, "Asset Number", "Asset Tag", "Asset tag"),
                "board_vendor": first_value(system_info, "Board manufacturer", "Board Manufacturer"),
                "board_model": first_value(system_info, "Board model", "Board product name", "Board Product Name"),
                "board_serial": first_value(system_info, "Board serial number", "Board Serial Number"),
                "raw_payload": json.dumps(system_info, ensure_ascii=False, default=str),
            }
            if not any(result_data.get(field) for field in _IPMI_IDENTITY_FIELDS):
                # 有 System 段但一个身份字段都没命中，说明键名与当前 pyghmi/BMC 不匹配，必须显式失败。
                raise RuntimeError(
                    "IPMI inventory 未解析到任何身份字段: "
                    f"sections={sorted(inventory)} keys={sorted(system_info)}"
                )
            logger.debug(
                "event=physcial_server_sync_target_succeeded transport=ipmi host=%s port=%s model_id=%s task_id=%s "
                "privilege=%s duration_ms=%s inventory_sections=%s inventory_fields=%s serial_number=%s",
                self.host,
                self.port,
                self.model_id,
                self.collection_task_id,
                self.privilege,
                round((time.monotonic() - started_at) * 1000, 2),
                sorted(inventory),
                len(system_info),
                result_data["serial_number"],
            )
            return {
                "success": True,
                "result": {
                    self.model_id: [result_data],
                },
            }
        except Exception as err:  # noqa: BLE001
            # pyghmi 的异常常为空消息（str(err) == "None"），必须带上目标、权限和异常类型才可定位。
            logger.error(
                "event=physcial_server_sync_target_failed transport=ipmi host=%s port=%s model_id=%s task_id=%s "
                "privilege=%s failed_stage=ipmi_inventory error_type=%s error=%s duration_ms=%s",
                self.host,
                self.port,
                self.model_id,
                self.collection_task_id,
                self.privilege,
                type(err).__name__,
                err,
                round((time.monotonic() - started_at) * 1000, 2),
                exc_info=True,
            )
            return {"result": {"cmdb_collect_error": str(err) or type(err).__name__}, "success": False}


def first_value(data: Dict[str, Any], *keys: str):
    """按候选字段顺序取第一个非空值，用于兼容不同厂商 FRU/inventory 返回的字段名差异。"""
    for key in keys:
        value = data.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        text = str(value).strip()
        if text:
            return text
    return ""
