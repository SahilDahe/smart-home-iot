from __future__ import annotations

import atexit
import json
import logging
import math
import os
import threading
import time
import uuid
from collections.abc import Mapping
from typing import Any

import cv2
import mediapipe as mp
import numpy as np
from flask import Flask, Response, jsonify, render_template, request
from flask_socketio import SocketIO

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None


# ============================================================
# CONFIGURATION
# ============================================================

def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)

    if value is None:
        return default

    return value.strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


APP_HOST = os.getenv(
    "SMART_HOME_HOST",
    "0.0.0.0"
)

APP_PORT = env_int(
    "SMART_HOME_PORT",
    5001
)

CAMERA_INDEX = env_int(
    "CAMERA_INDEX",
    0
)

CAMERA_WIDTH = env_int(
    "CAMERA_WIDTH",
    640
)

CAMERA_HEIGHT = env_int(
    "CAMERA_HEIGHT",
    480
)


# ============================================================
# MQTT
# ============================================================

MQTT_ENABLED = env_flag(
    "MQTT_ENABLED",
    False
)

MQTT_BROKER = os.getenv(
    "MQTT_BROKER",
    "broker.hivemq.com"
)

MQTT_PORT = env_int(
    "MQTT_PORT",
    1883
)

MQTT_USERNAME = os.getenv(
    "MQTT_USERNAME"
)

MQTT_PASSWORD = os.getenv(
    "MQTT_PASSWORD"
)

MQTT_TOPIC_PREFIX = os.getenv(
    "MQTT_TOPIC_PREFIX",
    "home"
).strip("/")

MQTT_STATUS_TOPIC = os.getenv(
    "MQTT_STATUS_TOPIC",
    ""
).strip()

MQTT_QOS = max(
    0,
    min(
        env_int("MQTT_QOS", 1),
        2
    )
)

MQTT_RETAIN = env_flag(
    "MQTT_RETAIN",
    False
)


# ============================================================
# GESTURE CONFIGURATION
# ============================================================

# Number of consecutive matching MediaPipe frames required
# before a gesture becomes stable.
GESTURE_STABLE_FRAMES = max(
    5,
    env_int(
        "GESTURE_STABLE_FRAMES",
        8
    )
)

# Every normal gesture must be held for this long.
GESTURE_HOLD_SECONDS = max(
    1.0,
    env_float(
        "GESTURE_HOLD_SECONDS",
        3.0
    )
)

# Delay between repeated executions of the same gesture.
GESTURE_COOLDOWN_SECONDS = max(
    0.5,
    env_float(
        "GESTURE_COOLDOWN_SECONDS",
        1.2
    )
)

# SOS sequence must be completed inside this window.
SOS_MAX_SECONDS = max(
    1.0,
    env_float(
        "SOS_MAX_SECONDS",
        3.0
    )
)

GESTURE_UI_UPDATE_SECONDS = 0.10


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=os.getenv(
        "LOG_LEVEL",
        "INFO"
    ).upper()
)

logger = logging.getLogger(
    "smart-home"
)


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)

app.config["SECRET_KEY"] = os.getenv(
    "FLASK_SECRET_KEY",
    "smart-home-secret"
)

app.config[
    "SEND_FILE_MAX_AGE_DEFAULT"
] = 0

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading"
)


# ============================================================
# GLOBAL STATE
# ============================================================

state: dict[str, Any] = {
    "light1": "OFF",
    "light2": "OFF",
    "fan": "OFF",
    "pump": "OFF",

    "door": "LOCKED",

    "temperature": "24°C",
    "humidity": "60%",

    "gesture": "NONE",
    "gesture_progress": "0",
    "gesture_phase": "READY",
    "gesture_action": "Show a gesture to begin",

    "alert": "NORMAL",
    "sos": "INACTIVE",

    "mqtt": (
        "DISABLED"
        if not MQTT_ENABLED
        else "CONNECTING"
    ),

    "esp": "OFFLINE",
    "motion": "CLEAR",
}

state_lock = threading.RLock()


DEVICE_ACTIONS: dict[str, set[str]] = {
    "light1": {"ON", "OFF"},
    "light2": {"ON", "OFF"},
    "fan": {"ON", "OFF"},
    "pump": {"ON", "OFF"},
    "door": {"LOCKED", "UNLOCKED"},
}


