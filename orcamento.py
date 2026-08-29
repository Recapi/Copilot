#!/usr/bin/env python3
"""Controle de cota mensal de IA distribuida por dias uteis.

Ideia central: em vez de fixar uma cota diaria no comeco do mes, a cota do dia e
RECALCULADA toda vez que voce roda o programa:

    permitido_hoje = (restante - reserva) / dias_uteis_restantes

Isso e auto-corretivo. Se voce economizou ontem, hoje voce pode mais. Se estourou
ontem, hoje aperta sozinho. Nao precisa de logica de "sobra acumulada".

Sem dependencias externas (a maquina corporativa nao tem admin).

Uso rapido:
    python3 orcamento.py init --cota 300
    python3 orcamento.py status
    python3 orcamento.py gasto 3 --modelo claude-sonnet --nota "refactor do parser"
    python3 orcamento.py pode 5 && echo "pode gastar"
    python3 orcamento.py plano
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

from calendario import Calendario

BASE = Path(os.environ.get("ORCAMENTO_DIR", Path(__file__).parent)).resolve()
CONFIG_PATH = BASE / "config.json"
USO_PATH = BASE / "uso.jsonl"

PADRAO = {
    "cota_ciclo": 10000.0,
    "unidade": "creditos de IA",
    "dia_reset": 1,
    "reserva_pct": 0.15,
    "incluir_facultativos": True,
    "feriados_extras": [],
    "feriados_removidos": [],
    "multiplicadores": {},
}


# --------------------------------------------------------------------------- #
# Config e registro de uso
# --------------------------------------------------------------------------- #

def carregar_config() -> dict:
    cfg = dict(PADRAO)
    if CONFIG_PATH.exists():
        cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
    return cfg


def salvar_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def registrar(qtd: float, modelo: str | None, nota: str | None, data: dt.date) -> None:
    linha = {"data": data.isoformat(), "qtd": round(qtd, 4)}
    if modelo:
        linha["modelo"] = modelo
    if nota:
        linha["nota"] = nota
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


# --------------------------------------------------------------------------- #
# Ciclo de faturamento
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# Calculo
# --------------------------------------------------------------------------- #

def reserva(total: float, uteis_restantes: int, uteis_totais: int) -> float:
    """Quanto da reserva ainda fica retido, dado quantos dias uteis faltam.

    Decai linearmente e chega a zero no ultimo dia util, para a cota nao morrer
    com sobra presa que o ciclo vai zerar de qualquer jeito.
    """
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
    cal = Calendario(
        incluir_facultativos=cfg["incluir_facultativos"],
        extras=cfg["feriados_extras"],
        remover=cfg["feriados_removidos"],
    )
    inicio, fim = ciclo(hoje, int(cfg["dia_reset"]))

    uso = [u for u in ler_uso() if inicio.isoformat() <= u["data"] <= fim.isoformat()]
    gasto_ciclo = sum(float(u["qtd"]) for u in uso)
    gasto_hoje = sum(float(u["qtd"]) for u in uso if u["data"] == hoje.isoformat())

    cota = float(cfg["cota_ciclo"])
    restante = max(0.0, cota - gasto_ciclo)

    uteis_totais = cal.contar(inicio, fim)
    uteis_restantes = cal.contar(hoje, fim)  # inclui hoje, se hoje for util
    uteis_decorridos = uteis_totais - uteis_restantes

    # A reserva de seguranca e liberada linearmente ate o fim do ciclo: no comeco
    # segura quase tudo, no ultimo dia util nao segura nada.
    reserva_total = cota * float(cfg["reserva_pct"])
    reserva_efetiva = min(restante, reserva(reserva_total, uteis_restantes, uteis_totais))

    if uteis_restantes > 0:
        permitido_hoje = max(0.0, (restante - reserva_efetiva) / uteis_restantes)
    else:
        permitido_hoje = restante  # fora de dia util / fim do ciclo: sobrou o que sobrou

    if not cal.eh_dia_util(hoje):
        # Fim de semana ou feriado: nao ha cota do dia. Se precisar mesmo, gaste
        # da reserva conscientemente.
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
        hoje=hoje.isoformat(),
        hoje_eh_util=cal.eh_dia_util(hoje),
        motivo_nao_util=cal.motivo_nao_util(hoje),
        ciclo_inicio=inicio.isoformat(),
        ciclo_fim=fim.isoformat(),
        cota=round(cota, 2),
        gasto_ciclo=round(gasto_ciclo, 2),
        gasto_hoje=round(gasto_hoje, 2),
        restante=round(restante, 2),
        reserva_efetiva=round(reserva_efetiva, 2),
        dias_uteis_totais=uteis_totais,
        dias_uteis_decorridos=uteis_decorridos,
        dias_uteis_restantes=uteis_restantes,
        permitido_hoje=round(permitido_hoje, 2),
        saldo_hoje=round(saldo_hoje, 2),
        teto_emergencia=round(min(permitido_hoje * 2, restante), 2),
        ritmo_alvo=round(ritmo_alvo, 2),
        ritmo_real=round(ritmo_real, 2),
        projecao_fim_ciclo=round(projecao, 2),
        data_estouro=data_estouro,
        situacao=situacao,
    )


# --------------------------------------------------------------------------- #
# Apresentacao
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def cmd_init(args) -> int:
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
    print(f"config salva em {CONFIG_PATH}")
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
    """Portao para scripts: sai com 0 se cabe no orcamento do dia, 1 se nao cabe."""
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


def cmd_plano(args) -> int:
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


def data_arg(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")

    def add_data(sp):
        sp.add_argument("--data", type=data_arg, help="simula outra data (YYYY-MM-DD)")

    sp = sub.add_parser("init", help="cria/atualiza a configuracao")
    sp.add_argument("--cota", type=float, help="cota total do ciclo")
    sp.add_argument("--unidade", help='ex.: "requisicoes premium"')
    sp.add_argument("--dia-reset", type=int, dest="dia_reset", help="dia do mes em que a cota reseta")
    sp.add_argument("--reserva", type=float, help="fracao de reserva, ex.: 0.15")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("status", help="quanto posso gastar hoje")
    sp.add_argument("--json", action="store_true")
    add_data(sp)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("gasto", help="registra consumo")
    sp.add_argument("qtd", type=float)
    sp.add_argument("--modelo", help="aplica o multiplicador do modelo, se configurado")
    sp.add_argument("--nota")
    sp.add_argument("--quieto", action="store_true")
    add_data(sp)
    sp.set_defaults(func=cmd_gasto)

    sp = sub.add_parser("pode", help="portao para scripts: exit 0 se cabe no dia")
    sp.add_argument("custo", type=float)
    sp.add_argument("--emergencia", action="store_true", help="permite invadir a reserva")
    sp.add_argument("--json", action="store_true")
    add_data(sp)
    sp.set_defaults(func=cmd_pode)

    sp = sub.add_parser("plano", help="distribuicao dia a dia ate o fim do ciclo")
    add_data(sp)
    sp.set_defaults(func=cmd_plano)

    sp = sub.add_parser("resumo", help="uso por dia e por modelo no ciclo")
    add_data(sp)
    sp.set_defaults(func=cmd_resumo)

    args = p.parse_args(argv)
    if not getattr(args, "cmd", None):
        args = p.parse_args(["status"])
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
