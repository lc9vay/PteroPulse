#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor.py — Pterodactyl 面板多服务器状态监控 (Render Web Service 版)

运行形态:
  * 作为 Render Web Service 常驻运行:
      - 内置 HTTP 服务，监听 $PORT(默认 10000)，/ 与 /health 供健康检查
      - 后台线程每 30s 依次拉取各服务器 Client API /resources，打印状态日志
      - 自 ping 保活：自动使用 RENDER_EXTERNAL_URL / SELF_URL，启动后立即 ping，
        之后每 3 分钟 ping 一次 /health，降低免费实例休眠概率
      - 服务器离线时打印 ALERT 日志(退出码保持 0 以维持服务存活)
      - 网页仪表盘: / /page (HTML) 与 /api/status (JSON)
      - starting / stopping 等中间态单独显示，不再一律显示「离线」
      - 仪表盘用 SSE(/api/stream) 实时推送，不再 10 秒轮询
  * 本地单次检查: python monitor.py --once  (拉一次所有服务器状态并退出)

环境变量:
  PTERO_PANEL         面板地址(必填,除非每个 key 都自带 @面板地址)
  PTERO_API_KEYS      Client API Key，多个用逗号分隔；支持 key@https://面板
  PTERO_SERVERS_JSON  可选手动绑定服务器列表 JSON
  PORT                监听端口(Render 注入，默认 10000)
  SELF_URL            自身公网地址(可选；默认用 RENDER_EXTERNAL_URL)
  MONITOR_INTERVAL    轮询秒数(默认 10)
  KEEPALIVE_INTERVAL  自 ping 间隔秒数(默认 180，即 3 分钟)
  ADMIN_TOKEN         管理 API 鉴权
  TG_BOT_TOKEN / TG_CHAT_ID  Telegram 掉线/恢复通知
  RENDER_API_KEY / SERVICE_ID  管理接口持久化用
