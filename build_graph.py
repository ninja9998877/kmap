#!/usr/bin/env python3
"""把教材目录展开成知识点图谱。

    python build_graph.py math 2            # 二年级数学（上下册一起）
    python build_graph.py math 2 --dry      # 只看模型返回，不写文件

**取数纪律（这是本项目的铁律）：数据来源必须和可信度一起记。**

  单元结构  → source: textbook   来自教材目录（catalog/*.json，网络检索所得）
  知识点细节 → source: inferred   掌握判据、权重、边 是模型展开的，**必须核**

也就是说：这份图谱的**骨架是检索来的，肉是模型长的**。两者都在文件里标清楚，
并且把"该核什么"写进 known_gaps / to_verify —— 不许把 inferred 当 textbook 用。
"""
import json
import re
import sys
from pathlib import Path

import paths
import tutor
import yaml

# 资料（目录、图谱）在数据目录，不在代码旁边 —— 代码公开、资料私有。
ROOT = paths.DATA
CATALOG = paths.CATALOG
TERM_CODE = {'上册': 'a', '下册': 'b'}

# 学科 → (目录文件, 出版方, 教师身份, 节点前缀)
SUBJECTS = {
    'math': ('math-beijing.json', '北京版', '小学数学教研员', 'm'),
    'english': ('english-beijing.json', '北京版', '小学英语教研员', 'e'),
    'chinese': ('chinese-tongbian.json', '统编版', '小学语文教研员', 'c'),
}


SUBJECT_NAME = {'math': '数学', 'english': '英语', 'chinese': '语文'}


def iter_units(book: dict):
    """把一册的目录统一成 [(单元名, [课名…])]。

    ★ 三科的目录格式**各不相同**，这是取数时就得面对的：
      数学：units 是 [{no,name}]，课放在 book["lessons"][no]
      英语：units 就是一个字符串数组（每个 Unit 一条，没有课级）
      语文：units 是 [{name, lessons}]
    不统一成一种形状，后面每个学科都要分叉一遍。
    """
    for u in book.get('units') or []:
        if isinstance(u, str):
            yield u, []
            continue
        name = u.get('name') or ''
        if u.get('no'):
            name = '%s、%s' % (u['no'], name)
        lessons = u.get('lessons') or (book.get('lessons') or {}).get(u.get('no'), [])
        yield name, list(lessons)


def catalog_text(book: dict) -> str:
    """把一册的目录渲染成给模型看的纯文本。"""
    out = []
    for name, lessons in iter_units(book):
        out.append(name)
        for lesson in lessons:
            out.append('    - %s' % lesson)
    return '\n'.join(out)

# 不建节点的栏目：它们是"栏目"不是"知识点"
SKIP_UNITS = {'总复习', '数学百花园', '附页', '前言'}

PROMPT = """你是{teacher}，在给一个「小学六年知识地图」项目建知识点图谱。

下面是**{edition}{grade}年级{term}**的教材目录（来自网络检索的教辅目录，
不是课本原文，可能有排版误差）：

{catalog}

请把它展开成知识点图谱。要求：

1. **节点粒度**：目录里列到「课」的，每课一个节点；只列到「单元 / Unit」的，
   每个单元一个节点。这些**不建节点**：总复习、数学百花园、附页、前言、
   Vocabulary、单词表、英语歌曲、英语故事、口语交际、习作、语文园地
   （它们是栏目或需要课本原文才有内容）。标着「活动 / 综合性学习」的也跳过。

   ★★ **只能来自上面这份目录。** 目录里缺的行（排版漏字、编号跳号）就**跳过**，
   **绝对不要替它补一个**。哪怕你确信那一册应该有某个单元 —— 目录里没有就是没有。
   凭空补出来的节点会被当成真实教材内容展示给孩子，比少一个节点严重得多。
   节点的 `unit` 字段也必须用目录里出现过的单元名，不要自创。

2. 每个节点给这几样：
   - `id`：小写拼音，形如 "fen-shu-cheng-fa"（英语用 unit1-greeting 这种），
     **不带年级前缀**（脚本会加）
   - `name`：知识点名（中文，简洁，10 字以内）
   - `mastery_test`：**能做出什么算会了**。一句话，要具体到可以出题验证。
     好例子："能算分数乘分数，并说清为什么分子乘分子"；
             "能用 What time is it? 问答整点时间"
     坏例子："理解分数乘法"、"掌握本单元"（不可测）
   - `weight`：1-5，依据是**这个知识点在整个小学阶段的份量**，不是它在本单元的位置。
     核心概念/后续大量依赖它的给 4-5；技巧性或拓展性的给 1-2。

3. 边（三种，每条都要写 why）：
   - `prereq`：学 to 之前必须先会 from —— **同一册内部的先修链要尽量连起来**
   - `peer`：同级、互相印证
   - `confuse`：孩子会搞混的一对
   **宁少勿滥。** 只写你有把握的，不要为了凑数硬连。

4. 只输出 JSON，不要别的话：

{{
  "nodes": [
    {{"id": "...", "name": "...", "unit": "上册 第二单元 表内乘法和除法（一）",
      "mastery_test": "...", "weight": 4}}
  ],
  "edges": [
    {{"from": "id1", "to": "id2", "kind": "prereq", "why": "..."}}
  ]
}}"""


