"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const NOMES_DIAS = { seg: "Seg", ter: "Ter", qua: "Qua", qui: "Qui", sex: "Sex", sab: "Sáb", dom: "Dom" };

const estado = {
  aba: "vivo",
  config: null,        // config salva no servidor
  rascunho: null,      // config em edição
  dias: [],
  modoTeste: false,
  status: null,
  camSel: null,
  zonaNova: [],        // pontos do polígono em desenho
  eventosOffset: 0,
  eventosCarregados: [],   // itens já exibidos na galeria
  selecionando: false,
  selecionados: new Set(),
  eventoAberto: null,      // evento mostrado no visualizador
  sons: [],                // opções de som da sirene
  temPersonalizado: false, // o usuário já enviou um arquivo próprio
};

// ---------- utilidades ----------

function el(tag, attrs = {}, ...filhos) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k.startsWith("aria-")) e.setAttribute(k, String(v));
    else if (v === true) e.setAttribute(k, "");
    else if (v !== false && v != null) e.setAttribute(k, v);
  }
  for (const f of filhos.flat()) if (f != null) e.append(f instanceof Node ? f : document.createTextNode(f));
  return e;
}

async function api(caminho, opcoes = {}) {
  const o = { ...opcoes, headers: { ...(opcoes.headers || {}) } };
  if (o.method && o.method !== "GET") o.headers["X-Painel"] = "1";
  if (o.json !== undefined) {
    o.body = JSON.stringify(o.json);
    o.headers["Content-Type"] = "application/json";
    delete o.json;
  }
  const r = await fetch(caminho, o);
  if (!r.ok) {
    let msg = `${r.status}`;
    try { const j = await r.json(); msg = j.detail || j.erro || msg; } catch {}
    throw new Error(msg);
  }
  return r;
}

let toastTimer;
function toast(msg, erro = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast visivel" + (erro ? " erro" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 3500);
}

const fmtData = (iso) => new Date(iso).toLocaleString("pt-BR", { dateStyle: "short", timeStyle: "medium" });
const fmtDuracao = (s) => {
  const d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}min` : `${m}min`;
};
const clone = (o) => JSON.parse(JSON.stringify(o));

function resumoJanelas(janelas) {
  if (!janelas.length) return "sem horário";
  return janelas.map((j) => {
    const dias = j.dias.length === 7 ? "todo dia" : j.dias.map((d) => NOMES_DIAS[d]).join(" ");
    const hr = j.inicio === j.fim ? "24h" : `${j.inicio}–${j.fim}`;
    return `${dias} ${hr}`;
  }).join(" · ");
}

// ---------- abas ----------

function trocarAba(aba) {
  estado.aba = aba;
  $$(".abas button").forEach((b) => b.setAttribute("aria-selected", b.dataset.aba === aba));
  $$(".aba").forEach((s) => s.classList.toggle("ativa", s.id === `aba-${aba}`));
  ligarStreams(aba === "vivo");
  if (aba === "eventos") carregarEventos(true);
  if (aba === "config") renderConfig();
  if (aba === "status") { renderStatus(); carregarLogs(); }
  try { localStorage.setItem("aba", aba); } catch {}
}

// ---------- ao vivo ----------

function montarGrade() {
  for (const s of streams.values()) s.ativo = false;
  streams.clear();
  const grade = $("#grade");
  grade.replaceChildren();
  grade.classList.remove("foco");
  for (const cam of estado.config.cameras) {
    const tile = el("div", { class: "tile", "data-cam": cam.id, tabindex: "0" },
      el("img", { alt: `Imagem ao vivo de ${cam.nome}`, hidden: true }),
      el("div", { class: "vazio" }, "Conectando…"),
      el("div", { class: "tile-topo" },
        el("span", { class: "tile-nome" }, cam.nome),
        el("span", { class: "pilula conexao" }, "…"),
        el("span", { class: "espaco" }),
        el("span", { class: "pilula pessoas", hidden: true }),
        el("span", { class: "pilula rec", hidden: true }, "● REC"),
        el("span", { class: "pilula janela" }, "…"),
      ),
      el("div", { class: "tile-base" },
        el("span", { class: "sutil horario" }, resumoJanelas(cam.janelas)),
        el("span", { class: "espaco" }),
        el("button", { class: "btn pequeno armar-cam", onclick: (e) => { e.stopPropagation(); alternarCamera(cam.id); } }, "…"),
        el("button", { class: "btn pequeno", onclick: (e) => { e.stopPropagation(); testarDeteccao(cam); } }, "Testar detecção"),
        el("button", { class: "btn pequeno", onclick: (e) => { e.stopPropagation(); focar(tile); } }, "Ampliar"),
      ),
    );
    tile.addEventListener("dblclick", () => focar(tile));
    grade.append(tile);
  }
  if (!estado.config.cameras.length) grade.append(el("div", { class: "vazio-grande" }, "Nenhuma câmera configurada."));
  ligarStreams(estado.aba === "vivo");
}

function focar(tile) {
  const grade = $("#grade");
  const ja = tile.classList.contains("focada");
  $$(".tile", grade).forEach((t) => t.classList.remove("focada"));
  grade.classList.toggle("foco", !ja);
  if (!ja) tile.classList.add("focada");
  $$(".tile .btn:last-child", grade).forEach((b) => (b.textContent = grade.classList.contains("foco") ? "Voltar à grade" : "Ampliar"));
}

// Ao vivo por requisições curtas (uma imagem a cada ~0,2 s por câmera). Um stream
// aberto por câmera esgotava o limite de ~6 conexões do navegador com 2 abas abertas.
const streams = new Map();

function ligarStreams(ligar) {
  $$("#grade .tile").forEach((tile) => {
    const id = tile.dataset.cam;
    if (ligar && !streams.has(id)) {
      const s = { ativo: true };
      streams.set(id, s);
      atualizarAoVivo(tile, id, s);
    }
    if (!ligar && streams.has(id)) { streams.get(id).ativo = false; streams.delete(id); }
  });
}

async function atualizarAoVivo(tile, id, s) {
  const img = $("img", tile), vazio = $(".vazio", tile);
  let anterior = null, falhas = 0;
  while (s.ativo && tile.isConnected) {
    const t0 = performance.now();
    if (!document.hidden) {  // aba em segundo plano: não baixa nada
      try {
        const r = await fetch(`/api/cameras/${id}/snapshot?anotado=1&largura=960`, { cache: "no-store" });
        if (!r.ok) throw new Error(r.status);
        const url = URL.createObjectURL(await r.blob());
        img.src = url;
        if (anterior) URL.revokeObjectURL(anterior);
        anterior = url;
        falhas = 0;
        img.hidden = false; vazio.hidden = true;
      } catch {
        if (++falhas >= 3 || !anterior) { img.hidden = true; vazio.hidden = false; }  // falha isolada: mantém a última imagem
      }
    }
    await new Promise((ok) => setTimeout(ok, Math.max(50, 200 - (performance.now() - t0))));
  }
  if (anterior) URL.revokeObjectURL(anterior);
}

function atualizarTiles() {
  const st = estado.status;
  if (!st) return;
  for (const c of st.cameras) {
    const tile = $(`#grade .tile[data-cam="${c.id}"]`);
    if (!tile) continue;
    const con = $(".conexao", tile);
    const online = c.conectado && c.idade_frame_s != null && c.idade_frame_s < 10;
    con.textContent = online ? `ao vivo · ${c.fps} fps` : "offline";
    con.className = "pilula conexao " + (online ? "ok" : "off");
    if (!online) $(".vazio", tile).textContent = c.erro ? `Sem sinal: ${c.erro}` : "Conectando…";

    const jan = $(".janela", tile);
    if (!st.armado) { jan.textContent = "desarmado"; jan.className = "pilula janela ocioso"; }
    else if (!c.ativo) { jan.textContent = "câmera desarmada"; jan.className = "pilula janela ocioso"; }
    else if (c.em_janela) { jan.textContent = "vigiando"; jan.className = "pilula janela vigiando"; }
    else { jan.textContent = "desarmado"; jan.className = "pilula janela ocioso"; }

    const p = $(".pessoas", tile);
    p.hidden = !c.pessoas_agora;
    p.textContent = `${c.pessoas_agora} pessoa${c.pessoas_agora > 1 ? "s" : ""}`;
    p.className = "pilula pessoas pessoa";
    tile.classList.toggle("alerta", c.pessoas_agora > 0 && c.em_janela);
    $(".rec", tile).hidden = !c.gravando;
    tile.classList.toggle("inativa", !c.ativo);
    $(".armar-cam", tile).textContent = c.ativo ? "Desarmar câmera" : "Armar câmera";
  }
}

