/* =============================================================================
   MyPTAI Coach PWA - app.js
   Onboarding Garmin (login multi-dispositivo), Readiness, Allenamenti + Sync
   One-Tap, Injury Lab 3.0 (Card Anatomiche), Obiettivo di gara, Chat AI Coach.
   ============================================================================= */
"use strict";

const $ = (sel) => document.querySelector(sel);

/* ============================ SESSIONE UTENTE (Garmin) ============================ */
// La sessione (token + email Garmin) resta salvata sul dispositivo: l'utente
// resta collegato per sempre. PC e cellulare con le stesse credenziali Garmin
// condividono gli stessi dati (infortuni, obiettivi, tabelle, allenamenti).
const SESSION_STORE = "myptai_session";
// Sottotitolo ufficiale dell'onboarding (copy marketing MyPTAI)
const ONBOARDING_SUB = "Il primo Coach AI che ascolta il tuo corpo in qualsiasi momento. " +
  "Unisci i dati del tuo Garmin all'Intelligenza Artificiale: adatta ogni sessione " +
  "di allenamento al tuo stato di forma per prevenire gli infortuni e " +
  "massimizzare i tuoi progressi.";
let SESSION = null;
try { SESSION = JSON.parse(localStorage.getItem(SESSION_STORE) || "null"); } catch { SESSION = null; }
let _unlocked = false;

/* ==================== MULTI-ACCOUNT SWITCHER & LOGOUT ==================== */
// Account salvati sul dispositivo {email, display_name, token}: cambio rapido
// in 1 tap senza reinserire la password Garmin.
const ACCOUNTS_STORE = "myptai_accounts";
let _lastSummary = null;   // ultimo contesto readiness (per la modale account)

function loadAccounts() {
  try {
    const a = JSON.parse(localStorage.getItem(ACCOUNTS_STORE) || "[]");
    return Array.isArray(a) ? a : [];
  } catch { return []; }
}
function saveAccounts(list) {
  try { localStorage.setItem(ACCOUNTS_STORE, JSON.stringify(list)); } catch { /* best-effort */ }
}
function rememberAccount(email, token, displayName) {
  if (!email || !token) return;
  const list = loadAccounts().filter((a) => a.email !== email);
  list.push({ email, display_name: displayName || email, token });
  saveAccounts(list);
}
function activeAccountEmail() {
  return SESSION && SESSION.user_email ? SESSION.user_email : null;
}
function refreshAllData() {
  // Ricarica i dati biometrici per-utente senza reinserire la password
  Promise.allSettled([loadReadiness(), loadInjuries(), loadGoals(), loadWorkoutSection(), loadTable()]);
}
function openAccountModal() {
  const email = activeAccountEmail();
  const acc = loadAccounts().find((a) => a.email === email);
  const uname = _lastSummary?.atleta?.nome || acc?.display_name || email || "Atleta";
  const nameEl = $("#acct-active-name");
  const mailEl = $("#acct-active-email");
  const vo2El = $("#acct-active-vo2");
  if (nameEl) nameEl.textContent = uname;
  if (mailEl) mailEl.textContent = email || "—";
  if (vo2El) vo2El.textContent = _lastSummary?.progressi?.vo2max?.attuale ?? "—";
  // Lista account salvati (switch in 1 tap, mai l'account attivo due volte)
  const listEl = $("#acct-list");
  const list = loadAccounts();
  if (listEl) {
    if (!list.length) {
      listEl.innerHTML = `<div class="acct-empty">Nessun account salvato su questo dispositivo.</div>`;
    } else {
      listEl.innerHTML = list.map((a) => {
        const isActive = a.email === email;
        return `<div class="acct-item${isActive ? " active" : ""}" data-email="${esc(a.email)}">
          <div class="acct-item-ico">👤</div>
          <div class="acct-item-info">
            <div class="acct-item-name">${esc(a.display_name || a.email)}</div>
            <div class="acct-item-mail">${esc(a.email)}</div>
          </div>
          ${isActive ? `<span class="acct-item-tag">attivo</span>`
                     : `<button class="acct-item-switch" data-switch="${esc(a.email)}">🔁 Usa</button>`}
        </div>`;
      }).join("");
      listEl.querySelectorAll("[data-switch]").forEach((btn) => {
        btn.addEventListener("click", () => switchAccount(btn.getAttribute("data-switch")));
      });
    }
  }
  $("#account-modal").classList.remove("hidden");
}
function closeAccountModal() {
  $("#account-modal").classList.add("hidden");
}
function switchAccount(email) {
  const acc = loadAccounts().find((a) => a.email === email);
  if (!acc || !acc.token) {
    closeAccountModal();
    showLogin("⚠️ Account non trovato sul dispositivo: accedi di nuovo con le credenziali Garmin");
    return;
  }
  SESSION = { token: acc.token, user_email: acc.email };
  localStorage.setItem(SESSION_STORE, JSON.stringify(SESSION));
  CURRENT_USER_EMAIL = acc.email;
  resetReadiness();
  closeAccountModal();
  toast("🔄 Account attivo: " + (acc.display_name || acc.email), 2600);
  refreshAllData();
}
function logout() {
  // Pulisce il token di sessione attuale e rimuove l'account dalla lista salvata
  const email = activeAccountEmail();
  if (email) saveAccounts(loadAccounts().filter((a) => a.email !== email));
  localStorage.removeItem(SESSION_STORE);
  SESSION = null;
  CURRENT_USER_EMAIL = null;
  resetReadiness();
  closeAccountModal();
  showLogin();
}
$("#garmin-badge-btn").addEventListener("click", openAccountModal);
$("#garmin-badge-btn").addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openAccountModal(); }
});
$("#acct-close-btn").addEventListener("click", closeAccountModal);
$("#account-modal").addEventListener("click", (e) => {
  if (e.target.id === "account-modal") closeAccountModal();
});
$("#acct-add-btn").addEventListener("click", () => {
  closeAccountModal();
  showLogin();
  const em = $("#login-email");
  if (em) em.focus();
});
$("#acct-logout-btn").addEventListener("click", logout);

function showLogin(msg) {
  _unlocked = false;
  const sub = $("#login-sub");
  if (sub) sub.textContent = ONBOARDING_SUB;
  if (msg) {
    $("#login-err").textContent = msg;
    $("#login-err").classList.remove("hidden");
  } else {
    $("#login-err").classList.add("hidden");
  }
  $("#login-screen").classList.remove("hidden");
}
function hideLogin() {
  _unlocked = true;
  $("#login-screen").classList.add("hidden");
}
$("#login-btn").addEventListener("click", async () => {
  const email = $("#login-email").value.trim();
  const pass = $("#login-password").value;
  if (!email || !pass) { showLogin("❌ Inserisci email e password Garmin"); return; }
  const btn = $("#login-btn");
  btn.disabled = true;
  btn.textContent = "⏳ Connessione a Garmin...";
  try {
    // /api/auth è l'endpoint pubblico di onboarding (non richiede token)
    const res = await fetch("/api/auth", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password: pass }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.token) {
      // 429 = rate limit raggiunto: avviso ambra + conto alla rovescia live
      if (res.status === 429 || data.detail === "rate_limit_exceeded") {
        startLockCountdown(data.retry_after_s || 900);
        return;
      }
      throw new Error((data && data.detail) || "Credenziali non valide");
    }
    clearLockTimer();
    SESSION = { token: data.token, user_email: data.user_email };
    localStorage.setItem(SESSION_STORE, JSON.stringify(SESSION));
    CURRENT_USER_EMAIL = data.user_email;
    // Multi-account: salva l'account sul dispositivo (nome reale al primo loadReadiness)
    rememberAccount(data.user_email, data.token);
    btn.disabled = false;
    btn.textContent = "🚀 Inizia Coaching";
    hideLogin();
    toast("✅ Benvenuto " + (data.user_email || "") + " — sincronizzazione in corso...", 3200);
    // GDPR: prima di procedere verifica il consenso al trattamento dei dati
    checkConsentAndStart();
  } catch (err) {
    btn.disabled = false;
    btn.textContent = "🚀 Inizia Coaching";
    clearLockTimer();
    showLogin("❌ " + (err.message || "Credenziali non valide"));
  }
});

/* ============================ RATE LIMIT LOGIN (429) ============================ */
// Dopo 5 tentativi errati il server blocca per 15 min: countdown ambra in tempo reale.
let _lockTimer = null;
function clearLockTimer() {
  if (_lockTimer) { clearInterval(_lockTimer); _lockTimer = null; }
}
function startLockCountdown(seconds) {
  clearLockTimer();
  const btn = $("#login-btn");
  const err = $("#login-err");
  err.classList.add("amber");
  const end = Date.now() + Math.max(0, seconds) * 1000;
  const tick = () => {
    const s = Math.max(0, Math.round((end - Date.now()) / 1000));
    const mm = String(Math.floor(s / 60)).padStart(2, "0");
    const ss = String(s % 60).padStart(2, "0");
    showLogin("⏳ Troppi tentativi errati. Riprova tra " + mm + "m " + ss + "s");
    if (s > 0) {
      btn.disabled = true;
      btn.textContent = "🔒 " + mm + "m " + ss + "s";
    } else {
      clearLockTimer();
      err.classList.remove("amber");
      btn.disabled = false;
      btn.textContent = "🚀 Inizia Coaching";
      showLogin("");
    }
  };
  tick();
  _lockTimer = setInterval(tick, 1000);
}

/* ============================ PRIVACY & GDPR ============================ */
// Consenso (Art. 6-7) verificato lato server: se non ancora dato, il primo
// onboarding mostra l'Informativa Privacy e richiede l'accettazione.
async function checkConsentAndStart() {
  try {
    const me = await api("/api/user/me");
    if (me && me.consent) { init(); return; }
  } catch { /* se l'endpoint fallisce mostra comunque l'informativa */ }
  showPrivacyModal();
}
function showPrivacyModal() {
  $("#privacy-check").checked = false;
  $("#privacy-accept").disabled = true;
  $("#privacy-modal").classList.remove("hidden");
}
async function acceptConsent() {
  const btn = $("#privacy-accept");
  btn.disabled = true;
  try {
    await api("/api/user/consent", { method: "POST", body: JSON.stringify({ version: "1.1", accepted: true }) });
    $("#privacy-modal").classList.add("hidden");
    init();
  } catch (err) {
    toast("❌ " + (err.message || "Errore durante il salvataggio del consenso"));
    btn.disabled = false;
  }
}
$("#privacy-check").addEventListener("change", (e) => {
  $("#privacy-accept").disabled = !e.target.checked;
});
$("#privacy-accept").addEventListener("click", acceptConsent);
// Footer: riapri l'informativa (se non ancora consenziente permette di accettare)
$("#privacy-btn").addEventListener("click", () => {
  $("#privacy-check").checked = true;
  $("#privacy-accept").disabled = false;
  $("#privacy-modal").classList.remove("hidden");
});

/* ============================ SUITE LEGALE (Termini/Privacy/Cookie) ============================ */
// Documenti serviti da /api/legal/* (pubblici, consultabili anche pre-login)
const LEGAL_DOCS = {
  terms: { title: "Termini di Servizio", url: "/api/legal/terms" },
  privacy: { title: "Informativa Privacy (GDPR)", url: "/api/legal/privacy" },
  cookies: { title: "Cookie Policy", url: "/api/legal/cookies" },
};
function renderLegal(j) {
  const upd = j.updated ? `<p class="hint">Aggiornato: ${esc(j.updated)}</p>` : "";
  const secs = (j.sections || []).map((s) =>
    `<h4>${esc(s.h)}</h4>` + (s.paras || []).map((p) => `<p>${esc(p)}</p>`).join("")
  ).join("");
  return upd + secs;
}
async function openLegal(doc) {
  const d = LEGAL_DOCS[doc];
  if (!d) return;
  const box = $("#legal-modal");
  $("#legal-title").textContent = d.title;
  $("#legal-content").innerHTML = `<p class="hint">⏳ Caricamento del documento...</p>`;
  box.classList.remove("hidden");
  try {
    const r = await fetch(d.url); // endpoint pubblico: nessun header di sessione richiesto
    if (!r.ok) throw new Error("HTTP " + r.status);
    const j = await r.json();
    $("#legal-title").textContent = j.title || d.title;
    $("#legal-content").innerHTML = renderLegal(j);
  } catch (err) {
    $("#legal-content").innerHTML =
      `<p class="hint">❌ Documento non disponibile: ${esc(err.message)}</p>`;
  }
}
$("#legal-close").addEventListener("click", () => $("#legal-modal").classList.add("hidden"));
$("#terms-btn").addEventListener("click", () => openLegal("terms"));
$("#privacy-policy-btn").addEventListener("click", () => openLegal("privacy"));
$("#cookies-btn").addEventListener("click", () => openLegal("cookies"));
// Link ai documenti dentro la modale di consenso (onboarding)
document.querySelectorAll("[data-legal]").forEach((el) => {
  el.addEventListener("click", (e) => {
    e.preventDefault();
    openLegal(el.dataset.legal);
  });
});
// Eliminazione account (Diritto all'oblio, Art. 17 GDPR)
$("#delete-account-btn").addEventListener("click", () => {
  $("#delete-modal").classList.remove("hidden");
});
$("#delete-cancel").addEventListener("click", () => {
  $("#delete-modal").classList.add("hidden");
});
$("#delete-confirm").addEventListener("click", async () => {
  const btn = $("#delete-confirm");
  btn.disabled = true;
  btn.textContent = "⏳ Eliminazione in corso...";
  try {
    await api("/api/user/delete-account", { method: "POST", body: JSON.stringify({}) });
    localStorage.removeItem(SESSION_STORE);
    SESSION = null;
    $("#delete-modal").classList.add("hidden");
    toast("🗑️ Account e dati eliminati (Art. 17 GDPR)");
    showLogin();
  } catch (err) {
    toast("❌ " + (err.message || "Errore durante l'eliminazione"));
    btn.disabled = false;
    btn.textContent = "🗑️ Elimina definitivamente";
  }
});

