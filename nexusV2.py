import argparse
import ast
import copy
import json
import os
import queue
import readline
import re
import shutil
import signal
import subprocess
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
APP_VERSION = "6.2.0-PYTHON-ENGINE"

CONFIG_DIR = Path.home() / ".config" / "nexus"
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history"
GENERATED_DIR = CONFIG_DIR / "generated"

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.6-flash"
API_KEY_ENV_PREFIX = "NEXUS_GEMINI_KEY_"

DEFAULT_CONFIG: dict[str, Any] = {
    "model": DEFAULT_MODEL,
    "temperature": 0.1,
    "max_output_tokens": 3000,
    "programming_max_output_tokens": 10000,
    "api_timeout": 90,
    "terminal_timeout": 300,
    "generated_script_timeout": 300,
    "api_min_interval": 8,
    "execution_cooldown": 1,
    "rate_limit_backoff": 60,
    "auth_cooldown": 300,
    "error_cooldown": 15,
    "max_key_failover": None,
    "max_retries": 1,
    "dangerous_always_confirm": True,
    "max_output_chars": 8000,
    "request_max_chars": 20000,
    "max_generated_script_chars": 200000,
    "keep_generated_scripts": True,
    "log_enabled": False,
    "keys": [],
}

AGENTS = {
    "unica": "RESPOSTA ÚNICA",
    "interpretacao": "INTERPRETAÇÃO",
    "planejamento": "PLANEJAMENTO",
    "decisao": "DECISÃO",
    "programacao": "PROGRAMAÇÃO",
    "validacao": "VALIDAÇÃO",
    "conclusao": "CONCLUSÃO",
}

PROMPTS = {
    "unica": """
Você é o agente principal do NEXUS TERMINAL.
Resolva o pedido em uma única decisão.

Tarefas simples podem usar command.
Tarefas com análise, processamento, automação, crawling, parsing,
arquivos, APIs ou múltiplas etapas devem preferir mode="python".
Não coloque código Python neste agente.

Retorne SOMENTE JSON válido:
{
  "execute": true,
  "mode": "python|command|response",
  "command": "",
  "response": "",
  "reason": "..."
}

Se mode="command", forneça UM comando Linux.
Se mode="response", forneça a resposta sem executar.
""",

    "interpretacao": """
Você é o agente INTERPRETAÇÃO do NEXUS TERMINAL.

Transforme o pedido em uma especificação objetiva.
Não execute nada e não invente resultados.

Retorne SOMENTE JSON válido:
{
  "task": "...",
  "requirements": ["..."],
  "inputs": ["..."],
  "outputs": ["..."],
  "constraints": ["..."]
}
""",

    "planejamento": """
Você é o agente PLANEJAMENTO do NEXUS TERMINAL.

Crie um plano curto e executável para a tarefa.
Não execute nada.

Retorne SOMENTE JSON válido:
{
  "steps": ["..."],
  "expected_output": "...",
  "risks": ["..."]
}
""",

    "decisao": """
Você é o agente DECISÃO do NEXUS TERMINAL.

Escolha:
- python: lógica, automação, crawling, scraping, análise,
  processamento, múltiplas etapas, arquivos, APIs ou parsing.
- command: um único comando Linux simples é claramente suficiente.
- response: nenhuma execução é necessária.

Retorne SOMENTE JSON válido:
{
  "mode": "python|command|response",
  "reason": "..."
}
""",

    "programacao": """
Você é o agente PROGRAMAÇÃO do NEXUS TERMINAL.

CRIE UM PROGRAMA PYTHON COMPLETO, FUNCIONAL E EXECUTÁVEL.

O código retornado será:
1. salvo em arquivo .py REAL;
2. validado por AST e py_compile;
3. executado no Linux REAL;
4. analisado pelos agentes de validação e conclusão.

REGRA ABSOLUTA:
O campo "code" deve conter TODO o código Python.

NUNCA use:
...
"..."
'...'
# ...
# código aqui
# codigo aqui
# restante do código
# resto do código
# código omitido
# codigo omitido
TODO
IMPLEMENTAR AQUI
IMPLEMENTE AQUI

Não resuma o programa.
Não omita funções.
Não use pseudocódigo.
Não coloque markdown dentro de "code".
Não invente resultados.

Para uma tarefa de análise de arquivos Python, o programa deve realmente
localizar os arquivos .py, ler os arquivos, analisar o conteúdo e produzir
uma saída verificável.

Use biblioteca padrão quando suficiente.
Se biblioteca externa for indispensável, informe em "dependencies".
Não instale dependências automaticamente.
Trate erros e use exit code diferente de zero em falha.

Para web/crawling, respeite robots.txt quando aplicável, timeouts,
limites razoáveis e erros HTTP.
Não exfiltre segredos, tokens, senhas ou conteúdo privado.
Evite subprocess e shell=True quando não forem necessários.

Retorne SOMENTE JSON válido:
{
  "language": "python",
  "filename": "nome_descritivo.py",
  "code": "CÓDIGO PYTHON COMPLETO",
  "dependencies": [],
  "reason": "..."
}
""",

    "validacao": """
Você é o agente VALIDAÇÃO do NEXUS TERMINAL.

Analise somente o resultado REAL.
Considere stdout, stderr, exit code e a tarefa solicitada.
Não invente sucesso.

Retorne SOMENTE JSON:
{
  "success": true,
  "reason": "...",
  "next_action": "none|retry|report"
}
""",

    "conclusao": """
Você é o agente CONCLUSÃO do NEXUS TERMINAL.

Resuma somente o que foi realmente executado e observado.

Retorne SOMENTE JSON:
{
  "done": true,
  "message": "..."
}
""",
}

