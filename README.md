
# ADB Chaos

A tiny terminal-first Android random-control playground built for Linux and Raspberry Pi.

> Connect an Android phone with USB debugging enabled, run the program, and ADB Chaos waits for an authorized device. Once it appears, the chaos starts automatically.

## What it does

- Detects an authorized ADB device automatically.
- Randomly taps inside the screen.
- Performs random swipes.
- Occasionally presses Home or Back.
- Occasionally changes the volume.
- Takes a PNG screenshot after each action by default.
- Stores every action in `events.jsonl`.
- Stores session/device/config information in `session.json`.
- Stores a final summary in `summary.json`.
- Caps the number of screenshots per session so an unattended run does not fill the SD card.
- Optional terminal image previews using `chafa`.

It deliberately does **not** run arbitrary shell commands on the phone.

## Debian 13 / Trixie setup

The simplest setup is:

```bash
sudo apt update
sudo apt install adb python3
```

Then:

```bash
git clone https://github.com/alphacat731-rgb/adbcontrol.git
cd adbcontrol
python3 adb_chaos.py
```

The program will wait until an authorized device appears.

On the phone, enable **Developer options → USB debugging**, plug the phone in, and accept the computer's RSA fingerprint prompt.

## Useful commands

Run for 60 seconds:

```bash
python3 adb_chaos.py --duration 60
```

Slow it down:

```bash
python3 adb_chaos.py --min-delay 1.0 --max-delay 3.0
```

Capture every 5 actions:

```bash
python3 adb_chaos.py --screenshot-every 5
```

Disable screenshots:

```bash
python3 adb_chaos.py --screenshot-every 0
```

Show screenshots in the terminal when `chafa` is installed:

```bash
sudo apt install chafa
python3 adb_chaos.py --preview
```

Use one specific device:

```bash
adb devices
python3 adb_chaos.py --serial YOUR_SERIAL
```

Repeat a test pattern:

```bash
python3 adb_chaos.py --seed 1234 --duration 30
```

## Session layout

Each run creates a folder like:

```text
sessions/
└── 2026-10-01_21-30-12_R58M123ABC/
    ├── 00001.png
    ├── 00002.png
    ├── 00003.png
    ├── events.jsonl
    ├── session.json
    └── summary.json
```

The first PNG is the phone's initial state before random actions begin.

## Safety / storage notes

This tool is intended for a device you own or are authorized to control. Keep an eye on the phone while experimenting: random taps are random.

The default session limit is 300 screenshots. Change it with `--max-screenshots` if you need a smaller or larger run.

Press **Ctrl+C** to stop.

## Requirements

- Linux / Raspberry Pi
- Python 3.10+
- ADB
- Android device with USB debugging enabled
- No Python third-party packages required

## License

MIT