ACTION_ALIASES = {
    "1": "ON",
    "0": "OFF",
    "TRUE": "ON",
    "FALSE": "OFF",
    "LOCK": "LOCKED",
    "UNLOCK": "UNLOCKED",
}


def state_snapshot() -> dict[str, Any]:
    with state_lock:
        return dict(state)


def update_state(
    changes: Mapping[str, Any],
    *,
    emit: bool = True,
) -> dict[str, Any]:

    normalized = {
        str(key): value
        for key, value in changes.items()
    }

    with state_lock:

        changed = any(
            state.get(key) != value
            for key, value in normalized.items()
        )

        if changed:
            state.update(normalized)

        snapshot = dict(state)

    if changed and emit:
        socketio.emit(
            "state",
            snapshot
        )

    return snapshot


# ============================================================
# DEVICE VALIDATION
# ============================================================

def normalise_control(
    device: Any,
    action: Any,
) -> tuple[str, str]:

    device_name = str(
        device
    ).strip().lower()

    raw_action = str(
        action
    ).strip().upper()

    action_name = ACTION_ALIASES.get(
        raw_action,
        raw_action
    )

    if device_name not in DEVICE_ACTIONS:
        raise ValueError(
            f"Unknown device: {device_name}"
        )

    if action_name not in DEVICE_ACTIONS[
        device_name
    ]:
        raise ValueError(
            f"Unsupported action for "
            f"{device_name}: {action_name}"
        )

    return (
        device_name,
        action_name
    )


# ============================================================
# MQTT BRIDGE
# ============================================================

class MqttBridge:

    def __init__(self) -> None:
        self.client: Any | None = None
        self._started = False
        self._lock = threading.Lock()

    def start(self) -> None:

        if not MQTT_ENABLED:
            update_state({
                "mqtt": "DISABLED"
            })
            return

        if mqtt is None:

            logger.warning(
                "paho-mqtt is not installed."
            )

            update_state({
                "mqtt": "UNAVAILABLE",
                "alert": "MQTT_LIBRARY_MISSING"
            })

            return

        with self._lock:

            if self._started:
                return

            self._started = True

            client_id = (
                "smart-home-flask-"
                + uuid.uuid4().hex[:10]
            )

            self.client = mqtt.Client(
                client_id=client_id,
                protocol=mqtt.MQTTv311
            )

            self.client.on_connect = (
                self._on_connect
            )

            self.client.on_disconnect = (
                self._on_disconnect
            )

            self.client.on_message = (
                self._on_message
            )

            self.client.reconnect_delay_set(
                min_delay=1,
                max_delay=30
            )

            if MQTT_USERNAME:
                self.client.username_pw_set(
                    MQTT_USERNAME,
                    MQTT_PASSWORD
                )

            try:

                self.client.connect_async(
                    MQTT_BROKER,
                    MQTT_PORT,
                    keepalive=45
                )

                self.client.loop_start()

                update_state({
                    "mqtt": "CONNECTING"
                })

                logger.info(
                    "MQTT connecting to %s:%s",
                    MQTT_BROKER,
                    MQTT_PORT
                )

            except Exception as exc:

                logger.warning(
                    "MQTT startup failed: %s",
                    exc
                )

                update_state({
                    "mqtt": "ERROR"
                })

    def stop(self) -> None:

        if self.client is None:
            return

        try:
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass

    def publish_command(
        self,
        device: str,
        value: str,
    ) -> bool:

        if not MQTT_ENABLED:
            return False

        self.start()

        if self.client is None or mqtt is None:
            return False

        topic = (
            f"{MQTT_TOPIC_PREFIX}/{device}"
            if MQTT_TOPIC_PREFIX
            else device
        )

        try:

            result = self.client.publish(
                topic,
                value,
                qos=MQTT_QOS,
                retain=MQTT_RETAIN
            )

            if result.rc != mqtt.MQTT_ERR_SUCCESS:

                logger.warning(
                    "MQTT publish failed: %s",
                    result.rc
                )

                update_state({
                    "mqtt": "DISCONNECTED"
                })

                return False

            logger.info(
                "MQTT command: %s -> %s",
                topic,
                value
            )

            return True

        except Exception as exc:

            logger.warning(
                "MQTT publish exception: %s",
                exc
            )

            update_state({
                "mqtt": "ERROR"
            })

            return False

    def _on_connect(
        self,
        client: Any,
        userdata: Any,
        flags: Any,
        reason_code: Any,
        properties: Any = None,
    ) -> None:

        if reason_code != 0:

            update_state({
                "mqtt": "ERROR",
                "esp": "OFFLINE"
            })

            return

        logger.info("MQTT connected.")

        update_state({
            "mqtt": "CONNECTED",
            "esp": "ONLINE"
        })

        if MQTT_STATUS_TOPIC:

            try:
                client.subscribe(
                    MQTT_STATUS_TOPIC,
                    qos=MQTT_QOS
                )
            except Exception:
                pass

    def _on_disconnect(
        self,
        client: Any,
        userdata: Any,
        *args: Any,
    ) -> None:

        if MQTT_ENABLED:

            update_state({
                "mqtt": "DISCONNECTED",
                "esp": "OFFLINE"
            })

    def _on_message(
        self,
        client: Any,
        userdata: Any,
        message: Any,
    ) -> None:

        try:

            payload = (
                message.payload
                .decode("utf-8")
                .strip()
            )

        except UnicodeDecodeError:
            return

        apply_mqtt_status(
            message.topic,
            payload
        )


