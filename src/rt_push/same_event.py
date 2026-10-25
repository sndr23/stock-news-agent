# filepath: src/rt_push/same_event.py
"""同事件判定：事件签名、噪声过滤、同事件合并规则（自 scripts/real_time_push.py 拆出，行为不变）"""

import logging
import re
import time
from typing import NamedTuple

from src.rt_push.common import (
    _as_list, _normalize_entity,
    _lcs_len, _lcs_subseq_len, _env_int, _state_timestamp,
)
from src.tools.calculators import _EVENT_KEYWORD_GROUPS, _extract_core_numbers, _has_tech_keyword


logger = logging.getLogger("real_time_push")



# ============================================================
# 新闻指纹（跨轮次去重，多源事件级）
# ============================================================
def _normalize_title(title: str) -> str:
    """标题归一化：去标点/空白/emoji，仅保留中英文与数字（用于标题兜底指纹）

    2026-09-07：保留数字间的小数点——"2.9个基点"曾被规范化成"29个基点"，
    同一数字的指纹分裂（2026-09-05 美债条目实测）。非数字间的点仍剥除
    （英文句尾句点等）。
    """
    t = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9.]+", "", title or "")
    t = re.sub(r"(?<!\d)\.|\.(?!\d)", "", t)  # 仅保留数字间的小数点
    return t[:40]


# 推送层私有事件组叠加（2026-09-07 P0 审计）：宏观资本运作动作词。
# 仅本模块 _event_signature_light 扫描时与共享表合并使用，不回写
# calculators._EVENT_KEYWORD_GROUPS（该表被 UI/推送多条管线共享，改动影响面大）。
_EXTRA_EVENT_GROUPS = [("注资", ["注资", "增资", "特别国债"])]


def _event_signature_light(news: dict) -> tuple:
    """轻量事件签名 (stocks, events, numbers)

    不复用 calculators._event_signature 的 stocks 兜底提取：
    它假设"标题冒号前是股票名"（公告格式），对新闻标题如
    "央行宣布：降准0.5个百分点" 会把"央行宣布"误判为股票，
    导致同一新闻因标点差异（：vs ,）指纹分叉。
    本函数 stocks 仅取显式字段（龙虎榜/业绩预告等自带 name/affected_stocks），
    events/numbers 复用项目的事件关键词组与金额提取。
    """
    stocks = set(_as_list(news.get("affected_stocks")))
    name = news.get("name", "")
    if name:
        stocks.add(name)
    text = f"{news.get('title', '')} {news.get('content', '')}"
    events = set()
    # 2026-09-07 P0 审计：叠加推送层私有事件组 _EXTRA_EVENT_GROUPS——
    # "注资/增资/特别国债"类宏观动作词不在共享表（calculators.py 保持不动），
    # 导致"财政部注资8家中央金融企业"题材多源报道事件组全空、指纹分裂走
    # 纯标题路径，轮内合并与跨轮 48h 拦截全部兜不住（17:02-21:02 五连推实证）。
    for group_name, keywords in list(_EVENT_KEYWORD_GROUPS) + _EXTRA_EVENT_GROUPS:
        if any(kw in text for kw in keywords):
            events.add(group_name)
    numbers = _extract_core_numbers(text)
    return stocks, events, numbers


# 跨源同事件的高置信核心事实锚点。只收语义明确的事实完成态/状态变化，
# 不把“扩产/产能/看好”等宽泛主题词单独当作同事件依据。
_CORE_FACT_GROUPS = (
    ("security_warning", ("安全警示", "安全警报", "安全警告", "安全提醒")),
    ("mass_production", ("正式量产", "实现量产", "投入量产", "量产")),
    ("capacity_expansion", ("产能扩张", "扩充产能", "新增产能", "产能翻倍", "扩产")),
    ("buyback_completion", ("回购完成", "完成回购", "回购注销", "注销回购股份")),
    ("regulatory_filing", ("立案调查", "被立案", "立案")),
    ("delisting", ("退市", "终止上市")),
    ("strike", ("罢工", "大罢工")),
    ("shutdown", ("停产", "停工")),
)


_TITLE_NUMERIC_ANCHOR_RE = re.compile(
    r"第[一二三四五六七八九十百]+代|[A-Za-z]{2,12}\d+[A-Za-z0-9]*|"
    r"\d+(?:\.\d+)?(?:亿|万|%|倍|GB|TB|nm|纳米|百分点|年|月|日|G)"
)


def _core_fact_anchors(text: str) -> set:
    """提取标题中的高置信核心事实类别。"""
    text = str(text or "")
    return {name for name, words in _CORE_FACT_GROUPS
            if any(word in text for word in words)}


def _title_numeric_anchors(text: str) -> set:
    """提取代际、产品型号和带单位数字，作为跨源数字事实锚点。"""
    return set(_TITLE_NUMERIC_ANCHOR_RE.findall(str(text or "")))


def _sectors_overlap(sec_a, sec_b) -> set:
    """板块交集（子串包含语义，2026-08-13 修复）

    LLM 抽的 sectors 不稳定："AI算力" vs "AI"+"算力"、"算力/CPO" vs "CPO"，
    精确相等导致同板块事件漏合并（上海算力补贴×2 实证）。用子串包含判断同板块。
    """
    a = {str(s).strip() for s in _as_list(sec_a) if str(s).strip()}
    b = {str(s).strip() for s in _as_list(sec_b) if str(s).strip()}
    hit = set()
    for sa in a:
        for sb in b:
            if sa and sb and (sa == sb or sa in sb or sb in sa):
                hit.add(sa if len(sa) <= len(sb) else sb)
    return hit


def _push_event_sig(news: dict, judge: dict) -> dict:
    """生成推送级事件签名：规则抽取(个股/事件组/金额) + LLM主体(entities) + 归一化标题

    指纹 _news_fingerprint 解决"完全同一条"的跨轮去重；本签名解决"同一事件的
    不同报道"（标题措辞/金额表述/信号词子集不同导致指纹分裂，
    恩智浦收购Ambarella三源三推实证）。
    """
    stocks, events, numbers = _event_signature_light(news)
    entities = {_normalize_entity(str(e).strip()) for e in _as_list(judge.get("entities")) if str(e).strip()}
    sectors = {str(s).strip() for s in _as_list(judge.get("sectors")) if str(s).strip()}
    return {
        "stocks": sorted(stocks),
        "entities": sorted(entities),
        "events": sorted(events),
        "numbers": sorted(numbers),
        "sectors": sorted(sectors),
        "scope": str(judge.get("scope") or "stock"),
        "title_norm": _normalize_title(news.get("title", "")),
    }


def _merge_event_sig(sig_a: dict, sig_b: dict) -> dict:
    """并集合并两个事件签名（分组内传递合并用），标题保留较长者"""
    return {
        "stocks": sorted(set(_as_list(sig_a.get("stocks"))) | set(_as_list(sig_b.get("stocks")))),
        "entities": sorted(set(_as_list(sig_a.get("entities"))) | set(_as_list(sig_b.get("entities")))),
        "events": sorted(set(_as_list(sig_a.get("events"))) | set(_as_list(sig_b.get("events")))),
        "numbers": sorted(set(_as_list(sig_a.get("numbers"))) | set(_as_list(sig_b.get("numbers")))),
        "sectors": sorted(set(_as_list(sig_a.get("sectors"))) | set(_as_list(sig_b.get("sectors")))),
        "title_norm": sig_a.get("title_norm", "") if len(sig_a.get("title_norm", "")) >= len(sig_b.get("title_norm", "")) else sig_b.get("title_norm", ""),
    }


# 标题方向线索词（LCS 兜底合并的方向对立守卫）
_TITLE_BULLISH_HINTS = ["涨", "升", "利好", "增持", "预增", "突破", "涨停", "上涨", "飙升", "反弹", "回升", "创新高"]
_TITLE_BEARISH_HINTS = ["跌", "降", "利空", "减持", "预减", "跌停", "下跌", "暴跌", "走低", "重挫", "回落", "跳水"]


def _title_direction_conflict(ta: str, tb: str) -> bool:
    """两条归一化标题方向是否对立（防止"集体收涨 vs 集体收跌"被 LCS 误合并漏推）"""
    bull_a = any(k in ta for k in _TITLE_BULLISH_HINTS)
    bear_b = any(k in tb for k in _TITLE_BEARISH_HINTS)
    bear_a = any(k in ta for k in _TITLE_BEARISH_HINTS)
    bull_b = any(k in tb for k in _TITLE_BULLISH_HINTS)
    return (bull_a and bear_b) or (bear_a and bull_b)


