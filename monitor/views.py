from django.utils.translation import gettext_lazy as _
from datetime import timedelta
from django.conf import settings
from django.contrib import messages
from django.contrib.auth import logout
from django.core.paginator import Paginator
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST
from .auth import operator_required
from .permissions import probe_editor_required, platform_admin_required
from .forms import TargetForm
from .metrics import availability, period_stats, duration
from .models import Target, Incident
from .notifications import send_notification
from .probes import probe


def login(request):
    return render(request, 'monitor/login.html', {'configured': bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET and settings.OWNER_EMAIL)})


@require_POST
def signout(request):
    logout(request)
    return redirect('login')


def summary(target, now):
    latest = target.checks.order_by('-checked_at').first()
    status, tone = (_('Suspendu'), 'paused') if not target.enabled else (_('Inconnu'), 'unknown')
    if target.enabled and latest and latest.checked_at + timedelta(seconds=latest.coverage_seconds + 15) >= now:
        status, tone = (_('Disponible'), 'up') if latest.healthy else (_('Indisponible'), 'down')
    return {'target': target, 'latest': latest, 'status': status, 'tone': tone}


@operator_required
def dashboard(request):
    now = timezone.now()
    rows = []
    for target in Target.objects.all():
        row = summary(target, now)
        checks = list(target.checks.filter(checked_at__gte=now - timedelta(days=30, minutes=5)).order_by('checked_at'))
        row['windows'] = [availability(checks, max(target.created_at, now - timedelta(days=d)), now) for d in (1, 7, 30)]
        rows.append(row)
    counts = {tone: sum(r['tone'] == tone for r in rows) for tone in ('up', 'down', 'unknown', 'paused')}
    return render(request, 'monitor/dashboard.html', {'rows': rows, 'counts': counts, 'now': now})


