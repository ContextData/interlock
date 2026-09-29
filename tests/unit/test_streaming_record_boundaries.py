"""Where one streamed record ends, and why CSV is not newline-delimited.

The streaming redaction path scans one record at a time. It found records by
splitting on raw newline characters, which is right for ndjson and plain text
and wrong for CSV: RFC 4180 permits a newline inside a quoted field, so a
value wrapping across one was cut into fragments that matched no pattern and
passed through unredacted.

Three of the six fast-tier patterns can span a newline, because their
separator classes include `\\s`: CREDIT_CARD, PHONE and MRN. SSN and EMAIL
cannot - which is why the obvious probe value never revealed it, and why the
existing fixtures, though they take the streaming branch, proved nothing about
this.
"""

from __future__ import annotations

import pytest

from interlock.gateway.http_proxy import _is_csv_like, _record_boundary
from interlock.pipeline.pii_fast import PIIFastScanner


def _split_all(text: str, *, csv_aware: bool) -> list[str]:
    """Split exactly as the streaming loop does, so the test exercises the loop."""
    records: list[str] = []
    pending = text
    in_quotes = False
    while True:
        boundary, in_quotes = _record_boundary(pending, csv_aware=csv_aware, in_quotes=in_quotes)
        if boundary < 0:
            break
        records.append(pending[:boundary])
        pending = pending[boundary:]
    if pending:
        records.append(pending)
    return records


class TestNewlineDelimitedContent:
    def test_plain_text_splits_on_every_newline(self) -> None:
        assert _split_all("a\nb\nc\n", csv_aware=False) == ["a\n", "b\n", "c\n"]

    def test_a_trailing_partial_line_is_held_back(self) -> None:
        """The reassembly the streaming path depends on."""
        boundary, _ = _record_boundary("no newline yet", csv_aware=False, in_quotes=False)

        assert boundary == -1

    def test_quotes_are_not_special_outside_csv(self) -> None:
        """ndjson strings contain quotes constantly; treating them as CSV would break it."""
        body = '{"a":"x\ny"}\n'

        assert _split_all(body, csv_aware=False) == ['{"a":"x\n', 'y"}\n']


class TestCsvRecords:
    def test_a_newline_inside_a_quoted_field_is_not_a_boundary(self) -> None:
        body = 'id,note\n1,"line one\nline two"\n'

        assert _split_all(body, csv_aware=True) == ["id,note\n", '1,"line one\nline two"\n']

    def test_an_escaped_quote_does_not_end_the_field(self) -> None:
        """RFC 4180: a doubled quote inside a quoted field is a literal quote."""
        body = 'id,note\n1,"she said ""hi""\nand left"\n'

        assert _split_all(body, csv_aware=True) == [
            "id,note\n",
            '1,"she said ""hi""\nand left"\n',
        ]

    def test_a_quote_at_the_chunk_edge_waits_rather_than_guessing(self) -> None:
        """An ambiguous trailing quote must not be resolved by guessing.

        Inside a quoted field, a final `"` could open an escape pair whose
        second half is in the next chunk. Splitting there would cut a record in
        half - the exact defect, reintroduced by the fix for it.
        """
        boundary, in_quotes = _record_boundary('1,"abc"', csv_aware=True, in_quotes=True)

        assert boundary == -1
        assert in_quotes is True

    def test_quote_state_carries_across_records(self) -> None:
        body = 'a,"one\ntwo"\nb,"three\nfour"\n'

        assert _split_all(body, csv_aware=True) == ['a,"one\ntwo"\n', 'b,"three\nfour"\n']

    def test_unquoted_csv_still_splits_per_line(self) -> None:
        body = "id,name\n1,Ada\n2,Grace\n"

        assert _split_all(body, csv_aware=True) == ["id,name\n", "1,Ada\n", "2,Grace\n"]