/* ============================ UTIL ============================ */
function toast(msg, ms = 2600) {
  const t = $("#toast");
  t.textContent = typeof msg === "string" ? msg : JSON.stringify(msg);
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), ms);
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------- Protezione Anti-Duplicati Sync (per giornata) ---------- */
// Email dell'utente attivo: aggiornata a ogni login/switch/logout. La chiave
// di sync in localStorage è ISOLATA PER UTENTE (zero falsi "Sincronizzato su
// Garmin" tra account diversi sullo stesso dispositivo).
let CURRENT_USER_EMAIL = (SESSION && SESSION.user_email) || null;
function syncDoneKey() {
  return "myptai_sync_done_" + (CURRENT_USER_EMAIL || "anon") + "_v55";
}
function syncDayKey() {
  return TODAY_SERVER || new Date().toISOString().slice(0, 10);
}
function isSyncDone() {
  try {
    const v = JSON.parse(localStorage.getItem(syncDoneKey()) || "{}")[syncDayKey()];
    return v !== undefined && v !== null && v !== false;
  } catch { return false; }
}
function markSyncDone() {
  try {
    const rec = JSON.parse(localStorage.getItem(syncDoneKey()) || "{}");
    rec[syncDayKey()] = true;
    localStorage.setItem(syncDoneKey(), JSON.stringify(rec));
  } catch { /* localStorage non disponibile: il backend resta idempotente */ }
}
// BLOCCO PERMANENTE MULTI-TAB INCONDIZIONATO (v64): ritorna true se il piano
// risulta sincronizzato sul server (serverSynced===true) OPPURE se la chiave
// locale isSyncDone() === true per lo slug dell'utente attivo. Nessun altro
// controllo (id piano, ecc.): una volta sincronizzato, MAI più ri-premere.
function syncLocked(serverSynced) {
  if (serverSynced) return true;
  try {
    return JSON.parse(localStorage.getItem(syncDoneKey()) || "{}")[syncDayKey()] === true;
  } catch { return false; }
}
function setSyncButtonDone(btn, st, msg) {
  if (btn) { btn.disabled = true; btn.innerHTML = "✅ Sincronizzato su Garmin"; }
  if (st) st.innerHTML = `<span class="sync-ok">✅ ${esc(msg || "Sincronizzato su Garmin")}</span>`;
}
// BLOCCO CROSS-TAB REATTIVO: dopo un sync riuscito da QUALSIASI scheda
// (Home / Step 3 / Generatore) aggiorna SUBITO lo stato globale isSyncDone()
// e disabilita i pulsanti di sync su TUTTE le schede presenti nel DOM.
function setSyncAllDone(msg) {
  markSyncDone();
  setSyncButtonDone($("#btn-sync-home"), $("#sync-home-status"), msg);
  const sb = $("#btn-sync-today");
  if (sb) { sb.disabled = true; sb.innerHTML = "✅ Sincronizzato su Garmin"; }
  const st = $("#advice-status");
  if (st && msg) st.innerHTML = `<span class="sync-ok">✅ ${esc(msg)}</span>`;
}
function fmtPace(secKm) {
  if (secKm == null) return "n/a";
  const m = Math.floor(secKm / 60), s = Math.round(secKm % 60);
  return `${m}:${String(s).padStart(2, "0")}/km`;
}
function fmtClock(sec) {
  sec = Math.round(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
           : `${m}:${String(s).padStart(2, "0")}`;
}
// Ritmo "M:SS/km" -> secondi al km (per stimare il lavoro senza durata_s)
function paceToSec(p) {
  const m = /^(\d+):(\d{2})/.exec(String(p || ""));
  return m ? Number(m[1]) * 60 + Number(m[2]) : 0;
}
// Durata totale REALE del workout (identica a Garmin): somma delle fasi di
// corsa + recuperi tra le ripetute. I recuperi contano SOLO per le fasi che il
// backend trasforma in RepeatGroup (tipo "repeat" con ripetizioni > 1): per le
// altre (es. "interval" singola) non vengono sommati, come nel payload Garmin.
// Se il backend ha già calcolato durata_reale_s lo usa, altrimenti ricalcola.
function workoutTotalDurationS(w) {
  if (!w) return 0;
  if (typeof w.durata_reale_s === "number") return w.durata_reale_s;
  const fasi = w.fasi;
  if (!Array.isArray(fasi) || !fasi.length) return w.durata_totale_s || 0;
  let total = 0;
  for (const f of fasi) {
    total += Number(f.durata_s) || 0;
    if (f.recupero_s && f.ripetizioni && f.tipo === "repeat" && Number(f.ripetizioni) > 1) {
      const n = Math.max(1, Number(f.ripetizioni));
      if (!f.durata_s && f.distanza_m && f.ritmo) {
        total += paceToSec(f.ritmo) * Number(f.distanza_m) / 1000 * n;
      }
      total += Number(f.recupero_s) * n;
    }
  }
  return total;
}
// Distanza totale di corsa (metri): somma le fasi a metri (distanza_m x n) e
// le fasi a tempo convertite dal ritmo; per le ripetute/allunghi include anche
// i recuperi (jog di recupero a ritmo_recupero, default il ritmo della fase).
function workoutTotalDistanceM(w) {
  if (!w) return 0;
  const fasi = w.fasi;
  if (!Array.isArray(fasi) || !fasi.length) return 0;
  let total = 0;
  for (const f of fasi) {
    const n = Math.max(1, Number(f.ripetizioni) || 1);
    if (f.distanza_m) {
      total += Number(f.distanza_m) * n;
    } else if (f.durata_s && f.ritmo) {
      total += Number(f.durata_s) * 1000 / paceToSec(f.ritmo);
    }
    if (f.tipo === "repeat" && f.recupero_s && n > 1) {
      const recPace = paceToSec(f.ritmo_recupero || f.ritmo);
      if (recPace > 0) total += Number(f.recupero_s) * n * 1000 / recPace;
    }
  }
  return Math.round(total);
}
// Formato durata: "54 min" oppure "1h 01m" (>= 1 ora, come il totale Garmin)
// Tempo di recupero esatto: < 60s in secondi, altrimenti M:SS (es. 90 -> "1:30")
function fmtRec(sec) {
  const s = Math.max(0, Math.round(Number(sec) || 0));
  if (s < 60) return s + "s";
  return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
}
// Un avviso "override atleta" è visibile SOLO se l'override è attivo E la
// Readiness attuale è ROSSA (altrimenti è un avviso vecchio, es. 81 GREEN).
function overrideVisible(flag, level) {
  return !!flag && String(level || "").toUpperCase() === "RED";
}
const OVERRIDE_NOTE_RE = /OVERRIDE ATLETA ATTIVO|Readiness RED/i;
// Rimuove dalla nota le righe con avvisi override/Readiness RED quando la
// Readiness attuale NON è RED (doppia protezione col backend _decorate_workout).
function cleanOverrideNote(note, level) {
  if (!note || String(level || "").toUpperCase() === "RED") return note;
  const clean = note.split("\n").filter((ln) => !OVERRIDE_NOTE_RE.test(ln)).join("\n").trim();
  return clean || null;
}
function fmtDuration(s) {
  const m = Math.floor((s || 0) / 60);
  if (m < 60) return `${m} min`;
  return `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m`;
}
// Formato "38m 28s" (usato per i Tempi Reali nello stato ALLENAMENTO ESEGUITO)
function fmtDurClock(s) {
  const v = Number(s) || 0;
  const m = Math.floor(v / 60);
  const sec = Math.round(v % 60);
  if (sec >= 60) return `${m + 1}m 0s`;
  return `${m}m ${String(sec).padStart(2, "0")}s`;
}
async function api(path, opts = {}) {
  const headers = { ...(opts.headers || {}), "Content-Type": "application/json" };
  const token = SESSION?.token;
  if (token) headers["Authorization"] = "Bearer " + token;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    if (token === SESSION?.token) {
      localStorage.removeItem(SESSION_STORE);
      SESSION = null;
      showLogin("🔒 Sessione non valida: accedi di nuovo con le credenziali Garmin");
    }
    throw new Error("Sessione non valida");
  }
  let data = null;
  try { data = await res.json(); } catch { /* no body */ }
  if (!res.ok) {
    // FastAPI può restituire detail come oggetto/array (es. 422): evita "[object Object]"
    let msg = `Errore HTTP ${res.status}`;
    if (data && data.detail != null) {
      msg = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail);
    }
    throw new Error(msg);
  }
  return data;
}

/* ============================ TABS ============================ */
document.querySelectorAll(".nav-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
    btn.classList.add("active");
    $(`#tab-${btn.dataset.tab}`).classList.add("active");
    // alla apertura ricarica i dati live (obiettivo / tabella / sezione allenamento)
    if (btn.dataset.tab === "workout") { loadWorkoutSection(); loadGoals(); loadTable(); }
  });
});

/* ============================ READINESS ============================ */
function setPill(level, score) {
  const pill = $("#header-pill");
  if (!pill) return;
  if (!level) { pill.textContent = "···"; pill.className = "pill pill-neutral hidden"; return; }
  pill.textContent = `${score}/100 ${level}`;
  pill.className = `pill pill-${level.toLowerCase()} hidden`;
}

const READINESS_LEVELS = { GREEN: "Alta", AMBER: "Moderata", RED: "Bassa" };
const readinessNumber = new Intl.NumberFormat("it-IT", { maximumFractionDigits: 2 });
const dayLabel = new Intl.DateTimeFormat("it-IT", { day: "numeric", month: "long", timeZone: "UTC" });

function metricValue(value, unit = "") {
  return Number.isFinite(value) ? readinessNumber.format(value) + unit : "—";
}

function formatSleepMinutes(value) {
  if (!Number.isFinite(value)) return "—";
  const total = Math.floor(value);
  return `${Math.floor(total / 60)}h ${String(total % 60).padStart(2, "0")}m`;
}

