#!/usr/bin/env python3
"""
test_store_level_pricing.py

Tests ONE claim: do the Apify fast-food menu actors return genuinely
store-level pricing, or one national price list with a decorative ZIP input?

Method: run the same actor against three ZIPs in deliberately different
pricing markets, intersect the menus, and measure what share of shared
items have more than one distinct price across the three runs.

Verdict bands (from the project brief):
    > 50%   store-level pricing is real -> build on first-party data
    10-50%  partial -> scope the tool to the categories that do vary
    < 10%   national list -> first-party counter pricing is not available

Usage:
    export APIFY_TOKEN=apify_api_...
    pip install requests
    python test_store_level_pricing.py                  # all configured chains
    python test_store_level_pricing.py --chain tacobell
    python test_store_level_pricing.py --resolve-only   # find input key, no runs
    python test_store_level_pricing.py --zips 10001,67501,99501

Costs actor runs. --resolve-only and the schema-first key resolution exist
so we don't burn runs probing for the input field name.
"""

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict

try:
    import requests
except ImportError:
    sys.exit("Missing dependency. Run: pip install requests")

API = "https://api.apify.com/v2"

# --- Chains under test -------------------------------------------------------
# Taco Bell actor ID is confirmed from the brief. The Burger King and McDonald's
# IDs are the same publisher's naming convention but are UNVERIFIED -- the
# actor page was not reachable when this was written. resolve_actor() checks
# each ID against the API and, on a 404, searches the publisher's other actors
# and prints the real IDs rather than failing silently.
CHAINS = {
    "tacobell": "fortuitous_pirate~tacobell-menu-prices-nutrition-calories",
    "burgerking": "fortuitous_pirate~burgerking-menu-prices-nutrition-calories",
    "mcdonalds": "fortuitous_pirate~mcdonalds-menu-prices-nutrition-calories",
}
PUBLISHER = "fortuitous_pirate"

# Three markets chosen to maximize price signal: high-cost urban, low-cost
# midwest, high-cost remote. If pricing is store-level at all, it varies here.
DEFAULT_ZIPS = ["10001", "67501", "99501"]

# Ordered guesses for the input field name. resolve_input_key() reads the
# actor's real input schema first and only falls back to this list.
CANDIDATE_KEYS = [
    "zipCode", "zipcode", "zip", "postalCode", "postcode",
    "location", "searchLocation", "address", "storeLocation", "query",
]

# Field-name heuristics for reading the output rows.
NAME_HINTS = ["name", "item", "product", "title", "menuitem", "itemname"]
PRICE_HINTS = ["price", "cost", "amount", "priceusd"]
CATEGORY_HINTS = ["category", "section", "menucategory", "group", "type"]

MONEY = re.compile(r"-?\d+(?:[.,]\d+)?")


# --- Apify plumbing ----------------------------------------------------------

def make_session(token):
    """Auth travels in a header, not the query string.

    With a token we set the header ourselves. Without one we send no auth and
    rely on the Apify key being stored as an API credential on the cloud
    environment: Anthropic's agent proxy attaches the Authorization header
    after the request leaves the VM, which also makes api.apify.com reachable
    regardless of the environment's network access level.
    """
    s = requests.Session()
    if token:
        s.headers["Authorization"] = f"Bearer {token}"
    return s


def api_get(path, sess, **params):
    return sess.get(f"{API}/{path}", params=params, timeout=60)


def resolve_actor(actor_id, sess):
    """Confirm the actor ID exists. On 404, list the publisher's actors."""
    r = api_get(f"acts/{actor_id}", sess)
    if r.status_code == 200:
        d = r.json()["data"]
        return {"id": actor_id, "name": d.get("name"), "title": d.get("title")}
    if r.status_code == 404:
        print(f"  ! actor not found: {actor_id}")
        alt = api_get("store", sess, search=PUBLISHER, limit=50)
        if alt.status_code == 200:
            items = alt.json().get("data", {}).get("items", [])
            hits = [i for i in items if PUBLISHER in (i.get("username") or "")]
            if hits:
                print(f"  ? {PUBLISHER} publishes these actors -- correct CHAINS:")
                for i in hits:
                    print(f"      {i.get('username')}~{i.get('name')}  ({i.get('title')})")
        return None
    print(f"  ! actor lookup failed: HTTP {r.status_code} {r.text[:200]}")
    return None


