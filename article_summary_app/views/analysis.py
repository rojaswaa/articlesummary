"""Core analysis views: folder browsing, the analysis pipeline, sessions, PDF serving."""
import concurrent.futures
import csv
import json
import os
import subprocess
import threading
import time

import httpx
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Count
from django.http import JsonResponse, HttpResponse, FileResponse, StreamingHttpResponse
from django.shortcuts import render, redirect, get_object_or_404
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from ..analyzer import get_available_models, check_connectivity, is_aligned, FIELDS
from ..extractor import analyze_pdf
from ..models import Session, PDFJob
from .common import build_profile_data, get_user_profile, is_allowed_folder, log, remember_allowed_folder, resolve_allowed_pdf

ANALYSIS_TIMEOUT_SECONDS = 180  # per-PDF cap

# ── Dashboard ──

@login_required
@ensure_csrf_cookie
def index(request):
    # Fetch recent sessions for the sidebar
    recent_sessions = Session.objects.filter(user=request.user).order_by('-created_at')[:10]

    # Quick connectivity check for active providers
    profile = get_user_profile(request.user)
    pd = {
        "ai_provider": profile.ai_provider,
        "ollama_base_url": profile.ollama_base_url,
        "lmstudio_base_url": profile.lmstudio_base_url,
        "gemini_api_key": profile.gemini_api_key,
        "mistral_api_key": profile.mistral_api_key,
    }
    conn_status = check_connectivity(pd)

    active_ai = profile.ai_provider
    active_ocr = profile.ocr_provider  # mistral | macocr | pytesseract

    ai_ok = conn_status.get(active_ai, {}).get("available", True) if active_ai in conn_status else True
    ocr_ok = conn_status.get("mistral", {}).get("available", True) if active_ocr == "mistral" else True

    return render(request, 'index.html', {
        'recent_sessions': recent_sessions,
        'ai_ok': ai_ok,
        'ocr_ok': ocr_ok,
        'active_ai': active_ai,
        'active_ocr': active_ocr,
        'conn_status': conn_status,
    })


@login_required
@require_http_methods(["GET"])
def browse_folder(request):
    script = (
        'tell application "Finder"\n'
        '  activate\n'
        '  set folderPath to POSIX path of (choose folder with prompt "Select a folder with PDF files")\n'
        'end tell\n'
        'return folderPath'
    )
    # Note: osascript only works on macOS
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            # User cancelled the dialog, or AppleScript failed
            stderr = result.stderr.strip()
            if "User canceled" in stderr or "(-128)" in stderr:
                return JsonResponse({"path": None, "api_error": "No folder selected", "pdfs": []})
            log.error(f"osascript failed: {stderr}")
            return JsonResponse({"error": stderr or "Folder picker failed", "path": None, "pdfs": []})
        folder_path = result.stdout.strip()
    except Exception as e:
        log.error(f"Error browsing folder: {e}")
        return JsonResponse({"error": str(e), "path": None, "pdfs": []})

    if not folder_path:
        return JsonResponse({"path": None, "api_error": "No folder selected", "pdfs": []})

    remember_allowed_folder(request, folder_path)

    pdfs = []
    for root, dirs, files in os.walk(folder_path):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for f in files:
            if f.lower().endswith(".pdf") and not f.startswith("."):
                full_path = os.path.join(root, f)
                pdfs.append(os.path.relpath(full_path, folder_path))
    pdfs.sort()
    pdfs = list(dict.fromkeys(pdfs))  # Deduplicate while preserving order
    return JsonResponse({"path": folder_path, "pdfs": pdfs})


# ── Analysis pipeline ──

