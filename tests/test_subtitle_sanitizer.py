"""
Unit tests for the text cleaner (pipeline step 2.1).

Covers the acceptance matrix of ``openspec/step2_Subtitles.md`` §8: every rule of
2.1.1/2.1.2 gets at least one "must be removed" and one "must survive" case,
because over-cleaning is the real risk here, not under-cleaning.
"""

from __future__ import annotations

import pytest

from app.subtitle_parser import SanitizerOptions, SubtitleSanitizer, clean_subtitle_text

pytestmark = pytest.mark.unit

# (raw cue text, expected cleaned text) — pairs from the acceptance matrix plus
# the edge cases found while writing the rules.
MATRIX = [
    # 1. HTML tags
    ("<i>Put it down, Walter.</i>", "Put it down, Walter."),
    ("<b>Hey</b> there", "Hey there"),
    ('<font color="#E5E5E5">Are you okay?</font>', "Are you okay?"),
    ("Line one<br />Line two", "Line one Line two"),
    # 2. SubStation Alpha overrides
    (r"{\an8}I am the one who knocks.{\pos(20, 200)}", "I am the one who knocks."),
    (r"{\b1}Bold claim{\b0}, isn't it?", "Bold claim, isn't it?"),
    (r"{\an8}You can't[bleep]be serious", "You can't be serious"),
    # 3. SDH cues
    ("[door creaks] Where is he?", "Where is he?"),
    ("[suspenseful music plays]", ""),
    ("[Upbeat music]", ""),
    ("<b>[MUSIC]</b>", ""),
    # 4. ALL-CAPS parentheses vs. ordinary speech
    ("(CHUCKLES) We can do it (together).", "We can do it (together)."),
    ("(SIGHS) Fine.", "Fine."),
    ("Don't (now!) touch that", "Don't (now!) touch that"),
    ("The year (1999) was strange", "The year (1999) was strange"),
    ("He said (NO!) and left", "He said (NO!) and left"),
    ("(USA) is not a sound effect", "(USA) is not a sound effect"),
    # 5. Speaker labels
    ("WALTER: Put it down.", "Put it down."),
    ("JESSE: Yo, Mr. White", "Yo, Mr. White"),
    ("MAN ON TV: Breaking news", "Breaking news"),
    ("OFFICER 1: Clear the area", "Clear the area"),
    ("DR. SMITH: The results are in", "The results are in"),
    ("COACH:\nPractice starts now.", "Practice starts now."),
    ("DON'T: Get out", "Get out"),
    ("www.site.com:8080 is down", "www.site.com:8080 is down"),
    ("Time: 5 minutes left", "Time: 5 minutes left"),
    # 6. Dialogue dashes; quotes are content and are never stripped
    ("- Hey.", "Hey."),
    ("\u2014Wake up.", "Wake up."),
    ("-Hey.\n-Wake up.", "Hey. Wake up."),
    (">> Come here", "Come here"),
    (">>>\tCopy.", "Copy."),
    ('"Come here."', '"Come here."'),
    ("Well - ", "Well"),
    # 7. Music. "#" is a marker only when it frames the line (D18).
    ("\u266a Fair is foul \u266a", ""),
    ("# Silent night #", ""),
    ("\u266a\u266b\u266c", ""),
    ('I said #13-37, not "hot".', 'I said #13-37, not "hot".'),
    ("This is #blessed territory", "This is #blessed territory"),
    ("#blessed #13-37 and #hashtag.", "blessed #13-37 and #hashtag."),
    ("Trailing hash #", "Trailing hash"),
    # 8. Multi-line cues are merged with a single space
    ("Hello   world \n\n second  line", "Hello world second line"),
    ('"Fire off.\nPull trigger."', '"Fire off. Pull trigger."'),
    (r"First line\Nsecond hard break.", "First line second hard break."),
    # 9. Noise-only cues disappear
    ("...", ""),
    ("-", ""),
    ("", ""),
    ("   ", ""),
    ("\u200b", ""),
    # 10. Unicode normalization / entities
    (
        "Caf\u00e9 \u2014 na\u00efve "
        "r\u00e9sum\u00e9 \u4f60\u597d \u041f\u0440\u0438\u0432\u0435\u0442",
        "Caf\u00e9 - na\u00efve "
        "r\u00e9sum\u00e9 \u4f60\u597d \u041f\u0440\u0438\u0432\u0435\u0442",
    ),
    ("&quot;Where are you going?&quot;", '"Where are you going?"'),
    ("It&apos;s &amp; it&apos;s fine", "It's & it's fine"),
    # ``&lt;``/``&gt;`` are deliberately absent from _ENTITY_MAP: decoding them first
    # would draw a real tag out of the text and make a second pass mandatory.
    ("Radio says &lt;unknown&gt;.", "Radio says &lt;unknown&gt;."),
    ("A\u00a0B", "A B"),
    ("\u2026and then", "...and then"),
    # 11. Ordinary speech must survive untouched
    ("I can't believe it.", "I can't believe it."),
    ("The rules are simple: don't get caught.", "The rules are simple: don't get caught."),
    ("2 + 2 = 4 and 3 - 1 = 2", "2 + 2 = 4 and 3 - 1 = 2"),
]


@pytest.mark.parametrize("raw,expected", MATRIX)
def test_matrix(raw: str, expected: str) -> None:
    assert clean_subtitle_text(raw) == expected


def test_both_documented_entry_points_agree() -> None:
    assert SubtitleSanitizer().clean("[music] Run!") == "Run!"
    assert SubtitleSanitizer().clean_subtitle_text("[music] Run!") == "Run!"


def test_empty_input_is_safe() -> None:
    assert clean_subtitle_text("") == ""
    assert clean_subtitle_text("\n\n") == ""


def test_no_control_characters_left() -> None:
    assert clean_subtitle_text("Hello\x00\x07 world\x1f") == "Hello world"


def test_no_double_spaces_after_merge() -> None:
    result = clean_subtitle_text("A  B\nC   D")
    assert "  " not in result
    assert result == "A B C D"


def test_speaker_label_can_be_disabled() -> None:
    sanitizer = SubtitleSanitizer(SanitizerOptions(strip_speaker_labels=False))
    assert sanitizer.clean("WALTER: Put it down.") == "WALTER: Put it down."


def test_dialogue_dash_can_be_disabled() -> None:
    sanitizer = SubtitleSanitizer(SanitizerOptions(strip_dialogue_dashes=False))
    assert sanitizer.clean("- Hey.") == "- Hey."


def test_music_line_can_be_kept_without_markers() -> None:
    sanitizer = SubtitleSanitizer(SanitizerOptions(drop_music_lines=False))
    assert sanitizer.clean("\u266a Ring of fire \u266a") == "Ring of fire"


def test_merge_lines_can_be_disabled() -> None:
    sanitizer = SubtitleSanitizer(SanitizerOptions(merge_lines=False))
    assert sanitizer.clean("- Hey.\n- Wake up.") == "Hey.\nWake up."


def test_options_are_immutable_and_copy_on_replace() -> None:
    options = SanitizerOptions()
    changed = options.replaced(merge_lines=False)
    assert options.merge_lines is True
    assert changed.merge_lines is False
    assert changed.drop_music_lines is True


def test_default_options_match_the_specification() -> None:
    options = SanitizerOptions()
    assert (options.merge_lines, options.drop_music_lines) == (True, True)
    assert (options.strip_speaker_labels, options.strip_dialogue_dashes) == (True, True)
