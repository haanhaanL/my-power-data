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
from matplotlib.lines import Line2D
import matplotlib.font_manager

matplotlib.font_manager._load_fontmanager(try_read_cache=False)

# ============ 配置 ============
URL_TEAM1 = "https://y.qq.com/tmead/promotion/h5/team-activity/detail?_hidehd=1&_miniplayer=1&adtag=sszd&singerId=6665073&activityId=17931826095559&searchid=113943832761792946,&hosteuin=ow657wSlownlon**&ADTAG=wxfshare"
URL_POWER = "https://y.qq.com/tmead/promotion/h5/powering/1793152992875?_hidehd=1&_miniplayer=1&adtag=share_task&singerId=6665073&p_share=w2pZEizuC5V-xTHRIivoTwD_5f_DMRfGNgTtS7isP_4&hosteuin=oiEqow6FoiCA7n**&ADTAG=wxfshare"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "power_data.csv")
CHART_FILE = os.path.join(BASE_DIR, "chart.png")
BJT = timezone(timedelta(hours=8))

# ============ 核心抓取函数 ============
def parse_num(text):
    if not text: return None
    m = re.search(r"[\d,]+", str(text))
    return int(m.group(0).replace(",", "")) if m else None

async def scrape_team(page, url):
    if not url: return {"今日心动人数": None, "团建电力值": None}
    # 拦截图片、字体、媒体资源，大幅提速页面加载
    await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())
    
    print("[scrape_team] 页面加载中...")
    await page.goto(url, wait_until="networkidle", timeout=60000)
    
    result = {"今日心动人数": None, "团建电力值": None}
    
    # 显式轮询：最多等 20 秒，每秒查一次
    for i in range(20):
        try:
            body_text = await page.locator("body").inner_text()
            
            # 1. 抓团建电力值（通常带有逗号且较大）
            matches = re.findall(r"(\d{1,3}(?:,\d{3})+)\s*/\s*(\d{1,3}(?:,\d{3})+)", body_text)
            if matches and not result["团建电力值"]:
                best = max(matches, key=lambda x: int(x[0].replace(",", "")))
                result["团建电力值"] = int(best[0].replace(",", ""))
                print(f"[scrape_team] 捕获团建电力值: {result['团建电力值']}")
                
            # 2. 抓今日心动人数
            heart_ele = page.locator("text=本团今日心动").first
            if await heart_ele.count() > 0:
                parent_text = await heart_ele.evaluate("el => el.parentElement.innerText")
                # 如果提取到的是完整数字
                num = parse_num(parent_text)
                if num and not result["今日心动人数"]:
                    result["今日心动人数"] = num
                    print(f"[scrape_team] 捕获心动人数: {result['今日心动人数']}")
            
            # 如果两个数据都拿到了，提前结束等待
            if result["今日心动人数"] and result["团建电力值"]:
                break
        except Exception as e:
            print(f"[scrape_team] 轮询异常: {e}")
        
        await page.wait_for_timeout(1000)
        
    return result

async def scrape_power(page):
    await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())
    
    print("[scrape_power] 页面加载中...")
    await page.goto(URL_POWER, wait_until="networkidle", timeout=60000)
    
    result = {"总电力值": None}
    
    for i in range(20):
        try:
            body_text = await page.locator("body").inner_text()
            # 找页面上最大的带逗号数字
            nums = re.findall(r"\d{1,3}(?:,\d{3})+", body_text)
            if nums:
                max_num = max(int(n.replace(",", "")) for n in nums)
                # 防止抓到无关的小数字（比如 10,000 这种），设定阈值 1亿
                if max_num > 100000000:
                    result["总电力值"] = max_num
                    print(f"[scrape_power] 捕获总电力值: {result['总电力值']}")
                    break
        except Exception as e:
            print(f"[scrape_power] 轮询异常: {e}")
        
        await page.wait_for_timeout(1000)
        
    return result

