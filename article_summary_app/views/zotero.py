"""Zotero integration: library loading and saving aligned articles back to collections.

The heavy lifting lives in two event generators (`_load_zotero_pdfs_events`,
`_save_to_zotero_events`). The SSE views stream their events to the client;
the plain JSON views drain them and return only the final payload.
"""
import json
import os
import re
import tempfile
import threading
import time
from collections import Counter, defaultdict

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404

import httpx
from pyzotero.zotero import Zotero

from ..analyzer import is_aligned
from ..crossref import query_crossref
from ..models import Session, ArticleSearch
from ..unpaywall import get_oa_pdf_url
from .common import get_user_profile, log, ZOTERO_STORAGE

ZOTERO_LINK_RE = r'zotero://select/(library|groups)(?:/(\d+))?/items/([A-Z0-9]+)'
BATCH_SIZE = 50


def get_zotero_config(user):
    profile = get_user_profile(user)
    return {
        "user_id": profile.zotero_user_id,
        "api_key": profile.zotero_api_key,
        "library_type": profile.zotero_library_type,
        "api_mode": profile.zotero_api_mode,
    }


def get_zotero_client(user, lib_id=None, lib_type=None, api_key=None):
    config = get_zotero_config(user)
    uid = str(lib_id or config["user_id"] or "").strip()
    ltype = lib_type or config["library_type"]
    akey = api_key or config["api_key"]

    if uid == "undefined": uid = ""
    if ltype == "undefined": ltype = "user"

    if not uid: return None

    if config["api_mode"] == "local":
        return Zotero(uid or "0", ltype, akey, local=True)

    if not akey: return None
    return Zotero(uid, ltype, akey)


def _zotero_api_call(func, *args, **kwargs):
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            if "429" in str(e):
                time.sleep((attempt + 1) * 3)
                continue
            if attempt == max_retries - 1: raise
            time.sleep(1)
    return None


def _extract_author_info(creators):
    """Return (sort_key, display_string) from a Zotero creators list."""
    if not creators:
        return ("", "Unknown")
    authors = [c for c in creators if c.get("creatorType") == "author"] or creators[:1]
    first = authors[0]
    last = first.get("lastName") or (first.get("name", "").split()[-1] if first.get("name") else "")
    if len(authors) == 1:
        display = last or first.get("name", "Unknown")
    elif len(authors) == 2:
        last2 = authors[1].get("lastName") or (authors[1].get("name", "").split()[-1] if authors[1].get("name") else "")
        display = f"{last} & {last2}"
    else:
        display = f"{last} et al."
    return (last.lower(), display)


@login_required
def zotero_collections(request):
    config = get_zotero_config(request.user)
    zot = get_zotero_client(request.user)
    if not zot:
        return JsonResponse({"error": "Zotero credentials not set", "libraries": []})

    try:
        libraries = []
        # Main Library
        collections = _zotero_api_call(zot.collections, limit=100)
        if collections:
            libraries.append({
                "id": config["user_id"],
                "name": "My Library",
                "type": config["library_type"],
                "collections": [{"id": c["key"], "name": c["data"]["name"]} for c in collections]
            })

        # Groups (only if type is user)
        if config["library_type"] == "user":
            groups = _zotero_api_call(zot.groups)
            if groups:
                for group in groups:
                    group_id = str(group["id"])
                    group_zot = get_zotero_client(request.user, lib_id=group_id, lib_type="group")
                    group_collections = _zotero_api_call(group_zot.collections, limit=100)
                    if group_collections:
                        libraries.append({
                            "id": group_id,
                            "name": group["data"]["name"],
                            "type": "group",
                            "collections": [{"id": c["key"], "name": c["data"]["name"]} for c in group_collections]
                        })
        return JsonResponse({"libraries": libraries})
    except Exception as e:
        log.exception(f"[Zotero Collections] Failed for user={request.user.username} "
                      f"(library_type={config['library_type']}, user_id={config['user_id']}): {e}")
        return JsonResponse({"error": str(e), "libraries": []})


