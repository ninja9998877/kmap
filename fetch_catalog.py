#!/usr/bin/env python3
"""从 51教学网 抓各年级教材目录，转成结构化数据。

**这是取数工具，不是数据本身。** 用它拿到的东西一律标 `source: textbook`
（来自教材目录）—— 但要注意这只是**教辅站收录的目录**，不等于孩子手上的课本。
版本是否一致仍要核（见生成出来的 yaml 里的 known_gaps）。

为什么要有这个脚本：
  - 目录页的 post 编号是连续的，人工一个个开太慢，而且拿到的内容会截断。
  - **可重复**：将来教材改版，改一下年份重跑就行，不用手抄。

用法：
    python fetch_catalog.py math 195 206 > catalog-math.json
    python fetch_catalog.py --dump 196        # 看单页解析结果，用来对解析逻辑

限速：每次请求间隔 1.2 秒。那个站是个人站，别把人打挂。
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request

UA = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
BASE = 'https://www.51jiaoxue.cn/post/%d.html'
DELAY = 2.0        # 个人站，慢一点没关系，别把人打挂

# 目录后面可能出现的、不属于目录的行 —— 见到就停
_STOP = {'前 言', '前言', '亲爱的同学', '目录'}


def fetch(url: str, tries: int = 3) -> str:
    """带重试的抓取。

    实测这个站会间歇性抽风（同一批 12 个请求，前两个好好的，后面全超时）——
    不重试的话会得到"抓到了但解析不出目录"这种误导性的结果，
    看起来像解析器坏了，其实是根本没拿到页面。
    """
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.read().decode('utf-8', 'replace')
        except Exception as err:                  # noqa: BLE001 —— 网络错什么都有
            last = err
            if i < tries - 1:
                time.sleep(2 * (i + 1))
    raise last


def clean(line: str) -> str:
    """一行目录 → 干净的文字。

    目录行的形态是 `三、认识10以内的数 ············ 14` —— 中间那串点点是
    排版用的，页码也是。两样都去掉，只留名字。
    """
    line = re.sub(r'<[^>]+>', '', line)          # 去标签
    line = line.replace('&nbsp;', ' ').replace('&amp;', '&')
    line = line.strip()
    if not line:
        return ''
    # 去掉尾部的点点和页码
    line = re.sub(r'[·.．\s]*\d+\s*$', '', line)
    line = re.sub(r'[·.．]{2,}', ' ', line)       # 中间成串的点点
    line = re.sub(r'[\s·.．]+$', '', line)
    return line.strip()


def parse(html: str) -> dict:
    """从一页里抠出标题和目录。

    ★ 结束位置**不能**靠"目录后面跟着『前 言』"来定 —— 有的页有那句，有的没有，
      于是同一个格式的页面一半成功一半失败（第一版就是这么栽的：12 册只解析出 1 册）。
      改成按**目录行自己的特征**判断：不是目录行就停。
    """
    title = ''
    m = re.search(r'<title>(.*?)</title>', html, re.S)
    if m:
        title = re.sub(r'\s*-\s*51教学网.*$', '', m.group(1)).strip()

    # 从正文里的"目录<br/>"开始（meta 里那份是截断的，不能用）
    m = re.search(r'目录\s*<br\s*/?>(.*)', html, re.S)
    if not m:
        return {'title': title, 'lines': []}

    lines = []
    for chunk in re.split(r'<br\s*/?>', m.group(1)):
        name = clean(chunk)
        if not name:
            continue
        if name in _STOP:
            break
        # 目录行都很短（单元名或课名）。一旦出现一整句话，目录就已经结束了。
        if len(name) > 24:
            break
        lines.append(name)
    return {'title': title, 'lines': lines}


def looks_like_unit(name: str) -> bool:
    """这一行是"单元"还是"课"？

    单元长这样：`三、认识人民币` / `五 认识图形` / `十一 、总复习`
    课长这样：  `1. 乘法的初步认识` / `口算乘法` / `年、月、日`
    """
    return bool(re.match(r'^[一二三四五六七八九十]+\s*[、.．]?\s*\S', name))


def main() -> int:
    args = sys.argv[1:]

    if args and args[0] == '--dump':
        html = fetch(BASE % int(args[1]))
        parsed = parse(html)
        print('标题:', parsed['title'])
        print('共 %d 行:' % len(parsed['lines']))
        for i, line in enumerate(parsed['lines']):
            kind = '单元' if looks_like_unit(line) else '  课'
            print('  %2d %s  %s' % (i, kind, line))
        return 0

    if len(args) != 3:
        print(__doc__)
        return 2
    subject, lo, hi = args[0], int(args[1]), int(args[2])

    out = []
    for pid in range(lo, hi + 1):
        try:
            parsed = parse(fetch(BASE % pid))
        except Exception as err:  # noqa: BLE001
            print('  !! post/%d 抓取失败: %s' % (pid, err), file=sys.stderr)
            continue
        if not parsed['lines']:
            print('  !! post/%d 没解析出目录（%s）' % (pid, parsed['title']), file=sys.stderr)
        out.append({'post': pid, 'subject': subject, **parsed})
        time.sleep(DELAY)                       # 别把个人站打挂

    json.dump(out, sys.stdout, ensure_ascii=False, indent=1)
    return 0


if __name__ == '__main__':
    sys.exit(main())
