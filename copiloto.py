#!/usr/bin/env python3
"""copiloto.py — kit completo, num arquivo so, para usar o Copilot CLI com pouca cota.

Autossuficiente de proposito: Python puro (stdlib), sem pip, sem admin.
Copie este arquivo para qualquer maquina que tenha Python 3.10+ e pronto.

O que tem dentro:
  - orcamento : cota mensal rateada por DIAS UTEIS (feriados brasileiros),
                recalculada por dia, com reserva e portao para scripts
  - mapa      : mapa do(s) repositorio(s) gerado localmente, de graca
  - harness   : modelo forte planeja -> barato executa -> forte valida,
                com verificacao local gratis antes do validador caro
  - instalar  : grava os custom agents (.agent.md) e o AGENTS.md num repo

Onde as coisas ficam:
  ~/.copiloto/           estado PESSOAL (a cota e sua, nao do projeto):
    config.json          configuracao do orcamento
    uso.jsonl            lancamentos de consumo
    harness.json         papeis/modelos padrao (opcional)
  ./copiloto.json        configuracao DO PROJETO (repos, verificacoes, papeis)
  ./plano.md             plano da tarefa atual
  ./progresso.json       passos ja aprovados (nao re-paga)
  (COPILOTO_DIR muda a pasta pessoal; HARNESS_DRY_RUN=1 simula sem gastar)

Trabalho com dois repositorios (fonte + config): rode `copiloto.py init` na
pasta que enxerga os dois e liste-os em "repos" no copiloto.json — o mapa, o
diff do validador e as verificacoes cobrem todos; "add_dirs" repassa cada um
ao copilot via --add-dir.

Como CASCA do copilot do trabalho:
  copiloto.py pedir "pergunta rapida"     prompt avulso passando pelo portao de
                                          orcamento e registrando o consumo
  copiloto.py sessao                      mostra o saldo do dia e abre o copilot
                                          interativo (lembra de sincronizar ao sair)
  copiloto.py atualizar                   baixa a versao mais nova deste arquivo
                                          do GitHub (Recapi/Copilot) e se substitui

Uso rapido:
  python3 copiloto.py orcamento init --cota 10000
  python3 copiloto.py orcamento status
  python3 copiloto.py mapa ../fonte ../config -o MAPA.md
  python3 copiloto.py init --repo ../fonte --repo ../config
  python3 copiloto.py planejar "adicionar retry no cliente HTTP"
  python3 copiloto.py rodar
"""

from __future__ import annotations

import argparse
import ast
import datetime as dt
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, asdict
from pathlib import Path

VERSAO = "2026.08.29.3"
REPO_ATUALIZACAO = os.environ.get("COPILOTO_REPO", "Recapi/Copilot")

# --------------------------------------------------------------------------- #
# Caminhos de estado
# --------------------------------------------------------------------------- #

DIR_PESSOAL = Path(os.environ.get("COPILOTO_DIR", str(Path.home() / ".copiloto")))
CONFIG_ORC = DIR_PESSOAL / "config.json"
USO_PATH = DIR_PESSOAL / "uso.jsonl"
CONFIG_HARNESS_GLOBAL = DIR_PESSOAL / "harness.json"
CONFIG_PROJETO = Path("copiloto.json")
PLANO = Path("plano.md")
PROGRESSO = Path("progresso.json")


def _migrar_estado_antigo() -> None:
    """Traz config.json/uso.jsonl/harness.json da era multi-arquivo (ficavam
    ao lado dos scripts) para ~/.copiloto, uma unica vez."""
    antigo = Path(__file__).parent
    pares = [(antigo / "config.json", CONFIG_ORC),
             (antigo / "uso.jsonl", USO_PATH),
             (antigo / "harness.json", CONFIG_HARNESS_GLOBAL)]
    for velho, novo in pares:
        if velho.exists() and not novo.exists():
            novo.parent.mkdir(parents=True, exist_ok=True)
            novo.write_text(velho.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"(migrado {velho.name} -> {novo})", file=sys.stderr)


# =========================================================================== #
# CALENDARIO — dias uteis e feriados nacionais brasileiros
# =========================================================================== #

def pascoa(ano: int) -> dt.date:
    """Domingo de Pascoa pelo algoritmo de Meeus/Jones/Butcher."""
    a = ano % 19
    b, c = divmod(ano, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes, dia = divmod(h + l - 7 * m + 114, 31)
    return dt.date(ano, mes, dia + 1)


# Feriados nacionais fixos (Lei 662/1949, 6.802/1980 e 14.759/2023).
_FIXOS = {
    (1, 1): "Confraternizacao Universal",
    (4, 21): "Tiradentes",
    (5, 1): "Dia do Trabalho",
    (9, 7): "Independencia",
    (10, 12): "Nossa Senhora Aparecida",
    (11, 2): "Finados",
    (11, 15): "Proclamacao da Republica",
    (11, 20): "Consciencia Negra",
    (12, 25): "Natal",
}


def feriados(ano: int, incluir_facultativos: bool = True) -> dict[dt.date, str]:
    """Carnaval e Corpus Christi sao ponto facultativo, nao feriado legal, mas
    na pratica quase ninguem trabalha: por padrao entram na conta."""
    resultado = {dt.date(ano, m, d): nome for (m, d), nome in _FIXOS.items()}
    p = pascoa(ano)
    resultado[p - dt.timedelta(days=2)] = "Sexta-feira Santa"
    if incluir_facultativos:
        resultado[p - dt.timedelta(days=48)] = "Carnaval (segunda)"
        resultado[p - dt.timedelta(days=47)] = "Carnaval (terca)"
        resultado[p + dt.timedelta(days=60)] = "Corpus Christi"
    return resultado


class Calendario:
    """Decide se uma data e dia util e conta dias uteis em intervalos."""

    def __init__(self, incluir_facultativos: bool = True,
                 extras=(), remover=()) -> None:
        self.incluir_facultativos = incluir_facultativos
        self.extras = {dt.date.fromisoformat(s) for s in extras}
        self.remover = {dt.date.fromisoformat(s) for s in remover}
        self._cache: dict[int, dict[dt.date, str]] = {}

    def feriados_do_ano(self, ano: int) -> dict[dt.date, str]:
        if ano not in self._cache:
            self._cache[ano] = feriados(ano, self.incluir_facultativos)
        base = dict(self._cache[ano])
        for d in self.extras:
            if d.year == ano:
                base[d] = "Feriado local/empresa"
        for d in self.remover:
            base.pop(d, None)
        return base

    def eh_dia_util(self, d: dt.date) -> bool:
        if d.weekday() >= 5:
            return False
        return d not in self.feriados_do_ano(d.year)

    def motivo_nao_util(self, d: dt.date) -> str | None:
        if d.weekday() == 5:
            return "sabado"
        if d.weekday() == 6:
            return "domingo"
        return self.feriados_do_ano(d.year).get(d)

    def dias_uteis(self, inicio: dt.date, fim: dt.date) -> list[dt.date]:
        if fim < inicio:
            return []
        dias = []
        d = inicio
        while d <= fim:
            if self.eh_dia_util(d):
                dias.append(d)
            d += dt.timedelta(days=1)
        return dias

    def contar(self, inicio: dt.date, fim: dt.date) -> int:
        return len(self.dias_uteis(inicio, fim))


# =========================================================================== #
# ORCAMENTO — cota mensal distribuida por dias uteis
# =========================================================================== #

PADRAO_ORC = {
    "cota_ciclo": 10000.0,
    "unidade": "creditos de IA",
    "dia_reset": 1,
    "reserva_pct": 0.15,
    "incluir_facultativos": True,
    "feriados_extras": [],
    "feriados_removidos": [],
    "multiplicadores": {},
}


def carregar_config() -> dict:
    cfg = dict(PADRAO_ORC)
    if CONFIG_ORC.exists():
        cfg.update(json.loads(CONFIG_ORC.read_text(encoding="utf-8")))
    return cfg


def salvar_config(cfg: dict) -> None:
    DIR_PESSOAL.mkdir(parents=True, exist_ok=True)
    CONFIG_ORC.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def registrar(qtd: float, modelo: str | None, nota: str | None, data: dt.date) -> None:
    linha = {"data": data.isoformat(), "qtd": round(qtd, 4)}
    if modelo:
        linha["modelo"] = modelo
    if nota:
        linha["nota"] = nota
    DIR_PESSOAL.mkdir(parents=True, exist_ok=True)
    with USO_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(linha, ensure_ascii=False) + "\n")


def ler_uso() -> list[dict]:
    if not USO_PATH.exists():
        return []
    itens = []
    for n, linha in enumerate(USO_PATH.read_text(encoding="utf-8").splitlines(), 1):
        linha = linha.strip()
        if not linha:
            continue
        try:
            itens.append(json.loads(linha))
        except json.JSONDecodeError:
            print(f"aviso: linha {n} de uso.jsonl ignorada (json invalido)", file=sys.stderr)
    return itens


