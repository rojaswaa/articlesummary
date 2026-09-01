"""Reference extraction views: Zotero browsing, extraction pipeline, history, export."""
import concurrent.futures
import csv
import json
import os
import threading

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse, HttpResponse
from django.shortcuts import render, get_object_or_404
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_http_methods

from ..models import ReferenceExtractionJob, ExtractedReference
from ..ref_extractor import extract_references
from .common import build_profile_data, get_user_profile, log, resolve_allowed_pdf, ZOTERO_STORAGE

EXTRACTION_TIMEOUT_SECONDS = 300


@login_required
@ensure_csrf_cookie
def references_index(request):
    profile = get_user_profile(request.user)
    recent_jobs = ReferenceExtractionJob.objects.filter(
        user=request.user, status='done'
    ).prefetch_related('references')[:20]
    return render(request, 'references.html', {
        'recent_jobs': recent_jobs,
        'active_ai': profile.ai_provider,
        'active_ocr': profile.ocr_provider,
    })


@login_required
@require_http_methods(["POST"])
def references_extract(request):
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, ValueError):
        return JsonResponse({"error": "Invalid JSON"}, status=400)
    
    pdf_path = data.get("pdf_path", "").strip()
    zotero_item_key = data.get("zotero_item_key", "").strip()
    zotero_collection_key = data.get("zotero_collection_key", "").strip()
    
    if not pdf_path:
        return JsonResponse({"error": "Missing pdf_path"}, status=400)
    
    full_path = os.path.join(ZOTERO_STORAGE, pdf_path) if not os.path.isabs(pdf_path) else pdf_path
    resolved = resolve_allowed_pdf(request, full_path)
    if resolved is None:
        return JsonResponse({"error": f"PDF not found: {pdf_path}"}, status=404)
    full_path = str(resolved)
    
    existing = ReferenceExtractionJob.objects.filter(
        user=request.user,
        pdf_path=full_path,
        status='done'
    ).first()
    
    if existing and not data.get("force_reextract"):
        return JsonResponse({
            "job_id": str(existing.id),
            "status": "done",
            "cached": True,
        })
    
    job = ReferenceExtractionJob.objects.create(
        user=request.user,
        pdf_path=full_path,
        zotero_item_key=zotero_item_key,
        zotero_collection_key=zotero_collection_key,
    )
    
    threading.Thread(
        target=_run_extraction,
        args=(job.id, request.user.id),
        daemon=True
    ).start()
    
    return JsonResponse({
        "job_id": str(job.id),
        "status": "processing",
        "cached": False,
    })


def _run_extraction(job_id, user_id):
    from django.contrib.auth.models import User
    
    job = ReferenceExtractionJob.objects.get(id=job_id)
    user = User.objects.get(id=user_id)
    profile = get_user_profile(user)
    
    # Preload the model in LM Studio with the user's custom hardware/context configurations
    if profile.ai_provider == "lmstudio" and profile.lmstudio_model:
        from .analysis import _load_lmstudio_model
        _load_lmstudio_model(None, profile, profile.lmstudio_model)
    elif profile.ai_provider == "ollama" and profile.ollama_model:
        from .analysis import _load_ollama_model
        _load_ollama_model(None, profile, profile.ollama_model)
        
    profile_data = build_profile_data(profile, min_tokens=4096)
    
    job.status = 'processing'
    job.save()
    
    inner = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = inner.submit(extract_references, job.pdf_path, profile_data)
        result = future.result(timeout=EXTRACTION_TIMEOUT_SECONDS)
        
        job.ocr_method = result.get("ocr_method", "")
        job.article_doi = result.get("article_doi") or ""
        job.debug_log = result.get("debug_log", "")
        job.raw_llm_response = result.get("raw_llm_response", "")
        
        if "error" in result:
            job.error = result["error"]
            job.status = 'error'
            job.save()
            return
            
        job.status = 'done'
        job.error = None
        job.save()
        
        refs_to_create = []
        for ref_data in result.get("references", []):
            refs_to_create.append(ExtractedReference(
                job=job,
                order=ref_data.get("order", 0),
                raw_text=ref_data.get("raw_text", ""),
                author=ref_data.get("author", ""),
                title=ref_data.get("title", ""),
                year=ref_data.get("year", ""),
                journal=ref_data.get("journal", ""),
                volume=ref_data.get("volume", ""),
                issue=ref_data.get("issue", ""),
                pages=ref_data.get("pages", ""),
                doi=ref_data.get("doi", ""),
            ))
        ExtractedReference.objects.bulk_create(refs_to_create)
        
    except concurrent.futures.TimeoutError:
        log.warning(f"Timeout extracting references from {job.pdf_path}")
        job.error = f"Extraction timed out after {EXTRACTION_TIMEOUT_SECONDS // 60} minutes."
        job.status = 'error'
        job.save()
    except Exception as e:
        log.error(f"Error extracting references from {job.pdf_path}: {e}")
        job.error = str(e)
        job.status = 'error'
        job.save()
    finally:
        inner.shutdown(wait=False, cancel_futures=True)


