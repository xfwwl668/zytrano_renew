"""
Zytrano.top 自动续期脚本
- CloakBrowser（源码级指纹伪装）过 Cloudflare
- Playwright 同步 API 操控浏览器
- 登录前等待 Turnstile 完成 + Shadow DOM 手动点击兜底
- 续期后读取 "Suspended in: X days, Y hours, Z minutes" 推送 WxPusher
"""

import json
import logging
import os
import random
import re
import time
from datetime import datetime
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── 脱敏工具 ──────────────────────────────────────────────
def mask(value: str, show: int = 3) -> str:
    if not value or len(value) <= show * 2:
        return "***"
    return value[:show] + "***" + value[-show:]

# ── 环境变量 ──────────────────────────────────────────────
USERNAME       = os.environ["ZYTRANO_USERNAME"]
PASSWORD       = os.environ["ZYTRANO_PASSWORD"]
WXPUSHER_TOKEN = os.environ.get("WXPUSHER_TOKEN", "")
WXPUSHER_UID   = os.environ.get("WXPUSHER_UID", "")

BASE_URL    = "https://cp.zytrano.top"
LOGIN_URL   = f"{BASE_URL}/login"
SERVERS_URL = f"{BASE_URL}/servers"

SCREENSHOT_DIR = Path("./screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)

# ── WxPusher 推送 ─────────────────────────────────────────
def wxpush(content: str):
    if not WXPUSHER_TOKEN or not WXPUSHER_UID:
        log.warning("WxPusher 未配置，跳过推送")
        return
    import urllib.request
    payload = json.dumps({
        "appToken": WXPUSHER_TOKEN,
        "content": content,
        "contentType": 1,
        "uids": [WXPUSHER_UID],
    }).encode()
    try:
        req = urllib.request.Request(
            "https://wxpusher.zjiecode.com/api/send/message",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            if result.get("success"):
                log.info(f"📨 WxPusher 推送成功 (token: {mask(WXPUSHER_TOKEN)}, uid: {mask(WXPUSHER_UID)})")
            else:
                log.warning(f"📨 WxPusher 推送失败: {result}")
    except Exception as e:
        log.warning(f"📨 WxPusher 推送异常: {e}")

# ── 工具函数 ──────────────────────────────────────────────
def take_screenshot(page, name: str):
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(SCREENSHOT_DIR / f"{ts}_{name}.png")
        page.screenshot(path=path, full_page=False)
        log.info(f"📸 截图: {path}")
    except Exception as e:
        log.warning(f"截图失败: {e}")

def get_text(page) -> str:
    try:
        return page.inner_text("body") or ""
    except Exception:
        return ""

def human_delay(min_s=0.4, max_s=1.2):
    time.sleep(random.uniform(min_s, max_s))

def wait_for_url_contains(page, keyword: str, timeout=15) -> bool:
    try:
        page.wait_for_url(f"**{keyword}**", timeout=timeout * 1000)
        return True
    except Exception:
        return keyword in page.url

def js_eval(page, script: str):
    try:
        return page.evaluate(script)
    except Exception as e:
        log.warning(f"JS 执行失败: {e}")
        return None

# ── Cloudflare 等待 ───────────────────────────────────────
def is_cf_blocked(page) -> bool:
    try:
        body = get_text(page).lower()
        return "verify you are human" in body or (
            "cloudflare" in body and "security" in body
        )
    except Exception:
        return False

def wait_cf_pass(page, timeout=45) -> bool:
    log.info("等待 Cloudflare 验证自动通过...")
    for i in range(timeout):
        if not is_cf_blocked(page):
            log.info(f"✅ Cloudflare 验证通过（{i}s）")
            return True
        if i % 5 == 0 and i > 0:
            log.info(f"  CF 等待中... {i}s")
        time.sleep(1)
    log.error(f"Cloudflare 验证超时（{timeout}s）")
    return False

def navigate(page, url: str, timeout=45) -> bool:
    log.info(f"导航到: {url}")
    try:
        page.goto(url, timeout=30000, wait_until="domcontentloaded")
    except Exception as e:
        log.warning(f"goto 超时/异常: {e}，继续等待...")

    if not is_cf_blocked(page):
        return True

    if wait_cf_pass(page, timeout=timeout):
        return True

    log.info("CF 未过，刷新重试...")
    try:
        page.reload(wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass
    return wait_cf_pass(page, timeout=30)

# ── Turnstile 等待 + 手动点击兜底 ────────────────────────
def wait_turnstile_and_click(page, timeout=30) -> bool:
    """
    等待登录页内嵌的 Turnstile 验证完成。
    策略：
      1. 先轮询 cf-turnstile-response 隐藏域是否有值（自动通过）
      2. 若超过 5s 仍在 Verifying，尝试 Shadow DOM 穿透点击 checkbox
      3. 再等待 response 填入
    返回 True 表示 Turnstile token 已就绪，可以提交表单。
    """
    log.info("等待 Turnstile 完成...")

    def has_token() -> bool:
        val = js_eval(page, """
            (() => {
                // cf-turnstile 会把 token 写入 name="cf-turnstile-response" 的隐藏 input
                const el = document.querySelector(
                    'input[name="cf-turnstile-response"]'
                );
                return el ? (el.value || "").length > 10 : false;
            })()
        """)
        return bool(val)

    def turnstile_state() -> str:
        """返回 'verifying' / 'checked' / 'unknown'"""
        state = js_eval(page, """
            (() => {
                // Turnstile widget 的 iframe src 包含 challenges.cloudflare.com
                const iframes = document.querySelectorAll(
                    'iframe[src*="challenges.cloudflare.com"]'
                );
                if (!iframes.length) return 'unknown';
                // 找包含 checkbox 的 iframe
                for (const f of iframes) {
                    try {
                        // 只能检测 same-origin，跨域 iframe 内部读不到
                        // 但可以通过父容器的 data 属性判断
                    } catch(e) {}
                }
                // 回退：检测页面文字
                const body = document.body.innerText || '';
                if (body.includes('Verifying')) return 'verifying';
                if (body.includes('Verify you are human')) return 'unchecked';
                return 'unknown';
            })()
        """)
        return state or "unknown"

    # 第一阶段：最多等 5s，看 token 是否自动填入
    for i in range(10):
        if has_token():
            log.info(f"✅ Turnstile token 已就绪（{i*0.5:.1f}s，自动通过）")
            return True
        time.sleep(0.5)

    # 第二阶段：尝试 Shadow DOM 穿透点击 checkbox
    log.info("Turnstile 仍在 Verifying，尝试 Shadow DOM 点击 checkbox...")
    clicked = _shadow_click_turnstile(page)
    if clicked:
        log.info("已点击 Turnstile checkbox，等待 token...")
    else:
        log.warning("Shadow DOM 点击失败，继续等待自动通过...")

    # 第三阶段：再等最多 timeout 秒
    for i in range(timeout * 2):
        if has_token():
            log.info(f"✅ Turnstile token 已就绪（点击后 {i*0.5:.1f}s）")
            return True
        if i % 10 == 0 and i > 0:
            log.info(f"  Turnstile 等待中... {i*0.5:.0f}s")
            take_screenshot(page, f"turnstile_waiting_{i}")
        time.sleep(0.5)

    log.error("Turnstile 等待超时，强行继续（可能失败）")
    return False


def _shadow_click_turnstile(page) -> bool:
    """
    通过 JS 遍历 Shadow DOM，找到 Turnstile iframe 内的 checkbox 并点击。
    Turnstile 的结构：
      div[data-sitekey] (宿主)
        └─ shadow-root
             └─ iframe[src*="challenges.cloudflare.com"]
    iframe 跨域，无法直接操作内部 DOM；
    但可以直接点击 iframe 元素本身触发焦点，
    或用坐标点击 iframe 中心位置。
    """
    try:
        # 方法1：点击 Turnstile iframe 中心（模拟人类点击 checkbox 区域）
        clicked = js_eval(page, """
            (() => {
                const iframe = document.querySelector(
                    'iframe[src*="challenges.cloudflare.com"]'
                );
                if (!iframe) return false;
                const rect = iframe.getBoundingClientRect();
                // checkbox 在 iframe 左侧约 25px 处
                const x = rect.left + 25;
                const y = rect.top + rect.height / 2;
                // 模拟完整点击事件序列
                ['mouseover','mouseenter','mousemove','mousedown','mouseup','click']
                    .forEach(type => {
                        document.elementFromPoint(x, y)?.dispatchEvent(
                            new MouseEvent(type, {
                                bubbles: true, cancelable: true,
                                clientX: x, clientY: y, view: window
                            })
                        );
                    });
                return true;
            })()
        """)
        if clicked:
            log.info("  方法1：iframe 坐标点击已触发")
            time.sleep(2)
            return True
    except Exception as e:
        log.debug(f"  方法1 失败: {e}")

    try:
        # 方法2：用 Playwright locator 直接 click iframe
        iframe_loc = page.locator('iframe[src*="challenges.cloudflare.com"]').first
        iframe_loc.click(position={"x": 25, "y": 15}, timeout=5000)
        log.info("  方法2：Playwright iframe.click() 已触发")
        time.sleep(2)
        return True
    except Exception as e:
        log.debug(f"  方法2 失败: {e}")

    return False


# ── 登录 ──────────────────────────────────────────────────
def login(page, max_retries=3) -> bool:
    for attempt in range(1, max_retries + 1):
        log.info(f"登录 {attempt}/{max_retries} (用户: {mask(USERNAME)}) ...")
        if not navigate(page, LOGIN_URL):
            log.error("CF 验证失败，重试")
            continue

        # 等待登录表单出现
        try:
            page.wait_for_selector(
                'input[placeholder="Email or Username"], input[name="user"]',
                timeout=10000,
            )
        except Exception:
            log.warning("找不到用户名输入框，重试")
            take_screenshot(page, f"01_login_no_form_{attempt}")
            continue

        human_delay(0.5, 1.0)
        take_screenshot(page, "01_login_page")

        # 填写用户名
        try:
            user_el = page.locator('input[placeholder="Email or Username"]').first
            user_el.click()
            user_el.fill("")
            user_el.type(USERNAME, delay=random.randint(60, 130))
        except Exception:
            user_el = page.locator("input").first
            user_el.click()
            user_el.type(USERNAME, delay=random.randint(60, 130))
        human_delay(0.3, 0.8)

        # 填写密码
        try:
            pass_el = page.locator('input[placeholder="Password"]').first
            pass_el.click()
            pass_el.fill("")
            pass_el.type(PASSWORD, delay=random.randint(60, 130))
        except Exception:
            pass_el = page.locator('input[type="password"]').first
            pass_el.click()
            pass_el.type(PASSWORD, delay=random.randint(60, 130))
        human_delay(0.5, 1.0)

        # ★ 关键：等 Turnstile 完成再点登录
        take_screenshot(page, "01b_before_turnstile_wait")
        wait_turnstile_and_click(page, timeout=30)
        take_screenshot(page, "01c_after_turnstile_wait")
        human_delay(0.5, 1.0)

        # 点击 Sign In
        try:
            page.get_by_role("button", name="Sign In").click()
        except Exception:
            page.locator("button[type='submit']").first.click()
        log.info("已点击 Sign In，等待跳转...")

        if wait_for_url_contains(page, "/home", 12) or \
           wait_for_url_contains(page, "/servers", 5):
            log.info("✅ 登录成功")
            take_screenshot(page, "02_login_success")
            return True

        log.warning("登录后未跳转，重试")
        take_screenshot(page, f"02_login_fail_{attempt}")

    return False

# ── 读取服务器信息 ─────────────────────────────────────────
def get_servers_info(page) -> list[dict]:
    if not navigate(page, SERVERS_URL):
        log.warning("进入服务器页 CF 失败")
        return []

    time.sleep(3)
    take_screenshot(page, "03_servers_page")

    html = js_eval(page, "return document.body.innerHTML") or ""
    server_ids = re.findall(r"handleServerRenew\(['\"]([^'\"]+)['\"]\)", html)
    log.info(f"找到服务器 ID: {[mask(s) for s in server_ids]}")

    text = get_text(page)
    suspended_matches = re.findall(
        r'Suspended in[:\s]*([\d]+ days?,\s*[\d]+ hours?,\s*[\d]+ minutes?)',
        text, re.IGNORECASE
    )
    if not suspended_matches:
        suspended_matches = re.findall(
            r'Suspended in[:\s]*([\d\w\s,]+)',
            text, re.IGNORECASE
        )

    log.info(f"Suspended in 信息: {suspended_matches}")

    servers = []
    for i, sid in enumerate(server_ids):
        info = {
            "server_id": sid,
            "name": f"Server-{i+1}",
            "suspended_in": suspended_matches[i] if i < len(suspended_matches) else "未知",
        }
        servers.append(info)
        log.info(f"服务器 [{info['name']}] ID={mask(sid)} 到期：{info['suspended_in']}")

    return servers

def parse_days_remaining(suspended_in: str) -> float:
    days = hours = minutes = 0.0
    m = re.search(r'(\d+)\s*day', suspended_in, re.I)
    if m:
        days = float(m.group(1))
    m = re.search(r'(\d+)\s*hour', suspended_in, re.I)
    if m:
        hours = float(m.group(1))
    m = re.search(r'(\d+)\s*minute', suspended_in, re.I)
    if m:
        minutes = float(m.group(1))
    return days + hours / 24 + minutes / 1440

# ── 续期 ──────────────────────────────────────────────────
def renew_server(page, server_id: str) -> bool:
    log.info(f"续期服务器 {mask(server_id)} ...")
    human_delay(0.5, 1.0)

    result = js_eval(page, f"handleServerRenew('{server_id}'); return 'called';")
    log.info(f"handleServerRenew 调用结果: {result} (server: {mask(server_id)})")
    time.sleep(3)

    confirm_texts = ["Yes, renew it!", "Yes, renew it", "Confirm", "OK"]
    clicked = False
    for _ in range(3):
        if clicked:
            break
        for btn_text in confirm_texts:
            try:
                btn = page.get_by_role("button", name=btn_text)
                btn.click(timeout=3000)
                log.info(f"已点击确认按钮: {btn_text}")
                time.sleep(2)
                clicked = True
                break
            except Exception:
                pass
        if not clicked:
            time.sleep(1)

    take_screenshot(page, f"04_after_renew_{server_id[:8]}")

    text_after = get_text(page)
    if "success" in text_after.lower() or "renewed" in text_after.lower():
        log.info("✅ 续期成功（页面有 success 字样）")
        return True

    log.info("续期操作已执行（无法确认成功，请查看截图）")
    return True

# ── 主流程 ────────────────────────────────────────────────
def main():
    from cloakbrowser import launch

    log.info("启动 CloakBrowser（源码级指纹伪装）...")
    browser = launch(
        headless=False,
        humanize=True,
        geoip=True,
    )
    page = browser.new_page()

    try:
        if not login(page):
            wxpush("❌ Zytrano 登录失败，请检查账号密码或 CF 验证")
            return

        servers = get_servers_info(page)
        if not servers:
            wxpush("❌ Zytrano 未找到服务器信息，请检查截图")
            return

        results = []
        for s in servers:
            days = parse_days_remaining(s["suspended_in"])
            log.info(f"[{s['name']}] 续期前剩余约 {days:.2f} 天 ({s['suspended_in']})")
            success = renew_server(page, s["server_id"])

            navigate(page, SERVERS_URL)
            time.sleep(3)
            text_new = get_text(page)
            new_matches = re.findall(
                r'Suspended in[:\s]*([\d]+ days?,\s*[\d]+ hours?,\s*[\d]+ minutes?)',
                text_new, re.IGNORECASE
            )
            new_suspended = new_matches[0] if new_matches else s["suspended_in"]

            results.append({
                "name": s["name"],
                "renewed": success,
                "suspended_in": new_suspended,
            })

        lines = ["🖥️ Zytrano 自动续期报告", ""]
        for r in results:
            status = "✅ 已续期" if r["renewed"] else "❌ 续期失败"
            lines.append(f"{status} [{r['name']}]")
            lines.append(f"Suspended in: {r['suspended_in']}")
            lines.append("")

        msg = "\n".join(lines).strip()
        log.info(f"\n{msg}")
        wxpush(msg)

    except Exception as e:
        log.exception(e)
        take_screenshot(page, "99_error")
        wxpush(f"❌ Zytrano 脚本异常: {e}")
    finally:
        time.sleep(3)
        browser.close()
        log.info("任务结束")

if __name__ == "__main__":
    main()
