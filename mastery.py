"""掌握度 —— 把"答对次数"换成"隔了多久还记得"。

为什么不能用答对次数：

孩子把一科刷一遍，整张图就全深了。一次答对说明不了任何事 —— 可能是刚讲完
还热着，可能是蒙的，可能是背下了答案。**颜色必须由"记忆能撑多久"算出来**，
而不是"对了几道"。

算法用 SM-2（Anki 用了十几年的那个）。没上 FSRS 的理由很具体：FSRS 有 17 个
**必须用真实复习历史拟合**的权重参数，我现在一条真实数据都没有，套一组默认值
等于假装精确。SM-2 参数少、经得起推敲、每一项都能单测。等攒够数据再换，
接口不用动（就是下面 `update()` 一个函数）。

**回答速度在这里的位置**（这是设计上最容易做错的一处）：

    速度**不换算成分数**。快 ≠ 会（可能是蒙的），慢 ≠ 不会（数学需要想的
    时间）。它的正确用途是把"答对"这件事**细分**成三档：

        答错            -> Again
        答对但慢        -> Hard
        答对、正常      -> Good
        答对且快        -> Easy

    快慢的基准是**这个孩子自己在该知识点上的历史中位数**，不是绝对秒数 ——
    同一个知识点，他上次做同类题用了多久，才是有意义的参照。

还有一条：**别给孩子看计时器**。倒计时会让人焦虑，而数学恰恰需要想的时间。
计时在后台做，反馈只用正向说法（"比上次快了"）。
"""
import datetime
import math

# 四档评级。数值就是 SM-2 的质量分（q<3 视为答错）。
AGAIN, HARD, GOOD, EASY = 1, 2, 3, 4
_RATING_Q = {AGAIN: 2, HARD: 3, GOOD: 4, EASY: 5}

EF_MIN = 1.3
EF_START = 2.5

# 中等难度（difficulty=3）下，孩子答一道题的典型用时。第一次见面没有历史，
# 用这个兜底；之后全部由他自己的实际用时校准。
DEFAULT_PACE = 35.0
PACE_RATE = 2 / 3.0        # 快档阈值：不到期望时间的 2/3
HARD_AT = 1 / 0.6          # 慢档阈值：超过期望时间的 1/0.6 ≈ 1.67 倍


# ---------------------------------------------------------------- 速度 → 评级

def expected_seconds(card: dict, difficulty: int = 3) -> float:
    """这道题"正常"该花多久。基准是这个孩子在**这个知识点**上的节奏。"""
    pace = (card or {}).get('pace') or DEFAULT_PACE
    # 难度 3 为基准；难题给更多时间。夹在 0.5~2 倍之间，别让 LLM 标的难度乱飞。
    factor = min(2.0, max(0.5, (difficulty or 3) / 3.0))
    return pace * factor


def rating_from(correct: bool, elapsed, expected) -> int:
    """把 (对错, 用时) 变成四档。用时缺失就退成 GOOD —— 不猜。"""
    if not correct:
        return AGAIN
    if not elapsed or not expected or expected <= 0:
        return GOOD
    ratio = elapsed / expected
    if ratio <= PACE_RATE:
        return EASY
    if ratio >= HARD_AT:
        return HARD
    return GOOD


def update_pace(card: dict, elapsed, difficulty: int = 3) -> float:
    """用本次用时校准节奏基准。

    **只在答对时更新** —— 答错的用时（愣住、乱试）不代表他的正常节奏，
    让它进来会把基准带偏。用指数滑动，单次影响不超过三分之一。
    """
    pace = (card or {}).get('pace') or DEFAULT_PACE
    if not elapsed or elapsed <= 0:
        return pace
    factor = min(2.0, max(0.5, (difficulty or 3) / 3.0))
    observed = elapsed / factor
    # 单次样本噪音很大，压到 1/3 权重；同时防止飙到荒谬的值
    observed = min(600.0, max(3.0, observed))
    return pace * (1 - 1 / 3.0) + observed * (1 / 3.0)


