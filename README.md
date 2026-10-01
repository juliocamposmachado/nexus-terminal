# NEXUS TERMINAL

**NEXUS TERMINAL** é um agente Linux escrito em Python com PTY real, integração com Gemini, execução de comandos, roteamento local e pool ilimitado de chaves com rotação sequencial.

<img width="1365" height="717" alt="image" src="https://github.com/user-attachments/assets/3a2a0fbd-bce2-4a74-a65e-8fe696224974" />


Esta versão utiliza o fluxo **One-Shot**:

```text
Pedido do usuário
      ↓
Roteador local
      ↓
Uma chamada Gemini
      ↓
Resposta ou comando Linux
      ↓
Execução imediata no PTY real
```

Não existe mais um pipeline obrigatório de interpretação, planejamento, decisão, execução e validação para cada pedido.

## Principais características

- Uma chamada lógica de API por pedido técnico;
- Próximo pedido utiliza a próxima chave do pool;
- Failover automático somente quando a chave atual falha;
- Quantidade ilimitada de chaves do ponto de vista da aplicação;
- Aceita 1, 10, 100 ou mais chaves, conforme os recursos disponíveis;
- Suporte a bloco de chaves com aspas, vírgulas e linhas vazias;
- Remoção automática de duplicatas preservando a ordem;
- Suporte a variáveis de ambiente `NEXUS_GEMINI_KEY_1..N`;
- Suporte a `NEXUS_GEMINI_KEYS` para um bloco completo;
- PTY Linux real persistente com `pexpect`;
- Captura de saída e código de saída dos comandos;
- Execução imediata do comando retornado pela IA;
- Confirmação para comandos potencialmente perigosos;
- Roteador local para comandos simples sem consumir API;
- Autocomplete de comandos, caminhos e comandos internos;
- Configuração persistente com permissão `0600`;
- Histórico protegido;
- Limpeza do eco do shell, prompts de heredoc e marcadores internos do PTY;
- Teste local sem consumir API.

## Requisitos

- Linux;
- Python 3.10 ou superior;
- Bash ou shell compatível;
- Acesso à internet para chamadas Gemini;
- Uma ou mais chaves Gemini válidas;
- Pacotes Python `pexpect` e `requests`.

## Instalação

Clone o repositório:

```bash
git clone https://github.com/SEU_USUARIO/SEU_REPOSITORIO.git
cd SEU_REPOSITORIO
```

Instale as dependências:

```bash
python3 -m pip install --user pexpect requests
```

Em distribuições que exigem a opção de sistema gerenciado:

```bash
python3 -m pip install --user --break-system-packages pexpect requests
```

Dê permissão de execução ao programa:

```bash
chmod +x nexus.py
```

## Execução

Execute:

```bash
python3 nexus.py
```

Ou:

```bash
./nexus.py
```

Verifique a versão:

```bash
python3 nexus.py --version
```

## Configuração das chaves

Dentro do programa, execute:

```text
/setup
```

Cole as chaves em um bloco. O programa aceita os seguintes formatos.

### Uma chave por linha

```text
CHAVE_1
CHAVE_2
CHAVE_3
```

### Formato com aspas e vírgulas

```text
"CHAVE_1",

"CHAVE_2",

"CHAVE_3",
```

### Formato separado por vírgulas

```text
CHAVE_1, CHAVE_2, CHAVE_3
```

Depois de colar o bloco, finalize com uma linha vazia.

O programa irá:

- remover aspas;
- remover vírgulas;
- ignorar linhas vazias;
- remover espaços extras;
- remover chaves duplicadas;
- preservar a ordem;
- salvar as chaves com permissão `0600`.

As chaves são armazenadas em:

```text
~/.config/nexus/config.json
```

O arquivo não deve ser enviado ao GitHub.

## Variáveis de ambiente

Também é possível configurar as chaves por variáveis de ambiente, sem gravá-las no código:

```bash
export NEXUS_GEMINI_KEY_1="SUA_CHAVE_1"
export NEXUS_GEMINI_KEY_2="SUA_CHAVE_2"
export NEXUS_GEMINI_KEY_3="SUA_CHAVE_3"
```

Não existe limite fixo no número da variável. Por exemplo:

```bash
export NEXUS_GEMINI_KEY_25="SUA_CHAVE_25"
export NEXUS_GEMINI_KEY_100="SUA_CHAVE_100"
```

Também é possível usar um bloco completo:

```bash
export NEXUS_GEMINI_KEYS='"SUA_CHAVE_1", "SUA_CHAVE_2", "SUA_CHAVE_3"'
```

A ordem de carregamento é:

1. Chaves salvas em `config.json`;
2. Variáveis `NEXUS_GEMINI_KEY_N`, ordenadas numericamente;
3. Conteúdo de `NEXUS_GEMINI_KEYS`;
4. Duplicatas removidas preservando a primeira ocorrência.

## Como funciona a rotação

Cada pedido técnico usa uma chamada lógica:

```text
Pedido 1 → chave #1 → resposta ou comando
Pedido 2 → chave #2 → resposta ou comando
Pedido 3 → chave #3 → resposta ou comando
```

Se uma chave falhar durante o pedido:

```text
Pedido 1 → chave #1 → HTTP 429
Pedido 1 → chave #2 → mesma solicitação
```

A próxima chave recebe a mesma solicitação original.

Falhas consideradas para failover incluem:

- HTTP `429`;
- quota excedida;
- HTTP `401` ou `403`;
- timeout;
- erro de rede;
- HTTP `5xx`;
- indisponibilidade temporária.

Durante a mesma tarefa, uma chave que falhou não é repetida indefinidamente. Em um novo pedido, o estado temporário de falha é limpo e a rotação continua a partir do cursor atual.

