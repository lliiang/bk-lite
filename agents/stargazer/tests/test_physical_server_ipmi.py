import sys
import types
from pathlib import Path

import pytest

STARGAZER_ROOT = Path(__file__).resolve().parents[1]
if str(STARGAZER_ROOT) not in sys.path:
    sys.path.insert(0, str(STARGAZER_ROOT))

from plugins.inputs.physcial_server.physcial_server_info import (  # noqa: E402
    PhyscialServerIPMIInfo,
    _resolve_ipmi_privilege,
)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 4),
        ("administrator", 4),
        ("Administrator", 4),
        ("operator", 3),
        ("user", 2),
        ("callback", 1),
        (4, 4),
        ("4", 4),
        ("unknown", 4),
    ],
)
def test_resolve_ipmi_privilege(raw, expected):
    assert _resolve_ipmi_privilege(raw) == expected


def _install_fake_pyghmi(monkeypatch, fake_command_cls):
    fake_command_module = types.ModuleType("pyghmi.ipmi.command")
    fake_command_module.Command = fake_command_cls
    fake_ipmi_module = types.ModuleType("pyghmi.ipmi")
    fake_pyghmi_module = types.ModuleType("pyghmi")
    monkeypatch.setitem(sys.modules, "pyghmi", fake_pyghmi_module)
    monkeypatch.setitem(sys.modules, "pyghmi.ipmi", fake_ipmi_module)
    monkeypatch.setitem(sys.modules, "pyghmi.ipmi.command", fake_command_module)


def test_ipmi_collector_resolves_privilege_name_to_int(monkeypatch):
    captured = {}

    class FakeCommand:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def get_inventory(self):
            return {
                "System": {
                    "Serial Number": "SN-1",
                    "Manufacturer": "Vendor",
                }
            }

    _install_fake_pyghmi(monkeypatch, FakeCommand)

    collector = PhyscialServerIPMIInfo(
        {
            "host": "10.0.0.1",
            "port": 623,
            "username": "admin",
            "password": "pw",
            "privilege": "administrator",
        }
    )

    result = collector._list_all_resources_sync()

    assert isinstance(captured["privlevel"], int)
    assert captured["privlevel"] == 4
    assert captured["bmc"] == "10.0.0.1"
    assert result["success"] is True
    assert result["result"]["physcial_server"][0]["serial_number"] == "SN-1"


def test_ipmi_collector_keeps_numeric_privilege(monkeypatch):
    captured = {}

    class FakeCommand:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def get_inventory(self):
            return {"System": {}}

    _install_fake_pyghmi(monkeypatch, FakeCommand)

    collector = PhyscialServerIPMIInfo(
        {
            "host": "10.0.0.2",
            "port": 623,
            "username": "admin",
            "password": "pw",
            "privilege": 3,
        }
    )

    result = collector._list_all_resources_sync()

    assert isinstance(captured["privlevel"], int)
    assert captured["privlevel"] == 3
    assert result["success"] is True
