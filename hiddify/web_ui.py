#!/usr/bin/env python3
"""
Hiddify VPN — ingress web dashboard.
Serves on :8080. Features:
  - Real-time uptime counter (HH:MM:SS), resets on VPN restart
  - RX / TX traffic from tun0 interface stats
  - VPN on/off toggle via supervisor API
  - Multi-subscription management (add/remove/refresh via UI)
  - Profile selector across all subscriptions
"""
import json
import os
import time
import uuid
import subprocess
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

STATE_FILE          = "/data/hiddify/state.json"
PROFILES_FILE       = "/data/hiddify/profiles.json"   # legacy single-sub
SUBSCRIPTIONS_FILE  = "/data/hiddify/subscriptions.json"
ACTIVE_PROFILE_FILE = "/data/hiddify/active_profile.json"
TUN_STATS           = "/sys/class/net/tun0/statistics"
OPTIONS_FILE        = "/data/options.json"
ICON_FILE           = "/icon.png"
VPN_STOP_FLAG       = "/data/hiddify/vpn_stop_requested"
VPN_START_FLAG      = "/data/hiddify/vpn_start_requested"
VPN_RESTART_FLAG    = "/data/hiddify/vpn_restart_requested"


# ── Helpers ────────────────────────────────────────────────────────────────────

def _read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _net_bytes():
    rx = tx = 0
    try:
        rx = int(open(f"{TUN_STATS}/rx_bytes").read())
        tx = int(open(f"{TUN_STATS}/tx_bytes").read())
    except Exception:
        pass
    return rx, tx


def _read_subscriptions():
    return _read_json(SUBSCRIPTIONS_FILE, [])


def _read_active_profile():
    return _read_json(ACTIVE_PROFILE_FILE, {})


def _fetch_profiles_for_url(url, timeout=30):
    """Call parse_sub.py --list and return [{index, name}, ...]."""
    r = subprocess.run(
        ["python3", "/parse_sub.py", "--url", url, "--list", "--out", "/tmp/probe_sub.json"],
        capture_output=True, text=True, timeout=timeout,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-400:] or "parse_sub.py failed")
    names = json.loads(r.stdout.strip())
    if not names:
        raise RuntimeError("No profiles found at this URL")
    return [{"index": i, "name": n} for i, n in enumerate(names)]


def _stats():
    state   = _read_json(STATE_FILE)
    rx, tx  = _net_bytes()

    started_at = state.get("started_at")
    uptime_sec = 0
    if started_at and state.get("status") == "connected":
        try:
            uptime_sec = max(0, int(time.time() - float(started_at)))
        except (ValueError, TypeError):
            uptime_sec = 0

    # ── Subscriptions & profiles ─────────────────────────────────────────────
    subs   = _read_subscriptions()
    active = _read_active_profile()

    all_profiles = []
    for sub in subs:
        custom_names = sub.get("custom_names", {})  # {str(index): custom_name}
        for p in sub.get("profiles", []):
            orig_name   = p["name"]
            display     = custom_names.get(str(p["index"]), orig_name)
            label       = display if len(subs) <= 1 else f"[{sub['name']}] {display}"
            is_active   = (
                sub["id"] == active.get("sub_id")
                and p["index"] == active.get("profile_index", -1)
            )
            all_profiles.append({
                "sub_id":    sub["id"],
                "index":     p["index"],
                "name":      label,
                "orig_name": orig_name,
                "active":    is_active,
            })

    # Fallback: legacy profiles.json (before multi-sub)
    if not all_profiles:
        options     = _read_json(OPTIONS_FILE)
        current_idx = int(options.get("selected_profile", 0))
        old         = _read_json(PROFILES_FILE, [])
        all_profiles = [
            {"sub_id": "", "index": p["index"], "name": p["name"],
             "active": p["index"] == current_idx}
            for p in old
        ]

    # active profile name for status card
    active_name = state.get("profile", "")
    for p in all_profiles:
        if p.get("active"):
            active_name = p["name"]
            break

    # subscriptions summary for UI
    subs_summary = [
        {
            "id":            s["id"],
            "name":          s["name"],
            "url_short":     s["url"][:50] + ("…" if len(s["url"]) > 50 else ""),
            "profile_count": len(s.get("profiles", [])),
            "profiles":      [
                {
                    "index":       p["index"],
                    "name":        p["name"],
                    "custom_name": s.get("custom_names", {}).get(str(p["index"]), ""),
                }
                for p in s.get("profiles", [])
            ],
        }
        for s in subs
    ]

    grpc_up = _grpc_port_open()

    return {
        "status":           state.get("status", "unknown"),
        "ip":               state.get("ip", ""),
        "profile":          active_name,
        "uptime_seconds":   uptime_sec,
        "rx_bytes":         rx,
        "tx_bytes":         tx,
        "profiles":         all_profiles,
        "subscriptions":    subs_summary,
        "grpc_up":          grpc_up,
    }


def _run_speedtest():
    import urllib.request, ssl as _ssl
    urls = [
        "https://speed.cloudflare.com/__down?bytes=10000000",
        "http://speed.cloudflare.com/__down?bytes=10000000",
        "https://ash-speed.hetzner.com/10MB.bin",
    ]
    ctx = _ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = _ssl.CERT_NONE
    last_err = ""
    for url in urls:
        try:
            req    = urllib.request.Request(url, headers={"User-Agent": "curl/8.0"})
            opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
            t0 = time.time()
            total = 0
            with opener.open(req, timeout=25) as r:
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    total += len(chunk)
            elapsed = time.time() - t0
            if total > 0 and elapsed > 0:
                return {"ok": True, "down_mbps": round(total * 8 / elapsed / 1_000_000, 1)}
        except Exception as e:
            last_err = str(e)
    return {"ok": False, "error": last_err or "all URLs failed"}


