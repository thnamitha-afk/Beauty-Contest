# app.py - Beauty Contest (hash-chained ledger version)
# Commit -> Reveal -> Results (guess 2/3 of the class average)
#
# Every commit and reveal is stored as a block in ONE append-only chain.
# Each block contains the hash of the previous block, so editing or deleting
# any earlier row breaks the chain and is detected on the Results tab.
# No setup needed: the ledger file is created by the app.

import csv
import hashlib
import io
import threading
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import streamlit as st

K_FACTOR = 2 / 3
ALLOWED_MIN, ALLOWED_MAX = 0, 100
GENESIS = "0" * 64

DATA_DIR = Path(__file__).parent / "ledger_data"
LEDGER_FILE = "chain.csv"
FIELDS = ["block", "timestamp_utc", "kind", "uni_id", "data", "prev_hash", "block_hash"]


@st.cache_resource
def get_lock():
    return threading.Lock()


# ---------------------------------------------------------------- helpers
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def block_hash(block, ts, kind, uid, data, prev):
    return sha256(f"{block}|{ts}|{kind}|{uid}|{data}|{prev}")


def to_csv_text(fieldnames, rows):
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=fieldnames)
    wr.writeheader()
    for r in rows:
        wr.writerow({k: r.get(k, "") for k in fieldnames})
    return buf.getvalue()


# ---------------------------------------------------------------- chain (append-only)
def read_chain():
    path = DATA_DIR / LEDGER_FILE
    if not path.exists():
        return []
    with get_lock():
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))


def append_block(kind: str, uni_id: str, data: str):
    """kind = 'commit' (data = commit hash) or 'reveal' (data = 'number|nonce')."""
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / LEDGER_FILE
    with get_lock():
        rows = []
        if path.exists() and path.stat().st_size > 0:
            with open(path, newline="", encoding="utf-8") as f:
                rows = list(csv.DictReader(f))
        block = len(rows)
        prev = rows[-1]["block_hash"] if rows else GENESIS
        ts = datetime.now(timezone.utc).isoformat()
        row = {
            "block": block,
            "timestamp_utc": ts,
            "kind": kind,
            "uni_id": uni_id,
            "data": data,
            "prev_hash": prev,
            "block_hash": block_hash(block, ts, kind, uni_id, data, prev),
        }
        new = not path.exists() or path.stat().st_size == 0
        with open(path, "a", newline="", encoding="utf-8") as f:  # append only
            wr = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                wr.writeheader()
            wr.writerow(row)
        return row


def verify_chain(rows):
    """Recompute every block. Returns (ok, message)."""
    prev = GENESIS
    for i, r in enumerate(rows):
        if str(r["block"]) != str(i):
            return False, f"Block numbering broken at position {i}."
        if r["prev_hash"] != prev:
            return False, f"Chain broken at block {i}: previous hash does not match."
        expected = block_hash(r["block"], r["timestamp_utc"], r["kind"], r["uni_id"], r["data"], r["prev_hash"])
        if r["block_hash"] != expected:
            return False, f"Block {i} was modified: its hash does not match its content."
        prev = r["block_hash"]
    return True, f"Chain is valid ({len(rows)} blocks)."


# ---------------------------------------------------------------- results (consensus)
def compute_results(rows):
    """Deterministic: depends only on the order of blocks."""
    commits, reveals = {}, {}
    for r in rows:
        uid = (r.get("uni_id") or "").strip()
        if not uid:
            continue
        b = int(r["block"])
        if r["kind"] == "commit":
            commits.setdefault(uid, []).append((b, r["data"].strip()))
        elif r["kind"] == "reveal":
            num, _, nonce = r["data"].partition("|")
            reveals.setdefault(uid, []).append((b, num.strip(), nonce))

    table = []
    for uid, rlist in reveals.items():
        b_rev, num_raw, nonce = max(rlist, key=lambda x: x[0])  # latest reveal
        try:
            num = int(num_raw)
        except ValueError:
            num = None
        earlier = [c for c in commits.get(uid, []) if c[0] < b_rev]  # commits before the reveal
        chosen = max(earlier, key=lambda x: x[0]) if earlier else None

        reason, verified = "ok", False
        if chosen is None:
            reason = "no_commit_before_reveal"
        elif num is None:
            reason = "number_not_int"
        elif not (ALLOWED_MIN <= num <= ALLOWED_MAX):
            reason = "out_of_range"
        else:
            verified = sha256(f"{uid}|{num}|{nonce}") == chosen[1]
            if not verified:
                reason = "hash_mismatch"
        table.append(
            {
                "uni_id": uid,
                "number": "" if num is None else str(num),
                "nonce": nonce,
                "commit_block": "" if chosen is None else str(chosen[0]),
                "reveal_block": str(b_rev),
                "verified": "True" if verified else "False",
                "reason": reason,
            }
        )

    valid = [(r["uni_id"], int(r["number"])) for r in table if r["verified"] == "True"]
    if valid:
        avg_val = mean(n for _, n in valid)
        target = K_FACTOR * avg_val
        dist = [(u, n, abs(n - target)) for u, n in valid]
        min_dist = min(d for _, _, d in dist)
        winners = [(u, n, d) for u, n, d in dist if abs(d - min_dist) < 1e-12]
    else:
        avg_val = target = min_dist = None
        winners = []

    for r in table:
        r["distance"] = f"{abs(int(r['number']) - target):.6f}" if (r["verified"] == "True" and target is not None) else ""
    leaderboard = sorted(table, key=lambda r: (r["verified"] != "True", r["distance"] or "ZZZ", r["uni_id"]))

    lines = ["=== CLASS RESULTS ===", f"k_factor = {K_FACTOR}"]
    if valid:
        lines += [
            f"participants_counted = {len(valid)}",
            f"average = {avg_val:.6f}",
            f"target = {target:.6f}",
            f"min_distance = {min_dist:.6f}",
            "winners:",
        ] + [f"  - {u} with {n} (distance {d:.6f})" for u, n, d in winners]
    else:
        lines.append("No valid verified reveals. Cannot compute winners.")

    return {
        "valid": valid,
        "avg": avg_val,
        "target": target,
        "winners": winners,
        "leaderboard": leaderboard,
        "results_txt": "\n".join(lines) + "\n",
    }


