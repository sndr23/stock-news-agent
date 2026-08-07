# filepath: src/tools/keyword_tables.py
"""
共享关键词表（单一事实来源）
====================================================
历史问题：HIGH_SIGNAL_KEYWORDS 在 src/agent/nodes.py 与 scripts/real_time_push.py
各维护一份且内容已漂移；科技词表也有 TECH_HARDWARE_KEYWORDS / TECH_SECTOR_KEYWORDS
/ _TECH_SECTOR_TERMS 三处。关键词漂移会导致两条管线行为不一致（路由、指纹、
外围科技判定各说各话），已在此收敛为共享模块：

- 批处理管线 (nodes.py)：路由决策 _has_high_signal 使用 HIGH_SIGNAL_KEYWORDS
- 实时推送 (real_time_push.py)：预筛直通 + 事件指纹 sig 路径使用 HIGH_SIGNAL_KEYWORDS，
  外围科技增强识别使用 OVERSEAS_TECH_KEYWORDS
- 规则打分 (calculators.py)：继续保留 TECH_HARDWARE_KEYWORDS（硬件词表，打分专用，
  与路由/外围判定语义不同，不合并）

英文缩写词边界（2026-08-06 修复，P2）：
- HIGH_SIGNAL_KEYWORDS 中的 ST/*ST/IPO 此前在 nodes.py 与 real_time_push.py 均以
  裸子串匹配，"STorage/STMicroelectronics" 会误命中 ST → 预筛直通误判 + 指纹
  sig 路径合并导致漏推。
- 现提供 signal_kw_pattern / has_signal_keyword / find_signal_keywords 共享实现，
  两管线统一调用，杜绝再次漂移。
"""
import re

# ============================================================
# 重磅信号关键词（两条管线共用）
# 用途：① nodes.py 路由：命中则必须走 LLM 深度分析
#       ② real_time_push.py：命中则跳过预筛分数限制直接进 LLM 判定，
#          并作为事件指纹的 sig 路径关键词
# ============================================================
HIGH_SIGNAL_KEYWORDS = [
    # 重大公司事件
    "退市", "退市新规", "立案调查", "重大违法", "破产", "业绩暴雷", "巨额亏损",
    "债务违约", "重大重组", "重大资产重组", "借壳", "并购", "涨停", "跌停",
    "监管处罚", "业绩超预期", "业绩预增", "业绩预减", "爆雷", "ST", "*ST",
    # 信号情报
    "龙虎榜", "机构净买入", "业绩预告",
    # 国内政策与监管
    "降准", "降息", "加息", "印花税", "注册制",
    "产业政策", "补贴", "减税", "政策利好",
    "国常会", "证监会", "央行", "国务院", "政治局", "中央经济工作会议",
    "平准基金", "国家队", "汇金",
    # 中等信号（公司资本运作）
    "北向资金", "回购", "增持", "减持", "分红", "股权激励",
    "IPO", "定增", "可转债",
    # 海外央行与宏观
    "美联储", "鲍威尔", "欧央行", "非农",
    "汇率", "人民币贬值", "美元飙升",
    # 外围风险事件（对A股有传导效应）
    "熔断", "暴跌", "崩盘", "债务危机", "银行危机", "金融风险",
    "战争", "军事冲突", "制裁", "地缘", "俄乌", "中东", "台海",
    "关税", "贸易战", "出口管制", "禁运",
    "原油暴跌", "原油暴涨", "油价",
    # 外围市场（直接传导A股科技情绪）
    "韩指", "日经", "美股暴跌", "纳指",
    # 科技板块重磅（用户定制：算力/科技股/英伟达/韩国/中际旭创 必进 LLM 判定）
    "算力", "科技股", "英伟达", "韩国", "中际旭创",
]

# ============================================================
# 外围科技板块关键词（实时推送"外围科技必推"增强识别用）
# 注：2026-08-01 用户调优版——收敛到硬科技词（PCB/MLCC/光模块/CPO 等），
# 移除过宽的"消费电子/软件/机器人/智能驾驶"，避免外围消费/软件新闻误触发。
# ============================================================
OVERSEAS_TECH_KEYWORDS = [
    "半导体", "芯片", "集成电路", "AI", "人工智能", "算力", "英伟达",
    "纳指", "科技股", "通信", "光模块", "CPO",
    "存储", "晶圆", "先进封装", "GPU", "PCB", "MLCC",
]

# 外围资讯源标记（富途全球 + 华尔街见闻 + 金十数据）
OVERSEAS_SOURCE_MARKERS = ["富途", "华尔街", "金十"]


# ============================================================
# 英文缩写词边界匹配（2026-08-06 新增，两管线共享）
# 背景：HIGH_SIGNAL_KEYWORDS 中的 ST/*ST/IPO 是英文缩写，裸子串匹配会误命中
# "STorage"/"STMicroelectronics"（实测 'STMicroelectronics 财报' 误命中 ST），
# 导致预筛直通误判 + _news_fingerprint 的 sig 路径指纹合并 → 漏推。
# AMD/CPO 等科技缩写已在 calculators._TECH_ENGLISH_WORDS 有词边界，
# 此处统一为 HIGH_SIGNAL_KEYWORDS 提供相同保护。
# ============================================================

