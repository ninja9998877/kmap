"""**唯一一处**决定「资料数据在哪」的地方。

为什么要拆开：**代码公开，资料私有。**

教材目录（`catalog/*.json`）、知识点图谱（`graph-*.yaml`）、掌握度（`progress.json`）
是这个项目里真正有价值、也真正属于私人的东西 —— 它们是给孩子一份一份攒出来的，
不是工具。而工具（判分、出题、界面、这个文件）谁都可以看。

所以代码仓库里**一个 data 文件都没有**，靠这个模块去找。找的顺序：

1. `$KMAP_DATA` —— 显式指定，脚本和测试用它
2. 代码目录**旁边的** `../kmap-data` —— 日常就靠这条，不用设环境变量
3. 代码目录**本身** —— 有人 clone 完直接把文件丢进来说"我就想这么用"，也认

前两条都不中时**不报错**，回到第 3 条 —— 一个空目录会让界面显示
「0/18 个单元有内容」，那本身就是要传达的信息。但服务启动时会把实际用的目录
打印出来，免得出现"明明有文件却说没有"那种查半天的情况。
"""

import os
from pathlib import Path

# 代码在哪（这个文件所在目录）
CODE = Path(__file__).resolve().parent


def _resolve_data_dir() -> Path:
    env = os.environ.get('KMAP_DATA')
    if env:
        return Path(env).expanduser()
    sibling = CODE.parent / 'kmap-data'
    if sibling.is_dir():
        return sibling
    return CODE


DATA = _resolve_data_dir()

# 各种资料文件。全部走 DATA，一个都不许用 CODE —— 用了就等于把资料
# 拉回公开仓库里了（而那是**不会报错**的那种错：本地一切正常，
# 直到你发现 graph-*.yaml 出现在了公开仓库的提交里）。
CATALOG = DATA / 'catalog'


def graph(grade: int, subject: str) -> Path:
    """一个「年级 × 学科」对应的图谱文件。约定就是文件名本身。"""
    return DATA / ('graph-%d-%s.yaml' % (grade, subject))


def progress() -> Path:
    """掌握度文件。`KMAP_PROGRESS` 优先 —— 跑测试时别把孩子的真实进度打脏。"""
    env = os.environ.get('KMAP_PROGRESS')
    return Path(env) if env else (DATA / 'progress.json')


def describe() -> str:
    """给人看的一行，说明数据是从哪来的。启动时打印。"""
    if os.environ.get('KMAP_DATA'):
        how = 'KMAP_DATA 指定'
    elif DATA == CODE:
        how = '代码目录（没找到 kmap-data，资料和代码混在一起）'
    else:
        how = '代码旁边的 kmap-data'
    return '%s  [%s]' % (DATA, how)
