from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('apitesting', '0014_api_lov'),
    ]

    operations = [
        migrations.RemoveField(model_name='apilov', name='project'),
    ]
