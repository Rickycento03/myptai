"""Private, collision-resistant storage shared by the web app and Garmin tools."""
import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TOKEN_FILES = frozenset({"garmin_tokens.json", "oauth1_token.json", "oauth2_token.json"})


def canonical_email(email: str) -> str:
    value = (email or "").strip().lower()
    if not value or "@" not in value:
        raise ValueError("Account identity required")
    return value


def account_key(email: str) -> str:
    return hashlib.sha256(canonical_email(email).encode("utf-8")).hexdigest()


def private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Symlink not permitted for private storage")
    path.chmod(0o700)
    return path


def write_private_json(path: Path, value) -> None:
    if path.parent == PROJECT_ROOT:
        path.parent.mkdir(parents=True, exist_ok=True)
    else:
        private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=".myptai-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
        path.chmod(0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def user_dir(email: str) -> Path:
    return PROJECT_ROOT / "data" / "users" / account_key(email)


def owner_email() -> str:
    try:
        cfg = json.loads((PROJECT_ROOT / "config.json").read_text())
        return canonical_email((cfg.get("garmin") or {}).get("email"))
    except (OSError, ValueError, TypeError):
        return ""


def deletion_marker(email: str) -> Path:
    return PROJECT_ROOT / "data" / "deleted_users" / (account_key(email) + ".json")


def may_migrate_owner(email: str) -> bool:
    return canonical_email(email) == owner_email() and not deletion_marker(email).exists()


def legacy_paths(email: str) -> dict:
    email = canonical_email(email)
    old_token_slug = email.replace("@", "_").replace(".", "_")
    web_slug = re.sub(r"[^a-z0-9]+", "_", email).strip("_")
    memory_slug = "".join(c if c.isalnum() or c in "._-" else "_" for c in email)
    paths = {
        "web": PROJECT_ROOT / "data" / "users" / web_slug,
        "memory": PROJECT_ROOT / "data" / "users" / memory_slug,
    }
    # Historical email handling accepted slash characters. Never follow those paths.
    if "/" not in old_token_slug and "\\" not in old_token_slug:
        paths["tokens"] = Path.home() / (".garminconnect_session_" + old_token_slug)
        paths["dump"] = PROJECT_ROOT / "data" / "dumps" / ("garmin_raw_dump_" + old_token_slug + ".json")
    return paths


def migrate_owner_file(email: str, destination: Path, candidates) -> None:
    """Only the explicitly configured owner can inherit a legacy global file."""
    if destination.exists() or not may_migrate_owner(email):
        return
    # A legacy slug may name several distinct registered accounts. Migration
    # must not guess ownership merely because the destination is the owner.
    import database as db
    try:
        other_paths = [p for other in db.list_user_emails() if canonical_email(other) != canonical_email(email)
                       for p in legacy_paths(other).values()]
    except Exception:
        return
    for source in candidates:
        source = Path(source)
        if any(source == p or p in source.parents for p in other_paths):
            continue
        if source.is_file() and not source.is_symlink():
            write_private_json(destination, json.loads(source.read_text(encoding="utf-8")))
            return


def bind_tokenstore(path: Path, email: str) -> None:
    private_dir(path)
    for name in TOKEN_FILES:
        p = path / name
        if p.exists():
            if not p.is_file() or p.is_symlink():
                raise ValueError("Invalid token file")
            p.chmod(0o600)
    write_private_json(path / "account.json", {"version": 1, "account_key": account_key(email)})


def tokenstore_is_bound(path: Path, email: str) -> bool:
    try:
        if path.is_symlink():
            return False
        metadata = json.loads((path / "account.json").read_text())
        return (metadata.get("version") == 1 and metadata.get("account_key") == account_key(email)
                and any((path / name).is_file() and not (path / name).is_symlink() for name in TOKEN_FILES))
    except (OSError, ValueError, TypeError):
        return False


def remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def temporary_tokenstores(email: str) -> list:
    """Find this account's bound session copies left by a previous process."""
    result = []
    for path in Path(tempfile.gettempdir()).glob("myptai_sync_*"):
        if (not path.is_symlink() and path.is_dir() and path.stat().st_uid == os.getuid()
                and tokenstore_is_bound(path / "session", email)):
            result.append(path)
    return result
