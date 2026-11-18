"""Offline regression tests. Garmin/AI calls use synthetic doubles only."""
import asyncio
import json
import os
import socket
import stat
import sys
import tempfile
import types
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import database as db
import user_storage as storage
import garmin_probe as probe
import athlete_state as memory
import web_server as web
from fastapi.testclient import TestClient
from ai_coach import AICoach
from workout_sync import WorkoutSync


class FakeGarmin:
    calls = []

    def __init__(self, email, password):
        self.email, self.password = email, password
        self.client = self

    def login(self, tokenstore=None):
        chosen = tokenstore or os.environ.get("GARMINTOKENS")
        files = Path(chosen) / "garmin_tokens.json" if chosen else None
        restored = bool(files and files.is_file())
        self.calls.append({"restored": restored, "explicit": bool(tokenstore)})
        if not restored and self.password != "valid-test-password":
            raise ValueError("invalid credentials")
        return None, None

    def dump(self, destination):
        dest = Path(destination)
        dest.mkdir(parents=True, exist_ok=True)
        (dest / "garmin_tokens.json").write_text('{"di_refresh_token":"synthetic-test-refresh","di_token":"synthetic-test-access"}')

    def get_full_name(self):
        return "Synthetic athlete"


class SecurityRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="myptai-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "web").mkdir()
        self.owner = "owner@example.test"
        self.email = "athlete@example.test"
        (self.root / "config.json").write_text(json.dumps({
            "garmin": {"email": self.owner, "password": "valid-test-password"},
            "deepseek": {"api_key": "synthetic-test-key", "base_url": "https://example.test"}}))
        patches = [patch.object(module, "PROJECT_ROOT", self.root)
                   for module in (storage, probe, memory, web)]
        patches += [patch.object(web, name, self.root / value) for name, value in {
            "DATA_DIR": "data", "USERS_DIR": "data/users", "CONFIG_PATH": "config.json",
            "SESSIONS_PATH": "data/sessions.json", "SECRET_FILE": "data/secret.key",
            "GOALS_PATH": "data/goals.json", "TABLE_PATH": "data/table_plan.json",
            "PLAN_PATH": "workout_plan.json", "SUMMARY_PATH": "analytics_summary.json"}.items()]
        patches += [patch.object(memory, "MASTER_MEMORY_PATH", self.root / "data/athlete_state.json"),
                    patch.object(storage.Path, "home", return_value=self.root),
                    patch.object(memory, "FALLBACK_COACH_MEMORY", self.root / "coach_memory.json"),
                    patch.object(db, "DB_PATH", self.root / "data/myptai.db"),
                    patch.object(web, "_cipher", web._AESGCMCipher(b"0" * 32)),
                    patch.object(web, "_start_biometric_extract"),
                    patch.object(web, "_api_limiter", web._RateLimiter(10000, 60)),
                    patch.object(web, "_auth_limiter", web._RateLimiter(10000, 900)),
                    patch.object(web, "_login_attempt_limiter", web._RateLimiter(10000, 900)),
                    patch.object(web, "_table_limiter", web._RateLimiter(3, 86400)),
                    patch.object(web, "_garmin_login_retry_until", 0.0),
                    patch.object(web, "_user_ts_cache", {}),
                    patch.object(web, "_chat_histories", {}),
                    patch.object(socket.socket, "connect", side_effect=AssertionError("External network forbidden")),
                    patch.dict(sys.modules, {"garminconnect": types.SimpleNamespace(Garmin=FakeGarmin)})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        FakeGarmin.calls.clear()
        web._ensure_data_files()
        db.init_db()
        self.uid = db.upsert_user(self.email)
        self.client = TestClient(web.app)
        self.addCleanup(self.client.close)

    def headers(self, email=None, consent=True):
        email = email or self.email
        uid = db.get_user_by_email(email)["id"]
        if consent:
            db.set_user_consent(uid, web.CONSENT_VERSION, True)
        return {"Authorization": "Bearer " + web._new_session(uid, email)}

    def token_payload(self, headers, mutate):
        digest = web.hashlib.sha256(headers["Authorization"][7:].encode()).hexdigest()
        sessions = web._load_sessions()
        payload = json.loads(web._get_cipher().decrypt(sessions[digest].encode()))
        mutate(payload)
        sessions[digest] = web._get_cipher().encrypt(json.dumps(payload).encode()).decode()
        web._save_sessions(sessions)

    def test_wrong_password_cannot_use_database_permanent_or_environment_tokens(self):
        permanent = probe.tokenstore_path(self.email)
        FakeGarmin(self.email, "valid-test-password").dump(permanent)
        storage.bind_tokenstore(permanent, self.email)
        with tempfile.TemporaryDirectory() as tmp:
            web._save_garth_tokens(self.uid, FakeGarmin(self.email, "valid-test-password"), Path(tmp))
        with patch.dict(os.environ, {"GARMINTOKENS": str(permanent), "GARMIN_TOKENSTORE": str(permanent)}):
            response = self.client.post("/api/auth", json={"email": self.email, "password": "wrong-test-password"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(web._load_sessions(), {})
        self.assertFalse(FakeGarmin.calls[-1]["restored"])
        self.assertTrue(FakeGarmin.calls[-1]["explicit"])

    def test_valid_password_still_creates_session_and_bound_tokens(self):
        response = self.client.post("/api/auth", json={"email": self.email, "password": "valid-test-password"})
        self.assertEqual(response.status_code, 200)
        token = response.json()["token"]
        result = self.client.get("/api/user/me", headers={"Authorization": "Bearer " + token})
        self.assertEqual(result.json()["email"], self.email)
        self.assertTrue(storage.tokenstore_is_bound(probe.tokenstore_path(self.email), self.email))
        web._start_biometric_extract.assert_not_called()

    def test_password_spaces_are_not_removed(self):
        response = self.client.post("/api/auth", json={"email": self.email, "password": " valid-test-password "})
        self.assertEqual(response.status_code, 401)

    def test_verified_password_can_login_without_new_garmin_sso(self):
        first=self.client.post("/api/auth",json={"email":self.email,"password":"valid-test-password"})
        self.assertEqual(first.status_code,200)
        verifier=db.get_login_verifier(self.uid)
        self.assertIsNotNone(verifier)
        self.assertNotIn(b"valid-test-password",verifier)
        clear=web._get_cipher().decrypt(verifier)
        self.assertNotIn(b"valid-test-password",clear)
        with patch.object(web,"_fresh_garmin_password_login",side_effect=AssertionError("No SSO needed")):
            second=self.client.post("/api/auth",json={"email":self.email,"password":"valid-test-password"})
        self.assertEqual(second.status_code,200)

    def test_wrong_password_still_fails_with_valid_local_verifier_and_tokens(self):
        self.client.post("/api/auth",json={"email":self.email,"password":"valid-test-password"})
        response=self.client.post("/api/auth",json={"email":self.email,"password":"wrong-test-password"})
        self.assertEqual(response.status_code,401)
        self.assertNotIn("token",response.json())

    def test_owner_migration_requires_exact_configured_password(self):
        uid=db.upsert_user(self.owner)
        db.set_user_tokens(uid,web._get_cipher().encrypt(json.dumps({"garmin_tokens.json":'{"di_refresh_token":"synthetic-test-refresh"}'}).encode()))
        with patch.object(web,"_fresh_garmin_password_login",side_effect=AssertionError("No SSO needed")):
            response=self.client.post("/api/auth",json={"email":self.owner,"password":"valid-test-password"})
        self.assertEqual(response.status_code,200)
        self.assertTrue(web._password_verified(uid,self.owner,"valid-test-password"))
        self.assertTrue(storage.tokenstore_is_bound(probe.tokenstore_path(self.owner),self.owner))

    def test_owner_legacy_tokens_with_wrong_password_cannot_migrate(self):
        uid=db.upsert_user(self.owner)
        db.set_user_tokens(uid,web._get_cipher().encrypt(json.dumps({"garmin_tokens.json":"{}"}).encode()))
        response=self.client.post("/api/auth",json={"email":self.owner,"password":"wrong-test-password"})
        self.assertEqual(response.status_code,401)
        self.assertIsNone(db.get_login_verifier(uid))

    def test_other_account_cannot_use_owner_migration_or_copied_verifier(self):
        uid=db.upsert_user(self.owner)
        web._save_login_verifier(uid,self.owner,"valid-test-password")
        db.set_login_verifier(self.uid,db.get_login_verifier(uid))
        db.set_user_tokens(self.uid,web._get_cipher().encrypt(json.dumps({"garmin_tokens.json":"{}"}).encode()))
        with patch.object(web,"_fresh_garmin_password_login",side_effect=web._GarminLoginUnavailable(300)) as fresh:
            response=self.client.post("/api/auth",json={"email":self.email,"password":"valid-test-password"})
        self.assertEqual(response.status_code,503)
        fresh.assert_called_once()
        self.assertNotIn("token",response.json())

    def test_provider_rate_limit_is_not_wrong_credentials_or_auth_penalty(self):
        RateLimited=type("GarminConnectTooManyRequestsError",(Exception,),{})
        with patch.object(probe,"build_client",side_effect=RateLimited("All login strategies rate limited")) as fresh:
            for _ in range(6):
                response=self.client.post("/api/auth",json={"email":self.email,"password":"valid-test-password"})
                self.assertEqual(response.status_code,503)
                self.assertEqual(response.headers["retry-after"],response.json()["retry_after_s"].__str__())
            self.assertEqual(fresh.call_count,1)
        self.assertFalse(any(web._auth_limiter._hits.values()))

    def test_provider_connection_error_is_not_credential_error(self):
        Unavailable=type("GarminConnectConnectionError",(Exception,),{})
        with patch.object(probe,"build_client",side_effect=Unavailable("All login strategies exhausted: HTTP 403")):
            response=self.client.post("/api/auth",json={"email":self.email,"password":"valid-test-password"})
        self.assertEqual(response.status_code,503)
        self.assertNotIn("token",response.json())
        self.assertFalse(any(web._auth_limiter._hits.values()))

    def test_old_config_password_cannot_override_an_enrolled_new_password(self):
        uid=db.upsert_user(self.owner)
        web._save_login_verifier(uid,self.owner,"different-test-password")
        db.set_user_tokens(uid,web._get_cipher().encrypt(json.dumps({"garmin_tokens.json":"{}"}).encode()))
        with patch.object(web,"_fresh_garmin_password_login",side_effect=web._GarminLoginUnavailable(300)):
            response=self.client.post("/api/auth",json={"email":self.owner,"password":"valid-test-password"})
        self.assertEqual(response.status_code,503)
        self.assertTrue(web._password_verified(uid,self.owner,"different-test-password"))

    def test_local_wrong_password_attempts_are_bounded_during_provider_outage(self):
        self.client.post("/api/auth",json={"email":self.email,"password":"valid-test-password"})
        with patch.object(web,"_auth_limiter",web._RateLimiter(5,900)), \
             patch.object(web,"_fresh_garmin_password_login",side_effect=web._GarminLoginUnavailable(300)):
            for _ in range(5):
                self.assertEqual(self.client.post("/api/auth",json={"email":self.email,"password":"wrong-test-password"}).status_code,503)
            self.assertEqual(self.client.post("/api/auth",json={"email":self.email,"password":"wrong-test-password"}).status_code,429)

    def test_verifier_and_new_sessions_have_fixed_proof_expiration(self):
        response=self.client.post("/api/auth",json={"email":self.email,"password":"valid-test-password"})
        proof=json.loads(web._get_cipher().decrypt(db.get_login_verifier(self.uid)))
        sessions=web._load_sessions()
        session=json.loads(web._get_cipher().decrypt(next(iter(sessions.values())).encode()))
        self.assertLessEqual(session["expires_at"],proof["expires_at"])
        self.assertAlmostEqual(proof["expires_at"]-proof["verified_at"],web._PASSWORD_PROOF_TTL_S)
        proof["verified_at"]=web.time.time()-web._PASSWORD_PROOF_TTL_S-1
        proof["expires_at"]=web.time.time()-1
        db.set_login_verifier(self.uid,web._get_cipher().encrypt(json.dumps(proof).encode()))
        with patch.object(web,"_fresh_garmin_password_login",side_effect=web._GarminLoginUnavailable(300)):
            response=self.client.post("/api/auth",json={"email":self.email,"password":"valid-test-password"})
        self.assertEqual(response.status_code,503)
        self.assertNotIn("token",response.json())

    def test_fresh_password_reenrollment_revokes_old_device_sessions(self):
        self.client.post("/api/auth",json={"email":self.email,"password":"valid-test-password"})
        old=self.headers()
        web._save_login_verifier(self.uid,self.email,"different-test-password")
        self.assertEqual(self.client.get("/api/user/me",headers=old).status_code,401)
        self.assertFalse(web._password_verified(self.uid,self.email,"valid-test-password"))

    def test_wrapped_cloudflare_authentication_error_is_not_password_failure(self):
        Wrapped=type("GarminConnectAuthenticationError",(Exception,),{})
        with patch.object(probe,"build_client",side_effect=Wrapped("Login failed: Cloudflare HTTP 403")):
            response=self.client.post("/api/auth",json={"email":self.email,"password":"valid-test-password"})
        self.assertEqual(response.status_code,503)
        self.assertFalse(any(web._auth_limiter._hits.values()))

    def test_same_account_login_is_atomic_and_concurrent_attempt_is_rejected(self):
        from concurrent.futures import ThreadPoolExecutor
        started,release=threading.Event(),threading.Event()
        def fresh(email,password):
            started.set()
            if not release.wait(5):raise AssertionError("Test synchronization timed out")
            return FakeGarmin(email,password)
        with patch.object(web,"_fresh_garmin_password_login",side_effect=fresh),ThreadPoolExecutor(max_workers=1) as pool:
            first=pool.submit(self.client.post,"/api/auth",json={"email":self.email,"password":"valid-test-password"})
            self.assertTrue(started.wait(5))
            try:
                second=self.client.post("/api/auth",json={"email":self.email,"password":"valid-test-password"})
                self.assertEqual(second.status_code,503)
                self.assertNotIn("token",second.json())
            finally:
                release.set()
            self.assertEqual(first.result(timeout=5).status_code,200)

    def test_aliases_have_distinct_storage_in_all_representations(self):
        aliases = ["a.b@example.test", "a_b@example.test", "a+b@example.test", "a-b@example.test", "a/b@example.test"]
        for helper in (probe.tokenstore_path, probe.per_user_dump_path, storage.user_dir, memory._user_memory_path):
            self.assertEqual(len({helper(email) for email in aliases}), len(aliases))
        self.assertEqual(storage.account_key(" A.B@EXAMPLE.TEST "), storage.account_key(aliases[0]))

    def test_internal_bound_token_reuse_still_works_without_password(self):
        target = probe.tokenstore_path(self.email)
        FakeGarmin(self.email, "valid-test-password").dump(target)
        storage.bind_tokenstore(target, self.email)
        probe.build_client(self.email, "")
        self.assertTrue(FakeGarmin.calls[-1]["restored"])
        sync = WorkoutSync(config={"garmin": {"email": self.email, "password": "", "tokenstore_path": str(target)}})
        sync.client()
        self.assertTrue(FakeGarmin.calls[-1]["restored"])

    def test_mismatched_or_unbound_store_is_rejected(self):
        target = probe.tokenstore_path(self.email)
        FakeGarmin(self.email, "valid-test-password").dump(target)
        with self.assertRaises(RuntimeError):
            probe.build_client(self.email, "")
        storage.bind_tokenstore(target, self.owner)
        with self.assertRaises(RuntimeError):
            probe.build_client(self.email, "")

    def test_encrypted_token_envelope_rejects_alias_and_path_traversal(self):
        payload = {"version": 2, "account_key": storage.account_key(self.email),
                   "files": {"garmin_tokens.json": "{}"}}
        encoded = web._get_cipher().encrypt(json.dumps(payload).encode())
        with self.assertRaises(ValueError):
            web._write_garmin_token_dir(encoded, self.root / "restore", self.owner)
        payload["files"] = {"../../outside.json": "{}"}
        with self.assertRaises(ValueError):
            web._write_garmin_token_dir(web._get_cipher().encrypt(json.dumps(payload).encode()), self.root / "restore", self.email)
        self.assertFalse((self.root / "outside.json").exists())

    def test_token_restore_and_json_writes_have_private_permissions(self):
        payload = {"version": 2, "account_key": storage.account_key(self.email),
                   "files": {"garmin_tokens.json": "{}"}}
        target = web._write_garmin_token_dir(web._get_cipher().encrypt(json.dumps(payload).encode()), self.root / "restore", self.email)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((target / "garmin_tokens.json").stat().st_mode), 0o600)
        self.assertTrue(storage.tokenstore_is_bound(target, self.email))

    def test_other_users_cannot_inherit_or_overwrite_owner_memory(self):
        master = self.root / "data/athlete_state.json"
        storage.write_private_json(master, {"profilo_atleta": {"id": self.owner, "nome": "Synthetic owner"}, "private": "owner-only-test"})
        state = memory.load_athlete_state("other@example.test")
        self.assertNotIn("private", state)
        self.assertEqual(state["note_soggettive_e_stile_vita"], {})
        self.assertNotEqual(state["profilo_atleta"].get("nome"), "Synthetic owner")
        memory.save_athlete_state({"profilo_atleta": {"id": self.email}}, self.email)
        self.assertEqual(json.loads(master.read_text())["private"], "owner-only-test")
        owner = memory.load_athlete_state(self.owner)
        self.assertEqual(owner["private"], "owner-only-test")
        self.assertTrue((storage.user_dir(self.owner) / "athlete_state.json").is_file())

    def test_empty_owner_profile_has_no_embedded_personal_records(self):
        state = memory.load_athlete_state(self.owner)
        profile = state["profilo_atleta"]
        self.assertEqual(profile["id"], self.owner)
        self.assertEqual(profile["nome"], "")
        self.assertEqual(profile["pb"], {})
        self.assertEqual(profile["fisiologia_cardiaca"], {})
        self.assertEqual(state["stato_fisiologico_live"], {})
        self.assertEqual(state["note_soggettive_e_stile_vita"], {})
        state["private"] = "saved-personal-state"
        memory.save_athlete_state(state, self.owner)
        self.assertEqual(memory.load_athlete_state(self.owner), state)

    def test_memory_hrv_band_uses_only_current_user_values(self):
        fresh = memory.sync_state_from_biometrics({}, self.email)
        self.assertIsNone(fresh["stato_fisiologico_live"]["recovery_metrics"]["hrv_baseline_range"])
        measured = memory.sync_state_from_biometrics({"biometria": {"hrv_baseline": {"balancedLow": 45, "balancedUpper": 80}}}, self.email)
        self.assertEqual(measured["stato_fisiologico_live"]["recovery_metrics"]["hrv_baseline_range"], "45-80")
        retained = memory.sync_state_from_biometrics({}, self.email)
        self.assertEqual(retained["stato_fisiologico_live"]["recovery_metrics"]["hrv_baseline_range"], "45-80")
        other = memory.sync_state_from_biometrics({}, "other@example.test")
        self.assertIsNone(other["stato_fisiologico_live"]["recovery_metrics"]["hrv_baseline_range"])

    def test_lap_classification_uses_private_account_boundaries(self):
        from analytics_engine import AnalyticsEngine
        config = {"garmin": {"email": self.owner}, "athlete": {"max_hr": 191, "lactate_threshold_hr": 170,
                  "threshold_pace_sec_per_km": 300, "lap_hr_boundaries_bpm": [110, 145, 165]}}
        engine = AnalyticsEngine(config=config, dump={}, email=self.owner)
        lap = {"averageHR": 150, "distance": 1000, "duration": 360}
        self.assertEqual(engine.classify_lap(lap)["intensity"], "EASY_Z3")
        engine.athlete["lap_hr_boundaries_bpm"] = [110, 155, 170]
        self.assertEqual(engine.classify_lap(lap)["intensity"], "EASY_Z2")
        other = AnalyticsEngine(config=config, dump={}, email=self.email)
        self.assertNotIn("lap_hr_boundaries_bpm", other.athlete)

    def test_owner_migration_skips_colliding_legacy_account(self):
        alias = self.owner.replace("owner", "ow.ner")
        owner = self.owner.replace("owner", "ow_ner")
        cfg=json.loads((self.root / "config.json").read_text())
        cfg["garmin"]["email"]=owner
        (self.root / "config.json").write_text(json.dumps(cfg))
        db.upsert_user(alias)
        db.upsert_user(owner)
        shared=storage.legacy_paths(alias)["web"] / "athlete_state.json"
        storage.write_private_json(shared,{"private":"alias-only"})
        state=memory.load_athlete_state(owner)
        self.assertNotIn("private",state)

    def test_secondary_analytics_never_inherits_owner_profile_or_empty_dump_fallback(self):
        from analytics_engine import AnalyticsEngine
        config={"garmin":{"email":self.owner},"athlete":{
            "name":"Private synthetic owner","age":42,"weight_kg":88,"max_hr":199,"baseline_vo2max":57}}
        with patch.object(AnalyticsEngine,"_load_json",side_effect=AssertionError("Global fallback forbidden")):
            engine=AnalyticsEngine(config=config,dump={},email=self.email)
        self.assertNotIn("age",engine.athlete)
        self.assertNotIn("weight_kg",engine.athlete)
        self.assertEqual(engine.athlete["name"],self.email)
        self.assertEqual(engine.dump,{})
        summary=engine.build_context()
        self.assertIsNone(summary["atleta"]["eta"])
        self.assertIsNone(summary["atleta"]["peso_kg"])

    def test_decorating_workouts_never_reads_default_owner_summary(self):
        with patch.object(web,"_load_summary",side_effect=AssertionError("Owner fallback forbidden")):
            self.assertEqual(web._decorate_workout({"nome":"Synthetic workout"})["nome"],"Synthetic workout")
        with patch.object(web,"_default_email",return_value=""):
            with self.assertRaises(web.HTTPException):web._user_id_for()
        self.assertIsNone(db.get_user_by_email(""))

    def test_legacy_table_mirror_is_not_read_or_written_by_other_account(self):
        storage.write_private_json(web.TABLE_PATH, {"owner": "synthetic-owner-table"})
        self.assertEqual(web._load_user_json(self.email, "table_plan.json", {}), {})
        web._write_user_json(self.email, "table_plan.json", {"entries": []})
        self.assertEqual(json.loads(web.TABLE_PATH.read_text()), {"owner": "synthetic-owner-table"})

    def test_restart_does_not_reimport_legacy_data_for_existing_sql_account(self):
        db.upsert_user(self.owner)
        storage.write_private_json(web.SUMMARY_PATH,{"garmin_data_date":"2000-01-01","synthetic":True})
        storage.write_private_json(web.TABLE_PATH,{"entries":[{"synthetic":True}]})
        web._migrate_legacy_to_sql()
        self.assertIsNone(db.get_biometric(db.get_user_by_email(self.owner)["id"]))
        self.assertIsNone(db.load_workout_doc(db.get_user_by_email(self.owner)["id"],"table"))

    def test_first_migration_preserves_original_biometric_date(self):
        summary={"garmin_data_date":"2000-01-01","synthetic":True}
        storage.write_private_json(web.SUMMARY_PATH,summary)
        web._migrate_legacy_to_sql()
        owner_uid=db.get_user_by_email(self.owner)["id"]
        self.assertEqual(db.get_biometric(owner_uid,"2000-01-01"),summary)
        self.assertIsNone(db.get_biometric(owner_uid))

    def test_account_delete_removes_bound_orphan_token_copies_only(self):
        own=self.root/"myptai_sync_old_process"/"session"
        other=self.root/"myptai_sync_other_account"/"session"
        for target,email in ((own,self.email),(other,self.owner)):
            FakeGarmin(email,"valid-test-password").dump(target)
            storage.bind_tokenstore(target,email)
        with patch("user_storage.tempfile.gettempdir",return_value=str(self.root)):
            web._delete_user_files(self.uid,self.email)
        self.assertFalse(own.parent.exists())
        self.assertTrue(other.exists())

    def test_sessions_expire_and_legacy_sessions_are_denied(self):
        for mutator in (
            lambda p: p.update(expires_at=web.time.time()-1),
            lambda p: p.update(last_seen=web.time.time()-web.SESSION_IDLE_TTL_S-1, created_at=web.time.time()-web.SESSION_IDLE_TTL_S-2),
            lambda p: p.pop("version"),
            lambda p: p.update(expires_at=float("inf")),
        ):
            headers = self.headers()
            self.token_payload(headers, mutator)
            self.assertEqual(self.client.get("/api/user/me", headers=headers).status_code, 401)

    def test_logout_revokes_only_current_device(self):
        first, second = self.headers(), self.headers()
        self.assertEqual(self.client.post("/api/user/logout", headers=first).status_code, 200)
        self.assertEqual(self.client.get("/api/user/me", headers=first).status_code, 401)
        self.assertEqual(self.client.get("/api/user/me", headers=second).status_code, 200)

    def test_consent_gates_personal_apis_but_not_profile_legal_or_deletion(self):
        headers = self.headers(consent=False)
        with patch.object(web, "_load_summary") as summary:
            self.assertEqual(self.client.get("/api/readiness", headers=headers).status_code, 403)
            self.assertEqual(self.client.post("/api/refresh", headers=headers).status_code, 403)
            summary.assert_not_called()
        self.assertEqual(self.client.get("/api/user/me", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/api/legal/privacy").status_code, 200)
        self.assertEqual(self.client.post("/api/user/consent", headers=headers, json={"accepted":True,"version":"9.9"}).status_code, 400)
        self.assertEqual(self.client.post("/api/user/consent", headers=headers, json={"accepted":True,"version":"1.1"}).status_code, 200)
        web._start_biometric_extract.assert_called_once_with(self.uid, self.email)
        with patch.object(web, "_load_summary", return_value={}):
            self.assertEqual(self.client.get("/api/health", headers=headers).status_code, 200)
        self.client.post("/api/user/consent", headers=headers, json={"accepted":False,"version":"1.1"})
        self.assertEqual(self.client.get("/api/readiness", headers=headers).status_code, 403)

    def test_background_extract_does_not_run_without_consent_or_after_deletion(self):
        with patch.object(web, "_initial_biometric_extract_locked") as extract:
            web._initial_biometric_extract(self.uid, self.email)
            db.set_user_consent(self.uid, "1.1", True)
            db.delete_user(self.uid)
            web._initial_biometric_extract(self.uid, self.email)
            extract.assert_not_called()

    def test_api_responses_including_auth_errors_cannot_be_cached(self):
        for headers in ({}, self.headers(consent=False)):
            response = self.client.get("/api/readiness", headers=headers)
            self.assertIn("no-store", response.headers["cache-control"])
            self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_delete_cleans_own_files_revokes_sessions_and_preserves_other_user(self):
        other = "other@example.test"
        other_uid = db.upsert_user(other)
        for email in (self.email, other):
            storage.write_private_json(storage.user_dir(email) / "athlete_state.json", {"synthetic": True})
            storage.write_private_json(probe.per_user_dump_path(email), {"synthetic": True})
            FakeGarmin(email, "valid-test-password").dump(probe.tokenstore_path(email))
        shared_memory = storage.legacy_paths(self.email)["memory"]
        storage.write_private_json(shared_memory / "athlete_state.json", {})
        first, second = self.headers(), self.headers()
        other_headers = self.headers(other)
        self.assertEqual(self.client.post("/api/user/delete-account", headers=first).status_code, 200)
        self.assertIsNone(db.get_user(self.uid))
        self.assertFalse(storage.user_dir(self.email).exists())
        self.assertFalse(probe.per_user_dump_path(self.email).exists())
        self.assertFalse(shared_memory.exists())
        self.assertTrue(storage.user_dir(other).exists())
        self.assertTrue(probe.per_user_dump_path(other).exists())
        self.assertEqual(self.client.get("/api/user/me", headers=second).status_code, 401)
        self.assertEqual(self.client.get("/api/user/me", headers=other_headers).status_code, 200)
        self.assertIsNotNone(db.get_user(other_uid))

    def test_delete_preserves_ambiguous_legacy_files(self):
        a, b = "a.b@example.test", "a_b@example.test"
        aid = db.upsert_user(a)
        db.upsert_user(b)
        target = storage.legacy_paths(a)["tokens"]
        with patch.object(storage.Path, "home", return_value=self.root):
            target = storage.legacy_paths(a)["tokens"]
            storage.write_private_json(target / "garmin_tokens.json", {})
            result = web._delete_user_files(aid, a)
            self.assertTrue(target.exists())
            self.assertIn("ambiguous_legacy_path", result["skipped"])

    def test_owner_delete_removes_global_data_and_prevents_legacy_resurrection(self):
        uid = db.upsert_user(self.owner)
        storage.write_private_json(self.root / "garmin_raw_dump.json", {"synthetic":True})
        storage.write_private_json(web.TABLE_PATH, {"entries":[{"synthetic":True}]})
        (self.root / ".env").write_text("GARMIN_EMAIL=owner@example.test\nGARMIN_PASSWORD=synthetic-old-password\nOTHER_SETTING=keep\n")
        headers = self.headers(self.owner)
        self.assertEqual(self.client.post("/api/user/delete-account", headers=headers).status_code, 200)
        self.assertFalse((self.root / "garmin_raw_dump.json").exists())
        self.assertEqual(json.loads((self.root / "config.json").read_text())["garmin"], {})
        self.assertEqual((self.root / ".env").read_text(), "OTHER_SETTING=keep\n")
        web._migrate_legacy_to_sql()
        self.assertIsNone(db.get_user(uid))

    def test_extreme_race_date_rejected_before_summary_provider_or_allocation(self):
        db.set_goals(self.uid, {"race_type":"21K", "target_time_s":6000, "enabled":True, "race_date":"9999-12-31"})
        with patch.object(web, "_load_summary") as summary, patch.object(AICoach, "generate_week") as provider:
            response = self.client.post("/api/table/generate", headers=self.headers(), json={"duration":"race", "days":[0,2,4]})
        self.assertEqual(response.status_code, 400)
        summary.assert_not_called()
        provider.assert_not_called()

    def test_invalid_calendar_date_does_not_modify_goal(self):
        response = self.client.post("/api/goals", headers=self.headers(), json={"race_type":"21K","target_time_s":6000,"race_date":"2027-02-30"})
        self.assertEqual(response.status_code, 400)
        self.assertIsNone(db.get_goals(self.uid))

    def test_normal_week_generation_preserved_without_real_provider(self):
        def generate(_, dates, *args):
            return [{"nome":"Synthetic easy run","fasi":[]} for _ in dates]
        with patch.object(web,"_load_summary",return_value={"readiness":{"readiness_score":80}}), \
             patch.object(AICoach,"generate_week",autospec=True,side_effect=generate), \
             patch.object(AICoach,"validate_workout",return_value=(True,[])):
            response=self.client.post("/api/table/generate",headers=self.headers(),json={"duration":"1w","days":[0,2,4]})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(len(response.json()["table"]["entries"]),7)

    def test_provider_call_budget_covers_retries_and_fallbacks(self):
        coach=AICoach(config={"deepseek":{"api_key":"synthetic-test-key"}},summary={"readiness":{}})
        coach.provider_budget={"calls":1,"tokens":1000}
        with patch("ai_coach.httpx.post") as post:
            post.return_value.json.return_value={}
            coach._call_api([],0.3,500)
            with self.assertRaises(RuntimeError):coach._call_api([],0.3,500)
            self.assertEqual(post.call_count,1)

    def test_compact_ai_context_excludes_unneeded_identity_and_raw_data(self):
        coach=AICoach(config={"deepseek":{"api_key":"synthetic-test-key"}},summary={
            "atleta":{"nome":"Synthetic full name","email":"synthetic@example.test","fc_soglia":170},
            "raw_private_data":"synthetic-secret-field","readiness":{"readiness_score":80}})
        prompt=json.dumps(coach.build_prompt())
        self.assertNotIn("Synthetic full name",prompt)
        self.assertNotIn("synthetic@example.test",prompt)
        self.assertNotIn("synthetic-secret-field",prompt)
        self.assertIn("170",prompt)

    def test_telegram_default_closed_and_explicit_chat_allowed(self):
        import telegram_bot
        update=types.SimpleNamespace(effective_chat=types.SimpleNamespace(id=12345))
        for value in (None,[],["12345"],[True]):
            self.assertFalse(telegram_bot.is_allowed(update,{"telegram":{"allowed_chat_ids":value}}))
        self.assertTrue(telegram_bot.is_allowed(update,{"telegram":{"allowed_chat_ids":[12345]}}))
        self.assertFalse(telegram_bot.is_allowed(update,{"telegram":{"allowed_chat_ids":[67890]}}))

    def test_telegram_consent_gate_follows_current_owner_consent(self):
        import telegram_bot
        uid=db.upsert_user(self.owner)
        config={"garmin":{"email":self.owner}}
        self.assertFalse(telegram_bot._owner_has_consent(config))
        db.set_user_consent(uid,"1.1",True)
        self.assertTrue(telegram_bot._owner_has_consent(config))
        db.set_user_consent(uid,"1.1",False)
        self.assertFalse(telegram_bot._owner_has_consent(config))


if __name__ == "__main__":
    unittest.main()
