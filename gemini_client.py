"""Gemini API를 Anthropic SDK와 같은 모양(client.messages.create)으로 쓰게 해 주는 얇은 어댑터.

- 구글 검색 연결(google_search 도구)로 웹을 검색합니다.
- 응답의 groundingChunks에 있는 출처 주소를 모아 'web_search_tool_result' 블록으로 돌려줍니다.
  (slang_common.search_urls가 그대로 읽어서, '검색에서 실제로 본 주소만 출처로 인정'하는 규칙이 똑같이 적용됩니다.)
- Gemini가 주는 출처 주소는 구글 중계 주소(vertexaisearch.cloud.google.com/grounding-api-redirect/…)인 경우가 많아서,
  따라 들어가 원래 페이지 주소로 풀어 줍니다. 풀지 못한 주소는 버립니다.
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


class _Messages:
    def __init__(self, key, resolver=resolve_url, opener=None):
        self.key = key
        self.resolver = resolver
        self.opener = opener or urllib.request.urlopen

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
        results = []
        for ch in chunks:
            u = self.resolver((ch.get("web") or {}).get("uri"))
            if u:
                results.append(NS(url=u))
        return NS(content=[NS(type="web_search_tool_result", content=results), NS(type="text", text=text)])


class GeminiClient:
    def __init__(self, key, **kw):
        self.messages = _Messages(key, **kw)
