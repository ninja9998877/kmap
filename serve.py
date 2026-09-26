#!/usr/bin/env python3
"""六年知识地图 —— 局域网服务（页面 + 出题 API）

安全: 只对局域网开放 —— 按 allowed_subnets (默认 10.0.0.0/24) 校验来源 IP,
其他来源直接 403。绑 0.0.0.0 但白名单挡在路由之前, 不做端口转发, 不暴露公网。
(与 lan-dashboard 同一套规矩)

页面:
    /            学习页 —— 选知识点 → 现场出题 → 作答 → 判分 → 看掌握度变化
    /plan        最初那份产品方案与原型(静态)

接口:
    GET  /api/graph                图谱 + 掌握度现状
    POST /api/generate  {id, n}    现场出一个知识点的题
    POST /api/grade     {qid, i, answer, elapsed}
    POST /api/finish    {qid}      结算这一节, 更新掌握度

★ 答案**绝不发给前端**。题目出好后存在服务端内存里(pending), 前端只拿到题干
和选项, 判分在服务端做。否则孩子按一下 F12 就看见答案了。
"""
import http.server
import ipaddress
import json
import os
import random
import socket
import string
import sys
import threading
import time
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import grader        # noqa: E402
import mastery       # noqa: E402
import paths         # noqa: E402
import tutor         # noqa: E402

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get('KMAP_PORT') or 8890)
LISTEN = '0.0.0.0'


# ---------------------------------------------------------------- 网络身份
#
# ★ 这一段原来只有一行硬写的 `ALLOWED = ['10.0.0.0/24', ...]`，配一个
#   `connect(('10.0.0.1', 80))` 猜地址的 lan_ip()。两处都假定"家里一定是 10.0.0.x"。
#   搬个家 / 换个路由器 / 插到别的网口，症状是**平板打开一片空白或 403，
#   而电脑这边一切正常** —— 没有一个字提示问题出在网段上。