def book_of(catalog: dict, grade: int, term: str):
    for b in catalog['books']:
        if b['grade'] == grade and b['term'] == term:
            return b
    return None


def ask(grade: int, term: str, book: dict, subject: str, tries: int = 3) -> dict:
    """要模型吐 JSON —— **带重试**。

    模型偶尔会给出语法不合法的 JSON（实测：语文三年级那次 `Expecting ',' delimiter`，
    同一份输入重跑就好了）。这不是"这个年级做不了"，是输出抖动，
    重试一次通常就过。重试三次还不行才是真问题。
    """
    _, edition, teacher, _ = SUBJECTS[subject]
    messages = [
        {'role': 'system', 'content': '你是%s，输出严格的 JSON。' % teacher},
        {'role': 'user', 'content': PROMPT.format(
            grade=grade, term=term, catalog=catalog_text(book),
            edition=edition, teacher=teacher)},
    ]
    last = None
    for i in range(tries):
        raw = tutor.chat(messages, max_tokens=16000)
        m = re.search(r'\{.*\}', raw, re.S)
        if not m:
            last = '模型没返回 JSON'
            continue
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError as err:
            last = 'JSON 不合法：%s' % err
            continue
        if not data.get('nodes'):
            last = '模型没给出任何节点'
            continue
        return data
    raise RuntimeError('%d 次都没拿到可用的 JSON（最后：%s）' % (tries, last))


def normalize(grade: int, term: str, data: dict, prefix_letter: str = 'm') -> dict:
    """加年级前缀、补 unit、校验 id 唯一、剔除悬空边。

    id 前缀必须加：不同册之间模型很容易起出同名的拼音 id
    （"分数的认识"在三年级和五年级各有一课），不加前缀就会互相覆盖，
    而且是**静默覆盖** —— 图谱里少一个节点，没人会发现。
    """
    prefix = '%s%d%s-' % (prefix_letter, grade, TERM_CODE[term])
    nodes, seen = [], {}
    for n in data['nodes']:
        raw_id = re.sub(r'[^a-z0-9\-]', '', str(n.get('id', '')).lower().strip())
        if not raw_id:
            raw_id = re.sub(r'[^a-z0-9]', '', str(n.get('name', '')))[:12] or 'kp'
        nid = prefix + raw_id
        while nid in seen:                     # 撞了就加序号，别静默丢
            seen[nid] += 1
            nid = '%s%s-%d' % (prefix, raw_id, seen[nid])
        seen.setdefault(nid, 1)
        nodes.append({
            'id': nid,
            'name': str(n.get('name', '')).strip(),
            'grade': grade,
            'unit': str(n.get('unit') or ('%s %s' % (term, ''))).strip(),
            'mastery_test': str(n.get('mastery_test', '')).strip(),
            'weight': int(n.get('weight') or 3),
            'status': 'active',
            'source': 'inferred',              # ★ 细节是模型长的
        })

    ids = {n['id'] for n in nodes}
    edges, dropped = [], 0
    for e in data.get('edges', []):
        a, b = e.get('from'), e.get('to')
        # 模型给的 from/to 也需要加前缀
        a = prefix + re.sub(r'[^a-z0-9\-]', '', str(a or '').lower())
        b = prefix + re.sub(r'[^a-z0-9\-]', '', str(b or '').lower())
        if a not in ids or b not in ids or a == b:
            dropped += 1
            continue
        edges.append({'from': a, 'to': b,
                      'kind': e.get('kind') if e.get('kind') in ('prereq', 'peer', 'confuse')
                              else 'prereq',
                      'why': str(e.get('why', '')).strip()})
    return {'nodes': nodes, 'edges': edges, 'dropped_edges': dropped,
            'model_nodes': len(data['nodes'])}