def _process_zotero_item(item, pdfs, seen_paths, zotero_links, zotero_metadata,
                         library_type, library_id, parent_meta_lookup):
    """Process one Zotero item. PDF attachments that exist on disk get added to
    pdfs; everything else is skipped (top-level metadata is captured in the
    pre-pass)."""
    data = item.get("data", {})
    item_key = item.get("key")

    if data.get("itemType") != "attachment":
        return
    if data.get("contentType") != "application/pdf":
        return

    parent_key = data.get("parentItem")
    filename = data.get("filename")
    if not filename:
        return

    rel_path = os.path.join(item_key, filename)
    full_path = os.path.join(ZOTERO_STORAGE, rel_path)

    if data.get("linkMode") == "linked_file" and data.get("path"):
        full_path = data.get("path")
        rel_path = filename

    if not os.path.exists(full_path):
        return
    if rel_path in seen_paths:
        return
    seen_paths.add(rel_path)

    pdfs.append(rel_path)
    item_to_link = parent_key if parent_key else item_key

    item_lib = item.get("library", {})
    actual_lib_type = item_lib.get("type", library_type)
    actual_lib_id = item_lib.get("id")

    if actual_lib_type == "user":
        zotero_links[rel_path] = f"zotero://select/library/items/{item_to_link}"
    elif actual_lib_id:
        zotero_links[rel_path] = f"zotero://select/groups/{actual_lib_id}/items/{item_to_link}"
    else:
        zotero_links[rel_path] = f"zotero://select/groups/{library_id}/items/{item_to_link}"

    meta = (parent_meta_lookup or {}).get(parent_key) or {}
    zotero_metadata[rel_path] = {
        "title":          meta.get("title", ""),
        "author_sort":    meta.get("author_sort", ""),
        "author_display": meta.get("author_display", ""),
        "year":           meta.get("year", ""),
        "date_modified":  data.get("dateModified", ""),
        "parent_key":     item_to_link,
    }


def _group_articles(pdfs, zotero_metadata):
    """Group PDF paths by parent article; newest version of each is selected."""
    parent_groups = defaultdict(list)
    for rel_path in pdfs:
        meta = zotero_metadata.get(rel_path, {})
        pk = meta.get("parent_key") or rel_path.split(os.sep)[0]
        parent_groups[pk].append((rel_path, meta))

    article_groups = []
    for pk, versions in parent_groups.items():
        versions.sort(key=lambda x: x[1].get("date_modified", ""), reverse=True)
        fm = versions[0][1]
        sort_key = fm.get("author_sort", "") or fm.get("title", "").lower()
        article_groups.append({
            "parent_key":     pk,
            "title":          fm.get("title") or versions[0][0].split(os.sep)[-1],
            "author_sort":    fm.get("author_sort", ""),
            "author_display": fm.get("author_display", ""),
            "year":           fm.get("year", ""),
            "_sort_key":      sort_key,
            "versions": [
                {"path": p, "date_modified": m.get("date_modified", ""), "selected": i == 0}
                for i, (p, m) in enumerate(versions)
            ],
        })

    # Sort: author A→Z (fall back to title when author is unknown), then year
    article_groups.sort(key=lambda g: (g["_sort_key"], g["year"]))
    for g in article_groups:
        del g["_sort_key"]   # don't send internal field to client

    pdfs_ordered = [g["versions"][0]["path"] for g in article_groups]
    return article_groups, pdfs_ordered


# ── Library loading ──

