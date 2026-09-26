"""判分引擎 —— 能确定性判的，绝不问 LLM。

为什么要单独一个模块、为什么这么较真：

小学数学题的答案几乎全是**确定的数**。选择比对选项、填空规范化匹配、计算用
符号求值，都能做到 100% 准确而且免费。把"判断对错"交给概率模型，代价是它
会算错而且错得很自信 —— 而孩子会照单全收。

铁律三条：

1. **绝不用浮点。** `1/3 * 3` 必须等于 1，不是 0.9999999999999999。
   全程 `Fraction`。
2. **判分是纯函数** —— 不碰网络、不碰文件、不读时间、不读环境变量。
   它必须能在一秒内跑几百个用例。
3. **孩子写对了却被判错，比判错更伤。** 所以长尾写法要尽量吃下：
   全角、千分位、"3880千克"、`二分之一`、`85%`……见文件末尾的自测清单。

`eval_expr` 是给**出题器**用的：模型出了题之后，用它把自己的算式重算一遍，
和它给的答案比对。对不上就丢弃重出 —— 这样孩子才不会遇到一道"答案写错了"
的题。它是这套设计里唯一不能省掉的机关。
"""
import ast
import re
from fractions import Fraction

# ---------------------------------------------------------------- 规范化

# 全角 → 半角。孩子用中文输入法时，"８６１"、"１／２"、"，" 都很常见。
_FULLWIDTH = {chr(0xFF01 + i): chr(0x21 + i) for i in range(94)}
_FULLWIDTH.update({
    '。': '.', '，': ',', '％': '%', '／': '/', '－': '-', '＋': '+',
    '（': '(', '）': ')', '　': ' ', '·': '.', '×': '*', '÷': '/',
})


def _to_halfwidth(text: str) -> str:
    """全角转半角，**保留空白**。

    和 normalize 分开是因为 eval_expr 要用它：表达式里的空白是语法的一部分
    （`1 if x else 2` 去掉空白会变成 `1ifxelse2`，被 Python 语法分析器当成
    "invalid decimal literal" —— 虽然结果一样是被拒绝，但那是一条脏路径）。
    """
    return ''.join(_FULLWIDTH.get(ch, ch) for ch in str(text))


def normalize(text: str) -> str:
    """全角转半角，去掉所有空白。用于解析孩子的答案。"""
    if text is None:
        return ''
    return re.sub(r'\s+', '', _to_halfwidth(text))


# 句读和括号：孩子写答案时带不带都不该影响判分
_PUNCT = '。．.,，、；;：:！!？?「」『』“”‘’（）()[]【】<>《》\'"`·-—_'


def norm_text(text) -> str:
    """文本答案的规范化（语文 / 英语用）。

    去空白、去首尾标点、转半角、统一小写 —— 但不做任何"意思上"的宽容：
    它**不判断同义**。"潮来前" 和 "潮来之前" 在它眼里是两回事，
    所以语文题必须出成**答案唯一**的形式，有一堆合理说法的就该出成选择题。
    """
    s = re.sub(r'[\s　]+', '', _to_halfwidth(str(text or '')))
    return s.strip(_PUNCT).lower()


# ---------------------------------------------------------------- 中文数词

_CN_DIGIT = {'零': 0, '〇': 0, '一': 1, '壹': 1, '二': 2, '两': 2, '贰': 2,
             '三': 3, '叁': 3, '四': 4, '肆': 4, '五': 5, '伍': 5,
             '六': 6, '陆': 6, '七': 7, '柒': 7, '八': 8, '捌': 8, '九': 9, '玖': 9}
_CN_UNIT = {'十': 10, '拾': 10, '百': 100, '佰': 100, '千': 1000, '仟': 1000}
_CN_BIG = {'万': 10000, '亿': 100000000}


