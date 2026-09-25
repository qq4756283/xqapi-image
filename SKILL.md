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
# 0. 先查模型名（别照抄文档占位符）
python scripts/xqapi_image.py models

# 文生图
python scripts/xqapi_image.py gen "一座未来城市，黄昏，体积光" -m gpt-image-2 -r 2k -o out/city.png

# 长任务 / 4k → 一律加 --async，绕开 600s 同步上限
python scripts/xqapi_image.py gen "赛博朋克海报" -m gpt-image-2 -r 4k --async --outdir out/

# 图生图（多张参考图，multipart 路）
python scripts/xqapi_image.py edit "把背景换成雪山" -m gpt-image-2 -i cat.png -i style.png -r 2k

# 局部重绘
python scripts/xqapi_image.py edit "把猫换成狗" -m gpt-image-2 -i cat.png --mask mask.png

# 参考图是直链（JSON 路）
python scripts/xqapi_image.py edit "换背景" -m gpt-image-2 --image-url https://example.com/cat.png

# b64 内联，不走托管直链（下载慢时可试）
python scripts/xqapi_image.py gen "logo" -m gpt-image-2 --b64-json -o out/logo.png

# 探测某模型支持哪些 resolution（会花钱）
python scripts/xqapi_image.py probe -m gpt-image-2
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

## 关键约定（实测踩坑点）

> 以下三处是**实测踩出来的**，不是文档写的。照文档写会踩。

### 1. User-Agent 是硬性要求

不带 `User-Agent` 头 → **HTTP 403 Cloudflare Error 1010: Access denied**。

| UA | 结果 |
|---|---|
| 不带 UA 头 | ❌ 403 / Error 1010 |
| 脚本名 UA（`MyAgent/1.0`） | ✅ 200 |
| 浏览器 UA | ✅ 200 |
| `Python-urllib/3.x` 默认 | ❌ SSL 连接被重置 |

**不需要伪装浏览器**，但必须带一个像样的 UA。脚本已默认带浏览器 UA，
一般不用管；要覆盖用 `--user-agent` 或环境变量 `XQAPI_USER_AGENT`。

### 2. 模型名别照抄文档

文档示例里的 `your-image-model` / `gpt-image-1` 都是占位符，传 `gpt-image-1`
会返回 `400 模型不存在或未启用`。**先查再调**：

```bash
python scripts/xqapi_image.py models
```

实测该站当前图像模型为 **`gpt-image-2`**（`/v1/models` 共 24 个模型，
图像模型仅此一个）。

### 3. 延迟构成 —— 下载可能比生成还慢

实测 `gpt-image-2` 出 1 张 1k 图：

```
POST 提交 → 200 响应      109.6s   ← 纯生成
直链下载 2.48MB           69.9s    ← 占比 39%，很反常
──────────────────────────────────
端到端                    179.5s
```

1k 就要 110s，**4k 基本必然顶到 600s 上限**。所以：

- 长任务 / 4k / 多张 → **一律加 `--async`**
- 嫌下载慢 → 试 `--b64-json` 走内联 base64，可能更快
- 脚本已把下载单独计时并打印，方便你判断瓶颈

### 其余约定

| 项 | 值 | 说明 |
|---|---|---|
| 同步等待上限 | **600s** | 与官方 SDK 默认超时一致，超时站点返回 **504** |
| 504 退出码 | **4** | 脚本在 stderr 给 `--async` 建议 |
| 403 退出码 | **3** | UA 缺失；脚本会直接给出处置建议 |
| 异步开关 | `--async` | 请求带 `async:true`，拿 task_id 自己轮询 |
| resolution | `1k`/`2k`/`4k` | **大小写不敏感**，脚本自动小写归一；未实测各档耗时 |
| 参考图上限 | **16** | 超了直接报错，不发请求 |
| quality / size / output_format | 视模型而定 | **不要写死**，用 `--extra` 透传 |
| 图片落地 | 默认直链 | 加 `--b64-json` 改内联 base64 |

### 参考图两种给法互斥

- **multipart 路**：`-i a.png -i b.png` 重复字段，可带 `--mask`
- **JSON 路**：`--image-url <url>`，每项只能 `image_url` 或 `file_id` 二选一

两者同时给会被脚本拒绝（这是刻意的，避免歧义）。另有扩展字段
`--image-urls <u1> <u2>` 直接给一组 URL。

### 图生图实测数据（gpt-image-2）