def _load_zotero_pdfs_events(user, collection_id, library_id, library_type):
    """Yield progress events while loading PDFs from a Zotero library.
    The final event has step='complete' and carries the full payload."""
    user_label = f"user={user.username}"
    coll_label = f"collection={collection_id or 'all'}"

    def _evt(step, message, data=None):
        if step == "error":
            log.error(f"[Zotero Load] [{user_label}] [{coll_label}] ERROR: {message}")
        else:
            log.info(f"[Zotero Load] [{user_label}] [{coll_label}] [{step}] {message}")
        return {"step": step, "message": message, **(data or {})}

    log.info(f"[Zotero Load] ── New request ── {user_label} | {coll_label} | "
             f"library={library_id or 'personal'} type={library_type}")
    yield _evt("connecting", "Connecting to Zotero library...")

    zot = get_zotero_client(user, lib_id=library_id, lib_type=library_type)
    if not zot:
        yield _evt("error", "Failed to create Zotero client — check credentials in Settings.")
        return

    # ── Fetch items ──
    if collection_id and collection_id != "all":
        yield _evt("fetching", f"Fetching items from collection {collection_id}...")
        all_items = _zotero_api_call(zot.everything, zot.collection_items(collection_id))
    else:
        yield _evt("fetching", "Fetching all items from library...")
        all_items = _zotero_api_call(zot.everything, zot.items())

    all_items = all_items or []
    type_counts = Counter(i.get("data", {}).get("itemType", "unknown") for i in all_items)
    log.info(f"[Zotero Load] Retrieved {len(all_items)} items: " +
             ", ".join(f"{t}={c}" for t, c in sorted(type_counts.items())))
    yield _evt("fetching_done", f"Retrieved {len(all_items)} items from Zotero.",
               {"item_count": len(all_items)})

    # ── Pass 1: parent metadata index ──
    yield _evt("metadata", "Building article metadata index...")
    parent_meta_lookup = {}
    no_creators = 0
    for item in all_items:
        data = item.get("data", {})
        if data.get("itemType") not in ("attachment", "note"):
            creators = data.get("creators", [])
            if not creators:
                no_creators += 1
            sort_key, display = _extract_author_info(creators)
            raw_date = data.get("date", "") or ""
            year = raw_date[:4] if len(raw_date) >= 4 else raw_date
            parent_meta_lookup[item["key"]] = {
                "title":          data.get("title", ""),
                "author_sort":    sort_key,
                "author_display": display,
                "year":           year,
            }
    log.info(f"[Zotero Load] Metadata index: {len(parent_meta_lookup)} articles indexed "
             f"({no_creators} had no creator info).")
    yield _evt("metadata_done", f"Indexed {len(parent_meta_lookup)} articles.",
               {"article_count": len(parent_meta_lookup)})

    # ── Pass 2: scan local storage ──
    yield _evt("scanning", "Scanning local Zotero storage for PDFs...")
    pdfs, seen_paths = [], set()
    zotero_links, zotero_metadata = {}, {}
    not_on_disk = 0
    orphan_attachments = 0

    for item in all_items:
        data = item.get("data", {})
        pk_hint = data.get("parentItem")
        if data.get("itemType") == "attachment" and data.get("contentType") == "application/pdf":
            if pk_hint and pk_hint not in parent_meta_lookup:
                orphan_attachments += 1
            filename = data.get("filename", "")
            if filename:
                full = os.path.join(ZOTERO_STORAGE, item["key"], filename)
                if data.get("linkMode") == "linked_file" and data.get("path"):
                    full = data["path"]
                if not os.path.exists(full):
                    not_on_disk += 1
        _process_zotero_item(item, pdfs, seen_paths, zotero_links, zotero_metadata,
                             library_type, library_id, parent_meta_lookup)

    log.info(f"[Zotero Load] Storage scan complete: {len(pdfs)} PDFs found | "
             f"{not_on_disk} PDF attachments not on disk | "
             f"{orphan_attachments} attachments with no indexed parent.")
    yield _evt("scanning_done", f"Found {len(pdfs)} PDFs on disk.", {"pdf_count": len(pdfs)})

    # ── Group & sort ──
    yield _evt("grouping", "Grouping and sorting articles...")
    article_groups, pdfs_ordered = _group_articles(pdfs, zotero_metadata)
    dup_count = sum(1 for g in article_groups if len(g["versions"]) > 1)
    no_author = sum(1 for g in article_groups if not g["author_sort"])
    log.info(f"[Zotero Load] Grouping complete: {len(article_groups)} unique articles | "
             f"{dup_count} with multiple PDF versions | {no_author} without author metadata.")
    yield _evt("grouping_done",
               f"Sorted {len(article_groups)} unique articles. {dup_count} duplicate PDF(s) collapsed.",
               {"dup_count": dup_count, "article_count": len(article_groups)})

    yield _evt("complete", "Ready.", {
        "path":            ZOTERO_STORAGE,
        "pdfs":            pdfs_ordered,
        "zotero_links":    zotero_links,
        "zotero_metadata": zotero_metadata,
        "article_groups":  article_groups,
    })


def _load_params(request):
    return (
        request.GET.get("collection_id"),
        request.GET.get("library_id"),
        request.GET.get("library_type", get_user_profile(request.user).zotero_library_type),
    )


