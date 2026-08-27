#!/bin/bash
# Launch sgt_diag.py DETACHED and return immediately.
#
# Why: RUN_SHELL_COMMAND blocks the calling macro until the command exits, but
# sgt_diag.py works by POSTing gcode back to Klipper. Klipper cannot execute
# that gcode while it is still blocked inside the macro - so the two deadlock
# and the script's HTTP call eventually times out having done nothing.
#
# Returning straight away frees Klipper's gcode queue, letting the detached
# script drive the machine normally. Output goes to the log instead of the
# console; read it with SHOW_SGT_LOG.
LOG=/home/voron24/printer_data/logs/sgt_diag.log
: > "$LOG"
setsid nohup /usr/bin/python3 -u /home/voron24/sgt_diag.py "$@" >> "$LOG" 2>&1 &
echo "started: sgt_diag.py $* -> run SHOW_SGT_LOG to follow progress"
