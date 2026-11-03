# Ledger — Insurance Fraud Ring Detection

Ledger is an investigation dashboard for insurance claims. It stores every claim in PostgreSQL, finds claims that share a garage, doctor, vehicle, witness, customer, phone number or address, and traces those links into networks with a recursive SQL query. Each claim gets one risk score from 0 to 100 and an Isolation Forest unusualness score. The dashboard then recommends where a person should look first.

It does **not** decide that a claim is fraud. The strongest result it can give is **Requires Investigation**. A shared garage or a high score is a reason to review the file, not a verdict.

The demo ships with 57 synthetic claims, including one deliberate six-claim ring and several ordinary overlaps, so the pages have something real to show. Anything you file from **New Claim** is saved in the same database and scored with the same rules.

## What you see

| Page | What it answers |
|---|---|
| Overview | How many claims need investigation, how many are under review, and how much money is involved |
| All claims | Every saved claim, with search by claim ID, customer, policy number, phone, VIN, garage or doctor |
| Suspicious claims | Only claims recommended for a human look. Same rule as the sidebar count |
| Fraud networks | Each connected group, the shared entities, and a graph |
| Risk scoring | The four-factor formula, and a check that one claim's points add up to its stored score |
| ML anomalies | Which claims look unusual compared with the rest of this portfolio |
| Evidence log | A plain-language case file for one network or one claim |
| New claim | Files a real claim: validate, save in one transaction, then analyse |

## Run it on any computer

You need three things, on Windows, macOS or Linux:

- [Docker](https://docs.docker.com/get-docker/) (Docker Desktop on Windows and macOS, Docker Engine on Linux)
- Python 3.10 or newer (`python3 --version` or `python --version`)
- A web browser

The database password lives only in an environment variable. It is not stored in this repository.

### 1. Get the code

```bash
git clone https://github.com/Kasvi-1603/DBMS_el.git
cd DBMS_el
```

### 2. Start PostgreSQL

```bash
docker run -d --name fraud-postgres \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=fraud_db \
  -p 5432:5432 \
  postgres:16
```

Wait a few seconds, then load the schema. `schema.sql` creates the tables and applies the migrations. Run it only on a new database: the file drops existing tables.

```bash
docker cp database fraud-postgres:/tmp/ledger-schema
docker exec fraud-postgres psql -U postgres -d fraud_db -v ON_ERROR_STOP=1 -f /tmp/ledger-schema/schema.sql
```

### 3. Point the app at the database

Pick the block for your shell. This lasts only for that terminal, so set it again in every new terminal (and again after you open a second one for the dashboard).

**macOS / Linux (bash or zsh)**

```bash
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/fraud_db
```

**Windows PowerShell**

```powershell
$env:DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:5432/fraud_db"
```

**Windows Command Prompt**

```bat
set DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/fraud_db
```

### 4. Install the Python packages

From the project folder. On Windows, use `python` instead of `python3` if that is what `python --version` answers to.

```bash
python3 -m venv .venv
```

**macOS / Linux**

```bash
.venv/bin/python -m pip install -r backend/requirements.txt
```

**Windows**

```powershell
.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt
```

### 5. Load the demo claims

This **deletes every row** and inserts the 57-claim demo set. That is what you want on a fresh database. Do not run it later if you want to keep claims you filed yourself.

**macOS / Linux**

```bash
cd backend
../.venv/bin/python seed_postgres.py --reset
cd ..
```

**Windows PowerShell**

```powershell
cd backend
..\.venv\Scripts\python.exe seed_postgres.py --reset
cd ..
```

### 6. Start the API

Leave this terminal open. The API listens on http://127.0.0.1:5000, applies any new migration on startup, and scores claims that have not been analysed yet.

**macOS / Linux**

```bash
cd backend
../.venv/bin/python app.py
```

**Windows PowerShell**

```powershell
cd backend
..\.venv\Scripts\python.exe app.py
```

### 7. Open the dashboard

In a **second** terminal, set `DATABASE_URL` again only if you run tests from here. The page itself talks to the API, not to Postgres.

**macOS / Linux**

```bash
cd frontend
python3 -m http.server 8080 --bind 127.0.0.1
```

**Windows PowerShell**

```powershell
cd frontend
python -m http.server 8080 --bind 127.0.0.1
```

Then open http://127.0.0.1:8080/dashboard.html

If the API is not on this same machine, open `dashboard.html?api=http://HOST:5000`.

### Next time

The database container keeps its data. You only repeat the parts that are not already running:

```bash
docker start fraud-postgres
# set DATABASE_URL in the terminal, then:
cd backend && ../.venv/bin/python app.py          # Windows: ..\.venv\Scripts\python.exe app.py
# second terminal:
cd frontend && python3 -m http.server 8080 --bind 127.0.0.1
```

### If a port is already taken

- **5432.** Another Postgres is using it, or Windows has reserved it (Hyper-V sometimes reserves 5355–5454). Start the container with `-p 15432:5432` instead, and change `5432` to `15432` in `DATABASE_URL`.
- **5000 or 8080.** Start the API with `PORT=5001` (`$env:PORT = "5001"` in PowerShell) and the dashboard with `python -m http.server 8765 --bind 127.0.0.1`. Open `http://127.0.0.1:8765/dashboard.html?api=http://127.0.0.1:5001`.
- **Docker says the container name exists.** Use `docker start fraud-postgres` instead of `docker run`.

### File a claim that the model flags

On **New claim**, leave the customer ID empty and enter a new person (any name, a 10-digit phone, an address with a house number). Policy premium `5000`, start date `2026-01-01`, claim amount `400000`, category Collision, incident date inside the policy and not in the future. Submit. Expect **Anomalous** and **Requires Investigation**. Use a new policy number if you submit again.

## Layout

```
database/
  schema.sql                        Fresh install only (drops tables!) — includes the migration
  migrations/001_integrity_and_networks.sql
                                    Idempotent upgrade: constraints, analysis columns,
                                    normalisation functions, claim_edges view
backend/
  app.py                            Flask REST API + the New Claim workflow
  networks.py                       The ONE recursive CTE, connected components, shared
                                    entities, graph building
  risk_features.py                  The ONE feature definition, risk formula, Isolation
                                    Forest and recommendation rule
  seed_postgres.py                  DESTRUCTIVE demo seeder (requires --reset)
  generate_demo_data.py             Offline SQLite snapshot (same scoring code) → frontend/data.json
ml/anomaly_detection.py             Read-only standalone scorer (same scoring code)
frontend/dashboard.html             Single-page dashboard (reads the live API)
tests/system_test.py                20 end-to-end tests against the real database
tests/TEST_RESULTS.md               Output of the last test run
```

Every place that computes networks, risk or anomalies imports `networks.py` /
`risk_features.py`; there is no second implementation that could disagree.
(Running `generate_demo_data.py` on the seed data reproduces the live totals
exactly: 57 claims, 9 networks, 10 Requires Investigation, 25 suspicious,
18 ML anomalies.)

## Setup and launch (Windows / PowerShell)

Credentials are never stored in the repository. The API reads them from the
`DATABASE_URL` environment variable and refuses to start without it.

```powershell
# 1. Database (PostgreSQL 16 in Docker). On this machine port 5432 sits in a
#    Windows-reserved range, so the container publishes 127.0.0.1:15432.
docker start fraud-postgres-15432

# 2. Connection string, for this shell session only
$env:DATABASE_URL = "postgresql://postgres:<password>@127.0.0.1:15432/fraud_db"

# 3. Backend (applies pending migrations, analyses unanalysed claims, serves :5000)
cd backend
..\.venv\Scripts\python.exe -m pip install -r requirements.txt   # first time only
..\.venv\Scripts\python.exe app.py

# 4. Frontend, in a second terminal (8000/8080 are blocked on this machine)
cd frontend
..\.venv\Scripts\python.exe -m http.server 8765 --bind 127.0.0.1
# open http://127.0.0.1:8765/dashboard.html   (append ?api=http://host:port for another API)
# sections can be deep-linked: dashboard.html#networks, #ml, #analyze …
```

Use `.venv`. The `venv` folder that came with the project points at another
user's Python installation and does not run.

**Upgrading an existing database:** the API applies
`database/migrations/*.sql` on every startup. These files are idempotent and
never delete data. **Fresh install only:**
`psql -d fraud_db -f database/schema.sql` drops every table first.
**Demo data:** `python seed_postgres.py --reset` truncates *all* tables,
including claims filed through the dashboard. Back up first with
`pg_dump -Fc`.

## Database relationships

```
claimants 1─* policies 1─* claims *─1 vehicles
                             │  *─1 garages
                             │  *─1 doctors
                             └──*─* witnesses   (claim_witnesses junction)
analysis_runs                one row per analysis run (model config + counts)
```

Constraints added by the migration:
- `policies_dates_check`: the policy end date is on or after its start date.
- `claims_status_check`: claim status must be a known value.
- `claims_risk_score_range`: the risk score is between 0 and 100, or NULL.
- A unique index on policy numbers, ignoring case.
- A unique partial index on `submission_key`, which makes submissions idempotent.

Columns added to `claims`: `ml_anomaly`, `ml_anomaly_score`, `analyzed_at`
and `analysis_error`. NULL in these columns means "not analysed yet". It is
never treated as 0 or "normal".

## How networks are found

`claim_edges` is a view with one row per pair of claims that share something
concrete:

| link type | rule |
|---|---|
| garage / doctor / vehicle | same foreign key ID |
| witness | same `witness_id` in `claim_witnesses` |
| customer | same `claimant_id` (a repeat claimant, which is not a hidden link) |
| phone | same `norm_phone()` (digits only, last 10), **different** claimants |
| address | same `norm_address()` (lower-case, punctuation collapsed), **different** claimants |

The normalisation functions return NULL for placeholders:
- **Phone:** fewer than 7 digits, or one digit repeated (for example `0000000000`).
- **Address:** no house number, or shorter than 6 characters.

Because of this, "N/A", "-" or "0" never link two claims. Records are
**never** matched on name alone. Two garages both called "Verma Auto Works"
are different records unless the user supplies the same ID. For that reason
labels always show the ID, e.g. "Verma Auto Works (garage #4, 16 Park Street)".

The recursive CTE in `networks.py` expands claims over a symmetric adjacency
list:

```sql
WITH RECURSIVE adjacency(src, dst) AS (
    SELECT claim_a, claim_b FROM claim_edges
    UNION SELECT claim_b, claim_a FROM claim_edges
), network(claim_id) AS (
    SELECT %(start)s
    UNION
    SELECT a.dst FROM adjacency a JOIN network n ON a.src = n.claim_id
)
SELECT claim_id FROM network
```

`UNION` (rather than `UNION ALL`) over `claim_id` alone is what prevents
cycles. A claim already in the result is never added again, so recursion stops
when no new claim is reached. No depth cap is needed, and none can truncate a
long chain. Network size counts **distinct claims, including the claim
itself**.

Other definitions:
- **Direct connection:** the two claims share an entity.
- **Indirect connection:** the claims are reachable only through other claims.
  The hop count comes from a breadth-first search.
- **Network ID:** `N-<lowest claim id>`. It is stable as long as that claim stays in the component.

## Risk score (0–100)

The score is the sum of four factors. Each factor is rounded to 0.1 and the
total is the sum of the rounded factors, so the displayed factors always add
up to the displayed score.

| factor | points | formula |
|---|---|---|
| claim / premium ratio | 35 | `35 × min(ratio / 8, 1)`; missing premium gives 0 |
| network size | 25 | `25 × min((size − 1) / 5, 1)` |
| garage frequency | 20 | `20 × min((claims at this garage − 1) / 5, 1)`, so a garage used once scores 0 |
| category deviation | 20 | `20 × min(max((amount − category avg) / category avg, 0) / 2, 1)`; needs ≥ 5 claims in the category |

## Isolation Forest

The model is trained on six features:
- `claim_amount`
- `claim_premium_ratio`
- `network_size`
- `garage_frequency`
- `doctor_frequency`
- `category_deviation`

Preprocessing:
- A missing ratio is replaced with the portfolio median.
- A missing category deviation is replaced with 0.
- Each feature is then standardised (z-scored).

Settings are `n_estimators=300`, `contamination="auto"` and `random_state=42`.

- **Score:** `−score_samples(x)`, where higher means more unusual.
- **Flag:** a claim is flagged when its score is above the threshold (`−offset_` = 0.5).
- **Refitting:** the model is refit on the whole portfolio at every analysis run.
- **Training and inference:** both use the same matrix-building function.

This has some consequences:
- Adding a claim can change other claims' scores.
- With fewer than 20 claims, the result is explicitly **unavailable** rather than guessed.
- The share of flagged claims comes from the data, not from a quota. On the seed data it is 18 of 57 (32%).

An anomaly means "unusual for this synthetic portfolio", not fraud.

## Recommendation rule (one rule, used on every page)

| recommendation | rule |
|---|---|
| Requires Investigation | risk ≥ 50 |
| Under Review | 35 ≤ risk < 50, **or** the Isolation Forest flags the claim |
| No immediate concern | anything else |
| Analysis pending | the claim is saved but has not been analysed yet |

An ML flag on its own never raises a claim above Under Review. The reasons
for each recommendation are listed next to it. "Suspicious claims" means
Under Review plus Requires Investigation. The sidebar badge and the
Suspicious page use the same count.

## New Claim workflow (`POST /api/claims/submit`)

1. **Validate:**
   - The request must contain every required field.
   - Types must be correct.
   - Dates must be ISO format, and the incident date must fall within the policy period and not be in the future.
   - A date of birth must be before the incident date.
   - Every given ID must exist.

   Error codes are 400 for malformed or missing input, 422 for an unknown ID and 409 for a conflict. Every problem is listed by field name, and nothing is written.
2. **Save in one transaction** (customer → policy → entities → claim → witnesses). Any failure rolls back everything.
   - **Existing customer:** their stored details are used. If the request includes different details, the response is 409 and the customer is not silently overwritten.
   - **Policy:** an existing policy number is reused only if it belongs to the same customer and its terms match. A number owned by another customer is a 409.
3. **Analyse in a second transaction.** This step locks the portfolio with an advisory lock, then recomputes networks, risk and the Isolation Forest for every claim, then records an `analysis_runs` row. If it fails, the claim **stays saved**, `analysis_error` is stored, and the response says `analysis_ok:false` with a `retry_url` (`POST /api/claims/<id>/analyze`).
4. **Retry safely.** The dashboard sends a `submission_key`, and a SHA-256 fingerprint of the request body is stored with it (migration 002).
   - Re-sending the same key **with the same body** returns the original claim with `duplicate:true` instead of creating a second one.
   - Re-sending the same key **with a different body** gets a 409 instead of silently dropping the new values. The dashboard then issues a fresh key, so the next Submit files the edited form as a separate claim.

```bash
curl -X POST http://127.0.0.1:5000/api/claims/submit -H "Content-Type: application/json" -d '{
  "submission_key": "demo-0001",
  "customer": {"full_name": "Vikram Chopra", "phone": "9900033344", "address": "12 Industrial Yard"},
  "policy":   {"premium": 5300, "start_date": "2025-03-01"},
  "claim":    {"amount": 49500, "category": "collision", "incident_date": "2026-09-22"},
  "garage": {"garage_id": 99}, "doctor": {"doctor_id": 99},
  "vehicle": {"vehicle_id": 902}, "witnesses": [{"witness_id": 902}]
}'
```

## API

| endpoint | purpose |
|---|---|
| `GET /api/summary` | all counters (same rule as the pages), thresholds, model config, last run |
| `GET /api/claims?q=&category=&status=&recommendation=&min_risk=` | search by claim ID, customer ID/name, policy number (partial, case-insensitive), phone, VIN, plate, garage, doctor; `X-Total-Count` header |
| `GET /api/claims/<id>` | full report: claim, network, direct/indirect links, shared entities, graph, risk breakdown, ML, recommendation + reasons |
| `GET /api/claims/<id>/network` | the CTE result for one claim |
| `GET /api/networks` | every connected component (size > 1) |
| `GET /api/category-breakdown`, `GET /api/entities` | category totals; entity lists for the form |
| `GET /api/customers/<id>` | read-only customer details + policies (New Claim form) |
| `POST /api/claims/submit` | see above |
| `POST /api/claims/<id>/analyze` | retry analysis |
| `POST /api/claims/<id>/status` | change case status (validated; 404 for unknown claims) |

## Tests

From the project folder, with `DATABASE_URL` set as in the setup above:

```bash
# macOS / Linux
PYTHONIOENCODING=utf-8 .venv/bin/python tests/system_test.py

# Windows PowerShell
$env:PYTHONIOENCODING = "utf-8"; .\.venv\Scripts\python.exe tests\system_test.py
```

Add `--keep` to leave the test claims in the database.

The tests run against the **real** database. Every record they create is
tagged and deleted at the end, followed by a re-analysis. Database and model
failures are simulated by monkeypatching inside the test process, so the
application contains no fault-injection code.

The last run passed **20/20**. Inputs, expected results, actual results and
evidence are in `tests/TEST_RESULTS.md`.

## Limitations

- **Synthetic data.** The data is generated (57 claims), and surnames repeat
  across unrelated records. Averages, thresholds and anomaly rates describe
  this dataset only.
- **Transductive model.** The Isolation Forest is refit on the whole portfolio,
  so scores are relative to the claims on file and shift when claims are added.
- **Address matching is a heuristic.** "12 MG Road" and "12, M.G. Road"
  match, but "Twelve MG Rd" does not. There is no geocoding.
- **Test side effects.** Test runs add `analysis_runs` rows and advance ID
  sequences. They do not leave claims behind.
- **One extreme claim moves other claims' scores.** Category deviation is
  measured against the category *mean*, and the Isolation Forest is refit on
  every run. A single extreme claim therefore lowers other claims' scores in
  its category. Example: adding claim #110 (₹4,00,000 collision) dropped
  claim #54 from 64.8 to 43.7. This is the documented behaviour, not a bug,
  but it is a known robustness weakness (the "masking effect"). A median or
  MAD baseline would be more robust.
- **Browser coverage.** Every page, search, the drawer, the Evidence Log
  selectors, Risk Scoring and a New Claim submission were driven in headless
  Edge with Playwright. The analysis-failure retry button and the
  network-error retry path were tested through the API only, because they
  need a server fault the UI can't produce.
- **Claims #58–#62 are absent.** The claims referenced in the original
  screenshots don't exist in this database. Their root causes were reproduced
  with test claims instead (tests 8, 16 and 18–20).