def _clamp_dia(ano: int, mes: int, dia: int) -> dt.date:
    """Dia 31 num mes de 30 dias vira o ultimo dia do mes."""
    if mes == 12:
        prox = dt.date(ano + 1, 1, 1)
    else:
        prox = dt.date(ano, mes + 1, 1)
    ultimo = (prox - dt.timedelta(days=1)).day
    return dt.date(ano, mes, min(dia, ultimo))


def ciclo(hoje: dt.date, dia_reset: int) -> tuple[dt.date, dt.date]:
    """Intervalo fechado [inicio, fim] do ciclo de faturamento que contem `hoje`."""
    inicio_mes = _clamp_dia(hoje.year, hoje.month, dia_reset)
    if hoje >= inicio_mes:
        inicio = inicio_mes
    else:
        ano, mes = (hoje.year - 1, 12) if hoje.month == 1 else (hoje.year, hoje.month - 1)
        inicio = _clamp_dia(ano, mes, dia_reset)
    ano, mes = (inicio.year + 1, 1) if inicio.month == 12 else (inicio.year, inicio.month + 1)
    fim = _clamp_dia(ano, mes, dia_reset) - dt.timedelta(days=1)
    return inicio, fim


def reserva(total: float, uteis_restantes: int, uteis_totais: int) -> float:
    """Quanto da reserva ainda fica retido: decai linearmente e zera no ultimo
    dia util, para a cota nao morrer com sobra presa."""
    if uteis_totais <= 0:
        return 0.0
    return max(0.0, total * (uteis_restantes - 1) / uteis_totais)


@dataclass
class Situacao:
    hoje: str
    hoje_eh_util: bool
    motivo_nao_util: str | None
    ciclo_inicio: str
    ciclo_fim: str
    cota: float
    gasto_ciclo: float
    gasto_hoje: float
    restante: float
    reserva_efetiva: float
    dias_uteis_totais: int
    dias_uteis_decorridos: int
    dias_uteis_restantes: int
    permitido_hoje: float
    saldo_hoje: float
    teto_emergencia: float
    ritmo_alvo: float
    ritmo_real: float
    projecao_fim_ciclo: float
    data_estouro: str | None
    situacao: str


def calcular(cfg: dict, hoje: dt.date | None = None) -> Situacao:
    hoje = hoje or dt.date.today()
    cal = Calendario(cfg["incluir_facultativos"], cfg["feriados_extras"], cfg["feriados_removidos"])
    inicio, fim = ciclo(hoje, int(cfg["dia_reset"]))

    uso = [u for u in ler_uso() if inicio.isoformat() <= u["data"] <= fim.isoformat()]
    gasto_ciclo = sum(float(u["qtd"]) for u in uso)
    gasto_hoje = sum(float(u["qtd"]) for u in uso if u["data"] == hoje.isoformat())

    cota = float(cfg["cota_ciclo"])
    restante = max(0.0, cota - gasto_ciclo)

    uteis_totais = cal.contar(inicio, fim)
    uteis_restantes = cal.contar(hoje, fim)  # inclui hoje, se util
    uteis_decorridos = uteis_totais - uteis_restantes

    reserva_total = cota * float(cfg["reserva_pct"])
    reserva_efetiva = min(restante, reserva(reserva_total, uteis_restantes, uteis_totais))

    if uteis_restantes > 0:
        permitido_hoje = max(0.0, (restante - reserva_efetiva) / uteis_restantes)
    else:
        permitido_hoje = restante
    if not cal.eh_dia_util(hoje):
        permitido_hoje = 0.0

    saldo_hoje = permitido_hoje - gasto_hoje
    ritmo_alvo = cota / uteis_totais if uteis_totais else 0.0
    dias_contados = max(1, uteis_decorridos + (1 if cal.eh_dia_util(hoje) else 0))
    ritmo_real = gasto_ciclo / dias_contados
    projecao = gasto_ciclo + ritmo_real * max(0, uteis_restantes - (1 if cal.eh_dia_util(hoje) else 0))

    data_estouro = None
    if ritmo_real > 0:
        acumulado = gasto_ciclo
        for d in cal.dias_uteis(hoje + dt.timedelta(days=1), fim):
            acumulado += ritmo_real
            if acumulado >= cota:
                data_estouro = d.isoformat()
                break

    if gasto_ciclo >= cota:
        situacao = "estourado"
    elif projecao > cota * 1.02:
        situacao = "acima do ritmo"
    elif projecao < cota * 0.6:
        situacao = "sobrando muito"
    else:
        situacao = "no ritmo"

    return Situacao(
        hoje=hoje.isoformat(), hoje_eh_util=cal.eh_dia_util(hoje),
        motivo_nao_util=cal.motivo_nao_util(hoje),
        ciclo_inicio=inicio.isoformat(), ciclo_fim=fim.isoformat(),
        cota=round(cota, 2), gasto_ciclo=round(gasto_ciclo, 2),
        gasto_hoje=round(gasto_hoje, 2), restante=round(restante, 2),
        reserva_efetiva=round(reserva_efetiva, 2),
        dias_uteis_totais=uteis_totais, dias_uteis_decorridos=uteis_decorridos,
        dias_uteis_restantes=uteis_restantes,
        permitido_hoje=round(permitido_hoje, 2), saldo_hoje=round(saldo_hoje, 2),
        teto_emergencia=round(min(permitido_hoje * 2, restante), 2),
        ritmo_alvo=round(ritmo_alvo, 2), ritmo_real=round(ritmo_real, 2),
        projecao_fim_ciclo=round(projecao, 2), data_estouro=data_estouro,
        situacao=situacao,
    )


VERDE, AMARELO, VERMELHO, CINZA, NEGRITO, FIM = (
    ("\033[32m", "\033[33m", "\033[31m", "\033[90m", "\033[1m", "\033[0m")
    if sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    else ("", "", "", "", "", "")
)


def barra(fracao: float, largura: int = 32) -> str:
    fracao = max(0.0, min(1.0, fracao))
    cheio = int(round(fracao * largura))
    cor = VERDE if fracao < 0.7 else AMARELO if fracao < 0.95 else VERMELHO
    return f"{cor}{'#' * cheio}{CINZA}{'.' * (largura - cheio)}{FIM}"


def imprimir(s: Situacao, cfg: dict) -> None:
    unidade = cfg["unidade"]
    print()
    print(f"  {NEGRITO}Ciclo {s.ciclo_inicio} -> {s.ciclo_fim}{FIM}   ({unidade})")
    print(f"  {barra(s.gasto_ciclo / s.cota if s.cota else 0)}  "
          f"{s.gasto_ciclo:g} / {s.cota:g} usados  |  restam {NEGRITO}{s.restante:g}{FIM}")
    print()
    print(f"  Dias uteis:      {s.dias_uteis_decorridos} decorridos, "
          f"{NEGRITO}{s.dias_uteis_restantes} restantes{FIM} (de {s.dias_uteis_totais})")
    print(f"  Reserva retida:  {s.reserva_efetiva:g}  {CINZA}(liberada aos poucos ate o fim do ciclo){FIM}")
    print()
    if not s.hoje_eh_util:
        print(f"  {AMARELO}Hoje ({s.hoje}) nao e dia util: {s.motivo_nao_util}.{FIM}")
        print(f"  {CINZA}A cota diaria e distribuida so entre dias uteis. Se trabalhar hoje,{FIM}")
        print(f"  {CINZA}o gasto sai da reserva e aperta os proximos dias.{FIM}")
    else:
        cor = VERDE if s.saldo_hoje > 0 else VERMELHO
        print(f"  {NEGRITO}Hoje voce pode gastar: {cor}{s.permitido_hoje:g}{FIM}"
              f"   (ja gastou {s.gasto_hoje:g}, "
              f"{'sobram ' + format(s.saldo_hoje, 'g') if s.saldo_hoje >= 0 else 'passou ' + format(-s.saldo_hoje, 'g')})")
        print(f"  {CINZA}Teto de emergencia (queima 2 dias de uma vez): {s.teto_emergencia:g}{FIM}")
    print()
    cor_sit = {"no ritmo": VERDE, "sobrando muito": AMARELO,
               "acima do ritmo": VERMELHO, "estourado": VERMELHO}[s.situacao]
    print(f"  Ritmo alvo:  {s.ritmo_alvo:g}/dia util     "
          f"Ritmo real: {s.ritmo_real:g}/dia util   -> {cor_sit}{s.situacao}{FIM}")
    print(f"  Projecao de fim de ciclo: {s.projecao_fim_ciclo:g} de {s.cota:g}", end="")
    if s.data_estouro:
        print(f"   {VERMELHO}(a cota acaba em {s.data_estouro}){FIM}")
    else:
        print()
    print()


