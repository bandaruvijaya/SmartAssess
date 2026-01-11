import os
import sqlite3
import pickle
import requests
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# =========================
# Optional imports (safe)
# =========================
try:
    import faiss
    FAISS_AVAILABLE = True
except ImportError:
    FAISS_AVAILABLE = False

try:
    from sentence_transformers import SentenceTransformer
    EMBEDDER_AVAILABLE = True
except ImportError:
    EMBEDDER_AVAILABLE = False

try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False

# =========================
# App
# =========================
app = FastAPI(title="SmartAssess API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # restrict after Netlify URL is known
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# Config
# =========================
DB_PATH = os.getenv("DATABASE_PATH", "./data/smartassess.db")
FAISS_INDEX_PATH = "embeddings/faiss.index"
META_PATH = "embeddings/metadata.pkl"
EMBED_MODEL_NAME = "all-MiniLM-L6-v2"
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

# =========================
# Database
# =========================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fullname TEXT,
            email TEXT UNIQUE,
            password TEXT
        )
    """)
    conn.commit()
    conn.close()

def get_db():
    return sqlite3.connect(DB_PATH)

init_db()

# =========================
# ML Resources (LAZY LOAD)
# =========================
index = None
metadata = None
embedder = None
llm = None

def load_faiss():
    global index, metadata
    if not FAISS_AVAILABLE:
        return
    if index is None and os.path.exists(FAISS_INDEX_PATH):
        index = faiss.read_index(FAISS_INDEX_PATH)
        with open(META_PATH, "rb") as f:
            metadata = pickle.load(f)

def get_embedder():
    global embedder
    if not EMBEDDER_AVAILABLE:
        raise HTTPException(503, "sentence-transformers not installed")

    if embedder is None:
        try:
            embedder = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception as e:
            print("❌ Model load failed:", e)
            raise HTTPException(503, "Embedding model failed to load")

    return embedder


def get_llm():
    global llm
    if not GEMINI_AVAILABLE or not GEMINI_API_KEY:
        return None
    if llm is None:
        genai.configure(api_key=GEMINI_API_KEY)
        llm = genai.GenerativeModel("gemini-1.5-flash")
    return llm

# =========================
# Schemas
# =========================
class SignupRequest(BaseModel):
    fullname: str
    email: str
    password: str

class LoginRequest(BaseModel):
    email: str
    password: str

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
def analyze_query(text: str):
    llm = get_llm()
    if not llm:
        return {"focus": "MIX"}
    try:
        prompt = f"""
Return ONLY JSON:
{{ "focus": "K | P | A | MIX" }}
Text: {text}
"""
        r = llm.generate_content(prompt).text
        import json
        return json.loads(r[r.find("{"):r.rfind("}")+1])
    except Exception:
        return {"focus": "MIX"}

def search_faiss(query, top_k=10):
    load_faiss()
    model = get_embedder()
    if index is None or metadata is None:
        return []
    emb = model.encode([query], normalize_embeddings=True)
    scores, idxs = index.search(np.array(emb).astype("float32"), top_k)
    return [metadata[i] for i in idxs[0] if i >= 0]

# =========================
# Health
# =========================
@app.get("/health")
def health():
    return {"status": "ok"}

# =========================
# Auth APIs (JSON)
# =========================
@app.post("/signup")
def signup(data: SignupRequest):
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO users (fullname, email, password) VALUES (?, ?, ?)",
            (data.fullname, data.email, data.password)
        )
        conn.commit()
        conn.close()
        return {"success": True}
    except Exception:
        raise HTTPException(400, "User already exists")

@app.post("/login")
def login(data: LoginRequest):
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "SELECT * FROM users WHERE email=? AND password=?",
        (data.email, data.password)
    )
    user = cur.fetchone()
    conn.close()
    if not user:
        raise HTTPException(401, "Invalid credentials")
    return {"success": True}

# =========================
# Recommendation API
# =========================
@app.post("/recommend", response_model=RecommendResponse)
def recommend(req: RecommendRequest):
    results = search_faiss(req.query)
    if not results:
        raise HTTPException(503, "Recommendation unavailable")

    return {
        "recommendations": [
            {
                "assessment_name": r["assessment_name"],
                "assessment_url": r["url"]
            } for r in results
        ]
    }

