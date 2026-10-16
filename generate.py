#!/usr/bin/env python3
"""
오늘의 신조어 생성 스크립트

하는 일
  1. 웹 검색으로 요즘 쓰이는 한국어 신조어를 찾고,
  2. words.json에 아직 없는 단어 1개를 골라 뜻·유래·예문을 정리한 뒤,
  3. 오늘 날짜(한국 시간)를 붙여 words.json 맨 끝에 추가합니다.

필요한 것
  - Python 3.9 이상
  - pip install anthropic
  - 환경변수 ANTHROPIC_API_KEY (Anthropic Console에서 발급)

사용법
  python generate.py            # 오늘 단어가 없으면 새로 만들어 추가
  python generate.py --dry-run  # 파일은 건드리지 않고 결과만 화면에 출력
  python generate.py --force    # 오늘 단어가 이미 있어도 새로 만들어 교체
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

HERE = Path(__file__).resolve().parent
WORDS_PATH = HERE / "words.json"
MODEL = os.environ.get("SLANG_MODEL", "claude-sonnet-5-5")
KST = ZoneInfo("Asia/Seoul")

PROMPT = """오늘은 {today}입니다. 웹 검색을 사용해서 지금 한국의 SNS·커뮤니티·뉴스에서 실제로 쓰이는 '새로운 신조어' 후보를 찾아 주세요.

규칙
- 아래 '이미 소개한 단어'에 있는 단어는 고르지 마세요.
- 최근 몇 달 안에 퍼진 말을 우선하고, 오래된 유행어(예: 갓생, 중꺾마)는 피하세요.
- 신뢰할 만한 출처(뉴스, 국립국어원, 나무위키 등)에서 뜻이 확인되는 단어만 고르세요.
- 비속어·혐오 표현·특정 집단 비하 표현은 고르지 마세요.
- 출처 문장을 그대로 베끼지 말고 직접 쓴 문장으로 설명하세요.
- 뜻이 출처마다 갈리면 note에 그 사실을 적으세요.
- example은 직접 지어낸 짧은 대화체 문장 한 줄로 쓰세요.
- 선택한 단어와 관련된 단어도 최대 3개 찾아 related에 넣으세요. 종류는 '반대 뜻'(반대말), '비슷한 뜻'(같은 뜻의 다른 말), '비슷한 구성'(같은 방식으로 만들어진 말) 중 하나입니다.
- related 항목은 출처에서 실제로 확인된 것만 넣으세요. 확인되지 않으면 빈 배열 []로 두세요. 억지로 채우지 마세요.
- related의 각 항목에도 직접 쓴 한 문장 뜻풀이(meaning)와 https://로 시작하는 출처(source)를 넣으세요.

이미 소개한 단어: {known}

마지막에는 설명 없이, 아래 형식의 JSON 객체 하나만 출력하세요.
{{"word": "...", "meaning": "한두 문장", "example": "예문 한 줄", "origin": "유래 한두 문장", "category": "줄임말|감탄사|감성 리액션|SNS 표현|사람 유형|커뮤니티 중 하나", "note": "없으면 빈 문자열", "source": "https://로 시작하는 출처 주소", "related": [{{"type": "반대 뜻|비슷한 뜻|비슷한 구성", "word": "...", "meaning": "한 문장", "source": "https://..."}}]}}
"""


def load_words():
    with open(WORDS_PATH, encoding="utf-8") as f:
        return json.load(f)


def extract_json(text):
    """응답 글에서 JSON 객체 하나를 꺼냅니다."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("응답에서 JSON을 찾지 못했습니다:\n" + text[:500])
    return json.loads(m.group(0))


