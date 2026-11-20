# app.py - Beauty Contest (Streamlit version of commit_client.py, reveal_client.py
# and merge_consensus_from_api.py)
#
# The API URL (Google Apps Script web app ending in /exec) can be set once in
# Streamlit secrets (API_URL = "https://script.google.com/macros/s/.../exec")
# or pasted in the sidebar, exactly like the original scripts.

import csv
import hashlib
import io
from datetime import datetime, timezone
from statistics import mean

import requests
import streamlit as st

API_DEFAULT = "https://script.google.com/macros/s/REPLACE_WITH_YOUR_/exec"

# Deadlines are enforced in UTC to avoid timezone issues.
# Commit deadline: 15 Nov 2026 23:59:59 Europe/Paris (UTC+1) == 22:59:59 UTC
COMMIT_DEADLINE_UTC = datetime(2026, 11, 15, 22, 59, 59, tzinfo=timezone.utc)
# Reveal opens right after: 16 Nov 2026 00:00:00 Paris == 23:00:00 UTC on 15 Nov
REVEAL_OPEN_UTC = datetime(2026, 11, 15, 23, 0, 0, tzinfo=timezone.utc)

# Consensus parameters
K_FACTOR = 2 / 3
ALLOWED_MIN, ALLOWED_MAX = 0, 100

MIN_DT = datetime.min.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------- helpers
def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def now_utc():
    return datetime.now(timezone.utc)


def parse_ts(ts: str):
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def get_csv(url: str):
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.text)))


def to_csv_text(fieldnames, rows):
    buf = io.StringIO()
    wr = csv.DictWriter(buf, fieldnames=fieldnames)
    wr.writeheader()
    for r in rows:
        wr.writerow({k: r.get(k, "") for k in fieldnames})
    return buf.getvalue()


def default_api():
    try:
        return st.secrets.get("API_URL", API_DEFAULT)
    except Exception:
        return API_DEFAULT


def api_ready(api: str) -> bool:
    return api.startswith("https://") and "REPLACE_WITH_YOUR" not in api


# ---------------------------------------------------------------- consensus
def merge_consensus(base: str):
    commits_raw = get_csv(f"{base}?table=commits")
    reveals_raw = get_csv(f"{base}?table=reveals")

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
        # latest reveal
        latest_rev = sorted(rlist, key=lambda x: x["ts_parsed"] or MIN_DT)[-1]
        ts_rev = latest_rev["ts_parsed"] or MIN_DT

        try:
            num = int(latest_rev["number_raw"])
        except Exception:
            num = None

        # latest commit made before or at the reveal
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

    leaderboard = sorted(
        verified_rows, key=lambda r: (r["verified"] != "True", r.get("distance") or "ZZZ", r["uni_id"])
    )
    merged_commits = sorted(matched_commits.values(), key=lambda r: (r["uni_id"], r["timestamp_utc"]))
    merged_reveals = sorted(
        [r for r in verified_rows if r["verified"] == "True"], key=lambda r: (r["uni_id"], r["timestamp_utc"])
    )

    lines = ["=== CLASS RESULTS (Latest-Attempt Consensus) ===", f"k_factor = {K_FACTOR}"]
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

api = st.sidebar.text_input("API URL (ends with /exec)", value=default_api()).strip()
if not api_ready(api):
    st.sidebar.warning("Paste the real API URL (the placeholder is still in place).")

current = now_utc()
if current <= COMMIT_DEADLINE_UTC:
    st.info("Commit phase is **open** (deadline 15 Nov 2026, 23:59:59 Paris time).")
else:
    st.warning("Commit window is **closed** (deadline was 15 Nov 2026, 23:59:59 Paris time).")

tab_commit, tab_reveal, tab_merge = st.tabs(["Commit (at home)", "Reveal (in class)", "Consensus"])

