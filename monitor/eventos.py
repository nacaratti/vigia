"""Registro de eventos em SQLite."""
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

ESQUEMA = """
CREATE TABLE IF NOT EXISTS eventos (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,          -- ISO 8601, horário local
    camera_id   TEXT NOT NULL,
    camera_nome TEXT NOT NULL,
    pessoas     INTEGER NOT NULL,
    confianca   REAL NOT NULL,          -- maior confiança entre as pessoas
    imagem      TEXT NOT NULL,          -- caminho relativo à raiz do projeto
    alerta      TEXT NOT NULL DEFAULT 'pendente'  -- pendente|enviado|falhou|desativado
);
CREATE INDEX IF NOT EXISTS ix_eventos_ts ON eventos(ts);
CREATE INDEX IF NOT EXISTS ix_eventos_cam ON eventos(camera_id, ts);
"""


class Eventos:
    def __init__(self, caminho: Path):
        caminho.parent.mkdir(parents=True, exist_ok=True)
        self._raiz = caminho.resolve().parent.parent  # data/eventos.db -> raiz do projeto
        self._db = sqlite3.connect(caminho, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(ESQUEMA)
            colunas = {l[1] for l in self._db.execute("PRAGMA table_info(eventos)")}
            if "video" not in colunas:  # bancos criados antes da gravação de vídeo
                self._db.execute("ALTER TABLE eventos ADD COLUMN video TEXT")
                self._db.commit()

    def registrar(self, camera_id, camera_nome, pessoas, confianca, imagem, alerta="pendente") -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO eventos (ts, camera_id, camera_nome, pessoas, confianca, imagem, alerta)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (datetime.now().isoformat(timespec="seconds"), camera_id, camera_nome,
                 pessoas, round(confianca, 3), imagem, alerta),
            )
            return cur.lastrowid

    def atualizar_alerta(self, evento_id: int, status: str):
        with self._lock, self._db:
            self._db.execute("UPDATE eventos SET alerta = ? WHERE id = ?", (status, evento_id))

    def definir_video(self, evento_id: int, caminho: Path) -> bool:
        """False se o evento foi apagado enquanto o vídeo era gravado."""
        rel = caminho.resolve().relative_to(self._raiz).as_posix()
        with self._lock, self._db:
            return self._db.execute("UPDATE eventos SET video = ? WHERE id = ?", (rel, evento_id)).rowcount > 0

    def _apagar_arquivo(self, rel: str | None) -> int:
        """Apaga um arquivo de snapshots/ e devolve quantos bytes liberou."""
        if not rel:
            return 0
        caminho = (self._raiz / rel).resolve()
        if not caminho.is_relative_to(self._raiz / "snapshots") or not caminho.is_file():
            return 0
        tamanho = caminho.stat().st_size
        caminho.unlink()
        return tamanho

    def apagar(self, ids: list[int], so_video: bool) -> dict:
        """Apaga os vídeos (mantendo foto e registro) ou os eventos inteiros."""
        ids = [int(i) for i in ids][:500]
        if not ids:
            return {"apagados": 0, "bytes": 0}
        marcas = ",".join("?" * len(ids))
        with self._lock:
            linhas = self._db.execute(f"SELECT id, imagem, video FROM eventos WHERE id IN ({marcas})", ids).fetchall()
            with self._db:
                if so_video:
                    self._db.execute(f"UPDATE eventos SET video = NULL WHERE id IN ({marcas})", ids)
                else:
                    self._db.execute(f"DELETE FROM eventos WHERE id IN ({marcas})", ids)
        liberados = 0
        for l in linhas:
            liberados += self._apagar_arquivo(l["video"])
            if not so_video:
                liberados += self._apagar_arquivo(l["imagem"])
        n = sum(1 for l in linhas if l["video"]) if so_video else len(linhas)
        return {"apagados": n, "bytes": liberados}

    def armazenamento(self) -> dict:
        with self._lock:
            linhas = self._db.execute("SELECT imagem, video FROM eventos").fetchall()
        total = {"videos": 0, "bytes_videos": 0, "bytes_fotos": 0}
        for l in linhas:
            for chave, rel in (("bytes_fotos", l["imagem"]), ("bytes_videos", l["video"])):
                if rel and (self._raiz / rel).is_file():
                    total[chave] += (self._raiz / rel).stat().st_size
                    total["videos"] += chave == "bytes_videos"
        return total

    def listar(self, camera=None, data=None, limite=50, offset=0) -> tuple[list[dict], int]:
        filtros, args = [], []
        if camera:
            filtros.append("camera_id = ?")
            args.append(camera)
        if data:  # YYYY-MM-DD
            filtros.append("ts >= ? AND ts < ?")
            args += [f"{data}T00:00:00", f"{data}T99"]
        where = f"WHERE {' AND '.join(filtros)}" if filtros else ""
        with self._lock:
            total = self._db.execute(f"SELECT COUNT(*) FROM eventos {where}", args).fetchone()[0]
            linhas = self._db.execute(
                f"SELECT * FROM eventos {where} ORDER BY id DESC LIMIT ? OFFSET ?", args + [limite, offset]
            ).fetchall()
        return [dict(l) for l in linhas], total

    def obter(self, evento_id: int) -> dict | None:
        with self._lock:
            l = self._db.execute("SELECT * FROM eventos WHERE id = ?", (evento_id,)).fetchone()
        return dict(l) if l else None

    def resumo_hoje(self) -> dict:
        hoje = datetime.now().date().isoformat()
        with self._lock:
            linhas = self._db.execute(
                "SELECT camera_id, COUNT(*) n, MAX(ts) ultimo FROM eventos WHERE ts >= ? GROUP BY camera_id",
                (f"{hoje}T00:00:00",),
            ).fetchall()
        return {l["camera_id"]: {"hoje": l["n"], "ultimo": l["ultimo"]} for l in linhas}
