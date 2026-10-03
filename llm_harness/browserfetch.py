from __future__ import annotations

import base64
import json
import os
import socket
import struct
import time
import urllib.parse
import urllib.request

DEFAULT_JS = 'document.title + "\\n\\n" + document.body.innerText'
DEFAULT_PORT = 9222
SETTLE_DELAY = 1.5
MAX_WAIT = 30
MAX_OUTPUT = 50_000

LAUNCH_CMD = 'open -na "Google Chrome" --args --remote-debugging-port={port}'


class _WS:
    def __init__(self, ws_url: str, host: str, port: int):
        path = ws_url.split("://", 1)[1].split("/", 1)[1]
        self.sock = socket.create_connection((host, port), timeout=30)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET /{path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self.sock.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake failed")
            buf += chunk
        status = buf.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise ConnectionError(
                f"websocket handshake failed: {status.decode(errors='replace')}")
        self.buf = buf.split(b"\r\n\r\n", 1)[1]

    def _read(self, n: int) -> bytes:
        out = b""
        while len(out) < n:
            if self.buf:
                take = self.buf[: n - len(out)]
                self.buf = self.buf[len(take):]
                out += take
            else:
                chunk = self.sock.recv(n - len(out))
                if not chunk:
                    raise ConnectionError("socket closed")
                out += chunk
        return out

    def _frame_out(self, opcode: int, payload: bytes, mask: bool) -> None:
        n = len(payload)
        if n < 126:
            second = (0x80 if mask else 0) | n
            extra = b""
        elif n < 65536:
            second = (0x80 if mask else 0) | 126
            extra = struct.pack(">H", n)
        else:
            second = (0x80 if mask else 0) | 127
            extra = struct.pack(">Q", n)
        header = bytes([0x80 | opcode, second]) + extra
        if mask:
            key = os.urandom(4)
            body = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
            header += key + body
        else:
            header += payload
        self.sock.sendall(header)

    def send_text(self, text: str) -> None:
        self._frame_out(0x1, text.encode(), True)

    def _recv_frame(self):
        h = self._read(2)
        fin = h[0] & 0x80
        opcode = h[0] & 0x0F
        ln = h[1] & 0x7F
        masked = h[1] & 0x80
        if ln == 126:
            ln = struct.unpack(">H", self._read(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._read(8))[0]
        mask = self._read(4) if masked else b""
        data = self._read(ln) if ln else b""
        if masked:
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
        return fin, opcode, data

    def recv_json(self):
        msg = b""
        while True:
            fin, opcode, data = self._recv_frame()
            if opcode == 0x8:
                raise ConnectionError("closed by browser")
            if opcode == 0x9:
                self._frame_out(0xA, data, False)
                continue
            if opcode in (0x1, 0x2):
                msg = data
            elif opcode == 0x0:
                msg += data
            else:
                continue
            if fin:
                return json.loads(msg)


class _CDP:
    def __init__(self, ws_url: str, host: str, port: int):
        self.ws = _WS(ws_url, host, port)
        self._id = 0

    def call(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send_text(
            json.dumps({"id": mid, "method": method, "params": params or {}}))
        while True:
            msg = self.ws.recv_json()
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(
                        msg["error"].get("message", "cdp error"))
                return msg.get("result", {})

    def close(self) -> None:
        try:
            self.ws.sock.close()
        except OSError:
            pass


def _http(port: int, path: str, method: str = "GET") -> bytes:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read()


def _new_tab(port: int, url: str) -> dict:
    path = "/json/new?" + urllib.parse.quote(url, safe="")
    try:
        return json.loads(_http(port, path, "PUT"))
    except Exception:
        return json.loads(_http(port, path, "GET"))


def _close_tab(port: int, target_id: str) -> None:
    try:
        ver = json.loads(_http(port, "/json/version"))
        cdp = _CDP(ver["webSocketDebuggerUrl"], "127.0.0.1", port)
        cdp.call("Target.closeTarget", {"targetId": target_id})
        cdp.close()
    except Exception:
        pass


def fetch(url: str, wait: float = 10.0, js: str = DEFAULT_JS,
          port: int = DEFAULT_PORT) -> str:
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return "error: url must start with http:// or https://"
    wait = min(max(float(wait or 10.0), 1.0), MAX_WAIT)
    port = int(port or DEFAULT_PORT)
    target = None
    cdp = None
    try:
        try:
            target = _new_tab(port, url)
        except Exception as e:
            return (
                f"error: Chrome debug port {port} unreachable ({e}). "
                f"Quit Chrome completely (Cmd+Q), then start it once with: "
                f"{LAUNCH_CMD.format(port=port)}")
        cdp = _CDP(target["webSocketDebuggerUrl"], "127.0.0.1", port)
        cdp.call("Runtime.enable")
        cdp.call("Page.enable")
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            res = cdp.call(
                "Runtime.evaluate",
                {"expression": "document.readyState", "returnByValue": True})
            if res.get("result", {}).get("value") == "complete":
                break
            time.sleep(0.25)
        time.sleep(SETTLE_DELAY)
        res = cdp.call(
            "Runtime.evaluate",
            {"expression": js, "returnByValue": True, "awaitPromise": True})
        value = res.get("result", {}).get("value")
        if value is None:
            return "error: page evaluation returned nothing"
        text = value if isinstance(value, str) else \
            json.dumps(value, ensure_ascii=False)
        if len(text) > MAX_OUTPUT:
            text = text[:MAX_OUTPUT] + \
                f"\n...[truncated, {len(text) - MAX_OUTPUT} chars dropped]"
        return text or "(page returned no text)"
    finally:
        if cdp:
            cdp.close()
        if target:
            _close_tab(port, target["id"])


def tool_browser_fetch(root: str, args: dict) -> str:
    return fetch(args.get("url", ""), float(args.get("wait") or 10),
                 args.get("js") or DEFAULT_JS)


if __name__ == "__main__":
    import sys
    url = sys.argv[1] if len(sys.argv) > 1 else ""
    wait = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0
    js = sys.argv[3] if len(sys.argv) > 3 else DEFAULT_JS
    print(fetch(url, wait, js))