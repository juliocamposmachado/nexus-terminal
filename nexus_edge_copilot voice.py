import argparse
import ast
import copy
from datetime import datetime, timezone
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
import time
import textwrap
import tempfile
import uuid
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
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

try:
    import pexpect
except ImportError:
    print("Dependência ausente: pexpect")
    print("Instale: python3 -m pip install --user pexpect")
    raise SystemExit(1)


import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Optional


try:
    from playwright.sync_api import (
        sync_playwright,
        TimeoutError as PlaywrightTimeoutError,
    )
except ImportError:
    print(
        "ERRO: Playwright não está instalado.",
        file=sys.stderr,
    )
    print(
        "Instale com:",
        file=sys.stderr,
    )
    print(
        "python3 -m pip install --user playwright",
        file=sys.stderr,
    )
    sys.exit(1)


# ==============================================================
# ERROR
# ==============================================================


class EdgeCopilotBrowserError(Exception):
    """Erro específico do Microsoft Browser Bridge."""


# ==============================================================
# EDGE + COPILOT
# ==============================================================


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

APP_NAME = "NEXUS TERMINAL"
APP_VERSION = "7.1.0-COPILOT-VOICE"

DEFAULT_PROJECT_DIR = Path("/home/zorin/Nexus Copilot")
if not DEFAULT_PROJECT_DIR.is_dir():
    DEFAULT_PROJECT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = Path(os.environ.get("NEXUS_PROJECT_DIR", DEFAULT_PROJECT_DIR)).expanduser().resolve()
DATA_DIR = Path(os.environ.get("NEXUS_DATA_DIR", PROJECT_DIR / ".nexus")).expanduser().resolve()

