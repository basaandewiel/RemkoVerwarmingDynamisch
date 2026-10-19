#!/usr/bin/env python3
"""Dump álle registers die de Remko-gateway publiceert naar een JSON-bestand.

Doel: ontdekken **welk register de GUI-instelling "vaste waarde" van de
VK-modus bevat**. De gateway publiceert meer dan wij bijhouden; door twee dumps
te vergelijken terwijl je in de GUI de vaste waarde verzet, zie je exact welke
register(s) meebewegen.

Read-only (subscribe + één FORCE_RESPONSE-query).

Gebruik:
  python3 dump_registers.py                 # naar /tmp/remko_dump.json
  python3 dump_registers.py --out a.json    # eigen pad
  python3 dump_registers.py --wait 12       # langer luisteren

Vergelijken (op de Pi of lokaal):
  python3 dump_registers.py --out /tmp/a.json     # vóór het verzetten
  # ... verzet de vaste waarde in de GUI ...
  python3 dump_registers.py --out /tmp/b.json     # ná het verzetten
  python3 compare_dumps.py /tmp/a.json /tmp/b.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Optional

import energy_log
import main as app
import mqtt_out

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def collect(mqtt_cfg: dict, wait: float) -> dict:
    mqtt = mqtt_out._paho()
    data_topic = energy_log.data_topic(mqtt_cfg)
    cmd_topic = energy_log.command_topic(mqtt_cfg)
    seen: dict = {}

    def on_connect(client, _u, _f, _rc, properties=None):
        client.subscribe(data_topic, qos=0)
        client.publish(cmd_topic, json.dumps(
            {"FORCE_RESPONSE": True, "query_list": []}), qos=0)

    def on_message(_client, _u, message):
        if message.topic != data_topic:
            return
        try:
            seen.update(json.loads(message.payload.decode()).get("values", {}))
        except (ValueError, TypeError):
            pass

    client = energy_log._make_connected_client(mqtt, mqtt_cfg)
    client.on_connect = on_connect
    client.on_message = on_message
    client.loop_start()
    time.sleep(wait)
    client.loop_stop()
    client.disconnect()
    return seen


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="Dump alle Remko-registers naar JSON")
    ap.add_argument("--config", default=os.path.join(SCRIPT_DIR, "config.json"))
    ap.add_argument("--out", default="/tmp/remko_dump.json")
    ap.add_argument("--wait", type=float, default=10.0, help="seconden luisteren")
    args = ap.parse_args(argv)

    try:
        cfg = app.load_config(args.config)
    except OSError as exc:
        print(f"FOUT: config niet leesbaar: {exc}", file=sys.stderr)
        return 1
    mqtt_cfg = cfg.get("mqtt") or {}
    if not mqtt_cfg.get("enabled"):
        print("FOUT: mqtt.enabled = false", file=sys.stderr)
        return 1
    if mqtt_out._paho() is None:
        print("FOUT: paho-mqtt niet geïnstalleerd", file=sys.stderr)
        return 1

    seen = collect(mqtt_cfg, args.wait)
    with open(os.path.expanduser(args.out), "w", encoding="utf-8") as fh:
        json.dump(seen, fh, indent=0, sort_keys=True)
    print(f"{len(seen)} registers -> {os.path.expanduser(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
