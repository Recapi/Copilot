# Instrucoes do projeto

<!-- Modelo de AGENTS.md para colocar na raiz de cada repositorio do trabalho.
     O Copilot CLI carrega este arquivo automaticamente em toda sessao, entao
     ele precisa ficar CURTO (menos de ~100 linhas): tudo aqui vira tokens de
     entrada em cada prompt. Detalhe vai para .memory/ e so e lido sob demanda. -->

## Sobre o projeto
- Stack: <linguagens e frameworks principais>
- Build: `<comando>`
- Testes: `<comando>`  |  Lint: `<comando>`
- Nao mexer em: `<pastas geradas, vendored, etc.>`

## Regras de economia (temos pouca cota)
- Antes de explorar o repositorio, leia `MAPA.md` — ele ja tem a estrutura e
  os simbolos de cada arquivo.
- Leia so os trechos necessarios (`rg` primeiro), nunca arquivos inteiros
  por curiosidade.
- Valide com linter e testes locais ANTES de dar a tarefa por encerrada.
- Prefira responder com diff/patch, nao com arquivo inteiro reescrito.
- Va direto ao ponto nas respostas; sem preambulo nem recapitulacao.

## Memoria do projeto
Fatos duraveis ficam em `.memory/` (um markdown por topico):
- `.memory/decisoes.md` — decisoes de arquitetura e por que
- `.memory/comandos.md` — comandos e fluxos descobertos na pratica
- `.memory/pegadinhas.md` — o que ja quebrou e como evitar

No inicio de uma tarefa, leia APENAS os topicos relevantes para ela.
Ao concluir uma tarefa que revelou algo durvel (decisao, comando, pegadinha),
acrescente 1-3 linhas no arquivo certo — sem reescrever o arquivo.
