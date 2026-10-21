from biraflow import textproc as tp
from biraflow.textproc import Replacement, Snippet


def test_spoken_punctuation_and_tidy():
    assert tp.local_rules("olá vírgula tudo bem ponto de interrogação") == "Olá, tudo bem?"
    assert tp.local_rules("primeira linha. Nova linha. Segunda linha") == "Primeira linha.\nSegunda linha"
    assert tp.local_rules("fim ponto final novo parágrafo começo") == "Fim.\n\nComeço"
    assert tp.local_rules("ele disse abre aspas oi fecha aspas") == "Ele disse “oi”"
    assert tp.local_rules("fiz dois pontos no jogo") == "Fiz dois pontos no jogo"   # not a command


def test_fillers_and_stutters_keep_real_words():
    assert tp.local_rules("Então, é, eu eu queria hã marcar uma reunião") == "Então, eu queria marcar uma reunião"
    assert tp.local_rules("quero um café, é isso") == "Quero um café, é isso"   # "um" and verb "é" stay
    assert tp.local_rules("não não, que que é isso") == "Não não, que que é isso"
    assert tp.local_rules("uh I think hmm we should") == "I think we should"


def test_send_command():
    assert tp.split_send_command("Bom dia a todos. Pressione enter.", ["pressione enter"]) == ("Bom dia a todos.", True)
    assert tp.split_send_command("vou enviar amanhã", ["pressione enter"]) == ("vou enviar amanhã", False)
    assert tp.split_send_command("ok, press enter", ["press enter"]) == ("ok", True)


def test_snippets_inline_and_alone():
    snippets = [Snippet("meu email", "bira@exemplo.com"), Snippet("assinatura", "Abraços,\nBira")]
    p = tp.prepare("Meu e-mail é meu email.", local=True, spoken=True, send_phrases=[], snippets=snippets)
    # "e-mail" with a hyphen also matches the trigger
    assert p.tokens and "⟦S1⟧" in p.text and not p.snippet_only
    assert tp.finish(p.text, p, []) == "⟦S1⟧ é ⟦S1⟧.".replace("⟦S1⟧", "bira@exemplo.com")

    p = tp.prepare("Assinatura.", local=True, spoken=True, send_phrases=[], snippets=snippets)
    assert p.snippet_only
    assert tp.finish("whatever the LLM said", p, []) == "Abraços,\nBira"


def test_lost_token_falls_back_to_local_text():
    p = tp.prepare("me manda no meu email", local=True, spoken=True, send_phrases=[],
                   snippets=[Snippet("meu email", "bira@exemplo.com")])
    assert tp.finish("Me manda no seu e-mail.", p, []) == "Me manda no bira@exemplo.com"


def test_replacements_case_and_sentence_start():
    rules = [Replacement("ese", "esse"), Replacement("github", "GitHub"), Replacement("cloud code", "Claude Code")]
    assert tp.apply_replacements("Ese projeto no github. ese aqui", rules) == "Esse projeto no GitHub. Esse aqui"
    assert tp.apply_replacements("uso o cloud code e o Cloud-Code", rules) == "uso o Claude Code e o Claude Code"
    assert tp.apply_replacements("essencial", rules) == "essencial"           # whole words only


def test_style_tweaks():
    assert tp.style_tweaks("Tô chegando.", "very_casual") == "tô chegando"
    assert tp.style_tweaks("git status.", "technical") == "git status"
    assert tp.style_tweaks("Olá. Tudo bem.", "very_casual") == "Olá. Tudo bem."   # multi-sentence kept
    assert tp.style_tweaks("Prezado João.", "formal") == "Prezado João."
    # e-mail: the app adds the signature, a dictated one is dropped
    assert tp.style_tweaks("Oi Ana,\n\nSegue o contrato.\n\nObrigado!\n\nAtt,\nBira", "email") == \
        "Oi Ana,\n\nSegue o contrato.\n\nObrigado!"
    assert tp.style_tweaks("Fico no aguardo. Atenciosamente, Ana Paula", "email") == "Fico no aguardo."
    assert tp.style_tweaks("Obrigado, att Bira.", "email") == "Obrigado."
    assert tp.style_tweaks("Ele respondeu atenciosamente a todos.", "email") == "Ele respondeu atenciosamente a todos."
    assert tp.style_tweaks("Obrigado!\n\nAtt,\nBira", "formal") == "Obrigado!\n\nAtt,\nBira"


