#!/usr/bin/env python3
"""
Hiddify VPN — traffic watchdog + auto-failover.

Logic:
  1. Every 10 s read tun0 RX bytes.
  2. If no new bytes for DEAD_THRESHOLD seconds → VPN is dead.
  3. Try switching to next server in the selector group (up to MAX_SERVER_TRIES times),
     waiting RETRY_DELAY s between each.  If traffic resumes → stop.
  4. If all server tries exhausted → switch to next profile and restart VPN.
"""

import json
import os
import socket
import struct
import subprocess
import sys
import time

# ── Config ─────────────────────────────────────────────────────────────────────

DEAD_THRESHOLD   = 60      # seconds without new RX bytes → declare dead
RETRY_DELAY      = 15      # seconds between server switches
MAX_SERVER_TRIES = 5       # server switches before profile failover
CHECK_INTERVAL   = 10      # seconds between normal traffic checks
GRPC_PORT        = 17078

SUBSCRIPTIONS_FILE  = "/data/hiddify/subscriptions.json"
ACTIVE_PROFILE_FILE = "/data/hiddify/active_profile.json"
VPN_RESTART_FLAG    = "/data/hiddify/vpn_restart_requested"
STATE_FILE          = "/data/hiddify/state.json"


def log(msg):
    print(f"[monitor] {msg}", flush=True)


# ── tun0 traffic ───────────────────────────────────────────────────────────────

def tun_rx():
    try:
        return int(open("/sys/class/net/tun0/statistics/rx_bytes").read().strip())
    except Exception:
        return None


def tun_up():
    try:
        r = subprocess.run(["ip", "link", "show", "tun0"], capture_output=True, text=True)
        return r.returncode == 0 and "tun0" in r.stdout
    except Exception:
        return False


# ── gRPC helpers (raw HTTP/2, no external deps) ────────────────────────────────

def _h2f(t, f, sid, p=b''):
    return struct.pack('>I', len(p))[1:] + bytes([t, f]) + struct.pack('>I', sid) + p


def _hs(s):
    b = s.encode() if isinstance(s, str) else s
    return bytes([len(b)]) + b


def _pb_str(field, value):
    b = value.encode() if isinstance(value, str) else value
    tag = bytes([(field << 3) | 2])
    n = len(b)
    if n < 128:
        return tag + bytes([n]) + b
    return tag + bytes([(n & 0x7F) | 0x80, n >> 7]) + b


def grpc_call(method, body=b'', timeout=8):
    hpack = (
        bytes([0x83, 0x86])
        + bytes([0x44]) + _hs(method)
        + bytes([0x41]) + _hs(f'127.0.0.1:{GRPC_PORT}')
        + bytes([0x40]) + _hs('content-type') + _hs('application/grpc')
        + bytes([0x40]) + _hs('te') + _hs('trailers')
    )
    msg = b'\x00' + struct.pack('>I', len(body)) + body
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(('127.0.0.1', GRPC_PORT))
        s.sendall(
            b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
            + _h2f(0x04, 0x00, 0)
            + _h2f(0x01, 0x04, 1, hpack)
            + _h2f(0x00, 0x01, 1, msg)
        )
        resp = b''
        while True:
            try:
                chunk = s.recv(4096)
                if not chunk: break
                resp += chunk
            except socket.timeout:
                break
        s.close()
        return resp
    except Exception:
        return b''


def grpc_up():
    try:
        s = socket.socket(); s.settimeout(1); s.connect(('127.0.0.1', GRPC_PORT)); s.close(); return True
    except Exception:
        return False


def grpc_url_test():
    grpc_call('/hcore.Core/UrlTest', b'', timeout=10)


def grpc_select_outbound(group_tag, outbound_tag):
    body = _pb_str(1, group_tag) + _pb_str(2, outbound_tag)
    grpc_call('/hcore.Core/SelectOutbound', body)
    log(f"SelectOutbound: group={group_tag} → {outbound_tag}")


