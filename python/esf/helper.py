"""Opt-in, per-user startup for the loopback companion. No administrator privileges or OAuth cache.

The local API key lives in a private file and survives restarts so an already paired browser stays
paired. ChatGPT tokens still live only in the server's memory. Service definitions contain paths,
never credentials. Native service managers own the process; we never kill an arbitrary saved PID.
"""

from __future__ import annotations

import json
import os
import plistlib
import secrets
import socket
import stat
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import httpx

from . import cli
from .config import REPO_ROOT, Settings

LABEL = "org.effectsizefinder.helper"
UNIT = "effect-size-finder.service"
HEALTH = {"app": "effect-size-finder", "version": 1}


class HelperError(RuntimeError):
    pass


def load_config(path: Path) -> dict:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise HelperError(f"Helper settings must be a private file owned by your account: {path}")
        config = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(config, dict) or config.get("version") != 1
                or type(config.get("port")) is not int or not 1024 <= config["port"] <= 65535
                or not isinstance(config.get("access_key"), str) or len(config["access_key"]) < 32
                or not cli.ACCESS_TOKEN.fullmatch(config["access_key"])
                or any(not isinstance(config.get(name), str) or not Path(config[name]).is_absolute()
                       for name in ("data_dir", "web_dir"))):
            raise ValueError("invalid settings")
        return config
    except (OSError, ValueError, TypeError) as exc:
        raise HelperError(f"Cannot read helper settings at {path}. Run esf helper install to set it up.") from exc


def run_helper(path: Path) -> None:
    config = load_config(path)
    settings = replace(Settings.from_env(), data_dir=Path(config["data_dir"]), web_dir=Path(config["web_dir"]))
    cli.serve_app("127.0.0.1", config["port"], open_browser=False, token=config["access_key"], settings=settings)


def healthy(config: dict) -> bool:
    # Never send a local connection key through an environment's HTTP proxy or a redirect.
    try:
        with httpx.Client(trust_env=False, timeout=0.75, follow_redirects=False) as client:
            response = client.get(f"http://127.0.0.1:{config['port']}/api/helper", headers={"X-ESF-Token": config["access_key"]})
        return response.status_code == 200 and response.json() == HEALTH
    except (httpx.HTTPError, ValueError):
        return False


