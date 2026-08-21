"""查询中信期货在中金所股指期货(IF/IH/IC/IM)每日净持仓变化量(每天加的净多/净空手数)。

数据源: 中金所官网持仓排名 XML
  http://www.cffex.com.cn/sj/ccpm/{YYYYMM}/{DD}/{product}.xml
datatypeid: 0=成交量, 1=持买单量(多单), 2=持卖单量(空单)  [官方 ccpm.js 确认]

口径: 全合约汇总 —— 每个品种所有挂牌合约的中信期货多单/空单加总，
      净增减 = 多单增减合计 - 空单增减合计，再跨 IF/IH/IC/IM 四品种求和。
      净增减 > 0 = 当天净加多单; 净增减 < 0 = 当天净加空单。
"""
import requests
import xml.etree.ElementTree as ET
from datetime import date, timedelta
import sys

BASE = "http://www.cffex.com.cn/sj/ccpm/{ym}/{d}/{product}.xml"
PRODUCTS = ["IF", "IH", "IC", "IM"]
MEMBER = "中信期货"


def fetch(product, d):
    url = BASE.format(ym=d.strftime("%Y%m"), d=d.strftime("%d"), product=product)
    try:
        r = requests.get(url, timeout=15)
    except Exception:
        return None
    if r.status_code != 200 or len(r.text) < 500:
        return None
    return ET.fromstring(r.text)


def parse(root):
    rows = []
    for data in root.findall("data"):
        rows.append(
            {
                "contract": data.find("instrumentid").text,
                "dtype": data.find("datatypeid").text,
                "name": data.find("shortname").text,
                "vol": int(data.find("volume").text),
                "var": int(data.find("varvolume").text),
            }
        )
    return rows


def trading_days(end, n):
    days = []
    d = end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days


def main():
    # 默认从今天起向前回溯（此前硬编码 2026-08-21 过期后近 N 日数据会缺失）
    end = date.today()
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    days = trading_days(end, n)

    # 每个交易日: {product: {buy_var, sell_var, net_var}}
    daily = {}
    for p in PRODUCTS:
        for d in days:
            root = fetch(p, d)
            if root is None:
                continue
            rows = parse(root)
            buy_var = sell_var = 0
            for r in rows:
                if r["name"].startswith(MEMBER):
                    if r["dtype"] == "1":
                        buy_var += r["var"]
                    elif r["dtype"] == "2":
                        sell_var += r["var"]
            day = d.strftime("%m-%d")
            daily.setdefault(day, {})[p] = {
                "buy_var": buy_var,
                "sell_var": sell_var,
                "net_var": buy_var - sell_var,
            }

    print("=== 中信期货 每日净持仓变化量(手) 全合约口径 ===  (正=净加多单, 负=净加空单)")
    print(f"{'日期':<6}{'IF净增减':>9}{'IH净增减':>9}{'IC净增减':>9}{'IM净增减':>9}{'四品种合计':>10}")
    for day in sorted(daily, reverse=True):
        cells = daily[day]
        total = sum(c["net_var"] for c in cells.values())
        row = [day]
        for p in PRODUCTS:
            c = cells.get(p)
            row.append(f"{c['net_var']:+d}" if c else "--")
        row.append(f"{total:+d}")
        print(f"{row[0]:<6}{row[1]:>9}{row[2]:>9}{row[3]:>9}{row[4]:>9}{row[5]:>10}")

    print("\n=== 明细: 每日多单增减 / 空单增减(手) ===")
    for day in sorted(daily, reverse=True):
        cells = daily[day]
        b = sum(c["buy_var"] for c in cells.values())
        s = sum(c["sell_var"] for c in cells.values())
        print(f"{day}: 多单增减 {b:+d} | 空单增减 {s:+d} | 净增减 {b-s:+d}")


if __name__ == "__main__":
    main()
