"""Article search views: run a multi-source search, evaluate results, history."""
import concurrent.futures
import csv
import json
import threading
import time

from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import models
from django.core.paginator import Paginator
from django.db.models import Count
from django.http import JsonResponse, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from ..analyzer import is_aligned, FIELDS
from ..models import ArticleSearch, SearchResultArticle
from ..search import fetch_all, evaluate_article
from ..search_providers import PROVIDERS, PROVIDER_LABELS
from .common import build_profile_data, get_user_profile, log

EVAL_TIMEOUT_SECONDS = 180  # per-article LLM cap
HEARTBEAT_INTERVAL = 5      # seconds between worker liveness ticks
STALE_AFTER = 90            # a run whose heartbeat is older than this is dead


def _start_heartbeat(search_id):
    """Tick `heartbeat` while a worker is alive so a dead run can be detected.
    Returns a stop Event — set it to end the heartbeat.

    Resilient to transient DB errors (e.g. SQLite 'database is locked' under
    write contention): a failed tick is skipped, never killing the thread — a
    dead heartbeat here would falsely flag a live run as orphaned."""
    stop = threading.Event()

    def loop():
        while True:
            try:
                ArticleSearch.objects.filter(id=search_id).update(heartbeat=timezone.now())
            except Exception as e:
                log.debug(f"heartbeat tick failed for {search_id}: {e}")
            if stop.wait(HEARTBEAT_INTERVAL):
                break

    threading.Thread(target=loop, daemon=True).start()
    return stop


def _is_run_live(search) -> bool:
    """True if a search's worker is genuinely still running (fresh heartbeat)."""
    if not search.heartbeat:
        return False
    return (timezone.now() - search.heartbeat).total_seconds() < STALE_AFTER

# Sources needing an API key → the Profile attr that must be set to enable them
_KEYED_SOURCES = {s: attr for s, (_, attr) in PROVIDERS.items() if attr}


@login_required
@ensure_csrf_cookie
def search_index(request):
    profile = get_user_profile(request.user)
    providers = [{
        "key": key,
        "label": PROVIDER_LABELS.get(key, key),
        # a keyed source is disabled in the UI until its key is saved in Settings
        "needs_key": key in _KEYED_SOURCES and not getattr(profile, _KEYED_SOURCES[key], ""),
    } for key in PROVIDERS]
    return render(request, 'search.html', {
        'recent_searches': ArticleSearch.objects.filter(user=request.user)[:10],
        'providers': providers,
    })


