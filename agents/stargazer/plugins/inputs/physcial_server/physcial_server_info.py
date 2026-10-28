# -- coding: utf-8 --
# @File: physcial_server_info.py.py
# @Time: 2025/12/25 11:14
# @Author: windyzhao
import asyncio
import json
import shutil
import subprocess
import time
from typing import Any, Dict, Optional, Tuple

from core.collection.contracts import AccessProbeResult, AccessProbeStatus
from plugins.inputs.physcial_server.redfish_info import PhyscialServerRedfishInfo
from plugins.inputs.physcial_server.server_info_parse import parse_server_info
from plugins.script_executor import SSHPlugin
from sanic.log import logger


_IPMI_PRIVILEGE_MAP = {
    "callback": "CALLBACK",
    "user": "USER",
    "operator": "OPERATOR",
    "administrator": "ADMINISTRATOR",
}


def _coerce_privilege(privilege: Any) -> str:
    """把字符串/数字 privilege 转成 ipmitool -L 级别字符串；未指定或非法值返回 USER。"""
    if privilege is None or str(privilege).strip() == "":
        return "USER"
    token = str(privilege).lower().strip()
    if token in _IPMI_PRIVILEGE_MAP:
        return _IPMI_PRIVILEGE_MAP[token]
    try:
        value = int(token)
    except (TypeError, ValueError):
        return "USER"
    for name, level in ((1, "CALLBACK"), (2, "USER"), (3, "OPERATOR"), (4, "ADMINISTRATOR")):
        if value == name:
            return level
    return "USER"


def _classify_ipmitool_error(err: Exception) -> tuple[str, str]:
    """把 ipmitool 异常映射成 (stage, summary)，用于 probe/collect 统一错误码。"""
    err_str = str(err).lower().strip()
    if not err_str or err_str == "none":
        return "timeout", "IPMI 无响应（BMC 不可达、UDP 623 被拦截或目标 IP/端口错误）"
    if "timeout" in err_str or "timed out" in err_str:
        return "timeout", f"IPMI 连接超时: {err}"
    if (
        "authentication" in err_str
        or "credential" in err_str
        or "unauthorized" in err_str
        or "password" in err_str
        or "privilege" in err_str
        or "invalid user" in err_str
        or "wrong password" in err_str
        or "rakp" in err_str
        or "integrity code" in err_str
        or "kg" in err_str
    ):
        return "auth", f"IPMI 认证失败: {err}"
    if (
        "connection refused" in err_str
        or "unreachable" in err_str
        or "no route" in err_str
        or "unable to transmit" in err_str
        or "unable to establish ipmi" in err_str
        or "unable to establish rmcp" in err_str
    ):
        return "connect", f"IPMI 连接失败: {err}"
    return "unknown", f"IPMI 未知错误: {err}"


_VENDOR_FRU_FIELD_MAP = {
    "default": {
        "serial_number": ["Serial Number", "Product Serial", "Chassis serial number"],
        "model": ["Model", "Product Name"],
        "brand": ["Manufacturer", "Product Manufacturer"],
        "asset_code": ["Asset Number", "Product Asset Tag"],
        "board_vendor": ["Board manufacturer", "Board Mfg"],
        "board_model": ["Board model", "Board product name", "Board Product"],
        "board_serial": ["Board serial number", "Board Serial"],
    },
    "lenovo": {
        "model": ["Model", "Product Name"],
        "brand": ["Manufacturer", "Product Manufacturer"],
    },
    "dell": {
        "serial_number": ["Serial Number"],
        "model": ["Product Name"],
        "brand": ["Manufacturer"],
    },
    "hp": {
        "serial_number": ["Serial Number"],
        "model": ["Product Name"],
        "brand": ["Manufacturer"],
    },
    "hpe": {
        "serial_number": ["Serial Number"],
        "model": ["Product Name"],
        "brand": ["Manufacturer"],
    },
}


def first_value(data: Dict[str, Any], *keys: str) -> str:
    """按候选字段顺序取第一个非空值，并去除字符串首尾空格；用于兼容不同厂商 FRU/inventory 返回的字段名差异。"""
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            if isinstance(value, str):
                return value.strip()
            return str(value)
    return ""


