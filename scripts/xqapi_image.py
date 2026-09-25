#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xqapi-image — xqapi.com 图像生成 CLI

对齐 OpenAI /v1/images/generations 与 /v1/images/edits 形状，
额外封装站点自有能力：resolution 档位、async 任务轮询、600s 超时 504 回退、
b64_json 内联落地。

只用标准库，无第三方依赖。

用法速查
--------
文生图:
    python xqapi_image.py gen "一座未来城市" -m gpt-image-2 -r 2k -o out/city.png
    python xqapi_image.py gen "赛博朋克海报" -r 4k --async --outdir out/
图生图:
    python xqapi_image.py edit "把背景换成雪山" -i cat.png -i style.png -r 2k
    python xqapi_image.py edit "局部重绘" -i cat.png --mask m.png
URL 参考图(JSON 路):
    python xqapi_image.py edit "换背景" --image-url https://example.com/cat.png
目录内 URL 批量:
    python xqapi_image.py edit "统一加雪景" --image-urls a.png.url b.png.url
参数探测:
    python xqapi_image.py probe -m gpt-image-2
任务查询:
    python xqapi_image.py task <task_id>
"""

from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

# ----------------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------------

DEFAULT_BASE = "https://xqapi.com/v1"

# 与官方 SDK 默认请求超时一致
SYNC_TIMEOUT = 600.0

# async 轮询参数
POLL_INTERVAL = 3.0
POLL_MAX_WAIT = 3600.0

# 站点硬限制
MAX_REF_IMAGES = 16

RESOLUTIONS = ("1k", "2k", "4k")

# ---------------------------------------------------------------------------
# UA —— 实测必需，不是可选项
#
# 不带 User-Agent 头会被站点前置的 Cloudflare 拦掉：
#   HTTP 403  Error 1010: Access denied
# 带任意"像样"的 UA（脚本名或浏览器 UA）都能通过，不需要伪装成浏览器。
# 但 Python-urllib 默认 UA 会触发 SSL 层异常，所以必须显式设置。
# 默认给浏览器 UA 最稳。
# ---------------------------------------------------------------------------
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# 图片下载单独超时。实测 2.5MB 的 1024x1024 PNG 走了近 70s，
# 默认 120s 对 4k 大图偏紧。
DOWNLOAD_TIMEOUT = 300.0


class XqapiError(RuntimeError):
    """API 返回的错误。"""

    def __init__(self, status: int, body: str, url: str = ""):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} @ {url}: {body[:500]}")


class Timeout504(XqapiError):
    """同步等待超过 600s，站点按超时返回 504。"""


# ----------------------------------------------------------------------------
# 凭据
# ----------------------------------------------------------------------------


def load_api_key(cli_value: str | None) -> str:
    """优先级：命令行 > 环境变量 > ~/.dsh/xqapi.key > ~/.xqapi.key"""
    if cli_value:
        return cli_value.strip()

    for var in ("XQAPI_API_KEY", "XQAPI_KEY"):
        v = os.environ.get(var)
        if v:
            return v.strip()

    for p in (
        Path.home() / ".dsh" / "xqapi.key",
        Path.home() / ".xqapi.key",
    ):
        if p.is_file():
            txt = p.read_text(encoding="utf-8").strip()
            if txt:
                return txt

    die(
        "未找到 API Key。请任选一种方式提供：\n"
        "  1) --api-key sk-xxx\n"
        "  2) 环境变量 XQAPI_API_KEY\n"
        "  3) 写入 %USERPROFILE%\\.dsh\\xqapi.key"
    )
    raise SystemExit(2)  # unreachable


def die(msg: str, code: int = 2) -> None:
    print(f"[xqapi-image] 错误: {msg}", file=sys.stderr)
    raise SystemExit(code)


# ----------------------------------------------------------------------------
# multipart 编码（标准库，无依赖）
# ----------------------------------------------------------------------------


def _encode_multipart(
    fields: dict[str, str],
    files: list[tuple[str, Path]],
) -> tuple[bytes, str]:
    """
    fields: 普通文本字段
    files:  [(form_field_name, path), ...]  同名可重复（image 字段多图）
    """
    boundary = f"----xqapi{uuid.uuid4().hex}"
    crlf = b"\r\n"
    buf = bytearray()

    for name, value in fields.items():
        if value is None:
            continue
        buf += b"--" + boundary.encode() + crlf
        buf += f'Content-Disposition: form-data; name="{name}"'.encode() + crlf
        buf += crlf
        buf += str(value).encode("utf-8") + crlf

    for name, path in files:
        if not path.is_file():
            die(f"参考图不存在: {path}")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        buf += b"--" + boundary.encode() + crlf
        buf += (
            f'Content-Disposition: form-data; name="{name}"; '
            f'filename="{path.name}"'
        ).encode() + crlf
        buf += f"Content-Type: {ctype}".encode() + crlf
        buf += crlf
        buf += path.read_bytes() + crlf

    buf += b"--" + boundary.encode() + b"--" + crlf
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------


def resolve_ua(cli_value: str | None = None) -> str:
    """UA 解析。优先级：命令行 > 环境变量 > 内置浏览器 UA。"""
    if cli_value:
        return cli_value.strip()
    v = os.environ.get("XQAPI_USER_AGENT")
    if v:
        return v.strip()
    return DEFAULT_UA


def _request(
    url: str,
    api_key: str,
    *,
    json_body: dict | None = None,
    multipart: tuple[bytes, str] | None = None,
    timeout: float = SYNC_TIMEOUT,
    ua: str | None = None,
) -> tuple[int, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        # 缺了这个头会被 Cloudflare 以 1010 拒绝
        "User-Agent": ua or DEFAULT_UA,
    }

    data: bytes | None = None
    if multipart is not None:
        data, ctype = multipart
        headers["Content-Type"] = ctype
    elif json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, body
    except urllib.error.URLError as e:
        # 本地超时 / 连接错误。urllib 超时抛 URLError(socket.timeout)
        reason = getattr(e, "reason", e)
        if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower():
            return 504, json.dumps(
                {"error": {"message": f"客户端等待超过 {timeout}s", "type": "client_timeout"}}
            )
        return 0, json.dumps({"error": {"message": f"网络错误: {reason}"}})


def _download(url: str, dest: Path, timeout: float = DOWNLOAD_TIMEOUT,
              ua: str | None = None, retries: int = 3) -> Path:
    """
    下载直链到本地。带重试 + 断点续传。

    实测该站直链在传输中途会被掐断（http.client.IncompleteRead），
    单次下载不可靠，必须重试；已落盘的部分用 HTTP Range 续传，
    避免每次从头重拉 2.5MB。
    """
    ctx = ssl.create_default_context()
    last_err: Exception | None = None

    for attempt in range(1, retries + 1):
        have = dest.stat().st_size if dest.exists() else 0
        headers = {"User-Agent": ua or DEFAULT_UA}
        if have:
            headers["Range"] = f"bytes={have}-"

        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                code = resp.status
                total = resp.headers.get("Content-Length")
                total = int(total) if total and total.isdigit() else None

                # 服务端不支持 Range 会回 200 全量，此时必须从头写
                mode = "ab" if (have and code == 206) else "wb"
                if mode == "wb":
                    have = 0

                with open(dest, mode) as f:
                    while True:
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        f.write(chunk)

            got = dest.stat().st_size
            if total is not None and have + total != got:
                raise IOError(f"长度不符: 期望 {have + total}, 实际 {got}")
            return dest

        except Exception as e:
            last_err = e
            got = dest.stat().st_size if dest.exists() else 0
            print(
                f"[xqapi-image] 下载失败 ({attempt}/{retries}): "
                f"{type(e).__name__}  已落盘 {got} 字节，续传中…",
                file=sys.stderr,
            )
            if attempt < retries:
                time.sleep(1.5 * attempt)

    die(f"下载重试 {retries} 次仍失败: {last_err}")
    raise SystemExit(2)  # unreachable


# ----------------------------------------------------------------------------
# 响应落地
# ----------------------------------------------------------------------------


def _guess_ext(fmt: str | None, url: str = "") -> str:
    if fmt:
        f = fmt.lower().lstrip(".")
        if f in ("jpg", "jpeg"):
            return ".jpg"
        if f in ("png", "webp"):
            return f".{f}"
    if url:
        suffix = Path(url.split("?")[0]).suffix.lower()
        if suffix in (".png", ".jpg", ".jpeg", ".webp"):
            return ".jpg" if suffix == ".jpeg" else suffix
    return ".png"


def save_results(
    payload: dict,
    outdir: Path,
    *,
    prefix: str = "img",
    out: Path | None = None,
    ua: str | None = None,
    quiet: bool = False,
) -> list[Path]:
    """
    把响应里的 data[] 全部落地。返回写出文件列表。
    支持两种：{"url": ...} 直链下载；{"b64_json": ...} 内联解码。
    """
    data = payload.get("data") or []
    if not data:
        die(f"响应中没有 data[]: {json.dumps(payload, ensure_ascii=False)[:500]}")

    outdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    for idx, item in enumerate(data):
        if out is not None and len(data) == 1:
            dest = out
        else:
            fmt = item.get("output_format") or payload.get("output_format")
            ext = _guess_ext(fmt, item.get("url", ""))
            stamp = time.strftime("%Y%m%d-%H%M%S")
            dest = outdir / f"{prefix}-{stamp}-{idx}{ext}"

        dest.parent.mkdir(parents=True, exist_ok=True)

        if item.get("b64_json"):
            dest.write_bytes(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            # 直链下载。实测这一步可能很慢（2.5MB 走了近 70s），单独计时。
            t0 = time.monotonic()
            _download(item["url"], dest, ua=ua)
            if not quiet:
                print(
                    f"  下载 {time.monotonic() - t0:.1f}s  "
                    f"{dest.stat().st_size / 1024 / 1024:.2f} MB",
                    file=sys.stderr,
                )
        else:
            print(
                f"[xqapi-image] 警告: data[{idx}] 既无 url 也无 b64_json，跳过",
                file=sys.stderr,
            )
            continue

        written.append(dest)
        print(f"  写出: {dest}  ({dest.stat().st_size} bytes)")

    usage = payload.get("usage")
    if usage:
        print(f"  usage: {json.dumps(usage, ensure_ascii=False)}")

    return written


# ----------------------------------------------------------------------------
# 调用核心：同步 + 600s 504 回退 async
# ----------------------------------------------------------------------------


def _extract_task_id(payload: dict) -> str | None:
    for k in ("task_id", "id", "taskId"):
        v = payload.get(k)
        if isinstance(v, str) and v:
            return v
    for k in ("data", "task"):
        v = payload.get(k)
        if isinstance(v, dict):
            for kk in ("task_id", "id"):
                if isinstance(v.get(kk), str):
                    return v[kk]
    return None


def _looks_like_task(payload: dict) -> bool:
    """async 响应识别：有 task_id 且无 data[] 或 data 非图片列表。"""
    if _extract_task_id(payload) is None:
        return False
    data = payload.get("data")
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if "url" in data[0] or "b64_json" in data[0]:
            return False
    return True


def poll_task(
    base: str,
    api_key: str,
    task_id: str,
    *,
    interval: float = POLL_INTERVAL,
    max_wait: float = POLL_MAX_WAIT,
    quiet: bool = False,
    ua: str | None = None,
) -> dict:
    """轮询异步任务直到出图 / 失败 / 超时。"""
    deadline = time.monotonic() + max_wait
    attempt = 0

    while True:
        attempt += 1
        if time.monotonic() > deadline:
            die(f"异步任务 {task_id} 轮询超过 {max_wait}s 仍未完成")

        url = f"{base}/images/tasks/{task_id}"
        t0 = time.monotonic()
        status, body = _request(url, api_key, json_body=None, timeout=60.0, ua=ua)
        elapsed = time.monotonic() - t0

        # GET 语义：这里用 POST 空体查任务，若站点要 GET 会 405，
        # 回退到 GET 形式。
        if status in (404, 405):
            status, body = _get(url, api_key, timeout=60.0, ua=ua)

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            die(f"任务查询返回非 JSON (HTTP {status}): {body[:300]}")

        if status >= 400 and status not in (404, 405):
            raise XqapiError(status, body, url)

        state = str(
            payload.get("status")
            or payload.get("state")
            or (payload.get("data") or {}).get("status")
            or ""
        ).lower()

        if not quiet:
            print(
                f"  [poll #{attempt}] status={state or '?'} "
                f"({elapsed:.1f}s) task={task_id}",
                file=sys.stderr,
            )

        if state in ("succeeded", "success", "completed", "done", "finished"):
            return payload
        if state in ("failed", "error", "canceled", "cancelled"):
            die(f"异步任务失败: {json.dumps(payload, ensure_ascii=False)[:500]}")

        # 有些实现直接在轮询响应里返回 data[]
        data = payload.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            if "url" in data[0] or "b64_json" in data[0]:
                return payload

        time.sleep(interval)


def _get(url: str, api_key: str, timeout: float = 60.0,
         ua: str | None = None) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Accept": "application/json",
            "User-Agent": ua or DEFAULT_UA,
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return 0, json.dumps({"error": {"message": str(getattr(e, "reason", e))}})


def call_image_api(
    base: str,
    api_key: str,
    endpoint: str,
    *,
    json_body: dict | None = None,
    multipart: tuple[bytes, str] | None = None,
    use_async: bool = False,
    timeout: float = SYNC_TIMEOUT,
    ua: str | None = None,
) -> dict:
    """
    统一入口。返回最终含 data[] 的 payload。

    use_async=False: 同步等，600s 上限；若拿到 504 或识别出任务 ID，
                     自动降级为轮询（除非 --no-async-fallback）。
    use_async=True : 请求里带 async:true，直接拿任务 ID 轮询。
    """
    url = f"{base}/{endpoint}"
    body = dict(json_body) if json_body else None

    if use_async and body is not None:
        body["async"] = True
        print("[xqapi-image] 异步模式：申请任务 ID", file=sys.stderr)

    print(f"[xqapi-image] POST {url}", file=sys.stderr)
    t0 = time.monotonic()
    status, text = _request(
        url, api_key, json_body=body, multipart=multipart, timeout=timeout, ua=ua
    )
    elapsed = time.monotonic() - t0
    print(f"[xqapi-image] <- HTTP {status}  ({elapsed:.1f}s)", file=sys.stderr)

    # 504：同步超时
    if status == 504:
        if use_async:
            die("异步任务申请阶段就超时了，请重试或检查站点状态")
        raise Timeout504(504, text, url)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        die(f"非 JSON 响应 (HTTP {status}): {text[:300]}")

    if status >= 400:
        msg = json.dumps(payload, ensure_ascii=False)
        # Cloudflare 1010：UA 缺失或不被接受。这是最容易被误判成"Key 无效"的坑。
        if status == 403 or "1010" in msg:
            die(
                "被 Cloudflare 拒绝 (HTTP 403 / Error 1010)。\n"
                "  原因：User-Agent 缺失或不被接受——站点把 UA 当硬性要求。\n"
                "  处置：本脚本默认已带浏览器 UA；若你覆盖过，去掉 --user-agent，\n"
                "        或显式给一个像样的值：--user-agent 'Mozilla/5.0 ...'",
                3,
            )
        raise XqapiError(status, msg, url)

    # 拿到任务 ID -> 轮询
    if _looks_like_task(payload):
        task_id = _extract_task_id(payload)
        print(f"[xqapi-image] 异步任务: {task_id}", file=sys.stderr)
        return poll_task(base, api_key, task_id,
                         interval=POLL_INTERVAL, max_wait=POLL_MAX_WAIT, ua=ua)

    return payload


# ----------------------------------------------------------------------------
# 子命令
# ----------------------------------------------------------------------------


def _common_opts(p: argparse.ArgumentParser) -> None:
    p.add_argument("-m", "--model", required=True, help="图像模型名（见站点模型详情页）")
    p.add_argument("-r", "--resolution", choices=RESOLUTIONS, type=str.lower,
                   help="尺寸档位 1k/2k/4k（大小写不敏感）")
    p.add_argument("-q", "--quality", help="quality 取值以模型详情页为准")
    p.add_argument("-s", "--size", help="官方像素串，如 1024x1024；取值以模型详情页为准")
    p.add_argument("-n", "--count", type=int, help="生成张数")
    p.add_argument("-f", "--output-format", help="png / jpeg / webp，取值以模型详情页为准")
    p.add_argument("--b64-json", action="store_true",
                   help="response_format=b64_json，走内联 base64 而非托管直链")
    p.add_argument("--async", dest="use_async", action="store_true",
                   help="带 async:true，拿任务 ID 自己轮询（绕开 600s 上限）")
    p.add_argument("--timeout", type=float, default=SYNC_TIMEOUT,
                   help=f"同步等待秒数，默认 {SYNC_TIMEOUT:.0f}（与官方 SDK 一致）")
    p.add_argument("--poll-interval", type=float, default=POLL_INTERVAL)
    p.add_argument("--poll-max-wait", type=float, default=POLL_MAX_WAIT)
    p.add_argument("--extra", action="append", default=[], metavar="K=V",
                   help="透传额外字段，可重复；值自动识别 JSON/数字/字符串")
    p.add_argument("--api-key", help="覆盖 API Key")
    p.add_argument("--base", default=os.environ.get("XQAPI_BASE_URL", DEFAULT_BASE),
                   help=f"API 根地址，默认 {DEFAULT_BASE}")
    p.add_argument("--user-agent", "--ua", dest="user_agent",
                   help="覆盖 User-Agent。**缺 UA 会被 Cloudflare 403 拒绝**，"
                        "默认已带浏览器 UA，一般不用改")
    p.add_argument("-o", "--out", help="单图时指定完整输出路径")
    p.add_argument("--outdir", default="out", help="多图输出目录，默认 out/")
    p.add_argument("--json", dest="dump_json", action="store_true",
                   help="额外打印原始响应 JSON")


def _apply_extra(body: dict, extras: list[str]) -> None:
    for kv in extras:
        if "=" not in kv:
            die(f"--extra 需要 K=V 形式，收到: {kv}")
        k, v = kv.split("=", 1)
        try:
            body[k] = json.loads(v)
        except json.JSONDecodeError:
            body[k] = v


def _fill_resolution(body: dict, res: str | None) -> None:
    if res:
        body["resolution"] = res.lower()


def cmd_gen(args: argparse.Namespace) -> int:
    api_key = load_api_key(args.api_key)
    ua = resolve_ua(args.user_agent)

    body: dict = {"model": args.model, "prompt": args.prompt}
    _fill_resolution(body, args.resolution)
    if args.quality:
        body["quality"] = args.quality
    if args.size:
        body["size"] = args.size
    if args.count:
        body["n"] = args.count
    if args.output_format:
        body["output_format"] = args.output_format
    if args.b64_json:
        body["response_format"] = "b64_json"
    _apply_extra(body, args.extra)

    payload = call_image_api(
        args.base, api_key, "images/generations",
        json_body=body, use_async=args.use_async, timeout=args.timeout, ua=ua,
    )

    if args.dump_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    files = save_results(
        payload, Path(args.outdir),
        prefix="gen", out=Path(args.out) if args.out else None, ua=ua,
    )
    print(f"\n[xqapi-image] 完成，{len(files)} 张图")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    api_key = load_api_key(args.api_key)
    ua = resolve_ua(args.user_agent)

    imgs: list[Path] = [Path(p) for p in (args.image or [])]
    if len(imgs) > MAX_REF_IMAGES:
        die(f"参考图最多 {MAX_REF_IMAGES} 张，收到 {len(imgs)} 张")

    use_json_route = bool(args.image_url)

    if use_json_route and imgs:
        die("参考图两种给法互斥：本地文件(-i) 与 --image-url 不能同时用")

    if not imgs and not args.image_url:
        die("图生图至少需要一张参考图：-i <file> 或 --image-url <url>")

    common = {
        "model": args.model,
        "prompt": args.prompt,
    }
    _fill_resolution(common, args.resolution)
    if args.quality:
        common["quality"] = args.quality
    if args.size:
        common["size"] = args.size
    if args.output_format:
        common["output_format"] = args.output_format
    if args.b64_json:
        common["response_format"] = "b64_json"

    if use_json_route:
        # JSON 路：images[] 每项二选一 image_url / file_id
        items = [{"image_url": u} for u in args.image_url]
        body = dict(common)
        body["images"] = items
        if args.image_urls:
            body["image_urls"] = list(args.image_urls)
        _apply_extra(body, args.extra)

        print(f"[xqapi-image] JSON 路，{len(items)} 张 URL 参考图", file=sys.stderr)
        payload = call_image_api(
            args.base, api_key, "images/edits",
            json_body=body, use_async=args.use_async, timeout=args.timeout, ua=ua,
        )
    else:
        # multipart 路：重复 image 字段
        fields = dict(common)
        _apply_extra(fields, args.extra)
        files: list[tuple[str, Path]] = [("image", p) for p in imgs]
        if args.mask:
            files.append(("mask", Path(args.mask)))
        mp = _encode_multipart(fields, files)

        print(
            f"[xqapi-image] multipart 路，{len(imgs)} 张参考图"
            + (" + mask" if args.mask else ""),
            file=sys.stderr,
        )
        payload = call_image_api(
            args.base, api_key, "images/edits",
            multipart=mp, use_async=args.use_async, timeout=args.timeout, ua=ua,
        )

    if args.dump_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    out_files = save_results(
        payload, Path(args.outdir),
        prefix="edit", out=Path(args.out) if args.out else None, ua=ua,
    )
    print(f"\n[xqapi-image] 完成，{len(out_files)} 张图")
    return 0


def cmd_task(args: argparse.Namespace) -> int:
    api_key = load_api_key(args.api_key)
    ua = resolve_ua(getattr(args, "user_agent", None))
    payload = poll_task(
        args.base, api_key, args.task_id,
        interval=args.poll_interval, max_wait=args.poll_max_wait, ua=ua,
    )
    if args.dump_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload.get("data"):
        save_results(payload, Path(args.outdir), prefix="task",
                     out=Path(args.out) if args.out else None, ua=ua)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    """
    参数探测：逐档试 resolution，记录哪些被接受。
    会真实产生费用，仅在你明确要摸底时用。
    """
    api_key = load_api_key(args.api_key)
    ua = resolve_ua(getattr(args, "user_agent", None))
    results: dict[str, str] = {}

    for res in RESOLUTIONS:
        body = {"model": args.model, "prompt": args.prompt, "resolution": res}
        print(f"\n=== 探测 resolution={res} ===", file=sys.stderr)
        try:
            payload = call_image_api(
                args.base, api_key, "images/generations",
                json_body=body, use_async=True, timeout=args.timeout, ua=ua,
            )
            ok = bool(payload.get("data"))
            results[res] = "OK" if ok else "OK(无 data)"
        except XqapiError as e:
            results[res] = f"拒绝 HTTP {e.status}: {e.body[:200]}"

    print("\n[xqapi-image] resolution 探测结果:")
    for k, v in results.items():
        print(f"  {k}: {v}")
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    """
    列出可用模型。文档示例里的模型名多为占位符，照抄会吃
    {'message': '模型不存在或未启用。'}，所以先查再调。
    """
    api_key = load_api_key(args.api_key)
    ua = resolve_ua(getattr(args, "user_agent", None))
    url = f"{args.base}/models"
    status, text = _get(url, api_key, timeout=60.0, ua=ua)

    if status >= 400:
        die(f"查询模型列表失败 HTTP {status}: {text[:300]}", 3)

    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        die(f"模型列表返回非 JSON: {text[:300]}", 3)

    items = payload.get("data") or []
    if not items:
        die("模型列表为空")

    # 关键词粗筛图像模型
    HINTS = ("image", "img", "dall", "flux", "sd", "seedream", "qwen-image",
             "nano", "banana", "vision")

    print(f"共 {len(items)} 个模型:\n")
    image_like = []
    for m in items:
        mid = m.get("id", "?") if isinstance(m, dict) else str(m)
        low = mid.lower()
        hit = any(h in low for h in HINTS)
        print(f"  {'*' if hit else ' '} {mid}")
        if hit:
            image_like.append(mid)

    if image_like:
        print(f"\n疑似图像/视觉模型 ({len(image_like)}):")
        for m in image_like:
            print(f"  {m}")

    if args.json:
        print("\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xqapi-image",
        description="xqapi.com 图像生成 CLI（文生图 / 图生图）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    g = sub.add_parser("gen", help="文生图 POST /v1/images/generations")
    g.add_argument("prompt", help="提示词")
    _common_opts(g)
    g.set_defaults(func=cmd_gen)

    e = sub.add_parser("edit", help="图生图/编辑 POST /v1/images/edits")
    e.add_argument("prompt", help="改图指令")
    e.add_argument("-i", "--image", action="append", default=[],
                   help=f"本地参考图，可重复，最多 {MAX_REF_IMAGES} 张（multipart 路）")
    e.add_argument("--mask", help="局部重绘蒙版（multipart 路）")
    e.add_argument("--image-url", action="append", default=[],
                   help="参考图直链，可重复（JSON 路，与 -i 互斥）")
    e.add_argument("--image-urls", nargs="+", default=[],
                   help="扩展字段 image_urls，直接给一组 URL")
    _common_opts(e)
    e.set_defaults(func=cmd_edit)

    t = sub.add_parser("task", help="查询/继续等待异步任务")
    t.add_argument("task_id")
    t.add_argument("--poll-interval", type=float, default=POLL_INTERVAL)
    t.add_argument("--poll-max-wait", type=float, default=POLL_MAX_WAIT)
    t.add_argument("--api-key")
    t.add_argument("--base", default=os.environ.get("XQAPI_BASE_URL", DEFAULT_BASE))
    t.add_argument("--user-agent", "--ua", dest="user_agent")
    t.add_argument("-o", "--out")
    t.add_argument("--outdir", default="out")
    t.add_argument("--json", dest="dump_json", action="store_true")
    t.set_defaults(func=cmd_task)

    pr = sub.add_parser("probe", help="探测模型支持的 resolution 档位（会产生费用）")
    pr.add_argument("-m", "--model", required=True)
    pr.add_argument("-p", "--prompt", default="a red apple on a table")
    pr.add_argument("--timeout", type=float, default=SYNC_TIMEOUT)
    pr.add_argument("--api-key")
    pr.add_argument("--base", default=os.environ.get("XQAPI_BASE_URL", DEFAULT_BASE))
    pr.add_argument("--user-agent", "--ua", dest="user_agent")
    pr.set_defaults(func=cmd_probe)

    md = sub.add_parser("models", help="列出可用模型（先查再调，别照抄文档占位符）")
    md.add_argument("--api-key")
    md.add_argument("--base", default=os.environ.get("XQAPI_BASE_URL", DEFAULT_BASE))
    md.add_argument("--user-agent", "--ua", dest="user_agent")
    md.add_argument("--json", action="store_true")
    md.set_defaults(func=cmd_models)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Timeout504 as e:
        print(
            "\n[xqapi-image] 同步等待超过上限（504）。\n"
            "  建议：重跑时加 --async，或调大 --timeout / --poll-max-wait。",
            file=sys.stderr,
        )
        return 4
    except XqapiError as e:
        print(f"\n[xqapi-image] API 错误: {e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\n[xqapi-image] 已中断", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
