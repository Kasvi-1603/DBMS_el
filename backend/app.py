"""
Insurance Fraud Ring Detection — Flask API (Ledger)

Endpoints:
  GET  /api/summary                    -> KPI totals, counters, thresholds, model config
  GET  /api/claims                     -> every claim (filters: q, category, status,
                                           recommendation, min_risk)
  GET  /api/claims/<id>                -> full investigation report for one claim
  GET  /api/claims/<id>/network        -> recursive CTE network for a claim, with hop counts
  POST /api/claims/<id>/analyze        -> re-run analysis (safe retry; never creates a claim)
  POST /api/claims/<id>/status         -> update the investigator's case status
  GET  /api/networks                   -> every connected component (+ shared entities, graph)
  GET  /api/category-breakdown         -> claim count/total/avg risk per category
  GET  /api/entities                   -> existing customers/vehicles/garages/doctors/witnesses
  GET  /api/customers/<id>             -> one customer's stored details and policies
  POST /api/claims/submit              -> New Claim workflow (validate -> save in one
                                           transaction -> analyse portfolio -> report)

Configure the database with the DATABASE_URL environment variable, e.g.
  postgresql://user:password@localhost:5432/fraud_db
"""
import glob
import hashlib
import json
import math
import os
import re
import sys
import uuid
from contextlib import contextmanager
from datetime import date

import psycopg2
import psycopg2.errors
import psycopg2.extras
from flask import Flask, jsonify, request
from flask_cors import CORS
from werkzeug.exceptions import HTTPException

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import networks as net  # noqa: E402
import risk_features as rf  # noqa: E402

app = Flask(__name__)
CORS(app)

DATABASE_URL = os.environ.get("DATABASE_URL")
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "database", "migrations")
CATEGORIES = ["collision", "medical", "theft", "fire", "water_damage"]
CASE_STATUSES = ("open", "under_review", "flagged", "closed")
MAX_MONEY = 9_999_999_999.99  # NUMERIC(12,2)


@contextmanager
def db():
    """One connection per request, always closed. Nothing is committed unless
    the caller commits, so an exception rolls the whole transaction back."""
    if not DATABASE_URL:
        raise ApiError(503, "DATABASE_URL is not set. Export it before starting the API.")
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    try:
        yield conn
    finally:
        conn.close()


def apply_migrations(conn):
    with conn.cursor() as cur:
        for path in sorted(glob.glob(os.path.join(MIGRATIONS_DIR, "*.sql"))):
            with open(path, encoding="utf-8") as f:
                cur.execute(f.read())
    conn.commit()


class ApiError(Exception):
    def __init__(self, status, message, errors=None):
        super().__init__(message)
        self.status, self.message, self.errors = status, message, errors or []


@app.errorhandler(ApiError)
def handle_api_error(e):
    return jsonify({"error": e.message, "errors": e.errors}), e.status


@app.errorhandler(psycopg2.OperationalError)
def handle_db_down(e):
    return jsonify({"error": f"Database unavailable: {str(e).strip()}"}), 503


@app.errorhandler(Exception)
def handle_unexpected(e):
    if isinstance(e, HTTPException):
        return jsonify({"error": e.description}), e.code
    app.logger.exception(e)
    return jsonify({"error": f"Internal error: {type(e).__name__}: {e}"}), 500


# ============================================================================
# Shared read models — every page is built from these
# ============================================================================

CLAIMS_SQL = """
    SELECT c.claim_id, c.policy_id, p.policy_number, p.premium_amount, p.start_date, p.end_date,
           cl.claimant_id, cl.full_name AS claimant, cl.phone, cl.address,
           c.claim_amount, c.category, c.status, c.incident_date, c.description,
           c.risk_score, c.ml_anomaly, c.ml_anomaly_score, c.analyzed_at, c.analysis_error,
           c.garage_id, g.name AS garage_name, c.doctor_id, d.full_name AS doctor_name,
           c.vehicle_id, v.vin, v.plate_number,
           COALESCE((SELECT array_agg(cw.witness_id ORDER BY cw.witness_id)
                     FROM claim_witnesses cw WHERE cw.claim_id = c.claim_id), '{}') AS witness_ids
    FROM claims c
    JOIN policies p   ON c.policy_id = p.policy_id
    JOIN claimants cl ON p.claimant_id = cl.claimant_id
    LEFT JOIN garages g  ON c.garage_id = g.garage_id
    LEFT JOIN doctors d  ON c.doctor_id = d.doctor_id
    LEFT JOIN vehicles v ON c.vehicle_id = v.vehicle_id
"""


def _f(v):
    return float(v) if v is not None else None


