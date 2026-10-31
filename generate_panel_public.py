#!/usr/bin/env python3
# Kindle 信息面板 · 公网版 (只放公共数据, 无日程/待办)
# 数据: Open-Meteo 天气 / RSS 头条 / 腾讯行情日K / 每日一句
# 输出: docs/panel.png (供 GitHub Pages 托管)
import datetime
import json
import math
import os
import re
import urllib.request
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw, ImageFont

W, H = 1264, 1680
MARGIN = 64
WHITE, BLACK, GRAY, LIGHT = 255, 0, 136, 180
HERE = os.path.dirname(os.path.abspath(__file__))


def load_font(size):
    candidates = [
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/Library/Fonts/Arial Unicode.ttf",
    ]
    for p in candidates:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except Exception:
                continue
    return ImageFont.load_default()


F = load_font


def http_get(url, timeout=10, encoding=None):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 kindle-panel"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except Exception:
        # 本机 Python 3.7 的 SSL 对部分站点握手失败(EOF in violation of protocol),
        # 退到 curl(用系统 LibreSSL)兜底。CI 上 urllib 正常, 走不到这里。
        import subprocess
        proc = subprocess.run(
            ["curl", "-sS", "--noproxy", "*", "--max-time", str(timeout), url],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            raise
        raw = proc.stdout
    return raw.decode(encoding) if encoding else raw


def get_weather():
    try:
        url = ("https://api.open-meteo.com/v1/forecast?latitude=31.23&longitude=121.47"
               "&current=temperature_2m,weather_code,relative_humidity_2m"
               "&daily=temperature_2m_max,temperature_2m_min"
               "&timezone=Asia%2FShanghai&forecast_days=1")
        d = json.loads(http_get(url))
        cur = d["current"]
        code = int(cur["weather_code"])
        desc = {0: "晴", 1: "晴间多云", 2: "局部多云", 3: "阴", 45: "雾", 48: "雾凇",
                51: "毛毛雨", 53: "小雨", 55: "中雨", 61: "小雨", 63: "中雨", 65: "大雨",
                71: "小雪", 73: "中雪", 75: "大雪", 80: "阵雨", 81: "阵雨", 82: "强阵雨",
                95: "雷阵雨", 96: "雷阵雨伴雹", 99: "强雷暴"}.get(code, "未知")
        daily = d.get("daily", {})
        return {
            "temp": round(cur["temperature_2m"]),
            "desc": desc,
            "humidity": cur.get("relative_humidity_2m"),
            "tmax": round(daily["temperature_2m_max"][0]) if daily.get("temperature_2m_max") else None,
            "tmin": round(daily["temperature_2m_min"][0]) if daily.get("temperature_2m_min") else None,
            "ok": True,
        }
    except Exception:
        return {"temp": 21, "desc": "多云", "humidity": 65, "tmax": 25, "tmin": 16, "ok": False}


# ---------------------------------------------------------------- 双周K线

KLINE_SPECS = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("usIXIC",   "纳斯达克"),
    ("sh600036", "招商银行"),
]
BAR_GROUP = 10        # 每根蜡烛 = 10 个交易日 ≈ 两周
BAR_COUNT = 8         # 一共 8 根 ≈ 16 周 ≈ 4 个月
FETCH_DAYS = 120      # 多拉一些余量, 免得节假日把分组截短


def fetch_kline(code):
    """返回 [(date, open, close, high, low), ...], 按时间正序。"""
    host = "usfqkline" if code.startswith("us") else "fqkline"
    url = ("https://web.ifzq.gtimg.cn/appstock/app/%s/get?param=%s,day,,,%d,qfq"
           % (host, code, FETCH_DAYS))
    d = json.loads(http_get(url, timeout=15))
    node = d["data"][code]
    rows = None
    for key in ("qfqday", "day", "hfqday"):
        if isinstance(node.get(key), list) and node[key]:
            rows = node[key]
            break
    if not rows:
        return []
    bars = []
    for r in rows:
        try:
            # 腾讯格式: [日期, 开, 收, 高, 低, 成交量]
            bars.append((str(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4])))
        except (ValueError, IndexError, TypeError):
            continue
    return bars


def _week_index(dstr):
    """把 'YYYY-MM-DD' 折算成连续周序号, 跨年也单调递增。"""
    d = datetime.date(int(dstr[0:4]), int(dstr[5:7]), int(dstr[8:10]))
    iso = d.isocalendar()
    return iso[0] * 53 + iso[1]


