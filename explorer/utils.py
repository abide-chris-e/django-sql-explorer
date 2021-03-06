import functools
import csv
import cStringIO
import json
import re
from time import time
from explorer import app_settings
from django.db import connections, connection, router, models, transaction, DatabaseError
from django.http import HttpResponse

EXPLORER_PARAM_TOKEN = "$$"

## SQL Specific Things


def passes_blacklist(sql):
    clean = functools.reduce(lambda sql, term: sql.upper().replace(term, ""), app_settings.EXPLORER_SQL_WHITELIST, sql)
    return not any(write_word in clean.upper() for write_word in app_settings.EXPLORER_SQL_BLACKLIST)


def execute_query(sql):
    conn = None
    apps = [a for a in models.get_apps() if a.__package__ not in app_settings.EXPLORER_SCHEMA_EXCLUDE_APPS]
    for app in apps:
        for model in models.get_models(app):
            if model._meta.db_table in sql:
                conn = connections[router.db_for_read(model)]
    if conn is None:
        conn = connections[app_settings.EXPLORER_CONNECTION_NAME] if app_settings.EXPLORER_CONNECTION_NAME\
            else connection
    cursor = conn.cursor()
    start_time = time()

    sid = transaction.savepoint()
    try:
        cursor.execute(sql)
        transaction.savepoint_commit(sid)
    except DatabaseError:
        transaction.savepoint_rollback(sid)
        raise

    end_time = time()
    duration = (end_time - start_time) * 1000
    return cursor, duration


def execute_and_fetch_query(sql):
    cursor, duration = execute_query(sql)
    headers = [d[0] for d in cursor.description] if cursor.description else ['--']
    transforms = get_transforms(headers, app_settings.EXPLORER_TRANSFORMS)
    data = [transform_row(transforms, r) for r in cursor.fetchall()]
    return headers, data, duration, None


def get_transforms(headers, transforms):
    relevant_transforms = []
    for field, template in transforms:
        try:
            relevant_transforms.append((headers.index(field), template))
        except ValueError:
            pass
    return relevant_transforms


def transform_row(transforms, row):
    row = [x.encode('utf-8') if type(x) is unicode else x for x in list(row)]
    for i, t in transforms:
        row[i] = t.format(str(row[i]))
    return row


def schema_info():
    """
    Construct schema information via introspection of the django models in the database.

    :return: Schema information of the following form, sorted by db_table_name.
        [
            ("package.name -> ModelClass", "db_table_name",
                [
                    ("db_column_name", "DjangoFieldType"),
                    (...),
                ]
            )
        ]

    """

    ret = []
    apps = [a for a in models.get_apps() if a.__package__ not in app_settings.EXPLORER_SCHEMA_EXCLUDE_APPS]
    for app in apps:
        for model in models.get_models(app):
            friendly_model = "%s -> %s" % (app.__package__, model._meta.object_name)
            ret.append((
                          friendly_model,
                          model._meta.db_table,
                          [_format_field(f) for f in model._meta.fields]
                      ))

            #Do the same thing for many_to_many fields. These don't show up in the field list of the model
            #because they are stored as separate "through" relations and have their own tables
            ret += [(
                       friendly_model,
                       m2m.rel.through._meta.db_table,
                       [_format_field(f) for f in m2m.rel.through._meta.fields]
                    ) for m2m in model._meta.many_to_many]

    return sorted(ret, key=lambda t: t[1])


def _format_field(field):
    return (field.get_attname_column()[1], field.get_internal_type())


def param(name):
    return "%s%s%s" % (EXPLORER_PARAM_TOKEN, name, EXPLORER_PARAM_TOKEN)


def swap_params(sql, params):
    p = params.items() if params else {}
    for k, v in p:
        sql = sql.replace(param(k), str(v))
    return sql


def extract_params(text):
    regex = re.compile("\$\$([a-zA-Z0-9_|-]+)\$\$")
    params = re.findall(regex, text)
    return dict(zip(params, ['' for i in range(len(params))]))


def write_csv(headers, data):
    csv_data = cStringIO.StringIO()
    writer = csv.writer(csv_data)
    writer.writerow(headers)
    map(lambda row: writer.writerow(row), data)
    return csv_data.getvalue()


def build_download_response(query, request):
    data = csv_report(query, url_get_params(request))
    response = HttpResponse(data, content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename=%s.csv' % query.title.replace(',', '')
    response['Content-Length'] = len(data)
    return response


def csv_report(query, params=None):
    headers, data, duration, error = query.headers_and_data(params)
    if error:
        return error
    return write_csv(headers, data)


## Helpers
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.views import login
from django.contrib.auth import REDIRECT_FIELD_NAME


def safe_admin_login_prompt(request):
    defaults = {
        'template_name': 'admin/login.html',
        'authentication_form': AdminAuthenticationForm,
        'extra_context': {
            'title': 'Log in',
            'app_path': request.get_full_path(),
            REDIRECT_FIELD_NAME: request.get_full_path(),
        },
    }
    return login(request, **defaults)


def shared_dict_update(target, source):
    for k_d1 in target:
        if k_d1 in source:
            target[k_d1] = source[k_d1]
    return target


def safe_cast(val, to_type, default=None):
    try:
        return to_type(val)
    except ValueError:
        return default


def safe_json(val):
    try:
        return json.loads(val)
    except ValueError:
        return None


def get_int_from_request(request, name, default):
    val = request.GET.get(name, default)
    return safe_cast(val, int, default) if val else None


def get_json_from_request(request, name):
    val = request.GET.get(name, None)
    return safe_json(val) if val else None


def url_get_rows(request):
    return get_int_from_request(request, 'rows', app_settings.EXPLORER_DEFAULT_ROWS)


def url_get_query_id(request):
    return get_int_from_request(request, 'query_id', None)


def url_get_log_id(request):
    return get_int_from_request(request, 'querylog_id', None)


def url_get_params(request):
    return get_json_from_request(request, 'params')



