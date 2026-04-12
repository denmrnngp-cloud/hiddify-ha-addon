#!/usr/bin/env bash
set -euo pipefail

CONFIG_JSON="/data/options.json"
HIDDIFY_CONFIG="/data/hiddify/config.json"
HIDDIFY_BIN="/usr/local/bin/sing-box"
STATE_FILE="/data/hiddify/state.json"
PROFILES_FILE="/data/hiddify/profiles.json"
SUBSCRIPTIONS_FILE="/data/hiddify/subscriptions.json"
ACTIVE_PROFILE_FILE="/data/hiddify/active_profile.json"
LOG_FILE="/data/hiddify/hiddify.log"
HA_URL="http://supervisor/core/api"
HA_TOKEN="${SUPERVISOR_TOKEN:-}"

mkdir -p /data/hiddify

# ── Read add-on options ────────────────────────────────────────────────────────

TUN_MODE=$(jq -r '.tun_mode // true' "$CONFIG_JSON")
LOG_LEVEL=$(jq -r '.log_level // "info"' "$CONFIG_JSON")
PROXY_DOMAINS=$(jq -r '.proxy_domains // ""' "$CONFIG_JSON")

# ── Import subscription_urls from HA config into subscriptions.json ────────────
# Each URL in subscription_urls not yet tracked gets auto-imported with profile fetch.
python3 << 'PYEOF'
import json, os, subprocess

CONFIG_JSON        = "/data/options.json"
SUBSCRIPTIONS_FILE = "/data/hiddify/subscriptions.json"

def load(p, d):
    try: return json.load(open(p))
    except Exception: return d

opts = load(CONFIG_JSON, {})
subs = load(SUBSCRIPTIONS_FILE, [])

# Support both subscription_urls (list) and legacy subscription_url (str)
urls_from_config = opts.get("subscription_urls") or []
if not urls_from_config:
    single = opts.get("subscription_url", "")
    if single: urls_from_config = [single]

known_urls = {s["url"] for s in subs}
changed = False
for url in urls_from_config:
    url = url.strip()
    if not url or url in known_urls: continue
    print(f"[hiddify] Auto-importing: {url[:60]}...", flush=True)
    try:
        r = subprocess.run(
            ["python3", "/parse_sub.py", "--url", url, "--list", "--out", "/tmp/probe_import.json"],
            capture_output=True, text=True, timeout=30,
        )
        names    = json.loads(r.stdout.strip()) if r.returncode == 0 else []
        profiles = [{"index": i, "name": n} for i, n in enumerate(names)]
    except Exception as e:
        print(f"[hiddify] Fetch failed: {e}", flush=True)
        profiles = []
    import uuid as _u
    name = url.split("#")[-1] if "#" in url else url[:40]
    subs.append({"id": str(_u.uuid4())[:8], "url": url, "name": name, "profiles": profiles})
    known_urls.add(url)
    changed = True
    print(f"[hiddify] Imported '{name}' ({len(profiles)} profiles)", flush=True)

if changed:
    os.makedirs(os.path.dirname(SUBSCRIPTIONS_FILE), exist_ok=True)
    json.dump(subs, open(SUBSCRIPTIONS_FILE, "w"), indent=2, ensure_ascii=False)
    print(f"[hiddify] subscriptions.json: {len(subs)} total", flush=True)
PYEOF

