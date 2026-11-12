#!/usr/bin/env python3
"""shutlite — controle remoto de desligamento do PC pela rede local.

Servidor HTTP stdlib-only para Windows:
- interface web em / (arquivo web/index.html)
- API JSON em /api/*
- comandos via shutdown.exe (/s, /r, /a)

Uso:
    python server.py                 # sobe o servidor (PIN gerado na 1ª execução)
    python server.py --info          # mostra endereços e PIN
    python server.py --stop          # para o servidor que está rodando
    python server.py --dry-run       # simula: nunca chama shutdown.exe
    python server.py --autostart on  # inicia junto com o Windows

Config: CLI > variáveis de ambiente (PIN, PORT) > config.json na pasta de dados.
"""

from __future__ import annotations

import argparse
import hmac
import http.server
import ipaddress
import json
import logging
import logging.handlers
import math
import os
import queue
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

__version__ = "2.1.0"

LOG = logging.getLogger("shutlite")

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
INDEX_FILE = WEB_DIR / "index.html"

DEFAULT_PORT = 3333
MIN_PIN_LEN = 4
MIN_SECONDS = 5
MAX_SECONDS = 24 * 3600
MAX_BODY = 64 * 1024
MAX_SUBS = 32  # páginas abertas ao mesmo tempo no /api/events
ACTIONS = ("shutdown", "restart")

Runner = Callable[[str, int], bool]


# ---------------------------------------------------------------- config

def default_data_dir() -> Path:
    env = os.getenv("SHUTLITE_HOME")
    if env:
        return Path(env)
    local = os.getenv("LOCALAPPDATA")
    if local:
        return Path(local) / "shutlite"
    return Path.home() / ".shutlite"


