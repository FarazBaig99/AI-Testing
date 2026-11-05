from django.contrib import admin

from .models import Milestone, Module, Project, ProjectMember, Release, TestCase, TestResult, TestRunner

admin.site.register([Project, Release, Module, TestCase, TestRunner, TestResult, ProjectMember, Milestone])
