import json, subprocess, sys, time
from pathlib import Path
import pandas as pd
import requests
import streamlit as st
from app.config import DATA_DIR

st.set_page_config(page_title="YottaFlex Replenishment",page_icon="📦",layout="wide")
st.title("📦 Demand Forecasting & Replenishment")
st.caption("SKU-location forecasting, supplier constraints, exception handling, and traceable decisions.")
API=st.sidebar.text_input("API base URL","http://127.0.0.1:8000")
st.sidebar.markdown("### Workflow")
st.sidebar.write("1. Generate data")
st.sidebar.write("2. Forecast demand")
st.sidebar.write("3. Recommend replenishment")
st.sidebar.write("4. Validate outputs")

def api_post(path,params=None,json_body=None):
    try:
        r=requests.post(f"{API}{path}",params=params,json=json_body,timeout=600)
        r.raise_for_status()
        return r.json()
    except requests.HTTPError as e:
        # Show FastAPI's useful validation/exception detail rather than only
        # the generic "500 Server Error" message.
        response = getattr(e, "response", None)
        detail = None
        if response is not None:
            try:
                payload = response.json()
                detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
            except Exception:
                detail = response.text[:2000]
        st.error(f"API request failed ({response.status_code if response is not None else 'HTTP error'}): {detail or e}")
        return None
    except Exception as e:
        st.error(f"API request failed: {e}")
        return None

with st.expander("1. Generate synthetic data",expanded=not (DATA_DIR/"manifest.json").exists()):
    c1,c2,c3,c4=st.columns(4)
    n_skus=c1.number_input("SKUs",min_value=1,max_value=2000,value=50)
    n_stores=c2.number_input("Stores",min_value=1,max_value=100,value=5)
    n_days=c3.number_input("Days",min_value=1,max_value=365,value=90)
    seed=c4.number_input("Seed",value=20261022)
    full_scale = st.checkbox("Use required full scale (2,000 SKUs × 100 stores × 365 days)", value=False)
    if full_scale:
        st.warning("Full scale creates 73 million SKU-store-day records in both sales and inventory exports. It requires substantial disk space and can take a long time. Use a small run first.")
    elif int(n_skus) * int(n_stores) * int(n_days) > 100_000:
        st.warning("This configuration creates more than 100,000 SKU-store-day records. Generation and validation will take longer.")
    if st.button("Generate data",type="primary"):
        request_body = {"seed":int(seed), "full":bool(full_scale)}
        if not full_scale:
            request_body.update({"n_skus":int(n_skus),"n_stores":int(n_stores),"n_days":int(n_days)})
        try:
            started = requests.post(f"{API}/api/generate-job", json=request_body, timeout=15)
            if started.status_code >= 400:
                try:
                    detail = started.json().get("detail", started.text)
                except Exception:
                    detail = started.text
                st.error(f"Could not start generation ({started.status_code}): {detail}")
            else:
                job_id = started.json()["job_id"]
                status_box = st.status("Generation started. You can keep this page open while the dataset is generated...", expanded=True)
                last_status = None
                while True:
                    time.sleep(3)
                    status_response = requests.get(f"{API}/api/generate-job/{job_id}", timeout=15)
                    status_response.raise_for_status()
                    job = status_response.json()
                    if job["status"] != last_status:
                        status_box.write(f"Generation status: **{job['status']}**")
                        last_status = job["status"]
                    if job["status"] == "completed":
                        status_box.update(label="Data generation completed", state="complete", expanded=False)
                        manifest = job["manifest"]
                        scale = manifest["scale"]
                        st.success(f"Generated {manifest['sales_rows']:,} sales rows and {manifest['inventory_rows']:,} inventory rows for {scale['n_skus']} SKUs × {scale['n_stores']} stores × {scale['n_days']} days.")
                        break
                    if job["status"] == "failed":
                        status_box.update(label="Data generation failed", state="error", expanded=True)
                        st.error(job.get("error", "Unknown generation error"))
                        break
        except Exception as e:
            st.error(f"Generation job error: {e}")

c1,c2,c3=st.columns(3)
if c1.button("2. Run forecast"):
    try:
        current_manifest = json.loads((DATA_DIR/"manifest.json").read_text(encoding="utf-8"))
        configured_days = int(current_manifest.get("scale", {}).get("n_days", 90))
    except Exception:
        configured_days = 90
    effective_holdout = min(14, max(1, configured_days - 1))
    with st.spinner("Comparing temporal holdout models and forecasting..."):
        result=api_post("/api/forecast",params={"holdout_days":effective_holdout,"horizon_days":7})
    if result: st.success(f"Forecast complete. Selected model: {result['model']}.")