CONFIG_DIR = DATA_DIR
CONFIG_FILE = CONFIG_DIR / "config.json"
HISTORY_FILE = CONFIG_DIR / "history"
GENERATED_DIR = CONFIG_DIR / "generated"
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
    "voice_duration_seconds": 7,
    "voice_language": "pt",
    "voice_model": "base",
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
    "error_cooldown": 15,
    "max_key_failover": 5,
    "max_retries": 1,

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
}

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

    "evolucao": """
Você é o agente EVOLUÇÃO do NEXUS TERMINAL.

Você receberá SOMENTE uma função Python real e um pedido de evolução.
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
        "/nexus stop", "/status", "/quota", "/quota-reset", "/config", "/keys",
        "/reset-limits", "/setup", "/self-test", "/scripts", "/agente", "/evoluir", "/help", "/clear", "/exit",
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

class LocalVoiceInput:
    """Captura e transcreve voz localmente, sem API de IA."""

    STOP_WORDS = {"desligar voz", "desativar voz", "parar voz", "voice off", "sair da voz"}

    def __init__(self, config: dict[str, Any]):
        duration_value = os.environ.get("NEXUS_VOICE_DURATION", config.get("voice_duration_seconds", 7))
        self.duration = max(2, min(30, int(duration_value)))
        self.language = os.environ.get("NEXUS_VOICE_LANGUAGE", str(config.get("voice_language", "pt"))).strip() or "pt"
        self.model_name = os.environ.get("NEXUS_WHISPER_MODEL", str(config.get("voice_model", "base"))).strip() or "base"
        self._model = None

    def _record(self, path: Path) -> None:
        ffmpeg = shutil.which("ffmpeg")
        arecord = shutil.which("arecord")
        if ffmpeg:
            command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                       "-f", "pulse", "-i", "default", "-t", str(self.duration),
                       "-ac", "1", "-ar", "16000", str(path)]
        elif arecord:
            command = [arecord, "-q", "-d", str(self.duration), "-f", "S16_LE",
                       "-r", "16000", "-c", "1", str(path)]
        else:
            raise AgentError("Captura de áudio indisponível: instale ffmpeg ou alsa-utils.")
        print(f"[NEXUS VOICE] Gravando por {self.duration}s... fale agora.")
        result = subprocess.run(command, capture_output=True, text=True, timeout=self.duration + 10)
        if result.returncode != 0 or not path.exists() or path.stat().st_size < 1000:
            detail = (result.stderr or result.stdout or "sem detalhes").strip()
            raise AgentError(f"Não foi possível capturar o microfone: {detail[-1000:]}")

    def _transcribe_faster_whisper(self, path: Path) -> str:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return ""
        if self._model is None:
            print(f"[NEXUS VOICE] Carregando modelo local faster-whisper: {self.model_name}")
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
        segments, _ = self._model.transcribe(str(path), language=self.language, vad_filter=True)
        return " ".join(segment.text.strip() for segment in segments).strip()

    def _transcribe_whisper_cli(self, path: Path) -> str:
        whisper = shutil.which("whisper")
        if not whisper:
            return ""
        with tempfile.TemporaryDirectory(prefix="nexus-whisper-") as output_dir:
            command = [whisper, str(path), "--model", self.model_name, "--language", self.language,
                       "--task", "transcribe", "--output_format", "txt", "--output_dir", output_dir]
            result = subprocess.run(command, capture_output=True, text=True, timeout=180)
            text_path = Path(output_dir) / f"{path.stem}.txt"
            if result.returncode == 0 and text_path.exists():
                return text_path.read_text(encoding="utf-8", errors="ignore").strip()
        return ""

    def listen_once(self) -> str:
        cache_dir = Path.home() / ".cache" / "nexus"
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"voice_{int(time.time())}.wav"
        try:
            self._record(path)
            text = self._transcribe_faster_whisper(path)
            if not text:
                text = self._transcribe_whisper_cli(path)
            if not text:
                raise AgentError(
                    "Transcrição local indisponível. Instale faster-whisper com "
                    "'python -m pip install faster-whisper' ou o comando whisper."
                )
            print(f"[NEXUS VOICE] Você disse: {text}")
            return text
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
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
        self.ai = BrowserClient(self.config)
        self.running = True
        self.stop_event = threading.Event()
        self.task_id = 0
        self.current_task_calls = 0
        self.current_budget = 0
        self.last_output = ""
        self.last_exit: Optional[int] = None
        self.auto_mode = False
        self.voice_mode = False
        self.voice = LocalVoiceInput(self.config)

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
        program = self.call_agent(
            "programacao",
            program_state,
        )

        try:
            program = validate_program_response(program)
        except AgentError as exc:
            # O Microsoft Copilot pode retornar JSON formalmente válido, mas sem código.
            # Nesse caso, repetir com uma instrução explícita é mais seguro do
            # que salvar/executar um arquivo inexistente ou incompleto.
            message = str(exc).lower()
            retryable = "código vazio" in message or "code" in message
            if not retryable or not self.can_call_api():
                raise
            terminal_log(
                "Resposta de programação incompleta; solicitando código completo novamente",
                "WARN",
            )
            retry_state = {
                **program_state,
                "retry_instruction": (
                    "A resposta anterior falhou porque o campo code estava vazio ou incompleto. "
                    "Retorne SOMENTE JSON válido com language=python, filename, dependencies "
                    "e code contendo o programa Python COMPLETO. "
                    "Não use code_lines, não use markdown, não use reticências, "
                    "não resuma e não omita nenhuma função."
                ),
            }
            program = validate_program_response(
                self.call_agent("programacao", retry_state)
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
        self.current_budget = 2 if preferred_mode in {"python", "response"} else 1
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
        program = validate_program_response(
            self.call_agent(
                "programacao",
                {
                    **state,
                    "decision": decision,
                    "retry_instruction": (
                        "Retorne somente JSON com code contendo o programa Python completo."
                    ),
                },
            )
        )
        path = self.save_generated_script(program["filename"], program["code"])
        print(f"\n[NEXUS] SCRIPT PYTHON CRIADO REALMENTE:\n  {path}")
        output, exit_code, executed = self.execute_generated_script(path, program.get("reason", ""))
        if executed:
            print("\n\033[1;35mWHITE RAT:\033[0m")
            print(output if output else f"Script concluído. exit code={exit_code}")

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

    def evolve_code(self) -> None:
        """Pergunta a alteração, substitui somente uma função e cria nova versão."""
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

    def voice_command(self, line: str = "/voice") -> None:
        """Ativa/desativa a entrada por voz local, sem API."""
        option = line[len("/voice"):].strip().lower()
        if option in {"off", "desligar", "desativar", "0"}:
            self.voice_mode = False
            print("[NEXUS VOICE] Modo de voz desativado.")
            return
        if option in {"status", "estado"}:
            print(f"[NEXUS VOICE] {'ATIVO' if self.voice_mode else 'DESATIVADO'}; duração={self.voice.duration}s; modelo={self.voice.model_name}")
            return
        self.voice_mode = not self.voice_mode if not option or option == "toggle" else option in {"on", "ligar", "ativar", "1"}
        print(f"[NEXUS VOICE] Modo de voz {'ativado' if self.voice_mode else 'desativado'}. ")
        if self.voice_mode:
            print("Fale o pedido; diga 'desligar voz' para voltar ao teclado.")

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
        print(f"Voz local:        {'ATIVA' if self.voice_mode else 'DESATIVADA'}")
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

    @staticmethod
    def help() -> None:
        print("""
