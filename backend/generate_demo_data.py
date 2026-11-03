"""
Generates a realistic demo dataset (SQLite, mirrors the PostgreSQL schema),
embeds a deliberate fraud ring plus a few lone anomalies, then runs:
  1. Recursive CTE network detection (shared garage/doctor/vehicle/witness;
     the SQLite snapshot has no phone/address/customer edges because every
     synthetic customer here is unique)
  2. Composite risk scoring and 3. Isolation Forest — both through
     risk_features.py, the same code the PostgreSQL API uses.

Output: backend/demo.db (SQLite) and frontend/data.json — an offline reference
snapshot. The dashboard itself reads the live API, not this file.
"""
import json
import os
import random
import sqlite3
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import risk_features as rf  # noqa: E402

random.seed(42)
DB_PATH = os.path.join(HERE, "demo.db")
OUT_PATH = os.path.join(HERE, "..", "frontend", "data.json")

# ---------------------------------------------------------------------------
# 1. Schema (SQLite dialect, same shape as schema.sql)
# ---------------------------------------------------------------------------
conn = sqlite3.connect(DB_PATH)
conn.executescript("""
DROP TABLE IF EXISTS claim_witnesses;
DROP TABLE IF EXISTS claims;
DROP TABLE IF EXISTS policies;
DROP TABLE IF EXISTS claimants;
DROP TABLE IF EXISTS vehicles;
DROP TABLE IF EXISTS garages;
DROP TABLE IF EXISTS doctors;
DROP TABLE IF EXISTS witnesses;

CREATE TABLE claimants (
    claimant_id INTEGER PRIMARY KEY, full_name TEXT, phone TEXT,
    address TEXT, date_of_birth TEXT
);
CREATE TABLE vehicles (
    vehicle_id INTEGER PRIMARY KEY, vin TEXT, make TEXT, model TEXT, plate_number TEXT
);
CREATE TABLE garages (
    garage_id INTEGER PRIMARY KEY, name TEXT, address TEXT, phone TEXT
);
CREATE TABLE doctors (
    doctor_id INTEGER PRIMARY KEY, full_name TEXT, clinic_name TEXT, phone TEXT
);
CREATE TABLE witnesses (
    witness_id INTEGER PRIMARY KEY, full_name TEXT, phone TEXT, address TEXT
);
CREATE TABLE policies (
    policy_id INTEGER PRIMARY KEY, claimant_id INTEGER, policy_number TEXT,
    premium_amount REAL, start_date TEXT, end_date TEXT
);
CREATE TABLE claims (
    claim_id INTEGER PRIMARY KEY, policy_id INTEGER, vehicle_id INTEGER,
    garage_id INTEGER, doctor_id INTEGER, claim_amount REAL,
    incident_date TEXT, category TEXT, status TEXT, risk_score REAL
);
CREATE TABLE claim_witnesses (
    claim_id INTEGER, witness_id INTEGER, PRIMARY KEY (claim_id, witness_id)
);
""")

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
    return f"9{random.randint(100000000,999999999)}"

def rand_address():
    return f"{random.randint(1,400)} {random.choice(STREETS)}"

def rand_date(start_year=2024):
    d = date(start_year, 1, 1) + timedelta(days=random.randint(0, 600))
    return d.isoformat()

cur = conn.cursor()

# ---------------------------------------------------------------------------
# 2. Legit garages / doctors / vehicles / witnesses (background population)
# ---------------------------------------------------------------------------
for i in range(1, 36):
    cur.execute("INSERT INTO garages VALUES (?,?,?,?)",
                (i, f"{random.choice(LAST)} Auto Works", rand_address(), rand_phone()))
for i in range(1, 28):
    cur.execute("INSERT INTO doctors VALUES (?,?,?,?)",
                (i, f"Dr. {rand_name()}", f"{random.choice(LAST)} Clinic", rand_phone()))
for i in range(1, 130):
    cur.execute("INSERT INTO vehicles VALUES (?,?,?,?,?)",
                (i, f"VIN{100000+i}", random.choice(["Toyota","Honda","Ford","Hyundai","Suzuki"]),
                 random.choice(["Sedan","Hatchback","SUV","Pickup"]), f"KA{random.randint(10,99)}AB{random.randint(1000,9999)}"))
for i in range(1, 55):
    cur.execute("INSERT INTO witnesses VALUES (?,?,?,?)",
                (i, rand_name(), rand_phone(), rand_address()))

