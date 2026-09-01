import re
import tempfile
from pathlib import Path

from django.contrib.auth.models import User
from django.test import SimpleTestCase, TestCase, RequestFactory

from .analyzer import _parse_response, coerce_bool, is_aligned, FIELDS
from .extractor import _extract_abstract_from_text
from .views.common import resolve_allowed_pdf
from .views.zotero import _extract_author_info, _group_articles, ZOTERO_LINK_RE
from .search import harmonize_and_dedup
from .search_providers import normalize_query, _year_ok, _year_bounds


class ParseResponseTests(SimpleTestCase):
    def test_clean_json(self):
        data = _parse_response('{"title": "A", "aligns_with_criteria": true}')
        self.assertEqual(data["title"], "A")
        self.assertIs(data["aligns_with_criteria"], True)

    def test_json_wrapped_in_markdown_fence(self):
        raw = '```json\n{"title": "A", "aligns_with_criteria": "false"}\n```'
        data = _parse_response(raw)
        self.assertEqual(data["title"], "A")
        self.assertIs(data["aligns_with_criteria"], False)

    def test_think_tags_stripped(self):
        raw = '<think>reasoning here {not json}</think>{"aligns_with_criteria": "true"}'
        data = _parse_response(raw)
        self.assertIs(data["aligns_with_criteria"], True)

    def test_string_false_is_not_truthy(self):
        # Models often return the string "false"; it must not count as aligned.
        data = _parse_response('{"aligns_with_criteria": "false"}')
        self.assertIs(data["aligns_with_criteria"], False)

    def test_bad_escapes_recovered(self):
        raw = r'{"title": "10\.1234 study", "aligns_with_criteria": true}'
        data = _parse_response(raw)
        self.assertIn("study", data["title"])

    def test_garbage_raises_for_retry(self):
        # Unparseable output must raise so the job lands in 'error' (retryable),
        # not be silently recorded as an excluded result.
        with self.assertRaises(ValueError):
            _parse_response("total nonsense, no JSON at all")

    def test_non_object_json_raises_for_retry(self):
        with self.assertRaises(ValueError):
            _parse_response('["a", "list"]')


class CoerceBoolTests(SimpleTestCase):
    def test_truthy_values(self):
        for v in (True, "true", "True", " TRUE ", "yes", "1"):
            self.assertIs(coerce_bool(v), True, v)

    def test_falsy_values(self):
        for v in (False, "false", "False", "no", "0", None, "", "null"):
            self.assertIs(coerce_bool(v), False, v)

    def test_is_aligned_handles_legacy_strings(self):
        self.assertTrue(is_aligned({"aligns_with_criteria": "true"}))
        self.assertFalse(is_aligned({"aligns_with_criteria": "false"}))
        self.assertFalse(is_aligned({}))
        self.assertFalse(is_aligned(None))


class AbstractExtractionTests(SimpleTestCase):
    def test_extracts_abstract_before_introduction(self):
        text = ("Some Title\nAbstract: " + "This study examines things in depth. " * 5 +
                "\nIntroduction\nThe rest of the paper.")
        abstract, is_real = _extract_abstract_from_text(text)
        self.assertTrue(is_real)
        self.assertIn("This study examines", abstract)
        self.assertNotIn("rest of the paper", abstract)

    def test_no_abstract_found(self):
        abstract, is_real = _extract_abstract_from_text("No marker here at all.")
        self.assertFalse(is_real)
        self.assertEqual(abstract, "")

    def test_short_match_rejected(self):
        abstract, is_real = _extract_abstract_from_text("Abstract: too short. Introduction follows")
        self.assertFalse(is_real)


class ZoteroHelperTests(SimpleTestCase):
    def test_link_regex_personal_library(self):
        m = re.match(ZOTERO_LINK_RE, "zotero://select/library/items/QZ2S4S99")
        self.assertEqual(m.group(1), "library")
        self.assertIsNone(m.group(2))
        self.assertEqual(m.group(3), "QZ2S4S99")

    def test_link_regex_group_library(self):
        m = re.match(ZOTERO_LINK_RE, "zotero://select/groups/123456/items/ABCDEFGH")
        self.assertEqual(m.group(1), "groups")
        self.assertEqual(m.group(2), "123456")
        self.assertEqual(m.group(3), "ABCDEFGH")

    def test_author_info_single(self):
        sort_key, display = _extract_author_info([{"creatorType": "author", "lastName": "Smith", "firstName": "A"}])
        self.assertEqual(sort_key, "smith")
        self.assertEqual(display, "Smith")

    def test_author_info_two(self):
        creators = [
            {"creatorType": "author", "lastName": "Smith"},
            {"creatorType": "author", "lastName": "Jones"},
        ]
        self.assertEqual(_extract_author_info(creators)[1], "Smith & Jones")

    def test_author_info_many(self):
        creators = [{"creatorType": "author", "lastName": n} for n in ("Smith", "Jones", "Lee")]
        self.assertEqual(_extract_author_info(creators)[1], "Smith et al.")

    def test_author_info_empty(self):
        self.assertEqual(_extract_author_info([]), ("", "Unknown"))

    def test_group_articles_picks_newest_version(self):
        pdfs = ["K1/old.pdf", "K2/new.pdf"]
        meta = {
            "K1/old.pdf": {"parent_key": "P1", "title": "Paper", "author_sort": "smith",
                           "author_display": "Smith", "year": "2020", "date_modified": "2020-01-01"},
            "K2/new.pdf": {"parent_key": "P1", "title": "Paper", "author_sort": "smith",
                           "author_display": "Smith", "year": "2020", "date_modified": "2024-01-01"},
        }
        groups, ordered = _group_articles(pdfs, meta)
        self.assertEqual(len(groups), 1)
        self.assertEqual(ordered, ["K2/new.pdf"])
        self.assertTrue(groups[0]["versions"][0]["selected"])
        self.assertEqual(groups[0]["versions"][0]["path"], "K2/new.pdf")


class ResolveAllowedPdfTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = User.objects.create_user("tester", password="x")
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        self.pdf = self.folder / "paper.pdf"
        self.pdf.write_bytes(b"%PDF-1.4 fake")
        self.secret = self.folder / "secrets.env"
        self.secret.write_text("KEY=value")

    def tearDown(self):
        self.tmp.cleanup()

    def _request(self, allowed_folders=None):
        request = self.factory.get("/pdf")
        request.user = self.user
        request.session = {"allowed_folders": allowed_folders or []}
        return request

    def test_pdf_in_allowed_folder_resolves(self):
        request = self._request([str(self.folder)])
        self.assertEqual(resolve_allowed_pdf(request, str(self.pdf)), self.pdf.resolve())

    def test_pdf_outside_allowed_roots_rejected(self):
        request = self._request([])  # nothing allowed
        self.assertIsNone(resolve_allowed_pdf(request, str(self.pdf)))

    def test_non_pdf_rejected_even_inside_allowed_folder(self):
        request = self._request([str(self.folder)])
        self.assertIsNone(resolve_allowed_pdf(request, str(self.secret)))

    def test_traversal_out_of_allowed_folder_rejected(self):
        request = self._request([str(self.folder)])
        sneaky = str(self.folder / ".." / ".." / "etc" / "passwd")
        self.assertIsNone(resolve_allowed_pdf(request, sneaky))

    def test_pdf_in_session_folder_resolves(self):
        from .models import Session
        Session.objects.create(user=self.user, folder=str(self.folder), criteria="c")
        request = self._request([])
        self.assertEqual(resolve_allowed_pdf(request, str(self.pdf)), self.pdf.resolve())


class HarmonizeDedupTests(SimpleTestCase):
    def _rec(self, source, title="", doi="", year="", abstract=""):
        return {"source": source, "title": title, "doi": doi, "year": year,
                "abstract": abstract, "authors": "", "venue": "", "url": ""}

    def test_dedup_by_doi_merges_sources_and_keeps_longer_abstract(self):
        out = harmonize_and_dedup([
            self._rec("crossref", "A", doi="10.1/x", abstract="short"),
            self._rec("openalex", "A", doi="10.1/x", abstract="a much longer abstract"),
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(sorted(out[0]["sources"]), ["crossref", "openalex"])
        self.assertEqual(out[0]["abstract"], "a much longer abstract")

    def test_dedup_by_title_year_when_no_doi(self):
        out = harmonize_and_dedup([
            self._rec("arxiv", "Deep Learning!", year="2020"),
            self._rec("core", "deep learning", year="2020"),
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(sorted(out[0]["sources"]), ["arxiv", "core"])

    def test_different_year_not_merged(self):
        out = harmonize_and_dedup([
            self._rec("arxiv", "Same Title", year="2020"),
            self._rec("core", "Same Title", year="2021"),
        ])
        self.assertEqual(len(out), 2)

    def test_record_without_doi_or_title_passes_through(self):
        out = harmonize_and_dedup([self._rec("crossref", title="", doi="")])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["sources"], ["crossref"])


class NormalizeQueryTests(SimpleTestCase):
    Q = '(AI OR "machine learning*") AND (faculty OR professor*)'

    def test_boolean_passthrough(self):
        self.assertEqual(normalize_query(self.Q, "boolean"), self.Q)

    def test_s2_uses_symbol_operators(self):
        out = normalize_query(self.Q, "s2")
        self.assertIn("|", out)
        self.assertIn("+", out)
        self.assertNotIn(" OR ", out)
        self.assertNotIn(" AND ", out)

    def test_plain_strips_operators_parens_wildcards(self):
        out = normalize_query(self.Q, "plain")
        for bad in ("(", ")", "*", " OR ", " AND "):
            self.assertNotIn(bad, out)
        self.assertIn("machine learning", out)
        self.assertIn("faculty", out)


class YearFilterTests(SimpleTestCase):
    def test_bounds_parsing(self):
        self.assertEqual(_year_bounds("2015", "2020"), (2015, 2020))
        self.assertEqual(_year_bounds("", None), (None, None))
        self.assertEqual(_year_bounds("2015-01-01", "junk"), (2015, None))

    def test_year_ok_range(self):
        self.assertTrue(_year_ok("2018", 2015, 2020))
        self.assertFalse(_year_ok("2010", 2015, 2020))
        self.assertFalse(_year_ok("2025", 2015, 2020))

    def test_unparseable_year_passes(self):
        self.assertTrue(_year_ok("", 2015, 2020))
        self.assertTrue(_year_ok(None, 2015, 2020))

    def test_open_ended_bounds(self):
        self.assertTrue(_year_ok("2000", None, 2020))
        self.assertFalse(_year_ok("2021", None, 2020))
        self.assertTrue(_year_ok("2030", 2015, None))


class FieldsContractTests(SimpleTestCase):
    def test_fields_has_18_entries_plus_apa(self):
        # 18 prompt fields + apa_reference (filled from Crossref)
        self.assertEqual(len(FIELDS), 19)
        self.assertIn("aligns_with_criteria", FIELDS)
        self.assertIn("apa_reference", FIELDS)
