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
        for p in sub.get("profiles", []):
            label = p["name"] if len(subs) <= 1 else f"[{sub['name']}] {p['name']}"
            is_active = (
                sub["id"] == active.get("sub_id")
                and p["index"] == active.get("profile_index", -1)
            )
            all_profiles.append({
                "sub_id": sub["id"],
                "index":  p["index"],
                "name":   label,
                "active": is_active,
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
        }
        for s in subs
    ]

    return {
        "status":           state.get("status", "unknown"),
        "ip":               state.get("ip", ""),
        "profile":          active_name,
        "uptime_seconds":   uptime_sec,
        "rx_bytes":         rx,
        "tx_bytes":         tx,
        "profiles":         all_profiles,
        "subscriptions":    subs_summary,
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

  <div class="footer">Updates every 2 s · Hiddify VPN 2.1.0</div>
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
    <div class="sub-item">
      <div class="sub-item-info">
        <div class="sub-item-name">${escHtml(s.name)}</div>
        <div class="sub-item-url">${escHtml(s.url_short)}</div>
        <div class="sub-item-count">${s.profile_count} profile${s.profile_count !== 1 ? "s" : ""}</div>
      </div>
      <button class="btn-sm btn-refresh" onclick="refreshSub('${s.id}')" title="Re-fetch profiles">↻</button>
      <button class="btn-sm btn-remove"  onclick="removeSub('${s.id}')"  title="Remove">✕</button>
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

        else:
            self.send_response(404)
            self.end_headers()


# ── Entry ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("WEB_PORT", 8080))
    print(f"[web_ui] Listening on :{port}", flush=True)
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
