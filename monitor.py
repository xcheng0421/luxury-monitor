#!/usr/bin/env python3
"""
奢侈品补货监控（Van Cleef & Arpels 等页面通用）

判断逻辑：
  页面 HTML 里“有货按钮”和“缺货按钮”可能一直同时存在，只是其中一个被隐藏。
  所以这里不是搜文字，而是找到匹配文字的元素后，逐个检查它是否“真的看得见”：
  display / visibility / opacity / hidden 属性 / 元素尺寸，沿着父级一路检查。

结果与退出码：
  0  正常完成（有货且已发送通知，或缺货）
  1  两个按钮都看不到（页面被拦截、改版、没加载出来等），并保存截图
  2  有货，但所有通知渠道都发送失败 / 测试模式发送失败

用法：
  python monitor.py                # 正常检测
  python monitor.py --test-email   # 只发一封测试邮件，不打开网页
  python monitor.py --test-bark    # 只发一条 Bark 紧急推送，不打开网页
  python monitor.py --dry-run      # 检测但不发送通知
  python monitor.py --headed       # 本地调试时显示浏览器窗口

需要的环境变量（在 GitHub 里放在 Secrets 中，代码里不写任何密码）：
  BARK_KEY            Bark 的设备 key
  GMAIL_USER          用来发信的 Gmail 地址
  GMAIL_APP_PASSWORD  Gmail 应用专用密码（16 位）
  MAIL_TO             收件邮箱（可选，不填则发给 GMAIL_USER 自己）
可选（非敏感）配置：
  PRODUCT_URL, PRODUCT_NAME, IN_STOCK_TEXT, OUT_OF_STOCK_TEXT,
  WAIT_SECONDS, SCREENSHOT_PATH, BARK_SERVER
"""
from __future__ import annotations

import argparse
import json
import os
import re
import smtplib
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

# ----------------------------- 配置（非敏感） -----------------------------
DEFAULT_URL = (
    "https://www.vancleefarpels.com/us/en/collections/jewelry/alhambra/"
    "vcaro8dd00---sweet-alhambra-bracelet-6-motifs.html"
)
PRODUCT_URL = os.environ.get("PRODUCT_URL") or DEFAULT_URL
PRODUCT_NAME = os.environ.get("PRODUCT_NAME") or "Sweet Alhambra bracelet, 6 motifs"
# 注意：下面两个文字请以网页上真实按钮文字为准（教程里有查看方法）
IN_STOCK_TEXT = os.environ.get("IN_STOCK_TEXT") or "ADD TO BAG"
OUT_OF_STOCK_TEXT = os.environ.get("OUT_OF_STOCK_TEXT") or "ORDER BY PHONE"
WAIT_SECONDS = int(os.environ.get("WAIT_SECONDS") or "30")
SCREENSHOT_PATH = Path(os.environ.get("SCREENSHOT_PATH") or "screenshot.png")
BARK_SERVER = (os.environ.get("BARK_SERVER") or "https://api.day.app").rstrip("/")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# 可能是“按钮”的元素
BUTTON_SELECTOR = "button, a, [role='button'], input[type='submit'], input[type='button']"

# 判断一个元素是否真的可见（沿父级检查）
VISIBLE_JS = """
(el) => {
  const r = el.getBoundingClientRect();
  if (r.width < 1 || r.height < 1) return false;
  for (let n = el; n && n.nodeType === 1; n = n.parentElement) {
    if (n.hasAttribute('hidden')) return false;
    const s = window.getComputedStyle(n);
    if (s.display === 'none') return false;
    if (s.visibility === 'hidden' || s.visibility === 'collapse') return false;
    if (parseFloat(s.opacity) === 0) return false;
  }
  return true;
}
"""


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def get_env(name: str, required: bool = True) -> str:
    value = (os.environ.get(name) or "").strip()
    if required and not value:
        raise RuntimeError(f"缺少环境变量 {name}（请在 GitHub Secrets 中配置）")
    return value


