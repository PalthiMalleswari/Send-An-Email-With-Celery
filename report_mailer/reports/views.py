from django.shortcuts import render

# Create your views here.

import json
from django.http import JsonResponse
from celery.result import AsyncResult
from .tasks import build_and_email_workbook
from django.views.decorators.csrf import csrf_exempt
from kombu.exceptions import OperationalError

@csrf_exempt
def start_report(request):
    """
    Background Job
    """

    data = json.loads(request.body or "{}")
    print(data)
    email = data.get("email")
    if not email:
        return JsonResponse({"error":"email is required"},status=400)
    try:

        task = build_and_email_workbook.delay(email,title="Sales Report")

    except OperationalError:
        return JsonResponse(
            {"error":"Service temporarily unavailable, Please retry"},
            status=503,
        )
    return JsonResponse({"task_id":task.id},status=202)

@csrf_exempt
def report_status(request,task_id):
    """
    React Poll this to see if job is done.
    """

    result = AsyncResult(task_id)
    payload = {"task_id":task_id,"status":result.state}

    if result.ready():
        if result.successful():

            payload["status"] = result.result
        else:
            payload["error"] = str(result.result)

    return JsonResponse(payload)