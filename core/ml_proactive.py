"""ML-based proactive engine: anomaly detection, pattern mining, prediction.

Contract: this is an aux subsystem layered on top of `proactive.py`. Without
sklearn every trainer/predictor fail-softs (False/None/[]) and fitted-state
flags stay honest. Model persistence is fail-soft but LOUD: corrupt or
partial model pickles are reported on console, never silently trusted —
a pickle missing a key leaves the default (unfitted) component in place.
Training/inference failures are printed, not swallowed.
"""

from __future__ import annotations

import asyncio
import pickle
import time
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

try:
    import numpy as np
    from sklearn.cluster import DBSCAN
    from sklearn.ensemble import IsolationForest, RandomForestClassifier
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.preprocessing import StandardScaler

    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    np = None

from .config import Config
from .memory import MemoryStore
from .proactive import ProactiveEngine, Suggestion


class SuggestionType(Enum):
    HABIT = "habit"
    ROUTINE = "routine"
    ANOMALY = "anomaly"
    PREDICTION = "prediction"
    AUTOMATION = "automation"
    REMINDER = "reminder"
    OPTIMIZATION = "optimization"


@dataclass
class MLFeatureVector:
    timestamp: float
    hour_of_day: int
    day_of_week: int
    action_type: str
    tool_used: str
    duration_ms: int
    success: bool
    context: dict = field(default_factory=dict)

    def to_array(self) -> list:
        # Stable hash (crc32), NOT built-in hash(): hash() is salted per
        # process, which would silently invalidate persisted models on restart.
        import zlib

        return [
            self.hour_of_day / 24.0,
            self.day_of_week / 7.0,
            zlib.crc32(self.action_type.encode()) % 1000 / 1000.0,
            zlib.crc32(self.tool_used.encode()) % 1000 / 1000.0,
            min(self.duration_ms / 60000.0, 1.0),
            1.0 if self.success else 0.0,
        ]


@dataclass
class UserPattern:
    pattern_id: str
    pattern_type: str
    name: str
    description: str
    confidence: float
    frequency: int
    last_seen: float
    features: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)


@dataclass
class AnomalyScore:
    score: float
    is_anomaly: bool
    feature_contributions: dict
    timestamp: float
    context: dict


