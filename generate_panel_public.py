#!/usr/bin/env python3
# Kindle 信息面板 · 公网版 (只放公共数据, 无日程/待办)
# 数据: Open-Meteo 天气 / RSS 头条 / 腾讯行情指数 / 每日一句
# 输出: docs/panel.png (供 GitHub Pages 托管)
import datetime
import json
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


def get_indices():
    # 三个 A 股指数 + 纳斯达克(隔夜) + 招商银行(个股)
    codes = ["sh000001", "sz399001", "sz399006", "usIXIC", "sh600036"]
    try:
        raw = http_get("https://qt.gtimg.cn/q=" + ",".join(codes), encoding="gbk")
        order = {c: i for i, c in enumerate(codes)}
        found = {}
        for line in raw.split(";"):
            if "=" not in line:
                continue
            head, _, body = line.partition('="')
            body = body.rstrip('"')
            code = head.replace("v_", "").strip()
            f = body.split("~")
            if len(f) < 6:
                continue
            try:
                name, cur, prev = f[1], float(f[3]), float(f[4])
            except (ValueError, IndexError):
                continue
            if not prev:
                continue
            chg = (cur - prev) / prev * 100
            found[code] = (name, cur, chg)
        # 按传入顺序输出, 避免接口返回顺序变化导致版面跳动
        out = [found[c] for c in codes if c in found]
        if out:
            return out
    except Exception:
        pass
    return [("上证指数", 3123.45, 0.8), ("深证成指", 9876.54, -0.4),
            ("创业板指", 1876.32, 1.2), ("纳斯达克", 25437.00, -2.86),
            ("招商银行", 43.55, 1.42)]


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
    x0, y0, x1, y1 = box
    l, t, r, b = draw.textbbox((0, 0), s, font=font)
    w, h = r - l, b - t
    if anchor == "la":
        px, py = x0, y0
    elif anchor == "ma":
        px, py = (x0 + x1) / 2 - w / 2, (y0 + y1) / 2 - h / 2
    elif anchor == "ra":
        px, py = x1 - w, y0
    draw.text((px, py), s, font=font, fill=fill)


def section(draw, title, box):
    x0, y0, x1, y1 = box
    draw.rectangle([x0, y0, x1, y1], outline=BLACK, width=3)
    draw.rectangle([x0, y0, x1, y0 + 52], fill=BLACK)
    tbox(draw, (x0 + 16, y0, x1 - 16, y0 + 52), title, F(30), fill=WHITE, anchor="la")


def build(out_path):
    img = Image.new("L", (W, H), WHITE)
    draw = ImageDraw.Draw(img)

    now = datetime.datetime.now()
    weather = get_weather()
    indices = get_indices()
    headlines = get_headlines()
    quote = get_quote()

    date_main = now.strftime("%-m月%-d日")
    weekday = "周" + "一二三四五六日"[now.weekday()]
    updated = now.strftime("%H:%M")
    tbox(draw, (MARGIN, 50, W - MARGIN, 190), date_main, F(118), anchor="la")
    tbox(draw, (MARGIN, 190, W - MARGIN, 250),
         f"{weekday}  ·  更新于 {updated}", F(38), fill=GRAY, anchor="la")
    wtxt = f"{weather['desc']}  {weather['temp']}℃"
    wb = draw.textbbox((0, 0), wtxt, font=F(56))
    tbox(draw, (W - MARGIN - (wb[2] - wb[0]), 60, W - MARGIN, 130), wtxt, F(56), anchor="ra")
    if not weather["ok"]:
        tbox(draw, (W - MARGIN - 240, 140, W - MARGIN, 180), "(样例数据)", F(24), fill=GRAY, anchor="ra")
    draw.line([(MARGIN, 280), (W - MARGIN, 280)], fill=LIGHT, width=2)

    col_w = (W - 2 * MARGIN - 40) // 2
    y_top, y_bot = 300, 760

    section(draw, "天气 · 上海", (MARGIN, y_top, MARGIN + col_w, y_bot))
    wx, wy = MARGIN + 24, y_top + 120
    tbox(draw, (wx, wy, wx + col_w, wy + 100), weather["desc"], F(72), anchor="la")
    tbox(draw, (wx, wy + 108, wx + col_w, wy + 220), f"{weather['temp']}℃", F(86), anchor="la")
    detail = []
    if weather.get("tmax") is not None:
        detail.append(f"{weather['tmin']}~{weather['tmax']}℃")
    if weather.get("humidity") is not None:
        detail.append(f"湿度 {weather['humidity']}%")
    tbox(draw, (wx, wy + 232, wx + col_w, wy + 280), "  ".join(detail), F(32), fill=GRAY, anchor="la")

    idx_x0 = MARGIN + col_w + 40
    section(draw, "行情", (idx_x0, y_top, W - MARGIN, y_bot))
    # 行距按条数自适应: 3 条时 86px, 4 条时自动收窄, 避免溢出区块
    n_idx = len(indices)
    sec_top = y_top + 80
    row_gap = min(86, (y_bot - sec_top - 14) // max(n_idx, 1))
    sy = sec_top
    for i, (name, cur, chg) in enumerate(indices):
        tbox(draw, (idx_x0 + 24, sy, idx_x0 + 204, sy + 50), name, F(34), anchor="la")
        tbox(draw, (W - MARGIN - 240, sy, W - MARGIN - 150, sy + 50), f"{cur:.2f}", F(34), anchor="ra")
        arrow = "▲" if chg >= 0 else "▼"
        tbox(draw, (W - MARGIN - 140, sy, W - MARGIN - 24, sy + 50), f"{arrow}{abs(chg):.2f}%", F(30), anchor="ra")
        if i < n_idx - 1:
            draw.line([(idx_x0 + 24, sy + row_gap - 12), (W - MARGIN - 24, sy + row_gap - 12)],
                      fill=LIGHT, width=2)
        sy += row_gap

    y2, y2b = 780, 1420
    section(draw, "今日头条", (MARGIN, y2, W - MARGIN, y2b))
    ty = y2 + 80
    for i, title in enumerate(headlines, 1):
        tbox(draw, (MARGIN + 24, ty, MARGIN + 80, ty + 60), f"{i}", F(40), fill=GRAY, anchor="la")
        tbox(draw, (MARGIN + 90, ty, W - MARGIN - 24, ty + 60), title, F(34), anchor="la")
        draw.line([(MARGIN + 24, ty + 78), (W - MARGIN - 24, ty + 78)], fill=LIGHT, width=2)
        ty += 112

    y3, y3b = 1440, H - MARGIN
    section(draw, "每日一句", (MARGIN, y3, W - MARGIN, y3b))
    tbox(draw, (MARGIN + 24, y3 + 60, W - MARGIN - 24, y3b - 20), quote, F(38), anchor="ma")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img.save(out_path)
    print(f"saved -> {out_path} ({W}x{H})")


if __name__ == "__main__":
    build(os.path.join(HERE, "docs", "panel.png"))