def shape_claim(r, net_idx, threshold):
    amount, premium = float(r["claim_amount"]), _f(r["premium_amount"])
    score = _f(r["risk_score"]) if r["analyzed_at"] is not None else None
    ml = r["ml_anomaly"] if r["analyzed_at"] is not None else None
    ml_score = _f(r["ml_anomaly_score"]) if ml is not None else None
    recommendation, reasons = rf.recommend(score, ml, ml_score, threshold)
    nid, nsize = net_idx.get(r["claim_id"], (None, 1))
    return {
        "claim_id": r["claim_id"],
        "claimant_id": r["claimant_id"],
        "claimant": r["claimant"],
        "phone": r["phone"],
        "address": r["address"],
        "policy_id": r["policy_id"],
        "policy_number": r["policy_number"],
        "policy_start": str(r["start_date"]),
        "policy_end": str(r["end_date"]) if r["end_date"] else None,
        "amount": amount,
        "premium": premium,
        "ratio": round(amount / premium, 2) if premium else None,
        "category": r["category"],
        "status": r["status"],
        "incident_date": str(r["incident_date"]),
        "description": r["description"],
        "risk_score": score,
        "ml_anomaly": ml,
        "ml_anomaly_score": ml_score,
        "analysis_error": r["analysis_error"],
        "analyzed": r["analyzed_at"] is not None,
        "recommendation": recommendation,
        "recommendation_reasons": reasons,
        "network_id": nid,
        "network_size": nsize,
        "garage_id": r["garage_id"],
        "garage_name": r["garage_name"],
        "doctor_id": r["doctor_id"],
        "doctor_name": r["doctor_name"],
        "vehicle_id": r["vehicle_id"],
        "vin": r["vin"],
        "plate_number": r["plate_number"],
        "witness_ids": list(r["witness_ids"] or []),
    }


def claims_rows(cur, networks=None, where="", params=None):
    if networks is None:
        networks = net.compute_networks(cur)
    run = rf.latest_run(cur)
    threshold = run["ml_threshold"] if run else None
    cur.execute(CLAIMS_SQL + where + " ORDER BY c.claim_id DESC", params or {})
    idx = net.network_index(networks)
    return [shape_claim(r, idx, threshold) for r in cur.fetchall()], networks


def is_suspicious(c):
    return c["recommendation"] in (rf.STATUS_REVIEW, rf.STATUS_INVESTIGATE)


def claim_report(cur, claim_id):
    """Everything known about one claim — used by New Claim, the claim drawer,
    Risk Scoring and the Evidence Log, so they can never disagree."""
    claims, networks = claims_rows(cur, where=" WHERE c.claim_id = %(id)s", params={"id": claim_id})
    if not claims:
        raise ApiError(404, f"Claim #{claim_id} does not exist.")
    claim = claims[0]

    members = net.network_members(cur, claim_id)
    edges = net.fetch_edges(cur, members) if len(members) > 1 else []
    hops = net.hop_distances(claim_id, members, edges)
    entities = net.shared_entities(cur, members)

    member_rows, _ = claims_rows(cur, networks, " WHERE c.claim_id = ANY(%(ids)s)", {"ids": members})
    connected = [{
        "claim_id": m["claim_id"], "claimant_id": m["claimant_id"], "claimant": m["claimant"],
        "category": m["category"], "amount": m["amount"], "risk_score": m["risk_score"],
        "recommendation": m["recommendation"], "status": m["status"],
        "hops": hops.get(m["claim_id"]), "relation": "direct" if hops.get(m["claim_id"]) == 1 else "indirect",
    } for m in member_rows if m["claim_id"] != claim_id]
    connected.sort(key=lambda m: (m["hops"] or 99, m["claim_id"]))

    _, feats, _ = rf.portfolio_features(cur, networks)
    breakdown, live_total = rf.risk_breakdown(feats[claim_id])
    stored = claim["risk_score"]

    return {
        "claim": claim,
        "network": {
            "network_id": claim["network_id"],
            "size": len(members),
            "claim_ids": members,
            "direct_claim_ids": sorted(c["claim_id"] for c in connected if c["relation"] == "direct"),
            "indirect_claim_ids": sorted(c["claim_id"] for c in connected if c["relation"] == "indirect"),
            "connected_claims": connected,
            "shared_entities": entities,
            "direct_shared_entities": [e for e in entities if claim_id in e["claim_ids"]],
            "evidence_readable": [net.readable(e) for e in entities],
            "graph": net.build_graph(cur, members, entities),
        },
        "risk": {
            "score": stored,
            "breakdown": breakdown,
            "breakdown_total": live_total,
            "consistent": stored is not None and abs(stored - live_total) < 0.05,
        },
        "ml": {
            "anomaly": claim["ml_anomaly"],
            "score": claim["ml_anomaly_score"],
            "available": claim["ml_anomaly"] is not None,
            "threshold": (rf.latest_run(cur) or {}).get("ml_threshold"),
        },
        "recommendation": claim["recommendation"],
        "recommendation_reasons": claim["recommendation_reasons"],
    }


def network_report(cur, members, claims_by_id):
    entities = net.shared_entities(cur, members)
    rows = [claims_by_id[m] for m in members]
    scored = [r["risk_score"] for r in rows if r["risk_score"] is not None]
    claimants = {}
    for r in rows:
        claimants.setdefault(r["claimant_id"], r["claimant"])
    rec_counts = {}
    for r in rows:
        rec_counts[r["recommendation"]] = rec_counts.get(r["recommendation"], 0) + 1
    return {
        "network_id": f"N-{members[0]}",
        "claim_ids": members,
        "size": len(members),
        "total_amount": round(sum(r["amount"] for r in rows), 2),
        "avg_risk_score": round(sum(scored) / len(scored), 1) if scored else None,
        "max_risk_score": max(scored) if scored else None,
        "ml_anomalies": sum(1 for r in rows if r["ml_anomaly"]),
        "recommendation_counts": rec_counts,
        "claimants": [{"claimant_id": k, "name": v} for k, v in claimants.items()],
        "claims": [{k: r[k] for k in ("claim_id", "claimant_id", "claimant", "category", "amount",
                                      "risk_score", "ml_anomaly", "recommendation", "status")} for r in rows],
        "shared_entities": entities,
        "evidence_readable": [net.readable(e) for e in entities],
        "graph": net.build_graph(cur, members, entities),
    }


