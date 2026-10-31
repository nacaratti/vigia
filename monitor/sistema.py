"""Orquestra fontes de vídeo, detecção, eventos e alertas para todas as câmeras."""
import logging
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from . import captura, config as cfgmod
from .captura import Fonte, verificar_credenciais
from .deteccao import Detector, desenhar, separar_por_zona
from .eventos import Eventos
from .gravacao import Gravador
from .horarios import em_janela
from .notificador import Telegram
from .sirene import Sirene

log = logging.getLogger("sistema")
FRAME_VELHO_S = 5  # frame mais antigo que isso não é analisado
MOV_SEGURA_S = 6        # continua analisando por este tempo após o último movimento
MOV_SEGURANCA_S = 15    # mesmo sem movimento, analisa pelo menos a cada 15 s
MOV_FAIXA = 0.08        # ignora 8% de cima e de baixo (relógio e nome do canal do DVR)


class DetectorMovimento:
    """Diferença contra um fundo médio, em imagem pequena e borrada: custa quase nada."""

    def __init__(self):
        self.fundo = None

    def medir(self, frame) -> float:
        """% da imagem que mudou em relação ao fundo."""
        h, w = frame.shape[:2]
        peq = cv2.resize(frame, (320, max(1, round(320 * h / w))), interpolation=cv2.INTER_AREA)
        cinza = cv2.GaussianBlur(cv2.cvtColor(peq, cv2.COLOR_BGR2GRAY), (7, 7), 0)
        corte = round(cinza.shape[0] * MOV_FAIXA)
        cinza = cinza[corte:cinza.shape[0] - corte]
        if self.fundo is None or self.fundo.shape != cinza.shape:
            self.fundo = cinza.astype(np.float32)
            return 0.0
        diff = cv2.absdiff(cinza, cv2.convertScaleAbs(self.fundo))
        cv2.accumulateWeighted(cinza, self.fundo, 0.25)
        return float(np.count_nonzero(diff > 25)) * 100 / diff.size


