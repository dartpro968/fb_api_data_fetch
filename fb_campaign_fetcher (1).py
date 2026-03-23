import os
import streamlit as st
import requests
import pandas as pd
import io

st.set_page_config(page_title="FB Campaign Inspector", page_icon="📊", layout="wide")

# Try loading token from token.txt
default_token = ""
token_path = os.path.join(os.path.dirname(__file__), "token.txt")
if os.path.exists(token_path):
    try:
        with open(token_path, "r") as f:
            default_token = f.read().strip()
    except:
        pass

st.markdown("""
<style>
.info-note {
    font-size: 12px; color: #6b7280;
    background: #111827; border: 1px dashed #374151;
    border-radius: 6px; padding: 8px 14px; margin-top: 6px;
}
</style>
""", unsafe_allow_html=True)


# ── API ────────────────────────────────────────────────────────────────────────

def api_get(url, params):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                err = data["error"]
                # If it's a rate limit error, maybe wait? For now just raise.
                raise Exception(f"FB API {err.get('code','')}: {err.get('message','Unknown error')}")
            return data
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1: continue
            raise Exception("Request timed out after multiple attempts.")
        except requests.exceptions.ConnectionError:
            if attempt < max_retries - 1: continue
            raise Exception("Connection error. Please check your internet.")
        except requests.exceptions.RequestException as e:
            raise Exception(f"Network error: {e}")
    return None


def get_campaign_info(campaign_id, token):
    return api_get(
        f"https://graph.facebook.com/v19.0/{campaign_id}",
        {"fields": "id,name,status,objective,daily_budget,lifetime_budget,budget_remaining", "access_token": token}
    )


def get_adsets(campaign_id, token):
    url = f"https://graph.facebook.com/v19.0/{campaign_id}/adsets"
    params = {
        "fields": (
            "id,name,status,targeting,promoted_object,daily_budget,lifetime_budget,budget_remaining,"
            "creative{object_story_spec,link_url},"
            "ads{creative{object_story_spec,link_url,asset_feed_spec}}"
        ),
        "access_token": token,
        "limit": 100,
    }
    results = []
    while url:
        data = api_get(url, params)
        if data is None:
            break
        results.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
        params = {}
    return results


# ── Parsers ────────────────────────────────────────────────────────────────────

def parse_audiences(targeting):
    inc = [a.get("name", a.get("id", "")) for a in targeting.get("custom_audiences", [])]
    exc = [a.get("name", a.get("id", "")) for a in targeting.get("excluded_custom_audiences", [])]
    return " | ".join(filter(None, inc)), " | ".join(filter(None, exc))


def extract_urls_from_story_spec(spec):
    """Dig into object_story_spec for link URLs."""
    urls = set()
    if not isinstance(spec, dict):
        return urls
    for key in ["link_data", "video_data", "photo_data"]:
        block = spec.get(key, {})
        if isinstance(block, dict):
            for field in ["link", "url", "call_to_action"]:
                val = block.get(field)
                if isinstance(val, str) and val.startswith("http"):
                    urls.add(val)
                elif isinstance(val, dict):
                    inner = val.get("value", {})
                    if isinstance(inner, dict):
                        link = inner.get("link") or inner.get("url")
                        if link and link.startswith("http"):
                            urls.add(link)
    return urls


