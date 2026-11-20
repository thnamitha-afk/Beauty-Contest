"""
Decentralized Beauty Contest - Project submission
Course : Fintech & DeFi, NEOMA Business School (MiM PGE Grande Ecole)
Author : Namitha Tumakuru Harish

A Keynesian beauty contest ("guess 2/3 of the class average") built so that it
mimics blockchain principles: trustless, transparent, verifiable.

Design choices
  commit-reveal    -> transaction lifecycle (hash first, proof later)
  append-only CSVs -> the ledger / blockchain
  Results tab      -> consensus: a deterministic function of the two ledgers,
                      so anyone who re-runs it gets exactly the same answer

Ledger backends (chosen automatically)
  1. Google Sheets + Apps Script web app when API_URL is set in Streamlit secrets
     (persistent; server-side timestamps).
  2. Local append-only CSV files otherwise (zero setup, but Streamlit Community
     Cloud wipes local files when the app restarts or sleeps).

Optional Streamlit secrets (App settings -> Secrets):
  API_URL  = "https://script.google.com/macros/s/XXXX/exec"
  DEADLINE = "2026-11-30 23:59:59"      # commit deadline, Paris time
"""

import csv
import hashlib
import io
import random
import secrets as pysecrets
import threading
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

AUTHOR = "Namitha Tumakuru Harish"
PROGRAMME = "NEOMA Business School · MiM PGE Grande Ecole · Fintech & DeFi"

PARIS = ZoneInfo("Europe/Paris")
DEFAULT_DEADLINE = "2026-11-15 23:59:59"  # value from the PRD; override via secrets
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
    except Exception:  # no secrets file configured
        return default


def get_deadline() -> datetime:
    raw = str(get_secret("DEADLINE", DEFAULT_DEADLINE))
    return datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=PARIS)


API_URL = get_secret("API_URL")


# --------------------------------------------------------------------------
# Cryptography
# --------------------------------------------------------------------------
def normalize_number(text: str):
    """Canonical string for a number in [0, 100] ('50' and '50.0' hash the same), else None."""
    try:
        d = Decimal(str(text).strip().replace(",", "."))
    except InvalidOperation:
        return None
    if not d.is_finite() or d < 0 or d > 100:
        return None
    return format(d.normalize(), "f")


def make_commit(uni_id: str, number: str, nonce: str) -> str:
    """commit = sha256(uni_id|number|nonce)"""
    return hashlib.sha256(f"{uni_id}|{number}|{nonce}".encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Ledger (append-only)
# --------------------------------------------------------------------------
def ledger_append(kind: str, record: dict):
    if API_URL:
        r = requests.post(API_URL, json={"kind": kind, **record}, timeout=30)
        r.raise_for_status()
        return
    DATA_DIR.mkdir(exist_ok=True)
    path = DATA_DIR / f"{kind}.csv"
    row = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), **record}
    with _LOCK:
        new = not path.exists()
        with open(path, "a", newline="", encoding="utf-8") as f:  # "a": rows are never rewritten
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
# Consensus: a deterministic function of (commits, reveals, deadline)
# --------------------------------------------------------------------------
def run_consensus(commits: pd.DataFrame, reveals: pd.DataFrame, deadline: datetime):
    deadline_utc = deadline.astimezone(timezone.utc)
    c, r = commits.copy(), reveals.copy()
    for df in (c, r):
        df["uni_id"] = df["uni_id"].astype(str).str.strip()
        df["ts"] = pd.to_datetime(df["timestamp_utc"], utc=True, errors="coerce")
        df["row"] = range(len(df))

    rows = []
    for uni_id in sorted(r["uni_id"].unique()):
        # Rule 1: latest reveal per ID
        rv = r[r["uni_id"] == uni_id].sort_values(["ts", "row"]).iloc[-1]
        # Rule 2: latest commit made before the deadline AND before that reveal
        cm = c[(c["uni_id"] == uni_id) & (c["ts"] <= deadline_utc) & (c["ts"] <= rv["ts"])]
        number_norm = normalize_number(rv["number"])
        status, commit = "valid", ""
        if cm.empty:
            status = "no prior commit"
        else:
            commit = cm.sort_values(["ts", "row"]).iloc[-1]["commit"].strip().lower()
            if number_norm is None:
                status = "number out of range / invalid"  # Rule 4: range check
            elif make_commit(uni_id, number_norm, rv["nonce"]) != commit:
                status = "hash mismatch"  # Rule 3: valid hash
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


def fingerprint(df: pd.DataFrame) -> str:
    return hashlib.sha256(df.to_csv(index=False).encode()).hexdigest()


