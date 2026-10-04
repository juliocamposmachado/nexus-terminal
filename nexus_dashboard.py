import asyncio
import json
import os
import re
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk, messagebox

try:
    from playwright.async_api import async_playwright
except ImportError:
    async_playwright = None


BASE_DIR = Path(__file__).resolve().parent
BACKEND = BASE_DIR / "nexus_edge_copilot.py"
PROFILE_DIR = BASE_DIR / "edge_profile"
DATA_DIR = BASE_DIR / "workspace"
DATA_DIR.mkdir(exist_ok=True)
PROFILE_DIR.mkdir(exist_ok=True)

GMAIL_URL = "https://mail.google.com/mail/u/0/?ogbl#inbox"

WEATHER_URLS = [
    "https://www.google.com/search?q=tempo+hoje+Sao+Paulo",
    "https://www.accuweather.com/pt/br/sao-paulo/4588/weather-forecast/4588",
]

MAX_EMAILS = 3


class NexusBrowser:
    def __init__(self):
        self.playwright = None
        self.context = None
        self.pages = []
        self.ready = False

    async def start(self):
        if async_playwright is None:
            raise RuntimeError(
                "Playwright não está instalado. "
                "Execute: python3 -m pip install playwright"
            )

        self.playwright = await async_playwright().start()

        executable = self.find_edge()

        launch_args = {
            "user_data_dir": str(PROFILE_DIR),
            "headless": False,
            "viewport": {
                "width": 1440,
                "height": 900
            },
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--disable-notifications",
                "--start-maximized",
            ],
        }

        if executable:
            launch_args["executable_path"] = executable

        self.context = await self.playwright.chromium.launch_persistent_context(
            **launch_args
        )

        self.pages = self.context.pages

        if not self.pages:
            page = await self.context.new_page()
            self.pages.append(page)

        self.ready = True

    def find_edge(self):
        candidates = [
            "/usr/bin/microsoft-edge",
            "/usr/bin/microsoft-edge-stable",
            "/usr/bin/msedge",
            "/usr/bin/msedge-stable",
        ]

        for candidate in candidates:
            if os.path.exists(candidate):
                return candidate

        return None

    async def new_page(self):
        page = await self.context.new_page()
        self.pages.append(page)
        return page

    async def open(self, url):
        page = await self.new_page()

        try:
            await page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=60000
            )
        except Exception:
            pass

        await page.wait_for_timeout(3000)
        return page

    async def speak(self, text):
        if not text:
            return

        if not self.pages:
            return

        page = self.pages[-1]

        safe_text = text.replace("\\", "\\\\")
        safe_text = safe_text.replace("`", "\\`")

        script = f"""
        (() => {{
            const text = `{safe_text}`;

            try {{
                window.speechSynthesis.cancel();

                const utterance = new SpeechSynthesisUtterance(text);

                utterance.lang = "pt-BR";
                utterance.rate = 0.95;
                utterance.pitch = 1.0;
                utterance.volume = 1.0;

                const voices = window.speechSynthesis.getVoices();

                const preferred = voices.find(v =>
                    v.lang &&
                    v.lang.toLowerCase().startsWith("pt-br")
                ) || voices.find(v =>
                    v.lang &&
                    v.lang.toLowerCase().startsWith("pt")
                );

                if (preferred) {{
                    utterance.voice = preferred;
                }}

                window.speechSynthesis.speak(utterance);

                return true;
            }} catch (e) {{
                return false;
            }}
        }})()
        """

        try:
            await page.evaluate(script)
        except Exception:
            pass

    async def close(self):
        try:
            if self.context:
                await self.context.close()
        finally:
            if self.playwright:
                await self.playwright.stop()


