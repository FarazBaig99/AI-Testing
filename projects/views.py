from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone

from .forms import ModuleForm, ProjectForm, ReleaseForm, TestCaseForm, TestRunnerForm
from .models import Module, Project, Release, TestCase, TestResult, TestRunner


@login_required
def dashboard(request):
    runners = TestRunner.objects.prefetch_related('results')
    results = TestResult.objects.all()
    context = {
        'project_count': Project.objects.count(),
        'module_count': Module.objects.count(),
        'case_count': TestCase.objects.count(),
        'automatable_count': TestCase.objects.filter(automatable=True).count(),
        'runner_count': runners.count(),
        'pass_count': results.filter(status='Pass').count(),
        'fail_count': results.filter(status='Fail').count(),
        'pending_count': results.filter(status='Pending').count(),
        'recent_runners': runners[:5],
        'my_runner_count': runners.filter(assigned_to=request.user).count(),
    }
    return render(request, 'projects/dashboard.html', context)


@login_required
def my_tasks(request):
    runners = TestRunner.objects.filter(assigned_to=request.user).select_related('project', 'created_by')
    runners = list(runners.prefetch_related('results'))
    status_filter = request.GET.get('status', 'All')
    stats = {
        'total': len(runners),
        'pending': sum(r.status == 'Pending' for r in runners),
        'in_progress': sum(r.status == 'In Progress' for r in runners),
        'completed': sum(r.status == 'Completed' for r in runners),
    }
    if status_filter != 'All':
        runners = [r for r in runners if r.status == status_filter]
    return render(request, 'projects/my_tasks.html',
                  {'runners': runners, 'stats': stats, 'status_filter': status_filter})


# ---------- Projects ----------

@login_required
def project_list(request):
    search = request.GET.get('search', '')
    projects = Project.objects.annotate(
        num_modules=Count('modules', filter=Q(modules__parent__isnull=True), distinct=True),
        num_cases=Count('test_cases', distinct=True),
        num_runners=Count('runners', distinct=True),
    ).select_related('created_by').prefetch_related('linked_projects')
    if search:
        projects = projects.filter(Q(name__icontains=search) | Q(description__icontains=search))
    return render(request, 'projects/project_list.html', {
        'projects': projects, 'search': search,
        'total_cases': TestCase.objects.count(), 'total_modules': Module.objects.count(),
    })


@login_required
def project_form(request, pk=None):
    project = get_object_or_404(Project, pk=pk) if pk else None
    form = ProjectForm(request.POST or None, instance=project)
    if form.is_valid():
        obj = form.save(commit=False)
        if not pk:
            obj.created_by = request.user
        obj.save()
        form.save_m2m()
        messages.success(request, f'Project "{obj.name}" saved.')
        return redirect('project_detail', pk=obj.pk)
    return render(request, 'projects/form.html', {
        'form': form, 'title': 'Edit Project' if pk else 'New Project',
        'back_url': project and f'/projects/{project.pk}/' or '/projects/',
    })


@login_required
def project_delete(request, pk):
    project = get_object_or_404(Project, pk=pk)
    if request.method == 'POST':
        project.delete()
        messages.success(request, 'Project deleted.')
        return redirect('project_list')
    return render(request, 'projects/confirm_delete.html', {'object': project, 'back_url': f'/projects/{pk}/'})


@login_required
def project_detail(request, pk):
    project = get_object_or_404(Project, pk=pk)
    search = request.GET.get('search', '')
    root_modules = project.modules.filter(parent__isnull=True).prefetch_related('children__test_cases', 'test_cases')
    cases = project.test_cases.all()
    results = TestResult.objects.filter(runner__project=project)
    total_results = results.count()
    pass_count = results.filter(status='Pass').count()
    matches = None
    if search:
        query = Q(title__icontains=search) | Q(module__name__icontains=search)
        if search.lstrip('#').isdigit():
            query |= Q(id=int(search.lstrip('#')))
        matches = cases.filter(query).select_related('module')
    context = {
        'project': project,
        'root_modules': root_modules,
        'search': search,
        'matches': matches,
        'stats': {
            'cases': cases.count(),
            'automatable': cases.filter(automatable=True).count(),
            'manual': cases.filter(automatable=False).count(),
            'modules': project.modules.filter(parent__isnull=True).count(),
            'submodules': project.modules.filter(parent__isnull=False).count(),
            'runners': project.runners.count(),
            'releases': project.releases.count(),
            'api_cases': project.api_test_cases.count(),
            'passed': pass_count,
            'failed': results.filter(status='Fail').count(),
            'pending': results.filter(status='Pending').count(),
            'pass_rate': round(pass_count * 100 / total_results, 1) if total_results else 0,
        },
        'labels': cases.values('case_type').annotate(n=Count('id')).order_by('-n'),
        'releases': project.releases.annotate(n=Count('runners')),
    }
    return render(request, 'projects/project_detail.html', context)