# --------------------------------------------------------------------------
# Demo: simulated class, fully in memory (never touches the real ledger)
# --------------------------------------------------------------------------
def simulate_class(n_students: int, seed: int, deadline: datetime):
    rng = random.Random(seed)
    base = deadline.astimezone(timezone.utc)
    commits, reveals, notes = [], [], []
    t = 0

    def stamp(before_deadline: bool):
        nonlocal t
        t += 1
        delta = timedelta(hours=-48, seconds=t) if before_deadline else timedelta(hours=1, seconds=t)
        return (base + delta).isoformat()

    for i in range(1, n_students + 1):
        uid = f"S{i:03d}"
        number = str(rng.randint(0, 100))
        nonce = pysecrets.token_hex(4)
        # A first commit; some students change their mind (latest commit must count)
        if i % 7 == 0:
            old = str(rng.randint(0, 100))
            commits.append({"timestamp_utc": stamp(True), "uni_id": uid, "commit": make_commit(uid, old, nonce)})
            notes.append(f"{uid}: committed twice, the latest commit counts")
        commits.append({"timestamp_utc": stamp(True), "uni_id": uid, "commit": make_commit(uid, number, nonce)})
        reveal_nonce, reveal_number = nonce, number
        if i == 3:  # forgot the nonce
            reveal_nonce = "forgotten"
            notes.append(f"{uid}: wrong nonce, so the reveal is invalid")
        if i == 5:  # cheater: changes the number after seeing others
            reveal_number = str((int(number) + 20) % 101)
            notes.append(f"{uid}: tried to change the number after committing, so the hash does not match")
        reveals.append({"timestamp_utc": stamp(False), "uni_id": uid, "number": reveal_number, "nonce": reveal_nonce})

    # Student who commits after the deadline: the commit is ignored
    commits.append({"timestamp_utc": stamp(False), "uni_id": "LATE01", "commit": make_commit("LATE01", "10", "x")})
    reveals.append({"timestamp_utc": stamp(False), "uni_id": "LATE01", "number": "10", "nonce": "x"})
    notes.append("LATE01: committed after the deadline, so no valid prior commit")
    # Out-of-range number (hash is correct but the range rule rejects it)
    commits.append({"timestamp_utc": stamp(True), "uni_id": "RANGE01", "commit": make_commit("RANGE01", "150", "y")})
    reveals.append({"timestamp_utc": stamp(False), "uni_id": "RANGE01", "number": "150", "nonce": "y"})
    notes.append("RANGE01: number 150 is outside [0, 100], so it is invalid")

    return pd.DataFrame(commits), pd.DataFrame(reveals), notes


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
st.set_page_config(page_title="Decentralized Beauty Contest", page_icon="🎯", layout="centered")
st.title("🎯 Decentralized Beauty Contest")
st.caption(f"Project submission by **{AUTHOR}** · {PROGRAMME}")

deadline = get_deadline()
commit_open = datetime.now(PARIS) <= deadline

if commit_open:
    st.info(f"**Commit phase** is open until **{deadline.strftime('%d %b %Y, %H:%M:%S')}** (Paris time).")
else:
    st.warning(
        f"Commit deadline ({deadline.strftime('%d %b %Y, %H:%M:%S')} Paris time) has passed: "
        "**reveal phase** is open."
    )
if not API_URL:
    st.caption(
        "⚠️ Local ledger mode: data lives on the app server and may be lost if the app restarts. "
        "Add `API_URL` in the app secrets to use a persistent Google Sheets ledger."
    )

tab_commit, tab_reveal, tab_ledger, tab_results, tab_demo, tab_about = st.tabs(
    ["1 · Commit", "2 · Reveal", "Ledger", "Results", "Demo", "About"]
)

# ---- Commit ---------------------------------------------------------------
with tab_commit:
    st.subheader("Commit phase")
    st.write("Choose a number between 0 and 100. Only its hash is published; the number stays secret until the reveal.")
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
            st.code(f"sha256({c_uni.strip()}|{n}|{c_nonce})\n= {make_commit(c_uni.strip(), n, c_nonce)}")
            st.warning("Save your University ID, number and nonce. Without them your reveal is invalid.")

    if st.button("Submit commit", type="primary", disabled=not commit_open):
        n = normalize_number(c_num)
        if not (c_uni.strip() and c_nonce and n is not None):
            st.error("Fill in a University ID, a number between 0 and 100, and a nonce.")
        elif datetime.now(PARIS) > deadline:
            st.error("The deadline has passed.")
        else:
            try:
                ledger_append("commits", {"uni_id": c_uni.strip(), "commit": make_commit(c_uni.strip(), n, c_nonce)})
                st.success("Commit appended to the ledger. You may resubmit before the deadline; the latest one counts.")
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
            st.error("Fill in a University ID, a number between 0 and 100, and your nonce.")
        else:
            try:
                ledger_append("reveals", {"uni_id": r_uni.strip(), "number": n, "nonce": r_nonce})
                st.success("Reveal appended to the ledger.")
                commits = ledger_read("commits")
                mine = commits[commits["uni_id"].str.strip() == r_uni.strip()]
                if not mine.empty and mine.iloc[-1]["commit"].strip().lower() == make_commit(r_uni.strip(), n, r_nonce):
                    st.success("✅ Your reveal matches your latest commit.")
                else:
                    st.warning("⚠️ Your reveal does not match your latest commit and will be invalid. Check your inputs and resubmit.")
            except Exception as e:
                st.error(f"Could not reach the ledger: {e}")

