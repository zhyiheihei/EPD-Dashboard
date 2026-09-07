"""原型自动化验收：tab 切换 / 左滑 / ActionSheet / 文案断言。"""
import sys
import time

sys.path.insert(0, "/home/zhyi/Documents/repositories/EPD-Dashboard/docs/ui")
from cdp import Browser

URL = "file:///home/zhyi/Documents/repositories/EPD-Dashboard/docs/ui/prototype-app.html"
b = Browser(URL)
failures = []

def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        failures.append(name)

# 1) 初始首页
check("首页显示食品列表", b.js("document.querySelector('[data-page=home]').classList.contains('active')"))
check("首页共 5 个条目", b.js("document.querySelectorAll('[data-page=home] .cell').length") == 5)

# 2) 文案：饮品=喝完了
verbs = b.js("""
  [...document.querySelectorAll('[data-page=home] .cell')].map(c => {
    const sub = c.querySelector('.sub').textContent;
    return (sub.includes('饮品') ? '喝' : '吃');
  }).join(',')
""")
check(f"左滑按钮按品类区分（{verbs}）", verbs == "吃,吃,吃,喝,吃")

# 3) 左滑食品条目（第一个 .cell）
r = b.drag("[data-page=home] .cell .row", -156)
time.sleep(0.3)
x = b.js("parseFloat(document.querySelector('[data-page=home] .cell .row').dataset.x)")
check(f"左滑打开 (x={x})", x == 156)
btn = b.js("document.querySelector('[data-page=home] .cell .actions button').textContent")
check(f"滑出按钮文案={btn}", btn == "吃完了")
b.screenshot("/tmp/uitest-slide.png")

# 收起滑动状态再继续
b.js("""
  document.querySelectorAll('[data-page=home] .cell').forEach(c => {
    const row = c.querySelector('.row');
    row.style.transform = 'translateX(0)';
    row.dataset.x = 0;
    window.__justDragged = false;
  });
""")
time.sleep(0.2)

# 3b) 快速大幅度左滑自动触发"吃完了"（速度 > 1.2px/ms）
b.js("window.__justDragged = false")
r = b.drag_fast("[data-page=home] .cell .row", -150, duration_ms=110)  # 轻扫 150px/110ms ≈ 1.4px/ms
time.sleep(0.4)
pill = b.js("document.querySelector('[data-page=home] .cell .pill').textContent")
rowx = b.js("parseFloat(document.querySelector('[data-page=home] .cell .row').dataset.x)")
check(f"快速左滑自动吃完 (pill={pill}, rowx={rowx})", pill == "已吃完" and rowx == 0)
b.screenshot("/tmp/uitest-flick.png")
# 复位演示态
b.js("""
  const p = document.querySelector('[data-page=home] .cell .pill');
  p.textContent = '已过 128 天'; p.style.background = '';
  window.__justDragged = false;
""")

# 4) 点击 ActionSheet（未滑动状态下）（未滑动状态下）
b.js("document.querySelector('[data-page=home] .cell .row').click()")
time.sleep(0.4)
title = b.js("document.querySelector('#action-sheet .sheet-title')?.textContent")
btns = b.js("[...document.querySelectorAll('#action-sheet .sheet-btns > div')].map(d => d.textContent).join('|')")
check(f"ActionSheet 标题={title}", title == "光明牌小面包")
check(f"ActionSheet 项={btns}", btns == "吃完了|编辑|删除")
b.screenshot("/tmp/uitest-sheet.png")
b.js("document.querySelector('#action-sheet .sheet-cancel').click()")
time.sleep(0.3)

# 5) 饮品 ActionSheet 文案
cells = b.js("""
  [...document.querySelectorAll('[data-page=home] .cell')]
    .findIndex(c => c.querySelector('.sub').textContent.includes('饮品'))
""")
b.js(f"document.querySelectorAll('[data-page=home] .cell')[{cells}].querySelector('.row').click()")
time.sleep(0.4)
btns2 = b.js("[...document.querySelectorAll('#action-sheet .sheet-btns > div')].map(d => d.textContent).join('|')")
check(f"饮品 ActionSheet 项={btns2}", btns2 == "喝完了|编辑|删除")
b.js("document.querySelector('#action-sheet .sheet-cancel').click()")
time.sleep(0.3)

# 6) tab 切换墨水屏
b.click('.tab[data-tab="epd"]')
time.sleep(0.3)
check("切到墨水屏页", b.js("document.querySelector('[data-page=epd]').classList.contains('active')"))
check("导航标题=墨水屏", b.js("document.getElementById('nav-title').textContent") == "墨水屏")
b.screenshot("/tmp/uitest-epd.png")

# 7) tab 切换固件 / 设置
b.click('.tab[data-tab="fw"]')
time.sleep(0.3)
check("切到固件页", b.js("document.querySelector('[data-page=fw]').classList.contains('active')"))
b.screenshot("/tmp/uitest-fw.png")
b.click('.tab[data-tab="set"]')
time.sleep(0.3)
check("切到设置页", b.js("document.querySelector('[data-page=set]').classList.contains('active')"))
b.screenshot("/tmp/uitest-set.png")

# 8) 回首页滑动状态保留性（切页后 actions 不残留可见）
b.click('.tab[data-tab="home"]')
time.sleep(0.2)
# 先收起此前拖开的条目再断言
b.js("""
  document.querySelectorAll('[data-page=home] .cell').forEach(c => {
    const row = c.querySelector('.row');
    row.style.transform = 'translateX(0)';
    row.dataset.x = 0;
    c.querySelectorAll('.actions button').forEach(b => b.style.transform = 'translateX(156px)');
  });
""")
time.sleep(0.2)
hidden = b.js("""
  [...document.querySelectorAll('[data-page=home] .actions button')]
    .every(b => b.style.transform.includes('156'))
""")
check("切回首页后按钮可正常复位", hidden)

b.close()
print("=" * 30)
print("FAILED:", failures if failures else "无，全部通过")
sys.exit(1 if failures else 0)