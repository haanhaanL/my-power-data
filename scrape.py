import asyncio
import csv
import os
import re
from datetime import datetime, timedelta, timezone
from playwright.async_api import async_playwright

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as ticker
from matplotlib.patches import Patch
from matplotlib.lines import Line2D
import matplotlib.font_manager

# 强制刷新字体缓存，确保 CI 新装字体能被识别
matplotlib.font_manager._load_fontmanager(try_read_cache=False)

# ============ 配置 ============
URL_TEAM1 = "https://y.qq.com/tmead/promotion/h5/team-activity/detail?_hidehd=1&_miniplayer=1&adtag=sszd&singerId=6665073&activityId=17931826095559&searchid=113943832761792946,&hosteuin=ow657wSlownlon**&ADTAG=wxfshare"
URL_TEAM2 = None
URL_POWER = "https://y.qq.com/tmead/promotion/h5/powering/1793152992875?_hidehd=1&_miniplayer=1&adtag=share_task&singerId=6665073&p_share=w2pZEizuC5V-xTHRIivoTwD_5f_DMRfGNgTtS7isP_4&hosteuin=oiEqow6FoiCA7n**&ADTAG=wxfshare"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "power_data.csv")
CHART_FILE = os.path.join(BASE_DIR, "chart.png")

# 北京时间 UTC+8
BJT = timezone(timedelta(hours=8))


# ============ 抓取与存储 ============
def parse_num(text):
    if not text: return None
    m = re.search(r"[\d,]+", str(text))
    return int(m.group(0).replace(",", "")) if m else None


async def scrape_team(page, url):
    if not url: return {"今日心动人数": None, "团建电力值": None}
    await page.goto(url, wait_until="domcontentloaded", timeout=90000)

    result = {"今日心动人数": None, "团建电力值": None}

    # --- 心动人数：等"含数字的文本"出现，最多 30s ---
    try:
        await page.wait_for_function(
            """() => {
                const els = document.querySelectorAll('.stats-value .value');
                return [...els].some(e => /\\d/.test(e.innerText));
            }""",
            timeout=30000,
        )
    except Exception as e:
        print(f"[scrape_team] 等心动文本超时: {e}")

    # 遍历取第一个"非空且含数字"的，避开隐藏占位/骨架屏
    try:
        loc = page.locator(".stats-value .value")
        value_text = None
        for i in range(await loc.count()):
            t = await loc.nth(i).inner_text()
            if t and re.search(r"\d", t):
                value_text = t
                break
        print(f"[debug] 心动人数文本: {value_text!r}")
        result["今日心动人数"] = parse_num(value_text)
    except Exception as e:
        print(f"[scrape_team] 抓取心动人数失败: {e}")

    # --- 兜底：文本锚点正则 ---
    if result["今日心动人数"] is None:
        try:
            body_text = await page.locator("body").inner_text()
            m = re.search(r"心动[^\d]{0,15}([\d,]+)", body_text)
            if m:
                result["今日心动人数"] = int(m.group(1).replace(",", ""))
                print(f"[debug] 心动人数兜底命中: {result['今日心动人数']}")
            else:
                print(f"[debug] 心动全失败, body 前 500: {body_text[:500]!r}")
                await page.screenshot(path="debug_team.png", full_page=True)
        except Exception as e:
            print(f"[scrape_team] 兜底失败: {e}")

    # --- 团建电力值：保持原样 ---
    try:
        body_text = await page.locator("body").inner_text()
        matches = re.findall(r"(\d{1,3}(?:,\d{3})+)\s*/\s*(\d{1,3}(?:,\d{3})+)", body_text)
        if matches:
            best = max(matches, key=lambda x: int(x[0].replace(",", "")))
            result["团建电力值"] = int(best[0].replace(",", ""))
    except Exception as e:
        print(f"[scrape_team] 抓取团建电力值失败: {e}")
    return result


