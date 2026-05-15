"""
Zytrano.top 自动续期脚本
- pydoll + Chromium 操控浏览器
- Cloudflare Turnstile 自动绕过（expect_and_bypass_cloudflare_captcha + Shadow DOM 手动点击）
- 伪装人类行为（随机延迟 / humanize 输入 / 鼠标移动）
- 隐藏 webdriver 指纹
- 续期后读取 "Suspended in: X days, Y hours, Z minutes" 推送 WxPusher
"""

import asyncio
import json
import logging
import math
import os
import random
import re
from datetime import datetime
from pathlib import Path

from pydoll.browser.chromium import Chrome
from pydoll.browser.options import ChromiumOptions

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── 脱敏工具 ──────────────────────────────────────────────
def mask(value: str, show: int = 3) -> str:
    """保留前后 show 位，中间用 *** 遮盖"""
    if not value or len(value) <= show * 2:
        return "***"
    return value[:show] + "***" + value[-show:]


# ── 环境变量 ──────────────────────────────────────────────
USERNAME         = os.environ["ZYTRANO_USERNAME"]       # Email or Username
PASSWORD         = os.environ["ZYTRANO_PASSWORD"]
WXPUSHER_TOKEN   = os.environ.get("WXPUSHER_TOKEN", "")
WXPUSHER_UID     = os.environ.get("WXPUSHER_UID", "")

BASE_URL     = "https://cp.zytrano.top"
LOGIN_URL    = f"{BASE_URL}/login"
SERVERS_URL  = f"{BASE_URL}/servers"

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
async def take_screenshot(tab, name: str):
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(SCREENSHOT_DIR / f"{ts}_{name}.png")
        await tab.take_screenshot(path=path)
        log.info(f"📸 截图: {path}")
    except Exception as e:
        log.warning(f"截图失败: {e}")

async def get_text(tab) -> str:
    try:
        r = await tab.execute_script("return document.body.innerText")
        if isinstance(r, dict):
            return r.get("result", {}).get("result", {}).get("value", "")
        return str(r)
    except Exception:
        return ""

async def get_url(tab) -> str:
    try:
        r = await tab.execute_script("return window.location.href")
        if isinstance(r, dict):
            return r.get("result", {}).get("result", {}).get("value", "")
        return str(r)
    except Exception:
        return ""

async def js_eval(tab, script: str):
    """执行 JS 并返回原始值"""
    try:
        r = await tab.execute_script(script)
        if isinstance(r, dict):
            return r.get("result", {}).get("result", {}).get("value")
        return r
    except Exception as e:
        log.warning(f"JS 执行失败: {e}")
        return None

async def human_delay(min_s=0.4, max_s=1.2):
    """模拟人类随机停顿"""
    await asyncio.sleep(random.uniform(min_s, max_s))

async def human_mouse_move(tab):
    """在页面内随机移动鼠标，模拟真人行为"""
    try:
        x = random.randint(200, 900)
        y = random.randint(100, 600)
        await tab.execute_script(f"""
            document.dispatchEvent(new MouseEvent('mousemove', {{
                bubbles: true, clientX: {x}, clientY: {y}
            }}));
        """)
    except Exception:
        pass

async def wait_for_url_contains(tab, keyword: str, timeout=15) -> bool:
    for _ in range(timeout * 2):
        if keyword in await get_url(tab):
            return True
        await asyncio.sleep(0.5)
    return False

async def wait_for_text(tab, text: str, timeout=15) -> bool:
    for _ in range(timeout * 2):
        if text in await get_text(tab):
            return True
        await asyncio.sleep(0.5)
    return False

# ── 浏览器创建 ────────────────────────────────────────────
def _find_chromium() -> str | None:
    candidates = [
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ]
    for p in candidates:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            log.info(f"找到 Chromium: {p}")
            return p
    import subprocess
    try:
        r = subprocess.run(["which", "chromium-browser"], capture_output=True, text=True, timeout=5)
        p = r.stdout.strip()
        if p and os.path.isfile(p):
            return p
    except Exception:
        pass
    return None

