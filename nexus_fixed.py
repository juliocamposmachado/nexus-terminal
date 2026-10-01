#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NEXUS TERMINAL - WHITE RAT
v5.1.0

Smart Router + Real PTY + Gemini Multi-Agent + Key Pool + Autocomplete.

Principais melhorias sobre a v4:
- NUNCA grava API keys no código: usa ~/.config/nexus/config.json
  ou variáveis NEXUS_GEMINI_KEY_1..NEXUS_GEMINI_KEY_N.
- Pool de chaves thread-safe, ilimitado, com rotação sequencial, cooldown e failover.
- Respeita Retry-After e diferencia 429, 401/403, 5xx e erros 4xx.
- Limite de chamadas por tarefa e orçamento por complexidade.
- Router local mais amplo: tarefas simples não consomem API.
- --auto/--fast podem aparecer em qualquer ordem.
- PTY real persistente, com cwd sincronizado e captura de exit code.
- Comandos perigosos exigem confirmação mesmo em modo automático.
- Autocomplete de /comandos, executáveis e caminhos.
- Configuração segura com permissões 0600.
- Histórico protegido.
- Self-test sem consumir API.
- Logs opcionais sem registrar chaves.

Instalação:
  python3 -m pip install --user pexpect requests

Primeiro uso:
  python3 nexus.py --setup

Executar:
  python3 nexus.py
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import queue
import readline
import re
import shutil
import signal
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import pexpect
except ImportError:
    print("Dependência ausente: pexpect")
    print("Instale: python3 -m pip install --user pexpect requests")
    raise SystemExit(1)

try:
    import requests
except ImportError:
    print("Dependência ausente: requests")
    print("Instale: python3 -m pip install --user pexpect requests")
    raise SystemExit(1)


APP_NAME = "NEXUS TERMINAL"
APP_VERSION = "5.0.0-SMART-ROUTER"
CONFIG_DIR = Path.home() / ".config" / "nexus"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.6-flash"

# Não coloque chaves reais aqui.
API_KEY_ENV_PREFIX = "NEXUS_GEMINI_KEY_"

DEFAULT_CONFIG: dict[str, Any] = {
    "model": DEFAULT_MODEL,
    "temperature": 0.1,
    "max_output_tokens": 700,
    "api_timeout": 60,
    "terminal_timeout": 120,
    "max_agent_cycles": 3,
    "max_api_calls_per_task": 12,
    "medium_api_budget": 5,
    "complex_api_budget": 8,
    "api_min_interval": 8,
    "execution_cooldown": 1,
    "rate_limit_backoff": 60,
    "auth_cooldown": 300,
    "error_cooldown": 15,
    # None = tentar todas as chaves disponíveis, sem limite artificial.
    "max_key_failover": None,
    "max_retries": 1,
    "backoff_multiplier": 2,
    "auto_execute": False,
    "dangerous_always_confirm": True,
    "max_output_chars": 6000,
    "request_max_chars": 12000,
    "log_enabled": False,
    "keys": [],
}

AGENTS = {
    "interpretacao": "INTERPRETAÇÃO",
    "planejamento": "PLANEJAMENTO",
    "decisao": "DECISÃO",
    "execucao": "EXECUÇÃO",
    "validacao": "VALIDAÇÃO",
    "conclusao": "CONCLUSÃO",
}

PROMPTS = {
    "interpretacao": """
Você é o agente INTERPRETAÇÃO do NEXUS TERMINAL.
Transforme o pedido do usuário em uma tarefa Linux objetiva.
Não execute nada, não invente dados e não crie tarefas extras.
Retorne SOMENTE JSON válido:
{"task":"...","requirements":[]}
""",
    "planejamento": """
Você é o agente PLANEJAMENTO do NEXUS TERMINAL.
Determine a próxima ação necessária usando o estado REAL do terminal.
Uma próxima etapa por vez. Não repita ações já concluídas.
Retorne SOMENTE JSON válido:
{"next_step":"...","reason":"..."}
""",
    "decisao": """
Você é o agente DECISÃO do NEXUS TERMINAL.
Determine se é necessário executar Linux agora.
Se a informação já estiver disponível, execute=false.
Retorne SOMENTE JSON válido:
{"execute":true,"reason":"..."}
""",
    "execucao": """
Você é o agente EXECUÇÃO do NEXUS TERMINAL.
Converta a próxima etapa em UM ÚNICO comando Linux.
O comando será executado em um PTY Linux REAL.
Prefira comandos de leitura para investigação.
Não use markdown.
Não use comandos destrutivos sem necessidade.
Nunca produza rm -rf /, mkfs, wipefs, dd em dispositivo, parted,
fdisk, shutdown, reboot ou poweroff sem solicitação explícita e inequívoca.
Retorne SOMENTE JSON válido:
{"command":"..."}
""",
    "validacao": """
Você é o agente VALIDAÇÃO do NEXUS TERMINAL.
Analise SOMENTE o resultado REAL fornecido pelo terminal.
exit code 0 não garante sucesso.
Retorne SOMENTE JSON válido:
{"success":true,"reason":"..."}
""",
    "conclusao": """
Você é o agente CONCLUSÃO do NEXUS TERMINAL.
Resuma o resultado final em uma frase curta, sem inventar dados.
Retorne SOMENTE JSON válido:
{"done":true,"message":"..."}
""",
}

ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\)|[@-Z\\-_])",
    re.VERBOSE,
)

