#!/usr/bin/env python3
"""
Claude Code launcher + reverse-proxy (single-file, interactive provider manager).

用法:
    python3 claude-launch.py              # 交互式菜单 (选择模型启动)
    python3 claude-launch.py --manage     # 直接进入提供商管理菜单
    python3 claude-launch.py --proxy      # 代理模式 (内部使用)
    python3 claude-launch.py --list       # 打印所有已注册模型

配置文件:
    ~/.claude-launcher/providers.json
    (首次运行若不存在, 会用内置 DEFAULT_PROVIDERS 初始化)
"""
from __future__ import annotations

import argparse
import copy
import http.server
import json
import os
import re
import socketserver
import subprocess
import sys
import time
import urllib.error
import urllib.request


# ═══════════════════════════════════════════════════════════════════
#  常量
# ═══════════════════════════════════════════════════════════════════
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 8888
PROXY_URL  = f"http://{PROXY_HOST}:{PROXY_PORT}"

SETTINGS_PATH = os.path.expanduser("~/.claude/settings.json")
CONFIG_DIR    = os.path.expanduser("~/.claude-launcher")
CONFIG_PATH   = os.path.join(CONFIG_DIR, "providers.json")
SELF_PATH     = os.path.abspath(__file__)

DEFAULT_PROVIDERS: list[dict] = []

COLOR_PALETTE = [
    ("31", "红  red"),
    ("32", "绿  green"),
    ("33", "黄  yellow"),
    ("34", "蓝  blue"),
    ("35", "品  magenta"),
    ("36", "青  cyan"),
    ("37", "白  white"),
    ("91", "亮红 bright-red"),
    ("92", "亮绿 bright-green"),
    ("93", "亮黄 bright-yellow"),
    ("94", "亮蓝 bright-blue"),
    ("95", "亮品 bright-magenta"),
    ("96", "亮青 bright-cyan"),
]


# ═══════════════════════════════════════════════════════════════════
#  配置持久化
# ═══════════════════════════════════════════════════════════════════
def load_providers() -> list[dict]:
    if not os.path.exists(CONFIG_PATH):
        save_providers(DEFAULT_PROVIDERS)
        return copy.deepcopy(DEFAULT_PROVIDERS)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("providers.json 顶层必须是数组")
    for p in data:
        for k in ("label", "color", "base_url", "api_key", "models"):
            if k not in p:
                raise ValueError(f"provider 缺字段: {k}")
        if not isinstance(p["models"], list):
            raise ValueError("models 必须是列表")
    return data


def save_providers(providers: list[dict]) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(providers, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_PATH)


def build_model_index(providers: list[dict]) -> dict[str, tuple[str, str]]:
    idx: dict[str, tuple[str, str]] = {}
    for p in providers:
        for m in p["models"]:
            idx[m] = (p["base_url"], p["api_key"])
    return idx


def flat_models(providers: list[dict]) -> list[tuple[str, dict]]:
    out = []
    for p in providers:
        for m in p["models"]:
            out.append((m, p))
    return out