# 市场开收盘/复盘类快讯识别（2026-08-03 21:32 美股开盘三源三推实证）
# 该类快讯来自多源（东财/新浪/富途/华尔街）标题措辞差异大、无事件组/实体/金额，
# Jaccard(0.14~0.41) 与 LCS 兜不住 → 按"同时段组 + 同市场域"合并。
_SESSION_GROUPS = {
    "open": ["开盘", "早盘", "盘前"],
    "noon": ["午评", "午盘"],
    "close": ["收盘", "尾盘", "盘后"],
}
_MARKET_DOMAIN_MARKERS = {
    "us": ["美股", "道指", "纳指", "标普", "谷歌", "苹果", "特斯拉", "英伟达"],
    "a": ["A股", "沪指", "深成指", "创业板", "科创50", "沪深"],
    "kr": ["韩股", "韩国综指", "科斯达克"],
    "jp": ["日经", "日股"],
    "hk": ["恒指", "港股"],
    "eu": ["欧股", "欧洲股市"],
}
# 市场指数/基准词（盘中行情动态同事件判定用，2026-08-04 00:32 纳指三推实证）
_INDEX_TOKENS = [
    "纳指", "道指", "标普", "恒指", "日经", "韩指", "韩国综指", "科斯达克",
    "沪指", "深成指", "创业板指", "科创50", "上证", "沪深300", "富时100",
]


# 栏目汇总/盘面播报类噪声识别（2026-08-11 审核实证：48h 内 61 条推送中 13 条为
# 栏目汇总/指数播报/盘面异动类——"晚间新闻精选""隔夜要闻""九点特供""风口研报"
# "KOSPI涨超2%""概念异动拉升"等被 LLM 判强档后仍推送，属"非重大消息"，按用户
# 口径（仅强利好/强利空 + 重大事件）不应推。以下三组规则在 5c 强档门槛后硬过滤，
# 即使 LLM 判强档也降级不推，记录 seen 并标注原因。
# 注意：勿加"早报/晚报"等过宽词——"期货早报"含非农等重大宏观数据，曾实证合理推送。
_NOISE_COLUMN_MARKERS = [
    "新闻精选", "晚间新闻", "要闻速递", "隔夜要闻", "全球要闻", "九点特供", "风口研报",
    "投资避雷针", "午评", "收评", "盘前速递", "新闻速览", "涨停分析",
    "盘前要闻", "市场要闻", "研报精选",
    # 2026-08-12 审核实证漏网："金十数据整理欧盘美盘重要新闻汇总"（8-11 23:32）被推送，
    # 既有词表有"新闻精选"但无"新闻汇总"。补入，含"重要新闻汇总/要闻汇总"等变体。
    # 勿加"早报/晚报"——"期货早报"含非农等宏观数据曾实证合理推送。
    "新闻汇总", "要闻汇总", "市场汇总",
    # 2026-09-04 审核实证：金十"期货热点追踪""局势跟踪"为日报式栏目（中东/俄乌各推一条的来源），
    # 属栏目汇总类噪声。补入；剥离栏目词后仍含重大事件（如"局势跟踪：以色列空袭核设施"）由
    # 分支1 的"栏目内嵌重大事件"语义放行。
    "期货热点追踪", "局势跟踪",
    # 2026-09-04 近一周审核实证（9-01 14:17）："主力资金流向前十大个股"——
    # 纯数据播报栏目，无事件语义。
    "主力资金流向", "资金流向",
]

_DAILY_COLUMN_MARKERS = (
    "局势跟踪", "期货热点追踪", "盘前要闻一览", "要闻速递", "新闻汇总", "要闻汇总",
    "市场汇总", "资金流向日报", "主力资金监控", "投资避雷针", "行业动态汇总",
    "每日动态汇总", "早间要闻", "隔夜要闻",
)

_BROADCAST_MOVE_RE = re.compile(
    r"(涨超|跌超|涨逾|跌逾|涨幅|跌幅|大涨|大跌|暴涨|暴跌|走强|走弱|拉升|下挫|"
    r"高开|低开|创新高|创新低|净流入|净流出|异动|翻红|翻绿|收涨|收跌|现报)"
)
_BROADCAST_MAG_RE = re.compile(
    r"(\d+(?:\.\d+)?%|\d+\.\d+%|超\d+%|\d+个点|\d+cm|\d+天\d+板)"
)
_BROADCAST_EVENT_VERBS = (
    "公告", "订单", "中标", "签约", "签署", "收购", "并购", "增持", "回购", "业绩",
    "净利", "营收", "投产", "扩产", "产能", "涨价", "提价", "定增", "分红", "获批",
    "量产", "交付", "出货", "制裁", "管制", "关税", "降准", "降息", "加息", "立案",
    "问询", "减持", "融资", "合作", "大单", "解禁", "停牌", "复牌", "调查", "处罚",
    # 行业价格/供需信号豁免（2026-10-25 二审）：存储涨价链、覆铜板、材料常以
    # “价格持续狂飙/报价上调/缺货”表述而不含“涨价”字面，纯涨幅词会被播报体误杀，
    # 丢掉用户核心板块的景气度信号（实证：“内存条价格持续狂飙，华强北一年涨超300%”）。
    "价格", "报价", "售价", "缺货", "供不应求", "库存",
)


def _is_broadcast_body(title: str, exempt_names=None) -> str:
    """盘面播报体识别：主体+涨跌措辞(+涨跌停或幅度数字) 且 无事件动词 → 返回 "播报体"

    exempt_names: 豁免主体名集合（用户持仓/watchlist）。命中即返回 ""，保持现有
    「持仓股的涨跌播报仍推」的既定行为，不得改动这一语义。
    """
    title = str(title or "")
    if not title:
        return ""
    if any(v in title for v in _BROADCAST_EVENT_VERBS):
        return ""
    if exempt_names and any(name and name in title for name in exempt_names):
        return ""
    if not _BROADCAST_MOVE_RE.search(title):
        return ""
    if (_BROADCAST_MAG_RE.search(title)
            or any(k in title for k in ("涨停", "跌停", "连板"))):
        return "播报体"
    return ""


# 指数盘中行情播报的波动措辞（与 _INDEX_TOKENS 组合判定，避免误伤个股消息）
_NOISE_INDEX_MOVE = [
    "涨超", "跌超", "涨逾", "跌逾", "涨幅扩大", "跌幅扩大", "涨幅扩大至",
    "跌幅扩大至", "现报", "开涨", "开跌", "收涨", "收跌", "上涨", "下跌",
    # 2026-08-25 审核实证："午评创业板指半日跌35"（"半日跌"）——既有
    # MOVE 词表缺"半日/盘中/日内涨跌"表述，"跌35"非"下跌"漏判。补入。
    "半日跌", "半日涨", "盘中跌", "盘中涨", "日内跌", "日内涨",
    # 2026-09-04 审核实证："日元涨幅超过1"——"涨幅超过/跌幅超过"表述不在词表漏网。补入。
    "涨幅超过", "跌幅超过", "涨幅超", "跌幅超",
    # 2026-09-04 审核实证："柴油期货价格创新高"——"创新高/创新低"为行情措辞漏网。
    # 仅与 _NOISE_INDEX_NAMES 组合判定，个股"中际旭创创新高"不命中指数名单不受影响。
    "创新高", "创新低",
    # 2026-09-04 晚审计补："伦敦金现短线走低"类同族短线行情措辞。
    "短线走高", "短线走低",
]
# 纯盘面异动措辞（板块/概念异动、非龙头个股异动；龙头个股消息由 is_leader 例外放行）
_NOISE_INTRADAY_MARKERS = [
    "异动拉升", "直线涨停", "直线跌停", "盘初走强", "盘中走强", "短线走高",
    "震荡拉升", "尾盘拉升", "午后拉升", "冲高回落", "快速走强", "集体走强",
    # 2026-08-13 审核实证漏网："PCB概念表现活跃 方邦股份涨超10%" 被推送——
    # "表现活跃/概念活跃"等盘面情绪措辞不在词表。补入；龙头例外仍由 is_leader 放行
    # （"中际旭创表现活跃"类不误伤）。
    "表现活跃", "概念活跃", "持续活跃", "反复活跃", "概念走强", "概念走高",
    "集体大涨", "涨停潮", "活跃拉升",
    # 2026-08-25 审核实证：净流出/震荡回升/跳水/涨停等盘面措辞漏网
    # （"主力资金转融券标的板块净流出超742亿"、"人形机器人概念震荡回升兆威机电
    # 涨停"全被推送）。龙头例外仍由 is_leader 放行，不误伤自选龙头（"中际旭创跳水"仍推）。
    "震荡回升", "震荡回落", "震荡走高", "震荡走低",
    "净流入", "净流出", "中单净流出", "资金出逃",
    "快速跳水", "大幅跳水", "跳水", "异动",
    "涨停", "跌停",
    # 2026-09-04 晚审计实证："智能驾驶概念集体高开 大众交通2连板""AI应用概念
    # 延续强势 易点天下涨超10%"被推送——盘面措辞缺"集体高开/延续强势/连板"。
    "集体高开", "集体低开", "延续强势", "持续下挫", "持续走强", "连板",
]
# 盘面异动例外：标题含重大公司治理/监管事件词时放行给 LLM 判定
# （2026-09-04 晚审计回放实证："涨停芯片股背后藏雷！实控人、总经理双双被留置，
# 控股股东的持股100%冻结"会被"涨停"误判盘面异动——但核心是留置/冻结重大事件，
# 盘面词仅是背景。例外仅跳过噪声判定，最终仍走强档门槛 + LLM 把关。）
_MAJOR_CORPORATE_EVENT_MARKERS = [
    "立案", "留置", "退市", "欺诈", "被罚", "刑拘", "逮捕", "冻结",
    "破产", "暴雷", "爆雷", "双双被", "被查", "回应", "公告", "澄清",
    "减持", "回购", "增持", "处罚", "问询", "批复", "核准",
]


