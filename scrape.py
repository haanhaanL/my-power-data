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


# ============ 工具函数 ============
def parse_num(text):
    if not text:
        return None
    m = re.search(r"[\d,]+", str(text))
    return int(m.group(0).replace(",", "")) if m else None


async def block_unnecessary_resources(page):
    """提速拦截：不加载图片、视频、字体"""
    await page.route(
        "**/*",
        lambda route: route.abort()
        if route.request.resource_type in ["image", "media", "font"]
        else route.continue_(),
    )


# ============ 抓取逻辑（带重试与防超时） ============
async def scrape_team(page, url):
    if not url:
        return {"今日心动人数": None, "团建电力值": None}

    await block_unnecessary_resources(page)
    result = {"今日心动人数": None, "团建电力值": None}

    # 最多重试 3 次
    for attempt in range(3):
        try:
            # 使用 domcontentloaded 防止统计脚本挂起导致超时
            await page.goto(url, wait_until="domcontentloaded", timeout=20000)

            # 1. 抓取“今日心动人数”
            heart_locator = page.locator("text=本团今日心动").first
            await heart_locator.wait_for(state="attached", timeout=15000) # 最长等15秒
            parent_text = await heart_locator.evaluate("el => el.parentElement.innerText")
            num = parse_num(parent_text)
            
            if num is not None and num > 0:
                result["今日心动人数"] = num

            # 2. 抓取“团建电力值”
            power_locator = page.locator("text=/\\d{1,3}(?:,\\d{3})+\\s*\\/\\s*\\d{1,3}(?:,\\d{3})+/").first
            await power_locator.wait_for(state="attached", timeout=15000)
            power_text = await power_locator.inner_text()
            matches = re.findall(r"(\d{1,3}(?:,\d{3})+)\s*/\s*(\d{1,3}(?:,\d{3})+)", power_text)
            
            if matches:
                best = max(matches, key=lambda x: int(x[0].replace(",", "")))
                if int(best[0].replace(",", "")) > 1000000:
                    result["团建电力值"] = int(best[0].replace(",", ""))

            # 判断是否成功抓到有效数据
            if result["今日心动人数"] and result["团建电力值"]:
                print(f"[scrape_team] 第 {attempt+1} 次尝试成功!")
                break  # 数据完整，跳出重试
            else:
                print(f"[scrape_team] 第 {attempt+1} 次尝试数据为空，刷新重试...")
                await page.reload(wait_until="domcontentloaded", timeout=15000)
                await asyncio.sleep(2) # 刷新后稍作缓冲

        except Exception as e:
            print(f"[scrape_team] 第 {attempt+1} 次尝试超时或失败: {e}")
            await asyncio.sleep(2)

    return result


async def scrape_power(page):
    await block_unnecessary_resources(page)
    result = {"总电力值": None}

    for attempt in range(3):
        try:
            await page.goto(URL_POWER, wait_until="domcontentloaded", timeout=20000)
            # 等待包含千分位的数字出现
            await page.wait_for_selector("text=/\\d{1,3}(?:,\\d{3})+/", timeout=15000)

            body_text = await page.locator("body").inner_text()
            nums = re.findall(r"\d{1,3}(?:,\d{3})+", body_text)
            if nums:
                max_num = max(int(n.replace(",", "")) for n in nums)
                # 总电力值通常极大，超过一亿
                if max_num > 100000000:
                    result["总电力值"] = max_num
                    print(f"[scrape_power] 第 {attempt+1} 次尝试成功!")
                    break
            print(f"[scrape_power] 第 {attempt+1} 次尝试未拿到合适数值，刷新重试...")
            await page.reload(wait_until="domcontentloaded", timeout=15000)
            await asyncio.sleep(2)

        except Exception as e:
            print(f"[scrape_power] 第 {attempt+1} 次尝试超时或失败: {e}")
            await asyncio.sleep(2)

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
    """宽松解析时间字符串，兼容 2026/11/3 09:00 格式"""
    if not s: return None
    s = re.sub(r"(\d+[/-]\d+[/-]\d+)\s+(\d{1}):", r"\1 0\2:", s)
    s = s.strip().replace("/", "-")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None


def find_prev_row(curr_dt, parsed_rows, col_idx):
    """
    寻找严格早于当前时间，且对应列有有效数据的最近一条记录
    """
    candidate = None
    for dt, r in parsed_rows:
        if dt >= curr_dt:
            break
        # 检查该行的目标列是否有有效数据
        if len(r) > col_idx and r[col_idx] not in ("", None):
            try:
                int(r[col_idx])
                candidate = (dt, r) # 记录这行，继续往下找，直到遇到当前时间
            except ValueError:
                pass
    return candidate


# ============ 写入 CSV（含跨天增幅计算） ============
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

    # 解析历史行
    parsed_rows = []
    for r in all_rows:
        if not r or len(r) < 10:
            continue
        dt = parse_dt_loose(r[0])
        if dt:
            parsed_rows.append((dt, r))
    parsed_rows.sort(key=lambda x: x[0])

    def calc_delta(curr_val, col_idx):
        if curr_val is None or curr_val == "":
            return ""
        
        # 每一列单独去寻找最近一条有有效数据的记录
        prev = find_prev_row(curr_dt, parsed_rows, col_idx)
        if prev is None:
            return ""
            
        prev_dt, prev_row = prev
        
        # 跨天增幅留空
        if prev_dt.date() != curr_dt.date():
            return ""
            
        try:
            prev_val_str = prev_row[col_idx]
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
            writer.writerow([
                "时间", "心动人数1", "增幅", "团建电力值1", "增幅", 
                "心动人数2", "增幅", "团建电力值2", "增幅", "总电力值", "增幅"
            ])
        writer.writerow(row)

    print(f"✅ [{formatted_time}] 写入完成。\n")


# ============ 主流程 ============
async def run_once():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        
        # === 深度伪装真实 iPhone 环境 ===
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
            viewport={"width": 390, "height": 844},
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        
        # 禁用 WebRTC，防止真实 IP 泄露导致被拒
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            delete navigator.__proto__.webdriver;
            window.RTCPeerConnection = function() {};
            window.RTCPeerConnection.prototype.createOffer = function() { return Promise.resolve(); };
        """)

        page1 = await context.new_page()
        page2 = await context.new_page()

        data = {"时间": datetime.now(BJT).strftime("%Y-%m-%d %H:%M:%S")}

        print("开始抓取数据...")
        # 并行抓取（两个页面各自带重试逻辑）
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

    # 如果全没抓到，就不写了（避免污染 CSV）
    if not data["今日心动人数1"] and not data["团建电力值1"] and not data["总电力值"]:
        print("❌ 本次抓取所有数据均失败，放弃写入 CSV 以保证数据纯净。")
        return

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
