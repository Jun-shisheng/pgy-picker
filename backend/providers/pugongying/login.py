"""蒲公英登录流程（自动化）。

链路（M1 实测，见 实测记录）：
    ① 落地页点「账号登录」→ 弹窗默认在「短信登录」页签，**必须先切到「账号登录」**
       → 填 邮箱 + 密码 → **勾选隐私政策** → 点提交按钮
    ② 登录后跳 `/role-select` 选择身份
    ③ 以子账号身份进入 → 才到工作区

踩过的坑全在这里（第一次做会全踩一遍）：
- 勾选框是自定义控件：`input[type=checkbox]` 尺寸 0×0，**真正可点的是外层 div**；
  且落地页底部「联系我们」表单有一个**文案完全相同**的勾选框，必须限定在弹窗内。
- 「登录」两个字全页有 4 处；导航的 `button.login-btn` 在 DOM 里更靠前且**在视口外**，
  取"第一个文案匹配"必点错。真正的提交按钮是 `button.beer-login-btn`。
- 弹窗右侧可能超出视口（视口 921，链接 x≈927），要用会**自动滚入视图**的点击。
"""

from __future__ import annotations

import logging

log = logging.getLogger("pgy.pugongying.login")

_USER_INFO = "/api/solar/user/info"

# ---- 页面上的定位脚本（与页面结构强耦合，平台改版时改这里）----

_FIND_AGREEMENT = r"""
() => {
  const pwd = document.querySelector("input[type=password]");
  if (!pwd) return {error: "no password input"};
  let n = pwd;
  for (let i = 0; i < 14 && n; i++) {
    if (/我已阅读并同意/.test(n.textContent || "")) {
      const cb = n.querySelector("input[type=checkbox]");
      if (cb) {
        const w = cb.closest("div[class*='d-checkbox']") || cb.parentElement;
        const r = w.getBoundingClientRect();
        return {x: r.x + r.width / 2, y: r.y + r.height / 2,
                w: Math.round(r.width), h: Math.round(r.height), checked: cb.checked};
      }
    }
    n = n.parentElement;
  }
  return {error: "密码框所在容器里找不到协议勾选框"};
}
"""


def login_status(page) -> int:
    try:
        return int(page.evaluate(
            f"async () => (await fetch('{_USER_INFO}', {{credentials:'include'}})).status"))
    except Exception:
        return 0


def _viewport(page) -> tuple[int, int]:
    """真实窗口的 CSS 视口尺寸（不依赖 `page.viewport_size`：外部 CDP 接管时它可能为空）。"""
    try:
        size = page.evaluate("() => ({w: window.innerWidth, h: window.innerHeight})")
        return int(size.get("w") or 0), int(size.get("h") or 0)
    except Exception:
        return 0, 0


def _in_viewport(box: dict | None, width: int, height: int) -> bool:
    if not box or not width or not height:
        return True
    x, y = float(box.get("x") or 0), float(box.get("y") or 0)
    right, bottom = x + float(box.get("width") or 0), y + float(box.get("height") or 0)
    return right > 0 and bottom > 0 and x < width and y < height


def _click_text(page, texts: list[str], label: str) -> bool:
    """按候选文案点**可见且在视口内**的那个。

    ⚠️ Playwright 的 `is_visible()` 只表示"有布局盒子"，**不代表在屏幕上**。
    平台导航栏那个同名的「登录」按钮在视口外（实测 x≈1133），先点它等于点空。
    所以这里用 window.innerWidth/Height 做一次真实可见性判断。
    """
    width, height = _viewport(page)
    for text in texts:
        try:
            loc = page.get_by_text(text, exact=True)
            for i in range(loc.count()):
                item = loc.nth(i)
                try:
                    if not item.is_visible():
                        continue
                    if not _in_viewport(item.bounding_box(), width, height):
                        continue
                    item.click(timeout=4_000)
                    log.info("已点击%s：%s", label, text)
                    return True
                except Exception:
                    continue
        except Exception:
            continue
    return False


