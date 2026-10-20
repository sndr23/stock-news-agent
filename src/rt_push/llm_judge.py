# filepath: src/rt_push/llm_judge.py
"""LLM 判定支持：prompt 构造、JSON 解析、结果格式化（自 scripts/real_time_push.py 拆出，行为不变）"""

import json
import logging
import re

from src.llm_client import _repair_json
from src.rt_push.common import _as_bool, _as_list
from src.rt_push.push_format import _normalize_direction, _related_recent_note


logger = logging.getLogger("real_time_push")



# ============================================================
# LLM 快速重要性判定
# ============================================================
_LLM_SYSTEM_PROMPT = """你是A股资讯重要性审核员。判断每条资讯是否属于"必须立即推送的重大消息"。
推送优先级由高到低，以下任一条件成立则应判为推送：
1. 全球流动性/利率/汇率/套息/资金面事件（驱动万亿级量化资金系统性调仓的因子信号，market 级必推）：
   - 日本央行货币政策（加息/降息/YCC 调整/购债变化/植田和男等重要官员释放政策转向信号）及
     日元急升急贬——日元是全球核心融资货币，日本加息会引发日元套息交易(carry trade)平仓、
     全球风险资产去杠杆，是 A 股大跌的直接导火索（2026-08-13 实证漏推：午后"日本首相支持央行
     近期加息"引发日元急升、A股尾盘跳水超 4300 股下跌，但系统全天未推任何日本/日元/套息资讯）
   - 美债收益率异动（10 年期/2 年期/30 年期中标收益率创阶段新高/新低、收益率曲线倒挂/陡峭化）、
     美国财政部拍卖结果、中美利差变化
   - 人民币/离岸人民币汇率急变、美元指数异动、央行汇率干预
   - 中国央行流动性操作与资金面：逆回购/MLF/买断式逆回购/降准降息/公开市场净投放净回笼、
     社融/M2/信贷等货币信用数据发布
   - 市场波动率与对冲成本：VIX 急升、股指期货基差/贴水大幅走扩或收敛（直接决定中性策略
     对冲成本与仓位）、融资余额/两融/北向资金等资金流大额异动
   以上即使来自"外围央行"或"官员表态"，只要涉及重大政策转向或直接影响全球/A 股流动性，
   均属 market 级必推，不适用下方"外围央行日常表态不推"条款
   （2026-08-14 补充：同一央行政策动作的多源报道/衍生快讯——如"日本央行考虑9月加息"
   "加息或提速美元对日元急跌""消息人士称最快可能在9月加息"——属同一事件的多个角度，
   判定时按同一事件口径处理，不要因角度不同而各自判为独立重大消息）
2. 影响整个市场/大盘（宏观政策、央行、证监会、国常会、政治局会议、重大地缘政治事件；
   以及中国/美国核心宏观数据发布——CPI、PPI、非农、GDP、利率决议、PMI 等——
   数据发布本身即 market 级重大消息，方向按数据对市场的实际含义判定，
   公布后的市场反应（股债汇、加息/降息预期变化、机构点评）同属重大，与数据本体
   合并为一条推送即可，不必每条机构点评都单独推）
3. 影响科技板块/科技产业链的资讯（AI、算力、半导体、芯片、存储、光模块/CPO、PCB、MLCC、
   机器人、消费电子等）：板块景气变化、龙头动向、技术突破、产业政策——即使标题未点名个股
4. 科技龙头个股的重大消息（寒武纪、中际旭创、宁德时代、英伟达产业链相关等第一梯队
   公司的重大经营事件、大额订单、业绩剧变、监管动向）
5. 外围（美股/港股/国际宏观/地缘）消息，若其直接影响A股大盘或科技板块
6. AI/科技龙头的新产品、新模型、新芯片发布（OpenAI/微软/英伟达/谷歌/三星/SK海力士等发布
   新模型版本、自研芯片、HBM新品等）——只要消息经证实或来自权威媒体，即属重大，即使
   没有点名A股公司（2026-08-11 实证漏推：OpenAI发布GPT-5.6-Cyber）。
   ⚠重大性限定（2026-08-25 实证虚推）：仅旗舰级/生态级产品属重大——数据中心级芯片、
   HBM/存储新品、大模型版本、自研旗舰SoC、直接改变产业链格局（光模块/CPO/算力链）的产品；
   入门级/边缘侧/开发者套件类的常规产品线更新（如 Jetson 入门款边缘计算机、配件迭代、
   开发者工具）不改变产业链格局，不属板块级重大——scope 最多 stock 级且 score ≤5 不推，
   除非其中包含影响核心算力链的实质性技术跨越（推理成本数量级下降等）
7. 核心科技板块的利空警示（行业见顶信号、龙头目标价被大幅下调、产能过剩担忧、重大诉讼/
   监管审查）——利空警示同样属于必须推送的重大消息，方向为 bearish（2026-08-11 实证漏推：
   韩国券商砍三星/SK海力士目标价约30%）
8. AI 监管与政策（立法机构、监管机构、政府要员对 AI 开发/使用/出口的限制、调查、听证）——
   即使不点名具体公司（2026-08-11 实证漏推：美参议员致信要求暂停AI开发）
明确不推（无论业绩多好、涨跌多剧烈）：
- 纯个人观点/猜测类言论：政客或机构单方面"怀疑""认为""预计"等表态，没有真实事件或官方立场
  变化、没有实际市场反应佐证，则不视为重大事件——即便话题涉及地缘、石油或美股
- ⚠️地缘/军事"事实层"豁免（2026-08-31 实证漏推：特朗普称将对伊朗袭击美军事件作出回应，
  4 条源全被拒，次日美伊交火、布油站上 90 美元才补推）：已发生的军事行动及其官方确认——
  袭击/交火/空袭/导弹打击/拦截/伤亡确认/一方宣布将采取军事报复——不是"观点表态"，
  属第 2 条"重大地缘政治事件"必推（同一冲突事件的多源/多角度快讯按同一事件口径合并判定）。
  只有未伴随新事实的口头警告、预期管理、立场重申才适用上方"表态不推"条款
- 只影响中小市值个股自身股价的消息：业绩预告/业绩变动、小额回购、增持/减持、中标/签约、
  日常经营、子公司事项、分红送转等——除非该股是行业龙头或直接改变板块逻辑
- ⚠️龙头大额资本运作必推（2026-08-31 实证漏推：中际旭创拟 40-80 亿元回购 21:34 被拒、
  次日 06:02 换措辞才推，延迟 9 小时）：核心科技龙头（寒武纪、中际旭创、宁德时代、
  英伟达产业链第一梯队等）的回购/增持计划金额 ≥10 亿元，属第 4 条"科技龙头重大消息"
  必推——判定看行业第一梯队属性（如"光模块巨头"），不要因消息是回购/增持类就按
  "中小市值消息"条款降级
- 外围央行（非中国）的日常表态/会议纪要/储备数据（印度央行、匈牙利央行、澳洲联储等的例行表态），
  除非涉及重大政策转向或直接影响 A 股；注意：①此条不含宏观数据发布本身——
  外围重要数据（美国 CPI/PPI/非农/利率决议等）因直接影响全球市场与 A 股，仍按优先级 1 推送；
  ②日本央行加息/降息/YCC 调整及日元套息交易动向不属于"日常表态"——日元是全球核心融资货币，
  日本货币政策转向会通过套息平仓直接冲击 A 股与全球风险资产，按优先级 1 必推（2026-08-13 实证漏推）
- 分析师评级调整/目标价小幅变动（杰富瑞、伯恩斯坦等），除非幅度极大且已引发市场剧烈反应
- 券商研报/机构观点/主题性分析：无具体事件佐证的定性判断——"机构称""研报""XX投资机会"
  "XX向XX传导""XX进入XX期""XX有望受益""XX空间广阔"等，即使涉及科技/AI/算力板块也不推；
  必须是已发生的具体事件（硬数据如"台积电CoWoS良率升至98%"、明确订单金额、政策动作、公司公告）
  才可能构成重大（2026-08-13 实证滥推："机构称物理AI市场空间""液冷投资向零部件传导"等）
- 无官方确认的产品传闻/路线图预测（苹果折叠 iPhone、郭明錤预测等）
- 与上述优先级均无关的其他资讯

对每条输入严格输出一个 JSON 数组元素，字段：
{"idx": 输入的idx原样回显, "title": "原标题", "push": true/false, "score": 0到10的整数, "direction": "bullish|mildly_bullish|neutral|mixed|mildly_bearish|bearish",
 "scope": "market|sector|stock", "sectors": ["板块名"], "entities": ["事件主体公司/机构规范简称，1-3个，无则空数组"], "is_leader_stock": true/false,
 "env_note": "与当前量化环境的关系标注", "reason": "一句话理由"}
direction 必须区分强度：只有影响显著且方向明确才用 bullish/bearish（强档）；
小幅波动用 mildly_bullish/mildly_bearish；方向不明用 neutral/mixed。
is_leader_stock: 仅当该资讯主体是行业龙头个股（市值/地位第一梯队）时为 true，否则 false。
entities: 事件的当事公司/机构/人物规范简称（如"恩智浦""安霸""美联储"），用于跨源同事件去重，必须与标题所述主体一致。
env_note: 仅当用户消息开头给出【当前量化环境】时输出——资讯方向与量化环境同向标"共振: ..."（一句话）；
反向标"背离: ..."并提示谨慎（如"背离: 利好消息但风险收缩期，量化资金在降仓，谨慎对待"）；
环境缺失或该资讯与量化资金环境无关时输出空字符串。env_note 只描述事件与量化环境的关系，
不得包含任何买卖建议。
related_recent: 输入行可能带 related_recent 字段（近48h同主体的已推事件及方向）——这是叙事链上下文：
同主体出现反向事件（叙事反转，如连续利好后突发利空）属信号增强，可在事实清楚时上调 score；
同向事件为叙事延续，本条 direction 仍按本条自身事实独立判定，不要因为先前事件方向而改变本条方向。
注意: 输出必须是合法 JSON，字符串内的双引号必须转义为 \"（或改用「」）；idx 必须原样回显。
不要输出任何 JSON 以外的文字。"""


