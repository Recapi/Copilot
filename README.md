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
| "harmens" | **harness** | O "arnês": o script que amarra os modelos num fluxo. É o `harness.py` desta pasta. |
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
- Por script (consumo do mês, para alimentar o `orcamento.py`):
  ```bash
  gh api "/users/SEU_LOGIN/settings/billing/ai_credit/usage?year=2026&month=8" \
     -H "X-GitHub-Api-Version: 2026-03-10"
  ```
  Não existe endpoint oficial de **saldo restante** — só de consumo; o
  restante é `cota − consumo` (que é exatamente o que o `orcamento.py` faz).

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

## 4. As ferramentas desta pasta

| Arquivo | O que faz | Custo |
|---|---|---|
| `calendario.py` | Dias úteis + feriados nacionais BR (validado contra a Portaria MGI: 2026 tem 21 dias úteis em agosto, Carnaval 16–17/02 etc.) | zero |
| `orcamento.py` | A cota do mês rateada pelos **dias úteis restantes**, recalculada a cada dia, com reserva de 15% que vai sendo liberada até o fim do ciclo | zero |
| `mapa.py` | Gera `MAPA.md` do repositório (estrutura, símbolos, dependências) por regex local — o modelo começa sabendo onde as coisas estão em vez de explorar pagando token | zero |
| `harness.py` | O orquestrador dos 3 papéis, com portão de orçamento e leitura do consumo real via `--usage-output-file` do Copilot CLI | só as chamadas de modelo |
| `prompts/` | Os 3 prompts de papel (planejar / executar / validar) | — |
| `modelos/` | Templates: `.agent.md` (custom agents nativos do Copilot CLI), `AGENTS.md` com padrão de memória, exemplo de MCP de memória | — |

### O orçamento em 30 segundos

```bash
python3 orcamento.py init --cota 10000 --unidade "creditos de IA"
python3 orcamento.py status      # quanto posso gastar HOJE
python3 orcamento.py gasto 120 --modelo claude-sonnet --nota "refactor X"
python3 orcamento.py pode 300 && copilot -p "..."   # portão p/ scripts (exit 0/1)
python3 orcamento.py plano       # distribuição dia a dia até o fim do mês
python3 orcamento.py resumo      # gasto por dia e por modelo
```

A fórmula: `permitido_hoje = (restante − reserva) / dias_uteis_restantes`.
Recalculada todo dia, ela se corrige sozinha: economizou ontem → hoje pode
mais; estourou ontem → hoje aperta — sem precisar de lógica de "sobra
acumulada". Sábado, domingo e feriado não têm cota própria.

### O harness em 30 segundos

```bash
python3 mapa.py /caminho/do/repo -o MAPA.md   # 1x por repo, grátis
python3 harness.py init                        # detecta testes/linter do projeto
python3 harness.py planejar "adicionar retry com backoff no cliente HTTP"
# revise plano.md no editor (corrigir plano é grátis; corrigir código é caro)
python3 harness.py rodar                       # executa e valida passo a passo
python3 harness.py custo                       # como ficou o orçamento
```

Configure os modelos em `harness.json` (papel → comando + custo estimado).
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
5. **`MAPA.md` (mapa.py) + `rg` antes de perguntar** — cole só o trecho/diff
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