# ---------------------------------------------------------------- SM-2

def new_card() -> dict:
    return {'reps': 0, 'lapses': 0, 'ef': EF_START, 'interval': 0,
            'due': None, 'last': None, 'pace': None, 'seen': 0}


def update(card: dict, rating: int, today: datetime.date = None) -> dict:
    """复习一次，返回**新**卡片（纯函数，不改传入的那个）。"""
    today = today or datetime.date.today()
    card = dict(new_card(), **(card or {}))
    q = _RATING_Q.get(rating, 4)

    card['seen'] = card.get('seen', 0) + 1
    card['last'] = today.isoformat()

    if q < 3:
        # 答错：重复次数归零，明天再来。EF 照降 —— 这题对他确实更难。
        card['reps'] = 0
        card['lapses'] = card.get('lapses', 0) + 1
        interval = 1
    else:
        card['reps'] = card.get('reps', 0) + 1
        if card['reps'] == 1:
            interval = 1
        elif card['reps'] == 2:
            interval = 6
        else:
            # ★ 上一次的 interval 必须 >= 1，否则乘完还是 0，卡片永远出不来
            interval = max(1, round(max(1, card.get('interval') or 1) * card['ef']))

    # EF 按 SM-2 原式更新，下限 1.3
    ef = card.get('ef', EF_START) + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    card['ef'] = max(EF_MIN, ef)
    card['interval'] = interval
    card['due'] = (today + datetime.timedelta(days=interval)).isoformat()
    return card


# ---------------------------------------------------------------- 颜色 / 队列

def strength(card: dict) -> float:
    """0~1，图上颜色用。**由间隔算，不由答对次数算。**

    这是整个设计里最要紧的一条区别：刷一遍不会让图变深，只有隔了很久还记得
    才会。21 天封顶 —— 再长对"小学一个学期"来说没有额外信息。
    """
    if not card or not card.get('seen'):
        return 0.0
    interval = max(0, card.get('interval') or 0)
    if interval <= 0:
        return 0.0
    return min(1.0, math.log1p(interval) / math.log1p(21))


def is_due(card: dict, today: datetime.date = None) -> bool:
    if not card or not card.get('due'):
        return False
    today = today or datetime.date.today()
    try:
        return datetime.date.fromisoformat(card['due']) <= today
    except ValueError:
        return False


def describe(card: dict, today: datetime.date = None) -> str:
    """给人看的一句话。不给孩子看分数，只描述下次什么时候再来。"""
    if not card or not card.get('seen'):
        return '还没测过'
    today = today or datetime.date.today()
    if is_due(card, today):
        return '今天该复习了'
    days = (datetime.date.fromisoformat(card['due']) - today).days
    if days <= 1:
        return '明天再见一次'
    return '%d 天后再见一次' % days


# ---------------------------------------------------------------- 自测