# active_profile.json (written by UI) is the single source of truth.
# If it exists → use it. Otherwise fall back to first subscription / options.json.
if [ -f "$ACTIVE_PROFILE_FILE" ] && [ -f "$SUBSCRIPTIONS_FILE" ]; then
    _ACTIVE_SUB_ID=$(jq -r '.sub_id // ""' "$ACTIVE_PROFILE_FILE")
    _ACTIVE_IDX=$(jq -r '.profile_index // 0' "$ACTIVE_PROFILE_FILE")
    _ACTIVE_URL=$(jq -r --arg id "$_ACTIVE_SUB_ID" '.[] | select(.id == $id) | .url' "$SUBSCRIPTIONS_FILE" 2>/dev/null || echo "")
    if [ -n "$_ACTIVE_URL" ]; then
        SUB_URL="$_ACTIVE_URL"
        PROFILE_IDX="$_ACTIVE_IDX"
        echo "[hiddify] Active profile: sub=$_ACTIVE_SUB_ID idx=$PROFILE_IDX url=${SUB_URL:0:50}..."
    else
        # Active sub was removed — fall back to first available
        SUB_URL=$(jq -r '.[0].url // ""' "$SUBSCRIPTIONS_FILE" 2>/dev/null || echo "")
        PROFILE_IDX=0
        echo "[hiddify] Active sub not found, falling back to first subscription"
    fi
elif [ -f "$SUBSCRIPTIONS_FILE" ]; then
    # No active profile yet — use first subscription, first profile
    SUB_URL=$(jq -r '.[0].url // ""' "$SUBSCRIPTIONS_FILE" 2>/dev/null || echo "")
    PROFILE_IDX=0
    echo "[hiddify] No active profile, using first subscription"
else
    # Legacy: no subscriptions.json → use options.json directly
    SUB_URL=$(jq -r '.subscription_url // (.subscription_urls[0] // "")' "$CONFIG_JSON")
    PROFILE_IDX=$(jq -r '.selected_profile // 0' "$CONFIG_JSON")
    echo "[hiddify] Legacy mode: url=${SUB_URL:0:50}... idx=$PROFILE_IDX"
fi

echo "[hiddify] Starting Hiddify VPN add-on"
echo "[hiddify] Subscription: ${SUB_URL:0:60}..."
echo "[hiddify] Profile index: $PROFILE_IDX"
echo "[hiddify] TUN mode: $TUN_MODE"

# ── Validate ───────────────────────────────────────────────────────────────────

if [ -z "$SUB_URL" ]; then
    echo "[hiddify] ERROR: subscription_url is empty. Set it in add-on configuration."
    sleep 30
    exit 1
fi

# ── HA state helper ────────────────────────────────────────────────────────────

