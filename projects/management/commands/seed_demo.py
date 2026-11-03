from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from accounts.models import Profile
from projects.models import Module, Project, Release, TestCase, TestResult, TestRunner


class Command(BaseCommand):
    help = 'Create a demo user and a sample project with modules, test cases, releases and a runner'

    def handle(self, *args, **options):
        user, created = User.objects.get_or_create(username='demo', defaults={'email': 'demo@example.com'})
        if created:
            user.set_password('Demo@12345')
            user.save()
        Profile.objects.get_or_create(user=user, defaults={'job_title': 'QA Engineer', 'department': 'Quality Assurance'})

        if Project.objects.filter(name='Demo Shop').exists():
            self.stdout.write('Demo data already exists.')
            return

        project = Project.objects.create(name='Demo Shop', description='Sample e-commerce web application', created_by=user)
        r1 = Release.objects.create(project=project, name='v1.0.0', description='First release')
        Release.objects.create(project=project, name='v1.1.0', description='Cart improvements')

        auth = Module.objects.create(project=project, name='Authentication')
        login = Module.objects.create(project=project, name='Login', parent=auth)
        signup = Module.objects.create(project=project, name='Sign Up', parent=auth)
        cart = Module.objects.create(project=project, name='Shopping Cart')

        cases = [
            (login, 'Verify login with valid credentials', 'Positive', 'High', True,
             '1. Open login page\n2. Enter valid username and password\n3. Click Login', 'User lands on dashboard'),
            (login, 'Verify login with wrong password', 'Negative', 'High', True,
             '1. Open login page\n2. Enter valid username, wrong password\n3. Click Login', 'Error "Invalid credentials" is shown'),
            (signup, 'Verify sign up with a new email', 'Positive', 'Medium', False,
             '1. Open sign up page\n2. Fill all fields\n3. Submit', 'Account is created and user is logged in'),
            (signup, 'Verify sign up rejects duplicate email', 'Negative', 'Medium', False,
             '1. Open sign up page\n2. Enter an existing email\n3. Submit', 'Error "email already registered" is shown'),
            (cart, 'Add product to cart', 'Functional', 'Critical', True,
             '1. Open a product\n2. Click Add to cart', 'Cart count increases by 1'),
            (cart, 'Remove product from cart', 'Functional', 'Medium', False,
             '1. Open cart\n2. Click Remove on an item', 'Item is removed and total is updated'),
        ]
        objs = [TestCase.objects.create(project=project, module=m, title=t, case_type=ct, priority=p, automatable=a,
                                        steps=s, expected_result=e, created_by=user)
                for m, t, ct, p, a, s, e in cases]

        runner = TestRunner.objects.create(project=project, release=r1, name='Sprint 1 Smoke', assigned_to=user,
                                           created_by=user, description='Smoke test before v1.0.0')
        TestResult.objects.bulk_create([TestResult(runner=runner, test_case=tc) for tc in objs])
        self.stdout.write(self.style.SUCCESS('Demo data created. Login: demo / Demo@12345'))
