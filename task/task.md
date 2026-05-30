# SWE-bench Lite task: pallets__flask-5063

**Repo:** pallets/flask  
**Base commit (already checked out at /workspace):** `182ce3dd15dfa3537391c3efaf9c3ff407d134d4`

## Problem statement

Flask routes to return domain/sub-domains information
Currently when checking **flask routes** it provides all routes but **it is no way to see which routes are assigned to which subdomain**.

**Default server name:**
SERVER_NAME: 'test.local'

**Domains (sub-domains):**
test.test.local
admin.test.local
test.local

**Adding blueprints:**
app.register_blueprint(admin_blueprint,url_prefix='',subdomain='admin')
app.register_blueprint(test_subdomain_blueprint,url_prefix='',subdomain='test')


```
$ flask routes
 * Tip: There are .env or .flaskenv files present. Do "pip install python-dotenv" to use them.
Endpoint                                                 Methods    Rule
-------------------------------------------------------  ---------  ------------------------------------------------
admin_blueprint.home                                      GET        /home
test_subdomain_blueprint.home                             GET        /home
static                                                    GET        /static/<path:filename>
...
```


**Feature request**
It will be good to see something like below (that will make more clear which route for which subdomain, because now need to go and check configuration).
**If it is not possible to fix routes**, can you add or tell which method(s) should be used to get below information from flask? 

```
$ flask routes
 * Tip: There are .env or .flaskenv files present. Do "pip install python-dotenv" to use them.
Domain                Endpoint                                             Methods    Rule
-----------------   ----------------------------------------------------  ----------  ------------------------------------------------
admin.test.local     admin_blueprint.home                                  GET        /home
test.test.local      test_subdomain_blueprint.home                         GET        /home
test.local           static                                                GET        /static/<path:filename>
...
```



## Acceptance tests (must pass after your fix)

- `tests/test_cli.py::TestRoutes::test_subdomain`
- `tests/test_cli.py::TestRoutes::test_host`

## Existing-test regressions to avoid

Make sure these still pass: `tests/test_cli.py::TestRoutes` (all sibling tests).
