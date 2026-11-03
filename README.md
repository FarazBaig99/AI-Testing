# Test Manager (Python / Django)

A simple test management app modelled on PSW TestManager Pro.

## Features
- **Accounts**: Register, Login, Logout, Profile (job title, department, capacity, change password)
- **Dashboard**: workspace overview and execution summary
- **My Tasks**: test runners assigned to you, filter by Pending / In Progress / Completed
- **Projects**: create / edit / delete / search
- **Releases**: per project (e.g. `v1.0.0`) – shown as a dropdown when creating a test runner
- **Modules**: parent modules and submodules (tree view, expand / collapse)
- **Test Cases**: title, description, preconditions, steps, expected result, priority, label/type, automatable
- **Test Runners**: pick release, dates, assignee and test cases → execute each case as
  Pass / Fail / Blocked / On Hold / Partial Pass with comments; progress and pass rate shown
  (keyboard shortcuts P / F / B / H / S on the execute page)

## Run
```bash
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo          # optional: demo user + sample project
python manage.py createsuperuser    # optional: for /admin/
python manage.py runserver
```
Open http://127.0.0.1:8000/ — demo login: `demo` / `Demo@12345` (or register a new account).

## Structure
```
testmanager/   settings & root urls
accounts/      register, login, logout, profile
projects/      projects, releases, modules, test cases, test runners, results
templates/     Bootstrap 5 UI (sidebar layout)
```
