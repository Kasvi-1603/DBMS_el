"""
Seeds PostgreSQL `fraud_db` with the same demo dataset used by
generate_demo_data.py (SQLite), then runs the real pipeline against Postgres:

  1. Insert claimants, policies, vehicles, garages, doctors, witnesses,
     claims, claim_witnesses (same random seed -> same dataset shape).
  2. Network detection, risk scoring and Isolation Forest via the shared
     networks.py / risk_features.py — exactly what app.py runs.

DESTRUCTIVE: it TRUNCATES every table (all claims, customers and policies,
including ones filed through the dashboard) before seeding, so it refuses to
run without --reset.

Usage:
    export DATABASE_URL=postgresql://user:pass@localhost:5432/fraud_db
    python seed_postgres.py --reset
    python seed_postgres.py --reset --export-json ../frontend/data.json
"""
import argparse
import os
import random
import sys
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import risk_features  # noqa: E402
from app import apply_migrations  # noqa: E402

random.seed(42)

CATEGORIES = ["collision", "medical", "theft", "fire", "water_damage"]
FIRST = ["Aarav","Priya","Rohan","Ishita","Vikram","Neha","Karan","Sara",
         "Arjun","Meera","Dev","Ananya","Rahul","Divya","Sameer","Pooja",
         "Kabir","Tanya","Nikhil","Riya","Omar","Layla","Marco","Elena",
         "James","Grace","Liam","Zara","Yusuf","Nadia"]
LAST = ["Sharma","Verma","Iyer","Khan","Nair","Gupta","Reddy","Das",
        "Mehta","Chopra","Bose","Rao","Fernandes","D'Souza","Malik",
        "Kapoor","Joshi","Bhatt","Menon","Pillai"]
STREETS = ["MG Road","Park Street","Church Street","Lake View","Hill Road",
           "Station Road","Green Avenue","Sunset Blvd","River Side","Oak Lane"]


def rand_name():
    return f"{random.choice(FIRST)} {random.choice(LAST)}"


def rand_phone():
    return f"9{random.randint(100000000, 999999999)}"


def rand_address():
    return f"{random.randint(1, 400)} {random.choice(STREETS)}"


def rand_date(start_year=2024):
    d = date(start_year, 1, 1) + timedelta(days=random.randint(0, 600))
    return d.isoformat()


def connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Set DATABASE_URL first, e.g. postgresql://user:pass@localhost:5432/fraud_db")
    return psycopg2.connect(url, cursor_factory=psycopg2.extras.RealDictCursor)


# ---------------------------------------------------------------------------
# Step 1: clean slate (schema/views come from database/migrations)
# ---------------------------------------------------------------------------
def reset_schema(cur):
    cur.execute("""
        TRUNCATE claim_witnesses, claims, policies, claimants,
                 vehicles, garages, doctors, witnesses, analysis_runs RESTART IDENTITY CASCADE;
    """)


