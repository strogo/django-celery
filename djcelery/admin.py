from pprint import pformat

from django import forms
from django.contrib import admin
from django.contrib.admin import helpers
from django.contrib.admin.views import main as main_views
from django.shortcuts import render_to_response
from django.template import RequestContext
from django.utils.encoding import force_unicode
from django.utils.html import escape
from django.utils.translation import ugettext_lazy as _

from celery import states
from celery import registry
from celery.app import default_app
from celery.task.control import broadcast, revoke, rate_limit
from celery.utils import abbrtask

from djcelery import loaders
from djcelery.models import TaskState, WorkerState
from djcelery.models import PeriodicTask, IntervalSchedule, CrontabSchedule
from djcelery.utils import naturaldate


TASK_STATE_COLORS = {states.SUCCESS: "green",
                     states.FAILURE: "red",
                     states.REVOKED: "magenta",
                     states.STARTED: "yellow",
                     states.RETRY: "orange",
                     "RECEIVED": "blue"}
NODE_STATE_COLORS = {"ONLINE": "green",
                     "OFFLINE": "gray"}


class MonitorList(main_views.ChangeList):

    def __init__(self, *args, **kwargs):
        super(MonitorList, self).__init__(*args, **kwargs)
        self.title = self.model_admin.list_page_title


def attrs(**kwargs):
    def _inner(fun):
        for attr_name, attr_value in kwargs.items():
            setattr(fun, attr_name, attr_value)
        return fun
    return _inner


def display_field(short_description, admin_order_field, allow_tags=True,
        **kwargs):
    return attrs(short_description=short_description,
                 admin_order_field=admin_order_field,
                 allow_tags=allow_tags, **kwargs)


def action(short_description, **kwargs):
    return attrs(short_description=short_description, **kwargs)


@display_field(_("state"), "state")
def colored_state(task):
    state = escape(task.state)
    color = TASK_STATE_COLORS.get(task.state, "black")
    return """<b><span style="color: %s;">%s</span></b>""" % (color, state)


@display_field(_("state"), "last_timestamp")
def node_state(node):
    state = node.is_alive() and "ONLINE" or "OFFLINE"
    color = NODE_STATE_COLORS[state]
    return """<b><span style="color: %s;">%s</span></b>""" % (color, state)


@display_field(_("ETA"), "eta")
def eta(task):
    if not task.eta:
        return """<span style="color: gray;">none</span>"""
    return escape(task.eta)


@display_field(_("when"), "tstamp")
def tstamp(task):
    return """<div title="%s">%s</div>""" % (escape(str(task.tstamp)),
                                             escape(naturaldate(task.tstamp)))


@display_field(_("name"), "name")
def name(task):
    short_name = abbrtask(task.name, 16)
    return """<div title="%s"><b>%s</b></div>""" % (escape(task.name),
                                                    escape(short_name))


def fixedwidth(field, name=None, pt=6, width=16, maxlen=64, pretty=False):

    @display_field(name or field, field)
    def f(task):
        val = getattr(task, field)
        if pretty:
            val = pformat(val, width=width)
        if val.startswith("u'") or val.startswith('u"'):
            val = val[2:-1]
        shortval = val.replace(",", ",\n")
        shortval = shortval.replace("\n", "<br />")

        if len(shortval) > maxlen:
            shortval = shortval[:maxlen] + "..."
        return """<span title="%s", style="font-size: %spt;
                               font-family: Menlo, Courier;
                  ">%s</span>""" % (escape(val[:255]), pt, escape(shortval), )
    return f


class ModelMonitor(admin.ModelAdmin):
    can_add = False
    can_delete = False

    def get_changelist(self, request, **kwargs):
        return MonitorList

    def change_view(self, request, object_id, extra_context=None):
        extra_context = extra_context or {}
        extra_context.setdefault("title", self.detail_title)
        return super(ModelMonitor, self).change_view(request, object_id,
                                                     extra_context)

    def has_delete_permission(self, request, obj=None):
        if not self.can_delete:
            return False
        return super(ModelMonitor, self).has_delete_permission(request, obj)

    def has_add_permission(self, request):
        if not self.can_add:
            return False
        return super(ModelMonitor, self).has_add_permission(request)