def _write_json(path: Path, obj: object) -> None:
    """Grava de forma atômica: nunca deixa arquivo pela metade."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path) -> dict:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


@dataclass(frozen=True)
class Config:
    port: int
    pin: str
    data_dir: Path
    bind: str = ""  # vazio = todas as interfaces (IPv4 + IPv6)
    allow_public: bool = False
    allow_hosts: tuple = ()
    dry_run: bool = False

    @classmethod
    def load(cls, args: argparse.Namespace) -> "Config":
        data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
        cfg_file = data_dir / "config.json"
        saved = _read_json(cfg_file)

        port = args.port or os.getenv("PORT") or saved.get("port") or DEFAULT_PORT
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise ValueError(f"porta inválida: {port}") from None
        if not (1 <= port <= 65535):
            raise ValueError(f"porta inválida: {port}")

        pin = args.pin or os.getenv("PIN") or saved.get("pin")
        if not pin:  # primeira execução: PIN aleatório, nunca um padrão fixo
            pin = f"{secrets.randbelow(10 ** 6):06d}"
            _write_json(cfg_file, {**saved, "pin": pin})
        if not isinstance(pin, str) or len(pin) < MIN_PIN_LEN:
            raise ValueError(f"PIN precisa de ao menos {MIN_PIN_LEN} caracteres")

        return cls(
            port=port,
            pin=pin,
            data_dir=data_dir,
            bind=args.bind or "",
            allow_public=args.allow_public,
            allow_hosts=tuple(h.lower() for h in (args.allow_host or [])),
            dry_run=args.dry_run,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="shutlite — servidor local de desligamento")
    p.add_argument("--port", type=int, default=None, help=f"porta (padrão: $PORT ou {DEFAULT_PORT})")
    p.add_argument("--pin", type=str, default=None, help="PIN (padrão: $PIN ou o salvo em config.json)")
    p.add_argument("--bind", type=str, default=None, help="endereço de escuta (padrão: todas as interfaces)")
    p.add_argument("--data-dir", type=str, default=None, help="pasta de config, estado e log")
    p.add_argument("--allow-public", action="store_true",
                   help="aceita clientes fora da rede local (ex.: VPN). Use com cuidado")
    p.add_argument("--allow-host", action="append", metavar="NOME",
                   help="nome de host extra aceito no cabeçalho Host (pode repetir)")
    p.add_argument("--dry-run", action="store_true", help="simula: não chama shutdown.exe")
    p.add_argument("--info", action="store_true", help="mostra endereços e PIN e sai")
    p.add_argument("--stop", action="store_true", help="para o servidor em execução e sai")
    p.add_argument("--autostart", choices=("on", "off"), help="liga/desliga o início com o Windows")
    p.add_argument("--debug", action="store_true", help="log verboso")
    p.add_argument("--version", action="version", version=f"shutlite {__version__}")
    return p.parse_args(argv)


# ---------------------------------------------------------------- shutdown.exe

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ERROR_NOTHING_TO_ABORT = 1116


def run_shutdown(action: str, seconds: int) -> bool:
    """Chama shutdown.exe e diz se deu certo. /s desliga, /r reinicia, /a aborta."""
    if action == "abort":
        cmd = ["shutdown.exe", "/a"]
    else:
        cmd = ["shutdown.exe", "/r" if action == "restart" else "/s", "/t", str(seconds)]
    try:
        res = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             errors="replace", timeout=15, creationflags=NO_WINDOW)
    except FileNotFoundError:
        LOG.error("shutdown.exe não encontrado — isto só roda no Windows")
        return False
    except (subprocess.TimeoutExpired, OSError) as exc:
        LOG.error("shutdown.exe não respondeu: %s", exc)
        return False
    if res.returncode == 0:
        return True
    if action == "abort" and res.returncode == ERROR_NOTHING_TO_ABORT:
        return True  # não havia nada agendado: é o resultado que queríamos
    LOG.error("shutdown.exe falhou (código %s): %s", res.returncode,
              (res.stderr or res.stdout or "").strip())
    return False


def dry_run_shutdown(action: str, seconds: int) -> bool:
    LOG.info("[simulação] %s em %ss", action, seconds)
    return True


# ---------------------------------------------------------------- estado

class ShutdownError(RuntimeError):
    """O Windows recusou o comando; a mensagem vai para o usuário."""


class ShutdownState:
    """Guarda o agendamento atual. Quem executa de verdade é o shutdown.exe.

    O estado é salvo em disco para o painel continuar certo (e o cancelamento
    continuar funcionando) depois que o servidor reinicia.
    """

    def __init__(self, runner: Runner = run_shutdown, store: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._op = threading.Lock()  # um comando do Windows por vez
        self._runner = runner
        self._store = store
        self._at: float | None = None
        self._action: str | None = None
        self._total = 0
        self._rev = 0  # revisão: só aumenta enquanto o servidor vive
        self._restore()

    def schedule(self, seconds: int, action: str) -> dict:
        seconds = max(MIN_SECONDS, min(int(seconds), MAX_SECONDS))
        with self._op:
            pending = self.snapshot()["scheduled"]
            if pending:  # o Windows recusa um segundo agendamento (erro 1190)
                self._runner("abort", 0)
            ok = self._runner(action, seconds)
            if not ok and not pending:  # pode haver um agendamento feito por fora
                ok = self._runner("abort", 0) and self._runner(action, seconds)
            if not ok:
                self._set(None, None, 0)
                raise ShutdownError("O Windows recusou o comando. Tente de novo.")
            self._set(time.time() + seconds, action, seconds)
        return self.snapshot()

    def cancel(self) -> dict:
        with self._op:
            if not self._runner("abort", 0):
                raise ShutdownError("Não foi possível cancelar. Tente de novo.")
            self._set(None, None, 0)
        return self.snapshot()

    def snapshot(self) -> dict:
        with self._lock:
            at, action, total, rev = self._at, self._action, self._total, self._rev
        left = int(at - time.time()) if at is not None else 0
        if at is not None and left <= 0:  # já executou / expirou
            with self._lock:
                if self._at == at:
                    self._at, self._action, self._total = None, None, 0
            self._persist()
        if left <= 0:
            return {"scheduled": False, "left": 0, "total": 0, "action": None, "rev": rev}
        return {"scheduled": True, "left": left, "total": total, "action": action, "rev": rev}

    def _set(self, at: float | None, action: str | None, total: int) -> None:
        with self._lock:
            self._at, self._action, self._total = at, action, total
            self._rev += 1
        self._persist()

    def _persist(self) -> None:
        if self._store is None:
            return
        with self._lock:
            at, action, total = self._at, self._action, self._total
        try:
            if at is None:
                self._store.unlink(missing_ok=True)
            else:
                _write_json(self._store, {"at": at, "action": action, "total": total})
        except OSError as exc:
            LOG.warning("não deu para salvar o estado: %s", exc)

    def _restore(self) -> None:
        if self._store is None:
            return
        saved = _read_json(self._store)
        at, action, total = saved.get("at"), saved.get("action"), saved.get("total")
        if (isinstance(at, (int, float)) and not isinstance(at, bool)
                and at > time.time() and action in ACTIONS):
            self._at, self._action = float(at), action
            self._total = total if isinstance(total, int) and total > 0 else int(at - time.time())


def seconds_until(hh: int, mm: int, now: float | None = None) -> int:
    """Segundos até a próxima ocorrência de HH:MM no horário local."""
    now = time.time() if now is None else now
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    if target <= now:  # amanhã; mktime normaliza o dia e respeita horário de verão
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, hh, mm, 0, 0, 0, -1))
    return int(target - now)


# ---------------------------------------------------------------- PIN

class PinGuard:
    """Trava um endereço por alguns minutos depois de PINs errados seguidos."""

    def __init__(self, max_fails: int = 5, lock_seconds: int = 300) -> None:
        self._max = max_fails
        self._lock_seconds = lock_seconds
        self._lock = threading.Lock()
        self._recs: dict[str, list] = {}  # ip -> [erros, travado_até]

    def blocked(self, ip: str) -> int:
        """Segundos que faltam para liberar este endereço (0 = liberado)."""
        with self._lock:
            rec = self._recs.get(ip)
            left = rec[1] - time.monotonic() if rec else 0
        return math.ceil(left) if left > 0 else 0

    def fail(self, ip: str) -> None:
        with self._lock:
            if len(self._recs) > 1024:  # não cresce sem limite
                now = time.monotonic()
                self._recs = {k: v for k, v in self._recs.items() if v[1] > now}
            rec = self._recs.setdefault(ip, [0, 0.0])
            rec[0] += 1
            if rec[0] >= self._max:
                rec[0] = 0
                rec[1] = time.monotonic() + self._lock_seconds
                LOG.warning("PIN errado %s vezes: %s travado por %ss", self._max, ip, self._lock_seconds)

    def ok(self, ip: str) -> None:
        with self._lock:
            self._recs.pop(ip, None)


# ---------------------------------------------------------------- rede

def _norm_ip(addr: str):
    """Endereço do cliente como objeto ipaddress (desfaz ::ffff:a.b.c.d)."""
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        return ip.ipv4_mapped
    return ip


def is_local_net(addr: str) -> bool:
    """Loopback, faixas privadas (RFC 1918 / ULA) ou link-local."""
    ip = _norm_ip(addr)
    return bool(ip and (ip.is_loopback or ip.is_private or ip.is_link_local))


def is_loopback(addr: str) -> bool:
    ip = _norm_ip(addr)
    return bool(ip and ip.is_loopback)


def host_ok(host: str, extra: tuple = ()) -> bool:
    """Valida o cabeçalho Host. Barra DNS rebinding: um site externo só chega
    aqui usando um nome de domínio dele, nunca um IP literal ou o nome do PC."""
    host = host.strip().lower()
    if not host:
        return False
    if host.startswith("["):
        name = host[1:host.find("]")] if "]" in host else host
    elif host.count(":") == 1:
        name = host.rsplit(":", 1)[0]
    else:
        name = host
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    me = socket.gethostname().lower()
    return name in {"localhost", me, me + ".local", *extra}


def get_ipv4() -> list[str]:
    out: list[str] = []
    try:  # IP da rota padrão primeiro: é o que o celular alcança
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))  # UDP: nada é enviado
            out.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            if ip not in out:
                out.append(ip)
    except OSError:
        pass
    return [ip for ip in out if not ip.startswith("127.") and ip != "0.0.0.0"]


def get_ipv6(allow_public: bool = False) -> list[str]:
    out: list[str] = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6)
    except OSError:
        return out
    for info in infos:
        ip = _norm_ip(info[4][0])
        if ip is None or ip.version != 6 or ip.is_loopback or ip.is_link_local:
            continue
        if not allow_public and not ip.is_private:
            continue
        if str(ip) not in out:
            out.append(str(ip))
    return out


def list_urls(cfg: Config, port: int) -> list[str]:
    urls = [f"http://localhost:{port}"]
    urls += [f"http://{ip}:{port}" for ip in get_ipv4()]
    urls += [f"http://[{ip}]:{port}" for ip in get_ipv6(cfg.allow_public)]
    return urls


# ---------------------------------------------------------------- http

def _broadcast(srv: "DualStackServer", snapshot: dict) -> None:
    """Avisa todas as páginas conectadas no /api/events. Nunca trava o POST."""
    with srv.subs_lock:
        subs = list(srv.subs)
    for inbox in subs:
        try:
            inbox.put_nowait(snapshot)
        except queue.Full:
            pass  # cliente lento perde esse frame; o próximo reconcile pega


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = f"shutlite/{__version__}"
    timeout = 30  # conexão parada não segura uma thread para sempre

    @property
    def _cfg(self) -> Config:
        return self.server.config  # type: ignore[attr-defined]

    @property
    def _state(self) -> ShutdownState:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: object) -> None:
        LOG.debug("%s %s", self.address_string(), fmt % args)

    # -- helpers
    def _json(self, obj: object, code: int = 200, headers: dict | None = None) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path, ctype: str) -> None:
        try:
            body = path.read_bytes()
        except OSError:
            self._json({"error": "Não encontrado."}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")  # nenhum site embute o painel
        self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict | None:
        """Corpo JSON como dict. None = grande demais."""
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            n = 0
        if n > MAX_BODY:
            return None
        if n <= 0:
            return {}
        try:
            obj = json.loads(self.rfile.read(n))
        except ValueError:
            return {}
        return obj if isinstance(obj, dict) else {}

    def _guard(self) -> bool:
        """Checagens que valem para toda requisição."""
        if not self._cfg.allow_public and not is_local_net(self.client_address[0]):
            self._json({"error": "Acesso permitido só pela rede local."}, 403)
            return False
        if not host_ok(self.headers.get("Host", ""), self._cfg.allow_hosts):
            self._json({"error": "Host não permitido."}, 403)
            return False
        return True

    def _events(self) -> None:
        """Stream SSE: empurra cada mudança de estado para esta página, ao vivo."""
        srv = self.server
        inbox: queue.Queue = queue.Queue(maxsize=8)
        with srv.subs_lock:
            full = len(srv.subs) >= MAX_SUBS
            if not full:
                srv.subs.add(inbox)
        if full:
            self._json({"error": "Muitas páginas abertas."}, 503)
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._send_event(self._state.snapshot())  # sincroniza na hora
            while True:
                try:
                    self._send_event(inbox.get(timeout=20))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # heartbeat, segura a conexão
                    self.wfile.flush()
        except (OSError, ValueError):
            pass  # cliente fechou a página
        finally:
            with srv.subs_lock:
                srv.subs.discard(inbox)

    def _send_event(self, snapshot: dict) -> None:
        self.wfile.write(("data: " + json.dumps(snapshot) + "\n\n").encode("utf-8"))
        self.wfile.flush()

    def _apply(self, change: Callable[[], dict]) -> None:
        """Roda uma mudança de estado, avisa as páginas e responde."""
        try:
            snap = change()
        except ShutdownError as exc:
            _broadcast(self.server, self._state.snapshot())
            self._json({"error": str(exc)}, 500)
            return
        _broadcast(self.server, snap)
        self._json(snap)

    # -- rotas
    def do_GET(self) -> None:
        if not self._guard():
            return
        route = urllib.parse.urlparse(self.path).path
        if route == "/":
            self._file(INDEX_FILE, "text/html; charset=utf-8")
        elif route == "/qrcode.min.js":
            self._file(WEB_DIR / "qrcode.min.js", "text/javascript; charset=utf-8")
        elif route == "/api/status":
            self._json(self._state.snapshot())
        elif route == "/api/events":
            self._events()
        elif route == "/api/info":
            local = is_loopback(self.client_address[0])
            info = {
                "version": __version__,
                "local": local,
                "dry_run": self._cfg.dry_run,
                "urls": list_urls(self._cfg, self.server.server_address[1]),
            }
            if local:  # só o próprio PC vê o PIN, para parear o celular
                info["pin"] = self._cfg.pin
            self._json(info)
        else:
            self._json({"error": "Não encontrado."}, 404)

    POST_ROUTES = ("/api/shutdown", "/api/restart", "/api/at", "/api/cancel", "/api/quit")

    def do_POST(self) -> None:
        body = self._body()  # lê antes de responder, mesmo em erro
        if not self._guard():
            return
        route = urllib.parse.urlparse(self.path).path
        if route not in self.POST_ROUTES:
            self._json({"error": "Não encontrado."}, 404)
            return
        if body is None:
            self._json({"error": "Requisição grande demais."}, 413)
            return
        # navegador sempre manda Origin em POST: outro site não passa daqui
        origin = self.headers.get("Origin")
        if origin is not None and (
                urllib.parse.urlparse(origin).netloc.lower() != self.headers.get("Host", "").lower()):
            self._json({"error": "Origem não permitida."}, 403)
            return
        ctype = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._json({"error": "Use Content-Type: application/json."}, 415)
            return

        ip = str(_norm_ip(self.client_address[0]))
        guard: PinGuard = self.server.pin_guard  # type: ignore[attr-defined]
        wait = guard.blocked(ip)
        if wait:
            self._json({"error": f"Muitas tentativas. Tente de novo em {wait} s."}, 429,
                       {"Retry-After": str(wait)})
            return
        pin = body.get("pin")
        if not isinstance(pin, str) or not hmac.compare_digest(
                pin.encode("utf-8"), self._cfg.pin.encode("utf-8")):
            guard.fail(ip)
            self._json({"error": "PIN inválido. Confira o PIN e tente de novo."}, 401)
            return
        guard.ok(ip)

        if route in ("/api/shutdown", "/api/restart"):
            seconds = body.get("seconds", 60)
            if isinstance(seconds, bool) or not isinstance(seconds, int):
                self._json({"error": "Duração inválida. Use segundos, ex: 1800 para 30 min."}, 400)
                return
            action = route.rsplit("/", 1)[1]
            self._apply(lambda: self._state.schedule(seconds, action))
        elif route == "/api/at":
            action = body.get("action", "shutdown")
            try:
                hh, mm = map(int, str(body.get("at", "")).strip().split(":"))
            except ValueError:
                hh = mm = -1
            if not (0 <= hh <= 23 and 0 <= mm <= 59) or action not in ACTIONS:
                self._json({"error": "Horário inválido. Use HH:MM, ex: 23:30."}, 400)
                return
            self._apply(lambda: self._state.schedule(seconds_until(hh, mm), action))
        elif route == "/api/cancel":
            self._apply(self._state.cancel)
        elif route == "/api/quit":
            if not is_loopback(self.client_address[0]):
                self._json({"error": "Só o próprio PC pode parar o servidor."}, 403)
                return
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()


class DualStackServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    # No Windows, SO_REUSEADDR deixaria duas instâncias na mesma porta.
    allow_reuse_address = os.name != "nt"
    config: Config
    state: ShutdownState
    pin_guard: PinGuard
    subs: set
    subs_lock: threading.Lock

    def server_bind(self) -> None:
        try:  # um socket só, IPv4 + IPv6
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except OSError:
            pass
        super().server_bind()

    def handle_error(self, request: object, client_address: object) -> None:
        # página fechada no meio da conversa: normal, não merece traceback
        LOG.debug("conexão interrompida: %s", client_address, exc_info=True)


def build_server(cfg: Config, runner: Runner | None = None) -> DualStackServer:
    class Server(DualStackServer):
        pass

    if cfg.bind:
        Server.address_family = socket.AF_INET6 if ":" in cfg.bind else socket.AF_INET
        addr = cfg.bind
    elif socket.has_dualstack_ipv6():
        Server.address_family, addr = socket.AF_INET6, "::"
    else:
        Server.address_family, addr = socket.AF_INET, "0.0.0.0"
    srv = Server((addr, cfg.port), Handler)
    srv.config = cfg
    if runner is None:
        runner = dry_run_shutdown if cfg.dry_run else run_shutdown
    srv.state = ShutdownState(runner, cfg.data_dir / "state.json")
    srv.pin_guard = PinGuard()
    srv.subs = set()
    srv.subs_lock = threading.Lock()
    return srv


# ---------------------------------------------------------------- comandos

def print_info(cfg: Config, show_pin: bool) -> None:
    print(f"[shutlite {__version__}] porta={cfg.port}" + ("  (simulação)" if cfg.dry_run else ""))
    for url in list_urls(cfg, cfg.port):
        print(f"  {url}")
    if show_pin:
        print(f"  PIN: {cfg.pin}")
    else:
        print("  PIN: abra o painel neste PC ou rode: python server.py --info")


def stop_running(cfg: Config) -> int:
    req = urllib.request.Request(
        f"http://127.0.0.1:{cfg.port}/api/quit",
        data=json.dumps({"pin": cfg.pin}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(req, timeout=5).close()
    except (urllib.error.URLError, OSError):
        print("shutlite não está rodando nesta porta (ou o PIN é outro).")
        return 1
    print("shutlite parado.")
    return 0


def set_autostart(enable: bool) -> int:
    appdata = os.getenv("APPDATA")
    if os.name != "nt" or not appdata:
        print("Início automático só existe no Windows.")
        return 1
    link = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "shutlite.cmd"
    if not enable:
        link.unlink(missing_ok=True)
        print("Início automático desligado.")
        return 0
    pyw = Path(sys.executable).with_name("pythonw.exe")
    exe = pyw if pyw.exists() else Path(sys.executable)
    link.write_text(
        f'@echo off\r\nchcp 65001 >nul\r\nstart "" "{exe}" "{Path(__file__).resolve()}"\r\n',
        encoding="utf-8",
    )
    print(f"Início automático ligado: {link}")
    return 0


def setup_logging(cfg: Config, debug: bool) -> None:
    LOG.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s [shutlite] %(message)s", "%Y-%m-%d %H:%M:%S")
    if sys.stderr is not None:  # com pythonw não há console
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter("[shutlite] %(message)s"))
        LOG.addHandler(console)
    try:
        cfg.data_dir.mkdir(parents=True, exist_ok=True)
        logfile = logging.handlers.RotatingFileHandler(
            cfg.data_dir / "shutlite.log", maxBytes=256 * 1024, backupCount=1, encoding="utf-8")
        logfile.setFormatter(fmt)
        LOG.addHandler(logfile)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.autostart:
        return set_autostart(args.autostart == "on")
    try:
        cfg = Config.load(args)
    except ValueError as exc:
        raise SystemExit(f"shutlite: {exc}") from None
    if args.stop:
        return stop_running(cfg)
    if args.info:
        print_info(cfg, show_pin=True)
        return 0

    setup_logging(cfg, args.debug)
    if not INDEX_FILE.exists():
        LOG.error("web/index.html não encontrado em %s", INDEX_FILE)
        return 1
    try:
        srv = build_server(cfg)
    except OSError as exc:
        LOG.error("não deu para abrir a porta %s (o shutlite já está rodando?): %s", cfg.port, exc)
        return 1
    print_info(cfg, show_pin=False)
    print("Ctrl+C para parar.")
    LOG.info("rodando na porta %s", cfg.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nparado.")
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
