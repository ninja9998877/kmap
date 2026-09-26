"""出题器 —— 现场出题，但答案是我们自己验过的。

★ 这是整套设计里唯一不能省掉的机关。

题目既然是现出的，答案也是现算的 —— 那答案本身就可能错。而"正确"答案错了
的题，比判分不准还致命：孩子会照单全收一个错的东西。所以：

    让模型出题（它的强项），
    但**绝不让它当答案的权威**。

流程是：模型吐结构化四件套（题干 / 算式 / 答案 / 类型 / 难度 / 梯子）→
我们拿 `grader.eval_expr` 把它的算式**重算一遍** → 和它给的答案比对 →
对不上就丢弃重出。孩子看到的每一道题，答案都被独立验过。

教学法（考察面 → 阶梯 → 答错先给梯子）来自同目录的 `prompt-3-questions.md`，
这里只是把它转成结构化输出。那份提示词写得很好，别在这里重写一遍教学论。

三个刻意的取舍：

1. **只出能判分的题**（填空 / 选择）。说理题没法确定性判分，而把判分交给模型
   正是这套设计要避免的事 —— 所以这类题宁可不出现。想加，得先想清楚它怎么判。
2. **三科的"验"不是一个强度**（见 `validate` 的注释）：
   - 数学：答案由我们自己**重算验证**（expr → eval_expr → 比对）。最硬。
   - 语文 / 英语：没有算式可算，只能保证**答案唯一、判分一致**；
     答案本身对不对，程序**验不了**。不要把它说成"答案都验过了"。
3. **题不落盘**。每次现场出，孩子不会两次看到同一道题。代价是必须联网、且有
   几秒延迟（见 generate() 的注释）。

★ 还有一个血的教训写在 `SYSTEM_TMPL` 里：这段提示词原来**硬编码成"小学数学老师、
  六年级"**，加语文英语时忘了改 —— 结果语文《观潮》的梯子出成了
  「"十八"是由 1 个十和 ___ 个一组成的」（一年级数学）。
  **按学科定制不是可选项。**
"""
import json
import os
import re
import time
import urllib.error
import urllib.request

import grader

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_BASE = 'https://api.deepseek.com/v1'
DEFAULT_MODEL = 'deepseek-chat'


# ---------------------------------------------------------------- 配置