# ============================================================================
# Read endpoints
# ============================================================================

@app.route("/api/summary")
def summary():
    with db() as conn, conn.cursor() as cur:
        claims, networks = claims_rows(cur)
        recs = [c["recommendation"] for c in claims]
        return jsonify({
            "total_claims": len(claims),
            "analysed_claims": sum(1 for c in claims if c["analyzed"]),
            "pending_analysis": sum(1 for c in claims if not c["analyzed"]),
            "requires_investigation": recs.count(rf.STATUS_INVESTIGATE),
            "under_review": recs.count(rf.STATUS_REVIEW),
            "no_immediate_concern": recs.count(rf.STATUS_NONE),
            "suspicious_claims": sum(1 for c in claims if is_suspicious(c)),
            "high_risk_claims": sum(1 for c in claims if (c["risk_score"] or 0) >= rf.HIGH_RISK_THRESHOLD),
            "ml_evaluated": sum(1 for c in claims if c["ml_anomaly"] is not None),
            "ml_anomalies": sum(1 for c in claims if c["ml_anomaly"]),
            "ml_unavailable": sum(1 for c in claims if c["ml_anomaly"] is None),
            "total_networks": len(networks),
            "claims_in_networks": sum(len(n) for n in networks),
            "largest_network": max((len(n) for n in networks), default=0),
            "total_claim_value": round(sum(c["amount"] for c in claims), 2),
            "flagged_value": round(sum(c["amount"] for c in claims if is_suspicious(c)), 2),
            "investigation_value": round(sum(c["amount"] for c in claims
                                             if c["recommendation"] == rf.STATUS_INVESTIGATE), 2),
            "thresholds": rf.risk_config(),
            "model": rf.model_config(),
            "last_run": rf.latest_run(cur),
        })


@app.route("/api/claims")
def list_claims():
    """Every persisted claim. Optional filters: q (claim ID, customer ID/name,
    policy number, phone, VIN/plate, garage, doctor), category, status (case
    status), recommendation, min_risk."""
    q = (request.args.get("q") or "").strip()
    category = request.args.get("category")
    status = request.args.get("status")
    recommendation = request.args.get("recommendation")
    min_risk = request.args.get("min_risk", type=float)

    clauses, params = [], {}
    if q:
        params["like"] = f"%{q}%"
        text_match = ("cl.full_name ILIKE %(like)s OR p.policy_number ILIKE %(like)s OR v.vin ILIKE %(like)s "
                      "OR v.plate_number ILIKE %(like)s OR g.name ILIKE %(like)s OR d.full_name ILIKE %(like)s")
        digits = re.sub(r"\D", "", q)
        if len(digits) >= 7:
            params["digits"] = f"%{digits[-10:]}%"
            text_match += " OR regexp_replace(cl.phone, '[^0-9]', '', 'g') LIKE %(digits)s"
        m = re.fullmatch(r"#?\s*(\d+)", q)
        if m:
            params["num"] = int(m.group(1))
            text_match += " OR c.claim_id = %(num)s OR cl.claimant_id = %(num)s"
        clauses.append(f"({text_match})")
    if category and category != "all":
        clauses.append("c.category = %(category)s")
        params["category"] = category
    if status and status != "all":
        clauses.append("c.status = %(status)s")
        params["status"] = status
    if min_risk:
        clauses.append("c.risk_score >= %(min_risk)s")
        params["min_risk"] = min_risk
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""

    with db() as conn, conn.cursor() as cur:
        rows, _ = claims_rows(cur, where=where, params=params)
    if recommendation and recommendation != "all":
        rows = [r for r in rows if r["recommendation"] == recommendation]
    resp = jsonify(rows)
    resp.headers["X-Total-Count"] = str(len(rows))
    return resp


@app.route("/api/claims/<int:claim_id>")
def claim_detail(claim_id):
    with db() as conn, conn.cursor() as cur:
        return jsonify(claim_report(cur, claim_id))


