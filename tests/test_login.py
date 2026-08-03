import os
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from flowclone import login


def successful_runner(calls):
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run


def test_enable_writes_app_aware_launch_agent(tmp_path):
    app = tmp_path / "FlowClone.app"
    app.mkdir()
    agent = tmp_path / "LaunchAgents" / "com.flowclone.login.plist"
    calls = []

    login.enable(app, agent, successful_runner(calls))

    with open(agent, "rb") as fh:
        payload = plistlib.load(fh)
    assert payload["Label"] == login.LABEL
    assert payload["ProgramArguments"] == ["/usr/bin/open", "-g", str(app)]
    assert payload["RunAtLoad"] is True
    assert calls[0][0][:3] == ["launchctl", "bootstrap", f"gui/{os.getuid()}"]


def test_enable_rolls_back_when_launchctl_fails(tmp_path):
    app = tmp_path / "FlowClone.app"
    app.mkdir()
    agent = tmp_path / "agent.plist"

    def fail(*_args, **_kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="not permitted")

    with pytest.raises(login.LoginItemError, match="not permitted"):
        login.enable(app, agent, fail)
    assert not agent.exists()


def test_enable_requires_an_installed_app(tmp_path):
    with pytest.raises(login.LoginItemError):
        login.enable(tmp_path / "missing.app", tmp_path / "agent.plist")


def test_disable_boots_out_and_removes_agent(tmp_path):
    agent = tmp_path / "agent.plist"
    agent.write_text("placeholder")
    calls = []

    login.disable(agent, successful_runner(calls))

    assert not agent.exists()
    assert calls[0][0][:3] == ["launchctl", "bootout", f"gui/{os.getuid()}"]


def test_current_app_path_prefers_launcher_environment(tmp_path, monkeypatch):
    app = tmp_path / "FlowClone.app"
    app.mkdir()
    monkeypatch.setenv(login.APP_PATH_ENV, str(app))
    assert login.current_app_path() == Path(app).resolve()
