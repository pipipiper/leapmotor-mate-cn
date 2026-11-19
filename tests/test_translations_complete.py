"""Every language must carry every string, on every surface that has strings.

This exists because nothing was checking. Two separate holes had been open for a while and neither
produced an error anywhere:

  * de.json was 80 strings short and pl.json 91, so German and Polish users read parts of the app
    in English — including a beta tester who sends us data in German every day.

It was invisible: a missing key isn't a crash, it's a gap. The only thing that finds a gap is
something that counts. Hence this file.
"""
import json
import pathlib
import re

import pytest

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"
LOCALES = WEB / "locales"
LANGS = ("en", "it", "fr", "de", "pl", "pt-PT", "nl", "es", "cs", "zh-TW", "zh-CN")


def _flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(_flat(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out


def _locale(lang):
    return _flat(json.loads((LOCALES / f"{lang}.json").read_text()))


@pytest.mark.parametrize("lang", [l for l in LANGS if l != "en"])
def test_locale_has_every_string(lang):
    en, other = _locale("en"), _locale(lang)
    missing = sorted(k for k in en if k not in other)
    assert not missing, f"{lang}.json is missing {len(missing)}: {missing[:10]}"


@pytest.mark.parametrize("lang", [l for l in LANGS if l != "en"])
def test_locale_has_no_stray_strings(lang):
    """A key that exists only in one language is a rename that half-landed."""
    en, other = _locale("en"), _locale(lang)
    stray = sorted(k for k in other if k not in en)
    assert not stray, f"{lang}.json has {len(stray)} keys English doesn't: {stray[:10]}"


def test_every_language_has_a_file():
    for lang in LANGS:
        assert (LOCALES / f"{lang}.json").is_file(), f"no locale file for {lang}"


@pytest.mark.parametrize("lang", [l for l in LANGS if l != "en"])
def test_the_maintenance_page_has_every_string_too(lang):
    """A THIRD copy, found while adding Spanish: `maintenance.py` keeps its own per-language dicts
    and falls back to English key by key, so a gap there is silent in exactly the same way — the
    Maintenance page half in your language and half in English, and nothing failing anywhere."""
    import maintenance
    for name in ("_LABELS", "_CATS", "_CHROME", "_P"):
        d = getattr(maintenance, name)
        if name == "_P":                       # {lang: {fragment: text}}, not {key: {lang: text}}
            assert lang in d, f"maintenance._P has no {lang} block"
            missing = sorted(k for k in d["en"] if k not in d[lang])
        else:                                  # {key: {lang: text}}
            missing = sorted(k for k, v in d.items() if not v.get(lang))
        assert not missing, f"maintenance.{name} is missing {len(missing)} for {lang}: {missing[:8]}"


def test_no_page_title_is_hardcoded_english():
    """The browser tab is a surface too, and it was the last one still in English.

    Found by reading the SERVED pages of a Spanish install rather than the templates: Trips,
    Charges, Statistics, Commands and Settings printed their English title in all seven
    languages, and had done since those pages existed. Nothing failed — a hard-coded string
    is not a missing key, so no completeness check could ever see it."""
    allowed = {"LeapMotor Mate", "Wallbox"}          # the product name and a word that is the same everywhere
    for tpl in sorted((WEB / "templates").glob("*.html")):
        for block in re.findall(r"\{%\s*block title\s*%\}(.*?)\{%\s*endblock", tpl.read_text()):
            words = re.sub(r"\{\{.*?\}\}", "", block)     # drop what Jinja will fill in
            for token in allowed:
                words = words.replace(token, "")
            assert not re.search(r"[A-Za-z]{2}", words), \
                f"{tpl.name} hardcodes {block.strip()!r} in its <title> — every language reads it"


def test_the_allowed_language_list_agrees_with_the_settings_menu():
    """The China build offers exactly the languages main.py accepts. A value the page offers but the
    server refuses would silently reset a reader's choice on the next save."""
    text = (WEB / "main.py").read_text()
    lists = re.findall(r'lang if lang in \(([^)]*)\)', text)
    assert lists, "the language tuple moved — this test is now watching nothing"
    parsed = [tuple(re.findall(r'"([\w-]+)"', l)) for l in lists]
    assert len(set(parsed)) == 1, f"main.py disagrees with itself about languages: {set(parsed)}"
    offered = set(parsed[0])
    assert offered == {"zh-CN", "zh-TW", "en"}, f"main.py accepts {offered}"
    settings = (WEB / "templates" / "settings.html").read_text()
    for lang in offered:
        assert f'<option value="{lang}"' in settings, f"Settings does not offer {lang}"


@pytest.mark.parametrize('lang', LANGS)
def test_locale_strings_are_inside_the_runtime_translation_dictionary(lang):
    data = json.loads((LOCALES / f'{lang}.json').read_text())
    assert set(data) == {'translations', 'months', 'weekdays'}, (
        f'{lang}: root-level strings are silently ignored by i18n.get_t')


@pytest.mark.parametrize('lang', LANGS)
def test_cn_charge_strings_resolve_through_the_real_translator(lang):
    from i18n import get_t
    t = get_t(lang)
    for key in ('cn_charge_sync_btn', 'cn_charge_syncing', 'cn_charge_stats_title',
                'cn_charge_total_kwh', 'cn_charge_total_count', 'cn_charge_fast_pct',
                'cn_charge_detail_title', 'cn_charge_no_data', 'gps_unavailable',
                'soc_unavailable'):
        assert t(key) != key, (lang, key)
    if lang == 'zh-CN':
        assert t('cn_charge_stats_title') == '充电统计'
        assert t('cn_charge_sync_btn') == '同步官方充电记录'
