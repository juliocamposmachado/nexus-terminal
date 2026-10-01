#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""NEXUS TERMINAL GUI - assistente seguro de chaves Gemini e console NEXUS."""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

APP_TITLE = "NEXUS TERMINAL — Gemini Key Manager"
AI_STUDIO_URL = "https://aistudio.google.com/api-keys"
CONFIG_DIR = Path.home() / ".config" / "nexus"
CONFIG_FILE = CONFIG_DIR / "config.json"
NEXUS_FILE = Path(__file__).with_name("nexus_fixed.py")


def parse_keys(text: str) -> list[str]:
    """Aceita linhas, vírgulas, ponto e vírgula e formato JSON com aspas."""
    quoted = re.findall(r'"([^"\r\n]*)"', text or "")
    parts = quoted if quoted else re.split(r"[\r\n,;]+", text or "")
    result: list[str] = []
    for item in parts:
        item = item.strip().strip('"\'').strip()
        if not item or item.startswith("#"):
            continue
        if item not in result:
            result.append(item)
    return result


def load_config() -> dict:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            pass
    if not isinstance(data.get("keys"), list):
        data["keys"] = []
    return data


def save_keys(keys: list[str]) -> None:
    data = load_config()
    data["keys"] = keys
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_FILE)
    os.chmod(CONFIG_FILE, 0o600)


class KeyWizard(tk.Toplevel):
    def __init__(self, master: "NexusGUI", initial: bool = False):
        super().__init__(master)
        self.master_app = master
        self.title("Configurar chaves Gemini")
        self.geometry("760x560")
        self.minsize(620, 440)
        self.transient(master)
        self.grab_set()
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.count = tk.IntVar(value=max(1, len(master.keys) or 1))
        self._build(initial)

    def _build(self, initial: bool) -> None:
        pad = {"padx": 14, "pady": 8}
        ttk.Label(self, text="Assistente de chaves Gemini", font=("TkDefaultFont", 15, "bold")).pack(anchor="w", **pad)
        ttk.Label(self, text=("Quantas chaves você pretende usar? A aplicação não impõe limite.\n"
                              "O Google exige criação manual após login; este assistente não lê sua senha."), justify="left").pack(anchor="w", **pad)
        row = ttk.Frame(self); row.pack(fill="x", **pad)
        ttk.Label(row, text="Quantidade planejada:").pack(side="left")
        ttk.Spinbox(row, from_=1, to=1000000, textvariable=self.count, width=12).pack(side="left", padx=8)
        ttk.Button(row, text="Abrir Google AI Studio", command=lambda: webbrowser.open(AI_STUDIO_URL)).pack(side="left", padx=8)
        ttk.Label(self, text=AI_STUDIO_URL, foreground="#1d5fa7").pack(anchor="w", padx=14)
        ttk.Label(self, text=("No Google AI Studio: faça login, crie as chaves e cole abaixo.\n"
                              "Aceita uma chave por linha ou este formato: \"CHAVE\","), justify="left").pack(anchor="w", **pad)
        self.text = tk.Text(self, height=16, wrap="none", undo=True, font=("TkFixedFont", 10))
        self.text.pack(fill="both", expand=True, padx=14, pady=4)
        self.text.focus_set()
        buttons = ttk.Frame(self); buttons.pack(fill="x", padx=14, pady=10)
        ttk.Button(buttons, text="Salvar chaves", command=self.save).pack(side="right", padx=5)
        ttk.Button(buttons, text="Cancelar", command=self.cancel).pack(side="right")
        self.status = ttk.Label(self, text="As chaves não serão exibidas no log.", foreground="#555")
        self.status.pack(anchor="w", padx=14, pady=(0, 10))
        if initial and self.master_app.keys:
            self.status.configure(text=f"Já existem {len(self.master_app.keys)} chaves. Colar um novo bloco substituirá o pool.")

    def save(self) -> None:
        keys = parse_keys(self.text.get("1.0", "end"))
        if not keys:
            messagebox.showwarning("Nenhuma chave", "Cole pelo menos uma chave Gemini.", parent=self)
            return
        try:
            planned = max(1, int(self.count.get()))
        except (ValueError, tk.TclError):
            planned = len(keys)
        if len(keys) != planned:
            if not messagebox.askyesno("Quantidade diferente", f"Você informou {planned}, mas colou {len(keys)} chave(s). Salvar assim mesmo?", parent=self):
                return
        save_keys(keys)
        self.master_app.reload_keys()
        self.text.delete("1.0", "end")
        messagebox.showinfo("Pool salvo", f"{len(keys)} chave(s) salvas com permissão 0600.", parent=self)
        self.destroy()

    def cancel(self) -> None:
        self.destroy()


class NexusGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1050x700")
        self.minsize(800, 520)
        self.keys: list[str] = []
        self.proc: subprocess.Popen[str] | None = None
        self.output_queue: queue.Queue[str] = queue.Queue()
        self._build()
        self.reload_keys()
        self.after(100, self._drain_output)
        self.after(300, self._startup_wizard)

    def _build(self) -> None:
        style = ttk.Style(self)
        try: style.theme_use("clam")
        except tk.TclError: pass
        top = ttk.Frame(self, padding=12); top.pack(fill="x")
        ttk.Label(top, text="NEXUS TERMINAL", font=("TkDefaultFont", 18, "bold")).pack(side="left")
        self.pool_label = ttk.Label(top, text="Pool: carregando..."); self.pool_label.pack(side="left", padx=20)
        ttk.Button(top, text="Gerar/adicionar chaves", command=self.open_wizard).pack(side="right", padx=4)
        ttk.Button(top, text="Abrir AI Studio", command=lambda: webbrowser.open(AI_STUDIO_URL)).pack(side="right", padx=4)
        body = ttk.PanedWindow(self, orient="vertical"); body.pack(fill="both", expand=True, padx=12, pady=4)
        out_frame = ttk.Frame(body); body.add(out_frame, weight=5)
        self.output = tk.Text(out_frame, state="disabled", wrap="word", bg="#101418", fg="#d8e6d8", insertbackground="white", font=("TkFixedFont", 10))
        scroll = ttk.Scrollbar(out_frame, orient="vertical", command=self.output.yview); self.output.configure(yscrollcommand=scroll.set)
        self.output.pack(side="left", fill="both", expand=True); scroll.pack(side="right", fill="y")
        cmd_frame = ttk.Frame(body, padding=8); body.add(cmd_frame, weight=0)
        ttk.Label(cmd_frame, text="Comando NEXUS:").pack(side="left")
        self.command = ttk.Entry(cmd_frame); self.command.pack(side="left", fill="x", expand=True, padx=8); self.command.bind("<Return>", lambda _: self.send_command())
        ttk.Button(cmd_frame, text="Enviar", command=self.send_command).pack(side="left")
        bottom = ttk.Frame(self, padding=(12, 4)); bottom.pack(fill="x")
        self.start_btn = ttk.Button(bottom, text="Iniciar NEXUS", command=self.start_nexus); self.start_btn.pack(side="left")
        ttk.Button(bottom, text="Parar tarefa", command=lambda: self.send_text("/nexus stop")).pack(side="left", padx=6)
        ttk.Button(bottom, text="Limpar tela", command=self.clear_output).pack(side="left")
        ttk.Button(bottom, text="Sair", command=self.close).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)

    def _startup_wizard(self) -> None:
        # Sempre pergunta no primeiro uso; com pool existente, permite revisar/adicionar.
        KeyWizard(self, initial=True)

    def reload_keys(self) -> None:
        self.keys = load_config().get("keys", [])
        self.pool_label.configure(text=f"Pool: {len(self.keys)} chave(s) | config 0600")

    def open_wizard(self) -> None:
        KeyWizard(self)

    def log(self, text: str) -> None:
        self.output.configure(state="normal"); self.output.insert("end", text); self.output.see("end"); self.output.configure(state="disabled")

    def clear_output(self) -> None:
        self.output.configure(state="normal"); self.output.delete("1.0", "end"); self.output.configure(state="disabled")

    def start_nexus(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.log("\n[NEXUS GUI] O processo já está em execução.\n"); return
        if not NEXUS_FILE.exists():
            messagebox.showerror("Arquivo ausente", f"Não encontrei {NEXUS_FILE}", parent=self); return
        self.proc = subprocess.Popen([sys.executable, "-u", str(NEXUS_FILE)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, cwd=str(NEXUS_FILE.parent))
        self.start_btn.configure(text="NEXUS em execução")
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self.output_queue.put(line)
        self.output_queue.put("\n[NEXUS GUI] Processo encerrado.\n")

    def _drain_output(self) -> None:
        try:
            while True: self.log(self.output_queue.get_nowait())
        except queue.Empty: pass
        self.after(100, self._drain_output)

    def send_text(self, text: str) -> None:
        if not self.proc or self.proc.poll() is not None or not self.proc.stdin:
            self.log("\n[NEXUS GUI] Inicie o NEXUS primeiro.\n"); return
        self.proc.stdin.write(text + "\n"); self.proc.stdin.flush()

    def send_command(self) -> None:
        text = self.command.get().strip()
        if text:
            self.send_text(text); self.command.delete(0, "end")

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            if not messagebox.askyesno("Sair", "Encerrar o NEXUS agora?", parent=self): return
            try: self.proc.terminate()
            except OSError: pass
        self.destroy()


if __name__ == "__main__":
    NexusGUI().mainloop()
