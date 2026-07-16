// =============================================================================
//  app.js — AlgEduDocs Dashboard: site selection, discovery (SSE), results
//  table, download (SSE), cloud upload with provider selection.
// =============================================================================

const state = {
  site: null,
  cycle: null,
  niveau: null,
  matieresDisponibles: {},
  matieresSelectionnees: new Set(),
  fiches: [],
  fichesFiltrees: [],
  fichesSelectionnees: new Set(),
  currentDiscoveryJob: null,
  currentDownloadJob: null,
  dernierDossierTelecharge: null,
  vue: 'table',
};

// ── THEME TOGGLE ─────────────────────────────────────────────────────

function initThemeToggle() {
  const saved = localStorage.getItem("algEdudocs_theme") || "dark";
  document.documentElement.setAttribute("data-theme", saved);

  const btn = document.createElement("button");
  btn.className = "theme-toggle";
  btn.setAttribute("aria-label", "Basculer le thème");
  btn.textContent = saved === "dark" ? "\u2600\ufe0f" : "\u{1F319}";
  btn.addEventListener("click", () => {
    const current = document.documentElement.getAttribute("data-theme");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("algEdudocs_theme", next);
    btn.textContent = next === "dark" ? "\u2600\ufe0f" : "\u{1F319}";
  });

  const header = document.querySelector("header");
  if (header) header.appendChild(btn);
}

// ── ÉTAPE 1 : choix du site ──────────────────────────────────────────

function initSiteCards() {
  document.querySelectorAll(".site-card").forEach((card) => {
    card.addEventListener("click", () => {
      document.querySelectorAll(".site-card").forEach((c) => c.classList.remove("active"));
      card.classList.add("active");
      state.site = card.dataset.site;
      resetApresSite();
      chargerCycles();
    });
  });
}

function resetApresSite() {
  state.cycle = null;
  state.niveau = null;
  state.matieresDisponibles = {};
  state.matieresSelectionnees.clear();
  state.fiches = [];
  state.fichesSelectionnees.clear();
  const panelNiveau = document.querySelector("#panel-niveau");
  if (panelNiveau) panelNiveau.style.display = "block";
  const panelFiches = document.querySelector("#panel-fiches");
  if (panelFiches) panelFiches.style.display = "none";
  const panelDownload = document.querySelector("#panel-download");
  if (panelDownload) panelDownload.style.display = "none";
  const selectCycle = document.querySelector("#select-cycle");
  if (selectCycle) {
    selectCycle.innerHTML = '<option value="">— Choisir —</option>';
  }
  const selectNiveau = document.querySelector("#select-niveau");
  if (selectNiveau) {
    selectNiveau.innerHTML = '<option value="">— Choisir le cycle d\'abord —</option>';
    selectNiveau.disabled = true;
  }
  const matieresGrid = document.querySelector("#matieres-grid");
  if (matieresGrid) {
    matieresGrid.innerHTML = '<span class="chip-empty">Choisir un niveau pour voir les mati\u00e8res</span>';
  }
  const btnDecouvrir = document.querySelector("#btn-decouvrir");
  if (btnDecouvrir) btnDecouvrir.disabled = true;
}

async function chargerCycles() {
  const res = await fetch("/api/cycles/" + state.site);
  const data = await res.json();
  state._cyclesData = data;
  const sel = document.querySelector("#select-cycle");
  sel.innerHTML = '<option value="">— Choisir —</option>';
  Object.entries(data).forEach(([slug, info]) => {
    const opt = document.createElement("option");
    opt.value = slug;
    opt.textContent = info.label || slug;
    sel.appendChild(opt);
  });
}

function onCycleChange() {
  const sel = document.querySelector("#select-cycle");
  const cycle = sel.value;
  state.cycle = cycle || null;
  state.niveau = null;
  state.matieresDisponibles = {};
  state.matieresSelectionnees = new Set();
  const btnDecouvrir = document.querySelector("#btn-decouvrir");
  if (btnDecouvrir) btnDecouvrir.disabled = true;
  const matieresGrid = document.querySelector("#matieres-grid");
  if (matieresGrid) {
    matieresGrid.innerHTML = '<span class="chip-empty">Choisir un niveau pour voir les mati\u00e8res</span>';
  }

  const selNiveau = document.querySelector("#select-niveau");
  selNiveau.innerHTML = '<option value="">— Choisir —</option>';
  if (!cycle) {
    selNiveau.disabled = true;
    return;
  }
  const niveaux = state._cyclesData[cycle].niveaux;
  Object.entries(niveaux).forEach(([slug, label]) => {
    const opt = document.createElement("option");
    opt.value = slug;
    opt.textContent = label;
    selNiveau.appendChild(opt);
  });
  selNiveau.disabled = false;
}

function onNiveauChange() {
  const niveau = document.querySelector("#select-niveau").value;
  state.niveau = niveau || null;
  const btnDecouvrir = document.querySelector("#btn-decouvrir");
  if (btnDecouvrir) btnDecouvrir.disabled = !niveau;
  if (niveau) {
    chargerMatieresReelles();
  }
}

// ── Matières chips ───────────────────────────────────────────────────

let _matieresRequestToken = 0;

