# -*- coding: utf-8 -*-
"""字幕句（Segment）。

单独放在这里，是为了让界面模块（transcript/window）不必为了一个数据类去导入
识别、翻译那些重型依赖——那样会让软件启动慢好几秒。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Segment:
    sid: str
    jp: str
    zh: str = ""
    t0: float = 0.0          # 相对会话开始的秒数
    t1: float = 0.0
    rtf: float = 0.0
    asr_seconds: float = 0.0
    provider: str = ""
    live_jp: str = ""        # 流式引擎当时的草稿（用于对比）
    done: bool = False
