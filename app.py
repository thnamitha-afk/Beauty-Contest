# app.py - Beauty Contest
# Commit -> Reveal -> Results (guess 2/3 of the class average)
#
# Works out of the box: the ledgers are append-only CSV files kept by the app.
# Optional: set API_URL in Streamlit secrets to use an external Google Sheets
# ledger instead (not required).

import csv
import hashlib
import io
import threading
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean

import requests
import streamlit as st

K_FACTOR = 2 / 3
ALLOWED_MIN, ALLOWED_MAX = 0, 100

DATA_DIR = Path(__file__).parent / "ledger_data"
FIELDS = {
    "commits": ["timestamp_utc", "uni_id", "commit"],
    "reveals": ["timestamp_utc", "uni_id", "number", "nonce"],
}
MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


@st.cache_resource
def get_lock():
    return threading.Lock()


# ---------------------------------------------------------------- helpers
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def parse_ts(ts: str):
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def api_url():
    try:
        return st.secrets.get("API_URL")
    except Exception:
        return None


def to_csv_text(fieldnames, rows):
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=fieldnames)
    wr.writeheader()
    for r in rows:
        wr.writerow({k: r.get(k, "") for k in fieldnames})
    return buf.getvalue()


# ---------------------------------------------------------------- ledger (append-only)
def ledger_append(kind: str, record: dict):
    api = api_url()
    if api:
        r = requests.post(api, json={"kind": kind, **record}, timeout=20)
        r.raise_for_status()
        return
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{kind}.csv"
    row = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), **record}
    with get_lock():
        new = (not path.exists()) or path.stat().st_size == 0
        with open(path, "a", newline="", encoding="utf-8") as f:  # append only
            wr = csv.DictWriter(f, fieldnames=FIELDS[kind])
            if new:
                wr.writeheader()
            wr.writerow(row)


def ledger_read(kind: str):
    api = api_url()
    if api:
        r = requests.get(api, params={"table": kind}, timeout=20)
        r.raise_for_status()
        return list(csv.DictReader(io.StringIO(r.text)))
    path = DATA_DIR / f"{kind}.csv"
    if not path.exists():
        return []
    with get_lock():
        with open(path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))