# ── Protobuf decoder (OutboundGroupList) ───────────────────────────────────────

def _varint(data, pos):
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80): break
        shift += 7
    return result, pos


def _len_delim(data, pos):
    length, pos = _varint(data, pos)
    return data[pos:pos+length], pos+length


def _decode_string_field(data):
    return data.decode('utf-8', errors='replace')


def _decode_outbound_info(data):
    info = {"tag": "", "type": "", "delay": 0}
    pos = 0
    while pos < len(data):
        tb = data[pos]; pos += 1
        field, wire = tb >> 3, tb & 7
        if wire == 2:
            sub, pos = _len_delim(data, pos)
            if field == 1: info["tag"] = _decode_string_field(sub)
            elif field == 2: info["type"] = _decode_string_field(sub)
        elif wire == 0:
            val, pos = _varint(data, pos)
            if field == 4: info["delay"] = val
        elif wire == 5: pos += 4
        elif wire == 1: pos += 8
        else: break
    return info


def _decode_group(data):
    g = {"tag": "", "type": "", "selected": "", "selectable": False, "items": []}
    pos = 0
    while pos < len(data):
        tb = data[pos]; pos += 1
        field, wire = tb >> 3, tb & 7
        if wire == 2:
            sub, pos = _len_delim(data, pos)
            if field == 1: g["tag"] = _decode_string_field(sub)
            elif field == 2: g["type"] = _decode_string_field(sub)
            elif field == 3: g["selected"] = _decode_string_field(sub)
            elif field == 6: g["items"].append(_decode_outbound_info(sub))
        elif wire == 0:
            val, pos = _varint(data, pos)
            if field == 4: g["selectable"] = bool(val)
        elif wire == 5: pos += 4
        elif wire == 1: pos += 8
        else: break
    return g


def get_selectable_groups():
    raw = grpc_call('/hcore.Core/MainOutboundsInfo', b'', timeout=6)
    if not raw:
        return []
    # extract gRPC DATA frame
    grpc_data = b''
    i = 0
    while i + 9 <= len(raw):
        fl  = struct.unpack('>I', b'\x00' + raw[i:i+3])[0]
        ft  = raw[i+3]
        pay = raw[i+9:i+9+fl]
        if ft == 0x00 and len(pay) >= 5:
            gl = struct.unpack('>I', pay[1:5])[0]
            grpc_data = pay[5:5+gl]
            break
        i += 9 + fl
    if not grpc_data:
        return []
    # decode OutboundGroupList
    groups = []
    pos = 0
    while pos < len(grpc_data):
        tb = grpc_data[pos]; pos += 1
        field, wire = tb >> 3, tb & 7
        if wire == 2:
            sub, pos = _len_delim(grpc_data, pos)
            if field == 1:
                g = _decode_group(sub)
                if g["selectable"] and g["items"]:
                    groups.append(g)
        elif wire == 0: _, pos = _varint(grpc_data, pos)
        elif wire == 5: pos += 4
        elif wire == 1: pos += 8
        else: break
    return groups


# ── Profile failover ────────────────────────────────────────────────────────────

def _read_json(path, default=None):
    try:
        return json.load(open(path))
    except Exception:
        return default if default is not None else {}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    json.dump(data, open(path, 'w'), indent=2, ensure_ascii=False)