| 操作 | 路径 | 生成 | 下载 | 端到端 | 结果 |
|---|---|---|---|---|---|
| 单图编辑 | multipart | 69.2s | 81.8s | 151.0s | ✅ |
| 单图编辑 | JSON image_url | 69.5s | 44.9s | 114.4s | ✅ |
| 多图编辑(2张) | multipart 同步 | >600s | — | **超时** | ❌ TimeoutError |
| 多图编辑(2张) | multipart + async | — | — | SSL EOF | ❌ 连接被断 |

**关键结论：**

1. **单图编辑没问题**，multipart 和 JSON 两路都通，JSON 路下载更快。
2. **multipart 多图同步路会超 600s 超时**——服务端处理多图参考明显更慢。
3. **multipart 多图 + async 会 SSL EOF**——上传 2 份图片的 body 可能被前置切断。
4. **多图编辑推荐 JSON 路**：`--image-url <u1> --image-url <u2> --async`，
   传直链而非上传文件，绕开 body 大小问题。
5. 带.mask 的局部重绘走 multipart 同步路同样有超时风险，**建议加 `--async`**。

脚本已在 multipart 多图或带 mask 且未加 `--async` 时打印警告。

### 长任务怎么选

| 场景 | 做法 |
|---|---|
| 文生图 1k | 默认同步，50~110s |
| 文生图 4k | **加 `--async`** |
| 图生图单图 | 同步可，JSON 路更快 |
| 图生图多图 | **加 `--async`，用 JSON 路 `--image-url`** |
| 图生图 + mask | **加 `--async`** |
| 同步路吃到 504 / SSL EOF | 重跑加 `--async`，或换 JSON 路 |
| 断线了想续 | `python scripts/xqapi_image.py task <task_id>` |

### 异步任务实测形状（文档没写，实测出来的）

提交 async 请求后，响应形状（不是同步的 `data[]`）：

```json
{
  "id": "71954476-...",
  "object": "image.generation.task",
  "model": "gpt-image-2",
  "status": "pending",
  "results": [],
  "usage": {},
  "error": null
}
```

查询端点：**`GET /v1/tasks/{id}`**（不是 `/images/tasks/`，实测后者 404）

状态流转：`pending` → `processing` → **`completed`** | `failed`

**注意：终态是 `completed`，不是文档示例里的 `succeeded`。**

完成时 `results` 是 **URL 字符串数组**（不是 `data[]` 对象数组）：
```json
{"status": "completed", "results": ["https://xqapi.com/uploads/.../xxx.png"]}
```

脚本内部已做归一化：`results[]` → `data[]` 对象数组，下游 `save_results` 统一处理。

失败时 `error` 字段有安全错误信息：
```json
{"status": "failed", "error": {"code": "CONTENT_POLICY", "message": "...", "type": "..."}}
```

脚本会读 `error.message` 和 `error.code` 打印诊断，退出码 3。

## 参数速查

通用（`gen` / `edit` 都有）：

```
-m, --model            必填，模型名（先跑 models 子命令查）
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
--user-agent, --ua     覆盖 UA（默认已带浏览器 UA；缺 UA 会被 403）
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
    model="gpt-image-2",
    prompt="一座未来城市",
    extra_body={"resolution": "2k"},   # 本站扩展字段走 extra_body
)
print(r.data[0].url)
```

注意：`resolution`、`async` 是本站扩展，官方 SDK 没有对应形参，**必须走 `extra_body`**。

另外：站点要求带 User-Agent。官方 SDK 会自带一个（`OpenAI/Python x.y.z`），
实测这类普通 UA 能过；但如果你在别的地方手工发请求，**漏了 UA 就是 403**。

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
| **403 / Error 1010** | **UA 缺失或不被接受** | 去掉 `--user-agent` 用默认，或给一个像样的 UA |
| `模型不存在或未启用` (400) | 模型名照抄了文档占位符 | 先跑 `models` 子命令查真名 |
| 退出码 4 | 同步超 600s，站点 504 或客户端超时 | 加 `--async` |
| 退出码 3 + SSL EOF | multipart 多图上传 body 被断 | 加 `--async`，或改 JSON 路 `--image-url` |
| 退出码 3 | API 报错 | 看 stderr 里的响应体 |
| `quality` 传了报 400 | 该模型不支持这个取值 | 去掉，或 `probe` 摸一遍 |
| 参考图 > 16 | 站点硬限制 | 拆成多次调用 |
| 多图编辑超时 | multipart 多图同步路实测 >600s | JSON 路 `--image-url` + `--async` |
| 出图快、等下载慢 | 直链下载带宽受限（实测 2.5MB 走 70s） | 试 `--b64-json` |
| 中文乱码 | 控制台码页非 UTF-8 | 设 `PYTHONIOENCODING=utf-8` |