async function alternarArmado() {
  const novo = !estado.status?.armado;
  try {
    await api("/api/armar", { method: "POST", json: { armado: novo } });
    toast(novo ? "Sistema armado." : "Sistema desarmado: nenhum evento ou alerta será gerado.");
  } catch (e) { toast(e.message, true); }
  atualizarStatus();
}

async function alternarCamera(id) {
  const c = estado.status?.cameras.find((x) => x.id === id);
  if (!c) return;
  if (sujo()) return toast("Salve ou descarte as alterações da aba Configuração antes.", true);
  try {
    const r = await api(`/api/cameras/${id}/ativo`, { method: "POST", json: { ativo: !c.ativo } });
    aplicarConfig((await r.json()).config);
    toast(`${c.nome} ${c.ativo ? "desarmada" : "armada"}.`);
  } catch (e) { toast(e.message, true); }
  atualizarStatus();
}

async function testarDeteccao(cam) {
  try {
    const r = await api(`/api/cameras/${cam.id}/testar`, { method: "POST" });
    const dentro = +r.headers.get("X-Pessoas-Zona"), fora = +r.headers.get("X-Pessoas-Fora");
    const url = URL.createObjectURL(await r.blob());
    abrirVisor(url, `${cam.nome} · teste agora: ${dentro} pessoa(s) na zona` + (fora ? `, ${fora} fora da zona (cinza)` : ""));
  } catch (e) {
    toast(`Falha no teste: ${e.message}`, true);
  }
}

function abrirVisor(src, legenda, video = null, evento = null) {
  estado.eventoAberto = evento;
  $("#visor-acoes").hidden = !evento;
  $("#visor-apagar-video").hidden = !evento?.video;
  const img = $("#visor-img"), vid = $("#visor-video");
  img.hidden = !!video;
  vid.hidden = !video;
  if (video) { vid.poster = src; vid.src = video; vid.play().catch(() => {}); }
  else { vid.removeAttribute("src"); vid.load(); img.src = src; }
  $("#visor-legenda").textContent = legenda;
  $("#visor").showModal();
}

// ---------- eventos ----------

async function carregarEventos(reiniciar) {
  if (reiniciar) estado.eventosOffset = 0;
  const p = new URLSearchParams({ limite: 48, offset: estado.eventosOffset });
  if ($("#f-camera").value) p.set("camera", $("#f-camera").value);
  if ($("#f-data").value) p.set("data", $("#f-data").value);
  let dados;
  try { dados = await (await api(`/api/eventos?${p}`)).json(); }
  catch (e) { return toast(`Erro ao carregar eventos: ${e.message}`, true); }

  const lista = $("#lista-eventos");
  if (reiniciar) { lista.replaceChildren(); estado.eventosCarregados = []; }
  for (const ev of dados.itens) {
    estado.eventosCarregados.push(ev);
    const statusAlerta = { enviado: "enviado", falhou: "falhou", pendente: "enviando", desativado: "sem alerta", intervalo: "sem msg (intervalo)" }[ev.alerta] || ev.alerta;
    const card = el("button", {
      class: "evento" + (estado.selecionados.has(ev.id) ? " sel" : ""),
      "data-id": ev.id,
      onclick: () => {
        if (estado.selecionando) return alternarSelecao(ev.id, card);
        abrirVisor(`/api/eventos/${ev.id}/imagem`,
          `${ev.camera_nome} · ${fmtData(ev.ts)} · ${ev.pessoas} pessoa(s) · confiança ${Math.round(ev.confianca * 100)}% · alerta: ${statusAlerta}`,
          ev.video ? `/api/eventos/${ev.id}/video` : null, ev);
      },
    },
      el("img", { src: `/api/eventos/${ev.id}/imagem`, alt: `Evento em ${ev.camera_nome}`, loading: "lazy" }),
      el("div", { class: "evento-info" },
        el("div", { class: "evento-linha" },
          el("span", { class: "evento-cam" }, ev.camera_nome),
          el("span", {}, ev.video ? el("span", { class: "chip video" }, "▶ vídeo") : null, " ",
            el("span", { class: `chip ${ev.alerta}` }, statusAlerta))),
        el("div", { class: "evento-linha sutil" },
          el("span", { class: "mono" }, fmtData(ev.ts)),
          el("span", {}, `${ev.pessoas} pessoa${ev.pessoas > 1 ? "s" : ""} · ${Math.round(ev.confianca * 100)}%`)),
      ),
    );
    lista.append(card);
  }
  estado.eventosOffset += dados.itens.length;
  if (!lista.children.length) lista.append(el("div", { class: "vazio-grande" }, "Nenhum evento registrado com esses filtros."));
  $("#ev-total").textContent = `${dados.total} evento${dados.total === 1 ? "" : "s"}`;
  $("#ev-mais").hidden = estado.eventosOffset >= dados.total;
  atualizarUso();
}

const fmtMB = (b) => !b ? "0 MB" : b >= 2 ** 30 ? `${(b / 2 ** 30).toFixed(1)} GB` : `${Math.max(0.1, b / 2 ** 20).toFixed(1)} MB`;

async function atualizarUso() {
  try {
    const u = await (await api("/api/armazenamento")).json();
    $("#ev-uso").textContent = `· ${u.videos} vídeo${u.videos === 1 ? "" : "s"} ocupando ${fmtMB(u.bytes_videos)}`;
  } catch {}
}

function modoSelecao(ligar) {
  estado.selecionando = ligar;
  estado.selecionados.clear();
  $("#lista-eventos").classList.toggle("selecionando", ligar);
  $$("#lista-eventos .evento.sel").forEach((c) => c.classList.remove("sel"));
  $("#barra-selecao").hidden = !ligar;
  $("#ev-selecionar").hidden = ligar;
  atualizarBarraSelecao();
}