# 需要词边界的英文缩写信号词（词表内全部英文缩写，自动从 HIGH_SIGNAL_KEYWORDS 提取）
_HIGH_SIGNAL_ENGLISH = {
    kw for kw in HIGH_SIGNAL_KEYWORDS
    if re.fullmatch(r"[A-Za-z*]+", kw or "")
}

# 预编译：kw → 词边界正则（编译后的 Pattern）
# 英文缩写加词边界（前后不能紧跟字母/数字），防 STorage/nAMD 误命中；
# 中文词保持子串匹配。*ST 的 * 是正则元字符，re.escape 处理。
_SIGNAL_KW_PATTERNS = {
    kw: re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])" if kw in _HIGH_SIGNAL_ENGLISH else re.escape(kw)
    )
    for kw in HIGH_SIGNAL_KEYWORDS
}
# 合并成单条预编译正则（一次扫描全部关键词，替代逐词 in 遍历）
SIGNAL_KEYWORD_PATTERN = re.compile("|".join(p.pattern for p in _SIGNAL_KW_PATTERNS.values()))


def signal_kw_pattern(kw: str) -> str:
    """信号关键词 → 正则片段：英文缩写加词边界（供外部扩展合并用，如 nodes 合并科技硬件词）"""
    if kw in _HIGH_SIGNAL_ENGLISH:
        return rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])"
    return re.escape(kw)


def has_signal_keyword(text: str) -> bool:
    """检测文本是否命中任一高信号关键词（英文缩写词边界感知）"""
    if not text:
        return False
    return bool(SIGNAL_KEYWORD_PATTERN.search(text))


def find_signal_keywords(text: str) -> list:
    """返回文本命中的高信号关键词列表（英文缩写词边界感知）

    与 has_signal_keyword 的区别：返回具体命中的词（用于事件指纹 sig 路径）。
    用逐词正则 search 替代裸子串 in（ST/IPO 不再误命中 STorage）。
    """
    if not text:
        return []
    return [kw for kw in HIGH_SIGNAL_KEYWORDS if _SIGNAL_KW_PATTERNS[kw].search(text)]


# ============================================================
# 事件指纹专用信号词（2026-08-07 新增，防宽泛词指纹碰撞）
# ============================================================
# 背景: _news_fingerprint 用"命中信号词集合"作为事件级指纹键的一部分。
# 但 HIGH_SIGNAL_KEYWORDS 中混有宽泛市场/机构/主题词（韩国/纳指/央行/油价等），
# 它们本身不能标识具体事件：实测"韩国总统宣布新产业计划"与"韩国半导体出口大增"
# 均仅命中"韩国"→ 同日指纹完全相同 → 后一条被 seen 跳过 → 漏推。
# 方案: 指纹路径使用 find_signal_fp_keywords（排除宽泛词后的核心事件词）；
# 预筛直通（has_signal_keyword / find_signal_keywords）保持全部词不变——
# 宽泛词仍能送 LLM 判定，只是不参与指纹合并。
# 仅命中宽泛词的新闻自动退回"标题归一化指纹"，跨源同事件由推送级
# _is_same_event（市场域/指数词/方向守卫）在推送前兜底合并。
_SIGNAL_FP_BROAD_WORDS = {
    # 机构/主体类（不能标识事件本身："央行"可出现在降准/加息/利率决议/官员讲话等不同事件）
    "央行", "国务院", "证监会", "政治局", "汇金", "国家队", "平准基金",
    "美联储", "鲍威尔", "欧央行", "中央经济工作会议",
    # 市场/主题类（不同事件共用同一市场语境；龙虎榜/机构净买入无个股字段时
    # 必须退回事件/标题指纹，避免同日多条龙虎榜因共享词合并）
    "韩国", "日经", "纳指", "美股", "科技股", "汇率", "油价", "北向资金",
    "机构净买入", "龙虎榜",
}
# 注意：并购/回购/增持/减持/股权激励/分红/IPO/定增/可转债等资本运作词
# 不列入宽泛排除词——它们依赖个股名（st 键）区分公司，且 sig 路径刻意不掺入
# 金额以合并"5亿 vs 5.5亿"等同事件多源表述（test_high_signal_amount_insensitive_same_fp
# 保护此行为）；若误列入，寒武纪两次不同金额回购将被拆成不同指纹。
# 预编译：宽泛词同样按英文缩写词边界处理（IPO 不误命中 IPOdroid 等）
_FP_BROAD_PATTERNS = {
    kw: re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])" if re.fullmatch(r"[A-Za-z*]+", kw or "") else re.escape(kw)
    )
    for kw in _SIGNAL_FP_BROAD_WORDS
}


def find_signal_fp_keywords(text: str) -> list:
    """事件指纹专用信号词：命中列表减去宽泛市场/机构/主题词

    仅用于 _news_fingerprint 的 sig 路径。命中返回核心事件词；
    仅命中宽泛词（如只含"韩国/纳指/央行"）返回空 → 调用方退回标题指纹，
    避免不同事件因共享宽泛词被合并成同一指纹（漏推根因之一）。
    """
    if not text:
        return []
    hits = [kw for kw in HIGH_SIGNAL_KEYWORDS if _SIGNAL_KW_PATTERNS[kw].search(text)]
    if not hits:
        return []
    core = [kw for kw in hits if kw not in _FP_BROAD_PATTERNS]
    return core
