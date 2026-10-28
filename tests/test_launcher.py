from types import SimpleNamespace
from unittest.mock import Mock
import json
import sys
import threading
import urllib.error

import pytest
from repolaunch import __main__ as launcher


def test_health_requires_repolaunch_identity(monkeypatch):
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(launcher.urllib.request, "build_opener", Mock(return_value=opener))
    for payload, expected in [
        ({"application": "repolaunch", "ready": True}, True),
        ({"application": "another-service", "ready": True}, False),
        ({"application": "repolaunch", "ready": False}, False),
        ({"version": "0.1.0"}, False),
        (["repolaunch"], False),
    ]:
        response.read.return_value = json.dumps(payload).encode()
        assert launcher.probe_server(8787) is expected


def test_health_rejects_huge_or_non_json_response(monkeypatch):
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    opener = Mock()
    opener.open.return_value = response
    monkeypatch.setattr(launcher.urllib.request, "build_opener", Mock(return_value=opener))
    for data in [b"x" * 16385, b"<html>Other app</html>"]:
        response.read.return_value = data
        assert launcher.probe_server(8787) is False


def test_health_unreachable_is_not_ready(monkeypatch):
    opener = Mock()
    opener.open.side_effect = urllib.error.URLError("connection refused")
    monkeypatch.setattr(launcher.urllib.request, "build_opener", Mock(return_value=opener))
    assert launcher.probe_server(8787) is False


def test_browser_only_opens_after_positive_readiness(monkeypatch):
    browser = Mock()
    probe = Mock(return_value=True)
    monkeypatch.setattr(launcher, "_open_browser", browser)
    monkeypatch.setattr(launcher, "probe_server", probe)
    launcher._ready_handoff(SimpleNamespace(started=True), 8787, threading.Event(), True)
    browser.assert_called_once_with("http://127.0.0.1:8787")


def test_timeout_does_not_open_browser(monkeypatch):
    browser = Mock()
    monkeypatch.setattr(launcher, "_open_browser", browser)
    launcher._ready_handoff(SimpleNamespace(started=False), 8787, threading.Event(), True, timeout=0)
    browser.assert_not_called()


def test_no_browser_flag_still_checks_health(monkeypatch):
    browser = Mock()
    monkeypatch.setattr(launcher, "_open_browser", browser)
    monkeypatch.setattr(launcher, "probe_server", Mock(return_value=True))
    launcher._ready_handoff(SimpleNamespace(started=True), 8787, threading.Event(), False)
    browser.assert_not_called()


@pytest.fixture
def main_environment(monkeypatch):
    monkeypatch.setattr(launcher, "_configure_logging", Mock())
    monkeypatch.setattr(launcher.time, "sleep", Mock())
    browser = Mock()
    monkeypatch.setattr(launcher, "_open_browser", browser)
    return browser


def test_existing_own_service_is_reused(monkeypatch, main_environment):
    monkeypatch.setattr(launcher, "probe_server", Mock(return_value=True))
    reserve = Mock()
    monkeypatch.setattr(launcher, "reserve_listener", reserve)
    assert launcher.main(["--open-browser"]) == 0
    reserve.assert_not_called()
    main_environment.assert_called_once()


def test_occupied_foreign_port_does_not_open_or_kill(monkeypatch, main_environment):
    monkeypatch.setattr(launcher, "probe_server", Mock(return_value=False))
    monkeypatch.setattr(launcher, "reserve_listener", Mock(side_effect=OSError("occupied")))
    assert launcher.main(["--open-browser"]) == 1
    main_environment.assert_not_called()


def test_simultaneous_own_start_is_reused(monkeypatch, main_environment):
    monkeypatch.setattr(launcher, "probe_server", Mock(side_effect=[False, True]))
    monkeypatch.setattr(launcher, "reserve_listener", Mock(side_effect=OSError("occupied")))
    assert launcher.main(["--open-browser"]) == 0
    main_environment.assert_called_once()