async function chargerMatieresReelles() {
  const monToken = ++_matieresRequestToken;
  const siteAuMoment = state.site;
  const cycleAuMoment = state.cycle;
  const niveauAuMoment = state.niveau;

  const grid = document.querySelector("#matieres-grid");
  grid.innerHTML = '<span class="chip-empty">⏳ R\u00e9cup\u00e9ration des mati\u00e8res disponibles...</span>';
  const diagLines = [];

  const res = await fetch("/api/matieres", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ site: siteAuMoment, cycle: cycleAuMoment, niveau: niveauAuMoment }),
  });
  const { job_id } = await res.json();

  ouvrirSSE(job_id, {
    onLog: (d) => {
      if (monToken !== _matieresRequestToken) return;
      diagLines.push(d.msg);
      grid.innerHTML =
        '<span class="chip-empty">⏳ R\u00e9cup\u00e9ration en cours...</span>' +
        '<div class="diag-log">' + diagLines.map(escapeHtml).join("<br>") + "</div>";
    },
    onEnd: async () => {
      if (monToken !== _matieresRequestToken) return;
      const jobRes = await fetch("/api/job/" + job_id);
      const jobData = await jobRes.json();
      if (monToken !== _matieresRequestToken) return;
      if (jobData.error || !jobData.result) {
        grid.innerHTML =
          '<span class="chip-empty">⚠ Impossible de r\u00e9cup\u00e9rer les mati\u00e8res — lancez la d\u00e9couverte directement, elles seront d\u00e9tect\u00e9es au passage</span>' +
          (diagLines.length ? '<div class="diag-log">' + diagLines.map(escapeHtml).join("<br>") + "</div>" : "");
        state.matieresDisponibles = {};
        return;
      }
      state.matieresDisponibles = jobData.result.matieres || {};
      if (!Object.keys(state.matieresDisponibles).length && diagLines.length) {
        afficherChipsMatieres();
        grid.innerHTML += '<div class="diag-log">' + diagLines.map(escapeHtml).join("<br>") + "</div>";
        return;
      }
      afficherChipsMatieres();
    },
  });
}

function afficherChipsMatieres() {
  const grid = document.querySelector("#matieres-grid");
  const entries = Object.entries(state.matieresDisponibles);
  if (!entries.length) {
    grid.innerHTML = '<span class="chip-empty">Aucune mati\u00e8re d\u00e9tect\u00e9e — la d\u00e9couverte les trouvera peut-\u00eatre quand m\u00eame</span>';
    return;
  }
  grid.innerHTML = "";

  const actions = document.createElement("div");
  actions.className = "matieres-actions";
  actions.innerHTML = `
    <button type="button" class="btn-mini" id="btn-tout-selectionner">\u2713 Tout s\u00e9lectionner</button>
    <button type="button" class="btn-mini" id="btn-tout-deselectionner">\u2717 Tout d\u00e9s\u00e9lectionner</button>
  `;
  grid.appendChild(actions);

  const chipsWrap = document.createElement("div");
  chipsWrap.className = "matieres-chips-wrap";
  grid.appendChild(chipsWrap);

  state.matieresSelectionnees = new Set();

  entries.forEach(([slug, label]) => {
    const chip = document.createElement("div");
    chip.className = "chip";
    chip.textContent = label;
    chip.dataset.slug = slug;
    chip.addEventListener("click", () => {
      chip.classList.toggle("selected");
      if (chip.classList.contains("selected")) state.matieresSelectionnees.add(slug);
      else state.matieresSelectionnees.delete(slug);
    });
    chipsWrap.appendChild(chip);
  });

  document.querySelector("#btn-tout-selectionner").addEventListener("click", () => {
    chipsWrap.querySelectorAll(".chip").forEach((c) => {
      c.classList.add("selected");
      state.matieresSelectionnees.add(c.dataset.slug);
    });
  });
  document.querySelector("#btn-tout-deselectionner").addEventListener("click", () => {
    chipsWrap.querySelectorAll(".chip").forEach((c) => c.classList.remove("selected"));
    state.matieresSelectionnees.clear();
  });
}

// ── ÉTAPE 2 : découverte (SSE) ──────────────────────────────────────

function logLine(container, msg, cls) {
  if (!cls) cls = "";
  const div = document.createElement("div");
  div.className = "line" + (cls ? " " + cls : "");
  div.textContent = msg;
  container.appendChild(div);
  container.scrollTop = container.scrollHeight;
}

function ouvrirSSE(jobId, { onProgress, onLog, onEnd }) {
  const es = new EventSource("/api/stream/" + jobId);
  es.addEventListener("log", (e) => onLog && onLog(JSON.parse(e.data)));
  es.addEventListener("progress", (e) => onProgress && onProgress(JSON.parse(e.data)));
  var _done = false, poll = null;
  var terminer = function () {
    if (_done) return; _done = true;
    if (poll) clearInterval(poll);
    es.close();
    onEnd && onEnd();
  };
  es.addEventListener("end", terminer);
  es.onerror = function () {};
  poll = setInterval(function () {
    fetch("/api/job/" + jobId).then(function (r) { return r.json(); }).then(function (j) {
      if (j.status !== "running") terminer();
    }).catch(function () {});
  }, 2000);
  return es;
}

