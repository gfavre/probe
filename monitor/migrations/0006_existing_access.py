from django.db import migrations


def preserve_existing_access(apps, schema_editor):
    User = apps.get_model('auth', 'User')
    Profile = apps.get_model('monitor', 'UserProfile')
    for user in User.objects.all().iterator():
        # Preserve existing access; new accounts will default to read-only.
        Profile.objects.update_or_create(user_id=user.pk, defaults={'role': 'admin' if user.is_superuser else 'editor'})


class Migration(migrations.Migration):
    dependencies = [('monitor', '0005_platformsettings_userprofile_role')]
    operations = [migrations.RunPython(preserve_existing_access, migrations.RunPython.noop)]
