#!/usr/bin/env python3

import os
import sys
import subprocess
import shutil
from pathlib import Path
from datetime import datetime

VERSION = "1.0.0"

HOME = Path.home()
LOG_DIR = HOME / ".nexus" / "security"
LOG_FILE = LOG_DIR / "install.log"
REPORT_FILE = LOG_DIR / "install-report.txt"

LOG_DIR.mkdir(parents=True, exist_ok=True)


# ============================================================
# CORES
# ============================================================

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"


def title(text):
    print(f"\n{BOLD}{text}{RESET}")


def ok(text):
    print(f"{GREEN}✓{RESET} {text}")


def warn(text):
    print(f"{YELLOW}!{RESET} {text}")


def error(text):
    print(f"{RED}✗{RESET} {text}")


def info(text):
    print(f"{CYAN}→{RESET} {text}")


def log(text):
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{datetime.now().isoformat()}] {text}\n")


def run(command, check=False):
    log("$ " + " ".join(command))

    result = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT
    )

    if result.stdout:
        print(result.stdout, end="")
        log(result.stdout)

    if check and result.returncode != 0:
        raise RuntimeError(
            f"Comando falhou: {' '.join(command)}"
        )

    return result


def sudo(command):
    return run(["sudo"] + command)


# ============================================================
# SEGURANÇA
# ============================================================

def check_environment():

    if os.geteuid() == 0:
        error("Não execute este programa como root.")
        print("Execute normalmente:")
        print("  python3 nexus_security_toolkit.py")
        sys.exit(1)

    if not shutil.which("sudo"):
        error("sudo não está instalado.")
        sys.exit(1)

    if not shutil.which("apt-get"):
        error("Este programa requer uma distribuição baseada em Debian/Ubuntu.")
        sys.exit(1)

    if not Path("/etc/os-release").exists():
        error("Não foi possível identificar o sistema operacional.")
        sys.exit(1)

    os_release = {}

    for line in Path("/etc/os-release").read_text(
        encoding="utf-8"
    ).splitlines():

        if "=" in line:
            key, value = line.split("=", 1)
            os_release[key] = value.strip('"')

    distro_id = os_release.get("ID", "")
    id_like = os_release.get("ID_LIKE", "")

    allowed = {
        "zorin",
        "ubuntu",
        "debian",
        "linuxmint",
        "pop"
    }

    if distro_id not in allowed and not any(
        x in id_like for x in ("debian", "ubuntu")
    ):
        warn(
            f"Sistema não reconhecido: "
            f"{distro_id} / {id_like}"
        )

        answer = input("Continuar mesmo assim? [y/N]: ")

        if answer.lower() != "y":
            sys.exit(0)

    # Não modificar sistemas com Kali configurado.
    apt_sources = [
        Path("/etc/apt/sources.list"),
        Path("/etc/apt/sources.list.d")
    ]

    kali_found = False

    for source in apt_sources:

        if source.is_file():

            try:
                content = source.read_text(
                    encoding="utf-8",
                    errors="ignore"
                )

                if (
                    "kali.org" in content
                    or "kali-rolling" in content
                    or "kali-last-snapshot" in content
                ):
                    kali_found = True

            except Exception:
                pass

        elif source.is_dir():

            for file in source.glob("*"):

                if not file.is_file():
                    continue

                try:
                    content = file.read_text(
                        encoding="utf-8",
                        errors="ignore"
                    )

                    if (
                        "kali.org" in content
                        or "kali-rolling" in content
                        or "kali-last-snapshot" in content
                    ):
                        kali_found = True

                except Exception:
                    pass

    if kali_found:

        error("Foi detectado um repositório Kali configurado.")

        print()
        print(
            "Este instalador não altera sistemas "
            "que possuem repositórios Kali."
        )

        sys.exit(1)

    return os_release


# ============================================================
# FERRAMENTAS
# ============================================================

TOOLS = [

    ("Reconhecimento", "nmap", "nmap"),
    ("Reconhecimento", "masscan", "masscan"),
    ("Reconhecimento", "dnsutils", "dig"),
    ("Reconhecimento", "whois", "whois"),

    ("Web", "ffuf", "ffuf"),
    ("Web", "gobuster", "gobuster"),
    ("Web", "nikto", "nikto"),
    ("Web", "sqlmap", "sqlmap"),

    ("Redes", "wireshark", "wireshark"),
    ("Redes", "tcpdump", "tcpdump"),
    ("Redes", "netcat-openbsd", "nc"),
    ("Redes", "traceroute", "traceroute"),

    ("Senhas / Hashes", "hashcat", "hashcat"),
    ("Senhas / Hashes", "john", "john"),
    ("Senhas / Hashes", "hydra", "hydra"),

    ("Engenharia Reversa", "ghidra", "ghidra"),
    ("Engenharia Reversa", "gdb", "gdb"),
    ("Engenharia Reversa", "binutils", "objdump"),

    ("Análise", "yara", "yara"),
    ("Análise", "binwalk", "binwalk"),
    ("Análise", "sleuthkit", "fls"),
]


