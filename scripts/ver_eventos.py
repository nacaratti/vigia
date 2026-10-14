"""Mostra os últimos eventos registrados no SQLite.

    python scripts/ver_eventos.py              # últimos 20
    python scripts/ver_eventos.py -n 50 --camera cam1
    python scripts/ver_eventos.py --data 2026-10-09
    python scripts/ver_eventos.py --teste      # banco do modo teste
"""
import argparse
import sqlite3
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=20)
    ap.add_argument("--camera")
    ap.add_argument("--data", help="YYYY-MM-DD")
    ap.add_argument("--teste", action="store_true")
    a = ap.parse_args()

    banco = RAIZ / "data" / ("eventos_teste.db" if a.teste else "eventos.db")
    if not banco.exists():
        sys.exit(f"Nenhum evento ainda ({banco.relative_to(RAIZ)} não existe).")
    filtros, args = [], []
    if a.camera:
        filtros.append("camera_id = ?")
        args.append(a.camera)
    if a.data:
        filtros.append("ts LIKE ?")
        args.append(f"{a.data}%")
    where = f"WHERE {' AND '.join(filtros)}" if filtros else ""
    with sqlite3.connect(banco) as db:
        linhas = db.execute(
            f"SELECT id, ts, camera_nome, pessoas, confianca, alerta, imagem FROM eventos {where} "
            "ORDER BY id DESC LIMIT ?", args + [a.n]).fetchall()
    if not linhas:
        print("Nenhum evento com esses filtros.")
        return
    print(f"{'#':>5}  {'Data/hora':19}  {'Câmera':18} {'Pess.':>5} {'Conf.':>5}  {'Alerta':10} Imagem")
    for i, ts, cam, p, conf, alerta, img in linhas:
        print(f"{i:>5}  {ts.replace('T', ' '):19}  {cam[:18]:18} {p:>5} {conf:>5.0%}  {alerta:10} {img}")


if __name__ == "__main__":
    main()