mqtt_bridge = MqttBridge()


# ============================================================
# MQTT STATUS PARSING
# ============================================================

def sensor_value(
    value: Any,
    unit: str,
) -> str:

    text = str(value).strip()

    if unit in text:
        return text

    return f"{text}{unit}"


def apply_mqtt_status(
    topic: str,
    payload: str,
) -> None:

    try:
        decoded = json.loads(payload)
    except (ValueError, TypeError):
        decoded = None

    if isinstance(decoded, Mapping):

        changes: dict[str, Any] = {}

        for key, value in decoded.items():

            field = str(key).lower()

            if field in DEVICE_ACTIONS:

                changes[field] = str(
                    value
                ).upper()

            elif field in {
                "temperature",
                "temp",
            }:

                changes[
                    "temperature"
                ] = sensor_value(
                    value,
                    "°C"
                )

            elif field in {
                "humidity",
                "humid",
            }:

                changes[
                    "humidity"
                ] = sensor_value(
                    value,
                    "%"
                )

            elif field == "motion":

                changes["motion"] = str(
                    value
                ).upper()

        if changes:
            update_state(changes)

        return

    relative_topic = topic.strip("/")

    if (
        MQTT_TOPIC_PREFIX
        and relative_topic.startswith(
            f"{MQTT_TOPIC_PREFIX}/"
        )
    ):

        relative_topic = relative_topic[
            len(MQTT_TOPIC_PREFIX) + 1:
        ]

    parts = relative_topic.split("/")

    field: str | None = None

    if (
        len(parts) >= 2
        and parts[0] == "status"
    ):

        field = parts[1]

    elif (
        len(parts) >= 2
        and parts[-1] == "status"
    ):

        field = parts[-2]

    elif len(parts) == 1:

        field = parts[0]

    if field in DEVICE_ACTIONS:

        update_state({
            field: payload.upper()
        })

    elif field in {
        "temperature",
        "temp",
    }:

        update_state({
            "temperature":
                sensor_value(
                    payload,
                    "°C"
                )
        })

    elif field in {
        "humidity",
        "humid",
    }:

        update_state({
            "humidity":
                sensor_value(
                    payload,
                    "%"
                )
        })

    elif field == "motion":

        motion = payload.upper()

        update_state({
            "motion": motion,
            "alert": (
                "MOTION"
                if motion in {
                    "ON",
                    "1",
                    "TRUE",
                    "DETECTED",
                }
                else "NORMAL"
            )
        })


# ============================================================
# DEVICE UPDATE
# ============================================================

def update_devices(
    changes: Mapping[str, str],
    *,
    publish: bool = True,
) -> dict[str, Any]:

    cleaned: dict[str, str] = {}

    for device, action in changes.items():

        name, value = normalise_control(
            device,
            action
        )

        cleaned[name] = value

    snapshot = update_state(
        cleaned
    )

    if publish:

        for device, value in cleaned.items():

            mqtt_bridge.publish_command(
                device,
                value
            )

    return snapshot