def clean(entry):
    need = ["word", "meaning", "example", "origin", "category", "source"]
    for k in need:
        if not str(entry.get(k, "")).strip():
            raise ValueError(f"'{k}' 항목이 비어 있습니다: {entry}")
    out = {k: str(entry[k]).strip() for k in need}
    note = str(entry.get("note", "")).strip()
    if note:
        out["note"] = note
    if not out["source"].startswith("https://"):
        out["source"] = ""
    allowed = {"반대 뜻", "비슷한 뜻", "비슷한 구성"}
    related = []
    for r in entry.get("related") or []:
        if not isinstance(r, dict):
            continue
        word = str(r.get("word", "")).strip()
        meaning = str(r.get("meaning", "")).strip()
        src = str(r.get("source", "")).strip()
        rtype = str(r.get("type", "")).strip()
        if not word or not meaning or rtype not in allowed or not src.startswith("https://"):
            continue  # 불완전하거나 출처 없는 항목은 버립니다
        related.append({"type": rtype, "word": word, "meaning": meaning, "source": src})
    if related:
        out["related"] = related[:3]
    return out


PER_DAY = 2  # 하루에 만드는 단어 수 (앱의 B 버튼, A 버튼에 하나씩 연결됩니다)


def generate_one(client, today, words):
    """웹 검색으로 새 단어 1개를 만들어 돌려줍니다. 3번까지 다시 시도합니다."""
    known = ", ".join(w["word"] for w in words)
    last_err = None
    for attempt in range(1, 4):
        print(f"  [{attempt}/3] 신조어를 검색하는 중… (1~2분 걸릴 수 있어요)")
        try:
            resp = client.messages.create(
                model=MODEL,
                max_tokens=3000,
                tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 5}],
                messages=[{"role": "user", "content": PROMPT.format(today=today, known=known)}],
            )
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
            entry = clean(extract_json(text))
            if entry["word"] in {w["word"] for w in words}:
                raise ValueError(f"이미 있는 단어가 다시 나왔습니다: {entry['word']}")
            entry["date"] = today
            return entry
        except Exception as e:  # 형식 오류나 중복이면 다시 시도
            last_err = e
            print("    실패:", e)
    raise RuntimeError(f"3번 시도했지만 실패했습니다. 마지막 오류: {last_err}")


def save(data, words, today):
    data["words"] = words
    data["updated"] = today
    tmp = WORDS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    tmp.replace(WORDS_PATH)


def main():
    ap = argparse.ArgumentParser(description="오늘의 신조어를 words.json에 추가합니다.")
    ap.add_argument("--dry-run", action="store_true", help="파일을 수정하지 않고 결과만 출력")
    ap.add_argument("--force", action="store_true", help="오늘 단어가 이미 있어도 지우고 새로 만듦")
    ap.add_argument("--count", type=int, default=PER_DAY, help=f"하루에 만들 단어 수 (기본 {PER_DAY})")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY 환경변수가 설정되어 있지 않습니다. README의 '2단계'를 따라 설정해 주세요.")

    today = datetime.now(KST).strftime("%Y-%m-%d")
    data = load_words()
    words = data.get("words", [])

    if args.force:
        words = [w for w in words if w.get("date") != today]
    have = [w for w in words if w.get("date") == today]
    need = args.count - len(have)
    if need <= 0:
        print(f"오늘({today}) 단어가 이미 {len(have)}개 있습니다: {', '.join(w['word'] for w in have)}  (다시 만들려면 --force)")
        return

    client = anthropic.Anthropic()
    made = 0
    for n in range(need):
        print(f"\n[{len(have) + n + 1}/{args.count}번째 단어]")
        try:
            entry = generate_one(client, today, words)
        except RuntimeError as e:
            print(e)
            break  # 이미 만든 단어는 그대로 저장하고 끝냅니다
        words.append(entry)
        made += 1
        print("\n선정된 단어:")
        print(json.dumps(entry, ensure_ascii=False, indent=2))
        if not args.dry_run:
            save(data, words, today)  # 하나 만들 때마다 바로 저장
            print(f"words.json에 저장했습니다. (총 {len(words)}개)")

    if args.dry_run:
        print("\n(--dry-run) words.json은 수정하지 않았습니다.")
    if made == 0:
        sys.exit("새 단어를 만들지 못했습니다.")


if __name__ == "__main__":
    main()
