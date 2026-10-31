"""Descoberta de gravadores e câmeras na rede LOCAL.

1. `buscar_aparelhos()`: ONVIF (WS-Discovery, multicast) + varredura da porta 554 na sub-rede.
2. `BuscaCanais`: com usuário/senha, descobre o formato da URL RTSP (pelo ONVIF ou
   testando os formatos das marcas mais comuns) e tira uma miniatura de cada canal.

Cuidado com o bloqueio de senha: DVRs bloqueiam o usuário após poucos logins errados.
A busca para no primeiro "senha recusada" e cada aparelho fica um tempo sem novas tentativas.
"""
import base64
import hashlib
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlsplit
from xml.etree import ElementTree as ET

import cv2
import numpy as np
import requests

log = logging.getLogger("descoberta")

# Formatos conhecidos. {stream} = subtype + 1 (Hikvision: canal 1 principal = 101, secundário = 102).
MARCAS = [
    ("Intelbras / Dahua", "rtsp://{user}:{password}@{host}:{port}/cam/realmonitor?channel={channel}&subtype={subtype}"),
    ("Hikvision / HiLook", "rtsp://{user}:{password}@{host}:{port}/Streaming/Channels/{channel}0{stream}"),
    ("XMeye / genéricos", "rtsp://{host}:{port}/user={user}&password={password}&channel={channel}&stream={subtype}.sdp?real_stream"),
    ("Uniview", "rtsp://{user}:{password}@{host}:{port}/unicast/c{channel}/s{subtype}/live"),
]
PAUSA_APOS_RECUSA_S = 300
_recusas: dict[str, float] = {}  # host -> instante da última senha recusada


# --- 1. aparelhos na rede ------------------------------------------------------

def _ip_local() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))  # não envia nada; só escolhe a interface
        return s.getsockname()[0]
    finally:
        s.close()


_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
 xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