@login_required
@require_http_methods(["POST"])
def analyze_start(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    folder = data.get("folder")
    pdfs = data.get("pdfs")
    criteria = (data.get("criteria") or "").strip()
    session_name = (data.get("name") or "").strip()
    zotero_links = data.get("zoteroLinks", {})
    zotero_metadata = data.get("zoteroMetadata", {})

    if not all([folder, pdfs, criteria]):
        return JsonResponse({"error": "Missing parameters"}, status=400)

    if not is_allowed_folder(request, folder):
        return JsonResponse({"error": "Folder not allowed — select it via the folder picker or Zotero."}, status=403)

    session = Session.objects.create(
        user=request.user,
        name=session_name,
        folder=folder,
        criteria=criteria,
        zotero_links=zotero_links,
        zotero_metadata=zotero_metadata,
    )

    # Preserve the order sent by the frontend (author-alphabetical);
    # use dict.fromkeys to deduplicate while keeping order.
    for path in list(dict.fromkeys(pdfs)):
        PDFJob.objects.create(session=session, pdf_path=path)

    threading.Thread(
        target=_analysis_orchestrator,
        args=(session.id, request.user.id),
        daemon=True
    ).start()

    return JsonResponse({"session_id": str(session.id)})


def _run_job(session_id, job_id, folder, criteria, profile_data):
    """Process a single PDF job with a hard timeout that doesn't block the queue."""
    job = PDFJob.objects.get(id=job_id)
    if job.status != 'pending' or Session.objects.filter(id=session_id, is_cancelled=True).exists():
        if job.status == 'pending':
            job.status = 'cancelled'
            job.save()
        return

    job.status = 'processing'
    job.save()

    full_path = os.path.join(folder, job.pdf_path)
    # Inner single-thread executor gives us a timeout; shutdown(wait=False) lets
    # the queue move on even if the underlying call is still hanging.
    inner = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = inner.submit(analyze_pdf, full_path, criteria, profile_data=profile_data)
        job.result = future.result(timeout=ANALYSIS_TIMEOUT_SECONDS)
        job.status = 'done'
        job.error = None
    except concurrent.futures.TimeoutError:
        log.warning(f"Timeout analyzing {full_path}")
        job.error = f"Analysis timed out after {ANALYSIS_TIMEOUT_SECONDS // 60} minutes."
        job.status = 'error'
    except Exception as e:
        log.error(f"Error analyzing {full_path}: {e}")
        job.error = str(e)
        job.status = 'error'
    finally:
        inner.shutdown(wait=False, cancel_futures=True)
    job.save()


def _analysis_orchestrator(session_id, user_id):
    session = Session.objects.get(id=session_id)
    user = User.objects.get(id=user_id)
    profile = get_user_profile(user)

    provider = profile.ai_provider
    model_key = {
        "ollama": profile.ollama_model,
        "lmstudio": profile.lmstudio_model,
        "llama_server": profile.llama_server_model,
        "gemini": profile.gemini_model,
    }.get(provider, "")

    # Model loading for LM Studio and Ollama
    if provider == "lmstudio":
        _load_lmstudio_model(session, profile, model_key)
    elif provider == "ollama":
        _load_ollama_model(session, profile, model_key)
    else:
        session.load_progress = 100
        session.save()

    profile_data = build_profile_data(profile)

    job_ids = list(session.jobs.filter(status='pending').values_list('id', flat=True))
    # Local servers process one model call at a time; cloud providers can take
    # a few PDFs in parallel.
    max_workers = 3 if provider == "gemini" else 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_run_job, session_id, jid, session.folder, session.criteria, profile_data)
                   for jid in job_ids]
        for f in concurrent.futures.as_completed(futures):
            exc = f.exception()
            if exc:
                log.error(f"Job runner crashed: {exc}")


def _load_lmstudio_model(session, profile, model_key):
    url = profile.lmstudio_base_url.rstrip('/')
    try:
        # Construct ONLY the keys recognized by LM Studio's POST /api/v1/models/load REST API in snake_case.
        # Sending unrecognized keys (like keepModelInMemory or cache quantizations) causes a fatal
        # "unrecognized_keys" error on the LM Studio server, failing the model load.
        payload = {
            "model": model_key,
            "context_length": profile.lmstudio_context_length,
            "eval_batch_size": profile.lmstudio_eval_batch_size,
            "flash_attention": profile.lmstudio_flash_attention,
            "num_experts": profile.lmstudio_num_experts,
        }
        log.info(f"Preloading LM Studio model {model_key} with config: {payload}")
        httpx.post(f"{url}/api/v1/models/load", json=payload, timeout=60)
    except Exception as e:
        log.warning(f"LM Studio model preload failed (continuing): {e}")
    finally:
        if session:
            session.load_progress = 100
            session.save()


def _load_ollama_model(session, profile, model_key):
    url = profile.ollama_base_url.rstrip('/')
    try:
        log.info(f"Preloading Ollama model {model_key}...")
        # Send empty prompt request with keep_alive = -1 to load it indefinitely
        httpx.post(
            f"{url}/api/generate",
            json={"model": model_key, "prompt": "", "keep_alive": -1},
            timeout=120
        )
        log.info(f"Ollama model {model_key} preloaded successfully.")
    except Exception as e:
        log.warning(f"Ollama model preload failed: {e}")
    finally:
        if session:
            session.load_progress = 100
            session.save()


def _session_statuses(session):
    results = {}
    for j in session.jobs.all():
        aligns = None
        if j.status == "done" and j.result and "fields" in j.result:
            aligns = is_aligned(j.result["fields"])
        results[j.pdf_path] = {
            "status": j.status,
            "error": j.error,
            "aligns": aligns,
        }
    return results


@login_required
def analyze_status(request, session_id):
    session = get_object_or_404(Session, id=session_id, user=request.user)
    return JsonResponse(_session_statuses(session))


@login_required
def analyze_status_stream(request, session_id):
    """SSE endpoint streaming job statuses until every job reaches a terminal state."""
    session = get_object_or_404(Session, id=session_id, user=request.user)
    terminal = {"done", "error", "cancelled"}

    def event_stream():
        while True:
            try:
                statuses = _session_statuses(session)
            except Exception as e:
                log.error(f"Status stream error: {e}")
                break
            yield f"data: {json.dumps(statuses)}\n\n"
            if statuses and all(s["status"] in terminal for s in statuses.values()):
                break
            time.sleep(2)

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