def parse_cn_numeral(s: str):
    """解析中文数词：三千八百八十 → 3880，二十五 → 25，十五 → 15。

    认不出来返回 None（**不要猜** —— 猜错了就是凭空判对）。
    """
    if not s:
        return None
    total, section, number = 0, 0, 0
    seen = False
    for ch in s:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
            seen = True
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            # "十五" 里的十前面没有数字，按 1 算
            section += (number or 1) * unit
            number = 0
            seen = True
        elif ch in _CN_BIG:
            section = (section + number) * _CN_BIG[ch]
            total += section
            section, number = 0, 0
            seen = True
        else:
            return None
    if not seen:
        return None
    return Fraction(total + section + number)


# ---------------------------------------------------------------- 数值解析

# 尾部单位：逗号后缀里连续的中文字符（"3880千克" 的 "千克"）。
# 只剥**尾部**，且剥完必须还剩数字，否则不动 —— 避免把 "分之" 剥坏。
_TRAILING_UNIT = re.compile(r'[^\d)）]+$')


def _parse_arabic(s: str):
    """1/2 · 0.5 · 861 · 3,880。用 Fraction 的字符串构造，天然精确。"""
    s = s.replace(',', '')
    if not s:
        return None
    try:
        return Fraction(s)
    except (ValueError, ZeroDivisionError):
        pass
    # 带分数 3又1/2 或 3 1/2
    m = re.fullmatch(r'(\d+)又(\d+)/(\d+)', s)
    if m:
        whole, num, den = (int(x) for x in m.groups())
        if den == 0:
            return None
        return Fraction(whole) + Fraction(num, den)
    return None


def _parse_core(s: str):
    """不带单位的核心解析。返回 Fraction 或 None。"""
    if not s:
        return None

    percent = s.endswith('%')
    if percent:
        s = s[:-1]

    value = None

    # 带分数（中文）：三又二分之一
    m = re.fullmatch(r'(.+?)又(.+?)分之(.+)', s)
    if m:
        whole, den, num = (_parse_core(x) for x in m.groups())
        if whole is not None and den not in (None, 0) and num is not None:
            value = whole + num / den

    # 中文分数：二分之一 = 1/2（"A分之B" 读作 B/A）
    if value is None:
        m = re.fullmatch(r'(.+?)分之(.+)', s)
        if m:
            den, num = (_parse_core(x) for x in m.groups())
            if den not in (None, 0) and num is not None:
                value = num / den

    # 阿拉伯数字 / 小数 / 分数
    if value is None:
        value = _parse_arabic(s)

    # 纯中文数词
    if value is None:
        value = parse_cn_numeral(s)

    if value is not None and percent:
        value = value / 100
    return value


def parse_answer(text):
    """把孩子的答案解析成 Fraction。认不出来返回 None。

    接受：861 · 3,880 · 3880千克 · 1/2 · 0.5 · 二分之一 · 三又二分之一 ·
          85% · ３０００米 · 负号
    """
    s = normalize(text).rstrip('。.')
    if not s:
        return None

    value = _parse_core(s)
    if value is not None:
        return value

    # 剥掉尾部单位再试一次。只在**整体解析失败**时才做，
    # 所以不会把 "二分之一" 里的字剥掉（它整体就解析成功了）。
    stripped = _TRAILING_UNIT.sub('', s)
    if stripped and stripped != s:
        return _parse_core(stripped)
    return None


# ---------------------------------------------------------------- 表达式求值

def eval_expr(expr: str) -> Fraction:
    """安全地算一个算术表达式，精确到分数。

    给**出题器**用：模型给了算式和答案，这里重算一遍对不上就丢弃重出。

    用 ast 而不是 eval —— 这是从模型来的字符串，虽然大概率无害，
    但"大概率"不是写在这里的理由。只放行数字和四则运算。
    """
    if not expr or not isinstance(expr, str):
        raise ValueError('空表达式')
    # 中文全角、×÷ 统一换成半角运算符。**不去空白** —— 空白是表达式语法的一部分。
    s = _to_halfwidth(expr).strip()
    if '%' in s:
        raise ValueError('表达式里不该有百分号：%s' % expr)
    try:
        tree = ast.parse(s, mode='eval')
    except SyntaxError as exc:
        raise ValueError('表达式解析不了：%s' % expr) from exc
    return _eval_node(tree.body)