def cmd_orc_init(args) -> int:
    cfg = carregar_config()
    if args.cota is not None:
        cfg["cota_ciclo"] = float(args.cota)
    if args.unidade:
        cfg["unidade"] = args.unidade
    if args.dia_reset is not None:
        cfg["dia_reset"] = int(args.dia_reset)
    if args.reserva is not None:
        cfg["reserva_pct"] = float(args.reserva)
    salvar_config(cfg)
    USO_PATH.touch()
    print(f"config salva em {CONFIG_ORC}")
    print(json.dumps(cfg, indent=2, ensure_ascii=False))
    return 0


def cmd_status(args) -> int:
    cfg = carregar_config()
    s = calcular(cfg, args.data)
    if args.json:
        print(json.dumps(asdict(s), indent=2, ensure_ascii=False))
    else:
        imprimir(s, cfg)
    return 0


def cmd_gasto(args) -> int:
    cfg = carregar_config()
    qtd = float(args.qtd)
    if args.modelo and args.modelo in cfg["multiplicadores"]:
        qtd *= float(cfg["multiplicadores"][args.modelo])
    data = args.data or dt.date.today()
    registrar(qtd, args.modelo, args.nota, data)
    s = calcular(cfg, data)
    print(f"registrado: {qtd:g} em {data.isoformat()}"
          f"{' (' + args.modelo + ')' if args.modelo else ''}")
    if not args.quieto:
        imprimir(s, cfg)
    return 0


def cmd_pode(args) -> int:
    """Portao para scripts: sai com 0 se cabe no orcamento do dia, 1 se nao."""
    cfg = carregar_config()
    s = calcular(cfg, args.data)
    custo = float(args.custo)
    cabe = custo <= s.saldo_hoje
    limite = "saldo do dia"
    if not cabe and args.emergencia:
        cabe = custo <= s.restante - s.reserva_efetiva
        limite = "reserva"
    if args.json:
        print(json.dumps({"cabe": cabe, "custo": custo, "saldo_hoje": s.saldo_hoje,
                          "restante": s.restante, "limite": limite}, ensure_ascii=False))
    elif cabe:
        print(f"ok: {custo:g} cabe (saldo do dia: {s.saldo_hoje:g})")
    else:
        print(f"nao: {custo:g} excede o saldo do dia ({s.saldo_hoje:g})", file=sys.stderr)
    return 0 if cabe else 1


def cmd_orc_plano(args) -> int:
    cfg = carregar_config()
    hoje = args.data or dt.date.today()
    s = calcular(cfg, hoje)
    cal = Calendario(cfg["incluir_facultativos"], cfg["feriados_extras"], cfg["feriados_removidos"])
    fim = dt.date.fromisoformat(s.ciclo_fim)
    dias = cal.dias_uteis(hoje, fim)
    restante = s.restante
    reserva_total = s.cota * float(cfg["reserva_pct"])
    print(f"\n  Plano ate {s.ciclo_fim} ({len(dias)} dias uteis, {restante:g} {cfg['unidade']} restantes)\n")
    print(f"  {'data':<12} {'dia':<5} {'permitido':>10} {'acumulado':>11}")
    print(f"  {'-'*12} {'-'*5} {'-'*10} {'-'*11}")
    acum = 0.0
    for i, d in enumerate(dias):
        faltam = len(dias) - i
        retido = min(restante, reserva(reserva_total, faltam, s.dias_uteis_totais))
        permitido = max(0.0, (restante - retido) / faltam)
        acum += permitido
        restante -= permitido
        semana = ["seg", "ter", "qua", "qui", "sex"][d.weekday()]
        marca = f" {CINZA}<- hoje{FIM}" if d == hoje else ""
        print(f"  {d.isoformat():<12} {semana:<5} {permitido:>10.2f} {acum:>11.2f}{marca}")
    print()
    return 0


def cmd_resumo(args) -> int:
    cfg = carregar_config()
    hoje = args.data or dt.date.today()
    inicio, fim = ciclo(hoje, int(cfg["dia_reset"]))
    uso = [u for u in ler_uso() if inicio.isoformat() <= u["data"] <= fim.isoformat()]
    if not uso:
        print("nenhum uso registrado neste ciclo")
        return 0
    por_dia: dict[str, float] = {}
    por_modelo: dict[str, float] = {}
    for u in uso:
        por_dia[u["data"]] = por_dia.get(u["data"], 0) + float(u["qtd"])
        m = u.get("modelo", "(sem modelo)")
        por_modelo[m] = por_modelo.get(m, 0) + float(u["qtd"])
    pico = max(por_dia.values()) or 1
    print(f"\n  Uso por dia ({inicio} -> {fim}):")
    for d in sorted(por_dia):
        print(f"    {d}  {por_dia[d]:>8.2f}  {'#' * int(round(por_dia[d] / pico * 30))}")
    print("\n  Uso por modelo:")
    for m, v in sorted(por_modelo.items(), key=lambda kv: -kv[1]):
        print(f"    {m:<28} {v:>8.2f}")
    print()
    return 0


def _gh(argumentos: list[str]) -> str:
    try:
        r = subprocess.run(["gh"] + argumentos, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        raise RuntimeError("o comando 'gh' (GitHub CLI) nao esta instalado ou nao esta no PATH")
    except subprocess.TimeoutExpired:
        raise RuntimeError("gh demorou demais para responder")
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[:500] or f"gh saiu com codigo {r.returncode}")
    return r.stdout


def consumo_api(login: str, ano: int, mes: int) -> tuple[float, int]:
    """Consumo de AI Credits do mes segundo a API de billing do GitHub.

    Usa GET /users/{login}/settings/billing/ai_credit/usage. A resposta traz
    usageItems com grossQuantity (total), discountQuantity (coberto pela cota
    do plano) e netQuantity (excedente cobrado). Parsing defensivo: o formato
    e recente e pode ganhar campos.

    Limitacao conhecida: a chamada nao pagina. Os itens sao agregados por
    modelo/sku no mes, entao a lista e curta na pratica; se um dia o total
    parecer menor que o do site, suspeite de paginacao.
    """
    saida = _gh([
        "api", f"/users/{login}/settings/billing/ai_credit/usage?year={ano}&month={mes}",
        "-H", "X-GitHub-Api-Version: 2026-03-10",
    ])
    dados = json.loads(saida)
    itens = None
    if isinstance(dados, list):
        itens = dados
    elif isinstance(dados, dict):
        for chave, valor in dados.items():
            if isinstance(valor, list) and "item" in chave.lower():
                itens = valor
                break
    if itens is None:
        chaves = sorted(dados)[:8] if isinstance(dados, dict) else type(dados).__name__
        raise RuntimeError(f"resposta sem lista de usageItems (topo: {chaves})")
    total = 0.0
    for item in itens:
        if not isinstance(item, dict):
            continue
        bruto = item.get("grossQuantity")
        if bruto is None:
            bruto = float(item.get("discountQuantity") or 0) + float(item.get("netQuantity") or 0)
        total += float(bruto or 0)
    return total, len(itens)


def cmd_sincronizar(args) -> int:
    """Ajusta o registro local para bater com o consumo real medido pelo GitHub.

    O uso.jsonl so conhece o que foi registrado aqui; o que voce gastar no
    VS Code ou no site fica de fora. Este comando busca o total do mes na API
    e registra a diferenca. Idempotente: a diferenca converge para zero.
    """
    cfg = carregar_config()
    hoje = args.data or dt.date.today()
    if int(cfg["dia_reset"]) != 1:
        print(f"aviso: dia_reset={cfg['dia_reset']}, mas a API do GitHub agrega por mes "
              "civil (a cota do Copilot reseta dia 1 UTC) — sincronizando o mes de hoje.",
              file=sys.stderr)
    try:
        login = args.login or _gh(["api", "user", "--jq", ".login"]).strip()
        total_api, n_itens = consumo_api(login, hoje.year, hoje.month)
    except (RuntimeError, json.JSONDecodeError, AttributeError) as e:
        print(f"erro ao consultar a API de billing: {e}", file=sys.stderr)
        print("Dicas: 'gh auth login' primeiro; se reclamar de escopo, "
              "'gh auth refresh -h github.com -s user'. Se a conta e corporativa, o "
              "endpoint de usuario pode nao expor o consumo (peca ao admin) — registre "
              "na mao com 'gasto'. Planos anuais legados usam premium_request/usage.",
              file=sys.stderr)
        return 1

    # Compara mes civil com mes civil: a API agrega por mes, entao o total local
    # tambem precisa ser o do mes de hoje (nao o do ciclo). O lancamento de
    # ajuste e datado de hoje, que pertence aos dois recortes.
    inicio_mes = hoje.replace(day=1)
    prox_mes = _clamp_dia(hoje.year, hoje.month, 31) + dt.timedelta(days=1)
    local = sum(float(u["qtd"]) for u in ler_uso()
                if inicio_mes.isoformat() <= u["data"] < prox_mes.isoformat())
    diferenca = round(total_api - local, 4)
    print(f"API ({login}, {hoje.year}-{hoje.month:02d}): {total_api:g} {cfg['unidade']} "
          f"em {n_itens} itens | registrado localmente: {local:g}")
    if abs(diferenca) < 0.005:
        print("ja esta em dia — nada a ajustar")
    else:
        if diferenca < 0:
            print("aviso: a API reporta MENOS que o registro local — pode ser atraso de "
                  "ingestao do lado do GitHub (gasto de hoje ainda nao contabilizado). "
                  "O proximo sincronizar reequilibra.", file=sys.stderr)
        registrar(diferenca, "(sincronizacao)", f"ajuste para bater com a API ({total_api:g} no mes)", hoje)
        verbo = "acrescentado" if diferenca > 0 else "abatido"
        print(f"{verbo} {abs(diferenca):g} para bater com a API")
    imprimir(calcular(cfg, hoje), cfg)
    return 0


