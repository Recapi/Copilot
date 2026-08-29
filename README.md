# copiloto.py

Casca do **GitHub Copilot CLI** para quem tem pouca cota: um modelo forte
planeja, um barato executa, o forte só valida — e um orçamento distribui os
créditos do mês pelos **dias úteis** (feriados brasileiros inclusos).

**Um arquivo só.** Python 3.10+ puro, sem pip, sem admin. Tudo pelo menu:

```bash
python3 copiloto.py        # sem argumentos = menu numerado (nada de decorar flag)
```

## Fluxo de trabalho

### Uma vez, no PC do trabalho

1. Baixe **um arquivo**:
   `https://raw.githubusercontent.com/Recapi/Copilot/main/copiloto.py`
2. `python3 copiloto.py` e, no menu:
   - **1 Orçamento → Configurar** — informe sua cota do mês (ex.: 10000)
   - **7 Escolher modelos** — escolha da lista e salve como **padrão geral**.
     Fica gravado em `~/.copiloto/harness.json`: vale para sempre e para todos
     os projetos, até você trocar de novo.
   - **8 Libs → Instalar todas** — rg, rtk e ast-grep, baixados das releases
     oficiais para dentro da casca (o PATH da máquina não muda)

### Uma vez por projeto

3. **2 Projeto do trabalho** — informe git, branch e pasta do **fonte** e do
   **config**, e mande baixar (clona na primeira vez, atualiza depois)
4. **3 Analisar repos → Gerar os três** — `MAPA.md`, `ARQUITETURA.md` e
   `BANCO.md`, tudo local e grátis (é o que impede o modelo de explorar o
   repositório pagando token)

### Toda tarefa

5. **4 Tarefa → Planejar** — o modelo forte escreve o `plano.md` em passos
6. **Revise o `plano.md` no editor** — corrigir plano é grátis; corrigir
   código depois custa crédito
7. **4 Tarefa → Rodar** — o barato executa passo a passo, testes/linter locais
   validam de graça, e o forte só revisa o diff. Parou no meio? Rodar de novo
   pula o que já foi aprovado (não paga duas vezes)

### No dia a dia

- Dúvida rápida → **5 Pergunta avulsa** (modelo barato, passa pelo portão)
- Usar o copilot solto → **6 Sessão** (mostra o saldo antes de abrir)
- Fim do dia → **1 Orçamento → Sincronizar** (puxa o gasto real do GitHub,
  pegando o que você usou fora da casca)

### Manutenção

Melhorou algo em casa → `git push` → no trabalho: **10 Atualizar** (ele baixa
a versão nova deste próprio arquivo e se substitui, com backup).

## Por que economiza

- O modelo **caro nunca digita código**: escreve plano e lê diff, as duas
  coisas mais curtas do fluxo.
- **Teste e linter rodam de graça antes** do validador caro — você não paga
  modelo forte para descobrir que um teste quebrou.
- **Portão de orçamento** antes de toda chamada: a cota do dia é
  `(restante − reserva) ÷ dias úteis restantes`, recalculada diariamente.
  Sem saldo, ele para e explica, em vez de queimar o mês numa tarde.
- As **libs de economia** (rtk comprime saída de comando, rg busca antes de
  abrir arquivo, ast-grep refatora sem modelo) são instaladas pela casca e os
  prompts já instruem o agente a usá-las.

## Onde cada coisa fica salva

| Onde | O quê |
|---|---|
| `~/.copiloto/` | Pessoal e permanente: cota (`config.json`), consumo (`uso.jsonl`), **modelos padrão** (`harness.json`), libs (`libs/bin`) |
| `./copiloto.json` | Do projeto: repos, git/branch, verificações (versionável no git do trabalho) |
| `./plano.md` + `./progresso.json` | Da tarefa atual |

## Comandos para scripts (opcional)

Tudo do menu existe como subcomando, se um dia quiser automatizar:
`orcamento {status,gasto,pode,plano,resumo,sincronizar}`, `mapa`,
`arquitetura`, `banco`, `projeto baixar`, `init`, `planejar "..."`, `rodar`,
`pedir "..."`, `sessao`, `libs {status,instalar}`, `atualizar`.
`HARNESS_DRY_RUN=1` simula sem gastar.

## Mais detalhes

Instalação sem admin no Windows, como funciona a cobrança do Copilot (AI
Credits), memória/grafo, checklist completo de economia e o que **não** fazer:
veja o [DETALHES.md](DETALHES.md).