@login_required
def references_status(request, job_id):
    job = get_object_or_404(ReferenceExtractionJob, id=job_id, user=request.user)
    return JsonResponse({
        "status": job.status,
        "error": job.error,
        "debug_log": job.debug_log,
        "raw_llm_response": job.raw_llm_response,
    })


@login_required
def references_result(request, job_id):
    job = get_object_or_404(ReferenceExtractionJob, id=job_id, user=request.user)
    if job.status != 'done':
        return JsonResponse({"error": "Job not complete", "status": job.status}, status=400)
    refs = list(job.references.all().values(
        'order', 'raw_text', 'author', 'title', 'year',
        'journal', 'volume', 'issue', 'pages', 'doi'
    ))
    
    # Sort alphabetically by author (case-insensitive, fallback to title)
    refs.sort(key=lambda r: ((r.get('author') or '').lower().strip() or (r.get('title') or '').lower().strip()))
    
    return JsonResponse({
        "job_id": str(job.id),
        "pdf_path": job.pdf_path,
        "pdf_filename": os.path.basename(job.pdf_path),
        "ocr_method": job.ocr_method,
        "article_doi": job.article_doi,
        "ref_count": len(refs),
        "references": refs,
        "created_at": job.created_at.isoformat(),
        "debug_log": job.debug_log,
        "raw_llm_response": job.raw_llm_response,
    })


@login_required
def references_export(request, job_id):
    job = get_object_or_404(ReferenceExtractionJob, id=job_id, user=request.user)
    if job.status != 'done':
        return JsonResponse({"error": "Job not complete"}, status=400)
    response = HttpResponse(content_type='text/csv')
    filename = os.path.splitext(os.path.basename(job.pdf_path))[0]
    response['Content-Disposition'] = f'attachment; filename="references_{filename}.csv"'
    writer = csv.writer(response)
    writer.writerow(['#', 'Author', 'Title', 'Year', 'Journal/Source', 'Volume', 'Issue', 'Pages', 'DOI', 'Raw Text'])
    
    refs = list(job.references.all())
    # Sort alphabetically by author (case-insensitive, fallback to title)
    refs.sort(key=lambda r: ((r.author or '').lower().strip() or (r.title or '').lower().strip()))
    
    for idx, ref in enumerate(refs, 1):
        writer.writerow([
            idx, ref.author, ref.title, ref.year,
            ref.journal, ref.volume, ref.issue, ref.pages, ref.doi, ref.raw_text,
        ])
    return response


@login_required
def references_history(request):
    jobs = ReferenceExtractionJob.objects.filter(user=request.user).order_by('-created_at')
    data = [{
        "id": str(j.id),
        "pdf_path": j.pdf_path,
        "pdf_filename": os.path.basename(j.pdf_path),
        "status": j.status,
        "ref_count": j.references.count() if j.status == 'done' else 0,
        "created_at": j.created_at.isoformat(),
        "zotero_item_key": j.zotero_item_key,
    } for j in jobs]
    return JsonResponse(data, safe=False)


@login_required
def references_citations(request, job_id):
    """Forward citations (papers citing this article) via Semantic Scholar."""
    job = get_object_or_404(ReferenceExtractionJob, id=job_id, user=request.user)
    if not job.article_doi:
        return JsonResponse({"error": "No DOI found for this article — citations unavailable."}, status=404)

    from ..semantic_scholar import get_citations
    citations = get_citations(job.article_doi)
    if citations is None:
        return JsonResponse({"error": "Article not found in Semantic Scholar."}, status=404)

    citations.sort(key=lambda c: (c.get("year") or "0"), reverse=True)
    return JsonResponse({
        "job_id": str(job.id),
        "doi": job.article_doi,
        "citation_count": len(citations),
        "citations": citations,
    })


@login_required
@require_http_methods(["DELETE", "POST"])
def references_delete(request, job_id):
    job = get_object_or_404(ReferenceExtractionJob, id=job_id, user=request.user)
    job.delete()
    return JsonResponse({"ok": True})