DANGEROUS_PATTERNS = [
    r"\brm\s+-rf\s+/(?:\s|$)",
    r"\brm\s+-rf\s+/\*",
    r"\brm\s+.*\s+/(?:\s|$)",
    r"\bmkfs(?:\.|\s)",
    r"\bwipefs\b",
    r"\bparted\b",
    r"\bfdisk\b",
    r"\bcfdisk\b",
    r"\bsgdisk\b",
    r"\bdd\s+.*\bof=/dev/",
    r"(?:>|>>|2>|2>>|&>)\s*/dev/(?:sd[a-z]|nvme\d+n\d+)",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bpoweroff\b",
    r"\bsystemctl\s+(?:reboot|poweroff|halt)\b",
    r":\(\)\s*\{",
    r"\bchmod\s+-r\s+777\s+/$",
    r"\bchown\s+-r\s+.*\s+/$",
]


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x1b[200~", "").replace("\x1b[201~", "")
    return ANSI_ESCAPE_RE.sub("", text).strip()


def clean_command(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x00", "").replace("\x7f", "").strip()
    text = re.sub(r"^```(?:bash|sh|shell)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text).strip()
    text = re.sub(r"^\s*command\s*:\s*", "", text, flags=re.I)
    return text.strip()


def normalize_input(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip().lower())


def as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "sim", "s", "y"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def is_dangerous(command: str) -> bool:
    normalized = re.sub(r"\s+", " ", command.lower()).strip()
    return any(re.search(pattern, normalized) for pattern in DANGEROUS_PATTERNS)


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)


def load_config() -> dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                config.update(data)
        except Exception as exc:
            print(f"[NEXUS] Aviso: config.json inválido: {exc}")
    # Normaliza campos.
    if not isinstance(config.get("keys"), list):
        config["keys"] = []
    return config


def save_config(config: dict[str, Any]) -> None:
    # Remove chaves inválidas e normaliza strings.
    data = copy.deepcopy(config)
    data["keys"] = [str(k).strip() for k in data.get("keys", []) if str(k).strip()]
    atomic_write(CONFIG_FILE, json.dumps(data, indent=2, ensure_ascii=False))


def parse_api_keys_block(value: str) -> list[str]:
    """Extrai chaves de bloco simples ou no formato de lista JSON."""
    if not value:
        return []
    text = str(value).strip()

    # Aceita exatamente este formato, inclusive indentação e vírgula final:
    # "chave-1",
    # "chave-2",
    # "chave-3",
    quoted = re.findall(r'"([^"\r\n]*)"', text)
    if quoted:
        return [item.strip() for item in quoted if item.strip()]

    # Também mantém compatibilidade com uma chave por linha ou separadores.
    parts = re.split(r"[\r\n,;]+", text)
    result: list[str] = []
    for item in parts:
        item = item.strip().strip('"\'')
        if item:
            result.append(item)
    return result


def get_api_keys(config: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    configured = config.get("keys", [])
    if isinstance(configured, list):
        for item in configured:
            keys.extend(parse_api_keys_block(str(item)))

    # Lê NEXUS_GEMINI_KEY_1, NEXUS_GEMINI_KEY_2, ... sem limite fixo.
    env_items: list[tuple[int, str]] = []
    pattern = re.compile(r"^" + re.escape(API_KEY_ENV_PREFIX) + r"(\d+)$")
    for name, value in os.environ.items():
        match = pattern.match(name)
        if match and value.strip():
            env_items.append((int(match.group(1)), value.strip()))
    for _, value in sorted(env_items):
        keys.extend(parse_api_keys_block(value))

    # Também aceita um bloco inteiro em NEXUS_GEMINI_KEYS.
    keys.extend(parse_api_keys_block(os.environ.get("NEXUS_GEMINI_KEYS", "")))
    # Remove duplicadas preservando a ordem configurada.
    return list(dict.fromkeys(keys))


def validate_keys(config: dict[str, Any]) -> list[str]:
    keys = get_api_keys(config)
    if not keys:
        return ["Nenhuma chave Gemini configurada."]
    return []


class RateLimitError(Exception):
    pass


class AgentError(Exception):
    pass


@dataclass
class KeyState:
    cooldown_until: float = 0.0
    requests: int = 0
    errors: int = 0
    rate_limits: int = 0
    auth_errors: int = 0


class KeyPool:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.lock = threading.RLock()
        self.index = 0
        self.states: list[KeyState] = []
        self.failed_this_task: set[int] = set()

    def begin_task(self) -> None:
        """Limpa falhas temporárias para uma nova tarefa do usuário."""
        with self.lock:
            self.failed_this_task.clear()

    @property
    def keys(self) -> list[str]:
        keys = get_api_keys(self.config)
        while len(self.states) < len(keys):
            self.states.append(KeyState())
        # Se o pool foi reduzido, estados antigos ficam preservados para quando
        # chaves forem adicionadas novamente, sem afetar a ordem atual.
        return keys

    def available_indices(self, excluded: set[int] | None = None) -> list[int]:
        excluded = set(excluded or set()) | self.failed_this_task
        now = time.monotonic()
        keys = self.keys
        return [i for i in range(len(keys)) if i not in excluded and self.states[i].cooldown_until <= now]

    def next_key(self, excluded: set[int] | None = None) -> tuple[int, str]:
        with self.lock:
            keys = self.keys
            excluded = set(excluded or set()) | self.failed_this_task
            available = self.available_indices(excluded)
            if not available:
                if not keys:
                    raise AgentError("Nenhuma chave Gemini configurada.")
                now = time.monotonic()
                future = [self.states[i].cooldown_until for i in range(len(keys)) if i not in excluded and self.states[i].cooldown_until > now]
                if future:
                    wait = max(1, int(min(future) - now + 0.999))
                    raise RateLimitError(f"Todas as chaves disponíveis estão em cooldown. Aguarde {wait}s.")
                if len(self.failed_this_task) >= len(keys):
                    raise RateLimitError("Todas as chaves falharam nesta tarefa; execução encerrada para evitar loop.")
                raise AgentError("Nenhuma chave disponível no pool.")
            total = len(keys)
            for offset in range(total):
                candidate = (self.index + offset) % total
                if candidate in available:
                    self.index = (candidate + 1) % total
                    self.states[candidate].requests += 1
                    return candidate, keys[candidate]
            raise AgentError("Falha ao selecionar chave.")

    def mark_429(self, index: int, seconds: Optional[int] = None) -> None:
        with self.lock:
            seconds = seconds if seconds is not None else int(self.config.get("rate_limit_backoff", 60))
            self.states[index].rate_limits += 1
            self.states[index].cooldown_until = time.monotonic() + max(1, seconds)

    def mark_error(self, index: int, cooldown: Optional[int] = None) -> None:
        with self.lock:
            cooldown = int(cooldown if cooldown is not None else self.config.get("error_cooldown", 15))
            self.states[index].errors += 1
            self.states[index].cooldown_until = time.monotonic() + max(1, cooldown)

    def mark_auth_error(self, index: int) -> None:
        with self.lock:
            self.states[index].auth_errors += 1
            self.states[index].cooldown_until = time.monotonic() + int(self.config.get("auth_cooldown", 300))

    def mark_failed_this_task(self, index: int) -> None:
        with self.lock:
            self.failed_this_task.add(index)

    def reset(self) -> None:
        with self.lock:
            for state in self.states:
                state.cooldown_until = 0.0

    def status(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        keys = self.keys
        result = []
        for i in range(len(keys)):
            s = self.states[i]
            if s.cooldown_until > now:
                state = f"COOLDOWN {int(s.cooldown_until - now)}s"
            else:
                state = "DISPONÍVEL"
            result.append({"index": i + 1, "state": state, "requests": s.requests, "429": s.rate_limits, "errors": s.errors, "auth": s.auth_errors})
        return result


class RateLimiter:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.lock = threading.Lock()
        self.last_request = 0.0

    def wait(self) -> None:
        with self.lock:
            minimum = max(0.0, float(self.config.get("api_min_interval", 8)))
            remaining = minimum - (time.monotonic() - self.last_request)
            if remaining > 0:
                time.sleep(remaining)
            self.last_request = time.monotonic()

    @staticmethod
    def backoff(seconds: float, reason: str) -> None:
        seconds = max(0.0, seconds)
        if seconds <= 0:
            return
        print(f"[NEXUS] {reason}: aguardando {seconds:.1f}s")
        time.sleep(seconds)


class GeminiClient:
    def __init__(self, config: dict[str, Any], limiter: RateLimiter, key_pool: KeyPool):
        self.config = config
        self.limiter = limiter
        self.key_pool = key_pool
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": f"NEXUS-TERMINAL/{APP_VERSION}"})

    def ask(self, agent_id: str, state: dict[str, Any]) -> str:
        if agent_id not in AGENTS:
            raise AgentError(f"Agente desconhecido: {agent_id}")
        keys = self.key_pool.keys
        if not keys:
            raise AgentError("Nenhuma API key configurada. Use /setup ou NEXUS_GEMINI_KEY_1..N.")
        request_state = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        max_chars = int(self.config.get("request_max_chars", 12000))
        request_state = request_state[:max_chars]
        model = str(self.config.get("model", DEFAULT_MODEL)).strip()
        url = GEMINI_URL.format(model=model)
        payload = {
            "system_instruction": {"parts": [{"text": PROMPTS[agent_id]}]},
            "contents": [{"role": "user", "parts": [{"text": request_state}]}],
            "generationConfig": {
                "temperature": float(self.config.get("temperature", 0.1)),
                "maxOutputTokens": int(self.config.get("max_output_tokens", 700)),
                "candidateCount": 1,
                "responseMimeType": "application/json",
            },
        }
        # O failover é sempre ilimitado: percorre todas as chaves carregadas,
        # uma por vez e em rotação sequencial. O campo antigo max_key_failover
        # é ignorado para não limitar configurações criadas em versões antigas.
        max_failover = len(keys)
        max_retries = max(0, int(self.config.get("max_retries", 1)))
        used: set[int] = set()

        for _ in range(max_failover):
            key_index, key = self.key_pool.next_key(used)
            used.add(key_index)
            print(f"[NEXUS] {AGENTS[agent_id]} → chave #{key_index + 1} → Gemini")
            for retry in range(max_retries + 1):
                self.limiter.wait()
                try:
                    response = self.session.post(
                        url,
                        headers={"Content-Type": "application/json", "x-goog-api-key": key},
                        json=payload,
                        timeout=int(self.config.get("api_timeout", 60)),
                    )
                except requests.exceptions.Timeout:
                    self.key_pool.mark_error(key_index)
                    if retry < max_retries:
                        self.limiter.backoff(2 ** retry, "Timeout")
                        continue
                    self.key_pool.mark_failed_this_task(key_index)
                    break
                except requests.exceptions.RequestException as exc:
                    self.key_pool.mark_error(key_index)
                    if retry < max_retries:
                        self.limiter.backoff(2 ** retry, "Erro de rede")
                        continue
                    self.key_pool.mark_failed_this_task(key_index)
                    break

                if response.status_code == 429:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        seconds = int(retry_after) if retry_after else None
                    except ValueError:
                        seconds = None
                    self.key_pool.mark_429(key_index, seconds)
                    self.key_pool.mark_failed_this_task(key_index)
                    print(f"[NEXUS] HTTP 429 na chave #{key_index + 1}; failover.")
                    break

                if response.status_code in (401, 403):
                    self.key_pool.mark_auth_error(key_index)
                    self.key_pool.mark_failed_this_task(key_index)
                    print(f"[NEXUS] Chave #{key_index + 1} rejeitada HTTP {response.status_code}; isolada temporariamente.")
                    break

                if 500 <= response.status_code <= 599:
                    self.key_pool.mark_error(key_index)
                    if retry < max_retries:
                        self.limiter.backoff(2 ** retry, f"Servidor HTTP {response.status_code}")
                        continue
                    self.key_pool.mark_failed_this_task(key_index)
                    break

                if response.status_code >= 400:
                    body = response.text[:2500]
                    raise AgentError(f"Gemini HTTP {response.status_code}:\n{body}")

                try:
                    data = response.json()
                except ValueError as exc:
                    raise AgentError(f"Resposta da API não é JSON: {exc}") from exc

                candidates = data.get("candidates") or []
                if not candidates:
                    feedback = data.get("promptFeedback")
                    raise AgentError(f"Gemini retornou candidates vazio. Feedback: {feedback}")
                parts = (candidates[0].get("content") or {}).get("parts") or []
                result = "".join(str(p.get("text", "")) for p in parts if isinstance(p, dict)).strip()
                if not result:
                    raise AgentError("Gemini retornou resposta vazia.")
                return result

        raise RateLimitError("O pool não conseguiu atender a chamada após failover.")


def extract_json(text: str) -> dict[str, Any]:
    if not text:
        raise AgentError("Resposta vazia do agente.")
    raw = str(text).strip()
    candidates = [raw]
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    candidates.append(cleaned)
    decoder = json.JSONDecoder()
    for candidate in candidates:
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    for i, char in enumerate(raw):
        if char != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(raw[i:])
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    raise AgentError("O agente não retornou JSON válido:\n" + raw[:2500])


GREETING_RESPONSES = {
    "oi": "Olá! Estou operacional. Como posso ajudar?",
    "olá": "Olá! Estou operacional. Como posso ajudar?",
    "ola": "Olá! Estou operacional. Como posso ajudar?",
    "oii": "Olá! Estou operacional. Como posso ajudar?",
    "oie": "Olá! Estou operacional. Como posso ajudar?",
    "hello": "Hello! NEXUS operacional.",
    "hi": "Olá! NEXUS operacional.",
    "bom dia": "Bom dia! NEXUS operacional. Como posso ajudar?",
    "boa tarde": "Boa tarde! NEXUS operacional. Como posso ajudar?",
    "boa noite": "Boa noite! NEXUS operacional. Como posso ajudar?",
}

DIRECT_COMMANDS = {
    "onde estou": "pwd",
    "qual meu diretório": "pwd",
    "qual é meu diretório": "pwd",
    "qual e meu diretorio": "pwd",
    "mostre meu diretório": "pwd",
    "liste os arquivos": "ls -lah",
    "listar arquivos": "ls -lah",
    "mostre os arquivos": "ls -lah",
    "memória": "free -h",
    "memoria": "free -h",
    "mostre a memória": "free -h",
    "mostre a memoria": "free -h",
    "ram": "free -h",
    "disco": "df -h /",
    "espaço em disco": "df -h /",
    "espaco em disco": "df -h /",
    "kernel": "uname -a",
    "qual o kernel": "uname -a",
    "qual é o kernel": "uname -a",
    "qual e o kernel": "uname -a",
    "hostname": "hostname",
    "qual meu usuário": "whoami",
    "qual é meu usuário": "whoami",
    "qual e meu usuario": "whoami",
    "usuário": "whoami",
    "usuario": "whoami",
    "uptime": "uptime",
    "processador": "lscpu",
    "cpu": "lscpu",
    "temperatura": "sensors",
}

CHANGE_PREFIXES = (
    "instale ", "instalar ", "configure ", "configurar ", "crie ", "criar ",
    "remova ", "remover ", "desinstale ", "desinstalar ", "adicione ", "adicionar ",
    "corrija ", "corrigir ", "execute ", "executar ", "mate ", "inicie ", "iniciar ",
    "pare ", "parar ", "edite ", "editar ", "monte ", "montar ",
)

COMPLEX_TERMS = (
    "diagnóstico completo", "diagnostico completo", "analise tudo", "análise tudo",
    "analise completa", "análise completa", "investigue", "investigar", "gargalo",
    "problema do sistema", "problemas do sistema", "otimize meu sistema", "otimizar meu sistema",
    "corrija meu sistema", "verifique tudo", "diagnóstico geral", "diagnostico geral",
)


def local_route(request: str) -> dict[str, Any]:
    normalized = normalize_input(request)
    if normalized in GREETING_RESPONSES:
        return {"type": "conversation", "complexity": "direct", "api": 0, "response": GREETING_RESPONSES[normalized]}
    if normalized in {"obrigado", "obrigada", "valeu", "vlw", "thanks"}:
        return {"type": "conversation", "complexity": "direct", "api": 0, "response": "Disponha. NEXUS continua operacional."}
    if normalized in {"quem é você", "quem e voce", "o que é você", "o que e voce"}:
        return {"type": "conversation", "complexity": "direct", "api": 0, "response": "Sou o NEXUS TERMINAL, assistente Linux com PTY real e pipeline de agentes."}
    if normalized in DIRECT_COMMANDS:
        return {"type": "direct", "complexity": "direct", "api": 0, "command": DIRECT_COMMANDS[normalized]}
    if any(word in normalized for word in COMPLEX_TERMS):
        return {"type": "technical", "complexity": "complex", "api": 8}
    if normalized.startswith(CHANGE_PREFIXES):
        return {"type": "technical", "complexity": "medium", "api": 5}
    return {"type": "technical", "complexity": "medium", "api": 5}


class RealPTY:
    def __init__(self) -> None:
        self.shell = os.environ.get("SHELL") or "/bin/bash"
        if not os.path.exists(self.shell):
            self.shell = "/bin/bash"
        self.child: Optional[pexpect.spawn] = None
        self.output_queue: queue.Queue[bytes] = queue.Queue()
        self.lock = threading.RLock()
        self.alive = False
        self.cwd = str(Path.home())

    def start(self) -> None:
        env = os.environ.copy()
        env.update({"TERM": "xterm-256color", "NEXUS_TERMINAL": "1", "INPUTRC": "/dev/null"})
        shell_name = Path(self.shell).name
        args = ["--noprofile", "--norc", "-i"] if shell_name == "bash" else ["-i"]
        self.child = pexpect.spawn(self.shell, args, env=env, encoding=None, echo=True, dimensions=(40, 120), timeout=0.1)
        self.alive = True
        threading.Thread(target=self._reader, daemon=True, name="nexus-pty-reader").start()
        time.sleep(0.25)
        if shell_name == "bash":
            self.write("bind 'set enable-bracketed-paste off'\n")
            self.write("printf '\\033[?2004l'\n")
            self.write("PS1='\\u@\\h:\\w\\$ '\n")
        time.sleep(0.2)
        self.drain()
        self.refresh_cwd()

    def _reader(self) -> None:
        while self.alive and self.child is not None:
            try:
                data = self.child.read_nonblocking(size=4096, timeout=0.1)
                if data:
                    self.output_queue.put(data)
            except pexpect.TIMEOUT:
                continue
            except (pexpect.EOF, OSError):
                break
            except Exception as exc:
                self.output_queue.put(f"\n[NEXUS PTY ERROR] {exc}\n".encode())
                break
        self.alive = False

    def write(self, data: str | bytes) -> bool:
        if not self.alive or self.child is None:
            return False
        try:
            with self.lock:
                self.child.write(data)
            return True
        except Exception:
            return False

    def drain(self) -> bytes:
        chunks = []
        while True:
            try:
                chunks.append(self.output_queue.get_nowait())
            except queue.Empty:
                break
        return b"".join(chunks)

    def run_command(self, command: str, timeout: int = 120) -> tuple[str, Optional[int]]:
        command = clean_command(command)
        if not command:
            return "", None
        marker = f"__NEXUS_EXIT_{uuid.uuid4().hex}__"
        payload = f"{command}\nprintf '\\n{marker}:%s\\n' $?\n"
        self.drain()
        if not self.write(payload):
            return "", None
        captured = b""
        deadline = time.monotonic() + max(1, timeout)
        pattern = re.compile(rb"\n" + re.escape(marker.encode()) + rb":(-?\d+)\r?\n")
        while time.monotonic() < deadline:
            captured += self.drain()
            match = pattern.search(captured)
            if match:
                code = int(match.group(1))
                output = captured[:match.start()]
                self.refresh_cwd()
                return self.clean_output(output), code
            time.sleep(0.03)
        self.write("\x03")
        return self.clean_output(captured), None

    def refresh_cwd(self) -> None:
        if not self.alive:
            return
        marker = f"__NEXUS_PWD_{uuid.uuid4().hex}__"
        self.drain()
        self.write(f"pwd\nprintf '\\n{marker}\\n'\n")
        captured = b""
        deadline = time.monotonic() + 3
        pattern = re.compile(rb"\r?\n(.*?)\r?\n" + re.escape(marker.encode()))
        while time.monotonic() < deadline:
            captured += self.drain()
            match = pattern.search(captured)
            if match:
                value = match.group(1).decode("utf-8", errors="replace").strip()
                if value.startswith("/"):
                    self.cwd = value
                return
            time.sleep(0.03)

    @staticmethod
    def clean_output(data: bytes) -> str:
        return clean_text(data.decode("utf-8", errors="replace")) if data else ""

    def interrupt(self) -> None:
        if self.alive:
            self.write("\x03")

    def stop(self) -> None:
        self.alive = False
        try:
            if self.child is not None:
                self.child.close(force=True)
        except Exception:
            pass


class NexusCompleter:
    COMMANDS = [
        "/nexus", "/nexus --auto", "/nexus --fast", "/nexus --auto --fast",
        "/nexus stop", "/status", "/config", "/keys", "/reset-limits",
        "/setup", "/self-test", "/help", "/clear", "/exit",
    ]

    def __init__(self, app: "NexusApp"):
        self.app = app
        self.command_cache: Optional[list[str]] = None

    def build_command_cache(self) -> list[str]:
        if self.command_cache is not None:
            return self.command_cache
        commands: set[str] = set()
        for directory in os.environ.get("PATH", "").split(os.pathsep):
            if not directory:
                continue
            try:
                for item in Path(directory).iterdir():
                    if item.is_file() and os.access(item, os.X_OK):
                        commands.add(item.name)
            except (OSError, PermissionError):
                continue
        self.command_cache = sorted(commands)
        return self.command_cache

    def complete(self, text: str, state: int) -> Optional[str]:
        buffer = readline.get_line_buffer()
        if buffer.startswith("/"):
            options = [x for x in self.COMMANDS if x.startswith(buffer)]
        elif len(buffer[:readline.get_endidx()].strip().split()) <= 1:
            options = [x for x in self.build_command_cache() if x.startswith(text)]
        else:
            return self.complete_path(text, state)
        return options[state] if state < len(options) else None

    def complete_path(self, text: str, state: int) -> Optional[str]:
        if not text:
            text = ""
        expanded = os.path.expanduser(text)
        path = Path(expanded)
        if text.endswith(os.sep):
            directory, prefix = path, ""
        else:
            directory, prefix = path.parent, path.name
        try:
            items = sorted(directory.iterdir(), key=lambda p: p.name.lower())
        except (OSError, PermissionError):
            return None
        options = []
        for item in items:
            if not item.name.startswith(prefix):
                continue
            if text in ("", "."):
                value = item.name
            else:
                value = str(path.parent / item.name)
            if item.is_dir():
                value += os.sep
            options.append(value)
        return options[state] if state < len(options) else None


def setup_readline(app: "NexusApp") -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        readline.set_history_length(1000)
        if HISTORY_FILE.exists():
            readline.read_history_file(str(HISTORY_FILE))
    except Exception:
        pass
    readline.set_completer(NexusCompleter(app).complete)
    readline.parse_and_bind("tab: complete")
    readline.parse_and_bind("set show-all-if-ambiguous on")
    readline.set_completer_delims(" \t\n;")


def save_history() -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        readline.write_history_file(str(HISTORY_FILE))
        os.chmod(HISTORY_FILE, 0o600)
    except Exception:
        pass


class NexusApp:
    def __init__(self) -> None:
        self.config = load_config()
        self.pty = RealPTY()
        self.key_pool = KeyPool(self.config)
        self.limiter = RateLimiter(self.config)
        self.ai = GeminiClient(self.config, self.limiter, self.key_pool)
        self.running = True
        self.stop_event = threading.Event()
        self.task_id = 0
        self.current_task_calls = 0
        self.current_budget = 0
        self.last_output = ""
        self.last_exit: Optional[int] = None
        self.auto_mode = False

    def terminal_state(self) -> dict[str, Any]:
        limit = int(self.config.get("max_output_chars", 6000))
        return {"cwd": self.pty.cwd, "shell": self.pty.shell, "last_exit_code": self.last_exit, "last_output": self.last_output[-limit:]}

    def can_call_api(self) -> bool:
        return self.current_task_calls < self.current_budget

    def call_agent(self, agent_id: str, state: dict[str, Any]) -> dict[str, Any]:
        if not self.can_call_api():
            raise AgentError("Orçamento de API da tarefa atingido.")
        self.current_task_calls += 1
        print(f"\n┌──────────────────────────────────────┐\n│ {AGENTS[agent_id]:<36} │\n└──────────────────────────────────────┘")
        print(f"[NEXUS] API {self.current_task_calls}/{self.current_budget}")
        return extract_json(self.ai.ask(agent_id, state))

    def execute_command(self, command: str) -> tuple[str, Optional[int], bool]:
        command = clean_command(command)
        if not command:
            return "", None, False
        print(f"\n\033[1;36mWHITE RAT → \033[0m{command}")
        if is_dangerous(command) and self.config.get("dangerous_always_confirm", True):
            print("\033[1;31mCOMANDO POTENCIALMENTE PERIGOSO\033[0m")
            answer = input("Executar mesmo assim? [sim/N]: ").strip().lower()
            if answer not in {"sim", "s", "yes", "y"}:
                print("[NEXUS] Comando cancelado.")
                return "", None, False
        output, code = self.pty.run_command(command, int(self.config.get("terminal_timeout", 120)))
        self.last_output, self.last_exit = output, code
        if output:
            print(f"\n{output}")
        print(f"\n[exit code: {code}]")
        cooldown = int(self.config.get("execution_cooldown", 1))
        if cooldown:
            time.sleep(cooldown)
        return output, code, True

    def confirm_command(self, command: str) -> bool:
        if self.auto_mode and not is_dangerous(command):
            return True
        print("\nExecutar:\n  " + command)
        answer = input("[s]im [n]ão [a]uto [x]parar: ").strip().lower()
        if answer in {"x", "stop"}:
            self.stop_event.set()
            self.pty.interrupt()
            return False
        if answer in {"a", "auto"}:
            self.auto_mode = True
            return not is_dangerous(command)
        return answer in {"s", "sim", "y", "yes"}

    def run_local_command(self, command: str) -> None:
        print("\n[NEXUS] ROTEADOR LOCAL → 0 API")
        output, code, executed = self.execute_command(command)
        if executed:
            print("\n\033[1;35mWHITE RAT:\033[0m")
            print(output if output else f"Comando concluído. exit code={code}")

    def run_technical(self, request: str, mode: str, complexity: str, fast: bool = False) -> None:
        if complexity == "medium":
            budget = int(self.config.get("medium_api_budget", 5))
        else:
            budget = int(self.config.get("complex_api_budget", 8))
        if fast:
            budget = min(budget, 4)
        self.current_budget = min(budget, int(self.config.get("max_api_calls_per_task", 12)))

        interpretation = self.call_agent("interpretacao", {"user_request": request})
        if complexity == "medium":
            plan = self.call_agent("planejamento", {"task": interpretation, "terminal": self.terminal_state()})
            execution = self.call_agent("execucao", {"task": interpretation, "plan": plan, "terminal": self.terminal_state()})
            command = clean_command(execution.get("command", ""))
            if not command:
                raise AgentError("O agente não produziu comando.")
            if not self.confirm_command(command):
                return
            output, code, executed = self.execute_command(command)
            if not executed:
                return
            if self.can_call_api():
                validation = self.call_agent("validacao", {"task": interpretation, "command": command, "exit_code": code, "real_output": output[-6000:]})
                print(f"\n\033[1;35mWHITE RAT:\033[0m {validation.get('reason', 'Validação concluída.')}")
            return

        max_cycles = int(self.config.get("max_agent_cycles", 3))
        for cycle in range(max_cycles):
            if self.stop_event.is_set():
                print("[NEXUS] Tarefa interrompida.")
                return
            print(f"\n[NEXUS] CICLO {cycle + 1}/{max_cycles}")
            plan = self.call_agent("planejamento", {"task": interpretation, "terminal": self.terminal_state(), "cycle": cycle + 1})
            if plan_requires_decision(plan):
                decision = self.call_agent("decisao", {"task": interpretation, "plan": plan, "terminal": self.terminal_state()})
                if not as_bool(decision.get("execute", False)):
                    if self.can_call_api():
                        conclusion = self.call_agent("conclusao", {"task": interpretation, "decision": decision})
                        self.print_conclusion(conclusion)
                    return
            execution = self.call_agent("execucao", {"task": interpretation, "plan": plan, "terminal": self.terminal_state()})
            command = clean_command(execution.get("command", ""))
            if not command:
                raise AgentError("Nenhum comando produzido.")
            if not self.confirm_command(command):
                return
            output, code, executed = self.execute_command(command)
            if not executed:
                return
            if not self.can_call_api():
                print("[NEXUS] Orçamento encerrado.")
                return
            validation = self.call_agent("validacao", {"task": interpretation, "command": command, "exit_code": code, "real_output": output[-6000:], "cycle": cycle + 1})
            if as_bool(validation.get("success", False)):
                print(f"\n\033[1;35mWHITE RAT:\033[0m {validation.get('reason', 'Tarefa concluída.')}")
                return
            print(f"[NEXUS] Validação: {validation.get('reason', 'falha')}")
        print("[NEXUS] Limite de ciclos atingido.")

    @staticmethod
    def print_conclusion(conclusion: dict[str, Any]) -> None:
        print(f"\n\033[1;35mWHITE RAT:\033[0m {conclusion.get('message', 'Tarefa concluída.')}")

    def process_nexus(self, line: str) -> None:
        raw = line[len("/nexus"):].strip()
        if not raw:
            print("Uso: /nexus [--auto] [--fast] <tarefa>")
            return
        if raw == "stop":
            self.stop_event.set(); self.pty.interrupt(); print("[NEXUS] Parada solicitada."); return

        tokens = raw.split()
        mode_auto = False
        fast = False
        while tokens and tokens[0] in {"--auto", "--fast"}:
            token = tokens.pop(0)
            mode_auto |= token == "--auto"
            fast |= token == "--fast"
        # Aceita também flags depois de outra flag e remove todas as ocorrências iniciais.
        while tokens and tokens[-1] in {"--auto", "--fast"}:
            token = tokens.pop()
            mode_auto |= token == "--auto"
            fast |= token == "--fast"
        raw = " ".join(tokens).strip()
        if not raw:
            print("Informe a tarefa.")
            return

        self.auto_mode = mode_auto
        self.stop_event.clear()
        self.task_id += 1
        print(f"\n{'=' * 56}\nNEXUS TASK #{self.task_id}\n{'=' * 56}")
        route = local_route(raw)
        print(f"[NEXUS] Roteamento local: {route['type'].upper()}")
        print(f"[NEXUS] Complexidade: {route['complexity'].upper()}")
        print(f"[NEXUS] APIs planejadas: {route['api']}")
        if route["type"] == "conversation":
            print(f"\n\033[1;35mWHITE RAT:\033[0m\n{route['response']}")
            return
        if route["type"] == "direct":
            self.run_local_command(route["command"])
            return
        self.key_pool.begin_task()
        self.current_task_calls = 0
        try:
            self.run_technical(raw, "auto" if mode_auto else "normal", route["complexity"], fast)
        except (RateLimitError, AgentError) as exc:
            print(f"\n\033[1;31m[NEXUS] {type(exc).__name__.upper()}:\033[0m\n{exc}")
        except KeyboardInterrupt:
            print("\n[NEXUS] Tarefa interrompida.")

    def status(self) -> None:
        print("\n================ NEXUS STATUS ================")
        print(f"Versão:          {APP_VERSION}")
        print(f"Modelo:          {self.config.get('model')}")
        print(f"Shell:           {self.pty.shell}")
        print(f"PTY:             {'ONLINE' if self.pty.alive else 'OFFLINE'}")
        print(f"Diretório PTY:   {self.pty.cwd}")
        print(f"Intervalo API:   {self.config.get('api_min_interval')}s")
        print(f"APIs tarefa:     {self.current_task_calls}/{self.current_budget}")
        print("\nPOOL DE CHAVES")
        for item in self.key_pool.status():
            print(f"#{item['index']} {item['state']:<18} calls={item['requests']:<4} 429={item['429']:<3} err={item['errors']:<3} auth={item['auth']:<3}")
        print("===============================================")

    def show_keys(self) -> None:
        print("\n================ KEY POOL ====================")
        for item in self.key_pool.status():
            print(f"Chave #{item['index']}: {item['state']:<18} requests={item['requests']} 429={item['429']} errors={item['errors']} auth={item['auth']}")
        print("As chaves nunca são exibidas.")
        print("===============================================")

    def setup_keys(self) -> None:
        print("\nNEXUS KEY SETUP")
        print("As chaves serão gravadas em ~/.config/nexus/config.json com permissão 0600.")
        print("Cole um bloco com uma chave por linha; finalize com uma linha vazia.")
        print("Também aceito chaves separadas por vírgula ou ponto e vírgula.")
        print("Deixe a primeira linha vazia para manter o pool atual.")
        first = input("Bloco de chaves: ")
        if not first.strip():
            print("[NEXUS] Pool atual mantido.")
            return
        pasted = [first]
        while True:
            line = input()
            if not line.strip():
                break
            pasted.append(line)
        new_keys = list(dict.fromkeys(parse_api_keys_block("\n".join(pasted))))
        self.config["keys"] = new_keys
        save_config(self.config)
        self.key_pool = KeyPool(self.config)
        self.ai.key_pool = self.key_pool
        print(f"[NEXUS] Configuração salva. Pool: {len(get_api_keys(self.config))} chave(s), sem limite.")

    def reset_limits(self) -> None:
        self.key_pool.reset()
        print("[NEXUS] Cooldowns do pool zerados.")

    @staticmethod
    def help() -> None:
        print("""
================ NEXUS HELP ====================

/nexus <tarefa>             tarefa inteligente
/nexus --auto <tarefa>      execução automática segura
/nexus --fast <tarefa>      orçamento reduzido
/nexus --auto --fast ...    combina os modos
/nexus stop                 interrompe a tarefa
/status                     estado do NEXUS/pool
/keys                       saúde das chaves
/setup                      cola qualquer quantidade de chaves
/reset-limits               remove cooldowns
/config                     mostra configuração
/self-test                  diagnóstico local
/clear                      limpa a tela
/help                       esta ajuda
/exit                       sai

TAB                         autocomplete
↑ / ↓                       histórico

Exemplos:
  /nexus oi
  /nexus mostre minha memória
  /nexus qual é meu kernel
  /nexus instale docker
  /nexus --auto verifique o espaço em disco
  /nexus analise completamente meu sistema

=================================================
""")

    def show_config(self) -> None:
        print(f"\nConfig: {CONFIG_FILE}\n")
        for key, value in self.config.items():
            if key == "keys":
                print(f"{key:<30} [{'CONFIGURADA' if value else 'VAZIA'}]")
            else:
                print(f"{key:<30} {value}")

    def command(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        if line == "/exit": self.running = False; return
        if line == "/help": self.help(); return
        if line == "/status": self.status(); return
        if line == "/keys": self.show_keys(); return
        if line == "/setup": self.setup_keys(); return
        if line == "/reset-limits": self.reset_limits(); return
        if line == "/config": self.show_config(); return
        if line == "/clear": os.system("clear"); return
        if line == "/self-test": self_test(); return
        if line.startswith("/nexus"):
            self.process_nexus(line); return
        self.pty.write(line + "\n")

    def banner(self) -> None:
        print(f"""
╔════════════════════════════════════════════════════════════╗
║                    NEXUS TERMINAL                         ║
║                       WHITE RAT                           ║
║                         v5.1                              ║
╠════════════════════════════════════════════════════════════╣
║ ROUTER LOCAL       → 0 API quando possível               ║
║ MULTI-AGENT        → interpretação / plano / execução     ║
║ PTY                 → Linux REAL                          ║
║ VALIDAÇÃO           → resultado REAL                      ║
║ KEY POOL            → quantidade ilimitada / failover sequencial            ║
║ AUTOCOMPLETE        → TAB                                 ║
╚════════════════════════════════════════════════════════════╝
Modelo: {self.config.get('model')}
Intervalo API: {self.config.get('api_min_interval')}s
Digite /help para ajuda.
""")

    def run(self) -> None:
        self.pty.start()
        setup_readline(self)
        self.banner()
        errors = validate_keys(self.config)
        if errors:
            print("\033[1;33m[NEXUS] POOL:\033[0m")
            for error in errors:
                print("  - " + error)
            print("Use /setup ou NEXUS_GEMINI_KEY_1..N ou NEXUS_GEMINI_KEYS.")
        else:
            print(f"\033[1;32m[NEXUS] POOL COM {len(get_api_keys(self.config))} CHAVE(S) ATIVO\033[0m")
        while self.running and self.pty.alive:
            try:
                output = self.pty.drain()
                if output:
                    sys.stdout.write(output.decode("utf-8", errors="replace")); sys.stdout.flush()
                line = input("NEXUS> ")
                self.command(line)
            except EOFError:
                break
            except KeyboardInterrupt:
                print("\n[NEXUS] Ctrl+C → interrupção solicitada.")
                self.stop_event.set(); self.pty.interrupt()
            except Exception as exc:
                print(f"\n[NEXUS] Erro: {exc}")
        save_history()
        self.pty.stop()


def plan_requires_decision(plan: dict[str, Any]) -> bool:
    text = str(plan.get("next_step", "")).lower()
    return any(x in text for x in ("decidir", "avaliar se", "verificar se é necessário", "verificar se e necessario", "somente se"))


def self_test() -> None:
    print("\n==============================================")
    print("NEXUS SELF TEST")
    print("==============================================")
    print(f"[OK] Python: {sys.version.split()[0]}")
    print("[OK] pexpect")
    print("[OK] requests")
    print("[OK] readline")
    config = load_config()
    print(f"[OK] config: {CONFIG_FILE}")
    keys = get_api_keys(config)
    print(f"[INFO] pool: {len(keys)} chave(s) configurada(s), sem limite fixo")
    shell = os.environ.get("SHELL") or "/bin/bash"
    print(f"[OK] Shell: {shell}")
    for command in ("bash", "pwd", "ls", "uname"):
        print(f"[OK] {command}" if shutil.which(command) else f"[WARN] {command} não encontrado")
    print("\n[TEST] Inicializando PTY...")
    pty = RealPTY()
    try:
        pty.start()
        output, code = pty.run_command("printf 'NEXUS_SELF_TEST_OK'", 10)
        if "NEXUS_SELF_TEST_OK" in output and code == 0:
            print("[OK] PTY REAL funcionando")
        else:
            print(f"[FAIL] PTY output={output!r} exit={code}")
    except Exception as exc:
        print(f"[FAIL] PTY: {exc}")
    finally:
        pty.stop()
    print("\n==============================================")
    print("SELF TEST FINALIZADO")
    print("==============================================")


def main() -> None:
    parser = argparse.ArgumentParser(description="NEXUS TERMINAL Smart Router + Multi-Agent")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--setup", action="store_true")
    args = parser.parse_args()
    if args.version:
        print(APP_NAME, APP_VERSION); return
    if args.self_test:
        self_test(); return
    app = NexusApp()

    def signal_handler(signum: int, frame: Any) -> None:
        app.stop_event.set()
        try: app.pty.interrupt()
        except Exception: pass

    signal.signal(signal.SIGINT, signal_handler)
    try:
        if args.setup:
            app.pty.start(); app.setup_keys(); return
        app.run()
    except KeyboardInterrupt:
        print("\n[NEXUS] Encerrando.")
    except Exception as exc:
        print(f"\n\033[1;31m[NEXUS] ERRO FATAL:\033[0m\n{exc}")
    finally:
        save_history()
        app.pty.stop()


if __name__ == "__main__":
    main()
