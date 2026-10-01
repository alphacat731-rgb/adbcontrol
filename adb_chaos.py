#!/usr/bin/env python3
"""
ADB Chaos - adaptive Android UI explorer.

Designed for Debian/Raspberry Pi and intended for an Android device that the
operator owns or is authorized to control.

The smart engine:
- observes Android's UI hierarchy with uiautomator;
- scores safe actionable controls;
- remembers state/control outcomes;
- learns a compact transition graph;
- explores new states before repeating known dead ends;
- remembers useful scroll directions;
- backs out of stale loops;
- records screenshots, UI XML and JSONL decision history.

Only fixed ADB operations are used. UI text is never interpolated into a shell
command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import signal
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


APP_NAME = "ADB CHAOS"
VERSION = "0.4.0"

ANSI_RESET = "\033[0m"
ANSI_CYAN = "\033[96m"
ANSI_GREEN = "\033[92m"
ANSI_YELLOW = "\033[93m"
ANSI_RED = "\033[91m"
ANSI_MAGENTA = "\033[95m"
ANSI_BLUE = "\033[94m"
ANSI_DIM = "\033[2m"
ANSI_BOLD = "\033[1m"

# Conservative labels. The explorer simply will not activate a matching
# visible control. This keeps random exploration away from sensitive actions.
BLOCKED_LABELS = (
    "delete",
    "remove",
    "uninstall",
    "erase",
    "wipe",
    "factory reset",
    "reset all",
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
    "otp",
    "verification",
    "sign in",
    "log in",
    "login",
    "account",
    "allow",
    "deny",
    "grant",
    "install",
    "download",
    "subscribe",
    "shutdown",
    "power off",
)

POSITIVE_LABELS = {
    "open": 8.0,
    "next": 8.0,
    "continue": 8.0,
    "start": 7.0,
    "play": 7.0,
    "more": 6.0,
    "menu": 6.0,
    "explore": 6.0,
    "view": 5.0,
    "show": 5.0,
    "details": 5.0,
    "info": 4.0,
    "about": 4.0,
    "refresh": 3.0,
    "search": 3.0,
    "ok": 3.0,
    "done": 3.0,
    "close": 2.0,
    "skip": 1.0,
}

SHORT_CLASS_BONUS = {
    "button": 3.5,
    "imagebutton": 3.5,
    "checkbox": 2.0,
    "switch": 2.0,
    "radiobutton": 2.0,
    "tab": 2.0,
    "menuitem": 2.0,
    "listitem": 1.5,
    "spinner": 1.5,
}

PACKAGE_RE = re.compile(r"[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)+")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
NUMBER_RE = re.compile(r"\b\d{2,}\b")
SPACE_RE = re.compile(r"\s+")


class AdbError(RuntimeError):
    """Raised when an ADB operation cannot be completed."""


@dataclass(frozen=True)
class Device:
    serial: str
    state: str
    model: str = ""
    product: str = ""

    @property
    def authorized(self) -> bool:
        return self.state == "device"


@dataclass(frozen=True)
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
    def area(self) -> int:
        return self.width * self.height

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

    def coarse(self) -> tuple[int, int, int, int]:
        # 32 px buckets avoid treating tiny animation shifts as new geometry.
        return (
            self.left // 32,
            self.top // 32,
            self.right // 32,
            self.bottom // 32,
        )

    def as_string(self) -> str:
        return f"[{self.left},{self.top}][{self.right},{self.bottom}]"


@dataclass(frozen=True)
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
    def short_class(self) -> str:
        return self.class_name.rsplit(".", 1)[-1].lower()

    @property
    def label(self) -> str:
        return (
            self.text.strip()
            or self.content_desc.strip()
            or self.resource_id.rsplit("/", 1)[-1]
            or self.short_class
        )

    @property
    def key(self) -> str:
        # Stable logical identity. Exact coordinates are intentionally omitted.
        return "|".join(
            (
                self.class_name,
                normalize_text(self.text),
                normalize_text(self.content_desc),
                normalize_resource(self.resource_id),
            )
        )

    def is_blocked(self) -> bool:
        label = normalize_text(
            " ".join(
                (
                    self.text,
                    self.content_desc,
                    self.resource_id,
                )
            )
        )
        return any(
            token in label
            for token in BLOCKED_LABELS
        )

    def safe(self) -> bool:
        if not self.enabled or not self.visible:
            return False
        if self.bounds.width < 8 or self.bounds.height < 8:
            return False
        return not self.is_blocked()

    def actionable(self) -> bool:
        if not self.safe():
            return False
        if self.clickable:
            return True
        cls = self.short_class
        return any(
            marker in cls
            for marker in (
                "button",
                "checkbox",
                "switch",
                "radio",
                "tab",
                "menuitem",
            )
        )


@dataclass(frozen=True)
class UiSnapshot:
    xml: str
    nodes: list[UiNode]
    package: str
    activity: str
    fingerprint: str

    @property
    def clickable(self) -> list[UiNode]:
        result: list[UiNode] = []
        seen: set[str] = set()

        for node in self.nodes:
            if not node.actionable():
                continue
            if node.key in seen:
                continue
            seen.add(node.key)
            result.append(node)

        return result

    @property
    def scrollables(self) -> list[UiNode]:
        result = [
            node
            for node in self.nodes
            if node.scrollable
            and node.safe()
            and node.bounds.width >= 40
            and node.bounds.height >= 40
        ]
        result.sort(
            key=lambda node: node.bounds.area,
            reverse=True,
        )
        return result

    @property
    def text_labels(self) -> list[str]:
        labels: list[str] = []
        for node in self.nodes:
            if not node.visible:
                continue
            value = node.label.strip()
            if value:
                labels.append(value)
        return labels


@dataclass
class TargetStats:
    attempts: int = 0
    changed: int = 0
    novel: int = 0
    no_change: int = 0
    last_destination: str = ""

    def score(self) -> float:
        if self.attempts <= 0:
            return 12.0

        change_rate = self.changed / self.attempts
        novel_rate = self.novel / self.attempts
        no_change_rate = self.no_change / self.attempts

        return (
            novel_rate * 15.0
            + change_rate * 6.0
            - no_change_rate * 5.0
            - min(10.0, self.attempts * 1.9)
        )


@dataclass
class StateStats:
    package: str = ""
    activity: str = ""
    visits: int = 0
    dead_end_count: int = 0


@dataclass
class Brain:
    states: dict[str, StateStats] = field(default_factory=dict)
    targets: dict[str, TargetStats] = field(default_factory=dict)
    graph: dict[str, dict[str, str]] = field(default_factory=dict)
    no_change_streak: int = 0
    last_state: str = ""

    def state(self, snapshot: UiSnapshot) -> StateStats:
        current = self.states.setdefault(
            snapshot.fingerprint,
            StateStats(
                package=snapshot.package,
                activity=snapshot.activity,
            ),
        )
        current.visits += 1
        return current

    def target(
        self,
        state_id: str,
        target_id: str,
    ) -> TargetStats:
        return self.targets.setdefault(
            f"{state_id}::{target_id}",
            TargetStats(),
        )

    def record_result(
        self,
        before: str,
        target: str,
        after: str,
        changed: bool,
        novel: bool,
    ) -> None:
        stats = self.target(
            before,
            target,
        )
        stats.attempts += 1

        if changed:
            stats.changed += 1
        else:
            stats.no_change += 1

        if novel:
            stats.novel += 1

        stats.last_destination = after

        self.graph.setdefault(
            before,
            {},
        )[target] = after

    def save(
        self,
        path: Path,
    ) -> None:
        payload = {
            "version": VERSION,
            "states": {
                state_id: {
                    "package": state.package,
                    "activity": state.activity,
                    "visits": state.visits,
                    "dead_end_count": state.dead_end_count,
                }
                for state_id, state in self.states.items()
            },
            "targets": {
                key: {
                    "attempts": value.attempts,
                    "changed": value.changed,
                    "novel": value.novel,
                    "no_change": value.no_change,
                    "last_destination": value.last_destination,
                }
                for key, value in self.targets.items()
            },
            "graph": self.graph,
        }

        temp = path.with_suffix(
            path.suffix + ".tmp"
        )
        temp.write_text(
            json.dumps(
                payload,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        temp.replace(path)

    @classmethod
    def load(
        cls,
        path: Path,
    ) -> "Brain":
        brain = cls()

        if not path.exists():
            return brain

        try:
            payload = json.loads(
                path.read_text(
                    encoding="utf-8"
                )
            )
        except (
            OSError,
            json.JSONDecodeError,
        ):
            return brain

        if not isinstance(payload, dict):
            return brain

        states = payload.get(
            "states",
            {},
        )
        if isinstance(states, dict):
            for key, raw in states.items():
                if not isinstance(raw, dict):
                    continue
                brain.states[str(key)] = StateStats(
                    package=str(
                        raw.get(
                            "package",
                            "",
                        )
                    ),
                    activity=str(
                        raw.get(
                            "activity",
                            "",
                        )
                    ),
                    visits=int(
                        raw.get(
                            "visits",
                            0,
                        )
                    ),
                    dead_end_count=int(
                        raw.get(
                            "dead_end_count",
                            0,
                        )
                    ),
                )

        targets = payload.get(
            "targets",
            {},
        )
        if isinstance(targets, dict):
            for key, raw in targets.items():
                if not isinstance(raw, dict):
                    continue
                brain.targets[str(key)] = TargetStats(
                    attempts=int(
                        raw.get(
                            "attempts",
                            0,
                        )
                    ),
                    changed=int(
                        raw.get(
                            "changed",
                            0,
                        )
                    ),
                    novel=int(
                        raw.get(
                            "novel",
                            0,
                        )
                    ),
                    no_change=int(
                        raw.get(
                            "no_change",
                            0,
                        )
                    ),
                    last_destination=str(
                        raw.get(
                            "last_destination",
                            "",
                        )
                    ),
                )

        graph = payload.get(
            "graph",
            {},
        )
        if isinstance(graph, dict):
            brain.graph = {
                str(source): {
                    str(target): str(destination)
                    for target, destination in mapping.items()
                }
                for source, mapping in graph.items()
                if isinstance(mapping, dict)
            }

        return brain


@dataclass
class Config:
    serial: str | None
    mode: str
    min_delay: float
    max_delay: float
    duration: float | None
    screenshot_every: int
    max_screenshots: int
    capture_on_change: bool
    dump_ui: bool
    preview: bool
    persistent_memory: bool
    memory_file: str
    smart_back_after: int
    max_consecutive_no_change: int
    seed: int | None
    wait_for_device: bool


def normalize_text(value: str) -> str:
    value = value.strip().lower()
    value = TIME_RE.sub(
        "<time>",
        value,
    )
    value = NUMBER_RE.sub(
        "<n>",
        value,
    )
    value = SPACE_RE.sub(
        " ",
        value,
    )
    return value[:120]


def normalize_resource(value: str) -> str:
    value = value.strip().lower()
    if not value:
        return ""
    return value.rsplit(
        "/",
        1,
    )[-1][:120]


def normalize_attribute(value: str) -> str:
    value = normalize_text(value)
    return value[:160]


def log(
    message: str,
    color: str = ANSI_RESET,
) -> None:
    stamp = datetime.now().strftime(
        "%H:%M:%S"
    )
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
    command = [
        "adb",
        "-s",
        serial,
        *args,
    ]

    try:
        process = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AdbError(
            "adb no está instalado. Ejecuta: "
            "sudo apt install adb"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AdbError(
            "ADB agotó el tiempo de espera."
        ) from exc

    if process.returncode != 0:
        stderr = process.stderr.decode(
            "utf-8",
            errors="replace",
        ).strip()
        raise AdbError(
            stderr
            or (
                "ADB terminó con código "
                f"{process.returncode}"
            )
        )

    if binary:
        return process.stdout

    return process.stdout.decode(
        "utf-8",
        errors="replace",
    )


def ensure_adb() -> None:
    if shutil.which("adb") is None:
        raise AdbError(
            "No encuentro adb. Instálalo con "
            "'sudo apt install adb'."
        )


def parse_devices(
    raw: str,
) -> list[Device]:
    devices: list[Device] = []

    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(
            "List of devices attached"
        ):
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        serial = parts[0]
        state = parts[1]
        model = ""
        product = ""

        for token in parts[2:]:
            if token.startswith(
                "model:"
            ):
                model = token.split(
                    ":",
                    1,
                )[1].replace(
                    "_",
                    " ",
                )
            elif token.startswith(
                "product:"
            ):
                product = token.split(
                    ":",
                    1,
                )[1]

        devices.append(
            Device(
                serial=serial,
                state=state,
                model=model,
                product=product,
            )
        )

    return devices


def list_devices() -> list[Device]:
    try:
        process = subprocess.run(
            [
                "adb",
                "devices",
                "-l",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=10,
            check=False,
        )
    except FileNotFoundError as exc:
        raise AdbError(
            "No encuentro adb. Instálalo con "
            "'sudo apt install adb'."
        ) from exc

    if process.returncode != 0:
        raise AdbError(
            process.stderr.decode(
                "utf-8",
                errors="replace",
            ).strip()
        )

    return parse_devices(
        process.stdout.decode(
            "utf-8",
            errors="replace",
        )
    )


def choose_device(
    serial: str | None,
) -> Device | None:
    devices = list_devices()

    if serial:
        return next(
            (
                device
                for device in devices
                if device.serial == serial
            ),
            None,
        )

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
            print(
                f"  - {device.serial} "
                f"({device.model or 'unknown'})"
            )
        return None

    if devices:
        log(
            "Dispositivos detectados: "
            + ", ".join(
                f"{device.serial}:{device.state}"
                for device in devices
            ),
            ANSI_YELLOW,
        )

    return None


def wait_for_device(
    serial: str | None,
) -> Device:
    while True:
        device = choose_device(
            serial
        )
        if (
            device is not None
            and device.authorized
        ):
            return device
        time.sleep(1.0)


def parse_screen_size(
    text: str,
) -> tuple[int, int] | None:
    match = re.search(
        r"(?:Physical|Override) size:\s*(\d+)x(\d+)",
        text,
    )
    if match is None:
        match = re.search(
            r"(\d+)x(\d+)",
            text,
        )
    if match is None:
        return None

    width, height = map(
        int,
        match.groups(),
    )
    return width, height


def get_screen_size(
    device: Device,
) -> tuple[int, int]:
    output = str(
        run_adb(
            device.serial,
            "shell",
            "wm",
            "size",
        )
    )

    size = parse_screen_size(
        output
    )
    if size is None:
        raise AdbError(
            "No pude detectar la resolución del móvil."
        )
    return size


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
    except AdbError:
        return "?"

    level = re.search(
        r"level:\s*(\d+)",
        output,
    )
    if level is None:
        return "?"

    status = re.search(
        r"status:\s*(\d+)",
        output,
    )
    status_name = {
        "2": "charging",
        "3": "discharging",
        "4": "not charging",
        "5": "full",
    }.get(
        status.group(1)
        if status is not None
        else "",
        "",
    )

    return (
        f"{level.group(1)}% {status_name}".strip()
    )


def get_current_window(
    device: Device,
) -> tuple[str, str]:
    patterns = (
        r"mCurrentFocus=Window\{[^}]+\s+u\d+\s+([^}]+)\}",
        r"mFocusedApp=ActivityRecord\{[^}]+\s+([^/\s]+)/([^}\s]+)",
    )

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

        for pattern in patterns:
            match = re.search(
                pattern,
                output,
            )
            if match is None:
                continue

            focus = match.group(1)
            parts = focus.split(
                "/",
                1,
            )

            package = parts[0]
            activity = (
                parts[1]
                if len(parts) == 2
                else ""
            )

            if package:
                return (
                    package,
                    activity,
                )

        packages = PACKAGE_RE.findall(
            output
        )
        if packages:
            return packages[-1], ""

    return "", ""


def parse_bounds(
    value: str,
) -> Bounds | None:
    match = re.fullmatch(
        r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]",
        value.strip(),
    )

    if match is None:
        return None

    return Bounds(
        *map(
            int,
            match.groups(),
        )
    )


def parse_ui_xml(
    xml: str,
) -> list[UiNode]:
    try:
        root = ET.fromstring(
            xml
        )
    except ET.ParseError as exc:
        raise AdbError(
            f"UI XML inválido: {exc}"
        ) from exc

    nodes: list[UiNode] = []

    for index, element in enumerate(
        root.iter("node")
    ):
        attrs = element.attrib
        bounds = parse_bounds(
            attrs.get(
                "bounds",
                "",
            )
        )

        if bounds is None:
            continue

        nodes.append(
            UiNode(
                index=index,
                class_name=attrs.get(
                    "class",
                    "",
                ),
                text=attrs.get(
                    "text",
                    "",
                ),
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
                clickable=attrs.get(
                    "clickable",
                    "false",
                ).lower() == "true",
                scrollable=attrs.get(
                    "scrollable",
                    "false",
                ).lower() == "true",
                enabled=attrs.get(
                    "enabled",
                    "true",
                ).lower() == "true",
                visible=attrs.get(
                    "visible-to-user",
                    "true",
                ).lower() == "true",
                bounds=bounds,
            )
        )

    return nodes


def dump_ui(
    device: Device,
) -> str:
    remote = "/sdcard/adbchaos_window.xml"

    result = str(
        run_adb(
            device.serial,
            "shell",
            "uiautomator",
            "dump",
            remote,
            timeout=20,
        )
    )

    if "ERROR" in result.upper():
        raise AdbError(
            f"uiautomator dump falló: {result.strip()}"
        )

    return str(
        run_adb(
            device.serial,
            "shell",
            "cat",
            remote,
            timeout=10,
        )
    )


def make_ui_snapshot(
    device: Device,
) -> UiSnapshot:
    xml = dump_ui(
        device
    )
    nodes = parse_ui_xml(
        xml
    )
    package, activity = get_current_window(
        device
    )

    visible = [
        (
            node.package,
            node.short_class,
            normalize_text(node.text),
            normalize_text(
                node.content_desc
            ),
            normalize_resource(
                node.resource_id
            ),
            node.clickable,
            node.scrollable,
            node.enabled,
            node.bounds.coarse(),
        )
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
        / (
            f"{stamp}_"
            f"{safe_name(device.serial)}"
        )
    )
    folder.mkdir(
        parents=True,
        exist_ok=True,
    )
    return folder


def save_session_metadata(
    session_dir: Path,
    device: Device,
    size: tuple[int, int],
    config: Config,
) -> None:
    payload = {
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
        "config": {
            "mode": config.mode,
            "min_delay": config.min_delay,
            "max_delay": config.max_delay,
            "duration": config.duration,
            "screenshot_every": (
                config.screenshot_every
            ),
            "max_screenshots": (
                config.max_screenshots
            ),
            "capture_on_change": (
                config.capture_on_change
            ),
            "dump_ui": config.dump_ui,
            "preview": config.preview,
            "persistent_memory": (
                config.persistent_memory
            ),
            "memory_file": config.memory_file,
            "smart_back_after": (
                config.smart_back_after
            ),
            "max_consecutive_no_change": (
                config.max_consecutive_no_change
            ),
            "seed": config.seed,
        },
    }

    (session_dir / "session.json").write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def append_event(
    session_dir: Path,
    event: dict[str, Any],
) -> None:
    with (
        session_dir
        / "events.jsonl"
    ).open(
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
    number: int,
) -> Path:
    output = run_adb(
        device.serial,
        "exec-out",
        "screencap",
        "-p",
        binary=True,
        timeout=20,
    )

    if not isinstance(
        output,
        bytes,
    ) or not output.startswith(
        b"\x89PNG"
    ):
        raise AdbError(
            "ADB no devolvió un PNG válido."
        )

    path = (
        session_dir
        / f"{number:05d}.png"
    )
    path.write_bytes(
        output
    )
    return path


def save_ui_xml(
    session_dir: Path,
    number: int,
    xml: str,
) -> Path:
    folder = session_dir / "ui"
    folder.mkdir(
        parents=True,
        exist_ok=True,
    )

    path = (
        folder
        / f"{number:05d}.xml"
    )
    path.write_text(
        xml,
        encoding="utf-8",
    )
    return path


def maybe_preview(
    path: Path,
) -> None:
    if shutil.which(
        "chafa"
    ) is None:
        return

    try:
        result = subprocess.run(
            [
                "chafa",
                "--format=symbols",
                "--size=54x24",
                str(path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if result.returncode == 0:
            print(
                result.stdout.decode(
                    "utf-8",
                    errors="replace",
                )
            )
    except (
        OSError,
        subprocess.TimeoutExpired,
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

    cx, cy = bounds.center

    jitter_x = min(
        max(1, bounds.width // 9),
        16,
    )
    jitter_y = min(
        max(1, bounds.height // 9),
        16,
    )

    x = max(
        bounds.left + 1,
        min(
            bounds.right - 1,
            cx + random.randint(
                -jitter_x,
                jitter_x,
            ),
        ),
    )
    y = max(
        bounds.top + 1,
        min(
            bounds.bottom - 1,
            cy + random.randint(
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
        f"tap ({x},{y}) "
        f"[{node.label[:70]}]"
    )


def smart_swipe(
    device: Device,
    node: UiNode | None,
    width: int,
    height: int,
    direction: str,
) -> str:
    if node is None:
        bounds = Bounds(
            int(width * 0.10),
            int(height * 0.16),
            int(width * 0.90),
            int(height * 0.84),
        )
    else:
        bounds = node.bounds

    bounds = bounds.clamp(
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
        > bounds.height * 1.5
    )

    if horizontal:
        center_y = (
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

        y1 = y2 = center_y
    else:
        center_x = (
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

        x1 = x2 = center_x

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
        f"{bounds.as_string()} "
        f"{duration_ms}ms"
    )


def semantic_score(
    node: UiNode,
    width: int,
    height: int,
) -> float:
    score = 1.0

    label = normalize_text(
        " ".join(
            (
                node.text,
                node.content_desc,
                node.resource_id,
            )
        )
    )

    for word, bonus in POSITIVE_LABELS.items():
        if re.search(
            rf"\b{re.escape(word)}\b",
            label,
        ):
            score += bonus

    class_name = node.short_class

    for marker, bonus in SHORT_CLASS_BONUS.items():
        if marker in class_name:
            score += bonus
            break

    if node.text.strip():
        score += 1.5

    if node.content_desc.strip():
        score += 1.0

    if (
        node.bounds.width >= 40
        and node.bounds.height >= 30
    ):
        score += 1.0

    area_ratio = (
        node.bounds.area
        / max(
            1,
            width * height,
        )
    )

    if 0.001 <= area_ratio <= 0.25:
        score += 1.5
    elif area_ratio > 0.65:
        score -= 3.0

    center_x, center_y = node.bounds.center
    distance = (
        abs(center_x - width / 2)
        / max(
            1,
            width / 2,
        )
        + abs(center_y - height / 2)
        / max(
            1,
            height / 2,
        )
    )

    score += max(
        0.0,
        1.7 - distance,
    )

    return max(
        0.1,
        score,
    )


def choose_smart_target(
    snapshot: UiSnapshot,
    brain: Brain,
    width: int,
    height: int,
) -> tuple[UiNode, float] | None:
    candidates = snapshot.clickable

    if not candidates:
        return None

    scored: list[
        tuple[float, UiNode]
    ] = []

    visits = brain.states.get(
        snapshot.fingerprint,
        StateStats(),
    ).visits

    for node in candidates:
        stats = brain.target(
            snapshot.fingerprint,
            node.key,
        )

        score = semantic_score(
            node,
            width,
            height,
        )
        score += stats.score()

        if stats.attempts == 0:
            score += 9.0

        if visits >= 5 and stats.attempts == 0:
            score += 5.0

        # Penalize a target whose known destination is simply the current state.
        if (
            stats.last_destination
            and stats.last_destination
            == snapshot.fingerprint
        ):
            score -= 8.0

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
        1.9
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

    score, node = random.choices(
        frontier,
        weights=weights,
        k=1,
    )[0]

    return node, score


def is_dialog(
    snapshot: UiSnapshot,
) -> bool:
    labels = " ".join(
        normalize_text(value)
        for value in snapshot.text_labels
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

    dialog_class = any(
        "dialog" in node.class_name.lower()
        for node in snapshot.nodes
        if node.visible
    )

    return (
        dialog_class
        and len(snapshot.clickable) <= 8
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

    scored: list[
        tuple[float, UiNode]
    ] = []

    for node in snapshot.clickable:
        label = normalize_text(
            node.label
        )

        score = semantic_score(
            node,
            1000,
            2000,
        )

        for index, word in enumerate(
            priority
        ):
            if re.search(
                rf"\b{re.escape(word)}\b",
                label,
            ):
                score += (
                    14.0
                    - index * 1.5
                )

        score += brain.target(
            snapshot.fingerprint,
            node.key,
        ).score()

        scored.append(
            (
                score,
                node,
            )
        )

    if not scored:
        return None

    scored.sort(
        key=lambda item: item[0],
        reverse=True,
    )
    return scored[0][1]


def choose_scroll_direction(
    snapshot: UiSnapshot,
    brain: Brain,
) -> str:
    up = brain.target(
        snapshot.fingerprint,
        "SCROLL::up",
    )
    down = brain.target(
        snapshot.fingerprint,
        "SCROLL::down",
    )

    if (
        up.attempts == 0
        and down.attempts > 0
    ):
        return "up"

    if (
        down.attempts == 0
        and up.attempts > 0
    ):
        return "down"

    up_score = up.score()
    down_score = down.score()

    if abs(
        up_score - down_score
    ) < 1.5:
        return random.choice(
            (
                "up",
                "down",
            )
        )

    return (
        "up"
        if up_score > down_score
        else "down"
    )


def smart_decision(
    device: Device,
    snapshot: UiSnapshot,
    brain: Brain,
    width: int,
    height: int,
    config: Config,
) -> tuple[str, str, str]:
    state = brain.state(
        snapshot
    )

    if (
        brain.no_change_streak
        >= config.smart_back_after
    ):
        state.dead_end_count += 1
        return (
            action_key(
                device,
                "4",
                "Back (stuck state)",
            ),
            "BACK",
            "backtracking: repeated state",
        )

    if is_dialog(
        snapshot
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
                "dialog strategy",
            )

    scrollables = snapshot.scrollables
    candidates = snapshot.clickable

    if (
        scrollables
        and random.random() < 0.22
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
        direction = choose_scroll_direction(
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
            f"scroll exploration: {direction}",
        )

    if candidates:
        choice = choose_smart_target(
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
                "learned actionable target",
            )

    if scrollables:
        node = scrollables[0]
        direction = choose_scroll_direction(
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
            "fallback scroll",
        )

    state.dead_end_count += 1

    return (
        action_key(
            device,
            "4",
            "Back (dead end)",
        ),
        "BACK",
        "no actionable or scrollable UI",
    )


def random_action(
    device: Device,
    width: int,
    height: int,
) -> tuple[str, str]:
    action = random.choices(
        (
            "tap",
            "swipe",
            "back",
            "home",
            "volume",
        ),
        weights=(
            34,
            31,
            20,
            4,
            11,
        ),
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
            f"random tap ({x},{y})",
            "RANDOM_TAP",
        )

    if action == "swipe":
        direction = random.choice(
            (
                "up",
                "down",
            )
        )
        smart_swipe(
            device,
            None,
            width,
            height,
            direction,
        )
        return (
            f"random swipe {direction}",
            f"RANDOM_SCROLL::{direction}",
        )

    if action == "back":
        return (
            action_key(
                device,
                "4",
                "Back",
            ),
            "BACK",
        )

    if action == "home":
        return (
            action_key(
                device,
                "3",
                "Home",
            ),
            "HOME",
        )

    key = random.choice(
        (
            "24",
            "25",
        )
    )

    return (
        action_key(
            device,
            key,
            "Volume up"
            if key == "24"
            else "Volume down",
        ),
        "VOLUME",
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

    save_session_metadata(
        session_dir,
        device,
        (
            width,
            height,
        ),
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
    action_number = 0
    screenshot_number = 0
    ui_number = 0
    started = time.monotonic()
    previous_state = ""
    old_sigint = signal.getsignal(
        signal.SIGINT
    )
    old_sigterm = signal.getsignal(
        signal.SIGTERM
    )

    def stop_handler(
        signum: int,
        frame: Any,
    ) -> None:
        nonlocal stopped
        stopped = True
        print()
        log(
            "Parada solicitada. Cerrando de forma limpia...",
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
        # Initial observation and initial capture.
        initial: UiSnapshot | None = None

        if config.mode == "smart":
            try:
                initial = make_ui_snapshot(
                    device
                )
                previous_state = (
                    initial.fingerprint
                )
                brain.last_state = (
                    initial.fingerprint
                )
                brain.state(
                    initial
                )
            except AdbError as exc:
                log(
                    f"UI inicial no disponible: {exc}",
                    ANSI_YELLOW,
                )

        if (
            initial is not None
            and config.dump_ui
            and ui_number
            < config.max_screenshots
        ):
            ui_number += 1
            save_ui_xml(
                session_dir,
                ui_number,
                initial.xml,
            )

        if config.screenshot_every > 0:
            try:
                screenshot_number += 1
                path = capture_screenshot(
                    device,
                    session_dir,
                    screenshot_number,
                )
                if config.preview:
                    maybe_preview(
                        path
                    )
            except AdbError as exc:
                log(
                    f"Captura inicial fallida: {exc}",
                    ANSI_YELLOW,
                )

        append_event(
            session_dir,
            {
                "action": 0,
                "type": "initial_state",
                "timestamp": datetime.now().isoformat(
                    timespec="seconds"
                ),
                "status": "ok",
                "package": (
                    initial.package
                    if initial
                    else ""
                ),
                "activity": (
                    initial.activity
                    if initial
                    else ""
                ),
                "state": (
                    initial.fingerprint
                    if initial
                    else ""
                ),
                "clickable": (
                    len(initial.clickable)
                    if initial
                    else 0
                ),
                "scrollable": (
                    len(initial.scrollables)
                    if initial
                    else 0
                ),
            },
        )

        while not stopped:
            if (
                config.duration is not None
                and (
                    time.monotonic()
                    - started
                ) >= config.duration
            ):
                break

            action_number += 1
            before: UiSnapshot | None = None
            after: UiSnapshot | None = None
            action_text = ""
            action_id = ""
            decision = ""
            status = "ok"
            error = None
            changed = False
            novel = False

            try:
                if config.mode == "smart":
                    before = make_ui_snapshot(
                        device
                    )

                    if (
                        previous_state
                        and before.fingerprint
                        == previous_state
                    ):
                        brain.no_change_streak += 1
                    else:
                        brain.no_change_streak = 0

                    (
                        action_text,
                        action_id,
                        decision,
                    ) = smart_decision(
                        device,
                        before,
                        brain,
                        width,
                        height,
                        config,
                    )
                else:
                    (
                        action_text,
                        action_id,
                    ) = random_action(
                        device,
                        width,
                        height,
                    )

            except AdbError as exc:
                status = "error"
                error = str(exc)

            # Give Android time to finish navigation/animation.
            time.sleep(
                max(
                    0.20,
                    min(
                        1.0,
                        config.min_delay * 0.55,
                    ),
                )
            )

            if (
                config.mode == "smart"
                and status == "ok"
            ):
                try:
                    after = make_ui_snapshot(
                        device
                    )

                    if before is not None:
                        changed = (
                            before.fingerprint
                            != after.fingerprint
                        )

                        novel = (
                            after.fingerprint
                            not in brain.states
                        )

                        brain.record_result(
                            before.fingerprint,
                            action_id,
                            after.fingerprint,
                            changed,
                            novel,
                        )

                    if novel:
                        brain.state(
                            after
                        )

                    if changed:
                        brain.no_change_streak = 0
                    else:
                        brain.no_change_streak += 1

                    previous_state = (
                        after.fingerprint
                    )
                    brain.last_state = (
                        after.fingerprint
                    )

                except AdbError as exc:
                    status = "ui_error"
                    error = (
                        f"Post-action UI: {exc}"
                    )

            state_changed_capture = (
                config.capture_on_change
                and changed
            )

            should_capture = (
                config.screenshot_every > 0
                and (
                    action_number
                    % config.screenshot_every
                    == 0
                )
            ) or state_changed_capture

            screenshot_path: str | None = None

            if (
                should_capture
                and screenshot_number
                < config.max_screenshots
            ):
                try:
                    screenshot_number += 1
                    path = capture_screenshot(
                        device,
                        session_dir,
                        screenshot_number,
                    )
                    screenshot_path = str(
                        path
                    )

                    if config.preview:
                        maybe_preview(
                            path
                        )

                except AdbError as exc:
                    error = (
                        f"Screenshot: {exc}"
                    )

            xml_path: str | None = None
            chosen_snapshot = (
                after
                or before
            )

            if (
                chosen_snapshot is not None
                and config.dump_ui
                and ui_number
                < config.max_screenshots
            ):
                try:
                    ui_number += 1
                    path = save_ui_xml(
                        session_dir,
                        ui_number,
                        chosen_snapshot.xml,
                    )
                    xml_path = str(
                        path
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

            target_stats: dict[str, Any] = {}

            if (
                config.mode == "smart"
                and before is not None
            ):
                stats = brain.target(
                    before.fingerprint,
                    action_id,
                )
                target_stats = {
                    "attempts": stats.attempts,
                    "changed": stats.changed,
                    "novel": stats.novel,
                    "no_change": stats.no_change,
                    "last_destination": (
                        stats.last_destination
                    ),
                }

            append_event(
                session_dir,
                {
                    "action": action_number,
                    "type": (
                        "smart_ui_action"
                        if config.mode == "smart"
                        else "random_action"
                    ),
                    "timestamp": datetime.now().isoformat(
                        timespec="seconds"
                    ),
                    "status": status,
                    "error": error,
                    "description": action_text,
                    "decision": decision,
                    "target": action_id,
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
                        brain.states
                    ),
                    "known_targets": len(
                        brain.targets
                    ),
                    "graph_edges": sum(
                        len(edges)
                        for edges in brain.graph.values()
                    ),
                    "target_stats": target_stats,
                    "battery": get_battery(
                        device
                    ),
                    "screenshot": screenshot_path,
                    "ui_xml": xml_path,
                },
            )

            if status == "ok":
                result_label = (
                    "NEW STATE"
                    if novel
                    else (
                        "CHANGED"
                        if changed
                        else "NO CHANGE"
                    )
                )

                log(
                    f"#{action_number:04d} "
                    f"{action_text} | "
                    f"{result_label} | "
                    f"{package or '?'} | "
                    f"states={len(brain.states)} "
                    f"edges={sum(len(v) for v in brain.graph.values())} "
                    f"| 🔋 {get_battery(device)}"
                    + (
                        f" | 📸 {screenshot_path}"
                        if screenshot_path
                        else ""
                    ),
                    ANSI_GREEN,
                )
            else:
                log(
                    f"#{action_number:04d} "
                    f"{action_text or 'action'}: "
                    f"{error}",
                    ANSI_RED,
                )

            if (
                screenshot_number
                >= config.max_screenshots
                and config.max_screenshots > 0
            ):
                log(
                    "Límite de capturas alcanzado.",
                    ANSI_YELLOW,
                )

            if (
                ui_number
                >= config.max_screenshots
                and config.max_screenshots > 0
            ):
                # Do not spam this line repeatedly.
                if ui_number == config.max_screenshots:
                    log(
                        "Límite de UI XML alcanzado.",
                        ANSI_YELLOW,
                    )

            if config.persistent_memory:
                try:
                    brain.save(
                        memory_path
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

            if (
                config.mode == "smart"
                and brain.no_change_streak
                >= config.max_consecutive_no_change
            ):
                brain.no_change_streak = (
                    config.smart_back_after - 1
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

        if config.persistent_memory:
            try:
                brain.save(
                    memory_path
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
            "actions": action_number,
            "screenshots": screenshot_number,
            "ui_xml_files": ui_number,
            "known_states": len(
                brain.states
            ),
            "known_targets": len(
                brain.targets
            ),
            "graph_edges": sum(
                len(edges)
                for edges in brain.graph.values()
            ),
            "elapsed_seconds": round(
                elapsed,
                2,
            ),
            "memory_file": (
                str(memory_path)
                if config.persistent_memory
                else None
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
            f"{action_number} acciones | "
            f"{screenshot_number} capturas | "
            f"{len(brain.states)} estados | "
            f"{sum(len(v) for v in brain.graph.values())} edges | "
            f"{elapsed:.1f}s",
            ANSI_CYAN,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "ADB Chaos: exploración Android adaptativa "
            "con memoria, grafo y capturas."
        )
    )

    parser.add_argument(
        "--serial",
        help="Serial ADB concreto.",
    )

    parser.add_argument(
        "--mode",
        choices=(
            "smart",
            "random",
        ),
        default="smart",
        help="Modo de exploración. Por defecto: smart.",
    )

    parser.add_argument(
        "--min-delay",
        type=float,
        default=0.55,
        help="Espera mínima entre acciones.",
    )

    parser.add_argument(
        "--max-delay",
        type=float,
        default=1.80,
        help="Espera máxima entre acciones.",
    )

    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Duración máxima en segundos.",
    )

    parser.add_argument(
        "--screenshot-every",
        type=int,
        default=1,
        help=(
            "Capturar cada N acciones. "
            "Usa 0 para desactivar las capturas periódicas."
        ),
    )

    parser.add_argument(
        "--max-screenshots",
        type=int,
        default=300,
        help="Máximo de PNG/XML por sesión.",
    )

    parser.add_argument(
        "--capture-on-change",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Capturar cuando cambia el estado de la UI.",
    )

    parser.add_argument(
        "--dump-ui",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Guardar jerarquía UI XML.",
    )

    parser.add_argument(
        "--preview",
        action="store_true",
        help="Mostrar las capturas con chafa.",
    )

    parser.add_argument(
        "--persistent-memory",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Aprender entre sesiones.",
    )

    parser.add_argument(
        "--memory-file",
        default="chaos_memory.json",
        help="Archivo de memoria persistente.",
    )

    parser.add_argument(
        "--smart-back-after",
        type=int,
        default=3,
        help="Back tras N observaciones consecutivas sin cambio.",
    )

    parser.add_argument(
        "--max-consecutive-no-change",
        type=int,
        default=7,
        help="Umbral para recuperar un estado estancado.",
    )

    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Semilla aleatoria para repetir una prueba.",
    )

    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Salir si no hay un dispositivo autorizado.",
    )

    return parser


def validate_config(
    config: Config,
) -> None:
    if config.min_delay < 0:
        raise ValueError(
            "--min-delay no puede ser negativo."
        )

    if config.max_delay < config.min_delay:
        raise ValueError(
            "--max-delay debe ser >= --min-delay."
        )

    if config.duration is not None and config.duration <= 0:
        raise ValueError(
            "--duration debe ser > 0."
        )

    if config.screenshot_every < 0:
        raise ValueError(
            "--screenshot-every debe ser >= 0."
        )

    if config.max_screenshots < 0:
        raise ValueError(
            "--max-screenshots debe ser >= 0."
        )

    if config.smart_back_after < 1:
        raise ValueError(
            "--smart-back-after debe ser >= 1."
        )

    if config.max_consecutive_no_change < 1:
        raise ValueError(
            "--max-consecutive-no-change debe ser >= 1."
        )

    if not config.memory_file.strip():
        raise ValueError(
            "--memory-file no puede estar vacío."
        )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    config = Config(
        serial=args.serial,
        mode=args.mode,
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        duration=args.duration,
        screenshot_every=args.screenshot_every,
        max_screenshots=args.max_screenshots,
        capture_on_change=args.capture_on_change,
        dump_ui=args.dump_ui,
        preview=args.preview,
        persistent_memory=args.persistent_memory,
        memory_file=args.memory_file,
        smart_back_after=args.smart_back_after,
        max_consecutive_no_change=(
            args.max_consecutive_no_change
        ),
        seed=args.seed,
        wait_for_device=not args.no_wait,
    )

    try:
        validate_config(
            config
        )
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
                "Activa Depuración USB y acepta la huella RSA.",
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
