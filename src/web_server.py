#!/usr/bin/env python3
"""
web_server.py - FASE 6: Server API FastAPI + PWA MyPT AI
=================================================================
Espone gli endpoint REST consumati dalla PWA mobile-first (web/):

  API:
    GET  /api/health                     -> health check + readiness rapida
    GET  /api/readiness                  -> Readiness + biometria + progressi (dati locali)
    POST /api/refresh                    -> sync live Garmin (dump + summary rigenerati)
    GET  /api/workouts/latest            -> ultimo piano generato (workout_plan.json)
    POST /api/workouts/generate          -> genera piano con DeepSeek (body: request, override)
    POST /api/workouts/sync              -> ONE-TAP: upload + schedule su calendario Garmin
    GET/POST   /api/injuries             -> lista / aggiunta fastidio muscolare
    PATCH/DELETE /api/injuries/{id}      -> risolvi / rimuovi fastidio
    GET/POST   /api/goals                -> obiettivo di gara (10K/21K + tempo target)
    POST /api/chat                       -> chat conversazionale con l'AI Coach

  PWA:
    / (mount statico di web/)            -> index.html + css/js + manifest + sw

REGOLA SOGLIA: FC Soglia (176 bpm) e Passo Soglia (4:44/km) sono valori UFFICIALI
Garmin SEMPRE fissi. Le stime locali (es. lap Z4) non li sostituiscono mai:
vengono esposte solo come confronto informativo e il prompt AI impone i valori
ufficiali come unico riferimento di soglia.

Avvio:
  uvicorn src.web_server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import base64
import copy
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
import database as db  # SQLite multi-utente (data/myptai.db) — richiede src/ nel path
WEB_DIR = PROJECT_ROOT / "web"
DATA_DIR = PROJECT_ROOT / "data"
GOALS_PATH = DATA_DIR / "goals.json"
TABLE_PATH = DATA_DIR / "table_plan.json"
PLAN_PATH = PROJECT_ROOT / "workout_plan.json"
SUMMARY_PATH = PROJECT_ROOT / "analytics_summary.json"
CONFIG_PATH = PROJECT_ROOT / "config.json"
SESSIONS_PATH = DATA_DIR / "sessions.json"
USERS_DIR = DATA_DIR / "users"
SECRET_KEY_ENV = "MYPTAI_SECRET_KEY"   # chiave AES-256 (env, come da requisito)
SECRET_FILE = DATA_DIR / "secret.key"   # fallback persistente auto-generato

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("garmin_ai_coach_web")

# Docs Swagger abilitati SOLO in dev (GARMIN_COACH_ENV=dev): in produzione
# /docs, /redoc e /openapi.json non vengono esposti (nessuno schema API pubblico).
_ENABLE_DOCS = os.environ.get("GARMIN_COACH_ENV", "production") == "dev"
app = FastAPI(
    title="MyPT AI — Coach personale · dati reali Garmin",
    description="API + PWA per Readiness, Allenamenti, Sync Garmin, Infortuni, Obiettivo gara e Chat AI Coach.",
    version="1.0.0",
    docs_url="/docs" if _ENABLE_DOCS else None,
    redoc_url="/redoc" if _ENABLE_DOCS else None,
    openapi_url="/openapi.json" if _ENABLE_DOCS else None,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =============================================================================
# Sicurezza & Sessioni utente (multi-dispositivo)
# -----------------------------------------------------------------------------
# Autenticazione a sessione utente (multi-dispositivo):
#   La PWA fa login con Email+Password Garmin via POST /api/auth -> riceve un
#   token salvato sul dispositivo (localStorage). Ogni richiesta /api/* lo
#   invia come "Authorization: Bearer <token>"; il server risolve user_email
#   e lega i dati (infortuni, obiettivi, tabella, piani) a quell'utente ->
#   stessa esperienza su PC e cellulare con le stesse credenziali Garmin.
#   Nessuna chiave API statica legacy: il token di sessione è l'unico metodo.
# CORS è OUTERMOST (gestisce OPTIONS preflight), l'auth è INNER.
# =============================================================================


# ---- Criptografia AES-256-GCM (token e dati di sessione a riposo) ----------
class _AESGCMCipher:
    """Wrapper AES-256-GCM (cryptography.hazmat): cifra bytes ->
    base64url(nonce | ciphertext | tag). Chiave a 256 bit dall'ambiente.
    Nota: Fernet è AES-128-CBC+HMAC-SHA256 (non GCM/256 bit); per rispettare
    il requisito AES-256-GCM si usa direttamente AESGCM (NIST SP 800-38D)."""

    _NONCE_LEN = 12  # 96 bit raccomandato

    def __init__(self, key: bytes):
        self._aes = AESGCM(key)

    def encrypt(self, plaintext: bytes) -> bytes:
        nonce = os.urandom(self._NONCE_LEN)
        return base64.urlsafe_b64encode(nonce + self._aes.encrypt(nonce, plaintext, None))

    def decrypt(self, token: bytes) -> bytes:
        data = base64.urlsafe_b64decode(token)
        nonce, ct = data[: self._NONCE_LEN], data[self._NONCE_LEN:]
        return self._aes.decrypt(nonce, ct, None)


_cipher: Optional[_AESGCMCipher] = None


def _load_secret_key() -> bytes:
    """Chiave AES-256 da MYPTAI_SECRET_KEY (env) oppure chiave persistente
    auto-generata in data/secret.key al primo avvio."""
    raw = (os.environ.get(SECRET_KEY_ENV) or "").strip()
    if raw:
        try:
            b = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
            if len(b) == 32:
                return b
        except Exception:  # noqa: BLE001
            pass
        return hashlib.sha256(raw.encode()).digest()  # passphrase -> 32 byte
    SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not SECRET_FILE.exists():
        SECRET_FILE.write_bytes(base64.urlsafe_b64encode(os.urandom(32)))
        try:
            os.chmod(SECRET_FILE, 0o600)
        except OSError:
            pass
    return base64.urlsafe_b64decode(SECRET_FILE.read_bytes().strip())


def _get_cipher() -> _AESGCMCipher:
    global _cipher
    if _cipher is None:
        with _lock:
            if _cipher is None:
                _cipher = _AESGCMCipher(_load_secret_key())
    return _cipher


# ---- Rate limiting (anti brute-force / DoS) ---------------------------------
class _RateLimiter:
    """Fixed-window limiter in-memory (processo single-worker di uvicorn)."""

    def __init__(self, limit: int, window_s: float):
        self.limit = limit
        self.window_s = window_s
        self._hits: dict = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> tuple:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window_s]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                retry = max(1, int(self.window_s - (now - hits[0])) + 1)
                return False, retry
            hits.append(now)
            self._hits[key] = hits
            return True, 0

    def check(self, key: str) -> tuple:
        """Verifica il limite SENZA registrare il tentativo (per i limiter
        che contano solo i fallimenti, es. /api/auth)."""
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window_s]
            self._hits[key] = hits
            if len(hits) >= self.limit:
                retry = max(1, int(self.window_s - (now - hits[0])) + 1)
                return False, retry
            return True, 0

    def record(self, key: str) -> None:
        """Registra un tentativo fallito (finestra scorrevole identica ad allow/check)."""
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window_s]
            hits.append(now)
            self._hits[key] = hits


_auth_limiter = _RateLimiter(limit=5, window_s=900)    # max 5 tentativi/15 min per IP
_api_limiter = _RateLimiter(limit=60, window_s=60)     # max 60 req/min sulle API per IP


def _client_ip(request: Request) -> str:
    """IP del client, anti-spoofing: X-Real-IP è impostato da nginx da
    $remote_addr (affidabile: sovrascrive qualunque valore inviato dal client).
    Fallback: ultimo elemento di X-Forwarded-For (nginx vi appende in coda
    l'IP reale con $proxy_add_x_forwarded_for)."""
    real = (request.headers.get("x-real-ip") or "").strip()
    if real:
        return real
    fwd = (request.headers.get("x-forwarded-for") or "").strip()
    if fwd:
        return fwd.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _too_many(retry: int, msg: str) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={"detail": msg, "retry_after_s": retry},
        headers={"Retry-After": str(retry)},
    )


def _sanitize(text) -> str:
    """Sanificazione input: strip + html.escape (anti-XSS stored)."""
    return html.escape(str(text or "").strip())


# ---- Sessioni dispositivo (token casuale, cifrato AES-256-GCM a riposo) -----
def _load_sessions() -> dict:
    sessions = _read_json(SESSIONS_PATH, {})
    # purge sessioni legacy in chiaro (formato pre-cifratura)
    clean = {k: v for k, v in sessions.items() if isinstance(v, str)}
    if len(clean) != len(sessions):
        _save_sessions(clean)
    return clean


def _save_sessions(sessions: dict) -> None:
    _write_json(SESSIONS_PATH, sessions)


def _new_session(user_id: int, email: str) -> str:
    """Crea una sessione permanente per il dispositivo. Il token è casuale
    (256 bit) e il record di sessione viene cifrato AES-256-GCM a riposo:
    nel file resta solo l'impronta SHA-256 del token + blob cifrato.
    La sessione lega user_id (SQL) + email Garmin dell'utente."""
    token = secrets.token_urlsafe(32)
    now = datetime.now().isoformat(timespec="seconds")
    payload = {"token": token, "user_id": user_id, "user_email": email,
               "created_at": now, "last_seen": now}
    encrypted = _get_cipher().encrypt(json.dumps(payload).encode()).decode()
    with _lock:
        sessions = _load_sessions()
        sessions[hashlib.sha256(token.encode()).hexdigest()] = encrypted
        _save_sessions(sessions)
    return token


def _resolve_session(request: Request) -> Optional[tuple]:
    """Risolve il token sessione -> (user_id, user_email) o None."""
    auth = request.headers.get("authorization") or ""
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    digest = hashlib.sha256(token.encode()).hexdigest()
    # IMPORTANTE: _get_cipher() PRIMA del lock (è thread-safe con double-checked
    # locking). Chiamarlo DENTRO `with _lock:` con _cipher==None causerebbe un
    # self-deadlock (threading.Lock NON rientrante) che congela l'event loop.
    cipher = _get_cipher()
    with _lock:
        sessions = _load_sessions()
        blob = sessions.get(digest)
        if not blob:
            return None
        try:
            payload = json.loads(cipher.decrypt(blob.encode()))
        except Exception:  # noqa: BLE001 (token manomesso o chiave ruotata)
            return None
        # confronto constant-time del token decifrato con quello presentato
        if not hmac.compare_digest(str(payload.get("token")), token):
            return None
        email = payload.get("user_email")
        user_id = payload.get("user_id")
        if not email or not user_id:
            return None
        now = datetime.now().isoformat(timespec="seconds")
        if payload.get("last_seen") != now:  # riscrittura max 1 volta/secondo
            payload["last_seen"] = now
            sessions[digest] = cipher.encrypt(json.dumps(payload).encode()).decode()
            _save_sessions(sessions)
        return int(user_id), email


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=(), payment=()"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data: https:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self';"
    )
    return response