@login_required
@require_http_methods(["POST"])
def search_start(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    query = (data.get("query") or "").strip()
    criteria = (data.get("criteria") or "").strip()
    sources = [s for s in (data.get("sources") or []) if s in PROVIDERS]
    name = (data.get("name") or "").strip()

    def _year(v):
        v = (str(v or "")).strip()
        return v if v.isdigit() and len(v) == 4 else ""
    year_from = _year(data.get("year_from"))
    year_to = _year(data.get("year_to"))
    fin = data.get("filters") or {}
    filters = {
        "scope": "title_abstract" if fin.get("scope") == "title_abstract" else "all",
        "journal_only": bool(fin.get("journal_only")),
        "has_abstract": bool(fin.get("has_abstract")),
        "full_text": bool(fin.get("full_text")),
    }

    if not query or not criteria or not sources:
        return JsonResponse({"error": "query, criteria and at least one source are required"}, status=400)

    search = ArticleSearch.objects.create(
        user=request.user, name=name, query=query, criteria=criteria, sources=sources,
        year_from=year_from, year_to=year_to, filters=filters,
        heartbeat=timezone.now(),  # avoids an orphan-detection flicker before the first tick
    )
    threading.Thread(target=_search_orchestrator, args=(search.id, request.user.id), daemon=True).start()
    return JsonResponse({"search_id": str(search.id)})


def _is_cancelled(search_id) -> bool:
    return ArticleSearch.objects.filter(id=search_id, is_cancelled=True).exists()


def _is_paused(search_id) -> bool:
    return ArticleSearch.objects.filter(id=search_id, is_paused=True).exists()


def _append_log(search_id, lines, msg):
    """Append a timestamped line to the in-memory log (persisted by callers)."""
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    log.info(f"[search {str(search_id)[:8]}] {msg}")
    lines.append(line)


def _search_orchestrator(search_id, user_id):
    """Phase 1: fetch + harmonize + store. Ends at 'searched' — evaluation is a
    separate, user-triggered step."""
    search = ArticleSearch.objects.get(id=search_id)
    profile = get_user_profile(User.objects.get(id=user_id))
    heartbeat = _start_heartbeat(search_id)

    lock = threading.Lock()
    progress = {
        "phase": "searching",
        "providers": {s: {"count": 0, "done": False} for s in search.sources},
        "fetched": 0, "unique": 0, "evaluated": 0, "aligned": 0,
    }
    log_lines = []
    last_save = [0.0]

    def _persist(force=False):
        now = time.time()
        if not force and now - last_save[0] < 1.5:
            return
        last_save[0] = now
        ArticleSearch.objects.filter(id=search_id).update(
            progress=progress, debug_log="\n".join(log_lines))

    def on_progress(source, count, done):
        with lock:
            progress["providers"][source] = {"count": count, "done": done}
            progress["fetched"] = sum(p["count"] for p in progress["providers"].values())
            _persist()
        if done:
            with lock:
                _append_log(search_id, log_lines, f"{PROVIDER_LABELS.get(source, source)}: fetched {count}")

    try:
        with lock:
            _append_log(search_id, log_lines, f"Search started: {len(search.sources)} sources — {search.query[:80]}")
        articles = fetch_all(search.query, search.sources, profile,
                             cancel=lambda: _is_cancelled(search_id),
                             on_progress=on_progress,
                             year_from=search.year_from, year_to=search.year_to,
                             filt=search.filters)
        with lock:
            progress["unique"] = len(articles)
            _append_log(search_id, log_lines, f"Fetched {progress['fetched']} records → {len(articles)} unique after dedup")

        SearchResultArticle.objects.bulk_create([
            SearchResultArticle(
                search=search,
                title=a.get("title", ""), authors=a.get("authors", ""),
                year=a.get("year", ""), doi=a.get("doi", ""),
                abstract=a.get("abstract", ""), venue=a.get("venue", ""),
                url=a.get("url", ""), sources=a.get("sources", []),
            ) for a in articles
        ], batch_size=500)
    except Exception as e:
        log.error(f"Search {search_id} failed during fetch: {e}")
        with lock:
            _append_log(search_id, log_lines, f"ERROR during fetch: {e}")
        progress["phase"] = "error"
        heartbeat.set()
        ArticleSearch.objects.filter(id=search_id).update(
            status='error', error=str(e), progress=progress, debug_log="\n".join(log_lines))
        return

    if _is_cancelled(search_id):
        progress["phase"] = "cancelled"
        heartbeat.set()
        _persist(force=True)
        ArticleSearch.objects.filter(id=search_id).update(status='cancelled')
        return

    progress["phase"] = "searched"
    with lock:
        _append_log(search_id, log_lines, f"Search complete: {progress['unique']} articles ready to evaluate.")
    heartbeat.set()
    ArticleSearch.objects.filter(id=search_id).update(
        status='searched', progress=progress, debug_log="\n".join(log_lines))


def _evaluation_orchestrator(search_id, user_id, source=None):
    """Phase 2: evaluate pending articles against the criteria. Pausable
    (stops between chunks) and resumable (only 'pending' articles are processed,
    so re-running continues where it left off). When `source` is set, only
    articles returned by that provider are evaluated."""
    search = ArticleSearch.objects.get(id=search_id)
    profile = get_user_profile(User.objects.get(id=user_id))
    profile_data = build_profile_data(profile)
    heartbeat = _start_heartbeat(search_id)

    lock = threading.Lock()
    log_lines = (search.debug_log or "").split("\n") if search.debug_log else []
    progress = search.progress or {}
    progress["phase"] = "evaluating"
    # Recompute counters from the DB so resume stays accurate.
    done_qs = search.articles.exclude(status__in=["pending", "processing"])
    progress["evaluated"] = done_qs.count()
    progress["aligned"] = sum(1 for a in search.articles.filter(status="done").only("evaluation")
                              if is_aligned(a.evaluation))

    def _persist(force=False):
        ArticleSearch.objects.filter(id=search_id).update(
            progress=progress, debug_log="\n".join(log_lines))

    # A crash/pause mid-flight can leave rows 'processing'; reclaim them.
    search.articles.filter(status="processing").update(status="pending")
    ArticleSearch.objects.filter(id=search_id).update(status="evaluating", is_paused=False)

    provider = profile.ai_provider
    max_workers = 3 if provider == "gemini" else 1
    if source:
        pending = [aid for aid, srcs in search.articles.filter(status="pending").values_list("id", "sources")
                   if source in (srcs or [])]
    else:
        pending = list(search.articles.filter(status="pending").values_list("id", flat=True))
    scope = f" from {source}" if source else ""
    with lock:
        _append_log(search_id, log_lines, f"Evaluating {len(pending)} articles{scope} ({provider}, {max_workers} worker(s))…")
    _persist()

    # Chunk == worker count: pause/cancel is checked before every batch that
    # actually runs concurrently (i.e. before each single article on a local,
    # 1-worker provider), so pausing takes effect after the current article.
    stopped = None  # 'paused' | 'cancelled'
    for i in range(0, len(pending), max_workers):
        if _is_cancelled(search_id):
            stopped = "cancelled"
            break
        if _is_paused(search_id):
            stopped = "paused"
            break
        chunk = pending[i:i + max_workers]
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_evaluate_one, search_id, aid, search.criteria, profile_data)
                       for aid in chunk]
            for f in concurrent.futures.as_completed(futures):
                exc = f.exception()
                aligned = False if exc else bool(f.result())
                if exc:
                    log.error(f"Article evaluation crashed: {exc}")
                with lock:
                    progress["evaluated"] += 1
                    if aligned:
                        progress["aligned"] += 1
        _persist()

    if stopped == "cancelled":
        final = "cancelled"
    elif stopped == "paused":
        final = "paused"
    else:
        final = "done"
    progress["phase"] = final
    heartbeat.set()
    with lock:
        _append_log(search_id, log_lines,
                    f"Evaluation {final}: {progress['evaluated']} evaluated, {progress['aligned']} aligned.")
    ArticleSearch.objects.filter(id=search_id).update(
        status=final, progress=progress, debug_log="\n".join(log_lines))


