"""두 생성 스크립트(generate.py, generate_foreign.py)가 함께 쓰는 도우미.

단어 화면의 왼쪽은 '10년 전·20년 전·30년 전에 같은 뜻으로 쓰이던 말'(past), 오른쪽은 '반대어'입니다.
출처(https)가 확인되지 않은 항목은 모두 버립니다.
"""
import json
import os
import re
import sys
from urllib.parse import urlparse


def norm_url(u):
    """주소 비교용: 앞뒤 공백·#조각·끝의 /를 없애고 주소 앞부분(도메인)은 소문자로."""
    u = str(u or "").strip().split("#")[0].rstrip("/")
    try:
        p = urlparse(u)
        return f"{p.scheme.lower()}://{p.netloc.lower()}{p.path}" + (f"?{p.query}" if p.query else "")
    except Exception:
        return u


def search_urls(resp):
    """이번 응답에서 AI가 웹 검색으로 실제로 받아 본 페이지 주소(검색 결과·인용)만 모읍니다.
    출처로 적힌 주소가 이 목록에 없으면 AI가 지어낸 주소일 수 있으므로 버립니다."""
    urls = set()
    for b in getattr(resp, "content", []) or []:
        t = getattr(b, "type", "")
        if t == "web_search_tool_result":
            c = getattr(b, "content", None)
            if isinstance(c, list):
                for r in c:
                    u = getattr(r, "url", None)
                    if u:
                        urls.add(norm_url(u))
        for c in getattr(b, "citations", None) or []:
            u = getattr(c, "url", None)
            if u:
                urls.add(norm_url(u))
    return urls


def source_ok(src, allowed):
    """출처는 https 주소이고, (allowed가 주어지면) 실제 검색에서 본 주소여야 합니다."""
    src = str(src or "").strip()
    return src.startswith("https://") and (allowed is None or norm_url(src) in allowed)


def extract_json(text, kind="{"):
    pat = r"\{.*\}" if kind == "{" else r"\[.*\]"
    m = re.search(pat, text, re.S)
    if not m:
        raise ValueError("응답에서 JSON을 찾지 못했습니다:\n" + text[:500])
    return json.loads(m.group(0))


def _item(r, foreign, allowed=None):
    if not isinstance(r, dict):
        return None
    word = str(r.get("word", "")).strip()
    meaning = str(r.get("meaning", "")).strip()
    src = str(r.get("source", "")).strip()
    if not word or not meaning or not source_ok(src, allowed):
        return None
    out = {"word": word, "meaning": meaning, "source": src}
    if foreign:
        pron = str(r.get("pron_ko", "")).strip()
        if not re.search(r"[가-힣]", pron):
            return None  # 외국어는 한글 발음이 있어야 합니다
        out["pron_ko"] = pron
        reading = str(r.get("reading", "")).strip()
        if reading and reading != word:
            out["reading"] = reading
    return out


def clean_past(items, foreign=False, allowed=None):
    """10·20·30년 전 유사어: 시기마다 최대 2개."""
    out, count = [], {10: 0, 20: 0, 30: 0}
    for r in items or []:
        try:
            years = int(r.get("years"))
        except Exception:
            continue
        if years not in count or count[years] >= 2:
            continue
        it = _item(r, foreign, allowed)
        if it:
            it["years"] = years
            out.append(it)
            count[years] += 1
    out.sort(key=lambda x: x["years"])
    return out


def clean_opposites(items, foreign=False, allowed=None):
    out = []
    for r in items or []:
        it = _item(r, foreign, allowed)
        if it:
            it["type"] = "반대 뜻"
            out.append(it)
    return out[:2]


def past_rules(lang_label, year, only=None):
    """only=10/20/30이면 그 시기만, None이면 세 시기 모두."""
    eras = list(only) if only else [10, 20, 30]
    spans = ", ".join(f"{e}년 전({year - e}년경)" for e in eras)
    return (f"- past: 지금 고른 단어와 {spans}에 쓰이던 {lang_label} 말을 시기마다 최대 2개 찾아 넣으세요. "
            "정확히 그 해가 아니어도, 출처에 그 시기 전후 5년 안에 쓰였다고 나오면 됩니다. "
            "뜻이 같은 말뿐 아니라, 같은 상황에서 같은 역할로 쓰이던 말도 넣으세요(예: 놀람·당황·짜증·감탄 같은 반응 표현, 같은 방식으로 말을 줄이거나 비틀어 만든 말, 외국어에서 들어와 쓰이던 표현 포함). "
            "각 항목의 meaning에는 그 말의 뜻과, 지금 단어와 어떤 점이 비슷한지를 한 문장으로 쓰세요. "
            "그 시기에 쓰였다는 것이 출처에서 실제로 확인된 말만 넣고, 확인되지 않으면 그 시기는 비우세요. 억지로 채우지 마세요. "
            "연도별·시대별 유행어를 정리한 기사, 사전, 논문, 위키 문서를 검색해서 찾으세요. 각 항목에 years(" + "|".join(map(str, eras)) + "), word, meaning, source(https)를 넣으세요.")