# ---- Ledger ---------------------------------------------------------------
with tab_ledger:
    st.subheader("Public ledgers")
    st.write("Anyone can read and download both ledgers. Rows can only be appended.")
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


def show_results(commits_df, reveals_df, key_prefix):
    merged, board, avg, target, winners = run_consensus(commits_df, reveals_df, deadline)
    if avg is None:
        st.warning("No valid reveals yet.")
    else:
        m1, m2 = st.columns(2)
        m1.metric("Class average", f"{avg:.2f}")
        m2.metric("Target (⅔ × average)", f"{target:.2f}")
        st.success(f"🏆 Winner(s): {', '.join(winners)}")
        st.markdown("**Leaderboard**")
        st.dataframe(board[["rank", "uni_id", "number", "distance"]], use_container_width=True)
        st.download_button("Download leaderboard.csv", board.to_csv(index=False), "leaderboard.csv", "text/csv", key=f"{key_prefix}_lb")
    st.markdown("**Merged reveals (with validation status)**")
    st.dataframe(merged, use_container_width=True)
    st.download_button("Download merged_reveals.csv", merged.to_csv(index=False), "merged_reveals.csv", "text/csv", key=f"{key_prefix}_mg")
    st.download_button(
        "Download summary.txt",
        summary_text(merged, board, avg, target, winners, deadline),
        "summary.txt",
        "text/plain",
        key=f"{key_prefix}_sm",
    )
    return board


# ---- Results --------------------------------------------------------------
with tab_results:
    st.subheader("Consensus & results")
    st.write(
        "For each University ID the consensus takes the **latest reveal** and the **latest commit made before the "
        "deadline and before that reveal**. A reveal is valid if its hash matches that commit and the number is in [0, 100]. "
        "The winner is the valid number closest to ⅔ of the average of all valid numbers."
    )
    if commit_open:
        st.info("Results are available after the commit deadline.")
    elif st.button("Run consensus", type="primary"):
        try:
            show_results(ledger_read("commits"), ledger_read("reveals"), "live")
        except Exception as e:
            st.error(f"Consensus failed: {e}")

# ---- Demo -----------------------------------------------------------------
with tab_demo:
    st.subheader("Demo: simulated class")
    st.write(
        "Generates a fake class **in memory** (the real ledger is not touched) including edge cases "
        "(wrong nonce, cheating attempt, late commit, out-of-range number, multiple commits), then runs the consensus. "
        "It also re-runs the consensus on shuffled copies of the ledgers to show the result is deterministic."
    )
    d1, d2 = st.columns(2)
    n_students = d1.slider("Number of students", 10, 60, 25)
    seed = d2.number_input("Random seed", 0, 9999, 42)
    if st.button("Run simulation", type="primary"):
        sim_c, sim_r, notes = simulate_class(n_students, int(seed), deadline)
        board = show_results(sim_c, sim_r, "demo")
        st.markdown("**Edge cases injected**")
        for note in notes:
            st.write(f"- {note}")
        # Determinism check: two independent "validators" with rows in a different order
        b1 = run_consensus(sim_c, sim_r, deadline)[1]
        b2 = run_consensus(sim_c.sample(frac=1, random_state=1), sim_r.sample(frac=1, random_state=2), deadline)[1]
        same = fingerprint(b1) == fingerprint(b2)
        st.markdown("**Consensus check**")
        st.code(f"validator A: {fingerprint(b1)}\nvalidator B: {fingerprint(b2)}")
        (st.success if same else st.error)(
            "Both validators computed an identical leaderboard." if same else "Leaderboards differ!"
        )

# ---- About ----------------------------------------------------------------
with tab_about:
    st.markdown(
        f"""
### Project
Submission by **{AUTHOR}** for the Fintech & DeFi course at NEOMA Business School.
The goal is to play the **Keynesian beauty contest** (guess ⅔ of the class average) in a way that reproduces
blockchain principles without a trusted teacher.

### How the game maps to blockchain
| Game step | Blockchain concept |
|---|---|
| Commit = sha256(uni_id\\|number\\|nonce) | Transaction broadcast (content hidden) |
| Reveal (uni_id, number, nonce) | Transaction execution / proof |
| Append-only commits & reveals CSVs | Blockchain / public ledger |
| Results tab (deterministic merge) | Consensus: every validator reaches the same state |

### Why a nonce?
Numbers are only 0–100, so anyone could hash all 101 values and read your commit. The secret nonce makes the
hash impossible to brute-force before the reveal.

### Trust model and limitations
- **Trustless verification:** both ledgers are downloadable and the consensus is a pure function of them, so any
  student can recompute the result (see the Demo tab for the determinism check).
- **Append-only** is enforced by the app and the Apps Script (no edit or delete endpoint), not cryptographically.
  A real chain would also link each row to the hash of the previous one; this is the natural next improvement.
- **Timestamps** are set server-side when the Google Sheets ledger is used, so students cannot fake a commit time.
- University IDs are public, and the app does not authenticate users: someone could commit under another ID.
  The latest-commit rule limits the damage, but real identity would need signatures (public-key cryptography).
- Spam is not prevented, which is acceptable in a classroom.
"""
    )
