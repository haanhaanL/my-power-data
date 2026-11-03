import asyncio
import csv
import os
import re
from datetime import datetime, timedelta, timezone
from playwright.async_api import async_playwright

# ============ 配置 ============
URL_TEAM1 = "https://y.qq.com/tmead/promotion/h5/team-activity/detail?_hidehd=1&_miniplayer=1&adtag=sszd&singerId=6665073&activityId=17931826095559&searchid=113943832761792946,&hosteuin=ow657wSlownlon**&ADTAG=wxfshare"
URL_POWER = "https://y.qq.com/tmead/promotion/h5/powering/1793152992875?_hidehd=1&_miniplayer=1&adtag=share_task&singerId=6665073&p_share=w2pZEizuC5V-xTHRIivoTwD_5f_DMRfGNgTtS7isP_4&hosteuin=oiEqow6FoiCA7n**&ADTAG=wxfshare"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "power_data.csv")
HTML_FILE = os.path.join(BASE_DIR, "index.html")

BJT = timezone(timedelta(hours=8))


# ============ 抓取与存储 ============
def parse_num(text):
    if not text: return None
    m = re.search(r"[\d,]+", str(text))
    return int(m.group(0).replace(",", "")) if m else None


async def scrape_team(page, url):
    if not url: return {"今日心动人数": None, "团建电力值": None}
    
    await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())
    result = {"今日心动人数": None, "团建电力值": None}

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        
        heart_locator = page.locator("text=本团今日心动").first
        await heart_locator.wait_for(state="attached", timeout=8000) 
        parent_text = await heart_locator.evaluate("el => el.parentElement.innerText")
        num = parse_num(parent_text)
        if num is not None and num > 0:
            result["今日心动人数"] = num
            
        power_locator = page.locator("text=/\\d{1,3}(?:,\\d{3})+\\s*\\/\\s*\\d{1,3}(?:,\\d{3})+/").first
        await power_locator.wait_for(state="attached", timeout=8000)
        power_text = await power_locator.inner_text()
        matches = re.findall(r"(\d{1,3}(?:,\d{3})+)\s*/\s*(\d{1,3}(?:,\d{3})+)", power_text)
        if matches:
            best = max(matches, key=lambda x: int(x[0].replace(",", "")))
            if int(best[0].replace(",", "")) > 1000000:
                result["团建电力值"] = int(best[0].replace(",", ""))

    except Exception as e:
        print(f"[scrape_team] 抓取超时或失败: {e}")

    return result


async def scrape_power(page):
    await page.route("**/*", lambda route: route.abort() if route.request.resource_type in ["image", "media", "font"] else route.continue_())
    result = {"总电力值": None}
    
    try:
        await page.goto(URL_POWER, wait_until="domcontentloaded", timeout=15000)
        await page.wait_for_selector("text=/\\d{1,3}(?:,\\d{3})+/", timeout=8000)
        
        body_text = await page.locator("body").inner_text()
        nums = re.findall(r"\d{1,3}(?:,\d{3})+", body_text)
        if nums:
            max_num = max(int(n.replace(",", "")) for n in nums)
            if max_num > 100000000:
                result["总电力值"] = max_num
    except Exception as e:
        print(f"[scrape_power] 抓取超时或失败: {e}")
        
    return result


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
        try: return datetime.strptime(s, fmt)
        except: pass
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

    is_hourly = (curr_dt.minute <= 10 or curr_dt.minute >= 55)
    prev_row = get_prev_hour_record(curr_dt, all_rows) if is_hourly else None

    def calc_delta(curr_val, col_idx):
        if not is_hourly or curr_val is None or prev_row is None: return ""
        try:
            prev_val_str = prev_row[col_idx]
            if prev_val_str == "" or prev_val_str is None: return ""
            return int(curr_val) - int(prev_val_str)
        except: return ""

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
            writer.writerow(["时间", "心动人数1", "增幅", "团建电力值1", "增幅", "心动人数2", "增幅", "团建电力值2", "增幅", "总电力值", "增幅"])
        writer.writerow(row)
    print(f"✅ [{formatted_time}] 写入完成。\n")


# ============ 晚间截图逻辑 ============
async def capture_html_screenshot(page, save_path):
    """用 Playwright 打开本地生成的 index.html 并截图"""
    if not os.path.exists(HTML_FILE):
        print("⚠️ 找不到 index.html，跳过截图")
        return
    
    print(f"🌙 正在打开网页准备截图 -> {os.path.basename(save_path)}")
    file_url = f"file://{HTML_FILE}"
    await page.goto(file_url, wait_until="networkidle")
    
    # 等待 ECharts 渲染完成
    await page.wait_for_timeout(3000)
    
    # 截图整个页面
    await page.screenshot(path=save_path, full_page=True)
    print(f"📸 网页截图已保存：{save_path}")


# ============ 主流程 ============
async def run_once():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
            viewport={"width": 1200, "height": 800}
        )

        page1 = await context.new_page()
        page2 = await context.new_page()

        now_bjt = datetime.now(BJT)
        data = {"时间": now_bjt.strftime("%Y-%m-%d %H:%M:%S")}
        curr_dt = datetime.strptime(data["时间"], "%Y-%m-%d %H:%M:%S")

        t1, pwr = await asyncio.gather(
            scrape_team(page1, URL_TEAM1),
            scrape_power(page2),
        )

        data["今日心动人数1"], data["团建电力值1"] = t1["今日心动人数"], t1["团建电力值"]
        data["今日心动人数2"], data["团建电力值2"] = None, None
        data["总电力值"] = pwr["总电力值"]

        # 写入 CSV
        append_csv(data)

        # ⭐ 测试逻辑：将时间改为 9:30 到 9:40 之间触发
        if now_bjt.hour == 9 and 30 <= now_bjt.minute <= 40:
            print("🧪 检测到测试时段 (9:30-9:40)，开始网页截图...")
            
            # 图片命名规则保持不变（依然是 chart_年-月-日.png）
            date_str = now_bjt.strftime("%Y-%m-%d")
            chart_file_with_date = os.path.join(BASE_DIR, f"chart_{date_str}.png")
            
            page3 = await context.new_page()
            try:
                await capture_html_screenshot(page3, chart_file_with_date)
            except Exception as e:
                print(f"截图失败: {e}")
            finally:
                await page3.close()
        else:
            print("⏳ 当前不在测试时段 (9:30-9:40)，跳过截图。")

        await browser.close()


async def main():
    print("开始执行单次抓取...")
    try:
        await run_once()
        print("单次抓取任务完成。")
    except Exception as e:
        print(f"[抓取失败]: {e}")


if __name__ == "__main__":
    asyncio.run(main())
