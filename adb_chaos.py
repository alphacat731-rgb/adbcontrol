
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
VERSION = "0.3.0"

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
    persistent_memory: bool
    capture_on_change: bool
    memory_file: str


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
    transition_graph: dict[str, dict[str, str]] = field(
        default_factory=dict
    )
    target_results: dict[str, dict[str, int]] = field(
        default_factory=dict
    )

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

    def target_stats(
        self,
        screen_fp: str,
        action_id: str,
    ) -> dict[str, int]:
        key = f"{screen_fp}|{action_id}"
        return self.target_results.setdefault(
            key,
            {
                "attempts": 0,
                "changed": 0,
                "novel": 0,
                "no_change": 0,
            },
        )

    def record_transition(
        self,
        before_fp: str,
        action_id: str,
        after_fp: str,
        changed: bool,
        novel: bool,
    ) -> None:
        stats = self.target_stats(
            before_fp,
            action_id,
        )
        stats["attempts"] += 1

        if changed:
            stats["changed"] += 1
        else:
            stats["no_change"] += 1

        if novel:
            stats["novel"] += 1

        self.transition_graph.setdefault(
            before_fp,
            {},
        )[action_id] = after_fp

    def serialize(self, limit: int = 2000) -> dict:
        target_items = sorted(
            self.target_results.items(),
            key=lambda item: (
                item[1].get("novel", 0),
                item[1].get("changed", 0),
                item[1].get("attempts", 0),
            ),
            reverse=True,
        )[:limit]

        state_items = list(
            self.screen_visits.items()
        )[:limit]

        graph_items = list(
            self.transition_graph.items()
        )[:limit]

        return {
            "version": VERSION,
            "screen_visits": dict(
                state_items
            ),
            "seen_actions": dict(
                list(self.seen_actions.items())[:limit]
            ),
            "target_results": {
                key: dict(value)
                for key, value in target_items
            },
            "transition_graph": {
                key: dict(value)
                for key, value in graph_items
            },
        }

    @classmethod
    def load(
        cls,
        path: Path,
    ) -> "Brain":
        brain = cls()

        if not path.exists():
            return brain

        try:
            raw = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )
        except (
            OSError,
            json.JSONDecodeError,
        ):
            return brain

        if not isinstance(raw, dict):
            return brain

        visits = raw.get(
            "screen_visits",
            {}
        )
        if isinstance(visits, dict):
            brain.screen_visits = {
                str(key): int(value)
                for key, value in visits.items()
            }

        seen = raw.get(
            "seen_actions",
            {}
        )
        if isinstance(seen, dict):
            brain.seen_actions = {
                str(key): int(value)
                for key, value in seen.items()
            }

        targets = raw.get(
            "target_results",
            {}
        )
        if isinstance(targets, dict):
            brain.target_results = {
                str(key): {
                    "attempts": int(
                        value.get("attempts", 0)
                    ),
                    "changed": int(
                        value.get("changed", 0)
                    ),
                    "novel": int(
                        value.get("novel", 0)
                    ),
                    "no_change": int(
                        value.get("no_change", 0)
                    ),
                }
                for key, value in targets.items()
                if isinstance(value, dict)
            }

        graph = raw.get(
            "transition_graph",
            {}
        )
        if isinstance(graph, dict):
            brain.transition_graph = {
                str(key): {
                    str(action): str(state)
                    for action, state in value.items()
                }
                for key, value in graph.items()
                if isinstance(value, dict)
            }

        return brain


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


def normalize_fingerprint_text(
    value: str,
) -> str:
    value = value.strip().lower()
    value = re.sub(
        r"\b\d{1,2}:\d{2}(?::\d{2})?\b",
        "<time>",
        value,
    )
    value = re.sub(
        r"\b\d{2,}\b",
        "<n>",
        value,
    )
    value = re.sub(
        r"\s+",
        " ",
        value,
    )
    return value[:120]


def normalize_resource(
    value: str,
) -> str:
    value = value.strip().lower()
    if not value:
        return ""
    return value.rsplit(
        "/",
        1,
    )[-1][:120]


