#!/usr/bin/env bash
# Start de vloerverwarming-regeling als achtergrondproces (via cron @reboot) op
# de Pi. Geen systemd/sudo nodig als je dit als gewone gebruiker draait.
#
# De regeling draait in `--watch`: elke `interval_minutes` herberekent ze het
# goedkoopste laadblok en schakelt ze register 1951 (aan/uit) of 1975 (moduleren).
#
# Gebruik:
#   bash start-heating-comfort.sh          # (her)start nu
#   bash start-heating-comfort.sh stop     # stoppen
#   bash start-heating-comfort.sh status   # draait hij?
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="$HOME/heating_comfort.log"
PIDFILE="$HOME/.cache/remko-wkf70/heating_comfort.pid"
ACTION="${1:-start}"

mkdir -p "$(dirname "$PIDFILE")"

stop_service() {
    if [ -f "$PIDFILE" ]; then
        kill "$(cat "$PIDFILE")" 2>/dev/null || true
        rm -f "$PIDFILE"
    fi
    pkill -f "[h]eating_comfort.py" 2>/dev/null || true
    sleep 1
}

case "$ACTION" in
    stop)
        stop_service
        echo ">> vloerverwarming-regeling gestopt"
        exit 0
        ;;
    status)
        if pgrep -af "[h]eating_comfort.py"; then
            echo ">> draait"
            tail -3 "$LOG" 2>/dev/null || true
        else
            echo ">> draait NIET"
        fi
        exit 0
        ;;
esac

stop_service
cd "$DIR"
setsid nohup "$DIR/venv/bin/python3" heating_comfort.py --watch \
    >> "$LOG" 2>&1 < /dev/null &
echo $! > "$PIDFILE"
sleep 3
if pgrep -af "[h]eating_comfort.py"; then
    echo ">> vloerverwarming-regeling gestart (pid $(cat "$PIDFILE"))"
    echo ">> log: $LOG"
else
    echo "FOUT: regeling niet gestart — zie $LOG" >&2
    tail -5 "$LOG" 2>/dev/null || true
    exit 1
fi