# ============================================================
# MEDIAPIPE GEOMETRY
# ============================================================

mp_hands = mp.solutions.hands
mp_drawing = mp.solutions.drawing_utils


def distance(
    a: Any,
    b: Any,
) -> float:

    return math.hypot(
        a.x - b.x,
        a.y - b.y
    )


def angle(
    a: Any,
    b: Any,
    c: Any,
) -> float:

    abx = a.x - b.x
    aby = a.y - b.y

    cbx = c.x - b.x
    cby = c.y - b.y

    dot = (
        abx * cbx
        +
        aby * cby
    )

    mag1 = math.hypot(
        abx,
        aby
    )

    mag2 = math.hypot(
        cbx,
        cby
    )

    if mag1 < 1e-6 or mag2 < 1e-6:
        return 0.0

    cosine = dot / (
        mag1 * mag2
    )

    cosine = max(
        -1.0,
        min(
            1.0,
            cosine
        )
    )

    return math.degrees(
        math.acos(cosine)
    )


# ============================================================
# STRONG GESTURE DETECTOR
# ============================================================

class GestureDetector:

    FINGERS = {
        "INDEX": (8, 6, 5),
        "MIDDLE": (12, 10, 9),
        "RING": (16, 14, 13),
        "PINKY": (20, 18, 17),
    }

    @staticmethod
    def finger_extended(
        landmarks: Any,
        tip: int,
        pip: int,
        mcp: int,
        scale: float,
    ) -> bool:

        finger_angle = angle(
            landmarks[mcp],
            landmarks[pip],
            landmarks[tip]
        )

        tip_distance = distance(
            landmarks[tip],
            landmarks[0]
        )

        pip_distance = distance(
            landmarks[pip],
            landmarks[0]
        )

        return (
            finger_angle >= 155.0
            and
            tip_distance
            > pip_distance * 1.10
            and
            tip_distance
            > scale * 1.35
        )

    @staticmethod
    def thumb_down(
        landmarks: Any,
        scale: float,
    ) -> bool:

        thumb_tip = landmarks[4]
        thumb_ip = landmarks[3]
        wrist = landmarks[0]

        vertically_down = (
            thumb_tip.y
            >
            wrist.y + scale * 0.28
            and
            thumb_tip.y
            >
            thumb_ip.y + scale * 0.10
        )

        thumb_distance = distance(
            thumb_tip,
            wrist
        )

        return (
            vertically_down
            and
            thumb_distance
            > scale * 0.75
        )

    def detect(
        self,
        hand: Any,
    ) -> str:

        landmarks = hand.landmark

        scale = max(
            distance(
                landmarks[0],
                landmarks[9]
            ),
            0.06
        )

        extended: list[str] = []

        for name, (
            tip,
            pip,
            mcp,
        ) in self.FINGERS.items():

            if self.finger_extended(
                landmarks,
                tip,
                pip,
                mcp,
                scale
            ):

                extended.append(
                    name
                )

        finger_count = len(
            extended
        )

        # Thumb down
        if (
            finger_count == 0
            and
            self.thumb_down(
                landmarks,
                scale
            )
        ):
            return "THUMB_DOWN"

        # Fist
        if finger_count == 0:
            return "FIST"

        # Index
        if (
            finger_count == 1
            and
            extended[0] == "INDEX"
        ):
            return "INDEX"

        # Two fingers
        if (
            finger_count == 2
            and
            set(extended)
            == {
                "INDEX",
                "MIDDLE",
            }
        ):
            return "TWO"

        # Three fingers
        if (
            finger_count == 3
            and
            set(extended)
            == {
                "INDEX",
                "MIDDLE",
                "RING",
            }
        ):
            return "THREE"

        # Four fingers
        if (
            finger_count == 4
            and
            set(extended)
            == {
                "INDEX",
                "MIDDLE",
                "RING",
                "PINKY",
            }
        ):
            return "FOUR"

        # Palm is deliberately kept as a distinct
        # SOS-sequence gesture.
        if (
            finger_count == 4
            and
            distance(
                landmarks[4],
                landmarks[2]
            )
            > scale * 0.75
        ):
            return "PALM"

        return "NONE"


