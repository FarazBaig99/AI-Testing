from django.contrib import admin

from .models import Module, Project, Release, TestCase, TestResult, TestRunner

admin.site.register([Project, Release, Module, TestCase, TestRunner, TestResult])