@operator_required
def target_detail(request, pk, tab='overview'):
    target = get_object_or_404(Target, pk=pk)
    now = timezone.now()
    context = {**summary(target, now), 'tab': tab, 'now': now}
    if tab == 'failures':
        failures = target.checks.filter(healthy=False)
        status = request.GET.get('status', '')
        if status.isdigit():
            failures = failures.filter(status_code=int(status))
        elif status == 'network':
            failures = failures.filter(status_code__isnull=True)
        context.update(page=Paginator(failures.order_by('-checked_at'), 50).get_page(request.GET.get('page')),
                       codes=target.checks.filter(healthy=False, status_code__isnull=False).values_list('status_code', flat=True).distinct().order_by('status_code'), selected_status=status)
    elif tab == 'alerts':
        context['form'] = TargetForm(instance=target)
    else:
        checks = list(target.checks.filter(checked_at__gte=now - timedelta(days=90, minutes=5)).order_by('checked_at'))
        incidents = list(target.incidents.all())
        context['periods'] = [{'label': label, **period_stats(checks, incidents, max(target.created_at, now - timedelta(days=d)), now)} for label, d in [(_('24 heures'), 1), (_('7 jours'), 7), (_('30 jours'), 30), (_('90 jours'), 90)]]
        context['history'] = []
        for offset in range(89, -1, -1):
            end = now - timedelta(days=offset)
            start = end - timedelta(days=1)
            day = [c for c in checks if start - timedelta(minutes=5) <= c.checked_at < end]
            stats = availability(day, start, end)
            tone = 'unknown' if stats['uptime'] == '—' else ('down' if any(not c.healthy for c in day if c.checked_at >= start) else 'up')
            context['history'].append({'tone': tone, 'label': f'{start:%d.%m}: {stats["uptime"]}, {_("couverture")} {stats["coverage"]}'})
        chart_days = {'1': 1, '7': 7, '30': 30}.get(request.GET.get('range'), 1)
        start = now - timedelta(days=chart_days)
        recent = [c for c in checks if c.checked_at >= start]
        # At most 240 real observations; gaps split lines instead of inventing data.
        step = max(1, (len(recent) + 239) // 240)
        selected = recent[::step]
        if recent and (not selected or selected[-1] is not recent[-1]):
            selected.append(recent[-1])
        peak = max((c.duration_ms for c in selected), default=1) or 1
        segments, points = [], []
        previous = None
        for c in selected:
            if previous and c.checked_at - previous.checked_at > timedelta(seconds=previous.coverage_seconds * step * 1.5):
                segments.append(' '.join(points))
                points = []
            x = 20 + 960 * (c.checked_at - start).total_seconds() / (now - start).total_seconds()
            y = 180 - 150 * c.duration_ms / peak
            points.append(f'{x:.1f},{y:.1f}')
            previous = c
        if points:
            segments.append(' '.join(points))
        healthy_recent = [c.duration_ms for c in recent if c.healthy]
        context.update(segments=segments, chart_points=len(recent), chart_peak=peak, chart_days=chart_days,
                       mean_ms=round(sum(healthy_recent) / len(healthy_recent)) if healthy_recent else None,
                       incidents=[{'incident': i, 'duration': duration(((i.ended_at or now) - i.started_at).total_seconds())} for i in incidents[:20]])
    return render(request, 'monitor/detail.html', context)


@probe_editor_required
def target_form(request, pk=None):
    target = get_object_or_404(Target, pk=pk) if pk else None
    form = TargetForm(request.POST or None, instance=target)
    if request.method == 'POST' and form.is_valid():
        target = form.save()
        messages.success(request, _('Probe enregistrée.'))
        return redirect('target_detail', pk=target.pk)
    sections = [
        ('general', _('Général'), _('L’URL surveillée et son nom dans le tableau de bord.'), ['name', 'url', 'notes', 'enabled']),
        ('checks', _('Contrôles'), _('Un contrôle à chaque échéance, sans retry.'), ['interval_minutes', 'timeout_seconds', 'ip_version', 'verify_tls', 'follow_redirects', 'cache_buster']),
        ('request', _('Requête HTTP'), _('Les données envoyées au service à chaque contrôle.'), ['method', 'auth_type', 'auth_username', 'auth_password', 'auth_token', 'body_type', 'body', 'request_headers']),
        ('response', _('Réponse attendue'), _('Un contrôle réussit si tous les critères sont remplis.'), ['expected_status_codes', 'keyword', 'keyword_ignore_case', 'response_headers']),
        ('alerts', _('Alertes'), _('Un canal par probe. Aucun rappel pendant une panne.'), ['notification_channel', 'email_to', 'telegram_chat_id', 'failure_threshold', 'notification_delay_minutes']),
    ]
    return render(request, 'monitor/form.html', {'form': form, 'target': target, 'sections': [{'key': key, 'title': title, 'description': description, 'fields': [form[name] for name in fields]} for key, title, description, fields in sections]})


@probe_editor_required
@require_POST
def target_toggle(request, pk):
    target = get_object_or_404(Target, pk=pk)
    target.enabled = not target.enabled
    target.next_check_at = timezone.now()
    target.save(update_fields=['enabled', 'next_check_at'])
    messages.success(request, _('Surveillance activée.') if target.enabled else _('Surveillance suspendue.'))
    return redirect('target_detail', pk=pk)


@probe_editor_required
@require_POST
def target_test(request, pk):
    target = get_object_or_404(Target, pk=pk)
    result = probe(target)
    return render(request, 'monitor/test_result.html', {'target': target, 'result': result})


@probe_editor_required
@require_POST
def notification_test(request, pk):
    target = get_object_or_404(Target, pk=pk)
    destination = target.email_to if target.notification_channel == 'email' else target.telegram_chat_id
    try:
        send_notification(f'TEST: {target.name}', 'La sonde peut envoyer des notifications.', target.notification_channel, destination)
    except ValueError:
        messages.error(request, _('Envoi refusé. Vérifiez la configuration du canal sur le serveur.'))
    else:
        messages.success(request, _('Message accepté. Vérifiez sa réception.'))
    return redirect('target_alerts', pk=pk)


@probe_editor_required
def target_delete(request, pk):
    target = get_object_or_404(Target, pk=pk)
    if request.method == 'POST':
        target.delete()
        messages.success(request, _('Probe et historique supprimés.'))
        return redirect('dashboard')
    return render(request, 'monitor/delete.html', {'target': target})


@operator_required
def incidents(request):
    page = Paginator(Incident.objects.select_related('target'), 50).get_page(request.GET.get('page'))
    return render(request, 'monitor/incidents.html', {'page': page})


def home(request):
    if request.user.is_authenticated:
        return dashboard(request)
    return login(request)


@platform_admin_required
def invitations(request):
    from django.core.exceptions import ValidationError
    from .forms import InvitationForm
    from .invitations import create_invitation, send_invitation, invitation_url
    from .models import Invitation
    form = InvitationForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        try:
            invitation = create_invitation(form.cleaned_data['email'], role=form.cleaned_data['role'])
        except ValidationError as exc:
            form.add_error('email', exc)
        else:
            try:
                send_invitation(invitation)
            except ValueError:
                messages.error(request, _('Compte créé, mais l’email n’a pas été envoyé. Vous pouvez copier le lien ou renvoyer l’invitation.'))
            else:
                messages.success(request, _('Invitation envoyée. Le lien est valable 7 jours.'))
            return redirect('invitations')
    items = [{'invitation': i, 'url': invitation_url(i) if i.pending else ''} for i in Invitation.objects.select_related('user')]
    return render(request, 'monitor/invitations.html', {'form': form, 'items': items})


@platform_admin_required
@require_POST
def revoke_invitation(request, pk):
    from .models import Invitation
    Invitation.objects.filter(pk=pk, accepted_at__isnull=True).update(revoked_at=timezone.now())
    messages.success(request, _('Invitation révoquée.'))
    return redirect('invitations')


def accept_invitation(request, token):
    from django.contrib.auth import login as auth_login
    from django.core.exceptions import ValidationError
    from .forms import AcceptInvitationForm
    from .invitations import get_invitation, activate_invitation
    invitation = get_invitation(token)
    if invitation is None:
        return render(request, 'monitor/invitation_invalid.html', status=400)
    form = AcceptInvitationForm(request.POST or None, user=invitation.user)
    if request.method == 'POST' and form.is_valid():
        try:
            user = activate_invitation(invitation, form.cleaned_data['password'])
        except ValidationError:
            return render(request, 'monitor/invitation_invalid.html', status=400)
        auth_login(request, user, backend='django.contrib.auth.backends.ModelBackend')
        messages.success(request, _('Compte activé.'))
        return redirect('dashboard')
    return render(request, 'monitor/accept_invitation.html', {'invitation': invitation, 'form': form})


@operator_required
def styleguide(request):
    from .design import styleguide_context
    return render(request, 'monitor/styleguide.html', styleguide_context())


@operator_required
@require_POST
def set_profile_language(request):
    from django.http import HttpResponseBadRequest
    from django.utils.http import url_has_allowed_host_and_scheme
    from .models import UserProfile
    language = request.POST.get('language')
    if language not in UserProfile.Language.values:
        return HttpResponseBadRequest('Unsupported language')
    UserProfile.objects.update_or_create(user=request.user, defaults={'language': language})
    destination = request.POST.get('next', '/')
    if not url_has_allowed_host_and_scheme(destination, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        destination = '/'
    return redirect(destination)


@operator_required
def profile(request):
    from django.db import transaction
    from .forms import ProfileForm
    from .models import UserProfile
    form = ProfileForm(request.POST or None, instance=request.user)
    if request.method == 'POST' and form.is_valid():
        with transaction.atomic():
            form.save()
            UserProfile.objects.update_or_create(user=request.user, defaults={'language': form.cleaned_data['language']})
        messages.success(request, _('Profil enregistré.'))
        return redirect('profile')
    return render(request, 'monitor/profile.html', {'form': form})


@operator_required
def profile_password(request):
    from django.contrib.auth import update_session_auth_hash
    from django.contrib.auth.forms import PasswordChangeForm, SetPasswordForm
    form_class = PasswordChangeForm if request.user.has_usable_password() else SetPasswordForm
    form = form_class(request.user, request.POST or None)
    if request.method == 'POST' and form.is_valid():
        user = form.save()
        update_session_auth_hash(request, user)
        messages.success(request, _('Mot de passe enregistré.'))
        return redirect('profile')
    return render(request, 'monitor/profile_password.html', {'form': form})


@platform_admin_required
def platform_settings(request):
    from django.contrib.auth import get_user_model
    from .forms import PlatformSettingsForm
    from .models import UserProfile
    from .permissions import access_role
    form = PlatformSettingsForm(request.POST or None)
    if request.method == 'POST' and form.is_valid():
        form.save()
        messages.success(request, _('Réglages plateforme enregistrés.'))
        return redirect('platform_settings')
    accounts = [{'user': user, 'role': access_role(user) if user.is_active else getattr(getattr(user, 'profile', None), 'role', UserProfile.Role.VIEWER)} for user in get_user_model().objects.select_related('profile', 'invitation').order_by('username')]
    groups = [
        ('Postmark', ['postmark_server_token', 'clear_postmark_server_token', 'email_from', 'email_to', 'postmark_message_stream']),
        ('Slack', ['slack_webhook_url', 'clear_slack_webhook_url']),
        ('Telegram', ['telegram_bot_token', 'clear_telegram_bot_token', 'telegram_chat_id']),
        ('Google', ['google_client_id', 'google_client_secret', 'clear_google_client_secret']),
    ]
    return render(request, 'monitor/platform.html', {'form': form, 'groups': [(label, [form[name] for name in fields]) for label, fields in groups], 'accounts': accounts, 'roles': UserProfile.Role.choices})


@platform_admin_required
@require_POST
def set_user_role(request, pk):
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.http import HttpResponseBadRequest
    from .models import UserProfile
    from .permissions import access_role
    role = request.POST.get('role')
    if role not in UserProfile.Role.values:
        return HttpResponseBadRequest('Unsupported role')
    with transaction.atomic():
        users = list(get_user_model().objects.select_for_update().filter(is_active=True).order_by('pk'))
        target = next((user for user in users if user.pk == pk), None)
        if target is None:
            return HttpResponseBadRequest('Unknown active account')
        if role != UserProfile.Role.ADMIN and not any(user.pk != pk and access_role(user) == UserProfile.Role.ADMIN for user in users):
            messages.error(request, _('Au moins un administrateur plateforme doit rester actif.'))
        else:
            UserProfile.objects.update_or_create(user=target, defaults={'role': role})
            target.is_staff = target.is_superuser = role == UserProfile.Role.ADMIN
            target.save(update_fields=['is_staff', 'is_superuser'])
            messages.success(request, _('Droits enregistrés.'))
    return redirect('platform_settings')


def locked_account_change(request, pk, *, delete=False):
    """Serialize with role changes to keep an active platform administrator."""
    from django.contrib.auth import get_user_model
    from django.db import transaction
    from django.http import Http404
    from .models import UserProfile
    from .permissions import access_role
    with transaction.atomic():
        users = list(get_user_model().objects.select_for_update().order_by('pk'))
        target = next((user for user in users if user.pk == pk), None)
        if target is None:
            raise Http404
        if target.pk == request.user.pk:
            messages.error(request, _('Vous ne pouvez pas désactiver ou supprimer votre propre compte.'))
        elif (delete or target.is_active) and not any(user.pk != pk and access_role(user) == UserProfile.Role.ADMIN for user in users):
            messages.error(request, _('Au moins un administrateur plateforme doit rester actif.'))
        elif delete:
            target.delete()
            messages.success(request, _('Compte supprimé.'))
        elif target.is_active:
            target.is_active = False
            target.save(update_fields=['is_active'])
            # Remove existing sessions so reactivation requires a fresh sign-in.
            from django.contrib.sessions.models import Session
            for session in Session.objects.filter(expire_date__gt=timezone.now()).iterator():
                if session.get_decoded().get('_auth_user_id') == str(target.pk):
                    session.delete()
            messages.success(request, _('Compte désactivé.'))
        elif hasattr(target, 'invitation') and target.invitation.accepted_at is None:
            messages.error(request, _('Ce compte doit accepter une invitation avant son activation.'))
        else:
            target.is_active = True
            target.save(update_fields=['is_active'])
            messages.success(request, _('Compte réactivé.'))
    return redirect('platform_settings')


@platform_admin_required
@require_POST
def user_toggle(request, pk):
    return locked_account_change(request, pk)


@platform_admin_required
def user_delete(request, pk):
    from django.contrib.auth import get_user_model
    target = get_object_or_404(get_user_model(), pk=pk)
    if request.method == 'POST':
        return locked_account_change(request, pk, delete=True)
    return render(request, 'monitor/user_delete.html', {'account': target})