gesture_detector = GestureDetector()


# ============================================================
# GESTURE STABILIZER
# ============================================================

class GestureStabilizer:

    def __init__(
        self,
        required_frames: int,
    ) -> None:

        self.required_frames = (
            required_frames
        )

        self.candidate = "NONE"
        self.candidate_frames = 0
        self.stable = "NONE"

    def update(
        self,
        raw: str,
    ) -> tuple[str, bool]:

        if raw == self.candidate:

            self.candidate_frames += 1

        else:

            self.candidate = raw
            self.candidate_frames = 1

        changed = False

        if (
            self.candidate_frames
            >= self.required_frames
            and
            self.candidate
            != self.stable
        ):

            self.stable = (
                self.candidate
            )

            changed = True

        return (
            self.stable,
            changed
        )


gesture_stabilizer = (
    GestureStabilizer(
        GESTURE_STABLE_FRAMES
    )
)


# ============================================================
# NORMAL GESTURE HOLD CONTROLLER
# ============================================================

class GestureHoldController:

    GESTURE_INFO = {

        "INDEX": {
            "action": "Toggle Light 1",
            "device": "light1",
        },

        "TWO": {
            "action": "Toggle Light 2",
            "device": "light2",
        },

        "THREE": {
            "action": "Toggle Fan",
            "device": "fan",
        },

        "FOUR": {
            "action": "Toggle Pump",
            "device": "pump",
        },

        "FIST": {
            "action": "Lock Door",
            "device": "door",
        },

        "THUMB_DOWN": {
            "action": "Unlock Door",
            "device": "door",
        },
    }

    def __init__(self) -> None:

        self.current = "NONE"
        self.start_time: float | None = None

        self.triggered = False

        self.last_action_time = 0.0
        self.last_ui_update = 0.0

    def reset(
        self,
        emit: bool = True,
    ) -> None:

        self.current = "NONE"
        self.start_time = None
        self.triggered = False

        if emit:

            update_state({
                "gesture_progress": "0",
                "gesture_phase": "READY",
                "gesture_action":
                    "Show a gesture to begin",
            })

    def process(
        self,
        gesture: str,
    ) -> None:

        now = time.monotonic()

        # No gesture
        if gesture == "NONE":

            if self.current != "NONE":
                logger.info(
                    "Gesture released."
                )

            self.reset()
            return

        # PALM belongs to SOS.
        if gesture == "PALM":
            return

        # Unsupported gesture.
        if gesture not in self.GESTURE_INFO:

            self.current = gesture
            self.start_time = None
            self.triggered = False

            update_state({

                "gesture_progress": "0",

                "gesture_phase":
                    "IGNORED",

                "gesture_action":
                    "Gesture not assigned",
            })

            return

        info = self.GESTURE_INFO[
            gesture
        ]

        # New gesture.
        if gesture != self.current:

            self.current = gesture
            self.start_time = now
            self.triggered = False
            self.last_ui_update = 0.0

            update_state({

                "gesture":
                    gesture,

                "gesture_progress":
                    "0",

                "gesture_phase":
                    "HOLD",

                "gesture_action":
                    info["action"],
            })

            logger.info(
                "Gesture hold started: %s",
                gesture
            )

            return

        # Already executed.
        if self.triggered:

            update_state({

                "gesture_phase":
                    "TRIGGERED",

                "gesture_progress":
                    "100",

            })

            return

        if self.start_time is None:
            self.start_time = now

        elapsed = (
            now - self.start_time
        )

        progress = int(
            min(
                100,
                (
                    elapsed
                    /
                    GESTURE_HOLD_SECONDS
                )
                * 100
            )
        )

        # UI progress.
        if (
            now
            -
            self.last_ui_update
            >=
            GESTURE_UI_UPDATE_SECONDS
        ):

            self.last_ui_update = now

            update_state({

                "gesture":
                    gesture,

                "gesture_progress":
                    str(progress),

                "gesture_phase":
                    "HOLD",

                "gesture_action":
                    info["action"],
            })

        # 3-second hold complete.
        if (
            elapsed
            >=
            GESTURE_HOLD_SECONDS
        ):

            self.triggered = True
            self.last_action_time = now

            self.execute(
                gesture
            )

    def execute(
        self,
        gesture: str,
    ) -> None:

        info = self.GESTURE_INFO[
            gesture
        ]

        device = info["device"]

        # Toggle devices.
        if device in {
            "light1",
            "light2",
            "fan",
            "pump",
        }:

            current = (
                state_snapshot()
                .get(
                    device,
                    "OFF"
                )
            )

            next_value = (
                "OFF"
                if current == "ON"
                else "ON"
            )

            update_devices({
                device:
                    next_value
            })

            update_state({

                "gesture_progress":
                    "100",

                "gesture_phase":
                    "TRIGGERED",

                "gesture_action":
                    f"{info['action']}: "
                    f"{next_value}",
            })

            logger.info(
                "Gesture action: "
                "%s -> %s",
                gesture,
                next_value,
            )

            return

        # Lock door.
        if gesture == "FIST":

            update_devices({
                "door": "LOCKED"
            })

            update_state({

                "gesture_progress":
                    "100",

                "gesture_phase":
                    "TRIGGERED",

                "gesture_action":
                    "Door locked",
            })

            return

        # Unlock door.
        if gesture == "THUMB_DOWN":

            update_devices({
                "door": "UNLOCKED"
            })

            update_state({

                "gesture_progress":
                    "100",

                "gesture_phase":
                    "TRIGGERED",

                "gesture_action":
                    "Door unlocked",
            })


