"""Janelas de horário por dia da semana, com suporte a cruzar meia-noite."""
from datetime import datetime

from .config import DIAS


def _minutos(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def em_janela(janelas: list[dict], agora: datetime | None = None) -> bool:
    """True se `agora` cai em alguma janela.

    - inicio < fim: mesmo dia (ex.: 08:00–18:00).
    - inicio > fim: cruza a meia-noite; a janela pertence ao dia em que começa
      (sex 22:00–06:00 vale de sex 22:00 até sáb 06:00).
    - inicio == fim: o dia inteiro.
    """
    agora = agora or datetime.now()
    minuto = agora.hour * 60 + agora.minute
    hoje = DIAS[agora.weekday()]
    ontem = DIAS[(agora.weekday() - 1) % 7]
    for j in janelas:
        ini, fim, dias = _minutos(j["inicio"]), _minutos(j["fim"]), j["dias"]
        if ini == fim:
            if hoje in dias:
                return True
        elif ini < fim:
            if hoje in dias and ini <= minuto < fim:
                return True
        elif (hoje in dias and minuto >= ini) or (ontem in dias and minuto < fim):
            return True
    return False
