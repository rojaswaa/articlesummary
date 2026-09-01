"""
WSGI config for article_summary project.

It exposes the WSGI callable as a module-level variable named ``application``.

For more information on this file, see
https://docs.djangoproject.com/en/6.0/howto/deployment/wsgi/
"""

import os
import atexit
import httpx
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "article_summary.settings")

application = get_wsgi_application()

def cleanup():
    try:
        from django.db import connection
        if "article_summary_app_profile" in connection.introspection.table_names():
            from article_summary_app.models import Profile
            profile = Profile.objects.first()
            if profile:
                provider = profile.ai_provider
                if provider == "lmstudio" and profile.lmstudio_model:
                    lm_url = profile.lmstudio_base_url.rstrip("/")
                    model_key = profile.lmstudio_model
                    # Unload LM Studio model
                    try:
                        httpx.post(
                            f"{lm_url}/api/v1/models/unload",
                            json={"instance_id": model_key},
                            timeout=5.0
                        )
                    except: pass
                    
                    # Stop LM Studio server
                    try:
                        import subprocess
                        subprocess.run(["lms", "server", "stop"], capture_output=True)
                    except: pass
                elif provider == "ollama" and profile.ollama_model:
                    ollama_url = profile.ollama_base_url.rstrip("/")
                    model_key = profile.ollama_model
                    # Unload Ollama model by sending keep_alive = 0
                    try:
                        httpx.post(
                            f"{ollama_url}/api/generate",
                            json={"model": model_key, "prompt": "", "keep_alive": 0},
                            timeout=5.0
                        )
                    except: pass
    except Exception as e:
        pass

atexit.register(cleanup)