# ---------------------------------------------------------------- UI
st.set_page_config(page_title="Beauty Contest", page_icon="🎯", layout="centered")
st.title("🎯 Beauty Contest")
st.caption("Pick a number from 0 to 100. The winner is the number closest to ⅔ of the class average.")

tab_commit, tab_reveal, tab_results = st.tabs(["Commit", "Reveal", "Results"])

# ---- Commit ---------------------------------------------------------------
with tab_commit:
    st.subheader("Commit")
    st.write("Your number stays secret: only its hash is added to the chain.")
    uni_id = st.text_input("University ID", key="c_uid").strip()
    number = st.number_input("Your number (0–100)", ALLOWED_MIN, ALLOWED_MAX, 50, 1, key="c_num")
    nonce = st.text_input("Secret nonce (keep it safe)", key="c_nonce").strip()

    if uni_id and nonce:
        preimage = f"{uni_id}|{int(number)}|{nonce}"
        st.write("Your commit hash:")
        st.code(sha256(preimage))
        st.write("Write this down, you need it to reveal:")
        st.code(f"PREIMAGE: {preimage}")

    if st.button("Submit commit", type="primary"):
        if not uni_id or not nonce:
            st.error("University ID and nonce cannot be empty.")
        else:
            try:
                row = append_block("commit", uni_id, sha256(f"{uni_id}|{int(number)}|{nonce}"))
                st.success(f"Commit added as block {row['block']}. You can commit again; the latest one counts.")
                st.code(f"block_hash = {row['block_hash']}")
            except Exception as e:
                st.error(f"Could not save: {e}")

# ---- Reveal ---------------------------------------------------------------
with tab_reveal:
    st.subheader("Reveal")
    st.write("Re-enter exactly what you committed.")
    r_uid = st.text_input("University ID", key="r_uid").strip()
    r_num = st.number_input("Your number (0–100)", ALLOWED_MIN, ALLOWED_MAX, 50, 1, key="r_num")
    r_nonce = st.text_input("Your secret nonce", key="r_nonce").strip()

    if st.button("Submit reveal", type="primary"):
        if not r_uid or not r_nonce:
            st.error("University ID and nonce cannot be empty.")
        else:
            try:
                mine = [c for c in read_chain() if c["kind"] == "commit" and c["uni_id"].strip() == r_uid]
                latest = mine[-1]["data"].strip() if mine else ""
                row = append_block("reveal", r_uid, f"{int(r_num)}|{r_nonce}")
                if latest == sha256(f"{r_uid}|{int(r_num)}|{r_nonce}"):
                    st.success(f"Reveal added as block {row['block']} and it matches your commit ✅")
                else:
                    st.warning(
                        f"Reveal added as block {row['block']}, but it does NOT match your latest commit. "
                        "Check your inputs and reveal again."
                    )
            except Exception as e:
                st.error(f"Could not save: {e}")

# ---- Results --------------------------------------------------------------
with tab_results:
    st.subheader("Results")
    st.write(
        "First the chain is verified block by block. Then, for each University ID, the latest reveal is matched to the "
        "latest commit made before it. A reveal counts if its hash matches that commit and the number is within 0–100."
    )
    if st.button("Show results", type="primary"):
        try:
            rows = read_chain()
            ok, msg = verify_chain(rows)
            (st.success if ok else st.error)(("⛓️ " if ok else "⚠️ ") + msg)

            res = compute_results(rows)
            if res["valid"]:
                c1, c2, c3 = st.columns(3)
                c1.metric("Participants", len(res["valid"]))
                c2.metric("Average", f"{res['avg']:.4f}")
                c3.metric("Target (⅔ × avg)", f"{res['target']:.4f}")
                st.success("🏆 Winner(s): " + ", ".join(f"{u} ({n})" for u, n, _ in res["winners"]))
            else:
                st.warning("No valid reveals yet.")

            st.markdown("**Leaderboard**")
            st.dataframe(
                [
                    {k: r.get(k, "") for k in ["uni_id", "number", "verified", "reason", "distance"]}
                    for r in res["leaderboard"]
                ],
                use_container_width=True,
            )
            with st.expander("Ledger blocks"):
                st.dataframe(rows, use_container_width=True)

            d1, d2, d3 = st.columns(3)
            d1.download_button("chain.csv", to_csv_text(FIELDS, rows), "chain.csv", "text/csv")
            d2.download_button(
                "class_leaderboard.csv",
                to_csv_text(
                    ["uni_id", "number", "verified", "reason", "distance", "commit_block", "reveal_block", "nonce"],
                    res["leaderboard"],
                ),
                "class_leaderboard.csv",
                "text/csv",
            )
            d3.download_button("class_results.txt", res["results_txt"], "class_results.txt", "text/plain")
        except Exception as e:
            st.error(f"Could not read the ledger: {e}")