gesture_hold_controller = (
    GestureHoldController()
)


# ============================================================
# SOS CONTROLLER
# ============================================================

class SOSController:

    SEQUENCE = [
        "PALM",
        "FIST",
        "PALM",
        "FIST",
    ]

    def __init__(self) -> None:

        self.index = 0
        self.start_time: float | None = None
        self.last_gesture = "NONE"

    def reset(self) -> None:

        self.index = 0
        self.start_time = None
        self.last_gesture = "NONE"

    def update(
        self,
        gesture: str,
    ) -> bool:

        now = time.monotonic()

        # Hand released.
        if gesture == "NONE":

            self.reset()

            return False

        # Ignore repeated stable state.
        if gesture == self.last_gesture:
            return False

        self.last_gesture = gesture

        # Start sequence.
        if self.index == 0:

            if gesture == self.SEQUENCE[0]:

                self.index = 1

                self.start_time = now

                logger.info(
                    "SOS sequence started."
                )

            return False

        # Timeout.
        if (
            self.start_time is not None
            and
            now - self.start_time
            > SOS_MAX_SECONDS
        ):

            self.reset()

            if (
                gesture
                ==
                self.SEQUENCE[0]
            ):

                self.index = 1
                self.start_time = now

            return False

        # Correct next step.
        if (
            gesture
            ==
            self.SEQUENCE[self.index]
        ):

            self.index += 1

            # Sequence complete.
            if (
                self.index
                >=
                len(self.SEQUENCE)
            ):

                logger.warning(
                    "SOS gesture completed."
                )

                self.reset()

                return True

            return False

        # Wrong gesture.
        if (
            gesture
            ==
            self.SEQUENCE[0]
        ):

            self.index = 1
            self.start_time = now

        else:

            self.reset()

        return False


sos_controller = SOSController()


def activate_sos() -> None:

    logger.warning(
        "SOS ACTIVATED"
    )

    # Turn every light ON.
    update_devices({
        "light1": "ON",
        "light2": "ON",
    })

    update_state({

        "sos":
            "ACTIVE",

        "alert":
            "SOS",

        "gesture_phase":
            "TRIGGERED",

        "gesture_progress":
            "100",

        "gesture_action":
            "SOS ACTIVATED",
    })


# ============================================================
# CAMERA PROCESSING
# ============================================================