def _detect_vendor(system_info: Dict[str, Any]) -> str:
    """根据 Manufacturer / Product Manufacturer 等字段识别厂商关键字。"""
    brand = first_value(
        system_info,
        "Manufacturer",
        "Product Manufacturer",
        "Board manufacturer",
        "Board Mfg",
    ).lower()
    for vendor in _VENDOR_FRU_FIELD_MAP:
        if vendor != "default" and vendor in brand:
            return vendor
    return "default"


def _get_fru_field(system_info: Dict[str, Any], field: str, vendor: str) -> str:
    """按厂商返回对应的 FRU 字段候选列表；厂商未覆盖的字段使用 default。"""
    candidates = _VENDOR_FRU_FIELD_MAP.get(vendor, {}).get(field)
    if not candidates:
        candidates = _VENDOR_FRU_FIELD_MAP["default"][field]
    return first_value(system_info, *candidates)


class PhyscialServerProtocolInfo:
    """物理服务器带外协议入口；旧任务默认走 IPMI。"""

    def __init__(self, kwargs, *, transport=None):
        protocol = str(kwargs.get("collection_protocol") or "ipmi").strip().lower()
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
            return AccessProbeResult(status=AccessProbeStatus.NOT_SUPPORTED)
        return await probe()


class PhyscialServerInfo(SSHPlugin):
    async def list_all_resources(self, need_raw=False):
        """
        Convert collected data to a standard format.
        """
        logger.error(
            "event=physcial_server_ssh_collect_entry host=%s model_id=%s marker=SSH_PATH",
            self.host,
            self.model_id,
        )
        model_id = self.model_id or "physcial_server"
        try:
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
            else:
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


