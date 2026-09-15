#!/usr/bin/env python3
"""
database.py - Persistenza SQLite multi-utente (MyPT AI)
========================================================
Database SQLite unico: data/myptai.db (sovrascrivibile via env MYPTAI_DB).

Tabelle:
  users             -> account registrati (email Garmin univoca + token cifrati)
  user_biometrics   -> contesto biometrico (Readiness/summary) per utente/giorno
  user_workouts     -> piani generati e tabelle (kind: plan|table) per utente
  user_injuries     -> Injury Lab: fastidi muscolari per utente
  user_goals        -> obiettivo di gara (10K/21K) per utente

Sicurezza:
  - Le credenziali Garmin NON vengono mai salvate: solo il tokenstore della
    sessione Garmin, cifrato AES-256-GCM con la chiave master del server
    (campo users.tokens_enc BLOB).
  - Ogni query è parametrica (nessuna SQL injection).
  - FOREIGN KEY ... ON DELETE CASCADE: la cancellazione account (Art. 17 GDPR)
    elimina in modo atomico tutti i dati dell'utente.
  - Accesso single-process uvicorn: connessione per chiamata + lock sulle scritture.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "myptai.db"

DB_PATH = Path(os.environ.get("MYPTAI_DB", str(DEFAULT_DB_PATH)))
_lock = threading.Lock()


def datetime_now() -> str:
    return datetime.now().isoformat(timespec="seconds")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  email TEXT NOT NULL UNIQUE,
  garmin_display_name TEXT,
  tokens_enc BLOB,                 -- tokenstore sessione Garmin cifrato AES-256-GCM
  consent_version TEXT,
  consent_accepted INTEGER NOT NULL DEFAULT 0,
  created_at TEXT,
  last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS user_biometrics (
  user_id INTEGER NOT NULL,
  date TEXT NOT NULL,
  summary_json TEXT NOT NULL,
  updated_at TEXT,
  PRIMARY KEY (user_id, date),
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_workouts (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  kind TEXT NOT NULL,              -- 'plan' (workout_plan) | 'table' (tabella)
  date TEXT NOT NULL DEFAULT '__doc__',
  workout_json TEXT,
  synced INTEGER NOT NULL DEFAULT 0,
  garmin_workout_id TEXT,
  updated_at TEXT,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_injuries (
  id TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  part TEXT, label TEXT, severity INTEGER NOT NULL DEFAULT 1,
  note TEXT, x REAL, y REAL,
  created_at TEXT, resolved INTEGER NOT NULL DEFAULT 0,
  report_json TEXT,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS user_goals (
  user_id INTEGER PRIMARY KEY,
  race_type TEXT, target_time_s INTEGER, race_date TEXT,
  enabled INTEGER NOT NULL DEFAULT 0, updated_at TEXT,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Indici secondari: le query di isolamento filtrano per user_id (e kind).
CREATE INDEX IF NOT EXISTS idx_injuries_user ON user_injuries(user_id);
CREATE INDEX IF NOT EXISTS idx_workouts_user_kind ON user_workouts(user_id, kind);
CREATE INDEX IF NOT EXISTS idx_biometrics_user_date ON user_biometrics(user_id, date);
"""


def db_path() -> Path:
    return DB_PATH


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    """Crea le tabelle se non esistono (idempotente, chiamata all'avvio)."""
    with _lock:
        conn = _connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row is not None else None


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def upsert_user(email: str, display_name: Optional[str] = None) -> int:
    """Crea l'utente se non esiste, aggiorna last_login (ritorna user_id)."""
    now = datetime_now()
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE users SET last_login_at = ?, garmin_display_name = "
                    "COALESCE(?, garmin_display_name) WHERE id = ?",
                    (now, display_name, row["id"]),
                )
                user_id = row["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO users (email, garmin_display_name, created_at, last_login_at) "
                    "VALUES (?, ?, ?, ?)",
                    (email, display_name, now, now),
                )
                user_id = cur.lastrowid
            conn.commit()
            return user_id
        finally:
            conn.close()


