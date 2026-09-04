#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hurtigruten 船票价格监控脚本
监控目标：Svolvær(SVJ) -> Tromsø(TOS)，2026年10月9日 22:15 出发那一班
一旦"每人价格"低于阈值(默认1600 kr)，就通过【微信】推送降价提醒。

============================================================
⭐ 本次更新：把原来的 ntfy.sh 推送，换成了「微信推送」
   个人微信没有官方推送接口，所以走下面两种免费中间服务之一：
   方案A（推荐，最稳、不限条数）：企业微信群机器人
   方案B（零门槛，免费约200条/天）：pushplus 推送加

------------------------------------------------------------
方案A 企业微信群机器人 设置步骤（推荐）
   1. 打开 https://work.weixin.qq.com 注册一个企业微信（个人也能免费注册）
   2. 建一个群（可以只拉自己一个人），点群右上角「...」→「添加群机器人」→ 新建
   3. 机器人建好后复制它的 Webhook 地址，形如：
      https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx-xxxx
   4. 把下面 WECOM_WEBHOOK 改成这个地址（或配置环境变量 WECOM_WEBHOOK）

------------------------------------------------------------
方案B pushplus 推送加 设置步骤（不想注册企业微信时用）
   1. 浏览器打开 https://www.pushplus.plus ，微信扫码登录
   2. 在「一对多消息」/ 个人中心里复制你的 Token
   3. 微信里关注「推送加」公众号，绑定同一个账号
   4. 把下面 PUSHPLUS_TOKEN 改成你的 token（或配置环境变量 PUSHPLUS_TOKEN）

------------------------------------------------------------
两种方案都填了就优先用企业微信；都没填则只在本机终端提示，不会报错。

使用前必读（三件事）：
1. 我（助手）这边没法自己在后台常驻运行这个脚本 —— 本文件末尾有两种免费
   的自动化方式（本机 cron/计划任务 / GitHub Actions）。
2. 这个页面是JS动态渲染的（不是纯静态HTML），所以脚本用 Playwright
   （无头浏览器）打开页面等它加载完，再从渲染后的文字里用正则找价格。
   你拿到后第一件事请【手动跑一次】确认能正确抓到当前价格，
   不要直接丢去定时任务里裸奔。
3. 如果网站有反爬虫机制（比如一直卡在验证页面），这个脚本可能会抓不到
   内容 —— 那种情况下建议改成"多手动查几次"，或者告诉我报错信息，
   我再调整。
============================================================

依赖安装（终端里执行）：
    pip install playwright --break-system-packages
    playwright install chromium
