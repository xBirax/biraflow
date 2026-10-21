"""Fix the spoken words of the catalogs by hand.

The model translates well, but a few words are ambiguous and have to be the ones a
person would actually say ("ponto" is "dot" in an e-mail, "period" is a full stop).

    python scripts/fix_spoken.py en es fr de it zh-Hans ja ko
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from biraflow import i18n  # noqa: E402

# code -> {key: words}
WORDS: dict[str, dict[str, list[str]]] = {
    "en": {
        "at": ["at sign"],
        "dot": ["dot"],
        "dot_final": ["full stop"],
        "emoji_of": [""],
        "send": ["press enter", "hit enter"],
        "cancel": ["cancel dictation", "cancel the dictation"],
        "undo": ["delete last dictation", "undo last dictation"],
        "sign_off": ["regards", "best regards", "kind regards", "sincerely", "cheers", "best"],
    },
    "es": {
        "at": ["arroba"],
        "dot": ["punto"],
        "dot_final": ["punto final"],
        "send": ["pulsa enter", "presiona enter", "envía enter"],
        "cancel": ["cancela el dictado", "cancelar dictado"],
        "undo": ["borra el último dictado", "deshaz el último dictado"],
        "sign_off": ["saludos", "un abrazo", "atentamente", "apreciado"],
    },
    "fr": {
        "at": ["arobase", "at"],
        "dot": ["point"],
        "dot_final": ["point final"],
        "send": ["appuie sur entrée", "appuie sur entree", "envoie entrée"],
        "cancel": ["annule la dictée", "annuler la dictée"],
        "undo": ["supprime la dernière dictée", "annule la dernière dictée"],
        "sign_off": ["cordialement", "salutations", "bien à vous", "merci"],
    },
    "de": {
        "at": ["at zeichen", "at"],
        "dot": ["punkt"],
        "dot_final": ["punkt am ende"],
        "send": ["drück enter", "druecke enter", "eingabetaste"],
        "cancel": ["diktat abbrechen", "abbrechen diktat"],
        "undo": ["letztes diktat löschen", "letzte diktat löschen"],
        "sign_off": ["grüße", "viele grüße", "freundliche grüße", "mit freundlichen grüßen"],
    },
    "it": {
        "at": ["chiocca"],
        "dot": ["punto"],
        "dot_final": ["punto finale"],
        "send": ["premi invio", "premi enter", "invio"],
        "cancel": ["annulla la dettatura", "annullare dettatura"],
        "undo": ["cancella l'ultima dettatura", "annulla l'ultima dettatura"],
        "sign_off": ["a presto", "cordiali saluti", "saluti", "grazie"],
    },
    "zh-Hans": {
        "at": ["艾特", "小老鼠"],
        "dot": ["点", "小数点"],
        "dot_final": ["句号"],
        "send": ["按回车", "回车"],
        "cancel": ["取消听写", "取消录音"],
        "undo": ["删除上次听写", "撤销上次听写"],
        "sign_off": ["此致敬礼", "祝好", "谢谢"],
    },
    "ja": {
        "at": ["アットマーク"],
        "dot": ["ドット"],
        "dot_final": ["ピリオド"],
        "send": ["エンター", "改行して"],
        "cancel": ["記録をキャンセル", "入力を取り消す"],
        "undo": ["前の記録を削除", "直前の入力を削除"],
        "sign_off": ["敬具", "よろしくお願いします"],
    },
    "ko": {
        "at": ["앗 사인", "골뱅이"],
        "dot": ["도트"],
        "dot_final": ["마침표"],
        "send": ["엔터", "엔터키"],
        "cancel": ["녹취 취소", "취소"],
        "undo": ["마지막 녹취 삭제", "이전 기록 삭제"],
        "sign_off": ["감사합니다", "잘 부탁드립니다"],
    },
}


def main() -> int:
    codes = sys.argv[1:] or list(WORDS)
    for code in codes:
        path = i18n.LANG_DIR / f"{code}.json"
        if not path.exists():
            print(f"{code}: ainda sem catálogo")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        spoken = data.setdefault("spoken", {})
        for key, words in WORDS.get(code, {}).items():
            spoken[key] = words
        with path.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1, sort_keys=True)
            fh.write("\n")
        print(f"{code}: {len(spoken)} falas ajustadas")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
