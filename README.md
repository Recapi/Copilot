# Guia: Copilot CLI com pouca cota — harness de 3 papéis + orçamento por dias úteis

Escrito em 29/08/2026, com base em pesquisa verificada nos docs oficiais do
GitHub. Tudo nesta pasta roda em **Python puro, sem instalar nada** — de
propósito, porque o PC do trabalho não tem admin.

---

## 1. Primeiro: os nomes que você tentou lembrar EXISTEM

Você não inventou nada — lembrou de um combo real de ferramentas de economia
de tokens que circula em artigos de 2026:

| Você disse | Nome real | O que faz |
|---|---|---|
| "harmens" | **harness** | O "arnês": o script que amarra os modelos num fluxo. É o `planejar`/`rodar` do `copiloto.py`. |
| "RTK" | **rtk** (rtk-ai/rtk) | Comprime a saída de comandos (git, npm, ls...) antes de chegar ao modelo. Corta 60–90% desses tokens. |
| "headroom" | **Headroom** (headroomlabs-ai/headroom) | Comprime contexto (logs, JSON, arquivos) antes de mandar pro modelo. |
| "mancave" | **Caveman** (sílabas invertidas: cave-man) | Faz o modelo responder telegráfico — corta tokens de saída (números auto-reportados). |
| "ponytail" | **Ponytail** (DietrichGebert/ponytail) | Regra "dev sênior preguiçoso": gera o mínimo de código que resolve (YAGNI). |

Nenhuma delas é orquestrador — são acessórios de economia. O orquestrador é o
harness. Das quatro, as que valem no seu caso: **Ponytail** e **rtk** (ambas
dizem suportar Copilot CLI). Caveman/Headroom são opcionais depois.

## 2. Descoberta importante sobre a sua cota ("10000 AIA")

O Copilot **mudou de modelo de cobrança em 01/06/2026**: saiu de "premium
requests" e virou **GitHub AI Credits** (1 crédito = US$ 0,01), cobrados por
**token** — cada rodada do agente consome, e sessões longas custam mais.
"AIA" não existe como unidade oficial; o mais provável é que seus **10.000
sejam AI Credits/mês** (um budget por usuário que o admin configurou — 10.000
créditos = US$ 100).

⚠️ Exceção: planos **anuais** Pro/Pro+ continuam no sistema antigo de
requests × multiplicador. Confira qual é o seu caso antes de otimizar:

- No CLI: `/usage` (mostra consumo da sessão) e `copilot help billing`.
- No site: `github.com/settings/billing` → aba "AI usage", ou a página de
  settings do Copilot → "Usage this cycle" (mostra tipo "450 / 1.900 AI
  credits used").
- Por script (é o que o `copiloto.py orcamento sincronizar` faz sozinho):
  ```bash
  gh api "/users/SEU_LOGIN/settings/billing/ai_credit/usage?year=2026&month=8" \
     -H "X-GitHub-Api-Version: 2026-03-10"
  ```
  Não existe endpoint oficial de **saldo restante** — só de consumo; o
  restante é `cota − consumo` (que é exatamente o que o `copiloto.py` faz).

Se for Business/Enterprise: a cota padrão é 1.900 créditos/usuário (promo de
3.000 **acaba em 01/09/2026** — semana que vem! — a cota efetiva pode cair).

## 3. A arquitetura (o que você pediu, formalizado)

```
                     ┌─────────────────────────────────┐
    tarefa ─────────▶│ PLANEJADOR (modelo forte, caro) │──▶ plano.md
                     └─────────────────────────────────┘      │
        ┌─────────────────────────────────────────────────────┘
        ▼  para cada passo:
   ┌──────────────────────────────────┐
   │ EXECUTOR (modelo barato)         │◀────────┐
   └──────────────┬───────────────────┘         │ corrige de graça
                  ▼                             │ (até 2x)
   ┌──────────────────────────────────┐         │
   │ testes + linter LOCAIS (grátis)  │── falhou┘
   └──────────────┬───────────────────┘
                  ▼ passou
   ┌──────────────────────────────────┐
   │ VALIDADOR (modelo forte)         │──▶ APROVADO → próximo passo
   └──────────────────────────────────┘    AJUSTAR  → volta ao executor barato
                                           REFAZER  → volta ao planejador
```

As três regras de economia embutidas:

1. **O modelo caro nunca digita código.** Ele escreve plano e lê diff — as
   duas coisas mais curtas do fluxo.
2. **Verificação grátis antes do validador caro.** Teste quebrado volta pro
   executor barato; você nunca paga modelo forte pra descobrir que um teste
   falhou.
3. **Portão de orçamento antes de toda chamada paga.** Se não cabe na cota do
   dia, o script para e explica — em vez de queimar o mês numa tarde.

## 4. A ferramenta: um arquivo só

Tudo mora em **`copiloto.py`** — um único arquivo, Python 3.10+ puro (stdlib,
sem pip, sem admin). Para levar pro trabalho: copie **um arquivo**. Os prompts
dos 3 papéis e os templates estão embutidos nele.

| Comando | O que faz | Custo |
|---|---|---|
| `copiloto.py orcamento ...` | A cota do mês rateada pelos **dias úteis restantes** (feriados BR validados contra a Portaria MGI), recalculada a cada dia, com reserva de 15% liberada até o fim do ciclo | zero |
| `copiloto.py mapa ...` | Gera `MAPA.md` de um ou **vários** repositórios (símbolos via `ast` em Python, regex nas outras linguagens) — o modelo começa sabendo onde as coisas estão | zero |
| `copiloto.py planejar / rodar` | O harness dos 3 papéis, com portão de orçamento, sessões reutilizadas e consumo real via `--usage-output-file` | só as chamadas de modelo |
| `copiloto.py instalar <repo>` | Grava os custom agents (`.github/agents/*.agent.md`) e o `AGENTS.md` com padrão de memória num repo; `--mcp` mostra o exemplo do grafo de memória | zero |

Onde as coisas ficam: o estado **pessoal** (cota, consumo) vai para
`~/.copiloto/` — a cota é sua, não do projeto; a configuração **do projeto**
(repos, verificações, modelos por papel) fica em `./copiloto.json`, criada pelo
`init` e versionável no git do projeto.

### Como casca do copilot do trabalho

O `copiloto.py` é a porta de entrada; o `copilot` de verdade roda por baixo:

```bash
python3 copiloto.py pedir "o que faz a funcao X?"   # prompt avulso: passa pelo
                                                    # portão, usa o modelo barato
                                                    # (--forte / --modelo mudam)
python3 copiloto.py sessao                          # mostra o saldo do dia e abre
                                                    # o copilot interativo; ao sair,
                                                    # lembra de sincronizar o gasto
python3 copiloto.py atualizar                       # baixa a versão mais nova DESTE
                                                    # arquivo do GitHub e se substitui
                                                    # (backup .bak; valida antes)
```

O `atualizar` baixa de `Recapi/Copilot` (branch `main`) por HTTPS direto — 
respeita `HTTPS_PROXY` — com fallback para o `gh`. Fluxo: melhora em casa,
`git push`, e no trabalho só `python3 copiloto.py atualizar`.

### As libs de economia (instala, atualiza e usa)

```bash
python3 copiloto.py libs status              # o que está instalado nesta máquina
python3 copiloto.py libs instalar --todas    # ripgrep, rtk, ast-grep, repomix
python3 copiloto.py libs instalar rtk        # (rodar de novo = atualizar)
```

Sem admin, e **sem tocar na máquina**: os binários vêm da release oficial no
GitHub de cada projeto (asset escolhido pela sua plataforma) e ficam em
`~/.copiloto/libs/bin` — uma pasta que entra no PATH **apenas dos processos
que a casca abre** (o copilot, as verificações, a sessão interativa). O PATH
do seu usuário não muda; fora do `copiloto.py`, é como se as libs não
existissem. Quem preferir no PATH de verdade usa `--global`. ast-grep e
repomix também instalam via npm quando houver.

E o **usar** é automático: os prompts do planejador e do executor ganham um
bloco listando as ferramentas presentes na máquina com a instrução de uso —
`rtk` prefixando comandos de terminal, `rg` antes de abrir arquivo, `ast-grep`
para refactor estrutural. Se a lib não está instalada, o bloco não aparece e
nada quebra.

### Trabalho com dois repositórios (fonte + config)

Rode o `init` numa pasta que enxergue os dois e liste ambos:

```bash
cd ~/projetos/meu-sistema        # pasta que contém fonte/ e config/
python3 copiloto.py init --repo ./fonte --repo ./config
python3 copiloto.py mapa ./fonte ./config -o MAPA.md
```

Com isso: o `MAPA.md` tem uma seção por repo; o **diff que o validador lê
cobre os dois** (rotulado por repo); as verificações detectadas rodam com
`cd <repo> &&`; e cada repo é repassado ao copilot via `--add-dir`, para o
executor poder mexer nos dois.

### O orçamento em 30 segundos

```bash
python3 copiloto.py orcamento init --cota 10000 --unidade "creditos de IA"
python3 copiloto.py orcamento status   # quanto posso gastar HOJE
python3 copiloto.py orcamento gasto 120 --modelo claude-sonnet --nota "refactor X"
python3 copiloto.py orcamento pode 300 && copilot -p "..."  # portão (exit 0/1)
python3 copiloto.py orcamento plano    # distribuição dia a dia até o fim do mês
python3 copiloto.py orcamento resumo   # gasto por dia e por modelo
python3 copiloto.py orcamento sincronizar  # puxa o consumo REAL do mês via
                                           # `gh api` (pega o gasto de fora)
```

O `sincronizar` precisa do `gh` autenticado (`gh auth login`; se reclamar de
escopo, `gh auth refresh -h github.com -s user`). É idempotente: rode quantas
vezes quiser, a diferença converge para zero.

A fórmula: `permitido_hoje = (restante − reserva) / dias_uteis_restantes`.
Recalculada todo dia, ela se corrige sozinha: economizou ontem → hoje pode
mais; estourou ontem → hoje aperta — sem precisar de lógica de "sobra
acumulada". Sábado, domingo e feriado não têm cota própria.

### O harness em 30 segundos

```bash
python3 copiloto.py mapa -o MAPA.md      # 1x por repo, grátis
python3 copiloto.py init                 # cria copiloto.json e detecta testes/linter
python3 copiloto.py planejar "adicionar retry com backoff no cliente HTTP"
# revise plano.md no editor (corrigir plano é grátis; corrigir código é caro)
python3 copiloto.py rodar                # executa e valida passo a passo
python3 copiloto.py custo                # como ficou o orçamento
```

Três comportamentos que economizam de verdade:

- **Prompt via stdin** — o prompt (que embute o MAPA.md) vai por stdin, não por
  argumento; no Windows a linha de comando estoura em ~32 KB e via stdin não há
  limite. Se a sua versão do CLI não aceitar prompt por stdin, acrescente
  `"-p", "{prompt}"` ao `cmd` do papel em `copiloto.json`.
- **Sessão reutilizada** — cada papel continua a própria sessão do copilot
  (`--resume`) dentro de uma execução: o contexto não é reenviado inteiro a
  cada chamada (input em cache custa ~10%), e correções viram follow-ups curtos.
- **Progresso salvo** (`progresso.json`) — se a execução parar no passo 3, rodar
  de novo pula (e não paga) os passos já aprovados. `--refazer` ignora isso;
  mudou o `plano.md`, o progresso é descartado sozinho.

Configure os modelos em `copiloto.json` (papel → comando + custo estimado).
Modelos que a pesquisa encontrou no CLI hoje: fortes = `claude-sonnet-4.6`,
`gpt-5.3-codex`, `gemini-3.1-pro`; baratos = `gpt-5-mini`, `claude-haiku-4.5`.
Confirme os nomes exatos com `/model` dentro do `copilot` — muda toda hora.

Dica verificada no binário v1.0.81: o modo prompt aceita `--output-format json`
(uma linha JSON por evento, estilo JSONL) — se quiser parsear a saída do agente
num script com mais segurança que o texto do `-s`, é esse o caminho.

## 5. Instalar no PC do trabalho SEM admin (Windows)

Tudo abaixo grava só no seu perfil de usuário — nada pede UAC:

```powershell
# 1. Node portátil (o zip oficial "Standalone Binary" win-x64 de nodejs.org)
#    Extraia em %LOCALAPPDATA%\nodejs e adicione ao PATH DO USUÁRIO:
setx PATH "%PATH%;%LOCALAPPDATA%\nodejs"     # setx SEM /M = não pede admin

# 2. Copilot CLI (no Windows o npm global já cai em %APPDATA%\npm, seu)
npm install -g @github/copilot
copilot          # e faça /login
# (alternativa sem npm: winget install GitHub.Copilot)

# 3. Python: Microsoft Store (per-user), ou uv:
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Evite: **nvm-windows** (instalador pede admin e `nvm use` precisa de
privilégio de symlink) e **Volta** (instala em Program Files). Se quiser um
gerenciador, use **Scoop** (100% user-space em `~\scoop`).

Pedras no caminho corporativo (soluções legítimas, sem burlar política):

- **Proxy:** `setx HTTPS_PROXY "http://proxy.empresa:8080"` + `npm config set
  proxy/https-proxy`.
- **Inspeção SSL:** exporte a CA raiz da empresa para um `.pem` e aponte tudo
  pra ele: `setx NODE_EXTRA_CA_CERTS`, `npm config set cafile`, `pip config
  set global.cert`. **Nunca** desligue a verificação (`strict-ssl false`) —
  indefensável em auditoria. Obs.: há versões do Copilot CLI com bug de não
  honrar `NODE_EXTRA_CA_CERTS` (issue #333) — se der "fetch failed", é isso.
- **AppLocker bloqueando .exe no perfil:** rode como script pelo Node aprovado
  da TI: `node %APPDATA%\npm\node_modules\@github\copilot\index.js`. E abra
  chamado pedindo exceção — é o caminho defensável.
- **Política da org:** erros tipo "disabled by your organization's Copilot
  policy" são configuração do admin (MCP vem desligado por padrão em
  Business/Enterprise) — não é problema seu nem da máquina.

## 6. Memória ("mapa neural" / grafo)

Duas camadas, da mais simples pra mais sofisticada:

**Camada 1 — grátis, sem MCP, aprovável por qualquer TI (comece aqui):**
o padrão `AGENTS.md` + pasta `.memory/` (template em `modelos/AGENTS.md`).
O Copilot CLI carrega `AGENTS.md` automaticamente; ele fica curto (<100
linhas) e aponta para `.memory/decisoes.md`, `.memory/comandos.md`,
`.memory/pegadinhas.md`, lidos só quando relevantes. O agente é instruído a
acrescentar 1–3 linhas ao fim de cada tarefa. Versionado no git = memória
compartilhada com o time, de graça.

**Camada 2 — grafo de conhecimento via MCP (se a política permitir):**
o servidor oficial `@modelcontextprotocol/server-memory` — um grafo
(entidades → relações → observações) num único arquivo JSONL local. Sem banco,
sem Docker, sem embeddings, sem mandar nada pra fora. Exemplo pronto em
`modelos/mcp-config.exemplo.json` (copie para `~/.copilot/mcp-config.json`).
Importante: fixe `MEMORY_FILE_PATH`, senão o arquivo some em atualização.

Alternativas que a pesquisa aprovou se quiser mais: **basic-memory**
(markdown + busca semântica local, estilo Obsidian) e **Serena** (além de
memória, dá ferramentas de símbolo via LSP — o agente lê a função certa em
vez do arquivo inteiro). Evite no seu contexto: Zep (virou cloud pago),
Graphiti/Neo4j/Letta (precisam de Docker/banco = admin), mem0 default (manda
contexto pra OpenAI).

Regra de ouro: memória **sempre-carregada** curta (vira token de entrada em
TODO prompt); o resto recuperado sob demanda.

## 7. Checklist de economia (ordenado por ganho ÷ esforço)

1. **Modelo barato como padrão** (`/model` → gpt-5-mini ou haiku). É a maior
   alavanca isolada: 20–40× de diferença por token.
2. **Um prompt = uma spec completa.** Objetivo, arquivos, restrições, formato
   de saída, "não faça X". Cinco idas-e-voltas custam ~5× mais que um prompt
   bem escrito.
3. **`copilot --continue`** para retomar sessão (input em cache custa ~10% do
   preço) em vez de re-explicar o projeto. 1 tarefa = 1 sessão.
4. **"Rode lint e testes e corrija até passar" no MESMO prompt** — validador
   grátis dentro da rodada, em vez de gastar outro prompt pra perguntar
   "ficou certo?".
5. **`MAPA.md` (`copiloto.py mapa`) + `rg` antes de perguntar** — cole só o trecho/diff
   relevante; não deixe o agente "procurar" pagando token.
6. **Peça diff/patch, não arquivo inteiro** (output custa ~5× o input).
7. **`/context` e `/compact`** quando a sessão inchar; desabilite MCP servers
   que não está usando (schema de tool entra em todo request).
8. **`--max-ai-credits N`** nas chamadas headless — teto duro por execução.
9. **`repomix --compress`** (via `npx`, sem instalar) quando precisar dar
   visão geral de repo grande: ~70% menos tokens mantendo assinaturas.
10. **Ponytail + rtk** se quiser apertar mais depois que o resto estiver rodando.

## 8. O que NÃO fazer

- **Proxies não oficiais** que expõem o Copilot como API (copilot-api e
  afins): violam os termos, já houve bloqueio ativo ("Forbidden... Terms of
  Service", maio/2026), e o risco cai na **sua conta corporativa**.
- Rotas "zona cinzenta" (token OAuth extraído para usar em outra ferramenta).
  Se quiser um agente de terminal alternativo legítimo, **opencode** tem
  parceria oficial com o GitHub desde 01/2026 (login Copilot nativo) — mas
  confirme com o admin se a política da org permite.
- Sync de sessões do CLI para a conta GitHub é ligado por padrão
  (`~/.copilot/session-state/` sobe) — cheque se a política da empresa aceita.