class MonitorCamera(threading.Thread):
    """Analisa ~fps_analise frames/s da câmera quando ela está dentro da janela."""

    def __init__(self, sistema: "Sistema", cam: dict, fonte: Fonte):
        super().__init__(name=f"monitor-{cam['id']}", daemon=True)
        self.sistema = sistema
        self.cam = cam
        self.fonte = fonte
        self.log = logging.getLogger(f"monitor.{cam['id']}")
        self._parar = threading.Event()
        self.historico: deque = deque(maxlen=5)
        self.em_janela = False
        self.ultimo_alerta = 0.0     # último envio ao Telegram (cooldown)
        self.em_evento = False       # já existe evento aberto para a presença atual
        self._ultima_presenca = 0.0  # última análise que viu pessoa na zona
        self.ultimas = ([], [], 0.0, [])  # (dentro, fora, ts, pets) da última inferência
        self.descartadas_pet = 0  # "pessoas" descartadas por serem um cachorro/gato
        self.inferencias = 0
        self.ms_inferencia = 0.0
        self.movimento = DetectorMovimento()
        self.movimento_pct = 0.0
        self.puladas = 0  # análises evitadas por falta de movimento
        self._ultimo_mov = 0.0
        self._ultima_inf = 0.0
        self.gravador = Gravador(self)
        self._presenca_inicio = 0.0   # 1ª análise com pessoa na zona nesta presença
        self._sirene_disparada = False

    def parar(self):
        self._parar.set()

    @property
    def det(self) -> dict:
        return cfgmod.deteccao_efetiva(self.sistema.cfg, self.cam)

    def run(self):
        ultimo_seq = -1
        while not self._parar.is_set():
            det = self.det
            if self.historico.maxlen != det["n"]:
                self.historico = deque(self.historico, maxlen=det["n"])
            self._parar.wait(1 / det["fps_analise"])

            # Armado = vigia agora. O horário só arma/desarma o sistema sozinho (ver _agendador).
            self.em_janela = self.sistema.armado and self.cam["ativo"]
            if not self.em_janela:  # desarmado: nem roda inferência
                self.historico.clear()
                self.em_evento = False
                self._presenca_inicio, self._sirene_disparada = 0.0, False
                continue
            if self.em_evento and time.time() - self._ultima_presenca >= det["ausencia_s"]:
                self.em_evento = False  # a pessoa sumiu: a próxima aparição é um evento novo
                self._presenca_inicio, self._sirene_disparada = 0.0, False

            frame, seq, ts = self.fonte.ultimo()
            if frame is None or seq == ultimo_seq or time.time() - ts > FRAME_VELHO_S:
                continue
            ultimo_seq = seq
            detector = self.sistema.detector
            if detector is None:
                continue

            agora = time.time()
            self.movimento_pct = self.movimento.medir(frame)
            if self.movimento_pct >= det["movimento_min"]:
                self._ultimo_mov = agora
            precisa_analisar = (
                not det["filtro_movimento"]
                or agora - self._ultimo_mov < MOV_SEGURA_S
                or agora - self._ultima_inf >= MOV_SEGURANCA_S
                or any(self.historico)  # pessoa vista há pouco: continua olhando, mesmo parada
            )
            if not precisa_analisar:
                self.historico.append(False)
                self.puladas += 1
                continue
            self._ultima_inf = agora

            t0 = time.perf_counter()
            try:
                todas, pets, descartadas = detector.detectar(
                    frame, det["confianca"], det["imgsz"], det["limite_animal"] if det["filtro_animal"] else None)
            except Exception:
                self.log.exception("Erro na inferência")
                continue
            ms = (time.perf_counter() - t0) * 1000
            self.ms_inferencia = 0.8 * self.ms_inferencia + 0.2 * ms if self.inferencias else ms
            self.inferencias += 1

            h, w = frame.shape[:2]
            dentro, fora = separar_por_zona(todas, self.cam["zonas"], w, h)
            self.ultimas = (dentro, fora, time.time(), pets)
            if descartadas:
                self.descartadas_pet += descartadas
                self.log.info("Descartada 'pessoa' que era %s", ", ".join(p[5] for p in pets))
            self.historico.append(bool(dentro))
            if dentro:
                self._ultima_presenca = time.time()
                if not self._presenca_inicio:
                    self._presenca_inicio = self._ultima_presenca

            # Um evento por presença: só abre outro depois de `ausencia_s` sem ninguém.
            if not self.em_evento and sum(self.historico) >= det["k"]:
                self._disparar(frame, dentro, fora, pets, det)

            # Sirene: pessoa continua na zona por `atraso_s` segundos dentro do evento.
            sir = self.sistema.cfg["sirene"]
            if (sir["ativo"] and self.em_evento and dentro and not self._sirene_disparada
                    and time.time() - self._presenca_inicio >= sir["atraso_s"]):
                self._sirene_disparada = True
                self.sistema.sirene.disparar(sir["duracao_s"], f"{len(dentro)} pessoa(s) em {self.cam['nome']}")

    def _disparar(self, frame, dentro, fora, pets, det):
        self.em_evento = True
        agora = datetime.now()
        pasta = self.sistema.pasta_snapshots / agora.strftime("%Y-%m-%d")
        pasta.mkdir(parents=True, exist_ok=True)
        arquivo = pasta / f"{self.cam['id']}_{agora.strftime('%H%M%S')}.jpg"
        cv2.imwrite(str(arquivo), desenhar(frame, dentro, fora, self.cam["zonas"], pets))

        conf = max(d[4] for d in dentro)
        rel = arquivo.relative_to(cfgmod.RAIZ).as_posix()
        ev = self.sistema.eventos
        eid = ev.registrar(self.cam["id"], self.cam["nome"], len(dentro), conf, rel)
        if self.sistema.cfg["gravacao"]["ativo"]:
            self.gravador.iniciar(eid, arquivo.with_suffix(".mp4"))
        legenda = (
            f"🚨 {len(dentro)} pessoa(s) em {self.cam['nome']}\n"
            f"{agora.strftime('%d/%m/%Y %H:%M:%S')} · confiança {conf:.0%}"
        )
        if not self.sistema.cfg["alertas"]["telegram"]:
            status = "desativado"
        elif time.time() - self.ultimo_alerta < det["cooldown_s"]:
            status = "intervalo"  # evento registrado, mas sem nova mensagem tão cedo
        else:
            self.ultimo_alerta = time.time()
            status = self.sistema.telegram.enviar_foto(eid, legenda, arquivo)
        if status != "pendente":
            ev.atualizar_alerta(eid, status)
        self.log.info("ALERTA: %s pessoa(s), confiança %.2f, evento #%s (telegram: %s)", len(dentro), conf, eid, status)