function alternarSelecao(id, card) {
  if (estado.selecionados.has(id)) estado.selecionados.delete(id); else estado.selecionados.add(id);
  card.classList.toggle("sel", estado.selecionados.has(id));
  atualizarBarraSelecao();
}

function atualizarBarraSelecao() {
  const ids = [...estado.selecionados];
  const comVideo = estado.eventosCarregados.filter((e) => estado.selecionados.has(e.id) && e.video).length;
  $("#sel-contagem").textContent = `${ids.length} selecionado${ids.length === 1 ? "" : "s"}`;
  $("#sel-apagar-videos").disabled = !comVideo;
  $("#sel-apagar-videos").textContent = comVideo ? `Apagar ${comVideo} vídeo${comVideo === 1 ? "" : "s"}` : "Apagar vídeos";
  $("#sel-apagar-eventos").disabled = !ids.length;
}

async function apagar(ids, tipo) {
  const evs = estado.eventosCarregados.filter((e) => ids.includes(e.id));
  const n = tipo === "video" ? evs.filter((e) => e.video).length : ids.length;
  if (!n) return false;
  const msg = tipo === "video"
    ? `Apagar ${n} vídeo${n === 1 ? "" : "s"}? A foto e o registro do evento continuam.\n\nNão é possível desfazer.`
    : `Apagar ${n} evento${n === 1 ? "" : "s"} (foto, vídeo e registro)?\n\nNão é possível desfazer.`;
  if (!confirm(msg)) return false;
  try {
    const r = await (await api("/api/eventos/apagar", { method: "POST", json: { ids, tipo } })).json();
    toast(`${r.apagados} ${tipo === "video" ? "vídeo(s)" : "evento(s)"} apagado(s) · ${fmtMB(r.bytes)} liberados.`);
  } catch (e) {
    toast(`Erro ao apagar: ${e.message}`, true);
    return false;
  }
  await carregarEventos(true);
  return true;
}

function preencherFiltroCameras() {
  const sel = $("#f-camera");
  const atual = sel.value;
  sel.replaceChildren(el("option", { value: "" }, "Todas"),
    ...estado.config.cameras.map((c) => el("option", { value: c.id }, c.nome)));
  sel.value = atual;
}

// ---------- configuração ----------

const sujo = () => JSON.stringify(estado.rascunho) !== JSON.stringify(estado.config);

function marcarAlteracao() {
  $("#barra-salvar").hidden = !sujo();
  $("#cfg-erro").textContent = "";
}

function renderConfig() {
  if (!estado.rascunho) return;
  const ro = estado.modoTeste;
  $("#aviso-teste-config").hidden = !ro;
  const cams = estado.rascunho.cameras;
  if (!cams.find((c) => c.id === estado.camSel)) estado.camSel = cams[0]?.id ?? null;

  $("#cfg-cams").replaceChildren(...cams.map((c) =>
    el("button", { class: "item-cam" + (c.id === estado.camSel ? " sel" : ""), onclick: () => { estado.camSel = c.id; estado.zonaNova = []; renderConfig(); } },
      el("span", { class: "ponto" + (c.ativo ? "" : " inativo") }), c.nome)));
  $("#cfg-add-cam").hidden = ro;
  $("#cfg-buscar").hidden = ro;
  $("#cfg-assistente").hidden = ro;

  renderCamera(cams.find((c) => c.id === estado.camSel), ro);
  renderDeteccao(ro);
  marcarAlteracao();
}

function campoNumero(rotulo, valor, onchange, { min, max, step = 1, dica } = {}) {
  return el("label", {}, rotulo,
    el("input", { type: "number", value: valor, min, max, step, onchange: (e) => onchange(e.target.value === "" ? null : +e.target.value) }),
    dica ? el("span", { class: "dica" }, dica) : null);
}

