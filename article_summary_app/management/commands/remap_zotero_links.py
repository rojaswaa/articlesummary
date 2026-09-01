"""
Management command: remap_zotero_links

Fixes zotero_links stored in a Session whose PDF links were all stamped
with the wrong library ID during browsing. For each link it:
  1. Extracts the item key
  2. Looks it up via the Zotero API (group library first, then personal)
  3. Reads the item's actual `library` field to build the correct link
  4. Saves the corrected zotero_links back to the session

Usage:
    python manage.py remap_zotero_links "Legal, Ethical, and Societal Issues"

Optional flags:
    --dry-run   Print what would change without saving to the database
    --group-id  Override the group library ID to search (default: read from profile)
"""

import re
import time
import logging

from django.core.management.base import BaseCommand, CommandError
from django.contrib.auth.models import User

from article_summary_app.models import Profile, Session, PDFJob
from pyzotero import Zotero

log = logging.getLogger(__name__)


def _make_client(user_id, lib_id, lib_type, api_key, api_mode, local=False):
    if api_mode == "local":
        return Zotero(lib_id or "0", lib_type, api_key, local=True)
    return Zotero(str(lib_id), lib_type, api_key)


def _batch_fetch(zot, keys, batch_size=50):
    """Fetch a list of item keys in batches; return dict key→item."""
    cache = {}
    for i in range(0, len(keys), batch_size):
        batch = keys[i:i + batch_size]
        try:
            results = zot.items(itemKey=",".join(batch))
            if results:
                for it in results:
                    cache[it["key"]] = it
        except Exception as e:
            log.warning(f"Batch fetch error: {e}")
        time.sleep(0.1)
    return cache


def _build_content_index(zot, limit=500):
    """
    Fetch the most recently modified items + attachments from a library
    and return two indexes: doi_index (doi→item) and filename_index (filename→item).
    `limit` controls how many top-level items and attachments to pull.
    """
    doi_index = {}
    filename_index = {}

    try:
        # Top-level items — index by DOI
        items = zot.items(sort="dateModified", direction="desc",
                          limit=limit, itemType="-attachment")
        for item in (items or []):
            data = item.get("data", {})
            doi = (data.get("DOI") or "").strip().lower()
            if doi:
                doi_index[doi] = item

        # Attachments — index by filename
        attachments = zot.items(sort="dateModified", direction="desc",
                                limit=limit, itemType="attachment")
        for att in (attachments or []):
            data = att.get("data", {})
            fname = (data.get("filename") or "").strip().lower()
            if fname:
                filename_index[fname] = att
    except Exception as e:
        log.warning(f"Content index build error: {e}")

    return doi_index, filename_index


