from django.contrib.auth.models import User
from django.db import models


class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    job_title = models.CharField(max_length=100, blank=True)
    department = models.CharField(max_length=100, blank=True)
    capacity_hours = models.PositiveIntegerField('Default capacity hours per week', default=40)

    def __str__(self):
        return self.user.username