def get_user_by_email(email: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            return _row_to_dict(conn.execute(
                "SELECT * FROM users WHERE email = ?", (email,)).fetchone())
        finally:
            conn.close()


def get_user(user_id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            return _row_to_dict(conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())
        finally:
            conn.close()


def set_user_tokens(user_id: int, tokens_enc: bytes) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute("UPDATE users SET tokens_enc = ? WHERE id = ?", (tokens_enc, user_id))
            conn.commit()
        finally:
            conn.close()


def get_user_tokens(user_id: int) -> Optional[bytes]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute("SELECT tokens_enc FROM users WHERE id = ?", (user_id,)).fetchone()
            return bytes(row["tokens_enc"]) if row and row["tokens_enc"] is not None else None
        finally:
            conn.close()


def set_user_consent(user_id: int, version: str, accepted: bool) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "UPDATE users SET consent_version = ?, consent_accepted = ? WHERE id = ?",
                (version, 1 if accepted else 0, user_id),
            )
            conn.commit()
        finally:
            conn.close()


def get_user_consent(user_id: int) -> dict:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT consent_version, consent_accepted FROM users WHERE id = ?",
                (user_id,)).fetchone()
            if not row:
                return {"accepted": False, "version": None}
            return {"accepted": bool(row["consent_accepted"]), "version": row["consent_version"]}
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# user_biometrics (summary Readiness per utente/giorno)
# ---------------------------------------------------------------------------
def save_biometric(user_id: int, summary: dict, day: Optional[str] = None) -> None:
    day = day or datetime_now()[:10]
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO user_biometrics (user_id, date, summary_json, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(user_id, date) DO UPDATE SET summary_json = excluded.summary_json, "
                "updated_at = excluded.updated_at",
                (user_id, day, json.dumps(summary, ensure_ascii=False, default=str), datetime_now()),
            )
            conn.commit()
        finally:
            conn.close()


def get_biometric(user_id: int, day: Optional[str] = None) -> Optional[dict]:
    day = day or datetime_now()[:10]
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT summary_json FROM user_biometrics WHERE user_id = ? AND date = ?",
                (user_id, day)).fetchone()
            if not row:
                return None
            return json.loads(row["summary_json"])
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# user_workouts (plan e table come documenti JSON per utente)
# ---------------------------------------------------------------------------
def save_workout_doc(user_id: int, kind: str, doc: dict) -> None:
    """Salva un documento (kind='plan'|'table'). Il documento completo (workout,
    flag sync, garmin_workout_id) è serializzato in workout_json."""
    with _lock:
        conn = _connect()
        try:
            existing = conn.execute(
                "SELECT id FROM user_workouts WHERE user_id = ? AND kind = ?",
                (user_id, kind)).fetchone()
            doc_id = existing["id"] if existing else f"{kind}-{user_id}"
            conn.execute(
                "INSERT INTO user_workouts (id, user_id, kind, date, workout_json, synced, "
                "garmin_workout_id, updated_at) VALUES (?, ?, ?, '__doc__', ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET workout_json = excluded.workout_json, "
                "synced = excluded.synced, garmin_workout_id = excluded.garmin_workout_id, "
                "updated_at = excluded.updated_at",
                (doc_id, user_id, kind,
                 json.dumps(doc, ensure_ascii=False, default=str),
                 1 if doc.get("synced") else 0,
                 doc.get("garmin_workout_id"), datetime_now()),
            )
            conn.commit()
        finally:
            conn.close()


def load_workout_doc(user_id: int, kind: str) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            row = conn.execute(
                "SELECT workout_json FROM user_workouts WHERE user_id = ? AND kind = ?",
                (user_id, kind)).fetchone()
            if not row:
                return None
            doc = json.loads(row["workout_json"])
            doc.setdefault("synced", False)
            return doc
        finally:
            conn.close()


def delete_workout_doc(user_id: int, kind: str) -> None:
    """v88: elimina la traccia di un documento (kind='plan'|'table') dal DB.
    Usato dal wipe totale per rimuovere la tabella Microciclo da user_workouts."""
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "DELETE FROM user_workouts WHERE user_id = ? AND kind = ?",
                (user_id, kind))
            conn.commit()
        finally:
            conn.close()