def switch_to_next_profile():
    """Advance to next profile in subscriptions, write active_profile.json, signal restart."""
    subs = _read_json(SUBSCRIPTIONS_FILE, [])
    if not subs:
        log("No subscriptions found — cannot failover profile")
        return False

    active = _read_json(ACTIVE_PROFILE_FILE, {})
    cur_sub_id  = active.get("sub_id", "")
    cur_idx     = active.get("profile_index", 0)

    # Build flat list of all (sub_id, profile_index, name)
    all_profiles = []
    for sub in subs:
        for p in sub.get("profiles", []):
            all_profiles.append((sub["id"], p["index"], p["name"]))

    if not all_profiles:
        log("No profiles available — cannot failover")
        return False

    # Find current position
    cur_pos = next(
        (i for i, (sid, pidx, _) in enumerate(all_profiles)
         if sid == cur_sub_id and pidx == cur_idx),
        None
    )

    if cur_pos is None:
        next_pos = 0
    else:
        next_pos = (cur_pos + 1) % len(all_profiles)

    next_sub_id, next_idx, next_name = all_profiles[next_pos]

    log(f"Profile failover: [{cur_sub_id}]#{cur_idx} → [{next_sub_id}]#{next_idx} ({next_name})")

    _write_json(ACTIVE_PROFILE_FILE, {
        "sub_id":        next_sub_id,
        "profile_index": next_idx,
        "profile_name":  next_name,
    })

    # Signal run.sh to restart VPN with new profile
    open(VPN_RESTART_FLAG, "w").close()
    return True


# ── Server cycling ─────────────────────────────────────────────────────────────

def get_next_server(groups):
    """Return (group_tag, next_outbound_tag) rotating from current selected."""
    for g in groups:
        items = [it["tag"] for it in g["items"] if it.get("tag")]
        if not items:
            continue
        cur = g.get("selected", "")
        try:
            pos = items.index(cur)
            nxt = items[(pos + 1) % len(items)]
        except ValueError:
            nxt = items[0]
        return g["tag"], nxt
    return None, None


# ── Main loop ──────────────────────────────────────────────────────────────────

def main():
    log("Watchdog started")
    last_rx        = None
    last_rx_change = time.time()
    dead_detected  = False

    while True:
        time.sleep(CHECK_INTERVAL)

        if not tun_up():
            # VPN not running — reset counters, don't failover
            last_rx        = None
            last_rx_change = time.time()
            dead_detected  = False
            continue

        rx = tun_rx()

        if rx is None:
            continue

        if last_rx is None or rx != last_rx:
            # Traffic is flowing (or just connected)
            if dead_detected:
                log("Traffic resumed — watchdog reset")
                dead_detected = False
            last_rx        = rx
            last_rx_change = time.time()
            continue

        # RX bytes have not changed
        stale_sec = time.time() - last_rx_change

        if stale_sec < DEAD_THRESHOLD:
            continue

        # ── VPN appears dead ──────────────────────────────────────────────────
        if not dead_detected:
            log(f"No new RX bytes for {int(stale_sec)}s — starting failover")
            dead_detected = True

        if not grpc_up():
            log("gRPC not reachable — triggering profile restart")
            switch_to_next_profile()
            dead_detected  = False
            last_rx        = None
            last_rx_change = time.time()
            time.sleep(RETRY_DELAY)
            continue

        # Try cycling servers up to MAX_SERVER_TRIES times
        log("Running UrlTest before server cycle")
        grpc_url_test()
        time.sleep(3)

        switched = 0
        for attempt in range(1, MAX_SERVER_TRIES + 1):
            groups = get_selectable_groups()
            group_tag, next_tag = get_next_server(groups)

            if group_tag and next_tag:
                log(f"Server switch {attempt}/{MAX_SERVER_TRIES}: → {next_tag}")
                grpc_select_outbound(group_tag, next_tag)
            else:
                log(f"No selectable servers found (attempt {attempt})")

            # Wait and check traffic
            for _ in range(RETRY_DELAY):
                time.sleep(1)
                new_rx = tun_rx()
                if new_rx is not None and new_rx != last_rx:
                    log(f"Traffic resumed after server switch {attempt} — stopping failover")
                    last_rx        = new_rx
                    last_rx_change = time.time()
                    dead_detected  = False
                    switched = -1  # signal: success
                    break
            if switched == -1:
                break
            switched = attempt

        if switched == MAX_SERVER_TRIES:
            log(f"All {MAX_SERVER_TRIES} server switches failed — switching profile")
            switch_to_next_profile()
            dead_detected  = False
            last_rx        = None
            last_rx_change = time.time()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Watchdog stopped")
        sys.exit(0)
