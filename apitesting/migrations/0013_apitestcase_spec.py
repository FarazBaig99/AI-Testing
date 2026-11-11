from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apitesting', '0012_project_variables'),
    ]

    operations = [
        migrations.AddField(
            model_name='apitestcase',
            name='spec',
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