class TestTheLeakItself:
    """The point of the change, asserted against the scanner rather than by shape."""

    @pytest.mark.parametrize(
        ("label", "record"),
        [
            ("credit card", '1,"card 4111 1111 1111\n1111 on file"\n'),
            ("phone", '1,"call 415-555\n1212 today"\n'),
            ("mrn", '1,"ref MRN\n1234567 attached"\n'),
        ],
    )
    def test_a_value_wrapping_a_quoted_newline_is_still_detected(
        self, label: str, record: str
    ) -> None:
        scanner = PIIFastScanner()

        for fragment in _split_all(record, csv_aware=True):
            if scanner.scan(fragment):
                return

        pytest.fail(
            f"the {label} spanning a quoted newline was not detected in any record, "
            "so it would stream to the caller unredacted"
        )

    @pytest.mark.parametrize(
        ("label", "record"),
        [
            ("credit card", '1,"card 4111 1111 1111\n1111 on file"\n'),
            ("phone", '1,"call 415-555\n1212 today"\n'),
        ],
    )
    def test_splitting_on_raw_newlines_is_what_lost_it(self, label: str, record: str) -> None:
        """Pins the mechanism, so the fix cannot be mistaken for a coincidence.

        Under the old behaviour no fragment matches; under the new one a
        fragment does. If this stops failing to detect, the premise of the fix
        has changed and the fix should be re-examined.
        """
        scanner = PIIFastScanner()

        naive = _split_all(record, csv_aware=False)

        assert not any(scanner.scan(fragment) for fragment in naive), (
            f"the {label} is now detected even when split naively, so this test no "
            "longer demonstrates why CSV awareness is needed"
        )


class TestContentTypeSelection:
    @pytest.mark.parametrize(
        "content_type", ["text/csv", "application/csv", "text/csv; charset=utf-8", "TEXT/CSV"]
    )
    def test_csv_media_types_are_recognised(self, content_type: str) -> None:
        assert _is_csv_like(content_type) is True

    @pytest.mark.parametrize(
        "content_type",
        ["text/plain", "application/x-ndjson", "application/json", "application/xml", ""],
    )
    def test_other_media_types_are_not_treated_as_csv(self, content_type: str) -> None:
        """Applying CSV quoting to ndjson would merge records at every string quote."""
        assert _is_csv_like(content_type) is False


def _split_incrementally(chunks: list[str], *, csv_aware: bool) -> list[str]:
    """Split the way the gateway does: chunk by chunk, holding a partial record.

    Distinct from `_split_all`, which sees the whole body at once. That
    difference is not cosmetic - it is where the real bug was.
    """
    records: list[str] = []
    pending = ""
    in_quotes = False
    for chunk in chunks:
        pending += chunk
        while True:
            boundary, boundary_quotes = _record_boundary(
                pending, csv_aware=csv_aware, in_quotes=in_quotes
            )
            if boundary < 0:
                break
            records.append(pending[:boundary])
            pending = pending[boundary:]
            in_quotes = boundary_quotes
    if pending:
        records.append(pending)
    return records


class TestIncrementalDelivery:
    """Chunk-by-chunk, because whole-string splitting hid a real defect.

    The first version of the CSV fix adopted the quote state returned when no
    boundary was found. That scan walks the whole pending buffer without
    consuming any of it, so the buffer is rescanned from its start when the
    next chunk arrives and those quotes are counted twice, inverting the state.
    A CSV body redacted correctly in one chunk and leaked when split across
    two - and every whole-string test passed throughout.
    """

    @pytest.mark.parametrize("cut", range(1, 40))
    def test_the_records_are_the_same_wherever_the_chunk_boundary_falls(self, cut: int) -> None:
        body = 'id,note\n1,"card 4111 1111 1111\n1111 on file"\n2,ok\n'
        expected = _split_all(body, csv_aware=True)

        actual = _split_incrementally([body[:cut], body[cut:]], csv_aware=True)

        assert actual == expected, f"a chunk boundary at byte {cut} changed the records"

    @pytest.mark.parametrize("cut", range(1, 40))
    def test_a_wrapped_value_survives_any_chunk_boundary(self, cut: int) -> None:
        """The property the leak actually turns on."""
        body = 'id,note\n1,"card 4111 1111 1111\n1111 on file"\n'
        scanner = PIIFastScanner()

        records = _split_incrementally([body[:cut], body[cut:]], csv_aware=True)

        assert any(scanner.scan(record) for record in records), (
            f"with a chunk boundary at byte {cut} the card was not detected in any "
            f"record: {records}"
        )

    def test_many_small_chunks_behave_like_one_large_one(self) -> None:
        body = 'a,"one\ntwo"\nb,"three\nfour"\nc,plain\n'

        assert _split_incrementally(list(body), csv_aware=True) == _split_all(body, csv_aware=True)