ANSI_ESCAPE_RE = re.compile(
    r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\)|[@-Z\\-_])"
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

SCRIPT_DANGEROUS_PATTERNS = [
    r"\bos\.system\s*\(",
    r"\bos\.popen\s*\(",
    r"\bsubprocess\.(?:run|Popen|call|check_call|check_output)\s*\(",
    r"\bshell\s*=\s*True",
    r"\bshutil\.rmtree\s*\(",
    r"\bPath\s*\([^)]*\)\.unlink\s*\(",
    r"\bos\.(?:remove|unlink|rmdir)\s*\(",
    r"\bos\.(?:chmod|chown)\s*\(",
    r"\bsocket\.(?:socket|create_connection)\s*\(",
    r"\brequests\.(?:post|put|patch|delete)\s*\(",
]

SCRIPT_IMPORT_CONFIRM = {
    "subprocess",
    "socket",
    "ftplib",
    "telnetlib",
}

PLACEHOLDERS = (
    "...",
    "# ...",
    '"..."',
    "'...'",
    "TODO",
    "IMPLEMENTAR AQUI",
    "IMPLEMENTE AQUI",
    "# código aqui",
    "# codigo aqui",
    "# restante do código",
    "# restante do codigo",
    "# resto do código",
    "# resto do codigo",
    "# código omitido",
    "# codigo omitido",
)

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

def is_dangerous(command: str) -> bool:
    normalized = re.sub(r"\s+", " ", command.lower()).strip()
    return any(re.search(pattern, normalized) for pattern in DANGEROUS_PATTERNS)

def script_requires_confirmation(code: str) -> bool:
    if any(re.search(pattern, code) for pattern in SCRIPT_DANGEROUS_PATTERNS):
        return True
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return True
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] in SCRIPT_IMPORT_CONFIRM for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in SCRIPT_IMPORT_CONFIRM:
                return True
    return False

def validate_generated_python(code: str) -> str:
    if not isinstance(code, str):
        raise AgentError("O campo code não é uma string Python.")
    code = code.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not code:
        raise AgentError("O agente PROGRAMAÇÃO retornou código vazio.")

    max_chars = 200000
    if len(code) > max_chars:
        raise AgentError(f"Script excede o limite de {max_chars} caracteres.")

    lowered = code.lower()
    for placeholder in PLACEHOLDERS:
        if placeholder.lower() in lowered:
            raise AgentError(
                f"Código Python incompleto: placeholder detectado: {placeholder!r}"
            )

    try:
        ast.parse(code, filename="<nexus-generated>")
    except SyntaxError as exc:
        raise AgentError(
            f"Código Python inválido: linha {exc.lineno}: {exc.msg}"
        ) from exc

    return code

def sanitize_filename(filename: str, fallback: str) -> str:
    name = Path(filename or fallback).name
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    if not name.endswith(".py"):
        name += ".py"
    if name in {".py", "..py"}:
        name = fallback
    return name

def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.chmod(tmp, mode)
    except OSError:
        pass
    os.replace(tmp, path)

