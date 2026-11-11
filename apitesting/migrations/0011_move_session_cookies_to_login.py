from django.db import migrations


def move_cookies(apps, schema_editor):
    """Before 0010 every login's session cookies landed in the shared jar, so a request sent with another login (or
    with none) still carried them. When exactly one login has ever logged in, the shared cookies are its session:
    move them to that login. With several, nobody can tell whose they are and they stay shared."""
    AuthProfile = apps.get_model('apitesting', 'AuthProfile')
    ApiGlobals = apps.get_model('apitesting', 'ApiGlobals')
    logged_in = list(AuthProfile.objects.filter(last_status='Passed'))
    g = ApiGlobals.objects.order_by('pk').first()
    if len(logged_in) != 1 or g is None or not g.cookies:
        return
    profile = logged_in[0]
    profile.cookies = list(g.cookies)
    profile.save(update_fields=['cookies'])
    g.cookies = []
    g.save(update_fields=['cookies'])


class Migration(migrations.Migration):

    dependencies = [
        ('apitesting', '0010_multiple_auth_profiles'),
    ]

    operations = [
        migrations.RunPython(move_cookies, migrations.RunPython.noop),
    ]