@app.middleware("http")
async def session_auth(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/") and request.method != "OPTIONS":
        # 0) Rate limiting anti brute-force/DoS: max 60 req/min per IP
        ok, retry = _api_limiter.allow(_client_ip(request))
        if not ok:
            return _too_many(retry, "Troppe richieste: attendi qualche secondo e riprova.")
        # 1) Sessione utente valida -> autorizzato, dati legati a user_id
        session = _resolve_session(request)
        if session:
            request.state.user_id, request.state.user_email = session
            return await call_next(request)
        # 2) Pubbliche: /api/auth (login) e /api/legal/* (documenti legali
        #    consultabili anche prima del consenso e del login)
        if path == "/api/auth" or path.startswith("/api/legal/"):
            request.state.user_email = None
            return await call_next(request)
        # 3) Nessuna chiave legacy: le API richiedono il token di sessione utente.
        return JSONResponse(
            status_code=401,
            content={"detail": "Sessione non valida: accedi di nuovo con le credenziali Garmin."},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


class AuthRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=200, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(..., min_length=1, max_length=200)


class ConsentPayload(BaseModel):
    version: str = Field("1.1", pattern=r"^\d+\.\d+$")
    accepted: bool = True


class _GarminAuthError(Exception):
    """Credenziali Garmin errate o account non autorizzato."""


def _garmin_display_name(client) -> Optional[str]:
    """Nome visualizzato Garmin (best-effort, mai bloccante)."""
    try:
        info = client.get_full_name()
        if isinstance(info, dict):
            return (info.get("fullName") or info.get("full_name") or "").strip() or None
        if isinstance(info, str) and info.strip():
            return info.strip()
    except Exception:  # noqa: BLE001
        pass
    return None


def _tokenstore_expired(tokens_enc: bytes, margin_s: int = 60) -> bool:
    """True se il tokenstore cifrato contiene un oauth2 scaduto (o quasi).
    Solo lettura LOCALE (nessuna chiamata di rete): evita che build_client()
    tenti il refresh di token storici scaduti (attesa Garmin di 3-10s) prima
    del login diretto, garantendo risposte di autenticazione in <2s.
    Supporta entrambi i formati: garth legacy (oauth2_token.json con
    expires_at esplicito) e garminconnect >= 0.3 (garmin_tokens.json, senza
    scadenza esplicita -> non marcato scaduto, la validazione resta live)."""
    try:
        payload = json.loads(_get_cipher().decrypt(bytes(tokens_enc)))
        oauth2_raw = json.loads(payload.get("oauth2_token.json") or "{}")
        exp = oauth2_raw.get("expires_at")
        if exp is not None:
            return float(exp) < time.time() + margin_s
        return False
    except Exception:  # noqa: BLE001 (payload illeggibile -> non riutilizzabile)
        return True


def _garmin_login_register(email: str, password: str) -> int:
    """Login LIVE su Garmin Connect + registrazione/aggiornamento utente nel
    Database SQL + salvataggio del tokenstore cifrato AES-256-GCM (la password
    NON viene mai salvata). Ritorna user_id.
    Qualsiasi account Garmin valido può accedere (multi-utente: Riccardo,
    fratello, amici, clienti)."""
    from garmin_probe import build_client

    email = email.strip().lower()
    password = password.strip()
    tmp_dir = Path(tempfile.mkdtemp(prefix="myptai_ts_"))
    try:
        # 1) Tokenstore già salvato (DB cifrato): ripristino senza re-login SSO
        #    SOLO se il token oauth2 è ancora valido (pre-check locale, zero rete).
        #    Se scaduto -> fallback IMMEDIATO al login diretto, senza attendere
        #    il refresh di token storici (garanzia risposta < 2s).
        user = db.get_user_by_email(email)
        if user and user.get("tokens_enc") and not _tokenstore_expired(user["tokens_enc"]):
            try:
                ts_dir = _write_garmin_token_dir(user["tokens_enc"], tmp_dir, email)
                client = build_client(email, password, str(ts_dir))
                display = _garmin_display_name(client)
                uid = db.upsert_user(email, display)
                _save_garth_tokens(uid, client, tmp_dir)  # aggiorna il tokenstore cifrato
                _save_permanent_tokenstore(client, email)  # sessione anche su disco permanente
                return uid
            except _GarminAuthError:
                raise
            except Exception as err:  # noqa: BLE001 (tokenstore invalido a runtime)
                logger.info(f"auth: tokenstore non riutilizzabile per {email} ({err}), login diretto")
        elif user and user.get("tokens_enc"):
            logger.info(f"auth: tokenstore scaduto per {email} -> login diretto immediato (zero attese)")

        # 2) Login diretto SSO Garmin con le credenziali appena inserite
        client = build_client(email, password, None)
        display = _garmin_display_name(client)
        uid = db.upsert_user(email, display)
        # Salva il tokenstore della sessione Garmin, cifrato AES-256-GCM
        try:
            _save_garth_tokens(uid, client, tmp_dir)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"auth: salvataggio tokenstore cifrato fallito ({err})")
        _save_permanent_tokenstore(client, email)  # sessione anche su disco permanente
        return uid
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _dump_client_tokens(client, dest_dir: Path) -> dict:
    """Serializza i token della sessione Garmin nel formato della libreria
    installata, scrivendo i file dentro dest_dir:
    - garminconnect >= 0.3 (client.client.dump -> garmin_tokens.json);
    - versioni garth legacy (client.garth.dump -> oauth1/oauth2_token.json).
    Ritorna il payload {nome_file: contenuto} da cifrare nel DB."""
    payload: dict = {}
    dest_dir.mkdir(parents=True, exist_ok=True)
    inner = getattr(client, "client", None)
    if inner is not None and hasattr(inner, "dump"):
        try:
            inner.dump(str(dest_dir))
        except Exception as err:  # noqa: BLE001
            logger.warning(f"dump tokenstore 0.3.x non riuscito: {err}")
        p = dest_dir / "garmin_tokens.json"
        payload["garmin_tokens.json"] = p.read_text(encoding="utf-8") if p.exists() else ""
    if getattr(client, "garth", None) is not None:
        try:
            client.garth.dump(str(dest_dir))
        except Exception as err:  # noqa: BLE001
            logger.warning(f"dump tokenstore garth non riuscito: {err}")
        for name in ("oauth1_token.json", "oauth2_token.json"):
            p = dest_dir / name
            payload[name] = p.read_text(encoding="utf-8") if p.exists() else ""
    return payload


def _save_garth_tokens(uid: int, client, tmp_dir: Path) -> None:
    """Serializza il tokenstore garmin (formato della libreria installata) in
    una sottocartella temporanea, lo cifra AES-256-GCM e lo salva nel DB
    (users.tokens_enc). Non lascia mai file in chiaro su disco permanente."""
    try:
        ts_dir = tmp_dir / "session"
        ts_dir.mkdir(parents=True, exist_ok=True)  # garantisce la cartella del tokenstore
        payload = _dump_client_tokens(client, ts_dir)
        if payload.get("garmin_tokens.json") or payload.get("oauth2_token.json"):
            db.set_user_tokens(uid, _get_cipher().encrypt(json.dumps(payload).encode()))
    except Exception as err:  # noqa: BLE001
        logger.warning(f"garth tokenstore non salvabile: {err}")


def _write_garmin_token_dir(tokens_enc: bytes, base_dir: Path, email: Optional[str] = None) -> Path:
    """Decifra il tokenstore dal DB e scrive i file json (garmin_tokens.json e/o
    oauth1/oauth2_token.json) in base_dir/session (directory compatibile con
    client.login / garth.load). Se email e' fornito, ripristina il tokenstore
    ANCHE nel percorso permanente per-utente tokenstore_path(email)."""
    ts_dir = base_dir / "session"
    ts_dir.mkdir(parents=True, exist_ok=True)
    payload = json.loads(_get_cipher().decrypt(bytes(tokens_enc)))
    for name, content in payload.items():
        if name.endswith(".json"):
            (ts_dir / name).write_text(content or "", encoding="utf-8")
    if email:
        try:
            from garmin_probe import tokenstore_path
            ts_perm = tokenstore_path(email)
            ts_perm.parent.mkdir(parents=True, exist_ok=True)
            for name, content in payload.items():
                if name.endswith(".json"):
                    (ts_perm / name).write_text(content or "", encoding="utf-8")
        except Exception as err:  # noqa: BLE001
            logger.warning(f"tokenstore permanente non ripristinabile per {email}: {err}")
    return ts_dir


_INITBIO_RUNNING: dict = {}
_INITBIO_GUARD = threading.Lock()


def _save_permanent_tokenstore(client, email: str) -> None:
    """Salva la sessione garth nel percorso permanente per-utente
    tokenstore_path(email) (~/.garminconnect_session_{slug}) IN AGGIUNTA al
    DB cifrato (doppia persistenza: ripristino rapido anche fuori dal server)."""
    try:
        from garmin_probe import tokenstore_path
        ts_perm = tokenstore_path(email)
        ts_perm.parent.mkdir(parents=True, exist_ok=True)
        _dump_client_tokens(client, ts_perm)
    except Exception as err:  # noqa: BLE001
        logger.warning(f"auth: tokenstore permanente non salvabile per {email}: {err}")


def _initial_biometric_extract(uid: int, email: str, password: str = "") -> None:
    """Estrazione live completa IMMEDIATA dopo il login (multi-utente):
    run_probe(email, password, str(tokenstore_path(email)), per_user_dump_path(email))
    genera data/dumps/garmin_raw_dump_{slug}.json e popola la biometria nel DB
    SQL via db.save_biometric(uid, summary). Best-effort: un errore non blocca
    mai il login (l'utente puo sempre forzare /api/refresh)."""
    with _INITBIO_GUARD:
        if _INITBIO_RUNNING.get(uid):
            logger.info(f"init-bio: estrazione gia in corso per {email}, skip")
            return
        _INITBIO_RUNNING[uid] = True
    try:
        from garmin_probe import run_probe, per_user_dump_path, tokenstore_path
        from analytics_engine import AnalyticsEngine
        dump_path = per_user_dump_path(email)
        run_probe(email, password, str(tokenstore_path(email)), dump_path)
        if not dump_path.exists():
            logger.warning(f"init-bio: dump Garmin non generato per {email}")
            return
        with open(dump_path, "r", encoding="utf-8") as f:
            dump = json.load(f)
        summary = AnalyticsEngine(dump=dump).build_context()
        if summary:
            db.save_biometric(uid, summary)
            logger.info(f"init-bio: summary biometrico salvato per {email} (uid={uid})")
    except Exception as err:  # noqa: BLE001
        logger.warning(f"init-bio: estrazione iniziale fallita per {email}: {err}")
    finally:
        with _INITBIO_GUARD:
            _INITBIO_RUNNING.pop(uid, None)


@app.post("/api/auth")
def auth(payload: AuthRequest, request: Request):
    """Onboarding APERTO A CHIUNQUE (multi-utente): valida le credenziali LIVE
    su Garmin Connect via Garmin(email, password).login(). Se la login riesce:
    crea/aggiorna l'utente nel Database SQL, salva i token Garmin cifrati in
    AES-256-GCM e rilascia il Bearer token di sessione (user_id + email).
    Rate limiting anti brute-force: max 5 tentativi / 15 min (900 s) per IP.
    In caso di blocco: HTTP 429 con retry_after_s per il countdown client."""
    ip = _client_ip(request)
    ok, retry = _auth_limiter.check(ip)  # check senza consumare: contano SOLO i fallimenti
    if not ok:
        return JSONResponse(
            status_code=429,
            content={
                "detail": "rate_limit_exceeded",
                "message": "Troppi tentativi errati per sicurezza.",
                "retry_after_s": retry,
            },
            headers={"Retry-After": str(retry)},
        )
    email = (payload.email or "").strip().lower()
    password = (payload.password or "").strip()
    try:
        user_id = _garmin_login_register(email, password)
    except Exception as err:  # noqa: BLE001
        msg = str(err).lower()
        if any(k in msg for k in ("password", "credential", "login", "unauthorized",
                                  "invalid", "401", "not found", "access denied")):
            _auth_limiter.record(ip)  # SOLO credenziali errate: conta come tentativo
            raise HTTPException(status_code=401,
                                detail="Credenziali Garmin non valide: verifica email e password.")
        # Garmin irraggiungibile/errore infrastrutturale: NON consuma tentativi
        logger.warning(f"auth: errore Garmin per {email}: {err}")
        raise HTTPException(status_code=502,
                            detail="Garmin Connect non raggiungibile: riprova tra poco.")
    token = _new_session(user_id, email)
    # Estrazione live completa IMMEDIATA dopo il login (multi-utente): parte in
    # background con run_probe(email, password, tokenstore_path(email), dump_path)
    # e popola subito la biometria (db.save_biometric) senza rallentare il login.
    threading.Thread(
        target=_initial_biometric_extract,
        args=(user_id, email, password),
        daemon=True,
        name=f"myptai-init-bio-{user_id}",
    ).start()
    return {
        "status": "ok",
        "token": token,
        "user_email": email,
        "message": "Sessione creata: dati sincronizzati su tutti i tuoi dispositivi",
    }


@app.get("/api/user/me")
def user_me(request: Request):
    """Profilo utente corrente: email + stato consenso GDPR (Art. 6-7)."""
    email = getattr(request.state, "user_email", None)
    uid = getattr(request.state, "user_id", None)
    consent = db.get_user_consent(uid) if uid else {"accepted": False, "version": None}
    return {
        "email": email,
        "consent": bool(consent.get("accepted")),
        "consent_version": consent.get("version"),
    }


@app.post("/api/user/consent")
def user_consent(payload: ConsentPayload, request: Request):
    """Registra il consenso al trattamento dei dati (Art. 6-7 GDPR)."""
    email = getattr(request.state, "user_email", None)
    uid = getattr(request.state, "user_id", None)
    db.set_user_consent(uid, payload.version, bool(payload.accepted))
    record = {
        "email": email,
        "accepted": bool(payload.accepted),
        "version": payload.version,
        "accepted_at": datetime.now().isoformat(timespec="seconds"),
        "ip": _client_ip(request),
    }
    return {"status": "ok", "consent": record}


@app.post("/api/user/delete-account")
def user_delete_account(request: Request):
    """Diritto all'oblio (Art. 17 GDPR): elimina dal Database SQL TUTTI i dati
    dell'utente (cascata: biometriche, allenamenti, infortuni, obiettivo) e
    revoca le sue sessioni su tutti i dispositivi."""
    email = getattr(request.state, "user_email", None)
    uid = getattr(request.state, "user_id", None)
    rows = db.delete_user(uid) if uid else 0
    cipher = _get_cipher()  # fuori dal lock (evita self-deadlock su _lock)
    with _lock:  # revoca sessioni dell'utente (token non più risolvibili)
        sessions = _load_sessions()
        kept = {}
        for digest, blob in sessions.items():
            try:
                payload = json.loads(cipher.decrypt(blob.encode()))
            except Exception:  # noqa: BLE001
                continue
            if int(payload.get("user_id") or -1) != uid:
                kept[digest] = blob
        _save_sessions(kept)
    logger.warning(f"delete-account (Art. 17 GDPR): {email} (user_id={uid}) — {rows} righe SQL rimosse")
    return {
        "status": "ok",
        "deleted": [f"SQL cascade: {rows} righe"],
        "message": "Account e dati personali eliminati dal server (Art. 17 GDPR).",
    }


# =============================================================================
# Suite legale: Termini di Servizio, Privacy Policy (GDPR), Cookie Policy
# Esposta via /api/legal/* (pubblica, consultabile prima del consenso/login)
# =============================================================================
LEGAL_TEXTS = {
    "privacy": {
        "title": "Informativa Privacy (GDPR)",
        "updated": "2026-08-16",
        "sections": [
            {
                "h": "1. Titolare e finalità del trattamento",
                "paras": [
                    "MyPT AI è un coach personale di corsa che genera allenamenti basandosi sui dati reali estratti dal tuo account Garmin Connect.",
                    "Il trattamento ha finalità esclusive di coaching sportivo: calcolo della Readiness, prescrizione degli allenamenti, sincronizzazione sul calendario Garmin e monitoraggio infortuni.",
                ],
            },
            {
                "h": "2. Categorie di dati trattati (dati biometrici, Art. 9 GDPR)",
                "paras": [
                    "Il servizio elabora dati biometrici e relativi alla salute (categoria speciale ai sensi dell'Art. 9 GDPR): frequenza cardiaca, HRV notturno e settimanale, Sleep Score, Body Battery, VO2max, passi e ritmi delle sessioni di corsa.",
                    "I dati vengono estratti in modo automatizzato dal tuo account Garmin e analizzati in forma pseudonimizzata: l'AI generativa riceve esclusivamente una sintesi numerica compatta (score e indicatori), mai i dati grezzi.",
                    "I dati non vengono in alcun caso ceduti, venduti o condivisi con terzi, né utilizzati per finalità di profilazione pubblicitaria.",
                ],
            },
            {
                "h": "3. Base giuridica (Art. 6 GDPR)",
                "paras": [
                    "Il trattamento si fonda sul tuo consenso esplicito (Art. 6.1.a e 9.2.a GDPR), prestato con la casella dedicata all'onboarding e revocabile in qualsiasi momento, e sull'esecuzione di un servizio richiesto dall'interessato (Art. 6.1.b).",
                ],
            },
            {
                "h": "4. Sicurezza dei dati",
                "paras": [
                    "I dati sono protetti con crittografia AES-256-GCM a riposo per token di sessione e dati sensibili, trasporto TLS/HTTPS (certificato valido), rate-limiting anti brute-force e accesso in sola lettura al tuo account Garmin tramite sessione autorizzata.",
                    "Le credenziali Garmin non vengono mai salvate: l'accesso avviene via sessione token temporanea non reversibile a testo chiaro.",
                ],
            },
            {
                "h": "5. Conservazione e localizzazione",
                "paras": [
                    "I dati vengono conservati su un server dedicato (VPS) per il solo tempo necessario al servizio di coaching attivo, e per l'utente singolo. Alla cancellazione dell'account tutti i file personali vengono rimossi.",
                ],
            },
            {
                "h": "6. I tuoi diritti (Art. 15-22 GDPR)",
                "paras": [
                    "Hai diritto di accesso, rettifica, cancellazione (diritto all'oblio, Art. 17), portabilità, limitazione e opposizione al trattamento, nonché di revocare il consenso in qualsiasi momento.",
                    "Puoi esercitare i diritti dall'app (pulsante 'Elimina account e dati' nel footer) o contattando il titolare. Hai inoltre diritto di proporre reclamo al Garante per la Protezione dei Dati Personali.",
                ],
            },
            {
                "h": "7. Cancellazione account (Art. 17 GDPR)",
                "paras": [
                    "La funzione 'Elimina account e dati' rimuove definitivamente dal server infortuni, obiettivo di gara, tabella allenamenti, piani generati e consenso, e revoca tutte le sessioni attive su tutti i tuoi dispositivi. L'operazione è irreversibile.",
                ],
            },
        ],
    },
    "terms": {
        "title": "Termini di Servizio e Disclaimer Medico",
        "updated": "2026-08-16",
        "sections": [
            {
                "h": "1. Oggetto del servizio",
                "paras": [
                    "MyPT AI fornisce un servizio di coaching sportivo assistito da intelligenza artificiale: analisi dei dati Garmin Connect, calcolo della Readiness, generazione di allenamenti strutturati e sincronizzazione sul calendario Garmin.",
                ],
            },
            {
                "h": "2. Uso del servizio",
                "paras": [
                    "Il servizio è destinato a uso personale dell'atleta autorizzato. L'utente si impegna a fornire dati veritieri e a non utilizzare il servizio per finalità diverse dal coaching sportivo.",
                ],
            },
            {
                "h": "3. ⚠️ Disclaimer Medico (leggere con attenzione)",
                "paras": [
                    "MyPT AI NON è un dispositivo medico e NON fornisce diagnosi, terapie o consulenza medica o cardiologica. I contenuti generati (allenamenti, zone cardiache, ritmi, valutazioni della Readiness) hanno esclusiva finalità informativa e sportiva.",
                    "PRIMA di iniziare o modificare un programma di allenamento è obbligatorio consultare un medico, verificare la propria idoneità sportiva (certificato medico sportivo) e sottoporsi a controllo cardiologico se indicato.",
                    "Se durante l'allenamento avverti dolore toracico, palpitazioni, capogiri, dispnea eccessiva o qualsiasi altro sintomo, INTERROMPI immediatamente l'attività e consulta un medico. Non riprendere l'allenamento senza parere medico.",
                    "Le soglie cardiache e i ritmi indicati sono stime basate sui dati Garmin e non sostituiscono una valutazione medica: l'utente è l'unico responsabile delle proprie scelte di allenamento e della propria salute.",
                ],
            },
            {
                "h": "4. Limitazione di responsabilità",
                "paras": [
                    "Il servizio è fornito 'così com'è' (as-is), senza garanzie di continuità, accuratezza o risultati. MyPT AI non è responsabile di infortuni, danni alla salute o mancati risultati derivanti dall'uso degli allenamenti proposti.",
                    "L'utilizzo del servizio implica l'accettazione integrale del presente disclaimer e l'assunzione di piena responsabilità per la propria condizione fisica.",
                ],
            },
            {
                "h": "5. Modifiche ai Termini",
                "paras": [
                    "I presenti Termini possono essere aggiornati; la versione corrente è sempre consultabile dal footer dell'app. Il proseguimento dell'utilizzo dopo un aggiornamento costituisce accettazione dei nuovi Termini.",
                ],
            },
        ],
    },
    "cookies": {
        "title": "Cookie Policy",
        "updated": "2026-08-16",
        "sections": [
            {
                "h": "1. Cosa sono i cookie",
                "paras": [
                    "I cookie sono piccoli file testuali memorizzati dal browser durante la navigazione. La normativa (GDPR e Direttiva ePrivacy) richiede trasparenza sull'uso dei cookie e di tecnologie analoghe.",
                ],
            },
            {
                "h": "2. Cookie utilizzati da MyPT AI",
                "paras": [
                    "MyPT AI NON utilizza cookie di profilazione, cookie pubblicitari o cookie di terze parti (nessun tracciamento cross-site, nessun pixel, nessun analytics di marketing).",
                    "La sessione utente è gestita tramite un token tecnico conservato esclusivamente nel localStorage del browser (tecnologia analoga ai cookie tecnici ex Art. 122 Codice Privacy): serve solo a mantenere l'accesso e non viene condiviso con terzi.",
                ],
            },
            {
                "h": "3. Dati di navigazione e sicurezza",
                "paras": [
                    "Il server registra nei log esclusivamente dati tecnici (indirizzo IP, timestamp, endpoint richiesto) per finalità di sicurezza e anti-abuso (rate limiting). Tali log non vengono utilizzati per profilazione e non vengono ceduti a terzi.",
                ],
            },
            {
                "h": "4. Come gestire il token tecnico",
                "paras": [
                    "Non è necessario alcun intervento: il token tecnico è indispensabile per il funzionamento dell'app. Puoi eliminarlo in qualsiasi momento dal footer con 'Elimina account e dati' oppure cancellando i dati del sito dal browser (logout automatico).",
                ],
            },
        ],
    },
}


@app.get("/api/legal/privacy")
def legal_privacy():
    """Informativa Privacy GDPR (pubblica, consultabile pre-consenso)."""
    return LEGAL_TEXTS["privacy"]


@app.get("/api/legal/terms")
def legal_terms():
    """Termini di Servizio con Disclaimer Medico (pubblici)."""
    return LEGAL_TEXTS["terms"]


@app.get("/api/legal/cookies")
def legal_cookies():
    """Cookie Policy (pubblica)."""
    return LEGAL_TEXTS["cookies"]


# =============================================================================
# Persistenza locale (data/goals.json) - single user
# =============================================================================
_lock = threading.Lock()

# Lock di sync PER-UTENTE: serializza gli upload Garmin di uno stesso utente
# (idempotenza anti-duplicati: nessuna doppia scrittura sul calendario).
_SYNC_LOCKS: dict = {}
_SYNC_LOCKS_GUARD = threading.Lock()


def _sync_lock_for(uid: int) -> threading.Lock:
    """Ritorna il lock dedicato all'utente (creandolo al primo uso)."""
    with _SYNC_LOCKS_GUARD:
        return _SYNC_LOCKS.setdefault(uid, threading.Lock())

DEFAULT_GOALS = {
    "race_type": None,          # "10K" | "21K" | None
    "target_time_s": None,      # secondi (es. 5400 = 1h30m)
    "race_date": None,          # "YYYY-MM-DD" (per la tabella "Fino alla Gara")
    "enabled": False,
    "updated_at": None,
}
DEFAULT_TABLE = {
    "generated_at": None,
    "duration": None,           # "1w" | "2w" | "1m" | "race"
    "duration_label": None,
    "days": [],                 # [0..6], 0=Lunedì ... 6=Domenica
    "start_date": None,
    "end_date": None,
    "entries": [],              # [{date, weekday, kind: workout|rest, workout, synced, adapted, override}]
}

# Nomi giorni (date.weekday(): Lunedì=0 ... Domenica=6)
WEEKDAY_IT = ["Lunedì", "Martedì", "Mercoledì", "Giovedì", "Venerdì", "Sabato", "Domenica"]
# Durata in giorni dei preset (escluso "race" gestito dalla data gara)
TABLE_DURATION_DAYS = {"1w": 7, "2w": 14, "1m": 28}
TABLE_DURATION_LABEL = {"1w": "1 Settimana", "2w": "2 Settimane", "1m": "1 Mese", "race": "Fino alla Gara"}
# Tipi di fase considerati "lavoro duro" (per il controllo adattivo giornaliero)
TABLE_HARD_PHASES = {"repeat", "interval", "tempo", "progression"}


def _ensure_data_files():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    USERS_DIR.mkdir(parents=True, exist_ok=True)
    if not SESSIONS_PATH.exists():
        _write_json(SESSIONS_PATH, {})
    if not GOALS_PATH.exists():
        _write_json(GOALS_PATH, DEFAULT_GOALS)


def _read_json(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return copy.deepcopy(default)


def _write_json(path: Path, data) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    tmp.replace(path)


# =============================================================================
# Persistenza locale PER-UTENTE (data/users/{email}/...) con mirror legacy
# =============================================================================
def _slug_email(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (email or "").lower()).strip("_") or "default"


def _default_email() -> str:
    """Email Garmin configurata sul server (unico account di coaching)."""
    try:
        return ((_load_config().get("garmin") or {}).get("email") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _user_dir(email: Optional[str] = None) -> Path:
    return USERS_DIR / _slug_email(email or _default_email())


def _user_file(email: Optional[str], name: str) -> Path:
    return _user_dir(email) / name


def _load_user_json(email: Optional[str], name: str, default):
    """Legge il file per-utente; se assente lo inizializza dal file legacy
    (root/data) così i dati esistenti non vanno persi alla migrazione."""
    email = email or _default_email()
    user_path = _user_file(email, name)
    if not user_path.exists():
        legacy = DATA_DIR / name
        if legacy.exists():
            try:
                shutil.copy2(legacy, user_path)
            except OSError as err:  # noqa: BLE001
                logger.warning(f"copy legacy {name} -> {user_path}: {err}")
    return _read_json(user_path, default)


def _write_user_json(email: Optional[str], name: str, data) -> None:
    """Scrive il file per-utente + mirror legacy (compatibilità bot/CLI)."""
    email = email or _default_email()
    _write_json(_user_file(email, name), data)
    _write_json(DATA_DIR / name, data)


def _user_id_for(email: Optional[str] = None) -> int:
    """user_id SQL per l'email dell'utente (crea l'utente se non esiste,
    es. primo accesso). I dati sono SEMPRE isolati per user_id."""
    email = ((email or _default_email()) or "").strip().lower()
    user = db.get_user_by_email(email)
    if user:
        return int(user["id"])
    return db.upsert_user(email)


def _load_injuries(email: Optional[str] = None) -> list:
    return db.list_injuries(_user_id_for(email))


def _save_injuries(email: Optional[str], injuries: list) -> None:
    """Sincronizza la lista infortuni dell'utente sul SQL (delete-all + insert)."""
    db.replace_injuries(_user_id_for(email), injuries)


def _load_goals(email: Optional[str] = None) -> dict:
    uid = _user_id_for(email)
    row = db.get_goals(uid)
    if not row:
        return copy.deepcopy(DEFAULT_GOALS)
    return {
        **DEFAULT_GOALS,
        "race_type": row.get("race_type"),
        "target_time_s": row.get("target_time_s"),
        "race_date": row.get("race_date"),
        "enabled": bool(row.get("enabled")),
        "updated_at": row.get("updated_at"),
    }


def _save_goals(email: Optional[str], goals: dict) -> None:
    db.set_goals(_user_id_for(email), goals)


def _load_table(email: Optional[str] = None) -> dict:
    table = db.load_workout_doc(_user_id_for(email), "table") or copy.deepcopy(DEFAULT_TABLE)
    # Arricchisci ogni workout con durata_reale_s (fasi + recuperi, identica a Garmin)
    for e in table.get("entries") or []:
        if e.get("workout"):
            _decorate_workout(e["workout"])
    return table


def _save_table(email: Optional[str], table: dict) -> None:
    db.save_workout_doc(_user_id_for(email), "table", table)


def _load_plan(email: Optional[str] = None) -> dict:
    """workout_plan.json per-utente (SQL: kind='plan')."""
    return db.load_workout_doc(_user_id_for(email), "plan") or {}


def _write_plan(email: Optional[str], plan: dict) -> None:
    """Salva il piano per-utente (SQL) + mirror legacy solo per l'account
    principale (bot Telegram / CLI mantengono il comportamento storico)."""
    email = email or _default_email()
    db.save_workout_doc(_user_id_for(email), "plan", plan)
    if (email or "").lower() == (_default_email() or "").lower():
        _write_json(PLAN_PATH, plan)


def _migrate_legacy_to_sql() -> None:
    """Migrazione one-shot: copia i dati legacy (JSON per-utente del principale)
    nelle tabelle SQL alla prima esecuzione multi-utente. Idempotente."""
    email = _default_email()
    if not email:
        return
    uid = _user_id_for(email)
    # obiettivo di gara
    if not db.get_goals(uid):
        legacy = _read_json(_user_file(email, "goals.json"), None) or _read_json(GOALS_PATH, None)
        if legacy and legacy.get("race_type"):
            db.set_goals(uid, legacy)
    # piano allenamento
    if not db.load_workout_doc(uid, "plan"):
        p = _read_json(_user_file(email, "workout_plan.json"), None) or _read_json(PLAN_PATH, None)
        if p and p.get("workout"):
            db.save_workout_doc(uid, "plan", p)
    # tabella multisettimanale
    if not db.load_workout_doc(uid, "table"):
        t = _read_json(_user_file(email, "table_plan.json"), None) or _read_json(TABLE_PATH, None)
        if t and t.get("entries"):
            db.save_workout_doc(uid, "table", t)
    # summary biometrico di oggi
    s = _read_json(_user_file(email, "analytics_summary.json"), None) or _read_json(SUMMARY_PATH, None)
    if s and not db.get_biometric(uid):
        db.save_biometric(uid, s)
    logger.info(f"migrazione legacy -> SQL completata per {email} (user_id={uid})")


def _decorate_workout(w: dict, readiness: dict = None) -> dict:
    """Arricchisce il workout per la risposta API (mai persistito):
    - durata_reale_s: durata totale identica a Garmin (fasi + recuperi);
    - nota ripulita: se la Readiness attuale NON è RED, rimuove dalla nota gli
      avvisi di override vecchi ("OVERRIDE ATLETA ATTIVO" / "Readiness RED")
      così la riga gialla in fondo alla scheda non compare più.
    Idempotente e sicura: opera solo sulla copia in memoria."""
    if not isinstance(w, dict):
        return w
    if isinstance(w.get("fasi"), list) and w.get("fasi"):
        if not isinstance(w.get("durata_reale_s"), (int, float)):
            from analytics_engine import workout_duration_with_recovery_s
            w["durata_reale_s"] = round(workout_duration_with_recovery_s(w), 1)
    if readiness is None:
        try:
            readiness = _load_summary().get("readiness", {})
        except Exception as err:  # noqa: BLE001
            logger.warning(f"decorate: readiness non disponibile ({err})")
            readiness = {}
    if readiness.get("readiness_level") != "RED":
        w["note"] = _strip_override_lines(w.get("note"))
    return w


_OVERRIDE_NOTE_RE = re.compile(r"OVERRIDE ATLETA ATTIVO|Readiness RED", re.IGNORECASE)


def _strip_override_lines(note) -> Optional[str]:
    """Rimuove dalla nota le righe con avvisi di override/Readiness RED
    (visibili solo quando la Readiness attuale è davvero RED). Ritorna None se
    la nota risulta vuota, così il frontend non renderizza la riga di avviso."""
    if not note or not isinstance(note, str):
        return note
    clean = "\n".join(ln for ln in note.split("\n") if not _OVERRIDE_NOTE_RE.search(ln)).strip()
    return clean or None


# =============================================================================
# Modelli Pydantic
# =============================================================================
class InjuryCreate(BaseModel):
    part: str = Field(..., min_length=2, max_length=40, pattern=r"^[a-z0-9_]+$")  # id zona (es. "calf_l")
    label: str = Field(..., min_length=1, max_length=60)     # etichetta leggibile
    severity: int = Field(1, ge=1, le=5)                     # 1-5
    note: Optional[str] = Field(None, max_length=300)
    x: Optional[float] = Field(None, ge=0, le=200)           # punto esatto del pin (viewBox)
    y: Optional[float] = Field(None, ge=0, le=430)


class InjuryUpdate(BaseModel):
    severity: Optional[int] = Field(None, ge=1, le=5)
    note: Optional[str] = Field(None, max_length=300)
    resolved: Optional[bool] = None
    x: Optional[float] = Field(None, ge=0, le=200)
    y: Optional[float] = Field(None, ge=0, le=430)


class WorkoutGenerateRequest(BaseModel):
    request: Optional[str] = Field(None, max_length=1000)
    override: Optional[bool] = None      # None = auto-detect insistenza


class WorkoutSyncRequest(BaseModel):
    date: Optional[str] = None           # YYYY-MM-DD; default = domani


class GoalPayload(BaseModel):
    race_type: str = Field(..., pattern="^(10K|21K)$")
    target_time_s: int = Field(..., gt=0, le=6 * 3600)
    enabled: bool = True
    race_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class TableGenerateRequest(BaseModel):
    duration: str = Field(..., pattern="^(1w|2w|1m|race)$")
    days: List[int] = Field(..., min_length=1, max_length=7)   # 0=Lun ... 6=Dom
    notes: Optional[str] = Field(None, max_length=500)


class TableSyncRequest(BaseModel):
    date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")  # None = tutti i non sincronizzati


class TableAdaptRequest(BaseModel):
    mode: str = Field(..., pattern="^(adapt|override)$")
    date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")   # default = oggi


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=1000)
    override: Optional[bool] = None
    history: Optional[List[dict]] = Field(None, max_length=20)


# =============================================================================
# Utility condivise
# =============================================================================
def _load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


_EMPTY_SUMMARY = {
    "readiness": {}, "biometria": {}, "allenamento": {}, "progressi": {},
    "attivita_recenti": [], "atleta": {},
}


def _load_summary(email: Optional[str] = None) -> dict:
    """Contesto compatto dell'atleta, ISOLATO per user_id (SQL).
    - Account principale (config): pipeline LIVE Garmin (comportamento storico);
    - Altri utenti: contesto SEMPRE rigenerato dal dump per-utente
      data/dumps/garmin_raw_dump_{slug}.json via AnalyticsEngine(dump, email),
      con cache SQL aggiornata ad ogni rigenerazione: elimina istantaneamente
      eventuali cache residue con i dati dell'account principale."""
    email = ((email or _default_email()) or "").strip().lower()
    owner = (_default_email() or "").strip().lower()
    uid = _user_id_for(email)
    if email == owner:
        try:
            from analytics_engine import AnalyticsEngine
            return _decorate_summary(AnalyticsEngine().build_context(), email)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"_load_summary: analisi live fallita ({err}), uso cache SQL")
    # Account secondari: rigenerazione LIVE dal proprio dump (mai cache sporca)
    try:
        from analytics_engine import AnalyticsEngine
        from garmin_probe import per_user_dump_path
        dump_path = per_user_dump_path(email)
        if dump_path.exists():
            dump = AnalyticsEngine._load_json(dump_path)
            summary = AnalyticsEngine(dump=dump, email=email).build_context()
            db.save_biometric(uid, summary)
            return _decorate_summary(summary, email)
        logger.warning(f"_load_summary: dump per-utente assente ({dump_path}), uso cache SQL")
    except Exception as err:  # noqa: BLE001
        logger.warning(f"_load_summary: analisi per-utente fallita ({err}), uso cache SQL")
    cached = db.get_biometric(uid)
    if cached:
        if "hrv_weekly" not in ((cached.get("readiness") or {}).get("components") or {}):
            cached["readiness"] = {}
        return _decorate_summary(cached, email)
    return _decorate_summary(copy.deepcopy(_EMPTY_SUMMARY), email)


def _merge_goal(summary: dict, email: Optional[str] = None) -> dict:
    """Inietta l'obiettivo di gara (se configurato) nel contesto per l'AI.

    NON tocca la REGOLA SOGLIA: fc_soglia 176 / passo_soglia 4:44 restano i
    valori ufficiali Garmin già presenti in summary['atleta'].
    """
    goals = _load_goals(email)
    if not goals.get("enabled") or not goals.get("race_type"):
        return summary

    ctx = copy.deepcopy(summary)
    rt = goals["race_type"]
    tgt = int(goals["target_time_s"])
    km = 10.0 if rt == "10K" else 21.0975
    pace_sec_km = tgt / km
    m, s = divmod(int(round(pace_sec_km)), 60)
    pace_display = f"{m}:{s:02d}/km"

    ctx["obiettivo_gara"] = {
        "distanza": rt,
        "tempo_target": tgt,
        "tempo_target_display": _fmt_clock(tgt),
        "ritmo_target": pace_display,
        "race_date": goals.get("race_date"),
        "note": (f"Obiettivo di gara: {rt} in {_fmt_clock(tgt)} "
                 f"(ritmo target ~{pace_display}). Il coach deve costruire "
                 "l'allenamento tenendo conto di questo obiettivo di gara "
                 "senza alterare la FC soglia ufficiale Garmin (176 bpm) "
                 "né il passo soglia (4:44/km)."),
    }
    if goals.get("race_date"):
        ctx["obiettivo_gara"]["note"] += f" Gara il {goals['race_date']}."
    return ctx


def _fmt_clock(sec: int) -> str:
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _fmt_pace(sec_km) -> str:
    if not sec_km:
        return "n/a"
    m, s = divmod(int(round(sec_km)), 60)
    return f"{m}:{s:02d}/km"


def _pace_to_sec(pace_str) -> Optional[int]:
    """'5:35/km' -> 335 s/km (None se non parlabile)."""
    if not pace_str:
        return None
    try:
        t = str(pace_str).strip().split("/")[0]
        m, s = t.split(":")
        return int(m) * 60 + int(s)
    except Exception:  # noqa: BLE001
        return None


def _fmt_dur(sec) -> str:
    if not sec:
        return "n/a"
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _workout_distance_m(workout: dict) -> Optional[int]:
    """Distanza totale prescritta in metri: fasi con distanza_m diretta, fasi a
    tempo convertite dal ritmo, ripetute conteggiate con le iterazioni e i
    recuperi come jog a ritmo_recupero (coerente con workoutTotalDistanceM)."""
    fasi = workout.get("fasi") or []
    if not fasi:
        return None
    tot = 0.0
    for f in fasi:
        d = f.get("distanza_m")
        n = f.get("ripetizioni") or 1
        if d:
            tot += d * n
        else:
            dur = f.get("durata_s")
            rit = _pace_to_sec(f.get("ritmo"))
            if dur and rit:
                tot += dur / rit * 1000.0
        # recuperi delle ripetute: jog di recupero a ritmo_recupero (es. 10:00/km)
        rec = f.get("recupero_s")
        rit_rec = _pace_to_sec(f.get("ritmo_recupero"))
        if rec and rit_rec and n:
            tot += rec * n / rit_rec * 1000.0
    return round(tot) if tot else None


def _workout_completed_state(summary: dict) -> dict:
    """STATO 'ALLENAMENTO ESEGUITO' (v67): se l'utente ha svolto un'attività
    di corsa OGGI (summary['attivita_recenti'][0]), espone i dati reali
    d'esecuzione (distanza km, durata, passo medio, FC media) e il flag
    garmin_workout_used (corsa guidata dall'orologio Garmin)."""
    attivita = (summary or {}).get("attivita_recenti") or []
    if not attivita:
        return {"workout_completed": False}
    # Parsing robusto della data Garmin: "2026-08-17T07:00:00" o "2026-08-17 07:00:00"
    # -> "2026-08-17" (v72): confronto pulito con date.today().isoformat().
    data_attivita = str(attivita[0].get("data") or "").split("T")[0].split(" ")[0]
    if data_attivita != date.today().isoformat():
        return {"workout_completed": False}
    a = attivita[0]
    if not a.get("tipo"):
        return {"workout_completed": False}
    return {
        "workout_completed": True,
        "distanza_km": a.get("distanza_km"),
        "durata_s": a.get("tempo_s"),
        "passo_medio": a.get("ritmo_medio"),
        "hr_medio": a.get("hr_medio"),
        "garmin_workout_used": bool(a.get("garmin_workout_used")),
    }


def _decorate_summary(summary: dict, email: Optional[str] = None) -> dict:
    """DECORAZIONE OBBLIGATORIA DEL SUMMARY (v75): prima di restituire il
    dict da _load_summary esegue SEMPRE l'unione con lo stato 'ALLENAMENTO
    ESEGUITO' odierno (_workout_completed_state) ed inietta il
    post_run_feedback (confronto eseguito vs prescritto). Così la risposta
    API di /api/readiness contiene SEMPRE workout_completed: True e i dati
    reali d'esecuzione quando l'utente ha corso oggi, indipendentemente
    dal percorso di generazione (live / dump per-utente / cache SQL).
    Il piano prescritto per il feedback è summary['allenamento'] (piano di
    oggi generato nel contesto).
    v87: inietta anche la MAPPA INFORTUNI attivi + impatto biomeccanico
    (15ª metrica per l'incrocio del Coach AI)."""
    if not isinstance(summary, dict):
        return summary
    summary.update(_workout_completed_state(summary))
    summary["post_run_feedback"] = _post_run_feedback(summary, summary.get("allenamento"))
    try:
        from analytics_engine import active_injuries_impact
        attivi = [i for i in _load_injuries(email) if not i.get("resolved")]
        impact = active_injuries_impact(attivi)
        summary["infortuni"] = attivi
        summary["injury_impact_pct"] = impact.get("pct") if isinstance(impact, dict) else None
    except Exception as err:  # noqa: BLE001
        logger.warning(f"decorate: infortuni non disponibili ({err})")
        summary["infortuni"] = []
        summary["injury_impact_pct"] = None
    return summary


def _sec_to_pace(sec_km: Optional[float]) -> str:
    """Convertitore ritmo: 338.9 -> "5:39/km" (v79)."""
    if not sec_km or sec_km <= 0:
        return "—"
    m, s = int(sec_km // 60), int(round(sec_km % 60))
    if s >= 60:
        m += 1
        s = 0
    return f"{m}:{s:02d}/km"


def _group_laps_by_workout_phase(laps: list) -> list:
    """RAGGRUPPAMENTO INTELLIGENTE CONTIGUO dei lap reali Garmin (v79).

    Riceve i lap classificati dall'orologio (running_analysis[0]["laps"] ->
    attivita_recenti[0]["laps"]) e li raggruppa in macro-fasi consecutive
    SENZA tralasciare i lap di transizione:
    - il PRIMO lap singolo = Riscaldamento (Warmup);
    - i blocchi lunghi (>=120s, >=300m, ritmo <=6:40/km) = Corsa Facile Z2
      (main); i lap di transizione corti (<250m e <90s) adiacenti vengono
      uniti al blocco (es. Lap 2 di 2km + Lap 3 di 2km + Lap 4 di 119m);
    - le sequenze di ALLUNGHI (<=25s, ritmo <=5:30/km) alternate alle
      camminate di recupero (<=4') formano un'unica macro-fase
      (es. Lap 5..12 = 4 allunghi 15s + 4 recuperi 1' camminati);
    - tutto ciò che resta dopo gli allunghi = Defaticamento (Cooldown).
    """
    laps = [l for l in (laps or []) if l and l.get("lap_index") is not None]
    if not laps:
        return []

    def _pace(l):
        return l.get("pace_sec_km")

    def _allungo(l):
        return ((l.get("duration_s") or 0) <= 25
                and (l.get("distance_m") or 0) < 150
                and (_pace(l) or 999) <= 330)

    def _transizione(l):
        return ((l.get("duration_s") or 0) <= 90
                and (l.get("distance_m") or 0) < 250
                and not _allungo(l))

    def _camminata(l):
        return ((l.get("duration_s") or 0) <= 240
                and (_pace(l) or 0) > 420)

    def _main(l):
        p = _pace(l) or 0
        return ((l.get("duration_s") or 0) >= 120
                and (l.get("distance_m") or 0) >= 300
                and 0 < p <= 400)

    def _make(tag, group):
        dist_m = sum((l.get("distance_m") or 0) for l in group)
        dur_s = sum((l.get("duration_s") or 0) for l in group)
        # HR medio pesato per la durata, HR max = picco assoluto
        tot_dur = float(sum((l.get("duration_s") or 0) for l in group)) or 1.0
        hr_medio = int(round(sum((l.get("avg_hr") or 0) * (l.get("duration_s") or 0) for l in group) / tot_dur))
        hr_max = max((l.get("max_hr") or 0) for l in group) or None
        pace_sec = (dur_s / (dist_m / 1000.0)) if dist_m else None
        names = {
            "WARMUP": "Riscaldamento (Warmup)",
            "MAIN": "Corsa Facile Z2",
            "ALLUNGHI": "Allunghi 15s + Recuperi 1'",
            "COOLDOWN": "Defaticamento (Cooldown)",
        }
        return {
            "tag": tag,
            "nome": names.get(tag, tag),
            "lap_indices": [l.get("lap_index") for l in group],
            "lap_count": len(group),
            "distanza_km": round(dist_m / 1000.0, 2) if dist_m else 0.0,
            "durata_s": dur_s,
            "pace_sec_km": round(pace_sec, 1) if pace_sec else None,
            "pace_display": _sec_to_pace(pace_sec),
            "hr_medio": hr_medio if hr_medio else None,
            "hr_max": hr_max,
            "laps": group,
        }

    groups = []
    i, n = 1, len(laps)
    # 1) Riscaldamento: primo lap singolo
    groups.append(_make("WARMUP", [laps[0]]))
    while i < n:
        l = laps[i]
        if _main(l):
            grp = [l]
            i += 1
            while i < n and (_main(laps[i]) or _transizione(laps[i])):
                grp.append(laps[i])
                i += 1
            groups.append(_make("MAIN", grp))
        elif _allungo(l):
            grp = [l]
            i += 1
            while i < n and (_allungo(laps[i]) or _camminata(laps[i]) or _transizione(laps[i])):
                grp.append(laps[i])
                i += 1
            groups.append(_make("ALLUNGHI", grp))
        else:
            grp = [l]
            i += 1
            while i < n and not _main(laps[i]) and not _allungo(laps[i]):
                grp.append(laps[i])
                i += 1
            groups.append(_make("COOLDOWN", grp))
    return groups


def _overpacing_check(workout: dict, macro_fasi: list) -> Optional[dict]:
    """PENALIZZAZIONE PROPORZIONALE OVERPACING DINAMICO (v81).

    Confronta ogni macro-fase ESEGUITA con la fase prescritta omologa
    (Warmup->warmup, Main->ripetute/tempo, Cooldown->cooldown; gli Allunghi
    sono SEMPRE esenti, tolleranza illimitata per costruzione).

    Se il ritmo eseguito è PIÙ VELOCE del target oltre la tolleranza
    proporzionale alla distanza della fase (v. overpacing_tolerance_sec_km),
    ritorna {penalita (0..1), eccesso_s_km, tolleranza, avviso} altrimenti None.
    """
    if not workout or not macro_fasi:
        return None
    from analytics_engine import overpacing_tolerance_sec_km

    fasi = workout.get("fasi") or []
    target_warmup = None
    target_cooldown = None
    target_qualita = None  # repeat / interval / tempo / easy_run / progression
    for f in fasi:
        t = f.get("tipo") or ""
        r = _pace_to_sec(f.get("ritmo"))
        if not r:
            continue
        if t == "warmup" and target_warmup is None:
            target_warmup = r
        elif t == "cooldown" and target_cooldown is None:
            target_cooldown = r
        elif t in ("repeat", "interval", "tempo", "easy_run", "progression"):
            if target_qualita is None:
                target_qualita = r

    for mf in macro_fasi:
        tag = mf.get("tag")
        ritmo_eff = mf.get("pace_sec_km")
        if tag == "ALLUNGHI" or not ritmo_eff:
            continue  # allunghi: tolleranza illimitata, mai penalizzati
        target = {"WARMUP": target_warmup, "MAIN": target_qualita,
                  "COOLDOWN": target_cooldown}.get(tag)
        if not target:
            continue  # nessun target prescritto confrontabile
        if ritmo_eff >= target:
            continue  # più LENTO del target -> nessun overpacing
        dist_m = (mf.get("distanza_km") or 0) * 1000.0
        dur_s = mf.get("durata_s") or 0
        tol = overpacing_tolerance_sec_km(dist_m, dur_s)
        if tol is None:
            continue  # tolleranza illimitata (fase < 100m o < 30s)
        scostamento = target - ritmo_eff  # s/km più veloce del target
        if scostamento <= tol:
            continue  # dentro la tolleranza proporzionale
        eccesso = scostamento - tol
        penalita = min(eccesso / target, 0.5)  # max 50% per overpacing
        return {
            "penalita": penalita,
            "eccesso_s_km": round(eccesso, 1),
            "tolleranza": tol,
            "fase": mf.get("nome"),
            "pace_eseguito": mf.get("pace_display"),
            "pace_target": _sec_to_pace(target),
            "avviso": (
                f"Overpacing nella fase {mf.get('nome')}: corso a {mf.get('pace_display')} "
                f"vs target {_sec_to_pace(target)} — {eccesso:.0f}s/km OLTRE la tolleranza "
                f"di {tol}s/km prevista per questa distanza. Il surplus di velocità "
                f"compromette la qualità della seduta: rallenta ai ritmi prescritti."),
        }
    return None


def _post_run_feedback(summary: dict, workout: dict) -> dict:
    """RESOCONTO & FEEDBACK POST-CORSA (v66): se l'utente ha completato
    un'attività OGGI (summary['attivita_recenti'][0]), confronta l'attività
    reale (distanza, tempo, passo medio, FC media) con l'allenamento
    prescritto e calcola l'accuratezza d'esecuzione %."""
    empty = {"completed": False}
    if not summary or not workout:
        return empty
    attivita = summary.get("attivita_recenti") or []
    if not attivita:
        return empty
    # Parsing robusto della data Garmin (v75, coerente con v72):
    # "2026-08-17T07:00:00" o "2026-08-17 07:00:00" -> "2026-08-17"
    data_attivita = str(attivita[0].get("data") or "").split("T")[0].split(" ")[0]
    if data_attivita != date.today().isoformat():
        return empty

    a = attivita[0]
    pres_dist = _workout_distance_m(workout)
    pres_tempo = workout.get("durata_totale_s")
    pres_ritmo = _pace_to_sec(workout.get("ritmo_riferimento"))
    hr_ranges = [f.get("hr_target") for f in (workout.get("fasi") or [])
                 if f and isinstance(f.get("hr_target"), (list, tuple))
                 and len(f.get("hr_target")) == 2]
    pres_hr = round(sum((lo + hi) / 2 for lo, hi in hr_ranges) / len(hr_ranges)) if hr_ranges else None

    ex_dist = int(round((a.get("distanza_km") or 0) * 1000))
    ex_tempo = a.get("tempo_s")
    ex_ritmo = _pace_to_sec(a.get("ritmo_medio"))
    ex_hr = a.get("hr_medio")

    # Penalità % per ogni metrica confrontabile (0..1), media -> accuratezza
    pen = []
    if pres_dist and ex_dist:
        pen.append(min(abs(ex_dist - pres_dist) / pres_dist, 1.0))
    if pres_tempo and ex_tempo:
        pen.append(min(abs(ex_tempo - pres_tempo) / pres_tempo, 1.0))
    if pres_ritmo and ex_ritmo:
        pen.append(min(abs(ex_ritmo - pres_ritmo) / pres_ritmo, 1.0))
    if pres_hr and ex_hr and hr_ranges:
        hr_lo = min(lo for lo, _ in hr_ranges)
        hr_hi = max(hi for _, hi in hr_ranges)
        if ex_hr < hr_lo:
            pen.append(min((hr_lo - ex_hr) / hr_lo, 0.5))
        elif ex_hr > hr_hi:
            pen.append(min((ex_hr - hr_hi) / hr_hi, 0.5))

    # v81: PENALIZZAZIONE PROPORZIONALE OVERPACING DINAMICO — se una
    # macro-fase eseguita è PIÙ VELOCE del target oltre la tolleranza
    # proporzionale alla distanza, aggiunge la penalità % e l'avviso in nota.
    macro_fasi = _group_laps_by_workout_phase(a.get("laps"))
    overpacing = _overpacing_check(workout, macro_fasi)
    if overpacing:
        pen.append(overpacing["penalita"])

    # Accuratezza %: RMS delle penalità (penalizza di più gli scostamenti
    # grandi rispetto alla media aritmetica) -> esecuzione fedele = 100%.
    if pen:
        rms = (sum(p * p for p in pen) / len(pen)) ** 0.5
        acc = max(0.0, 100.0 - 100.0 * rms)
    else:
        acc = 100.0
    if acc >= 95:
        score, note = f"{acc:.0f}% PERFETTA", (
            "Esecuzione in linea con la prescrizione: distanza, ritmo e intensità rispettati. Ottimo lavoro!")
    elif acc >= 80:
        score, note = f"{acc:.0f}% OTTIMA", (
            "Ottima esecuzione: lievi scostamenti dalla scheda, nessuna criticità fisiologica.")
    elif acc >= 60:
        score, note = f"{acc:.0f}% BUONA", (
            "Buona esecuzione: qualche scostamento da distanza/ritmo. Cerca di rispettare meglio i ritmi prescritti.")
    else:
        score, note = f"{acc:.0f}% DA RIVEDERE", (
            "Esecuzione lontana dalla prescrizione: controlla recupero e ritmi nelle prossime sedute.")
    if overpacing:
        note = "⚠️ OVERPACING — " + overpacing["avviso"] + " " + note

    return {
        "completed": True,
        "accuracy_score": score,
        "prescribed": (
            f"{(pres_dist or 0) / 1000.0:.1f} km · {_fmt_dur(pres_tempo or 0)}"
            f"{' · @' + workout.get('ritmo_riferimento') if workout.get('ritmo_riferimento') else ''}"
            f"{' · HR ' + str(pres_hr) if pres_hr else ''}"),
        "executed": (
            f"{a.get('distanza_km') or 0:.1f} km · {_fmt_dur(ex_tempo or 0)}"
            f"{' · @' + (a.get('ritmo_medio') or 'n/a') if ex_tempo else ''}"
            f"{' · HR ' + str(ex_hr) if ex_hr else ''}"),
        "coach_note": note,
        # v79: macro-fasi raggruppate (contigue, con transizioni unite) +
        # elenco COMPLETO dei lap reali dell'orologio Garmin (zero omissioni).
        "macro_fasi": macro_fasi,
        "laps": a.get("laps") or [],
        "dettagli": {
            "distanza_prescritta_m": pres_dist,
            "distanza_eseguita_m": ex_dist,
            "tempo_prescritto_s": pres_tempo,
            "tempo_eseguito_s": ex_tempo,
            "ritmo_prescritto_sec_km": pres_ritmo,
            "ritmo_eseguito_sec_km": ex_ritmo,
            "hr_prescritta": pres_hr,
            "hr_eseguita": ex_hr,
            "accuracy_pct": round(acc, 1),
        },
    }


# =============================================================================
# Health
# =============================================================================
@app.get("/api/health")
def health(request: Request, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    try:
        summary = _load_summary(request.state.user_email)
        r = summary.get("readiness", {})
        score = r.get("readiness_score")
        readiness = {"score": score, "level": r.get("readiness_level")} if score is not None else None
    except Exception as err:  # noqa: BLE001
        logger.warning(f"health: readiness non disponibile ({err})")
        readiness = None
    return {
        "status": "ok",
        "service": "myptai-web",
        "version": app.version,
        "date": date.today().isoformat(),
        "readiness": readiness,
    }


# =============================================================================
# Readiness
# =============================================================================
@app.get("/api/readiness")
def get_readiness(request: Request, response: Response):
    """Readiness + biometria + progressi + attività recenti (dati per-utente)."""
    response.headers["Cache-Control"] = "private, no-store"
    email = getattr(request.state, "user_email", None)
    try:
        summary = _load_summary(email)
    except Exception as err:  # noqa: BLE001
        logger.exception("Errore /api/readiness")
        raise HTTPException(status_code=500, detail=f"Analisi non disponibile: {err}")

    r = summary.get("readiness", {})
    # RESOCONTO & FEEDBACK POST-CORSA (v66): confronto prescritto vs eseguito
    # se l'utente ha completato un'attività oggi (summary['attivita_recenti'])
    planned = _load_today_planned(email)
    return {
        "display_name": summary.get("atleta", {}).get("nome"),
        "email": email,
        "garmin_data_date": summary.get("garmin_data_date"),
        "garmin_fetched_at": summary.get("garmin_fetched_at"),
        "garmin_training_readiness": summary.get("garmin_training_readiness"),
        "garmin_vo2max": summary.get("garmin_vo2max"),
        "garmin_official_threshold": summary.get("garmin_official_threshold", {}),
        "readiness_estimated": r.get("estimated", False),
        "readiness_score": r.get("readiness_score"),
        "readiness_level": r.get("readiness_level"),
        "components": r.get("components", {}),
        "weights": r.get("weights", {}),
        "hrv_calibrating": r.get("hrv_calibrating", False),
        "rhr_reference": r.get("rhr_reference", {}),
        "rhr_adjustment_penalty": r.get("rhr_adjustment_penalty"),
        "biometria": summary.get("biometria", {}),
        "allenamento": summary.get("allenamento", {}),
        "progressi": summary.get("progressi", {}),
        "attivita_recenti": summary.get("attivita_recenti", []),
        "atleta": summary.get("atleta", {}),
        "data": date.today().isoformat(),
        "post_run_feedback": _post_run_feedback(summary, planned.get("workout")),
        # STATO 'ALLENAMENTO ESEGUITO' (v67): true + dati reali + flag orologio
        **_workout_completed_state(summary),
    }


# =============================================================================
# Memoria Master Atleta (athlete_state.json) & Workflow Scientifici
# =============================================================================
@app.get("/api/memory")
def get_athlete_memory(request: Request):
    """Restituisce la memoria persistente dell'atleta (profilo, PB, Karvonen, ACWR, recovery)."""
    from athlete_state import load_athlete_state, sync_state_from_biometrics
    email = getattr(request.state, "user_email", None)
    try:
        summary = _load_summary(email)
        state = sync_state_from_biometrics(summary, email)
        return state
    except Exception:
        return load_athlete_state(email)


@app.post("/api/memory")
async def update_athlete_memory(request: Request):
    """Aggiorna le note soggettive o il profilo nella memoria persistente."""
    from athlete_state import load_athlete_state, save_athlete_state
    email = getattr(request.state, "user_email", None)
    body = await request.json()
    state = load_athlete_state(email)
    if isinstance(body, dict):
        if "profilo_atleta" in body:
            state["profilo_atleta"].update(body["profilo_atleta"])
        if "note_soggettive_e_stile_vita" in body:
            state["note_soggettive_e_stile_vita"].update(body["note_soggettive_e_stile_vita"])
        if "log_infortuni_e_fastidi" in body:
            state["log_infortuni_e_fastidi"] = body["log_infortuni_e_fastidi"]
        save_athlete_state(state, email)
    return {"status": "ok", "state": state}


@app.post("/api/workouts/morning-check")
async def morning_check_workflow(request: Request):
    """WORKFLOW 1: Check Mattutino Automatico (Readiness + Fisiologia + Decision Engine + Sync Ready)."""
    from ai_coach import AICoach
    email = getattr(request.state, "user_email", None)
    summary = _load_summary(email)
    planned = _load_today_planned(email)
    coach = AICoach(summary=summary)
    result = await asyncio.to_thread(coach.evaluate_morning_readiness, planned.get("workout"))
    return result


@app.post("/api/workouts/post-run-deepdive")
async def post_run_deepdive_workflow(request: Request):
    """WORKFLOW 2: Analisi Post-Corsa Dettagliata (Lap-by-Lap, Voto /10, Split Errors, Biomeccanica)."""
    from ai_coach import AICoach
    email = getattr(request.state, "user_email", None)
    summary = _load_summary(email)
    activities = summary.get("attivita_recenti", [])
    if not activities:
        raise HTTPException(status_code=400, detail="Nessuna attività recente trovata nel profilo")

    latest_activity = activities[0]
    coach = AICoach(summary=summary)
    review = await asyncio.to_thread(coach.analyze_post_run_activity, latest_activity)
    return review


@app.post("/api/refresh")
async def refresh_garmin_data(request: Request):
    """Sync live PER-UTENTE: scarica dati freschi da Garmin Connect (con il
    tokenstore cifrato dell'utente) e rigenera il contesto nel Database SQL.
    - Account principale: pipeline storica (config) + cache SQL;
    - Altri utenti: dump per-utente (data/dumps/...) + summary in SQL."""
    email = getattr(request.state, "user_email", None)
    uid = getattr(request.state, "user_id", None)
    owner = _default_email()
    try:
        from garmin_probe import run_probe, per_user_dump_path
        from analytics_engine import AnalyticsEngine

        if (email or "").lower() == (owner or "").lower():
            # Account principale: pipeline storica con credenziali config
            await asyncio.to_thread(run_probe)
            summary = await asyncio.to_thread(AnalyticsEngine().run)
        else:
            # Altri utenti: tokenstore cifrato -> dump per-utente -> analisi
            tokens = db.get_user_tokens(uid) if uid else None
            if not tokens:
                raise HTTPException(
                    status_code=400,
                    detail="Sessione Garmin non disponibile: effettua di nuovo il login.")
            tmp_dir = Path(tempfile.mkdtemp(prefix="myptai_refresh_"))
            try:
                ts_dir = _write_garmin_token_dir(tokens, tmp_dir, email)
                dump_path = per_user_dump_path(email)
                await asyncio.to_thread(
                    run_probe, email, "", str(ts_dir), dump_path)
                if not dump_path.exists():
                    raise RuntimeError("dump Garmin per-utente non generato")
                with open(dump_path, "r", encoding="utf-8") as f:
                    dump = json.load(f)
                summary = AnalyticsEngine(dump=dump).build_context()
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        # Cache per-utente del contesto nel Database SQL (multi-dispositivo)
        if summary:
            db.save_biometric(uid or _user_id_for(email), summary)
        biometrics = summary.get("biometria", {})
        threshold = summary.get("garmin_official_threshold", {})
        readiness = summary.get("readiness", {})
        required = {
            "Durata sonno": biometrics.get("sonno_secondi"),
            "Sleep Score": biometrics.get("sleep_score"),
            "Stress notturno": biometrics.get("stress_notte"),
            "HRV notturna": biometrics.get("hrv_last_night"),
            "HRV settimanale": biometrics.get("hrv_weekly"),
            "Fascia HRV personale": None if readiness.get("hrv_calibrating") else biometrics.get("hrv_baseline"),
            "Body Battery": biometrics.get("body_battery_attuale"),
            "FC a riposo": biometrics.get("rhr"),
            "Stress diurno": biometrics.get("stress_giorno"),
            "VO₂max": summary.get("garmin_vo2max"),
            "FC soglia Garmin": threshold.get("heart_rate"),
            "Passo soglia Garmin": threshold.get("pace_sec_per_km"),
        }
        missing = [label for label, value in required.items() if value is None or value == {}]
        return {"status": "partial" if missing else "ok",
                "message": "Dati Garmin aggiornati",
                "date": summary.get("garmin_data_date"),
                "missing_metrics": missing}
    except HTTPException:
        raise
    except SystemExit:
        raise HTTPException(status_code=502, detail="Login Garmin fallito: dati locali ancora validi")
    except Exception as err:  # noqa: BLE001
        logger.exception("Errore /api/refresh")
        raise HTTPException(status_code=502, detail=f"Sync Garmin fallito: {err}")


# =============================================================================
# Allenamenti
# =============================================================================
@app.get("/api/workouts/latest")
def get_latest_workout(request: Request):
    email = getattr(request.state, "user_email", None)
    uid = getattr(request.state, "user_id", None)
    plan = _load_plan(email)
    if not plan or not plan.get("workout"):
        raise HTTPException(status_code=404, detail="Nessun allenamento generato. Usa /api/workouts/generate")
    # Re-verifica REALE del calendario Garmin Connect (con TTL per-utente):
    # se il workout non è più presente (eliminato dall'utente) -> synced=False
    _recheck_calendar_sync(email, uid, plan, None)
    _decorate_workout(plan.get("workout") or {})
    # v76: INIEZIONE OBBLIGATORIA dello stato "ALLENAMENTO ESEGUITO" odierno
    # nel payload di /api/workouts/latest (stessa fonte decorata di
    # /api/readiness): workout_completed, dati reali d'esecuzione e feedback
    # sono sempre presenti quando l'utente ha corso oggi.
    try:
        _summary = _load_summary(email)
        for _k in ("workout_completed", "distanza_km", "durata_s", "passo_medio",
                   "hr_medio", "garmin_workout_used", "post_run_feedback", "attivita_recenti"):
            if _k in _summary:
                plan[_k] = _summary[_k]
    except Exception:  # noqa: BLE001
        logger.warning("get_latest_workout: iniezione stato fallita, payload senza workout_completed")
    return plan


@app.post("/api/workouts/generate")
async def generate_workout(payload: WorkoutGenerateRequest, request: Request):
    """Genera l'allenamento con DeepSeek (riusa AICoach) e lo salva nel DB SQL."""
    from ai_coach import AICoach

    email = getattr(request.state, "user_email", None)
    summary = _merge_goal(_load_summary(email), email)
    coach = AICoach(summary=summary)

    override = payload.override
    if override is None:
        override = AICoach.detect_override(payload.request or "")

    try:
        result = await asyncio.to_thread(coach.generate, payload.request, override=override)
    except Exception as err:  # noqa: BLE001
        logger.exception("Errore generazione allenamento")
        raise HTTPException(status_code=502, detail=f"Generazione fallita: {err}")

    workout = result["workout"]
    valid, issues = coach.validate_workout(workout, override=override)
    plan_output = {
        "generated_at": date.today().isoformat(),
        "model": coach.model,
        "usage": result.get("usage", {}),
        "readiness_at_generation": coach.summary.get("readiness", {}),
        "override_richiesto": override,
        "validation_issues": issues,
        "workout": workout,
    }
    if (email or "").lower() == (_default_email() or "").lower():
        coach.save_output(plan_output)  # mirror legacy solo account principale
    _write_plan(email, plan_output)  # per-utente (SQL)
    readiness = coach.summary.get("readiness", {})
    return {
        "workout": _decorate_workout(workout),
        "valid": valid,
        "issues": issues,
        "override": _override_visible(override, readiness),
        "generated_at": plan_output["generated_at"],
    }


_user_ts_cache: dict = {}  # tokenstore temporanei decifrati per user_id (sync Garmin)


def _workout_sync_for(email: Optional[str], uid: Optional[int]):
    """WorkoutSync PER-UTENTE: l'account principale usa la pipeline storica
    (config.json); gli altri utenti usano il proprio tokenstore cifrato (DB).
    I tokenstore decifrati vivono in una dir temporanea (mai in chiaro su disco
    permanente) e vengono riusati per la durata del processo."""
    from workout_sync import WorkoutSync

    if (email or "").lower() == (_default_email() or "").lower():
        return WorkoutSync()
    tokens = db.get_user_tokens(uid) if uid else None
    if not tokens:
        raise HTTPException(status_code=403,
                            detail="Sessione Garmin non disponibile: effettua di nuovo il login")
    ts = _user_ts_cache.get(uid)
    if not ts or not ts.exists():
        ts = _write_garmin_token_dir(tokens, Path(tempfile.mkdtemp(prefix="myptai_sync_")), email)
        _user_ts_cache[uid] = ts
    cfg = copy.deepcopy(_load_config())
    cfg["garmin"] = {"email": email, "password": "", "tokenstore_path": str(ts)}
    return WorkoutSync(config=cfg)


def _same_workout(item: dict, plan: dict) -> bool:
    """v89: l'item sul calendario Garmin è lo STESSO allenamento del piano?
    Vero se il workoutId coincide OPPURE il nome corrisponde a quello del piano
    (es. 'Corsa di Scioglimento'). Usato da sync_workout per distinguere il
    'già caricato' (already) da un workout DIVERSO presente per oggi che va
    sostituito con la scheda consigliata."""
    wid = str(item.get("workoutId") or "")
    plan_id = str(((plan or {}).get("garmin_workout_id") or "") or "")
    if plan_id and wid and wid == plan_id:
        return True
    nm = str(item.get("workoutName") or "").lower()
    plan_name = str((((plan or {}).get("workout") or {}).get("nome") or "")).lower()
    return bool(plan_name) and plan_name in nm


@app.post("/api/workouts/sync")
async def sync_workout(payload: WorkoutSyncRequest, request: Request):
    """ONE-TAP: converte il piano corrente e lo carica sul calendario Garmin
    (per-utente: ciascun utente sul PROPRIO calendario Garmin).
    Idempotente: se il piano risulta già sincronizzato per quella data,
    risponde subito 200 senza un secondo upload a Garmin Connect."""
    email = getattr(request.state, "user_email", None)
    uid = getattr(request.state, "user_id", None)
    date_str = payload.date or date.today().isoformat()
    with _sync_lock_for(uid):
        plan = _load_plan(email)
        if not plan or not plan.get("workout"):
            raise HTTPException(status_code=400, detail="Nessun piano da sincronizzare: genera prima un allenamento")
        # Idempotenza: già caricato per questa data -> nessun secondo upload
        if plan.get("synced") and plan.get("synced_at") == date_str:
            return {
                "status": "ok",
                "already": True,
                "message": "Allenamento già caricato sul calendario Garmin",
                "date": date_str,
                "workout_id": plan.get("garmin_workout_id"),
                "garmin_workout": plan.get("garmin_workout_snapshot") or {},
            }
        try:
            # CHECK PREVENTIVO CALENDARIO REALE (v89): se per OGGI esiste GIÀ
            # un allenamento sul calendario Garmin Connect — a prescindere dal
            # garmin_workout_id locale — distinguiamo:
            #  · STESSO workout del piano (id o nome) -> already:True (bloccato,
            #    zero duplicati);
            #  · workout DIVERSO o NESSUN workout -> l'upload procede; un
            #    eventuale workout diverso per oggi viene prima rimosso, così il
            #    calendario ospita SOLO la scheda consigliata (utente che ha
            #    usato 'Elimina dal calendario' e preme di nuovo Sync).
            items = await asyncio.to_thread(_workout_sync_for(email, uid).scheduled_workouts)
            today_items = [i for i in items or []
                           if i.get("itemType") == "workout" and i.get("date") == date_str]
            matching = [i for i in today_items if _same_workout(i, plan)]
            if matching:
                real_id = matching[0].get("workoutId")
                plan["synced"] = True
                plan["synced_at"] = date_str
                plan["garmin_workout_id"] = real_id
                _write_plan(email, plan)
                return {
                    "status": "ok",
                    "already": True,
                    "message": "Allenamento già presente sul calendario Garmin",
                    "date": date_str,
                    "workout_id": real_id,
                    "garmin_workout": plan.get("garmin_workout_snapshot") or {},
                }
            for other in today_items:  # workout diversi per oggi: sostituzione pulita
                try:
                    await asyncio.to_thread(_workout_sync_for(email, uid).delete_workout,
                                            other.get("workoutId"))
                except Exception as err:  # noqa: BLE001
                    logger.warning(f"sync: rimozione workout diverso {other.get('workoutId')} fallita ({err})")
        except Exception as err:  # noqa: BLE001
            logger.warning(f"sync: check preventivo calendario fallito ({err}) — procedo con l'upload")
        try:
            result = await asyncio.to_thread(_workout_sync_for(email, uid).sync, date_str)
            # Traccia l'avvenuta sincronizzazione nel piano (evita upload duplicati)
            try:
                plan["synced"] = True
                plan["synced_at"] = date_str
                plan["garmin_workout_id"] = result.get("workout_id")
                plan["garmin_workout_snapshot"] = result.get("garmin_workout", {})
                _write_plan(email, plan)
            except (json.JSONDecodeError, OSError) as err:  # noqa: BLE001
                logger.warning(f"sync_workout: aggiornamento flag plan fallito ({err})")
        except Exception as err:  # noqa: BLE001
            logger.exception("Errore sync Garmin")
            raise HTTPException(status_code=502, detail=f"Sync su Garmin fallito: {err}")
    return {
        "status": "ok",
        "message": "Allenamento caricato sul calendario Garmin",
        "date": result.get("date"),
        "workout_id": result.get("workout_id"),
        "garmin_workout": result.get("garmin_workout", {}),
    }


def _load_today_planned(email: Optional[str] = None) -> dict:
    """Piano previsto per oggi (CARD MIDDLE / Adatta Seduta):
    priorità alla Tabella settimanale, altrimenti workout_plan.json."""
    planned = {"source": None, "workout": None, "synced": False, "adapted": False, "override": False}
    try:
        table = _load_table(email)
        entry = next((e for e in table.get("entries", []) if e.get("date") == date.today().isoformat()), None)
        if entry and entry.get("kind") == "workout":
            planned = {
                "source": "table",
                "workout": entry.get("workout"),
                "synced": entry.get("synced", False),
                "adapted": entry.get("adapted", False),
                "override": entry.get("override", False),
                "garmin_workout_id": entry.get("garmin_workout_id"),
            }
        else:
            plan = _load_plan(email)
            if plan.get("workout"):
                planned = {
                    "source": "plan",
                    "workout": plan.get("workout"),
                    "synced": plan.get("synced", False),
                    "adapted": plan.get("adapted", False),
                    "override": plan.get("override_richiesto", False),
                    "garmin_workout_id": plan.get("garmin_workout_id"),
                }
    except Exception as err:  # noqa: BLE001
        logger.warning(f"today: piano previsto non disponibile ({err})")
    _decorate_workout(planned.get("workout") or {})
    return planned


def _override_visible(flag, readiness: dict) -> bool:
    """Un avviso 'override atleta' è visibile SOLO se l'override è attivo E la
    Readiness attuale è effettivamente ROSSA. Se oggi è GREEN/AMBER, il flag
    persistito è un avviso vecchio (da un giorno con recupero insufficiente)
    e non va mostrato nell'App."""
    return bool(flag) and (readiness.get("readiness_level") == "RED")


_CAL_RECHECK_TTL_S = 15 * 60          # re-verifica calendario al massimo ogni 15' (latest)
_cal_recheck_ts: dict = {}            # uid -> timestamp ultima re-verifica


def _recheck_calendar_sync(email, uid, planned, today_items=None):
    """RE-VERIFICA REALE del calendario Garmin Connect per il piano di oggi.

    Se planned.synced=True ma il workout NON è più tra i calendarItems di oggi
    (eliminato manualmente dall'utente su Garmin Connect), aggiorna synced=False
    sia nella risposta sia nel persistente (piano o entry della tabella).
    Fail-safe: su errore di rete (token scaduto/offline) NON modifica nulla.
    - today_items passato: re-verifica gratuita (endpoint today ha già
      interrogato il calendario per la CARD TOP).
    - today_items=None: query live con TTL per-utente (endpoint latest, usato
      dalla Home: evita di rallentare ogni render con una chiamata Garmin).
    """
    if not planned or not planned.get("synced") or not planned.get("garmin_workout_id"):
        return planned
    wid = str(planned.get("garmin_workout_id"))
    items = today_items
    if items is None:
        now = time.time()
        if _cal_recheck_ts.get(uid, 0) > now - _CAL_RECHECK_TTL_S:
            return planned
        _cal_recheck_ts[uid] = now
        try:
            items = _workout_sync_for(email, uid).scheduled_workouts()
        except Exception as err:  # noqa: BLE001
            logger.warning(f"recheck calendario Garmin fallito per {email} ({err})")
            return planned
    gids = {str(i.get("workoutId")) for i in items or []
            if i.get("itemType") == "workout" and i.get("date") == date.today().isoformat()}
    if wid in gids:
        return planned
    # Workout sparito dal calendario: sblocca il sync (risposta + persistente)
    planned["synced"] = False
    if planned.get("source") == "table":
        table = _load_table(email)
        for e in table.get("entries") or []:
            if (e.get("date") == date.today().isoformat()
                    and str(e.get("garmin_workout_id") or "") == wid):
                e["synced"] = False
                e["synced_at"] = None
                break
        db.save_workout_doc(_user_id_for(email), "table", table)
    else:
        plan = _load_plan(email)
        if plan.get("synced") and str(plan.get("garmin_workout_id") or "") == wid:
            plan["synced"] = False
            plan["synced_at"] = None
            _write_plan(email, plan)
    logger.info(f"recheck calendario Garmin: workout {wid} non più sul calendario di {email} -> synced=False")
    return planned


def _fmt_score(score) -> str:
    """Formatta lo score Readiness: 86.3 -> '86.3', 86.0 -> '86'."""
    if not isinstance(score, (int, float)):
        return "—"
    return f"{score:.1f}" if score % 1 else str(int(score))


def _suggested_changes(mode: str, workout: dict, readiness: dict) -> str:
    """Descrizione dei cambiamenti consigliati (nessuna chiamata AI)."""
    name = (workout or {}).get("nome", "la seduta")
    score = _fmt_score(readiness.get("readiness_score"))
    level = readiness.get("readiness_level") or "—"
    if mode == "lighten":
        return (
            f"Readiness {level} ({score}/100) ma '{name}' è impegnativa: il Coach consiglia di ALLEGGERIRLA — "
            "meno volume e meno intensità (es. metà delle ripetute a ritmo controllato, niente Z5, "
            "defaticamento più lungo), per assecondare il recupero senza annullare la seduta."
        )
    return (
        f"Readiness {level} ({score}/100) e '{name}' è leggera: ottime condizioni, il Coach consiglia di "
        "POTENZIARLA — qualche km in più o un ultimo blocco più veloce, mantenendo la struttura della seduta."
    )


@app.get("/api/workouts/today")
async def get_today_workout_status(request: Request):
    """Stato di oggi per la sezione Allenamento:
    - garmin_calendar: workout già presente sul calendario Garmin per oggi (CARD TOP)
    - planned: piano previsto per oggi (tabella prima, altrimenti ultimo piano generato)
    - readiness + advice: consiglio del Coach Adattivo (CARD MIDDLE)
    """
    email = getattr(request.state, "user_email", None)
    uid = getattr(request.state, "user_id", None)
    today = date.today().isoformat()

    # --- 1) Calendario Garmin reale (CARD TOP) ---
    garmin = {"available": False, "has_workout": False, "workout": None}
    today_items = []
    try:
        items = await asyncio.to_thread(_workout_sync_for(email, uid).scheduled_workouts)
        garmin["available"] = True
        today_items = [
            i for i in items
            if i.get("date") == today and i.get("itemType") == "workout"
        ]
        if today_items:
            it = today_items[0]
            garmin["has_workout"] = True
            garmin["workout"] = {
                "name": it.get("title"),
                "schedule_id": it.get("id"),
                "workout_id": it.get("workoutId"),
                "duration_s": it.get("duration"),
                "distance_m": it.get("distance"),
                "sport": it.get("sportTypeKey"),
            }
    except Exception as err:  # noqa: BLE001
        logger.warning(f"today: query calendario Garmin fallita ({err})")

    # --- 2) Piano previsto per oggi (CARD MIDDLE) ---
    planned = _load_today_planned(email)

    # --- 2b) RE-VERIFICA REALE CALENDARIO (zero costi: items già interrogati
    # sopra per la CARD TOP): se il piano risulta sincronizzato ma il workout di
    # oggi NON è più sul calendario Garmin (eliminato dall'utente) -> synced=False
    planned = _recheck_calendar_sync(email, uid, planned, today_items)

    # --- 3) Readiness + consiglio adattivo ---
    summary = {}
    try:
        summary = _load_summary(email)
    except Exception as err:  # noqa: BLE001
        logger.warning(f"today: summary non disponibile ({err})")
    readiness = summary.get("readiness", {})

    # AUTO: Corsa di Scioglimento se fermo >= 4 giorni o picco HRV (anche con
    # Readiness GREEN), a meno che l'atleta non abbia già fatto override.
    if (_scioglimento_needed(summary) and not planned.get("override")
            and planned.get("workout")):
        planned = _apply_scioglimento(planned, email, summary)

    advice = _compute_advice(readiness, planned.get("workout"))
    if planned.get("adattamento") == "scioglimento":
        advice = {
            "level": "confirm",
            "icon": "💡",
            "message": SCIOLGIMENTO_NOTE,
        }
    # Pulizia vecchi avvisi override: mostrato solo se oggi la Readiness è ROSSA
    planned["override"] = _override_visible(planned.get("override"), readiness)

    return {
        "date": today,
        "garmin_calendar": garmin,
        "planned": planned,
        "readiness": readiness,
        "advice": advice,
        # RESOCONTO & FEEDBACK POST-CORSA (v66): confronto prescritto vs eseguito
        "post_run_feedback": _post_run_feedback(summary, planned.get("workout")),
        # STATO 'ALLENAMENTO ESEGUITO' (v67): true + dati reali + flag orologio
        **_workout_completed_state(summary),
    }


# =============================================================================
# Corsa di Scioglimento (adattamento dopo fermo prolungato / picco HRV)
# =============================================================================
SCIOLGIMENTO_NOTE = (
    "💡 Adattamento Coach: Vieni da diversi giorni di riposo. Convertita la seduta "
    "in una Corsa di Scioglimento per elasticizzare i tendini e prevenire dolori "
    "muscolari prima delle ripetute"
)

# Soglie di attivazione: >= 4 giorni senza correre OPPURE HRV notturno
# (ultima notte) >= 1.25x la media degli ultimi 7 giorni.
SCIOLGIMENTO_GIORNI_FERMO = 4
SCIOLGIMENTO_HRV_PICCO_RATIO = 1.25


def _scioglimento_needed(summary: dict) -> bool:
    """True se la seduta va convertita in Corsa di Scioglimento, ANCHE con
    Readiness GREEN:
      1) fermo prolungato: la corsa più recente risale a 4+ giorni fa;
      2) picco HRV: l'ultima notte >= 1.25x la media HRV degli ultimi 7 giorni.
    Con dati insufficienti ritorna False (nessun falso positivo su buchi dati)."""
    attivita = summary.get("attivita_recenti") or []
    if attivita:
        last = str(attivita[0].get("data") or "")
        if last:
            try:
                days_off = (date.today() - date.fromisoformat(last)).days
                if days_off >= SCIOLGIMENTO_GIORNI_FERMO:
                    return True
            except ValueError:
                pass
    biometria = summary.get("biometria") or {}
    hrv_night = biometria.get("hrv_last_night")
    hrv_weekly = biometria.get("hrv_weekly")
    if hrv_night is not None and hrv_weekly is not None:
        try:
            if float(hrv_night) >= SCIOLGIMENTO_HRV_PICCO_RATIO * float(hrv_weekly):
                return True
        except (TypeError, ValueError):
            pass
    return False


def _build_scioglimento_workout() -> dict:
    """Corsa di Scioglimento deterministica (nessuna chiamata DeepSeek):
    38 minuti ESATTI (2280s): 5' warmup + 23' Z2 + 1' allunghi (4x60m) +
    4' recuperi (4x1' rec 60s) + 5' cooldown. Distanza di corsa totale
    ~6.4 km (calcolata dal frontend con workoutTotalDistanceM: fasi a tempo
    convertite dal ritmo + allunghi a metri + jog di recupero @10:00/km).
    Ritmo corsa di riferimento: 5:35/km.
    Payload Garmin: allunghi a 15s per 60m + 60s recupero (end_condition
    "time" -> interval TIME 15s deterministico, indipendente dal ritmo),
    durata totale Garmin esattamente 2280s = 38'00"."""
    warmup_s, easy_s, cooldown_s = 5 * 60, 23 * 60, 5 * 60
    strides_s, recover_s = 1 * 60, 4 * 60  # 1' allunghi (4x60m) + 4' recuperi (4x1')
    return {
        "nome": "Corsa di Scioglimento",
        "obiettivo": ("Riattivare tendini e muscolatura dopo il riposo: "
                       "corsa facile Z2 a ritmo sciolto (5:30-5:45/km) + 4 allunghi leggeri"),
        "tipo": "RECUPERO",
        "readiness_richiesta": "GREEN",
        "ritmo_riferimento": "5:35/km",
        "durata_totale_s": warmup_s + easy_s + strides_s + recover_s + cooldown_s,  # 2280 = 38'
        "fasi": [
            {"tipo": "warmup", "durata_s": warmup_s, "distanza_m": 833, "zona": "Z1", "ritmo": "6:00/km",
             "hr_target": [114, 124], "note": "Riscaldamento molto leggero"},
            {"tipo": "easy_run", "durata_s": easy_s, "distanza_m": 4119, "zona": "Z2", "ritmo": "5:35/km",
             "hr_target": [124, 141], "note": "Corsa di scioglimento a ritmo comodo (5:30-5:45/km)"},
            {"tipo": "repeat", "durata_s": strides_s, "distanza_m": 60, "ripetizioni": 4,
             "zona": "Z2/Z3", "ritmo": "4:50/km", "ritmo_recupero": "10:00/km", "recupero_s": 60,
             "end_condition": "time",  # allunghi: 15s per 60m, durata deterministica Garmin
             "note": "4 allunghi leggeri (strides) in scioltezza, non sprint"},
            {"tipo": "cooldown", "durata_s": cooldown_s, "distanza_m": 833, "zona": "Z1", "ritmo": "6:00/km",
             "hr_target": [114, 124], "note": "Defaticamento leggero"},
        ],
        "note": SCIOLGIMENTO_NOTE,
    }


def _giorni_di_fermo(summary: dict) -> Optional[int]:
    """Giorni trascorsi dalla corsa più recente (None se non computabile)."""
    attivita = summary.get("attivita_recenti") or []
    if not attivita:
        return None
    last = str(attivita[0].get("data") or "")
    if not last:
        return None
    try:
        return (date.today() - date.fromisoformat(last)).days
    except ValueError:
        return None


def _apply_scioglimento(planned: dict, email: str, summary: dict) -> dict:
    """Converte la seduta di oggi in Corsa di Scioglimento e la PERSISTE nella
    fonte corretta (tabella settimanale oppure workout_plan.json), così il
    sync Garmin carica la seduta convertita. Ritorna il dict `planned`
    aggiornato per la risposta API."""
    workout = _build_scioglimento_workout()
    if planned.get("source") == "table":
        table = _load_table(email)
        for e in table.get("entries", []):
            if e.get("date") == date.today().isoformat() and e.get("kind") == "workout":
                e["workout"] = workout
                e["adapted"] = True
                e["override"] = False
                e["synced"] = False
                e["garmin_workout_id"] = None
                with _lock:
                    _save_table(email, table)
                break
    else:
        plan = _load_plan(email)
        if plan.get("workout"):
            plan["workout"] = workout
            plan["adapted"] = True
            plan["override_richiesto"] = False
            plan["synced"] = False
            plan["garmin_workout_id"] = None
            plan["readiness_at_generation"] = summary.get("readiness", {})
            _write_plan(email, plan)
    planned["workout"] = workout
    planned["adapted"] = True
    planned["override"] = False
    planned["synced"] = False
    planned["garmin_workout_id"] = None
    planned["adattamento"] = "scioglimento"
    planned["note"] = SCIOLGIMENTO_NOTE
    return planned


@app.post("/api/workouts/adapt-today")
async def adapt_today_plan(request: Request):
    """Adatta l'ultimo piano generato (workout_plan.json) alla Readiness attuale:
    rigenera una versione ridotta (recupero attivo se RED, volume/intensità ridotti se AMBER).
    Se oggi c'è un workout in tabella usa invece /api/table/adapt."""
    from ai_coach import AICoach

    email = getattr(request.state, "user_email", None)
    plan = _load_plan(email)
    if not plan or not plan.get("workout"):
        raise HTTPException(status_code=400, detail="Nessun piano da adattare: genera prima un allenamento")

    summary = _merge_goal(_load_summary(email), email)
    readiness = summary.get("readiness", {})
    level = readiness.get("readiness_level")
    score = readiness.get("readiness_score")

    coach = AICoach(summary=summary)
    original_name = (plan.get("workout") or {}).get("nome", "l'allenamento previsto")

    # Corsa di Scioglimento: fermo 4+ giorni o picco HRV (anche con Readiness GREEN)
    if _scioglimento_needed(summary):
        workout = _build_scioglimento_workout()
        valid, issues = coach.validate_workout(workout, override=False, readiness_check=False)
        plan["workout"] = workout
        plan["adapted"] = True
        plan["override_richiesto"] = False
        plan["readiness_at_generation"] = readiness
        # La seduta è cambiata: va ricaricata su Garmin (il vecchio sync non vale più)
        plan["synced"] = False
        plan["garmin_workout_id"] = None
        if (email or "").lower() == (_default_email() or "").lower():
            coach.save_output(plan)  # mirror legacy solo account principale
        _write_plan(email, plan)
        return {
            "status": "ok",
            "mode": "adapt",
            "adattamento": "scioglimento",
            "workout": _decorate_workout(workout, readiness),
            "valid": valid,
            "issues": issues,
            "message": SCIOLGIMENTO_NOTE,
        }

    request = (
        f"Adatta l'allenamento di oggi ('{original_name}') alla Readiness attuale "
        f"({level}, {score}/100). Se RED: genera un recupero attivo facile in Z1/Z2 (30-40 min). "
        "Se AMBER: riduci volume e intensità rispetto al piano originale (es. easy run Z3 più corta, "
        "metà delle ripetute a ritmo più controllato, senza Z5). "
        "Se GREEN: conferma l'allenamento previsto mantenendone struttura e ritmi "
        "(eventuale piccolo potenziamento solo nel blocco finale). Mantieni l'obiettivo di gara."
    )
    try:
        result = await asyncio.to_thread(coach.generate, request, override=False)
    except Exception as err:  # noqa: BLE001
        logger.exception("Errore adattamento allenamento")
        raise HTTPException(status_code=502, detail=f"Adattamento fallito: {err}")

    workout = result["workout"]
    valid, issues = coach.validate_workout(workout, override=False)
    plan["workout"] = workout
    plan["adapted"] = True
    plan["override_richiesto"] = False
    plan["readiness_at_generation"] = readiness
    # La seduta è cambiata: va ricaricata su Garmin (il vecchio sync non vale più)
    plan["synced"] = False
    plan["garmin_workout_id"] = None
    if (email or "").lower() == (_default_email() or "").lower():
        coach.save_output(plan)  # mirror legacy solo account principale
    _write_plan(email, plan)
    return {
        "status": "ok",
        "mode": "adapt",
        "workout": _decorate_workout(workout),
        "valid": valid,
        "issues": issues,
        "message": f"🔄 Seduta adattata alla Readiness {level} ({score}/100).",
    }


@app.post("/api/workouts/adapt-check")
async def adapt_check_workout(request: Request):
    """Controllo 'Adatta Seduta' (puro calcolo, NESSUNA chiamata AI):
    - se l'allenamento di oggi è già allineato alla Readiness live -> needed:false
    - se servono modifiche (alleggerire/potenziare) -> needed:true con i cambiamenti
      consigliati; l'utente decide poi se applicarli (endpoint esistenti di adapt)."""
    email = getattr(request.state, "user_email", None)
    planned = _load_today_planned(email)
    workout = planned.get("workout")
    summary = {}
    try:
        summary = _load_summary(email)
    except Exception as err:  # noqa: BLE001
        logger.warning(f"adapt-check: summary non disponibile ({err})")
    readiness = summary.get("readiness", {})
    score = readiness.get("readiness_score")
    level = readiness.get("readiness_level")

    # ---- INJURY LAB 2.0: peso biomeccanico degli infortuni attivi -----------
    from analytics_engine import active_injuries_impact, injury_impact_pct
    attivi = [i for i in _load_injuries(email) if not i.get("resolved")]
    injury_impact = active_injuries_impact(attivi)
    injuries_view = [{
        "id": i.get("id"), "part": i.get("part"), "label": i.get("label"),
        "severity": i.get("severity"), "impatto_pct": injury_impact_pct(i),
        "raccomandazione": i.get("report", {}).get("raccomandazione"),
    } for i in attivi]

    if not workout:
        if injury_impact.get("level") == "stop":
            return {
                "needed": False, "level": "stop",
                "message": (
                    f"🚫 {injury_impact['label']}: {injury_impact['raccomandazione']}. "
                    "Nessuna corsa oggi: riposo attivo o cross-training (nuoto/bici 30-40')."
                ),
                "readiness": readiness, "source": planned.get("source"),
                "injuries": injuries_view, "injury_impact": injury_impact,
            }
        return {
            "needed": False,
            "level": "none",
            "message": "📋 Nessun allenamento previsto per oggi: usa il Generatore su Richiesta.",
            "readiness": readiness,
            "source": planned.get("source"),
            "injuries": injuries_view, "injury_impact": injury_impact,
        }

    name = (workout or {}).get("nome", "la seduta")

    # L'infortunio ad alto impatto biomeccanico ha priorità sulla Readiness
    if injury_impact.get("level") == "stop":
        return {
            "needed": True, "mode": "lighten", "level": "stop", "icon": "🚫",
            "message": (
                f"🚫 {injury_impact['label']}: {injury_impact['raccomandazione']} "
                f"Oggi meglio rinunciare alla corsa: riposo attivo o cross-training (nuoto/bici 30-40')."
            ),
            "suggested": [
                "Riposo attivo: camminata 30' a passo leggero",
                "Cross-training: nuoto o bici 30-40' in Z1/Z2",
                "Mobilità e stretching mirati alla zona interessata",
            ],
            "workout_name": name, "readiness": readiness, "source": planned.get("source"),
            "injuries": injuries_view, "injury_impact": injury_impact,
        }
    if injury_impact.get("level") == "reduce":
        return {
            "needed": True, "mode": "lighten", "level": "reduce", "icon": "⚠️",
            "message": (
                f"⚠️ {injury_impact['label']}: {injury_impact['raccomandazione']} "
                "Consiglio: solo corsa facile breve (30-40') o cross-training oggi."
            ),
            "suggested": [
                "Corsa facile Z1/Z2 max 30-40' su terreno morbido",
                "In alternativa cross-training (nuoto/bici) 30-40'",
                "Esercizi di mobilità e rinforzo della zona",
            ],
            "workout_name": name, "readiness": readiness, "source": planned.get("source"),
            "injuries": injuries_view, "injury_impact": injury_impact,
        }

    # Corsa di Scioglimento: fermo >= 4 giorni o picco HRV (anche con Readiness
    # GREEN). Priorità agli infortuni (stop/reduce) già gestiti sopra.
    if _scioglimento_needed(summary) and not planned.get("override"):
        giorni = _giorni_di_fermo(summary)
        if giorni is not None and giorni >= SCIOLGIMENTO_GIORNI_FERMO:
            msg = (f"⚠️ Consigliato Adattamento Coach: Vieni da {giorni} giorni di riposo. "
                   "Clicca su 'Adatta Seduta' per convertire le ripetute in 35 min di "
                   "Corsa Facile Z2 per sciogliere i muscoli prima di fare carico")
        else:
            msg = ("⚠️ Consigliato Adattamento Coach: il tuo HRV notturno è in forte rimbalzo "
                   "dopo il riposo. Clicca su 'Adatta Seduta' per convertire le ripetute in 35 min "
                   "di Corsa Facile Z2 per sciogliere i muscoli prima di fare carico")
        return {
            "needed": True,
            "mode": "lighten",
            "level": "scioglimento",
            "icon": "💡",
            "message": msg,
            "suggested": [
                "Corsa di Scioglimento: 35 min in Z2 a ritmo comodo (5:30-5:45/km)",
                "4 allunghi leggeri (strides) in scioltezza in chiusura",
                "Stretching e mobilità post-corsa per elasticizzare tendini e muscolatura",
            ],
            "workout_name": name,
            "readiness": readiness,
            "source": planned.get("source"),
            "injuries": injuries_view, "injury_impact": injury_impact,
            "adattamento": "scioglimento",
        }

    advice = _compute_advice(readiness, workout)

    if advice["level"] == "confirm":
        extra = ""
        if injury_impact.get("level") in ("modify", "mild"):
            extra = (
                f" Nota infortunio: {injury_impact['label']} "
                f"({injury_impact['raccomandazione'].split('·')[-1].strip() or 'impatto moderato'}). "
                "Riduci il volume del 20% e fermati al primo dolore."
            )
        return {
            "needed": False,
            "level": "confirm",
            "message": (
                f"✅ L'allenamento in programma è già perfettamente allineato alla tua "
                f"Readiness di oggi ({_fmt_score(score)} {level or '—'}). Nessuna modifica necessaria."
                + extra
            ),
            "workout_name": name,
            "readiness": readiness,
            "source": planned.get("source"),
            "injuries": injuries_view, "injury_impact": injury_impact,
        }

    # lighten | boost -> cambiamenti consigliati (opzionali)
    mode = advice["level"]
    return {
        "needed": True,
        "mode": mode,
        "level": advice["level"],
        "icon": advice["icon"],
        "message": advice["message"],
        "suggested": _suggested_changes(mode, workout, readiness),
        "workout_name": name,
        "readiness": readiness,
        "source": planned.get("source"),
        "injuries": injuries_view, "injury_impact": injury_impact,
    }


@app.post("/api/workouts/wipe")
async def wipe_workout(request: Request):
    """🗑️ ELIMINAZIONE TOTALE (v88): reset completo del calendario Garmin
    Connect (TUTTI i workout programmati via delete_workout(id)) + eliminazione
    della tabella Microciclo 7 giorni dal DB SQL (user_workouts kind='table' e
    kind='plan') + svuotamento di table_plan.json e del piano attivo.
    Attiva la MODALITÀ ADATTIVA GIORNO PER GIORNO 100% dinamica live: nessun
    piano fisso, ogni scheda verrà rigenerata dalle 15 metriche live."""
    email = getattr(request.state, "user_email", None)
    uid = _user_id_for(email)
    deleted_ids = []
    deleted_names = []

    # 1) RESET COMPLETO CALENDARIO GARMIN CONNECT: cancella TUTTI i workout
    #    programmati (per workoutId salvato nel piano/tabella + re-scan reale
    #    dei calendarItems schedulati). Fail-safe: mai crash.
    try:
        sync = _workout_sync_for(email, uid)
        plan = _load_plan(email)
        candidate_ids = set()
        wid = plan.get("garmin_workout_id")
        if wid:
            candidate_ids.add(str(wid))
        # id salvati nelle entry della tabella (tutti i giorni, non solo oggi)
        try:
            table = _load_table(email)
            for e in (table.get("entries") or []):
                if e.get("synced") and e.get("garmin_workout_id"):
                    candidate_ids.add(str(e["garmin_workout_id"]))
        except Exception as err:  # noqa: BLE001
            logger.warning(f"wipe: lettura tabella per id Garmin fallita ({err})")
        # re-scan TOTALE del calendario reale: ogni item schedulato viene
        # cancellato (reset completo, indipendentemente dal nome)
        try:
            scheduled = await asyncio.to_thread(sync.scheduled_workouts)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"wipe: elenco calendario non disponibile ({err})")
            scheduled = []
        for it in (scheduled or []):
            cid = str(it.get("workoutId") or "")
            if cid:
                candidate_ids.add(cid)
                nm = str(it.get("workoutName") or "").strip()
                if nm:
                    deleted_names.append(nm)
        for cid in sorted(candidate_ids):
            try:
                await asyncio.to_thread(sync.delete_workout, cid)
                deleted_ids.append(cid)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"wipe: delete Garmin {cid} fallito ({err})")
    except Exception as err:  # noqa: BLE001
        logger.warning(f"wipe: fase Garmin fallita ({err})")

    # 2) ELIMINAZIONE TOTALE DAL DB SQL: traccia tabella Microciclo (kind='table')
    #    rimossa da user_workouts + piano attivo azzerato (kind='plan')
    try:
        db.delete_workout_doc(uid, "table")
    except Exception as err:  # noqa: BLE001
        logger.warning(f"wipe: eliminazione tabella SQL fallita ({err})")
    try:
        _write_plan(email, {})
    except Exception as err:  # noqa: BLE001
        logger.warning(f"wipe: azzeramento piano fallito ({err})")

    # 2b) File table_plan.json legacy svuotato (per-utente + mirror principale)
    try:
        empty_table = copy.deepcopy(DEFAULT_TABLE)
        _write_user_json(email, "table_plan.json", empty_table)
    except Exception as err:  # noqa: BLE001
        logger.warning(f"wipe: svuotamento table_plan.json fallito ({err})")

    return {
        "status": "ok",
        "message": "🗑️ Reset completo: calendario Garmin svuotato e tabella "
                    "Microciclo eliminata. ⚡️ Modalità Adattiva Giorno per "
                    "Giorno attiva: ogni scheda verrà generata in tempo reale "
                    "dalle 15 metriche live (VO2max, FC, HRV, Sonno, Body "
                    "Battery, Stress, Carichi, ACWR, Readiness, Infortuni).",
        "deleted_workout_ids": deleted_ids,
        "deleted_names": list(dict.fromkeys(deleted_names)),
        "table_eliminata": True,
        "modalita_adattiva": True,
    }


@app.post("/api/workouts/table/clear")
async def clear_workout_table(request: Request):
    """🗑️ ELIMINA SOLO LA TABELLA MICROCICLO (v90): rimuove la tabella
    settimanale dal DB SQL (user_workouts kind='table') e svuota il file
    legacy table_plan.json. NON tocca l'allenamento consigliato del giorno
    (piano kind='plan') né il calendario Garmin Connect: il workout di oggi
    resta attivo, sincronizzabile e NON viene eliminato."""
    email = getattr(request.state, "user_email", None)
    uid = _user_id_for(email)
    try:
        db.delete_workout_doc(uid, "table")
    except Exception as err:  # noqa: BLE001
        logger.warning(f"table/clear: eliminazione tabella SQL fallita ({err})")
    try:
        _write_user_json(email, "table_plan.json", copy.deepcopy(DEFAULT_TABLE))
    except Exception as err:  # noqa: BLE001
        logger.warning(f"table/clear: svuotamento table_plan.json fallito ({err})")
    return {
        "status": "ok",
        "message": "🗑️ Tabella Microciclo eliminata. L'allenamento di oggi resta "
                    "attivo sul calendario Garmin e rimane sincronizzabile.",
        "table_eliminata": True,
        "piano_preservato": True,
    }


@app.post("/api/workouts/unschedule")
async def unschedule_workout(request: Request):
    """🗓️ ELIMINA DAL CALENDARIO (v89): rimuove SOLO l'allenamento di OGGI dal
    calendario Garmin Connect (workoutId salvato + re-scan dei calendarItems di
    oggi), azzera i flag di sync (synced/synced_at/garmin_workout_id) nel piano
    e nella entry odierna della tabella: il pulsante Sync torna ABILITATO e può
    RICARICARE l'allenamento consigliato. NON tocca la tabella Microciclo né
    gli altri giorni (eliminazione totale = wipe_workout)."""
    email = getattr(request.state, "user_email", None)
    uid = _user_id_for(email)
    today_iso = date.today().isoformat()
    deleted_ids = []
    deleted_names = []

    # 1) Rimuovi dal calendario Garmin Connect i workout di OGGI (fail-safe)
    try:
        sync = _workout_sync_for(email, uid)
        plan = _load_plan(email)
        candidate_ids = set()
        wid = (plan or {}).get("garmin_workout_id")
        if wid:
            candidate_ids.add(str(wid))
        # entry odierna della tabella (se sincronizzata con un id proprio)
        try:
            table = _load_table(email)
            for e in (table.get("entries") or []):
                if e.get("date") == today_iso and e.get("synced") and e.get("garmin_workout_id"):
                    candidate_ids.add(str(e["garmin_workout_id"]))
        except Exception as err:  # noqa: BLE001
            logger.warning(f"unschedule: lettura tabella fallita ({err})")
        # re-scan reale del calendario: SOLO gli item programmati per OGGI
        try:
            scheduled = await asyncio.to_thread(sync.scheduled_workouts)
            for it in (scheduled or []):
                if it.get("itemType") == "workout" and it.get("date") == today_iso:
                    cid = str(it.get("workoutId") or "")
                    if cid:
                        candidate_ids.add(cid)
                        nm = str(it.get("workoutName") or "").strip()
                        if nm:
                            deleted_names.append(nm)
        except Exception as err:  # noqa: BLE001
            logger.warning(f"unschedule: elenco calendario non disponibile ({err})")
        for cid in sorted(c for c in candidate_ids if c):
            try:
                await asyncio.to_thread(sync.delete_workout, cid)
                deleted_ids.append(cid)
            except Exception as err:  # noqa: BLE001
                logger.warning(f"unschedule: delete Garmin {cid} fallito ({err})")
    except Exception as err:  # noqa: BLE001
        logger.warning(f"unschedule: fase Garmin fallita ({err})")

    # 2) SBLOCCO SYNC: azzera synced/synced_at/garmin_workout_id nel piano
    #    (SQL + mirror legacy) e nella entry odierna della tabella.
    try:
        plan = _load_plan(email) or {}
        if plan:
            plan["synced"] = False
            plan.pop("synced_at", None)
            plan.pop("garmin_workout_id", None)
            _write_plan(email, plan)
    except Exception as err:  # noqa: BLE001
        logger.warning(f"unschedule: sblocco piano fallito ({err})")
    try:
        table = _load_table(email)
        for e in (table.get("entries") or []):
            if e.get("date") == today_iso:
                e["synced"] = False
                e.pop("garmin_workout_id", None)
        _save_table(email, table)
    except Exception as err:  # noqa: BLE001
        logger.warning(f"unschedule: sblocco tabella fallito ({err})")

    return {
        "status": "ok",
        "message": "🗓️ Allenamento di oggi eliminato dal calendario Garmin. "
                    "Il Sync è ora sbloccato: puoi ricaricare la scheda "
                    "consigliata premendo Sync.",
        "deleted_workout_ids": deleted_ids,
        "deleted_names": list(dict.fromkeys(deleted_names)),
        "sync_sbloccato": True,
        "date": today_iso,
    }


# =============================================================================
# Infortuni / Fastidi muscolari (INJURY LAB 2.0)
# =============================================================================
def _decorate_injury(inj: dict) -> dict:
    """Arricchisce un infortunio con il Referto del Coach (diagnosi, cause,
    fisioterapia, mobilità) e l'impatto biomeccanico sulla corsa. Se il referto
    è già salvato lo riusa, altrimenti lo genera al volo (infortuni vecchi)."""
    if not isinstance(inj, dict):
        return inj
    if not isinstance(inj.get("report"), dict):
        from analytics_engine import generate_injury_report
        inj["report"] = generate_injury_report(
            inj.get("part", ""), inj.get("severity", 1), inj.get("label"))
    return inj


@app.get("/api/injuries")
def list_injuries(request: Request):
    email = getattr(request.state, "user_email", None)
    with _lock:
        return [_decorate_injury(i) for i in _load_injuries(email)]


@app.post("/api/injuries")
def add_injury(payload: InjuryCreate, request: Request):
    from analytics_engine import generate_injury_report
    email = getattr(request.state, "user_email", None)
    with _lock:
        injuries = _load_injuries(email)
        injury = {
            "id": str(uuid.uuid4())[:8],
            "part": payload.part,
            "label": _sanitize(payload.label),
            "severity": payload.severity,
            "note": _sanitize(payload.note),
            "created_at": date.today().isoformat(),
            "resolved": False,
            "x": payload.x,
            "y": payload.y,
            "report": generate_injury_report(payload.part, payload.severity, payload.label),
        }
        injuries.append(injury)
        _save_injuries(email, injuries)
        return injury


@app.patch("/api/injuries/{injury_id}")
def update_injury(injury_id: str, payload: InjuryUpdate, request: Request):
    from analytics_engine import generate_injury_report
    email = getattr(request.state, "user_email", None)
    with _lock:
        injuries = _load_injuries(email)
        for inj in injuries:
            if inj.get("id") == injury_id:
                if payload.severity is not None:
                    inj["severity"] = payload.severity
                if payload.note is not None:
                    inj["note"] = _sanitize(payload.note)
                if payload.resolved is not None:
                    inj["resolved"] = payload.resolved
                if payload.x is not None:
                    inj["x"] = payload.x
                if payload.y is not None:
                    inj["y"] = payload.y
                # Il referto dipende dalla severità: rigeneralo se è cambiata
                inj["report"] = generate_injury_report(
                    inj.get("part", ""), inj.get("severity", 1), inj.get("label"))
                _save_injuries(email, injuries)
                return inj
        raise HTTPException(status_code=404, detail="Fastidio non trovato")


@app.delete("/api/injuries/{injury_id}")
def delete_injury(injury_id: str, request: Request):
    email = getattr(request.state, "user_email", None)
    with _lock:
        injuries = _load_injuries(email)
        remaining = [i for i in injuries if i.get("id") != injury_id]
        if len(remaining) == len(injuries):
            raise HTTPException(status_code=404, detail="Fastidio non trovato")
        _save_injuries(email, remaining)
        return {"status": "ok", "removed": injury_id}


# =============================================================================
# Obiettivo di gara (10K / 21K)
# =============================================================================
def _goals_response(email: Optional[str] = None) -> dict:
    goals = _load_goals(email)
    return {
        **goals,
        "ritmo_target": _fmt_pace(goals["target_time_s"] / (21.0975 if goals.get("race_type") == "21K" else 10.0))
        if goals.get("enabled") and goals.get("target_time_s") else None,
    }


@app.get("/api/goals")
def get_goals(request: Request):
    return _goals_response(getattr(request.state, "user_email", None))


@app.post("/api/goals")
def set_goals(payload: GoalPayload, request: Request):
    email = getattr(request.state, "user_email", None)
    with _lock:
        goals = _load_goals(email)
        goals["race_type"] = payload.race_type
        goals["target_time_s"] = payload.target_time_s
        goals["race_date"] = payload.race_date
        goals["enabled"] = payload.enabled
        goals["updated_at"] = date.today().isoformat()
        _save_goals(email, goals)
    return _goals_response(email)


# =============================================================================
# Tabella multisettimanale adattiva (generatore + sync + controllo giornaliero)
# =============================================================================
def _is_hard_workout(workout) -> bool:
    """True se il workout contiene fasi dure (ripetute/soglia/tempo/progressione)."""
    if not isinstance(workout, dict):
        return False
    fasi = workout.get("fasi") or []
    return any(isinstance(f, dict) and f.get("tipo") in TABLE_HARD_PHASES for f in fasi)


def _compute_advice(readiness: dict, workout) -> dict:
    """Consiglio del Coach Adattivo: confronta l'allenamento del giorno con la
    Readiness live. Ritorna level confirm|lighten|boost + messaggio in italiano."""
    score = readiness.get("readiness_score")
    level = readiness.get("readiness_level")
    if not isinstance(score, (int, float)):
        score = None
    hard = _is_hard_workout(workout) if workout else False
    name = (workout or {}).get("nome", "la seduta") if isinstance(workout, dict) else "la seduta"

    if workout is None:
        return {
            "level": "none", "icon": "📋",
            "message": "Nessun allenamento pianificato per oggi. Generane uno nella card qui sotto o chiedi al Coach.",
        }
    if score is None:
        return {"level": "none", "icon": "📊", "message": "Dati insufficienti per valutare la Readiness."}
    if level == "RED" or score < 34:
        if hard:
            return {
                "level": "lighten", "icon": "🟥",
                "message": f"Readiness ROSSA ({score:.0f}/100) e '{name}' è una seduta impegnativa: consiglio di alleggerirla "
                            "(recupero attivo Z1/Z2) per non compromettere il recupero.",
            }
        return {
            "level": "lighten", "icon": "🟥",
            "message": f"Readiness ROSSA ({score:.0f}/100): meglio una seduta molto leggera o riposo attivo.",
        }
    if level == "AMBER" or score < 67:
        if hard:
            return {
                "level": "lighten", "icon": "🟨",
                "message": f"Readiness AMBER ({score:.0f}/100) e '{name}' è impegnativa: riduci volume e intensità "
                            "(es. metà ripetute, niente Z5).",
            }
        return {
            "level": "confirm", "icon": "🟨",
            "message": f"Readiness AMBER ({score:.0f}/100): seduta moderata ok, ma resta prudente sui ritmi.",
        }
    if readiness.get("estimated"):
        return {"level": "confirm", "icon": "🟨", "message": f"Readiness provvisoria ({score:.0f}/100)."}
    # GREEN
    if not hard:
        return {
            "level": "boost", "icon": "🟢",
            "message": f"Readiness VERDE ({score:.0f}/100) e seduta leggera: ottime condizioni, puoi potenziarla "
                        "(qualche km in più o ultimo blocco più veloce).",
        }
    return {
        "level": "confirm", "icon": "🟢",
        "message": f"Readiness VERDE ({score:.0f}/100): seduta confermata, ottime condizioni per correre.",
    }


def _today_table_status(table: dict, email: Optional[str] = None) -> dict:
    """Stato di oggi rispetto alla tabella: se oggi c'è un lavoro duro e la
    Readiness è RED/AMBER (senza override già applicato) -> needs_adaptation=True."""
    today = date.today().isoformat()
    entry = next((e for e in table.get("entries", []) if e.get("date") == today), None)
    if not entry or entry.get("kind") != "workout":
        return {"date": today, "kind": "rest", "needs_adaptation": False}
    readiness = {}
    try:
        readiness = _load_summary(email).get("readiness", {})
    except Exception as err:  # noqa: BLE001
        logger.warning(f"today_table_status: readiness non disponibile ({err})")
    level = readiness.get("readiness_level")
    score = readiness.get("readiness_score")
    hard = _is_hard_workout(entry.get("workout") or {})
    needs = bool(hard and level in ("RED", "AMBER") and not entry.get("override"))
    return {
        "date": today,
        "kind": "workout",
        "is_hard": hard,
        "workout_name": (entry.get("workout") or {}).get("nome"),
        "readiness_level": level,
        "readiness_score": score,
        "needs_adaptation": needs,
        "adapted": entry.get("adapted", False),
        "override": entry.get("override", False),
        "synced": entry.get("synced", False),
    }


@app.get("/api/table")
def get_table(request: Request):
    """Tabella corrente + stato di oggi (per il controllo adattivo giornaliero)."""
    email = getattr(request.state, "user_email", None)
    table = _load_table(email)
    today_status = _today_table_status(table, email)
    # Pulizia vecchi avvisi override: esposto solo se la Readiness attuale è ROSSA
    # (today_status viene calcolato PRIMA, sulle entry originali con flag grezzo)
    readiness = {}
    try:
        readiness = _load_summary(email).get("readiness", {})
    except Exception as err:  # noqa: BLE001
        logger.warning(f"table: readiness non disponibile ({err})")
    for e in table.get("entries", []):
        e["override"] = _override_visible(e.get("override"), readiness)
    return {**table, "readiness": readiness, "today": today_status}


@app.post("/api/table/generate")
async def generate_table(payload: TableGenerateRequest, request: Request):
    """Genera il microciclo con DeepSeek: un workout per ogni giorno scelto,
    riposo nei giorni liberi. Raggruppa per settimana e chiama generate_week
    (una chiamata per settimana: risparmio token + progressione del carico)."""
    from ai_coach import AICoach

    email = getattr(request.state, "user_email", None)
    days = sorted(set(payload.days))
    if any(d < 0 or d > 6 for d in days):
        raise HTTPException(status_code=400, detail="Giorni non validi (attesi 0-6, Lun=0...Dom=6)")

    summary = _merge_goal(_load_summary(email), email)
    coach = AICoach(summary=summary)

    # --- periodo --------------------------------------------------------------
    start = date.today()
    if payload.duration == "race":
        race = (_load_goals(email) or {}).get("race_date")
        if not race:
            raise HTTPException(
                status_code=400,
                detail="Imposta la data della gara nella sezione Obiettivo per usare 'Fino alla Gara'")
        try:
            end = date.fromisoformat(race)
        except ValueError:
            raise HTTPException(status_code=400, detail="Data della gara non valida")
        if end <= start:
            raise HTTPException(status_code=400, detail="La data della gara deve essere futura")
    else:
        end = start + timedelta(days=TABLE_DURATION_DAYS[payload.duration] - 1)

    all_dates: List[date] = []
    d = start
    while d <= end:
        all_dates.append(d)
        d += timedelta(days=1)
    training_dates = [d for d in all_dates if d.weekday() in days]
    if not training_dates:
        raise HTTPException(status_code=400, detail="Nessun giorno di allenamento nel periodo selezionato")

    # --- raggruppa i giorni di allenamento per settimana ISO --------------------
    weeks: dict = {}
    for td in training_dates:
        key = td.isocalendar()[:2]  # (year, week)
        weeks.setdefault(key, []).append(td)
    week_keys = sorted(weeks.keys())

    goals = _load_goals(email)
    goal_note = ""
    if goals.get("enabled") and goals.get("race_type"):
        goal_note = f"Obiettivo di gara: {goals['race_type']} in {_fmt_clock(goals['target_time_s'])}"
        if goals.get("race_date"):
            goal_note += f" (gara il {goals['race_date']})"
    if payload.notes:
        goal_note = (goal_note + " | " if goal_note else "") + _sanitize(payload.notes)

    # --- genera settimana per settimana ----------------------------------------
    workouts: list = []
    issues: list = []
    for wi, key in enumerate(week_keys, start=1):
        week_dates = [{"date": td.isoformat(), "weekday": WEEKDAY_IT[td.weekday()]}
                      for td in weeks[key]]
        try:
            week_workouts = await asyncio.to_thread(
                coach.generate_week, week_dates, wi, len(week_keys), goal_note)
        except Exception as err:  # noqa: BLE001
            logger.exception(f"Errore generazione settimana {wi}")
            raise HTTPException(
                status_code=502,
                detail=f"Generazione tabella fallita (settimana {wi} di {len(week_keys)}): {err}")
        for w in week_workouts:
            valid, w_issues = coach.validate_workout(w, readiness_check=False)
            if not valid:
                issues.extend(w_issues)
        workouts.extend(week_workouts)

    if len(workouts) != len(training_dates):
        raise HTTPException(status_code=502, detail="Workout generati incoerenti con i giorni selezionati")

    # --- costruisci le entries (workout sui giorni scelti, riposo sugli altri) --
    entries = []
    wi = 0
    for d in all_dates:
        if d.weekday() in days:
            entries.append({
                "date": d.isoformat(),
                "weekday": WEEKDAY_IT[d.weekday()],
                "kind": "workout",
                "workout": workouts[wi],
                "garmin_workout_id": None,
                "synced": False,
                "adapted": False,
                "override": False,
            })
            wi += 1
        else:
            entries.append({
                "date": d.isoformat(),
                "weekday": WEEKDAY_IT[d.weekday()],
                "kind": "rest",
                "workout": None,
                "garmin_workout_id": None,
                "synced": False,
                "adapted": False,
                "override": False,
            })

    table = {
        "generated_at": date.today().isoformat(),
        "duration": payload.duration,
        "duration_label": TABLE_DURATION_LABEL[payload.duration],
        "days": days,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "entries": entries,
    }
    with _lock:
        _save_table(email, table)
    # Durata reale (fasi + recuperi) identica al totale Garmin, in risposta
    for e in entries:
        if e.get("workout"):
            _decorate_workout(e["workout"])
    return {
        "status": "ok",
        "message": f"Tabella generata: {len(entries)} giorni ({len(training_dates)} allenamenti, {len(entries) - len(training_dates)} riposi)",
        "validation_issues": issues,
        "table": table,
        "today": _today_table_status(table, email),
    }


@app.post("/api/table/sync")
async def sync_table(payload: TableSyncRequest, request: Request):
    """ONE-TAP: carica i workout della tabella sul calendario Garmin.
    Con 'date' sincronizza solo quel giorno; senza, tutti i non ancora sincronizzati.
    Idempotente: se il giorno richiesto è già sincronizzato, risponde subito 200
    senza un secondo upload a Garmin Connect.
    Per-utente: ciascun utente sul PROPRIO calendario Garmin."""
    email = getattr(request.state, "user_email", None)
    uid = getattr(request.state, "user_id", None)
    with _sync_lock_for(uid):
        table = _load_table(email)
        if not table.get("entries"):
            raise HTTPException(status_code=400, detail="Nessuna tabella generata: usa Genera Tabella")

        if payload.date:
            entries = [e for e in table["entries"]
                       if e.get("date") == payload.date and e.get("kind") == "workout"]
            # Idempotenza: quella data è già sul calendario -> nessun secondo upload
            if entries and all(e.get("synced") for e in entries):
                e0 = entries[0]
                return {
                    "status": "ok",
                    "already": True,
                    "message": "Allenamento già caricato sul calendario Garmin",
                    "date": payload.date,
                    "workout_id": e0.get("garmin_workout_id"),
                }
        else:
            entries = [e for e in table["entries"]
                       if e.get("kind") == "workout" and not e.get("synced")]
        if not entries:
            raise HTTPException(status_code=400, detail="Nessun allenamento da sincronizzare")

        sync = _workout_sync_for(email, uid)
        # CHECK PREVENTIVO CALENDARIO REALE (una sola interrogazione): se per
        # una data esiste GIÀ un allenamento sul calendario Garmin (a prescindere
        # dall'id locale), la entry viene segnata come sincronizzata senza upload
        # -> ZERO duplicati inviati a Garmin Connect.
        existing_dates = {}
        try:
            items = await asyncio.to_thread(_workout_sync_for(email, uid).scheduled_workouts)
            for i in items or []:
                if i.get("itemType") == "workout" and i.get("date"):
                    existing_dates[i.get("date")] = i.get("workoutId")
        except Exception as err:  # noqa: BLE001
            logger.warning(f"table/sync: check preventivo calendario fallito ({err})")
        results = []
        for e in entries:
            plan = {"workout": e["workout"]}
            if e["date"] in existing_dates:
                # Già presente un allenamento per quella data -> zero duplicati
                e["synced"] = True
                e["garmin_workout_id"] = e.get("garmin_workout_id") or existing_dates[e["date"]]
                results.append({"date": e["date"], "status": "ok",
                                "workout_id": e.get("garmin_workout_id"), "already": True})
                continue
            try:
                result = await asyncio.to_thread(sync.sync_dict, plan, e["date"])
                e["synced"] = True
                e["garmin_workout_id"] = result.get("workout_id")
                results.append({"date": e["date"], "status": "ok",
                                "workout_id": result.get("workout_id")})
            except Exception as err:  # noqa: BLE001
                logger.exception(f"Sync Garmin fallito per {e['date']}")
                results.append({"date": e["date"], "status": "error", "error": str(err)})
        with _lock:
            _save_table(email, table)
    errors = [r for r in results if r["status"] == "error"]
    return {
        "status": "ok" if not errors else "partial",
        "results": results,
        "message": f"Sincronizzati {len(results) - len(errors)}/{len(results)} workout"
                   + (f" ({len(errors)} errori)" if errors else ""),
    }


@app.post("/api/table/adapt")
async def adapt_table_day(payload: TableAdaptRequest, request: Request):
    """Controllo adattivo del singolo giorno.
    mode=adapt: rigenera l'allenamento di oggi ridotto alla Readiness corrente.
    mode=override: mantiene l'allenamento originale (scelta esplicita dell'atleta)."""
    from ai_coach import AICoach

    email = getattr(request.state, "user_email", None)
    uid = getattr(request.state, "user_id", None)
    table = _load_table(email)
    target = payload.date or date.today().isoformat()
    entry = next((e for e in table.get("entries", []) if e.get("date") == target), None)
    if not entry or entry.get("kind") != "workout":
        raise HTTPException(status_code=400, detail="Nessun allenamento schedulato per questa data")

    summary = _merge_goal(_load_summary(email), email)
    readiness = summary.get("readiness", {})
    level = readiness.get("readiness_level")
    score = readiness.get("readiness_score")

    if payload.mode == "override":
        entry["override"] = True
        entry["adapted"] = False
        with _lock:
            _save_table(email, table)
        return {
            "status": "ok", "mode": "override",
            "entry": {**entry, "override": _override_visible(True, readiness)},
            "message": f"⚡️ Allenamento originale mantenuto (override). Readiness {level} ({score}/100).",
        }

    # mode == adapt: rigenera un allenamento ridotto alla Readiness attuale
    coach = AICoach(summary=summary)
    original_name = (entry.get("workout") or {}).get("nome", "l'allenamento previsto")

    # Corsa di Scioglimento: fermo 4+ giorni o picco HRV (anche con Readiness GREEN)
    if _scioglimento_needed(summary):
        workout = _build_scioglimento_workout()
        valid, issues = coach.validate_workout(workout, override=False, readiness_check=False)
        entry["workout"] = workout
        entry["adapted"] = True
        entry["override"] = False
        entry["synced"] = False
        entry["garmin_workout_id"] = None
        _decorate_workout(workout)
        with _lock:
            _save_table(email, table)
        return {
            "status": "ok", "mode": "adapt", "adattamento": "scioglimento",
            "entry": entry, "valid": valid, "issues": issues,
            "message": SCIOLGIMENTO_NOTE,
        }

    request = (
        f"Adatta l'allenamento di oggi previsto in tabella ('{original_name}') alla Readiness attuale "
        f"({level}, {score}/100). Se RED: genera un recupero attivo facile in Z1/Z2 (30-40 min). "
        "Se AMBER: riduci volume e intensità rispetto al piano originale (es. easy run Z3 più corta, "
        "metà delle ripetute a ritmo più controllato, senza Z5). "
        "Se GREEN: conferma l'allenamento previsto mantenendone struttura e ritmi "
        "(eventuale piccolo potenziamento solo nel blocco finale). Mantieni l'obiettivo di gara."
    )
    try:
        result = await asyncio.to_thread(coach.generate, request, override=False)
    except Exception as err:  # noqa: BLE001
        logger.exception("Errore adattamento allenamento")
        raise HTTPException(status_code=502, detail=f"Adattamento fallito: {err}")

    workout = result["workout"]
    valid, issues = coach.validate_workout(workout, override=False)
    entry["workout"] = workout
    entry["adapted"] = True
    entry["override"] = False
    entry["synced"] = False
    entry["garmin_workout_id"] = None
    _decorate_workout(workout)
    with _lock:
        _save_table(email, table)
    return {
        "status": "ok", "mode": "adapt", "entry": entry,
        "valid": valid, "issues": issues,
        "message": f"🔄 Allenamento di oggi adattato alla Readiness {level} ({score}/100).",
    }


# =============================================================================
# Chat AI Coach (conversazionale + generazione workout + override)
# =============================================================================
WORKOUT_INTENT_RE = re.compile(
    r"(allen\w*|workout|corr\w*|\brun\b|easy|ripetut\w*|interval|tempo|soglia|"
    r"fartlek|progression\w*|strides|1000m|800m|600m|400m|300m|200m|100m|"
    r"\bkm\b|gara|z2|z3|z4|z5|oggi|domani)",
    re.IGNORECASE,
)

CHAT_SYSTEM_PROMPT = (
    "Sei il Coach MyPT AI di Riccardo (coach personale di corsa basato sui dati reali Garmin, 23 anni, FC max 200, "
    "FC soglia 176 bpm, passo soglia 4:44/km, VO2max 54.5)."
    "\nREGOLE:"
    "\n1. Rispondi SOLO ed esclusivamente in base ai DATI REALI forniti nel messaggio utente. Non inventare numeri."
    "\n2. FC soglia (176 bpm) e passo soglia (4:44/km) sono valori UFFICIALI Garmin SEMPRE FISSI: non alterarli."
    "\n3. Rispondi in italiano, conciso e diretto (max 4-6 righe), come un coach su WhatsApp."
    "\n4. Se l'utente chiede consigli su infortuni/fastidi, dai indicazioni prudenti e suggerisci di ridurre il carico."
    "\n5. Se l'utente chiede un allenamento strutturato, usa il JSON dell'allenamento (strumento dedicato)."
)

# Storico conversazione in memoria, ISOLATO per user_id
_chat_histories: dict = {}


def _chat_history_for(user_id: int) -> list:
    return _chat_histories.setdefault(user_id, [])


def _generic_chat(config: dict, summary: dict, user_msg: str, history: List[dict]) -> str:
    """Chiamata conversazionale a DeepSeek (senza generare un workout JSON)."""
    ds = config.get("deepseek", {})
    messages = [{"role": "system", "content": CHAT_SYSTEM_PROMPT}]
    messages += history[-8:]
    compact = json.dumps(summary, ensure_ascii=False, default=str)
    messages.append({
        "role": "user",
        "content": f"DATI REALI DELL'ATLETA (unica fonte di verità):\n{compact}\n\n"
                   f"RICHIESTA: {user_msg}",
    })
    r = httpx.post(
        f"{ds['base_url']}/chat/completions",
        headers={"Authorization": f"Bearer {ds['api_key']}",
                 "Content-Type": "application/json"},
        json={"model": ds.get("model", "deepseek-v4-flash"),
              "messages": messages,
              "temperature": 0.5,
              "max_tokens": 1500},
        timeout=120,
    )
    r.raise_for_status()
    data = r.json()
    return (data["choices"][0]["message"].get("content") or "").strip() or "🤖 (nessuna risposta)"


@app.post("/api/chat")
async def chat(payload: ChatRequest, request: Request):
    """Chat conversazionale. Se la richiesta è un allenamento -> genera il piano.
    Readiness RED + lavoro duro senza insistenza -> avviso override (la PWA
    conferma e la richiesta viene ri-inviata con override=true)."""
    from ai_coach import AICoach

    config = _load_config()
    email = getattr(request.state, "user_email", None)
    uid = getattr(request.state, "user_id", None)
    summary = _merge_goal(_load_summary(email), email)
    msg = _sanitize(payload.message)

    readiness = summary.get("readiness", {})
    level = readiness.get("readiness_level")
    score = readiness.get("readiness_score")

    is_hard = AICoach.detect_hard_intent(msg)
    is_override = AICoach.detect_override(msg) or bool(payload.override)
    is_workout = bool(WORKOUT_INTENT_RE.search(msg))

    # 1) Readiness RED + lavoro duro + nessuna insistenza -> chiedi conferma
    if level == "RED" and is_hard and not is_override:
        return {
            "kind": "override_required",
            "reply": (
                f"🔴 Readiness RED ({score}/100): recupero insufficiente.\n\n"
                f"Hai chiesto un lavoro ad alta intensità. ⚠️ Attenzione fisiologica: "
                "con HRV e sonno in calo il rischio di sovrallenamento è aumentato.\n\n"
                "Se confermi, procedo comunque con l'allenamento richiesto. "
                "Altrimenti ti propongo un recupero attivo."
            ),
            "readiness_level": level,
            "readiness_score": score,
        }

    # 2) Richiesta di allenamento -> genera il piano strutturato
    if is_workout or is_hard:
        coach = AICoach(summary=summary)
        try:
            result = await asyncio.to_thread(coach.generate, msg, override=is_override)
            workout = result["workout"]
            valid, issues = coach.validate_workout(workout, override=is_override)
            plan_output = {
                "generated_at": date.today().isoformat(),
                "model": coach.model,
                "usage": result.get("usage", {}),
                "readiness_at_generation": summary.get("readiness", {}),
                "override_richiesto": is_override,
                "validation_issues": issues,
                "workout": workout,
            }
            if (email or "").lower() == (_default_email() or "").lower():
                coach.save_output(plan_output)  # mirror legacy solo account principale
            _write_plan(email, plan_output)
        except Exception as err:  # noqa: BLE001
            logger.exception("Errore chat -> generazione")
            return {"kind": "error", "reply": f"❌ Generazione fallita: {err}"}

        text = AICoach.format_workout(workout)
        if is_override:
            text += "\n\n⚠️ Override atleta attivo: allenamento generato su tua richiesta nonostante la Readiness bassa."
        if issues:
            text += f"\n\n⚠️ Avvisi di validazione: {len(issues)}"
        return {"kind": "workout", "reply": text, "workout": _decorate_workout(workout),
                "override": is_override, "valid": valid}

    # 3) Conversazione generica -> risposta testuale basata sui dati reali
    history = payload.history or []
    try:
        reply = await asyncio.to_thread(_generic_chat, config, summary, msg, history)
    except Exception as err:  # noqa: BLE001
        logger.exception("Errore chat generica")
        return {"kind": "error", "reply": f"❌ Chat non disponibile: {err}"}

    _chat_history_for(uid).append({"role": "user", "content": msg})
    _chat_history_for(uid).append({"role": "assistant", "content": reply})
    # Cap di sicurezza: mantieni solo le ultime 100 voci (50 turni) per utente
    hist = _chat_history_for(uid)
    if len(hist) > 100:
        del hist[:len(hist) - 100]
    return {"kind": "chat", "reply": reply}


# =============================================================================
# PWA (static files)
# =============================================================================
_ensure_data_files()
db.init_db()          # SQLite multi-utente (data/myptai.db)
_migrate_legacy_to_sql()  # one-shot: JSON legacy -> tabelle SQL

# Warm-up eager del cipher AES-GCM: inizializza _cipher PRIMA del primo
# request, così _get_cipher() non dovrà mai acquisire _lock a runtime
# (previene il self-deadlock "_get_cipher dentro with _lock" sull'event loop).
_get_cipher()

# Le route /api/* sono registrate sopra: il mount statico su "/" cattura solo
# i path non-API (index.html, css, js, manifest.json, sw.js, icons/...).
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="pwa")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_server:app", host="0.0.0.0", port=8000, reload=True)
