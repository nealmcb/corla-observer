"""The observer page run from the browser extension in Firefox (tests/test_browser_extension.py).

Written 2026-11-05 to test the extension in Firefox as well as Chromium (docs/EXTENSION.md).
Playwright cannot install an add-on in Firefox, so this drives Firefox directly over WebDriver BiDi
(its remote protocol, built in; ``websockets`` is the only package needed): it starts Firefox
headless with a fresh profile, installs the built extension as a temporary add-on (which needs no
signature), and opens the links page and an observer link through it, by script in the page.

It checks that the links page shows the identity the build computed; that the observation page
runs from the extension and loads the ballots; that a ballot entered as a match is sent and
answered (Live); and that the extension verified the receipts, found the extract in the published
commitments, found the log one chain, and found the server's signed report the same as the device's.
Prints a JSON summary of the checks.

Usage: python3 scripts/firefox_extension.py FIREFOX XPI IDENTITY OBSERVER_LINK COMMITMENTS_LISTING
"""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

import websockets  # type: ignore[import-not-found]

ADDON_ID = "corla-observer-client@electionaudits.org"
#: The extension's internal UUID, fixed in the profile so its pages' addresses are known.
UUID = "c0a1a000-0b5e-4e00-a000-000000000001"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Bidi:
    def __init__(self, ws: Any) -> None:
        self.ws, self.n = ws, 0

    async def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.n += 1
        mine = self.n
        await self.ws.send(json.dumps({"id": mine, "method": method, "params": params}))
        while True:
            message = json.loads(await self.ws.recv())
            if message.get("id") == mine:
                if message.get("type") == "error":
                    raise RuntimeError(f"{method}: {message.get('error')}: {message.get('message')}")
                return dict(message["result"])

    async def js(self, context: str, expression: str) -> Any:
        """An expression's value in the page (a promise awaited); its JSON for objects."""
        got = await self.call("script.evaluate", {"expression": f"(async () => JSON.stringify(await ({expression})))()", "target": {"context": context}, "awaitPromise": True})
        if got.get("type") == "exception":
            raise RuntimeError(got["exceptionDetails"].get("text"))
        value = got["result"].get("value")
        return json.loads(value) if isinstance(value, str) else None

    async def wait(self, context: str, expression: str, seconds: float = 20) -> Any:
        """Poll until the expression is truthy; its last value."""
        value = None
        for _ in range(int(seconds * 5)):
            try:
                value = await self.js(context, expression)
            except RuntimeError:
                value = None
            if value:
                return value
            await asyncio.sleep(0.2)
        return value


async def run(firefox: str, xpi: str, identity: str, link: str, listing: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []

    def check(what: str, ok: bool, detail: Any = None) -> None:
        checks.append({"check": what, "ok": bool(ok), "detail": detail})

    port = free_port()
    with tempfile.TemporaryDirectory() as profile:
        (Path(profile) / "user.js").write_text(f'user_pref("extensions.webextensions.uuids", {json.dumps(json.dumps({ADDON_ID: UUID}))});\n', encoding="utf-8")
        proc = subprocess.Popen([firefox, "--headless", "--remote-debugging-port", str(port), "--profile", profile, "--no-remote"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # fmt: skip
        try:
            ws = None
            for _ in range(150):
                try:
                    ws = await websockets.connect(f"ws://127.0.0.1:{port}/session", max_size=2**24)
                    break
                except OSError:
                    await asyncio.sleep(0.2)
            assert ws is not None, "Firefox did not open its remote protocol"
            b = Bidi(ws)
            await b.call("session.new", {"capabilities": {}})
            await b.call("webExtension.install", {"extensionData": {"type": "archivePath", "path": xpi}})
            context = (await b.call("browsingContext.getTree", {}))["contexts"][0]["context"]
            base = f"moz-extension://{UUID}"
            await b.call("browsingContext.navigate", {"context": context, "url": f"{base}/setup.html", "wait": "complete"})
            shown = await b.wait(context, "(document.getElementById('code-hash').textContent.match(/^[0-9a-f]{64}$/) || [null])[0]")
            check("the links page shows the identity the build computed", shown == identity, {"shown": shown, "built": identity})
            server = link.split("/observe/")[0]
            await b.js(context, f"new Promise(r => browser.storage.local.set({{{json.dumps('commitments:' + server)}: {{listing: {json.dumps(listing)}}}}}, r))")
            await b.call("browsingContext.navigate", {"context": context, "url": f"{base}/client.html#{quote(link, safe='')}", "wait": "complete"})
            started = await b.wait(context, "(() => { const i = document.getElementById('obs-intro'); return i && !i.hidden; })()")
            check("the page runs from the extension and loads the ballots", bool(started), await b.js(context, "document.getElementById('obs-status') && document.getElementById('obs-status').innerText"))
            if not started:
                return checks
            await b.js(context, "(document.getElementById('obs-intro-mode-two_tap').click(), document.getElementById('obs-intro-start').click(), true)")
            head = await b.wait(context, "(() => { const c = document.getElementById('obs-current'); return c && !c.hidden && document.querySelector('.obs-head').textContent; })()")
            check("it shows the first ballot", bool(head) and "Ballot 1 of" in head, head)
            await b.js(context, "(document.getElementById('obs-match').click(), true)")
            await asyncio.sleep(0.3)
            await b.js(context, "(document.getElementById('obs-match').click(), true)")
            status = await b.wait(context, "(() => { const t = document.getElementById('obs-status').innerText; return t.includes('Live') && t; })()")
            check("the entry is sent and answered (Live, nothing queued)", bool(status), status)
            line = await b.wait(context, "(() => { const t = (document.getElementById('ext-verify') || {}).innerText || ''; return t.includes('is one chain') && t.includes('signed report') && t; })()", 30)
            line = line or await b.js(context, "(document.getElementById('ext-verify') || {}).innerText")
            check("the extension verified the receipts", bool(line) and " verified" in line and "problem" not in line and "0 verified" not in line, line)
            check("the extract shown is in the published commitments", bool(line) and "is in the published commitments" in line, line)
            check("the log is one chain through the receipts' heads", bool(line) and "is one chain" in line, line)
            check("the server's signed report matches the device's own", bool(line) and "matches your device's: 1 ballots" in line, line)
            await ws.close()
        finally:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
    return checks


def main(argv: list[str]) -> int:
    checks = asyncio.run(asyncio.wait_for(run(*argv[:5]), 150))
    ok = bool(checks) and all(c["ok"] for c in checks)
    print(json.dumps({"ok": ok, "checks": checks}))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