def process_camera_frame(
    frame: Any,
    hands: Any,
) -> Any:

    rgb = cv2.cvtColor(
        frame,
        cv2.COLOR_BGR2RGB
    )

    rgb.flags.writeable = False

    results = hands.process(
        rgb
    )

    rgb.flags.writeable = True

    raw_gesture = "NONE"
    handedness = ""

    if results.multi_hand_landmarks:

        hand = (
            results
            .multi_hand_landmarks[0]
        )

        raw_gesture = (
            gesture_detector.detect(
                hand
            )
        )

        if results.multi_handedness:

            handedness = (
                results
                .multi_handedness[0]
                .classification[0]
                .label
            )

        mp_drawing.draw_landmarks(
            frame,
            hand,
            mp_hands.HAND_CONNECTIONS,
        )

    stable_gesture, changed = (
        gesture_stabilizer.update(
            raw_gesture
        )
    )

    if changed:

        update_state({
            "gesture":
                stable_gesture
        })

        # SOS uses:
        # PALM -> FIST -> PALM -> FIST
        if sos_controller.update(
            stable_gesture
        ):

            activate_sos()

    # Normal gesture processing.
    gesture_hold_controller.process(
        stable_gesture
    )

    snapshot = state_snapshot()

    phase = snapshot.get(
        "gesture_phase",
        "READY"
    )

    progress = snapshot.get(
        "gesture_progress",
        "0"
    )

    # Camera HUD.
    if snapshot.get(
        "sos"
    ) == "ACTIVE":

        label = "SOS ACTIVATED"

    elif stable_gesture == "NONE":

        label = "GESTURE: NONE"

    else:

        label = (
            f"GESTURE: "
            f"{stable_gesture}"
        )

        if phase == "HOLD":

            label += (
                f"  HOLD "
                f"{progress}%"
            )

        elif phase == "TRIGGERED":

            label += (
                "  EXECUTED"
            )

    cv2.putText(
        frame,
        label,
        (16, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.60,
        (
            80,
            255,
            210,
        ),
        2,
        cv2.LINE_AA,
    )

    if handedness:

        cv2.putText(
            frame,
            handedness,
            (16, 55),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.40,
            (
                190,
                200,
                215,
            ),
            1,
            cv2.LINE_AA,
        )

    return frame


# ============================================================
# CAMERA WORKER
# ============================================================

class CameraWorker:

    def __init__(self) -> None:

        self.condition = (
            threading.Condition()
        )

        self.thread: (
            threading.Thread
            | None
        ) = None

        self.stop_event = (
            threading.Event()
        )

        self.jpeg: bytes | None = None

        self.sequence = 0

    def start(self) -> None:

        with self.condition:

            if (
                self.thread
                and
                self.thread.is_alive()
            ):
                return

            self.stop_event.clear()

            self.thread = threading.Thread(
                target=self.run,
                name="camera-worker",
                daemon=True,
            )

            self.thread.start()

    def stop(self) -> None:

        self.stop_event.set()

        with self.condition:
            self.condition.notify_all()

    def get_jpeg(
        self,
        previous_sequence: int,
        timeout: float = 5.0,
    ) -> tuple[
        bytes | None,
        int,
    ]:

        self.start()

        deadline = (
            time.monotonic()
            +
            timeout
        )

        with self.condition:

            while (
                self.sequence
                ==
                previous_sequence
                and
                not self.stop_event.is_set()
            ):

                remaining = (
                    deadline
                    -
                    time.monotonic()
                )

                if remaining <= 0:
                    break

                self.condition.wait(
                    remaining
                )

            return (
                self.jpeg,
                self.sequence,
            )

    def run(self) -> None:

        capture = cv2.VideoCapture(
            CAMERA_INDEX
        )

        if not capture.isOpened():

            logger.error(
                "Camera unavailable."
            )

            update_state({
                "alert":
                    "CAMERA_UNAVAILABLE"
            })

            return

        capture.set(
            cv2.CAP_PROP_FRAME_WIDTH,
            CAMERA_WIDTH
        )

        capture.set(
            cv2.CAP_PROP_FRAME_HEIGHT,
            CAMERA_HEIGHT
        )

        try:

            with mp_hands.Hands(
                static_image_mode=False,
                max_num_hands=1,
                model_complexity=1,
                min_detection_confidence=0.72,
                min_tracking_confidence=0.72,
            ) as hands:

                while not self.stop_event.is_set():

                    ok, frame = (
                        capture.read()
                    )

                    if not ok:

                        time.sleep(
                            0.1
                        )

                        continue

                    frame = cv2.flip(
                        frame,
                        1
                    )

                    frame = (
                        process_camera_frame(
                            frame,
                            hands
                        )
                    )

                    encoded, jpeg = (
                        cv2.imencode(
                            ".jpg",
                            frame,
                            [
                                cv2.IMWRITE_JPEG_QUALITY,
                                85,
                            ],
                        )
                    )

                    if not encoded:
                        continue

                    with self.condition:

                        self.jpeg = (
                            jpeg.tobytes()
                        )

                        self.sequence += 1

                        self.condition.notify_all()

        except Exception as exc:

            logger.exception(
                "Camera worker failed: %s",
                exc
            )

            update_state({
                "alert":
                    "CAMERA_ERROR"
            })

        finally:

            capture.release()


camera_worker = CameraWorker()


def unavailable_camera_jpeg() -> bytes:

    image = np.zeros(
        (
            240,
            640,
            3,
        ),
        dtype=np.uint8,
    )

    cv2.putText(
        image,
        "Camera unavailable",
        (175, 120),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (
            90,
            110,
            255,
        ),
        2,
        cv2.LINE_AA,
    )

    _, jpeg = cv2.imencode(
        ".jpg",
        image
    )

    return jpeg.tobytes()


CAMERA_UNAVAILABLE_JPEG = (
    unavailable_camera_jpeg()
)


def camera_stream():

    sequence = -1

    while True:

        jpeg, sequence = (
            camera_worker.get_jpeg(
                sequence
            )
        )

        jpeg = (
            jpeg
            or
            CAMERA_UNAVAILABLE_JPEG
        )

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n"
            +
            jpeg
            +
            b"\r\n"
        )


