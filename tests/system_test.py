"""
End-to-end system tests against the REAL PostgreSQL database (DATABASE_URL).

Every record this script creates is tagged: customer names start with "TEST",
policy numbers with "TEST-<run>", claim descriptions with "[LEDGER-TEST <run>]",
and new garages/doctors/witnesses/vehicles with "TEST". At the end those rows
(and only those rows) are deleted and the portfolio is re-analysed, unless
--keep is given. Existing claims are never modified except for the derived
analysis columns that every analysis run recomputes.

Failure scenarios (database error mid-transaction, analysis crash) are
simulated by temporarily replacing a function inside this process — the
application has no fault-injection code.

    python tests/system_test.py            # run, write tests/TEST_RESULTS.md, clean up
    python tests/system_test.py --keep     # keep the test claims for manual inspection
"""
import argparse
import json
import os
import sys
import uuid
from collections import deque
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import psycopg2  # noqa: E402
import app as appmod  # noqa: E402
import networks as net  # noqa: E402
import risk_features as rf  # noqa: E402

RUN = uuid.uuid4().hex[:6].upper()
TAG = f"[LEDGER-TEST {RUN}]"
client = appmod.app.test_client()
results = []
created_claims = []


def db_one(sql, params=None):
    with appmod.db() as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchone()


def db_all(sql, params=None):
    with appmod.db() as conn, conn.cursor() as cur:
        cur.execute(sql, params or ())
        return cur.fetchall()


def counts():
    return dict(db_one("""SELECT (SELECT COUNT(*) FROM claims) AS claims, (SELECT COUNT(*) FROM policies) AS policies,
                                 (SELECT COUNT(*) FROM claimants) AS claimants, (SELECT COUNT(*) FROM garages) AS garages"""))


def record(num, name, test_input, expected, actual, passed, evidence, limitation=""):
    results.append({"num": num, "name": name, "input": test_input, "expected": expected, "actual": actual,
                    "result": "PASS" if passed else "FAIL", "evidence": evidence, "limitation": limitation})
    print(f"[{'PASS' if passed else 'FAIL'}] {num}. {name} — {actual}")


def payload(label, *, customer=None, policy=None, claim=None, **extra):
    n = len(created_claims) + len(results) + 1
    body = {
        "submission_key": f"test-{RUN}-{uuid.uuid4().hex[:10]}",
        "customer": customer if customer is not None else {
            "full_name": f"TEST {label}", "phone": f"70{abs(hash((RUN, label))) % 10**8:08d}",
            "address": f"{n} Test Street {RUN}"},
        "policy": policy if policy is not None else {
            "policy_number": f"TEST-{RUN}-{label}", "premium": 6000, "start_date": "2025-01-01"},
        "claim": claim if claim is not None else {
            "amount": 6000, "category": "collision", "incident_date": "2026-03-10"},
    }
    body["claim"].setdefault("description", TAG + " " + label)
    if "description" in body["claim"] and TAG not in body["claim"]["description"]:
        body["claim"]["description"] = TAG + " " + body["claim"]["description"]
    body.update(extra)
    return body


def submit(body):
    r = client.post("/api/claims/submit", json=body)
    data = r.get_json()
    if data and data.get("saved") and data["claim_id"] not in created_claims:
        created_claims.append(data["claim_id"])
    return r.status_code, data


def report(cid):
    return client.get(f"/api/claims/{cid}").get_json()


def ml_line(d):
    r = d["report"]
    return (f"claim #{d['claim_id']}: risk {r['claim']['risk_score']}, ML anomaly={r['ml']['anomaly']} "
            f"(score {r['ml']['score']}, threshold {r['ml']['threshold']}), network size {r['network']['size']}, "
            f"recommendation '{r['recommendation']}'")


def independent_components():
    """Connected components from a plain Python BFS over every claim_edges row —
    a second implementation to check the recursive CTE against."""
    adj = {}
    for e in db_all("SELECT claim_a, claim_b FROM claim_edges"):
        adj.setdefault(e["claim_a"], set()).add(e["claim_b"])
        adj.setdefault(e["claim_b"], set()).add(e["claim_a"])
    seen, comps = set(), []
    for start in sorted(adj):
        if start in seen:
            continue
        comp, q = set(), deque([start])
        while q:
            x = q.popleft()
            if x in comp:
                continue
            comp.add(x)
            q.extend(adj[x] - comp)
        seen |= comp
        comps.append(sorted(comp))
    return sorted(comps)