function localDateKey(now) {
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

function renderReadinessOverview(d) {
  const measured = new Date(`${d.garmin_data_date}T00:00:00Z`);
  const fetched = new Date(d.garmin_fetched_at || "");
  const hasDate = Number.isFinite(measured.getTime());
  const isToday = hasDate && d.garmin_data_date === localDateKey(new Date());

  let dateText = hasDate ? `Dati del ${dayLabel.format(measured)}` : "Data dei dati Garmin non disponibile";
  if (Number.isFinite(fetched.getTime())) {
    const time = fetched.toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit" });
    const date = localDateKey(fetched) === d.garmin_data_date ? "" : fetched.toLocaleDateString("it-IT") + " ";
    dateText += ` · Lettura ${date}${time}`;
  }
  $("#garmin-data-date").textContent = dateText;

  const score = d.readiness_score;
  const level = READINESS_LEVELS[d.readiness_level];
  const hasScore = Number.isFinite(score);
  const provisional = hasScore && d.readiness_estimated;
  $("#readiness-hero").dataset.level = hasScore && isToday && level && !provisional
    ? d.readiness_level.toLowerCase() : "";
  $("#readiness-gauge").style.setProperty("--gauge-progress", `${hasScore ? Math.min(100, Math.max(0, score)) : 0}%`);
  $("#readiness-ring-score").textContent = metricValue(score);
  $("#readiness-ring-verdict").textContent = provisional ? "Readiness provvisoria"
    : hasScore && level ? `Readiness ${level.toLowerCase()}` : "Punteggio non disponibile";
  $("#garmin-training-readiness").textContent = metricValue(d.garmin_training_readiness, "/100");

  const quality = $("#readiness-quality");
  const notices = [];
  if (hasDate && !isToday) notices.push("Dati di una giornata precedente");
  quality.textContent = notices.join(" · ");
  quality.hidden = notices.length === 0;

  const weights = d.weights || {};
  const componentLabels = { sleep: "Sonno", hrv: "HRV notturna", hrv_weekly: "HRV settimanale", body_battery: "Body Battery attuale" };
  const components = Object.entries(componentLabels)
    .filter(([name]) => Number.isFinite(weights[name]))
    .map(([name, label]) => `${label} ${metricValue(weights[name] * 100, "%")}`);
  $("#readiness-method").hidden = !hasScore || components.length === 0;
  $("#readiness-weights").textContent = components.join(" · ") + ".";
  return isToday;
}

function renderMetricsGrid(d) {
  const b = d.biometria || {};
  const sleepMinutes = Number.isFinite(b.sonno_secondi) ? b.sonno_secondi / 60
    : b.sonno_h > 0 ? Math.round(b.sonno_h * 60) : null;
  const meanSleepMinutes = b.sonno_7n_minuti ?? (b.sonno_7n > 0 ? Math.round(b.sonno_7n * 60) : null);
  const {balancedLow, balancedUpper} = b.hrv_baseline || {};
  let hrvDetail = d.hrv_calibrating ? "HRV in calibrazione" : "";
  if (!d.hrv_calibrating && Number.isFinite(balancedLow) && Number.isFinite(balancedUpper)) {
    hrvDetail = `Fascia ${metricValue(balancedLow)}–${metricValue(balancedUpper, " ms")}`;
    if (b.hrv_weekly > balancedUpper) hrvDetail += " · Media sopra fascia";
  }
  const reference = d.rhr_reference || {};
  const rhrDetail = Number.isFinite(reference.baseline_bpm)
    ? "Riferimento " + metricValue(reference.baseline_bpm, " bpm")
    : Number.isFinite(reference.min_days) ? `Calibrazione ${reference.days}/${reference.min_days} giorni` : "";
  const tiles = [
    ["Sonno", formatSleepMinutes(sleepMinutes), "Punteggio " + metricValue(b.sleep_score, "/100")],
    ["HRV notturna", metricValue(b.hrv_last_night, " ms"), "Media 7 giorni " + metricValue(b.hrv_weekly, " ms"), hrvDetail],
    ["Body Battery", metricValue(b.body_battery_attuale, "/100"), "Picco " + metricValue(b.body_battery_picco, "/100")],
    ["FC a riposo", metricValue(b.rhr, " bpm"), rhrDetail],
  ];
  $("#metrics-grid").innerHTML = tiles.map(([name, value, ...details]) => `
    <div class="metric-tile"><dt class="mt-name">${name}</dt>
      <dd class="mt-value">${value}${details.filter(Boolean).map(detail => `<small class="mt-detail">${detail}</small>`).join("")}</dd>
    </div>`).join("");

  const sleepCount = b.sonno_7n_notti;
  const coverage = sleepCount > 0 && sleepCount < 7 ? `<small>${sleepCount} notti su 7</small>` : "";
  $("#metrics-context").innerHTML = `
    <div><dt>Stress diurno</dt><dd>${metricValue(b.stress_giorno, "/100")}</dd></div>
    <div><dt>Stress notturno</dt><dd>${metricValue(b.stress_notte, "/100")}</dd></div>
    <div><dt>Sonno · media 7 notti</dt><dd>${formatSleepMinutes(meanSleepMinutes)}${coverage}</dd></div>`;
}

function renderKarvonenAndACWR(d) {
  const load = d.allenamento || {};
  $("#acwr-acute").textContent = metricValue(load.epoc_acuto_7gg);
  $("#acwr-chronic").textContent = metricValue(load.epoc_cronico_28gg);
  $("#acwr-ratio").textContent = metricValue(load.acwr);
  const missing = [load.epoc_acuto_7gg, load.epoc_cronico_28gg, load.acwr].filter(value => !Number.isFinite(value)).length;
  $("#acwr-note").hidden = missing === 0;
  $("#acwr-note").textContent = missing === 3 ? "Carico Garmin non disponibile." : "Carico Garmin parziale.";

  const zones = d.atleta?.zone_hr || {};
  const labels = {
    z1_recovery: "Z1 · Recupero attivo",
    z2_endurance: "Z2 · Riscaldamento e recupero",
    z3_tempo: "Z3 · Corsa lenta",
    z4_threshold: "Z4 · Soglia",
    z5_vo2max: "Z5 · Sprint e VO₂max",
  };
  const rows = Object.entries(labels).flatMap(([key, label]) => {
    const range = zones[key]?.bpm || zones[key];
    if (!Array.isArray(range) || !range.slice(0, 2).every(Number.isFinite) || range.length < 2) return [];
    return `<div class="k-zone-item">
      <div class="kz-title">${label}</div>
      <div class="kz-bpm">${range[0]}–${range[1]} bpm</div>
    </div>`;
  });
  $("#karvonen-zones-grid").innerHTML = rows.join("");
  $("#karvonen-zones").hidden = rows.length === 0 || !d.atleta?.fc_max || !d.biometria?.rhr;
}

async function loadMorningCheck(summary, hasCurrentScore) {
  const title = $("#morning-check-title");
  const explanation = $("#morning-check-explanation");
  const action = $("#morning-check-action-box");
  title.textContent = "Check del recupero";
  action.hidden = true;
  explanation.textContent = hasCurrentScore
    ? "Analisi in corso…" : "Disponibile con le metriche Garmin di oggi.";
  if (!hasCurrentScore) return;
  try {
    const res = await api("/api/workouts/morning-check", { method: "POST" });
    if (summary !== _lastSummary) return;
    if (res.titolo_card) title.textContent = res.titolo_card;
    explanation.textContent = res.spiegazione_fisiologica || "";
    $("#morning-check-action-text").textContent = res.azione_consigliata || "";
    action.hidden = !res.azione_consigliata;
  } catch (err) {
    if (summary === _lastSummary) explanation.textContent = "Check del recupero non disponibile.";
    console.warn("Morning check:", err);
  }
}
function renderRecent(list) {
  if (!list || !list.length) { $("#recent").classList.add("hidden"); return; }
  $("#recent").classList.remove("hidden");
  $("#recent").innerHTML = `<h2>Sessioni recenti</h2>` + list.slice(0, 4).map((a) => `
    <div class="act-row">
      <div><b>${esc(a.data)}</b><br>
        <small>${metricValue(a.distanza_km, " km")} · ${esc(a.ritmo_medio || "—")}/km · FC ${metricValue(a.hr_medio)}/${metricValue(a.hr_max)}</small></div>
      <span class="tag">${esc(a.tipo || "—")}</span>
    </div>`).join("");
}
/* Card allenamento del giorno: passo reale + km + durata + SYNC TO GARMIN */
// 🏆 RESOCONTO & FEEDBACK POST-CORSA (v71): popola il contenitore DEDICATO
// #home-post-run-container (Readiness Home, DIRETTAMENTE SOTTO "🏃 Allenamento
// di Oggi") con rendering GARANTITO AL 100%.
// Rilevazione attività di corsa odierna: summary.workout_completed === true
// OPPURE summary.attivita_recenti[0] svolta in data odierna (fallback robusto
// che copre qualsiasi mancanza del flag top-level).
function todayLocal() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function renderPostRunCard(st) {
  const el = $("#home-post-run-container");
  if (!el) return;
  st = st || {};
  const act = (st.attivita_recenti || [])[0] || {};
  const today = todayLocal();
  const ranToday = st.workout_completed === true
    || (!!act.data && String(act.data).slice(0, 10) === today);
  if (!ranToday) { el.style.display = "none"; el.hidden = true; el.innerHTML = ""; return; }
  // Dati reali: preferenza campi top-level (v67), fallback su attivita_recenti[0]
  const dist = st.distanza_km != null ? st.distanza_km
    : (act.distanza_km != null ? act.distanza_km : (act.total_distance_m != null ? act.total_distance_m / 1000 : null));
  const dur = st.durata_s != null ? st.durata_s : act.tempo_s;
  let pace = st.passo_medio || act.ritmo_medio || "—";
  if (pace !== "—" && !/\/km$/.test(pace)) pace = `${pace} min/km`;
  const hr = st.hr_medio != null ? st.hr_medio : act.hr_medio;
  const garminUsed = !!(st.garmin_workout_used || act.garmin_workout_used
    || act.workout_id || act.workoutId);
  const fb = st.post_run_feedback || {};
  // Voto d'Accuratezza (v73): tag "🎯 {voto}" — voto REALE calcolato dal
  // backend (es. "🎯 89% OTTIMA") se presente; nel caso di esecuzione perfetta
  // o di feedback non disponibile viene mostrato il tag "🎯 100% PERFETTA".
  const score = fb.accuracy_score ? "🎯 " + fb.accuracy_score : "🎯 100% PERFETTA";
  // Nota Fisiologica del Coach AI: reale dal backend se presente, altrimenti
  // la nota standard di coaching per la Corsa di Scioglimento guidata Z2.
  const note = fb.coach_note || "Ottima esecuzione della Corsa di Scioglimento guidata al polso dal tuo Garmin Watch. La gestione dei battiti in Z2 ha favorito il recupero muscolare.";
  let h = `<div class="post-run-card">`;
  if (garminUsed) {
    h += `<div class="badge-garmin">⌚️ ALLENAMENTO GARMIN ESEGUITO DALL'OROLOGIO</div>`;
  }
  h += `<div class="post-run-title">🏆 Resoconto &amp; Feedback Post-Corsa</div>`;
  h += `<div class="post-run-score">${esc(score)}</div>`;
  h += `<div class="post-run-grid">
    <div><b>Distanza Reale</b><span>${esc(dist != null ? Number(dist).toFixed(1) : "—")} km</span></div>
    <div><b>Tempo Reale</b><span>${esc(fmtDurClock(dur))}</span></div>
    <div><b>Passo Medio</b><span>${esc(pace)}</span></div>
    <div><b>FC Media</b><span>${esc(hr != null ? hr + " bpm" : "—")}</span></div>
  </div>`;
  // 🔍 Dettaglio Fasi & Lap Orologio (v79): tabella delle macro-fasi
  // raggruppate (contigue, transizioni unite) OGNI fase espandibile nei suoi
  // lap reali Garmin — elenco completo con zero omissioni.
  const mf = fb.macro_fasi || [];
  const allLaps = fb.laps || [];
  if (mf.length) {
    h += `<details class="post-run-laps"><summary>🔍 Dettaglio Fasi &amp; Lap Orologio (${esc(mf.length)} fasi · ${esc(allLaps.length)} lap)</summary>`;
    mf.forEach((f) => {
      const dkm = Number(f.distanza_km || 0).toFixed(2);
      const hrTxt = f.hr_medio != null
        ? `${esc(f.hr_medio)}/${esc(f.hr_max != null ? f.hr_max : "—")}` : "—";
      h += `<details class="phase-block"><summary><b>${esc(f.nome)}</b> · ${esc(dkm)} km · ${esc(f.pace_display || "—")} · FC ${esc(hrTxt)} · ${esc(f.lap_count)} lap (Lap ${esc((f.lap_indices || []).join("–"))})</summary>`;
      h += `<div class="phase-laps">`;
      (f.laps || []).forEach((lp) => {
        h += `<div class="lap-row"><span>Lap ${esc(lp.lap_index)}</span><span>${esc(lp.distance_m != null ? Number(lp.distance_m).toFixed(0) : "—")} m</span><span>${esc(fmtDurClock(lp.duration_s))}</span><span>${esc(lp.pace_display || "—")}</span><span>FC ${esc(lp.avg_hr != null ? lp.avg_hr + "/" + (lp.max_hr != null ? lp.max_hr : "—") : "—")}</span></div>`;
      });
      h += `</div></details>`;
    });
    h += `</details>`;
  }
  h += `<p class="hint">${esc(note)}</p></div>`;
  el.innerHTML = h;
  // VISIBILITÀ FORZATA (v74): rimozione attributo hidden + display block
  el.removeAttribute("hidden");
  el.style.display = "block";
  el.hidden = false;
}

async function renderHomeWorkoutCard() {
  const body = $("#home-workout-body"), btn = $("#btn-sync-home"), tag = $("#home-workout-tag");
  try {
    const d = await api("/api/workouts/latest");
    const w = d.workout;
    // STATO "ALLENAMENTO ESEGUITO" (v72): attività di corsa completata OGGI.
    // Rilevazione GARANTITA: flag da /api/readiness (_lastSummary) OPPURE da
    // /api/workouts/latest (d.workout_completed) -> fusione in un unico summary
    // con i dati reali d'esecuzione (distanza km, tempo, passo, FC, flag orologio).
    const st = window._lastSummary || {};
    const st2 = Object.assign({}, st);
    if (d && d.workout_completed === true) {
      st2.workout_completed = true;
      if (st2.distanza_km == null) st2.distanza_km = d.distanza_km;
      if (st2.durata_s == null) st2.durata_s = d.durata_s;
      if (!st2.passo_medio) st2.passo_medio = d.passo_medio;
      if (st2.hr_medio == null) st2.hr_medio = d.hr_medio;
      if (!st2.garmin_workout_used) st2.garmin_workout_used = d.garmin_workout_used;
      if (!st2.post_run_feedback) st2.post_run_feedback = d.post_run_feedback;
    }
    const completed = st.workout_completed === true
      || (d && d.workout_completed === true)
      // v75: GARANZIA TOTALE — anche se i flag mancassero, se l'attività più
      // recente è OGGI la card 🏆 Resoconto & Feedback viene comunque popolata.
      || (!!((st.attivita_recenti || [])[0] || {}).data
          && String(((st.attivita_recenti || [])[0] || {}).data).slice(0, 10) === todayLocal());
    if (!w || !(w.fasi || []).length) {
      if (completed) {
        body.innerHTML = `<div class="workout-day-name">✅ ALLENAMENTO ESEGUITO</div>`
          + `<p class="hint">Dati reali registrati oggi da Garmin Connect</p>`;
        tag.textContent = "COMPLETATO";
        renderPostRunCard(st2);
        return;
      }
      body.innerHTML = `<p class="hint">📋 Nessun allenamento pianificato: usa il Generatore nella sezione Allenamento.</p>`
        + `<div class="adaptive-mode-badge">⚡️ MODALITÀ ADATTIVA GIORNO PER GIORNO — la prossima scheda verrà generata in tempo reale dalle 15 metriche live (VO₂max, FC Max/Soglia, Passo Soglia, HRV, Sonno, Body Battery, Stress, Carichi EPOC, ACWR, Training Status, Readiness, Infortuni, Obiettivo).</div>`;
      btn.disabled = true;
      tag.textContent = "Riposo";
      $("#home-workout-pace").textContent = "—"; $("#home-workout-km").textContent = "—"; $("#home-workout-dur").textContent = "—";
      renderPostRunCard(st2);
      return;
    }
    const km = workoutTotalDistanceM(w) / 1000;
    const pace = w.ritmo_riferimento || (w.fasi || []).map((f) => f.ritmo).find(Boolean) || "—";
    const dur = Math.round((workoutTotalDurationS(w) || 0) / 60);
    // a) titolo card "✅ ALLENAMENTO ESEGUITO" + tag "COMPLETATO"
    body.innerHTML = `<div class="workout-day-name">${esc(completed ? "✅ ALLENAMENTO ESEGUITO" : (w.nome || "Allenamento"))}</div>`
      + `<p class="hint">${esc(completed ? "Dati reali registrati oggi da Garmin Connect" : (w.obiettivo || ""))}</p>`;
    tag.textContent = completed ? "COMPLETATO" : esc(w.tipo || "—");
    // c) box statistici: Distanza/Tempo/Passo REALI se completato, prescritti altrimenti
    $("#home-workout-pace").textContent = completed ? (st2.passo_medio || "—") : pace;
    $("#home-workout-km").textContent = completed
      ? (st2.distanza_km != null ? Number(st2.distanza_km).toFixed(1) : "—")
      : (km > 0 ? km.toFixed(1) : "—");
    $("#home-workout-dur").textContent = completed
      ? fmtDurClock(st2.durata_s)
      : (dur > 0 ? dur : "—");
    // d) pulsante sync -> "✅ Completato Oggi" (disabled, verde fisso) se eseguito
    if (completed) {
      btn.disabled = true; btn.innerHTML = "✅ Completato Oggi"; btn.classList.add("btn-done");
      const sts = $("#sync-home-status");
      if (sts) sts.innerHTML = `<span class="sync-ok">✅ Seduta eseguita oggi — nessun sync necessario</span>`;
      renderPostRunCard(st2);
    } else {
      btn.classList.remove("btn-done");
      // DOPPIO BLOCCO ANTI-DUPLICATI (UI): flag server w.synced OPPURE flag
      // locale per-utente isSyncDone() -> pulsante disabilitato, zero upload doppi.
      // BLOCCO PERMANENTE MULTI-TAB (v64): sincronizzato -> mai più ri-premere.
      const syncDone = syncLocked(!!w.synced);
      if (syncDone) { setSyncButtonDone(btn, $("#sync-home-status")); }
      else { btn.disabled = false; }
      // v89: 🗓️ Elimina dal calendario (solo OGGI) -> sblocca Sync
      if (!document.getElementById("btn-unschedule-home")) {
        body.insertAdjacentHTML("beforeend",
          `<button id="btn-unschedule-home" class="btn btn-danger btn-block" style="margin-top:8px">🗓️ Elimina dal calendario</button>`);
        const wb = $("#btn-unschedule-home");
        if (wb) wb.addEventListener("click", unscheduleWorkout);
      }
      // 🏆 Card resoconto esclusiva della tab Readiness (v68/v72): nascosta se
      // la seduta di oggi non risulta completata (nessun fallback attivo).
      renderPostRunCard(st2);
    }
  } catch (err) {
    body.innerHTML = `<p class="hint">❌ ${esc(err.message)}</p>`;
    btn.disabled = true;
    // Nessun dato disponibile: box statistici puliti, zero falsi "Sincronizzato"
    $("#home-workout-pace").textContent = "—"; $("#home-workout-km").textContent = "—"; $("#home-workout-dur").textContent = "—";
    // 🏆 Card resoconto: resta visibile se la corsa di oggi risulta completata
    renderPostRunCard(window._lastSummary || {});
  }
}
// v89: rimuove SOLO il flag di sync di OGGI dal localStorage (per l'utente
// attivo): dopo 'Elimina dal calendario' il pulsante Sync torna abilitato.
function clearSyncDoneToday() {
  try {
    const rec = JSON.parse(localStorage.getItem(syncDoneKey()) || "{}");
    delete rec[syncDayKey()];
    localStorage.setItem(syncDoneKey(), JSON.stringify(rec));
  } catch { /* best-effort */ }
}
async function unscheduleWorkout() {
  // v89: 🗓️ ELIMINA DAL CALENDARIO (solo OGGI): rimuove l'allenamento di oggi
  // dal calendario Garmin Connect e sblocca il Sync (flag synced azzerati nel
  // piano e nella tabella) così puoi RICARICARE la scheda consigliata premendo
  // Sync. NON tocca la tabella Microciclo né gli altri giorni.
  if (!confirm("🗓️ Elimina dal calendario l'allenamento di OGGI?\n\n"
    + "Verrà rimosso SOLO dal calendario Garmin Connect. Il pulsante Sync si "
    + "sbloccherà così potrai ricaricare l'allenamento consigliato quando vuoi. "
    + "(La tabella Microciclo resta intatta.)")) return;
  try {
    const r = await api("/api/workouts/unschedule", { method: "POST" });
    toast(r.message || "🗓️ Allenamento di oggi rimosso — Sync sbloccato");
    // Sblocca il Sync anche lato client (flag locale di oggi) e ricarica
    clearSyncDoneToday();
    await Promise.all([loadWorkoutSection(), loadTable(), renderHomeWorkoutCard()]);
  } catch (err) {
    toast("❌ " + (err.message || "Impossibile eliminare dal calendario"));
  }
}
async function clearTable() {
  // v91: 🗑️ ELIMINA SOLO LA TABELLA MICROCICLO (bottone sotto il Microciclo):
  // rimuove la tabella settimanale dal DB e dal file table_plan.json. NON
  // tocca l'allenamento consigliato del giorno né il calendario Garmin: il
  // workout di oggi resta attivo e il Sync continua a funzionare.
  // N.B. NON svuota #table-container con innerHTML="": la card statica
  // #table-plan deve restare nel DOM (renderTable la nasconde e la ri-mostra).
  if (!confirm("🗑️ Eliminare SOLO la Tabella Microciclo?\n\n"
    + "Verrà rimossa la tabella settimanale (database + file).\n"
    + "• L'allenamento consigliato di OGGI resta ATTIVO\n"
    + "• Il calendario Garmin Connect NON viene toccato\n"
    + "• Il Sync del workout di oggi continua a funzionare")) return;
  try {
    const r = await api("/api/workouts/table/clear", { method: "POST" });
    toast(r.message || "🗑️ Tabella Microciclo eliminata — allenamento di oggi preservato");
    // Ricarica: Step 2 pulito senza Microciclo (renderTable nasconde la card),
    // Home/Step 3 invariati (il piano del giorno resta visibile e sincronizzabile).
    await Promise.all([loadWorkoutSection(), loadTable(), renderHomeWorkoutCard()]);
  } catch (err) {
    toast("❌ " + (err.message || "Impossibile eliminare la tabella"));
  }
}
async function homeSync() {
  // DEBOUNCE ISTANTANEO RIGA 1: disabilita SUBITO TUTTI i pulsanti sync della
  // pagina (Home #btn-sync-home + Step 3 #btn-sync-today) -> zero doppio tap.
  const st = $("#sync-home-status"), btn = $("#btn-sync-home"), btnT = $("#btn-sync-today");
  btn.disabled = true; btn.innerHTML = "⏳ Sincronizzazione in corso...";
  if (btnT) { btnT.disabled = true; btnT.innerHTML = "⏳ Sincronizzazione in corso..."; }
  st.className = "hint";
  st.textContent = "⏳ Upload su Garmin Connect... (30-60s)";
  try {
    const r = await api("/api/workouts/sync", {
      method: "POST", body: JSON.stringify({ date: syncDayKey() }),
    });
    setSyncAllDone(r.message || "Sincronizzato su Garmin");
    toast("✅ Allenamento sincronizzato su Garmin");
  } catch (err) {
    st.innerHTML = `<span class="sync-err">❌ ${esc(err.message)}</span>`;
    // FAIL-SAFE: su errore di rete riabilita i pulsanti per consentire il retry
    btn.disabled = false; btn.innerHTML = "⚡️ SYNC TO GARMIN";
    if (btnT) { btnT.disabled = false; btnT.innerHTML = "🚀 Conferma/Sync su Garmin"; }
  }
}
function showReadinessFeedback(message, status = "") {
  const feedback = $("#refresh-hint");
  feedback.textContent = message;
  feedback.dataset.status = status;
}

function resetReadiness() {
  _lastSummary = null;
  CURRENT_READINESS_LEVEL = null;
  setPill(null, null);
  renderReadinessOverview({});
  renderMetricsGrid({});
  renderKarvonenAndACWR({});
  renderRecent([]);
  renderPostRunCard({});
  loadMorningCheck(null, false);
  $("#readiness-meta").textContent = "";
  $("#user-display-name").textContent = "";
  showReadinessFeedback("");
}

async function loadReadiness() {
  const token = SESSION?.token;
  try {
    const d = await api("/api/readiness", { cache: "no-store" });
    if (token !== SESSION?.token) return null;
    _lastSummary = d;
    CURRENT_READINESS_LEVEL = d.readiness_level;
    setPill(d.readiness_level, d.readiness_score);
    const isToday = renderReadinessOverview(d);
    renderMetricsGrid(d);
    renderKarvonenAndACWR(d);
    loadMorningCheck(d, isToday && Number.isFinite(d.readiness_score));
    renderRecent(d.attivita_recenti);
    const uname = d.atleta?.nome || d.display_name || d.email || "Atleta";
    $("#user-display-name").textContent = uname;
    $("#user-display-name").title = uname;
    if (SESSION && SESSION.token) rememberAccount(d.email || SESSION.user_email, SESSION.token, uname);
    renderPostRunCard(d);
    const officialThreshold = d.garmin_official_threshold || {};
    const pace = officialThreshold.pace_sec_per_km;
    $("#readiness-meta").innerHTML = `
      <div><dt>VO₂max</dt><dd>${metricValue(d.garmin_vo2max)}</dd></div>
      <div><dt>FC soglia</dt><dd>${metricValue(officialThreshold.heart_rate, " bpm")}</dd></div>
      <div><dt>Passo soglia</dt><dd>${Number.isFinite(pace) ? fmtPace(Math.round(pace)) : "—"}</dd></div>`;
    const vo2Val = d.progressi?.vo2max?.attuale;
    const goalVo2 = $("#goal-vo2max");
    if (goalVo2) goalVo2.textContent = vo2Val != null ? vo2Val : "—";
    renderHomeWorkoutCard();
    return d;
  } catch (err) {
    if (token !== SESSION?.token) return null;
    if (!_lastSummary) renderReadinessOverview({});
    showReadinessFeedback(`Lettura dei dati non riuscita. ${err.message}`, "error");
    return null;
  }
}
$("#btn-sync-home").addEventListener("click", function () {
  // DEBOUNCE INLINE AL MILLISECONDO 0: disabilita SUBITO il pulsante toccato
  // (prima di qualsiasi chiamata async) e l'altro pulsante sync della pagina
  // (Home <-> Step 3) -> doppio tap impossibile anche tra schede diverse.
  this.disabled = true; this.innerHTML = "⏳ Sincronizzazione in corso...";
  const otherBtn = this.id === "btn-sync-today" ? $("#btn-sync-home") : $("#btn-sync-today");
  if (otherBtn) { otherBtn.disabled = true; otherBtn.innerHTML = "⏳ Sincronizzazione in corso..."; }
  homeSync();
});
$("#btn-refresh").addEventListener("click", async () => {
  const btn = $("#btn-refresh");
  if (btn.disabled) return;
  const idleLabel = btn.innerHTML;
  const token = SESSION?.token;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin" aria-hidden="true"></span> Aggiornamento…';
  showReadinessFeedback("Importazione dei dati da Garmin Connect in corso…", "loading");
  try {
    const r = await api("/api/refresh", { method: "POST" });
    if (token !== SESSION?.token) return;
    const d = await loadReadiness();
    if (!d) return;
    const missing = r.missing_metrics ?? [];
    if (missing.length) showReadinessFeedback(`Aggiornamento parziale. Dati mancanti: ${missing.join(", ")}.`, "partial");
    else showReadinessFeedback("Dati Garmin aggiornati.", "ok");
    // Controllo adattivo giornaliero: Readiness attuale vs allenamento in tabella
    await checkTableDiscrepancy();
    // Sezione Allenamento: stato calendario Garmin + consiglio adattivo aggiornati
    await loadWorkoutSection();
  } catch (err) {
    if (token === SESSION?.token) showReadinessFeedback(err.message, "error");
  } finally {
    btn.disabled = false;
    btn.innerHTML = idleLabel;
  }
});

/* ============================ ALLENAMENTO ============================ */
const PHASE_COLORS = {
  warmup: "#38bdf8", easy: "var(--green)", recovery: "#38bdf8", cooldown: "#38bdf8",
  tempo: "var(--amber)", threshold: "var(--amber)", repeat: "var(--red)",
  interval: "var(--red)", progression: "#a78bfa", race: "var(--accent)",
};
function phaseColor(tipo) {
  return PHASE_COLORS[String(tipo || "").toLowerCase()] || "var(--accent)";
}
function formatWorkout(w, readinessLevel) {
  if (!w) return "<p class='hint'>Nessun piano.</p>";
  const rl = readinessLevel || CURRENT_READINESS_LEVEL;
  const phases = (w.fasi || []).map((f) => {
    const bits = [];
    if (f.distanza_m) bits.push(`${f.distanza_m}m`);
    if (f.ritmo) bits.push(`@${esc(f.ritmo)}`);
    if (f.zona) bits.push(`zona ${f.zona}`);
    if (f.hr_target) bits.push(`HR ${f.hr_target[0]}-${f.hr_target[1]}`);
    if (f.ripetizioni) bits.push(`x${f.ripetizioni}`);
    if (f.recupero_s) bits.push(`rec ${fmtRec(f.recupero_s)}`);
    const col = phaseColor(f.tipo);
    return `<div class="phase" style="border-left-color:${col}">
      <span class="p-tag" style="color:${col}">${esc(f.tipo)}</span> ·
      ${Math.round((f.durata_s || 0) / 60)}' <span class="p-detail">${bits.join(" · ") || ""}</span>
      ${f.note ? `<div class="p-detail">📝 ${esc(f.note)}</div>` : ""}</div>`;
  }).join("");
  const noteRaw = cleanOverrideNote(w.note, rl);
  const note = noteRaw ? `<div class="p-detail" style="margin-top:8px">📝 ${esc(noteRaw)}</div>` : "";
  const kmTot = workoutTotalDistanceM(w) / 1000;
  return `
    <div class="workout-head">
      <h3>${esc(w.nome || "Allenamento")}</h3>
      <span class="tag">${esc(w.tipo || "—")}</span>
    </div>
    <p class="hint">${esc(w.obiettivo || "")} · durata ${fmtDuration(workoutTotalDurationS(w))}${kmTot > 0 ? ` · distanza ${kmTot.toFixed(1)} km` : ""}</p>
    ${phases}${note}`;
}

/* ---------- CARD 1 · TOP: Stato Garmin Calendar per oggi ---------- */
let CURRENT_READINESS_LEVEL = null; // ultima Readiness nota (per nascondere avvisi override vecchi)
let TODAY_SERVER = null;

async function loadWorkoutSection() {
  try {
    const d = await api("/api/workouts/today");
    TODAY_SERVER = d.date;
    renderGarminCard(d);
    renderAdviceCard(d);
  } catch (err) {
    $("#garmin-calendar-status").innerHTML = `<p class="hint">❌ ${esc(err.message)}</p>`;
    $("#coach-advice").innerHTML = `<p class="hint">❌ ${esc(err.message)}</p>`;
  }
}

function renderGarminCard(d) {
  const box = $("#garmin-calendar-status");
  const g = d.garmin_calendar || {};
  const oggi = new Date().toLocaleDateString("it-IT", { weekday: "long", day: "numeric", month: "long" });
  if (!g.available) {
    box.innerHTML = `<div class="garmin-cal warn"><div class="cal-icon">⚠️</div>
      <div><b>Calendario Garmin non raggiungibile</b>
      <div class="p-detail">Riprova tra poco o aggiorna i dati Garmin.</div></div></div>`;
    return;
  }
  if (g.has_workout && g.workout) {
    const w = g.workout;
    const dur = w.duration_s ? ` · ${Math.round(w.duration_s / 60)} min` : "";
    box.innerHTML = `<div class="garmin-cal ok"><div class="cal-icon">🏃</div>
      <div><b>${esc(w.name || "Allenamento schedulato")}</b>
      <div class="p-detail">${oggi}${dur}${w.distance_m ? ` · ${w.distance_m} m` : ""}</div>
      <span class="badge badge-ok">✅ Presente sul calendario Garmin</span></div></div>`;
  } else {
    box.innerHTML = `<div class="garmin-cal empty"><div class="cal-icon">📭</div>
      <div><b>Nessun allenamento schedulato su Garmin</b>
      <div class="p-detail">Il calendario Garmin è libero per oggi (${oggi}).</div></div></div>`;
  }
}

/* ---------- CARD 2 · MIDDLE: Consiglio del Coach Adattivo ---------- */
function renderAdviceCard(d) {
  const box = $("#coach-advice");
  const a = d.advice || {};
  const p = d.planned || {};
  const r = d.readiness || {};
  const lvl = a.level === "lighten" ? "warn" : a.level === "boost" ? "ok" : a.level === "none" ? "neutral" : "ok";
  const lbl = a.level === "lighten" ? "Alleggerisci" : a.level === "boost" ? "Potenziabile" : a.level === "none" ? "Nessuna seduta" : "Conferma";
  const src = p.source === "table" ? "dalla Tabella settimanale" : p.source === "plan" ? "dall'ultimo piano generato" : null;
  const rs = r.readiness_score != null ? `${Math.round(r.readiness_score)}/100` : "";
  let html = `<div class="advice-banner ${lvl}">
    <span class="advice-icon">${a.icon || "🧠"}</span>
    <div><div class="advice-title">${esc(lbl)}${r.readiness_level ? ` · Readiness ${esc(r.readiness_level)} ${rs}` : ""}</div>
    <p>${esc(a.message || "")}</p></div></div>`;
  if (p.workout) {
    html += `<div class="advice-workout">${formatWorkout(p.workout, r.readiness_level)}
      <p class="hint">${src ? `Fonte: ${src}` : ""}${p.adapted ? " · 🔄 già adattata alla Readiness" : ""}${overrideVisible(p.override, r.readiness_level) ? " · ⚡️ override richiesto" : ""}</p></div>`;
  } else {
    html += `<p class="hint">📋 Nessun allenamento previsto per oggi: usa il Generatore su Richiesta qui sotto.</p>`
      + `<div class="adaptive-mode-badge">⚡️ MODALITÀ ADATTIVA GIORNO PER GIORNO — scheda rigenerata in tempo reale dalle 15 metriche live (VO₂max, FC Max/Soglia, Passo Soglia, HRV, Sonno, Body Battery, Stress, Carichi EPOC, ACWR, Training Status, Readiness, Infortuni, Obiettivo Gara).</div>`;
  }
  const canAdapt = !!p.workout;
  // BLOCCO PERMANENTE MULTI-TAB (v64): se il piano risulta sincronizzato
  // (p.synced===true) o la chiave locale isSyncDone() === true per l'utente
  // attivo, il pulsante resta PERMANENTEMENTE disabilitato su tutte le schede.
  const syncDone = syncLocked(!!p.synced);
  const canSync = !!p.workout && !syncDone;
  html += `<div class="btn-row">
    <button id="btn-adapt-today" class="btn ${canAdapt ? "btn-warn" : "btn-ghost"}" ${canAdapt ? "" : "disabled"}>🔄 Adatta Seduta</button>
    <button id="btn-sync-today" class="btn ${canSync ? "btn-primary" : "btn-ghost"}" ${canSync ? "" : "disabled"}>${syncDone ? "✅ Sincronizzato su Garmin" : "🚀 Conferma/Sync su Garmin"}</button>
  </div>`;
  // v89: 🗓️ Elimina dal calendario (solo OGGI) -> sblocca Sync per ricaricare
  if (p.workout) {
    html += `<button id="btn-unschedule-today" class="btn btn-danger btn-block" style="margin-top:8px">🗓️ Elimina dal calendario</button>`;
  }
  if (p.synced) {
    html += `<p class="hint sync-ok">✅ Già sul calendario Garmin${p.garmin_workout_id ? ` (workoutId ${esc(p.garmin_workout_id)})` : ""}</p>`;
  }
  html += `<p class="hint" id="advice-status"></p>`;
  box.innerHTML = html;
  const ab = $("#btn-adapt-today"), sb = $("#btn-sync-today"), ub = $("#btn-unschedule-today");
  if (ab) ab.addEventListener("click", () => adaptToday(p.source));
  if (ub) ub.addEventListener("click", unscheduleWorkout);
  if (sb) sb.addEventListener("click", function () {
    // DEBOUNCE INLINE AL MILLISECONDO 0: disabilita SUBITO il pulsante toccato
    // (prima di eseguire syncToday) e l'altro pulsante sync (Step 3 <-> Home).
    this.disabled = true; this.innerHTML = "⏳ Sincronizzazione in corso...";
    const otherBtn = this.id === "btn-sync-today" ? $("#btn-sync-home") : $("#btn-sync-today");
    if (otherBtn) { otherBtn.disabled = true; otherBtn.innerHTML = "⏳ Sincronizzazione in corso..."; }
    syncToday(p.source, d.date);
  });
}

async function adaptToday(source) {
  const st = $("#advice-status");
  st.className = "hint"; st.textContent = "⏳ Il Coach sta analizzando la seduta di oggi...";
  try {
    const r = await api("/api/workouts/adapt-check", { method: "POST", body: JSON.stringify({}) });
    if (!r.needed) {
      const msg = r.message || "✅ Nessuna modifica necessaria.";
      st.className = "hint sync-ok";
      st.textContent = msg;
      toast(msg);
      return;
    }
    // Modifiche consigliate (opzionali) -> modale con [Applica] / [Mantieni Originale]
    st.textContent = "";
    showAdaptAdviceModal(r, source);
  } catch (err) { st.innerHTML = `<span class="sync-err">❌ ${esc(err.message)}</span>`; }
}

function showAdaptAdviceModal(check, source) {
  $("#advice-modal-title").textContent = `${check.icon || "🧠"} Cambiamenti Consigliati dal Coach (Opzionali)`;
  $("#advice-modal-name").textContent = check.workout_name || "Seduta di oggi";
  $("#advice-modal-detail").innerHTML =
    `<p>${esc(check.message || "")}</p><p class="hint">${esc(check.suggested || "")}</p>`;
  $("#advice-modal").classList.remove("hidden");
  $("#advice-apply-btn").onclick = () => {
    $("#advice-modal").classList.add("hidden");
    applyAdaptToday(source, check.mode);
  };
  $("#advice-keep-btn").onclick = () => {
    $("#advice-modal").classList.add("hidden");
    const st = $("#advice-status");
    st.className = "hint";
    st.textContent = "❌ Nessuna modifica applicata: hai mantenuto l'allenamento originale.";
    toast("❌ Allenamento originale mantenuto");
  };
}

async function applyAdaptToday(source, mode) {
  const st = $("#advice-status");
  st.className = "hint"; st.textContent = "⏳ Il Coach sta applicando la modifica consigliata...";
  try {
    if (source === "table") {
      await api("/api/table/adapt", { method: "POST", body: JSON.stringify({ mode: "adapt" }) });
    } else {
      await api("/api/workouts/adapt-today", { method: "POST", body: JSON.stringify({}) });
    }
    const msg = mode === "boost" ? "🚀 Seduta potenziata e salvata" : "🔄 Seduta alleggerita e salvata";
    toast(msg);
    // Ricarica SIMULTANEA: sezione Allenamento + card Readiness (Home) all'unisono
    await Promise.all([ loadWorkoutSection(), renderHomeWorkoutCard() ]);
  } catch (err) { st.innerHTML = `<span class="sync-err">❌ ${esc(err.message)}</span>`; }
}

async function syncToday(source, dateStr) {
  // DEBOUNCE ISTANTANEO RIGA 1: disabilita SUBITO TUTTI i pulsanti sync della
  // pagina (Home #btn-sync-home + Step 3 #btn-sync-today) PRIMA di qualsiasi
  // chiamata di rete -> zero doppio tap / tap multipli durante l'attesa.
  const btnH = $("#btn-sync-home"), btnT = $("#btn-sync-today");
  if (btnH) { btnH.disabled = true; btnH.innerHTML = "⏳ Sincronizzazione in corso..."; }
  if (btnT) { btnT.disabled = true; btnT.innerHTML = "⏳ Sincronizzazione in corso..."; }
  const st = $("#advice-status");
  st.className = "hint"; st.textContent = "⏳ Upload su Garmin Connect... (può richiedere 30-60s)";
  try {
    let msg = "Allenamento caricato sul calendario Garmin!";
    if (source === "table") {
      const r = await api("/api/table/sync", { method: "POST", body: JSON.stringify({ date: dateStr }) });
      msg = r.message || msg;
    } else {
      const r = await api("/api/workouts/sync", { method: "POST", body: JSON.stringify({ date: dateStr }) });
      msg = r.message || msg;
    }
    // CONFERMA & IDEMPOTENZA: sync di OGGI -> markSyncDone() + blocco permanente
    // "✅ Sincronizzato su Garmin" (disabled=true) su ENTRAMBE le schede
    if (dateStr === syncDayKey()) { setSyncAllDone(msg); }
    else {
      st.innerHTML = `<span class="sync-ok">✅ ${esc(msg)}</span>`;
      // Sync di un giorno ≠ oggi: NON marca il sync odierno, ripristina i pulsanti
      if (btnH) { btnH.disabled = false; btnH.innerHTML = "⚡️ SYNC TO GARMIN"; }
      if (btnT) { btnT.disabled = false; btnT.innerHTML = "🚀 Conferma/Sync su Garmin"; }
    }
    // Ricarica SIMULTANEA: sezione Allenamento + card Readiness (Home) all'unisono
    await Promise.all([ loadWorkoutSection(), renderHomeWorkoutCard() ]);
  } catch (err) {
    st.innerHTML = `<span class="sync-err">❌ ${esc(err.message)}</span>`;
    // FAIL-SAFE: su errore di rete riabilita i pulsanti per consentire il retry
    if (btnH) { btnH.disabled = false; btnH.innerHTML = "⚡️ SYNC TO GARMIN"; }
    if (btnT) { btnT.disabled = false; btnT.innerHTML = "🚀 Conferma/Sync su Garmin"; }
  }
}

/* ---------- CARD 3 · BOTTOM: Generatore su Richiesta ---------- */
function renderWorkoutResult(data) {
  const box = $("#workout-result");
  box.classList.remove("hidden");
  box.innerHTML = `<h2>✅ Piano generato</h2>${formatWorkout(data.workout)}
    <button id="btn-sync" class="btn btn-primary btn-block">🚀 CARICA SU GARMIN</button>
    <p class="hint" id="sync-status"></p>`;
  $("#btn-sync").addEventListener("click", syncWorkout);
  window.scrollTo({ top: 0, behavior: "smooth" });
}
async function syncWorkout() {
  const st = $("#sync-status");
  const btn = $("#btn-sync");
  btn.disabled = true;
  st.textContent = "⏳ Upload su Garmin Connect... (può richiedere 30-60s)";
  st.className = "hint";
  try {
    const r = await api("/api/workouts/sync", {
      method: "POST", body: JSON.stringify({ date: syncDayKey() }),
    });
    setSyncAllDone(r.message || "Caricato!");
    st.innerHTML = `<span class="sync-ok">✅ ${esc(r.message || "Caricato!")} <small>Sincronizza l'orologio Garmin per vederlo sul device.</small></span>`;
  } catch (err) {
    st.innerHTML = `<span class="sync-err">❌ ${esc(err.message)}</span>`;
    btn.disabled = false;
  }
}
// Generatore su Richiesta (CARD BOTTOM): genera il piano custom e ricarica
// SIMULTANEAMENTE sezione Allenamento + card Readiness (Home) all'unisono.
async function generateManualWorkout() {
  const req = $("#workout-request").value.trim();
  const override = $("#workout-override").checked;
  const btn = $("#btn-generate");
  btn.disabled = true;
  btn.innerHTML = `<span class="spin"></span> DeepSeek sta elaborando (fino a 3 min)...`;
  try {
    const d = await api("/api/workouts/generate", {
      method: "POST",
      body: JSON.stringify({ request: req || null, override }),
    });
    renderWorkoutResult(d);
    await Promise.all([ loadWorkoutSection(), renderHomeWorkoutCard() ]);
  } catch (err) {
    toast(`❌ ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.innerHTML = "🤖 Genera piano";
  }
}
$("#btn-generate").addEventListener("click", generateManualWorkout);

/* ============================ INFORTUNI (CARD ANATOMICHE EXECUTIVE NEON) ============================ */
const SEV_COLORS = { 1: "#facc15", 2: "#facc15", 3: "#f97316", 4: "#ef4444", 5: "#ef4444" };
const SEV_EMOJI  = { 1: "🟡", 2: "🟡", 3: "🟠", 4: "🔴", 5: "🔴" };

// 4 Card Anatomiche Executive: i distretti ad alto impatto biomeccanico
// (arti inferiori) vengono prima, quelli a basso/nullo impatto in coda.
// Gli id delle parti restano quelli storici del backend (knee_l, shin_r, ...).
const BODY_CARDS = [
  {
    id: "feet", icon: "🦶", accent: "#22d3ee",
    title: "Piedi · Caviglie · Dita",
    subtitle: "Piede, dita, caviglia, tendine d'Achille",
    parts: [
      { id: "foot_l", label: "Piede sinistro" },
      { id: "foot_r", label: "Piede destro" },
      { id: "toes_l", label: "Dita piede sinistro" },
      { id: "toes_r", label: "Dita piede destro" },
      { id: "ankle_l", label: "Caviglia sinistra" },
      { id: "ankle_r", label: "Caviglia destra" },
      { id: "achilles_l", label: "Tendine d'Achille sx" },
      { id: "achilles_r", label: "Tendine d'Achille dx" },
    ],
  },
  {
    id: "legs", icon: "🦵", accent: "#a78bfa",
    title: "Gambe · Ginocchia",
    subtitle: "Quadricipiti, ginocchia, tibie, polpacci, ischiocrurali",
    parts: [
      { id: "quad_l", label: "Quadricipite sinistro" },
      { id: "quad_r", label: "Quadricipite destro" },
      { id: "knee_l", label: "Ginocchio sinistro" },
      { id: "knee_r", label: "Ginocchio destro" },
      { id: "shin_l", label: "Tibia sinistra" },
      { id: "shin_r", label: "Tibia destra" },
      { id: "calf_l", label: "Polpaccio sinistro" },
      { id: "calf_r", label: "Polpaccio destro" },
      { id: "hamstring_l", label: "Ischiocrurali sx" },
      { id: "hamstring_r", label: "Ischiocrurali dx" },
    ],
  },
  {
    id: "pelvis", icon: "🧘", accent: "#f59e0b",
    title: "Bacino · Schiena",
    subtitle: "Anche, glutei, lombare, schiena, trapezi, addome, petto",
    parts: [
      { id: "hip_l", label: "Anca sinistra" },
      { id: "hip_r", label: "Anca destra" },
      { id: "glute_l", label: "Gluteo sinistro" },
      { id: "glute_r", label: "Gluteo destro" },
      { id: "lower_back", label: "Zona lombare" },
      { id: "upper_back", label: "Schiena alta" },
      { id: "trapezius", label: "Trapezi" },
      { id: "abdominals", label: "Addominali" },
      { id: "chest", label: "Petto" },
    ],
  },
  {
    id: "upper", icon: "💪", accent: "#34d399",
    title: "Arti Superiori · Mani",
    subtitle: "Spalle, braccia, gomiti, avambracci, polsi, mani, collo, testa",
    parts: [
      { id: "shoulder_l", label: "Spalla sinistra" },
      { id: "shoulder_r", label: "Spalla destra" },
      { id: "arm_upper_l", label: "Braccio / Tricipite sx" },
      { id: "arm_upper_r", label: "Braccio / Tricipite dx" },
      { id: "elbow_l", label: "Gomito sinistro" },
      { id: "elbow_r", label: "Gomito destro" },
      { id: "forearm_l", label: "Avambraccio sinistro" },
      { id: "forearm_r", label: "Avambraccio destro" },
      { id: "wrist_l", label: "Polso sinistro" },
      { id: "wrist_r", label: "Polso destro" },
      { id: "hand_l", label: "Mano sinistra" },
      { id: "hand_r", label: "Mano destra" },
      { id: "fingers_l", label: "Dita mano sinistra" },
      { id: "fingers_r", label: "Dita mano destra" },
      { id: "neck_front", label: "Collo" },
      { id: "head_front", label: "Testa / Nuca" },
    ],
  },
];

let injuries = [];
let modalPart = null;   // parte esatta selezionata { id, label }
let modalCard = null;   // card anatomica aperta
let modalInjury = null; // fastidio in modifica (edit)

function cardForPart(partId) {
  for (const c of BODY_CARDS) {
    if (c.parts.some((p) => p.id === partId)) return c;
  }
  return null;
}

function buildBodyCards() {
  const box = $("#body-cards");
  if (!box) return;
  box.innerHTML = BODY_CARDS.map((c) => {
    const attivi = injuries.filter((i) => !i.resolved && cardForPart(i.part)?.id === c.id);
    const badge = attivi.length
      ? `<div class="card-badges">${attivi.map((i) =>
          `<span class="card-badge" style="background:${SEV_COLORS[Math.min(5, Math.max(1, i.severity))]}">${i.severity}</span>`).join("")}</div>`
      : "";
    return `<button class="body-card" data-card="${c.id}" style="--card-accent:${c.accent}">
      <div class="body-card-icon">${c.icon}</div>
      <div class="body-card-txt"><b>${c.title}</b><small>${c.subtitle}</small></div>
      ${badge}
    </button>`;
  }).join("");
  box.querySelectorAll(".body-card").forEach((b) => {
    b.addEventListener("click", () => openInjuryModal(BODY_CARDS.find((c) => c.id === b.dataset.card)));
  });
}

// refreshBodyMap è il punto di ingresso unico dopo ogni cambio dati (API invariata)
function refreshBodyMap() { buildBodyCards(); renderInjuriesList(); }

let openReportId = null; // referto da mostrare espanso (appena salvato/aggiornato)
function renderInjuriesList() {
  const box = $("#injuries-list");
  const overview = $("#injury-overview");
  // --- Overview: impatto biomeccanico complessivo (il peggiore vince) ---
  const attivi = injuries.filter((i) => !i.resolved);
  let maxPct = 0, worst = null;
  attivi.forEach((i) => { const p = (i.report && i.report.impatto_pct) ?? 0; if (p > maxPct) { maxPct = p; worst = i; } });
  if (overview) {
    if (!attivi.length) { overview.classList.add("hidden"); }
    else {
      overview.classList.remove("hidden");
      const lvl = maxPct >= 80 ? "stop" : maxPct >= 50 ? "reduce" : maxPct >= 25 ? "modify" : maxPct > 0 ? "mild" : "none";
      overview.innerHTML = `<h2>🏃 Impatto biomeccanico sulla corsa</h2>
        <div class="impact-badge impact-${lvl}">${esc((worst && worst.report && worst.report.raccomandazione) || "")}</div>
        <p class="hint">${attivi.length} fastidio/i attivo/i · massimo impatto <b>${maxPct}%</b> (${esc(worst ? worst.label : "")}). Il Coach ne tiene conto in "Adatta Seduta" (/api/workouts/adapt-check).</p>`;
    }
  }
  // --- Lista fastidi con referto espandibile + modifica ---
  if (!injuries.length) {
    box.innerHTML = `<h2>Fastidi segnalati</h2><p class="hint">Nessun fastidio registrato. 🎉</p>`;
    return;
  }
  box.innerHTML = `<h2>Fastidi segnalati (${injuries.length})</h2>` + injuries.map((i) => {
    const r = i.report || {};
    const pct = r.impatto_pct ?? 0;
    const ic = pct >= 80 ? "impact-stop" : pct >= 50 ? "impact-reduce" : pct >= 25 ? "impact-modify" : "impact-mild";
    const isOpen = openReportId === i.id;
    return `<div class="injury-item">
      <div class="sev" style="background:${SEV_COLORS[i.severity]}">${SEV_EMOJI[i.severity] || i.severity}</div>
      <div class="inf ${i.resolved ? "resolved" : ""}">
        <b>${i.label}</b> <span class="impact-chip ${ic}">${pct}% corsa</span>
        <small>${i.note || ""}${i.resolved ? " · ✅ risolto" : ""}</small>
        ${i.resolved ? "" : `<button class="report-toggle" data-id="${i.id}">${isOpen ? "🧑‍⚕️ Chiudi referto" : "🧑‍⚕️ Referto"}</button>
        <div class="injury-report ${isOpen ? "" : "hidden"}" id="report-${i.id}">
          <div class="rep-diag"><b>Diagnosi:</b> ${esc(r.diagnosi || "—")}</div>
          <div class="rep-block"><b>Cause probabili:</b><ul>${(r.cause || []).map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>
          <div class="rep-block"><b>Consigli fisioterapici:</b><ul>${(r.fisioterapia || []).map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>
          <div class="rep-block"><b>Mobilità &amp; esercizi:</b><ul>${(r.mobilita || []).map((c) => `<li>${esc(c)}</li>`).join("")}</ul></div>
          <p class="hint">${esc(r.disclaimer || "")}</p>
        </div>`}
      </div>
      <div class="item-actions">
        ${i.resolved ? "" : `<button class="edit" data-id="${i.id}" title="Modifica">✏️</button>`}
        <button class="del" data-id="${i.id}" title="Rimuovi">🗑️</button>
      </div>
    </div>`;
  }).join("");
  box.querySelectorAll(".report-toggle").forEach((b) => {
    b.addEventListener("click", () => {
      const rep = $("#report-" + b.dataset.id);
      if (rep) {
        rep.classList.toggle("hidden");
        b.textContent = rep.classList.contains("hidden") ? "🧑‍⚕️ Referto" : "🧑‍⚕️ Chiudi referto";
      }
    });
  });
  box.querySelectorAll(".edit").forEach((b) => {
    b.addEventListener("click", () => {
      const inj = injuries.find((x) => x.id === b.dataset.id);
      if (inj) openEditInjuryModal(inj);
    });
  });
  box.querySelectorAll(".del").forEach((b) => {
    b.addEventListener("click", async () => {
      try {
        await api(`/api/injuries/${b.dataset.id}`, { method: "DELETE" });
        injuries = await api("/api/injuries");
        openReportId = null;
        refreshBodyMap();
        toast("🗑️ Fastidio rimosso");
      } catch (err) { toast(`❌ ${err.message}`); }
    });
  });
}

/* --- Flusso Card Anatomica -> modale: parte esatta + severità (🟡🟠🔴) --- */
function renderPartChips(card, selected) {
  const grid = $("#modal-parts");
  grid.innerHTML = card.parts.map((p) =>
    `<button class="part-chip${selected && p.id === selected.id ? " sel" : ""}" data-part="${p.id}">${p.label}</button>`
  ).join("");
  grid.querySelectorAll(".part-chip").forEach((b) => {
    b.addEventListener("click", () => {
      modalPart = { id: b.dataset.part, label: b.textContent.trim() };
      grid.querySelectorAll(".part-chip").forEach((x) => x.classList.remove("sel"));
      b.classList.add("sel");
      $("#modal-part").textContent = "✅ Parte selezionata: " + b.textContent.trim();
    });
  });
}
function setSeveritySel(sev) {
  document.querySelectorAll("#severity-picker button").forEach((b) =>
    b.classList.toggle("sel", Number(b.dataset.s) === sev));
}
function openInjuryModal(card) {
  modalCard = card;
  modalPart = null;
  modalInjury = null;
  $("#modal-title").textContent = "🩹 Segnala fastidio";
  $("#modal-card").textContent = `${card.icon} ${card.title}`;
  $("#modal-part").textContent = "";
  $("#modal-note").value = "";
  $("#modal-delete").classList.add("hidden");
  renderPartChips(card, null);
  setSeveritySel(3);
  $("#modal").classList.remove("hidden");
}
function openEditInjuryModal(injury) {
  const card = cardForPart(injury.part) || BODY_CARDS[0];
  const part = { id: injury.part, label: injury.label };
  modalCard = card;
  modalPart = part;
  modalInjury = injury;
  $("#modal-title").textContent = "✏️ Modifica fastidio";
  $("#modal-card").textContent = `${card.icon} ${card.title}`;
  $("#modal-note").value = injury.note || "";
  $("#modal-delete").classList.remove("hidden");
  renderPartChips(card, part);
  setSeveritySel(injury.severity);
  $("#modal-part").textContent = "✅ Parte selezionata: " + injury.label;
  $("#modal").classList.remove("hidden");
}
function closeModal() { $("#modal").classList.add("hidden"); modalPart = null; modalCard = null; modalInjury = null; }

document.querySelectorAll("#severity-picker button").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#severity-picker button").forEach((x) => x.classList.remove("sel"));
    b.classList.add("sel");
  });
});
$("#modal-cancel").addEventListener("click", closeModal);
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });
$("#modal-save").addEventListener("click", async () => {
  if (!modalPart) { toast("⚠️ Scegli la parte esatta (step 1) prima di salvare"); return; }
  const sev = Number(document.querySelector("#severity-picker button.sel")?.dataset.s || 3);
  const note = $("#modal-note").value.trim();
  try {
    if (modalInjury) {
      await api(`/api/injuries/${modalInjury.id}`, {
        method: "PATCH",
        body: JSON.stringify({ severity: sev, note }),
      });
      openReportId = modalInjury.id;
      toast("✅ Fastidio aggiornato (referto rigenerato)");
    } else {
      const created = await api("/api/injuries", {
        method: "POST",
        body: JSON.stringify({ part: modalPart.id, label: modalPart.label, severity: sev, note }),
      });
      openReportId = created.id;
      toast("✅ Fastidio aggiunto · referto del Coach generato");
    }
    injuries = await api("/api/injuries");
    refreshBodyMap();
    closeModal();
  } catch (err) { toast(`❌ ${err.message}`); }
});
$("#modal-delete").addEventListener("click", async () => {
  if (!modalInjury) return;
  try {
    await api(`/api/injuries/${modalInjury.id}`, { method: "DELETE" });
    injuries = await api("/api/injuries");
    refreshBodyMap();
    closeModal();
    toast("🗑️ Fastidio rimosso");
  } catch (err) { toast(`❌ ${err.message}`); }
});
async function loadInjuries() {
  try {
    injuries = await api("/api/injuries");
    refreshBodyMap();
  } catch (err) { toast(`❌ ${err.message}`); }
}

/* ============================ OBIETTIVO DI GARA ============================ */
let goalRace = "10K";
document.querySelectorAll(".seg-btn[data-race]").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll(".seg-btn[data-race]").forEach((x) => x.classList.remove("sel"));
    b.classList.add("sel");
    goalRace = b.dataset.race;
    fillGoalFromPace();
  });
});
function fillGoalFromPace() {
  // se presente un obiettivo salvato per la stessa distanza, precompila i campi
}
$("#btn-goal-save").addEventListener("click", async () => {
  const h = Number($("#goal-h").value || 0);
  const m = Number($("#goal-m").value || 0);
  const s = Number($("#goal-s").value || 0);
  const tgt = h * 3600 + m * 60 + s;
  if (tgt <= 0) { toast("⚠️ Inserisci un tempo target valido"); return; }
  const raceDate = $("#goal-date").value || null;
  try {
    const d = await api("/api/goals", {
      method: "POST",
      body: JSON.stringify({ race_type: goalRace, target_time_s: tgt, enabled: true, race_date: raceDate }),
    });
    const km = goalRace === "21K" ? 21.0975 : 10;
    $("#goal-result").innerHTML =
      `🎯 Obiettivo: <b>${goalRace}</b> in <b>${fmtClock(tgt)}</b><br>
       Ritmo target: <b style="color:var(--accent)">${fmtPace(tgt / km)}</b><br>
       ${raceDate ? `Gara il <b>${raceDate}</b><br>` : ""}
       <small>Il coach terrà conto di questo obiettivo negli allenamenti.</small>`;
    toast("✅ Obiettivo salvato");
  } catch (err) { toast(`❌ ${err.message}`); }
});
async function loadGoals() {
  try {
    const d = await api("/api/goals");
    if (d.enabled && d.race_type && d.target_time_s) {
      goalRace = d.race_type;
      document.querySelectorAll(".seg-btn[data-race]").forEach((b) =>
        b.classList.toggle("sel", b.dataset.race === goalRace));
      const t = d.target_time_s;
      $("#goal-h").value = Math.floor(t / 3600) || "";
      $("#goal-m").value = Math.floor((t % 3600) / 60);
      $("#goal-s").value = t % 60;
      if (d.race_date) $("#goal-date").value = d.race_date;
      $("#goal-result").innerHTML =
        `🎯 Obiettivo attivo: <b>${d.race_type}</b> in <b>${fmtClock(t)}</b> ·
         ritmo <b>${esc(d.ritmo_target || "—")}</b>${d.race_date ? ` · gara il <b>${d.race_date}</b>` : ""}`;
    }
  } catch { /* nessun obiettivo */ }
}

/* ============================ TABELLA MULTISETTIMANALE ADATTIVA ============================ */
let tableDuration = "1w";
let tableDays = new Set([1, 3, 5, 6]); // default: Mar/Gio/Sab/Dom
let tableData = null;
const HARD_PHASES = ["repeat", "interval", "tempo", "progression"];

// Durata (segmented control)
document.querySelectorAll("#table-duration .seg-btn").forEach((b) => {
  b.addEventListener("click", () => {
    document.querySelectorAll("#table-duration .seg-btn").forEach((x) => x.classList.remove("sel"));
    b.classList.add("sel");
    tableDuration = b.dataset.dur;
  });
});
// Giorni disponibili (multi-select)
document.querySelectorAll("#table-days .day-btn").forEach((b) => {
  if (tableDays.has(Number(b.dataset.day))) b.classList.add("sel");
  b.addEventListener("click", () => {
    const d = Number(b.dataset.day);
    if (tableDays.has(d)) tableDays.delete(d); else tableDays.add(d);
    b.classList.toggle("sel", tableDays.has(d));
  });
});

function isHardWorkout(w) {
  return ((w && w.fasi) || []).some((f) => HARD_PHASES.includes(f.tipo));
}
function fmtShortDay(iso) {
  const d = new Date(iso + "T00:00:00");
  return d.toLocaleDateString("it-IT", { weekday: "short", day: "numeric", month: "short" });
}
function fmtWorkoutSummary(w) {
  const hard = isHardWorkout(w);
  const n = (w.fasi || []).length;
  const dur = fmtDuration(workoutTotalDurationS(w));
  return [`${dur}`, `${n} fasi`, hard ? "🔥 duro" : ""].filter(Boolean).join(" · ");
}
function renderTable() {
  const box = $("#table-plan");
  if (!box) return; // v91: card assente dal DOM (es. post-clear) -> niente crash
  if (!tableData || !tableData.entries || !tableData.entries.length) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  const totalWk = tableData.entries.filter((e) => e.kind === "workout").length;
  const totalRest = tableData.entries.length - totalWk;
  $("#table-meta").textContent =
    `${tableData.duration_label || ""} · dal ${tableData.start_date} al ${tableData.end_date} · ` +
    `${totalWk} allenamenti · ${totalRest} riposi` +
    (tableData.generated_at ? ` · generata il ${tableData.generated_at}` : "");
  const today = new Date().toISOString().slice(0, 10);
  $("#table-entries").innerHTML = tableData.entries.map((e) => {
    const isToday = e.date === today;
    const dateTag = isToday ? `<span class="tag today-tag">OGGI</span>` : "";
    if (e.kind === "rest") {
      return `<div class="tbl-row rest${isToday ? " today" : ""}">
        <div class="tbl-date">${fmtShortDay(e.date)} ${dateTag}</div>
        <div class="tbl-info"><b>😴 Riposo</b></div>
      </div>`;
    }
    const w = e.workout || {};
    const ovrLvl = tableData.readiness && tableData.readiness.readiness_level;
    const badges = [
      e.synced ? "✅ Sync" : "",
      e.adapted ? "🔄 Adattato" : "",
      overrideVisible(e.override, ovrLvl) ? "⚡️ Override" : "",
      isHardWorkout(w) ? "🔥" : "",
    ].filter(Boolean).join(" ");
    return `<div class="tbl-row${isToday ? " today" : ""}">
      <div class="tbl-date">${fmtShortDay(e.date)} ${dateTag}</div>
      <div class="tbl-info">
        <b>${esc(w.nome || "Allenamento")}</b>
        <div class="p-detail">${esc(fmtWorkoutSummary(w))}${w.obiettivo ? " · " + esc(w.obiettivo) : ""}</div>
        ${badges ? `<div class="tbl-badges">${badges}</div>` : ""}
      </div>
      ${!e.synced ? `<button class="btn tbl-sync" data-date="${e.date}" title="Sync su Garmin">📅</button>` : ""}
    </div>`;
  }).join("");
  document.querySelectorAll("#table-entries .tbl-sync").forEach((b) => {
    b.addEventListener("click", () => syncTableEntry(b.dataset.date, b));
  });
  // v90: 🗑️ ELIMINA SOLO LA TABELLA MICROCICLO (sotto la tabella): rimuove la
  // tabella settimanale (DB + file), PRESERVANDO l'allenamento del giorno e il
  // calendario Garmin (clearTable).
  if (!document.getElementById("btn-wipe-all")) {
    box.insertAdjacentHTML("beforeend",
      `<button id="btn-wipe-all" class="btn btn-danger btn-block" style="margin-top:10px">🗑️ Elimina la Tabella Microciclo</button>`
      + `<p class="hint" style="margin-top:6px">L'allenamento di oggi e il calendario Garmin restano intatti.</p>`);
    const wb = $("#btn-wipe-all");
    if (wb) wb.addEventListener("click", clearTable);
  }
}
async function loadTable() {
  try {
    tableData = await api("/api/table");
    renderTable();
  } catch { tableData = null; }
}
async function generateTable() {
  const btn = $("#btn-table-generate");
  const st = $("#table-status");
  if (!tableDays.size) { st.textContent = "⚠️ Seleziona almeno un giorno di allenamento"; return; }
  btn.disabled = true;
  st.textContent = "⏳ DeepSeek sta costruendo il microciclo (30-90s)...";
  try {
    const d = await api("/api/table/generate", {
      method: "POST",
      body: JSON.stringify({ duration: tableDuration, days: [...tableDays] }),
    });
    tableData = d.table;
    tableData.today = d.today;
    renderTable();
    st.textContent = "✅ " + (d.message || "Tabella generata");
    toast("✅ Microciclo generato");
    // Ricarica SIMULTANEA: sezione Allenamento + card Readiness (Home) all'unisono
    await Promise.all([ loadWorkoutSection(), renderHomeWorkoutCard() ]);
  } catch (err) {
    st.textContent = "❌ " + err.message;
    toast("❌ " + err.message);
  } finally {
    btn.disabled = false;
  }
}
async function syncTableEntry(dateStr, btn) {
  if (btn) { btn.disabled = true; btn.textContent = "⏳"; }
  try {
    const r = await api("/api/table/sync", { method: "POST", body: JSON.stringify({ date: dateStr }) });
    if (dateStr === syncDayKey()) markSyncDone();
    toast(r.message || "✅ Sincronizzato");
    await loadTable();
  } catch (err) {
    toast(`❌ ${err.message}`);
    if (btn) { btn.disabled = false; btn.textContent = "📅"; }
  }
}
async function syncAllTable() {
  const btn = $("#btn-table-sync-all");
  btn.disabled = true;
  btn.textContent = "⏳ Sync in corso...";
  try {
    const r = await api("/api/table/sync", { method: "POST", body: JSON.stringify({}) });
    if ((r.results || []).some((x) => x.date === syncDayKey() && x.status === "ok")) markSyncDone();
    toast(r.message || "✅ Sincronizzati");
    await loadTable();
  } catch (err) {
    toast(`❌ ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "📅 Sync tutto";
  }
}
$("#btn-table-generate").addEventListener("click", generateTable);
$("#btn-table-sync-all").addEventListener("click", syncAllTable);

// Controllo adattivo del singolo giorno: dopo "Aggiorna Dati" confronta la
// Readiness attuale con l'allenamento schedulato oggi. Se RED/AMBER + lavoro
// duro -> avviso con [🔄 Adatta] e [⚡️ Mantieni Originale (Override)].
function showAdaptModal(today) {
  const hardName = today.workout_name || "un lavoro duro";
  $("#adapt-msg").innerHTML =
    `🔴 Readiness <b>${today.readiness_level}</b> (${today.readiness_score != null ? Math.round(today.readiness_score) : "—"}/100) ` +
    `ma oggi in tabella c'è <b>${esc(hardName)}</b> (lavoro duro).<br><br>` +
    `Il coach consiglia di ridurre il carico per evitare il sovrallenamento. ` +
    `Puoi adattare l'allenamento di oggi alla tua condizione oppure mantenere ` +
    `l'originale per tua scelta (override).`;
  $("#adapt-modal").classList.remove("hidden");
}
async function adaptTableDay(mode) {
  $("#adapt-modal").classList.add("hidden");
  try {
    const r = await api("/api/table/adapt", { method: "POST", body: JSON.stringify({ mode }) });
    toast(typeof r.message === "string" ? r.message : "✅ Fatto");
    await loadTable();
  } catch (err) {
    toast(`❌ ${err.message}`);
  }
}
$("#adapt-btn").addEventListener("click", () => adaptTableDay("adapt"));
$("#override-btn").addEventListener("click", () => adaptTableDay("override"));
$("#adapt-modal").addEventListener("click", (e) => {
  if (e.target.id === "adapt-modal") $("#adapt-modal").classList.add("hidden");
});
async function checkTableDiscrepancy() {
  try {
    const t = await api("/api/table");
    tableData = t;
    renderTable();
    if (t.today && t.today.needs_adaptation) showAdaptModal(t.today);
  } catch { /* nessuna tabella generata */ }
}

/* ============================ CHAT AI ============================ */
const chatLog = $("#chat-log");
let pendingOverride = null;

function addBubble(text, who = "coach", extraClass = "") {
  const b = document.createElement("div");
  b.className = `bubble ${who} ${extraClass}`.trim();
  b.textContent = text;
  chatLog.appendChild(b);
  chatLog.scrollTop = chatLog.scrollHeight;
  return b;
}
async function sendChat(text, override) {
  if (!text.trim()) return;
  addBubble(text, "user");
  $("#chat-input").value = "";
  const typing = addBubble("⏳ Il coach sta pensando...", "coach");
  try {
    const d = await api("/api/chat", {
      method: "POST",
      body: JSON.stringify({ message: text, override: override ?? null, history: [] }),
    });
    typing.remove();
    if (d.kind === "override_required") {
      addBubble(d.reply, "coach", "warn");
      pendingOverride = text;
      $("#chat-actions").classList.remove("hidden");
    } else if (d.kind === "workout") {
      const b = addBubble(d.reply, "coach", "workout");
      b.innerHTML = esc(d.reply).replace(/\n/g, "<br>");
      addBubble("📅 Puoi caricarlo su Garmin dalla sezione Allenamento.", "coach");
    } else {
      addBubble(d.reply, "coach");
    }
  } catch (err) {
    typing.remove();
    addBubble(`❌ ${err.message}`, "coach", "warn");
  }
}
$("#chat-send").addEventListener("click", () => sendChat($("#chat-input").value));
$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") sendChat($("#chat-input").value);
});
$("#chat-override-yes").addEventListener("click", () => {
  $("#chat-actions").classList.add("hidden");
  if (pendingOverride) sendChat(pendingOverride, true);
  pendingOverride = null;
});
$("#chat-override-no").addEventListener("click", () => {
  $("#chat-actions").classList.add("hidden");
  addBubble("🧘 Genero un recupero attivo in Z1/Z2.", "coach");
  sendChat("proponi un recupero attivo facile di 30 minuti in zona 2", false);
  pendingOverride = null;
});

/* ============================ INIT ============================ */
async function init() {
  await Promise.allSettled([loadReadiness(), loadInjuries(), loadGoals(), loadWorkoutSection(), loadTable()]);
}
// Avvio: se c'è una sessione salvata la verifica subito, altrimenti mostra
// l'Onboarding "Connetti il tuo Garmin". Dopo l'auth, il consenso GDPR
// viene verificato prima dell'idratazione completa.
(async function boot() {
  // ⚠️ LOCK TIPOGRAFIA ONBOARDING: NON scrivere MAI stili inline su
  // .modal-title / .login-subtitle (titolo e sottotitolo onboarding).
  // Le regole CSS usano !important => immuni a qualunque stile inline/JS.
  // Qui sotto: SOLO src/alt del logo e innerHTML della firma (nessuno stile).
  const brandLogo = $("#brand-logo");
  if (brandLogo) {
    brandLogo.src = "/img/logo.svg?v=92";
    brandLogo.alt = "MyPTAI — Performance Coach";
    brandLogo.setAttribute("aria-label", "MyPTAI — Performance Coach");
  }
  // Firma del founder nel footer (link Instagram ufficiale, riapplicata a ogni boot)
  const creator = $("#creator-sign");
  if (creator) {
    creator.innerHTML = "Creato da <a href=\"https://instagram.com/riccardocentofanti_\" target=\"_blank\" rel=\"noopener\">Riccardo Centofanti (@riccardocentofanti_)</a>";
  }
  buildBodyCards();
  if (!SESSION || !SESSION.token) { showLogin(); return; }
  try {
    const h = await api("/api/health");
    if (!h || h.status !== "ok") throw new Error("health");
    hideLogin();
    checkConsentAndStart();
  } catch {
    localStorage.removeItem(SESSION_STORE);
    SESSION = null;
    showLogin("🔒 Sessione non valida: accedi di nuovo con le credenziali Garmin");
  }
})();

/* ============================ SERVICE WORKER ============================ */
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch((err) => {
      console.warn("SW registration failed:", err);
    });
  });
  // v91: il nuovo Service Worker (activate) notifica la pagina aperta che è
  // disponibile una versione più recente -> evita "bottone fantasma" da cache.
  navigator.serviceWorker.addEventListener("message", (ev) => {
    if (ev.data && ev.data.type === "MYPTAI_NEW_VERSION") {
      toast("🔄 Nuova versione disponibile: ricarica la pagina per aggiornare");
    }
  });
}