def _eval_node(node) -> Fraction:
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError('表达式里有非数字常量')
        # float 走字符串构造，保持十进制精确（0.1 就是 1/10，不是二进制近似）
        return Fraction(v) if isinstance(v, int) else Fraction(str(v))
    if isinstance(node, ast.UnaryOp):
        if isinstance(node.op, ast.USub):
            return -_eval_node(node.operand)
        if isinstance(node.op, ast.UAdd):
            return _eval_node(node.operand)
        raise ValueError('不支持的运算符')
    if isinstance(node, ast.BinOp):
        left, right = _eval_node(node.left), _eval_node(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            if right == 0:
                raise ValueError('除数为零')
            return left / right
        raise ValueError('不支持的运算符')
    raise ValueError('表达式里有不允许的东西：%s' % type(node).__name__)


# ---------------------------------------------------------------- 判分

def grade(question: dict, answer: str):
    """判一道题。返回 (对错, 说明)。

    `question` 至少要含 type 和答案；选择题用 answer_index。
    这是纯函数 —— 同样输入永远同样输出。
    """
    kind = question.get('type')
    raw = normalize(answer)
    if not raw:
        return False, '没有作答'

    if kind == 'choice':
        index = question.get('answer_index')
        letter = question.get('answer')          # 也可以给 "B" / "1"
        got = _choice_index(raw)
        if got is None:
            return False, '这题要选一个选项'
        if index is not None:
            return (got == index), ('选对了' if got == index else '选错了')
        if letter is not None:
            want = _choice_index(normalize(str(letter)))
            return (got == want), ('选对了' if got == want else '选错了')
        return False, '题目缺答案'

    # 填空：题目期望的可能是**数值**（数学）也可能是**文本**（语文/英语）。
    # 先看是哪种 —— 拿数值那套去解 "潮来前" 只会得到"请写数字"，很荒唐。
    candidates = _expected_values(question)
    if candidates:
        got = parse_answer(raw)
        if got is None:
            return False, '这个答案我没看懂（请写数字）'
        # 单位不一致要单独说 —— 这是"数对了但单位错"，比全错更该被指出来
        unit_msg = _unit_mismatch(question, raw)
        if got in candidates:
            if unit_msg:
                return False, unit_msg
            return True, '正确'
        return False, '不对'

    # 文本答案：规范化后严格相等。**不做同义判断** ——
    # "潮来前" 和 "潮来之前" 是两回事，所以语文题必须出成答案唯一的形式。
    wants = _expected_texts(question)
    if not wants:
        return False, '题目缺答案'
    return (norm_text(answer) in wants), ('正确' if norm_text(answer) in wants else '不对')


def _expected_values(question: dict):
    """题目认可的所有数值。accept 是额外接受的形式（比如 "3,880"）。"""
    out = []
    for key in ('answer', 'accept'):
        val = question.get(key)
        if val is None:
            continue
        items = val if isinstance(val, (list, tuple)) else [val]
        for item in items:
            if isinstance(item, (int, float)) and not isinstance(item, bool):
                out.append(Fraction(str(item)))
                continue
            parsed = parse_answer(str(item))
            if parsed is not None:
                out.append(parsed)
    return out


def _expected_texts(question: dict):
    """题目认可的文本答案（规范化后）。语文 / 英语用。"""
    out = set()
    for key in ('answer', 'accept'):
        val = question.get(key)
        if val is None:
            continue
        items = val if isinstance(val, (list, tuple)) else [val]
        for item in items:
            t = norm_text(item)
            if t:
                out.add(t)
    return out


def _choice_index(text: str):
    """把选项答案认成 0 基下标。

    ★ 格式约定（踩过一次，写在这里免得再踩）：**字母是首选格式**，"B" → 1。
    数字只为人手输入保留，按**1 基**理解（"1" 是第一个选项）。

    真实事故：前端原来传的是 0 基下标（第二个选项传 "1"），而这里按 1 基
    理解，于是**差一位** —— 孩子选了 B，被判成 A、判错，而"正确答案"显示的
    正是 B。表现就是"我明明选对了它说我错"。前端现在改传字母了。

    另外**负数一律当无效**：传 "0" 时老代码算出 -1，然后静默判错，
    同样是"选对被判错"的样子。宁可说一句"没看懂"，也别猜。
    """
    m = re.search(r'[A-Da-d]', text)
    if m:
        return ord(m.group(0).upper()) - ord('A')
    m = re.search(r'\d+', text)
    if m:
        n = int(m.group(0)) - 1
        return n if n >= 0 else None
    return None


def _unit_mismatch(question: dict, answer: str):
    """数对了但单位写了别的 —— 返回提示语；没问题返回 None。

    只做**同一单位内**的检查，不做单位换算：题目问"= ___米"，孩子写
    "3千米" 就是错的（数值 3 ≠ 3000，本来也判错）。这里管的是
    "3000千米" 这种 —— 数对了，单位多写错了一个。
    """
    unit = (question.get('unit') or '').strip()
    if not unit or unit in '。.．':          # 纯标点 = 题目没有单位
        return None
    m = re.search(r'[^\d.,%\s]+$', answer)   # 答案末尾的非数字部分
    if not m:
        return None
    wrote = m.group(0)
    # ★ 必须**严格相等**，不能用子串判断 —— "米" 是 "千米" 的子串，
    #   写成 `unit in wrote` 会把 "3000千米" 判成对的（自测抓到的）。
    if wrote != unit:
        return '数对了，但单位应该是「%s」' % unit
    return None


# ---------------------------------------------------------------- 自测

_SELFTEST = [
    # (输入, 期望的 Fraction 字符串或 None)
    ('861', '861'), ('3,880', '3880'), ('3880千克', '3880'),
    ('３０００米', '3000'), (' 3000 ', '3000'), ('3000。', '3000'),
    ('1/2', '1/2'), ('0.5', '1/2'), ('０.５', '1/2'),
    ('二分之一', '1/2'), ('三分之一', '1/3'), ('四分之三', '3/4'),
    ('三又二分之一', '7/2'), ('２又１／２', '5/2'),
    ('85%', '17/20'), ('100%', '1'), ('0.85', '17/20'),
    ('三千八百八十', '3880'), ('二十五', '25'), ('十五', '15'),
    ('一百零五', '105'), ('一万', '10000'),
    ('-3', '-3'), ('－3', '-3'),
    ('不知道', None), ('', None), ('妈妈', None),
]

_SELFTEST_EXPR = [
    ('476+385', '861'), ('23*12', '276'), ('25*36', '900'),
    ('1/3*3', '1'), ('96/4', '24'), ('(1+2)*3', '9'),
    ('0.1+0.2', '3/10'),          # 浮点会给出 0.30000000000000004
    ('3-5', '-2'), ('480/8', '60'),
]


def _selftest() -> int:
    bad = 0
    for text, want in _SELFTEST:
        got = parse_answer(text)
        ok = (got is None and want is None) or \
             (got is not None and want is not None and got == Fraction(want))
        if not ok:
            print('  ✗ parse_answer(%r) = %s，期望 %s' % (text, got, want))
            bad += 1
    print('parse_answer: %d/%d 通过' % (len(_SELFTEST) - bad, len(_SELFTEST)))

    bad2 = 0
    for expr, want in _SELFTEST_EXPR:
        try:
            got = eval_expr(expr)
        except ValueError as exc:
            print('  ✗ eval_expr(%r) 抛错: %s' % (expr, exc))
            bad2 += 1
            continue
        if got != Fraction(want):
            print('  ✗ eval_expr(%r) = %s，期望 %s' % (expr, got, want))
            bad2 += 1
    print('eval_expr:   %d/%d 通过' % (len(_SELFTEST_EXPR) - bad2, len(_SELFTEST_EXPR)))

    # eval_expr 必须是**拒绝**这些的
    for evil in ('__import__("os").system("ls")', '1 if 1 else 2', 'open("x")',
                 '[1,2]', '1%2', 'x+1'):
        try:
            eval_expr(evil)
            print('  ✗ eval_expr 竟然放行了: %r' % evil)
            bad2 += 1
        except ValueError:
            pass
    print('eval_expr 拒绝非算术输入: 通过')

    # 判分端到端
    q = {'type': 'fill', 'answer': '861', 'unit': '。', 'accept': ['3,880']}
    cases = [
        ({'type': 'fill', 'answer': '861'}, '861', True),
        ({'type': 'fill', 'answer': '861'}, '861千克', True),   # 单位不匹配但题目没单位
        ({'type': 'fill', 'answer': '861'}, '862', False),
        ({'type': 'fill', 'answer': '861'}, '不知道', False),
        ({'type': 'fill', 'answer': '1/2'}, '0.5', True),
        ({'type': 'fill', 'answer': '1/2'}, '二分之一', True),
        ({'type': 'fill', 'answer': '3000', 'unit': '米'}, '3000米', True),
        ({'type': 'fill', 'answer': '3000', 'unit': '米'}, '3000千米', False),
        ({'type': 'fill', 'answer': '3000', 'unit': '米'}, '3千米', False),
        # —— 选项答案的两种格式 ——
        # 字母：前端用的（也是首选），'B' 就是第二个
        ({'type': 'choice', 'answer_index': 1}, 'B', True),
        ({'type': 'choice', 'answer_index': 1}, 'b', True),
        ({'type': 'choice', 'answer_index': 0}, 'A', True),
        ({'type': 'choice', 'answer_index': 2}, 'C', True),
        ({'type': 'choice', 'answer_index': 1}, 'A', False),
        ({'type': 'choice', 'answer_index': 1}, 'C', False),
        # 数字：只给人手输入，按 1 基（'2' 是第二个）
        ({'type': 'choice', 'answer_index': 1}, '2', True),
        # ★ 下面这条是**回归用例**。真实事故：前端传 0 基下标（"1" 表示第二个），
        #   服务端按 1 基理解成"第一个"，于是孩子选了 B 被判错，
        #   而"正确答案"显示的正是 B。前端现在改传字母，这条锁住那个语义。
        ({'type': 'choice', 'answer_index': 1}, '1', False),
    ]
    bad3 = 0
    for question, ans, want in cases:
        got, why = grade(question, ans)
        if got != want:
            print('  ✗ grade(%s, %r) = %s(%s)，期望 %s'
                  % (question, ans, got, why, want))
            bad3 += 1
    print('grade:       %d/%d 通过' % (len(cases) - bad3, len(cases)))

    # 越界/负下标必须是"没看懂"，不能静默判错 —— "选对被判错"就是这么来的
    ok, why = grade({'type': 'choice', 'answer_index': 1}, '0')
    if ok or '选项' not in why:
        print('  ✗ 传 "0" 应当是无效输入（%s），不是静默判错（%s）' % (why, ok))
        bad3 += 1
    else:
        print('越界下标报"没看懂"而不是"选错": 通过')

    total_bad = bad + bad2 + bad3
    print()
    print('全部通过' if total_bad == 0 else '%d 项失败' % total_bad)
    return 1 if total_bad else 0


if __name__ == '__main__':
    import sys
    if '--selftest' in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
