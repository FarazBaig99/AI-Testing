import django.db.models.deletion
from django.db import migrations, models


def forwards(apps, schema_editor):
    Environment = apps.get_model('apitesting', 'Environment')
    EnvironmentUrl = apps.get_model('apitesting', 'EnvironmentUrl')
    for env in Environment.objects.all():
        if env.base_url:
            EnvironmentUrl.objects.create(environment=env, name='Base URL', key='baseUrl', url=env.base_url.rstrip('/'),
                                          is_default=True)


class Migration(migrations.Migration):

    dependencies = [
        ('apitesting', '0016_lov_categories'),
    ]

    operations = [
        migrations.AlterField(model_name='environment', name='base_url',
                              field=models.URLField(blank=True, max_length=500, verbose_name='Base URL')),
        migrations.CreateModel(
            name='EnvironmentUrl',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=100)),
                ('key', models.CharField(max_length=60)),
                ('url', models.URLField(max_length=500)),
                ('is_default', models.BooleanField(default=False)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('environment', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='urls', to='apitesting.environment')),
            ],
            options={'ordering': ['created_at'], 'unique_together': {('environment', 'name'), ('environment', 'key')}},
        ),
        migrations.RunPython(forwards, migrations.RunPython.noop),
    ]
