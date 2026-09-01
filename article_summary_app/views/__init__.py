"""View package — re-exports every view so urls.py can keep using `views.<name>`."""
from .common import get_user_profile
from .auth import user_signup, user_login, user_logout
from .analysis import (
    index, browse_folder,
    analyze_start, analyze_status, analyze_status_stream, analyze_progress,
    analyze_result, analyze_citations, analyze_stop, analyze_retry, analyze_export,
    list_sessions, delete_session, get_session_details,
    serve_pdf,
)
from .zotero import (
    zotero_collections, zotero_pdfs, zotero_pdfs_stream,
    analyze_zotero_save, analyze_zotero_save_stream, search_zotero_save, search_zotero_status,
)
from .admin import (
    admin, admin_save_ai_config, admin_models,
    manage_ollama, manage_lmstudio,
)
from .references import (
    references_index, references_extract, references_status,
    references_result, references_export, references_history, references_citations,
    references_delete,
)
from .search import (
    search_index, search_start, search_status, search_status_stream, search_result,
    search_stop, search_evaluate, search_pause, search_restart_eval,
    search_export, list_searches, delete_search,
)
