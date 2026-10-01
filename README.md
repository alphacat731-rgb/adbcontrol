
# ADB Chaos

A terminal-first Android UI exploration playground for Linux and Raspberry Pi.

## v0.2: Intelligent mode

The default mode is now smart. Instead of blindly throwing taps at random
screen coordinates, ADB Chaos:

- Dumps Android's accessibility/UI hierarchy before making a decision.
- Finds visible, enabled, clickable controls and uses their real bounds.
- Detects scrollable containers and can swipe inside them.
- Prefers navigation-like controls such as Open, Next, Continue, More, Menu,
  Start, Play, View, and Details.
- Remembers controls it already tried on the same screen and penalizes repeats.
- Generates a fingerprint for each observed UI state.
- Detects no-change loops and backs out when it gets stuck.
- Records the current package/activity when Android exposes it.
- Saves UI XML alongside screenshots for post-run inspection.
- Skips labels strongly associated with destructive actions, payments,
  authentication, communication, permissions, installs, and account operations.

The original random behaviour is still available:

~~~
python3 adb_chaos.py --mode random
~~~

## Install on Debian Trixie / Raspberry Pi

~~~
sudo apt update
sudo apt install -y adb python3
~~~

Then:

~~~
git clone https://github.com/alphacat731-rgb/adbcontrol.git
cd adbcontrol
python3 adb_chaos.py
~~~

The program waits for an authorized ADB device, then starts automatically.

On the Android device, enable Developer options -> USB debugging and accept
the computer's RSA fingerprint prompt.

## Recommended commands

Smart exploration for one minute:

~~~
python3 adb_chaos.py --duration 60
~~~

Slow exploration with terminal image previews:

~~~
sudo apt install -y chafa
python3 adb_chaos.py --min-delay 1.0 --max-delay 3.0 --preview
~~~

Capture every 5 actions:

~~~
python3 adb_chaos.py --screenshot-every 5
~~~

Disable PNG screenshots:

~~~
python3 adb_chaos.py --screenshot-every 0
~~~

Stop saving UI XML while keeping screenshots:

~~~
python3 adb_chaos.py --no-dump-ui
~~~

Use a specific device:

~~~
adb devices
python3 adb_chaos.py --serial YOUR_SERIAL
~~~

Repeat a test run:

~~~
python3 adb_chaos.py --seed 1234 --duration 30
~~~

Tune backtracking:

~~~
python3 adb_chaos.py --smart-back-after 2 --max-consecutive-no-change 5
~~~

## What gets stored?

Each run creates a folder like:

~~~
sessions/
└── 2026-10-01_22-30-12_R58M123ABC/
    ├── 00001.png
    ├── 00002.png
    ├── 00003.png
    ├── events.jsonl
    ├── session.json
    ├── summary.json
    └── ui/
        ├── 00001.xml
        ├── 00002.xml
        └── 00003.xml
~~~

events.jsonl contains the action, package, UI fingerprints before/after,
selected target information, screenshot, UI dump, and brain statistics.

## Why the XML matters

Screenshots tell you what the phone looked like. The UI hierarchy often tells
you what controls actually exist: their text, descriptions, classes, bounds,
clickability, and whether a container is scrollable.

That turns a blind bot into an explorer:

~~~
UI contains: 14 clickable controls
        |
rank by usefulness + novelty
        |
tap a promising control
        |
screen fingerprint changes
        |
inspect the new UI
        |
scroll a detected list
        |
inspect again
        |
Back if stuck
~~~

No ML model or Python third-party package is required.

## Safety / storage notes

Use this only on an Android device you own or are authorized to control.

The program deliberately does not execute arbitrary shell commands on the
phone. Its fixed ADB operations are UI observation, screenshots, taps, swipes,
navigation keys, and volume keys.

The default cap is 300 PNG screenshots + 300 UI XML files per session.

Ctrl+C stops the current session.

## Requirements

- Linux / Raspberry Pi
- Python 3.10+
- ADB
- Android with USB debugging enabled
- No Python third-party packages required

## License

MIT