def load_env_file(path: str) -> dict:
    """读一个 .env。不引三方库 —— 这东西的语法就只有 `K=V` 一行。"""
    out = {}
    try:
        with open(path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                k, v = line.split('=', 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def load_config() -> dict:
    """按优先级找 LLM 配置。

    环境变量优先于文件；`kmap/.env` 优先于别人的 .env。谁都不在就报错 ——
    这里**不猜**，猜出来的配置会在几秒钟后变成一个看不懂的 401。
    """
    values = {}
    values.update(load_env_file(os.path.join(HERE, '.env')))

    base = (os.environ.get('KMAP_LLM_BASE')
            or os.environ.get('LLM_BASE_URL')
            or values.get('LLM_BASE_URL')
            or DEFAULT_BASE)
    key = (os.environ.get('KMAP_LLM_KEY')
           or os.environ.get('LLM_API_KEY')
           or values.get('LLM_API_KEY')
           or os.environ.get('DEEPSEEK_API_KEY')
           or values.get('DEEPSEEK_API_KEY')
           or '')
    model = (os.environ.get('KMAP_LLM_MODEL')
             or os.environ.get('LLM_MODEL')
             or values.get('LLM_MODEL')
             or DEFAULT_MODEL)
    return {'base': base.rstrip('/'), 'key': key, 'model': model}


# ---------------------------------------------------------------- 调用

class TutorError(RuntimeError):
    """出题环节出的任何问题。调用方应该把它变成一句人话，而不是 500。"""


def _chat(messages, max_tokens=8000, json_mode=True, timeout=90, retries=1) -> str:
    """调模型，带重试。

    重试是必须的：出题挂一次，孩子看到的就是一个红框。实测偶发（同一台机器、
    同一份请求，前一次卡住、后一次 15 秒就回来了）—— 这种抖动不该被用户看见。
    但 **4xx 不重试**：401 / 400 重试一百次也是同样的错，只会让人多等一分钟。

    90 秒 × (1+1) 轮 ≈ 最坏 3 分钟。正常是 15 秒，留这么多余量是因为
    "卡住"和"慢"从外面看是一样的，而放弃得太早会让孩子白等一场。
    """
    delay = 2
    for attempt in range(retries + 1):
        try:
            return _chat_once(messages, max_tokens, json_mode, timeout)
        except TutorError as err:
            text = str(err)
            fatal = 'HTTP 4' in text or '没有配置 LLM key' in text
            if fatal or attempt == retries:
                raise
            time.sleep(delay)
            delay *= 2


def chat(messages, max_tokens=8000, timeout=90, retries=1) -> str:
    """公开的调用入口 —— 给 build_graph.py 这类离线工具用。

    没有单独实现一遍的理由：配置读取、重试、推理模型那个 max_tokens 的坑，
    都不该有第二份。**同一份逻辑绝不允许两处实现。**
    """
    return _chat(messages, max_tokens=max_tokens, timeout=timeout, retries=retries)


def _chat_once(messages, max_tokens, json_mode, timeout) -> str:
    """调一次模型，返回 content 字符串。

    ★ `max_tokens` 必须给足：这是个**推理模型**，`reasoning_content` 会先吃掉
    一大截额度，给小了（实测 20）会得到**空的 content**，而 HTTP 是 200 ——
    看起来像"模型不肯说话"，其实是额度被思考用光了。
    """
    cfg = load_config()
    if not cfg['key']:
        raise TutorError(
            '没有配置 LLM key。请在 kmap/.env 里写一行 LLM_API_KEY=... '
            '（或设置环境变量 KMAP_LLM_KEY）')

    body = {'model': cfg['model'], 'max_tokens': max_tokens, 'messages': messages,
            'temperature': 1.0}
    if json_mode:
        body['response_format'] = {'type': 'json_object'}

    req = urllib.request.Request(
        cfg['base'] + '/chat/completions',
        data=json.dumps(body).encode('utf-8'),
        method='POST',
        headers={'Authorization': 'Bearer ' + cfg['key'],
                 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as err:
        detail = err.read().decode('utf-8', 'replace')[:300]
        raise TutorError('模型返回 HTTP %s：%s' % (err.code, detail)) from None
    except Exception as err:
        raise TutorError('连不上模型：%s' % err) from None

    choice = (data.get('choices') or [{}])[0]
    content = (choice.get('message') or {}).get('content') or ''
    if not content.strip():
        finish = choice.get('finish_reason')
        usage = data.get('usage') or {}
        raise TutorError('模型没吐内容（finish_reason=%s，推理用了 %s 个 token）—— '
                         '多半是 max_tokens 被思考吃光了'
                         % (finish, usage.get('completion_tokens_details', {})
                            .get('reasoning_tokens', '?')))
    return content


def _extract_json(text: str) -> dict:
    """从回复里抠出 JSON。

    即使开了 json_object 模式也要容错：见过模型在 JSON 前后加一句
    "好的，这是题目："，以及用 ```json 包起来。
    """
    text = text.strip()
    fence = re.search(r'```(?:json)?\s*(.+?)\s*```', text, re.S)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start, end = text.find('{'), text.rfind('}')
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError as err:
            raise TutorError('模型给的不是合法 JSON：%s' % err) from None
    raise TutorError('模型回复里找不到 JSON')


# ---------------------------------------------------------------- 提示词

# 学科 → 出题时的"答案纪律"。三科的答案形态根本不同，
# 用一段提示词盖三科是错的（真出过事，见下面 SUBJECT_RULE）。
SUBJECT_RULE = {
    'math': {
        'name': '数学',
        'answer': '**答案必须是一个数**（可写分数如 "3/4"）。',
        'expr': ('每道填空题必须给 `expr`：一个**纯算式**，只含数字和 + - * / ( ) '
                 '和空格，不能有中文、单位、等号、百分号。程序会用你给的算式'
                 '**重算一遍**，和 `answer` 比对，对不上就丢弃重出。'),
        'extra': 'unit 填答案的单位后缀（如"千克"）；没有就空字符串。',
        # 数学是**程序性知识** —— 卡住通常是某一步不熟，拆小了就能过去
        'recover': """数学：给 `ladder` —— **同一个知识点内部更小的一步**，把他托上去。
   例：{"stem": "先只算个位：5 × 8 = ___", "expr": "5*8", "answer": "40"}""",
    },
    'english': {
        'name': '英语',
        'answer': ('**答案必须是一个单词、一个数字或一个短语**（如 "nine"、"10"、'
                   '"Teachers\' Day"），大小写不敏感。'),
        'expr': ('英语**没有** `expr` 字段，不要写。程序靠规范化后的字符串比对判分 —— '
                 '所以答案要写**唯一确定的形式**，不要给"两种都行"的答案（那种改成选择题）。'),
        'extra': ('如果考的是拼写，答案要给出唯一正确的拼法；如果考的是日期/时间，'
                  '用阿拉伯数字写（如 "10"）。'),
        # 英语答错多半是**没记住** —— 光给"更小的一步"没用，要建立联想
        'recover': """英语：给 `teach`（**记不住的问题，要用联想挂到他已经知道的东西上**）：
   {
     "link": "联想记忆：这个词/句怎么和他已知的东西连起来。
              要讲**为什么**能这么记（词根、读音、场景、画面都行），不要只是重复一遍",
     "scene": "它用在什么场合 —— 给一个具体的小场景，最好他熟悉",
     "similar": ["相近或容易混的表达，各配一句说明"]
   }
   ⚠️ 不要给 `ladder`。英语答错不是"步骤错了"，拆小步骤对他没有帮助。""",
    },
    'chinese': {
        'name': '语文',
        'answer': ('**答案必须是一个字、一个词或一个短句**（如 "潮来前"、"人声鼎沸"、'
                   '"比喻"），要短、要唯一。'),
        'expr': ('语文**没有** `expr` 字段，不要写。程序靠规范化后的字符串比对判分 —— '
                 '答案必须唯一确定，有多种合理说法的题目改成选择题。'),
        'extra': ('★ 语文最该考的是：字音字形、笔画笔顺、词语意思与搭配、课文内容、'
                  '古诗默写、修辞手法。这些都能出成有唯一答案的题。'),
        # 语文答错是**没读到/不知道** —— 要讲透、补背景，让他有印象
        'recover': """语文：给 `teach`（**不是"步骤错了"，是"没读到"或"不知道"** ——
   所以要讲透、补上背景，让这件事在他脑子里留下痕迹）：
   {
     "point": "这道题真正考的是什么（一句话，说清考点）",
     "background": "跟它相关的背景、典故或生活常识（1~2 句，**要有意思**，
                    是那种他会想讲给别人的东西）",
     "example": "再举一个同类的例子（帮他认出这一类）"
   }
   ⚠️ 不要给 `ladder`。语文的理解题没有"步骤"可拆，追问一句更小的话帮不到他。""",
    },
}

SYSTEM_TMPL = """你是小学{subject}老师，在为 **{grade} 年级**学生现场出题。你不是考官 ——
你的目标是让这个孩子**真的会**这个知识点，出题只是手段。

★ 硬性要求（违反了这道题会被程序丢弃）：

1. ★★ **学科纪律** —— 这是最容易犯的错。这道题的答案必须是 **{subject}** 的内容。
   **不要因为题干里出现数字、日期、方位，就把它变成一道数学题。**

   真实反例：语文《观潮》里有一句"农历八月十八是一年一度的观潮日"，
   出题时梯子给成了「"十八"是由 1 个十和 ___ 个一组成的」——
   那是**一年级数学**的"数的组成"。孩子正在做语文，突然被问数学，只会更困惑。

   语文正确地考这句话，应该考："这句话说的是什么" / "哪个词写出了潮水的声音"。

2. **年级纪律**：这是一位 **{grade} 年级**学生。不要出超出这个年级的题
   （别让一年级列竖式），也不要低幼到没意义。

3. **只出能判分的题**：{answer_rule}
   不要出"说说为什么"这类说理题 —— 程序无法可靠地判它对错。

4. {expr_rule}

5. {extra}

6. 题目难度用 difficulty 标 1~5（1 最容易）。

7. ★★ 每道题都要给"**他答错之后你怎么办**"。这件事**三科的做法根本不同**，
   给错了等于白给 —— 这一条是本科目最重要的产品决定：

{recover}

   ★ 无论哪一科：**不许跨学科**。孩子在做语文，你就只能给语文的东西；
   换一科考他一遍只会让他更困惑。

8. **说话的口吻**（只影响鼓励语，不影响内容）：

   `praise`（他答错选项时你说的话）要**像一个懂他的学长**，不是像老师：
   - ✗ 别用"你真棒""真聪明" —— 捧的是天赋不是努力，而且肉麻
   - ✓ 说清他**具体哪一步做对了**，口气随便点、短点

   ★ 但**题干、答案、讲解（why / teach）这些是严谨内容**——
   该怎么准确就怎么准确，**不要为了活泼牺牲准确**。口吻只加在"壳"上。

先在心里拆这个知识点的"考察面"（要真的会它，得会哪几件事），再出题。
出题必须覆盖拆出来的每一条考察面。

输出 JSON，结构：
{{
  "faces": [{{"name": "考察面的一句话", "tag": "地基|真懂|易错"}}],
  "questions": [
    {{
      "type": "fill",                  // 或 "choice"
      "stem": "题干，空处用 ___ 表示",
      "unit": "千克",                  // 填空答案的单位后缀；没有就空字符串
      "expr": "485*8",                 // 只有数学有这一项
      "answer": "3880",                // 唯一确定的答案
      "accept": ["3,880"],             // 可选：还接受哪些写法
      "face": 0,                       // 覆盖 faces 里的第几项
      "difficulty": 3,
      "ladder": {{"stem": "更小的一步", "expr": "5*8", "answer": "40"}},
      "teach": {{"point": "…", "background": "…", "example": "…"}},
      "why": "答对或讲评时用的一句话：为什么这么做"
    }},
    {{
      "type": "choice",
      "stem": "题干",
      "options": ["选项A", "选项B", "选项C"],   // 2~4 个，不要写 A/B/C 前缀
      "answer_index": 1,                       // 正确选项的下标，从 0 开始
      "face": 1,
      "difficulty": 2,
      "praise": {{"0": "选了这个的话，先肯定他哪一步对了"}},
      "ladder": {{"stem": "更小的一步", "options": ["…", "…"], "answer_index": 0}},
      "why": "讲评一句话"
    }}
  ]
}}

（`ladder` 和 `teach` **按上面第 7 条只给其中一个**，不要两个都给。）

只输出 JSON，不要别的话。"""


def system_prompt(grade: int, subject: str) -> str:
    rule = SUBJECT_RULE.get(subject, SUBJECT_RULE['math'])
    return SYSTEM_TMPL.format(subject=rule['name'], grade=grade,
                              answer_rule=rule['answer'], expr_rule=rule['expr'],
                              extra=rule['extra'], recover=rule['recover'])


def build_messages(kp: dict, n: int = 5, avoid: list = None,
                   grade: int = 6, subject: str = 'math') -> list:
    """把知识点变成一个出题请求。

    `avoid` 是这一节课**已经出过**的题干 —— 现场出题的代价就是可能撞题，
    把已出的塞回去让模型避开，比事后去重省事。
    """
    lines = [
        '学科：%s' % SUBJECT_RULE.get(subject, SUBJECT_RULE['math'])['name'],
        '年级：%s 年级' % grade,
        '知识点：%s' % kp.get('name', ''),
        '这一节要达到的标准：%s' % (kp.get('mastery_test') or '（未填）'),
    ]
    if kp.get('unit'):
        lines.append('它在教材里的位置：%s' % kp['unit'])
    if kp.get('weight_why'):
        lines.append('它为什么重要：%s' % kp['weight_why'])
    prereq = kp.get('prereq_names') or []
    if prereq:
        lines.append('先修知识（他没掌握这些的话，会卡在更前面）：%s'
                     % '、'.join(prereq))

    lines.append('')
    lines.append('请出 %d 道题。' % n)
    if avoid:
        lines.append('不要出下面这些已经出过的题（换个数、换个情境）：')
        for stem in avoid[:12]:
            lines.append('  - %s' % stem)

    return [{'role': 'system', 'content': system_prompt(grade, subject)},
            {'role': 'user', 'content': '\n'.join(lines)}]


# ---------------------------------------------------------------- 校验

def validate(q: dict, subject: str = 'math') -> tuple:
    """一道题能不能给孩子看。返回 (ok, 说明)。

    ★ 这里是"答案由我们自己验"落地的地方 —— 但**三科能验到什么程度不一样**：

      数学：有算式，`eval_expr` 重算一遍，和它给的答案**完全相等**才算过。最硬。
      语文 / 英语：没有算式可算。只能保证**答案唯一、判分一致**（规范化后比对），
                    但**答案本身对不对，程序验不了**。这一点必须如实标出来，
                    不能说成"答案都验过"。

    所以数学那条链是"答案由我们自己验"，语文英语只是"判分确定"。两回事。
    """
    if not isinstance(q, dict):
        return False, '不是一道题'
    if not (q.get('stem') or '').strip():
        return False, '没有题干'

    try:
        difficulty = int(q.get('difficulty') or 3)
    except (TypeError, ValueError):
        return False, 'difficulty 不是整数'
    if not 1 <= difficulty <= 5:
        return False, 'difficulty 超出 1~5'

    kind = q.get('type')
    if kind == 'fill':
        answer = str(q.get('answer', '')).strip()
        if not answer:
            return False, '没有答案'
        if subject == 'math':
            expr = q.get('expr')
            if not expr or not str(expr).strip():
                return False, '数学填空题没有 expr，答案就没人验'
            try:
                computed = grader.eval_expr(str(expr))
            except ValueError as err:
                return False, 'expr 算不出来（%s）' % err
            stated = grader.parse_answer(answer)
            if stated is None:
                return False, 'answer 不是一个数'
            if computed != stated:
                # 这一条就是整个模块存在的理由
                return False, ('答案对不上：它给的算式 %s = %s，但它写的答案是 %s'
                               % (expr, computed, q.get('answer')))
        else:
            # 语文/英语：答案不能是一整句话（那样多半有多种说法，判不准）
            if len(answer) > 24:
                return False, '答案太长（%d 字），多半有两种说法，改成选择题' % len(answer)
            if '\n' in answer:
                return False, '答案里有换行'
        if grade_check_ladder(q, subject) is False:
            return False, 'ladder 本身不自洽'

    elif kind == 'choice':
        options = q.get('options')
        if not isinstance(options, list) or not 2 <= len(options) <= 4:
            return False, '选项不是 2~4 个'
        idx = q.get('answer_index')
        if not isinstance(idx, int) or not 0 <= idx < len(options):
            return False, 'answer_index 越界'
        ladder = q.get('ladder') or {}
        if ladder.get('options') and not isinstance(ladder.get('answer_index'), int):
            return False, 'ladder 是选择题却没有 answer_index'
    else:
        return False, 'type 只能是 fill 或 choice'

    # ★ 答错之后给什么，三科不同（数学拆步骤 / 英语建联想 / 语文讲背景）。
    #   少了这一块，孩子答错后屏幕上什么都没有 —— 那才是真正的"白做了"。
    ladder = q.get('ladder') or {}
    teach = q.get('teach') or {}
    if subject == 'math':
        if not ladder.get('stem'):
            return False, '数学题没有 ladder —— 他答错时就没话可说了'
    elif subject == 'english':
        if not (teach.get('link') or '').strip():
            return False, '英语题没有 teach.link —— 答错时不给他联想记忆，等于白错'
    else:
        if not (teach.get('point') or '').strip():
            return False, '语文题没有 teach.point —— 答错时不讲透，等于白错'
    if ladder.get('stem') and RE_MATH_ISH.search(str(ladder.get('stem'))):
        if subject != 'math':
            return False, '梯子跑成数学题了'
    # ★ 梯子的学科纪律：语文/英语的梯子不该是一道纯算术题。
    #   真实的坑：语文《观潮》的梯子出成了「"十八"是由 1 个十和几个一组成的」。
    if subject != 'math':
        stem = str(ladder.get('stem', ''))
        if RE_MATH_ISH.search(stem):
            return False, ('梯子跑成数学题了（「%s」）—— 孩子在做%s，'
                           '换一科考他只会更困惑' % (stem[:24], SUBJECT_RULE[subject]['name']))
    return True, 'ok'


# 语文/英语题里不该出现的"数学味"说法
RE_MATH_ISH = re.compile(r'[几多少]\s*个\s*[十一百千万]|由\s*\d+\s*个|'
                         r'平均分|竖式|算式|列式|加减|乘除|求商|余数')


def grade_check_ladder(q: dict, subject: str = 'math'):
    """梯子自己也得能判分。返回 True/False/None（不适用）。

    只有数学能验（靠 expr 重算）；语文英语的梯子没有算式，返回 None 表示"不适用"——
    那不代表它是对的，只代表程序验不了。
    """
    ladder = q.get('ladder') or {}
    if q.get('type') != 'fill' or subject != 'math':
        return None
    if not ladder.get('expr'):
        return None
    try:
        computed = grader.eval_expr(str(ladder['expr']))
    except ValueError:
        return False
    stated = grader.parse_answer(str(ladder.get('answer', '')))
    return None if stated is None else (computed == stated)


def sanitize(q: dict, subject: str = 'math') -> dict:
    """把通过校验的题整理成判分器认的形状（grader.grade 的入参）。"""
    out = {'type': q['type'], 'stem': q['stem'].strip(),
           'difficulty': int(q.get('difficulty') or 3),
           'face': q.get('face'), 'why': (q.get('why') or '').strip(),
           'ladder': _clean_task(q.get('ladder') or {}),
           # 语文/英语答错后给的是"讲透/联想"，不是"更小的一步"
           'teach': {k: str(v).strip() for k, v in (q.get('teach') or {}).items()
                     if isinstance(v, (str, int, float)) and str(v).strip()}}
    if isinstance((q.get('teach') or {}).get('similar'), list):
        out['teach']['similar'] = [str(x).strip()
                                   for x in q['teach']['similar'] if str(x).strip()]
    # ★ 一个学科只留它该用的那一个。实测模型会两个都给（prompt 说了只给一个），
    #   结果数学题下面同时冒出"更小的一步"和"多讲两句"两块，冗余且乱。
    if subject == 'math':
        out.pop('teach', None)
    else:
        out['ladder'] = {}          # 语文/英语不用"更小的一步" 
    if q['type'] == 'fill':
        out['unit'] = (q.get('unit') or '').strip()
        out['answer'] = str(q['answer']).strip()
        # ★ 用 .get —— 语文/英语**没有** expr 字段。直接索引会 KeyError，
        #   而且是在服务端，前端只看到一个 500（真踩过）。
        if q.get('expr'):
            out['expr'] = str(q['expr']).strip()
        if q.get('accept'):
            out['accept'] = [str(a) for a in q['accept']]
    else:
        out['options'] = [str(o).strip() for o in q['options']]
        out['answer_index'] = int(q['answer_index'])
        if q.get('praise'):
            out['praise'] = {str(k): str(v) for k, v in q['praise'].items()}
    return out


def _clean_task(task: dict) -> dict:
    out = {'stem': (task.get('stem') or '').strip()}
    for k in ('unit', 'answer', 'expr'):
        if task.get(k) is not None:
            out[k] = str(task[k]).strip()
    if task.get('options'):
        out['options'] = [str(o).strip() for o in task['options']]
        out['answer_index'] = task.get('answer_index')
    return out


# ---------------------------------------------------------------- 主入口

def generate(kp: dict, n: int = 5, avoid: list = None, attempts: int = 3,
             grade: int = 6, subject: str = 'math') -> dict:
    """给一个知识点出 n 道**答案经过验证**的题。

    重出的代价是几秒 —— 但比起让孩子遇到一道答案错的题，这几秒很便宜。
    每一轮都会把上一轮**没通过校验的题**连同失败原因回给模型，让它自己修，
    比原样重来命中率高得多。
    """
    all_ok, all_bad, faces = [], [], []
    complaint = None
    for round_no in range(1, attempts + 1):
        need = n - len(all_ok)
        if need <= 0:
            break

        messages = build_messages(kp, need if round_no == 1 else need + 1, avoid,
                                  grade, subject)
        if complaint:
            messages.append({'role': 'user', 'content': complaint})
        if round_no > 1 and all_ok:
            messages.append({'role': 'user',
                             'content': '这次只出 %d 道**新的**，别和上面重复。' % need})

        payload = _extract_json(_chat(messages))
        if not faces and isinstance(payload.get('faces'), list):
            faces = [{'name': str(f.get('name', '')), 'tag': str(f.get('tag', ''))}
                     for f in payload['faces'] if isinstance(f, dict)]

        fresh_ok, fresh_bad = [], []
        for q in (payload.get('questions') or []):
            ok, reason = validate(q, subject)
            if ok:
                fresh_ok.append(sanitize(q, subject))
            else:
                fresh_bad.append((q, reason))

        # 去重：同一个题干只留一份（模型有时会自己重复）
        seen = {_stem_key(q) for q in all_ok}
        for q in fresh_ok:
            k = _stem_key(q)
            if k not in seen:
                seen.add(k)
                all_ok.append(q)
        all_bad = fresh_bad

        if len(all_ok) >= n:
            break
        # ★ 模型这一轮**一道题都没给** —— 那是偶发（实测：同一个知识点手动连出
        #   两次都好好的，第三次返回了空 questions）。这种情况要**再问一次**，
        #   不能当成"这个知识点出不了题"。下面那个 `not fresh_bad` 的早退原本
        #   会把"空回复"和"全被拒"混为一谈，直接放弃。
        if not payload.get('questions'):
            continue
        if not fresh_bad:
            break
        complaint = ('上面这些题被程序拒绝了，请重出替换（其余的直接按同样格式再给 '
                     '%d 道新的）：\n' % (n - len(all_ok))
                     + '\n'.join('- %s → %s' % (str(q.get('stem', ''))[:60], why)
                                 for q, why in fresh_bad[:6]))

    if not all_ok:
        if not all_bad:
            raise TutorError('模型连着 %d 次都没返回题目（服务是通的，多半是它这次'
                             '没按格式输出）—— 再点一次通常就好了' % attempts)
        detail = '；'.join(why for _, why in all_bad[:3])
        raise TutorError('出的题一道都没通过校验：%s' % detail)

    covered = {q.get('face') for q in all_ok if q.get('face') is not None}
    return {
        'faces': faces,
        'questions': all_ok[:n],
        'uncovered': [i for i in range(len(faces)) if i not in covered],
        'rejected': len(all_bad),
    }


def _stem_key(q: dict) -> str:
    return re.sub(r'\s+', '', str(q.get('stem', '')))[:40]


# ---------------------------------------------------------------- 自测

def _selftest() -> int:
    """不联网 —— 只验"校验器"能不能挡住坏题。"""
    bad = 0

    def expect(label, q, want_ok):
        nonlocal bad
        ok, why = validate(q)
        if ok != want_ok:
            print('  ✗ %s: 期望 %s，得到 %s（%s）' % (label, want_ok, ok, why))
            bad += 1

    good_fill = {'type': 'fill', 'stem': '485 × 8 = ___', 'expr': '485*8',
                 'answer': '3880', 'difficulty': 3,
                 'ladder': {'stem': '5 × 8 = ___', 'expr': '5*8', 'answer': '40'}}
    expect('正常填空题', good_fill, True)

    # ★ 核心用例：算式和答案对不上，必须被拦
    expect('答案算错', dict(good_fill, answer='3881'), False)
    expect('算式是错的', dict(good_fill, expr='485+8'), False)
    # ★ 全角乘号 `×` 会被 normalize 成 `*` —— **这是有意的**，模型十有八九
    #   会输出 `×`，拒掉它等于白白丢题。（第一版自测把这条写成"必须拒绝"，是错的。）
    expect('算式用全角乘号（等价，应当接受）', dict(good_fill, expr='485×8'), True)
    expect('算式里混进单位', dict(good_fill, expr='485千克*8'), False)
    expect('算式里带等号', dict(good_fill, expr='485*8=3880'), False)
    expect('答案不是数', dict(good_fill, answer='三千多'), False)
    expect('没有 expr', {k: v for k, v in good_fill.items() if k != 'expr'}, False)
    expect('没有梯子', {k: v for k, v in good_fill.items() if k != 'ladder'}, False)
    expect('难度越界', dict(good_fill, difficulty=9), False)
    expect('梯子自己对不上', dict(good_fill, ladder={'stem': 'x', 'expr': '5*8',
                                     'answer': '41'}), False)

    good_choice = {'type': 'choice', 'stem': '哪个最接近 600？',
                   'options': ['198+396', '305+298'], 'answer_index': 1,
                   'difficulty': 2, 'ladder': {'stem': '300+300 = ___',
                                               'expr': '300+300', 'answer': '600'}}
    expect('正常选择题', good_choice, True)
    expect('选项越界', dict(good_choice, answer_index=5), False)
    expect('只有一个选项', dict(good_choice, options=['a']), False)

    # 分数答案也要能验
    frac = {'type': 'fill', 'stem': '1/2 × 2 = ___', 'expr': '1/2*2',
            'answer': '1', 'difficulty': 2,
            'ladder': {'stem': '1/2 × 1 = ___', 'expr': '1/2*1', 'answer': '1/2'}}
    expect('分数答案', frac, True)
    expect('分数答案写错', dict(frac, answer='2'), False)

    # sanitize 之后必须能直接喂给 grader.grade
    clean = sanitize(good_fill)
    got, _ = grader.grade(clean, '3880')
    if not got:
        print('  ✗ sanitize 后的题用 grader 判分应该判对')
        bad += 1
    got, _ = grader.grade(clean, '3,880')
    if not got:
        print('  ✗ 千分位写法应该也判对')
        bad += 1

    # JSON 抠取容错
    for text, label in [
        ('{"a": 1}', '纯 JSON'),
        ('```json\n{"a": 1}\n```', '带代码块'),
        ('好的，这是题目：\n{"a": 1}\n希望有帮助', '前后有话'),
    ]:
        try:
            if _extract_json(text) != {'a': 1}:
                print('  ✗ _extract_json(%s) 结果不对' % label)
                bad += 1
        except TutorError as err:
            print('  ✗ _extract_json(%s) 抛错: %s' % (label, err))
            bad += 1

    print('出题器自测: %s' % ('全部通过' if bad == 0 else '%d 项失败' % bad))
    return 1 if bad else 0


if __name__ == '__main__':
    import sys
    if '--selftest' in sys.argv:
        sys.exit(_selftest())
    if '--demo' in sys.argv:
        kp = {'name': '圆的面积', 'mastery_test': '理解 S=πr² 的推导，会算圆的面积和圆环的面积',
              'unit_hint': '上册 第五单元 圆',
              'prereq_names': ['圆的周长', '圆的认识']}
        out = generate(kp, n=3)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        sys.exit(0)
    print(__doc__)
