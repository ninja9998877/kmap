"""美术素材的技术验收。

为什么要有这个：**做这件事的模型（我）看不了图片**。所以"验一下"不能靠眼看，
得把验收标准写成能跑的检查 —— 这也正是 `docs/art-prompts.md` 第 0 节那几条
约束的意义：它们每一条都能被机器判定。

运行：python tools/imgcheck.py art/castle.png [art/lord-math.png ...]

检查项（每一条都对应提示词里的一条硬要求）：

| 检查 | 不通过意味着 |
|---|---|
| PNG 且带 alpha 通道 | 深色模式下会是一块白板 |
| 四角必须全透明 | 背景不是真透明（只是"看起来白"） |
| 透明像素占比 > 15% | 图铺满了整张画布 —— 贴上去会盖住别的东西 |
| 内容在安全区内（四周留白 ≥ 3%） | 内容贴边，被裁进岛屿形状时会被切掉 |
| 内容区"有深色" | 没描边，在浅色背景上会糊成一片 |
| 主色不接近纯白 | 纯白在深色背景上会发光刺眼 |

**判断不了**：好不好看、风格对不对、剪影像不像。那些只能人眼看。
"""
import sys
from collections import Counter
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit('需要 Pillow：pip install pillow')

# 内容判定的阈值：alpha 大于这个值才算"画到了"
INK = 8


def report(path: Path) -> int:
    bad = []
    try:
        im = Image.open(path)
    except Exception as err:
        print('  ✗ %s 打不开：%s' % (path.name, err))
        return 1

    print('%s' % path.name)
    print('  格式 %s  模式 %s  尺寸 %dx%d' % (im.format, im.mode, im.width, im.height))
    if im.format != 'PNG':
        bad.append('不是 PNG（%s）' % im.format)
    if im.mode not in ('RGBA', 'LA', 'P'):
        bad.append('没有 alpha 通道（mode=%s）' % im.mode)
        print('  ✗ 没有 alpha —— 深色模式下会是一块白板')
        return 1

    im = im.convert('RGBA')
    a = im.getchannel('A')
    px = im.load()
    total = im.width * im.height

    hist = a.histogram()
    clear = sum(hist[:INK])                     # 完全透明（或几乎）
    # ★ 判"实心"用 ≥250 而不是 ==255。生成器常把实心区写成 254
    #   （舍入），写死 255 就会得出"一个实心像素都没有"这种**假失败** ——
    #   第一版就是这么虚报的，而它看起来像"整张图是半透明的"，很吓人。
    solid = sum(hist[250:])
    max_alpha = max(i for i, n in enumerate(hist) if n)

    # 四角
    corners = [(0, 0), (im.width - 1, 0), (0, im.height - 1), (im.width - 1, im.height - 1)]
    corner_alphas = [px[x, y][3] for x, y in corners]
    print('  透明像素 %.1f%%   实心像素 %.1f%%（alpha 峰值 %d）'
          % (clear / total * 100, solid / total * 100, max_alpha))
    print('  四角 alpha: %s' % corner_alphas)

    # 真的半透明（而不是舍入）才报
    if max_alpha < 250:
        bad.append('整张图都是半透明的（alpha 峰值只有 %d）—— 贴上去会发灰' % max_alpha)

    if clear / total < 0.15:
        bad.append('透明区域只有 %.1f%% —— 背景不是真透明，或者图铺满了整张画布'
                   % (clear / total * 100))
    if max(corner_alphas) > 0:
        bad.append('四角不是全透明（最大 alpha=%d）—— 这通常意味着背景没抠干净'
                   % max(corner_alphas))

    # 内容外接框 + 留白
    bbox = a.point(lambda v: 255 if v > INK else 0).getbbox()
    if not bbox:
        bad.append('整张图全是透明的（什么都没画）')
        print('  ✗ 全透明')
        return len(bad)
    left, top, right, bottom = bbox
    margins = {
        '左': left / im.width, '右': (im.width - right) / im.width,
        '上': top / im.height, '下': (im.height - bottom) / im.height,
    }
    print('  内容框 %dx%d（左右留白 %.1f%% / %.1f%%，上下 %.1f%% / %.1f%%）' % (
        right - left, bottom - top,
        margins['左'] * 100, margins['右'] * 100, margins['上'] * 100, margins['下'] * 100))
    tight = [k for k, v in margins.items() if v < 0.03]
    if tight:
        bad.append('内容贴边（%s 侧留白不足 3%%）—— 裁进岛屿形状时会被切掉'
                   % '/'.join(tight))

    # 内容区里有没有深色（描边），以及主色是不是接近纯白
    dark = 0
    opaque = 0
    colors = Counter()
    for y in range(top, bottom, max(1, (bottom - top) // 120)):
        for x in range(left, right, max(1, (right - left) // 120)):
            r, g, b, al = px[x, y]
            if al <= INK:
                continue
            opaque += 1
            if r + g + b < 240:
                dark += 1
            colors[(r // 24 * 24, g // 24 * 24, b // 24 * 24)] += 1
    if opaque == 0:
        bad.append('内容区一个不透明的像素都没有')
    else:
        ratio = dark / opaque
        print('  内容区深色像素 %.1f%%' % (ratio * 100))
        if ratio < 0.05:
            bad.append('内容区几乎没有深色（%.1f%%）—— 缺粗描边，在浅色背景上会糊成一片'
                       % (ratio * 100))
        top_colors = colors.most_common(4)
        print('  主色（量化后）: %s' % '  '.join(
            '#%02X%02X%02X×%.0f%%' % (r, g, b, n / max(1, sum(colors.values())) * 100)
            for (r, g, b), n in top_colors))
        whitish = sum(n for (r, g, b), n in colors.items() if r > 230 and g > 230 and b > 230)
        if sum(colors.values()) and whitish / sum(colors.values()) > 0.55:
            bad.append('主色偏纯白（%.0f%%）—— 深色背景上会发光刺眼'
                       % (whitish / sum(colors.values()) * 100))

    if bad:
        for b in bad:
            print('  ✗ %s' % b)
        return len(bad)
    print('  ✓ 技术验收通过（好不好看要靠人眼，这里判断不了）')
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        sys.exit('用法：python tools/imgcheck.py <图片> [更多图片...]')
    files = [Path(p) for p in sys.argv[1:]]
    bad = 0
    for f in files:
        if not f.is_file():
            print('%s\n  ✗ 文件不存在' % f.name)
            bad += 1
            continue
        bad += report(f)
        print()
    if bad:
        print('%d 项不合格' % bad)
        return 1
    print('全部技术验收通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
