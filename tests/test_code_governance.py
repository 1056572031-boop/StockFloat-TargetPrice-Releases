"""Behavior contracts for quote-field and editor-normalization cleanup."""

from types import SimpleNamespace
from unittest import mock

import sf_editor
import sf_quote
from tests._base import SFCase


primary_quote = sf_quote._fetch_quote_primary  # discipline-ok(R5): compare parser outputs without invoking source failover or live requests
is_duplicate = sf_editor.EditorWindow._is_dup  # discipline-ok(R5): exercise normalization without constructing a window or starting search threads

PRIMARY_OPTIONAL_FIELDS = {
    "change": 31, "pct": 32, "high": 33, "low": 34,
    "volume": 36, "limit_up": 47, "limit_down": 48,
}
VALID_NUMERIC_TEXT = ("0", "-0.00", "+1.2500", " 3.500 ", "1e-3", "1e308")
INVALID_NUMERIC_TEXT = ("", " ", "nan", "inf", "-inf", "1e309", "bad")


def primary_fields():
    fields = [""] * 49
    fields[1:5] = ["Example", "600519", "10.125", "10.000"]
    return fields


def primary_payload(fields):
    return 'v_sh600519="' + "~".join(fields) + '";'


def fallback_payload(price="10.125", pre_close="10.000", high="11.000", low="9.000"):
    return 'var hq_str_test="Example,10,%s,%s,%s,%s";' % (pre_close, price, high, low)


