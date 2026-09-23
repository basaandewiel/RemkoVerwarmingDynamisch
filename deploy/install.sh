#!/usr/bin/env bash
# Installeer remko-sww-boost als systemd-service op de Raspberry Pi.
#
# Gebruik (op de Pi, in de repo-map):
#   bash deploy/install.sh
#
# Wat dit doet:
#   1) python3 + venv controleren en dependencies installeren (alleen paho-mqtt)
#   2) config.json aanmaken vanuit config.example.json als die ontbreekt
#   3) de systemd-unit deploy/remko-sww-boost.service installeren en starten
#
# sudo wordt alleen voor de systeem-unit gebruikt (de rest draait als jouw
# gebruikersaccount).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="$(dirname "$SCRIPT_DIR")"              # repo-root
USER_NAME="${SUDO_USER:-$(id -un)}"         # pi of jouw loginnaam

echo "== remko-sww-boost installatie =="
echo "   map   : $DIR"
echo "   user  : $USER_NAME"

# 1) Python 3 en venv
if ! command -v python3 >/dev/null; then
    echo "FOUT: python3 niet gevonden. Installeer eerst: sudo apt install python3 python3-venv" >&2
    exit 1
fi
if [ ! -x "$DIR/venv/bin/python3" ]; then
    echo ">> virtualenv aanmaken"
    python3 -m venv "$DIR/venv"
fi
echo ">> dependencies installeren (paho-mqtt)"
"$DIR/venv/bin/pip" install --quiet --upgrade pip
"$DIR/venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

# 2) Config bij eerste run
if [ ! -f "$DIR/config.json" ]; then
    echo ">> config.example.json gekopieerd naar config.json"
    echo "   LET OP: vul hierna je ENTSO-E API-key en locatie in!"
    cp "$DIR/config.example.json" "$DIR/config.json"
else
    echo ">> config.json bestaat al (onaangeroerd)"
fi

# 3) Systemd-unit installeren (sudo)
UNIT_SRC="$SCRIPT_DIR/remko-sww-boost.service"
UNIT_DST="/etc/systemd/system/remko-sww-boost.service"
echo ">> systemd-unit installeren ($UNIT_DST)"
sd_ke="${USER_NAME//\//}"
sed -e "s|__USER__|$sd_ke|g" \
    -e "s|__DIR__|$DIR|g" "$UNIT_SRC" | sudo tee "$UNIT_DST" >/dev/null
sudo systemctl daemon-reload

# 4) Snelle zelfcontrole (geen publicatie)
echo ">> zelfcontrole: adviesberekening (--dry-run)"
"$DIR/venv/bin/python3" "$DIR/dhw_boost.py" --dry-run || {
    echo "  (adviescrash? Maar de service staat al klaar; check config.json.)" >&2
}

# 5) Service starten
echo ">> service starten en inschakelen bij boot"
sudo systemctl enable --now remko-sww-boost
echo
echo "Klaar. Controle:"
echo "  systemctl status remko-sww-boost"
echo "  journalctl -u remko-sww-boost -f"
echo
echo "Vul eerst je ENTSO-E API-key in: nano $DIR/config.json  (daarna: sudo systemctl restart remko-sww-boost)"