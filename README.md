
# ADB Chaos

Terminal-first Android UI explorer for Linux and Raspberry Pi.

## v0.3 - Adaptive Intelligence

The smart engine now learns instead of repeatedly making the same guesses.

### State understanding

- Reads Android's UI hierarchy with uiautomator.
- Detects clickable, enabled and visible controls.
- Detects scrollable containers.
- Reads button text, content descriptions, resource IDs and widget classes.
- Builds normalized state fingerprints.
- Ignores common volatile values such as clocks and large counters when building fingerprints.
- Buckets tiny coordinate changes, reducing fake states caused by animations/layout shifts.

### Learning

Every state/control pair gets a small history:

- how many times it was tried;
- how often it changed the UI;
- how often it revealed a previously unseen state;
- how often it produced no change.

The scorer uses that history to prefer unexplored controls and controls that
have historically produced useful new states.

### Exploration graph

~~~
STATE A
  |
  +-- target 1 --> STATE A      (no progress)
  |
  +-- target 2 --> STATE B      (new)
  |
  +-- target 3 --> STATE C      (new)
  |
  +-- scroll up --> STATE D     (new)
~~~

This graph is stored in chaos_memory.json by default, so a later run can start
with knowledge collected during earlier runs.

### Backtracking

When the same normalized state keeps appearing without progress, smart mode
uses Back to escape the loop. This prevents the classic endless:

~~~
tap
  -> same screen
tap
  -> same screen
tap
  -> same screen
...
~~~

### Dialog handling

Likely dialogs are detected from their UI structure/text. Safe controls such
as Close, OK, Done, Continue, Next and Skip receive priority.

The existing safety filter still refuses controls whose labels strongly suggest
destructive, financial, authentication, communication, installation,
permission or account operations.

## Screenshots

Screenshots remain a core part of the project.

By default the program can:
- capture every N actions;
- capture immediately when the normalized UI state changes;
- save the matching UI hierarchy XML;
- record before/after state fingerprints in events.jsonl.

A session looks like:

~~~
sessions/
└── 2026-10-01_22-30-12_DEVICE/
    ├── 00001.png
    ├── 00002.png
    ├── events.jsonl
    ├── session.json
    ├── summary.json
    └── ui/
        ├── 00001.xml
        ├── 00002.xml
        └── 00003.xml
~~~

This means you can inspect not only what the phone looked like, but also what
the explorer knew about the interface when it made each decision.

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

The program waits for an authorized ADB device and starts automatically.

On Android, enable Developer options -> USB debugging and accept the
computer's RSA fingerprint prompt.

For terminal screenshot previews:

~~~
sudo apt install -y chafa
python3 adb_chaos.py --preview
~~~

## Useful commands

Two-minute intelligent run:

~~~
python3 adb_chaos.py --duration 120
~~~

Slower run for easier observation:

~~~
python3 adb_chaos.py --min-delay 1.2 --max-delay 3.0 --preview
~~~

Capture every 5 actions plus any state change:

~~~
python3 adb_chaos.py --screenshot-every 5 --capture-on-change
~~~

Disable learning between runs:

~~~
python3 adb_chaos.py --no-persistent-memory
~~~

Start the learning database from zero:

~~~
rm -f chaos_memory.json
~~~

Use a custom learning file:

~~~
python3 adb_chaos.py --memory-file my_test_memory.json
~~~

Inspect the original dumb/random behaviour:

~~~
python3 adb_chaos.py --mode random
~~~

Specific device:

~~~
adb devices
python3 adb_chaos.py --serial YOUR_SERIAL
~~~

Repeatable test:

~~~
python3 adb_chaos.py --seed 1234 --duration 30
~~~

Tune backtracking:

~~~
python3 adb_chaos.py --smart-back-after 2 --max-consecutive-no-change 5
~~~

## CLI overview

~~~
--mode smart|random
--duration SECONDS
--min-delay SECONDS
--max-delay SECONDS
--screenshot-every N
--max-screenshots N
--capture-on-change / --no-capture-on-change
--persistent-memory / --no-persistent-memory
--memory-file PATH
--dump-ui / --no-dump-ui
--preview
--smart-back-after N
--max-consecutive-no-change N
--seed N
--serial SERIAL
~~~

## Requirements

- Linux / Raspberry Pi
- Python 3.10+
- ADB
- Android with USB debugging enabled
- No Python third-party packages required

## Safety / storage

Use this only on an Android device you own or are authorized to control.

The tool uses a fixed set of ADB operations for UI observation and interaction.
It does not turn UI text into arbitrary shell commands.

The default session limit is 300 PNG/XML files. This is intentionally capped
so an unattended run does not quietly consume the Raspberry Pi's storage.

chaos_memory.json is a local learning database and is not part of a session.

Ctrl+C stops the run and writes summary.json.

## License

MIT