# ============================================================================
def run_tests():
    appmod.startup()
    ml_outputs = []

    # ---- 1 / 19: normal isolated claim, appears in All Claims ----
    b = payload("NORMAL", claim={"amount": 5500, "category": "collision", "incident_date": "2026-03-10"})
    code, d = submit(b)
    r = d["report"]
    ok = code == 201 and d["analysis_ok"] and r["network"]["size"] == 1 and not r["network"]["shared_entities"]
    record(1, "Normal isolated claim", "new customer, unique phone/address, no garage/doctor/vehicle/witness, ₹5,500 on ₹6,000 premium",
           "201, saved, network size 1, no shared entities, recommendation follows the documented rule",
           f"HTTP {code}; {ml_line(d)}", ok, {"claim_id": d["claim_id"], "breakdown": [f["points"] for f in r["risk"]["breakdown"]]})
    normal_id = d["claim_id"]
    ml_outputs.append(("Normal isolated claim", ml_line(d)))
    all_ids = [c["claim_id"] for c in client.get("/api/claims").get_json()]
    susp = [c["claim_id"] for c in client.get("/api/claims").get_json() if c["recommendation"] in (rf.STATUS_REVIEW, rf.STATUS_INVESTIGATE)]
    record(19, "New normal claim appears in All Claims", f"claim #{normal_id}",
           "present in /api/claims; present in Suspicious only if its recommendation is Under Review/Requires Investigation",
           f"in All Claims: {normal_id in all_ids}; in Suspicious: {normal_id in susp}; recommendation '{r['recommendation']}'",
           normal_id in all_ids and ((normal_id in susp) == (r["recommendation"] != rf.STATUS_NONE)), {})

    # ---- 2: one shared entity ----
    b = payload("ONE-ENTITY", garage={"garage_id": 99})
    code, d = submit(b)
    n = d["report"]["network"]
    types = sorted({e["type"] for e in n["direct_shared_entities"]})
    ok = code == 201 and types == ["garage"] and set(range(1, 7)) <= set(n["direct_claim_ids"]) and n["size"] == 7
    record(2, "Claim connecting to an existing network through one entity", "garage_id 99 (QuickFix Auto Care) only",
           "joins network N-1; only a garage edge; direct links to claims #1-#6; size 7",
           f"HTTP {code}; network {n['network_id']} size {n['size']}; direct {n['direct_claim_ids']}; entity types {types}", ok,
           {"claim_id": d["claim_id"], "entities": [net.readable(e) for e in n["direct_shared_entities"]]})
    one_entity_id = d["claim_id"]
    ml_outputs.append(("Claim sharing only one entity", ml_line(d)))

    # ---- 3: several entity types ----
    b = payload("MULTI-ENTITY", claim={"amount": 48000, "category": "collision", "incident_date": "2026-02-02"},
                policy={"policy_number": f"TEST-{RUN}-MULTI", "premium": 5200, "start_date": "2025-03-01"},
                vehicle={"vehicle_id": 902}, garage={"garage_id": 99}, doctor={"doctor_id": 99},
                witnesses=[{"witness_id": 902}])
    code, d = submit(b)
    n = d["report"]["network"]
    types = sorted({e["type"] for e in n["direct_shared_entities"]})
    ok = code == 201 and types == ["doctor", "garage", "vehicle", "witness"] and n["size"] == 8
    record(3, "Claim connecting through multiple entity types", "vehicle 902, garage 99, doctor 99, witness 902; ₹48,000 on ₹5,200",
           "four entity types listed with exact claim IDs; network size 8 (6 ring + test 2 + this)",
           f"HTTP {code}; types {types}; size {n['size']}; recommendation '{d['report']['recommendation']}'", ok,
           {"claim_id": d["claim_id"], "entities": [net.readable(e) for e in n["direct_shared_entities"]]})
    ml_outputs.append(("Claim sharing several entities / connected to an existing network", ml_line(d)))

    # ---- 4: multi-hop traversal ----
    before = net.network_index(_networks())
    base_net = before[54][0]
    base_members = [m for m, (nid, _) in before.items() if nid == base_net]
    g31 = [r["claim_id"] for r in db_all("SELECT claim_id FROM claims WHERE garage_id = 31 ORDER BY 1")]
    code, d = submit(payload("MULTI-HOP", garage={"garage_id": 31}))
    n = d["report"]["network"]
    hops = {c["claim_id"]: c["hops"] for c in n["connected_claims"]}
    ok = (code == 201 and set(n["claim_ids"]) == set(base_members) | {d["claim_id"]}
          and set(n["direct_claim_ids"]) == set(g31) and max(hops.values()) >= 3)
    record(4, "Multi-hop network traversal", "garage_id 31 (used by claims %s)" % g31,
           f"joins the {len(base_members)}-claim component {base_net}; direct = garage-31 claims; others reached in 2+ hops",
           f"size {n['size']}; direct {n['direct_claim_ids']}; max hops {max(hops.values())}; "
           f"indirect count {len(n['indirect_claim_ids'])}", ok, {"claim_id": d["claim_id"], "hops": hops})

    # ---- 5: large component + CTE vs independent BFS, cycles ----
    cte = sorted(_networks())
    bfs = independent_components()
    ring = _members(1)
    ring_edges = db_one("SELECT COUNT(*) AS n FROM claim_edges WHERE claim_a = ANY(%s) AND claim_b = ANY(%s)",
                        (ring, ring))["n"]
    largest = max(cte, key=len)
    ok = cte == bfs and len(set(sum(cte, []))) == sum(len(c) for c in cte) and len(ring) == len(set(ring))
    record(5, "Large existing connected component, cycles and disjointness",
           "recursive CTE for every claim vs. an independent Python BFS over claim_edges",
           "identical components; no claim in two networks; cyclic ring (complete graph) returns each claim once",
           f"{len(cte)} components; largest {len(largest)} claims; CTE == BFS: {cte == bfs}; ring N-1 has {len(ring)} "
           f"distinct claims over {ring_edges} edges (cycles)", ok, {"largest": largest})

    # ---- 6: same phone, different address ----
    phone = f"98{abs(hash(RUN)) % 10**8:08d}"
    _, a = submit(payload("PHONE-A", customer={"full_name": "TEST Phone A", "phone": phone, "address": f"11 Alpha Road {RUN}"}))
    _, bb = submit(payload("PHONE-B", customer={"full_name": "TEST Phone B", "phone": f"+91 {phone[:5]} {phone[5:]}",
                                                "address": f"22 Beta Road {RUN}"}))
    ents = bb["report"]["network"]["direct_shared_entities"]
    types = [e["type"] for e in ents]
    ok = types == ["phone"] and ents[0]["claim_ids"] == sorted([a["claim_id"], bb["claim_id"]])
    record(6, "Same phone, different address", f"customer A phone {phone}; customer B phone '+91 {phone[:5]} {phone[5:]}'",
           "one phone edge after normalization; no address edge; both raw values shown",
           f"edges {types}; label '{ents[0]['label'] if ents else ''}'; claims {ents[0]['claim_ids'] if ents else []}", ok, {})

    # ---- 7: same address, different phone + vague address ----
    _, a = submit(payload("ADDR-A", customer={"full_name": "TEST Addr A", "phone": "7012300001", "address": f"77 Test Lane, Block-B {RUN}"}))
    _, bb = submit(payload("ADDR-B", customer={"full_name": "TEST Addr B", "phone": "7012300002", "address": f"77  test lane block b {RUN.lower()}"}))
    ents = bb["report"]["network"]["direct_shared_entities"]
    _, v1 = submit(payload("VAGUE-A", customer={"full_name": "TEST Vague A", "phone": "7012300003", "address": "Bengaluru"}))
    _, v2 = submit(payload("VAGUE-B", customer={"full_name": "TEST Vague B", "phone": "7012300004", "address": "bengaluru"}))
    ok = [e["type"] for e in ents] == ["address"] and v2["report"]["network"]["size"] == 1
    record(7, "Same address, different phone (and vague placeholder address)",
           "'77 Test Lane, Block-B' vs '77  test lane block b'; then two customers with address 'Bengaluru'",
           "one address edge after normalization; city-only 'Bengaluru' (no house number) creates no edge",
           f"edges {[e['type'] for e in ents]}; 'Bengaluru' pair network size {v2['report']['network']['size']}", ok, {})

    # ---- 8: existing customer (Claim #62 reproduction) ----
    c0 = counts()
    code_conf, conf = submit(payload("EXISTING-CONFLICT", customer={"claimant_id": 14, "full_name": "Liam Kapoor",
                                                                     "phone": "09236409075", "address": "Bengaluru"}))
    c1 = counts()
    code, d = submit(payload("EXISTING-14", customer={"claimant_id": 14}))
    n = d["report"]["network"]
    ents = {e["type"]: e for e in n["shared_entities"]}
    ok = (code_conf == 409 and c0 == c1 and code == 201 and d["claimant_id"] == 14
          and d["report"]["claim"]["phone"] == "9965960427" and set(n["claim_ids"]) == {14, 28, 43, d["claim_id"]}
          and n["direct_claim_ids"] == [14] and "customer" in ents)
    record(8, "Existing customer submission (screenshot Claim #62)",
           "A) claimant_id 14 + typed phone 09236409075 / 'Bengaluru'; B) claimant_id 14 with no typed details",
           "A) 409 conflict, nothing written; B) 201 using stored phone 9965960427 / 152 Station Road; network {14,28,43,new} "
           "with a 'same customer' edge to #14 and indirect links to #28/#43",
           f"A) HTTP {code_conf}, row counts unchanged: {c0 == c1} — '{conf['errors'][0]['message'][:90]}…'; "
           f"B) HTTP {code}, customer #{d['claimant_id']}, phone {d['report']['claim']['phone']}, network {n['claim_ids']}, "
           f"direct {n['direct_claim_ids']}, indirect {n['indirect_claim_ids']}", ok,
           {"edges": [net.readable(e) for e in n["shared_entities"]]})
    existing_claim = d["claim_id"]

    # ---- 9: new customer + same-name different person (screenshot #58/#62) ----
    code, d = submit(payload("NEW-CUST", customer={"full_name": "Liam Kapoor", "phone": "7099911122",
                                                    "address": f"5 Fresh Street {RUN}", "date_of_birth": "1990-05-04"}))
    row = db_one("SELECT * FROM claimants WHERE claimant_id = %s", (d["claimant_id"],))
    ok = (code == 201 and d["customer_new"] and d["claimant_id"] != 14 and row["phone"] == "7099911122"
          and str(row["date_of_birth"]) == "1990-05-04" and d["report"]["network"]["size"] == 1)
    record(9, "New customer submission (same name as customer #14, different person)",
           "full_name 'Liam Kapoor', new phone/address, DOB 1990-05-04",
           "new claimant row with exactly the submitted values; different customer ID from #14; NOT linked to #14 by name",
           f"HTTP {code}; new customer #{d['claimant_id']}; stored phone {row['phone']}, DOB {row['date_of_birth']}; "
           f"network size {d['report']['network']['size']}", ok, {})
    # rename so cleanup() recognises this test customer
    _exec("UPDATE claimants SET full_name = 'TEST Liam Kapoor' WHERE claimant_id = %s", (d["claimant_id"],))

    # ---- 10: missing required customer fields ----
    c0 = counts()
    code, d = submit(payload("MISSING", customer={"full_name": "TEST Missing"}))
    fields = sorted(e["field"] for e in d["errors"])
    ok = code == 400 and fields == ["customer.address", "customer.phone"] and counts() == c0
    record(10, "Missing required customer fields", "new customer with only full_name",
           "400 listing customer.phone and customer.address; nothing written",
           f"HTTP {code}; fields {fields}; counts unchanged {counts() == c0}", ok, {})

    # ---- 11: invalid entity IDs ----
    c0 = counts()
    code1, d1 = submit(payload("BAD-GARAGE", garage={"garage_id": 999999}))
    code2, d2 = submit(payload("BAD-CUST", customer={"claimant_id": 999999}))
    code3, d3 = submit(payload("BAD-VEH", vehicle={"vehicle_id": "abc"}))
    ok = code1 == 422 and code2 == 422 and code3 == 400 and counts() == c0
    record(11, "Invalid entity ID", "garage_id 999999; claimant_id 999999; vehicle_id 'abc'",
           "422 for unknown IDs, 400 for a malformed ID; never silently dropped; nothing written",
           f"HTTP {code1} '{d1['error']}'; HTTP {code2} '{d2['errors'][0]['message']}'; HTTP {code3}; counts unchanged {counts() == c0}",
           ok, {})

    # ---- 12: duplicate policy number ----
    c0 = counts()
    code1, d1 = submit(payload("DUP-POL", policy={"policy_number": "POL-2024001", "premium": 5000, "start_date": "2024-01-01"}))
    code2, _ = submit(payload("DUP-POL-CI", policy={"policy_number": "pol-2024001", "premium": 5000, "start_date": "2024-01-01"}))
    unchanged = counts() == c0
    code3, d3 = submit(payload("REUSE-POL", customer={"claimant_id": 1},
                               policy={"policy_number": "POL-2024001"},
                               claim={"amount": 3000, "category": "collision", "incident_date": "2024-06-01"}))
    ok = code1 == 409 and code2 == 409 and unchanged and code3 == 201 and d3["policy_reused"] and d3["policy_id"] == 1
    record(12, "Duplicate policy number", "new customer with POL-2024001 (owned by customer #1); lower-case 'pol-2024001'; "
           "then customer #1 filing on POL-2024001",
           "409 for another customer (case-insensitive), nothing written; the owner reuses the existing policy (no new policy row)",
           f"HTTP {code1}/{code2}, counts unchanged {unchanged}; owner: HTTP {code3}, policy_reused={d3.get('policy_reused')}, "
           f"policy_id {d3.get('policy_id')}", ok, {"message": d1["error"]})

    # ---- 13: invalid dates ----
    cases = {
        "incident before policy start": {"policy": {"policy_number": f"TEST-{RUN}-D1", "premium": 5000, "start_date": "2026-01-01"},
                                         "claim": {"amount": 1000, "category": "fire", "incident_date": "2025-12-01"}},
        "incident after policy end": {"policy": {"policy_number": f"TEST-{RUN}-D2", "premium": 5000, "start_date": "2025-01-01", "end_date": "2025-06-30"},
                                      "claim": {"amount": 1000, "category": "fire", "incident_date": "2025-08-01"}},
        "end before start": {"policy": {"policy_number": f"TEST-{RUN}-D3", "premium": 5000, "start_date": "2025-06-01", "end_date": "2025-01-01"}},
        "future incident": {"claim": {"amount": 1000, "category": "fire", "incident_date": str(date.today() + timedelta(days=5))}},
        "malformed date": {"claim": {"amount": 1000, "category": "fire", "incident_date": "31/02/2026"}},
        "DOB after incident": {"customer": {"full_name": "TEST Dob", "phone": "7012309999", "address": f"9 Dob Road {RUN}",
                                            "date_of_birth": "2026-04-01"}},
    }
    c0 = counts()
    outcomes = {}
    for label, over in cases.items():
        code, d = submit(payload("DATE", **over))
        outcomes[label] = (code, d["error"][:80])
    ok = all(c == 400 for c, _ in outcomes.values()) and counts() == c0
    record(13, "Invalid date combinations", ", ".join(cases), "400 with a specific explanation for each; nothing written",
           "; ".join(f"{k}: HTTP {c}" for k, (c, _) in outcomes.items()), ok, {k: m for k, (_, m) in outcomes.items()})

    # ---- 14: database failure during submission ----
    original = appmod.insert_claim_row

    def failing_insert(cur, values):
        raise psycopg2.OperationalError("simulated connection loss while inserting the claim row")
    appmod.insert_claim_row = failing_insert
    try:
        pnum = f"TEST-{RUN}-DBFAIL"
        code, d = submit(payload("DBFAIL", policy={"policy_number": pnum, "premium": 5000, "start_date": "2025-01-01"},
                                 garage={"name": f"TEST Garage DBFAIL {RUN}"}))
    finally:
        appmod.insert_claim_row = original
    leftovers = db_one("""SELECT (SELECT COUNT(*) FROM policies WHERE policy_number = %s) AS p,
                                 (SELECT COUNT(*) FROM claimants WHERE full_name = 'TEST DBFAIL') AS c,
                                 (SELECT COUNT(*) FROM garages WHERE name = %s) AS g""", (pnum, f"TEST Garage DBFAIL {RUN}"))
    ok = code == 500 and not d.get("saved") and leftovers["p"] == 0 and leftovers["c"] == 0 and leftovers["g"] == 0
    record(14, "Database failure during submission", "claim insert raises psycopg2.OperationalError after customer, policy and garage rows were inserted",
           "500 'NOT saved'; whole transaction rolled back — no orphan customer/policy/garage",
           f"HTTP {code}; saved={d.get('saved')}; leftover policy/customer/garage rows: {leftovers['p']}/{leftovers['c']}/{leftovers['g']}",
           ok, {"message": d["error"][:160]})

    # ---- 15: analysis failure after saving, then retry ----
    original_an = rf.analyse_portfolio

    def failing_analysis(cur, trigger):
        raise RuntimeError("simulated Isolation Forest failure")
    rf.analyse_portfolio = failing_analysis
    try:
        code, d = submit(payload("ANFAIL"))
    finally:
        rf.analyse_portfolio = original_an
    row = next(c for c in client.get("/api/claims").get_json() if c["claim_id"] == d["claim_id"])
    n_before = counts()["claims"]
    retry = client.post(f"/api/claims/{d['claim_id']}/analyze")
    rd = retry.get_json()
    ok = (code == 201 and d["saved"] and not d["analysis_ok"] and "simulated" in (d["analysis_error"] or "")
          and row["recommendation"] == rf.STATUS_PENDING and row["risk_score"] is None and row["ml_anomaly"] is None
          and retry.status_code == 200 and rd["analysis_ok"] and counts()["claims"] == n_before)
    record(15, "Model/analysis failure after saving", "analysis raises RuntimeError after the claim committed; then POST /analyze",
           "201 saved with analysis_ok=false and the error; claim listed as 'Analysis pending' with no fabricated score/ML; "
           "retry analyses it without creating another claim",
           f"HTTP {code}, analysis_ok={d['analysis_ok']}, error '{d['analysis_error']}'; listed as '{row['recommendation']}', "
           f"risk {row['risk_score']}, ML {row['ml_anomaly']}; retry HTTP {retry.status_code}, analysis_ok={rd['analysis_ok']}, "
           f"risk {rd['report']['claim']['risk_score']}; claim count unchanged {counts()['claims'] == n_before}", ok, {})

    # ---- 16: duplicate submission / retry ----
    body = payload("RETRY")
    c0 = counts()["claims"]
    code1, d1 = submit(body)
    code2, d2 = submit(body)
    code3, d3 = submit({**body, "claim": {**body["claim"], "amount": body["claim"]["amount"] + 1000}})
    stored = client.get(f"/api/claims/{d1['claim_id']}").get_json()["claim"]["amount"]
    ok = (code1 == 201 and code2 == 200 and d2["duplicate"] and d1["claim_id"] == d2["claim_id"]
          and code3 == 409 and stored == body["claim"]["amount"] and counts()["claims"] == c0 + 1)
    record(16, "Duplicate submission / retry", "same submission_key posted twice, then reused with a changed amount",
           "first 201; second 200 duplicate=true with the same claim ID; changed body 409 (not a silent duplicate); "
           "stored amount unchanged; exactly one new row",
           f"HTTP {code1} → #{d1['claim_id']}; HTTP {code2} duplicate={d2['duplicate']} → #{d2['claim_id']}; "
           f"changed body HTTP {code3}; stored amount {stored}; claims +{counts()['claims'] - c0}", ok,
           {"changed_body_error": (d3.get("error") or "")[:120]})

    # ---- 17 / 18: search ----
    pnum = f"TEST-{RUN}-SEARCH"
    code, d = submit(payload("SEARCH", policy={"policy_number": pnum, "premium": 7000, "start_date": "2025-01-01"}))
    hits = client.get("/api/claims", query_string={"q": pnum}).get_json()
    hits_lc = client.get("/api/claims", query_string={"q": pnum.lower()}).get_json()
    ok = [h["claim_id"] for h in hits] == [d["claim_id"]] and [h["claim_id"] for h in hits_lc] == [d["claim_id"]]
    record(17, "Search by newly created policy number", f"q='{pnum}' and lower-case",
           "exactly the new claim", f"{[h['claim_id'] for h in hits]} / {[h['claim_id'] for h in hits_lc]}", ok, {})
    by_id = [h["claim_id"] for h in client.get("/api/claims", query_string={"q": f"#{d['claim_id']}"}).get_json()]
    by_name = [h["claim_id"] for h in client.get("/api/claims", query_string={"q": "test search"}).get_json()]
    by_pol = [h["claim_id"] for h in client.get("/api/claims", query_string={"q": "POL-2024012"}).get_json()]
    ok = d["claim_id"] in by_id and d["claim_id"] in by_name and by_pol == [12]
    record(18, "Search by claim ID, customer name and existing policy number",
           f"q='#{d['claim_id']}', q='test search', q='POL-2024012'",
           "claim found by ID and by case-insensitive partial name; seeded policy POL-2024012 returns claim #12",
           f"by id {by_id}; by name {by_name}; POL-2024012 → {by_pol}", ok, {})

    # ---- Phase 6 ML cases (outputs reported, not asserted) ----
    _, d = submit(payload("HIGH-VALUE", claim={"amount": 400000, "category": "theft", "incident_date": "2026-01-15"},
                          policy={"policy_number": f"TEST-{RUN}-HV", "premium": 5000, "start_date": "2025-01-01"}))
    ml_outputs.append(("High-value claim with no shared entities (₹4,00,000 on ₹5,000)", ml_line(d)))
    _, d = submit(payload("UNUSUAL", claim={"amount": 2900, "category": "water_damage", "incident_date": "2026-01-20"},
                          policy={"policy_number": f"TEST-{RUN}-UN", "premium": 450, "start_date": "2025-01-01"}))
    ml_outputs.append(("Legitimate claim with an unusual value (₹450 premium, ₹2,900 claim)", ml_line(d)))

    # ---- 20: cross-page consistency for every test claim ----
    problems = []
    summ = client.get("/api/summary").get_json()
    allc = client.get("/api/claims").get_json()
    nets = client.get("/api/networks").get_json()
    by_id = {c["claim_id"]: c for c in allc}
    net_of = {cid: n["network_id"] for n in nets for cid in n["claim_ids"]}
    if summ["total_claims"] != len(allc) or summ["total_claims"] != counts()["claims"]:
        problems.append("total count mismatch")
    if summ["suspicious_claims"] != sum(1 for c in allc if c["recommendation"] in (rf.STATUS_REVIEW, rf.STATUS_INVESTIGATE)):
        problems.append("suspicious count mismatch")
    if summ["ml_anomalies"] != db_one("SELECT COUNT(*) AS n FROM claims WHERE ml_anomaly AND analyzed_at IS NOT NULL")["n"]:
        problems.append("ML count mismatch")
    if summ["total_networks"] != len(nets) or summ["claims_in_networks"] != sum(n["size"] for n in nets) \
            or summ["claims_in_networks"] != len(net_of):
        problems.append("network counts mismatch / overlap")
    for n in nets:
        ent_pairs = {(e["claim_ids"][i], f"{e['type']}-{e['key']}") for e in n["shared_entities"] for i in range(len(e["claim_ids"]))}
        edge_pairs = {(int(x["source"].split("-")[1]), x["target"]) for x in n["graph"]["edges"]}
        if ent_pairs != edge_pairs:
            problems.append(f"graph/entity mismatch in {n['network_id']}")
    for cid in created_claims:
        if cid not in by_id:
            continue
        row = db_one("""SELECT c.*, p.policy_number, p.claimant_id FROM claims c JOIN policies p USING (policy_id)
                        WHERE claim_id = %s""", (cid,))
        rep = report(cid)
        lst = by_id[cid]
        if not (row["claimant_id"] == lst["claimant_id"] == rep["claim"]["claimant_id"]):
            problems.append(f"#{cid} customer id")
        if row["policy_number"] != lst["policy_number"]:
            problems.append(f"#{cid} policy")
        if float(row["risk_score"]) != lst["risk_score"] or lst["risk_score"] != rep["risk"]["breakdown_total"] \
                or abs(sum(f["points"] for f in rep["risk"]["breakdown"]) - lst["risk_score"]) > 0.05:
            problems.append(f"#{cid} risk score/breakdown")
        if row["ml_anomaly"] != lst["ml_anomaly"] or rep["ml"]["anomaly"] != lst["ml_anomaly"]:
            problems.append(f"#{cid} ML")
        if lst["ml_anomaly_score"] is not None and (lst["ml_anomaly_score"] > rep["ml"]["threshold"]) != lst["ml_anomaly"]:
            problems.append(f"#{cid} ML score/threshold disagree")
        if lst["network_id"] != net_of.get(cid) or lst["network_size"] != rep["network"]["size"]:
            problems.append(f"#{cid} network")
    record(20, "Cross-page consistency", f"{len(created_claims)} test claims + all summary counters",
           "DB row = All Claims row = claim report = network list; counters equal their page data; graph edges = shared entities",
           "no discrepancies" if not problems else "; ".join(problems), not problems,
           {"summary": {k: summ[k] for k in ("total_claims", "suspicious_claims", "requires_investigation", "under_review",
                                            "ml_evaluated", "ml_anomalies", "ml_unavailable", "total_networks", "claims_in_networks")}})
    return ml_outputs