function renderCamera(cam, ro) {
  const box = $("#cfg-camera");
  if (!cam) { box.replaceChildren(el("p", { class: "sutil" }, "Nenhuma câmera.")); return; }
  const alterar = (fn) => { fn(); marcarAlteracao(); };

  const cabecalho = el("div", { class: "campos" },
    el("label", {}, "Nome", el("input", { value: cam.nome, disabled: ro, oninput: (e) => alterar(() => (cam.nome = e.target.value)) })),
    cam.fonte != null
      ? el("label", {}, "Fonte", el("input", { value: cam.fonte, disabled: true, size: 40 }))
      : el("label", {}, "Canal do DVR", el("input", { type: "number", min: 0, max: 64, value: cam.canal, disabled: ro, onchange: (e) => alterar(() => (cam.canal = +e.target.value)) })),
    el("label", { class: "toggle", style: "align-self:end;padding-bottom:6px" },
      el("input", { type: "checkbox", checked: cam.ativo, disabled: ro, onchange: (e) => alterar(() => { cam.ativo = e.target.checked; renderConfig(); }) }),
      "Monitorar esta câmera"),
  );

  // janelas de horário
  const janelas = el("div", {});
  cam.janelas.forEach((j, i) => {
    const cruza = j.inicio > j.fim;
    janelas.append(el("div", { class: "janela" },
      el("div", { class: "dias" }, estado.dias.map((d) => el("button", {
        class: "dia", type: "button", "aria-pressed": j.dias.includes(d), disabled: ro,
        onclick: () => alterar(() => {
          j.dias = j.dias.includes(d) ? j.dias.filter((x) => x !== d) : estado.dias.filter((x) => x === d || j.dias.includes(x));
          renderConfig();
        }),
      }, NOMES_DIAS[d]))),
      el("label", {}, "Início", el("input", { type: "time", value: j.inicio, disabled: ro, onchange: (e) => alterar(() => { j.inicio = e.target.value; renderConfig(); }) })),
      el("label", {}, "Fim", el("input", { type: "time", value: j.fim, disabled: ro, onchange: (e) => alterar(() => { j.fim = e.target.value; renderConfig(); }) })),
      el("span", { class: "cruza" }, j.inicio === j.fim ? "dia inteiro" : cruza ? "termina no dia seguinte" : ""),
      el("span", { class: "espaco" }),
      ro ? null : el("button", { class: "btn pequeno perigo", onclick: () => alterar(() => { cam.janelas.splice(i, 1); renderConfig(); }) }, "Remover"),
    ));
  });
  if (!cam.janelas.length) janelas.append(el("p", { class: "dica" }, "Sem horário: esta câmera só vigia quando você armar pelo botão."));

  // zonas
  const img = el("img", { alt: `Imagem atual de ${cam.nome}`, src: `/api/cameras/${cam.id}/snapshot?t=${Date.now()}` });
  const canvas = el("canvas");
  const editor = el("div", { class: "editor-zona" }, img, canvas);
  const desenharZonas = () => {
    const w = (canvas.width = img.clientWidth), h = (canvas.height = img.clientHeight);
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, w, h);
    const poli = (pts, fechado, cor) => {
      if (!pts.length) return;
      ctx.beginPath();
      pts.forEach(([x, y], k) => (k ? ctx.lineTo(x * w, y * h) : ctx.moveTo(x * w, y * h)));
      if (fechado) { ctx.closePath(); ctx.fillStyle = cor + "33"; ctx.fill(); }
      ctx.strokeStyle = cor; ctx.lineWidth = 2; ctx.stroke();
      ctx.fillStyle = cor;
      pts.forEach(([x, y]) => { ctx.beginPath(); ctx.arc(x * w, y * h, 4, 0, 7); ctx.fill(); });
    };
    cam.zonas.forEach((z) => poli(z, true, "#f2a93b"));
    poli(estado.zonaNova, false, "#5aa9ff");
  };
  img.addEventListener("load", desenharZonas);
  img.addEventListener("error", () => editor.replaceChildren(el("p", { class: "dica", style: "padding:20px;line-height:1.4" }, "Sem imagem desta câmera ainda. As zonas podem ser desenhadas quando ela estiver conectada.")));
  new ResizeObserver(desenharZonas).observe(img);
  if (!ro) canvas.addEventListener("click", (e) => {
    const r = canvas.getBoundingClientRect();
    estado.zonaNova.push([+((e.clientX - r.left) / r.width).toFixed(4), +((e.clientY - r.top) / r.height).toFixed(4)]);
    desenharZonas();
    atualizarBotoesZona();
  });

  const btnFechar = el("button", { class: "btn pequeno primario", onclick: () => alterar(() => { cam.zonas.push(estado.zonaNova); estado.zonaNova = []; renderConfig(); }) }, "Concluir zona");
  const btnDesfazer = el("button", { class: "btn pequeno", onclick: () => { estado.zonaNova.pop(); desenharZonas(); atualizarBotoesZona(); } }, "Desfazer ponto");
  const atualizarBotoesZona = () => { btnFechar.disabled = estado.zonaNova.length < 3; btnDesfazer.disabled = !estado.zonaNova.length; };
  atualizarBotoesZona();

  const barraZona = el("div", { class: "barra-zona" },
    ro ? null : [btnFechar, btnDesfazer],
    el("span", { class: "espaco" }),
    el("span", { class: "sutil" }, cam.zonas.length ? `${cam.zonas.length} zona(s)` : "sem zonas: a imagem toda conta"),
    ro || !cam.zonas.length ? null : el("button", { class: "btn pequeno perigo", onclick: () => alterar(() => { cam.zonas.pop(); renderConfig(); }) }, "Remover última zona"),
  );

  box.replaceChildren(...[
    el("h2", {}, cam.nome || cam.id, el("span", { class: "sutil mono" }, `  · ${cam.id}`)),
    cabecalho,
    el("h3", {}, "Armar automaticamente"),
    el("p", { class: "dica" }, "No início do horário o sistema arma sozinho e no fim desarma. Fora dele, você arma e desarma pelo botão do topo quando quiser."),
    janelas,
    ro ? null : el("button", { class: "btn pequeno", onclick: () => alterar(() => { cam.janelas.push({ dias: [...estado.dias], inicio: "22:00", fim: "06:00" }); renderConfig(); }) }, "+ Adicionar janela"),
    el("h3", {}, "Zonas de interesse"),
    el("p", { class: "dica" }, "Clique na imagem para marcar os cantos da área que importa (ex.: quintal, portão) e depois em “Concluir zona”. Só contam pessoas cujos pés estejam dentro de alguma zona; quem passa na calçada fica de fora."),
    barraZona,
    editor,
    ro ? null : el("div", { style: "margin-top:18px" },
      el("button", { class: "btn pequeno perigo", onclick: () => {
        if (!confirm(`Remover a câmera "${cam.nome}" da configuração? (os eventos já registrados continuam salvos)`)) return;
        alterar(() => { estado.rascunho.cameras = estado.rascunho.cameras.filter((c) => c !== cam); renderConfig(); });
      } }, "Remover câmera")),
  ].filter(Boolean));
}

function renderDeteccao(ro) {
  const d = estado.rascunho.deteccao;
  const set = (k) => (v) => { d[k] = v; marcarAlteracao(); };
  const campos = [
    campoNumero("Confiança mínima", d.confianca, set("confianca"), { min: 0.05, max: 0.99, step: 0.05, dica: "0,5 é um bom começo. Suba se houver alarmes falsos." }),
    campoNumero("Frames analisados/s", d.fps_analise, set("fps_analise"), { min: 0.2, max: 10, step: 0.5, dica: "Por câmera. 1–2 roda bem em CPU." }),
    campoNumero("K (frames com pessoa)", d.k, set("k"), { min: 1, max: 30 }),
    campoNumero("N (janela de frames)", d.n, set("n"), { min: 1, max: 30, dica: "Dispara se houver pessoa em K dos últimos N frames." }),
    campoNumero("Novo evento após ausência de (s)", d.ausencia_s, set("ausencia_s"), { min: 3, max: 3600, step: 5, dica: "Se a pessoa sumir por esse tempo e voltar, conta como um evento novo." }),
    campoNumero("Intervalo entre mensagens (s)", d.cooldown_s, set("cooldown_s"), { min: 0, max: 86400, step: 30, dica: "Por câmera. Limita só o Telegram; os eventos são todos registrados." }),
    campoNumero("Resolução da análise", d.imgsz, set("imgsz"), { min: 160, max: 1280, step: 32, dica: "640 é o padrão do YOLO." }),
    el("label", {}, "Modelo", el("input", { value: d.modelo, onchange: (e) => set("modelo")(e.target.value), size: 14 }),
      el("span", { class: "dica" }, "yolo11n (leve) · yolo11s (melhor à noite)")),
    el("label", { class: "toggle", style: "align-self:center" },
      el("input", { type: "checkbox", checked: d.filtro_movimento, onchange: (e) => set("filtro_movimento")(e.target.checked) }),
      "Só analisar quando houver movimento"),
    el("label", { class: "toggle", style: "align-self:center" },
      el("input", { type: "checkbox", checked: d.filtro_animal, onchange: (e) => set("filtro_animal")(e.target.checked) }),
      "Filtrar animais (cachorros/gatos confundidos com pessoa)"),
    campoNumero("Limite do filtro de animais", d.limite_animal, set("limite_animal"), { min: 0.1, max: 0.99, step: 0.05, dica: "Descarta a pessoa se a chance de ser animal passar disso. Menor = filtra mais (risco de ignorar pessoas)." }),
    campoNumero("Movimento mínimo (%)", d.movimento_min, set("movimento_min"), { min: 0.01, max: 50, step: 0.1, dica: "Parte da imagem que precisa mudar. Veja a coluna Movimento na aba Status para calibrar." }),
  ];
  const alertas = estado.rascunho.alertas;
  campos.unshift(el("label", { class: "toggle", style: "flex-basis:100%" },
    el("input", { type: "checkbox", checked: alertas.telegram, onchange: (e) => { alertas.telegram = e.target.checked; marcarAlteracao(); } }),
    "Enviar mensagens no Telegram"));
  $("#cfg-deteccao").replaceChildren(...campos);
  $$("#cfg-deteccao input").forEach((i) => (i.disabled = ro));

  const g = estado.rascunho.gravacao;
  const setG = (k) => (v) => { g[k] = v; marcarAlteracao(); };
  $("#cfg-gravacao").replaceChildren(
    el("label", { class: "toggle", style: "flex-basis:100%" },
      el("input", { type: "checkbox", checked: g.ativo, onchange: (e) => setG("ativo")(e.target.checked) }),
      "Gravar vídeo quando uma pessoa for detectada"),
    campoNumero("Segundos antes", g.pre_s, setG("pre_s"), { min: 0, max: 30, dica: "Pré-gravação: o vídeo começa antes da confirmação." }),
    campoNumero("Segundos depois", g.pos_s, setG("pos_s"), { min: 2, max: 120, dica: "Continua gravando após a pessoa sumir." }),
    campoNumero("Duração máxima (s)", g.max_s, setG("max_s"), { min: 10, max: 1800, step: 30 }),
  );
  $$("#cfg-gravacao input").forEach((i) => (i.disabled = ro));

  const sr = estado.rascunho.sirene;
  const setS = (k) => (v) => { sr[k] = v; marcarAlteracao(); };
  $("#cfg-sirene").replaceChildren(
    el("label", { class: "toggle", style: "flex-basis:100%" },
      el("input", { type: "checkbox", checked: sr.ativo, onchange: (e) => setS("ativo")(e.target.checked) }),
      "Tocar a sirene quando uma pessoa ficar na zona"),
    el("label", {}, "Som da sirene",
      el("select", { onchange: (e) => { pararPrevia(); setS("som")(e.target.value); } },
        estado.sons.map((s) => el("option", { value: s.id, selected: s.id === sr.som,
          disabled: s.id === "personalizado" && !estado.temPersonalizado }, s.nome)))),
    el("div", { class: "campo-botoes" },
      el("button", { class: "btn pequeno", id: "btn-previa", onclick: () => ouvirPrevia(sr.som, sr.voz) }, "▶ Ouvir aqui"),
      el("label", { class: "btn pequeno fantasma", title: "MP3, WAV, M4A ou OGG, até 15 MB e 60 s" }, "Enviar meu arquivo…",
        el("input", { type: "file", accept: "audio/*", hidden: true, onchange: (e) => enviarSom(e.target.files[0]) }))),
    el("label", { class: "toggle", style: "flex-basis:100%" },
      el("input", { type: "checkbox", checked: sr.voz, onchange: (e) => { pararPrevia(); setS("voz")(e.target.checked); } }),
      "Aviso de voz entre as sirenes (\"Atenção! Esta área é monitorada…\")"),
    campoNumero("Após quantos segundos na zona", sr.atraso_s, setS("atraso_s"), { min: 0, max: 120, dica: "0 = assim que o evento for confirmado. Alguns segundos evitam alarme falso." }),
    campoNumero("Tocar por até (s)", sr.duracao_s, setS("duracao_s"), { min: 5, max: 900, step: 5, dica: "Para sozinha depois disso. Também para ao desarmar, no botão vermelho ou pelo Telegram (/parar)." }),
    el("div", { style: "flex-basis:100%" },
      el("button", { class: "btn pequeno", onclick: testarSirene }, "🔊 Testar sirene (3 s)"),
      el("span", { class: "dica", style: "margin-left:10px" }, "O volume é o do Windows: deixe as caixas de som ligadas e altas.")),
  );
  $$("#cfg-sirene input, #cfg-sirene select").forEach((i) => (i.disabled = i.disabled || ro));
}

