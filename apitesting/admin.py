from django.contrib import admin

from .models import ApiFolder, ApiTestCase, CollectionSource, ImportSession

admin.site.register([CollectionSource, ApiFolder, ApiTestCase, ImportSession])
