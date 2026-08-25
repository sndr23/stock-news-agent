# -*- coding: utf-8 -*-
"""
量化因子采集器（factor_collector.py）
====================================================
定位：与 real_time_push.py（资讯事件流）并列的第二采集入口——"量化因子流"。
     资讯是"事件流"（去重用事件指纹），因子是"状态/时序"（去重用冷却时间），
     两者出口共用 src/tools/push.py 推送（红涨绿跌 + emoji，微信端体验一致）。

覆盖维度（2026-08-14 起，用户明确先补两维）：
1. 技术面（指数级）：上证指数 / 创业板指的均线(MA5/10/20/60)、动量(5/20日涨跌幅)、
   突破(20日新高/新低)、放量(成交额 vs 5/20日均量)——反映市场整体，非个股。
2. 宏观流动性：股指期货基差(IF/IC/IM/IH 主力连续 vs 对应现货指数)、
   汇率(美元/日元、美元/在岸人民币)——套息交易与中性策略对冲成本。

P1 扩展（2026-08-19）：
3. 自选股监控（P1-2）：watchlist.json 带代码条目 → 涨跌幅±5%/量比2.5/20日突破破位，
   异动推送"个股异动 + 近48h相关已推资讯"合并卡片（D2 完整解法）。
4. 资金流（P1-3）：两市主力净流入（东财 fflow）+ 融资余额日变化（东财 datacenter，
   T-1 披露）；超阈值推送并入因子异动卡片，并同步进快照"市场环境"行。
   注：北向资金净买入 2024-08 起停止实时披露，以两市主力净流入（同为机构/大单口径）替代。

数据源（均为免费公开 HTTP 接口，不依赖通达信 MCP 会话）：
- 腾讯行情  qt.gtimg.cn          → 指数实时（上证/创业板/宽基现货）
- 东方财富  push2his.eastmoney   → 指数日K（算均线/动量/突破/放量）
- 新浪外汇  hq.sinajs.cn/fx_     → 汇率（美元/日元、美元/人民币）
- 新浪期货  hq.sinajs.cn/nf_     → 股指期货主力连续（算基差）

用法：
  python scripts/factor_collector.py --dry-run   # 只采集+计算+打印快照，不推送
  python scripts/factor_collector.py --push      # 打印快照；有异动且过冷却则推微信
  python scripts/factor_collector.py --loop      # 常驻：交易时段每 RT_POLL_SECONDS(默认300s)
                                                 # 高频轮询，非交易时段每 RT_POLL_IDLE_SECONDS(默认1800s)
                                                 # 低频轮询（实时因子盘中分分钟变化，30分钟一轮会滞后）
"""
import argparse
import json
import logging
import math
import os
import re
import sys
import time
from datetime import datetime, date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import requests
from dotenv import load_dotenv

load_dotenv(PROJECT_ROOT / ".ENV")

from src.tools.push import push_via_wecom, push_via_pushplus  # 推送（含重试，复用现有出口）

logger = logging.getLogger("factor_collector")

# ============================================================
# 配置
# ============================================================
# 技术面指数（腾讯代码 → 新浪 symbol）。上证/创业板为主（用户长期偏好），宽基用于基差。
# 注：K 线用新浪 getKLineData（稳定无反爬）；实时行情用腾讯（含成交额）。
INDEXES = {
    "上证指数": {"tencent": "sh000001", "sina": "sh000001"},
    "创业板指": {"tencent": "sz399006", "sina": "sz399006"},
    "沪深300":  {"tencent": "sh000300", "sina": "sh000300"},
    "中证500":  {"tencent": "sh000905", "sina": "sh000905"},
    "中证1000": {"tencent": "sh000852", "sina": "sh000852"},
    "上证50":   {"tencent": "sh000016", "sina": "sh000016"},
}
# 核心监控指数（快照主展示 + 技术面异动检测对象）
CORE_INDEXES = ["上证指数", "创业板指"]

# 股指期货主力连续（新浪代码 → 对应现货指数名）
FUTURES = {
    "IF": {"sina": "nf_IF0", "index": "沪深300"},
    "IC": {"sina": "nf_IC0", "index": "中证500"},
    "IM": {"sina": "nf_IM0", "index": "中证1000"},
    "IH": {"sina": "nf_IH0", "index": "上证50"},
}

# 汇率（新浪代码 → 显示名）
FX = {
    "fx_susdjpy": "美元/日元",
    "fx_susdcny": "美元/在岸人民币",
}

# 异动阈值（第一版保守值，后续可调）
TH_FX_JPY_PCT = 1.5       # 美元/日元单日涨跌幅绝对值 > 1.5% → 套息平仓风险
TH_VOLUME_RATIO = 1.5     # 成交量 / 5日均量 > 1.5 → 放量
TH_BREAK_WINDOW = 20      # 突破窗口：20日新高/新低
TH_BASIS_HISTORY = 20     # 贴水"走扩"历史窗口：当前基差率创近20日最深才告警（相对分位，防常态贴水误报）
TH_COOLDOWN_HOURS = 6     # 同一因子告警冷却时长（小时）
# 个股监控阈值（P1-2 2026-08-19：watchlist.json 带代码条目）
TH_STOCK_CHG_PCT = 5.0     # 个股单日涨跌幅绝对值 ≥5% → 异动
TH_STOCK_VOL_RATIO = 2.5   # 个股量比（今日量/5日均量）≥2.5 → 异动
# 资金流阈值（P1-3 2026-08-19）
TH_MAIN_NETFLOW_YI = 300   # 两市主力净流入 |x| ≥300亿 → 异动
TH_MARGIN_CHG_YI = 80      # 融资余额单日变化 |x| ≥80亿 → 异动

# 复审补齐因子（P3 2026-08-19）：对标机构级因子体系的四大缺口
# P3-1 隔夜外盘：自选股全为 AI 硬件链，beta 直接挂钩纳指/英伟达/恒生科技
# （2026-08-19 实证：隔夜英伟达 -2.34% → 中际旭创 -9.36%，先行指标价值确认）
GLOBAL_QUOTES = {
    "纳斯达克100": "usNDX",
    "标普500": "usINX",
    "英伟达": "usNVDA",
    "恒生科技指数": "hkHSTECH",
}
TH_GLOBAL_PCT = 2.0        # 隔夜外盘 |涨跌| ≥2% → 异动告警（≥3% 升级 warning）
# P12（2026-08-21）：腾讯无韩指代码 → 东财 ulist 补充源。
# 韩国KOSPI（三星/SK海力士存储链）与自选股 AI 硬件链同频；北京时间 14:30 收盘
# 早于 A 股，盘中读数为实时先行信号（f2/f3 为 ×100 整数，新浪 int_kospi 已失效）。
GLOBAL_QUOTES_EM = {
    "韩国KOSPI": "100.KS11",
}
# P3-2 市场宽度：判断普涨普跌 vs 结构市（指数跌而普跌=真实风险，大权重拉指数=假强势）
TH_LIMIT_DOWN = 100        # 跌停家数 ≥100 → 情绪冰点告警
TH_BREADTH_DOWN_PCT = 80   # 下跌家数占比 ≥80% → 极端普跌告警
# P3-3 波动率状态：已实现波动率历史分位（机构 vol targeting 核心；高波期人工同样应降仓）
TH_VOL_PCTILE_HIGH = 80    # 20日已实现波动率 ≥ 近一年80分位 → 高波
TH_VOL_PCTILE_LOW = 20     # ≤20分位 → 低波
VOL_KLINE_DAYS = 260       # 波动率分位窗口（约一年交易日）
# P3-4 风格轮动：上证50/中证1000 比价变化（自选股偏科技成长，比价升=风格逆风）
TH_STYLE_CHG20 = 1.0       # 比价20日变化 |x| ≥1% → 判风格切换
# P4-2 涨停情绪温度计（2026-08-19）：短线情绪周期锚（冰点→低迷→正常→亢奋），
# 数据源东财涨停池/炸板池（与 P3-2 宽度同源，桶口径已交叉验证）
TH_ZT_EUPHORIA = 80        # 涨停家数 ≥80 且炸板率 <25% → 情绪亢奋
TH_ZT_FREEZE = 30          # 涨停家数 ≤30 → 情绪冰点
TH_ZB_FREEZE = 45          # 炸板率 ≥45% → 情绪冰点（连板晋级失败率高）
TH_ZB_WARN = 50            # 炸板率 ≥50% → 告警（warning，并入 risk_off 口径）
TH_MAX_LBC_WARN = 6        # 最高连板 ≥6 → 告警（投机过热，警惕监管/退潮）
# P7-1（2026-08-19）资金面：交易所质押式回购利率（GC007，机构资金面温度计；
# 数据源腾讯行情 sh204007，与指数行情同源）。正常区间 1.5~2.5%，税期/跨月/跨季尖峰。
TH_GC007_TIGHT = 3.0       # GC007 ≥3% → 资金面收紧（影子维度利空）
TH_GC007_ALERT = 3.5       # GC007 ≥3.5% → 告警（warning，并入 risk_off：市场级风险）
TH_GC007_SPIKE_PCT = 30    # GC007 日涨幅 ≥30% 且 ≥2% → 影子维度利空（利率急升）
TH_GC007_SPIKE_ALERT = 50  # GC007 日涨幅 ≥50% 且 ≥2% → 告警（warning，日内急升）
# P7-2 期权情绪：全市场期权成交量 PCR（认沽/认购，机构恐慌/贪婪温度计；
# 数据源东财期权列表 fs=m:10，与行业资金流同源）。正常区间 0.6~1.1。
TH_PCR_PANIC = 1.3         # PCR ≥1.3 → 恐慌对冲占优（影子维度利空）
TH_PCR_GREED = 0.55        # PCR ≤0.55 → 看涨情绪占优（影子维度利好）
TH_PCR_ALERT = 1.5         # PCR ≥1.5 → 告警（info，情绪极端不切 risk_off）
# P12 影子维度（2026-08-21）：韩指 KOSPI（存储链先行，14:30 BJT 收盘早于 A 股）。
# 阈值与环境行展示同口径（|涨跌|≥2% 才有意义），常态 0 分不稀释 IC 样本外的信号
TH_KOSPI_PCT = 2.0         # |韩KOSPI 涨跌| ≥2% → 影子维度 ±1（先行偏多/偏空）

# 量化资金状态变化推送（2026-08-14 用户需求："追踪万亿级量化资金走势"）：
# 盘中每轮检测"量化资金状态"（基差对冲方向 + 风险状态），发生变化才推送"量化资金动态"
# （如 IC 贴水由收敛转走扩、risk_off 切换），平时静默。冷却防盘中抖动反复推。
STATE_CHANGE_COOLDOWN_HOURS = 4  # 状态变化推送冷却（小时）
# 基差方向判断：贴水率"持续加深/变浅"的期数（>=3 才判方向，防单期抖动）
BASIS_DIR_LOOKBACK = 3

# P5-1（2026-08-19）：非线性门控——状态调节权重（显式规则，保持强归因）
# 高波/极端共振环境下利空维度话语权放大：机构降杠杆 + 套息平仓连锁，
# 1+1>2 的共振不能用线性加总表达
TH_GATE_MULT = 1.5          # 门控升权倍数
# P5-2（2026-08-19）：确信度分层——|score|≥0.67（等权下 4/6 维同向）才单独推
# "量化方向信号"；0.5~0.67 的弱翻转只进盘前/盘后简报（低频率、高确信度的字面实现）
STRONG_DIR_THRESHOLD = 0.67
# P5-3（2026-08-19）：数据健康度——源成功率 <70% 时信号附警示（机构级清洗 =
# 不只拿数据，还要知道自己在用什么、缺了什么）
HEALTH_WARN_RATIO = 0.7

STATE_PATH = PROJECT_ROOT / "logs" / "factor_state.json"
# real_time_push 的状态文件（P0 联动增强：方向信号附最近已推资讯，跨管线引用）
REALTIME_STATE_FILENAME = "real_time_state.json"
_REALTIME_STATE_PATH = PROJECT_ROOT / "logs" / "real_time_state.json"
# 自选股名单（P1-2：带代码条目做行情监控；纯名称条目只参与资讯匹配）
WATCHLIST_PATH = PROJECT_ROOT / "watchlist.json"

# ============================================================
# 数据源层（HTTP 适配，均带超时与异常隔离）
# ============================================================
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
}

# P9（2026-08-20）UA 轮换池：借鉴 stock-sdk（github.com/chengzuopeng/stock-sdk）
# userAgentPool 的治理思路——固定 UA 的脚本化请求易被数据源识别为爬虫
# （2026-08-14 东财 push2his 反爬 RemoteDisconnected 实证的根因之一），
# 轮换浏览器 UA 降低单来源识别风险。仅 Node 端有效同理，此处仅 Python 端使用。
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
]
_ua_index = [0]


def _get_ua() -> str:
    """轮换取 UA（round-robin；单进程内轮换，跨轮次随机性由请求频率稀释）"""
    ua = _UA_POOL[_ua_index[0] % len(_UA_POOL)]
    _ua_index[0] += 1
    return ua


def _http_get(url: str, params: dict = None, headers: dict = None, encoding: str = None,
              rotate_ua: bool = True) -> str:
    """GET 并返回文本；异常统一返回空串（上层判空）

    rotate_ua（P9）：默认 True，每请求轮换浏览器 UA（防固定 UA 被数据源
    识别为脚本）；传 False 保留调用方指定 UA（如新浪系需 Referer 时）。
    """
    try:
        hdrs = dict(headers or _HEADERS)
        if rotate_ua:
            hdrs["User-Agent"] = _get_ua()
        r = requests.get(url, params=params, headers=hdrs, timeout=10)
        if encoding:
            r.encoding = encoding
        return r.text or ""
    except Exception as e:
        logger.warning(f"请求失败 {url}: {e}")
        return ""


def fetch_index_quotes() -> dict:
    """腾讯指数实时行情 → {指数名: {price, prev_close, change_pct, amount_wan}}"""
    result = {}
    codes = ",".join(ix["tencent"] for ix in INDEXES.values())
    text = _http_get(f"http://qt.gtimg.cn/q={codes}", encoding="gbk")
    if not text:
        return result
    for line in text.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        var, _, payload = line.partition("=")
        payload = payload.strip().strip('"')
        parts = payload.split("~")
        if len(parts) < 38:
            continue
        name = parts[1]
        try:
            result[name] = {
                "price": float(parts[3]),
                "prev_close": float(parts[4]),
                "change_pct": float(parts[32]),
                "amount_wan": float(parts[37]),  # 成交额（万元）
            }
        except (ValueError, IndexError):
            continue
    return result


