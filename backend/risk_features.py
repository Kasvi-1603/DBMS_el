"""
Canonical risk scoring, ML features, Isolation Forest and final
recommendation. app.py, seed_postgres.py and ml/anomaly_detection.py all call
into this module; nothing else computes a risk score or a feature vector.

Risk score (0-100) = sum of four factor points, each rounded to 0.1:
    ratio     35 * min(ratio / 8, 1)                 ratio = claim / premium
    network   25 * min((network_size - 1) / 5, 1)    other claims in the component
    garage    20 * min((garage_uses - 1) / 5, 1)     other claims using the garage
    category  20 * min(max(deviation, 0) / 2, 1)     deviation = (amount - avg) / avg
Missing premium -> ratio factor 0 ("unavailable"); fewer than
MIN_CATEGORY_HISTORY claims in a category -> category factor 0 ("insufficient
history"). No factor can exceed its weight, so the total is bounded to 0-100.

Isolation Forest feature vector, in this exact order (FEATURE_NAMES):
    claim_amount, claim_premium_ratio, network_size, garage_frequency,
    doctor_frequency, category_deviation
Preprocessing: missing ratio -> median ratio of the portfolio, missing
category deviation -> 0 (treated as "at the average"), then z-score each column.
Model: IsolationForest(n_estimators=300, contamination="auto", random_state=42),
refit on the whole portfolio on every analysis run, so training and
inference always use the identical feature rows.
"""
import psycopg2.extras

import networks as net

FEATURE_NAMES = [
    "claim_amount",
    "claim_premium_ratio",
    "network_size",
    "garage_frequency",
    "doctor_frequency",
    "category_deviation",
]

RISK_WEIGHTS = {"ratio": 35, "network": 25, "garage": 20, "category": 20}
RATIO_SATURATION = 8          # ratio at which the ratio factor is maxed out
OTHERS_SATURATION = 5         # other connected claims / other garage uses for max points
CATEGORY_DEV_SATURATION = 2   # +200% above the category average for max points
MIN_CATEGORY_HISTORY = 5      # claims needed in a category before its average is used

REVIEW_THRESHOLD = 35
INVESTIGATION_THRESHOLD = 50
HIGH_RISK_THRESHOLD = 70

ML_N_ESTIMATORS = 300
ML_CONTAMINATION = "auto"
ML_RANDOM_STATE = 42
MIN_ML_CLAIMS = 20            # below this the forest is not fit; results are "unavailable"

STATUS_NONE = "No immediate concern"
STATUS_REVIEW = "Under Review"
STATUS_INVESTIGATE = "Requires Investigation"
STATUS_PENDING = "Analysis pending"

ANALYSIS_LOCK_KEY = 424242    # pg_advisory_xact_lock key: one portfolio analysis at a time


# ============================================================================
# Features
# ============================================================================

def portfolio_features(cur, networks=None):
    """Per-claim factor inputs for every claim in the database, computed from
    the current rows. Returns (ids, features_by_id, networks)."""
    if networks is None:
        networks = net.compute_networks(cur)
    size_by_claim = {cid: size for cid, (_, size) in net.network_index(networks).items()}

    cur.execute("""
        SELECT c.claim_id, c.garage_id, c.doctor_id, c.claim_amount, c.category,
               p.premium_amount
        FROM claims c JOIN policies p ON c.policy_id = p.policy_id
        ORDER BY c.claim_id
    """)
    rows = cur.fetchall()

    garage_counts, doctor_counts, cat_totals, cat_counts = {}, {}, {}, {}
    for r in rows:
        if r["garage_id"] is not None:
            garage_counts[r["garage_id"]] = garage_counts.get(r["garage_id"], 0) + 1
        if r["doctor_id"] is not None:
            doctor_counts[r["doctor_id"]] = doctor_counts.get(r["doctor_id"], 0) + 1
        cat_totals[r["category"]] = cat_totals.get(r["category"], 0.0) + float(r["claim_amount"])
        cat_counts[r["category"]] = cat_counts.get(r["category"], 0) + 1

    ids, feats = [], {}
    for r in rows:
        amount = float(r["claim_amount"])
        premium = float(r["premium_amount"]) if r["premium_amount"] is not None else None
        ratio = amount / premium if premium and premium > 0 else None
        n_cat = cat_counts[r["category"]]
        cat_avg = cat_totals[r["category"]] / n_cat
        cat_dev = (amount - cat_avg) / cat_avg if n_cat >= MIN_CATEGORY_HISTORY and cat_avg > 0 else None
        feats[r["claim_id"]] = {
            "amount": amount,
            "premium": premium,
            "ratio": ratio,
            "network_size": size_by_claim.get(r["claim_id"], 1),
            "garage_id": r["garage_id"],
            "garage_frequency": garage_counts.get(r["garage_id"], 0) if r["garage_id"] is not None else 0,
            "doctor_frequency": doctor_counts.get(r["doctor_id"], 0) if r["doctor_id"] is not None else 0,
            "category": r["category"],
            "category_count": n_cat,
            "category_avg": cat_avg,
            "category_deviation": cat_dev,
        }
        ids.append(r["claim_id"])
    return ids, feats, networks