# ---------------------------------------------------------------- results (consensus)
def compute_results(commits_raw, reveals_raw):
    commits = {}
    for r in commits_raw:
        uid = (r.get("uni_id") or "").strip()
        ch = (r.get("commit") or "").strip()
        ts = (r.get("timestamp_utc") or "").strip()
        if not uid or not ch:
            continue
        commits.setdefault(uid, []).append(
            {"timestamp_utc": ts, "ts_parsed": parse_ts(ts), "uni_id": uid, "commit": ch}
        )

    reveals = {}
    for r in reveals_raw:
        uid = (r.get("uni_id") or "").strip()
        num = (r.get("number") or "").strip()
        nonce = (r.get("nonce") or "").strip()
        ts = (r.get("timestamp_utc") or "").strip()
        if not uid or not num or not nonce:
            continue
        reveals.setdefault(uid, []).append(
            {"timestamp_utc": ts, "ts_parsed": parse_ts(ts), "uni_id": uid, "number_raw": num, "nonce": nonce}
        )

    verified_rows, matched_commits = [], {}
    for uid, rlist in reveals.items():
        latest_rev = sorted(rlist, key=lambda x: x["ts_parsed"] or MIN_DT)[-1]  # latest reveal
        ts_rev = latest_rev["ts_parsed"] or MIN_DT
        try:
            num = int(latest_rev["number_raw"])
        except Exception:
            num = None

        # latest commit made at or before that reveal
        cands = [c for c in commits.get(uid, []) if (c["ts_parsed"] or MIN_DT) <= ts_rev]
        cands.sort(key=lambda x: x["ts_parsed"] or MIN_DT)
        chosen = cands[-1] if cands else None
        if chosen:
            matched_commits[uid] = chosen

        reason, verified = "ok", False
        if chosen is None:
            reason = "no_commit_before_reveal"
        elif num is None:
            reason = "number_not_int"
        elif not (ALLOWED_MIN <= num <= ALLOWED_MAX):
            reason = "out_of_range"
        else:
            verified = sha256(f"{uid}|{num}|{latest_rev['nonce']}") == chosen["commit"]
            if not verified:
                reason = "hash_mismatch"

        verified_rows.append(
            {
                "uni_id": uid,
                "timestamp_utc": latest_rev["timestamp_utc"],
                "number": "" if num is None else str(num),
                "nonce": latest_rev["nonce"],
                "commit": chosen["commit"] if chosen else "",
                "preimage_hash": "" if num is None else sha256(f"{uid}|{num}|{latest_rev['nonce']}"),
                "verified": "True" if verified else "False",
                "reason": reason,
            }
        )

    valid = [(r["uni_id"], int(r["number"])) for r in verified_rows if r["verified"] == "True" and r["number"] != ""]
    if valid:
        avg_val = mean(n for _, n in valid)
        target = K_FACTOR * avg_val
        distances = [(uid, n, abs(n - target)) for uid, n in valid]
        min_dist = min(d for _, _, d in distances)
        winners = [(uid, n, d) for uid, n, d in distances if abs(d - min_dist) < 1e-12]
    else:
        avg_val = target = min_dist = None
        winners = []

    for r in verified_rows:
        r["distance"] = f"{abs(int(r['number']) - target):.6f}" if (r["verified"] == "True" and target is not None) else ""

    leaderboard = sorted(verified_rows, key=lambda r: (r["verified"] != "True", r.get("distance") or "ZZZ", r["uni_id"]))
    merged_commits = sorted(matched_commits.values(), key=lambda r: (r["uni_id"], r["timestamp_utc"]))
    merged_reveals = sorted(
        [r for r in verified_rows if r["verified"] == "True"], key=lambda r: (r["uni_id"], r["timestamp_utc"])
    )

    lines = ["=== CLASS RESULTS ===", f"k_factor = {K_FACTOR}"]
    if valid:
        lines += [
            f"participants_counted = {len(valid)}",
            f"average = {avg_val:.6f}",
            f"target = {target:.6f}",
            f"min_distance = {min_dist:.6f}",
            "winners:",
        ] + [f"  - {uid} with {n} (distance {d:.6f})" for uid, n, d in winners]
    else:
        lines.append("No valid verified reveals. Cannot compute winners.")

    return {
        "valid": valid,
        "avg": avg_val,
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
st.caption("Pick a number from 0 to 100. The winner is the number closest to ⅔ of the class average.")

tab_commit, tab_reveal, tab_results = st.tabs(["Commit", "Reveal", "Results"])

# ---- Commit ---------------------------------------------------------------
with tab_commit:
    st.subheader("Commit")
    st.write("Your number stays secret: only its hash is stored.")
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
                ledger_append("commits", {"uni_id": uni_id, "commit": sha256(f"{uni_id}|{int(number)}|{nonce}")})
                st.success("Commit saved. You can commit again; the latest one counts.")
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
                ledger_append("reveals", {"uni_id": r_uid, "number": int(r_num), "nonce": r_nonce})
                mine = [c for c in ledger_read("commits") if (c.get("uni_id") or "").strip() == r_uid]
                latest = (mine[-1].get("commit") or "").strip() if mine else ""
                if latest == sha256(f"{r_uid}|{int(r_num)}|{r_nonce}"):
                    st.success("Reveal saved and it matches your commit ✅")
                else:
                    st.warning("Reveal saved, but it does NOT match your latest commit. Check your inputs and reveal again.")
            except Exception as e:
                st.error(f"Could not save: {e}")

# ---- Results --------------------------------------------------------------
with tab_results:
    st.subheader("Results")
    st.write(
        "For each University ID: latest reveal, matched to the latest commit made before it. "
        "A reveal counts if its hash matches that commit and the number is within 0–100."
    )
    if st.button("Show results", type="primary"):
        try:
            commits_raw, reveals_raw = ledger_read("commits"), ledger_read("reveals")
            res = compute_results(commits_raw, reveals_raw)
            st.caption(f"{len(commits_raw)} commits · {len(reveals_raw)} reveals")
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
                [{k: r.get(k, "") for k in ["uni_id", "number", "verified", "reason", "distance"]} for r in res["leaderboard"]],
                use_container_width=True,
            )
            d1, d2 = st.columns(2)
            d1.download_button(
                "class_leaderboard.csv",
                to_csv_text(
                    ["uni_id", "number", "verified", "reason", "distance", "commit", "nonce", "timestamp_utc"],
                    res["leaderboard"],
                ),
                "class_leaderboard.csv",
                "text/csv",
            )
            d2.download_button("class_results.txt", res["results_txt"], "class_results.txt", "text/plain")
            d3, d4 = st.columns(2)
            d3.download_button(
                "commits.csv", to_csv_text(FIELDS["commits"], commits_raw), "commits.csv", "text/csv"
            )
            d4.download_button(
                "reveals.csv", to_csv_text(FIELDS["reveals"], reveals_raw), "reveals.csv", "text/csv"
            )
        except Exception as e:
            st.error(f"Could not read the ledger: {e}")