def _fetch_kline_sina(symbol: str, lmt: int) -> list:
    """新浪指数日K → [{date, open, close, high, low, volume}]，volume 单位=股"""
    params = {"symbol": symbol, "scale": "240", "ma": "no", "datalen": str(lmt)}
    text = _http_get(
        "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
        params=params,
        headers={"Referer": "http://finance.sina.com.cn", **_HEADERS},
    )
    if not text:
        return []
    try:
        data = json.loads(text)
    except ValueError:
        return []
    if not isinstance(data, list):
        return []
    out = []
    for item in data:
        try:
            out.append({
                "date": item["day"],
                "open": float(item["open"]),
                "close": float(item["close"]),
                "high": float(item["high"]),
                "low": float(item["low"]),
                "volume": float(item["volume"]),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return out


def _fetch_kline_tencent(symbol: str, lmt: int) -> list:
    """腾讯指数日K（备选，新浪失败时降级）→ 统一 {date, open, close, high, low, volume}

    腾讯 K 线 volume 单位=手（=股/100），此处 ×100 转成"股"与新浪口径一致。
    字段顺序：[日期, 开盘, 收盘, 最高, 最低, 成交量(手)]。
    """
    text = _http_get(
        "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        params={"param": f"{symbol},day,,,{lmt},qfq"},
    )
    if not text:
        return []
    try:
        data = json.loads(text)
        node = (data.get("data") or {}).get(symbol) or {}
        day = node.get("day") or node.get("qfqday") or []
    except (ValueError, TypeError):
        return []
    out = []
    for item in day:
        if len(item) < 6:
            continue
        try:
            out.append({
                "date": item[0],
                "open": float(item[1]),
                "close": float(item[2]),
                "high": float(item[3]),
                "low": float(item[4]),
                "volume": float(item[5]) * 100,  # 手 → 股
            })
        except (ValueError, TypeError, IndexError):
            continue
    return out


def _em_secid(symbol: str) -> str:
    """sina/腾讯格式 symbol → 东财 secid（P9）

    规则（与 stock-sdk toEastmoneySecid 的 CN 映射一致）：沪市前缀 1.、深市 0.。
    入参如 sh000001 / sz399006 / sh600183 / sz300308；无法识别返回 ""。
    """
    s = str(symbol or "").strip().lower()
    if s.startswith("sh"):
        return f"1.{s[2:]}"
    if s.startswith("sz"):
        return f"0.{s[2:]}"
    return ""


# 东财 push2his 多主机池（P9）：主域 + 数字前缀 CDN 域。
# 单个主机被限流/熔断时轮换下一台（stock-sdk fallback 机制同思路）。
_EM_KLINE_HOSTS = [
    "https://push2his.eastmoney.com",
    "https://7.push2his.eastmoney.com",
    "https://33.push2his.eastmoney.com",
    "https://63.push2his.eastmoney.com",
    "https://91.push2his.eastmoney.com",
]
# 东财公开接口的 ut 参数（各公开项目通用，非私有凭据）
_EM_KLINE_UT = "7eea3edcaed734bea9cbfc24409ed989"


def _fetch_kline_em(symbol: str, lmt: int, adjust: int = 0) -> list:
    """东财指数/个股日K（P9 新增第三冗余源）→ 统一 {date, open, close, high, low, volume}

    2026-08-14 实测裸请求被反爬（RemoteDisconnected），本函数按 stock-sdk 治理
    思路：UA 轮换 + 多主机 fallback + 重试。fields2 取 f51-f56 即可（量/额）。
    volume 单位=手，×100 转"股"与新浪口径一致（同腾讯降级函数）。
    fqt: 0=不复权 1=前复权 2=后复权（因子计算用不复权，与新浪口径一致；
    回测如需复权数据可传 fqt=1/2）。

    注意：东财 kline/get 的 lmt 参数不生效（beg/end 全量模式下忽略），
    实测 beg=0&end=20500000 返回全量（上证 8708 根 ≈ 1MB+，云端多指数会浪费
    流量与 IO）。故 beg 用「今日往前推 3×lmt 天」近似窗口（日K约 1.4 交易日/自然日，
    3 倍留足节假日/停牌冗余），请求后本地截取尾部 lmt 根保证准确。

    Returns:
        [{date, open, close, high, low, volume}] 升序；全部失败返回 []
    """
    secid = _em_secid(symbol)
    if not secid:
        logger.warning(f"东财K线: 无法识别 symbol={symbol}")
        return []
    today = datetime.now().strftime("%Y%m%d")
    beg_d = (datetime.now() - timedelta(days=max(lmt * 3, 60))).strftime("%Y%m%d")
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56",
        "ut": _EM_KLINE_UT,
        "klt": "101",           # 日K
        "fqt": str(adjust),     # 复权
        "secid": secid,
        "beg": beg_d,
        "end": today,
        "lmt": str(lmt),        # 上游忽略，本地截取兜底
    }
    last_err = ""
    for host in _EM_KLINE_HOSTS:
        text = _http_get(f"{host}/api/qt/stock/kline/get", params=params)
        if not text:
            last_err = "empty"
            logger.debug(f"东财K线 {symbol} 主机 {host} 失败，换下一台")
            continue
        try:
            data = json.loads(text)
            klines = (data.get("data") or {}).get("klines") or []
        except (ValueError, AttributeError):
            last_err = "parse"
            logger.debug(f"东财K线 {symbol} 主机 {host} 解析失败，换下一台")
            continue
        if not klines:
            last_err = "empty-k"
            continue
        out = []
        for line in klines:
            parts = str(line).split(",")
            if len(parts) < 6:
                continue
            try:
                out.append({
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]) * 100,  # 手 → 股
                })
            except (ValueError, TypeError):
                continue
        if out:
            return out[-lmt:]  # 本地截取尾部 N 根（上游忽略 lmt）
        last_err = "no-rows"
    logger.warning(f"东财K线全部主机失败 {symbol} (lmt={lmt}, err={last_err})")
    return []


def fetch_index_kline(symbol: str, lmt: int = 65) -> list:
    """指数日K → [{date, open, close, high, low, volume}]，升序（volume 单位=股）

    三冗余降级链（P9 2026-08-20）：新浪优先 → 腾讯 → 东财（新增）。
    2026-08-14 东财裸请求被反爬，现按 stock-sdk 治理思路（UA 轮换 + 多主机
    fallback）解锁作为第三冗余，消除新浪/腾讯双点依赖。
    """
    out = _fetch_kline_sina(symbol, lmt)
    if not out:
        logger.warning(f"新浪K线失败，降级腾讯: {symbol}")
        out = _fetch_kline_tencent(symbol, lmt)
    if not out:
        logger.warning(f"腾讯K线失败，降级东财: {symbol}")
        out = _fetch_kline_em(symbol, lmt)
    return out


def fetch_fx() -> dict:
    """新浪外汇 → {符号: {name, price, change_pct}}"""
    result = {}
    codes = ",".join(FX.keys())
    text = _http_get("http://hq.sinajs.cn/list=" + codes, headers={"Referer": "http://finance.sina.com.cn", **_HEADERS}, encoding="gbk")
    if not text:
        return result
    for line in text.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        var, _, payload = line.partition("=")
        payload = payload.strip().strip('"')
        parts = payload.split(",")
        if len(parts) < 12:
            continue
        sym = var.replace("var hq_str_", "").strip()
        try:
            result[sym] = {
                "name": parts[9],
                "price": float(parts[1]),
                "change_pct": float(parts[11]),
            }
        except (ValueError, IndexError):
            continue
    return result


def fetch_index_futures() -> dict:
    """新浪股指期货主力连续 → {期货代码: {price, prev_settle}}"""
    result = {}
    codes = ",".join(f["sina"] for f in FUTURES.values())
    text = _http_get("http://hq.sinajs.cn/list=" + codes, headers={"Referer": "http://finance.sina.com.cn", **_HEADERS}, encoding="gbk")
    if not text:
        return result
    for line in text.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        var, _, payload = line.partition("=")
        payload = payload.strip().strip('"')
        parts = payload.split(",")
        if len(parts) < 5:
            continue
        sym = var.replace("var hq_str_", "").strip()
        for code, conf in FUTURES.items():
            if conf["sina"] == sym:
                try:
                    result[code] = {
                        "price": float(parts[3]),       # 最新价
                        "prev_settle": float(parts[0]),  # 昨结算
                    }
                except (ValueError, IndexError):
                    continue
    return result


# ============================================================
# P1-2/P1-3 数据源：自选股行情 + 资金流
# ============================================================
def _stock_symbol(code: str) -> str:
    """6位代码 → 带交易所前缀 symbol（6/9开头→sh，0/3开头→sz，其余→bj）"""
    c = str(code).strip().lower()
    if c[:2] in ("sh", "sz", "bj"):
        return c
    if c.startswith(("6", "9")):
        return "sh" + c
    if c.startswith(("0", "3")):
        return "sz" + c
    return "bj" + c


