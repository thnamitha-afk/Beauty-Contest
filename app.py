"""
Decentralized Beauty Contest (Keynesian Beauty Contest, 2/3 of the average)
NEOMA Fintech & DeFi course

Blockchain analogy
  commit-reveal  -> transaction lifecycle
  ledger (CSV)   -> append-only blockchain
  consensus tab  -> deterministic merge anyone can re-run

Ledger backends (picked automatically)
  1. Google Sheets + Apps Script web app, if API_URL is set in Streamlit secrets
     (persistent, recommended for a real class).
  2. Local append-only CSV files (works out of the box, but Streamlit Community
     Cloud wipes local files when the app restarts or sleeps).

Optional Streamlit secrets (App settings -> Secrets):
  API_URL  = "https://script.google.com/macros/s/XXXX/exec"
  DEADLINE = "2026-11-15 23:59:59"      # Paris time
"""

import csv
import hashlib
import io
import secrets as pysecrets
import threading
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

PARIS = ZoneInfo("Europe/Paris")
DEFAULT_DEADLINE = "2026-11-15 23:59:59"
DATA_DIR = Path("ledger_data")
COLUMNS = {
    "commits": ["timestamp_utc", "uni_id", "commit"],
    "reveals": ["timestamp_utc", "uni_id", "number", "nonce"],
}
_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------
def get_secret(key, default=None):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def get_deadline() -> datetime:
    raw = str(get_secret("DEADLINE", DEFAULT_DEADLINE))
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=PARIS)


API_URL = get_secret("API_URL")


# --------------------------------------------------------------------------
# Cryptography
# --------------------------------------------------------------------------
def normalize_number(text: str):
    """Return a canonical string for the number (so '50', '50.0' hash the same) or None."""
    try:
        d = Decimal(str(text).strip().replace(",", "."))
    except InvalidOperation:
        return None
    if not d.is_finite() or d < 0 or d > 100:
        return None
    return format(d.normalize(), "f")


def make_commit(uni_id: str, number: str, nonce: str) -> str:
    return hashlib.sha256(f"{uni_id}|{number}|{nonce}".encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Ledger (append-only)
# --------------------------------------------------------------------------
def ledger_append(kind: str, record: dict):
    if API_URL:
        payload = {"kind": kind, **record}
        r = requests.post(API_URL, json=payload, timeout=30)
        r.raise_for_status()
        return
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{kind}.csv"
    row = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), **record}
    with _LOCK:
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:  # "a" = append only
            w = csv.DictWriter(f, fieldnames=COLUMNS[kind])
            if new:
                w.writeheader()
            w.writerow(row)


def ledger_read(kind: str) -> pd.DataFrame:
    if API_URL:
        r = requests.get(API_URL, params={"table": kind}, timeout=30)
        r.raise_for_status()
        return pd.read_csv(io.StringIO(r.text), dtype=str).fillna("")
    path = DATA_DIR / f"{kind}.csv"
    if not path.exists():
        return pd.DataFrame(columns=COLUMNS[kind])
    with _LOCK:
        return pd.read_csv(path, dtype=str).fillna("")