def fingerprint_node(
    node: UiNode,
) -> tuple:
    return (
        node.class_name,
        normalize_fingerprint_text(
            node.text
        ),
        normalize_fingerprint_text(
            node.content_desc
        ),
        normalize_resource(
            node.resource_id
        ),
        node.clickable,
        node.scrollable,
        node.enabled,
        (
            node.bounds.left // 32,
            node.bounds.top // 32,
            node.bounds.right // 32,
            node.bounds.bottom // 32,
        ),
    )


def make_ui_snapshot(
    device: Device,
) -> UiSnapshot:
    xml = dump_ui(device)
    nodes = parse_ui_xml(xml)
    package, activity = get_current_window(
        device
    )

    visible = [
        fingerprint_node(node)
        for node in nodes
        if node.visible
    ]
    visible.sort()

    payload = json.dumps(
        {
            "package": package,
            "activity": activity,
            "nodes": visible,
        },
        sort_keys=True,
        ensure_ascii=False,
    ).encode("utf-8")

    fingerprint = hashlib.sha256(
        payload
    ).hexdigest()[:16]

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
            "persistent_memory": (
                config.persistent_memory
            ),
            "capture_on_change": (
                config.capture_on_change
            ),
            "memory_file": config.memory_file,
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


def target_value(
    stats: dict[str, int],
) -> float:
    attempts = stats.get(
        "attempts",
        0,
    )

    if attempts == 0:
        return 10.0

    changed = stats.get(
        "changed",
        0,
    )
    novel = stats.get(
        "novel",
        0,
    )
    no_change = stats.get(
        "no_change",
        0,
    )

    change_rate = changed / attempts
    novel_rate = novel / attempts
    failure_rate = no_change / attempts

    return (
        novel_rate * 13.0
        + change_rate * 6.0
        - failure_rate * 4.0
        - min(
            10.0,
            attempts * 1.8,
        )
    )


def node_semantic_score(
    node: UiNode,
    width: int,
    height: int,
) -> float:
    score = 1.0

    label = " ".join(
        (
            node.text,
            node.content_desc,
            node.resource_id,
        )
    ).lower()

    for word, bonus in INTERESTING_LABELS.items():
        if re.search(
            rf"\b{re.escape(word)}\b",
            label,
        ):
            score += bonus

    class_name = node.class_name.lower()

    for name, bonus in (
        ("button", 3.5),
        ("imagebutton", 3.5),
        ("checkbox", 2.2),
        ("switch", 2.2),
        ("radio", 2.0),
        ("tab", 2.0),
        ("menuitem", 2.0),
        ("listitem", 1.5),
        ("spinner", 1.5),
    ):
        if name in class_name:
            score += bonus
            break

    if node.text.strip():
        score += 1.5

    if node.content_desc.strip():
        score += 1.0

    area = node.bounds.width * node.bounds.height
    screen = max(
        1,
        width * height,
    )
    ratio = area / screen

    if 0.001 <= ratio <= 0.25:
        score += 1.5
    elif ratio > 0.60:
        score -= 3.0

    center_x, center_y = node.bounds.center

    distance = (
        abs(center_x - width / 2)
        / max(1, width / 2)
        + abs(center_y - height / 2)
        / max(1, height / 2)
    )

    score += max(
        0.0,
        1.7 - distance,
    )

    return max(
        0.1,
        score,
    )


def choose_smart_node(
    snapshot: UiSnapshot,
    brain: Brain,
    width: int,
    height: int,
) -> tuple[UiNode, float] | None:
    candidates = snapshot.clickable

    if not candidates:
        return None

    scored = []

    visits = brain.screen_visits.get(
        snapshot.fingerprint,
        0,
    )

    for node in candidates:
        stats = brain.target_stats(
            snapshot.fingerprint,
            node.key,
        )

        score = node_semantic_score(
            node,
            width,
            height,
        )

        score += target_value(
            stats
        )

        if stats.get("attempts", 0) == 0:
            score += 8.0

        if visits >= 4:
            score += (
                5.0
                if stats.get(
                    "attempts",
                    0,
                ) == 0
                else 0.0
            )

        scored.append(
            (
                max(
                    0.1,
                    score,
                ),
                node,
            )
        )

    scored.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    frontier = scored[
        : min(
            10,
            len(scored),
        )
    ]

    temperature = (
        1.85
        if visits >= 6
        else 1.35
    )

    weights = [
        max(
            0.05,
            score,
        ) ** temperature
        for score, _ in frontier
    ]

    choice = random.choices(
        frontier,
        weights=weights,
        k=1,
    )[0]

    return choice