def _build_llm_user_prompt(items: list, env_context: str = "", pushed_events: list = None) -> str:
    """构造用户 prompt（P2-2：env_context 非空时头部注入量化环境，供共振/背离标注）

    P6-3（2026-08-19）：条目存在近 48h 同主体已推事件时注入 related_recent 字段
    （叙事链上下文，多数条目无匹配不注入，prompt 增量可控）。
    """
    lines = []
    for n in items:
        row = {
            "idx": n.get("_judge_idx"),
            "title": str(n.get("title", ""))[:80],
            "content": str(n.get("content", "") or "")[:200],
            "published_at": str(n.get("published_at", "")),
        }
        related = _related_recent_note(n, pushed_events or [])
        if related:
            row["related_recent"] = related
        lines.append(json.dumps(row, ensure_ascii=False))
    header = f"【当前量化环境】{env_context}\n\n" if env_context else ""
    return header + "请逐条审核以下资讯（不要遗漏任何一条，idx 原样回显）:\n[\n" + ",\n".join(lines) + "\n]"


def _rescue_judge_object(obj_text: str) -> dict | None:
    """从损坏的 JSON 对象文本中抢救判定字段（应对中文引号未转义等）

    例: {"title": "美联储巴尔金：利率是否已足够高是个"悬而未决"的问题", "push": false, ...}
    json.loads 与 _repair_json 均失败时，用正则直接抽取关键字段，
    避免整条判定被丢弃（漏推根因之一，2026-08-01 日志实证）。
    """
    m_push = re.search(r'"push"\s*:\s*(true|false)', obj_text, re.I)
    m_score = re.search(r'"score"\s*:\s*(\d+(?:\.\d+)?)', obj_text)
    if not m_push or not m_score:
        return None

    def _str_field(name: str, default: str = "") -> str:
        m = re.search(rf'"{name}"\s*:\s*"([^"]*)"', obj_text)
        return m.group(1) if m else default

    out = {
        "push": m_push.group(1).lower() == "true",
        "score": float(m_score.group(1)),
        "direction": _str_field("direction", "neutral"),
        "scope": _str_field("scope", "stock"),
        "reason": _str_field("reason", ""),
    }
    m_idx = re.search(r'"idx"\s*:\s*(\d+)', obj_text)
    if m_idx:
        out["idx"] = int(m_idx.group(1))
    # title 允许内含未转义引号：非贪婪匹配到第一个", "为止
    m_title = re.search(r'"title"\s*:\s*"(.+?)"\s*,', obj_text)
    if m_title:
        out["title"] = m_title.group(1)
    return out


