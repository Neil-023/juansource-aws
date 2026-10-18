import os
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Optional
from dotenv import load_dotenv, find_dotenv
from tavily import TavilyClient
from langchain_core.prompts import PromptTemplate
from .semantic_cache import (
    get_claim_embedding,
    lookup_semantic_cache,
    store_semantic_cache,
)
from .source_allowlist import ALLOWED_SOURCE_DOMAINS, filter_allowed_source_results

load_dotenv(find_dotenv(usecwd=True) or Path(__file__).resolve().parents[2] / ".env")

_llm: Optional[object] = None
_search: Optional[TavilyClient] = None
genai = None
_genai_exc = None
try:
    from langchain_google_genai import ChatGoogleGenerativeAI
    _langchain_google_exc = None
except Exception as exc:
    ChatGoogleGenerativeAI = None
    _langchain_google_exc = exc
    try:
        import google.generativeai as genai
    except Exception as genai_exc:
        _genai_exc = genai_exc

def _ensure_tavily_search() -> TavilyClient:
    global _search
    if _search is not None:
        return _search
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise RuntimeError(
            "Missing Tavily credentials: TAVILY_API_KEY. "
            "Set them in your environment or .env file."
        )
    _search = TavilyClient(api_key=api_key)
    return _search


def _run_tavily_search(search: TavilyClient, query: str, max_results: int = 10):
    response = search.search(
        query=query,
        search_depth="basic",
        max_results=max_results,
        include_domains=ALLOWED_SOURCE_DOMAINS,
    )
    if not isinstance(response, dict):
        return []
    results = response.get("results")
    if not isinstance(results, list):
        return []
    return filter_allowed_source_results(results)


def _format_search_results(results) -> str:
    if isinstance(results, str):
        return results[:3000]
    if isinstance(results, list):
        chunks = []
        for idx, item in enumerate(results[:10], start=1):
            if not isinstance(item, dict):
                continue
            title = item.get("title") or "Untitled"
            url = item.get("url") or item.get("link") or ""
            snippet = (
                item.get("content")
                or item.get("snippet")
                or item.get("description")
                or ""
            )[:500]
            chunks.append(f"{idx}. {title}\nURL: {url}\nSummary: {snippet}")
        return "\n\n".join(chunks)
    return str(results)

class _NativeGeminiClient:
    def __init__(self, api_key: str, model_name: str, temperature: float):
        if genai is None:
            raise RuntimeError(
                "LangChain Gemini adapter failed to load "
                "and google-generativeai is unavailable. "
                "Run: pip install -U langchain-core langchain-google-genai google-generativeai"
            ) from (_genai_exc or _langchain_google_exc)
        genai.configure(api_key=api_key)
        self._model = genai.GenerativeModel(
            model_name=model_name,
            generation_config={"temperature": temperature},
        )

    def invoke(self, prompt: str):
        result = self._model.generate_content(prompt)
        text = getattr(result, "text", None)
        if not text and hasattr(result, "candidates"):
            parts = []
            for candidate in result.candidates or []:
                if getattr(candidate, "content", None):
                    for part in getattr(candidate.content, "parts", []) or []:
                        content = getattr(part, "text", None) or part if isinstance(part, str) else ""
                        if content:
                            parts.append(content)
            text = "\n".join(parts)
        return SimpleNamespace(content=(text or str(result)))

def _ensure_llm() -> object:
    global _llm
    if _llm is not None:
        return _llm
    api_key = os.getenv('GOOGLE_API_KEY')
    creds_path = os.getenv('GOOGLE_APPLICATION_CREDENTIALS')
    if not api_key and not creds_path:
        raise RuntimeError(
            "No Gemini credentials found. Provide GOOGLE_API_KEY or GOOGLE_APPLICATION_CREDENTIALS "
            "before starting the backend."
        )
    model_name = os.getenv('GEMINI_MODEL', 'gemini-3.8-flash')
    temperature = float(os.getenv('LLM_TEMPERATURE', '0.1'))
    if ChatGoogleGenerativeAI is not None:
        _llm = ChatGoogleGenerativeAI(
            model=model_name,
            temperature=temperature,
            google_api_key=api_key or None,
        )
    else:
        _llm = _NativeGeminiClient(
            api_key=api_key or "",
            model_name=model_name,
            temperature=temperature,
        )
    return _llm

