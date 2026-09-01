"""
Management command: export_missing_items

Cross-references a session's zotero_links against the Zotero API to find
PDFs whose items are no longer in any Zotero library, then exports their
metadata (title, authors, DOI, APA reference) from the stored PDFJob results.

Usage:
    python manage.py export_missing_items "Legal, Ethical, and Societal Issues"
    python manage.py export_missing_items "Legal, Ethical, and Societal Issues" --output missing.csv
"""

import csv
import json
import time
import os

from django.core.management.base import BaseCommand, CommandError

from article_summary_app.models import Session, PDFJob
from pyzotero import Zotero

MISSING_KEYS = [
    'PX6NLEYC','BSPRECJJ','HPDCGGE3','6G555UPA','MBW66L9F','SS5WVDMM',
    'P6HGT62J','4VQ2EB3M','Q3MMRE6N','FDX3BQ5H','V5YARDS6','Q5FLEL9L',
    'KKNUQMVR','GKL2575N','46XCK7QG','C2WE5BTQ','3KZIBSTH','TG9JJEZJ',
    'QWMTYJJB','4AYFIHWP','BLXYMG8C','BN4VNIBX','KS4WLVUJ','ITP4QGG8',
    'MIJ6TWXN','LHVKBI5I','NLVAYVFA','EBVKFM7C','ZLLJ3NJT','XL5CGXJQ',
    'T8NMYELA','HYUYDJ5F','J4IR7Q8X','AGDSEXNR','5CVCDGEB','Q5P4RECC',
    'JC7X5F24','NIKDPBIG','MBL6F2QJ','MIY5FQTC','BBGJE4JH','XRN6T65D',
    '2V4QTJL6','83MPUAQL','5KZQL2CP','SDHK3YIK','K9Q3CH2P','3RTBTMWZ',
    'FML36VP7','AJYLHJ7M','AI5A7EEE','56PJ4B5G','IN982FWW','2B79M2U5',
    '3YCXABXP','7J3DLJCB','79UPY33L','GH6NUZP9','BD8I9AZZ','QDE8AECQ',
    'BIDWWG6B','T3DQB74P','S8PJ34KE','D2RIENW5','3ANNCNHJ','KQ74JUWM',
    'KF9UPAKC','V569QEBM','MFNXJAA6','HGNACKAX','D4IJHQHD','JXJMXHF3',
    'NYA9EEC4','69F4JZFD','DACXVW6H','LZNMFBBX','R565CYH4','8Y5PYELW',
    'URADM6KE','VM28RJCP',
]


class Command(BaseCommand):
    help = "Export metadata for PDFs whose Zotero records are missing."

    def add_arguments(self, parser):
        parser.add_argument("session_name", type=str)
        parser.add_argument(
            "--output", type=str, default=None,
            help="Output CSV path (default: missing_<session_name>.csv next to manage.py)"
        )
        parser.add_argument(
            "--aligned-only", action="store_true",
            help="Only include PDFs that aligned with the research criteria"
        )

    def handle(self, *args, **options):
        session_name = options["session_name"]
        aligned_only = options["aligned_only"]

        session = Session.objects.filter(name=session_name).first()
        if not session:
            raise CommandError(f"No session found: '{session_name}'")

        self.stdout.write(f"Session: {session.name} ({session.id})")

        zotero_links = session.zotero_links or {}

        # Build set of pdf_paths whose link key is in the missing list
        missing_set = set(MISSING_KEYS)
        import re
        LINK_RE = re.compile(r"zotero://select/(?:library|groups)(?:/\d+)?/items/([A-Z0-9]+)")

        missing_paths = set()
        for pdf_path, link in zotero_links.items():
            m = LINK_RE.match(link)
            if m and m.group(1) in missing_set:
                missing_paths.add(pdf_path)
            # Also catch by attachment key (first segment of rel_path)
            att_key = pdf_path.split("/")[0]
            if att_key in missing_set:
                missing_paths.add(pdf_path)

        self.stdout.write(f"Missing pdf_paths identified: {len(missing_paths)}")

        # Pull matching PDFJob records
        jobs = PDFJob.objects.filter(
            session=session,
            pdf_path__in=missing_paths,
            status="done",
        )
        self.stdout.write(f"PDFJob records found: {jobs.count()}")

        rows = []
        for job in jobs:
            result = job.result or {}
            fields = result.get("fields", {})

            aligns = fields.get("aligns_with_criteria", False)
            if aligned_only and not aligns:
                continue

            doi = (
                fields.get("doi")
                or result.get("doi")
                or ""
            )
            rows.append({
                "filename": os.path.basename(job.pdf_path),
                "attachment_key": job.pdf_path.split("/")[0],
                "title": fields.get("title", ""),
                "authors": fields.get("author", ""),
                "year": fields.get("year", ""),
                "doi": doi,
                "apa_reference": fields.get("apa_reference", ""),
                "aligns_with_criteria": aligns,
                "alignment_reason": fields.get("alignment_reason", ""),
            })

        # Sort: aligned first, then by author
        rows.sort(key=lambda r: (not r["aligns_with_criteria"], r["authors"].lower()))

        # Determine output path
        if options["output"]:
            out_path = options["output"]
        else:
            safe_name = session_name.replace(" ", "_").replace(",", "")
            out_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                f"../../../../missing_{safe_name}.csv"
            )
            out_path = os.path.normpath(out_path)

        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "filename", "attachment_key", "title", "authors", "year",
                "doi", "apa_reference", "aligns_with_criteria", "alignment_reason",
            ])
            writer.writeheader()
            writer.writerows(rows)

        self.stdout.write(self.style.SUCCESS(
            f"\nExported {len(rows)} records to:\n  {out_path}"
        ))

        # Print a quick summary to the terminal
        aligned_count = sum(1 for r in rows if r["aligns_with_criteria"])
        with_doi = sum(1 for r in rows if r["doi"])
        self.stdout.write(f"\nSummary:")
        self.stdout.write(f"  Total missing with job data : {len(rows)}")
        self.stdout.write(f"  Aligned with criteria       : {aligned_count}")
        self.stdout.write(f"  Have a DOI                  : {with_doi}")
        self.stdout.write(f"  Missing DOI                 : {len(rows) - with_doi}")

        if len(rows) - with_doi > 0:
            self.stdout.write("\nItems without a DOI (may need manual lookup):")
            for r in rows:
                if not r["doi"]:
                    self.stdout.write(f"  [{r['year']}] {r['authors']} — {r['title'][:80]}")