def test_learn_replacements_from_correction():
    rules = tp.learn_replacements("Ese projeto usa o cloud code no github.",
                                  "Esse projeto usa o Claude Code no GitHub.")
    got = {(r.heard.lower(), r.write) for r in rules}
    assert ("ese", "esse") in got           # sentence-start capital is not part of the rule
    assert ("cloud code", "Claude Code") in got
    assert ("github", "GitHub") in got
    assert tp.learn_replacements("Tenho 3 itens", "Tenho 4 itens") == []      # numbers are not rules
    # a long shared ending does not make two different addresses look alike
    assert tp.learn_replacements("Mande para resende ou @empresa hoje", "Mande para taspio@empresa hoje") == []
    got = {(r.heard, r.write) for r in tp.learn_replacements("Falei com a Kate e o cloud", "Falei com a Kayth e o Claude")}
    assert got == {("Kate", "Kayth"), ("cloud", "Claude")}
    assert tp.learn_replacements("Até as 10h", "Até as 15h") == []
    assert [(r.heard, r.write) for r in tp.learn_replacements("o mp3", "o MP3")] == [("mp3", "MP3")]


def test_pt_br_review_regressions():
    # a filler before the period keeps the period (the sentences stay apart)
    assert tp.local_rules("Fui ao mercado hum. Depois voltei.") == "Fui ao mercado. Depois voltei."
    assert tp.local_rules("Eu acho que sim, hum.") == "Eu acho que sim."
    assert tp.local_rules("Hmm? Sério?") == "Sério?"
    assert tp.local_rules("A inflação foi de 4 vírgula 5 por cento.") == "A inflação foi de 4,5 por cento."
    assert tp.local_rules("Já já te ligo.") == "Já já te ligo."
    assert tp.local_rules("Logo logo chega.") == "Logo logo chega."
    # quotes keep the comma before and the period after them
    assert tp.local_rules("Ele disse, abre aspas oi fecha aspas.") == "Ele disse, “oi”."
    # an ambiguous mark right before the send phrase still counts
    p = tp.prepare("Tudo certo ponto final pressione enter", local=True, spoken=True,
                   send_phrases=["pressione enter"], snippets=[])
    assert p.send and p.text == "Tudo certo."
    # a rule never rewrites a snippet token
    p = tp.prepare("Me manda o meu endereço.", local=True, spoken=True, send_phrases=[],
                   snippets=[Snippet("meu endereço", "Rua X")])
    assert tp.finish(p.text, p, [Replacement("s1", "xx")]) == "Me manda o Rua X."


def test_prepare_finish_end_to_end_with_send():
    p = tp.prepare("Bom dia vírgula pessoal pressione enter", local=True, spoken=True,
                   send_phrases=["pressione enter"], snippets=[])
    assert p.send and p.text == "Bom dia, pessoal"
    assert tp.finish("Bom dia, pessoal!", p, [Replacement("pessoal", "time")]) == "Bom dia, time!"


def test_local_rules_never_break_urls_paths_numbers_or_code():
    keep = [
        "github.com/bira", "https://exemplo.com.br", "bira@exemplo.com.br", "Node.js",
        r"C:\Users\Bira\notas.txt", "10:30", "3.5.1", "3,5", "npm run dev:watch", "usar o .NET",
    ]
    for s in keep:
        out = tp.local_rules(f"veja {s} agora")
        assert s in out, (s, out)
    assert tp.local_rules("a sequência é 1, 1, 2, 3, 5") == "A sequência é 1, 1, 2, 3, 5"
    assert tp.local_rules("fim, ponto final. começo") == "Fim. Começo"


