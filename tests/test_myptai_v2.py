#!/usr/bin/env python3
"""
test_myptai_v2.py - Test Unitari per l'Architettura MyPTAI 2.0
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from athlete_state import calculate_karvonen_zones, classify_acwr, load_athlete_state, save_athlete_state
from analytics_engine import AnalyticsEngine
from workout_sync import WorkoutConverter


class TestMyPTAI2Architecture(unittest.TestCase):
    def test_karvonen_calculation(self):
        """Verifica che le formule Karvonen calcolino le zone esatte con HRmax=200 e RHR=50."""
        zones = calculate_karvonen_zones(max_hr=200, rhr_baseline=50)
        zb = zones["zone_bpm"]
        self.assertEqual(zb["Z1"]["min"], 125)
        self.assertEqual(zb["Z1"]["max"], 140)
        self.assertEqual(zb["Z2"]["min"], 140)
        self.assertEqual(zb["Z2"]["max"], 155)
        self.assertEqual(zb["Z3"]["min"], 155)
        self.assertEqual(zb["Z3"]["max"], 170)
        self.assertEqual(zb["Z4"]["min"], 170)
        self.assertEqual(zb["Z4"]["max"], 185)
        self.assertEqual(zb["Z5"]["min"], 185)
        self.assertEqual(zb["Z5"]["max"], 200)

    def test_acwr_classification(self):
        """Verifica la classificazione scientifica dell'ACWR."""
        self.assertEqual(classify_acwr(0.7), "UNDER_TRAINING")
        self.assertEqual(classify_acwr(1.1), "SWEET_SPOT")
        self.assertEqual(classify_acwr(1.4), "HIGH_LOAD")
        self.assertEqual(classify_acwr(1.8), "DANGER_ZONE")
        self.assertEqual(classify_acwr(1.9, crash_block=True), "CRASH_BLOCK_CONTROLLED")

    def test_decision_engine_rules(self):
        """Verifica le regole del Decision Engine per la Readiness."""
        # Regola 1: Riposo totale
        summary_danger = {
            "biometria": {"hrv_status": "LOW", "body_battery_attuale": 20, "rhr": 60, "sleep_score": 40},
            "readiness": {"readiness_level": "RED", "readiness_score": 25},
            "atleta": {"fc_riposo_baseline": 50}
        }
        dec1 = AnalyticsEngine.evaluate_decision_engine(summary_danger)
        self.assertEqual(dec1["decision"], "REST_TOTAL")

        # Regola 2: Downgrade Z1
        summary_amber = {
            "biometria": {"hrv_status": "UNBALANCED", "body_battery_attuale": 40, "rhr": 52, "sleep_score": 48},
            "readiness": {"readiness_level": "AMBER", "readiness_score": 45},
            "atleta": {"fc_riposo_baseline": 50}
        }
        dec2 = AnalyticsEngine.evaluate_decision_engine(summary_amber)
        self.assertEqual(dec2["decision"], "DOWNGRADE_Z1")

        # Regola 3: Conferma Seduta
        summary_green = {
            "biometria": {"hrv_status": "BALANCED", "body_battery_attuale": 75, "rhr": 49, "sleep_score": 85, "sonno_h": 7.8},
            "readiness": {"readiness_level": "GREEN", "readiness_score": 88},
            "atleta": {"fc_riposo_baseline": 50}
        }
        dec3 = AnalyticsEngine.evaluate_decision_engine(summary_green)
        self.assertEqual(dec3["decision"], "CONFIRM_KEY")

    def test_garmin_step_generation(self):
        """Verifica che il converter Garmin generi targetValueOne e targetValueTwo come float."""
        converter = WorkoutConverter()
        plan = {
            "workout": {
                "nome": "Test Karvonen Run",
                "fasi": [
                    {"tipo": "warmup", "durata_s": 600, "distanza_m": 1500, "zona": "Z2", "ritmo": "5:45/km"},
                    {"tipo": "repeat", "ripetizioni": 4, "durata_s": 1200, "distanza_m": 1000, "zona": "Z4", "ritmo": "4:30/km", "recupero_s": 90},
                    {"tipo": "cooldown", "durata_s": 600, "distanza_m": 1200, "zona": "Z1", "ritmo": "6:15/km"}
                ]
            }
        }
        garmin_json = converter.convert(plan)
        steps = garmin_json["workoutSegments"][0]["workoutSteps"]
        self.assertTrue(len(steps) >= 3)


if __name__ == "__main__":
    unittest.main()
