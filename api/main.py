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

# Optional imports (graceful fallback for demo mode)
try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False
    print("⚠️  FAISS not available. Recommendations will be disabled.")

try:
    from sentence_transformers import SentenceTransformer
    EMBEDDER_AVAILABLE = True
except ImportError:
    EMBEDDER_AVAILABLE = False
    print("⚠️  sentence-transformers not available. Recommendations will be disabled.")

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except (ImportError, TypeError) as e:
    GEMINI_AVAILABLE = False
    print(f"⚠️  google-generativeai not available: {type(e).__name__}")

# ---------- Config ----------
FAISS_INDEX_PATH = "embeddings/faiss.index"
META_PATH = "embeddings/metadata.pkl"
EMBED_MODEL_NAME = "all-MiniLM-L6-v2"
EMBED_MODEL_PATH = os.getenv("EMBED_MODEL_PATH", EMBED_MODEL_NAME)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# ---------- App ----------
app = FastAPI(title="SHL Assessment Recommender")

# Enable CORS for frontend communication
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static assets (CSS, images)
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------- Database helpers ----------
DB_PATH = "smartassess.db"

def init_db():
    """Initialize SQLite database and create users table if not exists"""
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
    try:
        return sqlite3.connect(DB_PATH)
    except Exception as e:
        print(f"Database connection error: {e}")
        return None

# Initialize database on startup
init_db()

# ---------- Load resources ----------
# Check if required files exist (only if FAISS is available)
index = None
metadata = None
embedder = None
llm = None

if FAISS_AVAILABLE and EMBEDDER_AVAILABLE:
    files_exist = os.path.exists(FAISS_INDEX_PATH) and os.path.exists(META_PATH)
    if not files_exist:
        print(f"⚠️  FAISS index or metadata not found. Recommendations disabled.")
        print(f"   Expected: {FAISS_INDEX_PATH} and {META_PATH}")
    else:
        try:
            index = faiss.read_index(FAISS_INDEX_PATH)
            with open(META_PATH, "rb") as f:
                metadata = pickle.load(f)
            print("✓ FAISS index loaded.")
        except Exception as e:
            print(f"⚠️  Failed to load FAISS index: {e}")
            index = None
            metadata = None

if EMBEDDER_AVAILABLE:
    EMBED_MODEL_NAME = "all-MiniLM-L6-v2"
    EMBED_MODEL_PATH = os.getenv("EMBED_MODEL_PATH", EMBED_MODEL_NAME)
    try:
        embedder = SentenceTransformer(EMBED_MODEL_PATH)
        print("✓ Embedding model loaded.")
    except Exception as e:
        print(f"⚠️  Failed to load embedding model: {e}")
        embedder = None

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if GEMINI_AVAILABLE and GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        llm = genai.GenerativeModel("gemini-1.5-flash")
        print("✓ Gemini LLM initialized.")
    except Exception as e:
        print(f"⚠️  Failed to initialize Gemini: {e}")
        llm = None
else:
    print("⚠️  GEMINI_API_KEY not set or google-generativeai unavailable.")

# ---------- Schemas ----------
class RecommendRequest(BaseModel):
    query: str

class Recommendation(BaseModel):
    assessment_name: str
    assessment_url: str

class RecommendResponse(BaseModel):
    recommendations: list[Recommendation]

# ---------- Helpers ----------
def fetch_text_from_url(url: str) -> str:
    r = requests.get(url, timeout=10)
    r.raise_for_status()
    return r.text[:12000]  # keep it bounded

