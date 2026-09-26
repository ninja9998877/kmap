"""调 packyapi 生图。一次一张，存到 art/ 下面。

放在 build/ 里（已被 .gitignore 挡住）。key 从 .env.image 读，**不写进代码**。

用法：
    python build/imggen.py models                     # 看有哪些生图模型
    python build/imggen.py gen <模型> <提示词文件> <输出文件名> [尺寸]
"""
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def cfg():
    env = {}
    for line in (ROOT / '.env.image').read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            k, v = line.split('=', 1)
            env[k.strip()] = v.strip()
    base = env.get('IMAGE_BASE_URL', 'https://www.packyapi.com/v1')
    key = env.get('IMAGE_API_KEY')
    if not key:
        sys.exit('没读到 IMAGE_API_KEY（kmap/.env.image）')
    return base, key


def call(path, payload=None, method='GET', timeout=300):
    base, key = cfg()
    url = base + path
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': 'Bearer ' + key,
        'Content-Type': 'application/json',
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else 'models'

    if what == 'models':
        d = call('/models')
        ms = d.get('data') or []
        img = [m for m in ms if 'image-generation' in (m.get('supported_endpoint_types') or [])]
        print('模型总数 %d，其中能生图的 %d 个：' % (len(ms), len(img)))
        for m in img:
            print('   ', m['id'], '  by', m.get('owned_by'))
        return

    if what == 'gen':
        model, prompt_file, out_name = sys.argv[2], sys.argv[3], sys.argv[4]
        size = sys.argv[5] if len(sys.argv) > 5 else '1024x1024'
        raw = Path(prompt_file).read_text(encoding='utf-8')
        # 提示词文件里允许写注释（存的是**当时那一次用的原文**，注释说明怎么重画）。
        # 分隔线  之上是注释，之下才是真正发给模型的正文。
        prompt = (raw.split('---', 1)[1] if '
---
' in raw else raw).strip()
        print('模型 %s  尺寸 %s  提示词 %d 字' % (model, size, len(prompt)))
        payload = {
            'model': model, 'prompt': prompt, 'n': 1, 'size': size,
            # 这两个是 gpt-image 系列的"透明底"参数。透明是硬要求
            # （App 有深色模式，带白底的图在深色下就是一块白板），
            # 所以两个都带上；哪个不认就现去掉哪个（下面的循环）。
            'background': 'transparent',
            'output_format': 'png',
        }
        # ★ 中转站各家支持的参数不一样（`response_format` 就不认）。
        #   与其一次一次手工试，不如让脚本自己读报错里点名的参数、去掉、重试 ——
        #   但也只在**明确说某个参数不认**时才重试，别的错（额度、模型名）
        #   要立刻抛出来，免得把真问题吞成一个"参数问题"。
        for _ in range(4):
            try:
                d = call('/images/generations', payload, method='POST')
                break
            except urllib.error.HTTPError as err:
                body = err.read().decode('utf-8', 'replace')
                try:
                    bad = (json.loads(body).get('error') or {}).get('param')
                except Exception:
                    bad = None
                if err.code == 400 and bad and bad in payload:
                    print('  参数 %s 不认，去掉重试' % bad)
                    payload.pop(bad)
                    continue
                print('HTTP %s: %s' % (err.code, body[:600]))
                sys.exit(1)
        else:
            sys.exit('参数一路被拒，放弃')
        item = (d.get('data') or [{}])[0]
        if item.get('b64_json'):
            raw = base64.b64decode(item['b64_json'])
        elif item.get('url'):
            with urllib.request.urlopen(item['url'], timeout=180) as r:
                raw = r.read()
        else:
            print('返回里既没有 b64_json 也没有 url：')
            print(json.dumps(d, ensure_ascii=False)[:800])
            sys.exit(1)
        out = ROOT / 'art' / out_name
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(raw)
        print('写出 %s（%d 字节）' % (out, len(raw)))
        return

    sys.exit('不认识的动作：' + what)


if __name__ == '__main__':
    main()