# =========================================================================== #
# MAPA — mapa do repositorio gerado localmente (custo zero)
# =========================================================================== #

LINGUAGENS: dict[str, tuple[str, list[str]]] = {
    ".py": ("Python", [r"^class\s+(\w+)", r"^def\s+(\w+)", r"^async\s+def\s+(\w+)"]),
    ".js": ("JavaScript", [
        r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", r"^(?:export\s+)?class\s+(\w+)",
        r"^(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s*)?\(", r"^module\.exports\.(\w+)",
    ]),
    ".ts": ("TypeScript", [
        r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", r"^(?:export\s+)?class\s+(\w+)",
        r"^(?:export\s+)?(?:interface|type|enum)\s+(\w+)", r"^(?:export\s+)?const\s+(\w+)\s*[:=]",
    ]),
    ".java": ("Java", [r"^\s*(?:public|protected|private)?\s*(?:abstract\s+|final\s+)?(?:class|interface|enum|record)\s+(\w+)"]),
    ".go": ("Go", [r"^func\s+(?:\([^)]*\)\s*)?(\w+)", r"^type\s+(\w+)"]),
    ".rb": ("Ruby", [r"^\s*class\s+(\w+)", r"^\s*module\s+(\w+)", r"^\s*def\s+(\w+)"]),
    ".php": ("PHP", [r"^\s*(?:abstract\s+|final\s+)?class\s+(\w+)", r"^\s*function\s+(\w+)"]),
    ".cs": ("C#", [r"^\s*(?:public|internal|private)?\s*(?:static\s+|abstract\s+|sealed\s+)*(?:class|interface|record|struct|enum)\s+(\w+)"]),
    ".rs": ("Rust", [r"^(?:pub\s+)?fn\s+(\w+)", r"^(?:pub\s+)?(?:struct|enum|trait|impl)\s+(\w+)"]),
    ".sql": ("SQL", [r"(?i)^\s*create\s+(?:or\s+replace\s+)?(?:table|view|function|procedure)\s+[`\"\[]?(\w+)"]),
    ".sh": ("Shell", [r"^(?:function\s+)?(\w+)\s*\(\)\s*\{"]),
}
LINGUAGENS[".jsx"] = LINGUAGENS[".js"]
LINGUAGENS[".tsx"] = LINGUAGENS[".ts"]
LINGUAGENS[".mjs"] = LINGUAGENS[".js"]
LINGUAGENS[".cjs"] = LINGUAGENS[".js"]

IGNORAR_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env", "dist", "build",
    "target", "out", ".next", ".nuxt", "vendor", ".idea", ".vscode", "coverage",
    ".pytest_cache", ".mypy_cache", ".tox", "bin", "obj", ".gradle", ".terraform",
}
IGNORAR_ARQ = re.compile(
    r"\.(min\.js|min\.css|lock|map|png|jpe?g|gif|svg|ico|pdf|zip|tar|gz|exe|dll|so|"
    r"woff2?|ttf|eot|mp[34]|avi|mov|class|pyc|jar|war)$", re.I,
)
NOTAVEIS = {
    "readme.md", "readme.rst", "contributing.md", "makefile", "dockerfile",
    "docker-compose.yml", "package.json", "pyproject.toml", "setup.py",
    "requirements.txt", "pom.xml", "build.gradle", "go.mod", "cargo.toml",
    "composer.json", "gemfile", "main.py", "app.py", "manage.py", "index.js",
    "index.ts", "main.go", "main.java", "program.cs", "agents.md",
    ".github/copilot-instructions.md", "claude.md",
}
IMPORT_RE = {
    ".py": re.compile(r"^(?:from\s+([\w.]+)|import\s+([\w.]+))"),
    ".js": re.compile(r"""(?:from\s+['"]([^'"]+)['"]|require\(['"]([^'"]+)['"]\))"""),
}
IMPORT_RE[".ts"] = IMPORT_RE[".tsx"] = IMPORT_RE[".jsx"] = IMPORT_RE[".js"]


