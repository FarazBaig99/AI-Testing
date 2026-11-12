import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('apitesting', '0017_environment_urls'),
    ]

    operations = [
        migrations.CreateModel(
            name='CollectionFolder',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('name', models.CharField(max_length=200)),
                ('position', models.PositiveIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('collection', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='folders', to='apitesting.apicollection')),
                ('parent', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='children', to='apitesting.collectionfolder')),
            ],
            options={'ordering': ['position', 'id']},
        ),
        migrations.AddField(
            model_name='collectionrequest',
            name='folder',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, related_name='requests', to='apitesting.collectionfolder'),
        ),
    ]
