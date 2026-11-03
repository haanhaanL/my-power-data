import asyncio
import csv
import os
import re
from datetime import datetime, timedelta, timezone
from playwright.async_api import async_playwright

# ============ 配置 ============
URL_TEAM1 = "https://y.qq.com/tmead/promotion/h5/team-activity/detail?_hidehd=1&_miniplayer=1&adtag=img_share&singerId=6665073&activityId=17936311664035&searchid=568305526626555693,&p_share=waFlj7ebDqqbQF2OOC4PsiQYhkYZ9aIFBalaGNW5ISE&ADTAG=wxfshare&hosteuin=oiEq7e6k7Knqon**"
URL_POWER = "https://y.qq.com/tmead/promotion/h5/powering/1793152992875?_hidehd=1&_miniplayer=1&adtag=share_task&singerId=6665073&p_share=w2pZEizuC5V-xTHRIivoTwD_5f_DMRfGNgTtS7isP_4&hosteuin=oiEqow6FoiCA7n**&ADTAG=wxfshare"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_FILE = os.path.join(BASE_DIR, "power_data.csv")

# 北京时间 UTC+8
BJT = timezone(timedelta(hours=8))


# ============ 抓取与存储 ============
def parse_num(text):
    if not text:
        return None
    m = re.search(r"[\d,]+", str(text))
    return int(m.group(0).replace(",", "")) if m else None


async def scrape_team(page, url):
    if not url:
        return {"今日心动人数": None, "团建电力值": None}

    # 提速拦截：不加载图片、视频、字体
    await page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ["image", "media", "font"]
        else route.continue_(),
    )
    result = {"今日心动人数": None, "团建电力值": None}

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=15000)

        # 等带有“本团今日心动”的元素出现
        heart_locator = page.locator("text=本团今日心动").first
        await heart_locator.wait_for(state="attached", timeout=8000)
        parent_text = await heart_locator.evaluate("el => el.parentElement.innerText")
        num = parse_num(parent_text)
        if num is not None and num > 0:
            result["今日心动人数"] = num

        # 等带有 千分位/千分位 格式的数字出现
        power_locator = page.locator(
            "text=/\\d{1,3}(?:,\\d{3})+\\s*\\/\\s*\\d{1,3}(?:,\\d{3})+/"
        ).first
        await power_locator.wait_for(state="attached", timeout=8000)
        power_text = await power_locator.inner_text()
        matches = re.findall(
            r"(\d{1,3}(?:,\d{3})+)\s*/\s*(\d{1,3}(?:,\d{3})+)", power_text
        )
        if matches:
            best = max(matches, key=lambda x: int(x[0].replace(",", "")))
            if int(best[0].replace(",", "")) > 1000000:
                result["团建电力值"] = int(best[0].replace(",", ""))

    except Exception as e:
        print(f"[scrape_team] 抓取超时或失败: {e}")

    return result


async def scrape_power(page):
    await page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ["image", "media", "font"]
        else route.continue_(),
    )
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


# ============ CSV 读取与时间解析 ============
def load_all_rows():
    if not os.path.exists(CSV_FILE):
        return [], []
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030"):
        try:
            with open(CSV_FILE, "r", encoding=enc, newline="") as f:
                content = list(csv.reader(f))
            return content[0], content[1:]
        except Exception:
            continue
    return [], []


def parse_dt_loose(s):
    """宽松解析 CSV 里的时间字符串，兼容有无前导零、/ 与 -、带不带秒。"""
    if not s:
        return None
    s = re.sub(r"(\d+[/-]\d+[/-]\d+)\s+(\d{1}):", r"\1 0\2:", s)
    s = s.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def find_prev_row(curr_dt, parsed_rows):
    """
    取严格早于当前时间的最后一条记录（相邻两次抓取的增量语义）。
    parsed_rows 需已按时间升序排序，元素为 (datetime, row)。
    """
    candidate = None
    for dt, r in parsed_rows:
        if dt >= curr_dt:
            break
        candidate = (dt, r)
    return candidate  # (datetime, row) 或 None