async def scrape_power(page):
    await page.goto(URL_POWER, wait_until="domcontentloaded", timeout=90000)
    result = {"总电力值": None}
    try:
        # 等锚点"总电力"附近出现数字，最多 30s
        await page.wait_for_function(
            """() => /总电力[^\\d]{0,15}[\\d,]+/.test(document.body.innerText)""",
            timeout=30000,
        )
    except Exception as e:
        print(f"[scrape_power] 等总电力文本超时: {e}")

    try:
        body_text = await page.locator("body").inner_text()
        # 优先锚点提取
        m = re.search(r"总电力[^\d]{0,15}([\d,]+)", body_text)
        if m:
            result["总电力值"] = int(m.group(1).replace(",", ""))
            print(f"[debug] 总电力锚点命中: {result['总电力值']}")
        else:
            # 兜底：千分位 或 纯数字，过滤掉明显是小数字的
            nums = re.findall(r"\d{1,3}(?:,\d{3})+|\b\d+\b", body_text)
            cand = [int(n.replace(",", "")) for n in nums]
            cand = [c for c in cand if c > 1000]
            if cand:
                result["总电力值"] = max(cand)
                print(f"[debug] 总电力兜底命中: {result['总电力值']}")
            else:
                print(f"[scrape_power] 无候选, body 前 500: {body_text[:500]!r}")
                await page.screenshot(path="debug_power.png", full_page=True)
    except Exception as e:
        print(f"[scrape_power] 抓取总电力值失败: {e}")
    return result


async def scrape_team_with_retry(page, url, retries=2):
    """心动人数偶发为空时重试，最多 retries 次"""
    r = {"今日心动人数": None, "团建电力值": None}
    for i in range(retries + 1):
        r = await scrape_team(page, url)
        if r["今日心动人数"] is not None:
            return r
        if i < retries:
            print(f"[retry] 心动人数第 {i+1} 次重试...")
            await asyncio.sleep(3)
    return r


async def scrape_power_with_retry(page, retries=2):
    """总电力值偶发为空时重试，最多 retries 次"""
    r = {"总电力值": None}
    for i in range(retries + 1):
        r = await scrape_power(page)
        if r["总电力值"] is not None:
            return r
        if i < retries:
            print(f"[retry] 总电力值第 {i+1} 次重试...")
            await asyncio.sleep(3)
    return r


def load_all_rows():
    if not os.path.exists(CSV_FILE): return [], []
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            with open(CSV_FILE, "r", encoding=enc, newline="") as f:
                content = list(csv.reader(f))
            return content[0], content[1:]
        except:
            continue
    return [], []


def parse_dt_loose(s):
    if not s: return None
    s = re.sub(r'(\d+[/-]\d+[/-]\d+)\s+(\d{1}):', r'\1 0\2:', s)
    s = s.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except:
            pass
    return None


def get_prev_hour_record(curr_dt, all_rows):
    def fmt_no_pad(dt_obj):
        return f"{dt_obj.year}/{dt_obj.month}/{dt_obj.day} {dt_obj.hour}"

    if curr_dt.hour == 23 and curr_dt.minute >= 55:
        curr_hour_str = fmt_no_pad(curr_dt.replace(hour=23))
        prev_hour_str = fmt_no_pad(curr_dt.replace(hour=22))
    else:
        curr_hour_str = fmt_no_pad(curr_dt)
        prev_hour_str = fmt_no_pad(curr_dt - timedelta(hours=1))

    for row in reversed(all_rows):
        if not row or len(row) < 10: continue
        if row[0].startswith(curr_hour_str) and curr_dt.minute <= 5: pass
        if row[0].startswith(prev_hour_str): return row
    return None