def analyze_query_with_gemini(text: str) -> dict:
    if not llm:
        # Fallback when Gemini is not available
        return {
            "technical_skills": [],
            "soft_skills": [],
            "focus": "MIX"
        }
    
    prompt = f"""
You must return ONLY valid JSON.
No explanation. No markdown. No text outside JSON.

JSON format:
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
        raw = resp.text.strip()

        # Extract JSON if Gemini adds noise
        start = raw.find("{")
        end = raw.rfind("}")
        if start == -1 or end == -1:
            raise ValueError("No JSON found")

        json_text = raw[start:end + 1]

        import json
        return json.loads(json_text)

    except Exception as e:
        print("⚠️ Gemini parsing failed:", e)
        return {
            "technical_skills": [],
            "soft_skills": [],
            "focus": "MIX"
        }


def search_faiss(query_text: str, top_k: int = 20):
    if not index or not metadata or not embedder:
        return []
    try:
        q_emb = embedder.encode([query_text], normalize_embeddings=True)
        q_emb = np.array(q_emb).astype("float32")
        scores, idxs = index.search(q_emb, top_k)
        results = []
        for i in idxs[0]:
            if i < 0:
                continue
            results.append(metadata[i])
        return results
    except Exception as e:
        print(f"⚠️  FAISS search failed: {e}")
        return []

def rerank(results, focus, min_k=5, max_k=10):
    # Simple, deterministic re-rank:
    # If soft skills present → ensure P included
    if focus == "P":
        primary = [r for r in results if r.get("test_type") == "P"]
        secondary = [r for r in results if r.get("test_type") != "P"]
    elif focus == "K":
        primary = [r for r in results if r.get("test_type") == "K"]
        secondary = [r for r in results if r.get("test_type") != "K"]
    else:  # MIX
        primary = results
        secondary = []

    ordered = primary + secondary
    return ordered[:max_k] if len(ordered) >= min_k else ordered

# ---------- Endpoints ----------
@app.get("/health")
def health():
    return {"status": "ok"}

# Alias for frontend proxy compatibility
@app.get("/api/health")
def api_health_alias():
    return health()

@app.post("/recommend", response_model=RecommendResponse)
def recommend(req: RecommendRequest):
    if not index or not metadata or not embedder:
        raise HTTPException(
            status_code=503,
            detail="Recommendation service unavailable. FAISS index or embeddings not loaded."
        )
    
    text = req.query.strip()
    if not text:
        raise HTTPException(status_code=400, detail="Empty query")

    # URL handling
    if text.startswith("http://") or text.startswith("https://"):
        try:
            text = fetch_text_from_url(text)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {e}")

    analysis = analyze_query_with_gemini(text)
    focus = analysis.get("focus", "MIX")

    candidates = search_faiss(text, top_k=20)
    ranked = rerank(candidates, focus)

    recs = [
        Recommendation(
            assessment_name=r["assessment_name"],
            assessment_url=r["url"]
        )
        for r in ranked
    ]

    return {"recommendations": recs}

# Alias path to match frontend /api/recommend
@app.post("/api/recommend", response_model=RecommendResponse)
def recommend_alias(req: RecommendRequest):
    return recommend(req)

# ---------- Frontend Pages ----------
DEMO_MODE = os.getenv("DEMO_MODE", "0") == "1"

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

def page(path: str):
    full = os.path.join(FRONTEND_DIR, path)
    if not os.path.exists(full):
        raise HTTPException(status_code=404, detail="Page not found")
    return FileResponse(full)

@app.get("/")
def landing_page():
    return page("index.html")

@app.get("/login")
def login_page():
    return page("login.html")

@app.get("/signup")
def signup_page():
    return page("signup.html")

@app.get("/login-error")
def login_error_page():
    return page("login-error.html")

@app.get("/recommend-page")
def recommend_page(request: Request):
    user = request.cookies.get("user")
    if not user and not DEMO_MODE:
        return RedirectResponse("/login", status_code=302)
    return page("recommend.html")

@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie("user")
    return resp

# ---------- Auth (form posts) ----------
@app.post("/signup")
def signup_post(fullname: str = Form(...), email: str = Form(...), password: str = Form(...)):
    conn = get_connection()
    if not conn:
        return RedirectResponse("/login-error", status_code=302)
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (fullname, email, password) VALUES (?, ?, ?)",
            (fullname, email, password)
        )
        conn.commit()
        cur.close()
        conn.close()
        return RedirectResponse("/login", status_code=302)
    except Exception as e:
        print(f"Signup error: {e}")
        if conn:
            conn.close()
        return RedirectResponse("/login-error", status_code=302)

@app.post("/login")
def login_post(email: str = Form(...), password: str = Form(...)):
    conn = get_connection()
    if not conn:
        return RedirectResponse("/login-error", status_code=302)
    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM users WHERE email=? AND password=?",
            (email, password)
        )
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user:
            resp = RedirectResponse("/recommend-page", status_code=302)
            resp.set_cookie("user", email, httponly=True, samesite="lax")
            return resp
        else:
            return RedirectResponse("/login-error", status_code=302)
    except Exception as e:
        print(f"Login error: {e}")
        if conn:
            conn.close()
        return RedirectResponse("/login-error", status_code=302)
