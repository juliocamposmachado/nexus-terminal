# NEXUS TERMINAL — Copilot no Microsoft Edge, sem API

<img width="1367" height="718" alt="image" src="https://github.com/user-attachments/assets/34833e54-cc39-4d3a-9a7c-3fc8418355b5" />

[nexus.webm](https://github.com/user-attachments/assets/539a2bf3-1d7e-4449-be1b-54011e89e006)



# NEXUS TERMINAL — WHITE RAT

**Versão:** 8.0.0-SEVEN-PHASES

Assistente de terminal Linux com PTY real, executor de scripts Python, orquestrador cognitivo autônomo, gerador de projetos com 7 fases lógicas e modo silencioso terminal-first. O cérebro de raciocínio é o Microsoft Copilot, acessado diretamente via navegador Edge com Playwright.

---

## Novidades desta versão

### Sistema de 7 Fases Lógicas

O planejador do NEXUS agora funciona como um usuário comum interagindo com uma IA de desenvolvimento. Em vez de enviar o pedido original diretamente como "gere todo o projeto", o NEXUS primeiro consulta o Copilot como consultor técnico: "Como criar um site simples? Quais arquivos, componentes e etapas são necessários?".

A partir da resposta, o NEXUS constrói um plano com exatamente 7 fases obrigatórias, exibe um menu de planejamento visível no terminal e executa o plano progressivamente com progresso em tempo real.

**As 7 fases:**

| Fase | Nome | Função |
|---|---|---|
| 1 | Descoberta e Planejamento | Consulta o Copilot como realizar a tarefa, identifica tipo de projeto, arquivos e dependências |
| 2 | Arquitetura e Estrutura | Transforma a consulta em estrutura concreta: diretórios, arquivos, responsabilidades, ordem |
| 3 | Implementação Inicial | Cria os primeiros arquivos, um por tarefa, validando cada um antes de avançar |
| 4 | Componentes | Continua construção de componentes, funcionalidades e integrações |
| 5 | Integração | Verifica se os arquivos funcionam em conjunto, corrige incompatibilidades |
| 6 | Testes e Correções | Executa o projeto, detecta erros, solicita correções quando necessário |
| 7 | Validação Final | Compara o resultado real com o pedido original e confirma todas as fases |

**Menu de planejamento visível:**

Após a consulta inicial, o NEXUS imprime no terminal:

```
[NEXUS] PLANEJAMENTO IDENTIFICADO
  [FASE 1/7] DESCOBERTA E PLANEJAMENTO ........ CONCLUÍDA
  [FASE 2/7] ARQUITETURA E ESTRUTURA .......... CONCLUÍDA
  [FASE 3/7] IMPLEMENTAÇÃO INICIAL ............ PENDENTE
  [FASE 4/7] COMPONENTES ...................... PENDENTE
  [FASE 5/7] INTEGRAÇÃO ....................... PENDENTE
  [FASE 6/7] TESTES E CORREÇÕES ............... PENDENTE
  [FASE 7/7] VALIDAÇÃO FINAL .................. PENDENTE

[AÇÕES IDENTIFICADAS]
  01. Criar index.html
  02. Criar style.css
  03. Criar script.js
  ...
```

A quantidade de ações é dinâmica — projetos simples têm poucas ações, projetos complexos têm muitas.

**Progresso em tempo real:**

Durante cada ação, o terminal mostra:

```
[NEXUS] FASE 3/7 — IMPLEMENTAÇÃO INICIAL
[NEXUS] AÇÃO 01/09
[NEXUS] CONSULTANDO COPILOT → Como criar o index.html para este projeto?
[NEXUS] GERANDO → index.html
[NEXUS] VALIDANDO → index.html
[NEXUS] index.html GERADO E VALIDADO ✓
[NEXUS] PROGRESSO DO PROJETO [████░░░░░░░░░░░░░░░░░░░░░░] 11% (1/9)
[NEXUS] VERIFICANDO PRÓXIMA TAREFA...
```

**Controle total do NEXUS:**

- A IA recebe somente a tarefa específica do arquivo atual, nunca o pedido original como se fosse para gerar tudo
- O NEXUS sabe sempre: fase atual, ação atual, total de ações, ações concluídas, próxima ação e percentual de evolução
- O pedido original permanece imutável durante todo o processo
- O plano é dinâmico: se o Copilot identificar uma nova necessidade, o NEXUS adiciona uma ação sem apagar as existentes
- A conclusão somente ocorre após a Fase 7, confirmando que todas as fases foram processadas e o resultado atende ao objetivo original

### Planejador com Análise Arquitetural

O `PlanningIntegrity` foi reescrito para armazenar metadados arquiteturais completos:

- **`objective`** — objetivo geral do projeto
- **`project_type`** — tipo identificado (Python, Bash, web, API, CLI, full-stack, etc.)
- **`architecture`** — arquitetura proposta
- **`directory_structure`** — estrutura de diretórios necessária
- **`implementation_order`** — ordem de implementação
- **`validation_strategy`** — como validar o projeto ao final
- **`depends_on`** — dependências explícitas entre arquivos

Cada arquivo do plano possui dependências declaradas, e o NEXUS verifica se as dependências estão concluídas antes de gerar o próximo arquivo.

### Validação de Estrutura Global

Ao final, o NEXUS executa uma validação completa da estrutura do projeto:

- Verifica se todos os arquivos planejados foram criados
- Verifica se não há arquivos vazios
- Verifica sintaxe Python com AST
- Detecta placeholders em qualquer arquivo
- Confirma que o resultado corresponde ao objetivo original

### Detecção de Múltiplos Arquivos

O validador de resposta agora detecta se a IA retornou referências a outros arquivos além do solicitado, rejeitando respostas que tentam gerar mais de um arquivo por tarefa.

### Suporte a Múltiplas Linguagens

O validador de plano agora reconhece TypeScript, JavaScript, HTML, CSS e JSON além de Python e Bash, com detecção automática por extensão de arquivo.

---

## Modo Silencioso (Silent Headless Mode)

Seção `SILENT_HEADLESS` (v1.0) que prioriza execução via terminal e minimiza uso do navegador. O navegador abre apenas uma vez para login/autorização; depois, todas as operações rodam via terminal reaproveitando sessões persistentes.

**Cinco componentes:**

| Componente | Função |
|---|---|
| `SessionManager` | Detecta, valida, armazena, renova e encerra sessões do Copilot via arquivo JSON local (permissão 0600) |
| `AuthManager` | Gerencia primeira autorização; abre navegador apenas para login inicial |
| `TerminalExecutor` | Motor de execução real para terminal em modo silencioso |
| `HeadlessController` | Bloqueia aberturas desnecessárias de navegador quando o modo silencioso está ativo |
| `FallbackManager` | Classifica operações entre GUI_REQUIRED e TERMINAL |
| `SilentModeController` | Coordena todos os componentes acima |

**Comandos:**

```
/silent        → mostra estado do modo silencioso
/silent-on     → ativa modo silencioso (terminal-first, headless)
/silent-off    → desativa modo silencioso
```

---

## Arquitetura geral — 7 Fases

```
PEDIDO DO USUÁRIO
  ↓
FASE 1: CONSULTAR COPILOT ("Como fazer?")
  ↓
INTERPRETAR RESPOSTA
  ↓
GERAR MENU DE PLANEJAMENTO (7 fases + ações)
  ↓
FASE 2: DEFINIR ARQUITETURA
  ↓
FASE 3: GERAR ARQUIVO POR ARQUIVO (implementação inicial)
  ↓
FASE 4: IMPLEMENTAR COMPONENTES
  ↓
FASE 5: INTEGRAR
  ↓
FASE 6: TESTAR E CORRIGIR
  ↓
FASE 7: VALIDAR TUDO
  ↓
CONCLUSÃO
```

---

## Arquitetura geral — Pipeline NEXUS

```
PEDIDO
  ↓
ROTEADOR LOCAL (0 API quando possível)
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
             stdout / stderr
                    ↓
               exit code
```

---

## Comandos disponíveis

| Comando | Descrição |
|---|---|
| `/nexus <tarefa>` | Pipeline inteligente completo |
| `/nexus --auto <tarefa>` | Modo automático (sem confirmação por etapa) |
| `/nexus --fast <tarefa>` | Reduz etapas de planejamento |
| `/nexus stop` | Interrompe a tarefa atual |
| `/status` | Estado do NEXUS, pool de chaves e quota |
| `/quota` | Consumo e limites locais |
| `/quota-reset` | Zera contadores locais |
| `/keys` | Saúde das chaves |
| `/setup` | Configura a sessão Microsoft |
| `/reset-limits` | Remove cooldowns |
| `/config` | Mostra configuração atual |
| `/intel` | Capacidades AST/Ruff/Pyright/Jedi |
| `/intel ARQUIVO.py` | Diagnostica Python localmente |
| `/scripts` | Lista scripts Python gerados |
| `/workspace` | Workspaces temporários da sessão |
| `/workspace create [TIPO]` | Cria laboratório temporário |
| `/workspace venv ID` | Cria .venv no laboratório |
| `/workspace test ID` | Executa py_compile e pytest |
| `/workspace install ID PKG` | Instala dependência no .venv |
| `/workspace project-deps ID` | Instala requirements/pyproject/setup no .venv |
| `/workspace cleanup` | Remove workspaces expirados |
| `/workspace discard ID` | Descarta workspace não protegido |
| `/agente` | Gera um pequeno agente Python |
| `/evoluir` | Clona, busca por AST/texto, diagnostica e planeja |
| `/evoluir skill <nome>` | Escolhe uma habilidade diretamente |
| `/evoluir analisar` | Autoanálise em clone; não aplica alterações |
| `/evoluir auto` | Recomenda mudança e pede confirmações |
| `/evoluir status\|history` | Estado e histórico em memória da sessão |
| `/evoluir roadmap` | Planos pendentes em memória da sessão |
| `/evoluir rollback` | Restaura somente um clone validado |
| `/evoluir promover` | Promove um candidato validado para CURRENT |
| `/code <pedido>` | Gera projeto com 7 fases e salva arquivos |
| `/code --auto <pedido>` | Gera e salva sem perguntar novamente |
| `/code --root DIR <pedido>` | Escolhe o workspace de saída |
| `/orquestrar <objetivo>` | Orquestrador cognitivo autônomo |
| `/orquestrar --auto <obj>` | Orquestração sem confirmação por etapa |
| `/silent` | Estado do modo silencioso |
| `/silent-on` | Ativa modo silencioso (terminal-first, headless) |
| `/silent-off` | Desativa modo silencioso |
| `/self-test` | Diagnóstico local completo |
| `/clear` | Limpa a tela |
| `/help` | Esta ajuda |
| `/exit` | Sai do terminal |

---

## Classes principais

### Planejamento (7 fases)

- **`PlanningIntegrity`** — Planejador com 7 fases lógicas. Armazena objetivo imutável, metadados arquiteturais, tarefas com dependências, estados de fase e log de rejeições. Consulta o Copilot como consultor técnico e constrói plano executável.
- **`SEVEN_PHASES`** — Constante com os nomes das 7 fases: Descoberta, Arquitetura, Implementação Inicial, Componentes, Integração, Testes, Validação Final.
- **`print_planning_menu`** — Exibe o menu de planejamento com fases e ações identificadas.
- **`print_phase_header`** — Exibe cabeçalho da fase e ação atual.
- **`print_action_step`** — Exibe etapas da ação (consultando, gerando, validando, concluido).
- **`print_progress_percent`** — Exibe barra de progresso com percentual de evolução.

### Núcleo

- **`NexusApp`** — Aplicação principal. Gerencia PTY, pool de chaves, quota, workspaces, cliente Copilot, modo automático e modo silencioso.
- **`RealPTY`** — Terminal Linux real via pexpect. Shell bash com PS1 customizado, leitura assíncrona em thread dedicada.
- **`NexusCompleter`** — Autocompletar com TAB. Combina comandos internos com binários do `PATH`.

### Roteamento e agentes

- **`BrowserClient`** — Cliente de raciocínio via Microsoft Copilot no navegador Edge.
- **`EdgeCopilotBrowser`** — Controle do navegador via Playwright. Leitura de respostas com estabilização de DOM.
- **`KeyPool`** — Pool de chaves com failover controlado.
- **`QuotaManager`** — Controle conservador de quotas locais (RPM, TPM, RPD).
- **`RateLimiter`** — Limitador de taxa entre chamadas.

### Orquestrador cognitivo

- **`CognitiveOrchestrator`** — Ciclo autônomo: planeja, executa, valida, detecta estagnação e completa.
- **`SafetyGuard`** — Classificação de comandos por categoria de segurança.
- **`CompletionDetector`** — Detecta quando o objetivo foi atingido.
- **`StallDetector`** — Detecta estagnação do orquestrador.

### Modo silencioso

- **`SessionManager`** — CRUD de sessões com validação por expiração e perfil.
- **`AuthManager`** — Flag file de autorização; login inicial via browser.
- **`TerminalExecutor`** — Execução via PTY com categorização de segurança.
- **`HeadlessController`** — Gatekeeper de browser e apps gráficos.
- **`FallbackManager`** — Classificação GUI_REQUIRED vs TERMINAL.
- **`SilentModeController`** — Coordenador de todos os componentes.

### Evolução

- **`EvolutionSourceIndex`** — Indexação de seções do código-fonte.
- **`EvolutionSectionLocator`** — Localização de seções por AST/texto.
- **`EvolutionContextBuilder`** — Construção de contexto para o agente de evolução.

### Workspaces e code intelligence

- **`TemporaryWorkspaceManager`** — Criação, venv, testes, instalação de deps, cleanup.
- **`CodeIntelligenceEngine`** — Integração com AST, Ruff, Pyright e Jedi.

---

## Configuração

O arquivo de configuração é gerado em `~/.nexus/config.json` (ou `NEXUS_DATA_DIR/config.json`).

Principais opções:

| Chave | Padrão | Descrição |
|---|---|---|
| `model` | `microsoft-copilot` | Modelo de raciocínio |
| `browser_timeout` | `30000` | Timeout de inicialização do navegador (ms) |
| `browser_response_timeout` | `120000` | Timeout de resposta do Copilot (ms) |
| `terminal_timeout` | `300` | Timeout de comandos no PTY (s) |
| `generated_script_timeout` | `300` | Timeout de scripts Python gerados (s) |
| `workspace_isolation` | `true` | Isolamento de scripts em workspace temporário |
| `dangerous_always_confirm` | `true` | Confirmação obrigatória para comandos perigosos |
| `compact_protocol_enabled` | `true` | Compactação NCP/1 de payloads |
| `silent_mode` | `false` | Modo silencioso terminal-first |
| `max_auto_retries` | `12` | Tentativas por arquivo no modo automático |

---

## Variáveis de ambiente

| Variável | Descrição |
|---|---|
| `NEXUS_PROJECT_DIR` | Diretório base do projeto |
| `NEXUS_DATA_DIR` | Diretório de dados (config, scripts, sessões) |
| `NEXUS_CODE_ROOT` | Diretório raiz para projetos `/code` |
| `NEXUS_EDGE_PROFILE` | Diretório de perfil do Edge |
| `NEXUS_EDGE_HEADLESS` | `1` para Edge headless |
| `NEXUS_EDGE_EXECUTABLE` | Caminho do executável do Edge |
| `NEXUS_COPILOT_URL` | URL do Copilot |

---

## Como testar

### Teste do gerador de projetos com 7 fases

```
/code crie um site simples
```

O NEXUS irá:

1. Consultar o Copilot: "Como criar um site simples? Quais arquivos são necessários?"
2. Exibir o menu de planejamento com 7 fases e ações identificadas
3. Pedir confirmação para iniciar
4. Gerar cada arquivo individualmente com validação
5. Mostrar progresso em tempo real
6. Validar integração, testes e resultado final
7. Declarar conclusão somente após a Fase 7

### Teste do modo silencioso

```
/silent-on
/silent
/silent-off
```

### Teste do orquestrador cognitivo

```
/orquestrar verifique o uso de disco e memória do sistema
```

### Diagnóstico local completo

```
/self-test
```

---

## Requisitos

- Python 3.10+
- Linux com bash
- Microsoft Edge (canal `msedge`)
- Playwright (`pip install playwright && playwright install msedge`)
- pexpect
- Opcionais para code intelligence: Ruff, Pyright, Jedi

---

## Segurança

- Comandos perigosos exigem confirmação explícita
- Scripts Python passam por AST + py_compile antes da execução
- Sessões armazenadas com permissão 0600; nenhum cookie/token em logs
- Modo silencioso nunca contorna CAPTCHA, MFA ou OAuth
- Workspaces temporários são isolados e limpos após uso
- Nenhuma API key é incorporada no código ou logs
- Validação de estrutura global verifica todos os arquivos ao final
- Detecção de múltiplos arquivos rejeita respostas que desviam do plano