function adicionarCamera() {
  const cams = estado.rascunho.cameras;
  let n = cams.length + 1;
  while (cams.some((c) => c.id === `cam${n}`)) n++;
  const canais = cams.map((c) => c.canal || 0);
  cams.push({ id: `cam${n}`, nome: `Câmera ${n}`, canal: Math.max(0, ...canais) + 1, ativo: true,
    janelas: [{ dias: [...estado.dias], inicio: "22:00", fim: "06:00" }], zonas: [] });
  estado.camSel = `cam${n}`;
  renderConfig();
}

async function salvarConfig() {
  const btn = $("#cfg-salvar");
  btn.disabled = true;
  try {
    const r = await api("/api/config", { method: "PUT", json: estado.rascunho });
    aplicarConfig((await r.json()).config);
    toast("Configuração salva e aplicada.");
  } catch (e) {
    $("#cfg-erro").textContent = e.message;
  } finally {
    btn.disabled = false;
  }
}

function aplicarConfig(cfg) {
  const mudouCameras = JSON.stringify(estado.config?.cameras.map((c) => [c.id, c.nome, c.janelas])) !==
    JSON.stringify(cfg.cameras.map((c) => [c.id, c.nome, c.janelas]));
  estado.config = cfg;
  estado.rascunho = clone(cfg);
  estado.zonaNova = [];
  if (mudouCameras) { montarGrade(); preencherFiltroCameras(); }
  renderConfig();
}

// ---------- procurar câmeras ----------

const busca = { info: null, timer: null };

function passoBusca(nome) {
  for (const p of ["aparelhos", "acesso", "canais"]) $(`#busca-passo-${p}`).hidden = p !== nome;
}

async function abrirBusca() {
  if (estado.modoTeste) return toast("No modo de teste a configuração é só leitura.", true);
  if (sujo() && !confirm("Há alterações não salvas na configuração. Elas serão descartadas se você usar câmeras da busca. Continuar?")) return;
  try {
    busca.info = await (await api("/api/descoberta")).json();
  } catch (e) {
    return toast(e.message, true);
  }
  passoBusca("aparelhos");
  $("#busca").showModal();
  procurarAparelhos();
}

async function procurarAparelhos() {
  const lista = $("#busca-aparelhos");
  const btn = $("#busca-procurar");
  btn.disabled = true;
  lista.replaceChildren(el("p", { class: "busca-progresso carregando" }, "Procurando aparelhos na rede (cerca de 10 s)..."));
  try {
    const r = await (await api("/api/descoberta/aparelhos", { method: "POST", json: { rede: $("#busca-rede").value.trim() } })).json();
    const itens = r.aparelhos.map((a) => el("button", { class: "aparelho", onclick: () => escolherAparelho(a.ip) },
      el("span", { class: "ip" }, a.ip),
      el("span", { class: "nome" }, a.nome || "Aparelho com vídeo (RTSP)"),
      a.ip === busca.info.host ? el("span", { class: "selo atual" }, "configurado") : null,
      a.onvif ? el("span", { class: "selo" }, "ONVIF") : null,
      a.rtsp ? el("span", { class: "selo" }, "RTSP") : null));
    lista.replaceChildren(...(itens.length ? itens : [el("p", { class: "sutil" },
      "Nenhum aparelho encontrado. Se o gravador estiver em outra rede (atrás de outro roteador), informe o IP ou a rede abaixo.")]),
      el("p", { class: "dica" }, `Redes verificadas: ${r.redes.join(", ")}`));
  } catch (e) {
    lista.replaceChildren(el("p", { class: "erro" }, e.message));
  } finally {
    btn.disabled = false;
  }
}

function escolherAparelho(ip) {
  const atual = ip === busca.info.host;
  $("#busca-host").value = ip;
  $("#busca-porta").value = atual ? busca.info.porta : 554;
  $("#busca-usuario").value = atual ? busca.info.usuario : "";
  $("#busca-senha").value = "";
  $("#busca-senha").placeholder = atual && busca.info.tem_senha ? "(usar a senha já salva)" : "";
  passoBusca("acesso");
  (atual && busca.info.tem_senha ? $("#busca-iniciar") : $("#busca-usuario")).focus();
}

