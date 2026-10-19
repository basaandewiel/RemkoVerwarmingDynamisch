#!/usr/bin/env bash
# Start de registermonitor als achtergrondproces (via cron @reboot) op de Pi.
# Geen systemd/sudo nodig als je dit als gewone gebruiker draait.
#
# Gebruik:
#   bash start-register-monitor.sh          # (her)start de monitor nu
#   bash start-register-monitor.sh stop     # stoppen
#   bash start-register-monitor.sh status   # draait hij?
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$HOME/heating_probe.log"
PIDFILE="$HOME/.cache/remko-wkf70/register_monitor.pid"
ACTION="${1:-start}"

mkdir -p "$(dirname "$PIDFILE")"

stop_monitor() {
    if [ -f "$PIDFILE" ]; then
        kill "$(cat "$PIDFILE")" 2>/dev/null || true
        rm -f "$PIDFILE"
    fi
    pkill -f "[r]egister_monitor.py" 2>/dev/null || true
    sleep 1
}

case "$ACTION" in
    stop)
        stop_monitor
        echo ">> registermonitor gestopt"
        exit 0
        ;;
    status)
        if pgrep -af "[r]egister_monitor.py"; then
            echo ">> draait"
            tail -3 "$LOG" 2>/dev/null || true
        else
            echo ">> draait NIET"
        fi
        exit 0
        ;;
esac

stop_monitor
cd "$DIR"
setsid nohup "$DIR/venv/bin/python3" register_monitor.py --interval 10 \
    >> "$LOG" 2>&1 < /dev/null &
echo $! > "$PIDFILE"
sleep 3
if pgrep -af "[r]egister_monitor.py"; then
    echo ">> registermonitor gestart (pid $(cat "$PIDFILE"))"
    echo ">> log: $LOG"
else
    echo "FOUT: monitor niet gestart — zie $LOG" >&2
    tail -5 "$LOG" 2>/dev/null || true
    exit 1
fi
