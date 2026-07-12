"""
Sprint 46S (audit M14) — HMAC-signed ML model artifacts.

The audit's exact complaint: "src/ml/pipeline.py:216. Hoy el artefacto
es local y generado por el bot (riesgo contenido), pero si un modelo
se comparte o alguien gana escritura al path, es ejecución arbitraria
de código. Migrar a skops/ONNX o proteger con verificación de
integridad." We took the integrity-verification branch: ModelTrainer.save()
now writes a `<path>.sig` HMAC-SHA256 sidecar, and ModelTrainer.load()
verifies it BEFORE calling pickle.loads(), rejecting (returning None)
on any mismatch instead of unpickling a tampered/substituted file.

Run: python -m unittest tests.test_sprint_46s_m14_model_signing -v
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.ml.pipeline import ModelTrainer, _sig_path, _get_ml_artifact_secret, _sign_artifact


def _make_trained_trainer():
    trainer = ModelTrainer(model_type="random_forest")
    X = pd.DataFrame({"f1": [0.1, 0.3, 0.5, 0.7], "f2": [0.2, 0.4, 0.6, 0.8]})
    y = pd.Series([0, 1, 0, 1])
    trainer.train(X, y)
    return trainer


class ModelSigningSaveTest(unittest.TestCase):
    def test_save_writes_sidecar_signature_file(self):
        trainer = _make_trained_trainer()
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
        try:
            trainer.save(path)
            self.assertTrue(os.path.exists(_sig_path(path)))
            with open(_sig_path(path), "r", encoding="utf-8") as f:
                sig = f.read().strip()
            # hex sha256 digest -> 64 hex chars
            self.assertEqual(len(sig), 64)
            int(sig, 16)  # raises ValueError if not valid hex
        finally:
            os.unlink(path)
            if os.path.exists(_sig_path(path)):
                os.unlink(_sig_path(path))

    def test_signature_matches_payload(self):
        trainer = _make_trained_trainer()
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
        try:
            trainer.save(path)
            with open(path, "rb") as f:
                payload = f.read()
            with open(_sig_path(path), "r", encoding="utf-8") as f:
                sig = f.read().strip()
            self.assertEqual(sig, _sign_artifact(payload))
        finally:
            os.unlink(path)
            if os.path.exists(_sig_path(path)):
                os.unlink(_sig_path(path))


class ModelSigningLoadTest(unittest.TestCase):
    def test_valid_signed_artifact_loads(self):
        trainer = _make_trained_trainer()
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
        try:
            trainer.save(path)
            loaded = ModelTrainer.load(path)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.model_type, "random_forest")
        finally:
            os.unlink(path)
            if os.path.exists(_sig_path(path)):
                os.unlink(_sig_path(path))

    def test_tampered_artifact_rejected(self):
        """If the .pkl bytes change after signing (tampering, or a
        substituted file), load() must refuse to unpickle and return
        None -- never execute pickle.loads() on unverified bytes."""
        trainer = _make_trained_trainer()
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
        try:
            trainer.save(path)
            # Tamper: append garbage bytes to the signed artifact.
            with open(path, "ab") as f:
                f.write(b"tampered-extra-bytes")
            loaded = ModelTrainer.load(path)
            self.assertIsNone(loaded, "Tampered artifact must be rejected, not loaded")
        finally:
            os.unlink(path)
            if os.path.exists(_sig_path(path)):
                os.unlink(_sig_path(path))

    def test_substituted_artifact_with_stale_sidecar_rejected(self):
        """A completely different (but validly-pickled) artifact placed
        at the same path, with the OLD signature sidecar left in place,
        must be rejected -- this is the exact attack the audit named
        ('si un modelo se comparte... es ejecución arbitraria de código')."""
        trainer_a = _make_trained_trainer()
        trainer_b = _make_trained_trainer()
        trainer_b.model_type = "logistic"  # make it distinguishably different

        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
        try:
            trainer_a.save(path)  # writes path + correct sidecar for trainer_a
            # Now substitute the .pkl with trainer_b's payload, but the
            # sidecar (signed for trainer_a's bytes) is left untouched.
            trainer_b.save(path)  # this OVERWRITES the sidecar too in our
            # implementation, so simulate the more realistic attack: an
            # attacker overwrites ONLY the .pkl, not the sidecar.
            # Re-sign path with trainer_a's original bytes' sidecar by
            # restoring a stale signature deliberately:
            import shutil
            with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f2:
                stale_path = f2.name
            trainer_a.save(stale_path)
            shutil.copyfile(_sig_path(stale_path), _sig_path(path))
            os.unlink(stale_path)
            os.unlink(_sig_path(stale_path))

            loaded = ModelTrainer.load(path)
            self.assertIsNone(loaded, "Substituted artifact with a stale sidecar must be rejected")
        finally:
            os.unlink(path)
            if os.path.exists(_sig_path(path)):
                os.unlink(_sig_path(path))

    def test_missing_sidecar_loads_as_unsigned_legacy_artifact(self):
        """Backward compatibility: an artifact saved before this fix
        shipped (no .sig sidecar) must still load, with a warning --
        upgrading to signed artifacts must not strand pre-existing
        models."""
        trainer = _make_trained_trainer()
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
        try:
            trainer.save(path)
            os.unlink(_sig_path(path))  # simulate a pre-M14 artifact
            loaded = ModelTrainer.load(path)
            self.assertIsNotNone(loaded, "Missing sidecar should warn-and-load, not reject")
            self.assertEqual(loaded.model_type, "random_forest")
        finally:
            os.unlink(path)

    def test_corrupt_sidecar_rejected(self):
        """A sidecar that exists but isn't a usable signature (e.g.
        itself corrupted) must fail closed, not silently skip
        verification."""
        trainer = _make_trained_trainer()
        with tempfile.NamedTemporaryFile(suffix=".pkl", delete=False) as f:
            path = f.name
        try:
            trainer.save(path)
            with open(_sig_path(path), "w", encoding="utf-8") as f:
                f.write("not-a-valid-hex-signature!!")
            loaded = ModelTrainer.load(path)
            self.assertIsNone(loaded, "Corrupt sidecar must fail closed")
        finally:
            os.unlink(path)
            if os.path.exists(_sig_path(path)):
                os.unlink(_sig_path(path))


class ModelSigningSecretTest(unittest.TestCase):
    def test_env_secret_used_when_set(self):
        with patch.dict(os.environ, {"ML_ARTIFACT_SIGNING_SECRET": "test-secret-value"}):
            secret = _get_ml_artifact_secret()
            self.assertEqual(secret, b"test-secret-value")

    def test_secret_persisted_to_file_when_no_env(self):
        tmpdir = tempfile.mkdtemp()
        secret_path = os.path.join(tmpdir, "ml_secret.key")
        with patch.dict(os.environ, {"ML_ARTIFACT_SIGNING_SECRET_FILE": secret_path}, clear=False):
            os.environ.pop("ML_ARTIFACT_SIGNING_SECRET", None)
            secret1 = _get_ml_artifact_secret()
            self.assertTrue(os.path.exists(secret_path))
            # Second call must return the SAME persisted secret, not a
            # freshly-generated one -- otherwise every process restart
            # would invalidate every previously-signed artifact.
            secret2 = _get_ml_artifact_secret()
            self.assertEqual(secret1, secret2)


if __name__ == "__main__":
    unittest.main()
