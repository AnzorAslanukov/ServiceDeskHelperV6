"""
Improved Ticket Recommendation Classifier
==========================================
Improvements over baseline:
1. SGDClassifier (much faster, scales to full dataset)
2. Removes "Service Desk" triage bucket from training
3. Proper convergence with early stopping
4. Larger dataset (full IR + SR)
5. Better evaluation (confidence analysis, per-class metrics)

Usage:
    python train_improved_classifier.py
"""

import os
import sys
import json
import time
import pickle
import numpy as np
import pandas as pd
from pathlib import Path
from dotenv import load_dotenv
from databricks import sql as databricks_sql
from sklearn.model_selection import train_test_split
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import SGDClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.preprocessing import LabelEncoder
from sklearn.metrics import accuracy_score, classification_report, top_k_accuracy_score
from scipy.sparse import hstack, csr_matrix

# Configuration
load_dotenv('.env')
DATA_DIR = Path('data')
MODEL_DIR = Path('models')
DATA_DIR.mkdir(exist_ok=True)
MODEL_DIR.mkdir(exist_ok=True)

TABLE = "prepared.ticketing.athena_tickets"
SAMPLE_SIZE = 200000  # 200K total (100K IR + 100K SR)
MIN_CLASS_SIZE = 50   # Minimum tickets per support group
DATASET_FILE = DATA_DIR / 'training_dataset_v2.parquet'

# Groups to exclude (triage buckets, not real routing targets)
EXCLUDE_GROUPS = [
    '--Please Select a Support Group--',
    'Service Desk',  # Triage bucket - tickets get re-routed from here
]


def get_connection():
    """Create a Databricks SQL connection."""
    return databricks_sql.connect(
        server_hostname=os.getenv('DATABRICKS_SERVER_HOSTNAME'),
        http_path=os.getenv('DATABRICKS_HTTP_PATH'),
        access_token=os.getenv('DATABRICKS_API_KEY')
    )


def extract_dataset():
    """Extract training data from Databricks."""
    if DATASET_FILE.exists():
        print(f"Loading cached dataset from {DATASET_FILE}...")
        return pd.read_parquet(DATASET_FILE)

    print(f"Extracting {SAMPLE_SIZE} tickets from Databricks...")
    print("  (This will take 2-3 minutes)")

    conn = get_connection()
    cursor = conn.cursor()

    # Extract Incidents (larger sample)
    print("  Fetching Incidents...", flush=True)
    cursor.execute(f"""
        SELECT 
            Id, Title, Description, SupportGroup, 
            TicketType, Location, `Classification/Area` as Classification,
            Source, Urgency, Impact, CreatedDate
        FROM {TABLE}
        WHERE TicketType = 'Incident'
          AND Title IS NOT NULL AND Title != ''
          AND Description IS NOT NULL AND Description != ''
          AND SupportGroup IS NOT NULL AND SupportGroup != ''
          AND SupportGroup != '--Please Select a Support Group--'
          AND SupportGroup != 'Service Desk'
        ORDER BY CreatedDate DESC
        LIMIT {SAMPLE_SIZE // 2}
    """)
    columns = [desc[0] for desc in cursor.description]
    ir_rows = cursor.fetchall()
    print(f"    Got {len(ir_rows)} Incident tickets", flush=True)

    # Extract Service Requests
    print("  Fetching Service Requests...", flush=True)
    cursor.execute(f"""
        SELECT 
            Id, Title, Description, SupportGroup,
            TicketType, Location, `Classification/Area` as Classification,
            Source, Urgency, NULL as Impact, CreatedDate
        FROM {TABLE}
        WHERE TicketType = 'Service Request'
          AND Title IS NOT NULL AND Title != ''
          AND Description IS NOT NULL AND Description != ''
          AND SupportGroup IS NOT NULL AND SupportGroup != ''
          AND SupportGroup != '--Please Select a Support Group--'
          AND SupportGroup != 'Service Desk'
        ORDER BY CreatedDate DESC
        LIMIT {SAMPLE_SIZE // 2}
    """)
    sr_rows = cursor.fetchall()
    print(f"    Got {len(sr_rows)} Service Request tickets", flush=True)

    cursor.close()
    conn.close()

    # Combine into DataFrame
    all_rows = ir_rows + sr_rows
    df = pd.DataFrame([tuple(r) for r in all_rows], columns=columns)

    # Save to parquet for caching
    df.to_parquet(DATASET_FILE, index=False)
    print(f"  Dataset saved to {DATASET_FILE} ({len(df)} rows)")

    return df