# 指数行情播报需额外覆盖的指数名（_INDEX_TOKENS 之外的出现形式）
_NOISE_INDEX_NAMES = _INDEX_TOKENS + ["KOSPI", "恒生指数", "综合指数"] + [
    # 2026-09-04 审核实证：外盘行情播报漏网——"美元指数3日下跌""美元兑日元跌幅扩大至2"
    # "日元涨幅超过1""WTI原油期货收涨0.79美元报91.01" 全被推送。补外盘行情基准词，
    # 与 _NOISE_INDEX_MOVE 组合判定（仅"基准名+涨跌措辞"同时命中才拦，不误伤事件性新闻）。
    # "柴油"用"柴油期货/柴油价格"避免误伤"柴油车/柴油发电机"类标题。
    "美元指数", "美元兑日元", "日元", "WTI", "原油期货", "柴油期货", "柴油价格",
    # 2026-09-04 晚审计实证："现货黄金日内跌幅扩大至2%"（21:04 新代码时段）被推送——
    # 贵金属行情基准词缺失。事件性新闻（"央行增持黄金储备"）不含涨跌措辞不误拦。
    "现货黄金", "现货白银", "黄金期货", "白银期货", "伦敦金", "COMEX",
]
# 外盘科技龙头个股行情播报（2026-09-04 验收回放实证："SK海力士涨幅扩大至5%
# 三星电子涨逾3%"被推送——外盘公司不在指数名单漏网）。
# 公司名有事件语义（"台积电营收上涨10%"是产业信号），不能进 _NOISE_INDEX_NAMES
# 用宽泛词表（"上涨/下跌"会误伤财务新闻），改用严格数字涨幅措辞组合：
# 仅"公司名 + 盘中涨跌幅播报措辞"同时命中才拦，"SK海力士扩产 HBM"类
# 事件新闻不含严格措辞不误拦。
_NOISE_OVERSEAS_STOCK_NAMES = [
    "SK海力士", "三星电子", "台积电", "美光", "铠侠", "英伟达", "AMD", "英特尔",
]
_NOISE_OVERSEAS_STOCK_MOVE = [
    "涨超", "跌超", "涨逾", "跌逾", "涨幅扩大", "跌幅扩大", "涨幅扩大至",
    "跌幅扩大至", "现报", "开涨", "开跌", "收涨", "收跌", "涨幅超过", "跌幅超过",
    "涨幅超", "跌幅超", "创新高", "创新低", "盘中涨", "盘中跌",
]


# 同实体跨日报道的事件短语锚（2026-08-11 修复：索尼×台积电合资 48h 三推实证——
# "拟合资建厂" vs "批准成立合资企业" 实体顺序相反（索尼在前 vs 台积电在前），
# LCS 子序列仅 ~7 字达不到放宽阈值；同实体 + 共享事件动词 → 直接判定同事件。
# 仅保留高置信"事件动词"（合并后必为同一事件）；主题词（扩产/量产/良率/产能/
# 涨价/投资/营收等）易造成不同事件误并（台积电 CoWoS量产 vs 索尼合资 2029量产
# 实证），一律不进锚表，交由放宽 LCS 分支处理。金额守卫：
# 双方金额均明确且无交集 → 不同事件（防"50亿建厂 vs 10亿回购"误并漏推）。）
_EVENT_PHRASE_ANCHORS = [
    "合资", "收购", "并购", "建厂", "成立", "融资", "回购", "入股",
    "签约", "中标", "停牌", "重组", "破产", "退市", "处罚", "立案",
    "增持", "减持",
    # 2026-09-07 P0 审计：宏观资本运作动作词（财政部注资题材五连推实证）
    "注资", "增资",
]


# 宏观数据发布后的市场反应合并词（2026-08-12 实证：21:31"美国CPI符合预期美股高开"
# vs 22:01"美国7月通胀表现温和美股盘初纳指涨09光通信存储普涨"——同一宏观事件
# 48h 内推两次。entities 各抽各的（SK海力士/美光 vs CoreWeave/超微电脑）无交集，
# 标题共享段短，既有 7 个合并分支全部兜不住。守卫：双方均含宏观数据词 +
# 同市场域（防"德国CPI vs 美国CPI"误并——德国CPI标题无美股域词）+ 方向一致。）
_MACRO_EVENT_WORDS = ["CPI", "PPI", "通胀", "物价", "非农", "失业率", "GDP",
                      "利率决议", "FOMC", "就业数据"]


# 市场状态类事件组（2026-08-14 修复：日本央行加息三连推实证）
# "行情下跌"由 _EVENT_KEYWORD_GROUPS 规则命中 content 中"走低/暴跌/下挫"等词提取，
# 属市场状态描述而非事件动词。它使 _is_same_event 的"共享事件组"与
# "双方均无事件组"分支同时失效——第一条 events=["行情下跌"] 与其余 events=[] 的
# 报道无法匹配（shared_ev=∅ 且 not ev_a 不成立），同一事件多源报道 48h 内重复推送。
# _is_same_event 计算事件组前统一过滤，避免污染事件语义。
_MARKET_STATE_EVENTS = {"行情下跌", "行情上涨", "行情震荡"}


# 央行/货币当局实体（归一化后名称，2026-08-14 修复：宏观流动性事件跨源合并兜底）
# 日本央行加息类宏观事件：多源快讯措辞差异大（"最快可能在9月加息" vs
# "加息或提速美元对日元急跌…流动性冲击"），LCS 仅 4 字、jaccard 0.15~0.20、
# 无事件组/板块交集，既有全部合并分支兜不住（"加息"不在事件锚表、
# _MACRO_EVENT_WORDS 无"加息"、日股无市场域词）。新增分支守卫：
# 双方共享同一央行实体 + 标题均含宏观政策词 + 政策方向不冲突 → 同事件。
_CENTRAL_BANK_ENTITIES = {
    "日本央行", "美联储", "欧央行", "中国人民银行", "英国央行",
    "韩国央行", "澳洲联储", "瑞士央行", "加拿大央行",
}
# 宏观政策鹰派/鸽派方向词（方向守卫，防"考虑加息 vs 考虑降息"误并——
# _title_direction_conflict 的涨跌表不含"加息/降息"：加息无"涨/升"字）
_POLICY_HAWK_WORDS = ("加息", "升息", "缩表", "紧缩", "收紧")
_POLICY_DOVE_WORDS = ("降息", "扩表", "宽松", "降准", "量化宽松", "放水")
# 宏观政策事件触发词（合并判定：标题描述同一央行政策动作；需配合央行实体守卫）
# 仅含方向性政策动作（购债无方向——"购债操作安排"中性日常 vs "削减购债"鹰派，
# 不宜作为合并触发，避免"购债安排"与"加息传闻"误并）
_MACRO_POLICY_EVENT_WORDS = (
    "加息", "降息", "缩表", "扩表", "宽松", "紧缩",
    "升息", "降准", "量化宽松", "YCC", "收益率曲线控制",
)


# 行情普涨/普跌类措辞（sectors 交集合并用，2026-08-12 实证：22:01 同轮双推
# "美股光通信存储概念股普涨诺基亚升逾9" vs "美国7月通胀表现温和美股盘初纳指涨09
# 光通信股存储芯片股普涨"——entities 各抽各的（诺基亚/Ciena/Coherent vs
# CoreWeave/超微电脑），无共享时段词/指数词，LCS 仅"光通信"3 字兜不住。
# 两标题共享板块（光通信/半导体/存储）且均为美股行情 → 合并。
# 不含单字"涨/跌"防"涨价/跌幅"等非行情语境误并。）
_MARKET_MOVE_WORDS = [
    "普涨", "普跌", "高开", "低开", "走强", "走弱", "上涨", "下跌",
    "涨超", "跌超", "升逾", "涨逾", "跳水", "回升", "大涨", "大跌",
    "拉升", "上扬", "下挫", "冲高", "回落", "涨近", "跌近",
    "涨幅扩大", "跌幅扩大",
]


