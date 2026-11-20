"""Decentralized Beauty Contest - Streamlit app (NEOMA Fintech & DeFi).
Run:  streamlit run app.py
Optional: put  API_URL = "https://script.google.com/macros/s/.../exec"  in .streamlit/secrets.toml
"""
import secrets as pysecrets
from datetime import datetime

import pandas as pd
import streamlit as st

import core
from core import DEADLINE, PARIS, ApiLedger, LocalLedger, commit_hash

st.set_page_config(page_title="Decentralized Beauty Contest", page_icon="🎯", layout="wide")

# ---------------- sidebar: backend + phase ----------------
default_url = st.secrets.get("API_URL", "") if hasattr(st, "secrets") else ""
with st.sidebar:
    st.header("⚙️ Ledger backend")
    mode = st.radio("Backend", ["Google Sheets API", "Local demo ledger"],
                    index=0 if default_url else 1,
                    help="Local demo stores append-only CSVs next to the app (single machine only).")
    if mode == "Google Sheets API":
        url = st.text_input("Apps Script web app URL", value=default_url, placeholder="https://script.google.com/macros/s/.../exec")
        ledger = ApiLedger(url) if url else None
        if not url:
            st.warning("Paste the API URL from info.txt.")
    else:
        ledger = LocalLedger()

    st.header("⏱️ Phase")
    now = datetime.now(PARIS)
    simulate = st.selectbox("Phase override (demo only)", ["Automatic (by deadline)", "Force commit", "Force reveal"])
    if simulate == "Force commit":
        phase = "commit"
    elif simulate == "Force reveal":
        phase = "reveal"
    else:
        phase = "commit" if now <= DEADLINE else "reveal"
    st.metric("Current phase", phase.upper())
    st.caption(f"Deadline: {DEADLINE:%d %b %Y, %H:%M:%S} Paris time")
    if now <= DEADLINE:
        st.caption(f"Time left: {str(DEADLINE - now).split('.')[0]}")
    if simulate != "Automatic (by deadline)":
        st.info("Override changes UI gating only. Consensus always uses the real deadline timestamps.")

st.title("🎯 Decentralized Beauty Contest")
st.caption("Keynesian beauty contest with commit–reveal, an append-only ledger and deterministic consensus. "
           "Pick a number 0–100; closest to ⅔ of the class average wins.")

tab_commit, tab_reveal, tab_ledger, tab_result, tab_learn = st.tabs(
    ["1️⃣ Commit", "2️⃣ Reveal", "📒 Public ledgers", "🏆 Consensus & results", "📚 How it works"])


def need_ledger():
    if ledger is None:
        st.error("No ledger configured (see sidebar).")
        return False
    return True


# ---------------- commit ----------------
with tab_commit:
    st.subheader("Commit phase (before the deadline)")
    if phase != "commit":
        st.error("The commit deadline has passed. New commits are not accepted.")
    c1, c2 = st.columns(2)
    with c1:
        uid = st.text_input("University ID", key="c_uid")
        num = st.number_input("Your number (0–100)", min_value=0.0, max_value=100.0, value=33.0, step=1.0, key="c_num")
        if "c_nonce" not in st.session_state:
            st.session_state.c_nonce = ""
        nonce = st.text_input("Secret nonce", key="c_nonce", help="Any secret string. You need it again to reveal!")
        st.button("🎲 Generate random nonce", on_click=lambda: st.session_state.update(c_nonce=pysecrets.token_hex(12)))
    with c2:
        st.markdown("**Preimage & commit (computed locally)**")
        if uid and nonce:
            h = commit_hash(uid, num, nonce)
            st.code(f"{uid.strip()}|{core.normalize_number(num)}|{nonce}", language=None)
            st.code(h, language=None)
        else:
            h = None
            st.info("Enter ID and nonce to see your hash.")
        st.warning("⚠️ Save your ID, number and nonce. Lose the nonce → your reveal is invalid.")
    if st.button("Submit commit", type="primary", disabled=(phase != "commit" or not h)):
        if need_ledger():
            try:
                ledger.append("commits", {"uni_id": uid.strip(), "commit": h})
                st.success("Commit appended to the ledger. Only the hash was sent – your number stays secret.")
                st.download_button("⬇️ Download my preimage (keep it safe)",
                                   f"uni_id={uid.strip()}\nnumber={core.normalize_number(num)}\nnonce={nonce}\ncommit={h}\n",
                                   file_name=f"my_commit_{uid.strip()}.txt")
            except Exception as e:
                st.error(f"Submission failed: {e}")
    st.caption("Multiple attempts are allowed – the ledger is append-only and the latest commit counts.")