def _networks():
    with appmod.db() as conn, conn.cursor() as cur:
        return net.compute_networks(cur)


def _members(cid):
    with appmod.db() as conn, conn.cursor() as cur:
        return net.network_members(cur, cid)


def _exec(sql, params):
    with appmod.db() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        conn.commit()


def cleanup():
    with appmod.db() as conn, conn.cursor() as cur:
        cur.execute("SELECT claim_id FROM claims WHERE description LIKE %s", (TAG + "%",))
        ids = [r["claim_id"] for r in cur.fetchall()]
        cur.execute("""SELECT DISTINCT p.policy_id, p.claimant_id FROM policies p JOIN claimants cl USING (claimant_id)
                       WHERE p.policy_number LIKE %s OR cl.full_name LIKE 'TEST %%'""", (f"TEST-{RUN}-%",))
        pols = cur.fetchall()
        cur.execute("DELETE FROM claim_witnesses WHERE claim_id = ANY(%s)", (ids,))
        cur.execute("DELETE FROM claims WHERE claim_id = ANY(%s)", (ids,))
        cur.execute("""DELETE FROM policies WHERE policy_id = ANY(%s)
                       AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.policy_id = policies.policy_id)""",
                    ([p["policy_id"] for p in pols],))
        cur.execute("""DELETE FROM claimants WHERE full_name LIKE 'TEST %%'
                       AND NOT EXISTS (SELECT 1 FROM policies p WHERE p.claimant_id = claimants.claimant_id)""")
        cur.execute("DELETE FROM garages WHERE name LIKE 'TEST %' AND garage_id NOT IN "
                    "(SELECT garage_id FROM claims WHERE garage_id IS NOT NULL)")
        cur.execute("DELETE FROM doctors WHERE full_name LIKE 'TEST %' AND doctor_id NOT IN "
                    "(SELECT doctor_id FROM claims WHERE doctor_id IS NOT NULL)")
        cur.execute("DELETE FROM witnesses WHERE full_name LIKE 'TEST %' AND witness_id NOT IN "
                    "(SELECT witness_id FROM claim_witnesses)")
        rf.analyse_portfolio(cur, f"system test {RUN} cleanup")
        conn.commit()
    return len(ids)


