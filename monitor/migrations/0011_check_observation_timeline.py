from django.contrib.postgres.operations import AddIndexConcurrently, RemoveIndexConcurrently
from django.db import migrations, models


class Migration(migrations.Migration):
    atomic = False

    dependencies = [('monitor', '0010_product_name')]

    operations = [
        AddIndexConcurrently(
            model_name='check',
            index=models.Index(
                fields=['target', '-checked_at', '-id'], include=['healthy', 'coverage_seconds'],
                name='check_observation_timeline',
            ),
        ),
        RemoveIndexConcurrently(model_name='check', name='monitor_che_target__05320e_idx'),
    ]