def _session_group(ta: str, tb: str) -> str | None:
    """两条标题是否属于同一市场时段组（开盘组/午评组/收盘组），返回组名或 None"""
    ga = next((g for g, kws in _SESSION_GROUPS.items() if any(k in ta for k in kws)), None)
    gb = next((g for g, kws in _SESSION_GROUPS.items() if any(k in tb for k in kws)), None)
    return ga if (ga is not None and ga == gb) else None


def _market_domain_overlap(ta: str, tb: str) -> bool:
    """两条标题的市场域（美股/A股/韩股/日股/港股/欧股）是否有交集"""
    da = {d for d, kws in _MARKET_DOMAIN_MARKERS.items() if any(k in ta for k in kws)}
    db = {d for d, kws in _MARKET_DOMAIN_MARKERS.items() if any(k in tb for k in kws)}
    return bool(da & db)


def _shared_index_token(ta: str, tb: str) -> bool:
    """两条标题是否共享同一市场指数词（纳指/道指/标普/沪指等）"""
    return any(k in ta and k in tb for k in _INDEX_TOKENS)


def _session_conflict(ta: str, tb: str) -> bool:
    """两条标题是否属于冲突的市场时段（如 午评 vs 收盘）——有时段词且组不同"""
    ga = next((g for g, kws in _SESSION_GROUPS.items() if any(k in ta for k in kws)), None)
    gb = next((g for g, kws in _SESSION_GROUPS.items() if any(k in tb for k in kws)), None)
    return ga is not None and gb is not None and ga != gb


def _entity_overlap(ent_a: set, ent_b: set) -> bool:
    """实体模糊重叠（2026-08-11 修复：跨源同事件合并失败实证）

    LLM 对同一实体的表述不稳定："索尼" vs "索尼半导体解决方案公司"、
    "台积电" vs "台积电(TSM.N)"。仅靠精确相等会把同一事件拆成多条
    （索尼×台积电合资 48h 内三推实证）。判定：
    1. 完全相等（先经 _normalize_entity 归一化别名/代码后缀）
    2. 互相包含（"索尼" ⊂ "索尼半导体解决方案公司"）
    3. 共享连续子串占较短实体 ≥60% 且 ≥2 字（"索尼半导体" vs "索尼"）
    """
    na = {_normalize_entity(ea) for ea in ent_a if ea}
    nb = {_normalize_entity(eb) for eb in ent_b if eb}
    for ea in na:
        for eb in nb:
            if not ea or not eb:
                continue
            if ea == eb:
                return True
            if len(ea) >= 2 and len(eb) >= 2 and (ea in eb or eb in ea):
                return True
            m = min(len(ea), len(eb))
            if m >= 2:
                l = _lcs_len(ea, eb)
                if l >= 2 and l / m >= 0.6:
                    return True
    return False


def _strip_entities(text: str, entities: set) -> str:
    """从归一化标题剔除实体词（长词优先），用于"是否除主体外还共享内容"判定

    2026-08-11 修复：实体名重复出现会使 LCS 子序列虚高——
    "央行授权德银" 与 "央行十五五规划" 因"中国人民银行"各出现 1-2 次，
    子序列占比 0.52 被误并。剔除实体后计算共享连续子串可消除该假象。
    """
    for e in sorted(entities, key=len, reverse=True):
        if e:
            text = text.replace(str(e), "")
    return text


def _is_index_move_text(text: str) -> bool:
    """是否"指数名+行情措辞"（指数盘中播报，与 _is_noise_push 分支2 同判据）"""
    return (any(i in text for i in _NOISE_INDEX_NAMES)
            and any(m in text for m in _NOISE_INDEX_MOVE))


def _is_intraday_move_text(text: str) -> bool:
    """是否命中盘面异动措辞（与 _is_noise_push 分支3 同判据）"""
    return any(m in text for m in _NOISE_INTRADAY_MARKERS)


# 宏观/政策/地缘类（2026-08-12 修复：防偏科分层配额用——
# 命中此类词条的候选在溢出时优先于普通科技/行情候选，保证宏观不被打压。
# 命中词与 HIGH_SIGNAL_KEYWORDS 的宏观区保持一致，另补少量地缘/数据词。）
_MACRO_POLICY_KEYWORDS = [
    "央行", "证监会", "国常会", "政治局", "国务院", "降准", "降息", "加息",
    "CPI", "PPI", "通胀", "物价", "利率决议", "FOMC", "GDP", "失业率", "就业数据",
    "非农", "美债", "社融", "LPR", "PMI", "印花税", "注册制", "平准基金", "汇金", "国家队",
    "美联储", "鲍威尔", "欧央行", "地缘", "战争", "关税", "贸易战", "制裁", "出口管制",
    "中东", "台海", "原油暴跌", "原油暴涨",
]


# 同题材推送饱和上限（2026-08-12 审核实证：存储/半导体当日推 17 条占 47%，
# 信息价值递减、用户观感"推的都是不重要的"。同一题材（共享板块或实体）
# 24h 内已推 ≥ 上限后，新条目不再推，记 seen 标注；market 级（宏观数据/
# 大盘事件）豁免，保证 CPI 等 market 级数据永不被饱和拦截。
# 上限可通过环境变量 RT_TOPIC_LIMIT 调整。）
TOPIC_PUSH_LIMIT = _env_int("RT_TOPIC_LIMIT", 5)
TOPIC_PUSH_WINDOW_H = 24.0


# 2026-08-25 审核实证：market 级"sectors/entities 全豁免"被人利用——美加关税×7/伊朗×4
# 全部 scope=market 无限豁免溢出剩余。仅"宏观数据发布类"（CPI/非农/利率决议）保持豁免；
# 地缘/制裁/关税/央行表态类 market 参与"同主题饱和"，上限 3 条（比板块默认 5 条更严）。
_MARKET_THEME_PATTERNS = [
    ("美加关税", ["加拿大", "关税", "卢特尼克", "美加"]),
    ("伊朗制裁", ["伊朗", "里亚尔", "霍尔木兹"]),
    ("日本央行", ["日本央行", "日元", "植田"]),
    ("美债/美财长", ["美债", "贝森特"]),
    # 2026-09-04 审核实证：中东/油价系 48h 推约 8 条（特朗普打击伊朗、局势跟踪×2、
    # 驻军延长、互放狠话、柴油新高×2、油价周涨幅）——既有 4 槽位无俄乌/油价/以黎，
    # "中东驻军"不含"伊朗"关键词直接逃逸。补两槽位；"柴油"用"柴油期货/柴油价格"
    # 避免"柴油车/柴油发电机"误入油价主题。霍尔木兹保留在"伊朗制裁"槽避免重复计数。
    ("俄乌冲突", ["俄乌", "乌克兰", "克里姆林宫"]),
    ("中东局势/油价", ["中东", "以色列", "黎巴嫩", "油价", "原油", "柴油期货", "柴油价格"]),
]
MARKET_TOPIC_PUSH_LIMIT = _env_int("RT_MARKET_TOPIC_LIMIT", 3)


# 题材饱和时允许有限突破的高置信硬事件词表（2026-09-22 Q-03）。
# 完整范围：量产/投产/复产；涨价/提价/降价；扩产/扩建/产能扩张/新增产能/
# 产能翻倍/产能提升/产能释放；回购完成/完成回购/回购注销/注销回购股份；
# 立案/立案调查/行政处罚/监管处罚；退市/终止上市；罢工/停产/停工；
# 收购完成/完成收购/并购完成/重组完成；重大中标/中标/重大合同/签约。
# “产能+翻倍/倍增/增长/提升/扩张/释放”等组合也视为产能硬事实，覆盖
# “产能明年或翻倍”这类跨源插入预测词的标题，但不把单独“产能”当豁免依据。
_HARD_EVENT_NOVELTY_GROUPS = (
    ("mass_production", ("量产", "投产", "复产")),
    ("price_change", ("涨价", "提价", "降价")),
    ("capacity_expansion", ("扩产", "扩建", "产能扩张", "新增产能",
                             "产能翻倍", "产能提升", "产能释放")),
    ("buyback_completion", ("回购完成", "完成回购", "回购注销", "注销回购股份")),
    ("regulatory_action", ("立案", "立案调查", "行政处罚", "监管处罚")),
    ("delisting", ("退市", "终止上市")),
    ("labor_or_shutdown", ("罢工", "停产", "停工")),
    ("deal_completion", ("收购完成", "完成收购", "并购完成", "重组完成",
                          "重大中标", "中标", "重大合同", "签约")),
)
_CAPACITY_COMPOUND_MARKERS = ("翻倍", "倍增", "增长", "提升", "扩张", "释放", "扩产")