def _evaluate_one(search_id, article_id, criteria, profile_data) -> bool:
    """Evaluate one article; returns True if it aligned with the criteria."""
    article = SearchResultArticle.objects.get(id=article_id)
    if _is_cancelled(search_id):
        article.status = 'cancelled'
        article.save()
        return False

    article.status = 'processing'
    article.save(update_fields=['status'])

    data = {
        "title": article.title, "authors": article.authors,
        "abstract": article.abstract, "doi": article.doi,
    }
    inner = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = inner.submit(evaluate_article, data, criteria, profile_data)
        article.evaluation = future.result(timeout=EVAL_TIMEOUT_SECONDS)
        article.status = 'done'
        article.error = None
    except concurrent.futures.TimeoutError:
        article.error = f"Evaluation timed out after {EVAL_TIMEOUT_SECONDS // 60} minutes."
        article.status = 'error'
    except Exception as e:
        log.error(f"Error evaluating article {article_id}: {e}")
        article.error = str(e)
        article.status = 'error'
    finally:
        inner.shutdown(wait=False, cancel_futures=True)
    article.save()
    return article.status == 'done' and is_aligned(article.evaluation)


def _search_state(search):
    # Cheap aggregate counts — a search can hold thousands of rows, so never
    # load them all just to build a status tick.
    counts = search.articles.aggregate(
        total=Count('id'),
        evaluated=Count('id', filter=models.Q(status__in=['done', 'error', 'cancelled'])),
    )
    prog = search.progress or {}
    return {
        "status": search.status,
        "error": search.error,
        "phase": prog.get("phase", search.status),
        # True while a worker is actively ticking; the UI uses this (not the
        # status) to decide whether a run is genuinely alive.
        "live": _is_run_live(search),
        "total": counts["total"],
        "evaluated": counts["evaluated"],
        "aligned": prog.get("aligned", 0),
        "fetched": prog.get("fetched", 0),
        "unique": prog.get("unique", 0),
        "providers": prog.get("providers", {}),
        "log": (search.debug_log or "").splitlines()[-40:],
    }


