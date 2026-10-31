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


# ---------------------------------------------------------------- 日K线

KLINE_SPECS = [
    ("sh000001", "上证指数"),
    ("sz399001", "深证成指"),
    ("sz399006", "创业板指"),
    ("usIXIC",   "纳斯达克"),
    ("sh600036", "招商银行"),
]
KLINE_DAYS = 30


def fetch_kline(code):
    """返回 [(open, close, high, low), ...], 按时间正序。"""
    host = "usfqkline" if code.startswith("us") else "fqkline"
    url = ("https://web.ifzq.gtimg.cn/appstock/app/%s/get?param=%s,day,,,%d,qfq"
           % (host, code, KLINE_DAYS))
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
            bars.append((float(r[1]), float(r[2]), float(r[3]), float(r[4])))
        except (ValueError, IndexError, TypeError):
            continue
    return bars[-KLINE_DAYS:]


def _fake_bars(seed):
    """接口挂掉时的兜底波形, 保证版面不出现空洞。"""
    bars = []
    base = 100.0
    for i in range(KLINE_DAYS):
        op = base + math.sin((i + seed) / 3.2) * 5 + i * 0.25
        cl = op + math.sin((i + seed) * 1.7) * 1.6 + 0.3
        bars.append((op, cl, max(op, cl) + 1.1, min(op, cl) - 1.1))
    return bars


def get_klines():
    out = []
    for i, (code, name) in enumerate(KLINE_SPECS):
        try:
            bars = fetch_kline(code)
        except Exception:
            bars = []
        ok = len(bars) >= 5
        if not ok:
            bars = _fake_bars(i * 7)
        chg = 0.0
        if len(bars) >= 2 and bars[-2][1]:
            chg = (bars[-1][1] - bars[-2][1]) / bars[-2][1] * 100.0
        out.append((name, bars, chg, ok))
    return out


def draw_trend(draw, box, bars):
    """收盘价折线 + 日内高低影线。

    为什么不是蜡烛图: 30 天价格区间常是日内振幅的 10~20 倍, 在 170px 高的
    条带里蜡烛实体只剩 3~5px、影线几乎不可见, 退化成"小方块带", 读不出趋势。
    折线保住了形状, 淡影线保住每天的波动, 是财经印刷品的通行做法。
    """
    x0, y0, x1, y1 = [int(round(v)) for v in box]
    if not bars or x1 <= x0 or y1 <= y0:
        return
    hi = max(b[2] for b in bars)
    lo = min(b[3] for b in bars)
    if hi <= lo:
        hi, lo = hi + 1.0, lo - 1.0
    pad = (hi - lo) * 0.08
    hi += pad
    lo -= pad
    span = hi - lo
    hgt = y1 - y0
    n = len(bars)
    step = (x1 - x0) / float(n)

    def yv(v):
        return int(round(y1 - (v - lo) / span * hgt))

    # 1) 每日高低影线, 先画, 让折线压在上面
    for i, (op, cl, hh, ll) in enumerate(bars):
        cx = int(round(x0 + step * (i + 0.5)))
        draw.line([(cx, yv(hh)), (cx, yv(ll))], fill=GRAY, width=2)

    # 2) 收盘价折线
    pts = [(int(round(x0 + step * (i + 0.5))), yv(cl)) for i, (op, cl, hh, ll) in enumerate(bars)]
    if len(pts) > 1:
        draw.line(pts, fill=BLACK, width=4, joint="curve")

    # 3) 首尾端点: 起点空心, 终点实心
    draw.ellipse([pts[0][0] - 6, pts[0][1] - 6, pts[0][0] + 6, pts[0][1] + 6], fill=WHITE, outline=BLACK, width=2)
    draw.ellipse([pts[-1][0] - 7, pts[-1][1] - 7, pts[-1][0] + 7, pts[-1][1] + 7], fill=BLACK)


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

    # ---------------- 行情走势（占满整幅宽，每标的独立归一化）
    tbox(draw, (MARGIN, 268, W - MARGIN, 320), "行情 · 近一月收盘走势", F(32), anchor="la")
    k_top, k_bot = 340, 1250
    n = max(len(klines), 1)
    row_h = (k_bot - k_top) // n
    label_w = 172
    chart_x0 = MARGIN + label_w
    chart_x1 = W - MARGIN

    for i, (name, bars, chg, ok) in enumerate(klines):
        ry = k_top + i * row_h
        if i > 0:
            draw.line([(MARGIN, ry), (W - MARGIN, ry)], fill=LIGHT, width=1)
        tbox(draw, (MARGIN + 4, ry + 34, MARGIN + label_w - 12, ry + 80), name, F(32), anchor="la")
        arrow = "▲" if chg >= 0 else "▼"
        note = f"{arrow}{abs(chg):.2f}%" if ok else "—"
        tbox(draw, (MARGIN + 4, ry + 84, MARGIN + label_w - 12, ry + 120),
             note, F(24), fill=GRAY, anchor="la")
        draw_trend(draw, (chart_x0, ry + 16, chart_x1, ry + row_h - 16), bars)

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