async function lancerDecouverte() {
  const matieresDisponibles = Object.keys(state.matieresDisponibles || {});
  if (matieresDisponibles.length && state.matieresSelectionnees.size === 0) {
    alert("Veuillez s\u00e9lectionner au moins une mati\u00e8re (ou cliquez sur \u00ab Tout s\u00e9lectionner \u00bb).");
    return;
  }

  const btn = document.querySelector("#btn-decouvrir");
  btn.disabled = true;

  document.querySelector("#panel-fiches").style.display = "block";
  document.querySelector("#fiches-progress").classList.add("visible");
  document.querySelector("#fiches-log").innerHTML = "";
  document.querySelector("#fiches-table-wrap").innerHTML =
    '<div class="empty-state"><div class="glyph">⏳</div>D\u00e9couverte en cours\u2026 suivez la progression dans le journal ci-dessus.</div>';
  setProgress("#fiches-progress", 0, 0, 0, 0);

  const btnAnnuler = document.querySelector("#btn-annuler-decouverte");
  btnAnnuler.style.display = "inline-flex";
  btnAnnuler.disabled = false;

  const body = {
    site: state.site,
    cycle: state.cycle,
    niveau: state.niveau,
    matieres: Array.from(state.matieresSelectionnees),
  };

  const res = await fetch("/api/decouvrir", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const { job_id } = await res.json();
  state.currentDiscoveryJob = job_id;

  const tempsDebut = Date.now();
  const minuteur = setInterval(() => {
    const s = Math.floor((Date.now() - tempsDebut) / 1000);
    const m = Math.floor(s / 60);
    const reste = s % 60;
    const el = document.querySelector("#fiches-elapsed");
    if (el) el.textContent = m > 0 ? m + " min " + reste + "s \u00e9coul\u00e9es" : reste + "s \u00e9coul\u00e9es";
  }, 1000);

  ouvrirSSE(job_id, {
    onLog: (d) => logLine(document.querySelector("#fiches-log"), d.msg),
    onProgress: (d) => {
      if (d.phase === "discover") {
        setProgress("#fiches-progress", d.current, d.total, null, null, d.label);
      }
    },
    onEnd: async () => {
      clearInterval(minuteur);
      btn.disabled = false;
      btnAnnuler.style.display = "none";
      const jobRes = await fetch("/api/job/" + job_id);
      const jobData = await jobRes.json();
      if (jobData.error) {
        logLine(document.querySelector("#fiches-log"), "\u2717 Erreur : " + jobData.error, "err");
        document.querySelector("#fiches-table-wrap").innerHTML =
          '<div class="empty-state"><div class="glyph">\u26a0</div>La d\u00e9couverte a \u00e9chou\u00e9. Voir le journal ci-dessus pour le d\u00e9tail.</div>';
        return;
      }
      state.fiches = (jobData.result && jobData.result.fiches) || [];
      state.fichesSelectionnees = new Set(state.fiches.map(function (_, i) { return i; }));
      afficherFiches();
    },
  });
}

function setProgress(sel, current, total, ok, err, label) {
  const root = document.querySelector(sel);
  const pct = total ? Math.round((current / total) * 100) : 0;
  root.querySelector(".progress-bar-fill").style.width = pct + "%";
  const stats = root.querySelector(".progress-stats");
  let txt = current + "/" + (total || "?");
  if (label) txt += "  \u00b7  " + label;
  let okErrTxt = "";
  if (ok !== null && ok !== undefined) {
    okErrTxt = '<span class="ok">\u2713 ' + ok + '</span> \u00b7 <span class="err">\u2717 ' + err + "</span>";
  }
  stats.innerHTML = "<span>" + txt + "</span><span>" + okErrTxt + "</span>";
}

// ── ÉTAPE 3 : tableau / cartes des fiches + filtres ──────────────────

function changerVue(vue) {
  state.vue = vue;
  document.querySelectorAll('.vbtn').forEach(function(b) {
    b.classList.toggle('act', b.dataset.view === vue);
  });
  if (state.fiches.length) rendreVue();
}

function rendreVue() {
  if (state.vue === 'cards') rendreCartes();
  else rendreTableau();
}

function estCycleSecondaire() {
  return state.cycle === "lycee" || state.fiches.some(function (f) { return f.filiere; });
}

function afficherFiches() {
  state.fichesFiltrees = state.fiches.slice();
  state.vue = 'table';
  document.querySelectorAll('.vbtn').forEach(function(b) {
    b.classList.toggle('act', b.dataset.view === 'table');
  });
  initialiserFiltres();
  rendreTableau();
}

function rendreTableau() {
  const wrap = document.querySelector("#fiches-table-wrap");
  const avecFiliere = estCycleSecondaire();

  if (!state.fiches.length) {
    wrap.innerHTML = '<div class="empty-state"><div class="glyph">\u2205</div>Aucune fiche trouv\u00e9e pour ces crit\u00e8res.</div>';
    const panelDownload = document.querySelector("#panel-download");
    if (panelDownload) panelDownload.style.display = "none";
    const filtresRow = document.querySelector("#filtres-row");
    if (filtresRow) filtresRow.style.display = "none";
    return;
  }

  if (!state.fichesFiltrees.length) {
    wrap.innerHTML = '<div class="empty-state"><div class="glyph">\u2315</div>Aucune fiche ne correspond aux filtres actuels.</div>';
    updateCompteur();
    return;
  }

  var LABELS_TRIM = { 1: "1er trim.", 2: "2e trim.", 3: "3e trim.", 0: "\u2014" };

  var rows = state.fichesFiltrees.map(function (f) {
    var i = state.fiches.indexOf(f);
    var badges = [];
    if (f.type_doc === "E") badges.push('<span class="badge examen">Examen</span>');
    if (f.type_doc === "D") badges.push('<span class="badge devoir">Devoir</span>');
    if (f.corrige) badges.push('<span class="badge corrige">\u2713 Corrig\u00e9</span>');
    var annee = f.annee ? f.annee : "\u2014";
    var trim = LABELS_TRIM[f.trimestre] || "\u2014";
    var filiereCell = avecFiliere ? "<td>" + escapeHtml(f.filiere || "\u2014") + "</td>" : "";
    var voirLink = f.url ? '<a href="/api/resolve-pdf?url=' + encodeURIComponent(f.url) + '&site=' + encodeURIComponent(state.site) + '" target="_blank" class="btn-voir" title="Voir le sujet">\u{1F441}</a>' : "";
    return (
      '<tr data-idx="' + i + '">' +
        '<td><input type="checkbox" class="fiche-check" data-idx="' + i + '"' + (state.fichesSelectionnees.has(i) ? " checked" : "") + "></td>" +
        '<td class="titre">' + escapeHtml(f.titre) + " " + voirLink + "</td>" +
        "<td>" + escapeHtml(f.matiere) + "</td>" +
        filiereCell +
        "<td>" + trim + "</td>" +
        "<td>" + annee + "</td>" +
        '<td class="badge-cell">' + badges.join(" ") + "</td>" +
      "</tr>"
    );
  }).join("");

  wrap.innerHTML =
    '<table class="fiches">' +
      "<thead>" +
        "<tr>" +
          '<th style="width:34px"><input type="checkbox" id="check-all"></th>' +
          "<th>Titre</th>" +
          "<th>Mati\u00e8re</th>" +
          (avecFiliere ? "<th>Fili\u00e8re</th>" : "") +
          "<th>Trimestre</th>" +
          "<th>Ann\u00e9e</th>" +
          "<th>Type</th>" +
        "</tr>" +
      "</thead>" +
      "<tbody>" + rows + "</tbody>" +
    "</table>";

  var toutesCochees = state.fichesFiltrees.every(function (f) {
    return state.fichesSelectionnees.has(state.fiches.indexOf(f));
  });
  document.querySelector("#check-all").checked = toutesCochees;

  document.querySelector("#check-all").addEventListener("change", function (e) {
    var checked = e.target.checked;
    state.fichesFiltrees.forEach(function (f) {
      var idx = state.fiches.indexOf(f);
      if (checked) state.fichesSelectionnees.add(idx);
      else state.fichesSelectionnees.delete(idx);
    });
    rendreTableau();
  });

  document.querySelectorAll(".fiche-check").forEach(function (cb) {
    cb.addEventListener("change", function (e) {
      var idx = parseInt(e.target.dataset.idx, 10);
      if (e.target.checked) state.fichesSelectionnees.add(idx);
      else state.fichesSelectionnees.delete(idx);
      updateCompteur();
    });
  });

  updateCompteur();
  document.querySelector("#panel-download").style.display = "block";
}

function rendreCartes() {
  var wrap = document.querySelector("#fiches-table-wrap");
  var avecFiliere = estCycleSecondaire();

  if (!state.fiches.length) {
    wrap.innerHTML = '<div class="empty-state"><div class="glyph">\u2205</div>Aucune fiche trouv\u00e9e pour ces crit\u00e8res.</div>';
    var panelDownload = document.querySelector("#panel-download");
    if (panelDownload) panelDownload.style.display = "none";
    var filtresRow = document.querySelector("#filtres-row");
    if (filtresRow) filtresRow.style.display = "none";
    return;
  }

  if (!state.fichesFiltrees.length) {
    wrap.innerHTML = '<div class="empty-state"><div class="glyph">\u2315</div>Aucune fiche ne correspond aux filtres actuels.</div>';
    updateCompteur();
    return;
  }

  var LABELS_TRIM = { 1: "1er trim.", 2: "2e trim.", 3: "3e trim.", 0: "\u2014" };

  var cards = state.fichesFiltrees.map(function (f) {
    var i = state.fiches.indexOf(f);
    var badges = [];
    if (f.type_doc === "E") badges.push('<span class="badge examen">Examen</span>');
    if (f.type_doc === "D") badges.push('<span class="badge devoir">Devoir</span>');
    if (f.corrige) badges.push('<span class="badge corrige">\u2713 Corrig\u00e9</span>');
    var annee = f.annee ? f.annee : "\u2014";
    var trim = LABELS_TRIM[f.trimestre] || "\u2014";
    var filiereHtml = avecFiliere && f.filiere ? '<div class="c-filiere">' + escapeHtml(f.filiere) + "</div>" : "";
    var voirLink = f.url ? '<a href="/api/resolve-pdf?url=' + encodeURIComponent(f.url) + '&site=' + encodeURIComponent(state.site) + '" target="_blank" class="btn-voir" title="Voir le sujet">\u{1F441}</a>' : "";
    return (
      '<div class="fc" data-idx="' + i + '">' +
        '<label class="fc-cb">' +
          '<input type="checkbox" class="fiche-check" data-idx="' + i + '"' + (state.fichesSelectionnees.has(i) ? " checked" : "") + ">" +
        '</label>' +
        '<div class="fc-body">' +
          '<div class="fc-titre">' + escapeHtml(f.titre) + " " + voirLink + "</div>" +
          '<div class="fc-meta">' +
            '<span class="fc-mat">' + escapeHtml(f.matiere) + "</span>" +
            filiereHtml +
            '<span class="fc-trim">' + trim + "</span>" +
            '<span class="fc-annee">' + annee + "</span>" +
          "</div>" +
          '<div class="fc-badges">' + badges.join(" ") + "</div>" +
        "</div>" +
      "</div>"
    );
  }).join("");

  wrap.innerHTML = '<div class="fc-grid">' + cards + "</div>";

  var toutesCochees = state.fichesFiltrees.every(function (f) {
    return state.fichesSelectionnees.has(state.fiches.indexOf(f));
  });

  document.querySelectorAll(".fc .fiche-check").forEach(function (cb) {
    cb.addEventListener("change", function (e) {
      var idx = parseInt(e.target.dataset.idx, 10);
      if (e.target.checked) state.fichesSelectionnees.add(idx);
      else state.fichesSelectionnees.delete(idx);
      updateCompteur();
    });
  });

  updateCompteur();
  document.querySelector("#panel-download").style.display = "block";
}

function updateCompteur() {
  var selSize = state.fichesSelectionnees.size;
  var el = document.querySelector("#fiches-count");
  if (!el) return;
  el.innerHTML =
    "<b>" + selSize + "</b> / " + state.fiches.length + " fiches s\u00e9lectionn\u00e9es" +
    (selSize > 0
      ? ' <span style="opacity:.7;font-size:11px;">\u2b07 ' + selSize + " seront t\u00e9l\u00e9charg\u00e9es</span>"
      : ' <span style="opacity:.5;">\u21d0 cochez des fiches pour t\u00e9l\u00e9charger</span>') +
    (state.fichesFiltrees.length !== state.fiches.length
      ? ' <span style="opacity:.6;">(' + state.fichesFiltrees.length + " affich\u00e9es)</span>"
      : "");
  var cbAll = document.querySelector("#check-all");
  if (cbAll) {
    var toutesCochees = state.fichesFiltrees.length > 0 &&
      state.fichesFiltrees.every(function (f) { return state.fichesSelectionnees.has(state.fiches.indexOf(f)); });
    cbAll.checked = toutesCochees;
  }
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, function (c) {
    return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c];
  });
}