# --------------------------------------------------------------------------
# Consensus (deterministic: same ledgers + same deadline => same result)
# --------------------------------------------------------------------------
def run_consensus(commits: pd.DataFrame, reveals: pd.DataFrame, deadline: datetime):
    deadline_utc = deadline.astimezone(timezone.utc)
    c = commits.copy()
    r = reveals.copy()
    for df in (c, r):
        df["uni_id"] = df["uni_id"].astype(str).str.strip()
        df["ts"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
        df["row"] = range(len(df))

    rows = []
    for uni_id in sorted(r["uni_id"].unique()):
        # latest reveal
        rv = r[r["uni_id"] == uni_id].sort_values(["ts", "row"]).iloc[-1]
        # latest commit made before the deadline AND before that reveal
        cm = c[(c["uni_id"] == uni_id) & (c["ts"] <= deadline_utc) & (c["ts"] <= rv["ts"])]
        number_norm = normalize_number(rv["number"])
        status, commit = "valid", ""
        if cm.empty:
            status = "no prior commit"
        else:
            commit = cm.sort_values(["ts", "row"]).iloc[-1]["commit"].strip().lower()
            if number_norm is None:
                status = "number out of range / invalid"
            elif make_commit(uni_id, number_norm, rv["nonce"]) != commit:
                status = "hash mismatch"
        rows.append(
            {
                "uni_id": uni_id,
                "number": number_norm if number_norm is not None else rv["number"],
                "commit": commit,
                "reveal_time_utc": rv["timestamp_utc"],
                "status": status,
            }
        )

    merged = pd.DataFrame(rows, columns=["uni_id", "number", "commit", "reveal_time_utc", "status"])
    valid = merged[merged["status"] == "valid"].copy()
    if valid.empty:
        return merged, valid, None, None, []

    valid["number"] = valid["number"].astype(float)
    average = valid["number"].mean()
    target = 2 / 3 * average
    valid["distance"] = (valid["number"] - target).abs()
    valid = valid.sort_values(["distance", "uni_id"]).reset_index(drop=True)
    valid.insert(0, "rank", valid["distance"].rank(method="min").astype(int))
    winners = valid[valid["distance"] == valid["distance"].min()]["uni_id"].tolist()
    return merged, valid, average, target, winners


def summary_text(merged, board, average, target, winners, deadline) -> str:
    lines = [
        "DECENTRALIZED BEAUTY CONTEST - RESULTS",
        f"Commit deadline: {deadline.strftime('%Y-%m-%d %H:%M:%S')} Paris time",
        f"Reveals received: {len(merged)}",
        f"Valid reveals: {len(board)}",
    ]
    if average is None:
        lines.append("No valid reveals - no result.")
    else:
        lines += [
            f"Class average: {average:.4f}",
            f"Target (2/3 x average): {target:.4f}",
            f"Winner(s): {', '.join(winners)}",
            f"Winning distance: {board['distance'].min():.4f}",
        ]
    invalid = merged[merged["status"] != "valid"]
    if not invalid.empty:
        lines.append("Invalid reveals:")
        lines += [f"  - {u}: {s}" for u, s in zip(invalid["uni_id"], invalid["status"])]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
st.set_page_config(page_title="Decentralized Beauty Contest", page_icon="🎯", layout="centered")
st.title("🎯 Decentralized Beauty Contest")
st.caption("Keynesian beauty contest · commit-reveal · append-only ledger · consensus")

deadline = get_deadline()
now = datetime.now(PARIS)
commit_open = now <= deadline

if commit_open:
    st.info(f"**Commit phase** is open until **{deadline.strftime('%d %b %Y, %H:%M:%S')}** (Paris time).")
else:
    st.warning(
        f"Commit deadline ({deadline.strftime('%d %b %Y, %H:%M:%S')} Paris time) has passed. "
        "**Reveal phase** is open."
    )
if not API_URL:
    st.caption(
        "⚠️ Local ledger mode: data is stored on the app server and may be lost if the app restarts. "
        "Set `API_URL` in the app secrets to use a persistent Google Sheets ledger."
    )

tab_commit, tab_reveal, tab_ledger, tab_results, tab_about = st.tabs(
    ["1 · Commit", "2 · Reveal", "Ledger", "Results (consensus)", "How it works"]
)

# ---- Commit ---------------------------------------------------------------
with tab_commit:
    st.subheader("Commit phase")
    st.write("Pick a number between 0 and 100. Only its hash is published; your number stays secret until the reveal.")
    if "nonce" not in st.session_state:
        st.session_state["nonce"] = ""
    if st.button("🎲 Generate a random nonce"):
        st.session_state["nonce"] = pysecrets.token_hex(8)

    c_uni = st.text_input("University ID", key="c_uni")
    c_num = st.text_input("Your number (0–100)", key="c_num")
    c_nonce = st.text_input("Secret nonce (save it!)", key="nonce")

    if c_uni and c_num and c_nonce:
        n = normalize_number(c_num)
        if n is None:
            st.error("The number must be between 0 and 100.")
        else:
            st.code(f"commit = sha256({c_uni.strip()}|{n}|{c_nonce})\n       = {make_commit(c_uni.strip(), n, c_nonce)}")
            st.warning("Write down your University ID, number and nonce. Without them your reveal is invalid.")

    if st.button("Submit commit", type="primary", disabled=not commit_open):
        n = normalize_number(c_num)
        if not (c_uni.strip() and c_nonce and n is not None):
            st.error("Please fill in a University ID, a number between 0 and 100, and a nonce.")
        elif datetime.now(PARIS) > deadline:
            st.error("Deadline passed.")
        else:
            try:
                ledger_append(
                    "commits",
                    {"uni_id": c_uni.strip(), "commit": make_commit(c_uni.strip(), n, c_nonce)},
                )
                st.success("Commit appended to the ledger. You can re-submit before the deadline; the latest one counts.")
            except Exception as e:
                st.error(f"Could not reach the ledger: {e}")

# ---- Reveal ---------------------------------------------------------------
with tab_reveal:
    st.subheader("Reveal phase")
    if commit_open:
        st.info("The reveal phase opens after the commit deadline.")
    r_uni = st.text_input("University ID", key="r_uni")
    r_num = st.text_input("Your number (0–100)", key="r_num")
    r_nonce = st.text_input("Your nonce", key="r_nonce")

    if st.button("Submit reveal", type="primary", disabled=commit_open):
        n = normalize_number(r_num)
        if not (r_uni.strip() and r_nonce and n is not None):
            st.error("Please fill in a University ID, a number between 0 and 100, and your nonce.")
        else:
            try:
                ledger_append("reveals", {"uni_id": r_uni.strip(), "number": n, "nonce": r_nonce})
                st.success("Reveal appended to the ledger.")
                commits = ledger_read("commits")
                mine = commits[commits["uni_id"].str.strip() == r_uni.strip()]
                if not mine.empty and mine.iloc[-1]["commit"].strip().lower() == make_commit(r_uni.strip(), n, r_nonce):
                    st.success("✅ Your reveal matches your latest commit.")
                else:
                    st.warning("⚠️ Your reveal does not match your latest commit, so it will be invalid. Check your inputs and resubmit.")
            except Exception as e:
                st.error(f"Could not reach the ledger: {e}")

# ---- Ledger ---------------------------------------------------------------
with tab_ledger:
    st.subheader("Public ledgers")
    st.write("Everyone can read and download both ledgers. Rows can only be appended.")
    if st.button("🔄 Refresh"):
        st.rerun()
    try:
        commits_df, reveals_df = ledger_read("commits"), ledger_read("reveals")
        st.markdown(f"**Commits** ({len(commits_df)} rows)")
        st.dataframe(commits_df, use_container_width=True)
        st.download_button("Download commits.csv", commits_df.to_csv(index=False), "commits.csv", "text/csv")
        st.markdown(f"**Reveals** ({len(reveals_df)} rows)")
        if commit_open:
            st.caption("Reveals are hidden until the commit deadline passes.")
        else:
            st.dataframe(reveals_df, use_container_width=True)
            st.download_button("Download reveals.csv", reveals_df.to_csv(index=False), "reveals.csv", "text/csv")
    except Exception as e:
        st.error(f"Could not read the ledger: {e}")

# ---- Results --------------------------------------------------------------
with tab_results:
    st.subheader("Consensus & results")
    st.write(
        "Rules: for each University ID, take the **latest reveal** and the **latest commit made before the "
        "deadline and before that reveal**. The reveal is valid if its hash matches that commit and the number is in [0, 100]."
    )
    if commit_open:
        st.info("Results are available after the commit deadline.")
    elif st.button("Run consensus", type="primary"):
        try:
            merged, board, avg, target, winners = run_consensus(
                ledger_read("commits"), ledger_read("reveals"), deadline
            )
            if avg is None:
                st.warning("No valid reveals yet.")
            else:
                m1, m2 = st.columns(2)
                m1.metric("Class average", f"{avg:.2f}")
                m2.metric("Target (⅔ × average)", f"{target:.2f}")
                st.success(f"🏆 Winner(s): {', '.join(winners)}")
                st.markdown("**Leaderboard**")
                st.dataframe(board[["rank", "uni_id", "number", "distance"]], use_container_width=True)
                st.download_button("Download leaderboard.csv", board.to_csv(index=False), "leaderboard.csv", "text/csv")
            st.markdown("**Merged reveals (with validation status)**")
            st.dataframe(merged, use_container_width=True)
            st.download_button("Download merged_reveals.csv", merged.to_csv(index=False), "merged_reveals.csv", "text/csv")
            st.download_button(
                "Download summary.txt",
                summary_text(merged, board, avg, target, winners, deadline),
                "summary.txt",
                "text/plain",
            )
        except Exception as e:
            st.error(f"Consensus failed: {e}")

# ---- About ----------------------------------------------------------------
with tab_about:
    st.markdown(
        """
**The game.** Each student picks a number in [0, 100]. The winner is whoever is closest to **⅔ of the class average**.

**Why commit-reveal?** Publishing only `sha256(uni_id|number|nonce)` before the deadline proves you chose your number
in advance without revealing it, so nobody can copy or adapt to others.

| Game step | Blockchain concept |
|---|---|
| Commit (hash) | Transaction broadcast |
| Reveal | Transaction execution / proof |
| CSV ledgers (append-only) | Blockchain |
| Consensus script | Validators agreeing on the same state |

**Verify it yourself.** Download `commits.csv` and `reveals.csv` from the Ledger tab and recompute the hashes: you
should get exactly the same leaderboard, no instructor needed.
"""
    )
