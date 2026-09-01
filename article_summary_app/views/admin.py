"""Settings page and local AI server management."""
import subprocess

from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import render, redirect
from django.views.decorators.http import require_http_methods

from ..analyzer import get_available_models, check_connectivity
from .common import get_user_profile, log


@login_required
def admin(request):
    profile = get_user_profile(request.user)
    pd = {
        "ollama_base_url": profile.ollama_base_url,
        "lmstudio_base_url": profile.lmstudio_base_url,
        "gemini_api_key": profile.gemini_api_key,
        "mistral_api_key": profile.mistral_api_key,
    }
    conn_status = check_connectivity(pd)

    return render(request, "settings.html", {
        "profile": profile,
        "ai_provider": profile.ai_provider,
        "conn_status": conn_status,
        "ollama_models": get_available_models(profile.ollama_base_url, "ollama"),
        "lmstudio_models": get_available_models(profile.lmstudio_base_url, "lmstudio"),
        "gemini_models": get_available_models(profile.gemini_api_key, "gemini"),
        "llama_server_models": get_available_models(profile.llama_server_base_url, "llama_server"),
    })


@login_required
@require_http_methods(["POST"])
def admin_save_ai_config(request):
    p = get_user_profile(request.user)
    for field in ['ai_provider', 'ollama_base_url', 'ollama_model',
                  'lmstudio_base_url', 'lmstudio_model', 'lmstudio_model_arch',
                  'gemini_model', 'llama_server_base_url',
                  'llama_server_model', 'ocr_provider', 'reasoning',
                  'zotero_user_id', 'zotero_library_type', 'zotero_api_mode',
                  'lmstudio_llama_k_cache_quant_type', 'lmstudio_llama_v_cache_quant_type']:
        if field in request.POST:
            setattr(p, field, request.POST[field].strip())

    # API keys are never echoed back into the form; an empty submission
    # means "keep the saved key", not "clear it".
    for field in ['gemini_api_key', 'mistral_api_key', 'zotero_api_key',
                  'core_api_key', 'springer_api_key', 'semantic_scholar_api_key']:
        if request.POST.get(field, "").strip():
            setattr(p, field, request.POST[field].strip())

    # Boolean flags for LM Studio preloading
    if 'lmstudio_flash_attention' in request.POST:
        p.lmstudio_flash_attention = request.POST['lmstudio_flash_attention'].lower() == 'true'
    if 'lmstudio_keep_model_in_memory' in request.POST:
        p.lmstudio_keep_model_in_memory = request.POST['lmstudio_keep_model_in_memory'].lower() == 'true'
    if 'lmstudio_try_mmap' in request.POST:
        p.lmstudio_try_mmap = request.POST['lmstudio_try_mmap'].lower() == 'true'

    try:
        if 'temperature' in request.POST: p.temperature = float(request.POST['temperature'])
        if 'max_tokens' in request.POST: p.max_tokens = int(request.POST['max_tokens'])
        if 'lmstudio_max_context' in request.POST: p.lmstudio_max_context = int(request.POST['lmstudio_max_context'])
        if 'lmstudio_context_length' in request.POST: p.lmstudio_context_length = int(request.POST['lmstudio_context_length'])
        if 'lmstudio_eval_batch_size' in request.POST: p.lmstudio_eval_batch_size = int(request.POST['lmstudio_eval_batch_size'])
        if 'lmstudio_num_experts' in request.POST: p.lmstudio_num_experts = int(request.POST['lmstudio_num_experts'])
    except ValueError:
        pass  # keep previous values if the form sent garbage

    p.save()
    return redirect('admin')


@login_required
def admin_models(request):
    base_url = request.GET.get("base_url", "")
    provider = request.GET.get("provider", "ollama")
    models = get_available_models(base_url, provider)
    return JsonResponse({"models": models})


def _is_process_running(pattern, full_match=False):
    cmd = ["pgrep", "-f", pattern] if full_match else ["pgrep", pattern]
    try:
        subprocess.run(cmd, capture_output=True, check=True)
        return True
    except subprocess.CalledProcessError:
        return False


@login_required
@require_http_methods(["POST"])
def manage_ollama(request):
    action = request.POST.get("action")  # "start" or "stop"
    is_running = _is_process_running("ollama")

    try:
        if action == "start":
            if is_running:
                return JsonResponse({"status": "already_running"})
            subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return JsonResponse({"status": "starting"})

        elif action == "stop":
            if not is_running:
                return JsonResponse({"status": "already_stopped"})
            subprocess.run(["pkill", "ollama"], check=True)
            return JsonResponse({"status": "stopping"})

    except Exception as e:
        log.error(f"Ollama management error: {e}")
        return JsonResponse({"status": "error", "message": str(e)}, status=500)

    return JsonResponse({"status": "running" if is_running else "stopped"})


@login_required
@require_http_methods(["POST"])
def manage_lmstudio(request):
    action = request.POST.get("action")
    is_running = _is_process_running("LM Studio", full_match=True)

    try:
        if action == "start":
            if is_running: return JsonResponse({"status": "already_running"})
            subprocess.Popen(["lms", "server", "start"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return JsonResponse({"status": "starting"})
        elif action == "stop":
            if not is_running: return JsonResponse({"status": "already_stopped"})
            subprocess.run(["lms", "server", "stop"], check=True)
            return JsonResponse({"status": "stopping"})
    except Exception as e:
        log.error(f"LM Studio management error: {e}")
        return JsonResponse({"status": "error", "message": str(e)}, status=500)

    return JsonResponse({"status": "running" if is_running else "stopped"})
