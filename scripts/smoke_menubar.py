"""Headless smoke test for the menu-bar app: construct it, drive the state
machine and menu callbacks directly (no run loop, no permissions needed).

Usage: uv run python scripts/smoke_menubar.py
"""

import sys

from flowclone.menubar import TITLES, FlowCloneApp


def main() -> int:
    app = FlowCloneApp()
    assert app.title == TITLES["loading"], app.title
    assert app.cfg.dictionary, "config.toml dictionary did not load"

    # State transitions map to the right icon.
    app.set_state("recording")
    assert app.title == TITLES["recording"], app.title
    app.set_state("processing")
    assert app.title == TITLES["processing"], app.title
    app.set_state("idle")
    assert app.title == TITLES["idle"], app.title

    # Pause disables dictation and pins the paused icon even on an idle report.
    app.toggle_pause(None)
    assert app.paused and app.title == TITLES["paused"], app.title
    app.set_state("idle")
    assert app.title == TITLES["paused"], "idle must not override paused"
    app.on_press()  # must be a no-op while paused / model not loaded
    assert app._session is None, "press should not start a session while paused"
    app.toggle_pause(None)
    assert not app.paused and app.title == TITLES["idle"], app.title

    print("smoke_menubar: OK — construction, state icons, pause gating all pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