"""

import os
import re
import sys
import time
import json
import urllib.request
from datetime import datetime, timezone, timedelta

# 日志/通知统一用北京时间(UTC+8)输出
def beijing_now():
    return datetime.now(timezone(timedelta(hours=8)))

# ============ 你可能需要修改的配置 ============

# 你的Hurtigruten查询链接（原样贴的，不用改）
TARGET_URL = (
    "https://www.hurtigruten.com/da-dk/havn-til-havn/booking?searchData="
    "%7B%22locale%22%3A%22da-dk%22%2C%22searchUnit%22%3A%22day%22%2C%22startDate%22"
    "%3A%222026-10-08T10%3A43%3A23.000Z%22%2C%22returnDate%22%3A%222026-10-14T10%3A43"
    "%3A23.000Z%22%2C%22promotionCode%22%3Anull%2C%22fromPort%22%3A%22SVJ%22%2C%22toPort"
    "%22%3A%22TOS%22%2C%22vehicles%22%3A%7B%22cars%22%3A0%2C%22motorcycles%22%3A0%7D%2C"
    "%22isViaKirkenes%22%3Afalse%2C%22cabins%22%3A%5B%7B%22adults%22%3A2%2C%22children%22"
    "%3A0%2C%22infants%22%3A0%2C%22companions%22%3A0%2C%22students%22%3A0%2C%22seniors%22"
    "%3A0%2C%22militaryServicePersons%22%3A0%2C%22wheelchairs%22%3A0%2C%22pets%22%3A0%7D"
    "%5D%2C%22travelType%22%3A%22cabin%22%2C%22voyageSlug%22%3A%22%22%2C%22isRoundtrip%22"
    "%3Afalse%2C%22isDeckSpace%22%3Afalse%2C%22automaticPromotionCode%22%3Anull%7D"
)

# 要盯的那一班：出发日 09（10月），到达日 10（10月）——用来在一堆日期卡片里定位到正确的那一张
DEPARTURE_DAY = "09"
ARRIVAL_DAY = "10"

# 价格阈值：低于这个数字(挪威克朗 NOK，每人)就报警。你说的"1.6kr"我理解成 1600 kr
PRICE_THRESHOLD = 1600

# ============ 微信推送配置（方案A / 方案B 二选一）============
# 优先读取环境变量（GitHub Actions / cron 里放 Secrets 更安全）；
# 没配环境变量时，再用下面这俩默认值，请把默认值换成你自己的。

# 方案A：企业微信群机器人 Webhook 地址
WECOM_WEBHOOK = os.environ.get(
    "WECOM_WEBHOOK", "换成你的企业微信机器人Webhook地址"
)

# 方案B：pushplus 推送加 Token
PUSHPLUS_TOKEN = os.environ.get(
    "PUSHPLUS_TOKEN", "换成你的pushplus_token"
)

# （可选）是否同时保留原来的 ntfy.sh 推送。不想用就保持空字符串 ""。
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")

# 每次运行之间等待多少秒（仅当你用"死循环常驻"的跑法时才会用到，见文末方式A）
CHECK_INTERVAL_SECONDS = 30 * 60  # 30分钟查一次

# 日志文件，方便你回头看历史价格记录
LOG_FILE = "hurtigruten_price_log.txt"

# ============ 以下正常不用改 ============


def fetch_price():
    """
    用 Playwright 打开页面，等待渲染完成，
    从页面文字里用正则找到目标日期那张卡片的价格。
    返回 (价格数字_int_或_None, 原始匹配文本片段, 错误信息或None)
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        try:
            page.goto(TARGET_URL, timeout=60000, wait_until="networkidle")
        except Exception as e:
            # networkidle 有时候等不到（页面一直有后台请求），退化成等固定时间
            try:
                page.goto(TARGET_URL, timeout=60000)
            except Exception as e2:
                browser.close()
                return None, "", f"页面加载失败: {e2}"

        # 给客户端渲染多一点缓冲时间
        page.wait_for_timeout(5000)

        try:
            body_text = page.inner_text("body")
        except Exception as e:
            browser.close()
            return None, "", f"读取页面内容失败: {e}"

        browser.close()

    # 按"Vælg denne dato"这个按钮文字，把整页拆成一张张独立的日期卡片，
    # 逐张卡片检查"这张卡片第一个日期是不是09、第二个日期是不是10"，
    # 而不是用一个跨卡片的滑动窗口正则（那样容易被相邻卡片的价格串扰，已实测会出错）。
    cards = re.split(r"Vælg denne dato|Select this date|Velg denne dato", body_text)
    date_pat = re.compile(r"(\d{1,2})\s*(?:okt|oct)\.?\s*2026", re.IGNORECASE)

    target_card = None
    for card in cards:
        dates_in_card = date_pat.findall(card)
        if len(dates_in_card) < 2:
            continue
        departure_found, arrival_found = dates_in_card[0], dates_in_card[1]
        if departure_found == DEPARTURE_DAY and arrival_found == ARRIVAL_DAY:
            target_card = card
            break

    if target_card is None:
        return None, "", "没找到匹配 09→10 okt 2026 的日期卡片，页面结构/语言可能变了，需要人工检查"

    # 在这张卡片里找所有形如 "1.765 kr" / "1765 kr" / "2.353 kr" 的价格
    price_matches = re.findall(r"([\d][\d.,]*)\s*kr", target_card)
    if not price_matches:
        return None, target_card, "找到了目标卡片，但里面没抓到 kr 价格数字，可能格式变了"

    # 价格数字里的 "." 是千位分隔符（丹麦语/挪威语数字格式），去掉再转成整数
    numbers = []
    for raw in price_matches:
        cleaned = raw.replace(".", "").replace(",", "")
        try:
            numbers.append(int(cleaned))
        except ValueError:
            continue

    if not numbers:
        return None, target_card, "价格数字解析失败"

    # 划线的原价 + 折后现价通常都会一起被抓到，现价是较小的那个
    current_price = min(numbers)
    return current_price, target_card, None


