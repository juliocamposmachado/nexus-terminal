#!/usr/bin/env python3

import argparse
import json
import os
import queue
import re
import select
import signal
import sys
import threading
import time
from pathlib import Path

# ============================================================
# DEPENDÊNCIAS
# ============================================================

try:
    import pexpect
except ImportError:
    print("Dependência ausente: pexpect")
    print()
    print("Instale com:")
    print("python3 -m pip install pexpect requests")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("Dependência ausente: requests")
    print()
    print("Instale com:")
    print("python3 -m pip install pexpect requests")
    sys.exit(1)


# ============================================================
# CONFIGURAÇÃO
# ============================================================

APP_NAME = "NEXUS TERMINAL"
APP_VERSION = "2.1.0-GEMINI"

CONFIG_DIR = (
    Path.home()
    / ".config"
    / "nexus"
)

CONFIG_FILE = CONFIG_DIR / "config.json"

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/"
    "v1beta/models/{model}:generateContent"
)


# ============================================================
# GEMINI
# ============================================================

DEFAULT_CONFIG = {

    "provider": "gemini",

    "model": "gemini-3.6-flash",

    "api_key": "",

    "auto_execute": False,

    "max_output_chars": 12000,

    "temperature": 0.2,

    "max_output_tokens": 4096,
}


# ============================================================
# PROMPT DO AGENTE
# ============================================================

SYSTEM_PROMPT = r"""
Você é WHITE RAT, o agente Linux do NEXUS TERMINAL.

Você está conectado a um TERMINAL LINUX REAL.

PERSONALIDADE:

- extremamente inteligente
- técnico
- direto
- objetivo
- preciso
- pode ser sarcástico quando apropriado
- não inventa fatos
- não inventa resultados
- não afirma que executou algo sem receber o resultado real

============================================================
AMBIENTE
============================================================

O NEXUS TERMINAL possui acesso a um shell Linux REAL
através de um PTY persistente.

Você recebe informações reais sobre:

- shell
- usuário
- diretório atual
- código de saída
- saída real do terminal
- modo de execução

Você NÃO deve simular o terminal.

============================================================
EXECUÇÃO DE COMANDOS
============================================================

Quando o usuário pedir para executar alguma coisa no Linux,
você pode produzir uma ACTION:

<ACTION>
{"command":"comando aqui"}
</ACTION>

EXEMPLO:

<ACTION>
{"command":"uname -a"}
</ACTION>

Vários comandos:

<ACTION>
{"command":"pwd"}
</ACTION>

<ACTION>
{"command":"ls -la"}
</ACTION>

============================================================
REGRAS DAS ACTIONS
============================================================

1. Uma ACTION contém exatamente um comando.

2. O campo "command" deve conter somente o comando Linux.

3. Não coloque explicações dentro de "command".

4. Não coloque Markdown dentro de "command".

5. Não invente stdout.

6. Não invente stderr.

7. Não invente arquivos.

8. Não invente processos.

9. Não invente versões.

10. Não diga que um comando foi executado antes
    de receber o resultado real.

11. Depois que o NEXUS executar uma ACTION,
    ele enviará o resultado REAL para você.

12. Analise o resultado REAL antes de solicitar
    uma nova ACTION.

13. Se o resultado já for suficiente para responder
    ao usuário, não solicite outro comando.

14. Não execute comandos desnecessários.

============================================================
DIAGNÓSTICO
============================================================

Para diagnóstico Linux, prefira comandos reais.

Exemplos:

uname -a
hostnamectl
pwd
ls -la
lsblk
df -h
free -h
lscpu
lsusb
lspci
ip addr
ip route
systemctl status
journalctl
ps aux
top
docker ps
docker images
git status
git branch
python3 --version
pip --version

============================================================
OPERAÇÕES PERIGOSAS
============================================================

Não solicite automaticamente operações claramente destrutivas,
incluindo:

rm -rf /
rm -rf /*
mkfs
dd para dispositivos
apagamento de partições
formatação de discos
parted destrutivo
fdisk destrutivo
shutdown
reboot
poweroff

O NEXUS solicitará confirmação ao usuário.

============================================================
PRINCÍPIO FUNDAMENTAL
============================================================

Você não controla o terminal diretamente.

Você solicita uma ACTION.

O NEXUS executa a ACTION no PTY Linux real.

O NEXUS devolve o resultado real.

Você analisa o resultado real.

Somente então solicita outra ACTION, se necessário.

Nunca simule a execução.
"""


# ============================================================
# LIMPEZA DE INPUT
# ============================================================

ANSI_ESCAPE_RE = re.compile(
    r"""
    \x1B
    (?:
        \[[0-?]*[ -/]*[@-~]
        |
        \][^\x07]*(?:\x07|\x1B\\)
        |
        [@-Z\\-_]
    )
    """,
    re.VERBOSE,
)


