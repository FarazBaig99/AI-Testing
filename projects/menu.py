"""Left sidebar menu (TestOps layout) and the context processor that renders it.

Each item is one of:
- url_name: an existing page that needs no project (dashboard, test management, ...)
- project_url: an existing per-project page; the menu goes through a project picker first
- neither: a module not built yet; opens its "coming soon" page with `about` and `planned`
"""
from django.urls import reverse

MENU = [
    {'title': None, 'items': [
        {'slug': 'dashboard', 'label': 'Dashboard', 'icon': 'grid', 'url_name': 'dashboard'},
        {'slug': 'my-tasks', 'label': 'My Tasks', 'icon': 'clipboard-check', 'url_name': 'my_tasks'},
    ]},
    {'title': 'Project & Business Analysis', 'key': 'analysis', 'items': [
        {'slug': 'projects', 'label': 'Projects', 'icon': 'folder2-open', 'url_name': 'portfolio_list'},
        {'slug': 'requirements', 'label': 'Requirements & Docs', 'icon': 'file-earmark-text', 'badge': 'AI',
         'about': 'Upload and manage requirement documents (BRD, SRS, user stories) for each project.',
         'planned': ['Requirement documents per project', 'Requirement IDs and versions',
                     'AI extraction of requirements from documents']},
        {'slug': 'business-scenarios', 'label': 'Business Scenarios', 'icon': 'layers',
         'about': 'End-to-end business flows built from requirements, used as the base for test cases.',
         'planned': ['Scenario builder', 'Scenario repository', 'Link scenarios to test cases']},
        {'slug': 'rtm', 'label': 'RTM & Traceability', 'icon': 'diagram-3',
         'about': 'Requirement Traceability Matrix: which test cases cover which requirement.',
         'planned': ['Requirement list per project', 'Link test cases to requirements',
                     'Coverage and execution status per requirement']},
        {'slug': 'risk-register', 'label': 'Risk Register', 'icon': 'exclamation-triangle',
         'about': 'Track project and quality risks with impact, likelihood and mitigation.',
         'planned': ['Risk log with owner and status', 'Impact x likelihood rating', 'Mitigation tracking']},
    ]},
    {'title': 'Quality Engineering', 'key': 'quality', 'items': [
        {'slug': 'test-management', 'label': 'Test Management', 'icon': 'check2-square',
         'url_name': 'project_list'},
        {'slug': 'defects', 'label': 'Defect Management', 'icon': 'bug',
         'about': 'Log, assign and track defects raised from failed test executions.',
         'planned': ['Defect log with severity and priority', 'Auto-create defect from a failed result',
                     'Retest and closure workflow']},
        {'slug': 'automation', 'label': 'Automation Testing', 'icon': 'cpu',
         'about': 'Manage automated test suites and their run results.',
         'planned': ['Automation suites and scripts', 'Run history and reports', 'Link to automatable test cases']},
        {'slug': 'api-testing', 'label': 'API Testing', 'icon': 'terminal', 'url_name': 'api_workspace'},
        {'slug': 'database-testing', 'label': 'Database Testing', 'icon': 'database',
         'about': 'Verify application data directly in the database during test execution.',
         'planned': ['Database connections per environment', 'SQL query checks with expected results',
                     'Attach query results as test evidence']},
        {'slug': 'performance', 'label': 'Performance Testing', 'icon': 'speedometer2',
         'about': 'Plan and record load, stress and response-time tests.',
         'planned': ['Performance test plans', 'Response time and throughput results', 'Baseline comparison']},
        {'slug': 'security', 'label': 'Security Testing', 'icon': 'shield-check',
         'about': 'Track security test checklists and vulnerability findings.',
         'planned': ['OWASP-based checklists', 'Vulnerability findings with severity', 'Fix verification']},
        {'slug': 'cross-browser', 'label': 'Cross-Browser & Responsive', 'icon': 'globe',
         'about': 'Check that the application works across browsers and screen sizes.',
         'planned': ['Browser / device matrix', 'Results per browser and screen size', 'Screenshots as evidence']},
        {'slug': 'mobile-desktop', 'label': 'Mobile & Desktop Testing', 'icon': 'phone',
         'about': 'Test runs for mobile apps and desktop software.',
         'planned': ['Device and OS list', 'App build versions', 'Execution results per device']},
    ]},
    {'title': 'Delivery & Governance', 'key': 'delivery', 'items': [
        {'slug': 'reports', 'label': 'QA Reports & Analytics', 'icon': 'bar-chart',
         'about': 'Execution, defect and coverage reports across projects.',
         'planned': ['Execution summary reports', 'Defect trends', 'Export to PDF / Excel']},
        {'slug': 'releases', 'label': 'Releases & Quality Gates', 'icon': 'rocket-takeoff',
         'project_url': 'project_detail', 'anchor': '#tab-releases'},
        {'slug': 'environments', 'label': 'Environments & Integrations', 'icon': 'hdd-network',
         'project_url': 'api_environments'},
    ]},
    {'title': 'AI & Administration', 'key': 'admin', 'items': [
        {'slug': 'ai-assistant', 'label': 'AI QA Assistant', 'icon': 'stars', 'badge': 'GenAI',
         'about': 'AI help for writing test cases, reviewing coverage and analysing failures.',
         'planned': ['Generate test cases from requirements', 'Suggest missing scenarios',
                     'Summarise failed executions']},
        {'slug': 'org-team', 'label': 'Org & Team', 'icon': 'people', 'url_name': 'org_home'},
        {'slug': 'settings', 'label': 'Settings & Audit', 'icon': 'gear',
         'about': 'Application settings and an audit log of changes.',
         'planned': ['General settings', 'Audit trail of who changed what', 'Data export']},
    ]},
]

ITEMS = {item['slug']: item for section in MENU for item in section['items']}

# url_name -> menu slug, for pages that are not the menu item's own page
ACTIVE_BY_URL = {
    'dashboard': 'dashboard', 'my_tasks': 'my-tasks', 'profile': None,
    'release_create': 'releases', 'release_edit': 'releases',
    'api_environments': 'environments', 'api_environment_edit': 'environments',
    'api_environment_delete': 'environments',
}


def item_url(item):
    if 'url_name' in item:
        return reverse(item['url_name'])
    if 'project_url' in item:
        return reverse('menu_pick_project', args=[item['slug']])
    return reverse('menu_module', args=[item['slug']])


def active_slug(request):
    match = request.resolver_match
    if not match:
        return None
    if 'slug' in match.kwargs:
        return match.kwargs['slug']
    name = match.url_name
    if name in ACTIVE_BY_URL:
        return ACTIVE_BY_URL[name]
    if name and name.startswith(('portfolio_', 'delivery_', 'integrations_')):
        return 'projects'
    if name and name.startswith('org_'):
        return 'org-team'
    if name and name.startswith('api_'):
        return 'api-testing'
    if request.path.startswith('/projects/'):
        return 'test-management'
    return None


def sidebar(request):
    if not request.user.is_authenticated:
        return {}
    active = active_slug(request)
    sections = []
    for section in MENU:
        items = [dict(item, url=item_url(item), active=item['slug'] == active) for item in section['items']]
        sections.append(dict(section, items=items, has_active=any(i['active'] for i in items)))
    return {'sidebar_menu': sections}