def _selftest() -> int:
    bad = 0
    d0 = datetime.date(2026, 9, 21)

    def check(label, got, want):
        nonlocal bad
        if got != want:
            print('  ✗ %s = %r，期望 %r' % (label, got, want))
            bad += 1

    # ---- 速度四档 ----
    check('答错 -> Again', rating_from(False, 5, 35), AGAIN)
    check('答对很快 -> Easy', rating_from(True, 10, 35), EASY)
    check('答对正常 -> Good', rating_from(True, 35, 35), GOOD)
    check('答对很慢 -> Hard', rating_from(True, 90, 35), HARD)
    check('没计时 -> Good', rating_from(True, None, 35), GOOD)

    # ---- SM-2 主链：连续答对，间隔必须递增 ----
    card = new_card()
    intervals = []
    for i in range(5):
        card = update(card, GOOD, d0 + datetime.timedelta(days=i))
        intervals.append(card['interval'])
    check('连续答对的间隔', intervals, [1, 6, 15, 38, 95])
    if not all(b > a for a, b in zip(intervals, intervals[1:])):
        print('  ✗ 间隔不是严格递增: %r' % intervals)
        bad += 1

    # ---- 答错必须拉回到明天 ----
    card2 = update(card, AGAIN, d0)
    check('答错后间隔', card2['interval'], 1)
    check('答错后 reps 归零', card2['reps'], 0)
    check('答错计入 lapses', card2['lapses'], 1)
    check('答错后 due 是明天', card2['due'], (d0 + datetime.timedelta(days=1)).isoformat())

    # ---- EF 有下限，且 Easy 会涨 / Hard 会跌 ----
    c = new_card()
    c = update(c, EASY, d0); easy_ef = c['ef']
    c2 = new_card()
    c2 = update(c2, HARD, d0); hard_ef = c2['ef']
    if not easy_ef > EF_START > hard_ef:
        print('  ✗ EF 方向不对: easy=%s start=%s hard=%s' % (easy_ef, EF_START, hard_ef))
        bad += 1
    c3 = new_card()
    for i in range(30):
        c3 = update(c3, AGAIN, d0)
    check('EF 下限', c3['ef'] >= EF_MIN, True)

    # ---- 颜色由间隔算，不由答对次数算 ----
    fresh = new_card()
    check('没测过 = 0', strength(fresh), 0.0)
    once = update(new_card(), GOOD, d0)
    many = new_card()
    reps_only = dict(new_card())
    for i in range(5):
        reps_only = update(reps_only, AGAIN, d0)   # 一直答错：次数多，间隔永远 1
    if strength(reps_only) > strength(once):
        print('  ✗ 刷次数不该让颜色变深：答错5次(%.3f) 不该比答对1次(%.3f) 深'
              % (strength(reps_only), strength(once)))
        bad += 1
    # ★ 这才是真正的对照：见过的次数和颜色的深浅**必须无关**。
    #   只见过 1 次但撑住了 21 天的，要比见过 20 次但每次都只撑 1 天的深得多。
    spaced = dict(new_card(), seen=1, reps=1, interval=21, due='2026-10-12')
    crammed = dict(new_card(), seen=20, reps=20, interval=1, due='2026-09-22')
    if not strength(spaced) > strength(crammed) * 2:
        print('  ✗ 颜色仍被"见过多少次"影响：间隔21天/见1次=%.3f，'
              '间隔1天/见20次=%.3f' % (strength(spaced), strength(crammed)))
        bad += 1
    if not strength(spaced) > strength(crammed):
        bad += 1
    # 隔了很久还记得 -> 明显更深
    long_card = dict(new_card(), seen=5, reps=5, interval=60, due='2026-12-01')
    if not strength(long_card) > 0.9:
        print('  ✗ 60 天间隔应该接近满格，实际 %.3f' % strength(long_card))
        bad += 1

    # ---- 纯函数：不该改传入的卡片 ----
    original = dict(card)
    update(card, AGAIN, d0)
    if card != original:
        print('  ✗ update() 改了传入的卡片 —— 必须是纯函数')
        bad += 1

    # ---- 到期判断 ----
    check('刚答完的不算到期', is_due(update(new_card(), GOOD, d0), d0), False)
    check('明天到期', is_due(update(new_card(), GOOD, d0), d0 + datetime.timedelta(days=1)), True)

    # ---- pace 自适应：答得比基准快，基准要往下走 ----
    p0 = expected_seconds(new_card(), 3)
    p1 = update_pace(new_card(), 5, 3)     # 5 秒做完一道中等题
    if not p1 < p0:
        print('  ✗ 答得快时节奏基准应该变小：%.1f -> %.1f' % (p0, p1))
        bad += 1
    check('难度影响期望时间', expected_seconds({'pace': 30}, 5) > expected_seconds({'pace': 30}, 1), True)

    print('掌握度自测: %s' % ('全部通过' if bad == 0 else '%d 项失败' % bad))
    return 1 if bad else 0


if __name__ == '__main__':
    import sys
    if '--selftest' in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
