# NEXUS TERMINAL

<img width="1361" height="717" alt="image" src="https://github.com/user-attachments/assets/7cf21bac-c359-4990-b655-bb6c37a62be7" />

<img width="678" height="497" alt="image" src="https://github.com/user-attachments/assets/32b13626-8d89-42b9-826f-adc3503341ce" />


## WHITE RAT — AI Linux Agent

NEXUS TERMINAL é um agente de inteligência artificial para Linux desenvolvido em Python.

O projeto conecta um modelo **Google Gemini** a um **terminal Linux real**, utilizando um PTY persistente através do `pexpect`.

A IA não simula comandos.

Ela analisa a solicitação do usuário, propõe comandos Linux através de `ACTION`, o NEXUS executa esses comandos no shell real e devolve o resultado verdadeiro para a IA continuar o raciocínio.

---

## Visão geral

```text
┌─────────────────────────────────────────────────────────┐
│                    NEXUS TERMINAL                       │
│                       WHITE RAT                         │
└─────────────────────────────────────────────────────────┘

                         Usuário
                            │
                            ▼
                    ┌───────────────┐
                    │    NEXUS      │
                    │   Terminal    │
                    └───────┬───────┘
                            │
                            ▼
                    ┌───────────────┐
                    │    Gemini     │
                    │      AI       │
                    └───────┬───────┘
                            │
                       ACTION
                            │
                            ▼
                    ┌───────────────┐
                    │   Linux PTY   │
                    │     REAL      │
                    └───────┬───────┘
                            │
                       comando real
                            │
                            ▼
                    ┌───────────────┐
                    │ Resultado     │
                    │ REAL          │
                    └───────┬───────┘
                            │
                            ▼
                         Gemini
````

O ciclo principal é:

```text
PERGUNTA
   ↓
GEMINI
   ↓
ACTION
   ↓
LINUX REAL
   ↓
RESULTADO REAL
   ↓
GEMINI
   ↓
PRÓXIMA ACTION
```

---

# Principais características

* Terminal Linux real
* PTY persistente
* Shell interativo real
* Integração com Google Gemini
* Execução de comandos reais
* Contexto do terminal enviado para a IA
* Análise do resultado real
* Execução automática opcional
* Modo assistido
* Confirmação para comandos potencialmente perigosos
* Detecção de comandos destrutivos
* Suporte a colagem no terminal
* Tratamento de sequências ANSI
* Suporte a UTF-8
* Configuração persistente
* API key protegida no arquivo de configuração
* Interface totalmente baseada em terminal
* Desenvolvido em Python

---

# Requisitos

## Sistema

Linux com:

* Python 3
* Bash ou shell compatível
* Internet para acessar a API Gemini

## Python

Dependências:

```bash
python3
```

```bash
pexpect
```

```bash
requests
```

---

# Instalação

Clone o projeto:

```bash
git clone https://github.com/juliocamposmachado/nexus-terminal.git
```

Entre no diretório:

```bash
cd nexus-terminal
```

Instale as dependências:

```bash
python3 -m pip install pexpect requests
```

Ou, caso seu sistema utilize ambiente virtual:

```bash
python3 -m venv .venv
```

Ative:

```bash
source .venv/bin/activate
```

Instale:

```bash
pip install pexpect requests
```

---

# Executando

Dê permissão:

```bash
chmod +x nexus.py
```

Execute:

```bash
./nexus.py
```

Ou:

```bash
python3 nexus.py
```

---

# Configuração do Gemini

Execute:

```bash
./nexus.py --config
```

O NEXUS solicitará:

```text
NEXUS GEMINI CONFIG

Provider: Google Gemini

Modelo [gemini-3.6-flash]:

Gemini API key:
```

A configuração será armazenada em:

```text
~/.config/nexus/config.json
```

O arquivo possui permissões restritas:

```text
600
```

---

# API Key por variável de ambiente

Também é possível utilizar uma variável de ambiente:

```bash
export GEMINI_API_KEY="SUA_CHAVE"
```

Depois:

```bash
./nexus.py
```

Para tornar a variável persistente:

```bash
echo 'export GEMINI_API_KEY="SUA_CHAVE"' >> ~/.bashrc
```

Depois:

```bash
source ~/.bashrc
```

> Nunca publique sua API key no GitHub.

Recomenda-se utilizar variável de ambiente ou um arquivo de configuração local que não seja enviado ao repositório.

---

# Arquivo `.gitignore`

Crie um arquivo:

```text
.gitignore
```

Com:

```gitignore
__pycache__/
*.pyc
*.pyo

