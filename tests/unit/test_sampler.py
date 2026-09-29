"""Tests for the size-based content sampler."""

from interlock.worker.sampler import ContentSampler, SampledContent


class TestContentSampler:
    def setup_method(self) -> None:
        self.sampler = ContentSampler()

    def test_empty_content(self) -> None:
        result = self.sampler.sample("")
        assert result.strategy == "full"
        assert result.original_word_count == 0
        assert result.sampled_word_count == 0
        assert result.sampling_ratio == 1.0
        assert result.text == ""

    def test_whitespace_only(self) -> None:
        result = self.sampler.sample("   \n\n  ")
        assert result.strategy == "full"
        assert result.original_word_count == 0

    def test_small_content_returns_full(self) -> None:
        content = "Hello world. " * 100  # 200 words
        result = self.sampler.sample(content)
        assert result.strategy == "full"
        assert result.text == content
        assert result.sampling_ratio == 1.0
        assert result.original_word_count == result.sampled_word_count

    def test_small_threshold_boundary(self) -> None:
        # Exactly at the threshold should still be "full"
        content = "word " * 2500
        result = self.sampler.sample(content)
        assert result.strategy == "full"

    def test_medium_content_returns_strategic(self) -> None:
        # Build content with many paragraphs totaling ~5000 words
        paragraphs = []
        for i in range(50):
            paragraphs.append(f"Paragraph {i}. " + "filler word " * 98)
        content = "\n\n".join(paragraphs)

        result = self.sampler.sample(content)
        assert result.strategy == "strategic"
        assert result.sampled_word_count < result.original_word_count
        # Should be roughly 20-30% of original
        assert 0.1 < result.sampling_ratio < 0.5

    def test_large_content_returns_structure(self) -> None:
        # Build content >25000 words with headings
        sections = []
        for i in range(100):
            heading = f"# Section {i}"
            body = "This is the first sentence. " + "padding text " * 248
            sections.append(f"{heading}\n{body}")
        content = "\n\n".join(sections)

        result = self.sampler.sample(content)
        assert result.strategy == "structure"
        assert result.sampled_word_count < result.original_word_count
        assert result.sampling_ratio < 0.3

    def test_sampling_ratio_is_correct(self) -> None:
        content = "word " * 5000
        # This is medium range, so strategic
        result = self.sampler.sample(content)
        expected_ratio = result.sampled_word_count / result.original_word_count
        assert abs(result.sampling_ratio - round(expected_ratio, 4)) < 0.001

    def test_strategic_preserves_first_and_last(self) -> None:
        paragraphs = ["FIRST paragraph content here."]
        for i in range(48):
            paragraphs.append(f"Middle paragraph {i}. " + "filler " * 98)
        paragraphs.append("LAST paragraph content here.")
        content = "\n\n".join(paragraphs)

        result = self.sampler.sample(content)
        assert result.strategy == "strategic"
        assert "FIRST paragraph" in result.text
        assert "LAST paragraph" in result.text

    def test_structure_extracts_headings(self) -> None:
        sections = []
        for i in range(130):
            heading = f"# Heading {i}"
            body = f"First sentence of section {i}. " + "padding " * 210
            sections.append(f"{heading}\n{body}")
        content = "\n\n".join(sections)

        result = self.sampler.sample(content)
        assert result.strategy == "structure"
        assert "# Heading 0" in result.text
        assert "First sentence of section 0." in result.text

    def test_sampled_content_model(self) -> None:
        sc = SampledContent(
            text="hello",
            strategy="full",
            original_word_count=1,
            sampled_word_count=1,
            sampling_ratio=1.0,
        )
        assert sc.text == "hello"
        assert sc.strategy == "full"
