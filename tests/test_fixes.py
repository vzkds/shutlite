"""Testes das correções desta rodada: agendamento seguro, PIN com 2 camadas,
restauração com checagem de boot, --check/--stop, run_shutdown e shutlite.bat.

Nada aqui chama shutdown.exe: os runners são falsos e run_shutdown tem o
subprocess trocado por um mock. O relógio é falso (sem sleep), e as portas
são efêmeras. Roda em Windows e Ubuntu; o que é só Windows usa skipUnless.
"""

from __future__ import annotations

import contextlib
import http.client
import http.server
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import logging  # noqa: E402

import server  # noqa: E402

# Os avisos esperados do shutlite (ex.: "não consegui entregar") são checados com assertLogs nos testes;
# sem um handler, o Python imprimiria tudo no stderr da suíte. assertLogs continua funcionando normalmente.
logging.getLogger("shutlite").addHandler(logging.NullHandler())


class FakeClock:
    """Substitui server.time.time e server.time.monotonic: o tempo só anda quando o teste manda.

    Os dois relógios são independentes: `advance()` é tempo passando de verdade (os dois andam juntos);
    `jump()` só mexe no relógio de parede (NTP, ajuste manual, ou um sono que o monotônico não conta).
    """

    def __init__(self, t: float = 1_000_000.0) -> None:
        self.t = t  # parede
        self.m = 5_000.0  # monotônico (origem arbitrária, diferente da parede)

    def time(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.m

    def advance(self, seconds: float) -> None:
        self.t += seconds
        self.m += seconds

    def jump(self, seconds: float) -> None:
        self.t += seconds  # só a parede


def use_clock(testcase: unittest.TestCase, clock: FakeClock) -> FakeClock:
    for name in ("time", "monotonic"):
        patcher = mock.patch.object(server.time, name, getattr(clock, name))
        patcher.start()
        testcase.addCleanup(patcher.stop)
    return clock


def _silence(st: "server.ShutdownState") -> None:
    """Teardown de um estado de teste: para o timer SEM entregar nada ao Windows e tira a contagem local
    que ficou, para o servidor não entregar nada ao fechar (senão a suíte ganha entregas e logs fantasmas)."""
    st.stop(deliver=False)
    try:
        st.cancel()  # só limpa o estado (o runner é falso); uma falha aqui não importa no teardown
    except server.ShutdownError:
        pass


class _SignalOnAcquire:
    """Envolve o _op: avisa quando a thread nomeada tenta pegá-lo. Torna a corrida determinística."""

    def __init__(self, real, event: threading.Event, thread_name: str) -> None:
        self.real, self.event, self.thread_name = real, event, thread_name

    def acquire(self, *a, **kw):
        if threading.current_thread().name == self.thread_name:
            self.event.set()
        return self.real.acquire(*a, **kw)

    def release(self) -> None:
        self.real.release()


class Scripted:
    """Runner que responde na ordem de `results`; depois usa `default`.

    `tick` (opcional) é chamado a cada comando, para simular que o Windows
    demora (o relógio falso avança).
    """

    def __init__(self, results=(), default=True, tick=None):
        self.results = list(results)
        self.default = default
        self.tick = tick
        self.calls: list[tuple[str, int]] = []

    def __call__(self, action, seconds):
        self.calls.append((action, seconds))
        if self.tick:
            self.tick()
        return self.results.pop(0) if self.results else self.default


def new_state(runner, store: Path, boot=lambda: None):
    # native_timer=True: fluxo clássico (tudo via shutdown.exe na hora); o padrão silencioso é testado em SilentModeTests
    return server.ShutdownState(runner, store, boot=boot, native_timer=True)


# ------------------------------------------------------------------ 1. agendamento

class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "state.json"
        self.clock = use_clock(self, FakeClock())

    def armed(self, seconds=600, action="shutdown"):
        run = Scripted()
        st = new_state(run, self.store)
        st.schedule(seconds, action)
        run.calls.clear()
        return st, run

    def test_abort_fails_keeps_old_state(self):
        st, run = self.armed()
        before = st.snapshot()
        run.results = [False]  # o abort falha
        with self.assertRaises(server.ShutdownError) as cm:
            st.schedule(1200, "restart")
        self.assertEqual(str(cm.exception),
                         "Não consegui cancelar o agendamento atual; ele continua valendo.")
        self.assertEqual(run.calls, [("abort", 0)])
        after = st.snapshot()
        self.assertTrue(after["scheduled"])
        self.assertEqual((after["action"], after["total"], after["rev"]),
                         (before["action"], before["total"], before["rev"]))

    def test_new_fails_old_restored(self):
        st, run = self.armed(600, "shutdown")
        run.results = [True, False, True, True]  # abort ok, novo falha, abort final ok, restaura ok
        with self.assertRaises(server.ShutdownError) as cm:
            st.schedule(1200, "restart")
        self.assertEqual(str(cm.exception), "Não consegui criar o novo agendamento; o anterior foi mantido.")
        self.assertEqual(run.calls, [("abort", 0), ("restart", 1200), ("abort", 0), ("shutdown", 600)])
        snap = st.snapshot()
        self.assertTrue(snap["scheduled"])
        self.assertEqual((snap["action"], snap["total"]), ("shutdown", 600))
        self.assertIn("at", json.loads(self.store.read_text(encoding="utf-8")))

    def test_restored_remaining_time_counts_down(self):
        st, run = self.armed(600, "shutdown")
        self.clock.advance(100)
        run.results = [True, False, True, True]
        with self.assertRaises(server.ShutdownError):
            st.schedule(1200, "restart")
        self.assertEqual(run.calls[-1], ("shutdown", 500))  # 600 - 100 s já passados

    def test_new_fails_restore_fails(self):
        st, run = self.armed()
        run.results = [True, False, True, False]  # abort ok, novo falha, abort final ok, restaura falha
        with self.assertRaises(server.ShutdownError) as cm:
            st.schedule(1200, "restart")
        self.assertEqual(str(cm.exception),
                         "Não consegui criar o novo agendamento e o anterior foi cancelado. Nada está agendado.")
        self.assertFalse(st.snapshot()["scheduled"])
        self.assertFalse(self.store.exists())

    def test_old_with_min_seconds_left_is_not_restored(self):
        st, run = self.armed(server.MIN_SECONDS)
        self.clock.advance(1)  # sobra MIN_SECONDS - 1 <= MIN_SECONDS
        run.results = [True, False, True]
        with self.assertRaises(server.ShutdownError) as cm:
            st.schedule(1200, "restart")
        self.assertIn("anterior foi cancelado", str(cm.exception))
        self.assertEqual([c[0] for c in run.calls], ["abort", "restart", "abort"])  # sem restaurar

    def test_old_just_above_min_seconds_is_restored(self):
        st, run = self.armed(server.MIN_SECONDS + 5)  # 10 s
        self.clock.advance(4)  # sobram 6 s > MIN_SECONDS
        run.results = [True, False, True, True]
        with self.assertRaises(server.ShutdownError) as cm:
            st.schedule(1200, "restart")
        self.assertIn("foi mantido", str(cm.exception))
        self.assertEqual(run.calls, [("abort", 0), ("restart", 1200), ("abort", 0), ("shutdown", 6)])

    def test_abort_final_fails_unknown_state(self):
        st, run = self.armed()
        run.results = [True, False, False, False]  # abort ok, novo falha, abort final falha, restaura falha
        with self.assertRaises(server.ShutdownError) as cm:
            st.schedule(1200, "restart")
        self.assertIn("não consegui confirmar que não há nada agendado", str(cm.exception))
        self.assertIn("Use Cancelar tudo para garantir", str(cm.exception))
        self.assertFalse(st.snapshot()["scheduled"])

    def test_not_pending_plain_refusal(self):
        run = Scripted([False, True, False, True])  # novo falha, abort ok, novo falha, abort final ok
        st = new_state(run, self.store)
        with self.assertRaises(server.ShutdownError) as cm:
            st.schedule(600, "shutdown")
        self.assertEqual(str(cm.exception), "O Windows recusou o comando. Tente de novo.")
        self.assertEqual(run.calls, [("shutdown", 600), ("abort", 0), ("shutdown", 600), ("abort", 0)])

    def test_not_pending_everything_fails(self):
        run = Scripted(default=False)
        st = new_state(run, self.store)
        with self.assertRaises(server.ShutdownError) as cm:
            st.schedule(600, "shutdown")
        self.assertIn("Use Cancelar tudo para garantir", str(cm.exception))

    def test_external_schedule_replaced_still_works(self):
        run = Scripted([False, True, True])  # novo falha (1190), abort ok, novo ok
        st = new_state(run, self.store)
        self.assertTrue(st.schedule(600, "shutdown")["scheduled"])
        self.assertEqual(run.calls, [("shutdown", 600), ("abort", 0), ("shutdown", 600)])

    def test_deadline_counts_from_before_runner(self):
        run = Scripted(tick=lambda: self.clock.advance(0.5))  # o Windows "demora" 0,5 s
        st = new_state(run, self.store)
        t_start = self.clock.t
        st.schedule(600, "shutdown")
        self.assertEqual(st._at, t_start + 600)  # não t_start + 600,5

    def test_deadline_with_pending_counts_from_after_abort(self):
        st, run = self.armed(600)
        run.tick = lambda: self.clock.advance(0.5)  # cada comando do Windows demora 0,5 s
        t_start = self.clock.t
        st.schedule(900, "restart")
        # abort (0,5 s) acontece antes; o timer do restart começa depois dele
        self.assertEqual(st._at, t_start + 0.5 + 900)
        self.assertEqual(st.snapshot()["total"], 900)


# ------------------------------------------------------------------ 2. PIN

class GuardKeyTests(unittest.TestCase):
    def test_guard_key(self):
        k = server.guard_key
        self.assertEqual(k("192.168.1.5"), "192.168.1.5")
        self.assertEqual(k("::ffff:192.168.1.5"), "192.168.1.5")
        self.assertEqual(k("2001:db8:1:2:aaaa:bbbb:cccc:dddd"), k("2001:db8:1:2::1"))
        self.assertNotEqual(k("2001:db8:1:2::1"), k("2001:db8:1:3::1"))
        self.assertEqual(k("2001:db8:1:2::1"), "2001:db8:1:2::/64")
        self.assertEqual(k("::1"), "::1")
        self.assertEqual(k("fe80::1%eth0"), "fe80::/64")
        self.assertEqual(k("lixo"), "lixo")


class PinGuardTests(unittest.TestCase):
    def test_default_limits_match_spec(self):
        g = server.PinGuard()
        self.assertEqual((g._max, g._lock_seconds), (5, 300))
        self.assertEqual((g._gmax, g._gwindow, g._glock, g._gcap), (20, 600, 300, 3600))

    def test_ipv6_same_64_shares_counter(self):
        g = server.PinGuard(max_fails=3, lock_seconds=60)
        for a in ("2001:db8:1:2::1", "2001:db8:1:2::2", "2001:db8:1:2:ffff::9"):
            g.fail(a)
        self.assertGreater(g.blocked("2001:db8:1:2::77"), 0)
        self.assertEqual(g.blocked("2001:db8:1:3::1"), 0)

    def test_client_lock_expires(self):
        clock = FakeClock(0.0)
        g = server.PinGuard(max_fails=2, lock_seconds=60, clock=clock.time)
        g.fail("10.0.0.9"); g.fail("10.0.0.9")
        self.assertEqual(g.blocked("10.0.0.9"), 60)
        clock.advance(59)
        self.assertEqual(g.blocked("10.0.0.9"), 1)
        clock.advance(1)
        self.assertEqual(g.blocked("10.0.0.9"), 0)

    def test_global_limit_backoff_and_reset(self):
        clock = FakeClock(1000.0)
        g = server.PinGuard(max_fails=5, lock_seconds=300, global_max=4, global_window=600,
                            global_lock=300, global_cap=3600, clock=clock.time)
        spans = []
        for round_ in range(6):
            for i in range(4):  # 4 clientes, 1 erro cada: nenhum chega ao limite por cliente
                g.fail(f"10.0.{round_}.{i}")
            wait = g.blocked("10.9.9.9")  # cliente que nunca errou
            spans.append(wait)
            self.assertEqual(g.blocked("127.0.0.1"), 0)  # dono do PC nunca é travado pelo global
            clock.advance(wait + 1)
        self.assertEqual(spans, [300, 600, 1200, 2400, 3600, 3600])
        clock.advance(3601 + 3600)  # muito tempo sem ataque: volta ao início
        for i in range(4):
            g.fail(f"10.1.0.{i}")
        self.assertEqual(g.blocked("10.9.9.9"), 300)

    def test_global_window_expires(self):
        clock = FakeClock(0.0)
        g = server.PinGuard(global_max=4, global_window=600, clock=clock.time)
        for i in range(3):
            g.fail(f"10.0.0.{i}")
        clock.advance(700)  # os 3 erros saíram da janela de 600 s
        g.fail("10.0.0.9")
        self.assertEqual(g.blocked("10.8.8.8"), 0)

    def test_loopback_exempt_from_global(self):
        clock = FakeClock(0.0)
        g = server.PinGuard(max_fails=5, global_max=4, clock=clock.time)
        for _ in range(3):
            g.fail("127.0.0.1")
            g.fail("::1")
        self.assertEqual(g.blocked("10.8.8.8"), 0)  # erros do dono não contam na rede
        self.assertEqual(g._gfails, [])
        for _ in range(4):
            g.fail("127.0.0.1")
        self.assertGreater(g.blocked("127.0.0.1"), 0)  # a camada por cliente ainda vale
        self.assertEqual(g.blocked("10.8.8.8"), 0)

    def test_correct_pin_does_not_clear_global(self):
        clock = FakeClock(0.0)
        g = server.PinGuard(global_max=2, clock=clock.time)
        g.fail("10.0.0.1"); g.fail("10.0.0.2")
        self.assertGreater(g.blocked("10.0.0.3"), 0)
        g.ok("10.0.0.3")  # PIN certo zera só o cliente
        self.assertGreater(g.blocked("10.0.0.3"), 0)

    def test_correct_pin_clears_own_client_counter(self):
        g = server.PinGuard(max_fails=3, lock_seconds=60)
        g.fail("10.0.0.5"); g.fail("10.0.0.5")
        g.ok("10.0.0.5")
        g.fail("10.0.0.5")  # contador zerado: esta é só a 1ª de novo
        self.assertEqual(g.blocked("10.0.0.5"), 0)

    def test_every_wrong_pin_is_logged_without_the_pin(self):
        g = server.PinGuard(max_fails=5)
        with self.assertLogs("shutlite", "WARNING") as cm:
            g.fail("192.168.1.9")
            g.fail("192.168.1.9")
        self.assertEqual(len(cm.records), 2)
        self.assertIn("192.168.1.9", cm.records[0].getMessage())
        self.assertIn("erro 1 de 5", cm.records[0].getMessage())
        self.assertIn("erro 2 de 5", cm.records[1].getMessage())
        self.assertNotIn("482913", "\n".join(cm.output))


# ------------------------------------------------------------------ Config (MIN_PIN_LEN = 6)

class ConfigTests(unittest.TestCase):
    def args(self, **kw):
        base = dict(data_dir=None, port=None, pin=None, bind=None, allow_public=False,
                    allow_host=None, dry_run=False)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_min_pin_from_cli(self):
        self.assertEqual(server.MIN_PIN_LEN, 6)
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ValueError) as cm:
                server.Config.load(self.args(data_dir=d, pin="Ab3z"))
            msg = str(cm.exception)
            self.assertIn("--pin", msg)
            self.assertIn("config.json", msg)
            self.assertIn("6", msg)
            self.assertNotIn("Ab3z", msg)  # nunca repete o PIN
            self.assertEqual(server.Config.load(self.args(data_dir=d, pin="Ab3zQ9")).pin, "Ab3zQ9")

    def test_short_pin_from_config_with_bom(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_file = Path(d) / "config.json"
            cfg_file.write_bytes(b'\xef\xbb\xbf{"pin": "1234", "port": 4444}')  # BOM do Bloco de Notas
            with self.assertRaises(ValueError) as cm:
                server.Config.load(self.args(data_dir=d))
            self.assertIn("config.json", str(cm.exception))
            cfg_file.write_bytes(b'\xef\xbb\xbf{"pin": "abcdef", "port": 4444}')
            cfg = server.Config.load(self.args(data_dir=d))
            self.assertEqual((cfg.pin, cfg.port), ("abcdef", 4444))  # BOM não apaga a config

    def test_env_pin_short(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("PIN")
            os.environ["PIN"] = "999"
            try:
                with self.assertRaises(ValueError) as cm:
                    server.Config.load(self.args(data_dir=d))
                self.assertIn("variável de ambiente PIN", str(cm.exception))
                self.assertNotIn("999", str(cm.exception))
            finally:
                os.environ.pop("PIN", None)
                if old is not None:
                    os.environ["PIN"] = old


# ------------------------------------------------------------------ --stop e --check (servidor real, dry-run)

class StopCheckTests(unittest.TestCase):
    def start(self, pin="482913"):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = server.Config(port=0, pin=pin, data_dir=Path(tmp.name), bind="127.0.0.1", dry_run=True)
        srv = server.build_server(cfg, Scripted())
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv, Path(tmp.name)

    def run_stop(self, port, pin, tmpdir):
        cfg = server.Config(port=port, pin=pin, data_dir=tmpdir, bind="127.0.0.1")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = server.stop_running(cfg)
        return code, buf.getvalue()

    def free_port(self) -> int:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    def test_refused_means_not_running(self):
        with tempfile.TemporaryDirectory() as d:
            code, out = self.run_stop(self.free_port(), "482913", Path(d))
            self.assertEqual(code, 1)
            self.assertIn("não está rodando", out)

    def test_wrong_pin_then_blocked_then_right_pin(self):
        srv, tmp = self.start()
        port = srv.server_address[1]
        code, out = self.run_stop(port, "wrong123", tmp)
        self.assertEqual(code, 1)
        self.assertIn("PIN deste computador é diferente", out)
        self.assertNotIn("não está rodando", out)
        for _ in range(4):
            self.run_stop(port, "wrong123", tmp)
        code, out = self.run_stop(port, "482913", tmp)  # certo, mas este cliente está travado
        self.assertEqual(code, 1)
        self.assertIn("bloqueou novas tentativas", out)
        self.assertRegex(out, r"Espere \d+ s")

    def test_stop_ok_returns_zero(self):
        srv, tmp = self.start()
        code, out = self.run_stop(srv.server_address[1], "482913", tmp)
        self.assertEqual((code, out.strip()), (0, "shutlite parado."))

    def test_check_running_and_dead(self):
        srv, tmp = self.start()
        cfg = server.Config(port=srv.server_address[1], pin="482913", data_dir=tmp, bind="127.0.0.1")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(server.check_running(cfg), 0)
        self.assertIn("rodando", buf.getvalue())
        dead = server.Config(port=self.free_port(), pin="482913", data_dir=tmp, bind="127.0.0.1")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(server.check_running(dead), 1)
        self.assertIn("shutlite.log", buf.getvalue())

    def test_check_rejects_foreign_server_on_port(self):
        import http.server

        class Other(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # responde 200 com HTML: outro programa, não o shutlite
                body = b"<html>outro programa</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Other)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with tempfile.TemporaryDirectory() as d:
            cfg = server.Config(port=httpd.server_address[1], pin="482913", data_dir=Path(d), bind="127.0.0.1")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                self.assertEqual(server.check_running(cfg), 1)  # exit code: correto

    def test_check_foreign_html_message_says_not_shutlite(self):
        import http.server

        class Other(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = b"<html>outro programa</html>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), Other)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        self.addCleanup(t.join, 5)
        self.addCleanup(httpd.server_close)
        self.addCleanup(httpd.shutdown)
        with tempfile.TemporaryDirectory() as d:
            cfg = server.Config(port=httpd.server_address[1], pin="482913", data_dir=Path(d), bind="127.0.0.1")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                server.check_running(cfg)
            self.assertIn("não parece o shutlite", buf.getvalue())

    def test_self_url(self):
        def mk(bind):
            return server._self_url(server.Config(port=3333, pin="123456", data_dir=Path("."), bind=bind), "/x")
        self.assertEqual(mk(""), "http://127.0.0.1:3333/x")
        self.assertEqual(mk("0.0.0.0"), "http://127.0.0.1:3333/x")
        self.assertEqual(mk("::"), "http://[::1]:3333/x")
        self.assertEqual(mk("::1"), "http://[::1]:3333/x")
        self.assertEqual(mk("192.168.1.5"), "http://192.168.1.5:3333/x")


# ------------------------------------------------------------------ 3. _restore com boot

class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Path(self.tmp.name) / "state.json"
        self.clock = use_clock(self, FakeClock(1_000_000.0))

    def write(self, **over):
        data = {"at": self.clock.t + 600, "action": "shutdown", "total": 600}
        data.update(over)
        self.store.write_text(json.dumps(data), encoding="utf-8")

    def state(self, boot):
        return server.ShutdownState(Scripted(), self.store, boot=boot)

    def test_boot_is_saved_rounded(self):
        st = server.ShutdownState(Scripted(), self.store, boot=lambda: 1000.4)
        st.schedule(600, "shutdown")
        self.assertEqual(json.loads(self.store.read_text(encoding="utf-8"))["boot"], 1000)

    def test_no_boot_field_when_unknown(self):
        st = server.ShutdownState(Scripted(), self.store, boot=lambda: None)
        st.schedule(600, "shutdown")
        self.assertNotIn("boot", json.loads(self.store.read_text(encoding="utf-8")))

    def test_tolerance_is_30(self):
        self.assertEqual(server.BOOT_TOLERANCE, 30)

    def test_boot_difference_30_accepted(self):
        self.write(boot=1000)
        self.assertTrue(self.state(lambda: 1030).snapshot()["scheduled"])

    def test_boot_difference_31_discarded_and_file_removed(self):
        self.write(boot=1000)
        with self.assertLogs("shutlite", "WARNING") as cm:
            snap = self.state(lambda: 1031).snapshot()
        self.assertFalse(snap["scheduled"])
        self.assertFalse(self.store.exists())
        self.assertIn("reiniciado", cm.output[0])

    def test_old_file_without_boot_is_accepted(self):
        self.write()
        self.assertTrue(self.state(lambda: 123456.0).snapshot()["scheduled"])

    def test_boot_unknown_now_skips_check(self):
        self.write(boot=1000)
        self.assertTrue(self.state(lambda: None).snapshot()["scheduled"])

    def test_bad_values_are_discarded(self):
        now = self.clock.t
        cases = [
            {"at": float("inf")}, {"at": float("nan")}, {"at": True}, {"at": "amanha"},
            {"at": now + server.MAX_SECONDS + 3601}, {"at": now - 1}, {"action": "hibernate"},
        ]
        for over in cases:
            with self.subTest(over=over):
                self.write(**over)
                self.assertFalse(self.state(lambda: None).snapshot()["scheduled"])
                self.assertFalse(self.store.exists())

    def test_at_just_inside_limit_accepted(self):
        self.write(at=self.clock.t + server.MAX_SECONDS + 3600)
        self.assertTrue(self.state(lambda: None).snapshot()["scheduled"])

    def test_corrupt_file_is_ignored(self):
        self.store.write_text("{isto não é json", encoding="utf-8")
        self.assertFalse(self.state(lambda: None).snapshot()["scheduled"])


class RealBootTests(unittest.TestCase):
    """Usa o relógio e o boot reais. Só faz afirmações estáveis."""

    @unittest.skipUnless(os.name == "nt", "boot_time real só existe no Windows")
    def test_real_boot_time_sane_on_windows(self):
        b1 = server.boot_time()
        self.assertIsNotNone(b1)
        self.assertLess(b1, time.time())
        self.assertLess(abs(server.boot_time() - b1), 5)

    @unittest.skipIf(os.name == "nt", "só para sistemas que não são Windows")
    def test_real_boot_time_none_elsewhere(self):
        self.assertIsNone(server.boot_time())


# ------------------------------------------------------------------ 5. run_shutdown (subprocess falso)

class RunShutdownTests(unittest.TestCase):
    def fake(self, *, returncode=0, side_effect=None):
        result = SimpleNamespace(returncode=returncode, stdout="", stderr="falha simulada")
        return mock.patch.object(server.subprocess, "run", return_value=result, side_effect=side_effect)

    def test_shutdown_command(self):
        with self.fake() as run:
            self.assertTrue(server.run_shutdown("shutdown", 600))
        self.assertEqual(run.call_args[0][0], ["shutdown.exe", "/s", "/t", "600"])
        self.assertEqual(run.call_args[1]["timeout"], 6)  # B2: antes 15 s
        self.assertEqual(server.SHUTDOWN_TIMEOUT, 6)

    def test_restart_command(self):
        with self.fake() as run:
            self.assertTrue(server.run_shutdown("restart", 30))
        self.assertEqual(run.call_args[0][0], ["shutdown.exe", "/r", "/t", "30"])

    def test_abort_command(self):
        with self.fake() as run:
            self.assertTrue(server.run_shutdown("abort", 0))
        self.assertEqual(run.call_args[0][0], ["shutdown.exe", "/a"])

    def test_abort_1116_nothing_to_abort_is_success(self):
        with self.fake(returncode=server.ERROR_NOTHING_TO_ABORT):
            self.assertTrue(server.run_shutdown("abort", 0))

    def test_shutdown_1116_is_failure(self):
        with self.fake(returncode=server.ERROR_NOTHING_TO_ABORT):
            self.assertFalse(server.run_shutdown("shutdown", 600))

    def test_nonzero_exit_is_failure(self):
        with self.fake(returncode=1190), self.assertLogs("shutlite", "ERROR"):
            self.assertFalse(server.run_shutdown("shutdown", 600))

    def test_missing_shutdown_exe_is_failure(self):
        with self.fake(side_effect=FileNotFoundError()), self.assertLogs("shutlite", "ERROR"):
            self.assertFalse(server.run_shutdown("shutdown", 600))

    def test_timeout_is_failure(self):
        with self.fake(side_effect=subprocess.TimeoutExpired(["shutdown.exe"], 15)), \
                self.assertLogs("shutlite", "ERROR"):
            self.assertFalse(server.run_shutdown("shutdown", 600))

    def test_oserror_is_failure(self):
        with self.fake(side_effect=OSError("boom")), self.assertLogs("shutlite", "ERROR"):
            self.assertFalse(server.run_shutdown("abort", 0))


# ------------------------------------------------------------------ 4. shutlite.bat (só checagem de arquivo)

class LauncherTests(unittest.TestCase):
    def test_bat_exists(self):
        self.assertTrue((ROOT / "shutlite.bat").is_file())

    @unittest.skipUnless(os.name == "nt", "a checagem de CRLF depende do autocrlf do checkout")
    def test_bat_uses_crlf(self):
        data = (ROOT / "shutlite.bat").read_bytes()
        self.assertIn(b"\r\n", data)
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"), "há LF solto (sem CR)")


# ------------------------------------------------------------------ rodada de endurecimento (Ward)

class LocalNetTests(unittest.TestCase):
    """is_local_net: lista explícita, não ipaddress.is_private."""

    def test_accepts_home_network_addresses(self):
        for addr in ("192.168.1.5", "10.0.0.1", "172.16.0.1", "172.31.255.254", "169.254.7.7", "127.0.0.1",
                     "::ffff:192.168.1.5", "::ffff:10.0.0.1", "fd00::1", "fc00::1", "fdff:ffff::1",
                     "fe80::1%eth0", "fe80::1", "febf::1", "::1"):
            with self.subTest(addr=addr):
                self.assertTrue(server.is_local_net(addr))

    def test_rejects_everything_else(self):
        for addr in ("8.8.8.8", "172.32.0.1", "172.15.255.255", "2002:0808:0808::1",  # 6to4
                     "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
                     "2001:db8::1",  # documentação
                     "198.18.0.5", "240.0.0.1", "100.64.0.1", "0.0.0.0", "::", "fec0::1", "fe00::1",
                     "::ffff:8.8.8.8", "::ffff:198.18.0.5", "2001:4860:4860::8888", "lixo", ""):
            with self.subTest(addr=addr):
                self.assertFalse(server.is_local_net(addr))

    def test_get_ipv6_follows_the_same_rule(self):
        addrs = ["fd00::1", "2002:808:808::1", "2001:db8::1", "2001:0:4136:e378:8000:63bf:3fff:fdd2",
                 "fe80::1", "::1", "fd12:3456::9", "fd00::1"]
        infos = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", (a, 0, 0, 0)) for a in addrs]
        with mock.patch.object(server.socket, "getaddrinfo", return_value=infos):
            private = server.get_ipv6(False)
            public = server.get_ipv6(True)
        self.assertEqual(private, ["fd00::1", "fd12:3456::9"])  # sem duplicatas, só ULA
        self.assertTrue(all(server.is_local_net(a) for a in private))
        self.assertEqual(public, ["fd00::1", "2002:808:808::1", "2001:db8::1",
                                  "2001:0:4136:e378:8000:63bf:3fff:fdd2", "fd12:3456::9"])  # sem ::1 e fe80


class TamperedFilesTests(unittest.TestCase):
    """state.json / config.json adulterados não derrubam o boot."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = self.dir / "state.json"
        self.clock = use_clock(self, FakeClock(1_000_000.0))
        self.future = int(self.clock.t) + 600

    def restore(self, raw: str, boot=lambda: 1000.0):
        self.store.write_text(raw, encoding="utf-8")
        with self.assertLogs("shutlite", "WARNING") as cm:
            st = server.ShutdownState(Scripted(), self.store, boot=boot)
            snap = st.snapshot()
            server.LOG.warning("sentinela")  # garante que assertLogs tenha o que ler mesmo sem aviso
        return snap, cm.output

    def assert_discarded(self, raw: str):
        snap, logs = self.restore(raw)
        self.assertFalse(snap["scheduled"])
        self.assertFalse(self.store.exists(), "arquivo inválido deveria ter sido apagado")
        self.assertTrue(any("descartado" in line for line in logs), logs)

    def test_at_with_400_digits(self):
        self.assert_discarded('{"at": %s, "action": "shutdown", "total": 60}' % ("9" * 400))

    def test_at_1e400_is_inf(self):
        self.assert_discarded('{"at": 1e400, "action": "shutdown", "total": 60}')

    def test_boot_with_400_digits(self):
        self.assert_discarded('{"at": %d, "action": "shutdown", "total": 600, "boot": %s}'
                              % (self.future, "9" * 400))

    def test_boot_infinity_or_nan(self):
        for bad in ("Infinity", "-Infinity", "NaN", "1e400"):
            with self.subTest(boot=bad):
                self.assert_discarded('{"at": %d, "action": "shutdown", "total": 600, "boot": %s}'
                                      % (self.future, bad))

    def test_total_is_capped_to_max_seconds(self):
        self.store.write_text('{"at": %d, "action": "restart", "total": %s, "boot": 1000}'
                              % (self.future, "9" * 400), encoding="utf-8")
        snap = server.ShutdownState(Scripted(), self.store, boot=lambda: 1000.0).snapshot()
        self.assertTrue(snap["scheduled"])
        self.assertEqual(snap["total"], server.MAX_SECONDS)
        self.store.write_text('{"at": %d, "action": "restart", "total": %d}'
                              % (self.future, server.MAX_SECONDS + 5), encoding="utf-8")
        self.assertEqual(server.ShutdownState(Scripted(), self.store).snapshot()["total"], server.MAX_SECONDS)

    def test_deeply_nested_json_is_ignored_and_removed(self):
        self.store.write_text("[" * 50000, encoding="utf-8")
        snap = server.ShutdownState(Scripted(), self.store, boot=lambda: None).snapshot()
        self.assertFalse(snap["scheduled"])
        self.assertFalse(self.store.exists())

    def test_valid_state_still_restores(self):
        self.store.write_text('{"at": %d, "action": "shutdown", "total": 600, "boot": 1000}' % self.future,
                              encoding="utf-8")
        self.assertTrue(server.ShutdownState(Scripted(), self.store, boot=lambda: 1000.0).snapshot()["scheduled"])

    # -- config.json
    def load(self, raw: str):
        (self.dir / "config.json").write_text(raw, encoding="utf-8")
        args = SimpleNamespace(data_dir=str(self.dir), port=None, pin=None, bind=None, allow_public=False,
                               allow_host=None, dry_run=False)
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PORT", None)
            os.environ.pop("PIN", None)
            return server.Config.load(args)

    def test_bad_ports_in_config_are_clear_value_errors(self):
        for bad in ("1e400", "-1e400", "NaN", "true", "80.5", '"abc"', "[1]", '{"a": 1}', "70000",
                    "-5", "9" * 400):
            with self.subTest(port=bad):
                with self.assertRaises(ValueError) as cm:
                    self.load('{"port": %s, "pin": "123456"}' % bad)
                self.assertIn("porta inválida", str(cm.exception))
                self.assertLess(len(str(cm.exception)), 80)  # não despeja os 400 dígitos

    def test_good_ports_in_config(self):
        for good, want in (("4444", 4444), ('"4444"', 4444), ("3333.0", 3333)):
            with self.subTest(port=good):
                self.assertEqual(self.load('{"port": %s, "pin": "123456"}' % good).port, want)

    def test_bad_port_does_not_touch_config_file(self):
        raw = '{"port": 1e400}'
        with self.assertRaises(ValueError):
            self.load(raw)
        self.assertEqual((self.dir / "config.json").read_text(encoding="utf-8"), raw)  # sem PIN novo

    def test_main_reports_bad_config_without_traceback(self):
        (self.dir / "config.json").write_text('{"port": 1e400, "pin": "123456"}', encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PORT", None)
            with self.assertRaises(SystemExit) as cm:
                server.main(["--info", "--data-dir", str(self.dir)])
        self.assertIn("porta inválida", str(cm.exception))

    def test_main_logs_config_error_when_there_is_no_console(self):
        """pythonw: sys.stderr é None, então a mensagem tem de ir para o shutlite.log."""
        (self.dir / "config.json").write_text('{"port": 1e400, "pin": "123456"}', encoding="utf-8")
        with mock.patch.dict(os.environ, {}, clear=False), mock.patch.object(server.sys, "stderr", None):
            os.environ.pop("PORT", None)
            with self.assertRaises(SystemExit):
                server.main(["--info", "--data-dir", str(self.dir)])
        self.assertIn("porta inválida", (self.dir / "shutlite.log").read_text(encoding="utf-8"))


class BatHardeningTests(unittest.TestCase):
    def test_no_default_current_directory_is_set_before_any_command(self):
        lines = [ln.strip().lower() for ln in (ROOT / "shutlite.bat").read_text(encoding="ascii").splitlines()]
        i_setlocal = lines.index("setlocal")
        i_set = lines.index('set "nodefaultcurrentdirectoryinexepath=1"')
        i_first = next(i for i, ln in enumerate(lines) if ln.startswith(("cd ", "python ", "py ", "%py%")))
        self.assertLess(i_setlocal, i_set)
        self.assertLess(i_set, i_first)


class _Dummy:
    """Servidor HTTP de mentira numa porta efêmera; guarda cada requisição que recebe."""

    def __init__(self, testcase: unittest.TestCase, behave):
        self.hits: list[tuple[str, str]] = []
        owner = self

        class H(http.server.BaseHTTPRequestHandler):
            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(n)
                owner.hits.append((self.command, self.path))
                behave(self)

            do_GET = do_POST = _go

            def log_message(self, *a):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        t = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        t.start()
        testcase.addCleanup(t.join, 5)
        testcase.addCleanup(self.httpd.server_close)
        testcase.addCleanup(self.httpd.shutdown)


def _reply(handler, code, body=b"", headers=()):
    handler.send_response(code)
    for k, v in headers:
        handler.send_header(k, v)
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class NoRedirectTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def cfg(self, port):
        return server.Config(port=port, pin="482913", data_dir=Path(self.tmp.name), bind="127.0.0.1")

    def run_cmd(self, fn, cfg):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = fn(cfg)
        return code, buf.getvalue()

    def test_check_and_stop_do_not_follow_redirects(self):
        for status in (301, 302, 303, 307, 308):
            target = _Dummy(self, lambda h: _reply(h, 200, b'{"scheduled": false}'))
            origin = _Dummy(self, lambda h, s=status, t=target: _reply(
                h, s, headers=[("Location", f"http://127.0.0.1:{t.port}/api/status")]))
            with self.subTest(status=status, cmd="check"):
                code, out = self.run_cmd(server.check_running, self.cfg(origin.port))
                self.assertEqual(code, 1)
                self.assertIn("Outro programa está usando a porta", out)
                self.assertNotIn("rodando", out)
            with self.subTest(status=status, cmd="stop"):
                code, out = self.run_cmd(server.stop_running, self.cfg(origin.port))
                self.assertEqual(code, 1)
                self.assertIn("Outro programa está usando a porta", out)
                self.assertNotIn("parado", out)
            self.assertEqual(target.hits, [], "o redirecionamento foi seguido")
            self.assertEqual([m for m, _ in origin.hits], ["GET", "POST"])

    def test_opener_has_no_redirect_following(self):
        kinds = [type(h).__name__ for h in server._local_opener().handlers]
        self.assertIn("_NoRedirect", kinds)
        self.assertEqual(kinds.count("HTTPRedirectHandler"), 0)

    def test_opener_ignores_proxy_environment(self):
        ok = _Dummy(self, lambda h: _reply(h, 200, b'{"scheduled": false}'))
        dead_proxy = "http://127.0.0.1:1"  # porta fechada: se o proxy fosse usado, o --check falharia
        with mock.patch.dict(os.environ, {"http_proxy": dead_proxy, "HTTP_PROXY": dead_proxy}):
            os.environ.pop("no_proxy", None)
            os.environ.pop("NO_PROXY", None)
            code, _ = self.run_cmd(server.check_running, self.cfg(ok.port))
        self.assertEqual(code, 0)

    def test_plain_shutlite_answer_still_works(self):
        ok = _Dummy(self, lambda h: _reply(h, 200, b'{"scheduled": false}', [("Content-Type", "application/json")]))
        code, out = self.run_cmd(server.check_running, self.cfg(ok.port))
        self.assertEqual((code, out.strip()), (0, f"shutlite rodando na porta {ok.port}."))


class PrintableTests(unittest.TestCase):
    def test_control_characters_become_spaces(self):
        out = server._printable("a\x1b[31mb\nc\td\x00e\x7ff\x85g")
        self.assertEqual(out, "a [31mb c d e f g")
        self.assertFalse(any(unicodedata_category(ch) == "Cc" for ch in out))

    def test_invisible_formatting_and_lone_surrogates_are_dropped(self):
        self.assertEqual(server._printable("a‮b​c\ud800d"), "abcd")
        server._printable("x\ud800y").encode("utf-8")  # não levanta UnicodeEncodeError

    def test_limit_is_200(self):
        self.assertEqual(len(server._printable("x" * 200)), 200)
        long = server._printable("x" * 500)
        self.assertEqual(len(long), 200)
        self.assertTrue(long.endswith("..."))

    def test_http_error_text_cleans_server_text(self):
        def exc(body):
            return SimpleNamespace(read=lambda n: body)
        text = server._http_error_text(exc(json.dumps({"error": "boom\x1b[0m\nlinha‮2"}).encode()))
        self.assertEqual(text, "boom [0m linha2")
        self.assertEqual(len(server._http_error_text(exc(json.dumps({"error": "y" * 999}).encode()))), 200)
        self.assertEqual(server._http_error_text(exc(b"<html>nao e json</html>")), "")
        self.assertEqual(server._http_error_text(exc(b'["lista"]')), "")
        self.assertEqual(server._http_error_text(exc(b"\xff\xfe\x00")), "")

        def broken(n):
            raise http.client.IncompleteRead(b"x")
        self.assertEqual(server._http_error_text(SimpleNamespace(read=broken)), "")

    def test_stop_prints_only_clean_text_from_a_hostile_server(self):
        hostile = json.dumps({"error": "\x1b[2J\x1b[31mAPAGUE TUDO\nlinha2‮\ud800"})
        bad = _Dummy(self, lambda h: _reply(h, 500, hostile.encode("ascii"), [("Content-Type", "application/json")]))
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(buf):
            code = server.stop_running(server.Config(port=bad.port, pin="482913", data_dir=Path(d), bind="127.0.0.1"))
        out = buf.getvalue()
        self.assertEqual(code, 1)
        self.assertIn("HTTP 500", out)
        self.assertNotIn("\x1b", out)
        self.assertEqual(out.count("\n"), 1)  # só o fim da linha do print
        out.encode("utf-8")

    def test_retry_after_is_only_printed_when_it_is_a_short_ascii_number(self):
        for header, expect in (("42", "Espere 42 s"), ("²", "Espere alguns minutos"),
                               ("1234567", "Espere alguns minutos"), ("a\x1bb", "Espere alguns minutos")):
            with self.subTest(header=header):
                srv = _Dummy(self, lambda h, v=header: _reply(h, 429, b"{}", [("Retry-After", v)]))
                buf = io.StringIO()
                with tempfile.TemporaryDirectory() as d, contextlib.redirect_stdout(buf):
                    server.stop_running(server.Config(port=srv.port, pin="482913", data_dir=Path(d),
                                                      bind="127.0.0.1"))
                self.assertIn(expect, buf.getvalue())
                self.assertNotIn("\x1b", buf.getvalue())


def unicodedata_category(ch: str) -> str:
    import unicodedata
    return unicodedata.category(ch)


# ------------------------------------------------------------------ modo silencioso (padrão)

class SilentModeTests(unittest.TestCase):
    """Padrão: a contagem fica no shutlite e o Windows só é chamado no último minuto (HANDOFF_SECONDS)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = self.dir / "state.json"
        self.clock = use_clock(self, FakeClock(1_000_000.0))
        self.run = Scripted()

    def state(self, runner=None, **kw):
        kw.setdefault("boot", lambda: None)
        return server.ShutdownState(runner or self.run, self.store, **kw)

    def saved(self) -> dict:
        return json.loads(self.store.read_text(encoding="utf-8"))

    # -- agendar / adiar
    def test_long_schedule_makes_no_windows_calls(self):
        st = self.state()
        snap = st.schedule(600, "shutdown")
        self.assertEqual(self.run.calls, [])
        self.assertEqual((snap["scheduled"], snap["mode"], snap["action"], snap["total"], snap["left"]),
                         (True, "local", "shutdown", 600, 600))
        self.assertIs(self.saved()["handoff"], False)
        snap = st.schedule(1200, "restart")  # adiar/trocar antes do handoff: só estado
        self.assertEqual(self.run.calls, [])
        self.assertEqual((snap["mode"], snap["action"], snap["total"]), ("local", "restart", 1200))

    def test_idle_snapshot_keeps_old_fields_and_has_mode_none(self):
        snap = self.state().snapshot()
        self.assertEqual(snap, {"scheduled": False, "left": 0, "total": 0, "action": None, "mode": None,
                                "rev": 0})

    def test_schedule_up_to_handoff_window_is_immediate(self):
        for secs, expect_mode in ((5, "windows"), (30, "windows"), (server.HANDOFF_SECONDS, "windows"),
                                  (server.HANDOFF_SECONDS + 1, "local")):
            with self.subTest(seconds=secs):
                self.store.unlink(missing_ok=True)  # cada caso parte do zero (sem o agendamento do anterior)
                run = Scripted()
                snap = self.state(run).schedule(secs, "restart")
                self.assertEqual(snap["mode"], expect_mode)
                self.assertEqual(run.calls, [("restart", secs)] if expect_mode == "windows" else [])

    # -- handoff
    def test_handoff_at_t_minus_60_once(self):
        seen = []
        st = self.state(on_change=seen.append)
        st.schedule(600, "shutdown")
        self.clock.advance(539)  # faltam 61 s
        st.tick()
        self.assertEqual(self.run.calls, [])
        self.clock.advance(1)  # faltam 60 s
        st.tick()
        self.assertEqual(self.run.calls, [("shutdown", 60)])
        self.assertEqual(st.snapshot()["mode"], "windows")
        self.assertIs(self.saved()["handoff"], True)
        self.clock.advance(5)
        st.tick()
        st.tick()
        self.assertEqual(self.run.calls, [("shutdown", 60)], "o handoff acontece uma vez só")
        self.assertEqual([s["mode"] for s in seen], ["windows"])  # o painel é avisado (SSE)

    def test_after_handoff_postpone_aborts_then_goes_local(self):
        st = self.state()
        st.schedule(600, "shutdown")
        self.clock.advance(541)
        st.tick()
        self.run.calls.clear()
        snap = st.schedule(1200, "shutdown")  # já é do Windows: abort; o novo (longo) fica local
        self.assertEqual(self.run.calls, [("abort", 0)])
        self.assertEqual((snap["mode"], snap["total"]), ("local", 1200))

    # -- cancelar
    def test_cancel_before_handoff_does_not_call_windows(self):
        st = self.state()
        st.schedule(600, "shutdown")
        snap = st.cancel()
        self.assertEqual(self.run.calls, [])
        self.assertEqual((snap["scheduled"], snap["mode"]), (False, None))
        self.assertFalse(self.store.exists())

    def test_cancel_after_handoff_and_when_idle_call_abort(self):
        st = self.state()
        st.schedule(600, "shutdown")
        self.clock.advance(545)
        st.tick()
        self.run.calls.clear()
        self.assertFalse(st.cancel()["scheduled"])
        self.assertEqual(self.run.calls, [("abort", 0)])
        self.run.calls.clear()
        st.cancel()  # "Cancelar tudo" com o painel ocioso: abort para garantir
        self.assertEqual(self.run.calls, [("abort", 0)])

    # -- suspensão / prazo vencido
    def test_deadline_passed_while_suspended_is_discarded_never_late(self):
        st = self.state()
        st.schedule(600, "shutdown")
        self.clock.advance(700)  # o PC dormiu além do prazo
        with self.assertLogs("shutlite", "WARNING") as cm:
            st.tick()
        self.assertEqual(self.run.calls, [])
        self.assertFalse(st.snapshot()["scheduled"])
        self.assertFalse(self.store.exists())
        self.assertIn("nada será desligado", cm.output[0])
        # Regra nova: acordou dentro do último minuto, mas o sono não contou no monotônico (a parede
        # andou 560 s, ele não) → discordam → descarta; antes entregava o que faltava ("shutdown", 40).
        st.schedule(600, "shutdown")
        self.clock.jump(560)
        with self.assertLogs("shutlite", "WARNING") as cm2:
            st.tick()
        self.assertIn("relógio do sistema mudou ou o PC foi suspenso", cm2.output[0])
        self.assertEqual(self.run.calls, [])
        self.assertFalse(st.snapshot()["scheduled"])

    def test_handoff_failure_retries_each_tick_logs_once_then_discards_at_deadline(self):
        run = Scripted(default=False)
        st = self.state(run)
        st.schedule(600, "shutdown")
        self.clock.advance(541)  # faltam 59
        with self.assertLogs("shutlite", "ERROR") as cm:
            st.tick()  # 1ª falha: tenta também abort (timer externo); abort falha, sem 2ª tentativa
            self.clock.advance(1)
            st.tick()  # só uma tentativa por tick, sem novo LOG.error
        self.assertEqual(run.calls, [("shutdown", 59), ("abort", 0), ("shutdown", 58)])
        self.assertEqual(len(cm.output), 1)
        self.assertTrue(st.snapshot()["scheduled"])
        run.default = True  # o Windows voltou a responder: a próxima volta entrega
        self.clock.advance(1)
        st.tick()
        self.assertEqual(run.calls[-1], ("shutdown", 57))
        self.assertEqual(st.snapshot()["mode"], "windows")
        # outro agendamento, que nunca consegue entregar: descarta no prazo (não desliga atrasado)
        run2 = Scripted(default=False)
        st2 = server.ShutdownState(run2, self.dir / "other.json", boot=lambda: None)
        st2.schedule(600, "restart")
        self.clock.advance(545)
        with self.assertLogs("shutlite", "ERROR") as cm2:
            st2.tick()
            self.clock.advance(100)  # passou do prazo
            st2.tick()
        self.assertTrue(any("até o prazo" in line for line in cm2.output), cm2.output)
        self.assertFalse(st2.snapshot()["scheduled"])

    # -- corridas cancelar/adiar × handoff
    def test_cancel_during_slow_handoff_waits_then_aborts(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def slow(action, seconds):
            calls.append((action, seconds))
            if action != "abort":
                entered.set()
                release.wait(5)
            return True

        st = self.state(slow)
        st.schedule(600, "shutdown")
        self.clock.advance(545)
        t_tick = threading.Thread(target=st.tick)
        t_tick.start()
        self.assertTrue(entered.wait(5), "o handoff não chegou ao runner")
        out = {}
        t_cancel = threading.Thread(target=lambda: out.setdefault("snap", st.cancel()))
        t_cancel.start()  # só pode começar depois do handoff: o _op está com o tick
        release.set()
        t_tick.join(5)
        t_cancel.join(5)
        self.assertEqual(calls, [("shutdown", 55), ("abort", 0)])  # cancelou o timer que acabou de ser entregue
        self.assertEqual((out["snap"]["scheduled"], out["snap"]["mode"]), (False, None))

    def _tick_with_state_changed_before_op(self, change):
        st = self.state()
        st.schedule(600, "shutdown")
        self.clock.advance(545)  # dentro do último minuto: o tick decide entregar
        real = st._op

        class Hook:  # outro thread muda o estado entre a decisão e a pegada do _op
            def __enter__(_self):
                change(st)
                return real.__enter__()

            def __exit__(_self, *exc):
                return real.__exit__(*exc)

        st._op = Hook()
        st.tick()
        return st

    def test_cancel_wins_race_no_handoff(self):
        st = self._tick_with_state_changed_before_op(lambda s: s._set(None, None, 0, False))
        self.assertEqual(self.run.calls, [])
        self.assertFalse(st.snapshot()["scheduled"])

    def test_postpone_wins_race_no_handoff(self):
        st = self._tick_with_state_changed_before_op(
            lambda s: s._set(self.clock.t + 1200, "shutdown", 1200, False))
        self.assertEqual(self.run.calls, [])
        snap = st.snapshot()
        self.assertEqual((snap["mode"], snap["total"]), ("local", 1200))

    # -- restauração
    def write_state(self, **over):
        data = {"at": self.clock.t + 600, "action": "shutdown", "total": 600, "handoff": False}
        data.update(over)
        self.store.write_text(json.dumps(data), encoding="utf-8")

    def test_restore_local_past_deadline_is_discarded(self):
        self.write_state(at=self.clock.t - 5)
        with self.assertLogs("shutlite", "WARNING") as cm:
            st = self.state()
        self.assertFalse(st.snapshot()["scheduled"])
        self.assertFalse(self.store.exists())
        self.assertEqual(self.run.calls, [])
        self.assertIn("nada será desligado", cm.output[0])

    def test_restore_local_in_future_rearms_and_hands_off(self):
        for native in (False, True):  # mesmo no modo clássico uma contagem local restaurada termina
            with self.subTest(native_timer=native):
                self.write_state()
                run = Scripted()
                st = self.state(run, native_timer=native)
                self.assertEqual(st.snapshot()["mode"], "local")
                self.clock.advance(545)
                st.tick()
                self.assertEqual(run.calls, [("shutdown", 55)])
                self.clock.advance(-545)

    def test_restore_legacy_state_without_handoff_field_is_windows_owned(self):
        self.write_state()
        data = self.saved()
        del data["handoff"]
        self.store.write_text(json.dumps(data), encoding="utf-8")
        st = self.state()
        self.assertEqual(st.snapshot()["mode"], "windows")
        self.clock.advance(590)
        st.tick()
        self.assertEqual(self.run.calls, [])  # o Windows já tem o timer: nada a entregar

    def test_restore_local_state_survives_reboot_but_windows_owned_does_not(self):
        # Regra nova: a checagem de boot só vale para handoff=true (o timer do Windows morre no reinício).
        self.write_state(boot=1000)  # local
        st = self.state(boot=lambda: 1100)
        snap = st.snapshot()
        self.assertEqual((snap["scheduled"], snap["mode"]), (True, "local"))
        self.assertTrue(self.store.exists())
        self.write_state(boot=1000, handoff=True)  # do Windows
        with self.assertLogs("shutlite", "WARNING") as cm:
            st2 = self.state(boot=lambda: 1100)
        self.assertFalse(st2.snapshot()["scheduled"])
        self.assertIn("reiniciado", cm.output[0])

    def test_restore_bad_handoff_type_is_discarded(self):
        self.write_state(handoff="sim")
        with self.assertLogs("shutlite", "WARNING"):
            self.assertFalse(self.state().snapshot()["scheduled"])

    # -- opção, thread e HTTP
    def test_windows_timer_option_cli_and_config(self):
        def load(raw, flag=False):
            (self.dir / "config.json").write_text(raw, encoding="utf-8")
            args = SimpleNamespace(data_dir=str(self.dir), port=None, pin=None, bind=None, allow_public=False,
                                   allow_host=None, dry_run=False, windows_timer=flag)
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("PORT", None)
                os.environ.pop("PIN", None)
                return server.Config.load(args).windows_timer

        self.assertFalse(load('{"pin": "123456"}'))
        self.assertTrue(load('{"pin": "123456"}', flag=True))
        self.assertTrue(load('{"pin": "123456", "windows_timer": true}'))
        self.assertFalse(load('{"pin": "123456", "windows_timer": "true"}'))  # só o JSON true liga
        self.assertFalse(load('{"pin": "123456", "windows_timer": false}'))
        self.assertTrue(server.parse_args(["--windows-timer"]).windows_timer)
        self.assertFalse(server.parse_args([]).windows_timer)

    def test_timer_thread_starts_with_server_and_stops_on_close(self):
        cfg = server.Config(port=0, pin="482913", data_dir=self.dir, bind="127.0.0.1")
        srv = server.build_server(cfg, Scripted())
        t = srv.state._thread
        self.assertTrue(t.is_alive() and t.daemon)
        srv.state.start()
        self.assertIs(srv.state._thread, t, "start() de novo não duplica a thread")
        srv.server_close()
        t.join(5)
        self.assertFalse(t.is_alive())
        srv.state.stop()  # idempotente

    def test_http_default_is_silent(self):
        cfg = server.Config(port=0, pin="482913", data_dir=self.dir, bind="127.0.0.1")
        run = Scripted()
        srv = server.build_server(cfg, run)
        th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        th.start()
        self.addCleanup(th.join, 5)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        self.addCleanup(_silence, srv.state)

        def post(path, body):
            c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
            c.request("POST", path, json.dumps({"pin": "482913", **body}).encode(),
                      {"Content-Type": "application/json"})
            r = c.getresponse()
            data = json.loads(r.read())
            c.close()
            return r.status, data

        code, snap = post("/api/shutdown", {"seconds": 600})
        self.assertEqual((code, snap["mode"], snap["scheduled"]), (200, "local", True))
        code, snap = post("/api/cancel", {})
        self.assertEqual((code, snap["mode"], snap["scheduled"]), (200, None, False))
        self.assertEqual(run.calls, [], "agendar e cancelar (longo) não chamam o Windows")
        code, snap = post("/api/restart", {"seconds": 30})
        self.assertEqual((code, snap["mode"]), (200, "windows"))
        self.assertEqual(run.calls, [("restart", 30)])


# ------------------------------------------------------------------ revisão do modo silencioso (rodada 3)

def serve_quiet(tc: unittest.TestCase, data_dir: Path, runner, bind: str = "127.0.0.1", **cfg_kw):
    """Servidor real com o timer parado: o teste é quem dirige o tempo. O teardown para o timer sem entregar."""
    cfg = server.Config(port=0, pin="482913", data_dir=data_dir, bind=bind, **cfg_kw)
    srv = server.build_server(cfg, runner)
    srv.state.stop()
    th = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    th.start()
    tc.addCleanup(th.join, 5)
    tc.addCleanup(srv.server_close)
    tc.addCleanup(srv.shutdown)
    tc.addCleanup(_silence, srv.state)  # registrado por último: roda primeiro, antes do server_close
    return srv


def http_json(port: int, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        data = None if body is None else json.dumps(body).encode()
        hdr = {"Content-Type": "application/json"} if method == "POST" else {}
        c.request(method, path, body=data, headers=hdr)
        r = c.getresponse()
        return r.status, json.loads(r.read())
    finally:
        c.close()


class _SilentBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.store = self.dir / "state.json"
        self.clock = use_clock(self, FakeClock(1_000_000.0))
        self.run = Scripted()

    def make(self, runner=None, **kw):
        kw.setdefault("boot", lambda: None)
        st = server.ShutdownState(runner or self.run, self.store, **kw)
        self.addCleanup(_silence, st)  # todo estado que pode ter thread é parado no fim do teste
        return st

    def saved(self) -> dict:
        return json.loads(self.store.read_text(encoding="utf-8"))

    def write_state(self, **over):
        data = {"at": self.clock.t + 600, "action": "shutdown", "total": 600, "handoff": False}
        data.update(over)
        self.store.write_text(json.dumps(data), encoding="utf-8")


# -- 1) round-trip do state.json

class SilentRoundTripTests(_SilentBase):
    def test_local_countdown_roundtrip_keeps_deadline_and_no_windows_call(self):
        st = self.make()
        st.schedule(600, "shutdown")
        self.assertIs(self.saved()["handoff"], False)
        self.clock.advance(100)
        snap = self.make().snapshot()  # "reinicia" o servidor
        self.assertEqual((snap["mode"], snap["left"], snap["total"]), ("local", 500, 600))
        self.assertEqual(self.run.calls, [])

    def test_handoff_true_roundtrip_is_windows_and_not_repeated(self):
        st = self.make()
        st.schedule(600, "shutdown")
        self.clock.advance(540)
        st.tick()
        self.assertEqual(self.run.calls, [("shutdown", 60)])
        self.assertIs(self.saved()["handoff"], True)
        again = self.make()
        self.assertEqual(again.snapshot()["mode"], "windows")
        self.clock.advance(20)
        again.tick()
        self.assertEqual(self.run.calls, [("shutdown", 60)], "não entrega de novo depois de reiniciar")

    def test_non_bool_handoff_is_discarded(self):
        for bad in (None, 1, 0, "true", [], {}):
            with self.subTest(handoff=bad):
                self.write_state(handoff=bad)
                with self.assertLogs("shutlite", "WARNING"):
                    st = self.make()
                self.assertFalse(st.snapshot()["scheduled"])
                self.assertFalse(self.store.exists())
        self.assertEqual(self.run.calls, [])

    def test_local_countdown_roundtrip_survives_reboot_windows_owned_is_discarded(self):
        # Regra nova: contagem local sobrevive a reinício (o boot não conta); entregue ao Windows, não.
        st = self.make(boot=lambda: 1000.0)
        st.schedule(600, "shutdown")
        self.assertEqual(self.saved()["boot"], 1000)
        again = self.make(boot=lambda: 1100.0)
        self.assertEqual(again.snapshot()["mode"], "local")
        self.assertEqual(self.run.calls, [])
        self.clock.advance(540)
        again.tick()  # o handoff regrava o boot do momento da entrega
        self.assertEqual(self.run.calls, [("shutdown", 60)])
        self.assertEqual(self.saved()["boot"], 1100)
        with self.assertLogs("shutlite", "WARNING") as cm:
            later = self.make(boot=lambda: 1300.0)  # o PC reiniciou depois da entrega
        self.assertFalse(later.snapshot()["scheduled"])
        self.assertIn("reiniciado", cm.output[0])

    def test_local_countdown_kept_within_boot_tolerance_and_when_boot_unknown(self):
        self.make(boot=lambda: 1000.0).schedule(600, "shutdown")
        self.assertEqual(self.make(boot=lambda: 1000.0 + server.BOOT_TOLERANCE).snapshot()["mode"], "local")
        self.assertEqual(self.make(boot=lambda: None).snapshot()["mode"], "local")


# -- 2) mode em /api/status e nos frames SSE

class SilentStatusFramesTests(_SilentBase):
    def sse_frame(self, resp) -> dict:
        while True:
            line = resp.readline()
            if not line:
                self.fail("o stream SSE fechou antes do esperado")
            if line.startswith(b"data: "):
                return json.loads(line[6:])

    def test_status_and_sse_frames_follow_the_schedule(self):
        srv = serve_quiet(self, self.dir, self.run)
        port = srv.server_address[1]
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/api/events")
        resp = conn.getresponse()
        frames = [self.sse_frame(resp)["mode"]]  # sincronização ao conectar
        self.assertIsNone(http_json(port, "GET", "/api/status")[1]["mode"])

        http_json(port, "POST", "/api/shutdown", {"pin": "482913", "seconds": 600})
        frames.append(self.sse_frame(resp)["mode"])
        self.assertEqual(http_json(port, "GET", "/api/status")[1]["mode"], "local")

        self.clock.advance(540)
        srv.state.tick()  # o timer da vida real faria isto no T-60
        frames.append(self.sse_frame(resp)["mode"])
        self.assertEqual(http_json(port, "GET", "/api/status")[1]["mode"], "windows")

        http_json(port, "POST", "/api/cancel", {"pin": "482913"})
        frames.append(self.sse_frame(resp)["mode"])
        self.assertEqual(frames, [None, "local", "windows", None])
        self.assertEqual(self.run.calls, [("shutdown", 60), ("abort", 0)])


# -- 3) a thread do timer

class TimerThreadTests(_SilentBase):
    def test_exception_in_tick_keeps_thread_alive_and_logs_once(self):
        st = self.make()
        calls, done = [], threading.Event()

        def boom():
            calls.append(1)
            if len(calls) >= 5:
                done.set()
            raise RuntimeError("falha simulada")

        st.tick = boom
        with mock.patch.object(server, "TICK_SECONDS", 0.01), \
                self.assertLogs("shutlite", "ERROR") as cm:
            st.start()
            self.assertTrue(done.wait(5), "o timer parou depois da exceção")
            self.assertTrue(st._thread.is_alive())
            st.stop()
        self.assertGreaterEqual(len(calls), 5)
        self.assertEqual(len(cm.records), 1, "o mesmo erro não pode ser logado a cada tick")
        self.assertIn("falha no timer", cm.records[0].getMessage())

    def test_start_twice_does_not_duplicate_and_stop_is_idempotent(self):
        st = self.make()
        st.start()
        t = st._thread
        st.start()
        self.assertIs(st._thread, t)
        st.stop()
        st.stop()
        self.assertFalse(t.is_alive())
        self.assertIsNone(st._thread)

    def test_stop_without_start_is_safe(self):
        st = self.make()
        st.stop()
        st.stop()

    def test_timer_thread_hands_off_by_itself(self):
        called, evt = [], threading.Event()

        def runner(action, seconds):
            called.append((action, seconds))
            evt.set()
            return True

        st = self.make(runner)
        st.schedule(600, "shutdown")
        self.clock.advance(540)
        with mock.patch.object(server, "TICK_SECONDS", 0.01):
            st.start()
            self.assertTrue(evt.wait(5), "a thread não entregou ao Windows no T-60")
            st.stop()
        self.assertEqual(called, [("shutdown", 60)])


# -- 4) salto do relógio

class ClockJumpTests(_SilentBase):
    def test_wall_clock_back_is_a_discontinuity_and_discards(self):
        # Regra nova (dois relógios): a parede voltou 300 s, o monotônico não → discordam → descarta.
        # (Antes: a parede voltando só empurrava o prazo e a entrega acontecia depois.)
        st = self.make()
        st.schedule(600, "shutdown")
        self.clock.jump(-300)
        self.assertEqual(st.snapshot()["left"], 900)  # o painel segue a parede
        with self.assertLogs("shutlite", "WARNING") as cm:
            st.tick()
        self.assertIn("relógio do sistema mudou ou o PC foi suspenso", cm.output[0])
        self.assertIn("nada será desligado", cm.output[0])
        self.assertEqual(self.run.calls, [])
        self.assertFalse(st.snapshot()["scheduled"])
        self.assertFalse(self.store.exists())

    def test_clock_forward_past_deadline_discards_without_delivery(self):
        st = self.make()
        st.schedule(600, "shutdown")
        self.clock.advance(601)
        with self.assertLogs("shutlite", "WARNING") as cm:
            st.tick()
        self.assertEqual(self.run.calls, [])
        self.assertFalse(st.snapshot()["scheduled"])
        self.assertIn("nada será desligado", cm.output[0])

    def test_clock_forward_inside_last_minute_hands_off_only_remaining(self):
        st = self.make()
        st.schedule(600, "shutdown")
        self.clock.advance(590)
        st.tick()
        self.assertEqual(self.run.calls, [("shutdown", 10)])

    def test_clock_back_after_handoff_does_not_rehand_off(self):
        st = self.make()
        st.schedule(600, "shutdown")
        self.clock.advance(540)
        st.tick()
        self.clock.jump(-1000)  # parede para trás depois do handoff: o Windows é o dono, nada a fazer
        st.tick()
        self.assertEqual(self.run.calls, [("shutdown", 60)])
        self.assertEqual(st.snapshot()["mode"], "windows")

    def test_handoff_is_never_early_with_fractional_seconds(self):
        st = self.make()
        st.schedule(600, "shutdown")
        self.clock.advance(539.2)  # faltam 60,8 s: ainda não entrega
        st.tick()
        self.assertEqual(self.run.calls, [])
        self.clock.advance(0.9)  # faltam 59,9 s
        st.tick()
        ((action, secs),) = self.run.calls
        self.assertGreaterEqual(self.clock.t + secs, 1_000_000 + 600,
                                "o Windows não pode disparar antes do prazo")


# -- 5) cancelar/adiar no instante do handoff e durante o runner lento

class HandoffRaceTests(_SilentBase):
    def test_cancel_exactly_at_handoff_instant_leaves_no_ghost(self):
        st = self.make()
        st.schedule(600, "shutdown")
        self.clock.advance(540)  # faltam 60 s: exatamente o instante do handoff
        self.assertFalse(st.cancel()["scheduled"])
        st.tick()
        # B3: cancelar contagem local com < 90 s restando manda também um abort de segurança (e nada mais)
        self.assertEqual(self.run.calls, [("abort", 0)])
        self.assertFalse(self.store.exists())
        self.assertFalse(self.make().snapshot()["scheduled"], "nada ressuscita do disco")

    def test_postpone_exactly_at_handoff_instant_stays_local(self):
        st = self.make()
        st.schedule(600, "shutdown")
        self.clock.advance(540)
        snap = st.schedule(1200, "restart")
        st.tick()
        self.assertEqual(self.run.calls, [])
        self.assertEqual((snap["mode"], snap["total"], snap["action"]), ("local", 1200, "restart"))

    def test_postpone_during_slow_handoff_aborts_then_stays_local(self):
        entered, release = threading.Event(), threading.Event()

        def slow(action, seconds):
            self.run.calls.append((action, seconds))
            if action != "abort":
                entered.set()
                release.wait(5)
            return True

        st = self.make(slow)
        st.schedule(600, "shutdown")
        self.clock.advance(545)
        t_tick = threading.Thread(target=st.tick)
        t_tick.start()
        self.assertTrue(entered.wait(5), "o handoff não chegou ao runner")
        out = {}
        t_post = threading.Thread(target=lambda: out.setdefault("snap", st.schedule(1200, "restart")))
        t_post.start()  # precisa esperar o handoff terminar (_op)
        release.set()
        t_tick.join(5)
        t_post.join(5)
        self.assertEqual(self.run.calls, [("shutdown", 55), ("abort", 0)])
        self.assertEqual((out["snap"]["mode"], out["snap"]["total"]), ("local", 1200))
        self.assertIs(self.saved()["handoff"], False)


# -- 6) agendamento ≤ 60 s com contagem anterior e runner falhando

class ShortScheduleFailureTests(_SilentBase):
    def test_abort_confirms_clean_keeps_previous_local_countdown(self):
        st = self.make()
        st.schedule(600, "shutdown")  # só local: nenhuma chamada ao Windows
        self.run.results = [False, True, False, True]  # entrega falha, abort ok, entrega falha, abort ok
        with self.assertRaises(server.ShutdownError) as cm:
            st.schedule(30, "restart")
        self.assertIn("o agendamento anterior continua valendo", str(cm.exception))
        self.assertEqual(self.run.calls, [("restart", 30), ("abort", 0), ("restart", 30), ("abort", 0)])
        snap = st.snapshot()
        self.assertEqual((snap["mode"], snap["total"], snap["action"]), ("local", 600, "shutdown"))
        self.assertIs(self.saved()["handoff"], False)

    def test_abort_unconfirmed_drops_previous_countdown(self):
        # A contagem local anterior some; a mensagem agora diz isso (antes: "recusou… não consegui confirmar").
        st = self.make()
        st.schedule(600, "shutdown")
        self.run.default = False
        with self.assertRaises(server.ShutdownError) as cm:
            st.schedule(30, "restart")
        self.assertEqual(str(cm.exception), "Não consegui agendar agora e o agendamento anterior foi cancelado. "
                                            "Use Cancelar tudo para garantir.")
        self.assertFalse(st.snapshot()["scheduled"])
        self.assertFalse(self.store.exists())

    def test_short_schedule_refused_with_nothing_before_gives_plain_message(self):
        st = self.make()
        self.run.results = [False, True, False, True]
        with self.assertRaises(server.ShutdownError) as cm:
            st.schedule(30, "shutdown")
        self.assertEqual(str(cm.exception), "O Windows recusou o comando. Tente de novo.")
        self.assertEqual(self.run.calls, [("shutdown", 30), ("abort", 0), ("shutdown", 30), ("abort", 0)])
        self.assertFalse(st.snapshot()["scheduled"])

    def test_short_schedule_failing_restores_windows_owned_previous(self):
        st = self.make()
        st.schedule(30, "shutdown")  # ≤ 60: entregue na hora (Windows é o dono)
        self.run.calls.clear()
        self.run.results = [True, False, True, True]  # abort, entrega 20 s falha, abort, restaura 30 s
        with self.assertRaises(server.ShutdownError) as cm:
            st.schedule(20, "restart")
        self.assertIn("o anterior foi mantido", str(cm.exception))
        self.assertEqual(self.run.calls, [("abort", 0), ("restart", 20), ("abort", 0), ("shutdown", 30)])
        snap = st.snapshot()
        self.assertEqual((snap["mode"], snap["action"], snap["total"]), ("windows", "shutdown", 30))


# -- 7) regressão: --windows-timer segue o fluxo antigo

class ClassicRegressionTests(_SilentBase):
    def classic(self):
        return self.make(native_timer=True)

    def test_classic_schedule_is_immediate_and_windows_owned(self):
        st = self.classic()
        snap = st.schedule(600, "shutdown")
        self.assertEqual(self.run.calls, [("shutdown", 600)])
        self.assertEqual(snap["mode"], "windows")
        self.assertIs(self.saved()["handoff"], True)

    def test_classic_short_schedule_is_immediate_too(self):
        st = self.classic()
        st.schedule(5, "restart")
        self.assertEqual(self.run.calls, [("restart", 5)])

    def test_classic_postpone_aborts_then_reschedules(self):
        st = self.classic()
        st.schedule(600, "shutdown")
        snap = st.schedule(1200, "restart")
        self.assertEqual(self.run.calls, [("shutdown", 600), ("abort", 0), ("restart", 1200)])
        self.assertEqual((snap["mode"], snap["total"]), ("windows", 1200))

    def test_classic_cancel_and_cancel_failure_keep_state(self):
        st = self.classic()
        st.schedule(600, "shutdown")
        self.run.default = False
        with self.assertRaises(server.ShutdownError) as cm:
            st.cancel()
        self.assertEqual(str(cm.exception), "Não foi possível cancelar. Tente de novo.")
        self.assertEqual(st.snapshot()["mode"], "windows")
        self.run.default = True
        self.assertFalse(st.cancel()["scheduled"])

    def test_classic_refusal_message_and_cleared_state(self):
        st = self.classic()
        self.run.results = [False, True, False, True]
        with self.assertRaises(server.ShutdownError) as cm:
            st.schedule(600, "shutdown")
        self.assertEqual(str(cm.exception), "O Windows recusou o comando. Tente de novo.")
        self.assertFalse(st.snapshot()["scheduled"])

    def test_classic_restored_windows_timer_expires_without_runner_call(self):
        self.write_state(handoff=True)
        st = self.classic()
        self.assertEqual(st.snapshot()["mode"], "windows")
        self.clock.advance(700)
        st.tick()
        self.assertFalse(st.snapshot()["scheduled"])
        self.assertEqual(self.run.calls, [])

    def test_http_classic_via_config_is_immediate(self):
        srv = serve_quiet(self, self.dir, self.run, windows_timer=True)
        port = srv.server_address[1]
        self.assertTrue(srv.state._native)
        code, snap = http_json(port, "POST", "/api/restart", {"pin": "482913", "seconds": 30})
        self.assertEqual((code, snap["mode"]), (200, "windows"))
        self.assertEqual(self.run.calls, [("restart", 30)])


# -- 8) autostart: o .cmd não repassa opções; o README explica como mudar

@unittest.skipUnless(os.name == "nt", "o atalho de inicialização usa caminhos do Windows")
class AutostartTests(_SilentBase):
    def setUp(self):
        super().setUp()
        self.appdata = self.dir / "appdata"
        self.startup = self.appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        self.startup.mkdir(parents=True)
        self.link = self.startup / "shutlite.cmd"

    def set_autostart(self, enable: bool) -> int:
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"APPDATA": str(self.appdata)}), \
                mock.patch.object(server.os, "name", "nt"), contextlib.redirect_stdout(buf):
            return server.set_autostart(enable)

    def test_autostart_cmd_does_not_carry_timer_flag(self):
        self.assertEqual(self.set_autostart(True), 0)
        text = self.link.read_text(encoding="utf-8")
        self.assertIn("server.py", text)
        self.assertNotIn("--windows-timer", text)
        self.assertNotIn("windows_timer", text)

    def test_config_windows_timer_still_applies_to_autostarted_server(self):
        (self.dir / "config.json").write_text('{"pin": "123456", "windows_timer": true}', encoding="utf-8")
        self.assertEqual(self.set_autostart(True), 0)
        self.assertNotIn("--windows-timer", self.link.read_text(encoding="utf-8"))
        args = SimpleNamespace(data_dir=str(self.dir), port=None, pin=None, bind=None, allow_public=False,
                               allow_host=None, dry_run=False, windows_timer=False)
        self.assertTrue(server.Config.load(args).windows_timer)

    def test_autostart_off_removes_link(self):
        self.set_autostart(True)
        self.assertTrue(self.link.exists())
        self.assertEqual(self.set_autostart(False), 0)
        self.assertFalse(self.link.exists())

    def test_readme_says_how_to_keep_the_timer_option(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn('"windows_timer": true', readme)
        self.assertIn("--windows-timer", readme)
        self.assertRegex(readme, r"--autostart on`?, que não\s+repassa opções")


# ------------------------------------------------------------------ parte A: dois relógios, arquivos grandes

class ClockDiscontinuityTests(_SilentBase):
    """Parede e monotônico independentes: FakeClock.advance() move os dois, jump() só a parede."""

    JUMP_MSG = "relógio do sistema mudou ou o PC foi suspenso; agendamento descartado, nada será desligado"

    def armed(self, secs=1800):
        seen = []
        st = self.make(on_change=seen.append)
        st.schedule(secs, "shutdown")
        return st, seen

    def test_wall_jumps_discard_without_any_runner_call(self):
        # +1750 s: antes entregava ("shutdown", 50) com 25 min de antecedência; +1900 s: prazo "vencido";
        # −3600 s: nunca entregaria. Agora os três descartam, com aviso, sem falar com o Windows.
        for jump in (1750, 1900, -3600):
            with self.subTest(jump=jump):
                self.store.unlink(missing_ok=True)
                st, seen = self.armed(1800)
                self.clock.jump(jump)
                with self.assertLogs("shutlite", "WARNING") as cm:
                    st.tick()
                self.assertIn(self.JUMP_MSG, cm.output[0])
                self.assertEqual(self.run.calls, [])
                self.assertFalse(st.snapshot()["scheduled"])
                self.assertFalse(self.store.exists())
                self.assertEqual([s["mode"] for s in seen], [None])  # on_change: o painel vê ocioso
                self.clock.jump(-jump)
                st.tick()  # ocioso: nada acontece
                self.assertEqual(self.run.calls, [])

    def test_no_jump_delivers_normally(self):
        st, seen = self.armed(1800)
        self.clock.advance(1739)  # faltam 61 s nos dois relógios
        st.tick()
        self.assertEqual(self.run.calls, [])
        self.clock.advance(1)
        st.tick()
        self.assertEqual(self.run.calls, [("shutdown", 60)])
        self.assertEqual([s["mode"] for s in seen], ["windows"])

    def test_tolerance_is_five_seconds_inclusive(self):
        self.assertEqual(server.CLOCK_JUMP_TOLERANCE, 5.0)
        st, _ = self.armed(600)
        self.clock.advance(100)
        self.clock.jump(5.0)  # diferença de exatos 5 s: não é salto
        st.tick()
        self.assertTrue(st.snapshot()["scheduled"])
        self.clock.jump(0.5)  # 5,5 s: salto
        with self.assertLogs("shutlite", "WARNING") as cm:
            st.tick()
        self.assertIn(self.JUMP_MSG, cm.output[0])
        self.assertFalse(st.snapshot()["scheduled"])

    def test_handoff_is_decided_and_sized_by_the_monotonic_left(self):
        st, _ = self.armed(600)
        self.clock.advance(538)  # restam 62 s nos dois
        self.clock.jump(3)  # parede: 59 s; monotônico: 62 s; diferença 3 s (dentro da tolerância)
        st.tick()
        self.assertEqual(self.run.calls, [], "quem decide é o monotônico (62 > 60), não a parede")
        self.clock.advance(2)  # monotônico: 60 s (parede: 57 s)
        st.tick()
        self.assertEqual(self.run.calls, [("shutdown", 60)], "o /t usa o restante monotônico")

    def test_expired_on_either_clock_discards_without_delivery(self):
        st, _ = self.armed(600)
        self.clock.advance(596)  # restam 4 s
        self.clock.jump(4.5)  # parede: −0,5 s; monotônico: 4 s; diferença 4,5 s (sem salto)
        with self.assertLogs("shutlite", "WARNING") as cm:
            st.tick()
        self.assertEqual(self.run.calls, [])
        self.assertIn("nada será desligado", cm.output[0])
        self.assertNotIn("relógio do sistema mudou", cm.output[0])
        self.assertFalse(st.snapshot()["scheduled"])
        st2, _ = self.armed(600)  # agora vence só o monotônico
        self.clock.advance(599)
        self.clock.m += 1.5  # monotônico: −0,5 s; parede: +1 s; diferença 1,5 s
        with self.assertLogs("shutlite", "WARNING") as cm2:
            st2.tick()
        self.assertEqual(self.run.calls, [])
        self.assertIn("nada será desligado", cm2.output[0])
        self.assertFalse(st2.snapshot()["scheduled"])

    def test_snapshot_compares_in_float_and_rounds_up_for_display(self):
        st, _ = self.armed(600)
        self.clock.advance(599.5)
        snap = st.snapshot()  # int() trataria 0,5 s como "expirou" e descartaria cedo
        self.assertEqual((snap["scheduled"], snap["left"]), (True, 1))
        self.clock.advance(0.5)
        with self.assertLogs("shutlite", "WARNING"):
            self.assertFalse(st.snapshot()["scheduled"])

    def test_restore_arms_the_monotonic_deadline_from_now(self):
        self.write_state(at=self.clock.t + 100)
        st = self.make()
        self.clock.advance(100)
        with self.assertLogs("shutlite", "WARNING") as cm:
            st.tick()
        self.assertIn("nada será desligado", cm.output[0])
        self.write_state(at=self.clock.t + 1800)
        st2 = self.make()
        self.clock.jump(1750)
        with self.assertLogs("shutlite", "WARNING") as cm2:
            st2.tick()
        self.assertIn(self.JUMP_MSG, cm2.output[0])
        self.assertEqual(self.run.calls, [])


# ------------------------------------------------------------------ parte B: parada ordenada, cancelar na frente

class OrderlyStopTests(_SilentBase):
    """B1: parar o shutlite com contagem LOCAL pendente entrega o que falta ao Windows."""

    def hhmm(self, at):
        return time.strftime("%H:%M", time.localtime(at))

    def test_stop_with_local_countdown_hands_off_remaining(self):
        st = self.make()
        st.schedule(600, "restart")
        at = st._at
        self.clock.advance(100)  # restam 500 s
        with self.assertLogs("shutlite", "INFO") as cm:
            info = st.stop()
        self.assertEqual(self.run.calls, [("restart", 500)])
        self.assertEqual(info, {"ok": True, "action": "restart", "at": self.hhmm(at)})
        self.assertIn(f"Há um desligamento às {self.hhmm(at)}; entreguei ao Windows "
                      "(o aviso dele vai aparecer).", "\n".join(cm.output))
        self.assertIs(self.saved()["handoff"], True)
        snap = st.snapshot()
        self.assertEqual((snap["scheduled"], snap["mode"]), (True, "windows"))
        self.assertEqual(st.stop(), None)  # idempotente: não entrega de novo
        self.assertEqual(self.run.calls, [("restart", 500)])

    def test_stop_idle_or_already_windows_owned_makes_no_calls(self):
        st = self.make()
        self.assertIsNone(st.stop())
        st2 = self.make()
        st2.schedule(30, "shutdown")  # ≤ 60: já é do Windows
        self.run.calls.clear()
        self.assertIsNone(st2.stop())
        self.assertEqual(self.run.calls, [])
        self.assertIsNone(self.make().stop(deliver=False))

    def test_stop_deliver_false_only_stops_the_thread(self):
        st = self.make()
        st.schedule(600, "shutdown")
        st.start()
        self.assertIsNone(st.stop(deliver=False))
        self.assertEqual(self.run.calls, [])
        self.assertIs(self.saved()["handoff"], False)
        self.assertIsNone(st._thread)

    def test_stop_runner_failure_logs_error_returns_and_keeps_local_state(self):
        run = Scripted(default=False)
        st = self.make(run)
        st.schedule(600, "shutdown")
        with self.assertLogs("shutlite", "ERROR") as cm:
            info = st.stop()  # não levanta, não trava, uma tentativa só (sem abort/retry)
        self.assertEqual(run.calls, [("shutdown", 600)])
        self.assertEqual((info["ok"], info["reason"]), (False, "runner"))
        self.assertIn("NÃO consegui entregá-lo ao Windows", cm.output[0])
        self.assertIs(self.saved()["handoff"], False)  # segue local: se o shutlite voltar, retoma

    def test_stop_after_clock_jump_does_not_deliver(self):
        st = self.make()
        st.schedule(1800, "shutdown")
        self.clock.jump(1750)
        with self.assertLogs("shutlite", "ERROR") as cm:
            info = st.stop()
        self.assertEqual(self.run.calls, [])
        self.assertEqual((info["ok"], info["reason"]), (False, "clock"))
        self.assertIn("relógio do sistema mudou", cm.output[0])

    def test_stop_expired_countdown_delivers_nothing(self):
        st = self.make()
        st.schedule(600, "shutdown")
        self.clock.advance(601)
        self.assertIsNone(st.stop())
        self.assertEqual(self.run.calls, [])

    def test_stop_waits_at_most_stop_wait_for_a_busy_op(self):
        st = self.make()
        st.schedule(600, "shutdown")
        with mock.patch.object(server, "STOP_WAIT_SECONDS", 0.05), self.assertLogs("shutlite", "ERROR") as cm:
            st._op.acquire()  # um comando em andamento que não termina
            try:
                info = st.stop()
            finally:
                st._op.release()
        self.assertEqual((info["ok"], info["reason"]), (False, "busy"))
        self.assertRegex(info["at"], r"^\d\d:\d\d$")
        self.assertEqual(self.run.calls, [])
        self.assertIn("PODE acontecer", cm.output[0])
        self.assertNotIn("NÃO vai acontecer", cm.output[0])  # quem segura o _op pode estar entregando

    def test_server_close_hands_off_pending_local_countdown(self):
        run = Scripted()
        srv = serve_quiet(self, self.dir, run)
        http_json(srv.server_address[1], "POST", "/api/shutdown", {"pin": "482913", "seconds": 600})
        self.assertEqual(run.calls, [])
        srv.shutdown()  # para o serve_forever antes de fechar o socket (senão ele levanta OSError na thread)
        srv.server_close()  # também é o que o Ctrl+C faz (main: finally: server_close)
        self.assertEqual(run.calls, [("shutdown", 600)])
        self.assertIs(self.saved()["handoff"], True)

    def test_api_quit_hands_off_and_stop_command_reports_it(self):
        run = Scripted()
        srv = serve_quiet(self, self.dir, run)
        port = srv.server_address[1]
        http_json(port, "POST", "/api/restart", {"pin": "482913", "seconds": 600})
        cfg = server.Config(port=port, pin="482913", data_dir=self.dir, bind="127.0.0.1")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = server.stop_running(cfg)
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("shutlite parado.", out)
        self.assertRegex(out, r"Há um desligamento às \d\d:\d\d; entreguei ao Windows "
                              r"\(o aviso dele vai aparecer\)\.")
        self.assertEqual(run.calls, [("restart", 600)])

    def test_stop_command_warns_when_handoff_fails(self):
        run = Scripted(default=False)
        srv = serve_quiet(self, self.dir, run)
        port = srv.server_address[1]
        http_json(port, "POST", "/api/shutdown", {"pin": "482913", "seconds": 600})
        cfg = server.Config(port=port, pin="482913", data_dir=self.dir, bind="127.0.0.1")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertLogs("shutlite", "ERROR"):
            code = server.stop_running(cfg)
        self.assertEqual(code, 0)
        self.assertIn("ATENÇÃO", buf.getvalue())
        self.assertIn("NÃO consegui entregá-lo ao Windows", buf.getvalue())

    def test_stop_command_idle_prints_only_stopped(self):
        srv = serve_quiet(self, self.dir, self.run)
        cfg = server.Config(port=srv.server_address[1], pin="482913", data_dir=self.dir, bind="127.0.0.1")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(server.stop_running(cfg), 0)
        self.assertEqual(buf.getvalue().strip(), "shutlite parado.")
        self.assertEqual(self.run.calls, [])


class UserActionsGoFirstTests(_SilentBase):
    """B2: cancelar/adiar não ficam atrás da cadeia abort + nova tentativa do tick."""

    def wait_for_waiter(self, st):
        for _ in range(5000):  # no máximo ~5 s (o relógio falso não anda, então conta iterações)
            if st._waiting:
                return
            threading.Event().wait(0.001)
        self.fail("a ação do usuário não chegou a esperar o _op")

    def slow_failing_handoff(self, then_abort_ok=True):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def runner(action, seconds):
            calls.append((action, seconds))
            if action != "abort" and len(calls) == 1:
                entered.set()
                release.wait(5)
                return False  # a entrega lenta falha
            return then_abort_ok

        return runner, calls, entered, release

    def test_cancel_passes_ahead_of_the_tick_retry_chain(self):
        runner, calls, entered, release = self.slow_failing_handoff()
        st = self.make(runner)
        st.schedule(600, "shutdown")
        self.clock.advance(545)  # restam 55 s
        t_tick = threading.Thread(target=st.tick)
        t_tick.start()
        self.assertTrue(entered.wait(5))
        out = {}
        t_cancel = threading.Thread(target=lambda: out.setdefault("snap", st.cancel()))
        t_cancel.start()
        self.wait_for_waiter(st)
        release.set()
        t_tick.join(5)
        t_cancel.join(5)
        # sem a flag: [("shutdown",55), ("abort",0), ("shutdown",55)] do tick + o abort do cancelar.
        # Com ela o tick só faz a 1ª chamada e solta o _op; o cancelar manda só o abort de segurança (B3).
        self.assertEqual(calls, [("shutdown", 55), ("abort", 0)])
        self.assertEqual((out["snap"]["scheduled"], out["snap"]["mode"]), (False, None))
        self.assertEqual(st._waiting, 0)

    def test_postpone_passes_ahead_of_the_tick_retry_chain(self):
        runner, calls, entered, release = self.slow_failing_handoff()
        st = self.make(runner)
        st.schedule(600, "shutdown")
        self.clock.advance(545)
        t_tick = threading.Thread(target=st.tick)
        t_tick.start()
        self.assertTrue(entered.wait(5))
        out = {}
        t_post = threading.Thread(target=lambda: out.setdefault("snap", st.schedule(1200, "restart")))
        t_post.start()
        self.wait_for_waiter(st)
        release.set()
        t_tick.join(5)
        t_post.join(5)
        self.assertEqual(calls, [("shutdown", 55)])  # adiar local: zero chamadas além da entrega que falhou
        self.assertEqual((out["snap"]["mode"], out["snap"]["total"]), ("local", 1200))
        self.assertEqual(st._waiting, 0)

    def test_without_a_waiting_user_the_tick_still_retries_with_abort(self):
        runner, calls, entered, release = self.slow_failing_handoff()
        release.set()
        st = self.make(runner)
        st.schedule(600, "shutdown")
        self.clock.advance(545)
        st.tick()
        self.assertEqual(calls, [("shutdown", 55), ("abort", 0), ("shutdown", 55)])  # fluxo de antes
        self.assertEqual(st.snapshot()["mode"], "windows")

    def test_waiting_counter_is_released_even_when_the_action_fails(self):
        run = Scripted(default=False)
        st = self.make(run)
        with self.assertRaises(server.ShutdownError):
            st.schedule(30, "shutdown")
        with self.assertRaises(server.ShutdownError):
            st.cancel()  # ocioso: abort falha
        self.assertEqual(st._waiting, 0)
        self.assertTrue(st._op.acquire(timeout=1))  # e o _op não ficou preso
        st._op.release()

    def test_a_single_windows_call_is_bounded_by_six_seconds(self):
        # prova no subprocess.run (não só na constante): o teto de uma chamada ao shutdown.exe é 6 s
        fake = SimpleNamespace(returncode=0, stdout="", stderr="")
        with mock.patch.object(server.subprocess, "run", return_value=fake) as run:
            server.run_shutdown("shutdown", 600)
        self.assertEqual(run.call_args[1]["timeout"], 6)
        self.assertEqual(server.SHUTDOWN_TIMEOUT, 6)


class CancelSafetyAbortTests(_SilentBase):
    """B3: cancelar contagem local com < 90 s restando manda abort também; falha dele é ignorada."""

    def test_below_90_seconds_sends_abort(self):
        st = self.make()
        st.schedule(600, "shutdown")
        self.clock.advance(600 - 89)  # restam 89 s
        snap = st.cancel()
        self.assertEqual(self.run.calls, [("abort", 0)])
        self.assertFalse(snap["scheduled"])
        self.assertFalse(self.store.exists())

    def test_at_90_seconds_or_more_sends_nothing(self):
        for left in (90, 91, 600):
            with self.subTest(left=left):
                self.run.calls.clear()
                st = self.make()
                st.schedule(600, "shutdown")
                self.clock.advance(600 - left)
                st.cancel()
                self.assertEqual(self.run.calls, [])
                self.clock.advance(-(600 - left))

    def test_abort_failure_is_ignored(self):
        run = Scripted(default=False)
        st = self.make(run)
        st.schedule(600, "shutdown")
        self.clock.advance(560)  # restam 40 s
        snap = st.cancel()  # o abort falha, mas não é erro
        self.assertEqual(run.calls, [("abort", 0)])
        self.assertFalse(snap["scheduled"])
        self.assertFalse(self.store.exists())

    def test_cancel_of_windows_owned_and_idle_still_use_abort_and_can_fail(self):
        st = self.make()
        st.schedule(30, "shutdown")  # do Windows
        self.run.calls.clear()
        self.run.default = False
        with self.assertRaises(server.ShutdownError):
            st.cancel()
        self.assertEqual(self.run.calls, [("abort", 0)])
        self.assertTrue(st.snapshot()["scheduled"])  # abort falhou: estado mantido, como antes

    def test_remaining_uses_the_smaller_of_both_clocks(self):
        st = self.make()
        st.schedule(1800, "shutdown")
        self.clock.advance(1000)  # restam 800 nos dois
        self.clock.m += 750  # o monotônico diz 50 s (parede 800, diferença > tolerância): menor vale
        st.cancel()
        self.assertEqual(self.run.calls, [("abort", 0)])


class JsonSizeLimitTests(_SilentBase):
    def test_state_over_64kb_is_unreadable_and_removed(self):
        self.write_state(pad="x" * 1_000_000)  # ~1 MB
        self.assertGreater(self.store.stat().st_size, 1_000_000)
        with self.assertLogs("shutlite", "WARNING") as cm:
            st = self.make()
        self.assertFalse(st.snapshot()["scheduled"])
        self.assertFalse(self.store.exists())
        self.assertTrue(any("64 KB" in line and "ilegível" in line for line in cm.output), cm.output)
        self.assertEqual(self.run.calls, [])

    def test_boundary_exactly_64kb_loads_one_more_byte_does_not(self):
        p = self.dir / "t.json"
        empty = len('{"pad": ""}')
        raw = ('{"pad": "%s"}' % ("x" * (server.MAX_JSON_FILE - empty))).encode()
        self.assertEqual(len(raw), server.MAX_JSON_FILE)
        p.write_bytes(raw)
        self.assertIn("pad", server._read_json(p))
        p.write_bytes(raw[:-1] + b" }")  # 64 KB + 1 byte
        with self.assertLogs("shutlite", "WARNING"), \
                mock.patch.object(server.json, "loads", side_effect=AssertionError("não devia parsear")):
            self.assertEqual(server._read_json(p), {})

    def test_huge_file_is_not_read_whole(self):
        p = self.dir / "huge.json"
        with open(p, "wb") as f:
            f.seek(20 * 1024 * 1024 - 1)  # 20 MB (esparso quando o sistema permite)
            f.write(b"x")
        with self.assertLogs("shutlite", "WARNING"):
            self.assertEqual(server._read_json(p), {})  # lê só 64 KB + 1, sem MemoryError

    def test_config_over_64kb_is_ignored(self):
        (self.dir / "config.json").write_text(
            json.dumps({"port": 4444, "pin": "654321", "pad": "x" * 1_000_000}), encoding="utf-8")
        args = SimpleNamespace(data_dir=str(self.dir), port=None, pin=None, bind=None, allow_public=False,
                               allow_host=None, dry_run=False, windows_timer=False)
        with mock.patch.dict(os.environ, {}, clear=False), self.assertLogs("shutlite", "WARNING") as cm:
            os.environ.pop("PORT", None)
            os.environ.pop("PIN", None)
            cfg = server.Config.load(args)
        self.assertEqual(cfg.port, server.DEFAULT_PORT)  # o 4444 do arquivo grande não vale
        self.assertNotEqual(cfg.pin, "654321")
        self.assertTrue(any("config.json" in line for line in cm.output), cm.output)


# ------------------------------------------------------------------ revalidação final (rodada 4)

class StopCoverageTests(_SilentBase):
    """Cobertura que faltava na parada ordenada: cancelar durante a parada, quit sem PIN, quit de fora,
    paradas concorrentes e o aviso de ocupado."""

    def slow_handoff_runner(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def runner(action, seconds):
            calls.append((action, seconds))
            if action != "abort":
                entered.set()
                release.wait(5)
            return True

        return runner, calls, entered, release

    def test_cancel_during_orderly_stop_waits_then_aborts(self):  # (i)
        runner, calls, entered, release = self.slow_handoff_runner()
        st = self.make(runner)
        st.schedule(600, "restart")
        self.clock.advance(100)  # restam 500 s
        cancel_waiting = threading.Event()
        st._op = _SignalOnAcquire(st._op, cancel_waiting, "probe-cancel")
        out = {}
        t_stop = threading.Thread(target=lambda: out.setdefault("stop", st.stop()), name="probe-stop")
        t_stop.start()
        self.assertTrue(entered.wait(5), "a parada não chegou à entrega")
        t_cancel = threading.Thread(target=lambda: out.setdefault("cancel", st.cancel()), name="probe-cancel")
        t_cancel.start()
        self.assertTrue(cancel_waiting.wait(5), "o cancelar não chegou a esperar o _op")
        release.set()
        t_stop.join(5)
        t_cancel.join(5)
        self.assertEqual(calls, [("restart", 500), ("abort", 0)], "entregou e, depois, cancelou com abort")
        self.assertTrue(out["stop"]["ok"])
        self.assertFalse(out["cancel"]["scheduled"])
        self.assertFalse(self.store.exists(), "nada ressuscita do disco")

    def test_api_quit_without_or_with_wrong_pin_delivers_nothing(self):  # (ii)
        run = Scripted()
        srv = serve_quiet(self, self.dir, run)
        port = srv.server_address[1]
        http_json(port, "POST", "/api/shutdown", {"pin": "482913", "seconds": 600})
        for body in ({}, {"pin": "000000"}):
            with self.subTest(body=body):
                code, _ = http_json(port, "POST", "/api/quit", body)
                self.assertEqual(code, 401)
        self.assertEqual(run.calls, [], "quit sem PIN certo não pode entregar nada")
        self.assertIs(self.saved()["handoff"], False)
        code, snap = http_json(port, "GET", "/api/status")
        self.assertEqual((code, snap["mode"]), (200, "local"), "o servidor continua de pé")

    def test_api_quit_from_other_address_is_403_and_delivers_nothing(self):  # (iii)
        class LanHandler(server.Handler):  # simula um cliente da rede: a conexão real é loopback
            def setup(self):
                super().setup()
                self.client_address = ("192.168.1.9", self.client_address[1])

        run = Scripted()
        with mock.patch.object(server, "Handler", LanHandler):
            srv = serve_quiet(self, self.dir, run)
        port = srv.server_address[1]
        self.assertEqual(http_json(port, "POST", "/api/shutdown", {"pin": "482913", "seconds": 600})[0], 200)
        code, body = http_json(port, "POST", "/api/quit", {"pin": "482913"})  # PIN certo, mas não é o PC
        self.assertEqual(code, 403)
        self.assertIn("Só o próprio PC", body["error"])
        self.assertEqual(run.calls, [], "quit de fora não entrega nada")
        self.assertIs(self.saved()["handoff"], False)
        self.assertEqual(http_json(port, "GET", "/api/status")[1]["mode"], "local")

    def test_second_stop_that_was_waiting_does_not_deliver_again(self):  # (iv) + mata a mutação c
        entered, release = threading.Event(), threading.Event()
        calls = []

        def runner(action, seconds):
            calls.append((action, seconds))
            if action != "abort":
                entered.set()
                release.wait(5)
            return True

        st = self.make(runner)
        st.schedule(600, "restart")
        self.clock.advance(100)
        b_waiting = threading.Event()
        st._op = _SignalOnAcquire(st._op, b_waiting, "probe-B")  # B passa do 1º teste e espera o _op
        out = {}
        t_a = threading.Thread(target=lambda: out.setdefault("a", st.stop()), name="probe-A")
        t_a.start()
        self.assertTrue(entered.wait(5))
        t_b = threading.Thread(target=lambda: out.setdefault("b", st.stop()), name="probe-B")
        t_b.start()
        self.assertTrue(b_waiting.wait(5), "a segunda parada não chegou a esperar o _op")
        release.set()
        t_a.join(5)
        t_b.join(5)
        self.assertEqual(calls, [("restart", 500)], "uma entrega só, mesmo com duas paradas")
        self.assertTrue(out["a"]["ok"])
        # quem esperou vê que já foi entregue: não entrega de novo, mas avisa que o desligamento vai acontecer
        self.assertEqual((out["b"]["ok"], out["b"]["action"]), (True, "restart"))

    def test_api_quit_then_server_close_delivers_once(self):  # (iv) sequencial
        run = Scripted()
        srv = serve_quiet(self, self.dir, run)
        port = srv.server_address[1]
        http_json(port, "POST", "/api/restart", {"pin": "482913", "seconds": 600})
        code, body = http_json(port, "POST", "/api/quit", {"pin": "482913"})
        self.assertEqual(code, 200)
        self.assertEqual(body["handoff"]["ok"], True)
        srv.shutdown()  # o quit já pediu o shutdown; espera o serve_forever sair antes de fechar o socket
        srv.server_close()  # o main() faz isso no finally: não pode entregar de novo
        self.assertEqual(run.calls, [("restart", 600)])

    def _tick_blocked_in_handoff(self):
        """Agenda, chega ao último minuto e deixa o tick preso dentro do shutdown.exe (segurando o _op)."""
        entered, release = threading.Event(), threading.Event()
        calls = []

        def runner(action, seconds):
            calls.append((action, seconds))
            if action != "abort":
                entered.set()
                release.wait(5)
            return True

        st = self.make(runner)
        st.schedule(600, "shutdown")
        self.clock.advance(545)  # restam 55 s: o tick entrega
        t_tick = threading.Thread(target=st.tick)
        t_tick.start()
        self.assertTrue(entered.wait(5))
        self.addCleanup(t_tick.join, 5)
        self.addCleanup(release.set)
        return st, calls, release, t_tick

    def test_busy_stop_never_says_it_will_not_happen_when_inflight_handoff_succeeds(self):
        # antes: a parada dizia "NÃO vai acontecer" e o tick em andamento entregava logo depois
        st, calls, release, t_tick = self._tick_blocked_in_handoff()
        with mock.patch.object(server, "STOP_WAIT_SECONDS", 0.05), self.assertLogs("shutlite", "ERROR") as cm:
            info = st.stop()  # o _op está ocupado pelo tick: não dá para confirmar
        release.set()
        t_tick.join(5)
        self.assertEqual(calls, [("shutdown", 55)])
        self.assertIs(self.saved()["handoff"], True, "a entrega do tick foi bem-sucedida")
        self.assertEqual((info["ok"], info["reason"]), (False, "busy"))
        self.assertIn("PODE acontecer", cm.output[0])
        self.assertNotIn("NÃO vai acontecer", cm.output[0])

    def test_stop_reports_delivery_when_inflight_handoff_finishes_while_waiting(self):
        st, calls, release, t_tick = self._tick_blocked_in_handoff()
        threading.Timer(0.1, release.set).start()  # o shutdown.exe responde enquanto a parada espera
        with self.assertLogs("shutlite", "WARNING") as cm:
            info = st.stop()
        t_tick.join(5)
        self.assertEqual(calls, [("shutdown", 55)])  # entregue uma vez só, pelo tick
        self.assertIs(info["ok"], True)
        self.assertRegex(info["at"], r"^\d\d:\d\d$")
        self.assertTrue(any("já foi entregue ao Windows" in line for line in cm.output))

    def test_busy_stop_reports_delivery_if_handoff_already_marked(self):
        # o tick já marcou a entrega mas ainda segura o _op (avisando as páginas): é "vai acontecer"
        st = self.make()
        st.schedule(600, "shutdown")
        st._op.acquire()

        def mark():
            with st._lock:
                st._handoff = True
        threading.Timer(0.05, mark).start()  # durante a espera da parada, com o _op ainda ocupado
        try:
            with mock.patch.object(server, "STOP_WAIT_SECONDS", 0.5), self.assertLogs("shutlite", "WARNING"):
                info = st.stop()
        finally:
            st._op.release()
        self.assertEqual((info["ok"], info.get("reason")), (True, None))
        self.assertEqual(self.run.calls, [])

    def test_stop_command_busy_says_it_may_happen(self):
        run = Scripted()
        srv = serve_quiet(self, self.dir, run)
        port = srv.server_address[1]
        http_json(port, "POST", "/api/shutdown", {"pin": "482913", "seconds": 600})
        cfg = server.Config(port=port, pin="482913", data_dir=self.dir, bind="127.0.0.1")
        buf = io.StringIO()
        srv.state._op.acquire()  # um comando ao Windows em andamento
        try:
            with mock.patch.object(server, "STOP_WAIT_SECONDS", 0.05), contextlib.redirect_stdout(buf),                     self.assertLogs("shutlite", "ERROR"):
                code = server.stop_running(cfg)
        finally:
            srv.state._op.release()
        out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("PODE acontecer", out)
        self.assertIn("shutdown /a", out)
        self.assertNotIn("NÃO consegui entregá-lo", out)


class ReadBoundTests(_SilentBase):
    def test_read_is_bounded_to_64kb_plus_one(self):  # mata a mutação f1 (lê o arquivo inteiro)
        p = self.dir / "big.json"
        p.write_bytes(b"{" + b" " * 2_000_000 + b"}")
        real_open = open
        reads = []

        def spy(path, mode="r", *a, **kw):
            f = real_open(path, mode, *a, **kw)

            class W:
                def __enter__(s):
                    return s

                def __exit__(s, *exc):
                    f.close()

                def read(s, n=-1):
                    reads.append(n)
                    return f.read(n)

            return W()

        with mock.patch.object(server, "open", spy, create=True), self.assertLogs("shutlite", "WARNING"):
            self.assertEqual(server._read_json(p), {})
        self.assertTrue(reads)
        self.assertTrue(all(0 < n <= server.MAX_JSON_FILE + 1 for n in reads), reads)


class ReadmeDocumentsTests(unittest.TestCase):
    def test_readme_documents_final_minute_stop_risks_clock_and_restart(self):  # (vi)
        import re
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        needles = [
            r"quando faltam 60 segundos",   # aviso final de 60 s
            r"último alerta",
            r"matar o processo",            # risco de matar o processo
            r"taskkill",
            r"Há um desligamento",          # parada ordenada entrega e avisa
            r"entreguei",
            r"5\s+segundos",                # dois relógios e a tolerância de 5 s
            r"relógio\s+interno",
            r"### Se o PC reiniciar",
            r"--windows-timer",
        ]
        for needle in needles:
            with self.subTest(needle=needle):
                self.assertRegex(readme, needle)


if __name__ == "__main__":
    unittest.main(verbosity=2)