if c2.button("3. Run replenishment"):
    with st.spinner("Applying reorder and supplier constraints..."):
        result=api_post("/api/replenishment")
    if result: st.success("Recommendations generated.")
if c3.button("4. Validate all"):
    result=api_post("/api/validate")
    if result: st.success(f"Validation status: {result['status']}")

summary={}
try:
    summary=requests.get(f"{API}/api/summary",timeout=10).json()
except Exception:
    st.info("Start the API in a terminal with `uvicorn app.main:app --reload` to enable the dashboard.")
    st.stop()

counts=summary.get("row_counts",{})
metrics=summary.get("forecast_metrics",{})
rec_summary=summary.get("recommendation_summary",{})
validation=summary.get("validation_summary",{})
k1,k2,k3,k4=st.columns(4)
k1.metric("Sales records",f"{counts.get('sales_history.csv',0):,}")
k2.metric("Forecast records",f"{counts.get('forecast.csv',0):,}")
k3.metric("Recommended units",f"{rec_summary.get('total_recommended_units',0):,}")
k4.metric("Validation",validation.get("status","Not run"))

tabs=st.tabs(["Recommendations","Forecasts","Exceptions & Ground Truth","Metrics & Data"])
with tabs[0]:
    p=DATA_DIR/"purchase_recommendation.csv"
    if p.exists():
        df=pd.read_csv(p)
        a,b=st.columns(2)
        status=a.selectbox("Status",["ALL"]+sorted(df.status.unique().tolist()))
        store=b.selectbox("Store",["ALL"]+sorted(df.store_id.unique().tolist()))
        if status!="ALL": df=df[df.status==status]
        if store!="ALL": df=df[df.store_id==store]
        st.dataframe(df.head(1000), width="stretch")
        st.download_button("Download filtered recommendations",df.to_csv(index=False),"recommendations.csv","text/csv")
    else: st.info("Generate data and run replenishment to see recommendations.")
with tabs[1]:
    p=DATA_DIR/"forecast.csv"
    if p.exists():
        preview=pd.read_csv(p,nrows=1000)
        st.caption(f"Previewing the first {len(preview):,} forecast rows. The complete CSV remains available below.")
        st.dataframe(preview, width="stretch")
        with open(p,"rb") as forecast_file:
            st.download_button("Download complete forecast CSV",forecast_file,"forecast.csv","text/csv")
        daily_totals = {}
        for chunk in pd.read_csv(p,chunksize=50000,usecols=["target_date","predicted_demand"]):
            totals=chunk.groupby("target_date").predicted_demand.sum()
            for day,value in totals.items(): daily_totals[day]=daily_totals.get(day,0.0)+float(value)
        if daily_totals:
            daily_forecast=pd.DataFrame({"target_date":list(sorted(daily_totals)),"predicted_demand":[daily_totals[d] for d in sorted(daily_totals)]})
            st.line_chart(daily_forecast, x="target_date", y="predicted_demand", width="stretch")
    else: st.info("Run forecasting to see forecast output.")
with tabs[2]:
    p=DATA_DIR/"purchase_recommendation.csv"
    if p.exists():
        df=pd.read_csv(p)
        st.subheader("Exception counts")
        exception_data = (
            df[df.status.isin(["BLOCKED", "CAPACITY_EXCEPTION"])]
            .groupby(["exception_reason", "status"], dropna=False)
            .size()
            .reset_index(name="count")
        )
        if not exception_data.empty:
            st.bar_chart(exception_data, x="exception_reason", y="count", color="status", width="stretch")
        else:
            st.info("No blocked or capacity exceptions in the current recommendations.")
    for name in ["ground_truth_results.json","ground_truth.json"]:
        p=DATA_DIR/name
        if p.exists():
            st.subheader(name)
            st.json(json.loads(p.read_text(encoding="utf-8")))
with tabs[3]:
    st.subheader("Forecast evaluation")
    st.json(metrics or {"message":"Run forecasting to produce evaluation metrics."})
    st.subheader("Recommendation summary")
    st.json(rec_summary or {"message":"Run replenishment to produce recommendations."})
    st.subheader("Validation report")
    st.json(validation or {"message":"Run validation."})
    if validation.get("status") == "INCOMPLETE":
        st.warning("Validation is incomplete. Run the missing workflow steps listed in the report before treating the project run as complete.")
    elif validation.get("status") == "FAIL":
        st.error("Validation found issues. Review the errors and checks in the report.")
    elif validation.get("status") == "PASS":
        st.success("All configured end-to-end validation checks passed for this dataset.")
    st.subheader("Manifest / reproducibility")
    if "manifest" in summary: st.json(summary["manifest"])