// ── Filtres ──────────────────────────────────────────────────────────

function initialiserFiltres() {
  var row = document.querySelector("#filtres-row");
  if (!state.fiches.length) {
    if (row) row.style.display = "none";
    return;
  }
  if (row) row.style.display = "flex";

  var avecFiliere = estCycleSecondaire();
  var filtreFiliere = document.querySelector("#filtre-filiere");
  if (filtreFiliere) filtreFiliere.style.display = avecFiliere ? "inline-block" : "none";

  remplirOptions("#filtre-matiere", Array.from(new Set(state.fiches.map(function (f) { return f.matiere; }))).sort());
  if (avecFiliere) {
    remplirOptions("#filtre-filiere", Array.from(new Set(state.fiches.map(function (f) { return f.filiere; }).filter(Boolean))).sort());
  }
  remplirOptions("#filtre-annee", Array.from(new Set(state.fiches.map(function (f) { return f.annee; }).filter(Boolean))).sort(function (a, b) { return b - a; }));

  document.querySelectorAll(".filtre-input").forEach(function (el) { el.value = ""; });

  document.querySelectorAll(".filtre-input").forEach(function (el) {
    el.removeEventListener("input", appliquerFiltres);
    el.addEventListener("input", appliquerFiltres);
  });
  var btnEffacer = document.querySelector("#btn-effacer-filtres");
  if (btnEffacer) {
    btnEffacer.onclick = function () {
      document.querySelectorAll(".filtre-input").forEach(function (el) { el.value = ""; });
      appliquerFiltres();
    };
  }
}

