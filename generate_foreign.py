#!/usr/bin/env python3
"""
오늘의 외국어 신조어 생성 스크립트 (한국어 신조어 시스템과는 완전히 별개)

하는 일
  1. 웹 검색으로 영어·일본어·중국어 신조어를 언어별로 하나씩 찾고,
  2. foreign.json에 아직 없는 단어를 골라 뜻·유래·예문·한국어 발음을 정리한 뒤,
  3. 오늘 날짜(한국 시간)를 붙여 foreign.json 맨 끝에 추가합니다.

앱에서는 노란 버튼이 영어, 초록 버튼이 일본어/중국어(무작위)를 보여 줍니다.

필요한 것: Python 3.9+, pip install anthropic, 환경변수 ANTHROPIC_API_KEY

사용법
  python generate_foreign.py                 # 오늘 없는 언어만 새로 만들어 추가
  python generate_foreign.py --dry-run       # 파일은 건드리지 않고 결과만 출력
  python generate_foreign.py --lang en       # 영어만
  python generate_foreign.py --force         # 오늘 단어가 있어도 지우고 새로 만듦
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    import anthropic
except ImportError:
    sys.exit("anthropic 패키지가 없습니다. 먼저 'pip install anthropic'을 실행해 주세요.")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import slang_common as sc  # noqa: E402

HERE = Path(__file__).resolve().parent
PATH = HERE / "foreign.json"
MODEL = os.environ.get("SLANG_MODEL", "claude-sonnet-5-5")
KST = ZoneInfo("Asia/Seoul")

LANGS = {
    "en": ("영어", "영어권(미국·영국 등)", "reading은 빈 문자열", "예: OK boomer → 오케이 부머"),
    "ja": ("일본어", "일본", "reading에는 히라가나 읽기", "예: 推し活 → 오시카쓰"),
    "zh": ("중국어", "중국(중국 본토 인터넷)", "reading에는 병음(성조 표시)", "예: 躺平 → 탕핑"),
}

PROMPT = """오늘은 {today}입니다. 웹 검색을 사용해서 {where}에서 지금 SNS·커뮤니티·뉴스에서 실제로 쓰이는 {lang} 신조어 후보를 찾아 주세요. 한국어 신조어와는 관계없이, 그 언어권의 새로운 말이면 됩니다.

규칙
- 아래 '이미 소개한 단어'에 있는 단어는 고르지 마세요.
- 최근 몇 년 안에 퍼진 말을 우선하고, 신뢰할 만한 출처(뉴스, 사전, 위키백과 등)에서 뜻이 확인되는 단어만 고르세요.
- 비속어·혐오 표현·특정 집단 비하 표현은 고르지 마세요.
- 출처(source) 주소는 웹 검색 결과에 실제로 나온 페이지 주소만 쓰세요. 주소를 짐작하거나 지어내면 그 항목은 버려집니다.
- 출처 문장을 그대로 베끼지 말고 직접 쓴 한국어 문장으로 설명하세요.
- pron_ko는 원어 발음을 한글로 적은 것입니다. 반드시 한글로만 쓰세요. {pron_hint}
- {reading_hint}
- example은 직접 지어낸 짧은 원어 문장 한 줄, example_ko는 그 한국어 번역입니다.
- 뜻이 출처마다 갈리면 note에 그 사실을 적으세요.
{past_rules}
{opp_rules}
- past·반대어 항목에도 pron_ko(한글 발음)를 반드시 넣고, reading은 위와 같은 규칙으로 쓰세요.

이미 소개한 단어: {known}