# ---------------------------------------------------------------------------
# 3. The fraud ring: one garage + one doctor + a small pool of vehicles /
#    witnesses reused deliberately across 6 different claimants.
# ---------------------------------------------------------------------------
RING_GARAGE = 99
RING_DOCTOR = 99
cur.execute("INSERT INTO garages VALUES (?,?,?,?)", (RING_GARAGE, "QuickFix Auto Care", "12 Industrial Yard", "9888800001"))
cur.execute("INSERT INTO doctors VALUES (?,?,?,?)", (RING_DOCTOR, "Dr. R. Kulkarni", "Sunrise Ortho Clinic", "9888800002"))
RING_VEHICLES = [901, 902]
for vid in RING_VEHICLES:
    cur.execute("INSERT INTO vehicles VALUES (?,?,?,?,?)",
                (vid, f"VIN{vid}", "Toyota", "Sedan", f"KA05RG{vid}"))
RING_WITNESSES = [901, 902]
for wid in RING_WITNESSES:
    cur.execute("INSERT INTO witnesses VALUES (?,?,?,?)",
                (wid, rand_name(), rand_phone(), "12 Industrial Yard"))

claimant_id = 1
policy_id = 1
claim_id = 1

def new_claimant():
    global claimant_id
    cur.execute("INSERT INTO claimants VALUES (?,?,?,?,?)",
                (claimant_id, rand_name(), rand_phone(), rand_address(),
                 rand_date(1970)))
    cid = claimant_id
    claimant_id += 1
    return cid

def new_policy(cid, premium):
    global policy_id
    cur.execute("INSERT INTO policies VALUES (?,?,?,?,?,?)",
                (policy_id, cid, f"POL-{2024000+policy_id}", premium,
                 "2024-01-01", "2025-12-31"))
    pid = policy_id
    policy_id += 1
    return pid

def new_claim(pid, vehicle_id, garage_id, doctor_id, amount, category, status="open"):
    global claim_id
    cur.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?,?)",
                (claim_id, pid, vehicle_id, garage_id, doctor_id, amount,
                 rand_date(), category, status, None))
    cid = claim_id
    claim_id += 1
    return cid

# --- Ring claims: 6 different people, same garage/doctor/vehicle pool ------
ring_claim_ids = []
for i in range(6):
    cid = new_claimant()
    premium = random.uniform(4000, 6000)
    pid = new_policy(cid, premium)
    amount = random.uniform(38000, 62000)          # abnormally high vs premium
    veh = RING_VEHICLES[i % 2]
    cl = new_claim(pid, veh, RING_GARAGE, RING_DOCTOR, amount,
                    random.choice(["collision", "medical"]), "flagged")
    cur.execute("INSERT INTO claim_witnesses VALUES (?,?)", (cl, RING_WITNESSES[i % 2]))
    ring_claim_ids.append(cl)

# --- Normal background claims (no unusual sharing) -------------------------
normal_claim_ids = []
for i in range(48):
    cid = new_claimant()
    premium = random.uniform(3000, 9000)
    pid = new_policy(cid, premium)
    category = random.choice(CATEGORIES)
    amount = premium * random.uniform(0.5, 3.5)
    veh = random.randint(1, 129)
    garage = random.randint(1, 35) if random.random() > 0.15 else None
    doctor = random.randint(1, 27) if category == "medical" else None
    cl = new_claim(pid, veh, garage, doctor, amount, category)
    if random.random() < 0.15:
        cur.execute("INSERT INTO claim_witnesses VALUES (?,?)", (cl, random.randint(1, 54)))
    normal_claim_ids.append(cl)

# --- A couple of lone high-value outliers (no ring, just unusual amount) ---
lone_outlier_ids = []
for i in range(3):
    cid = new_claimant()
    premium = random.uniform(3000, 5000)
    pid = new_policy(cid, premium)
    amount = premium * random.uniform(6, 9)   # very high ratio, but isolated
    cl = new_claim(pid, random.randint(1, 129), random.randint(1, 35), None,
                    amount, "theft", "under_review")
    lone_outlier_ids.append(cl)

conn.commit()

# ---------------------------------------------------------------------------
# 4. Recursive CTE — connected-claim network detection
# ---------------------------------------------------------------------------
conn.executescript("""
DROP VIEW IF EXISTS claim_edges;
CREATE VIEW claim_edges AS
    SELECT a.claim_id AS claim_a, b.claim_id AS claim_b, 'garage' AS link_type, a.garage_id AS shared_entity
    FROM claims a JOIN claims b ON a.garage_id = b.garage_id AND a.claim_id < b.claim_id
    WHERE a.garage_id IS NOT NULL
UNION ALL
    SELECT a.claim_id, b.claim_id, 'doctor', a.doctor_id
    FROM claims a JOIN claims b ON a.doctor_id = b.doctor_id AND a.claim_id < b.claim_id
    WHERE a.doctor_id IS NOT NULL
UNION ALL
    SELECT a.claim_id, b.claim_id, 'vehicle', a.vehicle_id
    FROM claims a JOIN claims b ON a.vehicle_id = b.vehicle_id AND a.claim_id < b.claim_id
    WHERE a.vehicle_id IS NOT NULL
UNION ALL
    SELECT cw1.claim_id, cw2.claim_id, 'witness', cw1.witness_id
    FROM claim_witnesses cw1 JOIN claim_witnesses cw2
      ON cw1.witness_id = cw2.witness_id AND cw1.claim_id < cw2.claim_id;
""")

