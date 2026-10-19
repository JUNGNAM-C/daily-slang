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
from urllib.parse import urljoin
from types import SimpleNamespace as NS

API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
REDIRECT_HOSTS = ("vertexaisearch.cloud.google.com", "grounding-api-redirect")
UA = "Mozilla/5.0 (compatible; daily-slang-bot/1.0)"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None   # 따라가지 않고 멈춤 → 응답 머리글의 Location만 읽습니다


_opener_noredir = urllib.request.build_opener(_NoRedirect)


def resolve_url(uri, timeout=12):
    """구글 중계 주소면 가리키는 원래 페이지 주소로 풀고, 아니면 그대로 돌려줍니다. 실패하면 None.
    목적지 사이트에는 접속하지 않고, 구글이 알려 주는 이동 주소(Location)만 읽습니다."""
    if not uri:
        return None
    cur = uri
    for _ in range(4):
        if not any(h in cur for h in REDIRECT_HOSTS):
            return cur
        try:
            req = urllib.request.Request(cur, headers={"User-Agent": UA})
            with _opener_noredir.open(req, timeout=timeout) as r:   # 200이면 이동 주소가 없는 것
                return None
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location") if e.headers else None
            if e.code in (301, 302, 303, 307, 308) and loc:
                cur = urljoin(cur, loc)
                continue
            return None
        except Exception:
            return None
    return None if any(h in cur for h in REDIRECT_HOSTS) else cur


def _domain(x):
    """주소나 사이트 이름에서 도메인만 소문자로 뽑습니다(www. 제외)."""
    x = str(x or "").strip().lower()
    if not x:
        return ""
    if "://" in x:
        x = x.split("://", 1)[1]
    x = x.split("/")[0].split("?")[0]
    return x[4:] if x.startswith("www.") else x


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
        prompt += ("\n\n[추가 규칙] 모든 항목의 source에는 주소(URL)를 쓰지 말고, 방금 검색에서 실제로 읽은 사이트의 도메인 이름만 쓰세요"
                   "(예: namu.wiki, chosun.com). 주소는 시스템이 검색 결과에서 채웁니다. 검색에서 읽지 않은 사이트는 쓰지 마세요.")
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
        gm = cand.get("groundingMetadata") or {}
        chunks = gm.get("groundingChunks") or []
        info = []   # 청크 순서대로 (사이트 도메인, 원래 페이지 주소 또는 None)
        for ch in chunks:
            web = ch.get("web") or {}
            u = self.resolver(web.get("uri"))
            info.append((_domain(web.get("title")) or _domain(u), u))
        grounded = []
        for dom, u in info:
            if u and all(u != g[1] for g in grounded):
                grounded.append((dom, u))
        # 답변의 어느 문장이 어느 검색 결과(청크)에서 나왔는지(지지 정보)
        said = {}
        for sp in gm.get("groundingSupports") or []:
            seg = ((sp.get("segment") or {}).get("text") or "")
            for i in sp.get("groundingChunkIndices") or []:
                said[i] = said.get(i, "") + " " + seg
        print(f"    [Gemini 검색] 검색 결과 {len(chunks)}건 → 주소 확인 {len(grounded)}건 · 문장 연결 {len(said)}건 · 사이트: {', '.join(sorted({d for d, _ in grounded})) or '없음'}")
        if chunks and not grounded:
            print("::warning title=Gemini 검색 주소::검색 결과는 있었지만 원래 주소로 풀지 못했습니다(중계 주소 해석 실패)")
        if not chunks:
            print("::warning title=Gemini 검색::이번 답변에는 검색 결과(groundingChunks)가 없었습니다")
        results = [NS(url=u) for _, u in grounded]
        seen = {u for _, u in grounded}
        # 모델이 출처 주소를 직접 쓰면 지어낸 주소가 많아서, 출처는 '사이트 도메인'만 적게 하고 주소는 시스템이 채웁니다.
        # 같은 사이트의 검색된 페이지 중, ① 그 단어가 들어간 문장이 그 페이지에서 나왔다고 연결돼 있거나
        # ② 페이지를 열어 보니 본문에 그 단어가 있는 것만 씁니다. 둘 다 확인되지 않으면 버립니다.
        try:
            from slang_common import extract_json
            obj = extract_json(text)
        except Exception:
            obj = None
        if obj is not None:
            for d in _walk(obj):
                if "source" not in d:
                    continue
                src, word = str(d.get("source", "")).strip(), str(d.get("word", "")).strip()
                host = _domain(src)
                if src in seen:                      # 이미 검색 결과에 있는 주소
                    continue
                cand_idx = [i for i, (dom, u) in enumerate(info)
                            if u and host and (dom == host or dom.endswith("." + host) or host.endswith("." + dom))]
                pick = None
                w = word.lower()
                for i in cand_idx:                   # ① 문장 연결
                    if w and w in said.get(i, "").lower():
                        pick = info[i][1]
                        break
                if pick is None:
                    for i in cand_idx[:4]:           # ② 페이지 본문 확인
                        body = self.fetcher(info[i][1])
                        if body and w and w in body.lower():
                            pick = info[i][1]
                            break
                if pick is None and not cand_idx and src.startswith("https://") and w:   # 검색 결과에 없는 사이트면 직접 열어 확인
                    body = self.fetcher(src)
                    if body and w in body.lower():
                        seen.add(src)
                        results.append(NS(url=src))
                        continue
                if pick:
                    d["source"] = pick
            text = json.dumps(obj, ensure_ascii=False)
        return NS(content=[NS(type="web_search_tool_result", content=results), NS(type="text", text=text)])


class GeminiClient:
    def __init__(self, key, **kw):
        self.messages = _Messages(key, **kw)
