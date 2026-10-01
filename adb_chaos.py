
#!/usr/bin/env python3
"""
ADB Chaos - a small, terminal-first Android random-control playground.

Designed for Linux/Raspberry Pi. It uses only Python's standard library and
the external adb executable.

The program intentionally sticks to low-risk UI actions:
tap, swipe, Home, Back, volume, and screenshots. It does not execute
arbitrary shell commands on the device.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable


APP_NAME = "ADB CHAOS"
VERSION = "0.1.0"

ANSI_RESET = "\033[0m"
ANSI_CYAN = "\033[96m"
ANSI_GREEN = "\033[92m"
ANSI_YELLOW = "\033[93m"
ANSI_RED = "\033[91m"
ANSI_DIM = "\033[2m"
ANSI_BOLD = "\033[1m"


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


class AdbError(RuntimeError):
    pass


def log(message: str, color: str = ANSI_RESET) -> None:
    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"{ANSI_DIM}[{stamp}]{ANSI_RESET} {color}{message}{ANSI_RESET}", flush=True)


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
        raise AdbError(f"ADB timeout: {' '.join(command)}") from exc

    if completed.returncode != 0:
        stderr = completed.stderr.decode("utf-8", errors="replace").strip()
        raise AdbError(stderr or f"ADB salió con código {completed.returncode}")

    return completed.stdout if binary else completed.stdout.decode(
        "utf-8", errors="replace"
    )


def ensure_adb() -> None:
    if shutil.which("adb") is None:
        raise AdbError(
            "No encuentro 'adb'. En Debian Trixie puedes instalarlo con "
            "'sudo apt install adb'."
        )


def parse_devices(raw: str) -> list[Device]:
    devices: list[Device] = []
    for line in raw.splitlines():
        if not line.strip() or line.startswith("List of devices attached"):
            continue

        parts = line.split()
        if len(parts) < 2:
            continue

        serial, state = parts[0], parts[1]
        model = ""
        product = ""

        for item in parts[2:]:
            if item.startswith("model:"):
                model = item.split(":", 1)[1].replace("_", " ")
            elif item.startswith("product:"):
                product = item.split(":", 1)[1]

        devices.append(Device(serial, state, model, product))
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
        raise AdbError(raw.stderr.decode("utf-8", errors="replace").strip())

    return parse_devices(raw.stdout.decode("utf-8", errors="replace"))


def choose_device(serial: str | None) -> Device | None:
    devices = list_devices()

    if serial:
        for device in devices:
            if device.serial == serial:
                return device
        return None

    authorized = [d for d in devices if d.authorized]
    if len(authorized) == 1:
        return authorized[0]

    if len(authorized) > 1:
        log("Hay varios dispositivos autorizados. Usa --serial SERIAL.", ANSI_YELLOW)
        for d in authorized:
            label = d.model or d.serial
            print(f"  • {d.serial}  ({label})")
        return None

    if devices:
        states = ", ".join(f"{d.serial}: {d.state}" for d in devices)
        log(
            f"Dispositivo encontrado, pero ninguno está autorizado: {states}",
            ANSI_YELLOW,
        )
    return None


def wait_for_device(serial: str | None, poll: float = 1.0) -> Device:
    while True:
        device = choose_device(serial)
        if device and device.authorized:
            return device
        time.sleep(poll)


def parse_screen_size(text: str) -> tuple[int, int] | None:
    match = re.search(r"(?:Physical|Override) size:\s*(\d+)x(\d+)", text)
    if not match:
        match = re.search(r"(\d+)x(\d+)", text)
    if not match:
        return None

    width, height = map(int, match.groups())
    return width, height


def get_screen_size(device: Device) -> tuple[int, int]:
    output = run_adb(device.serial, "shell", "wm", "size")
    parsed = parse_screen_size(str(output))
    if not parsed:
        raise AdbError(f"No pude detectar la resolución del móvil: {output!r}")
    return parsed


def get_battery(device: Device) -> str:
    try:
        output = str(
            run_adb(device.serial, "shell", "dumpsys", "battery", timeout=8)
        )
        level = re.search(r"level:\s*(\d+)", output)
        status = re.search(r"status:\s*(\d+)", output)
        if not level:
            return "?"
        status_name = {
            "2": "charging",
            "3": "discharging",
            "4": "not charging",
            "5": "full",
        }.get(status.group(1) if status else "", "")
        return f"{level.group(1)}% {status_name}".strip()
    except AdbError:
        return "?"


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return value.strip("._-") or "device"


def make_session_dir(device: Device) -> Path:
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    folder = Path("sessions") / f"{stamp}_{safe_name(device.serial)}"
    folder.mkdir(parents=True, exist_ok=True)
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
        "started_at": datetime.now().isoformat(timespec="seconds"),
        "device": {
            "serial": device.serial,
            "state": device.state,
            "model": device.model,
            "product": device.product,
        },
        "screen": {"width": size[0], "height": size[1]},
        "config": {
            "min_delay": config.min_delay,
            "max_delay": config.max_delay,
            "screenshot_every": config.screenshot_every,
            "max_screenshots": config.max_screenshots,
            "duration": config.duration,
            "preview": config.preview,
            "seed": config.seed,
        },
    }
    (session_dir / "session.json").write_text(
        json.dumps(metadata, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def append_event(session_dir: Path, event: dict) -> None:
    path = session_dir / "events.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def capture_screenshot(device: Device, session_dir: Path, index: int) -> Path:
    target = session_dir / f"{index:05d}.png"
    png = run_adb(
        device.serial,
        "exec-out",
        "screencap",
        "-p",
        binary=True,
        timeout=20.0,
    )
    if not isinstance(png, bytes) or not png.startswith(b"\x89PNG"):
        raise AdbError("ADB no devolvió una captura PNG válida.")
    target.write_bytes(png)
    return target


def maybe_preview(path: Path) -> None:
    if shutil.which("chafa") is None:
        return

    try:
        completed = subprocess.run(
            ["chafa", "--format=symbols", "--size=48x20", str(path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        if completed.returncode == 0:
            print(completed.stdout.decode("utf-8", errors="replace"))
    except (subprocess.TimeoutExpired, OSError):
        pass


def action_tap(device: Device, width: int, height: int) -> str:
    x = random.randint(max(1, int(width * 0.08)), max(1, int(width * 0.92)))
    y = random.randint(max(1, int(height * 0.08)), max(1, int(height * 0.92)))
    run_adb(device.serial, "shell", "input", "tap", str(x), str(y))
    return f"tap ({x}, {y})"


def action_swipe(device: Device, width: int, height: int) -> str:
    x1 = random.randint(int(width * 0.20), int(width * 0.80))
    x2 = random.randint(int(width * 0.20), int(width * 0.80))
    y1 = random.randint(int(height * 0.25), int(height * 0.75))
    y2 = random.randint(int(height * 0.25), int(height * 0.75))

    if abs(y2 - y1) < height * 0.18:
        y2 = max(
            10,
            min(
                height - 10,
                y1 + random.choice([-1, 1]) * int(height * 0.30),
            ),
        )

    duration_ms = random.randint(250, 900)
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
    return f"swipe ({x1},{y1}) -> ({x2},{y2}) {duration_ms}ms"


def action_key(device: Device, keycode: str, label: str) -> str:
    run_adb(device.serial, "shell", "input", "keyevent", keycode)
    return label


def action_volume(device: Device) -> str:
    key = random.choice(["24", "25"])
    label = "volume up" if key == "24" else "volume down"
    return action_key(device, key, label)


def action_screen_refresh(device: Device) -> str:
    get_screen_size(device)
    return "screen probe"


def action_random_tap_burst(device: Device, width: int, height: int) -> str:
    count = random.randint(2, 4)
    points: list[str] = []
    for _ in range(count):
        points.append(action_tap(device, width, height))
        time.sleep(random.uniform(0.08, 0.20))
    return f"tap burst x{count} [{', '.join(points)}]"


def run_chaos(device: Device, config: Config) -> None:
    width, height = get_screen_size(device)
    session_dir = make_session_dir(device)
    save_metadata(session_dir, device, (width, height), config)

    stop_requested = False

    def stop_handler(signum: int, frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True
        print()
        log(
            "Parada solicitada. Terminando después de la acción actual...",
            ANSI_YELLOW,
        )

    old_sigint = signal.signal(signal.SIGINT, stop_handler)
    old_sigterm = signal.signal(signal.SIGTERM, stop_handler)

    action_no = 0
    screenshot_no = 0
    started = time.monotonic()
    announced_screenshot_cap = False

    action_choices: list[tuple[str, int, Callable[[], str]]] = [
        ("tap", 38, lambda: action_tap(device, width, height)),
        ("swipe", 28, lambda: action_swipe(device, width, height)),
        (
            "tap_burst",
            8,
            lambda: action_random_tap_burst(device, width, height),
        ),
        ("back", 9, lambda: action_key(device, "4", "Back")),
        ("home", 5, lambda: action_key(device, "3", "Home")),
        ("volume", 4, lambda: action_volume(device)),
        ("screen_probe", 8, lambda: action_screen_refresh(device)),
    ]

    log(f"{ANSI_BOLD}{APP_NAME} {VERSION}{ANSI_RESET}", ANSI_CYAN)
    log(
        f"Device: {device.serial} | {device.model or 'unknown model'}",
        ANSI_GREEN,
    )
    log(f"Screen: {width}x{height} | Session: {session_dir}", ANSI_CYAN)
    log("Ctrl+C para detener.", ANSI_YELLOW)

    # Initial state capture: useful for comparing the run with what happened
    # after the random actions begin.
    if config.screenshot_every > 0 and config.max_screenshots > 0:
        try:
            screenshot_no += 1
            screenshot = capture_screenshot(device, session_dir, screenshot_no)
            append_event(
                session_dir,
                {
                    "action": 0,
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "type": "initial_screenshot",
                    "description": "initial state",
                    "status": "ok",
                    "error": None,
                    "screenshot": str(screenshot),
                },
            )
            log(f"📸 Captura inicial → {screenshot}", ANSI_CYAN)
            if config.preview:
                maybe_preview(screenshot)
        except AdbError as exc:
            log(f"No se pudo guardar la captura inicial: {exc}", ANSI_YELLOW)

    try:
        while not stop_requested:
            if (
                config.duration is not None
                and time.monotonic() - started >= config.duration
            ):
                break

            names = [item[0] for item in action_choices]
            weights = [item[1] for item in action_choices]
            selected_name = random.choices(names, weights=weights, k=1)[0]
            action = next(
                item for item in action_choices if item[0] == selected_name
            )

            action_no += 1
            action_started = datetime.now().isoformat(timespec="seconds")

            try:
                description = action[2]()
                status = "ok"
                error = None
            except AdbError as exc:
                description = ""
                status = "error"
                error = str(exc)

            screenshot_path: str | None = None
            should_capture = (
                config.screenshot_every > 0
                and action_no % config.screenshot_every == 0
                and screenshot_no < config.max_screenshots
            )

            if should_capture and status == "ok":
                try:
                    screenshot_no += 1
                    screenshot = capture_screenshot(
                        device, session_dir, screenshot_no
                    )
                    screenshot_path = str(screenshot)
                    if config.preview:
                        maybe_preview(screenshot)
                except AdbError as exc:
                    error = f"Screenshot: {exc}"
                    status = "screenshot_error"

            event = {
                "action": action_no,
                "timestamp": action_started,
                "type": selected_name,
                "description": description,
                "status": status,
                "error": error,
                "screenshot": screenshot_path,
            }
            append_event(session_dir, event)

            if status == "ok":
                battery = get_battery(device)
                shot = (
                    f" | 📸 {screenshot_path}" if screenshot_path else ""
                )
                log(
                    f"#{action_no:04d} {description} | 🔋 {battery}{shot}",
                    ANSI_GREEN,
                )
            else:
                log(
                    f"#{action_no:04d} {selected_name}: {error}",
                    ANSI_RED,
                )
                time.sleep(1.5)

            if (
                screenshot_no >= config.max_screenshots
                and not announced_screenshot_cap
                and config.max_screenshots > 0
            ):
                announced_screenshot_cap = True
                log(
                    "Límite de capturas alcanzado; se continúa sin crear más PNG.",
                    ANSI_YELLOW,
                )

            delay = random.uniform(config.min_delay, config.max_delay)
            time.sleep(delay)

    finally:
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGTERM, old_sigterm)

    elapsed = time.monotonic() - started
    summary = {
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "actions": action_no,
        "screenshots": screenshot_no,
        "elapsed_seconds": round(elapsed, 2),
        "session_dir": str(session_dir),
    }
    (session_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    log(
        f"Sesión terminada: {action_no} acciones, {screenshot_no} capturas, "
        f"{elapsed:.1f}s → {session_dir}",
        ANSI_CYAN,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Control aleatorio de un Android mediante ADB, "
            "con capturas por sesión."
        )
    )
    parser.add_argument("--serial", help="Serial ADB concreto a controlar.")
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
        help="Máximo de PNG por sesión.",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help="Duración máxima en segundos. Sin límite si se omite.",
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
        help="Semilla aleatoria para repetir una sesión de pruebas.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help=(
            "No esperar a que aparezca un móvil autorizado; "
            "salir si no está conectado."
        ),
    )
    return parser


def validate_config(config: Config) -> None:
    if config.min_delay < 0 or config.max_delay < 0:
        raise ValueError("Los delays no pueden ser negativos.")
    if config.max_delay < config.min_delay:
        raise ValueError("--max-delay debe ser >= --min-delay.")
    if config.screenshot_every < 0:
        raise ValueError("--screenshot-every debe ser >= 0.")
    if config.max_screenshots < 0:
        raise ValueError("--max-screenshots debe ser >= 0.")
    if config.duration is not None and config.duration <= 0:
        raise ValueError("--duration debe ser mayor que 0.")


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
    )

    try:
        validate_config(config)
        ensure_adb()

        if config.seed is not None:
            random.seed(config.seed)

        log("Buscando dispositivo ADB...", ANSI_CYAN)
        device = choose_device(config.serial)

        if device is None and config.wait_for_device:
            log(
                "Esperando un móvil autorizado por USB...",
                ANSI_YELLOW,
            )
            device = wait_for_device(config.serial)

        if device is None:
            log(
                "No hay un dispositivo autorizado. Activa 'Depuración USB' "
                "y acepta la huella RSA en el móvil.",
                ANSI_RED,
            )
            return 2

        if not device.authorized:
            log(
                f"El dispositivo está en estado '{device.state}', no 'device'.",
                ANSI_RED,
            )
            return 2

        run_chaos(device, config)
        return 0

    except (AdbError, ValueError) as exc:
        log(str(exc), ANSI_RED)
        return 1
    except KeyboardInterrupt:
        print()
        log("Interrumpido.", ANSI_YELLOW)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