def parse_urls(adset):
    """
    Try every known location FB API might return a destination URL:
    1. promoted_object.url
    2. promoted_object.pixel_rule
    3. creative.object_story_spec (link_data / video_data)
    4. creative.link_url
    5. ads[].creative.object_story_spec
    6. ads[].creative.asset_feed_spec link_urls
    """
    urls = set()

    # 1. promoted_object
    po = adset.get("promoted_object") or {}
    if po.get("url"):
        urls.add(po["url"])
    rule = po.get("pixel_rule") or {}
    if isinstance(rule, dict):
        for v in rule.get("url", {}).values():
            if isinstance(v, list):
                urls.update(v)
            elif isinstance(v, str) and v.startswith("http"):
                urls.add(v)

    # 2. adset-level creative
    creative = adset.get("creative") or {}
    if creative.get("link_url"):
        urls.add(creative["link_url"])
    urls |= extract_urls_from_story_spec(creative.get("object_story_spec") or {})

    # 3. ads-level creatives
    ads_data = (adset.get("ads") or {}).get("data") or []
    for ad in ads_data:
        ad_creative = ad.get("creative") or {}
        if ad_creative.get("link_url"):
            urls.add(ad_creative["link_url"])
        urls |= extract_urls_from_story_spec(ad_creative.get("object_story_spec") or {})
        # asset_feed_spec
        afs = ad_creative.get("asset_feed_spec") or {}
        for link_obj in afs.get("link_urls", []):
            if isinstance(link_obj, dict):
                for field in ["website_url", "display_url"]:
                    v = link_obj.get(field)
                    if v and v.startswith("http"):
                        urls.add(v)

    return " | ".join(sorted(urls)) if urls else ""


# ── Excel Reader ───────────────────────────────────────────────────────────────

def read_excel(file):
    try:
        df = pd.read_excel(file, dtype=str, header=0)
    except Exception as e:
        raise Exception(f"Could not read file: {e}")
    df.columns = [str(c).strip() for c in df.columns]
    if df.shape[1] < 2:
        raise Exception("Need at least 2 columns: Account Name, Campaign ID.")
    cols = list(df.columns)
    df = df.rename(columns={cols[0]: "Account Name", cols[1]: "Campaign ID"})
    df = df[["Account Name", "Campaign ID"]].dropna(subset=["Campaign ID"]).copy()
    df["Campaign ID"] = df["Campaign ID"].str.strip()
    df["Account Name"] = df["Account Name"].fillna("Unknown").str.strip()
    df = df[df["Campaign ID"] != ""].reset_index(drop=True)
    if df.empty:
        raise Exception("No valid rows found.")
    return df


# ── UI ─────────────────────────────────────────────────────────────────────────

st.title("📊 FB Campaign Inspector")
st.caption("Fetches custom audiences & destination URLs across all campaigns in bulk.")
st.divider()

col_a, col_b = st.columns(2, gap="large")

with col_a:
    st.subheader("🔐 Access Token")
    access_token = st.text_input(
        "Facebook Ads Access Token",
        type="password",
        value=default_token,
        placeholder="EAAxxxxxxxxxxxxxxx...",
        help="Requires ads_read + ads_management permission.",
    )

with col_b:
    st.subheader("📂 Campaign List (Excel)")
    uploaded_file = st.file_uploader(
        "Upload Excel", type=["xlsx", "xls"], label_visibility="collapsed"
    )
st.divider()

col_options = st.multiselect(
    "🎯 Data Fields to Fetch",
    options=["Audiences", "Website URLs", "Budgets"],
    default=["Audiences", "Website URLs", "Budgets"],
    help="Select the specific data points you want to extract. Deselecting complex fields like URLs can improve speed."
)

st.divider()

# --- Session State for Results ---
if "all_rows" not in st.session_state:
    st.session_state.all_rows = []

def get_excel_io(data_df, opts):
    out = io.BytesIO()
    with pd.ExcelWriter(out, engine="openpyxl") as writer:
        data_df.to_excel(writer, sheet_name="All Data", index=False)
        if "Audiences" in opts:
            cols = ["Account Name", "Campaign Name", "Ad Set Name", "Inclusions (C1)", "Exclusions (C2)"]
            data_df[cols].to_excel(writer, sheet_name="Audiences", index=False)
        if "Website URLs" in opts:
            cols = ["Account Name", "Campaign Name", "Ad Set Name", "Website URL(s)"]
            data_df[cols].to_excel(writer, sheet_name="Website URLs", index=False)
        if "Budgets" in opts:
            cols = ["Account Name", "Campaign Name", "Campaign Budget", "Ad Set Name", "Ad Set Budget", "Ad Set Status"]
            data_df[cols].to_excel(writer, sheet_name="Budgets", index=False)
    out.seek(0)
    return out