# ---------- Releases ----------

@login_required
def release_form(request, pk, release_id=None):
    project = get_object_or_404(Project, pk=pk)
    release = get_object_or_404(Release, pk=release_id, project=project) if release_id else None
    form = ReleaseForm(request.POST or None, instance=release)
    if form.is_valid():
        obj = form.save(commit=False)
        obj.project = project
        obj.save()
        messages.success(request, f'Release "{obj.name}" saved.')
        return redirect('project_detail', pk=pk)
    return render(request, 'projects/form.html', {
        'form': form, 'project': project, 'title': 'Edit Release' if release else 'New Release',
        'back_url': f'/projects/{pk}/',
    })


# ---------- Modules ----------

@login_required
def module_form(request, pk, module_id=None):
    project = get_object_or_404(Project, pk=pk)
    module = get_object_or_404(Module, pk=module_id, project=project) if module_id else None
    initial = {'parent': request.GET.get('parent')} if not module else None
    form = ModuleForm(request.POST or None, instance=module, project=project, initial=initial)
    if form.is_valid():
        obj = form.save(commit=False)
        obj.project = project
        obj.save()
        messages.success(request, f'Module "{obj.name}" saved.')
        return redirect('project_detail', pk=pk)
    return render(request, 'projects/form.html', {
        'form': form, 'project': project, 'title': 'Edit Module' if module else 'Add Module',
        'back_url': f'/projects/{pk}/',
    })


@login_required
def module_delete(request, pk, module_id):
    module = get_object_or_404(Module, pk=module_id, project_id=pk)
    if request.method == 'POST':
        module.delete()
        messages.success(request, 'Module deleted.')
        return redirect('project_detail', pk=pk)
    return render(request, 'projects/confirm_delete.html', {'object': module, 'back_url': f'/projects/{pk}/'})


# ---------- Test cases ----------

@login_required
def testcase_form(request, pk, case_id=None):
    project = get_object_or_404(Project, pk=pk)
    if not project.modules.exists():
        messages.warning(request, 'Create a module first, then add test cases to it.')
        return redirect('module_create', pk=pk)
    case = get_object_or_404(TestCase, pk=case_id, project=project) if case_id else None
    initial = {'module': request.GET.get('module')} if not case else None
    form = TestCaseForm(request.POST or None, instance=case, project=project, initial=initial)
    if form.is_valid():
        obj = form.save(commit=False)
        obj.project = project
        if not case:
            obj.created_by = request.user
        obj.save()
        messages.success(request, f'Test case #{obj.pk} saved.')
        if 'save_add' in request.POST:
            return redirect(f'/projects/{pk}/testcases/create/?module={obj.module_id}')
        return redirect('testcase_detail', pk=pk, case_id=obj.pk)
    return render(request, 'projects/form.html', {
        'form': form, 'project': project, 'title': f'Edit Test Case #{case.pk}' if case else 'New Test Case',
        'back_url': f'/projects/{pk}/', 'save_add': not case,
    })


@login_required
def testcase_detail(request, pk, case_id):
    case = get_object_or_404(TestCase.objects.select_related('project', 'module'), pk=case_id, project_id=pk)
    history = case.results.select_related('runner', 'executed_by', 'environment').exclude(status='Pending').order_by('-executed_at')
    return render(request, 'projects/testcase_detail.html', {
        'case': case, 'project': case.project, 'steps': [s for s in case.steps.splitlines() if s.strip()],
        'history': history,
    })


@login_required
def testcase_delete(request, pk, case_id):
    case = get_object_or_404(TestCase, pk=case_id, project_id=pk)
    if request.method == 'POST':
        case.delete()
        messages.success(request, 'Test case deleted.')
        return redirect('project_detail', pk=pk)
    return render(request, 'projects/confirm_delete.html', {'object': case, 'back_url': f'/projects/{pk}/'})


# ---------- Test runners ----------