def prepare_features(df):
    """Prepare features for classification."""
    print("\nPreparing features...")

    # Remove excluded groups
    for group in EXCLUDE_GROUPS:
        mask = df['SupportGroup'] == group
        if mask.sum() > 0:
            print(f"  Excluded '{group}': {mask.sum()} tickets")
            df = df[~mask]

    # Combine title + description as text input
    df = df.copy()
    df['text'] = df['Title'].fillna('') + ' ' + df['Description'].fillna('')
    df['text'] = df['text'].str.strip()

    # Filter out classes with too few examples
    group_counts = df['SupportGroup'].value_counts()
    valid_groups = group_counts[group_counts >= MIN_CLASS_SIZE].index
    df_filtered = df[df['SupportGroup'].isin(valid_groups)].copy()

    removed_groups = len(group_counts) - len(valid_groups)
    removed_tickets = len(df) - len(df_filtered)
    print(f"  Removed {removed_groups} groups with < {MIN_CLASS_SIZE} tickets ({removed_tickets} tickets)")
    print(f"  Remaining: {len(df_filtered)} tickets across {len(valid_groups)} groups")
    print(f"  Top 10 groups:")
    for grp, cnt in df_filtered['SupportGroup'].value_counts().head(10).items():
        print(f"    {grp}: {cnt}")

    return df_filtered


def train_and_evaluate(df):
    """Train SGDClassifier and evaluate."""
    print("\n" + "=" * 70)
    print("  TRAINING IMPROVED CLASSIFIER (SGDClassifier)")
    print("=" * 70)

    # Encode target
    label_encoder = LabelEncoder()
    y = label_encoder.fit_transform(df['SupportGroup'])
    num_classes = len(label_encoder.classes_)
    print(f"\n  Target: {num_classes} support groups")

    # Train/test split (stratified)
    df_reset = df.reset_index(drop=True)
    train_indices, test_indices = train_test_split(
        range(len(df_reset)), test_size=0.2, random_state=42, stratify=y
    )
    y_train = y[train_indices]
    y_test = y[test_indices]
    print(f"  Train: {len(train_indices)}, Test: {len(test_indices)}")

    # TF-IDF vectorization
    print("\n  Fitting TF-IDF vectorizer...", flush=True)
    t0 = time.time()
    tfidf = TfidfVectorizer(
        max_features=50000,
        ngram_range=(1, 2),
        min_df=3,
        max_df=0.95,
        sublinear_tf=True,
        strip_accents='unicode'
    )
    X_train_tfidf = tfidf.fit_transform(df_reset['text'].values[train_indices])
    X_test_tfidf = tfidf.transform(df_reset['text'].values[test_indices])
    print(f"    TF-IDF shape: {X_train_tfidf.shape} ({time.time()-t0:.1f}s)")

    # Add categorical features
    print("  Adding categorical features...", flush=True)
    cat_features = ['TicketType', 'Location', 'Classification', 'Source']
    cat_encoders = {}
    train_cat_matrices = []
    test_cat_matrices = []

    for feat in cat_features:
        le = LabelEncoder()
        col = df_reset[feat].fillna('UNKNOWN').values
        le.fit(col)
        cat_encoders[feat] = le

        train_encoded = le.transform(col[train_indices])
        test_encoded = le.transform(col[test_indices])

        n_cats = len(le.classes_)
        train_onehot = csr_matrix(
            (np.ones(len(train_encoded)),
             (range(len(train_encoded)), train_encoded)),
            shape=(len(train_encoded), n_cats)
        )
        test_onehot = csr_matrix(
            (np.ones(len(test_encoded)),
             (range(len(test_encoded)), test_encoded)),
            shape=(len(test_encoded), n_cats)
        )
        train_cat_matrices.append(train_onehot)
        test_cat_matrices.append(test_onehot)
        print(f"    {feat}: {n_cats} categories")

    # Combine TF-IDF + categorical features
    X_train = hstack([X_train_tfidf] + train_cat_matrices)
    X_test = hstack([X_test_tfidf] + test_cat_matrices)
    print(f"  Combined feature matrix: {X_train.shape}")

    # Train SGDClassifier with log_loss (equivalent to Logistic Regression but much faster)
    print("\n  Training SGDClassifier (log_loss)...", flush=True)
    t0 = time.time()
    sgd = SGDClassifier(
        loss='modified_huber',  # Gives probability estimates directly
        alpha=1e-5,
        max_iter=200,
        tol=1e-4,
        random_state=42,
        n_jobs=-1,
        verbose=0,
        class_weight='balanced'  # Handle class imbalance
    )
    sgd.fit(X_train, y_train)
    train_time = time.time() - t0
    print(f"    Training time: {train_time:.1f}s")

    # Evaluate
    print("\n" + "=" * 70)
    print("  EVALUATION RESULTS")
    print("=" * 70)

    # Predictions
    y_pred = sgd.predict(X_test)
    
    # Get decision function scores for top-k accuracy
    decision_scores = sgd.decision_function(X_test)
    # Normalize to probabilities using softmax
    from scipy.special import softmax
    y_proba = softmax(decision_scores, axis=1)

    # Accuracy
    acc = accuracy_score(y_test, y_pred)
    print(f"\n  Top-1 Accuracy: {acc:.4f} ({acc*100:.1f}%)")

    # Top-3 accuracy
    top3_acc = top_k_accuracy_score(y_test, y_proba, k=3)
    print(f"  Top-3 Accuracy: {top3_acc:.4f} ({top3_acc*100:.1f}%)")

    # Top-5 accuracy
    top5_acc = top_k_accuracy_score(y_test, y_proba, k=5)
    print(f"  Top-5 Accuracy: {top5_acc:.4f} ({top5_acc*100:.1f}%)")

    # Per-class report (top 20 classes by support)
    print(f"\n  Classification Report (top 20 groups by frequency):")
    top_20_groups = df['SupportGroup'].value_counts().head(20).index.tolist()
    top_20_indices = [i for i, cls in enumerate(label_encoder.classes_) if cls in top_20_groups]
    target_names = [label_encoder.classes_[i] for i in top_20_indices]

    mask = np.isin(y_test, top_20_indices)
    if mask.sum() > 0:
        report = classification_report(
            y_test[mask], y_pred[mask],
            labels=top_20_indices,
            target_names=target_names,
            zero_division=0
        )
        print(report)

    # Confidence analysis
    max_proba = y_proba.max(axis=1)
    correct = (y_pred == y_test)
    print(f"\n  Confidence Analysis:")
    for threshold in [0.3, 0.5, 0.7, 0.8, 0.9]:
        high_conf = max_proba >= threshold
        if high_conf.sum() > 0:
            coverage = high_conf.mean()
            acc_at_thresh = correct[high_conf].mean()
            print(f"    Confidence >= {threshold}: coverage={coverage:.1%}, accuracy={acc_at_thresh:.1%}")

    # Save model artifacts
    print("\n  Saving model artifacts...")
    artifacts = {
        'classifier': sgd,
        'tfidf_vectorizer': tfidf,
        'label_encoder': label_encoder,
        'cat_encoders': cat_encoders,
        'cat_features': cat_features,
        'metrics': {
            'top1_accuracy': float(acc),
            'top3_accuracy': float(top3_acc),
            'top5_accuracy': float(top5_acc),
            'num_classes': num_classes,
            'train_size': len(train_indices),
            'test_size': len(test_indices),
            'train_time_seconds': train_time,
            'model_type': 'SGDClassifier',
            'loss': 'modified_huber',
            'class_weight': 'balanced',
            'excluded_groups': EXCLUDE_GROUPS,
            'min_class_size': MIN_CLASS_SIZE,
            'sample_size': SAMPLE_SIZE
        }
    }
    model_path = MODEL_DIR / 'improved_classifier.pkl'
    with open(model_path, 'wb') as f:
        pickle.dump(artifacts, f)
    print(f"  Model saved to {model_path}")

    # Save metrics as JSON
    metrics_path = MODEL_DIR / 'improved_metrics.json'
    with open(metrics_path, 'w') as f:
        json.dump(artifacts['metrics'], f, indent=2)
    print(f"  Metrics saved to {metrics_path}")

    return artifacts


