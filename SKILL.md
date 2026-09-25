---
name: xqapi-image
description: 用 xqapi.com 中转站生图/改图。文生图、图生图/局部重绘、单图多参考图、async 长任务、b64 内联落地。当用户说"生图/画一张/生成图片/文生图/图生图/改图/换背景/局部重绘/用 xqapi 出图"时使用。
---

# xqapi-image — 图像生成技能

用 `xqapi.com` 出图。底层是 OpenAI `/v1/images/*` 的兼容层，本站额外提供
`resolution` 档位、`async` 任务、`file_id` 文件体系。

## 何时用

- 用户要"画/生成/出一张图"
- 用户给参考图要"改图/换背景/换风格/局部重绘"
- 要批量、要 4k、要长任务不阻塞

## 快速上手

```bash
# 文生图
python scripts/xqapi_image.py gen "一座未来城市，黄昏，体积光" -m <MODEL> -r 2k -o out/city.png

# 长任务 / 4k → 一律加 --async，绕开 600s 同步上限
python scripts/xqapi_image.py gen "赛博朋克海报" -m <MODEL> -r 4k --async --outdir out/

# 图生图（多张参考图，multipart 路）
python scripts/xqapi_image.py edit "把背景换成雪山" -m <MODEL> -i cat.png -i style.png -r 2k

# 局部重绘
python scripts/xqapi_image.py edit "把猫换成狗" -m <MODEL> -i cat.png --mask mask.png

# 参考图是直链（JSON 路）
python scripts/xqapi_image.py edit "换背景" -m <MODEL> --image-url https://example.com/cat.png

# b64 内联，不走托管直链
python scripts/xqapi_image.py gen "logo" -m <MODEL> --b64-json -o out/logo.png

# 探测某模型支持哪些 resolution（会花钱）
python scripts/xqapi_image.py probe -m <MODEL>
```

## 配置 API Key

三种任选，优先级从高到低：

1. `--api-key sk-xxx`
2. 环境变量 `XQAPI_API_KEY`
3. 文件 `%USERPROFILE%\.dsh\xqapi.key`（或 `~/.xqapi.key`）

```powershell
# Windows 持久化
[Environment]::SetEnvironmentVariable("XQAPI_API_KEY","sk-xxx","User")
```

## 关键约定（踩坑点）

| 项 | 值 | 说明 |
|---|---|---|
| 同步等待上限 | **600s** | 与官方 SDK 默认超时一致，超时站点返回 **504** |
| 504 退出码 | **4** | 脚本在 stderr 给 `--async` 建议 |
| 异步开关 | `--async` | 请求带 `async:true`，拿 task_id 自己轮询 |
| resolution | `1k`/`2k`/`4k` | **大小写不敏感**，脚本自动小写归一 |
| 参考图上限 | **16** | 超了直接报错，不发请求 |
| quality / size / output_format | 视模型而定 | **不要写死**，取值以模型详情页为准，用 `--extra` 透传 |
| 图片落地 | 默认直链 | 加 `--b64-json` 改内联 base64 |

### 参考图两种给法互斥

- **multipart 路**：`-i a.png -i b.png` 重复字段，可带 `--mask`
- **JSON 路**：`--image-url <url>`，每项只能 `image_url` 或 `file_id` 二选一

两者同时给会被脚本拒绝（这是刻意的，避免歧义）。另有扩展字段
`--image-urls <u1> <u2>` 直接给一组 URL。

### 长任务怎么选

| 场景 | 做法 |
|---|---|
| 1k、普通质量 | 默认同步，通常几十秒 |
| 4k / 高质量 / 多张 | **加 `--async`** |
| 同步路吃到 504 | 重跑加 `--async`，或调 `--timeout` |
| 断线了想续 | `python scripts/xqapi_image.py task <task_id>` |

## 参数速查

通用（`gen` / `edit` 都有）：

```
-m, --model            必填，模型名
-r, --resolution       1k | 2k | 4k（大小写不敏感）
-q, --quality          quality，取值看模型详情页
-s, --size             官方像素串，如 1024x1024
-n, --count            张数（仅 gen）
-f, --output-format    png | jpeg | webp
--b64-json             response_format=b64_json
--async                异步任务模式
--timeout              同步等待秒数，默认 600
--poll-interval        轮询间隔，默认 3s
--poll-max-wait        轮询总上限，默认 3600s
--extra K=V            透传任意字段，可重复，值自动识别类型
--base                 API 根地址，默认 https://xqapi.com/v1
-o, --out              单图完整输出路径
--outdir               多图输出目录，默认 out/
--json                 额外打印原始响应
```

`edit` 独有：`-i/--image`（可重复）、`--mask`、`--image-url`（可重复）、`--image-urls`

## 用官方 SDK 直接调

本技能的形状与官方一致，不想用 CLI 时也能直接用 OpenAI SDK，只改 `base_url`：

```python
from openai import OpenAI

client = OpenAI(api_key="sk-xxx", base_url="https://xqapi.com/v1", timeout=600)

r = client.images.generate(
    model="<MODEL>",
    prompt="一座未来城市",
    extra_body={"resolution": "2k"},   # 本站扩展字段走 extra_body
)
print(r.data[0].url)
```

注意：`resolution`、`async` 是本站扩展，官方 SDK 没有对应形参，**必须走 `extra_body`**。

## 自测

```bash
python scripts/selftest.py        # 29 项离线单测，不联网
python scripts/e2e_mock_test.py   # 21 项端到端，起本地 mock 服务，不碰真实 API
```

## 文件

```
scripts/xqapi_image.py     CLI 主程序，纯标准库
scripts/selftest.py        离线单测
scripts/e2e_mock_test.py   本地 mock 端到端
examples/*.md              提示词与用法示例
```

## 排错

| 现象 | 原因 | 处置 |
|---|---|---|
| 退出码 4 | 同步超 600s，站点 504 | 加 `--async` |
| 退出码 3 | API 报错 | 看 stderr 里的响应体，多半是参数不被该模型支持 |
| `quality` 传了报 400 | 该模型不支持这个取值 | 去掉，或 `probe` 摸一遍 |
| 参考图 > 16 | 站点硬限制 | 拆成多次调用 |
| 中文乱码 | 控制台码页非 UTF-8 | 设 `PYTHONIOENCODING=utf-8`，或直接看落地的图片文件 |