"""
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

PANEL = os.environ.get("PTERO_PANEL", "").rstrip("/")
# 多面板支持: PTERO_API_KEYS 中每个条目可为 "ptlc_xxx" 或 "ptlc_xxx@https://其他面板地址"
PORT = int(os.environ.get("PORT", "10000"))
# 自保活地址：优先显式配置，否则自动取 Render 注入的对外 URL
SELF_URL = os.environ.get("SELF_URL", os.environ.get("RENDER_EXTERNAL_URL", "")).rstrip("/")
INTERVAL = int(os.environ.get("MONITOR_INTERVAL", "10"))
KEEPALIVE_INTERVAL = int(os.environ.get("KEEPALIVE_INTERVAL", "180"))  # 默认 3 分钟
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "")
_last_ok = {}        # serverId -> 最近一次 ok 布尔(掉线/恢复去重用)
RENDER_API_KEY = os.environ.get("RENDER_API_KEY", "")
SERVICE_ID = os.environ.get("SERVICE_ID", "")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_last_state = {}          # serverId -> state
_cache = {}               # serverId -> 最近一次成功结果 dict
# panel -> 响应风格: "standard"(官方 Pterodactyl) / "shironeko"(ShironekoServer 等魔改面板)
_panel_flavor = {}


def parse_key_entry(entry: str):
    """解析一个 key 条目。格式: 'ptlc_xxx' 或 'ptlc_xxx@https://面板地址'。
    返回 (key, panel)。panel 为空字符串表示用默认 PANEL。"""
    entry = entry.strip()
    if "@" in entry:
        key, panel = entry.rsplit("@", 1)
        panel = panel.strip().rstrip("/")
        if panel and not (panel.startswith("http://") or panel.startswith("https://")):
            raise ValueError("面板地址必须以 http:// 或 https:// 开头: %s" % panel)
        return key.strip(), panel
    return entry, ""


def _panel_of(key: str, entry_panel: str) -> str:
    """返回某 key 实际使用的面板地址。"""
    return entry_panel if entry_panel else PANEL


def _keys_to_env() -> str:
    """把当前 API_KEYS 序列化为环境变量值, 多面板的 key 带上 @panel。"""
    parts = []
    for k in API_KEYS:
        p = _raw_key_to_panel.get(k, "")
        parts.append(k + ("@" + p if p else ""))
    return ",".join(parts)


# ---- API Keys & 自动发现 ----
# PTERO_API_KEYS: 逗号分隔, 每个条目 "ptlc_xxx" 或 "ptlc_xxx@https://其他面板"
_key_entries = []     # [(key, panel), ...]  panel 为空=用默认 PANEL
_raw_key_to_panel = {}  # key -> panel, 供发现服务器时反查
_raw_list = os.environ.get("PTERO_API_KEYS", "").strip()
if not _raw_list:
    _raw_list = os.environ.get("PTERO_API_KEY", "").strip()
for _raw in _raw_list.split(","):
    _raw = _raw.strip()
    if not _raw:
        continue
    try:
        _k, _p = parse_key_entry(_raw)
    except ValueError as e:
        print(f"[monitor] 跳过无效 key 条目: {e}", file=sys.stderr)
        continue
    if not _k:
        continue
    _key_entries.append((_k, _p))
    _raw_key_to_panel[_k] = _p
API_KEYS = [k for k, _ in _key_entries]


def fetch(url: str, api_key: str, timeout: int = 20):
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "Authorization": "Bearer " + api_key,
        "User-Agent": UA,
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---- Telegram 推送 ----
def _urlopen(url, data=None, timeout=15):
    if data:
        req = urllib.request.Request(
            url, data=data,
            headers={"User-Agent": UA, "Content-Type": "application/json"},
        )
    else:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout)


def tg_send(text: str) -> bool:
    """推送文本到 Telegram。未配置则直接返回 False。"""
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TG_CHAT_ID,
        "text": text,
        "disable_web_page_preview": True,
        "parse_mode": "HTML",
    }).encode("utf-8")
    for attempt in range(2):
        try:
            with _urlopen(url, payload) as r:
                r.read()
            return True
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
            else:
                print(f"[monitor] TG 推送失败(忽略): {e}", flush=True)
                return False


def _get_server_name(sid: str) -> str:
    s = next((srv for srv in SERVERS if srv["serverId"] == sid), None)
    return s.get("name") or sid if s else sid


def note_transition(sid: str, prev_state: str, state: str, ok: bool, offline: bool = None):
    """状态翻转检测 + TG 通知。
    仅在「真正离线」(offline) 与「在线」(ok) 之间翻转时推送掉线/恢复；
    starting/stopping 过渡态不算离线，不发掉线通知；
    过渡态 -> running 视为启动完成，单独推送。"""
    if offline is None:
        offline = not ok
    if prev_state and state != prev_state:
        if ok:
            tag = "OK-恢复"
        elif offline:
            tag = "ALERT-掉线"
        else:
            tag = "INFO-过渡"
        print(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{tag}] [{sid}] "
            f"状态变化: {prev_state} -> {state}",
            flush=True,
        )
    # 用 offline 去重：只有真正掉线/恢复才 TG
    prev_off = _last_ok.get(sid)  # 这里存的是上次 offline 布尔；兼容旧语义用三态
    # 存 (ok, offline) 太重，改为：None=未知, True=在线, False=真离线, "t"=过渡
    if ok:
        cur = True
    elif offline:
        cur = False
    else:
        cur = "t"
    if prev_off is not None and prev_off != cur:
        name = _get_server_name(sid)
        state_line = (
            f"服务器: {name}\nID: {sid}\n"
            f"状态: <code>{state}</code>\n"
            f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        )
        if cur is True and prev_off is False:
            tg_send(f"🟢 <b>服务器已恢复</b>\n{state_line}")
        elif cur is True and prev_off == "t":
            tg_send(f"🟢 <b>服务器已启动</b>\n{state_line}")
        elif cur is False and prev_off is True:
            tg_send(f"🔴 <b>服务器掉线</b>\n{state_line}")
        # 其他过渡态之间的翻转不发 TG
    _last_ok[sid] = cur


# ---- 手动服务器绑定兜底 ----
_MANUAL_SERVERS = []
try:
    _raw_json = os.environ.get("PTERO_SERVERS_JSON", "").strip()
    if _raw_json:
        _manual = json.loads(_raw_json)
        if isinstance(_manual, list):
            _MANUAL_SERVERS = _manual
except Exception as _e:
    print(f"[monitor] PTERO_SERVERS_JSON 解析失败(忽略): {_e}", file=sys.stderr)


def _match_key_for_sid(target_sid: str, key: str, panel: str) -> bool:
    """验证某 key 是否能访问某 serverId。兼容官方与 Shironeko 响应结构。"""
    try:
        info = fetch(f"{panel}/api/client/servers/{target_sid}", key, timeout=12)
        a = info.get("attributes") or info.get("server") or {}
        ids = {str(a.get(k) or "").strip() for k in ("identifier", "uuid", "uuid_short")}
        return target_sid in ids
    except Exception:
        return False


def _discover_servers_for_key(key: str, panel: str) -> list:
    """用单个 key 拉取服务器列表, 返回 [(serverId, name), ...]。
    先试官方 Pterodactyl 的 /api/client, 遇 404 时回退 ShironekoServer 风格的
    /api/client/servers(其列表包装在 servers.data, ID 字段是 uuid/uuid_short)。"""
    try:
        data = fetch(f"{panel}/api/client", key, timeout=15)
        _panel_flavor.setdefault(panel, "standard")
        out = []
        for s in data.get("data", []):
            a = s.get("attributes", {})
            sid = str(a.get("identifier", "")).strip()
            if sid:
                out.append((sid, str(a.get("name") or "")))
        return out
    except urllib.error.HTTPError as e:
        if e.code not in (404, 405):
            raise
    data = fetch(f"{panel}/api/client/servers", key, timeout=15)
    _panel_flavor[panel] = "shironeko"
    out = []
    for s in (data.get("servers") or {}).get("data", []):
        sid = str(s.get("uuid") or s.get("uuid_short") or "").strip()
        if sid:
            out.append((sid, str(s.get("name") or "")))
    return out


def discover_servers() -> list:
    """对每个 apiKey 自动发现服务器，再用 PTERO_SERVERS_JSON 补全。"""
    found = {}
    for key in API_KEYS:
        panel = _panel_of(key, _raw_key_to_panel.get(key, ""))
        try:
            for sid, name in _discover_servers_for_key(key, panel):
                found[sid] = {"serverId": sid, "apiKey": key, "panel": panel, "name": name}
        except Exception as e:
            print(f"[monitor] apiKey 发现服务器失败(忽略): {e}", file=sys.stderr)
    for m in _MANUAL_SERVERS:
        sid = str(m.get("serverId", "")).strip()
        key = str(m.get("apiKey", "")).strip()
        panel = str(m.get("panel", "")).strip() or PANEL
        if not sid:
            continue
        if not key:
            for _k in API_KEYS:
                _p = _panel_of(_k, _raw_key_to_panel.get(_k, ""))
                if _match_key_for_sid(sid, _k, _p):
                    key, panel = _k, _p
                    break
        if key:
            if sid in found:
                if m.get("name"):
                    found[sid]["name"] = m["name"]
            else:
                found[sid] = {
                    "serverId": sid, "apiKey": key, "panel": panel, "name": m.get("name", ""),
                }
    return list(found.values())


# 面板地址校验: 未设置 PTERO_PANEL 时, 所有 key / 手动绑定条目必须自带面板地址,
# 否则启动即报错退出, 避免用空地址拼 URL 导致难以排查的静默失败
def _needs_default_panel() -> bool:
    if any(not p for _, p in _key_entries):
        return True
    for m in _MANUAL_SERVERS:
        if str(m.get("apiKey", "")).strip() and not str(m.get("panel", "")).strip():
            return True
    return False


if not PANEL and _needs_default_panel():
    print(
        "[monitor] 未配置面板地址: 请设置环境变量 PTERO_PANEL(如 https://panel.example.com),"
        "或在 PTERO_API_KEYS 中用 'key@https://面板地址'、"
        "在 PTERO_SERVERS_JSON 中用 \"panel\": \"https://...\" 逐条指定",
        file=sys.stderr,
    )
    sys.exit(1)

SERVERS = discover_servers()


def human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


def uptime_str(ms: int) -> str:
    if ms <= 0:
        return "0s"
    s = ms // 1000
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}d{h:02d}h{m:02d}m"
    return f"{h:02d}:{m:02d}:{rem % 60:02d}"


def get_status_dict(srv) -> dict:
    """拉取一台服务器状态。ok 仅当 state==running 且未挂起。
    响应解析兼容官方 Pterodactyl 与 ShironekoServer(魔改面板)两种风格。"""
    sid = srv["serverId"]
    panel = srv.get("panel") or PANEL
    base = f"{panel}/api/client/servers/{sid}"
    info = fetch(base, srv["apiKey"])
    flavor = _panel_flavor.get(panel)
    if flavor is None:
        # 详情响应: 官方是 {"attributes": {...}}, ShironekoServer 是 {"server": {...}}
        flavor = "shironeko" if "server" in info else "standard"
        _panel_flavor[panel] = flavor

    if flavor == "shironeko":
        a = info.get("server") or {}
        limits = a.get("limits") or {}
        suspended = bool(a.get("is_suspended"))
        stats = fetch(f"{base}/resources", srv["apiKey"])
        r = stats.get("resources") or {}
        state = r.get("state", "unknown")
        net = r.get("network") or {}
        net_tx = float(net.get("tx_bytes") or 0)
        net_rx = float(net.get("rx_bytes") or 0)
    else:
        a = info["attributes"]
        limits = a.get("limits") or {}
        stats = fetch(f"{base}/resources", srv["apiKey"])
        attr = stats["attributes"]
        state = attr.get("current_state", "unknown")
        suspended = attr.get("is_suspended", False)
        r = attr.get("resources", {})
        net_tx = float(r.get("network_tx_bytes") or 0)
        net_rx = float(r.get("network_rx_bytes") or 0)

    name = str(a.get("name") or "")
    mem_limit = float(limits.get("memory") or 0)
    disk_limit = float(limits.get("disk") or 0)
    cpu_limit = float(limits.get("cpu") or 0)
    cpu = float(r.get("cpu_absolute") or 0)
    mem_bytes = float(r.get("memory_bytes") or 0)
    disk_bytes = float(r.get("disk_bytes") or 0)
    uptime = int(r.get("uptime") or 0)

    mem_pct = mem_bytes / 1048576 / mem_limit * 100 if mem_limit > 0 else 0.0
    disk_pct = disk_bytes / 1048576 / disk_limit * 100 if disk_limit > 0 else 0.0
    # running 才算在线；starting/stopping 为过渡态（不算离线）；其余为离线
    ok = state == "running" and not suspended
    transitional = state in ("starting", "stopping") and not suspended
    offline = (not ok) and (not transitional)

    return {
        "name": name, "state": state, "suspended": suspended, "ok": ok,
        "transitional": transitional, "offline": offline,
        "cpu": cpu, "cpuLimit": cpu_limit, "cpuPct": cpu,
        "memUsedMB": mem_bytes / 1048576, "memLimitMB": mem_limit, "memPct": mem_pct,
        "diskUsedMB": disk_bytes / 1048576, "diskLimitMB": disk_limit, "diskPct": disk_pct,
        "netTxBytes": net_tx, "netRxBytes": net_rx,
        "netTx": human(net_tx), "netRx": human(net_rx),
        "uptimeMs": uptime, "uptime": uptime_str(uptime),
    }


def _fmt_line(d: dict) -> str:
    return (
        f"[monitor] 服务器[{d['name']}] 状态={d['state']} 挂起={d['suspended']}"
        f" CPU={d['cpu']:.1f}/{d['cpuLimit']:.0f}%"
        f" 内存={d['memUsedMB']:.1f}/{d['memLimitMB']:.0f}MB({d['memPct']:.1f}%)"
        f" 磁盘={d['diskUsedMB']:.1f}/{d['diskLimitMB']:.0f}MB({d['diskPct']:.1f}%)"
        f" 上行={d['netTx']} 下行={d['netRx']} 运行={d['uptime']}"
    )


def get_status_line(srv) -> tuple:
    d = get_status_dict(srv)
    return _fmt_line(d), d["ok"]


def monitor_loop():
    """后台循环：每 INTERVAL 秒轮询所有服务器。"""
    while True:
        for srv in SERVERS:
            sid = srv["serverId"]
            try:
                d = get_status_dict(srv)
                ok = d["ok"]
                state = d["state"]
                _cache[sid] = d
                print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {_fmt_line(d)}", flush=True)
                prev = _last_state.get(sid)
                note_transition(sid, prev or "", state, ok, d.get("offline", not ok))
                _last_state[sid] = state
                if d.get("offline"):
                    print(
                        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [ALERT] [{sid}] "
                        f"服务器离线！当前状态: {state}",
                        flush=True,
                    )
                elif d.get("transitional"):
                    print(
                        f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [INFO] [{sid}] "
                        f"过渡状态: {state}",
                        flush=True,
                    )
            except Exception as e:
                print(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [monitor] [{sid}] 查询失败: {e}",
                    flush=True,
                )
        time.sleep(INTERVAL)


def keepalive_loop():
    """免费实例保活：启动后立即 ping，之后按 KEEPALIVE_INTERVAL 自 ping /health。"""
    if not SELF_URL:
        print(
            "[monitor] 未设置 SELF_URL / RENDER_EXTERNAL_URL，跳过自 ping 保活。"
            "建议用 UptimeRobot 每 5 分钟访问 /health",
            flush=True,
        )
        return
    # 优先 ping 轻量 /health，失败再 ping 根路径
    targets = [
        SELF_URL.rstrip("/") + "/health",
        SELF_URL.rstrip("/"),
    ]
    print(
        f"[monitor] 保活已启用: 目标={targets[0]} 间隔={KEEPALIVE_INTERVAL}s",
        flush=True,
    )
    first = True
    while True:
        if not first:
            time.sleep(KEEPALIVE_INTERVAL)
        first = False
        ok = False
        last_err = None
        for url in targets:
            try:
                with urllib.request.urlopen(url, timeout=12) as resp:
                    resp.read()
                print(
                    f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [keepalive] ping {url} ok",
                    flush=True,
                )
                ok = True
                break
            except Exception as e:
                last_err = e
        if not ok:
            print(
                f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [keepalive] ping 失败: {last_err}",
                flush=True,
            )


def verify_server(srv) -> bool:
    try:
        panel = srv.get("panel") or PANEL
        base = f"{panel}/api/client/servers/{srv['serverId']}"
        fetch(base, srv["apiKey"], timeout=15)
        return True
    except Exception:
        return False


def persist_servers() -> bool:
    """把当前 API Keys 等写回 Render 环境变量。"""
    if not (RENDER_API_KEY and SERVICE_ID):
        print("[monitor] 缺少 RENDER_API_KEY/SERVICE_ID，无法持久化", flush=True)
        return False
    vars_payload = [
        {"key": "PTERO_API_KEYS", "value": _keys_to_env()},
        {"key": "MONITOR_SCRIPT", "value": os.environ.get("MONITOR_SCRIPT", "")},
        {"key": "ADMIN_TOKEN", "value": ADMIN_TOKEN},
        {"key": "RENDER_API_KEY", "value": RENDER_API_KEY},
        {"key": "SERVICE_ID", "value": SERVICE_ID},
    ]
    _servers_json = os.environ.get("PTERO_SERVERS_JSON", "").strip()
    if _servers_json:
        vars_payload.append({"key": "PTERO_SERVERS_JSON", "value": _servers_json})
    body = json.dumps(vars_payload).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.render.com/v1/services/{SERVICE_ID}/env-vars",
        data=body,
        method="PUT",
        headers={
            "Authorization": "Bearer " + RENDER_API_KEY,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        print("[monitor] 已持久化到 Render 环境变量", flush=True)
        return True
    except Exception as e:
        print(f"[monitor] 持久化失败: {e}", flush=True)
        return False


def status_json() -> list:
    """构造 /api/status 快照。优先缓存。"""
    out = []
    for srv in SERVERS:
        sid = srv["serverId"]
        base = {"serverId": sid, "panel": srv.get("panel") or PANEL}
        cached = _cache.get(sid)
        if cached:
            d = dict(cached)
        else:
            try:
                d = get_status_dict(srv)
                _cache[sid] = d
            except Exception:
                d = {
                    "name": srv["name"] or sid,
                    "state": _last_state.get(sid, "unknown"),
                    "ok": False, "suspended": False,
                    "transitional": False, "offline": True,
                    "cpu": 0.0, "cpuLimit": 0.0, "cpuPct": 0.0,
                    "memUsedMB": 0.0, "memLimitMB": 0.0, "memPct": 0.0,
                    "diskUsedMB": 0.0, "diskLimitMB": 0.0, "diskPct": 0.0,
                    "netTx": "0B", "netRx": "0B",
                    "netTxBytes": 0.0, "netRxBytes": 0.0,
                    "uptime": "0s", "uptimeMs": 0,
                }
        d.update(base)
        if srv["name"]:
            d["name"] = srv["name"]
        out.append(d)
    return out


def serve():
    """Render Web Service 常驻模式。"""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PteroPulse · 概览</title>
<style>
*{box-sizing:border-box}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;background:#f4f5f7;color:#1f2329;min-height:100vh}
.wrap{max-width:1280px;margin:0 auto;padding:28px 20px 40px}
header{margin-bottom:18px}
h1{font-size:20px;margin:0 0 4px;font-weight:700;display:flex;align-items:center;gap:8px}
.sub{font-size:13px;color:#8a8f98}
.stat-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-bottom:20px}
.stat-card{background:#fff;border:1px solid #ebedf0;border-radius:14px;padding:16px 18px;box-shadow:0 1px 2px #00000008}
.stat-card .lbl{font-size:13px;color:#6b7280;margin-bottom:10px;display:flex;align-items:center;gap:6px}
.stat-card .val{font-size:20px;font-weight:700;display:flex;align-items:center;gap:8px}
.stat-card .val .dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.stat-card .netrow{font-size:13px;font-weight:600;color:#374151;display:flex;gap:14px;flex-wrap:wrap}
.stat-card .netrow .rate{font-size:12px;color:#8a8f98;font-weight:500;margin-top:4px;display:block}
#alerts{display:none;background:#fef2f2;border:1px solid #fecaca;color:#b91c1c;border-radius:12px;padding:12px 16px;margin-bottom:16px;font-size:13px}
.list{background:#fff;border:1px solid #ebedf0;border-radius:14px;overflow:hidden;box-shadow:0 1px 2px #00000008}
.list-toolbar{display:flex;justify-content:flex-end;padding:10px 18px;font-size:12px;color:#8a8f98;border-bottom:1px solid #f1f2f4}
.row{display:flex;align-items:center;gap:10px;padding:16px 18px;border-bottom:1px solid #f1f2f4}
.row:last-child{border-bottom:none}
.row .status-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}
.row .name-col{width:150px;flex-shrink:0;font-weight:600;font-size:14px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.row .name-col .srv-state{font-size:11px;font-weight:500;color:#8a8f98;margin-top:3px}
.row .cols{flex:1;display:flex;flex-wrap:wrap;gap:22px;align-items:center}
.col{min-width:78px}
.col .k{font-size:11px;color:#9aa0a8;margin-bottom:4px;white-space:nowrap}
.col .v{font-size:13px;font-weight:600;color:#1f2329;white-space:nowrap}
.col .v u{text-decoration:none;display:block;height:3px;border-radius:3px;margin-top:5px;background:#e5e7eb;overflow:hidden}
.col .v u i{display:block;height:100%;border-radius:3px;transition:width .8s ease,background .4s}
.row.offline .name-col{color:#9aa0a8}
.row.offline .cols{color:#c4c8cd;font-size:13px}
.row.starting .name-col{color:#b45309}
.row.starting .name-col .srv-state{color:#d97706}
.row.starting .cols{color:#d97706;font-size:13px}
footer{color:#9aa0a8;text-align:center;margin-top:20px;font-size:12px}
@media (max-width:900px){.stat-grid{grid-template-columns:repeat(2,1fr)}}
@media (max-width:640px){
  .stat-grid{grid-template-columns:1fr 1fr}
  .row{flex-wrap:wrap}
  .row .name-col{width:auto}
  .row .cols{gap:14px 20px}
}
</style></head><body><div class="wrap">
<header>
  <h1>🖥 概览</h1>
  <div class="sub">当前时间 <span id="clock">—</span></div>
</header>
<div class="stat-grid">
  <div class="stat-card"><div class="lbl">服务器总数</div><div class="val"><span class="dot" style="background:#3b82f6"></span><span id="statTotal">0</span></div></div>
  <div class="stat-card"><div class="lbl">在线服务器</div><div class="val"><span class="dot" style="background:#22c55e"></span><span id="statOnline">0</span></div></div>
  <div class="stat-card"><div class="lbl">离线/异常</div><div class="val"><span class="dot" style="background:#ef4444"></span><span id="statOffline">0</span></div></div>
  <div class="stat-card"><div class="lbl">网络</div>
    <div class="netrow">↑<span id="netTxTotal">0</span> · ↓<span id="netRxTotal">0</span></div>
    <span class="rate" id="netRate">0 KB/s ↑ · 0 KB/s ↓</span>
  </div>
</div>
<div id="alerts"></div>
<div class="list">
  <div class="list-toolbar">实时推送 · 最后更新 <span id="ts">—</span></div>
  <div id="rows"></div>
</div>
<footer>PteroPulse</footer>
</div>
<script>
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function barColor(pct){return pct>90?'#ef4444':pct>75?'#f59e0b':'#22c55e'}
function human(n){
  var u=['B','KB','MB','GB','TB'],i=0;n=Number(n)||0;
  while(n>=1024&&i<u.length-1){n/=1024;i++}
  return n.toFixed(i===0?0:2)+' '+u[i];
}
function rateStr(bytesPerSec){return human(bytesPerSec)+'/s'}
var prev={};
function computeRate(d,now){
  var p=prev[d.serverId],txRate=0,rxRate=0;
  if(p){
    var dt=(now-p.t)/1000;
    if(dt>0){
      txRate=Math.max(0,(d.netTxBytes-p.tx)/dt);
      rxRate=Math.max(0,(d.netRxBytes-p.rx)/dt);
    }
  }
  prev[d.serverId]={tx:d.netTxBytes,rx:d.netRxBytes,t:now};
  return {tx:txRate,rx:rxRate};
}
function metric(label,pct,valueText){
  return '<div class="col"><div class="k">'+label+'</div><div class="v">'+valueText+
    '<u><i style="width:'+pct+'%;background:'+barColor(pct)+'"></i></u></div></div>';
}
function statusLabel(d){
  if(d.suspended) return '已挂起';
  var s=d.state||'';
  if(s==='starting') return '启动中';
  if(s==='stopping') return '停止中';
  if(s==='offline') return '离线';
  if(s==='running') return '运行中';
  return s || '未知';
}
function stateText(d){
  if(d.state) return d.state;
  if(d.suspended) return 'suspended';
  return d.ok ? 'running' : 'offline';
}
function fmtMem(d){
  var u=Number(d.memUsedMB)||0, lim=Number(d.memLimitMB)||0;
  if(lim>0) return u.toFixed(1)+'/'+lim.toFixed(0)+' MB';
  return u.toFixed(1)+' MB';
}
function fmtDisk(d){
  var u=Number(d.diskUsedMB)||0, lim=Number(d.diskLimitMB)||0;
  if(lim>=1024) return (u/1024).toFixed(2)+'/'+(lim/1024).toFixed(1)+' GB';
  if(lim>0) return u.toFixed(2)+'/'+lim.toFixed(0)+' MB';
  return u.toFixed(2)+' MB';
}
function metricsCols(d,rate,extraStatus){
  var cpu=Math.max(0,Math.min(100,d.cpuPct||0));
  var mem=Math.max(0,Math.min(100,d.memPct||0));
  var disk=Math.max(0,Math.min(100,d.diskPct||0));
  var stHtml = extraStatus ? '<div class="col"><div class="k">状态</div><div class="v">'+esc(extraStatus)+'</div></div>' : '';
  return stHtml+
    '<div class="col"><div class="k">运行时间</div><div class="v">'+esc(d.uptime||'—')+'</div></div>'+
    metric('CPU',cpu,cpu.toFixed(2)+'%')+
    metric('内存',mem,fmtMem(d))+
    metric('存储',disk,fmtDisk(d))+
    '<div class="col"><div class="k">上传</div><div class="v">'+rateStr(rate.tx)+'</div></div>'+
    '<div class="col"><div class="k">下载</div><div class="v">'+rateStr(rate.rx)+'</div></div>'+
    '<div class="col"><div class="k">总上传</div><div class="v">'+esc(d.netTx||'0B')+'</div></div>'+
    '<div class="col"><div class="k">总下载</div><div class="v">'+esc(d.netRx||'0B')+'</div></div>';
}
function rowHTML(d,rate){
  var ok=d.ok;
  var st=d.state||'';
  var isTrans = (typeof d.transitional==='boolean') ? d.transitional : (st==='starting'||st==='stopping');
  var isOffline = (typeof d.offline==='boolean') ? d.offline : (!ok && !isTrans && !d.suspended);
  // 在线 或 过渡态(starting/stopping)：展示真实状态 + 内存/磁盘等指标
  if(ok || isTrans){
    var dot = ok ? '#22c55e' : '#f59e0b';
    var cls = ok ? '' : ' starting';
    return '<div class="row'+cls+'"><span class="status-dot" style="background:'+dot+'"></span>'+
      '<div class="name-col"><div class="srv-name">'+esc(d.name)+'</div><div class="srv-state">'+esc(stateText(d))+'</div></div>'+
      '<div class="cols">'+metricsCols(d,rate)+'</div></div>';
  }
  // 真离线 / 挂起：仅显示状态文案
  var dot = d.suspended ? '#f59e0b' : '#ef4444';
  return '<div class="row offline"><span class="status-dot" style="background:'+dot+'"></span>'+
    '<div class="name-col"><div class="srv-name">'+esc(d.name)+'</div><div class="srv-state">'+esc(stateText(d))+'</div></div>'+
    '<div class="cols"><span>'+esc(statusLabel(d))+'</span></div></div>';
}
function applyStatus(arr){
  var html='',down=[],online=0,txTotal=0,rxTotal=0,txRateTotal=0,rxRateTotal=0,now=Date.now();
  for(var i=0;i<arr.length;i++){
    var d=arr[i];
    var rate={tx:0,rx:0};
    if(d.ok || d.transitional || d.state==='starting' || d.state==='stopping')rate=computeRate(d,now);
    html+=rowHTML(d,rate);
    var isOffline = (typeof d.offline==='boolean') ? d.offline : (!d.ok && d.state!=='starting' && d.state!=='stopping' && !d.suspended);
    var isTrans = (typeof d.transitional==='boolean') ? d.transitional : (d.state==='starting'||d.state==='stopping');
    if(d.ok)online++;
    else if(isOffline) down.push(d.name+'('+statusLabel(d)+')');
    else if(isTrans) {}
    else if(d.suspended) down.push(d.name+'(已挂起)');
    else down.push(d.name+'('+statusLabel(d)+')');
    txTotal+=Number(d.netTxBytes)||0;
    rxTotal+=Number(d.netRxBytes)||0;
    txRateTotal+=rate.tx;
    rxRateTotal+=rate.rx;
  }
  document.getElementById('rows').innerHTML=html;
  document.getElementById('statTotal').textContent=arr.length;
  document.getElementById('statOnline').textContent=online;
  document.getElementById('statOffline').textContent=down.length;
  document.getElementById('netTxTotal').textContent=human(txTotal);
  document.getElementById('netRxTotal').textContent=human(rxTotal);
  document.getElementById('netRate').textContent=rateStr(txRateTotal)+' ↑ · '+rateStr(rxRateTotal)+' ↓';
  var al=document.getElementById('alerts');
  if(down.length){
    al.style.display='block';
    al.innerHTML='⚠ 离线服务器: '+down.map(esc).join('、');
  }else{al.style.display='none'}
  var ts=new Date();
  document.getElementById('ts').textContent=ts.toLocaleString();
  document.getElementById('clock').textContent=ts.toLocaleTimeString();
}
function connectStream(){
  if(window.__es){try{window.__es.close()}catch(e){}}
  var es=new EventSource('/api/stream');
  window.__es=es;
  es.onmessage=function(ev){
    try{ applyStatus(JSON.parse(ev.data)); }catch(e){}
  };
  es.onerror=function(){
    // 断线后 2 秒重连；期间用短轮询兜底一次
    es.close();
    fetch('/api/status').then(function(r){return r.json()}).then(applyStatus).catch(function(){});
    setTimeout(connectStream, 2000);
  };
}
connectStream();
setInterval(function(){document.getElementById('clock').textContent=new Date().toLocaleTimeString()},1000);
</script></body></html>
"""

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, code, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            if not ADMIN_TOKEN:
                return False
            q = {}
            if "?" in self.path:
                for pair in self.path.split("?", 1)[1].split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        q[k] = v
            auth = self.headers.get("Authorization", "")
            return q.get("token") == ADMIN_TOKEN or auth == "Bearer " + ADMIN_TOKEN

        def _keys_public(self):
            out = []
            for k in API_KEYS:
                count = sum(1 for s in SERVERS if s["apiKey"] == k)
                p = _raw_key_to_panel.get(k, PANEL)
                out.append({
                    "apiKey": k, "panel": p, "serverCount": count,
                    "servers": [s["serverId"] for s in SERVERS if s["apiKey"] == k],
                })
            return out

        def do_HEAD(self):
            # Render / 外部监控常用 HEAD，避免 501
            path = self.path.split("?", 1)[0]
            if path in ("/", "/page", "/index.html", "/dashboard", "/health", "/api/status"):
                self.send_response(200)
                if path == "/api/status":
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                elif path == "/health":
                    self.send_header("Content-Type", "text/plain")
                else:
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/status":
                data = status_json()
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/api/stream":
                # Server-Sent Events：缓存有变化立刻推给前端（约 1s 内）
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()
                last = None
                try:
                    while True:
                        payload = json.dumps(status_json(), ensure_ascii=False)
                        if payload != last:
                            chunk = ("data: " + payload + "\n\n").encode("utf-8")
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            last = payload
                        else:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                        time.sleep(1)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
            elif path in ("/api/servers", "/api/keys"):
                if not self._authed():
                    self._send_json(401, {"error": "unauthorized"})
                    return
                self._send_json(200, self._keys_public())
            elif path in ("/", "/page", "/index.html", "/dashboard"):
                body = DASHBOARD_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"ok")
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if path not in ("/api/servers", "/api/keys"):
                self._send_json(404, {"error": "not found"})
                return
            if not self._authed():
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                n = int(self.headers.get("Content-Length", "0"))
                data = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
                raw_key = str(data.get("apiKey", "")).strip()
                try:
                    key, entry_panel = parse_key_entry(raw_key)
                except ValueError as e:
                    self._send_json(400, {"error": str(e), "keys": self._keys_public()})
                    return
                panel = _panel_of(key, entry_panel)
                if not key:
                    self._send_json(400, {"error": "apiKey 必填", "keys": self._keys_public()})
                    return
                if key in API_KEYS:
                    self._send_json(409, {"error": "该 apiKey 已存在", "keys": self._keys_public()})
                    return
                try:
                    if not _discover_servers_for_key(key, panel):
                        self._send_json(400, {
                            "error": "该 apiKey 无法访问任何服务器，已拒绝添加",
                            "keys": self._keys_public(),
                        })
                        return
                except Exception:
                    self._send_json(400, {
                        "error": "该 apiKey 无效或无权访问面板，已拒绝添加",
                        "keys": self._keys_public(),
                    })
                    return
                API_KEYS.append(key)
                _key_entries.append((key, entry_panel))
                _raw_key_to_panel[key] = entry_panel
                SERVERS[:] = discover_servers()
                _last_state.clear()
                _cache.clear()
                persisted = persist_servers()
                self._send_json(200, {
                    "ok": True, "persisted": persisted, "keys": self._keys_public(),
                })
            except Exception as e:
                self._send_json(400, {"error": "请求解析失败: " + str(e)})

        def do_DELETE(self):
            path = self.path.split("?", 1)[0]
            if not path.startswith("/api/servers/") and not path.startswith("/api/keys/"):
                self._send_json(404, {"error": "not found"})
                return
            if not self._authed():
                self._send_json(401, {"error": "unauthorized"})
                return
            q = {}
            if "?" in self.path:
                for pair in self.path.split("?", 1)[1].split("&"):
                    if "=" in pair:
                        k, v = pair.split("=", 1)
                        q[k] = v
            raw = path.split("?", 1)[0]
            seg = (
                raw[len("/api/servers/"):]
                if raw.startswith("/api/servers/")
                else raw[len("/api/keys/"):]
            )
            seg = seg.strip("/")
            if not seg:
                self._send_json(400, {"error": "缺少 apiKey"})
                return
            key = q.get("apiKey", seg)
            if key not in API_KEYS:
                self._send_json(404, {
                    "error": "该 apiKey 不存在", "keys": self._keys_public(),
                })
                return
            API_KEYS.remove(key)
            _key_entries[:] = [(k, p) for k, p in _key_entries if k != key]
            _raw_key_to_panel.pop(key, None)
            SERVERS[:] = discover_servers()
            _last_state.clear()
            _cache.clear()
            persisted = persist_servers()
            self._send_json(200, {
                "ok": True, "persisted": persisted, "keys": self._keys_public(),
            })

        def log_message(self, *args):
            pass

    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=keepalive_loop, daemon=True).start()
    print(
        f"[monitor] 监控启动: 默认面板={PANEL or '(未设置, 按 key 指定)'}  服务器数={len(SERVERS)}  "
        f"每 {INTERVAL}s 轮询  监听 :{PORT}  "
        f"保活={'ON '+SELF_URL if SELF_URL else 'OFF'}",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


def once():
    """本地/调试：单次检查所有服务器。"""
    rc = 0
    for srv in SERVERS:
        try:
            line, ok = get_status_line(srv)
            print(line)
            if not ok:
                rc = 1
        except Exception as e:
            print(f"[monitor] [{srv['serverId']}] 检查失败: {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    if not SERVERS:
        print(
            "[monitor] 未配置任何服务器(PTERO_API_KEYS 或 PTERO_SERVERS_JSON)",
            file=sys.stderr,
        )
        sys.exit(1)
    if "--once" in sys.argv:
        sys.exit(once())
    serve()
