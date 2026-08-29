#!/usr/bin/env python3
"""Orquestrador planejador -> executor -> validador, com portao de orcamento.

A economia vem de tres decisoes, nao de truque nenhum:

1. O modelo caro so aparece duas vezes por passo: para planejar e para validar.
   Todo o volume de digitacao e do modelo barato.
2. Antes de chamar o validador caro, as verificacoes LOCAIS rodam de graca. Se o
   teste ja falha, o executor barato tenta de novo sozinho. Nunca se paga um
   modelo forte para descobrir que um teste quebrou.
3. Toda chamada paga passa por um portao de orcamento (`orcamento.py`). Se nao
   cabe no dia, para e avisa, em vez de queimar a cota do mes numa tarde.

Config em `harness.json` (crie com `python3 harness.py init`).

Uso:
    python3 harness.py init
    python3 harness.py planejar "adicionar retry com backoff no cliente HTTP"
    python3 harness.py rodar            # executa e valida todos os passos
    python3 harness.py rodar --passo 3  # so um passo
    python3 harness.py custo            # o que ja foi gasto neste projeto
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import datetime as dt
from pathlib import Path

import orcamento

BASE = Path(__file__).parent.resolve()
CONFIG = BASE / "harness.json"
PROMPTS = BASE / "prompts"
PLANO = Path("plano.md")

PADRAO = {
    "_comentario": (
        "'cmd' de cada papel: o prompt e enviado por STDIN (sem limite de tamanho de "
        "linha de comando). Se a sua versao do CLI nao aceitar prompt por stdin, "
        "acrescente '-p', '{prompt}' ao cmd para envia-lo como argumento. "
        "{usage_file} vira um JSON com o consumo real da chamada (--usage-output-file). "
        "'custo' e a ESTIMATIVA em creditos usada pelo portao de orcamento ANTES de "
        "chamar; se o usage_file trouxer o consumo real, e ele que e registrado. "
        "'reusar_sessao' encadeia as chamadas de cada papel na mesma sessao do copilot "
        "(--resume), aproveitando o cache de contexto (~10% do preco do input). "
        "Confira os nomes de modelos disponiveis com /model dentro do copilot."
    ),
    "papeis": {
        "planejador": {
            "cmd": ["copilot", "--model", "claude-sonnet-4.6", "-s",
                    "--usage-output-file", "{usage_file}"],
            "custo": 40.0,
        },
        "executor": {
            "cmd": ["copilot", "--model", "gpt-5-mini", "-s", "--allow-all-tools",
                    "--usage-output-file", "{usage_file}"],
            "custo": 10.0,
        },
        "validador": {
            "cmd": ["copilot", "--model", "claude-sonnet-4.6", "-s",
                    "--usage-output-file", "{usage_file}"],
            "custo": 25.0,
        },
    },
    "verificacoes": [],
    "reusar_sessao": True,
    "max_tentativas_executor": 2,
    "max_rodadas_ajuste": 2,
    "timeout_seg": 900,
}


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

def carregar() -> dict:
    if not CONFIG.exists():
        print(f"erro: {CONFIG} nao existe. Rode: python3 harness.py init", file=sys.stderr)
        sys.exit(2)
    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    for chave, valor in PADRAO.items():
        cfg.setdefault(chave, valor)
    return cfg


def detectar_verificacoes(raiz: Path) -> list[str]:
    """Adivinha os comandos de verificacao gratuitos do projeto."""
    v = []
    if (raiz / "pyproject.toml").exists() or (raiz / "setup.py").exists() or list(raiz.glob("test_*.py")):
        if (raiz / "tests").is_dir() or list(raiz.glob("test_*.py")):
            v.append("python -m pytest -q")
        v.append("python -m compileall -q .")
    pkg = raiz / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        for nome in ("typecheck", "lint", "test"):
            if nome in scripts:
                v.append(f"npm run {nome} --silent")
    if (raiz / "go.mod").exists():
        v += ["go build ./...", "go test ./..."]
    if (raiz / "Cargo.toml").exists():
        v.append("cargo check")
    return v


# --------------------------------------------------------------------------- #
# Portao de orcamento
# --------------------------------------------------------------------------- #

class SemOrcamento(Exception):
    pass


def portao(custo: float, descricao: str, forcar: bool) -> None:
    if custo <= 0:
        return
    cfg_orc = orcamento.carregar_config()
    s = orcamento.calcular(cfg_orc)
    if custo <= s.saldo_hoje or forcar:
        return
    if not s.hoje_eh_util:
        motivo = (f"hoje e {s.motivo_nao_util}, e a cota e distribuida so entre dias uteis")
    else:
        motivo = (f"o saldo de hoje e {s.saldo_hoje:g} "
                  f"({s.restante:g} restantes no ciclo, {s.dias_uteis_restantes} dias uteis pela frente)")
    raise SemOrcamento(
        f"'{descricao}' custaria {custo:g}, mas {motivo}.\n"
        f"Opcoes: espere o proximo dia util, use --forcar para invadir a reserva, "
        f"ou rode este passo so com o modelo barato."
    )


def cobrar(custo: float, modelo: str, nota: str) -> None:
    if custo > 0:
        orcamento.registrar(custo, modelo, nota, dt.date.today())


# --------------------------------------------------------------------------- #
# Chamada ao agente
# --------------------------------------------------------------------------- #

def _creditos_do_usage(caminho: Path) -> float | None:
    """Extrai o total de creditos do JSON de --usage-output-file.

    O formato desse arquivo nao e documentado como estavel, entao a leitura e
    defensiva: procura recursivamente qualquer campo numerico cujo nome
    mencione 'credit'. Se nada for encontrado, cai na estimativa configurada.
    """
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None

    totais: list[float] = []   # campos com 'total' no nome
    parciais: list[float] = []  # demais campos com 'credit' no nome

    def varrer(obj):
        if isinstance(obj, dict):
            for chave, valor in obj.items():
                if isinstance(valor, (int, float)) and "credit" in chave.lower():
                    (totais if "total" in chave.lower() else parciais).append(float(valor))
                else:
                    varrer(valor)
        elif isinstance(obj, list):
            for item in obj:
                varrer(item)

    varrer(dados)
    if totais:
        return max(totais)
    if parciais:
        # Sem campo de total: o maior valor e o palpite mais seguro (um total
        # sempre e >= cada parte; somar partes + total contaria em dobro).
        return max(parciais)
    return None


SESSOES: dict[str, str] = {}  # papel -> id da sessao do copilot criada nesta execucao


def _dir_sessoes() -> Path:
    return Path(os.environ.get("COPILOT_HOME", str(Path.home() / ".copilot"))) / "session-state"


def _capturar_sessao(papel: str, inicio: float) -> None:
    """Guarda o id da sessao que o copilot criou nesta chamada.

    O CLI grava cada sessao em ~/.copilot/session-state/<id>/; a desta chamada
    e a mais recente com mtime posterior ao inicio dela. Melhor esforco: se o
    diretorio nao existir ou nada bater, seguimos sem reuso de sessao.
    """
    if papel in SESSOES:
        return
    ja_reivindicadas = set(SESSOES.values())
    try:
        candidatos = [(d.stat().st_mtime, d.name) for d in _dir_sessoes().iterdir()
                      if d.is_dir() and d.name not in ja_reivindicadas]
        candidatos = [c for c in candidatos if c[0] >= inicio - 2]
    except OSError:
        return
    if candidatos:
        SESSOES[papel] = max(candidatos)[1]


def tem_sessao(papel: str) -> bool:
    """True se as proximas chamadas deste papel vao continuar uma sessao existente."""
    return papel in SESSOES


def chamar(cfg: dict, papel: str, prompt: str, forcar: bool, nota: str) -> str:
    conf = cfg["papeis"][papel]
    custo = float(conf.get("custo", 0))
    portao(custo, f"{papel}: {nota}", forcar)

    usage_file = None
    via_argv = any("{prompt}" in parte for parte in conf["cmd"])
    cmd = []
    for parte in conf["cmd"]:
        if "{usage_file}" in parte:
            if usage_file is None:
                fd, tmp = tempfile.mkstemp(prefix="harness-usage-", suffix=".json")
                os.close(fd)
                usage_file = Path(tmp)
            parte = parte.replace("{usage_file}", str(usage_file))
        cmd.append(parte.replace("{prompt}", prompt))
    entrada = None if via_argv else prompt

    reusar = bool(cfg.get("reusar_sessao", True))
    if reusar and papel in SESSOES:
        cmd += ["--resume", SESSOES[papel]]

    modelo = next((cmd[i + 1] for i, p in enumerate(cmd) if p == "--model" and i + 1 < len(cmd)), papel)

    print(f"\n>> {papel} ({modelo}, estimado {custo:g}) — {nota}")
    if os.environ.get("HARNESS_DRY_RUN"):
        origem = "argv" if via_argv else f"stdin, {len(prompt)} chars"
        print(f"   [dry-run] {shlex.join(cmd)[:300]}  (prompt via {origem})")
        if usage_file:
            usage_file.unlink(missing_ok=True)
        return "[dry-run] sem saida"

    inicio = time.time()
    try:
        r = subprocess.run(cmd, input=entrada, capture_output=True, text=True,
                           timeout=int(cfg.get("timeout_seg", 900)))
    except FileNotFoundError:
        print(f"erro: comando '{cmd[0]}' nao encontrado. Ajuste 'cmd' em {CONFIG}.", file=sys.stderr)
        sys.exit(2)
    except subprocess.TimeoutExpired:
        if usage_file:
            usage_file.unlink(missing_ok=True)
        cobrar(custo, modelo, f"{nota} (timeout)")
        raise RuntimeError(f"{papel} estourou o timeout")
    # So captura sessao de chamada bem-sucedida: uma falha rapida (auth, modelo
    # invalido) nao cria sessao, e capturar aqui pegaria a sessao de outro papel.
    if reusar and r.returncode == 0:
        _capturar_sessao(papel, inicio)

    # Registra o consumo REAL se o CLI informou; senao, a estimativa.
    # Cobra mesmo em erro: a chamada foi consumida do mesmo jeito.
    real = _creditos_do_usage(usage_file) if usage_file else None
    if usage_file:
        usage_file.unlink(missing_ok=True)
    if real is not None and real > 0:
        print(f"   consumo real: {real:g} creditos")
        cobrar(real, modelo, nota)
    else:
        cobrar(custo, modelo, nota + " (estimado)")
    if r.returncode != 0:
        print(f"   (saiu com codigo {r.returncode})", file=sys.stderr)
        if r.stderr.strip():
            print("   " + r.stderr.strip()[:800], file=sys.stderr)
    return r.stdout


# --------------------------------------------------------------------------- #
# Verificacoes locais (de graca)
# --------------------------------------------------------------------------- #

def verificar(comandos: list[str]) -> tuple[bool, str]:
    if not comandos:
        return True, "(nenhuma verificacao local configurada)"
    partes = []
    ok_geral = True
    for c in comandos:
        print(f"   verificando (gratis): {c}")
        try:
            r = subprocess.run(c, shell=True, capture_output=True, text=True, timeout=600)
        except subprocess.TimeoutExpired:
            ok_geral = False
            partes.append(f"$ {c}\n[timeout]")
            continue
        ok = r.returncode == 0
        ok_geral = ok_geral and ok
        saida = (r.stdout + r.stderr).strip()
        partes.append(f"$ {c}\n[{'ok' if ok else 'FALHOU'}]\n{saida[-2500:]}")
    return ok_geral, "\n\n".join(partes)


def git_diff() -> str:
    try:
        r = subprocess.run(["git", "diff", "HEAD"], capture_output=True, text=True, timeout=60)
        return r.stdout
    except (subprocess.SubprocessError, FileNotFoundError):
        return "(git nao disponivel — anexe o diff manualmente)"


# --------------------------------------------------------------------------- #
# Plano
# --------------------------------------------------------------------------- #

def ler_passos(caminho: Path = PLANO) -> list[dict]:
    if not caminho.exists():
        print(f"erro: {caminho} nao existe. Rode 'planejar' primeiro.", file=sys.stderr)
        sys.exit(2)
    texto = caminho.read_text(encoding="utf-8")
    blocos = re.split(r"^##\s+Passo\s+", texto, flags=re.M)[1:]
    passos = []
    for i, bloco in enumerate(blocos, 1):
        titulo = re.sub(r"^\d+\s*[-—–:]*\s*", "", bloco.splitlines()[0].strip())
        verif = re.search(r"\*\*Verificacao local.*?:\*\*\s*`([^`]+)`", bloco)
        passos.append({
            "n": i,
            "titulo": titulo,
            "texto": "## Passo " + bloco.strip(),
            "verificacao": verif.group(1) if verif else None,
        })
    return passos


def ler_prompt(nome: str) -> str:
    caminho = PROMPTS / nome
    if not caminho.exists():
        print(f"erro: prompt {caminho} nao encontrado", file=sys.stderr)
        sys.exit(2)
    return caminho.read_text(encoding="utf-8")


def contexto_mapa() -> str:
    m = Path("MAPA.md")
    if m.exists():
        return f"\n\n--- MAPA DO REPOSITORIO ---\n{m.read_text(encoding='utf-8')}\n"
    return "\n\n(Nao ha MAPA.md. Gere com `python3 mapa.py` antes, para nao gastar credito explorando o repo.)\n"


# --------------------------------------------------------------------------- #
# Comandos
# --------------------------------------------------------------------------- #

def cmd_init(args) -> int:
    cfg = dict(PADRAO)
    if CONFIG.exists():
        cfg.update(json.loads(CONFIG.read_text(encoding="utf-8")))
    detectadas = detectar_verificacoes(Path.cwd())
    if detectadas and not cfg["verificacoes"]:
        cfg["verificacoes"] = detectadas
        print("verificacoes locais detectadas neste projeto:")
        for v in detectadas:
            print(f"  - {v}")
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nconfig em {CONFIG}")
    print("Ajuste os nomes dos modelos em 'papeis' antes de rodar de verdade.")
    return 0


def cmd_planejar(args) -> int:
    cfg = carregar()
    prompt = (
        ler_prompt("01-planejar.md")
        + contexto_mapa()
        + f"\n\n--- TAREFA ---\n{args.tarefa}\n\n"
        "Escreva o plano no arquivo plano.md, no formato exigido acima."
    )
    saida = chamar(cfg, "planejador", prompt, args.forcar, args.tarefa[:60])
    if saida.strip() and not PLANO.exists():
        PLANO.write_text(saida, encoding="utf-8")
        print(f"\nplano gravado em {PLANO} (a partir da saida do modelo)")
    if PLANO.exists():
        passos = ler_passos()
        print(f"\nplano com {len(passos)} passos:")
        for p in passos:
            print(f"  {p['n']}. {p['titulo']}")
        print("\nRevise o plano antes de rodar. Corrigir plano no editor e de graca;")
        print("corrigir codigo errado depois custa credito.")
    return 0


def executar_passo(cfg: dict, passo: dict, forcar: bool) -> str:
    """Retorna 'aprovado', 'ajustado', 'reprovado' ou 'bloqueado'."""
    verificacoes = ([passo["verificacao"]] if passo["verificacao"] else []) + cfg["verificacoes"]
    print(f"\n{'=' * 70}\nPASSO {passo['n']}: {passo['titulo']}\n{'=' * 70}")

    instrucao = ler_prompt("02-executar.md") + contexto_mapa() + "\n\n--- PASSO A EXECUTAR ---\n" + passo["texto"]
    saida_exec = chamar(cfg, "executor", instrucao, forcar, f"passo {passo['n']}")
    if "BLOQUEADO" in saida_exec:
        print("   executor reportou BLOQUEADO — parando este passo")
        return "bloqueado"

    # Ciclo barato: enquanto a verificacao local falhar, o executor barato tenta
    # de novo. Isso nao custa nada do modelo caro. Com sessao ativa, o follow-up
    # e curto — o contexto do passo ja esta na sessao do copilot.
    ok, relatorio = verificar(verificacoes)
    tentativa = 0
    while not ok and tentativa < int(cfg["max_tentativas_executor"]):
        tentativa += 1
        print(f"   verificacao falhou — devolvendo ao executor barato (tentativa {tentativa})")
        if tem_sessao("executor"):
            prompt_corr = ("A verificacao local do passo em que voce estava trabalhando "
                           f"falhou. Corrija e rode a verificacao de novo.\n\n{relatorio}")
        else:
            prompt_corr = instrucao + f"\n\n--- A VERIFICACAO FALHOU, CORRIJA ---\n{relatorio}"
        chamar(cfg, "executor", prompt_corr, forcar, f"passo {passo['n']} correcao {tentativa}")
        ok, relatorio = verificar(verificacoes)
    if not ok:
        print("   executor barato nao resolveu; escalando para o validador")

    # So agora entra o modelo caro.
    for rodada in range(1, int(cfg["max_rodadas_ajuste"]) + 1):
        prompt_val = (
            ler_prompt("03-validar.md")
            + "\n\n--- PASSO DO PLANO ---\n" + passo["texto"]
            + "\n\n--- DIFF ---\n```diff\n" + git_diff()[:30000] + "\n```"
            + "\n\n--- VERIFICACOES LOCAIS ---\n" + relatorio[:8000]
        )
        veredito_txt = chamar(cfg, "validador", prompt_val, forcar, f"validar passo {passo['n']}")
        alvo = veredito_txt.upper()
        if "APROVADO" in alvo:
            print("   APROVADO")
            return "aprovado"
        if "REFAZER" in alvo:
            print("   REFAZER — a abordagem esta errada, volte ao planejador")
            print(veredito_txt[:1500])
            return "reprovado"
        print(f"   AJUSTAR (rodada {rodada})")
        ajustes = veredito_txt
        if tem_sessao("executor"):
            prompt_aj = ("O validador revisou o seu trabalho neste passo e pediu os "
                         "ajustes abaixo. Aplique-os e rode a verificacao local.\n\n" + ajustes)
        else:
            prompt_aj = ler_prompt("02-executar.md") + "\n\n--- APLIQUE ESTES AJUSTES ---\n" + ajustes
        chamar(cfg, "executor", prompt_aj, forcar, f"ajuste passo {passo['n']} rodada {rodada}")
        ok, relatorio = verificar(verificacoes)

    print("   limite de rodadas de ajuste atingido")
    return "ajustado"


PROGRESSO = Path("progresso.json")


def _sha_plano() -> str:
    return hashlib.sha256(PLANO.read_bytes()).hexdigest()


def carregar_progresso(sha: str) -> dict[int, str]:
    if not PROGRESSO.exists():
        return {}
    try:
        dados = json.loads(PROGRESSO.read_text(encoding="utf-8"))
        if dados.get("plano_sha") != sha:
            print("plano.md mudou desde a ultima execucao — progresso anterior descartado")
            return {}
        return {int(k): str(v) for k, v in (dados.get("resultados") or {}).items()}
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        print("progresso.json ilegivel — recomecando do zero", file=sys.stderr)
        return {}


def salvar_progresso(sha: str, resultados: dict[int, str]) -> None:
    PROGRESSO.write_text(
        json.dumps({"plano_sha": sha, "resultados": {str(k): v for k, v in resultados.items()}},
                   indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def cmd_rodar(args) -> int:
    cfg = carregar()
    passos = ler_passos()
    sha = _sha_plano()
    anteriores = carregar_progresso(sha)
    if args.passo:
        passos = [p for p in passos if p["n"] == args.passo]
        if not passos:
            print(f"erro: passo {args.passo} nao existe no plano", file=sys.stderr)
            return 2
    if args.refazer:
        # Refaz so os passos selecionados; aprovacoes dos demais ficam de pe.
        for p in passos:
            anteriores.pop(p["n"], None)

    resultados = dict(anteriores)
    for passo in passos:
        # Passo ja aprovado numa execucao anterior nao roda (nem paga) de novo.
        if anteriores.get(passo["n"]) == "aprovado":
            print(f"passo {passo['n']} ja aprovado em execucao anterior — pulando "
                  "(use --refazer para repetir)")
            continue
        try:
            resultados[passo["n"]] = executar_passo(cfg, passo, args.forcar)
        except SemOrcamento as e:
            print(f"\n[ORCAMENTO] {e}", file=sys.stderr)
            resultados[passo["n"]] = "sem-orcamento"
            if not os.environ.get("HARNESS_DRY_RUN"):
                salvar_progresso(sha, resultados)
            break
        # Dry-run nao persiste: um ensaio nao pode rasurar aprovacoes reais.
        if not os.environ.get("HARNESS_DRY_RUN"):
            salvar_progresso(sha, resultados)

    print(f"\n{'=' * 70}\nRESUMO")
    for p in passos:
        print(f"  passo {p['n']}: {resultados.get(p['n'], 'nao rodou')}  — {p['titulo']}")
    orcamento.imprimir(orcamento.calcular(orcamento.carregar_config()), orcamento.carregar_config())
    return 0 if all(resultados.get(p["n"]) == "aprovado" for p in passos) else 1


def cmd_custo(args) -> int:
    cfg_orc = orcamento.carregar_config()
    orcamento.imprimir(orcamento.calcular(cfg_orc), cfg_orc)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("init", help="cria harness.json e detecta verificacoes locais")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("planejar", help="modelo forte escreve plano.md")
    sp.add_argument("tarefa")
    sp.add_argument("--forcar", action="store_true", help="ignora o portao de orcamento")
    sp.set_defaults(func=cmd_planejar)

    sp = sub.add_parser("rodar", help="executa (barato) e valida (caro) os passos")
    sp.add_argument("--passo", type=int)
    sp.add_argument("--forcar", action="store_true")
    sp.add_argument("--refazer", action="store_true",
                    help="ignora o progresso salvo e roda tudo de novo")
    sp.set_defaults(func=cmd_rodar)

    sp = sub.add_parser("custo", help="status do orcamento")
    sp.set_defaults(func=cmd_custo)

    args = p.parse_args()
    if not getattr(args, "cmd", None):
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