def _hard_event_novelty_facts(news: dict) -> set:
    """返回标题/正文中的硬事件事实族，避免单独宽泛主题词触发豁免。"""
    text = f"{news.get('title', '')} {news.get('content', '')}"
    facts = {name for name, words in _HARD_EVENT_NOVELTY_GROUPS
             if any(word in text for word in words)}
    if "产能" in text and any(word in text for word in _CAPACITY_COMPOUND_MARKERS):
        facts.add("capacity_expansion")
    if "回购" in text and any(word in text for word in ("完成", "完毕", "注销", "实施")):
        facts.add("buyback_completion")
    return facts


def _hard_event_chain_key(sig: dict, news: dict) -> str:
    """生成一次性豁免的事件链键；弱包装标题不会因历史存在自动消耗豁免。"""
    facts = _hard_event_novelty_facts(news)
    if not facts:
        return ""
    entities = sorted({_normalize_entity(e) for e in (
        _as_list(sig.get("stocks")) + _as_list(sig.get("entities"))) if e})
    sectors = sorted({str(s).strip() for s in _as_list(sig.get("sectors")) if str(s).strip()})
    return "facts=" + ",".join(sorted(facts)) + "|entities=" + ",".join(entities) + \
        "|sectors=" + ",".join(sectors)


def _hard_event_topic_exemption_available(news: dict, judge: dict,
                                          sig: dict, pushed_events: list) -> bool:
    """题材已饱和时，强方向硬事件是否还能获得一次链路豁免。"""
    if judge.get("direction") not in ("bullish", "bearish"):
        return False
    chain = _hard_event_chain_key(sig, news)
    if not chain:
        return False
    return not any(
        pe.get("topic_exemption") and pe.get("topic_exemption_chain") == chain
        for pe in pushed_events
    )


def _market_theme_keys(text: str) -> set:
    """标题命中的 market 主题键集合（market 级同主题饱和计数槽位）"""
    return {tk for tk, kws in _MARKET_THEME_PATTERNS if any(k in text for k in kws)}


def _sig_theme_keys(sig: dict) -> set:
    """事件命中的 market 主题键：标题 + 实体联合扫描。

    2026-08-25 实证漏拦："国际海事组织…中东海域68起袭击"标题无主题词，
    但 entities=[伊朗]——主题归属承载在实体上，仅扫标题会漏。
    """
    text = str(sig.get("title_norm") or "")
    for k in ("entities", "stocks"):
        text += " " + " ".join(str(x) for x in _as_list(sig.get(k)))
    return _market_theme_keys(text)


def _market_theme_saturated(keys: set, pushed_events: list) -> bool:
    """同 theme 24h 内已推 ≥ MARKET_TOPIC_PUSH_LIMIT 条则饱和（与板块饱和同窗口口径）"""
    now_ts = time.time()
    cnt = 0
    for pe in pushed_events:
        t = str(pe.get("t") or "")
        ts = _state_timestamp(t)
        if ts is None:
            continue
        if now_ts - ts > TOPIC_PUSH_WINDOW_H * 3600:
            continue
        if _sig_theme_keys(pe) & keys:
            cnt += 1
    return cnt >= MARKET_TOPIC_PUSH_LIMIT


def _is_macro_policy(news: dict) -> bool:
    """候选是否属宏观/政策/地缘类（溢出排序优先，防宏观被科技噪声挤占）"""
    title = str(news.get("title", "") or "")
    content = str(news.get("content", "") or "")
    text = f"{title} {content}"
    return any(k in text for k in _MACRO_POLICY_KEYWORDS)


# ============================================================
# 标题党治理（2026-09-07 P1）：实质版优先
# ============================================================
# 增量信息词：标题表达事实变化（数值变动/事件落地）而非情绪渲染
_HEADLINE_INCREMENTAL_MARKERS = (
    "较", "增至", "涨至", "跌至", "降至", "回落至", "增长", "增逾", "增",
    "落地", "获批", "核准", "中标", "签署", "达成", "上调", "下调",
    "增持", "减持", "回购", "解除质押", "质押", "扩产", "投产", "交付",
    "创新高", "创下", "突破", "立案", "调查", "回购",
)
# 纯情绪化词：渲染性措辞（仅在无数字、无增量信息时才判情绪化）
_HEADLINE_EMOTIONAL_MARKERS = (
    "疯涨", "疯抢", "炸裂", "惊呆", "震惊", "崩了", "疯了", "太猛",
    "杀疯了", "史诗级", "王炸", "炸锅", "嗨了", "慌了", "火了", "爆了",
    "彻底慌", "要变天", "大事不好", "紧急提醒", "速看", "刚刚传来",
)


def _headline_quality(news: dict) -> int:
    """标题"实质版"质量分（候选择优溢出排序用）

    +1 实质版：标题含具体实体名 + 具体数字 + 增量信息（较/增至/落地等）
    -1 纯情绪化：情绪词/感叹号渲染且无数字、无增量信息
     0 中性
    """
    title = str(news.get("title", "") or "").strip()
    if not title:
        return 0
    has_digit = bool(re.search(r"\d", title))
    has_incr = any(m in title for m in _HEADLINE_INCREMENTAL_MARKERS)
    has_entity = _title_has_entity(news, title)
    if has_entity and has_digit and has_incr:
        return 1
    if not has_digit and not has_incr and (
            any(m in title for m in _HEADLINE_EMOTIONAL_MARKERS)
            or "！" in title or title.count("!") >= 1):
        return -1
    return 0


_TRUNCATED_ENDINGS = ("…", "。。。", "...", "。。")


def _is_truncated_title(title: str) -> bool:
    """判断标题主体是否被省略号截断，兼容金十的【新闻主体】正文格式。"""
    t = str(title or "").strip()
    bracketed = re.search(r"【([^】]*)】", t)
    if bracketed:
        # 金十标题的新闻主体包在【】内；主体完整时，后面的正文截断不影响推送价值。
        if bracketed.group(1).strip().endswith(_TRUNCATED_ENDINGS):
            return True
        suffix = t[bracketed.end():].strip()
        # 闭合括号后只有裸省略号时，仍按标题截断标记处理；正文前缀后的省略号则忽略。
        return suffix in _TRUNCATED_ENDINGS
    return t.endswith(_TRUNCATED_ENDINGS)


def _title_has_entity(news: dict, title: str) -> bool:
    """标题是否含具体实体名：显式字段（name/affected_stocks/entities）
    出现在标题中，或公告式标题冒号前主体（"中际旭创:关于回购…"）"""
    entities = [str(e).strip() for e in _as_list(news.get("affected_stocks"))]
    entities += [str(e).strip() for e in _as_list(news.get("entities"))]
    name = str(news.get("name", "") or "").strip()
    if name:
        entities.append(name)
    if any(e and e in title for e in entities):
        return True
    if ("：" in title or ":" in title):
        head = re.split("[：:]", title, 1)[0].strip()
        if 2 <= len(head) <= 12:
            return True
    return False


def _candidate_sort_key(x: dict) -> tuple:
    """候选择优排序键（溢出截断时用）：高信号 > 宏观 > 完整标题 > 实质版 > 预筛分

    2026-09-07 标题党治理：同层内实质版（实体+数字+增量信息）优先，
    纯情绪化标题沉底——多源同事件各措辞竞逐最后槽位时，实质版胜出。
    截断标题只在同一优先层内沉底，避免候选溢出时挤掉高信号/宏观/实质版条目。
    """
    return (
        1 if x.get("_hit_signal") else 0,
        1 if _is_macro_policy(x) else 0,
        1 if not x.get("_truncated") else 0,
        _headline_quality(x),
        x["_pref_score"],
    )


