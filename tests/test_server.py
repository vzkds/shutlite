"""Testes do shutlite. Nenhum deles chama o shutdown.exe de verdade."""

from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

PIN = "482913"


class FakeRunner:
    """Finge ser o shutdown.exe: anota as chamadas e falha quando mandado."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.fail_next = 0
        self.fail_all = False

    def __call__(self, action: str, seconds: int) -> bool:
        self.calls.append((action, seconds))
        if self.fail_all:
            return False
        if self.fail_next and action != "abort":
            self.fail_next -= 1
            return False
        return True


class StateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "state.json"
        self.run = FakeRunner()
        self.state = server.ShutdownState(self.run, self.store, native_timer=True)  # fluxo clássico

    def test_schedule(self) -> None:
        snap = self.state.schedule(600, "shutdown")
        self.assertTrue(snap["scheduled"])
        self.assertEqual(snap["action"], "shutdown")
        self.assertEqual(snap["total"], 600)
        self.assertIn(snap["left"], (599, 600))
        self.assertEqual(self.run.calls, [("shutdown", 600)])

    def test_reschedule_aborts_first(self) -> None:
        self.state.schedule(600, "shutdown")
        self.state.schedule(1200, "restart")
        self.assertEqual(self.run.calls, [("shutdown", 600), ("abort", 0), ("restart", 1200)])
        self.assertEqual(self.state.snapshot()["action"], "restart")

    def test_external_schedule_is_replaced(self) -> None:
        self.run.fail_next = 1  # Windows já tinha um agendamento feito por fora
        snap = self.state.schedule(600, "shutdown")
        self.assertTrue(snap["scheduled"])
        self.assertEqual(self.run.calls, [("shutdown", 600), ("abort", 0), ("shutdown", 600)])

    def test_failure_raises_and_clears(self) -> None:
        self.run.fail_all = True
        with self.assertRaises(server.ShutdownError):
            self.state.schedule(600, "shutdown")
        self.assertFalse(self.state.snapshot()["scheduled"])

    def test_clamps_seconds(self) -> None:
        self.assertEqual(self.state.schedule(1, "shutdown")["total"], server.MIN_SECONDS)
        self.assertEqual(self.state.schedule(10 ** 9, "shutdown")["total"], server.MAX_SECONDS)

    def test_cancel(self) -> None:
        self.state.schedule(600, "shutdown")
        snap = self.state.cancel()
        self.assertFalse(snap["scheduled"])
        self.assertEqual(self.run.calls[-1], ("abort", 0))
        self.assertFalse(self.store.exists())

    def test_survives_restart(self) -> None:
        self.state.schedule(600, "restart")
        again = server.ShutdownState(FakeRunner(), self.store)
        snap = again.snapshot()
        self.assertTrue(snap["scheduled"])
        self.assertEqual(snap["action"], "restart")
        self.assertEqual(snap["total"], 600)

    def test_expired_state_is_not_restored(self) -> None:
        self.store.write_text(json.dumps({"at": time.time() - 5, "action": "shutdown", "total": 60}))
        self.assertFalse(server.ShutdownState(FakeRunner(), self.store).snapshot()["scheduled"])


class HelperTests(unittest.TestCase):
    def test_seconds_until_today_and_tomorrow(self) -> None:
        now = time.mktime((2026, 3, 10, 22, 0, 0, 0, 0, -1))
        self.assertEqual(server.seconds_until(23, 30, now), 90 * 60)
        self.assertEqual(server.seconds_until(21, 0, now), 23 * 3600)
        self.assertEqual(server.seconds_until(22, 0, now), 24 * 3600)

    def test_host_ok(self) -> None:
        self.assertTrue(server.host_ok("localhost:3333"))
        self.assertTrue(server.host_ok("192.168.0.10:3333"))
        self.assertTrue(server.host_ok("[fd00::1]:3333"))
        self.assertTrue(server.host_ok("pc.lan", ("pc.lan",)))
        self.assertFalse(server.host_ok("evil.example:3333"))
        self.assertFalse(server.host_ok(""))

    def test_is_local_net(self) -> None:
        for addr in ("127.0.0.1", "::1", "192.168.1.20", "10.0.0.5", "::ffff:192.168.1.20", "fe80::1%eth0"):
            self.assertTrue(server.is_local_net(addr), addr)
        for addr in ("8.8.8.8", "::ffff:8.8.8.8", "2001:4860:4860::8888", "lixo"):
            self.assertFalse(server.is_local_net(addr), addr)

    def test_pin_guard_locks_after_repeated_failures(self) -> None:
        guard = server.PinGuard(max_fails=3, lock_seconds=60)
        for _ in range(2):
            guard.fail("10.0.0.9")
        self.assertEqual(guard.blocked("10.0.0.9"), 0)
        guard.fail("10.0.0.9")
        self.assertGreater(guard.blocked("10.0.0.9"), 0)
        self.assertEqual(guard.blocked("10.0.0.8"), 0)


class HttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        cfg = server.Config(port=0, pin=PIN, data_dir=Path(self.tmp.name), bind="127.0.0.1",
                            windows_timer=True)  # fluxo clássico; o silencioso é testado em test_fixes.py
        self.run = FakeRunner()
        self.srv = server.build_server(cfg, self.run)
        self.port = self.srv.server_address[1]
        thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def request(self, method: str, path: str, body: object = None, headers: dict | None = None,
                host: str | None = None) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            data = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
            conn.putrequest(method, path, skip_host=host is not None)
            if host is not None:
                conn.putheader("Host", host)
            hdrs = {"Content-Type": "application/json"} if method == "POST" else {}
            hdrs.update(headers or {})
            if data is not None:
                hdrs["Content-Length"] = str(len(data))
            for k, v in hdrs.items():
                conn.putheader(k, v)
            conn.endheaders(data)
            res = conn.getresponse()
            raw = res.read()
            return res.status, (json.loads(raw) if raw else {})
        finally:
            conn.close()

    def post(self, path: str, body: dict | None = None, **kw) -> tuple[int, dict]:
        return self.request("POST", path, {"pin": PIN, **(body or {})}, **kw)

    def test_status_and_page(self) -> None:
        code, snap = self.request("GET", "/api/status")
        self.assertEqual(code, 200)
        self.assertFalse(snap["scheduled"])
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        self.assertEqual(res.getheader("X-Frame-Options"), "DENY")
        self.assertIn(b"shutlite", res.read())
        conn.request("GET", "/qrcode.min.js")
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        self.assertIn(b"QRCode", res.read())
        conn.close()

    def test_schedule_and_cancel(self) -> None:
        code, snap = self.post("/api/shutdown", {"seconds": 600})
        self.assertEqual(code, 200)
        self.assertTrue(snap["scheduled"])
        self.assertEqual(self.request("GET", "/api/status")[1]["action"], "shutdown")
        code, snap = self.post("/api/cancel")
        self.assertEqual(code, 200)
        self.assertFalse(snap["scheduled"])
        self.assertEqual(self.run.calls, [("shutdown", 600), ("abort", 0)])

    def test_restart_and_at(self) -> None:
        self.assertEqual(self.post("/api/restart", {"seconds": 30})[1]["action"], "restart")
        code, snap = self.post("/api/at", {"at": "23:30", "action": "restart"})
        self.assertEqual(code, 200)
        self.assertEqual(snap["action"], "restart")
        self.assertEqual(self.post("/api/at", {"at": "25:00"})[0], 400)
        self.assertEqual(self.post("/api/at", {"at": "abc"})[0], 400)
        self.assertEqual(self.post("/api/shutdown", {"seconds": "muito"})[0], 400)

    def test_wrong_pin_is_rejected_then_locked(self) -> None:
        for _ in range(5):
            self.assertEqual(self.request("POST", "/api/shutdown", {"pin": "000000"})[0], 401)
        code, body = self.post("/api/shutdown", {"seconds": 600})  # PIN certo, mas travado
        self.assertEqual(code, 429)
        self.assertIn("tentativas", body["error"])
        self.assertEqual(self.run.calls, [])

    def test_missing_pin(self) -> None:
        self.assertEqual(self.request("POST", "/api/cancel", {})[0], 401)
        self.assertEqual(self.request("POST", "/api/cancel", b"[1,2]")[0], 401)

    def test_cross_site_post_is_blocked(self) -> None:
        body = {"seconds": 600}
        self.assertEqual(self.post("/api/shutdown", body, headers={"Origin": "http://evil.example"})[0], 403)
        self.assertEqual(self.post("/api/shutdown", body, headers={"Content-Type": "text/plain"})[0], 415)
        same = {"Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.post("/api/shutdown", body, headers=same)[0], 200)

    def test_dns_rebinding_host_is_blocked(self) -> None:
        self.assertEqual(self.request("GET", "/api/info", host="evil.example")[0], 403)
        self.assertEqual(self.post("/api/shutdown", {"seconds": 600}, host="evil.example")[0], 403)
        self.assertEqual(self.run.calls, [])

    def test_info_gives_pin_to_this_pc(self) -> None:
        code, info = self.request("GET", "/api/info")
        self.assertEqual(code, 200)
        self.assertTrue(info["local"])
        self.assertEqual(info["pin"], PIN)
        self.assertEqual(info["urls"][0], f"http://localhost:{self.port}")

    def test_windows_refusal_becomes_error(self) -> None:
        self.run.fail_all = True
        code, body = self.post("/api/shutdown", {"seconds": 600})
        self.assertEqual(code, 500)
        self.assertIn("error", body)
        self.assertFalse(self.request("GET", "/api/status")[1]["scheduled"])

    def test_unknown_route(self) -> None:
        self.assertEqual(self.request("GET", "/api/nada")[0], 404)
        self.assertEqual(self.post("/api/nada")[0], 404)


if __name__ == "__main__":
    unittest.main()
