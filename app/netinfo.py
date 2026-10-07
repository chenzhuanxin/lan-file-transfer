# -*- coding: utf-8 -*-
"""
netinfo.py —— 网络与 IP 探测模块

职责：
  1. 枚举本机所有可用网卡与 IPv4 地址（区分物理网卡 / 虚拟网卡）
  2. 获取公网出口 IP（多源自动降级）
  3. 局域网设备扫描（ARP 邻居表 + UDP 广播探测）
  4. 推荐最佳局域网 IP（给接收端扫码连接用）
"""
from __future__ import annotations

import ipaddress
import socket
import subprocess
import sys
import threading
import time
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any


# ---------------------------------------------------------------- 网卡枚举

# 常见虚拟网卡关键词，用于降权（不绝对排除，用户可能真的在虚拟网络里互传）
VIRTUAL_HINTS = (
    "vmware", "virtualbox", "vbox", "hyper-v", "vethernet",
    "loopback", "bluetooth", "tap", "tun", "wsl", "docker",
    "radmin", "vpn", "zerotier", "tailscale", "clash", "virtual",
)


def _psutil_net_if_addrs() -> dict[str, list[dict[str, Any]]]:
    """用 psutil 枚举网卡（优先方案，信息最全）。"""
    try:
        import psutil
    except ImportError:
        return {}

    result: dict[str, list[dict[str, Any]]] = {}
    stats = psutil.net_if_stats()
    for name, addrs in psutil.net_if_addrs().items():
        entries = []
        for a in addrs:
            if a.family != socket.AF_INET:
                continue
            if not a.address:
                continue
            try:
                ipaddress.IPv4Address(a.address)
            except ValueError:
                continue
            is_up = stats[name].isup if name in stats else True
            speed = stats[name].speed if name in stats else 0
            entries.append({
                "ip": a.address,
                "netmask": a.netmask or "",
                "up": is_up,
                "speed_mbps": speed if name in stats else 0,
            })
        if entries:
            result[name] = entries
    return result


def _socket_fallback() -> dict[str, list[dict[str, Any]]]:
    """无 psutil 时的兜底方案。"""
    result: dict[str, list[dict[str, Any]]] = {}
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            try:
                ipaddress.IPv4Address(ip)
            except ValueError:
                continue
            result.setdefault("默认网卡", []).append({
                "ip": ip, "netmask": "", "up": True, "speed_mbps": 0,
            })
    except Exception:
        pass
    return result


def _is_virtual(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in VIRTUAL_HINTS)


def list_interfaces() -> list[dict[str, Any]]:
    """
    返回本机所有 IPv4 网卡列表，按「可信度」排序。

    每项结构：
      {name, ip, netmask, up, speed_mbps, virtual, score, broadcast, network}
    """
    raw = _psutil_net_if_addrs() or _socket_fallback()
    items: list[dict[str, Any]] = []

    for name, entries in raw.items():
        for e in entries:
            ip = e["ip"]
            if ip.startswith("127."):
                continue  # 回环地址，排除

            virtual = _is_virtual(name)
            score = 0
            if e.get("up"):
                score += 30
            if not virtual:
                score += 50
            if ip.startswith("192.168."):
                score += 25
            elif ip.startswith("10."):
                score += 20
            elif ip.startswith("172."):
                # 172.16.0.0/12 才是私有段
                try:
                    if ipaddress.IPv4Address(ip) in ipaddress.ip_network("172.16.0.0/12"):
                        score += 20
                except ValueError:
                    pass
            elif ip.startswith("169.254."):
                score -= 60  # APIPA 自动私有地址，基本不可用
            if e.get("speed_mbps", 0) >= 1000:
                score += 10
            elif e.get("speed_mbps", 0) >= 100:
                score += 5

            netmask = e.get("netmask") or "255.255.255.0"
            broadcast = ""
            network = ""
            try:
                iface = ipaddress.IPv4Interface(f"{ip}/{netmask}")
                broadcast = str(iface.network.broadcast_address)
                network = str(iface.network)
            except ValueError:
                pass

            items.append({
                "name": name,
                "ip": ip,
                "netmask": netmask,
                "up": bool(e.get("up")),
                "speed_mbps": e.get("speed_mbps", 0),
                "virtual": virtual,
                "score": score,
                "broadcast": broadcast,
                "network": network,
            })

    items.sort(key=lambda x: (-x["score"], x["ip"]))
    return items