def _topic_saturated(sig: dict, pushed_events: list) -> bool:
    """同题材推送是否已达饱和：market 级仅宏观数据豁免（其余走主题槽位，上限3）；
    板块/实体 24h 内已推 ≥ 上限（5）

    2026-08-13 P1 修复（口径对齐）：板块重叠改用 _sectors_overlap（子串包含，
    "AI算力"与"AI/算力"同板块）、实体重叠用归一化后交集（"大摩"与"摩根士丹利"
    同实体）——此前用精确交集，与 _is_same_event 合并口径不一致，实体为空时
    板块表述不一致会漏判同题材，饱和限流失准。
    """
    scope = str(sig.get("scope") or "stock")
    title = str(sig.get("title_norm") or "")
    # 2026-08-25 修复：market 级仅"宏观数据发布"豁免（CPI/非农等数据本体必须推）；
    # 地缘/制裁/关税/央行表态类 market 走同主题槽位（上限3）。主题检查先于实体
    # 槽位路径——实证漏拦：伊朗海事事件 entities=[伊朗]非空时走实体槽位（上限5，
    # 窗口内仅4条）绕过了主题路径（上限3，窗口内5条本应拦截）。
    if scope == "market":
        if _is_macro_data_release(title):
            return False
        keys = _sig_theme_keys(sig)
        if keys and _market_theme_saturated(keys, pushed_events):
            return True
    secs = set(_as_list(sig.get("sectors")))
    ents = {_normalize_entity(e) for e in _as_list(sig.get("stocks"))} | \
           {_normalize_entity(e) for e in _as_list(sig.get("entities"))}
    if not secs and not ents:
        return False
    now_ts = time.time()
    cnt = 0
    for pe in pushed_events:
        t = str(pe.get("t") or "")
        ts = _state_timestamp(t)
        if ts is None:
            continue
        if now_ts - ts > TOPIC_PUSH_WINDOW_H * 3600:
            continue
        psecs = set(_as_list(pe.get("sectors")))
        pents = {_normalize_entity(e) for e in _as_list(pe.get("stocks"))} | \
                {_normalize_entity(e) for e in _as_list(pe.get("entities"))}
        if _sectors_overlap(secs, psecs) or (ents & pents):
            cnt += 1
    return cnt >= TOPIC_PUSH_LIMIT


def _central_bank_shared(ent_a: set, ent_b: set) -> bool:
    """双方实体是否共享同一央行（归一化后精确匹配，防日本央行 vs 美联储误并）"""
    ca = {_normalize_entity(e) for e in ent_a} & _CENTRAL_BANK_ENTITIES
    cb = {_normalize_entity(e) for e in ent_b} & _CENTRAL_BANK_ENTITIES
    return bool(ca & cb)


def _policy_direction_conflict(ta: str, tb: str) -> bool:
    """宏观政策方向冲突（鹰派 vs 鸽派），防"考虑加息 vs 考虑降息"被宏观分支误并

    _title_direction_conflict 的涨跌表不含"加息/降息"（"加息"无"涨/升"字、
    "降息"含"降"但不含"跌"），对货币政策方向对立识别失效，需单独守卫。
    只有双方各自只表达一个相反方向时才拒绝合并；同一标题同时描述先加息再降息
    等情形属于混合表态，不足以证明两条报道方向对立。
    """
    hawk_a = any(w in ta for w in _POLICY_HAWK_WORDS)
    dove_a = any(w in ta for w in _POLICY_DOVE_WORDS)
    hawk_b = any(w in tb for w in _POLICY_HAWK_WORDS)
    dove_b = any(w in tb for w in _POLICY_DOVE_WORDS)
    return ((hawk_a and not dove_a and dove_b and not hawk_b)
            or (dove_a and not hawk_a and hawk_b and not dove_b))


class _SameEventCtx(NamedTuple):
    """_is_same_event 规则族共享上下文：签名字段一次性抽取，规则函数零重复解析"""
    ta: str            # 归一化标题（str(x or "")，None→""）
    tb: str
    ent_a: set         # 原始实体集（个股∪实体；归一化/模糊匹配在 _entity_overlap 内做）
    ent_b: set
    ev_a: set          # 事件组（已剔除市场状态类词）
    ev_b: set
    num_a: set         # 核心金额
    num_b: set
    sec_a: set
    sec_b: set
    shared_ev: set     # ev_a & ev_b
    ent_overlap: bool


def _same_event_ctx(sig_a: dict, sig_b: dict) -> _SameEventCtx:
    """从两个推送级签名抽取规则判定所需的全量上下文"""
    ent_a = set(_as_list(sig_a.get("stocks"))) | set(_as_list(sig_a.get("entities")))
    ent_b = set(_as_list(sig_b.get("stocks"))) | set(_as_list(sig_b.get("entities")))
    # 2026-08-14 修复：过滤市场状态类事件组（"行情下跌"由 content 关键词规则提取，
    # 属市场状态描述而非事件动词）。否则非空 events 使"共享事件组"分支
    # （shared_ev=∅）与"双方均无事件组"分支（not ev_a 不成立）同时失效，
    # 同事件多源报道无法合并（日本央行加息三连推实证）。
    ev_a = {e for e in set(_as_list(sig_a.get("events"))) if e not in _MARKET_STATE_EVENTS}
    ev_b = {e for e in set(_as_list(sig_b.get("events"))) if e not in _MARKET_STATE_EVENTS}
    # 2026-08-11 修复：实体模糊重叠（"索尼"⊂"索尼半导体解决方案公司"），
    # 此前仅精确相等，跨源同事件（索尼×台积电合资）48h 内三推实证
    return _SameEventCtx(
        ta=str(sig_a.get("title_norm", "") or ""),
        tb=str(sig_b.get("title_norm", "") or ""),
        ent_a=ent_a, ent_b=ent_b,
        ev_a=ev_a, ev_b=ev_b,
        num_a=set(_as_list(sig_a.get("numbers"))),
        num_b=set(_as_list(sig_b.get("numbers"))),
        sec_a=set(_as_list(sig_a.get("sectors"))),
        sec_b=set(_as_list(sig_b.get("sectors"))),
        shared_ev=ev_a & ev_b,
        ent_overlap=_entity_overlap(ent_a, ent_b),
    )


def _same_event_core_facts(ctx: _SameEventCtx) -> bool:
    """核心事实/别名/数字锚点兜底，识别包装标题与原始报道的同事件关系。

    只有共享高置信事实且具备实体或数字锚点才合并；双方都给出数字事实时，
    数字不一致直接终止，避免同公司不同代际/不同金额的事件被误合并。
    """
    if _title_direction_conflict(ctx.ta, ctx.tb):
        return False
    if not (_core_fact_anchors(ctx.ta) & _core_fact_anchors(ctx.tb)):
        return False
    if ctx.num_a and ctx.num_b and not (ctx.num_a & ctx.num_b):
        return False
    title_num_a = _title_numeric_anchors(ctx.ta)
    title_num_b = _title_numeric_anchors(ctx.tb)
    if title_num_a and title_num_b and not (title_num_a & title_num_b):
        return False
    return bool(ctx.ent_overlap or (ctx.num_a & ctx.num_b)
                or (title_num_a & title_num_b))


def _same_event_shared_group(ctx: _SameEventCtx) -> bool:
    """规则1-3：双方共享事件组（shared_ev≠∅）时的合并判定

    1. 主体(实体)交集非空 + 标题守卫 → 同主体同事件
    2. 核心金额交集非空 → 同事件不同措辞同金额
    3. 主体交集为空但标题 LCS≥5 → 多源同事件报道
    """
    if ctx.ent_overlap:
        # 2026-08-11 修复（误并实证）：共享事件组 + 实体模糊重叠 直接合并会把
        # "期货早报…连续21月增持黄金"（事件组=增持）与"多重稳市信号释放…"
        # （content 含"增持"事件组）误并为同事件——两者标题零共享。
        # 守卫：标题需共享 ≥3 字连续子串、字符集 Jaccard ≥0.15、或共享高置信
        # 事件短语锚（寒武纪股权激励类：同个股+同事件组+标题同主题 仍合并）。
        if ctx.ta and ctx.tb:
            return (
                _lcs_len(ctx.ta, ctx.tb) >= 3
                or len(set(ctx.ta) & set(ctx.tb)) / len(set(ctx.ta) | set(ctx.tb)) >= 0.15
                or any(p in ctx.ta and p in ctx.tb for p in _EVENT_PHRASE_ANCHORS)
            )
        return bool(ctx.ta or ctx.tb)
    if ctx.num_a & ctx.num_b:
        return True
    return _lcs_len(ctx.ta, ctx.tb) >= 5


def _same_event_cross_family(ctx: _SameEventCtx) -> bool:
    """事件组抽取不稳定兜底（2026-09-01 修复：美债收益率 4.75% 双推实证）

    21:34"美债10Y收益率升至4.75%为2025年1月以来最高"(entities=美联储)
    vs 22:02"美债10Y收益率突破4.75%创2025年1月以来新高"(entities=美债)
    28 分钟内双推。根因：LLM 对行情类快讯的事件组抽取不稳定——一方抽到
    事件组、另一方为空（或双方各抽各的），shared_ev=∅ 且不满足
    "双方均无事件组"，_is_same_event 直接 return False，连标题
    Jaccard 0.64 的规则4 都没机会执行。

    兜底判据（严守误并风险）：
    - 方向对立终止（"升至" vs "跌破"，沿用 _title_direction_conflict）
    - 金额守卫：双方金额均明确且无交集 → 不同事件
    - 标题高度相似才合并：字符集 Jaccard≥0.55，或连续 LCS≥8 且覆盖
      短标题 ≥50%（美债案例 Jaccard 0.643、LCS"美国10年期国债收益率"=10 字）
    """
    ta, tb = ctx.ta, ctx.tb
    if not (ta and tb):
        return False
    if _title_direction_conflict(ta, tb):
        return False
    if (ctx.num_a and ctx.num_b) and not (ctx.num_a & ctx.num_b):
        return False
    if len(set(ta) & set(tb)) / len(set(ta) | set(tb)) >= 0.55:
        return True
    shorter = min(len(ta), len(tb))
    if shorter >= 8:
        l = _lcs_len(ta, tb)
        if l >= 8 and l / shorter >= 0.5:
            return True
    return False


