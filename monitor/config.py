"""Carga, validação e gravação do config.yaml + credenciais do .env."""
import copy
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import quote

import yaml
from dotenv import load_dotenv

RAIZ = Path(__file__).resolve().parent.parent
CONFIG_PATH = RAIZ / "config.yaml"
load_dotenv(RAIZ / ".env")

DIAS = ["seg", "ter", "qua", "qui", "sex", "sab", "dom"]
# mesmas chaves de sirene.OPCOES (o primeiro é o padrão)
SONS_SIRENE = ["residencial", "alarme", "policia", "emergencia", "sintetizada", "personalizado"]
URL_PADRAO = "rtsp://{user}:{password}@{host}:{port}/cam/realmonitor?channel={channel}&subtype={subtype}"
DETECCAO_PADRAO = {
    "modelo": "yolo11n.pt",
    "confianca": 0.5,
    "fps_analise": 1.5,
    "imgsz": 640,
    "k": 3,
    "n": 5,
    "cooldown_s": 120,         # mínimo entre mensagens no Telegram, por câmera
    "ausencia_s": 20,          # sem ninguém por este tempo, a próxima aparição é um evento novo
    "filtro_movimento": True,  # só roda o YOLO quando algo se mexe na imagem
    "filtro_animal": True,     # confere cada "pessoa" com um classificador de animais
    "limite_animal": 0.6,      # descarta a "pessoa" se P(animal) >= este valor
    "movimento_min": 0.3,      # % da imagem que precisa mudar para contar como movimento
}
# (tipo, mínimo, máximo) de cada parâmetro numérico de detecção
LIMITES = {
    "confianca": (float, 0.05, 0.99),
    "fps_analise": (float, 0.2, 10.0),
    "imgsz": (int, 160, 1280),
    "k": (int, 1, 30),
    "n": (int, 1, 30),
    "cooldown_s": (int, 0, 86400),
    "ausencia_s": (int, 3, 3600),
    "movimento_min": (float, 0.01, 50.0),
    "limite_animal": (float, 0.1, 0.99),
}
_HORA = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
# O destino da URL é sempre {host} (o CAM_HOST do .env): impede que alguém edite o modelo
# pelo painel para mandar usuário/senha do DVR a um servidor de fora.
# {user}/{password} também podem ir no caminho (formato XMeye); {stream} = subtype + 1 (Hikvision).
_URL_TEMPLATE = re.compile(r"^rtsp://(\{user\}:\{password\}@)?\{host\}(:\{port\})?/[^@\s{}]*"
                           r"(\{channel\}|\{subtype\}|\{stream\}|\{user\}|\{password\}|[^@\s{}])*$")
_ID = re.compile(r"^[a-z0-9_-]{1,32}$")


class ConfigError(ValueError):
    pass


class _Dumper(yaml.SafeDumper):
    """Grava HH:MM entre aspas: sem elas o YAML 1.1 lê 06:00 como o número 360."""


_Dumper.add_representer(
    str, lambda d, v: d.represent_scalar("tag:yaml.org,2002:str", v, style="'" if _HORA.match(v) else None)
)


def _hora(v) -> str:
    if isinstance(v, int) and 0 <= v < 1440:  # 06:00 lido sem aspas vira 360
        return f"{v // 60:02d}:{v % 60:02d}"
    return str(v)


# --- segredos ---------------------------------------------------------------

def credenciais() -> dict:
    return {
        "user": os.getenv("CAM_USER", ""),
        "password": os.getenv("CAM_PASSWORD", ""),
        "host": os.getenv("CAM_HOST", ""),
        "port": os.getenv("CAM_RTSP_PORT", "554"),
    }


def _valor_env(v: str) -> str:
    if re.fullmatch(r"[^\s#'\"\\]*", v):
        return v
    if "'" not in v:
        return f"'{v}'"  # aspas simples: o python-dotenv lê literalmente
    return '"' + v.replace("\\", "\\\\").replace('"', '\\"') + '"'


def atualizar_env(valores: dict[str, str], path: Path = RAIZ / ".env"):
    """Grava/atualiza chaves no .env (mantém o resto do arquivo) e no ambiente atual."""
    linhas = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    faltando = dict(valores)
    for i, linha in enumerate(linhas):
        chave = linha.split("=", 1)[0].strip()
        if "=" in linha and not linha.lstrip().startswith("#") and chave in faltando:
            linhas[i] = f"{chave}={_valor_env(faltando.pop(chave))}"
    linhas += [f"{k}={_valor_env(v)}" for k, v in faltando.items()]
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".env.")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(linhas) + "\n")
    os.replace(tmp, path)
    os.environ.update(valores)


