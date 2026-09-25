# xqapi-image

用 [xqapi.com](https://xqapi.com) 中转站生图的 CLI 技能包。纯标准库，零依赖。

底层是 OpenAI `/v1/images/*` 的兼容层，本包额外封装了站点自有的
`resolution` 档位、`async` 任务、600s 超时 504 回退、`b64_json` 内联落地。

## 实测结论（先看这个，少踩坑）

以下三条是**实际打接口测出来的**，不是文档里写的：

**① User-Agent 是硬性要求。** 不带 UA 头直接吃
`403 Cloudflare Error 1010: Access denied`。不需要伪装浏览器，普通
UA 字符串就行；但 `Python-urllib` 默认 UA 会触发 SSL 重置，所以必须显式设置。
本包已默认带浏览器 UA。

**② 模型名别照抄文档。** 文档示例的 `your-image-model` / `gpt-image-1` 是占位符，
照抄会得到 `400 模型不存在或未启用`。先查：

```bash
python scripts/xqapi_image.py models
```

当前该站图像模型为 **`gpt-image-2`**。

**③ 下载可能比生成还慢。** 实测 `gpt-image-2` 出 1 张 1024×1024：

| 阶段 | 耗时 |
|---|---|
| 生成（POST → 200） | **109.6s** |
| 直链下载 2.48 MB | **69.9s** |
| 端到端 | **179.5s** |

1k 就要 110s，4k 基本必然顶到 600s 上限 —— **长任务一律加 `--async`**。
嫌下载慢可以试 `--b64-json`。

## 安装

无需安装，克隆即用（要求 Python 3.9+）：

```bash
git clone https://github.com/qq4756283/xqapi-image.git
cd xqapi-image
```

配置 Key：

```bash
# Linux / macOS
export XQAPI_API_KEY=sk-xxx

# Windows PowerShell 持久化
[Environment]::SetEnvironmentVariable("XQAPI_API_KEY","sk-xxx","User")
```

或写文件：`~/.dsh/xqapi.key`（Windows 为 `%USERPROFILE%\.dsh\xqapi.key`）。

## 用法

### 文生图

```bash
python scripts/xqapi_image.py gen "一座未来城市，黄昏，体积光" -m gpt-image-2 -r 2k -o out/city.png
```

### 长任务 / 4k —— 加 `--async`

同步路有 **600 秒硬上限**（与官方 SDK 默认超时一致），超了站点返回 **504**。
4k、高质量、多张这种情况一律走异步：

```bash
python scripts/xqapi_image.py gen "赛博朋克海报" -m gpt-image-2 -r 4k --async --outdir out/
```

断线了可以续查：

```bash
python scripts/xqapi_image.py task <task_id>
```

### 图生图 / 编辑

参考图有**两种给法，互斥**：

```bash
# multipart 路：重复 -i，本地文件，最多 16 张
python scripts/xqapi_image.py edit "把背景换成雪山" -m gpt-image-2 -i cat.png -i style.png -r 2k

# 局部重绘
python scripts/xqapi_image.py edit "把猫换成狗" -m gpt-image-2 -i cat.png --mask mask.png

# JSON 路：参考图是直链
python scripts/xqapi_image.py edit "换背景" -m gpt-image-2 --image-url https://example.com/cat.png

# 扩展字段 image_urls
python scripts/xqapi_image.py edit "统一加雪景" -m gpt-image-2 --image-urls https://a/1.png https://a/2.png
```

### 内联 base64

默认返回站点托管的直链；要内联 base64 落地：

```bash
python scripts/xqapi_image.py gen "logo" -m gpt-image-2 --b64-json -o out/logo.png
```

### 探测模型参数

`quality` / `size` / `output_format` 的合法取值取决于后端接了哪个模型。
不确定就摸一遍（**会产生费用**）：

```bash
python scripts/xqapi_image.py probe -m gpt-image-2
```

### 透传任意字段

```bash
python scripts/xqapi_image.py gen "x" -m gpt-image-2 --extra seed=42 --extra style=natural
```

值会自动识别类型（`42` → 数字，`true` → 布尔，其余 → 字符串）。

## 用官方 SDK 调

形状一致，改 `base_url` 即可。本站扩展字段走 `extra_body`：

```python
from openai import OpenAI

client = OpenAI(api_key="sk-xxx", base_url="https://xqapi.com/v1", timeout=600)

r = client.images.generate(
    model="gpt-image-2",
    prompt="一座未来城市",
    extra_body={"resolution": "2k"},
)
print(r.data[0].url)
```

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 参数/用法错误（缺 Key、参考图超 16 张、两种给法混用等） |
| 3 | API 报错（含 403 UA 被拒） |
| 4 | 同步等待超过 600s，站点 504 |
| 130 | 用户中断 |

## 自测

```bash
python scripts/selftest.py        # 29 项离线单测，不联网
python scripts/e2e_mock_test.py   # 21 项端到端，起本地 mock 服务，不碰真实 API
```

## 已知约定

- `resolution` 取值 `1k` / `2k` / `4k`，**大小写不敏感**
- 参考图上限 **16 张**（站点自定，官方无此限制）
- 返回形状 `{ created, data[], usage }`，与官方一致
- `image_urls` 与 `file_id` 是站点扩展，跨站不可移植

## 许可

MIT