def opposite_rules(lang_label):
    return f"- 반대어: 뜻이 반대인 {lang_label} 단어를 출처에서 확인된 것만 최대 2개 찾아 넣으세요. 확인되지 않으면 비우세요."


ENRICH_PROMPT = """{lang_label} 신조어 '{word}'(뜻: {meaning})에 대해 웹 검색으로 아래를 찾아 주세요.

규칙
{rules}
{pron_rule}
- 출처(source) 주소는 웹 검색 결과에 실제로 나온 페이지 주소만 쓰세요. 주소를 짐작하거나 지어내면 그 항목은 버려집니다.
- 설명은 출처 문장을 베끼지 말고 직접 쓴 한국어 문장으로 쓰세요. 비속어·혐오 표현은 넣지 마세요.

마지막에는 설명 없이 아래 형식의 JSON 객체 하나만 출력하세요. 없는 항목은 빈 배열입니다.
{shape}
"""


def enrich(client, model, words, lang_label, foreign, limit, today_year):
    """10·20·30년 전 유사어와 반대어가 아직 확인되지 않은 단어를 채웁니다.
    시기마다(그리고 반대어는 따로) 검색을 나눠서 하므로 한 번에 다 찾을 때보다 잘 찾습니다.
    한 번 확인한 단어는 enriched로 표시해 다시 찾지 않습니다."""
    todo = [w for w in words if not w.get("enriched")]
    todo.sort(key=lambda w: w.get("date") or "", reverse=True)  # 최근 날짜가 붙은 단어부터
    extra = ', "reading": "...", "pron_ko": "한글 발음"' if foreign else ""
    pron_rule = "- 각 항목의 pron_ko는 원어 발음을 한글로만 적고, reading에는 일본어는 히라가나, 중국어는 병음(영어는 빈 문자열)을 쓰세요." if foreign else ""
    done = 0
    for w in todo[:limit]:
        label_l = lang_label(w) if callable(lang_label) else lang_label
        print(f"  유사어·반대어 찾는 중: {w['word']}")
        past, opp, failed = [], [], False
        # 검색은 10년 / 20·30년 / 반대어 세 번으로 나눕니다 (30년 전은 출처가 드물어 20년 전과 묶음)
        jobs = [(eras, past_rules(label_l, today_year, eras),
                 '{"past": [{"years": %s, "word": "...", "meaning": "...", "source": "https://..."%s}]}' % ("|".join(map(str, eras)), extra))
                for eras in ((10,), (20, 30))]
        if not any(r.get("type") == "반대 뜻" for r in w.get("related", [])):
            jobs.append(("opp", opposite_rules(label_l),
                         '{"opposite": [{"word": "...", "meaning": "...", "source": "https://..."%s}]}' % extra))
        for key, rules, shape in jobs:
            try:
                resp = client.messages.create(
                    model=model,
                    max_tokens=2500,
                    tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}],
                    messages=[{"role": "user", "content": ENRICH_PROMPT.format(
                        lang_label=label_l, word=w["word"], meaning=w["meaning"], rules=rules, pron_rule=pron_rule, shape=shape)}],
                )
                text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
                data = extract_json(text)
                allowed = search_urls(resp)
            except Exception as e:
                print(f"    실패({key}, 다음에 다시 시도):", e)
                failed = True
                continue
            if key == "opp":
                opp = clean_opposites(data.get("opposite"), foreign, allowed)
            else:
                past += [x for x in clean_past(data.get("past"), foreign, allowed) if x["years"] in key]
        if failed:
            continue  # 한 번이라도 실패한 단어는 enriched로 표시하지 않고 다음에 다시 시도합니다
        if past and "past" not in w:
            w["past"] = sorted(past, key=lambda x: x["years"])
        if opp:
            w["related"] = list(w.get("related", [])) + opp
        w["enriched"] = True
        done += 1
    return done


def make_client():
    """쓸 AI를 고릅니다. 돌려주는 값: (client, 새 단어용 모델, 기존 단어 채우기용 모델, 이름)
    - 환경변수 SLANG_PROVIDER가 gemini/anthropic이면 그대로, 없으면 GEMINI_API_KEY가 있을 때 Gemini, 아니면 Anthropic.
    - 모델 이름은 SLANG_GEMINI_MODEL / SLANG_MODEL / SLANG_ENRICH_MODEL로 바꿀 수 있습니다."""
    prov = os.environ.get("SLANG_PROVIDER") or ("gemini" if os.environ.get("GEMINI_API_KEY") else "anthropic")
    if prov == "gemini":
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            return None, None, None, "gemini"
        from gemini_client import GeminiClient
        m = os.environ.get("SLANG_GEMINI_MODEL", "gemini-2.5-flash")
        return GeminiClient(key), m, m, "gemini"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, None, None, "anthropic"
    try:
        import anthropic
    except ImportError:
        sys.exit("anthropic 패키지가 없습니다. 'pip install anthropic'을 실행해 주세요.")
    return (anthropic.Anthropic(), os.environ.get("SLANG_MODEL", "claude-sonnet-5-5"),
            os.environ.get("SLANG_ENRICH_MODEL", "claude-haiku-5-5"), "anthropic")
