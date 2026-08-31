#!/bin/zsh
# FlowClone launcher — double-click me (or put me in the Dock / Login Items).
# Runs under Terminal.app, so it inherits the mic / Input Monitoring /
# Accessibility permissions you already granted to Terminal.
cd "$(dirname "$0")" || exit 1
UV="$(command -v uv)" || UV="$HOME/.local/bin/uv"
exec "$UV" run flowclone