# ── gRPC helpers (raw HTTP/2, no deps) ────────────────────────────────────────

GRPC_PORT = 17078

def _grpc_call(method, body=b'', port=GRPC_PORT, timeout=8):
    import socket, struct
    def h2f(t, f, sid, p=b''):
        return struct.pack('>I', len(p))[1:] + bytes([t, f]) + struct.pack('>I', sid) + p
    def hs(s):
        b = s.encode() if isinstance(s, str) else s
        return bytes([len(b)]) + b

    hpack = (
        bytes([0x83, 0x86])
        + bytes([0x44]) + hs(method)
        + bytes([0x41]) + hs(f'127.0.0.1:{port}')
        + bytes([0x40]) + hs('content-type') + hs('application/grpc')
        + bytes([0x40]) + hs('te') + hs('trailers')
    )
    msg = b'\x00' + struct.pack('>I', len(body)) + body
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(('127.0.0.1', port))
        s.sendall(
            b'PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n'
            + h2f(0x04, 0x00, 0)
            + h2f(0x01, 0x04, 1, hpack)
            + h2f(0x00, 0x01, 1, msg)
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
        return True, resp
    except Exception as e:
        return False, b''

def _grpc_port_open(port=GRPC_PORT):
    import socket
    try:
        s = socket.socket(); s.settimeout(1); s.connect(('127.0.0.1', port)); s.close(); return True
    except Exception:
        return False

def _pb_string(field, value):
    """Encode protobuf string field."""
    import struct
    b = value.encode() if isinstance(value, str) else value
    tag = bytes([(field << 3) | 2])
    n = len(b)
    if n < 128:
        return tag + bytes([n]) + b
    return tag + bytes([(n & 0x7F) | 0x80, n >> 7]) + b

def _grpc_url_test(tag=''):
    """Call Core.UrlTest to ping outbounds. Empty tag = test all."""
    body = _pb_string(1, tag) if tag else b''
    ok, _ = _grpc_call('/hcore.Core/UrlTest', body)
    return ok

def _grpc_url_test_active():
    """Call Core.UrlTestActive — tests only the currently selected outbound."""
    ok, _ = _grpc_call('/hcore.Core/UrlTestActive', b'')
    return ok

def _grpc_select_outbound(group_tag, outbound_tag):
    """Call Core.SelectOutbound to switch to a specific server."""
    body = _pb_string(1, group_tag) + _pb_string(2, outbound_tag)
    ok, _ = _grpc_call('/hcore.Core/SelectOutbound', body)
    return ok

def _grpc_get_outbounds():
    """
    Call Core.MainOutboundsInfo — returns a stream of OutboundGroupList.
    We read one frame and decode the protobuf minimally.
    Returns list of {group_tag, group_type, selected, items:[{tag,type,delay}]}
    """
    import struct
    ok, raw = _grpc_call('/hcore.Core/MainOutboundsInfo', b'', timeout=6)
    if not ok or not raw:
        return []

    # Find DATA frame (type 0x00) in HTTP/2 response
    grpc_data = b''
    i = 0
    while i + 9 <= len(raw):
        frame_len  = struct.unpack('>I', b'\x00' + raw[i:i+3])[0]
        frame_type = raw[i+3]
        payload    = raw[i+9:i+9+frame_len]
        if frame_type == 0x00 and len(payload) >= 5:
            grpc_len = struct.unpack('>I', payload[1:5])[0]
            grpc_data = payload[5:5+grpc_len]
            break
        i += 9 + frame_len

    if not grpc_data:
        return []

    return _decode_outbound_group_list(grpc_data)

def _decode_varint(data, pos):
    result, shift = 0, 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80): break
        shift += 7
    return result, pos

def _decode_len_delimited(data, pos):
    length, pos = _decode_varint(data, pos)
    return data[pos:pos+length], pos+length

def _decode_string(data, pos):
    b, pos = _decode_len_delimited(data, pos)
    return b.decode('utf-8', errors='replace'), pos

def _decode_outbound_group_list(data):
    """Minimal decoder for OutboundGroupList protobuf."""
    groups = []
    pos = 0
    while pos < len(data):
        if pos >= len(data): break
        tag_byte = data[pos]; pos += 1
        field = tag_byte >> 3
        wire  = tag_byte & 0x07
        if wire == 2:  # length-delimited
            sub, pos = _decode_len_delimited(data, pos)
            if field == 1:  # OutboundGroup
                groups.append(_decode_outbound_group(sub))
        elif wire == 0:
            _, pos = _decode_varint(data, pos)
        elif wire == 5:
            pos += 4
        elif wire == 1:
            pos += 8
        else:
            break
    return groups