class GmailReader:
    def __init__(self, browser):
        self.browser = browser

    async def read_last_emails(self):
        page = await self.browser.open(GMAIL_URL)

        await page.wait_for_timeout(5000)

        emails = []

        selectors = [
            "tr.zA",
            "div[role='main'] tr",
            "div[role='main'] [role='link']",
        ]

        rows = []

        for selector in selectors:
            try:
                rows = await page.locator(selector).all()
                if rows:
                    break
            except Exception:
                continue

        for row in rows[:MAX_EMAILS]:
            try:
                text = await row.inner_text()

                if text and len(text.strip()) > 10:
                    emails.append(
                        self.clean_email_text(text)
                    )
            except Exception:
                continue

        if len(emails) < MAX_EMAILS:
            emails = await self.extract_from_body(page)

        return emails[:MAX_EMAILS]

    async def extract_from_body(self, page):
        result = []

        try:
            body = await page.locator("body").inner_text()
        except Exception:
            return result

        lines = [
            x.strip()
            for x in body.splitlines()
            if x.strip()
        ]

        blacklist = {
            "caixa de entrada",
            "principal",
            "promoções",
            "social",
            "atualizações",
            "enviados",
            "rascunhos",
            "mais",
        }

        for line in lines:
            if len(line) < 10:
                continue

            if line.lower() in blacklist:
                continue

            if "google" in line.lower() and len(line) < 30:
                continue

            result.append(line)

            if len(result) >= MAX_EMAILS:
                break

        return result

    def clean_email_text(self, text):
        text = re.sub(r"\s+", " ", text)
        return text.strip()


class WeatherReader:
    def __init__(self, browser):
        self.browser = browser

    async def read_weather(self):
        page = await self.browser.open(WEATHER_URLS[0])

        await page.wait_for_timeout(3000)

        try:
            body = await page.locator("body").inner_text()
        except Exception:
            return "Não foi possível obter o tempo."

        lines = [
            x.strip()
            for x in body.splitlines()
            if x.strip()
        ]

        relevant = []

        keywords = [
            "°",
            "chuva",
            "sol",
            "nublado",
            "temperatura",
            "umidade",
            "vento",
            "máxima",
            "mínima",
        ]

        for line in lines:
            low = line.lower()

            if any(k in low for k in keywords):
                if len(line) < 180:
                    relevant.append(line)

            if len(relevant) >= 8:
                break

        if not relevant:
            return "Não foi possível identificar os dados meteorológicos."

        return " | ".join(relevant)


class NexusCognition:
    def __init__(self):
        self.backend = BACKEND

    def build_prompt(self, emails, weather):
        email_text = "\n".join(
            f"- {email}"
            for email in emails
        )

        prompt = f"""
Você é o núcleo de engenharia de cognição do NEXUS.

Data atual:
{datetime.now().strftime("%d/%m/%Y %H:%M")}

Informações meteorológicas:
{weather}

Últimos e-mails encontrados:
{email_text}

Produza um RESUMO EXECUTIVO DO DIA em português brasileiro.

Organize exatamente nesta lógica:

1. SAUDAÇÃO
2. VISÃO GERAL DO DIA
3. TEMPO
4. E-MAILS IMPORTANTES
5. PRIORIDADES
6. RECOMENDAÇÃO DO NEXUS

Se os e-mails não contiverem informações suficientes,
não invente informações.

Se houver informação meteorológica incompleta,
informe claramente.

O texto deve ser natural para leitura em voz alta.

Não use tabelas.
Não use markdown excessivo.
Não faça respostas longas.
"""

        return prompt

    def run_backend(self, prompt):
        if not self.backend.exists():
            return (
                "O arquivo nexus_edge_copilot.py "
                "não foi encontrado."
            )

        commands = [
            [
                sys.executable,
                str(self.backend),
                "--prompt",
                prompt,
            ],
            [
                sys.executable,
                str(self.backend),
                prompt,
            ],
        ]

        for command in commands:
            try:
                result = subprocess.run(
                    command,
                    cwd=str(BASE_DIR),
                    capture_output=True,
                    text=True,
                    timeout=180,
                )

                output = (
                    result.stdout.strip()
                    if result.stdout.strip()
                    else result.stderr.strip()
                )

                if output:
                    return output

            except Exception:
                continue

        return (
            "O núcleo cognitivo NEXUS não retornou uma resposta. "
            "Verifique o modo de execução do nexus_edge_copilot.py."
        )