# ---- Commit ---------------------------------------------------------------
with tab_commit:
    st.subheader("Commit phase")
    uni_id = st.text_input("Your University ID", key="c_uid").strip()
    number = st.number_input("Choose your number (0..100)", 0, 100, 50, 1, key="c_num")
    nonce = st.text_input("Choose a secret nonce (keep it safe)", key="c_nonce").strip()

    if uni_id and nonce:
        preimage = f"{uni_id}|{int(number)}|{nonce}"
        st.write("Your commit hash:")
        st.code(sha256(preimage))
        st.write("Write this down (you must re-enter it in class):")
        st.code(f"PREIMAGE: {preimage}")

    if st.button("Submit commit", type="primary"):
        if now_utc() > COMMIT_DEADLINE_UTC:
            st.error("⛔ Commit window is CLOSED.")
        elif not api_ready(api):
            st.error("Paste the API URL in the sidebar first.")
        elif not uni_id or not nonce:
            st.error("University ID and nonce cannot be empty.")
        else:
            payload = {"kind": "commit", "uni_id": uni_id, "commit": sha256(f"{uni_id}|{int(number)}|{nonce}")}
            try:
                r = requests.post(api, json=payload, timeout=15)
                st.write("Server:", r.status_code, r.text)
            except Exception as e:
                st.error(f"Network error: {e}")

# ---- Reveal ---------------------------------------------------------------
with tab_reveal:
    st.subheader("Reveal phase")
    if now_utc() < REVEAL_OPEN_UTC:
        st.info("⛔ Reveal window is not open yet. It opens after the commit deadline.")
    st.write("Re-enter EXACTLY what you committed at home.")
    r_uid = st.text_input("Your University ID", key="r_uid").strip()
    r_num = st.number_input("Your number (0..100)", 0, 100, 50, 1, key="r_num")
    r_nonce = st.text_input("Your secret nonce", key="r_nonce").strip()

    if st.button("Submit reveal", type="primary"):
        if now_utc() < REVEAL_OPEN_UTC:
            st.error("⛔ Reveal window is NOT open yet.")
        elif not api_ready(api):
            st.error("Paste the API URL in the sidebar first.")
        elif not r_uid or not r_nonce:
            st.error("University ID and nonce cannot be empty.")
        else:
            payload = {"kind": "reveal", "uni_id": r_uid, "number": int(r_num), "nonce": r_nonce}
            try:
                r = requests.post(api, json=payload, timeout=15)
                st.write("Server:", r.status_code, r.text)
            except Exception as e:
                st.error(f"Network error: {e}")

# ---- Consensus ------------------------------------------------------------
with tab_merge:
    st.subheader("Merge & consensus")
    st.write(
        "Latest reveal per University ID, matched to the latest commit made at or before it. "
        "A reveal counts if SHA256(uni_id|number|nonce) equals that commit and the number is in [0, 100]. "
        "Target = ⅔ × class average; the closest number wins."
    )
    if st.button("Run consensus", type="primary"):
        if not api_ready(api):
            st.error("Paste the API URL in the sidebar first.")
        else:
            try:
                res = merge_consensus(api)
                if res["valid"]:
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Participants", len(res["valid"]))
                    c2.metric("Average", f"{res['avg']:.4f}")
                    c3.metric("Target (⅔ × avg)", f"{res['target']:.4f}")
                    st.success("🏆 Winner(s): " + ", ".join(f"{u} ({n})" for u, n, _ in res["winners"]))
                else:
                    st.warning("No valid verified reveals. Cannot compute winners.")

                st.markdown("**Leaderboard**")
                st.dataframe(
                    [{k: r.get(k, "") for k in ["uni_id", "number", "verified", "reason", "distance"]} for r in res["leaderboard"]],
                    use_container_width=True,
                )
                d1, d2 = st.columns(2)
                d1.download_button(
                    "merged_commits.csv",
                    to_csv_text(["timestamp_utc", "uni_id", "commit"], res["merged_commits"]),
                    "merged_commits.csv",
                    "text/csv",
                )
                d2.download_button(
                    "merged_reveals.csv",
                    to_csv_text(
                        ["timestamp_utc", "uni_id", "number", "nonce", "preimage_hash", "verified"], res["merged_reveals"]
                    ),
                    "merged_reveals.csv",
                    "text/csv",
                )
                d3, d4 = st.columns(2)
                d3.download_button(
                    "class_leaderboard.csv",
                    to_csv_text(
                        ["uni_id", "number", "verified", "reason", "distance", "commit", "nonce", "timestamp_utc"],
                        res["leaderboard"],
                    ),
                    "class_leaderboard.csv",
                    "text/csv",
                )
                d4.download_button("class_results.txt", res["results_txt"], "class_results.txt", "text/plain")
            except Exception as e:
                st.error(f"Could not read the ledger: {e}")
