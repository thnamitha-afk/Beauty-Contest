# app.py - Beauty Contest
# Commit-reveal version of the "guess 2/3 of the class average" game.
# Self-contained: no external API, no setup. Ledgers are append-only CSV files
# stored next to the app (ledger_data/commits.csv and ledger_data/reveals.csv).

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

DATA_DIR = Path("ledger_data")
COMMITS_FILE = DATA_DIR / "commits.csv"
REVEALS_FILE = DATA_DIR / "reveals.csv"
COMMIT_COLS = ["timestamp_utc", "uni_id", "commit"]
REVEAL_COLS = ["timestamp_utc", "uni_id", "number", "nonce"]

_LOCK = threading.Lock()
MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- helpers
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_ts(ts: str):
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def append_row(path: Path, cols, row: dict):
    """Append-only write: existing rows are never modified or deleted."""
    DATA_DIR.mkdir(exist_ok=True)
    with _LOCK:
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            if new:
                w.writeheader()
            w.writerow({"timestamp_utc": now_iso(), **row})


def read_rows(path: Path):
    if not path.exists():
        return []
    with _LOCK:
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))


def to_csv_text(fieldnames, rows):
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=fieldnames)
    wr.writeheader()
    for r in rows:
        wr.writerow({k: r.get(k, "") for k in fieldnames})
    return buf.getvalue()


# ---------------------------------------------------------------- consensus
def consensus(commits_raw, reveals_raw):
    commits = {}
    for i, r in enumerate(commits_raw):
        uid = (r.get("uni_id") or "").strip()
        ch = (r.get("commit") or "").strip()
        ts = (r.get("timestamp_utc") or "").strip()
        if uid and ch:
            commits.setdefault(uid, []).append(
                {"timestamp_utc": ts, "ts": parse_ts(ts) or MIN_DT, "i": i, "uni_id": uid, "commit": ch}
            )

    reveals = {}
    for i, r in enumerate(reveals_raw):
        uid = (r.get("uni_id") or "").strip()
        num = (r.get("number") or "").strip()
        nonce = (r.get("nonce") or "").strip()
        ts = (r.get("timestamp_utc") or "").strip()
        if uid and num and nonce:
            reveals.setdefault(uid, []).append(
                {"timestamp_utc": ts, "ts": parse_ts(ts) or MIN_DT, "i": i, "number_raw": num, "nonce": nonce}
            )

    rows, matched = [], {}
    for uid, rlist in reveals.items():
        latest = sorted(rlist, key=lambda x: (x["ts"], x["i"]))[-1]  # latest reveal
        try:
            num = int(latest["number_raw"])
        except Exception:
            num = None

        # latest commit made at or before that reveal
        cands = sorted(
            [c for c in commits.get(uid, []) if c["ts"] <= latest["ts"]], key=lambda x: (x["ts"], x["i"])
        )
        chosen = cands[-1] if cands else None
        if chosen:
            matched[uid] = chosen

        reason, verified = "ok", False
        if chosen is None:
            reason = "no_commit_before_reveal"
        elif num is None:
            reason = "number_not_int"
        elif not (ALLOWED_MIN <= num <= ALLOWED_MAX):
            reason = "out_of_range"
        else:
            verified = sha256(f"{uid}|{num}|{latest['nonce']}") == chosen["commit"]
            if not verified:
                reason = "hash_mismatch"

        rows.append(
            {
                "uni_id": uid,
                "timestamp_utc": latest["timestamp_utc"],
                "number": "" if num is None else str(num),
                "nonce": latest["nonce"],
                "commit": chosen["commit"] if chosen else "",
                "preimage_hash": "" if num is None else sha256(f"{uid}|{num}|{latest['nonce']}"),
                "verified": "True" if verified else "False",
                "reason": reason,
            }
        )

    valid = [(r["uni_id"], int(r["number"])) for r in rows if r["verified"] == "True"]
    if valid:
        avg = mean(n for _, n in valid)
        target = K_FACTOR * avg
        dists = [(u, n, abs(n - target)) for u, n in valid]
        best = min(d for _, _, d in dists)
        winners = [(u, n, d) for u, n, d in dists if abs(d - best) < 1e-12]
    else:
        avg = target = best = None
        winners = []

    for r in rows:
        r["distance"] = f"{abs(int(r['number']) - target):.6f}" if (r["verified"] == "True" and target is not None) else ""

    leaderboard = sorted(rows, key=lambda r: (r["verified"] != "True", r["distance"] or "ZZZ", r["uni_id"]))
    merged_commits = sorted(matched.values(), key=lambda r: (r["uni_id"], r["timestamp_utc"]))
    merged_reveals = sorted([r for r in rows if r["verified"] == "True"], key=lambda r: (r["uni_id"], r["timestamp_utc"]))

    lines = ["=== CLASS RESULTS (Latest-Attempt Consensus) ===", f"k_factor = {K_FACTOR}"]
    if valid:
        lines += [
            f"participants_counted = {len(valid)}",
            f"average = {avg:.6f}",
            f"target = {target:.6f}",
            f"min_distance = {best:.6f}",
            "winners:",
        ] + [f"  - {u} with {n} (distance {d:.6f})" for u, n, d in winners]
    else:
        lines.append("No valid verified reveals. Cannot compute winners.")

    return {
        "valid": valid,
        "avg": avg,
        "target": target,
        "winners": winners,
        "leaderboard": leaderboard,
        "merged_commits": merged_commits,
        "merged_reveals": merged_reveals,
        "results_txt": "\n".join(lines) + "\n",
    }