# ----------------------------- 网页检测 -----------------------------
def text_pattern(text: str) -> re.Pattern:
    """把 'ADD TO BAG' 变成不区分大小写、空白宽松的正则。"""
    words = text.split()
    return re.compile(r"\s+".join(re.escape(w) for w in words), re.I)


def count_buttons(page, text: str) -> tuple[int, int]:
    """返回 (匹配该文字的元素总数, 其中真正可见的数量)。"""
    loc = page.locator(BUTTON_SELECTOR).filter(has_text=text_pattern(text))
    total = loc.count()
    visible = 0
    for i in range(total):
        try:
            if loc.nth(i).evaluate(VISIBLE_JS):
                visible += 1
        except Exception:
            pass  # 元素在检查过程中消失，忽略
    return total, visible


def check_page(headed: bool = False) -> str:
    """返回 'in_stock' | 'out_of_stock' | 'unknown'，并总是尝试保存截图。"""
    from playwright.sync_api import sync_playwright

    status = "unknown"
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            locale="en-US",
            timezone_id="America/Chicago",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        try:
            log(f"打开页面：{PRODUCT_URL}")
            page.goto(PRODUCT_URL, wait_until="domcontentloaded", timeout=60_000)
            try:
                log(f"页面标题：{page.title()!r}")
            except Exception:
                pass

            deadline = time.time() + WAIT_SECONDS
            in_t = in_v = out_t = out_v = 0
            while True:
                in_t, in_v = count_buttons(page, IN_STOCK_TEXT)
                out_t, out_v = count_buttons(page, OUT_OF_STOCK_TEXT)

                if in_v > 0:
                    status = "in_stock"
                    break
                if out_v > 0:
                    # 防止页面加载时先短暂显示“缺货”再变成“有货”，多等一下再确认
                    page.wait_for_timeout(2500)
                    in_t, in_v = count_buttons(page, IN_STOCK_TEXT)
                    out_t, out_v = count_buttons(page, OUT_OF_STOCK_TEXT)
                    status = "in_stock" if in_v > 0 else "out_of_stock"
                    break
                if time.time() >= deadline:
                    break
                page.wait_for_timeout(1000)

            log(f"[{IN_STOCK_TEXT}] 匹配 {in_t} 个，可见 {in_v} 个")
            log(f"[{OUT_OF_STOCK_TEXT}] 匹配 {out_t} 个，可见 {out_v} 个")
        except Exception as exc:  # 打不开页面、超时等
            log(f"访问页面出错：{exc!r}")
        finally:
            try:
                page.screenshot(path=str(SCREENSHOT_PATH))
                log(f"已保存截图：{SCREENSHOT_PATH}")
            except Exception as exc:
                log(f"保存截图失败：{exc!r}")
            browser.close()
    return status