def append_csv(row_dict):
    file_exists = os.path.exists(CSV_FILE)
    header, all_rows = load_all_rows()
    curr_dt = datetime.strptime(row_dict["时间"], "%Y-%m-%d %H:%M:%S")

    if curr_dt.hour == 23 and curr_dt.minute >= 55:
        formatted_time = f"{curr_dt.year}/{curr_dt.month}/{curr_dt.day} 23:59"
        curr_dt = curr_dt.replace(hour=23, minute=59)
    else:
        formatted_time = f"{curr_dt.year}/{curr_dt.month}/{curr_dt.day} {curr_dt.hour:02d}:{curr_dt.minute:02d}"

    # 容差放宽到 10 分钟，兼容 GitHub 启动延迟
    is_hourly = (curr_dt.minute <= 10 or curr_dt.minute >= 55)
    prev_row = get_prev_hour_record(curr_dt, all_rows) if is_hourly else None

    def calc_delta(curr_val, col_idx):
        if not is_hourly or curr_val is None or prev_row is None: return ""
        try:
            prev_val_str = prev_row[col_idx]
            if prev_val_str == "" or prev_val_str is None: return ""
            return int(curr_val) - int(prev_val_str)
        except:
            return ""

    row = [
        formatted_time,
        row_dict["今日心动人数1"] or "", calc_delta(row_dict["今日心动人数1"], 1),
        row_dict["团建电力值1"] or "", calc_delta(row_dict["团建电力值1"], 3),
        row_dict["今日心动人数2"] or "", calc_delta(row_dict["今日心动人数2"], 5),
        row_dict["团建电力值2"] or "", calc_delta(row_dict["团建电力值2"], 7),
        row_dict["总电力值"] or "", calc_delta(row_dict["总电力值"], 9),
    ]

    with open(CSV_FILE, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(
                ["时间", "心动人数1", "增幅", "团建电力值1", "增幅", "心动人数2", "增幅", "团建电力值2", "增幅",
                 "总电力值", "增幅"])
        writer.writerow(row)
    print(f"✅ [{formatted_time}] 写入完成。\n")


# ============ 图表生成 ============
def to_float(v):
    try:
        return float(v) if v not in (None, "") else None
    except:
        return None


def fmt_w(v):
    if v is None: return ""
    sign = "-" if v < 0 else ""
    w = abs(v) / 10000.0
    return f"{sign}{int(round(w))}w" if abs(w - round(w)) < 1e-9 else f"{sign}{w:.1f}w"


def generate_chart(target_date, save_path, is_final=False):
    header, rows = load_all_rows()
    if not rows: return

    data_map = {}
    for r in rows:
        if len(r) < 10: continue
        dt = parse_dt_loose(r[0])
        if dt is None: continue

        # 容差与 append_csv 保持一致
        if dt.minute > 10 and dt.minute < 55:
            continue

        target_date_str = dt.strftime('%Y-%m-%d')

        rec = {"dt": dt, "hour": dt.hour, "minute": dt.minute,
               "heart": to_float(r[1]), "heart_delta": to_float(r[2]),
               "team1": to_float(r[3]), "delta": to_float(r[4])}

        if dt.hour == 0:
            data_map[f"{target_date_str}_0"] = rec
            continue

        if dt.hour == 23 and dt.minute >= 55:
            next_day_str = (dt + timedelta(days=1)).strftime('%Y-%m-%d')
            data_map[f"{next_day_str}_0_2359"] = rec
            data_map.setdefault(f"{target_date_str}_23", rec)
            continue

        key = f"{target_date_str}_{dt.hour}"
        data_map.setdefault(key, rec)

    today_str = target_date.strftime("%Y-%m-%d")
    yest_date = target_date - timedelta(days=1)
    yest_str = yest_date.strftime("%Y-%m-%d")
    next_str = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")

    start_week_date = target_date - timedelta(days=6)
    active_dates = set()
    for k in data_map.keys():
        parts = k.split("_")
        if len(parts) >= 2:
            active_dates.add(parts[0])
    week_dates_str = sorted([d for d in active_dates if start_week_date.strftime("%Y-%m-%d") <= d <= today_str])
    week_dates = [datetime.strptime(d, "%Y-%m-%d").date() for d in week_dates_str]
    if not week_dates: week_dates = [yest_date, target_date]

    hours = list(range(0, 25))

    # 文泉驿正黑带粗体，放第一位；其余作为回退
    plt.rcParams["font.sans-serif"] = ["WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "Microsoft YaHei", "SimHei",
                                       "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, axes = plt.subplots(2, 1, figsize=(14, 10), sharex=True)
    fig.suptitle(f"本周数据对比 (结算日: {target_date})" if is_final else f"今天({target_date}) 本周数据对比",
                 fontsize=18, fontweight="bold", color="#D35400", y=0.96)

    today_delta_sum = 0
    for h in range(0, 25):
        if h == 0:
            rec = data_map.get(f"{today_str}_0")
        elif h == 24:
            rec = data_map.get(f"{next_str}_0_2359") or data_map.get(f"{next_str}_0")
        else:
            rec = data_map.get(f"{today_str}_{h}")
        if rec and rec["delta"] is not None:
            today_delta_sum += rec["delta"]

    yest_delta_sum = 0
    for h in range(0, 25):
        if h == 0:
            rec = data_map.get(f"{yest_str}_0")
        elif h == 24:
            rec = data_map.get(f"{today_str}_0_2359") or data_map.get(f"{today_str}_0")
        else:
            rec = data_map.get(f"{yest_str}_{h}")
        if rec and rec["delta"] is not None:
            yest_delta_sum += rec["delta"]

    fig.text(0.5, 0.91, f"昨日增幅总和: {fmt_w(yest_delta_sum)}   |   今日增幅总和: {fmt_w(today_delta_sum)}",
             ha='center', fontsize=14, fontweight='bold', color='#555555',
             bbox=dict(facecolor='#FDF2E9', edgecolor='#FFB27A', boxstyle='round,pad=0.5'))

    color_yest_line, color_today_line = "#FFB3C6", "#FF8C42"

    # ============ 子图1：心动人数 ============
    ax1 = axes[0]

    for d in week_dates:
        d_str = d.strftime("%Y-%m-%d")
        if d == target_date or d == yest_date:
            continue

        vals = []
        for h in hours:
            if h == 0:
                rec = data_map.get(f"{d_str}_0")
            elif h == 24:
                next_key = (datetime.strptime(d_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                rec = data_map.get(f"{next_key}_0_2359") or data_map.get(f"{next_key}_0")
            else:
                rec = data_map.get(f"{d_str}_{h}")

            val = rec["heart"] if rec and rec["heart"] is not None else float("nan")
            vals.append(val)

        ax1.plot(hours, vals, linestyle="--", color="#B0B0B0", linewidth=1.2, alpha=0.8)

    yest_heart, today_heart = [], []
    for h in hours:
        if h == 0:
            y_data = data_map.get(f"{yest_str}_0")
            t_data = data_map.get(f"{today_str}_0")
        elif h == 24:
            y_data = data_map.get(f"{today_str}_0_2359") or data_map.get(f"{today_str}_0")
            t_data = data_map.get(f"{next_str}_0_2359") or data_map.get(f"{next_str}_0")
        else:
            y_data = data_map.get(f"{yest_str}_{h}")
            t_data = data_map.get(f"{today_str}_{h}")

        y_val = y_data["heart"] if y_data and y_data["heart"] is not None else float("nan")
        t_val = t_data["heart"] if t_data and t_data["heart"] is not None else float("nan")

        yest_heart.append(y_val)
        today_heart.append(t_val)

    ax1.plot(hours, yest_heart, marker="o", linestyle="-", color=color_yest_line, linewidth=2.0, markersize=6,
             label="昨天心动人数")
    ax1.plot(hours, today_heart, marker="o", linestyle="-", color=color_today_line, linewidth=2.5, markersize=6,
             label="今天心动人数")

    for h, val in zip(hours, yest_heart):
        if val is not None and not (isinstance(val, float) and val != val):
            ax1.annotate(f'{int(val):,}', (h, val), textcoords="offset points", xytext=(0, 8), ha='center', fontsize=8,
                         color="#B36B7A", fontweight="bold")

    for h, val in zip(hours, today_heart):
        if val is not None and not (isinstance(val, float) and val != val):
            ax1.annotate(f'{int(val):,}', (h, val), textcoords="offset points", xytext=(0, -14), ha='center',
                         fontsize=8, color="#D35400", fontweight="bold")

            if h == 0:
                t_data = data_map.get(f"{today_str}_0")
            elif h == 24:
                t_data = data_map.get(f"{next_str}_0_2359") or data_map.get(f"{next_str}_0")
            else:
                t_data = data_map.get(f"{today_str}_{h}")

            delta = t_data["heart_delta"] if t_data else None
            if delta is not None:
                sign = "+" if delta > 0 else ""
                ax1.annotate(f'({sign}{int(delta):,})', (h, val), textcoords="offset points", xytext=(0, 6),
                             ha='center', fontsize=7, color="#E65100", fontweight="bold")

    ax1.set_title("心动人数1 对比 (本周)", fontsize=14, fontweight="bold", pad=10, color="#333333")
    ax1.set_ylabel("心动人数", fontsize=11)
    ax1.grid(axis='y', linestyle="--", alpha=0.4)
    ax1.grid(axis='x', linestyle="--", alpha=0.3)
    ax1.spines['top'].set_visible(False)
    ax1.spines['right'].set_visible(False)
    ax1.set_xlim(0, 24)

    # ============ 子图2：团建电力增幅 ============
    ax2 = axes[1]
    for d in week_dates:
        d_str = d.strftime("%Y-%m-%d")
        if d == target_date:
            color_hex, line_style, width, is_label = "#FF8C42", "-", 2.5, True
        elif d == yest_date:
            color_hex, line_style, width, is_label = "#FFB3C6", "-", 2.0, True
        else:
            color_hex, line_style, width, is_label = "#B0B0B0", "--", 1.2, False

        vals, totals = [], []
        for h in hours:
            if h == 0:
                rec0 = data_map.get(f"{d_str}_0")
                if rec0 and rec0["delta"] is not None:
                    vals.append(rec0["delta"])
                else:
                    vals.append(float("nan"))
                totals.append(rec0["team1"] if rec0 and rec0["team1"] is not None else None)
                continue

            if h == 24:
                next_key = (datetime.strptime(d_str, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
                rec = data_map.get(f"{next_key}_0_2359") or data_map.get(f"{next_key}_0")
            else:
                rec = data_map.get(f"{d_str}_{h}")

            vals.append(rec["delta"] if rec and rec["delta"] is not None else float("nan"))
            totals.append(rec["team1"] if rec and rec["team1"] is not None else None)

        ax2.plot(hours, vals, marker="o" if is_label else None, linestyle=line_style, color=color_hex, linewidth=width,
                 markersize=6 if is_label else 0, alpha=1.0 if is_label else 0.8)

        if is_label:
            for j, h in enumerate(hours):
                v, tot = vals[j], totals[j]
                if v is not None and not (isinstance(v, float) and v != v):
                    offset_y = -25 if d == yest_date else 16
                    text_color = "#B36B7A" if d == yest_date else "#D35400"
                    fw = "normal" if d == yest_date else "bold"
                    ax2.annotate(fmt_w(v), (h, v), textcoords="offset points", xytext=(0, offset_y), ha="center",
                                 fontsize=8, color=text_color, fontweight=fw)
                    if tot is not None:
                        ax2.annotate(f"总:{fmt_w(tot)}", (h, v), textcoords="offset points",
                                     xytext=(0, offset_y - 15 if d == yest_date else 28), ha="center", fontsize=7,
                                     color=text_color)

    ax2.set_title("团建电力值1 增幅对比 (本周)", fontsize=14, fontweight="bold", pad=10, color="#333333")
    ax2.set_ylabel("每小时增幅", fontsize=11)
    ax2.grid(axis='y', linestyle="--", alpha=0.4)
    ax2.grid(axis='x', linestyle="--", alpha=0.3)
    ax2.spines['top'].set_visible(False)
    ax2.spines['right'].set_visible(False)
    ax2.set_xlim(0, 24)

    def time_formatter(x, pos):
        if x == 0: return "00:00"
        if x == 24: return "24:00"
        return f"{int(x):02d}:00"

    formatter = ticker.FuncFormatter(time_formatter)

    ax2.xaxis.set_major_formatter(formatter)
    ax2.xaxis.set_major_locator(ticker.MultipleLocator(1))
    ax2.set_xlabel("时段", fontsize=11)
    fig.autofmt_xdate(rotation=30, ha="right")

    ax1_top = ax1.twiny()
    ax1_top.set_xlim(0, 24)
    ax1_top.xaxis.set_major_formatter(formatter)
    ax1_top.xaxis.set_major_locator(ticker.MultipleLocator(1))
    ax1_top.tick_params(axis='x', direction='out', length=3, pad=5, labelsize=10)
    ax1_top.spines['top'].set_visible(False)
    for label in ax1_top.get_xticklabels():
        label.set_rotation(30)
        label.set_ha('left')

    handles, labels = ax1.get_legend_handles_labels()
    handles.extend([
        Line2D([0], [0], color="#FF8C42", lw=2.5, linestyle="-", label=f"今天增幅 ({target_date})"),
        Line2D([0], [0], color="#FFB3C6", lw=2.0, linestyle="-", label=f"昨天增幅 ({yest_date})")
    ])

    fig.legend(handles=handles, loc='upper center', bbox_to_anchor=(0.5, 0.87), ncol=3, frameon=False, fontsize=11)

    fig.tight_layout(rect=[0, 0, 1, 0.85])
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"图表已生成：{save_path}")


# ============ 主流程 ============
async def run_once():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")

        async def route_handler(route):
            # 拦截字体和媒体，减少加载时间；图片保留，防止影响页面渲染
            if route.request.resource_type in ("font", "media"):
                await route.abort()
            else:
                await route.continue_()

        await context.route("**/*", route_handler)

        page1 = await context.new_page()
        page2 = await context.new_page()

        data = {"时间": datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")}
        curr_dt = datetime.strptime(data["时间"], "%Y-%m-%d %H:%M:%S")

        # 两个页面并行抓取（带重试）
        t1, pwr = await asyncio.gather(
            scrape_team_with_retry(page1, URL_TEAM1),
            scrape_power_with_retry(page2),
        )

        data["今日心动人数1"], data["团建电力值1"] = t1["今日心动人数"], t1["团建电力值"]
        data["今日心动人数2"], data["团建电力值2"] = None, None
        data["总电力值"] = pwr["总电力值"]
        await browser.close()

    append_csv(data)
    try:
        generate_chart(curr_dt.date(), CHART_FILE)
    except Exception as e:
        print(f"生成图表失败: {e}")


async def main():
    print("开始执行单次抓取...")
    try:
        await run_once()
        print("单次抓取任务完成。")
    except Exception as e:
        print(f"[抓取失败]: {e}")


if __name__ == "__main__":
    asyncio.run(main())