all_claim_ids = [r[0] for r in cur.execute("SELECT claim_id FROM claims").fetchall()]
visited = set()
networks = []

for start in all_claim_ids:
    if start in visited:
        continue
    # same shape as networks.NETWORK_MEMBERS_SQL: UNION on claim_id alone ends
    # the recursion once no new claim is reached (no depth cap, cycle-safe)
    rows = cur.execute("""
        WITH RECURSIVE adjacency(src, dst) AS (
            SELECT claim_a, claim_b FROM claim_edges
            UNION SELECT claim_b, claim_a FROM claim_edges
        ), network(claim_id) AS (
            SELECT ?
            UNION
            SELECT a.dst FROM adjacency a JOIN network n ON a.src = n.claim_id
        )
        SELECT claim_id FROM network
    """, (start,)).fetchall()
    members = sorted(r[0] for r in rows)
    if len(members) > 1:
        networks.append(members)
    visited.update(members)

# de-duplicate identical networks
uniq_networks = []
seen = set()
for net in networks:
    key = tuple(net)
    if key not in seen:
        seen.add(key)
        uniq_networks.append(net)

claim_to_network = {}
for idx, net in enumerate(uniq_networks):
    for c in net:
        claim_to_network[c] = idx

# shared-entity evidence per network
def network_evidence(members):
    placeholders = ",".join("?" * len(members))
    rows = cur.execute(f"""
        SELECT link_type, shared_entity, claim_a, claim_b FROM claim_edges
        WHERE claim_a IN ({placeholders}) AND claim_b IN ({placeholders})
    """, members + members).fetchall()
    ev = {}
    for link_type, entity, a, b in rows:
        ev.setdefault(link_type, set()).add(entity)
    return {k: sorted(v) for k, v in ev.items()}

# ---------------------------------------------------------------------------
# 5. Risk scoring
# ---------------------------------------------------------------------------
claims_rows = cur.execute("""
    SELECT c.claim_id, c.policy_id, c.vehicle_id, c.garage_id, c.doctor_id,
           c.claim_amount, c.incident_date, c.category, c.status,
           p.premium_amount, p.claimant_id, cl.full_name
    FROM claims c
    JOIN policies p ON c.policy_id = p.policy_id
    JOIN claimants cl ON p.claimant_id = cl.claimant_id
""").fetchall()

garage_counts, doctor_counts, cat_totals, cat_counts = {}, {}, {}, {}
for r in claims_rows:
    if r[3]:
        garage_counts[r[3]] = garage_counts.get(r[3], 0) + 1
    if r[4]:
        doctor_counts[r[4]] = doctor_counts.get(r[4], 0) + 1
    cat_totals[r[7]] = cat_totals.get(r[7], 0) + r[5]
    cat_counts[r[7]] = cat_counts.get(r[7], 0) + 1

def net_size(cid):
    idx = claim_to_network.get(cid)
    return len(uniq_networks[idx]) if idx is not None else 1

# feature dicts in exactly the shape risk_features.portfolio_features() builds
records, feats, ids = [], {}, []
for r in claims_rows:
    (cid, pid, veh, garage, doctor, amount, inc_date, category, status,
     premium, claimant_id_, name) = r
    n_cat = cat_counts[category]
    cat_avg = cat_totals[category] / n_cat
    f = {
        "amount": amount, "premium": premium, "ratio": amount / premium if premium else None,
        "network_size": net_size(cid), "garage_id": garage,
        "garage_frequency": garage_counts.get(garage, 0) if garage else 0,
        "doctor_frequency": doctor_counts.get(doctor, 0) if doctor else 0,
        "category": category, "category_count": n_cat, "category_avg": cat_avg,
        "category_deviation": (amount - cat_avg) / cat_avg if n_cat >= rf.MIN_CATEGORY_HISTORY else None,
    }
    feats[cid] = f
    ids.append(cid)
    score = rf.risk_score(f)
    cur.execute("UPDATE claims SET risk_score = ? WHERE claim_id = ?", (score, cid))

    records.append({
        "claim_id": cid, "claimant": name, "amount": round(amount, 2),
        "premium": round(premium, 2), "ratio": round(f["ratio"], 2) if f["ratio"] is not None else None,
        "category": category, "status": status, "incident_date": inc_date,
        "garage_id": garage, "doctor_id": doctor, "vehicle_id": veh,
        "garage_frequency": f["garage_frequency"],
        "category_deviation": round(f["category_deviation"], 2) if f["category_deviation"] is not None else None,
        "network_id": claim_to_network.get(cid),
        "network_size": f["network_size"], "risk_score": score,
    })