def port_busy(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def command(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HelperError(f"Could not run {args[0]}. Automatic startup needs a macOS login session or a Linux systemd user session.") from exc
    if check and result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip())[-500:]
        raise HelperError(f"{args[0]} could not manage the helper: {detail}")
    return result


def unit_quote(value: str, *, environment: bool = False) -> str:
    """systemd quoting, not shell quoting. Percent expands specifiers; ExecStart also expands dollars."""
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("\n", "\\n").replace("\r", "\\r")
    if environment:
        value = value.replace("$", "$$")
    return f'"{value}"'


class Helper:
    def __init__(self, *, platform: str | None = None, home: Path | None = None, uid: int | None = None):
        self.platform = platform or sys.platform
        self.home = home or Path.home()
        if self.platform != "darwin" and not self.platform.startswith("linux"):
            raise HelperError("Automatic startup currently supports macOS and Linux with systemd. On Windows, use esf serve.")
        self.uid = os.getuid() if uid is None else uid
        if self.platform == "darwin":
            self.root = self.home / "Library" / "Application Support" / "Effect Size Finder"
            self.service = self.home / "Library" / "LaunchAgents" / f"{LABEL}.plist"
        else:
            # Honor absolute XDG locations; relative values are not valid XDG paths.
            data = Path(os.environ.get("XDG_DATA_HOME", ""))
            config = Path(os.environ.get("XDG_CONFIG_HOME", ""))
            self.root = (data if data.is_absolute() else self.home / ".local" / "share") / "effect-size-finder"
            self.service = (config if config.is_absolute() else self.home / ".config") / "systemd" / "user" / UNIT
        self.config = self.root / "helper.json"

    def require_user(self) -> None:
        if self.uid == 0:
            raise HelperError("Run esf helper as your login account, without sudo; it installs only for that account.")

    def definition(self) -> str:
        # Do not resolve a virtualenv Python symlink: its location selects that environment.
        argv = [str(Path(sys.executable).absolute()), "-m", "esf.cli", "helper", "run", "--config", str(self.config)]
        if self.platform == "darwin":
            return plistlib.dumps({"Label": LABEL, "ProgramArguments": argv, "WorkingDirectory": str(REPO_ROOT),
                                   "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 30, "Umask": 0o077,
                                   "StandardOutPath": str(self.root / "helper.log"),
                                   "StandardErrorPath": str(self.root / "helper.log")}).decode("utf-8")
        return ("[Unit]\nDescription=Effect Size Finder local helper\n\n[Service]\nType=exec\n"
                f"ExecStart={' '.join(unit_quote(arg, environment=True) for arg in argv)}\n"
                "Restart=on-failure\nRestartSec=10\nTimeoutStopSec=30\nUMask=0077\n\n[Install]\nWantedBy=default.target\n")

    def start_service(self) -> None:
        if self.platform == "darwin":
            command(["launchctl", "enable", f"gui/{self.uid}/{LABEL}"])
            command(["launchctl", "bootstrap", f"gui/{self.uid}", str(self.service)])
        else:
            command(["systemctl", "--user", "daemon-reload"])
            command(["systemctl", "--user", "enable", "--now", UNIT])

    def stop_service(self) -> None:
        if self.platform == "darwin":
            target = f"gui/{self.uid}/{LABEL}"
            if command(["launchctl", "print", target], check=False).returncode == 0:
                command(["launchctl", "bootout", target])
        else:
            command(["systemctl", "--user", "disable", "--now", UNIT], check=self.service.exists())

    def wait_ready(self, config: dict, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if healthy(config):
                return
            time.sleep(0.2)
        logs = str(self.root / "helper.log") if self.platform == "darwin" else f"journalctl --user -u {UNIT}"
        raise HelperError(f"The helper did not start on port {config['port']}. Check {logs}. "
                          "Stop any earlier esf serve, then try again.")

    def wait_stopped(self, config: dict, timeout: float = 10) -> None:
        # launchctl bootout can return while the old Python process is still shutting down. Starting
        # immediately could collide on the port, or mistake that old process for a successful restart.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not port_busy(config["port"]):
                return
            time.sleep(0.1)
        raise HelperError(f"Port {config['port']} is still occupied after stopping the helper. Try again once it is free.")

    def install(self, port: int | None = None, *, open_browser: bool = True) -> None:
        self.require_user()
        old = load_config(self.config) if self.config.exists() else None
        port = port if port is not None else old["port"] if old else 8000
        if type(port) is not int or not 1024 <= port <= 65535:
            raise HelperError("Choose a helper port from 1024 through 65535.")
        previous_service = self.service.read_text(encoding="utf-8") if self.service.exists() else None
        own_running = bool(old and previous_service and healthy(old))
        if port_busy(port) and not (own_running and old["port"] == port):
            raise HelperError(f"Port {port} is already in use. Stop the earlier esf serve with Ctrl+C, "
                              "then run this command again, or choose --port PORT.")
        settings = Settings.from_env()
        config = {"version": 1, "port": port, "access_key": old["access_key"] if old else secrets.token_urlsafe(32),
                  "data_dir": str(settings.data_dir.resolve()), "web_dir": str(settings.web_dir.resolve())}
        definition = self.definition()
        if not (own_running and config == old and previous_service == definition):
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.root.chmod(0o700)
            self.service.parent.mkdir(parents=True, exist_ok=True)
            if previous_service:
                self.stop_service()
                if own_running:
                    self.wait_stopped(old)
            try:
                cli.write_private(self.config, json.dumps(config, indent=2) + "\n")
                cli.write_private(self.service, definition)
                self.start_service()
                self.wait_ready(config)
            except (HelperError, OSError):
                # A failed first installation must not leave a retrying login job behind. On an
                # update, put the previous service and its private configuration back.
                try:
                    self.stop_service()
                except HelperError:
                    pass
                self.service.unlink(missing_ok=True)
                if old:
                    cli.write_private(self.config, json.dumps(old, indent=2) + "\n")
                if previous_service:
                    cli.write_private(self.service, previous_service)
                    try:
                        self.start_service()
                    except HelperError:
                        pass
                raise
        print(f"Helper ready at http://127.0.0.1:{port}. It will start automatically when you log in.")
        if open_browser:
            self.open()
        else:
            print("Pair this browser once with esf helper open, then use Connect ChatGPT on the website.")

    def open(self) -> None:
        config = load_config(self.config)
        if not healthy(config):
            raise HelperError("The helper is not running. Run esf helper restart first.")
        launcher = self.root / "open.html"
        uri = cli.write_launcher(launcher, cli.page_address("127.0.0.1", config["port"], config["access_key"]))
        opened = False
        if cli.has_display():
            try:
                opened = cli.webbrowser.open(uri)
            except (OSError, cli.webbrowser.Error):
                pass
        print("The local pairing page opened. Return to the website and click Connect ChatGPT." if opened
              else f"Open {launcher} in the same browser you use for the website, then click Connect ChatGPT.")

    def status(self) -> bool:
        if not self.config.exists():
            print("Automatic helper is not installed. Run esf helper install.")
            return False
        config = load_config(self.config)
        running = healthy(config)
        print(f"Helper running at http://127.0.0.1:{config['port']}." if running
              else "Helper is not running. Run esf helper restart.")
        print("Automatic startup is installed." if self.service.exists() else "Automatic startup is not installed.")
        return running

    def restart(self) -> None:
        self.require_user()
        config = load_config(self.config)
        if not self.service.exists():
            raise HelperError("Automatic startup is not installed. Run esf helper install first.")
        self.stop_service()
        self.wait_stopped(config)
        self.start_service()
        self.wait_ready(config)
        print("Helper restarted. Your browser stays paired; sign in with ChatGPT again in the companion window.")

    def uninstall(self) -> None:
        self.require_user()
        self.stop_service()
        self.service.unlink(missing_ok=True)
        (self.root / "open.html").unlink(missing_ok=True)
        if self.platform.startswith("linux"):
            command(["systemctl", "--user", "daemon-reload"])
        print("Helper stopped and automatic startup removed. Your data and connection settings were kept.")
