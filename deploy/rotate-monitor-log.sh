#!/usr/bin/env bash
# Zorg dat de monitor-log niet onbeperkt groeit: knip hem in bij > 20 MB en
# houd één .1-bestand. Bedoeld als dagelijkse cron-taak (geen sudo nodig).
#
# Cron:
#   15 4 * * * bash ~/github/RemkoVerwarmingDynamisch/deploy/rotate-monitor-log.sh
set -euo pipefail

LOG="$HOME/heating_probe.log"
MAX_BYTES=$((20 * 1024 * 1024))

if [ ! -f "$LOG" ]; then
    exit 0
fi

size=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
if [ "$size" -lt "$MAX_BYTES" ]; then
    exit 0
fi

# Monitor vindt een verplaatst bestand prima: hij blijft naar de oude inode
# schrijven. Daarom: kopieer de staart naar een .1 en start de monitor opnieuw
# (de nieuwe run schrijft weer naar een vers logbestand).
mv "$LOG" "$LOG.1"
bash "$(dirname "${BASH_SOURCE[0]}")/start-register-monitor.sh" >/dev/null 2>&1 || true
echo "log geroteerd: $LOG.1"
