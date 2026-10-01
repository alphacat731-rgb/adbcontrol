#!/usr/bin/env bash
set -euo pipefail

echo "[adbcontrol] Installing Debian dependencies..."
sudo apt update
sudo apt install -y adb python3

echo
echo "[adbcontrol] Done."
echo "Run: python3 adb_chaos.py"
