from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('apitesting', '0011_move_session_cookies_to_login'),
        ('projects', '0012_alter_projectactivity_category'),
    ]

    operations = [
        migrations.CreateModel(
            name='ProjectVariables',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('variables', models.JSONField(blank=True, default=list)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('project', models.OneToOneField(on_delete=django.db.models.deletion.CASCADE, related_name='api_variables', to='projects.project')),
            ],
            options={'verbose_name_plural': 'project variables'},
        ),
    ]