function remplirOptions(selector, valeurs) {
  var sel = document.querySelector(selector);
  if (!sel) return;
  var premiereOption = sel.querySelector("option");
  sel.innerHTML = "";
  if (premiereOption) sel.appendChild(premiereOption);
  valeurs.forEach(function (v) {
    var opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v;
    sel.appendChild(opt);
  });
}

function appliquerFiltres() {
  var titreQ = (document.querySelector("#filtre-titre").value || "").trim().toLowerCase();
  var matiereQ = (document.querySelector("#filtre-matiere") && document.querySelector("#filtre-matiere").value) || "";
  var filiereQ = (document.querySelector("#filtre-filiere") && document.querySelector("#filtre-filiere").value) || "";
  var trimestreQ = (document.querySelector("#filtre-trimestre") && document.querySelector("#filtre-trimestre").value) || "";
  var anneeQ = (document.querySelector("#filtre-annee") && document.querySelector("#filtre-annee").value) || "";
  var typeQ = (document.querySelector("#filtre-type") && document.querySelector("#filtre-type").value) || "";
  var corrigeQ = (document.querySelector("#filtre-corrige") && document.querySelector("#filtre-corrige").value) || "";
  var limiteQ = parseInt((document.querySelector("#filtre-limite") && document.querySelector("#filtre-limite").value) || "0", 10) || 0;

  state.fichesFiltrees = state.fiches.filter(function (f) {
    if (titreQ && !(f.titre || "").toLowerCase().includes(titreQ)) return false;
    if (matiereQ && f.matiere !== matiereQ) return false;
    if (filiereQ && f.filiere !== filiereQ) return false;
    if (trimestreQ && String(f.trimestre) !== trimestreQ) return false;
    if (anneeQ && String(f.annee) !== anneeQ) return false;
    if (typeQ && f.type_doc !== typeQ) return false;
    if (corrigeQ === "1" && !f.corrige) return false;
    if (corrigeQ === "0" && f.corrige) return false;
    return true;
  });

  if (limiteQ > 0 && state.fichesFiltrees.length > limiteQ) {
    state.fichesFiltrees = state.fichesFiltrees.slice(0, limiteQ);
  }

  state.fichesSelectionnees = new Set(
    state.fichesFiltrees.map(function (f) { return state.fiches.indexOf(f); })
  );

  rendreVue();
}

// ── ÉTAPE 4 : téléchargement (SSE) ──────────────────────────────────

function nomDossierStable(site, niveau) {
  var capitaliser = function (s) {
    return s.replace(/(^|_|-|(?<=\d))([a-z])/g, function (_, sep, c) { return sep + c.toUpperCase(); });
  };
  var sitePart = capitaliser(site || "site");
  var niveauPart = capitaliser((niveau || "niveau").replace(/-/g, "_"));
  return sitePart + "_" + niveauPart;
}

async function lancerTelechargement(mode) {
  var fiches = state.fiches.filter(function (_, i) { return state.fichesSelectionnees.has(i); });
  if (!fiches.length) {
    alert("Veuillez s\u00e9lectionner au moins une fiche dans le tableau.");
    return;
  }

  var btnNormal = document.querySelector("#btn-telecharger");
  var btnZip = document.querySelector("#btn-telecharger-zip");
  var btnCloud = document.querySelector("#btn-telecharger-cloud");
  var btnAnnuler = document.querySelector("#btn-annuler-telechargement");
  [btnNormal, btnZip, btnCloud].forEach(function (b) { if (b) b.disabled = true; });
  btnAnnuler.style.display = "inline-flex";
  btnAnnuler.disabled = false;

  document.querySelector("#download-progress").classList.add("visible");
  document.querySelector("#download-log").innerHTML = "";
  document.querySelector("#download-result").classList.remove("visible");
  setProgress("#download-progress", 0, fiches.length, 0, 0);

  var dossier = mode === "zip"
    ? nomDossierStable(state.site, state.niveau) + "_zip_" + Date.now()
    : nomDossierStable(state.site, state.niveau);
  state.dernierDossierTelecharge = dossier;
  var niveauSelect = document.querySelector("#select-niveau");
  var niveauLabel = (niveauSelect && niveauSelect.selectedOptions && niveauSelect.selectedOptions[0] ? niveauSelect.selectedOptions[0].textContent : null) || state.niveau || "";

  var res = await fetch("/api/telecharger", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      site: state.site, fiches: fiches, dossier: dossier, niveau: niveauLabel,
      separer_corrige: (document.querySelector("#chk-separer-corrige") ? document.querySelector("#chk-separer-corrige").checked : true),
      clean_pdf: document.querySelector("#chk-clean-pdf") ? document.querySelector("#chk-clean-pdf").checked : true,
      cover_footer: document.querySelector("#chk-cover-footer") ? document.querySelector("#chk-cover-footer").checked : false,
    }),
  });
  var { job_id } = await res.json();
  state.currentDownloadJob = job_id;

  ouvrirSSE(job_id, {
    onLog: function (d) { logLine(document.querySelector("#download-log"), d.msg); },
    onProgress: function (d) {
      if (d.phase === "download") {
        setProgress("#download-progress", d.current, d.total, d.ok, d.err);
        logLine(
          document.querySelector("#download-log"),
          (d.success ? "\u2713 " : "\u2717 ") + d.titre + "  \u2014  " + d.info,
          d.success ? "ok" : "err"
        );
      }
    },
    onEnd: async function () {
      [btnNormal, btnZip, btnCloud].forEach(function (b) { if (b) b.disabled = false; });
      btnAnnuler.style.display = "none";
      state.currentDownloadJob = null;
      var jobRes = await fetch("/api/job/" + job_id);
      var jobData = await jobRes.json();
      var banner = document.querySelector("#download-result");
      banner.classList.add("visible");
      if (jobData.error) {
        banner.innerHTML = "\u2717 Erreur : " + escapeHtml(jobData.error);
        return;
      }
      var r = jobData.result || {};
      var html =
        "\u{1F3C1} Termin\u00e9 \u2014 <b>" + (r.ok || 0) + "</b> t\u00e9l\u00e9charg\u00e9s, <b>" + (r.err || 0) + "</b> \u00e9checs. " +
        'Dossier : <span class="dl-link">' + escapeHtml(dossier) + "</span>" +
        " \u00b7 \u{1F4CB} manifeste.json inclus";
      if (mode === "zip" && (r.ok || 0) > 0) {
        fetch("/api/zip/" + encodeURIComponent(dossier) + "?cleanup=1")
          .then(function (z) { return z.json(); })
          .then(function (z) {
            if (z.ok) {
              html += "<br>\u{1F4E6} ZIP cr\u00e9\u00e9 : <code>" + escapeHtml(z.fichier) + "</code>";
            } else {
              html += "<br>\u2717 Erreur cr\u00e9ation ZIP : " + escapeHtml(z.error || "");
            }
            banner.innerHTML = html;
          })
          .catch(function () {
            html += "<br>\u2717 Erreur cr\u00e9ation ZIP";
            banner.innerHTML = html;
          });
      }
      banner.innerHTML = html;
    },
  });
}