# ============ 数据存储与图表 ============
def load_all_rows():
    if not os.path.exists(CSV_FILE): return [], []
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            with open(CSV_FILE, "r", encoding=enc, newline="") as f:
                content = list(csv.reader(f))
            return content[0], content[1:]
        except: continue
    return [], []

def parse_dt_loose(s):
    if not s: return None
    s = re.sub(r'(\d+[/-]\d+[/-]\d+)\s+(\d{1}):', r'\1 0\2:', s)
    s = s.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try: return datetime.strptime(s, fmt)
        except: pass
    return None

def append_csv(row_dict):
    file_exists = os.path.exists(CSV_FILE)
    header, all_rows = load_all_rows()
    curr_dt = datetime.strptime(row_dict["时间"], "%Y-%m-%d %H:%M:%S")
    formatted_time = f"{curr_dt.year}/{curr_dt.month}/{curr_dt.day} {curr_dt.hour:02d}:{curr_dt.minute:02d}"

    prev_row = None
    for r in reversed(all_rows):
        if len(r) >= 10:
            row_dt = parse_dt_loose(r[0])
            if row_dt and row_dt < curr_dt:
                prev_row = r
                break

    def calc_delta(curr_val, col_idx):
        if curr_val is None or prev_row is None: return ""
        try:
            prev_val_str = prev_row[col_idx]
            if prev_val_str == "" or prev_val_str is None: return ""
            return int(curr_val) - int(prev_val_str)
        except: return ""

    row = [
        formatted_time,
        row_dict["今日心动人数1"] or "", calc_delta(row_dict["今日心动人数1"], 1),
        row_dict["团建电力值1"] or "", calc_delta(row_dict["团建电力值1"], 3),
        "", "", "", "",
        row_dict["总电力值"] or "", calc_delta(row_dict["总电力值"], 9),
    ]

    with open(CSV_FILE, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(["时间", "心动人数1", "增幅", "团建电力值1", "增幅", "心动人数2", "增幅", "团建电力值2", "增幅", "总电力值", "增幅"])
        writer.writerow(row)
    print(f"✅ [{formatted_time}] 写入完成。\n")

def generate_chart(target_date, save_path):
    header, rows = load_all_rows()
    if not rows: return

    data_map = {}
    for r in rows:
        if len(r) < 10: continue
        dt = parse_dt_loose(r[0])
        if dt is None: continue

        target_date_str = dt.strftime('%Y-%m-%d')
        float_hour = dt.hour + dt.minute / 60.0

        rec = {"dt": dt, "hour": float_hour, "heart": to_float(r[1]), "delta": to_float(r[4]), "team1": to_float(r[3])}
        key = f"{target_date_str}_{dt.hour:02d}{dt.minute:02d}"
        data_map[key] = rec

    today_str = target_date.strftime("%Y-%m-%d")
    yest_date = target_date - timedelta(days=1)
    yest_str = yest_date.strftime("%Y-%m-%d")

    plt.rcParams["font.sans-serif"] = ["WenQuanYi Zen Hei", "WenQuanYi Micro Hei", "Microsoft YaHei", "SimHei", "Arial Unicode MS", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    fig, axes = plt.subplots(2, 1, figsize=(14, 10), sharex=True)
    fig.suptitle(f"本周数据对比 (结算日: {target_date})", fontsize=18, fontweight="bold", color="#D35400", y=0.96)

    today_points = sorted([v for k, v in data_map.items() if k.startswith(today_str)], key=lambda x: x["dt"])
    yest_points = sorted([v for k, v in data_map.items() if k.startswith(yest_str)], key=lambda x: x["dt"])

    today_delta_sum = sum(p["delta"] for p in today_points if p["delta"] is not None)
    yest_delta_sum = sum(p["delta"] for p in yest_points if p["delta"] is not None)

    fig.text(0.5, 0.91, f"昨日增幅总和: {fmt_w(yest_delta_sum)}   |   今日增幅总和: {fmt_w(today_delta_sum)}",
             ha='center', fontsize=14, fontweight='bold', color='#555555',
             bbox=dict(facecolor='#FDF2E9', edgecolor='#FFB27A', boxstyle='round,pad=0.5'))

    # 子图1
    ax1 = axes[0]
    if yest_points:
        ax1.plot([p["hour"] for p in yest_points], [p["heart"] for p in yest_points], marker="o", markersize=3, color="#FFB3C6", linewidth=1.5, label="昨天")
    if today_points:
        ax1.plot([p["hour"] for p in today_points], [p["heart"] for p in today_points], marker="o", markersize=4, color="#FF8C42", linewidth=2, label="今天")

    ax1.set_title("心动人数1 对比", fontsize=14, fontweight="bold", pad=10)
    ax1.grid(axis='y', linestyle="--", alpha=0.4)
    ax1.set_xlim(0, 24)
    ax1.legend()

    # 子图2
    ax2 = axes[1]
    if yest_points:
        x = [p["hour"] for p in yest_points if p["delta"] is not None]
        y = [p["delta"] for p in yest_points if p["delta"] is not None]
        ax2.plot(x, y, marker="o", markersize=3, color="#FFB3C6", linewidth=1.5)
    if today_points:
        x = [p["hour"] for p in today_points if p["delta"] is not None]
        y = [p["delta"] for p in today_points if p["delta"] is not None]
        ax2.plot(x, y, marker="o", markersize=4, color="#FF8C42", linewidth=2)

    ax2.set_title("团建电力值1 增幅对比", fontsize=14, fontweight="bold", pad=10)
    ax2.grid(axis='y', linestyle="--", alpha=0.4)
    ax2.set_xlim(0, 24)
    ax2.set_xlabel("时段", fontsize=11)

    def time_formatter(x, pos):
        if x == 24: return "24:00"
        return f"{int(x):02d}:00"
    formatter = ticker.FuncFormatter(time_formatter)
    ax2.xaxis.set_major_formatter(formatter)
    ax2.xaxis.set_major_locator(ticker.MultipleLocator(2))
    
    ax1_top = ax1.twiny()
    ax1_top.set_xlim(0, 24)
    ax1_top.xaxis.set_major_formatter(formatter)
    ax1_top.xaxis.set_major_locator(ticker.MultipleLocator(2))

    # 添加时间戳，强制图片变动
    now_str = datetime.now(BJT).strftime('%Y-%m-%d %H:%M:%S')
    fig.text(0.98, 0.01, f"生成时间: {now_str}", ha='right', va='bottom', fontsize=10, color='gray', alpha=0.7)

    fig.tight_layout(rect=[0, 0, 1, 0.85])
    fig.savefig(save_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"图表已生成：{save_path}")

def to_float(v):
    try: return float(v) if v not in (None, "") else None
    except: return None

def fmt_w(v):
    if v is None: return ""
    sign = "-" if v < 0 else ""
    w = abs(v) / 10000.0
    return f"{sign}{int(round(w))}w" if abs(w - round(w)) < 1e-9 else f"{sign}{w:.1f}w"

# ============ 主流程 ============
async def run_once():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1")

        page1 = await context.new_page()
        page2 = await context.new_page()

        data = {"时间": datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")}
        curr_dt = datetime.strptime(data["时间"], "%Y-%m-%d %H:%M:%S")

        t1, pwr = await asyncio.gather(
            scrape_team(page1, URL_TEAM1),
            scrape_power(page2),
        )

        data["今日心动人数1"], data["团建电力值1"] = t1["今日心动人数"], t1["团建电力值"]
        data["总电力值"] = pwr["总电力值"]
        await browser.close()

    append_csv(data)
    try:
        generate_chart(curr_dt.date(), CHART_FILE)
    except Exception as e:
        print(f"生成图表失败: {e}")

async def main():
    print("开始执行单次抓取...")
    await run_once()
    print("单次抓取任务完成。")

if __name__ == "__main__":
    asyncio.run(main())