ha_state() {
    local status="$1"
    local ip="$2"
    local profile="$3"

    # started_at: set on first connect, cleared on disconnect
    local started_at=""
    if [ "$status" = "connected" ]; then
        # preserve existing started_at if already connected, else set now
        local prev_started
        prev_started=$(_STATE_FILE="$STATE_FILE" python3 -c "
import json,os
try:
    d=json.load(open(os.environ['_STATE_FILE']))
    print(d.get('started_at','') if d.get('status')=='connected' else '')
except: print('')
" 2>/dev/null || true)
        if [ -n "$prev_started" ]; then
            started_at="$prev_started"
        else
            started_at=$(date +%s)
        fi
    fi

    if [ -n "$HA_TOKEN" ]; then
        curl -s -X POST "$HA_URL/states/sensor.hiddify_status" \
            -H "Authorization: Bearer $HA_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{\"state\":\"$status\",\"attributes\":{\"friendly_name\":\"Hiddify VPN Status\",\"icon\":\"mdi:vpn\"}}" \
            >/dev/null 2>&1 || true

        curl -s -X POST "$HA_URL/states/sensor.hiddify_ip" \
            -H "Authorization: Bearer $HA_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{\"state\":\"$ip\",\"attributes\":{\"friendly_name\":\"Hiddify VPN IP\",\"icon\":\"mdi:ip-network\"}}" \
            >/dev/null 2>&1 || true

        curl -s -X POST "$HA_URL/states/sensor.hiddify_profile" \
            -H "Authorization: Bearer $HA_TOKEN" \
            -H "Content-Type: application/json" \
            -d "{\"state\":\"$profile\",\"attributes\":{\"friendly_name\":\"Hiddify Active Profile\",\"icon\":\"mdi:server-network\"}}" \
            >/dev/null 2>&1 || true
    fi

    # Save local state (read by web_ui.py)
    # Pass dynamic values as env vars to avoid shell-quoting issues
    _STATUS="$status" _IP="$ip" _PROFILE="$profile" \
    _STARTED="$started_at" _UPDATED="$(date -Iseconds)" \
    _STATE_FILE="$STATE_FILE" \
    python3 -c "
import json, os
d = {
    'status':     os.environ['_STATUS'],
    'ip':         os.environ['_IP'],
    'profile':    os.environ['_PROFILE'],
    'started_at': os.environ['_STARTED'],
    'updated':    os.environ['_UPDATED'],
}
with open(os.environ['_STATE_FILE'], 'w') as f:
    json.dump(d, f)
" 2>/dev/null || true
}

# ── TUN setup ──────────────────────────────────────────────────────────────────

setup_tun() {
    modprobe tun 2>/dev/null || true
    if [ ! -c /dev/net/tun ]; then
        mkdir -p /dev/net
        mknod /dev/net/tun c 10 200 2>/dev/null || true
        chmod 0666 /dev/net/tun
        echo "[hiddify] Created /dev/net/tun"
    fi
}

# ── Parse subscription ─────────────────────────────────────────────────────────

parse_config() {
    echo "[hiddify] Parsing subscription..." >&2
    ha_state "connecting" "" "Fetching config..."

    TUN_FLAG="--tun"
    [ "$TUN_MODE" = "false" ] && TUN_FLAG="--no-tun"

    # Save full profile list for web UI (--list mode)
    python3 /parse_sub.py --url "$SUB_URL" --list \
        --out "$HIDDIFY_CONFIG" 2>/dev/null \
        | python3 -c "
import json,sys
names=json.load(sys.stdin)
out=[{'index':i,'name':n} for i,n in enumerate(names)]
print(json.dumps(out))
" > "$PROFILES_FILE" 2>/dev/null || echo "[]" > "$PROFILES_FILE"

    PROFILE_NAME=$(python3 /parse_sub.py \
        --url "$SUB_URL" \
        --index "$PROFILE_IDX" \
        $TUN_FLAG \
        --log "$LOG_LEVEL" \
        --proxy-domains "$PROXY_DOMAINS" \
        --out "$HIDDIFY_CONFIG" 2>&1 | tail -1) || {
        echo "[hiddify] ERROR: Failed to parse subscription" >&2
        ha_state "error" "" "Failed to parse subscription"
        return 1
    }

    echo "[hiddify] Profile: $PROFILE_NAME" >&2
    # Only output the profile name to stdout (captured by callers via $())
    echo "$PROFILE_NAME"
}

# ── Trigger HA speedtest integration ──────────────────────────────────────────

trigger_speedtest() {
    if [ -n "${HA_TOKEN:-}" ]; then
        curl -s -X POST \
          -H "Authorization: Bearer $HA_TOKEN" \
          -H "Content-Type: application/json" \
          "$HA_URL/services/speedtestdotnet/speedtest" \
          -d '{}' >/dev/null 2>&1 || true
        echo "[hiddify] Triggered HA speedtest integration"
    fi
}

# ── Get external IP ────────────────────────────────────────────────────────────

get_ip() {
    local ip
    ip=$(curl -s --max-time 5 https://api.ipify.org 2>/dev/null || \
         curl -s --max-time 5 https://ifconfig.me 2>/dev/null || echo "")
    echo "$ip"
}

# ── Monitor loop ───────────────────────────────────────────────────────────────

monitor_loop() {
    local profile="$1"
    local prev_status=""

    while true; do
        sleep 10

        # Check if sing-box is still running
        if ! kill -0 "$HIDDIFY_PID" 2>/dev/null; then
            echo "[hiddify] Process died, restarting..."
            ha_state "disconnected" "" "$profile"
            return 1
        fi

        # Check TUN interface
        if [ "$TUN_MODE" = "true" ]; then
            if ip link show tun0 >/dev/null 2>&1; then
                if [ "$prev_status" != "connected" ]; then
                    VPN_IP=$(get_ip)
                    echo "[hiddify] Connected. IP: $VPN_IP  Profile: $profile"
                    ha_state "connected" "$VPN_IP" "$profile"
                    prev_status="connected"
                    trigger_speedtest &
                fi
            else
                if [ "$prev_status" != "connecting" ]; then
                    echo "[hiddify] TUN not up yet..."
                    ha_state "connecting" "" "$profile"
                    prev_status="connecting"
                fi
            fi
        fi
    done
}

# ── Cleanup ────────────────────────────────────────────────────────────────────

cleanup() {
    echo "[hiddify] Stopping..."
    ha_state "disconnected" "" ""
    [ -n "${WATCHDOG_PID:-}" ] && kill "$WATCHDOG_PID" 2>/dev/null || true
    [ -n "${HIDDIFY_PID:-}" ]  && kill "$HIDDIFY_PID"  2>/dev/null || true
    wait "${HIDDIFY_PID:-}" 2>/dev/null || true
    [ -n "${WEB_PID:-}" ]      && kill "$WEB_PID"       2>/dev/null || true
    ip link delete tun0 2>/dev/null || true
    exit 0
}

trap cleanup SIGTERM SIGINT SIGQUIT

# ── Main ───────────────────────────────────────────────────────────────────────

[ "$TUN_MODE" = "true" ] && setup_tun

ha_state "connecting" "" "Starting..."

PROFILE_NAME=$(parse_config) || {
    sleep 60
    exit 1
}

start_singbox() {
    echo "[hiddify] Starting sing-box..."
    echo "[hiddify] Binary version: $("$HIDDIFY_BIN" version 2>&1 | head -1)"
    echo "[hiddify] Config: $HIDDIFY_CONFIG"

    export ENABLE_DEPRECATED_LEGACY_DNS_SERVERS=true
    rm -f /data/hiddify/vpn_stop_requested /data/hiddify/vpn_start_requested

    "$HIDDIFY_BIN" run \
        -c "$HIDDIFY_CONFIG" \
        2>&1 | while IFS= read -r line; do echo "[core] $line"; done &
    HIDDIFY_PID=$!
    echo "$HIDDIFY_PID" > /data/hiddify/singbox.pid

    echo "[hiddify] PID: $HIDDIFY_PID"
    echo "[hiddify] /dev/net/tun: $(ls -la /dev/net/tun 2>&1)"
    echo "[hiddify] Interfaces after Core.Start: $(ip -br link show 2>/dev/null | tr '\n' ' ')"

    sleep 8
    echo "[hiddify] Interfaces after wait: $(ip -br link show 2>/dev/null | tr '\n' ' ')"

    if [ "$TUN_MODE" = "true" ]; then
        if ip link show tun0 >/dev/null 2>&1; then
            VPN_IP=$(get_ip)
            echo "[hiddify] VPN up. External IP: $VPN_IP"
            ha_state "connected" "$VPN_IP" "$PROFILE_NAME"
            trigger_speedtest &
        else
            echo "[hiddify] Waiting for TUN interface..."
            ha_state "connecting" "" "$PROFILE_NAME"
        fi
    else
        ha_state "connected" "" "$PROFILE_NAME"
    fi
}

start_singbox

# ── Register custom icon in HA Lovelace ───────────────────────────────────────

mkdir -p /config/www
cp /hiddify-icons.js /config/www/hiddify-icons.js

# Add /local/hiddify-icons.js to lovelace_resources storage if not already there
python3 << 'PYEOF'
import json, os, time

storage = "/config/.storage/lovelace_resources"
url     = "/local/hiddify-icons.js"

try:
    with open(storage) as f:
        data = json.load(f)
except Exception:
    data = {"version": 1, "minor_version": 1,
            "key": "lovelace_resources", "data": {"items": []}}

items = data.setdefault("data", {}).setdefault("items", [])
if not any(i.get("url") == url for i in items):
    items.append({"id": str(int(time.time()*1000)), "res_type": "module", "url": url})
    with open(storage, "w") as f:
        json.dump(data, f, indent=2)
    print(f"[hiddify] Lovelace resource registered: {url}")
else:
    print(f"[hiddify] Lovelace resource already present: {url}")
PYEOF

# ── Start web dashboard ────────────────────────────────────────────────────────

echo "[hiddify] Starting web dashboard on :8080"
WEB_PORT=8080 python3 /web_ui.py 2>&1 | while IFS= read -r line; do echo "[web] $line"; done &
WEB_PID=$!

# Start connection monitor (reconnect / server cycle / profile failover)
python3 /vpn_monitor.py 2>&1 | while IFS= read -r line; do echo "[watchdog] $line"; done &
WATCHDOG_PID=$!

# Start monitor in background
monitor_loop "$PROFILE_NAME" &
MONITOR_PID=$!

# ── Control loop — handles stop/start requests from web UI ────────────────────
while true; do
    # Poll every 2s for stop request or sing-box exit
    sleep 2

    # Profile switch / restart requested by web UI
    if [ -f /data/hiddify/vpn_restart_requested ]; then
        echo "[hiddify] Restart requested (profile switch)"
        rm -f /data/hiddify/vpn_restart_requested
        kill "$WATCHDOG_PID" 2>/dev/null || true
        kill "$MONITOR_PID"  2>/dev/null || true
        kill "$HIDDIFY_PID"  2>/dev/null || true
        wait "$HIDDIFY_PID"  2>/dev/null || true
        ip link delete tun0 2>/dev/null || true
        ha_state "connecting" "" "Reloading profile…"

        # Re-read active subscription (may have changed via UI)
        if [ -f "$ACTIVE_PROFILE_FILE" ] && [ -f "$SUBSCRIPTIONS_FILE" ]; then
            _SUB_ID=$(jq -r '.sub_id // ""' "$ACTIVE_PROFILE_FILE")
            _IDX=$(jq -r '.profile_index // 0' "$ACTIVE_PROFILE_FILE")
            _URL=$(jq -r --arg id "$_SUB_ID" '.[] | select(.id == $id) | .url' "$SUBSCRIPTIONS_FILE" 2>/dev/null || echo "")
            [ -n "$_URL" ] && SUB_URL="$_URL" && PROFILE_IDX="$_IDX"
        fi

        PROFILE_NAME=$(parse_config) || { sleep 60; exit 1; }
        start_singbox
        python3 /vpn_monitor.py 2>&1 | while IFS= read -r line; do echo "[watchdog] $line"; done &
        WATCHDOG_PID=$!
        monitor_loop "$PROFILE_NAME" &
        MONITOR_PID=$!
        continue
    fi

    # Stop requested by web UI
    if [ -f /data/hiddify/vpn_stop_requested ]; then
        echo "[hiddify] Stop requested by web UI"
        kill "$MONITOR_PID" 2>/dev/null || true
        kill "$HIDDIFY_PID" 2>/dev/null || true
        wait "$HIDDIFY_PID" 2>/dev/null || true
        ip link delete tun0 2>/dev/null || true
        ha_state "disconnected" "" ""
        rm -f /data/hiddify/vpn_stop_requested

        # Wait for start request (web UI is still alive)
        echo "[hiddify] VPN stopped. Waiting for start request..."
        while true; do
            sleep 2
            if [ -f /data/hiddify/vpn_start_requested ]; then
                echo "[hiddify] Start requested by web UI"
                rm -f /data/hiddify/vpn_start_requested
                start_singbox
                monitor_loop "$PROFILE_NAME" &
                MONITOR_PID=$!
                break
            fi
        done
        continue
    fi

    # sing-box exited on its own (crash or SIGTERM from HA)
    if ! kill -0 "$HIDDIFY_PID" 2>/dev/null; then
        wait "$HIDDIFY_PID" 2>/dev/null
        EXIT_CODE=$?
        kill "$MONITOR_PID" 2>/dev/null || true
        kill "$WEB_PID"     2>/dev/null || true
        echo "[hiddify] sing-box exited with code $EXIT_CODE"
        ha_state "disconnected" "" ""
        exit "$EXIT_CODE"
    fi
done