@login_required
def runner_list(request, pk):
    project = get_object_or_404(Project, pk=pk)
    runners = list(project.runners.select_related('release', 'assigned_to', 'created_by')
                   .prefetch_related('results__environment'))
    search = request.GET.get('search', '')
    status = request.GET.get('status', '')
    release = request.GET.get('release', '')
    if search:
        runners = [r for r in runners if search.lower() in r.name.lower()]
    if status:
        runners = [r for r in runners if r.status == status]
    if release:
        runners = [r for r in runners if str(r.release_id) == release]
    all_runners = list(project.runners.prefetch_related('results'))
    return render(request, 'projects/runner_list.html', {
        'project': project, 'runners': runners, 'releases': project.releases.all(),
        'search': search, 'status': status, 'release': release,
        'stats': {
            'total': len(all_runners),
            'completed': sum(r.status == 'Completed' for r in all_runners),
            'in_progress': sum(r.status == 'In Progress' for r in all_runners),
            'pending': sum(r.status == 'Pending' for r in all_runners),
        },
    })


@login_required
def runner_create(request, pk):
    project = get_object_or_404(Project, pk=pk)
    form = TestRunnerForm(request.POST or None, project=project,
                          initial={'execution_date': timezone.localdate()})
    if form.is_valid():
        runner = form.save(commit=False)
        runner.project = project
        runner.created_by = request.user
        runner.save()
        runner.test_cases.set(form.cleaned_data['test_cases'])
        _ensure_results(runner, runner.environment)
        env_note = f' on {runner.environment}' if runner.environment else ''
        messages.success(request, f'Test runner "{runner.name}" created with {runner.test_cases.count()} test cases{env_note}.')
        return redirect('runner_execute', pk=pk, runner_id=runner.pk)
    modules = project.modules.filter(parent__isnull=True).prefetch_related('test_cases', 'children__test_cases')
    return render(request, 'projects/runner_create.html', {'form': form, 'project': project, 'modules': modules})


def _ensure_results(runner, environment):
    """Every test case of the runner gets a Pending result the first time it runs on an environment."""
    have = set(runner.results.filter(environment=environment).values_list('test_case_id', flat=True))
    TestResult.objects.bulk_create([TestResult(runner=runner, test_case_id=tc_id, environment=environment)
                                    for tc_id in runner.test_cases.values_list('id', flat=True)
                                    if tc_id not in have])


def _runner_env(request, runner):
    """?env=<id>, else the runner's starting environment, else the project's first environment (None if none)."""
    envs = runner.project.api_environments.all()
    env_id = request.GET.get('env') or request.POST.get('env')
    env = envs.filter(pk=env_id).first() if str(env_id or '').isdigit() else None
    return env or runner.environment or envs.first()


@login_required
def runner_execute(request, pk, runner_id):
    runner = get_object_or_404(TestRunner.objects.select_related('project', 'release', 'assigned_to', 'environment'),
                               pk=runner_id, project_id=pk)
    env = _runner_env(request, runner)
    _ensure_results(runner, env)
    env_results = runner.results.filter(environment=env)
    env_query = f'?env={env.pk}' if env else ''

    if request.method == 'POST':
        changed = 0
        for result in env_results:
            status = request.POST.get(f'status_{result.pk}')
            comment = request.POST.get(f'comment_{result.pk}', '').strip()
            if status and (status != result.status or comment != result.comment):
                result.status = status
                result.comment = comment
                result.executed_by = request.user
                result.executed_at = timezone.now()
                result.save()
                changed += 1
        messages.success(request, f'{changed} result(s) saved' + (f' for {env}.' if env else '.'))
        return redirect(reverse('runner_execute', args=[pk, runner_id]) + env_query)

    status_filter = request.GET.get('status', 'All')
    results = env_results.select_related('test_case__module__parent', 'executed_by')
    if status_filter != 'All':
        results = results.filter(status=status_filter)
    groups = {}
    for r in results:
        groups.setdefault(str(r.test_case.module), []).append(r)
    groups = dict(sorted(groups.items()))
    progress = {row['environment'].pk: row for row in runner.environment_progress() if row['environment']}
    return render(request, 'projects/runner_execute.html', {
        'runner': runner, 'project': runner.project, 'groups': groups, 'counts': runner.counts(env_results),
        'env': env, 'env_query': env_query.replace('?', '&'),
        'env_tabs': [{'env': e, 'row': progress.get(e.pk)} for e in runner.project.api_environments.all()],
        'status_choices': [s for s, _ in TestResult.STATUS_CHOICES], 'status_filter': status_filter,
    })


@login_required
def runner_delete(request, pk, runner_id):
    runner = get_object_or_404(TestRunner, pk=runner_id, project_id=pk)
    if request.method == 'POST':
        runner.delete()
        messages.success(request, 'Test runner deleted.')
        return redirect('runner_list', pk=pk)
    return render(request, 'projects/confirm_delete.html', {'object': runner, 'back_url': f'/projects/{pk}/runners/'})