def load_watchlist_stocks() -> list:
    """watchlist.json stocks → [{"name","code","symbol"}]（P1-2）

    只取带代码的 dict 条目（纯名称字符串条目无法查行情，仅资讯管线匹配用）；
    文件缺失/损坏 → []（个股监控静默关闭，不影响指数/基差/汇率主流程）。
    """
    try:
        wl = json.loads(WATCHLIST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out = []
    for s in wl.get("stocks", []) or []:
        if not isinstance(s, dict):
            continue
        code = str(s.get("code", "") or "").strip()
        name = str(s.get("name", "") or "").strip()
        if code and name:
            out.append({"name": name, "code": code, "symbol": _stock_symbol(code)})
    return out


def fetch_stock_quotes(symbols: list) -> dict:
    """腾讯个股实时行情（与指数同一接口）→ {symbol: {name, price, prev_close, change_pct}}"""
    result = {}
    if not symbols:
        return result
    codes = ",".join(symbols)
    text = _http_get(f"http://qt.gtimg.cn/q={codes}", encoding="gbk")
    if not text:
        return result
    for line in text.split(";"):
        line = line.strip()
        if "=" not in line:
            continue
        var, _, payload = line.partition("=")
        payload = payload.strip().strip('"')
        parts = payload.split("~")
        if len(parts) < 38:
            continue
        # 腾讯 var 名即请求 symbol（如 v_sz300308），比 parts[2]（不带前缀）可靠
        sym = var.replace("v_", "").replace("hq_str_", "").strip()
        try:
            result[sym] = {
                "name": parts[1],
                "price": float(parts[3]),
                "prev_close": float(parts[4]),
                "change_pct": float(parts[32]),
            }
        except (ValueError, IndexError):
            continue
    return result


def fetch_market_flows() -> dict:
    """东财资金流（P1-3）：两市主力净流入（当日累计）+ 融资余额及日变化（T-1）

    返回 {"main_net_yi": float, "margin_yi": float, "margin_chg_yi": float}；
    任一数据源失败时对应字段缺省（互不影响，资金流是增强维度不 fail-stop）。
    """
    out = {}
    # 1) 两市主力净流入（沪深合并日级 fflow kline；行格式 "日期,主力净流入,小单,中单,大单,超大单"）
    text = _http_get("https://push2.eastmoney.com/api/qt/stock/fflow/kline/get", params={
        "lmt": "0", "klt": "101",
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56",
        "secid": "1.000001", "secid2": "0.399001",
    })
    if text:
        try:
            kl = (json.loads(text).get("data") or {}).get("klines") or []
            if kl:
                main_net = float(str(kl[-1]).split(",")[1])
                out["main_net_yi"] = round(main_net / 1e8, 1)
        except (ValueError, IndexError, TypeError):
            logger.warning("主力资金流解析失败（跳过该维度）")
    # 2) 融资余额（RPTA_RZRQ_LSHJ 按日汇总，RZYE=融资余额元，T-1 披露）
    text = _http_get("https://datacenter-web.eastmoney.com/api/data/v1/get", params={
        "reportName": "RPTA_RZRQ_LSHJ",
        "columns": "ALL",
        "sortColumns": "dim_date", "sortTypes": "-1",
        "pageSize": "2", "pageNumber": "1", "source": "WEB", "client": "WEB",
    })
    if text:
        try:
            rows = ((json.loads(text).get("result") or {}).get("data")) or []
            if rows:
                out["margin_yi"] = round(float(rows[0].get("RZYE", 0) or 0) / 1e8, 1)
            if len(rows) >= 2:
                out["margin_chg_yi"] = round(
                    (float(rows[0].get("RZYE", 0) or 0) - float(rows[1].get("RZYE", 0) or 0)) / 1e8, 1)
        except (ValueError, TypeError):
            logger.warning("融资余额解析失败（跳过该维度）")
    return out


def fetch_global_quotes() -> dict:
    """隔夜外盘（P3-1）：腾讯美股/港股指数 + 东财补充（P12 韩指）→ {名称: {price, change_pct}}

    与 A 股行情同一接口（qt.gtimg.cn），字段位相同（名称[1]/现价[3]/涨跌%[32]）。
    A 股交易时段读到的即隔夜收盘值（美股 4:00 收、恒生 16:00 收），
    盘前 9:15 首轮即可推送隔夜预警。
    """
    out = {}
    codes = ",".join(GLOBAL_QUOTES.values())
    text = _http_get(f"http://qt.gtimg.cn/q={codes}", encoding="gbk")
    if text:
        rev = {v.lower(): k for k, v in GLOBAL_QUOTES.items()}
        for line in text.split(";"):
            line = line.strip()
            if "=" not in line:
                continue
            var, _, payload = line.partition("=")
            payload = payload.strip().strip('"')
            parts = payload.split("~")
            name = rev.get(var.replace("v_", "").lower())
            if not name or len(parts) < 33:
                continue
            try:
                out[name] = {"price": float(parts[3]), "change_pct": float(parts[32])}
            except (ValueError, IndexError):
                continue
    # P12：东财补充源（腾讯缺失的全球指数，如韩指）；主源空/挂同样兜底，失败不影响主源
    out.update(_fetch_global_quotes_em())
    return out


def _fetch_global_quotes_em() -> dict:
    """东财全球指数补充（P12）→ {名称: {price, change_pct}}

    f2(价)/f3(涨跌%) 为 ×100 整数（实证 SPX 764116 ↔ 腾讯 7641.16 一致）；
    停牌/缺字段时东财返回 "-" 字符串，isinstance 过滤跳过。
    """
    if not GLOBAL_QUOTES_EM:
        return {}
    text = _http_get("https://push2.eastmoney.com/api/qt/ulist.np/get", params={
        "secids": ",".join(GLOBAL_QUOTES_EM.values()),
        "fields": "f2,f3,f12,f14", "ut": _EM_KLINE_UT})
    if not text:
        return {}
    try:
        diff = ((json.loads(text).get("data") or {}).get("diff")) or []
    except ValueError:
        return {}
    # f12 为裸代码（无 "100." 市场前缀），取 secid 后段反查名称
    rev = {v.split(".")[-1]: k for k, v in GLOBAL_QUOTES_EM.items()}
    out = {}
    for d in diff:
        name = rev.get(str(d.get("f12") or ""))
        f2, f3 = d.get("f2"), d.get("f3")
        if not name or not isinstance(f2, (int, float)) or not isinstance(f3, (int, float)):
            continue
        out[name] = {"price": f2 / 100, "change_pct": f3 / 100}
    return out


def fetch_market_breadth() -> dict:
    """市场宽度（P3-2）：东财涨跌分布单接口 → 涨跌家数/涨跌停/大跌家数

    桶语义（2026-08-19 与东财涨跌停池计数交叉验证，两侧均=118/36）：
    key=11 → 涨幅≥10%（≈涨停，含20cm）；key=-11 → 跌幅≤-10%（≈跌停）；
    其余 key=k → [k, k+1) 区间（floor 口径）。
    失败返回 {}（增强维度不 fail-stop）。
    """
    text = _http_get("https://push2ex.eastmoney.com/getTopicZDFenBu", params={
        "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt"})
    if not text:
        return {}
    try:
        fenbu = ((json.loads(text).get("data") or {}).get("fenbu")) or []
    except ValueError:
        return {}
    adv = dec = flat = limit_up = limit_down = big_down = 0
    for item in fenbu:
        if not isinstance(item, dict):
            continue
        for k, v in item.items():
            try:
                bucket, cnt = int(k), int(v)
            except (TypeError, ValueError):
                continue
            if bucket > 0:
                adv += cnt
                if bucket >= 11:
                    limit_up += cnt
            elif bucket < 0:
                dec += cnt
                if bucket <= -11:
                    limit_down += cnt
                if bucket <= -6:
                    big_down += cnt
            else:
                flat += cnt
    total = adv + dec + flat
    if not total:
        return {}
    return {
                "adv": adv, "dec": dec, "flat": flat,
                "down_pct": round(dec / total * 100, 1),
                "limit_up": limit_up, "limit_down": limit_down,
                "big_down": big_down,
            }


def _topic_pool(kind: str, d: str) -> dict:
    """东财涨停池(ZT)/炸板池(ZB)按日取数 → data dict；失败返回 {}

    必须带 date 参数且 sort=fbt:asc（实测缺任一均返回 data:null）。
    """
    text = _http_get(f"https://push2ex.eastmoney.com/getTopic{kind}Pool", params={
        "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
        "Pageindex": "0", "pagesize": "320", "sort": "fbt:asc", "date": d,
    })
    try:
        data = json.loads(text).get("data")
        return data if isinstance(data, dict) else {}
    except (ValueError, AttributeError):
        return {}


def fetch_zt_sentiment() -> dict:
    """涨停情绪温度计（P4-2）：涨停数/最高连板/炸板率 → 情绪档位

    取数口径：优先当日实时；当日无池（盘前/非交易日）回退最近交易日（最多3天），
    涨停池与炸板池严格同日（炸板率分子分母口径一致）。失败返回 {}。
    情绪档位：冰点（涨停≤30 或 炸板率≥45%）/ 亢奋（涨停≥80 且 炸板率<25%）/
    低迷（涨停<50）/ 正常。
    """
    for i in range(4):
        d = (date.today() - timedelta(days=i)).strftime("%Y%m%d")
        zt_data = _topic_pool("ZT", d)
        zt = zt_data.get("tc")
        if not isinstance(zt, int) or zt <= 0:
            continue
        zb = _topic_pool("ZB", d).get("tc")
        zb = zb if isinstance(zb, int) else 0
        lbc_counts = {}
        for item in (zt_data.get("pool") or []):
            if not isinstance(item, dict):
                continue
            lbc = item.get("lbc")
            if isinstance(lbc, int) and lbc > 0:
                lbc_counts[lbc] = lbc_counts.get(lbc, 0) + 1
        max_lbc = max(lbc_counts) if lbc_counts else 0
        total = zt + zb
        zbr = round(zb / total * 100, 1) if total else 0.0
        if zt <= TH_ZT_FREEZE or zbr >= TH_ZB_FREEZE:
            mood = "冰点"
        elif zt >= TH_ZT_EUPHORIA and zbr < 25:
            mood = "亢奋"
        elif zt < 50:
            mood = "低迷"
        else:
            mood = "正常"
        dist = "/".join(f"{k}板{v}" for k, v in sorted(lbc_counts.items()))
        return {"zt": zt, "zb": zb, "zbr": zbr, "max_lbc": max_lbc,
                "lbc_dist": dist, "mood": mood}
    return {}


def fetch_sector_flows(top_n: int = 3) -> dict:
    """行业板块主力净流入/流出 TOP（P4-3）：回答"钱在往哪跑"

    东财行业资金流（fs=m:90 t:2 行业板块，f62=主力净流入，单位元）。
    返回 {"inflow": [(行业, 亿)], "outflow": [(行业, 亿)]}；失败返回 {}。
    """
    text = _http_get("https://push2.eastmoney.com/api/qt/clist/get", params={
        "fid": "f62", "po": "1", "pz": "100", "pn": "1", "np": "1",
        "fltt": "2", "invt": "2", "fs": "m:90 t:2", "fields": "f12,f14,f62",
    })
    if not text:
        return {}
    try:
        rows = (json.loads(text).get("data") or {}).get("diff") or []
    except ValueError:
        return {}
    vals = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            vals.append((str(r.get("f14", "") or ""), float(r.get("f62")) / 1e8))
        except (TypeError, ValueError):
            continue
    if not vals:
        return {}
    inflow = [(n, round(v, 1)) for n, v in
              sorted(vals, key=lambda x: x[1], reverse=True) if v > 0][:top_n]
    outflow = [(n, round(v, 1)) for n, v in
               sorted(vals, key=lambda x: x[1]) if v < 0][:top_n]
    return {"inflow": inflow, "outflow": outflow}


def fetch_liquidity() -> dict:
    """资金面利率（P7-1 2026-08-19）：交易所质押式回购 GC007（7天）/ GC001（隔夜）

    GC007 是机构资金面温度计（DR007 的交易所口径代理）：税期/跨月/跨季资金收紧时
    尖峰先行，杠杆资金成本抬升 → 风险资产承压。数据源腾讯行情（与指数同源稳定），
    price 为年化利率（%）。返回 {"gc007": {...}, "gc001": {...}}；失败返回 {}。
    """
    text = _http_get("http://qt.gtimg.cn/q=sh204007,sh204001", encoding="gbk")
    if not text:
        return {}
    out = {}
    for code, key in (("sh204007", "gc007"), ("sh204001", "gc001")):
        m = re.search(rf'v_{code}="([^"]+)"', text)
        if not m:
            continue
        p = m.group(1).split("~")
        if len(p) < 33:
            continue
        try:
            out[key] = {"price": float(p[3]), "change_pct": float(p[32])}
        except (ValueError, IndexError):
            continue
    return out


def fetch_option_pcr(max_pages: int = 12) -> dict:
    """期权成交量 PCR（P7-2 2026-08-19）：全市场期权 认沽/认购 成交量比

    恐慌/贪婪温度计：PCR≥1.3 恐慌对冲占优（机构买保险），≤0.55 看涨占优。
    数据源东财期权列表（fs=m:10 全市场，与行业资金流同源），按合约名称
    "购"/"沽"分桶统计成交量；分页拉全量（每页500，上限 max_pages），
    任一页失败即中止按已拉数据统计（部分覆盖时 contracts < total 有标注）。
    返回 {"pcr", "call_vol", "put_vol", "contracts", "total"}；无认购量返回 {}。
    """
    call_v = put_v = contracts = total = 0
    for pn in range(1, max_pages + 1):
        text = _http_get("https://push2.eastmoney.com/api/qt/clist/get", params={
            "fid": "f5", "po": "1", "pz": "500", "pn": pn, "np": "1",
            "fltt": "2", "invt": "2", "fs": "m:10", "fields": "f12,f14,f5",
        })
        if not text:
            break
        try:
            data = json.loads(text).get("data") or {}
            rows = data.get("diff") or []
            total = int(data.get("total") or 0)
        except ValueError:
            break
        if not rows:
            break
        for r in rows:
            if not isinstance(r, dict):
                continue
            name = str(r.get("f14", "") or "")
            try:
                vol = int(r.get("f5") or 0)
            except (TypeError, ValueError):
                continue
            contracts += 1
            if "沽" in name:
                put_v += vol
            elif "购" in name:
                call_v += vol
        if contracts >= total > 0:
            break
    if call_v <= 0:
        return {}
    return {"pcr": round(put_v / call_v, 3), "call_vol": call_v,
            "put_vol": put_v, "contracts": contracts, "total": total}


def fetch_minute_kline(symbol: str = "sh000001", period: str = "m5",
                       count: int = 48) -> list:
    """分钟级 K 线（P8 2026-08-19 / P9 加固）：腾讯 ifzq mkline，盘中因子的数据底座

    数据源 https://ifzq.gtimg.cn/appstock/app/kline/mkline（与指数行情同域族，
    实测本地/云端均可用）。字段顺序 [时间YYYYMMDDHHmm, 开, 收, 高, 低, 量, {}]。
    默认 m5×48 根 = 当日全量 4 小时交易时段；返回升序 [{time,open,close,high,low,volume}]，
    失败返回 []（分钟因子缺省 0 分，不影响主流程）。

    P9（2026-08-20）：实测该接口对短时间连续请求有瞬时限流（空响应），
    空结果时 1s 后重试一次再放弃——盘中 15 分钟轮询叠加手动/测试调用时更稳。
    """
    url = f"https://ifzq.gtimg.cn/appstock/app/kline/mkline?param={symbol},{period},,{count}"
    text = _http_get(url)
    if not text:
        time.sleep(1)  # 腾讯 ifzq 瞬时限流兜底
        text = _http_get(url)
    if not text:
        return []
    try:
        data = json.loads(text).get("data") or {}
        rows = (data.get(symbol) or {}).get(period) or []
    except ValueError:
        return []
    out = []
    for r in rows:
        if not isinstance(r, list) or len(r) < 6:
            continue
        try:
            out.append({"time": str(r[0]), "open": float(r[1]), "close": float(r[2]),
                        "high": float(r[3]), "low": float(r[4]), "volume": float(r[5])})
        except (ValueError, TypeError):
            continue
    return out


# ============================================================
# 因子计算层
# ============================================================
def _third_friday(year: int, month: int) -> date:
    """某年某月的第三个周五（股指期货交割日 = 交割月第三个周五）"""
    first = date(year, month, 1)
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(days=14)


def _next_expiry_days(today: date = None) -> int:
    """主力合约剩余期限估算：距"下月第三个周五"的天数

    股指期货主力合约在当月交割前一周左右切换（如 8/21 交割，8 月中旬主力即切 9 月），
    临近交割的当月合约基差将在交割日收敛，不具对冲成本代表性。
    用下月交割日（当月+1 的第三个周五）近似主力剩余期限，年化贴水率更贴近中性策略实际口径。
    """
    today = today or date.today()
    m = today.month + 1
    y = today.year + (m - 1) // 12
    m = (m - 1) % 12 + 1
    d = _third_friday(y, m)
    return max(1, (d - today).days)


def _ma(values: list, n: int) -> float:
    if len(values) < n or n <= 0:
        return 0.0
    return sum(values[-n:]) / n


def calc_tech_factors(name: str, klines: list, quote: dict) -> dict:
    """技术面因子（指数级）：均线、动量、突破、放量"""
    if not klines or not quote:
        return {"name": name, "available": False}
    closes = [k["close"] for k in klines]
    volumes = [k["volume"] for k in klines]
    last = klines[-1]
    # 均线
    ma5 = _ma(closes, 5)
    ma10 = _ma(closes, 10)
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    price = quote["price"]
    # 均线状态
    if ma5 and ma10 and ma20 and ma60:
        if price > ma5 > ma10 > ma20 > ma60:
            trend = "多头排列"
        elif price < ma5 < ma10 < ma20 < ma60:
            trend = "空头排列"
        else:
            trend = "均线纠缠"
    else:
        trend = "数据不足"
    # 动量（5/20 日涨跌幅）
    mom5 = (price / closes[-6] - 1) * 100 if len(closes) >= 6 else 0.0
    mom20 = (price / closes[-21] - 1) * 100 if len(closes) >= 21 else 0.0
    # 突破：今日最高 vs 前20日（不含今日）最高；今日收盘 vs 前20日最低
    prev_high = max(k["high"] for k in klines[-TH_BREAK_WINDOW - 1:-1]) if len(klines) > TH_BREAK_WINDOW else last["high"]
    prev_low = min(k["low"] for k in klines[-TH_BREAK_WINDOW - 1:-1]) if len(klines) > TH_BREAK_WINDOW else last["low"]
    breakout = last["high"] > prev_high
    breakdown = last["close"] < prev_low
    # 放量：今日成交量 / 前5日、前20日均量（不含今日，标准量比口径）
    vol5 = _ma(volumes[:-1], 5)
    vol20 = _ma(volumes[:-1], 20)
    ratio5 = last["volume"] / vol5 if vol5 else 0.0
    ratio20 = last["volume"] / vol20 if vol20 else 0.0
    return {
        "name": name, "available": True,
        "price": price, "change_pct": quote["change_pct"],
        "ma5": ma5, "ma10": ma10, "ma20": ma20, "ma60": ma60,
        "trend": trend,
        "mom5": round(mom5, 2), "mom20": round(mom20, 2),
        "breakout": breakout, "breakdown": breakdown,
        "vol_ratio5": round(ratio5, 2), "vol_ratio20": round(ratio20, 2),
    }


def calc_basis(futures: dict, quotes: dict, remaining_days: int = None) -> dict:
    """股指期货基差：期货价 - 现货指数价；基差率 = 基差/现货×100%；年化贴水率 = 基差率×365/剩余天数

    年化贴水率是中性策略对冲成本的可比口径（日度基差率 -0.8% 对应年化约 -10%~-16%），
    剩余天数按最近交割日（当月/下月第三个周五）估算。
    """
    remaining_days = remaining_days or _next_expiry_days()
    result = {}
    for code, conf in FUTURES.items():
        fut = futures.get(code)
        idx = quotes.get(conf["index"])
        if not fut or not idx:
            continue
        basis = fut["price"] - idx["price"]
        basis_pct = basis / idx["price"] * 100 if idx["price"] else 0.0
        annual_pct = basis_pct * 365 / remaining_days if remaining_days else 0.0
        result[code] = {
            "index": conf["index"],
            "fut": fut["price"], "spot": idx["price"],
            "basis": round(basis, 2), "basis_pct": round(basis_pct, 3),
            "annual_pct": round(annual_pct, 2), "remaining_days": remaining_days,
        }
    return result


def calc_vol_regime(klines: list) -> dict:
    """已实现波动率状态（P3-3）：20日年化波动率 + 近一年滚动分位 → 高波/低波/正常

    机构 vol targeting 的核心输入：高波期系统性降仓。人工交易同样适用——
    高波状态提示降低单笔仓位/放宽止损容忍（波幅大易扫损），低波可适当放大。
    历史窗口不足 60 根 K 线 → available=False（分位无意义）。
    """
    if not klines or len(klines) < 60:
        return {"available": False}
    closes = [k["close"] for k in klines]
    rets = [closes[i] / closes[i - 1] - 1 for i in range(1, len(closes)) if closes[i - 1]]

    def _vol20(r: list) -> float:
        w = r[-20:]
        if len(w) < 20:
            return 0.0
        m = sum(w) / 20
        var = sum((x - m) ** 2 for x in w) / 19
        return (var * 252) ** 0.5 * 100

    hist = []
    for i in range(20, len(rets) + 1):
        v = _vol20(rets[:i])
        if v > 0:
            hist.append(v)
    if len(hist) < 20:
        return {"available": False}
    cur = hist[-1]
    pctile = sum(1 for v in hist if v <= cur) / len(hist) * 100
    if pctile >= TH_VOL_PCTILE_HIGH:
        regime = "高波"
    elif pctile <= TH_VOL_PCTILE_LOW:
        regime = "低波"
    else:
        regime = "正常"
    return {"available": True, "vol20": round(cur, 1), "pctile": round(pctile), "regime": regime}


def calc_style_rotation() -> dict:
    """大小盘风格轮动（P3-4）：上证50/中证1000 比价及 5/20 日变化

    比价升 → 大盘价值占优（科技成长逆风），降 → 小盘成长占优（自选股顺风）。
    两指数同一交易日历，收盘价按位对齐；数据不足返回 {}。
    """
    big = fetch_index_kline(INDEXES["上证50"]["sina"], 65)
    small = fetch_index_kline(INDEXES["中证1000"]["sina"], 65)
    n = min(len(big), len(small))
    if n < 21:
        return {}
    ratios = [big[i]["close"] / small[i]["close"] for i in range(n - 21, n) if small[i]["close"]]
    if len(ratios) < 21:
        return {}
    cur = ratios[-1]
    chg5 = (cur / ratios[-6] - 1) * 100
    chg20 = (cur / ratios[0] - 1) * 100
    if chg20 >= TH_STYLE_CHG20:
        trend = "大盘占优"
    elif chg20 <= -TH_STYLE_CHG20:
        trend = "小盘占优"
    else:
        trend = "风格均衡"
    return {"ratio": round(cur, 4), "chg5": round(chg5, 2), "chg20": round(chg20, 2), "trend": trend}


# ============================================================
# P8（2026-08-19）因子池扩展：日线衍生因子 + 分钟级因子（均为影子维度）
# 复用已拉的 65/260 日 K 线与当日 m5 分钟线，零/单次额外请求；
# 全部只记录进 direction_history 供 compute_factor_ic 回测，
# IC 达标后逐个升级正式维度（机构级因子上线流程，与 P7 同口径）。
# ============================================================
MOM_WINDOW = 20               # 动量窗口（交易日）
REV_WINDOW = 5                # 短期反转窗口
REV_OVERSOLD = -5.0           # 5日跌幅 ≤-5% → 超跌反弹倾向 +1
REV_OVERBOUGHT = 8.0          # 5日涨幅 ≥+8% → 超买回落倾向 -1
VOLRATIO_CONFIRM = 1.2        # 量比确认阈值（近5日均量 / 前20日均量）
MA_FAST, MA_SLOW = 5, 20      # 均线结构快慢线


def calc_daily_derived_factors(klines: list) -> dict:
    """日线衍生影子因子（P8）：动量/反转/均线/量价/跳空，复用上证日K

    打分口径（+1 利好倾向 / -1 利空倾向 / 0 中性）：
    - 动量: 近20日累计收益正负（趋势延续倾向）
    - 反转: 5日超跌 +1 / 超买 -1（均值回归倾向，与动量反向时互抵由回测裁决）
    - 均线: 收盘>MA20 且 MA5>MA20 = +1（多头结构）；收盘<MA20 且 MA5<MA20 = -1
    - 量价: 近5日均量/前20日均量 ≥1.2 时的涨跌方向（放量确认）
    - 跳空: 今日开盘 vs 昨收缺口方向（高开+1/低开-1，日内情绪起点）
    数据不足（<25 根）返回空 dict，各维度记 0 分。
    """
    if len(klines) < 25:
        return {}
    closes = [k["close"] for k in klines]
    vols = [k.get("volume") or 0 for k in klines]
    out = {}
    # 动量（20日）
    mom = (closes[-1] / closes[-MOM_WINDOW] - 1) * 100
    out["momentum_20d"] = 1.0 if mom > 0 else (-1.0 if mom < 0 else 0.0)
    out["_momentum_desc"] = f"近{MOM_WINDOW}日 {mom:+.1f}%"
    # 反转（5日）
    rev = (closes[-1] / closes[-REV_WINDOW] - 1) * 100
    if rev <= REV_OVERSOLD:
        out["reversal_5d"] = 1.0
    elif rev >= REV_OVERBOUGHT:
        out["reversal_5d"] = -1.0
    else:
        out["reversal_5d"] = 0.0
    out["_reversal_desc"] = f"近{REV_WINDOW}日 {rev:+.1f}%"
    # 均线结构
    ma_fast = sum(closes[-MA_FAST:]) / MA_FAST
    ma_slow = sum(closes[-MA_SLOW:]) / MA_SLOW
    if closes[-1] > ma_slow and ma_fast > ma_slow:
        out["ma_structure"] = 1.0
    elif closes[-1] < ma_slow and ma_fast < ma_slow:
        out["ma_structure"] = -1.0
    else:
        out["ma_structure"] = 0.0
    out["_ma_desc"] = (f"收盘{closes[-1]:.0f} vs MA{MA_SLOW} {ma_slow:.0f}"
                       f"（MA{MA_FAST} {ma_fast:.0f}）")
    # 量价配合
    vol5 = sum(vols[-5:]) / 5
    vol20 = sum(vols[-20:]) / 20
    ratio = vol5 / vol20 if vol20 else 0
    chg1 = closes[-1] - closes[-2]
    if ratio >= VOLRATIO_CONFIRM and chg1 != 0:
        out["volume_price"] = 1.0 if chg1 > 0 else -1.0
    else:
        out["volume_price"] = 0.0
    out["_vp_desc"] = f"量比{ratio:.2f} 昨{'涨' if chg1 > 0 else '跌'}"
    # 跳空缺口（今日开盘 vs 昨收）
    gap = (klines[-1]["open"] / closes[-2] - 1) * 100
    out["gap_today"] = 1.0 if gap > 0.05 else (-1.0 if gap < -0.05 else 0.0)
    out["_gap_desc"] = f"今日开盘缺口 {gap:+.2f}%"
    return out


def calc_minute_factors(minute_klines: list) -> dict:
    """分钟级影子因子（P8）：盘中动量 / 短线动能，m5 分钟线驱动

    打分口径：
    - 盘中动量: 当日全时段 m5 收盘价线性回归斜率（开盘→现在的整体趋势方向）
    - 短线动能: 最近 6 根 m5（30分钟）净方向（盘中实时有效，盘后即尾盘结论）
    斜率用首尾差/首值归一（简化口径，方向信息与最小二乘一致且零依赖）。
    数据不足（<6 根）返回空 dict。
    """
    if len(minute_klines) < 6:
        return {}
    closes = [k["close"] for k in minute_klines]
    out = {}
    # 盘中动量：全时段首尾方向（归一化）
    span = closes[-1] - closes[0]
    base = closes[0] or 1
    intraday = (span / base) * 100
    out["intraday_momentum"] = 1.0 if intraday > 0.1 else (-1.0 if intraday < -0.1 else 0.0)
    out["_intraday_desc"] = f"开盘至今 {intraday:+.2f}%（{len(closes)}根m5）"
    # 短线动能：最近 30 分钟净方向
    short = closes[-1] - closes[-6]
    short_pct = (short / (closes[-6] or 1)) * 100
    out["short_term_energy"] = 1.0 if short_pct > 0.05 else (-1.0 if short_pct < -0.05 else 0.0)
    out["_short_desc"] = f"近30分钟 {short_pct:+.2f}%"
    return out


# ============================================================
# 异动检测层
# ============================================================
def _history_values(seq: list) -> list:
    """提取贴水历史序列的数值（兼容旧 float 格式与新 {date,value} 格式）"""
    vals = []
    for x in seq or []:
        if isinstance(x, dict):
            v = x.get("v")
        elif isinstance(x, (int, float)):
            v = x
        else:
            v = None
        if v is not None:
            vals.append(v)
    return vals


def detect_anomalies(tech: dict, basis: dict, fx: dict, history: dict = None) -> tuple:
    """返回 (signals, new_history)。

    贴水"走扩"用 20 日历史分位：当前基差率创近 20 日最深（且序列≥5 个样本）才告警，
    避免常态贴水（A股股指期货常态日度 -0.8%~-1.3%）误报；其余因子用绝对阈值。
    2026-08-14 P0 修复：basis_history 按"交易日"采样（每交易日一个样本，取当日最新值），
    使"20 日最深"真正等于 20 个交易日，而非盘中 15 分钟一轮的约 5 小时。
    """
    signals = []
    history = history or {}
    new_history = {k: list(v) for k, v in history.items()}
    today = datetime.now().strftime("%Y-%m-%d")
    # 1) 股指期货贴水走扩（中性策略对冲成本上升，量化倾向降仓）
    for code in ("IC", "IM", "IF", "IH"):
        b = basis.get(code)
        if not b:
            continue
        cur = b["basis_pct"]
        seq = new_history.setdefault(code, [])
        # 按交易日采样：同一天更新最后样本（取当日最新），跨天才 append 新样本
        last = seq[-1] if seq else None
        if isinstance(last, dict) and last.get("d") == today:
            last["v"] = cur
        else:
            seq.append({"d": today, "v": cur})
        del seq[:-TH_BASIS_HISTORY]  # 只保留最近 N 个交易日
        values = _history_values(seq)
        if len(values) >= 5 and cur < min(values[:-1]):
            prev_min = min(values[:-1])
            signals.append({
                "key": f"basis_{code}",
                "level": "warning",
                "direction": "bearish",
                "title": f"{code} 贴水走扩（创20日最深）",
                "detail": f"{b['index']}：{code} 基差率 {cur}%（20日最深前值 {prev_min}%，年化 {b['annual_pct']}%），中性策略对冲成本上升、量化倾向降仓",
            })
    # 2) 美元/日元异动（套息交易平仓风险）
    jpy = fx.get("fx_susdjpy")
    if jpy and abs(jpy["change_pct"]) >= TH_FX_JPY_PCT:
        direction = "bearish" if jpy["change_pct"] < 0 else "bullish"
        signals.append({
            "key": "fx_usdjpy",
            "level": "warning",
            "direction": direction,
            "title": "日元急" + ("升" if jpy["change_pct"] < 0 else "贬"),
            "detail": f"美元/日元 {jpy['price']:.2f}（{jpy['change_pct']:+.2f}%），套息交易平仓风险" if jpy["change_pct"] < 0 else f"美元/日元 {jpy['price']:.2f}（{jpy['change_pct']:+.2f}%），日元走弱、套息资金回流风险资产",
        })
    # 3) 技术面：核心指数放量突破 / 放量破位
    for name in CORE_INDEXES:
        t = tech.get(name)
        if not t or not t.get("available"):
            continue
        if t["breakout"] and t["vol_ratio5"] >= TH_VOLUME_RATIO:
            signals.append({
                "key": f"breakout_{name}",
                "level": "info",
                "direction": "bullish",
                "title": f"{name} 放量突破20日新高",
                "detail": f"{name} {t['price']:.2f}（{t['change_pct']:+.2f}%），成交量 {t['vol_ratio5']}x 5日均量",
            })
        if t["breakdown"] and t["vol_ratio5"] >= TH_VOLUME_RATIO:
            signals.append({
                "key": f"breakdown_{name}",
                "level": "warning",
                "direction": "bearish",
                "title": f"{name} 放量跌破20日低点",
                "detail": f"{name} {t['price']:.2f}（{t['change_pct']:+.2f}%），成交量 {t['vol_ratio5']}x 5日均量",
            })
    return signals, new_history


def calc_risk_state(signals: list) -> str:
    """综合异动信号 → 风险状态：任一 warning 信号（贴水走扩/日元急升/放量破位）→ risk_off

    risk_off（风险收缩期）供 real_time_push 联动：对无硬事件佐证的科技利好降级不推。
    """
    if any(s.get("level") == "warning" for s in signals):
        return "risk_off"
    return "neutral"


def detect_stock_anomalies(tech: dict) -> list:
    """自选股异动（P1-2）：涨跌幅/量比/20日突破破位，单股单信号（按优先级取最先命中）

    优先级：涨跌幅异动 > 显著放量 > 放量突破/破位（同股多条件命中时只报最显著的一条，
    防单股多信号刷屏；各条件独立冷却 key，次日另一条件触发仍可推）。
    """
    signals = []
    for name, t in (tech or {}).items():
        if not isinstance(t, dict) or not t.get("available"):
            continue
        code = str(t.get("code", "") or "")
        label = f"{name}({code})" if code else name
        base = f"{label} {t['price']:.2f}（{t['change_pct']:+.2f}%）{t['trend']}"
        if abs(t["change_pct"]) >= TH_STOCK_CHG_PCT:
            up = t["change_pct"] > 0
            signals.append({
                "key": f"stock_chg_{code or name}",
                "level": "info" if up else "warning",
                "direction": "bullish" if up else "bearish",
                "title": f"{label} {'大涨' if up else '大跌'} {t['change_pct']:+.2f}%",
                "detail": f"{base}，量比5日 {t['vol_ratio5']}x",
                "stock": name, "code": code,
            })
        elif t["vol_ratio5"] >= TH_STOCK_VOL_RATIO:
            up = t["change_pct"] >= 0
            signals.append({
                "key": f"stock_vol_{code or name}",
                "level": "info",
                "direction": "bullish" if up else "bearish",
                "title": f"{label} 显著放量（量比 {t['vol_ratio5']}x）",
                "detail": f"{base}，成交量 {t['vol_ratio5']}x 5日均量",
                "stock": name, "code": code,
            })
        elif (t["breakout"] or t["breakdown"]) and t["vol_ratio5"] >= TH_VOLUME_RATIO:
            up = t["breakout"]
            signals.append({
                "key": f"stock_brk_{code or name}",
                "level": "info",
                "direction": "bullish" if up else "bearish",
                "title": f"{label} 放量{'突破20日新高' if up else '跌破20日低点'}",
                "detail": f"{base}，量比5日 {t['vol_ratio5']}x",
                "stock": name, "code": code,
            })
    return signals


def detect_flow_anomalies(flows: dict) -> list:
    """资金流异动（P1-3）：主力大幅净流入/流出、融资余额大增/大减

    不参与 risk_state 计算（risk 口径维持贴水/日元/破位不变），只并入告警推送。
    """
    signals = []
    main_net = flows.get("main_net_yi")
    if isinstance(main_net, (int, float)) and abs(main_net) >= TH_MAIN_NETFLOW_YI:
        inflow = main_net > 0
        signals.append({
            "key": "flow_main_net",
            "level": "info" if inflow else "warning",
            "direction": "bullish" if inflow else "bearish",
            "title": f"两市主力资金净{'流入' if inflow else '流出'} {abs(main_net):.0f} 亿",
            "detail": f"两市主力资金当日累计净{'流入' if inflow else '流出'} {abs(main_net):.0f} 亿"
                      f"（阈值 {TH_MAIN_NETFLOW_YI} 亿），大单/超大单{'积极进场' if inflow else '集中撤离'}",
        })
    margin_chg = flows.get("margin_chg_yi")
    if isinstance(margin_chg, (int, float)) and abs(margin_chg) >= TH_MARGIN_CHG_YI:
        up = margin_chg > 0
        signals.append({
            "key": "flow_margin",
            "level": "info",
            "direction": "bullish" if up else "bearish",
            "title": f"融资余额{'增加' if up else '减少'} {abs(margin_chg):.0f} 亿",
            "detail": f"融资余额 {flows.get('margin_yi', 0):.0f} 亿，较前一交易日"
                      f"{'增加' if up else '减少'} {abs(margin_chg):.0f} 亿（阈值 {TH_MARGIN_CHG_YI} 亿），"
                      f"杠杆资金{'加仓' if up else '降杠杆'}",
        })
    return signals


def detect_global_anomalies(global_quotes: dict) -> list:
    """隔夜外盘异动（P3-1）：|涨跌| ≥2% 告警，≥3% 升级 warning（联动 risk_off）

    自选股为 AI 硬件链，隔夜英伟达/纳指大跌是最直接的盘中预警先行指标。
    """
    signals = []
    for name, q in (global_quotes or {}).items():
        chg = q.get("change_pct") if isinstance(q, dict) else None
        if not isinstance(chg, (int, float)) or abs(chg) < TH_GLOBAL_PCT:
            continue
        down = chg < 0
        signals.append({
            "key": f"global_{name}",
            "level": "warning" if abs(chg) >= 3.0 else "info",
            "direction": "bearish" if down else "bullish",
            "title": f"隔夜{name}{'大跌' if down else '大涨'} {chg:+.2f}%",
            "detail": f"{name} {q['price']:.2f}（{chg:+.2f}%，阈值 {TH_GLOBAL_PCT}%），"
                      f"AI 硬件链/科技股开盘情绪{'承压' if down else '提振'}",
        })
    return signals


def detect_breadth_anomalies(breadth: dict) -> list:
    """市场宽度极端（P3-2）：跌停潮（warning）/极端普跌（info）

    跌停潮纳入 risk_off 口径（情绪冰点时科技利好新闻应降级——与 real_time_push
    的 _risk_off_downgrade 联动）。
    """
    signals = []
    if not isinstance(breadth, dict) or not breadth:
        return signals
    ld = breadth.get("limit_down")
    if isinstance(ld, int) and ld >= TH_LIMIT_DOWN:
        signals.append({
            "key": "breadth_limit_down",
            "level": "warning",
            "direction": "bearish",
            "title": f"跌停潮：{ld} 家跌停",
            "detail": f"跌停 {ld} 家（阈值 {TH_LIMIT_DOWN}），涨停仅 {breadth.get('limit_up', 0)} 家，"
                      f"情绪冰点",
        })
    dp = breadth.get("down_pct")
    if isinstance(dp, (int, float)) and dp >= TH_BREADTH_DOWN_PCT:
        signals.append({
            "key": "breadth_down_pct",
            "level": "info",
            "direction": "bearish",
            "title": f"极端普跌：{dp:.0f}% 个股下跌",
            "detail": f"下跌 {breadth.get('dec', 0)} 家 / 上涨 {breadth.get('adv', 0)} 家，"
                      f"跌超5% {breadth.get('big_down', 0)} 家",
        })
    return signals


def detect_sentiment_anomalies(sentiment: dict) -> list:
    """涨停情绪极端（P4-2）：炸板率≥50%（warning，并入 risk_off）/ 最高连板≥6（info）

    炸板潮=连板晋级失败率高 → 短线资金退潮信号，属市场级风险（与跌停潮同口径）；
    连板高度为投机过热提示（bullish 事件：情绪周期高潮），不切 risk_off。
    """
    signals = []
    if not isinstance(sentiment, dict) or not sentiment:
        return signals
    zbr = sentiment.get("zbr")
    if isinstance(zbr, (int, float)) and zbr >= TH_ZB_WARN:
        signals.append({
            "key": "sentiment_zbr",
            "level": "warning",
            "direction": "bearish",
            "title": f"炸板率 {zbr:.0f}%（分歧极端）",
            "detail": f"涨停 {sentiment.get('zt', 0)} 家 / 炸板 {sentiment.get('zb', 0)} 家"
                      f"（阈值 {TH_ZB_WARN}%），连板晋级失败率高，短线情绪退潮风险",
        })
    lbc = sentiment.get("max_lbc")
    if isinstance(lbc, int) and lbc >= TH_MAX_LBC_WARN:
        signals.append({
            "key": "sentiment_lbc",
            "level": "info",
            "direction": "bullish",
            "title": f"最高连板 {lbc} 板（投机过热）",
            "detail": f"涨停梯队 {sentiment.get('lbc_dist', '')}，妖股行情延续，"
                      f"警惕监管关注与情绪高潮后的退潮",
        })
    return signals


def detect_liquidity_anomalies(liquidity: dict) -> list:
    """资金面收紧（P7-1）：GC007 ≥3.5% 或 日内急升 ≥50%（warning，并入 risk_off）

    资金面紧张是市场级风险（杠杆资金成本抬升 → 风险资产承压），与跌停潮/炸板潮
    同口径切 risk_off；利率绝对水平与日内急升分开报（税期/跨月扰动先看急升）。
    """
    signals = []
    if not isinstance(liquidity, dict) or not liquidity:
        return signals
    gc = liquidity.get("gc007") or {}
    price, chg = gc.get("price"), gc.get("change_pct")
    if not isinstance(price, (int, float)):
        return signals
    if price >= TH_GC007_ALERT:
        signals.append({
            "key": "liquidity_gc007_high",
            "level": "warning",
            "direction": "bearish",
            "title": f"GC007 {price:.2f}%（资金面收紧）",
            "detail": f"交易所7天回购利率升至 {price:.2f}%（阈值 {TH_GC007_ALERT}%），"
                      f"杠杆资金成本抬升，风险资产承压；关注税期/跨月扰动是否持续",
        })
    elif (isinstance(chg, (int, float)) and chg >= TH_GC007_SPIKE_ALERT
          and price >= 2.0):
        signals.append({
            "key": "liquidity_gc007_spike",
            "level": "warning",
            "direction": "bearish",
            "title": f"GC007 急升 {chg:+.0f}% 至 {price:.2f}%",
            "detail": f"交易所7天回购利率日内急升（昨 {price / (1 + chg / 100):.2f}%），"
                      f"资金面边际收紧信号，关注央行公开市场操作",
        })
    return signals


def detect_option_anomalies(option: dict) -> list:
    """期权恐慌极端（P7-2）：成交量 PCR ≥1.5（info，不切 risk_off）

    PCR 是情绪温度计而非风险开关（统计口径含投机盘噪声），极端恐慌时单独告警
    供人工判断，与资金流异动同口径（只告警不切 risk_off）。
    """
    signals = []
    if not isinstance(option, dict) or not option:
        return signals
    pcr = option.get("pcr")
    if isinstance(pcr, (int, float)) and pcr >= TH_PCR_ALERT:
        signals.append({
            "key": "option_pcr_panic",
            "level": "info",
            "direction": "bearish",
            "title": f"期权 PCR {pcr:.2f}（恐慌对冲极端）",
            "detail": f"认沽/认购成交量比 {pcr:.2f}（阈值 {TH_PCR_ALERT}，"
                      f"认购 {option.get('call_vol', 0)} / 认沽 {option.get('put_vol', 0)} 张），"
                      f"机构买保险需求占优，警惕恐慌情绪传染",
        })
    return signals


def monitor_stocks() -> tuple:
    """自选股监控入口（P1-2）→ (异动信号, 技术面明细, 紧凑快照)

    watchlist 无带代码条目 → ([], {}, {})（监控静默关闭）；
    单只个股行情/K线失败仅跳过该股（quote 缺失或 K 线空 → available=False）。
    """
    stocks = load_watchlist_stocks()
    if not stocks:
        return [], {}, {}
    quotes = fetch_stock_quotes([s["symbol"] for s in stocks])
    tech = {}
    for s in stocks:
        q = quotes.get(s["symbol"])
        if not q:
            continue
        kline = fetch_index_kline(s["symbol"], 65)
        t = calc_tech_factors(s["name"], kline, q)
        t["code"] = s["code"]
        tech[s["name"]] = t
    signals = detect_stock_anomalies(tech)
    snap = {}
    for name, t in tech.items():
        if isinstance(t, dict) and t.get("available"):
            snap[name] = {
                "code": str(t.get("code", "") or ""),
                "price": round(_to_float(t.get("price")), 2),
                "change_pct": round(_to_float(t.get("change_pct")), 2),
            }
    return signals, tech, snap


# ============================================================
# 量化方向信号（2026-08-14 用户核心需求："利好因子买、利空因子卖，和量化同步"）
# 把量化因子翻译成"方向信号"：每个维度打分（+1 利好 / -1 利空 / 0 中性），
# 多因子合成综合方向（偏多=利好 / 偏空=利空 / 中性），方向改变时推送"量化方向信号"。
# 注：推送展示只用"利好/利空"口径（用户明确不写"买卖方向"）。
# ============================================================
def _calc_basis_direction(history: dict) -> dict:
    """各股指合约贴水方向：走扩(贴水加深=中性策略防守/减仓) / 收敛(贴水变浅=进攻/加仓) / 走平

    用最近 BASIS_DIR_LOOKBACK 期（>=3 个交易日）单调判断，防单期抖动：
    - 贴水率更负且逐期加深（-1.0 > -1.2 > -1.4）→ 走扩
    - 贴水率逐期变浅（-1.4 < -1.2 < -1.0）→ 收敛
    基于按交易日采样的 basis_history（{date,value}），日度贴水比盘中 15 分钟采样更稳。
    """
    result = {}
    for code in ("IC", "IM", "IF", "IH"):
        seq = history.get(code, [])
        values = _history_values(seq)
        if len(values) < BASIS_DIR_LOOKBACK:
            result[code] = "走平"  # 样本不足
            continue
        last3 = values[-BASIS_DIR_LOOKBACK:]
        if last3[0] > last3[1] > last3[2]:
            result[code] = "走扩"
        elif last3[0] < last3[1] < last3[2]:
            result[code] = "收敛"
        else:
            result[code] = "走平"
    return result


def _direction_analysis(tech: dict, basis: dict, fx: dict, risk_state: str, history: dict,
                        vol: dict = None, breadth: dict = None, weights: dict = None,
                        liquidity: dict = None, option: dict = None,
                        daily_factors: dict = None, minute_factors: dict = None,
                        global_quotes: dict = None) -> dict:
    """多因子合成量化方向信号

    六个维度各打分（+1 利好 / -1 利空 / 0 中性）：
    - 对冲（IC/IM 贴水方向）：收敛=+1（中性策略加仓→利好），走扩=-1（减仓→利空）
    - 风险（risk_state）：neutral=0，risk_off=-1（风险收缩=利空）
    - 量价（上证/创业板放量突破/破位）：突破=+1，破位=-1
    - 汇率（美元/日元）：单日波动>=1.5% 且日元急升(美元/日元跌)=-1（套息平仓→利空），日元急贬=+1
    - 波动率（P3-3）：任一核心指数高波=-1（高波期机构降杠杆）；正常/低波=0
    - 宽度（P3-2）：极端普跌（≥80%个股下跌）=-1，普涨（≤20%）=+1，其余=0
    综合分默认 = 6 个维度均值（含中性维度，多数维度同向才判方向），>=0.5 偏多(利好) /
    <=-0.5 偏空(利空) / 否则中性。
    weights（P4-6 2026-08-19）：IC 加权——各维度按回测 IC 重排话语权（历史≥20个交易日
    由 signal_backtest.compute_factor_ic 提供；未提供/样本不足时等权）。
    加权综合分 = Σ(w_i×s_i)/Σ(w_i)，权重未覆盖的维度按地板 0.1 参与
    （与 compute_factor_ic 的收缩口径一致）；阈值 ±0.5 的"多数同向"语义不变。
    P5-1 门控（2026-08-19）：非线性状态调节——权重在特定状态下被放大
    （显式规则非黑箱，归因到"哪个门控、放大了哪些维度"）：
    - 高波状态 → 利空维度权重 ×1.5（机构降杠杆环境，利空自我强化）
    - 极端普跌 + 日元急升 → 汇率/宽度权重 ×1.5（套息平仓连锁，共振 > 线性加总）
    P7 影子因子（2026-08-19，机构级因子上线流程）：流动性（GC007）/期权情绪（PCR）
    先以"影子维度"记录与展示（进 factors/direction_history 供 compute_factor_ic 回测），
    不参与综合分与门控——避免未经验证的新维度稀释已验证的 6 维话语权
    （如 -5/6=-0.83 强信号会被稀释成 -5/8=-0.63 弱信号）；IC 达标后可升级为正式维度。
    P12 影子维度（2026-08-21）：韩指 KOSPI（存储链先行，±2% 同环境行口径），
    与 P7/P8 同流程：打分记录进 direction_history 供 IC 累积，不参与综合分。
    返回值含 gates（生效门控说明）与 eff_weights（生效权重，含门控倍数）、
    shadow（影子维度名集合，展示层据此标注）。
    """
    dims = []  # (名称, 分值, 说明)
    # ① 对冲
    basis_dir = _calc_basis_direction(history)
    hedge_scores = []
    hedge_parts = []
    for code in ("IC", "IM"):
        d = basis_dir.get(code, "走平")
        if d == "收敛":
            hedge_scores.append(1); hedge_parts.append(f"{code}贴水收敛")
        elif d == "走扩":
            hedge_scores.append(-1); hedge_parts.append(f"{code}贴水走扩")
    if hedge_scores:
        hedge_desc = ("中性策略加仓/平对冲" if sum(hedge_scores) > 0
                      else ("中性策略减仓/加对冲" if sum(hedge_scores) < 0 else "对冲方向不明"))
        dims.append(("对冲", sum(hedge_scores) / len(hedge_scores),
                     hedge_desc + "（" + "、".join(hedge_parts) + "）"))
    # ② 风险
    if risk_state == "risk_off":
        dims.append(("风险", -1.0, "风险收缩期（贴水走扩/日元急升/放量破位）"))
    else:
        dims.append(("风险", 0.0, "风险中性"))
    # ③ 量价
    vol_score = 0.0
    vol_part = "量价平稳"
    for name in CORE_INDEXES:
        t = tech.get(name)
        if not t or not t.get("available"):
            continue
        if t["breakout"] and t["vol_ratio5"] >= TH_VOLUME_RATIO:
            vol_score = 1.0; vol_part = f"{name} 放量突破"
            break
        if t["breakdown"] and t["vol_ratio5"] >= TH_VOLUME_RATIO:
            vol_score = -1.0; vol_part = f"{name} 放量破位"
            break
    dims.append(("量价", vol_score, vol_part))
    # ④ 汇率
    fx_score = 0.0
    fx_part = "汇率平稳"
    jpy = fx.get("fx_susdjpy")
    if jpy and abs(jpy["change_pct"]) >= TH_FX_JPY_PCT:
        if jpy["change_pct"] < 0:
            fx_score = -1.0; fx_part = f"日元急升 {jpy['change_pct']:+.2f}%（套息平仓风险）"
        else:
            fx_score = 1.0; fx_part = f"日元急贬 {jpy['change_pct']:+.2f}%（套息资金回流）"
    dims.append(("汇率", fx_score, fx_part))
    # ⑤ 波动率（P3-3）：高波环境利空（机构降杠杆，人工同样应降仓）
    vol_score = 0.0
    vol_part = "波动率正常"
    for name, v in (vol or {}).items():
        if isinstance(v, dict) and v.get("regime") == "高波":
            vol_score = -1.0
            vol_part = f"{name} 高波（20日波动率 {v.get('vol20')}%，{v.get('pctile', 0):.0f}分位）"
            break
    dims.append(("波动率", vol_score, vol_part))
    # ⑥ 宽度（P3-2）：极端普跌利空 / 普涨利多
    breadth_score = 0.0
    breadth_part = "宽度均衡"
    dp = (breadth or {}).get("down_pct")
    if isinstance(dp, (int, float)):
        if dp >= TH_BREADTH_DOWN_PCT:
            breadth_score = -1.0
            breadth_part = f"极端普跌（{dp:.0f}%个股下跌）"
        elif dp <= 20:
            breadth_score = 1.0
            breadth_part = f"普涨（仅{dp:.0f}%下跌）"
    dims.append(("宽度", breadth_score, breadth_part))

    # P7 影子维度（打分记录与展示，不参与综合分/门控/生效权重）：
    # ⑦ 流动性（GC007）：≥3% 收紧=-1；日涨≥30% 且 ≥2% 利率急升=-1；宽松区间=0
    #   （低利率是常态而非边际利好，只报收紧不报宽松）
    # ⑧ 期权情绪（PCR）：≥1.3 恐慌对冲=-1；≤0.55 看涨占优=+1；正常区间=0
    shadow_dims = []
    gc = (liquidity or {}).get("gc007") or {}
    liq_score, liq_part = 0.0, "资金面平稳"
    gc_price, gc_chg = gc.get("price"), gc.get("change_pct")
    if isinstance(gc_price, (int, float)):
        if gc_price >= TH_GC007_TIGHT:
            liq_score = -1.0
            liq_part = f"GC007 {gc_price:.2f}%（资金面收紧，阈值{TH_GC007_TIGHT}%）"
        elif (isinstance(gc_chg, (int, float)) and gc_chg >= TH_GC007_SPIKE_PCT
              and gc_price >= 2.0):
            liq_score = -1.0
            liq_part = f"GC007 {gc_price:.2f}% 急升{gc_chg:+.0f}%（利率急升）"
        else:
            liq_part = f"GC007 {gc_price:.2f}%（资金面平稳）"
    shadow_dims.append(("流动性", liq_score, liq_part))
    pcr = (option or {}).get("pcr")
    opt_score, opt_part = 0.0, "期权情绪中性"
    if isinstance(pcr, (int, float)):
        if pcr >= TH_PCR_PANIC:
            opt_score = -1.0
            opt_part = f"PCR {pcr:.2f}（恐慌对冲占优）"
        elif pcr <= TH_PCR_GREED:
            opt_score = 1.0
            opt_part = f"PCR {pcr:.2f}（看涨情绪占优）"
        else:
            opt_part = f"PCR {pcr:.2f}（情绪中性）"
    shadow_dims.append(("期权情绪", opt_score, opt_part))
    # P8 影子维度（因子池扩展，2026-08-19）：日线衍生 5 维 + 分钟级 2 维。
    # 全部与 P7 同口径：打分记录进 direction_history 供 IC 回测，不参与综合分。
    p8_dims = [
        ("动量(20日)", daily_factors or {}, "momentum_20d", "_momentum_desc", "动量数据不足"),
        ("反转(5日)", daily_factors or {}, "reversal_5d", "_reversal_desc", "反转数据不足"),
        ("均线结构", daily_factors or {}, "ma_structure", "_ma_desc", "均线数据不足"),
        ("量价配合", daily_factors or {}, "volume_price", "_vp_desc", "量价数据不足"),
        ("跳空缺口", daily_factors or {}, "gap_today", "_gap_desc", "缺口数据不足"),
        ("盘中动量", minute_factors or {}, "intraday_momentum", "_intraday_desc", "分钟数据不足"),
        ("短线动能", minute_factors or {}, "short_term_energy", "_short_desc", "分钟数据不足"),
    ]
    for name, src, key, desc_key, miss_desc in p8_dims:
        s = src.get(key)
        if isinstance(s, (int, float)):
            shadow_dims.append((name, float(s), str(src.get(desc_key) or miss_desc)))
        else:
            shadow_dims.append((name, 0.0, miss_desc))
    # P12 影子维度：韩指 KOSPI（存储链先行，14:30 BJT 收盘早于 A 股，盘中实时信号；
    # |涨跌|≥2% 与环境行同口径，恒定记录供 IC 累积）
    kospi = (global_quotes or {}).get("韩国KOSPI") or {}
    kc = kospi.get("change_pct")
    if isinstance(kc, (int, float)) and abs(kc) >= TH_KOSPI_PCT:
        if kc > 0:
            ko_score, ko_part = 1.0, f"韩KOSPI {kc:+.2f}%（存储链先行大涨）"
        else:
            ko_score, ko_part = -1.0, f"韩KOSPI {kc:+.2f}%（存储链先行大跌）"
    else:
        ko_score, ko_part = 0.0, "韩指平稳"
    shadow_dims.append(("韩指", ko_score, ko_part))
    shadow_names = {name for name, s, _ in shadow_dims if s != 0}
    # shadow: 非零影子（门控/权重排除判定用）；shadow_all: 全量影子名（含零分，
    # 展示层隐藏零分影子用——两集合语义不同不可混用）
    shadow_all = {name for name, _, _ in shadow_dims}

    # P5-1：非线性门控——状态调节权重（显式规则，强归因：说明哪个门控放大了哪些维度）
    gates = []  # [(说明, 受影响维度名集合, 倍数)]
    high_vol = any(isinstance(v, dict) and v.get("regime") == "高波"
                   for v in (vol or {}).values())
    if high_vol:
        gates.append((f"高波状态·利空维度升权×{TH_GATE_MULT:g}",
                      {name for name, s, _ in dims if s < 0}, TH_GATE_MULT))
    jpy_c = jpy.get("change_pct") if isinstance(jpy, dict) else None
    if (isinstance(dp, (int, float)) and dp >= TH_BREADTH_DOWN_PCT
            and isinstance(jpy_c, (int, float)) and jpy_c <= -TH_FX_JPY_PCT):
        gates.append((f"套息平仓共振（普跌+日元急升）·汇率/宽度升权×{TH_GATE_MULT:g}",
                      {"汇率", "宽度"}, TH_GATE_MULT))

    # 综合分：等权均值 或 IC 加权（Σ(w×s)/Σw），门控倍数乘在基线权重上；
    # 保守口径：多数维度同向才判方向，防单维度噪音误判
    eff_w = {}
    for name, s, _ in dims:
        w = _to_float((weights or {}).get(name), 0.1) if weights else 1.0
        if w <= 0:
            w = 0.1 if weights else 1.0
        for _, affected, mult in gates:
            if name in affected:
                w = round(w * mult, 3)
        eff_w[name] = w
    w_sum = sum(eff_w.values())
    score = sum(eff_w[name] * s for name, s, _ in dims) / w_sum if w_sum else 0.0
    if score >= 0.5:
        direction = "偏多"
    elif score <= -0.5:
        direction = "偏空"
    else:
        direction = "中性"
    return {
        "score": round(score, 2), "direction": direction,
        "factors": dims + shadow_dims, "basis_dir": basis_dir, "risk_state": risk_state,
        "weighted": bool(weights), "weights": weights or {},
        "gates": [desc for desc, _, _ in gates], "eff_weights": eff_w,
        "shadow": shadow_names, "shadow_all": shadow_all,
    }


def _direction_changed(analysis: dict, last_dir: str) -> bool:
    """方向是否改变：偏多↔偏空、或 中性↔(偏多/偏空) 都算变化；首次运行（无基准）不算"""
    return bool(last_dir) and analysis["direction"] != last_dir


# ============================================================
# 状态持久化 + 冷却时间去重（因子是时序，非事件指纹）
# 云端（GIST_TOKEN/GIST_ID 存在时）持久化到 Gist 的 factor_state.json，
# 与 real_time_push 的 real_time_state.json 并列——云端 Actions 每次全新容器，
# 本地 logs/factor_state.json 不持久，贴水基线与 risk_state 必须存 Gist 才能跨轮积累。
# 本地模式写 logs/factor_state.json（real_time_push 联动读取）。
# ============================================================
FACTOR_STATE_FILENAME = "factor_state.json"


def _gist_load_factor(token: str, gist_id: str) -> dict:
    """从 Gist 读 factor_state.json（带时间戳防 CDN 缓存；首次运行文件不存在 → 空状态）"""
    url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "stock-news-agent-factor",
    }
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.get(url, headers=headers, timeout=20)
            resp.raise_for_status()
            files = resp.json().get("files") or {}
            fobj = files.get(FACTOR_STATE_FILENAME)
            if fobj is not None:
                return json.loads(fobj.get("content") or "{}")
        except Exception as e:
            last_error = e
            logger.warning(f"Gist 读取第{attempt + 1}次失败: {e}")
        if attempt < 2:
            # 指数退避（2s/4s）：短时 rate limit 窗口可被重试跨过，防贴水基线归零
            time.sleep(2 ** (attempt + 1))
    # 首次运行（文件尚未创建）与读取失败都返回空——factor 状态丢失只影响贴水基线
    # 积累（重新积累即可），不像推送去重基准丢失会造成重复推送，故允许空。
    if last_error:
        logger.warning(f"Gist factor_state.json 读取失败，按空状态处理: {last_error}")
    return {}


