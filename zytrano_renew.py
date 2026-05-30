"""
Zytrano.top 自动续期脚本 (多账号支持版)
- CloakBrowser（源码级指纹伪装）过 Cloudflare
- frame_locator 穿透 Turnstile iframe，点击 span.cb-i（视觉勾选框）
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

# ── 基础配置与环境变量读取 ──────────────────────────────────
# 微信推送配置（所有账号共用，也可根据需要移入账号配置中）
WXPUSHER_TOKEN = os.environ.get("WXPUSHER_TOKEN", "")
WXPUSHER_UID   = os.environ.get("WXPUSHER_UID", "")

BASE_URL    = "https://cp.zytrano.top"
LOGIN_URL   = f"{BASE_URL}/login"
SERVERS_URL = f"{BASE_URL}/servers"

SCREENSHOT_DIR = Path("./screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)


def load_accounts() -> list[dict]:
    """
    配置加载器：支持从单账号环境变量、或者 JSON 配置加载多账号
    """
    # 优先读取多账号 JSON 配置字符串 (例如单行环境变量)
    accounts_json = os.environ.get("ZYTRANO_ACCOUNTS_JSON")
    if accounts_json:
        try:
            return json.loads(accounts_json)
        except Exception as e:
            log.error(f"解析 ZYTRANO_ACCOUNTS_JSON 失败: {e}")

    # 兜底读取原先的单账号环境变量
    single_user = os.environ.get("ZYTRANO_USERNAME")
    single_pass = os.environ.get("ZYTRANO_PASSWORD")
    if single_user and single_pass:
        log.info("未检测到多账号 JSON，将使用单账号环境变量运行。")
        return [{"username": single_user, "password": single_pass}]

    raise ValueError("未配置任何账号信息！请检查环境变量 ZYTRANO_ACCOUNTS_JSON 或 ZYTRANO_USERNAME")


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

def js_eval(page, script: str):
    try:
        return page.evaluate(script)
    except Exception as e:
        log.warning(f"JS 执行失败: {e}")
        return None

# ── Cloudflare 全页拦截等待 ───────────────────────────────
def is_cf_blocked(page) -> bool:
    try:
        body = get_text(page).lower()
        return "verify you are human" in body or (
            "cloudflare" in body and "security" in body
        )
    except Exception:
        return False

def wait_cf_pass(page, timeout=45) -> bool:
    log.info("等待 Cloudflare 全页验证通过...")
    for i in range(timeout):
        if not is_cf_blocked(page):
            log.info(f"✅ Cloudflare 验证通过（{i}s）")
            return True
        if i % 5 == 0 and i > 0:
            log.info(f"   CF 等待中... {i}s")
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

# ── Turnstile 点击 ─────────────────────────────────────────
def click_turnstile_checkbox(page, timeout=30) -> bool:
    def dump_frames(label: str):
        try:
            frames = page.frames
            log.info(f"[诊断/{label}] 当前共 {len(frames)} 个 frame：")
            for i, f in enumerate(frames):
                url = (f.url or "about:blank")[:120]
                log.info(f"  [{i}] {url}")
        except Exception as e:
            log.warning(f"[诊断/{label}] dump_frames 失败: {e}")

    def dump_token_state(label: str):
        val = js_eval(page, """
            (() => {
                function deepQuery(root, sel) {
                    let el = root.querySelector(sel);
                    if (el) return el;
                    for (const host of root.querySelectorAll('*')) {
                        if (host.shadowRoot) {
                            el = deepQuery(host.shadowRoot, sel);
                            if (el) return el;
                        }
                    }
                    return null;
                }
                const el = deepQuery(document, 'input[name="cf-turnstile-response"]');
                if (!el) return 'INPUT_NOT_FOUND';
                const v = el.value || '';
                return v.length === 0 ? 'EMPTY' : `len=${v.length} prefix=${v.slice(0,20)}`;
            })()
        """)
        log.info(f"[诊断/{label}] cf-turnstile-response: {val}")

    def dump_page_state(label: str):
        try:
            url = page.url
            title = page.title()
            log.info(f"[诊断/{label}] URL={url}  title={title!r}")
        except Exception as e:
            log.warning(f"[诊断/{label}] dump_page_state 失败: {e}")

    def token_ready() -> bool:
        val = js_eval(page, """
            (() => {
                function deepQuery(root, sel) {
                    let el = root.querySelector(sel);
                    if (el) return el;
                    for (const host of root.querySelectorAll('*')) {
                        if (host.shadowRoot) {
                            el = deepQuery(host.shadowRoot, sel);
                            if (el) return el;
                        }
                    }
                    return null;
                }
                const el = deepQuery(document, 'input[name="cf-turnstile-response"]');
                return el ? (el.value || '').length > 10 : false;
            })()
        """)
        return bool(val)

    log.info("【Turnstile 阶段1】等待静默通过（最多 15s）...")
    dump_page_state("阶段1开始")
    dump_token_state("阶段1开始")
    for i in range(30):
        if token_ready():
            log.info(f"✅ Turnstile 静默通过（{i * 0.5:.1f}s），无需点击")
            return True
        time.sleep(0.5)
    dump_token_state("阶段1结束_未过")

    log.info("【Turnstile 阶段2】用 page.frames 枚举查找 Turnstile frame（最多 8s）...")
    cf_frame = None
    for tick in range(16):
        for f in page.frames:
            if "challenges.cloudflare.com" in (f.url or ""):
                cf_frame = f
                break
        if cf_frame:
            log.info(f"  ✅ 第 {tick * 0.5:.1f}s 找到 Turnstile frame")
            break
        time.sleep(0.5)

    if not cf_frame:
        log.warning("【Turnstile 阶段2】frames 枚举 8s 内未找到 Turnstile frame")
        dump_frames("枚举失败")
        take_screenshot(page, "turnstile_frame_not_found")

        log.info("  降级：尝试 iframe 坐标点击...")
        fallback_clicked = False
        try:
            iframe_el = page.locator('iframe[src*="challenges.cloudflare.com"]').first
            box = iframe_el.bounding_box()
            if box:
                x = box["x"] + 25
                y = box["y"] + box["height"] / 2
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.2, 0.4))
                page.mouse.click(x, y)
                log.info(f"  ✅ 降级坐标点击 ({x:.0f}, {y:.0f})")
                fallback_clicked = True
        except Exception as fe:
            log.error(f"  降级坐标点击失败: {fe}")

        if not fallback_clicked:
            return False
    else:
        log.info(f"【Turnstile 阶段2】frame URL: {cf_frame.url[:120]}")
        time.sleep(1)

        log.info("【Turnstile 阶段3】坐标点击 checkbox...")
        clicked = False
        try:
            frame_el = cf_frame.frame_element()
            box = frame_el.bounding_box()
            if box:
                x = box["x"] + 25
                y = box["y"] + box["height"] / 2
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.2, 0.4))
                page.mouse.click(x, y)
                log.info(f"  ✅ 坐标点击 ({x:.0f}, {y:.0f})")
                clicked = True
        except Exception as e:
            log.error(f"  坐标点击失败: {e}")

        if not clicked:
            return False

    log.info("【Turnstile 阶段4】等待 token 写入（最多 30s）...")
    for i in range(timeout * 2):
        if token_ready():
            log.info(f"✅ Turnstile token 就绪（{i * 0.5:.1f}s）")
            return True
        time.sleep(0.5)

    log.error("【Turnstile 阶段4】token 等待超时（30s）")
    return False

# ── 登录状态检测 ──────────────────────────────────────────
LOGGED_IN_URL_KEYS = ("/home", "/dashboard", "/servers")

def is_logged_in_page(page) -> bool:
    if any(k in page.url for k in LOGGED_IN_URL_KEYS):
        return True
    try:
        body = page.inner_text("body") or ""
        for kw in ("Credits", "Dashboard", "Servers", "Activity Logs"):
            if kw in body:
                log.info(f"[登录检测] 页面含关键词 '{kw}'，判断为已登录")
                return True
    except Exception:
        pass
    return False

# ── 登录（动态传入账号密码） ─────────────────────────────────
def login(page, account: dict, max_retries=2) -> bool:
    username = account["username"]
    password = account["password"]
    
    for attempt in range(1, max_retries + 1):
        log.info(f"登录 {attempt}/{max_retries} (用户: {mask(username)}) ...")

        if is_logged_in_page(page):
            return True

        if not navigate(page, LOGIN_URL):
            continue

        if is_logged_in_page(page):
            return True

        try:
            page.wait_for_selector(
                'input[placeholder="Email or Username"], input[name="user"]',
                timeout=10000,
            )
        except Exception:
            if is_logged_in_page(page):
                return True
            continue

        human_delay(0.5, 1.0)

        # 填写用户名
        try:
            user_el = page.locator('input[placeholder="Email or Username"]').first
            user_el.click()
            user_el.fill("")
            user_el.type(username, delay=random.randint(60, 130))
        except Exception:
            page.locator("input").first.type(username, delay=random.randint(60, 130))
        human_delay(0.3, 0.8)

        # 填写密码
        try:
            pass_el = page.locator('input[placeholder="Password"]').first
            pass_el.click()
            pass_el.fill("")
            pass_el.type(password, delay=random.randint(60, 130))
        except Exception:
            page.locator('input[type="password"]').first.type(
                password, delay=random.randint(60, 130)
            )
        human_delay(0.5, 1.0)

        # 点击 Turnstile
        click_turnstile_checkbox(page, timeout=30)
        human_delay(0.5, 1.0)

        # 点击 Sign In
        try:
            page.get_by_role("button", name="Sign In").click()
        except Exception:
            page.locator("button[type='submit']").first.click()

        try:
            page.wait_for_url(
                lambda url: any(k in url for k in LOGGED_IN_URL_KEYS),
                timeout=30000,
            )
            log.info(f"✅ 登录成功，当前 URL: {page.url}")
            return True
        except Exception:
            if is_logged_in_page(page):
                return True

        log.warning(f"登录后未跳转，当前 URL: {page.url}，重试")
    return False

# ── 读取服务器信息 ─────────────────────────────────────────
def get_servers_info(page) -> list[dict]:
    if not navigate(page, SERVERS_URL):
        return []

    time.sleep(3)
    js_eval(page, "(() => { window.scrollTo(0, document.body.scrollHeight); })()")
    time.sleep(1)
    js_eval(page, "(() => { window.scrollTo(0, 0); })()")
    time.sleep(1)

    html = js_eval(page, "() => document.body.innerHTML") or ""
    server_ids = re.findall(r"handleServerRenew\(['\"]([^\'\"]+)[\'\"]\)", html)

    text = get_text(page)
    suspended_matches = re.findall(
        r'Suspended in[:\s]*([\d]+ days?,\s*[\d]+ hours?,\s*[\d]+ minutes?)',
        text, re.IGNORECASE
    )
    if not suspended_matches:
        suspended_matches = re.findall(r'Suspended in[:\s]*([\d\w\s,]+)', text, re.IGNORECASE)

    servers = []
    for i, sid in enumerate(server_ids):
        info = {
            "server_id": sid,
            "name": f"Server-{i+1}",
            "suspended_in": suspended_matches[i] if i < len(suspended_matches) else "未知",
        }
        servers.append(info)
    return servers

def parse_days_remaining(suspended_in: str) -> float:
    days = hours = minutes = 0.0
    m = re.search(r'(\d+)\s*day', suspended_in, re.I)
    if m: days = float(m.group(1))
    m = re.search(r'(\d+)\s*hour', suspended_in, re.I)
    if m: hours = float(m.group(1))
    m = re.search(r'(\d+)\s*minute', suspended_in, re.I)
    if m: minutes = float(m.group(1))
    return days + hours / 24 + minutes / 1440

# ── 续期操作 ──────────────────────────────────────────────
def renew_server(page, server_id: str) -> bool:
    log.info(f"续期服务器 {mask(server_id)} ...")
    human_delay(0.5, 1.0)

    js_eval(page, f"() => {{ handleServerRenew('{server_id}'); return 'called'; }}")
    time.sleep(3)

    confirm_texts = ["Yes, renew it!", "Yes, renew it", "Confirm", "OK"]
    clicked = False
    for _ in range(3):
        if clicked:
            break
        for btn_text in confirm_texts:
            try:
                page.get_by_role("button", name=btn_text).click(timeout=3000)
                time.sleep(2)
                clicked = True
                break
            except Exception:
                pass
    return True

# ── 单个账号核心逻辑封装 ────────────────────────────────────
def run_for_account(context, account: dict) -> str:
    """
    在隔离的 Context 内为单个账号执行完整闭环逻辑，并返回对应的报告文本
    """
    username = account["username"]
    page = context.new_page()
    
    try:
        if not login(page, account):
            return f"❌ 账号 [{mask(username)}] 登录失败，请检查密码或 CF 验证"

        servers = get_servers_info(page)
        if not servers:
            return f"⚠️ 账号 [{mask(username)}] 未找到任何服务器信息"

        results = []
        for s in servers:
            days = parse_days_remaining(s["suspended_in"])
            log.info(f"[{s['name']}] 续期前剩余: {s['suspended_in']}")
            
            # 执行续期
            renew_server(page, s["server_id"])

            # 刷新页面重新读取最新的到期时间
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
                "suspended_in": new_suspended,
            })

        # 生成该账号的小报
        lines = [f"👤 账号: {mask(username)}"]
        for r in results:
            lines.append(f"  ✅ 已续期 [{r['name']}] -> 剩余: {r['suspended_in']}")
        return "\n".join(lines)

    except Exception as e:
        log.exception(f"账号 {username} 运行异常")
        take_screenshot(page, f"error_{username}")
        return f"❌ 账号 [{mask(username)}] 运行中发生异常: {e}"
    finally:
        page.close()


# ── 主流程 ────────────────────────────────────────────────
def main():
    from cloakbrowser import launch

    # 1. 加载账户列表
    try:
        accounts = load_accounts()
        log.info(f"成功加载 {len(accounts)} 个账号，准备开始自动续期任务...")
    except Exception as e:
        log.error(e)
        return

    # 2. 启动基础浏览器实例
    log.info("启动 CloakBrowser 主进程...")
    browser = launch(
        headless=False,
        humanize=True,
        geoip=True,
    )

    all_reports = ["🖥️ Zytrano 自动续期合并报告", ""]

    try:
        # 3. 遍历账号：每个账号创建一个独立的全新 Session 上下文
        for idx, account in enumerate(accounts, 1):
            log.info(f"\n================ 正在处理第 {idx}/{len(accounts)} 个账号 ================")
            
            # 用 new_context 彻底隔离 Cookie / 缓存，防止账号互串
            context = browser.new_context()
            
            account_report = run_for_account(context, account)
            all_reports.append(account_report)
            all_reports.append("") # 空行分隔
            
            context.close()
            
            # 多个账号之间随机加一点人类缓冲间隔
            if idx < len(accounts):
                sleep_time = random.randint(5, 12)
                log.info(f"等待 {sleep_time} 秒后切换下一个账号...")
                time.sleep(sleep_time)

        # 4. 汇总统一推送
        final_msg = "\n".join(all_reports).strip()
        log.info(f"\n最终汇总报告：\n{final_msg}")
        wxpush(final_msg)

    except Exception as e:
        log.exception(e)
        wxpush(f"❌ 任务全局异常: {e}")
    finally:
        browser.close()
        log.info("所有多账号续期任务结束。")

if __name__ == "__main__":
    main()
