"""
Zytrano.top 自动续期脚本 (高可用无人值守投产版)
- 实现了账号级别的异常防熔断机制，某个账号崩溃不影响后续队列
- 建立了全局绝对可靠的 try-finally 浏览器进程回收屏障
- 修复了 JS 参数化传递与前置探针检查，解决注入隐患与虚假调用
- 引入了解盾状态级强干预，拒绝盲目提交表单
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

def mask(value: str, show: int = 3) -> str:
    if not value or len(value) <= show * 2:
        return "***"
    return value[:show] + "***" + value[-show:]

# ── 环境变量与基础配置 ──────────────────────────────────────
WXPUSHER_TOKEN = os.environ.get("WXPUSHER_TOKEN", "")
WXPUSHER_UID   = os.environ.get("WXPUSHER_UID", "")

BASE_URL    = "https://cp.zytrano.top"
LOGIN_URL   = f"{BASE_URL}/login"
SERVERS_URL = f"{BASE_URL}/servers"

SCREENSHOT_DIR = Path("./screenshots")
SCREENSHOT_DIR.mkdir(exist_ok=True)


# ── 严格类型账号清洗器 ──────────────────────────────────────
def load_accounts() -> list[dict]:
    raw_content = ""
    source_info = ""

    env_json = os.environ.get("ZYTRANO_ACCOUNTS_JSON")
    if env_json:
        raw_content = env_json.strip()
        source_info = "环境变量 ZYTRANO_ACCOUNTS_JSON"
    else:
        local_file = Path("accounts.json")
        if local_file.exists():
            raw_content = local_file.read_text(encoding="utf-8").strip()
            source_info = "本地 accounts.json 文件"

    if not raw_content:
        single_user = os.environ.get("ZYTRANO_USERNAME")
        single_pass = os.environ.get("ZYTRANO_PASSWORD")
        if single_user and single_pass:
            log.info("未检测到多账号 JSON，降级使用标准单账号环境变量。")
            return [{"username": single_user, "password": single_pass}]
        raise ValueError("❌ 没有任何可供执行的账号源！")

    try:
        data = json.loads(raw_content)
        if isinstance(data, list):
            accounts_list = data
        elif isinstance(data, dict):
            if "accounts" in data and isinstance(data["accounts"], list):
                accounts_list = data["accounts"]
            elif "data" in data and isinstance(data["data"], list):
                accounts_list = data["data"]
            else:
                raise ValueError("字典结构中未包含合法的 'accounts' 或 'data' 数组")
        else:
            raise ValueError("JSON 顶级根节点类型错误")

        valid_accounts = []
        for idx, item in enumerate(accounts_list):
            if not isinstance(item, dict):
                log.warning(f"[{source_info}] 索引 {idx} 项非合法的字典，已跳过")
                continue
            u = item.get("username") or item.get("user") or item.get("email")
            p = item.get("password") or item.get("pwd")
            if u and p:
                valid_accounts.append({"username": str(u), "password": str(p)})
            else:
                log.warning(f"[{source_info}] 账号条目索引 {idx} 数据字段残缺，已跳过")

        if not valid_accounts:
            raise ValueError("洗涤后无可用的合法账号凭证")

        log.info(f"✅ [{source_info}] 捕获 {len(valid_accounts)} 个标准可用账号")
        return valid_accounts
    except Exception as e:
        raise ValueError(f"❌ 账号配置树深度解析崩溃 ({source_info}): {e}")


# ── 工具函数 ──────────────────────────────────────────────
def take_screenshot(page, name: str):
    try:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = str(SCREENSHOT_DIR / f"{ts}_{name}.png")
        page.screenshot(path=path)
    except Exception:
        pass

def get_text(page) -> str:
    try: return page.inner_text("body") or ""
    except Exception: return ""

def human_delay(min_s=0.5, max_s=1.2):
    time.sleep(random.uniform(min_s, max_s))

def js_eval(page, script: str, *args):
    """ 支持强类型参数化安全传递的评估器 """
    try: return page.evaluate(script, *args)
    except Exception: return None

def parse_days_remaining(suspended_in: str) -> float:
    days = hours = minutes = 0.0
    if not suspended_in or "未知" in suspended_in:
        return 0.0
    m = re.search(r'(\d+)\s*day', suspended_in, re.I)
    if m: days = float(m.group(1))
    m = re.search(r'(\d+)\s*hour', suspended_in, re.I)
    if m: hours = float(m.group(1))
    m = re.search(r'(\d+)\s*minute', suspended_in, re.I)
    if m: minutes = float(m.group(1))
    return days + (hours / 24.0) + (minutes / 1440.0)


# ── Cloudflare 拦截层与 Turnstile 原生穿透 ──────────────────
def is_cf_blocked(page) -> bool:
    try:
        body = get_text(page).lower()
        return "verify you are human" in body or ("cloudflare" in body and "security" in body)
    except Exception:
        return False

def wait_cf_pass(page, timeout=45) -> bool:
    for i in range(timeout):
        if not is_cf_blocked(page):
            return True
        time.sleep(1)
    return False

def navigate(page, url: str, timeout=45) -> bool:
    try: page.goto(url, timeout=30000, wait_until="domcontentloaded")
    except Exception: pass

    if not is_cf_blocked(page): return True
    if wait_cf_pass(page, timeout=timeout): return True

    try: page.reload(wait_until="domcontentloaded", timeout=30000)
    except Exception: pass
    return wait_cf_pass(page, timeout=30)

def click_turnstile_checkbox(page, timeout=30) -> bool:
    """ 完整还原 page.frames 扫描与真实 CDP 坐标物理点击的闭环实现 """
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

    for i in range(20):
        if token_ready():
            return True
        time.sleep(0.5)

    cf_frame = None
    for tick in range(16):
        for f in page.frames:
            if "challenges.cloudflare.com" in (f.url or ""):
                cf_frame = f
                break
        if cf_frame: break
        time.sleep(0.5)

    if not cf_frame:
        try:
            iframe_el = page.locator('iframe[src*="challenges.cloudflare.com"]').first
            box = iframe_el.bounding_box()
            if box:
                x, y = box["x"] + 25, box["y"] + (box["height"] / 2)
                page.mouse.move(x, y)
                time.sleep(0.3)
                page.mouse.click(x, y)
                log.info(f"🎯 触发坐标降级点击: ({x:.0f}, {y:.0f})")
        except Exception as e:
            log.error(f"❌ 坐标降级点击失败: {e}")
            return False
    else:
        try:
            frame_el = cf_frame.frame_element()
            box = frame_el.bounding_box()
            if box:
                x, y = box["x"] + 25, box["y"] + (box["height"] / 2)
                page.mouse.move(x, y)
                time.sleep(random.uniform(0.2, 0.4))
                page.mouse.click(x, y)
                log.info(f"🎯 核心内框坐标击发成功: ({x:.0f}, {y:.0f})")
            else:
                return False
        except Exception as e:
            log.error(f"❌ 内框物理映射异常: {e}")
            return False

    for i in range(timeout * 2):
        if token_ready():
            return True
        time.sleep(0.5)
    return False


# ── 登录流 (修复群体硬伤5：强校验解盾状态，未过直接熔断阻断) ──────────
LOGGED_IN_URL_KEYS = ("/home", "/dashboard", "/servers")

def is_logged_in_page(page) -> bool:
    if any(k in page.url for k in LOGGED_IN_URL_KEYS): return True
    body = get_text(page)
    return any(kw in body for kw in ("Credits", "Dashboard", "Servers"))

def login(page, account: dict) -> bool:
    username, password = account["username"], account["password"]
    for attempt in range(1, 3):
        if is_logged_in_page(page): return True
        if not navigate(page, LOGIN_URL): continue
        if is_logged_in_page(page): return True

        try:
            page.wait_for_selector('input', timeout=8000)
            human_delay(0.5, 1.0)

            # 阶梯型用户名输入容错
            try: page.locator('input[placeholder*="Email"], input[placeholder*="Username"]').first.fill(username, timeout=3000)
            except Exception:
                try: page.locator('input[name="user"], input[name="username"]').first.fill(username, timeout=2000)
                except Exception: page.locator('input[type="text"], input').first.fill(username)

            human_delay(0.3, 0.7)

            # 阶梯型密码输入容错
            try: page.locator('input[placeholder*="Password"]').first.fill(password, timeout=3000)
            except Exception:
                try: page.locator('input[name="password"], input[name="pwd"]').first.fill(password, timeout=2000)
                except Exception: page.locator('input[type="password"]').first.fill(password)

            human_delay(0.5, 1.0)

            # 【修复硬伤5】强审计 Turnstile 结果，解盾失败直接熔断本次，拒绝盲目提交
            cf_passed = click_turnstile_checkbox(page)
            if not cf_passed:
                log.error(f"❌ [账号: {mask(username)}] 本轮 Turnstile 盾面未能在时限内通过，放弃提交表单触发重试。")
                take_screenshot(page, f"login_cf_failed_{username[:4]}")
                continue 

            human_delay(0.4, 0.9)
            try: page.get_by_role("button", name=re.compile("Sign In|Login", re.I)).click(timeout=3000)
            except Exception: page.locator("button[type='submit'], button").first.click()

            page.wait_for_url(lambda url: any(k in url for k in LOGGED_IN_URL_KEYS), timeout=25000)
            return True
        except Exception as ex:
            log.warning(f"当前登录重试序列异常（{attempt}/2）: {ex}")
            if is_logged_in_page(page): return True
    return False


# ── 服务器结构拉取 ─────────────────────────────────────────
def get_servers_info(page) -> list[dict]:
    if not navigate(page, SERVERS_URL): return []
    time.sleep(3)
    js_eval(page, "window.scrollTo(0, document.body.scrollHeight);")
    time.sleep(1)
    
    html = js_eval(page, "() => document.body.innerHTML") or ""
    server_ids = re.findall(r"handleServerRenew\(['\"]([^\'\"]+)[\'\"]\)", html)

    text = get_text(page)
    suspended_matches = re.findall(r'Suspended in[:\s]*([\d]+ days?,\s*[\d]+ hours?,\s*[\d]+ minutes?)', text, re.I)
    if not suspended_matches:
        suspended_matches = re.findall(r'Suspended in[:\s]*([\d\w\s,]+)', text, re.I)

    servers = []
    for i, sid in enumerate(server_ids):
        servers.append({
            "server_id": str(sid),
            "index": i,
            "name": f"Server-{i+1}",
            "suspended_in": suspended_matches[i] if i < len(suspended_matches) else "未知",
        })
    return servers

def click_confirm_modal_if_exists(page) -> bool:
    confirm_texts = ["Yes, renew it!", "Yes, renew it", "Confirm", "OK"]
    for btn_text in confirm_texts:
        try:
            btn = page.get_by_role("button", name=btn_text)
            if btn.is_visible():
                btn.click(timeout=2000)
                log.info(f"-> 成功触发二层卡片弹窗点击: '{btn_text}'")
                time.sleep(2)
                return True
        except Exception:
            pass
    return False


# ── 单个账号核心闭环 (修复群体硬伤3、4：前置探针校验、参数化 JS、安全传参) ──
def run_for_account(page, account: dict) -> str:
    username = account["username"]
    if not login(page, account):
        return f"❌ 账号 [{mask(username)}] 鉴权登录失败 (风控拦截或凭证失效)"

    servers = get_servers_info(page)
    if not servers:
        return f"⚠️ 账号 [{mask(username)}] 底座名下无任何活跃容器实例"

    results = []
    for s in servers:
        target_id = s["server_id"]
        old_time_str = s["suspended_in"]
        old_days = parse_days_remaining(old_time_str)
        
        log.info(f"⏳ 容器 [{s['name']}] 续期前解析天数: {old_days:.4f} 天 ({old_time_str})")
        
        # 【修复硬伤3】前置探针检查：确认 window.handleServerRenew 挂载就绪
        fn_exists = js_eval(page, "() => typeof window.handleServerRenew === 'function'")
        if not fn_exists:
            log.error(f"❌ 目标页面中 handleServerRenew 全局核心续期函数丢失或未渲染完成")
            results.append({"name": s["name"], "success": False, "time_str": old_time_str, "err_msg": "JS核心函数丢失"})
            continue

        # 【修复硬伤4】安全求值重构：拒绝手工 format 拼接字符串，使用原生 args[0] 参数安全代理
        try:
            page.evaluate("id => window.handleServerRenew(id)", target_id)
            log.info(f"-> 续期 JS 底层指令安全投喂发射成功，正在等待交互模态窗口...")
        except Exception as e:
            log.error(f"❌ 续期 JS 指令执行期发生异常阻断: {e}")
            results.append({"name": s["name"], "success": False, "time_str": old_time_str, "err_msg": "JS执行抛错"})
            continue

        time.sleep(2)
        click_confirm_modal_if_exists(page)
        
        # 冷却并刷新主域
        time.sleep(3)
        navigate(page, SERVERS_URL)
        time.sleep(2)
        
        updated_list = get_servers_info(page)
        
        # 优选 ID，次选 index 精准比对
        matched_server = None
        for us in updated_list:
            if us["server_id"] == target_id:
                matched_server = us
                break
        
        if not matched_server:
            for us in updated_list:
                if us["index"] == s["index"]:
                    log.warning(f"⚠️ 服务器 ID 无法完成闭环匹配，降级采用自然索引 [{s['index']}] 兜底")
                    matched_server = us
                    break

        new_time_str = matched_server["suspended_in"] if matched_server else "未知"
        new_days = parse_days_remaining(new_time_str)
        log.info(f"⏳ 容器 [{s['name']}] 续期后解析天数: {new_days:.4f} 天 ({new_time_str})")

        # 严格浮点增量断言，防止假成功
        is_real_success = (new_days > (old_days + 0.5))

        results.append({
            "name": s["name"],
            "success": is_real_success,
            "time_str": new_time_str
        })

    lines = [f"👤 账号: {mask(username)}"]
    for r in results:
        err_suffix = f" ({r['err_msg']})" if "err_msg" in r else ""
        status = "✅ 续期成功" if r["success"] else "❌ 续期失败"
        lines.append(f"  {status} [{r['name']}] -> 剩余到期时间: {r['time_str']}{err_suffix}")
    return "\n".join(lines)


# ── 全局多账号总线控制 (修复群体硬伤1、2：加入账号防熔断、全局进程僵尸防御) ──
def main():
    from cloakbrowser import launch

    try:
        accounts = load_accounts()
    except Exception as e:
        log.critical(e)
        return

    all_reports = ["🖥️ Zytrano 自动续期终审合并报告", ""]
    has_any_error = False

    log.info("🚀 启动 CloakBrowser 生产主实例进程...")
    browser = launch(headless=False, humanize=True, geoip=True)

    # 【修复硬伤2】提升至最外围的全局坚固资源回收框架，无死角防僵尸残留
    try:
        for idx, account in enumerate(accounts, 1):
            username = account.get("username", "未知")
            log.info(f"\n{'='*20} 进程区间: 账号流水轴 ({idx} / {len(accounts)}) {'='*20}")
            
            # --- 【修复硬伤1】账号级彻底防熔断沙箱 ---
            try:
                context = None
                page = None
                try:
                    context = browser.new_context()
                    page = context.new_page()
                    log.info("🔒 成功挂载标准独立 Sandbox BrowserContext。")
                except Exception as err:
                    log.warning(f"⚠️ 无法分离沙盒 Context: {err}。执行进程级彻底重启...")
                    try: browser.close()
                    except Exception: pass
                    
                    # 物理隔离重拉起
                    browser = launch(headless=False, humanize=True, geoip=True)
                    page = browser.new_page()
                    log.info("🔒 物理层重置就绪，在全新独立浏览器进程空间中运行。")

                # 执行核心闭环逻辑
                account_report = run_for_account(page, account)
                all_reports.append(account_report)
                all_reports.append("")

                if "❌" in account_report or "⚠️" in account_report:
                    has_any_error = True

            except Exception as account_level_err:
                # 核心防熔断捕获点：一旦当前账号执行中网页关闭、闪退、DOM异常，被此完全阻断
                log.error(f"💥 [严重异常] 账号 [{mask(username)}] 执行中遭遇未捕获突发崩溃: {account_level_err}", exc_info=True)
                all_reports.append(f"👤 账号: {mask(username)}\n  ❌ 运行期突发全面崩溃 (已沙箱隔离) -> 错误原因: {account_level_err}\n")
                has_any_error = True
                
            finally:
                # 局部资源尽力释放
                if 'page' in locals() and page:
                    try: page.close()
                    except Exception: pass
                if 'context' in locals() and context:
                    try: context.close()
                    except Exception: pass

            # 步进随机延迟
            if idx < len(accounts):
                gap = random.randint(6, 12)
                log.info(f"🛡️ 规避批量指纹审计，挂起睡眠 {gap} 秒...")
                time.sleep(gap)

    except Exception as global_err:
        log.critical(f"🚨 全局总线级发生灾难性故障: {global_err}", exc_info=True)
        has_any_error = True
    finally:
        # 【修复硬伤2】无死角绝对回收屏障：无论是跑完还是中途被大范围报错打断，一定会干净关闭
        log.info("🧹 触发全局生命周期终点销毁机制，正在强制注销并闭合 Chromium 物理进程...")
        try:
            browser.close()
        except Exception as close_err:
            log.error(f"回收内核进程时发生次生故障: {close_err}")
        log.info("所有多账号浏览器执行矩阵注销完毕。")

    # 推送合并简报
    final_msg = "\n".join(all_reports).strip()
    log.info(f"\n输出最终统计报表:\n{final_msg}")
    
    # 微信合并推送
    if has_any_error:
        wxpush(f"🚨 Zytrano 挂机运维简报-异常或失败审计\n\n{final_msg}")
    else:
        log.info("🎉 完美大满贯！所有账号及名下服务器全量实质性增量续期完毕。保持静默，不干扰日常生活。")


def wxpush(content: str):
    if not WXPUSHER_TOKEN or not WXPUSHER_UID:
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
            pass
    except Exception as e:
        log.warning(f"📨 WxPusher 推送异常: {e}")

if __name__ == "__main__":
    main()