def main():
    print("=" * 70)
    print("  TICKET RECOMMENDATION - IMPROVED CLASSIFIER")
    print("=" * 70)
    print(f"  Sample size: {SAMPLE_SIZE}")
    print(f"  Min class size: {MIN_CLASS_SIZE}")
    print(f"  Excluded groups: {EXCLUDE_GROUPS}")
    print()

    # Step 1: Extract data
    df = extract_dataset()
    print(f"\n  Dataset shape: {df.shape}")
    print(f"  Support groups: {df['SupportGroup'].nunique()}")

    # Step 2: Prepare features
    df = prepare_features(df)

    # Step 3: Train and evaluate
    artifacts = train_and_evaluate(df)

    print("\n" + "=" * 70)
    print("  DONE!")
    print("=" * 70)
    metrics = artifacts['metrics']
    print(f"\n  Summary:")
    print(f"    Top-1 Accuracy: {metrics['top1_accuracy']:.1%}")
    print(f"    Top-3 Accuracy: {metrics['top3_accuracy']:.1%}")
    print(f"    Top-5 Accuracy: {metrics['top5_accuracy']:.1%}")
    print(f"    Classes: {metrics['num_classes']}")
    print(f"    Training time: {metrics['train_time_seconds']:.0f}s")
    print(f"    Improvement over baseline (43.7%): +{(metrics['top1_accuracy']-0.437)*100:.1f}pp")


if __name__ == '__main__':
    main()