def write_report(ml_outputs, cleaned):
    lines = [f"# System test results — run {RUN}", "",
             f"Run on {date.today()} against the live PostgreSQL database (`DATABASE_URL`) through the Flask app.",
             f"Test records were tagged `{TAG}` / `TEST-{RUN}-*` and "
             + (f"{cleaned} test claims were deleted afterwards, then the portfolio was re-analysed." if cleaned is not None
                else "were KEPT (--keep)."), "",
             f"**{sum(r['result'] == 'PASS' for r in results)} / {len(results)} passed.**", "",
             "| # | Test | Input | Expected | Actual | Result |", "|---|---|---|---|---|---|"]
    for r in sorted(results, key=lambda r: r["num"]):
        cell = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {r['num']} | {cell(r['name'])} | {cell(r['input'])} | {cell(r['expected'])} | {cell(r['actual'])} | {r['result']} |")
    lines += ["", "## Isolation Forest outputs (reported as observed, not asserted)", ""]
    lines += [f"- **{k}** — {v}" for k, v in ml_outputs]
    lines += ["", "## Evidence", "", "```json", json.dumps({r["num"]: r["evidence"] for r in results}, indent=2, default=str), "```"]
    with open(os.path.join(ROOT, "tests", "TEST_RESULTS.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()
    if not appmod.DATABASE_URL:
        sys.exit("Set DATABASE_URL first.")
    ml = []
    try:
        ml = run_tests()
    finally:
        cleaned = None if args.keep else cleanup()
        write_report(ml, cleaned)
    failed = [r for r in results if r["result"] != "PASS"]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed. Report: tests/TEST_RESULTS.md")
    sys.exit(1 if failed else 0)