# ---------------------------------------------------------------- UI
st.set_page_config(page_title="Beauty Contest", page_icon="🎯", layout="centered")
st.title("🎯 Beauty Contest")
st.caption("Pick a number from 0 to 100. The winner is closest to ⅔ of the class average.")

tab_commit, tab_reveal = st.tabs(["Commit", "Reveal"])

# ---- Commit ---------------------------------------------------------------
with tab_commit:
    st.subheader("Commit")
    st.write("Only the hash of your number is published. Your number stays secret until you reveal it.")
    c_uid = st.text_input("Your University ID", key="c_uid").strip()
    c_num = st.number_input("Choose your number (0..100)", ALLOWED_MIN, ALLOWED_MAX, 50, 1, key="c_num")
    c_nonce = st.text_input("Choose a secret nonce (keep it safe)", key="c_nonce").strip()

    if c_uid and c_nonce:
        preimage = f"{c_uid}|{int(c_num)}|{c_nonce}"
        st.write("Your commit hash:")
        st.code(sha256(preimage))
        st.write("Write this down, you must re-enter it in the Reveal tab:")
        st.code(f"PREIMAGE: {preimage}")

    if st.button("Submit commit", type="primary"):
        if not c_uid or not c_nonce:
            st.error("University ID and nonce cannot be empty.")
        else:
            append_row(COMMITS_FILE, COMMIT_COLS, {"uni_id": c_uid, "commit": sha256(f"{c_uid}|{int(c_num)}|{c_nonce}")})
            st.success("Commit recorded. You can commit again; the latest one counts.")

    commits_rows = read_rows(COMMITS_FILE)
    st.markdown(f"**Commits ledger** ({len(commits_rows)} rows)")
    st.dataframe(commits_rows, use_container_width=True)
    st.download_button("Download commits.csv", to_csv_text(COMMIT_COLS, commits_rows), "commits.csv", "text/csv")

# ---- Reveal ---------------------------------------------------------------
with tab_reveal:
    st.subheader("Reveal")
    st.write("Re-enter EXACTLY what you committed.")
    r_uid = st.text_input("Your University ID", key="r_uid").strip()
    r_num = st.number_input("Your number (0..100)", ALLOWED_MIN, ALLOWED_MAX, 50, 1, key="r_num")
    r_nonce = st.text_input("Your secret nonce", key="r_nonce").strip()

    if st.button("Submit reveal", type="primary"):
        if not r_uid or not r_nonce:
            st.error("University ID and nonce cannot be empty.")
        else:
            append_row(REVEALS_FILE, REVEAL_COLS, {"uni_id": r_uid, "number": int(r_num), "nonce": r_nonce})
            mine = [r for r in read_rows(COMMITS_FILE) if r["uni_id"].strip() == r_uid]
            if mine and mine[-1]["commit"].strip() == sha256(f"{r_uid}|{int(r_num)}|{r_nonce}"):
                st.success("✅ Reveal recorded and it matches your latest commit.")
            elif mine:
                st.warning("Reveal recorded, but it does NOT match your latest commit, so it will not count. Check your inputs and reveal again.")
            else:
                st.warning("Reveal recorded, but no commit was found for this University ID, so it will not count.")

    reveals_rows = read_rows(REVEALS_FILE)
    st.markdown(f"**Reveals ledger** ({len(reveals_rows)} rows)")
    st.dataframe(reveals_rows, use_container_width=True)
    st.download_button("Download reveals.csv", to_csv_text(REVEAL_COLS, reveals_rows), "reveals.csv", "text/csv")

    st.divider()
    st.subheader("Results")
    st.caption(
        "Latest reveal per ID, matched to the latest commit made before it. A reveal counts if its hash matches "
        "that commit and the number is between 0 and 100."
    )
    res = consensus(read_rows(COMMITS_FILE), reveals_rows)
    if res["valid"]:
        m1, m2, m3 = st.columns(3)
        m1.metric("Participants", len(res["valid"]))
        m2.metric("Average", f"{res['avg']:.4f}")
        m3.metric("Target (⅔ × avg)", f"{res['target']:.4f}")
        st.success("🏆 Winner(s): " + ", ".join(f"{u} ({n})" for u, n, _ in res["winners"]))
        st.markdown("**Leaderboard**")
        st.dataframe(
            [{k: r.get(k, "") for k in ["uni_id", "number", "verified", "reason", "distance"]} for r in res["leaderboard"]],
            use_container_width=True,
        )
        d1, d2 = st.columns(2)
        d1.download_button("class_leaderboard.csv", to_csv_text(
            ["uni_id", "number", "verified", "reason", "distance", "commit", "nonce", "timestamp_utc"], res["leaderboard"]
        ), "class_leaderboard.csv", "text/csv")
        d2.download_button("class_results.txt", res["results_txt"], "class_results.txt", "text/plain")
        d3, d4 = st.columns(2)
        d3.download_button("merged_commits.csv", to_csv_text(COMMIT_COLS, res["merged_commits"]), "merged_commits.csv", "text/csv")
        d4.download_button("merged_reveals.csv", to_csv_text(
            ["timestamp_utc", "uni_id", "number", "nonce", "preimage_hash", "verified"], res["merged_reveals"]
        ), "merged_reveals.csv", "text/csv")
    else:
        st.info("No valid reveals yet.")