@app.route("/api/claims/<int:claim_id>/network")
def claim_network(claim_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM claims WHERE claim_id = %s", (claim_id,))
        if not cur.fetchone():
            raise ApiError(404, f"Claim #{claim_id} does not exist.")
        members = net.network_members(cur, claim_id)
        hops = net.hop_distances(claim_id, members, net.fetch_edges(cur, members))
        return jsonify({
            "claim_id": claim_id,
            "network": [{"claim_id": m, "depth": hops.get(m)} for m in sorted(members, key=lambda m: hops.get(m, 99))],
            "size": len(members),
        })


@app.route("/api/networks")
def all_networks():
    with db() as conn, conn.cursor() as cur:
        claims, networks = claims_rows(cur)
        by_id = {c["claim_id"]: c for c in claims}
        results = [network_report(cur, members, by_id) for members in networks]
    results.sort(key=lambda n: (-(n["avg_risk_score"] or 0), n["claim_ids"][0]))
    return jsonify(results)


@app.route("/api/category-breakdown")
def category_breakdown():
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT category, COUNT(*) AS count, COALESCE(SUM(claim_amount), 0) AS total,
                   AVG(risk_score) FILTER (WHERE analyzed_at IS NOT NULL) AS avg_risk
            FROM claims GROUP BY category ORDER BY category
        """)
        return jsonify({r["category"]: {
            "count": r["count"], "total": float(r["total"]),
            "avg_risk": round(float(r["avg_risk"]), 1) if r["avg_risk"] is not None else None,
        } for r in cur.fetchall()})


@app.route("/api/entities")
def list_entities():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT claimant_id, full_name, phone FROM claimants ORDER BY claimant_id DESC LIMIT 500")
        claimants = cur.fetchall()
        cur.execute("SELECT vehicle_id, vin, plate_number, make, model FROM vehicles ORDER BY vehicle_id DESC LIMIT 500")
        vehicles = cur.fetchall()
        cur.execute("SELECT garage_id, name, address FROM garages ORDER BY garage_id DESC LIMIT 500")
        garages = cur.fetchall()
        cur.execute("SELECT doctor_id, full_name, clinic_name FROM doctors ORDER BY doctor_id DESC LIMIT 500")
        doctors = cur.fetchall()
        cur.execute("SELECT witness_id, full_name, phone FROM witnesses ORDER BY witness_id DESC LIMIT 500")
        witnesses = cur.fetchall()
    return jsonify({"claimants": claimants, "vehicles": vehicles, "garages": garages,
                    "doctors": doctors, "witnesses": witnesses})


@app.route("/api/customers/<int:claimant_id>")
def customer_detail(claimant_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT claimant_id, full_name, phone, address, date_of_birth FROM claimants "
                    "WHERE claimant_id = %s", (claimant_id,))
        row = cur.fetchone()
        if not row:
            raise ApiError(404, f"Customer #{claimant_id} does not exist.")
        cur.execute("""
            SELECT p.policy_id, p.policy_number, p.premium_amount, p.start_date, p.end_date,
                   COUNT(c.claim_id) AS claims
            FROM policies p LEFT JOIN claims c ON c.policy_id = p.policy_id
            WHERE p.claimant_id = %s GROUP BY p.policy_id ORDER BY p.policy_id
        """, (claimant_id,))
        policies = [{
            "policy_id": p["policy_id"], "policy_number": p["policy_number"],
            "premium": float(p["premium_amount"]), "start_date": str(p["start_date"]),
            "end_date": str(p["end_date"]) if p["end_date"] else None, "claims": p["claims"],
        } for p in cur.fetchall()]
    out = dict(row)
    out["date_of_birth"] = str(row["date_of_birth"]) if row["date_of_birth"] else None
    out["policies"] = policies
    return jsonify(out)


# ============================================================================
# Validation helpers — each records a field error instead of guessing
# ============================================================================

class Errors:
    """Collects every problem in a request so the user sees all of them at
    once. The response code is the most basic problem found: malformed or
    missing input (400) before unknown IDs (422) before conflicts (409)."""
    def __init__(self):
        self.items = []

    def add(self, field, message, code=400):
        self.items.append({"field": field, "message": message, "code": code})

    def has(self, field):
        return any(e["field"] == field for e in self.items)

    def raise_if_any(self):
        if self.items:
            codes = {e["code"] for e in self.items}
            status = 400 if 400 in codes else 422 if 422 in codes else 409
            raise ApiError(status, " ".join(e["message"] for e in self.items), self.items)


def get_text(src, key, field, errs, maxlen, required=False):
    v = src.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            errs.add(field, f"{field} is required.")
        return None
    v = re.sub(r"\s+", " ", str(v)).strip()
    if len(v) > maxlen:
        errs.add(field, f"{field} must be at most {maxlen} characters.")
    return v


def get_money(src, key, field, errs, required=True):
    v = src.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            errs.add(field, f"{field} is required.")
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        errs.add(field, f"{field} must be a number.")
        return None
    if math.isnan(x) or math.isinf(x) or x <= 0:
        errs.add(field, f"{field} must be greater than 0.")
        return None
    if x > MAX_MONEY:
        errs.add(field, f"{field} is too large.")
        return None
    return round(x, 2)


def get_date(src, key, field, errs, required=False):
    v = src.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            errs.add(field, f"{field} is required.")
        return None
    try:
        return date.fromisoformat(str(v).strip())
    except ValueError:
        errs.add(field, f"{field} must be a valid date in YYYY-MM-DD format.")
        return None


def get_id(src, key, field, errs):
    v = src.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    try:
        i = int(str(v).strip().lstrip("#"))
        if i <= 0:
            raise ValueError
        return i
    except ValueError:
        errs.add(field, f"{field} must be a positive whole number (got '{v}').")
        return None


def phone_key(p):
    d = re.sub(r"\D", "", p or "")
    return d[-10:] if len(d) >= 10 else d


def loose(s):
    return re.sub(r"\s+", " ", (s or "")).strip().casefold()


def vin_key(v):
    return re.sub(r"[^A-Za-z0-9]", "", v or "").upper()


def conflict(errs, field, entity, stored, entered):
    errs.add(field, f"{entity} is stored with {field.split('.')[-1]} '{stored}', but '{entered}' was entered. "
                    "Existing records are not changed by a claim submission — clear the field, use the stored "
                    "value, or create a new record instead.", 409)


def missing(errs, field, label, eid):
    errs.add(field, f"{label} #{eid} does not exist.", 422)


# ============================================================================
# The New Insurance Claim workflow
# ============================================================================

def resolve_customer(cur, customer, errs, warnings):
    cid = get_id(customer, "claimant_id", "customer.claimant_id", errs)
    name = get_text(customer, "full_name", "customer.full_name", errs, 120)
    phone = get_text(customer, "phone", "customer.phone", errs, 20)
    address = get_text(customer, "address", "customer.address", errs, 255)
    dob = get_date(customer, "date_of_birth", "customer.date_of_birth", errs)

    if cid is not None:
        cur.execute("SELECT * FROM claimants WHERE claimant_id = %s FOR SHARE", (cid,))
        row = cur.fetchone()
        if not row:
            missing(errs, "customer.claimant_id", "Customer", cid)
            return {"claimant_id": None, "new": False, "row": {}}
        label = f"Customer #{cid} ({row['full_name']})"
        if name and loose(name) != loose(row["full_name"]):
            conflict(errs, "customer.full_name", label, row["full_name"], name)
        if phone and phone_key(phone) != phone_key(row["phone"]):
            conflict(errs, "customer.phone", label, row["phone"], phone)
        if address and loose(address) != loose(row["address"]):
            conflict(errs, "customer.address", label, row["address"], address)
        if dob and row["date_of_birth"] and dob != row["date_of_birth"]:
            conflict(errs, "customer.date_of_birth", label, row["date_of_birth"], dob)
        return {"claimant_id": cid, "new": False, "row": dict(row)}

    for field, v in (("customer.full_name", name), ("customer.phone", phone), ("customer.address", address)):
        if not v:
            errs.add(field, f"{field} is required for a new customer (or give an existing customer.claimant_id).")
    if name and not re.search(r"[^\W\d_]", name):
        errs.add("customer.full_name", "customer.full_name must contain letters.")
    if phone and not 7 <= len(re.sub(r"\D", "", phone)) <= 15:
        errs.add("customer.phone", "customer.phone must contain 7 to 15 digits.")
    if dob and (dob >= date.today() or dob.year < 1900):
        errs.add("customer.date_of_birth", "customer.date_of_birth must be a past date after 1900.")
    return {"claimant_id": None, "new": True,
            "row": {"full_name": name, "phone": phone, "address": address, "date_of_birth": dob}}


def resolve_policy(cur, policy, cust, errs):
    number = get_text(policy, "policy_number", "policy.policy_number", errs, 40)
    premium = get_money(policy, "premium", "policy.premium", errs, required=False)
    start = get_date(policy, "start_date", "policy.start_date", errs)
    end = get_date(policy, "end_date", "policy.end_date", errs)

    if number:
        cur.execute("SELECT * FROM policies WHERE upper(policy_number) = upper(%s)", (number,))
        existing = cur.fetchone()
        if existing:
            if existing["claimant_id"] != cust["claimant_id"]:
                errs.add("policy.policy_number",
                         f"Policy number '{existing['policy_number']}' already exists and belongs to customer "
                         f"#{existing['claimant_id']}. Policy numbers are unique.", 409)
                return {"policy_id": None, "policy_number": number, "premium": premium,
                        "start_date": start, "end_date": end, "reused": False}
            label = f"Policy {existing['policy_number']}"
            if premium is not None and abs(premium - float(existing["premium_amount"])) > 0.005:
                conflict(errs, "policy.premium", label, float(existing["premium_amount"]), premium)
            if start and start != existing["start_date"]:
                conflict(errs, "policy.start_date", label, existing["start_date"], start)
            if end and end != existing["end_date"]:
                conflict(errs, "policy.end_date", label, existing["end_date"], end)
            return {"policy_id": existing["policy_id"], "policy_number": existing["policy_number"],
                    "premium": float(existing["premium_amount"]), "start_date": existing["start_date"],
                    "end_date": existing["end_date"], "reused": True}

    if premium is None and not errs.has("policy.premium"):
        errs.add("policy.premium", "policy.premium is required for a new policy.")
    if start is None and not errs.has("policy.start_date"):
        errs.add("policy.start_date", "policy.start_date is required for a new policy.")
    if start and end and end < start:
        errs.add("policy.end_date", f"policy.end_date ({end}) is before policy.start_date ({start}).")
    return {"policy_id": None, "policy_number": number, "premium": premium,
            "start_date": start, "end_date": end, "reused": False}


def resolve_simple_entity(cur, src, errs, warnings, *, section, table, id_col, name_col, name_len,
                          extra_cols, label):
    """Garage / doctor: reuse by ID (descriptive fields must agree), else reuse
    an exact name + phone/address match, else create. A name match alone is
    never treated as the same business/person."""
    eid = get_id(src, id_col, f"{section}.{id_col}", errs)
    name = get_text(src, name_col, f"{section}.{name_col}", errs, name_len)
    extras = {c: get_text(src, c, f"{section}.{c}", errs, n) for c, n in extra_cols.items()}

    if eid is not None:
        cur.execute(f"SELECT * FROM {table} WHERE {id_col} = %s", (eid,))
        row = cur.fetchone()
        if not row:
            missing(errs, f"{section}.{id_col}", label, eid)
            return None
        if name and loose(name) != loose(row[name_col]):
            conflict(errs, f"{section}.{name_col}", f"{label} #{eid}", row[name_col], name)
        for c, v in extras.items():
            if v and row.get(c) and loose(v) != loose(row[c]):
                conflict(errs, f"{section}.{c}", f"{label} #{eid}", row[c], v)
        return {"id": eid, "new": False}

    if not name:
        if any(extras.values()):
            errs.add(f"{section}.{name_col}", f"{section}.{name_col} is required to record a new {label.lower()}.")
        return None
    cur.execute(f"SELECT * FROM {table} WHERE lower({name_col}) = lower(%s)", (name,))
    same_name = cur.fetchall()
    for row in same_name:
        phone_match = extras.get("phone") and row.get("phone") and phone_key(extras["phone"]) == phone_key(row["phone"])
        other = [c for c in extras if c != "phone"]
        other_match = any(extras.get(c) and row.get(c) and loose(extras[c]) == loose(row[c]) for c in other)
        if phone_match or other_match:
            return {"id": row[id_col], "new": False, "matched": True}
    if same_name:
        ids = ", ".join(f"#{r[id_col]}" for r in same_name)
        warnings.append(f"A new {label.lower()} '{name}' was created. Existing {label.lower()}(s) {ids} have the "
                        f"same name but no matching contact details, so they were not assumed to be the same. "
                        f"Select an ID instead if it is the same {label.lower()}.")
    return {"id": None, "new": True, "values": {name_col: name, **extras}}


def resolve_vehicle(cur, src, errs):
    vid = get_id(src, "vehicle_id", "vehicle.vehicle_id", errs)
    vin = get_text(src, "vin", "vehicle.vin", errs, 32)
    others = {c: get_text(src, c, f"vehicle.{c}", errs, n)
              for c, n in (("plate_number", 20), ("make", 60), ("model", 60))}

    def check(row, label):
        if vin and vin_key(vin) != vin_key(row["vin"]):
            conflict(errs, "vehicle.vin", label, row["vin"], vin)
        for c, v in others.items():
            if v and row.get(c) and loose(v) != loose(row[c]):
                conflict(errs, f"vehicle.{c}", label, row[c], v)

    if vid is not None:
        cur.execute("SELECT * FROM vehicles WHERE vehicle_id = %s", (vid,))
        row = cur.fetchone()
        if not row:
            missing(errs, "vehicle.vehicle_id", "Vehicle", vid)
            return None
        check(row, f"Vehicle #{vid}")
        return {"id": vid, "new": False}
    if not vin:
        if any(others.values()):
            errs.add("vehicle.vin", "vehicle.vin is required to record a new vehicle (VIN is unique and mandatory).")
        return None
    cur.execute("SELECT * FROM vehicles WHERE upper(regexp_replace(vin, '[^A-Za-z0-9]', '', 'g')) = %s",
                (vin_key(vin),))
    row = cur.fetchone()
    if row:
        check(row, f"Vehicle #{row['vehicle_id']} (VIN {row['vin']})")
        return {"id": row["vehicle_id"], "new": False, "matched": True}
    return {"id": None, "new": True, "values": {"vin": vin.strip().upper(), **others}}


def resolve_witnesses(cur, witnesses, errs):
    if not isinstance(witnesses, list):
        errs.add("witnesses", "witnesses must be a list.")
        return []
    out = []
    for i, w in enumerate(witnesses):
        field = f"witnesses[{i}]"
        if not isinstance(w, dict):
            errs.add(field, f"{field} must be an object.")
            continue
        wid = get_id(w, "witness_id", f"{field}.witness_id", errs)
        name = get_text(w, "full_name", f"{field}.full_name", errs, 120)
        phone = get_text(w, "phone", f"{field}.phone", errs, 20)
        address = get_text(w, "address", f"{field}.address", errs, 255)
        if wid is not None:
            cur.execute("SELECT * FROM witnesses WHERE witness_id = %s", (wid,))
            row = cur.fetchone()
            if not row:
                missing(errs, f"{field}.witness_id", "Witness", wid)
                continue
            if name and loose(name) != loose(row["full_name"]):
                conflict(errs, f"{field}.full_name", f"Witness #{wid}", row["full_name"], name)
            if phone and row["phone"] and phone_key(phone) != phone_key(row["phone"]):
                conflict(errs, f"{field}.phone", f"Witness #{wid}", row["phone"], phone)
            out.append({"id": wid, "new": False})
            continue
        if not name:
            if phone or address:
                errs.add(f"{field}.full_name", f"{field}.full_name is required to record a new witness.")
            continue
        match = None
        if phone:
            cur.execute("SELECT witness_id, phone FROM witnesses WHERE lower(full_name) = lower(%s)", (name,))
            match = next((r["witness_id"] for r in cur.fetchall() if phone_key(r["phone"]) == phone_key(phone)), None)
        out.append({"id": match, "new": match is None,
                    "values": {"full_name": name, "phone": phone, "address": address}})
    return out


def insert_entity(cur, table, id_col, values):
    cols = list(values)
    cur.execute(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
                f"RETURNING {id_col}", [values[c] for c in cols])
    return cur.fetchone()[id_col]


def save_claim(cur, data, submission_key):
    """Validates the whole request, then writes customer, policy, entities,
    claim and witness links in the caller's transaction. Raises ApiError
    without writing anything if any field is invalid."""
    customer = data.get("customer") or {}
    policy = data.get("policy") or {}
    claim = data.get("claim") or {}
    errs, warnings = Errors(), []

    cust = resolve_customer(cur, customer, errs, warnings)
    pol = resolve_policy(cur, policy, cust, errs)
    amount = get_money(claim, "amount", "claim.amount", errs)
    category = (get_text(claim, "category", "claim.category", errs, 60, required=True) or "").lower() or None
    if category and category not in CATEGORIES:
        errs.add("claim.category", f"claim.category must be one of: {', '.join(CATEGORIES)}.")
    incident = get_date(claim, "incident_date", "claim.incident_date", errs, required=True)
    description = get_text(claim, "description", "claim.description", errs, 4000)

    vehicle = resolve_vehicle(cur, data.get("vehicle") or {}, errs)
    garage = resolve_simple_entity(cur, data.get("garage") or {}, errs, warnings, section="garage",
                                   table="garages", id_col="garage_id", name_col="name", name_len=150,
                                   extra_cols={"address": 255, "phone": 20}, label="Garage")
    doctor = resolve_simple_entity(cur, data.get("doctor") or {}, errs, warnings, section="doctor",
                                   table="doctors", id_col="doctor_id", name_col="full_name", name_len=120,
                                   extra_cols={"clinic_name": 150, "phone": 20}, label="Doctor")
    witnesses = resolve_witnesses(cur, data.get("witnesses") or [], errs)

    if incident:
        if incident > date.today():
            errs.add("claim.incident_date", f"claim.incident_date ({incident}) is in the future.")
        if pol["start_date"] and incident < pol["start_date"]:
            errs.add("claim.incident_date", f"Incident date {incident} is before the policy start date "
                                            f"{pol['start_date']} — the policy did not cover it yet.")
        if pol["end_date"] and incident > pol["end_date"]:
            errs.add("claim.incident_date", f"Incident date {incident} is after the policy end date "
                                            f"{pol['end_date']} — the policy had expired.")
        dob = cust["row"].get("date_of_birth")
        if dob and incident < dob:
            errs.add("customer.date_of_birth", "The customer's date of birth is after the incident date.")

    errs.raise_if_any()

    # ---- everything is valid: write it ----
    claimant_id = cust["claimant_id"]
    if cust["new"]:
        claimant_id = insert_entity(cur, "claimants", "claimant_id", cust["row"])

    policy_id, policy_number = pol["policy_id"], pol["policy_number"]
    if not pol["reused"]:
        temp_number = policy_number or f"TMP-{uuid.uuid4().hex[:20]}"
        try:
            cur.execute("SAVEPOINT policy_insert")
            policy_id = insert_entity(cur, "policies", "policy_id", {
                "claimant_id": claimant_id, "policy_number": temp_number, "premium_amount": pol["premium"],
                "start_date": pol["start_date"], "end_date": pol["end_date"]})
        except psycopg2.errors.UniqueViolation:
            cur.execute("ROLLBACK TO SAVEPOINT policy_insert")
            raise ApiError(409, f"Policy number '{policy_number}' is already in use.",
                           [{"field": "policy.policy_number", "message": "Duplicate policy number."}])
        if not policy_number:
            policy_number = f"POL-{policy_id}"
            cur.execute("UPDATE policies SET policy_number = %s WHERE policy_id = %s", (policy_number, policy_id))

    def ensure(ent, table, id_col):
        if ent is None:
            return None
        return ent["id"] if not ent["new"] else insert_entity(cur, table, id_col, ent["values"])

    vehicle_id = ensure(vehicle, "vehicles", "vehicle_id")
    garage_id = ensure(garage, "garages", "garage_id")
    doctor_id = ensure(doctor, "doctors", "doctor_id")
    witness_ids = sorted({ensure(w, "witnesses", "witness_id") for w in witnesses})

    claim_id = insert_claim_row(cur, {
        "policy_id": policy_id, "vehicle_id": vehicle_id, "garage_id": garage_id, "doctor_id": doctor_id,
        "claim_amount": amount, "incident_date": incident, "category": category, "status": "open",
        "description": description, "submission_key": submission_key,
    })
    for wid in witness_ids:
        cur.execute("INSERT INTO claim_witnesses (claim_id, witness_id) VALUES (%s, %s)", (claim_id, wid))

    return {
        "claim_id": claim_id, "claimant_id": claimant_id, "customer_new": cust["new"],
        "policy_id": policy_id, "policy_number": policy_number, "policy_reused": pol["reused"],
        "vehicle_id": vehicle_id, "garage_id": garage_id, "doctor_id": doctor_id,
        "witness_ids": witness_ids, "warnings": warnings,
    }


def insert_claim_row(cur, values):
    return insert_entity(cur, "claims", "claim_id", values)


def run_analysis(claim_id, trigger):
    """Separate transaction from the save. On failure the claim stays saved
    and the error is recorded on it so the UI can offer a retry."""
    try:
        with db() as conn, conn.cursor() as cur:
            result = rf.analyse_portfolio(cur, trigger)
            conn.commit()
        return True, None, result
    except Exception as e:  # analysis must never undo a committed claim
        msg = f"{type(e).__name__}: {e}".strip()[:1000]
        try:
            with db() as conn, conn.cursor() as cur:
                cur.execute("UPDATE claims SET analysis_error = %s WHERE claim_id = %s", (msg, claim_id))
                conn.commit()
        except Exception:
            pass
        return False, msg, None


def submission_response(claim_id, saved, analysis_ok, analysis_error, duplicate, status_code):
    report, report_error = None, None
    try:
        with db() as conn, conn.cursor() as cur:
            report = claim_report(cur, claim_id)
    except Exception as e:
        report_error = f"{type(e).__name__}: {e}"
    claim = (report or {}).get("claim") or {}
    body = {
        "saved": True,
        "duplicate": duplicate,
        "claim_id": claim_id,
        "claimant_id": claim.get("claimant_id", saved.get("claimant_id")),
        "customer_name": claim.get("claimant"),
        "policy_id": claim.get("policy_id", saved.get("policy_id")),
        "policy_number": claim.get("policy_number", saved.get("policy_number")),
        "customer_new": saved.get("customer_new"),
        "policy_reused": saved.get("policy_reused"),
        "warnings": saved.get("warnings", []),
        "analysis_ok": analysis_ok and report is not None and claim.get("analyzed", False),
        "analysis_error": analysis_error or report_error or claim.get("analysis_error"),
        "retry_url": f"/api/claims/{claim_id}/analyze",
        "report": report,
    }
    return jsonify(body), status_code


@app.route("/api/claims/submit", methods=["POST"])
def submit_claim():
    """
    Body:
    {
      "submission_key": "<uuid generated once per form submission>",
      "customer":  {"claimant_id"} OR {"full_name", "phone", "address", "date_of_birth"?},
      "policy":    {"policy_number"?, "premium", "start_date", "end_date"?}
                   (an existing policy number of the same customer is reused),
      "claim":     {"amount", "category", "incident_date", "description"?},
      "vehicle":   {"vehicle_id"} OR {"vin", "plate_number"?, "make"?, "model"?},
      "garage":    {"garage_id"} OR {"name", "address"?, "phone"?},
      "doctor":    {"doctor_id"} OR {"full_name", "clinic_name"?, "phone"?},
      "witnesses": [{"witness_id"} OR {"full_name", "phone"?, "address"?}, ...]
    }
    201: saved (analysis may still have failed — see analysis_ok/analysis_error)
    200: duplicate submission_key — the claim that already exists is returned
    400 invalid fields, 409 duplicate/conflicting records, 422 unknown IDs, 503 DB down
    """
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError(400, "Request body must be a JSON object.")
    key = (data.get("submission_key") or request.headers.get("Idempotency-Key") or "").strip() or None
    if key and len(key) > 64:
        raise ApiError(400, "submission_key must be at most 64 characters.")
    body = {k: v for k, v in data.items() if k != "submission_key"}
    fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()

    def replay(cur):
        """A reused key is only a duplicate if the body is the same; otherwise
        the new values would be dropped without the caller knowing."""
        cur.execute("SELECT claim_id, submission_hash FROM claims WHERE submission_key = %s", (key,))
        row = cur.fetchone()
        if not row:
            return None
        if row["submission_hash"] not in (None, fingerprint):
            raise ApiError(409, f"This submission key was already used to save Claim #{row['claim_id']} with "
                                f"different values. Nothing new was saved and Claim #{row['claim_id']} is unchanged. "
                                f"Submit again to file the edited form as a separate claim.",
                           [{"field": "submission_key", "message": "Reused with a different request body.",
                             "claim_id": row["claim_id"]}])
        return submission_response(row["claim_id"], {}, True, None, True, 200)

    if key:
        with db() as conn, conn.cursor() as cur:
            hit = replay(cur)
        if hit:
            return hit

    with db() as conn:
        try:
            with conn.cursor() as cur:
                saved = save_claim(cur, data, key)
                cur.execute("UPDATE claims SET submission_hash = %s WHERE claim_id = %s",
                            (fingerprint, saved["claim_id"]))
            conn.commit()
        except psycopg2.errors.UniqueViolation as e:
            conn.rollback()
            if key and "submission_key" in str(e):
                with conn.cursor() as cur:
                    hit = replay(cur)
                if hit:
                    return hit
            raise ApiError(409, f"Duplicate record: {e.diag.message_detail or e}")
        except ApiError:
            conn.rollback()
            raise
        except psycopg2.Error as e:
            conn.rollback()
            raise ApiError(500, f"The claim was NOT saved — the database rejected the transaction "
                                f"and every step was rolled back ({type(e).__name__}: {str(e).strip()}).")

    ok, err, _ = run_analysis(saved["claim_id"], f"claim #{saved['claim_id']} submitted")
    return submission_response(saved["claim_id"], saved, ok, err, False, 201)


@app.route("/api/claims/<int:claim_id>/analyze", methods=["POST"])
def reanalyze(claim_id):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM claims WHERE claim_id = %s", (claim_id,))
        if not cur.fetchone():
            raise ApiError(404, f"Claim #{claim_id} does not exist.")
    ok, err, _ = run_analysis(claim_id, f"retry for claim #{claim_id}")
    return submission_response(claim_id, {}, ok, err, False, 200 if ok else 500)


@app.route("/api/claims/<int:claim_id>/status", methods=["POST"])
def update_status(claim_id):
    new_status = (request.get_json(silent=True) or {}).get("status")
    if new_status not in CASE_STATUSES:
        raise ApiError(400, f"status must be one of: {', '.join(CASE_STATUSES)}.")
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE claims SET status = %s WHERE claim_id = %s RETURNING claim_id", (new_status, claim_id))
        if not cur.fetchone():
            raise ApiError(404, f"Claim #{claim_id} does not exist.")
        conn.commit()
    return jsonify({"claim_id": claim_id, "status": new_status})


def startup():
    if not DATABASE_URL:
        print("[startup] DATABASE_URL is not set; every endpoint will return 503 until it is.")
        return
    with db() as conn:
        apply_migrations(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM claims WHERE analyzed_at IS NULL")
            pending = cur.fetchone()["n"]
            cur.execute("SELECT COUNT(*) AS n FROM analysis_runs")
            never_run = cur.fetchone()["n"] == 0
    if pending or never_run:
        with db() as conn, conn.cursor() as cur:
            rf.analyse_portfolio(cur, "startup (migration / pending claims)")
            conn.commit()


if __name__ == "__main__":
    startup()
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", "5000")))
