<img width="1365" height="717" alt="image" src="https://github.com/user-attachments/assets/93bab8d1-3608-4653-a878-afec548c237a" />

<img width="1371" height="761" alt="image" src="https://github.com/user-attachments/assets/657f3a1c-47c1-4a39-b455-497f5aa78c79" />

<img width="1372" height="524" alt="image" src="https://github.com/user-attachments/assets/1a15adb9-01c2-44b0-8270-18c6b0394a9d" />

<img width="1373" height="764" alt="image" src="https://github.com/user-attachments/assets/84c11ad3-b328-4e51-9cb2-40f1bb230a4e" />

# NEXUS TERMINAL

**NEXUS TERMINAL** é um terminal inteligente em Python com roteamento local, integração com a API Gemini, execução controlada de comandos Linux, geração e validação de programas Python, gestão de quotas, pool de API keys, failover e observabilidade detalhada no terminal.

Versão documentada: **6.6.1-AGENT-GENERATOR-ROBUST**

> O NEXUS foi projetado para mostrar o que está acontecendo: cada etapa do pipeline, chamada de agente, espera do rate limiter, resposta HTTP, retry, execução e validação são exibidos no terminal.

## Índice

- [Recursos](#recursos)
- [Requisitos](#requisitos)
- [Instalação](#instalação)
- [Configuração da API Gemini](#configuração-da-api-gemini)
- [Execução](#execução)
- [Comandos interativos](#comandos-interativos)
- [Uso do `/nexus`](#uso-do-nexus)
- [Uso do `/evoluir`](#uso-do-evoluir)
- [Arquitetura](#arquitetura)
- [Resiliência de APIs](#resiliência-de-apis)
- [Logs e observabilidade](#logs-e-observabilidade)
- [Configuração avançada](#configuração-avançada)
- [Segurança](#segurança)
- [Testes e diagnóstico](#testes-e-diagnóstico)
- [Estrutura de arquivos](#estrutura-de-arquivos)
- [Solução de problemas](#solução-de-problemas)
- [Limitações e responsabilidade](#limitações-e-responsabilidade)

## Recursos

- Roteamento local de pedidos simples sem chamada de API quando possível.
- Pipeline inteligente com:
  - interpretação;
  - planejamento;
  - decisão de rota;
  - programação Python ou comando Linux;
  - execução real;
  - validação;
  - conclusão.
- Integração com a API Gemini via HTTP/REST usando `requests`.
- Pool de múltiplas API keys com rotação controlada.
- Failover para falhas de autenticação, rede e servidor.
- Tratamento de HTTP 429 com cooldown global e respeito ao cabeçalho `Retry-After`.
- Timeout configurável e backoff exponencial limitado.
- Controle local de RPM, RPD, tokens e cooldowns.
- Geração de scripts Python em arquivos reais.
- Validação por AST e `py_compile` antes da execução.
- Confirmação para comandos ou scripts potencialmente perigosos.
- Comando `/evoluir` para modificar somente uma função e salvar uma nova versão.
- Histórico do readline e autocomplete de comandos/caminhos.
- Painéis, timestamps, barras de progresso e logs de cada etapa no terminal.
- API keys ocultas nos logs e redigidas em mensagens de erro.

## Requisitos

- Python **3.10 ou superior**;
- Linux, macOS ou ambiente compatível com `bash` e pseudo-terminal;
- acesso à internet para utilizar a API Gemini;
- uma chave da API Gemini;
- pacotes Python:
  - `pexpect`;
  - `requests`.

O NEXUS utiliza recursos modernos de tipagem do Python, como `dict[str, Any]` e `str | None`. Por isso, recomenda-se Python 3.10+.

## Instalação

### 1. Clone o repositório

```bash
git clone https://github.com/SEU_USUARIO/SEU_REPOSITORIO.git
cd SEU_REPOSITORIO
```

Substitua a URL pelo endereço real do repositório.

### 2. Instale as dependências

Instalação apenas para o usuário atual:

```bash
python3 -m pip install --user pexpect requests
```

Ou, preferencialmente, crie um ambiente virtual:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install pexpect requests
```

### 3. Valide a instalação

```bash
python3 -m py_compile nexus.py
python3 nexus.py --self-test
```

O diagnóstico deve indicar, entre outros itens:

```text
[OK] pexpect
[OK] requests
[OK] AST validation
[OK] py_compile
[OK] PTY REAL funcionando
SELF TEST FINALIZADO
```

### 4. Torne o arquivo executável, opcionalmente

```bash
chmod +x nexus.py
```

Depois, ele poderá ser executado diretamente:

```bash
./nexus.py
```

## Configuração da API Gemini

O NEXUS pode receber as chaves de duas formas.

### Opção A — configuração interativa

Execute:

```bash
python3 nexus.py --setup
```

Ou, dentro do programa:

```text
/setup
```

Cole uma chave por linha e finalize com uma linha vazia.

As chaves são armazenadas em:

```text
~/.config/nexus/config.json
```

O diretório recebe permissões restritivas e o arquivo de configuração é salvo com permissão `0600`.

### Opção B — variáveis de ambiente

Uma chave:

```bash
export NEXUS_GEMINI_KEY_1="SUA_CHAVE_GEMINI"
```

Múltiplas chaves:

```bash
export NEXUS_GEMINI_KEY_1="CHAVE_1"
export NEXUS_GEMINI_KEY_2="CHAVE_2"
export NEXUS_GEMINI_KEY_3="CHAVE_3"
```

Também é possível usar a variável agregada:

```bash
export NEXUS_GEMINI_KEYS="CHAVE_1,CHAVE_2,CHAVE_3"
```

As chaves configuradas pelo arquivo e pelo ambiente são deduplicadas. O NEXUS nunca deve exibir a chave no terminal, em logs ou em mensagens de erro.

> Não coloque chaves reais no Git, no README, em issues ou em commits. Use variáveis de ambiente ou um arquivo local fora do controle de versão.

## Execução

Inicie o terminal inteligente com:

```bash
python3 nexus.py
```

Na inicialização, o NEXUS informa:

- versão;
- modelo configurado;
- intervalo mínimo entre chamadas;
- quota local;
- localização dos scripts gerados;
- quantidade de chaves disponíveis.

Se nenhuma chave estiver configurada, comandos locais ainda poderão funcionar, mas as tarefas que exigem IA retornarão uma mensagem solicitando `/setup` ou a configuração de uma variável de ambiente.

## Comandos interativos

| Comando | Função |
|---|---|
| `/nexus <tarefa>` | Executa o pipeline inteligente normal. |
| `/nexus --auto <tarefa>` | Ativa modo automático para comandos não perigosos. |
| `/nexus --fast <tarefa>` | Reduz as etapas de planejamento. |
| `/nexus --auto --fast <tarefa>` | Combina os dois modos. |
| `/nexus stop` | Interrompe a tarefa e envia interrupção ao PTY. |
| `/agente` | Gera um pequeno agente Python a partir de uma personalidade. |
| `/evoluir` | Evolui uma função específica e salva uma nova versão. |
| `/status` | Mostra estado do NEXUS, PTY, pool e quotas. |
| `/quota` | Mostra contadores e limites locais de quota. |
| `/quota-reset` | Zera os contadores locais de quota. |
| `/keys` | Mostra a saúde do pool sem exibir as chaves. |
| `/setup` | Configura as chaves Gemini interativamente. |
| `/reset-limits` | Remove cooldowns do pool e cooldown global. |
| `/config` | Mostra a configuração carregada, ocultando o conteúdo das chaves. |
| `/scripts` | Lista scripts Python gerados. |
| `/self-test` | Executa diagnóstico local. |
| `/clear` | Limpa a tela do terminal. |
| `/help` | Mostra a ajuda incorporada. |
| `/exit` | Encerra o NEXUS. |

Qualquer entrada que não seja um comando interno é encaminhada ao PTY local.

## Uso do `/nexus`

Exemplos:

```text
NEXUS> /nexus liste os arquivos do diretório atual
```

Pedidos simples podem ser roteados localmente para comandos como `pwd` ou `ls`, evitando consumo de API quando a rota local reconhece a solicitação.

Para uma tarefa que exige análise ou programação:

```text
NEXUS> /nexus analise os arquivos Python deste diretório e gere um relatório
```

O fluxo exibirá painéis semelhantes a:

```text
┌── PIPELINE NEXUS ────────────────────────────────────┐
│ Modo: normal                                         │
│ Complexidade: alta                                   │
│ Logs locais no terminal; sem chamadas extras         │
└─────────────────────────────────────────────────────┘
[NEXUS PROGRESS] Interpretação       [████······················] 1/7
```

## Uso do `/agente`

O comando `/agente` gera um pequeno agente Python independente a partir de uma personalidade e de um objetivo informados pelo usuário.

Execute:

```text
NEXUS> /agente
```

O NEXUS perguntará:

```text
Qual a personalidade do agente?
Nome do agente (Enter = agente_personalizado):
Qual é o objetivo principal do agente?
```

O gerador cria um programa Python completo com:

- `SYSTEM_PROMPT` incorporando a personalidade escolhida;
- protocolo `code_lines`, que reduz falhas de escape JSON durante a geração;
- segunda tentativa automática quando a resposta JSON vier inválida;
- loop de conversa no terminal;
- chamada à API Gemini via `requests`;
- leitura de chave por `NEXUS_GEMINI_KEY_1`, `GEMINI_API_KEY` ou `NEXUS_GEMINI_KEYS`;
- timeout configurável por `AGENT_API_TIMEOUT`;
- tratamento de ausência de chave, timeout, rede e HTTP 4xx/5xx;
- saída por `/sair`, `/exit` ou Ctrl+C;
- nenhuma execução de shell;
- nenhuma chave hardcoded no código gerado.

Exemplo de solicitação:

```text
Qual a personalidade do agente? Você é um tutor paciente de Python, didático e objetivo.
Nome do agente: tutor_python
Qual é o objetivo principal do agente? Ensinar programação Python com exemplos curtos e exercícios práticos.
```

O arquivo será salvo em:

```text
~/.config/nexus/generated/agente_tutor_python_YYYYMMDD_HHMMSS_<id>.py
```

Depois de configurar uma API key, execute o agente com:

```bash
python3 ~/.config/nexus/generated/agente_tutor_python_*.py
```

As dependências declaradas pelo agente são apenas informativas. O NEXUS não instala pacotes automaticamente. Revise o arquivo gerado e valide-o antes de uso:

```bash
python3 -m py_compile ~/.config/nexus/generated/agente_tutor_python_*.py
```

## Uso do `/evoluir`

O `/evoluir` altera somente uma função ou método, valida o resultado e salva uma nova versão sem sobrescrever o arquivo original.

Execute:

```text
NEXUS> /evoluir
```

O NEXUS perguntará:

```text
Arquivo Python alvo [/caminho/atual/nexus.py]:
Qual parte/classe? (Enter se estiver no módulo):
Qual função/método?
Qual evolução deseja aplicar?
```

### Exemplo — evoluir um método de uma classe

Arquivo de teste:

```python
class Calculadora:
    def somar(self, a, b):
        return a + b
```

Respostas:

```text
Arquivo Python alvo: /caminho/exemplo.py
Qual parte/classe? (Enter se estiver no módulo): Calculadora
Qual função/método? somar
Qual evolução deseja aplicar? Aceite números como strings, converta-os para float antes da soma e preserve o nome e a assinatura do método.
```

O agente recebe somente a função selecionada. O NEXUS exige que a resposta contenha exatamente uma função com o mesmo nome. Depois:

1. analisa o arquivo com AST;
2. localiza a função pelo nome e pela classe, se informada;
3. envia somente o trecho selecionado ao agente de evolução;
4. rejeita código vazio, inválido ou incompleto;
5. substitui somente o intervalo da função em memória;
6. valida o arquivo final com AST;
7. salva uma nova versão;
8. executa `py_compile` na nova versão;
9. preserva o arquivo original.

O arquivo criado segue este padrão:

```text
exemplo_evolucao_20261002_192500_a1b2c3.py
```

### Recomendações para pedidos de evolução

Prefira pedidos específicos:

```text
Evolua somente a função ask da classe GeminiClient para respeitar api_timeout, tratar erros HTTP 429, usar Retry-After e preservar o formato atual de retorno. Não altere outras funções.
```

Evite pedidos vagos como:

```text
Evolua o uso de APIs.
```

Se houver duas funções com o mesmo nome em classes diferentes, informe a classe para desambiguar.

> O `/evoluir` gera e valida a nova versão, mas não executa automaticamente o código evoluído. Revise o diff antes de substituir ou publicar o arquivo.

## Arquitetura

O fluxo principal é:

```text
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
┌───────────────────────┬──────────────────────┐
│ comando Linux          │ programa Python      │
│ confirmação            │ arquivo real        │
└───────────────────────┴──────────────────────┘
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
```

### Agentes internos

- **RESPOSTA ÚNICA**: resolve tarefas simples em uma única decisão.
- **INTERPRETAÇÃO**: transforma o pedido em especificação.
- **PLANEJAMENTO**: cria um plano executável.
- **DECISÃO**: escolhe resposta, comando ou Python.
- **PROGRAMAÇÃO**: gera programa Python completo.
- **VALIDAÇÃO**: avalia o resultado real.
- **CONCLUSÃO**: resume o que foi observado.
- **EVOLUÇÃO**: produz uma substituição para uma única função.

## Resiliência de APIs

A versão atual inclui uma camada específica para chamadas Gemini.

### Timeout

O tempo máximo de uma chamada é controlado por:

```json
"api_timeout": 90
```

O timeout impede que uma requisição fique bloqueada indefinidamente.

### Retry e backoff

Falhas temporárias de rede, timeout e respostas HTTP 5xx podem ser repetidas de acordo com:

```json
"max_retries": 1,
"api_backoff_base": 2,
"api_backoff_max": 30
```

O backoff é exponencial, mas limitado pelo valor máximo configurado.

### HTTP 429

Quando o provedor retorna HTTP 429, o NEXUS:

- lê `Retry-After`, quando presente;
- aplica cooldown global;
- registra o evento na quota local;
- não faz rotação cega pelas demais chaves;
- informa a espera no terminal;
- evita tratar várias chaves do mesmo projeto como solução para uma quota compartilhada.

### HTTP 401 e 403

Erros de autenticação ou autorização marcam a chave como problemática e permitem failover controlado para outra chave disponível, respeitando `max_key_failover`.

### Quota local

Os controles locais são mecanismos conservadores de proteção operacional. Eles **não representam a quota oficial do Google**.

Configurações relevantes:

```json
"quota_soft_rpm": 6,
"quota_soft_tpm": 0,
"quota_soft_rpd": 0,
"quota_global_cooldown": 60,
"quota_persist": true
```

## Logs e observabilidade

A observabilidade é local e não cria chamadas adicionais à API.

O terminal exibe:

- timestamp de cada evento;
- etapa atual;
- progresso do pipeline;
- agente e chamada atual;
- modelo e tamanho aproximado do payload;
- timeout configurado;
- número da chave selecionada, nunca o valor da chave;
- retry, failover e backoff;
- status HTTP e tempo de resposta;
- tempo total da API;
- execução no PTY;
- validação AST/compilação;
- resultado final.

Os logs não adicionam espera artificial. As únicas esperas exibidas são as já necessárias para:

- `api_min_interval`;
- retry/backoff;
- cooldown de quota;
- timeout da chamada;
- cooldown de execução local, quando configurado.

## Configuração avançada

A configuração padrão fica definida em `DEFAULT_CONFIG` e pode ser complementada ou substituída por:

```text
~/.config/nexus/config.json
```

Exemplo de configuração:

```json
{
  "model": "gemini-3.6-flash",
  "temperature": 0.1,
  "max_output_tokens": 3000,
  "programming_max_output_tokens": 10000,
  "api_timeout": 90,
  "api_backoff_base": 2,
  "api_backoff_max": 30,
  "terminal_timeout": 300,
  "generated_script_timeout": 300,
  "api_min_interval": 8,
  "max_key_failover": 5,
  "max_retries": 1,
  "quota_soft_rpm": 6,
  "quota_soft_tpm": 0,
  "quota_soft_rpd": 0,
  "quota_global_cooldown": 60,
  "quota_persist": true,
  "dangerous_always_confirm": true,
  "max_output_chars": 8000,
  "request_max_chars": 20000,
  "max_generated_script_chars": 200000,
  "keep_generated_scripts": true,
  "terminal_logs": true
}
```

Para visualizar a configuração carregada:

```text
NEXUS> /config
```

A lista de chaves é indicada como configurada ou vazia, sem exibir os valores.

## Segurança

O NEXUS adota as seguintes medidas:

- não exibe API keys no terminal;
- redige chaves em mensagens de erro;
- salva configuração com permissões restritivas;
- usa escrita atômica para arquivos persistentes;
- sanitiza nomes de scripts gerados;
- valida código Python com AST e `py_compile`;
- detecta placeholders e código incompleto;
- pede confirmação para comandos perigosos;
- pede confirmação para scripts com operações sensíveis;
- não instala automaticamente dependências declaradas por um script gerado;
- preserva o arquivo original durante `/evoluir`.

### Comandos perigosos

Comandos como remoção destrutiva, formatação de dispositivos, reboot, shutdown e alterações amplas de permissões são reconhecidos e exigem confirmação adicional quando aplicável.

Ainda assim, nenhuma camada automática substitui a revisão humana. Leia comandos, scripts e diffs antes de executá-los em ambientes importantes.

## Testes e diagnóstico

### Compilação

```bash
python3 -m py_compile nexus.py
```

### Autoteste

```bash
python3 nexus.py --self-test
```

O autoteste verifica:

- versão do Python;
- `pexpect`;
- `requests`;
- `readline`;
- configuração;
- quota manager;
- comandos básicos do sistema;
- validação AST;
- `py_compile`;
- inicialização do PTY.

### Verificação de versão

```bash
python3 nexus.py --version
```

### Teste manual de `/evoluir`

Crie um arquivo pequeno:

```bash
cat > exemplo_evoluir.py <<'PY'
class Calculadora:
    def somar(self, a, b):
        return a + b

if __name__ == "__main__":
    print(Calculadora().somar(2, 3))
PY
```

Teste o original:

```bash
python3 exemplo_evoluir.py
```

Use `/evoluir` no NEXUS, aponte para `exemplo_evoluir.py`, informe `Calculadora`, `somar` e uma evolução específica. Depois confira:

```bash
ls -lah exemplo_evoluir*
python3 -m py_compile exemplo_evolucao_*.py
```

O arquivo original deve permanecer no lugar.

## Estrutura de arquivos

```text
.
├── nexus.py                 # aplicação principal
├── README.md                # documentação do projeto
├── .venv/                   # opcional; ambiente virtual local
└── ...                      # arquivos do repositório
```

Arquivos gerados em tempo de execução:

```text
~/.config/nexus/
├── config.json              # configuração e, se escolhida, chaves
├── history                  # histórico readline
├── quota_state.json         # contadores locais persistentes
└── generated/               # scripts gerados pelo pipeline
```

O `/evoluir` salva a nova versão no diretório do arquivo alvo, seguindo o padrão `*_evolucao_YYYYMMDD_HHMMSS_<id>.py`.

## Solução de problemas

### `Dependência ausente: pexpect`

Instale as dependências:

```bash
python3 -m pip install --user pexpect requests
```

Se estiver usando ambiente virtual, ative-o antes.

### `Nenhuma API key configurada`

Configure uma chave:

```bash
python3 nexus.py --setup
```

ou:

```bash
export NEXUS_GEMINI_KEY_1="SUA_CHAVE_GEMINI"
```

### `Todas as chaves disponíveis estão em cooldown`

Verifique:

```text
NEXUS> /keys
NEXUS> /quota
NEXUS> /status
```

O cooldown pode ser removido com:

```text
NEXUS> /reset-limits
```

Esse comando remove cooldowns locais, mas não remove limites impostos pelo provedor.

### HTTP 429

HTTP 429 indica limitação de quota ou frequência. Aguarde o cooldown informado, revise `api_min_interval`, `quota_soft_rpm` e a quota oficial do projeto no Google AI Studio. Não presuma que adicionar mais chaves resolverá uma quota global compartilhada.

### `Função não encontrada` no `/evoluir`

Confira:

- o caminho absoluto do arquivo;
- o nome exato da função;
- a classe correta, quando for método;
- se a função é realmente `def` ou `async def` no escopo esperado.

### Função ambígua no `/evoluir`

Informe o nome da classe em **Qual parte/classe?**. Isso evita alterar a função errada.

### Falha de AST ou `py_compile`

A nova versão não deve ser usada. Revise a mensagem, mantenha o arquivo original e ajuste o pedido de evolução para ser mais específico.

## Limitações e responsabilidade

- O NEXUS depende da disponibilidade, autenticação, modelo e quotas da API Gemini.
- As quotas locais são estimativas de proteção e não substituem os limites oficiais do provedor.
- O modo automático não elimina todos os riscos; comandos e scripts devem ser revisados.
- Código gerado por IA deve ser auditado antes de uso em produção.
- O `/evoluir` modifica uma função por vez e não garante correção semântica completa do programa.
- O projeto não instala dependências automaticamente para scripts gerados.
- O arquivo original deve ser versionado com Git antes de evoluções importantes.

## Fluxo recomendado para contribuição

Antes de enviar alterações ao GitHub:

```bash
python3 -m py_compile nexus.py
python3 nexus.py --self-test
git diff --check
git status
```

Depois, revise especialmente:

- alterações no cliente de API;
- logs e possíveis vazamentos de segredo;
- tratamento de timeout, 429 e 5xx;
- comportamento do `/evoluir`;
- permissões dos arquivos gerados;
- compatibilidade com o Python suportado.

## Licença

Nenhum arquivo de licença foi presumido nesta documentação. Defina e adicione uma licença explícita ao repositório antes de publicar o projeto, conforme a intenção do mantenedor.
