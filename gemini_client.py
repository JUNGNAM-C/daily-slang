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


def norm_eq(a, b):
    """두 주소가 같은 페이지를 가리키는지(끝의 / 와 대소문자 차이 무시)."""
    f = lambda u: str(u or "").strip().split("#")[0].rstrip("/").lower()
    return f(a) == f(b)


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

    @staticmethod
    def _text(data):
        cands = data.get("candidates") or []
        if not cands:
            raise RuntimeError("Gemini가 답을 주지 않았습니다: " + json.dumps(data.get("promptFeedback", data), ensure_ascii=False)[:200])
        cand = cands[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
        return cand, text

    def _research(self, model, base, k):
        """1단계: 'JSON으로만 답하라'는 형식 요구가 있으면 Gemini가 검색을 건너뛰므로(진단 결과),
        먼저 형식 없이 자유롭게 검색·조사만 시킵니다."""
        prompt = ("아래 과제를 수행하는 데 필요한 자료를 구글 검색으로 충분히 조사해서, 서술형 메모로 정리해 주세요.\n"
                  "- 최종 답(JSON)은 아직 쓰지 마세요. 어떤 말을 찾았는지, 뜻, 쓰임, 어느 시기에 쓰였는지를 근거와 함께 적으세요.\n"
                  "- 검색에서 읽은 내용만 쓰고, 어느 사이트에서 읽었는지 사이트 이름을 함께 적으세요.\n\n"
                  "[과제 설명]\n" + base)
        if k >= 1:
            prompt = "(지난번에는 검색을 하지 않았습니다. 이번에는 반드시 구글 검색부터 하세요.)\n" + prompt
        data = self._post(model, {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                   "tools": [{"google_search": {}}],
                                   "generationConfig": {"maxOutputTokens": 8000, "temperature": 0.4}})
        cand, notes = self._text(data)
        gm = cand.get("groundingMetadata") or {}
        chunks = gm.get("groundingChunks") or []
        info = []   # 청크 순서대로 (사이트 도메인, 원래 페이지 주소 또는 None)
        for ch in chunks:
            web = ch.get("web") or {}
            u = self.resolver(web.get("uri"))
            info.append((_domain(web.get("title")) or _domain(u), u))
        said = {}   # 청크 번호 → 그 청크에서 나왔다고 연결된 조사 메모 문장들
        for sp in gm.get("groundingSupports") or []:
            seg = ((sp.get("segment") or {}).get("text") or "")
            for i in sp.get("groundingChunkIndices") or []:
                said[i] = said.get(i, "") + " " + seg
        ok = sum(1 for _, u in info if u)
        print(f"    [Gemini 검색] 검색 결과 {len(chunks)}건 → 주소 확인 {ok}건 · 문장 연결 {len(said)}건 · 사이트: {', '.join(sorted({d for d, u in info if u})) or '없음'}")
        if chunks and not ok:
            print("::warning title=Gemini 검색 주소::검색 결과는 있었지만 원래 주소로 풀지 못했습니다(중계 주소 해석 실패)")
        if not chunks:
            print("::warning title=Gemini 검색::이번 조사에는 검색 결과(groundingChunks)가 없었습니다")
        return notes, info, said

    def _format(self, model, max_tokens, base, notes, info):
        """2단계: 조사 메모와 실제 검색 출처 목록을 주고, 원래 과제의 형식(JSON)으로 정리시킵니다. (검색 도구 없이)"""
        lst = "\n".join(f"[{i + 1}] {dom} — {u}" for i, (dom, u) in enumerate(info) if u)
        prompt = (base + "\n\n[조사 메모 — 방금 구글 검색으로 조사한 결과입니다. 이 안의 내용만 쓰세요]\n" + notes +
                  "\n\n[출처 후보 — 실제로 검색에서 읽은 페이지 목록]\n" + lst +
                  "\n\n[규칙] 모든 항목의 source에는 위 목록의 주소를 한 글자도 바꾸지 말고 그대로 쓰세요. "
                  "그 말이 실제로 나온 페이지를 고르고, 목록에 근거가 없는 말은 항목에서 빼세요. 설명 없이 요구된 JSON만 출력하세요.")
        data = self._post(model, {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                   "generationConfig": {"maxOutputTokens": max(int(max_tokens), 8000), "temperature": 0.3}})
        return self._text(data)[1]

    def create(self, model, max_tokens, messages, tools=None, **_):
        base = "\n".join(m["content"] for m in messages if m.get("role") == "user")
        notes, info, said = "", [], {}
        for k in range(3):   # 검색이 한 번도 안 된 조사는 최대 3번까지 다시
            notes, info, said = self._research(model, base, k)
            if any(u for _, u in info):
                break
            print(f"    [Gemini 검색] 검색 없이 답해서 다시 요청합니다 ({k + 1}/3)")
        urls = [u for _, u in info if u]
        if not urls:   # 끝내 검색이 없으면 출처를 붙일 수 없으니 답을 만들지 않습니다
            return NS(content=[NS(type="web_search_tool_result", content=[]),
                               NS(type="text", text="검색 결과가 없어 답을 만들지 못했습니다.")], grounded=0)
        text = self._format(model, max_tokens, base, notes, info)
        results = [NS(url=u) for u in dict.fromkeys(urls)]
        # 검증: 모델이 고른 출처가 그 단어의 근거인지 확인합니다. 근거가 확인된 출처만 남기고 나머지는 비웁니다(→ 버려짐).
        #  ① 조사 메모에서 그 단어가 든 문장이 그 페이지에서 나왔다고 연결돼 있거나
        #  ② 그 페이지를 열어 보니 본문에 그 단어가 있으면 인정합니다.
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
                w = word.lower()
                chosen = [i for i, (_, u) in enumerate(info) if u and norm_eq(u, src)]
                host = _domain(src)
                same = [i for i, (dom, u) in enumerate(info)
                        if u and host and i not in chosen and (dom == host or dom.endswith("." + host) or host.endswith("." + dom))]
                order = chosen + [i for i in same] + [i for i, (_, u) in enumerate(info) if u and i not in chosen and i not in same]
                pick = None
                for i in order:                               # ① 문장 연결
                    if w and w in said.get(i, "").lower():
                        pick = info[i][1]
                        break
                if pick is None:
                    for i in (chosen + same)[:4]:             # ② 페이지 본문 확인 (모델이 고른 곳과 같은 사이트만)
                        body = self.fetcher(info[i][1])
                        if body and w and w in body.lower():
                            pick = info[i][1]
                            break
                d["source"] = pick or ""
            text = json.dumps(obj, ensure_ascii=False)
        return NS(content=[NS(type="web_search_tool_result", content=results), NS(type="text", text=text)], grounded=len(urls))


class GeminiClient:
    def __init__(self, key, **kw):
        self.messages = _Messages(key, **kw)
