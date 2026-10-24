"""Command line: `esf serve` starts the web app."""

from __future__ import annotations

import argparse
import html
import logging
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

from .chatgpt import CALLBACK_PATH

# Characters an access key may have: it goes in the page's address and in a request header.
ACCESS_TOKEN = re.compile(r"[A-Za-z0-9._~-]+")
STARTUP_FAILURE = 3  # uvicorn's exit code for a server that could not start


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="esf", description="Effect Size Finder")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser(
        "serve",
        help="start the web app",
        description="Start the web app, and print the address to open: it carries an access key, new for "
        "each launch unless ESF_ACCESS_TOKEN sets one.",
    )
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--no-browser", action="store_true", help="don't open the page in the default browser")
    helper = commands.add_parser("helper", help="manage the automatic local helper (macOS or Linux)")
    actions = helper.add_subparsers(dest="action", required=True)
    install = actions.add_parser("install", help="start now and automatically at login; pair this browser once")
    install.add_argument("--port", type=int, help="local port (8000 on first installation)")
    install.add_argument("--no-browser", action="store_true", help="pair later with esf helper open")
    for action, description in (("status", "check whether the helper is running"),
                                ("open", "pair this browser with the running helper"),
                                ("restart", "restart the helper after updating the code"),
                                ("uninstall", "stop the helper and remove automatic startup; keep your data")):
        actions.add_parser(action, help=description)
    run = actions.add_parser("run", help="run the installed helper (used by the startup service)")
    run.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "serve":
        serve_app(args.host, args.port, open_browser=not args.no_browser)
    elif args.command == "helper":
        from .helper import Helper, HelperError, run_helper

        try:
            if args.action == "run":
                run_helper(args.config)
            else:
                automatic = Helper()
                if args.action == "install":
                    automatic.install(args.port, open_browser=not args.no_browser)
                elif args.action == "status":
                    if not automatic.status():
                        raise SystemExit(1)
                elif args.action == "open":
                    automatic.open()
                else:
                    getattr(automatic, args.action)()
        except (HelperError, OSError) as exc:
            parser.exit(1, f"{exc}\n")


def access_token() -> str:
    """The key every /api request must carry: ESF_ACCESS_TOKEN, or a new one for this launch."""
    given = os.environ.get("ESF_ACCESS_TOKEN", "").strip()
    if given and not ACCESS_TOKEN.fullmatch(given):
        sys.exit("ESF_ACCESS_TOKEN may hold only letters, digits and . _ ~ -")
    return given or secrets.token_urlsafe(24)


def page_address(host: str, port: int, token: str | None) -> str:
    """The address to open, with the access key unless `token` is None. A server listening on every
    address is open at this computer's own. So is one on localhost: ChatGPT's sign-in comes back to
    127.0.0.1, an origin other than localhost's, where the page would not have the key it keeps."""
    name = "127.0.0.1" if host.lower() in ("", "0.0.0.0", "::", "localhost") else host
    if ":" in name and not name.startswith("["):
        name = f"[{name}]"
    return f"http://{name}:{port}/" + (f"#key={quote(token, safe='')}" if token is not None else "")


class HideCallbackQuery(logging.Filter):
    """uvicorn's access log shows each request's query string; the sign-in callback's carries ChatGPT's
    one-time code and state, which have no place in a log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        # uvicorn logs (client, method, path with query, HTTP version, status).
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            path, mark, _ = args[2].partition("?")
            if mark and path == CALLBACK_PATH:
                record.args = (*args[:2], f"{path}?<redacted>", *args[3:])
        return True


def write_launcher(path: Path, address: str) -> str:
    """A page that sends the browser on to the address, as Jupyter's does: handing the address itself to
    the browser would put the access key on its command line, which other users of this computer can
    list. Its file:// address, to open instead."""
    link = html.escape(address)
    write_private(
        path,
        f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={link}">'
        f'<title>Effect Size Finder</title><p><a href="{link}">Open Effect Size Finder</a></p>\n',
    )
    return path.resolve().as_uri()


def to_terminal() -> bool:
    """Whether what is printed goes to a terminal. A log the shell makes (esf serve > esf.log) is created
    under the shell's own umask, so other users of this computer may be able to read it."""
    try:
        return sys.stdout.isatty()
    except (AttributeError, ValueError):  # no stdout, or a closed one
        return False


def write_private(path: Path, text: str) -> None:
    """Write a file only this user can read. It is made anew, so an older file's mode doesn't carry over,
    and O_EXCL doesn't follow a link left in its place."""
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def has_display() -> bool:
    """Whether a browser window can open here. On Linux without a display (over SSH, say) Python would
    start a text browser in the terminal the server logs to; the printed address serves instead."""
    return not sys.platform.startswith("linux") or bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def announce(server, address: str, launcher: Path | None, address_file: Path | None = None, timeout: float = 60.0,
             written: list[Path] | None = None) -> None:
    """Once the server is up: print the address to open, and open it in the default browser (through
    `launcher`) unless that is None. With `address_file`, the address is written there and the address
    printed leaves out the access key and names the file instead. Only then, so a launch that fails (the
    port taken by a server already running, say) leaves that server's files alone; the files written are
    added to `written`, for the server to remove when it stops."""
    written = [] if written is None else written
    deadline = time.monotonic() + timeout
    while not server.started:
        if server.should_exit or time.monotonic() > deadline:
            return
        time.sleep(0.1)
    if address_file is not None:
        write_private(address_file, address + "\n")
        written.append(address_file)
    if address_file is None:
        print(f"Effect Size Finder is running. Open this address, which carries its access key: {address}", flush=True)
    else:
        print(f"Effect Size Finder is running at {address.split('#', 1)[0]}. The address to open, with its access key, "
              f"is in {address_file}, which only you can read.", flush=True)
    if launcher is None:
        return
    try:
        page = write_launcher(launcher, address)
        written.append(launcher)
        webbrowser.open(page)
    except (OSError, webbrowser.Error):
        pass  # the printed address still works


def serve_app(host: str, port: int, open_browser: bool = True, *, token: str | None = None, settings=None) -> None:
    import uvicorn

    from .config import Settings
    from .server import create_app

    # Everything the server writes (the searches, the sign-in's settings) is for this user only.
    os.umask(0o077)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = Settings.from_env() if settings is None else settings
    token = access_token() if token is None else token
    config = uvicorn.Config(create_app(settings, access_token=token), host=host, port=port)
    # Added once Config has set up uvicorn's loggers, so that setup can't leave it out.
    logging.getLogger("uvicorn.access").addFilter(HideCallbackQuery())
    server = uvicorn.Server(config)
    address = page_address(host, port, token)
    launcher = settings.data_dir / "open.html" if open_browser and has_display() else None
    # Printed to a file or a pipe, the key could be read wherever that output goes: it is kept in a file
    # of this user's alone instead (in the data folder, which create_app has made by now).
    address_file = None if to_terminal() else settings.data_dir / "address.txt"
    written: list[Path] = []
    try:
        threading.Thread(target=announce, args=(server, address, launcher, address_file),
                         kwargs={"written": written}, daemon=True).start()
        server.run()
    except KeyboardInterrupt:
        pass  # uvicorn raises Ctrl+C again once it has shut down
    finally:
        for path in written:
            path.unlink(missing_ok=True)
    if not server.started:
        sys.exit(STARTUP_FAILURE)


if __name__ == "__main__":
    main()
