import os, re, json, time, hashlib, textwrap
from datetime import datetime
from pathlib import Path

import requests
import yaml

# ----- Config -----
POSTS_DIR = Path(os.getenv("POSTS_DIR", "source/_posts")).resolve()
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-4o-mini")

ISSUE_TITLE = os.getenv("ISSUE_TITLE", "").strip()
ISSUE_BODY  = os.getenv("ISSUE_BODY", "")
ISSUE_NUMBER = os.getenv("ISSUE_NUMBER", "0")

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LOCAL_LLM_URL  = os.getenv("LOCAL_LLM_URL", "")

# ----- Helpers -----
def slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or f"post-{int(time.time())}"

def parse_issue_template(body: str) -> dict:
    """
    Parses GitHub Issue form-rendered body (which comes through as Markdown with headings).
    Falls back gracefully if user didn't use the template.
    """
    data = {
        "topic": "",
        "tone": "direct, witty, Texan straight-shooter",
        "tags": [],
        "category": None,
        "min_words": 900,
        "draft": False,
        "model": DEFAULT_MODEL
    }
    # naive parse of "Label\n\nvalue"
    blocks = re.split(r"\n#{1,6}\s*", body)  # in case GH adds headings
    # Also support key: value lines
    for line in body.splitlines():
        if ":" in line:
            k,v = line.split(":",1)
            k = k.strip().lower()
            v = v.strip()
            if k == "topic" and v:
                data["topic"] = v
            elif k == "tone" and v:
                data["tone"] = v
            elif k == "tags" and v:
                data["tags"] = [t.strip() for t in re.split(r",|\s+", v) if t.strip()]
            elif k == "category" and v:
                data["category"] = v
            elif k == "min words" and v.isdigit():
                data["min_words"] = int(v)
            elif k == "draft" and v.lower() in ("true","false"):
                data["draft"] = (v.lower() == "true")
            elif k == "model" and v:
                data["model"] = v

    # If still empty, treat entire body as topic
    if not data["topic"]:
        data["topic"] = body.strip() or ISSUE_TITLE.replace("[Blog]", "").strip()

    return data

def tavily_search(query: str, k: int = 6) -> list[dict]:
    if not TAVILY_API_KEY:
        return []
    url = "https://api.tavily.com/search"
    payload = {
        "api_key": TAVILY_API_KEY,
        "query": query,
        "search_depth": "advanced",
        "max_results": k,
        "include_answer": False
    }
    r = requests.post(url, json=payload, timeout=60)
    r.raise_for_status()
    j = r.json()
    return j.get("results", [])

def call_openai_chat(prompt: str, model: str) -> str:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing and LOCAL_LLM_URL not set")
    url = "https://api.openai.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}
    data = {
        "model": model,
        "temperature": 0.7,
        "messages": [
            {"role": "system", "content": "You are a senior technical writer who outputs clean Markdown."},
            {"role": "user", "content": prompt}
        ]
    }
    r = requests.post(url, headers=headers, json=data, timeout=120)
    r.raise_for_status()
    j = r.json()
    return j["choices"][0]["message"]["content"]

def call_local_llm(prompt: str, model: str) -> str:
    """
    Generic local endpoint: expects POST {model, prompt, stream:false} → {response: "..."}
    e.g., an Ollama-compatible gateway you run behind ngrok.
    """
    if not LOCAL_LLM_URL:
        raise RuntimeError("LOCAL_LLM_URL not configured")
    r = requests.post(LOCAL_LLM_URL, json={"model": model, "prompt": prompt, "stream": False}, timeout=180)
    r.raise_for_status()
    j = r.json()
    return j.get("response") or j.get("text") or ""

def build_prompt(topic: str, tone: str, min_words: int, findings: list[dict]) -> str:
    sources = "\n".join([f"- {f.get('title','?')} — {f.get('url','')}" for f in findings])
    return f"""Write a Hexo-ready Markdown blog post.

Requirements:
- Topic: {topic}
- Tone: {tone}
- Length: >= {min_words} words
- Start with an engaging paragraph, then insert `<!-- more -->`.
- Use clear H2/H3 sections, code blocks if helpful, and a concise summary.
- End with a section "## Sources" listing the provided URLs as bullets.
- Output ONLY the Markdown body (no YAML front matter).

You may rely on and paraphrase from these sources (do not copy verbatim):
{sources}
"""

def ensure_posts_dir():
    POSTS_DIR.mkdir(parents=True, exist_ok=True)

def write_post(front: dict, body_md: str, slug: str) -> Path:
    path = POSTS_DIR / f"{slug}.md"
    doc = "---\n" + yaml.safe_dump(front, sort_keys=False).strip() + "\n---\n\n" + body_md.strip() + "\n"
    path.write_text(doc, encoding="utf-8")
    return path

def make_slug(title: str) -> str:
    s = slugify(title)
    # Avoid collisions by appending short hash of time+title
    uniq = hashlib.sha1(f"{time.time()}-{title}".encode()).hexdigest()[:6]
    return f"{s}-{uniq}"

def extract_title_from_markdown(md: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", md, flags=re.M)
    if m:
        return m.group(1).strip()
    # try first non-empty line
    for line in md.splitlines():
        if line.strip():
            return line.strip()[:120]
    return fallback.strip()[:120]

def main():
    data = parse_issue_template(ISSUE_BODY)
    topic     = data["topic"]
    tone      = data["tone"]
    tags      = data["tags"]
    category  = data["category"]
    min_words = int(data["min_words"])
    draft     = bool(data["draft"])
    model     = data["model"] or DEFAULT_MODEL

    ensure_posts_dir()

    # Research
    findings = tavily_search(topic, k=6)
    source_urls = [f.get("url","") for f in findings if f.get("url")]

    # Draft
    prompt = build_prompt(topic, tone, min_words, findings)
    if OPENAI_API_KEY:
        body_md = call_openai_chat(prompt, model=model)
    else:
        body_md = call_local_llm(prompt, model=model)

    # Title & slug
    title = extract_title_from_markdown(body_md, fallback=topic.title())
    slug = make_slug(title)

    # Front matter for Hexo
    front = {
        "title": title,
        "date": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "tags": tags,
        "categories": [category] if category else [],
        "draft": draft,
        "slug": slug,
        "sources": source_urls
    }

    path = write_post(front, body_md, slug)

    # Expose slug to workflow
    print(f"::set-output name=slug::{slug}")
    print(f"Wrote: {path}")

if __name__ == "__main__":
    main()
