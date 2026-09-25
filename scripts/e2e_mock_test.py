# -*- coding: utf-8 -*-
"""
端到端自测：起一个本地 mock HTTP 服务，模拟 xqapi 的三种行为，
验证 CLI 的同步直出 / async 轮询 / 504 超时三条链路。

不触碰真实 API，不产生费用。
"""
import json
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
CLI = HERE / "xqapi_image.py"

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)

STATE = {"polls": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        raw = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        ctype = self.headers.get("Content-Type", "")

        # ---- 模式 A: 同步直出 URL ----
        if self.path.endswith("/images/generations"):
            body = {}
            if ctype.startswith("application/json"):
                body = json.loads(raw)
            if body.get("model") == "mock-slow":
                self._json(504, {"error": {"message": "gateway timeout"}})
                return
            if body.get("async") is True:
                STATE["polls"] = 0
                self._json(200, {"task_id": "task-123", "status": "pending"})
                return
            self._json(200, {
                "created": 1,
                "data": [{"url": f"http://127.0.0.1:{PORT}/img/0.png"}],
                "usage": {"total_tokens": 5},
            })
            return

        # ---- 模式 B: 异步任务查询 ----
        if "/images/tasks/" in self.path:
            STATE["polls"] += 1
            if STATE["polls"] < 3:
                self._json(200, {"status": "processing", "task_id": "task-123"})
            else:
                self._json(200, {
                    "status": "succeeded",
                    "task_id": "task-123",
                    "data": [{"url": f"http://127.0.0.1:{PORT}/img/0.png"}],
                    "usage": {"total_tokens": 7},
                })
            return

        # ---- 模式 C: 图生图 multipart ----
        if self.path.endswith("/images/edits"):
            is_mp = ctype.startswith("multipart/form-data")
            n_img = raw.count(b'name="image"')
            self._json(200, {
                "created": 2,
                "data": [{"b64_json": __import__("base64").b64encode(PNG).decode()}],
                "usage": {"total_tokens": 9, "route": "multipart" if is_mp else "json",
                          "ref_images": n_img},
            })
            return

        self._json(404, {"error": {"message": "not found"}})

    def do_GET(self):
        if self.path.startswith("/img/"):
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(PNG)))
            self.end_headers()
            self.wfile.write(PNG)
            return
        self._json(404, {"error": {"message": "nf"}})


PORT = 0
fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)


def run(args, env_extra=None):
    import os
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["XQAPI_API_KEY"] = "test-key"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(CLI)] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=120,
    )


srv = HTTPServer(("127.0.0.1", 0), Handler)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
print(f"mock server on 127.0.0.1:{PORT}\n")

with tempfile.TemporaryDirectory() as td:
    td = Path(td)

    print("== 链路1: 同步直出 URL ==")
    r = run(["gen", "a cat", "-m", "mock", "--base", f"http://127.0.0.1:{PORT}/v1",
             "--outdir", str(td / "a")])
    check("退出码 0", r.returncode == 0, r.stderr[-400:] if r.returncode else "")
    files = list((td / "a").glob("*.png"))
    check("下载落地 1 张", len(files) == 1, str(files))
    check("内容与源一致", files and files[0].read_bytes() == PNG)
    check("打印 usage", "total_tokens" in r.stdout)

    print("\n== 链路2: async 任务轮询 ==")
    r = run(["gen", "a dog", "-m", "mock", "--async",
             "--poll-interval", "0.2", "--base", f"http://127.0.0.1:{PORT}/v1",
             "--outdir", str(td / "b")])
    check("退出码 0", r.returncode == 0, r.stderr[-400:] if r.returncode else "")
    check("发生了 3 次轮询", STATE["polls"] == 3, f"polls={STATE['polls']}")
    check("轮询日志出现", "poll #" in r.stderr)
    check("任务完成后落地", len(list((td / "b").glob("*.png"))) == 1)

    print("\n== 链路3: 504 超时软着陆 ==")
    r = run(["gen", "x", "-m", "mock-slow",
             "--base", f"http://127.0.0.1:{PORT}/v1", "--outdir", str(td / "c")])
    check("退出码 4（超时专用码）", r.returncode == 4, f"rc={r.returncode}")
    check("给出 --async 建议", "--async" in r.stderr)

    print("\n== 链路4: multipart 图生图 ==")
    img = td / "ref.png"
    img.write_bytes(PNG)
    msk = td / "mask.png"
    msk.write_bytes(PNG)
    r = run(["edit", "换背景", "-m", "mock", "-i", str(img), "-i", str(img),
             "--mask", str(msk), "--base", f"http://127.0.0.1:{PORT}/v1",
             "--outdir", str(td / "d")])
    check("退出码 0", r.returncode == 0, r.stderr[-400:] if r.returncode else "")
    check("走了 multipart 路", "multipart 路" in r.stderr)
    check("b64 落地 1 张", len(list((td / "d").glob("*.png"))) == 1)
    check("mask 已随行", "mask" in r.stderr)

    print("\n== 链路5: JSON 图生图（--image-url） ==")
    r = run(["edit", "换背景", "-m", "mock", "--image-url", "http://x/a.png",
             "--base", f"http://127.0.0.1:{PORT}/v1", "--outdir", str(td / "e")])
    check("退出码 0", r.returncode == 0, r.stderr[-400:] if r.returncode else "")
    check("走了 JSON 路", "JSON 路" in r.stderr)

    print("\n== 链路6: 互斥校验 ==")
    r = run(["edit", "p", "-m", "mock", "-i", str(img), "--image-url", "http://x/a.png",
             "--base", f"http://127.0.0.1:{PORT}/v1"])
    check("本地图+URL 同时给被拒", r.returncode == 2)
    check("提示互斥", "互斥" in r.stderr)

    print("\n== 链路7: 无参考图 ==")
    r = run(["edit", "p", "-m", "mock", "--base", f"http://127.0.0.1:{PORT}/v1"])
    check("缺参考图被拒", r.returncode == 2)

    print("\n== 链路8: 缺 Key ==")
    import os
    env = dict(os.environ)
    env.pop("XQAPI_API_KEY", None)
    env.pop("XQAPI_KEY", None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["USERPROFILE"] = str(td / "nohome")
    env["HOME"] = str(td / "nohome")
    r = subprocess.run(
        [sys.executable, str(CLI), "gen", "x", "-m", "mock",
         "--api-key", "k", "--base", f"http://127.0.0.1:{PORT}/v1",
         "--outdir", str(td / "f")],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=60,
    )
    check("显式 --api-key 可用", r.returncode == 0, r.stderr[-300:] if r.returncode else "")

srv.shutdown()
print()
if fails:
    print(f"{len(fails)} 项失败: {fails}")
    sys.exit(1)
print("端到端全部通过")