def clean_terminal_input(value):

    if value is None:
        return ""

    value = str(value)

    # --------------------------------------------------------
    # BRACKETED PASTE
    # --------------------------------------------------------

    value = value.replace(
        "\x1b[200~",
        "",
    )

    value = value.replace(
        "\x1b[201~",
        "",
    )

    # --------------------------------------------------------
    # ANSI / ESC
    # --------------------------------------------------------

    value = ANSI_ESCAPE_RE.sub(
        "",
        value,
    )

    # --------------------------------------------------------
    # CONTROLES
    # --------------------------------------------------------

    cleaned = []

    for char in value:

        code = ord(char)

        if char in "\n\r\t":

            cleaned.append(char)

            continue

        if code >= 32 and code != 127:

            cleaned.append(char)

    return "".join(cleaned).strip()


def clean_shell_input(value):

    if value is None:
        return ""

    value = str(value)

    # Remove bracketed paste markers.
    value = value.replace(
        "\x1b[200~",
        "",
    )

    value = value.replace(
        "\x1b[201~",
        "",
    )

    # Remove ANSI escape sequences.
    value = ANSI_ESCAPE_RE.sub(
        "",
        value,
    )

    # Remove NUL.
    value = value.replace(
        "\x00",
        "",
    )

    # Remove DEL.
    value = value.replace(
        "\x7f",
        "",
    )

    return value


def normalize_api_key(value):

    value = clean_terminal_input(
        value
    )

    value = re.sub(
        r"\s+",
        "",
        value,
    )

    return value


# ============================================================
# CONFIGURAÇÃO
# ============================================================