def _parse_llm_array(content: str) -> list:
    """解析 LLM 返回的 JSON 数组（容错：代码块包裹/损坏对象/截断）

    注意: 不复用 nodes._safe_parse_json —— 它的容错逻辑只为 filtered_news/
    ranking 等 dict 结构设计，裸数组场景会全部 fallback 失败。
    本函数: 提取代码块 → 逐字符扫描平衡括号 → 逐对象解析
    （先 _repair_json 兜底，再 _rescue_judge_object 正则抢救）。
    """
    if not content or not content.strip():
        return []
    cleaned = content
    # 提取 ```json ... ``` 代码块
    cb_match = re.search(r'```(?:json)?\s*\n?(.*?)```', cleaned, re.DOTALL)
    if cb_match:
        cleaned = cb_match.group(1)
    # 定位数组起点（防御 LLM 在数组前输出解释文字）
    start = cleaned.find("[")
    if start < 0:
        return []

    items = []
    i = start
    obj_depth = 0
    obj_start = -1
    in_str = False
    escape = False
    while i < len(cleaned):
        ch = cleaned[i]
        if escape:
            escape = False
            i += 1
            continue
        if ch == "\\":
            escape = True
            i += 1
            continue
        if ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "{":
                if obj_depth == 0:
                    obj_start = i
                obj_depth += 1
            elif ch == "}":
                obj_depth -= 1
                if obj_depth == 0 and obj_start >= 0:
                    obj_text = cleaned[obj_start:i + 1]
                    try:
                        items.append(json.loads(obj_text))
                    except json.JSONDecodeError:
                        try:
                            items.append(json.loads(_repair_json(obj_text)))
                        except json.JSONDecodeError:
                            rescued = _rescue_judge_object(obj_text)
                            if rescued is not None:
                                items.append(rescued)
                            else:
                                logger.warning(f"LLM 返回单对象解析失败，跳过: {obj_text[:60]}")
                    obj_start = -1
        i += 1
    return items


