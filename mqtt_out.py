"""Optionele publicatie van het advies via MQTT (paho-mqtt).

Als paho-mqtt niet geïnstalleerd is, geeft publish() netjes False terug
zodat de rest van het programma gewoon werkt.
"""

from __future__ import annotations

import json
from typing import Dict, Optional


def publish(
    mqtt_config: Dict,
    payloads: Dict[str, object],
) -> bool:
    """Publiceer {topic_key: payload} op <topic_base>/<topic_key>."""
    if not mqtt_config.get("enabled"):
        return False
    try:
        import paho.mqtt.client as mqtt  # optionele dependency
    except ImportError:
        return False

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    username = mqtt_config.get("username")
    password = mqtt_config.get("password")
    if username:
        client.username_pw_set(username, password)
    client.connect(
        mqtt_config["host"],
        int(mqtt_config["port"]),
        keepalive=10,
    )
    client.loop_start()
    base = mqtt_config.get("topic_base", "remko/wkf70").rstrip("/")
    qos = int(mqtt_config.get("qos", 0))
    for key, payload in payloads.items():
        client.publish(
            f"{base}/{key}",
            json.dumps(payload, ensure_ascii=False, default=str),
            qos=qos,
        )
    client.loop_stop()
    client.disconnect()
    return True