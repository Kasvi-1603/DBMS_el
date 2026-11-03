"""
Shared-entity network detection — the single implementation used by app.py,
seed_postgres.py and ml/anomaly_detection.py.

The graph lives in the `claim_edges` view (database/migrations/001_*.sql):
one row per pair of claims per shared entity (garage, doctor, vehicle,
witness, same customer record, phone, address). Everything here reads that
view, so the network membership, the shared-entity list and the D3 graph are
always derived from the same edges.
"""
from collections import deque

# Connected component of one claim. The recursive term only carries claim_id,
# so UNION (not UNION ALL) discards any claim that was already reached: cycles
# and overlapping paths cannot re-enter the working table, and the recursion
# stops on its own once no new claim is found. There is deliberately no depth
# cap — a cap would silently cut long chains out of the network.
NETWORK_MEMBERS_SQL = """
WITH RECURSIVE adjacency(src, dst) AS (
    SELECT claim_a, claim_b FROM claim_edges
    UNION
    SELECT claim_b, claim_a FROM claim_edges
), network(claim_id) AS (
    SELECT %(start_id)s::int
    UNION
    SELECT a.dst FROM adjacency a JOIN network n ON a.src = n.claim_id
)
SELECT claim_id FROM network ORDER BY claim_id;
"""

ENTITY_LABELS = {
    "garage": "Garage", "doctor": "Doctor", "vehicle": "Vehicle", "witness": "Witness",
    "customer": "Same customer", "phone": "Phone", "address": "Address",
}
ENTITY_ORDER = ["customer", "garage", "doctor", "vehicle", "witness", "phone", "address"]


def network_members(cur, claim_id):
    cur.execute(NETWORK_MEMBERS_SQL, {"start_id": claim_id})
    return [r["claim_id"] for r in cur.fetchall()]


def compute_networks(cur):
    """Every connected component with 2+ distinct claims, as sorted id lists.
    Components are disjoint: a claim reached from an earlier start is never
    used as a new start, so no network is listed twice."""
    cur.execute("SELECT claim_id FROM claims ORDER BY claim_id")
    all_ids = [r["claim_id"] for r in cur.fetchall()]
    visited, networks = set(), []
    for cid in all_ids:
        if cid in visited:
            continue
        members = network_members(cur, cid)
        visited.update(members)
        if len(members) > 1:
            networks.append(members)
    return networks


def network_index(networks):
    """claim_id -> (network_id, size). network_id is 'N-<lowest claim id>', so
    it stays stable when unrelated networks appear or disappear."""
    out = {}
    for members in networks:
        nid = f"N-{members[0]}"
        for cid in members:
            out[cid] = (nid, len(members))
    return out


def fetch_edges(cur, claim_ids):
    cur.execute("""
        SELECT claim_a, claim_b, link_type, shared_entity FROM claim_edges
        WHERE claim_a = ANY(%(ids)s) AND claim_b = ANY(%(ids)s)
    """, {"ids": list(claim_ids)})
    return cur.fetchall()


def _entity_labels(cur, groups, claim_ids):
    """Human-readable label for every (link_type, shared_entity) key, using the
    stored display values (not the normalized matching keys)."""
    by_type = {}
    for (ltype, key) in groups:
        by_type.setdefault(ltype, []).append(key)
    labels = {}

    # garages/doctors/witnesses can share a name while being different records
    # (they are never merged on name), so the label always carries the ID
    lookups = {
        "garage": ("SELECT garage_id AS id, name || ' (garage #' || garage_id || COALESCE(', ' || NULLIF(address, ''), '') "
                   "|| ')' AS label FROM garages WHERE garage_id = ANY(%s)", int),
        "doctor": ("SELECT doctor_id AS id, full_name || ' (doctor #' || doctor_id || ')' AS label "
                   "FROM doctors WHERE doctor_id = ANY(%s)", int),
        "vehicle": ("SELECT vehicle_id AS id, 'VIN ' || vin || COALESCE(' (' || plate_number || ')', '') AS label "
                    "FROM vehicles WHERE vehicle_id = ANY(%s)", int),
        "witness": ("SELECT witness_id AS id, full_name || ' (witness #' || witness_id || ')' AS label "
                    "FROM witnesses WHERE witness_id = ANY(%s)", int),
        "customer": ("SELECT claimant_id AS id, full_name || ' (customer #' || claimant_id || ')' AS label "
                     "FROM claimants WHERE claimant_id = ANY(%s)", int),
    }
    for ltype, (sql, cast) in lookups.items():
        keys = by_type.get(ltype)
        if not keys:
            continue
        cur.execute(sql, ([cast(k) for k in keys],))
        names = {str(r["id"]): r["label"] for r in cur.fetchall()}
        for k in keys:
            labels[(ltype, k)] = names.get(k, f"{ENTITY_LABELS[ltype]} #{k}")

    if by_type.get("phone") or by_type.get("address"):
        cur.execute("""
            SELECT claim_id, phone, address, phone_norm, address_norm
            FROM claim_customer_info WHERE claim_id = ANY(%(ids)s)
        """, {"ids": list(claim_ids)})
        rows = cur.fetchall()
        for ltype, raw_col, norm_col in (("phone", "phone", "phone_norm"), ("address", "address", "address_norm")):
            for k in by_type.get(ltype, []):
                raw = sorted({r[raw_col] for r in rows if r[norm_col] == k})
                labels[(ltype, k)] = " / ".join(raw) if raw else k
    return labels