def listar_arquivos(raiz: Path) -> list[Path]:
    """Prefere o git (respeita .gitignore de graca); cai para os.walk sem ele."""
    try:
        saida = subprocess.run(
            ["git", "-C", str(raiz), "ls-files"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout
        arquivos = [raiz / linha for linha in saida.splitlines() if linha.strip()]
        if arquivos:
            return [a for a in arquivos if a.is_file()]
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass
    arquivos = []
    for dirpath, dirnames, filenames in os.walk(raiz):
        dirnames[:] = [d for d in dirnames if d not in IGNORAR_DIRS and not d.startswith(".")]
        for nome in filenames:
            arquivos.append(Path(dirpath) / nome)
    return arquivos


def extrair_python(texto: str, max_simbolos: int) -> tuple[list[str], list[str]] | None:
    """Simbolos e imports via ast (pega metodos de classe). None -> cai no regex."""
    try:
        arvore = ast.parse(texto)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    simbolos: list[str] = []
    for no in arvore.body:
        if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not no.name.startswith("_"):
                simbolos.append(no.name)
        elif isinstance(no, ast.ClassDef):
            if no.name.startswith("_"):
                continue
            simbolos.append(no.name)
            for filho in no.body:
                if isinstance(filho, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and not filho.name.startswith("_"):
                    simbolos.append(f"{no.name}.{filho.name}")
    if len(simbolos) > max_simbolos:
        simbolos = simbolos[:max_simbolos] + ["..."]
    imports: list[str] = []
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            imports += [alias.name for alias in no.names]
        elif isinstance(no, ast.ImportFrom) and no.module and no.level == 0:
            imports.append(no.module)
    return simbolos, imports


def extrair(caminho: Path, max_simbolos: int) -> tuple[int, list[str], list[str]]:
    ext = caminho.suffix.lower()
    lingua = LINGUAGENS.get(ext)
    try:
        texto = caminho.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return 0, [], []
    linhas = texto.count("\n") + 1
    if not lingua:
        return linhas, [], []
    if ext == ".py":
        preciso = extrair_python(texto, max_simbolos)
        if preciso is not None:
            return linhas, preciso[0], preciso[1]
    padroes = [re.compile(p) for p in lingua[1]]
    simbolos: list[str] = []
    for linha in texto.splitlines():
        if len(simbolos) >= max_simbolos:
            simbolos.append("...")
            break
        for pad in padroes:
            m = pad.match(linha)
            if m:
                nome = next((g for g in m.groups() if g), None)
                if nome and not nome.startswith("_") and nome not in simbolos:
                    simbolos.append(nome)
                break
    imports: list[str] = []
    imp_re = IMPORT_RE.get(ext)
    if imp_re:
        for linha in texto.splitlines()[:120]:
            m = imp_re.search(linha)
            if m:
                alvo = next((g for g in m.groups() if g), None)
                if alvo:
                    imports.append(alvo)
    return linhas, simbolos, imports


def humano(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    return f"{n/1000:.1f}k" if n >= 1000 else str(n)


def gerar_mapa(raiz: Path, max_simbolos: int, max_arquivos_detalhe: int) -> str:
    arquivos = [
        a for a in listar_arquivos(raiz)
        if not IGNORAR_ARQ.search(a.name)
        and not any(p in IGNORAR_DIRS for p in a.parts)
    ]
    dados = []
    por_lingua: Counter[str] = Counter()
    linhas_por_lingua: Counter[str] = Counter()
    imports_externos: Counter[str] = Counter()
    total_bytes = 0
    for a in arquivos:
        try:
            tam = a.stat().st_size
        except OSError:
            continue
        if tam > 800_000:
            continue
        total_bytes += tam
        linhas, simbolos, imports = extrair(a, max_simbolos)
        ext = a.suffix.lower()
        nome_lingua = LINGUAGENS.get(ext, (ext or "(sem extensao)", []))[0]
        por_lingua[nome_lingua] += 1
        linhas_por_lingua[nome_lingua] += linhas
        for imp in imports:
            if not imp.startswith((".", "/")):
                imports_externos[imp.split(".")[0].split("/")[0]] += 1
        dados.append({"rel": a.relative_to(raiz).as_posix(), "linhas": linhas, "simbolos": simbolos})

    dados.sort(key=lambda d: -d["linhas"])
    por_dir: dict[str, list[dict]] = defaultdict(list)
    for d in dados:
        por_dir[str(Path(d["rel"]).parent)].append(d)

    out: list[str] = []
    w = out.append
    w(f"# Mapa de `{raiz.name}`")
    w("")
    w("> Gerado localmente por `copiloto.py mapa`. Custo zero de credito.")
    w("> Leia isto antes de sair abrindo arquivo.")
    w("")
    w("## Resumo")
    w("")
    w(f"- Arquivos analisados: **{len(dados)}**")
    w(f"- Linhas de codigo: **{humano(sum(d['linhas'] for d in dados))}**")
    w(f"- Tamanho: **{total_bytes/1024/1024:.1f} MB**")
    w(f"- Estimativa de tokens do repo inteiro: **~{humano(int(total_bytes/3.7))}** "
      "(por isso nao se manda o repo inteiro)")
    w("")
    w("| Linguagem | Arquivos | Linhas |")
    w("|---|---:|---:|")
    for lingua, n in por_lingua.most_common(12):
        w(f"| {lingua} | {n} | {humano(linhas_por_lingua[lingua])} |")
    w("")
    notaveis = [d for d in dados if d["rel"].lower() in NOTAVEIS
                or Path(d["rel"]).name.lower() in NOTAVEIS]
    if notaveis:
        w("## Pontos de entrada e convencoes")
        w("")
        for d in sorted(notaveis, key=lambda d: d["rel"]):
            w(f"- `{d['rel']}` ({d['linhas']} linhas)")
        w("")
    if imports_externos:
        w("## Dependencias mais usadas")
        w("")
        w(", ".join(f"`{nome}` ({n}x)" for nome, n in imports_externos.most_common(15)))
        w("")
    w("## Arquivos maiores")
    w("")
    w("| Arquivo | Linhas |")
    w("|---|---:|")
    for d in dados[:15]:
        w(f"| `{d['rel']}` | {d['linhas']} |")
    w("")
    w("## Estrutura e simbolos")
    w("")
    detalhados = 0
    for dirname in sorted(por_dir):
        arqs = sorted(por_dir[dirname], key=lambda d: -d["linhas"])
        rotulo = "." if dirname == "." else dirname
        total_linhas = sum(a["linhas"] for a in arqs)
        w(f"### `{rotulo}/`  — {len(arqs)} arquivos, {humano(total_linhas)} linhas")
        w("")
        for d in arqs:
            if d["simbolos"] and detalhados < max_arquivos_detalhe:
                w(f"- `{Path(d['rel']).name}` ({d['linhas']}L): "
                  + ", ".join(f"`{s}`" for s in d["simbolos"]))
                detalhados += 1
            else:
                w(f"- `{Path(d['rel']).name}` ({d['linhas']}L)")
        w("")
    if detalhados >= max_arquivos_detalhe:
        w(f"> Simbolos omitidos apos {max_arquivos_detalhe} arquivos para nao inflar o mapa.")
        w("")
    return "\n".join(out)


def cmd_mapa(args) -> int:
    raizes = [Path(r).resolve() for r in (args.raizes or ["."])]
    for r in raizes:
        if not r.is_dir():
            print(f"erro: {r} nao e um diretorio", file=sys.stderr)
            return 1
    partes = []
    if len(raizes) > 1:
        partes.append("# Mapa do projeto (" + " + ".join(f"`{r.name}`" for r in raizes) + ")")
        partes.append("")
        partes.append("> Projeto em varios repositorios; cada secao abaixo e um deles.")
        partes.append("")
    for r in raizes:
        partes.append(gerar_mapa(r, args.max_simbolos, args.max_arquivos_detalhe))
        partes.append("")
    texto = "\n".join(partes)
    if args.stdout:
        print(texto)
    else:
        destino = Path(args.saida)
        destino.write_text(texto, encoding="utf-8")
        tokens = int(len(texto) / 3.7)
        print(f"{destino} gerado: {len(texto)} chars, ~{tokens} tokens de contexto")
    return 0


# =========================================================================== #
# HARNESS — planejador (forte) -> executor (barato) -> validador (forte)
# =========================================================================== #

PADRAO_HARNESS = {
    "_comentario": (
        "'cmd' de cada papel: o prompt e enviado por STDIN (sem limite de tamanho de "
        "linha de comando). Se a sua versao do CLI nao aceitar prompt por stdin, "
        "acrescente '-p', '{prompt}' ao cmd. {usage_file} vira um JSON com o consumo "
        "real da chamada (--usage-output-file). 'custo' e a ESTIMATIVA em creditos "
        "usada pelo portao ANTES de chamar; se o usage_file trouxer o consumo real, e "
        "ele que e registrado. 'reusar_sessao' encadeia as chamadas de cada papel na "
        "mesma sessao (--resume). 'repos' lista os repositorios do projeto (fonte, "
        "config...): o diff do validador e as verificacoes cobrem todos. 'add_dirs' e "
        "repassado ao copilot como --add-dir para ele poder tocar em cada pasta. "
        "Confira os nomes de modelos com /model dentro do copilot."
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
    "repos": ["."],
    "add_dirs": [],
    "verificacoes": [],
    "reusar_sessao": True,
    "max_tentativas_executor": 2,
    "max_rodadas_ajuste": 2,
    "timeout_seg": 900,
}

PROMPT_PLANEJAR = """\
# Papel: PLANEJADOR (modelo forte, caro — use pouco)

Voce planeja, voce NAO edita arquivo nenhum. Nao escreva codigo de implementacao.
Sua unica entrega e um plano que um modelo mais fraco consiga executar sem pensar.

## Por que isso importa

Quem vai executar seu plano e um modelo barato, com pouca capacidade de inferir
contexto. Toda ambiguidade que voce deixar vira retrabalho — e retrabalho custa
uma nova chamada ao modelo caro. Um passo mal especificado custa mais caro do que
voce ter gasto 200 tokens a mais especificando ele direito.

## Antes de planejar

1. Leia `MAPA.md` (se existir) em vez de varrer o repositorio. Ele ja tem a
   estrutura, os pontos de entrada e as convencoes.
2. Se faltar informacao, use busca dirigida (`rg`) e leia SO os trechos
   necessarios. Nao leia arquivo inteiro para ver uma funcao.
3. Se depois disso ainda faltar algo essencial, pare e pergunte. Perguntar e
   barato; planejar em cima de suposicao errada e caro.

## Formato obrigatorio da saida

Escreva em `plano.md`:

```markdown
# Objetivo
<uma frase: o que muda no comportamento do sistema quando isso terminar>

# Fora de escopo
<o que explicitamente NAO deve ser tocado>

# Riscos
<o que pode quebrar, e o que ja existe hoje que depende disso>

# Passos

## Passo 1 — <titulo curto no imperativo>
- **Arquivos:** caminho/exato.py (linhas ~120-160)
- **Mudanca:** <descricao precisa: qual funcao, qual assinatura, qual comportamento>
- **Nao faca:** <as tentacoes obvias que estariam erradas aqui>
- **Criterio de aceite:** <como se sabe que ficou certo, de forma objetiva>
- **Verificacao local (de graca):** `pytest tests/test_x.py::test_y -q`
- **Custo estimado:** baixo | medio | alto
```

## Regras dos passos

- Cada passo cabe numa cabeca so: um arquivo, ou um conjunto pequeno e coeso.
  Se um passo precisa de mais de ~80 linhas de diff, quebre em dois.
- Passos independentes entre si sempre que possivel.
- **Todo passo precisa de uma verificacao local que rode de graca** (teste,
  linter, type checker, build). Se voce nao consegue pensar numa, o passo esta
  mal definido: o validador caro nao pode ser a primeira linha de defesa.
- Ordene por dependencia, e diga quando o passo N depende do N-1.
- No maximo 8 passos. Mais que isso, entregue a fase 1 e diga que ha fase 2.
"""

PROMPT_EXECUTAR = """\
# Papel: EXECUTOR (modelo barato — faz o volume do trabalho)

Voce executa **exatamente um passo** do `plano.md`. Nada alem dele.

## Regras

1. Faca literalmente o que o passo manda. Se o plano diz para mexer em
   `parser.py`, nao encoste em `utils.py`.
2. Nao "aproveite para" arrumar outra coisa. Nao renomeie o que nao foi pedido.
   Nao reformate arquivo inteiro. Nao adicione dependencia nova.
3. Nao mude testes para eles passarem. Se um teste existente quebra, isso e um
   achado — reporte, nao esconda.
4. Rode a **verificacao local** que o passo especifica, e cole a saida dela.
5. Se o passo estiver ambiguo ou impossivel como escrito, **pare e reporte**.
   Nao adivinhe.

## Saida obrigatoria

```
PASSO: <numero e titulo>
STATUS: FEITO | BLOQUEADO
ARQUIVOS: <lista dos arquivos que voce realmente alterou>
VERIFICACAO: <comando que rodou>
RESULTADO: <saida resumida: passou / falhou e por que>
DESVIOS: <qualquer coisa diferente do plano, ou "nenhum">
```

Um bloqueio honesto e barato. Um passo "feito" que nao funciona custa duas
chamadas ao modelo caro para descobrir e consertar.
"""

PROMPT_VALIDAR = """\
# Papel: VALIDADOR (modelo forte, caro — a ultima linha de defesa)

Voce nao esta aqui para descobrir que o teste falhou. Isso o script ja fez de
graca antes de te chamar. Voce esta aqui para o que so um modelo forte enxerga:
o codigo passa nos testes **e mesmo assim esta errado**.

## O que voce recebe

- O trecho relevante do `plano.md` (o passo em questao)
- O `git diff` do que foi feito — **so o diff**
- A saida das verificacoes locais (que ja passaram)

## O que procurar, nesta ordem

1. **Cumpriu o criterio de aceite?** Compare o diff com o criterio, literalmente.
2. **Escopo:** o executor mexeu em algo que o plano nao mandava? E o erro mais
   comum de modelo barato.
3. **Correcao de verdade:** borda que o teste nao cobre, off-by-one, erro
   engolido, condicao invertida, estado compartilhado, concorrencia.
4. **Regressao:** algo que hoje depende desse comportamento vai quebrar.
5. **Teste dopado:** afrouxaram asserção ou mudaram o teste em vez do codigo?

Nao comente estilo, nomenclatura ou preferencia. Isso e trabalho de linter, e
linter e de graca.

## Saida obrigatoria

```
VEREDITO: APROVADO | AJUSTAR | REFAZER
```

- **APROVADO** — segue para o proximo passo. Nao escreva mais nada.
- **AJUSTAR** — o caminho esta certo, falta corrigir ponto especifico:

```
AJUSTES:
1. arquivo.py:linha — <o que esta errado> — <o que fazer exatamente>
```

- **REFAZER** — a abordagem esta errada na raiz. Duas frases: por que, e o que
  o plano deveria ter dito. Isso volta para o PLANEJADOR.

## Regra de custo

Na duvida entre APROVADO e AJUSTAR por algo pequeno e reversivel, aprove e
registre numa linha `RESSALVA:`. Rodada extra de ajuste custa creditos reais.
"""

PROMPTS = {
    "01-planejar.md": PROMPT_PLANEJAR,
    "02-executar.md": PROMPT_EXECUTAR,
    "03-validar.md": PROMPT_VALIDAR,
}


def ler_prompt(nome: str) -> str:
    """Prompt embutido, com override opcional por arquivo em ./prompts/."""
    for base in (Path.cwd() / "prompts", Path(__file__).parent / "prompts"):
        caminho = base / nome
        if caminho.exists():
            return caminho.read_text(encoding="utf-8")
    return PROMPTS[nome]


def carregar_harness() -> dict:
    """PADRAO <- ~/.copiloto/harness.json <- ./copiloto.json (mais especifico vence)."""
    cfg = json.loads(json.dumps(PADRAO_HARNESS))
    for caminho in (CONFIG_HARNESS_GLOBAL, CONFIG_PROJETO):
        if caminho.exists():
            try:
                cfg.update(json.loads(caminho.read_text(encoding="utf-8")))
            except json.JSONDecodeError:
                print(f"erro: {caminho} nao e JSON valido", file=sys.stderr)
                sys.exit(2)
    return cfg


def detectar_verificacoes(raiz: Path) -> list[str]:
    """Adivinha os comandos de verificacao gratuitos de um repo."""
    v = []
    prefixo = "" if raiz == Path(".").resolve() or str(raiz) == "." else f"cd {shlex.quote(str(raiz))} && "
    raiz = Path(raiz)
    if (raiz / "pyproject.toml").exists() or (raiz / "setup.py").exists() or list(raiz.glob("test_*.py")):
        if (raiz / "tests").is_dir() or list(raiz.glob("test_*.py")):
            v.append(prefixo + "python -m pytest -q")
        v.append(prefixo + "python -m compileall -q .")
    pkg = raiz / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
        except json.JSONDecodeError:
            scripts = {}
        for nome in ("typecheck", "lint", "test"):
            if nome in scripts:
                v.append(prefixo + f"npm run {nome} --silent")
    if (raiz / "go.mod").exists():
        v += [prefixo + "go build ./...", prefixo + "go test ./..."]
    if (raiz / "Cargo.toml").exists():
        v.append(prefixo + "cargo check")
    return v


class SemOrcamento(Exception):
    pass


def portao(custo: float, descricao: str, forcar: bool) -> None:
    if custo <= 0:
        return
    s = calcular(carregar_config())
    if custo <= s.saldo_hoje or forcar:
        return
    if not s.hoje_eh_util:
        motivo = f"hoje e {s.motivo_nao_util}, e a cota e distribuida so entre dias uteis"
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
        registrar(custo, modelo, nota, dt.date.today())


def _creditos_do_usage(caminho: Path) -> float | None:
    """Total de creditos do JSON de --usage-output-file (formato nao documentado
    como estavel; leitura defensiva: campos numericos com 'credit' no nome)."""
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    totais: list[float] = []
    parciais: list[float] = []

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
        # Sem campo de total: o maior valor e o palpite seguro (total >= parte;
        # somar partes + total contaria em dobro).
        return max(parciais)
    return None


SESSOES: dict[str, str] = {}  # papel -> id da sessao do copilot nesta execucao


def _dir_sessoes() -> Path:
    return Path(os.environ.get("COPILOT_HOME", str(Path.home() / ".copilot"))) / "session-state"


def _capturar_sessao(papel: str, inicio: float) -> None:
    """Melhor esforco: sessao mais recente criada apos o inicio da chamada,
    ignorando as ja reivindicadas por outros papeis."""
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
    return papel in SESSOES


def chamar(cfg: dict, papel: str, prompt: str, forcar: bool, nota: str) -> str:
    conf = cfg["papeis"][papel]
    custo = float(conf.get("custo", 0))
    portao(custo, f"{papel}: {nota}", forcar)

    usage_file = None
    via_argv = any("{prompt}" in parte for parte in conf["cmd"])
    if not via_argv and any(parte in ("-p", "--prompt") for parte in conf["cmd"]):
        # -p sem {prompt} engoliria a flag seguinte como prompt — e pagaria errado.
        print(f"erro: o cmd do papel '{papel}' tem -p/--prompt sem o placeholder "
              "{prompt}. Ou remova o -p (prompt vai por stdin) ou use '-p', "
              "'{prompt}' juntos.", file=sys.stderr)
        sys.exit(2)
    cmd = []
    for parte in conf["cmd"]:
        if "{usage_file}" in parte:
            if usage_file is None:
                fd, tmp = tempfile.mkstemp(prefix="copiloto-usage-", suffix=".json")
                os.close(fd)
                usage_file = Path(tmp)
            parte = parte.replace("{usage_file}", str(usage_file))
        cmd.append(parte.replace("{prompt}", prompt))
    entrada = None if via_argv else prompt

    for d in cfg.get("add_dirs", []):
        cmd += ["--add-dir", d]
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
        print(f"erro: comando '{cmd[0]}' nao encontrado. Ajuste 'cmd' em copiloto.json.",
              file=sys.stderr)
        sys.exit(2)
    except subprocess.TimeoutExpired:
        if usage_file:
            usage_file.unlink(missing_ok=True)
        cobrar(custo, modelo, f"{nota} (timeout)")
        raise RuntimeError(f"{papel} estourou o timeout")
    # So captura sessao de chamada bem-sucedida: falha rapida nao cria sessao,
    # e capturar aqui pegaria a sessao de outro papel.
    if reusar and r.returncode == 0:
        _capturar_sessao(papel, inicio)

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


def git_diff(cfg: dict) -> str:
    """Diff de TODOS os repos do projeto (fonte, config...), rotulado por repo."""
    partes = []
    for repo in cfg.get("repos", ["."]):
        try:
            r = subprocess.run(["git", "-C", repo, "diff", "HEAD"],
                               capture_output=True, text=True, timeout=60)
            corpo = r.stdout if r.returncode == 0 else f"(git falhou em {repo}: {r.stderr.strip()[:200]})"
        except (subprocess.SubprocessError, FileNotFoundError):
            corpo = f"(git indisponivel em {repo})"
        if len(cfg.get("repos", ["."])) > 1:
            partes.append(f"### repo: {repo}\n{corpo}")
        else:
            partes.append(corpo)
    return "\n\n".join(partes)


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


def contexto_mapa() -> str:
    m = Path("MAPA.md")
    if m.exists():
        return f"\n\n--- MAPA DO REPOSITORIO ---\n{m.read_text(encoding='utf-8')}\n"
    return ("\n\n(Nao ha MAPA.md. Gere com `python3 copiloto.py mapa` antes, para nao "
            "gastar credito explorando o repo.)\n")


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


def cmd_harness_init(args) -> int:
    if CONFIG_PROJETO.exists() and not args.sobrescrever:
        print(f"{CONFIG_PROJETO} ja existe (use --sobrescrever para regenerar)")
        return 0
    cfg = json.loads(json.dumps(PADRAO_HARNESS))
    if CONFIG_HARNESS_GLOBAL.exists():
        try:
            cfg.update(json.loads(CONFIG_HARNESS_GLOBAL.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    repos = args.repo or ["."]
    cfg["repos"] = repos
    cfg["add_dirs"] = [r for r in repos if r != "."]
    detectadas = []
    for r in repos:
        detectadas += detectar_verificacoes(Path(r))
    if detectadas:
        cfg["verificacoes"] = detectadas
        print("verificacoes locais detectadas:")
        for v in detectadas:
            print(f"  - {v}")
    CONFIG_PROJETO.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nconfig do projeto em {CONFIG_PROJETO.resolve()}")
    print("Ajuste os nomes dos modelos em 'papeis' antes de rodar de verdade.")
    return 0


def cmd_planejar(args) -> int:
    cfg = carregar_harness()
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
    # de novo. Com sessao ativa, o follow-up e curto.
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

    for rodada in range(1, int(cfg["max_rodadas_ajuste"]) + 1):
        prompt_val = (
            ler_prompt("03-validar.md")
            + "\n\n--- PASSO DO PLANO ---\n" + passo["texto"]
            + "\n\n--- DIFF ---\n```diff\n" + git_diff(cfg)[:30000] + "\n```"
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


def cmd_rodar(args) -> int:
    cfg = carregar_harness()
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
    imprimir(calcular(carregar_config()), carregar_config())
    return 0 if all(resultados.get(p["n"]) == "aprovado" for p in passos) else 1


def cmd_custo(args) -> int:
    cfg = carregar_config()
    imprimir(calcular(cfg), cfg)
    return 0


# =========================================================================== #
# INSTALAR — grava os custom agents e o AGENTS.md num repositorio
# =========================================================================== #

TPL_AGENTS_MD = """\
# Instrucoes do projeto

<!-- O Copilot CLI carrega este arquivo automaticamente em toda sessao; ele
     precisa ficar CURTO (menos de ~100 linhas): tudo aqui vira tokens de
     entrada em cada prompt. Detalhe vai para .memory/ e e lido sob demanda. -->

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
Ao concluir uma tarefa que revelou algo duravel, acrescente 1-3 linhas no
arquivo certo — sem reescrever o arquivo.
"""

TPL_AGENT_PLANEJADOR = """\
---
name: planejador
description: Planeja a tarefa em passos executaveis por um modelo barato. Nao edita codigo.
tools: ['shell(rg:*)', 'shell(git:*)', 'shell(cat:*)', 'shell(ls:*)', 'write(plano.md)']
---

""" + PROMPT_PLANEJAR

TPL_AGENT_EXECUTOR = """\
---
name: executor
description: Executa exatamente um passo do plano.md. Modelo barato, faz o volume do trabalho.
---

""" + PROMPT_EXECUTAR

TPL_AGENT_VALIDADOR = """\
---
name: validador
description: Valida o diff de um passo contra o plano. Modelo forte, so leitura.
tools: ['shell(git:*)', 'shell(rg:*)', 'shell(cat:*)']
---

""" + PROMPT_VALIDAR

TPL_MCP = """\
{
  "_comentario": [
    "Exemplo de ~/.copilot/mcp-config.json com o servidor de memoria em grafo.",
    "Em Copilot Business/Enterprise a politica 'MCP servers in Copilot' vem",
    "DESLIGADA por padrao — se o CLI reclamar de 'disabled by your",
    "organization's Copilot policy', e o admin que libera. Nesse caso use o",
    "padrao AGENTS.md + .memory/ que nao depende de MCP.",
    "Troque o caminho de MEMORY_FILE_PATH pelo seu."
  ],
  "mcpServers": {
    "memoria": {
      "type": "local",
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-memory"],
      "tools": ["*"],
      "env": { "MEMORY_FILE_PATH": "TROQUE/POR/SEU/CAMINHO/memoria-grafo.jsonl" }
    }
  }
}
"""


def cmd_instalar(args) -> int:
    """Grava .github/agents/*.agent.md e AGENTS.md no repo alvo."""
    destino = Path(args.repo).resolve()
    if not destino.is_dir():
        print(f"erro: {destino} nao e um diretorio", file=sys.stderr)
        return 1
    agents_dir = destino / ".github" / "agents"
    agents_dir.mkdir(parents=True, exist_ok=True)
    arquivos = {
        agents_dir / "planejador.agent.md": TPL_AGENT_PLANEJADOR,
        agents_dir / "executor.agent.md": TPL_AGENT_EXECUTOR,
        agents_dir / "validador.agent.md": TPL_AGENT_VALIDADOR,
        destino / "AGENTS.md": TPL_AGENTS_MD,
    }
    for caminho, conteudo in arquivos.items():
        if caminho.exists() and not args.sobrescrever:
            print(f"  pulado (ja existe): {caminho}")
            continue
        caminho.write_text(conteudo, encoding="utf-8")
        print(f"  gravado: {caminho}")
    if args.mcp:
        print("\nExemplo de MCP de memoria (copie para ~/.copilot/mcp-config.json):\n")
        print(TPL_MCP)
    return 0


# =========================================================================== #
# CASCA — pedir (prompt avulso), sessao (interativo) e atualizar (auto-update)
# =========================================================================== #

def cmd_pedir(args) -> int:
    """Prompt avulso para o copilot, passando pelo portao de orcamento.

    Usa o papel 'executor' (modelo barato) por padrao; --forte usa o
    planejador. O consumo real (ou a estimativa) e registrado no orcamento.
    """
    cfg = json.loads(json.dumps(carregar_harness()))
    papel = "planejador" if args.forte else "executor"
    conf = cfg["papeis"][papel]
    if args.modelo:
        cmd = conf["cmd"]
        if "--model" in cmd:
            cmd[cmd.index("--model") + 1] = args.modelo
        else:
            conf["cmd"] = cmd + ["--model", args.modelo]
    if args.custo is not None:
        conf["custo"] = float(args.custo)
    try:
        saida = chamar(cfg, papel, args.prompt, args.forcar, args.prompt[:60])
    except SemOrcamento as e:
        print(f"[ORCAMENTO] {e}", file=sys.stderr)
        return 1
    if saida.strip():
        print(saida)
    return 0


def cmd_sessao(args) -> int:
    """Mostra o saldo do dia e abre o copilot interativo (casca fina).

    O consumo de uma sessao interativa nao da para medir daqui — ao sair,
    lembre de rodar 'orcamento sincronizar' (ou 'orcamento gasto N').
    """
    cfg_orc = carregar_config()
    imprimir(calcular(cfg_orc), cfg_orc)
    cfg = carregar_harness()
    binario = cfg["papeis"]["executor"]["cmd"][0]
    cmd = [binario]
    for d in cfg.get("add_dirs", []):
        cmd += ["--add-dir", d]
    extras = list(args.args or [])
    if extras and extras[0] == "--":
        extras = extras[1:]
    cmd += extras
    print(f"{CINZA}$ {shlex.join(cmd)}{FIM}\n")
    try:
        codigo = subprocess.call(cmd)  # herda stdin/stdout: sessao interativa
    except FileNotFoundError:
        print(f"erro: '{binario}' nao encontrado no PATH. Instale o Copilot CLI "
              "ou ajuste o cmd em copiloto.json.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        codigo = 130
    print(f"\n{CINZA}Sessao encerrada. O gasto interativo nao e medido daqui:{FIM}")
    print(f"{CINZA}  python3 copiloto.py orcamento sincronizar   (via gh api){FIM}")
    print(f"{CINZA}  python3 copiloto.py orcamento gasto N       (na mao, veja /usage){FIM}")
    return codigo


def _parece_copiloto(texto: str) -> bool:
    """O arquivo baixado precisa parecer este programa (e uma versao que ja
    tenha auto-update, para nao regredir)."""
    return "copiloto.py" in texto[:2000] and "def cmd_atualizar(" in texto


def _baixar_atualizacao() -> str:
    """Baixa o copiloto.py mais novo do GitHub.

    Tenta HTTPS direto (urllib respeita HTTPS_PROXY do ambiente) com
    cache-buster — a CDN do raw.githubusercontent segura versao antiga por
    alguns minutos — e cai para o gh CLI (API, sempre fresca) se o direto
    falhar ou vier conteudo que nao parece este programa.
    """
    erro_http: Exception | None = None
    try:
        import urllib.request
        url = (f"https://raw.githubusercontent.com/{REPO_ATUALIZACAO}/main/"
               f"copiloto.py?nocache={int(time.time())}")
        with urllib.request.urlopen(url, timeout=30) as resposta:
            texto = resposta.read().decode("utf-8")
        if _parece_copiloto(texto):
            return texto
        erro_http = RuntimeError("conteudo baixado nao parece o copiloto.py "
                                 "(cache da CDN? tentando via gh)")
    except Exception as e:  # inclui SSL de inspecao corporativa
        erro_http = e
    try:
        texto = _gh(["api", f"repos/{REPO_ATUALIZACAO}/contents/copiloto.py",
                     "-H", "Accept: application/vnd.github.raw+json"])
        if _parece_copiloto(texto):
            return texto
        raise RuntimeError("a versao no GitHub nao tem o comando 'atualizar' — "
                           "confira o repo/branch")
    except RuntimeError as erro_gh:
        raise RuntimeError(
            f"download direto falhou ({type(erro_http).__name__}: {erro_http}) e o gh "
            f"tambem ({erro_gh}). Atras de proxy/inspecao SSL, configure HTTPS_PROXY e "
            f"SSL_CERT_FILE com a CA da empresa, use o gh autenticado, ou espere uns "
            f"minutos (cache da CDN apos um push recente).")


def cmd_atualizar(args) -> int:
    """Substitui este arquivo pela versao mais nova do GitHub, com backup."""
    destino = Path(__file__).resolve()
    atual = destino.read_text(encoding="utf-8")
    print(f"versao instalada: {VERSAO}")
    print(f"baixando de {REPO_ATUALIZACAO} (main)...")
    try:
        novo = _baixar_atualizacao()
    except RuntimeError as e:
        print(f"erro: {e}", file=sys.stderr)
        return 1

    # Sanidade antes de se sobrescrever: precisa compilar.
    try:
        compile(novo, "copiloto.py", "exec")
    except SyntaxError as e:
        print(f"erro: a versao baixada nao compila ({e}) — abortando.", file=sys.stderr)
        return 1

    m = re.search(r'^VERSAO = "([^"]+)"', novo, re.M)
    versao_nova = m.group(1) if m else "(sem VERSAO)"
    if novo == atual:
        print(f"ja esta na versao mais recente ({VERSAO}).")
        return 0

    backup = destino.with_suffix(".py.bak")
    backup.write_text(atual, encoding="utf-8")
    tmp = destino.with_suffix(".py.novo")
    tmp.write_text(novo, encoding="utf-8")
    os.replace(tmp, destino)
    print(f"atualizado: {VERSAO} -> {versao_nova}")
    print(f"backup da versao anterior em {backup.name}")
    return 0


# =========================================================================== #
# CLI
# =========================================================================== #

def data_arg(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    _migrar_estado_antigo()
    p = argparse.ArgumentParser(
        prog="copiloto.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--versao", action="version", version=f"copiloto {VERSAO}")
    sub = p.add_subparsers(dest="cmd")

    def add_data(sp):
        sp.add_argument("--data", type=data_arg, help="simula outra data (YYYY-MM-DD)")

    # ---- harness (fluxo principal, no topo) ----
    sp = sub.add_parser("init", help="cria copiloto.json do projeto e detecta verificacoes")
    sp.add_argument("--repo", action="append",
                    help="repositorio do projeto; repita para fonte + config (padrao: .)")
    sp.add_argument("--sobrescrever", action="store_true")
    sp.set_defaults(func=cmd_harness_init)

    sp = sub.add_parser("planejar", help="modelo forte escreve plano.md")
    sp.add_argument("tarefa")
    sp.add_argument("--forcar", action="store_true", help="ignora o portao de orcamento")
    sp.set_defaults(func=cmd_planejar)

    sp = sub.add_parser("rodar", help="executa (barato) e valida (caro) os passos")
    sp.add_argument("--passo", type=int)
    sp.add_argument("--forcar", action="store_true")
    sp.add_argument("--refazer", action="store_true",
                    help="ignora o progresso salvo dos passos selecionados")
    sp.set_defaults(func=cmd_rodar)

    sp = sub.add_parser("custo", help="status do orcamento (atalho de 'orcamento status')")
    sp.set_defaults(func=cmd_custo)

    # ---- casca ----
    sp = sub.add_parser("pedir", help="prompt avulso pelo portao de orcamento (modelo barato)")
    sp.add_argument("prompt")
    sp.add_argument("--forte", action="store_true", help="usa o modelo do planejador")
    sp.add_argument("--modelo", help="sobrescreve o modelo desta chamada")
    sp.add_argument("--custo", type=float, help="sobrescreve a estimativa do portao")
    sp.add_argument("--forcar", action="store_true")
    sp.set_defaults(func=cmd_pedir)

    sp = sub.add_parser("sessao", help="mostra o saldo do dia e abre o copilot interativo")
    sp.add_argument("args", nargs=argparse.REMAINDER,
                    help="argumentos extras repassados ao copilot (apos --)")
    sp.set_defaults(func=cmd_sessao)

    sp = sub.add_parser("atualizar", help=f"baixa a versao mais nova do GitHub ({REPO_ATUALIZACAO})")
    sp.set_defaults(func=cmd_atualizar)

    # ---- mapa ----
    sp = sub.add_parser("mapa", help="gera MAPA.md local (aceita varios repos)")
    sp.add_argument("raizes", nargs="*", help="raizes dos repos (padrao: .)")
    sp.add_argument("-o", "--saida", default="MAPA.md")
    sp.add_argument("--max-simbolos", type=int, default=12)
    sp.add_argument("--max-arquivos-detalhe", type=int, default=200)
    sp.add_argument("--stdout", action="store_true")
    sp.set_defaults(func=cmd_mapa)

    # ---- instalar ----
    sp = sub.add_parser("instalar", help="grava custom agents (.agent.md) e AGENTS.md num repo")
    sp.add_argument("repo", nargs="?", default=".")
    sp.add_argument("--sobrescrever", action="store_true")
    sp.add_argument("--mcp", action="store_true", help="mostra o exemplo de MCP de memoria")
    sp.set_defaults(func=cmd_instalar)

    # ---- orcamento ----
    orc = sub.add_parser("orcamento", help="cota mensal por dias uteis")
    osub = orc.add_subparsers(dest="orc_cmd")

    sp = osub.add_parser("init", help="cria/atualiza a configuracao")
    sp.add_argument("--cota", type=float)
    sp.add_argument("--unidade")
    sp.add_argument("--dia-reset", type=int, dest="dia_reset")
    sp.add_argument("--reserva", type=float)
    sp.set_defaults(func=cmd_orc_init)

    sp = osub.add_parser("status", help="quanto posso gastar hoje")
    sp.add_argument("--json", action="store_true")
    add_data(sp)
    sp.set_defaults(func=cmd_status)

    sp = osub.add_parser("gasto", help="registra consumo")
    sp.add_argument("qtd", type=float)
    sp.add_argument("--modelo")
    sp.add_argument("--nota")
    sp.add_argument("--quieto", action="store_true")
    add_data(sp)
    sp.set_defaults(func=cmd_gasto)

    sp = osub.add_parser("pode", help="portao para scripts: exit 0 se cabe no dia")
    sp.add_argument("custo", type=float)
    sp.add_argument("--emergencia", action="store_true")
    sp.add_argument("--json", action="store_true")
    add_data(sp)
    sp.set_defaults(func=cmd_pode)

    sp = osub.add_parser("plano", help="distribuicao dia a dia ate o fim do ciclo")
    add_data(sp)
    sp.set_defaults(func=cmd_orc_plano)

    sp = osub.add_parser("resumo", help="uso por dia e por modelo no ciclo")
    add_data(sp)
    sp.set_defaults(func=cmd_resumo)

    sp = osub.add_parser("sincronizar", help="puxa o consumo real do mes via gh api")
    sp.add_argument("--login")
    add_data(sp)
    sp.set_defaults(func=cmd_sincronizar)

    args = p.parse_args(argv)
    if not getattr(args, "cmd", None):
        p.print_help()
        return 0
    if args.cmd == "orcamento" and not getattr(args, "orc_cmd", None):
        orc.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