def best_local_ip() -> str:
    """挑一个最可能被局域网同伴访问到的 IP。"""
    for iface in list_interfaces():
        return iface["ip"]
    # 终极兜底：连一下外网看内核选了哪张网卡（不实际发包）
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------- 公网 IP

# 多源自动降级，任一成功即可
PUBLIC_IP_SOURCES = [
    ("https://ipv4.icanhazip.com", "text"),
    ("https://api.ipify.org", "text"),
    ("https://ipinfo.io/json", "json:ip"),
    ("https://ifconfig.me/ip", "text"),
]


def get_public_ip(timeout: float = 5.0) -> dict[str, Any]:
    """
    探测公网出口 IP。用 urllib 而非 requests，减少打包体积。
    返回 {ok, ip, source, error}
    """
    import urllib.request
    import json as _json

    ctx_headers = {"User-Agent": "Mozilla/5.0 (LANFileTransfer)"}

    for url, mode in PUBLIC_IP_SOURCES:
        try:
            req = urllib.request.Request(url, headers=ctx_headers)
            # 强制 IPv4，绕过部分环境 IPv6 优先导致的不一致
            opener = urllib.request.build_opener()
            with opener.open(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="ignore").strip()
            if mode == "json:ip":
                ip = _json.loads(body).get("ip", "").strip()
            else:
                ip = body
            # 基本校验
            ip = ip.split("\n")[0].strip()
            ipaddress.IPv4Address(ip)
            return {"ok": True, "ip": ip, "source": url, "error": ""}
        except Exception as exc:  # 换下一个源
            last_err = f"{type(exc).__name__}: {exc}"
            continue

    return {"ok": False, "ip": "", "source": "", "error": locals().get("last_err", "全部数据源不可达")}


# ---------------------------------------------------------------- 局域网扫描

def _read_arp_table() -> dict[str, str]:
    """
    读取系统 ARP 邻居表：{ip: mac}
    Windows 用 arp -a，Linux 用 ip neigh。
    """
    table: dict[str, str] = {}
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["arp", "-a"], capture_output=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout.decode("gbk", errors="ignore")
            # 形如:  192.168.1.1           50-e2-4e-52-21-48     动态
            for m in re.finditer(
                r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+([0-9a-fA-F]{2}[-:][0-9a-fA-F]{2}[-:]"
                r"[0-9a-fA-F]{2}[-:][0-9a-fA-F]{2}[-:][0-9a-fA-F]{2}[-:][0-9a-fA-F]{2})",
                out,
            ):
                table[m.group(1)] = m.group(2).replace("-", ":").lower()
        else:
            out = subprocess.run(
                ["ip", "neigh"], capture_output=True, timeout=10
            ).stdout.decode("utf-8", errors="ignore")
            for m in re.finditer(
                r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}).*?lladdr\s+([0-9a-fA-F:]{17})", out
            ):
                table[m.group(1)] = m.group(2).lower()
    except Exception:
        pass
    return table


def _is_broadcast_or_special(ip: str) -> bool:
    try:
        a = ipaddress.IPv4Address(ip)
    except ValueError:
        return True
    return a.is_multicast or a.is_loopback or str(a).endswith(".255")


