# -*- coding: utf-8 -*-
"""策略层（src/strategy/）
机构式多因子选股框架（不含自动下单）：数据 → 预处理 → 因子 → 评价 → 合成 → 风险 → 优化 → 回测 → 调仓建议。
分层职责见各模块 docstring；公共入口为 scripts/run_strategy.py。
"""
