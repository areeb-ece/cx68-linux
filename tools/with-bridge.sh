#!/bin/sh
# Run a command with the gamepad bridge running, and stop the bridge afterwards.
# Steam launch options:  /home/<you>/keyboard/tools/with-bridge.sh %command%
systemctl --user start cx68-bridge.service
"$@"
status=$?
systemctl --user stop cx68-bridge.service
exit $status
