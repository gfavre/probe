from django import template
from django.utils.translation import gettext

register = template.Library()


@register.filter
def diagnostic(value):
    # Translate persisted diagnostics without rewriting the recorded history.
    text = str(value or '')
    translations = {
        'En-tête attendu absent ou différent': gettext('En-tête attendu absent ou différent'),
        'Texte attendu absent (premier Mio)': gettext('Texte attendu absent (premier Mio)'),
        'Erreur réseau, DNS ou TLS': gettext('Erreur réseau, DNS ou TLS'),
        'Délai de réponse dépassé': gettext('Délai de réponse dépassé'),
    }
    for source, translated in translations.items():
        text = text.replace(source, translated)
    return text