def delete_user_workouts(user_id: int) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM user_workouts WHERE user_id = ?", (user_id,))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# user_injuries
# ---------------------------------------------------------------------------
def list_injuries(user_id: int) -> list:
    with _lock:
        conn = _connect()
        try:
            rows = conn.execute(
                "SELECT * FROM user_injuries WHERE user_id = ? ORDER BY created_at DESC, rowid",
                (user_id,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["report"] = json.loads(d.pop("report_json")) if d.get("report_json") else {}
                d["resolved"] = bool(d["resolved"])
                out.append(d)
            return out
        finally:
            conn.close()


def add_injury(user_id: int, injury: dict) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO user_injuries (id, user_id, part, label, severity, note, x, y, "
                "created_at, resolved, report_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (injury["id"], user_id, injury.get("part"), injury.get("label"),
                 injury.get("severity", 1), injury.get("note"), injury.get("x"),
                 injury.get("y"), injury.get("created_at"),
                 1 if injury.get("resolved") else 0,
                 json.dumps(injury.get("report") or {}, ensure_ascii=False, default=str)),
            )
            conn.commit()
        finally:
            conn.close()


def update_injury(user_id: int, injury: dict) -> bool:
    """Aggiorna il record dell'infortunio (match su id + user_id)."""
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "UPDATE user_injuries SET part = ?, label = ?, severity = ?, note = ?, x = ?, "
                "y = ?, resolved = ?, report_json = ? WHERE id = ? AND user_id = ?",
                (injury.get("part"), injury.get("label"), injury.get("severity", 1),
                 injury.get("note"), injury.get("x"), injury.get("y"),
                 1 if injury.get("resolved") else 0,
                 json.dumps(injury.get("report") or {}, ensure_ascii=False, default=str),
                 injury["id"], user_id),
            )
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def delete_injury(user_id: int, injury_id: str) -> bool:
    with _lock:
        conn = _connect()
        try:
            cur = conn.execute(
                "DELETE FROM user_injuries WHERE id = ? AND user_id = ?",
                (injury_id, user_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()


def replace_injuries(user_id: int, injuries: list) -> None:
    """Sostituisce l'intera lista infortuni dell'utente (delete-all + insert
    atomici) — usata dagli endpoint che mutano la lista in memoria."""
    with _lock:
        conn = _connect()
        try:
            conn.execute("DELETE FROM user_injuries WHERE user_id = ?", (user_id,))
            for inj in injuries:
                conn.execute(
                    "INSERT INTO user_injuries (id, user_id, part, label, severity, note, x, y, "
                    "created_at, resolved, report_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (inj.get("id"), user_id, inj.get("part"), inj.get("label"),
                     inj.get("severity", 1), inj.get("note"), inj.get("x"),
                     inj.get("y"), inj.get("created_at"),
                     1 if inj.get("resolved") else 0,
                     json.dumps(inj.get("report") or {}, ensure_ascii=False, default=str)))
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# user_goals
# ---------------------------------------------------------------------------
def get_goals(user_id: int) -> Optional[dict]:
    with _lock:
        conn = _connect()
        try:
            return _row_to_dict(conn.execute(
                "SELECT * FROM user_goals WHERE user_id = ?", (user_id,)).fetchone())
        finally:
            conn.close()


def set_goals(user_id: int, goals: dict) -> None:
    with _lock:
        conn = _connect()
        try:
            conn.execute(
                "INSERT INTO user_goals (user_id, race_type, target_time_s, race_date, enabled, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET race_type = excluded.race_type, "
                "target_time_s = excluded.target_time_s, race_date = excluded.race_date, "
                "enabled = excluded.enabled, updated_at = excluded.updated_at",
                (user_id, goals.get("race_type"), goals.get("target_time_s"),
                 goals.get("race_date"), 1 if goals.get("enabled") else 0,
                 goals.get("updated_at") or datetime_now()),
            )
            conn.commit()
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Cancellazione account (Art. 17 GDPR) — cascata atomica
# ---------------------------------------------------------------------------
def delete_user(user_id: int) -> int:
    """Elimina l'utente e tutti i suoi dati (ON DELETE CASCADE). Ritorna il
    numero totale di righe rimosse (utente incluso)."""
    with _lock:
        conn = _connect()
        try:
            rows = 0
            for tbl in ("user_biometrics", "user_workouts", "user_injuries", "user_goals"):
                cur = conn.execute(f"DELETE FROM {tbl} WHERE user_id = ?", (user_id,))
                rows += cur.rowcount
            cur = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
            rows += cur.rowcount
            conn.commit()
            return rows
        finally:
            conn.close()


def datetime_now() -> str:
    return datetime.now().isoformat(timespec="seconds")