def aggregate(bars, weeks=2, count=BAR_COUNT):
    """按日历周分组（每 weeks 周一根蜡烛）, 这样 A 股和美股的横轴时间完全对齐;
    若按交易日计数分组, 两个市场节假日不同, 起点会错开几天, 日期刻度就对不上。"""
    groups = {}
    for b in bars:
        groups.setdefault(_week_index(b[0]) // weeks, []).append(b)
    keys = sorted(groups)[-count:]
    out = []
    for k in keys:
        chunk = sorted(groups[k], key=lambda x: x[0])
        o = chunk[0][1]
        c = chunk[-1][2]
        h = max(x[3] for x in chunk)
        l = min(x[4] for x in chunk)
        out.append((chunk[-1][0], o, c, h, l))
    return out


def _fake_candles(seed):
    candles = []
    op = 100.0
    for i in range(BAR_COUNT):
        cl = op * (1 + math.sin((i + seed) * 0.9) * 0.05)
        candles.append(("", op, cl, max(op, cl) * 1.03, min(op, cl) * 0.97))
        op = cl
    return candles


def get_klines():
    out = []
    for i, (code, name) in enumerate(KLINE_SPECS):
        try:
            bars = fetch_kline(code)
        except Exception:
            bars = []
        candles = aggregate(bars) if bars else []
        ok = len(candles) >= 2
        if not ok:
            candles = _fake_candles(i * 3)
        # 涨跌幅用最近一个交易日(而不是双周), 与日常看盘口径一致;
        # 最后一根蜡烛是"进行中"的周期, 用它算涨跌会前后不可比。
        chg = 0.0
        if len(bars) >= 2 and bars[-2][2]:
            chg = (bars[-1][2] - bars[-2][2]) / bars[-2][2] * 100.0
        out.append((name, candles, chg, ok))
    return out


def draw_candles(draw, box, candles):
    """双周蜡烛图: 每根覆盖两周的开高低收, 实体和影线都立得起来。"""
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    if not candles or x1 <= x0 or y1 <= y0:
        return
    hi = max(c[3] for c in candles)
    lo = min(c[4] for c in candles)
    if hi <= lo:
        hi, lo = hi + 1.0, lo - 1.0
    pad = (hi - lo) * 0.10
    hi += pad
    lo -= pad
    span = hi - lo
    hgt = y1 - y0
    n = len(candles)
    step = (x1 - x0) / float(n)
    bw = max(8, int(step * 0.36))
    half = bw // 2
    centers = [int(round(x0 + step * (i + 0.5))) for i in range(n)]

    def yv(v):
        return int(round(y1 - (v - lo) / span * hgt))

    for i, (_d, op, cl, hh, ll) in enumerate(candles):
        cx = centers[i]
        # 影线: 贯穿最高到最低
        draw.line([(cx, yv(hh)), (cx, yv(ll))], fill=BLACK, width=3)
        top, btm = yv(max(op, cl)), yv(min(op, cl))
        if btm - top < 4:
            mid = (top + btm) // 2
            top, btm = mid - 2, mid + 2
        if cl >= op:
            draw.rectangle([cx - half, top, cx + half, btm], fill=BLACK)
        else:
            draw.rectangle([cx - half, top, cx + half, btm], fill=WHITE, outline=BLACK, width=3)
    return centers


# ---------------------------------------------------------------- 其他数据

FEEDS = [
    "https://sspai.com/feed",
    "https://www.ithome.com/rss/",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]


def get_headlines(limit=5):
    for url in FEEDS:
        try:
            root = ET.fromstring(http_get(url, timeout=12))
            titles = []
            for item in root.iter("item"):
                t = item.findtext("title")
                if t:
                    titles.append(re.sub(r"\s+", " ", t).strip())
                if len(titles) >= limit:
                    break
            if titles:
                return titles
        except Exception:
            continue
    return ["（暂无网络, 显示占位新闻标题）", "第二条示例标题", "第三条示例标题"]


def get_quote():
    quotes = ["先原理，后实践；慢就是快。", "把复杂留给自己，把简单留给用户。",
              "今天多学一点，明天少慌一点。"]
    return quotes[datetime.date.today().toordinal() % len(quotes)]


def tbox(draw, box, s, font, fill=BLACK, anchor="la"):
    """按字符墨迹边界对齐(而非字符串原点), 否则不同字号的右边界会差好几像素。"""
    x0, y0, x1, y1 = box
    l, t, r, b = draw.textbbox((0, 0), s, font=font)
    if anchor == "la":
        px, py = x0 - l, y0
    elif anchor == "ma":
        px, py = (x0 + x1) / 2 - (l + r) / 2, (y0 + y1) / 2 - (t + b) / 2
    elif anchor == "ra":
        px, py = x1 - r, y0
    else:
        px, py = x0, y0
    draw.text((px, py), s, font=font, fill=fill)


def section(draw, title, box):
    x0, y0, x1, y1 = box
    draw.rectangle([x0, y0, x1, y1], outline=BLACK, width=3)
    draw.rectangle([x0, y0, x1, y0 + 52], fill=BLACK)
    tbox(draw, (x0 + 16, y0, x1 - 16, y0 + 52), title, F(30), fill=WHITE, anchor="la")


def ellipsize(draw, s, font, max_w):
    if draw.textbbox((0, 0), s, font=font)[2] <= max_w:
        return s
    while s and draw.textbbox((0, 0), s + "…", font=font)[2] > max_w:
        s = s[:-1]
    return s + "…"


def build(out_path):
    img = Image.new("L", (W, H), WHITE)
    draw = ImageDraw.Draw(img)

    now = datetime.datetime.now()
    weather = get_weather()
    klines = get_klines()
    headlines = get_headlines()
    quote = get_quote()

    # ---------------- 报头
    date_main = now.strftime("%-m月%-d日")
    weekday = "周" + "一二三四五六日"[now.weekday()]
    updated = now.strftime("%H:%M")
    # 字体相关留白: 本机 PingFang 与 CI 的 Noto CJK 字形垂直度量不同,
    # 日期下沿到副标题的间距按宽的一侧留, 两边都不贴。
    tbox(draw, (MARGIN, 44, W - MARGIN, 179), date_main, F(112), anchor="la")
    tbox(draw, (MARGIN, 186, W - MARGIN, 234),
         f"{weekday}  ·  更新于 {updated}", F(34), fill=GRAY, anchor="la")

    # 天气并入报头右侧(不再单占一条横带), 把高度让给图表
    wtxt = f"{weather['desc']}  {weather['temp']}℃"
    wb = draw.textbbox((0, 0), wtxt, font=F(54))
    tbox(draw, (W - MARGIN - (wb[2] - wb[0]), 56, W - MARGIN, 124), wtxt, F(54), anchor="ra")
    detail = []
    if weather.get("tmax") is not None:
        detail.append(f"{weather['tmin']}~{weather['tmax']}℃")
    if weather.get("humidity") is not None:
        detail.append(f"湿度 {weather['humidity']}%")
    if detail:
        tbox(draw, (W - MARGIN - 420, 136, W - MARGIN, 174),
             "   ".join(detail), F(26), fill=GRAY, anchor="ra")
    if not weather["ok"]:
        tbox(draw, (W - MARGIN - 420, 176, W - MARGIN, 212), "(样例数据)", F(22), fill=GRAY, anchor="ra")
    draw.line([(MARGIN, 248), (W - MARGIN, 248)], fill=LIGHT, width=2)

    # ---------------- 行情双周K线（占满整幅宽，每标的独立归一化）
    tbox(draw, (MARGIN, 268, W - MARGIN, 320), "行情 · 双周 K 线", F(32), anchor="la")
    tbox(draw, (W - MARGIN - 620, 280, W - MARGIN, 316),
         "每根 2 周  ·  涨实心 / 跌空心", F(22), fill=GRAY, anchor="ra")
    k_top, k_bot = 336, 1250
    n = max(len(klines), 1)
    row_h = (k_bot - k_top) // n
    label_w = 172
    chart_x0 = MARGIN + label_w
    chart_x1 = W - MARGIN

    for i, (name, candles, chg, ok) in enumerate(klines):
        ry = k_top + i * row_h
        if i > 0:
            draw.line([(MARGIN, ry), (W - MARGIN, ry)], fill=LIGHT, width=1)
        tbox(draw, (MARGIN + 4, ry + 32, MARGIN + label_w - 12, ry + 76), name, F(32), anchor="la")
        arrow = "▲" if chg >= 0 else "▼"
        note = f"{arrow}{abs(chg):.2f}%" if ok else "—"
        tbox(draw, (MARGIN + 4, ry + 78, MARGIN + label_w - 12, ry + 114),
             note, F(24), fill=GRAY, anchor="la")
        centers = draw_candles(draw, (chart_x0, ry + 12, chart_x1, ry + row_h - 34), candles) or []
        # 每根蜡烛下方标出该段时间的截止日
        for cx, cd in zip(centers, candles):
            if not cd[0]:
                continue
            s = cd[0]
            try:
                tag = "%d/%d" % (int(s[5:7]), int(s[8:10]))
            except (ValueError, IndexError):
                continue
            tbox(draw, (cx - 44, ry + row_h - 30, cx + 44, ry + row_h - 8),
                 tag, F(17), fill=GRAY, anchor="ma")

    draw.line([(MARGIN, 1266), (W - MARGIN, 1266)], fill=LIGHT, width=2)

    # ---------------- 今日头条（每日一句挪到标题右侧, 不另占高度）
    tbox(draw, (MARGIN, 1286, W - MARGIN, 1338), "今日头条", F(32), anchor="la")
    tbox(draw, (W - MARGIN - 700, 1300, W - MARGIN, 1336), quote, F(22), fill=GRAY, anchor="ra")
    ty = 1350
    for i, title in enumerate(headlines, 1):
        tbox(draw, (MARGIN + 2, ty + 8, MARGIN + 48, ty + 48), f"{i}", F(27), fill=GRAY, anchor="la")
        t = ellipsize(draw, title, F(29), chart_x1 - (MARGIN + 58))
        tbox(draw, (MARGIN + 58, ty + 8, chart_x1, ty + 48), t, F(29), anchor="la")
        if i < len(headlines):
            draw.line([(MARGIN, ty + 53), (W - MARGIN, ty + 53)], fill=LIGHT, width=1)
        ty += 53

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img.save(out_path)
    print(f"saved -> {out_path} ({W}x{H})")


if __name__ == "__main__":
    build(os.path.join(HERE, "docs", "panel.png"))
