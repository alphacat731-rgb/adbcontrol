
#!/usr/bin/env python3
"""
ADB Chaos - intelligent Android UI exploration over ADB.

Smart mode inspects Android's UI hierarchy before choosing an action.
It prefers visible/clickable controls, detects scrollable containers, remembers
what it already tried on each screen, and uses screenshots + UI XML as a
lightweight visual/action memory.

It intentionally avoids arbitrary shell execution on the phone and refuses
to click controls whose labels strongly suggest destructive, financial,
account, authentication, or communication actions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
import signal
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


APP_NAME = "ADB CHAOS"
VERSION = "0.2.0"

ANSI_RESET = "\033[0m"
ANSI_CYAN = "\033[96m"
ANSI_GREEN = "\033[92m"
ANSI_YELLOW = "\033[93m"
ANSI_RED = "\033[91m"
ANSI_MAGENTA = "\033[95m"
ANSI_DIM = "\033[2m"
ANSI_BOLD = "\033[1m"

DANGEROUS_LABELS = (
    "delete",
    "remove",
    "uninstall",
    "factory reset",
    "reset all",
    "wipe",
    "format",
    "purchase",
    "buy",
    "checkout",
    "pay",
    "payment",
    "card",
    "bank",
    "transfer",
    "send money",
    "call",
    "dial",
    "sms",
    "message",
    "password",
    "passcode",
    "pin",
    "otp",
    "verification",
    "sign in",
    "log in",
    "login",
    "account",
    "permission",
    "allow",
    "deny",
    "grant",
    "install",
    "download",
    "subscribe",
    "shutdown",
    "power off",
)

INTERESTING_LABELS = {
    "open": 7,
    "next": 7,
    "continue": 7,
    "more": 6,
    "menu": 6,
    "start": 6,
    "play": 6,
    "view": 5,
    "show": 5,
    "explore": 5,
    "details": 5,
    "info": 4,
    "about": 4,
    "ok": 3,
    "done": 3,
    "close": 2,
    "skip": 2,
}

PACKAGE_RE = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+")


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    model: str = ""
    product: str = ""

    @property
    def authorized(self) -> bool:
        return self.state == "device"


@dataclass
class Config:
    serial: str | None
    min_delay: float
    max_delay: float
    screenshot_every: int
    max_screenshots: int
    duration: float | None
    wait_for_device: bool
    preview: bool
    seed: int | None
    mode: str
    dump_ui: bool
    smart_back_after: int
    max_consecutive_no_change: int


@dataclass
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return max(0, self.right - self.left)

    @property
    def height(self) -> int:
        return max(0, self.bottom - self.top)

    @property
    def center(self) -> tuple[int, int]:
        return (
            (self.left + self.right) // 2,
            (self.top + self.bottom) // 2,
        )

    def clamp(self, width: int, height: int) -> "Bounds":
        return Bounds(
            max(0, min(self.left, width)),
            max(0, min(self.top, height)),
            max(0, min(self.right, width)),
            max(0, min(self.bottom, height)),
        )

    def to_string(self) -> str:
        return f"[{self.left},{self.top}][{self.right},{self.bottom}]"


@dataclass
class UiNode:
    index: int
    class_name: str
    text: str
    content_desc: str
    resource_id: str
    package: str
    clickable: bool
    scrollable: bool
    enabled: bool
    visible: bool
    bounds: Bounds

    @property
    def label(self) -> str:
        return (
            self.text.strip()
            or self.content_desc.strip()
            or self.class_name
        )

    @property
    def key(self) -> str:
        return "|".join(
            [
                self.class_name,
                self.text.strip(),
                self.content_desc.strip(),
                self.resource_id,
                self.bounds.to_string(),
            ]
        )

    def safe_for_action(self) -> bool:
        if not self.enabled or not self.visible:
            return False

        if self.bounds.width < 8 or self.bounds.height < 8:
            return False

        label = " ".join(
            [
                self.text,
                self.content_desc,
                self.resource_id,
            ]
        ).lower()

        return not any(
            word in label
            for word in DANGEROUS_LABELS
        )


@dataclass
class UiSnapshot:
    xml: str
    nodes: list[UiNode]
    package: str
    activity: str
    fingerprint: str

    @property
    def clickable(self) -> list[UiNode]:
        return [
            node
            for node in self.nodes
            if node.clickable
            and node.safe_for_action()
        ]

    @property
    def scrollables(self) -> list[UiNode]:
        return [
            node
            for node in self.nodes
            if node.scrollable
            and node.safe_for_action()
        ]


@dataclass
class Brain:
    seen_actions: dict[str, int] = field(default_factory=dict)
    screen_visits: dict[str, int] = field(default_factory=dict)
    no_change_streak: int = 0
    last_fingerprint: str = ""
    total_decisions: int = 0

    def remember(
        self,
        screen_fp: str,
        node: UiNode,
    ) -> int:
        key = f"{screen_fp}|{node.key}"
        self.seen_actions[key] = (
            self.seen_actions.get(key, 0) + 1
        )
        return self.seen_actions[key]


class AdbError(RuntimeError):
    pass


def log(
    message: str,
    color: str = ANSI_RESET,
) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(
        f"{ANSI_DIM}[{stamp}]{ANSI_RESET} "
        f"{color}{message}{ANSI_RESET}",
        flush=True,
    )


def run_adb(
    serial: str,
    *args: str,
    binary: bool = False,
    timeout: float = 15.0,
) -> bytes | str:
    command = ["adb", "-s", serial, *args]

    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AdbError(
            "adb no está instalado. Instálalo con: sudo apt install adb"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbError(
            f"ADB timeout: {' '.join(command)}"
        ) from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode(
            "utf-8",
            errors="replace",
        ).strip()
        raise AdbError(
            stderr
            or f"ADB salió con código {completed.returncode}"
        )

    return (
        completed.stdout
        if binary
        else completed.stdout.decode(
            "utf-8",
            errors="replace",
        )
    )


def ensure_adb() -> None:
    if shutil.which("adb") is None:
        raise AdbError(
            "No encuentro 'adb'. En Debian Trixie puedes instalarlo "
            "con: sudo apt install adb."
        )


def parse_devices(raw: str) -> list[Device]:
    devices: list[Device] = []

    for line in raw.splitlines():
        if (
            not line.strip()
            or line.startswith("List of devices attached")
        ):
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        serial, state = parts[0], parts[1]
        model = ""
        product = ""

        for item in parts[2:]:
            if item.startswith("model:"):
                model = item.split(
                    ":",
                    1,
                )[1].replace("_", " ")
            elif item.startswith("product:"):
                product = item.split(
                    ":",
                    1,
                )[1]

        devices.append(
            Device(
                serial,
                state,
                model,
                product,
            )
        )

    return devices


def list_devices() -> list[Device]:
    try:
        raw = subprocess.run(
            ["adb", "devices", "-l"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AdbError(
            "No encuentro 'adb'. Instálalo con: sudo apt install adb"
        ) from exc

    if raw.returncode != 0:
        raise AdbError(
            raw.stderr.decode(
                "utf-8",
                errors="replace",
            ).strip()
        )

    return parse_devices(
        raw.stdout.decode(
            "utf-8",
            errors="replace",
        )
    )


def choose_device(
    serial: str | None,
) -> Device | None:
    devices = list_devices()

    if serial:
        for device in devices:
            if device.serial == serial:
                return device
        return None

    authorized = [
        device
        for device in devices
        if device.authorized
    ]

    if len(authorized) == 1:
        return authorized[0]

    if len(authorized) > 1:
        log(
            "Hay varios dispositivos autorizados. "
            "Usa --serial SERIAL.",
            ANSI_YELLOW,
        )
        for device in authorized:
            label = device.model or device.serial
            print(
                f"  • {device.serial} ({label})"
            )
        return None

    if devices:
        states = ", ".join(
            f"{device.serial}: {device.state}"
            for device in devices
        )
        log(
            "Dispositivo encontrado, pero ninguno está autorizado: "
            f"{states}",
            ANSI_YELLOW,
        )

    return None


def wait_for_device(
    serial: str | None,
    poll: float = 1.0,
) -> Device:
    while True:
        device = choose_device(serial)

        if device and device.authorized:
            return device

        time.sleep(poll)


def parse_screen_size(
    text: str,
) -> tuple[int, int] | None:
    match = re.search(
        r"(?:Physical|Override) size:\s*(\d+)x(\d+)",
        text,
    )

    if not match:
        match = re.search(
            r"(\d+)x(\d+)",
            text,
        )

    if not match:
        return None

    width, height = map(
        int,
        match.groups(),
    )
    return width, height


def get_screen_size(
    device: Device,
) -> tuple[int, int]:
    output = run_adb(
        device.serial,
        "shell",
        "wm",
        "size",
    )

    parsed = parse_screen_size(
        str(output)
    )

    if not parsed:
        raise AdbError(
            "No pude detectar la resolución del móvil: "
            f"{output!r}"
        )

    return parsed


def get_battery(
    device: Device,
) -> str:
    try:
        output = str(
            run_adb(
                device.serial,
                "shell",
                "dumpsys",
                "battery",
                timeout=8,
            )
        )

        level = re.search(
            r"level:\s*(\d+)",
            output,
        )
        status = re.search(
            r"status:\s*(\d+)",
            output,
        )

        if not level:
            return "?"

        status_name = {
            "2": "charging",
            "3": "discharging",
            "4": "not charging",
            "5": "full",
        }.get(
            status.group(1)
            if status
            else "",
            "",
        )

        return (
            f"{level.group(1)}% {status_name}"
            .strip()
        )

    except AdbError:
        return "?"


def get_current_window(
    device: Device,
) -> tuple[str, str]:
    """Best-effort package/activity detection."""
    for command in (
        (
            "shell",
            "dumpsys",
            "window",
            "windows",
        ),
        (
            "shell",
            "dumpsys",
            "window",
            "displays",
        ),
    ):
        try:
            output = str(
                run_adb(
                    device.serial,
                    *command,
                    timeout=8,
                )
            )
        except AdbError:
            continue

        activity = ""
        package = ""

        for pattern in (
            r"mCurrentFocus=Window\{[^}]+\s+u\d+\s+([^}]+)\}",
            r"mFocusedApp=ActivityRecord\{[^}]+\s+([^/\s]+)/([^}\s]+)",
        ):
            match = re.search(
                pattern,
                output,
            )
            if not match:
                continue

            focus = match.group(1)
            parts = focus.split("/")
            package = parts[0]
            activity = (
                "/".join(parts[1:])
                if len(parts) > 1
                else ""
            )

            if "." in package:
                return package, activity

        candidates = PACKAGE_RE.findall(
            output
        )

        if candidates:
            package = candidates[-1]

        if package:
            return package, activity

    return "", ""


def parse_bounds(
    value: str,
) -> Bounds | None:
    match = re.fullmatch(
        r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
        value.strip(),
    )

    if not match:
        return None

    left, top, right, bottom = map(
        int,
        match.groups(),
    )
    return Bounds(
        left,
        top,
        right,
        bottom,
    )


def parse_ui_xml(
    xml: str,
) -> list[UiNode]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise AdbError(
            f"La jerarquía UI no es XML válido: {exc}"
        ) from exc

    nodes: list[UiNode] = []

    for index, element in enumerate(
        root.iter("node")
    ):
        attrs = element.attrib
        bounds = parse_bounds(
            attrs.get("bounds", "")
        )

        if bounds is None:
            continue

        nodes.append(
            UiNode(
                index=index,
                class_name=attrs.get("class", ""),
                text=attrs.get("text", ""),
                content_desc=attrs.get(
                    "content-desc",
                    "",
                ),
                resource_id=attrs.get(
                    "resource-id",
                    "",
                ),
                package=attrs.get(
                    "package",
                    "",
                ),
                clickable=(
                    attrs.get(
                        "clickable",
                        "false",
                    ).lower()
                    == "true"
                ),
                scrollable=(
                    attrs.get(
                        "scrollable",
                        "false",
                    ).lower()
                    == "true"
                ),
                enabled=(
                    attrs.get(
                        "enabled",
                        "true",
                    ).lower()
                    == "true"
                ),
                visible=(
                    attrs.get(
                        "visible-to-user",
                        "true",
                    ).lower()
                    == "true"
                ),
                bounds=bounds,
            )
        )

    return nodes


def dump_ui(
    device: Device,
) -> str:
    remote = "/sdcard/adbchaos_window.xml"

    output = str(
        run_adb(
            device.serial,
            "shell",
            "uiautomator",
            "dump",
            remote,
            timeout=20.0,
        )
    )

    if "ERROR" in output.upper():
        raise AdbError(
            f"uiautomator dump falló: {output.strip()}"
        )

    return str(
        run_adb(
            device.serial,
            "shell",
            "cat",
            remote,
            timeout=10.0,
        )
    )


def make_ui_snapshot(
    device: Device,
) -> UiSnapshot:
    xml = dump_ui(device)
    nodes = parse_ui_xml(xml)
    package, activity = get_current_window(device)

    compact = []

    for node in nodes:
        if not node.visible:
            continue

        compact.append(
            (
                node.class_name,
                node.text.strip(),
                node.content_desc.strip(),
                node.resource_id,
                node.clickable,
                node.scrollable,
                node.bounds.to_string(),
            )
        )

    payload = json.dumps(
        {
            "package": package,
            "activity": activity,
            "nodes": compact,
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")

    fingerprint = hashlib.sha1(
        payload
    ).hexdigest()[:12]

    return UiSnapshot(
        xml=xml,
        nodes=nodes,
        package=package,
        activity=activity,
        fingerprint=fingerprint,
    )


def safe_name(
    value: str,
) -> str:
    value = re.sub(
        r"[^A-Za-z0-9._-]+",
        "_",
        value,
    )
    return (
        value.strip("._-")
        or "device"
    )


def make_session_dir(
    device: Device,
) -> Path:
    stamp = datetime.now().strftime(
        "%Y-%m-%d_%H-%M-%S"
    )
    folder = (
        Path("sessions")
        / f"{stamp}_{safe_name(device.serial)}"
    )
    folder.mkdir(
        parents=True,
        exist_ok=True,
    )
    return folder


def save_metadata(
    session_dir: Path,
    device: Device,
    size: tuple[int, int],
    config: Config,
) -> None:
    metadata = {
        "app": APP_NAME,
        "version": VERSION,
        "started_at": datetime.now().isoformat(
            timespec="seconds"
        ),
        "device": {
            "serial": device.serial,
            "state": device.state,
            "model": device.model,
            "product": device.product,
        },
        "screen": {
            "width": size[0],
            "height": size[1],
        },
        "mode": config.mode,
        "config": {
            "min_delay": config.min_delay,
            "max_delay": config.max_delay,
            "screenshot_every": (
                config.screenshot_every
            ),
            "max_screenshots": (
                config.max_screenshots
            ),
            "duration": config.duration,
            "preview": config.preview,
            "seed": config.seed,
            "dump_ui": config.dump_ui,
            "smart_back_after": (
                config.smart_back_after
            ),
            "max_consecutive_no_change": (
                config.max_consecutive_no_change
            ),
        },
    }

    (session_dir / "session.json").write_text(
        json.dumps(
            metadata,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def append_event(
    session_dir: Path,
    event: dict,
) -> None:
    path = session_dir / "events.jsonl"

    with path.open(
        "a",
        encoding="utf-8",
    ) as handle:
        handle.write(
            json.dumps(
                event,
                ensure_ascii=False,
            )
            + "\n"
        )


def capture_screenshot(
    device: Device,
    session_dir: Path,
    index: int,
) -> Path:
    target = (
        session_dir
        / f"{index:05d}.png"
    )

    png = run_adb(
        device.serial,
        "exec-out",
        "screencap",
        "-p",
        binary=True,
        timeout=20.0,
    )

    if (
        not isinstance(png, bytes)
        or not png.startswith(b"\x89PNG")
    ):
        raise AdbError(
            "ADB no devolvió una captura PNG válida."
        )

    target.write_bytes(png)
    return target


def save_ui_xml(
    session_dir: Path,
    index: int,
    xml: str,
) -> Path:
    folder = session_dir / "ui"
    folder.mkdir(
        exist_ok=True
    )

    target = (
        folder
        / f"{index:05d}.xml"
    )
    target.write_text(
        xml,
        encoding="utf-8",
    )
    return target


def maybe_preview(
    path: Path,
) -> None:
    if shutil.which("chafa") is None:
        return

    try:
        completed = subprocess.run(
            [
                "chafa",
                "--format=symbols",
                "--size=48x20",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )

        if completed.returncode == 0:
            print(
                completed.stdout.decode(
                    "utf-8",
                    errors="replace",
                )
            )

    except (
        subprocess.TimeoutExpired,
        OSError,
    ):
        pass


def action_key(
    device: Device,
    keycode: str,
    label: str,
) -> str:
    run_adb(
        device.serial,
        "shell",
        "input",
        "keyevent",
        keycode,
    )
    return label


def smart_score(
    node: UiNode,
    snapshot: UiSnapshot,
    brain: Brain,
    width: int,
    height: int,
) -> float:
    score = 1.0
    label = node.label.lower().strip()
    compact = re.sub(
        r"\s+",
        " ",
        label,
    )

    for key, bonus in INTERESTING_LABELS.items():
        if (
            compact == key
            or compact.startswith(key + " ")
        ):
            score += bonus
            break

    area = (
        node.bounds.width
        * node.bounds.height
    )
    screen_area = max(
        1,
        width * height,
    )
    ratio = area / screen_area

    if 0.002 <= ratio <= 0.20:
        score += 2.0
    elif ratio > 0.55:
        score -= 3.0

    center_x, center_y = node.bounds.center
    if (
        height * 0.05
        < center_y
        < height * 0.95
    ):
        score += 0.8

    seen = brain.seen_actions.get(
        f"{snapshot.fingerprint}|{node.key}",
        0,
    )
    score -= min(
        8.0,
        seen * 3.0,
    )

    if (
        node.text.strip()
        or node.content_desc.strip()
    ):
        score += 1.3

    return max(
        0.1,
        score,
    )


def choose_smart_node(
    snapshot: UiSnapshot,
    brain: Brain,
    width: int,
    height: int,
) -> UiNode | None:
    candidates = snapshot.clickable

    if not candidates:
        return None

    scored = [
        (
            smart_score(
                node,
                snapshot,
                brain,
                width,
                height,
            ),
            node,
        )
        for node in candidates
    ]

    scored.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    top = scored[
        : min(6, len(scored))
    ]

    weights = [
        max(score, 0.1) ** 2
        for score, _ in top
    ]

    return random.choices(
        [
            node
            for _, node in top
        ],
        weights=weights,
        k=1,
    )[0]


def smart_tap(
    device: Device,
    node: UiNode,
    width: int,
    height: int,
) -> str:
    bounds = node.bounds.clamp(
        width,
        height,
    )

    x, y = bounds.center

    jitter_x = min(
        max(2, bounds.width // 6),
        24,
    )
    jitter_y = min(
        max(2, bounds.height // 6),
        24,
    )

    x = max(
        bounds.left + 1,
        min(
            bounds.right - 1,
            x + random.randint(
                -jitter_x,
                jitter_x,
            ),
        ),
    )

    y = max(
        bounds.top + 1,
        min(
            bounds.bottom - 1,
            y + random.randint(
                -jitter_y,
                jitter_y,
            ),
        ),
    )

    run_adb(
        device.serial,
        "shell",
        "input",
        "tap",
        str(x),
        str(y),
    )

    return (
        f"smart tap ({x},{y}) "
        f"[{node.label[:70]}]"
    )


def smart_swipe(
    device: Device,
    target: UiNode | Bounds,
    width: int,
    height: int,
) -> str:
    bounds = (
        target.bounds
        if isinstance(target, UiNode)
        else target
    ).clamp(
        width,
        height,
    )

    if (
        bounds.width < 20
        or bounds.height < 20
    ):
        bounds = Bounds(
            int(width * 0.10),
            int(height * 0.18),
            int(width * 0.90),
            int(height * 0.82),
        )

    horizontal = (
        bounds.width
        > bounds.height * 1.35
    )

    if horizontal:
        y = (
            bounds.top
            + bounds.bottom
        ) // 2

        left = (
            bounds.left
            + max(
                10,
                bounds.width // 6,
            )
        )
        right = (
            bounds.right
            - max(
                10,
                bounds.width // 6,
            )
        )

        x1, x2 = right, left
        y1, y2 = y, y
    else:
        x = (
            bounds.left
            + bounds.right
        ) // 2

        top = (
            bounds.top
            + max(
                10,
                bounds.height // 6,
            )
        )
        bottom = (
            bounds.bottom
            - max(
                10,
                bounds.height // 6,
            )
        )

        x1, x2 = x, x
        y1, y2 = bottom, top

    duration_ms = random.randint(
        350,
        850,
    )

    run_adb(
        device.serial,
        "shell",
        "input",
        "swipe",
        str(x1),
        str(y1),
        str(x2),
        str(y2),
        str(duration_ms),
    )

    direction = (
        "horizontal"
        if horizontal
        else "vertical"
    )

    return (
        f"smart swipe {direction} "
        f"{bounds.to_string()} "
        f"{duration_ms}ms"
    )


def run_random_mode_action(
    device: Device,
    width: int,
    height: int,
) -> str:
    action = random.choices(
        [
            "tap",
            "swipe",
            "back",
            "home",
            "volume",
        ],
        weights=[
            36,
            29,
            20,
            5,
            10,
        ],
        k=1,
    )[0]

    if action == "tap":
        x = random.randint(
            int(width * 0.08),
            int(width * 0.92),
        )
        y = random.randint(
            int(height * 0.08),
            int(height * 0.92),
        )

        run_adb(
            device.serial,
            "shell",
            "input",
            "tap",
            str(x),
            str(y),
        )

        return (
            f"random tap ({x},{y})"
        )

    if action == "swipe":
        return smart_swipe(
            device,
            Bounds(
                int(width * 0.10),
                int(height * 0.15),
                int(width * 0.90),
                int(height * 0.85),
            ),
            width,
            height,
        )

    if action == "back":
        return action_key(
            device,
            "4",
            "Back",
        )

    if action == "home":
        return action_key(
            device,
            "3",
            "Home",
        )

    return action_key(
        device,
        random.choice(
            [
                "24",
                "25",
            ]
        ),
        "Volume change",
    )


def perform_smart_action(
    device: Device,
    snapshot: UiSnapshot,
    brain: Brain,
    width: int,
    height: int,
    back_after: int,
) -> tuple[str, str | None]:
    brain.total_decisions += 1

    brain.screen_visits[
        snapshot.fingerprint
    ] = brain.screen_visits.get(
        snapshot.fingerprint,
        0,
    ) + 1

    if (
        brain.no_change_streak
        >= back_after
        and brain.no_change_streak
        >= 2
    ):
        return (
            action_key(
                device,
                "4",
                "Smart Back (stuck)",
            ),
            None,
        )

    candidates = snapshot.clickable
    scrollables = snapshot.scrollables

    if not candidates and scrollables:
        node = random.choice(
            scrollables
        )

        return (
            smart_swipe(
                device,
                node,
                width,
                height,
            ),
            node.key,
        )

    node = choose_smart_node(
        snapshot,
        brain,
        width,
        height,
    )

    if node is None:
        if scrollables:
            node = random.choice(
                scrollables
            )

            return (
                smart_swipe(
                    device,
                    node,
                    width,
                    height,
                ),
                node.key,
            )

        return (
            action_key(
                device,
                "4",
                "Smart Back (no actionable UI)",
            ),
            None,
        )

    if (
        scrollables
        and random.random() < 0.18
    ):
        container = random.choice(
            scrollables
        )

        return (
            smart_swipe(
                device,
                container,
                width,
                height,
            ),
            container.key,
        )

    description = smart_tap(
        device,
        node,
        width,
        height,
    )

    use_count = brain.remember(
        snapshot.fingerprint,
        node,
    )

    return (
        f"{description} [trial {use_count}]",
        node.key,
    )


def run_chaos(
    device: Device,
    config: Config,
) -> None:
    width, height = get_screen_size(
        device
    )

    session_dir = make_session_dir(
        device
    )

    save_metadata(
        session_dir,
        device,
        (
            width,
            height,
        ),
        config,
    )

    brain = Brain()
    stop_requested = False

    def stop_handler(
        signum: int,
        frame: object,
    ) -> None:
        nonlocal stop_requested

        stop_requested = True
        print()

        log(
            "Parada solicitada. Terminando "
            "después de la acción actual...",
            ANSI_YELLOW,
        )

    old_sigint = signal.signal(
        signal.SIGINT,
        stop_handler,
    )
    old_sigterm = signal.signal(
        signal.SIGTERM,
        stop_handler,
    )

    action_no = 0
    screenshot_no = 0
    ui_no = 0
    started = time.monotonic()
    announced_screenshot_cap = False
    announced_ui_cap = False
    previous_screen_fp = ""

    log(
        f"{ANSI_BOLD}{APP_NAME} "
        f"{VERSION}{ANSI_RESET}",
        ANSI_CYAN,
    )

    mode_title = (
        "INTELLIGENT EXPLORATION"
        if config.mode == "smart"
        else "RANDOM MODE"
    )

    log(
        f"Mode: {mode_title}",
        ANSI_MAGENTA,
    )
    log(
        f"Device: {device.serial} | "
        f"{device.model or 'unknown model'}",
        ANSI_GREEN,
    )
    log(
        f"Screen: {width}x{height} | "
        f"Session: {session_dir}",
        ANSI_CYAN,
    )
    log(
        "El cerebro inspecciona la UI antes de tocar. "
        "Ctrl+C para detener.",
        ANSI_YELLOW,
    )

    def record_capture(
        snapshot: UiSnapshot | None,
        force: bool = False,
    ) -> tuple[
        str | None,
        str | None,
    ]:
        nonlocal screenshot_no, ui_no

        screenshot_path = None
        ui_path = None

        should_capture = (
            config.screenshot_every > 0
            and (
                force
                or action_no
                % config.screenshot_every
                == 0
            )
            and screenshot_no
            < config.max_screenshots
        )

        if should_capture:
            screenshot_no += 1

            try:
                screenshot = (
                    capture_screenshot(
                        device,
                        session_dir,
                        screenshot_no,
                    )
                )

                screenshot_path = str(
                    screenshot
                )

                if config.preview:
                    maybe_preview(
                        screenshot
                    )

            except AdbError as exc:
                log(
                    f"Captura fallida: {exc}",
                    ANSI_YELLOW,
                )

        if (
            snapshot is not None
            and config.dump_ui
            and ui_no < config.max_screenshots
        ):
            ui_no += 1

            try:
                ui_file = save_ui_xml(
                    session_dir,
                    ui_no,
                    snapshot.xml,
                )

                ui_path = str(
                    ui_file
                )

            except OSError as exc:
                log(
                    f"No se pudo guardar UI XML: {exc}",
                    ANSI_YELLOW,
                )

        return (
            screenshot_path,
            ui_path,
        )

    try:
        initial_snapshot = None

        if config.mode == "smart":
            try:
                initial_snapshot = (
                    make_ui_snapshot(
                        device
                    )
                )
                previous_screen_fp = (
                    initial_snapshot.fingerprint
                )

            except AdbError as exc:
                log(
                    "No se pudo inspeccionar "
                    f"la UI inicial: {exc}",
                    ANSI_YELLOW,
                )

        screenshot_path, ui_path = record_capture(
            initial_snapshot,
            force=True,
        )

        append_event(
            session_dir,
            {
                "action": 0,
                "timestamp": datetime.now().isoformat(
                    timespec="seconds"
                ),
                "type": "initial_state",
                "status": "ok",
                "package": (
                    initial_snapshot.package
                    if initial_snapshot
                    else ""
                ),
                "activity": (
                    initial_snapshot.activity
                    if initial_snapshot
                    else ""
                ),
                "screen_fingerprint": (
                    initial_snapshot.fingerprint
                    if initial_snapshot
                    else ""
                ),
                "screenshot": screenshot_path,
                "ui_xml": ui_path,
                "clickable_count": (
                    len(
                        initial_snapshot.clickable
                    )
                    if initial_snapshot
                    else 0
                ),
                "scrollable_count": (
                    len(
                        initial_snapshot.scrollables
                    )
                    if initial_snapshot
                    else 0
                ),
            },
        )

        if initial_snapshot:
            log(
                f"🧠 UI: "
                f"{len(initial_snapshot.nodes)} nodes | "
                f"{len(initial_snapshot.clickable)} clickable | "
                f"{len(initial_snapshot.scrollables)} scrollable | "
                f"pkg={initial_snapshot.package or '?'}",
                ANSI_CYAN,
            )

        while not stop_requested:
            if (
                config.duration is not None
                and (
                    time.monotonic()
                    - started
                )
                >= config.duration
            ):
                break

            action_no += 1

            action_started = (
                datetime.now().isoformat(
                    timespec="seconds"
                )
            )

            snapshot = None
            decision_note = ""
            target_key = None
            status = "ok"
            error = None
            description = ""

            try:
                if config.mode == "smart":
                    snapshot = make_ui_snapshot(
                        device
                    )

                    if (
                        snapshot.fingerprint
                        != brain.last_fingerprint
                    ):
                        brain.no_change_streak = 0
                    else:
                        brain.no_change_streak += 1

                    brain.last_fingerprint = (
                        snapshot.fingerprint
                    )

                    (
                        description,
                        target_key,
                    ) = perform_smart_action(
                        device,
                        snapshot,
                        brain,
                        width,
                        height,
                        config.smart_back_after,
                    )

                    decision_note = (
                        f"nodes={len(snapshot.nodes)} "
                        f"clickable={len(snapshot.clickable)} "
                        f"scrollable={len(snapshot.scrollables)} "
                        f"screen={snapshot.fingerprint}"
                    )

                else:
                    description = (
                        run_random_mode_action(
                            device,
                            width,
                            height,
                        )
                    )

            except AdbError as exc:
                status = "error"
                error = str(exc)

            time.sleep(
                min(
                    0.9,
                    max(
                        0.20,
                        config.min_delay * 0.5,
                    ),
                )
            )

            after_snapshot = None

            if (
                config.mode == "smart"
                and status == "ok"
            ):
                try:
                    after_snapshot = (
                        make_ui_snapshot(
                            device
                        )
                    )

                    if (
                        after_snapshot.fingerprint
                        == previous_screen_fp
                    ):
                        brain.no_change_streak += 1
                    else:
                        brain.no_change_streak = 0
                        brain.screen_visits.setdefault(
                            after_snapshot.fingerprint,
                            1,
                        )

                    previous_screen_fp = (
                        after_snapshot.fingerprint
                    )

                except AdbError as exc:
                    error = (
                        "Post-action UI: "
                        f"{exc}"
                    )
                    status = "ui_error"

            (
                screenshot_path,
                ui_path,
            ) = record_capture(
                after_snapshot or snapshot,
                force=False,
            )

            event = {
                "action": action_no,
                "timestamp": action_started,
                "type": (
                    "smart_ui_action"
                    if config.mode == "smart"
                    else "random_action"
                ),
                "description": description,
                "status": status,
                "error": error,
                "package": (
                    after_snapshot.package
                    if after_snapshot
                    else (
                        snapshot.package
                        if snapshot
                        else ""
                    )
                ),
                "activity": (
                    after_snapshot.activity
                    if after_snapshot
                    else (
                        snapshot.activity
                        if snapshot
                        else ""
                    )
                ),
                "screen_fingerprint_before": (
                    snapshot.fingerprint
                    if snapshot
                    else ""
                ),
                "screen_fingerprint_after": (
                    after_snapshot.fingerprint
                    if after_snapshot
                    else ""
                ),
                "decision": decision_note,
                "target_key": target_key,
                "screenshot": screenshot_path,
                "ui_xml": ui_path,
                "brain": {
                    "no_change_streak": (
                        brain.no_change_streak
                    ),
                    "known_screens": len(
                        brain.screen_visits
                    ),
                    "remembered_actions": len(
                        brain.seen_actions
                    ),
                },
            }

            append_event(
                session_dir,
                event,
            )

            if status == "ok":
                battery = get_battery(
                    device
                )

                package = (
                    event["package"]
                    or "unknown.package"
                )

                changed = (
                    event[
                        "screen_fingerprint_before"
                    ]
                    != event[
                        "screen_fingerprint_after"
                    ]
                    if (
                        config.mode == "smart"
                        and event[
                            "screen_fingerprint_after"
                        ]
                    )
                    else True
                )

                change_label = (
                    "CHANGED"
                    if changed
                    else "NO CHANGE"
                )

                shot = (
                    f" | 📸 {screenshot_path}"
                    if screenshot_path
                    else ""
                )

                log(
                    f"#{action_no:04d} "
                    f"{description} | "
                    f"{package} | "
                    f"{change_label} | "
                    f"🔋 {battery}{shot}",
                    ANSI_GREEN,
                )

            else:
                log(
                    f"#{action_no:04d} "
                    f"{description or 'action'}: "
                    f"{error}",
                    ANSI_RED,
                )

            if (
                screenshot_no
                >= config.max_screenshots
                and not announced_screenshot_cap
                and config.max_screenshots > 0
            ):
                announced_screenshot_cap = True

                log(
                    "Límite de capturas alcanzado; "
                    "se continúa sin crear más PNG.",
                    ANSI_YELLOW,
                )

            if (
                ui_no
                >= config.max_screenshots
                and not announced_ui_cap
                and config.max_screenshots > 0
            ):
                announced_ui_cap = True

                log(
                    "Límite de UI XML alcanzado; "
                    "se continúa sin guardar más XML.",
                    ANSI_YELLOW,
                )

            delay = random.uniform(
                config.min_delay,
                config.max_delay,
            )
            time.sleep(delay)

            if (
                brain.no_change_streak
                >= config.max_consecutive_no_change
            ):
                brain.no_change_streak = max(
                    0,
                    config.smart_back_after - 1,
                )

    finally:
        signal.signal(
            signal.SIGINT,
            old_sigint,
        )
        signal.signal(
            signal.SIGTERM,
            old_sigterm,
        )

    elapsed = (
        time.monotonic()
        - started
    )

    summary = {
        "finished_at": datetime.now().isoformat(
            timespec="seconds"
        ),
        "version": VERSION,
        "mode": config.mode,
        "actions": action_no,
        "screenshots": screenshot_no,
        "ui_xml_files": ui_no,
        "known_screens": len(
            brain.screen_visits
        ),
        "remembered_actions": len(
            brain.seen_actions
        ),
        "elapsed_seconds": round(
            elapsed,
            2,
        ),
        "session_dir": str(
            session_dir
        ),
    }

    (session_dir / "summary.json").write_text(
        json.dumps(
            summary,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    log(
        f"Sesión terminada: "
        f"{action_no} acciones, "
        f"{screenshot_no} capturas, "
        f"{ui_no} UI dumps, "
        f"{len(brain.screen_visits)} "
        f"pantallas conocidas, "
        f"{elapsed:.1f}s → "
        f"{session_dir}",
        ANSI_CYAN,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Control inteligente/aleatorio de un Android mediante ADB, "
            "con memoria de UI y capturas."
        )
    )

    parser.add_argument(
        "--serial",
        help="Serial ADB concreto a controlar.",
    )
    parser.add_argument(
        "--mode",
        choices=("smart", "random"),
        default="smart",
        help="Motor de decisión. Por defecto: smart.",
    )
    parser.add_argument(
        "--min-delay",
        type=float,
        default=0.45,
        help="Espera mínima entre acciones (segundos).",
    )
    parser.add_argument(
        "--max-delay",
        type=float,
        default=1.70,
        help="Espera máxima entre acciones (segundos).",
    )
    parser.add_argument(
        "--screenshot-every",
        type=int,
        default=1,
        help="Capturar cada N acciones. Usa 0 para desactivar capturas.",
    )
    parser.add_argument(
        "--max-screenshots",
        type=int,
        default=300,
        help="Máximo de PNG y XML de UI por sesión.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Duración máxima en segundos.",
    )
    parser.add_argument(
        "--preview",
        action="store_true",
        help="Muestra las capturas con chafa si está instalado.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Semilla aleatoria para repetir pruebas.",
    )
    parser.add_argument(
        "--dump-ui",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Guardar jerarquías UI XML. Por defecto: sí.",
    )
    parser.add_argument(
        "--smart-back-after",
        type=int,
        default=3,
        help="Tras N observaciones sin cambio, usa Back para escapar de bucles.",
    )
    parser.add_argument(
        "--max-consecutive-no-change",
        type=int,
        default=6,
        help="Límite para que el motor fuerce backtracking.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="No esperar a un móvil autorizado; salir si no está conectado.",
    )

    return parser


def validate_config(
    config: Config,
) -> None:
    if (
        config.min_delay < 0
        or config.max_delay < 0
    ):
        raise ValueError(
            "Los delays no pueden ser negativos."
        )

    if (
        config.max_delay
        < config.min_delay
    ):
        raise ValueError(
            "--max-delay debe ser >= --min-delay."
        )

    if config.screenshot_every < 0:
        raise ValueError(
            "--screenshot-every debe ser >= 0."
        )

    if config.max_screenshots < 0:
        raise ValueError(
            "--max-screenshots debe ser >= 0."
        )

    if (
        config.duration is not None
        and config.duration <= 0
    ):
        raise ValueError(
            "--duration debe ser mayor que 0."
        )

    if config.smart_back_after < 1:
        raise ValueError(
            "--smart-back-after debe ser >= 1."
        )

    if (
        config.max_consecutive_no_change
        < 1
    ):
        raise ValueError(
            "--max-consecutive-no-change debe ser >= 1."
        )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    config = Config(
        serial=args.serial,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        screenshot_every=args.screenshot_every,
        max_screenshots=args.max_screenshots,
        duration=args.duration,
        wait_for_device=not args.no_wait,
        preview=args.preview,
        seed=args.seed,
        mode=args.mode,
        dump_ui=args.dump_ui,
        smart_back_after=args.smart_back_after,
        max_consecutive_no_change=(
            args.max_consecutive_no_change
        ),
    )

    try:
        validate_config(config)
        ensure_adb()

        if config.seed is not None:
            random.seed(
                config.seed
            )

        log(
            f"{APP_NAME} {VERSION} iniciando...",
            ANSI_CYAN,
        )

        log(
            "Buscando dispositivo ADB...",
            ANSI_CYAN,
        )

        device = choose_device(
            config.serial
        )

        if (
            device is None
            and config.wait_for_device
        ):
            log(
                "Esperando un móvil autorizado por USB...",
                ANSI_YELLOW,
            )
            device = wait_for_device(
                config.serial
            )

        if device is None:
            log(
                "No hay un dispositivo autorizado. "
                "Activa 'Depuración USB' y acepta la huella RSA.",
                ANSI_RED,
            )
            return 2

        if not device.authorized:
            log(
                f"El dispositivo está en estado "
                f"'{device.state}', no 'device'.",
                ANSI_RED,
            )
            return 2

        run_chaos(
            device,
            config,
        )
        return 0

    except (
        AdbError,
        ValueError,
    ) as exc:
        log(
            str(exc),
            ANSI_RED,
        )
        return 1

    except KeyboardInterrupt:
        print()
        log(
            "Interrumpido.",
            ANSI_YELLOW,
        )
        return 130


if __name__ == "__main__":
    raise SystemExit(
        main()
    )