# III. The Reasoning Prompt Template
RAG_PROMPT_TEMPLATE = """
**FACT-CHECKER ASSIGNMENT: RAG Fake News Detector**

You are an objective, expert fact-checker. Your task is to analyze a user's query against
the real-time evidence retrieved from Tavily Search.

**1. QUERY/CLAIM TO VERIFY:**
{query}

**2. RETRIEVED EVIDENCE (Search Results):**
{search_results}

**INSTRUCTIONS FOR REASONING:**
A. **Classification:** Determine the veracity of the QUERY.
   - If the search results overwhelmingly confirm the claim, classify it as **REAL**.
   - If the search results **contradict** or **cannot find any corroborating information** for the claim, classify it as **FAKE**.
B. **Reasoning:** Your explanation must explicitly reference the information found in the **RETRIEVED EVIDENCE** section.
C. **Evidence Sourcing:** After writing your reasoning, create a list of the source **links** (URLs) from the **RETRIEVED EVIDENCE** that directly support your conclusion.

**FINAL OUTPUT FORMAT:**
Classification: [REAL or FAKE]
Reasoning: [Provide a concise, detailed, and evidence-based explanation for your classification.]
Evidence: [
  "https://www.source-link-1.com/article",
  "https://www.source-link-2.com/news"
]
"""
RAG_PROMPT = PromptTemplate.from_template(RAG_PROMPT_TEMPLATE)

def _extract_section(text: str, label: str) -> str:
    pattern = re.compile(rf"{label}\s*(.*?)(?=\n[A-Z][a-zA-Z]+:|$)", re.IGNORECASE | re.DOTALL)
    match = pattern.search(text)
    return match.group(1).strip() if match else ""

def _normalise_classification(value: str) -> str:
    lowered = (value or "").lower()
    if any(token in lowered for token in ["real", "true", "verified"]):
        return "real"
    if any(token in lowered for token in ["fake", "false", "hoax"]):
        return "fake"
    return "unknown"

def _parse_fact_check_output(raw: str):
    classification = _extract_section(raw, "Classification:")
    reasoning = _extract_section(raw, "Reasoning:")
    evidence_block = _extract_section(raw, "Evidence:")
    evidence = re.findall(r"https?://[^\s\"')]+", evidence_block or "")
    return _normalise_classification(classification), reasoning or raw.strip(), evidence

def run_fact_check(claim: str):
    if not claim.strip():
        return {"error": "Claim must not be empty."}

    claim_embedding = None
    try:
        claim_embedding = get_claim_embedding(claim)
        cache_hit = lookup_semantic_cache(claim_embedding)
        if cache_hit:
            print(
                "0. Semantic cache hit "
                f"(similarity={cache_hit.get('similarity', 0.0):.3f})."
            )
            return {
                "classification": cache_hit.get("classification", "unknown"),
                "reasoning": cache_hit.get("reasoning", ""),
                "evidence": cache_hit.get("evidence", []),
                "raw": cache_hit.get("raw", ""),
            }
    except Exception as cache_exc:
        print(f"0. Semantic cache lookup skipped: {cache_exc}")

    print(f"1. Verifying Claim: '{claim}'")
    try:
        search = _ensure_tavily_search()
        print("2. Performing Tavily Search...")
        search_results = _run_tavily_search(search, claim, max_results=10)
        print("3. Evidence retrieved.")
    except Exception as e:
        print(f"Error during Tavily Search: {e}")
        return {"error": str(e)}

    try:
        llm = _ensure_llm()
        final_prompt = RAG_PROMPT.format(
            query=claim,
            search_results=_format_search_results(search_results),
        )
        print("4. Sending evidence to Gemini for Reasoning...")
        response = llm.invoke(final_prompt)
        raw_text = getattr(response, "content", str(response)).strip()
        classification, reasoning, evidence = _parse_fact_check_output(raw_text)
        result = {
            "classification": classification,
            "reasoning": reasoning,
            "evidence": evidence,
            "raw": raw_text,
        }

        try:
            if claim_embedding is None:
                claim_embedding = get_claim_embedding(claim)
            store_semantic_cache(claim, claim_embedding, result)
            print("5. Semantic cache stored.")
        except Exception as cache_exc:
            print(f"5. Semantic cache write skipped: {cache_exc}")

        return result
    except Exception as e:
        print(f"Error during Gemini Reasoning: {e}")
        return {"error": str(e)}