def is_dialog(
    snapshot: UiSnapshot,
) -> bool:
    labels = " ".join(
        node.label.lower()
        for node in snapshot.nodes
        if node.visible
    )

    if any(
        phrase in labels
        for phrase in (
            "are you sure",
            "warning",
            "notice",
            "attention",
            "dialog",
        )
    ):
        return True

    return (
        any(
            "dialog" in node.class_name.lower()
            for node in snapshot.nodes
        )
        and len(
            snapshot.clickable
        ) <= 7
    )


def choose_dialog_target(
    snapshot: UiSnapshot,
    brain: Brain,
) -> UiNode | None:
    priority = (
        "close",
        "ok",
        "done",
        "continue",
        "next",
        "skip",
    )

    ranked = []

    for node in snapshot.clickable:
        label = node.label.lower()
        bonus = 0.0

        for index, word in enumerate(
            priority
        ):
            if re.search(
                rf"\b{re.escape(word)}\b",
                label,
            ):
                bonus += (
                    14.0
                    - index * 1.5
                )

        bonus += target_value(
            brain.target_stats(
                snapshot.fingerprint,
                node.key,
            )
        )

        ranked.append(
            (
                bonus,
                node,
            )
        )

    if not ranked:
        return None

    ranked.sort(
        key=lambda item: item[0],
        reverse=True,
    )

    return ranked[0][1]


def scroll_direction(
    snapshot: UiSnapshot,
    brain: Brain,
) -> str:
    up = brain.target_stats(
        snapshot.fingerprint,
        "SCROLL::up",
    )
    down = brain.target_stats(
        snapshot.fingerprint,
        "SCROLL::down",
    )

    up_attempts = up.get(
        "attempts",
        0,
    )
    down_attempts = down.get(
        "attempts",
        0,
    )

    if up_attempts == 0 and down_attempts > 0:
        return "up"

    if down_attempts == 0 and up_attempts > 0:
        return "down"

    up_value = target_value(
        up
    )
    down_value = target_value(
        down
    )

    if abs(
        up_value - down_value
    ) < 1.5:
        return random.choice(
            ("up", "down")
        )

    return (
        "up"
        if up_value > down_value
        else "down"
    )