async function iniciarBuscaCanais() {
  const corpo = {
    host: $("#busca-host").value.trim(), porta: +$("#busca-porta").value || 554,
    usuario: $("#busca-usuario").value.trim(), senha: $("#busca-senha").value,
  };
  try {
    await api("/api/descoberta/canais", { method: "POST", json: corpo });
  } catch (e) {
    return toast(e.message, true);
  }
  $("#busca-senha").value = "";
  $("#busca-canais").replaceChildren();
  $("#busca-aviso").hidden = true;
  $("#busca-aplicar").disabled = true;
  passoBusca("canais");
  acompanharBusca();
}

async function acompanharBusca() {
  clearTimeout(busca.timer);
  let st;
  try {
    st = await (await api("/api/descoberta/canais")).json();
  } catch (e) {
    $("#busca-progresso").textContent = e.message;
    return;
  }
  const prog = $("#busca-progresso");
  prog.className = "busca-progresso" + (st.fim ? "" : " carregando") + (st.erro ? " erro" : "");
  prog.textContent = st.erro || (st.fim ? `${st.marca} · ${st.mensagem}` : st.mensagem || "Conectando...");
  if (!st.fim) {
    if ($("#busca").open) busca.timer = setTimeout(acompanharBusca, 700);
    return;
  }
  if (!st.erro) mostrarCanais(st);
}

function mostrarCanais(st) {
  const mesmo = st.host === busca.info.host;
  const atuais = new Map(mesmo ? estado.config.cameras.filter((c) => c.fonte == null).map((c) => [c.canal, c]) : []);
  const cartoes = st.canais.map((c) => {
    const existente = atuais.get(c.canal);
    const marcar = existente ? true : c.sinal;
    const nome = el("input", { type: "text", value: existente?.nome || `Canal ${c.canal}`, "aria-label": `Nome do canal ${c.canal}` });
    const caixa = el("input", { type: "checkbox", checked: marcar, onchange: () => { card.classList.toggle("marcado", caixa.checked); atualizarAplicar(); } });
    const card = el("div", { class: "canal" + (marcar ? " marcado" : "") + (c.sinal ? "" : " sem-sinal"), "data-canal": c.canal },
      el("img", { src: `/api/descoberta/miniatura/${c.canal}?t=${Date.now()}`, alt: `Canal ${c.canal}` }),
      el("div", { class: "canal-corpo" },
        el("label", { class: "toggle" }, caixa, `Canal ${c.canal}`,
          c.sinal ? null : el("span", { class: "selo" }, "sem sinal"),
          existente ? el("span", { class: "selo atual" }, "já configurada") : null),
        nome));
    return card;
  });
  $("#busca-canais").replaceChildren(...(cartoes.length ? cartoes : [el("p", { class: "sutil" }, "Nenhum canal com imagem.")]));
  const aviso = $("#busca-aviso");
  aviso.hidden = false;
  aviso.textContent = mesmo
    ? "Câmeras já configuradas mantêm zonas e horários. As desmarcadas saem da configuração (os eventos gravados continuam)."
    : "Este aparelho substitui o atual: as câmeras de hoje serão trocadas pelas marcadas, que começam sem zonas (imagem toda) e com o horário 22:00–06:00.";
  atualizarAplicar();
}

function atualizarAplicar() {
  const n = $$("#busca-canais .canal.marcado").length;
  $("#busca-aplicar").disabled = n === 0;
  $("#busca-aplicar").textContent = n ? `Usar ${n} câmera${n > 1 ? "s" : ""}` : "Usar estas câmeras";
}

async function aplicarBusca() {
  const cameras = $$("#busca-canais .canal.marcado").map((c) => ({ canal: +c.dataset.canal, nome: $("input[type=text]", c).value.trim() }));
  const btn = $("#busca-aplicar");
  btn.disabled = true;
  try {
    const r = await api("/api/descoberta/aplicar", { method: "POST", json: { cameras } });
    aplicarConfig((await r.json()).config);
    if (assist.voltar) assist.voltar = "telegram";  // veio do assistente: segue para o próximo passo
    $("#busca").close();
    toast(`${cameras.length} câmera(s) configurada(s). Desenhe as zonas de cada uma.`);
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
  }
}

// ---------- assistente de instalação ----------

const assist = { info: null, timer: null, voltar: null, marcados: new Set() };

async function abrirAssistente(passo = "cameras") {
  if (estado.modoTeste) return toast("No modo de teste a configuração é só leitura.", true);
  try {
    assist.info = await (await api("/api/assistente")).json();
  } catch (e) {
    return toast(e.message, true);
  }
  if (!$("#assistente").open) $("#assistente").showModal();
  passoAssistente(passo);
}

function passoAssistente(passo) {
  clearTimeout(assist.timer);
  const ordem = ["cameras", "telegram", "pronto"];
  const { gravador, telegram } = assist.info;
  const feito = { cameras: !!gravador.host && gravador.cameras > 0 && !assist.info.primeira_vez, telegram: telegram.destinatarios > 0 };
  $$("#assist-passos li").forEach((li) => {
    li.classList.toggle("atual", li.dataset.passo === passo);
    li.classList.toggle("feito", li.dataset.passo !== passo && !!feito[li.dataset.passo]);
  });
  for (const p of ordem) $(`#assist-${p}`).hidden = p !== passo;

  if (passo === "cameras") {
    const box = $("#assist-cameras-estado");
    box.className = "estado-passo" + (feito.cameras ? " ok" : "");
    box.textContent = feito.cameras
      ? `✓ ${gravador.cameras} câmera(s) do gravador ${gravador.host}. Pode procurar de novo para incluir ou trocar câmeras.`
      : "Nenhum gravador configurado ainda. Clique em Procurar câmeras: o Vigia encontra o aparelho na rede e mostra as câmeras.";
    $("#assist-procurar").classList.toggle("primario", !feito.cameras);
    $("#assist-ir-telegram").classList.toggle("primario", feito.cameras);
  }
  if (passo === "telegram") {
    $("#assist-token").value = "";
    $("#assist-token").placeholder = telegram.bot ? `deixe em branco para manter @${telegram.bot}` : "123456789:AAH...";
    $("#assist-tg-bot").hidden = false;
    $("#assist-tg-pessoas").hidden = true;
    $("#assist-pular-tg").textContent = feito.telegram ? "Próximo" : "Pular por enquanto";
    if (telegram.bot) conectarBot();  // já configurado: vai direto para a lista de pessoas
  }
}

async function conectarBot() {
  const btn = $("#assist-conectar");
  btn.disabled = true;
  try {
    const bot = await (await api("/api/assistente/telegram/bot", { method: "POST", json: { token: $("#assist-token").value.trim() } })).json();
    $("#assist-token").value = "";
    $("#assist-tg-titulo").textContent = `✓ Bot conectado: ${bot.nome} (@${bot.usuario})`;
    const link = $("#assist-tg-link");
    link.href = bot.link;
    link.textContent = `t.me/${bot.usuario}`;
    assist.marcados = new Set();
    assist.carregado = false;
    $("#assist-tg-lista").replaceChildren();
    $("#assist-tg-bot").hidden = true;
    $("#assist-tg-pessoas").hidden = false;
    atualizarContatos();
  } catch (e) {
    toast(e.message, true);
  } finally {
    btn.disabled = false;
  }
}

