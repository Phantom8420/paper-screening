#!/bin/sh
# Double-click (macOS) or run (Linux) to open the screening app in your browser. Leave the window open while you work.
cd "$(dirname "$0")" || exit 1
if ! docker info >/dev/null 2>&1; then
  echo "Docker Desktop is not running. Start it, wait until it says it is running, then open this file again."
  read -r _; exit 1
fi
if ! docker image inspect paper-screening >/dev/null 2>&1; then
  echo "Setting up for the first time. This downloads about 2 GB and takes 10 to 20 minutes, once."
  docker build -t paper-screening . || { echo "Setup failed. Check the internet connection and try again."; read -r _; exit 1; }
fi
echo "Opening http://127.0.0.1:8000 in your browser. Close this window to stop the app."
(sleep 6; open http://127.0.0.1:8000 2>/dev/null || xdg-open http://127.0.0.1:8000) &
docker run --rm -p 127.0.0.1:8000:8000 -v paper-screening-data:/data paper-screening serve --host 0.0.0.0 --by "$USER"