# ============================================================
# FLASK ROUTES
# ============================================================

@app.route("/")
def index():

    mqtt_bridge.start()

    return render_template(
        "index.html"
    )


@app.route("/video_feed")
def video_feed():

    mqtt_bridge.start()

    return Response(
        camera_stream(),
        mimetype=(
            "multipart/x-mixed-replace;"
            " boundary=frame"
        ),
        headers={
            "Cache-Control":
                "no-store, "
                "no-cache, "
                "must-revalidate, "
                "max-age=0"
        },
    )


@app.route("/status")
def status():

    mqtt_bridge.start()

    return jsonify(
        state_snapshot()
    )


@app.route(
    "/api/control/<device>/<action>",
    methods=["POST"],
)
def control(
    device: str,
    action: str,
):

    mqtt_bridge.start()

    try:

        device_name, action_name = (
            normalise_control(
                device,
                action
            )
        )

        snapshot = update_devices({
            device_name:
                action_name
        })

        return jsonify({
            "success": True,
            "state": snapshot,
        })

    except ValueError as exc:

        return jsonify({
            "success": False,
            "error": str(exc),
        }), 400


@app.route(
    "/api/control",
    methods=["POST"],
)
def json_control():

    mqtt_bridge.start()

    payload = (
        request.get_json(
            silent=True
        )
        or {}
    )

    try:

        device_name, action_name = (
            normalise_control(
                payload.get("device"),
                payload.get("action")
            )
        )

        snapshot = update_devices({
            device_name:
                action_name
        })

        return jsonify({
            "success": True,
            "state": snapshot,
        })

    except ValueError as exc:

        return jsonify({
            "success": False,
            "error": str(exc),
        }), 400


# ============================================================
# SOS RESET
# ============================================================

@app.route(
    "/api/sos/reset",
    methods=["POST"],
)
def reset_sos():

    update_state({

        "sos":
            "INACTIVE",

        "alert":
            "NORMAL",

        "gesture_progress":
            "0",

        "gesture_phase":
            "READY",

        "gesture_action":
            "SOS acknowledged",
    })

    return jsonify({
        "success": True,
        "state": state_snapshot(),
    })


# ============================================================
# SOCKET.IO
# ============================================================

@socketio.on("connect")
def on_connect():

    mqtt_bridge.start()

    socketio.emit(
        "state",
        state_snapshot()
    )


# ============================================================
# SHUTDOWN
# ============================================================

@atexit.register
def shutdown():

    camera_worker.stop()

    mqtt_bridge.stop()


# ============================================================
# START
# ============================================================

if __name__ == "__main__":

    mqtt_bridge.start()

    socketio.run(
        app,
        host=APP_HOST,
        port=APP_PORT,
        debug=False,
    )