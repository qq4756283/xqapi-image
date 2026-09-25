# 用法示例

## 1. 最简文生图

```bash
python scripts/xqapi_image.py gen "一只戴墨镜的柴犬" -m gpt-image-2
```

落地到 `out/gen-<时间戳>-0.png`。

## 2. 指定输出路径 + 2k

```bash
python scripts/xqapi_image.py gen "水墨风格的远山" -m gpt-image-2 -r 2k -o out/shan.png
```

## 3. 4k 长任务（走异步，别等 600s 超时）

```bash
python scripts/xqapi_image.py gen "超写实机械蜂鸟，微距" -m gpt-image-2 -r 4k --async --outdir out/bird/
```

## 4. 多张候选

```bash
python scripts/xqapi_image.py gen "四个不同的 App 图标，扁平风" -m gpt-image-2 -n 4 --outdir out/icons/
```

## 5. 图生图 —— 多参考图（multipart 路）

```bash
python scripts/xqapi_image.py edit "保留主体，背景换成雪山日落" -m gpt-image-2 \
  -i cat.png -i style_ref.png -r 2k -o out/cat_snow.png
```

## 6. 局部重绘

```bash
# mask.png 里要重绘的区域为透明/白色（按模型约定）
python scripts/xqapi_image.py edit "把猫换成柯基" -m gpt-image-2 \
  -i cat.png --mask mask.png -o out/corgi.png
```

## 7. 参考图是网络直链（JSON 路）

```bash
python scripts/xqapi_image.py edit "把背景换成雪山" -m gpt-image-2 \
  --image-url https://example.com/cat.png -r 2k
```

## 8. 扩展字段 image_urls 一次给一组

```bash
python scripts/xqapi_image.py edit "统一改成水彩风格" -m gpt-image-2 \
  --image-urls https://a.com/1.png https://a.com/2.png https://a.com/3.png
```

## 9. 内联 base64 落地（不依赖站点托管直链）

```bash
python scripts/xqapi_image.py gen "极简线框猫头" -m gpt-image-2 --b64-json -o out/cat_line.png
```

## 10. 断线续查异步任务

```bash
python scripts/xqapi_image.py task task-abc123 --outdir out/resume/
```

## 11. 透传站点扩展字段

```bash
python scripts/xqapi_image.py gen "海报" -m gpt-image-2 \
  --extra seed=12345 --extra negative_prompt="低质量, 模糊" --extra style=vivid
```

## 12. 摸清某模型支持哪些档位

```bash
python scripts/xqapi_image.py probe -m gpt-image-2
```

输出形如：

```
[xqapi-image] resolution 探测结果:
  1k: OK
  2k: OK
  4k: 拒绝 HTTP 400: {"error":{"message":"unsupported resolution"}}
```

## 13. 官方 SDK 等价写法

```python
from openai import OpenAI

client = OpenAI(api_key="sk-xxx", base_url="https://xqapi.com/v1", timeout=600)

# 文生图
r = client.images.generate(
    model="gpt-image-2",
    prompt="一座未来城市",
    extra_body={"resolution": "2k", "async": True},  # 本站扩展走 extra_body
)

# 图生图（multipart）
with open("cat.png", "rb") as f:
    r = client.images.edit(model="gpt-image-2", image=f, prompt="把背景换成雪山")
```

## 14. 批量出图脚本（PowerShell）

```powershell
$prompts = @("赛博朋克街道", "蒸汽朋克飞艇", "太空电梯", "深海城市")
$i = 0
foreach ($p in $prompts) {
    $i++
    python scripts/xqapi_image.py gen $p -m gpt-image-2 -r 2k --async -o "out/batch-$i.png"
}
```