# ═══════════════════════════════════════════════════════════════════
#  Proxy
# ═══════════════════════════════════════════════════════════════════
class _ProxyHandler(http.server.BaseHTTPRequestHandler):
    model_index: dict[str, tuple[str, str]] = {}

    def log_message(self, fmt, *args):
        pass


    def _send_json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        try:
            clen = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(clen))
        except Exception as e:
            self._send_json(400, {"type": "error", "error": {
                "type": "invalid_request_error", "message": f"Bad JSON: {e}"}})
            return

        target = data.get("model", "")
        if target not in self.model_index:
            self._send_json(400, {"type": "error", "error": {
                "type": "invalid_request_error",
                "message": f"Unknown model: {target}. Available: {', '.join(self.model_index.keys())}"}})
            return

        base_url, api_key = self.model_index[target]
        req = urllib.request.Request(
            base_url + self.path,
            data=json.dumps(data).encode(), method="POST")
        req.add_header("x-api-key", api_key)
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("Content-Type", "application/json")
        req.add_header("User-Agent", "Claude-Code/2.1.114")

        try:
            resp = urllib.request.urlopen(req, timeout=600)
            self._stream_response(resp)
        except urllib.error.HTTPError as e:
            self._stream_response(e, is_error=True)
        except Exception as e:
            self._send_json(500, {"type": "error", "error": {
                "type": "api_error", "message": str(e)}})

    def _stream_response(self, resp, is_error=False):
        """Relay upstream headers + body as a stream, so SSE / chunked keeps flowing."""
        status = resp.code if is_error else resp.status
        self.send_response(status)
        # forward relevant headers but avoid hop-by-hop
        skip = {"connection", "keep-alive", "proxy-authenticate",
                "proxy-authorization", "te", "trailers",
                "transfer-encoding", "upgrade", "content-length"}
        for k, v in resp.headers.items():
            if k.lower() in skip:
                continue
            self.send_header(k, v)
        # let Python's HTTP server add its own framing; we stream with chunked
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            while True:
                chunk = resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(f"{len(chunk):x}\r\n".encode())
                self.wfile.write(chunk)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            # terminating zero-length chunk
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path.split("?", 1)[0] != "/v1/models":
            self._send_json(404, {"type": "error", "error": {
                "type": "not_found_error", "message": "Not found"}})
            return
        now = int(time.time())
        self._send_json(200, {
            "object": "list",
            "data": [{"id": m, "object": "model", "created": now}
                     for m in self.model_index]
        })


def run_proxy():
    _ProxyHandler.model_index = build_model_index(load_providers())
    socketserver.TCPServer.allow_reuse_address = True
    srv = http.server.ThreadingHTTPServer((PROXY_HOST, PROXY_PORT), _ProxyHandler)
    srv.daemon_threads = True
    sys.stderr.write(f"Proxy on {PROXY_HOST}:{PROXY_PORT} "
                     f"({len(_ProxyHandler.model_index)} models)\n")
    sys.stderr.flush()
    srv.serve_forever()


# ═══════════════════════════════════════════════════════════════════
#  UI helpers
# ═══════════════════════════════════════════════════════════════════
_COLOR = sys.stdout.isatty() and os.environ.get("TERM", "") not in ("", "dumb")

def c(code, text):
    return f"\033[{code}m{text}\033[0m" if _COLOR else text

def bold(t):   return c("1", t)
def dim(t):    return c("2", t)
def green(t):  return c("32", t)
def red(t):    return c("31", t)
def yellow(t): return c("33", t)
def cyan(t):   return c("36", t)

_ANSI = re.compile(r"\033\[[0-9;]*m")

def _vw(s: str) -> int:
    s = _ANSI.sub("", s)
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 0x2E80 else 1
    return w


def clear_screen():
    os.system("clear" if os.name != "nt" else "cls")


def render_box(lines: list[str], width: int = 70, title: str | None = None,
               subtitle: str | None = None):
    top    = "╭" + "─" * (width - 2) + "╮"
    bottom = "╰" + "─" * (width - 2) + "╯"
    sep    = "├" + "─" * (width - 2) + "┤"

    def row(text=""):
        pad = max(0, width - 2 - _vw(text))
        return "│ " + text + " " * pad + "│"

    print(bold(top))
    if title:
        print(row(bold("  " + title)))
    if subtitle:
        print(row(dim("  " + subtitle)))
    if title or subtitle:
        print(sep)
    for ln in lines:
        if ln == "__SEP__":
            print(sep)
        else:
            print(row(ln))
    print(bold(bottom))


