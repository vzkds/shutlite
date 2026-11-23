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
    python server.py --check         # exit 0 se o servidor está rodando, 1 se não
    python server.py --dry-run      # simula: nunca chama shutdown.exe
    python server.py --windows-timer # timer do Windows desde o início (padrão: contagem silenciosa aqui)
    python server.py --autostart on  # inicia junto com o Windows

Config: CLI > variáveis de ambiente (PIN, PORT) > config.json na pasta de dados.
"""

from __future__ import annotations

import argparse
import contextlib
import hmac
import http.client
import http.server
import ipaddress
import json
import logging
import logging.handlers
import math
import os
import queue
import re
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, NoReturn

__version__ = "2.2.0"

LOG = logging.getLogger("shutlite")

# Empacotado com PyInstaller (shutlite.exe): os arquivos da página ficam na pasta temporária do .exe.
FROZEN = bool(getattr(sys, "frozen", False))
BASE_DIR = Path(getattr(sys, "_MEIPASS", "")) if FROZEN else Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
INDEX_FILE = WEB_DIR / "index.html"

DEFAULT_PORT = 3333
MIN_PIN_LEN = 6
BOOT_TOLERANCE = 30  # s de diferença aceitos entre duas leituras da hora do boot
AT_FORMAT = re.compile(r"[0-9]{2}:[0-9]{2}")  # horário de /api/at: exatamente HH:MM
MIN_SECONDS = 5
MAX_SECONDS = 24 * 3600
HANDOFF_SECONDS = 60  # modo silencioso: o Windows só assume no último minuto (o aviso dele é o último alerta)
TICK_SECONDS = 1.0  # de quanto em quanto tempo o timer local confere o relógio
CLOCK_JUMP_TOLERANCE = 5.0  # s: acima disso, relógio de parede e monotônico discordam (salto/suspensão)
MAX_JSON_FILE = 64 * 1024  # bytes: state.json/config.json maiores que isso são tratados como ilegíveis
SHUTDOWN_TIMEOUT = 6  # s: quanto esperamos por UMA chamada ao shutdown.exe (quem segura o _op não passa disso)
STOP_WAIT_SECONDS = 2.0  # s: na parada ordenada, quanto esperamos um comando em andamento largar o _op
CANCEL_ABORT_BELOW = 90  # s: cancelar contagem local com menos que isso restando manda abort também
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
    """Lê um JSON pequeno (state.json/config.json). Ausente, ilegível, grande demais ou que não seja
    um objeto: {}. Lê no máximo MAX_JSON_FILE bytes, então lixo enorme não vira MemoryError."""
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_JSON_FILE + 1)
        if len(raw) > MAX_JSON_FILE:
            LOG.warning("%s passa de %s KB; tratado como ilegível", path.name, MAX_JSON_FILE // 1024)
            return {}
        obj = json.loads(raw.decode("utf-8-sig"))  # tolera o BOM do Bloco de Notas
    except (OSError, ValueError, RecursionError):  # RecursionError: JSON aninhado fundo demais
        return {}
    return obj if isinstance(obj, dict) else {}


def _parse_port(raw: object) -> int:
    """Porta de 1 a 65535 vinda da CLI, do ambiente ou do config.json (que pode ter qualquer lixo:
    1e400 vira inf, true, 80.5, listas...). Qualquer coisa fora disso é ValueError com mensagem clara."""
    try:
        if isinstance(raw, bool):
            raise ValueError
        port = int(raw)  # type: ignore[call-overload]
        if isinstance(raw, float) and port != raw:  # 80.5 não vira 80 em silêncio
            raise ValueError
    except (ArithmeticError, TypeError, ValueError):
        raise ValueError(f"porta inválida: {str(raw)[:40]}") from None
    if not (1 <= port <= 65535):
        raise ValueError(f"porta inválida: {str(port)[:40]}")
    return port


@dataclass(frozen=True)
class Config:
    port: int
    pin: str
    data_dir: Path
    bind: str = ""  # vazio = todas as interfaces (IPv4 + IPv6)
    allow_public: bool = False
    allow_hosts: tuple = ()
    dry_run: bool = False
    windows_timer: bool = False  # False = modo silencioso (padrão); True = tudo via shutdown.exe na hora

    @classmethod
    def load(cls, args: argparse.Namespace) -> "Config":
        data_dir = Path(args.data_dir) if args.data_dir else default_data_dir()
        cfg_file = data_dir / "config.json"
        saved = _read_json(cfg_file)

        port = _parse_port(args.port or os.getenv("PORT") or saved.get("port") or DEFAULT_PORT)

        pin = args.pin or os.getenv("PIN") or saved.get("pin")
        if not pin:  # primeira execução: PIN aleatório, nunca um padrão fixo
            pin = f"{secrets.randbelow(10 ** 6):06d}"
            _write_json(cfg_file, {**saved, "pin": pin})
        if not isinstance(pin, str) or len(pin) < MIN_PIN_LEN:
            origin = ("a opção --pin" if args.pin else "a variável de ambiente PIN" if os.getenv("PIN")
                      else f'o campo "pin" de {cfg_file}')
            raise ValueError(
                f"o PIN (vindo de {origin}) precisa ter ao menos {MIN_PIN_LEN} caracteres. "
                f'Para trocar: edite "pin" em {cfg_file} ou rode com --pin e {MIN_PIN_LEN}+ caracteres.')

        return cls(
            port=port,
            pin=pin,
            data_dir=data_dir,
            bind=args.bind or "",
            allow_public=args.allow_public,
            allow_hosts=tuple(h.lower() for h in (args.allow_host or [])),
            dry_run=args.dry_run,
            # só CLI > config.json (sem variável de ambiente); no config só o JSON `true` liga
            windows_timer=bool(getattr(args, "windows_timer", False)) or saved.get("windows_timer") is True,
        )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="shutlite — servidor local de desligamento")
    p.add_argument("--port", type=int, default=None, help=f"porta (padrão: $PORT ou {DEFAULT_PORT})")
    p.add_argument("--pin", type=str, default=None,
                   help=f"PIN, mínimo {MIN_PIN_LEN} caracteres (padrão: $PIN ou o salvo em config.json)")
    p.add_argument("--bind", type=str, default=None, help="endereço de escuta (padrão: todas as interfaces)")
    p.add_argument("--data-dir", type=str, default=None, help="pasta de config, estado e log")
    p.add_argument("--allow-public", action="store_true",
                   help="aceita clientes fora da rede local (ex.: VPN). Use com cuidado")
    p.add_argument("--allow-host", action="append", metavar="NOME",
                   help="nome de host extra aceito no cabeçalho Host (pode repetir)")
    p.add_argument("--dry-run", action="store_true", help="simula: não chama shutdown.exe")
    p.add_argument("--windows-timer", action="store_true",
                   help="usa o timer do Windows desde o início (com o aviso dele); sem isso, o shutlite "
                        "conta o tempo em silêncio e só chama o Windows no último minuto. "
                        'Também vale "windows_timer": true no config.json')
    p.add_argument("--info", action="store_true", help="mostra endereços e PIN e sai")
    p.add_argument("--stop", action="store_true", help="para o servidor em execução e sai")
    p.add_argument("--check", action="store_true",
                   help="sai com 0 se o servidor está rodando na porta da config, 1 se não")
    p.add_argument("--autostart", choices=("on", "off"), help="liga/desliga o início com o Windows")
    p.add_argument("--open", action="store_true", help="abre o painel no navegador ao iniciar")
    p.add_argument("--no-open", action="store_true", help="nunca abre o navegador (usado no início automático)")
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
                             errors="replace", timeout=SHUTDOWN_TIMEOUT, creationflags=NO_WINDOW)
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
    """Guarda o agendamento atual e decide quem conta o tempo.

    Modo silencioso (padrão): a contagem fica aqui (deadline em horário de relógio, salvo em disco) e
    o shutdown.exe é chamado UMA vez, quando faltam HANDOFF_SECONDS ou menos ("handoff"). Agendar,
    adiar e cancelar antes disso não falam com o Windows, então não disparam o aviso de desligamento
    dele. O aviso do último minuto é intencional. Depois do handoff o Windows é o dono: cancelar
    é `abort`.
    Modo clássico (native_timer=True): tudo vai ao shutdown.exe na hora (aviso incluso).

    Uma thread (start/stop) chama tick() a cada TICK_SECONDS (sem threading.Timer). O prazo `_at` é
    horário de parede e só serve para exibir e para persistir/restaurar; quem decide é um prazo
    monotônico do processo (`_mono`, calculado ao agendar e ao restaurar). A cada tick os dois
    restantes são comparados: se divergem mais que CLOCK_JUMP_TOLERANCE (relógio mudou por NTP/ajuste
    manual, ou o PC foi suspenso), o agendamento é descartado sem entrega, falha segura: o shutlite nunca
    desliga cedo, tarde ou por um relógio errado. Sem divergência, entrega ao Windows quando o
    restante monotônico chega a HANDOFF_SECONDS (usa esse restante no /t) e descarta se ele (ou o de
    parede) chega a zero.

    O estado é salvo em disco para o painel continuar certo depois que o servidor reinicia.
    """

    def __init__(self, runner: Runner = run_shutdown, store: Path | None = None,
                 boot: Callable[[], float | None] | None = None, native_timer: bool = False,
                 on_change: Callable[[dict], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._op = threading.Lock()  # um comando do Windows por vez (e o handoff também)
        self._runner = runner
        self._store = store
        self._boot = boot or boot_time  # injetável: os testes não dependem do Windows
        self._native = native_timer
        self.on_change = on_change  # chamado (sem locks) quando o timer muda o estado sozinho
        self._at: float | None = None  # prazo em horário de parede (exibir/persistir)
        self._mono: float | None = None  # o mesmo prazo no relógio monotônico (quem decide o handoff)
        self._action: str | None = None
        self._total = 0
        self._handoff = False  # True: o shutdown.exe já tem o timer (Windows é o dono)
        self._rev = 0  # revisão: só aumenta enquanto o servidor vive
        self._handoff_failed_at: float | None = None  # `at` cuja entrega já falhou (loga uma vez só)
        self._waiting = 0  # cancelar/adiar esperando o _op: o tick não encadeia comandos na frente deles
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._restore()

    # -- agendar / cancelar
    @contextlib.contextmanager
    def _user_turn(self):
        """O _op para uma ação do usuário (agendar/adiar/cancelar). Enquanto espera a vez fica
        registrado em `_waiting`: o tick, ao ver isso, não encadeia mais comandos do Windows na frente."""
        with self._lock:
            self._waiting += 1
        try:
            self._op.acquire()
        finally:
            with self._lock:
                self._waiting -= 1
        try:
            yield
        finally:
            self._op.release()

    def _user_waiting(self) -> bool:
        with self._lock:
            return self._waiting > 0

    def schedule(self, seconds: int, action: str) -> dict:
        """Agenda `action` em `seconds`, trocando o agendamento atual (se houver).

        Silencioso e seconds > HANDOFF_SECONDS: só muda o estado local (zero chamadas ao Windows;
        se o anterior já era do Windows, um abort antes). Caso contrário (≤ HANDOFF_SECONDS, ou modo
        clássico) entrega ao Windows agora. O Windows só aceita um agendamento por vez (erro 1190),
        então trocar é abort + novo, e o estado nunca pode mentir sobre ele:
        - abort falhou: o agendamento antigo continua valendo; o estado não muda.
        - abort ok, novo falhou: tenta restaurar o antigo; se não der, nada fica agendado.
        """
        seconds = max(MIN_SECONDS, min(int(seconds), MAX_SECONDS))
        with self._user_turn():
            self.snapshot()  # descarta o que já expirou antes de decidir
            with self._lock:
                prev_at, prev_action, prev_total, prev_handoff = (
                    self._at, self._action, self._total, self._handoff)
            win_pending = prev_at is not None and prev_handoff  # o Windows tem um timer nosso
            local_pending = prev_at is not None and not prev_handoff  # só nós sabemos dele
            if win_pending and not self._runner("abort", 0):
                LOG.error("não consegui cancelar o agendamento atual; mantido")
                raise ShutdownError("Não consegui cancelar o agendamento atual; ele continua valendo.")
            if not self._native and seconds > HANDOFF_SECONDS:  # silencioso: contagem só no shutlite
                self._set(time.time() + seconds, action, seconds, False)
            else:  # entrega ao Windows agora
                t0 = time.time()  # o timer do Windows começa aqui, não depois do runner
                ok = self._runner(action, seconds)
                if not ok and not win_pending:  # pode haver um agendamento feito por fora
                    if self._runner("abort", 0):
                        LOG.warning("havia um agendamento feito fora do shutlite; substituído")
                        t0 = time.time()
                        ok = self._runner(action, seconds)
                if not ok:
                    self._recover(win_pending, prev_at, prev_action, prev_total, local_pending)
                self._set(t0 + seconds, action, seconds, True)
        return self.snapshot()

    def _recover(self, pending: bool, prev_at: float | None, prev_action: str | None,
                 prev_total: int, local_pending: bool = False) -> NoReturn:
        """A entrega ao Windows falhou: deixa Windows == estado e explica o que houve.

        `pending`: o Windows tinha um timer nosso (já abortado). `local_pending`: havia uma contagem só
        local, que o Windows nunca viu; ela continua valendo se der para confirmar que o Windows está limpo.
        """
        cleared = self._runner("abort", 0)  # limpa agendamento pela metade (ex.: timeout)
        if pending and prev_at is not None and prev_action:
            left = int(prev_at - time.time())
            if left > MIN_SECONDS:
                t1 = time.time()
                if self._runner(prev_action, left):
                    LOG.warning("novo agendamento falhou; o anterior foi restaurado (%ss)", left)
                    self._set(t1 + left, prev_action, prev_total, True)
                    raise ShutdownError("Não consegui criar o novo agendamento; o anterior foi mantido.")
        if local_pending and cleared:
            LOG.warning("não consegui agendar agora; a contagem anterior continua valendo")
            raise ShutdownError("Não consegui agendar agora; o agendamento anterior continua valendo.")
        self._set(None, None, 0, False)
        if not cleared:
            LOG.error("não deu para confirmar que o Windows ficou sem agendamento")
            if local_pending:  # a contagem anterior se perdeu: diz isso, não só "recusou"
                raise ShutdownError(
                    "Não consegui agendar agora e o agendamento anterior foi cancelado. "
                    "Use Cancelar tudo para garantir.")
            raise ShutdownError(
                "O Windows recusou o comando e não consegui confirmar que não há nada agendado. "
                "Use Cancelar tudo para garantir.")
        if pending:
            raise ShutdownError(
                "Não consegui criar o novo agendamento e o anterior foi cancelado. Nada está agendado.")
        raise ShutdownError("O Windows recusou o comando. Tente de novo.")

    def cancel(self) -> dict:
        """Cancela. Contagem só local: limpa o estado, sem tocar no Windows (sem aviso); com menos de
        CANCEL_ABORT_BELOW s restando, manda também um abort de segurança (sem timer no Windows o 1116 não
        gera aviso; com timer, fecha a janela de um crash entre o runner OK do handoff e o persist), e
        uma falha dele é ignorada. Já entregue ao Windows, ou painel ocioso: abort (ocioso garante que o
        Windows está limpo)."""
        with self._user_turn():
            with self._lock:
                at, mono, handoff = self._at, self._mono, self._handoff
            if at is not None and not handoff and at > time.time():
                left = min(at - time.time(), (mono - time.monotonic()) if mono is not None else math.inf)
                self._set(None, None, 0, False)
                if left < CANCEL_ABORT_BELOW and not self._runner("abort", 0):
                    LOG.debug("abort de segurança ao cancelar a contagem local falhou; ignorado")
            else:
                if not self._runner("abort", 0):
                    raise ShutdownError("Não foi possível cancelar. Tente de novo.")
                self._set(None, None, 0, False)
        return self.snapshot()

    # -- timer local (modo silencioso)
    @staticmethod
    def _left(at: float, mono: float) -> tuple[float, float, bool]:
        """(restante pela parede, restante pelo monotônico, os dois discordam além da tolerância?)."""
        wall_left = at - time.time()
        mono_left = mono - time.monotonic()
        return wall_left, mono_left, abs(mono_left - wall_left) > CLOCK_JUMP_TOLERANCE

    def tick(self) -> None:
        """Um passo do timer (ver a regra dos dois relógios na docstring da classe): descarta se o
        relógio saltou ou o prazo passou; entrega ao Windows (uma vez) quando o restante monotônico chega
        a HANDOFF_SECONDS. Seguro contra cancelar/adiar concorrentes: a decisão é refeita sob _op, e o
        runner só roda se o estado ainda for o que foi decidido."""
        with self._lock:
            at, mono, handoff = self._at, self._mono, self._handoff
        if at is None or mono is None or handoff:
            return
        wall_left, mono_left, jumped = self._left(at, mono)
        if not jumped and wall_left > 0 and mono_left > HANDOFF_SECONDS:
            return  # caminho barato (1x/s): sem _op, sem disco
        changed = False
        with self._op:
            with self._lock:
                at, mono, action, handoff = self._at, self._mono, self._action, self._handoff
            if at is None or mono is None or handoff or action is None:
                return  # cancelado, trocado ou já entregue enquanto esperávamos o _op
            wall_left, mono_left, jumped = self._left(at, mono)
            if jumped:  # relógio mudou ou PC suspenso: não dá para confiar no prazo; falha segura
                self._set(None, None, 0, False)
                LOG.warning("relógio do sistema mudou ou o PC foi suspenso; agendamento descartado, "
                            "nada será desligado (parede %+.0fs, monotônico %+.0fs)", wall_left, mono_left)
                changed = True
            elif mono_left <= 0 or wall_left <= 0:  # o prazo passou sem entrega: nunca desligar atrasado
                failed = self._handoff_failed_at == at
                self._set(None, None, 0, False)
                if failed:
                    LOG.error("não consegui entregar o agendamento ao Windows até o prazo; descartado")
                else:
                    LOG.warning("o prazo do agendamento passou antes da entrega ao Windows "
                                "(PC suspenso ou servidor parado); descartado, nada será desligado")
                changed = True
            elif mono_left > HANDOFF_SECONDS:
                return  # adiado enquanto esperávamos
            else:
                ok = self._runner(action, max(1, math.ceil(mono_left)))
                if not ok and self._handoff_failed_at != at:  # 1ª falha: pode haver timer externo
                    self._handoff_failed_at = at
                    LOG.error("não consegui entregar o agendamento ao Windows; tento de novo a cada segundo")
                    # a cadeia abort + nova tentativa só segue se ninguém (cancelar/adiar) estiver esperando o
                    # _op: quem espera passa na frente em vez de ficar atrás de até 3 chamadas ao shutdown.exe
                    if not self._user_waiting() and self._runner("abort", 0) and not self._user_waiting():
                        ok = self._runner(action, max(1, math.ceil(mono - time.monotonic())))
                if ok:
                    self._finish_handoff(at)
                    changed = True
        if changed and self.on_change is not None:
            try:
                self.on_change(self.snapshot())
            except Exception:  # aviso às páginas nunca derruba o timer
                LOG.exception("falha ao avisar as páginas")

    def _finish_handoff(self, at: float) -> None:
        with self._lock:
            if self._at != at or self._handoff:
                return  # o estado mudou durante o comando lento (ex.: expirou); nada a marcar
            self._handoff = True
            self._rev += 1
        self._persist()
        LOG.info("agendamento entregue ao Windows (faltam ~%ss)", max(0, int(at - time.time())))

    def start(self) -> None:
        """Liga a thread do timer (daemon). Chamar de novo não duplica."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._run, name="shutlite-timer", daemon=True)
        self._thread.start()

    def hand_off_pending(self) -> dict | None:
        """Parada ordenada: se há uma contagem LOCAL pendente com restante > 0, entrega ao Windows o que
        falta (UMA tentativa, sem retry: o shutdown.exe demora no máximo SHUTDOWN_TIMEOUT s), marca
        handoff=true e persiste, para a parada do shutlite não cancelar o desligamento em silêncio.
        Devolve None se não havia nada a entregar, ou {"ok", "action", "at": "HH:MM", ...}. Espera no
        máximo STOP_WAIT_SECONDS por um comando em andamento largar o _op."""
        with self._lock:
            pending = self._at is not None and not self._handoff
        if not pending:
            return None
        if not self._op.acquire(timeout=STOP_WAIT_SECONDS):
            # Outro comando ao Windows segura o _op. Pode ser o próprio tick entregando este agendamento
            # (o shutdown.exe demorou): então NÃO dá para afirmar que ele não vai acontecer.
            with self._lock:
                at, action, handoff = self._at, self._action, self._handoff
            if at is None or action is None:
                return None  # cancelado enquanto esperávamos
            when = time.strftime("%H:%M", time.localtime(at))
            if handoff:  # o comando em andamento era a entrega, e ela já deu certo
                LOG.warning("Há um desligamento às %s; já foi entregue ao Windows (o aviso dele vai "
                            "aparecer).", when)
                return {"ok": True, "action": action, "at": when}
            LOG.error("Há um desligamento às %s, mas um comando ao Windows ainda estava em andamento e não "
                      "consegui confirmar a entrega. Ele PODE acontecer; para garantir que não aconteça, "
                      "rode: shutdown /a", when)
            return {"ok": False, "reason": "busy", "action": action, "at": when}
        info: dict | None = None
        try:
            with self._lock:
                at, mono, action, handoff = self._at, self._mono, self._action, self._handoff
            if at is None or mono is None or action is None:
                return None  # cancelado enquanto esperávamos
            when = time.strftime("%H:%M", time.localtime(at))
            if handoff:  # o tick entregou enquanto esperávamos: quem parou precisa saber que vai acontecer
                LOG.warning("Há um desligamento às %s; já foi entregue ao Windows (o aviso dele vai "
                            "aparecer).", when)
                return {"ok": True, "action": action, "at": when}
            wall_left, mono_left, jumped = self._left(at, mono)
            if jumped:  # relógio mudou ou PC suspenso: o prazo não é confiável; não arrisca desligar errado
                LOG.error("há um desligamento às %s, mas o relógio do sistema mudou; NÃO entreguei ao "
                          "Windows e ele NÃO vai acontecer", when)
                return {"ok": False, "reason": "clock", "action": action, "at": when}
            if mono_left <= 0 or wall_left <= 0:
                return None  # já venceu: nada a entregar (nunca desligar atrasado)
            if self._runner(action, max(1, math.ceil(mono_left))):
                self._finish_handoff(at)
                LOG.warning("Há um desligamento às %s; entreguei ao Windows (o aviso dele vai aparecer).", when)
                info = {"ok": True, "action": action, "at": when}
            else:
                LOG.error("Há um desligamento às %s, mas NÃO consegui entregá-lo ao Windows; sem o shutlite "
                          "rodando ele NÃO vai acontecer.", when)
                return {"ok": False, "reason": "runner", "action": action, "at": when}
        finally:
            self._op.release()
        if info is not None and self.on_change is not None:
            try:
                self.on_change(self.snapshot())
            except Exception:
                LOG.exception("falha ao avisar as páginas")
        return info

    def stop(self, deliver: bool = True) -> dict | None:
        """Parada ordenada do timer (idempotente). Com `deliver` (padrão), antes de parar entrega ao
        Windows a contagem local pendente (hand_off_pending) e devolve o resultado. Não fica preso a um
        comando do Windows travado: espera o _op até STOP_WAIT_SECONDS e a thread até 3 s."""
        self._stop_evt.set()  # nenhum tick novo começa daqui em diante
        info = self.hand_off_pending() if deliver else None
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3)
            if not t.is_alive():
                self._thread = None
        return info

    def _run(self) -> None:
        last_error = None
        while not self._stop_evt.wait(TICK_SECONDS):
            try:
                self.tick()
            except Exception as exc:  # o timer não pode morrer; não repete o mesmo erro a cada segundo
                if repr(exc) != last_error:
                    LOG.exception("falha no timer do shutlite")
                    last_error = repr(exc)

    # -- estado
    def snapshot(self) -> dict:
        with self._lock:
            at, action, total, rev, handoff = self._at, self._action, self._total, self._rev, self._handoff
        remaining = at - time.time() if at is not None else 0.0  # em float: só arredonda para exibir
        if at is not None and remaining <= 0:  # já executou / expirou
            with self._lock:
                expired = self._at == at
                if expired:
                    self._at, self._mono, self._action, self._total, self._handoff = None, None, None, 0, False
            if expired and not handoff:
                LOG.warning("o prazo do agendamento passou antes da entrega ao Windows; descartado")
            self._persist()
        if remaining <= 0:
            return {"scheduled": False, "left": 0, "total": 0, "action": None, "mode": None, "rev": rev}
        return {"scheduled": True, "left": math.ceil(remaining), "total": total, "action": action,
                "mode": "windows" if handoff else "local", "rev": rev}

    def _set(self, at: float | None, action: str | None, total: int, handoff: bool) -> None:
        with self._lock:
            self._at, self._action, self._total = at, action, total
            # mesmo prazo no relógio monotônico, calculado agora, com os dois relógios em sintonia
            self._mono = time.monotonic() + (at - time.time()) if at is not None else None
            self._handoff = handoff if at is not None else False
            self._rev += 1
        self._persist()

    def _persist(self) -> None:
        if self._store is None:
            return
        with self._lock:
            at, action, total, handoff = self._at, self._action, self._total, self._handoff
        try:
            if at is None:
                self._store.unlink(missing_ok=True)
            else:
                data = {"at": at, "action": action, "total": total, "handoff": handoff}
                boot = self._boot()
                if boot is not None:  # só no Windows; sem o campo, a checagem é pulada
                    data["boot"] = round(boot)
                _write_json(self._store, data)
        except OSError as exc:
            LOG.warning("não deu para salvar o estado: %s", exc)

    def _restore(self) -> None:
        """Recupera o agendamento gravado, se ele ainda puder estar valendo.

        Handoff feito (o timer do shutdown.exe vive no Windows e morre junto com ele): se o PC
        reiniciou depois que o estado foi gravado, o agendamento não existe mais e restaurá-lo faria o
        painel prometer um desligamento que não vem. Por isso o estado guarda a hora do boot e só é
        restaurado se ela não mudou.
        Contagem local (handoff=False): a contagem é do shutlite, então a checagem de boot é pulada e
        ela sobrevive a um reinício se o shutlite subir de novo (autostart); só o prazo vale (vencido
        com o shutlite/PC fora: descarta, nunca desliga atrasado).

        Limite conhecido (não verificado em hardware): com a Inicialização Rápida
        do Windows, desligar e ligar o PC pode manter o mesmo contador de tempo
        ligado, e a checagem não percebe o reinício. Reiniciar de verdade zera o
        contador e é detectado. Estados antigos, sem "boot", continuam aceitos.
        """
        if self._store is None:
            return
        saved = _read_json(self._store)
        if not saved:  # ausente, ilegível ou {}: não há o que restaurar; apaga o que sobrou
            self._persist()
            return
        now = time.time()
        reason = None
        restored: tuple[float, str, int, bool] | None = None
        try:  # o arquivo pode ter sido adulterado (int de 400 dígitos, 1e999...): nunca derruba o boot
            at, action, total = saved.get("at"), saved.get("action"), saved.get("total")
            handoff = saved.get("handoff", True)  # sem o campo (versão antiga): o Windows era o dono
            if not _is_number(at) or not math.isfinite(at) or action not in ACTIONS \
                    or not isinstance(handoff, bool):
                reason = "estado salvo inválido"
            elif at <= now:
                # Windows dono: já executou (silencioso). Contagem local: o prazo venceu com o
                # shutlite/PC fora; descartar com aviso, nunca desligar atrasado.
                reason = "já expirou" if handoff else (
                    "o prazo do agendamento passou enquanto o shutlite/PC estava fora; nada será desligado")
            elif at > now + MAX_SECONDS + 3600:
                reason = "horário longe demais no futuro"
            else:
                # A hora do boot só vale para quem tem o timer no Windows (handoff): ele morre no reinício.
                # A contagem local é do shutlite e sobrevive a um reinício se ele subir de novo (autostart).
                old = saved.get("boot") if handoff else None
                cur = self._boot() if _is_number(old) else None
                if _is_number(old) and not math.isfinite(old):
                    reason = "estado salvo inválido"
                elif _is_number(old) and cur is not None and abs(cur - old) > BOOT_TOLERANCE:
                    reason = "o PC foi reiniciado desde que foi agendado"
                else:
                    keep = total if isinstance(total, int) and not isinstance(total, bool) and total > 0 \
                        else int(at - now)
                    restored = (float(at), action, min(keep, MAX_SECONDS), handoff)
        except (ArithmeticError, ValueError, TypeError) as exc:
            reason, restored = f"estado salvo inválido ({type(exc).__name__})", None
        if restored is None:
            if reason != "já expirou":
                LOG.warning("agendamento salvo descartado: %s", reason or "estado salvo inválido")
            self._persist()  # estado vazio: apaga o arquivo velho
            return
        self._at, self._action, self._total, self._handoff = restored  # local: o timer retoma na 1ª volta
        self._mono = time.monotonic() + (self._at - now)  # prazo monotônico a partir de agora


