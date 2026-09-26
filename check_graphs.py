#!/usr/bin/env python3
"""批量校验 graph-*.yaml。

生成器一次吐 18 个文件，靠眼睛看是看不过来的。这个脚本查的都是
**"错了但不会报错"**那一类：

  - yaml 语法（模型展开的内容里最容易出问题的地方）
  - 边的两端是否真的存在（悬空边让"先修提示"指向一个不存在的节点）
  - id 是否唯一（撞了就是静默覆盖，图谱里少一个节点没人发现）
  - 必要字段是否齐全（缺 mastery_test 出题器就抓瞎）
  - mastery_test 是不是写成了"理解 XX"这种不可测的话
  - **跨册的 id 前缀冲突**（同一科不同年级撞前缀）

用法：
    python check_graphs.py            # 只报问题
    python check_graphs.py --all      # 连统计一起打
"""
import glob
import re
import sys
from collections import Counter, defaultdict

import paths
import yaml

# 不可测的写法：出题器拿这种 mastery_test 出不了题
VAGUE = re.compile(r'^(理解|掌握|熟悉|了解|认识|体会)\S{0,8}$')


def check(path: str) -> tuple:
    """返回 (问题列表, 统计)。"""
    problems = []
    try:
        with open(path, encoding='utf-8') as fh:
            doc = yaml.safe_load(fh)
    except Exception as err:                      # noqa: BLE001
        return ['yaml 解析失败：%s' % err], {}

    if not isinstance(doc, dict):
        return ['顶层不是 mapping'], {}

    nodes = doc.get('nodes') or []
    edges = doc.get('edges') or []
    ids = [n.get('id') for n in nodes]

    dup = [i for i, c in Counter(ids).items() if c > 1]
    if dup:
        problems.append('id 重复 %d 个：%s' % (len(dup), dup[:5]))

    idset = set(ids)
    dangling = [e for e in edges if e.get('from') not in idset or e.get('to') not in idset]
    if dangling:
        problems.append('悬空边 %d 条，例如 %s -> %s'
                        % (len(dangling), dangling[0].get('from'), dangling[0].get('to')))

    missing = defaultdict(list)
    for n in nodes:
        for field in ('id', 'name', 'unit', 'mastery_test', 'weight', 'source'):
            if not n.get(field):
                missing[field].append(n.get('id', '?'))
    for field, who in missing.items():
        problems.append('缺 %s 的节点 %d 个：%s' % (field, len(who), who[:4]))

    # mastery_test 必须"可以照着出一道题"
    vague = [n['id'] for n in nodes
             if n.get('mastery_test') and VAGUE.match(n['mastery_test'].strip())]
    if vague:
        problems.append('掌握判据不可测（写成"理解/掌握…"）%d 个：%s' % (len(vague), vague[:4]))

    short = [n['id'] for n in nodes
             if n.get('mastery_test') and len(n['mastery_test'].strip()) < 8]
    if short:
        problems.append('掌握判据过短 %d 个：%s' % (len(short), short[:4]))

    bad_weight = [n['id'] for n in nodes
                  if not isinstance(n.get('weight'), int) or not 1 <= n['weight'] <= 5]
    if bad_weight:
        problems.append('weight 不在 1~5 %d 个：%s' % (len(bad_weight), bad_weight[:4]))

    # ★ 最要紧的一条：节点说的"单元"必须真的在目录里。
    #   实测踩过 —— 教材目录里六上第四单元那一行排版缺失，模型就**自己补了一个**
    #   「比和按比例分配」出来。这是"凭空造"，而且会被当真实教材内容展示给孩子。
    #   所以拿目录当白名单硬对一遍。
    names = []
    for u in doc.get('units') or []:
        for x in u.get('单元') or []:
            n = (x.get('name') or '').strip()
            if n:
                names.append(n)
                names.append(re.sub(r'^[一二三四五六七八九十]+[、.．]\s*', '', n))
                # 英语的目录写 "Unit 1 Hello!"，模型常改写成 "第一单元 Hello!" ——
                # 同一个单元，把编号剥掉比话题名，别误报成"凭空补的"
                names.append(re.sub(r'^Unit\s*\d+\s*', '', n, flags=re.I))
    if names:
        ghosts = []
        for n in nodes:
            unit = (n.get('unit') or '').strip()
            if unit and not any(x and x in unit for x in names):
                ghosts.append('%s(%s)' % (n.get('id'), unit))
        if ghosts:
            problems.append('★★ unit 不在教材目录里 %d 个（疑似凭空补的）：%s'
                            % (len(ghosts), ghosts[:3]))

    kinds = Counter(e.get('kind') for e in edges)
    stats = {'nodes': len(nodes), 'edges': len(edges),
             'kinds': dict(kinds), 'ids': idset,
             'source': dict(Counter(n.get('source') for n in nodes))}
    return problems, stats


def main() -> int:
    show_all = '--all' in sys.argv
    # 走 paths：图谱是**资料**，在数据目录里，不在代码旁边。
    # （用相对 glob 的话，从代码目录跑就会「没找到 graph-*.yaml」，
    #   而文件明明在 —— 那是查半天也看不出原因的那种。）
    files = sorted(str(p) for p in paths.DATA.glob('graph-*.yaml'))
    if not files:
        print('没找到 graph-*.yaml（%s）' % paths.DATA)
        return 1

    total_nodes = total_edges = 0
    prefix_owner = {}          # id 前缀 -> 文件（查跨文件冲突）
    all_bad = 0

    for path in files:
        problems, stats = check(path)
        total_nodes += stats.get('nodes', 0)
        total_edges += stats.get('edges', 0)
        # 文科和英语的 id 前缀里没有重复的可能，但同科不同册之间要查
        for i in list(stats.get('ids', [])):
            head = re.match(r'^([a-z]\d[ab])-', i)
            if head:
                key = head.group(1)
                if key in prefix_owner and prefix_owner[key] != path:
                    problems.append('id 前缀 %s 也出现在 %s 里' % (key, prefix_owner[key]))
                prefix_owner[key] = path

        if problems:
            all_bad += 1
            print('✗ %s' % path)
            for p in problems:
                print('    %s' % p)
        elif show_all:
            print('✓ %-24s %2d 节点 / %2d 边  %s'
                  % (path, stats['nodes'], stats['edges'], stats.get('kinds', {})))

    print()
    print('%d 个文件，共 %d 节点 / %d 边' % (len(files), total_nodes, total_edges))
    print('全部通过' if all_bad == 0 else '%d 个文件有问题' % all_bad)
    return 1 if all_bad else 0


if __name__ == '__main__':
    sys.exit(main())