def _decode_outbound_group(data):
    group = {"tag": "", "type": "", "selected": "", "selectable": False, "items": []}
    pos = 0
    while pos < len(data):
        if pos >= len(data): break
        tag_byte = data[pos]; pos += 1
        field = tag_byte >> 3
        wire  = tag_byte & 0x07
        if wire == 2:
            sub, pos = _decode_len_delimited(data, pos)
            if field == 1:
                group["tag"] = sub.decode('utf-8', errors='replace')
            elif field == 2:
                group["type"] = sub.decode('utf-8', errors='replace')
            elif field == 3:
                group["selected"] = sub.decode('utf-8', errors='replace')
            elif field == 6:
                group["items"].append(_decode_outbound_info(sub))
        elif wire == 0:
            val, pos = _decode_varint(data, pos)
            if field == 4:
                group["selectable"] = bool(val)
        elif wire == 5:
            pos += 4
        elif wire == 1:
            pos += 8
        else:
            break
    return group

def _decode_outbound_info(data):
    info = {"tag": "", "type": "", "delay": 0}
    pos = 0
    while pos < len(data):
        if pos >= len(data): break
        tag_byte = data[pos]; pos += 1
        field = tag_byte >> 3
        wire  = tag_byte & 0x07
        if wire == 2:
            sub, pos = _decode_len_delimited(data, pos)
            if field == 1:
                info["tag"] = sub.decode('utf-8', errors='replace')
            elif field == 2:
                info["type"] = sub.decode('utf-8', errors='replace')
        elif wire == 0:
            val, pos = _decode_varint(data, pos)
            if field == 4:
                info["delay"] = val  # url_test_delay in ms
        elif wire == 5:
            pos += 4
        elif wire == 1:
            pos += 8
        else:
            break
    return info


def _vpn_stop():
    try:
        if os.path.exists(VPN_START_FLAG):
            os.remove(VPN_START_FLAG)
        open(VPN_STOP_FLAG, "w").close()
        return True
    except Exception:
        return False


def _vpn_start():
    try:
        if os.path.exists(VPN_STOP_FLAG):
            os.remove(VPN_STOP_FLAG)
        open(VPN_START_FLAG, "w").close()
        return True
    except Exception:
        return False


def _vpn_restart():
    """Signal run.sh to re-parse config and restart sing-box."""
    try:
        open(VPN_RESTART_FLAG, "w").close()
        return True
    except Exception:
        return False


