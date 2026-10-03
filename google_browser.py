import argparse
import re
import sys
import time
from pathlib import Path
from typing import Optional


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


class GoogleBrowserError(Exception):
    """Erro específico do Google Browser Bridge."""


class GoogleBrowser:
    """
    Ponte entre o NEXUS e o Google AI Mode através do navegador.

    Não utiliza API key.

    O perfil do navegador fica separado do Chrome pessoal para evitar
    conflito entre processos e para que o NEXUS tenha sua própria sessão.
    """

    GOOGLE_AI_URL = "https://www.google.com/ai"

    def __init__(
        self,
        profile_dir: Optional[str | Path] = None,
        headless: bool = False,
        timeout: int = 30000,
        response_timeout: int = 60000,
        slow_mo: int = 0,
    ) -> None:

        if profile_dir is None:
            profile_dir = (
                Path.home()
                / ".nexus"
                / "browser"
                / "google"
            )

        self.profile_dir = Path(profile_dir).expanduser()
        self.profile_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.headless = headless
        self.timeout = timeout
        self.response_timeout = response_timeout
        self.slow_mo = slow_mo

        self._playwright = None
        self._context = None
        self._page = None

    # ==========================================================
    # START / STOP
    # ==========================================================

    def start(self) -> None:
        """
        Inicializa o Chromium com perfil persistente.
        """

        if self._context is not None:
            return

        self._playwright = sync_playwright().start()

        try:

            self._context = (
                self._playwright.chromium
                .launch_persistent_context(
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

            raise GoogleBrowserError(
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

    def close(self) -> None:
        """
        Fecha o navegador e libera o Playwright.
        """

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
    # OPEN GOOGLE AI
    # ==========================================================

    def open_ai(self) -> None:
        """
        Abre o Google AI Mode.
        """

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
    # LOGIN
    # ==========================================================

    def ensure_ready(self) -> None:
        """
        Verifica se o Google está acessível.

        Não tenta descobrir nem preencher senha.

        Se o Google exigir login, o navegador permanece aberto para
        que o usuário possa fazer o login manualmente.
        """

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

            print(
                "\n[NEXUS GOOGLE] "
                "Login do Google necessário."
            )

            print(
                "[NEXUS GOOGLE] "
                "Faça o login manualmente no navegador."
            )

            print(
                "[NEXUS GOOGLE] "
                "Depois pressione ENTER aqui."
            )

            input()

            self.open_ai()

    # ==========================================================
    # FIND INPUT
    # ==========================================================

    def _find_question_input(self):
        """
        Localiza a caixa de pergunta do Google.

        O Google pode alterar o DOM. Por isso utilizamos vários
        seletores alternativos em vez de depender de um único ID.
        """

        page = self.page

        selectors = [

            # Textarea padrão
            'textarea',

            # Inputs relacionados a pesquisa
            'input[name="q"]',

            'textarea[name="q"]',

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
    # SUBMIT
    # ==========================================================

    def _submit_question(self, question: str) -> None:

        page = self.page

        input_box = (
            self._find_question_input()
        )

        if input_box is None:

            raise GoogleBrowserError(
                "Não encontrei a caixa de pergunta "
                "do Google AI Mode."
            )

        input_box.click()

        input_box.fill(question)

        # Primeiro tenta Enter.
        try:

            input_box.press("Enter")

            return

        except Exception:
            pass

        # Fallback: procurar botão.
        buttons = page.locator(
            "button"
        )

        button_patterns = [
            re.compile(
                r"enviar|send|pesquisar|search",
                re.I,
            ),
        ]

        for pattern in button_patterns:

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

                    if pattern.search(text):

                        button.click()

                        return

            except Exception:
                continue

        raise GoogleBrowserError(
            "Não foi possível enviar a pergunta."
        )

    # ==========================================================
    # TEXT SNAPSHOT
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
    # WAIT RESPONSE
    # ==========================================================

    def _wait_for_response(
        self,
        previous_text: str,
    ) -> str:
        """
        Aguarda o Google terminar de gerar a resposta.

        A estratégia não depende de um seletor específico da resposta.
        Observamos alterações do texto da página e indicadores de
        carregamento.

        Isso torna o módulo mais tolerante a mudanças de DOM.
        """

        page = self.page

        deadline = (
            time.monotonic()
            + self.response_timeout / 1000
        )

        last_text = previous_text
        stable_since = None

        while time.monotonic() < deadline:

            time.sleep(1.0)

            current_text = self._page_text()

            if not current_text:
                continue

            if current_text != last_text:

                last_text = current_text
                stable_since = time.monotonic()

                continue

            if stable_since is None:
                continue

            # Se o texto ficou estável por alguns segundos,
            # provavelmente a geração terminou.
            stable_for = (
                time.monotonic()
                - stable_since
            )

            if stable_for >= 3.0:

                # Verifica se ainda existe algum indicador
                # de carregamento.
                loading = self._has_loading_indicator()

                if not loading:
                    return current_text

        raise GoogleBrowserError(
            "Tempo limite aguardando a resposta do Google."
        )

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

    # ==========================================================
    # EXTRACT RESPONSE
    # ==========================================================

    def _extract_response(
        self,
        before: str,
        after: str,
        question: str,
    ) -> str:
        """
        Tenta extrair somente o conteúdo novo.

        Como o DOM do Google pode mudar, existem vários mecanismos
        de fallback.
        """

        # ------------------------------------------------------
        # Estratégia 1:
        # encontrar elementos semânticos de resposta.
        # ------------------------------------------------------

        page = self.page

        selectors = [

            '[data-ved]',

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

                    element = locator.nth(index)

                    if not element.is_visible():
                        continue

                    try:
                        text = element.inner_text()
                    except Exception:
                        continue

                    if len(text.strip()) > 100:
                        candidates.append(
                            text.strip()
                        )

            except Exception:
                continue

        # ------------------------------------------------------
        # Estratégia 2:
        # diferença textual entre antes/depois.
        # ------------------------------------------------------

        before_clean = before.strip()
        after_clean = after.strip()

        if (
            after_clean
            and before_clean
            and after_clean.startswith(
                before_clean
            )
        ):

            diff = after_clean[
                len(before_clean):
            ].strip()

            if len(diff) > 20:

                return self._clean_response(
                    diff,
                    question,
                )

        # ------------------------------------------------------
        # Estratégia 3:
        # escolher maior bloco textual.
        # ------------------------------------------------------

        if candidates:

            candidates.sort(
                key=len,
                reverse=True,
            )

            result = candidates[0]

            return self._clean_response(
                result,
                question,
            )

        # ------------------------------------------------------
        # Estratégia 4:
        # corpo completo.
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

        lines = [
            line.rstrip()
            for line in text.splitlines()
        ]

        cleaned = []

        for line in lines:

            stripped = line.strip()

            if not stripped:
                continue

            # Remove elementos óbvios de interface.
            if stripped in {
                "Google",
                "Pesquisa",
                "Pesquisar",
                "Modo IA",
                "AI Mode",
            }:
                continue

            cleaned.append(
                stripped
            )

        result = "\n".join(
            cleaned
        ).strip()

        return result

    # ==========================================================
    # ASK
    # ==========================================================

    def ask(
        self,
        question: str,
    ) -> str:
        """
        Envia uma pergunta ao Google AI Mode.

        Retorna somente texto.

        Exemplo:

            answer = google.ask(
                "O que é Linux?"
            )
        """

        question = question.strip()

        if not question:

            raise ValueError(
                "A pergunta não pode estar vazia."
            )

        self.start()

        self.ensure_ready()

        page = self.page

        # ------------------------------------------------------
        # Antes da pergunta
        # ------------------------------------------------------

        before = self._page_text()

        # ------------------------------------------------------
        # Envia
        # ------------------------------------------------------

        self._submit_question(
            question
        )

        # ------------------------------------------------------
        # Aguarda resposta
        # ------------------------------------------------------

        after = self._wait_for_response(
            before
        )

        # ------------------------------------------------------
        # Extrai
        # ------------------------------------------------------

        answer = self._extract_response(
            before,
            after,
            question,
        )

        if not answer:

            raise GoogleBrowserError(
                "O Google respondeu, "
                "mas não foi possível extrair o texto."
            )

        return answer

    # ==========================================================
    # CONTEXT / FOLLOW-UP
    # ==========================================================

    def follow_up(
        self,
        question: str,
    ) -> str:
        """
        Faz uma pergunta complementar dentro da mesma sessão.
        """

        return self.ask(question)

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


# ==============================================================
# CLI
# ==============================================================

def main():

    parser = argparse.ArgumentParser(
        description=(
            "NEXUS Google Browser Bridge - "
            "Google AI Mode sem API key"
        )
    )

    parser.add_argument(
        "question",
        nargs="*",
        help="Pergunta enviada ao Google AI Mode",
    )

    parser.add_argument(
        "--profile",
        default=None,
        help=(
            "Diretório do perfil persistente "
            "do navegador"
        ),
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=30000,
    )

    parser.add_argument(
        "--response-timeout",
        type=int,
        default=60000,
    )

    parser.add_argument(
        "--screenshot",
        default=None,
    )

    args = parser.parse_args()

    question = " ".join(
        args.question
    ).strip()

    if not question:

        question = input(
            "NEXUS GOOGLE > "
        ).strip()

    if not question:

        print(
            "Pergunta vazia.",
            file=sys.stderr,
        )

        return 1

    browser = GoogleBrowser(
        profile_dir=args.profile,
        timeout=args.timeout,
        response_timeout=args.response_timeout,
    )

    try:

        print(
            "\n[NEXUS → GOOGLE AI]"
        )

        print(
            f"Pergunta: {question}"
        )

        print(
            "Aguardando resposta...\n"
        )

        answer = browser.ask(
            question
        )

        print(
            "=" * 70
        )

        print(
            "GOOGLE AI → NEXUS"
        )

        print(
            "=" * 70
        )

        print(
            answer
        )

        print(
            "=" * 70
        )

        if args.screenshot:

            screenshot = browser.screenshot(
                args.screenshot
            )

            print(
                f"\nScreenshot: {screenshot}"
            )

        return 0

    except KeyboardInterrupt:

        print(
            "\nOperação cancelada."
        )

        return 130

    except GoogleBrowserError as exc:

        print(
            f"\n[NEXUS GOOGLE ERROR] {exc}",
            file=sys.stderr,
        )

        return 2

    except Exception as exc:

        print(
            f"\n[ERRO] {exc}",
            file=sys.stderr,
        )

        return 3

    finally:

        browser.close()


if __name__ == "__main__":
    sys.exit(
        main()
    )