@login_required
def search_status(request, search_id):
    search = get_object_or_404(ArticleSearch, id=search_id, user=request.user)
    return JsonResponse(_search_state(search))


@login_required
def search_status_stream(request, search_id):
    search = get_object_or_404(ArticleSearch, id=search_id, user=request.user)

    def event_stream():
        while True:
            try:
                search.refresh_from_db()
                state = _search_state(search)
            except Exception as e:
                log.error(f"Search status stream error: {e}")
                break
            yield f"data: {json.dumps(state)}\n\n"
            # 'searched' and 'paused' are resting states — no active background
            # work, so close the stream until the user triggers the next step.
            if state["status"] in ("searched", "paused", "done", "error", "cancelled"):
                break
            time.sleep(2)

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@login_required
def search_result(request, search_id):
    """One page of results. Unlimited searches can hold thousands of articles,
    so the table is paginated server-side instead of shipping them all at once."""
    search = get_object_or_404(ArticleSearch, id=search_id, user=request.user)

    def _int(name, default):
        try:
            return int(request.GET.get(name, default))
        except (TypeError, ValueError):
            return default

    page = _int("page", 1)
    page_size = min(max(_int("page_size", 50), 1), 200)
    aligned_only = request.GET.get("aligned_only") == "1"
    source = (request.GET.get("source") or "").strip()

    qs = search.articles.order_by("id")
    if aligned_only or source:
        # sources is a JSON list and is_aligned reads a JSON field, so filter in
        # Python; these paths are opt-in.
        ids = [a.id for a in qs
               if (not source or source in (a.sources or []))
               and (not aligned_only or (a.status == "done" and is_aligned(a.evaluation)))]
        qs = search.articles.filter(id__in=ids).order_by("id")

    paginator = Paginator(qs, page_size)
    page_obj = paginator.get_page(page)
    articles = [{
        "id": a.id, "title": a.title, "authors": a.authors, "year": a.year,
        "doi": a.doi, "venue": a.venue, "url": a.url, "sources": a.sources,
        "status": a.status, "aligns": is_aligned(a.evaluation) if a.status == "done" else None,
        "alignment_reason": (a.evaluation or {}).get("alignment_reason", "") if a.evaluation else "",
    } for a in page_obj]
    return JsonResponse({
        "query": search.query, "criteria": search.criteria,
        "name": search.name, "sources": search.sources,
        "year_from": search.year_from, "year_to": search.year_to,
        "filters": search.filters or {},
        "articles": articles,
        "total": paginator.count, "page": page_obj.number, "num_pages": paginator.num_pages,
    })


@login_required
@require_http_methods(["POST"])
def search_stop(request, search_id):
    ArticleSearch.objects.filter(id=search_id, user=request.user).update(is_cancelled=True)
    SearchResultArticle.objects.filter(
        search_id=search_id, search__user=request.user,
        status__in=['pending', 'processing'],
    ).update(status='cancelled')
    return JsonResponse({"ok": True})


def _eval_body(request, search):
    """Read the optional criteria + source-scope from an evaluate/restart body."""
    try:
        data = json.loads(request.body or "{}")
    except (json.JSONDecodeError, ValueError):
        data = {}
    new = (data.get("criteria") or "").strip()
    criteria = new if new and new != search.criteria else None
    source = (data.get("source") or "").strip() or None
    return criteria, source


def _scoped_pending(search, source):
    """Article ids that are pending, optionally limited to one provider."""
    rows = search.articles.filter(status="pending").values_list("id", "sources")
    return [aid for aid, srcs in rows if not source or source in (srcs or [])]


