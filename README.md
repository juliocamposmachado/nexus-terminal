# NEXUS TERMINAL — Copilot no Microsoft Edge, sem API

<img width="1367" height="718" alt="image" src="https://github.com/user-attachments/assets/34833e54-cc39-4d3a-9a7c-3fc8418355b5" />


Agente Linux em Python com execução real no terminal, usando o **Microsoft Copilot através do navegador Microsoft Edge**. Esta versão não usa Gemini API, OpenAI API, API key, `requests` para IA ou endpoint HTTP de modelo.

> **Status desta documentação:** preparada para a versão local `7.0.1-COPILOT-CONFIRM`, com correções de captura da resposta JSON e confirmação `sim / não / auto`.

## Relação com o repositório original

Repositório pesquisado: [github.com/juliocamposmachado/nexus-terminal](https://github.com/juliocamposmachado/nexus-terminal)

O README publicado no repositório ainda descreve o fluxo antigo baseado em **Gemini API**, pool de chaves e `requests`. Esta documentação substitui aquele fluxo para a instalação local baseada no arquivo `nexus_edge_copilot.py`.

A versão sem API usa esta arquitetura:

```text
pedido do usuário
      ↓
/nexus <pedido>
      ↓
roteamento local
      ↓
Microsoft Edge Flatpak
      ↓
Copilot no navegador
      ↓
JSON com response | command | python
      ↓
validação local
      ↓
pergunta: sim / não / auto
      ↓
PTY real do Linux
      ↓
saída, exit code e validação
```

## Recursos

- Microsoft Edge instalado via Flatpak.
- Microsoft Copilot acessado pela interface web.
- Nenhuma chave de API.
- Nenhum endpoint de IA chamado diretamente pelo Python.
- Leitura da resposta renderizada no navegador.
- Interpretação de JSON retornado pelo Copilot.
- Geração de comandos Linux.
- Confirmação antes da execução.
- Modos `sim`, `não`, `auto` e `parar`.
- Execução no PTY real do Linux.
- Captura de stdout e exit code.
- Abertura de sites com `xdg-open`.
- Abertura de aplicativos `.desktop` com `gtk-launch`.
- Abertura de aplicativos Flatpak com `flatpak run`.
- Catálogo de aplicativos instalados enviado ao Copilot.
- Geração e validação de scripts Python.
- Validação por AST e `py_compile`.
- Proteção adicional para comandos e scripts potencialmente perigosos.
- Perfil isolado do Edge Flatpak para evitar conflito com a sessão normal.

## Requisitos

- Zorin OS 18.1 Education ou outra distribuição Linux compatível.
- Python 3.10 ou superior.
- Microsoft Edge instalado via Flatpak.
- Flatpak disponível no sistema.
- Sessão gráfica ativa para abrir o Edge e aplicativos.
- Internet para acessar o Copilot.
- Login manual no Copilot na primeira utilização.

O projeto não exige:

- Gemini API key;
- OpenAI API key;
- `NEXUS_GEMINI_KEY_1`;
- `requests` para comunicação com IA;
- `python3 -m playwright install chromium`;
- Google Chrome instalado;
- Chromium instalado separadamente.

### Observação sobre Playwright

O Microsoft Edge é baseado no motor Chromium. Por isso, internamente o Playwright usa a API técnica `playwright.chromium` para conectar ao navegador via CDP. Isso **não abre Google Chrome nem o executável Chromium**. O processo iniciado pelo NEXUS é somente:

```bash
flatpak run com.microsoft.Edge
```

## Instalação no Zorin OS

### 1. Entre no diretório do projeto

```bash
cd "/home/zorin/Nexus Copilot"
```

### 2. Crie e ative o ambiente virtual

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Instale somente as dependências do navegador e do PTY

```bash
python -m pip install --upgrade pip
python -m pip install pexpect playwright
```

Não execute estes comandos para esta versão:

```bash
python -m playwright install chromium
python -m playwright install chrome
```

O Edge será iniciado pelo Flatpak do sistema.

### 4. Confirme o Edge Flatpak

```bash
flatpak list --app | grep -i edge
flatpak info com.microsoft.Edge
flatpak run com.microsoft.Edge --version
```

O identificador esperado é:

```text
com.microsoft.Edge
```

Se o comando mostrar outro identificador, defina:

```bash
export NEXUS_EDGE_FLATPAK_APP_ID="ID_EXIBIDO_PELO_FLATPAK"
```

### 5. Permita acesso ao diretório do projeto

Execute uma vez:

```bash
flatpak override --user \\
  --filesystem="/home/zorin/Nexus Copilot" \\
  com.microsoft.Edge
```

O perfil padrão do Edge usado pelo NEXUS fica em uma pasta interna permitida pelo Flatpak:

```text
~/.var/app/com.microsoft.Edge/data/nexus-edge-profile
```

## Instalação do arquivo atualizado

Copie o arquivo atualizado para o projeto:

```bash
cp nexus_edge_copilot.py "/home/zorin/Nexus Copilot/nexus_edge_copilot.py"
```

Caso o arquivo esteja em outro diretório, substitua o primeiro caminho pelo local correto.

Opcionalmente, crie um nome curto para iniciar o programa:

```bash
ln -sf nexus_edge_copilot.py nexus.py
```

## Validação antes da execução

```bash
cd "/home/zorin/Nexus Copilot"
source .venv/bin/activate
python3 -m py_compile nexus_edge_copilot.py
python3 nexus_edge_copilot.py --version
```

Resultado esperado na versão atual:

```text
NEXUS TERMINAL 7.0.1-COPILOT-CONFIRM
```

Teste o PTY sem chamar o Copilot:

```bash
python3 nexus_edge_copilot.py --self-test
```

## Primeira execução

```bash
cd "/home/zorin/Nexus Copilot"
source .venv/bin/activate
python3 nexus_edge_copilot.py
```

Na primeira chamada:

1. o NEXUS inicia o Microsoft Edge Flatpak;
2. abre o perfil isolado do NEXUS;
3. acessa a URL do Copilot configurada;
4. o usuário faz login manualmente, se necessário;
5. o NEXUS envia a instrução pelo campo de mensagem;
6. o NEXUS lê a resposta renderizada no navegador.

Não feche a janela do Edge enquanto uma tarefa estiver sendo processada.

## Uso do `/nexus`

Digite no terminal:

```text
NEXUS> /nexus listar todos os mp3 do meu notebook
```

O Copilot deve interpretar o pedido e devolver um JSON semelhante a:

```json
{
  "execute": true,
  "mode": "command",
  "command": "find /home/zorin -type f -iname \"*.mp3\"",
  "response": "",
  "reason": "Listar todos os arquivos MP3."
}
```

O NEXUS então mostra:

```text
Executar:
  find /home/zorin -type f -iname "*.mp3"
[s]im [n]ão [a]uto [x]parar:
```

### Opções de confirmação

| Entrada | Comportamento |
|---|---|
| `s` ou `sim` | Executa o comando atual. |
| `n` ou `não` | Cancela o comando atual. |
| `a` ou `auto` | Ativa o modo automático para comandos não perigosos. |
| `x` ou `parar` | Interrompe a tarefa e o PTY. |

O campo `execute` do Copilot não substitui essa confirmação local.

## Exemplos de comandos

### Comando simples

```text
/nexus mostrar meu diretório
/nexus listar os arquivos
/nexus verificar a memória
/nexus mostrar o espaço em disco
```

### Procurar arquivos MP3

```text
/nexus listar todos os mp3 do meu notebook
```

O resultado pode ser um comando como:

```bash
find /home/zorin -type f -iname "*.mp3"
```

### Abrir um site

```text
/nexus abrir https://www.google.com
```

O comando esperado é semelhante a:

```bash
xdg-open 'https://www.google.com'
```

### Abrir um programa instalado

```text
/nexus abrir o Firefox
/nexus abrir o LibreOffice
/nexus abrir o VLC
```

O Copilot escolhe um identificador do catálogo disponível, por exemplo:

```bash
gtk-launch firefox.desktop
```

ou:

```bash
flatpak run org.videolan.VLC
```

### Listar aplicativos sem abrir nada

```text
/nexus listar os aplicativos instalados
```

Esse pedido deve gerar uma resposta ou um comando de listagem, sem iniciar todos os programas.

> Não peça para abrir todos os programas instalados ao mesmo tempo sem revisar o comando. Isso pode abrir dezenas de janelas e sobrecarregar a sessão gráfica.

## Formato de resposta do Copilot

O agente principal deve retornar somente JSON válido:

```json
{
  "execute": true,
  "mode": "python|command|response",
  "command": "",
  "response": "",
  "reason": ""
}
```

### `mode: response`

Usado para responder sem executar comandos.

### `mode: command`

Usado para um único comando Linux. O NEXUS mostra o comando, pede confirmação e executa no PTY.

### `mode: python`

Usado para múltiplas etapas, loops, processamento de arquivos ou automação. O código é salvo, validado por AST e `py_compile`, e depois passa pela confirmação aplicável antes da execução.

## Configuração opcional

```bash
export NEXUS_PROJECT_DIR="/home/zorin/Nexus Copilot"
export NEXUS_EDGE_USE_FLATPAK=1
export NEXUS_EDGE_FLATPAK_APP_ID="com.microsoft.Edge"
export NEXUS_EDGE_FLATPAK_PROFILE="$HOME/.var/app/com.microsoft.Edge/data/nexus-edge-profile"
export NEXUS_EDGE_HEADLESS=0
export NEXUS_BROWSER_TIMEOUT=120000
```

A URL do Copilot pode ser definida pela variável abaixo:

```bash
export NEXUS_COPILOT_URL="https://copilot.com/chat?fromcode=cmm9tzigufu&sessionId=4b39bf2e-400d-6ca6-2a7c-dba341494dab&hasLW=true&es=SSR&redirfrom=userTypeCookie&redirfrom=cosmicRingCookie"
```

O `sessionId` presente nessa URL pode expirar ou ser alterado pelo Copilot. Se isso acontecer, atualize a URL configurada.

## Segurança

O NEXUS executa comandos reais no sistema. Portanto:

- leia sempre o comando antes de escolher `sim`;
- use `não` quando o comando não corresponder ao pedido;
- use `auto` apenas para tarefas repetitivas e de baixo risco;
- não aceite automaticamente comandos com `sudo`, remoção, formatação, desligamento ou alterações amplas;
- não coloque senhas, tokens ou chaves no prompt;
- não coloque credenciais em comandos que serão enviados ao Copilot;
- não execute scripts gerados sem revisar o caminho e a finalidade;
- mantenha `dangerous_always_confirm` habilitado.

A integração via navegador elimina a API key, mas **não elimina os riscos de executar comandos no sistema**.

## Diagnóstico

### Edge não abre

```bash
flatpak info com.microsoft.Edge
flatpak run com.microsoft.Edge --version
```

Se o erro mencionar o perfil, remova somente o perfil isolado do NEXUS com o programa fechado:

```bash
rm -rf "$HOME/.var/app/com.microsoft.Edge/data/nexus-edge-profile"
```

Na próxima execução, o perfil será recriado. Não remova o perfil pessoal normal do Edge.

### Copilot abre, mas não responde

- confirme que o login foi concluído;
- verifique se a página aberta é `copilot.com/chat`;
- não feche a janela do Edge;
- aguarde o carregamento da caixa `Message Copilot`;
- confirme se a URL de `NEXUS_COPILOT_URL` ainda está válida.

### Copilot responde, mas o terminal não lê

A versão 7.0.1 procura imediatamente o JSON renderizado e não depende da página inteira ficar estável. Verifique se o arquivo atualizado foi copiado:

```bash
python3 nexus_edge_copilot.py --version
```

Use a versão:

```text
7.0.1-COPILOT-CONFIRM
```

### Erro `name 'as_bool' is not defined`

Esse erro pertence a uma versão anterior. Substitua o arquivo pelo `nexus_edge_copilot.py` atualizado e compile novamente:

```bash
cp /caminho/da/versao/nexus_edge_copilot.py .
python3 -m py_compile nexus_edge_copilot.py
```

### Comando incorreto

Digite `n` na confirmação, copie o JSON exibido no Copilot e revise os campos `mode` e `command`. O NEXUS não deve executar um comando que não corresponda ao pedido.

## Estrutura recomendada

```text
/home/zorin/Nexus Copilot/
├── .venv/
├── nexus_edge_copilot.py
├── nexus.py -> nexus_edge_copilot.py       # opcional
├── README.md
└── .nexus/
    └── generated/
```

Perfil do Edge:

```text
/home/zorin/.var/app/com.microsoft.Edge/data/nexus-edge-profile
```

## Licença e responsabilidade

Consulte a licença e os termos do repositório original antes de redistribuir alterações.

O NEXUS é uma ferramenta de automação local. O usuário é responsável por revisar comandos, scripts, URLs abertas e alterações realizadas no sistema.

## Fontes consultadas

- [Repositório original no GitHub](https://github.com/juliocamposmachado/nexus-terminal)
- [README original](https://github.com/juliocamposmachado/nexus-terminal/blob/index.html/README.md)
- [Código original `nexus.py`](https://github.com/juliocamposmachado/nexus-terminal/blob/index.html/nexus.py)
- [Microsoft: Copilot no Edge](https://support.microsoft.com/en-us/microsoft-copilot/getting-started-with-copilot-in-microsoft-edge)
