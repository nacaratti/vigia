# Vigia: monitoramento das câmeras

Detecta pessoas nas câmeras de um DVR/NVR com RTSP (testado com Intelbras MHDX, compatível com Dahua), grava vídeo do evento e manda alerta com foto pelo Telegram. Roda num PC Windows da mesma rede local e tem um painel web em **http://127.0.0.1:8000**.

```
PC com o Vigia ── rede local ── DVR (RTSP, porta 554) ── câmeras
      └── internet (só saída) ── Telegram
```

Tudo roda localmente: nenhuma imagem sai de casa, exceto as fotos de alerta enviadas ao seu Telegram.

## Instalação (uma vez)

```powershell
cd C:\sistema-monitoramento
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # depois edite o .env
```

No `.env` ficam **todas** as credenciais (o `config.yaml` não guarda nenhuma):

| Variável | O que é |
|---|---|
| `CAM_USER` / `CAM_PASSWORD` | Usuário e senha do DVR. O ideal é criar no DVR um usuário só de visualização para este sistema. |
| `TELEGRAM_BOT_TOKEN` | No Telegram, fale com **@BotFather** → `/newbot` → copie o token. |
| `TELEGRAM_CHAT_ID` | Mande um "oi" para o seu bot e rode `python scripts\telegram_chat_id.py`. Para várias pessoas, separe por vírgula (`111,222`); cada uma precisa mandar "oi" ao bot antes. |

> ⚠️ O DVR bloqueia o usuário por cerca de 30 min depois de algumas senhas erradas. O sistema confere a senha **uma vez** ao iniciar: se ela for recusada, não insiste e mostra o aviso no painel.

## Testar

```powershell
# 1. Conexão com o DVR: descobre os canais com vídeo e salva fotos em snapshots\teste\
.\.venv\Scripts\python.exe scripts\testar_rtsp.py --canais 8

# 2. Pipeline completo sem câmera (vídeo gerado com pessoas de exemplo)
.\.venv\Scripts\python.exe scripts\gerar_video_teste.py
.\.venv\Scripts\python.exe main.py --video data\video_teste.mp4 --sem-alerta --abrir
```

No modo teste, os horários são ignorados e os eventos vão para `data\eventos_teste.db`. Tire o `--sem-alerta` para receber a foto no Telegram.

## Rodar

O jeito mais fácil é o atalho **Vigia** na área de trabalho. Ele inicia o programa em segundo plano, sem janela, e abre o painel. Se o programa já estiver rodando, só abre o painel. Para recriar o atalho, aponte-o para `.venv\Scripts\pythonw.exe "scripts\abrir_vigia.pyw"` com o ícone `assets\vigia.ico`.

Pelo terminal (vê o log na tela):

```powershell
.\.venv\Scripts\python.exe main.py --abrir
.\.venv\Scripts\python.exe main.py --ignorar-horario   # vigia o tempo todo, para testes
```

## Painel (http://127.0.0.1:8000)

- **Ao vivo**: as câmeras em grade. As caixas vermelhas são pessoas dentro da zona; as cinzas estão fora da zona e são ignoradas; as azuis são cachorros e gatos (nunca alertam). "Testar detecção" roda o detector na hora, mesmo fora do horário.
- **Eventos**: histórico com foto e vídeo, filtro por câmera e data, e o espaço ocupado pelos vídeos. Ao abrir um evento, você pode apagar só o **vídeo** (a foto e o registro ficam) ou o **evento** inteiro. Com **Selecionar** dá para apagar vários de uma vez. Sempre pede confirmação e não dá para desfazer.
- **Configuração**: nome e canal de cada câmera, horários, zonas (desenhadas clicando na imagem) e parâmetros de detecção. As mudanças valem na hora, sem reiniciar.
- **Status**: conexão, FPS, CPU, Telegram (com botão de teste) e o log.
- **Armado/Desarmado** (botão no canto superior direito): **armado detecta na hora, a qualquer horário**; desarmado não gera nada (eventos, gravações, alertas). Cada câmera também tem "Desarmar câmera" no rodapé da imagem (passe o mouse), que fica salvo na configuração.

O painel só aceita acesso deste PC. Para ver de fora de casa, use **Tailscale** (`python main.py --host <IP-100.x-do-Tailscale>`). **Nunca** faça redirecionamento de portas no roteador.

## Configuração (`config.yaml`)

Dá para editar pelo painel ou direto no arquivo (reinicie depois de editar o arquivo).