async function atualizarContatos() {
  clearTimeout(assist.timer);
  if (!$("#assistente").open || $("#assist-tg-pessoas").hidden) return;
  try {
    const { contatos } = await (await api("/api/assistente/telegram/contatos")).json();
    if (!assist.carregado) contatos.filter((c) => c.autorizado).forEach((c) => assist.marcados.add(c.id));
    assist.carregado = true;
    $("#assist-tg-lista").replaceChildren(...contatos.map((c) => {
      const caixa = el("input", { type: "checkbox", checked: assist.marcados.has(c.id), onchange: () => {
        caixa.checked ? assist.marcados.add(c.id) : assist.marcados.delete(c.id);
        $("#assist-salvar-tg").disabled = assist.marcados.size === 0;
      } });
      return el("label", { class: "contato" }, caixa, el("span", { class: "nome" }, c.nome),
        el("span", { class: "selo" + (c.autorizado ? " atual" : "") }, c.autorizado ? "recebe alertas" : "nova mensagem"));
    }));
    $("#assist-salvar-tg").disabled = assist.marcados.size === 0;
  } catch (e) {
    toast(e.message, true);
  }
  assist.timer = setTimeout(atualizarContatos, 2500);
}

async function salvarTelegram() {
  const btn = $("#assist-salvar-tg");
  btn.disabled = true;
  try {
    const r = await (await api("/api/assistente/telegram/salvar", { method: "POST", json: { chat_ids: [...assist.marcados] } })).json();
    aplicarConfig(r.config);
    toast(r.novos ? `Pronto! ${r.novos} pessoa(s) receberam uma mensagem de boas-vindas no Telegram.` : "Destinatários salvos.");
    assist.info = await (await api("/api/assistente")).json();
    passoAssistente("pronto");
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
  }
}

function concluirAssistente() {
  $("#assistente").close();
  trocarAba("config");
}

// ---------- status ----------

function cartao(titulo, valor, detalhe, extra) {
  return el("div", { class: "cartao" }, el("div", { class: "titulo" }, titulo), el("div", { class: "valor" }, valor),
    detalhe ? el("div", { class: "detalhe" }, detalhe) : null, extra);
}

function renderStatus() {
  const st = estado.status;
  if (!st) return;
  const tg = st.telegram;
  const btnTg = el("button", { class: "btn pequeno", onclick: testarTelegram }, "Enviar mensagem de teste");
  const btnCfgTg = estado.modoTeste ? null : el("button", { class: "btn pequeno", onclick: () => abrirAssistente("telegram") }, "Configurar");
  $("#st-cartoes").replaceChildren(
    cartao("Estado", st.armado ? "Armado" : "Desarmado",
      st.erro_credenciais ? `⚠ ${st.erro_credenciais}` : `${st.armado_origem || "—"} · desde ${st.armado_desde}`),
    cartao("CPU", `${Math.round(st.cpu)}%`, `este programa: ${st.cpu_processo}%`),
    cartao("Memória", `${st.memoria_mb} MB`, `ligado há ${fmtDuracao(st.uptime_s)}`),
    cartao("Modelo", st.modelo || "—", st.erro_modelo ? `erro: ${st.erro_modelo}` : "detecção de pessoas"),
    cartao("Telegram", !estado.config.alertas.telegram ? "Desligado" : tg.ativo ? "Ativo" : tg.configurado ? "Desligado" : "Não configurado",
      tg.erro ? `último erro: ${tg.erro}` : `${tg.enviados} alerta(s) enviado(s) nesta sessão`, el("span", { class: "acoes-cartao" }, tg.configurado ? btnTg : null, btnCfgTg)),
  );
  const cab = el("tr", {}, ["Câmera", "Conexão", "FPS", "Resolução", "Vigilância", "Movimento", "Análises (evitadas)", "Detecção K/N", "Pets confundidos", "Inferência", "Próx. mensagem", "Hoje", "Reconexões"].map((t) => el("th", {}, t)));
  const linhas = st.cameras.map((c) => el("tr", {},
    el("td", {}, c.nome),
    el("td", {}, el("span", { class: "pilula " + (c.conectado ? "ok" : "off") }, c.conectado ? "conectada" : "offline"), c.erro ? el("span", { class: "sutil" }, ` ${c.erro}`) : null),
    el("td", { class: "mono" }, c.fps),
    el("td", { class: "mono" }, c.resolucao ? c.resolucao.join("×") : "—"),
    el("td", {}, !c.ativo ? "desativada" : c.em_janela ? "vigiando" : "desarmado"),
    el("td", { class: "mono" }, c.em_janela ? `${c.movimento_pct}%` : "—"),
    el("td", { class: "mono" }, `${c.inferencias} (${c.puladas})`),
    el("td", { class: "mono" }, c.historico),
    el("td", { class: "mono" }, c.descartadas_pet),
    el("td", { class: "mono" }, c.inferencias ? `${c.ms_inferencia} ms` : "—"),
    el("td", { class: "mono" }, c.cooldown_restante_s ? `${c.cooldown_restante_s}s` : "livre"),
    el("td", { class: "mono" }, c.eventos_hoje),
    el("td", { class: "mono" }, c.reconexoes),
  ));
  $("#st-cams").replaceChildren(el("thead", {}, cab), el("tbody", {}, linhas));
}

async function carregarLogs() {
  try {
    const linhas = await (await api("/api/logs?n=200")).json();
    const pre = $("#st-logs");
    const noFim = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 30;
    pre.replaceChildren(...linhas.map((l) => el("div", { class: l.nivel },
      `${new Date(l.ts * 1000).toLocaleTimeString("pt-BR")}  ${l.nivel.padEnd(7)} ${l.origem}: ${l.msg}`)));
    if (noFim) pre.scrollTop = pre.scrollHeight;
  } catch {}
}

// Prévia no navegador (não toca nas caixas do PC, a não ser que o painel esteja aberto nele)
let previa = null;
function pararPrevia() {
  if (previa) { previa.pause(); previa = null; }
  const b = $("#btn-previa");
  if (b) b.textContent = "▶ Ouvir aqui";
}
function ouvirPrevia(som, voz) {
  if (previa) return pararPrevia();
  previa = new Audio(`/api/sirene/previa?som=${encodeURIComponent(som)}&voz=${voz ? "true" : "false"}&t=${Date.now()}`);
  previa.addEventListener("ended", pararPrevia);
  previa.play().catch((e) => { toast(`Não foi possível tocar a prévia: ${e.message}`, true); pararPrevia(); });
  $("#btn-previa").textContent = "■ Parar";
}

async function enviarSom(arquivo) {
  if (!arquivo) return;
  try {
    const r = await (await api("/api/sirene/arquivo", { method: "POST", body: arquivo, headers: { "Content-Type": "application/octet-stream" } })).json();
    estado.temPersonalizado = true;
    estado.rascunho.sirene.som = "personalizado";
    renderConfig();
    toast(`Arquivo recebido (${r.duracao_s} s). Clique em "Salvar e aplicar" para usá-lo na sirene.`);
  } catch (e) {
    toast(e.message, true);
  }
}