class BehaviorAnalyzer:
    def __init__(self, memory: MemoryStore, config: Config):
        self.memory = memory
        self.config = config
        self.action_history: deque = deque(maxlen=10000)
        self.patterns: dict[str, UserPattern] = {}
        self.anomaly_detector: IsolationForest | None = None
        # Guarded: without sklearn these classes don't exist (import fell through).
        self.scaler = StandardScaler() if SKLEARN_AVAILABLE else None
        self.is_fitted = False
        self._feature_cache: dict[str, np.ndarray] = {}
        self._last_training = 0
        self._training_interval = 3600
        self.min_samples_for_training = 50
        self._sequences: list = []

    def record_action(
        self,
        action_type: str,
        tool_used: str,
        duration_ms: int,
        success: bool,
        context: dict = None,
    ):
        vector = MLFeatureVector(
            timestamp=time.time(),
            hour_of_day=datetime.now().hour,
            day_of_week=datetime.now().weekday(),
            action_type=action_type,
            tool_used=tool_used,
            duration_ms=duration_ms,
            success=success,
            context=context or {},
        )
        self.action_history.append(vector)
        self._invalidate_cache()

    def _invalidate_cache(self):
        self._feature_cache.clear()
        self._last_training = 0

    def _get_training_data(self) -> np.ndarray | None:
        if len(self.action_history) < self.min_samples_for_training:
            return None

        vectors = [v.to_array() for v in self.action_history]
        return np.array(vectors)

    async def train_anomaly_detector(self):
        if not SKLEARN_AVAILABLE:
            return False

        data = self._get_training_data()
        if data is None:
            return False

        try:
            scaled_data = self.scaler.fit_transform(data)
            self.anomaly_detector = IsolationForest(
                contamination=0.1,
                random_state=42,
                n_estimators=100,
            )
            self.anomaly_detector.fit(scaled_data)
            self.is_fitted = True
            self._last_training = time.time()
            return True
        except Exception as e:
            print(f"[ML Proactive] Training failed: {e}")
            return False

    def detect_anomaly(self, vector: MLFeatureVector) -> AnomalyScore | None:
        if not SKLEARN_AVAILABLE or not self.is_fitted or self.anomaly_detector is None:
            return None

        try:
            arr = np.array([vector.to_array()])
            scaled = self.scaler.transform(arr)
            score = self.anomaly_detector.decision_function(scaled)[0]
            is_anomaly = self.anomaly_detector.predict(scaled)[0] == -1

            feature_names = [
                "hour_of_day",
                "day_of_week",
                "action_type",
                "tool_used",
                "duration",
                "success",
            ]
            contributions = dict(zip(feature_names, arr[0], strict=True))

            return AnomalyScore(
                score=float(score),
                is_anomaly=bool(is_anomaly),
                feature_contributions=contributions,
                timestamp=time.time(),
                context={"vector": vector.to_array()},
            )
        except Exception as e:
            # Silent-None here hid broken scaler/model state after a bad pickle load.
            print(f"[ML Proactive] Anomaly detection failed: {e}")
            return None

    def extract_patterns(self, min_frequency: int = 3) -> list[UserPattern]:
        if not SKLEARN_AVAILABLE or len(self.action_history) < min_frequency:
            return []

        sequences = self._extract_sequences()
        if len(sequences) < min_frequency:
            return []

        vectors = []
        labels = []
        for seq in sequences:
            if len(seq) >= 2:
                for i in range(len(seq) - 1):
                    vectors.append(seq[i].to_array() + seq[i + 1].to_array())
                    labels.append(f"{seq[i].action_type}->{seq[i + 1].action_type}")

        if len(vectors) < min_frequency:
            return []

        try:
            X = np.array(vectors)
            clustering = DBSCAN(eps=0.3, min_samples=min_frequency)
            clusters = clustering.fit_predict(X)

            patterns = []
            for cluster_id in set(clusters):
                if cluster_id == -1:
                    continue
                cluster_mask = clusters == cluster_id
                cluster_vectors = X[cluster_mask]
                cluster_labels = [labels[i] for i, m in enumerate(cluster_mask) if m]

                if len(cluster_vectors) >= min_frequency:
                    centroid = np.mean(cluster_vectors, axis=0)
                    pattern = UserPattern(
                        pattern_id=f"pattern_{uuid.uuid4().hex[:8]}",
                        pattern_type="sequence",
                        name=f"Pattern {len(self.patterns) + 1}",
                        description=f"Recurring sequence: {max(set(cluster_labels), key=cluster_labels.count)}",
                        confidence=len(cluster_vectors) / len(vectors),
                        frequency=len(cluster_vectors),
                        last_seen=time.time(),
                        features={"centroid": centroid.tolist()},
                    )
                    patterns.append(pattern)
                    self.patterns[pattern.pattern_id] = pattern

            return patterns
        except Exception as e:
            print(f"[ML Proactive] Pattern extraction failed: {e}")
            return []

    def _extract_sequences(self) -> list[list[MLFeatureVector]]:
        # Rebuild from scratch each call — the old version appended into
        # self._sequences and returned it, so every extract_patterns() call
        # re-added the same sequences and the list grew without bound.
        sequences = []
        current_seq = []
        last_time = 0

        for action in self.action_history:
            if last_time == 0 or action.timestamp - last_time < 300:
                current_seq.append(action)
            else:
                if len(current_seq) >= 2:
                    sequences.append(current_seq)
                current_seq = [action]
            last_time = action.timestamp

        if len(current_seq) >= 2:
            sequences.append(current_seq)

        self._sequences = sequences
        return sequences