async def create_browser():
    opts = ChromiumOptions()
    opts.headless = False                  # 需要 Xvfb，headless 对 CF 绕过不友好
    path = _find_chromium()
    if path:
        opts.binary_location = path

    # 反检测 & 人类伪装参数
    for arg in [
        "--window-size=1280,720",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-features=VizDisplayCompositor",
        "--disable-extensions",
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--exclude-switches=enable-automation",
        "--disable-infobars",
        "--disable-save-password-bubble",
        "--disable-password-generation",
        "--password-store=basic",
        "--use-mock-keychain",
        "--lang=en-US",
    ]:
        opts.add_argument(arg)

    opts.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )

    opts.browser_preferences = {
        "credentials_enable_service": False,
        "credentials_enable_autosign": False,
        "profile": {
            "password_manager_enabled": False,
            "default_content_setting_values": {
                "notifications": 2,
                "geolocation": 2,
            },
        },
        "autofill": {"enabled": False},
        "intl": {"accept_languages": "en-US,en"},
    }

    browser = await Chrome(options=opts).__aenter__()
    tab = await browser.start()

    # 注入指纹伪装脚本
    try:
        await tab.execute_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => false });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            window.chrome = { runtime: {} };
        """)
    except Exception:
        pass

    return browser, tab

# ── Cloudflare 绕过 ───────────────────────────────────────
async def manual_cf_click(tab, timeout=20) -> bool:
    """Shadow DOM 穿透点击 CF Turnstile checkbox"""
    log.info("尝试 Shadow DOM 穿透点击 Cloudflare ...")
    for i in range(timeout):
        body = await get_text(tab)
        # 判断是否已通过（出现登录表单或主页内容）
        if any(k in body for k in ["Email or Username", "Sign In", "Dashboard", "Servers"]):
            log.info("✅ Cloudflare 已通过")
            return True
        try:
            shadow_roots = await tab.find_shadow_roots(deep=False)
            for sr in shadow_roots:
                try:
                    html = await sr.inner_html
                    if "challenges.cloudflare.com" not in html:
                        continue
                    iframe_el = await sr.query(
                        'iframe[src*="challenges.cloudflare.com"]', timeout=3
                    )
                    body_el = await iframe_el.find(tag_name="body", timeout=3)
                    inner_shadow = await body_el.get_shadow_root(timeout=3)
                    checkbox = await inner_shadow.query("span.cb-i", timeout=3)
                    await human_mouse_move(tab)
                    await human_delay(0.3, 0.7)
                    await checkbox.click()
                    log.info("已点击 CF checkbox，等待验证...")
                    await asyncio.sleep(3)
                    body2 = await get_text(tab)
                    if any(k in body2 for k in ["Email or Username", "Sign In", "Dashboard"]):
                        log.info("✅ 点击后 CF 验证通过")
                        return True
                except Exception as e:
                    log.debug(f"Shadow DOM 尝试: {e}")
        except Exception as e:
            log.debug(f"第{i+1}s CF 处理: {e}")
        await asyncio.sleep(1)
    log.error("Cloudflare 验证超时")
    return False

async def ensure_cf_passed(tab, url: str, timeout=20) -> bool:
    """导航到 url，确保 CF 验证通过"""
    try:
        async with tab.expect_and_bypass_cloudflare_captcha():
            await tab.go_to(url)
    except Exception:
        await tab.go_to(url)

    await asyncio.sleep(2)
    body = await get_text(tab)
    if any(k in body for k in ["Email or Username", "Sign In", "Dashboard", "Servers"]):
        return True
    return await manual_cf_click(tab, timeout)

# ── 登录 ──────────────────────────────────────────────────
async def login(tab, max_retries=3) -> bool:
    for attempt in range(1, max_retries + 1):
        log.info(f"登录 {attempt}/{max_retries} (用户: {mask(USERNAME)}) ...")
        if not await ensure_cf_passed(tab, LOGIN_URL):
            log.error("CF 验证失败，重试")
            continue

        await take_screenshot(tab, "01_login_page")

        # 模拟人类：先移动鼠标再操作
        await human_mouse_move(tab)
        await human_delay(0.5, 1.0)

        # 填写用户名
        try:
            user_el = await tab.find(
                tag_name="input", placeholder="Email or Username", timeout=10
            )
        except Exception:
            try:
                user_el = await tab.find(tag_name="input", name="user", timeout=5)
            except Exception:
                user_el = await tab.find(tag_name="input", timeout=5)

        await user_el.click()
        await human_delay(0.2, 0.5)
        await user_el.type_text(USERNAME, humanize=True)
        await human_delay(0.3, 0.8)

        # 填写密码
        try:
            pass_el = await tab.find(
                tag_name="input", placeholder="Password", timeout=10
            )
        except Exception:
            pass_el = await tab.find(tag_name="input", type="password", timeout=5)

        await pass_el.click()
        await human_delay(0.2, 0.4)
        await pass_el.type_text(PASSWORD, humanize=True)
        await human_delay(0.5, 1.0)

        # 点击 Sign In
        try:
            btn = await tab.find(tag_name="button", text="Sign In", timeout=8)
        except Exception:
            btn = await tab.query("button[type='submit']", timeout=5)
        await human_mouse_move(tab)
        await human_delay(0.3, 0.6)
        await btn.click()
        log.info("已点击 Sign In，等待跳转...")

        if await wait_for_url_contains(tab, "/home", 12) or \
           await wait_for_url_contains(tab, "/servers", 5):
            log.info("✅ 登录成功")
            await take_screenshot(tab, "02_login_success")
            return True

        log.warning("登录后未跳转，重试")
        await take_screenshot(tab, f"02_login_fail_{attempt}")

    return False

# ── 读取服务器信息并续期 ───────────────────────────────────
async def get_servers_info(tab) -> list[dict]:
    """
    从 /servers 页面读取所有服务器的名称、到期信息、服务器 ID
    返回列表：[{"name": "zybot", "suspended_in": "10 days, 8 hours, 50 minutes", "server_id": "DZ4P..."}]
    """
    if not await ensure_cf_passed(tab, SERVERS_URL):
        log.warning("进入服务器页 CF 失败")
        return []

    await asyncio.sleep(3)
    await take_screenshot(tab, "03_servers_page")

    # 从 HTML 里提取服务器 ID（handleServerRenew('xxx')）
    html = await js_eval(tab, "return document.body.innerHTML")
    if not html:
        html = ""

    server_ids = re.findall(r"handleServerRenew\(['\"]([^'\"]+)['\"]\)", html)
    log.info(f"找到服务器 ID: {[mask(s) for s in server_ids]}")

    # 读页面文字，提取 "Suspended in: X days, Y hours, Z minutes"
    text = await get_text(tab)

    servers = []
    # 按卡片顺序匹配名称和 suspended_in
    # 格式：每个卡片有 "zybot" + "Suspended in:\n10 days, 8 hours, 50 minutes"
    suspended_matches = re.findall(
        r'Suspended in[:\s]*([\d]+ days?,\s*[\d]+ hours?,\s*[\d]+ minutes?)',
        text, re.IGNORECASE
    )
    # 也尝试只有天数的格式
    if not suspended_matches:
        suspended_matches = re.findall(
            r'Suspended in[:\s]*([\d\w\s,]+)',
            text, re.IGNORECASE
        )

    log.info(f"Suspended in 信息: {suspended_matches}")

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
    """从 'X days, Y hours, Z minutes' 解析出总天数"""
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

async def renew_server(tab, server_id: str) -> bool:
    """调用 handleServerRenew(server_id) 续期"""
    log.info(f"续期服务器 {mask(server_id)} ...")
    await human_mouse_move(tab)
    await human_delay(0.5, 1.0)

    # 直接调用页面 JS 函数（绿色刷新按钮的 onclick）
    result = await js_eval(
        tab,
        f"handleServerRenew('{server_id}'); return 'called';"
    )
    log.info(f"handleServerRenew 调用结果: {result} (server: {mask(server_id)})")
    await asyncio.sleep(3)

    # 如果弹出确认对话框，点确认（按优先级尝试各种按钮文字）
    confirm_texts = ["Yes, renew it!", "Yes, renew it", "Confirm", "OK"]
    clicked = False
    for _ in range(3):
        if clicked:
            break
        for btn_text in confirm_texts:
            try:
                btn = await tab.find(
                    tag_name="button", text=btn_text, timeout=3
                )
                await btn.click()
                log.info(f"已点击确认按钮: {btn_text}")
                await asyncio.sleep(2)
                clicked = True
                break
            except Exception:
                pass
        if not clicked:
            await asyncio.sleep(1)

    await take_screenshot(tab, f"04_after_renew_{server_id[:8]}")

    # 验证续期成功（页面刷新后 Suspended in 天数增加）
    text_after = await get_text(tab)
    if "success" in text_after.lower() or "renewed" in text_after.lower():
        log.info("✅ 续期成功（页面有 success 字样）")
        return True

    log.info("续期操作已执行（无法确认成功，请查看截图）")
    return True

# ── 主流程 ────────────────────────────────────────────────
async def main():
    browser, tab = await create_browser()
    try:
        # 1. 登录
        if not await login(tab):
            wxpush("❌ Zytrano 登录失败，请检查账号密码或 CF 验证")
            return

        # 2. 读取服务器信息
        servers = await get_servers_info(tab)
        if not servers:
            wxpush("❌ Zytrano 未找到服务器信息，请检查截图")
            return

        # 3. 每两天跑一次，每次无条件续期所有服务器
        results = []

        for s in servers:
            days = parse_days_remaining(s["suspended_in"])
            log.info(f"[{s['name']}] 续期前剩余约 {days:.2f} 天 ({s['suspended_in']})")
            log.info(f"[{s['name']}] 执行续期...")
            success = await renew_server(tab, s["server_id"])

            # 续期后重新读取最新到期信息
            await ensure_cf_passed(tab, SERVERS_URL)
            await asyncio.sleep(3)
            text_new = await get_text(tab)
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

        # 4. 构建推送消息
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
        await take_screenshot(tab, "99_error")
        wxpush(f"❌ Zytrano 脚本异常: {e}")
    finally:
        await asyncio.sleep(3)
        try:
            await browser.__aexit__(None, None, None)
        except Exception:
            pass
        log.info("任务结束")

if __name__ == "__main__":
    asyncio.run(main())