def test_review_regressions():
    # ambiguous commands only at a pause / end
    assert tp.local_rules("Lançamos a nova linha de produtos") == "Lançamos a nova linha de produtos"
    assert tp.local_rules("o ponto final da história") == "O ponto final da história"
    assert tp.local_rules("primeira linha, nova linha, segunda linha") == "Primeira linha\nSegunda linha"
    assert tp.local_rules("acabou ponto final") == "Acabou."
    # fillers without stray punctuation; hyphenated words kept
    assert tp.local_rules("ahn? sério") == "Sério"
    assert tp.local_rules("uh-oh, deu ruim") == "Uh-oh, deu ruim"
    # legit doubled words and leading dots
    assert "that that" in tp.local_rules("I know that that is true")
    assert tp.local_rules(".env file") == ".env file"
    # content edits are not learned; spelling fixes are
    assert tp.learn_replacements("Ele disse não.", "Ele disse sim.") == []
    assert [(r.heard, r.write) for r in tp.learn_replacements("o ese", "o esse")] == [("ese", "esse")]
    # replacements do not rewrite snippet text
    p = tp.prepare("manda pro meu email", local=True, spoken=True, send_phrases=[],
                   snippets=[Snippet("meu email", "bira@gmail.com")])
    rules = [Replacement("gmail", "Gmail"), Replacement("bira", "Bira Peres")]
    assert tp.finish(p.text, p, rules) == "Manda pro bira@gmail.com"



def _finish(raw, snippets=(), now=None):
    p = tp.prepare(raw, local=True, spoken=True, send_phrases=["pressione enter"], snippets=list(snippets))
    return tp.finish(p.text, p, [])


def test_spoken_email():
    assert _finish("meu email é joão arroba gmail ponto com, me manda") == "Meu email é joao@gmail.com, me manda"
    assert _finish("manda para joao.silva arroba empresa.com.br ponto final depois") == \
        "Manda para joao.silva@empresa.com.br. Depois"
    assert _finish("o arroba é um símbolo") == "O arroba é um símbolo"


def test_more_spoken_punctuation():
    assert _finish("ele disse reticências, e saiu") == "Ele disse... e saiu"     # no capital after "..."
    assert _finish("a resposta travessão sim") == "A resposta — sim"
    assert _finish("abre colchetes nota fecha colchetes") == "[nota]"


def test_spoken_emoji():
    assert _finish("manda o arquivo emoji de coração") == "Manda o arquivo ❤️"
    assert _finish("emoji joinha") == "👍"
    p = tp.prepare("valeu emoji risada pressione enter", local=True, spoken=True,
                   send_phrases=["pressione enter"], snippets=[])
    assert tp.finish(p.text, p, []) == "Valeu 😂" and p.send


def test_snippet_placeholders():
    import datetime as dt

    now = dt.datetime(2026, 10, 15, 14, 30)
    assert tp.render_snippet("Bira — {data_extenso}, {dia_semana} {hora} {x}", now) == \
        "Bira — 15 de outubro de 2026, quinta-feira 14:30 {x}"
    assert tp.render_snippet("{data} até {amanha}", now) == "15/10/2026 até 16/10/2026"


def test_cancel_phrase():
    assert tp.wants_cancel("isso aqui, cancelar ditado.")
    assert tp.wants_cancel("Cancela o ditado")
    assert not tp.wants_cancel("vou cancelar o ditado de amanhã com a turma")
    assert not tp.wants_cancel("cancelar a reunião")


def test_adapt_to_context():
    is_word = {"arquivo": True, "joão": False}.get
    assert tp.adapt_to_context("Arquivo amanhã", "Vou levar o ", is_word=is_word) == "arquivo amanhã"
    assert tp.adapt_to_context("Arquivo amanhã", "Vou levar o", is_word=is_word) == " arquivo amanhã"
    assert tp.adapt_to_context("João chegou", "e ", is_word=is_word) == "João chegou"        # a name
    assert tp.adapt_to_context("GitHub caiu", "o ", {"GitHub"}) == "GitHub caiu"
    assert tp.adapt_to_context("Novo dia", "Fim. ") == "Novo dia"                             # new sentence
    assert tp.adapt_to_context("Novo dia", "Fim.") == " Novo dia"
    assert tp.adapt_to_context("Texto", None) == "Texto"                                      # unknown
    assert tp.adapt_to_context("Texto", "") == "Texto"                                        # empty field
    assert tp.adapt_to_context("Texto", "(") == "Texto"
    assert tp.adapt_to_context("I think", "and ") == "I think"



def test_undo_phrase():
    assert tp.wants_undo("Apagar último ditado.")
    assert tp.wants_undo("apaga o último ditado")
    assert tp.wants_undo("undo last dictation")
    assert not tp.wants_undo("vou apagar o último ditado amanhã")


def test_clipboard_placeholder(monkeypatch):
    monkeypatch.setattr(tp, "clipboard_text", lambda: "https://exemplo.com")
    assert tp.render_snippet("Segue o link: {area_de_transferencia}") == "Segue o link: https://exemplo.com"