class PredictionEngine:
    def __init__(self, behavior_analyzer: BehaviorAnalyzer):
        self.behavior_analyzer = behavior_analyzer
        self.time_series_model: RandomForestClassifier | None = None
        self.vectorizer = TfidfVectorizer(max_features=100) if SKLEARN_AVAILABLE else None
        self.is_fitted = False
        self._last_training = 0
        self._training_interval = 1800

    def prepare_training_data(self) -> tuple:
        if len(self.behavior_analyzer.action_history) < 20:
            return None, None

        actions = list(self.behavior_analyzer.action_history)
        X = []
        y = []

        for i in range(len(actions) - 1):
            current = actions[i]
            next_action = actions[i + 1]

            features = current.to_array()
            features.append(next_action.hour_of_day / 24.0)
            features.append(next_action.day_of_week / 7.0)

            X.append(features)
            y.append(next_action.action_type)

        return np.array(X), np.array(y)

    def train(self) -> bool:
        if not SKLEARN_AVAILABLE:
            return False

        data = self.prepare_training_data()
        if data is None or data[0] is None:
            return False

        X, y = data
        if len(set(y)) < 2:
            return False

        try:
            self.time_series_model = RandomForestClassifier(
                n_estimators=50,
                max_depth=10,
                random_state=42,
            )
            self.time_series_model.fit(X, y)
            self.is_fitted = True
            self._last_training = time.time()
            return True
        except Exception as e:
            print(f"[Prediction Engine] Training failed: {e}")
            return False

    def predict_next_action(
        self, current_vector: MLFeatureVector, hour: int = None, day: int = None
    ) -> dict | None:
        if not self.is_fitted or self.time_series_model is None:
            return None

        try:
            hour = hour or datetime.now().hour
            day = day or datetime.now().weekday()

            features = current_vector.to_array()
            features.append(hour / 24.0)
            features.append(day / 7.0)

            X = np.array([features])
            prediction = self.time_series_model.predict(X)[0]
            probabilities = self.time_series_model.predict_proba(X)[0]
            classes = self.time_series_model.classes_

            top_3_idx = np.argsort(probabilities)[-3:][::-1]
            predictions = [
                {"action": classes[i], "probability": float(probabilities[i])} for i in top_3_idx
            ]

            return {
                "predicted_action": prediction,
                "confidence": float(max(probabilities)),
                "alternatives": predictions,
            }
        except Exception as e:
            print(f"[Prediction Engine] Prediction failed: {e}")
            return None

    def predict_tool_usage(self, context: dict) -> dict | None:
        if not self.is_fitted or self.time_series_model is None:
            return None
        return self.predict_next_action(
            MLFeatureVector(
                timestamp=time.time(),
                hour_of_day=datetime.now().hour,
                day_of_week=datetime.now().weekday(),
                action_type=context.get("action_type", "unknown"),
                tool_used=context.get("tool", "unknown"),
                duration_ms=0,
                success=True,
            )
        )