<e:Header><w:MessageID>uuid:{id}</w:MessageID><w:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
<w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action></e:Header>
<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body></e:Envelope>"""


def _sondar_onvif(espera: float, ips: list[str] = ()) -> dict[str, dict]:
    """WS-Discovery: pergunta 'quem é aparelho ONVIF?' (UDP 3702).

    O multicast só alcança a rede do PC; aparelhos atrás de outro roteador
    são perguntados diretamente pelo IP (`ips`).
    """
    achados = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)  # não sai da rede local
        s.settimeout(0.5)
        for destino in ["239.255.255.250", *ips]:
            for _ in range(2):
                s.sendto(_PROBE.format(id=uuid.uuid4()).encode(), (destino, 3702))
        fim = time.time() + espera
        while time.time() < fim:
            try:
                dados, (ip, _) = s.recvfrom(65535)
            except (socket.timeout, ConnectionResetError):  # Windows: ICMP "porta fechada" de um IP sondado
                continue
            texto = dados.decode("utf-8", "ignore")
            xaddrs = re.search(r"<[^>]*XAddrs>([^<]+)<", texto)
            escopos = re.search(r"<[^>]*Scopes[^>]*>([^<]+)<", texto)
            info = {"ip": ip, "onvif": xaddrs.group(1).split()[0] if xaddrs else ""}
            for esc in (escopos.group(1).split() if escopos else []):
                for chave in ("name", "hardware"):
                    if f"/{chave}/" in esc:
                        info[chave] = requests.utils.unquote(esc.rsplit("/", 1)[1])
            if ipaddress.ip_address(ip).is_private:
                achados[ip] = info
    except OSError as e:
        log.warning("Busca ONVIF falhou: %s", e)
    finally:
        s.close()
    return achados


def _porta_aberta(ip: str, porta: int, espera=0.6) -> bool:
    try:
        with socket.create_connection((ip, porta), timeout=espera):
            return True
    except OSError:
        return False


def _virtual(nome: str) -> bool:
    return bool(re.search(r"vEthernet|WSL|Hyper-V|VirtualBox|VMware|Bluetooth|Loopback|Tailscale|ZeroTier|docker", nome, re.I))


def redes_locais() -> list[ipaddress.IPv4Network]:
    """Sub-redes (/24) das placas de rede reais do PC."""
    import psutil

    redes = []
    ativas = {n for n, st in psutil.net_if_stats().items() if st.isup}
    for nome, enderecos in psutil.net_if_addrs().items():
        if nome not in ativas or _virtual(nome):
            continue
        for e in enderecos:
            if e.family == socket.AF_INET:
                ip = ipaddress.ip_address(e.address)
                if ip.is_private and not ip.is_link_local and not ip.is_loopback:
                    redes.append(ipaddress.ip_network(f"{ip}/24", strict=False))
    if not redes:
        redes.append(ipaddress.ip_network(f"{_ip_local()}/24", strict=False))
    return list(dict.fromkeys(redes)) + redes_acima()


def redes_acima() -> list[ipaddress.IPv4Network]:
    """Rede do roteador "de cima" quando há dois roteadores em casa (ex.: o da operadora + um Wi-Fi).

    Rastreia só os 2 primeiros saltos do caminho para a internet (nada sai de casa)
    e devolve a /24 do segundo salto se ele for um endereço privado.
    """
    if os.name == "nt":
        cmd = ["tracert", "-d", "-h", "2", "-w", "400", "1.1.1.1"]
    elif shutil.which("traceroute"):
        cmd = ["traceroute", "-n", "-m", "2", "-w", "1", "1.1.1.1"]
    else:
        return []
    try:
        saida = subprocess.run(cmd, capture_output=True, text=True, timeout=10, errors="ignore",
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (subprocess.TimeoutExpired, OSError):
        return []
    saltos = [m.group(1) for linha in saida.splitlines()
              if (m := re.match(r"\s*\d+\s.*?(\d+\.\d+\.\d+\.\d+)\s*$", linha))]
    if len(saltos) < 2:
        return []
    ip = ipaddress.ip_address(saltos[1])
    return [ipaddress.ip_network(f"{ip}/24", strict=False)] if ip.is_private and not ip.is_link_local else []


def rede_permitida(texto: str) -> ipaddress.IPv4Network:
    """IP ou rede digitados pelo usuário: só endereços privados, no máximo /22."""
    try:
        rede = ipaddress.ip_network(texto.strip(), strict=False)
    except ValueError:
        raise ValueError("Use um IP (192.168.1.2) ou uma rede (192.168.1.0/24).")
    if rede.version != 4 or not rede.is_private or rede.prefixlen < 22:
        raise ValueError("Só é permitido procurar em redes privadas (192.168.x, 10.x, 172.16-31.x), até /22.")
    return rede


def buscar_aparelhos(extras: list[ipaddress.IPv4Network] = ()) -> tuple[list[dict], list[ipaddress.IPv4Network]]:
    """Aparelhos com vídeo nas redes do PC (e nas `extras`): (aparelhos, redes verificadas)."""
    redes = list(dict.fromkeys([*redes_locais(), *extras]))
    ips = list(dict.fromkeys(str(ip) for r in redes for ip in (r.hosts() if r.num_addresses > 1 else [r.network_address])))
    with ThreadPoolExecutor(max_workers=128) as ex:
        rtsp = [ip for ip, ok in zip(ips, ex.map(lambda ip: _porta_aberta(ip, 554), ips)) if ok]
    onvif = _sondar_onvif(3.0, rtsp)
    saida = []
    for ip in sorted(set(onvif) | set(rtsp), key=ipaddress.ip_address):
        o = onvif.get(ip, {})
        saida.append({"ip": ip, "rtsp": ip in rtsp, "onvif": bool(o.get("onvif")),
                      "nome": " ".join(filter(None, [o.get("name"), o.get("hardware")])).strip()})
    return saida, redes


# --- 2. ONVIF autenticado --------------------------------------------------------

class SenhaRecusada(Exception):
    pass


def _onvif(url: str, corpo: str, user: str, senha: str, desvio: timedelta) -> ET.Element:
    seguranca = ""
    if user:
        nonce = os.urandom(16)
        criado = (datetime.now(timezone.utc) + desvio).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha1(nonce + criado.encode() + senha.encode()).digest()).decode()
        seguranca = f"""<s:Header><Security s:mustUnderstand="1" xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd">