.venv/
venv/
env/

.env
.env.*
*.log

config.json

.DS_Store
```

---

# Modos de operação

O NEXUS possui três modos principais.

## Modo normal

```text
/nexus <pergunta>
```

Exemplo:

```text
/nexus qual é a versão do meu kernel?
```

O Gemini poderá solicitar:

```xml
<ACTION>
{"command":"uname -a"}
</ACTION>
```

O NEXUS executará o comando no Linux real.

---

# Modo automático

```text
/nexus --auto <tarefa>
```

Exemplo:

```text
/nexus --auto faça um diagnóstico do sistema
```

Nesse modo, o agente pode executar automaticamente os comandos necessários.

Mesmo no modo automático, comandos classificados como potencialmente perigosos continuam sujeitos à confirmação.

---

# Modo assistido

```text
/nexus --assist <tarefa>
```

Exemplo:

```text
/nexus --assist analise o uso de memória deste computador
```

O agente propõe os comandos e o usuário decide se cada comando deve ser executado.

---

# Interromper o agente

Durante uma operação:

```text
/nexus stop
```

---

# Comandos internos

## Configuração

```text
/config
```

## Status

```text
/status
```

## Limpar tela

```text
/clear
```

## Sair

```text
/exit
```

---

# Exemplos

## Verificar sistema

```text
/nexus mostre informações completas sobre meu sistema Linux
```

O agente poderá executar:

```bash
uname -a
```

```bash
lscpu
```

```bash
free -h
```

```bash
df -h
```

```bash
lsblk
```

---

## Verificar processos

```text
/nexus quais processos estão consumindo mais memória?
```

---

## Verificar Docker

```text
/nexus verifique meus containers Docker
```

---

## Trabalhar com Git

```text
/nexus mostre o status do meu repositório Git
```

O agente poderá executar:

```bash
git status
```

---

# ACTION

A comunicação entre o Gemini e o NEXUS utiliza uma estrutura simples:

```xml
<ACTION>
{"command":"comando Linux"}
</ACTION>
```

Por exemplo:

```xml
<ACTION>
{"command":"pwd"}
</ACTION>
```

O NEXUS extrai o comando e executa no PTY.

Depois retorna para a IA:

```text
COMMAND:

pwd

EXIT CODE:

0

REAL OUTPUT:

/home/zorin
```

A IA então pode decidir se precisa executar outro comando.

---

# Terminal Linux REAL

O NEXUS não utiliza um terminal simulado.

A execução é realizada através de um pseudo-terminal:

```text
Python
   │
   ▼
pexpect
   │
   ▼
PTY
   │
   ▼
Bash
   │
   ▼