def prompt(text: str, default: str = "") -> str:
    suffix = f" [{dim(default)}]" if default else ""
    try:
        v = input(bold(f"  {text}{suffix} ▸ ")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return v or default


def confirm(text: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    v = prompt(f"{text} ({hint})", "")
    if not v:
        return default
    return v.lower() in ("y", "yes")


def pause():
    try:
        input(dim("  回车返回..."))
    except (EOFError, KeyboardInterrupt):
        print()


# ═══════════════════════════════════════════════════════════════════
#  主菜单渲染
# ═══════════════════════════════════════════════════════════════════
def render_main_menu(providers: list[dict]):
    lines: list[str] = []
    items = flat_models(providers)
    last = None
    for i, (model, prov) in enumerate(items, 1):
        if prov is not last:
            if last is not None:
                lines.append("")
            lines.append("  " + c(prov["color"] + ";1", "▎ " + prov["label"]))
            last = prov
        num = c("1;32", f"{i:>2})")
        name = bold(f"{model:<26}")
        host = dim(prov["base_url"])
        lines.append(f"    {num}  {name} {host}")
    if not items:
        lines.append(dim("  (无已配置模型, 按 M 进入管理菜单添加)"))

    lines.append("__SEP__")
    lines.append("   " + c("1;34", " M)") + "  管理提供商 / 模型")
    lines.append("   " + c("1;31", " 0)") + "  退出")

    render_box(
        lines,
        width=70,
        title="Claude Code  ·  模型选择启动器",
        subtitle="选择编号启动 Claude Code,  输入 M 进入管理菜单",
    )


# ═══════════════════════════════════════════════════════════════════
#  管理菜单渲染
# ═══════════════════════════════════════════════════════════════════
def render_manage_menu(providers: list[dict]):
    lines: list[str] = []
    if not providers:
        lines.append(dim("  (尚无提供商)"))
    else:
        for i, p in enumerate(providers, 1):
            num = c("1;32", f"{i:>2})")
            label = c(p["color"] + ";1", "▎ " + p["label"])
            lines.append(f"    {num}  {label}")
            lines.append("        " + dim(p["base_url"])
                         + dim(f"   ({len(p['models'])} 个模型)"))
            lines.append("")
        if lines and lines[-1] == "":
            lines.pop()

    lines.append("__SEP__")
    lines.append("   " + c("1;32", " N)") + "  新增提供商")
    lines.append("   " + c("1;33", " E <编号>)") + dim("   编辑 / 删除, 例如 E 2  或直接输入编号"))
    lines.append("   " + c("1;31", " 0)") + "  返回")

    render_box(
        lines,
        width=70,
        title="提供商管理",
        subtitle=f"配置文件: {CONFIG_PATH}",
    )


def render_provider_editor(prov: dict):
    lines: list[str] = []
    head = c(prov["color"] + ";1", "▎ " + prov["label"])
    lines.append(head)
    lines.append("  " + dim("base_url : ") + prov["base_url"])
    mask = prov["api_key"]
    if len(mask) > 14:
        mask = mask[:8] + "…" + mask[-4:]
    lines.append("  " + dim("api_key  : ") + mask)
    lines.append("  " + dim("color    : ") + c(prov["color"], "■■■") + dim(f"  ({prov['color']})"))
    lines.append("__SEP__")

    if not prov["models"]:
        lines.append(dim("  (无模型)"))
    else:
        for i, m in enumerate(prov["models"], 1):
            num = c("1;32", f"{i:>2})")
            lines.append(f"    {num}  {bold(m)}")

    lines.append("__SEP__")
    lines.append("   " + c("1;32", " A)") + "  新增模型")
    lines.append("   " + c("1;96", " F)") + "  从 API 自动拉取 / 追加模型")
    lines.append("   " + c("1;33", " R <编号>)") + "  删除指定模型")
    lines.append("   " + c("1;36", " M)") + "  修改 label / url / key / color")
    lines.append("   " + c("1;31", " X)") + "  删除此提供商")
    lines.append("   " + c("2",    " 0)") + "  返回")

    render_box(lines, width=72, title="编辑提供商")


def render_color_palette():
    lines: list[str] = []
    for i, (code, name) in enumerate(COLOR_PALETTE, 1):
        swatch = c(code, "■■■")
        lines.append(f"    {i:>2})  {swatch}  {c(code, name)}  " + dim(f"code={code}"))
    render_box(lines, width=56, title="选择颜色")



# ═══════════════════════════════════════════════════════════════════
#  模型发现 — 从 provider 接口拉取可用模型
# ═══════════════════════════════════════════════════════════════════
def fetch_models_from_api(base_url: str, api_key: str, timeout: int = 10):
    """
    Try common /models endpoints. Returns (models, tried) where:
      models : list[str] | None     -- None on total failure
      tried  : list[(url, status_or_err)]  -- diagnostics
    """
    base = base_url.rstrip("/")
    candidates = [base + "/v1/models", base + "/models"]
    if base.endswith("/anthropic"):
        stripped = base[:-len("/anthropic")]
        candidates += [stripped + "/v1/models", stripped + "/models"]

    tried = []
    for url in candidates:
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("x-api-key", api_key)
        req.add_header("anthropic-version", "2023-06-01")
        req.add_header("User-Agent", "Claude-Code/2.1.114")
        try:
            resp = urllib.request.urlopen(req, timeout=timeout)
            raw = resp.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                tried.append((url, f"{resp.status} non-json"))
                continue
            ids = _extract_model_ids(data)
            if ids:
                tried.append((url, f"{resp.status} OK ({len(ids)})"))
                return ids, tried
            tried.append((url, f"{resp.status} no ids"))
        except urllib.error.HTTPError as e:
            tried.append((url, f"HTTP {e.code}"))
        except Exception as e:
            tried.append((url, f"err {type(e).__name__}"))
    return None, tried


def _extract_model_ids(data) -> list:
    # OpenAI / Anthropic style: {"data":[{"id":"..."}]}
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        out = []
        for it in data["data"]:
            if isinstance(it, dict):
                mid = it.get("id") or it.get("model") or it.get("name")
                if isinstance(mid, str):
                    out.append(mid)
        if out:
            return out
    # bare list
    if isinstance(data, list):
        out = []
        for it in data:
            if isinstance(it, str):
                out.append(it)
            elif isinstance(it, dict):
                mid = it.get("id") or it.get("model") or it.get("name")
                if isinstance(mid, str):
                    out.append(mid)
        if out:
            return out
    # {"models":[...]}
    if isinstance(data, dict) and isinstance(data.get("models"), list):
        return _extract_model_ids(data["models"])
    return []


def _parse_index_selection(expr: str, n: int) -> list:
    """Parse '1,3,5-8' -> [0,2,4,5,6,7]. 'a'/'all' -> all. Empty -> []. Returns 0-based indices, sorted unique."""
    expr = expr.strip().lower()
    if not expr:
        return []
    if expr in ("a", "all", "*"):
        return list(range(n))
    out = set()
    for part in re.split(r"[,\s]+", expr):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)-(\d+)", part)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            if a > b:
                a, b = b, a
            for i in range(a, b + 1):
                if 1 <= i <= n:
                    out.add(i - 1)
            continue
        if part.isdigit():
            i = int(part)
            if 1 <= i <= n:
                out.add(i - 1)
            continue
        raise ValueError(f"无法解析片段: {part!r}")
    return sorted(out)


def select_models_from_list(available: list, already: set) -> list:
    """Interactive multi-select. Returns chosen model ids (already-existing ones excluded)."""
    clear_screen()
    lines = []
    lines.append(dim(f"  共 {len(available)} 个模型, 已存在的标 {green('✓')}, 输入编号多选"))
    lines.append("__SEP__")
    for i, m in enumerate(available, 1):
        mark = green(" ✓") if m in already else "  "
        num = c("1;32", f"{i:>2})")
        lines.append(f"    {num} {mark}  {bold(m)}")
    lines.append("__SEP__")
    lines.append("   " + c("1;33", " 支持格式:") + dim("  1,3,5-8   |   a = 全选   |   0 = 取消"))
    render_box(lines, width=70, title="选择要追加的模型")
    print()
    expr = prompt("选择", "0").strip()
    if expr in ("", "0", "q", "Q"):
        return []
    try:
        idxs = _parse_index_selection(expr, len(available))
    except ValueError as e:
        print(red(f"  {e}"))
        pause()
        return []
    chosen = [available[i] for i in idxs if available[i] not in already]
    return chosen


# ═══════════════════════════════════════════════════════════════════
#  管理菜单动作
# ═══════════════════════════════════════════════════════════════════
def pick_color(default_code: str) -> str:
    clear_screen()
    render_color_palette()
    print()
    v = prompt("选择颜色 (编号) 或直接输入 ANSI 码", default_code)
    if v.isdigit() and 1 <= int(v) <= len(COLOR_PALETTE):
        return COLOR_PALETTE[int(v) - 1][0]
    # treat as raw code
    if re.fullmatch(r"\d{1,3}(;\d{1,3})*", v):
        return v
    print(red("  输入无效, 保持原值"))
    return default_code


def action_add_provider(providers: list[dict]):
    clear_screen()
    render_box([
        "  依次输入以下字段, 留空可随时 Ctrl+C 取消:",
        "",
        "   • 显示名     (例: 'MyProv  ·  自家中转')",
        "   • 分组颜色   (可从调色板选, 或自填 ANSI 码)",
        "   • base_url   (http/https, 不带末尾 /)",
        "   • api_key    (x-api-key)",
        "   • 初始模型   (可留空, 后续在编辑页添加)",
    ], width=66, title="新增提供商")
    print()

    label = prompt("显示名")
    if not label:
        print(red("  已取消"))
        pause()
        return
    color = pick_color("36")
    base_url = prompt("base_url").rstrip("/")
    if not re.match(r"^https?://", base_url):
        print(red("  base_url 必须以 http:// 或 https:// 开头"))
        pause()
        return
    api_key = prompt("api_key")
    if not api_key:
        print(red("  api_key 不能为空"))
        pause()
        return
    models: list = []
    if confirm("尝试从该接口自动拉取模型列表?", True):
        print(dim("  · 正在请求 /v1/models ..."))
        got, tried = fetch_models_from_api(base_url, api_key)
        if got:
            print(green(f"  ✓ 拉到 {len(got)} 个模型"))
            chosen = select_models_from_list(got, set())
            models.extend(chosen)
            if chosen:
                print(green(f"  ✓ 选中 {len(chosen)} 个: {', '.join(chosen)}"))
        else:
            print(red("  ✗ 拉取失败, 端点尝试:"))
            for u, s in tried:
                print(dim(f"      · {u}  -> {s}"))
            print(dim("  改为手动输入"))
    if not models:
        raw_models = prompt("初始模型 (逗号或空格分隔, 可留空)")
        models = [m for m in re.split(r"[,\s]+", raw_models) if m]

    providers.append({
        "label":    label,
        "color":    color,
        "base_url": base_url,
        "api_key":  api_key,
        "models":   models,
    })
    save_providers(providers)
    print(green(f"  ✓ 已添加提供商 '{label}' (含 {len(models)} 个模型)"))
    pause()


def action_edit_metadata(prov: dict, providers: list[dict]):
    clear_screen()
    render_provider_editor(prov)
    print()
    print(dim("  每一项直接回车保持不变"))
    new_label = prompt("显示名", prov["label"])
    new_color_in = prompt("颜色 (回车不变,  或输入 'p' 打开调色板)", prov["color"])
    if new_color_in.lower() == "p":
        new_color = pick_color(prov["color"])
    else:
        new_color = new_color_in
    new_url = prompt("base_url", prov["base_url"]).rstrip("/")
    if not re.match(r"^https?://", new_url):
        print(red("  base_url 非法, 未保存"))
        pause()
        return
    new_key = prompt("api_key", prov["api_key"])

    prov["label"]    = new_label
    prov["color"]    = new_color
    prov["base_url"] = new_url
    prov["api_key"]  = new_key
    save_providers(providers)
    print(green("  ✓ 已保存"))
    pause()


def action_fetch_models(prov: dict, providers: list):
    print(dim(f"  · 请求 {prov['base_url']} ..."))
    got, tried = fetch_models_from_api(prov["base_url"], prov["api_key"])
    if not got:
        print(red("  ✗ 拉取失败, 端点尝试:"))
        for u, s in tried:
            print(dim(f"      · {u}  -> {s}"))
        pause()
        return
    already = set(prov["models"])
    chosen = select_models_from_list(got, already)
    if not chosen:
        print(dim("  未选中任何新模型"))
        pause()
        return
    prov["models"].extend(chosen)
    save_providers(providers)
    print(green(f"  ✓ 已追加 {len(chosen)} 个: {', '.join(chosen)}"))
    pause()


def action_add_model(prov: dict, providers: list[dict]):
    raw = prompt("新增模型 id (可一次输入多个, 逗号/空格分隔)")
    new = [m for m in re.split(r"[,\s]+", raw) if m]
    if not new:
        print(dim("  已取消"))
        pause()
        return
    added = []
    for m in new:
        if m in prov["models"]:
            print(yellow(f"  · '{m}' 已存在, 跳过"))
        else:
            prov["models"].append(m)
            added.append(m)
    if added:
        save_providers(providers)
        print(green(f"  ✓ 已新增 {len(added)} 个: {', '.join(added)}"))
    pause()


def action_remove_model(prov: dict, providers: list[dict], idx: int):
    if not (0 <= idx < len(prov["models"])):
        print(red("  无效编号"))
        pause()
        return
    m = prov["models"][idx]
    if not confirm(f"确认删除模型 '{m}' ?", False):
        print(dim("  已取消"))
        pause()
        return
    prov["models"].pop(idx)
    save_providers(providers)
    print(green(f"  ✓ 已删除 '{m}'"))
    pause()


def action_delete_provider(prov: dict, providers: list[dict]) -> bool:
    if not confirm(red(f"确认删除整个提供商 '{prov['label']}' 及其全部模型?"), False):
        print(dim("  已取消"))
        pause()
        return False
    providers.remove(prov)
    save_providers(providers)
    print(green("  ✓ 已删除"))
    pause()
    return True


def provider_editor_loop(prov: dict, providers: list[dict]):
    while True:
        if prov not in providers:
            return
        clear_screen()
        render_provider_editor(prov)
        print()
        v = prompt("操作 (A/F/R <n>/M/X/0)", "0").strip()
        if v in ("0", "q", "Q", ""):
            return
        vu = v.upper()
        if vu == "A":
            action_add_model(prov, providers)
        elif vu == "F":
            action_fetch_models(prov, providers)
        elif vu == "M":
            action_edit_metadata(prov, providers)
        elif vu == "X":
            if action_delete_provider(prov, providers):
                return
        elif vu.startswith("R"):
            rest = v[1:].strip()
            if not rest:
                print(red("  用法: R <编号>"))
                pause()
                continue
            try:
                idx = int(rest) - 1
            except ValueError:
                print(red("  编号必须为数字"))
                pause()
                continue
            action_remove_model(prov, providers, idx)
        else:
            print(red("  未识别命令"))
            pause()


def manage_loop(providers: list[dict]):
    while True:
        clear_screen()
        render_manage_menu(providers)
        print()
        v = prompt("操作 (N / E <n> / <n> / 0)", "0").strip()
        if v in ("0", "q", "Q", ""):
            return
        vu = v.upper()
        if vu == "N":
            action_add_provider(providers)
            continue
        rest = v[1:].strip() if vu.startswith("E") else v
        try:
            idx = int(rest) - 1
        except ValueError:
            print(red("  未识别命令 (N / E <n> / 0)"))
            pause()
            continue
        if not (0 <= idx < len(providers)):
            print(red("  编号超范围"))
            pause()
            continue
        provider_editor_loop(providers[idx], providers)


# ═══════════════════════════════════════════════════════════════════
#  启动 claude
# ═══════════════════════════════════════════════════════════════════
def _load_base_settings() -> dict:
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        print(red(f"  settings.json 解析失败: {e}"))
        sys.exit(1)


def _build_claude_settings(model: str) -> dict:
    s = copy.deepcopy(_load_base_settings())
    env = dict(s.get("env") or {})
    env.update({
        "ANTHROPIC_BASE_URL": PROXY_URL,
        "ANTHROPIC_API_KEY": "proxy",
        "ANTHROPIC_AUTH_TOKEN": "proxy",
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_CUSTOM_MODEL_OPTION": model,
    })
    s["env"] = env
    s["model"] = model
    return s


def start_proxy():
    os.system("pkill -f 'claude-launch.py --proxy' 2>/dev/null")
    os.system("pkill -f claude-proxy 2>/dev/null")
    time.sleep(0.4)
    subprocess.Popen(
        [sys.executable, "-u", SELF_PATH, "--proxy"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    deadline = time.time() + 5
    last = None
    while time.time() < deadline:
        try:
            urllib.request.urlopen(PROXY_URL + "/v1/models", timeout=1).read()
            return
        except Exception as e:
            last = e
            time.sleep(0.2)
    raise RuntimeError(f"proxy not ready: {last}")


def _mru_path() -> str:
    return os.path.join(CONFIG_DIR, "recent_projects.json")


def _load_mru() -> list[str]:
    try:
        with open(_mru_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return [p for p in data if isinstance(p, str) and os.path.isdir(p)]
    except Exception:
        return []


def _save_mru(path: str):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        items = _load_mru()
        path = os.path.abspath(path)
        items = [p for p in items if p != path]
        items.insert(0, path)
        items = items[:10]
        with open(_mru_path(), "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _find_git_root(start: str) -> str | None:
    cur = os.path.abspath(start)
    while True:
        if os.path.isdir(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _looks_like_project(d: str) -> bool:
    """任何下列特征命中即认为是项目根。"""
    if os.path.isdir(os.path.join(d, ".git")):
        return True
    markers = (
        "package.json", "pyproject.toml", "requirements.txt", "Cargo.toml",
        "go.mod", "pom.xml", "build.gradle", "build.gradle.kts",
        "Gemfile", "composer.json", "CMakeLists.txt", "Makefile",
        ".claude", "CLAUDE.md", "README.md",
    )
    return any(os.path.exists(os.path.join(d, m)) for m in markers)


def _auto_detect_project(cwd: str) -> str | None:
    """
    优先级:
    1) cwd 本身是项目根
    2) cwd 在某个 git 仓库里 → 取 git root
    3) cwd 有项目特征 → 用 cwd
    否则返回 None。
    """
    cwd = os.path.abspath(cwd)
    root = _find_git_root(cwd)
    if root:
        return root
    if _looks_like_project(cwd):
        return cwd
    return None


def _resolve_project_dir(cli_dir: str | None) -> str:
    """智能确定项目目录:CLI 参数 > 自动检测 cwd > 交互选择 MRU。"""
    if cli_dir:
        p = os.path.abspath(os.path.expanduser(cli_dir))
        if not os.path.isdir(p):
            print(red(f"  ! 目录不存在: {p}"))
            sys.exit(1)
        return p

    cwd = os.getcwd()
    # 若从 $HOME 或 / 启动,视作"无上下文",进入交互
    sentinel = cwd in (os.path.expanduser("~"), "/", "/root")
    auto = None if sentinel else _auto_detect_project(cwd)
    if auto:
        print(green("  ✓ 自动检测到项目: ") + bold(auto))
        return auto

    # 交互:展示 MRU + 手输
    mru = _load_mru()
    print(dim("\n  未检测到项目上下文,请选择或输入目录:"))
    for i, p in enumerate(mru, 1):
        tag = dim(" (git)") if os.path.isdir(os.path.join(p, ".git")) else ""
        print(f"    {i}) {p}{tag}")
    print(f"    N) 新目录路径")
    print(f"    .) 使用当前 cwd ({cwd})")
    try:
        raw = input("  选择: ").strip()
    except EOFError:
        raw = "."
    if raw == "." or raw == "":
        return cwd
    if raw.isdigit() and 1 <= int(raw) <= len(mru):
        return mru[int(raw) - 1]
    # 作为路径
    p = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isdir(p):
        try:
            os.makedirs(p, exist_ok=True)
            print(green(f"  ✓ 已创建目录: {p}"))
        except Exception as e:
            print(red(f"  ! 创建目录失败: {e}"))
            sys.exit(1)
    return p


def _ensure_git_repo(project_dir: str):
    """确保目录是 git 仓库;若不是则 init + 初次提交,便于 worktree/diff 类 hook 立即工作。"""
    if os.path.isdir(os.path.join(project_dir, ".git")):
        return
    try:
        subprocess.run(["git", "init", "-q"], cwd=project_dir, check=False)
        # 基础 .gitignore(若不存在)
        gi = os.path.join(project_dir, ".gitignore")
        if not os.path.exists(gi):
            with open(gi, "w", encoding="utf-8") as f:
                f.write("node_modules/\n__pycache__/\n*.pyc\n.venv/\nvenv/\n"
                        "dist/\nbuild/\n.DS_Store\n.env\n.env.local\n"
                        ".claude/cache/\n")
        subprocess.run(["git", "add", "-A"], cwd=project_dir, check=False)
        subprocess.run(
            ["git", "-c", "user.email=claude@local",
             "-c", "user.name=claude",
             "commit", "-q", "--allow-empty", "-m", "chore: init by claude-launch"],
            cwd=project_dir, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print(green("  ✓ git init 完成 ") + dim("(baseline commit)"))
    except Exception as e:
        print(red(f"  ! git init 失败: {e}"))


def _ensure_git_identity(project_dir: str):
    """若该仓库未设 user.email/name,设个本地默认,避免 commit/worktree hook 报错。"""
    try:
        for key, default in (("user.email", "claude@local"), ("user.name", "claude")):
            r = subprocess.run(["git", "config", "--get", key],
                               cwd=project_dir, capture_output=True, text=True)
            if not r.stdout.strip():
                subprocess.run(["git", "config", key, default], cwd=project_dir, check=False)
    except Exception:
        pass


def _ensure_claude_mem_worker():
    """确保 claude-mem worker 在运行(忽略失败)。"""
    try:
        r = subprocess.run(["npx", "claude-mem", "status"],
                           capture_output=True, text=True, timeout=5)
        if "running" in (r.stdout + r.stderr).lower():
            return
    except Exception:
        pass
    try:
        subprocess.Popen(
            ["npx", "claude-mem", "start"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            preexec_fn=os.setsid,
        )
        print(dim("  · claude-mem worker 已后台启动"))
    except Exception:
        pass


def exec_claude(model: str, project_dir: str | None = None):
    settings_arg = json.dumps(
        _build_claude_settings(model),
        ensure_ascii=False, separators=(",", ":"))
    env = os.environ.copy()
    env.update({
        "ANTHROPIC_BASE_URL": PROXY_URL,
        "ANTHROPIC_API_KEY": "proxy",
        "ANTHROPIC_AUTH_TOKEN": "proxy",
        "ANTHROPIC_MODEL": model,
        "ANTHROPIC_CUSTOM_MODEL_OPTION": model,
    })
    project_dir = _resolve_project_dir(project_dir)
    try:
        os.chdir(project_dir)
        print(green("  ✓ cwd = ") + bold(project_dir))
    except OSError as e:
        print(red(f"  ! chdir 失败: {e}"))
        sys.exit(1)
    _ensure_git_repo(project_dir)
    _ensure_git_identity(project_dir)
    _ensure_claude_mem_worker()
    _save_mru(project_dir)
    os.execvpe("claude", ["claude", "--settings", settings_arg], env)


# ═══════════════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════════════
def cmd_list():
    for p in load_providers():
        print(f"[{p['label']}]  {p['base_url']}")
        for m in p["models"]:
            print(f"  - {m}")


def cmd_menu(cli_dir: str | None = None):
    while True:
        providers = load_providers()
        clear_screen()
        render_main_menu(providers)
        print()
        v = prompt("请选择", "0").strip()
        if v in ("0", "q", "Q", ""):
            print(dim("  已退出"))
            return
        if v.upper() == "M":
            manage_loop(providers)
            continue
        try:
            idx = int(v) - 1
        except ValueError:
            print(red("  请输入数字 / M / 0"))
            pause()
            continue
        items = flat_models(providers)
        if not (0 <= idx < len(items)):
            print(red("  无效编号"))
            pause()
            continue

        model = items[idx][0]
        print()
        print(dim("  · 启动本地代理 ..."))
        try:
            start_proxy()
            print(green("  ✓ proxy 就绪  ") + dim(f"({PROXY_HOST}:{PROXY_PORT})"))
        except Exception as e:
            print(red(f"  ✗ proxy 启动失败: {e}"))
            sys.exit(1)
        print(dim("  · 启动 Claude Code  (model = ") + bold(model) + dim(")"))
        print()
        exec_claude(model, cli_dir)


def main():
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--proxy",  action="store_true", help="run as reverse proxy")
    ap.add_argument("--list",   action="store_true", help="list providers/models")
    ap.add_argument("--manage", action="store_true", help="jump into manager")
    ap.add_argument("--cwd", "-C", help="project directory to launch Claude in")
    args = ap.parse_args()

    if args.proxy:
        run_proxy()
    elif args.list:
        cmd_list()
    elif args.manage:
        manage_loop(load_providers())
    else:
        cmd_menu(args.cwd)


if __name__ == "__main__":
    main()
