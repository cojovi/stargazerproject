import os, re, json, time, hashlib
from datetime import datetime
from pathlib import Path
import requests, yaml

POSTS_DIR = Path(os.getenv("POSTS_DIR", "blog/source/_posts")).resolve()
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "gpt-4o-mini")

ISSUE_TITLE  = os.getenv("ISSUE_TITLE", "").strip()
ISSUE_BODY   = os.getenv("ISSUE_BODY", "")
ISSUE_NUMBER = os.getenv("ISSUE_NUMBER", "0")

TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
LOCAL_LLM_URL  = os.getenv("LOCAL_LLM_URL", "")

def slugify(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or f"post-{int(time.time())}"

def parse_issue(body: str) -> dict:
    data = {
        "topic": "",
        "tone": "direct, witty, Texan straight-shooter",
        "tags": [],
        "category": None,
        "min_words": 900,
        "draft": False,
        "model": DEFAULT_MODEL
    }
    for line in body.splitlines():
        if ":" in line:
            k,v = line.split(":",1)
            k = k.strip().lower()
            v = v.strip()
            if k == "topic" and v: data["topic"] = v
            elif k == "tone" and v: data["tone"] = v
            elif k == "tags" and v: data["tags"] = [t.strip() for t in re.split(r",|\s+", v) if t.strip()]
            elif k == "category" and v: data["category"] = v
            elif k == "min words" and v.isdigit(): data["min_words"] = int(v)
            elif k == "draft" and v.lower() in ("true","false"): data["draft"] = (v.lower()=="true")
            elif k == "model" and v: data["model"] = v
    if not data["topic"]:
        data["topic"] = body.strip() or ISSUE_TITLE.replace("[Blog]", "").strip()
    return data

def tavily(query: str, k:int=6)->list[dict]:
    if not TAVILY_API_KEY: return []
    r = requests.post("https://api.tavily.com/search",
                      json={"api_key": TAVILY_API_KEY, "query": query, "search_depth":"advanced",
                            "max_results":k, "include_answer":False}, timeout=60)
    r.raise_for_status()
    return r.json().get("results", [])

def openai_chat(prompt: str, model: str)->str:
    if not OPENAI_API_KEY: raise RuntimeError("OPENAI_API_KEY missing and LOCAL_LLM_URL not set")
    r = requests.post("https://api.openai.com/v1/chat/completions",
                      headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                      json={"model": model, "temperature":0.7,
                            "messages":[
                              {"role":"system","content":"You are a senior technical writer who outputs clean Markdown."},
                              {"role":"user","content": prompt}
                            ]},
                      timeout=180)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def local_llm(prompt:str, model:str)->str:
    if not LOCAL_LLM_URL: raise RuntimeError("LOCAL_LLM_URL not configured")
    r = requests.post(LOCAL_LLM_URL, json={"model": model, "prompt": prompt, "stream": False}, timeout=180)
    r.raise_for_status()
    j = r.json()
    return j.get("response") or j.get("text") or ""

def build_prompt(topic:str, tone:str, min_words:int, findings:list[dict])->str:
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

def write_post(front: dict, body_md: str, slug: str) -> Path:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    path = POSTS_DIR / f"{slug}.md"
    import yaml as _yaml
    doc = "---\n" + _yaml.safe_dump(front, sort_keys=False).strip() + "\n---\n\n" + body_md.strip() + "\n"
    path.write_text(doc, encoding="utf-8")
    return path

def title_from_md(md:str, fallback:str)->str:
    m = re.search(r"^#\s+(.+)$", md, flags=re.M)
    if m: return m.group(1).strip()
    for line in md.splitlines():
        if line.strip(): return line.strip()[:120]
    return fallback[:120]

def main():
    cfg = parse_issue(ISSUE_BODY)
    topic, tone = cfg["topic"], cfg["tone"]
    tags, category = cfg["tags"], cfg["category"]
    min_words, draft = int(cfg["min_words"]), bool(cfg["draft"])
    model = cfg["model"] or DEFAULT_MODEL

    findings = tavily(topic, k=6)
    source_urls = [f.get("url","") for f in findings if f.get("url")]

    prompt = build_prompt(topic, tone, min_words, findings)
    body = openai_chat(prompt, model) if OPENAI_API_KEY else local_llm(prompt, model)

    title = title_from_md(body, fallback=topic.title())
    base = slugify(title)
    uniq = hashlib.sha1(f"{time.time()}-{title}".encode()).hexdigest()[:6]
    slug = f"{base}-{uniq}"

    front = {
        "title": title,
        "date": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        "tags": tags,
        "categories": [category] if category else [],
        "draft": draft,
        "slug": slug,
        "sources": source_urls
    }
    path = write_post(front, body, slug)

    # expose output
    gh_out = os.getenv("GITHUB_OUTPUT")
    if gh_out:
      with open(gh_out, "a", encoding="utf-8") as f:
        f.write(f"slug={slug}\n")

    print(f"Wrote: {path}")

if __name__ == "__main__":
    main()