# ---------------------------------------------------------------------------
# Step 2: insert the same synthetic dataset as generate_demo_data.py
# ---------------------------------------------------------------------------
def seed_data(cur):
    for i in range(1, 36):
        cur.execute("INSERT INTO garages (garage_id, name, address, phone) VALUES (%s,%s,%s,%s)",
                    (i, f"{random.choice(LAST)} Auto Works", rand_address(), rand_phone()))
    for i in range(1, 28):
        cur.execute("INSERT INTO doctors (doctor_id, full_name, clinic_name, phone) VALUES (%s,%s,%s,%s)",
                    (i, f"Dr. {rand_name()}", f"{random.choice(LAST)} Clinic", rand_phone()))
    for i in range(1, 130):
        cur.execute("INSERT INTO vehicles (vehicle_id, vin, make, model, plate_number) VALUES (%s,%s,%s,%s,%s)",
                    (i, f"VIN{100000+i}", random.choice(["Toyota","Honda","Ford","Hyundai","Suzuki"]),
                     random.choice(["Sedan","Hatchback","SUV","Pickup"]),
                     f"KA{random.randint(10,99)}AB{random.randint(1000,9999)}"))
    for i in range(1, 55):
        cur.execute("INSERT INTO witnesses (witness_id, full_name, phone, address) VALUES (%s,%s,%s,%s)",
                    (i, rand_name(), rand_phone(), rand_address()))

    # --- the deliberate fraud ring ---
    RING_GARAGE, RING_DOCTOR = 99, 99
    cur.execute("INSERT INTO garages (garage_id, name, address, phone) VALUES (%s,%s,%s,%s)",
                (RING_GARAGE, "QuickFix Auto Care", "12 Industrial Yard", "9888800001"))
    cur.execute("INSERT INTO doctors (doctor_id, full_name, clinic_name, phone) VALUES (%s,%s,%s,%s)",
                (RING_DOCTOR, "Dr. R. Kulkarni", "Sunrise Ortho Clinic", "9888800002"))
    RING_VEHICLES = [901, 902]
    for vid in RING_VEHICLES:
        cur.execute("INSERT INTO vehicles (vehicle_id, vin, make, model, plate_number) VALUES (%s,%s,%s,%s,%s)",
                    (vid, f"VIN{vid}", "Toyota", "Sedan", f"KA05RG{vid}"))
    RING_WITNESSES = [901, 902]
    for wid in RING_WITNESSES:
        cur.execute("INSERT INTO witnesses (witness_id, full_name, phone, address) VALUES (%s,%s,%s,%s)",
                    (wid, rand_name(), rand_phone(), "12 Industrial Yard"))

    state = {"claimant_id": 1, "policy_id": 1, "claim_id": 1}

    def new_claimant():
        cid = state["claimant_id"]
        cur.execute("INSERT INTO claimants (claimant_id, full_name, phone, address, date_of_birth) VALUES (%s,%s,%s,%s,%s)",
                    (cid, rand_name(), rand_phone(), rand_address(), rand_date(1970)))
        state["claimant_id"] += 1
        return cid

    def new_policy(cid, premium):
        pid = state["policy_id"]
        cur.execute("INSERT INTO policies (policy_id, claimant_id, policy_number, premium_amount, start_date, end_date) VALUES (%s,%s,%s,%s,%s,%s)",
                    (pid, cid, f"POL-{2024000+pid}", premium, "2024-01-01", "2025-12-31"))
        state["policy_id"] += 1
        return pid

    def new_claim(pid, vehicle_id, garage_id, doctor_id, amount, category, status="open"):
        clid = state["claim_id"]
        cur.execute("""INSERT INTO claims
            (claim_id, policy_id, vehicle_id, garage_id, doctor_id, claim_amount, incident_date, category, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (clid, pid, vehicle_id, garage_id, doctor_id, amount, rand_date(), category, status))
        state["claim_id"] += 1
        return clid

    # ring claims: 6 different claimants sharing the ring garage/doctor/vehicles
    for i in range(6):
        cid = new_claimant()
        premium = random.uniform(4000, 6000)
        pid = new_policy(cid, premium)
        amount = random.uniform(38000, 62000)
        veh = RING_VEHICLES[i % 2]
        clid = new_claim(pid, veh, RING_GARAGE, RING_DOCTOR, amount,
                          random.choice(["collision", "medical"]), "flagged")
        cur.execute("INSERT INTO claim_witnesses (claim_id, witness_id) VALUES (%s,%s)",
                    (clid, RING_WITNESSES[i % 2]))

    # normal background claims
    for i in range(48):
        cid = new_claimant()
        premium = random.uniform(3000, 9000)
        pid = new_policy(cid, premium)
        category = random.choice(CATEGORIES)
        amount = premium * random.uniform(0.5, 3.5)
        veh = random.randint(1, 129)
        garage = random.randint(1, 35) if random.random() > 0.15 else None
        doctor = random.randint(1, 27) if category == "medical" else None
        clid = new_claim(pid, veh, garage, doctor, amount, category)
        if random.random() < 0.15:
            cur.execute("INSERT INTO claim_witnesses (claim_id, witness_id) VALUES (%s,%s)",
                        (clid, random.randint(1, 54)))

    # lone high-ratio outliers (no ring, just unusual amount)
    for i in range(3):
        cid = new_claimant()
        premium = random.uniform(3000, 5000)
        pid = new_policy(cid, premium)
        amount = premium * random.uniform(6, 9)
        new_claim(pid, random.randint(1, 129), random.randint(1, 35), None,
                  amount, "theft", "under_review")

    # keep the SERIAL sequences in sync with the explicit IDs we just inserted
    for table, col in [("garages", "garage_id"), ("doctors", "doctor_id"),
                        ("vehicles", "vehicle_id"), ("witnesses", "witness_id"),
                        ("claimants", "claimant_id"), ("policies", "policy_id"),
                        ("claims", "claim_id")]:
        cur.execute(f"SELECT setval(pg_get_serial_sequence('{table}','{col}'), (SELECT MAX({col}) FROM {table}))")


# ---------------------------------------------------------------------------
# Step 3: networks + risk scoring + Isolation Forest — the same function
# app.py runs after every submitted claim.
# ---------------------------------------------------------------------------
def score_all(cur):
    return risk_features.analyse_portfolio(cur, "seed_postgres.py")["networks"]


# ---------------------------------------------------------------------------
# Optional: export a data.json snapshot straight from Postgres, matching the
# shape the dashboard.html artifact expects (for parity / re-publishing).
# ---------------------------------------------------------------------------
def export_json(cur, networks, path):
    import json

    cur.execute("""
        SELECT c.claim_id, cl.full_name AS claimant, c.claim_amount AS amount,
               p.premium_amount AS premium, c.category, c.status, c.incident_date,
               c.risk_score, c.ml_anomaly, c.ml_anomaly_score,
               g.name AS garage_name, d.full_name AS doctor_name,
               c.garage_id, c.doctor_id, c.vehicle_id
        FROM claims c
        JOIN policies p ON c.policy_id = p.policy_id
        JOIN claimants cl ON p.claimant_id = cl.claimant_id
        LEFT JOIN garages g ON c.garage_id = g.garage_id
        LEFT JOIN doctors d ON c.doctor_id = d.doctor_id
        ORDER BY c.risk_score DESC
    """)
    claims = []
    claim_to_network = {m: i for i, members in enumerate(networks) for m in members}
    for r in cur.fetchall():
        d = dict(r)
        d["amount"] = float(d["amount"])
        d["premium"] = float(d["premium"])
        d["ratio"] = round(d["amount"] / d["premium"], 2) if d["premium"] else 0
        d["risk_score"] = float(d["risk_score"]) if d["risk_score"] is not None else None
        d["ml_anomaly_score"] = float(d["ml_anomaly_score"]) if d["ml_anomaly_score"] is not None else None
        d["network_id"] = claim_to_network.get(d["claim_id"])
        d["network_size"] = len(networks[d["network_id"]]) if d["network_id"] is not None else 1
        d["garage_name"] = d["garage_name"] or "—"
        d["doctor_name"] = d["doctor_name"] or "—"
        claims.append(d)

    with open(path, "w") as f:
        json.dump({"claims": claims, "networks_raw": networks}, f, indent=2, default=str)
    print(f"Exported {len(claims)} claims to {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true",
                        help="Required: confirms that every existing claim, customer and policy will be DELETED")
    parser.add_argument("--export-json", help="Also write a data.json snapshot to this path")
    args = parser.parse_args()
    if not args.reset:
        sys.exit("Refusing to run: this TRUNCATES all tables, including claims filed through the dashboard. "
                 "Back up first (pg_dump), then re-run with --reset.")

    conn = connect()
    try:
        apply_migrations(conn)
        with conn.cursor() as cur:
            print("Resetting schema...")
            reset_schema(cur)
            print("Seeding synthetic dataset...")
            seed_data(cur)
            conn.commit()

            print("Running recursive CTE fraud-ring detection + risk scoring + Isolation Forest...")
            networks = score_all(cur)
            print(f"  -> {len(networks)} connected networks found")
            conn.commit()

            if args.export_json:
                export_json(cur, networks, args.export_json)

            cur.execute("""
                SELECT COUNT(*) AS total_claims,
                       COUNT(*) FILTER (WHERE risk_score >= %(ri)s) AS requires_investigation,
                       COUNT(*) FILTER (WHERE risk_score >= %(hi)s) AS high_risk_claims,
                       COUNT(*) FILTER (WHERE ml_anomaly) AS ml_anomalies,
                       COALESCE(SUM(claim_amount),0) AS total_claim_value
                FROM claims
            """, {"ri": risk_features.INVESTIGATION_THRESHOLD, "hi": risk_features.HIGH_RISK_THRESHOLD})
            print("Done. /api/summary should now show:")
            print(dict(cur.fetchone()))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
