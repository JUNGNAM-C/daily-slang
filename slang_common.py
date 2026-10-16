"""두 생성 스크립트(generate.py, generate_foreign.py)가 함께 쓰는 도우미.

단어 화면의 왼쪽은 '10년 전·20년 전·30년 전에 같은 뜻으로 쓰이던 말'(past), 오른쪽은 '반대어'입니다.
출처(https)가 확인되지 않은 항목은 모두 버립니다.
"""
import json
import re


def extract_json(text, kind="{"):
    pat = r"\{.*\}" if kind == "{" else r"\[.*\]"
    m = re.search(pat, text, re.S)
    if not m:
        raise ValueError("응답에서 JSON을 찾지 못했습니다:\n" + text[:500])
    return json.loads(m.group(0))


def _item(r, foreign):
    if not isinstance(r, dict):
        return None
    word = str(r.get("word", "")).strip()
    meaning = str(r.get("meaning", "")).strip()
    src = str(r.get("source", "")).strip()
    if not word or not meaning or not src.startswith("https://"):
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


def clean_past(items, foreign=False):
    """10·20·30년 전 유사어: 시기마다 최대 2개."""
    out, count = [], {10: 0, 20: 0, 30: 0}
    for r in items or []:
        try:
            years = int(r.get("years"))
        except Exception:
            continue
        if years not in count or count[years] >= 2:
            continue
        it = _item(r, foreign)
        if it:
            it["years"] = years
            out.append(it)
            count[years] += 1
    out.sort(key=lambda x: x["years"])
    return out


def clean_opposites(items, foreign=False):
    out = []
    for r in items or []:
        it = _item(r, foreign)
        if it:
            it["type"] = "반대 뜻"
            out.append(it)
    return out[:2]


def past_rules(lang_label, year):
    return (f"- past: 지금 고른 단어와 같은 의미로 10년 전({year - 10}년경), 20년 전({year - 20}년경), 30년 전({year - 30}년경)에 {lang_label}에서 쓰이던 말을 시기마다 최대 2개 찾아 넣으세요. "
            "해당 시기에 쓰였다는 것이 출처에서 실제로 확인된 말만 넣고, 확인되지 않으면 그 시기는 비우세요. 억지로 채우지 마세요. 각 항목에 years(10|20|30), word, meaning(한 문장), source(https)를 넣으세요.")


def opposite_rules(lang_label):
    return f"- 반대어: 뜻이 반대인 {lang_label} 단어를 출처에서 확인된 것만 최대 2개 찾아 넣으세요. 확인되지 않으면 비우세요."


ENRICH_PROMPT = """{lang_label} 신조어 '{word}'(뜻: {meaning})에 대해 웹 검색으로 아래를 찾아 주세요.

규칙
{past_rules}
{opp_rules}
{pron_rule}
- 설명은 출처 문장을 베끼지 말고 직접 쓴 한국어 문장으로 쓰세요. 비속어·혐오 표현은 넣지 마세요.

마지막에는 설명 없이 아래 형식의 JSON 객체 하나만 출력하세요. 없는 항목은 빈 배열입니다.
{{"past": [{{"years": 10, "word": "...", "meaning": "...", "source": "https://..."{extra}}}], "opposite": [{{"word": "...", "meaning": "...", "source": "https://..."{extra}}}]}}
"""


def enrich(client, model, words, lang_label, foreign, limit, today_year, label=lambda w: w["word"]):
    """past·반대어가 아직 확인되지 않은 단어를 채웁니다. 한 번 확인한 단어는 enriched로 표시해 다시 찾지 않습니다."""
    todo = [w for w in words if not w.get("enriched")]
    todo.sort(key=lambda w: w.get("date") or "", reverse=True)  # 최근 날짜가 붙은 단어부터
    done = 0
    for w in todo[:limit]:
        print(f"  10·20·30년 전 유사어와 반대어 찾는 중: {w['word']}")
        label_l = lang_label(w) if callable(lang_label) else lang_label
        extra = ', "reading": "...", "pron_ko": "한글 발음"' if foreign else ""
        pron_rule = "- 각 항목의 pron_ko는 원어 발음을 한글로만 적고, reading에는 일본어는 히라가나, 중국어는 병음(영어는 빈 문자열)을 쓰세요." if foreign else ""
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=3000,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 6}],
                messages=[{"role": "user", "content": ENRICH_PROMPT.format(
                    lang_label=label_l, word=w["word"], meaning=w["meaning"], past_rules=past_rules(label_l, today_year),
                    opp_rules=opposite_rules(label_l), pron_rule=pron_rule, extra=extra)}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            data = extract_json(text)
        except Exception as e:
            print("    실패(다음에 다시 시도):", e)
            continue
        past = clean_past(data.get("past"), foreign)
        opp = clean_opposites(data.get("opposite"), foreign)
        if past and "past" not in w:
            w["past"] = past
        if opp and not any(r.get("type") == "반대 뜻" for r in w.get("related", [])):
            w["related"] = list(w.get("related", [])) + opp
        w["enriched"] = True
        done += 1
    return done
