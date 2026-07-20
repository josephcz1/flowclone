#!/bin/zsh
# FlowClone launcher — double-click me (or put me in the Dock / Login Items).
# Runs under Terminal.app, so it inherits the mic / Input Monitoring /
# Accessibility permissions you already granted to Terminal.
cd "/Users/josephchen/General/Projects/Whisprflow clone" || exit 1
exec "/Users/josephchen/.local/bin/uv" run flowclone
