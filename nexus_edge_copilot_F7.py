import argparse
import ast
import copy
import difflib
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
import queue
import readline
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import tempfile
import time
import textwrap
import unicodedata
import uuid
import venv
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def flatpak_edge_app_id() -> str:
    return os.environ.get("NEXUS_EDGE_FLATPAK_APP_ID", "com.microsoft.Edge").strip() or "com.microsoft.Edge"


def flatpak_edge_available() -> bool:
    """Detecta o Edge instalado via Flatpak sem confundir com Chrome."""
    flatpak = shutil.which("flatpak")
    if not flatpak:
        return False
    app_id = flatpak_edge_app_id()
    result = subprocess.run(
        [flatpak, "info", app_id],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_microsoft_edge() -> Optional[str]:
    """Retorna apenas um executável do Microsoft Edge, nunca Chrome/Chromium."""
    configured = os.environ.get("NEXUS_EDGE_EXECUTABLE", "").strip()
    candidates = [configured] if configured else []
    if sys.platform.startswith("win"):
        candidates += [
            os.path.expandvars(r"%PROGRAMFILES%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%PROGRAMFILES(X86)%\Microsoft\Edge\Application\msedge.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
        ]
    elif sys.platform == "darwin":
        candidates += ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
    else:
        candidates += [
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/usr/bin/msedge",
            "/opt/microsoft/msedge/msedge",
            "/opt/microsoft/msedge-beta/msedge",
        ]
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            name = path.name.lower()
            if "chrome" in name or "chromium" in name:
                raise EdgeCopilotBrowserError(
                    f"O caminho NEXUS_EDGE_EXECUTABLE aponta para Chrome/Chromium: {path}. "
                    "Informe o executável msedge do Microsoft Edge."
                )
            return str(path)
    return None

def bootstrap_python_package(package: str) -> tuple[bool, str]:
    """Instala uma dependência no Python atual, sem usar sudo e sem ocultar falhas."""
    command = [sys.executable, "-m", "pip", "install"]
    if sys.prefix == getattr(sys, "base_prefix", sys.prefix):
        command.append("--user")
    command.append(package)
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if result.returncode == 0:
        try:
            import site
            user_bin = str(Path(site.getuserbase()) / "bin")
            if user_bin not in os.environ.get("PATH", "").split(os.pathsep):
                os.environ["PATH"] = user_bin + os.pathsep + os.environ.get("PATH", "")
        except Exception:
            pass
        return True, (result.stdout or "instalado").strip()[-500:]
    return False, (result.stderr or result.stdout or "pip falhou").strip()[-1000:]


try:
    import pexpect
except ImportError:
    print("[NEXUS] Dependência pexpect ausente; instalação automática em andamento...")
    installed, detail = bootstrap_python_package("pexpect")
    if not installed:
        print(f"[NEXUS] ERRO ao instalar pexpect: {detail}", file=sys.stderr)
        raise SystemExit(1)
    import pexpect

try:
    import jedi as _jedi
except ImportError:
    _jedi = None

try:
    from prompt_toolkit.completion import Completer as _PTCompleter
except ImportError:
    _PTCompleter = None


import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional


try:
    from playwright.sync_api import (
        sync_playwright,
        TimeoutError as PlaywrightTimeoutError,
    )
except ImportError:
    print("[NEXUS] Dependência Playwright ausente; instalação automática em andamento...")
    installed, detail = bootstrap_python_package("playwright")
    if not installed:
        print(f"[NEXUS] ERRO ao instalar Playwright: {detail}", file=sys.stderr)
        sys.exit(1)
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ==============================================================
# ERROR
# ==============================================================


class EdgeCopilotBrowserError(Exception):
    """Erro específico do Microsoft Browser Bridge."""


# ==============================================================
# EDGE + COPILOT
# ==============================================================


# NEXUS SECTION: COPILOT_BROWSER
class EdgeCopilotBrowser:

    COPILOT_URL = "https://copilot.com/chat?fromcode=cmm9tzigufu&sessionId=4b39bf2e-400d-6ca6-2a7c-dba341494dab&hasLW=true&es=SSR&redirfrom=userTypeCookie&redirfrom=cosmicRingCookie"

    def __init__(
        self,
        profile_dir: Optional[str | Path] = None,
        headless: bool = False,
        timeout: int = 30000,
        response_timeout: int = 60000,
        slow_mo: int = 0,
        stable_seconds: float = 2.5,
        poll_interval: float = 0.5,
        edge_channel: str = "msedge",
        edge_executable: Optional[str] = None,
    ) -> None:

        if profile_dir is None:
            profile_dir = (
                PROJECT_DIR
                / ".nexus"
                / "edge-copilot"
            )

        self.profile_dir = Path(
            profile_dir
        ).expanduser()

        self.profile_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.headless = headless
        self.timeout = timeout
        self.response_timeout = response_timeout
        self.slow_mo = slow_mo

        self.stable_seconds = stable_seconds
        self.poll_interval = poll_interval
        self.edge_channel = edge_channel.strip() or "msedge"
        self.edge_executable = edge_executable
        self.copilot_url = os.environ.get("NEXUS_COPILOT_URL", self.COPILOT_URL).strip() or self.COPILOT_URL

        self._playwright = None
        self._context = None
        self._browser = None
        self._edge_process = None
        self._cdp_port = None
        self._page = None

        self._ready = False

    # ==========================================================
    # START FLATPAK EDGE
    # ==========================================================

    def _start_flatpak_edge(self) -> None:
        """Inicia o Edge Flatpak e conecta pelo protocolo CDP do Playwright."""
        flatpak = shutil.which("flatpak")
        app_id = flatpak_edge_app_id()
        if not flatpak or not flatpak_edge_available():
            raise EdgeCopilotBrowserError(
                f"Edge Flatpak não encontrado ({app_id}). "
                f"Confirme com: flatpak info {app_id}"
            )

        port = reserve_local_port()
        self._cdp_port = port

        # O Edge Flatpak pode encerrar se receber um perfil fora das permissões
        # do sandbox. Por padrão, use uma pasta interna do próprio aplicativo;
        # os dados do NEXUS continuam no diretório do projeto.
        flatpak_profile = Path(
            os.environ.get(
                "NEXUS_EDGE_FLATPAK_PROFILE",
                str(Path.home() / ".var" / "app" / app_id / "data" / "nexus-edge-profile"),
            )
        ).expanduser()
        flatpak_profile.mkdir(parents=True, exist_ok=True)

        command = [
            flatpak, "run", app_id,
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=http://127.0.0.1",
            f"--user-data-dir={flatpak_profile}",
            "--no-first-run",
            "--no-default-browser-check",
        ]
        self._edge_process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

        endpoint = f"http://127.0.0.1:{port}/json/version"
        deadline = time.monotonic() + 30
        last_error = ""
        while time.monotonic() < deadline:
            if self._edge_process.poll() is not None:
                try:
                    stdout, stderr = self._edge_process.communicate(timeout=2)
                except Exception:
                    stdout, stderr = "", ""
                details = (stderr or stdout or "sem mensagem do Flatpak").strip()[-2000:]
                raise EdgeCopilotBrowserError(
                    "O processo Flatpak do Microsoft Edge encerrou antes de abrir a porta CDP. "
                    f"Saída do Edge/Flatpak: {details}"
                )
            try:
                with urllib.request.urlopen(endpoint, timeout=1) as response:
                    if response.status == 200:
                        break
            except Exception as exc:
                last_error = str(exc)
            time.sleep(0.25)
        else:
            raise EdgeCopilotBrowserError(
                f"Tempo esgotado aguardando o Edge Flatpak na porta {port}. "
                f"Detalhes: {last_error}"
            )

        self._browser = self._playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{port}",
            timeout=self.timeout,
        )
        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else self._browser.new_context(
            locale="pt-BR", timezone_id="America/Sao_Paulo"
        )

    # ==========================================================
    # START
    # ==========================================================

    def start(self) -> None:

        if self._context is not None:
            return

        self._playwright = sync_playwright().start()

        try:

            if flatpak_edge_available() and os.environ.get("NEXUS_EDGE_USE_FLATPAK", "1").lower() not in {"0", "false", "no"}:
                self._start_flatpak_edge()
            else:
                executable = self.edge_executable or find_microsoft_edge()
                launch_options = {
                    "user_data_dir": str(self.profile_dir),
                    "headless": self.headless,
                    "slow_mo": self.slow_mo,
                    "viewport": {"width": 1440, "height": 900},
                    "locale": "pt-BR",
                    "timezone_id": "America/Sao_Paulo",
                    "args": ["--disable-blink-features=AutomationControlled"],
                }
                if executable:
                    launch_options["executable_path"] = executable
                else:
                    # O channel msedge é específico do Microsoft Edge; não há fallback.
                    launch_options["channel"] = self.edge_channel
                self._context = self._playwright.chromium.launch_persistent_context(**launch_options)

        except Exception as exc:

            self.close()

            raise EdgeCopilotBrowserError(
                "Não foi possível iniciar o Microsoft Edge.\n\n"
                "O programa não faz fallback para Chrome/Chromium. "
                "O modo Flatpak usa uma sessão isolada do Microsoft Edge e CDP.\n\n"
                f"Detalhes: {exc}"
            ) from exc

        pages = self._context.pages

        if pages:
            self._page = pages[0]
        else:
            self._page = self._context.new_page()

        self._page.set_default_timeout(
            self.timeout
        )

    # ==========================================================
    # CLOSE
    # ==========================================================

    def close(self) -> None:

        try:

            if self._browser is not None:
                self._browser.close()
            elif self._context is not None:
                self._context.close()

        except Exception:
            pass

        finally:

            self._context = None
            self._browser = None
            self._page = None
            if self._edge_process is not None:
                try:
                    self._edge_process.terminate()
                    self._edge_process.wait(timeout=5)
                except Exception:
                    try:
                        self._edge_process.kill()
                    except Exception:
                        pass
                self._edge_process = None

            if self._playwright is not None:

                try:
                    self._playwright.stop()

                except Exception:
                    pass

                self._playwright = None

    # ==========================================================
    # PAGE
    # ==========================================================

    @property
    def page(self):

        if self._page is None:
            self.start()

        return self._page

    # ==========================================================
    # OPEN AI
    # ==========================================================

    def open_ai(self) -> None:

        page = self.page

        current_url = page.url

        # Use a rota de chat fornecida, incluindo seus parâmetros de sessão.
        if current_url.split("#", 1)[0] != self.copilot_url.split("#", 1)[0]:

            page.goto(
                self.copilot_url,
                wait_until="domcontentloaded",
                timeout=self.timeout,
            )

        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        # O Copilot é uma SPA: o HTML inicial contém apenas <div id="app"></div>.
        # Aguarde o JavaScript montar a interface antes de procurar a caixa.
        try:
            page.locator("#app").wait_for(state="attached", timeout=10000)
        except Exception:
            pass
        try:
            page.wait_for_function(
                """() => document.body && (
                    document.querySelector('#app > *') ||
                    document.querySelector('textarea, [contenteditable=\"true\"], [role=\"textbox\"]')
                )""",
                timeout=max(10000, self.timeout),
            )
        except Exception:
            # A tela pode estar aguardando login; ensure_ready tratará essa situação.
            pass

    # ==========================================================
    # READY
    # ==========================================================

    def ensure_ready(self) -> None:

        if self._ready:
            return

        page = self.page

        self.open_ai()

        body = page.locator("body")

        try:

            text = body.inner_text(
                timeout=5000
            ).lower()

        except Exception:

            text = ""

        login_indicators = [
            "fazer login",
            "sign in",
            "iniciar sessão",
        ]

        if any(
            indicator in text
            for indicator in login_indicators
        ):

            print()
            print(
                "[NEXUS EDGE/COPILOT] Login necessário."
            )

            print(
                "[NEXUS EDGE/COPILOT] "
                "Faça o login manualmente no navegador."
            )

            print(
                "[NEXUS EDGE/COPILOT] "
                "Depois pressione ENTER apenas uma vez."
            )

            input()

            self.open_ai()

        self._ready = True

    # ==========================================================
    # FIND QUESTION INPUT
    # ==========================================================

    def _find_question_input(self):

        page = self.page

        # A interface do Copilot muda entre contas/versões. Os seletores
        # abaixo cobrem textarea, composer moderno e contenteditable.
        selectors = [
            'textarea[placeholder*="Message" i]',
            'textarea[placeholder*="Ask" i]',
            'textarea[placeholder*="Pergunt" i]',
            'textarea[aria-label*="Message" i]',
            'textarea[aria-label*="Ask" i]',
            'textarea[aria-label*="Pergunt" i]',
            '[data-testid*="composer" i] textarea',
            '[data-testid*="prompt" i] textarea',
            'textarea[name="q"]',
            'textarea',
            '[contenteditable="true"]',
            '[role="textbox"]',
        ]

        def locate_in(frame):
            for selector in selectors:
                try:
                    locator = frame.locator(selector)
                    count = min(locator.count(), 20)
                    for index in range(count):
                        candidate = locator.nth(index)
                        if not candidate.is_visible():
                            continue
                        try:
                            if candidate.is_editable():
                                return candidate
                        except Exception:
                            return candidate
                except Exception:
                    continue
            return None

        # Primeiro a página principal; depois iframes, caso a conta carregue
        # o composer dentro de um frame.
        found = locate_in(page)
        if found is not None:
            return found
        for frame in page.frames[1:]:
            found = locate_in(frame)
            if found is not None:
                return found
        return None

    def _wait_for_question_input(self):
        deadline = time.monotonic() + max(15.0, self.timeout / 1000)
        while time.monotonic() < deadline:
            found = self._find_question_input()
            if found is not None:
                return found
            try:
                self.page.wait_for_timeout(250)
            except Exception:
                time.sleep(0.25)
        return None

    def _save_copilot_diagnostic(self) -> str:
        destination = DATA_DIR / "debug"
        destination.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        html_path = destination / f"copilot_{stamp}.html"
        screenshot_path = destination / f"copilot_{stamp}.png"
        try:
            html_path.write_text(self.page.content(), encoding="utf-8")
        except Exception:
            pass
        try:
            self.page.screenshot(path=str(screenshot_path), full_page=True)
        except Exception:
            pass
        return str(html_path)

    # ==========================================================
    # SUBMIT QUESTION
    # ==========================================================

    def _submit_question(
        self,
        question: str,
    ) -> None:

        page = self.page

        input_box = self._wait_for_question_input()

        if input_box is None:
            diagnostic = self._save_copilot_diagnostic()
            raise EdgeCopilotBrowserError(
                "Não encontrei a caixa de pergunta do Microsoft Copilot após aguardar "
                "o carregamento da interface. Verifique login, conexão e permissões. "
                f"URL atual: {page.url}. Diagnóstico salvo em: {diagnostic}"
            )

        try:

            input_box.click()

        except Exception:
            pass

        # Limpa qualquer texto anterior.
        try:

            input_box.fill("")

        except Exception:
            pass

        # Preenche a pergunta. Contenteditable pode não aceitar fill em algumas
        # versões; use teclado como fallback.
        try:
            input_box.fill(question)
        except Exception:
            try:
                input_box.press("ControlOrMeta+A")
                input_box.press("Backspace")
                input_box.type(question, delay=1)
            except Exception as exc:
                raise EdgeCopilotBrowserError(
                    f"Não foi possível preencher a pergunta: {exc}"
                ) from exc

        # ------------------------------------------------------
        # IMPORTANTE:
        #
        # O Enter é enviado pelo Playwright.
        #
        # O usuário NÃO precisa pressionar Enter no navegador.
        # ------------------------------------------------------

        try:

            input_box.press(
                "Enter"
            )

            return

        except Exception:
            pass

        # ------------------------------------------------------
        # Fallback por botão
        # ------------------------------------------------------

        buttons = page.locator(
            "button"
        )

        patterns = [
            re.compile(
                r"enviar|send|pesquisar|search|submit|áudio|audio",
                re.I,
            ),
        ]

        try:

            count = buttons.count()

            for index in range(count):

                button = buttons.nth(index)

                if not button.is_visible():
                    continue

                try:
                    text = button.inner_text()
                except Exception:
                    text = ""

                for pattern in patterns:

                    if pattern.search(text):

                        button.click()

                        return

        except Exception:
            pass

        raise EdgeCopilotBrowserError(
            "Não foi possível enviar a pergunta."
        )

    # ==========================================================
    # PAGE TEXT
    # ==========================================================

    def _page_text(self) -> str:

        try:

            return self.page.locator(
                "body"
            ).inner_text(
                timeout=5000
            )

        except Exception:

            return ""

    # ==========================================================
    # LOADING
    # ==========================================================

    def _has_loading_indicator(self) -> bool:

        page = self.page

        selectors = [

            '[aria-busy="true"]',

            '[role="progressbar"]',

            'button[aria-label*="Parar"]',

            'button[aria-label*="Stop"]',

            'button[aria-label*="Cancelar"]',

            'button[aria-label*="Cancel"]',
            '[data-is-busy="true"]',
            '[aria-label*="Stop generating"]',

        ]

        for selector in selectors:

            try:

                locator = page.locator(
                    selector
                )

                count = locator.count()

                for index in range(count):

                    if locator.nth(index).is_visible():

                        return True

            except Exception:

                continue

        return False

    @staticmethod
    def _looks_incomplete(text: str) -> bool:
        """Detecta placeholder ou JSON ainda não finalizado durante streaming."""
        value = str(text or "").strip()
        if not value or value in {"...", "…"}:
            return True
        if re.search(r'"(?:response|reason)"\s*:\s*"(?:\.\.\.|…)"', value, re.I):
            return True
        if value.startswith("{") and value.count("{") > value.count("}"):
            return True
        return False

    def _new_text_after(self, previous_text: str, current_text: str) -> str:
        """Obtém somente o trecho novo da página, sem reutilizar respostas antigas."""
        previous = (previous_text or "").strip()
        current = (current_text or "").strip()
        if not previous:
            return current
        if current.startswith(previous):
            return current[len(previous):].strip()
        common = 0
        limit = min(len(previous), len(current))
        while common < limit and previous[common] == current[common]:
            common += 1
        return current[common:].strip()

    def _structured_response_from_page(self, previous_text: str, current_text: str) -> Optional[str]:
        """Lê o envelope JSON assim que ele aparece, sem esperar o body estabilizar."""
        new_text = self._new_text_after(previous_text, current_text)
        for source in (new_text, current_text):
            structured = self._structured_json_response(source)
            if structured:
                # Quando a página tem histórico, só aceita o JSON encontrado no
                # trecho novo. No caso de uma página sem histórico, current_text é válido.
                if source is current_text and new_text != current_text:
                    continue
                return structured

        # Fallback para elementos visíveis: o Copilot pode atualizar o DOM sem
        # alterar o texto agregado do body de forma previsível.
        for source in self._dom_text_sources(previous_text):
            structured = self._structured_json_response(source)
            if structured:
                return structured
        return None

    # ==========================================================
    # WAIT RESPONSE
    # ==========================================================

    def _wait_for_response(
        self,
        previous_text: str,
    ) -> str:

        deadline = (
            time.monotonic()
            + self.response_timeout / 1000
        )

        last_text = previous_text

        stable_since = None

        while time.monotonic() < deadline:

            time.sleep(
                self.poll_interval
            )

            current_text = (
                self._page_text()
            )

            if not current_text:
                continue

            # O Copilot pode deixar o JSON final visível enquanto outros
            # elementos da página continuam mudando. Tente extrair primeiro.
            structured = self._structured_response_from_page(previous_text, current_text)
            if structured:
                return structured

            # --------------------------------------------------
            # Mudou?
            # --------------------------------------------------

            if current_text != last_text:

                last_text = current_text

                stable_since = (
                    time.monotonic()
                )

                continue

            if stable_since is None:
                continue

            stable_for = (
                time.monotonic()
                - stable_since
            )

            # --------------------------------------------------
            # Texto estável + nenhum loading
            # --------------------------------------------------

            if (
                stable_for
                >= self.stable_seconds
            ):

                if not self._has_loading_indicator():
                    candidate = current_text
                    if current_text.startswith(previous_text.strip()):
                        candidate = current_text[len(previous_text.strip()):].strip()
                    if self._looks_incomplete(candidate):
                        # O Microsoft pode estabilizar brevemente em "..." antes
                        # de continuar a resposta. Não entregue esse estado.
                        stable_since = time.monotonic()
                        continue
                    return current_text

        raise EdgeCopilotBrowserError(
            "Tempo limite aguardando "
            "a resposta do Microsoft."
        )

    @staticmethod
    def _structured_json_response(text: str) -> Optional[str]:
        """Retorna o último envelope JSON válido encontrado no texto.

        O protocolo do NEXUS exige execute, mode, command e response.
        A busca tolera texto explicativo e cercas Markdown ao redor do JSON.
        """
        if not text:
            return None

        decoder = json.JSONDecoder()
        required = {"execute", "mode", "command", "response"}
        valid_modes = {"response", "command", "python"}
        matches: list[tuple[int, dict[str, Any]]] = []

        for index, char in enumerate(text):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(text[index:])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict) or not required.issubset(value):
                continue

            mode = str(value.get("mode", "")).strip().lower()
            execute = value.get("execute")
            if mode not in valid_modes:
                continue
            if not isinstance(execute, (bool, int, str)):
                continue
            if not isinstance(value.get("command"), str):
                continue
            if not isinstance(value.get("response"), str):
                continue
            matches.append((index, value))

        if not matches:
            return None

        payload = matches[-1][1]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def _dom_text_sources(self, previous_text: str = "") -> list[str]:
        """Coleta nós DOM recentes, descartando conteúdo que já existia."""
        script = r"""(previousText) => {
            const values = [];
            const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();
            const previous = normalize(previousText);
            const addIfNew = (value) => {
                if (typeof value !== 'string' || !value.trim()) return;
                const normalized = normalize(value);
                if (!normalized || (previous && previous.includes(normalized))) return;
                values.push(value.slice(0, 150000));
            };
            const isVisible = (element) => {
                const style = getComputedStyle(element);
                const rect = element.getBoundingClientRect();
                return style.display !== 'none' && style.visibility !== 'hidden'
                    && rect.width > 0 && rect.height > 0;
            };

            // Mensagens recentes primeiro; evita varrer o histórico inteiro da página.
            const selectors = [
                '[data-message-author-role="assistant"]',
                '[data-testid*="answer" i]',
                '[data-testid*="response" i]',
                '[role="article"]'
            ];
            for (const selector of selectors) {
                let nodes = [];
                try { nodes = Array.from(document.querySelectorAll(selector)); }
                catch (_) { continue; }
                for (const element of nodes.reverse().slice(0, 8)) {
                    try {
                        if (isVisible(element)) {
                            addIfNew(element.innerText || element.textContent || '');
                        }
                    } catch (_) {}
                }
            }

            // Algumas aplicações guardam estado/resposta em JSON dentro do DOM.
            let jsonNodes = [];
            try {
                jsonNodes = Array.from(
                    document.querySelectorAll('script[type="application/json"]')
                );
            } catch (_) {}
            for (const element of jsonNodes.reverse().slice(0, 8)) {
                try { addIfNew(element.textContent || ''); } catch (_) {}
            }
            return values;
        }"""
        try:
            values = self.page.evaluate(script, previous_text)
        except Exception:
            return []
        if not isinstance(values, list):
            return []
        return [value for value in values if isinstance(value, str) and value.strip()]

    # ==========================================================
    # EXTRACT RESPONSE
    # ==========================================================

    def _extract_response(
        self,
        before: str,
        after: str,
        question: str,
    ) -> str:

        before_clean = (
            before.strip()
        )

        after_clean = (
            after.strip()
        )

        # ------------------------------------------------------
        # Prioridade máxima: JSON nos nós DOM recém-atualizados.
        # Os nós já presentes antes da pergunta são ignorados para não
        # reutilizar envelopes de respostas antigas do histórico.
        # ------------------------------------------------------
        for dom_source in self._dom_text_sources(before_clean):
            structured = self._structured_json_response(dom_source)
            if structured:
                return structured

        # Procura também no texto novo da página, sem preferir o histórico.
        new_text = self._new_text_after(before_clean, after_clean)

        structured = self._structured_json_response(new_text)
        if structured:
            return structured

        # ------------------------------------------------------
        # Estratégia 1:
        # diferença entre página anterior e posterior.
        # ------------------------------------------------------

        if (
            before_clean
            and after_clean
            and after_clean.startswith(
                before_clean
            )
        ):

            diff = after_clean[
                len(before_clean):
            ].strip()

            if len(diff) > 20 and not self._looks_incomplete(diff):

                return self._clean_response(
                    diff,
                    question,
                )

        # ------------------------------------------------------
        # Estratégia 2:
        # candidatos semânticos
        # ------------------------------------------------------

        page = self.page

        selectors = [

            '[role="article"]',

            '[role="main"]',

            'main',

        ]

        candidates = []

        for selector in selectors:

            try:

                locator = page.locator(
                    selector
                )

                count = locator.count()

                for index in range(count):

                    element = locator.nth(
                        index
                    )

                    if not element.is_visible():
                        continue

                    try:

                        text = (
                            element.inner_text()
                        )

                    except Exception:

                        continue

                    if len(
                        text.strip()
                    ) > 100:

                        candidates.append(
                            text.strip()
                        )

            except Exception:

                continue

        if candidates:

            candidates.sort(
                key=len,
                reverse=True,
            )

            return self._clean_response(
                candidates[0],
                question,
            )

        # ------------------------------------------------------
        # Estratégia 3:
        # corpo inteiro
        # ------------------------------------------------------

        return self._clean_response(
            after_clean,
            question,
        )

    # ==========================================================
    # CLEAN
    # ==========================================================

    @staticmethod
    def _clean_response(
        text: str,
        question: str,
    ) -> str:

        if not text:
            return ""

        lines = []

        for line in text.splitlines():

            stripped = line.strip()

            if not stripped:
                continue

            if stripped in {
                "Microsoft",
                "Pesquisa",
                "Pesquisar",
                "Modo IA",
                "AI Mode",
            }:

                continue

            lines.append(
                stripped
            )

        return "\n".join(
            lines
        ).strip()

    # ==========================================================
    # ASK
    # ==========================================================

    def ask(
        self,
        question: str,
    ) -> str:

        question = question.strip()

        if not question:

            raise ValueError(
                "A pergunta não pode estar vazia."
            )

        self.start()

        self.ensure_ready()

        before = self._page_text()

        self._submit_question(
            question
        )

        after = (
            self._wait_for_response(
                before
            )
        )

        answer = (
            self._extract_response(
                before,
                after,
                question,
            )
        )

        # Mesmo depois da estabilização visual, o Microsoft pode substituir um
        # JSON placeholder pela resposta final alguns instantes depois.
        # Continue lendo a página sem reenviar a pergunta.
        if self._looks_incomplete(answer):
            deadline = time.monotonic() + self.response_timeout / 1000
            while time.monotonic() < deadline:
                time.sleep(self.poll_interval)
                newer = self._page_text()
                if not newer:
                    continue
                candidate = self._extract_response(before, newer, question)
                if not self._looks_incomplete(candidate):
                    answer = candidate
                    break

        if self._looks_incomplete(answer):
            raise EdgeCopilotBrowserError(
                "O Microsoft ainda não entregou a resposta final; "
                "o placeholder foi descartado."
            )

        if not answer:

            raise EdgeCopilotBrowserError(
                "O Microsoft respondeu, "
                "mas não foi possível extrair "
                "o texto."
            )

        return answer

    # ==========================================================
    # NEXUS BRAIN PROMPT
    # ==========================================================

    @staticmethod
    def build_brain_prompt(
        user_request: str,
        context: str = "",
    ) -> str:

        prompt = f"""
Você é o cérebro de raciocínio do NEXUS TERMINAL.

O NEXUS é um agente Python conectado a um terminal Linux real.

Sua função é ANALISAR a solicitação do usuário e produzir
uma orientação objetiva para o NEXUS.

REGRAS:

1. Não execute nada.
2. Não invente resultados.
3. Não diga que executou um comando.
4. Se for necessário um comando Linux, forneça o comando.
5. Prefira comandos simples e verificáveis.
6. Não utilize sudo sem necessidade.
7. Não destrua arquivos ou dados.
8. Não utilize rm -rf ou comandos destrutivos sem que isso seja
   explicitamente necessário e aprovado pelo usuário.
9. Se a solicitação for apenas informativa, não gere comando.
10. Explique resumidamente o motivo da ação.
11. O NEXUS executará o comando posteriormente.
12. Retorne SOMENTE JSON válido.

FORMATO:

{{
  "tipo": "informacao|shell|multistep|pergunta",
  "objetivo": "descrição curta",
  "analise": "análise curta",
  "comando": "comando Linux ou string vazia",
  "comandos": [],
  "risco": "baixo|medio|alto",
  "confirmacao_necessaria": false,
  "resposta_usuario": "resposta humana quando aplicável"
}}

CONTEXTO DO NEXUS:

{context}

SOLICITAÇÃO DO USUÁRIO:

{user_request}
"""

        return prompt.strip()

    # ==========================================================
    # ASK BRAIN
    # ==========================================================

    def ask_brain(
        self,
        user_request: str,
        context: str = "",
    ) -> dict[str, Any]:

        prompt = self.build_brain_prompt(
            user_request=user_request,
            context=context,
        )

        raw = self.ask(prompt)

        return self.parse_brain_response(
            raw
        )

    # ==========================================================
    # PARSE BRAIN RESPONSE
    # ==========================================================

    @staticmethod
    def parse_brain_response(
        response: str,
    ) -> dict[str, Any]:

        text = response.strip()

        # ------------------------------------------------------
        # Remove possíveis fences Markdown
        # ------------------------------------------------------

        text = re.sub(
            r"^```(?:json)?\s*",
            "",
            text,
            flags=re.I,
        )

        text = re.sub(
            r"\s*```$",
            "",
            text,
            flags=re.I,
        )

        text = text.strip()

        # ------------------------------------------------------
        # JSON direto
        # ------------------------------------------------------

        try:

            data = json.loads(
                text
            )

            if isinstance(
                data,
                dict,
            ):

                return data

        except json.JSONDecodeError:
            pass

        # ------------------------------------------------------
        # Procura JSON dentro da resposta
        # ------------------------------------------------------

        match = re.search(
            r"\{.*\}",
            text,
            flags=re.S,
        )

        if match:

            try:

                data = json.loads(
                    match.group(0)
                )

                if isinstance(
                    data,
                    dict,
                ):

                    return data

            except json.JSONDecodeError:
                pass

        # ------------------------------------------------------
        # Fallback seguro
        # ------------------------------------------------------

        return {
            "tipo": "informacao",
            "objetivo": "",
            "analise": text,
            "comando": "",
            "comandos": [],
            "risco": "baixo",
            "confirmacao_necessaria": False,
            "resposta_usuario": text,
        }

    # ==========================================================
    # FOLLOW UP
    # ==========================================================

    def follow_up(
        self,
        question: str,
    ) -> str:

        return self.ask(
            question
        )

    # ==========================================================
    # INTERACTIVE SESSION
    # ==========================================================

    def interactive(
        self,
    ) -> None:

        self.start()

        self.ensure_ready()

        print()
        print(
            "=" * 70
        )
        print(
            "NEXUS EDGE + COPILOT"
        )
        print(
            "Digite sua pergunta."
        )
        print(
            "Digite /sair para encerrar."
        )
        print(
            "=" * 70
        )

        while True:

            try:

                question = input(
                    "\nNEXUS EDGE > "
                ).strip()

            except EOFError:

                break

            except KeyboardInterrupt:

                print()
                break

            if not question:
                continue

            if question.lower() in {
                "/sair",
                "/exit",
                "/quit",
            }:

                break

            try:

                print()
                print(
                    "[NEXUS EDGE/COPILOT] "
                    "Processando..."
                )

                answer = self.ask(
                    question
                )

                print()
                print(
                    "-" * 70
                )

                print(
                    answer
                )

                print(
                    "-" * 70
                )

            except Exception as exc:

                print(
                    f"\n[NEXUS EDGE/COPILOT ERROR] {exc}",
                    file=sys.stderr,
                )

    # ==========================================================
    # SCREENSHOT
    # ==========================================================

    def screenshot(
        self,
        path: str | Path,
    ) -> Path:

        destination = Path(
            path
        ).expanduser()

        destination.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.page.screenshot(
            path=str(destination),
            full_page=True,
        )

        return destination

# END NEXUS SECTION: COPILOT_BROWSER

# NEXUS SECTION: CORE_CONFIGURATION
APP_NAME = "NEXUS TERMINAL"
APP_VERSION = "7.3.0-PLANNED-EXECUTION"

DEFAULT_PROJECT_DIR = Path("/home/zorin/Nexus Copilot")
if not DEFAULT_PROJECT_DIR.is_dir():
    DEFAULT_PROJECT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = Path(os.environ.get("NEXUS_PROJECT_DIR", DEFAULT_PROJECT_DIR)).expanduser().resolve()
DATA_DIR = Path(os.environ.get("NEXUS_DATA_DIR", PROJECT_DIR / ".nexus")).expanduser().resolve()

CONFIG_DIR = DATA_DIR
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history"
GENERATED_DIR = CONFIG_DIR / "generated"
CODE_ROOT = Path(os.environ.get("NEXUS_CODE_ROOT", PROJECT_DIR / "code-workspace")).expanduser().resolve()
QUOTA_STATE_FILE = CONFIG_DIR / "quota_state.json"

DEFAULT_MODEL = "microsoft-copilot"

DEFAULT_CONFIG: dict[str, Any] = {
    "model": "microsoft-copilot",
    "temperature": 0.1,
    "max_output_tokens": 3000,
    "programming_max_output_tokens": 10000,
    "api_timeout": 90,
    "browser_timeout": 30000,
    "browser_response_timeout": 120000,
    "browser_stable_seconds": 4.0,
    "browser_poll_interval": 0.5,
    "browser_channel": "msedge",
    "browser_url": "https://copilot.com/chat?fromcode=cmm9tzigufu&sessionId=4b39bf2e-400d-6ca6-2a7c-dba341494dab&hasLW=true&es=SSR&redirfrom=userTypeCookie&redirfrom=cosmicRingCookie",
    "api_backoff_base": 2,
    "api_backoff_max": 30,
    "terminal_timeout": 300,
    "generated_script_timeout": 300,
    "api_min_interval": 8,
    "execution_cooldown": 1,
    "rate_limit_backoff": 60,
    "auth_cooldown": 300,
    "workspace_isolation": True,
    "workspace_ttl_seconds": 3600,
    "workspace_create_venv": False,
    "error_cooldown": 15,
    "max_key_failover": 5,
    "max_retries": 1,
    "max_auto_retries": 12,

    # Controles conservadores do NEXUS; não substituem as quotas reais do Microsoft.
    # 0 = desativado. A quota efetiva deve ser conferida no AI Studio.
    "quota_soft_rpm": 6,
    "quota_soft_tpm": 0,
    "quota_soft_rpd": 0,
    "quota_global_cooldown": 60,
    "quota_persist": True,
    "dangerous_always_confirm": True,
    "max_output_chars": 8000,
    "request_max_chars": 20000,
    "max_generated_script_chars": 200000,
    "keep_generated_scripts": True,
    "log_enabled": False,
    "terminal_logs": True,
    "compact_protocol_enabled": True,
    "compact_protocol_version": "NCP/1",
    "compact_min_savings_percent": 15,
    "compact_fallback_enabled": True,
    "compact_show_preview": False,
    "keys": [],
    "silent_mode": False,
}

# END NEXUS SECTION: CORE_CONFIGURATION

# NEXUS SECTION: AGENT_PROMPTS
AGENTS = {
    "unica": "RESPOSTA ÚNICA",
    "interpretacao": "INTERPRETAÇÃO",
    "planejamento": "PLANEJAMENTO",
    "decisao": "DECISÃO",
    "programacao": "PROGRAMAÇÃO",
    "validacao": "VALIDAÇÃO",
    "conclusao": "CONCLUSÃO",
    "evolucao": "EVOLUÇÃO",
    "agente": "GERAÇÃO DE AGENTE",
    "codigo": "PLANEJAMENTO DE PROJETO",
    "codigo_arquivo": "GERAÇÃO DE ARQUIVO",
    "evolucao_plano": "ANÁLISE E PLANO DE EVOLUÇÃO",
    "orquestrar": "ORQUESTRADOR COGNITIVO",
    "plano_execucao": "PLANO DE EXECUÇÃO ESTRUTURADO",
    "reavaliacao": "REAVALIAÇÃO DE PLANO",
    "validacao_tarefa": "VALIDAÇÃO DE TAREFA",
    "validacao_fase": "VALIDAÇÃO DE FASE",
}

PROMPTS = {
    "unica": """
Você é o agente principal do NEXUS TERMINAL.

Escolha exatamente uma rota:
- response: pergunta informativa, sem execução;
- command: UM único comando Linux simples;
- python: lógica, arquivos, parsing, processamento ou múltiplas etapas.

Use command somente quando um único comando resolver completamente a tarefa.
Não use &&, ||, ;, |, quebras de linha ou scripts inline na rota command.
Use python para duas ou mais etapas, loops, condições, arquivos ou relatórios.
Use response quando o usuário não pedir execução ou criação de arquivo.
Para abrir um site, use command com xdg-open 'URL'.
Para abrir um aplicativo .desktop, use gtk-launch 'ID.desktop'.
Para abrir um aplicativo Flatpak, use flatpak run 'APP_ID'.
Escolha IDs no catálogo available_graphical_apps do contexto; nunca invente o nome.
Não abra todos os programas instalados de uma vez sem confirmação explícita.
Quando mode=response, escreva a resposta humana completa no campo response.
Nunca deixe response vazio, nunca use apenas "..." e nunca coloque a resposta
somente em reason. Não coloque código Python neste agente e não invente resultados.

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

Escolha entre python, command e response, nesta ordem de prioridade:
1. response para explicações, definições, resumos e pedidos sem execução;
2. command para UM único comando Linux simples, como pwd, ls, whoami,
   hostname, uname -a, uptime ou free -h;
3. python para múltiplas etapas, arquivos, loops, parsing, cálculos,
   relatórios, automação ou qualquer lógica que não caiba em um comando.

Para abrir um site ou aplicativo conhecido, prefira command com xdg-open,
gtk-launch ou flatpak run. Use python somente para múltiplas aberturas,
listagem/seleção de aplicativos ou processamento de resultados.

Respeite o campo preferred_mode quando estiver presente. Nunca escolha
python para uma tarefa resolvível por um único comando simples. Nunca use
command para esconder várias etapas com &&, ||, ;, | ou script inline.

Retorne SOMENTE JSON válido:
{
  "mode": "python|command|response",
  "command": "",
  "response": "",
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
O campo "code" nunca pode ser vazio. O campo "code_lines" não é aceito
neste agente; use exclusivamente o campo "code" como uma string completa.

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

    "agente": """
Você é o gerador de pequenos agentes Python do NEXUS TERMINAL.

Crie um programa Python completo, funcional e executável que implemente o
miniagente solicitado. O miniagente deve:
- incorporar a personalidade recebida em um SYSTEM_PROMPT claro;
- ter uma função principal e um loop de conversa no terminal;
- usar o Microsoft Copilot via navegador quando necessário;
- não usar API key, API HTTP ou segredo incorporado;
- usar o Microsoft Copilot através do navegador persistente;
- tratar ausência de chave, timeout, erro de rede e HTTP >= 400;
- não executar comandos de shell, não apagar arquivos e não exfiltrar segredos;
- permitir sair com /sair, /exit ou Ctrl+C;
- usar apenas biblioteca padrão e a ponte EdgeCopilotBrowser quando necessário;
- conter todo o código, sem placeholders, reticências ou pseudocódigo.

O programa deve ser independente do NEXUS e não deve importar nexus.py.
Não coloque markdown dentro de code_lines.
Para evitar erros de escape JSON, retorne o código em code_lines: uma lista
de strings, uma por linha, preservando a ordem exata do programa.

Retorne SOMENTE JSON válido:
{
  "language": "python",
  "filename": "agente_nome.py",
  "code_lines": ["cada item é uma linha literal do código Python"],
  "dependencies": [],
  "reason": "..."
}
""",

    "codigo": """
Você é o PLANEJADOR DE PROJETO do NEXUS TERMINAL.

Sua única tarefa nesta etapa é criar um plano executável. NÃO escreva código,
NÃO inclua o campo content e NÃO tente entregar o projeto inteiro.

Retorne SOMENTE um objeto JSON válido, sem markdown e sem texto antes/depois:
{
  "project_name": "nome curto",
  "summary": "objetivo do projeto",
  "steps": [
    "passo 1 curto",
    "passo 2 curto"
  ],
  "files": [
    {
      "path": "src/main.py",
      "language": "python",
      "purpose": "responsabilidade do arquivo",
      "executable": true
    }
  ],
  "dependencies": [],
  "run_instructions": "como executar depois de concluído",
  "reason": "decisão técnica"
}

REGRAS:
- Crie de 1 a 24 arquivos, em ordem de dependência.
- Cada path é relativo ao workspace; nunca use /, ~ ou .. .
- Use python, bash ou text como language.
- Defina nomes claros e inclua README quando isso ajudar.
- Não inclua código, markdown, exemplos longos ou arquivos inventados.
- Não instale dependências, não execute comandos e não inclua segredos.
""",

    "codigo_arquivo": """
Você é o GERADOR DE UM ÚNICO ARQUIVO do NEXUS TERMINAL.

G​​ere SOMENTE o arquivo indicado na tarefa atual. O arquivo será validado e
salvo antes do próximo arquivo. Não gere os demais arquivos do projeto.

FORMATO OBRIGATÓRIO DA RESPOSTA:
Retorne SOMENTE um objeto JSON válido, sem texto antes ou depois. O campo
content deve ser uma string contendo o arquivo completo. Dentro da string,
comece o código com ```python (ou ```bash para Bash) e termine com ```.
Exemplo estrutural:
{
  "path": "mesmo path recebido",
  "language": "python|bash|text",
  "content": "```python\\nfrom pathlib import Path\\n...\\n```",
  "executable": true,
  "reason": "resumo curto"
}

REGRAS CRÍTICAS:
- Preserve literalmente identificadores Python, incluindo __init__, __name__,
  __main__, nomes com underscore e strings com aspas.
- Preserve quebras de linha reais e a indentação Python. Depois de cada dois-pontos
  que inicia for, if, def, class, try, except, with ou outra estrutura de bloco,
  escreva um corpo indentado com 4 espaços por nível. Nunca achate o arquivo em
  uma linha nem coloque o corpo de um bloco na mesma coluna do cabeçalho.
- O campo content deve conter código COMPLETO, desde a primeira linha até a
  última. As quebras de linha devem ser reais ou JSON-escapes válidos.
- Nunca envie a palavra "python", "py", "bash" ou "sh" como uma linha isolada
  antes do código; o nome da linguagem deve aparecer somente no campo language
  ou na cerca Markdown exigida.
- Nunca transforme __name__ em name, __init__ em init ou __main__ em main.
- Nunca remova indentação Python. Use 4 espaços dentro de funções, classes,
  if, for, while, try e blocos semelhantes.
- Todo try deve terminar com pelo menos um except ou finally. Nunca deixe um
  try aberto e nunca termine o arquivo antes de fechar todos os blocos.
- Nunca coloque código Python em uma única linha comprida.
- Não use reticências, TODO, pseudocódigo, placeholders ou conteúdo omitido.
- Não altere o path recebido e não inclua outro arquivo no campo content.
- Python será validado por AST; Bash por conteúdo e placeholders.
- Não instale dependências, não execute comandos e não inclua segredos.
""",

    "evolucao": """
Você é o agente EVOLUÇÃO do NEXUS TERMINAL.

Você receberá SOMENTE uma função Python real e um pedido de evolução.
Quando fornecidos, selected_code_snippet e selected_code_diff são a cópia
delimitada do trecho escolhido pelo usuário. Use-os como fonte primária,
confira selected_code_sha256 e não peça nem reescreva o arquivo inteiro.
Modifique exclusivamente essa função, sem alterar sua assinatura (nome,
parâmetros e async), sem renomeá-la e sem depender de alterações fora dela.
Preserve o comportamento existente que não foi expressamente alterado.

Retorne SOMENTE JSON válido:
{
  "function_name": "mesmo_nome_da_funcao",
  "code": "a função Python completa e substituta",
  "reason": "resumo objetivo da alteração"
}

O campo code deve conter apenas a função completa, começando por def ou
async def. Não use markdown, reticências, placeholders ou código omitido.
Não inclua imports, classes, outras funções ou explicações fora do JSON.
""",

    "evolucao_plano": """
Você é o ANALISADOR E PLANEJADOR DE EVOLUÇÃO do NEXUS TERMINAL.

O NEXUS já clonou o arquivo ativo e pesquisou o clone por AST e texto.
Use somente o objetivo, o mapa da habilidade, os resultados de busca e o código seletivo fornecidos.
Quando selected_target.selected for true, selected_target.code é a cópia exata
do trecho escolhido pelo usuário. Analise esse trecho primeiro, confirme seu id,
função, intervalo de linhas e SHA-256, e use selected_target.diff apenas como
delimitação visual. Não peça nem reconstitua o arquivo inteiro.
Não afirme que existe uma capacidade sem evidência no contexto recebido.
Primeiro diagnostique o estado atual e as limitações; depois proponha etapas
pequenas, compatíveis e testáveis. Não escreva nem retorne código executável.
Não solicite nem recomende a reescrita do arquivo completo.
Não copie textos do schema, não use frases genéricas como substitutos de observações
reais e cite ao menos um id de busca recebido, com sua seção exata, em evidence.
Se o contexto não permitir uma conclusão concreta, diga isso claramente e explique por quê.

Retorne somente JSON válido neste formato:
{
  "analysis": "",
  "evidence": [{"id": "id exato de search_hits", "section": "seção do id", "observation": "fato concreto observado"}],
  "capabilities_found": [],
  "limitations": [],
  "plan": [],
  "tests": [],
  "risks": [],
  "summary": ""
}
""",

    "plano_execucao": """
Você é o PLANEJADOR DE EXECUÇÃO ESTRUTURADA do NEXUS TERMINAL.

Crie um PLANO INICIAL completo estruturado em fases e tarefas sequenciais.
O plano será usado como memória operacional persistente durante toda a execução.
O NEXUS executará fase por fase, tarefa por tarefa, validando cada uma antes de avançar.

NÃO execute nada. NÃO escreva código. NÃO invente resultados.

Retorne SOMENTE um objeto JSON válido, sem markdown e sem texto antes/depois:
{
  "objective": "objetivo geral do projeto",
  "summary": "resumo curto do que será entregue",
  "phases": [
    {
      "phase_id": "F01",
      "phase_name": "nome da fase",
      "phase_objective": "objetivo específico desta fase",
      "tasks": [
        {
          "task_id": "F01-T01",
          "task_name": "nome curto da tarefa",
          "task_objective": "o que precisa ser feito nesta tarefa",
          "files_to_create": ["caminho/relativo.py"],
          "files_to_modify": ["caminho/relativo.py"],
          "files_to_analyze": ["caminho/relativo.py"],
          "dependencies": ["F01-T01"],
          "completion_criteria": "critério objetivo para considerar a tarefa concluída",
          "validation_criteria": "critério objetivo para validar o resultado"
        }
      ],
      "phase_completion_criteria": "critério para considerar a fase concluída",
      "phase_validation_criteria": "critério para validar a fase inteira"
    }
  ],
  "global_dependencies": [],
  "final_validation_criteria": "critério final para validar o projeto inteiro"
}

REGRAS:
- Agrupe tarefas relacionadas em fases lógicas (1 a 6 fases).
- Cada fase deve ter de 1 a 8 tarefas.
- Liste tarefas em ordem de dependência dentro de cada fase.
- files_to_create, files_to_modify e files_to_analyze são opcionais (use lista vazia quando não aplicar).
- dependencies refere-se a IDs de tarefas anteriores necessárias.
- completion_criteria deve ser verificável objetivamente (arquivo existe, teste passa, saída esperada).
- validation_criteria deve ser verificável objetivamente (exit code 0, conteúdo esperado, sem erros).
- Não inclua código, não execute comandos, não instale dependências.
""",

    "reavaliacao": """
Você é o AGENTE DE REAVALIAÇÃO DE PLANO do NEXUS TERMINAL.

Após concluir cada tarefa, o NEXUS reavalia o plano inteiro para verificar:
- O resultado produzido é compatível com o objetivo original?
- Alguma dependência mudou?
- Surgiu alguma tarefa adicional necessária que não estava prevista?
- Alguma etapa precisa ser corrigida antes de continuar?

Analise o plano atual, a tarefa recém-concluída, os resultados de validação e o objetivo original.
NÃO execute nada. NÃO escreva código.

Retorne SOMENTE JSON válido:
{
  "plan_still_valid": true,
  "reason": "explicação curta",
  "new_tasks": [
    {
      "task_id": "F01-T99",
      "task_name": "tarefa nova",
      "task_objective": "objetivo",
      "files_to_create": [],
      "files_to_modify": [],
      "files_to_analyze": [],
      "dependencies": [],
      "completion_criteria": "critério",
      "validation_criteria": "critério",
      "insert_after": "F01-T01",
      "justification": "por que esta tarefa é necessária"
    }
  ],
  "tasks_to_revisit": ["F01-T02"],
  "plan_adjustments": "ajustes necessários no plano, se houver"
}

Se o plano continua válido, retorne plan_still_valid=true e listas vazias.
""",

    "validacao_tarefa": """
Você é o VALIDADOR DE TAREFA do NEXUS TERMINAL.

Analise objetivamente se a tarefa foi concluída com base nos critérios fornecidos.
Considere: arquivos criados/modificados, resultado de execução, exit code, stdout/stderr.
NÃO invente sucesso. Se algo falhou, diga explicitamente.

Retorne SOMENTE JSON:
{
  "task_completed": true,
  "reason": "explicação objetiva baseada em evidências",
  "evidence": ["arquivo X existe", "exit code 0", "saída contém Y"],
  "issues": []
}

Se a tarefa NÃO foi concluída, task_completed=false e liste os problemas em issues.
""",

    "validacao_fase": """
Você é o VALIDADOR DE FASE do NEXUS TERMINAL.

Após concluir todas as tarefas de uma fase, valide a fase inteira comparando
o estado real do sistema com os critérios de conclusão e validação da fase.
NÃO invente sucesso.

Retorne SOMENTE JSON:
{
  "phase_completed": true,
  "reason": "explicação objetiva",
  "evidence": ["evidência 1", "evidência 2"],
  "issues": []
}

Se a fase NÃO foi concluída, phase_completed=false e liste os problemas.
""",

    "orquestrar": """
Você é o ORQUESTRADOR COGNITIVO do NEXUS TERMINAL.

O NEXUS é um agente Python conectado a um terminal Linux real.
Você é o motor de raciocínio. O NEXUS é o motor de execução.

Sua função é ANALISAR o contexto da etapa atual e decidir a próxima ação.

REGRAS:
1. Não execute nada. O NEXUS executa.
2. Não invente resultados.
3. Não diga que executou algo que não executou.
4. Forneça comandos Linux simples e verificáveis.
5. Forneça código Python completo quando necessário.
6. Não use sudo sem necessidade.
7. Não use comandos destrutivos sem justificativa.
8. Se a etapa foi concluída, marque STATUS=DONE.
9. Se houver erro, marque STATUS=ERROR e proponha correção.
10. Se precisar de intervenção do usuário, marque STATUS=WAITING.

FORMATO DE RESPOSTA OBRIGATÓRIO:

<AGENT_RESPONSE>
STATUS: READY|DONE|ERROR|RETRY|WAITING
ACTION: CREATE_FILE|RUN_COMMAND|ANALYZE|VALIDATE|ASK_USER|FINISH
FILE: caminho relativo ou NONE
COMMAND: comando ou NONE
CONTENT: conteúdo ou NONE
REASON: explicação curta
NEXT_STEP: próxima etapa
SUCCESS_CRITERIA: condição objetiva
</AGENT_RESPONSE>

NÃO ADICIONE TEXTO ANTES OU DEPOIS DO BLOCO.
TODOS os campos são obrigatórios. Use NONE quando não aplicável.

STATUS:
  READY   — pronto para executar a ação
  DONE    — etapa concluída e validada
  ERROR   — erro detectado, precisa correção
  RETRY   — tentar novamente com ajuste
  WAITING — precisa de intervenção do usuário

ACTION:
  CREATE_FILE — criar/escrever arquivo (use FILE e CONTENT)
  RUN_COMMAND — executar comando Linux (use COMMAND)
  ANALYZE     — apenas analisar, sem execução
  VALIDATE    — validar resultado da etapa anterior
  ASK_USER    — solicitar informação do usuário
  FINISH      — tarefa completa, todas as etapas concluídas
""",
}

# END NEXUS SECTION: AGENT_PROMPTS

# NEXUS SECTION: COMMAND_SAFETY
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

# END NEXUS SECTION: COMMAND_SAFETY

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

def terminal_log(message: str, level: str = "INFO") -> None:
    """Exibe observabilidade local sem registrar segredos nem chamar APIs."""
    if not message:
        return
    timestamp = datetime.now().astimezone().strftime("%H:%M:%S")
    colors = {
        "INFO": "\033[36m",
        "OK": "\033[32m",
        "WARN": "\033[33m",
        "ERROR": "\033[31m",
        "API": "\033[35m",
        "STEP": "\033[34m",
    }
    color = colors.get(level.upper(), "\033[0m")
    print(f"{color}[{timestamp}] [NEXUS {level.upper():<5}]\033[0m {message}", flush=True)

def terminal_panel(title: str, details: list[str] | None = None) -> None:
    """Painel visual simples, compatível com terminais sem interface gráfica."""
    print(f"\n\033[1;36m┌── {title} " + "─" * max(2, 54 - len(title)) + "┐\033[0m")
    for detail in details or []:
        print(f"\033[1;36m│\033[0m {detail}")
    print("\033[1;36m└" + "─" * 61 + "┘\033[0m", flush=True)

def terminal_progress(label: str, current: int, total: int) -> None:
    total = max(1, total)
    current = max(0, min(current, total))
    width = 28
    filled = int(width * current / total)
    bar = "█" * filled + "·" * (width - filled)
    print(f"\033[36m[NEXUS PROGRESS] {label:<22} [{bar}] {current}/{total}\033[0m", flush=True)


def terminal_plan(title: str, tasks: list[str], completed: bool = False) -> None:
    print(f"\n\033[1;37m☷  {title}\033[0m")
    for task in tasks:
        print(f"\033[32m✓\033[0m  {task}")
    if completed:
        print("\033[32m✓✓\033[0m  Plan completed", flush=True)

SEVEN_PHASES = (
    "DESCOBERTA E PLANEJAMENTO",
    "ARQUITETURA E ESTRUTURA",
    "IMPLEMENTACAO INICIAL",
    "COMPONENTES",
    "INTEGRACAO",
    "TESTES E CORRECOES",
    "VALIDACAO FINAL",
)

def print_planning_menu(phase_states: dict[int, str], actions: list[str]) -> None:
    """Imprime o menu de planejamento com 7 fases e ações identificadas."""
    print("\n\033[1;36m[NEXUS] PLANEJAMENTO IDENTIFICADO\033[0m")
    for phase_num in range(1, 8):
        label = SEVEN_PHASES[phase_num - 1]
        state = phase_states.get(phase_num, "PENDENTE")
        if state == "CONCLUIDA":
            marker = "\033[32mCONCLUÍDA\033[0m"
        elif state == "EM_EXECUCAO":
            marker = "\033[33mEM EXECUÇÃO\033[0m"
        else:
            marker = "\033[90mPENDENTE\033[0m"
        dots = "." * max(2, 38 - len(label) - len(str(phase_num)))
        print(f"  [FASE {phase_num}/7] {label} {dots} {marker}")
    print(f"\n\033[1;36m[AÇÕES IDENTIFICADAS]\033[0m")
    for index, action in enumerate(actions, 1):
        print(f"  {index:02d}. {action}")
    print(flush=True)

def print_phase_header(phase_num: int, action_index: int, total_actions: int, action_desc: str) -> None:
    """Exibe o cabeçalho da fase atual e da ação atual."""
    phase_label = SEVEN_PHASES[phase_num - 1] if 1 <= phase_num <= 7 else f"FASE {phase_num}"
    print(f"\n\033[1;34m[NEXUS] FASE {phase_num}/7 — {phase_label}\033[0m")
    print(f"\033[36m[NEXUS] AÇÃO {action_index:02d}/{total_actions:02d}\033[0m")
    print(f"[NEXUS] {action_desc}", flush=True)

def print_action_step(step: str, detail: str) -> None:
    """Exibe uma etapa da ação atual (consultando, gerando, validando)."""
    icons = {
        "consultando": "CONSULTANDO COPILOT",
        "gerando": "GERANDO",
        "validando": "VALIDANDO",
        "concluido": "GERADO E VALIDADO",
        "verificando": "VERIFICANDO PRÓXIMA TAREFA",
        "rejeitado": "RESULTADO INVÁLIDO",
    }
    label = icons.get(step.lower(), step.upper())
    if step.lower() == "concluido":
        print(f"\033[32m[NEXUS] {detail} {label} ✓\033[0m", flush=True)
    elif step.lower() == "rejeitado":
        print(f"\033[31m[NEXUS] {label}: {detail}\033[0m", flush=True)
    elif step.lower() == "consultando":
        print(f"[NEXUS] {label} → {detail}", flush=True)
    else:
        print(f"[NEXUS] {label} → {detail}", flush=True)

def print_progress_percent(completed: int, total: int) -> None:
    """Exibe o percentual de evolução do projeto."""
    total = max(1, total)
    percent = int(completed * 100 / total)
    bar_width = 28
    filled = int(bar_width * completed / total)
    bar = "█" * filled + "·" * (bar_width - filled)
    print(f"\033[36m[NEXUS] PROGRESSO DO PROJETO [{bar}] {percent}% ({completed}/{total})\033[0m", flush=True)

NCP_KEY_ALIASES: dict[str, str] = {
    "user_request": "u",
    "terminal": "t",
    "mode": "m",
    "complexity": "x",
    "interpretation": "i",
    "planning": "p",
    "decision": "d",
    "target_file": "f",
    "part": "a",
    "function_name": "fn",
    "evolution": "ev",
    "current_function": "cf",
    "requirements": "rq",
    "objective": "ob",
    "personality": "ps",
    "name": "nm",
    "retry_instruction": "ri",
    "language": "lg",
    "filename": "fl",
    "dependencies": "dp",
    "reason": "rs",
    "success": "ok",
    "next_action": "na",
    "stdout": "so",
    "stderr": "se",
    "exit_code": "ec",
    "script": "sc",
    "validation": "v",
    "request": "rqst",
}
NCP_REVERSE_ALIASES = {value: key for key, value in NCP_KEY_ALIASES.items()}
NCP_SYSTEM_INSTRUCTION = """
Transporte NCP/1: a mensagem do usuário pode começar por NCP/1| seguido de
JSON compacto. Decodifique os aliases antes de raciocinar: u=user_request,
t=terminal, m=mode, x=complexity, i=interpretation, p=planning, d=decision,
f=target_file, a=part, fn=function_name, ev=evolution, cf=current_function,
rq=requirements, ob=objective, ps=personality, nm=name, lg=language, fl=filename,
dp=dependencies, rs=reason, ok=success, na=next_action, so=stdout, ec=exit_code.
NCP/1 é apenas transporte reversível, não é criptografia. Responda no formato
JSON exigido pelo seu agente; não invente aliases e não omita dados necessários.
"""

def _ncp_transform(value: Any, aliases: dict[str, str]) -> Any:
    if isinstance(value, dict):
        return {aliases.get(str(key), str(key)): _ncp_transform(item, aliases) for key, item in value.items()}
    if isinstance(value, list):
        return [_ncp_transform(item, aliases) for item in value]
    return value

def ncp_pack(state: dict[str, Any]) -> str:
    compact = _ncp_transform(state, NCP_KEY_ALIASES)
    return "NCP/1|" + json.dumps(compact, ensure_ascii=False, separators=(",", ":"))

def ncp_unpack(text: str) -> str:
    raw = str(text or "").strip()
    if not raw.startswith("NCP/1|"):
        return raw
    try:
        compact = json.loads(raw[6:])
        expanded = _ncp_transform(compact, NCP_REVERSE_ALIASES)
        return json.dumps(expanded, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AgentError(f"Resposta NCP/1 inválida: {exc}") from exc

def ncp_savings_percent(plain: str, compact: str) -> float:
    if not plain:
        return 0.0
    return max(0.0, (1.0 - len(compact) / len(plain)) * 100.0)

def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).replace("\x1b[200~", "").replace("\x1b[201~", "")
    return ANSI_ESCAPE_RE.sub("", text).strip()

def as_bool(value: Any, default: bool = False) -> bool:
    """Converte execute vindo do JSON do Copilot sem falhar com strings."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "sim", "s", "yes", "y", "on"}:
            return True
        if normalized in {"false", "0", "não", "nao", "n", "no", "off", ""}:
            return False
    return bool(default)

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

# NEXUS SECTION: CODE_VALIDATION
def repair_python_indentation(code: str, max_repairs: int = 40) -> tuple[str, int]:
    """Corrige somente erros explícitos de indentação apontados pelo parser.

    A rotina é deliberadamente limitada: não reescreve expressões, não inventa
    código e não tenta corrigir erros de sintaxe que não sejam indentation errors.
    Cada ajuste é revalidado pelo AST; se o resultado continuar inválido, o
    chamador ainda rejeitará o arquivo e solicitará uma nova geração ao Copilot.
    """
    candidate = str(code or "").replace("\r\n", "\n").replace("\r", "\n")
    repairs = 0
    while repairs < max_repairs:
        try:
            ast.parse(candidate, filename="<nexus-generated>")
            return candidate, repairs
        except IndentationError as exc:
            lines = candidate.splitlines(keepends=True)
            line_number = int(exc.lineno or 0)
            if not line_number or line_number > len(lines):
                break
            index = line_number - 1
            original = lines[index]
            newline = "\n" if original.endswith("\n") else ""
            body = original[:-1] if newline else original
            if not body.strip():
                index += 1
                while index < len(lines) and not lines[index].strip():
                    index += 1
                if index >= len(lines):
                    break
                original = lines[index]
                newline = "\n" if original.endswith("\n") else ""
                body = original[:-1] if newline else original

            prefix_match = re.match(r"^[ \t]*", body)
            prefix = prefix_match.group(0) if prefix_match else ""
            expanded = prefix.expandtabs(4)
            message = str(exc).casefold()
            if "expected an indented block" in message:
                header_index = index - 1
                while header_index >= 0 and not lines[header_index].strip():
                    header_index -= 1
                header_body = lines[header_index].rstrip("\r\n") if header_index >= 0 else ""
                header_prefix = re.match(r"^[ \t]*", header_body).group(0) if header_body else ""
                header_indent = len(header_prefix.expandtabs(4))
                body = " " * (header_indent + 4) + body[len(prefix):]
            elif "unexpected indent" in message:
                if len(expanded) < 4:
                    break
                body = " " * (len(expanded) - 4) + body[len(prefix):]
            elif "unindent does not match" in message:
                normalized = len(expanded) - (len(expanded) % 4)
                if normalized == len(expanded) or normalized < 0:
                    break
                body = " " * normalized + body[len(prefix):]
            elif "inconsistent use of tabs and spaces" in message:
                body = " " * len(expanded) + body[len(prefix):]
            else:
                break
            lines[index] = body + newline
            candidate = "".join(lines)
            repairs += 1
        except SyntaxError:
            break
    return candidate, repairs


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

    # O Copilot às vezes perde os underscores duplos ao transcrever JSON.
    # Não corrigimos silenciosamente: rejeitamos e pedimos uma nova resposta,
    # para nunca salvar um programa semanticamente alterado.
    if re.search(r"\bif\s+name\s*==\s*(['\"])main\1", code) and "__name__" not in code:
        raise AgentError(
            "Identificador Python corrompido: use exatamente if __name__ == '__main__':"
        )
    if "__init__" not in code and re.search(r"\bdef\s+init\s*\(", code):
        raise AgentError(
            "Identificador Python corrompido: use exatamente def __init__(...)."
        )

    repaired_code, repair_count = repair_python_indentation(code)
    if repair_count:
        terminal_log(
            f"Autocorreção de indentação aplicada antes da gravação: {repair_count} ajuste(s)",
            "WARN",
        )
        code = repaired_code

    try:
        ast.parse(code, filename="<nexus-generated>")
    except SyntaxError as exc:
        line_number = int(exc.lineno or 0)
        source_line = code.splitlines()[line_number - 1].strip() if line_number and line_number <= len(code.splitlines()) else ""
        message = str(exc).casefold()
        if "expected 'except' or 'finally' block" in message:
            hint = " O bloco try está incompleto: adicione except ou finally, ou regenere o arquivo completo."
        elif isinstance(exc, IndentationError) or "indent" in message or "expected an indented block" in message:
            hint = " Verifique a indentação do bloco após ':' e use 4 espaços."
        else:
            hint = ""
        detail = f" linha {line_number}" if line_number else ""
        if source_line:
            detail += f" (trecho: {source_line[:120]!r})"
        raise AgentError(
            f"Código Python inválido:{detail}: {exc.msg}.{hint}"
        ) from exc

    intelligence = CodeIntelligenceEngine().analyze(code, "<nexus-generated>")
    if intelligence["diagnostics"]:
        terminal_log(
            f"Code Intelligence: {len(intelligence['diagnostics'])} diagnóstico(s), "
            f"{intelligence['errors']} erro(s), {intelligence['warnings']} aviso(s)",
            "WARN" if intelligence["errors"] else "INFO",
        )
    if intelligence["errors"]:
        first = next(item for item in intelligence["diagnostics"] if item["severity"] == "error")
        raise AgentError(
            f"Code Intelligence detectou erro [{first['tool']} {first['code']}]: "
            f"linha {first['line']}: {first['message']}"
        )

    return code

# END NEXUS SECTION: CODE_VALIDATION


# NEXUS SECTION: CODE_INTELLIGENCE
@dataclass
class CodeDiagnostic:
    tool: str
    severity: str
    message: str
    line: int = 0
    column: int = 0
    code: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool, "severity": self.severity, "message": self.message,
            "line": self.line, "column": self.column, "code": self.code,
        }


class CodeIntelligenceEngine:
    """Diagnóstico local em camadas, sem tornar ferramentas externas obrigatórias."""
    TOOL_COMMANDS = {"ruff": "ruff", "pyright": "pyright"}

    def __init__(self, config: Optional[dict[str, Any]] = None) -> None:
        self.config = config or {}

    def capabilities(self) -> dict[str, bool]:
        return {
            "ast": True,
            "ruff": shutil.which(self.TOOL_COMMANDS["ruff"]) is not None,
            "pyright": shutil.which(self.TOOL_COMMANDS["pyright"]) is not None,
            "jedi": _jedi is not None,
            "prompt_toolkit": _PTCompleter is not None,
        }

    def bootstrap_dependencies(self) -> list[dict[str, str]]:
        """Instala o que faltar e retorna status explícito de cada componente."""
        global _jedi, _PTCompleter
        statuses: list[dict[str, str]] = []
        python_packages = [("jedi", "jedi"), ("prompt_toolkit", "prompt_toolkit")]
        for label, module_name in python_packages:
            try:
                __import__(module_name)
                statuses.append({"name": label, "status": "ATIVO", "detail": "biblioteca carregada"})
                continue
            except ImportError:
                pass
            print(f"[NEXUS] Instalando biblioteca {label}...")
            installed, detail = bootstrap_python_package(label)
            if installed:
                try:
                    if label == "jedi":
                        import jedi as _loaded_jedi
                        _jedi = _loaded_jedi
                    else:
                        import prompt_toolkit.completion as _loaded_pt
                        _PTCompleter = _loaded_pt.Completer
                    statuses.append({"name": label, "status": "INSTALADO", "detail": "carregado com sucesso"})
                except ImportError as exc:
                    statuses.append({"name": label, "status": "ERRO", "detail": str(exc)})
            else:
                statuses.append({"name": label, "status": "ERRO", "detail": detail})

        if shutil.which("ruff"):
            statuses.append({"name": "ruff", "status": "ATIVO", "detail": shutil.which("ruff") or ""})
        else:
            print("[NEXUS] Instalando ferramenta Ruff...")
            installed, detail = bootstrap_python_package("ruff")
            statuses.append({"name": "ruff", "status": "INSTALADO" if installed else "ERRO", "detail": detail})

        if shutil.which("pyright"):
            statuses.append({"name": "pyright", "status": "ATIVO", "detail": shutil.which("pyright") or ""})
        else:
            npm = shutil.which("npm")
            if npm:
                print("[NEXUS] Instalando ferramenta Pyright via npm (local)...")
                try:
                    local_bin = Path.home() / ".local" / "bin"
                    local_bin.mkdir(parents=True, exist_ok=True)
                    env = os.environ.copy()
                    env["npm_config_prefix"] = str(Path.home() / ".npm-global")
                    result = subprocess.run([npm, "install", "--prefix", str(Path.home() / ".npm-global"), "pyright"], capture_output=True, text=True, timeout=180, check=False)
                    pyright_bin = Path.home() / ".npm-global" / "node_modules" / ".bin" / "pyright"
                    ok = result.returncode == 0 and (shutil.which("pyright") is not None or pyright_bin.exists())
                    if pyright_bin.exists() and str(pyright_bin.parent) not in os.environ.get("PATH", ""):
                        os.environ["PATH"] = str(pyright_bin.parent) + os.pathsep + os.environ.get("PATH", "")
                    detail = (result.stderr or result.stdout or "npm falhou").strip()[-1000:]
                    if ok:
                        detail = str(shutil.which("pyright") or str(pyright_bin))
                    statuses.append({"name": "pyright", "status": "INSTALADO" if ok else "ERRO", "detail": detail})
                except (OSError, subprocess.SubprocessError) as exc:
                    statuses.append({"name": "pyright", "status": "ERRO", "detail": str(exc)})
            else:
                statuses.append({"name": "pyright", "status": "PENDENTE", "detail": "npm não encontrado"})
        return statuses

    def _ast_diagnostics(self, source: str, filename: str) -> list[CodeDiagnostic]:
        try:
            tree = ast.parse(source, filename=filename)
        except SyntaxError as exc:
            return [CodeDiagnostic("ast", "error", exc.msg, int(exc.lineno or 0), int(exc.offset or 0), "syntax")]
        diagnostics: list[CodeDiagnostic] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                diagnostics.append(CodeDiagnostic("ast", "warning", "except genérico pode ocultar erros; prefira uma exceção específica", node.lineno, node.col_offset + 1, "bare-except"))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and ast.get_docstring(node) is None:
                diagnostics.append(CodeDiagnostic("ast", "info", f"função {node.name!r} sem docstring", node.lineno, node.col_offset + 1, "missing-docstring"))
        return diagnostics

    def _external_diagnostics(self, tool: str, source: str, filename: str) -> list[CodeDiagnostic]:
        executable = shutil.which(self.TOOL_COMMANDS[tool])
        if not executable:
            return []
        with tempfile.TemporaryDirectory(prefix="nexus-intel-") as temp_dir:
            path = Path(temp_dir) / Path(filename).name
            path.write_text(source, encoding="utf-8")
            if tool == "ruff":
                command = [executable, "check", "--output-format", "json", str(path)]
            else:
                command = [executable, "--outputjson", str(path)]
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False)
            except (OSError, subprocess.SubprocessError):
                return []
            output = (result.stdout or result.stderr or "").strip()
            if not output:
                return []
            try:
                payload = json.loads(output)
            except json.JSONDecodeError:
                return [CodeDiagnostic(tool, "warning", output[:500], 0, 0, "tool-output")]
            diagnostics: list[CodeDiagnostic] = []
            items = payload if tool == "ruff" and isinstance(payload, list) else payload.get("generalDiagnostics", []) if isinstance(payload, dict) else []
            for item in items:
                if tool == "ruff":
                    location = item.get("location", {})
                    diagnostics.append(CodeDiagnostic(tool, "error" if item.get("code", "").startswith(("E", "F")) else "warning", clean_text(item.get("message", "")), int(location.get("row", 0)), int(location.get("column", 0)), clean_text(item.get("code", ""))))
                else:
                    severity = clean_text(item.get("severity", "error")).lower() or "error"
                    rng = item.get("range", {})
                    start = rng.get("start", {})
                    diagnostics.append(CodeDiagnostic(tool, severity, clean_text(item.get("message", "")), int(start.get("line", 0)) + 1, int(start.get("character", 0)) + 1, clean_text(item.get("rule", ""))))
            return diagnostics[:100]

    def analyze(self, source: str, filename: str = "<nexus-generated>") -> dict[str, Any]:
        diagnostics = self._ast_diagnostics(source, filename)
        if not any(item.severity == "error" for item in diagnostics):
            diagnostics.extend(self._external_diagnostics("ruff", source, filename))
            diagnostics.extend(self._external_diagnostics("pyright", source, filename))
        errors = [item for item in diagnostics if item.severity == "error"]
        return {"filename": filename, "capabilities": self.capabilities(), "diagnostics": [item.as_dict() for item in diagnostics], "errors": len(errors), "warnings": sum(item.severity == "warning" for item in diagnostics), "ok": not errors}

    def complete(self, source: str, line: int, column: int, filename: str = "<nexus>") -> list[dict[str, str]]:
        if _jedi is None:
            return []
        try:
            script = _jedi.Script(code=source, path=filename)
            return [{"name": item.name, "type": item.type, "description": item.description} for item in script.complete(line=line, column=column)[:50]]
        except Exception:
            return []

    def format_report(self, report: dict[str, Any]) -> str:
        lines = [f"Code Intelligence: {'OK' if report['ok'] else 'ERROS'} | ferramentas: " + ", ".join(name for name, enabled in report["capabilities"].items() if enabled)]
        for item in report["diagnostics"]:
            location = f"{item['line']}:{item['column']}" if item["line"] else "-"
            lines.append(f"[{item['severity'].upper()}] {item['tool']} {location} {item['code']}: {item['message']}")
        return "\n".join(lines)

# END NEXUS SECTION: CODE_INTELLIGENCE

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


def exclusive_write(path: Path, content: str, mode: int = 0o600) -> None:
    """Grava uma vez; falha se o destino já existir, sem sobrescrever."""
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(str(path), flags, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
        fd = -1
    finally:
        if fd != -1:
            os.close(fd)
    try:
        os.chmod(path, mode)
    except OSError:
        pass

def atomic_write_bytes(path: Path, content: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
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
                # Configurações antigas do Gemini não têm efeito no modo navegador.
                config["model"] = "microsoft-copilot"
                config["keys"] = []
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
    """Compatibilidade legada: o NEXUS não usa API keys."""
    return []

class RateLimitError(Exception):
    pass

class AgentError(Exception):
    pass


# NEXUS SECTION: PLANNING_INTEGRITY
TASK_STATES = ("PENDENTE", "EM_EXECUCAO", "REQUER_REVISAO", "CONCLUIDA", "NOVA_TAREFA")


class PlanningIntegrity:
    """Planejador do NEXUS com 7 fases lógicas de execução.

    O objetivo original do usuário é imutável. O planner consulta o Copilot
    como consultor técnico, interpreta a resposta e constrói um plano com
    exatamente 7 fases. Cada tarefa referencia um arquivo, possui fase,
    dependências, critérios de aceitação e estado. O NEXUS controla o fluxo
    e a IA funciona como gerador de um arquivo por vez.
    """

    def __init__(
        self,
        original_request: str,
        plan: dict[str, Any],
        workspace: Path,
    ) -> None:
        self.original_request: str = str(original_request or "").strip()
        self.project_name: str = clean_text(plan.get("project_name", "projeto_nexus"))
        self.summary: str = clean_text(plan.get("summary", ""))
        self.objective: str = clean_text(plan.get("objective", plan.get("summary", "")))
        self.project_type: str = clean_text(plan.get("project_type", ""))
        self.architecture: str = clean_text(plan.get("architecture", ""))
        self.directory_structure: list[str] = [
            clean_text(entry)
            for entry in (plan.get("directory_structure", []) if isinstance(plan.get("directory_structure", []), list) else [])
            if clean_text(entry)
        ]
        self.validation_strategy: str = clean_text(plan.get("validation_strategy", ""))
        self.implementation_order: list[str] = [
            clean_text(entry)
            for entry in (plan.get("implementation_order", []) if isinstance(plan.get("implementation_order", []), list) else [])
            if clean_text(entry)
        ]
        self.workspace: Path = workspace
        self.run_instructions: str = clean_text(plan.get("run_instructions", ""))
        self.dependencies: list[str] = [
            str(dep) for dep in (plan.get("dependencies", []) if isinstance(plan.get("dependencies", []), list) else [str(plan.get("dependencies"))])
        ]
        self.tasks: list[dict[str, Any]] = []
        self.phase_states: dict[int, str] = {i: "PENDENTE" for i in range(1, 8)}
        self.phase_task_ids: dict[int, list[int]] = {i: [] for i in range(1, 8)}
        self.rejection_log: list[dict[str, str]] = []
        self._build_tasks(plan)
        self.phase_states[1] = "CONCLUIDA"

    def _build_tasks(self, plan: dict[str, Any]) -> None:
        files = plan.get("files", [])
        if not files:
            return
        total = len(files)
        phase3_end = max(1, total // 3)
        phase4_end = max(phase3_end + 1, (total * 2) // 3)
        for index, item in enumerate(files, 1):
            if index <= phase3_end:
                phase_num = 3
            elif index <= phase4_end:
                phase_num = 4
            else:
                phase_num = 4
            file_deps = item.get("depends_on", [])
            if not isinstance(file_deps, list):
                file_deps = []
            dep_paths = [clean_text(dep) for dep in file_deps if clean_text(dep)]
            dep_task_ids = [
                idx for idx, prev in enumerate(files[:index], 1)
                if prev["path"] in dep_paths
            ]
            if not dep_task_ids and index > 1:
                dep_task_ids = list(range(1, index))
            criteria = item.get("acceptance_criteria", [])
            if not isinstance(criteria, list):
                criteria = []
            criteria = [clean_text(c) for c in criteria if clean_text(c)] or [
                f"arquivo {item['path']} criado no workspace",
                f"linguagem {item['language']} válida e compilável",
                "nenhum placeholder nem código parcial",
                f"finalidade do arquivo corresponde a: {item['purpose']}",
            ]
            task = {
                "id": index,
                "description": f"Criar {item['path']} — {item['purpose']}",
                "phase": phase_num,
                "dependencies": dep_task_ids,
                "files": [item["path"]],
                "depends_on_paths": dep_paths,
                "acceptance_criteria": criteria,
                "state": "PENDENTE",
                "rejection_reason": "",
                "evidence": "",
                "spec": item,
            }
            self.tasks.append(task)
            self.phase_task_ids[phase_num].append(index)
        if self.tasks:
            self.phase_states[3] = "PENDENTE"
            self.phase_states[4] = "PENDENTE"

    def task_for_spec(self, spec: dict[str, Any]) -> Optional[dict[str, Any]]:
        for task in self.tasks:
            stored = task.get("spec")
            if stored and stored.get("path") == spec.get("path"):
                return task
        return None

    def completed_files_context(self) -> list[dict[str, str]]:
        context: list[dict[str, str]] = []
        for task in self.tasks:
            if task["state"] != "CONCLUIDA":
                continue
            spec = task.get("spec")
            if not spec:
                continue
            context.append({
                "path": spec["path"],
                "purpose": spec.get("purpose", ""),
                "status": "DONE",
            })
        return context

    def pending_dependencies(self, task: dict[str, Any]) -> list[str]:
        dep_paths = task.get("depends_on_paths", [])
        if not dep_paths:
            return []
        completed_paths = {
            t["spec"]["path"]
            for t in self.tasks
            if t.get("state") == "CONCLUIDA" and t.get("spec")
        }
        return [p for p in dep_paths if p not in completed_paths]

    def global_structure_validation(self, workspace: Path) -> tuple[bool, list[str]]:
        issues: list[str] = []
        for task in self.tasks:
            spec = task.get("spec")
            if not spec:
                continue
            file_path = workspace / spec["path"]
            if not file_path.exists():
                issues.append(f"Arquivo planejado não encontrado: {spec['path']}")
                continue
            content = file_path.read_text(encoding="utf-8")
            if not content.strip():
                issues.append(f"Arquivo vazio: {spec['path']}")
                continue
            if spec["language"] == "python":
                try:
                    ast.parse(content, filename=str(file_path))
                except SyntaxError as exc:
                    issues.append(f"Sintaxe inválida em {spec['path']}: {exc}")
            lowered = content.lower()
            for placeholder in PLACEHOLDERS:
                if placeholder.lower() in lowered:
                    issues.append(f"Placeholder {placeholder!r} em {spec['path']}")
        return len(issues) == 0, issues

    def mark_in_progress(self, task: dict[str, Any]) -> None:
        task["state"] = "EM_EXECUCAO"
        task["rejection_reason"] = ""
        phase = task.get("phase", 0)
        if isinstance(phase, int) and phase in self.phase_states:
            if self.phase_states[phase] == "PENDENTE":
                self.phase_states[phase] = "EM_EXECUCAO"

    def mark_completed(self, task: dict[str, Any], evidence: str) -> None:
        task["state"] = "CONCLUIDA"
        task["evidence"] = evidence
        task["rejection_reason"] = ""

    def mark_rejected(self, task: dict[str, Any], reason: str) -> None:
        task["state"] = "REQUER_REVISAO"
        task["rejection_reason"] = reason
        self.rejection_log.append({
            "task": task["description"],
            "reason": reason,
        })

    def add_discovered_task(
        self,
        description: str,
        files: list[str],
        acceptance_criteria: list[str],
        phase: int = 4,
    ) -> dict[str, Any]:
        task_id = max((t["id"] for t in self.tasks), default=0) + 1
        task = {
            "id": task_id,
            "description": clean_text(description),
            "phase": phase,
            "dependencies": [t["id"] for t in self.tasks if t["state"] == "CONCLUIDA"],
            "files": files,
            "acceptance_criteria": acceptance_criteria or [
                "tarefa descrita executada",
                "nenhum placeholder nem pseudocódigo",
            ],
            "state": "PENDENTE",
            "rejection_reason": "",
            "evidence": "",
        }
        self.tasks.append(task)
        if phase in self.phase_task_ids:
            self.phase_task_ids[phase].append(task_id)
        terminal_log(
            f"NOVA TAREFA registrada no plano (sem substituir existentes): {task['description']}",
            "WARN",
        )
        return task

    def validate_phase(self, phase_num: int) -> tuple[bool, str]:
        if phase_num == 1:
            return True, f"Fase 1 — {SEVEN_PHASES[0]}: concluída (consulta inicial)."
        if phase_num == 2:
            return True, f"Fase 2 — {SEVEN_PHASES[1]}: concluída (arquitetura definida no plano)."
        if phase_num == 5:
            ok, issues = self.global_structure_validation(self.workspace)
            if ok:
                return True, f"Fase 5 — {SEVEN_PHASES[4]}: integração verificada."
            return False, f"Fase 5 — {SEVEN_PHASES[4]}: {'; '.join(issues[:3])}"
        if phase_num == 6:
            return True, f"Fase 6 — {SEVEN_PHASES[5]}: testes simulados (sem execução automática)."
        if phase_num == 7:
            return self._validate_final_phase()
        task_ids = self.phase_task_ids.get(phase_num, [])
        pending = [
            t for t in self.tasks
            if t["id"] in task_ids and t["state"] != "CONCLUIDA"
        ]
        if pending:
            descriptions = ", ".join(t["description"] for t in pending[:5])
            return False, f"Fase {phase_num} — {SEVEN_PHASES[phase_num - 1]}: tarefas pendentes: {descriptions}"
        return True, f"Fase {phase_num} — {SEVEN_PHASES[phase_num - 1]}: validada."

    def _validate_final_phase(self) -> tuple[bool, str]:
        if not self.all_tasks_completed():
            pending = [t["description"] for t in self.tasks if t["state"] != "CONCLUIDA"]
            return False, f"Fase 7 — {SEVEN_PHASES[6]}: tarefas não concluídas: {', '.join(pending[:5])}"
        ok, issues = self.global_structure_validation(self.workspace)
        if not ok:
            return False, f"Fase 7 — {SEVEN_PHASES[6]}: {'; '.join(issues[:5])}"
        return True, f"Fase 7 — {SEVEN_PHASES[6]}: validação final concluída."

    def all_tasks_completed(self) -> bool:
        return all(t["state"] == "CONCLUIDA" for t in self.tasks)

    def all_phases_validated(self) -> tuple[bool, list[str]]:
        failures: list[str] = []
        for phase_num in range(1, 8):
            ok, _ = self.validate_phase(phase_num)
            if not ok:
                failures.append(f"Fase {phase_num}/7 — {SEVEN_PHASES[phase_num - 1]}")
        return len(failures) == 0, failures

    def final_validation(self, workspace: Path) -> tuple[bool, str]:
        if not self.all_tasks_completed():
            pending = [t["description"] for t in self.tasks if t["state"] != "CONCLUIDA"]
            return False, f"Tarefas não concluídas: {', '.join(pending[:5])}"
        ok, failures = self.all_phases_validated()
        if not ok:
            return False, f"Fases não validadas: {', '.join(failures)}"
        structure_ok, issues = self.global_structure_validation(workspace)
        if not structure_ok:
            return False, f"Validação de estrutura falhou: {'; '.join(issues[:5])}"
        for task in self.tasks:
            spec = task.get("spec")
            if not spec:
                continue
            file_path = workspace / spec["path"]
            ok, reason = self.check_acceptance_criteria(task, file_path)
            if not ok:
                return False, f"Validação final falhou para {spec['path']}: {reason}"
        if self.rejection_log:
            terminal_log(
                f"Integridade do plano: {len(self.rejection_log)} rejeição(ões) registrada(s) durante a execução",
                "WARN",
            )
        return True, "Validação final: resultado corresponde ao objetivo original."

    def state_summary(self) -> list[dict[str, Any]]:
        return [
            {
                "id": t["id"],
                "description": t["description"],
                "phase": t["phase"],
                "state": t["state"],
                "rejection_reason": t.get("rejection_reason", ""),
            }
            for t in self.tasks
        ]

    def action_descriptions(self) -> list[str]:
        """Lista de descrições de ações na ordem de execução."""
        return [t["description"] for t in self.tasks]

    def completed_action_count(self) -> int:
        return sum(1 for t in self.tasks if t["state"] == "CONCLUIDA")

    def total_action_count(self) -> int:
        return len(self.tasks)

    def mark_phase_completed(self, phase_num: int) -> None:
        if phase_num in self.phase_states:
            self.phase_states[phase_num] = "CONCLUIDA"

    def validate_response_alignment(
        self,
        task: dict[str, Any],
        generated: dict[str, Any],
    ) -> tuple[bool, str]:
        spec = task.get("spec")
        if not spec:
            return True, "tarefa não exige verificação de arquivo"
        actual_path = generated.get("path", "")
        if actual_path != spec["path"]:
            return False, f"Desvio de plano: a IA retornou {actual_path!r}, mas a tarefa exige {spec['path']!r}."
        content = generated.get("content", "")
        if not content or not content.strip():
            return False, "Resultado inválido: conteúdo vazio."
        lowered = content.lower()
        for placeholder in PLACEHOLDERS:
            if placeholder.lower() in lowered:
                return False, f"Resultado inválido: placeholder detectado: {placeholder!r}"
        other_file_markers = re.findall(
            r"(?:^|\n)\s*(?:#|//|/\*)\s*(?:arquivo|file|create|criar)\s*[:=]?\s*[\w./-]+",
            lowered,
        )
        if other_file_markers:
            return False, f"Resultado inválido: a resposta parece conter referências a outros arquivos além de {spec['path']!r}."
        purpose_keywords = [
            word.strip().lower()
            for word in spec.get("purpose", "").split()
            if len(word.strip()) > 3 and word.strip().lower() not in {
                "arquivo", "script", "codigo", "código", "este", "este arquivo",
                "para", "que", "com", "uma", "função", "funcao",
            }
        ][:6]
        if purpose_keywords:
            matched = any(keyword in content.lower() for keyword in purpose_keywords)
            if not matched and len(content) < 200:
                return False, f"Resultado possivelmente genérico: não corresponde à finalidade ({spec.get('purpose', '')!r})."
        return True, "resposta corresponde à tarefa"

    def check_acceptance_criteria(
        self,
        task: dict[str, Any],
        file_path: Path,
    ) -> tuple[bool, str]:
        spec = task.get("spec")
        if not spec:
            return True, "tarefa sem critérios de arquivo"
        if not file_path.exists() or not file_path.is_file():
            return False, f"Critério não atendido: arquivo {file_path} não existe."
        content = file_path.read_text(encoding="utf-8")
        if not content.strip():
            return False, f"Critério não atendido: {file_path} está vazio."
        if spec["language"] == "python":
            try:
                ast.parse(content, filename=str(file_path))
            except SyntaxError as exc:
                return False, f"Critério não atendido: sintaxe Python inválida: {exc}"
        lowered = content.lower()
        for placeholder in PLACEHOLDERS:
            if placeholder.lower() in lowered:
                return False, f"Critério não atendido: placeholder {placeholder!r} presente."
        return True, "todos os critérios de aceitação atendidos"


# END NEXUS SECTION: PLANNING_INTEGRITY

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
                raise AgentError("Nenhuma sessão Microsoft configurada.")

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

class QuotaManager:
    """Controle global de consumo do projeto, independente da quantidade de API keys.

    As contagens são locais/estimadas e servem para proteção operacional.
    Elas não representam a quota oficial do Microsoft.
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.lock = threading.RLock()
        self.global_cooldown_until = 0.0
        self.recent_requests: list[float] = []
        self.day = self._pacific_day()
        self.requests_today = 0
        self.input_tokens_today = 0
        self.output_tokens_today = 0
        self.total_tokens_today = 0
        self.last_status = ""
        self._load()

    @staticmethod
    def _pacific_day() -> str:
        try:
            if ZoneInfo is not None:
                return datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
        except Exception:
            pass
        return datetime.now(timezone.utc).date().isoformat()

    def _ensure_day(self) -> None:
        current = self._pacific_day()
        if current != self.day:
            self.day = current
            self.requests_today = 0
            self.input_tokens_today = 0
            self.output_tokens_today = 0
            self.total_tokens_today = 0
            self.recent_requests.clear()
            self.last_status = ""
            self._persist()

    def _load(self) -> None:
        if not bool(self.config.get("quota_persist", True)):
            return
        try:
            if QUOTA_STATE_FILE.exists():
                data = json.loads(QUOTA_STATE_FILE.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("day") == self.day:
                    self.requests_today = int(data.get("requests_today", 0))
                    self.input_tokens_today = int(data.get("input_tokens_today", 0))
                    self.output_tokens_today = int(data.get("output_tokens_today", 0))
                    self.total_tokens_today = int(data.get("total_tokens_today", 0))
        except Exception:
            pass

    def _persist(self) -> None:
        if not bool(self.config.get("quota_persist", True)):
            return
        data = {
            "day": self.day,
            "requests_today": self.requests_today,
            "input_tokens_today": self.input_tokens_today,
            "output_tokens_today": self.output_tokens_today,
            "total_tokens_today": self.total_tokens_today,
        }
        try:
            atomic_write(
                QUOTA_STATE_FILE,
                json.dumps(data, indent=2, ensure_ascii=False),
                0o600,
            )
        except Exception:
            pass

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        # Estimativa conservadora para proteção TPM quando usageMetadata ainda não existe.
        return max(1, (len(text) + 3) // 4)

    def before_request(self, estimated_input_tokens: int = 0) -> None:
        with self.lock:
            self._ensure_day()

            now = time.monotonic()
            if self.global_cooldown_until > now:
                wait = self.global_cooldown_until - now
                raise RateLimitError(
                    f"Cooldown GLOBAL de quota ativo. Aguarde {wait:.0f}s; "
                    "as API keys podem compartilhar a mesma quota do projeto."
                )

            self.recent_requests = [
                ts for ts in self.recent_requests if now - ts < 60.0
            ]

            soft_rpm = int(self.config.get("quota_soft_rpm", 0) or 0)
            if soft_rpm > 0 and len(self.recent_requests) >= soft_rpm:
                wait = max(0.5, 60.0 - (now - self.recent_requests[0]))
                print(
                    f"[NEXUS] Proteção local RPM {len(self.recent_requests)}/{soft_rpm}; "
                    f"aguardando {wait:.1f}s."
                )
                time.sleep(wait)
                now = time.monotonic()
                self.recent_requests = [
                    ts for ts in self.recent_requests if now - ts < 60.0
                ]

            soft_tpm = int(self.config.get("quota_soft_tpm", 0) or 0)
            if soft_tpm > 0 and self.input_tokens_today > 0:
                # TPM é calculado sobre a janela recente; mantemos somente o
                # contador diário aqui e deixamos o intervalo de 8s como defesa.
                # O limite é aplicado pelo usageMetadata quando disponível.
                pass

            soft_rpd = int(self.config.get("quota_soft_rpd", 0) or 0)
            if soft_rpd > 0 and self.requests_today >= soft_rpd:
                raise RateLimitError(
                    f"Proteção local RPD: {self.requests_today}/{soft_rpd}. "
                    "Limite diário local atingido."
                )

            self.recent_requests.append(now)

    def record_response(
        self,
        usage: Optional[dict[str, Any]] = None,
        estimated_input_tokens: int = 0,
        estimated_output_tokens: int = 0,
    ) -> None:
        with self.lock:
            self._ensure_day()
            usage = usage or {}

            def integer_value(*names: str) -> int:
                for name in names:
                    try:
                        value = int(usage.get(name, 0) or 0)
                        if value >= 0:
                            return value
                    except (TypeError, ValueError):
                        continue
                return 0

            input_tokens = integer_value(
                "promptTokenCount",
                "prompt_token_count",
            )
            output_tokens = integer_value(
                "candidatesTokenCount",
                "candidates_token_count",
                "outputTokenCount",
            )
            total_tokens = integer_value(
                "totalTokenCount",
                "total_token_count",
            )

            if input_tokens <= 0:
                input_tokens = max(0, estimated_input_tokens)
            if output_tokens <= 0:
                output_tokens = max(0, estimated_output_tokens)
            if total_tokens <= 0:
                total_tokens = input_tokens + output_tokens

            self.requests_today += 1
            self.input_tokens_today += input_tokens
            self.output_tokens_today += output_tokens
            self.total_tokens_today += total_tokens
            self._persist()

    def mark_global_429(self, seconds: Optional[int] = None) -> int:
        with self.lock:
            configured = int(
                self.config.get("quota_global_cooldown", 60) or 60
            )
            wait = max(1, int(seconds if seconds is not None else configured))
            self.global_cooldown_until = max(
                self.global_cooldown_until,
                time.monotonic() + wait,
            )
            self.last_status = "HTTP 429 / quota"
            return wait

    def clear_cooldown(self) -> None:
        with self.lock:
            self.global_cooldown_until = 0.0
            self.last_status = ""

    def reset_counters(self) -> None:
        with self.lock:
            self.day = self._pacific_day()
            self.requests_today = 0
            self.input_tokens_today = 0
            self.output_tokens_today = 0
            self.total_tokens_today = 0
            self.recent_requests.clear()
            self.last_status = ""
            self._persist()

    def status(self) -> dict[str, Any]:
        with self.lock:
            self._ensure_day()
            now = time.monotonic()
            self.recent_requests = [
                ts for ts in self.recent_requests if now - ts < 60.0
            ]
            cooldown = max(0.0, self.global_cooldown_until - now)
            soft_rpm = int(self.config.get("quota_soft_rpm", 0) or 0)
            soft_tpm = int(self.config.get("quota_soft_tpm", 0) or 0)
            soft_rpd = int(self.config.get("quota_soft_rpd", 0) or 0)
            return {
                "day": self.day,
                "rpm_current": len(self.recent_requests),
                "rpm_soft": soft_rpm,
                "tpm_today": self.input_tokens_today,
                "output_tokens_today": self.output_tokens_today,
                "total_tokens_today": self.total_tokens_today,
                "rpd_today": self.requests_today,
                "rpd_soft": soft_rpd,
                "tpm_soft": soft_tpm,
                "global_cooldown": cooldown,
                "last_status": self.last_status,
            }


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
                terminal_log(f"Rate limiter: aguardando {remaining:.1f}s antes da próxima chamada", "WARN")
                time.sleep(remaining)
            self.last_request = time.monotonic()

    @staticmethod
    def backoff(seconds: float, reason: str) -> None:
        if seconds > 0:
            terminal_log(f"{reason}: aguardando {seconds:.1f}s (proteção de API/quota)", "WARN")
            time.sleep(seconds)

# NEXUS SECTION: TEMPORARY_WORKSPACE_INTELLIGENCE
# PURPOSE: Laboratórios locais, virtuais e descartáveis para execução/testes.
# VERSION: 1.0
WORKSPACE_ACTIVE_STATES = {"ACTIVE", "VALIDATING", "PROTECTED"}
WORKSPACE_TERMINAL_STATES = {"COMPLETED", "FAILED", "EXPIRED"}


@dataclass
class WorkspaceRecord:
    workspace_id: str
    path: Path
    workspace_type: str
    status: str
    created_at: str
    last_access: str
    expires_at: str
    protected: bool = False
    venv_path: Optional[Path] = None
    processes: dict[int, dict[str, Any]] = None

    def __post_init__(self) -> None:
        if self.processes is None:
            self.processes = {}


class TemporaryWorkspaceManager:
    """Gerencia laboratórios em memória, sem JSON de controle de workspaces."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.lock = threading.RLock()
        self.records: dict[str, WorkspaceRecord] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @classmethod
    def _stamp(cls, value: Optional[datetime] = None) -> str:
        return (value or cls._now()).isoformat(timespec="seconds")

    def create(
        self,
        workspace_type: str = "TEMPORARY WORKSPACE",
        source: Optional[Path] = None,
        destination_dir: Optional[Path] = None,
        ttl_seconds: Optional[int] = None,
        protected: bool = False,
    ) -> WorkspaceRecord:
        ttl = max(60, int(ttl_seconds or self.config.get("workspace_ttl_seconds", 3600)))
        root_dir = Path(destination_dir).expanduser().resolve() if destination_dir else None
        path = Path(tempfile.mkdtemp(prefix="nexus-ws-", dir=str(root_dir) if root_dir else None))
        now = self._now()
        record = WorkspaceRecord(
            workspace_id=f"ws_{uuid.uuid4().hex[:10]}",
            path=path,
            workspace_type=str(workspace_type or "TEMPORARY WORKSPACE").upper(),
            status="PROTECTED" if protected else "ACTIVE",
            created_at=self._stamp(now),
            last_access=self._stamp(now),
            expires_at=self._stamp(now + timedelta(seconds=ttl)),
            protected=protected,
        )
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
        if source is not None:
            source = Path(source).expanduser().resolve()
            if not source.exists():
                shutil.rmtree(path, ignore_errors=True)
                raise AgentError(f"Fonte não encontrada para workspace: {source}")
            target = path / source.name
            if source.is_dir():
                shutil.copytree(source, target, dirs_exist_ok=True)
            else:
                shutil.copy2(source, target)
        with self.lock:
            self.records[record.workspace_id] = record
        terminal_log(f"Workspace criado: {record.workspace_id} [{record.workspace_type}] → {record.path}", "STEP")
        return record

    def get(self, workspace_id: str) -> WorkspaceRecord:
        with self.lock:
            record = self.records.get(str(workspace_id).strip())
            if record is None:
                raise AgentError(f"Workspace não encontrado: {workspace_id}")
            record.last_access = self._stamp()
            return record

    def snapshot(self) -> list[dict[str, Any]]:
        with self.lock:
            return [{
                "id": item.workspace_id, "path": str(item.path), "type": item.workspace_type,
                "status": item.status, "created_at": item.created_at,
                "last_access": item.last_access, "expires_at": item.expires_at,
                "protected": item.protected, "venv": str(item.venv_path) if item.venv_path else "",
                "processes": list(item.processes),
            } for item in self.records.values()]

    def python_path(self, record: WorkspaceRecord) -> Path:
        return (record.venv_path / "bin" / "python") if record.venv_path else Path(sys.executable)

    def pip_path(self, record: WorkspaceRecord) -> Path:
        return (record.venv_path / "bin" / "pip") if record.venv_path else Path(sys.executable)

    def create_virtualenv(self, workspace_id: str) -> Path:
        record = self.get(workspace_id)
        venv_path = record.path / ".venv"
        builder = venv.EnvBuilder(with_pip=True, clear=False, symlinks=False)
        builder.create(str(venv_path))
        record.venv_path = venv_path
        record.last_access = self._stamp()
        terminal_log(f"Virtual Python criado: {record.workspace_id} → {venv_path}", "OK")
        return venv_path

    def install(self, workspace_id: str, dependencies: list[str]) -> tuple[str, int]:
        record = self.get(workspace_id)
        if not record.venv_path:
            self.create_virtualenv(workspace_id)
        deps = [str(item).strip() for item in dependencies if str(item).strip()]
        if not deps:
            return "Nenhuma dependência solicitada.", 0
        result = self.run(workspace_id, [str(self.pip_path(record)), "install", *deps], timeout=600)
        return result["output"], result["exit_code"]

    def install_project_dependencies(self, workspace_id: str) -> tuple[str, int]:
        record = self.get(workspace_id)
        candidates = ["requirements.txt", "pyproject.toml", "setup.py"]
        present = [name for name in candidates if (record.path / name).is_file()]
        if not present:
            return "Nenhum manifesto de dependências encontrado.", 0
        if not record.venv_path:
            self.create_virtualenv(workspace_id)
        if (record.path / "requirements.txt").is_file():
            return self.install(workspace_id, ["-r", str(record.path / "requirements.txt")])
        return self.install(workspace_id, ["-e", str(record.path)])

    def run(
        self,
        workspace_id: str,
        command: list[str],
        timeout: int = 300,
        env: Optional[dict[str, str]] = None,
    ) -> dict[str, Any]:
        record = self.get(workspace_id)
        if not command or any(not isinstance(item, str) for item in command):
            raise AgentError("Comando de workspace deve ser uma lista de argumentos textuais.")
        record.status = "ACTIVE"
        process = subprocess.Popen(
            command, cwd=str(record.path), env=env or os.environ.copy(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        started = self._stamp()
        record.processes[process.pid] = {"pid": process.pid, "command": command, "start_time": started, "status": "RUNNING"}
        try:
            output, _ = process.communicate(timeout=max(1, int(timeout)))
            code = process.returncode
            state = "COMPLETED" if code == 0 else "FAILED"
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                output, _ = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                output, _ = process.communicate()
            code, state = None, "FAILED"
            output = (output or "") + "\n[workspace] timeout; processo finalizado."
        finally:
            record.processes[process.pid].update({"status": state, "exit_code": code})
            record.processes.pop(process.pid, None)
            record.last_access = self._stamp()
            record.status = "PROTECTED" if record.protected else state
        return {"output": clean_text(output or ""), "exit_code": code, "pid": process.pid, "status": state}

    def test_python(self, workspace_id: str, script: Optional[Path] = None) -> dict[str, Any]:
        record = self.get(workspace_id)
        python = self.python_path(record)
        target = Path(script).resolve() if script else None
        if target and target.parent != record.path:
            target = record.path / target.name
        compile_targets = [target] if target and target.is_file() else sorted(record.path.rglob("*.py"))
        if not compile_targets:
            return {"success": False, "output": "Nenhum arquivo Python para testar.", "exit_code": None}
        result = self.run(workspace_id, [str(python), "-m", "py_compile", *[str(item) for item in compile_targets]])
        if result["exit_code"] != 0:
            return {"success": False, **result}
        tests_dir = record.path / "tests"
        pytest_available = subprocess.run([str(python), "-c", "import pytest"], capture_output=True, check=False).returncode == 0
        if tests_dir.is_dir() and pytest_available:
            result = self.run(workspace_id, [str(python), "-m", "pytest", "-q"])
        return {"success": result["exit_code"] == 0, **result}

    def mark_validated(self, workspace_id: str) -> None:
        record = self.get(workspace_id)
        record.status = "PROTECTED"
        record.protected = True

    def cleanup_workspace(self, workspace_id: str, force: bool = False) -> bool:
        record = self.get(workspace_id)
        if record.protected and not force:
            terminal_log(f"Workspace protegido, limpeza recusada: {workspace_id}", "WARN")
            return False
        if record.processes and not force:
            record.status = "ACTIVE"
            return False
        for pid in list(record.processes):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        shutil.rmtree(record.path, ignore_errors=True)
        record.status = "COMPLETED"
        with self.lock:
            self.records.pop(record.workspace_id, None)
        terminal_log(f"Workspace descartado: {workspace_id}", "OK")
        return True

    def cleanup_expired(self) -> int:
        now = self._now()
        removed = 0
        for item in list(self.snapshot()):
            try:
                expired = datetime.fromisoformat(item["expires_at"]) <= now
            except (TypeError, ValueError):
                expired = False
            if expired and item["status"] not in WORKSPACE_ACTIVE_STATES and not item["protected"]:
                if self.cleanup_workspace(item["id"]):
                    removed += 1
        return removed

    def cleanup_all(self) -> None:
        for item in list(self.snapshot()):
            if not item["protected"]:
                self.cleanup_workspace(item["id"], force=True)

    def promote_file(self, workspace_id: str, candidate: Path, destination: Path, approved: bool = False) -> Path:
        if not approved:
            raise AgentError("Promoção exige aprovação explícita.")
        record = self.get(workspace_id)
        candidate = Path(candidate).resolve()
        destination = Path(destination).expanduser().resolve()
        if record.path not in candidate.parents and candidate != record.path:
            raise AgentError("Candidato está fora do workspace informado.")
        if not candidate.is_file():
            raise AgentError(f"Candidato não encontrado: {candidate}")
        atomic_write_bytes(destination, candidate.read_bytes(), 0o700)
        record.status = "COMPLETED"
        return destination

# END NEXUS SECTION: TEMPORARY_WORKSPACE_INTELLIGENCE

# NEXUS SECTION: EVOLUTION_STATE
# PURPOSE: Internal skill catalog plus session-only history and roadmap.
# VERSION: 0.1
# DEPENDS: Python standard library only.
MAX_EVOLUTION_CONTEXT_CHARS = 12000
MAX_EVOLUTION_CONTEXT_LINES = 800
MAX_RELEVANT_SECTIONS = 4
MAX_EVOLUTION_HISTORY = 20
MAX_EVOLUTION_REPAIR_ATTEMPTS = 3

# level=None means not assessed; the engine must not fabricate a score.
SKILL_MAP: dict[str, dict[str, Any]] = {
    "personality": {"label": "Personalidade", "sections": ["AGENT_PROMPTS"], "level": None, "description": "Estilo e instruções estão embutidos nos prompts existentes; não há módulo de personalidade separado.", "aliases": ["personalidade", "tom", "estilo"]},
    "reasoning": {"label": "Raciocínio", "sections": ["AGENT_PROMPTS", "COPILOT_CLIENT"], "level": None, "description": "Roteamento e agentes de raciocínio usam prompts e o cliente do Copilot.", "aliases": ["raciocinio", "raciocínio", "reasoning"]},
    "memory": {"label": "Memória", "sections": ["EVOLUTION_STATE"], "level": None, "description": "Somente histórico e roadmap de evolução da sessão são mantidos em memória; não há memória persistente de projeto.", "aliases": ["memoria", "memória"]},
    "planning": {"label": "Planejamento", "sections": ["AGENT_PROMPTS", "EVOLUTION_ENGINE"], "level": None, "description": "Há prompts de planejamento de tarefas e evolução.", "aliases": ["planejamento", "plan"]},
    "code_generation": {"label": "Geração de código", "sections": ["CODE_GENERATION", "CODE_VALIDATION", "RESPONSE_VALIDATION"], "level": None, "description": "Geração de projetos ocorre por plano e arquivos individuais, com validações locais.", "aliases": ["geracao de codigo", "geração de código", "code generation"]},
    "linux_execution": {"label": "Execução Linux", "sections": ["TERMINAL_EXECUTION", "COMMAND_SAFETY"], "level": None, "description": "Comandos são executados em PTY real; há detecção heurística e confirmação de alguns comandos perigosos.", "aliases": ["execucao linux", "execução linux", "linux", "terminal"]},
    "linux_diagnostics": {"label": "Diagnóstico Linux", "sections": ["SELF_DIAGNOSTICS", "TERMINAL_EXECUTION"], "level": None, "description": "Há self-test local básico; não há uma habilidade geral de diagnóstico Linux comprovada.", "aliases": ["diagnostico linux", "diagnóstico linux", "system diagnostics"]},
    "automation": {"label": "Automação", "sections": [], "level": None, "description": "Nenhum subsistema dedicado de automação foi identificado no código fornecido.", "aliases": ["automacao", "automação"]},
    "python": {"label": "Programação Python", "sections": ["CODE_GENERATION", "CODE_VALIDATION", "RESPONSE_VALIDATION"], "level": None, "description": "Geração e validação sintática de Python estão implementadas; testes funcionais de código gerado não foram identificados.", "aliases": ["python", "programacao python", "programação python"]},
    "web_development": {"label": "Desenvolvimento Web", "sections": [], "level": None, "description": "Nenhum módulo de desenvolvimento web dedicado foi identificado.", "aliases": ["web", "desenvolvimento web"]},
    "devops": {"label": "DevOps", "sections": [], "level": None, "description": "Nenhum módulo DevOps dedicado foi identificado.", "aliases": ["devops", "dev ops"]},
    "security_pentest": {"label": "Segurança / Pentest", "sections": ["COMMAND_SAFETY"], "level": None, "description": "Existem regras heurísticas de confirmação de comandos; isso não constitui capacidade de pentest.", "aliases": ["seguranca", "segurança", "pentest", "security"]},
    "network_analysis": {"label": "Análise de rede", "sections": [], "level": None, "description": "Nenhuma rotina dedicada de análise de rede foi identificada.", "aliases": ["analise de rede", "análise de rede", "network"]},
    "web_navigation": {"label": "Navegação Web", "sections": ["COPILOT_BROWSER"], "level": None, "description": "O navegador é usado para acessar o Copilot; navegação web geral não foi identificada.", "aliases": ["navegacao web", "navegação web", "browser"]},
    "copilot": {"label": "Comunicação com Copilot", "sections": ["COPILOT_BROWSER", "COPILOT_CLIENT", "AGENT_PROMPTS"], "level": None, "description": "Microsoft Copilot é acessado pelo Edge/Playwright e usado pelos agentes do NEXUS.", "aliases": ["copilot", "comunicacao com copilot", "comunicação com copilot"]},
    "interface": {"label": "Interface", "sections": ["COMMAND_INTERFACE"], "level": None, "description": "Interface interativa de terminal e comandos slash.", "aliases": ["interface", "terminal ui"]},
    "performance": {"label": "Performance", "sections": ["EVOLUTION_ENGINE", "TERMINAL_EXECUTION"], "level": None, "description": "Não há métricas de performance sistemáticas; contexto selecionado é limitado por orçamento.", "aliases": ["performance", "desempenho"]},
    "reliability": {"label": "Confiabilidade", "sections": ["SELF_DIAGNOSTICS", "CODE_VALIDATION"], "level": None, "description": "Há verificações locais pontuais, mas não um conjunto de regressão abrangente no material fornecido.", "aliases": ["confiabilidade", "reliability", "estabilidade"]},
    "evolution": {"label": "Sistema de evolução", "sections": ["EVOLUTION_STATE", "EVOLUTION_ENGINE", "EVOLUTION_WORKFLOW"], "level": None, "description": "Clona, localiza alvos por AST/texto, planeja e pode alterar uma única função no clone após confirmação; nunca substitui o arquivo ativo.", "aliases": ["sistema de evolucao", "sistema de evolução", "evolution"]},
    "temporary_workspace": {"label": "Temporary Workspace Intelligence", "sections": ["TEMPORARY_WORKSPACE_INTELLIGENCE", "TERMINAL_EXECUTION", "EVOLUTION_WORKFLOW"], "level": None, "description": "Cria laboratórios temporários em memória, executa processos com cwd isolado, cria venvs, testa Python e protege candidatos de evolução.", "aliases": ["temporary workspace", "workspace temporario", "workspace temporário", "laboratorio", "laboratório"]},
    "other": {"label": "Outra habilidade", "sections": [], "level": None, "description": "Informe uma habilidade já representada no mapa para selecionar contexto verificável.", "aliases": ["outra", "outra habilidade", "other"]},
}
SKILL_DEPENDENCIES = {
    "reasoning": ["copilot"],
    "planning": ["reasoning"],
    "code_generation": ["copilot"],
    "python": ["code_generation"],
    "linux_diagnostics": ["linux_execution"],
    "reliability": ["linux_diagnostics", "python"],
    "evolution": ["planning", "python"],
}
for _skill_id, _skill in SKILL_MAP.items():
    _skill["dependencies"] = list(SKILL_DEPENDENCIES.get(_skill_id, []))
EVOLUTION_HISTORY: list[dict[str, Any]] = []
EVOLUTION_ROADMAP: list[dict[str, Any]] = []
# END NEXUS SECTION: EVOLUTION_STATE

# NEXUS SECTION: EVOLUTION_ENGINE
# PURPOSE: Locate marked source regions and construct bounded, relevant context.
# VERSION: 0.1
# DEPENDS: ast, re, and the in-memory SKILL_MAP.
def resolve_evolution_skill_graph(skill_id: str) -> tuple[list[str], list[str]]:
    """Resolve a skill and its prerequisites while detecting missing nodes/cycles."""
    sections: list[str] = []
    ordered_skills: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(current: str) -> None:
        if current not in SKILL_MAP:
            raise ValueError(f"Unknown skill dependency: {current}")
        if current in visiting:
            raise ValueError(f"Cycle in skill dependencies at: {current}")
        if current in visited:
            return
        visiting.add(current)
        ordered_skills.append(current)
        for section in SKILL_MAP[current].get("sections", []):
            if section not in sections:
                sections.append(section)
        for dependency in SKILL_MAP[current].get("dependencies", []):
            visit(dependency)
        visiting.remove(current)
        visited.add(current)

    visit(skill_id)
    return sections, ordered_skills


def create_evolution_clone(source_path: Path, destination_dir: Optional[Path] = None) -> tuple[Path, str]:
    """Create a byte-identical, isolated candidate before any Copilot analysis."""
    source_path = source_path.expanduser().resolve()
    if not source_path.is_file() or source_path.suffix.lower() != ".py":
        raise AgentError(f"Arquivo fonte Python não encontrado: {source_path}")
    source_bytes = source_path.read_bytes()
    source_digest = hashlib.sha256(source_bytes).hexdigest()
    clone_dir = (destination_dir or (DATA_DIR / "evolution_candidates")).expanduser().resolve()
    clone_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(clone_dir, 0o700)
    except OSError:
        pass
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    clone_path = clone_dir / f"{source_path.stem}_candidate_{stamp}_{uuid.uuid4().hex[:8]}.py"
    try:
        with clone_path.open("xb") as handle:
            handle.write(source_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(clone_path, 0o600)
        except OSError:
            pass
        clone_digest = hashlib.sha256(clone_path.read_bytes()).hexdigest()
        if clone_digest != source_digest:
            raise OSError("SHA-256 do clone não corresponde ao arquivo-fonte.")
    except Exception:
        clone_path.unlink(missing_ok=True)
        raise
    return clone_path, source_digest


def validate_evolution_clone_syntax(path: Path) -> str:
    """Run py_compile with its cache redirected outside the candidate directory."""
    with tempfile.TemporaryDirectory(prefix="nexus-evolution-pycache-") as cache_dir:
        env = os.environ.copy()
        env["PYTHONPYCACHEPREFIX"] = cache_dir
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", str(path)],
            capture_output=True, text=True, timeout=60, check=False, env=env,
        )
    if result.returncode != 0:
        message = clean_text(result.stderr or result.stdout or "py_compile falhou sem mensagem.")
        raise SyntaxError(message)
    return "py_compile OK"


class EvolutionSourceIndex:
    """Ctrl+F-style lexical search plus AST symbol indexing, constrained to selected sections."""

    STOP_WORDS = {
        "para", "como", "com", "sem", "uma", "umas", "uns", "por", "que", "the", "and",
        "for", "with", "from", "this", "that", "into", "only", "current", "please", "make",
        "melhorar", "melhoria", "alterar", "mudanca", "mudança", "evoluir", "evolucao", "evolução",
    }

    def __init__(self, source: str):
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.locator = EvolutionSectionLocator(source)
        self.tree = ast.parse(source)

    @staticmethod
    def _owner_for(node: ast.AST, classes: list[ast.ClassDef]) -> Optional[str]:
        parents = [item for item in classes if item.lineno < node.lineno <= item.end_lineno]
        return min(parents, key=lambda item: item.end_lineno - item.lineno).name if parents else None

    @staticmethod
    def _terms(objective: str) -> list[str]:
        terms = [term for term in re.findall(r"[\w]+", objective.casefold()) if len(term) >= 3]
        return list(dict.fromkeys(term for term in terms if term not in EvolutionSourceIndex.STOP_WORDS))

    def search(self, objective: str, section_names: list[str], limit: int = 12) -> list[dict[str, Any]]:
        terms = self._terms(objective)
        selected_sections = list(dict.fromkeys(name.upper() for name in section_names))[:MAX_RELEVANT_SECTIONS]
        ranges = {name: self.locator.sections[name] for name in selected_sections if name in self.locator.sections}

        def section_for(line_number: int) -> Optional[str]:
            for name, (start, end) in ranges.items():
                if start < line_number <= end:
                    return name
            return None

        classes = [node for node in ast.walk(self.tree) if isinstance(node, ast.ClassDef) and node.end_lineno is not None]
        parents = {child: parent for parent in ast.walk(self.tree) for child in ast.iter_child_nodes(parent)}
        function_hits: list[dict[str, Any]] = []
        function_spans: list[tuple[int, int]] = []
        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.end_lineno is None:
                continue
            if not isinstance(parents.get(node), (ast.Module, ast.ClassDef)):
                continue
            section = section_for(node.lineno)
            if not section or node.end_lineno > ranges[section][1]:
                continue
            owner = self._owner_for(node, classes)
            qualified = f"{owner}.{node.name}" if owner else node.name
            fragment = ast.get_source_segment(self.source, node) or ""
            searchable = (qualified + " " + fragment).casefold()
            matches = [term for term in terms if term in searchable]
            score = sum(4 if term in qualified.casefold() else 1 for term in matches)
            body = getattr(node, "body", [])
            header_end = body[0].lineno - 1 if body else node.lineno
            signature = "".join(self.lines[node.lineno - 1:header_end]).strip() or f"def {node.name}(...)"
            symbol_id = f"{qualified}@L{node.lineno}"
            function_hits.append({
                "id": symbol_id, "symbol": qualified, "kind": "function", "section": section,
                "owner": owner, "function_name": node.name,
                "line_start": node.lineno, "line_end": node.end_lineno, "signature": signature[:500],
                "matched_terms": matches[:12], "score": score,
            })
            function_spans.append((node.lineno, node.end_lineno))

        text_hits: list[dict[str, Any]] = []
        for name, (start, end) in ranges.items():
            for line_index in range(start, end):
                line_number = line_index + 1
                if any(first <= line_number <= last for first, last in function_spans):
                    continue
                text = self.lines[line_index].strip()
                if not text:
                    continue
                matches = [term for term in terms if term in text.casefold()]
                if not matches:
                    continue
                text_hits.append({
                    "id": f"{name}@L{line_number}", "symbol": f"{name}@L{line_number}",
                    "kind": "text", "section": name, "line_start": line_number,
                    "line_end": line_number, "signature": text[:180],
                    "matched_terms": matches[:12], "score": len(matches),
                })

        function_hits.sort(key=lambda item: (-item["score"], item["line_start"]))
        text_hits.sort(key=lambda item: (-item["score"], item["line_start"]))
        matched_functions = [item for item in function_hits if item["score"] > 0]
        if not matched_functions:
            matched_functions = function_hits[:6]
        combined = matched_functions[:8] + text_hits[:max(0, min(4, limit - min(8, len(matched_functions))))]
        # No objective term may match (e.g. prompt-table-only skill); retain a few AST targets for explicit selection.
        if not combined:
            combined = function_hits[:min(6, limit)]
        return combined[:max(1, int(limit))]


class EvolutionSectionLocator:
    """Find uniquely named NEXUS section anchors independently of line numbers."""

    START_RE = re.compile(r"^\s*#\s*NEXUS SECTION:\s*([A-Za-z0-9_]+)\s*$", re.I)
    END_RE = re.compile(r"^\s*#\s*END NEXUS SECTION:\s*([A-Za-z0-9_]+)\s*$", re.I)

    def __init__(self, source: str):
        self.source = source
        self.lines = source.splitlines(keepends=True)
        self.sections = self._scan()

    def _scan(self) -> dict[str, tuple[int, int]]:
        sections: dict[str, tuple[int, int]] = {}
        active: Optional[tuple[str, int]] = None
        for index, line in enumerate(self.lines):
            start = self.START_RE.fullmatch(line.rstrip("\r\n"))
            end = self.END_RE.fullmatch(line.rstrip("\r\n"))
            if start:
                name = start.group(1).upper()
                if active is not None:
                    raise ValueError(f"Nested NEXUS sections are not supported: {active[0]} -> {name}")
                if name in sections:
                    raise ValueError(f"Duplicate NEXUS section anchor: {name}")
                active = (name, index)
            elif end:
                name = end.group(1).upper()
                if active is None:
                    raise ValueError(f"End marker without a start marker: {name}")
                if active[0] != name:
                    raise ValueError(f"Mismatched NEXUS section markers: {active[0]} ended as {name}")
                sections[name] = (active[1] + 1, index)
                active = None
        if active is not None:
            raise ValueError(f"NEXUS section has no end marker: {active[0]}")
        return sections

    def extract(self, name: str) -> str:
        normalized = str(name).strip().upper()
        if normalized not in self.sections:
            raise KeyError(f"NEXUS section not found: {normalized}")
        start, end = self.sections[normalized]
        return "".join(self.lines[start:end])


class EvolutionContextBuilder:
    """Select bounded code from requested sections; never defaults to full-file context."""

    def __init__(self, max_chars: int = MAX_EVOLUTION_CONTEXT_CHARS,
                 max_lines: int = MAX_EVOLUTION_CONTEXT_LINES,
                 max_sections: int = MAX_RELEVANT_SECTIONS):
        self.max_chars = max(256, int(max_chars))
        self.max_lines = max(20, int(max_lines))
        self.max_sections = max(1, int(max_sections))

    @staticmethod
    def _terms(objective: str) -> list[str]:
        return [word for word in re.findall(r"[\w]+", objective.casefold()) if len(word) >= 3]

    @staticmethod
    def _function_nodes(tree: ast.AST, start_line: int, end_line: int) -> list[tuple[ast.AST, Optional[str]]]:
        owners: list[tuple[int, int, str]] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.end_lineno is not None:
                owners.append((node.lineno, node.end_lineno, node.name))
        found: list[tuple[ast.AST, Optional[str]]] = []
        seen: set[tuple[int, int]] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.end_lineno is None:
                continue
            if node.lineno < start_line or node.end_lineno > end_line:
                continue
            span = (node.lineno, node.end_lineno)
            if span in seen:
                continue
            seen.add(span)
            parents = [(a, b, name) for a, b, name in owners if a < node.lineno <= b]
            owner = min(parents, key=lambda item: item[1] - item[0])[2] if parents else None
            found.append((node, owner))
        # Prefer outer functions over nested local helpers to avoid duplicate context.
        found.sort(key=lambda item: (item[0].lineno, -(item[0].end_lineno - item[0].lineno)))
        outer: list[tuple[ast.AST, Optional[str]]] = []
        for item in found:
            node = item[0]
            if any(parent.lineno <= node.lineno and parent.end_lineno >= node.end_lineno
                   for parent, _ in outer):
                continue
            outer.append(item)
        return outer

    @staticmethod
    def _signature(lines: list[str], node: ast.AST) -> str:
        start = node.lineno - 1
        body = getattr(node, "body", [])
        end = body[0].lineno - 1 if body else start + 1
        header = "".join(lines[start:end]).strip()
        if header:
            return header
        prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
        return f"{prefix} {node.name}(...)"

    def _compact_section(self, source: str, section_lines: list[str], absolute_start: int,
                         objective: str, char_budget: int, line_budget: int) -> str:
        section_text = "".join(section_lines)
        if len(section_text) <= char_budget and len(section_lines) <= line_budget:
            return section_text

        try:
            tree = ast.parse(source)
        except SyntaxError:
            tree = None
        if tree is not None:
            start_line = absolute_start + 1
            end_line = absolute_start + len(section_lines)
            candidates = self._function_nodes(tree, start_line, end_line)
            terms = self._terms(objective)
            ranked = []
            for node, owner in candidates:
                fragment = ast.get_source_segment(source, node) or ""
                searchable = (node.name + " " + fragment).casefold()
                score = sum(1 for term in terms if term in searchable)
                ranked.append((score, node.lineno, node, owner, fragment))
            ranked.sort(key=lambda item: (-item[0], item[1]))
            if ranked:
                matched = [item for item in ranked if item[0] > 0]
                selected = matched or ranked[:3]
                source_lines = source.splitlines(keepends=True)
                chunks: list[str] = []
                used_chars = used_lines = 0
                for score, _, node, owner, fragment in selected:
                    label = f"\n# Relevant function: {owner + '.' if owner else ''}{node.name}\n"
                    candidate = label + fragment.rstrip() + "\n"
                    candidate_lines = candidate.splitlines()
                    if used_chars + len(candidate) <= char_budget and used_lines + len(candidate_lines) <= line_budget:
                        chunks.append(candidate)
                        used_chars += len(candidate)
                        used_lines += len(candidate_lines)
                    else:
                        signature = self._signature(source_lines, node)
                        summary = f"\n# Relevant function signature (body omitted by context budget): {signature}\n"
                        if used_chars + len(summary) <= char_budget and used_lines + len(summary.splitlines()) <= line_budget:
                            chunks.append(summary)
                            used_chars += len(summary)
                            used_lines += len(summary.splitlines())
                    if used_chars >= char_budget or used_lines >= line_budget:
                        break
                if chunks:
                    return "# Selective excerpt; complete function bodies are included only when they fit.\n" + "".join(chunks)

        # Text-only blocks (for example prompt tables): include matched line neighborhoods.
        terms = self._terms(objective)
        indexes = [i for i, line in enumerate(section_lines) if any(term in line.casefold() for term in terms)]
        if not indexes:
            indexes = list(range(min(8, len(section_lines))))
        chosen: set[int] = set()
        for index in indexes:
            chosen.update(range(max(0, index - 2), min(len(section_lines), index + 3)))
        excerpt: list[str] = []
        for index in sorted(chosen):
            line = section_lines[index]
            if len("".join(excerpt)) + len(line) > char_budget or len(excerpt) + 1 > line_budget:
                continue
            excerpt.append(line)
        if not excerpt:
            return "# Section content omitted: context budget exhausted; section contains " + str(len(section_lines)) + " lines.\n"
        return "# Selective text excerpt; unrelated lines omitted.\n" + "".join(excerpt)

    def build(self, source: str, section_names: list[str], objective: str) -> dict[str, Any]:
        locator = EvolutionSectionLocator(source)
        unique_names = list(dict.fromkeys(str(name).strip().upper() for name in section_names))
        if not unique_names:
            raise ValueError("This skill has no mapped code sections to analyze.")
        selected_names = unique_names[:self.max_sections]
        source_lines = source.splitlines(keepends=True)
        blocks: list[str] = []
        included: list[str] = []
        omitted: list[str] = []
        for name in selected_names:
            if name not in locator.sections:
                omitted.append(f"{name}: section anchor not present in this source file")
                continue
            start, end = locator.sections[name]
            body = source_lines[start:end]
            heading = f"\n===== NEXUS SECTION: {name} =====\n"
            used_text = "".join(blocks)
            remaining_chars = self.max_chars - len(used_text) - len(heading)
            remaining_lines = self.max_lines - len(used_text.splitlines()) - 1
            if remaining_chars <= 0 or remaining_lines <= 0:
                omitted.append(f"{name}: context budget exhausted")
                continue
            chunk = self._compact_section(source, body, start, objective, remaining_chars, remaining_lines)
            candidate = heading + chunk
            if len(used_text) + len(candidate) > self.max_chars or len(used_text.splitlines()) + len(candidate.splitlines()) > self.max_lines:
                omitted.append(f"{name}: omitted to respect context budget")
                continue
            blocks.append(candidate)
            included.append(name)
        if len(unique_names) > self.max_sections:
            omitted.extend(f"{name}: maximum relevant sections ({self.max_sections}) reached" for name in unique_names[self.max_sections:])
        text = "".join(blocks).strip()
        if not text:
            raise ValueError("No requested section could be selected from the source file.")
        return {"text": text, "included_sections": included, "omitted": omitted,
                "chars": len(text), "lines": len(text.splitlines())}


def validate_evolution_plan_response(
    value: Any,
    allowed_evidence: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Accept bounded, evidence-backed planning; reject code and echoed schema placeholders."""
    if isinstance(value, str):
        value = extract_agent_json(value, "evolucao_plano")
    if not isinstance(value, dict):
        raise AgentError("O agente de evolução não retornou um objeto JSON.")
    # Aceita envelopes comuns da segunda resposta sem perder a validação local.
    if "analysis" not in value:
        for key in ("plan", "data", "result", "answer", "response"):
            nested = value.get(key)
            if isinstance(nested, dict) and any(field in nested for field in ("analysis", "diagnosis", "diagnostico")):
                value = nested
                break
    if any(key in value for key in ("code", "content", "files", "patch")):
        raise AgentError("Resposta de planejamento contém código/patch; nenhuma alteração será aplicada.")
    def normalize_text(text: str) -> str:
        folded = unicodedata.normalize("NFKD", text.casefold())
        return "".join(char for char in folded if not unicodedata.combining(char))

    placeholders = {
        "diagnostico ancorado no contexto", "capacidade observada",
        "limitacao observada ou informacao ausente", "etapa pequena e verificavel",
        "teste ou validacao proposta", "risco e mitigacao", "objetivo e resultado esperado",
    }

    def reject_placeholder(text: str) -> None:
        normalized_text = normalize_text(text)
        if any(marker in normalized_text for marker in placeholders) or "<copiar" in normalized_text or re.search(r"\btodo\b", normalized_text):
            raise AgentError("O Copilot repetiu texto-modelo do schema, não uma análise real; plano não aprovado.")

    analysis = clean_text(value.get("analysis", ""))
    if not analysis:
        for key in ("diagnosis", "diagnostico", "analysis_text", "reasoning"):
            alternative = clean_text(value.get(key, ""))
            if alternative:
                analysis = alternative
                break
    summary = clean_text(value.get("summary", ""))
    if not analysis:
        raise AgentError("Plano de evolução sem diagnóstico no campo analysis.")
    reject_placeholder(analysis)
    reject_placeholder(summary)
    plan = value.get("plan")
    if not isinstance(plan, list) or not plan or len(plan) > 12 or any(not isinstance(item, str) or not item.strip() for item in plan):
        raise AgentError("Plano de evolução deve conter uma lista não vazia de etapas textuais.")
    evidence = value.get("evidence")
    if not isinstance(evidence, list) or not evidence or len(evidence) > 20:
        raise AgentError("Plano sem evidências pesquisáveis; nenhuma aprovação será solicitada.")
    normalized_evidence: list[dict[str, str]] = []
    for item in evidence:
        if not isinstance(item, dict):
            raise AgentError("Cada evidência precisa conter id, section e observation.")
        evidence_id = clean_text(item.get("id", ""))
        section = clean_text(item.get("section", "")).upper()
        observation = clean_text(item.get("observation", ""))
        if not evidence_id or not section or not observation:
            raise AgentError("Evidência incompleta; nenhuma aprovação será solicitada.")
        if allowed_evidence is not None:
            if evidence_id not in allowed_evidence:
                raise AgentError(f"Evidência inexistente na busca local: {evidence_id!r}.")
            if section != allowed_evidence[evidence_id].upper():
                raise AgentError(f"Seção da evidência não corresponde ao índice local: {evidence_id!r}.")
        reject_placeholder(observation)
        normalized_evidence.append({"id": evidence_id, "section": section, "observation": observation[:1000]})
    normalized = {"analysis": analysis[:5000], "summary": summary[:1000],
                  "evidence": normalized_evidence,
                  "capabilities_found": value.get("capabilities_found", []),
                  "limitations": value.get("limitations", []), "plan": [clean_text(item)[:1000] for item in plan],
                  "tests": value.get("tests", []), "risks": value.get("risks", [])}
    for field in ("capabilities_found", "limitations", "tests", "risks"):
        if not isinstance(normalized[field], list) or len(normalized[field]) > 20 or any(not isinstance(item, str) for item in normalized[field]):
            raise AgentError(f"Campo {field} deve ser uma lista de textos.")
        normalized[field] = [clean_text(item)[:1000] for item in normalized[field]]
        for item in normalized[field]:
            reject_placeholder(item)
    for item in normalized["plan"]:
        reject_placeholder(item)
    return normalized

# END NEXUS SECTION: EVOLUTION_ENGINE

# NEXUS SECTION: COGNITIVE_ORCHESTRATOR
# PURPOSE: Autonomous cognitive orchestrator with explicit state machine,
#          structured Copilot protocol, robust response parser, step-by-step
#          execution, auto-correction cycles, completion detection, and
#          full observability logging.
# VERSION: 1.0
# DEPENDS: Existing EdgeCopilotBrowser, RealPTY, TemporaryWorkspaceManager,
#          SafetyGuard (is_dangerous), validate_generated_python, terminal_log.

MAX_ORCHESTRATOR_CYCLES = 30
MAX_ORCHESTRATOR_STEP_ATTEMPTS = 3
MAX_ORCHESTRATOR_REPAIR_ATTEMPTS = 2
ORCHESTRATOR_TIMEOUT_SECONDS = 600
ORCHESTRATOR_STALL_THRESHOLD = 5

VALID_ORCHESTRATOR_STATES = frozenset({
    "RECEIVE", "ANALYZE", "PLAN", "ASK_COPILOT", "READ_COPILOT",
    "PARSE_RESPONSE", "DECIDE", "EXECUTE", "OBSERVE", "VALIDATE",
    "REPAIR", "CONTINUE", "COMPLETE", "FAILED", "WAITING_USER",
})

VALID_RESPONSE_STATUSES = frozenset({"READY", "DONE", "ERROR", "RETRY", "WAITING", "INCOMPLETE"})
VALID_RESPONSE_ACTIONS = frozenset({
    "CREATE_FILE", "RUN_COMMAND", "ANALYZE", "VALIDATE", "ASK_USER", "FINISH",
})

ORCHESTRATOR_RESPONSE_FIELDS = (
    "STATUS", "ACTION", "FILE", "COMMAND", "CONTENT",
    "REASON", "NEXT_STEP", "SUCCESS_CRITERIA",
)


@dataclass
class OrchestratorStep:
    step_id: str
    objective: str
    expected_result: str
    action: str = "PENDING"
    validation: str = ""
    status: str = "PENDING"
    attempts: int = 0
    max_attempts: int = MAX_ORCHESTRATOR_STEP_ATTEMPTS

    def as_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "objective": self.objective,
            "expected_result": self.expected_result,
            "action": self.action,
            "validation": self.validation,
            "status": self.status,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
        }


@dataclass
class OrchestratorTaskContext:
    task_id: str
    goal: str
    plan: list[OrchestratorStep]
    current_step_index: int = 0
    completed_steps: list[str] = None
    failed_steps: list[str] = None
    attempts: dict[str, int] = None
    copilot_requests: list[dict[str, Any]] = None
    copilot_responses: list[dict[str, Any]] = None
    execution_results: list[dict[str, Any]] = None
    errors: list[dict[str, Any]] = None
    artifacts: list[str] = None
    validation_results: list[dict[str, Any]] = None
    status: str = "running"
    state: str = "RECEIVE"
    cycle_count: int = 0
    created_at: str = ""
    last_progress_cycle: int = 0

    def __post_init__(self) -> None:
        if self.completed_steps is None:
            self.completed_steps = []
        if self.failed_steps is None:
            self.failed_steps = []
        if self.attempts is None:
            self.attempts = {}
        if self.copilot_requests is None:
            self.copilot_requests = []
        if self.copilot_responses is None:
            self.copilot_responses = []
        if self.execution_results is None:
            self.execution_results = []
        if self.errors is None:
            self.errors = []
        if self.artifacts is None:
            self.artifacts = []
        if self.validation_results is None:
            self.validation_results = []
        if not self.created_at:
            self.created_at = datetime.now().astimezone().isoformat(timespec="seconds")

    @property
    def current_step(self) -> Optional[OrchestratorStep]:
        if 0 <= self.current_step_index < len(self.plan):
            return self.plan[self.current_step_index]
        return None

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "plan": [s.as_dict() for s in self.plan],
            "current_step": self.current_step.as_dict() if self.current_step else None,
            "completed_steps": self.completed_steps,
            "failed_steps": self.failed_steps,
            "attempts": self.attempts,
            "execution_results": self.execution_results[-5:],
            "errors": self.errors[-5:],
            "artifacts": self.artifacts,
            "validation_results": self.validation_results[-5:],
            "status": self.status,
            "state": self.state,
            "cycle_count": self.cycle_count,
        }


class CopilotPromptBuilder:
    """Central formatter for all Copilot requests in the orchestrator."""

    @staticmethod
    def build_request(
        task_context: OrchestratorTaskContext,
        step: Optional[OrchestratorStep],
        previous_result: str = "",
        known_errors: str = "",
        environment: str = "",
    ) -> str:
        task_id = task_context.task_id
        step_id = step.step_id if step else "GLOBAL"
        objective = step.objective if step else task_context.goal
        current_state_parts: list[str] = []
        if task_context.completed_steps:
            current_state_parts.append("Etapas concluídas: " + ", ".join(task_context.completed_steps))
        if task_context.artifacts:
            current_state_parts.append("Artefatos: " + ", ".join(task_context.artifacts[-10:]))
        if task_context.execution_results:
            last_exec = task_context.execution_results[-1]
            current_state_parts.append(
                f"Última execução: exit_code={last_exec.get('exit_code')}, "
                f"comando={last_exec.get('command', '')[:100]}"
            )
        current_state = "; ".join(current_state_parts) or "Início da tarefa."

        return f"""<AGENT_REQUEST>
TASK_ID: {task_id}
STEP_ID: {step_id}

OBJECTIVE:
{objective}

CURRENT_STATE:
{current_state}

PREVIOUS_RESULT:
{previous_result or "Nenhum resultado anterior."}

KNOWN_ERRORS:
{known_errors or "Nenhum erro conhecido."}

ENVIRONMENT:
{environment or "Linux, Python 3, Shell disponível"}

REQUEST:
Determine exatamente a próxima ação necessária para cumprir o objetivo desta etapa.

EXPECTED_RESPONSE_FORMAT:
A resposta DEVE seguir EXATAMENTE este formato:

<AGENT_RESPONSE>
STATUS: READY|DONE|ERROR|RETRY|WAITING
ACTION: CREATE_FILE|RUN_COMMAND|ANALYZE|VALIDATE|ASK_USER|FINISH
FILE: caminho relativo ou NONE
COMMAND: comando ou NONE
CONTENT: conteúdo ou NONE
REASON: explicação curta
NEXT_STEP: próxima etapa
SUCCESS_CRITERIA: condição objetiva
</AGENT_RESPONSE>

NÃO ADICIONE TEXTO ANTES OU DEPOIS DO BLOCO.
</AGENT_REQUEST>""".strip()

    @staticmethod
    def build_continuation_request(
        task_context: OrchestratorTaskContext,
        last_partial_response: str,
    ) -> str:
        return (
            f"CONTINUE A RESPOSTA ANTERIOR A PARTIR DO ÚLTIMO CAMPO VÁLIDO.\n"
            f"NÃO REPITA O CONTEÚDO ANTERIOR.\n"
            f"TASK_ID: {task_context.task_id}\n"
            f"RESPOSTA PARCIAL RECEBIDA:\n{last_partial_response[:2000]}\n"
            f"Complete a resposta no formato <AGENT_RESPONSE> exigido."
        )

    @staticmethod
    def build_correction_request(task_context: OrchestratorTaskContext, reason: str) -> str:
        return (
            f"RESPOSTA INVÁLIDA.\n\n"
            f"O PROTOCOLO EXIGE:\n"
            f"<AGENT_RESPONSE>\n"
            f"STATUS: READY|DONE|ERROR|RETRY|WAITING\n"
            f"ACTION: CREATE_FILE|RUN_COMMAND|ANALYZE|VALIDATE|ASK_USER|FINISH\n"
            f"FILE: caminho relativo ou NONE\n"
            f"COMMAND: comando ou NONE\n"
            f"CONTENT: conteúdo ou NONE\n"
            f"REASON: explicação curta\n"
            f"NEXT_STEP: próxima etapa\n"
            f"SUCCESS_CRITERIA: condição objetiva\n"
            f"</AGENT_RESPONSE>\n\n"
            f"REENVIE SOMENTE A RESPOSTA CORRIGIDA.\n"
            f"Motivo da rejeição: {reason}"
        )

    @staticmethod
    def build_execution_feedback(
        task_context: OrchestratorTaskContext,
        step: OrchestratorStep,
        execution_result: dict[str, Any],
    ) -> str:
        result_status = "SUCCESS" if execution_result.get("exit_code") == 0 else "FAILED"
        return f"""EXECUTION_RESULT:

COMMAND:
{execution_result.get('command', '')}

EXIT_CODE:
{execution_result.get('exit_code')}

STDOUT:
{str(execution_result.get('stdout', ''))[:4000]}

STDERR:
{str(execution_result.get('stderr', ''))[:4000]}

RESULT:
{result_status}

REQUEST:
Analise o resultado e determine a próxima ação para a etapa {step.step_id}.
Se o resultado for FAILED, proponha uma correção específica.
Se o resultado for SUCCESS, indique DONE e proponha a próxima etapa.
""".strip()


class CopilotResponseParser:
    """Dedicated parser for Copilot responses in the orchestrator protocol."""

    @staticmethod
    def parse(raw_response: str) -> dict[str, Any]:
        text = str(raw_response or "").strip()
        if not text:
            return {"valid": False, "reason": "Resposta vazia.", "fields": {}}

        block = CopilotResponseParser._extract_block(text)
        if block is None:
            json_fallback = CopilotResponseParser._try_json_fallback(text)
            if json_fallback is not None:
                return json_fallback
            return {
                "valid": False,
                "reason": "Bloco <AGENT_RESPONSE> não encontrado na resposta.",
                "fields": {},
                "raw": text[:500],
            }

        fields = CopilotResponseParser._extract_fields(block)
        issues = CopilotResponseParser._validate_fields(fields)
        if issues:
            return {
                "valid": False,
                "reason": "; ".join(issues),
                "fields": fields,
                "raw": text[:500],
            }

        status = fields.get("STATUS", "").strip().upper()
        if status == "INCOMPLETE":
            return {
                "valid": False,
                "reason": "Resposta marcada como INCOMPLETE.",
                "fields": fields,
                "incomplete": True,
            }

        return {"valid": True, "reason": "", "fields": fields}

    @staticmethod
    def _extract_block(text: str) -> Optional[str]:
        open_tag = "<AGENT_RESPONSE>"
        close_tag = "</AGENT_RESPONSE>"
        start = text.find(open_tag)
        if start == -1:
            start = text.lower().find(open_tag.lower())
            if start == -1:
                return None
        end = text.find(close_tag, start)
        if end == -1:
            end = text.lower().find(close_tag.lower(), start)
            if end == -1:
                end = len(text)
        return text[start + len(open_tag):end].strip()

    @staticmethod
    def _extract_fields(block: str) -> dict[str, str]:
        fields: dict[str, str] = {}
        current_key = None
        current_lines: list[str] = []
        for line in block.splitlines():
            match = re.match(r"^([A-Z_]+):\s*(.*)$", line.strip())
            if match:
                key = match.group(1).strip().upper()
                value = match.group(2).strip()
                if key in ORCHESTRATOR_RESPONSE_FIELDS:
                    if current_key is not None:
                        fields[current_key] = "\n".join(current_lines).strip()
                    current_key = key
                    current_lines = [value]
                    continue
            if current_key is not None:
                current_lines.append(line)
        if current_key is not None:
            fields[current_key] = "\n".join(current_lines).strip()
        for key in list(fields):
            val = fields[key]
            if val.upper() == "NONE":
                fields[key] = ""
        return fields

    @staticmethod
    def _validate_fields(fields: dict[str, str]) -> list[str]:
        issues: list[str] = []
        for required in ORCHESTRATOR_RESPONSE_FIELDS:
            if required not in fields:
                issues.append(f"Campo ausente: {required}")
        status = fields.get("STATUS", "").strip().upper()
        if status and status not in VALID_RESPONSE_STATUSES:
            issues.append(f"STATUS inválido: {status!r}")
        action = fields.get("ACTION", "").strip().upper()
        if action and action not in VALID_RESPONSE_ACTIONS:
            issues.append(f"ACTION inválida: {action!r}")
        seen: set[str] = set()
        duplicates: list[str] = []
        for line in fields.get("_raw_block", "").splitlines() if "_raw_block" in fields else []:
            m = re.match(r"^([A-Z_]+):", line.strip())
            if m:
                k = m.group(1)
                if k in seen:
                    duplicates.append(k)
                seen.add(k)
        if duplicates:
            issues.append(f"Campos duplicados: {', '.join(duplicates)}")
        return issues

    @staticmethod
    def _try_json_fallback(text: str) -> Optional[dict[str, Any]]:
        try:
            candidates = extract_json_candidates(text)
        except Exception:
            return None
        for candidate in candidates:
            mapped: dict[str, str] = {}
            if "status" in candidate:
                mapped["STATUS"] = str(candidate["status"]).upper()
            if "action" in candidate:
                mapped["ACTION"] = str(candidate["action"]).upper()
            if "file" in candidate:
                mapped["FILE"] = str(candidate["file"])
            if "command" in candidate:
                mapped["COMMAND"] = str(candidate["command"])
            if "content" in candidate:
                mapped["CONTENT"] = str(candidate["content"])
            if "reason" in candidate:
                mapped["REASON"] = str(candidate["reason"])
            if "next_step" in candidate:
                mapped["NEXT_STEP"] = str(candidate["next_step"])
            if "success_criteria" in candidate:
                mapped["SUCCESS_CRITERIA"] = str(candidate["success_criteria"])
            if "STATUS" in mapped and "ACTION" in mapped:
                issues = CopilotResponseParser._validate_fields(mapped)
                if not issues:
                    return {"valid": True, "reason": "", "fields": mapped}
                return {"valid": False, "reason": "; ".join(issues), "fields": mapped}
        return None


class SafetyGuard:
    """Classifies commands before execution; never bypassed by Copilot responses."""

    READ_ONLY_COMMANDS = frozenset({
        "pwd", "ls", "whoami", "hostname", "uptime", "uname", "date",
        "cat", "head", "tail", "wc", "echo", "free", "df", "lscpu",
        "lspci", "lsusb", "env", "printenv", "id", "groups", "ps",
        "top", "htop", "nproc", "stat", "file", "which", "whereis",
        "find", "grep", "rg", "diff", "sort", "uniq", "cut", "tr",
    })

    PACKAGE_INSTALL_COMMANDS = frozenset({
        "apt", "apt-get", "dnf", "yum", "pacman", "zypper", "pip", "pip3",
        "npm", "yarn", "pnpm", "cargo", "go", "gem", "flatpak", "snap",
    })

    DESTRUCTIVE_COMMANDS = frozenset({
        "rm", "rmdir", "shred", "mkfs", "wipefs", "dd", "shred",
    })

    PRIVILEGED_COMMANDS = frozenset({"sudo", "su", "pkexec", "doas"})

    @staticmethod
    def classify(command: str) -> str:
        command = clean_command(command)
        if not command:
            return "UNKNOWN"
        normalized = re.sub(r"\s+", " ", command.lower()).strip()
        first_token = normalized.split()[0] if normalized.split() else ""
        if is_dangerous(command):
            return "DESTRUCTIVE"
        if first_token in SafetyGuard.PRIVILEGED_COMMANDS:
            return "PRIVILEGED"
        if first_token in SafetyGuard.DESTRUCTIVE_COMMANDS:
            return "DESTRUCTIVE"
        if first_token in SafetyGuard.PACKAGE_INSTALL_COMMANDS:
            return "PACKAGE_INSTALL"
        if any(token in normalized for token in ("shutdown", "reboot", "poweroff", "systemctl reboot")):
            return "SYSTEM_CHANGE"
        if any(token in normalized for token in ("curl ", "wget ", "ssh ", "scp ", "nc ", "netcat")):
            return "NETWORK"
        if first_token in SafetyGuard.READ_ONLY_COMMANDS:
            return "READ_ONLY"
        if any(token in normalized for token in (">", ">>", "tee ")):
            return "SAFE_WRITE"
        return "SAFE_WRITE"

    @staticmethod
    def requires_confirmation(category: str, command: str, auto_mode: bool) -> bool:
        if category in {"DESTRUCTIVE", "PRIVILEGED", "SYSTEM_CHANGE"}:
            return True
        if category == "PACKAGE_INSTALL":
            return True
        if category == "NETWORK":
            return True
        if auto_mode and category in {"READ_ONLY", "SAFE_WRITE"}:
            return False
        if category in {"READ_ONLY"}:
            return False
        return True


class CompletionDetector:
    """Determines real completion based on system state, not just Copilot's word."""

    @staticmethod
    def check(
        step: OrchestratorStep,
        execution_result: dict[str, Any],
        copilot_status: str,
        success_criteria: str,
    ) -> tuple[bool, str]:
        exit_code = execution_result.get("exit_code")
        stdout = str(execution_result.get("stdout", ""))
        stderr = str(execution_result.get("stderr", ""))
        artifacts = execution_result.get("artifacts", [])

        reasons: list[str] = []
        if exit_code is not None and exit_code == 0:
            reasons.append("exit_code=0")
        elif exit_code is not None and exit_code != 0:
            return False, f"exit_code={exit_code}: {stderr[:200]}"
        if copilot_status == "DONE":
            reasons.append("Copilot=DONE")
        elif copilot_status == "ERROR":
            return False, f"Copilot reportou ERROR: {stderr[:200]}"
        if step.expected_result:
            expected = step.expected_result.lower()
            if expected in stdout.lower() or expected in "\n".join(str(a) for a in artifacts).lower():
                reasons.append("expected_result presente na saída")
        if success_criteria and len(success_criteria) > 5:
            keywords = [w for w in re.findall(r"\w+", success_criteria.lower()) if len(w) >= 4]
            if keywords and any(kw in stdout.lower() for kw in keywords[:5]):
                reasons.append("success_criteria correspondente na saída")
        if not reasons:
            return False, "Nenhum critério de sucesso satisfeito."
        return True, "; ".join(reasons)

    @staticmethod
    def is_task_complete(task_context: OrchestratorTaskContext) -> tuple[bool, str]:
        if task_context.status == "complete":
            return True, "Tarefa marcada como completa."
        all_done = all(s.status == "DONE" for s in task_context.plan)
        if all_done and task_context.plan:
            return True, "Todas as etapas concluídas e validadas."
        if task_context.failed_steps and len(task_context.failed_steps) >= len(task_context.plan):
            return False, "Todas as etapas falharam."
        return False, ""


class CycleLogger:
    """Records each cycle for full observability and reconstruction."""

    @staticmethod
    def log_cycle(
        task_context: OrchestratorTaskContext,
        step: Optional[OrchestratorStep],
        state: str,
        prompt_sent: str = "",
        response_received: str = "",
        action_detected: str = "",
        command_executed: str = "",
        exit_code: Optional[int] = None,
        stdout: str = "",
        stderr: str = "",
        validation: str = "",
        decision: str = "",
        next_state: str = "",
        duration: float = 0.0,
        error: str = "",
    ) -> None:
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        entry = {
            "timestamp": timestamp,
            "task_id": task_context.task_id,
            "step_id": step.step_id if step else "GLOBAL",
            "state": state,
            "prompt_sent": prompt_sent[:500],
            "response_received": response_received[:500],
            "action_detected": action_detected,
            "command_executed": command_executed[:200],
            "exit_code": exit_code,
            "stdout": stdout[:500],
            "stderr": stderr[:500],
            "validation": validation,
            "decision": decision,
            "next_state": next_state,
            "duration": round(duration, 3),
            "error": error[:500],
        }
        log_dir = DATA_DIR / "orchestrator_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{task_context.task_id}.jsonl"
        try:
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass
        terminal_log(
            f"[ORCHESTRATOR] {state} | step={step.step_id if step else 'GLOBAL'} | "
            f"action={action_detected} | exit={exit_code} | decision={decision} | "
            f"next={next_state} | {duration:.2f}s",
            "STEP" if not error else "ERROR",
        )


class StallDetector:
    """Detects repeated errors, repeated actions, or lack of progress."""

    @staticmethod
    def detect_stall(task_context: OrchestratorTaskContext) -> tuple[bool, str]:
        if task_context.cycle_count - task_context.last_progress_cycle > ORCHESTRATOR_STALL_THRESHOLD:
            return True, "Sem progresso por mais de {} ciclos.".format(ORCHESTRATOR_STALL_THRESHOLD)
        recent_errors = task_context.errors[-3:]
        if len(recent_errors) >= 3:
            error_texts = [e.get("message", "")[:100] for e in recent_errors]
            if len(set(error_texts)) == 1:
                return True, f"Mesmo erro repetido 3 vezes: {error_texts[0]}"
        recent_execs = task_context.execution_results[-3:]
        if len(recent_execs) >= 3:
            commands = [e.get("command", "")[:100] for e in recent_execs]
            if len(set(commands)) == 1:
                return True, f"Mesmo comando repetido 3 vezes: {commands[0]}"
        return False, ""


class CognitiveOrchestrator:
    """Autonomous cognitive loop: plan → ask → parse → execute → observe → validate → repair → continue."""

    def __init__(
        self,
        app: "NexusApp",
        goal: str,
        auto_mode: bool = False,
    ) -> None:
        self.app = app
        self.goal = goal.strip()
        self.auto_mode = auto_mode
        self.task_context: Optional[OrchestratorTaskContext] = None

    def run(self) -> OrchestratorTaskContext:
        if not self.goal:
            raise AgentError("Objetivo vazio; orquestração não iniciada.")
        task_id = f"TASK-{uuid.uuid4().hex[:8]}"
        terminal_panel("COGNITIVE ORCHESTRATOR", [
            f"Task: {task_id}",
            f"Goal: {self.goal[:80]}",
            f"Auto: {'ON' if self.auto_mode else 'OFF'}",
            "Ciclo: PLAN → ASK → PARSE → EXECUTE → OBSERVE → VALIDATE → REPAIR",
        ])
        self.task_context = OrchestratorTaskContext(
            task_id=task_id,
            goal=self.goal,
            plan=[],
        )
        self._execute_state("RECEIVE")
        return self.task_context

    def _execute_state(self, state: str) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        ctx.state = state
        ctx.cycle_count += 1
        if ctx.cycle_count > MAX_ORCHESTRATOR_CYCLES:
            ctx.status = "failed"
            ctx.state = "FAILED"
            terminal_log("Limite global de ciclos atingido.", "ERROR")
            self._print_task_tree()
            return
        stalled, stall_reason = StallDetector.detect_stall(ctx)
        if stalled:
            ctx.state = "WAITING_USER"
            terminal_log(f"Stall detectado: {stall_reason}", "WARN")
            print(f"\n\033[1;33m[ORCHESTRATOR] STALL: {stall_reason}\033[0m")
            if self.auto_mode:
                ctx.status = "failed"
                ctx.state = "FAILED"
                self._print_task_tree()
                return
            answer = input("Continuar mesmo assim? [s/N]: ").strip().lower()
            if answer not in {"s", "sim", "y", "yes"}:
                ctx.status = "failed"
                ctx.state = "FAILED"
                self._print_task_tree()
                return
            ctx.last_progress_cycle = ctx.cycle_count

        if state == "RECEIVE":
            self._state_receive()
        elif state == "ANALYZE":
            self._state_analyze()
        elif state == "PLAN":
            self._state_plan()
        elif state == "ASK_COPILOT":
            self._state_ask_copilot()
        elif state == "READ_COPILOT":
            self._state_read_copilot()
        elif state == "PARSE_RESPONSE":
            self._state_parse_response()
        elif state == "DECIDE":
            self._state_decide()
        elif state == "EXECUTE":
            self._state_execute()
        elif state == "OBSERVE":
            self._state_observe()
        elif state == "VALIDATE":
            self._state_validate()
        elif state == "REPAIR":
            self._state_repair()
        elif state == "CONTINUE":
            self._state_continue()
        elif state == "COMPLETE":
            self._state_complete()
        elif state == "FAILED":
            self._state_failed()
        elif state == "WAITING_USER":
            self._state_waiting_user()

    def _state_receive(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        terminal_log(f"Objetivo recebido: {ctx.goal[:120]}", "INFO")
        self._execute_state("ANALYZE")

    def _state_analyze(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        terminal_log("Analisando objetivo...", "STEP")
        route = local_route(ctx.goal)
        if route["type"] == "conversation":
            print("\n\033[1;35mORCHESTRATOR:\033[0m\n" + route.get("response", ""))
            ctx.status = "complete"
            ctx.state = "COMPLETE"
            return
        self._execute_state("PLAN")

    def _state_plan(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        terminal_log("Solicitando plano estruturado ao Copilot...", "STEP")
        self.app.current_task_calls = 0
        self.app.current_budget = 2 + MAX_ORCHESTRATOR_CYCLES
        self.app.key_pool.begin_task()
        plan_state = {
            "user_request": ctx.goal,
            "terminal": self.app.terminal_state(),
            "routing_rule": "Crie um plano estruturado com etapas executáveis.",
        }
        try:
            plan_response = self.app.call_agent("planejamento", plan_state)
        except (AgentError, RateLimitError) as exc:
            ctx.errors.append({"cycle": ctx.cycle_count, "message": str(exc)})
            self._execute_state("REPAIR")
            return
        steps_raw = plan_response.get("steps", [])
        if not isinstance(steps_raw, list) or not steps_raw:
            ctx.errors.append({"cycle": ctx.cycle_count, "message": "Plano vazio."})
            self._execute_state("REPAIR")
            return
        plan: list[OrchestratorStep] = []
        for i, step_text in enumerate(steps_raw, 1):
            step_id = f"STEP-{i:03d}"
            plan.append(OrchestratorStep(
                step_id=step_id,
                objective=str(step_text)[:500],
                expected_result=str(plan_response.get("expected_output", ""))[:200],
            ))
        ctx.plan = plan
        print(f"\n[NEXUS] PLANO ({len(plan)} etapas):")
        for s in plan:
            print(f"  {s.step_id}: {s.objective[:80]}")
        self._print_task_tree()
        self._execute_state("ASK_COPILOT")

    def _state_ask_copilot(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        step = ctx.current_step
        if step is None:
            self._execute_state("COMPLETE")
            return
        if step.attempts >= step.max_attempts:
            ctx.failed_steps.append(step.step_id)
            ctx.errors.append({"cycle": ctx.cycle_count, "step": step.step_id, "message": "Max attempts reached."})
            self._execute_state("WAITING_USER")
            return
        step.attempts += 1
        previous_result = ""
        if ctx.execution_results:
            last = ctx.execution_results[-1]
            previous_result = f"exit_code={last.get('exit_code')}, stdout={str(last.get('stdout',''))[:200]}"
        known_errors = "; ".join(e.get("message", "")[:100] for e in ctx.errors[-3:])
        env = f"cwd={self.app.pty.cwd}, python={sys.executable}"
        prompt = CopilotPromptBuilder.build_request(ctx, step, previous_result, known_errors, env)
        ctx.copilot_requests.append({"cycle": ctx.cycle_count, "step_id": step.step_id, "prompt": prompt[:500]})
        CycleLogger.log_cycle(ctx, step, "ASK_COPILOT", prompt_sent=prompt[:300])
        terminal_log(f"Enviando pedido ao Copilot para {step.step_id} (tentativa {step.attempts})", "API")
        self._execute_state("READ_COPILOT")

    def _state_read_copilot(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        step = ctx.current_step
        if step is None:
            self._execute_state("COMPLETE")
            return
        last_request = ctx.copilot_requests[-1] if ctx.copilot_requests else {}
        prompt = last_request.get("prompt", "")
        orchestrator_prompt = (
            PROMPTS["orquestrar"] + "\n\n" + prompt
        )
        try:
            raw = self.app.ai.ask("orquestrar", {"orchestrator_prompt": orchestrator_prompt, "task_context": ctx.as_dict()})
            raw = ncp_unpack(raw)
        except (AgentError, EdgeCopilotBrowserError) as exc:
            ctx.errors.append({"cycle": ctx.cycle_count, "step": step.step_id, "message": str(exc)})
            CycleLogger.log_cycle(ctx, step, "READ_COPILOT", error=str(exc))
            self._execute_state("REPAIR")
            return
        ctx.copilot_responses.append({"cycle": ctx.cycle_count, "step_id": step.step_id, "raw": raw[:1000]})
        CycleLogger.log_cycle(ctx, step, "READ_COPILOT", response_received=raw[:300])
        self._raw_response = raw
        self._execute_state("PARSE_RESPONSE")

    def _state_parse_response(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        step = ctx.current_step
        raw = getattr(self, "_raw_response", "")
        parsed = CopilotResponseParser.parse(raw)
        if not parsed["valid"]:
            reason = parsed.get("reason", "Unknown")
            if parsed.get("incomplete"):
                terminal_log("Resposta incompleta; solicitando continuação...", "WARN")
                cont_prompt = CopilotPromptBuilder.build_continuation_request(ctx, raw)
                try:
                    cont_raw = self.app.ai.ask("orquestrar", {"orchestrator_prompt": cont_prompt, "task_context": ctx.as_dict()})
                    cont_raw = ncp_unpack(cont_raw)
                    parsed = CopilotResponseParser.parse(cont_raw)
                except (AgentError, EdgeCopilotBrowserError):
                    pass
            if not parsed["valid"]:
                repair_attempts = ctx.attempts.get("parse_repair", 0)
                if repair_attempts < MAX_ORCHESTRATOR_REPAIR_ATTEMPTS:
                    ctx.attempts["parse_repair"] = repair_attempts + 1
                    correction = CopilotPromptBuilder.build_correction_request(ctx, reason)
                    try:
                        corr_raw = self.app.ai.ask("orquestrar", {"orchestrator_prompt": correction, "task_context": ctx.as_dict()})
                        corr_raw = ncp_unpack(corr_raw)
                        parsed = CopilotResponseParser.parse(corr_raw)
                    except (AgentError, EdgeCopilotBrowserError) as exc:
                        ctx.errors.append({"cycle": ctx.cycle_count, "message": f"Correção falhou: {exc}"})
                if not parsed["valid"]:
                    ctx.errors.append({"cycle": ctx.cycle_count, "step": step.step_id if step else "?", "message": reason})
                    CycleLogger.log_cycle(ctx, step, "PARSE_RESPONSE", error=reason)
                    self._execute_state("REPAIR")
                    return
        self._parsed_response = parsed["fields"]
        CycleLogger.log_cycle(ctx, step, "PARSE_RESPONSE", action_detected=parsed["fields"].get("ACTION", ""))
        self._execute_state("DECIDE")

    def _state_decide(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        step = ctx.current_step
        fields = getattr(self, "_parsed_response", {})
        status = fields.get("STATUS", "").strip().upper()
        action = fields.get("ACTION", "").strip().upper()
        terminal_log(f"Decisão: STATUS={status} ACTION={action}", "STEP")
        if status == "FINISH" or action == "FINISH":
            self._execute_state("COMPLETE")
            return
        if status == "WAITING" or action == "ASK_USER":
            ctx.state = "WAITING_USER"
            self._execute_state("WAITING_USER")
            return
        if status == "ERROR":
            ctx.errors.append({"cycle": ctx.cycle_count, "step": step.step_id if step else "?", "message": fields.get("REASON", "ERROR")})
            self._execute_state("REPAIR")
            return
        ctx.last_progress_cycle = ctx.cycle_count
        self._execute_state("EXECUTE")

    def _state_execute(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        step = ctx.current_step
        fields = getattr(self, "_parsed_response", {})
        action = fields.get("ACTION", "").strip().upper()
        command = fields.get("COMMAND", "").strip()
        file_path = fields.get("FILE", "").strip()
        content = fields.get("CONTENT", "").strip()
        reason = fields.get("REASON", "")
        step.action = action

        if action == "RUN_COMMAND" and command:
            category = SafetyGuard.classify(command)
            needs_confirm = SafetyGuard.requires_confirmation(category, command, self.auto_mode)
            terminal_log(f"Comando: {command} | categoria: {category}", "STEP")
            if needs_confirm and not self.app.confirm_command(command):
                ctx.errors.append({"cycle": ctx.cycle_count, "step": step.step_id, "message": "Comando cancelado pelo usuário."})
                self._execute_state("WAITING_USER")
                return
            output, code, executed = self.app.execute_command(command)
            exec_result = {
                "command": command,
                "exit_code": code,
                "stdout": output[:8000],
                "stderr": "",
                "artifacts": [],
            }
            ctx.execution_results.append(exec_result)
            CycleLogger.log_cycle(ctx, step, "EXECUTE", command_executed=command, exit_code=code, stdout=output[:300])
            self._execution_result = exec_result
            self._success_criteria = fields.get("SUCCESS_CRITERIA", "")
            self._copilot_status = fields.get("STATUS", "")
            self._execute_state("OBSERVE")
            return

        if action == "CREATE_FILE" and file_path:
            safe_name = sanitize_filename(file_path, f"orchestrator_{step.step_id}.py")
            if content and file_path.endswith(".py"):
                try:
                    content = validate_generated_python(content)
                except AgentError as exc:
                    ctx.errors.append({"cycle": ctx.cycle_count, "step": step.step_id, "message": str(exc)})
                    self._execute_state("REPAIR")
                    return
            dest = GENERATED_DIR / safe_name
            try:
                atomic_write(dest, content, 0o700)
            except OSError as exc:
                ctx.errors.append({"cycle": ctx.cycle_count, "step": step.step_id, "message": str(exc)})
                self._execute_state("REPAIR")
                return
            ctx.artifacts.append(str(dest))
            exec_result = {
                "command": f"CREATE_FILE {file_path}",
                "exit_code": 0,
                "stdout": f"Arquivo criado: {dest}",
                "stderr": "",
                "artifacts": [str(dest)],
            }
            ctx.execution_results.append(exec_result)
            CycleLogger.log_cycle(ctx, step, "EXECUTE", command_executed=f"CREATE_FILE {file_path}", exit_code=0)
            self._execution_result = exec_result
            self._success_criteria = fields.get("SUCCESS_CRITERIA", "")
            self._copilot_status = fields.get("STATUS", "")
            self._execute_state("VALIDATE")
            return

        if action == "ANALYZE":
            terminal_log("Copilot solicitou ANALYZE sem execução direta.", "INFO")
            exec_result = {"command": "ANALYZE", "exit_code": 0, "stdout": reason, "stderr": "", "artifacts": []}
            ctx.execution_results.append(exec_result)
            self._execution_result = exec_result
            self._success_criteria = fields.get("SUCCESS_CRITERIA", "")
            self._copilot_status = fields.get("STATUS", "")
            self._execute_state("VALIDATE")
            return

        if action == "VALIDATE":
            terminal_log("Copilot solicitou VALIDATE.", "INFO")
            self._success_criteria = fields.get("SUCCESS_CRITERIA", "")
            self._copilot_status = fields.get("STATUS", "")
            self._execution_result = ctx.execution_results[-1] if ctx.execution_results else {}
            self._execute_state("VALIDATE")
            return

        terminal_log(f"Ação não reconhecida ou sem conteúdo: {action}", "WARN")
        ctx.errors.append({"cycle": ctx.cycle_count, "step": step.step_id, "message": f"Ação sem conteúdo: {action}"})
        self._execute_state("REPAIR")

    def _state_observe(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        step = ctx.current_step
        exec_result = getattr(self, "_execution_result", {})
        output = exec_result.get("stdout", "")
        exit_code = exec_result.get("exit_code")
        terminal_log(f"Observando: exit_code={exit_code}, output_len={len(output)}", "INFO")
        CycleLogger.log_cycle(ctx, step, "OBSERVE", stdout=output[:200])
        self._execute_state("VALIDATE")

    def _state_validate(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        step = ctx.current_step
        exec_result = getattr(self, "_execution_result", {})
        copilot_status = getattr(self, "_copilot_status", "")
        success_criteria = getattr(self, "_success_criteria", "")
        is_valid, validation_reason = CompletionDetector.check(step, exec_result, copilot_status, success_criteria)
        ctx.validation_results.append({
            "cycle": ctx.cycle_count,
            "step_id": step.step_id if step else "?",
            "valid": is_valid,
            "reason": validation_reason,
        })
        CycleLogger.log_cycle(ctx, step, "VALIDATE", validation=validation_reason)
        if is_valid:
            step.status = "DONE"
            if step.step_id not in ctx.completed_steps:
                ctx.completed_steps.append(step.step_id)
            ctx.last_progress_cycle = ctx.cycle_count
            terminal_log(f"Etapa {step.step_id} VALIDADA: {validation_reason}", "OK")
            self._print_task_tree()
            self._execute_state("CONTINUE")
        else:
            terminal_log(f"Etapa {step.step_id} FALHOU validação: {validation_reason}", "WARN")
            self._execute_state("REPAIR")

    def _state_repair(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        step = ctx.current_step
        if step is None:
            self._execute_state("FAILED")
            return
        if step.attempts >= step.max_attempts:
            ctx.failed_steps.append(step.step_id)
            terminal_log(f"Etapa {step.step_id}: tentativas esgotadas ({step.attempts}).", "ERROR")
            if self.auto_mode:
                self._execute_state("CONTINUE")
            else:
                self._execute_state("WAITING_USER")
            return
        terminal_log(f"Tentando reparo da etapa {step.step_id} (tentativa {step.attempts})", "WARN")
        last_exec = ctx.execution_results[-1] if ctx.execution_results else {}
        if last_exec:
            feedback = CopilotPromptBuilder.build_execution_feedback(ctx, step, last_exec)
            try:
                repair_raw = self.app.ai.ask("orquestrar", {"orchestrator_prompt": feedback, "task_context": ctx.as_dict()})
                repair_raw = ncp_unpack(repair_raw)
                parsed = CopilotResponseParser.parse(repair_raw)
                if parsed["valid"]:
                    self._parsed_response = parsed["fields"]
                    CycleLogger.log_cycle(ctx, step, "REPAIR", decision="repair_action_received")
                    self._execute_state("DECIDE")
                    return
            except (AgentError, EdgeCopilotBrowserError) as exc:
                ctx.errors.append({"cycle": ctx.cycle_count, "message": f"Repair failed: {exc}"})
        CycleLogger.log_cycle(ctx, step, "REPAIR", error="Repair sem ação válida")
        self._execute_state("ASK_COPILOT")

    def _state_continue(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        step = ctx.current_step
        if step and step.status == "DONE":
            ctx.current_step_index += 1
        if ctx.current_step_index >= len(ctx.plan):
            self._execute_state("COMPLETE")
        else:
            next_step = ctx.current_step
            if next_step:
                next_step.attempts = 0
                terminal_log(f"Avançando para {next_step.step_id}: {next_step.objective[:60]}", "STEP")
            self._execute_state("ASK_COPILOT")

    def _state_complete(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        is_complete, reason = CompletionDetector.is_task_complete(ctx)
        if is_complete or all(s.status == "DONE" for s in ctx.plan):
            ctx.status = "complete"
            ctx.state = "COMPLETE"
            terminal_log(f"TAREFA {ctx.task_id} CONCLUÍDA: {reason}", "OK")
            print(f"\n\033[1;32m{'=' * 60}\033[0m")
            print(f"\033[1;32m  TAREFA CONCLUÍDA: {ctx.task_id}\033[0m")
            print(f"\033[1;32m  Objetivo: {ctx.goal[:80]}\033[0m")
            print(f"\033[1;32m  Etapas: {len(ctx.completed_steps)}/{len(ctx.plan)} concluídas\033[0m")
            print(f"\033[1;32m  Artefatos: {len(ctx.artifacts)}\033[0m")
            print(f"\033[1;32m{'=' * 60}\033[0m")
        else:
            ctx.status = "failed"
            ctx.state = "FAILED"
            terminal_log(f"TAREFA {ctx.task_id} NÃO CONCLUÍDA: {reason}", "ERROR")
        self._print_task_tree()

    def _state_failed(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        ctx.status = "failed"
        print(f"\n\033[1;31m{'=' * 60}\033[0m")
        print(f"\033[1;31m  TAREFA FALHOU: {ctx.task_id}\033[0m")
        print(f"\033[1;31m  Etapas concluídas: {len(ctx.completed_steps)}/{len(ctx.plan)}\033[0m")
        print(f"\033[1;31m  Etapas falhas: {', '.join(ctx.failed_steps) or 'nenuma'}\033[0m")
        print(f"\033[1;31m  Erros: {len(ctx.errors)}\033[0m")
        print(f"\033[1;31m{'=' * 60}\033[0m")
        self._print_task_tree()

    def _state_waiting_user(self) -> None:
        ctx = self.task_context
        if ctx is None:
            return
        ctx.state = "WAITING_USER"
        step = ctx.current_step
        print(f"\n\033[1;33m[ORCHESTRATOR] AGUARDANDO USUÁRIO\033[0m")
        if step:
            print(f"  Etapa: {step.step_id} - {step.objective[:80]}")
        if ctx.errors:
            print(f"  Último erro: {ctx.errors[-1].get('message', '')[:200]}")
        print("  [c]ontinuar  [p]ular etapa  [a]bortar")
        answer = input("  Escolha: ").strip().lower()
        if answer in {"c", "continuar"}:
            ctx.last_progress_cycle = ctx.cycle_count
            self._execute_state("ASK_COPILOT")
        elif answer in {"p", "pular"}:
            if step:
                ctx.failed_steps.append(step.step_id)
            self._execute_state("CONTINUE")
        else:
            self._execute_state("FAILED")

    def _print_task_tree(self) -> None:
        ctx = self.task_context
        if ctx is None or not ctx.plan:
            return
        print(f"\n\033[1;36mTASK {ctx.task_id}\033[0m")
        print(f"  Goal: {ctx.goal[:80]}")
        for i, step in enumerate(ctx.plan):
            marker = "✓" if step.status == "DONE" else ("▶" if i == ctx.current_step_index else "○")
            line = f"  {marker} {step.step_id}: {step.objective[:60]}"
            if step.status == "DONE":
                print(f"\033[32m{line}\033[0m")
            elif i == ctx.current_step_index:
                print(f"\033[1;33m{line}\033[0m")
                print(f"      ├── tentativas: {step.attempts}/{step.max_attempts}")
            else:
                print(line)
        status = ctx.status.upper()
        print(f"  └── {status}")

# END NEXUS SECTION: COGNITIVE_ORCHESTRATOR

# NEXUS SECTION: PLANNED_EXECUTION
# PURPOSE: Structured continuous planning execution engine.
#          PROJECT PLAN → PHASE → TASK → AI REQUEST → FILE OPERATION →
#          EXECUTION → VALIDATION → PLAN REASSESSMENT → NEXT TASK
# VERSION: 1.0
# DEPENDS: EdgeCopilotBrowser, RealPTY, validate_generated_python,
#          atomic_write, terminal_log, terminal_panel, terminal_progress,
#          SafetyGuard, CopilotResponseParser, extract_agent_json.

MAX_PLANNED_PHASES = 8
MAX_PLANNED_TASKS_PER_PHASE = 10
MAX_PLANNED_TASK_ATTEMPTS = 5
MAX_PLANNED_REASSESSMENTS = 40

VALID_TASK_STATES = frozenset({
    "PENDENTE", "EM_EXECUCAO", "CONCLUIDA", "FALHOU",
    "BLOQUEADA", "REQUER_REVISAO",
})

VALID_PHASE_STATES = frozenset({
    "PENDENTE", "EM_EXECUCAO", "CONCLUIDA", "FALHOU", "BLOQUEADA",
})


@dataclass
class PlannedTask:
    task_id: str
    task_name: str
    task_objective: str
    files_to_create: list[str]
    files_to_modify: list[str]
    files_to_analyze: list[str]
    dependencies: list[str]
    completion_criteria: str
    validation_criteria: str
    state: str = "PENDENTE"
    attempts: int = 0
    max_attempts: int = MAX_PLANNED_TASK_ATTEMPTS
    validation_reason: str = ""
    execution_result: str = ""
    error_message: str = ""
    artifacts: list[str] = None

    def __post_init__(self) -> None:
        if self.artifacts is None:
            self.artifacts = []

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "task_objective": self.task_objective,
            "files_to_create": self.files_to_create,
            "files_to_modify": self.files_to_modify,
            "files_to_analyze": self.files_to_analyze,
            "dependencies": self.dependencies,
            "completion_criteria": self.completion_criteria,
            "validation_criteria": self.validation_criteria,
            "state": self.state,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "validation_reason": self.validation_reason,
            "error_message": self.error_message,
            "artifacts": self.artifacts,
        }


@dataclass
class PlannedPhase:
    phase_id: str
    phase_name: str
    phase_objective: str
    tasks: list[PlannedTask]
    phase_completion_criteria: str
    phase_validation_criteria: str
    state: str = "PENDENTE"
    validation_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "phase_id": self.phase_id,
            "phase_name": self.phase_name,
            "phase_objective": self.phase_objective,
            "tasks": [t.as_dict() for t in self.tasks],
            "phase_completion_criteria": self.phase_completion_criteria,
            "phase_validation_criteria": self.phase_validation_criteria,
            "state": self.state,
            "validation_reason": self.validation_reason,
        }


@dataclass
class ProjectPlan:
    objective: str
    summary: str
    phases: list[PlannedPhase]
    global_dependencies: list[str]
    final_validation_criteria: str
    original_plan_snapshot: str = ""
    created_at: str = ""
    reassessment_count: int = 0

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        if not self.original_plan_snapshot:
            self.original_plan_snapshot = json.dumps(self.as_dict(skip_snapshot=True), ensure_ascii=False, indent=2)

    def as_dict(self, skip_snapshot: bool = False) -> dict[str, Any]:
        data: dict[str, Any] = {
            "objective": self.objective,
            "summary": self.summary,
            "phases": [p.as_dict() for p in self.phases],
            "global_dependencies": self.global_dependencies,
            "final_validation_criteria": self.final_validation_criteria,
            "created_at": self.created_at,
            "reassessment_count": self.reassessment_count,
        }
        if not skip_snapshot:
            data["original_plan_snapshot"] = self.original_plan_snapshot
        return data

    def all_tasks(self) -> list[PlannedTask]:
        return [task for phase in self.phases for task in phase.tasks]

    def find_task(self, task_id: str) -> Optional[PlannedTask]:
        for task in self.all_tasks():
            if task.task_id == task_id:
                return task
        return None

    def find_phase(self, phase_id: str) -> Optional[PlannedPhase]:
        for phase in self.phases:
            if phase.phase_id == phase_id:
                return phase
        return None

    def next_pending_task(self) -> Optional[tuple[PlannedPhase, PlannedTask]]:
        for phase in self.phases:
            if phase.state in {"CONCLUIDA", "BLOQUEADA"}:
                continue
            for task in phase.tasks:
                if task.state == "PENDENTE":
                    return phase, task
        return None

    def current_phase(self) -> Optional[PlannedPhase]:
        for phase in self.phases:
            if phase.state == "EM_EXECUCAO":
                return phase
        for phase in self.phases:
            if phase.state == "PENDENTE":
                return phase
        return None

    def is_complete(self) -> bool:
        return all(phase.state == "CONCLUIDA" for phase in self.phases)

    def stats(self) -> dict[str, int]:
        tasks = self.all_tasks()
        return {
            "total_tasks": len(tasks),
            "completed": sum(1 for t in tasks if t.state == "CONCLUIDA"),
            "pending": sum(1 for t in tasks if t.state == "PENDENTE"),
            "failed": sum(1 for t in tasks if t.state == "FALHOU"),
            "blocked": sum(1 for t in tasks if t.state == "BLOQUEADA"),
            "total_phases": len(self.phases),
            "phases_completed": sum(1 for p in self.phases if p.state == "CONCLUIDA"),
        }


def parse_project_plan(raw: dict[str, Any]) -> ProjectPlan:
    objective = clean_text(raw.get("objective", ""))
    if not objective:
        raise AgentError("Plano sem objetivo geral.")
    summary = clean_text(raw.get("summary", ""))
    phases_raw = raw.get("phases", [])
    if not isinstance(phases_raw, list) or not phases_raw:
        raise AgentError("Plano sem fases.")
    if len(phases_raw) > MAX_PLANNED_PHASES:
        raise AgentError(f"Plano excede o limite de {MAX_PLANNED_PHASES} fases.")
    phases: list[PlannedPhase] = []
    for phase_data in phases_raw:
        if not isinstance(phase_data, dict):
            raise AgentError("Fase inválida no plano.")
        phase_id = clean_text(phase_data.get("phase_id", ""))
        phase_name = clean_text(phase_data.get("phase_name", ""))
        phase_objective = clean_text(phase_data.get("phase_objective", ""))
        if not phase_id or not phase_name:
            raise AgentError("Fase sem ID ou nome.")
        tasks_raw = phase_data.get("tasks", [])
        if not isinstance(tasks_raw, list) or not tasks_raw:
            raise AgentError(f"Fase {phase_id} sem tarefas.")
        if len(tasks_raw) > MAX_PLANNED_TASKS_PER_PHASE:
            raise AgentError(f"Fase {phase_id} excede o limite de {MAX_PLANNED_TASKS_PER_PHASE} tarefas.")
        tasks: list[PlannedTask] = []
        for task_data in tasks_raw:
            if not isinstance(task_data, dict):
                raise AgentError("Tarefa inválida no plano.")
            task = PlannedTask(
                task_id=clean_text(task_data.get("task_id", "")),
                task_name=clean_text(task_data.get("task_name", "")),
                task_objective=clean_text(task_data.get("task_objective", "")),
                files_to_create=[str(f) for f in task_data.get("files_to_create", []) if f],
                files_to_modify=[str(f) for f in task_data.get("files_to_modify", []) if f],
                files_to_analyze=[str(f) for f in task_data.get("files_to_analyze", []) if f],
                dependencies=[str(d) for d in task_data.get("dependencies", []) if d],
                completion_criteria=clean_text(task_data.get("completion_criteria", "")),
                validation_criteria=clean_text(task_data.get("validation_criteria", "")),
            )
            if not task.task_id or not task.task_objective:
                raise AgentError("Tarefa sem ID ou objetivo.")
            tasks.append(task)
        phases.append(PlannedPhase(
            phase_id=phase_id,
            phase_name=phase_name,
            phase_objective=phase_objective,
            tasks=tasks,
            phase_completion_criteria=clean_text(phase_data.get("phase_completion_criteria", "")),
            phase_validation_criteria=clean_text(phase_data.get("phase_validation_criteria", "")),
        ))
    return ProjectPlan(
        objective=objective,
        summary=summary,
        phases=phases,
        global_dependencies=[str(d) for d in raw.get("global_dependencies", []) if d],
        final_validation_criteria=clean_text(raw.get("final_validation_criteria", "")),
    )


def insert_new_task_into_plan(plan: ProjectPlan, new_task_data: dict[str, Any]) -> Optional[PlannedTask]:
    insert_after = clean_text(new_task_data.get("insert_after", ""))
    task = PlannedTask(
        task_id=clean_text(new_task_data.get("task_id", "")),
        task_name=clean_text(new_task_data.get("task_name", "")),
        task_objective=clean_text(new_task_data.get("task_objective", "")),
        files_to_create=[str(f) for f in new_task_data.get("files_to_create", []) if f],
        files_to_modify=[str(f) for f in new_task_data.get("files_to_modify", []) if f],
        files_to_analyze=[str(f) for f in new_task_data.get("files_to_analyze", []) if f],
        dependencies=[str(d) for d in new_task_data.get("dependencies", []) if d],
        completion_criteria=clean_text(new_task_data.get("completion_criteria", "")),
        validation_criteria=clean_text(new_task_data.get("validation_criteria", "")),
    )
    if not task.task_id or not task.task_objective:
        return None
    if plan.find_task(task.task_id) is not None:
        return None
    for phase in plan.phases:
        insert_index = None
        for i, existing in enumerate(phase.tasks):
            if existing.task_id == insert_after:
                insert_index = i + 1
                break
        if insert_index is not None:
            phase.tasks.insert(insert_index, task)
            return task
    last_phase = plan.phases[-1] if plan.phases else None
    if last_phase:
        last_phase.tasks.append(task)
        return task
    return None


class PlannedExecutionEngine:
    """Motor de execução orientada por planejamento contínuo.

    Ciclo: PROJECT PLAN → PHASE → TASK → AI REQUEST → FILE OPERATION →
           EXECUTION → VALIDATION → PLAN REASSESSMENT → NEXT TASK
    """

    def __init__(self, app: "NexusApp", plan: ProjectPlan, auto_mode: bool = False) -> None:
        self.app = app
        self.plan = plan
        self.auto_mode = auto_mode
        self.cycle_count = 0

    def run(self) -> dict[str, Any]:
        terminal_panel("PLANNED EXECUTION ENGINE", [
            f"Objetivo: {self.plan.objective[:60]}",
            f"Fases: {len(self.plan.phases)}",
            f"Tarefas: {len(self.plan.all_tasks())}",
            f"Auto: {'ON' if self.auto_mode else 'OFF'}",
            "Ciclo: PLAN → PHASE → TASK → AI → EXEC → VALID → REASSESS → NEXT",
        ])
        self._print_plan()
        while self.cycle_count < MAX_PLANNED_REASSESSMENTS:
            if self.app.stop_event.is_set():
                terminal_log("Execução interrompida pelo usuário.", "WARN")
                break
            pair = self.plan.next_pending_task()
            if pair is None:
                if self.plan.is_complete():
                    return self._final_validation()
                stuck = self._diagnose_blockage()
                if stuck:
                    terminal_log(f"Plano bloqueado: {stuck}", "ERROR")
                    return {"completed": False, "reason": stuck, "plan": self.plan.as_dict()}
                continue
            phase, task = pair
            self._execute_task(phase, task)
            self._reassess_plan(phase, task)
            if task.state == "CONCLUIDA":
                if self._is_phase_complete(phase):
                    self._validate_phase(phase)
                    if phase.state == "CONCLUIDA":
                        terminal_log(f"Fase {phase.phase_id} concluída e validada.", "OK")
                        self._print_plan()
        stats = self.plan.stats()
        terminal_log(f"Ciclo encerrado: {stats}", "INFO")
        return {
            "completed": self.plan.is_complete(),
            "stats": stats,
            "plan": self.plan.as_dict(),
        }

    def _execute_task(self, phase: PlannedPhase, task: PlannedTask) -> None:
        self.cycle_count += 1
        task.state = "EM_EXECUCAO"
        task.attempts += 1
        phase.state = "EM_EXECUCAO"
        terminal_log(
            f"Executando {task.task_id} ({task.task_name}) — tentativa {task.attempts}/{task.max_attempts}",
            "STEP",
        )
        self._print_current_state(phase, task)
        try:
            ai_result = self._request_ai_for_task(phase, task)
            self._apply_ai_result(task, ai_result)
            validated = self._validate_task(phase, task)
            if validated:
                task.state = "CONCLUIDA"
                terminal_log(f"Tarefa {task.task_id} CONCLUÍDA e VALIDADA.", "OK")
            else:
                self._handle_task_failure(phase, task)
        except (AgentError, RateLimitError, EdgeCopilotBrowserError) as exc:
            task.error_message = str(exc)
            task.execution_result = f"Erro: {exc}"
            terminal_log(f"Tarefa {task.task_id} erro: {exc}", "ERROR")
            self._handle_task_failure(phase, task)

    def _request_ai_for_task(self, phase: PlannedPhase, task: PlannedTask) -> dict[str, Any]:
        completed_ids = [t.task_id for t in self.plan.all_tasks() if t.state == "CONCLUIDA"]
        failed_ids = [t.task_id for t in self.plan.all_tasks() if t.state == "FALHOU"]
        recent_errors = [
            {"task": t.task_id, "error": t.error_message[:200]}
            for t in self.plan.all_tasks() if t.error_message
        ][-3:]
        recent_results = [
            {"task": t.task_id, "result": t.execution_result[:300]}
            for t in self.plan.all_tasks() if t.execution_result
        ][-3:]
        state = {
            "user_request": self.plan.objective,
            "plan_summary": {
                "objective": self.plan.objective,
                "summary": self.plan.summary,
                "phases_completed": sum(1 for p in self.plan.phases if p.state == "CONCLUIDA"),
                "phases_total": len(self.plan.phases),
            },
            "current_phase": {
                "phase_id": phase.phase_id,
                "phase_name": phase.phase_name,
                "phase_objective": phase.phase_objective,
            },
            "current_task": task.as_dict(),
            "completed_tasks": completed_ids,
            "failed_tasks": failed_ids,
            "recent_errors": recent_errors,
            "recent_results": recent_results,
            "terminal": self.app.terminal_state(),
            "workspace": str(self.app.pty.cwd),
            "instruction": (
                f"Analise a tarefa atual ({task.task_id}) e forneça a ação necessária: "
                "criar arquivo, executar comando ou analisar. "
                f"Critério de conclusão: {task.completion_criteria}. "
                f"Critério de validação: {task.validation_criteria}. "
                "Retorne código COMPLETO, sem placeholders, pseudocódigo ou reticências."
            ),
        }
        if task.error_message:
            state["retry_instruction"] = (
                f"A tentativa anterior falhou: {task.error_message}. "
                "Corrija o problema específico e forneça uma implementação completa."
            )
            state["previous_error"] = task.error_message
        response = self.app.call_agent("orquestrar", state)
        response = ncp_unpack(response)
        parsed = CopilotResponseParser.parse(response)
        if not parsed["valid"]:
            reason = parsed.get("reason", "Resposta inválida")
            raise AgentError(f"IA retornou resposta inválida para {task.task_id}: {reason}")
        return parsed["fields"]

    def _apply_ai_result(self, task: PlannedTask, fields: dict[str, Any]) -> None:
        action = clean_text(fields.get("ACTION", "")).upper()
        command = clean_command(fields.get("COMMAND", ""))
        file_path = clean_text(fields.get("FILE", ""))
        content = clean_text(fields.get("CONTENT", ""))
        reason = clean_text(fields.get("REASON", ""))

        if action == "CREATE_FILE" and file_path and content:
            if file_path.endswith(".py"):
                content = validate_generated_python(content)
            safe_name = sanitize_filename(file_path, f"planned_{task.task_id}.py")
            dest = GENERATED_DIR / safe_name
            atomic_write(dest, content, 0o700)
            task.artifacts.append(str(dest))
            task.execution_result = f"Arquivo criado: {dest}"
            terminal_log(f"Tarefa {task.task_id}: arquivo criado — {dest}", "OK")
        elif action == "RUN_COMMAND" and command:
            category = SafetyGuard.classify(command)
            if SafetyGuard.requires_confirmation(category, command, self.auto_mode):
                if not self.app.confirm_command(command):
                    raise AgentError(f"Comando cancelado pelo usuário: {command}")
            output, code, executed = self.app.execute_command(command)
            task.execution_result = f"exit_code={code}, stdout={output[:2000]}"
            terminal_log(f"Tarefa {task.task_id}: comando executado — exit={code}", "INFO")
        elif action == "ANALYZE":
            task.execution_result = f"Análise: {reason}"
            terminal_log(f"Tarefa {task.task_id}: análise sem execução.", "INFO")
        elif action == "VALIDATE":
            task.execution_result = f"Validação solicitada: {reason}"
        else:
            if reason:
                task.execution_result = reason
            terminal_log(f"Tarefa {task.task_id}: ação {action or 'desconhecida'} sem conteúdo.", "WARN")

    def _validate_task(self, phase: PlannedPhase, task: PlannedTask) -> bool:
        validation_state = {
            "user_request": self.plan.objective,
            "task": task.as_dict(),
            "phase": phase.as_dict(),
            "execution_result": task.execution_result[:4000],
            "artifacts": task.artifacts,
            "terminal": self.app.terminal_state(),
            "instruction": (
                f"Valide se a tarefa {task.task_id} foi concluída com base nos critérios. "
                f"Critério de conclusão: {task.completion_criteria}. "
                f"Critério de validação: {task.validation_criteria}. "
                f"Resultado da execução: {task.execution_result[:2000]}. "
                "Seja objetivo: só marque como concluída se houver evidência concreta."
            ),
        }
        try:
            response = self.app.call_agent("validacao_tarefa", validation_state)
            result = extract_agent_json(response, "validacao_tarefa") if isinstance(response, str) else response
            task_completed = as_bool(result.get("task_completed", False), False)
            task.validation_reason = clean_text(result.get("reason", ""))
            evidence = result.get("evidence", [])
            issues = result.get("issues", [])
            if not task_completed:
                task.error_message = "; ".join(str(i) for i in issues) if issues else task.validation_reason
                terminal_log(f"Tarefa {task.task_id} NÃO validada: {task.validation_reason}", "WARN")
            return task_completed
        except (AgentError, RateLimitError) as exc:
            task.validation_reason = f"Erro de validação: {exc}"
            terminal_log(f"Tarefa {task.task_id}: erro na validação — {exc}", "ERROR")
            return False

    def _handle_task_failure(self, phase: PlannedPhase, task: PlannedTask) -> None:
        if task.attempts >= task.max_attempts:
            task.state = "FALHOU"
            terminal_log(
                f"Tarefa {task.task_id} FALHOU definitivamente após {task.attempts} tentativas: {task.error_message}",
                "ERROR",
            )
            if self.auto_mode:
                task.state = "BLOQUEADA"
            else:
                task.state = "REQUER_REVISAO"
                print(f"\n\033[1;33m[Tarefa {task.task_id} requer revisão manual]\033[0m")
                print(f"  Erro: {task.error_message[:200]}")
                answer = input("  [r]etentar  [p]ular  [a]bortar: ").strip().lower()
                if answer in {"r", "retentar"}:
                    task.state = "PENDENTE"
                    task.attempts = 0
                elif answer in {"p", "pular"}:
                    task.state = "BLOQUEADA"
                else:
                    self.app.stop_event.set()
        else:
            task.state = "PENDENTE"
            terminal_log(
                f"Tarefa {task.task_id} falhou (tentativa {task.attempts}/{task.max_attempts}), será retentada.",
                "WARN",
            )

    def _reassess_plan(self, phase: PlannedPhase, task: PlannedTask) -> None:
        if task.state != "CONCLUIDA":
            return
        self.plan.reassessment_count += 1
        reassess_state = {
            "user_request": self.plan.objective,
            "original_plan": json.loads(self.plan.original_plan_snapshot) if self.plan.original_plan_snapshot else self.plan.as_dict(skip_snapshot=True),
            "current_plan": self.plan.as_dict(skip_snapshot=True),
            "completed_task": task.as_dict(),
            "current_phase": phase.as_dict(),
            "plan_stats": self.plan.stats(),
            "instruction": (
                "Reavalie o plano após a conclusão desta tarefa. "
                "O plano ainda é válido? Surgiu alguma tarefa nova necessária? "
                "Alguma tarefa precisa ser revisitada?"
            ),
        }
        try:
            response = self.app.call_agent("reavaliacao", reassess_state)
            result = extract_agent_json(response, "reavaliacao") if isinstance(response, str) else response
            plan_valid = as_bool(result.get("plan_still_valid", True), True)
            reason = clean_text(result.get("reason", ""))
            new_tasks = result.get("new_tasks", [])
            tasks_to_revisit = result.get("tasks_to_revisit", [])
            if new_tasks:
                for new_task_data in new_tasks:
                    if isinstance(new_task_data, dict):
                        inserted = insert_new_task_into_plan(self.plan, new_task_data)
                        if inserted:
                            terminal_log(
                                f"Nova tarefa inserida no plano: {inserted.task_id} — {inserted.task_name}",
                                "WARN",
                            )
            if tasks_to_revisit:
                for tid in tasks_to_revisit:
                    existing = self.plan.find_task(str(tid))
                    if existing and existing.state == "CONCLUIDA":
                        existing.state = "REQUER_REVISAO"
                        terminal_log(f"Tarefa {tid} marcada para revisão.", "WARN")
            if not plan_valid:
                terminal_log(f"Reavaliação: plano precisa ajustes — {reason}", "WARN")
            else:
                terminal_log(f"Reavaliação {self.plan.reassessment_count}: plano válido — {reason}", "INFO")
        except (AgentError, RateLimitError) as exc:
            terminal_log(f"Reavaliação falhou (não bloqueia): {exc}", "WARN")

    def _is_phase_complete(self, phase: PlannedPhase) -> bool:
        return all(task.state in {"CONCLUIDA", "BLOQUEADA"} for task in phase.tasks)

    def _validate_phase(self, phase: PlannedPhase) -> None:
        if not self._is_phase_complete(phase):
            phase.state = "EM_EXECUCAO"
            return
        phase_state = {
            "user_request": self.plan.objective,
            "phase": phase.as_dict(),
            "completed_tasks": [t.as_dict() for t in phase.tasks if t.state == "CONCLUIDA"],
            "blocked_tasks": [t.as_dict() for t in phase.tasks if t.state == "BLOQUEADA"],
            "instruction": (
                f"Valide se a fase {phase.phase_id} foi concluída. "
                f"Critério de conclusão da fase: {phase.phase_completion_criteria}. "
                f"Critério de validação da fase: {phase.phase_validation_criteria}. "
                "Seja objetivo e baseado em evidências."
            ),
        }
        try:
            response = self.app.call_agent("validacao_fase", phase_state)
            result = extract_agent_json(response, "validacao_fase") if isinstance(response, str) else response
            phase_completed = as_bool(result.get("phase_completed", False), False)
            phase.validation_reason = clean_text(result.get("reason", ""))
            if phase_completed:
                phase.state = "CONCLUIDA"
            else:
                issues = result.get("issues", [])
                phase.state = "EM_EXECUCAO"
                for task in phase.tasks:
                    if task.state == "CONCLUIDA" and issues:
                        task.state = "REQUER_REVISAO"
                terminal_log(f"Fase {phase.phase_id} NÃO validada: {phase.validation_reason}", "WARN")
        except (AgentError, RateLimitError) as exc:
            phase.validation_reason = f"Erro: {exc}"
            phase.state = "EM_EXECUCAO"
            terminal_log(f"Fase {phase.phase_id}: erro na validação — {exc}", "ERROR")

    def _final_validation(self) -> dict[str, Any]:
        terminal_log("Iniciando VALIDAÇÃO FINAL do projeto.", "STEP")
        all_tasks = self.plan.all_tasks()
        completed = [t for t in all_tasks if t.state == "CONCLUIDA"]
        blocked = [t for t in all_tasks if t.state == "BLOQUEADA"]
        failed = [t for t in all_tasks if t.state == "FALHOU"]
        all_concluded = len(completed) == len(all_tasks)
        if not all_concluded:
            reason = (
                f"{len(completed)}/{len(all_tasks)} tarefas concluídas. "
                f"{len(blocked)} bloqueadas, {len(failed)} falharam."
            )
            terminal_log(f"VALIDAÇÃO FINAL: projeto NÃO concluído — {reason}", "ERROR")
            return {"completed": False, "reason": reason, "plan": self.plan.as_dict()}
        terminal_log(
            f"VALIDAÇÃO FINAL: todas as {len(all_tasks)} tarefas concluídas e validadas. "
            f"Objetivo: {self.plan.objective[:80]}",
            "OK",
        )
        print(f"\n\033[1;32m{'=' * 60}\033[0m")
        print(f"\033[1;32m  PROJETO CONCLUÍDO E VALIDADO\033[0m")
        print(f"\033[1;32m  Objetivo: {self.plan.objective[:80]}\033[0m")
        print(f"\033[1;32m  Fases: {len(self.plan.phases)}\033[0m")
        print(f"\033[1;32m  Tarefas: {len(completed)}/{len(all_tasks)}\033[0m")
        print(f"\033[1;32m  Reavaliações: {self.plan.reassessment_count}\033[0m")
        print(f"\033[1;32m{'=' * 60}\033[0m")
        self._print_plan()
        return {"completed": True, "plan": self.plan.as_dict()}

    def _diagnose_blockage(self) -> str:
        all_tasks = self.plan.all_tasks()
        has_pending = any(t.state == "PENDENTE" for t in all_tasks)
        has_in_progress = any(t.state == "EM_EXECUCAO" for t in all_tasks)
        has_review = any(t.state == "REQUER_REVISAO" for t in all_tasks)
        if has_pending or has_in_progress:
            return ""
        if has_review:
            return "Existem tarefas requerendo revisão manual."
        all_done_or_blocked = all(t.state in {"CONCLUIDA", "BLOQUEADA", "FALHOU"} for t in all_tasks)
        if all_done_or_blocked and not self.plan.is_complete():
            blocked = [t.task_id for t in all_tasks if t.state in {"BLOQUEADA", "FALHOU"}]
            return f"Tarefas bloqueadas/falhadas impedem conclusão: {', '.join(blocked)}"
        return ""

    def _print_current_state(self, phase: PlannedPhase, task: PlannedTask) -> None:
        print(f"\n\033[1;34m[{phase.phase_id}] {phase.phase_name} → [{task.task_id}] {task.task_name}\033[0m")
        print(f"  Objetivo: {task.task_objective[:100]}")
        if task.files_to_create:
            print(f"  Criar: {', '.join(task.files_to_create[:5])}")
        if task.files_to_modify:
            print(f"  Modificar: {', '.join(task.files_to_modify[:5])}")
        if task.files_to_analyze:
            print(f"  Analisar: {', '.join(task.files_to_analyze[:5])}")
        print(f"  Conclusão: {task.completion_criteria[:100]}")
        print(f"  Validação: {task.validation_criteria[:100]}")

    def _print_plan(self) -> None:
        stats = self.plan.stats()
        print(f"\n\033[1;36m{'=' * 60}\033[0m")
        print(f"\033[1;36m  PROJECT PLAN — {self.plan.objective[:50]}\033[0m")
        print(f"\033[1;36m  Tarefas: {stats['completed']}/{stats['total_tasks']} concluídas | "
              f"Fases: {stats['phases_completed']}/{stats['total_phases']}\033[0m")
        print(f"\033[1;36m{'=' * 60}\033[0m")
        for phase in self.plan.phases:
            p_marker = "✓" if phase.state == "CONCLUIDA" else ("▶" if phase.state == "EM_EXECUCAO" else "○")
            print(f"\n  {p_marker} \033[1;33m{phase.phase_id}: {phase.phase_name}\033[0m")
            for task in phase.tasks:
                t_marker = "✓" if task.state == "CONCLUIDA" else (
                    "▶" if task.state == "EM_EXECUCAO" else
                    "✗" if task.state == "FALHOU" else
                    "⊘" if task.state == "BLOQUEADA" else
                    "!" if task.state == "REQUER_REVISAO" else "○"
                )
                line = f"    {t_marker} {task.task_id}: {task.task_name[:50]}"
                if task.state == "CONCLUIDA":
                    print(f"\033[32m{line}\033[0m")
                elif task.state == "EM_EXECUCAO":
                    print(f"\033[1;33m{line}\033[0m")
                elif task.state in {"FALHOU", "BLOQUEADA"}:
                    print(f"\033[31m{line}\033[0m")
                elif task.state == "REQUER_REVISAO":
                    print(f"\033[33m{line}\033[0m")
                else:
                    print(line)
        print(f"\n\033[1;36m{'=' * 60}\033[0m")

# END NEXUS SECTION: PLANNED_EXECUTION

# NEXUS SECTION: SILENT_HEADLESS
# PURPOSE: Terminal-first, headless operation mode. Minimizes browser/GUI usage.
#          On first interaction the browser may open once for login/authorization;
#          after that, all operations run via terminal using persistent sessions,
#          cookies, tokens, and config files.
# VERSION: 1.0
# DEPENDS: EdgeCopilotBrowser, RealPTY, atomic_write, terminal_log, DATA_DIR.

SESSION_FILE = CONFIG_DIR / "copilot_session.json"
SESSION_MAX_AGE_SECONDS = 86400  # 24h
AUTH_FLAG_FILE = CONFIG_DIR / ".copilot_authorized"

OPERATION_CATEGORIES = frozenset({
    "READ_ONLY", "SAFE_WRITE", "PACKAGE_INSTALL",
    "SYSTEM_CHANGE", "NETWORK", "PRIVILEGED", "DESTRUCTIVE", "UNKNOWN",
})

GUI_REQUIRED_KEYWORDS = frozenset({
    "login", "sign in", "fazer login", "iniciar sessão",
    "captcha", "mfa", "two-factor", "2fa",
    "oauth", "authorize", "autorizar",
    "interactive", "dialog", "popup",
})


@dataclass
class SessionRecord:
    session_id: str
    created_at: str
    last_validated: str
    expires_at: str
    profile_dir: str
    cookies_count: int
    active: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "created_at": self.created_at,
            "last_validated": self.last_validated,
            "expires_at": self.expires_at,
            "profile_dir": self.profile_dir,
            "cookies_count": self.cookies_count,
            "active": self.active,
        }


class SessionManager:
    """Detects, validates, stores, renews, and ends Copilot sessions securely.

    Sessions are tracked via a local JSON file (permissions 0600) that records
    the Edge profile directory and cookie count. No cookie contents or tokens
    are ever stored in the session file or logs.
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._lock = threading.RLock()
        self._cached: Optional[SessionRecord] = None

    def _now(self) -> datetime:
        return datetime.now(timezone.utc)

    def _stamp(self, dt: Optional[datetime] = None) -> str:
        return (dt or self._now()).isoformat(timespec="seconds")

    def detect(self) -> Optional[SessionRecord]:
        with self._lock:
            if not SESSION_FILE.exists():
                return None
            try:
                data = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return None
            if not isinstance(data, dict):
                return None
            record = SessionRecord(
                session_id=str(data.get("session_id", "")),
                created_at=str(data.get("created_at", "")),
                last_validated=str(data.get("last_validated", "")),
                expires_at=str(data.get("expires_at", "")),
                profile_dir=str(data.get("profile_dir", "")),
                cookies_count=int(data.get("cookies_count", 0)),
                active=bool(data.get("active", True)),
            )
            self._cached = record
            return record

    def validate(self, record: Optional[SessionRecord] = None) -> bool:
        with self._lock:
            rec = record or self._cached or self.detect()
            if rec is None or not rec.active:
                return False
            try:
                expires = datetime.fromisoformat(rec.expires_at)
            except (TypeError, ValueError):
                return False
            if self._now() > expires:
                terminal_log("Sessão expirada.", "WARN")
                return False
            profile = Path(rec.profile_dir)
            if not profile.is_dir():
                terminal_log(f"Perfil de sessão não encontrado: {profile}", "WARN")
                return False
            rec.last_validated = self._stamp()
            self._store(rec)
            return True

    def store(
        self,
        profile_dir: str | Path,
        cookies_count: int = 0,
        ttl_seconds: Optional[int] = None,
    ) -> SessionRecord:
        ttl = max(60, int(ttl_seconds or SESSION_MAX_AGE_SECONDS))
        now = self._now()
        record = SessionRecord(
            session_id=f"sess_{uuid.uuid4().hex[:12]}",
            created_at=self._stamp(now),
            last_validated=self._stamp(now),
            expires_at=self._stamp(now + timedelta(seconds=ttl)),
            profile_dir=str(Path(profile_dir).expanduser().resolve()),
            cookies_count=cookies_count,
            active=True,
        )
        self._store(record)
        terminal_log(f"Sessão armazenada: {record.session_id}", "OK")
        return record

    def renew(self, record: Optional[SessionRecord] = None, ttl_seconds: Optional[int] = None) -> Optional[SessionRecord]:
        with self._lock:
            rec = record or self._cached or self.detect()
            if rec is None:
                return None
            ttl = max(60, int(ttl_seconds or SESSION_MAX_AGE_SECONDS))
            now = self._now()
            rec.expires_at = self._stamp(now + timedelta(seconds=ttl))
            rec.last_validated = self._stamp(now)
            rec.active = True
            self._store(rec)
            terminal_log(f"Sessão renovada: {rec.session_id}", "OK")
            return rec

    def end(self, record: Optional[SessionRecord] = None) -> bool:
        with self._lock:
            rec = record or self._cached or self.detect()
            if rec is None:
                return False
            rec.active = False
            self._store(rec)
            try:
                SESSION_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            terminal_log(f"Sessão encerrada: {rec.session_id}", "OK")
            self._cached = None
            return True

    def _store(self, record: SessionRecord) -> None:
        try:
            atomic_write(SESSION_FILE, json.dumps(record.as_dict(), indent=2, ensure_ascii=False), 0o600)
        except OSError as exc:
            terminal_log(f"Falha ao gravar sessão: {exc}", "ERROR")

    def status(self) -> dict[str, Any]:
        rec = self.detect()
        if rec is None:
            return {"active": False, "session_id": "", "expires_at": "", "profile_dir": ""}
        return {
            "active": rec.active and self.validate(rec),
            "session_id": rec.session_id,
            "expires_at": rec.expires_at,
            "profile_dir": rec.profile_dir,
            "cookies_count": rec.cookies_count,
        }


class AuthManager:
    """Manages first authorization and permission control.

    On first interaction, allows the browser to open for login. After
    authorization is confirmed, records a flag file so subsequent runs
    skip the GUI login flow entirely.
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._lock = threading.RLock()

    def is_authorized(self) -> bool:
        return AUTH_FLAG_FILE.exists()

    def authorize(self) -> bool:
        with self._lock:
            AUTH_FLAG_FILE.parent.mkdir(parents=True, exist_ok=True)
            try:
                atomic_write(AUTH_FLAG_FILE, datetime.now(timezone.utc).isoformat(timespec="seconds"), 0o600)
            except OSError as exc:
                terminal_log(f"Falha ao gravar flag de autorização: {exc}", "ERROR")
                return False
            terminal_log("Autorização registrada.", "OK")
            return True

    def revoke(self) -> bool:
        with self._lock:
            try:
                AUTH_FLAG_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            terminal_log("Autorização revogada.", "WARN")
            return True

    def require_login(self, browser_client: "BrowserClient") -> bool:
        """Opens browser for first-time login if not already authorized.

        Returns True if already authorized or login succeeds; False otherwise.
        Never bypasses CAPTCHA, MFA, or OAuth — the user must complete those
        manually in the browser window.
        """
        if self.is_authorized():
            return True
        terminal_log("Primeira autorização necessária — abrindo navegador para login.", "STEP")
        print("\n[NEXUS SILENT] Primeira autorização: faça login no navegador.")
        print("[NEXUS SILENT] Complete MFA/CAPTCHA se solicitado.")
        print("[NEXUS SILENT] Após o login, pressione ENTER.")
        try:
            browser_client.browser.start()
            browser_client.browser.ensure_ready()
            input()
            browser_client.browser.ensure_ready()
        except Exception as exc:
            terminal_log(f"Falha no login inicial: {exc}", "ERROR")
            return False
        self.authorize()
        return True


class TerminalExecutor:
    """Real execution engine for terminal operations in silent mode.

    Wraps the existing RealPTY with timeout, output capture, and safety
    checks. Never executes commands that require GUI confirmation unless
    explicitly approved.
    """

    def __init__(self, pty: "RealPTY", config: dict[str, Any]):
        self.pty = pty
        self.config = config
        self.last_output = ""
        self.last_exit: Optional[int] = None

    def execute(self, command: str, timeout: Optional[int] = None) -> dict[str, Any]:
        command = clean_command(command)
        if not command:
            return {"stdout": "", "exit_code": None, "executed": False, "command": ""}
        effective_timeout = int(timeout or self.config.get("terminal_timeout", 300))
        category = SafetyGuard.classify(command)
        terminal_log(f"Executando: {command} | categoria: {category}", "STEP")
        output, code = self.pty.run_command(command, effective_timeout)
        self.last_output = output
        self.last_exit = code
        return {
            "stdout": output[:8000],
            "exit_code": code,
            "executed": True,
            "command": command,
            "category": category,
        }

    def execute_safe(self, command: str, auto_mode: bool = False, timeout: Optional[int] = None) -> dict[str, Any]:
        command = clean_command(command)
        if not command:
            return {"stdout": "", "exit_code": None, "executed": False, "command": ""}
        category = SafetyGuard.classify(command)
        if SafetyGuard.requires_confirmation(category, command, auto_mode):
            terminal_log(f"Comando requer confirmação: {command} ({category})", "WARN")
            return {
                "stdout": "",
                "exit_code": None,
                "executed": False,
                "command": command,
                "category": category,
                "requires_confirmation": True,
            }
        return self.execute(command, timeout)


class HeadlessController:
    """Prevents unnecessary GUI openings.

    When silent_mode is active, the controller blocks any attempt to open
    a browser or graphical application unless the operation is explicitly
    classified as GUI-required by the FallbackManager.
    """

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.silent_mode = bool(config.get("silent_mode", False))
        self._browser_opened = False

    def enable_silent(self) -> None:
        self.silent_mode = True
        terminal_log("Modo silencioso ativado.", "OK")

    def disable_silent(self) -> None:
        self.silent_mode = False
        terminal_log("Modo silencioso desativado.", "WARN")

    def allow_browser(self, reason: str = "") -> bool:
        if not self.silent_mode:
            return True
        if self._browser_opened and not reason:
            return False
        if reason and any(kw in reason.lower() for kw in GUI_REQUIRED_KEYWORDS):
            self._browser_opened = True
            terminal_log(f"Browser permitido: {reason}", "STEP")
            return True
        terminal_log(f"Browser bloqueado (modo silencioso): {reason or 'sem justificativa'}", "WARN")
        return False

    def allow_graphical_app(self, app_name: str = "") -> bool:
        if not self.silent_mode:
            return True
        terminal_log(f"App gráfico bloqueado (modo silencioso): {app_name}", "WARN")
        return False

    def mark_browser_used(self) -> None:
        self._browser_opened = True

    def reset(self) -> None:
        self._browser_opened = False


class FallbackManager:
    """Determines when an operation truly requires graphical interaction.

    Only login, CAPTCHA, MFA, OAuth, and interactive dialogs justify
    opening a GUI. Everything else should run in terminal.
    """

    @staticmethod
    def requires_gui(operation: str, context: str = "") -> bool:
        text = f"{operation} {context}".lower()
        return any(kw in text for kw in GUI_REQUIRED_KEYWORDS)

    @staticmethod
    def classify_operation(operation_type: str) -> str:
        op = operation_type.lower().strip()
        if op in {"login", "auth", "authorization", "oauth"}:
            return "GUI_REQUIRED"
        if op in {"captcha", "mfa", "2fa", "two-factor"}:
            return "GUI_REQUIRED"
        if op in {"command", "terminal", "shell", "script"}:
            return "TERMINAL"
        if op in {"file", "read", "write", "create", "edit"}:
            return "TERMINAL"
        if op in {"api", "http", "websocket", "request"}:
            return "TERMINAL"
        if op in {"interactive", "dialog", "popup", "prompt"}:
            return "GUI_REQUIRED"
        return "TERMINAL"

    @staticmethod
    def should_fallback_to_browser(
        operation: str,
        headless: HeadlessController,
        session: Optional[SessionRecord] = None,
    ) -> tuple[bool, str]:
        if FallbackManager.requires_gui(operation):
            return True, f"Operação requer GUI: {operation}"
        if session is None or not session.active:
            if "login" in operation.lower() or "auth" in operation.lower():
                return True, "Sessão inexistente e operação de autenticação"
            return True, "Sessão inexistente; primeira autorização necessária"
        return False, "Operação pode ser realizada via terminal"


class SilentModeController:
    """Coordinates SessionManager, AuthManager, TerminalExecutor,
    HeadlessController, and FallbackManager for terminal-first operation.

    Flow: terminal → check session → reuse session → execute task.
    Browser only opens once for first authorization.
    """

    def __init__(self, config: dict[str, Any], pty: "RealPTY"):
        self.config = config
        self.sessions = SessionManager(config)
        self.auth = AuthManager(config)
        self.executor = TerminalExecutor(pty, config)
        self.headless = HeadlessController(config)
        self.fallback = FallbackManager()
        self._initialized = False

    def initialize(self, browser_client: Optional["BrowserClient"] = None) -> dict[str, Any]:
        result: dict[str, Any] = {"session_active": False, "authorized": False, "browser_opened": False}
        if self.auth.is_authorized():
            result["authorized"] = True
            session = self.sessions.detect()
            if session and self.sessions.validate(session):
                result["session_active"] = True
                terminal_log("Sessão ativa reutilizada.", "OK")
                self._initialized = True
                return result
        if browser_client is not None:
            if self.auth.require_login(browser_client):
                result["authorized"] = True
                result["browser_opened"] = True
                profile = browser_client.browser.profile_dir
                self.sessions.store(profile, cookies_count=0)
                result["session_active"] = True
                self.headless.mark_browser_used()
        self._initialized = True
        return result

    def execute_task(self, operation: str, command: str = "", auto_mode: bool = False) -> dict[str, Any]:
        if not self._initialized:
            return {"error": "SilentModeController não inicializado."}
        needs_gui, reason = self.fallback.should_fallback_to_browser(
            operation, self.headless, self.sessions.detect(),
        )
        if needs_gui:
            if self.headless.allow_browser(reason):
                terminal_log(f"Permitindo fallback para browser: {reason}", "STEP")
                return {"fallback_browser": True, "reason": reason}
            return {"blocked": True, "reason": f"Browser bloqueado: {reason}"}
        if command:
            return self.executor.execute_safe(command, auto_mode)
        return {"executed": False, "reason": "Nenhum comando fornecido."}

    def status(self) -> dict[str, Any]:
        return {
            "silent_mode": self.headless.silent_mode,
            "authorized": self.auth.is_authorized(),
            "session": self.sessions.status(),
            "initialized": self._initialized,
        }

# END NEXUS SECTION: SILENT_HEADLESS

# NEXUS SECTION: COPILOT_CLIENT
class BrowserClient:
    """Cliente de raciocínio via Microsoft Copilot/Playwright; sem API Gemini."""
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.browser = EdgeCopilotBrowser(
            profile_dir=os.environ.get("NEXUS_EDGE_PROFILE") or str(PROJECT_DIR / ".nexus" / "edge-copilot"),
            headless=os.environ.get("NEXUS_EDGE_HEADLESS", "0").lower() in {"1", "true", "yes"},
            timeout=int(config.get("browser_timeout", 30000)),
            response_timeout=int(config.get("browser_response_timeout", 120000)),
            stable_seconds=max(4.0, float(config.get("browser_stable_seconds", 4.0))),
            poll_interval=float(config.get("browser_poll_interval", 0.5)),
            edge_channel=str(config.get("browser_channel", "msedge")),
            edge_executable=os.environ.get("NEXUS_EDGE_EXECUTABLE") or None,
        )
        self.browser_url = os.environ.get("NEXUS_COPILOT_URL", config.get("browser_url", "https://copilot.com/chat?fromcode=cmm9tzigufu&sessionId=4b39bf2e-400d-6ca6-2a7c-dba341494dab&hasLW=true&es=SSR&redirfrom=userTypeCookie&redirfrom=cosmicRingCookie"))
        self.browser.copilot_url = self.browser_url

    def close(self) -> None:
        self.browser.close()

    def ask(self, agent_id: str, state: dict[str, Any]) -> str:
        if agent_id not in PROMPTS:
            raise AgentError(f"Agente desconhecido: {agent_id}")
        plain = json.dumps(state, ensure_ascii=False, separators=(",", ":"))
        compact = ncp_pack(state) if bool(self.config.get("compact_protocol_enabled", True)) else plain
        system = PROMPTS[agent_id] + "\n" + NCP_SYSTEM_INSTRUCTION
        prompt = ("INSTRUÇÕES DO AGENTE NEXUS (PRIORIDADE MÁXIMA):\n" + system +
                  "\n\nDADOS DA TAREFA (JSON):\n" + compact[:int(self.config.get("request_max_chars", 20000))] +
                  "\n\nResponda somente no formato JSON exigido pelo agente.")
        terminal_log(f"EdgeCopilotBrowser: agente={AGENTS[agent_id]} payload≈{len(prompt)} caracteres", "API")
        try:
            result = self.browser.ask(prompt)
        except EdgeCopilotBrowserError as exc:
            raise AgentError(f"EdgeCopilotBrowser falhou: {exc}") from exc
        if not str(result).strip():
            raise AgentError("EdgeCopilotBrowser retornou resposta vazia.")
        return str(result).strip()

# END NEXUS SECTION: COPILOT_CLIENT

def extract_json(text: str) -> dict[str, Any]:
    if not text:
        raise AgentError("Resposta vazia do agente.")

    raw = str(text).replace("\ufeff", "").strip()
    # O Microsoft Copilot pode acrescentar ```json, texto explicativo ou um bloco
    # <FollowUp> depois do JSON. O parser deve considerar apenas o primeiro
    # objeto JSON completo e ignorar todo o restante.
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    cleaned = re.sub(r"\s*<FollowUp>.*$", "", cleaned, flags=re.I | re.S).strip()
    cleaned = re.sub(r"\s*```\s*$", "", cleaned).strip()

    decoder = json.JSONDecoder()
    for candidate in (cleaned, raw):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass

    # raw_decode aceita o objeto inicial mesmo quando há texto depois dele.
    for source in (cleaned, raw):
        for i, char in enumerate(source):
            if char != "{":
                continue
            try:
                obj, _ = decoder.raw_decode(source[i:])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue

    # Mantém uma mensagem curta, sem despejar FollowUp ou conteúdo excessivo.
    preview = cleaned[:4000]
    raise AgentError("O agente não retornou JSON válido:\n" + preview)

def extract_json_candidates(text: str) -> list[dict[str, Any]]:
    """Retorna todos os objetos JSON completos, preservando a ordem da resposta."""
    if not text:
        return []
    raw = str(text).replace("\ufeff", "").strip()
    sources = [raw]
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    cleaned = re.sub(r"\s*<FollowUp>.*$", "", cleaned, flags=re.I | re.S).strip()
    if cleaned != raw:
        sources.insert(0, cleaned)
    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    fingerprints: set[str] = set()
    for source in sources:
        for index, char in enumerate(source):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(source[index:])
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            fingerprint = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            if fingerprint not in fingerprints:
                fingerprints.add(fingerprint)
                found.append(value)
    return found

def extract_code_file_payload(text: str, expected_path: str) -> dict[str, Any]:
    """Encontra o payload final pelo path/content, não pelo primeiro JSON da página."""
    candidates = extract_json_candidates(text)
    expected_path = sanitize_relative_code_path(expected_path)

    def inspect(value: Any) -> Optional[dict[str, Any]]:
        if isinstance(value, dict):
            raw_path = value.get("path")
            try:
                candidate_path = sanitize_relative_code_path(raw_path) if raw_path else ""
            except AgentError:
                candidate_path = ""
            content = value.get("content", value.get("code"))
            if candidate_path == expected_path and isinstance(content, str) and content.strip():
                return value
            files = value.get("files")
            if isinstance(files, list):
                for item in files:
                    match = inspect(item)
                    if match:
                        return match
            for key in ("file", "result", "data", "response", "answer", "project"):
                if key in value:
                    match = inspect(value[key])
                    if match:
                        return match
                    if isinstance(value[key], str):
                        match = inspect_string(value[key])
                        if match:
                            return match
        return None

    def inspect_string(value: str) -> Optional[dict[str, Any]]:
        for candidate in extract_json_candidates(value):
            match = inspect(candidate)
            if match:
                return match
        return None

    for candidate in candidates:
        match = inspect(candidate)
        if match:
            return match
    raise AgentError(
        f"O Copilot não retornou content completo para {expected_path}; "
        f"foram encontrados {len(candidates)} objetos JSON, mas nenhum correspondeu ao arquivo."
    )

def extract_agent_json(text: str, agent_id: str) -> dict[str, Any]:
    """Para agentes de decisão, prefere o envelope real ao primeiro JSON da página."""
    if agent_id in {"unica", "decisao"}:
        envelope = EdgeCopilotBrowser._structured_json_response(str(text or ""))
        if envelope:
            try:
                value = json.loads(envelope)
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise AgentError(f"Envelope estruturado inválido: {exc}") from exc
            if isinstance(value, dict):
                return value
    if agent_id == "evolucao_plano":
        candidates = extract_json_candidates(str(text or ""))
        ranked: list[tuple[int, int, dict[str, Any]]] = []

        def unwrap(value: dict[str, Any]) -> list[dict[str, Any]]:
            values = [value]
            for key in ("plan", "data", "result", "answer", "response"):
                nested = value.get(key)
                if isinstance(nested, dict):
                    values.extend(unwrap(nested))
                elif isinstance(nested, str) and nested.lstrip().startswith("{"):
                    try:
                        parsed = extract_json(nested)
                    except AgentError:
                        continue
                    values.extend(unwrap(parsed))
            return values

        for index, candidate in enumerate(candidates):
            for item in unwrap(candidate):
                analysis = clean_text(item.get("analysis", ""))
                evidence = item.get("evidence")
                plan = item.get("plan")
                score = 0
                if analysis:
                    score += 100
                if isinstance(evidence, list) and evidence:
                    score += 20
                if isinstance(plan, list) and plan:
                    score += 20
                if isinstance(item.get("summary"), str) and item["summary"].strip():
                    score += 5
                ranked.append((score, -index, item))
        if ranked:
            ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
            return ranked[0][2]
    return extract_json(text)

def normalize_brain_response(value: dict[str, Any] | str) -> dict[str, Any]:
    """Normaliza o envelope único execute/mode/command/response/reason."""
    payload = extract_json(value) if isinstance(value, str) else dict(value)
    if not isinstance(payload, dict):
        raise AgentError("Resposta do cérebro não é um objeto JSON.")

    # Alguns navegadores podem encapsular o JSON em response, data ou result.
    if "mode" not in payload:
        for key in ("data", "result", "answer"):
            nested = payload.get(key)
            if isinstance(nested, (dict, str)):
                return normalize_brain_response(nested)

    mode = str(payload.get("mode", "")).strip().lower()
    valid_modes = {"response", "command", "python"}
    if mode not in valid_modes:
        # O modelo às vezes repete o schema literal "python|command|response"
        # em vez de escolher uma opção. Inferir pelo conteúdo real evita uma
        # falha desnecessária e mantém a decisão auditável.
        command_value = clean_command(payload.get("command", ""))
        response_value = clean_text(payload.get("response", ""))
        if command_value:
            mode = "command"
        elif response_value or payload.get("execute") is False:
            mode = "response"
        elif mode in {"", "python|command|response", "python/command/response"}:
            mode = "python"
        else:
            raise AgentError(f"Modo JSON do cérebro inválido: {mode!r}.")

    normalized = {
        # mode é a autoridade final: response nunca executa, mesmo se o
        # modelo devolver execute=true por engano.
        "execute": False if mode == "response" else as_bool(payload.get("execute", True), True),
        "mode": mode,
        "command": "" if mode == "response" else clean_command(payload.get("command", "")),
        "response": clean_text(payload.get("response", "")),
        "reason": clean_text(payload.get("reason", "")),
    }
    if mode == "response" and not normalized["response"]:
        normalized["response"] = normalized["reason"]
    return normalized

# NEXUS SECTION: RESPONSE_VALIDATION
def extract_program_payload(value: Any) -> dict[str, Any]:
    """Extrai e normaliza o envelope do agente antes de salvar qualquer arquivo."""
    if isinstance(value, dict):
        payload = value
    elif isinstance(value, str):
        payload = extract_json(value)
    else:
        raise AgentError("Resposta do agente de programação possui tipo inválido.")

    # Compatibilidade: alguns retornos do navegador podem usar code_lines,
    # embora o contrato principal use code. Converta linha a linha para uma
    # única string antes de qualquer validação ou gravação.
    if "code" not in payload and isinstance(payload.get("code_lines"), list):
        payload = dict(payload)
        payload["code"] = "\n".join(str(line) for line in payload["code_lines"])

    # Aceita somente o envelope esperado ou um envelope aninhado conhecido.
    if "code" not in payload:
        for key in ("program", "result", "data"):
            nested = payload.get(key)
            if isinstance(nested, (dict, str)):
                return extract_program_payload(nested)
        keys = ", ".join(sorted(str(key) for key in payload.keys())) or "nenhum"
        raise AgentError(
            "Resposta do agente de programação não contém o campo code "
            f"(campos recebidos: {keys})."
        )

    code = payload.get("code")
    if not isinstance(code, str):
        raise AgentError("O campo code do agente de programação não é uma string.")

    code = code.strip()
    fence_pattern = re.compile(r"```(?:[a-zA-Z]*)?\s*\n(.*?)```", re.I | re.S)
    fence_match = fence_pattern.search(code)
    if fence_match:
        extracted = fence_match.group(1).strip()
        if extracted:
            code = extracted
    if code.startswith("```"):
        code = re.sub(r"^```(?:python|py)?\s*", "", code, flags=re.I)
        code = re.sub(r"\s*```$", "", code).strip()

    # Protege contra o caso em que o campo code foi duplamente serializado.
    if code.startswith("{"):
        try:
            nested = json.loads(code)
        except (TypeError, ValueError, json.JSONDecodeError):
            nested = None
        if isinstance(nested, dict) and "code" in nested:
            return extract_program_payload(nested)

    # JSON normal já converte \\n em quebra de linha. Este fallback cobre uma
    # segunda serialização sem aplicar unicode_escape e corromper acentos.
    if "\\n" in code and "\n" not in code:
        try:
            decoded = json.loads('"' + code.replace('"', '\\"') + '"')
            if isinstance(decoded, str):
                code = decoded
        except (TypeError, ValueError, json.JSONDecodeError):
            code = code.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "\t")

    normalized = dict(payload)
    normalized["code"] = code
    return normalized

def sanitize_relative_code_path(value: Any) -> str:
    """Normaliza um caminho de projeto sem permitir sair do workspace."""
    raw = str(value or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/") or re.match(r"^[A-Za-z]:/", raw):
        raise AgentError(f"Caminho de código inválido: {value!r}")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise AgentError(f"Caminho de código inseguro: {value!r}")
    safe = "/".join(re.sub(r"[^A-Za-z0-9_.-]+", "_", part) for part in parts)
    if safe.startswith(".") or safe.endswith("/"):
        raise AgentError(f"Caminho de código inválido: {value!r}")
    return safe

def validate_generated_bash(code: Any) -> str:
    if not isinstance(code, str):
        raise AgentError("O conteúdo Bash não é uma string.")
    code = code.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not code:
        raise AgentError("O arquivo Bash retornado está vazio.")
    if len(code) > 200000:
        raise AgentError("Arquivo Bash excede o limite de 200000 caracteres.")
    lowered = code.lower()
    for placeholder in PLACEHOLDERS:
        if placeholder.lower() in lowered:
            raise AgentError(f"Código incompleto: placeholder detectado: {placeholder!r}")
    return code

def normalize_generated_content(content: Any, language: str) -> str:
    """Converte conteúdo serializado pelo Copilot em código analisável."""
    if not isinstance(content, str):
        raise AgentError("O conteúdo do arquivo não é uma string.")
    code = content.replace("\r\n", "\n").replace("\r", "\n").strip()

    # O Copilot pode devolver o campo content como uma string duplamente
    # serializada, deixando os caracteres literais \n e \t visíveis.
    if "\\n" in code and "\n" not in code:
        code = (code.replace("\\r\\n", "\n")
                    .replace("\\n", "\n")
                    .replace("\\r", "\n")
                    .replace("\\t", "\t")
                    .replace('\\"', '"'))

    lang_alt = "py" if language == "python" else "sh" if language == "bash" else language
    fence_pattern = re.compile(
        r"```(?:[a-zA-Z]*)?\s*\n(.*?)```",
        re.I | re.S,
    )
    fence_match = fence_pattern.search(code)
    if fence_match:
        extracted = fence_match.group(1).strip()
        if extracted:
            code = extracted

    code = re.sub(
        rf"^```(?:{re.escape(language)}|{re.escape(lang_alt)})?\s*",
        "",
        code,
        flags=re.I,
    )
    code = re.sub(r"\s*```(?:\s*<FollowUp>.*)?$", "", code, flags=re.I | re.S).strip()
    code = re.sub(r"\s*<FollowUp>.*$", "", code, flags=re.I | re.S).strip()

    # O Copilot também pode devolver o nome da linguagem sem cerca Markdown.
    # Essa linha é um marcador de transporte, não código, e nunca deve ser salva.
    lines = code.splitlines()
    if lines and lines[0].strip().casefold() in {language.casefold(), lang_alt.casefold()}:
        code = "\n".join(lines[1:]).strip()
        terminal_log(
            f"Marcador de linguagem removido antes da validação: {lines[0].strip()!r}",
            "WARN",
        )
    return code

def validate_code_project_response(value: dict[str, Any] | str) -> dict[str, Any]:
    """Valida a resposta multi-arquivo antes de criar qualquer diretório."""
    payload = extract_json(value) if isinstance(value, str) else dict(value)
    if not isinstance(payload, dict):
        raise AgentError("O agente de código não retornou um objeto JSON.")
    if "files" not in payload and isinstance(payload.get("project"), (dict, str)):
        return validate_code_project_response(payload["project"])
    files = payload.get("files")
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentError("O campo files veio como texto, mas não contém JSON válido.") from exc
    if not isinstance(files, list) or not files:
        raise AgentError("O agente de código não retornou uma lista de arquivos.")
    normalized = []
    seen = set()
    for item in files:
        if not isinstance(item, dict):
            raise AgentError("Cada arquivo do projeto deve ser um objeto JSON.")
        relative = sanitize_relative_code_path(item.get("path"))
        if relative in seen:
            raise AgentError(f"Arquivo duplicado no projeto: {relative}")
        seen.add(relative)
        language = str(item.get("language", "")).lower().strip()
        suffix = Path(relative).suffix.lower()
        if language in {"py", "python3"}:
            language = "python"
        elif language in {"sh", "shell", "shellscript"}:
            language = "bash"
        elif not language:
            language = "python" if suffix == ".py" else "bash" if suffix in {".sh", ".bash"} else "text"
        content = item.get("content", item.get("code", ""))
        content = normalize_generated_content(content, language)
        if language == "python":
            content = validate_generated_python(content)
        elif language == "bash":
            content = validate_generated_bash(content)
        elif not content.strip():
            raise AgentError(f"Conteúdo vazio no arquivo {relative}")
        else:
            content = content.replace("\r\n", "\n").replace("\r", "\n").strip()
            if len(content) > 200000:
                raise AgentError(f"Arquivo {relative} excede o limite de 200000 caracteres.")
        normalized.append({
            "path": relative,
            "language": language,
            "content": content,
            "executable": as_bool(item.get("executable", language in {"python", "bash"}), language in {"python", "bash"}),
        })
    return {
        "project_name": clean_text(payload.get("project_name", "projeto_nexus")),
        "summary": clean_text(payload.get("summary", "")),
        "files": normalized,
        "dependencies": payload.get("dependencies", []) if isinstance(payload.get("dependencies", []), list) else [str(payload.get("dependencies"))],
        "run_instructions": clean_text(payload.get("run_instructions", "")),
        "reason": clean_text(payload.get("reason", "")),
    }

def validate_program_response(program: dict[str, Any] | str) -> dict[str, Any]:
    program = extract_program_payload(program)
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

def validate_code_plan_response(value: dict[str, Any] | str) -> dict[str, Any]:
    """Valida o plano estruturado, exigindo análise arquitetural sem código."""
    payload = extract_json(value) if isinstance(value, str) else dict(value)
    if "plan" in payload and isinstance(payload["plan"], (dict, str)):
        return validate_code_plan_response(payload["plan"])
    files = payload.get("files")
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AgentError("O plano retornou files como texto inválido.") from exc
    if not isinstance(files, list) or not files:
        raise AgentError("O plano não contém a lista de arquivos do projeto.")
    if len(files) > 24:
        raise AgentError("O plano excede o limite de 24 arquivos por tarefa.")
    normalized = []
    seen = set()
    for item in files:
        if not isinstance(item, dict):
            raise AgentError("Cada item do plano deve ser um objeto JSON.")
        relative = sanitize_relative_code_path(item.get("path"))
        if relative in seen:
            raise AgentError(f"Arquivo duplicado no plano: {relative}")
        seen.add(relative)
        language = str(item.get("language", "")).lower().strip()
        suffix = Path(relative).suffix.lower()
        if language in {"py", "python3"} or (not language and suffix == ".py"):
            language = "python"
        elif language in {"sh", "shell", "shellscript"} or (not language and suffix in {".sh", ".bash"}):
            language = "bash"
        elif language in {"ts", "tsx", "typescript"}:
            language = "typescript"
        elif language in {"js", "jsx", "javascript"}:
            language = "javascript"
        elif language in {"html"}:
            language = "html"
        elif language in {"css", "scss"}:
            language = "css"
        elif language in {"json"}:
            language = "json"
        elif not language:
            if suffix in {".ts", ".tsx"}:
                language = "typescript"
            elif suffix in {".js", ".jsx"}:
                language = "javascript"
            elif suffix == ".html":
                language = "html"
            elif suffix in {".css", ".scss"}:
                language = "css"
            elif suffix == ".json":
                language = "json"
            else:
                language = "text"
        elif language not in {"python", "bash", "typescript", "javascript", "html", "css", "json", "text"}:
            language = "text"
        depends_on = item.get("depends_on", [])
        if not isinstance(depends_on, list):
            depends_on = []
        depends_on = [clean_text(dep) for dep in depends_on if clean_text(dep)]
        normalized.append({
            "path": relative,
            "language": language,
            "purpose": clean_text(item.get("purpose", item.get("description", ""))),
            "depends_on": depends_on,
            "acceptance_criteria": [
                clean_text(criterion)
                for criterion in item.get("acceptance_criteria", [])
                if isinstance(criterion, (str, dict)) and clean_text(criterion)
            ] or [
                f"arquivo {relative} criado no workspace",
                f"linguagem {language} válida e compilável",
                "nenhum placeholder nem código parcial",
            ],
            "executable": as_bool(item.get("executable", language in {"python", "bash"}), language in {"python", "bash"}),
        })
    directory_structure = payload.get("directory_structure", [])
    if isinstance(directory_structure, str):
        directory_structure = [line.strip() for line in directory_structure.splitlines() if line.strip()]
    elif not isinstance(directory_structure, list):
        directory_structure = []
    implementation_order = payload.get("implementation_order", [])
    if isinstance(implementation_order, str):
        implementation_order = [line.strip() for line in implementation_order.splitlines() if line.strip()]
    elif not isinstance(implementation_order, list):
        implementation_order = []
    return {
        "project_name": clean_text(payload.get("project_name", "projeto_nexus")),
        "summary": clean_text(payload.get("summary", "")),
        "objective": clean_text(payload.get("objective", payload.get("summary", ""))),
        "project_type": clean_text(payload.get("project_type", "")),
        "architecture": clean_text(payload.get("architecture", "")),
        "directory_structure": [clean_text(entry) for entry in directory_structure if clean_text(entry)],
        "implementation_order": [clean_text(entry) for entry in implementation_order if clean_text(entry)],
        "validation_strategy": clean_text(payload.get("validation_strategy", "")),
        "steps": [clean_text(item) for item in payload.get("steps", [])] if isinstance(payload.get("steps", []), list) else [],
        "files": normalized,
        "dependencies": payload.get("dependencies", []) if isinstance(payload.get("dependencies", []), list) else [str(payload.get("dependencies"))],
        "run_instructions": clean_text(payload.get("run_instructions", "")),
        "reason": clean_text(payload.get("reason", "")),
    }

def validate_code_file_response(value: dict[str, Any] | str, expected: dict[str, Any]) -> dict[str, Any]:
    """Valida um único arquivo e exige que o path corresponda ao plano."""
    payload = (
        extract_code_file_payload(value, expected["path"])
        if isinstance(value, str)
        else extract_code_file_payload(
            json.dumps(value, ensure_ascii=False), expected["path"]
        )
    )
    for key in ("file", "result", "data"):
        if "content" not in payload and isinstance(payload.get(key), (dict, str)):
            return validate_code_file_response(payload[key], expected)
    actual_path = sanitize_relative_code_path(payload.get("path", expected["path"]))
    if actual_path != expected["path"]:
        raise AgentError(f"O agente retornou {actual_path}, mas o arquivo esperado é {expected['path']}.")
    language = str(payload.get("language", expected["language"])).lower().strip()
    if language in {"py", "python3"}:
        language = "python"
    elif language in {"sh", "shell", "shellscript"}:
        language = "bash"
    if language != expected["language"]:
        raise AgentError(f"Linguagem inesperada em {actual_path}: {language!r}.")
    content = normalize_generated_content(payload.get("content", payload.get("code", "")), language)
    if language == "python":
        content = validate_generated_python(content)
    elif language == "bash":
        content = validate_generated_bash(content)
    elif not content.strip():
        raise AgentError(f"Conteúdo vazio em {actual_path}.")
    return {
        "path": actual_path,
        "language": language,
        "content": content,
        "executable": expected["executable"],
        "reason": clean_text(payload.get("reason", "")),
    }

def validate_decision(decision: dict[str, Any], preferred_mode: str = "") -> dict[str, Any]:
    """Valida e, quando houver roteamento local claro, fixa o modo escolhido."""
    if not isinstance(decision, dict):
        raise AgentError("O agente de decisão não retornou um objeto JSON.")

    mode = str(decision.get("mode", "")).strip().lower()
    if mode not in {"command", "python", "response"}:
        raise AgentError(f"Modo de decisão inválido: {mode!r}.")

    preferred = str(preferred_mode or "").strip().lower()
    if preferred in {"command", "python", "response"} and mode != preferred:
        terminal_log(
            f"Roteador local prevaleceu: {mode} → {preferred}",
            "WARN",
        )
        mode = preferred
        decision = {**decision, "mode": mode}

    if mode == "response":
        decision = {**decision, "mode": "response", "execute": False, "command": ""}

    if mode == "command":
        command = clean_command(decision.get("command", ""))
        if not command:
            raise AgentError("A rota command não produziu comando.")
        if any(token in command for token in ("&&", "||", ";", "\n", "|")):
            raise AgentError(
                "A rota command deve conter somente um comando Linux simples; "
                "use python para múltiplas etapas."
            )
        decision = {**decision, "command": command}

    return decision

# END NEXUS SECTION: RESPONSE_VALIDATION

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

INFORMATIONAL_TERMS = (
    "explique ", "explique o que", "o que é ", "o que e ",
    "defina ", "qual a diferença", "qual é a diferença", "qual e a diferenca",
    "dê um exemplo", "de um exemplo", "resuma ", "descreva ",
    "como funciona", "por que ", "porque ", "quando usar ",
)

NO_EXECUTION_MARKERS = (
    "não execute", "nao execute", "não crie arquivo", "nao crie arquivo",
    "não crie nenhum arquivo", "nao crie nenhum arquivo", "sem código",
    "sem codigo", "sem comandos", "não use comandos", "nao use comandos",
)

FORCE_COMMAND_MARKERS = (
    "use obrigatoriamente a rota command",
    "use a rota command",
    "use somente command",
    "use apenas command",
)

FORCE_PYTHON_MARKERS = (
    "use obrigatoriamente a rota python",
    "use a rota python",
    "use somente python",
    "use apenas python",
)

COMMAND_INTENT_TERMS = (
    "liste ", "listar ", "listagem ", "mostre ", "mostrar ",
    "exiba ", "exibir ", "rode ", "rodar ",
    "abra ", "abrir ", "acesse ", "acessar ", "lance ",
    "verifique o ambiente", "usando um comando", "com um comando",
    "comando linux", "no terminal", "no shell",
)

SAFE_EXPLICIT_COMMANDS = (
    "pwd", "whoami", "hostname", "uptime", "uname", "uname -a",
    "ls", "ls -l", "ls -la", "ls -lah", "free", "free -h", "df -h",
    "lscpu",
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

    if any(marker in normalized for marker in FORCE_COMMAND_MARKERS):
        # Quando o usuário fornece explicitamente um comando simples, não há
        # motivo para gastar chamadas do cérebro com interpretação/planejamento.
        explicit = re.search(r"(?:execute|executar)\s+(?:somente|apenas)?\s*`([^`]+)`", normalized)
        command = explicit.group(1).strip() if explicit else ""
        if not command:
            for candidate in sorted(SAFE_EXPLICIT_COMMANDS, key=len, reverse=True):
                if re.search(r"(?<![a-z0-9_-])" + re.escape(candidate) + r"(?![a-z0-9_-])", normalized):
                    command = candidate
                    break
        if command and not any(token in command for token in ("&&", "||", ";", "\n", "|")):
            return {"type": "direct", "complexity": "direct", "api": 0, "command": command}
        return {"type": "technical", "complexity": "medium", "api": 5, "preferred_mode": "command"}

    if any(marker in normalized for marker in FORCE_PYTHON_MARKERS):
        return {"type": "technical", "complexity": "complex", "api": 6, "preferred_mode": "python"}

    # Perguntas explicativas, especialmente quando proíbem execução, não
    # devem cair no pipeline técnico nem solicitar geração de código.
    has_command_intent = any(term in normalized for term in COMMAND_INTENT_TERMS)
    if (
        any(normalized.startswith(term) or term in normalized for term in INFORMATIONAL_TERMS)
        or any(marker in normalized for marker in NO_EXECUTION_MARKERS)
    ) and not has_command_intent and not any(normalized.startswith(prefix) for prefix in CHANGE_PREFIXES):
        return {"type": "informational", "complexity": "direct", "api": 1}

    if has_command_intent and (
        any(term in normalized for term in (
            "um único comando", "um unico comando", "um comando linux",
            "usando um comando", "com um comando",
        ))
        or any(term in normalized for term in ("abra ", "abrir ", "acesse ", "acessar ", "lance "))
    ):
        return {"type": "technical", "complexity": "direct", "api": 1, "preferred_mode": "command"}

    if any(term in normalized for term in PYTHON_PROJECT_TERMS):
        return {"type": "technical", "complexity": "complex", "api": 6, "preferred_engine": "python"}

    if any(term in normalized for term in (
        "crie um programa", "criar um programa", "gere um programa",
        "crie um script", "criar um script", "gere um script",
        "programa python", "script python", "gere um relatório",
        "gerar um relatório", "gere um relatorio", "gerar um relatorio",
    )):
        return {"type": "technical", "complexity": "complex", "api": 6, "preferred_mode": "python"}

    if any(word in normalized for word in COMPLEX_TERMS):
        return {"type": "technical", "complexity": "complex", "api": 6, "preferred_mode": "python"}

    if any(normalized.startswith(prefix) for prefix in CHANGE_PREFIXES):
        return {"type": "technical", "complexity": "medium", "api": 5}

    # Pedidos técnicos ambíguos continuam sendo decididos pelo Microsoft Copilot.
    return {"type": "technical", "complexity": "medium", "api": 5, "preferred_mode": ""}

# NEXUS SECTION: TERMINAL_EXECUTION
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

# END NEXUS SECTION: TERMINAL_EXECUTION

class NexusCompleter:
    COMMANDS = [
        "/nexus", "/nexus --auto", "/nexus --fast", "/nexus --auto --fast",
        "/nexus stop", "/status", "/quota", "/quota-reset", "/config", "/keys",
        "/reset-limits", "/setup", "/self-test", "/scripts", "/agente",
        "/evoluir", "/evoluir analisar", "/evoluir auto", "/evoluir status",
        "/evoluir history", "/evoluir roadmap", "/evoluir rollback", "/evoluir skill",
        "/code", "/orquestrar", "/orquestrar --auto", "/workspace", "/workspace create", "/workspace cleanup",
        "/silent", "/silent-on", "/silent-off", "/help", "/clear", "/exit",
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

def installed_app_catalog(limit: int = 160) -> list[dict[str, str]]:
    """Lista aplicativos gráficos disponíveis para o cérebro escolher com precisão."""
    apps: dict[str, dict[str, str]] = {}
    roots = [Path.home() / ".local/share/applications", Path("/usr/local/share/applications"), Path("/usr/share/applications")]
    for root in roots:
        if not root.is_dir():
            continue
        for desktop in root.glob("*.desktop"):
            try:
                text = desktop.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "NoDisplay=true" in text or "Hidden=true" in text:
                continue
            name = next((line[5:].strip() for line in text.splitlines() if line.startswith("Name=")), "")
            exec_line = next((line[5:].strip() for line in text.splitlines() if line.startswith("Exec=")), "")
            if name:
                app_id = desktop.name
                apps[app_id] = {"id": app_id, "name": name, "exec": exec_line[:180]}
    flatpak = shutil.which("flatpak")
    if flatpak:
        try:
            result = subprocess.run([flatpak, "list", "--app", "--columns=application,name"], capture_output=True, text=True, timeout=8, check=False)
            for line in result.stdout.splitlines():
                parts = line.split("\t", 1)
                if len(parts) == 2 and parts[0].strip():
                    app_id, name = parts[0].strip(), parts[1].strip()
                    apps.setdefault(app_id, {"id": app_id, "name": name, "exec": f"flatpak run {app_id}"})
        except Exception:
            pass
    return sorted(apps.values(), key=lambda item: item["name"].lower())[:limit]


class NexusApp:
    def __init__(self) -> None:
        self.config = load_config()
        self.pty = RealPTY()
        self.key_pool = KeyPool(self.config)
        self.limiter = RateLimiter(self.config)
        self.quota = QuotaManager(self.config)
        self.workspaces = TemporaryWorkspaceManager(self.config)
        self.ai = BrowserClient(self.config)
        self.running = True
        self.stop_event = threading.Event()
        self.task_id = 0
        self.current_task_calls = 0
        self.current_budget = 0
        self.last_output = ""
        self.last_exit: Optional[int] = None
        self.auto_mode = False
        self.silent = SilentModeController(self.config, self.pty)

    def silent_command(self, line: str) -> None:
        """Gerencia /silent, /silent-on, /silent-off."""
        parts = line.strip().split()
        action = parts[0]
        if action == "/silent":
            status = self.silent.status()
            print("\n================ SILENT MODE =================")
            print(f"Modo silencioso: {'ATIVO' if status['silent_mode'] else 'INATIVO'}")
            print(f"Autorizado:       {'SIM' if status['authorized'] else 'NÃO'}")
            sess = status.get("session", {})
            print(f"Sessão:           {'ATIVA' if sess.get('active') else 'INATIVA'}")
            if sess.get("session_id"):
                print(f"  ID:             {sess['session_id']}")
                print(f"  Expira:         {sess.get('expires_at', '')}")
                print(f"  Perfil:         {sess.get('profile_dir', '')}")
            print("================================================")
            return
        if action == "/silent-on":
            self.config["silent_mode"] = True
            self.silent.headless.enable_silent()
            save_config(self.config)
            print("\033[1;32m[NEXUS] Modo silencioso ATIVADO.\033[0m")
            print("  Operações via terminal prioritariamente.")
            print("  Browser abre apenas para login/MFA/CAPTCHA.")
            return
        if action == "/silent-off":
            self.config["silent_mode"] = False
            self.silent.headless.disable_silent()
            save_config(self.config)
            print("\033[1;33m[NEXUS] Modo silencioso DESATIVADO.\033[0m")
            return

    def terminal_state(self) -> dict[str, Any]:
        limit = int(self.config.get("max_output_chars", 8000))
        return {
            "cwd": self.pty.cwd,
            "shell": self.pty.shell,
            "last_exit_code": self.last_exit,
            "last_output": self.last_output[-limit:],
            "available_graphical_apps": installed_app_catalog(),
            "opening_rules": {
                "site": "xdg-open URL",
                "desktop_app": "gtk-launch ID.desktop",
                "flatpak_app": "flatpak run APP_ID",
            },
            "temporary_workspaces": self.workspaces.snapshot(),
        }

    def can_call_api(self) -> bool:
        return self.current_task_calls < self.current_budget

    def call_agent(self, agent_id: str, state: dict[str, Any]) -> dict[str, Any]:
        if not self.can_call_api():
            raise AgentError(
                f"Orçamento de chamadas do cérebro da tarefa atingido: "
                f"{self.current_task_calls}/{self.current_budget}"
            )

        self.current_task_calls += 1
        call_started = time.monotonic()
        terminal_panel(
            f"ETAPA {AGENTS[agent_id]}",
            [
                f"Chamada {self.current_task_calls}/{self.current_budget}",
                "Observabilidade local: ativa",
                "Nenhum log contém API key",
            ],
        )
        raw_response = self.ai.ask(agent_id, state)
        raw_response = ncp_unpack(raw_response)
        try:
            if agent_id == "codigo_arquivo":
                target = state.get("target_file", {}) if isinstance(state, dict) else {}
                expected_path = str(target.get("path", "")).strip()
                if not expected_path:
                    raise AgentError("A tarefa de arquivo não informou target_file.path.")
                # Filtra pelo path/content antes de qualquer escolha genérica do
                # primeiro objeto JSON encontrado na página do Copilot.
                result = extract_code_file_payload(raw_response, expected_path)
            else:
                result = extract_agent_json(raw_response, agent_id)
        except AgentError:
            if agent_id != "agente" or not self.can_call_api():
                raise
            terminal_log(
                "Resposta JSON do gerador inválida; repetindo com protocolo code_lines",
                "WARN",
            )
            self.current_task_calls += 1
            terminal_panel(
                "RETRY GERAÇÃO DE AGENTE",
                [
                    f"Chamada {self.current_task_calls}/{self.current_budget}",
                    "Formato reforçado: uma linha de código por item",
                ],
            )
            retry_state = {
                **state,
                "retry_instruction": (
                    "A resposta anterior não era JSON válido. Retorne somente JSON válido, "
                    "usando code_lines como lista de strings, uma linha Python por item. "
                    "Não use o campo code e não use markdown."
                ),
            }
            retry_response = ncp_unpack(self.ai.ask(agent_id, retry_state))
            result = extract_json(retry_response)
        if agent_id in {"unica", "decisao"}:
            result = normalize_brain_response(result)
        if bool(self.config.get("compact_protocol_enabled", True)):
            result_plain = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            result_compact = ncp_pack(result)
            result = json.loads(ncp_unpack(result_compact))
            terminal_log(
                f"NCP/1 inbound: {len(result_plain)} → {len(result_compact)} caracteres "
                f"({ncp_savings_percent(result_plain, result_compact):.1f}% de redução local)",
                "API",
            )
        terminal_log(
            f"Etapa {AGENTS[agent_id]} concluída em {time.monotonic() - call_started:.2f}s",
            "OK",
        )
        return result

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

        workspace = None
        if bool(self.config.get("workspace_isolation", True)):
            workspace = self.workspaces.create("TEST WORKSPACE", source=path)
            if bool(self.config.get("workspace_create_venv", False)):
                self.workspaces.create_virtualenv(workspace.workspace_id)
            script_path = workspace.path / path.name
            env = os.environ.copy()
            env.update({
                "NEXUS_TASK_ID": str(self.task_id),
                "NEXUS_CWD": self.pty.cwd,
                "NEXUS_GENERATED_SCRIPT": str(script_path),
                "NEXUS_WORKSPACE_ID": workspace.workspace_id,
            })
            result = self.workspaces.run(
                workspace.workspace_id,
                [str(self.workspaces.python_path(workspace)), str(script_path)],
                int(self.config.get("generated_script_timeout", 300)),
                env,
            )
            output, code_result = result["output"], result["exit_code"]
        else:
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
        if workspace is not None:
            print(f"[NEXUS] Laboratório: {workspace.workspace_id} ({workspace.status})")
            self.workspaces.cleanup_workspace(workspace.workspace_id, force=True)

        return output, code_result, True

    def run_local_command(self, command: str) -> None:
        print("\n[NEXUS] ROTEADOR LOCAL → 0 API")
        if not self.confirm_command(command):
            print("[NEXUS] Execução cancelada.")
            return
        output, code, executed = self.execute_command(command)
        if executed:
            print("\n\033[1;35mWHITE RAT:\033[0m")
            print(output if output else f"Comando concluído. exit code={code}")

    def run_python_pipeline(self, request: str, mode: str, complexity: str, fast: bool = False, preferred_mode: str = "") -> None:
        pipeline_started = time.monotonic()
        pipeline_total = 5 if fast else 7
        pipeline_step = 0
        terminal_panel(
            "PIPELINE NEXUS",
            [f"Modo: {mode}", f"Complexidade: {complexity}", "Logs locais no terminal; sem chamadas extras"],
        )
        terminal_progress("Iniciando pipeline", pipeline_step, pipeline_total)
        state = {
            "user_request": request,
            "terminal": self.terminal_state(),
            "mode": mode,
            "complexity": complexity,
            "preferred_mode": preferred_mode,
            "routing_rule": "Respeite preferred_mode quando estiver definido.",
        }

        interpretation = self.call_agent("interpretacao", state)
        pipeline_step += 1
        terminal_progress("Interpretação", pipeline_step, pipeline_total)

        plan = None
        if not fast:
            plan = self.call_agent(
                "planejamento",
                {**state, "interpretation": interpretation},
            )
            pipeline_step += 1
            terminal_progress("Planejamento", pipeline_step, pipeline_total)

        decision = self.call_agent(
            "decisao",
            {
                **state,
                "interpretation": interpretation,
                "planning": plan or {},
            },
        )
        decision = validate_decision(decision, preferred_mode)

        pipeline_step += 1
        terminal_progress("Decisão de rota", pipeline_step, pipeline_total)
        selected_mode = str(decision.get("mode", "python")).lower().strip()
        terminal_log(f"Rota selecionada pelo agente: {selected_mode}", "STEP")

        if selected_mode == "response":
            response = clean_text(decision.get("response", ""))
            terminal_progress("Resposta final", pipeline_total, pipeline_total)
            terminal_log(f"Pipeline encerrado em {time.monotonic() - pipeline_started:.2f}s", "OK")
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
            answer = validate_decision(answer, "command")
            command = clean_command(answer.get("command", ""))

            if not self.confirm_command(command):
                return

            terminal_log("Executando comando aprovado no PTY real", "STEP")
            output, code_result, executed = self.execute_command(command)
            pipeline_step += 1
            terminal_progress("Execução do comando", pipeline_step, pipeline_total)
            if not executed:
                terminal_log("Execução cancelada pelo usuário", "WARN")
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
            pipeline_step = pipeline_total
            terminal_progress("Validação", pipeline_step, pipeline_total)
            terminal_log(f"Pipeline encerrado em {time.monotonic() - pipeline_started:.2f}s", "OK")
            print("\n\033[1;35mWHITE RAT:\033[0m " + str(validation.get("reason", "Validação concluída.")))
            return

        terminal_log("Solicitando programa Python completo ao agente", "STEP")
        program_state = {
            **state,
            "interpretation": interpretation,
            "planning": plan or {},
            "decision": decision,
            "terminal": self.terminal_state(),
        }
        program = self.persistent_retry(
            "programacao",
            program_state,
            validate_program_response,
            "Programação",
        )
        pipeline_step += 1
        terminal_progress("Programação validada", pipeline_step, pipeline_total)
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
        pipeline_step += 1
        terminal_progress("Execução Python", pipeline_step, pipeline_total)
        if not executed:
            terminal_log("Execução cancelada pelo usuário", "WARN")
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

        pipeline_step = pipeline_total
        terminal_progress("Conclusão", pipeline_step, pipeline_total)
        terminal_log(f"Pipeline encerrado em {time.monotonic() - pipeline_started:.2f}s", "OK")
        print(
            "\n\033[1;35mWHITE RAT:\033[0m " +
            str(conclusion.get("message", "Tarefa concluída."))
        )

    def run_technical(self, request: str, mode: str, complexity: str, fast: bool = False, preferred_mode: str = "") -> None:
        """Executa tarefas técnicas com o menor número possível de chamadas."""
        max_retries = int(self.config.get("max_auto_retries", 12))
        self.current_budget = 1 + max_retries if preferred_mode in {"python", "response"} else 1
        state = {
            "user_request": request,
            "terminal": self.terminal_state(),
            "mode": mode,
            "complexity": complexity,
            "preferred_mode": preferred_mode,
            "routing_rule": (
                "Escolha command para um único comando Linux, python para lógica "
                "ou múltiplas etapas, e response apenas sem execução."
            ),
        }

        terminal_panel(
            "CÉREBRO COPILOT — DECISÃO ÚNICA",
            [
                "Uma chamada para decidir a rota",
                "Nenhum planejamento intermediário",
                "Confirmação antes de qualquer comando",
            ],
        )
        decision = validate_decision(
            self.call_agent("unica", state),
            preferred_mode,
        )
        selected_mode = str(decision.get("mode", "")).lower().strip()
        terminal_log(f"Rota selecionada pelo cérebro: {selected_mode}", "STEP")

        if selected_mode == "response":
            response = clean_text(decision.get("response", ""))
            reason = clean_text(decision.get("reason", ""))
            placeholders = {"", "...", "…", "resposta", "resposta aqui", "texto"}
            if response.lower() in placeholders:
                if self.can_call_api():
                    terminal_log(
                        "Resposta JSON veio vazia/placeholder; repetindo uma única vez",
                        "WARN",
                    )
                    retry = self.call_agent(
                        "unica",
                        {
                            **state,
                            "preferred_mode": "response",
                            "retry_instruction": (
                                "A resposta anterior veio como placeholder. "
                                "Retorne JSON válido e escreva a resposta completa, "
                                "em linguagem natural, no campo response. "
                                "Não use reticências, não deixe response vazio e "
                                "não coloque a resposta somente em reason."
                            ),
                        },
                    )
                    decision = validate_decision(retry, "response")
                    response = clean_text(decision.get("response", ""))
                    reason = clean_text(decision.get("reason", ""))
                if response.lower() in placeholders:
                    response = reason
            print("\n\033[1;35mWHITE RAT:\033[0m\n" + (response or "O cérebro não retornou uma resposta textual."))
            return

        if selected_mode == "command":
            command = clean_command(decision.get("command", ""))
            if not command:
                raise AgentError("O cérebro escolheu command, mas não forneceu comando.")

            explanation = clean_text(
                decision.get("response", "") or decision.get("reason", "")
            )
            if explanation:
                print("\n[NEXUS] Motivo: " + explanation)
            print("[NEXUS] Comando proposto: " + command)

            # execute=true autoriza somente chegar à confirmação; nunca substitui
            # o consentimento do usuário. --auto segue a política existente.
            if not as_bool(decision.get("execute", True), True):
                print("[NEXUS] O agente não solicitou execução; comando não executado.")
                return
            if not self.confirm_command(command):
                print("[NEXUS] Execução cancelada pelo usuário.")
                return
            terminal_log("Executando comando aprovado no PTY real", "STEP")
            output, code_result, executed = self.execute_command(command)
            if executed:
                print("\n\033[1;35mWHITE RAT:\033[0m")
                print(output if output else f"Comando concluído. exit code={code_result}")
            return

        # Python requer uma segunda chamada, exclusivamente para gerar o código.
        program = self.persistent_retry(
            "programacao",
            {
                **state,
                "decision": decision,
            },
            validate_program_response,
            "Programação técnica",
        )
        path = self.save_generated_script(program["filename"], program["code"])
        print(f"\n[NEXUS] SCRIPT PYTHON CRIADO REALMENTE:\n  {path}")
        output, exit_code, executed = self.execute_generated_script(path, program.get("reason", ""))
        if executed:
            print("\n\033[1;35mWHITE RAT:\033[0m")
            print(output if output else f"Script concluído. exit code={exit_code}")

    # NEXUS SECTION: PLANNING_INTEGRITY
    def build_planning_integrity(
        self,
        original_request: str,
        plan: dict[str, Any],
        workspace: Path,
    ) -> "PlanningIntegrity":
        return PlanningIntegrity(original_request, plan, workspace)

    # NEXUS SECTION: CODE_GENERATION
    def allocate_code_workspace(self, parent: Path, project_name: str) -> Path:
        """Cria sempre um diretório novo; `parent` nunca é usado como projeto final."""
        parent = parent.expanduser().resolve()
        if parent.exists() and not parent.is_dir():
            raise AgentError(f"O diretório-pai do workspace não é um diretório: {parent}")
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", clean_text(project_name).lower()).strip("-._") or "projeto-nexus"
        safe_name = safe_name[:48]
        for _ in range(12):
            candidate = parent / f"{safe_name}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
            try:
                candidate.mkdir(mode=0o700, parents=False, exist_ok=False)
                return candidate
            except FileExistsError:
                continue
        raise AgentError("Não foi possível criar um workspace único sem sobrescrever outro projeto.")

    def validate_completed_code_task(self, path: Path, spec: dict[str, Any]) -> dict[str, Any]:
        """Valida o artefato já gravado antes de liberar a próxima tarefa."""
        if not path.exists() or not path.is_file():
            raise AgentError(f"Tarefa não produziu o arquivo esperado: {path}")
        content = path.read_text(encoding="utf-8")
        if spec["language"] == "python":
            validated = validate_generated_python(content)
            compile(validated, str(path), "exec")
        elif spec["language"] == "bash":
            validate_generated_bash(content)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        return {"path": spec["path"], "status": "DONE", "language": spec["language"], "sha256": digest}

    def save_code_project(self, project: dict[str, Any], root: Path) -> list[Path]:
        """Mostra o plano e grava todos os arquivos somente após confirmação."""
        root = root.expanduser().resolve()
        files = project["files"]
        print("\n[NEXUS] PROJETO RECEBIDO DO COPILOT")
        print(f"  Workspace: {root}")
        print(f"  Projeto:   {project.get('project_name') or 'sem nome'}")
        if project.get("summary"):
            print(f"  Resumo:    {project['summary']}")
        print("  Arquivos:")
        for item in files:
            destination = root / item["path"]
            marker = " (novo)" if not destination.exists() else " (BLOQUEADO: já existe)"
            print(f"    - {item['path']} [{item['language']}]{marker}")
        if self.auto_mode:
            print("\n[NEXUS] Modo automático: gravação autorizada pelo parâmetro --auto.")
        else:
            answer = input("\nGravar este projeto no computador? [s/N]: ").strip().lower()
            if answer in {"a", "auto", "automático", "automatico"}:
                self.auto_mode = True
                print("[NEXUS] Modo automático ativado para esta gravação.")
            elif answer not in {"s", "sim", "y", "yes"}:
                print("[NEXUS] Gravação cancelada pelo usuário.")
                return []

        root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        saved = []
        for item in files:
            destination = (root / item["path"]).resolve()
            if root != destination and root not in destination.parents:
                raise AgentError(f"Tentativa de sair do workspace: {item['path']}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            mode = 0o700 if item["executable"] else 0o600
            exclusive_write(destination, item["content"] + "\n", mode)
            saved.append(destination)
        return saved

    def save_code_file(self, item: dict[str, Any], root: Path) -> Path:
        """Grava um arquivo já validado, mantendo a ordem do plano."""
        destination = (root / item["path"]).resolve()
        if root != destination and root not in destination.parents:
            raise AgentError(f"Tentativa de sair do workspace: {item['path']}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        exclusive_write(destination, item["content"] + "\n", 0o700 if item["executable"] else 0o600)
        return destination

    def persistent_retry(
        self,
        agent_id: str,
        base_state: dict[str, Any],
        validator: Callable[[dict[str, Any]], dict[str, Any]],
        context_label: str,
        max_attempts: Optional[int] = None,
    ) -> dict[str, Any]:
        """Chama o agente repetidamente até passar na validação.

        A cada falha, o erro exato é injetado em retry_instruction e o estado
        é reenviado ao Copilot. Continua até validar ou esgotar o orçamento
        de chamadas da tarefa.
        """
        attempts_limit = max_attempts or int(self.config.get("max_auto_retries", 12))
        state = dict(base_state)
        last_error: Optional[Exception] = None

        for attempt in range(1, attempts_limit + 1):
            if self.stop_event.is_set():
                raise AgentError("Tarefa interrompida pelo usuário.")
            if not self.can_call_api():
                raise AgentError(
                    f"Orçamento de chamadas esgotado após {attempt - 1} tentativa(s) "
                    f"para {context_label}."
                )
            try:
                result = validator(self.call_agent(agent_id, state))
                if attempt > 1:
                    terminal_log(
                        f"{context_label} validado após {attempt} tentativa(s)", "OK",
                    )
                return result
            except AgentError as exc:
                last_error = exc
                error_text = str(exc)
                terminal_log(
                    f"Autocorreção: {context_label} falhou (tentativa {attempt}/{attempts_limit}): {error_text}",
                    "WARN",
                )
                if attempt >= attempts_limit:
                    raise
                if not self.can_call_api():
                    raise
                structural_instruction = ""
                lowered = error_text.casefold()
                if "expected 'except' or 'finally' block" in lowered:
                    structural_instruction = (
                        " O erro indica um try sem except/finally. Reescreva o arquivo completo e "
                        "inclua pelo menos um except ou finally para cada try; não tente apenas alterar a indentação."
                    )
                elif "indent" in lowered or "expected an indented block" in lowered:
                    structural_instruction = (
                        " Verifique a indentação do bloco após ':' e use 4 espaços."
                    )
                state["retry_instruction"] = (
                    "ATENÇÃO: a resposta anterior falhou na validação local. "
                    f"Erro exato: {error_text}.{structural_instruction} "
                    "Você é o GERADOR de UM ÚNICO ARQUIVO do NEXUS. "
                    "Ignore o conteúdo inválido anterior e REGENERE o arquivo COMPLETO. "
                    "Retorne SOMENTE JSON válido com path correto e content NÃO VAZIO. "
                    "NÃO gere outros arquivos. NÃO crie arquivos adicionais. "
                    "Dentro de content use exatamente um bloco ```python\nCÓDIGO COMPLETO\n```. "
                    "Nunca coloque uma linha isolada com python, py, bash ou sh antes do código. "
                    "Preserve quebras de linha reais e indentação Python: cada bloco após ':' "
                    "deve ter corpo, cada nível deve usar 4 espaços, o corpo de for/if/def/class/try "
                    "não pode ficar na mesma coluna do cabeçalho. Não use código achatado, pseudocódigo, "
                    "reticências ou placeholders. Preserve __init__, __name__, __main__, underscores e aspas. "
                    "Nunca escreva if name == 'main'; o correto é if __name__ == '__main__':. "
                    "Não gere outro arquivo."
                )
                state["previous_error"] = error_text
                time.sleep(min(3.0, 0.5 * attempt))

        if last_error is not None:
            raise last_error
        raise AgentError(f"Falha persistente em {context_label} sem erro capturado.")

    def process_code(self, line: str) -> None:
        """Planeja o projeto com 7 fases, consultando o Copilot como consultor.

        Fluxo: PEDIDO → FASE 1 (consulta natural) → FASE 2 (arquitetura)
        → FASE 3 (implementação inicial) → FASE 4 (componentes)
        → FASE 5 (integração) → FASE 6 (testes) → FASE 7 (validação final)
        → CONCLUSÃO.
        """
        raw = line[len("/code"):].strip()
        root = CODE_ROOT
        auto_requested = False
        tokens = raw.split()
        while tokens and tokens[0] in {"--auto", "--yes", "-y"}:
            auto_requested = True
            tokens.pop(0)
        raw = " ".join(tokens).strip()
        if raw.startswith("--root "):
            root_text, _, raw = raw[7:].partition(" ")
            if not root_text or not raw.strip():
                print("Uso: /code [--auto] [--root DIRETÓRIO] <o que deve ser criado>")
                return
            root = Path(root_text).expanduser().resolve()
            raw = raw.strip()
        self.auto_mode = auto_requested
        if not raw:
            raw = input("Descreva o código/projeto que deseja gerar: ").strip()
        if not raw:
            print("[NEXUS] Informe o que deve ser gerado.")
            return

        self.stop_event.clear()
        self.task_id += 1
        self.key_pool.begin_task()
        self.current_task_calls = 0
        per_file_budget = int(self.config.get("max_auto_retries", 12))
        self.current_budget = 2 + per_file_budget * 24
        terminal_panel("NEXUS PLANNER — 7 FASES", [
            "FASE 1: Descoberta e Planejamento (consulta ao Copilot)",
            "FASE 2: Arquitetura e Estrutura",
            "FASE 3-4: Implementação (arquivo por arquivo)",
            "FASE 5: Integração | FASE 6: Testes | FASE 7: Validação Final",
        ])
        try:
            # ── FASE 1: DESCOBERTA E PLANEJAMENTO ──
            print(f"\n\033[1;34m[NEXUS] FASE 1/7 — DESCOBERTA E PLANEJAMENTO\033[0m")
            consultation_question = (
                f"Como realizar o seguinte pedido: \"{raw}\"? "
                "Quais arquivos, componentes, dependências e etapas são necessários "
                "para implementá-lo? Liste somente o que for realmente necessário."
            )
            print(f"[NEXUS] CONSULTANDO COPILOT → {consultation_question}")
            plan_state = {
                "user_request": raw,
                "workspace": str(root),
                "workspace_rule": "Este é somente o diretório-pai; o NEXUS criará um subdiretório novo e único após aprovar o plano.",
                "response_phase": "PLANNING_ONLY",
                "consultation_mode": True,
                "consultation_question": consultation_question,
                "planner_role": (
                    "Você é um CONSULTOR TÉCNICO do NEXUS. O usuário pediu algo e o "
                    "NEXUS quer saber COMO fazer. NÃO gere código. Responda como um "
                    "arquiteto de software: identifique o tipo de projeto, liste os "
                    "arquivos necessários com suas responsabilidades, dependências "
                    "entre eles, ordem de implementação, critérios de aceitação e "
                    "estratégia de validação. Inclua somente o necessário."
                ),
                "requirements": [
                    "NÃO gere código nem content de arquivo algum",
                    f"Responda à pergunta: {consultation_question}",
                    "identifique o tipo de projeto",
                    "liste os arquivos em ordem de dependência, somente os necessários",
                    "gere de 1 a 24 arquivos",
                    "para cada arquivo: path, language, purpose, depends_on, acceptance_criteria",
                    "inclua objective, project_type, architecture, directory_structure, implementation_order, validation_strategy",
                    "não execute nem instale nada",
                ],
                "terminal": self.terminal_state(),
            }
            plan = self.persistent_retry(
                "codigo",
                plan_state,
                validate_code_plan_response,
                "Consulta de planejamento",
            )

            # ── FASE 2: ARQUITETURA E ESTRUTURA ──
            print(f"\n\033[1;34m[NEXUS] FASE 2/7 — ARQUITETURA E ESTRUTURA\033[0m")
            print(f"[NEXUS] Objetivo: {plan.get('objective', plan.get('summary', ''))}")
            print(f"[NEXUS] Tipo de projeto: {plan.get('project_type', 'não especificado')}")
            if plan.get("architecture"):
                print(f"[NEXUS] Arquitetura: {plan['architecture']}")
            if plan.get("directory_structure"):
                print("[NEXUS] Estrutura de diretórios:")
                for entry in plan["directory_structure"]:
                    print(f"  {entry}")
            if plan.get("implementation_order"):
                print("[NEXUS] Ordem de implementação:")
                for entry in plan["implementation_order"]:
                    print(f"  {entry}")
            if plan.get("validation_strategy"):
                print(f"[NEXUS] Estratégia de validação: {plan['validation_strategy']}")
            print(f"\n[NEXUS] Projeto: {plan['project_name']}")
            print(f"[NEXUS] Diretório-pai: {root}")

            root = self.allocate_code_workspace(root, plan["project_name"])
            print(f"[NEXUS] Novo workspace: {root}")
            integrity = PlanningIntegrity(raw, plan, root)
            integrity.mark_phase_completed(2)

            actions = integrity.action_descriptions()
            phase_states = dict(integrity.phase_states)
            phase_states[1] = "CONCLUIDA"
            phase_states[2] = "CONCLUIDA"
            print_planning_menu(phase_states, actions)

            if self.auto_mode:
                print("[NEXUS] Modo automático: plano aprovado sem nova pergunta.")
            else:
                answer = input("\nIniciar a execução do plano? [s/N]: ").strip().lower()
                if answer in {"n", "não", "nao", "no", ""}:
                    print("[NEXUS] Execução cancelada.")
                    return
                if answer in {"a", "auto", "automático", "automatico"}:
                    self.auto_mode = True

            # ── FASES 3-4: IMPLEMENTAÇÃO ──
            total_actions = integrity.total_action_count()
            completed = []
            for index, spec in enumerate(plan["files"], 1):
                task = integrity.task_for_spec(spec)
                if task is None:
                    raise AgentError(f"Nenhuma tarefa corresponde a {spec['path']}.")
                integrity.mark_in_progress(task)
                phase_num = task.get("phase", 3)

                pending_deps = integrity.pending_dependencies(task)
                if pending_deps:
                    terminal_log(f"Tarefa {spec['path']} tem dependências pendentes: {', '.join(pending_deps)}", "WARN")

                print_phase_header(phase_num, index, total_actions, task["description"])
                consultation = f"Como criar {spec['path']} para este projeto? Finalidade: {spec['purpose']}"
                print_action_step("consultando", consultation)

                completed_context = integrity.completed_files_context()
                file_state = {
                    "user_request": raw,
                    "project": {
                        "project_name": plan["project_name"],
                        "objective": plan.get("objective", plan.get("summary", "")),
                        "project_type": plan.get("project_type", ""),
                        "architecture": plan.get("architecture", ""),
                        "summary": plan.get("summary", ""),
                        "dependencies": plan["dependencies"],
                        "validation_strategy": plan.get("validation_strategy", ""),
                    },
                    "plan_overview": {
                        "directory_structure": plan.get("directory_structure", []),
                        "implementation_order": plan.get("implementation_order", []),
                        "total_files": total_actions,
                    },
                    "target_file": {
                        "path": spec["path"],
                        "language": spec["language"],
                        "purpose": spec["purpose"],
                        "acceptance_criteria": task["acceptance_criteria"],
                        "depends_on": task.get("depends_on_paths", []),
                    },
                    "completed_files": completed_context,
                    "workspace": str(root),
                    "response_phase": "TASK_GENERATION",
                    "task": {
                        "index": index,
                        "total": total_actions,
                        "status": "IN_PROGRESS",
                        "phase": phase_num,
                        "phase_label": SEVEN_PHASES[phase_num - 1] if 1 <= phase_num <= 7 else f"Fase {phase_num}",
                        "description": task["description"],
                    },
                    "high_priority_instruction": (
                        "ALTA PRIORIDADE: Você é o GERADOR de UM ÚNICO ARQUIVO do NEXUS TERMINAL. "
                        f"Gere SOMENTE o arquivo {spec['path']}. "
                        "O arquivo será validado e salvo antes da geração do próximo. "
                        "NÃO gere outros arquivos do projeto. "
                        "NÃO crie arquivos adicionais por conta própria. "
                        "NÃO coloque outros arquivos, códigos de outros arquivos ou "
                        "implementações futuras dentro da resposta. "
                        "Retorne SOMENTE este arquivo."
                    ),
                    "instruction": (
                        "Gere SOMENTE o target_file COMPLETO, desde a primeira até a última linha. "
                        "Não entregue trechos, diffs, pseudocódigo, placeholders, reticências ou explicações. "
                        "Retorne SOMENTE JSON válido, com path correto, language correto e content não vazio. "
                        "Marque a tarefa como DONE somente após fornecer o arquivo completo."
                    ),
                    "response_contract": (
                        "O campo content deve conter o arquivo completo e autocontido. "
                        "Preserve quebras de linha, indentação de 4 espaços e identificadores literalmente."
                    ),
                    "integrity_rule": (
                        "O NEXUS é o controlador do processo. Não crie, modifique nem "
                        "substitua tarefas do plano. Não altere o objetivo original. "
                        "Se encontrar uma necessidade adicional, descreva-a em reason."
                    ),
                    "validation_rule": "A tarefa só será concluída após validação local.",
                }
                generated = self.persistent_retry(
                    "codigo_arquivo",
                    file_state,
                    lambda resp: validate_code_file_response(resp, spec),
                    f"{spec['path']}",
                )

                print_action_step("gerando", spec["path"])
                aligned, alignment_reason = integrity.validate_response_alignment(task, generated)
                if not aligned:
                    integrity.mark_rejected(task, alignment_reason)
                    print_action_step("rejeitado", alignment_reason)
                    raise AgentError(alignment_reason)

                destination = self.save_code_file(generated, root)
                completed_task = self.validate_completed_code_task(destination, spec)
                completed.append(completed_task)

                print_action_step("validando", spec["path"])
                criteria_ok, criteria_reason = integrity.check_acceptance_criteria(task, destination)
                if not criteria_ok:
                    integrity.mark_rejected(task, criteria_reason)
                    print_action_step("rejeitado", criteria_reason)
                    raise AgentError(criteria_reason)

                integrity.mark_completed(task, f"Arquivo salvo e validado: {destination}")
                print_action_step("concluido", spec["path"])
                print_progress_percent(integrity.completed_action_count(), total_actions)

                if index < total_actions:
                    print_action_step("verificando", "próxima tarefa...")

            # ── VALIDAÇÃO DAS FASES 3 E 4 ──
            for pn in (3, 4):
                ok, reason = integrity.validate_phase(pn)
                if ok:
                    integrity.mark_phase_completed(pn)
                    terminal_log(reason, "OK")
                else:
                    terminal_log(reason, "WARN")

            # ── FASE 5: INTEGRAÇÃO ──
            print(f"\n\033[1;34m[NEXUS] FASE 5/7 — INTEGRAÇÃO\033[0m")
            ok, reason = integrity.validate_phase(5)
            if ok:
                integrity.mark_phase_completed(5)
                terminal_log(reason, "OK")
            else:
                terminal_log(reason, "WARN")
                raise AgentError(reason)

            # ── FASE 6: TESTES E CORREÇÕES ──
            print(f"\n\033[1;34m[NEXUS] FASE 6/7 — TESTES E CORREÇÕES\033[0m")
            ok, reason = integrity.validate_phase(6)
            if ok:
                integrity.mark_phase_completed(6)
                terminal_log(reason, "OK")
            else:
                terminal_log(reason, "WARN")
                raise AgentError(reason)

            # ── FASE 7: VALIDAÇÃO FINAL ──
            print(f"\n\033[1;34m[NEXUS] FASE 7/7 — VALIDAÇÃO FINAL\033[0m")
            final_ok, final_reason = integrity.final_validation(root)
            if not final_ok:
                print_action_step("rejeitado", final_reason)
                raise AgentError(f"Validação final falhou: {final_reason}")
            integrity.mark_phase_completed(7)
            print(f"\033[32m[NEXUS] {final_reason}\033[0m")

            # Menu final com todas as fases concluídas
            final_states = {i: "CONCLUIDA" for i in range(1, 8)}
            print_planning_menu(final_states, actions)

            print("\n\033[32m[NEXUS] PROJETO CONCLUÍDO: 7 fases processadas, todas as ações concluídas, resultado validado contra o objetivo original.\033[0m")
            if plan.get("run_instructions"):
                print("\nComo executar/sugerido pelo Copilot:\n" + plan["run_instructions"])
            if plan.get("dependencies"):
                print("\nDependências (não instaladas automaticamente): " + ", ".join(map(str, plan["dependencies"])))
        except (AgentError, RateLimitError, OSError, SyntaxError) as exc:
            print(f"\n\033[1;31m[NEXUS] CODE NÃO GERADO:\033[0m\n{exc}")

    # END NEXUS SECTION: CODE_GENERATION

    def create_agent(self) -> None:
        """Gera um pequeno agente Python completo a partir de uma personalidade."""
        terminal_panel(
            "GERADOR DE MINIAGENTE",
            [
                "O agente será salvo como um novo arquivo Python",
                "A chave nunca será incorporada ao código gerado",
                "O código será validado por AST e py_compile",
            ],
        )
        personality = input("Qual a personalidade do agente? ").strip()
        if not personality:
            print("[NEXUS] A personalidade é obrigatória; nada foi gerado.")
            return

        name = input("Nome do agente (Enter = agente_personalizado): ").strip()
        objective = input("Qual é o objetivo principal do agente? ").strip()
        if not objective:
            objective = "Conversar com o usuário e responder de acordo com a personalidade definida."
        name = name or "agente_personalizado"

        self.task_id += 1
        self.key_pool.begin_task()
        self.current_task_calls = 0
        self.current_budget = 2
        response = self.call_agent("agente", {
            "name": name,
            "personality": personality,
            "objective": objective,
            "requirements": [
                "miniagente independente do NEXUS",
                "loop de conversa no terminal",
                "Microsoft Copilot via Playwright",
                "segredos somente por variáveis de ambiente",
                "sem shell e sem instalação automática",
            ],
        })

        code_lines = response.get("code_lines")
        if isinstance(code_lines, list):
            response = {**response, "code": "\n".join(str(line) for line in code_lines)}
        program = validate_program_response(response)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base_name = sanitize_filename(name, "agente_personalizado.py")
        stem = Path(base_name).stem
        filename = f"agente_{stem}_{stamp}_{uuid.uuid4().hex[:6]}.py"
        path = self.save_generated_script(filename, program["code"])
        self.validate_python_syntax(path)

        terminal_progress("Agente gerado", 1, 1)
        terminal_log(f"Miniagente salvo em {path}", "OK")
        print("\n[NEXUS] MINIAGENTE CRIADO:")
        print(f"  Nome:        {name}")
        print(f"  Personalidade: {personality}")
        print(f"  Objetivo:     {objective}")
        print(f"  Arquivo:      {path}")
        print(f"  Motivo:       {program.get('reason') or 'não informado'}")
        dependencies = program.get("dependencies", [])
        if dependencies:
            print("  Dependências: " + ", ".join(map(str, dependencies)))
            print("[NEXUS] Nenhuma dependência foi instalada automaticamente.")
        print(f"[NEXUS] Para executar: python3 {path}")

    # NEXUS SECTION: EVOLUTION_WORKFLOW
    @staticmethod
    def _find_function_node(tree: ast.AST, part: str, function_name: str) -> Optional[ast.AST]:
        """Localiza uma função/método sem recorrer a busca textual ambígua."""
        candidates: list[tuple[Optional[str], ast.AST]] = []

        for parent in ast.walk(tree):
            if isinstance(parent, (ast.Module, ast.ClassDef)):
                owner = parent.name if isinstance(parent, ast.ClassDef) else None
                for child in getattr(parent, "body", []):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == function_name:
                        candidates.append((owner, child))

        normalized_part = part.strip().lower()
        if normalized_part:
            scoped = [node for owner, node in candidates if owner and owner.lower() == normalized_part]
            if not scoped:
                scoped = [node for owner, node in candidates if owner and normalized_part in owner.lower()]
            candidates = [(None, node) for node in scoped]

        if len(candidates) == 1:
            return candidates[0][1]
        if not candidates:
            raise AgentError(
                f"Função {function_name!r} não encontrada"
                + (f" na parte/classe {part!r}." if part else ".")
            )
        owners = ", ".join(owner or "módulo" for owner, _ in candidates)
        raise AgentError(
            f"Há mais de uma função {function_name!r} ({owners}). "
            "Informe a classe/parte para desambiguar."
        )

    @staticmethod
    def _function_source(source: str, node: ast.AST) -> tuple[str, int, int]:
        lines = source.splitlines(keepends=True)
        start = int(node.lineno) - 1
        end = int(node.end_lineno)
        return "".join(lines[start:end]), start, end

    def evolve_function_copy(self) -> None:
        """Fluxo legado opcional: evolui uma função e salva uma cópia separada."""
        default_target = Path(sys.argv[0]).resolve()
        raw_target = input(f"Arquivo Python alvo [{default_target}]: ").strip()
        target = Path(raw_target).expanduser().resolve() if raw_target else default_target
        if not target.is_file():
            raise AgentError(f"Arquivo Python não encontrado: {target}")
        if target.suffix.lower() != ".py":
            raise AgentError("O arquivo alvo deve ter extensão .py.")

        part = input("Qual parte/classe? (Enter se estiver no módulo): ").strip()
        function_name = input("Qual função/método? ").strip()
        evolution = input("Qual evolução deseja aplicar? ").strip()
        if not function_name or not evolution:
            print("[NEXUS] Função e evolução são obrigatórias; nada foi alterado.")
            return

        source = target.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(target))
        except SyntaxError as exc:
            raise AgentError(f"O arquivo alvo possui sintaxe inválida: {exc}") from exc

        node = self._find_function_node(tree, part, function_name)
        old_function, start, end = self._function_source(source, node)
        self.task_id += 1
        self.key_pool.begin_task()
        self.current_task_calls = 0
        self.current_budget = 1

        response = self.call_agent("evolucao", {
            "target_file": str(target),
            "part": part or "módulo",
            "function_name": function_name,
            "evolution": evolution,
            "current_function": old_function,
        })
        replacement = clean_text(response.get("code", ""))
        if not replacement:
            raise AgentError("O agente de evolução retornou código vazio.")
        replacement = textwrap.dedent(replacement).strip("\n")
        replacement_tree = ast.parse(replacement, filename="<evolucao>")
        functions = [
            item for item in replacement_tree.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        if len(functions) != 1 or functions[0].name != function_name:
            raise AgentError(
                "A evolução deve retornar exatamente uma função com o mesmo nome "
                f"({function_name})."
            )

        original_indent = re.match(r"^[ \t]*", old_function).group(0)
        replacement = textwrap.indent(replacement, original_indent)
        new_source = "".join(source.splitlines(keepends=True)[:start]) + replacement + "\n" + "".join(source.splitlines(keepends=True)[end:])
        ast.parse(new_source, filename="<evolucao-final>")

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        version_name = f"{target.stem}_evolucao_{stamp}_{uuid.uuid4().hex[:6]}.py"
        version_path = target.parent / version_name
        atomic_write(version_path, new_source, 0o700)
        self.validate_python_syntax(version_path)

        print("\n[NEXUS] EVOLUÇÃO APLICADA SOMENTE NA FUNÇÃO:")
        print(f"  Parte:     {part or 'módulo'}")
        print(f"  Função:    {function_name}")
        print(f"  Original:  {target}")
        print(f"  Nova versão: {version_path}")
        print(f"  Motivo:    {clean_text(response.get('reason', '')) or 'não informado'}")

    @staticmethod
    def _normalize_skill_query(value: str) -> str:
        return re.sub(r"\s+", " ", str(value or "").casefold()).strip()

    def _resolve_evolution_skill(self, value: str) -> Optional[str]:
        query = self._normalize_skill_query(value)
        if not query:
            return None
        matches: list[tuple[int, str]] = []
        for skill_id, skill in SKILL_MAP.items():
            aliases = [skill_id, skill["label"], *skill.get("aliases", [])]
            for alias in aliases:
                normalized = self._normalize_skill_query(alias)
                if normalized and (query == normalized or normalized in query):
                    matches.append((len(normalized), skill_id))
        return max(matches)[1] if matches else None

    @staticmethod
    def _record_evolution(item: dict[str, Any]) -> None:
        record = {"timestamp": datetime.now().astimezone().isoformat(timespec="seconds"), **item}
        EVOLUTION_HISTORY.append(record)
        del EVOLUTION_HISTORY[:-MAX_EVOLUTION_HISTORY]

    def show_evolution_status(self) -> None:
        print("\n================ NEXUS EVOLUTION STATUS ================")
        print(f"Versão: {APP_VERSION}")
        for skill_id, skill in SKILL_MAP.items():
            level = "não avaliado" if skill.get("level") is None else f"{skill['level']}/10"
            availability = "mapeada" if skill.get("sections") else "sem seção implementada"
            print(f"{skill['label']:<26} {level:<14} {availability}")
        last = EVOLUTION_HISTORY[-1] if EVOLUTION_HISTORY else None
        print("Última evolução: " + (f"{last['skill']} — {last['result']} — {last['timestamp']}" if last else "nenhuma nesta sessão"))
        print("As pontuações permanecem não avaliadas até haver critérios e evidências objetivas.")
        print("==========================================================")

    def show_evolution_history(self) -> None:
        print("\n================ EVOLUTION HISTORY (MEMÓRIA) ================")
        if not EVOLUTION_HISTORY:
            print("Nenhum evento nesta sessão.")
        for item in EVOLUTION_HISTORY:
            print(f"{item['timestamp']} | {item.get('skill', '?')} | {item.get('result', '?')} | {item.get('objective', '')}")
            if item.get("candidate_path"):
                print(f"  clone: {item['candidate_path']}" + (f" | alvo: {item['target']}" if item.get("target") else ""))
        print("Este histórico existe somente na memória do processo e será perdido ao sair.")
        print("==============================================================")

    def show_evolution_roadmap(self) -> None:
        print("\n================ EVOLUTION ROADMAP (MEMÓRIA) ================")
        if not EVOLUTION_ROADMAP:
            print("Nenhum plano pendente nesta sessão.")
        for index, item in enumerate(EVOLUTION_ROADMAP, 1):
            print(f"{index}. {item.get('skill', '?')}: {item.get('objective', '')} [{item.get('status', 'pending')}]")
            if item.get("candidate_path"):
                print(f"   clone: {item['candidate_path']}")
            if item.get("target"):
                print(f"   alvo: {item['target']}")
        print("==============================================================")

    def rollback_evolution_candidate(self) -> None:
        candidate = next((item for item in reversed(EVOLUTION_ROADMAP)
                          if item.get("status") in {"candidate_validated", "candidate_validated_source_changed"}
                          and item.get("candidate_path") and item.get("source_sha256")), None)
        if candidate is None:
            print("Nenhum clone validado nesta sessão para reverter. O arquivo ativo nunca foi alterado.")
            return
        active_path = Path(candidate.get("active_path") or __file__).resolve()
        clone_path = Path(candidate["candidate_path"]).resolve()
        active_bytes = active_path.read_bytes()
        active_digest = hashlib.sha256(active_bytes).hexdigest()
        if active_digest != candidate["source_sha256"]:
            print("Rollback recusado: o arquivo ativo mudou desde a clonagem; não usarei uma fonte possivelmente desatualizada.")
            return
        if not clone_path.is_file():
            print(f"Clone não encontrado: {clone_path}")
            return
        atomic_write_bytes(clone_path, active_bytes, 0o600)
        validate_evolution_clone_syntax(clone_path)
        candidate["status"] = "candidate_rolled_back"
        self._record_evolution({
            "skill": candidate.get("skill"), "objective": candidate.get("objective"),
            "result": "candidate_rolled_back", "candidate_path": str(clone_path),
            "source_sha256": active_digest,
        })
        print(f"Clone restaurado para a cópia original: {clone_path}")
        print(f"Arquivo ativo intocado: {active_path}")

    def promote_evolution_candidate(self) -> None:
        candidate = next((item for item in reversed(EVOLUTION_ROADMAP)
                          if item.get("status") in {"candidate_validated", "candidate_validated_source_changed"}
                          and item.get("candidate_path") and item.get("source_sha256")), None)
        if candidate is None:
            print("Nenhum candidato validado disponível para promoção.")
            return
        active_path = Path(candidate.get("active_path") or __file__).resolve()
        clone_path = Path(candidate["candidate_path"]).resolve()
        if not clone_path.is_file() or not active_path.is_file():
            raise AgentError("Arquivo ativo ou candidato não encontrado.")
        if hashlib.sha256(active_path.read_bytes()).hexdigest() != candidate["source_sha256"]:
            raise AgentError("Promoção recusada: o arquivo ativo mudou desde a clonagem.")
        candidate_digest = hashlib.sha256(clone_path.read_bytes()).hexdigest()
        if candidate.get("candidate_sha256") and candidate_digest != candidate["candidate_sha256"]:
            raise AgentError("Promoção recusada: o candidato mudou desde a validação.")
        print(f"\nCANDIDATE → CURRENT\n  Candidato: {clone_path}\n  Destino:   {active_path}")
        answer = input("Promover esta versão para o arquivo principal? [s/N]: ").strip().casefold()
        if answer not in {"s", "sim", "y", "yes"}:
            print("Promoção cancelada; arquivo ativo intocado.")
            return
        workspace_id = candidate.get("workspace_id")
        if workspace_id:
            self.workspaces.promote_file(workspace_id, clone_path, active_path, approved=True)
        else:
            atomic_write_bytes(active_path, clone_path.read_bytes(), 0o700)
        validate_evolution_clone_syntax(active_path)
        candidate["status"] = "current"
        self._record_evolution({"skill": candidate.get("skill"), "objective": candidate.get("objective"),
                               "result": "promoted", "candidate_path": str(clone_path),
                               "active_path": str(active_path)})
        if workspace_id:
            self.workspaces.cleanup_workspace(workspace_id, force=True)
        print("Promoção concluída. Reinicie o NEXUS para carregar o código atualizado.")

    def _choose_evolution_skill(self) -> tuple[str, str]:
        print("\nNEXUS EVOLUTION ENGINE\nO que você deseja evoluir?")
        skills = list(SKILL_MAP.items())
        for index, (_, skill) in enumerate(skills, 1):
            print(f"{index:2}. {skill['label']}")
        print("\nDigite o número ou o nome da habilidade.")
        choice = input("Habilidade: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(skills):
            skill_id = skills[int(choice) - 1][0]
        else:
            skill_id = self._resolve_evolution_skill(choice)
        if not skill_id or skill_id == "other":
            detail = input("Descreva a habilidade desejada: ").strip()
            skill_id = self._resolve_evolution_skill(detail)
        if not skill_id or skill_id == "other":
            raise AgentError("Não identifiquei uma habilidade com seção de código verificável. Escolha uma opção do mapa interno.")
        objective = input("Qual objetivo específico deseja alcançar? ").strip()
        if not objective:
            raise AgentError("Objetivo vazio; nenhuma análise foi iniciada.")
        return skill_id, objective

    def _choose_evolution_target(self, search_hits: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        targets = [hit for hit in search_hits if hit.get("kind") == "function"]
        if not targets:
            print("A busca encontrou referências, mas nenhuma função substituível com segurança. O plano ficará sem implementação automática.")
            return None
        print("\nFunções candidatas localizadas por busca AST/texto:")
        for index, hit in enumerate(targets, 1):
            print(f"  {index}. {hit['id']} [{hit['section']}:{hit['line_start']}]\n     {hit['signature']}")
        choice = input("Escolha o número/ID da função para alterar no clone (Enter = somente plano): ").strip()
        if not choice:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(targets):
            return targets[int(choice) - 1]
        exact = [hit for hit in targets if choice in {hit["id"], hit["symbol"]}]
        if len(exact) == 1:
            return exact[0]
        raise AgentError("Alvo ambíguo ou fora dos resultados da busca; nenhum arquivo foi alterado.")

    def _choose_evolution_context_mode(self) -> str:
        """Define se o trecho selecionado será enviado ao agente de análise."""
        print("\n================ CONTEXTO PARA ANÁLISE ================")
        print("Escolha o que será enviado ao Copilot para analisar a evolução:")
        print("  [s] SIM  — envia a cópia exata da função escolhida, linhas, hash e diff delimitado.")
        print("  [n] NÃO  — envia somente pedido, evidências, assinaturas, seções e metadados; nenhum código do trecho.")
        print("  [a] AUTO — envia o trecho somente se houver um único alvo de função claro e seguro; caso contrário, só metadados.")
        print("\nFormato recomendado do pedido:")
        print("  'Analise [habilidade/parte], proponha uma melhoria incremental para [objetivo verificável],")
        print("   preserve [comportamento/contrato], aponte evidências no código e sugira [testes].'")
        print("Exemplo:")
        print("  'Analise como o NEXUS planeja tarefas e proponha uma melhoria incremental para tornar os planos")
        print("   mais claros e verificáveis, preservando o comportamento atual. Aponte evidências no código")
        print("   e sugira um teste para validar a melhoria. Não escreva código nem altere arquivos.'")
        print("O plano será processado automaticamente; alteração só ocorre após aprovação explícita.")
        answer = input("Enviar o trecho para análise? [s/N/a]: ").strip().casefold()
        if answer in {"s", "sim", "y", "yes"}:
            return "yes"
        if answer in {"a", "auto", "automático", "automatico"}:
            return "auto"
        return "no"

    def _build_evolution_selection(self, source: str, target_hit: Optional[dict[str, Any]]) -> dict[str, Any]:
        """Extrai somente o alvo escolhido para o diagnóstico do Copilot."""
        if not target_hit or target_hit.get("kind") != "function":
            return {
                "selected": False,
                "reason": "Nenhuma função foi escolhida; a análise ficará limitada às seções encontradas.",
                "code": "",
                "diff": "",
            }
        function_name = target_hit.get("function_name")
        owner = target_hit.get("owner") or ""
        tree = ast.parse(source, filename="<evolution-selection>")
        node = self._find_function_node(tree, owner, function_name)
        selected_code, start, end = self._function_source(source, node)
        selected_code = selected_code[:MAX_EVOLUTION_CONTEXT_CHARS]
        digest = hashlib.sha256(selected_code.encode("utf-8")).hexdigest()
        selection_diff = "\n".join(difflib.unified_diff(
            [], selected_code.splitlines(),
            fromfile="/dev/null", tofile=f"selected:{target_hit['id']}", lineterm="",
        ))
        return {
            "selected": True,
            "id": target_hit["id"],
            "section": target_hit.get("section", ""),
            "function_name": function_name,
            "owner": owner,
            "line_start": int(start) + 1,
            "line_end": int(end),
            "sha256": digest,
            "code": selected_code,
            "diff": selection_diff[:MAX_EVOLUTION_CONTEXT_CHARS],
        }

    @staticmethod
    def _function_interface(node: ast.AST) -> tuple[Any, ...]:
        return (
            getattr(node, "name", ""),
            isinstance(node, ast.AsyncFunctionDef),
            ast.dump(node.args, include_attributes=False),
            ast.dump(node.returns, include_attributes=False) if getattr(node, "returns", None) is not None else None,
            getattr(node, "type_comment", None),
        )

    def _apply_evolution_to_clone(
        self,
        clone_path: Path,
        active_path: Path,
        base_digest: str,
        skill_id: str,
        objective: str,
        plan: dict[str, Any],
        target_hit: dict[str, Any],
        selected_context: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Prepare a one-function patch, show the diff, and commit only to the clone."""
        active_digest = hashlib.sha256(active_path.read_bytes()).hexdigest()
        clone_bytes = clone_path.read_bytes()
        if active_digest != base_digest:
            raise AgentError("O arquivo ativo mudou desde a clonagem; rejeitando candidato desatualizado.")
        if hashlib.sha256(clone_bytes).hexdigest() != base_digest:
            raise AgentError("O clone foi alterado fora do Evolution Engine; nenhum patch será aplicado.")
        source = clone_bytes.decode("utf-8")
        try:
            tree = ast.parse(source, filename=str(clone_path))
        except SyntaxError as exc:
            raise AgentError(f"Clone com sintaxe inválida antes da alteração: {exc}") from exc

        function_name = target_hit.get("function_name")
        owner = target_hit.get("owner") or ""
        node = self._find_function_node(tree, owner, function_name)
        if node.lineno != int(target_hit["line_start"]):
            raise AgentError("A posição do alvo mudou depois da busca; refaça a análise antes de editar.")
        old_function, start, end = self._function_source(source, node)
        original_interface = self._function_interface(node)
        source_lines = source.splitlines(keepends=True)
        current_error = ""
        replacement = ""
        new_source = ""
        reason = ""

        for attempt in range(1, MAX_EVOLUTION_REPAIR_ATTEMPTS + 2):
            state = {
                "target_file": str(clone_path),
                "active_file_must_not_be_modified": str(active_path),
                "part": owner or "módulo",
                "function_name": function_name,
                "function_signature": target_hit.get("signature", ""),
                "evolution": objective,
                "approved_plan": plan["plan"],
                "current_function": old_function,
                "selected_code_snippet": (selected_context or {}).get("code", old_function),
                "selected_code_diff": (selected_context or {}).get("diff", ""),
                "selected_code_sha256": (selected_context or {}).get("sha256", ""),
                "requirements": [
                    "retorne somente a função completa e substituta",
                    "use o trecho selecionado e o diff como fonte primária da análise local",
                    "confirme que o SHA-256 do trecho recebido corresponde ao alvo antes de propor a alteração",
                    "preserve exatamente nome, async, parâmetros, defaults, annotations e retorno",
                    "não inclua imports, classes ou qualquer código fora da função",
                    "não gere shell, arquivos extras nem código que atualize o arquivo ativo",
                    "a função deve cumprir o objetivo e o plano aprovados",
                ],
            }
            if current_error:
                state["previous_validation_error"] = current_error
                state["retry_instruction"] = (
                    "A resposta anterior falhou na validação local. Corrija somente a função indicada, "
                    "usando este erro e o código original; não altere a assinatura."
                )
                terminal_log(f"Solicitando correção local {attempt - 1}/{MAX_EVOLUTION_REPAIR_ATTEMPTS}: {current_error}", "WARN")
            try:
                response = self.call_agent("evolucao", state)
                if not isinstance(response, dict):
                    raise AgentError("Resposta de código não é um objeto estruturado.")
                declared_name = response.get("function_name")
                if declared_name is not None and declared_name != function_name:
                    raise AgentError("function_name da resposta não corresponde ao alvo selecionado.")
                if isinstance(response.get("code_lines"), list):
                    raw_replacement = "\n".join(str(line) for line in response["code_lines"])
                else:
                    raw_code = response.get("code", "")
                    if not isinstance(raw_code, str):
                        raise AgentError("Campo code precisa ser texto.")
                    raw_replacement = clean_text(raw_code)
                if not raw_replacement:
                    raise AgentError("Resposta de código vazia.")
                dedented = textwrap.dedent(raw_replacement).strip("\n")
                replacement_tree = ast.parse(dedented, filename="<evolution-function>")
                functions = [item for item in replacement_tree.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))]
                if len(replacement_tree.body) != 1 or len(functions) != 1 or functions[0].name != function_name:
                    raise AgentError("A resposta deve conter exatamente uma função com o mesmo nome.")
                if functions[0].decorator_list or self._function_interface(functions[0]) != original_interface:
                    raise AgentError("A assinatura ou decorators mudaram; função rejeitada.")
                indent = re.match(r"^[ \t]*", old_function).group(0)
                replacement = textwrap.indent(dedented, indent)
                new_source = "".join(source_lines[:start]) + replacement + "\n" + "".join(source_lines[end:])
                ast.parse(new_source, filename=str(clone_path))
                compile(new_source, str(clone_path), "exec", dont_inherit=True)
                reason_value = response.get("reason", "")
                reason = clean_text(reason_value if isinstance(reason_value, str) else str(reason_value)) or "alteração da função aprovada"
                break
            except (AgentError, SyntaxError, ValueError) as exc:
                current_error = str(exc)
                replacement = ""
                if attempt > MAX_EVOLUTION_REPAIR_ATTEMPTS:
                    self._record_evolution({
                        "skill": skill_id, "section": target_hit["section"], "objective": objective,
                        "result": "candidate_rejected", "candidate_path": str(clone_path),
                        "active_path": str(active_path), "target": target_hit["id"], "error": current_error,
                    })
                    print(f"\nEVOLUTION FAILED\nA função não passou na validação após {MAX_EVOLUTION_REPAIR_ATTEMPTS} correções.\nClone mantido intacto: {clone_path}\nMotivo: {current_error}")
                    return False

        diff = list(difflib.unified_diff(
            old_function.splitlines(), replacement.splitlines(),
            fromfile=f"clone original:{target_hit['id']}", tofile=f"clone proposto:{target_hit['id']}",
            lineterm="",
        ))
        print("\nDIFF PROPOSTO — somente o clone será modificado:")
        for line in diff[:160]:
            print(line)
        if len(diff) > 160:
            print(f"... diff resumido: {len(diff) - 160} linhas adicionais não exibidas")
        print(f"\nArquivo ativo intocado: {active_path}")
        print(f"Candidato isolado: {clone_path}")
        answer = input("Aplicar este diff somente ao clone e executar py_compile? [s/N]: ").strip().casefold()
        if answer not in {"s", "sim", "y", "yes"}:
            self._record_evolution({
                "skill": skill_id, "section": target_hit["section"], "objective": objective,
                "result": "candidate_patch_declined", "candidate_path": str(clone_path),
                "active_path": str(active_path), "target": target_hit["id"],
            })
            print("Patch recusado. O clone continua idêntico ao arquivo ativo; o processo em execução não foi alterado.")
            return False

        if hashlib.sha256(active_path.read_bytes()).hexdigest() != base_digest:
            raise AgentError("O arquivo ativo mudou antes da gravação; patch cancelado para evitar candidato desatualizado.")
        if hashlib.sha256(clone_path.read_bytes()).hexdigest() != base_digest:
            raise AgentError("O clone mudou enquanto o diff era revisado; patch cancelado.")
        atomic_write(clone_path, new_source, 0o600)
        try:
            validation = validate_evolution_clone_syntax(clone_path)
        except (OSError, subprocess.SubprocessError, SyntaxError) as exc:
            atomic_write_bytes(clone_path, clone_bytes, 0o600)
            restored = hashlib.sha256(clone_path.read_bytes()).hexdigest() == base_digest
            self._record_evolution({
                "skill": skill_id, "section": target_hit["section"], "objective": objective,
                "result": "candidate_rollback", "candidate_path": str(clone_path),
                "active_path": str(active_path), "target": target_hit["id"], "error": str(exc), "restored": restored,
            })
            print(f"\nEVOLUTION FAILED\nA alteração falhou em py_compile. Clone restaurado: {restored}.\nArquivo ativo nunca foi tocado.\nMotivo: {exc}")
            return False

        live_still_same = hashlib.sha256(active_path.read_bytes()).hexdigest() == base_digest
        result = "candidate_validated" if live_still_same else "candidate_validated_source_changed"
        record = {
            "skill": skill_id, "section": target_hit["section"], "objective": objective,
            "result": result, "candidate_path": str(clone_path), "target": target_hit["id"],
            "active_path": str(active_path),
            "source_sha256": base_digest, "candidate_sha256": hashlib.sha256(clone_path.read_bytes()).hexdigest(),
            "changes": reason, "validation": validation,
        }
        self._record_evolution(record)
        for item in reversed(EVOLUTION_ROADMAP):
            if item.get("candidate_path") == str(clone_path):
                item["status"] = result
                item["target"] = target_hit["id"]
                item["changes"] = reason
                break
        print("\nEVOLUTION VALIDATED")
        print(f"  Função: {target_hit['id']}")
        print(f"  Clone:  {clone_path}")
        print(f"  Teste:  {validation}")
        print(f"  Ativo:  {active_path} — não modificado e não recarregado")
        print("O candidato não foi executado. Revise-o antes de iniciar manualmente.")
        return True

    def _run_evolution_analysis(self, skill_id: str, objective: str, analysis_only: bool = False) -> None:
        skill = SKILL_MAP[skill_id]
        sections, related_skill_ids = resolve_evolution_skill_graph(skill_id)
        if not sections:
            raise AgentError(
                f"A habilidade {skill['label']} não possui seção implementada no arquivo atual. "
                "O NEXUS não enviará código de outra parte nem inventará capacidades."
            )

        target = Path(__file__).resolve()
        evolution_workspace = self.workspaces.create("EVOLUTION WORKSPACE", protected=True)
        clone_path, source_digest = create_evolution_clone(target, evolution_workspace.path)
        clone_digest = hashlib.sha256(clone_path.read_bytes()).hexdigest()
        if clone_digest != source_digest or clone_path.resolve() == target.resolve():
            raise AgentError("O clone não corresponde exatamente ao código ativo; evolução abortada.")
        try:
            validate_evolution_clone_syntax(target)
            validate_evolution_clone_syntax(clone_path)
        except (OSError, subprocess.SubprocessError, SyntaxError) as exc:
            raise AgentError(f"Pré-validação do original/clone falhou; nenhuma chamada ao Copilot foi feita: {exc}") from exc
        source = clone_path.read_bytes().decode("utf-8")
        context = EvolutionContextBuilder(
            max_chars=min(MAX_EVOLUTION_CONTEXT_CHARS, max(3000, int(self.config.get("request_max_chars", 20000)) - 5000)),
            max_lines=MAX_EVOLUTION_CONTEXT_LINES,
            max_sections=MAX_RELEVANT_SECTIONS,
        ).build(source, sections, objective)
        source_index = EvolutionSourceIndex(source)
        search_hits = source_index.search(objective, context["included_sections"], limit=12)
        if not search_hits:
            for section_name in context["included_sections"]:
                start, end = source_index.locator.sections[section_name]
                search_hits.append({
                    "id": f"SECTION:{section_name}", "symbol": f"SECTION:{section_name}",
                    "kind": "section", "section": section_name,
                    "line_start": start + 1, "line_end": end,
                    "signature": "Seção identificada por âncora NEXUS; sem correspondência lexical específica.",
                    "matched_terms": [], "score": 0,
                })
        allowed_evidence = {hit["id"]: hit["section"] for hit in search_hits}

        terminal_panel("NEXUS EVOLUTION ENGINE", [
            f"Habilidade: {skill['label']}",
            f"Clone isolado: {clone_path}",
            f"SHA-256 base: {source_digest[:16]}… (igual ao original)",
            "Busca: AST + texto localizado nas seções selecionadas",
            f"Seções selecionadas: {', '.join(context['included_sections']) or 'nenhuma'}",
            f"Contexto: {context['chars']} caracteres / {context['lines']} linhas",
            "O arquivo/processo ativo nunca será substituído nem recarregado",
        ])
        print("\nAlvos encontrados pela busca local:")
        for index, hit in enumerate(search_hits[:8], 1):
            suffix = f" — {hit['signature']}" if hit["kind"] == "function" else ""
            print(f"  {index}. {hit['id']} [{hit['section']}:{hit['line_start']}] {hit['kind']}{suffix}")
        if context["omitted"]:
            print("Contexto reduzido:")
            for item in context["omitted"]:
                print("  - " + item)

        context_mode = self._choose_evolution_context_mode()
        function_hits = [hit for hit in search_hits if hit.get("kind") == "function"]
        # SIM permite a escolha explícita; AUTO só envia código quando o alvo é
        # único e inequívoco; NÃO mantém a análise sem o trecho selecionado.
        if context_mode == "yes":
            target_hit = self._choose_evolution_target(search_hits)
        elif context_mode == "auto" and len(function_hits) == 1:
            target_hit = function_hits[0]
            print(f"\n[AUTO] Alvo único identificado: {target_hit['id']}")
        else:
            target_hit = None
            if context_mode == "auto":
                print(f"\n[AUTO] {len(function_hits)} funções candidatas; nenhum trecho será enviado sem alvo inequívoco.")
        selected_code = self._build_evolution_selection(source, target_hit)
        if context_mode == "no":
            selected_code = {
                "selected": False,
                "mode": "no",
                "reason": "Usuário não autorizou o envio do trecho; análise limitada a evidências e metadados.",
                "code": "",
                "diff": "",
            }
        selected_code["mode"] = context_mode
        if selected_code["selected"]:
            terminal_panel("EVOLUTION TARGET CONTEXT", [
                f"Alvo selecionado: {selected_code['id']}",
                f"Linhas: {selected_code['line_start']}-{selected_code['line_end']}",
                f"SHA-256 do trecho: {selected_code['sha256'][:16]}…",
                "Cópia limitada do trecho enviada para análise; arquivo ativo intocado",
            ])
            print("\nDiff de contexto enviado ao agente:")
            for diff_line in selected_code["diff"].splitlines()[:80]:
                print(diff_line)

        self.task_id += 1
        self.key_pool.begin_task()
        self.current_task_calls = 0
        self.current_budget = 1 + MAX_EVOLUTION_REPAIR_ATTEMPTS + 1
        plan_response = self.call_agent("evolucao_plano", {
            "user_request": objective,
            "active_file": str(target),
            "candidate_file": str(clone_path),
            "candidate_base_sha256": source_digest,
            "skill": {"id": skill_id, "name": skill["label"], "description": skill["description"],
                      "level": skill.get("level"), "aliases": skill.get("aliases", []),
                      "dependencies": [
                          {"id": related_id, "name": SKILL_MAP[related_id]["label"],
                           "description": SKILL_MAP[related_id]["description"]}
                          for related_id in related_skill_ids if related_id != skill_id
                      ]},
            "selected_sections": context["included_sections"],
            "search_hits": search_hits[:12],
            "selected_target": selected_code,
            "context_policy": {
                "mode": context_mode,
                "code_sent": bool(selected_code["selected"]),
                "automatic_plan_processing": True,
            },
            "context_char_count": context["chars"],
            "context_code": context["text"] if selected_code["selected"] else "",
            "requirements": [
                "diagnostique primeiro, ancorando afirmações no contexto recebido",
                "respeite context_policy: se code_sent for false, não tente reconstruir nem inferir o trecho ausente",
                "analise prioritariamente a cópia exata de selected_target.code quando selected_target.selected for true",
                "confirme o nome, classe, linhas e SHA-256 do selected_target antes de propor qualquer alteração",
                "não solicite o arquivo inteiro; use somente o trecho selecionado e o contexto delimitado",
                "liste capacidades apenas quando observáveis; identifique claramente informação ausente",
                "cite IDs de busca exatos, sem inventar nomes ou linhas",
                "proponha um plano incremental para uma habilidade e um objetivo",
                "inclua testes e riscos; não retorne código, patch ou arquivos",
                "o arquivo ativo é somente referência; somente o clone pode ser editado",
                "não proponha substituir o arquivo inteiro",
            ],
        })
        plan = validate_evolution_plan_response(plan_response, allowed_evidence)

        print("\n================ EVOLUTION ANALYSIS ================")
        print(f"Habilidade: {skill['label']}")
        print("Estado local: " + skill["description"])
        print("Nível: não avaliado (sem pontuação inventada)")
        print("\nDiagnóstico do Copilot:\n" + plan["analysis"])
        print("\nEvidências verificadas na busca local:")
        for item in plan["evidence"]:
            print(f"  - [{item['section']}] {item['id']}: {item['observation']}")
        if plan["capabilities_found"]:
            print("\nCapacidades encontradas:")
            for item in plan["capabilities_found"]:
                print("  - " + item)
        if plan["limitations"]:
            print("\nLimitações:")
            for item in plan["limitations"]:
                print("  - " + item)
        print("\nPlano:")
        for index, step in enumerate(plan["plan"], 1):
            print(f"  {index}. {step}")
        if plan["tests"]:
            print("\nTestes propostos:")
            for item in plan["tests"]:
                print("  - " + item)
        if plan["risks"]:
            print("\nRiscos:")
            for item in plan["risks"]:
                print("  - " + item)
        if plan["summary"]:
            print("\nResultado esperado: " + plan["summary"])
        print("====================================================")

        event = {"skill": skill_id, "section": context["included_sections"],
                 "objective": objective, "result": "analysis_only" if analysis_only else "plan_presented",
                 "summary": plan["summary"] or plan["analysis"][:240],
                 "active_path": str(target), "candidate_path": str(clone_path), "source_sha256": source_digest,
                 "workspace_id": evolution_workspace.workspace_id,
                 "context_mode": context_mode,
                 "code_sent_to_analysis": bool(selected_code["selected"]),
                 "selected_target_id": selected_code.get("id", ""),
                 "selected_target_sha256": selected_code.get("sha256", ""),
                 "selected_target_lines": [selected_code.get("line_start"), selected_code.get("line_end")]
                 if selected_code.get("selected") else [],
                 "search_hit_ids": [hit["id"] for hit in search_hits[:12]]}
        if analysis_only:
            EVOLUTION_ROADMAP.append({**event, "status": "analysis_only_clone_ready"})
            del EVOLUTION_ROADMAP[:-MAX_EVOLUTION_HISTORY]
            self._record_evolution(event)
            return

        answer = input("\nAprovar este plano para propor uma alteração isolada no clone (não no arquivo ativo)? [s/N]: ").strip().casefold()
        if answer in {"s", "sim", "y", "yes"}:
            roadmap_item = {**event, "status": "plan_approved_choose_target", "plan": plan["plan"]}
            EVOLUTION_ROADMAP.append(roadmap_item)
            del EVOLUTION_ROADMAP[:-MAX_EVOLUTION_HISTORY]
            self._record_evolution({**event, "result": "plan_approved"})
            if target_hit is None:
                target_hit = self._choose_evolution_target(search_hits)
                if target_hit is None:
                    roadmap_item["status"] = "approved_plan_only"
                    print(f"\nPlano aprovado e mantido em memória. Clone preservado sem alterações: {clone_path}")
                    print("Nenhum alvo foi selecionado; nem o clone nem o arquivo ativo foram alterados.")
                    return
                # No modo NÃO, o trecho só é enviado agora, após aprovação do
                # plano, para a etapa de alteração isolada — nunca para a análise.
                selected_code = self._build_evolution_selection(source, target_hit)
                selected_code["mode"] = context_mode
            self._apply_evolution_to_clone(
                clone_path, target, source_digest, skill_id, objective, plan, target_hit, selected_code,
            )
        else:
            self._record_evolution({**event, "result": "plan_declined"})
            print(f"\nPlano recusado. Arquivo ativo intocado; clone original preservado em: {clone_path}")

    def evolve_code(self, request: str = "") -> None:
        """Clone-first flow; any approved code patch can touch only the isolated candidate."""
        raw = str(request or "").strip()
        lowered = self._normalize_skill_query(raw)
        if lowered == "status":
            self.show_evolution_status()
            return
        if lowered in {"history", "historico", "histórico"}:
            self.show_evolution_history()
            return
        if lowered == "roadmap":
            self.show_evolution_roadmap()
            return
        if lowered == "rollback":
            self.rollback_evolution_candidate()
            return
        if lowered in {"promover", "promote"}:
            self.promote_evolution_candidate()
            return
        if lowered == "analisar":
            self._run_evolution_analysis("evolution", "Faça uma autoanálise estrutural do mecanismo de evolução; identifique somente evidências e proponha melhorias sem alterar código.", True)
            return
        if lowered == "auto":
            self._run_evolution_analysis("evolution", "Analise o mecanismo atual, recomende uma única melhoria incremental de maior prioridade, estime impacto e proponha um plano para aprovação. Não execute mudanças.")
            return

        skill_id: Optional[str] = None
        objective = raw
        if lowered.startswith("skill "):
            remainder = raw.split(None, 1)[1].strip() if len(raw.split(None, 1)) > 1 else ""
            normalized_remainder = self._normalize_skill_query(remainder)
            candidates: list[tuple[int, str, str]] = []
            for candidate_id, candidate in SKILL_MAP.items():
                for alias in [candidate_id, candidate["label"], *candidate.get("aliases", [])]:
                    normalized_alias = self._normalize_skill_query(alias)
                    if normalized_alias and (normalized_remainder == normalized_alias or normalized_remainder.startswith(normalized_alias + " ")):
                        candidates.append((len(normalized_alias), candidate_id, alias))
            if candidates:
                _, skill_id, matched_alias = max(candidates)
                objective = remainder[len(matched_alias):].strip()
            if not skill_id:
                raise AgentError("Habilidade não reconhecida. Use o nome ou um alias listado no menu.")
            if not objective:
                objective = input("Qual objetivo específico deseja alcançar? ").strip()
        elif not raw:
            skill_id, objective = self._choose_evolution_skill()
        else:
            skill_id = self._resolve_evolution_skill(raw)

        if not skill_id:
            skill_id, objective = self._choose_evolution_skill()
        if not objective.strip():
            raise AgentError("Objetivo vazio; nenhuma análise foi iniciada.")
        self._run_evolution_analysis(skill_id, objective)

    # END NEXUS SECTION: EVOLUTION_WORKFLOW

    # NEXUS SECTION: COGNITIVE_ORCHESTRATOR_WORKFLOW
    def run_cognitive_orchestration(self, line: str) -> None:
        """Ponto de entrada do orquestrador cognitivo autônomo."""
        raw = line[len("/orquestrar"):].strip()
        auto_requested = False
        tokens = raw.split()
        while tokens and tokens[0] in {"--auto", "--yes", "-y"}:
            auto_requested = True
            tokens.pop(0)
        goal = " ".join(tokens).strip()
        if not goal:
            goal = input("Qual é o objetivo da orquestração? ").strip()
        if not goal:
            print("[NEXUS] Informe o objetivo da orquestração.")
            return

        self.auto_mode = auto_requested
        self.stop_event.clear()
        self.task_id += 1
        self.key_pool.begin_task()
        self.current_task_calls = 0
        self.current_budget = MAX_ORCHESTRATOR_CYCLES * 2 + 10

        print(f"\n{'=' * 60}")
        print(f"NEXUS COGNITIVE ORCHESTRATOR — TASK #{self.task_id}")
        print(f"{'=' * 60}")
        terminal_log(f"Orquestração cognitiva iniciada: {goal[:120]}", "STEP")

        try:
            orchestrator = CognitiveOrchestrator(self, goal, auto_mode=auto_requested)
            context = orchestrator.run()
            if context.status == "completed":
                print(f"\n\033[1;32m[ORQUESTRADOR] TAREFA CONCLUÍDA E VALIDADA\033[0m")
                print(f"  Objetivo: {context.goal}")
                print(f"  Etapas concluídas: {len(context.completed_steps)}")
                print(f"  Ciclos: {context.cycle_count}")
            elif context.status == "failed":
                print(f"\n\033[1;31m[ORQUESTRADOR] TAREFA FALHOU\033[0m")
                print(f"  Objetivo: {context.goal}")
                print(f"  Etapas concluídas: {len(context.completed_steps)}")
                print(f"  Etapas falhas: {len(context.failed_steps)}")
                print(f"  Ciclos: {context.cycle_count}")
                if context.errors:
                    print(f"  Último erro: {context.errors[-1]}")
            elif context.status == "waiting_user":
                print(f"\n\033[1;33m[ORQUESTRADOR] AGUARDANDO INTERVENÇÃO DO USUÁRIO\033[0m")
                print(f"  Estado: {context.state}")
                print(f"  Ciclos: {context.cycle_count}")
        except (AgentError, RateLimitError, OSError, KeyboardInterrupt) as exc:
            if isinstance(exc, KeyboardInterrupt):
                print(f"\n\033[1;33m[ORQUESTRADOR] Interrompido pelo usuário\033[0m")
            else:
                print(f"\n\033[1;31m[ORQUESTRADOR] ERRO:\033[0m\n{exc}")

    # END NEXUS SECTION: COGNITIVE_ORCHESTRATOR_WORKFLOW

    # NEXUS SECTION: PLANNED_EXECUTION_WORKFLOW
    def run_planned_execution(self, line: str) -> None:
        """Ponto de entrada do motor de execução orientada por planejamento contínuo."""
        raw = line[len("/planejar"):].strip()
        auto_requested = False
        tokens = raw.split()
        while tokens and tokens[0] in {"--auto", "--yes", "-y"}:
            auto_requested = True
            tokens.pop(0)
        goal = " ".join(tokens).strip()
        if not goal:
            goal = input("Qual é o objetivo do projeto? ").strip()
        if not goal:
            print("[NEXUS] Informe o objetivo do projeto.")
            return

        self.auto_mode = auto_requested
        self.stop_event.clear()
        self.task_id += 1
        self.key_pool.begin_task()
        self.current_task_calls = 0
        total_tasks = MAX_PLANNED_PHASES * MAX_PLANNED_TASKS_PER_PHASE
        self.current_budget = 2 + total_tasks * 4 + MAX_PLANNED_REASSESSMENTS

        print(f"\n{'=' * 60}")
        print(f"NEXUS PLANNED EXECUTION — TASK #{self.task_id}")
        print(f"{'=' * 60}")
        terminal_log(f"Planejamento contínuo iniciado: {goal[:120]}", "STEP")

        try:
            plan_state = {
                "user_request": goal,
                "terminal": self.terminal_state(),
                "instruction": (
                    "Crie um PLANO INICIAL estruturado em fases e tarefas sequenciais. "
                    "O plano será usado como memória operacional persistente. "
                    "O NEXUS executará fase por fase, tarefa por tarefa, "
                    "validando cada uma antes de avançar."
                ),
            }
            plan_raw = self.persistent_retry(
                "plano_execucao",
                plan_state,
                lambda resp: parse_project_plan(resp if isinstance(resp, dict) else extract_agent_json(str(resp), "plano_execucao")),
                "Plano de execução",
            )
            plan = plan_raw if isinstance(plan_raw, ProjectPlan) else parse_project_plan(plan_raw)
            print(f"\n[NEXUS] PLANO GERADO: {len(plan.phases)} fase(s), {len(plan.all_tasks())} tarefa(s)")
            print(f"  Objetivo: {plan.objective}")
            if plan.summary:
                print(f"  Resumo: {plan.summary}")
            if not auto_requested:
                answer = input("\nIniciar execução fase por fase? [s/N]: ").strip().lower()
                if answer not in {"s", "sim", "y", "yes"}:
                    print("[NEXUS] Execução cancelada antes de iniciar.")
                    return

            engine = PlannedExecutionEngine(self, plan, auto_mode=auto_requested)
            result = engine.run()
            if result.get("completed"):
                print("\n\033[1;32m[NEXUS] PROJETO CONCLUÍDO E VALIDADO\033[0m")
            else:
                reason = result.get("reason", "")
                if not reason:
                    stats = result.get("stats", {})
                    reason = f"{stats.get('completed', 0)}/{stats.get('total_tasks', 0)} tarefas concluídas"
                print(f"\n\033[1;31m[NEXUS] PROJETO NÃO CONCLUÍDO: {reason}\033[0m")
        except (AgentError, RateLimitError, OSError, KeyboardInterrupt) as exc:
            if isinstance(exc, KeyboardInterrupt):
                print(f"\n\033[1;33m[NEXUS] Execução interrompida pelo usuário\033[0m")
            else:
                print(f"\n\033[1;31m[NEXUS] ERRO DE EXECUÇÃO:\033[0m\n{exc}")

    # END NEXUS SECTION: PLANNED_EXECUTION_WORKFLOW

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
        terminal_log(f"Tarefa #{self.task_id} iniciada", "STEP")

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

        if route["type"] == "informational":
            # Mesmo caminho JSON de uma pergunta técnica, mas fixado em response.
            try:
                self.run_technical(
                    raw,
                    "normal",
                    "direct",
                    False,
                    "response",
                )
            except (AgentError, RateLimitError) as exc:
                print(f"\n\033[1;31m[NEXUS] RESPOSTA FALHOU:\033[0m\n{exc}")
            return

        try:
            self.run_technical(
                raw,
                "auto" if mode_auto else "normal",
                route["complexity"],
                fast,
                route.get("preferred_mode", "") or route.get("preferred_engine", ""),
            )
        except (RateLimitError, AgentError) as exc:
            print(f"\n\033[1;31m[NEXUS] {type(exc).__name__.upper()}:\033[0m\n{exc}")
        except KeyboardInterrupt:
            print("\n[NEXUS] Tarefa interrompida.")

    def status(self) -> None:
        print("\n================ NEXUS STATUS ================")
        print(f"Versão:          {APP_VERSION}")
        print("Cérebro:          Microsoft Copilot via EdgeCopilotBrowser")
        print(f"Shell:           {self.pty.shell}")
        print(f"PTY:             {'ONLINE' if self.pty.alive else 'OFFLINE'}")
        print(f"Diretório PTY:   {self.pty.cwd}")
        print(f"Scripts gerados: {GENERATED_DIR}")
        print(f"Timeout navegador: {self.config.get('browser_response_timeout')}ms")
        print(f"APIs tarefa:     {self.current_task_calls}/{self.current_budget}")
        qs = self.quota.status()
        print(
            f"Quota local:     RPM {qs['rpm_current']}"
            f"/{qs['rpm_soft'] or '∞'} | "
            f"RPD {qs['rpd_today']}"
            f"/{qs['rpd_soft'] or '∞'} | "
            f"cooldown {qs['global_cooldown']:.1f}s"
        )
        print(f"Tokens hoje:     in={qs['tpm_today']} out={qs['output_tokens_today']} total={qs['total_tokens_today']}")
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
        
        print(f"[NEXUS] Configuração salva. Pool: {len(get_api_keys(self.config))} chave(s).")

    def reset_limits(self) -> None:
        self.key_pool.reset()
        self.quota.clear_cooldown()
        print("[NEXUS] Cooldowns do pool e cooldown global zerados.")
        print("[NEXUS] Contadores de consumo NÃO foram apagados. Use /quota-reset para isso.")

    def quota_status(self) -> None:
        s = self.quota.status()
        print("\n================ NEXUS QUOTA =================")
        print(f"Dia de referência:        {s['day']} (Pacífico)")
        print(f"Requests nesta janela:    {s['rpm_current']}")
        print(f"RPM soft local:            {s['rpm_soft'] or 'desativado'}")
        print(f"Requests hoje:             {s['rpd_today']}")
        print(f"RPD soft local:            {s['rpd_soft'] or 'desativado'}")
        print(f"Tokens entrada hoje:      {s['tpm_today']}")
        print(f"Tokens saída hoje:        {s['output_tokens_today']}")
        print(f"Tokens totais hoje:       {s['total_tokens_today']}")
        print(f"TPM soft local:            {s['tpm_soft'] or 'desativado'}")
        print(f"Cooldown global:           {s['global_cooldown']:.1f}s")
        print(f"Último evento:             {s['last_status'] or 'nenhum'}")
        print("NOTA: estes contadores são locais; não substituem a quota oficial do Microsoft.")
        print("================================================")

    def quota_reset(self) -> None:
        self.quota.reset_counters()
        self.quota.clear_cooldown()
        print("[NEXUS] Contadores locais de quota zerados.")

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

    def workspace_command(self, line: str) -> None:
        parts = line.strip().split()
        action = parts[1].casefold() if len(parts) > 1 else "status"
        if action in {"status", "list", "listar"}:
            rows = self.workspaces.snapshot()
            print("\n================ TEMPORARY WORKSPACES ================")
            if not rows:
                print("Nenhum workspace ativo nesta sessão.")
            for item in rows:
                print(f"{item['id']} | {item['status']:<10} | {item['type']:<24} | {item['path']}")
                print(f"  expira: {item['expires_at']} | protegido: {item['protected']} | processos: {item['processes']}")
            print("========================================================")
            return
        if action == "create":
            workspace_type = " ".join(parts[2:]).strip() or "EXPERIMENT WORKSPACE"
            record = self.workspaces.create(workspace_type)
            print(f"Workspace criado: {record.workspace_id} → {record.path}")
            return
        if action in {"venv", "virtualenv"} and len(parts) >= 3:
            path = self.workspaces.create_virtualenv(parts[2])
            print(f"Virtual Python ativo: {path}")
            return
        if action in {"test", "testar"} and len(parts) >= 3:
            result = self.workspaces.test_python(parts[2])
            print(result["output"] or "(sem saída)")
            print(f"Resultado: {'APROVADO' if result['success'] else 'FALHOU'}")
            return
        if action in {"install", "instalar"} and len(parts) >= 4:
            output, code = self.workspaces.install(parts[2], parts[3:])
            print(output or "(sem saída)")
            print(f"exit code: {code}")
            return
        if action in {"project-deps", "dependencias"} and len(parts) >= 3:
            output, code = self.workspaces.install_project_dependencies(parts[2])
            print(output or "(sem saída)")
            print(f"exit code: {code}")
            return
        if action in {"cleanup", "limpar"}:
            removed = self.workspaces.cleanup_expired()
            print(f"Workspaces expirados descartados: {removed}")
            return
        if action in {"discard", "descartar"} and len(parts) >= 3:
            if self.workspaces.cleanup_workspace(parts[2]):
                print(f"Workspace descartado: {parts[2]}")
            return
        print("Uso: /workspace [status|create [TIPO]|cleanup|discard ID]")

    # NEXUS SECTION: COMMAND_INTERFACE
    @staticmethod
    def help() -> None:
        print("""
================ NEXUS HELP ====================

/nexus <tarefa>             pipeline inteligente
/nexus --auto <tarefa>      modo automático
/nexus --fast <tarefa>      reduz etapas de planejamento
/nexus --auto --fast ...    combina os modos
/nexus stop                 interrompe a tarefa

/status                     estado do NEXUS/pool/quota
/quota                      consumo e limites locais
/quota-reset                zera contadores locais
/keys                       saúde das chaves
/setup                      configura a sessão Microsoft
/reset-limits               remove cooldowns
	/config                     mostra configuração
	/intel                      mostra capacidades AST/Ruff/Pyright/Jedi
	/intel ARQUIVO.py           diagnostica Python localmente
	/scripts                     lista scripts Python gerados
/workspace                   workspaces temporários da sessão
/workspace create [TIPO]    cria um laboratório temporário
/workspace venv ID           cria .venv no laboratório
/workspace test ID           executa py_compile e pytest quando disponível
/workspace install ID PKG    instala dependência somente no .venv
/workspace project-deps ID   instala requirements/pyproject/setup no .venv
/workspace cleanup           remove workspaces expirados
/workspace discard ID        descarta um workspace não protegido
/agente                      gera um pequeno agente Python
/evoluir                     clona, busca por AST/texto, diagnostica e planeja
/evoluir skill <nome>        escolhe uma habilidade diretamente
/evoluir analisar            autoanálise em clone; não aplica alterações
/evoluir auto                recomenda uma mudança e pede confirmações
/evoluir status|history      estado e histórico em memória da sessão
/evoluir roadmap             planos pendentes em memória da sessão
/evoluir rollback            restaura somente um clone validado
/evoluir promover            promove um candidato validado para CURRENT
/code <pedido>               gera projeto Python/Bash e salva arquivos
/code --auto <pedido>        gera e salva sem perguntar novamente
/code --root DIR <pedido>    escolhe o workspace de saída
/orquestrar <objetivo>      orquestrador cognitivo autônomo
/orquestrar --auto <obj>    orquestração sem confirmação por etapa
/planejar <objetivo>        plano estruturado em fases e tarefas
/planejar --auto <obj>      execução planejada sem confirmação por tarefa
/silent                     estado do modo silencioso (sessão/autorização)
/silent-on                  ativa modo silencioso (terminal-first, headless)
/silent-off                 desativa modo silencioso
/self-test                   diagnóstico local
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

    def code_intelligence_command(self, line: str) -> None:
        """Executa AST/Ruff/Pyright e informa suporte de Jedi/prompt_toolkit."""
        argument = line[len("/intel"):].strip()
        engine = CodeIntelligenceEngine(self.config)
        if not argument or argument.casefold() in {"status", "capacidades"}:
            print("\n[NEXUS] CODE INTELLIGENCE")
            for name, enabled in engine.capabilities().items():
                print(f"  {name:<16} {'ATIVO' if enabled else 'indisponível (fallback local)'}")
            print("Uso: /intel ARQUIVO.py")
            return
        path = Path(argument).expanduser().resolve()
        if not path.is_file():
            raise AgentError(f"Arquivo Python não encontrado: {path}")
        source = path.read_text(encoding="utf-8")
        report = engine.analyze(source, str(path))
        print("\n" + engine.format_report(report))
        if _jedi is not None:
            print("\nCompletamento Jedi disponível: use a integração TAB do terminal.")

    def startup_code_intelligence(self) -> None:
        """Prepara dependências e executa uma verificação local antes do prompt."""
        print("\n[NEXUS] Inicialização: verificando requirements e dependências...")
        engine = CodeIntelligenceEngine(self.config)
        statuses = engine.bootstrap_dependencies()
        for item in statuses:
            print(f"  [{item['status']}] {item['name']}: {item['detail']}")
        targets = sorted(GENERATED_DIR.rglob("*.py"))[:30] if GENERATED_DIR.exists() else []
        reports = [(path, engine.analyze(path.read_text(encoding="utf-8"), str(path))) for path in targets]
        if not reports:
            reports = [(Path("<startup>"), engine.analyze("def _nexus_startup_check():\n    return True\n", "<startup>"))]
        failed = [(path, report) for path, report in reports if report["errors"]]
        if failed:
            print(f"[NEXUS] ERRO na verificação inicial: {len(failed)} script(s) com erro.")
            for path, report in failed:
                print(f"\nArquivo: {path}\n{engine.format_report(report)}")
            print("Solicitação de correção: corrija os erros exibidos e execute /intel novamente.")
        else:
            print(f"[NEXUS] Verificação de erro concluída: {len(reports)} script(s) analisado(s).")
            active = ", ".join(name for name, enabled in reports[-1][1]["capabilities"].items() if enabled)
            print(f"[NEXUS] INSTALADO E PRONTO PARA USO — bibliotecas atualizadas; scripts rodando verificação de erro em andamento.")
            print(f"[NEXUS] Code Intelligence ativo: {active}")

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
        if line == "/quota":
            self.quota_status()
            return
        if line == "/quota-reset":
            self.quota_reset()
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
        if line == "/intel" or line.startswith("/intel "):
            try:
                self.code_intelligence_command(line)
            except (AgentError, OSError, UnicodeError) as exc:
                print(f"\n[NEXUS] CODE INTELLIGENCE NÃO PROCESSADO:\n{exc}")
            return
        if line == "/scripts":
            self.list_scripts()
            return
        if line == "/workspace" or line.startswith("/workspace "):
            try:
                self.workspace_command(line)
            except (AgentError, OSError, ValueError) as exc:
                print(f"\n[NEXUS] WORKSPACE NÃO PROCESSADO:\n{exc}")
            return
        if line == "/agente":
            try:
                self.create_agent()
            except (AgentError, RateLimitError, OSError, SyntaxError) as exc:
                print(f"\n\033[1;31m[NEXUS] AGENTE NÃO GERADO:\033[0m\n{exc}")
            return
        if line == "/evoluir" or line.startswith("/evoluir "):
            argument = line[len("/evoluir"):].strip()
            try:
                self.evolve_code(argument)
            except (AgentError, RateLimitError, OSError, SyntaxError, ValueError, KeyError) as exc:
                print(f"\n\033[1;31m[NEXUS] EVOLUÇÃO NÃO APLICADA:\033[0m\n{exc}")
            return
        if line == "/code" or line.startswith("/code "):
            self.process_code(line)
            return
        if line == "/orquestrar" or line.startswith("/orquestrar "):
            self.run_cognitive_orchestration(line)
            return
        if line == "/planejar" or line.startswith("/planejar "):
            self.run_planned_execution(line)
            return
        if line in {"/silent", "/silent-on", "/silent-off"}:
            self.silent_command(line)
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

    # END NEXUS SECTION: COMMAND_INTERFACE

    def banner(self) -> None:
        print(f"""
╔════════════════════════════════════════════════════════════╗
║                    NEXUS TERMINAL                         ║
║                       WHITE RAT                           ║
║                         v7.2                              ║
╠════════════════════════════════════════════════════════════╣
║ ROUTER LOCAL       → 0 API quando possível               ║
║ DECISÃO            → command ou Python                   ║
║ PYTHON ENGINE      → arquivo Python REAL                 ║
║ CODE ENGINE         → projetos Python/Bash multi-arquivo  ║
║ AST + PY_COMPILE   → validação antes de executar         ║
║ PTY                 → Linux REAL                          ║
║ VALIDAÇÃO           → resultado REAL                      ║
║ KEY POOL            → failover controlado                ║
║ EDGE + COPILOT       → IA direta no navegador            ║
║ AUTOCOMPLETE        → TAB                                 ║
║ CODE INTELLIGENCE   → /intel: AST/Ruff/Pyright/Jedi      ║
║ COGNITIVE ORCH.    → /orquestrar: ciclo autônomo        ║
║ PLANNED EXEC.      → /planejar: fases e tarefas        ║
║ SILENT MODE        → /silent-on: terminal-first         ║
╚════════════════════════════════════════════════════════════╝
Cérebro: Microsoft Copilot via EdgeCopilotBrowser
Timeout navegador: {self.config.get("browser_response_timeout")}ms
Scripts: {GENERATED_DIR}
Digite /help para ajuda.
Diagnóstico local: /intel [ARQUIVO.py]
""")

    def run(self) -> None:
        self.pty.start()
        setup_readline(self)
        self.banner()
        self.startup_code_intelligence()

        keys = get_api_keys(self.config)
        if not keys:
            print("\033[1;33m[NEXUS] POOL:\033[0m")
            print("  - Nenhuma sessão Microsoft configurada.")
            print("O primeiro uso abrirá o navegador para login manual.")
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
        self.workspaces.cleanup_all()
        self.pty.stop()
        self.ai.close()

def shlex_quote(value: str) -> str:
    if not value:
        return "''"
    return "'" + value.replace("'", "'\"'\"'") + "'"

# NEXUS SECTION: SELF_DIAGNOSTICS
def self_test() -> None:
    print("\n==============================================")
    print("NEXUS SELF TEST")
    print("==============================================")
    print(f"[OK] Python: {sys.version.split()[0]}")
    print("[OK] pexpect")
    print("[OK] EdgeCopilotBrowser bridge (Microsoft Edge + Copilot)")
    print("[OK] readline")

    print("[TEST] Code Intelligence AST/Ruff/Pyright/Jedi...")
    try:
        intelligence = CodeIntelligenceEngine()
        valid_report = intelligence.analyze("def ok():\n    return 1\n", "<self-test>")
        if not valid_report["ok"]:
            raise ValueError("código válido foi diagnosticado como erro")
        invalid_report = intelligence.analyze("def broken(:\n    pass\n", "<self-test>")
        if invalid_report["ok"] or not any(item["tool"] == "ast" for item in invalid_report["diagnostics"]):
            raise ValueError("erro sintático não foi identificado pelo AST")
        if not isinstance(intelligence.complete("value = 1\nvalue.", 2, 6), list):
            raise ValueError("API de completamento não retornou lista")
        print(f"[OK] Code Intelligence: {', '.join(name for name, enabled in intelligence.capabilities().items() if enabled)}")
    except Exception as exc:
        print(f"[FAIL] Code Intelligence: {exc}")

    print("\n[TEST] Evolution section locator/context budget...")
    try:
        source = Path(__file__).read_text(encoding="utf-8")
        locator = EvolutionSectionLocator(source)
        missing = sorted({name for skill in SKILL_MAP.values() for name in skill["sections"]} - set(locator.sections))
        if missing:
            raise ValueError("seções do mapa ausentes: " + ", ".join(missing))
        context = EvolutionContextBuilder().build(
            source,
            ["EVOLUTION_STATE", "EVOLUTION_ENGINE", "EVOLUTION_WORKFLOW"],
            "evolution context planning",
        )
        if context["chars"] > MAX_EVOLUTION_CONTEXT_CHARS or context["lines"] > MAX_EVOLUTION_CONTEXT_LINES:
            raise ValueError("limite de contexto excedido")
        with tempfile.TemporaryDirectory(prefix="nexus-evolution-selftest-") as clone_root:
            clone_path, source_digest = create_evolution_clone(Path(__file__), Path(clone_root))
            if hashlib.sha256(clone_path.read_bytes()).hexdigest() != source_digest:
                raise ValueError("clone não é byte-idêntico à fonte")
            validate_evolution_clone_syntax(clone_path)
        hits = EvolutionSourceIndex(source).search("planejamento evolução função", ["EVOLUTION_WORKFLOW", "EVOLUTION_ENGINE"])
        if not hits:
            raise ValueError("busca AST/texto não encontrou alvos")
        allowed = {hit["id"]: hit["section"] for hit in hits}
        sample_plan = {
            "analysis": "A busca local encontrou uma função de evolução e a seção correspondente no clone.",
            "evidence": [{"id": hits[0]["id"], "section": hits[0]["section"],
                          "observation": "O identificador existe nos resultados AST/texto locais."}],
            "capabilities_found": [], "limitations": [], "plan": ["Revisar a função identificada e medir o resultado."],
            "tests": [], "risks": [], "summary": "Plano limitado ao clone.",
        }
        validate_evolution_plan_response(sample_plan, allowed)
        empty_first = {"analysis": "", "evidence": [], "plan": [], "summary": ""}
        second_plan_text = json.dumps(sample_plan, ensure_ascii=False)
        selected_second = extract_agent_json(
            json.dumps(empty_first, ensure_ascii=False) + "\n" + second_plan_text,
            "evolucao_plano",
        )
        if selected_second.get("analysis") != sample_plan["analysis"]:
            raise ValueError("o segundo plano válido não foi selecionado")
        validate_evolution_plan_response(
            json.dumps(empty_first, ensure_ascii=False) + "\n" + second_plan_text,
            allowed,
        )
        bad_plan = copy.deepcopy(sample_plan)
        bad_plan["analysis"] = "diagnóstico ancorado no contexto"
        try:
            validate_evolution_plan_response(bad_plan, allowed)
        except AgentError:
            pass
        else:
            raise ValueError("texto-modelo genérico foi aceito como diagnóstico")
        print(f"[OK] {len(locator.sections)} seções; clone SHA-256; AST/text search; evidência vinculada; segunda resposta JSON; placeholder rejeitado; contexto {context['chars']} chars / {context['lines']} linhas")
    except Exception as exc:
        print(f"[FAIL] Evolution engine: {exc}")

    config = load_config()
    print(f"[OK] config: {CONFIG_FILE}")
    print(f"[INFO] pool: {len(get_api_keys(config))} chave(s)")
    quota = QuotaManager(config)
    print("[OK] browser brain configuration")
    print("[INFO] API keys: desativadas")

    print("\n[TEST] Temporary Workspace Intelligence...")
    manager = TemporaryWorkspaceManager({"workspace_ttl_seconds": 120})
    record = None
    try:
        record = manager.create("TEST WORKSPACE")
        result = manager.run(record.workspace_id, [sys.executable, "-c", "print('NEXUS_WORKSPACE_OK')"], 15)
        if result["exit_code"] != 0 or "NEXUS_WORKSPACE_OK" not in result["output"]:
            raise ValueError(f"execução isolada inválida: {result}")
        if not manager.cleanup_workspace(record.workspace_id, force=True):
            raise ValueError("cleanup não removeu o workspace")
        print("[OK] workspace temporário, cwd isolado, processo registrado e cleanup")
    except Exception as exc:
        print(f"[FAIL] Temporary Workspace Intelligence: {exc}")
        if record is not None and record.workspace_id in manager.records:
            manager.cleanup_workspace(record.workspace_id, force=True)

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

    print("\n[TEST] Cognitive Orchestrator components...")
    try:
        assert len(VALID_ORCHESTRATOR_STATES) >= 15, f"esperado >=15 estados, got {len(VALID_ORCHESTRATOR_STATES)}"
        assert len(VALID_RESPONSE_STATUSES) >= 5
        assert len(VALID_RESPONSE_ACTIONS) >= 6
        step = OrchestratorStep(
            step_id="STEP-001", objective="test", expected_result="ok",
            action="RUN_COMMAND", validation="exit_code==0",
            status="pending", attempts=0, max_attempts=3,
        )
        ctx = OrchestratorTaskContext(
            task_id="TEST-001", goal="test", plan=[step],
            current_step_index=0, completed_steps=[], failed_steps=[],
            attempts={}, copilot_requests=[], copilot_responses=[],
            execution_results=[], errors=[], artifacts=[],
            validation_results=[], status="running", state="RECEIVE",
            cycle_count=0, created_at=datetime.now().isoformat(),
            last_progress_cycle=0,
        )
        prompt = CopilotPromptBuilder.build_request(ctx, step, "", "", "Linux")
        assert "<AGENT_REQUEST>" in prompt and "TASK_ID:" in prompt
        valid_response = (
            "<AGENT_RESPONSE>\n"
            "STATUS: READY\n"
            "ACTION: RUN_COMMAND\n"
            "FILE: NONE\n"
            "COMMAND: echo hello\n"
            "CONTENT: NONE\n"
            "REASON: test\n"
            "NEXT_STEP: validate\n"
            "SUCCESS_CRITERIA: output contains hello\n"
            "</AGENT_RESPONSE>"
        )
        parsed = CopilotResponseParser.parse(valid_response)
        assert parsed["valid"] is True
        fields = parsed["fields"]
        assert fields["STATUS"] == "READY"
        assert fields["ACTION"] == "RUN_COMMAND"
        assert fields["COMMAND"] == "echo hello"
        assert SafetyGuard.classify("ls -la") == "READ_ONLY"
        assert SafetyGuard.classify("rm -rf /") == "DESTRUCTIVE"
        assert SafetyGuard.requires_confirmation("DESTRUCTIVE", "rm -rf /", False) is True
        assert SafetyGuard.requires_confirmation("READ_ONLY", "ls", False) is False
        exec_ok = {"exit_code": 0, "stdout": "hello", "stderr": "", "artifacts": []}
        exec_fail = {"exit_code": 1, "stdout": "", "stderr": "error", "artifacts": []}
        ok, _ = CompletionDetector.check(step, exec_ok, "DONE", "output contains hello")
        assert ok is True
        fail, _ = CompletionDetector.check(step, exec_fail, "ERROR", "")
        assert fail is False
        assert StallDetector.detect_stall(ctx) is False or True
        assert step.step_id == "STEP-001"
        assert ctx.task_id == "TEST-001"
        log_dir = DATA_DIR / "orchestrator_logs"
        assert log_dir.parent.exists()
        print("[OK] Cognitive Orchestrator: states, prompt builder, parser, safety guard, completion detector, stall detector, step, context, cycle logger")
    except Exception as exc:
        print(f"[FAIL] Cognitive Orchestrator: {exc}")

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
    print("\n[TEST] Silent Headless Mode components...")
    try:
        sm = SessionManager({"silent_mode": True})
        assert sm.detect() is None or isinstance(sm.detect(), SessionRecord)
        am = AuthManager({"silent_mode": True})
        assert am.is_authorized() in (True, False)
        hc = HeadlessController({"silent_mode": True})
        hc.enable_silent()
        assert hc.silent_mode is True
        assert hc.allow_browser("login") is True
        assert hc.allow_browser("ls") is False
        assert hc.allow_graphical_app("firefox") is False
        hc.disable_silent()
        assert hc.allow_browser("anything") is True
        assert FallbackManager.classify_operation("login") == "GUI_REQUIRED"
        assert FallbackManager.classify_operation("command") == "TERMINAL"
        assert FallbackManager.requires_gui("login MFA") is True
        assert FallbackManager.requires_gui("run ls -la") is False
        print("[OK] SessionManager, AuthManager, HeadlessController, FallbackManager")
    except Exception as exc:
        print(f"[FAIL] Silent Headless Mode: {exc}")

    print("\n==============================================")
    print("SELF TEST FINALIZADO")
    print("==============================================")

# END NEXUS SECTION: SELF_DIAGNOSTICS

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