<UsernameToken><Username>{_xml(user)}</Username>
<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</Password>
<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</Nonce>
<Created xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">{criado}</Created>
</UsernameToken></Security></s:Header>"""
    env = f"""<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
 xmlns:tds="http://www.onvif.org/ver10/device/wsdl" xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
 xmlns:tt="http://www.onvif.org/ver10/schema">{seguranca}<s:Body>{corpo}</s:Body></s:Envelope>"""
    r = requests.post(url, data=env.encode(), headers={"Content-Type": "application/soap+xml; charset=utf-8"}, timeout=8)
    if r.status_code in (400, 401, 403, 500) and re.search(r"NotAuthorized|Unauthorized|FailedAuthentication|Sender not Authorized", r.text, re.I):
        raise SenhaRecusada()
    if r.status_code == 401:
        raise SenhaRecusada()
    r.raise_for_status()
    return ET.fromstring(r.content)


def _xml(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _txt(no: ET.Element, nome: str) -> list[ET.Element]:
    return [e for e in no.iter() if e.tag.rsplit("}", 1)[-1] == nome]


def onvif_streams(xaddr: str, user: str, senha: str) -> list[dict]:
    """Uma URL RTSP por câmera (perfil principal de cada fonte de vídeo). Faz UM login."""
    desvio = timedelta(0)
    try:  # sem senha: hora do aparelho (o login ONVIF falha se os relógios divergirem)
        raiz = _onvif(xaddr, "<tds:GetSystemDateAndTime/>", "", "", desvio)
        utc = next(iter(_txt(raiz, "UTCDateTime")), None)
        if utc is not None:
            v = {e.tag.rsplit("}", 1)[-1]: int(e.text) for e in utc.iter() if e.text and e.text.strip().isdigit()}
            aparelho = datetime(v["Year"], v["Month"], v["Day"], v["Hour"], v["Minute"], v["Second"], tzinfo=timezone.utc)
            desvio = aparelho - datetime.now(timezone.utc)
    except (requests.RequestException, ET.ParseError, KeyError, ValueError):
        pass
    raiz = _onvif(xaddr, '<tds:GetCapabilities><tds:Category>Media</tds:Category></tds:GetCapabilities>', user, senha, desvio)
    media = next((e.text for e in _txt(raiz, "XAddr") if e.text), xaddr)
    raiz = _onvif(media, "<trt:GetProfiles/>", user, senha, desvio)
    por_fonte: dict[str, dict] = {}
    for perfil in _txt(raiz, "Profiles"):
        fonte = next((e.text for e in _txt(perfil, "SourceToken")), None)
        if not fonte or fonte in por_fonte:  # o primeiro perfil de cada fonte é o principal
            continue
        res = next(iter(_txt(perfil, "Resolution")), None)
        tam = {e.tag.rsplit("}", 1)[-1]: e.text for e in res} if res is not None else {}
        por_fonte[fonte] = {"token": perfil.get("token"), "largura": int(tam.get("Width", 0) or 0),
                            "altura": int(tam.get("Height", 0) or 0)}
    saida = []
    for fonte, p in por_fonte.items():
        corpo = (f'<trt:GetStreamUri><trt:StreamSetup><tt:Stream>RTP-Unicast</tt:Stream><tt:Transport><tt:Protocol>RTSP</tt:Protocol>'
                 f'</tt:Transport></trt:StreamSetup><trt:ProfileToken>{_xml(p["token"])}</trt:ProfileToken></trt:GetStreamUri>')
        uri = next((e.text for e in _txt(_onvif(media, corpo, user, senha, desvio), "Uri") if e.text), "")
        if uri:
            saida.append({**p, "fonte": fonte, "uri": uri.strip()})
    return saida


def formato_de_uri(uris: list[str]) -> tuple[str, list[int]] | None:
    """Reconhece a marca pelas URLs que o ONVIF devolveu: (modelo, canais)."""
    canais = []
    padroes = [
        (MARCAS[0][1], r"/cam/realmonitor\?channel=(\d+)"),
        (MARCAS[1][1], r"/Streaming/Channels/(\d+?)0\d\b"),
        (MARCAS[2][1], r"channel=(\d+)&stream="),
        (MARCAS[3][1], r"/unicast/c(\d+)/s\d"),
    ]
    for modelo, rx in padroes:
        canais = [int(m.group(1)) for u in uris if (m := re.search(rx, u, re.I))]
        if canais and len(canais) == len(uris):
            return modelo, canais
    return None


# --- 3. busca de canais (em segundo plano, com progresso para o painel) ---------

def montar_url(modelo: str, host: str, porta: int, user: str, senha: str, canal: int, subtype: int) -> str:
    return modelo.format(user=quote(user, safe=""), password=quote(senha, safe=""), host=host, port=porta,
                         channel=canal, subtype=subtype, stream=subtype + 1)


def _ffprobe(url: str, segundos=8) -> str:
    """ok | senha | nao_encontrado | erro"""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-timeout", str(segundos * 1_000_000),
                            "-select_streams", "v:0", "-show_entries", "stream=width", "-of", "csv=p=0", url],
                           capture_output=True, text=True, timeout=segundos + 5,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (subprocess.TimeoutExpired, OSError):
        return "erro"
    if "401" in r.stderr or "Unauthorized" in r.stderr:
        return "senha"
    if r.returncode == 0 and r.stdout.strip():
        return "ok"
    return "nao_encontrado" if ("404" in r.stderr or "Not Found" in r.stderr) else "erro"


def _miniatura(url: str) -> tuple[bytes | None, bool]:
    """(JPEG 320 px, tem sinal). O gravador só entrega a imagem no próximo quadro-chave (~4 s)."""
    try:
        r = subprocess.run(["ffmpeg", "-v", "error", "-rtsp_transport", "tcp", "-timeout", "8000000",
                            "-analyzeduration", "500000", "-probesize", "200000", "-i", url,
                            "-frames:v", "1", "-vf", "scale=320:-2", "-f", "image2pipe", "-vcodec", "mjpeg", "-q:v", "5", "-"],
                           capture_output=True, timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (subprocess.TimeoutExpired, OSError):
        return None, False
    if not r.stdout:
        return None, False
    img = cv2.imdecode(np.frombuffer(r.stdout, np.uint8), cv2.IMREAD_GRAYSCALE)
    # Canal sem câmera: o gravador manda uma tela preta, às vezes com o logo e a hora por cima.
    sinal = img is not None and (img < 24).mean() < 0.85
    return r.stdout, sinal


class BuscaCanais:
    """Uma busca por vez. O painel consulta `estado()` enquanto ela roda."""

    def __init__(self, host: str, porta: int, user: str, senha: str, max_canais: int = 16, mascarar=None):
        self.host, self.porta, self.user, self.senha = host, porta, user, senha
        self.max_canais = max_canais
        self._mascarar = mascarar or (lambda t: t)
        self.etapa, self.mensagem, self.erro = "iniciando", "", ""
        self.marca, self.modelo = "", ""
        self.canais: list[dict] = []
        self.miniaturas: dict[int, bytes] = {}
        self.fim = False
        self.thread = threading.Thread(target=self._rodar, name="busca-canais", daemon=True)

    def estado(self) -> dict:
        return {"host": self.host, "etapa": self.etapa, "mensagem": self.mensagem, "erro": self.erro,
                "marca": self.marca, "canais": self.canais, "fim": self.fim}

    def _rodar(self):
        try:
            espera = PAUSA_APOS_RECUSA_S - (time.time() - _recusas.get(self.host, 0))
            if espera > 0:
                raise SenhaRecusada(f"A senha foi recusada há pouco. Aguarde {int(espera // 60) + 1} min antes de "
                                    "tentar de novo (o gravador pode bloquear o usuário após várias tentativas).")
            if not shutil.which("ffprobe"):
                raise RuntimeError("ffprobe não encontrado. Instale o FFmpeg (veja o README).")
            if not _porta_aberta(self.host, self.porta, 3):
                raise RuntimeError(f"O aparelho {self.host} não respondeu na porta de vídeo (RTSP) {self.porta}.")
            # Primeiro os formatos conhecidos: com a senha certa, nenhum login errado é feito.
            # O ONVIF fica por último porque alguns gravadores têm um usuário ONVIF separado
            # (o login recusado conta para o bloqueio).
            canais = self._por_tentativa() or self._por_onvif()
            if not self.modelo:
                raise RuntimeError("Não reconheci o formato deste aparelho. Verifique se é um gravador/câmera com RTSP "
                                   "ligado e, se tiver, ative o ONVIF no menu de rede dele.")
            self._miniaturas(canais)
        except SenhaRecusada as e:
            _recusas[self.host] = time.time()
            self.erro = str(e) or "Usuário ou senha recusados pelo aparelho. Confira e tente de novo daqui a 5 min."
            log.warning("Busca de câmeras em %s: senha recusada", self.host)
        except Exception as e:
            self.erro = self._mascarar(str(e))
            log.warning("Busca de câmeras em %s falhou: %s", self.host, self.erro)
        finally:
            self.etapa, self.fim = "fim", True

    def _por_onvif(self) -> list[int]:
        self.etapa, self.mensagem = "onvif", "Perguntando ao aparelho pelo ONVIF..."
        if not any(_porta_aberta(self.host, p) for p in (80, 8000, 8080, 8899)):
            return []
        for porta in (80, 8000, 8080, 8899):
            if not _porta_aberta(self.host, porta):
                continue
            try:
                streams = onvif_streams(f"http://{self.host}:{porta}/onvif/device_service", self.user, self.senha)
            except SenhaRecusada:
                raise SenhaRecusada("Nenhum formato conhecido funcionou e o ONVIF recusou o usuário/senha. Alguns "
                                    "gravadores têm um usuário ONVIF separado (veja no menu de rede dele). "
                                    "Aguarde 5 min antes de tentar de novo.")
            except (requests.RequestException, ET.ParseError):
                continue
            achado = formato_de_uri([s["uri"] for s in streams])
            if achado:
                self.modelo, canais = achado
                self.marca = next(n for n, m in MARCAS if m == self.modelo) + " (pelo ONVIF)"
                p = urlsplit(streams[0]["uri"]).port
                if p:
                    self.porta = p
                return sorted(set(canais))
            log.info("ONVIF em %s devolveu URLs de formato desconhecido", self.host)
            return []
        return []

    def _por_tentativa(self) -> list[int]:
        self.etapa = "formato"
        for nome, modelo in MARCAS:
            self.mensagem = f"Testando o formato {nome}..."
            for canal in (1, 2, 3, 4):
                st = _ffprobe(montar_url(modelo, self.host, self.porta, self.user, self.senha, canal, 1))
                if st == "senha":
                    raise SenhaRecusada()
                if st == "ok":
                    self.marca, self.modelo = nome, modelo
                    return list(range(1, self.max_canais + 1))
        return []

    def _miniaturas(self, canais: list[int]):
        self.etapa = "canais"
        self.mensagem = f"Verificando {len(canais)} canais..."

        def um(canal):
            jpg, sinal = _miniatura(montar_url(self.modelo, self.host, self.porta, self.user, self.senha, canal, 1))
            if jpg:
                self.miniaturas[canal] = jpg
            return {"canal": canal, "imagem": bool(jpg), "sinal": bool(sinal)}

        with ThreadPoolExecutor(max_workers=4) as ex:  # poucos por vez: o gravador limita conexões
            for item in ex.map(um, canais):
                self.canais.append(item)
                self.mensagem = f"Verificando canais... {len(self.canais)} de {len(canais)}"
        self.canais = [c for c in self.canais if c["imagem"]]
        n = sum(c["sinal"] for c in self.canais)
        self.mensagem = f"{n} câmera(s) com imagem" + (f", {len(self.canais) - n} canal(is) sem sinal." if len(self.canais) > n else ".")