def _gist_save_factor(token: str, gist_id: str, state: dict) -> None:
    """写回 Gist factor_state.json（整文件原子替换）"""
    url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "stock-news-agent-factor",
    }
    payload = {"files": {FACTOR_STATE_FILENAME: {"content": json.dumps(state, ensure_ascii=False, indent=2)}}}
    last_error = None
    for attempt in range(3):
        try:
            resp = requests.patch(url, json=payload, headers=headers, timeout=20)
            resp.raise_for_status()
            return
        except Exception as e:
            last_error = e
            if attempt < 2:
                # 指数退避（2s/4s）：短时 rate limit 窗口可被重试跨过
                logger.warning(f"Gist 写入第{attempt + 1}次失败: {e}, {2 ** (attempt + 1)}s 后重试")
                time.sleep(2 ** (attempt + 1))
    raise RuntimeError(f"Gist factor_state.json 写入失败（已重试2次）: {last_error}")


def _load_state() -> dict:
    """加载状态：云端优先 Gist（跨容器持久），本地用文件"""
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()
    if gist_token and gist_id:
        try:
            return _gist_load_factor(gist_token, gist_id)
        except Exception as e:
            logger.warning(f"Gist 状态加载失败，降级本地: {e}")
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def _save_state(state: dict) -> None:
    """保存状态：本地文件总是写（作为日志/降级）；云端同时写 Gist"""
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()
    if gist_token and gist_id:
        try:
            _gist_save_factor(gist_token, gist_id, state)
            logger.info("因子状态已保存到 Gist（basis_history/risk_state/cooldown）")
        except Exception as e:
            # factor 状态丢失影响小（基线重新积累），本地已保存，不 fail-stop
            logger.warning(f"Gist 因子状态写入失败（本地已保存）: {e}")


