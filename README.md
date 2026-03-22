# Hiddify VPN — Home Assistant Add-on Repository

Custom add-on repository for Home Assistant.

## Installation

1. Go to **Settings → Add-ons → Add-on Store**
2. Click **⋮ (three dots)** → **Repositories**
3. Add: `https://github.com/denmrnngp-cloud/hiddify-ha-addon`
4. Find **Hiddify VPN** in the store and install

## Add-ons

### Hiddify VPN

Routes traffic through a VPN powered by [sing-box](https://github.com/SagerNet/sing-box).

- Paste your subscription URL or direct proxy link (vless://, vmess://, trojan://...)
- Supports TUN mode (all traffic) or proxy-only mode (SOCKS5/HTTP)
- Creates HA sensor entities: status, external IP, active profile
- Protocols: VLESS, VLESS+Reality, VMess, Trojan, Shadowsocks, Hysteria2, TUIC
