"""
Management command: report_skipped

Lists aligned PDFs from a session that will be skipped during "Save to Zotero"
because their Zotero link points to an item that no longer exists.

Verification is done by batch-fetching the linked keys from the Zotero API.

Usage:
    python manage.py report_skipped "Legal, Ethical, and Societal Issues"
    python manage.py report_skipped "Legal, Ethical, and Societal Issues" --output skipped.csv
"""

import csv
import os
import re
import time

from django.core.management.base import BaseCommand, CommandError

from article_summary_app.models import Session, PDFJob
from pyzotero import Zotero

LINK_RE = re.compile(r"zotero://select/(library|groups)(?:/(\d+))?/items/([A-Z0-9]+)")


def _batch_fetch_keys(zot, keys, batch_size=50):
    found = set()
    for i in range(0, len(keys), batch_size):
        batch = keys[i:i + batch_size]
        try:
            results = zot.items(itemKey=",".join(batch))
            for item in (results or []):
                found.add(item["key"])
        except Exception:
            pass
        time.sleep(0.1)
    return found


class Command(BaseCommand):
    help = "Report aligned PDFs that will be skipped during Zotero export."

    def add_arguments(self, parser):
        parser.add_argument("session_name", type=str)
        parser.add_argument("--output", type=str, default=None)

    def handle(self, *args, **options):
        session_name = options["session_name"]

        session = Session.objects.filter(name=session_name).first()
        if not session:
            raise CommandError(f"No session found: '{session_name}'")

        profile = session.user.profile
        api_key = profile.zotero_api_key
        user_id = profile.zotero_user_id

        zotero_links = session.zotero_links or {}

        # ── Get all done jobs ─────────────────────────────────────────────────
        all_jobs = list(PDFJob.objects.filter(session=session, status="done"))
        aligned_jobs = [
            j for j in all_jobs
            if (j.result or {}).get("fields", {}).get("aligns_with_criteria")
        ]
        self.stdout.write(f"Session: '{session.name}' — {len(all_jobs)} total | {len(aligned_jobs)} aligned")

        # ── Collect linked keys grouped by library ────────────────────────────
        # key → (lib_type, lib_id)
        key_to_lib = {}
        for job in all_jobs:
            link = zotero_links.get(job.pdf_path, "")
            m = LINK_RE.match(link)
            if not m:
                continue
            lib_type = "user" if m.group(1) == "library" else "group"
            lib_id = m.group(2)  # None for personal
            key_to_lib[m.group(3)] = (lib_type, lib_id)

        # ── Verify keys exist in Zotero ───────────────────────────────────────
        self.stdout.write("Verifying linked keys against Zotero API...")

        # Group keys by library for efficient batching
        by_lib = {}
        for key, (lt, lid) in key_to_lib.items():
            by_lib.setdefault((lt, lid), []).append(key)

        confirmed_keys = set()
        for (lt, lid), keys in by_lib.items():
            if lt == "user":
                zot = Zotero(user_id, "user", api_key)
                label = "personal"
            else:
                zot = Zotero(lid, "group", api_key)
                label = f"group {lid}"
            self.stdout.write(f"  Checking {len(keys)} keys in {label}...")
            found = _batch_fetch_keys(zot, keys)
            confirmed_keys.update(found)
            self.stdout.write(f"    Confirmed: {len(found)} | Missing: {len(keys) - len(found)}")

        # ── Classify every job ────────────────────────────────────────────────
        skipped = []
        will_export = 0

        for job in all_jobs:
            fields = (job.result or {}).get("fields", {})
            link = zotero_links.get(job.pdf_path, "")
            m = LINK_RE.match(link)
            link_key = m.group(3) if m else None

            if not link_key or link_key not in confirmed_keys:
                doi = fields.get("doi") or (job.result or {}).get("doi") or ""
                skipped.append({
                    "filename": os.path.basename(job.pdf_path),
                    "pdf_path": job.pdf_path,
                    "title": fields.get("title", ""),
                    "authors": fields.get("author", ""),
                    "year": str(fields.get("year", "")),
                    "doi": doi,
                    "apa_reference": fields.get("apa_reference", ""),
                    "aligns_with_criteria": fields.get("aligns_with_criteria", False),
                    "reason": "no link" if not link else "item not found in Zotero",
                    "current_link": link or "",
                })
            else:
                will_export += 1

        skipped.sort(key=lambda r: r["authors"].lower())

        # ── Print summary ─────────────────────────────────────────────────────
        self.stdout.write(f"\n  Will export    : {will_export}")
        self.stdout.write(f"  Will be skipped: {len(skipped)}")

        if not skipped:
            self.stdout.write(self.style.SUCCESS("\nAll aligned items have valid Zotero links — nothing will be skipped."))
            return

        self.stdout.write("\nSkipped items:")
        for r in skipped:
            doi_str = f" | DOI: {r['doi']}" if r['doi'] else " | No DOI"
            self.stdout.write(f"  [{r['year']}] {r['authors'][:50]}")
            self.stdout.write(f"    {r['title'][:70]}{doi_str}")
            self.stdout.write(f"    Reason: {r['reason']}")

        # ── Write CSV ─────────────────────────────────────────────────────────
        if options["output"]:
            out_path = options["output"]
        else:
            safe = session_name.replace(" ", "_").replace(",", "")
            out_path = os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                f"../../../../skipped_{safe}.csv"
            ))

        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "filename", "aligns_with_criteria", "title", "authors", "year",
                "doi", "apa_reference", "reason", "current_link", "pdf_path",
            ])
            writer.writeheader()
            writer.writerows(skipped)

        self.stdout.write(self.style.SUCCESS(f"\nCSV saved to: {out_path}"))