def filter_by_cooldown(signals: list, state: dict) -> list:
    """冷却过滤：同一 key 在 TH_COOLDOWN_HOURS 内已告警则跳过"""
    now = time.time()
    fresh = []
    cooldown = state.setdefault("cooldown", {})
    for s in signals:
        last = cooldown.get(s["key"], 0)
        if now - last < TH_COOLDOWN_HOURS * 3600:
            continue
        fresh.append(s)
        cooldown[s["key"]] = now
    return fresh


# ============================================================
# 紧凑快照（供 real_time_push 资讯卡片"市场环境"行与盘前/盘后简报复用）
# ============================================================
def build_snapshot(tech: dict, basis: dict, fx: dict, risk_state: str,
                   stocks: dict = None, flows: dict = None, global_quotes: dict = None,
                   breadth: dict = None, vol: dict = None, style: dict = None,
                   sentiment: dict = None, sector_flows: dict = None,
                   liquidity: dict = None, option: dict = None,
                   sources: dict = None) -> dict:
    """把当轮因子结果压成紧凑 dict，随 factor_state.json 持久化（Gist/本地）

    只保留跨模块展示需要的字段；不混入 basis_history 等大体量时序。
    real_time_push 侧读取时对字段缺失全程防御（旧状态文件无 snapshot 键）。
    stocks/flows（P1-2/P1-3）、global/breadth/vol/style（P3 2026-08-19）、
    sentiment/sector_flows（P4 2026-08-19）、liquidity/option（P7 2026-08-19）、
    sources（P5-3 数据健康度）：
    各增强维度缺省不写入键（旧快照无这些键，读取方 _factor_env_line/简报均容错）。
    """
    indexes = {}
    for name, t in (tech or {}).items():
        if isinstance(t, dict) and t.get("available"):
            indexes[name] = {
                "price": round(_to_float(t.get("price")), 2),
                "change_pct": round(_to_float(t.get("change_pct")), 2),
                "trend": str(t.get("trend", "") or ""),
            }
    basis_compact = {}
    for code in ("IF", "IC", "IM", "IH"):
        b = (basis or {}).get(code)
        if isinstance(b, dict):
            basis_compact[code] = {
                "basis_pct": _to_float(b.get("basis_pct")),
                "annual_pct": _to_float(b.get("annual_pct")),
            }
    fx_compact = {}
    for sym, label in FX.items():
        f = (fx or {}).get(sym)
        if isinstance(f, dict):
            fx_compact[label] = {
                "price": round(_to_float(f.get("price")), 4),
                "change_pct": round(_to_float(f.get("change_pct")), 2),
            }
    out = {
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "risk_state": risk_state,
        "indexes": indexes,
        "basis": basis_compact,
        "fx": fx_compact,
    }
    if stocks:
        out["stocks"] = stocks
    if flows:
        out["flows"] = {
            "main_net_yi": _to_float(flows.get("main_net_yi")),
            "margin_yi": _to_float(flows.get("margin_yi")),
            "margin_chg_yi": _to_float(flows.get("margin_chg_yi")),
        }
    # P3（2026-08-19）：外盘/宽度/波动率/风格紧凑键
    if global_quotes:
        out["global"] = {
            name: {"price": round(_to_float(q.get("price")), 2),
                   "change_pct": round(_to_float(q.get("change_pct")), 2)}
            for name, q in global_quotes.items() if isinstance(q, dict)
        }
    if breadth:
        out["breadth"] = {k: breadth.get(k) for k in
                          ("adv", "dec", "down_pct", "limit_up", "limit_down", "big_down")
                          if k in breadth}
    if vol:
        vol_compact = {}
        for name, v in vol.items():
            if isinstance(v, dict) and v.get("available"):
                vol_compact[name] = {
                    "vol20": _to_float(v.get("vol20")),
                    "pctile": _to_float(v.get("pctile")),
                    "regime": str(v.get("regime", "") or ""),
                }
        if vol_compact:
            out["vol"] = vol_compact
    if style:
        out["style"] = {
            "ratio": _to_float(style.get("ratio")),
            "chg5": _to_float(style.get("chg5")),
            "chg20": _to_float(style.get("chg20")),
            "trend": str(style.get("trend", "") or ""),
        }
    # P4（2026-08-19）：涨停情绪 / 行业资金流紧凑键
    if sentiment:
        out["sentiment"] = {k: sentiment.get(k) for k in
                            ("zt", "zb", "zbr", "max_lbc", "lbc_dist", "mood")
                            if k in sentiment}
    if sector_flows:
        out["sector_flows"] = {
            "inflow": [list(x) for x in (sector_flows.get("inflow") or [])],
            "outflow": [list(x) for x in (sector_flows.get("outflow") or [])],
        }
    # P7（2026-08-19）：资金面利率 / 期权情绪紧凑键
    if liquidity:
        liq_compact = {}
        for k in ("gc007", "gc001"):
            v = liquidity.get(k)
            if isinstance(v, dict):
                liq_compact[k] = {"price": _to_float(v.get("price")),
                                  "change_pct": _to_float(v.get("change_pct"))}
        if liq_compact:
            out["liquidity"] = liq_compact
    if option and option.get("pcr") is not None:
        out["option"] = {k: option.get(k) for k in
                         ("pcr", "call_vol", "put_vol", "contracts") if k in option}
    # P5-3：数据健康度紧凑键（含 weak_direction 由 run_once 维护，不在此处）
    if isinstance(sources, dict) and sources.get("total"):
        out["sources"] = {"ok": sources.get("ok", 0), "total": sources.get("total", 0)}
    return out


