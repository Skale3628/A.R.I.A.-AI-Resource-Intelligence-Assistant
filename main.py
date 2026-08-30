"""
main.py - Flask app for HR Assistant RAG Chatbot

Features:
- Upload, delete, and search HR policy documents (TXT) for retrieval-augmented QA.
- All documents chunked, embedded, and stored in a local FAISS index.
- Context-aware LLM answers with chat history.
- Secure session management; all config/secrets in .env.

Extensible:
- Swap FAISS for any vector DB (Azure Cognitive Search, Pinecone, Qdrant, etc.).
- Pluggable chunking, embedding, and prompt engineering.
- Easy to extend for per-user document isolation, advanced auth, cloud storage, or new UI.


"""

import os
from flask import Flask, request, jsonify, render_template, session
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

from rag_utils.faiss_utils import (
    allowed_file, build_index_and_docs, load_index_and_docs, rag_chat_search
)
from rag_utils.embedding import get_embedding
from rag_utils.gpt import chatgpt_response
from rag_utils.prompts import HRPrompts

# -----------------------------------------------------------------------------
# App Setup & Config
# -----------------------------------------------------------------------------

load_dotenv()
UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "data")
INDEX_STORE = os.getenv("INDEX_STORE", "vector_db")
ALLOWED_EXTENSIONS = {'txt'}

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "very_secret_key_12345")

# Ensure upload and vector db folders exist
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)
if not os.path.exists(INDEX_STORE):
    os.makedirs(INDEX_STORE)

# Load or initialize FAISS index and document store on startup
faiss_index, docs = load_index_and_docs()


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------

@app.route("/", methods=["GET"])
def home():
    """
    Render the main chat UI for HR Assistant.

    Returns:
        HTML page with chat interface.

    Other options:
        - Use a SPA (React, Vue) frontend and serve only APIs here.
        - Add login/register UI and route accordingly.
        - Allow for language/culture-specific chat UIs.
    """
    return render_template("index.html")


@app.route("/manage", methods=["GET"])
def manage():
    """
    Render the management UI for uploading and deleting documents.

    Returns:
        HTML page with upload/delete controls.

    Other options:
        - Expose upload/delete only to admin or logged-in users.
        - Use a drag-and-drop modern JS uploader.
        - Support bulk import (zip, folder upload).
    """
    return render_template("manage.html")


@app.route("/list_files", methods=["GET"])
def list_files():
    """
    API to list all uploaded TXT files in the upload folder.

    Returns:
        JSON: {"files": [list of filenames]}

    Other options:
        - Return additional metadata (upload date, size).
        - Store file info in a DB and return paginated lists.
        - Support listing per-user files only.
    """
    files = [f for f in os.listdir(UPLOAD_FOLDER) if f.endswith('.txt')]
    return jsonify({"files": files})


@app.route("/upload", methods=["POST"])
def upload_files():
    """
    API endpoint for uploading TXT documents.

    - Accepts multiple files (with key 'files[]').
    - Saves to UPLOAD_FOLDER.
    - Triggers chunking, embedding, and index rebuild.

    Returns:
        JSON with upload status and message.

    Other options:
        - Accept more file types (PDF, DOCX), add OCR/extract text logic.
        - Store files in cloud (Azure Blob, S3, GCS) and load directly from there.
        - Use background tasks/queues for chunking/indexing large uploads.
        - Add file deduplication, virus scanning, or validation.
        - Support per-user or per-org isolation: save to subfolders by user/org.
    """
    if 'files[]' not in request.files:
        return jsonify({"error": "No files part"}), 400
    files = request.files.getlist('files[]')
    uploaded = []
    file_contents = []
    for file in files:
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            filepath = os.path.join(UPLOAD_FOLDER, filename)
            file.save(filepath)
            uploaded.append(filename)
            with open(filepath, "r", encoding="utf-8") as f:
                file_contents.append((filename, f.read()))
    # Rebuild FAISS index and docs after upload
    global faiss_index, docs
    faiss_index, docs = build_index_and_docs(get_embedding, file_contents=file_contents)
    return jsonify({"uploaded": uploaded, "message": f"Uploaded {len(uploaded)} files. Index rebuilt."})


