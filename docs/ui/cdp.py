"""极简 CDP 客户端：驱动系统 google-chrome headless 做点击/拖拽/DOM 查询/截图。"""
import base64
import json
import subprocess
import time
import urllib.request

import websocket


class Browser:
    def __init__(self, url: str, port: int = 9223, width: int = 414, height: int = 820):
        self.proc = subprocess.Popen(
            [
                "google-chrome", "--headless=new", "--disable-gpu", "--no-sandbox",
                f"--remote-debugging-port={port}", f"--window-size={width},{height}",
                "--remote-allow-origins=*", "--hide-scrollbars", "about:blank",
            ],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.port = port
        deadline = time.time() + 10
        self.target_id = None
        while time.time() < deadline:
            try:
                targets = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=1).read())
                page = next((t for t in targets if t["type"] == "page"), None)
                if page:
                    self.target_id = page["id"]
                    break
            except Exception:
                time.sleep(0.3)
        assert self.target_id, "chrome 启动失败"
        ws_url = None
        for _ in range(20):
            try:
                info = json.loads(urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/json/list", timeout=1).read())
                ws_url = next(t["webSocketDebuggerUrl"] for t in info if t["id"] == self.target_id)
                break
            except Exception:
                time.sleep(0.3)
        self.ws = websocket.create_connection(ws_url, timeout=10, suppress_origin=True)
        self._id = 0
        self.navigate(url)
        time.sleep(0.8)

    def send(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._id:
                return msg.get("result", {})

    def navigate(self, url: str):
        self.send("Page.navigate", url=url)

    def js(self, expr: str):
        r = self.send("Runtime.evaluate", expression=expr, returnByValue=True,
                      awaitPromise=True)
        return r.get("result", {}).get("value")

    def click(self, selector: str):
        return self.js(f"""
          (() => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return 'NOT FOUND';
            el.dispatchEvent(new MouseEvent('click', {{bubbles: true}}));
            return 'ok';
          }})()
        """)

    def drag(self, selector: str, dx: int, steps: int = 8):
        """对元素中心做鼠标拖拽（触控板/鼠标路径）。"""
        return self.js(f"""
          (async () => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return 'NOT FOUND';
            const r = el.getBoundingClientRect();
            const x0 = r.left + r.width / 2, y = r.top + r.height / 2;
            const fire = (type, x) => el.dispatchEvent(new MouseEvent(type, {{
              bubbles: true, clientX: x, clientY: y, buttons: 1}}));
            fire('mousedown', x0);
            for (let i = 1; i <= {steps}; i++) {{
              fire('mousemove', x0 + {dx} * i / {steps});
              await new Promise(r => setTimeout(r, 60));  // 慢速拖拽，不触发 flick
            }}
            fire('mouseup', x0 + {dx});
            return 'dragged';
          }})()
        """)

    def screenshot(self, path: str):
        data = self.send("Page.captureScreenshot", format="png")
        with open(path, "wb") as f:
            f.write(base64.b64decode(data["data"]))

    def close(self):
        try:
            self.ws.close()
        finally:
            self.proc.terminate()

    def drag_fast(self, selector: str, dx: int, duration_ms: int = 90):
        """快速滑动（flick）：短时间大位移，最后 mouseup 后不再跟随。"""
        return self.js(f"""
          (async () => {{
            const el = document.querySelector({json.dumps(selector)});
            if (!el) return 'NOT FOUND';
            const r = el.getBoundingClientRect();
            const x0 = r.left + r.width / 2, y = r.top + r.height / 2;
            const fire = (type, x) => el.dispatchEvent(new MouseEvent(type, {{
              bubbles: true, clientX: x, clientY: y, buttons: 1}}));
            fire('mousedown', x0);
            const steps = 8;
            for (let i = 1; i <= steps; i++) {{
              fire('mousemove', x0 + {dx} * i / steps);
              await new Promise(r => setTimeout(r, {duration_ms} / steps));
            }}
            fire('mouseup', x0 + {dx});
            return 'flicked';
          }})()
        """)