def segredos() -> list[str]:
    valores = [os.getenv("CAM_PASSWORD", ""), os.getenv("TELEGRAM_BOT_TOKEN", "")]
    saida = set()
    for v in valores:
        if v and len(v) >= 3:
            saida.update({v, quote(v, safe="")})
    return sorted(saida, key=len, reverse=True)


def mascarar(texto: str) -> str:
    for s in segredos():
        texto = texto.replace(s, "****")
    return texto


# --- validação --------------------------------------------------------------

def _validar_deteccao(d: dict, parcial: bool) -> dict:
    if d is None:
        return {}
    if not isinstance(d, dict):
        raise ConfigError("deteccao deve ser um objeto")
    saida = {} if parcial else dict(DETECCAO_PADRAO)
    for chave, valor in d.items():
        if valor is None or valor == "":
            continue
        if chave in ("filtro_movimento", "filtro_animal"):
            saida[chave] = bool(valor)
        elif chave == "modelo":
            if not isinstance(valor, str) or not re.match(r"^[\w.-]+\.pt$", valor):
                raise ConfigError("deteccao.modelo deve ser um nome de arquivo .pt")
            saida[chave] = valor
        elif chave in LIMITES:
            tipo, mn, mx = LIMITES[chave]
            try:
                v = tipo(valor)
            except (TypeError, ValueError):
                raise ConfigError(f"deteccao.{chave} inválido: {valor!r}")
            if not mn <= v <= mx:
                raise ConfigError(f"deteccao.{chave} deve estar entre {mn} e {mx}")
            saida[chave] = v
        else:
            raise ConfigError(f"parâmetro de detecção desconhecido: {chave}")
    k, n = saida.get("k"), saida.get("n")
    if k is not None and n is not None and k > n:
        raise ConfigError("deteccao.k não pode ser maior que n")
    return saida


def _validar_janela(j: dict, onde: str) -> dict:
    dias = j.get("dias") or []
    if not isinstance(dias, list) or not dias or any(x not in DIAS for x in dias):
        raise ConfigError(f"{onde}: dias deve ser lista com valores de {DIAS}")
    ini, fim = _hora(j.get("inicio", "")), _hora(j.get("fim", ""))
    if not _HORA.match(ini) or not _HORA.match(fim):
        raise ConfigError(f"{onde}: inicio/fim devem estar no formato HH:MM")
    return {"dias": [x for x in DIAS if x in dias], "inicio": ini, "fim": fim}


def _validar_zona(z, onde: str) -> list:
    if not isinstance(z, list) or len(z) < 3:
        raise ConfigError(f"{onde}: polígono precisa de pelo menos 3 pontos")
    pontos = []
    for p in z:
        try:
            x, y = float(p[0]), float(p[1])
        except (TypeError, ValueError, IndexError):
            raise ConfigError(f"{onde}: ponto inválido {p!r}")
        if not (0 <= x <= 1 and 0 <= y <= 1):
            raise ConfigError(f"{onde}: coordenadas devem estar entre 0 e 1")
        pontos.append([round(x, 4), round(y, 4)])
    return pontos


def _validar_camera(c: dict, i: int) -> dict:
    if not isinstance(c, dict):
        raise ConfigError(f"cameras[{i}] deve ser um objeto")
    cid = str(c.get("id", ""))
    if not _ID.match(cid):
        raise ConfigError(f"cameras[{i}].id deve ter só letras minúsculas, números, _ ou -")
    onde = f"câmera {cid}"
    cam = {
        "id": cid,
        "nome": str(c.get("nome") or cid)[:60],
        "ativo": bool(c.get("ativo", True)),
    }
    if c.get("fonte"):
        cam["fonte"] = str(c["fonte"])
    else:
        try:
            cam["canal"] = int(c.get("canal"))
        except (TypeError, ValueError):
            raise ConfigError(f"{onde}: canal deve ser um número")
        if not 0 <= cam["canal"] <= 64:
            raise ConfigError(f"{onde}: canal fora da faixa 0–64")
    cam["janelas"] = [_validar_janela(j, f"{onde}, janela {k + 1}") for k, j in enumerate(c.get("janelas") or [])]
    cam["zonas"] = [_validar_zona(z, f"{onde}, zona {k + 1}") for k, z in enumerate(c.get("zonas") or [])]
    det = _validar_deteccao(c.get("deteccao"), parcial=True)
    if det:
        cam["deteccao"] = det
    return cam


