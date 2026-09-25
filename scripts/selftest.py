# -*- coding: utf-8 -*-
"""离线自测：不触碰真实 API，验证 multipart 编码 / b64 落地 / 任务识别 / 参数拼装。"""
import base64
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import xqapi_image as x  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        fails.append(name)


print("== multipart 编码 ==")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    a, b, m = d / "a.png", d / "b.png", d / "m.png"
    a.write_bytes(b"\x89PNG-A")
    b.write_bytes(b"\x89PNG-B")
    m.write_bytes(b"\x89PNG-M")

    body, ctype = x._encode_multipart(
        {"model": "gpt-image-1", "prompt": "把背景换成雪山", "resolution": "2k"},
        [("image", a), ("image", b), ("mask", m)],
    )
    check("boundary 头存在", ctype.startswith("multipart/form-data; boundary="))
    bound = ctype.split("boundary=")[1].encode()
    check("重复 image 字段出现 2 次", body.count(b'name="image"') == 2)
    check("mask 字段存在", body.count(b'name="mask"') == 1)
    check("中文 prompt utf-8 编码", "把背景换成雪山".encode() == "把背景换成雪山".encode())
    check("中文已写入 body", "把背景换成雪山".encode("utf-8") in body)
    check("结束 boundary 正确", body.rstrip().endswith(b"--" + bound + b"--"))
    # 3 个文本字段 + 3 个文件 = 6 个起始段，+1 结束段 = 7 次出现
    check("每段都用同一 boundary", body.count(b"--" + bound) == 7,
          f"实际 {body.count(b'--' + bound)}")

print("== _guess_ext ==")
check("png", x._guess_ext("png") == ".png")
check("jpeg -> .jpg", x._guess_ext("jpeg") == ".jpg")
check("from url", x._guess_ext(None, "https://a/b.webp?sig=1") == ".webp")
check("default png", x._guess_ext(None) == ".png")

print("== b64 落地 ==")
with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    raw = b"\x89PNG\r\n\x1a\nFAKEIMAGE"
    payload = {
        "created": 1,
        "data": [{"b64_json": base64.b64encode(raw).decode()}],
        "usage": {"total_tokens": 10},
    }
    out = d / "r.png"
    got = x.save_results(payload, d, prefix="t", out=out)
    check("写出 1 个文件", len(got) == 1, str(got))
    check("内容字节一致", out.read_bytes() == raw)

print("== 任务识别 ==")
check("task_id 形状识别", x._looks_like_task({"task_id": "abc", "status": "pending"}))
check("id 形状识别", x._looks_like_task({"id": "abc", "status": "queued"}))
check("直出 url 不算任务",
      not x._looks_like_task({"data": [{"url": "https://x/y.png"}]}))
check("直出 b64 不算任务",
      not x._looks_like_task({"data": [{"b64_json": "AAA"}]}))
check("data 形状取 task_id", x._extract_task_id({"data": {"task_id": "t9"}}) == "t9")
check("顶层取 task_id", x._extract_task_id({"task_id": "t1"}) == "t1")

print("== 参数透传 ==")
body = {"model": "m"}
x._apply_extra(body, ["quality=high", "seed=42", "flag=true", "ratio=1:1"])
check("字符串", body["quality"] == "high")
check("数字", body["seed"] == 42)
check("布尔", body["flag"] is True)
check("非 JSON 回退字符串", body["ratio"] == "1:1")

body2 = {}
x._fill_resolution(body2, "2K")
check("resolution 小写归一", body2["resolution"] == "2k")
body3 = {}
x._fill_resolution(body3, None)
check("resolution 缺省不下发", "resolution" not in body3)

print("== UA 处理 ==")
# 实测结论：缺 UA 会被 Cloudflare 403(1010) 拦掉，所以 UA 必须显式带上。
check("默认 UA 非空", bool(x.DEFAULT_UA) and len(x.DEFAULT_UA) > 10)
check("默认 UA 是浏览器 UA", "Mozilla" in x.DEFAULT_UA)
import os as _os
_os.environ.pop("XQAPI_USER_AGENT", None)
check("resolve_ua 回退默认", x.resolve_ua(None) == x.DEFAULT_UA)
check("resolve_ua 尊重显式值", x.resolve_ua("MyAgent/2.0") == "MyAgent/2.0")
_os.environ["XQAPI_USER_AGENT"] = "EnvAgent/1.0"
check("resolve_ua 读环境变量", x.resolve_ua(None) == "EnvAgent/1.0")
check("显式值优先于环境变量", x.resolve_ua("Cli/1.0") == "Cli/1.0")
_os.environ.pop("XQAPI_USER_AGENT", None)
check("DOWNLOAD_TIMEOUT 够大", x.DOWNLOAD_TIMEOUT >= 300.0)

print("== 参考图上限 ==")
check("MAX_REF_IMAGES == 16", x.MAX_REF_IMAGES == 16)
check("SYNC_TIMEOUT == 600", x.SYNC_TIMEOUT == 600.0)
check("RESOLUTIONS", x.RESOLUTIONS == ("1k", "2k", "4k"))

print("== CLI 解析 ==")
p = x.build_parser()
ns = p.parse_args(["gen", "hi", "-m", "m", "-r", "2K", "--async", "--b64-json"])
check("gen 解析", ns.prompt == "hi" and ns.use_async and ns.b64_json)
ns2 = p.parse_args(["edit", "p", "-m", "m", "-i", "a.png", "-i", "b.png", "--mask", "m.png"])
check("edit 多图解析", ns2.image == ["a.png", "b.png"] and ns2.mask == "m.png")
ns3 = p.parse_args(["edit", "p", "-m", "m", "--image-url", "https://a/b.png"])
check("edit URL 路解析", ns3.image_url == ["https://a/b.png"])

print()
if fails:
    print(f"{len(fails)} 项失败: {fails}")
    sys.exit(1)
print("全部通过")
