"""APIs synced before body templates existed: give every empty body the template its sync would now produce."""
from django.db import migrations

XML_TEMPLATE = '<?xml version="1.0" encoding="UTF-8"?>\n<root/>'


def _template(content_type):
    return XML_TEMPLATE if 'xml' in (content_type or '').lower() else '{}'


def fill(apps, schema_editor):
    from apitesting.parsers import fingerprint

    ApiTestCase = apps.get_model('apitesting', 'ApiTestCase')
    CollectionRequest = apps.get_model('apitesting', 'CollectionRequest')
    for case in ApiTestCase.objects.filter(body='').select_related('folder'):
        case.body = _template(case.content_type)
        # same fingerprint a re-sync computes, so the API is not reported as "Updated"
        case.fingerprint = fingerprint({'folder': case.folder.name, 'method': case.method, 'url': case.url,
                                        'content_type': case.content_type, 'headers': case.headers,
                                        'query_params': case.query_params, 'body': case.body})
        case.save(update_fields=['body', 'fingerprint'])
    # collection copies of synced APIs that still have no body
    for item in CollectionRequest.objects.filter(body='', source__isnull=False, body_mode__in=['none', 'raw']):
        item.body = _template(item.source.content_type if item.source_id else '')
        item.body_mode = 'raw'
        item.raw_language = 'xml' if item.body.startswith('<?xml') else 'json'
        item.save(update_fields=['body', 'body_mode', 'raw_language'])


class Migration(migrations.Migration):

    dependencies = [
        ('apitesting', '0006_scripts_and_variables'),
    ]

    operations = [
        migrations.RunPython(fill, migrations.RunPython.noop),
    ]