def resolve_input_key(actor_id, sess):
    """Read the actor's published input schema to get the REAL field name.

    This is the whole point of doing schema-first: reading the schema costs
    nothing, while probing candidate keys costs one actor run per guess.
    Returns (key, source, schema_properties).
    """
    r = api_get(f"acts/{actor_id}/builds/default", sess)
    if r.status_code != 200:
        return None, f"schema unavailable (HTTP {r.status_code})", {}

    data = r.json().get("data", {})
    schema = data.get("inputSchema") or {}
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except json.JSONDecodeError:
            return None, "input schema was not valid JSON", {}

    props = schema.get("properties", {}) or {}
    if not props:
        return None, "input schema had no properties", {}

    required = schema.get("required", []) or []

    def score(key, spec):
        blob = f"{key} {spec.get('title','')} {spec.get('description','')}".lower()
        s = 0
        if re.search(r"zip|postal", blob):
            s += 100
        if "location" in blob or "address" in blob:
            s += 40
        if key in CANDIDATE_KEYS:
            s += 20 - CANDIDATE_KEYS.index(key)
        if key in required:
            s += 15
        if spec.get("type") == "string":
            s += 5
        return s

    ranked = sorted(props.items(), key=lambda kv: score(*kv), reverse=True)
    best, best_spec = ranked[0]
    if score(best, best_spec) < 20:
        return None, "no field in schema looks location-shaped", props
    return best, "input schema", props


def run_actor(actor_id, payload, sess, wait_s=600):
    """Start a run, poll to completion, return dataset items."""
    r = sess.post(f"{API}/acts/{actor_id}/runs", json=payload, timeout=60)
    if r.status_code not in (200, 201):
        return None, f"start failed HTTP {r.status_code}: {r.text[:300]}"

    run = r.json()["data"]
    run_id, ds_id = run["id"], run["defaultDatasetId"]

    deadline = time.time() + wait_s
    status = run["status"]
    while status in ("READY", "RUNNING") and time.time() < deadline:
        time.sleep(5)
        p = api_get(f"actor-runs/{run_id}", sess)
        if p.status_code != 200:
            return None, f"poll failed HTTP {p.status_code}"
        status = p.json()["data"]["status"]

    if status != "SUCCEEDED":
        return None, f"run ended {status}"

    d = api_get(f"datasets/{ds_id}/items", sess, clean="true", format="json")
    if d.status_code != 200:
        return None, f"dataset fetch failed HTTP {d.status_code}"
    return d.json(), None


# --- Output-shape reading ----------------------------------------------------

def pick_field(rows, hints, want_money=False):
    """Find the field holding item names / prices / categories."""
    if not rows:
        return None
    keys = defaultdict(int)
    for row in rows[:200]:
        if isinstance(row, dict):
            for k in row:
                keys[k] += 1

    def score(k):
        kl = k.lower().replace("_", "").replace(" ", "")
        s = 0
        for i, h in enumerate(hints):
            if kl == h:
                s += 100 - i
            elif h in kl:
                s += 50 - i
        if want_money:
            vals = [r.get(k) for r in rows[:50] if isinstance(r, dict)]
            if any(parse_price(v) is not None for v in vals):
                s += 30
            else:
                s -= 60
        return s

    best = max(keys, key=score) if keys else None
    return best if best and score(best) > 0 else None


def parse_price(v):
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return round(float(v), 2)
    if isinstance(v, str):
        m = MONEY.search(v.replace(",", ""))
        if m:
            try:
                return round(float(m.group()), 2)
            except ValueError:
                return None
    return None


