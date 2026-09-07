"""正式 WebUI 端到端回归（对运行中的服务端 + 真实数据库）。

前置：服务端跑在 127.0.0.1:18313（token e2e-token），数据库有导入数据。
运行：.venv/bin/python docs/ui/webtest.py
"""
import json
import sys
import time
import urllib.request

sys.path.insert(0, "/home/zhyi/Documents/repositories/EPD-Dashboard/docs/ui")
from cdp import Browser

BASE = "http://127.0.0.1:18313"
failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f" ({extra})" if extra else ""))
    if not cond:
        failures.append(name)


def api(method, path, body=None):
    req = urllib.request.Request(
        BASE + "/api" + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer e2e-token", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


b = Browser(BASE + "/ui/")
time.sleep(1.2)
# 首次访问：token 顶栏出现并登录
bar = b.js("!!document.getElementById('token-bar')")
b.js("document.getElementById('token-input').value = 'e2e-token'")
b.js("document.getElementById('token-save').click()")
time.sleep(1.8)

cells = b.js("document.querySelectorAll('#food-list .cell').length")
check("登录后首页渲染", cells > 0, f"{cells} 条")
b.screenshot("/tmp/webui-t1-home.png")

# 记一笔（真实入库）
count_before = b.js("foods.length")
b.js("document.getElementById('fab').click()")
time.sleep(0.4)
b.js("""
  document.getElementById('add-name').value = 'WebUI回归测试条目';
  document.getElementById('add-qty').value = 1;
  document.getElementById('add-life').value = 6;
""")
b.js("document.getElementById('add-save').click()")
time.sleep(1.5)
new_item = b.js("foods.find(f => f.name === 'WebUI回归测试条目') ? foods.find(f => f.name === 'WebUI回归测试条目').id : null")
check("记一笔真实入库", new_item is not None)
b.screenshot("/tmp/webui-t2-added.png")

# 吃完（软删）→ 已吃完区（计数动态断言）
eaten_before = b.js("foods.filter(f => f.consumed_at !== null).length")
b.js(f"document.querySelector(\"[data-id='{new_item}'] .act-eat\").click()")
time.sleep(1.2)
eaten_after = b.js("foods.filter(f => f.consumed_at !== null).length")
eaten = b.js(f"foods.find(f => f.id === {new_item}).consumed_at !== null")
check("吃完后条目进入已吃完分组", eaten and eaten_after == eaten_before + 1,
      f"{eaten_before} -> {eaten_after}")
b.screenshot("/tmp/webui-t3-eaten.png")

# 恢复
b.js(f"document.querySelector(\"[data-id='{new_item}'] .act-eat\").click()")
time.sleep(1.2)
check("恢复后回到在库区", b.js(f"foods.find(f => f.id === {new_item}).consumed_at === null"))

# 硬删除（清理测试数据）
b.js(f"document.querySelector(\"[data-id='{new_item}'] .row\").click()")
time.sleep(0.4)
b.js("[...document.querySelectorAll('#action-sheet .sheet-btns > div')].find(d => d.textContent === '删除').click()")
time.sleep(1.2)
check("删除后条目消失", b.js(f"foods.find(f => f.id === {new_item})") is None)

# 墨水屏页
b.js("showTab('epd')")
time.sleep(1.5)
status = api("GET", "/epd/status")
check("墨水屏页推送状态显示", (b.js("document.getElementById('push-reason').textContent") != "—"))
b.screenshot("/tmp/webui-t4-epd.png")

# 固件页
b.js("showTab('fw')")
time.sleep(1.5)
fw_cells = b.js("document.querySelectorAll('#fw-list .cell').length")
check("固件页列表渲染", fw_cells > 0, f"{fw_cells} 包")
b.screenshot("/tmp/webui-t4-fw.png")

b.close()
print("=" * 30)
print("FAILED:", failures if failures else "无，全部通过")
sys.exit(1 if failures else 0)