def _is_number(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def boot_time() -> float | None:
    """Hora (epoch) em que o Windows iniciou, ou None se não der para saber.

    Calculada como agora - GetTickCount64. Só existe no Windows; nos outros
    sistemas devolve None e a checagem de reinício é pulada.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        get = ctypes.windll.kernel32.GetTickCount64  # type: ignore[attr-defined]
        get.restype = ctypes.c_uint64
        get.argtypes = []
        return time.time() - get() / 1000.0
    except (AttributeError, OSError):
        return None


def seconds_until(hh: int, mm: int, now: float | None = None) -> int:
    """Segundos até a próxima ocorrência de HH:MM no horário local."""
    now = time.time() if now is None else now
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    if target <= now:  # amanhã; mktime normaliza o dia e respeita horário de verão
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday + 1, hh, mm, 0, 0, 0, -1))
    return int(target - now)


# ---------------------------------------------------------------- PIN

def guard_key(addr: str) -> str:
    """Quem conta como "o mesmo cliente" para o limite de PIN.

    IPv4: o próprio endereço. IPv6: o prefixo /64, porque um dono de rede tem
    bilhões de endereços dentro dele e trocar de um para outro não deve zerar o
    contador. Loopback fica por endereço exato.
    """
    ip = _norm_ip(addr)
    if ip is None:
        return addr
    if ip.version == 6 and not ip.is_loopback:
        return str(ipaddress.IPv6Network((ip, 64), strict=False))
    return str(ip)


class PinGuard:
    """Freia tentativa e erro de PIN, em duas camadas.

    1. Por cliente (`guard_key`): `max_fails` erros seguidos travam o cliente por
       `lock_seconds`.
    2. Global: `global_max` erros em `global_window` s, somando TODOS os clientes
       que não são loopback, travam todos os não-loopback. O 1º bloqueio dura
       `global_lock` s e dobra a cada reincidência, até `global_cap` s; volta ao
       início depois de `global_cap` s sem novo bloqueio. Isso cobre quem troca de
       endereço (IPv6, vários aparelhos) para escapar da camada 1.

    Loopback (o dono do PC) não entra no contador global nem no bloqueio global,
    para que `--stop` e o painel local continuem funcionando durante um ataque
    vindo da rede. Ele segue sujeito à camada 1.
    """

    def __init__(self, max_fails: int = 5, lock_seconds: int = 300,
                 global_max: int = 20, global_window: int = 600,
                 global_lock: int = 300, global_cap: int = 3600,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._max = max_fails
        self._lock_seconds = lock_seconds
        self._gmax = global_max
        self._gwindow = global_window
        self._glock = global_lock
        self._gcap = global_cap
        self._clock = clock
        self._lock = threading.Lock()
        self._recs: dict[str, list] = {}  # chave -> [erros, travado_até]
        self._gfails: list[float] = []  # instantes dos erros recentes (não-loopback)
        self._glocked_until = 0.0
        self._glevel = 0  # quantos bloqueios globais seguidos (para o backoff)

    def blocked(self, addr: str) -> int:
        """Segundos que faltam para liberar este cliente (0 = liberado)."""
        now = self._clock()
        key = guard_key(addr)
        owner = is_loopback(addr)
        with self._lock:
            rec = self._recs.get(key)
            until = rec[1] if rec else 0.0
            if not owner:
                until = max(until, self._glocked_until)
        left = until - now
        return math.ceil(left) if left > 0 else 0

    def fail(self, addr: str) -> None:
        """Registra um PIN errado. Quem chama já checou `blocked` antes."""
        now = self._clock()
        key = guard_key(addr)
        owner = is_loopback(addr)
        client_lock = global_lock = 0
        with self._lock:
            if len(self._recs) > 1024:  # não cresce sem limite
                self._recs = {k: v for k, v in self._recs.items() if v[1] > now}
            rec = self._recs.setdefault(key, [0, 0.0])
            rec[0] += 1
            count = rec[0]
            if rec[0] >= self._max:
                rec[0] = 0
                rec[1] = now + self._lock_seconds
                client_lock = self._lock_seconds
            gcount = 0
            if not owner:
                self._gfails = [t for t in self._gfails if t > now - self._gwindow]
                self._gfails.append(now)
                gcount = len(self._gfails)
                if gcount >= self._gmax:
                    if now > self._glocked_until + self._gcap:
                        self._glevel = 0  # muito tempo sem ataque: recomeça do menor
                    self._glevel = min(self._glevel + 1, 16)
                    global_lock = min(self._glock * 2 ** (self._glevel - 1), self._gcap)
                    self._glocked_until = now + global_lock
                    self._gfails.clear()
        if owner:
            LOG.warning("PIN errado de %s (este PC): erro %s de %s", addr, count, self._max)
        else:
            LOG.warning("PIN errado de %s: erro %s de %s deste cliente, %s de %s na rede",
                        addr, count, self._max, gcount, self._gmax)
        if client_lock:
            LOG.warning("PIN errado %s vezes: %s travado por %ss", self._max, key, client_lock)
        if global_lock:
            LOG.warning("PIN errado %s vezes na rede: todos os clientes externos travados por %ss",
                        self._gmax, global_lock)

    def ok(self, addr: str) -> None:
        """PIN certo: zera o contador deste cliente. O global não zera de propósito."""
        with self._lock:
            self._recs.pop(guard_key(addr), None)


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


# Lista explícita: ipaddress.is_private também aceita 6to4 (2002::/16), Teredo (2001::/32),
# documentação (2001:db8::/32), 198.18.0.0/15, 240.0.0.0/4 etc., que não são "a rede de casa".
LOCAL_NETS_V4 = tuple(ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"))
LOCAL_NETS_V6 = tuple(ipaddress.ip_network(n) for n in ("::1/128", "fc00::/7", "fe80::/10"))


def _is_local_ip(ip: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    nets = LOCAL_NETS_V4 if ip.version == 4 else LOCAL_NETS_V6
    return any(ip in net for net in nets)


def is_local_net(addr: str) -> bool:
    """Loopback, RFC 1918, link-local e ULA (fc00::/7); ::ffff:a.b.c.d vale como IPv4."""
    ip = _norm_ip(addr)
    return ip is not None and _is_local_ip(ip)


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
        if not allow_public and not _is_local_ip(ip):  # mesma regra de is_local_net
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

    def _is_this_pc(self) -> bool:
        """A conexão veio deste próprio PC: loopback, ou (com --bind em um IP) uma conexão
        cuja origem é o IP do --bind e cujo destino no socket é esse mesmo IP. Um cliente
        da rede nunca tem o IP do servidor como origem; sem --bind só vale loopback."""
        client = _norm_ip(self.client_address[0])
        if client is None:
            return False
        if client.is_loopback:
            return True
        bind = _norm_ip(self._cfg.bind) if self._cfg.bind else None
        dest = _norm_ip(self.request.getsockname()[0])
        return bind is not None and client == bind and client == dest

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

        ip = self.client_address[0]
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
            at = str(body.get("at", "")).strip()
            if AT_FORMAT.fullmatch(at):  # só HH:MM com dois dígitos ASCII; "7:5" e "7 : 5" não passam
                hh, mm = int(at[:2]), int(at[3:])
            else:
                hh = mm = -1
            if not (0 <= hh <= 23 and 0 <= mm <= 59) or action not in ACTIONS:
                self._json({"error": "Horário inválido. Use HH:MM, ex: 23:30."}, 400)
                return
            self._apply(lambda: self._state.schedule(seconds_until(hh, mm), action))
        elif route == "/api/cancel":
            self._apply(self._state.cancel)
        elif route == "/api/quit":
            if not self._is_this_pc():
                self._json({"error": "Só o próprio PC pode parar o servidor."}, 403)
                return
            # parada ordenada: um desligamento só local seria cancelado em silêncio; entrega ao Windows antes
            handoff = self._state.hand_off_pending()  # ≤ ~STOP_WAIT_SECONDS + SHUTDOWN_TIMEOUT
            self._json({"ok": True, **({"handoff": handoff} if handoff else {})})
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

    def server_close(self) -> None:
        state = getattr(self, "state", None)  # ainda não existe se o bind falhou
        if state is not None:
            state.stop()  # para a thread do timer junto com o servidor
        super().server_close()


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
    srv.pin_guard = PinGuard()
    srv.subs = set()
    srv.subs_lock = threading.Lock()
    srv.state = ShutdownState(runner, cfg.data_dir / "state.json", native_timer=cfg.windows_timer,
                              on_change=lambda snap: _broadcast(srv, snap))  # handoff/descarte avisam o painel
    srv.state.start()  # timer local; parado em server_close()
    return srv


# ---------------------------------------------------------------- comandos

def print_info(cfg: Config, show_pin: bool) -> None:
    print(f"[shutlite {__version__}] porta={cfg.port}" + ("  (simulação)" if cfg.dry_run else ""))
    for url in list_urls(cfg, cfg.port):
        print(f"  {url}")
    if show_pin:
        print(f"  PIN: {cfg.pin}")
    else:
        print(f"  PIN: abra o painel neste PC ou rode: {self_command()} --info")


def _self_url(cfg: Config, path: str) -> str:
    """Endereço pelo qual este PC alcança o próprio servidor."""
    host = cfg.bind
    if not host or host == "0.0.0.0":
        host = "127.0.0.1"
    elif host == "::":
        host = "::1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{cfg.port}{path}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Não segue redirecionamento: o shutlite nunca redireciona, então um 3xx é outro programa."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None  # o urllib então levanta HTTPError(3xx), que os chamadores tratam


def _local_opener() -> urllib.request.OpenerDirector:
    # sem proxy (é este PC) e sem seguir redirects (nada de GET cego para onde o outro programa mandar)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def _printable(text: str, limit: int = 200) -> str:
    """Texto de um servidor que não controlamos, seguro para imprimir no console: caracteres de
    controle (inclui ESC, \\n, \\t) viram espaço; formatação invisível (bidi, largura zero) e
    surrogates soltos (quebrariam o print) somem; no máximo `limit` caracteres."""
    out = []
    for ch in text:
        cat = unicodedata.category(ch)
        if cat == "Cc":
            out.append(" ")
        elif cat not in ("Cf", "Cs"):
            out.append(ch)
    clean = " ".join("".join(out).split())
    return clean if len(clean) <= limit else clean[:limit - 3] + "..."


def _http_error_text(exc: urllib.error.HTTPError) -> str:
    try:
        obj = json.loads(exc.read(4096))
        return _printable(str(obj.get("error", ""))) if isinstance(obj, dict) else ""
    except (OSError, ValueError, http.client.HTTPException):
        return ""


def stop_running(cfg: Config) -> int:
    req = urllib.request.Request(
        _self_url(cfg, "/api/quit"),
        data=json.dumps({"pin": cfg.pin}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:  # o servidor pode levar até ~8 s: entrega a contagem local ao Windows antes de responder
        with _local_opener().open(req, timeout=STOP_WAIT_SECONDS + SHUTDOWN_TIMEOUT + 3) as res:
            body = res.read(4096)
    except urllib.error.HTTPError as exc:  # o servidor respondeu: está rodando, mas recusou
        if exc.code == 401:
            print(f"O PIN deste computador é diferente do PIN do servidor na porta {cfg.port}. "
                  "Use --pin com o PIN certo (e a mesma --port/--data-dir de quando ele foi iniciado).")
        elif exc.code == 429:
            wait = exc.headers.get("Retry-After", "")
            print("O shutlite bloqueou novas tentativas de PIN deste computador"
                  + (f". Espere {wait} s e tente de novo." if wait.isascii() and wait.isdigit() and len(wait) <= 6
                     else ". Espere alguns minutos."))
        elif 300 <= exc.code < 400:  # o shutlite nunca redireciona: é outro programa na porta
            print(f"Outro programa está usando a porta {cfg.port}; não parece o shutlite.")
        else:
            detail = _http_error_text(exc)
            print(f"O servidor recusou o pedido (HTTP {exc.code})" + (f": {detail}" if detail else "."))
        return 1
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ConnectionRefusedError):
            print(f"shutlite não está rodando na porta {cfg.port}.")
        else:
            print(f"Não consegui falar com o shutlite na porta {cfg.port}: {reason}")
        return 1
    print("shutlite parado.")
    try:  # a contagem local pendente foi entregue ao Windows (ou falhou): quem parou precisa saber
        handoff = json.loads(body).get("handoff")
    except (ValueError, AttributeError, RecursionError):
        handoff = None
    if isinstance(handoff, dict):
        at = _printable(str(handoff["at"]), 20) if handoff.get("at") else ""
        if handoff.get("ok"):
            print(f"Há um desligamento às {at}; entreguei ao Windows (o aviso dele vai aparecer).")
        elif handoff.get("reason") == "busy":  # não dá para saber: nunca afirmar que não vai acontecer
            print("ATENÇÃO: há um desligamento agendado" + (f" às {at}" if at else "")
                  + " e um comando ao Windows ainda estava em andamento; não consegui confirmar a entrega."
                  " Ele PODE acontecer. Para garantir que não aconteça, rode: shutdown /a")
        else:
            print("ATENÇÃO: havia um desligamento agendado" + (f" às {at}" if at else "")
                  + ", mas NÃO consegui entregá-lo ao Windows; sem o shutlite rodando ele NÃO vai acontecer.")
    return 0


def check_running(cfg: Config) -> int:
    """--check: 0 se um shutlite responde na porta da config, 1 se não."""
    log = cfg.data_dir / "shutlite.log"
    try:
        with _local_opener().open(_self_url(cfg, "/api/status"), timeout=3) as res:
            data = json.loads(res.read(65536))
    except (urllib.error.HTTPError, ValueError):  # respondeu, mas não é a nossa API (erro HTTP ou não-JSON, ex. HTML)
        print(f"Outro programa está usando a porta {cfg.port}; não parece o shutlite. "
              f"Veja também: {log}")
        return 1
    except (urllib.error.URLError, OSError):
        print(f"shutlite não respondeu na porta {cfg.port}. Veja o motivo em: {log}")
        return 1
    if not isinstance(data, dict) or "scheduled" not in data:
        print(f"Outro programa está usando a porta {cfg.port}; não parece o shutlite. "
              f"Veja também: {log}")
        return 1
    print(f"shutlite rodando na porta {cfg.port}.")
    return 0


def self_command() -> str:
    """Como o usuário chama o shutlite, para as dicas impressas."""
    return "shutlite.exe" if FROZEN else "python server.py"


def autostart_script() -> str:
    """Conteúdo do .cmd da pasta Inicializar. No shutlite.exe abre minimizado; no Python, sem janela."""
    if FROZEN:
        cmd = f'start "" /min "{Path(sys.executable).resolve()}" --no-open'
    else:
        pyw = Path(sys.executable).with_name("pythonw.exe")
        exe = pyw if pyw.exists() else Path(sys.executable)
        cmd = f'start "" "{exe}" "{Path(__file__).resolve()}"'
    return f"@echo off\r\nchcp 65001 >nul\r\n{cmd}\r\n"


def should_open_browser(args: argparse.Namespace, argv: list[str]) -> bool:
    """--open abre o painel; o shutlite.exe aberto com dois cliques (sem argumentos) também abre."""
    if args.no_open:
        return False
    return args.open or (FROZEN and not argv)


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
    link.write_text(autostart_script(), encoding="utf-8")
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
        msg = f"shutlite: {exc}"
        if sys.stderr is None:  # pythonw (início automático): sem console, a mensagem se perderia
            try:
                folder = Path(args.data_dir) if args.data_dir else default_data_dir()
                folder.mkdir(parents=True, exist_ok=True)
                with open(folder / "shutlite.log", "a", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
            except OSError:
                pass
        raise SystemExit(msg) from None
    if args.stop:
        return stop_running(cfg)
    if args.check:
        return check_running(cfg)
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
    print("Feche esta janela ou aperte Ctrl+C para parar." if FROZEN else "Ctrl+C para parar.")
    LOG.info("rodando na porta %s", cfg.port)
    if should_open_browser(args, sys.argv[1:] if argv is None else argv):
        webbrowser.open(f"http://localhost:{srv.server_address[1]}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nparado.")
    finally:
        srv.server_close()
    return 0


def _run_exe() -> int:
    """No shutlite.exe aberto com dois cliques, um erro na partida fecharia a janela antes de ser lido."""
    try:
        code = main()
    except SystemExit as exc:
        if exc.code is not None and not isinstance(exc.code, int):
            print(exc.code)
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    if code and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\nAperte Enter para fechar.")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(_run_exe() if FROZEN else main())
