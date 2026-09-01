from django.apps import AppConfig
import logging
import os
import shutil

log = logging.getLogger(__name__)

class ArticleSummaryAppConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'article_summary_app'

    def ready(self):
        # Startup checks
        log.info("============================================================")
        log.info("SYSTEM STARTUP: Checking global configurations")
        
        # Check Tesseract
        tesseract_path = shutil.which("tesseract")
        if not tesseract_path:
            log.warning("!!! WARNING: 'tesseract' executable not found in PATH. Local OCR will fail.")
        else:
            log.info(f"SUCCESS: Found tesseract at {tesseract_path}")

        # Check MarkItDown (first-pass PDF text-layer extraction)
        try:
            import markitdown  # noqa: F401
            log.info("SUCCESS: markitdown is installed (fast text-layer extraction enabled).")
        except ImportError:
            log.warning("!!! WARNING: 'markitdown' not installed. All PDFs will go straight to the OCR provider.")

        # Check Zotero Storage
        zotero_storage = os.path.expanduser("~/Zotero/storage")
        if not os.path.isdir(zotero_storage):
            log.warning(f"!!! WARNING: Zotero storage folder not found at {zotero_storage}")
        else:
            log.info(f"SUCCESS: Zotero storage folder found.")
            
        # Check main API keys in ENV (system defaults)
        main_keys = {
            "GEMINI_API_KEY": os.getenv("GEMINI_API_KEY"),
            "MISTRAL_API_KEY": os.getenv("MISTRAL_API_KEY")
        }
        for key, val in main_keys.items():
            if not val:
                log.info(f"INFO: {key} is NOT configured in .env (System Default)")
            else:
                log.info(f"SUCCESS: {key} is configured in .env (System Default)")

        log.info("============================================================")
        self.reclaim_orphaned_searches()
        self.preload_active_model()

    def reclaim_orphaned_searches(self):
        """Worker threads for searches/evaluations die when the process restarts,
        but the DB still says 'searching'/'evaluating' — leaving the UI polling a
        run nothing is driving. Reclaim those rows so the user can resume."""
        try:
            from django.db import connection
            if "article_summary_app_articlesearch" not in connection.introspection.table_names():
                return
            from .models import ArticleSearch, SearchResultArticle

            # Evaluations are resumable: reset in-flight rows and mark paused.
            evaluating = list(ArticleSearch.objects.filter(status="evaluating").values_list("id", flat=True))
            if evaluating:
                SearchResultArticle.objects.filter(
                    search_id__in=evaluating, status="processing").update(status="pending")
                for s in ArticleSearch.objects.filter(id__in=evaluating):
                    prog = s.progress or {}
                    prog["phase"] = "paused"
                    s.progress = prog
                    s.status = "paused"
                    s.is_paused = False
                    s.save(update_fields=["status", "is_paused", "progress"])
                log.info(f"STARTUP: reclaimed {len(evaluating)} interrupted evaluation(s) → paused (resumable).")

            # A partial fetch can't be safely resumed → mark as error.
            searching = ArticleSearch.objects.filter(status="searching")
            n = searching.update(status="error", error="Interrupted by a server restart during search. Please re-run.")
            if n:
                log.info(f"STARTUP: marked {n} interrupted search(es) as error.")
        except Exception as e:
            log.warning(f"STARTUP: failed to reclaim orphaned searches: {e}")

    def preload_active_model(self):
        try:
            from django.db import connection
            if "article_summary_app_profile" not in connection.introspection.table_names():
                log.info("STARTUP PRELOAD: Profile table does not exist yet (likely running migrations). Skipping.")
                return
            from .models import Profile
            profile = Profile.objects.first()
            if not profile:
                log.info("STARTUP PRELOAD: No profiles found in database. Skipping.")
                return
            
            provider = profile.ai_provider
            if provider == "lmstudio" and profile.lmstudio_model:
                from .views.analysis import _load_lmstudio_model
                import threading
                log.info(f"STARTUP PRELOAD: Spawning thread to preload LM Studio model: {profile.lmstudio_model}")
                threading.Thread(
                    target=_load_lmstudio_model,
                    args=(None, profile, profile.lmstudio_model),
                    daemon=True
                ).start()
            elif provider == "ollama" and profile.ollama_model:
                from .views.analysis import _load_ollama_model
                import threading
                log.info(f"STARTUP PRELOAD: Spawning thread to preload Ollama model: {profile.ollama_model}")
                threading.Thread(
                    target=_load_ollama_model,
                    args=(None, profile, profile.ollama_model),
                    daemon=True
                ).start()
        except Exception as e:
            log.warning(f"STARTUP PRELOAD: Failed to preload active model: {e}")