def to_yaml(grade: int, data: dict, books: list, subject: str = 'math') -> str:
    units_block = []
    for b in books:
        units_block.append({'册': b['term'],
                            '单元': [{'name': name} for name, _ in iter_units(b)]})

    head = {
        'meta': {
            'grade': grade,
            'subject': SUBJECT_NAME[subject],
            'edition': SUBJECTS[subject][1],
            'edition_year': '2013 版目录 —— 孩子用的可能是 2024 修订版，必须核',
            'source_material': [
                '%s %d 年级教材目录（见 catalog/%s）' % (SUBJECTS[subject][1], grade, SUBJECTS[subject][0]),
            ],
            'known_gaps': [
                '★ 单元结构来自教辅站收录的 2013 版目录，而 2026 秋全国已完成新教材替换。'
                '新旧版大幅接近但**不是同一份**，拿孩子的课本核过之前不要当成可信。',
                '★ 掌握判据、权重、边 全部是模型展开的（source: inferred），**必须核**。',
                '权重是估的，真实依据应该是课本/练习册在每个知识点上配的题量。',
            ],
        },
        'units': units_block,
        'nodes': data['nodes'],
        'edges': data['edges'],
        'to_verify': [
            {'id': 1, 'q': '这一册的单元和各课标题，和孩子的课本一致吗？',
             'why': '不一致的话整张表都要调。目录取自教辅站（2013 版），'
                    '孩子用的大概率是 2024 修订版。',
             'how': '翻课本目录页，和上面的 units 段逐行对。'},
            {'id': 2, 'q': 'mastery_test 写得可测吗？',
             'why': '它决定出题时要考什么。写成"理解 XX"这种不可测的话，'
                    '出题器就抓瞎了。',
             'how': '每条问自己一句：能不能照这句话出一道题。'},
            {'id': 3, 'q': '权重凭什么定的？',
             'why': '它决定图上节点多大、以及薄弱时先补哪个。拍脑袋定会让这两件事都错。',
             'how': '数一数练习册在每个知识点上配的题量。'},
            {'id': 4, 'q': 'confuse 边真的是孩子会混的吗？',
             'why': '这些边是靠教学经验推的，没拿真题验过。',
             'how': '做几道题看他是不是真在那儿翻车。'},
        ],
    }
    body = yaml.dump(head, allow_unicode=True, sort_keys=False, width=100,
                     default_flow_style=False)
    return ('# 本文件由 build_graph.py 生成 —— 改内容请改 catalog/ 下的原始目录后重跑，\n'
            '# 直接手改会在下次重跑时被覆盖。\n'
            '#\n'
            '# 取数纪律：单元结构来自教材目录（textbook）；\n'
            '#          掌握判据/权重/边 是模型展开的（inferred）—— 两者都在字段里标着。\n'
            '#\n'
            '---\n' + body)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    dry = '--dry' in sys.argv
    if len(args) != 2:
        print(__doc__)
        return 2
    subject, grade = args[0], int(args[1])
    if subject not in SUBJECTS:
        print('学科只能是：%s' % ' / '.join(SUBJECTS))
        return 2
    filename, edition, teacher, prefix_letter = SUBJECTS[subject]
    catalog = json.loads((CATALOG / filename).read_text(encoding='utf-8'))
    books = [b for b in catalog['books'] if b['grade'] == grade]
    if not books:
        print('目录里没有 %d 年级' % grade)
        return 1

    merged = {'nodes': [], 'edges': []}
    for book in books:
        # 来源不一定是 51教学网的 post 编号 —— 语文来自博客/电子书课本网
        src = ('post/%d' % book['post']) if book.get('post') else book.get('source', '见 catalog')
        print('  正在展开：%d 年级 %s（%s）' % (grade, book['term'], src), file=sys.stderr)
        raw = ask(grade, book['term'], book, subject)
        part = normalize(grade, book['term'], raw, prefix_letter)
        # 把单元名补成"上册 第一单元 XXX"这种完整形式
        for n in part['nodes']:
            if not n['unit'] or n['unit'] == book['term'] + ' ':
                n['unit'] = '%s（未标注单元）' % book['term']
            elif not n['unit'].startswith(book['term']):
                n['unit'] = '%s %s' % (book['term'], n['unit'])
        merged['nodes'] += part['nodes']
        merged['edges'] += part['edges']
        print('    %d 个节点、%d 条边（模型给的节点 %d，丢弃悬空边 %d）'
              % (len(part['nodes']), len(part['edges']),
                 part['model_nodes'], part['dropped_edges']), file=sys.stderr)

    if dry:
        print(json.dumps(merged, ensure_ascii=False, indent=1)[:3000])
        return 0

    out = ROOT / ('graph-%d-%s.yaml' % (grade, subject))
    out.write_text(to_yaml(grade, merged, books, subject), encoding='utf-8')
    print('已写 %s —— %d 节点 / %d 边' % (out.name, len(merged['nodes']), len(merged['edges'])),
          file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
