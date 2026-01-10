import os
import pickle
import requests
import sqlite3
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import numpy as np

# =========================
# Optional imports (graceful fallback)
# =========================
try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False
    print("⚠️ FAISS not available. Recommendations disabled.")

try:
    from sentence_transformers import SentenceTransformer
    EMBEDDER_AVAILABLE = True
except ImportError:
    EMBEDDER_AVAILABLE = False
    print("⚠️ sentence-transformers not available.")

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except (ImportError, TypeError):
    GEMINI_AVAILABLE = False
    print("⚠️ google-generativeai not available.")

# =========================
# Config
# =========================
FAISS_INDEX_PATH = "embeddings/faiss.index"
META_PATH = "embeddings/metadata.pkl"
EMBED_MODEL_NAME = "all-MiniLM-L6-v2"
EMBED_MODEL_PATH = os.getenv("EMBED_MODEL_PATH", EMBED_MODEL_NAME)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# =========================
# App
# =========================
app = FastAPI(title="SHL Assessment Recommender")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

# =========================
# Database (SQLite – Railway Safe)
# =========================
DB_PATH = os.getenv("DATABASE_PATH", "./data/smartassess.db")

# Ensure data directory exists
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fullname TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()

def get_connection():
    return sqlite3.connect(DB_PATH)

init_db()

# =========================
# Load resources
# =========================
index = None
metadata = None
embedder = None
llm = None

if FAISS_AVAILABLE and EMBEDDER_AVAILABLE:
    if os.path.exists(FAISS_INDEX_PATH) and os.path.exists(META_PATH):
        try:
            index = faiss.read_index(FAISS_INDEX_PATH)
            with open(META_PATH, "rb") as f:
                metadata = pickle.load(f)
            print("✓ FAISS index loaded")
        except Exception as e:
            print(f"⚠️ Failed to load FAISS index: {e}")

if EMBEDDER_AVAILABLE:
    try:
        embedder = SentenceTransformer(EMBED_MODEL_PATH)
        print("✓ Embedding model loaded")
    except Exception as e:
        print(f"⚠️ Failed to load embedder: {e}")

if GEMINI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        llm = genai.GenerativeModel("gemini-1.5-flash")
        print("✓ Gemini initialized")
    except Exception as e:
        print(f"⚠️ Gemini init failed: {e}")

# =========================
# Schemas
# =========================
class RecommendRequest(BaseModel):
    query: str

class Recommendation(BaseModel):
    assessment_name: str
    assessment_url: str

class RecommendResponse(BaseModel):
    recommendations: list[Recommendation]

# =========================
# Helpers
# =========================
def fetch_text_from_url(url: str) -> str:
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.text[:12000]

def analyze_query_with_gemini(text: str) -> dict:
    if not llm:
        return {"technical_skills": [], "soft_skills": [], "focus": "MIX"}

    prompt = f"""
Return ONLY JSON:
{{
  "technical_skills": [],
  "soft_skills": [],
  "focus": "K | P | A | MIX"
}}
Text:
{text}
"""
    try:
        resp = llm.generate_content(prompt)
        raw = resp.text
        start, end = raw.find("{"), raw.rfind("}")
        import json
        return json.loads(raw[start:end + 1])
    except Exception:
        return {"technical_skills": [], "soft_skills": [], "focus": "MIX"}

def search_faiss(query_text: str, top_k: int = 20):
    if not index or not metadata or not embedder:
        return []
    q_emb = embedder.encode([query_text], normalize_embeddings=True)
    scores, idxs = index.search(np.array(q_emb).astype("float32"), top_k)
    return [metadata[i] for i in idxs[0] if i >= 0]

def rerank(results, focus, max_k=10):
    if focus in ["K", "P"]:
        primary = [r for r in results if r.get("test_type") == focus]
        secondary = [r for r in results if r.get("test_type") != focus]
        return (primary + secondary)[:max_k]
    return results[:max_k]

# =========================
# API Endpoints
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/recommend", response_model=RecommendResponse)
def recommend(req: RecommendRequest):
    if not index or not embedder:
        raise HTTPException(503, "Recommendation service unavailable")

    text = req.query.strip()
    analysis = analyze_query_with_gemini(text)
    ranked = rerank(search_faiss(text), analysis.get("focus", "MIX"))

    return {
        "recommendations": [
            {"assessment_name": r["assessment_name"], "assessment_url": r["url"]}
            for r in ranked
        ]
    }

@app.post("/api/recommend", response_model=RecommendResponse)
def recommend_alias(req: RecommendRequest):
    return recommend(req)

# =========================
# Frontend
# =========================
DEMO_MODE = os.getenv("DEMO_MODE", "0") == "1"
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

def page(name):
    path = os.path.join(FRONTEND_DIR, name)
    if not os.path.exists(path):
        raise HTTPException(404)
    return FileResponse(path)

@app.get("/")
def landing():
    return page("index.html")

@app.get("/login")
def login():
    return page("login.html")

@app.get("/signup")
def signup():
    return page("signup.html")

@app.get("/recommend-page")
def recommend_page(request: Request):
    if not request.cookies.get("user") and not DEMO_MODE:
        return RedirectResponse("/login", 302)
    return page("recommend.html")

@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", 302)
    resp.delete_cookie("user")
    return resp

# =========================
# Auth
# =========================
@app.post("/signup")
def signup_post(fullname: str = Form(...), email: str = Form(...), password: str = Form(...)):
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (fullname, email, password) VALUES (?, ?, ?)",
            (fullname, email, password)
        )
        conn.commit()
        conn.close()
        return RedirectResponse("/login", 302)
    except Exception:
        return RedirectResponse("/login", 302)

@app.post("/login")
def login_post(email: str = Form(...), password: str = Form(...)):
    conn = get_connection()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE email=? AND password=?", (email, password))
    user = cur.fetchone()
    conn.close()

    if user:
        resp = RedirectResponse("/recommend-page", 302)
        resp.set_cookie("user", email, httponly=True, samesite="lax")
        return resp
    return RedirectResponse("/login", 302)