# ============ 写入 CSV（含增幅计算） ============
def append_csv(row_dict):
    file_exists = os.path.exists(CSV_FILE)
    header, all_rows = load_all_rows()
    curr_dt = datetime.strptime(row_dict["时间"], "%Y-%m-%d %H:%M:%S")

    # 23:55 之后统一归到 23:59
    if curr_dt.hour == 23 and curr_dt.minute >= 55:
        formatted_time = f"{curr_dt.year}/{curr_dt.month}/{curr_dt.day} 23:59"
        curr_dt = curr_dt.replace(hour=23, minute=59)
    else:
        formatted_time = (
            f"{curr_dt.year}/{curr_dt.month}/{curr_dt.day} "
            f"{curr_dt.hour:02d}:{curr_dt.minute:02d}"
        )

    # 解析历史行，按时间升序
    parsed_rows = []
    for r in all_rows:
        if not r or len(r) < 10:
            continue
        dt = parse_dt_loose(r[0])
        if dt:
            parsed_rows.append((dt, r))
    parsed_rows.sort(key=lambda x: x[0])

    # 回溯：严格早于当前时间的最后一条
    prev = find_prev_row(curr_dt, parsed_rows)
    prev_dt, prev_row = (prev[0], prev[1]) if prev else (None, None)

    def calc_delta(curr_val, col_idx):
        if curr_val is None or curr_val == "":
            return ""
        if prev_row is None or prev_dt is None:
            return ""
        # 跨天：今日心动人数会重置，增幅无意义，留空
        if prev_dt.date() != curr_dt.date():
            return ""
        try:
            prev_val_str = prev_row[col_idx]
            if prev_val_str == "" or prev_val_str is None:
                return ""
            return int(curr_val) - int(prev_val_str)
        except (ValueError, TypeError, IndexError):
            return ""

    row = [
        formatted_time,
        row_dict["今日心动人数1"] or "",
        calc_delta(row_dict["今日心动人数1"], 1),
        row_dict["团建电力值1"] or "",
        calc_delta(row_dict["团建电力值1"], 3),
        row_dict["今日心动人数2"] or "",
        calc_delta(row_dict["今日心动人数2"], 5),
        row_dict["团建电力值2"] or "",
        calc_delta(row_dict["团建电力值2"], 7),
        row_dict["总电力值"] or "",
        calc_delta(row_dict["总电力值"], 9),
    ]

    with open(CSV_FILE, "a", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(
                [
                    "时间",
                    "心动人数1",
                    "增幅",
                    "团建电力值1",
                    "增幅",
                    "心动人数2",
                    "增幅",
                    "团建电力值2",
                    "增幅",
                    "总电力值",
                    "增幅",
                ]
            )
        writer.writerow(row)

    if prev_dt is None:
        prev_info = "无"
    elif prev_dt.date() != curr_dt.date():
        prev_info = f"{prev_dt}（跨天，增幅留空）"
    else:
        prev_info = str(prev_dt)
    print(f"✅ [{formatted_time}] 写入完成。prev_row={prev_info}\n")


# ============ 主流程 ============
async def run_once():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
            "Mobile/15E148 Safari/604.1"
        )

        page1 = await context.new_page()
        page2 = await context.new_page()

        data = {"时间": datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")}

        # 并行抓取
        t1, pwr = await asyncio.gather(
            scrape_team(page1, URL_TEAM1),
            scrape_power(page2),
        )

        data["今日心动人数1"] = t1["今日心动人数"]
        data["团建电力值1"] = t1["团建电力值"]
        data["今日心动人数2"] = None
        data["团建电力值2"] = None
        data["总电力值"] = pwr["总电力值"]

        await browser.close()

    append_csv(data)


async def main():
    print("开始执行单次抓取...")
    try:
        await run_once()
        print("单次抓取任务完成。")
    except Exception as e:
        print(f"[抓取失败]: {e}")


if __name__ == "__main__":
    asyncio.run(main())
