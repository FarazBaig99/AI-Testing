import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

STRFTIME_TO_PATTERN = [('%Y', 'yyyy'), ('%y', 'yy'), ('%m', 'MM'), ('%d', 'dd'), ('%H', 'HH'), ('%M', 'mm'), ('%S', 'ss'),
                       ('%b', 'MMM'), ('%B', 'MMMM'), ('%I', 'hh'), ('%p', 'a'), ('%z', 'Z'), ('T', "'T'")]


def forwards(apps, schema_editor):
    """Rows made by the first LOV version: kind -> produces + options."""
    ApiLov = apps.get_model('apitesting', 'ApiLov')
    for l in ApiLov.objects.all():
        o = dict(l.options or {})
        kind = l.kind
        if kind == 'date':
            offset = int(float(o.get('offset', 0) or 0))
            unit = o.get('unit', 'days')
            fmt = o.get('format', '%Y-%m-%d') or '%Y-%m-%d'
            for a, b in STRFTIME_TO_PATTERN:
                fmt = fmt.replace(a, b)
            l.produces, l.options = 'date', {
                'base': 'future' if offset > 0 else 'past' if offset < 0 else 'current', 'amount': abs(offset),
                'unit': unit if unit in ('days', 'weeks', 'months') else 'days', 'format': fmt,
                'time': 'current', 'timezone': 'auto'}
        elif kind == 'timestamp':
            offset = int(float(o.get('offset', 0) or 0))
            l.produces, l.options = 'date', {
                'base': 'future' if offset > 0 else 'past' if offset < 0 else 'current', 'amount': abs(offset),
                'unit': 'days', 'format': 'x' if o.get('precision') == 'milliseconds' else 'X',
                'time': 'current', 'timezone': 'auto'}
        elif kind == 'list':
            l.produces = 'defined'
            l.values = [v.strip() for v in str(o.get('values', '')).splitlines() if v.strip()]
            l.options = {'mode': o.get('mode', 'random')}
        else:
            l.produces, l.options = 'generated', dict(o, kind=kind)
        l.save()


class Migration(migrations.Migration):

    dependencies = [
        ('apitesting', '0015_remove_apilov_project'),
        ('projects', '0012_alter_projectactivity_category'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(model_name='apilov', name='aliases', field=models.TextField(blank=True)),
        migrations.AddField(model_name='apilov', name='description', field=models.TextField(blank=True)),
        migrations.AddField(model_name='apilov', name='produces',
                            field=models.CharField(choices=[('defined', 'Defined values'), ('unique', 'Unique value'), ('date', 'Date'), ('generated', 'Generated value')], default='defined', max_length=20)),
        migrations.AddField(model_name='apilov', name='values', field=models.JSONField(blank=True, default=list)),
        migrations.RunPython(forwards, migrations.RunPython.noop),
        migrations.RemoveField(model_name='apilov', name='kind'),
        migrations.AlterModelOptions(name='apilov', options={'ordering': ['name'], 'verbose_name': 'LOV category', 'verbose_name_plural': 'LOV categories'}),
        migrations.CreateModel(
            name='ApiLovDateFormat',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100, unique=True)),
                ('pattern', models.CharField(max_length=100)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to=settings.AUTH_USER_MODEL)),
            ],
            options={'ordering': ['name']},
        ),
        migrations.CreateModel(
            name='ApiLovUsedValue',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('value', models.CharField(max_length=600)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('lov', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='used_values', to='apitesting.apilov')),
                ('project', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='api_lov_used_values', to='projects.project')),
            ],
            options={'unique_together': {('lov', 'value')}},
        ),
    ]