def _is_same_event(sig_a: dict, sig_b: dict) -> bool:
    """判断两个推送级事件签名是否指向同一事件（满足其一即同事件）

    结构（SNA-04 重构，行为与重构前完全等价，57 处去重专项测试锁定）：
    - 共享事件组族 _same_event_shared_group：规则1-3
    - 无事件组族 _same_event_no_group：规则4-8（市场域/实体锚定/标题相似度）
    - 跨族兜底 _same_event_cross_family（2026-09-01）：事件组单边缺失/双方
      各抽各的时的标题相似度兜底（美债 4.75% 双推实证）

    规则目录：
    1. 主体(个股/LLM实体)交集非空 且 事件组交集非空 → 同主体同事件
       （"寒武纪股权激励大消息" vs "寒武纪:2026年限制性股票激励计划(草案)"）
    2. 核心金额交集非空 且 事件组交集非空 → 同事件不同措辞同金额
       （"30.53亿补充协议" vs "30.53亿元协议"）
    3. 主体交集为空 但事件组交集非空 且 归一化标题最长公共子串≥5 →
       多源同事件报道（"恩智浦洽谈收购Ambarella" vs "安霸股价因传恩智浦洽谈收购而飙升"）
    4. 双方均无事件组（普通流水）且标题字符集 Jaccard≥0.6 → 同一条目的改写
    5. 双方均无事件组 且 归一化标题 LCS 覆盖较短标题≥55%（同实体/同金额放宽至35%）→ 同事件
       （板块/产业资讯兜底：存储ETF纳入、券商研报等 events/entities/numbers 全空，
        Jaccard 因长标题被稀释到 0.4 拦不住，实证花旗研报×2、长鑫ETF×2 同轮双推）
       —— LCS 兜底前先做方向对立守卫，防止涨/跌相反但句式相近的报道被误合并
    6. 双方均无事件组 且 同市场时段域（美股开盘/午评/收盘类多源快讯）→ 同事件
       （2026-08-03 21:32 美股开盘三源三推实证：标题措辞差异大，
        "美股开盘三大股指齐涨" vs "道指开盘涨0.52%" 仅共享'开盘'，
        Jaccard/LCS 均兜不住；按 同时段组 + 同市场域 + 方向不冲突 合并）
    7. 双方均无事件组 且 同市场域 + 共享市场指数词（盘中行情动态）→ 同事件
       （2026-08-04 00:32 纳指三推实证：美股涨幅扩大纳指涨超2 / 纳指涨200现报… /
        纳指涨超2 Meta涨超6 无时段词，Jaccard 0.20~0.33、LCS 占比 0.25~0.46，
        但均含"纳指"且方向一致）
    （板块行情/宏观数据/央行政策/实体锚定等增量规则见各规则函数内注释）
    """
    ctx = _same_event_ctx(sig_a, sig_b)
    if _same_event_core_facts(ctx):
        return True
    if ctx.shared_ev:
        return _same_event_shared_group(ctx)
    if not ctx.ev_a and not ctx.ev_b:
        return _same_event_no_group(ctx)
    # 2026-09-01：事件组单边缺失/双方各抽各的（LLM 对行情类快讯抽取不稳定）
    # 不再直接 return False——美债 4.75% 双推实证，改走跨族相似度兜底
    # （方向/金额守卫 + 高相似阈值，见 _same_event_cross_family）
    return _same_event_cross_family(ctx)


def _same_event_no_group(ctx: _SameEventCtx) -> bool:
    """规则4-8：双方均无事件组（普通流水/板块资讯/行情快讯）时的合并判定"""
    ta, tb = ctx.ta, ctx.tb
    if not (ta and tb):
        return False
    if _title_direction_conflict(ta, tb):
        return False
    r = _same_event_market_rules(ctx)
    if r is not None:
        return r
    # 规则4：标题字符集 Jaccard≥0.6 → 同一条目的改写
    if len(set(ta) & set(tb)) / len(set(ta) | set(tb)) >= 0.6:
        return True
    r = _same_event_entity_anchor_rules(ctx)
    if r is not None:
        return r
    return _same_event_title_similarity(ctx)


def _same_event_market_rules(ctx: _SameEventCtx) -> bool | None:
    """市场域规则族：时段组/指数词/板块行情/宏观数据/央行政策类多源快讯合并

    返回 True=同事件 / False=方向对立终止（不再走后续规则）/ None=无判定。
    """
    ta, tb = ctx.ta, ctx.tb
    # 市场开收盘/复盘类快讯：同时段组 + 同市场域 → 同事件（多源措辞差异大）
    if _session_group(ta, tb) is not None and _market_domain_overlap(ta, tb):
        return True
    # 盘中行情动态（涨超/现报/涨幅扩大等）：同市场域 + 共享市场指数词
    # + 时段不冲突（防 午评 vs 收盘 因共享指数词误并）→ 同事件
    if (not _session_conflict(ta, tb) and _market_domain_overlap(ta, tb)
            and _shared_index_token(ta, tb)):
        return True
    # 2026-08-12 修复（22:01 同轮双推实证）：美股盘初"光通信/存储普涨"
    # 多源报道 entities 各抽各的、无共享时段/指数词、LCS 仅"光通信"3字——
    # 既有分支全部兜不住。新增 sectors 交集合并：均无事件组 + 板块交集
    # + 同市场域 + 任一含行情措辞（方向一致已在入口守卫）。
    # 误并评估：sectors 不同（涨价逻辑 vs 行情情绪）通常交集为空；
    # 交集非空且同域时，少推一条比重复推送更符合用户口径。
    if (ctx.sec_a & ctx.sec_b) and _market_domain_overlap(ta, tb):
        if any(w in ta for w in _MARKET_MOVE_WORDS) or any(w in tb for w in _MARKET_MOVE_WORDS):
            return True
    # 2026-08-12 修复（21:31 vs 22:01 跨轮重复实证）：同一宏观数据事件
    # （CPI符合预期美股高开 / 通胀温和美股盘初纳指涨）48h 内推两次。
    # 双方均含宏观数据词 + 同市场域 → 同事件（方向一致已在入口守卫）。
    # 市场域守卫防不同国家 CPI 误并（德国/意大利 CPI 标题无美股域词）。
    if _market_domain_overlap(ta, tb) and \
            any(w in ta for w in _MACRO_EVENT_WORDS) and \
            any(w in tb for w in _MACRO_EVENT_WORDS):
        return True
    # 2026-08-14 修复（日本央行加息三连推实证）：宏观流动性/货币政策事件
    # 多源报道措辞差异大（"最快可能在9月加息" vs "加息或提速美元对日元急跌
    # …流动性冲击"），LCS 仅 4 字、jaccard 0.15~0.20、无事件组/板块交集，
    # 且三条均 market 级（_topic_saturated 豁免），既有分支全部兜不住。
    # 新增：共享同一央行实体 + 标题均含宏观政策词 + 政策方向不冲突 → 同事件。
    # 守卫：①同一央行实体（防日本央行 vs 美联储误并）；②方向守卫（防
    # "考虑加息 vs 考虑降息"）；③标题均含政策词（防仅共享"日本央行"实体
    # 的两条无关央行新闻被误并）。
    if _central_bank_shared(ctx.ent_a, ctx.ent_b):
        if _policy_direction_conflict(ta, tb):
            return False
        if (any(w in ta for w in _MACRO_POLICY_EVENT_WORDS)
                and any(w in tb for w in _MACRO_POLICY_EVENT_WORDS)):
            return True
    return None