def _hang_judge(news: dict) -> dict:
    """LLM 未判定的挂起状态：不推、不落指纹，留待下一轮重新送 LLM 判定。

    2026-08-03 用户口径：删除规则降级路径，预筛通过的全部候选必须由 LLM
    判定推/不推，Python 规则不得直接通关。judged=False 的条目在 run_once
    中被跳过且不写入 seen 指纹，因此下轮会重新进入 LLM 判定。
    """
    return {
        "title": str(news.get("title", "") or ""),
        "push": False,
        "judged": False,
        "score": 0,
        "direction": "neutral",
        "scope": "stock",
        "sectors": [],
        "entities": [],
        "is_leader_stock": False,
        "env_note": "",
        "reason": "LLM未判定，挂起下轮重试",
    }


def _format_llm_judge_result(news: dict, entry: dict) -> dict:
    """将单条 LLM 回显归一化为推送管线使用的判定结构。"""
    title = str(news.get("title", "") or "").strip()
    return {
        "title": title,
        "push": _as_bool(entry.get("push", False), False),
        "judged": True,
        "score": entry.get("score", 0),
        "direction": _normalize_direction(entry.get("direction", "neutral"), news),
        "scope": str(entry.get("scope", "stock") or "stock").lower(),
        "sectors": _as_list(entry.get("sectors")),
        "entities": [str(x).strip() for x in _as_list(entry.get("entities"))
                     if isinstance(x, (str, int, float)) and str(x).strip()],
        "is_leader_stock": _as_bool(entry.get("is_leader_stock", False), False),
        "env_note": str(entry.get("env_note", "") or "").strip()[:80],
        "reason": str(entry.get("reason", "") or "").strip(),
    }