# ----------------------------- 通知 -----------------------------
def send_bark(title: str, body: str) -> bool:
    """Bark 紧急通知：level=critical，call=1（静音/专注/睡眠模式也会响，并重复响铃）。"""
    try:
        key = get_env("BARK_KEY")
        payload = {
            "device_key": key,
            "title": title,
            "body": body,
            "level": "critical",
            "call": "1",
            "volume": 10,
            "sound": "alarm",
            "group": "restock",
            "url": PRODUCT_URL,
        }
        req = urllib.request.Request(
            f"{BARK_SERVER}/push",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace") or "{}")
        if data.get("code") == 200:
            log("Bark 推送成功")
            return True
        log(f"Bark 推送失败：{data}")
    except urllib.error.HTTPError as exc:
        log(f"Bark 推送失败：HTTP {exc.code} {exc.read().decode('utf-8', 'replace')[:200]}")
    except Exception as exc:
        log(f"Bark 推送失败：{exc!r}")
    return False


def send_email(subject: str, body: str, attach_screenshot: bool = False) -> bool:
    """通过 Gmail SMTP（应用专用密码）发邮件。"""
    try:
        user = get_env("GMAIL_USER")
        password = get_env("GMAIL_APP_PASSWORD").replace(" ", "")
        to_addr = get_env("MAIL_TO", required=False) or user

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = user
        msg["To"] = to_addr
        msg.set_content(body)

        if attach_screenshot and SCREENSHOT_PATH.exists():
            msg.add_attachment(
                SCREENSHOT_PATH.read_bytes(),
                maintype="image",
                subtype="png",
                filename=SCREENSHOT_PATH.name,
            )

        context = ssl.create_default_context()
        last_exc: Exception | None = None
        # 最多重试 3 轮；每轮先试 465(SSL)，失败再试 587(STARTTLS)
        for attempt in range(1, 4):
            for mode in ("465-SSL", "587-STARTTLS"):
                stage = "connect"  # 记录失败发生在哪一步（不会打印任何密码）
                try:
                    if mode == "465-SSL":
                        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as smtp:
                            stage = "login"
                            smtp.login(user, password)
                            stage = "send"
                            smtp.send_message(msg)
                    else:
                        with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as smtp:
                            stage = "ehlo"
                            smtp.ehlo()
                            stage = "starttls"
                            smtp.starttls(context=context)
                            stage = "ehlo2"
                            smtp.ehlo()
                            stage = "login"
                            smtp.login(user, password)
                            stage = "send"
                            smtp.send_message(msg)
                    log(f"邮件发送成功（{mode}）-> {to_addr}")
                    return True
                except smtplib.SMTPAuthenticationError as exc:
                    # 账号或应用专用密码错误，重试没有意义
                    log(f"邮件发送失败：Gmail 拒绝登录，请检查 GMAIL_USER / GMAIL_APP_PASSWORD：{exc!r}")
                    return False
                except Exception as exc:
                    last_exc = exc
                    log(f"邮件发送失败（第 {attempt} 轮，{mode}，失败阶段={stage}）：{exc!r}")
            time.sleep(3 * attempt)
        log(f"邮件多次重试后仍然失败：{last_exc!r}")
        return False
    except Exception as exc:
        log(f"邮件发送失败：{exc!r}")
        return False


# ----------------------------- 主流程 -----------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="奢侈品补货监控")
    parser.add_argument("--test-email", action="store_true", help="只发一封测试邮件，不访问网页")
    parser.add_argument("--test-bark", action="store_true", help="只发一条 Bark 紧急测试推送，不访问网页")
    parser.add_argument("--dry-run", action="store_true", help="检测但不发送通知")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口（本地调试用）")
    args = parser.parse_args()

    if args.test_email:
        ok = send_email(
            "[测试] 补货监控邮件测试",
            "如果你收到这封邮件，说明 Gmail 发信配置正确。\n"
            f"监控商品：{PRODUCT_NAME}\n{PRODUCT_URL}\n"
            f"发送时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        )
        return 0 if ok else 2

    if args.test_bark:
        ok = send_bark("[测试] 补货监控", "如果静音下手机也响了，说明 Bark 紧急通知配置正确。")
        return 0 if ok else 2

    status = check_page(headed=args.headed)

    if status == "unknown":
        log(
            f"两个按钮都看不到（{IN_STOCK_TEXT} / {OUT_OF_STOCK_TEXT}）。"
            "可能被网站拦截、页面改版或按钮文字不对，请查看截图。退出码 1。"
        )
        return 1

    if status == "out_of_stock":
        log("当前缺货，不发送通知。")
        return 0

    # ---- 有货 ----
    log("检测到有货！")
    if args.dry_run:
        log("dry-run 模式：不发送通知。")
        return 0

    title = f"有货了！{PRODUCT_NAME}"
    body = f"{PRODUCT_NAME} 现在可以加入购物袋，赶快下单。\n{PRODUCT_URL}"
    bark_ok = send_bark(title, body)
    mail_ok = send_email(
        f"🔔 {title}",
        f"{body}\n\n检测时间：{datetime.now():%Y-%m-%d %H:%M:%S}（见附件截图）",
        attach_screenshot=True,
    )
    return 0 if (bark_ok or mail_ok) else 2


if __name__ == "__main__":
    sys.exit(main())