## Formato da resposta da IA

A chamada One-Shot solicita JSON no seguinte formato:

```json
{
  "response": "resposta textual opcional",
  "command": "comando Linux opcional",
  "reason": "motivo"
}
```

Se `command` estiver preenchido, o NEXUS:

1. mostra o comando;
2. solicita confirmação quando necessário;
3. executa o comando no PTY real;
4. mostra a saída;
5. mostra o código de saída.

Se somente `response` estiver preenchido, o texto é exibido sem executar comandos.

## Exemplos de uso

### Comando resolvido localmente, sem API

```text
/nexus mostre meu diretório
```

```text
/nexus mostre a memória
```

```text
/nexus liste os arquivos
```

### Pedido técnico com uma chamada

```text
/nexus mostre a versão do kernel Linux
```

A IA pode retornar:

```json
{
  "response": "",
  "command": "uname -r",
  "reason": "Consulta local do kernel"
}
```

### Criar e executar um script Bash

```text
/nexus crie um script Bash temporário em /tmp/nexus-test.sh, execute-o e mostre o resultado. Use um único comando shell seguro.
```

### Múltiplos passos no PTY

```text
/nexus crie um diretório em /tmp/nexus-data, gere um arquivo com cinco linhas, ordene seu conteúdo e crie um relatório com a quantidade de linhas
```

## Comandos internos

| Comando | Função |
|---|---|
| `/nexus <tarefa>` | Faz uma chamada One-Shot e executa a ação retornada |
| `/nexus --auto <tarefa>` | Executa automaticamente comandos não perigosos |
| `/nexus --fast <tarefa>` | Modo compatível de execução rápida |
| `/nexus stop` | Interrompe a tarefa atual |
| `/status` | Mostra o estado do NEXUS e do pool |
| `/keys` | Mostra a saúde das chaves sem revelar os valores |
| `/setup` | Configura ou substitui o bloco de chaves |
| `/reset-limits` | Remove cooldowns das chaves |
| `/config` | Mostra a configuração sem imprimir as chaves |
| `/self-test` | Executa diagnóstico local sem consumir API |
| `/clear` | Limpa o terminal |
| `/help` | Mostra a ajuda |
| `/exit` | Encerra o programa |

## Testes

### Teste de sintaxe

```bash
python3 -m py_compile nexus.py
```

### Self-test

```bash
python3 nexus.py --self-test
```

O self-test verifica:

- versão do Python;
- `pexpect`;
- `requests`;
- `readline`;
- shell disponível;
- comandos básicos;
- PTY real;
- código de saída do processo.

Esse teste não consome API.

### Teste do PTY

Dentro do NEXUS:

```text
printf 'NEXUS_TEST_OK'
```

O resultado esperado inclui:

```text
NEXUS_TEST_OK
[exit code: 0]
```

### Teste de pipeline

```text
printf 'banana\nlaranja\nbanana\nuva\n' | sort | uniq -c | sort -nr
```

### Teste de script Bash

```text
printf '%s\n' '#!/usr/bin/env bash' 'set -euo pipefail' 'mkdir -p /tmp/nexus-test' 'printf "%s\n" alpha beta gamma > /tmp/nexus-test/input.txt' 'sort /tmp/nexus-test/input.txt > /tmp/nexus-test/sorted.txt' 'wc -l /tmp/nexus-test/sorted.txt' > /tmp/nexus-test.sh && chmod +x /tmp/nexus-test.sh && /tmp/nexus-test.sh
```

## Segurança

- Nunca coloque chaves reais no código-fonte;
- Nunca envie `config.json` para o GitHub;
- Nunca publique chaves em issues, logs ou screenshots;
- O programa não imprime chaves completas;
- A configuração é gravada com permissão `0600`;
- Comandos potencialmente perigosos pedem confirmação;
- O PTY permanece real, portanto comandos confirmados têm efeito no sistema;
- Revogue chaves que tenham sido expostas publicamente;
- Use um arquivo `.gitignore` apropriado.

Exemplo de `.gitignore`:

```gitignore
__pycache__/
*.pyc
.env
config.json
*.log
.DS_Store
```

A configuração normalmente fica fora do repositório, em `~/.config/nexus/`.

## Arquitetura

```text
Entrada do usuário
        ↓
Router local
        ├── conversa local
        ├── comando local
        └── pedido técnico
                ↓
         Gemini One-Shot
                ↓
       response ou command
                ↓
          Confirmação
                ↓
             PTY real
                ↓
       saída + exit code
```

O pool de chaves mantém, para cada chave:

- quantidade de chamadas;
- quantidade de HTTP 429;
- quantidade de erros;
- quantidade de erros de autenticação;
- cooldown individual.

## Arquivos principais

```text
nexus.py                       versão principal para execução
nexus_terminal_sequencial.py   cópia da versão sequencial
nexus_fixed.py                 cópia corrigida do núcleo
README.md                      documentação
```

A interface gráfica não faz parte desta versão de uso. O projeto é executado exclusivamente pelo código Python no terminal.

## Limitações conhecidas

- Uma única chamada por pedido reduz o custo e a latência, mas não faz validação automática posterior do resultado;
- Se todas as chaves pertencerem ao mesmo projeto Google, elas podem compartilhar a mesma quota;
- HTTP 429 pode indicar limite do projeto, modelo ou conta, e não apenas uma chave individual;
- O comando retornado pela IA deve ser revisado antes da confirmação;
- O PTY executa comandos reais no sistema;
- Chaves inválidas ou revogadas são isoladas temporariamente pelo failover.

## Licença

Adicione aqui a licença escolhida para o projeto, por exemplo:

```text
MIT License
```

Não publique chaves Gemini reais no repositório.