================ NEXUS HELP ====================

/nexus <tarefa>             pipeline inteligente
/nexus --auto <tarefa>      modo automático
/nexus --fast <tarefa>      reduz etapas de planejamento
/nexus --auto --fast ...    combina os modos
/nexus stop                 interrompe a tarefa
/voice                      ativa/desativa entrada por voz local
/voice on|off|status        controla ou consulta a voz

/status                     estado do NEXUS/pool/quota
/quota                      consumo e limites locais
/quota-reset                zera contadores locais
/keys                       saúde das chaves
/setup                      configura a sessão Microsoft
/reset-limits               remove cooldowns
/config                     mostra configuração
/scripts                    lista scripts Python gerados
/agente                     gera um pequeno agente Python
/evoluir                    evolui uma única função e salva nova versão
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
        if line == "/voice" or line.startswith("/voice "):
            self.voice_command(line)
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
        if line == "/scripts":
            self.list_scripts()
            return
        if line == "/agente":
            try:
                self.create_agent()
            except (AgentError, RateLimitError, OSError, SyntaxError) as exc:
                print(f"\n\033[1;31m[NEXUS] AGENTE NÃO GERADO:\033[0m\n{exc}")
            return
        if line == "/evoluir":
            try:
                self.evolve_code()
            except (AgentError, RateLimitError, OSError, SyntaxError) as exc:
                print(f"\n\033[1;31m[NEXUS] EVOLUÇÃO NÃO APLICADA:\033[0m\n{exc}")
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
║ KEY POOL            → failover controlado                ║
║ EDGE + COPILOT       → IA direta no navegador            ║
║ AUTOCOMPLETE        → TAB                                 ║
╚════════════════════════════════════════════════════════════╝
Cérebro: Microsoft Copilot via EdgeCopilotBrowser
Timeout navegador: {self.config.get("browser_response_timeout")}ms
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
                if self.voice_mode:
                    spoken = self.voice.listen_once()
                    if spoken.strip().lower() in LocalVoiceInput.STOP_WORDS:
                        self.voice_mode = False
                        print("[NEXUS VOICE] Modo de voz desativado.")
                    elif spoken.strip():
                        self.process_nexus("/nexus " + spoken)
                    continue
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
        self.ai.close()

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
    print("[OK] EdgeCopilotBrowser bridge (Microsoft Edge + Copilot)")
    print("[OK] readline")

    config = load_config()
    print(f"[OK] config: {CONFIG_FILE}")
    print(f"[INFO] pool: {len(get_api_keys(config))} chave(s)")
    quota = QuotaManager(config)
    print("[OK] browser brain configuration")
    print("[INFO] API keys: desativadas")

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