async function lancerTelechargementCloud() {
  var select = document.querySelector("#select-cloud-compte");
  var val = select ? select.value : "";
  var parts = val.split("::");
  var provider = parts[0];
  var compte = parts[1];
  if (!provider || !compte) {
    alert("Veuillez choisir un compte cloud dans la liste.");
    return;
  }

  var fiches = state.fiches.filter(function (_, i) { return state.fichesSelectionnees.has(i); });
  if (!fiches.length) {
    alert("Veuillez s\u00e9lectionner au moins une fiche dans le tableau.");
    return;
  }

  var btn = document.querySelector("#btn-telecharger-plus-cloud");
  var btnNormal = document.querySelector("#btn-telecharger");
  var btnZip = document.querySelector("#btn-telecharger-zip");
  var btnCloud = document.querySelector("#btn-telecharger-cloud");
  var btnAnnuler = document.querySelector("#btn-annuler-telechargement");
  [btn, btnNormal, btnZip, btnCloud].forEach(function (b) { if (b) b.disabled = true; });
  btnAnnuler.style.display = "inline-flex";
  btnAnnuler.disabled = false;

  document.querySelector("#download-progress").classList.add("visible");
  document.querySelector("#download-log").innerHTML = "";
  document.querySelector("#download-result").classList.remove("visible");
  setProgress("#download-progress", 0, fiches.length, 0, 0);

  var dossier = nomDossierStable(state.site, state.niveau) + "_cloud_" + Date.now();
  state.dernierDossierTelecharge = dossier;
  var niveauSelect = document.querySelector("#select-niveau");
  var niveauLabel = (niveauSelect && niveauSelect.selectedOptions && niveauSelect.selectedOptions[0] ? niveauSelect.selectedOptions[0].textContent : null) || state.niveau || "";

  var res = await fetch("/api/telecharger-cloud", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      site: state.site, fiches: fiches, dossier: dossier, niveau: niveauLabel,
      provider: provider, compte: compte,
      separer_corrige: (document.querySelector("#chk-separer-corrige") ? document.querySelector("#chk-separer-corrige").checked : true),
      clean_pdf: document.querySelector("#chk-clean-pdf") ? document.querySelector("#chk-clean-pdf").checked : true,
      cover_footer: document.querySelector("#chk-cover-footer") ? document.querySelector("#chk-cover-footer").checked : false,
    }),
  });
  var { job_id } = await res.json();
  state.currentDownloadJob = job_id;

  ouvrirSSE(job_id, {
    onLog: function (d) { logLine(document.querySelector("#download-log"), d.msg); },
    onProgress: function (d) {
      if (d.phase === "download") {
        setProgress("#download-progress", d.current, d.total, d.ok, d.err);
        if (d.titre) logLine(document.querySelector("#download-log"), (d.success ? "\u2713 " : "\u2717 ") + d.titre + "  \u2014  " + d.info, d.success ? "ok" : "err");
      } else if (d.phase === "cloud") {
        setProgress("#download-progress", d.current, d.total, d.ok, d.err);
      }
    },
    onEnd: async function () {
      [btn, btnNormal, btnZip, btnCloud].forEach(function (b) { if (b) b.disabled = false; });
      btnAnnuler.style.display = "none";
      state.currentDownloadJob = null;
      var jobRes = await fetch("/api/job/" + job_id);
      var jobData = await jobRes.json();
      var banner = document.querySelector("#download-result");
      banner.classList.add("visible");
      if (jobData.error) {
        banner.innerHTML = "\u2717 Erreur : " + escapeHtml(jobData.error);
        return;
      }
      var r = jobData.result || {};
      var cloud = r.cloud || {};
      var html = "\u{1F3C1} Termin\u00e9 \u2014 <b>" + (r.ok || 0) + "</b> t\u00e9l\u00e9charg\u00e9s, <b>" + (r.err || 0) + "</b> \u00e9checs.";
      if (cloud.ok !== undefined) {
        html += "<br>\u2601 Cloud (<em>" + escapeHtml(cloud.provider || provider) + "</em>) : <b>" + cloud.ok + "</b> envoy\u00e9s, <b>" + cloud.err + "</b> \u00e9checs.";
        var reussis = (cloud.fichiers || []).filter(function (f) { return f.ok && f.url_partage; });
        if (reussis.length) {
          html += '<div class="cloud-liens-list">' + reussis.map(function (f) {
            var tailleKo = f.taille_octets ? Math.round(f.taille_octets / 1024) + " Ko" : "\u2014";
            return '<div class="cloud-lien-item">\u{1F4C4} ' + escapeHtml(f.nom) + " (" + tailleKo + ') \u2014 ' +
                   '<a class="dl-link" href="' + escapeHtml(f.url_partage) + '" target="_blank">lien de partage</a></div>';
          }).join("") + "</div>";
        }
      }
      banner.innerHTML = html;
    },
  });
}

