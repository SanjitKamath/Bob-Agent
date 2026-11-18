#!/bin/bash
# Starts, restarts, stops or checks Bob's background service (bobd) and the Ollama that Bob
# manages. Both run as login items (launchd agents) that Bob's setup installed.
#
#   deploy/bobd.sh start | restart | stop | status
#
# The last line printed is a one-line summary the app shows.
set -u

ACTION=${1:-start}
BOB_URL=${BOB_URL:-http://127.0.0.1:8765}
OLLAMA_URL=${BOB_OLLAMA_URL:-http://127.0.0.1:11435}
DOMAIN="gui/$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"
BOBD=com.bob.bobd
OLLAMA=com.bob.ollama

up() { curl -sf -m 2 "$BOB_URL/health" >/dev/null 2>&1; }
ollama_up() { curl -sf -m 2 "$OLLAMA_URL/api/version" >/dev/null 2>&1; }
loaded() { launchctl print "$DOMAIN/$1" >/dev/null 2>&1; }

wait_for() { # command, seconds
    for _ in $(seq 1 "$2"); do "$1" && return 0; sleep 1; done
    return 1
}

# Start one agent: load it from its plist if it is not loaded, then start it.
load() {
    local plist="$AGENTS/$1.plist"
    [ -f "$plist" ] || return 1
    # The agents do not run on load (RunAtLoad is off): loading one is not starting it.
    loaded "$1" || launchctl bootstrap "$DOMAIN" "$plist" >/dev/null 2>&1
    launchctl kickstart "$DOMAIN/$1" >/dev/null 2>&1
}

start_all() {
    if [ ! -f "$AGENTS/$BOBD.plist" ]; then
        echo "Bob is not set up on this Mac yet. Open Bob and choose Run setup again."
        return 1
    fi
    ollama_up || { load "$OLLAMA" && wait_for ollama_up 30; } \
        || echo "The model runner did not answer within 30 seconds; replies will fail until it does."
    if up; then
        echo "bobd is already running."
        return 0
    fi
    load "$BOBD"
    if wait_for up 45; then
        echo "bobd started."
        return 0
    fi
    echo "bobd did not answer within 45 seconds. See data/bobd.log in Bob's folder."
    return 1
}

case "$ACTION" in
    start) start_all ;;
    restart)
        if loaded "$BOBD"; then
            launchctl kickstart -k "$DOMAIN/$BOBD" >/dev/null 2>&1
            sleep 2
            if wait_for up 45; then echo "bobd restarted."; else
                echo "bobd did not come back within 45 seconds. See data/bobd.log in Bob's folder."
                exit 1
            fi
        else
            start_all | sed 's/^bobd started\.$/bobd restarted./'
            exit "${PIPESTATUS[0]}"
        fi
        ;;
    stop)
        # Booting an agent out keeps it stopped until the next login or the next start.
        loaded "$BOBD" && launchctl bootout "$DOMAIN/$BOBD" >/dev/null 2>&1
        loaded "$OLLAMA" && launchctl bootout "$DOMAIN/$OLLAMA" >/dev/null 2>&1
        echo "Stopped bobd and its model runner. They start again at login, or with: deploy/bobd.sh start"
        ;;
    status)
        if up; then echo "bobd is running."; else echo "bobd is not running."; exit 1; fi
        ;;
    *)
        echo "usage: deploy/bobd.sh start|restart|stop|status" >&2
        exit 2
        ;;
esac
