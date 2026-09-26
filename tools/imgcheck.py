"""美术素材的技术验收。

为什么要有这个：**做这件事的模型（我）看不了图片**。所以"验一下"不能靠眼看，
得把验收标准写成能跑的检查 —— 这也正是 `docs/art-prompts.md` 第 0 节那几条
约束的意义：它们每一条都能被机器判定。

运行：python tools/imgcheck.py art/castle.png art/terrain-1-grass.png ...

★ 两类素材的标准**不一样**，按文件名分开（这条分界线很重要）：

| 类别 | 哪些文件 | 背景 | 为什么 |
|---|---|---|---|
| **独立物件** | 城堡 / 领主 / 徽记 / 图标 | **必须透明** | 直接按坐标摆在页面上。带白底 = 深色模式下是一块白板 |
| **地形** | `terrain-*.png` | **必须满幅出血** | 会被裁进岛屿形状。留了透明边 = 裁完是缺一块的 |

检查项（每条都对应提示词里的一条硬要求）：

| 检查 | 不通过意味着 |
|---|---|
| PNG | 其它格式浏览器不保证支持 |
| 正方形 | 地形要裁进方形的岛屿框，非正方形会变形 |
| 独立物件：四角全透明 | 背景不是真透明（只是"看起来白"） |
| 独立物件：透明占比 > 15% | 图铺满画布，摆上去会盖住别的东西 |
| 独立物件：内容四周留白 ≥ 3% | 贴边，缩放时容易被切 |
| 地形：不透明占比 > 97% | 有透明边 —— 裁进岛屿形状会缺一块 |
| 有深色（描边） | 缺粗描边，在浅色背景上会糊成一片 |
| 主色不偏纯白 | 纯白在深色背景上会发光刺眼 |

**判断不了**：好不好看、风格对不对、剪影像不像。那些只能人眼看
（用 /art-preview）。
"""
import sys
from collections import Counter
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit('需要 Pillow：pip install pillow')

INK = 8          # alpha 大于这个值才算"画到了"
SOLID = 250      # ★ 不写 255：生成器常把实心区写成 254（舍入），
                 #   写死 255 会得出"一个实心像素都没有"这种**假失败**
                 #   （看起来像"整张图半透明"，很吓人）。第一版就是这么虚报的。


