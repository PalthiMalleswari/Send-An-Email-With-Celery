
from django.urls import path
from . import views

urlpatterns = [
    path("start/",views.start_report,name="start_report"),
    path("status/<str:task_id>/",views.report_status, name="report_status")
]
