"""
Management command: dedup_collection

Finds duplicate items in a Zotero collection and removes the extras,
keeping the copy with the most complete metadata.

Deduplication priority:
  1. DOI (exact match, case-insensitive)
  2. Title (case-insensitive, after stripping whitespace)

"Remove" means removing from the collection only — items are NOT deleted
from the library.

Usage:
    python manage.py dedup_collection ZXCXBL45 --group-id 5850258
    python manage.py dedup_collection ZXCXBL45 --group-id 5850258 --dry-run
"""

import time
from collections import defaultdict

from django.core.management.base import BaseCommand, CommandError

from article_summary_app.models import Profile
from pyzotero import Zotero


def _metadata_score(item):
    """Score an item by how complete its metadata is. Higher = keep this one."""
    data = item.get("data", {})
    score = 0
    for field in ("DOI", "title", "abstractNote", "date", "volume", "issue",
                  "pages", "publicationTitle", "url", "ISBN", "ISSN"):
        if data.get(field, "").strip():
            score += 1
    return score


class Command(BaseCommand):
    help = "Remove duplicate items from a Zotero collection."

    def add_arguments(self, parser):
        parser.add_argument("collection_key", type=str, help="Zotero collection key (e.g. ZXCXBL45)")
        parser.add_argument("--group-id", type=str, required=True, help="Group library ID")
        parser.add_argument("--dry-run", action="store_true", help="Print what would be removed without changing anything")
        parser.add_argument("--username", type=str, default="admin", help="Django username to read API key from")

    def handle(self, *args, **options):
        coll_key = options["collection_key"]
        group_id = options["group_id"]
        dry_run = options["dry_run"]
        username = options["username"]

        try:
            profile = Profile.objects.get(user__username=username)
        except Profile.DoesNotExist:
            raise CommandError(f"No profile found for username '{username}'")

        zot = Zotero(group_id, "group", profile.zotero_api_key)

        self.stdout.write(f"Fetching items from collection {coll_key} in group {group_id}...")
        items = zot.everything(zot.collection_items(coll_key))
        # Filter to top-level items only (skip attachments, notes)
        items = [i for i in items if i.get("data", {}).get("itemType") not in ("attachment", "note")]
        self.stdout.write(f"  {len(items)} top-level items found.")

        # ── Group by DOI ──────────────────────────────────────────────────────
        doi_groups = defaultdict(list)
        no_doi = []
        for item in items:
            doi = item.get("data", {}).get("DOI", "").strip().lower()
            if doi:
                doi_groups[doi].append(item)
            else:
                no_doi.append(item)

        # ── Group no-DOI items by title ───────────────────────────────────────
        title_groups = defaultdict(list)
        unique_items = []
        for item in no_doi:
            title = item.get("data", {}).get("title", "").strip().lower()
            if title:
                title_groups[title].append(item)
            else:
                unique_items.append(item)  # no DOI and no title — can't dedup

        # ── Collect duplicates ────────────────────────────────────────────────
        to_remove = []  # list of (item, reason)
        kept = 0

        for doi, group in doi_groups.items():
            if len(group) == 1:
                kept += 1
                continue
            group.sort(key=_metadata_score, reverse=True)
            winner = group[0]
            kept += 1
            for dup in group[1:]:
                to_remove.append((dup, f"DOI duplicate of {winner['key']}"))

        for title, group in title_groups.items():
            if len(group) == 1:
                kept += 1
                continue
            group.sort(key=_metadata_score, reverse=True)
            winner = group[0]
            kept += 1
            for dup in group[1:]:
                to_remove.append((dup, f"Title duplicate of {winner['key']}"))

        kept += len(unique_items)

        self.stdout.write(f"\nResults: {kept} unique | {len(to_remove)} duplicates to remove\n")

        if not to_remove:
            self.stdout.write(self.style.SUCCESS("No duplicates found."))
            return

        for item, reason in to_remove:
            data = item.get("data", {})
            title = data.get("title", "(no title)")[:70]
            self.stdout.write(f"  REMOVE [{item['key']}] {title}")
            self.stdout.write(f"         Reason: {reason}")

        if dry_run:
            self.stdout.write(self.style.WARNING(f"\nDRY RUN — {len(to_remove)} items would be removed from collection."))
            return

        # ── Remove duplicates from collection (not from library) ──────────────
        self.stdout.write(f"\nRemoving {len(to_remove)} items from collection...")
        removed = 0
        for item, reason in to_remove:
            try:
                data = item.get("data", {})
                current_colls = data.get("collections", [])
                if coll_key in current_colls:
                    current_colls.remove(coll_key)
                    data["collections"] = current_colls
                    zot.update_item(item)
                    removed += 1
                    self.stdout.write(f"  ✓ Removed from collection: {data.get('title','')[:60]}")
                else:
                    self.stdout.write(f"  - Already not in collection: {item['key']}")
                time.sleep(0.15)
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"  ✗ Error removing {item['key']}: {e}"))

        self.stdout.write(self.style.SUCCESS(f"\nDone. {removed} duplicates removed from collection '{coll_key}'."))