def report(path: Path, kind: str) -> int:
    """kind: 'sprite' 或 'terrain'"""
    bad = []
    fullbleed = kind == 'terrain'
    try:
        im = Image.open(path)
    except Exception as err:
        print('  ✗ %s 打不开：%s' % (path.name, err))
        return 1

    label = '地形（满幅出血）' if fullbleed else '独立物件（透明底）'
    print('%s   【%s】' % (path.name, label))
    print('  格式 %s  模式 %s  尺寸 %dx%d' % (im.format, im.mode, im.width, im.height))

    if im.format != 'PNG':
        bad.append('不是 PNG（%s）' % im.format)
    if im.width != im.height:
        bad.append('不是正方形（%dx%d）—— 地形会被裁进方形的岛屿框，非正方形会变形'
                   % (im.width, im.height))

    has_alpha = im.mode in ('RGBA', 'LA', 'P')
    if not has_alpha and not fullbleed:
        bad.append('没有 alpha 通道（mode=%s）—— 深色模式下会是一块白板' % im.mode)
        for b in bad:
            print('  ✗ %s' % b)
        return len(bad)

    im = im.convert('RGBA')
    px = im.load()
    a = im.getchannel('A')
    hist = a.histogram()
    total = im.width * im.height
    clear = sum(hist[:INK])                       # 完全透明（或几乎）
    solid = sum(hist[SOLID:])                     # 实心（含 254 那种舍入）
    max_alpha = max(i for i, n in enumerate(hist) if n)
    clear_ratio = clear / total

    if fullbleed:
        print('  不透明占比 %.1f%%（地形要满幅，越接近 100 越好）' % ((1 - clear_ratio) * 100))
        if clear_ratio > 0.03:
            bad.append('有 %.1f%% 是透明的 —— 地形要满幅出血，'
                       '留了透明边裁进岛屿形状就会缺一块' % (clear_ratio * 100))
    else:
        corners = [(0, 0), (im.width - 1, 0), (0, im.height - 1), (im.width - 1, im.height - 1)]
        corner_alphas = [px[x, y][3] for x, y in corners]
        print('  透明像素 %.1f%%   实心像素 %.1f%%（alpha 峰值 %d）'
              % (clear_ratio * 100, solid / total * 100, max_alpha))
        print('  四角 alpha: %s' % corner_alphas)
        if max_alpha < SOLID:
            bad.append('整张图都是半透明的（alpha 峰值只有 %d）—— 贴上去会发灰' % max_alpha)
        if clear_ratio < 0.15:
            bad.append('透明区域只有 %.1f%% —— 背景不是真透明，或者图铺满了整张画布'
                       % (clear_ratio * 100))
        if max(corner_alphas) > 0:
            bad.append('四角不是全透明（最大 alpha=%d）—— 通常意味着背景没抠干净'
                       % max(corner_alphas))

    # ---- 内容分布 / 颜色 ----
    if fullbleed:
        left, top, right, bottom = 0, 0, im.width, im.height
    else:
        bbox = a.point(lambda v: 255 if v > INK else 0).getbbox()
        if not bbox:
            bad.append('整张图全是透明的（什么都没画）')
            for b in bad:
                print('  ✗ %s' % b)
            return len(bad)
        left, top, right, bottom = bbox
        margins = {'左': left / im.width, '右': (im.width - right) / im.width,
                   '上': top / im.height, '下': (im.height - bottom) / im.height}
        print('  内容框 %dx%d（四边留白 %.1f%% / %.1f%% / %.1f%% / %.1f%%）' % (
            right - left, bottom - top, margins['左'] * 100, margins['右'] * 100,
            margins['上'] * 100, margins['下'] * 100))
        tight = [k for k, v in margins.items() if v < 0.03]
        if tight:
            bad.append('内容贴边（%s 侧留白不足 3%%）—— 缩放或裁切时容易被切掉'
                       % '/'.join(tight))

    dark = opaque = 0
    colors = Counter()
    step_x = max(1, (right - left) // 140)
    step_y = max(1, (bottom - top) // 140)
    for y in range(top, bottom, step_y):
        for x in range(left, right, step_x):
            r, g, b, al = px[x, y]
            if al <= INK:
                continue
            opaque += 1
            if r + g + b < 240:
                dark += 1
            colors[(r // 24 * 24, g // 24 * 24, b // 24 * 24)] += 1

    if opaque == 0:
        bad.append('一个不透明的像素都没有')
    else:
        n_all = sum(colors.values())
        dark_ratio = dark / opaque
        whitish = sum(n for (r, g, b), n in colors.items() if r > 230 and g > 230 and b > 230)
        print('  深色像素 %.1f%%   不同色（量化后）%d 种'
              % (dark_ratio * 100, len(colors)))
        print('  主色: %s' % '  '.join(
            '#%02X%02X%02X×%.0f%%' % (r, g, b, n / n_all * 100)
            for (r, g, b), n in colors.most_common(4)))
        if dark_ratio < 0.03:
            bad.append('几乎没有深色（%.1f%%）—— 缺粗描边/深色结构，'
                       '在浅色背景上会糊成一片' % (dark_ratio * 100))
        if whitish / n_all > 0.55:
            bad.append('主色偏纯白（%.0f%%）—— 深色背景上会发光刺眼'
                       % (whitish / n_all * 100))
        # 地形要有内容和层次，一块纯色贴上去还是"简陋"
        if fullbleed and len(colors) < 12:
            bad.append('整张只有 %d 种颜色（量化后）—— 像一块纯色，'
                       '贴上去地图还是空的，请加地面纹理和四周的景物' % len(colors))

    if bad:
        for b in bad:
            print('  ✗ %s' % b)
    else:
        print('  ✓ 技术验收通过（好不好看要靠人眼，这里判断不了）')
    return len(bad)


def kind_of(path: Path) -> str:
    return 'terrain' if path.name.startswith('terrain-') else 'sprite'


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit('用法：python tools/imgcheck.py <图片> [更多图片...]')
    bad = 0
    for p in sys.argv[1:]:
        f = Path(p)
        if not f.is_file():
            print('%s\n  ✗ 文件不存在' % f.name)
            bad += 1
            continue
        bad += report(f, kind_of(f))
        print()
    if bad:
        print('%d 项不合格' % bad)
        return 1
    print('全部技术验收通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
