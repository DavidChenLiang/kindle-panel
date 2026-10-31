#!/usr/bin/env python3
# Kindle 信息面板 · 公网版 (只放公共数据, 无日程/待办)
# 数据: Open-Meteo 天气 / RSS 头条 / 腾讯行情日K / 每日一句
# 输出: docs/panel.png (供 GitHub Pages 托管)
import datetime
import json
import math
import os
import re
import time
import urllib.request
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw, ImageFont

W, H = 1264, 1680
MARGIN = 64
WHITE, BLACK, GRAY, LIGHT = 255, 0, 136, 180
DARK = 85        # 跌的蜡烛用深灰实心: 78 根密排时空心框只有 6px 宽, 会碎成一串小圈
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
BAR_COUNT = 78        # 三年 = 156 周 = 78 根双周蜡烛
FETCH_BARS = 800      # 日K根数, 800 根≈1150 自然日, 足够覆盖三年(1095 天)且留余量
REQ_GAP = 0.6         # 腾讯日K接口有频率限制, 连续快速请求会返回空


def fetch_kline(code):
    """返回 [(date, open, close, high, low), ...], 按时间正序。"""
    host = "usfqkline" if code.startswith("us") else "fqkline"
    url = ("https://web.ifzq.gtimg.cn/appstock/app/%s/get?param=%s,day,,,%d,qfq"
           % (host, code, FETCH_BARS))
    d = json.loads(http_get(url, timeout=15))
    data = d.get("data") if isinstance(d, dict) else None
    if not isinstance(data, dict):
        return []          # 接口偶发会返回异常结构, 交给上层用兜底波形
    node = data.get(code)
    if not isinstance(node, dict):
        return []
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


def _to_date(s):
    return datetime.date(int(s[0:4]), int(s[5:7]), int(s[8:10]))