def ml_matrix(ids, feats):
    """Feature rows in FEATURE_NAMES order with the documented imputation."""
    ratios = sorted(f["ratio"] for f in feats.values() if f["ratio"] is not None)
    median_ratio = ratios[len(ratios) // 2] if ratios else 0.0
    X = []
    for cid in ids:
        f = feats[cid]
        X.append([
            f["amount"],
            f["ratio"] if f["ratio"] is not None else median_ratio,
            f["network_size"],
            f["garage_frequency"],
            f["doctor_frequency"],
            f["category_deviation"] if f["category_deviation"] is not None else 0.0,
        ])
    return X


# ============================================================================
# Risk score
# ============================================================================

def risk_breakdown(f):
    """Factor-by-factor points for one claim's features. The total is the sum
    of the rounded factor points, so the breakdown always adds up exactly."""
    w = RISK_WEIGHTS
    ratio = f["ratio"]
    nsize = f["network_size"]
    g_others = max(f["garage_frequency"] - 1, 0) if f["garage_id"] is not None else 0
    dev = f["category_deviation"]
    cat_label = f["category"].replace("_", " ")

    ratio_pts = round(min(ratio / RATIO_SATURATION, 1) * w["ratio"], 1) if ratio is not None else 0.0
    net_pts = round(min((nsize - 1) / OTHERS_SATURATION, 1) * w["network"], 1)
    garage_pts = round(min(g_others / OTHERS_SATURATION, 1) * w["garage"], 1)
    cat_pts = round(min(max(dev, 0) / CATEGORY_DEV_SATURATION, 1) * w["category"], 1) if dev is not None else 0.0

    if ratio is None:
        ratio_expl = "The policy premium is missing or zero, so the ratio cannot be computed — 0 points."
    else:
        ratio_expl = (f"The claim is {ratio:.2f}× the premium. Points = {w['ratio']} × min({ratio:.2f} / "
                      f"{RATIO_SATURATION}, 1) = {ratio_pts}.")
    if nsize > 1:
        net_expl = (f"Linked (directly or through other claims) to {nsize - 1} other claim(s) by shared "
                    f"entities. Points = {w['network']} × min({nsize - 1} / {OTHERS_SATURATION}, 1) = {net_pts}. "
                    "Association is not proof of fraud.")
    else:
        net_expl = f"No other claim shares an entity with this claim — 0 of {w['network']} points."
    if f["garage_id"] is None:
        garage_expl = f"No garage on this claim — 0 of {w['garage']} points."
    else:
        garage_expl = (f"This garage appears on {f['garage_frequency']} claim(s) in total, i.e. {g_others} "
                       f"other(s). Points = {w['garage']} × min({g_others} / {OTHERS_SATURATION}, 1) = {garage_pts}.")
    if dev is None:
        cat_expl = (f"Only {f['category_count']} {cat_label} claim(s) on file (fewer than "
                    f"{MIN_CATEGORY_HISTORY}), so no category average is used — 0 points.")
    else:
        cat_expl = (f"Average {cat_label} claim in this (synthetic) portfolio: ₹{f['category_avg']:,.0f} over "
                    f"{f['category_count']} claims; this claim is {dev * 100:+.0f}% from it. Points = "
                    f"{w['category']} × min(max({dev:.2f}, 0) / {CATEGORY_DEV_SATURATION}, 1) = {cat_pts}.")

    breakdown = [
        {"key": "ratio", "factor": "Claim / premium ratio",
         "value": f"{ratio:.2f}×" if ratio is not None else "unavailable",
         "points": ratio_pts, "max_points": w["ratio"], "explanation": ratio_expl},
        {"key": "network", "factor": "Network size",
         "value": f"{nsize} claim{'s' if nsize != 1 else ''} in component",
         "points": net_pts, "max_points": w["network"], "explanation": net_expl},
        {"key": "garage", "factor": "Garage frequency",
         "value": f"{g_others} other claim(s)" if f["garage_id"] is not None else "no garage",
         "points": garage_pts, "max_points": w["garage"], "explanation": garage_expl},
        {"key": "category", "factor": "Category deviation",
         "value": f"{dev * 100:+.0f}% vs {cat_label} average" if dev is not None else "insufficient history",
         "points": cat_pts, "max_points": w["category"], "explanation": cat_expl},
    ]
    total = round(ratio_pts + net_pts + garage_pts + cat_pts, 1)
    return breakdown, total


def risk_score(f):
    return risk_breakdown(f)[1]


# ============================================================================
# Isolation Forest
# ============================================================================

def fit_isolation_forest(X):
    """Z-scores the columns (claim_amount is in thousands, the others are small
    numbers) and fits the forest. Returns (model, mean, std, threshold) where
    threshold is the anomaly-score cut-off: score > threshold <=> anomalous."""
    import numpy as np
    from sklearn.ensemble import IsolationForest

    Xa = np.array(X, dtype=float)
    mean = Xa.mean(axis=0)
    std = Xa.std(axis=0)
    std[std == 0] = 1.0
    model = IsolationForest(n_estimators=ML_N_ESTIMATORS, contamination=ML_CONTAMINATION,
                            random_state=ML_RANDOM_STATE)
    model.fit((Xa - mean) / std)
    return model, mean, std, float(-model.offset_)


def score_isolation_forest(model, mean, std, X):
    """Returns (flags, scores). score = -score_samples(x): higher = more unusual
    (roughly 0.35-0.75). The flag comes from model.predict, which is exactly
    score > threshold, so the two can never disagree."""
    import numpy as np

    Xs = (np.array(X, dtype=float) - mean) / std
    flags = [bool(p == -1) for p in model.predict(Xs)]
    scores = [round(float(s), 3) for s in -model.score_samples(Xs)]
    return flags, scores


# ============================================================================
# Final recommendation
# ============================================================================

def recommend(score, ml_anomaly, ml_score=None, ml_threshold=None):
    """Investigation recommendation and the exact rule(s) that produced it.
      Requires Investigation : risk score >= 50
      Under Review           : 35 <= risk score < 50, OR Isolation Forest anomaly
      No immediate concern   : otherwise
    An ML anomaly alone can raise a claim to Under Review, never higher:
    the forest measures how unusual a claim is, not whether it is fraud."""
    if score is None:
        return STATUS_PENDING, ["This claim has been saved but not analysed yet."]
    reasons = []
    if score >= INVESTIGATION_THRESHOLD:
        status = STATUS_INVESTIGATE
        reasons.append(f"Risk score {score} is at or above the investigation threshold ({INVESTIGATION_THRESHOLD}).")
    elif score >= REVIEW_THRESHOLD:
        status = STATUS_REVIEW
        reasons.append(f"Risk score {score} is between the review threshold ({REVIEW_THRESHOLD}) "
                       f"and the investigation threshold ({INVESTIGATION_THRESHOLD}).")
    elif ml_anomaly:
        status = STATUS_REVIEW
        reasons.append(f"Risk score {score} is below {REVIEW_THRESHOLD}, but the Isolation Forest marked the "
                       "claim as unusual, so it is queued for review.")
    else:
        status = STATUS_NONE
        reasons.append(f"Risk score {score} is below the review threshold ({REVIEW_THRESHOLD}).")

    if ml_anomaly is None:
        reasons.append("ML result unavailable — the recommendation uses the risk score only.")
    elif ml_anomaly:
        detail = f" (anomaly score {ml_score} > threshold {ml_threshold})" if ml_score is not None and ml_threshold else ""
        reasons.append(f"Isolation Forest: unusual compared with the rest of the portfolio{detail}. "
                       "This measures unusualness, not the probability of fraud.")
    else:
        reasons.append("Isolation Forest: not unusual compared with the rest of the portfolio.")
    return status, reasons


# ============================================================================
# Portfolio analysis (the only writer of risk_score / ml_anomaly)
# ============================================================================

def analyse_portfolio(cur, trigger):
    """Recomputes networks, risk scores and Isolation Forest results for EVERY
    claim and stores them. Every claim must be re-scored, not just a new one:
    a new claim can enlarge an existing network or raise a garage's frequency,
    which changes other claims' factors too.

    Auto-triage (existing behaviour, unchanged): an 'open' claim whose risk
    score reaches the investigation threshold is moved to 'under_review'.
    Claims an investigator already moved are never demoted.

    Runs inside the caller's transaction; the caller commits or rolls back."""
    cur.execute("SELECT pg_advisory_xact_lock(%s)", (ANALYSIS_LOCK_KEY,))
    ids, feats, networks = portfolio_features(cur)

    scores = {cid: risk_score(feats[cid]) for cid in ids}

    ml_available = len(ids) >= MIN_ML_CLAIMS
    flags, ml_scores, threshold, reason = {}, {}, None, None
    if ml_available:
        X = ml_matrix(ids, feats)
        model, mean, std, threshold = fit_isolation_forest(X)
        f_list, s_list = score_isolation_forest(model, mean, std, X)
        flags = dict(zip(ids, f_list))
        ml_scores = dict(zip(ids, s_list))
    else:
        reason = f"Only {len(ids)} claims on file; at least {MIN_ML_CLAIMS} are needed to fit the Isolation Forest."

    psycopg2.extras.execute_values(cur, """
        UPDATE claims AS c SET risk_score = v.risk_score, ml_anomaly = v.ml_anomaly,
               ml_anomaly_score = v.ml_score, analyzed_at = NOW(), analysis_error = NULL
        FROM (VALUES %s) AS v(claim_id, risk_score, ml_anomaly, ml_score)
        WHERE c.claim_id = v.claim_id
    """, [(cid, scores[cid], flags.get(cid), ml_scores.get(cid)) for cid in ids],
        template="(%s::int, %s::numeric, %s::boolean, %s::numeric)")

    cur.execute("UPDATE claims SET status = 'under_review' WHERE status = 'open' AND risk_score >= %s",
                (INVESTIGATION_THRESHOLD,))

    flagged = sum(1 for v in flags.values() if v) if ml_available else None
    cur.execute("""
        INSERT INTO analysis_runs (trigger, claims_evaluated, ml_available, ml_flagged, ml_threshold,
                                   ml_contamination, ml_unavailable_reason, networks_found)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING run_id, run_at
    """, (trigger, len(ids), ml_available, flagged, threshold, str(ML_CONTAMINATION), reason, len(networks)))
    run = cur.fetchone()
    return {
        "run_id": run["run_id"], "claims_evaluated": len(ids), "ml_available": ml_available,
        "ml_flagged": flagged, "ml_threshold": threshold, "networks": networks,
    }


def latest_run(cur):
    cur.execute("SELECT * FROM analysis_runs ORDER BY run_id DESC LIMIT 1")
    row = cur.fetchone()
    if not row:
        return None
    row = dict(row)
    row["run_at"] = row["run_at"].isoformat(sep=" ", timespec="seconds")
    row["ml_threshold"] = float(row["ml_threshold"]) if row["ml_threshold"] is not None else None
    return row


def model_config():
    return {
        "features": FEATURE_NAMES,
        "n_estimators": ML_N_ESTIMATORS,
        "contamination": str(ML_CONTAMINATION),
        "random_state": ML_RANDOM_STATE,
        "min_claims": MIN_ML_CLAIMS,
        "preprocessing": "missing ratio -> portfolio median; missing category deviation -> 0; z-score per feature",
        "score_meaning": "anomaly score = -score_samples(x); higher = more unusual; anomalous when score > threshold",
        "threshold_note": "With contamination='auto' scikit-learn fixes the cut-off at 0.5 on this score "
                          "(offset_ = -0.5), so the number of flagged claims follows from the data, not a quota.",
    }


def risk_config():
    return {
        "weights": RISK_WEIGHTS,
        "ratio_saturation": RATIO_SATURATION,
        "others_saturation": OTHERS_SATURATION,
        "category_deviation_saturation": CATEGORY_DEV_SATURATION,
        "min_category_history": MIN_CATEGORY_HISTORY,
        "review_threshold": REVIEW_THRESHOLD,
        "investigation_threshold": INVESTIGATION_THRESHOLD,
        "high_risk_threshold": HIGH_RISK_THRESHOLD,
    }