class MLProactiveEngine:
    def __init__(self, config: Config, memory: MemoryStore):
        self.config = config
        self.memory = memory
        self.behavior_analyzer = BehaviorAnalyzer(memory, config)
        self.prediction_engine = PredictionEngine(self.behavior_analyzer)
        self.suggestion_engine = ProactiveEngine(config, memory)

        self.is_running = False
        self._tasks: list[asyncio.Task] = []
        self._model_path = Path(config.db_path.parent / "ml_models")
        self._model_path.mkdir(parents=True, exist_ok=True)

        self._load_models()
        self._register_default_templates()

    def _load_models(self):
        model_file = self._model_path / "models.pkl"
        if model_file.exists():
            try:
                with open(model_file, "rb") as f:
                    data = pickle.load(f)
                # Only adopt components that actually unpickled — a partial
                # pickle must not replace a healthy default with None (which
                # would crash the scaler mid-detect later).
                detector = data.get("anomaly_detector")
                scaler = data.get("scaler")
                if detector is not None and scaler is not None:
                    self.behavior_analyzer.anomaly_detector = detector
                    self.behavior_analyzer.scaler = scaler
                    self.behavior_analyzer.is_fitted = bool(data.get("is_fitted", False))
                elif data.get("is_fitted"):
                    print(
                        "[ML Proactive] Model file claims fitted but is missing detector/scaler; ignoring"
                    )
                predictor = data.get("time_series_model")
                if predictor is not None:
                    self.prediction_engine.time_series_model = predictor
                    self.prediction_engine.is_fitted = bool(data.get("prediction_fitted", False))
                self.behavior_analyzer.patterns = data.get("patterns") or {}
                print("[ML Proactive] Models loaded from disk")
            except Exception as e:
                print(f"[ML Proactive] Failed to load models: {e}")

    def _save_models(self):
        model_file = self._model_path / "models.pkl"
        try:
            data = {
                "anomaly_detector": self.behavior_analyzer.anomaly_detector,
                "scaler": self.behavior_analyzer.scaler,
                "is_fitted": self.behavior_analyzer.is_fitted,
                "time_series_model": self.prediction_engine.time_series_model,
                "prediction_fitted": self.prediction_engine.is_fitted,
                "patterns": self.behavior_analyzer.patterns,
                "saved_at": time.time(),
            }
            with open(model_file, "wb") as f:
                pickle.dump(data, f)
        except Exception as e:
            print(f"[ML Proactive] Failed to save models: {e}")

    def _register_default_templates(self):
        self.templates = {
            "habit_reminder": "You usually {action} around {time}. Want me to set a reminder?",
            "anomaly_alert": "Unusual activity detected: {action} at {time}. Everything okay?",
            "prediction_suggestion": "Based on your patterns, you might want to {action} soon.",
            "routine_optimization": "I noticed you {pattern}. Want me to automate this?",
            "anomaly_followup": "That was unusual ({action}). Should I log this or create a reminder?",
        }

    async def start(self):
        self.is_running = True
        self._tasks = [
            asyncio.create_task(self._training_loop()),
            asyncio.create_task(self._suggestion_loop()),
            asyncio.create_task(self._cleanup_loop()),
        ]
        await self._initial_training()

    async def _initial_training(self):
        await self.behavior_analyzer.train_anomaly_detector()
        await asyncio.to_thread(self.prediction_engine.train)

    async def _training_loop(self):
        while self.is_running:
            try:
                await asyncio.sleep(3600)
                if self.is_running:
                    await self.behavior_analyzer.train_anomaly_detector()
                    await asyncio.to_thread(self.prediction_engine.train)
                    self._save_models()
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[ML Proactive] Training loop error: {e}")

    async def _suggestion_loop(self):
        while self.is_running:
            try:
                await asyncio.sleep(300)
                if not self.is_running:
                    break
            except asyncio.CancelledError:
                break

    async def _cleanup_loop(self):
        while self.is_running:
            await asyncio.sleep(3600)
            if not self.is_running:
                break
            self._cleanup_old_data()

    def _cleanup_old_data(self):
        cutoff = time.time() - 2592000
        self.behavior_analyzer.action_history = deque(
            [a for a in self.behavior_analyzer.action_history if a.timestamp > cutoff],
            maxlen=10000,
        )
        self._save_models()

    def record_action(
        self,
        action_type: str,
        tool_used: str,
        duration_ms: int,
        success: bool,
        context: dict = None,
    ):
        self.behavior_analyzer.record_action(action_type, tool_used, duration_ms, success, context)

    def get_suggestions(self, context: dict = None) -> list[Suggestion]:
        ctx = context or {}
        suggestions = []

        anomaly = self.behavior_analyzer.detect_anomaly(
            MLFeatureVector(
                timestamp=time.time(),
                hour_of_day=datetime.now().hour,
                day_of_week=datetime.now().weekday(),
                action_type=ctx.get("action_type", "unknown"),
                tool_used=ctx.get("tool", "unknown"),
                duration_ms=0,
                success=True,
            )
        )

        if anomaly and anomaly.is_anomaly:
            suggestions.append(
                Suggestion(
                    id=f"anomaly_{uuid.uuid4().hex[:8]}",
                    type=SuggestionType.ANOMALY,
                    title="Unusual Activity Detected",
                    # ctx (not raw context): get_suggestions(None) must not crash here.
                    description=f"Unusual activity: {ctx.get('action_type', 'unknown')} at {datetime.now().strftime('%H:%M')}",
                    confidence=min(0.9, 1.0 - anomaly.score),
                    action="notify_alert",
                    action_args={
                        "message": "Unusual activity detected",
                        "title": "Anomaly Detected",
                    },
                )
            )

        prediction = self.prediction_engine.predict_next_action(
            MLFeatureVector(
                timestamp=time.time(),
                hour_of_day=datetime.now().hour,
                day_of_week=datetime.now().weekday(),
                action_type="idle",
                tool_used="none",
                duration_ms=0,
                success=True,
            )
        )

        if prediction and prediction["confidence"] > 0.7:
            suggestions.append(
                Suggestion(
                    id=f"prediction_{uuid.uuid4().hex[:8]}",
                    type=SuggestionType.PREDICTION,
                    title="Predicted Action",
                    description=f"You might want to {prediction['predicted_action']} soon (confidence: {prediction['confidence']:.0%})",
                    confidence=prediction["confidence"],
                    action="notify",
                    action_args={"message": f"Predicted: {prediction['predicted_action']}"},
                )
            )

        patterns = self.behavior_analyzer.extract_patterns()
        for pattern in patterns[:2]:
            suggestions.append(
                Suggestion(
                    id=f"pattern_{pattern.pattern_id}",
                    type=SuggestionType.HABIT,
                    title=f"Detected Pattern: {pattern.name}",
                    description=pattern.description,
                    confidence=pattern.confidence,
                    action="notify",
                    action_args={"message": f"Pattern detected: {pattern.description}"},
                )
            )

        return suggestions

    async def stop(self):
        self.is_running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        self._save_models()

    def get_stats(self) -> dict:
        return {
            "is_running": self.is_running,
            "actions_recorded": len(self.behavior_analyzer.action_history),
            "patterns_found": len(self.behavior_analyzer.patterns),
            "anomaly_fitted": self.behavior_analyzer.is_fitted,
            "prediction_fitted": self.prediction_engine.is_fitted,
            "models_saved": self._model_path.exists(),
        }
