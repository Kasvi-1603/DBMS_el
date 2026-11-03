"""
Isolation Forest anomaly detection for the fraud-ring detection system.

Uses the exact feature definition, imputation, z-scoring and model settings
from backend/risk_features.py, and the network detection from
backend/networks.py — the same code app.py runs — so a claim is never
"anomalous" here and "normal" on the dashboard.

This script only READS the database and writes a JSON report; the stored
ml_anomaly / ml_anomaly_score columns are written by the API's analysis run
(risk_features.analyse_portfolio).

Usage:
    # Score every claim currently in Postgres and write anomalies.json
    python anomaly_detection.py --db postgresql://user:pass@host/fraud_db

    # Score a CSV that already has the 6 canonical feature columns
    # (claim_amount, claim_premium_ratio, network_size, garage_frequency,
    #  doctor_frequency, category_deviation)
    python anomaly_detection.py --csv claims_features.csv
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))
import risk_features  # noqa: E402  (path must be set up first)


def score_from_db(conn_string: str):
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(conn_string, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        with conn.cursor() as cur:
            ids, feats, _ = risk_features.portfolio_features(cur)
        return ids, risk_features.ml_matrix(ids, feats)
    finally:
        conn.close()


def score_from_csv(path: str):
    import pandas as pd

    df = pd.read_csv(path)
    missing = [f for f in risk_features.FEATURE_NAMES if f not in df.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")
    if df[risk_features.FEATURE_NAMES].isna().any().any():
        raise ValueError("CSV has empty feature values; fill them with the documented imputation first "
                         "(median ratio, category deviation 0).")
    ids = df["claim_id"].tolist() if "claim_id" in df.columns else list(range(len(df)))
    return ids, df[risk_features.FEATURE_NAMES].values.tolist()


def main():
    parser = argparse.ArgumentParser(description="Flag unusual insurance claims (unusual is not the same as fraud)")
    parser.add_argument("--csv", help="Path to a CSV with the 6 canonical feature columns")
    parser.add_argument("--db", default=os.environ.get("DATABASE_URL"),
                        help="Postgres connection string (defaults to $DATABASE_URL)")
    parser.add_argument("--out", default="anomalies.json", help="Output JSON path")
    args = parser.parse_args()

    if not args.csv and not args.db:
        sys.exit("Provide either --csv or --db (or set DATABASE_URL)")

    ids, X = score_from_csv(args.csv) if args.csv else score_from_db(args.db)
    if len(X) < risk_features.MIN_ML_CLAIMS:
        sys.exit(f"Only {len(X)} claims; at least {risk_features.MIN_ML_CLAIMS} are needed for a reliable fit.")

    model, mean, std, threshold = risk_features.fit_isolation_forest(X)
    flags, scores = risk_features.score_isolation_forest(model, mean, std, X)

    results = [
        {"claim_id": cid, "ml_anomaly": flag, "ml_anomaly_score": score,
         **dict(zip(risk_features.FEATURE_NAMES, row))}
        for cid, flag, score, row in zip(ids, flags, scores, X)
    ]
    results.sort(key=lambda r: -r["ml_anomaly_score"])

    with open(args.out, "w") as f:
        json.dump({"threshold": threshold, "config": risk_features.model_config(), "claims": results}, f, indent=2)

    flagged = sum(1 for r in results if r["ml_anomaly"])
    print(f"Scored {len(results)} claims — {flagged} unusual (score > {threshold}), "
          f"{len(results) - flagged} not unusual.")
    print(f"Written to {args.out}")


if __name__ == "__main__":
    main()
