from allauth.socialaccount.providers.google import views as google
from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView
from django.contrib.auth.views import LoginView
from monitor import views
from monitor.branding import platform_logo
from monitor.permissions import is_platform_admin
from monitor.health import health

admin.site.has_permission = lambda request: is_platform_admin(request.user)
handler500 = 'monitor.errors.server_error'
handler404 = 'monitor.errors.page_not_found'
urlpatterns = [
    path('health/', health, name='health'),
    path('branding/logo/<slug:digest>/', platform_logo, name='platform_logo'),
    path('favicon.ico', RedirectView.as_view(url='/static/monitor/icons/favicon.ico', permanent=True)),
    path('login/', views.login, name='login'),
    path('login/local/', LoginView.as_view(template_name='monitor/local_login.html', redirect_authenticated_user=True), name='local_login'),
    path('logout/', views.signout, name='logout'),
    path('profile/language/', views.set_profile_language, name='set_profile_language'),
    path('profile/', views.profile, name='profile'),
    path('profile/password/', views.profile_password, name='profile_password'),
    path('platform/', views.platform_settings, name='platform_settings'),
    path('platform/users/<int:pk>/role/', views.set_user_role, name='set_user_role'),
    path('platform/users/<int:pk>/toggle/', views.user_toggle, name='user_toggle'),
    path('platform/users/<int:pk>/delete/', views.user_delete, name='user_delete'),
    path('accounts/login/', RedirectView.as_view(pattern_name='login'), name='account_login'),
    path('accounts/logout/', views.signout, name='account_logout'),
    path('accounts/google/login/', google.oauth2_login, name='google_login'),
    path('accounts/google/login/callback/', google.oauth2_callback, name='google_callback'),
    path('accounts/social/signup/', RedirectView.as_view(pattern_name='login'), name='socialaccount_signup'),
    path('accounts/social/login/cancelled/', views.google_login_cancelled, name='socialaccount_login_cancelled'),
    path('admin/', admin.site.urls),
    path('', views.home, name='dashboard'),
    path('incidents/', views.incidents, name='incidents'),
    path('styleguide/', views.styleguide, name='styleguide'),
    path('invitations/', views.invitations, name='invitations'),
    path('invitations/<int:pk>/revoke/', views.revoke_invitation, name='revoke_invitation'),
    path('invitations/accept/<str:token>/', views.accept_invitation, name='accept_invitation'),
    path('probes/new/', views.target_form, name='target_create'),
    path('probes/<int:pk>/', views.target_detail, name='target_detail'),
    path('probes/<int:pk>/failures/', views.target_detail, {'tab': 'failures'}, name='target_failures'),
    path('probes/<int:pk>/alerts/', views.target_detail, {'tab': 'alerts'}, name='target_alerts'),
    path('probes/<int:pk>/edit/', views.target_form, name='target_edit'),
    path('probes/<int:pk>/toggle/', views.target_toggle, name='target_toggle'),
    path('probes/<int:pk>/test/', views.target_test, name='target_test'),
    path('probes/<int:pk>/test-notification/', views.notification_test, name='notification_test'),
    path('probes/<int:pk>/simulate-outage/', views.simulate_outage, name='simulate_outage'),
    path('probes/<int:pk>/delete/', views.target_delete, name='target_delete'),
]