def aggregate(bars, days=14, count=BAR_COUNT):
    """以最后一个交易日为终点往回切, 每 days 个自然日一根蜡烛。

    不用 ISO 周配对: 按自然周配对时, 最后那一组会被切在周中(只含 5 个交易日),
    它的涨跌幅度与前面 10 个交易日的组不可比, 最后一根蜡烛会突然变成巨人。
    以终点为基准往回切, 每组都是完整的 14 天, 所有标的横轴也严格对齐。
    """
    if not bars:
        return []
    end = _to_date(bars[-1][0])
    groups = {}
    for b in bars:
        off = (end - _to_date(b[0])).days
        if off < 0:
            continue
        groups.setdefault(off // days, []).append(b)
    out = []
    for k in sorted(groups)[0:count][::-1]:   # 取最近 count 组, 再按时间正序
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
        if i > 0:
            time.sleep(REQ_GAP)   # 拉开间隔, 否则第 4~5 只必被限流返回空
        bars = []
        for attempt in range(2):  # 限流是偶发的, 重试一次基本都能救回来
            try:
                bars = fetch_kline(code)
            except Exception:
                bars = []
            if len(bars) >= 500:   # 三年口径至少要有 2 年以上的日K才算拉全
                break
            time.sleep(1.2)
        candles = aggregate(bars) if bars else []
        ok = len(candles) >= 2
        if not ok:
            candles = _fake_candles(i * 3)
            print("[warn] %s K线拉取失败(%d 条), 使用占位波形" % (name, len(bars)))
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
    pad = (hi - lo) * 0.04
    hi += pad
    lo -= pad
    span = hi - lo
    hgt = y1 - y0
    n = len(candles)
    step = (x1 - x0) / float(n)
    # 78 根时间距只有 ~12px, 宽度按间距比例取, 但下限压到 4px:
    # 若沿用 max(8,...) 会撑到间距的 65%, 蜡烛之间几乎没有空隙。
    bw = max(4, int(round(step * 0.70)))
    half = max(1, bw // 2)
    # 影线宽度跟着实体走: 一年 26 根时实体只有十几像素, 影线还用 3px 会让
    # 每根都像"十"字架, 实体反而被压没了。
    lw = 3 if bw >= 24 else (2 if bw >= 12 else 1)
    centers = [int(round(x0 + step * (i + 0.5))) for i in range(n)]

    def yv(v):
        return int(round(y1 - (v - lo) / span * hgt))

    for i, (_d, op, cl, hh, ll) in enumerate(candles):
        cx = centers[i]
        col = BLACK if cl >= op else DARK
        # 影线: 贯穿最高到最低
        draw.line([(cx, yv(hh)), (cx, yv(ll))], fill=col, width=lw)
        top, btm = yv(max(op, cl)), yv(min(op, cl))
        # 实体至少 5px 高, 否则 3 年图上大部分实体只剩 2~4px, 看起来像一条虚线
        if btm - top < 5:
            mid = (top + btm) // 2
            top, btm = mid - 2, mid + 3
        draw.rectangle([cx - half, top, cx + half, btm], fill=col)
    return centers


# ---------------------------------------------------------------- 其他数据

# 面板已不再展示"今日头条"(用户要求去掉), 取数逻辑保留着, 想恢复只需在 build()
# 里重新调用 get_headlines() 并把绘制循环加回去, 不必重写。
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


# ---------------------------------------------------------------- 每日谏言
# 全部取价值投资口径(格雷厄姆 / 巴菲特 / 芒格 / 马克斯 / 林奇 / 邓普顿 / 博格 /
# 卡拉曼 / 费雪), 中文为意译。正文控制在 20 字以内 —— 它要挤在"今日头条"
# 标题右侧那一行里, 超了就压到标题。
QUOTES = [
    # --- 格雷厄姆《证券分析》《聪明的投资者》
    ("市场短期是投票机，长期是称重机。", "格雷厄姆"),
    ("安全边际，是投资的中心思想。", "格雷厄姆"),
    ("投资者最大的敌人，往往是他自己。", "格雷厄姆"),
    ("风险来自永久性损失，而非价格波动。", "格雷厄姆"),
    # --- 巴菲特
    ("价格是你付出的，价值是你得到的。", "巴菲特"),
    ("别人贪婪时恐惧，别人恐惧时贪婪。", "巴菲特"),
    ("不想持有十年，就别持有十分钟。", "巴菲特"),
    ("潮水退去，才知道谁在裸泳。", "巴菲特"),
    ("时间是优秀企业的朋友。", "巴菲特"),
    ("宁以合理价买伟大，不以廉价买平庸。", "巴菲特"),
    ("我们最喜欢的持有期限，是永远。", "巴菲特"),
    ("风险来自你不知道自己在做什么。", "巴菲特"),
    ("第一条是不亏钱，第二条是记住第一条。", "巴菲特"),
    ("伟大的公司，必有一条持久的护城河。", "巴菲特"),
    ("你不必对每一次挥棒都出手。", "巴菲特"),
    ("只在自己的能力圈里投资。", "巴菲特"),
    # --- 芒格《穷查理宝典》
    ("反过来想，总是反过来想。", "芒格"),
    ("大钱不在买卖中，而在等待中。", "芒格"),
    ("不能承受腰斩，就不适合买股票。", "芒格"),
    ("分散投资，是对无知的保护。", "芒格"),
    ("知道自己不知道什么，比聪明更重要。", "芒格"),
    # --- 霍华德·马克斯《投资最重要的事》
    ("关键不是买好东西，而是买得好。", "马克斯"),
    ("你无法预测，但可以为未来准备。", "马克斯"),
    ("风险，是可能发生的比实际更多。", "马克斯"),
    # --- 彼得·林奇
    ("每只股票背后，都是一家公司。", "林奇"),
    ("知道为什么持有，比持有什么更重要。", "林奇"),
    ("在股市赚钱的关键，是不被吓跑。", "林奇"),
    # --- 邓普顿
    ("牛市在悲观中诞生，在怀疑中成长。", "邓普顿"),
    ("最悲观的时刻，往往是最好的买点。", "邓普顿"),
    # --- 约翰·博格
    ("时间是你的朋友，冲动是你的敌人。", "博格"),
    ("你拿到的是市场收益减掉成本。", "博格"),
    ("别在草堆里找针，买下整个草堆。", "博格"),
    # --- 塞思·卡拉曼《安全边际》
    ("价值投资的核心是安全边际，不是预测。", "卡拉曼"),
    ("现金不是拖累，而是选择权。", "卡拉曼"),
    # --- 菲利普·费雪《怎样选择成长股》
    ("买真正优秀的公司，然后耐心持有。", "费雪"),
    ("值得长期持有的公司，总是少数。", "费雪"),
    # --- 价值投资通识(不假托名人)
    ("买股票就是买企业的一部分。", ""),
    ("好公司，也要等一个好价格。", ""),
    ("复利需要时间，时间需要耐心。", ""),
    ("看得懂才买，看不懂就不买。", ""),
    ("与其猜市场，不如算企业。", ""),
    ("慢，是因为看得长。", ""),
]


def _interleave(items):
    """按作者轮流取, 让相邻两天尽量不是同一个人。

    上面那段按作者分组写, 可读性好, 但直接按日期取模会连排一周巴菲特
    ("—— 巴菲特"连看八天很单调)。这里把各组打散成交替顺序。
    """
    buckets = {}
    for q in items:
        buckets.setdefault(q[1] or "通识", []).append(q)
    out = []
    while any(buckets.values()):
        for k in list(buckets):
            if buckets[k]:
                out.append(buckets[k].pop(0))
    return out


QUOTES = _interleave(QUOTES)


def get_quote():
    """按日期取模: 同一天多次生成(CI 每半小时重跑)保持同一句, 跨天才换。

    返回 (正文, 署名)。通识类句子没有明确出处, who 为空串 —— 不假托名人。
    """
    return QUOTES[datetime.date.today().toordinal() % len(QUOTES)]


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
    quote, qwho = get_quote()

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
    # 跨年图上"11/14"看不出是去年, 把实际数据区间写进副标题, 年数按真实跨度算
    sub = "近三年  ·  每根 2 周  ·  涨深 / 跌浅"
    span_days, first_d, last_d = 0, "", ""
    try:
        first_d, last_d = klines[0][1][0][0], klines[0][1][-1][0]
        span_days = (_to_date(last_d) - _to_date(first_d)).days
        sub = ("近 %d 年  %s–%s  ·  每根 2 周  ·  涨深 / 跌浅"
               % (max(1, int(round(span_days / 365.0))),
                  first_d[2:7].replace("-", "/"), last_d[2:7].replace("-", "/")))
    except (IndexError, TypeError, ValueError):
        pass
    tbox(draw, (W - MARGIN - 760, 280, W - MARGIN, 316), sub, F(22), fill=GRAY, anchor="ra")
    # 跨度超过 500 天时日期改用 年/月, 否则"11/14"在三年图上分不清是哪一年
    use_ym = span_days > 500
    k_top, k_bot = 336, 1370       # 去掉头条后把省下的高度给 K 线, 每行 206px(原 182)
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
        # 蜡烛下方标截止日。26 根时间距仅 ~37px 而标签宽 ~46px, 全标会互相压叠,
        # 所以按可用间距抽稀; 首尾两根必标(标出区间两端), 末尾太挤时用末根替换倒数第二个。
        nc = len(candles)
        step_px = (centers[1] - centers[0]) if nc > 1 else 0
        every = max(1, int(math.ceil(86.0 / step_px))) if step_px else 1
        idxs = list(range(0, nc, every)) if nc else []
        if idxs and idxs[-1] != nc - 1:
            if nc - 1 - idxs[-1] < every * 0.5:
                idxs[-1] = nc - 1
            else:
                idxs.append(nc - 1)
        pick = set(idxs)
        for idx, (cx, cd) in enumerate(zip(centers, candles)):
            if idx not in pick:
                continue
            if not cd[0]:
                continue
            s = cd[0]
            try:
                if use_ym:
                    tag = "%s/%d" % (s[2:4], int(s[5:7]))          # 23/11
                else:
                    tag = "%d/%d" % (int(s[5:7]), int(s[8:10]))     # 11/14
            except (ValueError, IndexError):
                continue
            tbox(draw, (cx - 44, ry + row_h - 30, cx + 44, ry + row_h - 8),
                 tag, F(17), fill=GRAY, anchor="ma")

    # ---------------- 每日谏言（去掉头条后, 这块整体让给谏言放大）
    # 区块 1390~1616, 内容垂直居中: 上方留白 ≈ 下方留白, 否则底部会空一大块。
    draw.line([(MARGIN, 1390), (W - MARGIN, 1390)], fill=LIGHT, width=2)
    tag_w, tag_h, tag_y = 66, 32, 1426
    draw.rectangle([MARGIN, tag_y, MARGIN + tag_w, tag_y + tag_h], fill=BLACK)
    tbox(draw, (MARGIN, tag_y, MARGIN + tag_w, tag_y + tag_h), "谏言", F(20), fill=WHITE, anchor="ma")

    fq = F(48)
    qx = MARGIN + tag_w + 26
    qt = ellipsize(draw, quote, fq, W - MARGIN - qx)
    tbox(draw, (qx, 1424, W - MARGIN, 1500), qt, fq, anchor="la")
    if qwho:
        tbox(draw, (qx, 1526, W - MARGIN, 1574), "—— " + qwho, F(28), fill=GRAY, anchor="ra")

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    img.save(out_path)
    print(f"saved -> {out_path} ({W}x{H})")


if __name__ == "__main__":
    build(os.path.join(HERE, "docs", "panel.png"))