@login_required
def analyze_progress(request, session_id):
    get_object_or_404(Session, id=session_id, user=request.user)

    def event_stream():
        while True:
            try:
                session = Session.objects.get(id=session_id, user_id=request.user.id)
                yield f"data: {json.dumps({'progress': session.load_progress})}\n\n"
                if session.load_progress >= 100: break
            except Exception: break
            time.sleep(1)
    return StreamingHttpResponse(event_stream(), content_type='text/event-stream')


@login_required
def analyze_result(request, session_id, pdf_path):
    session = get_object_or_404(Session, id=session_id, user=request.user)
    job = PDFJob.objects.filter(session=session, pdf_path=pdf_path).first()
    if not job or not job.result:
        return JsonResponse({"error": "Not ready or not found"}, status=404)
    return JsonResponse(job.result)


@login_required
def analyze_citations(request, session_id, pdf_path):
    """Forward citations (Semantic Scholar) for an analyzed article, with
    papers already present in this session flagged by DOI."""
    session = get_object_or_404(Session, id=session_id, user=request.user)
    job = PDFJob.objects.filter(session=session, pdf_path=pdf_path).first()
    if not job or not job.result:
        return JsonResponse({"error": "Article not analyzed yet."}, status=404)

    result = job.result or {}
    doi = result.get("doi") or (result.get("fields") or {}).get("doi") or ""
    if not doi:
        return JsonResponse({"error": "No DOI found for this article — citations unavailable."}, status=404)

    from ..semantic_scholar import get_citations
    citations = get_citations(doi)
    if citations is None:
        return JsonResponse({"error": "Article not found in Semantic Scholar."}, status=404)

    session_dois = set()
    for j in session.jobs.filter(status='done'):
        r = j.result or {}
        d = r.get("doi") or (r.get("fields") or {}).get("doi") or ""
        if d:
            session_dois.add(d.lower())
    for c in citations:
        c["in_session"] = bool(c.get("doi")) and c["doi"].lower() in session_dois

    citations.sort(key=lambda c: (c.get("year") or "0"), reverse=True)
    return JsonResponse({"doi": doi, "citation_count": len(citations), "citations": citations})


@login_required
@require_http_methods(["POST"])
def analyze_stop(request):
    try:
        sid = json.loads(request.body).get("session_id")
        # 1. Mark session as cancelled
        Session.objects.filter(id=sid, user=request.user).update(is_cancelled=True)
        # 2. Immediately mark all non-terminal jobs as cancelled
        PDFJob.objects.filter(
            session_id=sid,
            session__user=request.user,
            status__in=['pending', 'processing']
        ).update(status='cancelled')
        return JsonResponse({"ok": True})
    except Exception as e:
        log.error(f"Error stopping analysis: {e}")
        return JsonResponse({"error": str(e)}, status=500)


@login_required
@require_http_methods(["POST"])
def analyze_retry(request, session_id):
    session = get_object_or_404(Session, id=session_id, user=request.user)
    # Include both error and cancelled jobs in the retry
    session.jobs.filter(status__in=['error', 'cancelled']).update(status='pending', error=None)
    session.is_cancelled = False
    session.save()

    threading.Thread(
        target=_analysis_orchestrator,
        args=(session.id, request.user.id),
        daemon=True
    ).start()
    return JsonResponse({"ok": True})


@login_required
def analyze_export(request, session_id):
    session = get_object_or_404(Session, id=session_id, user=request.user)

    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="export_{str(session_id)[:8]}.csv"'

    writer = csv.writer(response)
    writer.writerow(["filename"] + FIELDS + ["zotero_link"])

    for job in session.jobs.filter(status='done'):
        fields = job.result.get("fields", {}) if job.result else {}
        if is_aligned(fields):
            z_link = session.zotero_links.get(job.pdf_path, "")
            row = [job.pdf_path.split("/")[-1]] + [fields.get(f, "") for f in FIELDS] + [z_link]
            writer.writerow(row)
    return response


# ── Session History ──

@login_required
def list_sessions(request):
    sessions = Session.objects.filter(user=request.user).annotate(
        job_count=Count('jobs'),
        done_count=Count('jobs', filter=models.Q(jobs__status='done'))
    )
    data = [{
        "id": str(s.id),
        "name": s.name,
        "folder": s.folder,
        "created_at": s.created_at.isoformat(),
        "job_count": s.job_count,
        "done_count": s.done_count
    } for s in sessions]
    return JsonResponse(data, safe=False)


@login_required
@require_http_methods(["DELETE", "POST"])
def delete_session(request, session_id):
    session = get_object_or_404(Session, id=session_id, user=request.user)
    session.delete()
    return JsonResponse({"ok": True})


@login_required
def get_session_details(request, session_id):
    session = get_object_or_404(Session, id=session_id, user=request.user)
    return JsonResponse({
        "name": session.name,
        "folder": session.folder,
        "criteria": session.criteria,
        "zotero_links": session.zotero_links,
        "zotero_metadata": session.zotero_metadata,
    })


# ── PDF serving ──

@login_required
def serve_pdf(request):
    path = resolve_allowed_pdf(request, request.GET.get("path", ""))
    if path is None:
        return HttpResponse("Not Found", status=404)
    return FileResponse(open(path, 'rb'), content_type='application/pdf')
