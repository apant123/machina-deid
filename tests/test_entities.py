"""Stage 2: pseudonym shape guarantees and entity-resolution rules (synthetic fixtures only)."""
import os

os.environ.setdefault("DEID_SEED", "00" * 32)

from collections import Counter  # noqa: E402

from deid.entities import Builder, Ent, key  # noqa: E402
from deid.pseudonym import fake_token, same_widths  # noqa: E402


def test_fake_tokens_keep_lengths_in_every_encoding():
    for tok, cls in [("Vallemo", "surname"), ("Méndez", "surname"), ("Nicolás", "given"), ("徐子卓", "cjk_person"),
                     ("白鹤", "cjk"), ("Жмыхов", "surname"), ("Zürich", "word"), ("Kestrelix13", "word"), ("XKU", "acronym")]:
        f = fake_token(tok, cls)
        assert f != tok and same_widths(tok, f), (tok, f)


def test_fake_tokens_are_deterministic_and_case_shaped():
    assert fake_token("Talvenk", "surname") == fake_token("Talvenk", "surname")
    f = fake_token("TenHornedMoose", "word")
    assert [c.isupper() for c in f] == [c.isupper() for c in "TenHornedMoose"]


def _builder():
    b = Builder()
    b.short_names = []
    b.real_tokens = frozenset()
    return b


def test_person_variants_share_token_pseudonyms():
    b = _builder()
    b.add("Mira Talvenk", "person", "field", 10, "author")
    e = b.ents[key("Mira Talvenk")]
    v = dict(b.variants(e, {}))
    first, last = v["Mira Talvenk"].split(" ")
    assert v["Mira_Talvenk"] == f"{first}_{last}"
    assert v["Talvenk, Mira"] == f"{last}, {first}"
    assert v["Talvenk"] == last  # rare surname alone


def test_org_keeps_generic_words_and_email_keeps_tld():
    b = _builder()
    tm = {}
    org = Ent("x", Counter(org=1))
    out = b.render("Universidad Valdemoro IV de Alcantra", org, tm)
    assert out.startswith("Universidad ") and " de " in out and "Valdemoro" not in out and "Alcantra" not in out
    em = Ent("y", Counter(email=1))
    out = b.render("orvank@kzu.edu", em, tm)
    assert out.endswith(".edu") and "orvank" not in out and "kzu" not in out
    assert b.render("talvenkm@gmail.com", em, tm).endswith("@gmail.com")


def test_tool_names_kept_and_weak_tokens_pruned():
    b = _builder()
    b.add("SOLIDWORKS 2018 SP4.0", "org", "field")
    b.add("Open CASCADE", "org", "field")
    b.add("Toolbox", "account", "field")
    b.add("Infinity", "account", "regex", tag="profile")  # corroborated by a profile folder -> kept (field scope)
    b.add("Bearings", "account", "field")
    b.prune()
    assert key("Open CASCADE") not in b.ents and key("SOLIDWORKS") not in b.ents
    assert "infinity" in b.ents and "bearings" not in b.ents and "toolbox" not in b.ents
    row = b._row(b.ents["infinity"], "Infinity", "Xxxxxxxx", None)
    assert row["scope"] == "field"


def test_ner_span_acceptance():
    acc = Builder.accept_span
    from deid.entities import dictionary
    d = dictionary()
    assert acc("Lucien VARGANT", "person", 0.95, 3, d)[0]
    assert not acc("Hex Nut", "organization", 0.6, 9, d)[0]
    assert acc("Brightfield Research Pte Ltd", "company", 0.63, 1, d)[0]
    assert not acc("Bearing Housing", "person", 0.7, 2, d)[0]


def test_ner_rejects_code_tokens_titles_and_initials():
    acc = Builder.accept_span
    from deid.entities import dictionary
    d = dictionary()
    assert not acc("USART", "organization", 0.9, 5, d, "USART synchronous mode")[0]  # all-caps, NER only
    assert not acc("DMA_Channel", "organization", 0.9, 5, d, "DMA_Channel")[0]
    assert not acc("TIM Channel", "organization", 0.89, 5, d, "/** @defgroup TIM Channel TIM Channel")[0]
    assert not acc("D. S", "person", 0.9, 1, d, "Carter, A. D. S. (1950).")[0]
    assert not acc("WFei", "person", 0.9, 1, d, "WFei")[0]
    assert not acc("工程师", "person", 0.9, 1, d, "现场工程师")[0]  # job title, no surname
    assert acc("毛昱诚", "person", 0.9, 1, d, "梁家源 刘丰源 毛昱诚")[0]
    assert not acc("机械学院", "organization", 0.9, 1, d, "吉林大学 机械学院")[0]


def test_no_tiny_initial_variants_and_short_keys_are_path_scoped():
    b = _builder()
    b.add("Zelnik O", "person", "ner", 1, "x")
    v = dict(b.variants(b.ents[key("Zelnik O")], {}))
    assert "NO" not in v and "N. O" not in v
    b.add("rje", "account", "regex", tag="profile")
    assert b._row(b.ents["rje"], "rje", "gkd", None)["scope"] == "path"


def test_casefold_length_change_keeps_widths():
    b = _builder()
    tm = {}
    b.render("Wessels", Ent("a", Counter(person=1)), tm)
    out = b.render("Weßels", Ent("b", Counter(person=1)), tm)
    assert same_widths("Weßels", out)