conn.commit()

# ---------------------------------------------------------------------------
# 6. Isolation Forest — same features, preprocessing and settings as the API
# ---------------------------------------------------------------------------
X = rf.ml_matrix(ids, feats)
model, mean, std, threshold = rf.fit_isolation_forest(X)
flags, ml_scores = rf.score_isolation_forest(model, mean, std, X)

for rec, flag, ascore in zip(records, flags, ml_scores):
    rec["ml_anomaly"] = flag
    rec["ml_anomaly_score"] = ascore
    rec["recommendation"] = rf.recommend(rec["risk_score"], flag, ascore, threshold)[0]

# ---------------------------------------------------------------------------
# 7. Assemble network + evidence summaries, write dashboard JSON
# ---------------------------------------------------------------------------
network_summaries = []
for idx, members in enumerate(uniq_networks):
    evidence = network_evidence(members)
    member_records = [r for r in records if r["claim_id"] in members]
    total_amount = sum(m["amount"] for m in member_records)
    avg_risk = sum(m["risk_score"] for m in member_records) / len(member_records)
    network_summaries.append({
        "network_id": idx,
        "claim_ids": members,
        "size": len(members),
        "total_amount": round(total_amount, 2),
        "avg_risk_score": round(avg_risk, 1),
        "evidence": evidence,
        "claimants": [m["claimant"] for m in member_records],
    })

network_summaries.sort(key=lambda n: -n["avg_risk_score"])
records.sort(key=lambda r: -r["risk_score"])

suspicious = [r for r in records if r["recommendation"] in (rf.STATUS_REVIEW, rf.STATUS_INVESTIGATE)]
summary = {
    "total_claims": len(records),
    "total_networks": len(network_summaries),
    "requires_investigation": sum(1 for r in records if r["recommendation"] == rf.STATUS_INVESTIGATE),
    "suspicious_claims": len(suspicious),
    "high_risk_claims": sum(1 for r in records if r["risk_score"] >= rf.HIGH_RISK_THRESHOLD),
    "ml_anomalies": sum(1 for r in records if r["ml_anomaly"]),
    "ml_threshold": threshold,
    "total_claim_value": round(sum(r["amount"] for r in records), 2),
    "flagged_value": round(sum(r["amount"] for r in suspicious), 2),
}

garages_lookup = {r[0]: r[1] for r in cur.execute("SELECT garage_id, name FROM garages")}
doctors_lookup = {r[0]: r[1] for r in cur.execute("SELECT doctor_id, full_name FROM doctors")}

for r in records:
    r["garage_name"] = garages_lookup.get(r["garage_id"], "—")
    r["doctor_name"] = doctors_lookup.get(r["doctor_id"], "—") if r["doctor_id"] else "—"

for n in network_summaries:
    ev = n["evidence"]
    readable = []
    if "garage" in ev:
        readable += [f"Shared garage: {garages_lookup.get(g,'?')}" for g in ev["garage"]]
    if "doctor" in ev:
        readable += [f"Shared doctor: {doctors_lookup.get(d,'?')}" for d in ev["doctor"]]
    if "vehicle" in ev:
        readable += [f"Shared vehicle ID(s): {ev['vehicle']}"]
    if "witness" in ev:
        readable += [f"Shared witness ID(s): {ev['witness']}"]
    n["evidence_readable"] = readable

category_breakdown = {}
for cat in CATEGORIES:
    cat_claims = [r for r in records if r["category"] == cat]
    category_breakdown[cat] = {
        "count": len(cat_claims),
        "total": round(sum(c["amount"] for c in cat_claims), 2),
        "avg_risk": round(sum(c["risk_score"] for c in cat_claims) / len(cat_claims), 1) if cat_claims else 0,
    }

out = {
    "summary": summary,
    "claims": records,
    "networks": network_summaries,
    "category_breakdown": category_breakdown,
}

with open(OUT_PATH, "w") as f:
    json.dump(out, f, indent=2)

print("Summary:", json.dumps(summary, indent=2))
print(f"Networks detected: {len(network_summaries)}")
print(f"Wrote {OUT_PATH}")
conn.close()