def shared_entities(cur, claim_ids):
    """Every entity shared by 2+ of the given claims, with the exact claim ids
    that share it. Derived from claim_edges, so it agrees with the CTE."""
    claim_ids = sorted(set(claim_ids))
    if len(claim_ids) < 2:
        return []
    groups = {}
    for e in fetch_edges(cur, claim_ids):
        groups.setdefault((e["link_type"], e["shared_entity"]), set()).update((e["claim_a"], e["claim_b"]))
    labels = _entity_labels(cur, groups, claim_ids)
    out = [{
        "type": ltype,
        "type_label": ENTITY_LABELS.get(ltype, ltype),
        "key": key,
        "label": labels.get((ltype, key), key),
        "claim_ids": sorted(ids),
    } for (ltype, key), ids in groups.items()]
    out.sort(key=lambda s: (ENTITY_ORDER.index(s["type"]) if s["type"] in ENTITY_ORDER else 99, s["claim_ids"]))
    return out


def readable(entity):
    ids = ", ".join(f"#{i}" for i in entity["claim_ids"])
    return f"{entity['type_label']} — {entity['label']} — claims {ids}"


def hop_distances(claim_id, members, edges):
    """Breadth-first hop count from claim_id to every other member, over the
    same edges the CTE traversed. 1 = direct relationship, 2+ = indirect."""
    adj = {m: set() for m in members}
    for e in edges:
        adj.setdefault(e["claim_a"], set()).add(e["claim_b"])
        adj.setdefault(e["claim_b"], set()).add(e["claim_a"])
    dist, queue = {claim_id: 0}, deque([claim_id])
    while queue:
        cur_id = queue.popleft()
        for nxt in adj.get(cur_id, ()):
            if nxt not in dist:
                dist[nxt] = dist[cur_id] + 1
                queue.append(nxt)
    return dist


def build_graph(cur, claim_ids, entities=None):
    """Bipartite claim <-> shared-entity graph. An entity node exists only if
    it is shared by 2+ of these claims, and a claim connects to it only if
    that claim actually has that garage/doctor/vehicle/witness/customer/phone/
    address — the same facts listed in `entities`."""
    claim_ids = sorted(set(claim_ids))
    if not claim_ids:
        return {"nodes": [], "edges": []}
    if entities is None:
        entities = shared_entities(cur, claim_ids)
    cur.execute("""
        SELECT c.claim_id, cl.claimant_id, cl.full_name, c.risk_score
        FROM claims c JOIN policies p ON c.policy_id = p.policy_id
        JOIN claimants cl ON p.claimant_id = cl.claimant_id
        WHERE c.claim_id = ANY(%(ids)s)
    """, {"ids": claim_ids})
    nodes, edges = [], []
    for r in cur.fetchall():
        nodes.append({
            "id": f"claim-{r['claim_id']}", "type": "claim", "claim_id": r["claim_id"],
            "label": f"#{r['claim_id']}",
            "detail": f"Claim #{r['claim_id']} — {r['full_name']} (customer #{r['claimant_id']})"
                      + (f", risk {float(r['risk_score'])}" if r["risk_score"] is not None else ""),
        })
    for ent in entities:
        node_id = f"{ent['type']}-{ent['key']}"
        nodes.append({
            "id": node_id, "type": ent["type"], "label": f"{ent['type_label']}: {ent['label']}",
            "detail": readable(ent),
        })
        for cid in ent["claim_ids"]:
            edges.append({"source": f"claim-{cid}", "target": node_id, "type": ent["type"]})
    return {"nodes": nodes, "edges": edges}