class PhyscialServerIPMIInfo:
    def __init__(self, kwargs):
        self.host = kwargs.get("host", "")
        self.port = int(kwargs.get("port", 623))
        self.username = kwargs.get("username", kwargs.get("user", ""))
        self.password = kwargs.get("password", "")
        self.requested_privilege = kwargs.get("privilege")
        self.privilege = _coerce_privilege(self.requested_privilege)
        self.model_id = kwargs.get("model_id", "physcial_server")

    def _run_ipmitool(self, *args: str, timeout: int = 30) -> str:
        """执行 ipmitool 子命令并返回 stdout。"""
        if not shutil.which("ipmitool"):
            raise RuntimeError("ipmitool not found in PATH")
        cmd = [
            "ipmitool",
            "-I",
            "lanplus",
            "-H",
            self.host,
            "-U",
            self.username,
            "-P",
            self.password,
            "-L",
            self.privilege,
            "-p",
            str(self.port),
        ] + list(args)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"ipmitool timeout after {exc.timeout}s") from exc
        # ipmitool 有时部分 FRU 读取失败导致非零退出，但 stdout 仍有有效数据。
        if result.returncode != 0 and "FRU Device Description" not in result.stdout:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip())
        return result.stdout

    async def probe(self):
        """带凭据轻量探测：ipmitool 查询电源状态，用于自动发现先验连接性。"""
        return await asyncio.to_thread(self._probe_sync)

    def _probe_sync(self):
        logger.info(
            "event=physical_server_ipmi_probe_start host=%s port=%s username=%s configured_privilege=%s",
            self.host,
            self.port,
            self.username,
            self.requested_privilege,
        )
        try:
            output = self._run_ipmitool("chassis", "power", "status")
            power_state = output.strip().replace("Chassis Power is", "").strip()
            logger.info(
                "event=physical_server_ipmi_probe_success host=%s port=%s effective_privilege=%s power_state=%s",
                self.host,
                self.port,
                self.privilege,
                power_state,
            )
            return AccessProbeResult(
                status=AccessProbeStatus.READY,
                evidence={"power_state": power_state, "effective_privilege": self.privilege},
            )
        except Exception as err:  # noqa: BLE001
            stage, summary = _classify_ipmitool_error(err)
            status_map = {
                "timeout": AccessProbeStatus.NO_RESPONSE,
                "auth": AccessProbeStatus.AUTH_FAILED,
                "connect": AccessProbeStatus.TARGET_UNREACHABLE,
            }
            logger.error(
                "event=physical_server_ipmi_probe_failed host=%s port=%s username=%s error_type=%s stage=%s error_msg=%s detail=%s",
                self.host,
                self.port,
                self.username,
                type(err).__name__,
                stage,
                err,
                summary,
                exc_info=(stage == "unknown"),
            )
            return AccessProbeResult(
                status=status_map.get(stage, AccessProbeStatus.MISCONFIGURED),
                error_code=f"ipmi_probe_{stage}",
                detail=summary,
            )

    async def list_all_resources(self):
        return await asyncio.to_thread(self._list_all_resources_sync)

    def _get_fru_data_via_ipmitool(self) -> Tuple[Dict[str, Any], Dict[str, Dict[str, str]]]:
        """通过 ipmitool 读取 FRU；返回 (system_info, all_frus)。"""
        output = self._run_ipmitool("fru", "print")
        frus = self._parse_ipmitool_fru(output)
        return self._pick_system_fru(frus), frus

    def _get_power_state_via_ipmitool(self) -> str:
        """通过 ipmitool 查询电源状态。"""
        try:
            output = self._run_ipmitool("chassis", "power", "status")
            return output.strip().replace("Chassis Power is", "").strip()
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "event=physical_server_ipmi_power_state_failed host=%s error_type=%s error_msg=%s",
                self.host,
                type(err).__name__,
                err,
            )
            return ""

    def _get_health_via_sel(self) -> str:
        """通过 ipmitool SEL 判断健康状态；发现 Critical/Non-Recoverable 事件则标记异常。"""
        try:
            output = self._run_ipmitool("sel", "list")
            if not output.strip():
                return "OK"
            for line in output.splitlines():
                lower = line.lower()
                if any(k in lower for k in ("critical", "non-recoverable", "fatal", "failure")):
                    return "Critical"
            return "OK"
        except Exception as err:  # noqa: BLE001
            logger.warning(
                "event=physical_server_ipmi_health_failed host=%s error_type=%s error_msg=%s",
                self.host,
                type(err).__name__,
                err,
            )
            return "Unknown"

    @staticmethod
    def _extract_cpu_info(frus: Dict[str, Dict[str, str]]) -> Dict[str, str]:
        """从 FRU 记录中提取 CPU 信息；不同品牌 FRU 命名可能不同。"""
        cpu_info: Dict[str, str] = {"cpu_vendor": "", "cpu_model": "", "cpu_core": "", "cpu_arch": ""}
        cpu_count = 0
        for name, data in frus.items():
            # 常见命名：CPU 1, CPU 2, Processor, CPU FRU 等
            if not any(k in name.upper() for k in ("CPU", "PROCESSOR")):
                continue
            cpu_count += 1
            vendor = first_value(data, "Product Manufacturer", "Manufacturer")
            model = first_value(data, "Product Name", "Model")
            if not cpu_info["cpu_vendor"] and vendor:
                cpu_info["cpu_vendor"] = vendor
            if not cpu_info["cpu_model"] and model:
                cpu_info["cpu_model"] = model
        if cpu_count:
            cpu_info["cpu_core"] = str(cpu_count)
        # IPMI 拿不到架构，按型号简单推断
        model_lower = cpu_info["cpu_model"].lower()
        if any(k in model_lower for k in ("xeon", "epyc", "ryzen", "core")):
            cpu_info["cpu_arch"] = "x86_64"
        elif any(k in model_lower for k in ("aarch64", "arm")):
            cpu_info["cpu_arch"] = "aarch64"
        return cpu_info

    @staticmethod
    def _extract_memory_info(frus: Dict[str, Dict[str, str]]) -> list[Dict[str, str]]:
        """从 FRU 记录中提取内存信息；返回内存对象列表。"""
        memories: list[Dict[str, str]] = []
        for name, data in frus.items():
            upper = name.upper()
            if not any(k in upper for k in ("DIMM", "MEMORY", "RAM")):
                continue
            memories.append({
                "self_device": "",  # 由调用方填充
                "manufacturer": first_value(data, "Product Manufacturer", "Manufacturer"),
                "model": first_value(data, "Product Name", "Model"),
                "part_number": first_value(data, "Product Part Number", "Part Number"),
                "sn": first_value(data, "Product Serial", "Serial Number"),
                "capacity": "",
            })
        return memories

    @staticmethod
    def _parse_ipmitool_fru(output: str) -> Dict[str, Dict[str, str]]:
        """解析 'ipmitool fru print' 文本输出为 {fru_name: {key: value}}。"""
        frus: Dict[str, Dict[str, str]] = {}
        current_name: Optional[str] = None
        current_data: Dict[str, str] = {}
        for line in output.splitlines():
            stripped = line.rstrip()
            if not stripped.strip():
                continue
            if stripped.startswith("FRU Device Description"):
                if current_name is not None:
                    frus[current_name] = current_data
                current_name = stripped.split(":", 1)[1].strip()
                current_data = {}
            elif current_name is not None and ":" in stripped:
                key, value = stripped.split(":", 1)
                current_data[key.strip()] = value.strip()
        if current_name is not None:
            frus[current_name] = current_data
        return frus

    @staticmethod
    def _pick_system_fru(frus: Dict[str, Dict[str, str]]) -> Dict[str, str]:
        """从多个 FRU 设备中挑选包含产品信息的 System FRU。"""
        preferred = ("System", "Builtin FRU Device", "Baseboard", "Chassis")
        for name in preferred:
            if name in frus:
                return frus[name]
        for name, data in frus.items():
            if data.get("Product Name") or data.get("Product Manufacturer"):
                return data
        return next(iter(frus.values()), {}) if frus else {}

    def _format_ipmi_result(
        self,
        system_info: Dict[str, Any],
        cpu_info: Dict[str, str],
        power_state: str,
        health: str,
        memories: list[Dict[str, str]],
    ) -> Dict[str, Any]:
        """把 System FRU 信息格式化为 CMDB 标准字段；按厂商选择字段名。"""
        vendor = _detect_vendor(system_info)
        result_data = {
            "inst_name": self.host,
            "organization": "",
            "ip_addr": self.host,
            "serial_number": _get_fru_field(system_info, "serial_number", vendor),
            "model": _get_fru_field(system_info, "model", vendor),
            "brand": _get_fru_field(system_info, "brand", vendor),
            "asset_code": _get_fru_field(system_info, "asset_code", vendor),
            "cpu_vendor": cpu_info.get("cpu_vendor", ""),
            "cpu_model": cpu_info.get("cpu_model", ""),
            "cpu_core": cpu_info.get("cpu_core", ""),
            "cpu_threads": "",
            "cpu_arch": cpu_info.get("cpu_arch", ""),
            "board_vendor": _get_fru_field(system_info, "board_vendor", vendor),
            "board_model": _get_fru_field(system_info, "board_model", vendor),
            "board_serial": _get_fru_field(system_info, "board_serial", vendor),
            "power_state": power_state,
            "health": health,
            "raw_payload": json.dumps(
                system_info,
                ensure_ascii=False,
                default=lambda o: o.hex() if isinstance(o, (bytes, bytearray)) else str(o),
            ),
        }
        return {
            "success": True,
            "result": {
                self.model_id: [result_data],
                "memory": [{**mem, "self_device": self.host} for mem in memories],
            },
        }

    def _list_all_resources_sync(self):
        start_ts = time.time()
        logger.info(
            "event=physical_server_ipmi_collect_entry host=%s model_id=%s marker=IPMI_PATH",
            self.host,
            self.model_id,
        )
        try:
            step_ts = time.time()
            system_info, all_frus = self._get_fru_data_via_ipmitool()
            cpu_info = self._extract_cpu_info(all_frus)
            memories = self._extract_memory_info(all_frus)
            power_state = self._get_power_state_via_ipmitool()
            health = self._get_health_via_sel()
            logger.info(
                "event=physical_server_ipmi_collect_step host=%s step=read_fru_via_ipmitool elapsed_ms=%.0f fru_items=%s memory_count=%s",
                self.host,
                (time.time() - step_ts) * 1000,
                list(all_frus.keys()),
                len(memories),
            )
            if not system_info:
                return {
                    "result": {"cmdb_collect_error": "unable to retrieve system FRU via ipmitool"},
                    "success": False,
                }

            step_ts = time.time()
            result = self._format_ipmi_result(system_info, cpu_info, power_state, health, memories)
            logger.info(
                "event=physical_server_ipmi_collect_step host=%s step=format_result elapsed_ms=%.0f total_elapsed_ms=%.0f",
                self.host,
                (time.time() - step_ts) * 1000,
                (time.time() - start_ts) * 1000,
            )
            return result
        except Exception as err:  # noqa: BLE001
            stage, summary = _classify_ipmitool_error(err)
            logger.error(
                "event=physical_server_ipmi_collect_failed host=%s model_id=%s error_type=%s stage=%s error_msg=%s detail=%s total_elapsed_ms=%.0f",
                self.host,
                self.model_id,
                type(err).__name__,
                stage,
                err,
                summary,
                (time.time() - start_ts) * 1000,
                exc_info=(stage == "unknown"),
            )
            return {"result": {"cmdb_collect_error": str(err)}, "success": False}
