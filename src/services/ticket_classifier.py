"""
Ticket Classifier Service — wraps the trained TF-IDF/SGD model for support group prediction.

Loads the pickled model from ticket_classifier/improved_classifier.pkl and provides
a simple predict() interface that returns top-N support group predictions with
confidence scores.

Model architecture:
- TF-IDF vectorizer (50,000 features, bigrams, sublinear TF)
- One-hot encoded categorical features (TicketType, Location, Classification, Source)
- SGDClassifier with modified_huber loss (226 support group classes)
- Total feature dimension: 51,050
"""

import logging
import pickle
from pathlib import Path
from typing import Any

import numpy as np
from scipy.sparse import hstack, csr_matrix

logger = logging.getLogger(__name__)

# Path to the trained model
_MODEL_PATH = Path(__file__).resolve().parent.parent.parent / "ticket_classifier" / "improved_classifier.pkl"


class TicketClassifier:
    """
    Wrapper around the trained TF-IDF + SGDClassifier pipeline.

    The model expects:
    - text: combined Title + Description (TF-IDF vectorized)
    - categorical features: TicketType, Location, Classification, Source (one-hot encoded)

    It returns support group predictions with confidence scores.
    """

    def __init__(self, model_path: Path = _MODEL_PATH) -> None:
        """Load the pickled model components."""
        logger.info(f"Loading ticket classifier from {model_path}")
        with open(model_path, "rb") as f:
            model_data = pickle.load(f)

        self._vectorizer = model_data["tfidf_vectorizer"]
        self._classifier = model_data["classifier"]
        self._label_encoder = model_data["label_encoder"]
        self._cat_encoders: dict = model_data["cat_encoders"]  # {col_name: LabelEncoder}
        self._cat_features: list[str] = model_data["cat_features"]  # ordered column names

        self._classes = self._label_encoder.classes_
        logger.info(
            f"Classifier loaded: {len(self._classes)} support groups, "
            f"categorical features: {self._cat_features}"
        )

    @property
    def support_groups(self) -> list[str]:
        """Return the list of all support groups the classifier knows about."""
        return list(self._classes)

    def predict(
        self,
        title: str = "",
        description: str = "",
        ticket_type: str = "",
        location: str = "",
        classification: str = "",
        source: str = "",
        top_k: int = 5,
    ) -> list[dict[str, Any]]:
        """
        Predict the top-K support groups for a ticket.

        Args:
            title: Ticket title/short description.
            description: Ticket description/body.
            ticket_type: "Incident" or "Service Request".
            location: Site/location (e.g., "HUP", "CAMPUS").
            classification: Classification/Area category.
            source: Ticket source (e.g., "Phone", "Email").
            top_k: Number of top predictions to return.

        Returns:
            List of dicts with 'support_group' and 'confidence' keys,
            sorted by confidence descending.
        """
        # Build text feature (same as training: Title + " " + Description)
        text = f"{title} {description}".strip()
        if not text:
            text = "unknown"

        # TF-IDF transform → (1, 50000) sparse matrix
        text_features = self._vectorizer.transform([text])

        # Build one-hot categorical features
        cat_features = self._encode_categorical(
            ticket_type=ticket_type,
            location=location,
            classification=classification,
            source=source,
        )

        # Combine: [TF-IDF | one-hot categoricals] → (1, 51050)
        features = hstack([text_features, cat_features])

        # Get probability estimates via decision_function + softmax
        # SGDClassifier with modified_huber supports predict_proba
        probabilities = self._classifier.predict_proba(features)[0]

        # Get top-K indices sorted by probability
        top_indices = np.argsort(probabilities)[::-1][:top_k]

        results = []
        for idx in top_indices:
            results.append({
                "support_group": self._classes[idx],
                "confidence": float(probabilities[idx]),
            })

        return results

    def _encode_categorical(
        self,
        ticket_type: str = "",
        location: str = "",
        classification: str = "",
        source: str = "",
    ) -> csr_matrix:
        """
        One-hot encode categorical features to match training format.

        Each LabelEncoder maps a value to an integer index.
        We then create a one-hot vector of size len(encoder.classes_).
        Unknown values get an all-zeros vector (no category activated).
        """
        # Map field names (matching cat_features order) to values
        field_map = {
            "TicketType": ticket_type or "",
            "Location": location or "",
            "Classification": classification or "",
            "Source": source or "",
        }

        encoded_parts = []
        for col in self._cat_features:
            encoder = self._cat_encoders[col]
            n_classes = len(encoder.classes_)
            value = field_map.get(col, "")

            # Create one-hot vector
            vec = np.zeros(n_classes)
            if value and value in encoder.classes_:
                idx = encoder.transform([value])[0]
                vec[idx] = 1.0

            encoded_parts.append(csr_matrix(vec.reshape(1, -1)))

        return hstack(encoded_parts)


# ── Singleton Instance ────────────────────────────────────────────────

_classifier_instance: TicketClassifier | None = None


def get_ticket_classifier() -> TicketClassifier:
    """Get or create the singleton classifier instance."""
    global _classifier_instance
    if _classifier_instance is None:
        _classifier_instance = TicketClassifier()
    return _classifier_instance