class TaskMonitor(ModelMonitor):
    detail_title = _("Task detail")
    list_page_title = _("Tasks")
    rate_limit_confirmation_template = "djcelery/confirm_rate_limit.html"
    date_hierarchy = "tstamp"
    fieldsets = (
            (None, {
                "fields": ("state", "task_id", "name", "args", "kwargs",
                           "eta", "runtime", "worker", "tstamp"),
                "classes": ("extrapretty", ),
            }),
            ("Details", {
                "classes": ("collapse", "extrapretty"),
                "fields": ("result", "traceback", "expires"),
            }),
    )
    list_display = (fixedwidth("task_id", name=_("UUID"), pt=8),
                    colored_state,
                    name,
                    fixedwidth("args", pretty=True),
                    fixedwidth("kwargs", pretty=True),
                    eta,
                    tstamp,
                    "worker")
    readonly_fields = ("state", "task_id", "name", "args", "kwargs",
                       "eta", "runtime", "worker", "result", "traceback",
                       "expires", "tstamp")
    list_filter = ("state", "name", "tstamp", "eta", "worker")
    search_fields = ("name", "task_id", "args", "kwargs", "worker__hostname")
    actions = ["revoke_tasks",
               "rate_limit_tasks"]

    @action(_("Revoke selected tasks"))
    def revoke_tasks(self, request, queryset):
        connection = default_app.broker_connection()
        try:
            for state in queryset:
                revoke(state.task_id, connection=connection)
        finally:
            connection.close()

    @action(_("Rate limit selected tasks"))
    def rate_limit_tasks(self, request, queryset):
        tasks = set([task.name for task in queryset])
        opts = self.model._meta
        app_label = opts.app_label
        if request.POST.get("post"):
            rate = request.POST["rate_limit"]
            connection = default_app.broker_connection()
            try:
                for task_name in tasks:
                    rate_limit(task_name, rate, connection=connection)
            finally:
                connection.close()
            return None

        context = {
            "title": _("Rate limit selection"),
            "queryset": queryset,
            "object_name": force_unicode(opts.verbose_name),
            "action_checkbox_name": helpers.ACTION_CHECKBOX_NAME,
            "opts": opts,
            "root_path": self.admin_site.root_path,
            "app_label": app_label,
        }

        return render_to_response(self.rate_limit_confirmation_template,
                context, context_instance=RequestContext(request))

    def get_actions(self, request):
        actions = super(TaskMonitor, self).get_actions(request)
        actions.pop("delete_selected", None)
        return actions


class WorkerMonitor(ModelMonitor):
    can_add = True
    detail_title = _("Node detail")
    list_page_title = _("Worker Nodes")
    list_display = ("hostname", node_state)
    readonly_fields = ("last_heartbeat", )
    actions = ["shutdown_nodes",
               "enable_events",
               "disable_events"]

    @action(_("Shutdown selected worker nodes"))
    def shutdown_nodes(self, request, queryset):
        broadcast("shutdown", destination=[n.hostname for n in queryset])

    @action(_("Enable event mode for selected nodes."))
    def enable_events(self, request, queryset):
        broadcast("enable_events",
                  destination=[n.hostname for n in queryset])

    @action(_("Disable event mode for selected nodes."))
    def disable_events(self, request, queryset):
        broadcast("disable_events",
                  destination=[n.hostname for n in queryset])

    def get_actions(self, request):
        actions = super(WorkerMonitor, self).get_actions(request)
        actions.pop("delete_selected", None)
        return actions

admin.site.register(TaskState, TaskMonitor)
admin.site.register(WorkerState, WorkerMonitor)


# ### Periodic Tasks


class LaxChoiceField(forms.ChoiceField):

    def valid_value(self, value):
        return True


def periodic_task_form():
    loaders.autodiscover()
    tasks = list(sorted(registry.tasks.regular().keys()))
    choices = (("", ""), ) + tuple(zip(tasks, tasks))

    class PeriodicTaskForm(forms.ModelForm):
        regtask = LaxChoiceField(label=_(u"Task (registered)"),
                                 choices=choices, required=False)
        task = forms.CharField(label=_("Task (custom)"), required=False,
                               max_length=200)

        class Meta:
            model = PeriodicTask

        def clean(self):
            data = super(PeriodicTaskForm, self).clean()
            regtask = data.get("regtask")
            if regtask:
                data["task"] = regtask
            if not data["task"]:
                exc = forms.ValidationError(_(u"Need name of task"))
                self._errors["task"] = self.error_class(exc.messages)
                raise exc
            return data

    return PeriodicTaskForm


class PeriodicTaskAdmin(admin.ModelAdmin):
    model = PeriodicTask
    form = periodic_task_form()
    fieldsets = (
            (None, {
                "fields": ("name", "regtask", "task", "enabled"),
                "classes": ("extrapretty", "wide"),
            }),
            ("Schedule", {
                "fields": ("interval", "crontab"),
                "classes": ("extrapretty", "wide", ),
            }),
            ("Arguments", {
                "fields": ("args", "kwargs"),
                "classes": ("extrapretty", "wide", "collapse"),
            }),
            ("Execution Options", {
                "fields": ("expires", "queue", "exchange", "routing_key"),
                "classes": ("extrapretty", "wide", "collapse"),
            }),
    )

    def __init__(self, *args, **kwargs):
        super(PeriodicTaskAdmin, self).__init__(*args, **kwargs)
        self.form = periodic_task_form()


admin.site.register(IntervalSchedule)
admin.site.register(CrontabSchedule)
admin.site.register(PeriodicTask, PeriodicTaskAdmin)
