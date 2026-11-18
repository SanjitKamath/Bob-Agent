#!/bin/bash
# Starts, restarts, stops or checks bobd. The Bob app's "Start bobd" button runs this.
#
#   deploy/bobd.sh start | restart | stop | status
#
# If you installed deploy/launchd/com.bob.bobd.plist as a LaunchAgent, this uses launchd.
# Otherwise it runs bobd in the background from this repo, logging to data/bobd.log.
# It also starts Ollama if nothing answers on its port. The last line printed is a
# one-line summary the app shows.
set -u

ACTION=${1:-start}
BOB_HOME=$(cd "$(dirname "$0")/.." && pwd)
BOB_URL=${BOB_URL:-http://127.0.0.1:8765}
OLLAMA_URL=${OLLAMA_URL:-http://127.0.0.1:11434}
LABEL=com.bob.bobd
DOMAIN="gui/$(id -u)"
LOG="$BOB_HOME/data/bobd.log"
PIDFILE="$BOB_HOME/data/bobd.pid"
# Apps opened from Finder get a minimal PATH; add the usual places for uv, git and ollama.
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

up() { curl -sf -m 2 "$BOB_URL/health" >/dev/null 2>&1; }
ollama_up() { curl -sf -m 2 "$OLLAMA_URL/api/version" >/dev/null 2>&1; }
in_launchd() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }
in_system_launchd() { launchctl print "system/$LABEL" >/dev/null 2>&1; }
our_pid() {
    [ -f "$PIDFILE" ] || return 1
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null)
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null && echo "$pid"
}

wait_up() {
    for _ in $(seq 1 "${1:-45}"); do
        up && return 0
        sleep 1
    done
    return 1
}

ensure_ollama() {
    ollama_up && return 0
    if launchctl print "$DOMAIN/com.bob.ollama" >/dev/null 2>&1; then
        launchctl kickstart "$DOMAIN/com.bob.ollama" >/dev/null 2>&1
    elif [ -d /Applications/Ollama.app ]; then
        open -g -a Ollama
    elif command -v ollama >/dev/null 2>&1; then
        nohup ollama serve >>"$BOB_HOME/data/ollama.log" 2>&1 &
    else
        echo "Ollama is not installed or not running; replies will fail until it is."
        return 1
    fi
    for _ in $(seq 1 20); do ollama_up && return 0; sleep 1; done
    echo "Ollama did not answer within 20 seconds."
    return 1
}

start_direct() {
    mkdir -p "$BOB_HOME/data"
    cd "$BOB_HOME" || exit 1
    if [ -x "$BOB_HOME/.venv/bin/bob" ]; then
        nohup "$BOB_HOME/.venv/bin/bob" serve >>"$LOG" 2>&1 &
    elif command -v uv >/dev/null 2>&1; then
        nohup uv run bob serve >>"$LOG" 2>&1 &
    else
        echo "Cannot find Bob's environment. In $BOB_HOME run: uv sync"
        exit 1
    fi
    echo $! >"$PIDFILE"
}

stop_bobd() {
    if in_launchd; then
        # KeepAlive would bring it straight back; booting it out stops it until next login.
        launchctl bootout "$DOMAIN/$LABEL" >/dev/null 2>&1
        echo "Stopped bobd (launchd). It starts again at login, or with: deploy/bobd.sh start"
        return 0
    fi
    local pid
    if pid=$(our_pid); then
        kill "$pid" 2>/dev/null
        for _ in $(seq 1 15); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
        kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
        rm -f "$PIDFILE"
        echo "Stopped bobd."
        return 0
    fi
    if up; then
        echo "bobd is running, but not from this script or launchd. Stop it where you started it (Ctrl+C in its terminal)."
        return 1
    fi
    echo "bobd is not running."
}

start_bobd() {
    ensure_ollama
    if up; then
        echo "bobd is already running."
        return 0
    fi
    if in_system_launchd; then
        echo "bobd is a system service (the separate 'bob' user). Start it with: sudo launchctl kickstart system/$LABEL"
        return 1
    fi
    local plist="$HOME/Library/LaunchAgents/$LABEL.plist"
    if in_launchd; then
        launchctl kickstart "$DOMAIN/$LABEL" >/dev/null 2>&1
    elif [ -f "$plist" ]; then
        launchctl bootstrap "$DOMAIN" "$plist" >/dev/null 2>&1
    else
        start_direct
    fi
    if wait_up 45; then
        echo "bobd started."
        return 0
    fi
    echo "bobd did not answer within 45 seconds. See $([ -f "$plist" ] && echo /tmp/bobd.log || echo "$LOG")."
    return 1
}

case "$ACTION" in
    start) start_bobd ;;
    stop) stop_bobd ;;
    restart)
        if in_launchd && up; then
            launchctl kickstart -k "$DOMAIN/$LABEL" >/dev/null 2>&1
            sleep 2
            wait_up 45 && echo "bobd restarted." || { echo "bobd did not come back within 45 seconds. See /tmp/bobd.log."; exit 1; }
        else
            stop_bobd >/dev/null
            sleep 1
            start_bobd | sed 's/^bobd started\.$/bobd restarted./'
            exit "${PIPESTATUS[0]}"
        fi
        ;;
    status)
        if up; then echo "bobd is running."; else echo "bobd is not running."; exit 1; fi
        ;;
    *)
        echo "usage: deploy/bobd.sh start|restart|stop|status" >&2
        exit 2
        ;;
esac