마지막에는 설명 없이, 아래 형식의 JSON 객체 하나만 출력하세요.
{{"word": "원어 표기", "reading": "...", "pron_ko": "한글 발음", "meaning": "한국어 한두 문장", "origin": "한국어 한두 문장", "example": "원어 예문", "example_ko": "한국어 번역", "category": "줄임말|감탄사|SNS 표현|사람 유형|사회 현상|커뮤니티 중 하나", "note": "없으면 빈 문자열", "source": "https://로 시작하는 출처 주소", "past": [{{"years": 10, "word": "원어", "reading": "...", "pron_ko": "한글 발음", "meaning": "한국어 한 문장", "source": "https://..."}}], "related": [{{"word": "원어", "reading": "...", "pron_ko": "한글 발음", "meaning": "한국어 한 문장", "source": "https://..."}}]}}
"""


def load():
    if PATH.exists():
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"words": []}


def extract_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("응답에서 JSON을 찾지 못했습니다:\n" + text[:500])
    return json.loads(m.group(0))


def clean(entry, lang, allowed=None):
    need = ["word", "pron_ko", "meaning", "origin", "example", "example_ko", "source"]
    for k in need:
        if not str(entry.get(k, "")).strip():
            raise ValueError(f"'{k}' 항목이 비어 있습니다: {entry}")
    out = {"lang": lang}
    out.update({k: str(entry[k]).strip() for k in need})
    if not re.search(r"[가-힣]", out["pron_ko"]):
        raise ValueError(f"pron_ko가 한글이 아닙니다: {out['pron_ko']}")
    if not sc.source_ok(out["source"], allowed):
        raise ValueError("출처가 없거나, 실제 검색에서 확인되지 않은 주소입니다: " + out["source"])
    for k in ("reading", "category", "note"):
        v = str(entry.get(k, "")).strip()
        if v and v != out["word"]:
            out[k] = v
    past = sc.clean_past(entry.get("past"), True, allowed)
    if past:
        out["past"] = past
    opp = sc.clean_opposites(entry.get("related"), True, allowed)
    if opp:
        out["related"] = opp
    out["enriched"] = True  # 생성할 때 이미 함께 찾았으므로 나중에 다시 채우지 않습니다
    return out


def generate_one(client, lang, today, words):
    name, where, reading_hint, pron_hint = LANGS[lang]
    known = ", ".join(w["word"] for w in words if w.get("lang") == lang) or "(없음)"
    last = None
    for attempt in range(1, 4):
        print(f"  [{attempt}/3] {name} 신조어를 검색하는 중… (1~2분 걸릴 수 있어요)")
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=4500,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 8}],
                messages=[{"role": "user", "content": PROMPT.format(
                    today=today, where=where, lang=name, known=known, reading_hint=reading_hint, pron_hint=pron_hint,
                    past_rules=sc.past_rules(name, int(today[:4])), opp_rules=sc.opposite_rules(name))}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            entry = clean(extract_json(text), lang, sc.search_urls(resp))
            if entry["word"] in {w["word"] for w in words if w.get("lang") == lang}:
                raise ValueError(f"이미 있는 단어가 다시 나왔습니다: {entry['word']}")
            entry["date"] = today
            return entry
        except Exception as e:
            last = e
            print("    실패:", e)
    raise RuntimeError(f"3번 시도했지만 실패했습니다. 마지막 오류: {last}")


def save(data, words, today):
    data["words"] = words
    data["updated"] = today
    tmp = PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(PATH)


def main():
    ap = argparse.ArgumentParser(description="오늘의 외국어 신조어를 foreign.json에 추가합니다.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="오늘 단어가 이미 있어도 지우고 새로 만듦")
    ap.add_argument("--no-enrich", action="store_true", help="기존 단어의 10·20·30년 전 유사어·반대어 채우기를 건너뜀")
    ap.add_argument("--enrich-limit", type=int, default=4, help="이번에 채워 줄 기존 단어 수 (기본 4, 최근 날짜 단어부터)")
    ap.add_argument("--lang", choices=list(LANGS), action="append", help="만들 언어 (여러 번 지정 가능, 기본: 전부)")
    args = ap.parse_args()
    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY 환경변수가 설정되어 있지 않습니다.")

    today = datetime.now(KST).strftime("%Y-%m-%d")
    data = load()
    words = data.get("words", [])
    langs = args.lang or list(LANGS)
    if args.force:
        words = [w for w in words if not (w.get("date") == today and w.get("lang") in langs)]
    client = anthropic.Anthropic()
    made = failed = 0
    for lang in langs:
        if any(w.get("date") == today and w.get("lang") == lang for w in words):
            print(f"[{LANGS[lang][0]}] 오늘({today}) 단어가 이미 있습니다. (다시 만들려면 --force)")
            continue
        print(f"\n[{LANGS[lang][0]}]")
        try:
            entry = generate_one(client, lang, today, words)
        except RuntimeError as e:
            print(e)
            failed += 1
            continue  # 한 언어가 실패해도 나머지는 계속합니다
        words.append(entry)
        made += 1
        print("선정된 단어:")
        print(json.dumps(entry, ensure_ascii=False, indent=2))
        if not args.dry_run:
            save(data, words, today)
            print(f"foreign.json에 저장했습니다. (총 {len(words)}개)")
    if not args.no_enrich:
        print("\n[10·20·30년 전 유사어·반대어 채우기]")
        n = sc.enrich(client, MODEL, words, lambda w: LANGS[w["lang"]][0], True, args.enrich_limit, int(today[:4]))
        if n and not args.dry_run:
            save(data, words, today)
            print(f"{n}개 단어를 확인했고 foreign.json에 저장했습니다.")
    if args.dry_run:
        print("\n(--dry-run) foreign.json은 수정하지 않았습니다.")
    if made == 0 and failed:
        sys.exit("새 단어를 만들지 못했습니다.")


if __name__ == "__main__":
    main()
