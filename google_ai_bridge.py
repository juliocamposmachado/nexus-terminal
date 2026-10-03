import argparse
import contextlib
import json
import os
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


class GoogleAIBridgeError(Exception):
    """Erro específico do Google Browser Bridge."""


# ==============================================================
# GOOGLE BROWSER
# ==============================================================


class GoogleAIBridge:

    GOOGLE_AI_URL = "https://www.google.com/ai"

    def __init__(
        self,
        profile_dir: Optional[str | Path] = None,
        headless: bool = False,
        timeout: int = 30000,
        response_timeout: int = 60000,
        slow_mo: int = 0,
        stable_seconds: float = 2.0,
        poll_interval: float = 0.5,
    ) -> None:

        if profile_dir is None:
            profile_dir = os.environ.get("GOOGLE_PROFILE_DIR") or (Path.home() / ".config" / "google-ai-bridge")

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

        self._playwright = None
        self._context = None
        self._page = None

        self._ready = False

    # ==========================================================
    # START
    # ==========================================================

    def start(self) -> None:

        if self._context is not None:
            return

        self._playwright = sync_playwright().start()

        try:

            self._context = (
                self._playwright.chromium.launch_persistent_context(
                    user_data_dir=str(
                        self.profile_dir
                    ),
                    headless=self.headless,
                    slow_mo=self.slow_mo,
                    viewport={
                        "width": 1440,
                        "height": 900,
                    },
                    locale="pt-BR",
                    timezone_id="America/Sao_Paulo",
                    args=[
                        "--disable-blink-features=AutomationControlled",
                    ],
                )
            )

        except Exception as exc:

            self.close()

            raise GoogleAIBridgeError(
                f"Não foi possível iniciar Chromium: {exc}"
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

            if self._context is not None:
                self._context.close()

        except Exception:
            pass

        finally:

            self._context = None
            self._page = None

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

        if (
            "google.com" not in current_url
            or "/ai" not in current_url
        ):

            page.goto(
                self.GOOGLE_AI_URL,
                wait_until="domcontentloaded",
                timeout=self.timeout,
            )

        try:

            page.wait_for_load_state(
                "networkidle",
                timeout=10000,
            )

        except Exception:
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
                "[NEXUS GOOGLE] Login necessário."
            )

            print(
                "[NEXUS GOOGLE] "
                "Faça o login manualmente no navegador."
            )

            print(
                "[NEXUS GOOGLE] "
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

        selectors = [

            'textarea',

            'textarea[name="q"]',

            'input[name="q"]',

            'textarea[aria-label*="Pergunte"]',

            'textarea[placeholder*="Pergunte"]',

            'textarea[aria-label*="Ask"]',

            'textarea[placeholder*="Ask"]',

            'input[aria-label*="Pergunte"]',

            'input[placeholder*="Pergunte"]',

            'input[aria-label*="Ask"]',

            'input[placeholder*="Ask"]',

        ]

        for selector in selectors:

            try:

                locator = page.locator(
                    selector
                )

                count = locator.count()

                for index in range(count):

                    candidate = locator.nth(index)

                    if candidate.is_visible():

                        return candidate

            except Exception:

                continue

        return None

    # ==========================================================
    # SUBMIT QUESTION
    # ==========================================================

    def _submit_question(
        self,
        question: str,
    ) -> None:

        page = self.page

        input_box = (
            self._find_question_input()
        )

        if input_box is None:

            raise GoogleAIBridgeError(
                "Não encontrei a caixa de pergunta "
                "do Google AI Mode."
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

        # Preenche a pergunta.
        try:

            input_box.fill(question)

        except Exception as exc:

            raise GoogleAIBridgeError(
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
                r"enviar|send|pesquisar|search",
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

        raise GoogleAIBridgeError(
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
                        # O Google pode estabilizar brevemente em "..." antes
                        # de continuar a resposta. Não entregue esse estado.
                        stable_since = time.monotonic()
                        continue
                    return current_text

        raise GoogleAIBridgeError(
            "Tempo limite aguardando "
            "a resposta do Google."
        )

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
                "Google",
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

        # Mesmo depois da estabilização visual, o Google pode substituir um
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
            raise GoogleAIBridgeError(
                "O Google ainda não entregou a resposta final; "
                "o placeholder foi descartado."
            )

        if not answer:

            raise GoogleAIBridgeError(
                "O Google respondeu, "
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
            "NEXUS GOOGLE BRAIN"
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
                    "\nNEXUS GOOGLE > "
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
                    "[NEXUS GOOGLE] "
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
                    f"\n[NEXUS GOOGLE ERROR] {exc}",
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

# ======================================================================
# CLI GOOGLE AI BRIDGE
# ======================================================================

def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "s", "on"}


def _make_bridge(args) -> GoogleAIBridge:
    return GoogleAIBridge(
        profile_dir=args.profile or os.environ.get("GOOGLE_PROFILE_DIR"),
        headless=args.headless,
        timeout=args.timeout,
        response_timeout=args.response_timeout,
        stable_seconds=args.stable_time,
        poll_interval=args.poll_interval,
    )


def _json_result(success: bool, response: str = "", error: str | None = None) -> str:
    return json.dumps(
        {"success": success, "response": response if success else "", "error": error},
        ensure_ascii=False,
    )


def run_server(args) -> int:
    bridge = _make_bridge(args)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            bridge.start()
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
                prompt = str(item.get("prompt", "")).strip()
                if not prompt:
                    raise ValueError("campo prompt vazio")
                with contextlib.redirect_stdout(sys.stderr):
                    answer = bridge.ask(prompt)
                print(_json_result(True, answer), flush=True)
            except Exception as exc:
                print(_json_result(False, error=str(exc)), flush=True)
        return 0
    finally:
        bridge.close()


def run_interactive(args) -> int:
    bridge = _make_bridge(args)
    try:
        bridge.start()
        print("GOOGLE AI BRIDGE")
        print("Digite sua pergunta. Use /sair para encerrar.")
        while True:
            try:
                prompt = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if prompt.lower() in {"/sair", "/exit", "/quit"}:
                break
            if not prompt:
                continue
            try:
                print("\n[GOOGLE AI] Enviando...", file=sys.stderr)
                answer = bridge.ask(prompt)
                print("\n[GOOGLE AI] Resposta:\n")
                print(answer)
            except Exception as exc:
                print(f"[GOOGLE AI ERROR] {exc}", file=sys.stderr)
        return 0
    finally:
        bridge.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Google AI Mode via navegador Playwright")
    parser.add_argument("question", nargs="*", help="pergunta")
    parser.add_argument("--prompt", default=None)
    parser.add_argument("--interactive", action="store_true")
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--json", action="store_true", dest="json_mode")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--headless", action="store_true", default=_env_bool("GOOGLE_HEADLESS", False))
    parser.add_argument("--timeout", type=int, default=int(os.environ.get("GOOGLE_TIMEOUT", "30000")))
    parser.add_argument("--response-timeout", type=int, default=int(os.environ.get("GOOGLE_RESPONSE_TIMEOUT", "120000")))
    parser.add_argument("--stable-time", type=float, default=float(os.environ.get("GOOGLE_STABLE_TIME", "2.0")))
    parser.add_argument("--poll-interval", type=float, default=float(os.environ.get("GOOGLE_POLL_INTERVAL", "0.5")))
    args = parser.parse_args()

    if args.server:
        return run_server(args)
    if args.interactive:
        return run_interactive(args)

    prompt = args.prompt or " ".join(args.question).strip()
    if not prompt:
        prompt = input("GOOGLE AI > ").strip()
    if not prompt:
        result = _json_result(False, error="pergunta vazia")
        print(result if args.json_mode else "Pergunta vazia.", file=sys.stderr if not args.json_mode else sys.stdout)
        return 1

    bridge = _make_bridge(args)
    try:
        with contextlib.redirect_stdout(sys.stderr):
            answer = bridge.ask(prompt)
        if args.json_mode:
            print(_json_result(True, answer))
        else:
            print(answer)
        return 0
    except Exception as exc:
        if args.json_mode:
            print(_json_result(False, error=str(exc)))
        else:
            print(f"[GOOGLE AI ERROR] {exc}", file=sys.stderr)
        return 2
    finally:
        bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