def scan_lan(
    cidr: str | None = None,
    timeout: float = 0.35,
    max_workers: int = 128,
    quick: bool = True,
) -> dict[str, Any]:
    """
    扫描局域网存活设备。

    quick=True 时只做「ARP 表 + 本网段 ping 广播唤醒」，速度快（1-2 秒）；
    quick=False 时对整段并发 TCP 探活，慢但更全。

    返回 {ok, network, devices:[{ip, mac, hostname, source}], local_ip, elapsed}
    """
    t0 = time.time()

    if cidr is None:
        iface = None
        for it in list_interfaces():
            iface = it
            break
        if iface and iface.get("network"):
            cidr = iface["network"]
        else:
            local = best_local_ip()
            cidr = str(ipaddress.ip_network(f"{local}/24", strict=False))

    try:
        net = ipaddress.ip_network(cidr, strict=False)
    except ValueError as exc:
        return {"ok": False, "error": f"网段格式错误: {exc}", "devices": []}

    local_ip = best_local_ip()
    devices: dict[str, dict[str, Any]] = {}

    # ① ARP 表（最快的线索来源）
    for ip, mac in _read_arp_table().items():
        try:
            if ipaddress.IPv4Address(ip) not in net:
                continue
        except ValueError:
            continue
        if _is_broadcast_or_special(ip):
            continue
        devices[ip] = {"ip": ip, "mac": mac, "hostname": "", "source": "arp"}

    # ② 广播唤醒：往本网段广播地址发 UDP 包，促使网关/设备回填 ARP
    try:
        socks = []
        bcast = str(net.broadcast_address)
        for port in (9, 1900, 5353):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.settimeout(0.1)
            try:
                s.sendto(b"LANFILEPROBE", (bcast, port))
            except Exception:
                pass
            socks.append(s)
        time.sleep(0.25)
        for s in socks:
            s.close()
    except Exception:
        pass

    # ③ 重新读 ARP（广播后通常能多出几台）
    for ip, mac in _read_arp_table().items():
        try:
            if ipaddress.IPv4Address(ip) not in net:
                continue
        except ValueError:
            continue
        if _is_broadcast_or_special(ip):
            continue
        devices.setdefault(ip, {"ip": ip, "mac": mac, "hostname": "", "source": "arp"})
        if not devices[ip]["mac"]:
            devices[ip]["mac"] = mac

    # ④ 深度模式：对整段做 TCP 探活（挑几个常见端口，任一通即视为存活）
    if not quick and net.num_addresses <= 1024:
        probe_ports = (80, 443, 445, 3389, 8080, 22, 9000, 62078)

        def _probe(ip: str) -> str | None:
            for port in probe_ports:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                try:
                    if s.connect_ex((ip, port)) == 0:
                        return ip
                except Exception:
                    pass
                finally:
                    s.close()
            return None

        targets = [
            str(h) for h in net.hosts()
            if str(h) != local_ip and not _is_broadcast_or_special(str(h))
        ]
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for hit in pool.map(_probe, targets):
                if hit:
                    devices.setdefault(hit, {
                        "ip": hit, "mac": "", "hostname": "", "source": "tcp-probe",
                    })

    # ⑤ 解析主机名（并发，带超时保护，失败就留空）
    def _resolve(ip: str) -> str:
        try:
            socket.setdefaulttimeout(0.5)
            return socket.gethostbyaddr(ip)[0]
        except Exception:
            return ""

    ip_list = list(devices.keys())
    if ip_list:
        with ThreadPoolExecutor(max_workers=min(64, max(1, len(ip_list)))) as pool:
            names = list(pool.map(_resolve, ip_list))
        for ip, name in zip(ip_list, names):
            devices[ip]["hostname"] = name

    out = sorted(devices.values(), key=lambda d: tuple(int(x) for x in d["ip"].split(".")))

    return {
        "ok": True,
        "network": str(net),
        "local_ip": local_ip,
        "devices": out,
        "elapsed": round(time.time() - t0, 2),
        "error": "",
    }


def diagnose() -> dict[str, Any]:
    """
    网络环境自检：用于启动时给用户提示「跨网直连是否可能」。
    """
    ifaces = list_interfaces()
    pub = get_public_ip()

    # 判断公网 IP 是否等于任一网卡 IP（相等=直接公网，否则=在 NAT 后面）
    behind_nat = True
    if pub.get("ok"):
        for it in ifaces:
            if it["ip"] == pub["ip"]:
                behind_nat = False

    # UPnP 探测（判断能否自动做端口映射）
    upnp_ok = False
    try:
        msg = (
            'M-SEARCH * HTTP/1.1\r\n'
            'HOST:239.255.255.250:1900\r\n'
            'MAN:"ssdp:discover"\r\nMX:2\r\n'
            'ST:urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n\r\n'
        )
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(2.0)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.sendto(msg.encode(), ("239.255.255.250", 1900))
        s.recvfrom(2048)
        upnp_ok = True
        s.close()
    except Exception:
        upnp_ok = False

    return {
        "interfaces": ifaces,
        "public_ip": pub,
        "behind_nat": behind_nat,
        "upnp_available": upnp_ok,
        "lan_ok": bool(ifaces),
    }


if __name__ == "__main__":
    import json
    print("=== 网卡 ===")
    for i in list_interfaces():
        print(f"  {i['ip']:16s} {i['name']:30s} score={i['score']:3d} "
              f"virtual={i['virtual']} {i['speed_mbps']}Mbps")
    print("\n=== 推荐 IP ===", best_local_ip())
    print("\n=== 公网 IP ===", get_public_ip())
    print("\n=== 局域网扫描 ===")
    print(json.dumps(scan_lan(), ensure_ascii=False, indent=2))