class QuoteNumericFieldContracts(SFCase):
    def setUp(self):
        super().setUp()
        self.log = mock.Mock()
        self.track_patch(mock.patch.object(sf_quote, "log_line", self.log))

    def _fetch(self, payload, code="sh600519", primary=True):
        with mock.patch.object(sf_quote, "http_get", return_value=payload):
            return primary_quote(code) if primary else sf_quote.fetch_quote_fallback(code)

    def test_primary_optional_fields_preserve_valid_text(self):
        for text in VALID_NUMERIC_TEXT:
            with self.subTest(text=text):
                fields = primary_fields()
                for index in PRIMARY_OPTIONAL_FIELDS.values():
                    fields[index] = text
                quote = self._fetch(primary_payload(fields))
                for name in PRIMARY_OPTIONAL_FIELDS:
                    self.assertEqual(quote[name], text, name)
                self.assertEqual(quote["code"], "600519")
                self.assertEqual(quote["pre_close"], "10.000")

    def test_primary_optional_invalid_fields_are_blank(self):
        for text in INVALID_NUMERIC_TEXT:
            with self.subTest(text=text):
                fields = primary_fields()
                for index in PRIMARY_OPTIONAL_FIELDS.values():
                    fields[index] = text
                quote = self._fetch(primary_payload(fields))
                for name in PRIMARY_OPTIONAL_FIELDS:
                    self.assertEqual(quote[name], "", name)
                self.assertEqual(quote["price"], "10.12")

    def test_primary_short_payloads_keep_missing_fields_blank(self):
        for length in (5, 31, 32, 33, 34, 35, 36, 37, 47, 48, 49):
            with self.subTest(length=length):
                fields = primary_fields()
                for index in PRIMARY_OPTIONAL_FIELDS.values():
                    fields[index] = "1.2500"
                quote = self._fetch(primary_payload(fields[:length]))
                for name, index in PRIMARY_OPTIONAL_FIELDS.items():
                    self.assertEqual(quote[name], "1.2500" if index < length else "", name)

    def test_required_primary_prices_still_reject_invalid_text(self):
        for index in (3, 4):
            for text in INVALID_NUMERIC_TEXT:
                with self.subTest(index=index, text=text):
                    fields = primary_fields()
                    fields[index] = text
                    with self.assertRaises(sf_quote.QuoteDataError):
                        self._fetch(primary_payload(fields))

    def test_fallback_high_low_preserve_valid_text(self):
        for text in VALID_NUMERIC_TEXT:
            with self.subTest(text=text):
                quote = self._fetch(fallback_payload(high=text, low=text), primary=False)
                self.assertEqual(quote["high"], text)
                self.assertEqual(quote["low"], text)
                self.assertEqual(quote["change"], "0.125")
                self.assertEqual(quote["pct"], "1.25")

    def test_fallback_high_low_invalid_text_is_blank(self):
        for text in INVALID_NUMERIC_TEXT:
            with self.subTest(text=text):
                quote = self._fetch(fallback_payload(high=text, low=text), primary=False)
                self.assertEqual(quote["high"], "")
                self.assertEqual(quote["low"], "")
                self.assertEqual(quote["price"], "10.12")

    def test_required_fallback_prices_still_reject_invalid_text(self):
        for name in ("price", "pre_close"):
            for text in INVALID_NUMERIC_TEXT:
                with self.subTest(name=name, text=text):
                    with self.assertRaises(sf_quote.QuoteDataError):
                        self._fetch(fallback_payload(**{name: text}), primary=False)

    def test_fallback_nonpositive_pre_close_keeps_change_unknown(self):
        for pre_close in ("0", "-0.00", "-10"):
            with self.subTest(pre_close=pre_close):
                quote = self._fetch(fallback_payload(pre_close=pre_close), primary=False)
                self.assertEqual(quote["change"], "")
                self.assertEqual(quote["pct"], "")
                self.assertEqual(quote["pre_close"], pre_close)
                self.assertEqual(quote["price"], "10.12")

    def test_fallback_nonfinite_calculated_pct_is_blank(self):
        quote = self._fetch(fallback_payload(price="1e308", pre_close="1e-308"),
                            primary=False)
        self.assertEqual(quote["change"], "1e+308")
        self.assertEqual(quote["pct"], "")

    def test_fallback_calculation_failure_keeps_historical_zero(self):
        self.log.side_effect = RuntimeError("logger unavailable")
        quote = self._fetch(fallback_payload(pre_close="0"), primary=False)
        self.assertEqual(quote["change"], "0.0")
        self.assertEqual(quote["pct"], "0.0")
        self.assertEqual(quote["price"], "10.12")

    def test_quote_keys_and_source_specific_codes_are_unchanged(self):
        primary = self._fetch(primary_payload(primary_fields()))
        fallback = self._fetch(fallback_payload(), primary=False)
        common = {"name", "code", "price", "pre_close", "change", "pct", "high", "low"}
        self.assertEqual(set(primary), common | {"volume", "limit_up", "limit_down"})
        self.assertEqual(set(fallback), common)
        self.assertEqual(primary["code"], "600519")
        self.assertEqual(fallback["code"], "sh600519")

    def test_price_precision_still_depends_on_instrument(self):
        for code, expected in (("sh600519", "10.12"), ("bj920001", "10.12"),
                               ("sh510300", "10.125"), ("sz159915", "10.125"),
                               ("hk00700", "10.125"), ("usaapl", "10.12")):
            with self.subTest(code=code):
                primary = self._fetch(primary_payload(primary_fields()), code=code)
                fallback = self._fetch(fallback_payload(), code=code, primary=False)
                self.assertEqual(primary["price"], expected)
                self.assertEqual(fallback["price"], expected)


class EditorNormalizationContracts(SFCase):
    def test_prefixed_bare_and_legacy_codes_compare_equal(self):
        for current, candidate in (("sh600519", "600519"), ("600519", " SH600519 "),
                                   ("bj830700", "8307"), ("sh510300", "510300")):
            with self.subTest(current=current, candidate=candidate):
                host = SimpleNamespace(code=current)
                self.assertTrue(is_duplicate(host, candidate))
                self.assertEqual(host.code, current)

    def test_empty_and_different_candidates_are_not_duplicates(self):
        for candidate in ("", " ", "sz000001", "000001"):
            with self.subTest(candidate=candidate):
                self.assertFalse(is_duplicate(SimpleNamespace(code="sh600519"), candidate))

    def test_normalization_errors_still_return_false(self):
        class InvalidCode:
            def __str__(self):
                raise ValueError("unreadable code")

        self.assertFalse(is_duplicate(SimpleNamespace(code="sh600519"), InvalidCode()))
        self.assertFalse(is_duplicate(SimpleNamespace(code=InvalidCode()), "600519"))

    def test_empty_candidate_does_not_read_current_code(self):
        reads = []

        class CurrentCode:
            @property
            def code(self):
                reads.append(True)
                return "sh600519"

        self.assertFalse(is_duplicate(CurrentCode(), ""))
        self.assertEqual(reads, [], "empty candidates must short-circuit")