Linux
```

Isso permite manter uma sessão persistente.

Por exemplo:

```text
cd ~/projeto
```

seguido por:

```text
pwd
```

mantém o contexto do diretório dentro da sessão.

---

# Sistema de segurança

O NEXUS possui uma camada básica de proteção para comandos potencialmente destrutivos.

Entre os padrões monitorados estão:

```text
rm -rf /
rm -rf /*
mkfs
dd ... of=/dev/...
parted
fdisk
format
shutdown
reboot
poweroff
fork bomb
```

Quando um comando é identificado como potencialmente perigoso, o NEXUS solicita confirmação.

Exemplo:

```text
COMANDO POTENCIALMENTE PERIGOSO.

Executar mesmo assim? [sim/N]:
```

O objetivo é evitar que uma ação destrutiva seja executada acidentalmente.

> Este mecanismo não deve ser considerado uma sandbox ou uma garantia de segurança. O usuário continua responsável pelos comandos executados no sistema.

---

# Colagem no terminal

O NEXUS trata sequências de controle utilizadas pelo recurso de **bracketed paste** do terminal.

Sequências como:

```text
ESC [200~
```

e:

```text
ESC [201~
```

são removidas quando necessário.

Também são tratadas sequências ANSI indesejadas.

Isso permite colar comandos normalmente no terminal sem introduzir caracteres estranhos no shell.

---

# Estrutura do projeto

Estrutura básica:

```text
nexus-terminal/
│
├── nexus.py
├── README.md
├── .gitignore
└── LICENSE
```

Configuração do usuário:

```text
~/.config/nexus/
└── config.json
```

---

# Arquitetura

O projeto é dividido conceitualmente em quatro componentes principais.

## 1. NexusApp

Responsável pela aplicação principal.

Gerencia:

* comandos
* modos de execução
* interação com usuário
* agente
* contexto

---

## 2. RealPTY

Responsável pelo terminal Linux real.

Utiliza:

```python
pexpect
```

Responsabilidades:

* iniciar shell
* escrever comandos
* receber saída
* manter sessão
* capturar exit code
* controlar tamanho do terminal
* tratar saída ANSI

---

## 3. AIProvider

Responsável pela comunicação com Gemini.

Utiliza:

```python
requests
```

A comunicação utiliza a API oficial do Gemini.

O endpoint é construído automaticamente:

```text
https://generativelanguage.googleapis.com/
v1beta/models/{model}:generateContent
```

A URL não é configurável pelo usuário, evitando que uma configuração antiga aponte acidentalmente para outro provedor.

---

## 4. Action Parser

Responsável por localizar:

```xml
<ACTION>
...
</ACTION>
```

na resposta da IA.

O JSON é analisado e o comando é extraído:

```json
{
  "command": "pwd"
}
```

---

# Filosofia do projeto

O princípio fundamental do NEXUS é:

> **A IA não deve fingir que executou um comando.**

Ela solicita.

O Linux executa.

O Linux responde.

A IA analisa.

Isso cria um ciclo baseado em resultados reais:

```text
AI
 ↓
COMMAND
 ↓
REAL LINUX
 ↓
REAL OUTPUT
 ↓
AI
```

Em vez de:

```text
AI
 ↓
"Eu executei..."
 ↓
resultado inventado
```

---

# Estado do projeto

**Versão atual:**

```text
2.1.0-GEMINI
```

Status:

```text
Desenvolvimento ativo
```

---

# Roadmap

Possíveis evoluções:

* [ ] Histórico de comandos
* [ ] Histórico de conversas
* [ ] Cancelamento imediato de processos
* [ ] Controle avançado de permissões
* [ ] Sandbox opcional
* [ ] Logs de execução
* [ ] Sessões persistentes
* [ ] Múltiplos agentes
* [ ] Sistema de plugins
* [ ] Integração com Docker
* [ ] Integração com Kubernetes
* [ ] Integração SSH
* [ ] Interface TUI
* [ ] Interface gráfica
* [ ] Execução paralela controlada
* [ ] Memória de projeto
* [ ] Sistema de planejamento multi-etapas

---

# Tecnologias

O projeto utiliza:

* Python 3
* Google Gemini API
* Pexpect
* Requests
* Linux PTY
* Bash
* JSON
* ANSI terminal control

---

# Licença

Este projeto é distribuído sob os termos definidos no arquivo:

```text
LICENSE
```

---

# Autor

**Julio Cesar Campos Machado**

NEXUS TERMINAL
WHITE RAT

GitHub:

[https://github.com/juliocamposmachado](https://github.com/juliocamposmachado)

Projeto:

[https://github.com/juliocamposmachado/nexus-terminal](https://github.com/juliocamposmachado/nexus-terminal)

---

# Contribuições

Sugestões, melhorias, correções e contribuições são bem-vindas.

Para contribuir:

```bash
git clone https://github.com/juliocamposmachado/nexus-terminal.git
```

Crie uma branch:

```bash
git checkout -b minha-feature
```

Faça suas alterações:

```bash
git add .
```

Crie o commit:

```bash
git commit -m "Adiciona nova funcionalidade"
```

Envie:

```bash
git push origin minha-feature
```

---

## NEXUS TERMINAL

```text
AI THINKS.
LINUX EXECUTES.
REAL OUTPUT RETURNS.
```

**WHITE RAT — NEXUS TERMINAL**