async function envoyerVersCloud() {
  var dossier = state.dernierDossierTelecharge;
  if (!dossier) {
    alert("Utilisez plut\u00f4t le bouton \u00ab \u2b07\u2601 T\u00e9l\u00e9charger + Cloud \u00bb qui fait les deux en un clic !");
    return;
  }
  var select = document.querySelector("#select-cloud-compte");
  var val = select ? select.value : "";
  var parts = val.split("::");
  var provider = parts[0];
  var compte = parts[1];
  if (!provider || !compte) {
    alert("Veuillez choisir un compte cloud dans la liste.");
    return;
  }

  var btnCloud = document.querySelector("#btn-telecharger-cloud");
  var btnAnnuler = document.querySelector("#btn-annuler-telechargement");
  btnCloud.disabled = true;
  btnAnnuler.style.display = "inline-flex";
  btnAnnuler.disabled = false;

  document.querySelector("#download-progress").classList.add("visible");
  document.querySelector("#download-log").innerHTML = "";

  var res = await fetch("/api/cloud-upload", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider: provider, compte: compte, dossier: dossier }),
  });
  var { job_id } = await res.json();
  state.currentDownloadJob = job_id;

  ouvrirSSE(job_id, {
    onLog: function (d) { logLine(document.querySelector("#download-log"), d.msg); },
    onEnd: async function () {
      btnCloud.disabled = false;
      btnAnnuler.style.display = "none";
      state.currentDownloadJob = null;
      var jobRes = await fetch("/api/job/" + job_id);
      var jobData = await jobRes.json();
      var banner = document.querySelector("#download-result");
      banner.classList.add("visible");
      if (jobData.error) {
        banner.innerHTML = "\u2717 Erreur cloud : " + escapeHtml(jobData.error);
        return;
      }
      var r = jobData.result || {};
      var html = "\u2601 Envoi vers " + escapeHtml(r.provider || "") + " (" + escapeHtml(r.compte || "") + ") termin\u00e9 \u2014 " +
        "<b>" + (r.ok || 0) + "</b> envoy\u00e9s, <b>" + (r.err || 0) + "</b> \u00e9checs.";
      var reussis = (r.fichiers || []).filter(function (f) { return f.ok && f.url_partage; });
      if (reussis.length) {
        html += '<div class="cloud-liens-list">' + reussis.map(function (f) {
          var tailleKo = f.taille_octets ? Math.round(f.taille_octets / 1024) + " Ko" : "\u2014";
          return '<div class="cloud-lien-item">\u{1F4C4} ' + escapeHtml(f.nom) + " (" + tailleKo + ') \u2014 ' +
                 '<a class="dl-link" href="' + escapeHtml(f.url_partage) + '" target="_blank">lien de partage</a></div>';
        }).join("") + "</div>";
      }
      banner.innerHTML = html;
    },
  });
}

// ── Cancel / Reset ───────────────────────────────────────────────────

function annulerDecouverteCourante() {
  if (!state.currentDiscoveryJob) return;
  fetch("/api/annuler/" + state.currentDiscoveryJob, { method: "POST" });
  logLine(document.querySelector("#fiches-log"), "\u23f9 Annulation demand\u00e9e, arr\u00eat en cours...");
  document.querySelector("#btn-annuler-decouverte").disabled = true;
}

function annulerTelechargementCourant() {
  if (!state.currentDownloadJob) return;
  fetch("/api/annuler/" + state.currentDownloadJob, { method: "POST" });
  logLine(document.querySelector("#download-log"), "\u23f9 Annulation demand\u00e9e, arr\u00eat en cours...");
  document.querySelector("#btn-annuler-telechargement").disabled = true;
}