class Sistema:
    def __init__(self, video_teste: str | None = None, sem_alerta: bool = False):
        self.modo_teste = bool(video_teste)
        self.video_teste = video_teste
        self.ignorar_horario = self.modo_teste
        sufixo = "teste" if self.modo_teste else ""
        self.pasta_snapshots = cfgmod.RAIZ / "snapshots" / (sufixo or "eventos")
        self.eventos = Eventos(cfgmod.RAIZ / "data" / (f"eventos_{sufixo}.db" if sufixo else "eventos.db"))
        self.telegram = Telegram(ao_concluir=self.eventos.atualizar_alerta, desativado=sem_alerta)
        self.sirene = Sirene(ao_mudar=self._sirene_mudou)
        self.armado = self.modo_teste
        self.armado_origem = "modo teste" if self.modo_teste else ""
        self.armado_desde = time.time()
        self._parar_agendador = threading.Event()
        self.inicio = time.time()
        self.detector: Detector | None = None
        self.erro_detector = ""
        self.erro_credenciais = ""
        self.fontes: dict[str, Fonte] = {}
        self.monitores: dict[str, MonitorCamera] = {}
        self._lock = threading.RLock()
        self.cfg = self._config_efetiva(cfgmod.carregar())
        self.sirene.configurar(self.cfg["sirene"]["som"], self.cfg["sirene"]["voz"])

    def _config_efetiva(self, cfg: dict) -> dict:
        if not self.modo_teste:
            return cfg
        cfg = cfgmod.copia(cfg)
        cfg["cameras"] = [{
            "id": "teste", "nome": f"Vídeo de teste ({Path(self.video_teste).name})",
            "fonte": str(Path(self.video_teste).resolve()), "ativo": True,
            "janelas": [{"dias": cfgmod.DIAS, "inicio": "00:00", "fim": "00:00"}], "zonas": [],
        }]
        return cfg

    # --- ciclo de vida ------------------------------------------------------

    def iniciar(self):
        self._carregar_detector(self.cfg["deteccao"]["modelo"])
        rtsp = [c for c in self.cfg["cameras"] if not c.get("fonte")]
        if rtsp:
            self.erro_credenciais = verificar_credenciais(
                cfgmod.url_camera(self.cfg, rtsp[0]), cfgmod.credenciais()["password"])
            if self.erro_credenciais:
                log.error("Câmeras não serão conectadas: %s", self.erro_credenciais)
        with self._lock:
            for cam in self.cfg["cameras"]:
                self._iniciar_camera(cam)
        log.info("Sistema iniciado com %s câmera(s)%s", len(self.cfg["cameras"]),
                 " [MODO TESTE]" if self.modo_teste else "")
        if not self.modo_teste:
            threading.Thread(target=self._agendador, name="agendador", daemon=True).start()
        self._ligar_telegram()

    def _ligar_telegram(self):
        """Escuta os comandos do bot (não faz nada se o Telegram não estiver configurado)."""
        if not self.telegram.ativo:
            return
        comandos = {"armar": self._tg_armar, "desarmar": self._tg_desarmar, "status": self._tg_status,
                    "parar": self._tg_parar, "parar_sirene": self._tg_parar, "ver": self._tg_ver,
                    "menu": self._tg_menu, "start": self._tg_menu, "ajuda": self._tg_menu, "help": self._tg_menu}
        self.telegram.escutar(comandos)
        self.telegram.registrar_comandos([("armar", "Armar o alarme agora"), ("desarmar", "Desarmar o alarme"),
                                          ("status", "Ver o estado do sistema"), ("ver", "Foto atual das câmeras (ex.: /ver 2)"),
                                          ("parar", "Desligar a sirene"),
                                          ("menu", "Botões de controle")])

    def configurar_telegram(self, token: str, chat_ids: list[str]) -> list[str]:
        """Grava bot e destinatários no .env, liga os alertas e avisa quem foi incluído agora."""
        anteriores = set(self.telegram.chat_ids) if token == self.telegram.token else set()
        cfgmod.atualizar_env({"TELEGRAM_BOT_TOKEN": token, "TELEGRAM_CHAT_ID": ",".join(chat_ids)})
        self.telegram.recarregar()
        if not self.cfg["alertas"]["telegram"] and not self.modo_teste:
            novo = cfgmod.copia(self.cfg)
            novo["alertas"]["telegram"] = True
            self.aplicar_config(novo)
        self._ligar_telegram()
        novos = [c for c in chat_ids if c not in anteriores]
        for cid in novos:
            self.telegram.desconhecidos.pop(cid, None)
        if novos:
            self.telegram.enviar_aviso("✅ Vigia conectado! Você vai receber aqui os alertas com foto.\n"
                                       "Mande /menu para ver os comandos (armar, desarmar, ver câmeras...).",
                                       botoes=self.BOTOES_MENU, apenas=novos)
        log.info("Telegram configurado pelo painel: %s destinatário(s)", len(chat_ids))
        return novos

    # --- comandos do Telegram (só chegam aqui os chat ids autorizados) -------

    BOTOES_MENU = [("🔒 Armar", "armar"), ("🔓 Desarmar", "desarmar"), ("📊 Status", "status"), ("📷 Ver câmeras", "ver"),
                   ("🔕 Parar sirene", "parar")]

    @staticmethod
    def _nome(quem: str) -> str:
        return quem.removeprefix("Telegram (").rstrip(")")

    def _tg_menu(self, quem: str, chat_id: str, arg: str = ""):
        return self._texto_status(), self.BOTOES_MENU

    def _tg_status(self, quem: str, chat_id: str, arg: str = ""):
        return self._texto_status(), self.BOTOES_MENU

    def _texto_status(self) -> str:
        cams = [c for c in self.cfg["cameras"] if c["ativo"]]
        online = sum(1 for c in cams if self.fontes.get(c["id"]) and self.fontes[c["id"]].conectado)
        estado = "🔒 ARMADO" if self.armado else "🔓 DESARMADO"
        linhas = [f"{estado} ({self.armado_origem or '—'}, desde {time.strftime('%H:%M', time.localtime(self.armado_desde))})",
                  f"📷 Câmeras conectadas: {online} de {len(cams)}",
                  f"🔊 Sirene: {'TOCANDO' if self.sirene.tocando else 'desligada'}"]
        hoje = sum(v["hoje"] for v in self.eventos.resumo_hoje().values())
        linhas.append(f"🚶 Eventos hoje: {hoje}")
        return "\n".join(linhas)

    def _tg_armar(self, quem: str, chat_id: str, arg: str = ""):
        if self.armado:
            return f"Já estava armado ({self.armado_origem}).", self.BOTOES_MENU
        nome = self._nome(quem)
        self.definir_armado(True, f"Telegram · {nome}")
        self.telegram.enviar_aviso(f"🔒 Alarme ARMADO por {nome}.", excluir=chat_id)
        return "🔒 Armado. Vigiando as câmeras agora.", self.BOTOES_MENU

    def _tg_desarmar(self, quem: str, chat_id: str, arg: str = ""):
        if not self.armado:
            return f"Já estava desarmado ({self.armado_origem}).", self.BOTOES_MENU
        nome = self._nome(quem)
        self.definir_armado(False, f"Telegram · {nome}")
        self.telegram.enviar_aviso(f"🔓 Alarme DESARMADO por {nome}.", excluir=chat_id)
        return "🔓 Desarmado. Nenhum evento, alerta ou sirene até armar de novo.", self.BOTOES_MENU

    def _tg_parar(self, quem: str, chat_id: str, arg: str = ""):
        if not self.sirene.tocando:
            return "A sirene não está tocando."
        self.sirene.parar(quem=quem)
        return "🔕 Sirene desligada."

    def _tg_ver(self, quem: str, chat_id: str, arg: str = ""):
        """Foto atual (com zonas e detecções) de uma câmera ou de todas, só para quem pediu."""
        cams = [c for c in self.cfg["cameras"] if c["ativo"]]
        if arg:
            a = arg.lower()
            cams = [c for c in cams if a in (c["id"].lower(), c["nome"].lower())
                    or (a.isdigit() and c["nome"].lower().endswith(f" {a}"))]
            if not cams:
                return f"Câmera \"{arg}\" não encontrada.", self._botoes_cameras()
        fotos, sem_imagem = [], []
        for cam in cams:
            img = self.frame_anotado(cam["id"])
            if img is None:
                sem_imagem.append(cam["nome"])
                continue
            if img.shape[1] > 1280:
                img = cv2.resize(img, (1280, round(img.shape[0] * 1280 / img.shape[1])), interpolation=cv2.INTER_AREA)
            ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                fotos.append((f"{cam['nome']} · {time.strftime('%H:%M:%S')}", jpg.tobytes()))
        log.info("Fotos das câmeras pedidas pelo %s", quem)
        texto = ""
        if fotos and not self.telegram.enviar_fotos_para(chat_id, fotos):
            texto = "Não consegui enviar as fotos agora. Tente de novo."
        if sem_imagem:
            texto = (texto + "\n" if texto else "") + "Sem imagem: " + ", ".join(sem_imagem)
        return texto or "📷 Ver de novo:", self._botoes_cameras()

    def _botoes_cameras(self):
        cams = [c for c in self.cfg["cameras"] if c["ativo"]]
        return [(c["nome"], f"ver {c['id']}") for c in cams] + [("📷 Todas", "ver"), ("📊 Status", "status")]

    def _sirene_mudou(self, tocando: bool, motivo: str, quem: str):
        if not self.cfg["alertas"]["telegram"]:
            return
        if tocando:
            self.telegram.enviar_aviso(f"🔊 SIRENE TOCANDO: {motivo}", [("🔕 Parar sirene", "parar_sirene")])
        elif quem.startswith("Telegram"):
            self.telegram.enviar_aviso(f"🔕 Sirene desligada por {quem.removeprefix('Telegram (').rstrip(')')}.")

    def definir_armado(self, armado: bool, origem: str):
        if armado != self.armado or origem != self.armado_origem:
            self.armado, self.armado_origem, self.armado_desde = armado, origem, time.time()
            log.info("%s (%s)", "ARMADO" if armado else "DESARMADO", origem)
        if not armado:
            self.sirene.parar(quem="desarmado")

    def no_horario(self) -> bool:
        """Alguma câmera armável está no horário automático agora?"""
        return any(c["ativo"] and em_janela(c["janelas"]) for c in self.cfg["cameras"])

    def _agendador(self):
        """Arma no início do horário e desarma no fim. O botão manual vale até a próxima virada."""
        anterior = self.no_horario()
        if self.ignorar_horario:
            self.definir_armado(True, "manual, ao iniciar")
        else:  # ao ligar o PC no meio da noite, já começa armado
            self.definir_armado(anterior, "horário automático")
        while not self._parar_agendador.wait(15):
            agora = self.no_horario()
            if agora != anterior:
                self.definir_armado(agora, "horário automático")
            anterior = agora

    def parar(self):
        self._parar_agendador.set()
        self.sirene.parar(quem="Vigia encerrado")
        with self._lock:
            for cid in list(self.fontes):
                self._parar_camera(cid)

    def _carregar_detector(self, modelo: str):
        try:
            self.detector = Detector(modelo)
            self.erro_detector = ""
        except Exception as e:
            self.erro_detector = str(e)
            log.exception("Não foi possível carregar o modelo %s", modelo)

    def _iniciar_camera(self, cam: dict):
        bloqueio = "" if cam.get("fonte") else self.erro_credenciais
        fonte = Fonte(cam["id"], cfgmod.url_camera(self.cfg, cam), bloqueio)
        mon = MonitorCamera(self, cam, fonte)
        self.fontes[cam["id"]], self.monitores[cam["id"]] = fonte, mon
        fonte.start()
        mon.start()
        mon.gravador.start()

    def _parar_camera(self, cid: str):
        mon = self.monitores.pop(cid)
        mon.parar()
        mon.gravador.parar()
        self.fontes.pop(cid).parar()

    def aplicar_config(self, novo: dict) -> dict:
        """Valida, salva e aplica sem reiniciar o programa."""
        if self.modo_teste:
            raise cfgmod.ConfigError("No modo de teste a configuração não pode ser salva.")
        fontes_atuais = {c["id"]: c.get("fonte") for c in self.cfg["cameras"]}
        for cam in novo.get("cameras") or []:
            if isinstance(cam, dict) and cam.get("fonte") and cam.get("fonte") != fontes_atuais.get(cam.get("id")):
                raise cfgmod.ConfigError("Fontes de vídeo por arquivo/URL só podem ser definidas editando o config.yaml.")
        novo = cfgmod.salvar(novo)
        with self._lock:
            antigo = self.cfg
            self.cfg = novo
            if novo["deteccao"]["modelo"] != antigo["deteccao"]["modelo"]:
                self._carregar_detector(novo["deteccao"]["modelo"])
            novas = {c["id"]: c for c in novo["cameras"]}
            for cid in list(self.monitores):
                if cid not in novas:
                    self._parar_camera(cid)
            for cid, cam in novas.items():
                mon = self.monitores.get(cid)
                if mon is None:
                    self._iniciar_camera(cam)
                elif cfgmod.url_camera(novo, cam) != mon.fonte.url:
                    self._parar_camera(cid)
                    self._iniciar_camera(cam)
                else:
                    mon.cam = cam  # horários, zonas, parâmetros: vale no próximo ciclo
        self.sirene.configurar(novo["sirene"]["som"], novo["sirene"]["voz"])
        log.info("Configuração atualizada pelo painel")
        return novo

    def definir_camera_ativa(self, cid: str, ativo: bool) -> dict:
        """Arma/desarma uma câmera (fica salvo no config.yaml)."""
        if self.modo_teste:  # no modo teste não há config para salvar
            cam = self.camera(cid)
            cam["ativo"] = ativo
            return self.cfg
        novo = cfgmod.copia(self.cfg)
        for cam in novo["cameras"]:
            if cam["id"] == cid:
                cam["ativo"] = ativo
        return self.aplicar_config(novo)

    def aplicar_descoberta(self, busca, escolhidas: list[dict]) -> dict:
        """Usa o resultado da busca de câmeras: formato da marca, aparelho e câmeras escolhidas.

        No mesmo aparelho, câmeras já configuradas no mesmo canal mantêm zonas e horários.
        Usuário/senha vão para o .env (só se mudaram).
        """
        if self.modo_teste:
            raise cfgmod.ConfigError("No modo de teste a configuração não pode ser salva.")
        if not escolhidas:
            raise cfgmod.ConfigError("Escolha pelo menos uma câmera.")
        validos = {c["canal"] for c in busca.canais}
        cred = cfgmod.credenciais()
        novo = cfgmod.copia(self.cfg)
        novo["dvr"]["url_template"] = busca.modelo
        existentes = {c["canal"]: c for c in novo["cameras"] if not c.get("fonte")} if busca.host == cred["host"] else {}
        janelas = (novo["cameras"][0]["janelas"] if novo["cameras"]
                   else [{"dias": list(cfgmod.DIAS), "inicio": "22:00", "fim": "06:00"}])
        cams = [c for c in novo["cameras"] if c.get("fonte")]
        canais = sorted({int(e["canal"]) for e in escolhidas})
        ids = {c["id"] for c in cams} | {existentes[k]["id"] for k in canais if k in existentes}
        nomes = {int(e["canal"]): str(e.get("nome") or "").strip()[:60] for e in escolhidas}
        for canal in canais:
            if canal not in validos:
                raise cfgmod.ConfigError(f"O canal {canal} não apareceu na busca.")
            if canal in existentes:
                cam = {**existentes[canal], "nome": nomes[canal] or existentes[canal]["nome"]}
            else:
                n = canal
                while f"cam{n}" in ids:
                    n += 1
                cam = {"id": f"cam{n}", "nome": nomes[canal] or f"Canal {canal}", "canal": canal, "ativo": True,
                       "janelas": cfgmod.copia(janelas), "zonas": []}
                ids.add(cam["id"])
            cams.append(cam)
        novo["cameras"] = cams
        cfgmod.validar(cfgmod.copia(novo))  # erro aqui não mexe no .env
        if (busca.host, str(busca.porta), busca.user, busca.senha) != (cred["host"], cred["port"], cred["user"], cred["password"]):
            cfgmod.atualizar_env({"CAM_HOST": busca.host, "CAM_RTSP_PORT": str(busca.porta),
                                  "CAM_USER": busca.user, "CAM_PASSWORD": busca.senha})
            log.info("Gravador atualizado no .env pelo painel (%s)", busca.host)
        # A busca acabou de conectar com estes dados: libera câmeras travadas por senha recusada.
        self.erro_credenciais = ""
        captura._pausa_senha["ate"] = 0.0
        log.info("Câmeras definidas pela busca: %s (%s)", ", ".join(f"canal {c}" for c in canais), busca.marca)
        return self.aplicar_config(novo)

    # --- consultas para o painel -------------------------------------------

    def camera(self, cid: str) -> dict | None:
        return next((c for c in self.cfg["cameras"] if c["id"] == cid), None)

    def frame_anotado(self, cid: str, anotar=True):
        mon, fonte = self.monitores.get(cid), self.fontes.get(cid)
        if not fonte:
            return None
        frame, _, _ = fonte.ultimo()
        if frame is None or not anotar:
            return frame
        dentro, fora, ts, pets = mon.ultimas
        if time.time() - ts > 3:
            dentro, fora, pets = [], [], []
        return desenhar(frame, dentro, fora, mon.cam["zonas"], pets)

    def testar_deteccao(self, cid: str):
        """Roda uma inferência agora, mesmo fora da janela. (imagem, dentro, fora)"""
        cam, fonte = self.camera(cid), self.fontes.get(cid)
        if not cam or not fonte or self.detector is None:
            return None
        frame, _, _ = fonte.ultimo()
        if frame is None:
            return None
        det = cfgmod.deteccao_efetiva(self.cfg, cam)
        todas, pets, _ = self.detector.detectar(
            frame, det["confianca"], det["imgsz"], det["limite_animal"] if det["filtro_animal"] else None)
        dentro, fora = separar_por_zona(todas, cam["zonas"], frame.shape[1], frame.shape[0])
        return desenhar(frame, dentro, fora, cam["zonas"], pets), dentro, fora

    def status(self) -> dict:
        import psutil

        resumo = self.eventos.resumo_hoje()
        cams = []
        for cam in self.cfg["cameras"]:
            f, m = self.fontes.get(cam["id"]), self.monitores.get(cam["id"])
            if not f or not m:
                continue
            _, _, ts = f.ultimo()
            det = m.det
            dentro, _, ts_det, pets = m.ultimas
            cams.append({
                "id": cam["id"], "nome": cam["nome"], "ativo": cam["ativo"],
                "conectado": f.conectado, "fps": round(f.fps, 1),
                "idade_frame_s": round(time.time() - ts, 1) if ts else None,
                "resolucao": f.resolucao, "reconexoes": f.reconexoes, "erro": f.ultimo_erro,
                "em_janela": m.em_janela,
                "pessoas_agora": len(dentro) if time.time() - ts_det < 3 else 0,
                "pets_agora": len(pets) if time.time() - ts_det < 3 else 0,
                "descartadas_pet": m.descartadas_pet,
                "historico": f"{sum(m.historico)}/{det['k']} de {det['n']}",
                "inferencias": m.inferencias, "ms_inferencia": round(m.ms_inferencia),
                "puladas": m.puladas, "movimento_pct": round(m.movimento_pct, 2),
                "cooldown_restante_s": max(0, round(det["cooldown_s"] - (time.time() - m.ultimo_alerta))),
                "em_evento": m.em_evento,
                "gravando": m.gravador.gravando,
                "eventos_hoje": resumo.get(cam["id"], {}).get("hoje", 0),
                "ultimo_evento": resumo.get(cam["id"], {}).get("ultimo"),
            })
        proc = self._proc = getattr(self, "_proc", None) or psutil.Process()
        return {
            "armado": self.armado, "armado_origem": self.armado_origem,
            "armado_desde": time.strftime("%H:%M", time.localtime(self.armado_desde)),
            "no_horario": self.no_horario(), "modo_teste": self.modo_teste, "ignorar_horario": self.ignorar_horario,
            "uptime_s": round(time.time() - self.inicio),
            "cpu": psutil.cpu_percent(None), "cpu_processo": round(proc.cpu_percent(None) / psutil.cpu_count(), 1),
            "memoria_mb": round(proc.memory_info().rss / 2**20),
            "modelo": self.detector.nome if self.detector else None, "erro_modelo": self.erro_detector,
            "erro_credenciais": self.erro_credenciais,
            "sirene": self.sirene.status(),
            "telegram": {
                "configurado": self.telegram.configurado, "ativo": self.telegram.ativo,
                "enviados": self.telegram.enviados, "erro": self.telegram.ultimo_erro,
            },
            "cameras": cams,
        }