@login_required
@require_http_methods(["POST"])
def search_evaluate(request, search_id):
    """Start (or resume) evaluation of a search's pending articles, optionally
    scoped to a single provider."""
    search = get_object_or_404(ArticleSearch, id=search_id, user=request.user)
    # If a worker is genuinely still running (fresh heartbeat), don't start a
    # second one — but this is not an error: just tell the client to keep
    # polling. A stale 'evaluating'/'searching' is an orphaned run and falls
    # through to be taken over.
    if search.status in ("searching", "evaluating") and _is_run_live(search):
        return JsonResponse({"ok": True, "already_running": True})
    criteria, source = _eval_body(request, search)
    if not _scoped_pending(search, source):
        return JsonResponse({"error": "Nothing left to evaluate."}, status=400)
    fields = ["is_paused", "is_cancelled", "heartbeat"]
    if criteria:
        search.criteria = criteria
        fields.append("criteria")
    search.is_paused = False
    search.is_cancelled = False
    search.heartbeat = timezone.now()  # claim the run immediately
    search.save(update_fields=fields)
    threading.Thread(target=_evaluation_orchestrator,
                     args=(search.id, request.user.id, source), daemon=True).start()
    return JsonResponse({"ok": True})


@login_required
@require_http_methods(["POST"])
def search_restart_eval(request, search_id):
    """Re-evaluate from scratch, discarding previous verdicts. When a provider
    is given, only that provider's articles are reset and re-evaluated."""
    search = get_object_or_404(ArticleSearch, id=search_id, user=request.user)
    if search.status in ("searching", "evaluating") and _is_run_live(search):
        return JsonResponse({"error": f"Search is {search.status}."}, status=409)
    criteria, source = _eval_body(request, search)

    if source:
        ids = [a.id for a in search.articles.only("id", "sources") if source in (a.sources or [])]
        reset_qs = search.articles.filter(id__in=ids)
    else:
        reset_qs = search.articles.all()
    if not reset_qs.exists():
        return JsonResponse({"error": "No articles to evaluate."}, status=400)
    reset_qs.update(status="pending", evaluation=None, error=None)

    prog = search.progress or {}
    # Recount so partial (scoped) restarts stay accurate.
    prog["evaluated"] = search.articles.exclude(status__in=["pending", "processing"]).count()
    prog["aligned"] = sum(1 for a in search.articles.filter(status="done").only("evaluation")
                          if is_aligned(a.evaluation))
    prog["phase"] = "evaluating"
    search.progress = prog
    search.is_paused = False
    search.is_cancelled = False
    search.heartbeat = timezone.now()
    fields = ["progress", "is_paused", "is_cancelled", "heartbeat"]
    if criteria:
        search.criteria = criteria
        fields.append("criteria")
    search.save(update_fields=fields)
    threading.Thread(target=_evaluation_orchestrator,
                     args=(search.id, request.user.id, source), daemon=True).start()
    return JsonResponse({"ok": True})


@login_required
@require_http_methods(["POST"])
def search_pause(request, search_id):
    """Pause a running evaluation — it stops between chunks, leaving remaining
    articles pending so evaluation can be resumed later."""
    ArticleSearch.objects.filter(id=search_id, user=request.user).update(is_paused=True)
    return JsonResponse({"ok": True})


@login_required
def search_export(request, search_id):
    search = get_object_or_404(ArticleSearch, id=search_id, user=request.user)
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="search_{str(search_id)[:8]}.csv"'
    writer = csv.writer(response)
    meta = ["title", "authors", "year", "doi", "venue", "url", "sources"]
    writer.writerow(meta + FIELDS)
    for a in search.articles.filter(status='done'):
        ev = a.evaluation or {}
        if is_aligned(ev):
            writer.writerow(
                [a.title, a.authors, a.year, a.doi, a.venue, a.url, ", ".join(a.sources)]
                + [ev.get(f, "") for f in FIELDS]
            )
    return response


@login_required
def list_searches(request):
    searches = ArticleSearch.objects.filter(user=request.user).annotate(
        article_count=Count('articles'),
        aligned_count=Count('articles', filter=models.Q(articles__status='done')),
    )
    data = [{
        "id": str(s.id), "name": s.name, "query": s.query,
        "status": s.status, "created_at": s.created_at.isoformat(),
        "article_count": s.article_count,
    } for s in searches]
    return JsonResponse(data, safe=False)


@login_required
@require_http_methods(["DELETE", "POST"])
def delete_search(request, search_id):
    get_object_or_404(ArticleSearch, id=search_id, user=request.user).delete()
    return JsonResponse({"ok": True})
