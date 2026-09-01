import os

from django.db import models
from django.contrib.auth.models import User
from django.db.models.signals import post_save
from django.dispatch import receiver
import uuid

class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    
    # AI Provider Settings
    ai_provider = models.CharField(max_length=50, default='ollama')
    
    # Ollama
    ollama_base_url = models.CharField(max_length=255, default='http://localhost:11434')
    ollama_model = models.CharField(max_length=255, blank=True)
    
    # LM Studio
    lmstudio_base_url = models.CharField(max_length=255, default='http://localhost:1234')
    lmstudio_model = models.CharField(max_length=255, blank=True)
    lmstudio_model_arch = models.CharField(max_length=100, blank=True)
    lmstudio_max_context = models.IntegerField(default=32768)
    lmstudio_context_length = models.IntegerField(default=262144)
    lmstudio_eval_batch_size = models.IntegerField(default=262144)
    lmstudio_flash_attention = models.BooleanField(default=True)
    lmstudio_keep_model_in_memory = models.BooleanField(default=True)
    lmstudio_try_mmap = models.BooleanField(default=True)
    lmstudio_num_experts = models.IntegerField(default=8)
    lmstudio_llama_k_cache_quant_type = models.CharField(max_length=50, default='false')
    lmstudio_llama_v_cache_quant_type = models.CharField(max_length=50, default='false')
    
    # Gemini
    gemini_api_key = models.CharField(max_length=255, blank=True)
    gemini_model = models.CharField(max_length=255, default='gemini-2.0-flash')
    
    # Llama Server
    llama_server_base_url = models.CharField(max_length=255, default='http://localhost:8012')
    llama_server_model = models.CharField(max_length=255, blank=True)
    
    # OCR Settings
    ocr_provider = models.CharField(max_length=50, default='mistral')
    mistral_api_key = models.CharField(max_length=255, blank=True)
    
    # Generation Settings
    reasoning = models.CharField(max_length=20, default='off')
    temperature = models.FloatField(default=0.7)
    max_tokens = models.IntegerField(default=2048)
    
    # Article Search API keys
    core_api_key = models.CharField(max_length=255, blank=True)
    springer_api_key = models.CharField(max_length=255, blank=True)
    semantic_scholar_api_key = models.CharField(max_length=255, blank=True)

    # Zotero Settings
    zotero_user_id = models.CharField(max_length=100, blank=True)
    zotero_api_key = models.CharField(max_length=255, blank=True)
    zotero_library_type = models.CharField(max_length=20, default='user')
    zotero_api_mode = models.CharField(max_length=20, default='remote')

    def __str__(self):
        return f"Profile for {self.user.username}"

@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        Profile.objects.create(user=instance)

class Session(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, blank=True, default="")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='sessions')
    folder = models.CharField(max_length=512)
    criteria = models.TextField()
    zotero_links = models.JSONField(default=dict, blank=True)
    zotero_metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    is_cancelled = models.BooleanField(default=False)
    load_progress = models.IntegerField(default=0)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Session {self.id} - {self.user.username} ({self.created_at.strftime('%Y-%m-%d %H:%M')})"

class PDFJob(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('done', 'Done'),
        ('error', 'Error'),
        ('cancelled', 'Cancelled'),
    ]
    
    session = models.ForeignKey(Session, on_delete=models.CASCADE, related_name='jobs')
    pdf_path = models.CharField(max_length=512)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    result = models.JSONField(null=True, blank=True)
    error = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"Job for {self.pdf_path} in session {self.session_id}"


class ReferenceExtractionJob(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('done', 'Done'),
        ('error', 'Error'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='ref_extraction_jobs')
    pdf_path = models.CharField(max_length=512)
    zotero_item_key = models.CharField(max_length=100, blank=True)
    zotero_collection_key = models.CharField(max_length=100, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    error = models.TextField(null=True, blank=True)
    ocr_method = models.CharField(max_length=50, blank=True)
    article_doi = models.CharField(max_length=255, blank=True)
    debug_log = models.TextField(null=True, blank=True)
    raw_llm_response = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'pdf_path']),
        ]

    def __str__(self):
        return f"RefExtraction {self.id} - {os.path.basename(self.pdf_path)}"


class ArticleSearch(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('searching', 'Searching'),
        ('searched', 'Searched'),        # fetch done, evaluation not complete
        ('evaluating', 'Evaluating'),
        ('paused', 'Paused'),            # evaluation paused, resumable
        ('done', 'Done'),
        ('error', 'Error'),
        ('cancelled', 'Cancelled'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='article_searches')
    name = models.CharField(max_length=255, blank=True, default="")
    query = models.TextField()
    criteria = models.TextField()
    sources = models.JSONField(default=list)  # provider keys selected for this search
    year_from = models.CharField(max_length=4, blank=True, default="")
    year_to = models.CharField(max_length=4, blank=True, default="")
    filters = models.JSONField(default=dict, blank=True)  # scope, journal_only, has_abstract, full_text
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    error = models.TextField(null=True, blank=True)
    is_cancelled = models.BooleanField(default=False)
    is_paused = models.BooleanField(default=False)
    heartbeat = models.DateTimeField(null=True, blank=True)  # last tick of a live worker
    progress = models.JSONField(default=dict, blank=True)  # phase + per-provider fetch counts
    debug_log = models.TextField(blank=True, default="")   # timestamped trace of the run
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"Search {self.id} - {self.query[:40]}"


class SearchResultArticle(models.Model):
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('done', 'Done'),
        ('error', 'Error'),
        ('cancelled', 'Cancelled'),
    ]

    search = models.ForeignKey(ArticleSearch, on_delete=models.CASCADE, related_name='articles')
    title = models.TextField(blank=True)
    authors = models.TextField(blank=True)
    year = models.CharField(max_length=20, blank=True)
    doi = models.CharField(max_length=255, blank=True)
    abstract = models.TextField(blank=True)
    venue = models.TextField(blank=True)
    url = models.TextField(blank=True)
    sources = models.JSONField(default=list)  # which APIs returned this article
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    evaluation = models.JSONField(null=True, blank=True)  # analyzer fields (alignment + reason)
    error = models.TextField(null=True, blank=True)

    def __str__(self):
        return f"{self.title[:50]} ({self.search_id})"


class ExtractedReference(models.Model):
    job = models.ForeignKey(ReferenceExtractionJob, on_delete=models.CASCADE, related_name='references')
    order = models.IntegerField(default=0)
    raw_text = models.TextField(blank=True)
    author = models.TextField(blank=True)
    title = models.TextField(blank=True)
    year = models.CharField(max_length=20, blank=True)
    journal = models.TextField(blank=True)
    volume = models.CharField(max_length=50, blank=True)
    issue = models.CharField(max_length=50, blank=True)
    pages = models.CharField(max_length=50, blank=True)
    doi = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['order']

    def __str__(self):
        return f"Ref #{self.order}: {self.title[:50] if self.title else self.raw_text[:50]}"