- **Horário automático** (`janelas`): no início do horário o sistema **arma sozinho** e no fim **desarma sozinho**. O botão manual vale a qualquer momento, até a próxima virada de horário (ex.: desarmar às 23:00 ao chegar em casa; às 06:00 ele já estaria desarmado de qualquer forma). Ao iniciar o programa, ele começa armado se estiver dentro do horário. `dias` aceita `seg ter qua qui sex sab dom`; janela que cruza a meia-noite pertence ao dia em que começa; `inicio == fim` = dia inteiro.
- **Zonas**: polígonos com coordenadas de 0 a 1. Uma pessoa conta se os pés (a base da caixa) estiverem dentro de alguma zona.
- **Anti-falso-positivo**: só alerta se houver pessoa em `k` dos últimos `n` frames analisados.
- **Intervalo entre alertas** (`cooldown_s`): no máximo 1 alerta por câmera nesse intervalo.
- **Modelo e resolução**: `yolo11s.pt` com `imgsz: 960` reconhece bem melhor pessoas distantes no infravermelho do que `yolo11n.pt` a 640. Na cena do quintal a confiança subiu de 37% para 68%.
- **Filtro de movimento** (`filtro_movimento`, `movimento_min`): o YOLO só roda quando pelo menos `movimento_min`% da imagem muda. Mesmo sem movimento, analisa a cada 15 s, e continua analisando enquanto houver pessoa recente. Isso reduziu a CPU de ~57% para ~13% com 4 câmeras. Para calibrar, veja a coluna "Movimento" na aba Status: com a cena parada, o valor deve ficar abaixo do limite.
- **Filtro de animais** (`filtro_animal`, `limite_animal`): cada "pessoa" detectada passa por um classificador (ImageNet) que estima a chance de ser um animal; acima do limite (padrão 0,6) ela é descartada e aparece no painel como "animal". Em testes com vídeos reais, cachorros ficaram entre 0,70 e 0,96 e pessoas entre 0,00 e 0,07 (mediana).
- **Pets**: cachorros e gatos nunca geram alerta. O detector também os reconhece (caixa azul no painel) e descarta uma "pessoa" que esteja na mesma posição de um pet com confiança parecida, o engano típico de um cachorro em pé no infravermelho. A coluna "Pets confundidos" na aba Status conta esses descartes.
- **Threads do detector**: variável `DETECCAO_THREADS` no `.env` (padrão 4).
- **Câmera nova**: use "+ Adicionar câmera" no painel e informe o canal do DVR. Para descobrir os canais com vídeo, rode `scripts\testar_rtsp.py`.
- **Arquivo pessoal**: o `config.yaml` (com os nomes e zonas das suas câmeras) é criado a partir do `config.example.yaml` na primeira execução e **não vai para o git**.

## Iniciar com o Windows

```powershell
powershell -ExecutionPolicy Bypass -File scripts\instalar_inicializacao.ps1
```

Isso cria a tarefa "Vigia - Monitoramento", que inicia ao entrar no Windows e reinicia se o programa cair. Para rodar mesmo **sem ninguém logado**, abra o Agendador de Tarefas, edite a tarefa e marque "Executar estando o usuário conectado ou não" (o Windows pede sua senha nessa tela).

**Suspensão do PC:** enquanto o Vigia está aberto, ele pede ao Windows para **não suspender**. A tela continua desligando normalmente, e o plano de energia não é alterado; ao fechar o Vigia, o PC volta a suspender como antes. Se o PC suspender por outro motivo (fechar a tampa do notebook, botão de energia), a detecção para até ele acordar, mas o DVR continua gravando no HD dele. Para desligar esse comportamento, use `main.py --permitir-suspensao`.

## Consultar eventos pelo terminal

```powershell
.\.venv\Scripts\python.exe scripts\ver_eventos.py -n 30
.\.venv\Scripts\python.exe scripts\ver_eventos.py --camera cam1 --data 2026-10-09
```

## Arquivos

| Caminho | Conteúdo |
|---|---|
| `main.py` | Ponto de entrada (monitoramento + painel) |
| `monitor/` | Captura, detecção, horários, eventos, Telegram e API |
| `web/` | Painel (HTML/CSS/JS, sem dependências externas) |
| `data/eventos.db` | Eventos (SQLite) |
| `snapshots/eventos/AAAA-MM-DD/` | Fotos dos alertas, com as caixas desenhadas |
| `logs/monitor.log` | Log com rotação (5 × 5 MB); senhas e tokens aparecem como `****` |
| `models/` | Modelo YOLO (baixado automaticamente) |

## Segurança

O projeto foi feito para ficar **dentro da rede de casa**. Conhecer o código não dá acesso ao seu sistema; para isso alguém precisaria estar na sua rede **e** ter as suas senhas.

- **Nunca faça redirecionamento de portas** no roteador (nem para o painel, nem para o DVR). Para acesso de fora, use uma VPN como o **Tailscale** e rode `main.py --host <IP do Tailscale>`.
- **Painel**: por padrão só aceita conexões do próprio PC (`127.0.0.1`). Para abri-lo a outros dispositivos é obrigatório definir `PAINEL_SENHA` no `.env` (mínimo 10 caracteres); o navegador pede a senha.
- **Proteções do painel**: verificação do cabeçalho `Host` (contra DNS rebinding), cabeçalho próprio exigido em toda escrita (contra CSRF), arquivos servidos e apagados só dentro de `snapshots/`, e o endereço do DVR na URL RTSP é sempre o `CAM_HOST` do `.env` (a configuração não consegue mandar a senha do DVR para outro servidor).
- **Segredos só no `.env`**, que o `.gitignore` exclui; senhas e tokens aparecem como `****` nos logs. Se uma chave vazar, gere outra (no Telegram: `/revoke` no @BotFather).
- **DVR**: crie um usuário só de visualização para o Vigia em vez de usar o `admin`. Errar a senha várias vezes bloqueia o usuário no DVR; o Vigia testa a senha uma única vez ao iniciar e não insiste.
- **Telegram**: o bot só envia mensagens; ele não aceita comandos, então não é uma porta de entrada.
- **Ficam fora do git** (ver `.gitignore`): `.env`, `config.yaml`, `snapshots/` (fotos e vídeos), `data/` (eventos), `logs/`, `models/`.


## Licença

MIT. Veja [LICENSE](LICENSE).