def _write_profile(idx):
    opts = _read_json(OPTIONS_FILE)
    opts["selected_profile"] = int(idx)
    try:
        with open(OPTIONS_FILE, "w") as f:
            json.dump(opts, f)
        return True
    except Exception:
        return False


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Hiddify VPN</title>
<style>
  :root {
    --bg:      #111318;
    --card:    #1c1f26;
    --border:  #2a2d36;
    --text:    #e8eaf0;
    --muted:   #7b7f8e;
    --green:   #4caf50;
    --yellow:  #ffc107;
    --red:     #f44336;
    --blue:    #2196f3;
    --r:       14px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    min-height: 100vh; display: flex;
    align-items: flex-start; justify-content: center;
    padding: 28px 16px;
  }
  .wrap { width: 100%; max-width: 580px; display: flex; flex-direction: column; gap: 14px; }

  .header { display: flex; align-items: center; gap: 14px; }
  .logo svg { width: 42px; height: 42px; }
  .title-block h1 { font-size: 20px; font-weight: 700; }
  .title-block p  { font-size: 12px; color: var(--muted); margin-top: 2px; }

  .badge {
    display: inline-flex; align-items: center; gap: 7px;
    padding: 5px 13px; border-radius: 99px; font-size: 13px; font-weight: 600;
    background: var(--card); border: 1px solid var(--border); margin-left: auto;
  }
  .dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; box-shadow: 0 0 6px currentColor; }
  .connected    .dot { background: var(--green);  color: var(--green); }
  .connecting   .dot { background: var(--yellow); color: var(--yellow); animation: pulse 1s infinite; }
  .disconnected .dot, .error .dot, .unknown .dot { background: var(--red); color: var(--red); }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.4} }

  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: var(--r); padding: 18px 20px;
  }
  .card .label { font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: .8px; margin-bottom: 8px; }
  .card .value { font-size: 26px; font-weight: 700; font-variant-numeric: tabular-nums; }
  .card .sub   { font-size: 12px; color: var(--muted); margin-top: 4px; }

  .card.traffic { grid-column: 1/-1; display: flex; padding: 0; overflow: hidden; }
  .th { flex: 1; padding: 18px 20px; }
  .th:first-child { border-right: 1px solid var(--border); }
  .rx .value { color: var(--blue);  }
  .tx .value { color: var(--green); }

  .card.fw { grid-column: 1/-1; }
  .card.fw .value { font-size: 16px; word-break: break-all; }

  .controls { display: flex; gap: 10px; flex-wrap: wrap; }
  .btn {
    flex: 1; min-width: 120px; padding: 13px 20px; border-radius: 10px;
    border: none; font-size: 14px; font-weight: 600; cursor: pointer;
    transition: filter .15s, transform .1s;
  }
  .btn:active { transform: scale(.97); }
  .btn-on  { background: var(--green); color: #fff; }
  .btn-off { background: var(--red);   color: #fff; }
  .btn-on:hover  { filter: brightness(1.15); }
  .btn-off:hover { filter: brightness(1.15); }
  .btn:disabled { opacity: .45; cursor: default; }

  .profile-row { display: flex; align-items: center; gap: 10px; }
  select {
    flex: 1; background: var(--card); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 14px; font-size: 14px; cursor: pointer;
  }
  .btn-switch {
    padding: 10px 20px; border-radius: 8px; border: none;
    background: var(--blue); color: #fff; font-size: 14px; font-weight: 600;
    cursor: pointer;
  }
  .btn-switch:hover { filter: brightness(1.15); }
  .btn-switch:disabled { opacity: .4; cursor: default; }

  /* Subscriptions */
  .sub-list { display: flex; flex-direction: column; gap: 8px; }
  .sub-item {
    display: flex; align-items: center; gap: 10px;
    background: rgba(0,0,0,.2); border-radius: 8px; padding: 10px 12px;
  }
  .sub-item-info { flex: 1; min-width: 0; }
  .sub-item-name { font-size: 13px; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub-item-url  { font-size: 11px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub-item-count { font-size: 11px; color: var(--green); margin-top: 2px; }
  .btn-sm {
    padding: 6px 12px; border-radius: 6px; border: none;
    font-size: 12px; font-weight: 600; cursor: pointer; flex-shrink: 0;
  }
  .btn-refresh { background: rgba(33,150,243,.2); color: var(--blue); }
  .btn-remove  { background: rgba(244,67,54,.2);  color: var(--red); }
  .btn-sm:hover { filter: brightness(1.3); }
  .btn-sm:disabled { opacity: .4; cursor: default; }

  .add-row { display: flex; gap: 8px; flex-wrap: wrap; }
  input[type=text], input[type=url] {
    flex: 1; min-width: 0; background: var(--card); color: var(--text);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 14px; font-size: 13px;
  }
  input::placeholder { color: var(--muted); }

  .msg { font-size: 13px; color: var(--muted); text-align: center; min-height: 20px; }
  .footer { text-align: center; font-size: 11px; color: var(--muted); padding-top: 2px; }

  /* Profile rename inline */
  .profile-edit-row { display:flex; align-items:center; gap:6px; margin-top:4px; }
  .profile-edit-row input { flex:1; padding:5px 8px; font-size:12px; border-radius:6px; border:1px solid var(--border); background:var(--bg); color:var(--text); }
  .btn-save { padding:5px 10px; border-radius:6px; border:none; background:rgba(76,175,80,.25); color:var(--green); font-size:12px; font-weight:600; cursor:pointer; }
  .btn-save:hover { filter:brightness(1.3); }

  /* Server list */
  .server-item { display:flex; align-items:center; gap:8px; padding:8px 10px; border-radius:8px; background:rgba(0,0,0,.2); cursor:pointer; transition:background .15s; }
  .server-item:hover { background:rgba(255,255,255,.05); }
  .server-item.active { background:rgba(76,175,80,.12); border:1px solid rgba(76,175,80,.3); }
  .server-tag { flex:1; font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .server-delay { font-size:11px; font-weight:600; min-width:45px; text-align:right; }
  .delay-good { color:var(--green); }
  .delay-ok   { color:var(--yellow); }
  .delay-bad  { color:var(--red); }
  .delay-none { color:var(--muted); }
</style>
</head>
<body>
<div class="wrap">

  <!-- header -->
  <div class="header">
    <div class="logo">
      <svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg" style="width:42px;height:42px;fill:#4caf50">
        <path d="M2,21V15H4.5V17.5H5.5V12H8V21H5.5V19H4.5V21Z
                 M10,21V9H13V21Z
                 M15,21V9H17.5V21Z
                 M16,4H20V7.5H16Z"/>
      </svg>
    </div>
    <div class="title-block">
      <h1>Hiddify VPN</h1>
      <p>powered by sing-box</p>
    </div>
    <div id="badge" class="badge unknown">
      <span class="dot"></span>
      <span id="status-text">—</span>
    </div>
  </div>

  <!-- stats grid -->
  <div class="grid">
    <div class="card">
      <div class="label">Uptime</div>
      <div class="value" id="uptime">—</div>
      <div class="sub">since last connect</div>
    </div>
    <div class="card">
      <div class="label">External IP</div>
      <div class="value" id="ip" style="font-size:17px">—</div>
      <div class="sub">VPN exit node</div>
    </div>
    <div class="card traffic">
      <div class="th rx">
        <div class="label">↓ Download</div>
        <div class="value" id="rx">—</div>
        <div class="sub">received via tun0</div>
      </div>
      <div class="th tx">
        <div class="label">↑ Upload</div>
        <div class="value" id="tx">—</div>
        <div class="sub">sent via tun0</div>
      </div>
    </div>
    <div class="card fw">
      <div class="label">Active Profile</div>
      <div class="value" id="profile">—</div>
    </div>
  </div>

  <!-- speed test -->
  <div class="card" style="display:flex;flex-direction:column;gap:10px">
    <div class="label" style="margin-bottom:0">Speed Test (via VPN)</div>
    <div style="display:flex;align-items:center;gap:14px;flex-wrap:wrap">
      <button class="btn btn-on" id="btn-speed" onclick="runSpeed()" style="flex:0 0 auto;min-width:140px">⚡ Run Test</button>
      <div style="display:flex;gap:24px;flex:1">
        <div>
          <div class="label">↓ Download</div>
          <div class="value" id="speed-down" style="font-size:22px">—</div>
        </div>
      </div>
    </div>
    <div class="msg" id="speed-msg"></div>
  </div>

  <!-- VPN controls + profile selector -->
  <div class="card" style="display:flex;flex-direction:column;gap:12px">
    <div class="label" style="margin-bottom:0">VPN Control</div>
    <div class="controls">
      <button class="btn btn-on"  id="btn-start" onclick="vpnAction('start')">▶ Start VPN</button>
      <button class="btn btn-off" id="btn-stop"  onclick="vpnAction('stop')">■ Stop VPN</button>
    </div>

    <div class="label" style="margin-top:4px;margin-bottom:0">Profile</div>
    <div class="profile-row">
      <select id="profile-sel"></select>
      <button class="btn-switch" id="btn-switch" onclick="switchProfile()">Apply</button>
    </div>
    <div class="msg" id="msg"></div>
  </div>

  <!-- Subscriptions management -->
  <div class="card" style="display:flex;flex-direction:column;gap:12px">
    <div class="label" style="margin-bottom:0">Subscriptions</div>

    <div class="sub-list" id="sub-list">
      <div style="font-size:13px;color:var(--muted)">Loading…</div>
    </div>

    <div class="label" style="margin-top:4px;margin-bottom:0">Add subscription</div>
    <div class="add-row">
      <input type="url" id="add-url" placeholder="https://… or vless://… or vmess://…" />
    </div>
    <div class="add-row">
      <input type="text" id="add-name" placeholder="Name (optional)" style="flex:0 0 160px;min-width:120px"/>
      <button class="btn-switch" id="btn-add" onclick="addSubscription()" style="flex:0 0 auto">+ Add</button>
    </div>
    <div class="msg" id="sub-msg"></div>
  </div>

  <!-- Server selector (shown when VPN connected and gRPC available) -->
  <div class="card" id="server-card" style="display:none;flex-direction:column;gap:12px">
    <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
      <div class="label" style="margin-bottom:0;flex:1">Servers</div>
      <button class="btn-sm btn-refresh" id="btn-urltest" onclick="runUrlTest()" style="padding:7px 14px;font-size:13px">
        ⚡ Test All
      </button>
    </div>
    <div id="server-list" style="display:flex;flex-direction:column;gap:6px">
      <div style="font-size:12px;color:var(--muted)">Click "Test All" to load servers</div>
    </div>
    <div class="msg" id="server-msg"></div>
  </div>

  <div class="footer">Updates every 2 s · Hiddify VPN 2.2.0</div>
</div>

<script>
let _uptimeSec = 0;
let _tick = null;
let _profiles = [];

function fmt(b) {
  if (!b) return "0 B";
  if (b < 1024)       return b + " B";
  if (b < 1048576)    return (b/1024).toFixed(1) + " KB";
  if (b < 1073741824) return (b/1048576).toFixed(2) + " MB";
  return (b/1073741824).toFixed(2) + " GB";
}
function fmtT(s) {
  const h = String(Math.floor(s/3600)).padStart(2,"0");
  const m = String(Math.floor((s%3600)/60)).padStart(2,"0");
  const sc = String(s%60).padStart(2,"0");
  return `${h}:${m}:${sc}`;
}
function setMsg(id, t, ok=true) {
  const el = document.getElementById(id);
  el.textContent = t;
  el.style.color = ok ? "var(--muted)" : "var(--red)";
  if (t) setTimeout(()=>{ if(el.textContent===t) el.textContent=""; }, 5000);
}

function updateProfiles(profiles) {
  _profiles = profiles || [];
  const sel = document.getElementById("profile-sel");
  if (!_profiles.length) {
    sel.innerHTML = '<option value="">— add a subscription —</option>';
    document.getElementById("btn-switch").disabled = true;
    return;
  }
  sel.innerHTML = _profiles.map((p, i) =>
    `<option value="${i}" ${p.active ? "selected" : ""}>${p.name}</option>`
  ).join("");
  document.getElementById("btn-switch").disabled = false;
}

function updateSubscriptions(subs) {
  const el = document.getElementById("sub-list");
  if (!subs || !subs.length) {
    el.innerHTML = '<div style="font-size:13px;color:var(--muted)">No subscriptions yet. Add one below.</div>';
    return;
  }
  el.innerHTML = subs.map(s => `
    <div class="sub-item" style="flex-direction:column;align-items:stretch;gap:8px">
      <div style="display:flex;align-items:center;gap:10px">
        <div class="sub-item-info">
          <div class="sub-item-name">${escHtml(s.name)}</div>
          <div class="sub-item-url">${escHtml(s.url_short)}</div>
          <div class="sub-item-count">${s.profile_count} profile${s.profile_count !== 1 ? "s" : ""}</div>
        </div>
        <button class="btn-sm btn-refresh" onclick="refreshSub('${s.id}')" title="Re-fetch profiles">↻</button>
        <button class="btn-sm btn-remove"  onclick="removeSub('${s.id}')"  title="Remove">✕</button>
      </div>
      ${s.profiles && s.profiles.length ? `
      <div style="padding-left:4px;display:flex;flex-direction:column;gap:4px">
        ${s.profiles.map(p => `
        <div>
          <div style="font-size:12px;color:var(--muted)">${escHtml(p.name)}</div>
          <div class="profile-edit-row">
            <input type="text" id="rename-${s.id}-${p.index}"
              value="${escHtml(p.custom_name)}"
              placeholder="Custom name (optional)"/>
            <button class="btn-save" onclick="renameProfile('${s.id}',${p.index})">Save</button>
          </div>
        </div>`).join("")}
      </div>` : ""}
    </div>
  `).join("");
}

function escHtml(s) {
  return s.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}

async function poll() {
  try {
    const r = await fetch("stats");
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();

    const badge = document.getElementById("badge");
    badge.className = "badge " + (d.status || "unknown");
    document.getElementById("status-text").textContent = d.status || "unknown";
    document.getElementById("ip").textContent      = d.ip      || "—";
    document.getElementById("profile").textContent = d.profile || "—";
    document.getElementById("rx").textContent = fmt(d.rx_bytes);
    document.getElementById("tx").textContent = fmt(d.tx_bytes);

    _uptimeSec = d.uptime_seconds || 0;
    document.getElementById("uptime").textContent = fmtT(_uptimeSec);
    if (d.status === "connected") {
      if (!_tick) _tick = setInterval(()=>{ _uptimeSec++; document.getElementById("uptime").textContent=fmtT(_uptimeSec); }, 1000);
    } else {
      if (_tick) { clearInterval(_tick); _tick = null; }
      document.getElementById("uptime").textContent = "—";
    }

    const running = (d.status === "connected" || d.status === "connecting");
    document.getElementById("btn-start").disabled = running;
    document.getElementById("btn-stop").disabled  = !running;

    updateProfiles(d.profiles);
    updateSubscriptions(d.subscriptions);

    // Show server panel only when VPN is running and gRPC available
    const serverCard = document.getElementById("server-card");
    if (serverCard) serverCard.style.display = d.grpc_up ? "flex" : "none";
  } catch(e) {
    document.getElementById("status-text").textContent = "unreachable";
  }
}

async function vpnAction(action) {
  document.getElementById("btn-start").disabled = true;
  document.getElementById("btn-stop").disabled  = true;
  setMsg("msg", action === "start" ? "Starting VPN…" : "Stopping VPN…");
  try {
    const r = await fetch(`vpn/${action}`, {method:"POST"});
    const d = await r.json();
    setMsg("msg", d.ok ? (action==="start"?"VPN starting…":"VPN stopped.") : ("Error: "+(d.error||"unknown")), d.ok);
  } catch(e) {
    setMsg("msg", "Request failed", false);
  }
  setTimeout(poll, 2000);
}

async function switchProfile() {
  const idx = parseInt(document.getElementById("profile-sel").value);
  if (isNaN(idx) || !_profiles[idx]) return;
  const p = _profiles[idx];
  document.getElementById("btn-switch").disabled = true;
  setMsg("msg", "Switching profile…");
  try {
    const params = p.sub_id
      ? `sub_id=${encodeURIComponent(p.sub_id)}&index=${p.index}`
      : `index=${p.index}`;
    const r = await fetch(`profile/set?${params}`, {method:"POST"});
    const d = await r.json();
    setMsg("msg", d.ok ? "Profile saved. VPN restarting…" : ("Error: "+(d.error||"")), d.ok);
  } catch(e) {
    setMsg("msg", "Request failed", false);
  }
  setTimeout(poll, 3000);
}

async function addSubscription() {
  const url  = document.getElementById("add-url").value.trim();
  const name = document.getElementById("add-name").value.trim();
  if (!url) { setMsg("sub-msg", "Enter a URL", false); return; }
  const btn = document.getElementById("btn-add");
  btn.disabled = true;
  setMsg("sub-msg", "Fetching profiles… (may take ~15 s)");
  try {
    const r = await fetch("subscription/add", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({url, name: name || url.slice(0,40)}),
    });
    const d = await r.json();
    if (d.ok) {
      setMsg("sub-msg", `Added: ${d.profiles} profile${d.profiles!==1?"s":""}${d.existing?" (updated)":""}`);
      document.getElementById("add-url").value  = "";
      document.getElementById("add-name").value = "";
      poll();
    } else {
      setMsg("sub-msg", "Error: " + (d.error || "unknown"), false);
    }
  } catch(e) {
    setMsg("sub-msg", "Request failed", false);
  }
  btn.disabled = false;
}

async function refreshSub(id) {
  setMsg("sub-msg", "Refreshing…");
  try {
    const r = await fetch(`subscription/refresh?id=${id}`, {method:"POST"});
    const d = await r.json();
    setMsg("sub-msg", d.ok ? `Refreshed: ${d.profiles} profiles` : ("Error: "+(d.error||"")), d.ok);
    if (d.ok) poll();
  } catch(e) {
    setMsg("sub-msg", "Request failed", false);
  }
}

async function removeSub(id) {
  if (!confirm("Remove this subscription?")) return;
  try {
    const r = await fetch(`subscription/remove?id=${id}`, {method:"POST"});
    const d = await r.json();
    if (d.ok) poll();
    else setMsg("sub-msg", "Error: " + (d.error||""), false);
  } catch(e) {
    setMsg("sub-msg", "Request failed", false);
  }
}

async function renameProfile(subId, idx) {
  const inp = document.getElementById(`rename-${subId}-${idx}`);
  if (!inp) return;
  const name = inp.value.trim();
  try {
    const r = await fetch("profile/rename", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({sub_id: subId, index: idx, name}),
    });
    const d = await r.json();
    setMsg("sub-msg", d.ok ? "Name saved" : ("Error: " + (d.error||"")), d.ok);
    if (d.ok) poll();
  } catch(e) {
    setMsg("sub-msg", "Request failed", false);
  }
}

let _serverGroups = [];

async function runUrlTest() {
  const btn = document.getElementById("btn-urltest");
  btn.disabled = true;
  btn.textContent = "Testing…";
  setMsg("server-msg", "Pinging servers…");
  try {
    await fetch("server/urltest", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
    // Wait a moment for results to populate
    await new Promise(r => setTimeout(r, 2500));
    await loadServers();
    setMsg("server-msg", "Done");
    setTimeout(() => { if(document.getElementById("server-msg").textContent==="Done") document.getElementById("server-msg").textContent=""; }, 3000);
  } catch(e) {
    setMsg("server-msg", "Request failed", false);
  }
  btn.disabled = false;
  btn.textContent = "⚡ Test All";
}

async function loadServers() {
  try {
    const r = await fetch("server/list", {method:"POST", headers:{"Content-Type":"application/json"}, body:"{}"});
    if (!r.ok) return;
    const d = await r.json();
    if (!d.ok || !d.groups) return;
    _serverGroups = d.groups.filter(g => g.selectable && g.items && g.items.length);
    renderServers();
  } catch(e) {}
}

function renderServers() {
  const el = document.getElementById("server-list");
  if (!_serverGroups.length) {
    el.innerHTML = '<div style="font-size:12px;color:var(--muted)">No selectable groups found. Click "Test All" first.</div>';
    return;
  }
  el.innerHTML = _serverGroups.map(g => `
    <div>
      <div style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px">${escHtml(g.tag)}</div>
      ${g.items.map(item => {
        const delay = item.delay || 0;
        const cls   = !delay ? "delay-none" : delay < 300 ? "delay-good" : delay < 800 ? "delay-ok" : "delay-bad";
        const lbl   = !delay ? "—" : delay + " ms";
        const active = item.tag === g.selected ? " active" : "";
        return `<div class="server-item${active}" onclick="selectServer('${escHtml(g.tag)}','${escHtml(item.tag)}')">
          <div class="server-tag">${escHtml(item.tag)}</div>
          <div class="server-delay ${cls}">${lbl}</div>
        </div>`;
      }).join("")}
    </div>
  `).join("");
}

async function selectServer(groupTag, outboundTag) {
  setMsg("server-msg", `Switching to ${outboundTag}…`);
  try {
    const r = await fetch("server/select", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({group_tag: groupTag, outbound_tag: outboundTag}),
    });
    const d = await r.json();
    if (d.ok) {
      setMsg("server-msg", `Active: ${outboundTag}`);
      setTimeout(loadServers, 500);
    } else {
      setMsg("server-msg", "Error: " + (d.error||""), false);
    }
  } catch(e) {
    setMsg("server-msg", "Request failed", false);
  }
}

async function runSpeed() {
  const btn = document.getElementById("btn-speed");
  const msg = document.getElementById("speed-msg");
  const val = document.getElementById("speed-down");
  btn.disabled = true;
  val.textContent = "…";
  msg.textContent = "Running 10 MB download test via VPN…";
  msg.style.color = "var(--muted)";
  try {
    const r = await fetch("speedtest");
    const d = await r.json();
    if (d.ok) {
      val.textContent = d.down_mbps + " Mbit/s";
      msg.textContent = "Test complete";
    } else {
      val.textContent = "—";
      msg.textContent = "Error: " + (d.error || "unknown");
      msg.style.color = "var(--red)";
    }
  } catch(e) {
    val.textContent = "—";
    msg.textContent = "Request failed";
    msg.style.color = "var(--red)";
  }
  btn.disabled = false;
  setTimeout(() => { if(msg.textContent==="Test complete") msg.textContent=""; }, 4000);
}

poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""


# ── Request handler ────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, body):
        b = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(b))
        self.end_headers()
        self.wfile.write(b)

    def _read_body_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length))

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"

        if path in ("/", "/index.html"):
            self._html(HTML)

        elif path.endswith("/stats"):
            self._json(200, _stats())

        elif path.endswith("/speedtest"):
            import threading
            result = {}
            def _run():
                result.update(_run_speedtest())
            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=35)
            self._json(200, result if result else {"ok": False, "error": "timeout"})

        elif path.endswith("/icon.png"):
            try:
                with open(ICON_FILE, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", len(body))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            except Exception:
                self.send_response(404)
                self.end_headers()

        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        qs   = parse_qs(urlparse(self.path).query)

        # ── VPN control ────────────────────────────────────────────────────────
        if path.endswith("/vpn/start"):
            self._json(200, {"ok": _vpn_start()})

        elif path.endswith("/vpn/stop"):
            self._json(200, {"ok": _vpn_stop()})

        # ── Profile switch ─────────────────────────────────────────────────────
        elif path.endswith("/profile/set"):
            sub_id = qs.get("sub_id", [""])[0]
            try:
                idx = int(qs.get("index", ["0"])[0])
            except ValueError:
                self._json(400, {"ok": False, "error": "bad index"})
                return

            if sub_id:
                # Multi-subscription flow
                subs = _read_subscriptions()
                sub  = next((s for s in subs if s["id"] == sub_id), None)
                if not sub:
                    self._json(404, {"ok": False, "error": "subscription not found"})
                    return
                profiles = sub.get("profiles", [])
                pname    = profiles[idx]["name"] if idx < len(profiles) else str(idx)
                _write_json(ACTIVE_PROFILE_FILE, {
                    "sub_id": sub_id, "profile_index": idx, "profile_name": pname,
                })
                # Sync to options.json so HA config panel reflects current selection
                opts = _read_json(OPTIONS_FILE)
                opts["subscription_url"]  = sub["url"]
                opts["selected_profile"]  = idx
                try:
                    with open(OPTIONS_FILE, "w") as f:
                        json.dump(opts, f, indent=2)
                except Exception:
                    pass
                _vpn_restart()
                self._json(200, {"ok": True})
            else:
                # Legacy single-subscription flow
                ok = _write_profile(idx)
                if ok:
                    _vpn_restart()
                self._json(200, {"ok": ok})

        # ── Subscription management ────────────────────────────────────────────
        elif path.endswith("/subscription/add"):
            try:
                body  = self._read_body_json()
                url   = body.get("url", "").strip()
                name  = body.get("name", "").strip() or url[:40]
                if not url:
                    self._json(400, {"ok": False, "error": "url required"})
                    return

                profiles = _fetch_profiles_for_url(url)
                subs     = _read_subscriptions()

                # Update if URL already exists
                for s in subs:
                    if s["url"] == url:
                        s["profiles"] = profiles
                        s["name"]     = name or s["name"]
                        _write_json(SUBSCRIPTIONS_FILE, subs)
                        self._json(200, {"ok": True, "profiles": len(profiles), "existing": True})
                        return

                sub_id = str(uuid.uuid4())[:8]
                subs.append({"id": sub_id, "url": url, "name": name, "profiles": profiles})
                _write_json(SUBSCRIPTIONS_FILE, subs)

                # Auto-select first profile if this is the first subscription
                if len(subs) == 1 and profiles:
                    _write_json(ACTIVE_PROFILE_FILE, {
                        "sub_id": sub_id, "profile_index": 0,
                        "profile_name": profiles[0]["name"],
                    })

                self._json(200, {"ok": True, "id": sub_id, "profiles": len(profiles), "existing": False})

            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        elif path.endswith("/subscription/remove"):
            sub_id = qs.get("id", [""])[0]
            subs   = _read_subscriptions()
            subs   = [s for s in subs if s["id"] != sub_id]
            _write_json(SUBSCRIPTIONS_FILE, subs)
            # Clear active profile if it was from this subscription
            active = _read_active_profile()
            if active.get("sub_id") == sub_id:
                if subs and subs[0].get("profiles"):
                    first = subs[0]
                    _write_json(ACTIVE_PROFILE_FILE, {
                        "sub_id": first["id"],
                        "profile_index": 0,
                        "profile_name": first["profiles"][0]["name"],
                    })
                else:
                    try:
                        os.remove(ACTIVE_PROFILE_FILE)
                    except Exception:
                        pass
            self._json(200, {"ok": True})

        elif path.endswith("/subscription/refresh"):
            sub_id = qs.get("id", [""])[0]
            subs   = _read_subscriptions()
            for s in subs:
                if s["id"] == sub_id:
                    try:
                        s["profiles"] = _fetch_profiles_for_url(s["url"])
                        _write_json(SUBSCRIPTIONS_FILE, subs)
                        self._json(200, {"ok": True, "profiles": len(s["profiles"])})
                    except Exception as e:
                        self._json(500, {"ok": False, "error": str(e)})
                    return
            self._json(404, {"ok": False, "error": "subscription not found"})

        # ── Custom profile name ────────────────────────────────────────────────
        elif path.endswith("/profile/rename"):
            try:
                body    = self._read_body_json()
                sub_id  = body.get("sub_id", "")
                idx     = int(body.get("index", 0))
                new_name = body.get("name", "").strip()
                subs = _read_subscriptions()
                for s in subs:
                    if s["id"] == sub_id:
                        names = s.setdefault("custom_names", {})
                        if new_name:
                            names[str(idx)] = new_name
                        else:
                            names.pop(str(idx), None)  # reset to original
                        _write_json(SUBSCRIPTIONS_FILE, subs)
                        self._json(200, {"ok": True})
                        return
                self._json(404, {"ok": False, "error": "subscription not found"})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        # ── Server change (URL test + select best) ─────────────────────────────
        elif path.endswith("/server/urltest"):
            if not _grpc_port_open():
                self._json(503, {"ok": False, "error": "VPN not running (gRPC unavailable)"})
                return
            tag = self._read_body_json().get("tag", "")
            ok  = _grpc_url_test(tag)
            self._json(200, {"ok": ok})

        elif path.endswith("/server/list"):
            if not _grpc_port_open():
                self._json(503, {"ok": False, "error": "VPN not running"})
                return
            groups = _grpc_get_outbounds()
            self._json(200, {"ok": True, "groups": groups})

        elif path.endswith("/server/select"):
            try:
                body        = self._read_body_json()
                group_tag   = body.get("group_tag", "select")
                outbound_tag = body.get("outbound_tag", "")
                if not outbound_tag:
                    self._json(400, {"ok": False, "error": "outbound_tag required"})
                    return
                if not _grpc_port_open():
                    self._json(503, {"ok": False, "error": "VPN not running"})
                    return
                ok = _grpc_select_outbound(group_tag, outbound_tag)
                self._json(200, {"ok": ok})
            except Exception as e:
                self._json(500, {"ok": False, "error": str(e)})

        else:
            self.send_response(404)
            self.end_headers()


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("WEB_PORT", 8080))
    print(f"[web_ui] Listening on :{port}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
