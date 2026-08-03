"""Register the installed FlowClone app to open at macOS login."""

import os
import plistlib
import subprocess
from pathlib import Path

LABEL = "com.flowclone.login"
AGENT_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
APP_PATH_ENV = "FLOWCLONE_APP_PATH"


class LoginItemError(RuntimeError):
    pass


def current_app_path() -> Path | None:
    """The FlowClone bundle hosting this process, if it can be identified."""
    configured = os.environ.get(APP_PATH_ENV)
    candidates = [Path(configured)] if configured else []
    candidates.append(Path("/Applications/FlowClone.app"))
    for candidate in candidates:
        if candidate.suffix == ".app" and candidate.exists():
            return candidate.resolve()
    return None


def enabled(path: Path = AGENT_PATH) -> bool:
    return path.exists()


def enable(
    app_path: Path | None = None,
    agent_path: Path = AGENT_PATH,
    runner=subprocess.run,
) -> None:
    app_path = app_path or current_app_path()
    if app_path is None or app_path.suffix != ".app" or not app_path.exists():
        raise LoginItemError(
            "Install and launch /Applications/FlowClone.app before enabling login startup."
        )

    payload = {
        "Label": LABEL,
        "ProgramArguments": ["/usr/bin/open", "-g", str(app_path)],
        "RunAtLoad": True,
        "KeepAlive": False,
    }
    agent_path.parent.mkdir(parents=True, exist_ok=True)
    temp = agent_path.with_name(f".{agent_path.name}.tmp")
    with open(temp, "wb") as fh:
        plistlib.dump(payload, fh)
    temp.replace(agent_path)

    domain = f"gui/{os.getuid()}"
    result = runner(
        ["launchctl", "bootstrap", domain, str(agent_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        try:
            agent_path.unlink()
        except FileNotFoundError:
            pass
        message = (result.stderr or result.stdout or "launchctl bootstrap failed").strip()
        raise LoginItemError(message)


def disable(
    agent_path: Path = AGENT_PATH,
    runner=subprocess.run,
) -> None:
    domain = f"gui/{os.getuid()}"
    runner(
        ["launchctl", "bootout", domain, str(agent_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    try:
        agent_path.unlink()
    except FileNotFoundError:
        pass