def _to_float(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _load_realtime_state() -> dict:
    """读 real_time_push 状态（云端 Gist 优先，本地降级）；失败返回 {}"""
    state = {}
    gist_token = os.getenv("GIST_TOKEN", "").strip()
    gist_id = os.getenv("GIST_ID", "").strip()
    if gist_token and gist_id:
        try:
            url = f"https://api.github.com/gists/{gist_id}?ts={int(time.time() * 1000)}"
            headers = {"Authorization": f"token {gist_token}",
                       "Accept": "application/vnd.github+json",
                       "User-Agent": "stock-news-agent-factor"}
            resp = requests.get(url, headers=headers, timeout=15)
            resp.raise_for_status()
            fobj = (resp.json().get("files") or {}).get(REALTIME_STATE_FILENAME)
            if fobj is not None:
                state = json.loads(fobj.get("content") or "{}")
        except Exception as e:
            logger.debug(f"Gist 资讯状态读取失败，降级本地: {e}")
    if not state:
        try:
            state = json.loads(_REALTIME_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
    return state


def _pushed_news_rows(hours: int) -> list:
    """近 N 小时已推资讯 [(datetime, title)]，按时间倒序（未推/无标题/时间异常剔除）"""
    state = _load_realtime_state()
    now = datetime.now()
    rows = []
    for rec in (state.get("seen") or {}).values():
        if not isinstance(rec, dict) or not rec.get("pushed"):
            continue
        try:
            t = datetime.strptime(str(rec.get("t", "")), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if 0 <= (now - t).total_seconds() <= hours * 3600:
            title = str(rec.get("title", "")).strip()
            if title:
                rows.append((t, title))
    rows.sort(reverse=True)
    return rows


def _recent_pushed_titles(hours: int = 2, limit: int = 5) -> list:
    """读取 real_time_push 已推事件（近 N 小时）标题（P0 联动增强）

    因子方向翻转推送时附上，完成"因子↔资讯"双向引用——
    用户看到方向信号时能立即知道驱动它的相关事件。
    """
    return [title for _, title in _pushed_news_rows(hours)[:limit]]


def _related_pushed_news(keywords: list, hours: int = 48, limit: int = 5) -> list:
    """近 N 小时已推资讯中标题含任一关键词的相关条目（P1-2 个股异动卡片附引用）

    个股异动推送时附"近48h该股相关已推资讯"，用户看到异动即可回看事件背景
    （D2 解法：异动 + 资讯合并卡片）。无匹配返回 []。
    """
    if not keywords:
        return []
    kws = [str(k).strip() for k in keywords if str(k).strip()]
    out = []
    for _, title in _pushed_news_rows(hours):
        if any(k in title for k in kws):
            out.append(title)
            if len(out) >= limit:
                break
    return out


# ============================================================
# 格式化
# ============================================================
_RED = "#e23a3a"
_GREEN = "#2e7d32"


def format_snapshot(tech: dict, basis: dict, fx: dict, stocks: dict = None, flows: dict = None,
                    global_quotes: dict = None, breadth: dict = None, vol: dict = None,
                    style: dict = None, sentiment: dict = None,
                    sector_flows: dict = None, liquidity: dict = None,
                    option: dict = None) -> str:
    """因子快照（markdown）"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"## 量化因子快照 {now}", ""]
    # 技术面（指数级）
    lines.append("### 技术面 · 指数级")
    for name in CORE_INDEXES:
        t = tech.get(name)
        if not t or not t.get("available"):
            lines.append(f"- {name}：数据缺失")
            continue
        color = _RED if t["change_pct"] >= 0 else _GREEN
        arrow = "▲" if t["change_pct"] >= 0 else "▼"
        ma_line = f"MA5 {t['ma5']:.0f} / MA20 {t['ma20']:.0f} / MA60 {t['ma60']:.0f}"
        lines.append(
            f"- <font color=\"{color}\">{arrow} {name} {t['price']:.2f}（{t['change_pct']:+.2f}%）</font>"
            f"｜{t['trend']}｜动量 5日{t['mom5']:+.1f}% 20日{t['mom20']:+.1f}%"
        )
        lines.append(f"  > {ma_line}｜量比5日 {t['vol_ratio5']}x" + ("｜⚠️放量" if t["vol_ratio5"] >= TH_VOLUME_RATIO else ""))
    # 宏观流动性
    lines.append("")
    lines.append("### 宏观流动性")
    if basis:
        for code in ("IF", "IC", "IM", "IH"):
            b = basis.get(code)
            if not b:
                continue
            tag = "贴水" if b["basis_pct"] < 0 else "升水"
            lines.append(f"- {code}（{b['index']}）{tag} {abs(b['basis_pct'])}%（年化 {b['annual_pct']}%）｜距交割 {b['remaining_days']} 天")
    if fx:
        for sym, label in FX.items():
            f = fx.get(sym)
            if not f:
                continue
            color = _RED if f["change_pct"] >= 0 else _GREEN
            lines.append(f"- <font color=\"{color}\">{label} {f['price']:.4f}（{f['change_pct']:+.2f}%）</font>")
    # 资金面利率（P7-1）：GC007 收紧时标红，宽松区间常规展示
    if liquidity:
        gc = liquidity.get("gc007") or {}
        gc1 = liquidity.get("gc001") or {}
        if isinstance(gc.get("price"), (int, float)):
            tight = gc["price"] >= TH_GC007_TIGHT
            flag = "⚠️" if tight else ""
            chg = gc.get("change_pct")
            chg_s = f"（{chg:+.0f}%）" if isinstance(chg, (int, float)) else ""
            gc1_s = (f"｜GC001 {gc1['price']:.2f}%"
                     if isinstance(gc1.get("price"), (int, float)) else "")
            lines.append(f"- {flag}GC007 {gc['price']:.2f}%{chg_s}{gc1_s}"
                         + ("｜资金面收紧" if tight else ""))
    # 期权情绪（P7-2）：PCR 温度计
    if option and option.get("pcr") is not None:
        pcr = option["pcr"]
        flag = "⚠️" if pcr >= TH_PCR_PANIC else ""
        lines.append(f"- {flag}期权 PCR {pcr:.2f}（认购 {option.get('call_vol', 0)} 张"
                     f" / 认沽 {option.get('put_vol', 0)} 张）"
                     + ("｜恐慌对冲占优" if pcr >= TH_PCR_PANIC else
                        "｜看涨占优" if pcr <= TH_PCR_GREED else ""))
    # 隔夜外盘（P3-1）
    if global_quotes:
        lines.append("")
        lines.append("### 隔夜外盘")
        for name, q in global_quotes.items():
            if not isinstance(q, dict):
                continue
            chg = q.get("change_pct", 0)
            color = _RED if chg >= 0 else _GREEN
            arrow = "▲" if chg >= 0 else "▼"
            flag = "｜⚠️异动" if abs(chg) >= TH_GLOBAL_PCT else ""
            lines.append(f"- <font color=\"{color}\">{arrow} {name} {q['price']:.2f}（{chg:+.2f}%）</font>{flag}")
    # 宽度与波动率（P3-2/P3-3）
    if breadth or vol:
        lines.append("")
        lines.append("### 宽度与波动率")
        if breadth:
            dp = breadth.get("down_pct")
            if isinstance(dp, (int, float)):
                lines.append(f"- 涨跌家数：涨 {breadth.get('adv', 0)} / 跌 {breadth.get('dec', 0)}"
                             f"（{dp:.0f}% 下跌）｜涨停 {breadth.get('limit_up', 0)} / 跌停 {breadth.get('limit_down', 0)}"
                             f"｜跌超5% {breadth.get('big_down', 0)}")
        for name, v in (vol or {}).items():
            if not isinstance(v, dict) or not v.get("available"):
                continue
            tag = "⚠️高波" if v.get("regime") == "高波" else v.get("regime", "")
            lines.append(f"- {name} 波动率：20日年化 {v.get('vol20', 0):.1f}%"
                         f"（近一年 {v.get('pctile', 0):.0f} 分位，{tag}）")
    # 风格轮动（P3-4）
    if style:
        lines.append(f"- 风格轮动：{style.get('trend', '')}"
                     f"（50/1000 比价 20日 {style.get('chg20', 0):+.1f}%，5日 {style.get('chg5', 0):+.1f}%）")
    # 涨停情绪（P4-2）：短线情绪周期温度计
    if sentiment and sentiment.get("zt"):
        mood = sentiment.get("mood", "")
        flag = "🔥" if mood == "亢奋" else ("❄️" if mood == "冰点" else "")
        lines.append(f"- 涨停情绪：{flag}{mood}｜涨停 {sentiment.get('zt', 0)} 家"
                     f"（连板高度 {sentiment.get('max_lbc', 0)}，梯队 {sentiment.get('lbc_dist', '')}）"
                     f"｜炸板率 {sentiment.get('zbr', 0):.0f}%（炸板 {sentiment.get('zb', 0)} 家）")
    # 资金面（P1-3）
    if flows:
        lines.append("")
        lines.append("### 资金面")
        mn = flows.get("main_net_yi")
        if isinstance(mn, (int, float)):
            color = _RED if mn >= 0 else _GREEN
            lines.append(f"- <font color=\"{color}\">两市主力净{'流入' if mn >= 0 else '流出'} {abs(mn):.0f} 亿</font>")
        my = flows.get("margin_yi")
        mc = flows.get("margin_chg_yi")
        if isinstance(my, (int, float)):
            extra = (f"，较前日{'+' if mc >= 0 else ''}{mc:.0f} 亿"
                     if isinstance(mc, (int, float)) else "")
            lines.append(f"- 融资余额 {my:.0f} 亿{extra}（T-1）")
    # 行业资金流 TOP（P4-3）：独立于 flows 块（两数据源独立失败时仍可展示）
    if sector_flows:
        if not flows:
            lines.append("")
            lines.append("### 资金面")
        in_s = " / ".join(f"{n} {v:+.1f}亿" for n, v in (sector_flows.get("inflow") or []))
        out_s = " / ".join(f"{n} {v:+.1f}亿" for n, v in (sector_flows.get("outflow") or []))
        if in_s:
            lines.append(f"- 行业主力净流入 TOP：{in_s}")
        if out_s:
            lines.append(f"  > 净流出 TOP：{out_s}")
    # 自选股（P1-2）
    if stocks:
        lines.append("")
        lines.append("### 自选股")
        for name, t in stocks.items():
            if not isinstance(t, dict) or not t.get("available"):
                continue
            color = _RED if t["change_pct"] >= 0 else _GREEN
            arrow = "▲" if t["change_pct"] >= 0 else "▼"
            code = str(t.get("code", "") or "")
            label = f"{name}({code})" if code else name
            lines.append(
                f"- <font color=\"{color}\">{arrow} {label} {t['price']:.2f}"
                f"（{t['change_pct']:+.2f}%）</font>｜{t['trend']}｜量比 {t['vol_ratio5']}x"
            )
    return "\n".join(lines)


def format_alert(signal: dict) -> str:
    """单条异动告警"""
    icon = "▲" if signal["direction"] == "bullish" else "▼"
    color = _RED if signal["direction"] == "bullish" else _GREEN
    tag = "强利好" if signal["direction"] == "bullish" else "强利空"
    return f"<font color=\"{color}\">{icon} {tag}</font> **{signal['title']}**\n> {signal['detail']}"


_DIR_LABEL = {"偏多": "利好", "偏空": "利空", "中性": "中性"}


def format_direction_signal(analysis: dict, last_dir: str = "", winrate: dict = None,
                            sources: dict = None) -> str:
    """量化方向信号推送正文：综合方向（利好/利空/中性）+ 各因子明细 + 历史可信度

    用户口径（2026-08-14）：只用"利好/利空"表述，不写"买卖方向"。
    winrate（P4-1 2026-08-19）：近30天已推事件方向一致率（signal_backtest.compute_winrate），
    人工决策时据此给信号定权重；样本 <10 条不展示（防小样本误导）。
    sources（P5-3 2026-08-19）：数据健康度 {"ok", "total"}，成功率 <70% 时附警示——
    归因的最后一环：人工决策时知道本信号缺了哪些数据。
    """
    direction = analysis["direction"]
    score = analysis["score"]
    display = _DIR_LABEL.get(direction, direction)
    if direction == "偏多":
        color, arrow = _RED, "▲"
    elif direction == "偏空":
        color, arrow = _GREEN, "▼"
    else:
        color, arrow = "#888888", "◆"
    suffix = ""
    if last_dir and last_dir != direction:
        suffix = f"（前值 {_DIR_LABEL.get(last_dir, last_dir)}）"
    if analysis.get("escalated"):
        suffix += "（确信度升级）"
    # P4-6：IC 加权状态标注（历史≥20交易日自动启用）
    weights = analysis.get("weights") or {}
    ic_n = analysis.get("ic_n")
    if weights and isinstance(ic_n, int) and ic_n > 0:
        suffix += f"（IC加权 n={ic_n}）"
    # P5-2：确信度强度标注（等权下 4/6 维同向）
    if abs(score) >= STRONG_DIR_THRESHOLD:
        suffix += "（强信号）"
    lines = [
        f"<font color=\"{color}\">{arrow} 量化方向：{display}（{score:+.2f}）</font>{suffix}",
        "",
        "因子明细：",
    ]
    # P5-1：生效权重（含门控倍数）优先于 IC 基线权重展示——共振归因
    # P8：影子维度仅展示非零（有信号的）——因子池扩到 15 维后，零分影子逐行
    # 展示会让卡片膨胀到 17 行；零分维度对决策无增量信息（分值仍全量进
    # direction_history 供 IC 回测，展示与记录解耦）。
    eff_w = analysis.get("eff_weights") or {}
    shadow = analysis.get("shadow") or set()
    # 全量影子名（含零分）——零分影子隐藏判定；兼容无 shadow_all 的旧快照
    shadow_all = analysis.get("shadow_all") or shadow
    for name, s, desc in analysis.get("factors", []):
        if name in shadow_all and s == 0:
            continue
        if s > 0:
            mark = "▲ 利好"
        elif s < 0:
            mark = "▼ 利空"
        else:
            mark = "— 中性"
        if name in shadow:
            tag = "（影子·未参与合成）"
            w_tag = ""
        else:
            tag = ""
            w_show = eff_w.get(name, weights.get(name))
            w_tag = f"（权重{w_show:.1f}）" if isinstance(w_show, (int, float)) else ""
        lines.append(f"- {mark}｜{name}{tag}{w_tag}：{desc}")
    # P5-1：门控共振归因（非线性升权的显式说明）
    gates = analysis.get("gates") or []
    if gates:
        lines.append("")
        lines.append("共振门控：" + "；".join(gates))
    # P5-3：数据健康度警示（源成功率 <70%）
    if isinstance(sources, dict) and sources.get("total"):
        ratio = sources["ok"] / sources["total"]
        if ratio < HEALTH_WARN_RATIO:
            lines.append("")
            lines.append(f"⚠️ 数据健康度 {sources['ok']}/{sources['total']}（低于"
                         f"{HEALTH_WARN_RATIO:.0%}），本信号基于部分数据")
    # P4-1：信号可信度标注（近30天已推事件方向一致率，n≥10 才展示）
    if isinstance(winrate, dict) and winrate.get("n", 0) >= 10:
        wr_parts = []
        for n in (1, 3, 5):
            v = winrate.get(f"hit_{n}")
            if isinstance(v, (int, float)):
                wr_parts.append(f"后{n}日 {v:.0f}%")
        if wr_parts:
            lines.append("")
            lines.append(f"信号可信度（近30天已推事件一致率，n={winrate['n']}）：" + "｜".join(wr_parts))
    return "\n".join(lines)


# ============================================================
# 推送出口（复用 push.py）
# ============================================================
def do_push(title: str, content: str) -> dict:
    pushplus_token = os.getenv("PUSHPLUS_TOKEN", "").strip()
    wecom_webhook = os.getenv("WECOM_WEBHOOK", "").strip()
    if pushplus_token:
        return push_via_pushplus(pushplus_token, title, content)
    if wecom_webhook:
        return push_via_wecom(wecom_webhook, title, content)
    logger.error("未配置推送后端（PUSHPLUS_TOKEN 或 WECOM_WEBHOOK）")
    return {"code": 400, "msg": "未配置推送后端"}


# ============================================================
# 主流程
# ============================================================
def run_once(push: bool, collect: bool = False) -> dict:
    quotes = fetch_index_quotes()
    fx = fetch_fx()
    futures = fetch_index_futures()

    tech = {}
    vol = {}
    sh_kline = []  # 上证日K（P4-6：IC 计算复用，免二次请求）
    for name in CORE_INDEXES:
        # P3-3：K 线窗口 65→260（近一年），技术面因子与波动率分位共用同一段数据
        kline = fetch_index_kline(INDEXES[name]["sina"], VOL_KLINE_DAYS)
        tech[name] = calc_tech_factors(name, kline, quotes.get(name, {}))
        vol[name] = calc_vol_regime(kline)
        if name == "上证指数":
            sh_kline = kline

    basis = calc_basis(futures, quotes)

    # P1-2（2026-08-19）：自选股监控（watchlist 带代码条目，空名单静默跳过）
    stock_signals, stocks_tech, stocks_snap = monitor_stocks()
    # P1-3（2026-08-19）：资金流（两市主力净流入 + 融资余额变化，任一源失败缺字段）
    flows = fetch_market_flows()
    # P3（2026-08-19）：隔夜外盘 / 市场宽度 / 风格轮动（均增强维度，失败缺省不 fail-stop）
    global_quotes = fetch_global_quotes()
    breadth = fetch_market_breadth()
    style = calc_style_rotation()
    # P4（2026-08-19）：涨停情绪温度计 / 行业资金流 TOP
    sentiment = fetch_zt_sentiment()
    sector_flows = fetch_sector_flows()
    # P7（2026-08-19）：资金面利率（GC007）/ 期权成交量 PCR（影子因子：展示+记录，
    # 不参与方向合成，IC 回测达标后升级正式维度）
    liquidity = fetch_liquidity()
    option = fetch_option_pcr()
    # P8（2026-08-19）：因子池扩展——日线衍生因子（复用上证日K零请求）+
    # 分钟级因子（m5×48 当日全量，腾讯 ifzq 源）。均为影子维度。
    daily_factors = calc_daily_derived_factors(sh_kline)
    minute = fetch_minute_kline(INDEXES["上证指数"].get("tencent") or "sh000001", "m5", 48)
    minute_factors = calc_minute_factors(minute)

    # P5-3（2026-08-19）：数据健康度——各源成功与否（空结果=源失败/无数据）。
    # 机构级清洗的标志：不只拿数据，还知道自己在用什么、缺了什么；
    # 成功率 <70% 时方向信号附警示（自选股不纳入：空名单≠源失败）
    sources_ok = {
        "指数行情": bool(quotes), "股指期货": bool(futures), "汇率": bool(fx),
        "指数K线": bool(sh_kline), "资金流": bool(flows), "隔夜外盘": bool(global_quotes),
        "市场宽度": bool(breadth),
        "波动率": any(isinstance(v, dict) and v.get("available") for v in vol.values()),
        "风格轮动": bool(style), "涨停情绪": bool(sentiment), "行业资金流": bool(sector_flows),
        "资金面利率": bool(liquidity), "期权PCR": bool(option),
        "分钟K线": bool(minute),
    }
    health = {"ok": sum(sources_ok.values()), "total": len(sources_ok)}
    health["ratio"] = round(health["ok"] / health["total"], 2) if health["total"] else 0.0
    failed = [k for k, v in sources_ok.items() if not v]
    print(f"[数据健康度] {health['ok']}/{health['total']}"
          + (f"（缺：{'、'.join(failed)}）" if failed else ""))

    state = _load_state()
    signals, new_history = detect_anomalies(tech, basis, fx, state.get("basis_history"))
    # P3：外盘/宽度为市场级风险（隔夜暴跌3%+、跌停潮），纳入 risk_off 口径；
    # P4：炸板潮（炸板率≥50%）同属市场级风险，并入 risk_off；
    # P7：资金面收紧（GC007≥3.5% 或急升）同属市场级风险，并入 risk_off；
    # 资金流异动/期权恐慌维持只并入告警推送不切 risk_off（P1-3/P7 口径不变）
    signals = (signals + detect_global_anomalies(global_quotes)
               + detect_breadth_anomalies(breadth) + detect_sentiment_anomalies(sentiment)
               + detect_liquidity_anomalies(liquidity))
    risk_state = calc_risk_state(signals)
    signals = signals + detect_flow_anomalies(flows) + detect_option_anomalies(option)
    fresh = filter_by_cooldown(signals, state) if push else signals

    snapshot = format_snapshot(tech, basis, fx, stocks=stocks_tech, flows=flows,
                               global_quotes=global_quotes, breadth=breadth, vol=vol, style=style,
                               sentiment=sentiment, sector_flows=sector_flows,
                               liquidity=liquidity, option=option)
    print(snapshot)

    # 异动检测结果（dry-run 也展示，但不推送、不更新状态）
    if signals:
        print("\n[异动检测]")
        for s in signals:
            print(f"  - {s['title']}｜{s['detail']}")
    else:
        print("\n[异动检测] 无")
    if stock_signals:
        print("\n[自选股异动]")
        for s in stock_signals:
            print(f"  - {s['title']}｜{s['detail']}")
    print(f"[风险状态] {risk_state}")

    pushed = []
    # 状态落盘放宽为 persist（push 或 collect）：修复 2026-08-22 停推回归——
    # 原 `if push:` 门控把 build_snapshot/_save_state 一并关进推送分支，云端 --dry-run
    # 空跑不写，快照 4 天停更，择时修正层（贴水/资金/情绪）持续用旧数据。
    # collect=只采集写快照/状态不推送；dry-run=纯只读维持不变。
    persist = push or collect
    if persist:
        state["basis_history"] = new_history
        state["risk_state"] = risk_state
        # 紧凑快照（P0-1 2026-08-19）：资讯卡片"市场环境"行 + 盘前/盘后简报的数据源。
        # 每轮覆盖写，保持最新；factor_collector 盘中 15 分钟/盘后 60 分钟一轮，
        # 快照时效由读取方（real_time_push）按 ts 判断过期。
        state["snapshot"] = build_snapshot(tech, basis, fx, risk_state,
                                           stocks=stocks_snap, flows=flows,
                                           global_quotes=global_quotes, breadth=breadth,
                                           vol=vol, style=style,
                                           sentiment=sentiment, sector_flows=sector_flows,
                                           liquidity=liquidity, option=option,
                                           sources=health)
    if push:
        if fresh:
            content = "## 量化因子异动告警\n\n" + "\n\n".join(format_alert(s) for s in fresh)
            r = do_push("量化因子异动", content)
            pushed = [s["title"] for s in fresh]
            print(f"\n[推送] {len(fresh)} 条异动：{pushed} → code={r.get('code', r.get('errcode'))}")
        else:
            print("\n[推送] 本轮无异动（或均在冷却期）")
        # P1-2：自选股异动单独成卡——每股附近48h相关已推资讯（D2 合并卡片），
        # 冷却 key 独立于指数告警（stock_chg_代码 等），同一股票 6h 内不重推。
        stock_fresh = filter_by_cooldown(stock_signals, state)
        if stock_fresh:
            parts = []
            for s in stock_fresh:
                block = format_alert(s)
                related = _related_pushed_news([s.get("stock", ""), s.get("code", "")])
                if related:
                    block += "\n近48h相关已推资讯：\n" + "\n".join(f"- {t}" for t in related)
                parts.append(block)
            content3 = "## 自选股异动\n\n" + "\n\n".join(parts)
            r3 = do_push("自选股异动", content3)
            pushed += [s["title"] for s in stock_fresh]
            print(f"\n[推送] 自选股异动 {len(stock_fresh)} 条 → code={r3.get('code', r3.get('errcode'))}")
        # 量化方向信号（核心需求"利好买、利空卖，和量化同步"）：多因子合成方向
        # （偏多=利好/偏空=利空/中性），方向改变时推送"量化方向信号"（含各因子利好/利空明细）。
        # 独立冷却（change_cooldown）防盘中方向抖动反复推。
        # P4-6：IC 加权——用已积累的 direction_history（≥20个交易日）回测各维度 IC，
        # 复用本轮上证 K 线算次日收益（零额外请求）；失败/样本不足等权回退
        factor_ic = {}
        try:
            import signal_backtest as sb
            factor_ic = sb.compute_factor_ic(state.get("direction_history") or {},
                                             index_closes=sh_kline)
        except Exception as e:
            logger.debug(f"因子IC计算失败，等权回退: {e}")
        if factor_ic:
            state["factor_ic"] = factor_ic
        analysis = _direction_analysis(tech, basis, fx, risk_state, new_history,
                                       vol=vol, breadth=breadth,
                                       weights=(factor_ic or {}).get("weights") or {},
                                       liquidity=liquidity, option=option,
                                       daily_factors=daily_factors,
                                       minute_factors=minute_factors,
                                       global_quotes=global_quotes)
        if factor_ic.get("weights"):
            analysis["ic_n"] = factor_ic.get("n")
        # P4-6：方向历史落盘（每交易日一条，当日盘中多轮覆盖取最新），供后续 IC 回测；
        # 保留最近 120 个交易日（约半年，防 Gist 状态膨胀）
        day = datetime.now().strftime("%Y-%m-%d")
        dhist = state.setdefault("direction_history", {})
        dhist[day] = {
            "dir": analysis["direction"], "score": analysis["score"],
            "factors": {name: s for name, s, _ in analysis["factors"]},
        }
        if len(dhist) > 120:
            for k in sorted(dhist)[: len(dhist) - 120]:
                del dhist[k]
        last_dir = state.get("last_direction")
        # P5-2：确信度分层——方向变化仅是触发条件，|score|≥0.67（4/6 维同向）才单独推送；
        # 弱翻转（0.5~0.67）记入 weak_direction 只进盘前/盘后简报，不单独打扰。
        # 弱→强同向增强视为"确信度升级"，同样推送（防弱翻转静默后漏报强信号）
        changed = _direction_changed(analysis, last_dir)
        prev_weak = state.get("weak_direction") or {}
        escalated = (not changed and prev_weak.get("dir") == analysis["direction"]
                     and abs(analysis["score"]) >= STRONG_DIR_THRESHOLD)
        if changed or escalated:
            if abs(analysis["score"]) >= STRONG_DIR_THRESHOLD:
                now_ts = time.time()
                change_cooldown = state.setdefault("change_cooldown", {})
                if now_ts - change_cooldown.get("direction", 0) >= STATE_CHANGE_COOLDOWN_HOURS * 3600:
                    if escalated:
                        analysis["escalated"] = True
                    # P4-1：附近30天已推事件方向一致率（signal_backtest），
                    # 失败（无状态/无行情）静默降级为无标注，不影响方向推送
                    winrate = {}
                    try:
                        import signal_backtest as sb
                        winrate = sb.compute_winrate(days=30)
                    except Exception as e:
                        logger.debug(f"信号胜率计算失败，跳过标注: {e}")
                    content = "## 量化方向信号\n\n" + format_direction_signal(
                        analysis, last_dir, winrate=winrate, sources=health)
                    # P0 联动增强：附最近已推资讯标题，方向信号与资讯事件互为印证
                    related = _recent_pushed_titles()
                    if related:
                        content += "\n\n近2小时已推资讯：\n" + "\n".join(f"- {t}" for t in related)
                    r4 = do_push("量化方向信号", content)
                    print(f"\n[方向] {analysis['direction']}（{analysis['score']:+.2f}，强信号）"
                          f" → code={r4.get('code', r4.get('errcode'))}")
                    change_cooldown["direction"] = now_ts
                    state.pop("weak_direction", None)
            else:
                state["weak_direction"] = {
                    "dir": analysis["direction"], "score": analysis["score"],
                    "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
                }
                print(f"\n[方向] {analysis['direction']}（{analysis['score']:+.2f}）弱翻转"
                      f"（<{STRONG_DIR_THRESHOLD}），不单独推送，进简报")
        state["last_direction"] = analysis["direction"]
    if persist:
        _save_state(state)

    return {"tech": tech, "basis": basis, "fx": fx, "signals": signals, "pushed": pushed}


def _is_trading_time(now: datetime = None) -> bool:
    """A股交易时段：周一至周五 9:30-11:30 / 13:00-15:00（不含节假日，第一版用 weekday 近似）

    实时因子（基差/汇率/量比）在盘中分分钟变化，交易时段必须高频轮询；
    非交易时段因子静止，降频省资源。
    """
    now = now or datetime.now()
    if now.weekday() >= 5:
        return False
    hm = now.hour * 60 + now.minute
    return (9 * 60 + 30 <= hm <= 11 * 60 + 30) or (13 * 60 <= hm <= 15 * 60)


def main():
    parser = argparse.ArgumentParser(description="量化因子采集器")
    parser.add_argument("--dry-run", action="store_true", help="只采集+计算+打印，不推送")
    parser.add_argument("--push", action="store_true", help="打印快照；有异动且过冷却则推送")
    parser.add_argument("--collect", action="store_true",
                        help="只采集+写快照/状态，不推送（云端择时修正层数据源用）")
    parser.add_argument("--loop", action="store_true", help="常驻轮询")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.loop:
        # 动态轮询：交易时段高频（默认 5 分钟），非交易时段低频（默认 30 分钟）。
        # 实时因子（基差/汇率/量比）盘中分分钟变化，8-13 日元急升引发跳水即 30 分钟内事件，
        # 30 分钟一轮会滞后错过；盘后因子静止，降频省资源。
        active_poll = max(30, int(os.getenv("RT_POLL_SECONDS", "300")))
        idle_poll = max(300, int(os.getenv("RT_POLL_IDLE_SECONDS", "1800")))
        logger.info(f"因子采集器常驻运行：交易时段每 {active_poll}s 一轮，非交易时段每 {idle_poll}s 一轮")
        while True:
            try:
                run_once(push=True)
            except Exception as e:
                logger.error(f"轮询异常: {e}")
            trading = _is_trading_time()
            poll = active_poll if trading else idle_poll
            logger.info(f"下一轮 {poll}s 后（{'交易时段' if trading else '非交易时段'}）")
            time.sleep(poll)
    else:
        run_once(push=args.push, collect=args.collect)


if __name__ == "__main__":
    main()