def validar(dados: dict) -> dict:
    if not isinstance(dados, dict):
        raise ConfigError("config deve ser um objeto")
    dvr = dados.get("dvr") or {}
    template = str(dvr.get("url_template") or URL_PADRAO)
    if not _URL_TEMPLATE.match(template):
        raise ConfigError("dvr.url_template deve ser rtsp://{user}:{password}@{host}:{port}/... "
                          "(o endereço do DVR vem sempre do CAM_HOST no .env)")
    subtype = int(dvr.get("subtype", 1))
    if subtype not in (0, 1, 2):
        raise ConfigError("dvr.subtype deve ser 0, 1 ou 2")
    cameras = [_validar_camera(c, i) for i, c in enumerate(dados.get("cameras") or [])]
    ids = [c["id"] for c in cameras]
    if len(ids) != len(set(ids)):
        raise ConfigError("ids de câmera repetidos")
    grav = dados.get("gravacao") or {}
    if not isinstance(grav, dict):
        raise ConfigError("gravacao deve ser um objeto")
    gravacao = {"ativo": bool(grav.get("ativo", True))}
    for chave, (padrao, mn, mx) in {"pre_s": (5, 0, 30), "pos_s": (10, 2, 120), "max_s": (300, 10, 1800)}.items():
        try:
            v = int(grav.get(chave, padrao))
        except (TypeError, ValueError):
            raise ConfigError(f"gravacao.{chave} inválido")
        if not mn <= v <= mx:
            raise ConfigError(f"gravacao.{chave} deve estar entre {mn} e {mx}")
        gravacao[chave] = v
    sir = dados.get("sirene") or {}
    if not isinstance(sir, dict):
        raise ConfigError("sirene deve ser um objeto")
    sirene = {"ativo": bool(sir.get("ativo", False)), "voz": bool(sir.get("voz", True)),
              "som": str(sir.get("som") or SONS_SIRENE[0])}
    if sirene["som"] not in SONS_SIRENE:
        raise ConfigError(f"sirene.som deve ser um de: {', '.join(SONS_SIRENE)}")
    for chave, (padrao, mn, mx) in {"atraso_s": (5, 0, 120), "duracao_s": (60, 5, 900)}.items():
        try:
            v = int(sir.get(chave, padrao))
        except (TypeError, ValueError):
            raise ConfigError(f"sirene.{chave} inválido")
        if not mn <= v <= mx:
            raise ConfigError(f"sirene.{chave} deve estar entre {mn} e {mx}")
        sirene[chave] = v
    alertas = dados.get("alertas") or {}
    if not isinstance(alertas, dict):
        raise ConfigError("alertas deve ser um objeto")
    cfg = {
        "dvr": {"url_template": template, "subtype": subtype},
        "alertas": {"telegram": bool(alertas.get("telegram", True))},
        "gravacao": gravacao,
        "sirene": sirene,
        "deteccao": _validar_deteccao(dados.get("deteccao") or {}, parcial=False),
        "cameras": cameras,
    }
    det = cfg["deteccao"]
    for cam in cameras:
        efetiva = {**det, **cam.get("deteccao", {})}
        if efetiva["k"] > efetiva["n"]:
            raise ConfigError(f"câmera {cam['id']}: k não pode ser maior que n")
    return cfg


# --- leitura/gravação -------------------------------------------------------

EXEMPLO_PATH = RAIZ / "config.example.yaml"


def carregar(path: Path = CONFIG_PATH) -> dict:
    if not path.exists() and path == CONFIG_PATH and EXEMPLO_PATH.exists():
        import shutil
        shutil.copyfile(EXEMPLO_PATH, path)  # primeira execução: parte do exemplo
    with open(path, encoding="utf-8") as f:
        return validar(yaml.safe_load(f) or {})


def salvar(cfg: dict, path: Path = CONFIG_PATH) -> dict:
    cfg = validar(cfg)
    texto = "# Configuração do monitoramento (editada pelo painel). Credenciais ficam no .env.\n"
    texto += yaml.dump(cfg, Dumper=_Dumper, allow_unicode=True, sort_keys=False, default_flow_style=None)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(texto)
    os.replace(tmp, path)
    return cfg


def deteccao_efetiva(cfg: dict, cam: dict) -> dict:
    return {**cfg["deteccao"], **cam.get("deteccao", {})}


def url_camera(cfg: dict, cam: dict) -> str:
    """URL real (com senha). Nunca logar o retorno sem mascarar()."""
    if cam.get("fonte"):
        return cam["fonte"]
    c = credenciais()
    return cfg["dvr"]["url_template"].format(
        user=quote(c["user"], safe=""),
        password=quote(c["password"], safe=""),
        host=c["host"],
        port=c["port"],
        channel=cam["canal"],
        subtype=cfg["dvr"]["subtype"],
        stream=cfg["dvr"]["subtype"] + 1,
    )


def copia(cfg: dict) -> dict:
    return copy.deepcopy(cfg)
