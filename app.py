import streamlit as st
import firebase_admin
from firebase_admin import credentials, firestore
import hashlib

# 1. Initialize Firestore Read-Only (Use Secrets in Streamlit Cloud)
if not firebase_admin._apps:
    cred = credentials.Certificate(dict(st.secrets["gcp_service_account"]))
    firebase_admin.initialize_app(cred)
db = firestore.client()

# 2. Token Validation Logic (Must match the hash logic in your private app)
def validate_token(doc_number, provided_token):
    # This must match your Private App's build_public_share_token logic
    # It checks against the specific document ID in your Firestore
    doc_ref = db.collection('invoices').document(doc_number).get()
    if not doc_ref.exists:
        return False, None
    data = doc_ref.to_dict()
    # Ensure the hash matches the stored token for this document
    return data.get('share_token') == provided_token, data

# 3. Streamlit Interface
st.title("Document Verification Portal")

query_params = st.query_params
doc_id = query_params.get("share_doc")
token = query_params.get("token")

if doc_id and token:
    is_valid, data = validate_token(doc_id, token)
    if is_valid:
        st.success(f"Document {doc_id} Verified.")
        # Logic to render or download the PDF stored in Firestore/Storage
        st.download_button("📥 Download Invoice", data=..., file_name=f"{doc_id}.pdf")
    else:
        st.error("Invalid or expired link.")
else:
    st.warning("Please scan a valid QR code.")