def save_config(config):

    CONFIG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    CONFIG_FILE.write_text(
        json.dumps(
            config,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    try:

        os.chmod(
            CONFIG_FILE,
            0o600,
        )

    except OSError:

        pass


def load_config():

    CONFIG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    cfg = DEFAULT_CONFIG.copy()

    if CONFIG_FILE.exists():

        try:

            raw = CONFIG_FILE.read_text(
                encoding="utf-8"
            )

            data = json.loads(
                raw
            )

            if isinstance(
                data,
                dict,
            ):

                cfg.update(
                    data
                )

        except Exception as exc:

            print()
            print(
                "[NEXUS] Erro lendo configuração:"
            )

            print(
                exc
            )

            print(
                "[NEXUS] "
                "Usando configuração padrão."
            )

    # --------------------------------------------------------
    # FORÇAR GEMINI
    # --------------------------------------------------------

    cfg[
        "provider"
    ] = "gemini"

    # --------------------------------------------------------
    # REMOVER URL ANTIGA
    # --------------------------------------------------------

    cfg.pop(
        "api_url",
        None,
    )

    # --------------------------------------------------------
    # API KEY
    # --------------------------------------------------------

    configured_key = normalize_api_key(
        cfg.get(
            "api_key",
            "",
        )
    )

    environment_key = normalize_api_key(
        os.environ.get(
            "GEMINI_API_KEY",
            "",
        )
    )

    if environment_key:

        cfg[
            "api_key"
        ] = environment_key

    else:

        cfg[
            "api_key"
        ] = configured_key

    # --------------------------------------------------------
    # MODELO
    # --------------------------------------------------------

    model = clean_terminal_input(
        cfg.get(
            "model",
            DEFAULT_CONFIG["model"],
        )
    )

    if not model:

        model = DEFAULT_CONFIG[
            "model"
        ]

    cfg[
        "model"
    ] = model

    # --------------------------------------------------------
    # GARANTIR CONFIGURAÇÃO LIMPA
    # --------------------------------------------------------

    save_config(
        cfg
    )

    return cfg


# ============================================================
# URL GEMINI
# ============================================================

def build_gemini_url(model):

    model = clean_terminal_input(
        model
    )

    return (
        "https://generativelanguage.googleapis.com/"
        "v1beta/models/"
        + model
        + ":generateContent"
    )


# ============================================================
# CONFIGURE
# ============================================================

def configure():

    cfg = load_config()

    print()
    print(
        "=============================================="
    )
    print(
        "              NEXUS GEMINI CONFIG"
    )
    print(
        "=============================================="
    )

    print()

    print(
        "Arquivo:"
    )

    print(
        CONFIG_FILE
    )

    print()

    print(
        "Provider: Google Gemini"
    )

    # --------------------------------------------------------
    # MODEL
    # --------------------------------------------------------

    current_model = cfg.get(
        "model",
        DEFAULT_CONFIG["model"],
    )

    model = input(
        f"Modelo [{current_model}]: "
    )

    model = clean_terminal_input(
        model
    )

    if model:

        cfg[
            "model"
        ] = model

    # --------------------------------------------------------
    # API KEY
    # --------------------------------------------------------

    print()

    print(
        "API key."
    )

    print(
        "Enter mantém a atual."
    )

    print(
        "- remove a chave."
    )

    print()

    key = input(
        "Gemini API key: "
    )

    key = normalize_api_key(
        key
    )

    if key == "-":

        cfg[
            "api_key"
        ] = ""

    elif key:

        cfg[
            "api_key"
        ] = key

    # --------------------------------------------------------
    # TEMPERATURE
    # --------------------------------------------------------

    current_temperature = cfg.get(
        "temperature",
        0.2,
    )

    temperature = input(
        f"Temperature [{current_temperature}]: "
    )

    temperature = clean_terminal_input(
        temperature
    )

    if temperature:

        try:

            value = float(
                temperature
            )

            if value < 0:
                value = 0

            if value > 2:
                value = 2

            cfg[
                "temperature"
            ] = value

        except ValueError:

            print(
                "Temperature inválida."
            )

    # --------------------------------------------------------
    # MAX TOKENS
    # --------------------------------------------------------

    current_tokens = cfg.get(
        "max_output_tokens",
        4096,
    )

    tokens = input(
        f"Max output tokens [{current_tokens}]: "
    )

    tokens = clean_terminal_input(
        tokens
    )

    if tokens:

        try:

            cfg[
                "max_output_tokens"
            ] = max(
                1,
                int(tokens),
            )

        except ValueError:

            print(
                "Valor inválido."
            )

    # --------------------------------------------------------
    # REMOVER API URL ANTIGA
    # --------------------------------------------------------

    cfg.pop(
        "api_url",
        None,
    )

    # --------------------------------------------------------
    # SALVAR
    # --------------------------------------------------------

    save_config(
        cfg
    )

    print()

    print(
        "Configuração Gemini salva."
    )

    print(
        f"Provider: Google Gemini"
    )

    print(
        f"Modelo:   {cfg['model']}"
    )

    print(
        f"URL:      {build_gemini_url(cfg['model'])}"
    )

    if cfg.get(
        "api_key"
    ):

        key = cfg[
            "api_key"
        ]

        if len(key) > 12:

            masked = (
                key[:6]
                + "..."
                + key[-4:]
            )

        else:

            masked = "***"

        print(
            f"API key:  {masked}"
        )

    else:

        print(
            "API key:  NÃO CONFIGURADA"
        )

    print()


# ============================================================
# PTY LINUX REAL
# ============================================================

class RealPTY:

    def __init__(self):

        self.shell = (
            os.environ.get(
                "SHELL"
            )
            or "/bin/bash"
        )

        if not os.path.exists(
            self.shell
        ):

            self.shell = "/bin/bash"

        self.child = None

        self.lock = threading.RLock()

        self.output_queue = queue.Queue()

        self.reader_thread = None

        self.alive = False

    # ========================================================
    # START
    # ========================================================

    def start(self):

        env = os.environ.copy()

        # ----------------------------------------------------
        # TERMINAL
        # ----------------------------------------------------

        env[
            "TERM"
        ] = "xterm-256color"

        env[
            "NEXUS_TERMINAL"
        ] = "1"

        # ----------------------------------------------------
        # DESABILITAR BRACKETED PASTE DO READLINE
        # ----------------------------------------------------

        env[
            "INPUTRC"
        ] = "/dev/null"

        # ----------------------------------------------------
        # SPAWN
        # ----------------------------------------------------

        self.child = pexpect.spawn(

            self.shell,

            [
                "--noprofile",
                "--norc",
                "-i",
            ],

            env=env,

            encoding=None,

            echo=True,

            dimensions=(
                40,
                120,
            ),

            timeout=0.1,
        )

        self.alive = True

        self.reader_thread = threading.Thread(

            target=self._reader,

            daemon=True,
        )

        self.reader_thread.start()

        # ----------------------------------------------------
        # CONFIGURAR BASH
        # ----------------------------------------------------

        time.sleep(
            0.1
        )

        self._send_control_command(
            "bind 'set enable-bracketed-paste off'"
        )

        self._send_control_command(
            "printf '\\033[?2004l'"
        )

        # ----------------------------------------------------
        # PROMPT LIMPO
        # ----------------------------------------------------

        self._send_control_command(
            "PS1='\\u@\\h:\\w\\$ '"
        )

        # ----------------------------------------------------
        # LIMPAR SAÍDA INICIAL
        # ----------------------------------------------------

        time.sleep(
            0.1
        )

        self.drain()

    # ========================================================
    # CONTROLE INTERNO
    # ========================================================

    def _send_control_command(
        self,
        command,
    ):

        if (
            self.child is None
            or not self.alive
        ):

            return

        try:

            with self.lock:

                self.child.write(
                    command
                    + "\n"
                )

        except Exception:

            pass

    # ========================================================
    # READER
    # ========================================================

    def _reader(self):

        while (
            self.alive
            and self.child is not None
        ):

            try:

                data = (
                    self.child.read_nonblocking(

                        size=4096,

                        timeout=0.1,
                    )
                )

                if data:

                    self.output_queue.put(
                        data
                    )

            except pexpect.TIMEOUT:

                continue

            except (
                pexpect.EOF,
                OSError,
            ):

                break

            except Exception as exc:

                self.output_queue.put(

                    (
                        "\n"
                        "[NEXUS PTY ERROR] "
                        f"{exc}\n"
                    ).encode()
                )

                break

        self.alive = False

    # ========================================================
    # WRITE
    # ========================================================

    def write(
        self,
        data,
    ):

        if (
            not self.alive
            or self.child is None
        ):

            return False

        try:

            # ------------------------------------------------
            # NORMALIZAR INPUT
            # ------------------------------------------------

            data = clean_shell_input(
                data
            )

            with self.lock:

                self.child.write(
                    data
                )

            return True

        except Exception as exc:

            print()
            print(
                "[NEXUS] "
                "Erro escrevendo no PTY:"
            )

            print(
                exc
            )

            return False

    # ========================================================
    # RESIZE
    # ========================================================

    def resize(
        self,
        rows,
        cols,
    ):

        if self.child is None:

            return

        try:

            self.child.setwinsize(
                rows,
                cols,
            )

        except Exception:

            pass

    # ========================================================
    # DRAIN
    # ========================================================

    def drain(self):

        chunks = []

        while True:

            try:

                chunks.append(
                    self.output_queue.get_nowait()
                )

            except queue.Empty:

                break

        if not chunks:

            return b""

        data = b"".join(
            chunks
        )

        return data

    # ========================================================
    # RUN COMMAND
    # ========================================================

    def run_command_and_capture(
        self,
        command,
        timeout=120,
    ):

        command = clean_shell_input(
            command
        )

        marker = (
            "__NEXUS_EXIT_"
            + str(os.getpid())
            + "_"
            + str(time.time_ns())
            + "__"
        )

        # ----------------------------------------------------
        # COMMAND
        # ----------------------------------------------------

        payload = (
            command
            + "\n"
            + "printf '\\n"
            + marker
            + ":%s\\n' $?\n"
        )

        # ----------------------------------------------------
        # LIMPAR BUFFER
        # ----------------------------------------------------

        self.drain()

        # ----------------------------------------------------
        # ENVIAR
        # ----------------------------------------------------

        self.write(
            payload
        )

        captured = b""

        deadline = (
            time.time()
            + timeout
        )

        pattern = re.compile(

            rb"\n"
            + re.escape(
                marker.encode()
            )
            + rb":(-?\d+)\r?\n"
        )

        # ----------------------------------------------------
        # CAPTURAR
        # ----------------------------------------------------

        while (
            time.time()
            < deadline
        ):

            captured += self.drain()

            match = pattern.search(
                captured
            )

            if match:

                exit_code = int(
                    match.group(1)
                )

                output = (
                    captured[
                        :match.start()
                    ]
                )

                # ------------------------------------------------
                # LIMPEZA DE ESCAPES
                # ------------------------------------------------

                output = self.clean_output(
                    output
                )

                return (
                    output,
                    exit_code,
                )

            time.sleep(
                0.03
            )

        captured = self.clean_output(
            captured
        )

        return (
            captured,
            None,
        )

    # ========================================================
    # CLEAN OUTPUT
    # ========================================================

    @staticmethod
    def clean_output(
        data,
    ):

        if not data:

            return b""

        try:

            text = data.decode(
                "utf-8",
                errors="replace",
            )

        except Exception:

            return data

        # ----------------------------------------------------
        # BRACKETED PASTE
        # ----------------------------------------------------

        text = text.replace(
            "\x1b[200~",
            "",
        )

        text = text.replace(
            "\x1b[201~",
            "",
        )

        # ----------------------------------------------------
        # REMOVER CÓDIGOS ANSI DE CONTROLE
        # ----------------------------------------------------

        text = ANSI_ESCAPE_RE.sub(
            "",
            text,
        )

        return text.encode(
            "utf-8",
            errors="replace",
        )

    # ========================================================
    # STOP
    # ========================================================

    def stop(self):

        self.alive = False

        try:

            if self.child is not None:

                self.child.close(
                    force=True
                )

        except Exception:

            pass


# ============================================================
# GEMINI AI PROVIDER
# ============================================================

class AIProvider:

    def __init__(
        self,
        config,
    ):

        self.config = config

    # ========================================================
    # ASK
    # ========================================================

    def ask(
        self,
        user_message,
        context,
    ):

        # ----------------------------------------------------
        # API KEY
        # ----------------------------------------------------

        key = normalize_api_key(
            self.config.get(
                "api_key",
                "",
            )
        )

        if not key:

            key = normalize_api_key(
                os.environ.get(
                    "GEMINI_API_KEY",
                    "",
                )
            )

        if not key:

            raise RuntimeError(

                "Gemini API key "
                "não configurada.\n\n"

                "Configure com:\n"

                "export GEMINI_API_KEY="
                "\"SUA_CHAVE\"\n\n"

                "ou use:\n"
                "/config"
            )

        # ----------------------------------------------------
        # MODEL
        # ----------------------------------------------------

        model = clean_terminal_input(
            self.config.get(
                "model",
                DEFAULT_CONFIG["model"],
            )
        )

        if not model:

            model = DEFAULT_CONFIG[
                "model"
            ]

        # ----------------------------------------------------
        # URL FIXA
        # ----------------------------------------------------

        api_url = build_gemini_url(
            model
        )

        # ----------------------------------------------------
        # SYSTEM
        # ----------------------------------------------------

        system_instruction = (

            SYSTEM_PROMPT

            + "\n\n"

            + "CONTEXTO ATUAL DO TERMINAL:\n"

            + json.dumps(
                context,
                ensure_ascii=False,
                indent=2,
            )
        )

        # ----------------------------------------------------
        # CONTENTS
        # ----------------------------------------------------

        contents = [

            {
                "role": "user",

                "parts": [

                    {
                        "text": user_message
                    }

                ],
            }

        ]

        # ----------------------------------------------------
        # PAYLOAD
        # ----------------------------------------------------

        payload = {

            "system_instruction": {

                "parts": [

                    {
                        "text":
                            system_instruction
                    }

                ]

            },

            "contents": contents,

            "generationConfig": {

                "temperature": float(

                    self.config.get(
                        "temperature",
                        0.2,
                    )

                ),

                "maxOutputTokens": int(

                    self.config.get(
                        "max_output_tokens",
                        4096,
                    )

                ),

                "candidateCount": 1,
            },
        }

        # ----------------------------------------------------
        # HEADERS
        # ----------------------------------------------------

        headers = {

            "Content-Type":
                "application/json",

            "x-goog-api-key":
                key,
        }

        # ----------------------------------------------------
        # LOG
        # ----------------------------------------------------

        print()

        print(
            "[NEXUS] Consultando Gemini..."
        )

        print(
            f"[NEXUS] Model: {model}"
        )

        # ----------------------------------------------------
        # REQUEST
        # ----------------------------------------------------

        try:

            response = requests.post(

                api_url,

                headers=headers,

                json=payload,

                timeout=120,
            )

        except requests.exceptions.Timeout:

            raise RuntimeError(

                "Timeout: o Gemini não "
                "respondeu em 120 segundos."
            )

        except requests.exceptions.ConnectionError as exc:

            raise RuntimeError(

                "Erro de conexão com Gemini:\n"
                + str(exc)
            )

        except requests.exceptions.RequestException as exc:

            raise RuntimeError(

                "Erro de comunicação com Gemini:\n"
                + str(exc)
            )

        # ----------------------------------------------------
        # HTTP ERROR
        # ----------------------------------------------------

        if response.status_code >= 400:

            try:

                error_data = response.json()

                error_text = json.dumps(

                    error_data,

                    ensure_ascii=False,

                    indent=2,
                )

            except Exception:

                error_text = (

                    response.text

                    or "(resposta vazia)"
                )

            raise RuntimeError(

                "Gemini retornou HTTP "

                + str(
                    response.status_code
                )

                + ":\n"

                + error_text[:10000]
            )

        # ----------------------------------------------------
        # JSON
        # ----------------------------------------------------

        try:

            data = response.json()

        except Exception:

            raise RuntimeError(

                "Gemini respondeu algo "
                "que não é JSON:\n"

                + response.text[:10000]
            )

        # ----------------------------------------------------
        # EXTRAR TEXTO
        # ----------------------------------------------------

        try:

            candidates = data.get(
                "candidates",
                []
            )

            if not candidates:

                raise RuntimeError(

                    "Gemini retornou "
                    "candidates vazio:\n"

                    + json.dumps(

                        data,

                        ensure_ascii=False,

                        indent=2,
                    )[:10000]
                )

            candidate = candidates[0]

            content = candidate.get(
                "content",
                {}
            )

            parts = content.get(
                "parts",
                []
            )

            text_parts = []

            for part in parts:

                if not isinstance(
                    part,
                    dict,
                ):

                    continue

                text = part.get(
                    "text"
                )

                if text:

                    text_parts.append(
                        str(text)
                    )

            result = "\n".join(
                text_parts
            ).strip()

            if not result:

                raise RuntimeError(

                    "Gemini não "
                    "retornou texto:\n"

                    + json.dumps(

                        data,

                        ensure_ascii=False,

                        indent=2,
                    )[:10000]
                )

            return result

        except RuntimeError:

            raise

        except Exception as exc:

            raise RuntimeError(

                "Erro interpretando "
                "resposta do Gemini:\n"

                + str(exc)

                + "\n\nResposta:\n"

                + json.dumps(

                    data,

                    ensure_ascii=False,

                    indent=2,
                )[:10000]
            )


# ============================================================
# ACTION PARSER
# ============================================================

def extract_actions(
    text,
):

    actions = []

    pattern = re.compile(

        r"<ACTION>\s*(.*?)\s*</ACTION>",

        re.DOTALL
        | re.IGNORECASE,
    )

    for match in pattern.finditer(
        text
    ):

        raw = (
            match.group(1)
            .strip()
        )

        try:

            obj = json.loads(
                raw
            )

            command = obj.get(
                "command"
            )

            if (
                isinstance(
                    command,
                    str,
                )
                and command.strip()
            ):

                actions.append(
                    command.strip()
                )

        except json.JSONDecodeError:

            continue

    return actions


# ============================================================
# SEGURANÇA
# ============================================================

def is_dangerous(
    command,
):

    normalized = (
        command
        .lower()
        .strip()
    )

    dangerous_patterns = [

        r"\brm\s+(-[a-z]*f[a-z]*\s+)?/",

        r"\brm\s+-rf\s+\*",

        r"\brm\s+-fr\s+\*",

        r"\bmkfs(\.|$)",

        r"\bdd\s+.*\bof=/dev/",

        r"\bparted\b",

        r"\bfdisk\b",

        r"\bformat\b",

        r"\bshutdown\b",

        r"\breboot\b",

        r"\bpoweroff\b",

        r":\(\)\s*\{",
    ]

    return any(

        re.search(
            pattern,
            normalized,
        )

        for pattern
        in dangerous_patterns
    )


# ============================================================
# NEXUS APPLICATION
# ============================================================

class NexusApp:

    def __init__(self):

        self.config = load_config()

        self.pty = RealPTY()

        self.ai = AIProvider(
            self.config
        )

        self.running = True

        self.auto = False

        self.assist = False

        self.last_output = ""

        self.last_exit = None

        self.agent_stop = threading.Event()

    # ========================================================
    # BANNER
    # ========================================================

    def print_banner(
        self,
    ):

        print(
            """
╔══════════════════════════════════════════════════════════╗
║                    NEXUS TERMINAL                       ║
║                      WHITE RAT                          ║
║                                                          ║
║  Linux PTY REAL  •  Gemini API  •  AI Agent             ║
╚══════════════════════════════════════════════════════════╝

Shell : {shell}
CWD   : {cwd}
AI    : Google Gemini

Comandos NEXUS:

  /nexus <pergunta>
  /nexus --auto <tarefa>
  /nexus --assist <tarefa>
  /nexus stop

  /config
  /status
  /clear
  /exit

O terminal abaixo é um shell Linux REAL.

""".format(

                shell=self.pty.shell,

                cwd=os.getcwd(),
            )
        )

    # ========================================================
    # CONTEXTO
    # ========================================================

    def context(
        self,
    ):

        max_chars = int(

            self.config.get(
                "max_output_chars",
                12000,
            )
        )

        return {

            "shell":
                self.pty.shell,

            "cwd":
                os.getcwd(),

            "user":
                os.environ.get(
                    "USER",
                    "",
                ),

            "last_exit_code":
                self.last_exit,

            "terminal_output":
                self.last_output[
                    -max_chars:
                ],
        }

    # ========================================================
    # EXECUTE AGENT COMMAND
    # ========================================================

    def execute_agent_command(
        self,
        command,
    ):

        command = clean_shell_input(
            command
        )

        print()

        print(
            "\033[1;36m"
            "WHITE RAT →"
            "\033[0m"
        )

        print(
            "\033[1m$ "
            + command
            + "\033[0m"
        )

        # ----------------------------------------------------
        # PERIGOSO
        # ----------------------------------------------------

        if is_dangerous(
            command
        ):

            print()

            print(
                "\033[1;31m"
                "COMANDO POTENCIALMENTE PERIGOSO."
                "\033[0m"
            )

            answer = input(
                "Executar mesmo assim? [sim/N]: "
            )

            answer = clean_terminal_input(
                answer
            ).lower()

            if answer not in (
                "s",
                "sim",
                "y",
                "yes",
            ):

                print(
                    "Execução cancelada."
                )

                return (
                    "",
                    None,
                )

        # ----------------------------------------------------
        # EXECUÇÃO
        # ----------------------------------------------------

        output, code = (

            self.pty.run_command_and_capture(

                command,

                timeout=120,
            )
        )

        text = output.decode(

            "utf-8",

            errors="replace",
        )

        self.last_output = text

        self.last_exit = code

        if text:

            print(

                text,

                end=(

                    ""

                    if text.endswith(
                        "\n"
                    )

                    else "\n"
                ),
            )

        print()

        print(

            "\033[90m"

            f"[exit code: {code}]"

            "\033[0m"
        )

        return (
            text,
            code,
        )

    # ========================================================
    # AGENTE
    # ========================================================

    def ask_agent(
        self,
        request,
        mode="normal",
    ):

        self.agent_stop.clear()

        conversation = request

        for step in range(
            12
        ):

            if self.agent_stop.is_set():

                print(
                    "\n[NEXUS] "
                    "Agente interrompido."
                )

                return

            ctx = self.context()

            ctx[
                "execution_mode"
            ] = mode.upper()

            ctx[
                "agent_step"
            ] = step + 1

            ctx[
                "max_agent_steps"
            ] = 12

            try:

                answer = self.ai.ask(

                    conversation,

                    ctx,
                )

            except Exception as exc:

                print()

                print(
                    "\033[1;31m"
                    "[GEMINI ERROR]"
                    "\033[0m"
                )

                print(
                    exc
                )

                return

            print()

            print(
                "\033[1;35m"
                "WHITE RAT:"
                "\033[0m"
            )

            print(
                answer
            )

            actions = extract_actions(
                answer
            )

            if not actions:

                return

            for command in actions:

                if self.agent_stop.is_set():

                    return

                # ============================================
                # AUTO
                # ============================================

                if mode == "auto":

                    output, code = (

                        self.execute_agent_command(

                            command
                        )
                    )

                # ============================================
                # NORMAL / ASSIST
                # ============================================

                else:

                    print()

                    print(
                        "Executar comando?"
                    )

                    print(
                        "  $ "
                        + command
                    )

                    choice = input(
                        "[s]im [n]ão "
                        "[a]uto [x]parar: "
                    )

                    choice = (
                        clean_terminal_input(
                            choice
                        ).lower()
                    )

                    if choice in (
                        "x",
                        "stop",
                    ):

                        print(
                            "Agente interrompido."
                        )

                        return

                    if choice in (
                        "a",
                        "auto",
                    ):

                        mode = "auto"

                        output, code = (

                            self.execute_agent_command(

                                command
                            )
                        )

                    elif choice in (
                        "s",
                        "sim",
                        "y",
                        "yes",
                    ):

                        output, code = (

                            self.execute_agent_command(

                                command
                            )
                        )

                    else:

                        print(
                            "Comando recusado."
                        )

                        return

                # ============================================
                # RESULTADO
                # ============================================

                conversation = (

                    "O comando foi executado "
                    "no terminal Linux REAL.\n\n"

                    "COMMAND:\n"

                    + command

                    + "\n\n"

                    "EXIT CODE:\n"

                    + str(code)

                    + "\n\n"

                    "REAL OUTPUT:\n"

                    + output[-12000:]

                    + "\n\n"

                    "Analise SOMENTE o resultado real "
                    "acima.\n\n"

                    "Se for necessário continuar, "
                    "produza a próxima ACTION.\n\n"

                    "Se a tarefa estiver concluída, "
                    "responda normalmente sem ACTION."
                )

    # ========================================================
    # /NEXUS
    # ========================================================

    def process_nexus(
        self,
        line,
    ):

        parts = line.strip().split(
            maxsplit=2
        )

        if len(parts) == 1:

            print(
                "Uso:"
            )

            print(
                "  /nexus <pergunta>"
            )

            print(
                "  /nexus --auto <tarefa>"
            )

            print(
                "  /nexus --assist <tarefa>"
            )

            print(
                "  /nexus stop"
            )

            return

        # ----------------------------------------------------
        # STOP
        # ----------------------------------------------------

        if parts[1] == "stop":

            self.agent_stop.set()

            print(
                "[NEXUS] "
                "Sinal de parada enviado."
            )

            return

        # ----------------------------------------------------
        # AUTO
        # ----------------------------------------------------

        if parts[1] == "--auto":

            request = (

                parts[2]

                if len(parts) > 2

                else ""
            )

            if not request:

                print(
                    "Informe a tarefa."
                )

                return

            self.ask_agent(
                request,
                "auto",
            )

            return

        # ----------------------------------------------------
        # ASSIST
        # ----------------------------------------------------

        if parts[1] == "--assist":

            request = (

                parts[2]

                if len(parts) > 2

                else ""
            )

            if not request:

                print(
                    "Informe a tarefa."
                )

                return

            self.ask_agent(
                request,
                "assist",
            )

            return

        # ----------------------------------------------------
        # NORMAL
        # ----------------------------------------------------

        request = line.strip()[

            len("/nexus"):
        ].strip()

        self.ask_agent(
            request,
            "normal",
        )

    # ========================================================
    # COMANDOS
    # ========================================================

    def command(
        self,
        line,
    ):

        # ----------------------------------------------------
        # LIMPAR INPUT DO USUÁRIO
        # ----------------------------------------------------

        line = clean_shell_input(
            line
        )

        stripped = line.strip()

        if not stripped:

            return

        # ----------------------------------------------------
        # EXIT
        # ----------------------------------------------------

        if stripped == "/exit":

            self.running = False

            return

        # ----------------------------------------------------
        # CLEAR
        # ----------------------------------------------------

        if stripped == "/clear":

            os.system(
                "clear"
            )

            return

        # ----------------------------------------------------
        # CONFIG
        # ----------------------------------------------------

        if stripped == "/config":

            configure()

            self.config = load_config()

            self.ai = AIProvider(
                self.config
            )

            return

        # ----------------------------------------------------
        # STATUS
        # ----------------------------------------------------

        if stripped == "/status":

            key_configured = bool(

                self.config.get(
                    "api_key"
                )

                or

                os.environ.get(
                    "GEMINI_API_KEY"
                )
            )

            model = self.config.get(
                "model",
                DEFAULT_CONFIG["model"],
            )

            print()

            print(
                "NEXUS STATUS"
            )

            print(
                "  Version: "
                + APP_VERSION
            )

            print(
                "  PTY: "
                + (

                    "ONLINE"

                    if self.pty.alive

                    else "OFFLINE"
                )
            )

            print(
                "  Shell: "
                + self.pty.shell
            )

            print(
                "  CWD: "
                + os.getcwd()
            )

            print(
                "  Provider: Google Gemini"
            )

            print(
                "  API URL: "
                + build_gemini_url(
                    model
                )
            )

            print(
                "  Model: "
                + model
            )

            print(
                "  API Key: "
                + (

                    "CONFIGURADA"

                    if key_configured

                    else "NÃO CONFIGURADA"
                )
            )

            print(
                "  Temperature: "
                + str(

                    self.config.get(
                        "temperature",
                        0.2,
                    )
                )
            )

            print(
                "  Max tokens: "
                + str(

                    self.config.get(
                        "max_output_tokens",
                        4096,
                    )
                )
            )

            print()

            return

        # ----------------------------------------------------
        # NEXUS
        # ----------------------------------------------------

        if stripped.startswith(
            "/nexus"
        ):

            self.process_nexus(
                stripped
            )

            return

        # ----------------------------------------------------
        # SHELL REAL
        # ----------------------------------------------------

        self.pty.write(
            line
        )

    # ========================================================
    # LOOP
    # ========================================================

    def run(
        self,
    ):

        self.pty.start()

        self.print_banner()

        while (

            self.running

            and

            self.pty.alive
        ):

            # ------------------------------------------------
            # OUTPUT
            # ------------------------------------------------

            output = self.pty.drain()

            if output:

                text = output.decode(

                    "utf-8",

                    errors="replace",
                )

                self.last_output = (
                    text[-12000:]
                )

                sys.stdout.write(
                    text
                )

                sys.stdout.flush()

            # ------------------------------------------------
            # INPUT
            # ------------------------------------------------

            ready = select.select(

                [sys.stdin],

                [],

                [],

                0.05,
            )[0]

            if sys.stdin in ready:

                line = sys.stdin.readline()

                if not line:

                    break

                self.command(
                    line
                )

        self.pty.stop()


# ============================================================
# MAIN
# ============================================================

def main():

    parser = argparse.ArgumentParser(

        description=(

            "NEXUS Terminal - "

            "Linux PTY real + "

            "Google Gemini AI Agent"
        )
    )

    parser.add_argument(

        "--config",

        action="store_true",

        help="configurar Gemini API",
    )

    parser.add_argument(

        "--version",

        action="store_true",

        help="mostrar versão",
    )

    args = parser.parse_args()

    # --------------------------------------------------------
    # VERSION
    # --------------------------------------------------------

    if args.version:

        print(

            f"{APP_NAME} "

            f"{APP_VERSION}"
        )

        return

    # --------------------------------------------------------
    # CONFIG
    # --------------------------------------------------------

    if args.config:

        configure()

        return

    # --------------------------------------------------------
    # APP
    # --------------------------------------------------------

    app = NexusApp()

    # --------------------------------------------------------
    # CTRL+C
    # --------------------------------------------------------

    def sigint_handler(
        signum,
        frame,
    ):

        app.agent_stop.set()

        if app.pty.alive:

            app.pty.write(
                "\x03"
            )

    signal.signal(

        signal.SIGINT,

        sigint_handler,
    )

    # --------------------------------------------------------
    # RUN
    # --------------------------------------------------------

    try:

        app.run()

    except KeyboardInterrupt:

        pass

    finally:

        app.pty.stop()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":

    main()
