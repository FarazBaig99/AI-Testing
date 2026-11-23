from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apitesting', '0025_apiworkflow_workflownode_workflowrun_workflowedge'),
    ]

    operations = [
        migrations.CreateModel(
            name='IntegrationSettings',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('clickup_token', models.CharField(blank=True, max_length=255, verbose_name='ClickUp API token')),
                ('clickup_list_id', models.CharField(blank=True, max_length=64, verbose_name='ClickUp list id')),
                ('jira_url', models.CharField(blank=True, max_length=255, verbose_name='Jira URL')),
                ('jira_email', models.CharField(blank=True, max_length=255, verbose_name='Jira e-mail')),
                ('jira_token', models.CharField(blank=True, max_length=255, verbose_name='Jira API token')),
                ('jira_project_key', models.CharField(blank=True, max_length=32, verbose_name='Jira project key')),
                ('jira_issue_type', models.CharField(default='Bug', max_length=64, verbose_name='Jira issue type')),
                ('ci_token', models.CharField(blank=True, max_length=128, verbose_name='CI/CD trigger token')),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
        ),
    ]