EXTRAS = [
    "curl",
    "wget",
    "git",
    "python3",
    "python3-pip",
    "python3-venv",
    "build-essential"
]


# ============================================================
# APT
# ============================================================

def apt_package_exists(package):

    result = subprocess.run(
        ["apt-cache", "show", package],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )

    return result.returncode == 0


def package_installed(package):

    result = subprocess.run(
        [
            "dpkg-query",
            "-W",
            "-f=${Status}",
            package
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True
    )

    return "install ok installed" in result.stdout


def binary_exists(binary):

    return shutil.which(binary) is not None


# ============================================================
# INSTALAÇÃO
# ============================================================

def install_package(
    category,
    package,
    binary,
    installed,
    already,
    unavailable,
    failed
):

    print()
    print(
        f"{BOLD}[{category}] {package}{RESET}"
    )

    if binary_exists(binary):

        already.append(package)

        ok(
            f"{package} já está disponível "
            f"({binary})"
        )

        return

    if not apt_package_exists(package):

        unavailable.append(package)

        warn(
            f"{package} não está disponível "
            f"no APT deste sistema"
        )

        return

    info(f"Instalando {package}...")

    result = sudo([
        "apt-get",
        "install",
        "-y",
        package
    ])

    if result.returncode != 0:

        failed.append(package)

        error(
            f"Falha na instalação: {package}"
        )

        return

    if binary_exists(binary):

        installed.append(package)

        ok(
            f"{package} instalado com sucesso"
        )

    else:

        failed.append(package)

        error(
            f"{package} foi instalado, "
            f"mas {binary} não foi localizado"
        )


# ============================================================
# STATUS
# ============================================================

def create_status_command():

    status_file = HOME / ".local" / "bin" / "nexus-security-status"

    status_file.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    content = r'''#!/usr/bin/env python3

import shutil
import subprocess

TOOLS = [
    ("RECONHECIMENTO", [
        ("nmap", "nmap"),
        ("masscan", "masscan"),
        ("dnsutils", "dig"),
        ("whois", "whois"),
    ]),

    ("WEB", [
        ("ffuf", "ffuf"),
        ("gobuster", "gobuster"),
        ("nikto", "nikto"),
        ("sqlmap", "sqlmap"),
    ]),

    ("REDES", [
        ("wireshark", "wireshark"),
        ("tcpdump", "tcpdump"),
        ("netcat", "nc"),
        ("traceroute", "traceroute"),
    ]),

    ("SENHAS / HASHES", [
        ("hashcat", "hashcat"),
        ("john", "john"),
        ("hydra", "hydra"),
    ]),

    ("ENGENHARIA REVERSA", [
        ("ghidra", "ghidra"),
        ("gdb", "gdb"),
        ("binutils", "objdump"),
    ]),

    ("ANÁLISE", [
        ("yara", "yara"),
        ("binwalk", "binwalk"),
        ("sleuthkit", "fls"),
    ]),
]


print("\033[1mNEXUS SECURITY TOOLKIT — STATUS\033[0m")

for category, tools in TOOLS:

    print()
    print(category)

    for name, binary in tools:

        path = shutil.which(binary)

        if path:

            version = ""

            try:

                result = subprocess.run(
                    [binary, "--version"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    timeout=3
                )

                if result.stdout:
                    version = result.stdout.splitlines()[0]

            except Exception:
                pass

            print(
                f"\033[32m✓\033[0m "
                f"{name:<18} "
                f"{version or path}"
            )

        else:

            print(
                f"\033[31m✗\033[0m "
                f"{name:<18} não encontrada"
            )
'''

    status_file.write_text(
        content,
        encoding="utf-8"
    )

    status_file.chmod(0o755)

    return status_file


# ============================================================
# RELATÓRIO
# ============================================================

def create_report(
    os_release,
    installed,
    already,
    unavailable,
    failed
):

    with REPORT_FILE.open(
        "w",
        encoding="utf-8"
    ) as f:

        f.write(
            "NEXUS SECURITY TOOLKIT\n"
        )

        f.write(
            f"Versão: {VERSION}\n"
        )

        f.write(
            f"Data: {datetime.now().isoformat()}\n"
        )

        f.write(
            f"Sistema: "
            f"{os_release.get('PRETTY_NAME', 'unknown')}\n"
        )

        f.write(
            f"Kernel: "
            f"{os.uname().sysname} "
            f"{os.uname().release}\n"
        )

        f.write("\n")
        f.write("RESUMO\n")

        f.write(
            f"Instaladas: {len(installed)}\n"
        )

        f.write(
            f"Já disponíveis: {len(already)}\n"
        )

        f.write(
            f"Indisponíveis: {len(unavailable)}\n"
        )

        f.write(
            f"Falhas: {len(failed)}\n"
        )

        sections = [
            ("INSTALADAS", installed),
            ("JÁ DISPONÍVEIS", already),
            ("INDISPONÍVEIS", unavailable),
            ("FALHAS", failed)
        ]

        for title_text, values in sections:

            f.write("\n")
            f.write(title_text + "\n")

            if values:

                for value in values:
                    f.write(f"  - {value}\n")

            else:

                f.write("  - nenhuma\n")


# ============================================================
# MAIN
# ============================================================

def main():

    title(
        "=============================================="
    )

    title(
        f"       NEXUS SECURITY TOOLKIT v{VERSION}"
    )

    title(
        "=============================================="
    )

    print()

    os_release = check_environment()

    print(
        f"Sistema: "
        f"{os_release.get('PRETTY_NAME', 'unknown')}"
    )

    print(
        f"Kernel: "
        f"{os.uname().sysname} "
        f"{os.uname().release}"
    )

    print(
        f"Log: {LOG_FILE}"
    )

    print()

    installed = []
    already = []
    unavailable = []
    failed = []

    # --------------------------------------------------------
    # APT UPDATE
    # --------------------------------------------------------

    title("1. Atualizando índices APT")

    result = sudo([
        "apt-get",
        "update"
    ])

    if result.returncode == 0:
        ok("Índices APT atualizados")
    else:
        warn(
            "apt-get update apresentou erros."
        )

    # --------------------------------------------------------
    # TOOLS
    # --------------------------------------------------------

    title(
        "2. Instalando ferramentas de segurança"
    )

    for category, package, binary in TOOLS:

        install_package(
            category,
            package,
            binary,
            installed,
            already,
            unavailable,
            failed
        )

    # --------------------------------------------------------
    # EXTRAS
    # --------------------------------------------------------

    title(
        "3. Instalando dependências auxiliares"
    )

    for package in EXTRAS:

        if package_installed(package):

            ok(
                f"{package} já instalado"
            )

            continue

        if not apt_package_exists(package):

            warn(
                f"{package} indisponível"
            )

            continue

        info(
            f"Instalando {package}..."
        )

        result = sudo([
            "apt-get",
            "install",
            "-y",
            package
        ])

        if result.returncode == 0:
            ok(
                f"{package} instalado"
            )
        else:
            warn(
                f"Falha ao instalar {package}"
            )

    # --------------------------------------------------------
    # WIRESHARK
    # --------------------------------------------------------

    title("4. Configuração do Wireshark")

    if binary_exists("wireshark"):

        result = subprocess.run(
            [
                "getent",
                "group",
                "wireshark"
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        if result.returncode == 0:

            groups = subprocess.run(
                ["id", "-nG", os.environ["USER"]],
                stdout=subprocess.PIPE,
                text=True
            ).stdout.split()

            if "wireshark" in groups:

                ok(
                    "Usuário já pertence ao grupo wireshark"
                )

            else:

                result = sudo([
                    "usermod",
                    "-aG",
                    "wireshark",
                    os.environ["USER"]
                ])

                if result.returncode == 0:

                    ok(
                        "Usuário adicionado ao grupo wireshark"
                    )

                    warn(
                        "Faça logout/login para ativar o grupo."
                    )

                else:

                    warn(
                        "Não foi possível adicionar "
                        "o usuário ao grupo wireshark."
                    )

    else:

        warn(
            "Wireshark não está instalado."
        )

    # --------------------------------------------------------
    # LIMPEZA
    # --------------------------------------------------------

    title("5. Limpeza")

    sudo([
        "apt-get",
        "autoremove",
        "-y"
    ])

    sudo([
        "apt-get",
        "clean"
    ])

    # --------------------------------------------------------
    # STATUS
    # --------------------------------------------------------

    status_file = create_status_command()

    # --------------------------------------------------------
    # REPORT
    # --------------------------------------------------------

    create_report(
        os_release,
        installed,
        already,
        unavailable,
        failed
    )

    # --------------------------------------------------------
    # RESULTADO
    # --------------------------------------------------------

    title(
        "=============================================="
    )

    title(
        "          INSTALAÇÃO CONCLUÍDA"
    )

    title(
        "=============================================="
    )

    print()

    print(
        f"  Instaladas:       {len(installed)}"
    )

    print(
        f"  Já disponíveis:   {len(already)}"
    )

    print(
        f"  Indisponíveis:    {len(unavailable)}"
    )

    print(
        f"  Falhas:           {len(failed)}"
    )

    print()

    print(
        f"Relatório:\n"
        f"  {REPORT_FILE}"
    )

    print()

    print(
        f"Log:\n"
        f"  {LOG_FILE}"
    )

    print()

    print(
        f"Status:\n"
        f"  {status_file}"
    )

    print()

    if unavailable:

        warn(
            "Algumas ferramentas não estão "
            "disponíveis nos repositórios atuais."
        )

    if failed:

        warn(
            "Existem falhas. Consulte o relatório."
        )

    print()

    title(
        "NEXUS SECURITY TOOLKIT pronto."
    )


if __name__ == "__main__":

    try:
        main()

    except KeyboardInterrupt:

        print()
        warn("Instalação interrompida pelo usuário.")
        sys.exit(130)

    except Exception as exc:

        print()
        error(f"ERRO: {exc}")
        log(f"ERRO: {exc}")
        sys.exit(1)