def index_menu(rows, name_f, price_f, cat_f):
    """{normalized item name: (price, category)} -- cheapest wins on dupes."""
    out = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get(name_f)
        price = parse_price(row.get(price_f))
        if not isinstance(name, str) or price is None:
            continue
        key = re.sub(r"\s+", " ", name.strip().lower())
        cat = row.get(cat_f) if cat_f else None
        if key not in out or price < out[key][0]:
            out[key] = (price, cat)
    return out


# --- The actual measurement --------------------------------------------------

def compare(menus):
    """menus: {zip: {item: (price, cat)}} -> variance stats."""
    zips = list(menus)
    shared = set(menus[zips[0]])
    for z in zips[1:]:
        shared &= set(menus[z])

    varying, by_cat = [], defaultdict(lambda: [0, 0])
    for item in sorted(shared):
        prices = [menus[z][item][0] for z in zips]
        cat = menus[zips[0]][item][1] or "(uncategorized)"
        differs = len(set(prices)) > 1
        by_cat[cat][1] += 1
        if differs:
            by_cat[cat][0] += 1
            varying.append((item, cat, prices, max(prices) - min(prices)))

    pct = (len(varying) / len(shared) * 100) if shared else 0.0
    return {
        "zips": zips,
        "per_zip_counts": {z: len(menus[z]) for z in zips},
        "shared": len(shared),
        "varying": len(varying),
        "pct": pct,
        "by_category": {k: v for k, v in by_cat.items()},
        "examples": sorted(varying, key=lambda x: -x[3])[:10],
    }


def verdict(pct):
    if pct > 50:
        return "STORE-LEVEL PRICING IS REAL -- build on first-party data."
    if pct >= 10:
        return "PARTIAL -- scope the tool to the categories that vary (see breakdown)."
    return "NATIONAL LIST -- the ZIP input is decorative. Report back before building."


# --- Per-chain driver --------------------------------------------------------