def _same_event_entity_anchor_rules(ctx: _SameEventCtx) -> bool | None:
    """实体锚定规则族：同实体（模糊重叠）+ 金额/短语锚/板块/剔除实体后共享内容

    返回 True=同事件 / False=金额冲突终止 / None=无判定。
    """
    ta, tb = ctx.ta, ctx.tb
    if not (ctx.ent_overlap and not _title_direction_conflict(ta, tb)):
        return None
    # 2026-08-11 修复：同实体 + 共享事件短语 → 同事件（跨日报道实体顺序相反、
    # 措辞差异大，LCS 兜不住：索尼×台积电合资三推实证）。金额守卫：
    # 双方金额均明确且无交集 → 不同事件（防"50亿建厂 vs 10亿回购"误并漏推）。
    if (ctx.num_a and ctx.num_b) and not (ctx.num_a & ctx.num_b):
        return False
    if any(p in ta and p in tb for p in _EVENT_PHRASE_ANCHORS):
        return True
    # 2026-08-13 修复：同实体 + 无事件组 + 同板块（sectors 交集≥2 词）
    # + 标题共享≥2字 → 同事件（四川算力政策同轮双推实证："建强成都平原
    # 算力核心区" vs "布局万卡级以上智算集群" 同实体同板块、无共享锚、
    # LCS 仅 2 字，既有分支全兜不住）。
    # 守卫：金额冲突已排除；sectors 交集≥2 防"英伟达AI芯片 vs 英伟达AI服务器"
    # 仅共享宽泛"AI"被误并（漏推守卫）。
    # 2026-08-13 二轮：sectors 用 _sectors_overlap（子串包含）替代精确交集，
    # 修复 LLM 抽板块"AI算力"vs"AI/算力"不一致导致的漏合并（上海算力补贴×2）。
    if len(_sectors_overlap(ctx.sec_a, ctx.sec_b)) >= 2 and _lcs_len(ta, tb) >= 2:
        return True
    # 2026-08-25 审核实证：同实体兄弟报道（字节豆包×2/小米玄戒×2/华为
    # 发布会×2）措辞差异大、无事件组/板块交集/LCS 不足，同轮重复推送。
    # 判据：实体重叠 + 剔除实体词后标题仍共享连续内容；共享≥6字专有
    # 主题词，或同板块时共享≥2字。避免"国际标准/美加贸易"4字通用短语
    # 把固态电池/磁性元件、关税抬升/谈判破裂两对不同事件误并。
    sa = _strip_entities(ta, ctx.ent_a)
    sb = _strip_entities(tb, ctx.ent_b)
    if sa and sb:
        s_lcs = _lcs_len(sa, sb)
        # 共享连续段 ≥6（专有主题词，如"发布豆包工作"）；或同板块 + ≥2
        #（玄戒/华为发布会类）。纯 ≥4 兜底会误并两对不同事件——
        # "固态电池国际标准" vs "磁性元件国际标准"（共享"国际标准"4字）、
        # "关税抬升至50" vs "美加贸易谈判破裂"（共享"美加贸易"4字）。
        if s_lcs >= 6 or (len(_sectors_overlap(ctx.sec_a, ctx.sec_b)) >= 1 and s_lcs >= 2):
            return True
    return None


def _same_event_title_similarity(ctx: _SameEventCtx) -> bool:
    """标题相似度兜底：LCS 连续子串与子序列阈值，同实体/同金额放宽阈值

    2026-08-11 修复：同主体(实体模糊重叠)或同金额时放宽标题相似阈值——
    跨日报道措辞差异大（"拟合资建厂" vs "批准成立合资企业"），
    但主体+金额一致，放宽 LCS 即可合并，防 48h 内重复推送
    （韩国5万亿基金×2、索尼台积电合资×3 实证）。
    放宽仅对"同实体/同金额"生效：SK海力士不同事件（扩产 vs 股东回报）
    标题无共享长段，仍不会被误并（漏推守卫）。
    """
    ta, tb = ctx.ta, ctx.tb
    shorter = min(len(ta), len(tb))
    if shorter < 8:
        return False
    same_anchor = ctx.ent_overlap or bool(ctx.num_a & ctx.num_b)
    # 误并守卫（2026-08-11 实证）："央行授权德银" vs "央行十五五规划"
    # 仅因实体名"中国人民银行"重复出现，LCS 子序列虚高至 0.52 被误并。
    # 同锚放宽前要求：剔除实体词后标题仍有 ≥3 字连续共享内容，
    # 即两条报道除主体外确实描述同一件事。
    if same_anchor:
        strip_a = _strip_entities(ta, ctx.ent_a)
        strip_b = _strip_entities(tb, ctx.ent_b)
        if strip_a and strip_b and _lcs_len(strip_a, strip_b) < 3:
            return False
    # 连续子串兜底（比例足够高 → 同事件）
    if _lcs_len(ta, tb) / shorter >= (0.35 if same_anchor else 0.55):
        return True
    # 子序列兜底：允许单字替换（"存储ETF" vs "内存ETF"）
    # 仅在匹配长度绝对充足且占比高时启用，避免日常流水误并
    sub = _lcs_subseq_len(ta, tb)
    sub_min = 10 if same_anchor else 12
    sub_ratio = 0.45 if same_anchor else 0.6
    return sub >= sub_min and sub / shorter >= sub_ratio


# ============================================================
# 规则预筛
# ============================================================
# 宏观数据发布识别（2026-08-12 修复：美国7月CPI系列87条全漏推——
# 根因是 HIGH_SIGNAL_KEYWORDS 缺 CPI/PPI 等词（已补），此为第二层兜底：
# 覆盖"消费者物价指数""生产者出厂价格"等全称变体，以及数据语境快讯。
# 命中 = 宏观数据词 + 数据语境（公布/预期/年率/录得等），确定性事件直接送 LLM 判定。
# 注：数据词用词边界匹配（CPI 不误命中普通文本）；不含数据语境的数据词
# （如"芯片通胀""油价上涨"）不在此直通，仍走常规打分路径。
_MACRO_DATA_PATTERNS = [
    r"(?<![A-Za-z0-9])CPI(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])PPI(?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])FOMC(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])GDP(?![A-Za-z0-9])",
    r"(?<![A-Za-z0-9])PMI(?![A-Za-z0-9])", r"(?<![A-Za-z0-9])LPR(?![A-Za-z0-9])",
    "非农", "失业率", "通胀", "物价", "利率决议", "美债", "社融", "贸易帐",
    "消费者物价", "生产者物价", "出厂价格", "居民消费价格", "工业生产者",
]
_MACRO_RELEASE_CONTEXT = ["公布", "发布", "数据", "预期", "前值", "年率", "月率",
                          "录得", "符合预期", "同比", "环比", "符合市场预期"]


def _is_macro_data_release(text: str) -> bool:
    """宏观数据发布类快讯识别：数据词 + 数据语境 → 直通 LLM 判定"""
    if not text:
        return False
    if not any(re.search(p, text) for p in _MACRO_DATA_PATTERNS):
        return False
    return any(c in text for c in _MACRO_RELEASE_CONTEXT)


# 大额经营事件动作词（2026-08-13 修复：预筛漏判实证——"宁德时代签订200亿储能订单"0.19、
# "SK海力士重启中国NAND工厂"0.39、"贵州茅台中标50亿工程"0.14 均因词表缺动作词被拦。
# 中标/签约/建厂/扩产等是"重大经营事件"信号，与 HIGH_SIGNAL 的并购/重组同级，
# 但刻意不并入 HIGH_SIGNAL_KEYWORDS（避免扩大指纹 sig 路径影响面——
# 这些词直通 LLM 判定即可，是否推送仍由 LLM 把关龙头/重要性）。
_DEAL_ACTION_WORDS = [
    "中标", "签约", "签订", "签署", "斩获", "拿下",
    "大额订单", "重大合同", "重大工程", "建厂", "投建", "投产", "扩产", "扩建",
    "重启", "量产", "增资", "落地",
]
_DEAL_AMOUNT_RE = re.compile(r"\d+(?:\.\d+)?\s*亿")


def _is_major_deal_event(text: str) -> bool:
    """大额经营事件识别：动作词 + (金额≥1亿 或 科技硬件词) → 直通 LLM 判定

    只影响中小市值个股自身的中标/签约按用户口径不推，但预筛阶段无法知道
    谁是龙头，故"大额经营事件"统一送 LLM，由 LLM 判断龙头与否（与"回购/并购"
    等资本运作词的处理一致）。小额（<1亿）且非科技的经营动作不直通，避免
    中小市值日常经营消息挤占候选。
    """
    if not text:
        return False
    if not any(a in text for a in _DEAL_ACTION_WORDS):
        return False
    if _DEAL_AMOUNT_RE.search(text):
        return True
    if _has_tech_keyword(text):
        return True
    return False


def _hit_watchlist(news: dict, watchlist: set) -> bool:
    """资讯主体是否命中自选龙头名单（标题/名称/代码匹配）"""
    if not watchlist:
        return False
    title = str(news.get("title", "") or "")
    name = str(news.get("name", "") or "")
    content = str(news.get("content", "") or "")
    for n in watchlist:
        if n and (n in title or n in name or n in content):
            return True
    return False
