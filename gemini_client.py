"""Gemini API를 Anthropic SDK와 같은 모양(client.messages.create)으로 쓰게 해 주는 얇은 어댑터.

- 구글 검색 연결(google_search 도구)로 웹을 검색합니다.
- 응답의 groundingChunks에 있는 출처 주소를 모아 'web_search_tool_result' 블록으로 돌려줍니다.
  (slang_common.search_urls가 그대로 읽어서, '검색에서 실제로 본 주소만 출처로 인정'하는 규칙이 똑같이 적용됩니다.)
- Gemini가 주는 출처 주소는 구글 중계 주소(vertexaisearch.cloud.google.com/grounding-api-redirect/…)인 경우가 많아서,
  따라 들어가 원래 페이지 주소로 풀어 줍니다. 풀지 못한 주소는 버립니다.
- 답변이 적은 출처가 검색 결과 목록에 없으면, 그 주소를 지금 직접 열어 보고 해당 단어가 본문에 있을 때만 인정합니다.
- 표준 라이브러리만 사용합니다(추가 설치 없음).
"""
import json
import os
import time
import urllib.error
import urllib.request
from types import SimpleNamespace as NS

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com", "grounding-api-redirect")
UA = "Mozilla/5.0 (compatible; daily-slang-bot/1.0)"


def resolve_url(uri, timeout=12):
    """구글 중계 주소면 최종 페이지 주소로 풀고, 아니면 그대로 돌려줍니다. 실패하면 None."""
    if not uri:
        return None
    if not any(h in uri for h in REDIRECT_HOSTS):
        return uri
    try:
        req = urllib.request.Request(uri, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # 리다이렉트를 따라감
            final = r.geturl()
        if any(h in final for h in REDIRECT_HOSTS):
            return None
        return final
    except urllib.error.HTTPError as e:   # 목적지가 403 등을 줘도 최종 주소는 알 수 있음
        final = getattr(e, "url", None) or e.geturl()
        return None if (not final or any(h in final for h in REDIRECT_HOSTS)) else final
    except Exception:
        return None


def fetch_text(url, timeout=12, limit=800_000):
    """페이지를 실제로 받아 봅니다. 성공(200)하면 본문 글자, 아니면 None."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ko,en;q=0.8"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status != 200:
                return None
            return r.read(limit).decode(r.headers.get_content_charset() or "utf-8", "ignore")
    except Exception:
        return None


def _walk(o):
    """JSON 안의 모든 객체(dict)를 차례로 돌려줍니다."""
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


class _Messages:
    def __init__(self, key, resolver=resolve_url, opener=None, fetcher=fetch_text):
        self.key = key
        self.resolver = resolver
        self.opener = opener or urllib.request.urlopen
        self.fetcher = fetcher

    def _post(self, model, body):
        req = urllib.request.Request(
            API.format(model=model), data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.key}, method="POST")
        last = None
        for i in range(4):   # 한도 초과(429)·서버 오류(5xx)는 잠깐 쉬었다가 다시
            try:
                with self.opener(req, timeout=180) as r:
                    return json.loads(r.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8")[:300]
                except Exception:
                    pass
                last = RuntimeError(f"Gemini 오류 {e.code}: {detail}")
                if e.code not in (429, 500, 502, 503, 504):
                    raise last
            except Exception as e:
                last = e
            time.sleep(8 * (i + 1))
        raise last

    def create(self, model, max_tokens, messages, tools=None, **_):
        prompt = "\n".join(m["content"] for m in messages if m.get("role") == "user")
        body = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"maxOutputTokens": max(int(max_tokens), 8000),
                                 "temperature": 0.4, "thinkingConfig": {"thinkingBudget": 1024}},
        }
        data = self._post(model, body)
        cands = data.get("candidates") or []
        if not cands:
            raise RuntimeError("Gemini가 답을 주지 않았습니다: " + json.dumps(data.get("promptFeedback", data), ensure_ascii=False)[:200])
        cand = cands[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
        chunks = (cand.get("groundingMetadata") or {}).get("groundingChunks") or []
        results, seen = [], set()
        for ch in chunks:
            raw = (ch.get("web") or {}).get("uri")
            u = self.resolver(raw)
            if u:
                if raw and u != raw:
                    text = text.replace(raw, u)   # 답변이 중계 주소를 그대로 적었다면 원래 주소로 바꿈
                if u not in seen:
                    seen.add(u)
                    results.append(NS(url=u))
        # 답변이 적은 출처 중 검색 결과에 없는 주소는, 지금 실제로 열어 보고 그 단어가 본문에 있으면 인정합니다.
        # (열리지 않거나 단어가 없으면 인정하지 않습니다 = 지어낸 주소는 여전히 버려집니다)
        try:
            from slang_common import extract_json
            obj = extract_json(text)
        except Exception:
            obj = None
        if obj is not None:
            for d in _walk(obj):
                src, word = str(d.get("source", "")).strip(), str(d.get("word", "")).strip()
                if not src.startswith("https://") or not word or src in seen:
                    continue
                if any(h in src for h in REDIRECT_HOSTS):
                    fixed = self.resolver(src)
                    if not fixed:
                        continue
                    text = text.replace(src, fixed)
                    src = fixed
                    if src in seen:
                        continue
                body = self.fetcher(src)
                if body and word.lower() in body.lower():
                    seen.add(src)
                    results.append(NS(url=src))
        return NS(content=[NS(type="web_search_tool_result", content=results), NS(type="text", text=text)])


class GeminiClient:
    def __init__(self, key, **kw):
        self.messages = _Messages(key, **kw)
