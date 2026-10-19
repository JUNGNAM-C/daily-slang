#!/usr/bin/env python3
"""Gemini 검색 연결(구글 검색)이 어떤 조건에서 켜지는지 점검하는 진단 스크립트.
파일은 건드리지 않고, 결과를 Actions 화면 상단의 '알림(notice)' 주석으로만 보여 줍니다."""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gemini_client import _Messages  # noqa: E402

KEY = os.environ.get("GEMINI_API_KEY")
if not KEY:
    print("::error title=probe::GEMINI_API_KEY가 없습니다")
    sys.exit(1)

P_KO = "구글 검색으로 2026년 요즘 영어권에서 유행하는 신조어 3개를 찾아서 뜻과 출처 사이트를 알려 주세요."
P_EN = "Search the web for 3 English slang terms that are trending in 2026 and tell me their meanings and the source sites."
P_JSON = ("[필수] 답하기 전에 반드시 구글 검색 도구를 먼저 사용해 최소 3번 검색하세요.\n"
          "2026년 요즘 영어권에서 쓰이는 신조어 1개를 골라 JSON 객체 하나로만 답하세요: "
          '{"word": "...", "meaning": "한국어 뜻", "source": "사이트 도메인"}')

CASES = [
    ("gemini-3.8-flash", "한국어 질문", P_KO, True),
    ("gemini-3.8-flash", "한국어 질문", P_KO, False),
    ("gemini-3.8-flash", "영어 질문", P_EN, False),
    ("gemini-3.8-flash", "JSON 질문", P_JSON, False),
    ("gemini-3.8-flash", "JSON 질문", P_JSON, True),
    ("gemini-3.6-flash", "한국어 질문", P_KO, False),
    ("gemini-3.6-flash", "JSON 질문", P_JSON, False),
    ("gemini-3.5-flash-lite", "한국어 질문", P_KO, False),
    ("gemini-3.1-flash-lite", "한국어 질문", P_KO, False),
]

m = _Messages(KEY)
for model, name, prompt, think in CASES:
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "tools": [{"google_search": {}}],
            "generationConfig": {"maxOutputTokens": 8000, "temperature": 0.4}}
    if think:
        body["generationConfig"]["thinkingConfig"] = {"thinkingBudget": 1024}
    tag = f"{model} | {name} | 생각설정 {'있음' if think else '없음'}"
    try:
        d = m._post(model, body)
        c = (d.get("candidates") or [{}])[0]
        gm = c.get("groundingMetadata") or {}
        n = len(gm.get("groundingChunks") or [])
        q = gm.get("webSearchQueries") or []
        txt = "".join(p.get("text", "") for p in (c.get("content") or {}).get("parts", []))
        print(f"::notice title=probe::{tag} → 검색결과 {n}건, 검색어 {len(q)}개 {q[:2]}, 종료 {c.get('finishReason')}, 글자 {len(txt)}")
    except Exception as e:
        print(f"::notice title=probe::{tag} → 오류: {str(e).replace(chr(10), ' ')[:220]}")
