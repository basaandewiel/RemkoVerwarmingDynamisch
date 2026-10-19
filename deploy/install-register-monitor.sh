#!/usr/bin/env bash
# Start de registermonitor als een echte service op de Pi, zodat hij een nacht
# (of langer) kan doorlopen en niet stopt als je SSH-sessie sluit.
#
# Gebruik (op de Pi):
#   bash deploy/install-register-monitor.sh          # start/vervang monitor
#   bash deploy/install-register-monitor.sh stop     # stoppen
#   bash deploy/install-register-monitor.sh status   # status
#
# Installeert de systemd-unit `remko-register-monitor` (read-only: stuurt
# alleen FORCE_RESPONSE-query's) en schakelt hem in bij boot.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="$(dirname "$SCRIPT_DIR")"
USER_NAME="${SUDO_USER:-$(id -un)}"
UNIT=remko-register-monitor
ACTION="${1:-start}"

if [ "$ACTION" = "stop" ]; then
    sudo systemctl disable --now "$UNIT" 2>/dev/null || true
    echo ">> $UNIT gestopt en uitgeschakeld"
    exit 0
fi

if [ "$ACTION" = "status" ]; then
    systemctl status "$UNIT" --no-pager || true
    echo
    echo ">> live meekijken:  journalctl -u $UNIT -f"
    echo ">> CSV:             ~/.cache/remko-wkf70/heating_probe.csv"
    exit 0
fi

sd_ke="${USER_NAME//\//}"
echo "== $UNIT installeren =="
echo "   map  : $DIR"
echo "   user : $USER_NAME"

sudo tee "/etc/systemd/system/$UNIT.service" >/dev/null <<EOF
[Unit]
Description=REMKO WKF70 registermonitor (read-only; warmtevraag-registers volgen)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$sd_ke
WorkingDirectory=$DIR
ExecStart=$DIR/venv/bin/python3 register_monitor.py --interval 10
Environment=PYTHONUNBUFFERED=1
Restart=on-failure
RestartSec=60
StandardOutput=append:/home/$sd_ke/heating_probe.log
StandardError=append:/home/$sd_ke/heating_probe.log

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT"
sleep 4
echo
echo "Klaar. Status:"
sudo systemctl status "$UNIT" --no-pager -l | head -15 || true
echo
echo ">> live:      journalctl -u $UNIT -f"
echo ">> rapport:   $DIR/venv/bin/python3 $DIR/register_monitor.py --report"
echo ">> conclusie: $DIR/venv/bin/python3 $DIR/analyse_heating_demand.py"