def _route_ipv4():
    """内核要往外发一个包时，会挑哪张网卡的本机地址。

    UDP connect 不真发包、也不要求对端可达，纯粹是问一下路由表。
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('10.0.0.1', 80))
        return s.getsockname()[0]
    except Exception:
        return None
    finally:
        s.close()


def _all_ipv4():
    """本机所有 IPv4 —— 含 Tailscale、WSL/Hyper-V 那些虚拟网卡。"""
    out = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in out:
                out.append(ip)
    except Exception:
        pass
    return out


def _usable(ip):
    """这个地址值不值得印给孩子看。回环 / 169.254 / Tailscale 都不行。

    ★ 家里这台装了 Tailscale，主机名解析出来是 100.64.0.0/10 里的一个地址。
      **平板连不上那个地址**，而且它跟着 Tailscale 走、说变就变。
      地址必须过这道筛子才能印 —— 否则二维码会印成一个永远打不开的地址，
      而症状是"明明扫出来了却打不开"，没人会想到是地址选错了。
    """
    try:
        a = ipaddress.ip_address(ip)
    except (TypeError, ValueError):
        return False
    if a.is_loopback or a.is_link_local:
        return False
    return a not in ipaddress.ip_network('100.64.0.0/10')   # CGNAT ≈ Tailscale


def lan_ip():
    """平板该用的那个地址：路由表说的优先，其次本机其它可用地址。"""
    ip = _route_ipv4()
    if ip and _usable(ip):
        return ip
    for ip in _all_ipv4():
        if _usable(ip):
            return ip
    return '<局域网IP>'


def _covered_by(ip, nets):
    try:
        a = ipaddress.ip_address(ip)
    except (TypeError, ValueError):
        return False
    for n in nets:
        try:
            if a in ipaddress.ip_network(n, strict=False):
                return True
        except ValueError:
            continue
    return False


def _default_allowed():
    """白名单 = 本机实际所在的私有网段 + 本机回环。可用 KMAP_ALLOWED 覆盖。

    回环那一项不能省：只写局域网网段的话，**本机浏览器 localhost 会 403**。

    ★ 不硬写网段的原因见本节开头。放行的是"本机自己所在的网段"，
      所以仍然是"只有局域网"，只是不再假定局域网长什么样。
    """
    env = os.environ.get('KMAP_ALLOWED')
    nets = []
    if env:
        nets = [s.strip() for s in env.split(',') if s.strip()]
    else:
        for ip in [_route_ipv4()] + _all_ipv4():
            if not ip or not _usable(ip):
                continue
            try:
                nets.append(str(ipaddress.ip_network(ip + '/24', strict=False)))
            except ValueError:
                pass
        if not nets:
            nets = ['10.0.0.0/24']      # 一个网卡都探不到时的兜底
    nets.append('127.0.0.1/32')
    out = []
    for n in nets:
        if n not in out:
            out.append(n)
    return out


ALLOWED = _default_allowed()

# 掌握度文件。可以用 KMAP_PROGRESS 指到别处 —— 跑测试时别把真实进度打脏，
# 将来要分多个孩子也是各指一个文件。
PROGRESS_FILE = paths.progress()
PENDING_TTL = 2 * 3600          # 出好的题留两小时, 够一节课了


# ---------------------------------------------------------------- 图谱

GRADES = [1, 2, 3, 4, 5, 6]
SUBJECTS = [
    {'key': 'math',    'name': '数学'},
    {'key': 'chinese', 'name': '语文'},
    {'key': 'english', 'name': '英语'},
]
SUBJECT_NAME = {s['key']: s['name'] for s in SUBJECTS}

_GRAPH_CACHE = {}          # (grade, subject) -> {'data':…, 'mtime':…}


def graph_path(grade: int, subject: str) -> Path:
    """一个「年级 × 学科」对应的图谱文件。

    约定就是**文件名本身**：`graph-{年级}-{学科}.yaml`。
    加一个新单元 = 放一个新文件，代码一行都不用动。
    """
    return paths.graph(grade, subject)


def load_graph(grade: int = 6, subject: str = 'math') -> dict:
    """读知识点图谱。按 (年级,学科) 和 mtime 缓存 —— 改了 yaml 刷新就生效。"""
    path = graph_path(grade, subject)
    if not path.exists():
        raise FileNotFoundError(
            '%s 还没有内容（%d 年级 %s）' % (path.name, grade, SUBJECT_NAME.get(subject, subject)))
    mtime = path.stat().st_mtime
    cached = _GRAPH_CACHE.get((grade, subject))
    if cached and cached['mtime'] == mtime:
        return cached['data']

    import yaml
    with open(path, encoding='utf-8') as fh:
        raw = yaml.safe_load(fh)

    nodes = {}
    for n in raw.get('nodes') or []:
        nodes[n['id']] = {
            'id': n['id'], 'name': n.get('name', ''), 'grade': n.get('grade'),
            'unit': n.get('unit', ''), 'weight': n.get('weight'),
            'mastery_test': n.get('mastery_test', ''),
            'weight_why': n.get('weight_why', ''),
            'source': n.get('source', ''),
        }
    edges = [{'from': e['from'], 'to': e['to'], 'kind': e.get('kind', 'prereq'),
              'why': e.get('why', '')} for e in (raw.get('edges') or [])]
    # 出题时要把先修节点的名字告诉模型 —— 它才知道"卡住了该往回指哪儿"
    for e in edges:
        if e['kind'] == 'prereq' and e['to'] in nodes and e['from'] in nodes:
            nodes[e['to']].setdefault('prereq_names', []).append(nodes[e['from']]['name'])

    data = {'nodes': list(nodes.values()), 'edges': edges,
            'units': raw.get('units') or {}, 'to_verify': raw.get('to_verify') or [],
            'source_file': path.name,
            'grade': grade, 'subject': subject,
            'subject_name': SUBJECT_NAME.get(subject, subject)}
    _GRAPH_CACHE[(grade, subject)] = {'data': data, 'mtime': mtime}
    return data


def api_catalog() -> dict:
    """三级结构：年级 → 学科 → 有没有内容，**外加每个学科的战况**。

    这一层存在的理由：1-6 年级 × 3 学科 = 18 个格子，现在只有一格有内容。
    与其把空位藏起来，不如**显示出来**（点进去说"即将支持"）—— 这样地图是完整的，
    看得到全貌，也看得到东西在长。藏起来的话，界面会随数据增长而不断变形。

    ★ 战况统计是**顺手**算的，不是新接口：这个函数本来就要把每张图谱读一遍
      （为了数 count），快照也只取一次。所以首页（六座城堡）和年级页
      （三个领主）共用这一次请求，不用再打 18 次 /api/graph。

    ★ 五个计数**必须加起来等于 count**。对不上的话说明有节点的状态漏了 ——
      这是自洽性检查，测试里会断言。
    """
    snap = PROGRESS.snapshot()
    grades = []
    for g in GRADES:
        subs = []
        for s in SUBJECTS:
            subs.append(_subject_battle(g, s, snap))
        grades.append({'grade': g, 'subjects': subs})
    return {'grades': grades, 'subjects': SUBJECTS}


def _subject_battle(grade: int, subject: dict, snap: dict = None) -> dict:
    """一个学科的"军团战况"：规模 + 五个状态的计数。

    ★ 抽出来是因为**两处要用同一份**：目录（六个城堡 / 三个领主）和
      交卷后的战报（"这个军团还剩几员将领"）。两处各数一遍的话，
      数字不一致时不会报错，只会出现"领主说剩 4 个、战报说剩 5 个"。
    """
    subject = subject if isinstance(subject, dict) else {'key': subject, 'name': subject}
    snap = snap if snap is not None else PROGRESS.snapshot()
    path = graph_path(grade, subject['key'])
    nodes = []
    if path.exists():
        try:
            nodes = load_graph(grade, subject['key'])['nodes']
        except Exception:                         # noqa: BLE001 —— 坏文件不该让整个目录打不开
            nodes = []
    # 逐节点取状态，再按状态计数。状态来自 mastery.state()（唯一实现）。
    tally = {st: 0 for st in mastery.STATES}
    for n in nodes:
        card_state = (snap.get(n['id']) or {}).get('state') or mastery.ST_UNTOUCHED
        tally[card_state] = tally.get(card_state, 0) + 1
    return {'key': subject['key'], 'name': subject['name'],
            'ready': path.exists() and bool(nodes),
            'count': len(nodes),
            'destroyed': tally[mastery.ST_DESTROYED],
            'suppressing': tally[mastery.ST_SUPPRESSING],
            'engaged': tally[mastery.ST_ENGAGED],
            'untouched': tally[mastery.ST_UNTOUCHED],
            'unstable': tally[mastery.ST_UNSTABLE]}


# ---------------------------------------------------------------- 进度存储

class Progress:
    """每个孩子一份的掌握度。单文件 JSON，读写都加锁。

    时间一律以 `YYYY-MM-DD` 字符串**原样**存 —— 不做 Date 往返，
    免得时区把它挪一天（"明天再见一次"变成后天是很烦人的 bug）。
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data = self._read()

    def _read(self) -> dict:
        try:
            with open(self.path, encoding='utf-8') as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                data.setdefault('cards', {})
                data.setdefault('history', [])
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        return {'cards': {}, 'history': []}

    def _write(self):
        tmp = self.path.with_suffix('.json.tmp')
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(self._data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)      # 原子替换：断电也不会留下半截文件

    def snapshot(self) -> dict:
        with self._lock:
            cards = json.loads(json.dumps(self._data['cards']))
        today = _today()
        out = {}
        for kp, card in cards.items():
            out[kp] = {'strength': round(mastery.strength(card), 3),
                       'due': card.get('due'), 'seen': card.get('seen', 0),
                       'interval': card.get('interval', 0),
                       'due_now': mastery.is_due(card, today),
                       'describe': mastery.describe(card, today),
                       # 战况五档（未遭遇/遭遇中/压制中/已消灭/有异动）。
                       # ★ 分档逻辑**只在 mastery.state() 一处** —— 前端卡片和
                       #   下面的战况统计都读这个值。前端再算一遍的话，
                       #   两处不一致时不会报错，只会出现"总数对不上"。
                       'state': mastery.state(card)}
        return out

    def card(self, kp: str) -> dict:
        with self._lock:
            return dict(self._data['cards'].get(kp) or {})

    def commit(self, kp: str, ratings: list, detail: list) -> dict:
        """一节做完，结算一次。返回新卡片和一句人话。"""
        rating = combine_ratings(ratings)
        with self._lock:
            card = mastery.update(self._data['cards'].get(kp) or {}, rating, _today())
            # 节奏基准也要更新 —— 但那要用真实用时，单独算
            self._data['cards'][kp] = card
            self._data['history'].append({
                'at': _today().isoformat(), 'kp': kp, 'rating': rating,
                'ratings': list(ratings), 'detail': detail,
            })
            self._data['history'] = self._data['history'][-500:]
            self._write()
        return card

    def last_session_secs(self, kp: str):
        """上一次做这一节一共花了多少秒。没做过返回 None。

        必须是**真实数字** —— 效率叙事一旦编（"一般人要练 4 次"），
        孩子迟早会识破，而且那时候他连真的部分也不信了。
        """
        with self._lock:
            for rec in reversed(self._data['history']):
                if rec.get('kp') != kp:
                    continue
                return sum(float(d.get('elapsed') or 0)
                           for d in (rec.get('detail') or []))
        return None

    def tune_pace(self, kp: str, elapsed, difficulty):
        """校准"这个孩子在这一点上的节奏基准"。

        ★ 只在卡片**已经存在**时才写。否则一次没做完的练习（答对一题就退出）
        会凭空造出一张 seen=None 的空卡片留在文件里 —— 实际发生过，看起来像
        "这个知识点测过了"，其实什么都没记住。
        """
        with self._lock:
            card = self._data['cards'].get(kp)
            if not card:
                return
            card['pace'] = mastery.update_pace(card, elapsed, difficulty)
            self._write()


