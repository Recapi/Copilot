"""Dias uteis e feriados nacionais brasileiros, em Python puro.

Sem dependencias externas de proposito: a maquina corporativa nao tem admin,
entao nada de `pip install holidays`. Tudo aqui usa apenas a stdlib.
"""

from __future__ import annotations

import datetime as dt
from typing import Iterable


def pascoa(ano: int) -> dt.date:
    """Domingo de Pascoa pelo algoritmo de Meeus/Jones/Butcher (calendario gregoriano)."""
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
    """Feriados nacionais do ano.

    Carnaval e Corpus Christi sao ponto facultativo, nao feriado legal, mas na
    pratica quase ninguem trabalha: por padrao entram na conta.
    """
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

    def __init__(
        self,
        incluir_facultativos: bool = True,
        extras: Iterable[str] = (),
        remover: Iterable[str] = (),
    ) -> None:
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
        if d.weekday() >= 5:  # sabado=5, domingo=6
            return False
        return d not in self.feriados_do_ano(d.year)

    def motivo_nao_util(self, d: dt.date) -> str | None:
        if d.weekday() == 5:
            return "sabado"
        if d.weekday() == 6:
            return "domingo"
        return self.feriados_do_ano(d.year).get(d)

    def dias_uteis(self, inicio: dt.date, fim: dt.date) -> list[dt.date]:
        """Dias uteis no intervalo fechado [inicio, fim]."""
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


if __name__ == "__main__":
    import sys

    ano = int(sys.argv[1]) if len(sys.argv) > 1 else dt.date.today().year
    cal = Calendario()
    print(f"Feriados nacionais de {ano} (com pontos facultativos):")
    for d, nome in sorted(cal.feriados_do_ano(ano).items()):
        semana = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"][d.weekday()]
        print(f"  {d.isoformat()} ({semana})  {nome}")
    total = cal.contar(dt.date(ano, 1, 1), dt.date(ano, 12, 31))
    print(f"\nDias uteis em {ano}: {total}")