# --- Intermediate Download Button ---
if st.session_state.all_rows:
    p_df = pd.DataFrame(st.session_state.all_rows)
    st.download_button(
        "📥 Download Current Progress (Excel)",
        data=get_excel_io(p_df, col_options),
        file_name="fb_campaign_progress.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True
    )
    if st.button("🗑️ Clear Current Data (Reset)"):
        st.session_state.all_rows = []
        st.rerun()

st.divider()

campaigns_df = None
if uploaded_file:
    try:
        campaigns_df = read_excel(uploaded_file)
        st.success(f"✅ {len(campaigns_df)} campaign(s) loaded")
        st.dataframe(campaigns_df, use_container_width=True, hide_index=True)
    except Exception as e:
        st.error(f"File error: {e}")

fetch_btn = st.button("🚀 Fetch All Campaigns", type="primary")

# ── Fetch: collect EVERYTHING first, then display ──────────────────────────────

if fetch_btn:
    if not (access_token and access_token.strip()):
        st.error("Access token is required.")
        st.stop()
    if campaigns_df is None:
        st.error("Upload a campaign Excel file first.")
        st.stop()

    token = access_token.strip()
    total = len(campaigns_df)
    errors = []

    st.divider()
    progress_bar = st.progress(0)
    status_text = st.empty()

    for i, row in campaigns_df.iterrows():
        account = row["Account Name"]
        cid = row["Campaign ID"]
        pct = (i + 1) / total
        status_text.markdown(f"⏳ **Fetching {i+1} / {total}** — `{account}` (Campaign `{cid}`)")
        progress_bar.progress(pct)

        try:
            camp = get_campaign_info(cid, token)
            if not camp:
                raise Exception("Campaign data not found or API error.")
                
            adsets = get_adsets(cid, token)
            
            # Extract budgets (handle both daily and lifetime)
            camp_budget_str = "Not Fetched"
            if "Budgets" in col_options:
                camp_budget = camp.get("daily_budget") or camp.get("lifetime_budget") or camp.get("budget_remaining") or "0"
                if camp.get("daily_budget"):
                    camp_budget_str = f"Daily: {int(camp['daily_budget'])/100:.2f}"
                elif camp.get("lifetime_budget"):
                    camp_budget_str = f"Lifetime: {int(camp['lifetime_budget'])/100:.2f}"
                else:
                    camp_budget_str = "Not Set"

            if not adsets:
                row_data = {
                    "Account Name": account,
                    "Campaign ID": cid,
                    "Campaign Name": camp.get("name", ""),
                    "Campaign Status": camp.get("status", ""),
                    "Ad Set Name": "—",
                    "Ad Set ID": "—",
                    "Ad Set Status": "—",
                }
                if "Budgets" in col_options:
                    row_data["Campaign Budget"] = camp_budget_str
                    row_data["Ad Set Budget"] = "—"
                if "Audiences" in col_options:
                    row_data["Inclusions (C1)"] = "None"
                    row_data["Exclusions (C2)"] = "None"
                if "Website URLs" in col_options:
                    row_data["Website URL(s)"] = "N/A"
                
                st.session_state.all_rows.append(row_data)
                continue

            for adset in adsets:
                row_data = {
                    "Account Name": account,
                    "Campaign ID": cid,
                    "Campaign Name": camp.get("name", ""),
                    "Campaign Status": camp.get("status", ""),
                    "Ad Set Name": adset.get("name", ""),
                    "Ad Set ID": adset.get("id", ""),
                    "Ad Set Status": adset.get("status", ""),
                }
                
                if "Audiences" in col_options:
                    targeting = adset.get("targeting") or {}
                    inc, exc = parse_audiences(targeting)
                    row_data["Inclusions (C1)"] = inc if inc else "None"
                    row_data["Exclusions (C2)"] = exc if exc else "None"
                
                if "Website URLs" in col_options:
                    url_str = parse_urls(adset)
                    row_data["Website URL(s)"] = url_str if url_str else "N/A"
                
                if "Budgets" in col_options:
                    row_data["Campaign Budget"] = camp_budget_str
                    # Ad set budget
                    if adset.get("daily_budget"):
                        as_budget_str = f"Daily: {int(adset['daily_budget'])/100:.2f}"
                    elif adset.get("lifetime_budget"):
                        as_budget_str = f"Lifetime: {int(adset['lifetime_budget'])/100:.2f}"
                    else:
                        as_budget_str = "Inherited/Not Set"
                    row_data["Ad Set Budget"] = as_budget_str

                st.session_state.all_rows.append(row_data)

        except Exception as e:
            errors.append(f"Campaign `{cid}` ({account}): {e}")

        # --- AUTO-SAVE AFTER 20 CAMPAIGNS ---
        if (i + 1) % 20 == 0 or (i + 1) == total:
            if st.session_state.all_rows:
                try:
                    temp_df = pd.DataFrame(st.session_state.all_rows)
                    # Save both CSV and Excel for maximum safety/usability
                    temp_df.to_csv("fb_fetch_autosave.csv", index=False)
                    with pd.ExcelWriter("fb_fetch_autosave.xlsx", engine="openpyxl") as writer:
                        temp_df.to_excel(writer, index=False)
                except:
                    pass

    progress_bar.progress(1.0)
    status_text.markdown("✅ **Done fetching all campaigns.**")
    st.info("💡 Results auto-saved to `fb_fetch_autosave.xlsx` in the application folder.")

    # ── Display errors ─────────────────────────────────────────────────────────
    if errors:
        with st.expander(f"⚠️ {len(errors)} error(s) — click to expand"):
            for err in errors:
                st.error(err)

    # ── Show combined table ────────────────────────────────────────────────────
    if st.session_state.all_rows:
        result_df = pd.DataFrame(st.session_state.all_rows)

        st.subheader(f"📋 Combined Results — {len(result_df)} Ad Set(s) across {total} Campaign(s)")

        tab_list = []
        if "Audiences" in col_options: tab_list.append("🎯 Audiences")
        if "Website URLs" in col_options: tab_list.append("🌐 Website URLs")
        if "Budgets" in col_options: tab_list.append("💰 Budget Analysis")
        tab_list.append("📄 All Columns")
        
        tabs = st.tabs(tab_list)
        tab_idx = 0

        if "Audiences" in col_options:
            with tabs[tab_idx]:
                aud_cols = ["Account Name", "Campaign Name", "Ad Set Name", "Inclusions (C1)", "Exclusions (C2)"]
                st.dataframe(result_df[aud_cols], use_container_width=True, hide_index=True)
            tab_idx += 1

        if "Website URLs" in col_options:
            with tabs[tab_idx]:
                url_cols = ["Account Name", "Campaign Name", "Ad Set Name", "Website URL(s)"]
                st.dataframe(result_df[url_cols], use_container_width=True, hide_index=True)
            tab_idx += 1
            
        if "Budgets" in col_options:
            with tabs[tab_idx]:
                bud_cols = ["Account Name", "Campaign Name", "Campaign Budget", "Ad Set Name", "Ad Set Budget", "Ad Set Status"]
                st.dataframe(result_df[bud_cols], use_container_width=True, hide_index=True)
            tab_idx += 1

        with tabs[tab_idx]:
            st.dataframe(result_df, use_container_width=True, hide_index=True)

        # ── Export ─────────────────────────────────────────────────────────────
        st.divider()
        st.download_button(
            label="⬇️ Final Download — Full Results (Excel)",
            data=get_excel_io(result_df, col_options),
            file_name="fb_campaign_results.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary"
        )
    else:
        st.warning("No data retrieved. Verify your access token and campaign IDs.")

st.divider()
st.caption("Facebook Ads API v19.0 · Requires ads_read permission")