def send_wechat_notification(price):
    """通过微信推送降价提醒（企业微信群机器人 优先，pushplus 兜底）。

    两个都没配置时，只在终端提示，不报错。
    """
    title = "🚢 船票降价提醒"
    now = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
    text = (
        f"{title}\n"
        f"航线：Svolvær → Tromsø（2026-10-09 出发）\n"
        f"当前价格：{price} kr/人\n"
        f"预警阈值：{PRICE_THRESHOLD} kr/人\n"
        f"查询时间：{now}"
    )

    sent = False

    # 方案A：企业微信群机器人（推荐，最稳、不限条数）
    if WECOM_WEBHOOK and not WECOM_WEBHOOK.startswith("换成"):
        try:
            payload = json.dumps({
                "msgtype": "text",
                "text": {"content": text},
            }).encode("utf-8")
            req = urllib.request.Request(WECOM_WEBHOOK, data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            resp = urllib.request.urlopen(req, timeout=15)
            body = resp.read().decode("utf-8", "ignore")
            print(f"✅ 已通过企业微信推送（{body.strip()}）")
            sent = True
        except Exception as e:
            print(f"❌ 企业微信推送失败: {e}")

    # 方案B：pushplus 推送加（兜底）
    if not sent and PUSHPLUS_TOKEN and not PUSHPLUS_TOKEN.startswith("换成"):
        try:
            payload = json.dumps({
                "token": PUSHPLUS_TOKEN,
                "title": title,
                "content": text,
                "channel": "wechat",
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://www.pushplus.plus/send", data=payload, method="POST"
            )
            req.add_header("Content-Type", "application/json")
            resp = urllib.request.urlopen(req, timeout=15)
            body = resp.read().decode("utf-8", "ignore")
            print(f"✅ 已通过 pushplus 推送（{body.strip()}）")
            sent = True
        except Exception as e:
            print(f"❌ pushplus 推送失败: {e}")

    if not sent:
        print("⚠️ 未配置任何微信推送方式（WECOM_WEBHOOK / PUSHPLUS_TOKEN 都没填），只在终端提示。")
        print("   请按文件头部说明配置其一，降价时才能真正推到微信。")


def send_ntfy_notification(price):
    """（可选）通过 ntfy.sh 推送到手机，默认关闭，配了 NTFY_TOPIC 才生效。"""
    if not NTFY_TOPIC:
        return
    url = f"https://ntfy.sh/{NTFY_TOPIC}"
    message = f"Hurtigruten Svolvær→Tromsø (10.9) 降价了！当前 {price} kr/人，低于阈值 {PRICE_THRESHOLD} kr"
    data = message.encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Title", "船票降价提醒")
    req.add_header("Priority", "high")
    req.add_header("Tags", "ship,moneybag")
    try:
        urllib.request.urlopen(req, timeout=15)
        print("✅ 已推送 ntfy 通知")
    except Exception as e:
        print(f"❌ ntfy 推送失败: {e}（但价格确实降了，请手动去看一眼）")


def log_result(price, error):
    ts = beijing_now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        if error:
            f.write(f"{ts}\t错误\t{error}\n")
        else:
            f.write(f"{ts}\t{price}\n")


def check_once():
    print(f"[{beijing_now().strftime('%Y-%m-%d %H:%M:%S')}] 正在查询价格...")
    price, block, error = fetch_price()

    if error:
        print(f"⚠️ {error}")
        if block:
            print("---- 抓到的原始文本片段（供人工核对）----")
            print(block[:500])
            print("---------------------------------------")
        log_result(None, error)
        return

    print(f"当前价格: {price} kr/人")
    log_result(price, None)

    if price < PRICE_THRESHOLD:
        print(f"🎉 低于阈值 {PRICE_THRESHOLD} kr！")
        send_wechat_notification(price)
        send_ntfy_notification(price)
    else:
        print(f"（未低于阈值 {PRICE_THRESHOLD} kr，继续观察）")


if __name__ == "__main__":
    if "--loop" in sys.argv:
        # 方式A：脚本自己死循环常驻（需要你的电脑/服务器一直开着不能关）
        print(f"进入循环监控模式，每 {CHECK_INTERVAL_SECONDS//60} 分钟查一次，Ctrl+C 停止。")
        while True:
            try:
                check_once()
            except Exception as e:
                print(f"本轮出现未预期错误: {e}")
                log_result(None, f"未预期错误: {e}")
            time.sleep(CHECK_INTERVAL_SECONDS)
    else:
        # 方式B（默认）：只跑一次，配合 cron / 计划任务 / GitHub Actions 定时调用
        check_once()