function reinitialiserDashboard() {
  if (state.currentDiscoveryJob) fetch("/api/annuler/" + state.currentDiscoveryJob, { method: "POST" });
  if (state.currentDownloadJob) fetch("/api/annuler/" + state.currentDownloadJob, { method: "POST" });

  state.site = null;
  state.cycle = null;
  state.niveau = null;
  state.matieresDisponibles = {};
  state.matieresSelectionnees = new Set();
  state.fiches = [];
  state.fichesFiltrees = [];
  state.fichesSelectionnees = new Set();
  state.currentDiscoveryJob = null;
  state.currentDownloadJob = null;
  state.dernierDossierTelecharge = null;

  document.querySelectorAll(".site-card").forEach(function (c) { c.classList.remove("active"); });
  var pn = document.querySelector("#panel-niveau");
  if (pn) pn.style.display = "none";
  var pf = document.querySelector("#panel-fiches");
  if (pf) pf.style.display = "none";
  var pd = document.querySelector("#panel-download");
  if (pd) pd.style.display = "none";
  var fl = document.querySelector("#fiches-log");
  if (fl) fl.innerHTML = "";
  var dl = document.querySelector("#download-log");
  if (dl) dl.innerHTML = "";
  var dr = document.querySelector("#download-result");
  if (dr) dr.classList.remove("visible");
  var fp = document.querySelector("#fiches-progress");
  if (fp) fp.classList.remove("visible");
  var dp = document.querySelector("#download-progress");
  if (dp) dp.classList.remove("visible");
  var bad = document.querySelector("#btn-annuler-decouverte");
  if (bad) bad.style.display = "none";
  var bat = document.querySelector("#btn-annuler-telechargement");
  if (bat) bat.style.display = "none";
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// ── Cloud ────────────────────────────────────────────────────────────

var NOMS_PROVIDERS = { gdrive: "Google Drive", mega: "Mega", onedrive: "OneDrive", dropbox: "Dropbox" };
var _cloudConnectes = {};

async function verifierEtatCloud() {
  try {
    var res = await fetch("/api/cloud-status");
    var data = await res.json();
    if (data.pret && data.comptes && Object.keys(data.comptes).length) {
      if (data.connectes) Object.assign(_cloudConnectes, data.connectes);

      var select = document.querySelector("#select-cloud-compte");
      select.innerHTML = "";
      Object.entries(data.comptes).forEach(function (_ref) {
        var provider = _ref[0];
        var noms = _ref[1];
        noms.forEach(function (nom) {
          var opt = document.createElement("option");
          var key = provider + "::" + nom;
          opt.value = key;
          var connecte = _cloudConnectes[key];
          var icone = "";
          if (connecte === true) icone = "\u{1F513} ";
          else if (connecte === false) icone = "\u{1F512} ";
          opt.textContent = icone + (NOMS_PROVIDERS[provider] || provider) + " \u2014 " + nom;
          select.appendChild(opt);
        });
      });
      var cloudRow = document.querySelector("#cloud-row");
      if (cloudRow) cloudRow.style.display = "flex";
      onCloudCompteChange();
    }
  } catch (e) {
    // Cloud not configured — hide
  }
}

function onCloudCompteChange() {
  var select = document.querySelector("#select-cloud-compte");
  var key = select ? select.value : "";
  var connecte = _cloudConnectes[key];
  var btnCloud = document.querySelector("#btn-telecharger-cloud");
  var btnCloudPlus = document.querySelector("#btn-telecharger-plus-cloud");
  var btnConnect = document.querySelector("#btn-connecter-cloud");

  if (connecte === false) {
    if (btnCloud) btnCloud.style.display = "none";
    if (btnCloudPlus) btnCloudPlus.style.display = "none";
    if (btnConnect) btnConnect.style.display = "inline-flex";
  } else if (connecte === true || connecte === undefined) {
    if (btnCloud) btnCloud.style.display = "inline-flex";
    if (btnCloudPlus) btnCloudPlus.style.display = "inline-flex";
    if (btnConnect) btnConnect.style.display = "none";
  }
}

// ── Google OAuth2 ────────────────────────────────────────────────────

document.addEventListener("click", function (e) {
  if (e.target.id === "btn-connecter-cloud") {
    var select = document.querySelector("#select-cloud-compte");
    var key = select ? select.value : "";
    var parts = key.split("::");
    var provider = parts[0];
    var compte = parts[1];
    if (provider === "gdrive") {
      logLine(document.querySelector("#download-log"), "\u{1F511} Ouverture du navigateur pour l'authentification Google...");
      logLine(document.querySelector("#download-log"), "\u23f3 Autorisez l'acc\u00e8s dans la fen\u00eatre qui s'ouvre, puis revenez ici.");

      fetch("/api/cloud/google/auth?compte=" + encodeURIComponent(compte))
        .then(function (r) { return r.json(); })
        .then(function () {
          var poll = setInterval(function () {
            fetch("/api/cloud/google/auth-status/" + encodeURIComponent(compte))
              .then(function (r) { return r.json(); })
              .then(function (sd) {
                if (sd.status === "connected") {
                  clearInterval(poll);
                  logLine(document.querySelector("#download-log"), "\u{1F513} Compte " + compte + " connect\u00e9 avec succ\u00e8s !");
                  verifierEtatCloud();
                } else if (sd.status === "error") {
                  clearInterval(poll);
                  logLine(document.querySelector("#download-log"), "\u274c \u00c9chec : " + (sd.error || "erreur inconnue"));
                }
              });
          }, 1500);
        })
        .catch(function (err) { alert("Erreur : " + err.message); });
    } else {
      alert("L'authentification automatique n'est pas encore support\u00e9e pour ce provider.");
    }
  }
});

// ── INIT ─────────────────────────────────────────────────────────────

document.addEventListener("DOMContentLoaded", function () {
  initThemeToggle();
  initSiteCards();
  verifierEtatCloud();
  setInterval(verifierEtatCloud, 30000);

  var selCycle = document.querySelector("#select-cycle");
  if (selCycle) selCycle.addEventListener("change", onCycleChange);

  var selNiveau = document.querySelector("#select-niveau");
  if (selNiveau) selNiveau.addEventListener("change", onNiveauChange);

  var btnDecouvrir = document.querySelector("#btn-decouvrir");
  if (btnDecouvrir) {
    btnDecouvrir.addEventListener("click", function () {
      lancerDecouverte();
    });
  }

  var btnAnnulerDecouverte = document.querySelector("#btn-annuler-decouverte");
  if (btnAnnulerDecouverte) btnAnnulerDecouverte.addEventListener("click", annulerDecouverteCourante);

  var btnTelecharger = document.querySelector("#btn-telecharger");
  if (btnTelecharger) btnTelecharger.addEventListener("click", function () { lancerTelechargement("normal"); });

  var btnTelechargerZip = document.querySelector("#btn-telecharger-zip");
  if (btnTelechargerZip) btnTelechargerZip.addEventListener("click", function () { lancerTelechargement("zip"); });

  var btnTelechargerCloud = document.querySelector("#btn-telecharger-cloud");
  if (btnTelechargerCloud) btnTelechargerCloud.addEventListener("click", envoyerVersCloud);

  var btnTelechargerPlusCloud = document.querySelector("#btn-telecharger-plus-cloud");
  if (btnTelechargerPlusCloud) btnTelechargerPlusCloud.addEventListener("click", lancerTelechargementCloud);

  var selectCloudCompte = document.querySelector("#select-cloud-compte");
  if (selectCloudCompte) selectCloudCompte.addEventListener("change", onCloudCompteChange);

  var btnAnnulerTelechargement = document.querySelector("#btn-annuler-telechargement");
  if (btnAnnulerTelechargement) btnAnnulerTelechargement.addEventListener("click", annulerTelechargementCourant);

  var btnReinitialiser = document.querySelector("#btn-reinitialiser");
  if (btnReinitialiser) btnReinitialiser.addEventListener("click", reinitialiserDashboard);
});
