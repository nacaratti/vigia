"""Varre a sub-rede LOCAL procurando gravadores/câmeras (portas RTSP/HTTP/Dahua).

Uso:
    python scripts/descobrir_rede.py                 # detecta a sub-rede automaticamente
    python scripts/descobrir_rede.py 192.168.1.0/24  # sub-rede explícita

Por segurança, só aceita redes privadas (RFC 1918) de no máximo /22.
"""
import ipaddress
import socket
import sys
from concurrent.futures import ThreadPoolExecutor

PORTAS = {
    554: "RTSP",
    80: "HTTP",
    37777: "Dahua/Intelbras (SDK)",
    8000: "HTTP alt / Hikvision",
    443: "HTTPS",
}
TIMEOUT = 0.6


def detectar_subrede() -> ipaddress.IPv4Network:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Não envia pacote algum; só descobre qual interface seria usada.
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    finally:
        s.close()
    return ipaddress.ip_network(f"{ip}/24", strict=False)


def porta_aberta(ip: str, porta: int) -> bool:
    try:
        with socket.create_connection((ip, porta), timeout=TIMEOUT):
            return True
    except OSError:
        return False


def banner_http(ip: str) -> str:
    try:
        with socket.create_connection((ip, 80), timeout=1.5) as s:
            s.sendall(f"HEAD / HTTP/1.0\r\nHost: {ip}\r\n\r\n".encode())
            dados = s.recv(1024).decode(errors="ignore")
        for linha in dados.splitlines():
            if linha.lower().startswith(("server:", "www-authenticate:")):
                return linha.strip()
    except OSError:
        pass
    return ""


def opcoes_rtsp(ip: str) -> str:
    """Envia RTSP OPTIONS sem credenciais e devolve a linha de status/servidor."""
    try:
        with socket.create_connection((ip, 554), timeout=1.5) as s:
            s.sendall(f"OPTIONS rtsp://{ip}:554/ RTSP/1.0\r\nCSeq: 1\r\n\r\n".encode())
            dados = s.recv(1024).decode(errors="ignore")
        linhas = dados.splitlines()
        servidor = next((l.strip() for l in linhas if l.lower().startswith("server:")), "")
        return f"{linhas[0].strip() if linhas else ''} {servidor}".strip()
    except OSError:
        return ""


def varrer_host(ip: str):
    abertas = [p for p in PORTAS if porta_aberta(ip, p)]
    return ip, abertas


def main():
    rede = ipaddress.ip_network(sys.argv[1], strict=False) if len(sys.argv) > 1 else detectar_subrede()
    if not rede.is_private or rede.prefixlen < 22:
        sys.exit(f"Recusado: {rede} não é uma rede privada pequena (RFC 1918, /22 ou menor).")

    print(f"Varrendo {rede} nas portas {', '.join(map(str, PORTAS))} ...")
    with ThreadPoolExecutor(max_workers=128) as ex:
        resultados = list(ex.map(varrer_host, (str(h) for h in rede.hosts())))

    candidatos = [(ip, ps) for ip, ps in resultados if ps]
    if not candidatos:
        print("Nenhum host com essas portas abertas.")
        return

    print(f"\n{'IP':<16} {'Portas':<28} Detalhes")
    for ip, ps in sorted(candidatos, key=lambda r: ipaddress.ip_address(r[0])):
        detalhes = []
        if 554 in ps:
            detalhes.append(opcoes_rtsp(ip))
        if 80 in ps:
            detalhes.append(banner_http(ip))
        marca = "  <-- provável câmera/DVR" if 554 in ps or 37777 in ps else ""
        print(f"{ip:<16} {','.join(map(str, ps)):<28} {' | '.join(d for d in detalhes if d)}{marca}")


if __name__ == "__main__":
    main()
