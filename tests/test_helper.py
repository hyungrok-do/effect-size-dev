"""Automatic helper installation is opt-in; ordinary tests never register a real login service."""

import json
import os
import plistlib
import shutil
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from esf import cli, helper
from esf.config import REPO_ROOT
from test_server import make_client


@pytest.fixture
def installed_environment(tmp_path, monkeypatch):
    for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "ESF_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ESF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(helper, "port_busy", lambda port: False)
    monkeypatch.setattr(helper, "healthy", lambda config: True)
    calls = []

    def run(args, **kwargs):
        assert kwargs == {"capture_output": True, "text": True, "timeout": 45, "check": False}
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr(helper.subprocess, "run", run)
    return calls


@pytest.mark.parametrize("platform", ["darwin", "linux"])
def test_install_is_private_per_user_idempotent_and_reversible(tmp_path, monkeypatch, capsys, installed_environment, platform):
    calls = installed_environment
    # Preserve the venv executable spelling even when it is a symlink to the base interpreter.
    venv_python = tmp_path / 'env with spaces $HOME %n "quote"' / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(sys.executable)
    monkeypatch.setattr(helper.sys, "executable", str(venv_python))
    instance = helper.Helper(platform=platform, home=tmp_path, uid=501)
    instance.install(open_browser=False)
    config = helper.load_config(instance.config)
    key = config["access_key"]
    assert config["port"] == 8000 and config["data_dir"] == str(tmp_path / "data")
    assert stat.S_IMODE(instance.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(instance.config.stat().st_mode) == 0o600
    assert stat.S_IMODE(instance.service.stat().st_mode) == 0o600
    definition = instance.service.read_text()
    assert key not in definition and not any(key in str(call) for call in calls)
    if platform == "darwin":
        job = plistlib.loads(definition.encode())
        assert job["ProgramArguments"][0] == str(venv_python)
        assert job["ProgramArguments"][-2:] == ["--config", str(instance.config)]
        assert job["KeepAlive"] is True and job["RunAtLoad"] is True and job["Umask"] == 0o077
        assert calls[-1] == ["launchctl", "bootstrap", "gui/501", str(instance.service)]
    else:
        assert '$$HOME %%n \\"quote\\"' in definition
        assert "WantedBy=default.target" in definition and "UMask=0077" in definition
        assert calls[-1] == ["systemctl", "--user", "enable", "--now", helper.UNIT]
    before = len(calls)
    instance.install(open_browser=False)
    assert len(calls) == before  # no unnecessary restart/sign-out on repeated setup
    instance.restart()
    assert helper.load_config(instance.config)["access_key"] == key
    assert instance.status()
    instance.uninstall()
    assert not instance.service.exists() and helper.load_config(instance.config)["access_key"] == key
    assert key not in capsys.readouterr().out


def test_browser_pairing_uses_a_private_launcher_and_keeps_keys_out_of_arguments(tmp_path, monkeypatch, capsys, installed_environment):
    instance = helper.Helper(platform="darwin", home=tmp_path, uid=501)
    monkeypatch.setattr(cli, "has_display", lambda: True)
    opened = []
    monkeypatch.setattr(cli.webbrowser, "open", lambda uri: opened.append(uri) or True)
    instance.install()
    key = helper.load_config(instance.config)["access_key"]
    launcher = instance.root / "open.html"
    assert opened == [launcher.as_uri()]
    assert key not in opened[0] and key in launcher.read_text()
    assert stat.S_IMODE(launcher.stat().st_mode) == 0o600 and key not in capsys.readouterr().out
    monkeypatch.setattr(cli.webbrowser, "open", lambda uri: False)
    instance.open()
    assert str(launcher) in capsys.readouterr().out


def test_install_does_not_replace_a_process_that_owns_the_port(tmp_path, monkeypatch, installed_environment):
    instance = helper.Helper(platform="linux", home=tmp_path, uid=501)
    monkeypatch.setattr(helper, "port_busy", lambda port: True)
    with pytest.raises(helper.HelperError, match="already in use"):
        instance.install()
    assert not instance.root.exists() and not instance.service.exists() and not installed_environment
    with pytest.raises(helper.HelperError, match="1024 through 65535"):
        instance.install(80)


def test_failed_start_removes_a_new_login_job_and_restores_an_older_one(tmp_path, monkeypatch, installed_environment):
    instance = helper.Helper(platform="darwin", home=tmp_path, uid=501)

    def failed(*args):
        raise helper.HelperError("startup failed")

    real_wait = instance.wait_ready
    monkeypatch.setattr(instance, "wait_ready", failed)
    with pytest.raises(helper.HelperError, match="startup failed"):
        instance.install(open_browser=False)
    assert not instance.service.exists()
    key = helper.load_config(instance.config)["access_key"]
    monkeypatch.setattr(instance, "wait_ready", real_wait)
    instance.install(open_browser=False)
    before = instance.service.read_text()
    monkeypatch.setattr(instance, "wait_ready", failed)
    with pytest.raises(helper.HelperError, match="startup failed"):
        instance.install(8123, open_browser=False)
    assert instance.service.read_text() == before
    restored = helper.load_config(instance.config)
    assert restored["port"] == 8000 and restored["access_key"] == key


def test_config_rejects_exposed_secrets_and_run_stays_on_loopback(tmp_path, monkeypatch, installed_environment):
    instance = helper.Helper(platform="linux", home=tmp_path, uid=501)
    instance.install(open_browser=False)
    config = helper.load_config(instance.config)
    served = []
    monkeypatch.setenv("ESF_ACCESS_TOKEN", "a-different-key-must-not-replace-the-paired-key")
    monkeypatch.setattr(cli, "serve_app", lambda *args, **kwargs: served.append((args, kwargs)))
    cli.main(["helper", "run", "--config", str(instance.config)])
    [(args, kwargs)] = served
    assert args == ("127.0.0.1", 8000) and kwargs["open_browser"] is False
    assert kwargs["token"] == config["access_key"] and kwargs["settings"].data_dir == tmp_path / "data"
    instance.config.chmod(0o644)
    with pytest.raises(helper.HelperError, match="private file"):
        helper.load_config(instance.config)
    instance.config.chmod(0o600)
    link = instance.root / "linked.json"
    link.symlink_to(instance.config)
    with pytest.raises(helper.HelperError, match="private file"):
        helper.load_config(link)
    for port in ("8000", True, -1, 65536):
        cli.write_private(instance.config, json.dumps({**config, "port": port}))
        with pytest.raises(helper.HelperError, match="Cannot read"):
            helper.load_config(instance.config)


def test_health_probe_stays_local_ignores_proxies_and_never_follows_redirects(monkeypatch):
    real_client = httpx.Client
    requests = []

    def redirect(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://elsewhere.example/"})

    def client(**kwargs):
        assert kwargs == {"trust_env": False, "timeout": 0.75, "follow_redirects": False}
        return real_client(transport=httpx.MockTransport(redirect), **kwargs)

    monkeypatch.setattr(helper.httpx, "Client", client)
    assert not helper.healthy({"port": 8000, "access_key": "private-key"})
    assert len(requests) == 1 and str(requests[0].url) == "http://127.0.0.1:8000/api/helper"
    assert requests[0].headers["x-esf-token"] == "private-key"


def test_health_endpoint_is_authenticated_and_loopback_only(tmp_path):
    client = make_client(tmp_path, access_token="private-key")
    assert client.get("/api/helper").status_code == 401
    result = client.get("/api/helper", headers={"X-ESF-Token": "private-key"})
    assert result.status_code == 200 and result.json() == helper.HEALTH
    assert result.headers["cache-control"] == "no-store"
    assert client.get("/api/helper", headers={"X-ESF-Token": "private-key", "X-Forwarded-For": "127.0.0.1"}).status_code == 403


def test_reinstallation_waits_for_the_old_port_and_launchd_registration(tmp_path, monkeypatch, installed_environment):
    instance = helper.Helper(platform="darwin", home=tmp_path, uid=501)
    instance.install(open_browser=False)
    occupied = iter([False, True, True, False])  # the new port is free; the old one takes time to close
    waits = []
    monkeypatch.setattr(helper, "port_busy", lambda port: next(occupied))
    monkeypatch.setattr(helper.time, "sleep", lambda seconds: waits.append(seconds))
    run = helper.subprocess.run
    bootstraps = []

    def delayed_registration(args, **kwargs):
        if args[:2] == ["launchctl", "bootstrap"]:
            bootstraps.append(args)
            if len(bootstraps) == 1:
                return subprocess.CompletedProcess(args, 5, stdout="", stderr="Bootstrap failed: 5: Input/output error")
        return run(args, **kwargs)

    monkeypatch.setattr(helper.subprocess, "run", delayed_registration)
    instance.install(8123, open_browser=False)
    assert waits == [0.1, 0.1, 0.5] and len(bootstraps) == 2
    instance.restart()
    assert installed_environment[-1] == ["launchctl", "kickstart", "-k", f"gui/501/{helper.LABEL}"]


def test_setup_explains_unsupported_sessions_without_changing_startup(tmp_path, installed_environment):
    with pytest.raises(helper.HelperError, match="On Windows, use esf serve"):
        helper.Helper(platform="win32", home=tmp_path)
    with pytest.raises(helper.HelperError, match="without sudo"):
        helper.Helper(platform="darwin", home=tmp_path, uid=0).install()
    assert not installed_environment


@pytest.mark.skipif(not sys.platform.startswith("linux") or not shutil.which("systemd-analyze"), reason="systemd parser unavailable")
def test_linux_definition_passes_the_native_systemd_parser(tmp_path):
    instance = helper.Helper(platform="linux", home=tmp_path / "user with spaces %n", uid=1000)
    unit = tmp_path / helper.UNIT
    unit.write_text(instance.definition())
    result = subprocess.run(["systemd-analyze", "verify", str(unit)], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.environ.get("ESF_TEST_LOGIN_SERVICE") != "1", reason="real login service is opt-in, exercised on macOS CI")
def test_native_login_service_lifecycle(tmp_path, monkeypatch):
    """Actually bootstrap, probe, restart and unload on the CI runner's launchd user domain."""
    monkeypatch.setenv("ESF_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ESF_WEB_DIR", str(REPO_ROOT / "web"))
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    instance = helper.Helper(home=tmp_path)
    try:
        instance.install(port, open_browser=False)
        config = helper.load_config(instance.config)
        assert instance.status()
        instance.restart()
        assert helper.load_config(instance.config) == config and instance.status()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            new_port = sock.getsockname()[1]
        instance.install(new_port, open_browser=False)
        changed = helper.load_config(instance.config)
        assert changed["port"] == new_port and changed["access_key"] == config["access_key"] and instance.status()
        assert not helper.healthy(config)
        config = changed
        instance.uninstall()
        deadline = time.monotonic() + 10
        while helper.healthy(config) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not helper.healthy(config) and not instance.service.exists()
    except Exception:
        log = instance.root / "helper.log"
        if log.exists():
            print(log.read_text(encoding="utf-8")[-6000:])
        raise
    finally:
        instance.uninstall()