def test_chain(chain, actor_id, zips, sess, outdir, resolve_only):
    print(f"\n{'='*72}\n{chain}  ({actor_id})\n{'='*72}")

    info = resolve_actor(actor_id, sess)
    if not info:
        return None
    print(f"  actor: {info['title'] or info['name']}")

    key, source, props = resolve_input_key(actor_id, sess)
    if key:
        print(f"  input key: '{key}'  (from {source})")
    else:
        print(f"  input key unresolved ({source}); probing CANDIDATE_KEYS -- costs runs")
    if props:
        print(f"  schema fields: {', '.join(sorted(props))}")
    if resolve_only:
        return {"chain": chain, "input_key": key, "input_key_source": source,
                "schema_fields": sorted(props)}

    tried = [key] if key else list(CANDIDATE_KEYS)
    menus, raw, working_key = {}, {}, key

    for z in zips:
        rows, err = None, None
        for cand in tried:
            print(f"  running ZIP {z} with {{'{cand}': '{z}'}} ...", flush=True)
            rows, err = run_actor(actor_id, {cand: z}, sess)
            if rows:
                working_key = cand
                tried = [cand]  # lock it in; don't probe again
                break
            print(f"    -> {err}")
        if not rows:
            print(f"  ! no data for ZIP {z}; aborting {chain}")
            return None
        raw[z] = rows
        print(f"    -> {len(rows)} rows")

    sample = raw[zips[0]]
    name_f = pick_field(sample, NAME_HINTS)
    price_f = pick_field(sample, PRICE_HINTS, want_money=True)
    cat_f = pick_field(sample, CATEGORY_HINTS)

    print(f"\n  OUTPUT SCHEMA (keys on first row):")
    if sample and isinstance(sample[0], dict):
        for k, v in sample[0].items():
            print(f"    {k}: {type(v).__name__} = {json.dumps(v)[:70]}")
    print(f"  -> name field: {name_f} | price field: {price_f} | category field: {cat_f}")

    if not name_f or not price_f:
        print("  ! could not identify name/price fields; inspect the raw dump")
        (outdir / f"{chain}_raw.json").write_text(json.dumps(raw, indent=2))
        return None

    for z in zips:
        menus[z] = index_menu(raw[z], name_f, price_f, cat_f)

    stats = compare(menus)
    stats.update({"chain": chain, "actor_id": actor_id, "input_key": working_key,
                  "input_key_source": source, "fields": {"name": name_f,
                  "price": price_f, "category": cat_f}})

    print(f"\n  items per ZIP: {stats['per_zip_counts']}")
    print(f"  shared items:  {stats['shared']}")
    print(f"  price varies:  {stats['varying']}  ({stats['pct']:.1f}%)")
    print(f"  VERDICT: {verdict(stats['pct'])}")

    if stats["by_category"]:
        print("\n  by category (varying/shared):")
        for cat, (v, t) in sorted(stats["by_category"].items(),
                                  key=lambda kv: -(kv[1][0] / kv[1][1] if kv[1][1] else 0)):
            print(f"    {cat[:40]:<42} {v}/{t}  ({v/t*100 if t else 0:.0f}%)")

    if stats["examples"]:
        print("\n  widest spreads:")
        for item, cat, prices, spread in stats["examples"]:
            print(f"    {item[:44]:<46} {prices}  spread ${spread:.2f}")

    (outdir / f"{chain}_raw.json").write_text(json.dumps(raw, indent=2))
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--chain", action="append", choices=sorted(CHAINS),
                    help="repeatable; default all")
    ap.add_argument("--actor", help="override actor ID (with a single --chain)")
    ap.add_argument("--zips", default=",".join(DEFAULT_ZIPS))
    ap.add_argument("--proxy-auth", action="store_true",
                    help="send no token; the Apify key is an API credential on "
                         "the cloud environment and the agent proxy attaches it")
    ap.add_argument("--resolve-only", action="store_true",
                    help="resolve actor + input key only; no actor runs, no cost")
    ap.add_argument("--outdir", default="pricing_test_output")
    args = ap.parse_args()

    token = os.environ.get("APIFY_TOKEN")
    if not token and not args.proxy_auth:
        sys.exit(
            "APIFY_TOKEN is not set.\n"
            "  export APIFY_TOKEN=apify_api_...\n"
            "  ...or pass --proxy-auth if the Apify key is stored as an API\n"
            "  credential on the cloud environment, where the agent proxy\n"
            "  attaches it and this script never sees it."
        )
    sess = make_session(token)
    if not token:
        print("no APIFY_TOKEN; relying on a proxy-attached API credential")

    zips = [z.strip() for z in args.zips.split(",") if z.strip()]
    if len(zips) < 2:
        sys.exit("Need at least 2 ZIPs to compare.")

    from pathlib import Path
    outdir = Path(args.outdir)
    outdir.mkdir(exist_ok=True)

    chains = args.chain or sorted(CHAINS)
    results = []
    for c in chains:
        actor = args.actor if (args.actor and len(chains) == 1) else CHAINS[c]
        try:
            r = test_chain(c, actor, zips, sess, outdir, args.resolve_only)
        except requests.RequestException as e:
            print(f"  ! network error on {c}: {e}")
            r = None
        if r:
            results.append(r)

    if results and not args.resolve_only:
        print(f"\n{'='*72}\nSUMMARY\n{'='*72}")
        for r in results:
            if "pct" in r:
                print(f"  {r['chain']:<12} key='{r['input_key']}'  "
                      f"{r['varying']}/{r['shared']} shared items vary  "
                      f"({r['pct']:.1f}%)")
        pooled = sum(r.get("varying", 0) for r in results)
        total = sum(r.get("shared", 0) for r in results)
        if total:
            print(f"\n  POOLED: {pooled}/{total} ({pooled/total*100:.1f}%)")
            print(f"  {verdict(pooled/total*100)}")

    (outdir / "results.json").write_text(json.dumps(results, indent=2, default=str))
    print(f"\n  raw dumps + results.json -> {outdir}/")


if __name__ == "__main__":
    main()