class NexusDashboard:
    def __init__(self, root):
        self.root = root

        self.root.title(
            "NEXUS COGNITION DASHBOARD"
        )

        self.root.geometry("1450x900")
        self.root.minsize(1100, 700)

        self.browser = None
        self.email_reader = None
        self.weather_reader = None
        self.cognition = NexusCognition()

        self.current_summary = ""

        self.status_var = tk.StringVar(
            value="Inicializando NEXUS..."
        )

        self.clock_var = tk.StringVar()

        self.build_interface()
        self.update_clock()

        threading.Thread(
            target=self.start_system,
            daemon=True
        ).start()

    def build_interface(self):
        style = ttk.Style()

        try:
            style.theme_use("clam")
        except Exception:
            pass

        header = ttk.Frame(
            self.root,
            padding=15
        )

        header.pack(
            fill="x"
        )

        title = ttk.Label(
            header,
            text="NEXUS",
            font=("Arial", 28, "bold")
        )

        title.pack(
            side="left"
        )

        subtitle = ttk.Label(
            header,
            text="ENGINEERING OF COGNITION",
            font=("Arial", 12)
        )

        subtitle.pack(
            side="left",
            padx=20
        )

        clock = ttk.Label(
            header,
            textvariable=self.clock_var,
            font=("Arial", 12)
        )

        clock.pack(
            side="right"
        )

        status = ttk.Label(
            self.root,
            textvariable=self.status_var,
            padding=(15, 5)
        )

        status.pack(
            fill="x"
        )

        main = ttk.Frame(
            self.root,
            padding=15
        )

        main.pack(
            fill="both",
            expand=True
        )

        left = ttk.Frame(main)

        left.pack(
            side="left",
            fill="both",
            expand=True,
            padx=(0, 10)
        )

        right = ttk.Frame(main)

        right.pack(
            side="right",
            fill="both",
            expand=True,
            padx=(10, 0)
        )

        self.create_section(
            left,
            "RESUMO COGNITIVO DO DIA"
        )

        self.summary = tk.Text(
            left,
            wrap="word",
            font=("Arial", 13),
            padx=15,
            pady=15
        )

        self.summary.pack(
            fill="both",
            expand=True
        )

        self.create_section(
            right,
            "ÚLTIMOS E-MAILS"
        )

        self.emails = tk.Text(
            right,
            wrap="word",
            font=("Arial", 11),
            padx=15,
            pady=15,
            height=12
        )

        self.emails.pack(
            fill="both",
            expand=True
        )

        self.create_section(
            right,
            "METEOROLOGIA"
        )

        self.weather = tk.Text(
            right,
            wrap="word",
            font=("Arial", 11),
            padx=15,
            pady=15,
            height=8
        )

        self.weather.pack(
            fill="both",
            expand=True
        )

        controls = ttk.Frame(
            self.root,
            padding=15
        )

        controls.pack(
            fill="x"
        )

        ttk.Button(
            controls,
            text="ATUALIZAR",
            command=self.refresh
        ).pack(
            side="left",
            padx=5
        )

        ttk.Button(
            controls,
            text="LER RESUMO",
            command=self.speak_summary
        ).pack(
            side="left",
            padx=5
        )

        ttk.Button(
            controls,
            text="ABRIR GMAIL",
            command=self.open_gmail
        ).pack(
            side="left",
            padx=5
        )

        ttk.Button(
            controls,
            text="TEMPO",
            command=self.open_weather
        ).pack(
            side="left",
            padx=5
        )

        ttk.Button(
            controls,
            text="SAIR",
            command=self.close
        ).pack(
            side="right",
            padx=5
        )

    def create_section(self, parent, title):
        label = ttk.Label(
            parent,
            text=title,
            font=("Arial", 11, "bold")
        )

        label.pack(
            anchor="w",
            pady=(8, 5)
        )

    def update_clock(self):
        now = datetime.now()

        self.clock_var.set(
            now.strftime(
                "%d/%m/%Y  %H:%M:%S"
            )
        )

        self.root.after(
            1000,
            self.update_clock
        )

    def start_system(self):
        asyncio.run(
            self.initialize_browser()
        )

    async def initialize_browser(self):
        try:
            self.set_status(
                "Inicializando navegador cognitivo..."
            )

            self.browser = NexusBrowser()

            await self.browser.start()

            self.email_reader = GmailReader(
                self.browser
            )

            self.weather_reader = WeatherReader(
                self.browser
            )

            self.set_status(
                "NEXUS ONLINE — preparando resumo do dia..."
            )

            await self.perform_daily_analysis()

        except Exception as e:
            self.set_status(
                f"Erro: {e}"
            )

    async def perform_daily_analysis(self):
        try:
            self.set_status(
                "Abrindo Gmail e lendo os últimos 3 e-mails..."
            )

            emails = await self.email_reader.read_last_emails()

            self.root.after(
                0,
                lambda: self.show_emails(emails)
            )

            self.set_status(
                "Consultando informações meteorológicas..."
            )

            weather = await self.weather_reader.read_weather()

            self.root.after(
                0,
                lambda: self.show_weather(weather)
            )

            self.set_status(
                "Executando engenharia de cognição NEXUS..."
            )

            prompt = self.cognition.build_prompt(
                emails,
                weather
            )

            summary = await asyncio.to_thread(
                self.cognition.run_backend,
                prompt
            )

            self.current_summary = summary

            self.root.after(
                0,
                lambda: self.show_summary(summary)
            )

            self.set_status(
                "Resumo concluído — NEXUS operacional."
            )

            await self.browser.speak(
                self.prepare_speech(summary)
            )

        except Exception as e:
            self.set_status(
                f"Falha na análise: {e}"
            )

    def prepare_speech(self, text):
        text = re.sub(
            r"[*#_`]+",
            "",
            text
        )

        text = re.sub(
            r"\n+",
            ". ",
            text
        )

        return text.strip()

    def show_summary(self, text):
        self.summary.delete(
            "1.0",
            tk.END
        )

        self.summary.insert(
            tk.END,
            text
        )

        self.summary.see(
            "1.0"
        )

    def show_emails(self, emails):
        self.emails.delete(
            "1.0",
            tk.END
        )

        if not emails:
            self.emails.insert(
                tk.END,
                "Nenhum e-mail identificado."
            )
            return

        for index, email in enumerate(
            emails,
            start=1
        ):
            self.emails.insert(
                tk.END,
                f"{index}. {email}\n\n"
            )

    def show_weather(self, weather):
        self.weather.delete(
            "1.0",
            tk.END
        )

        self.weather.insert(
            tk.END,
            weather
        )

    def set_status(self, text):
        self.root.after(
            0,
            lambda: self.status_var.set(text)
        )

    def refresh(self):
        if not self.browser:
            return

        threading.Thread(
            target=self.refresh_thread,
            daemon=True
        ).start()

    def refresh_thread(self):
        asyncio.run(
            self.perform_daily_analysis()
        )

    def speak_summary(self):
        if not self.current_summary:
            return

        threading.Thread(
            target=self.speak_thread,
            daemon=True
        ).start()

    def speak_thread(self):
        asyncio.run(
            self.speak_async()
        )

    async def speak_async(self):
        if self.browser:
            await self.browser.speak(
                self.prepare_speech(
                    self.current_summary
                )
            )

    def open_gmail(self):
        threading.Thread(
            target=self.open_gmail_thread,
            daemon=True
        ).start()

    def open_gmail_thread(self):
        asyncio.run(
            self.open_gmail_async()
        )

    async def open_gmail_async(self):
        if self.browser:
            await self.browser.open(
                GMAIL_URL
            )

    def open_weather(self):
        threading.Thread(
            target=self.open_weather_thread,
            daemon=True
        ).start()

    def open_weather_thread(self):
        asyncio.run(
            self.open_weather_async()
        )

    async def open_weather_async(self):
        if self.browser:
            await self.browser.open(
                WEATHER_URLS[0]
            )

    def close(self):
        threading.Thread(
            target=self.close_thread,
            daemon=True
        ).start()

        self.root.after(
            500,
            self.root.destroy
        )

    def close_thread(self):
        try:
            if self.browser:
                asyncio.run(
                    self.browser.close()
                )
        except Exception:
            pass


def main():
    root = tk.Tk()

    app = NexusDashboard(
        root
    )

    root.protocol(
        "WM_DELETE_WINDOW",
        app.close
    )

    root.mainloop()


if __name__ == "__main__":
    main()