def login(page, account: str, password: str, *, role: str = "", timeout_s: int = 40) -> bool:
    """执行登录；返回是否登录成功。

    `role` 非空时，登录后在 `/role-select` 进入该身份（如「代理商」）。
    **凭据不进日志**：所有可能带出实参的调用都做了替换处理。
    """
    if login_status(page) == 200:
        log.info("已是登录态")
        return True

    # ① 打开登录弹窗。页面/弹窗可能还在渲染，点不到就等一会儿再点一次
    # （自动登录发生在客户端刚启动时，最容易撞上"页面还没稳"）。
    opened = False
    for attempt in range(3):
        if _click_text(page, ["账号登录", "登录"], "登录入口"):
            opened = True
            break
        page.wait_for_timeout(1_500)
    if not opened:
        log.warning("找不到/点不到登录入口（页面可能还没加载完）")
    page.wait_for_timeout(2_000)

    # ② 切到「账号登录」页签（默认在「短信登录」，密码框在未激活页签里不可见）
    _click_text(page, ["账号登录"], "账号登录页签")
    page.wait_for_timeout(1_500)

    # ③ 等密码框出现。没出现就再试一次切换页签 —— 弹窗渲染慢时第一次点会落空。
    try:
        page.locator("input[type='password']").first.wait_for(state="visible", timeout=8_000)
    except Exception:
        log.warning("密码框暂不可见，再试一次切换到「账号登录」")
        _click_text(page, ["账号登录"], "账号登录页签")
        try:
            page.locator("input[type='password']").first.wait_for(state="visible", timeout=8_000)
        except Exception:
            log.warning("密码框仍不可见，登录可能无法继续")
    try:
        account_input = page.locator(
            "input[placeholder*='邮箱']:visible, input[placeholder*='账号']:visible").first
        account_input.fill(account, timeout=8_000)
    except Exception as exc:
        # Playwright 的异常消息会把 fill 的实参带出来 —— 账号也要抹掉再记。
        log.warning("填账号失败：%s", str(exc).replace(account, "<已隐藏>").splitlines()[0][:160])
    try:
        page.locator("input[type='password']").first.fill(password, timeout=8_000)
    except Exception as exc:
        log.warning("填密码失败：%s", str(exc).replace(password, "<已隐藏>").splitlines()[0][:160])

    # ④ 勾选隐私政策（限定在弹窗内；点容器，用真实鼠标事件）
    info = page.evaluate(_FIND_AGREEMENT) or {}
    if info.get("error"):
        log.warning("勾选框定位失败：%s", info["error"])
    elif info.get("checked"):
        log.info("隐私政策已是勾选状态")
    elif info.get("w") and info.get("h"):
        page.mouse.click(info["x"], info["y"])
        page.wait_for_timeout(400)
    else:
        log.warning("勾选框容器无尺寸，跳过")

    # ⑤ 提交（真正的提交按钮；不能用文案找，导航同名按钮在视口外）
    try:
        page.locator("button.beer-login-btn").first.click(timeout=8_000)
        log.info("已点击登录按钮")
    except Exception:
        log.warning("未点到 button.beer-login-btn，改按回车")
        page.keyboard.press("Enter")

    # ⑥ 等结果（验证码/短信验证由人在窗口里处理，程序只等，不绕）
    for _ in range(max(1, timeout_s // 3)):
        page.wait_for_timeout(3_000)
        if login_status(page) == 200:
            break

    ok = login_status(page) == 200
    if not ok:
        log.warning("登录未成功（可能是验证码/短信验证/网络未就绪，需要人工在窗口里处理）")
        return False
    log.info("登录成功")

    # ⑦ 角色选择
    if role:
        enter_role(page, role)
    return True


def enter_role(page, role: str) -> bool:
    """在 `/role-select` 进入指定身份。已经是工作区则直接返回 True。"""
    try:
        if "/role-select" not in page.url:
            return True
    except Exception:
        return True

    # 卡片右侧的链接可能超出视口，用会**自动滚入视图**的点击。
    cards = page.get_by_text(f"我是{role}", exact=True)
    if not cards.count():
        log.warning("角色页找不到「我是%s」", role)
        return False
    try:
        # 该卡片内的「以子账号身份进入」——取同名链接里最靠右的那个。
        page.get_by_text("以子账号身份进入", exact=True).last.click(force=True, timeout=8_000)
        log.info("已进入身份：%s", role)
    except Exception as exc:
        log.warning("进入身份失败：%s", str(exc).splitlines()[0][:120])
        return False
    page.wait_for_timeout(8_000)
    page.goto("https://pgy.xiaohongshu.com/solar/pre-trade/note/kol",
              wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(5_000)
    return True
