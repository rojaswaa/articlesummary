"""
Management command: remap_from_collection

Fixes dead zotero_links in a session by matching unresolved PDFs against
items in a specific Zotero collection, using title similarity as the key.

Usage:
    python manage.py remap_from_collection "Legal, Ethical, and Societal Issues" ZXCXBL45 --group-id 5850258
    python manage.py remap_from_collection "Legal, Ethical, and Societal Issues" ZXCXBL45 --group-id 5850258 --dry-run
"""

import re
import time

from django.core.management.base import BaseCommand, CommandError

from article_summary_app.models import Session, PDFJob
from pyzotero import Zotero

LINK_RE = re.compile(r"zotero://select/(library|groups)(?:/(\d+))?/items/([A-Z0-9]+)")


def _normalise(text):
    """Lowercase, strip punctuation and extra spaces for fuzzy comparison."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _doi_normalise(doi):
    doi = (doi or "").strip().lower()
    if doi.startswith("https://doi.org/"):
        doi = doi[len("https://doi.org/"):]
    return doi


class Command(BaseCommand):
    help = "Remap dead session zotero_links by matching against a Zotero collection via title/DOI."

    def add_arguments(self, parser):
        parser.add_argument("session_name", type=str)
        parser.add_argument("collection_key", type=str, help="Zotero collection key to match against")
        parser.add_argument("--group-id", type=str, required=True)
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        session_name = options["session_name"]
        coll_key = options["collection_key"]
        group_id = options["group_id"]
        dry_run = options["dry_run"]

        session = Session.objects.filter(name=session_name).first()
        if not session:
            raise CommandError(f"No session found: '{session_name}'")

        profile = session.user.profile
        zot = Zotero(group_id, "group", profile.zotero_api_key)

        # ── Fetch collection items and build indexes ───────────────────────────
        self.stdout.write(f"Fetching items from collection {coll_key}...")
        coll_items = zot.everything(zot.collection_items(coll_key))
        top_items = [i for i in coll_items
                     if i.get("data", {}).get("itemType") not in ("attachment", "note")]
        self.stdout.write(f"  {len(top_items)} top-level items found.")

        doi_index   = {}   # normalised doi → item
        title_index = {}   # normalised title → item

        for item in top_items:
            data = item.get("data", {})
            doi = _doi_normalise(data.get("DOI", ""))
            if doi:
                doi_index[doi] = item
            title = _normalise(data.get("title", ""))
            if title:
                title_index[title] = item

        self.stdout.write(f"  Index: {len(doi_index)} DOIs | {len(title_index)} titles")

        # ── Find unresolved pdf_paths in the session ───────────────────────────
        zotero_links = session.zotero_links or {}

        # Batch-verify which link keys actually exist in Zotero
        all_link_keys = {}  # key → [pdf_path]
        for pdf_path, link in zotero_links.items():
            m = LINK_RE.match(link)
            if m:
                all_link_keys.setdefault(m.group(3), []).append(pdf_path)

        self.stdout.write(f"\nVerifying {len(all_link_keys)} linked keys against Zotero API...")
        confirmed = set()
        keys_list = list(all_link_keys.keys())
        for i in range(0, len(keys_list), 50):
            batch = keys_list[i:i+50]
            try:
                results = zot.items(itemKey=",".join(batch))
                for it in (results or []):
                    confirmed.add(it["key"])
            except Exception:
                pass
            time.sleep(0.1)

        # Also check other libraries for the remaining keys
        user_id = profile.zotero_user_id
        api_key = profile.zotero_api_key
        missing_keys = [k for k in keys_list if k not in confirmed]
        if missing_keys:
            personal_zot = Zotero(user_id, "user", api_key)
            for i in range(0, len(missing_keys), 50):
                batch = missing_keys[i:i+50]
                try:
                    results = personal_zot.items(itemKey=",".join(batch))
                    for it in (results or []):
                        confirmed.add(it["key"])
                except Exception:
                    pass
                time.sleep(0.1)

        dead_paths = [
            pdf_path
            for key, paths in all_link_keys.items()
            if key not in confirmed
            for pdf_path in paths
        ]
        self.stdout.write(f"  {len(confirmed)} confirmed | {len(dead_paths)} dead links to fix")

        if not dead_paths:
            self.stdout.write(self.style.SUCCESS("Nothing to fix."))
            return

        # ── Load PDFJob metadata for dead paths ────────────────────────────────
        jobs = {
            j.pdf_path: j
            for j in PDFJob.objects.filter(session=session, pdf_path__in=dead_paths, status="done")
        }

        # ── Match each dead path against the collection index ──────────────────
        new_links = dict(zotero_links)
        matched = 0
        unmatched = []

        for pdf_path in dead_paths:
            job = jobs.get(pdf_path)
            fields = (job.result or {}).get("fields", {}) if job else {}

            doi = _doi_normalise(fields.get("doi") or (job.result or {}).get("doi", "") if job else "")
            title = _normalise(fields.get("title", ""))
            filename = pdf_path.split("/")[-1]

            found_item = None
            match_method = None

            if doi and doi in doi_index:
                found_item = doi_index[doi]
                match_method = f"DOI ({doi})"
            elif title and title in title_index:
                found_item = title_index[title]
                match_method = f"exact title"
            else:
                # Partial title match — session title contains collection title or vice versa
                for coll_title, item in title_index.items():
                    if (title and coll_title and
                            (title in coll_title or coll_title in title or
                             # Word overlap: if 5+ words match in order
                             _word_overlap(title, coll_title) >= 5)):
                        found_item = item
                        match_method = f"partial title"
                        break

            if found_item:
                item_key = found_item["key"]
                item_lib = found_item.get("library", {})
                lib_type = item_lib.get("type", "group")
                lib_id = item_lib.get("id")

                if lib_type == "user":
                    correct_link = f"zotero://select/library/items/{item_key}"
                else:
                    correct_link = f"zotero://select/groups/{lib_id}/items/{item_key}"

                old_link = zotero_links.get(pdf_path, "")
                new_links[pdf_path] = correct_link
                matched += 1
                self.stdout.write(f"  ✓ [{match_method}] {filename[:50]}")
                self.stdout.write(f"      {old_link}")
                self.stdout.write(f"    → {correct_link}")
            else:
                unmatched.append((pdf_path, title, doi))
                self.stdout.write(f"  ✗ No match: {filename[:70]}")

        self.stdout.write(f"\nResult: {matched} matched | {len(unmatched)} unmatched")

        if unmatched:
            self.stdout.write("\nStill unmatched:")
            for pdf_path, title, doi in unmatched:
                self.stdout.write(f"  {pdf_path.split('/')[-1][:70]}")
                self.stdout.write(f"    title: {title[:60]}")
                if doi:
                    self.stdout.write(f"    doi:   {doi}")

        if matched == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to update."))
            return

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes saved."))
        else:
            session.zotero_links = new_links
            session.save(update_fields=["zotero_links"])
            self.stdout.write(self.style.SUCCESS(f"Saved {matched} corrected links to session."))


def _word_overlap(a, b):
    """Count how many consecutive words from a appear in b."""
    words_a = a.split()
    words_b = set(b.split())
    return sum(1 for w in words_a if w in words_b)