@app.route("/delete_file", methods=["POST"])
def delete_file():
    """
    API endpoint for deleting a TXT file and updating the vector DB.

    Expects JSON {"filename": "name.txt"}
    - Removes file from UPLOAD_FOLDER.
    - Rebuilds index/docs without the deleted file.

    Returns:
        JSON with delete status and message.

    Other options:
        - Instead of rebuilding, support in-place deletion from FAISS (advanced).
        - Store deleted files in a recycle bin/soft-delete folder for restore.
        - Require user confirmation or admin rights for deletion.
        - Add audit logs for file operations.
    """
    data = request.get_json(force=True)
    filename = data.get("filename")
    path = os.path.join(UPLOAD_FOLDER, filename)
    if not os.path.exists(path):
        return jsonify({"error": "File not found"}), 404
    os.remove(path)
    # Rebuild FAISS index/docs
    files = [f for f in os.listdir(UPLOAD_FOLDER) if f.endswith('.txt')]
    file_contents = []
    for fname in files:
        with open(os.path.join(UPLOAD_FOLDER, fname), "r", encoding="utf-8") as f:
            file_contents.append((fname, f.read()))
    global faiss_index, docs
    faiss_index, docs = build_index_and_docs(get_embedding, file_contents=file_contents)
    return jsonify({"deleted": filename, "message": "File deleted and index rebuilt."})


@app.route("/search", methods=["POST"])
def search():
    """
    API endpoint for RAG QA: retrieves top-k relevant context from the vector DB
    and returns LLM-powered answer (with full chat history).

    Expects:
        JSON: {"query": str, "top_k": int (optional, default 3)}

    Returns:
        JSON: {
            "results": [top doc chunks with scores/meta],
            "answer": str,
            "history": list of {"user":..., "bot":...}
        }

    Other options:
        - Use other vector DBs (Azure Cognitive Search, Pinecone, Qdrant, Chroma).
        - Hybrid retrieval: combine FAISS with BM25/keyword/metadata filtering.
        - Add semantic reranking, MMR, diversity or passage scoring.
        - For multi-user, search only within current user's doc chunks.
        - Add streaming responses for LLM answers (SSE/WebSockets).
        - Integrate feedback/rating API for model improvement.
    """
    data = request.get_json(force=True)
    query = data.get("query", "")
    k = int(data.get("top_k", 3))
    resp, code = rag_chat_search(
        query, k, session,
        faiss_index, docs,
        get_embedding,
        HRPrompts.get_system_prompt,
        chatgpt_response
    )
    print(resp)
    return jsonify(resp), code


@app.route("/new_chat", methods=["POST"])
def new_chat():
    """
    API endpoint to clear the current session's conversation history.

    Returns:
        JSON {"status": "cleared"}

    Other options:
        - Persist chat history to DB for analytics or user access later.
        - Allow users to export chat as PDF or CSV.
        - Log cleared sessions for security/audit.
    """
    session.pop("history", None)
    return jsonify({"status": "cleared"})


# -----------------------------------------------------------------------------
# Main Entrypoint
# -----------------------------------------------------------------------------

if __name__ == "__main__":
    """
    Run the Flask app (debug mode). For production, use Gunicorn or Azure App Service entrypoint.

    Other options:
        - Run with Gunicorn, uWSGI, or other WSGI servers in production.
        - Deploy to Azure App Service, AWS Elastic Beanstalk, GCP Cloud Run, etc.
        - Serve behind HTTPS and reverse proxy (nginx, Azure Front Door).
        - Use environment variable for PORT.
    """
    app.run(host="0.0.0.0", port=8000, debug=True)
