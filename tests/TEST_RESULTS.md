# System test results — run BB071E

Run on 2026-10-30 against the live PostgreSQL database (`DATABASE_URL`) through the Flask app.
Test records were tagged `[LEDGER-TEST BB071E]` / `TEST-BB071E-*` and 18 test claims were deleted afterwards, then the portfolio was re-analysed.

**20 / 20 passed.**

| # | Test | Input | Expected | Actual | Result |
|---|---|---|---|---|---|
| 1 | Normal isolated claim | new customer, unique phone/address, no garage/doctor/vehicle/witness, ₹5,500 on ₹6,000 premium | 201, saved, network size 1, no shared entities, recommendation follows the documented rule | HTTP 201; claim #150: risk 4.0, ML anomaly=False (score 0.435, threshold 0.5), network size 1, recommendation 'No immediate concern' | PASS |
| 2 | Claim connecting to an existing network through one entity | garage_id 99 (QuickFix Auto Care) only | joins network N-1; only a garage edge; direct links to claims #1-#6; size 7 | HTTP 201; network N-1 size 7; direct [1, 2, 3, 4, 5, 6]; entity types ['garage'] | PASS |
| 3 | Claim connecting through multiple entity types | vehicle 902, garage 99, doctor 99, witness 902; ₹48,000 on ₹5,200 | four entity types listed with exact claim IDs; network size 8 (6 ring + test 2 + this) | HTTP 201; types ['doctor', 'garage', 'vehicle', 'witness']; size 8; recommendation 'Requires Investigation' | PASS |
| 4 | Multi-hop network traversal | garage_id 31 (used by claims [51, 54]) | joins the 18-claim component N-7; direct = garage-31 claims; others reached in 2+ hops | size 19; direct [51, 54]; max hops 8; indirect count 16 | PASS |
| 5 | Large existing connected component, cycles and disjointness | recursive CTE for every claim vs. an independent Python BFS over claim_edges | identical components; no claim in two networks; cyclic ring (complete graph) returns each claim once | 10 components; largest 19 claims; CTE == BFS: True; ring N-1 has 8 distinct claims over 67 edges (cycles) | PASS |
| 6 | Same phone, different address | customer A phone 9801660901; customer B phone '+91 98016 60901' | one phone edge after normalization; no address edge; both raw values shown | edges ['phone']; label '+91 98016 60901 / 9801660901'; claims [154, 155] | PASS |
| 7 | Same address, different phone (and vague placeholder address) | '77 Test Lane, Block-B' vs '77  test lane block b'; then two customers with address 'Bengaluru' | one address edge after normalization; city-only 'Bengaluru' (no house number) creates no edge | edges ['address']; 'Bengaluru' pair network size 1 | PASS |
| 8 | Existing customer submission (screenshot Claim #62) | A) claimant_id 14 + typed phone 09236409075 / 'Bengaluru'; B) claimant_id 14 with no typed details | A) 409 conflict, nothing written; B) 201 using stored phone 9965960427 / 152 Station Road; network {14,28,43,new} with a 'same customer' edge to #14 and indirect links to #28/#43 | A) HTTP 409, row counts unchanged: True — 'Customer #14 (Liam Kapoor) is stored with phone '9965960427', but '09236409075' was entere…'; B) HTTP 201, customer #14, phone 9965960427, network [14, 28, 43, 160], direct [14], indirect [28, 43] | PASS |
| 9 | New customer submission (same name as customer #14, different person) | full_name 'Liam Kapoor', new phone/address, DOB 1990-05-04 | new claimant row with exactly the submitted values; different customer ID from #14; NOT linked to #14 by name | HTTP 201; new customer #154; stored phone 7099911122, DOB 1990-05-04; network size 1 | PASS |
| 10 | Missing required customer fields | new customer with only full_name | 400 listing customer.phone and customer.address; nothing written | HTTP 400; fields ['customer.address', 'customer.phone']; counts unchanged True | PASS |
| 11 | Invalid entity ID | garage_id 999999; claimant_id 999999; vehicle_id 'abc' | 422 for unknown IDs, 400 for a malformed ID; never silently dropped; nothing written | HTTP 422 'Garage #999999 does not exist.'; HTTP 422 'Customer #999999 does not exist.'; HTTP 400; counts unchanged True | PASS |
| 12 | Duplicate policy number | new customer with POL-2024001 (owned by customer #1); lower-case 'pol-2024001'; then customer #1 filing on POL-2024001 | 409 for another customer (case-insensitive), nothing written; the owner reuses the existing policy (no new policy row) | HTTP 409/409, counts unchanged True; owner: HTTP 201, policy_reused=True, policy_id 1 | PASS |
| 13 | Invalid date combinations | incident before policy start, incident after policy end, end before start, future incident, malformed date, DOB after incident | 400 with a specific explanation for each; nothing written | incident before policy start: HTTP 400; incident after policy end: HTTP 400; end before start: HTTP 400; future incident: HTTP 400; malformed date: HTTP 400; DOB after incident: HTTP 400 | PASS |
| 14 | Database failure during submission | claim insert raises psycopg2.OperationalError after customer, policy and garage rows were inserted | 500 'NOT saved'; whole transaction rolled back — no orphan customer/policy/garage | HTTP 500; saved=None; leftover policy/customer/garage rows: 0/0/0 | PASS |
| 15 | Model/analysis failure after saving | analysis raises RuntimeError after the claim committed; then POST /analyze | 201 saved with analysis_ok=false and the error; claim listed as 'Analysis pending' with no fabricated score/ML; retry analyses it without creating another claim | HTTP 201, analysis_ok=False, error 'RuntimeError: simulated Isolation Forest failure'; listed as 'Analysis pending', risk None, ML None; retry HTTP 200, analysis_ok=True, risk 4.4; claim count unchanged True | PASS |
| 16 | Duplicate submission / retry | same submission_key posted twice, then reused with a changed amount | first 201; second 200 duplicate=true with the same claim ID; changed body 409 (not a silent duplicate); stored amount unchanged; exactly one new row | HTTP 201 → #164; HTTP 200 duplicate=True → #164; changed body HTTP 409; stored amount 6000.0; claims +1 | PASS |
| 17 | Search by newly created policy number | q='TEST-BB071E-SEARCH' and lower-case | exactly the new claim | [165] / [165] | PASS |
| 18 | Search by claim ID, customer name and existing policy number | q='#165', q='test search', q='POL-2024012' | claim found by ID and by case-insensitive partial name; seeded policy POL-2024012 returns claim #12 | by id [165]; by name [165]; POL-2024012 → [12] | PASS |
| 19 | New normal claim appears in All Claims | claim #150 | present in /api/claims; present in Suspicious only if its recommendation is Under Review/Requires Investigation | in All Claims: True; in Suspicious: False; recommendation 'No immediate concern' | PASS |
| 20 | Cross-page consistency | 18 test claims + all summary counters | DB row = All Claims row = claim report = network list; counters equal their page data; graph edges = shared entities | no discrepancies | PASS |

## Isolation Forest outputs (reported as observed, not asserted)

- **Normal isolated claim** — claim #150: risk 4.0, ML anomaly=False (score 0.435, threshold 0.5), network size 1, recommendation 'No immediate concern'
- **Claim sharing only one entity** — claim #151: risk 49.4, ML anomaly=True (score 0.556, threshold 0.5), network size 7, recommendation 'Under Review'
- **Claim sharing several entities / connected to an existing network** — claim #152: risk 83.7, ML anomaly=True (score 0.578, threshold 0.5), network size 8, recommendation 'Requires Investigation'
- **High-value claim with no shared entities (₹4,00,000 on ₹5,000)** — claim #166: risk 55.0, ML anomaly=True (score 0.747, threshold 0.5), network size 1, recommendation 'Requires Investigation'
- **Legitimate claim with an unusual value (₹450 premium, ₹2,900 claim)** — claim #167: risk 28.2, ML anomaly=False (score 0.455, threshold 0.5), network size 1, recommendation 'No immediate concern'

## Evidence

```json
{
  "1": {
    "claim_id": 150,
    "breakdown": [
      4.0,
      0.0,
      0.0,
      0.0
    ]
  },
  "19": {},
  "2": {
    "claim_id": 151,
    "entities": [
      "Garage \u2014 QuickFix Auto Care (garage #99, 12 Industrial Yard) \u2014 claims #1, #2, #3, #4, #5, #6, #151"
    ]
  },
  "3": {
    "claim_id": 152,
    "entities": [
      "Garage \u2014 QuickFix Auto Care (garage #99, 12 Industrial Yard) \u2014 claims #1, #2, #3, #4, #5, #6, #151, #152",
      "Doctor \u2014 Dr. R. Kulkarni (doctor #99) \u2014 claims #1, #2, #3, #4, #5, #6, #152",
      "Vehicle \u2014 VIN VIN902 (KA05RG902) \u2014 claims #2, #4, #6, #152",
      "Witness \u2014 Nikhil Menon (witness #902) \u2014 claims #2, #4, #6, #152"
    ]
  },
  "4": {
    "claim_id": 153,
    "hops": {
      "51": 1,
      "54": 1,
      "48": 2,
      "7": 3,
      "27": 3,
      "42": 3,
      "35": 4,
      "44": 4,
      "19": 5,
      "25": 5,
      "30": 5,
      "34": 5,
      "10": 6,
      "12": 6,
      "20": 6,
      "40": 6,
      "47": 7,
      "21": 8
    }
  },
  "5": {
    "largest": [
      7,
      10,
      12,
      19,
      20,
      21,
      25,
      27,
      30,
      34,
      35,
      40,
      42,
      44,
      47,
      48,
      51,
      54,
      153
    ]
  },
  "6": {},
  "7": {},
  "8": {
    "edges": [
      "Same customer \u2014 Liam Kapoor (customer #14) \u2014 claims #14, #160",
      "Garage \u2014 Bose Auto Works (garage #26, 206 Hill Road) \u2014 claims #14, #43",
      "Vehicle \u2014 VIN VIN100090 (KA72AB4595) \u2014 claims #28, #43"
    ]
  },
  "9": {},
  "10": {},
  "11": {},
  "12": {
    "message": "Policy number 'POL-2024001' already exists and belongs to customer #1. Policy numbers are unique."
  },
  "13": {
    "incident before policy start": "Incident date 2025-12-01 is before the policy start date 2026-01-01 \u2014 the policy",
    "incident after policy end": "Incident date 2025-08-01 is after the policy end date 2025-06-30 \u2014 the policy ha",
    "end before start": "policy.end_date (2025-01-01) is before policy.start_date (2025-06-01). Incident ",
    "future incident": "claim.incident_date (2026-11-04) is in the future.",
    "malformed date": "claim.incident_date must be a valid date in YYYY-MM-DD format.",
    "DOB after incident": "The customer's date of birth is after the incident date."
  },
  "14": {
    "message": "The claim was NOT saved \u2014 the database rejected the transaction and every step was rolled back (OperationalError: simulated connection loss while inserting the "
  },
  "15": {},
  "16": {
    "changed_body_error": "This submission key was already used to save Claim #164 with different values. Nothing new was saved and Claim #164 is u"
  },
  "17": {},
  "18": {},
  "20": {
    "summary": {
      "total_claims": 77,
      "suspicious_claims": 28,
      "requires_investigation": 14,
      "under_review": 14,
      "ml_evaluated": 77,
      "ml_anomalies": 17,
      "ml_unavailable": 0,
      "total_networks": 12,
      "claims_in_networks": 55
    }
  }
}
```