async function testarSirene() {
  try { await api("/api/sirene/testar", { method: "POST" }); toast("Sirene de teste: 3 segundos."); }
  catch (e) { toast(`Sirene: ${e.message}`, true); }
}

async function pararSirene() {
  try { await api("/api/sirene/parar", { method: "POST" }); toast("Sirene desligada."); } catch (e) { toast(e.message, true); }
  atualizarStatus();
}

async function testarTelegram() {
  try {
    const r = await (await api("/api/telegram/teste", { method: "POST" })).json();
    toast(r.ok ? "Mensagem de teste enviada ao Telegram." : `Telegram: ${r.mensagem}`, !r.ok);
  } catch (e) { toast(e.message, true); }
}

// ---------- ciclo ----------

async function atualizarStatus() {
  try {
    estado.status = await (await api("/api/status")).json();
  } catch { return; }
  const selo = $("#selo-teste");
  selo.hidden = !(estado.status.modo_teste || estado.status.ignorar_horario);
  selo.textContent = estado.status.modo_teste ? "MODO TESTE" : "HORÁRIO IGNORADO";
  const armado = estado.status.armado;
  const b = $("#btn-armar");
  b.setAttribute("aria-pressed", armado);
  $(".rotulo", b).textContent = armado ? "Armado" : "Desarmado";
  const st = estado.status;
  b.title = `${armado ? "Armado: detectando pessoas agora" : "Desarmado: nada é detectado"}` +
    (st.armado_origem ? ` (${st.armado_origem}, desde ${st.armado_desde}).` : ".") +
    "\nO horário de cada câmera arma e desarma sozinho; o botão vale até a próxima virada de horário.";
  atualizarTiles();
  $("#btn-parar-sirene").hidden = !estado.status.sirene?.tocando;
  if (estado.aba === "status") renderStatus();
}

async function iniciar() {
  const dados = await (await api("/api/config")).json();
  estado.dias = dados.dias;
  estado.modoTeste = dados.modo_teste;
  try {
    const sons = await (await api("/api/sirene/sons")).json();
    estado.sons = sons.sons;
    estado.temPersonalizado = sons.personalizado;
  } catch {}
  $("#selo-teste").hidden = !dados.modo_teste;
  aplicarConfig(dados.config);

  $$(".abas button").forEach((b) => b.addEventListener("click", () => trocarAba(b.dataset.aba)));
  $("#btn-armar").addEventListener("click", alternarArmado);
  $("#btn-parar-sirene").addEventListener("click", pararSirene);
  $("#f-camera").addEventListener("change", () => carregarEventos(true));
  $("#f-data").addEventListener("change", () => carregarEventos(true));
  $("#f-limpar").addEventListener("click", () => { $("#f-camera").value = ""; $("#f-data").value = ""; carregarEventos(true); });
  $("#ev-mais").addEventListener("click", () => carregarEventos(false));
  $("#visor").addEventListener("close", () => $("#visor-video").pause());
  $("#ev-selecionar").addEventListener("click", () => modoSelecao(true));
  $("#sel-cancelar").addEventListener("click", () => modoSelecao(false));
  $("#sel-todos").addEventListener("click", () => {
    $$("#lista-eventos .evento").forEach((c) => { estado.selecionados.add(+c.dataset.id); c.classList.add("sel"); });
    atualizarBarraSelecao();
  });
  $("#sel-apagar-videos").addEventListener("click", async () => { if (await apagar([...estado.selecionados], "video")) modoSelecao(false); });
  $("#sel-apagar-eventos").addEventListener("click", async () => { if (await apagar([...estado.selecionados], "evento")) modoSelecao(false); });
  $("#visor-apagar-video").addEventListener("click", async () => {
    if (await apagar([estado.eventoAberto.id], "video")) $("#visor").close();
  });
  $("#visor-apagar-evento").addEventListener("click", async () => {
    if (await apagar([estado.eventoAberto.id], "evento")) $("#visor").close();
  });
  $("#cfg-add-cam").addEventListener("click", adicionarCamera);
  $("#cfg-buscar").addEventListener("click", abrirBusca);
  $("#busca-procurar").addEventListener("click", procurarAparelhos);
  $("#busca-rede").addEventListener("keydown", (e) => { if (e.key === "Enter") procurarAparelhos(); });
  $("#busca-voltar").addEventListener("click", () => passoBusca("aparelhos"));
  $("#busca-voltar2").addEventListener("click", () => passoBusca("acesso"));
  $("#busca-iniciar").addEventListener("click", iniciarBuscaCanais);
  $("#busca-senha").addEventListener("keydown", (e) => { if (e.key === "Enter") iniciarBuscaCanais(); });
  $("#busca-aplicar").addEventListener("click", aplicarBusca);
  $("#busca").addEventListener("close", () => {
    clearTimeout(busca.timer);
    if (assist.voltar) { const p = assist.voltar; assist.voltar = null; abrirAssistente(p); }
  });
  $("#cfg-assistente").addEventListener("click", () => abrirAssistente());
  $("#assist-procurar").addEventListener("click", async () => {
    assist.voltar = "cameras";
    $("#assistente").close();
    await abrirBusca();
    if (!$("#busca").open) { assist.voltar = null; abrirAssistente("cameras"); }
  });
  $("#assist-ir-telegram").addEventListener("click", () => passoAssistente("telegram"));
  $("#assist-conectar").addEventListener("click", conectarBot);
  $("#assist-token").addEventListener("keydown", (e) => { if (e.key === "Enter") conectarBot(); });
  $("#assist-trocar-bot").addEventListener("click", () => {
    clearTimeout(assist.timer);
    $("#assist-tg-pessoas").hidden = true;
    $("#assist-tg-bot").hidden = false;
    $("#assist-token").focus();
  });
  $("#assist-salvar-tg").addEventListener("click", salvarTelegram);
  $("#assist-voltar-cameras").addEventListener("click", () => passoAssistente("cameras"));
  $("#assist-pular-tg").addEventListener("click", () => passoAssistente("pronto"));
  $("#assist-concluir").addEventListener("click", concluirAssistente);
  $("#assistente").addEventListener("close", () => clearTimeout(assist.timer));
  $("#cfg-salvar").addEventListener("click", salvarConfig);
  $("#cfg-descartar").addEventListener("click", () => { estado.rascunho = clone(estado.config); estado.zonaNova = []; renderConfig(); });
  window.addEventListener("beforeunload", (e) => { if (sujo()) e.preventDefault(); });

  setInterval(() => ($("#relogio").textContent = new Date().toLocaleString("pt-BR", { weekday: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" })), 1000);
  setInterval(atualizarStatus, 2000);
  setInterval(() => estado.aba === "status" && carregarLogs(), 4000);
  await atualizarStatus();
  if (dados.primeira_vez) abrirAssistente();

  let aba = "vivo";
  try { aba = localStorage.getItem("aba") || "vivo"; } catch {}
  trocarAba(aba);
}

iniciar().catch((e) => toast(`Não foi possível conectar ao sistema: ${e.message}`, true));
