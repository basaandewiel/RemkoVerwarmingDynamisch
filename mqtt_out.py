"""Optionele publicatie via MQTT (paho-mqtt).

Als paho-mqtt niet geïnstalleerd is, geven publish() en publish_command()
netjes False terug zodat de rest van het programma gewoon werkt.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Dict, List, Tuple


def _paho():
    """Importeer paho-mqtt; None als het niet geïnstalleerd is."""
    try:
        import paho.mqtt.client as mqtt  # optionele dependency
        return mqtt
    except ImportError:
        return None


def _make_client(mqtt):
    """Maak een client zowel voor paho-mqtt 2.x als 1.x (CallbackAPIVersion
    bestaat alleen in 2.x; 1.x accepteert geen extra argument)."""
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    return mqtt.Client()  # paho-mqtt 1.x


def _connect(client, mqtt_config: Dict) -> None:
    username = mqtt_config.get("username")
    password = mqtt_config.get("password")
    if username:
        client.username_pw_set(username, password)
    client.connect(
        mqtt_config["host"],
        int(mqtt_config["port"]),
        keepalive=10,
    )


def _publish_messages(mqtt_config: Dict, messages: List[Tuple[str, object]]) -> bool:
    """Verbind en publiceer [(topic, payload), ...]. True als het gelukt is."""
    if not mqtt_config.get("enabled"):
        return False
    mqtt = _paho()
    if mqtt is None:
        return False
    try:
        client = _make_client(mqtt)
        _connect(client, mqtt_config)
        client.loop_start()
        qos = int(mqtt_config.get("qos", 0))
        for topic, payload in messages:
            client.publish(
                topic,
                json.dumps(payload, ensure_ascii=False, default=str),
                qos=qos,
            )
        time.sleep(0.2)  # geef de netwerkloop even de kans te flushen
        client.loop_stop()
        client.disconnect()
        return True
    except OSError as exc:
        print(f"FOUT: MQTT-publicatie mislukt: {exc}", file=sys.stderr)
        return False
    except Exception as exc:  # noqa: BLE001 — MQTT mag nooit de run breken
        print(f"FOUT: MQTT-publicatie mislukt: {exc}", file=sys.stderr)
        return False


def publish(
    mqtt_config: Dict,
    payloads: Dict[str, object],
) -> bool:
    """Publiceer {topic_key: payload} op <topic_base>/<topic_key>."""
    if not mqtt_config.get("enabled"):
        return False
    base = mqtt_config.get("topic_base", "remko/wkf70").rstrip("/")
    messages = [
        (f"{base}/{key}", payload) for key, payload in payloads.items()
    ]
    return _publish_messages(mqtt_config, messages)


def publish_command(
    mqtt_config: Dict,
    topic: str,
    payload: object,
) -> bool:
    """Publiceer één bericht op een exact topic (bv. het SWW-boost-commando)."""
    return _publish_messages(mqtt_config, [(topic, payload)])