# ---------------- reveal ----------------
with tab_reveal:
    st.subheader("Reveal phase (after the deadline)")
    if phase != "reveal":
        st.info("Reveal opens after the commit deadline.")
    r1, r2, r3 = st.columns(3)
    ruid = r1.text_input("University ID", key="r_uid")
    rnum = r2.number_input("Your number (0–100)", min_value=0.0, max_value=100.0, value=33.0, step=1.0, key="r_num")
    rnonce = r3.text_input("Your nonce", key="r_nonce")

    local_ok = None
    if ruid and rnonce and ledger is not None and phase == "reveal":
        try:
            cm = ledger.read("commits")
            cm = cm[cm["uni_id"].str.strip() == ruid.strip()]
            if cm.empty:
                st.warning("No commit found for this ID on the ledger.")
            else:
                local_ok = cm.iloc[-1]["commit"].strip().lower() == commit_hash(ruid, rnum, rnonce)
                (st.success if local_ok else st.error)(
                    "✅ Matches your latest commit." if local_ok else
                    "❌ Does not match your latest commit – check number/nonce. (You can still submit, but it will be invalid.)")
        except Exception as e:
            st.warning(f"Could not pre-check against the ledger: {e}")

    if st.button("Submit reveal", type="primary", disabled=(phase != "reveal" or not (ruid and rnonce))):
        if need_ledger():
            try:
                ledger.append("reveals", {"uni_id": ruid.strip(), "number": core.normalize_number(rnum), "nonce": rnonce})
                st.success("Reveal appended to the ledger.")
            except Exception as e:
                st.error(f"Submission failed: {e}")

# ---------------- ledgers ----------------
with tab_ledger:
    st.subheader("Public, verifiable ledgers")
    if st.button("🔄 Refresh") or True:
        if ledger is None:
            st.info("Configure a backend in the sidebar.")
        else:
            try:
                cm, rv = ledger.read("commits"), ledger.read("reveals")
                a, b = st.columns(2)
                a.markdown(f"**commits** ({len(cm)} rows)")
                a.dataframe(cm, use_container_width=True, hide_index=True)
                a.download_button("⬇️ commits.csv", cm.to_csv(index=False), "commits.csv")
                b.markdown(f"**reveals** ({len(rv)} rows)")
                b.dataframe(rv, use_container_width=True, hide_index=True)
                b.download_button("⬇️ reveals.csv", rv.to_csv(index=False), "reveals.csv")
            except Exception as e:
                st.error(f"Could not read ledgers: {e}")

# ---------------- consensus ----------------
with tab_result:
    st.subheader("Consensus: anyone can run this and get identical results")
    st.markdown("Rules: latest reveal per ID (after the deadline) + latest **prior** commit (before the deadline), "
                "hash must match, number in [0, 100].")
    if st.button("Run consensus", type="primary") and need_ledger():
        try:
            merged, lb, s = core.run_consensus(ledger.read("commits"), ledger.read("reveals"))
            if s["average"] is None:
                st.warning("No valid reveals yet.")
            else:
                m1, m2, m3, m4 = st.columns(4)
                m1.metric("Valid entries", s["n_valid"])
                m2.metric("Class average", f"{s['average']:.2f}")
                m3.metric("Target (⅔ × avg)", f"{s['target']:.2f}")
                m4.metric("Winner(s)", ", ".join(s["winners"]))
                st.markdown("**Leaderboard**")
                st.dataframe(lb, use_container_width=True, hide_index=True)
                st.bar_chart(lb.set_index("uni_id")["number"])
            st.markdown("**Merged commits + reveals (with validation status)**")
            st.dataframe(merged.drop(columns=["nonce"]), use_container_width=True, hide_index=True)
            d1, d2, d3 = st.columns(3)
            d1.download_button("⬇️ merged.csv", merged.to_csv(index=False), "merged.csv")
            d2.download_button("⬇️ leaderboard.csv", lb.to_csv(index=False), "leaderboard.csv")
            d3.download_button("⬇️ summary.txt", core.summary_text(s), "summary.txt")
        except Exception as e:
            st.error(f"Consensus failed: {e}")

# ---------------- learn ----------------
with tab_learn:
    st.subheader("Blockchain analogy")
    st.table(pd.DataFrame({
        "Game element": ["Commit (hash)", "Reveal (preimage)", "Append-only ledger", "Merge script", "Public CSV endpoints"],
        "Blockchain concept": ["Transaction broadcast / hash commitment", "Transaction opening / proof",
                               "Blockchain", "Consensus rules", "Transparency / auditability"]}))
    st.markdown("""
- **Trustless:** nobody needs to trust the instructor – hashes bind you to your number before anyone sees it.
- **Verifiable:** `sha256(uni_id|number|nonce)` can be recomputed by anyone.
- **Append-only:** the backend exposes no update/delete; the latest valid entry counts.
- **Deterministic consensus:** same inputs → same winner on every machine.
""")
