# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
from pocsynth.prompts import build_prompt, build_system_prompt
from pocsynth.textutil import (
    convert_html_to_markdown,
    sanitize_filename_part,
    strip_model_preamble,
)


class TestStripModelPreamble:
    def test_strips_fenced_html_block(self):
        text = '```html\n<p class="x">hi</p>\n```'
        assert strip_model_preamble(text) == '<p class="x">hi</p>'

    def test_preserves_quotes_and_attributes(self):
        text = '<p class="x" data-v="1">hi "there"</p>'
        assert strip_model_preamble(text) == text

    def test_strips_here_is_preamble(self):
        text = "Here is the converted HTML:\n<p>content</p>"
        assert strip_model_preamble(text) == "<p>content</p>"

    def test_noop_when_no_preamble(self):
        text = "<h1>Title</h1>\n<p>body</p>"
        assert strip_model_preamble(text) == text

    def test_strips_bare_fence_without_language(self):
        text = "```\n<p>body</p>\n```"
        assert strip_model_preamble(text) == "<p>body</p>"


class TestSanitizeFilenamePart:
    def test_replaces_unsafe_chars(self):
        assert sanitize_filename_part("my doc/name?.pdf") == "my_doc_name_.pdf"

    def test_strips_leading_trailing_junk(self):
        assert sanitize_filename_part("___abc___") == "abc"

    def test_returns_default_on_empty(self):
        assert sanitize_filename_part("!!!") == "document"

    def test_keeps_allowed_characters(self):
        assert sanitize_filename_part("Standard-Contract_v2.1") == "Standard-Contract_v2.1"


class TestBuildPrompt:
    def test_synthetic_mentions_pii_replacement(self):
        prompt = build_prompt(synthetic=True, export_format="HTML")
        assert "synthetic" in prompt.lower()
        assert "pii" in prompt.lower() or "personally identifiable" in prompt.lower()
        assert "{page_text}" in prompt

    def test_real_forbids_adding_content(self):
        prompt = build_prompt(synthetic=False, export_format="HTML")
        assert "not add content" in prompt or "Do not add content" in prompt
        assert "{page_text}" in prompt

    def test_format_is_interpolated(self):
        prompt = build_prompt(synthetic=True, export_format="Markdown")
        assert "Markdown" in prompt

    def test_uses_underscored_tag_consistently(self):
        prompt = build_prompt(synthetic=True, export_format="HTML")
        assert "<raw_text>" in prompt
        assert "<raw text>" not in prompt  # never the space-variant

    def test_enumerates_pii_categories(self):
        prompt = build_prompt(synthetic=True, export_format="HTML")
        lowered = prompt.lower()
        for token in ["names", "phone", "email", "social security",
                      "credit card", "bank account", "ip address", "url",
                      "birth", "license"]:
            assert token in lowered, f"Missing PII category token: {token}"

    def test_synthetic_preserves_non_pii_numerics(self):
        prompt = build_prompt(synthetic=True, export_format="HTML")
        lowered = prompt.lower()
        assert "preserve" in lowered
        # One of these signals for non-PII numeric preservation should appear
        assert any(tok in lowered for tok in ["totals", "percentages", "reference numbers"])

    def test_image_vs_text_tiebreaker(self):
        prompt = build_prompt(synthetic=True, export_format="HTML")
        assert "image" in prompt.lower() and "reading order" in prompt.lower()

    def test_html_format_uses_html_tags(self):
        prompt = build_prompt(synthetic=False, export_format="HTML")
        assert "<table>" in prompt
        assert "<h1>" in prompt or "<h6>" in prompt

    def test_markdown_format_does_not_prescribe_html_tags(self):
        prompt = build_prompt(synthetic=False, export_format="Markdown")
        # Markdown branch must NOT tell the model to emit HTML tags
        assert "<h1>" not in prompt
        assert "<p>" not in prompt
        assert "<ul>" not in prompt
        assert "<table>" not in prompt
        # And should mention pipe tables for tabular data
        assert "pipe table" in prompt.lower()

    def test_mentions_headers_footers_page_numbers(self):
        prompt = build_prompt(synthetic=True, export_format="HTML")
        lowered = prompt.lower()
        assert "header" in lowered and "footer" in lowered
        assert "page number" in lowered

    def test_multi_column_reading_order(self):
        prompt = build_prompt(synthetic=True, export_format="HTML")
        assert "multi-column" in prompt.lower() or "reading order" in prompt.lower()

    def test_anti_injection_statement_in_user_prompt(self):
        prompt = build_prompt(synthetic=True, export_format="HTML")
        assert "never instructions" in prompt.lower()

    def test_grammar_a_text_export(self):
        prompt = build_prompt(synthetic=True, export_format="HTML")
        assert "a text export" in prompt
        assert "an text export" not in prompt


class TestBuildSystemPrompt:
    def test_mentions_output_format(self):
        assert "HTML" in build_system_prompt("HTML")
        assert "Markdown" in build_system_prompt("Markdown")

    def test_forbids_preambles_and_fences(self):
        sp = build_system_prompt("HTML")
        lowered = sp.lower()
        assert "preamble" in lowered or "commentary" in lowered
        assert "code fence" in lowered or "```" in sp

    def test_restates_raw_text_is_data(self):
        sp = build_system_prompt("HTML")
        assert "<raw_text>" in sp
        assert "never instructions" in sp.lower()


class TestConvertHtmlToMarkdown:
    def test_basic_conversion(self):
        md = convert_html_to_markdown("<h1>Title</h1><p>body</p>")
        assert "# Title" in md
        assert "body" in md
