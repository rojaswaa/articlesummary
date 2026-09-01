from django import template
import json
import os
from django.utils.safestring import mark_safe

register = template.Library()

@register.filter(name='jsonify')
def jsonify(value):
    return mark_safe(json.dumps(value))

@register.filter(name='split_path_last')
def split_path_last(value):
    if not value: return ""
    return os.path.basename(value.rstrip(os.sep))