def combine_ratings(ratings: list) -> int:
    """一节课 5 道题 = 一次复习。综合成**一个**评级。

    规则说清楚（这决定卡片怎么走）：
      全对          → 用速度档的**中位数**（别让一道超快的把整体拉高）
      错一半以上    → Again，明天再来
      错一两道      → Hard
    """
    if not ratings:
        return mastery.GOOD
    wrong = sum(1 for r in ratings if r == mastery.AGAIN)
    if wrong == 0:
        ordered = sorted(ratings)
        return ordered[len(ordered) // 2]
    if wrong * 2 > len(ratings):
        return mastery.AGAIN
    return mastery.HARD


def _today():
    import datetime
    return datetime.date.today()


# ---------------------------------------------------------------- 待判的题

class Pending:
    """出好的题放在这里，等前端一道道来判。

    只存内存：题目本来就不落盘（每次现场出），重启丢掉正好。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._items = {}

    def put(self, kp_id: str, questions: list, faces: list,
            grade: int = 6, subject: str = 'math') -> str:
        qid = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))
        with self._lock:
            self._items[qid] = {'kp': kp_id, 'questions': questions, 'faces': faces,
                                'grade': grade, 'subject': subject,
                                'ratings': [], 'detail': [], 'at': time.time()}
            self._sweep()
        return qid

    def _sweep(self):
        now = time.time()
        for key in [k for k, v in self._items.items() if now - v['at'] > PENDING_TTL]:
            self._items.pop(key, None)

    def get(self, qid: str):
        with self._lock:
            item = self._items.get(qid)
            return dict(item) if item else None

    def record(self, qid: str, rating: int, detail: dict):
        with self._lock:
            item = self._items.get(qid)
            if not item:
                return
            item['ratings'].append(rating)
            item['detail'].append(detail)

    def drop(self, qid: str):
        with self._lock:
            self._items.pop(qid, None)


PROGRESS = Progress(PROGRESS_FILE)
PENDING = Pending()


# ---------------------------------------------------------------- 业务

def _untouched_count(grade: int, subject: str) -> int:
    """同一张图里，今天**不用碰**的知识点有几个。

    这是"我故意没给他做"的量化依据 —— 效率感最反直觉的一面：
    **不做什么，也是成绩。**（间隔重复只在快忘时才让你复习，
    没到期还去练是白费力气。）
    """
    try:
        graph = load_graph(grade, subject)
    except FileNotFoundError:
        return 0
    snap = PROGRESS.snapshot()
    return sum(1 for n in graph['nodes']
               if not (snap.get(n['id']) or {}).get('due_now'))


def _all_graphs():
    """**数据目录**里所有已存在的「年级×学科」图谱。坏文件跳过，不连坐。

    注意是数据目录，不是代码目录 —— 代码公开、资料私有，见 paths.py。
    """
    for g in GRADES:
        for s in SUBJECTS:
            if graph_path(g, s['key']).exists():
                try:
                    yield load_graph(g, s['key'])
                except Exception:                 # noqa: BLE001
                    continue


def api_graph(grade: int = 6, subject: str = 'math') -> dict:
    graph = load_graph(grade, subject)
    return {'nodes': graph['nodes'], 'edges': graph['edges'],
            'units': graph['units'], 'to_verify': graph['to_verify'],
            'source_file': graph['source_file'],
            'grade': grade, 'subject': subject,
            'subject_name': graph['subject_name'],
            'progress': PROGRESS.snapshot()}


def api_queue() -> dict:
    """今天该复习什么。

    只收**到期的** —— 就是 SM-2 排出 due 且已经过了那天的。不掺"我觉得他薄弱"
    这种判断，那样复习队列会变成一个说不清的东西。

    排序按**权重降序**：时间只有一份，先花在重要的知识点上。同样重要的按到期日
    先后（拖得越久越先做）。

    ★ 跨年级跨学科扫 —— "今天该复习什么"不该分科目。所以这里遍历全部图谱，
    每条结果都带上自己是哪个年级哪个学科的。
    """
    snap = PROGRESS.snapshot()
    due = []
    for graph in _all_graphs():
        for node in graph['nodes']:
            p = snap.get(node['id'])
            if not p or not p.get('due_now'):
                continue
            due.append({'id': node['id'], 'name': node['name'],
                        'grade': graph['grade'], 'subject': graph['subject'],
                        'subject_name': graph['subject_name'],
                        'unit': node['unit'] or '',
                        'weight': node.get('weight') or 3,
                        'due': p.get('due'), 'strength': p.get('strength', 0),
                        'interval': p.get('interval', 0)})
    due.sort(key=lambda x: (-(x['weight'] or 0), str(x.get('due') or '')))
    return {'due': due, 'count': len(due)}


def api_generate(body: dict) -> dict:
    grade = int(body.get('grade') or 6)
    subject = (body.get('subject') or 'math').strip()
    graph = load_graph(grade, subject)
    kp_id = (body.get('id') or '').strip()
    node = next((n for n in graph['nodes'] if n['id'] == kp_id), None)
    if not node:
        raise ValueError('没有这个知识点：%s' % kp_id)
    n = int(body.get('n') or 5)
    n = max(1, min(8, n))

    result = tutor.generate(node, n=n, grade=grade, subject=subject)
    qid = PENDING.put(kp_id, result['questions'], result['faces'], grade, subject)

    # ★ 只把能给孩子看的部分发出去：题干和选项。答案留在服务端。
    public = []
    for i, q in enumerate(result['questions']):
        item = {'i': i, 'type': q['type'], 'stem': q['stem'],
                'difficulty': q.get('difficulty', 3), 'face': q.get('face')}
        if q['type'] == 'choice':
            item['options'] = q['options']
        else:
            item['unit'] = q.get('unit', '')
        public.append(item)
    return {'qid': qid, 'kp': node, 'faces': result['faces'],
            'questions': public, 'uncovered': result['uncovered'],
            'rejected': result['rejected']}


def api_grade(body: dict) -> dict:
    qid = (body.get('qid') or '').strip()
    item = PENDING.get(qid)
    if not item:
        raise ValueError('这一节的题已经过期了（服务重启或超过两小时），请重新出题')

    index = int(body.get('i', -1))
    if not 0 <= index < len(item['questions']):
        raise ValueError('题号越界')
    question = item['questions'][index]

    answer = body.get('answer') or ''
    elapsed = body.get('elapsed')
    try:
        elapsed = float(elapsed) if elapsed is not None else None
    except (TypeError, ValueError):
        elapsed = None

    correct, why = grader.grade(question, answer)
    card = PROGRESS.card(item['kp'])
    expected = mastery.expected_seconds(card, question.get('difficulty', 3))
    rating = mastery.rating_from(correct, elapsed, expected)

    if correct and elapsed:
        PROGRESS.tune_pace(item['kp'], elapsed, question.get('difficulty', 3))

    PENDING.record(qid, rating, {'i': index, 'correct': correct,
                                 'elapsed': elapsed, 'rating': rating})

    out = {'correct': correct, 'why': why, 'rating': rating,
           'answer': _reveal(question), 'expected_secs': round(expected, 1),
           'done': index >= len(item['questions']) - 1,
           'answered': len(item['detail'])}
    # 选择题把下标直接给前端，别让它从 "B. 3.14×4×4" 里反解
    if question.get('type') == 'choice':
        out['answer_index'] = question.get('answer_index')
    # 答错才给"接下来怎么办" —— 答对了给这个是莫名其妙的
    if not correct:
        # ★ 空对象也要转成 None。`sanitize` 把语文/英语的 ladder 清成了 `{}`，
        #   而 `{}` 在 JS 里是**真值** ⇒ 前端渲染出一个没有题干、却带输入框的
        #   空梯子（用户实测："让我试试这个更小的，还让我填答案，我有点莫名其妙"）。
        ladder = question.get('ladder') or None
        out['ladder'] = ladder if (ladder and ladder.get('stem')) else None
        # 语文/英语给的是"讲透/联想"（teach），不是数学那种"更小的一步"
        out['teach'] = question.get('teach') or None
        if question.get('type') == 'choice' and question.get('praise'):
            out['praise'] = question['praise'].get(_as_index(answer))
    return out


def _as_index(answer: str):
    import re
    m = re.search(r'[A-Da-d]', answer or '')
    if m:
        return str(ord(m.group(0).upper()) - ord('A'))
    m = re.search(r'\d', answer or '')
    return str(int(m.group(0)) - 1) if m else ''


def _reveal(question: dict) -> str:
    """判完之后才把答案给前端 —— 那时已经不需要藏了。"""
    if question['type'] == 'choice':
        opts = question.get('options') or []
        idx = question.get('answer_index')
        letter = chr(ord('A') + idx) if isinstance(idx, int) else '?'
        text = opts[idx] if isinstance(idx, int) and 0 <= idx < len(opts) else ''
        return '%s. %s' % (letter, text)
    unit = question.get('unit') or ''
    return '%s%s' % (question.get('answer', ''), unit)


def prereq_hint(kp_id: str, ok_ratio: float, grade: int = 6, subject: str = 'math'):
    """这一节没做好时，指回**最该补的那个先修节点**。

    这是整个产品最独特的价值：一个知识点崩掉，根往往在更前面 —— 三年级的除法
    薄弱，是二年级的乘法没过关。一张"分数"永远说不出这件事。

    `prompt-3-questions.md` 里写着这条教学法：
        如果第 1 道地基题他就卡住，立刻停下，不要往下走。
        报告"这个点先放着，我们回去补 {先修节点} 更划算。"

    什么情况**不**指：
    - 过了一半以上：那多半是这一节本身的某个考察面没稳，先修是好的
    - 没有先修边（图谱里就没画）
    - 候选的先修节点自己都挺稳（≥0.6）：那可能只是这次粗心，乱指会让他白跑一趟
    """
    if ok_ratio >= 0.5:
        return None
    graph = load_graph(grade, subject)
    nodes = {n['id']: n for n in graph['nodes']}
    snap = PROGRESS.snapshot()

    cands = []
    for edge in graph['edges']:
        if edge['kind'] != 'prereq' or edge['to'] != kp_id:
            continue
        node = nodes.get(edge['from'])
        if not node:
            continue
        p = snap.get(edge['from']) or {}
        cands.append({'id': node['id'], 'name': node['name'],
                      'unit': node.get('unit') or '',
                      'why': edge.get('why', ''),
                      'strength': p.get('strength', 0),
                      'seen': p.get('seen', 0)})
    if not cands:
        return None

    # 越弱越该补；一样弱就先补从没测过的
    cands.sort(key=lambda x: (x['strength'], -x['seen']))
    weakest = cands[0]
    if weakest['strength'] >= 0.6:
        return None
    return {'target': weakest, 'others': cands[1:3]}


def api_finish(body: dict) -> dict:
    qid = (body.get('qid') or '').strip()
    item = PENDING.get(qid)
    if not item:
        raise ValueError('这一节的题已经过期了')
    if not item['ratings']:
        raise ValueError('还没有作答记录')

    # 效率叙事要用的几个**真实**数字 —— 必须在 commit **之前**取，
    # commit 会把它们改掉（lapses 加一、history 多一条）。
    before = PROGRESS.card(item['kp'])
    last_secs = PROGRESS.last_session_secs(item['kp'])
    this_secs = sum(float(d.get('elapsed') or 0) for d in item['detail'])
    lapses_before = before.get('lapses', 0)

    card = PROGRESS.commit(item['kp'], item['ratings'], item['detail'])
    PENDING.drop(qid)
    today = _today()
    ok_n = sum(1 for d in item['detail'] if d['correct'])
    total = len(item['detail'])
    return {'rating': combine_ratings(item['ratings']),
            'ratings': item['ratings'],
            'correct_count': ok_n,
            'total': total,
            'card': card,
            'strength': round(mastery.strength(card), 3),
            'describe': mastery.describe(card, today),
            # 这位将领现在的战况（未遭遇/遭遇中/压制中/已消灭/有异动）。
            # 结算页的战报行直接用它 —— 前端不自己拿 strength 再判一遍。
            'state': mastery.state(card),
            # 打完这一节之后，这个军团还剩几员将领没拿下。**有限**的数，
            # 所以能给人"快打完了"的感觉 —— 这是这套叙事真正起作用的地方。
            'legion': _subject_battle(item.get('grade', 6),
                                      item.get('subject', 'math')),
            # —— 下面是"效率感"那一套：全是真实的，不编 ——
            # 这一节花了多久 / 上次花了多久（同一个人、同一个知识点）
            'this_secs': round(this_secs),
            'last_secs': round(last_secs) if last_secs else None,
            # 这个点以前栽过几次（commit 前的累计答错数）
            'lapses_before': lapses_before,
            # 同一张图里今天**不用碰**的知识点有几个 —— "不做什么"也是成绩
            'untouched': _untouched_count(item.get('grade', 6), item.get('subject', 'math')),
            # 没做好就指回先修 —— 这是图谱里那些边唯一真正派上用场的地方
            'prereq': prereq_hint(item['kp'], (ok_n / total) if total else 0,
                                  item.get('grade', 6), item.get('subject', 'math'))}


# ---------------------------------------------------------------- HTTP

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'kmap/1.0'

    def _allowed(self):
        try:
            ip = ipaddress.ip_address(self.client_address[0])
            return any(ip in ipaddress.ip_network(s, strict=False) for s in ALLOWED)
        except Exception:
            return False

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')   # 改完刷新就是新的
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode('utf-8'),
                   'application/json; charset=utf-8')

    def _file(self, name: str):
        try:
            body = (ROOT / name).read_bytes()
        except Exception as err:
            self._send(404, ('读不到 %s: %s' % (name, err)).encode('utf-8'),
                       'text/plain; charset=utf-8')
            return
        self._send(200, body, 'text/html; charset=utf-8')

    def _query(self) -> dict:
        from urllib.parse import urlparse, parse_qs
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    # 素材目录里允许发出去的扩展名。**白名单，不是黑名单** ——
    # 黑名单（"拦掉 .py"）迟早漏一个，白名单漏不掉。
    ART_TYPES = {'.png': 'image/png', '.webp': 'image/webp', '.svg': 'image/svg+xml',
                 '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg'}

    def _art(self, name: str):
        """发 art/ 目录下的素材（美术画的图）。

        ★ 这是本项目**唯一**按请求路径发文件的地方，所以路径穿越必须挡住：
          `urlparse` 出来的路径已经去掉了 query，但里面可能带 `..`、`%2e%2e`
          解码后的形态、或者反斜杠（Windows 上 `..\\` 也是穿越）。
          做法是**只认裸文件名**：出现任何分隔符或 `..` 就直接 404，
          连拼路径的机会都不给。再加扩展名白名单。
        """
        from urllib.parse import unquote
        try:
            name = unquote(name)
        except Exception:
            name = ''
        if (not name or '..' in name or '/' in name or '\\' in name
                or name.startswith('.')):
            self._send(404, b'bad asset name', 'text/plain; charset=utf-8')
            return
        ctype = self.ART_TYPES.get(Path(name).suffix.lower())
        if not ctype:
            self._send(404, ('素材只发图片：%s' % ', '.join(sorted(self.ART_TYPES))).encode('utf-8'),
                       'text/plain; charset=utf-8')
            return
        path = ROOT / 'art' / name
        if not path.is_file():
            # 404 是**正常**状态：素材还没画。前端靠 onerror 回落到 emoji，
            # 所以这里不要报错、不要噪音。
            self._send(404, ('还没有这个素材：art/%s' % name).encode('utf-8'),
                       'text/plain; charset=utf-8')
            return
        try:
            body = path.read_bytes()
        except Exception as err:
            self._send(500, ('读不到 %s: %s' % (path, err)).encode('utf-8'),
                       'text/plain; charset=utf-8')
            return
        self._send(200, body, ctype)

    def _apk(self):
        """把安卓外壳的 APK 发给平板。

        ★ 这一条**不用重打**：APK 是个指回这个服务的 WebView，界面和题库全在
          服务端 —— 以后改 learn.html、改图谱、改判分，平板上刷新一下就是新的。
          所以这个路由一辈子大概只会被用到一两次（初次安装、或者外壳本身改了）。

        为什么值得有：平板浏览器打开 http://<电脑>:8890/app.apk 就能直接下载安装，
        **不过云、不用数据线**，而且发的是局域网内的东西，不经过任何外部服务。
        （微信不能直接发 .apk，得改名或走网盘 —— 这条路绕开了这个麻烦。）
        """
        path = ROOT / 'apk' / 'kmap-shell.apk'
        if not path.exists():
            self._send(404, (
                '这个服务这里还没有 APK。\n\n'
                '把从 kmap-shell 的 CI 下载到的 APK 放到：\n'
                '  %s\n'
                '再刷新这个页面。\n' % path).encode('utf-8'),
                'text/plain; charset=utf-8')
            return
        try:
            body = path.read_bytes()
        except Exception as err:
            self._send(500, ('读不到 %s: %s' % (path, err)).encode('utf-8'),
                       'text/plain; charset=utf-8')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'application/vnd.android.package-archive')
        self.send_header('Content-Length', str(len(body)))
        # 不要缓存：装了新的就该是新的
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Disposition', 'attachment; filename="kmap-shell.apk"')
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if not self._allowed():
            # 原来这里只有一句 "forbidden (LAN only)"。平板上一片白 ——
            # 看不出是网段不对、还是服务没开、还是防火墙。说了等于没说。
            self._send(403, ('这台设备（%s）不在白名单里，所以打不开。\n'
                             '允许的网段：%s\n'
                             '让孩子的平板连到同一个 WiFi 就行。\n'
                             % (self.client_address[0], ', '.join(ALLOWED))
                             ).encode('utf-8'), 'text/plain; charset=utf-8')
            return
        path = self.path.split('?')[0]
        try:
            if path in ('/', '/index.html'):
                self._file('learn.html')
            elif path == '/plan':
                self._file('index.html')
            elif path == '/art-preview':
                # 给**人眼**看素材用的（技术验收由 tools/imgcheck.py 做）。
                # 模型看不了图片，所以好不好看这一步必须交给人。
                self._file('art-preview.html')
            elif path == '/app.apk':
                self._apk()
            elif path.startswith('/art/'):
                # 美术素材。**素材还没画是正常状态** —— 前端 onerror 回落到 emoji，
                # 所以缺图时这里 404 不报错、也不刷日志。
                self._art(path[len('/art/'):])
            elif path == '/api/health':
                # 给自测脚本用的：说清这个服务连的是**哪一份**进度文件。
                # 不这么做的话，e2e 会把测试数据写进孩子的真实进度，
                # 而且**界面上看不出来哪条是假的**（见 .selftest/kmap-guard.mjs）。
                self._json({'ok': True,
                            'progress_file': PROGRESS_FILE.name,
                            'scratch': PROGRESS_FILE.name != 'progress.json'})
            elif path == '/api/catalog':
                self._json(api_catalog())
            elif path == '/api/graph':
                q = self._query()
                try:
                    grade = int(q.get('grade') or 6)
                except ValueError:
                    grade = 6
                self._json(api_graph(grade, (q.get('subject') or 'math').strip()))
            elif path == '/api/queue':
                self._json(api_queue())
            elif path == '/favicon.ico':
                self._send(204, b'', 'image/x-icon')
            else:
                self._send(404, b'not found', 'text/plain; charset=utf-8')
        except Exception as err:                      # noqa: BLE001
            traceback.print_exc()
            self._json({'error': str(err)}, 500)

    def do_POST(self):
        if not self._allowed():
            # 原来这里只有一句 "forbidden (LAN only)"。平板上一片白 ——
            # 看不出是网段不对、还是服务没开、还是防火墙。说了等于没说。
            self._send(403, ('这台设备（%s）不在白名单里，所以打不开。\n'
                             '允许的网段：%s\n'
                             '让孩子的平板连到同一个 WiFi 就行。\n'
                             % (self.client_address[0], ', '.join(ALLOWED))
                             ).encode('utf-8'), 'text/plain; charset=utf-8')
            return
        path = self.path.split('?')[0]
        try:
            length = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(length) or b'{}')
        except Exception:
            self._json({'error': '请求体不是 JSON'}, 400)
            return

        routes = {'/api/generate': api_generate,
                  '/api/grade': api_grade,
                  '/api/finish': api_finish}
        handler = routes.get(path)
        if not handler:
            self._send(404, b'not found', 'text/plain; charset=utf-8')
            return
        try:
            self._json(handler(body))
        except tutor.TutorError as err:
            # 出题失败是**可预期的**（没配 key / 模型抽风），给一句人话，别 500
            self._json({'error': str(err), 'kind': 'tutor'}, 200)
        except ValueError as err:
            self._json({'error': str(err), 'kind': 'input'}, 200)
        except Exception as err:                      # noqa: BLE001
            traceback.print_exc()
            self._json({'error': '%s: %s' % (type(err).__name__, err),
                        'kind': 'server'}, 500)

    def log_message(self, fmt, *args):
        # 只记接口，静态文件太吵
        if '/api/' in (self.path or ''):
            sys.stderr.write('  %s %s\n' % (self.command, self.path))


def _print_qr(url: str) -> None:
    """在终端里打一个二维码 —— 平板扫一下就能打开，不用手打 IP。

    **每次启动都重新生成**：局域网地址会变（DHCP，这台机器的 MAC 还是随机的），
    上次截图里那个二维码下次可能就失效了。所以别把它存下来反复用。
    """
    try:
        import qrcode
    except ImportError:
        print('  （想要二维码的话：pip install qrcode）', flush=True)
        return
    qr = qrcode.QRCode(border=1, box_size=1)
    qr.add_data(url)
    qr.make(fit=True)
    print('  ↓ 平板/手机扫这个（不用手打地址）', flush=True)
    qr.print_ascii(invert=True)
    print(flush=True)


def main():
    cfg = tutor.load_config()
    ip = lan_ip()

    units = list(_all_graphs())
    n_nodes = sum(len(g['nodes']) for g in units)
    filled = {(g['grade'], g['subject']) for g in units}
    total = len(GRADES) * len(SUBJECTS)

    srv = http.server.ThreadingHTTPServer((LISTEN, PORT), Handler)
    print('六年知识地图  %s:%d  (仅局域网 %s)' % (LISTEN, PORT, ALLOWED), flush=True)
    print('  内容: %d/%d 个「年级×学科」单元有图谱, 共 %d 个知识点'
          % (len(units), total, n_nodes), flush=True)
    for g in units:
        print('    %d 年级 %s —— %s (%d 节点)'
              % (g['grade'], g['subject_name'], g['source_file'], len(g['nodes'])), flush=True)
    if not filled:
        print('    ⚠️  一个都没有 —— 放一个 graph-{年级}-{学科}.yaml 进来', flush=True)
    print('  出题: %s / %s' % (cfg['base'], cfg['model']), flush=True)
    if not cfg['key']:
        print('  ⚠️  没有配 LLM key —— 页面能打开，但点"开始"会报错。'
              '在 kmap/.env 里写一行 LLM_API_KEY=…', flush=True)
    print('  资料: %s' % paths.describe(), flush=True)
    print('  进度: %s' % PROGRESS_FILE, flush=True)
    print(flush=True)
    print('  手机 / 平板  http://%s:%d' % (ip, PORT), flush=True)
    print('  本机浏览器   http://localhost:%d' % PORT, flush=True)
    print('  最初的方案页 http://localhost:%d/plan' % PORT, flush=True)
    if not _covered_by(ip, ALLOWED):
        print('  ⚠️  上面那个地址不在白名单 %s 里 —— 平板会收到 403。' % ALLOWED, flush=True)
        print('     要么清掉 KMAP_ALLOWED，要么把它设成含 %s 的网段。' % ip, flush=True)
    print(flush=True)
    _print_qr('http://%s:%d' % (ip, PORT))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('exit', flush=True)


if __name__ == '__main__':
    sys.exit(main())
