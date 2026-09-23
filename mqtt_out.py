"""Optionele publicatie via MQTT (paho-mqtt).

Als paho-mqtt niet geïnstalleerd is, geven publish() en publish_command()
netjes False terug zodat de rest van het programma gewoon werkt.
"""

from __future__ import annotations

import json
import sys
from typing import Dict, List, Tuple

# Hoe lang wachten op de broker-bevestiging (PUBACK bij QoS >= 1) voordat we
# een publicatie als mislukt beschouwen.
PUBLISH_CONFIRM_TIMEOUT = 6.0


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


def _publish_messages(
    mqtt_config: Dict,
    messages: List[Tuple[str, object]],
    qos: int | None = None,
    retain: bool | None = None,
) -> Dict:
    """Verbind en publiceer [(topic, payload), ...] en wacht per bericht op
    bevestiging: bij QoS >= 1 tot de broker een PUBACK stuurt, bij QoS 0 tot
    het bericht op de socket staat.

    Retourneert {"ok": bool, "detail": str} zodat de aanroeper kan laten
    zien dat een publicatie écht bevestigd is (of waarom niet).
    """
    if not mqtt_config.get("enabled"):
        return {"ok": False, "detail": "mqtt.enabled = false"}
    mqtt = _paho()
    if mqtt is None:
        return {"ok": False, "detail": "paho-mqtt niet geïnstalleerd"}
    if qos is None:
        qos = int(mqtt_config.get("qos", 0))
    if retain is None:
        retain = bool(mqtt_config.get("retain", False))

    ok = False
    detail = "onbekend"
    client = None
    try:
        client = _make_client(mqtt)
        _connect(client, mqtt_config)
        client.loop_start()
        for topic, payload in messages:
            payload_str = json.dumps(payload, ensure_ascii=False, default=str)
            info = client.publish(topic, payload_str, qos=qos, retain=retain)
            rc = info.rc if hasattr(info, "rc") else mqtt.MQTT_ERR_SUCCESS
            if rc != mqtt.MQTT_ERR_SUCCESS:
                detail = f"rc={rc} (publish geweigerd door de client)"
                break
            try:
                info.wait_for_publish(timeout=PUBLISH_CONFIRM_TIMEOUT)
                if info.is_published():
                    ok = True
                    detail = "bevestigd" if qos >= 1 else "op de socket gezet (qos 0)"
                else:
                    detail = f"geen bevestiging binnen {PUBLISH_CONFIRM_TIMEOUT:g}s"
                    break
            except Exception as exc:  # timeout e.d.
                detail = f"geen bevestiging binnen {PUBLISH_CONFIRM_TIMEOUT:g}s ({exc})"
                break
    except OSError as exc:
        detail = str(exc) or exc.__class__.__name__
    except Exception as exc:  # noqa: BLE001 — MQTT mag nooit de run breken
        detail = f"{exc.__class__.__name__}: {exc}"
    finally:
        if client is not None:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:  # noqa: BLE001 — opruimen mag nooit falen afdwingen
                pass
    return {"ok": ok, "detail": detail}


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
    return _publish_messages(mqtt_config, messages)["ok"]


def publish_command(
    mqtt_config: Dict,
    topic: str,
    payload: object,
    qos: int | None = None,
    retain: bool | None = None,
) -> Tuple[bool, str]:
    """Publiceer één bericht op een exact topic (bv. het SWW-boost-commando).

    Geeft (ok, detail) terug; detail vertelt of de broker de ontvangst
    bevestigd heeft.
    """
    res = _publish_messages(mqtt_config, [(topic, payload)], qos=qos, retain=retain)
    return res["ok"], res["detail"]


if __name__ == "__main__":
    # Snel zelf-testje vanaf de CLI:
    #   python3 mqtt_out.py 192.168.1.1 1883 topic/waarde '{"a":1}'
    # drukt 'ok: True detail: bevestigd' als de broker het acknowledge't.
    if len(sys.argv) == 5:
        cfg = {
            "enabled": True,
            "host": sys.argv[1],
            "port": int(sys.argv[2]),
            "username": None,
            "password": None,
            "qos": 1,
        }
        ok, detail = publish_command(cfg, sys.argv[3], sys.argv[4])
        print(f"ok: {ok} detail: {detail}")