def load_config() -> dict[str, Any]:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CONFIG_DIR, 0o700)
    except OSError:
        pass
    config = copy.deepcopy(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                config.update(data)
        except Exception as exc:
            print(f"[NEXUS] Aviso: config.json inválido: {exc}")
    if not isinstance(config.get("keys"), list):
        config["keys"] = []
    return config

def save_config(config: dict[str, Any]) -> None:
    data = copy.deepcopy(config)
    data["keys"] = [str(k).strip() for k in data.get("keys", []) if str(k).strip()]
    atomic_write(CONFIG_FILE, json.dumps(data, indent=2, ensure_ascii=False))

def parse_api_keys_block(value: str) -> list[str]:
    if not value:
        return []
    text = str(value).strip()
    quoted = re.findall(r'"([^"\r\n]*)"', text)
    if quoted:
        return [item.strip() for item in quoted if item.strip()]
    parts = re.split(r"[\r\n,;]+", text)
    return [item.strip().strip('"\'') for item in parts if item.strip()]

def get_api_keys(config: dict[str, Any]) -> list[str]:
    keys: list[str] = []
    configured = config.get("keys", [])
    if isinstance(configured, list):
        for item in configured:
            keys.extend(parse_api_keys_block(str(item)))

    env_items: list[tuple[int, str]] = []
    pattern = re.compile(r"^" + re.escape(API_KEY_ENV_PREFIX) + r"(\d+)$")
    for name, value in os.environ.items():
        match = pattern.match(name)
        if match and value.strip():
            env_items.append((int(match.group(1)), value.strip()))
    for _, value in sorted(env_items):
        keys.extend(parse_api_keys_block(value))

    keys.extend(parse_api_keys_block(os.environ.get("NEXUS_GEMINI_KEYS", "")))
    return list(dict.fromkeys(keys))

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

    @property
    def keys(self) -> list[str]:
        keys = get_api_keys(self.config)
        while len(self.states) < len(keys):
            self.states.append(KeyState())
        return keys

    def begin_task(self) -> None:
        with self.lock:
            self.failed_this_task.clear()

    def next_key(self, excluded: set[int] | None = None) -> tuple[int, str]:
        with self.lock:
            keys = self.keys
            if not keys:
                raise AgentError("Nenhuma chave Gemini configurada.")

            excluded = set(excluded or set()) | self.failed_this_task
            now = time.monotonic()
            available = [
                i for i in range(len(keys))
                if i not in excluded and self.states[i].cooldown_until <= now
            ]
            if not available:
                future = [
                    self.states[i].cooldown_until
                    for i in range(len(keys))
                    if i not in excluded and self.states[i].cooldown_until > now
                ]
                if future:
                    wait = max(1, int(min(future) - now + 0.999))
                    raise RateLimitError(
                        f"Todas as chaves disponíveis estão em cooldown. Aguarde {wait}s."
                    )
                raise RateLimitError("Todas as chaves falharam nesta tarefa.")

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
            self.failed_this_task.clear()

    def status(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        keys = self.keys
        result = []
        for i in range(len(keys)):
            s = self.states[i]
            state = f"COOLDOWN {int(s.cooldown_until - now)}s" if s.cooldown_until > now else "DISPONÍVEL"
            result.append({
                "index": i + 1,
                "state": state,
                "requests": s.requests,
                "429": s.rate_limits,
                "errors": s.errors,
                "auth": s.auth_errors,
            })
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
        if seconds > 0:
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
        if agent_id not in PROMPTS:
            raise AgentError(f"Agente desconhecido: {agent_id}")

        keys = self.key_pool.keys
        if not keys:
            raise AgentError("Nenhuma API key configurada. Use /setup.")

        request_state = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        request_state = request_state[:int(self.config.get("request_max_chars", 20000))]

        model = str(self.config.get("model", DEFAULT_MODEL)).strip()
        url = GEMINI_URL.format(model=model)

        if agent_id == "programacao":
            max_tokens = int(self.config.get("programming_max_output_tokens", 10000))
        else:
            max_tokens = int(self.config.get("max_output_tokens", 3000))

        payload = {
            "system_instruction": {"parts": [{"text": PROMPTS[agent_id]}]},
            "contents": [{"role": "user", "parts": [{"text": request_state}]}],
            "generationConfig": {
                "temperature": float(self.config.get("temperature", 0.1)),
                "maxOutputTokens": max_tokens,
                "candidateCount": 1,
                "responseMimeType": "application/json",
            },
        }

        max_retries = max(0, int(self.config.get("max_retries", 1)))
        used: set[int] = set()

        for _ in range(len(keys)):
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
                        timeout=int(self.config.get("api_timeout", 90)),
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
                    print(f"[NEXUS] chave #{key_index + 1} rejeitada HTTP {response.status_code}; failover.")
                    break

                if 500 <= response.status_code <= 599:
                    self.key_pool.mark_error(key_index)
                    if retry < max_retries:
                        self.limiter.backoff(2 ** retry, f"Servidor HTTP {response.status_code}")
                        continue
                    self.key_pool.mark_failed_this_task(key_index)
                    break

                if response.status_code >= 400:
                    raise AgentError(f"Gemini HTTP {response.status_code}: {response.text[:2500]}")

                try:
                    data = response.json()
                except ValueError as exc:
                    raise AgentError(f"Resposta da API não é JSON: {exc}") from exc

                candidates = data.get("candidates") or []
                if not candidates:
                    raise AgentError(f"Gemini retornou candidates vazio. Feedback: {data.get('promptFeedback')}")

                parts = (candidates[0].get("content") or {}).get("parts") or []
                result = "".join(
                    str(p.get("text", "")) for p in parts if isinstance(p, dict)
                ).strip()

                if not result:
                    raise AgentError("Gemini retornou resposta vazia.")
                return result

        raise RateLimitError("O pool não conseguiu atender a chamada após failover.")

def extract_json(text: str) -> dict[str, Any]:
    if not text:
        raise AgentError("Resposta vazia do agente.")

    raw = str(text).strip()
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    decoder = json.JSONDecoder()
    for candidate in (raw, cleaned):
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

    raise AgentError("O agente não retornou JSON válido:\n" + raw[:4000])

def validate_program_response(program: dict[str, Any]) -> dict[str, Any]:
    language = str(program.get("language", "python")).lower().strip()
    if language != "python":
        raise AgentError(f"Agente de programação retornou language={language!r}.")

    code = validate_generated_python(program.get("code", ""))
    filename = sanitize_filename(
        str(program.get("filename", "")),
        "nexus_generated.py",
    )

    dependencies = program.get("dependencies", [])
    if not isinstance(dependencies, list):
        dependencies = [str(dependencies)]

    return {
        "language": "python",
        "filename": filename,
        "code": code,
        "dependencies": dependencies,
        "reason": clean_text(program.get("reason", "")),
    }

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

PYTHON_PROJECT_TERMS = (
    "todos os arquivos",
    "todos os .py",
    "arquivos python",
    "arquivos .py",
    "analisar projeto",
    "analise projeto",
    "análise projeto",
    "analisar arquivos",
    "analise arquivos",
    "análise arquivos",
    "varrer projeto",
    "varredura",
    "gerar relatório",
    "gerar relatorio",
    "processar arquivos",
    "extrair dados",
    "crawling",
)

COMPLEX_TERMS = (
    "diagnóstico completo", "diagnostico completo", "analise tudo", "análise tudo",
    "analise completa", "análise completa", "investigue", "investigar", "gargalo",
    "problema do sistema", "problemas do sistema", "otimize meu sistema",
    "otimizar meu sistema", "corrija meu sistema", "verifique tudo",
    "diagnóstico geral", "diagnostico geral", "catálogo completo", "catalogo completo",
    "rastreie", "rastrear", "extraia", "extrair", "analise a página",
    "analise o site", "varra o site", "varrer o site",
)

def local_route(request: str) -> dict[str, Any]:
    normalized = normalize_input(request)

    if normalized in GREETING_RESPONSES:
        return {"type": "conversation", "complexity": "direct", "api": 0, "response": GREETING_RESPONSES[normalized]}

    if normalized in {"obrigado", "obrigada", "valeu", "vlw", "thanks"}:
        return {"type": "conversation", "complexity": "direct", "api": 0, "response": "Disponha. NEXUS continua operacional."}

    if normalized in {"quem é você", "quem e voce", "o que é você", "o que e voce"}:
        return {
            "type": "conversation",
            "complexity": "direct",
            "api": 0,
            "response": "Sou o NEXUS TERMINAL, assistente Linux com PTY real e executor de scripts Python.",
        }

    if normalized in DIRECT_COMMANDS:
        return {"type": "direct", "complexity": "direct", "api": 0, "command": DIRECT_COMMANDS[normalized]}

    if any(term in normalized for term in PYTHON_PROJECT_TERMS):
        return {"type": "technical", "complexity": "complex", "api": 6, "preferred_engine": "python"}

    if any(word in normalized for word in COMPLEX_TERMS):
        return {"type": "technical", "complexity": "complex", "api": 6}

    if any(normalized.startswith(prefix) for prefix in CHANGE_PREFIXES):
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

        self.child = pexpect.spawn(
            self.shell, args, env=env, encoding=None, echo=False,
            dimensions=(40, 120), timeout=0.1,
        )
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
                output = self.clean_command_output(captured[:match.start()], command)
                self.refresh_cwd()
                return output, code
            time.sleep(0.03)

        self.write("\x03")
        captured += self.drain()
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

    @classmethod
    def clean_command_output(cls, data: bytes, command: str) -> str:
        text = cls.clean_output(data)
        lines: list[str] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            if stripped == command.strip():
                continue
            if stripped.startswith(">"):
                continue
            lines.append(line)
        return "\n".join(lines).strip()

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
        "/setup", "/self-test", "/scripts", "/help", "/clear", "/exit",
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
        expanded = os.path.expanduser(text or "")
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
            value = item.name if text in ("", ".") else str(path.parent / item.name)
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
        limit = int(self.config.get("max_output_chars", 8000))
        return {
            "cwd": self.pty.cwd,
            "shell": self.pty.shell,
            "last_exit_code": self.last_exit,
            "last_output": self.last_output[-limit:],
        }

    def can_call_api(self) -> bool:
        return self.current_task_calls < self.current_budget

    def call_agent(self, agent_id: str, state: dict[str, Any]) -> dict[str, Any]:
        if not self.can_call_api():
            raise AgentError(
                f"Orçamento de API da tarefa atingido: "
                f"{self.current_task_calls}/{self.current_budget}"
            )

        self.current_task_calls += 1
        print(
            f"\n┌──────────────────────────────────────┐\n"
            f"│ {AGENTS[agent_id]:<36} │\n"
            f"└──────────────────────────────────────┘"
        )
        print(f"[NEXUS] API {self.current_task_calls}/{self.current_budget}")
        return extract_json(self.ai.ask(agent_id, state))

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

        output, code = self.pty.run_command(
            command,
            int(self.config.get("terminal_timeout", 300)),
        )
        self.last_output = output
        self.last_exit = code

        if output:
            print(f"\n{output}")
        print(f"\n[exit code: {code}]")

        cooldown = int(self.config.get("execution_cooldown", 1))
        if cooldown:
            time.sleep(cooldown)

        return output, code, True

    def save_generated_script(self, filename: str, code: str) -> Path:
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(GENERATED_DIR, 0o700)
        except OSError:
            pass

        safe_name = sanitize_filename(filename, f"nexus_task_{self.task_id}.py")
        path = GENERATED_DIR / safe_name
        code = validate_generated_python(code)

        if not code.startswith("#!"):
            code = "#!/usr/bin/env python3\n# -*- coding: utf-8 -*-\n" + code

        atomic_write(path, code, 0o700)
        return path

    def validate_python_syntax(self, path: Path) -> None:
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError) as exc:
            raise AgentError(f"Validação AST falhou: {exc}") from exc

        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise AgentError(
                "Falha no py_compile:\n" +
                (result.stderr or result.stdout)[-5000:]
            )

    def execute_generated_script(self, path: Path, reason: str = "") -> tuple[str, Optional[int], bool]:
        code = path.read_text(encoding="utf-8")

        print("\n\033[1;36mNEXUS PYTHON EXECUTOR\033[0m")
        print(f"[NEXUS] Arquivo: {path}")
        if reason:
            print(f"[NEXUS] Motivo: {reason}")

        self.validate_python_syntax(path)
        print("[NEXUS] AST + py_compile: OK")

        if script_requires_confirmation(code) and self.config.get("dangerous_always_confirm", True):
            print("\033[1;31mSCRIPT CONTÉM OPERAÇÕES QUE EXIGEM CONFIRMAÇÃO\033[0m")
            answer = input("Executar este script? [sim/N]: ").strip().lower()
            if answer not in {"sim", "s", "yes", "y"}:
                print("[NEXUS] Script cancelado.")
                return "", None, False

        command = (
            f"cd {shlex_quote(str(path.parent))} && "
            f"{shlex_quote(sys.executable)} {shlex_quote(path.name)}"
        )

        exports = (
            f"export NEXUS_TASK_ID={shlex_quote(str(self.task_id))}; "
            f"export NEXUS_CWD={shlex_quote(self.pty.cwd)}; "
            f"export NEXUS_GENERATED_SCRIPT={shlex_quote(str(path))}; "
        )

        output, code_result = self.pty.run_command(
            exports + command,
            int(self.config.get("generated_script_timeout", 300)),
        )

        self.last_output = output
        self.last_exit = code_result

        print("\n\033[1;35mWHITE RAT — SAÍDA REAL:\033[0m")
        print(output if output else "(sem stdout)")
        print(f"\n[exit code: {code_result}]")

        return output, code_result, True

    def run_local_command(self, command: str) -> None:
        print("\n[NEXUS] ROTEADOR LOCAL → 0 API")
        output, code, executed = self.execute_command(command)
        if executed:
            print("\n\033[1;35mWHITE RAT:\033[0m")
            print(output if output else f"Comando concluído. exit code={code}")

    def run_python_pipeline(self, request: str, mode: str, complexity: str, fast: bool = False) -> None:
        state = {
            "user_request": request,
            "terminal": self.terminal_state(),
            "mode": mode,
            "complexity": complexity,
        }

        interpretation = self.call_agent("interpretacao", state)

        plan = None
        if not fast:
            plan = self.call_agent(
                "planejamento",
                {**state, "interpretation": interpretation},
            )

        decision = self.call_agent(
            "decisao",
            {
                **state,
                "interpretation": interpretation,
                "planning": plan or {},
            },
        )

        selected_mode = str(decision.get("mode", "python")).lower().strip()

        if selected_mode == "response":
            response = clean_text(decision.get("response", ""))
            print("\n\033[1;35mWHITE RAT:\033[0m\n" + (response or "A tarefa não exige execução."))
            return

        if selected_mode == "command":
            answer = self.call_agent(
                "unica",
                {
                    **state,
                    "interpretation": interpretation,
                    "planning": plan or {},
                    "decision": decision,
                },
            )
            command = clean_command(answer.get("command", ""))
            if not command:
                raise AgentError("Decisão command, mas nenhum comando foi produzido.")

            if not self.confirm_command(command):
                return

            output, code_result, executed = self.execute_command(command)
            if not executed:
                return

            validation = self.call_agent(
                "validacao",
                {
                    "request": request,
                    "command": command,
                    "stdout": output[-8000:],
                    "exit_code": code_result,
                },
            )
            print("\n\033[1;35mWHITE RAT:\033[0m " + str(validation.get("reason", "Validação concluída.")))
            return

        program = self.call_agent(
            "programacao",
            {
                **state,
                "interpretation": interpretation,
                "planning": plan or {},
                "decision": decision,
                "terminal": self.terminal_state(),
            },
        )

        program = validate_program_response(program)
        print(f"[NEXUS] Código Python validado: {len(program['code'])} caracteres.")

        path = self.save_generated_script(program["filename"], program["code"])

        print(
            "\n[NEXUS] SCRIPT PYTHON CRIADO REALMENTE:\n"
            f"  {path}"
        )

        dependencies = program.get("dependencies", [])
        if dependencies:
            print("[NEXUS] Dependências declaradas: " + ", ".join(map(str, dependencies)))
            print("[NEXUS] Nenhum pacote será instalado automaticamente.")

        output, exit_code, executed = self.execute_generated_script(
            path,
            program.get("reason", ""),
        )
        if not executed:
            return

        validation = self.call_agent(
            "validacao",
            {
                "request": request,
                "script": str(path),
                "stdout": output[-8000:],
                "exit_code": exit_code,
                "terminal": self.terminal_state(),
            },
        )

        if not bool(validation.get("success", False)):
            print("\n\033[1;33m[NEXUS] VALIDAÇÃO INDICOU FALHA\033[0m")
            print(validation.get("reason", "Resultado não validado."))
            return

        conclusion = self.call_agent(
            "conclusao",
            {
                "request": request,
                "script": str(path),
                "stdout": output[-8000:],
                "exit_code": exit_code,
                "validation": validation,
            },
        )

        print(
            "\n\033[1;35mWHITE RAT:\033[0m " +
            str(conclusion.get("message", "Tarefa concluída."))
        )

    def run_technical(self, request: str, mode: str, complexity: str, fast: bool = False) -> None:
        self.current_budget = 5 if fast else 7
        self.run_python_pipeline(request, mode, complexity, fast)

    def process_nexus(self, line: str) -> None:
        raw = line[len("/nexus"):].strip()
        if not raw:
            print("Uso: /nexus [--auto] [--fast] <tarefa>")
            return

        if raw == "stop":
            self.stop_event.set()
            self.pty.interrupt()
            print("[NEXUS] Parada solicitada.")
            return

        tokens = raw.split()
        mode_auto = False
        fast = False

        changed = True
        while changed:
            changed = False
            if tokens and tokens[0] in {"--auto", "--fast"}:
                token = tokens.pop(0)
                mode_auto |= token == "--auto"
                fast |= token == "--fast"
                changed = True
            if tokens and tokens[-1] in {"--auto", "--fast"}:
                token = tokens.pop()
                mode_auto |= token == "--auto"
                fast |= token == "--fast"
                changed = True

        raw = " ".join(tokens).strip()
        if not raw:
            print("Informe a tarefa.")
            return

        self.auto_mode = mode_auto
        self.stop_event.clear()
        self.task_id += 1
        self.key_pool.begin_task()
        self.current_task_calls = 0

        print(f"\n{'=' * 60}\nNEXUS TASK #{self.task_id}\n{'=' * 60}")

        route = local_route(raw)
        print(f"[NEXUS] Roteamento local: {route['type'].upper()}")
        print(f"[NEXUS] Complexidade: {route['complexity'].upper()}")
        print("[NEXUS] Arquitetura: INTERPRETAÇÃO → DECISÃO → PYTHON → EXECUÇÃO REAL → VALIDAÇÃO")

        if route["type"] == "conversation":
            print("\n\033[1;35mWHITE RAT:\033[0m\n" + route["response"])
            return

        if route["type"] == "direct":
            self.run_local_command(route["command"])
            return

        try:
            self.run_technical(
                raw,
                "auto" if mode_auto else "normal",
                route["complexity"],
                fast,
            )
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
        print(f"Scripts gerados: {GENERATED_DIR}")
        print(f"Intervalo API:   {self.config.get('api_min_interval')}s")
        print(f"APIs tarefa:     {self.current_task_calls}/{self.current_budget}")
        print("\nPOOL DE CHAVES")
        for item in self.key_pool.status():
            print(
                f"#{item['index']} {item['state']:<18} "
                f"calls={item['requests']:<4} 429={item['429']:<3} "
                f"err={item['errors']:<3} auth={item['auth']:<3}"
            )
        print("===============================================")

    def show_keys(self) -> None:
        print("\n================ KEY POOL ====================")
        for item in self.key_pool.status():
            print(
                f"Chave #{item['index']}: {item['state']:<18} "
                f"requests={item['requests']} 429={item['429']} "
                f"errors={item['errors']} auth={item['auth']}"
            )
        print("As chaves nunca são exibidas.")
        print("===============================================")

    def setup_keys(self) -> None:
        print("\nNEXUS KEY SETUP")
        print("As chaves serão gravadas em ~/.config/nexus/config.json com permissão 0600.")
        print("Cole um bloco com uma chave por linha; finalize com uma linha vazia.")
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

        print(f"[NEXUS] Configuração salva. Pool: {len(get_api_keys(self.config))} chave(s).")

    def reset_limits(self) -> None:
        self.key_pool.reset()
        print("[NEXUS] Cooldowns do pool zerados.")

    def list_scripts(self) -> None:
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        scripts = sorted(GENERATED_DIR.glob("*.py"), key=lambda p: p.stat().st_mtime, reverse=True)

        print("\n================ SCRIPTS NEXUS ===============")
        if not scripts:
            print("Nenhum script gerado.")
        else:
            for path in scripts:
                print(f"{path.name:<45} {path.stat().st_size:>8} bytes")
        print(f"\nDiretório: {GENERATED_DIR}")
        print("===============================================")

    @staticmethod
    def help() -> None:
        print("""
================ NEXUS HELP ====================

/nexus <tarefa>             pipeline inteligente
/nexus --auto <tarefa>      modo automático
/nexus --fast <tarefa>      reduz etapas de planejamento
/nexus --auto --fast ...    combina os modos
/nexus stop                 interrompe a tarefa

/status                     estado do NEXUS/pool
/keys                       saúde das chaves
/setup                      configura chaves Gemini
/reset-limits               remove cooldowns
/config                     mostra configuração
/scripts                    lista scripts Python gerados
/self-test                  diagnóstico local
/clear                      limpa a tela
/help                       esta ajuda
/exit                       sai

ARQUITETURA:

PEDIDO
  ↓
ROTEADOR LOCAL
  ↓
INTERPRETAÇÃO
  ↓
PLANEJAMENTO
  ↓
DECISÃO
  ↓
┌───────────────┬─────────────────┐
│ comando       │ programa Python │
│ Linux         │ arquivo REAL    │
└───────────────┴─────────────────┘
                    ↓
              AST + py_compile
                    ↓
             EXECUÇÃO REAL
                    ↓
             stdout/stderr
                    ↓
               exit code
                    ↓
               VALIDAÇÃO
                    ↓
               CONCLUSÃO

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
        if line == "/exit":
            self.running = False
            return
        if line == "/help":
            self.help()
            return
        if line == "/status":
            self.status()
            return
        if line == "/keys":
            self.show_keys()
            return
        if line == "/setup":
            self.setup_keys()
            return
        if line == "/reset-limits":
            self.reset_limits()
            return
        if line == "/config":
            self.show_config()
            return
        if line == "/scripts":
            self.list_scripts()
            return
        if line == "/clear":
            os.system("clear")
            return
        if line == "/self-test":
            self_test()
            return
        if line.startswith("/nexus"):
            self.process_nexus(line)
            return
        self.pty.write(line + "\n")

    def banner(self) -> None:
        print(f"""
╔════════════════════════════════════════════════════════════╗
║                    NEXUS TERMINAL                         ║
║                       WHITE RAT                           ║
║                         v6.2                              ║
╠════════════════════════════════════════════════════════════╣
║ ROUTER LOCAL       → 0 API quando possível               ║
║ DECISÃO            → command ou Python                   ║
║ PYTHON ENGINE      → arquivo Python REAL                 ║
║ AST + PY_COMPILE   → validação antes de executar         ║
║ PTY                 → Linux REAL                          ║
║ VALIDAÇÃO           → resultado REAL                      ║
║ KEY POOL            → failover sequencial                ║
║ AUTOCOMPLETE        → TAB                                 ║
╚════════════════════════════════════════════════════════════╝
Modelo: {self.config.get('model')}
Intervalo API: {self.config.get('api_min_interval')}s
Scripts: {GENERATED_DIR}
Digite /help para ajuda.
""")

    def run(self) -> None:
        self.pty.start()
        setup_readline(self)
        self.banner()

        keys = get_api_keys(self.config)
        if not keys:
            print("\033[1;33m[NEXUS] POOL:\033[0m")
            print("  - Nenhuma chave Gemini configurada.")
            print("Use /setup ou NEXUS_GEMINI_KEY_1..N.")
        else:
            print(f"\033[1;32m[NEXUS] POOL COM {len(keys)} CHAVE(S) ATIVO\033[0m")

        while self.running and self.pty.alive:
            try:
                output = self.pty.drain()
                if output:
                    sys.stdout.write(output.decode("utf-8", errors="replace"))
                    sys.stdout.flush()
                line = input("NEXUS> ")
                self.command(line)
            except EOFError:
                break
            except KeyboardInterrupt:
                print("\n[NEXUS] Ctrl+C → interrupção solicitada.")
                self.stop_event.set()
                self.pty.interrupt()
            except Exception as exc:
                print(f"\n[NEXUS] Erro: {exc}")

        save_history()
        self.pty.stop()

def shlex_quote(value: str) -> str:
    if not value:
        return "''"
    return "'" + value.replace("'", "'\"'\"'") + "'"

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
    print(f"[INFO] pool: {len(get_api_keys(config))} chave(s)")

    shell = os.environ.get("SHELL") or "/bin/bash"
    print(f"[OK] Shell: {shell}")

    for command in ("bash", "python3", "pwd", "ls", "uname"):
        print(f"[OK] {command}" if shutil.which(command) else f"[WARN] {command} não encontrado")

    print("\n[TEST] AST de código gerado...")
    sample = "print('NEXUS_GENERATED_CODE_OK')\n"
    try:
        validate_generated_python(sample)
        print("[OK] AST validation")
    except Exception as exc:
        print(f"[FAIL] AST: {exc}")

    print("\n[TEST] py_compile...")
    tmp = GENERATED_DIR / f".selftest_{uuid.uuid4().hex}.py"
    try:
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write(tmp, sample, 0o700)
        subprocess.run([sys.executable, "-m", "py_compile", str(tmp)], check=True, capture_output=True, text=True)
        print("[OK] py_compile")
    except Exception as exc:
        print(f"[FAIL] py_compile: {exc}")
    finally:
        try:
            tmp.unlink(missing_ok=True)
            pyc = Path(str(tmp) + "c")
            pyc.unlink(missing_ok=True)
            cache = tmp.parent / "__pycache__"
            if cache.exists():
                for item in cache.glob(tmp.stem + ".*.pyc"):
                    item.unlink(missing_ok=True)
        except Exception:
            pass

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
    parser = argparse.ArgumentParser(description="NEXUS TERMINAL Smart Router + Python Engine")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--setup", action="store_true")
    args = parser.parse_args()

    if args.version:
        print(APP_NAME, APP_VERSION)
        return
    if args.self_test:
        self_test()
        return

    app = NexusApp()

    def signal_handler(signum: int, frame: Any) -> None:
        app.stop_event.set()
        try:
            app.pty.interrupt()
        except Exception:
            pass

    signal.signal(signal.SIGINT, signal_handler)

    try:
        if args.setup:
            app.pty.start()
            app.setup_keys()
            return
        app.run()
    except KeyboardInterrupt:
        print("\n[NEXUS] Encerrando.")
    except Exception as exc:
        print("\n\033[1;31m[NEXUS] ERRO FATAL:\033[0m\n" + str(exc))
    finally:
        save_history()
        app.pty.stop()

if __name__ == "__main__":
    main()