class Command(BaseCommand):
    help = "Remap zotero_links for a session using each item's actual library metadata."

    def add_arguments(self, parser):
        parser.add_argument("session_name", type=str, help="Exact session name to fix")
        parser.add_argument("--dry-run", action="store_true", help="Print changes without saving")
        parser.add_argument("--group-id", type=str, default=None, help="Group library ID to search (overrides profile)")
        parser.add_argument("--match-collection", type=str, default=None,
                            help="Collection key to use as the source for third-pass matching (e.g. ZXCXBL45)")

    def handle(self, *args, **options):
        session_name = options["session_name"]
        dry_run = options["dry_run"]
        override_group_id = options["group_id"]

        # ── Find the session ──────────────────────────────────────────────────
        sessions = Session.objects.filter(name=session_name).select_related("user__profile")
        if not sessions.exists():
            raise CommandError(f"No session found with name: '{session_name}'")
        if sessions.count() > 1:
            self.stdout.write(self.style.WARNING(
                f"Multiple sessions match '{session_name}'. Processing all of them."
            ))

        for session in sessions:
            self._remap_session(session, dry_run, override_group_id, options.get("match_collection"))

    def _remap_session(self, session, dry_run, override_group_id, match_collection_key=None):
        self.stdout.write(f"\nSession: '{session.name}' (ID: {session.id})")
        self.stdout.write(f"User: {session.user.username}")

        profile = session.user.profile
        api_key = profile.zotero_api_key
        user_id = profile.zotero_user_id
        api_mode = profile.zotero_api_mode

        if not api_key or not user_id:
            raise CommandError("Zotero credentials not set for this user's profile.")

        self.stdout.write(f"Zotero user ID: {user_id} | Mode: {api_mode}")

        # ── Parse existing links ──────────────────────────────────────────────
        zotero_links = session.zotero_links or {}
        if not zotero_links:
            self.stdout.write(self.style.WARNING("No zotero_links stored in this session. Nothing to do."))
            return

        LINK_RE = re.compile(r"zotero://select/(library|groups)(?:/(\d+))?/items/([A-Z0-9]+)")
        # Group link-keys by pdf_path and also build an attachment-key index.
        # rel_path format is "ATTACHMENT_KEY/filename.pdf" — the attachment key
        # is the first path component, which is different from the link's item key
        # (which is the parent item key, or same as attachment key if standalone).
        key_to_paths = {}          # link_item_key → [pdf_path, ...]
        attachment_key_to_paths = {}  # attachment_key → [pdf_path, ...]

        for pdf_path, link in zotero_links.items():
            m = LINK_RE.match(link)
            if not m:
                self.stdout.write(self.style.WARNING(f"  Unrecognised link format: {link}"))
                continue
            item_key = m.group(3)
            key_to_paths.setdefault(item_key, []).append(pdf_path)

            # Extract attachment key from the rel_path (first segment before '/')
            att_key = pdf_path.split("/")[0]
            if att_key and att_key != item_key:
                attachment_key_to_paths.setdefault(att_key, []).append(pdf_path)

        all_keys = list(key_to_paths.keys())
        self.stdout.write(f"Unique item keys to resolve: {len(all_keys)}")

        # ── Build clients ─────────────────────────────────────────────────────
        personal_client = _make_client(user_id, user_id, "user", api_key, api_mode)

        # Discover ALL groups the user belongs to
        if override_group_id:
            all_group_ids = [override_group_id]
        else:
            self.stdout.write("Discovering all Zotero groups for this user...")
            try:
                groups = personal_client.groups()
                all_group_ids = [str(g["id"]) for g in (groups or [])]
                self.stdout.write(f"  Found groups: {all_group_ids}")
            except Exception as e:
                self.stdout.write(self.style.WARNING(f"  Could not fetch groups: {e}"))
                all_group_ids = []
            # Always include whichever group ID is already in the links
            for link in zotero_links.values():
                m = LINK_RE.match(link)
                if m and m.group(1) == "groups" and m.group(2):
                    gid = m.group(2)
                    if gid not in all_group_ids:
                        all_group_ids.append(gid)

        # ── Fetch items ───────────────────────────────────────────────────────
        item_cache = {}
        remaining = list(all_keys)

        for gid in all_group_ids:
            if not remaining:
                break
            client = _make_client(user_id, gid, "group", api_key, api_mode)
            self.stdout.write(f"Fetching {len(remaining)} keys from group {gid}...")
            found = _batch_fetch(client, remaining)
            item_cache.update(found)
            remaining = [k for k in remaining if k not in item_cache]
            self.stdout.write(f"  Found {len(found)} | Still missing: {len(remaining)}")

        if remaining:
            self.stdout.write(f"Fetching {len(remaining)} remaining keys from personal library...")
            found = _batch_fetch(personal_client, remaining)
            item_cache.update(found)
            remaining = [k for k in remaining if k not in item_cache]
            self.stdout.write(f"  Found {len(found)} | Still missing: {len(remaining)}")

        # Second pass: for still-missing link keys, look up via the ATTACHMENT key
        # embedded in the rel_path (first segment of "ATTACHMENT_KEY/filename.pdf").
        # This resolves cases where the stored link key is wrong/stale but the
        # attachment itself still exists; we climb to its parentItem.
        if remaining:
            self.stdout.write(f"\nSecond pass: looking up attachment keys for {len(remaining)} unresolved items...")
            # Collect the attachment keys for only the still-missing link keys
            missing_pdf_paths = set()
            for k in remaining:
                for p in key_to_paths[k]:
                    missing_pdf_paths.add(p)

            att_keys_to_fetch = [
                att_k for att_k, paths in attachment_key_to_paths.items()
                if any(p in missing_pdf_paths for p in paths)
            ]
            self.stdout.write(f"  Attachment keys to look up: {len(att_keys_to_fetch)}")

            att_cache = {}  # attachment_key → item
            for gid in all_group_ids:
                if not att_keys_to_fetch:
                    break
                client = _make_client(user_id, gid, "group", api_key, api_mode)
                found = _batch_fetch(client, att_keys_to_fetch)
                att_cache.update(found)
                att_keys_to_fetch = [k for k in att_keys_to_fetch if k not in att_cache]

            if att_keys_to_fetch:
                found = _batch_fetch(personal_client, att_keys_to_fetch)
                att_cache.update(found)

            # For each found attachment, resolve its parent and add to item_cache
            parents_to_fetch = {}  # parent_key → (lib_type, lib_id)
            for att_key, att_item in att_cache.items():
                att_data = att_item.get("data", {})
                parent_key = att_data.get("parentItem")
                att_lib = att_item.get("library", {})
                if parent_key and parent_key not in item_cache:
                    parents_to_fetch[parent_key] = (att_lib.get("type"), str(att_lib.get("id", "")))
                elif not parent_key:
                    # Standalone attachment — use it directly
                    item_cache[att_key] = att_item

            if parents_to_fetch:
                self.stdout.write(f"  Fetching {len(parents_to_fetch)} parent items discovered via attachments...")
                # Group by library for efficient batching
                by_lib = {}
                for pk, (lt, lid) in parents_to_fetch.items():
                    by_lib.setdefault((lt, lid), []).append(pk)
                for (lt, lid), pkeys in by_lib.items():
                    if lt == "user":
                        c = personal_client
                    else:
                        c = _make_client(user_id, lid, "group", api_key, api_mode)
                    found = _batch_fetch(c, pkeys)
                    item_cache.update(found)
                    self.stdout.write(f"    {lt} {lid}: found {len(found)}/{len(pkeys)}")

            # Re-map: for pdf_paths whose link key is still missing, point them
            # at the parent item found via the attachment lookup
            for att_key, att_item in att_cache.items():
                att_data = att_item.get("data", {})
                parent_key = att_data.get("parentItem")
                resolved_key = parent_key if parent_key else att_key

                for pdf_path in attachment_key_to_paths.get(att_key, []):
                    if pdf_path in missing_pdf_paths and resolved_key in item_cache:
                        # Re-register under the resolved key so the rebuild loop handles it
                        key_to_paths.setdefault(resolved_key, [])
                        if pdf_path not in key_to_paths[resolved_key]:
                            key_to_paths[resolved_key].append(pdf_path)
                        # Remove from the old (wrong) key
                        old_link_key = LINK_RE.match(zotero_links[pdf_path]).group(3)
                        if old_link_key in key_to_paths and pdf_path in key_to_paths[old_link_key]:
                            key_to_paths[old_link_key].remove(pdf_path)

            remaining = [k for k in remaining if k not in item_cache and
                         all(p not in {pp for rk in item_cache for pp in key_to_paths.get(rk, [])}
                             for p in key_to_paths.get(k, []))]

        # Third pass: re-added items have new keys — build a DOI + filename index
        # from recently-modified items across all libraries, then match locally.
        if remaining:
            self.stdout.write(f"\nThird pass: building content index for {len(remaining)} unresolved items...")

            # Collect DOI and filename targets from PDFJob results
            missing_pdf_paths = {p for k in remaining for p in key_to_paths[k]}
            jobs = PDFJob.objects.filter(
                session=session,
                pdf_path__in=missing_pdf_paths,
                status="done",
            )
            job_map = {j.pdf_path: j for j in jobs}

            # Build merged DOI→item and filename→attachment indexes.
            # If --match-collection is given, fetch directly from that collection
            # (much more targeted than scanning recent items across the whole library).
            merged_doi_index = {}
            merged_att_index = {}   # filename (lower) → attachment item

            if match_collection_key:
                # Find which group has this collection
                coll_client = None
                coll_label = None
                for gid in all_group_ids:
                    try:
                        c = _make_client(user_id, gid, "group", api_key, api_mode)
                        colls = c.collections()
                        if any(col["key"] == match_collection_key for col in (colls or [])):
                            coll_client = c
                            coll_label = f"group {gid}"
                            break
                    except Exception:
                        pass
                if not coll_client:
                    coll_client = personal_client
                    coll_label = "personal"

                self.stdout.write(f"  Fetching all items from collection {match_collection_key} in {coll_label}...")
                coll_items = coll_client.everything(coll_client.collection_items(match_collection_key))
                self.stdout.write(f"    {len(coll_items or [])} items in collection")

                for item in (coll_items or []):
                    data = item.get("data", {})
                    item_type = data.get("itemType")
                    if item_type == "attachment":
                        fname = (data.get("filename") or "").strip().lower()
                        if fname:
                            merged_att_index[fname] = item
                    elif item_type not in ("note",):
                        doi = (data.get("DOI") or "").strip().lower()
                        # Normalise: strip https://doi.org/ prefix if present
                        if doi.startswith("https://doi.org/"):
                            doi = doi[len("https://doi.org/"):]
                        if doi:
                            merged_doi_index[doi] = item

                # Also fetch children (attachments) for top-level items to build filename index
                self.stdout.write(f"  Fetching attachments for collection items...")
                top_level_keys = [
                    i["key"] for i in (coll_items or [])
                    if i.get("data", {}).get("itemType") not in ("attachment", "note")
                ]
                for i in range(0, len(top_level_keys), 50):
                    batch = top_level_keys[i:i+50]
                    for key in batch:
                        try:
                            children = coll_client.children(key)
                            for child in (children or []):
                                cdata = child.get("data", {})
                                if cdata.get("itemType") == "attachment":
                                    fname = (cdata.get("filename") or "").strip().lower()
                                    if fname:
                                        merged_att_index[fname] = child
                        except Exception:
                            pass
                    time.sleep(0.1)
                self.stdout.write(f"  Index built — DOIs: {len(merged_doi_index)} | Filenames: {len(merged_att_index)}")

            else:
                all_clients_labeled = [
                    (_make_client(user_id, gid, "group", api_key, api_mode), f"group {gid}")
                    for gid in all_group_ids
                ] + [(personal_client, "personal")]

                for client, label in all_clients_labeled:
                    self.stdout.write(f"  Indexing {label}...")
                    doi_idx, fname_idx = _build_content_index(client, limit=500)
                    merged_doi_index.update(doi_idx)
                    merged_att_index.update(fname_idx)
                    self.stdout.write(f"    DOIs: {len(doi_idx)} | Filenames: {len(fname_idx)}")
                    time.sleep(0.2)

            # For attachments, pre-fetch their parent keys so we can resolve top-level items
            parent_keys_needed = set()
            for att in merged_att_index.values():
                pk = att.get("data", {}).get("parentItem")
                if pk and pk not in item_cache:
                    parent_keys_needed.add(pk)

            if parent_keys_needed:
                self.stdout.write(f"  Fetching {len(parent_keys_needed)} parent items for indexed attachments...")
                for client, label in all_clients_labeled:
                    if not parent_keys_needed:
                        break
                    found = _batch_fetch(client, list(parent_keys_needed))
                    item_cache.update(found)
                    parent_keys_needed -= set(found.keys())

            # Match each missing pdf_path against the indexes
            found_by_content = {}

            for pdf_path in sorted(missing_pdf_paths):
                job = job_map.get(pdf_path)
                if not job or not job.result:
                    continue

                fields = job.result.get("fields", {})
                doi = (fields.get("doi") or job.result.get("doi") or "").strip().lower()
                if doi.startswith("https://doi.org/"):
                    doi = doi[len("https://doi.org/"):]
                filename = pdf_path.split("/")[-1].lower()

                matched_item = None

                # Try DOI
                if doi and doi in merged_doi_index:
                    matched_item = merged_doi_index[doi]

                # Try filename → climb to parent
                if not matched_item and filename in merged_att_index:
                    att = merged_att_index[filename]
                    parent_key = att.get("data", {}).get("parentItem")
                    if parent_key and parent_key in item_cache:
                        matched_item = item_cache[parent_key]
                    elif not parent_key:
                        matched_item = att  # standalone attachment

                if matched_item:
                    found_by_content[pdf_path] = matched_item
                    title = matched_item.get("data", {}).get("title", "")[:60]
                    self.stdout.write(f"  ✓ {pdf_path.split('/')[-1][:50]} → {title}")
                else:
                    self.stdout.write(f"  ✗ No match: {pdf_path.split('/')[-1][:70]}")

            # Register found items into the key_to_paths map for the rebuild loop
            for pdf_path, item in found_by_content.items():
                new_key = item["key"]
                item_cache[new_key] = item
                key_to_paths.setdefault(new_key, [])
                if pdf_path not in key_to_paths[new_key]:
                    key_to_paths[new_key].append(pdf_path)
                old_m = LINK_RE.match(zotero_links.get(pdf_path, ""))
                if old_m:
                    old_key = old_m.group(3)
                    if old_key in key_to_paths and pdf_path in key_to_paths[old_key]:
                        key_to_paths[old_key].remove(pdf_path)

            still_unresolved = len(missing_pdf_paths) - len(found_by_content)
            self.stdout.write(
                f"\nThird pass result: {len(found_by_content)} matched | {still_unresolved} still unresolved"
            )

        # ── Rebuild links ─────────────────────────────────────────────────────
        new_links = dict(zotero_links)  # start from current, overwrite what we can fix
        changed = 0
        unchanged = 0
        skipped = 0

        for item_key, pdf_paths in key_to_paths.items():
            item = item_cache.get(item_key)
            if not item:
                skipped += len(pdf_paths)
                continue

            item_lib = item.get("library", {})
            actual_type = item_lib.get("type")   # "user" or "group"
            actual_id = item_lib.get("id")       # numeric int

            if actual_type == "user":
                correct_link = f"zotero://select/library/items/{item_key}"
            elif actual_type == "group" and actual_id:
                correct_link = f"zotero://select/groups/{actual_id}/items/{item_key}"
            else:
                self.stdout.write(self.style.WARNING(f"  Cannot determine library for {item_key}, skipping"))
                skipped += len(pdf_paths)
                continue

            for pdf_path in pdf_paths:
                old_link = zotero_links.get(pdf_path, "")
                if old_link != correct_link:
                    self.stdout.write(f"  CHANGE  {pdf_path}")
                    self.stdout.write(f"    old: {old_link}")
                    self.stdout.write(f"    new: {correct_link}")
                    new_links[pdf_path] = correct_link
                    changed += 1
                else:
                    unchanged += 1

        # ── Summary ───────────────────────────────────────────────────────────
        self.stdout.write(f"\nSummary: {changed} changed | {unchanged} already correct | {skipped} skipped (not found)")

        if changed == 0:
            self.stdout.write(self.style.SUCCESS("Nothing to update."))
            return

        if dry_run:
            self.stdout.write(self.style.WARNING("DRY RUN — no changes saved."))
        else:
            session.zotero_links = new_links
            session.save(update_fields=["zotero_links"])
            self.stdout.write(self.style.SUCCESS(f"Saved corrected zotero_links to session '{session.name}'."))