def test_server_receives_reserved_socket_and_cleanup(monkeypatch, main_environment):
    listener = Mock()
    monkeypatch.setattr(launcher, "probe_server", Mock(return_value=False))
    monkeypatch.setattr(launcher, "reserve_listener", Mock(return_value=listener))
    worker = Mock()
    monkeypatch.setattr(launcher.threading, "Thread", Mock(return_value=worker))
    server = Mock(started=True)
    uvicorn = SimpleNamespace(Config=Mock(return_value=object()), Server=Mock(return_value=server))
    monkeypatch.setitem(sys.modules, "uvicorn", uvicorn)
    assert launcher.main(["--no-browser"]) == 0
    server.run.assert_called_once_with(sockets=[listener])
    listener.close.assert_called_once()
    main_environment.assert_not_called()


def test_application_startup_failure_is_not_success(monkeypatch, main_environment):
    listener = Mock()
    monkeypatch.setattr(launcher, "probe_server", Mock(return_value=False))
    monkeypatch.setattr(launcher, "reserve_listener", Mock(return_value=listener))
    worker = Mock()
    monkeypatch.setattr(launcher.threading, "Thread", Mock(return_value=worker))
    server = Mock(started=False)
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(Config=Mock(), Server=Mock(return_value=server)))
    assert launcher.main([]) == 1
    listener.close.assert_called_once()


def test_config_exception_is_reported_and_socket_released(monkeypatch, main_environment):
    listener = Mock()
    monkeypatch.setattr(launcher, "probe_server", Mock(return_value=False))
    monkeypatch.setattr(launcher, "reserve_listener", Mock(return_value=listener))
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(Config=Mock(side_effect=ImportError("dependency missing"))))
    assert launcher.main([]) == 1
    listener.close.assert_called_once()


def test_invalid_ports_are_rejected():
    for port in ["80", "65536"]:
        with pytest.raises(SystemExit) as error:
            launcher.main(["--port", port])
        assert error.value.code == 2


def test_listener_closes_if_bind_fails(monkeypatch):
    listener = Mock()
    listener.bind.side_effect = OSError("occupied")
    monkeypatch.setattr(launcher.socket, "socket", Mock(return_value=listener))
    with pytest.raises(OSError):
        launcher.reserve_listener(8787)
    listener.close.assert_called_once()


def test_auto_port_uses_next_available_without_stopping_any_process(monkeypatch, main_environment):
    listener = Mock()
    reserve = Mock(side_effect=[OSError("8787 occupied"), listener])
    monkeypatch.setattr(launcher, "probe_server", Mock(return_value=False))
    monkeypatch.setattr(launcher, "reserve_listener", reserve)
    monkeypatch.setattr(launcher.threading, "Thread", Mock(return_value=Mock()))
    server = Mock(started=True)
    config = Mock(return_value=object())
    monkeypatch.setitem(sys.modules, "uvicorn", SimpleNamespace(Config=config, Server=Mock(return_value=server)))
    assert launcher.main(["--auto-port", "--open-browser"]) == 0
    assert [call.args[0] for call in reserve.call_args_list] == [8787, 8788]
    assert config.call_args.kwargs["port"] == 8788
    server.run.assert_called_once_with(sockets=[listener])
    main_environment.assert_not_called()  # opening stays gated by the readiness worker


def test_auto_port_reuses_healthy_instance_on_next_port(monkeypatch, main_environment):
    monkeypatch.setattr(launcher, "probe_server", Mock(side_effect=[False, True]))
    reserve = Mock(side_effect=OSError("8787 occupied"))
    monkeypatch.setattr(launcher, "reserve_listener", reserve)
    assert launcher.main(["--auto-port", "--open-browser"]) == 0
    main_environment.assert_called_once_with("http://127.0.0.1:8788")
    reserve.assert_called_once_with(8787)


def test_auto_port_is_bounded_and_never_wraps(monkeypatch, main_environment):
    monkeypatch.setattr(launcher, "probe_server", Mock(return_value=False))
    reserve = Mock(side_effect=OSError("occupied"))
    monkeypatch.setattr(launcher, "reserve_listener", reserve)
    assert launcher.main(["--auto-port", "--port", "65534", "--open-browser"]) == 1
    assert [call.args[0] for call in reserve.call_args_list] == [65534, 65535]
    main_environment.assert_not_called()
