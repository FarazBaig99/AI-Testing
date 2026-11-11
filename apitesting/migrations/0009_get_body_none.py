"""Saved GET / HEAD requests that only hold the empty body template: body mode back to "none" (template kept)."""
from django.db import migrations


def set_none(apps, schema_editor):
    from apitesting.parsers import is_empty_body

    CollectionRequest = apps.get_model('apitesting', 'CollectionRequest')
    for item in CollectionRequest.objects.filter(method__in=['GET', 'HEAD'], body_mode='raw'):
        if is_empty_body(item.body):
            item.body_mode = 'none'
            item.save(update_fields=['body_mode'])


class Migration(migrations.Migration):

    dependencies = [
        ('apitesting', '0008_auth_profiles_and_cookies'),
    ]

    operations = [
        migrations.RunPython(set_none, migrations.RunPython.noop),
    ]