def smart_swipe(
    device: Device,
    target: UiNode | None,
    width: int,
    height: int,
    direction: str,
) -> str:
    bounds = (
        target.bounds
        if target is not None
        else Bounds(
            int(width * 0.10),
            int(height * 0.16),
            int(width * 0.90),
            int(height * 0.84),
        )
    ).clamp(
        width,
        height,
    )

    if (
        bounds.width < 50
        or bounds.height < 50
    ):
        bounds = Bounds(
            int(width * 0.10),
            int(height * 0.16),
            int(width * 0.90),
            int(height * 0.84),
        )

    horizontal = (
        bounds.width
        > bounds.height * 1.55
    )

    if horizontal:
        y = (
            bounds.top
            + bounds.bottom
        ) // 2
        left = (
            bounds.left
            + max(
                18,
                bounds.width // 8,
            )
        )
        right = (
            bounds.right
            - max(
                18,
                bounds.width // 8,
            )
        )

        if direction == "left":
            x1, x2 = right, left
        else:
            x1, x2 = left, right

        y1 = y2 = y
    else:
        x = (
            bounds.left
            + bounds.right
        ) // 2
        top = (
            bounds.top
            + max(
                18,
                bounds.height // 8,
            )
        )
        bottom = (
            bounds.bottom
            - max(
                18,
                bounds.height // 8,
            )
        )

        if direction == "up":
            y1, y2 = bottom, top
        else:
            y1, y2 = top, bottom

        x1 = x2 = x

    duration_ms = random.randint(
        450,
        900,
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

    return (
        f"swipe {direction} "
        f"{bounds.to_string()} "
        f"{duration_ms}ms"
    )


def perform_smart_action(
    device: Device,
    snapshot: UiSnapshot,
    brain: Brain,
    width: int,
    height: int,
    config: Config,
) -> tuple[str, str]:
    brain.total_decisions += 1

    state_fp = snapshot.fingerprint
    brain.screen_visits[
        state_fp
    ] = brain.screen_visits.get(
        state_fp,
        0,
    ) + 1

    # Backtracking is deliberately deterministic once a state has become
    # clearly stuck. This prevents endless loops on static screens.
    if (
        brain.no_change_streak
        >= config.smart_back_after
    ):
        run_adb(
            device.serial,
            "shell",
            "input",
            "keyevent",
            "4",
        )
        return (
            "Back (stuck state)",
            "BACK",
        )

    if (
        is_dialog(snapshot)
        and random.random() < 0.92
    ):
        node = choose_dialog_target(
            snapshot,
            brain,
        )

        if node is not None:
            return (
                smart_tap(
                    device,
                    node,
                    width,
                    height,
                ),
                node.key,
            )

    candidates = snapshot.clickable
    scrollables = snapshot.scrollables

    # Most of the time the bot explores a clickable frontier. Occasionally it
    # scrolls even when buttons exist to expose content hidden below the fold.
    if candidates:
        if (
            scrollables
            and random.random() < 0.20
            and brain.no_change_streak < 2
        ):
            node = random.choice(
                scrollables[
                    : min(
                        3,
                        len(scrollables),
                    )
                ]
            )
            direction = scroll_direction(
                snapshot,
                brain,
            )
            return (
                smart_swipe(
                    device,
                    node,
                    width,
                    height,
                    direction,
                ),
                f"SCROLL::{direction}",
            )

        choice = choose_smart_node(
            snapshot,
            brain,
            width,
            height,
        )

        if choice is not None:
            _, node = choice
            return (
                smart_tap(
                    device,
                    node,
                    width,
                    height,
                ),
                node.key,
            )

    if scrollables:
        node = scrollables[0]
        direction = scroll_direction(
            snapshot,
            brain,
        )

        return (
            smart_swipe(
                device,
                node,
                width,
                height,
                direction,
            ),
            f"SCROLL::{direction}",
        )

    run_adb(
        device.serial,
        "shell",
        "input",
        "keyevent",
        "4",
    )

    return (
        "Back (dead end)",
        "BACK",
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
        (width, height),
        config,
    )

    memory_path = Path(
        config.memory_file
    )

    brain = (
        Brain.load(memory_path)
        if config.persistent_memory
        else Brain()
    )

    stopped = False
    action_no = 0
    screenshot_no = 0
    ui_no = 0
    started = time.monotonic()
    previous_fp = ""
    old_sigint = signal.getsignal(
        signal.SIGINT
    )
    old_sigterm = signal.getsignal(
        signal.SIGTERM
    )

    def stop_handler(
        signum: int,
        frame: object,
    ) -> None:
        nonlocal stopped
        stopped = True
        print()
        log(
            "Parada solicitada. Cerrando...",
            ANSI_YELLOW,
        )

    signal.signal(
        signal.SIGINT,
        stop_handler,
    )
    signal.signal(
        signal.SIGTERM,
        stop_handler,
    )

    log(
        f"{ANSI_BOLD}{APP_NAME} "
        f"{VERSION}{ANSI_RESET}",
        ANSI_CYAN,
    )
    log(
        "Motor: "
        + (
            "ADAPTIVE INTELLIGENCE"
            if config.mode == "smart"
            else "RANDOM"
        ),
        ANSI_MAGENTA,
    )
    log(
        f"Device: {device.serial} | "
        f"{device.model or 'unknown'}",
        ANSI_GREEN,
    )
    log(
        f"Screen: {width}x{height}",
        ANSI_CYAN,
    )

    if config.persistent_memory:
        log(
            f"Learning DB: {memory_path}",
            ANSI_BLUE,
        )

    log(
        "Ctrl+C para detener.",
        ANSI_YELLOW,
    )

    try:
        while not stopped:
            if (
                config.duration is not None
                and (
                    time.monotonic()
                    - started
                ) >= config.duration
            ):
                break

            action_no += 1
            before = None
            after = None
            action_text = ""
            action_id = ""
            status = "ok"
            error = None
            decision = ""

            try:
                if config.mode == "smart":
                    before = make_ui_snapshot(
                        device
                    )

                    if (
                        previous_fp
                        and before.fingerprint
                        == previous_fp
                    ):
                        brain.no_change_streak += 1
                    else:
                        brain.no_change_streak = 0

                    brain.last_fingerprint = (
                        before.fingerprint
                    )

                    (
                        action_text,
                        action_id,
                    ) = perform_smart_action(
                        device,
                        before,
                        brain,
                        width,
                        height,
                        config,
                    )

                    decision = (
                        f"state={before.fingerprint} "
                        f"nodes={len(before.nodes)} "
                        f"clickable={len(before.clickable)} "
                        f"scrollable={len(before.scrollables)}"
                    )

                else:
                    (
                        action_text,
                        action_id,
                    ) = run_random_mode_action(
                        device,
                        width,
                        height,
                    )

            except AdbError as exc:
                status = "error"
                error = str(exc)

            time.sleep(
                min(
                    1.0,
                    max(
                        0.25,
                        config.min_delay * 0.55,
                    ),
                )
            )

            changed = False
            novel = False

            if (
                config.mode == "smart"
                and status == "ok"
            ):
                try:
                    after = make_ui_snapshot(
                        device
                    )

                    changed = (
                        before is not None
                        and before.fingerprint
                        != after.fingerprint
                    )

                    novel = (
                        after.fingerprint
                        not in brain.screen_visits
                    )

                    if before is not None:
                        brain.record_transition(
                            before.fingerprint,
                            action_id,
                            after.fingerprint,
                            changed,
                            novel,
                        )

                    if novel:
                        brain.screen_visits.setdefault(
                            after.fingerprint,
                            0,
                        )

                    if changed:
                        brain.no_change_streak = 0
                    else:
                        brain.no_change_streak += 1

                    previous_fp = (
                        after.fingerprint
                    )

                except AdbError as exc:
                    status = "ui_error"
                    error = (
                        "Post-action UI: "
                        f"{exc}"
                    )

            should_capture = (
                config.screenshot_every > 0
                and (
                    action_no
                    % config.screenshot_every
                    == 0
                )
            )

            if (
                config.capture_on_change
                and changed
            ):
                should_capture = True

            screenshot_path = None

            if (
                should_capture
                and screenshot_no
                < config.max_screenshots
            ):
                try:
                    screenshot_no += 1
                    screenshot_file = (
                        capture_screenshot(
                            device,
                            session_dir,
                            screenshot_no,
                        )
                    )
                    screenshot_path = str(
                        screenshot_file
                    )

                    if config.preview:
                        maybe_preview(
                            screenshot_file
                        )

                except AdbError as exc:
                    error = (
                        f"Screenshot: {exc}"
                    )

            xml_path = None
            chosen_snapshot = (
                after
                or before
            )

            if (
                chosen_snapshot is not None
                and config.dump_ui
                and ui_no
                < config.max_screenshots
            ):
                try:
                    ui_no += 1
                    xml_file = save_ui_xml(
                        session_dir,
                        ui_no,
                        chosen_snapshot.xml,
                    )
                    xml_path = str(
                        xml_file
                    )
                except OSError as exc:
                    error = (
                        f"UI XML: {exc}"
                    )

            package = ""
            activity = ""

            if after is not None:
                package = after.package
                activity = after.activity
            elif before is not None:
                package = before.package
                activity = before.activity

            stats = {}
            if (
                config.mode == "smart"
                and before is not None
                and action_id
                and action_id != "BACK"
            ):
                stats = brain.target_stats(
                    before.fingerprint,
                    action_id,
                )

            append_event(
                session_dir,
                {
                    "action": action_no,
                    "timestamp": datetime.now().isoformat(
                        timespec="seconds"
                    ),
                    "type": (
                        "smart_ui_action"
                        if config.mode == "smart"
                        else "random_action"
                    ),
                    "description": action_text,
                    "target": action_id,
                    "decision": decision,
                    "status": status,
                    "error": error,
                    "package": package,
                    "activity": activity,
                    "state_before": (
                        before.fingerprint
                        if before
                        else ""
                    ),
                    "state_after": (
                        after.fingerprint
                        if after
                        else ""
                    ),
                    "state_changed": changed,
                    "state_was_novel": novel,
                    "no_change_streak": (
                        brain.no_change_streak
                    ),
                    "known_states": len(
                        brain.screen_visits
                    ),
                    "known_targets": len(
                        brain.target_results
                    ),
                    "target_stats": stats,
                    "battery": get_battery(
                        device
                    ),
                    "screenshot": screenshot_path,
                    "ui_xml": xml_path,
                },
            )

            if status == "ok":
                label = (
                    "NEW STATE"
                    if novel
                    else (
                        "CHANGED"
                        if changed
                        else "NO CHANGE"
                    )
                )

                log(
                    f"#{action_no:04d} "
                    f"{action_text} | "
                    f"{label} | "
                    f"states={len(brain.screen_visits)} "
                    f"targets={len(brain.target_results)} "
                    f"| battery={get_battery(device)}"
                    + (
                        f" | PNG={screenshot_path}"
                        if screenshot_path
                        else ""
                    ),
                    ANSI_GREEN,
                )

            else:
                log(
                    f"#{action_no:04d} "
                    f"{action_text or 'action'}: "
                    f"{error}",
                    ANSI_RED,
                )

            if config.persistent_memory:
                try:
                    save_memory(
                        memory_path,
                        brain,
                    )
                except OSError as exc:
                    log(
                        f"No se pudo guardar memoria: {exc}",
                        ANSI_YELLOW,
                    )

            time.sleep(
                random.uniform(
                    config.min_delay,
                    config.max_delay,
                )
            )

    finally:
        signal.signal(
            signal.SIGINT,
            old_sigint,
        )
        signal.signal(
            signal.SIGTERM,
            old_term,
        )

        if config.persistent_memory:
            try:
                save_memory(
                    memory_path,
                    brain,
                )
            except OSError:
                pass

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
            "known_states": len(
                brain.screen_visits
            ),
            "known_targets": len(
                brain.target_results
            ),
            "graph_edges": sum(
                len(edges)
                for edges in brain.transition_graph.values()
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
            f"{action_no} acciones | "
            f"{screenshot_no} capturas | "
            f"{len(brain.screen_visits)} estados | "
            f"{sum(len(edges) for edges in brain.transition_graph.values())} edges | "
            f"{elapsed:.1f}s",
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
        "--capture-on-change",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Capturar siempre cuando la UI cambie.",
    )
    parser.add_argument(
        "--persistent-memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Aprender entre sesiones en chaos_memory.json.",
    )
    parser.add_argument(
        "--memory-file",
        default="chaos_memory.json",
        help="Archivo JSON para la memoria persistente.",
    )

    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="No esperar a un móvil autorizado; salir si no está conectado.",
    )

    parser.add_argument(
        "--memory-file",
        default="chaos_memory.json",
        help="Archivo JSON de aprendizaje persistente.",
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

    if not config.memory_file.strip():
        raise ValueError(
            "--memory-file no puede estar vacío."
        )



def save_memory(
    path: Path,
    brain: Brain,
) -> None:
    temp = path.with_suffix(
        path.suffix + ".tmp"
    )

    temp.write_text(
        json.dumps(
            brain.serialize(),
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    temp.replace(path)

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
        persistent_memory=(
            args.persistent_memory
        ),
        capture_on_change=(
            args.capture_on_change
        ),
        memory_file=args.memory_file,
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
