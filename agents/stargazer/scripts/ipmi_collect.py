#!/usr/bin/env python3
"""独立 IPMI 采集脚本（不启动 Stargazer 服务）。

只依赖 ``pyghmi`` 与标准库：

    pip install 'pyghmi>=1.6.5'
    python ipmi_collect.py --host 10.0.0.11 --username admin --password 'xxx'

输出的字段与 Stargazer ``physcial_server_ipmi`` 插件完全一致
（``plugins/inputs/physcial_server/physcial_server_info.py``）:
``ip_addr`` / ``port`` / ``serial_number`` / ``model`` / ``brand`` /
``asset_code`` / ``board_vendor`` / ``board_model`` / ``board_serial`` /
``raw_payload``。

三种输出格式：

* ``stargazer``（默认）: ``{"success": true, "result": {"physcial_server": [...]}}``
  多目标时输出 ``{"results": [...]}``，可当插件返回值直接比对。
* ``cmdb``: ``{"model_id": "physcial_server", "items": [...]}``，即
  ``POST /openapi/v1/cmdb/instance-batch-create`` 的请求体；配合 ``--push``
  可直接写入 CMDB。``raw_payload`` 不是模型字段，此格式下会被剔除。
* ``jsonl``: 每行一个实例对象，便于落盘后二次处理。

注意：pyghmi 的 ``Command`` 没有超时参数（见
``specs/changes/cmdb-collection-timeout-and-ip-precheck/spec.md``），
脚本用 ``--timeout`` 控制主线程等待时间；超时目标会被记为失败，
底层会话线程可能仍在后台重试，直到进程退出。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

MODEL_ID = "physcial_server"
DEFAULT_PORT = 623
DEFAULT_PRIVILEGE = "administrator"
DEFAULT_WORKERS = 8
DEFAULT_TIMEOUT = 30
PUSH_BATCH_SIZE = 100

# 与 physcial_server_info.py 的 _IPMI_PRIVILEGE_MAP 保持一致。
_PRIVILEGE_MAP = {
    "callback": 1,
    "user": 2,
    "operator": 3,
    "administrator": 4,
}

# get_inventory()["System"] 在不同厂商下的候选字段名。
_SYSTEM_FIELDS = {
    "serial_number": ("Serial Number",),
    "model": ("Model", "Product name"),
    "brand": ("Manufacturer",),
    "asset_code": ("Asset Number",),
    "board_vendor": ("Board manufacturer",),
    "board_model": ("Board model", "Board product name"),
    "board_serial": ("Board serial number",),
}


@dataclass(frozen=True)
class Target:
    host: str
    port: int = DEFAULT_PORT


def resolve_privilege(value: Any) -> int:
    """把权限名称/数字字符串解析成 pyghmi 需要的整数 privlevel。"""
    if isinstance(value, int):
        return value
    text = str(value or DEFAULT_PRIVILEGE).strip().lower()
    if text.isdigit():
        return int(text)
    return _PRIVILEGE_MAP.get(text, _PRIVILEGE_MAP[DEFAULT_PRIVILEGE])


def first_value(data: Dict[str, Any], *keys: str) -> str:
    """按候选字段顺序取第一个非空值，兼容不同厂商的 FRU 返回。"""
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def normalize_power_state(value: Any) -> str:
    """把 pyghmi 的 on/off 规范成 CMDB 用的 On/Off。"""
    token = str(value or "").strip().lower()
    if token in ("on", "off"):
        return token.capitalize()
    return str(value or "").strip()


def summarize_health(payload: Any) -> str:
    """把 get_health() 的子系统状态收敛成一个总体状态。"""
    if not isinstance(payload, dict):
        return ""
    entries = payload.get("health") or []
    statuses = []
    for entry in entries:
        if isinstance(entry, dict):
            token = str(entry.get("Status") or "").strip().lower()
            if token:
                statuses.append(token)
    if not statuses:
        return ""
    if any("critical" in token for token in statuses):
        return "Critical"
    if all(token == "ok" for token in statuses):
        return "OK"
    return "Warning"


def read_power_health(command: Any) -> Dict[str, str]:
    """可选读取电源/健康状态；失败时保持字段缺失，不影响身份采集。"""
    result: Dict[str, str] = {}
    try:
        power = command.get_power() or {}
        state = normalize_power_state(power.get("powerstate") if isinstance(power, dict) else "")
        if state:
            result["power_state"] = state
    except Exception:  # noqa: BLE001 - 部分 BMC 不支持电源查询
        pass
    try:
        health = summarize_health(command.get_health())
        if health:
            result["health"] = health
    except Exception:  # noqa: BLE001 - 部分 BMC 不支持健康查询
        pass
    return result


def build_item(target: Target, system_info: Dict[str, Any]) -> Dict[str, Any]:
    """把 get_inventory()["System"] 映射成 CMDB IPMI 插件同款字段。"""
    item: Dict[str, Any] = {
        # inst_name 与 CMDB 的 resolve_physical_server_inst_name 一致：只取采集目标 IP。
        "inst_name": target.host,
        "ip_addr": target.host,
        "port": target.port,
    }
    for field, keys in _SYSTEM_FIELDS.items():
        item[field] = first_value(system_info, *keys)
    item["raw_payload"] = json.dumps(system_info, ensure_ascii=False)
    return item


def collect_target(
    target: Target,
    username: str,
    password: str,
    privilege: int,
    with_power_health: bool = False,
) -> Dict[str, Any]:
    """同步采集单个 BMC，返回 ``{"success": bool, "item": {...}}`` 结果字典。"""
    base = {"host": target.host, "port": target.port}
    try:
        from pyghmi.ipmi.command import Command
    except Exception as exc:  # noqa: BLE001
        return {
            **base,
            "success": False,
            "error": f"ImportError: pyghmi is required ({exc})",
        }

    try:
        command = Command(
            bmc=target.host,
            userid=username,
            password=password,
            port=target.port,
            privlevel=privilege,
        )
        inventory = command.get_inventory()
        system_info = dict(inventory.get("System") or {}) if isinstance(inventory, dict) else {}
        item = build_item(target, system_info)
        if with_power_health:
            item.update(read_power_health(command))
        return {**base, "success": True, "item": item, "inventory_sections": sorted(inventory.keys()) if isinstance(inventory, dict) else []}
    except Exception as err:  # noqa: BLE001 - pyghmi 异常常为空消息
        return {
            **base,
            "success": False,
            "error": f"{type(err).__name__}: {str(err) or 'no message'}",
        }


def collect_target_guarded(
    target: Target,
    username: str,
    password: str,
    privilege: int,
    with_power_health: bool,
    timeout: int,
) -> Dict[str, Any]:
    """用 daemon 线程包住 pyghmi 调用，避免单个目标卡死整批。"""
    holder: Dict[str, Any] = {}

    def run() -> None:
        holder["value"] = collect_target(target, username, password, privilege, with_power_health)

    worker = threading.Thread(target=run, name=f"ipmi-{target.host}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        return {
            "host": target.host,
            "port": target.port,
            "success": False,
            "error": f"Timeout: {timeout}s 内未返回（pyghmi 无内置超时）",
        }
    return holder.get("value") or {
        "host": target.host,
        "port": target.port,
        "success": False,
        "error": "Unknown: 采集线程未返回结果",
    }


def load_targets(hosts: Optional[List[str]], hosts_file: Optional[str], default_port: int) -> List[Target]:
    """解析 ``--host``（可逗号分隔）与 ``--hosts-file``（每行 host[:port]）。"""
    seen = set()
    targets: List[Target] = []

    def add(raw: str) -> None:
        text = str(raw or "").strip()
        if not text:
            return
        host, port = text, default_port
        if text.count(":") == 1:
            host_part, port_part = text.rsplit(":", 1)
            if port_part.isdigit():
                host, port = host_part.strip(), int(port_part)
        if not host:
            return
        key = (host.lower(), port)
        if key in seen:
            return
        seen.add(key)
        targets.append(Target(host=host, port=port))

    for entry in hosts or []:
        for part in str(entry).split(","):
            add(part)
    if hosts_file:
        with open(hosts_file, encoding="utf-8") as handle:
            for line in handle:
                cleaned = line.split("#", 1)[0].strip()
                if cleaned:
                    add(cleaned)
    return targets


def build_stargazer_payload(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    """对齐插件返回值：单目标与原插件一致，多目标用 results 数组。"""
    if len(results) == 1:
        only = results[0]
        if only.get("success"):
            return {"success": True, "result": {MODEL_ID: [only["item"]]}}
        return {"success": False, "result": {"cmdb_collect_error": only.get("error", "")}}

    rows = []
    for result in results:
        row: Dict[str, Any] = {
            "host": result["host"],
            "port": result["port"],
            "success": bool(result.get("success")),
        }
        if result.get("success"):
            row["result"] = {MODEL_ID: [result["item"]]}
        else:
            row["error"] = result.get("error", "")
        rows.append(row)
    return {"results": rows}


def build_cmdb_items(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """生成 batch_create 可用实例：去掉模型里不存在的 raw_payload 与空值。"""
    items = []
    for result in results:
        if not result.get("success"):
            continue
        item = {
            key: value
            for key, value in (result.get("item") or {}).items()
            if key != "raw_payload" and value not in (None, "")
        }
        items.append(item)
    return items


def push_to_cmdb(base_url: str, token: str, items: List[Dict[str, Any]], timeout: int = 60) -> int:
    """按 100 条一批写入 CMDB；返回成功提交的条数。"""
    url = base_url.rstrip("/") + "/openapi/v1/cmdb/instance-batch-create"
    pushed = 0
    for start in range(0, len(items), PUSH_BATCH_SIZE):
        chunk = items[start : start + PUSH_BATCH_SIZE]
        payload = json.dumps({"model_id": MODEL_ID, "items": chunk}, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
                "Api-Authorization": token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
                print(
                    f"[push] {start + len(chunk)}/{len(items)} status={response.status} body={body[:200]}",
                    file=sys.stderr,
                )
                pushed += len(chunk)
        except urllib.error.HTTPError as err:
            body = err.read().decode("utf-8", errors="replace")
            print(f"[push] batch {start} failed: HTTP {err.code} {body[:400]}", file=sys.stderr)
        except Exception as err:  # noqa: BLE001 - 单批失败不阻断其余批次
            print(f"[push] batch {start} failed: {type(err).__name__}: {err}", file=sys.stderr)
    return pushed


def render(results: List[Dict[str, Any]], output_format: str) -> str:
    if output_format == "jsonl":
        return "\n".join(json.dumps(item, ensure_ascii=False) for item in build_cmdb_items(results))
    if output_format == "cmdb":
        payload: Any = {"model_id": MODEL_ID, "items": build_cmdb_items(results)}
    else:
        payload = build_stargazer_payload(results)
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="独立 IPMI 采集脚本，输出对齐 CMDB physcial_server 模型",
    )
    parser.add_argument("--host", action="append", help="BMC 地址，支持逗号分隔与 host:port，可重复")
    parser.add_argument("--hosts-file", help="目标文件，每行 host 或 host:port，# 后为注释")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="默认 IPMI 端口（默认 623）")
    parser.add_argument(
        "--username",
        default=os.environ.get("IPMI_USERNAME", ""),
        help="IPMI 用户名，默认读 IPMI_USERNAME",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("IPMI_PASSWORD"),
        help="IPMI 密码，默认读 IPMI_PASSWORD",
    )
    parser.add_argument(
        "--privilege",
        default=DEFAULT_PRIVILEGE,
        help="callback/user/operator/administrator 或 1-4",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS, help="并发目标数（默认 8）")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="单目标等待秒数（默认 30）")
    parser.add_argument(
        "--format",
        choices=("stargazer", "cmdb", "jsonl"),
        default="stargazer",
        help="输出格式",
    )
    parser.add_argument(
        "--with-power-health",
        action="store_true",
        help="额外采集 power_state/health（部分 BMC 不支持）",
    )
    parser.add_argument("--output", help="写入文件而不是 stdout")
    parser.add_argument(
        "--push",
        metavar="BASE_URL",
        help="把 cmdb items POST 到网关，如 https://bk-lite.example.com",
    )
    parser.add_argument(
        "--api-token",
        default=os.environ.get("CMDB_API_TOKEN"),
        help="CMDB API Token，默认读 CMDB_API_TOKEN",
    )
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    targets = load_targets(args.host, args.hosts_file, args.port)
    if not targets:
        parser.error("至少需要一个 --host 或 --hosts-file")
    if not args.username:
        parser.error("缺少 --username（或环境变量 IPMI_USERNAME）")
    if not args.password:
        parser.error("缺少 --password（或环境变量 IPMI_PASSWORD）")

    privilege = resolve_privilege(args.privilege)

    def run(target: Target) -> Dict[str, Any]:
        return collect_target_guarded(
            target,
            args.username,
            args.password,
            privilege,
            args.with_power_health,
            args.timeout,
        )

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(run, targets))

    text = render(results, args.format)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + ("\n" if text else ""), encoding="utf-8")
    else:
        print(text)

    failed = [result for result in results if not result.get("success")]
    print(
        f"[summary] total={len(results)} success={len(results) - len(failed)} failed={len(failed)}",
        file=sys.stderr,
    )
    for result in failed:
        print(f"[failed] {result['host']}:{result['port']} {result.get('error', '')}", file=sys.stderr)

    if args.push:
        if not args.api_token:
            print("[push] 缺少 --api-token（或 CMDB_API_TOKEN），已跳过", file=sys.stderr)
            return 1
        items = build_cmdb_items(results)
        if args.format != "cmdb":
            print(f"[push] --format={args.format}，推送仍使用 cmdb items 结构", file=sys.stderr)
        pushed = push_to_cmdb(args.push, args.api_token, items)
        print(f"[push] pushed={pushed} total_items={len(items)}", file=sys.stderr)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