@login_required
def zotero_pdfs(request):
    collection_id, library_id, library_type = _load_params(request)
    final, error = None, None
    try:
        for evt in _load_zotero_pdfs_events(request.user, collection_id, library_id, library_type):
            if evt["step"] == "complete":
                final = evt
            elif evt["step"] == "error":
                error = evt["message"]
    except Exception as e:
        log.exception(f"[Zotero Load] Unhandled exception: {e}")
        error = str(e)

    if final:
        return JsonResponse({k: final[k] for k in
                             ("path", "pdfs", "zotero_links", "zotero_metadata", "article_groups")})
    return JsonResponse({"error": error or "Unknown error", "pdfs": [],
                         "zotero_links": {}, "zotero_metadata": {}, "article_groups": []})


@login_required
def zotero_pdfs_stream(request):
    """SSE endpoint that streams progress events while loading Zotero PDFs."""
    collection_id, library_id, library_type = _load_params(request)

    def _generate():
        try:
            for evt in _load_zotero_pdfs_events(request.user, collection_id, library_id, library_type):
                yield f"data: {json.dumps(evt)}\n\n"
        except Exception as e:
            log.exception(f"[Zotero Load] Unhandled exception: {e}")
            yield f"data: {json.dumps({'step': 'error', 'message': f'Error: {e}'})}\n\n"

    response = StreamingHttpResponse(_generate(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


# ── Saving aligned articles back to Zotero ──

def _save_to_zotero_events(user, session):
    """Yield progress events while saving aligned articles into a Zotero collection.
    The final event has step='complete' with the summary counts."""

    def _evt(step, message, **extra):
        if step == "error":
            log.error(f"[Zotero Save] {message}")
        elif step == "item_moved":
            log.info(f"[Zotero Save]   ✓ {message.lstrip('✓ ')}")
        else:
            log.info(f"[Zotero Save] [{step}] {message}")
        return {"step": step, "message": message, **extra}

    log.info("[Zotero Save] ── Starting export ──────────────────────────")
    yield _evt("auditing", "Auditing aligned PDFs...")
    library_items = {}
    total_aligned = 0
    with_links = 0

    for job in session.jobs.filter(status='done'):
        fields = job.result.get("fields", {}) if job.result else {}
        if not is_aligned(fields):
            continue
        total_aligned += 1
        z_link = session.zotero_links.get(job.pdf_path, "")
        if not z_link:
            fname = job.pdf_path.split('/')[-1]
            for path, link in session.zotero_links.items():
                if path.endswith(fname):
                    z_link = link
                    break
        if not z_link:
            continue
        with_links += 1
        m = re.match(ZOTERO_LINK_RE, z_link)
        if m:
            lib_type = 'user' if m.group(1) == 'library' else 'group'
            gk = (lib_type, m.group(2))
            library_items.setdefault(gk, set()).add(m.group(3))

    unique_ids = sum(len(s) for s in library_items.values())
    yield _evt("auditing_done",
               f"{total_aligned} aligned PDFs | {with_links} had links | {unique_ids} unique IDs",
               total_aligned=total_aligned, with_links=with_links, unique_ids=unique_ids)

    if not library_items:
        yield _evt("error", f"No Zotero links found. Aligned: {total_aligned}, With links: {with_links}")
        return

    coll_name = f"Aligned: {session.name}" if session.name else f"Aligned: Session {str(session.id)[:8]}"
    saved_count = 0

    def _fetch_into_cache(client, keys, cache, tag_personal=False):
        for i in range(0, len(keys), BATCH_SIZE):
            batch = keys[i:i + BATCH_SIZE]
            fetched = _zotero_api_call(client.items, itemKey=','.join(batch))
            if fetched:
                for it in fetched:
                    if tag_personal:
                        it['_actual_lib_type'] = 'user'
                        it['_actual_lib_id'] = None
                    cache[it['key']] = it

    for (lib_type, lib_id), item_keys_set in library_items.items():
        item_keys = list(item_keys_set)
        lib_label = f"{'Personal' if lib_type == 'user' else 'Group'} library{' ' + lib_id if lib_id else ''}"
        client_lib_id = None if lib_type == 'user' else lib_id
        zot = get_zotero_client(user, lib_id=client_lib_id, lib_type=lib_type)

        if not zot:
            yield _evt("error", f"Could not connect to {lib_label}.")
            continue

        yield _evt("connecting", f"Connected to {lib_label}.", lib_label=lib_label)

        # ── Step 1: Fetch items ──
        yield _evt("fetching", f"Fetching {len(item_keys)} items from {lib_label}...")
        item_cache = {}
        _fetch_into_cache(zot, item_keys, item_cache)

        # Keys not found in a group library may belong to the personal library:
        # a group collection can contain items whose PDFs live in the user's
        # personal library, so their links carry the group ID incorrectly.
        missing_keys = [k for k in item_keys if k not in item_cache]
        if missing_keys and lib_type == 'group':
            yield _evt("fetching_fallback",
                       f"{len(missing_keys)} keys missing from group — checking personal library...",
                       missing=len(missing_keys))
            personal_zot = get_zotero_client(user, lib_id=None, lib_type='user')
            if personal_zot:
                _fetch_into_cache(personal_zot, missing_keys, item_cache, tag_personal=True)

        yield _evt("fetching_done", f"Fetched {len(item_cache)} of {len(item_keys)} items.",
                   fetched=len(item_cache), total=len(item_keys))

        # ── Step 2: Find or create collection ──
        yield _evt("collection", f'Looking up collection "{coll_name}"...')
        new_coll_key = None
        try:
            existing_colls = _zotero_api_call(zot.collections, limit=100)
            if existing_colls:
                for ec in existing_colls:
                    if ec.get('data', {}).get('name') == coll_name:
                        new_coll_key = ec['key']
                        break
        except Exception as e:
            log.warning(f"[Zotero Save] Collection lookup failed: {e}")

        if new_coll_key:
            yield _evt("collection_found", f'Found existing collection "{coll_name}".', coll_key=new_coll_key)
        else:
            yield _evt("collection_creating", f'Creating new collection "{coll_name}"...')
            coll_resp = _zotero_api_call(zot.create_collections, [{'name': coll_name}])
            if isinstance(coll_resp, list) and coll_resp:
                new_coll_key = coll_resp[0].get('key')
            elif isinstance(coll_resp, dict) and coll_resp.get('success'):
                new_coll_key = list(coll_resp['success'].values())[0]
            if new_coll_key:
                yield _evt("collection_created", f'Created collection "{coll_name}".', coll_key=new_coll_key)
            else:
                yield _evt("error", f'Failed to create collection "{coll_name}".')
                continue

        # ── Step 3: Resolve parents (attachments climb to their parentItem) ──
        yield _evt("resolving", f"Resolving parent items for {len(item_keys)} keys...")
        parent_keys_to_move = set()
        parent_keys_to_fetch = set()
        for k in item_keys:
            item_data = item_cache.get(k)
            if item_data:
                d = item_data.get('data', {})
                if d.get('itemType') == 'attachment' and d.get('parentItem'):
                    pk = d['parentItem']
                    parent_keys_to_move.add(pk)
                    if pk not in item_cache:
                        parent_keys_to_fetch.add(pk)
                else:
                    parent_keys_to_move.add(k)
            else:
                log.warning(f"[Zotero Save]   Item {k} not found in cache — skipping.")

        if parent_keys_to_fetch:
            yield _evt("resolving_parents", f"Fetching {len(parent_keys_to_fetch)} parent items...",
                       count=len(parent_keys_to_fetch))
            pf_list = list(parent_keys_to_fetch)
            _fetch_into_cache(zot, pf_list, item_cache)
            still_missing = [k for k in pf_list if k not in item_cache]
            if still_missing and lib_type == 'group':
                pzot = get_zotero_client(user, lib_id=None, lib_type='user')
                if pzot:
                    _fetch_into_cache(pzot, still_missing, item_cache, tag_personal=True)

        yield _evt("resolving_done", f"{len(parent_keys_to_move)} unique articles to move.",
                   count=len(parent_keys_to_move))

        # ── Step 4: Move articles into collection ──
        yield _evt("moving", f"Adding {len(parent_keys_to_move)} articles to collection...")
        moved = 0
        failed = 0
        for p_key in list(parent_keys_to_move):
            try:
                item_to_update = item_cache.get(p_key) or _zotero_api_call(zot.item, p_key)
                if not item_to_update:
                    failed += 1
                    log.warning(f"[Zotero Save]   Item {p_key} not found — skipping.")
                    continue
                d = item_to_update.get('data', {})
                if not d:
                    failed += 1
                    continue

                # Use the item's actual library client (may differ from the loop's library)
                actual_lib_type = item_to_update.get('_actual_lib_type', lib_type)
                actual_lib_id = item_to_update.get('_actual_lib_id', lib_id)
                item_client = (
                    get_zotero_client(user, lib_id=actual_lib_id, lib_type=actual_lib_type)
                    if actual_lib_type != lib_type or actual_lib_id != lib_id
                    else zot
                )

                current_colls = d.get('collections', [])
                if new_coll_key not in current_colls:
                    current_colls.append(new_coll_key)
                    d['collections'] = current_colls
                    if _zotero_api_call(item_client.update_item, item_to_update):
                        moved += 1
                        saved_count += 1
                        title = d.get('title', 'Unknown')[:45]
                        yield _evt("item_moved", f"✓ {title}...",
                                   moved=moved, total=len(parent_keys_to_move))
                    else:
                        failed += 1
                        log.error(f"[Zotero Save]   update_item failed for {p_key}")
                else:
                    moved += 1
                    saved_count += 1
                    log.info(f"[Zotero Save]   Already in collection: {d.get('title', '')[:45]}")
                time.sleep(0.05)
            except Exception as err:
                failed += 1
                log.error(f"[Zotero Save]   Error moving {p_key}: {err}")

        yield _evt("moving_done", f'Saved {moved} articles to "{coll_name}". {failed} failed.',
                   moved=moved, failed=failed, coll_name=coll_name)

    log.info(f"[Zotero Save] ── Export complete: {saved_count} articles saved ──")
    yield _evt("complete", f"Done. {saved_count} articles saved to Zotero.",
               saved_count=saved_count, total_aligned=total_aligned, unique_articles=unique_ids)


# ── Pushing search results (new items) to Zotero ──

def _parse_creators(authors: str) -> list:
    """Split a harmonized 'Given Family, Given Family' author string into Zotero
    creator records."""
    creators = []
    for name in (authors or "").split(","):
        name = name.strip()
        if not name:
            continue
        parts = name.split()
        if len(parts) == 1:
            creators.append({"creatorType": "author", "firstName": "", "lastName": parts[0]})
        else:
            creators.append({"creatorType": "author",
                             "firstName": " ".join(parts[:-1]), "lastName": parts[-1]})
    return creators[:100]


# In-memory progress for the background push, keyed by search_id. Fine for a
# local single-process tool; lost on restart (the worker dies then anyway).
_ZOTERO_PUSH: dict[str, dict] = {}


def _zotero_item_from_article(article, template, enrich):
    """Build a Zotero journalArticle from an article, enriched via Crossref by
    DOI when `enrich` is set (fuller authors, journal, volume/issue/pages, date)."""
    title = article.title or ""
    authors = article.authors or ""
    abstract = article.abstract or ""
    venue = article.venue or ""
    date = article.year or ""
    volume = issue = pages = ""

    if enrich and article.doi:
        cr = query_crossref(article.doi) or {}
        title = cr.get("title") or title
        authors = cr.get("author") or authors
        abstract = cr.get("abstract") or abstract
        venue = cr.get("journal") or venue
        date = cr.get("published_date") or cr.get("year") or date
        volume, issue, pages = cr.get("volume", ""), cr.get("issue", ""), cr.get("pages", "")

    item = dict(template)
    item.update({
        "title": title, "creators": _parse_creators(authors),
        "abstractNote": abstract, "publicationTitle": venue, "date": date,
        "volume": volume, "issue": issue, "pages": pages,
        "DOI": article.doi or "", "url": article.url or "",
    })
    return item


_PDF_HEADERS = {"User-Agent": "Mozilla/5.0 (ArticleSummary; +https://example.com)"}


def _download_pdf(url):
    """Fetch a PDF's bytes, tolerating the flaky TLS of many OA hosts. Returns
    the bytes only if the response really is a PDF (magic header)."""
    # ponytail: OA PDFs are public, no credentials sent — so on a cert-chain
    # failure we retry with verification off rather than lose the file.
    for verify in (True, False):
        try:
            r = httpx.get(url, follow_redirects=True, timeout=60,
                          headers=_PDF_HEADERS, verify=verify)
        except httpx.HTTPError as e:
            if verify:  # cert error or transient — try once more, unverified
                continue
            log.warning(f"[Zotero Push] PDF download failed for {url}: {e}")
            return None
        if r.status_code != 200:
            log.warning(f"[Zotero Push] PDF download got {r.status_code} for {url}")
            return None
        content = r.content
        if content[:5] == b"%PDF-" or "pdf" in r.headers.get("content-type", "").lower():
            return content
        log.warning(f"[Zotero Push] {url} did not return a PDF (content-type "
                    f"{r.headers.get('content-type', '?')}).")
        return None
    return None


def _attach_oa_pdf(zot, parent_key, doi, email):
    """Download the OA PDF for a DOI (via Unpaywall) and attach it. Returns
    'attached', 'no_oa' (no open-access PDF exists) or 'failed' (download/upload
    error)."""
    pdf_url = get_oa_pdf_url(doi, email)
    if not pdf_url:
        return "no_oa"
    content = _download_pdf(pdf_url)
    if not content:
        return "failed"
    # A real filename (pyzotero uses it as the attachment title/filename).
    fname = re.sub(r"[^A-Za-z0-9._-]", "_", doi.strip()) + ".pdf"
    tmp_dir = tempfile.mkdtemp()
    tmp_path = os.path.join(tmp_dir, fname)
    try:
        with open(tmp_path, "wb") as f:
            f.write(content)
        result = _zotero_api_call(zot.attachment_simple, [tmp_path], parent_key)
        # upload() returns {'success': [...], 'failure': [...], 'unchanged': [...]}
        if isinstance(result, dict):
            log.info(f"[Zotero Push] upload {doi}: success={len(result.get('success', []))} "
                     f"unchanged={len(result.get('unchanged', []))} failure={len(result.get('failure', []))}")
            if result.get("failure") or not (result.get("success") or result.get("unchanged")):
                return "failed"
        return "attached"
    except Exception as e:
        log.warning(f"[Zotero Push] PDF attach failed for {doi}: {e}")
        return "failed"
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        os.rmdir(tmp_dir)


def _run_zotero_push(user_id, search_id, opts):
    prog = _ZOTERO_PUSH[search_id]
    try:
        user = User.objects.get(id=user_id)
        search = ArticleSearch.objects.get(id=search_id)
        zot = get_zotero_client(user, lib_id=opts["library_id"], lib_type=opts["library_type"])
        if not zot:
            prog.update(done=True, error="Could not connect to the chosen Zotero library.")
            return
        log.info(f"[Zotero Push] {str(search_id)[:8]}: target library "
                 f"{zot.library_type}/{zot.library_id}")

        coll_name = f"Search: {search.name}" if search.name else f"Search: {search.query[:40]}"
        prog["collection"] = coll_name

        # Find or create the collection under the chosen parent.
        coll_key = None
        for ec in (_zotero_api_call(zot.everything, zot.collections()) or []):
            d = ec.get("data", {})
            if d.get("name") == coll_name and (d.get("parentCollection") or None) == opts["parent_collection"]:
                coll_key = ec["key"]
                break
        if not coll_key:
            cdata = {"name": coll_name}
            if opts["parent_collection"]:
                cdata["parentCollection"] = opts["parent_collection"]
            resp = _zotero_api_call(zot.create_collections, [cdata])
            if isinstance(resp, dict) and resp.get("success"):
                coll_key = list(resp["success"].values())[0]
        if not coll_key:
            prog.update(done=True, error=f'Could not create collection "{coll_name}".')
            return

        existing_dois = set()
        for it in (_zotero_api_call(zot.everything, zot.collection_items(coll_key)) or []):
            doi = (it.get("data", {}).get("DOI") or "").strip().lower()
            if doi:
                existing_dois.add(doi)

        aligned = [a for a in search.articles.filter(status="done") if is_aligned(a.evaluation)]
        template = _zotero_api_call(zot.item_template, "journalArticle")

        for a in aligned:
            if a.doi and a.doi.strip().lower() in existing_dois:
                prog["skipped"] += 1
                prog["processed"] += 1
                continue
            item = _zotero_item_from_article(a, template, opts["enrich"])
            item["collections"] = [coll_key]
            resp = _zotero_api_call(zot.create_items, [item])
            key = None
            if isinstance(resp, dict) and resp.get("success"):
                key = list(resp["success"].values())[0]
                prog["saved"] += 1
            else:
                log.warning(f"[Zotero Push] create failed: {(resp or {}).get('failed')}")
            if key and opts["fetch_pdf"] and a.doi:
                outcome = _attach_oa_pdf(zot, key, a.doi, opts["email"])
                if outcome == "attached":
                    prog["pdfs"] += 1
                elif outcome == "failed":
                    prog["pdf_failed"] += 1
            prog["processed"] += 1

        prog["done"] = True
        log.info(f"[Zotero Push] {str(search_id)[:8]}: saved {prog['saved']}, skipped "
                 f"{prog['skipped']}, PDFs {prog['pdfs']} ({prog['pdf_failed']} failed) → '{coll_name}'")
    except Exception as e:
        log.exception(f"[Zotero Push] worker failed for {search_id}: {e}")
        prog.update(done=True, error=str(e))


@login_required
def search_zotero_save(request, search_id):
    """Kick off a background push of a search's aligned articles to Zotero:
    creates enriched items (Crossref by DOI) and attaches the OA PDF (Unpaywall)
    when available. Progress is polled via search_zotero_status."""
    search = get_object_or_404(ArticleSearch, id=search_id, user=request.user)
    if get_zotero_config(request.user)["api_mode"] == "local":
        return JsonResponse({"error": "Saving to Zotero needs the Web API. Set Zotero API mode "
                                      "to 'remote' with an API key in Settings."}, status=400)
    if not get_zotero_client(request.user):
        return JsonResponse({"error": "Zotero credentials not set — configure them in Settings."}, status=400)

    existing = _ZOTERO_PUSH.get(str(search_id))
    if existing and not existing.get("done"):
        return JsonResponse({"ok": True, "already_running": True})

    aligned_count = sum(1 for a in search.articles.filter(status="done") if is_aligned(a.evaluation))
    if not aligned_count:
        return JsonResponse({"error": "No aligned articles to save."}, status=400)

    try:
        body = json.loads(request.body or "{}")
    except (json.JSONDecodeError, ValueError):
        body = {}
    opts = {
        "library_id": (body.get("library_id") or "").strip() or None,
        "library_type": (body.get("library_type") or "").strip() or None,
        "parent_collection": (body.get("parent_collection") or "").strip() or None,
        "enrich": body.get("enrich", True),
        "fetch_pdf": body.get("fetch_pdf", True),
        "email": request.user.email or "articlesummary@example.com",
    }
    _ZOTERO_PUSH[str(search_id)] = {
        "total": aligned_count, "processed": 0, "saved": 0, "skipped": 0,
        "pdfs": 0, "pdf_failed": 0, "collection": "", "done": False, "error": None,
    }
    threading.Thread(target=_run_zotero_push,
                     args=(request.user.id, str(search_id), opts), daemon=True).start()
    return JsonResponse({"ok": True, "total": aligned_count})


@login_required
def search_zotero_status(request, search_id):
    get_object_or_404(ArticleSearch, id=search_id, user=request.user)
    prog = _ZOTERO_PUSH.get(str(search_id))
    if not prog:
        return JsonResponse({"idle": True})
    return JsonResponse(prog)


@login_required
def analyze_zotero_save(request, session_id):
    session = get_object_or_404(Session, id=session_id, user=request.user)
    final, error = None, None
    try:
        for evt in _save_to_zotero_events(request.user, session):
            if evt["step"] == "complete":
                final = evt
            elif evt["step"] == "error" and not error:
                error = evt["message"]
    except Exception as e:
        log.exception(f"[Zotero Save] Unhandled exception: {e}")
        error = str(e)

    if final:
        return JsonResponse({"ok": True, "saved_count": final["saved_count"],
                             "total_aligned": final["total_aligned"],
                             "unique_articles": final["unique_articles"]})
    return JsonResponse({"error": error or "Unknown error"}, status=400)


@login_required
def analyze_zotero_save_stream(request, session_id):
    """SSE endpoint that streams step-by-step progress while saving to Zotero."""
    session = get_object_or_404(Session, id=session_id, user=request.user)

    def _generate():
        try:
            for evt in _save_to_zotero_events(request.user, session):
                yield f"data: {json.dumps(evt)}\n\n"
        except Exception as e:
            log.exception(f"[Zotero Save] Unhandled exception: {e}")
            yield f"data: {json.dumps({'step': 'error', 'message': f'Error: {e}'})}\n\n"

    response = StreamingHttpResponse(_generate(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response
